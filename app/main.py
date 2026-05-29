from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import String, cast, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.database import SessionLocal, engine, get_db
from app.migrations import ensure_runtime_schema
from app.models import Base, Bookmark, Directory, ParseJob, Tag
from app.schemas import ConfirmBookmarkRequest, ParseItemsRequest, ParseLinksRequest
from app.services.directories import (
    get_or_create_directory_path,
    list_directory_paths,
    move_directory,
    normalize_directory_path,
    rename_directory,
)
from app.services.llm import generate_suggestion
from app.services.parser import fetch_and_extract
from app.services.tags import get_or_create_tags, normalize_tags
from app.services.items import InputItem, content_fingerprint, normalize_keywords, parse_mixed_input
from app.services.settings_store import get_effective_settings, mask_secret, save_frontend_settings
from app.services.urls import canonicalize_url, extract_urls


BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_runtime_schema(engine)
    yield


app = FastAPI(title="拾签", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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
    items = parse_mixed_input(input_text)
    if not items:
        raise HTTPException(status_code=400, detail="No content found")
    for item in items:
        job = _create_job(db, item, provider)
        background_tasks.add_task(_process_job_in_background, job.id, provider)
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
def jobs(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": _recent_jobs(db, limit=100),
            "has_active_jobs": _has_active_jobs(db),
        },
    )


@app.get("/review/{job_id}")
def review(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "request": request,
            "job": job,
            "extracted": job.extracted_json or {},
            "suggestion": job.suggestion_json or {},
            "directories": db.scalars(select(Directory).order_by(Directory.path)).all(),
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
    db: Session = Depends(get_db),
):
    stmt = select(Bookmark).options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
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
    if directory:
        stmt = stmt.join(Bookmark.directory).where(Directory.path.like(f"{normalize_directory_path(directory)}%"))
    if tag:
        stmt = stmt.join(Bookmark.tags).where(Tag.name == tag)
    stmt = _apply_bookmark_sort(stmt, sort)
    items = db.scalars(stmt).unique().all()
    return templates.TemplateResponse(
        request,
        "bookmarks.html",
        {
            "request": request,
            "bookmarks": items,
            "directories": db.scalars(select(Directory).order_by(Directory.path)).all(),
            "tags": db.scalars(select(Tag).order_by(Tag.name)).all(),
            "query": query,
            "tag_filter": tag,
            "directory_filter": directory,
            "sort": sort,
        },
    )


@app.get("/bookmarks/{bookmark_id}/open")
def open_bookmark(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    if bookmark.source_type != "url" or not bookmark.url:
        return RedirectResponse("/bookmarks", status_code=303)
    bookmark.opened_count = (bookmark.opened_count or 0) + 1
    bookmark.last_opened_at = datetime.utcnow()
    db.commit()
    return RedirectResponse(bookmark.url, status_code=302)


@app.get("/directories")
def directories(request: Request, error: str = "", db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "directories.html",
        {
            "request": request,
            "directories": db.scalars(select(Directory).order_by(Directory.path)).all(),
            "error": error,
        },
    )


@app.post("/directories")
def create_directory(path: str = Form(...), db: Session = Depends(get_db)):
    get_or_create_directory_path(db, path)
    db.commit()
    return RedirectResponse("/directories", status_code=303)


@app.post("/directories/{directory_id}/rename")
def rename_directory_route(directory_id: int, name: str = Form(...), db: Session = Depends(get_db)):
    try:
        rename_directory(db, directory_id, name)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/directories?error={str(exc)}", status_code=303)
    return RedirectResponse("/directories", status_code=303)


@app.post("/directories/{directory_id}/move")
def move_directory_route(directory_id: int, parent_id: int | None = Form(None), db: Session = Depends(get_db)):
    try:
        move_directory(db, directory_id, parent_id)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return RedirectResponse(f"/directories?error={str(exc)}", status_code=303)
    return RedirectResponse("/directories", status_code=303)


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
    for item in parse_mixed_input(payload.input):
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
            }
        )
    return {"jobs": jobs}


@app.get("/api/jobs/{job_id}")
def get_job_api(job_id: int, db: Session = Depends(get_db)):
    job = db.get(ParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "id": job.id,
        "input_url": job.input_url,
        "source_type": job.source_type,
        "raw_input": job.raw_input,
        "status": job.status,
        "stage": job.stage,
        "progress_percent": job.progress_percent,
        "error": job.error,
        "extracted": job.extracted_json,
        "suggestion": job.suggestion_json,
        "provider": job.provider,
        "model": job.model,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


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
    stmt = select(Bookmark).options(selectinload(Bookmark.tags), selectinload(Bookmark.directory))
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
    if directory:
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
                "content_type": item.content_type,
                "directory": item.directory.path if item.directory else None,
                "tags": [tag.name for tag in item.tags],
            }
            for item in db.scalars(stmt).unique().all()
        ]
    }


@app.post("/api/bookmarks/{bookmark_id}/open")
def open_bookmark_api(bookmark_id: int, db: Session = Depends(get_db)):
    bookmark = db.get(Bookmark, bookmark_id)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Bookmark not found")
    bookmark.opened_count = (bookmark.opened_count or 0) + 1
    bookmark.last_opened_at = datetime.utcnow()
    db.commit()
    return {
        "id": bookmark.id,
        "opened_count": bookmark.opened_count,
        "last_opened_at": bookmark.last_opened_at.isoformat() if bookmark.last_opened_at else None,
    }


def _create_job(db: Session, item: InputItem, provider: str | None) -> ParseJob:
    runtime_settings = get_effective_settings(db, settings)
    job = ParseJob(
        input_url=item.raw_input,
        raw_input=item.raw_input,
        source_type=item.source_type,
        status="pending",
        stage="等待处理",
        progress_percent=0,
        provider=provider or runtime_settings.llm_provider,
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
        job.updated_at = datetime.utcnow()
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
    suggestion = job.suggestion_json or {}
    directory = get_or_create_directory_path(db, directory_path or suggestion.get("recommended_directory_path") or "未分类")
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
    bookmark.tags = get_or_create_tags(db, tags)
    db.commit()
    db.refresh(bookmark)
    return bookmark


def _recent_jobs(db: Session, limit: int) -> list[ParseJob]:
    return list(db.scalars(select(ParseJob).order_by(ParseJob.created_at.desc()).limit(limit)).all())


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
            .where(ParseJob.status.in_(["pending", "processing"]))
            .order_by(ParseJob.created_at.desc())
            .limit(1)
        )
        is not None
    )


def _extract_job_input(job: ParseJob) -> dict:
    source_type = job.source_type or "url"
    raw_input = job.raw_input or job.input_url
    if source_type == "url":
        extracted = fetch_and_extract(raw_input).as_dict()
        extracted["source_type"] = "url"
        extracted["raw_input"] = raw_input
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
    }


def _name_from_text(text: str) -> str:
    compact = " ".join(text.strip().split())
    return compact[:60] or "未命名收藏"
