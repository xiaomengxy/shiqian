from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal, engine, get_db
from app.migrations import ensure_runtime_schema
from app.models import Base, Bookmark, Directory, ParseJob, ParseSession, Tag
from app.schemas import (
    ConfirmBookmarkRequest,
    DirectoryBulkDeleteRequest,
    DirectoryChildRequest,
    DirectoryCreateRequest,
    DirectoryMoveRequest,
    MoveBookmarkRequest,
    DirectoryRenameRequest,
    ParseItemsRequest,
    ParseLinksRequest,
    SummaryRewriteRequest,
    UpdateBookmarkRequest,
)
from app.services.directories import (
    get_or_create_directory_path,
    list_directory_paths,
    move_directory,
    normalize_directory_path,
    rename_directory,
)
from app.services.llm import generate_suggestion, normalize_suggestion, rewrite_summary
from app.services.parser import fetch_and_extract
from app.services.tags import get_or_create_tags, normalize_tags
from app.services.items import InputItem, content_fingerprint, group_mixed_input, normalize_keywords, parse_mixed_input
from app.services.settings_store import get_effective_settings, mask_secret, save_frontend_settings
from app.services.time_display import local_datetime
from app.services.urls import canonicalize_url, extract_urls


BASE_DIR = Path(__file__).resolve().parent
UNCATEGORIZED_PATH = "未分类"
UNCATEGORIZED_FILTER = "__none__"


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema(engine)
    yield


app = FastAPI(title="拾签", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")
templates.env.filters["local_datetime"] = local_datetime


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    runtime_settings = get_effective_settings(db, settings)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "settings": runtime_settings,
            "recent_jobs": _recent_jobs(db, limit=5),
            "has_openai_key": bool(runtime_settings.openai_api_key),
            "has_deepseek_key": bool(runtime_settings.deepseek_api_key),
        },
    )


@app.post("/items/parse")
def parse_items_form(
    background_tasks: BackgroundTasks,
    input_text: str = Form(...),
    provider: str | None = Form(None),
    db: Session = Depends(get_db),
):
    runtime_settings = get_effective_settings(db, settings)
    items = _preview_group_input(input_text, provider, runtime_settings)
    if not items:
        raise HTTPException(status_code=400, detail="No content found")
    session = _create_parse_session(db, input_text, provider, items)
    return RedirectResponse(f"/grouping/{session.id}", status_code=303)


@app.get("/grouping/{session_id}")
def grouping_review(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.get(ParseSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Grouping session not found")
    return templates.TemplateResponse(
        request,
        "grouping.html",
        {
            "request": request,
            "session": session,
            "groups": session.groups_json or [],
        },
    )


@app.post("/grouping/{session_id}/all-in-one")
def grouping_all_in_one(session_id: int, db: Session = Depends(get_db)):
    session = db.get(ParseSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Grouping session not found")
    item = _input_item_from_group(
        session.raw_input,
        reason="用户选择全部作为一条收藏",
        confidence=1.0,
        grouping_source="user",
    )
    session.groups_json = [_group_payload(item)]
    session.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/grouping/{session.id}", status_code=303)


@app.post("/grouping/{session_id}/split-blank")
def grouping_split_blank(session_id: int, db: Session = Depends(get_db)):
    session = db.get(ParseSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Grouping session not found")
    items = parse_mixed_input(session.raw_input)
    if not items:
        raise HTTPException(status_code=400, detail="No content found")
    session.groups_json = [_group_payload(item) for item in items]
    session.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(f"/grouping/{session.id}", status_code=303)


@app.post("/grouping/{session_id}/confirm")
async def grouping_confirm(
    session_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    session = db.get(ParseSession, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Grouping session not found")
    form = await request.form()
    raw_inputs = [str(value).strip() for value in form.getlist("raw_input")]
    source_types = [str(value).strip() for value in form.getlist("source_type")]
    items: list[InputItem] = []
    for index, raw_input in enumerate(raw_inputs):
        if not raw_input:
            continue
        source_type = source_types[index] if index < len(source_types) else ""
        items.append(
            _input_item_from_group(
                raw_input,
                source_type=source_type,
                reason="用户确认分组",
                confidence=1.0,
                grouping_source="user",
            )
        )
    if not items:
        raise HTTPException(status_code=400, detail="No content found")
    session.groups_json = [_group_payload(item) for item in items]
    session.status = "confirmed"
    session.updated_at = datetime.utcnow()
    db.commit()
    for item in items:
        job = _create_job(db, item, session.provider)
        background_tasks.add_task(_process_job_in_background, job.id, session.provider)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/links/parse")
def parse_links_form(
    background_tasks: BackgroundTasks,
    urls: str = Form(...),
    provider: str | None = Form(None),
    db: Session = Depends(get_db),
):
    parsed_urls = extract_urls(urls)
    if not parsed_urls:
        raise HTTPException(status_code=400, detail="No URLs found")
    for url in parsed_urls:
        job = _create_job(db, InputItem(raw_input=url, source_type="url"), provider)
        background_tasks.add_task(_process_job_in_background, job.id, provider)
    return RedirectResponse("/jobs", status_code=303)


@app.get("/jobs")
def jobs(request: Request, show_saved: bool = False, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        _jobs_context(db, show_saved=show_saved),
    )


@app.get("/jobs/partials", response_class=HTMLResponse)
def jobs_partial(request: Request, show_saved: bool = False, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "_jobs_list.html",
        {"request": request, **_jobs_context(db, show_saved=show_saved)},
    )


@app.post("/jobs/cleanup")
def cleanup_jobs(db: Session = Depends(get_db)):
    now = datetime.utcnow()
    jobs = db.scalars(
        select(ParseJob).where(ParseJob.deleted_at.is_(None), ParseJob.status.in_(["completed", "failed", "saved"]))
    ).all()
    for job in jobs:
        job.deleted_at = now
        job.updated_at = now
    db.commit()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ["pending", "processing"]:
        raise HTTPException(status_code=400, detail="Running jobs cannot be deleted")
    job.deleted_at = datetime.utcnow()
    job.updated_at = job.deleted_at
    db.commit()
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{job_id}/retry")
def retry_job(job_id: int, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    job = _prepare_job_retry(db, job_id)
    background_tasks.add_task(_process_job_in_background, job.id, job.provider)
    return RedirectResponse("/jobs", status_code=303)


@app.post("/jobs/{job_id}/restore")
def restore_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job.deleted_at = None
    job.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/trash", status_code=303)


@app.post("/jobs/{job_id}/purge")
def purge_job(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.delete(job)
    db.commit()
    return RedirectResponse("/trash", status_code=303)


@app.get("/review/{job_id}")
def review(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    suggestion = _normalized_job_suggestion(db, job) or {}
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "request": request,
            "job": job,
            "extracted": job.extracted_json or {},
            "suggestion": suggestion,
            "directories": db.scalars(select(Directory).order_by(Directory.path)).all(),
            "directory_alternatives": _directory_alternatives(db, suggestion, job.extracted_json or {}),
            "tags": db.scalars(select(Tag).order_by(Tag.name)).all(),
        },
    )


@app.post("/bookmarks/confirm")
def confirm_bookmark_form(
    job_id: int = Form(...),
    directory_path: str = Form(...),
    tags: str = Form(""),
    keywords: str = Form(""),
    summary: str = Form(...),
    name: str = Form(""),
    title: str = Form(""),
    db: Session = Depends(get_db),
):
    _confirm_bookmark(
        db,
        job_id,
        directory_path,
        normalize_tags(tags),
        normalize_keywords(keywords),
        summary,
        name or title or None,
    )
    return RedirectResponse("/bookmarks", status_code=303)


@app.get("/bookmarks")
def bookmarks(
    request: Request,
    query: str = "",
    tag: str = "",
    directory: str = "",
    sort: str = "updated",
    tree_view: str = "structure",
    db: Session = Depends(get_db),
):
    context = _bookmark_browser_context(db, query, tag, directory, sort, tree_view)
    return templates.TemplateResponse(
        request,
        "bookmarks.html",
        {"request": request, **context},
    )


@app.get("/bookmarks/partials", response_class=HTMLResponse)
def bookmarks_partial(
    request: Request,
    query: str = "",
    tag: str = "",
    directory: str = "",
    sort: str = "updated",
    tree_view: str = "structure",
    db: Session = Depends(get_db),
):
    return templates.TemplateResponse(
        request,
        "_bookmarks_browser.html",
        {"request": request, **_bookmark_browser_context(db, query, tag, directory, sort, tree_view)},
    )


@app.get("/bookmarks/{bookmark_id}/open")
def open_bookmark(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if bookmark.source_type != "url" or not bookmark.url:
        return RedirectResponse("/bookmarks", status_code=303)
    bookmark.opened_count = (bookmark.opened_count or 0) + 1
    bookmark.last_opened_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(bookmark.url, status_code=302)


@app.get("/bookmarks/{bookmark_id}/edit")
def edit_bookmark_page(bookmark_id: int, request: Request, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    return templates.TemplateResponse(
        request,
        "bookmark_edit.html",
        {
            "request": request,
            "bookmark": bookmark,
            "directories": db.scalars(select(Directory).order_by(Directory.path)).all(),
            "tag_text": ", ".join(tag.name for tag in bookmark.tags),
            "keyword_text": ", ".join(bookmark.keywords or []),
        },
    )


@app.post("/bookmarks/{bookmark_id}/edit")
def update_bookmark_form(
    bookmark_id: int,
    name: str = Form(...),
    summary: str = Form(...),
    directory_path: str = Form(...),
    tags: str = Form(""),
    keywords: str = Form(""),
    raw_input: str = Form(""),
    url: str = Form(""),
    db: Session = Depends(get_db),
):
    _update_bookmark(
        db,
        bookmark_id,
        name=name,
        summary=summary,
        directory_path=directory_path,
        tags=normalize_tags(tags),
        keywords=normalize_keywords(keywords),
        raw_input=raw_input,
        url=url or None,
    )
    return RedirectResponse("/bookmarks", status_code=303)


@app.post("/bookmarks/{bookmark_id}/delete")
def delete_bookmark(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.deleted_at = datetime.utcnow()
    bookmark.updated_at = bookmark.deleted_at
    db.commit()
    return RedirectResponse("/bookmarks", status_code=303)


@app.post("/bookmarks/{bookmark_id}/restore")
def restore_bookmark(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.deleted_at = None
    bookmark.updated_at = datetime.utcnow()
    db.commit()
    return RedirectResponse("/trash", status_code=303)


@app.post("/bookmarks/{bookmark_id}/purge")
def purge_bookmark(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.tags.clear()
    db.delete(bookmark)
    db.commit()
    return RedirectResponse("/trash", status_code=303)


@app.get("/directories")
def directories():
    return RedirectResponse("/bookmarks", status_code=303)


@app.get("/directories/partials", response_class=HTMLResponse)
def directories_partial():
    raise HTTPException(status_code=404, detail="Directory management moved to bookmarks")


@app.post("/directories")
def create_directory():
    return RedirectResponse("/bookmarks", status_code=303)


@app.post("/directories/{directory_id}/children")
def create_child_directory(directory_id: int):
    return RedirectResponse("/bookmarks", status_code=303)


@app.post("/directories/{directory_id}/rename")
def rename_directory_route(directory_id: int):
    return RedirectResponse("/bookmarks", status_code=303)


@app.post("/directories/{directory_id}/move")
def move_directory_route(directory_id: int):
    return RedirectResponse("/bookmarks", status_code=303)


@app.get("/trash")
def trash(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "trash.html",
        {"request": request, **_trash_context(db)},
    )


@app.get("/trash/partials", response_class=HTMLResponse)
def trash_partial(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "_trash_lists.html",
        {"request": request, **_trash_context(db)},
    )


@app.get("/settings")
def settings_page(request: Request, saved: str = "", db: Session = Depends(get_db)):
    runtime_settings = get_effective_settings(db, settings)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "settings": runtime_settings,
            "has_openai_key": bool(runtime_settings.openai_api_key),
            "has_deepseek_key": bool(runtime_settings.deepseek_api_key),
            "openai_key_label": mask_secret(runtime_settings.openai_api_key),
            "deepseek_key_label": mask_secret(runtime_settings.deepseek_api_key),
            "saved": saved == "1",
        },
    )


@app.post("/settings")
def update_settings(
    llm_provider: str = Form(...),
    openai_model: str = Form("gpt-5-mini"),
    deepseek_model: str = Form("deepseek-v4-flash"),
    openai_api_key: str = Form(""),
    deepseek_api_key: str = Form(""),
    clear_openai_key: str | None = Form(None),
    clear_deepseek_key: str | None = Form(None),
    db: Session = Depends(get_db),
):
    save_frontend_settings(
        db,
        llm_provider=llm_provider,
        openai_model=openai_model,
        deepseek_model=deepseek_model,
        openai_api_key=openai_api_key,
        deepseek_api_key=deepseek_api_key,
        clear_openai_key=clear_openai_key == "on",
        clear_deepseek_key=clear_deepseek_key == "on",
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/api/items/parse")
def parse_items_api(payload: ParseItemsRequest, db: Session = Depends(get_db)):
    jobs = []
    runtime_settings = get_effective_settings(db, settings)
    items = (
        group_mixed_input(payload.input, payload.provider, runtime_settings)
        if payload.grouping_mode == "smart"
        else parse_mixed_input(payload.input)
    )
    for item in items:
        job = _create_job(db, item, payload.provider)
        _process_job(db, job.id, payload.provider)
        db.refresh(job)
        jobs.append(
            {
                "id": job.id,
                "status": job.status,
                "source_type": job.source_type,
                "stage": job.stage,
                "progress_percent": job.progress_percent,
                "group_confidence": job.group_confidence,
                "group_reason": job.group_reason,
                "grouping_source": job.grouping_source,
            }
        )
    return {"jobs": jobs}


@app.post("/api/links/parse")
def parse_links_api(payload: ParseLinksRequest, db: Session = Depends(get_db)):
    jobs = []
    for url in payload.urls:
        job = _create_job(db, InputItem(raw_input=url, source_type="url"), payload.provider)
        _process_job(db, job.id, payload.provider)
        db.refresh(job)
        jobs.append(
            {
                "id": job.id,
                "status": job.status,
                "url": job.input_url,
                "source_type": job.source_type,
                "stage": job.stage,
                "progress_percent": job.progress_percent,
                "group_confidence": job.group_confidence,
                "group_reason": job.group_reason,
                "grouping_source": job.grouping_source,
            }
        )
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
def get_job_api(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    _reconcile_saved_jobs(db, [job])
    return {
        "id": job.id,
        "input_url": job.input_url,
        "source_type": job.source_type,
        "raw_input": job.raw_input,
        "status": job.status,
        "stage": job.stage,
        "progress_percent": job.progress_percent,
        "error": job.error,
        "group_confidence": job.group_confidence,
        "group_reason": job.group_reason,
        "grouping_source": job.grouping_source,
        "extracted": job.extracted_json,
        "suggestion": _normalized_job_suggestion(db, job),
        "provider": job.provider,
        "model": job.model,
        "created_at": job.created_at.isoformat(),
        "parsed_at": job.parsed_at.isoformat() if job.parsed_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "created_at_display": local_datetime(job.created_at),
        "parsed_at_display": local_datetime(job.parsed_at),
        "updated_at_display": local_datetime(job.updated_at),
    }


@app.post("/api/jobs/{job_id}/summary/rewrite")
def rewrite_job_summary_api(job_id: int, payload: SummaryRewriteRequest, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status not in ["completed", "saved"]:
        raise HTTPException(status_code=400, detail="Job is not ready for summary rewrite")
    extracted = job.extracted_json or {}
    suggestion = _normalized_job_suggestion(db, job) or {}
    runtime_settings = get_effective_settings(db, settings)
    result = rewrite_summary(
        extracted,
        suggestion,
        payload.current_summary,
        payload.length,
        payload.style,
        payload.provider,
        runtime_settings,
    )
    next_suggestion = dict(job.suggestion_json or suggestion)
    next_suggestion["summary"] = result.summary
    if result.error:
        next_suggestion["summary_rewrite_error"] = result.error
    else:
        next_suggestion.pop("summary_rewrite_error", None)
    job.suggestion_json = next_suggestion
    job.provider = result.provider or job.provider
    job.model = result.model or job.model
    job.updated_at = datetime.utcnow()
    db.commit()
    return {
        "summary": result.summary,
        "provider": result.provider,
        "model": result.model,
        "error": result.error,
    }


@app.post("/api/jobs/{job_id}/retry")
def retry_job_api(job_id: int, db: Session = Depends(get_db)):
    job = _prepare_job_retry(db, job_id)
    _process_job(db, job.id, job.provider)
    db.refresh(job)
    return {
        "id": job.id,
        "status": job.status,
        "stage": job.stage,
        "progress_percent": job.progress_percent,
        "error": job.error,
    }


@app.post("/api/directories")
def create_directory_api(payload: DirectoryCreateRequest, db: Session = Depends(get_db)):
    if _is_uncategorized_path(payload.path):
        raise HTTPException(status_code=400, detail="未分类是系统内置归档入口，不需要创建目录。")
    directory = get_or_create_directory_path(db, payload.path)
    db.commit()
    db.refresh(directory)
    return _directory_payload(directory)


@app.post("/api/directories/{directory_id}/rename")
def rename_directory_api(directory_id: int, payload: DirectoryRenameRequest, db: Session = Depends(get_db)):
    try:
        directory = db.get(Directory, directory_id)
        if directory and directory.parent_id is None and _is_uncategorized_path(payload.name):
            raise ValueError("未分类是系统内置归档入口，不需要创建目录。")
        directory = rename_directory(db, directory_id, payload.name)
        db.commit()
        db.refresh(directory)
        return _directory_payload(directory)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/directories/{directory_id}/move")
def move_directory_api(directory_id: int, payload: DirectoryMoveRequest, db: Session = Depends(get_db)):
    try:
        directory = db.get(Directory, directory_id)
        if directory and payload.parent_id is None and _is_uncategorized_path(directory.name):
            raise ValueError("未分类是系统内置归档入口，不需要创建目录。")
        directory = move_directory(db, directory_id, payload.parent_id)
        db.commit()
        db.refresh(directory)
        return _directory_payload(directory)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/directories/{directory_id}/children")
def create_child_directory_api(directory_id: int, payload: DirectoryChildRequest, db: Session = Depends(get_db)):
    parent = db.get(Directory, directory_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="Directory not found")
    child_path = f"{parent.path}/{payload.name}"
    if _is_uncategorized_path(child_path):
        raise HTTPException(status_code=400, detail="未分类是系统内置归档入口，不需要创建目录。")
    child = get_or_create_directory_path(db, child_path)
    db.commit()
    db.refresh(child)
    return _directory_payload(child)


@app.get("/api/directories/{directory_id}/delete-preview")
def directory_delete_preview_api(directory_id: int, db: Session = Depends(get_db)):
    directory = db.get(Directory, directory_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="Directory not found")
    return _directory_delete_preview(db, [directory])


@app.delete("/api/directories/{directory_id}")
def delete_directory_api(directory_id: int, db: Session = Depends(get_db)):
    directory = db.get(Directory, directory_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="Directory not found")
    preview = _delete_directories(db, [directory])
    db.commit()
    return {**preview, "deleted": True, "redirect_directory": UNCATEGORIZED_FILTER}


@app.post("/api/directories/bulk-delete")
def bulk_delete_directories_api(payload: DirectoryBulkDeleteRequest, db: Session = Depends(get_db)):
    directories = list(db.scalars(select(Directory).where(Directory.id.in_(payload.ids))).all())
    if not directories:
        raise HTTPException(status_code=404, detail="Directories not found")
    preview = _directory_delete_preview(db, directories)
    if payload.preview:
        return preview
    preview = _delete_directories(db, directories)
    db.commit()
    return {**preview, "deleted": True, "redirect_directory": UNCATEGORIZED_FILTER}


@app.delete("/api/jobs/{job_id}")
def delete_job_api(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ["pending", "processing"]:
        raise HTTPException(status_code=400, detail="Running jobs cannot be deleted")
    job.deleted_at = datetime.utcnow()
    job.updated_at = job.deleted_at
    db.commit()
    return {"id": job.id, "deleted_at": job.deleted_at.isoformat()}


@app.post("/api/jobs/{job_id}/restore")
def restore_job_api(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job.deleted_at = None
    job.updated_at = datetime.utcnow()
    db.commit()
    return {"id": job.id, "restored": True}


@app.delete("/api/jobs/{job_id}/purge")
def purge_job_api(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    db.delete(job)
    db.commit()
    return {"id": job_id, "purged": True}


@app.post("/api/bookmarks/confirm")
def confirm_bookmark_api(payload: ConfirmBookmarkRequest, db: Session = Depends(get_db)):
    bookmark = _confirm_bookmark(
        db,
        payload.job_id,
        payload.directory_path,
        payload.tags,
        payload.keywords,
        payload.summary,
        payload.name or payload.title,
    )
    return {"id": bookmark.id, "canonical_url": bookmark.canonical_url}


@app.get("/api/bookmarks")
def bookmarks_api(
    query: str = "",
    tag: str = "",
    directory: str = "",
    sort: str = "updated",
    db: Session = Depends(get_db),
):
    stmt = (
        select(Bookmark)
        .options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
        .where(Bookmark.deleted_at.is_(None))
    )
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Bookmark.title.ilike(like),
                Bookmark.summary.ilike(like),
                Bookmark.raw_input.ilike(like),
                Bookmark.url.ilike(like),
                cast(Bookmark.keywords, String).ilike(like),
            )
        )
    if _is_uncategorized_filter(directory):
        stmt = stmt.outerjoin(Bookmark.directory).where(
            or_(
                Bookmark.directory_id.is_(None),
                Directory.path == UNCATEGORIZED_PATH,
                Directory.path.like(f"{UNCATEGORIZED_PATH}/%"),
            )
        )
    elif directory:
        stmt = stmt.join(Bookmark.directory).where(Directory.path.like(f"{normalize_directory_path(directory)}%"))
    if tag:
        stmt = stmt.join(Bookmark.tags).where(Tag.name == tag)
    stmt = _apply_bookmark_sort(stmt, sort)
    return {
        "bookmarks": [
            {
                "id": item.id,
                "url": item.url if item.source_type == "url" else None,
                "source_type": item.source_type,
                "raw_input": item.raw_input,
                "title": item.title,
                "name": item.title,
                "summary": item.summary,
                "keywords": item.keywords or [],
                "opened_count": item.opened_count or 0,
                "last_opened_at": item.last_opened_at.isoformat() if item.last_opened_at else None,
                "parsed_at": item.parsed_at.isoformat() if item.parsed_at else None,
                "created_at": item.created_at.isoformat(),
                "updated_at": item.updated_at.isoformat() if item.updated_at else None,
                "last_opened_at_display": local_datetime(item.last_opened_at),
                "parsed_at_display": local_datetime(item.parsed_at),
                "created_at_display": local_datetime(item.created_at),
                "updated_at_display": local_datetime(item.updated_at),
                "content_type": item.content_type,
                "directory": item.directory.path if item.directory else None,
                "tags": [tag.name for tag in item.tags],
            }
            for item in db.scalars(stmt).unique().all()
        ]
    }


@app.post("/api/bookmarks/{bookmark_id}")
def update_bookmark_api(bookmark_id: int, payload: UpdateBookmarkRequest, db: Session = Depends(get_db)):
    bookmark = _update_bookmark(
        db,
        bookmark_id,
        name=payload.name,
        summary=payload.summary,
        directory_path=payload.directory_path,
        tags=payload.tags,
        keywords=payload.keywords,
        raw_input=payload.raw_input,
        url=payload.url,
    )
    return {"id": bookmark.id, "updated": True}


@app.post("/api/bookmarks/{bookmark_id}/move")
def move_bookmark_api(bookmark_id: int, payload: MoveBookmarkRequest, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    directory = db.get(Directory, payload.directory_id) if payload.directory_id else None
    if payload.directory_id and directory is None:
        raise HTTPException(status_code=404, detail="Directory not found")
    if directory and _is_uncategorized_path(directory.path):
        directory = None
    bookmark.directory = directory
    bookmark.updated_at = datetime.utcnow()
    db.commit()
    return {
        "id": bookmark.id,
        "directory_id": directory.id if directory else None,
        "directory": directory.path if directory else UNCATEGORIZED_PATH,
    }


@app.delete("/api/bookmarks/{bookmark_id}")
def delete_bookmark_api(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.deleted_at = datetime.utcnow()
    bookmark.updated_at = bookmark.deleted_at
    db.commit()
    return {"id": bookmark.id, "deleted_at": bookmark.deleted_at.isoformat()}


@app.post("/api/bookmarks/{bookmark_id}/restore")
def restore_bookmark_api(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.deleted_at = None
    bookmark.updated_at = datetime.utcnow()
    db.commit()
    return {"id": bookmark.id, "restored": True}


@app.delete("/api/bookmarks/{bookmark_id}/purge")
def purge_bookmark_api(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.tags.clear()
    db.delete(bookmark)
    db.commit()
    return {"id": bookmark_id, "purged": True}


@app.post("/api/bookmarks/{bookmark_id}/open")
def open_bookmark_api(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.opened_count = (bookmark.opened_count or 0) + 1
    bookmark.last_opened_at = datetime.utcnow()
    db.commit()
    return {
        "id": bookmark.id,
        "opened_count": bookmark.opened_count,
        "last_opened_at": bookmark.last_opened_at.isoformat() if bookmark.last_opened_at else None,
    }


def _create_parse_session(db: Session, raw_input: str, provider: str | None, items: list[InputItem]) -> ParseSession:
    session = ParseSession(
        raw_input=raw_input,
        provider=provider,
        groups_json=[_group_payload(item) for item in items],
        status="draft",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _preview_group_input(raw_input: str, provider: str | None, runtime_settings) -> list[InputItem]:
    if not extract_urls(raw_input):
        return [
            _input_item_from_group(
                raw_input,
                reason="纯文本默认先作为一条，空行只作为排版线索",
                confidence=0.72,
                grouping_source="review_default",
            )
        ]
    return group_mixed_input(raw_input, provider, runtime_settings)


def _group_payload(item: InputItem) -> dict:
    return {
        "raw_input": item.raw_input,
        "source_type": item.source_type,
        "primary_url": item.primary_url,
        "group_confidence": item.group_confidence,
        "group_reason": item.group_reason,
        "grouping_source": item.grouping_source,
    }


def _input_item_from_group(
    raw_input: str,
    *,
    source_type: str | None = None,
    reason: str = "用户确认分组",
    confidence: float = 1.0,
    grouping_source: str = "user",
) -> InputItem:
    raw_input = raw_input.strip()
    urls = extract_urls(raw_input)
    clean_source_type = (source_type or "").strip()
    if clean_source_type not in {"url", "term", "text"}:
        if urls:
            clean_source_type = "url"
        else:
            clean_source_type = "term" if len(raw_input) <= 40 and "\n" not in raw_input else "text"
    primary_url = urls[0] if clean_source_type == "url" and urls else None
    if clean_source_type == "url" and primary_url is None:
        clean_source_type = "text"
    return InputItem(
        raw_input=raw_input,
        source_type=clean_source_type,
        primary_url=primary_url,
        group_confidence=confidence,
        group_reason=reason,
        grouping_source=grouping_source,
    )


def _create_job(db: Session, item: InputItem, provider: str | None) -> ParseJob:
    runtime_settings = get_effective_settings(db, settings)
    job = ParseJob(
        input_url=item.primary_url or item.raw_input,
        raw_input=item.raw_input,
        source_type=item.source_type,
        status="pending",
        stage="等待处理",
        progress_percent=0,
        provider=provider or runtime_settings.llm_provider,
        group_confidence=item.group_confidence,
        group_reason=item.group_reason,
        grouping_source=item.grouping_source,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _process_job_in_background(job_id: int, provider: str | None) -> None:
    db = SessionLocal()
    try:
        _process_job(db, job_id, provider)
    finally:
        db.close()


def _process_job(db: Session, job_id: int, provider: str | None) -> None:
    job = db.get(ParseJob, job_id)
    if job is None:
        return
    _set_job_progress(db, job, "processing", "准备处理", 5)
    try:
        if job.source_type == "url":
            _set_job_progress(db, job, "processing", "抓取链接内容", 20)
        else:
            _set_job_progress(db, job, "processing", "整理文本内容", 20)
        extracted = _extract_job_input(job)
        _set_job_progress(db, job, "processing", "生成名称、标签和目录建议", 65)
        runtime_settings = get_effective_settings(db, settings)
        suggestion_result = generate_suggestion(extracted, list_directory_paths(db), provider, runtime_settings)
        suggestion = suggestion_result.suggestion
        if suggestion_result.error:
            suggestion["llm_error"] = suggestion_result.error
        job.extracted_json = extracted
        job.suggestion_json = suggestion
        job.provider = suggestion_result.provider
        job.model = suggestion_result.model
        job.status = "completed"
        job.stage = "等待确认"
        job.progress_percent = 100
        job.parsed_at = datetime.utcnow()
        job.updated_at = job.parsed_at
        job.error = None
    except Exception as exc:
        job.status = "failed"
        job.stage = "处理失败"
        job.progress_percent = 100
        job.updated_at = datetime.utcnow()
        job.error = str(exc)
    db.commit()


def _set_job_progress(db: Session, job: ParseJob, status: str, stage: str, progress_percent: int) -> None:
    job.status = status
    job.stage = stage
    job.progress_percent = max(0, min(100, progress_percent))
    job.updated_at = datetime.utcnow()
    db.commit()


def _normalized_job_suggestion(db: Session, job: ParseJob) -> dict | None:
    if not job.suggestion_json:
        return None
    return normalize_suggestion(job.suggestion_json, list_directory_paths(db))


def _directory_alternatives(db: Session, suggestion: dict, extracted: dict) -> list[dict]:
    directories = db.scalars(
        select(Directory)
        .where(Directory.path != UNCATEGORIZED_PATH, ~Directory.path.like(f"{UNCATEGORIZED_PATH}/%"))
        .order_by(Directory.path)
    ).all()
    values: list[dict] = []
    seen: set[str] = set()

    def add(path: str, reason: str, weight: int = 0) -> None:
        normalized = UNCATEGORIZED_PATH if _is_uncategorized_path(path) else normalize_directory_path(path)
        if normalized in seen:
            return
        seen.add(normalized)
        values.append({"path": normalized, "reason": reason, "weight": weight})

    add(suggestion.get("recommended_directory_path") or UNCATEGORIZED_PATH, "当前推荐", 100)
    raw_path = suggestion.get("raw_recommended_directory_path")
    if raw_path and raw_path != suggestion.get("recommended_directory_path"):
        add(raw_path, "原始 AI 建议", 80)
    add(UNCATEGORIZED_PATH, "不确定时先暂存", 70)

    terms = {
        str(value).lower()
        for value in [
            extracted.get("title"),
            extracted.get("source_domain"),
            suggestion.get("name"),
            *(suggestion.get("tags") or []),
            *(suggestion.get("keywords") or []),
        ]
        if value
    }
    scored: list[tuple[int, Directory]] = []
    for directory in directories:
        haystack = directory.path.lower()
        score = sum(1 for term in terms if term and (term in haystack or haystack in term))
        if score:
            scored.append((score, directory))
    for score, directory in sorted(scored, key=lambda item: (-item[0], item[1].path))[:4]:
        add(directory.path, "匹配标签或关键词", 50 + score)
    for directory in directories[:4]:
        add(directory.path, "已有目录", 10)
        if len(values) >= 6:
            break
    return values[:6]


def _directory_payload(directory: Directory) -> dict:
    return {
        "id": directory.id,
        "name": directory.name,
        "path": directory.path,
        "parent_id": directory.parent_id,
        "depth": directory.depth,
    }


def _directory_delete_preview(db: Session, directories: list[Directory]) -> dict:
    roots = _compact_directory_roots(directories)
    if not roots:
        raise HTTPException(status_code=404, detail="Directories not found")
    subtree = _directory_subtree(db, roots)
    bookmark_count = (
        db.scalar(
            select(func.count(Bookmark.id)).where(
                Bookmark.deleted_at.is_(None),
                Bookmark.directory_id.in_([directory.id for directory in subtree]),
            )
        )
        if subtree
        else 0
    ) or 0
    return {
        "directory_count": len(subtree),
        "root_count": len(roots),
        "children_count": max(len(subtree) - len(roots), 0),
        "bookmark_count": bookmark_count,
        "paths": [directory.path for directory in roots],
    }


def _delete_directories(db: Session, directories: list[Directory]) -> dict:
    roots = _compact_directory_roots(directories)
    preview = _directory_delete_preview(db, roots)
    subtree = _directory_subtree(db, roots)
    subtree_ids = [directory.id for directory in subtree]
    if subtree_ids:
        for bookmark in db.scalars(select(Bookmark).where(Bookmark.directory_id.in_(subtree_ids))):
            bookmark.directory_id = None
            bookmark.updated_at = datetime.utcnow()
    for directory in roots:
        db.delete(directory)
    return preview


def _compact_directory_roots(directories: list[Directory]) -> list[Directory]:
    valid = [directory for directory in directories if directory and not _is_uncategorized_path(directory.path)]
    if len(valid) != len(directories):
        raise HTTPException(status_code=400, detail="Built-in uncategorized directory cannot be deleted")
    ordered = sorted({directory.id: directory for directory in valid}.values(), key=lambda item: item.path)
    roots: list[Directory] = []
    for directory in ordered:
        if any(directory.path == root.path or directory.path.startswith(f"{root.path}/") for root in roots):
            continue
        roots.append(directory)
    return roots


def _directory_subtree(db: Session, roots: list[Directory]) -> list[Directory]:
    if not roots:
        return []
    conditions = []
    for directory in roots:
        conditions.append(Directory.path == directory.path)
        conditions.append(Directory.path.like(f"{directory.path}/%"))
    return list(db.scalars(select(Directory).where(or_(*conditions)).order_by(Directory.path)).all())


def _confirm_bookmark(
    db: Session,
    job_id: int,
    directory_path: str,
    tags: list[str],
    keywords: list[str],
    summary: str,
    name: str | None = None,
) -> Bookmark:
    job = db.get(ParseJob, job_id)
    if job is None or not job.extracted_json:
        raise HTTPException(status_code=404, detail="Completed job not found")
    extracted = job.extracted_json
    suggestion = _normalized_job_suggestion(db, job) or {}
    directory = _bookmark_directory_from_path(
        db, directory_path or suggestion.get("recommended_directory_path") or UNCATEGORIZED_PATH
    )
    source_type = job.source_type or extracted.get("source_type") or "url"
    final_keywords = normalize_keywords(keywords or suggestion.get("keywords") or [])
    canonical_url = None
    content_hash = None
    if source_type == "url":
        canonical_url = canonicalize_url(extracted.get("canonical_url") or extracted.get("url") or job.raw_input)
        bookmark = db.scalar(select(Bookmark).where(Bookmark.canonical_url == canonical_url))
    else:
        content_hash = content_fingerprint(source_type, extracted.get("raw_input") or job.raw_input)
        bookmark = db.scalar(select(Bookmark).where(Bookmark.content_hash == content_hash))
    if bookmark is None:
        bookmark = Bookmark(
            url=extracted.get("url") if source_type == "url" else None,
            canonical_url=canonical_url,
            content_hash=content_hash,
            source_type=source_type,
            raw_input=extracted.get("raw_input") or job.raw_input,
            keywords=final_keywords,
            title=name or suggestion.get("name") or extracted.get("title") or "未命名收藏",
            summary=summary,
            content_type=suggestion.get("content_type") or extracted.get("content_type") or "webpage",
            source_domain=extracted.get("source_domain") or "",
            directory=directory,
            parsed_at=job.parsed_at or job.updated_at or job.created_at,
            status="saved",
        )
        db.add(bookmark)
    else:
        bookmark.title = name or suggestion.get("name") or bookmark.title
        bookmark.summary = summary
        bookmark.content_type = suggestion.get("content_type") or extracted.get("content_type") or bookmark.content_type
        bookmark.source_domain = extracted.get("source_domain") or bookmark.source_domain
        bookmark.raw_input = extracted.get("raw_input") or job.raw_input
        bookmark.keywords = final_keywords
        bookmark.directory = directory
        bookmark.parsed_at = bookmark.parsed_at or job.parsed_at or job.updated_at or job.created_at
        bookmark.deleted_at = None
    bookmark.tags = get_or_create_tags(db, tags)
    if job.parsed_at is None:
        job.parsed_at = job.updated_at or job.created_at
    job.status = "saved"
    job.stage = "已保存到收藏"
    job.progress_percent = 100
    job.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(bookmark)
    return bookmark


def _update_bookmark(
    db: Session,
    bookmark_id: int,
    *,
    name: str,
    summary: str,
    directory_path: str,
    tags: list[str],
    keywords: list[str],
    raw_input: str,
    url: str | None,
) -> Bookmark:
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None or bookmark.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Bookmark not found")

    directory = _bookmark_directory_from_path(db, directory_path or UNCATEGORIZED_PATH)
    bookmark.title = (name or "").strip() or bookmark.title
    bookmark.summary = (summary or "").strip()
    bookmark.raw_input = raw_input.strip() or bookmark.raw_input
    bookmark.keywords = normalize_keywords(keywords)
    bookmark.directory = directory
    bookmark.updated_at = datetime.utcnow()

    if bookmark.source_type == "url":
        final_url = (url or bookmark.url or "").strip()
        if final_url:
            canonical_url = canonicalize_url(final_url)
            duplicate = db.scalar(
                select(Bookmark).where(Bookmark.canonical_url == canonical_url, Bookmark.id != bookmark.id)
            )
            if duplicate:
                raise HTTPException(status_code=400, detail="URL already exists")
            bookmark.url = final_url
            bookmark.canonical_url = canonical_url
            bookmark.source_domain = urlparse(final_url).netloc.lower()
            bookmark.raw_input = bookmark.raw_input or final_url
    else:
        new_hash = content_fingerprint(bookmark.source_type, bookmark.raw_input)
        duplicate = db.scalar(select(Bookmark).where(Bookmark.content_hash == new_hash, Bookmark.id != bookmark.id))
        if duplicate:
            raise HTTPException(status_code=400, detail="Content already exists")
        bookmark.content_hash = new_hash
        bookmark.url = None
        bookmark.canonical_url = None
        bookmark.source_domain = ""

    bookmark.tags = get_or_create_tags(db, tags)
    db.commit()
    db.refresh(bookmark)
    return bookmark


def _recent_jobs(db: Session, limit: int, show_saved: bool = False) -> list[ParseJob]:
    jobs = list(
        db.scalars(
            select(ParseJob).where(ParseJob.deleted_at.is_(None)).order_by(ParseJob.created_at.desc()).limit(limit * 2)
        ).all()
    )
    _reconcile_saved_jobs(db, jobs)
    if not show_saved:
        jobs = [job for job in jobs if job.status != "saved"]
    jobs = jobs[:limit]
    return jobs


def _jobs_context(db: Session, show_saved: bool = False) -> dict:
    jobs = _recent_jobs(db, limit=100, show_saved=show_saved)
    saved_count = db.scalar(
        select(func.count(ParseJob.id)).where(ParseJob.deleted_at.is_(None), ParseJob.status == "saved")
    ) or 0
    return {
        "jobs": jobs,
        "has_active_jobs": _has_active_jobs(db),
        "show_saved": show_saved,
        "saved_count": saved_count,
    }


def _prepare_job_retry(db: Session, job_id: int) -> ParseJob:
    job = db.get(ParseJob, job_id)
    if job is None or job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    if job.status in ["pending", "processing"]:
        raise HTTPException(status_code=400, detail="Running jobs cannot be retried")
    if job.status == "saved":
        raise HTTPException(status_code=400, detail="Saved jobs cannot be retried")
    job.status = "pending"
    job.stage = "等待重试"
    job.progress_percent = 0
    job.error = None
    job.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(job)
    return job


def _reconcile_saved_jobs(db: Session, jobs: list[ParseJob]) -> None:
    changed = False
    for job in jobs:
        if job.status != "completed":
            continue
        if _matching_bookmark_for_job(db, job) is None:
            continue
        if job.parsed_at is None:
            job.parsed_at = job.updated_at or job.created_at
        job.status = "saved"
        job.stage = "已保存到收藏"
        job.progress_percent = 100
        job.updated_at = datetime.utcnow()
        changed = True
    if changed:
        db.commit()


def _matching_bookmark_for_job(db: Session, job: ParseJob) -> Bookmark | None:
    extracted = job.extracted_json or {}
    source_type = job.source_type or extracted.get("source_type") or "url"
    if source_type == "url":
        url = extracted.get("canonical_url") or extracted.get("url") or job.raw_input or job.input_url
        if not url:
            return None
        canonical_url = canonicalize_url(url)
        return db.scalar(
            select(Bookmark).where(Bookmark.deleted_at.is_(None), Bookmark.canonical_url == canonical_url)
        )
    raw_input = extracted.get("raw_input") or job.raw_input or job.input_url
    if not raw_input:
        return None
    content_hash = content_fingerprint(source_type, raw_input)
    return db.scalar(select(Bookmark).where(Bookmark.deleted_at.is_(None), Bookmark.content_hash == content_hash))


def _bookmark_browser_context(db: Session, query: str, tag: str, directory: str, sort: str, tree_view: str) -> dict:
    tree_view = tree_view if tree_view in {"structure", "items"} else "structure"
    directory_items = db.scalars(
        select(Directory)
        .where(Directory.path != UNCATEGORIZED_PATH, ~Directory.path.like(f"{UNCATEGORIZED_PATH}/%"))
        .order_by(Directory.path)
    ).all()
    direct_counts = _directory_bookmark_counts(db)
    directory_tree = _build_directory_tree(directory_items, direct_counts)
    items = _query_bookmarks(db, query=query, tag=tag, directory=directory, sort=sort)
    directory_bookmarks_by_id: dict[int, list[Bookmark]] = {}
    uncategorized_bookmarks: list[Bookmark] = []
    if tree_view == "items":
        directory_bookmarks_by_id, uncategorized_bookmarks = _directory_tree_bookmarks(db)
    all_count = db.scalar(select(func.count(Bookmark.id)).where(Bookmark.deleted_at.is_(None))) or 0
    uncategorized_count = (
        db.scalar(
            select(func.count(Bookmark.id))
            .outerjoin(Bookmark.directory)
            .where(
                Bookmark.deleted_at.is_(None),
                or_(
                    Bookmark.directory_id.is_(None),
                    Directory.path == UNCATEGORIZED_PATH,
                    Directory.path.like(f"{UNCATEGORIZED_PATH}/%"),
                ),
            )
        )
        or 0
    )
    current_directory = None
    if directory and not _is_uncategorized_filter(directory):
        current_directory = db.scalar(select(Directory).where(Directory.path == normalize_directory_path(directory)))
    view_title = "全部收藏"
    if _is_uncategorized_filter(directory):
        view_title = UNCATEGORIZED_PATH
    elif current_directory:
        view_title = current_directory.path
    if tag:
        view_title = f"{view_title} · #{tag}"
    if query:
        view_title = f"{view_title} · 搜索“{query}”"
    return {
        "bookmarks": items,
        "bookmark_count": len(items),
        "all_count": all_count,
        "uncategorized_count": uncategorized_count,
        "directories": directory_items,
        "directory_tree": directory_tree,
        "directory_bookmarks_by_id": directory_bookmarks_by_id,
        "uncategorized_bookmarks": uncategorized_bookmarks,
        "tags": db.scalars(select(Tag).order_by(Tag.name)).all(),
        "query": query,
        "tag_filter": tag,
        "directory_filter": directory,
        "current_directory": current_directory,
        "directory_count": len(directory_items),
        "sort": sort,
        "tree_view": tree_view,
        "view_title": view_title,
    }


def _query_bookmarks(db: Session, *, query: str, tag: str, directory: str, sort: str) -> list[Bookmark]:
    stmt = (
        select(Bookmark)
        .options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
        .where(Bookmark.deleted_at.is_(None))
    )
    if query:
        like = f"%{query}%"
        stmt = stmt.where(
            or_(
                Bookmark.title.ilike(like),
                Bookmark.summary.ilike(like),
                Bookmark.raw_input.ilike(like),
                Bookmark.url.ilike(like),
                cast(Bookmark.keywords, String).ilike(like),
            )
        )
    if _is_uncategorized_filter(directory):
        stmt = stmt.outerjoin(Bookmark.directory).where(
            or_(
                Bookmark.directory_id.is_(None),
                Directory.path == UNCATEGORIZED_PATH,
                Directory.path.like(f"{UNCATEGORIZED_PATH}/%"),
            )
        )
    elif directory:
        stmt = stmt.join(Bookmark.directory).where(Directory.path.like(f"{normalize_directory_path(directory)}%"))
    if tag:
        stmt = stmt.join(Bookmark.tags).where(Tag.name == tag)
    stmt = _apply_bookmark_sort(stmt, sort)
    return db.scalars(stmt).unique().all()


def _apply_bookmark_sort(stmt, sort: str):
    if sort == "opened":
        return stmt.order_by(Bookmark.last_opened_at.desc(), Bookmark.updated_at.desc())
    if sort == "open_count":
        return stmt.order_by(Bookmark.opened_count.desc(), Bookmark.updated_at.desc())
    return stmt.order_by(Bookmark.updated_at.desc())


def _has_active_jobs(db: Session) -> bool:
    return (
        db.scalar(
            select(ParseJob.id)
            .where(ParseJob.deleted_at.is_(None), ParseJob.status.in_(["pending", "processing"]))
            .order_by(ParseJob.created_at.desc())
            .limit(1)
        )
        is not None
    )


def _directory_bookmark_counts(db: Session) -> dict[int, int]:
    rows = db.execute(
        select(Bookmark.directory_id, func.count(Bookmark.id))
        .where(Bookmark.deleted_at.is_(None), Bookmark.directory_id.is_not(None))
        .group_by(Bookmark.directory_id)
    ).all()
    return {directory_id: count for directory_id, count in rows if directory_id is not None}


def _directory_tree_bookmarks(db: Session) -> tuple[dict[int, list[Bookmark]], list[Bookmark]]:
    bookmarks = db.scalars(
        select(Bookmark)
        .options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
        .where(Bookmark.deleted_at.is_(None))
        .order_by(Bookmark.updated_at.desc())
    ).unique().all()
    by_directory: dict[int, list[Bookmark]] = {}
    uncategorized: list[Bookmark] = []
    for bookmark in bookmarks:
        if bookmark.directory_id is None or bookmark.directory is None or _is_uncategorized_path(bookmark.directory.path):
            uncategorized.append(bookmark)
            continue
        by_directory.setdefault(bookmark.directory_id, []).append(bookmark)
    return by_directory, uncategorized


def _bookmark_directory_from_path(db: Session, path: str) -> Directory | None:
    if _is_uncategorized_path(path):
        return None
    return get_or_create_directory_path(db, path)


def _is_uncategorized_path(path: str) -> bool:
    normalized = normalize_directory_path(path)
    return normalized == UNCATEGORIZED_PATH or normalized.startswith(f"{UNCATEGORIZED_PATH}/")


def _is_uncategorized_filter(directory: str) -> bool:
    return directory == UNCATEGORIZED_FILTER or (bool(directory) and _is_uncategorized_path(directory))


def _build_directory_tree(directories: list[Directory], bookmark_counts: dict[int, int]) -> list[dict]:
    nodes = {
        directory.id: {
            "id": directory.id,
            "name": directory.name,
            "path": directory.path,
            "depth": directory.depth,
            "direct_count": bookmark_counts.get(directory.id, 0),
            "count": bookmark_counts.get(directory.id, 0),
            "children": [],
        }
        for directory in directories
    }
    roots: list[dict] = []
    for directory in directories:
        node = nodes[directory.id]
        if directory.parent_id and directory.parent_id in nodes:
            nodes[directory.parent_id]["children"].append(node)
        else:
            roots.append(node)
    for root in roots:
        _sum_directory_counts(root)
    return roots


def _sum_directory_counts(node: dict) -> int:
    total = node["direct_count"]
    for child in node["children"]:
        total += _sum_directory_counts(child)
    node["count"] = total
    return total


def _trash_context(db: Session) -> dict:
    deleted_bookmarks = db.scalars(
        select(Bookmark)
        .options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
        .where(Bookmark.deleted_at.is_not(None))
        .order_by(Bookmark.deleted_at.desc())
    ).all()
    deleted_jobs = db.scalars(
        select(ParseJob).where(ParseJob.deleted_at.is_not(None)).order_by(ParseJob.deleted_at.desc())
    ).all()
    return {"bookmarks": deleted_bookmarks, "jobs": deleted_jobs}


def _extract_job_input(job: ParseJob) -> dict:
    source_type = job.source_type or "url"
    raw_input = job.raw_input or job.input_url
    if source_type == "url":
        fetch_url = job.input_url or raw_input
        extracted = fetch_and_extract(fetch_url).as_dict()
        extracted["source_type"] = "url"
        extracted["raw_input"] = raw_input
        extracted["group_reason"] = job.group_reason
        extracted["group_confidence"] = job.group_confidence
        extracted["grouping_source"] = job.grouping_source
        return extracted
    title = _name_from_text(raw_input)
    return {
        "url": None,
        "canonical_url": None,
        "title": title,
        "description": raw_input,
        "content": raw_input,
        "content_type": source_type,
        "source_type": source_type,
        "source_domain": "",
        "raw_input": raw_input,
        "fetch_status": "local_text",
        "group_reason": job.group_reason,
        "group_confidence": job.group_confidence,
        "grouping_source": job.grouping_source,
    }


def _name_from_text(text: str) -> str:
    compact = " ".join(text.strip().split())
    return compact[:60] or "未命名收藏"
