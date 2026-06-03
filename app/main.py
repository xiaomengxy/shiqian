from __future__ import annotations

import asyncio
import html
import re
import uuid
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import SessionLocal, engine, get_db
from app.migrations import ensure_runtime_schema
from app.models import Base, BilibiliSession, BilibiliVideoCache, Directory, Note
from app.schemas import (
    BilibiliImportRequest,
    CreateDirectoryRequest,
    CreateNoteRequest,
    MoveDirectoryRequest,
    MoveNoteRequest,
    RenameDirectoryRequest,
    UpdateNoteRequest,
)
from app.services.bilibili import BilibiliService
from app.services.bilibili_notes import (
    VideoContentResult,
    assess_content_trust,
    compose_video_note,
    extract_bilibili_summary_text,
    generate_video_note_markdown,
    subtitle_url_from_player,
    video_payload_from_media,
)
from app.services.directories import get_or_create_directory_path, move_directory, normalize_directory_path, rename_directory
from app.services.settings_store import (
    get_effective_settings,
    mask_secret,
    save_frontend_settings,
)
from app.services.time_display import local_datetime


ALLOWED_SOURCE_TYPES = {"manual", "url", "bilibili"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if engine.dialect.name == "sqlite":
        ensure_runtime_schema(engine)
    else:
        Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="拾签 Notes", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["local_datetime"] = local_datetime
BILIBILI_NOTE_VERSION = 2
BILIBILI_IMPORT_JOBS: dict[str, dict] = {}
BILIBILI_IMPORT_TASKS: dict[str, asyncio.Task] = {}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
def index(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "request": request,
            "notes": [_note_payload(note) for note in _query_notes(db)],
            "directories": _directory_payloads(db),
            "tree": _tree_payload(db),
            "active_nav": "notes",
        },
    )


@app.get("/trash")
def trash(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "trash.html",
        {"request": request, "notes": _query_notes(db, deleted=True), "active_nav": "trash"},
    )


@app.get("/learning")
def learning(request: Request):
    return templates.TemplateResponse(
        request,
        "learning.html",
        {"request": request, "active_nav": "learning"},
    )


@app.get("/api/bilibili/session")
def bilibili_session_api(db: Session = Depends(get_db)):
    session = _latest_bilibili_session(db)
    if not session:
        return {"logged_in": False, "user": None}
    return {"logged_in": True, "user": _bilibili_user_payload(session)}


@app.post("/api/bilibili/logout")
def bilibili_logout_api(db: Session = Depends(get_db)):
    for session in db.scalars(select(BilibiliSession).where(BilibiliSession.is_valid.is_(True))).all():
        session.is_valid = False
    db.commit()
    return {"logged_in": False}


@app.get("/api/bilibili/qrcode")
async def bilibili_qrcode_api():
    bili = BilibiliService()
    try:
        return await bili.generate_qrcode()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await bili.close()


@app.get("/api/bilibili/qrcode/poll/{qrcode_key}")
async def bilibili_qrcode_poll_api(qrcode_key: str, db: Session = Depends(get_db)):
    bili = BilibiliService()
    try:
        result = await bili.poll_qrcode_status(qrcode_key)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await bili.close()
    if result.get("status") != "confirmed":
        return result

    cookies = result.get("cookies") or {}
    auth = BilibiliService(
        sessdata=cookies.get("SESSDATA"),
        bili_jct=cookies.get("bili_jct"),
        dedeuserid=cookies.get("DedeUserID"),
    )
    user_info: dict = {}
    try:
        user_info = await auth.get_user_info()
    except Exception:
        user_info = {"mid": cookies.get("DedeUserID"), "uname": "Bilibili 用户"}
    finally:
        await auth.close()

    now = datetime.utcnow()
    for old in db.scalars(select(BilibiliSession).where(BilibiliSession.is_valid.is_(True))).all():
        old.is_valid = False
    session = BilibiliSession(
        session_id=str(uuid.uuid4()),
        bili_mid=_safe_int(user_info.get("mid") or cookies.get("DedeUserID")),
        bili_uname=user_info.get("uname") or "Bilibili 用户",
        bili_face=user_info.get("face"),
        sessdata=cookies.get("SESSDATA"),
        bili_jct=cookies.get("bili_jct"),
        dedeuserid=str(cookies.get("DedeUserID") or ""),
        refresh_token=result.get("refresh_token") or "",
        is_valid=True,
        last_active_at=now,
        created_at=now,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {"status": "confirmed", "message": "登录成功", "user": _bilibili_user_payload(session)}


@app.get("/api/bilibili/favorites")
async def bilibili_favorites_api(db: Session = Depends(get_db)):
    session = _require_bilibili_session(db)
    bili = _bilibili_service_from_session(session)
    try:
        folders = await bili.get_user_favorites(session.bili_mid or session.dedeuserid)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await bili.close()
    return {
        "folders": [
            {
                "media_id": folder.get("id"),
                "title": folder.get("title") or "未命名收藏夹",
                "media_count": folder.get("media_count") or 0,
                "is_default": _is_default_bilibili_folder(folder),
            }
            for folder in folders
            if folder.get("id")
        ]
    }


@app.get("/api/bilibili/favorites/{media_id}/videos")
async def bilibili_favorite_videos_api(media_id: int, page: int | None = None, db: Session = Depends(get_db)):
    session = _require_bilibili_session(db)
    bili = _bilibili_service_from_session(session)
    folder_info = {}
    medias = []
    has_more = False
    try:
        if page is not None:
            page_number = max(1, page)
            result = await bili.get_favorite_content(media_id, pn=page_number, ps=20)
            folder_info = result.get("info") or {}
            medias = result.get("medias") or []
            has_more = bool(result.get("has_more"))
        else:
            page_number = 1
            while True:
                result = await bili.get_favorite_content(media_id, pn=page_number, ps=20)
                if page_number == 1:
                    folder_info = result.get("info") or {}
                medias.extend(result.get("medias") or [])
                has_more = bool(result.get("has_more"))
                if not has_more:
                    break
                page_number += 1
                await asyncio.sleep(0.15)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await bili.close()
    videos = _bilibili_video_payloads(db, medias)
    payload = {
        "folder_info": folder_info,
        "videos": videos,
        "total": len(videos),
    }
    if page is not None:
        payload["page"] = max(1, page)
        payload["has_more"] = has_more
    return payload


def _bilibili_video_payloads(db: Session, medias: list[dict]) -> list[dict]:
    videos = []
    for media in medias:
        payload = video_payload_from_media(media)
        if not payload.get("bvid") or _is_invalid_bilibili_video(media):
            continue
        cached = db.scalar(select(BilibiliVideoCache).where(BilibiliVideoCache.bvid == payload["bvid"]))
        note = db.scalar(
            select(Note).where(Note.source_type == "bilibili", Note.source_id == payload["bvid"], Note.deleted_at.is_(None))
        )
        payload["note_id"] = note.id if note else (cached.note_id if cached else None)
        payload["content_source"] = cached.content_source if cached else None
        cache_meta = cached.meta if cached else {}
        note_meta = note.source_meta if note else {}
        status_meta = note_meta if isinstance(note_meta, dict) and note_meta.get("content_trust") else cache_meta
        if isinstance(status_meta, dict):
            payload["content_source"] = status_meta.get("content_source") or payload["content_source"]
            payload["content_trust"] = status_meta.get("content_trust")
            payload["trust_reason"] = status_meta.get("trust_reason")
        else:
            payload["content_trust"] = None
            payload["trust_reason"] = None
        note_is_current = _bilibili_note_is_current(note) if note else False
        payload["needs_regeneration"] = bool(note and not note_is_current)
        videos.append(payload)
    return videos


@app.get("/api/bilibili/favorites/{media_id}/videos/all", include_in_schema=False)
async def bilibili_all_videos_compat_api(media_id: int, db: Session = Depends(get_db)):
    session = _require_bilibili_session(db)
    bili = _bilibili_service_from_session(session)
    folder_info = {}
    medias = []
    try:
        page_number = 1
        while True:
            result = await bili.get_favorite_content(media_id, pn=page_number, ps=20)
            if page_number == 1:
                folder_info = result.get("info") or {}
            medias.extend(result.get("medias") or [])
            if not result.get("has_more"):
                break
            page_number += 1
            await asyncio.sleep(0.15)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    finally:
        await bili.close()
    videos = _bilibili_video_payloads(db, medias)
    return {"folder_info": folder_info, "videos": videos, "total": len(videos)}


@app.post("/api/bilibili/import")
async def bilibili_import_api(payload: BilibiliImportRequest, db: Session = Depends(get_db)):
    session = _require_bilibili_session(db)
    runtime = get_effective_settings(db, settings)
    bili = _bilibili_service_from_session(session)
    results = []
    try:
        directory = get_or_create_directory_path(db, f"Bilibili/{payload.folder_title or str(payload.media_id)}")
        for incoming in payload.videos:
            result = await _import_bilibili_video(
                db=db,
                bili=bili,
                directory=directory,
                video=incoming.model_dump(),
                media_id=payload.media_id,
                folder_title=payload.folder_title or str(payload.media_id),
                overwrite=payload.overwrite,
                runtime=runtime,
            )
            results.append(result)
            db.commit()
    finally:
        await bili.close()
    return {"items": results}


@app.post("/api/bilibili/import-jobs", status_code=202)
async def bilibili_import_job_create_api(payload: BilibiliImportRequest, db: Session = Depends(get_db)):
    session = _require_bilibili_session(db)
    job_id = str(uuid.uuid4())
    selected_count = len(payload.videos)
    job = {
        "id": job_id,
        "status": "queued",
        "media_id": payload.media_id,
        "folder_title": payload.folder_title or str(payload.media_id),
        "total": selected_count,
        "done": 0,
        "current_title": "",
        "message": "等待开始...",
        "items": [],
        "overwrite": payload.overwrite,
        "cancel_requested": False,
        "created_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
        "owner_mid": session.bili_mid,
    }
    BILIBILI_IMPORT_JOBS[job_id] = job
    task = asyncio.create_task(
        _run_bilibili_import_job(
            job_id=job_id,
            session_payload={
                "sessdata": session.sessdata,
                "bili_jct": session.bili_jct,
                "dedeuserid": session.dedeuserid,
            },
            payload_data=payload.model_dump(),
        )
    )
    BILIBILI_IMPORT_TASKS[job_id] = task
    task.add_done_callback(lambda _task, _job_id=job_id: BILIBILI_IMPORT_TASKS.pop(_job_id, None))
    return {"job": _bilibili_import_job_payload(job)}


@app.get("/api/bilibili/import-jobs/current")
def bilibili_import_job_current_api(db: Session = Depends(get_db)):
    session = _latest_bilibili_session(db)
    if not session:
        return {"job": None}
    jobs = list(BILIBILI_IMPORT_JOBS.values())
    if session.bili_mid is not None:
        jobs = [job for job in jobs if job.get("owner_mid") == session.bili_mid]
    active = [job for job in jobs if job.get("status") in {"queued", "running", "canceling"}]
    if active:
        return {"job": _bilibili_import_job_payload(active[-1])}
    if jobs:
        return {"job": _bilibili_import_job_payload(jobs[-1])}
    return {"job": None}


@app.get("/api/bilibili/import-jobs/{job_id}")
def bilibili_import_job_api(job_id: str):
    job = BILIBILI_IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    return {"job": _bilibili_import_job_payload(job)}


@app.post("/api/bilibili/import-jobs/{job_id}/cancel")
def bilibili_import_job_cancel_api(job_id: str):
    job = BILIBILI_IMPORT_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")
    if job.get("status") in {"done", "failed", "canceled"}:
        return {"job": _bilibili_import_job_payload(job)}
    job["cancel_requested"] = True
    job["status"] = "canceling"
    job["message"] = "正在中断当前解析..."
    job["updated_at"] = datetime.utcnow().isoformat()
    task = BILIBILI_IMPORT_TASKS.get(job_id)
    if task and not task.done():
        task.cancel()
    return {"job": _bilibili_import_job_payload(job)}


@app.get("/settings")
def settings_page(request: Request, saved: str = "", db: Session = Depends(get_db)):
    runtime = get_effective_settings(db, settings)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "settings": runtime,
            "saved": saved,
            "has_deepseek_key": bool(runtime.deepseek_api_key),
            "has_openai_key": bool(runtime.openai_api_key),
            "deepseek_key_label": mask_secret(runtime.deepseek_api_key),
            "openai_key_label": mask_secret(runtime.openai_api_key),
            "active_nav": "settings",
        },
    )


@app.post("/settings")
async def update_settings(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    save_frontend_settings(
        db,
        llm_provider=str(form.get("llm_provider") or "deepseek"),
        openai_model=str(form.get("openai_model") or ""),
        openai_base_url=str(form.get("openai_base_url") or ""),
        deepseek_model=str(form.get("deepseek_model") or ""),
        openai_api_key=str(form.get("openai_api_key") or ""),
        deepseek_api_key=str(form.get("deepseek_api_key") or ""),
        clear_openai_key=form.get("clear_openai_key") == "on",
        clear_deepseek_key=form.get("clear_deepseek_key") == "on",
    )
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.get("/bookmarks", include_in_schema=False)
def old_bookmarks_redirect():
    return RedirectResponse("/", status_code=307)


@app.get("/jobs", include_in_schema=False)
@app.get("/review/{_job_id}", include_in_schema=False)
@app.get("/grouping/{_session_id}", include_in_schema=False)
@app.get("/directories", include_in_schema=False)
@app.get("/directories/partials", include_in_schema=False)
def old_workflow_redirect(_job_id: int | None = None, _session_id: int | None = None):
    return RedirectResponse("/", status_code=307)


@app.get("/api/notes")
def list_notes_api(
    query: str = "",
    status: str = "active",
    folder: str = "",
    directory_id: int | None = None,
    db: Session = Depends(get_db),
):
    deleted = status == "trash"
    notes = _query_notes(db, query=query, folder=folder, directory_id=directory_id, deleted=deleted)
    return {
        "notes": [_note_payload(note) for note in notes],
        "tree": _tree_payload(db, query=query, deleted=deleted),
        "directories": _directory_payloads(db),
        "folders": _directory_payloads(db),
        "total_count": _note_count(db),
    }


@app.post("/api/notes", status_code=201)
def create_note_api(payload: CreateNoteRequest, db: Session = Depends(get_db)):
    directory = _directory_from_note_payload(db, payload.directory_id, payload.folder_path)
    note = Note(
        title=_clean_title(payload.title),
        body_md=payload.body_md or "",
        directory=directory,
        folder_path=directory.path if directory else "",
        source_url=_clean_optional(payload.source_url),
        source_type=_clean_source_type(payload.source_type, payload.source_url),
        source_id=_clean_optional(payload.source_id),
        source_meta=payload.source_meta or {},
    )
    db.add(note)
    db.commit()
    db.refresh(note)
    return {"note": _note_payload(note)}


@app.get("/api/notes/{note_id}")
def get_note_api(note_id: int, db: Session = Depends(get_db)):
    return {"note": _note_payload(_get_note(db, note_id))}


@app.put("/api/notes/{note_id}")
def update_note_api(note_id: int, payload: UpdateNoteRequest, db: Session = Depends(get_db)):
    note = _get_note(db, note_id)
    directory = _directory_from_note_payload(db, payload.directory_id, payload.folder_path)
    note.title = _clean_title(payload.title)
    note.body_md = payload.body_md or ""
    note.directory = directory
    note.folder_path = directory.path if directory else ""
    note.source_url = _clean_optional(payload.source_url)
    note.source_type = _clean_source_type(payload.source_type, payload.source_url)
    note.source_id = _clean_optional(payload.source_id)
    note.source_meta = payload.source_meta or {}
    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(note)
    return {"note": _note_payload(note)}


@app.delete("/api/notes/{note_id}")
def delete_note_api(note_id: int, db: Session = Depends(get_db)):
    note = _get_note(db, note_id)
    if note.deleted_at is None:
        now = datetime.utcnow()
        note.deleted_at = now
        note.updated_at = now
        db.commit()
        db.refresh(note)
    return {"note": _note_payload(note)}


@app.post("/api/notes/{note_id}/restore")
def restore_note_api(note_id: int, db: Session = Depends(get_db)):
    note = _get_note(db, note_id, include_deleted=True)
    note.deleted_at = None
    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(note)
    return {"note": _note_payload(note)}


@app.delete("/api/notes/{note_id}/purge")
def purge_note_api(note_id: int, db: Session = Depends(get_db)):
    note = _get_note(db, note_id, include_deleted=True)
    db.delete(note)
    db.commit()
    return {"deleted": True, "id": note_id}


@app.post("/api/notes/{note_id}/move")
def move_note_api(note_id: int, payload: MoveNoteRequest, db: Session = Depends(get_db)):
    note = _get_note(db, note_id)
    directory = _get_directory(db, payload.directory_id) if payload.directory_id else None
    note.directory = directory
    note.folder_path = directory.path if directory else ""
    note.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(note)
    return {"note": _note_payload(note), "tree": _tree_payload(db), "directories": _directory_payloads(db)}


@app.post("/api/directories", status_code=201)
def create_directory_api(payload: CreateDirectoryRequest, db: Session = Depends(get_db)):
    name = _clean_directory_name(payload.name)
    parent = _get_directory(db, payload.parent_id) if payload.parent_id else None
    path = "/".join(part for part in [parent.path if parent else "", name] if part)
    if db.scalar(select(Directory).where(Directory.path == path)):
        raise HTTPException(status_code=409, detail="Directory already exists")
    directory = Directory(name=name, parent=parent, path=path, depth=parent.depth + 1 if parent else 0)
    db.add(directory)
    db.commit()
    db.refresh(directory)
    return {"directory": _directory_payload(directory), "tree": _tree_payload(db), "directories": _directory_payloads(db)}


@app.put("/api/directories/{directory_id}")
def rename_directory_api(directory_id: int, payload: RenameDirectoryRequest, db: Session = Depends(get_db)):
    try:
        directory = rename_directory(db, directory_id, payload.name)
        _refresh_note_folder_paths(db)
        db.commit()
        db.refresh(directory)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"directory": _directory_payload(directory), "tree": _tree_payload(db), "directories": _directory_payloads(db)}


@app.post("/api/directories/{directory_id}/move")
def move_directory_api(directory_id: int, payload: MoveDirectoryRequest, db: Session = Depends(get_db)):
    try:
        directory = move_directory(db, directory_id, payload.parent_id)
        _refresh_note_folder_paths(db)
        db.commit()
        db.refresh(directory)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"directory": _directory_payload(directory), "tree": _tree_payload(db), "directories": _directory_payloads(db)}


@app.delete("/api/directories/{directory_id}")
def delete_directory_api(directory_id: int, db: Session = Depends(get_db)):
    directory = _get_directory(db, directory_id)
    ids = _descendant_directory_ids(db, directory)
    for note in db.scalars(select(Note).where(Note.directory_id.in_(ids))).all():
        note.directory_id = None
        note.folder_path = ""
        note.updated_at = datetime.utcnow()
    for target in db.scalars(select(Directory).where(Directory.id.in_(ids)).order_by(Directory.depth.desc())).all():
        db.delete(target)
    db.commit()
    return {"deleted": True, "id": directory_id, "tree": _tree_payload(db), "directories": _directory_payloads(db)}


def _query_notes(
    db: Session,
    *,
    query: str = "",
    folder: str = "",
    directory_id: int | None = None,
    deleted: bool = False,
) -> list[Note]:
    _repair_stale_bilibili_notes(db)
    stmt = select(Note).options(joinedload(Note.directory))
    stmt = stmt.where(Note.deleted_at.is_not(None) if deleted else Note.deleted_at.is_(None))
    clean_query = query.strip()
    if clean_query:
        like = f"%{clean_query}%"
        stmt = stmt.where(or_(Note.title.ilike(like), Note.body_md.ilike(like), Note.source_url.ilike(like)))
    if directory_id is not None:
        directory = db.get(Directory, directory_id)
        if directory is None:
            return []
        ids = _descendant_directory_ids(db, directory)
        stmt = stmt.where(Note.directory_id.in_(ids))
    else:
        clean_folder = _clean_folder_path(folder)
        if clean_folder:
            directory = db.scalar(select(Directory).where(Directory.path == clean_folder))
            if directory:
                stmt = stmt.where(Note.directory_id.in_(_descendant_directory_ids(db, directory)))
            else:
                stmt = stmt.where(or_(Note.folder_path == clean_folder, Note.folder_path.like(f"{clean_folder}/%")))
    return list(db.scalars(stmt.order_by(Note.updated_at.desc(), Note.created_at.desc())).all())


def _get_note(db: Session, note_id: int, *, include_deleted: bool = False) -> Note:
    stmt = select(Note).where(Note.id == note_id)
    if not include_deleted:
        stmt = stmt.where(Note.deleted_at.is_(None))
    note = db.scalar(stmt)
    if note is None:
        raise HTTPException(status_code=404, detail="Note not found")
    return note


def _note_payload(note: Note) -> dict:
    directory = note.directory
    directory_path = directory.path if directory else note.folder_path or ""
    return {
        "id": note.id,
        "title": note.title,
        "body_md": note.body_md,
        "directory_id": directory.id if directory else None,
        "folder_path": directory_path,
        "source_url": note.source_url,
        "source_type": note.source_type,
        "source_id": note.source_id,
        "source_meta": note.source_meta or {},
        "deleted_at": note.deleted_at.isoformat() if note.deleted_at else None,
        "created_at": note.created_at.isoformat(),
        "updated_at": note.updated_at.isoformat(),
        "created_at_display": local_datetime(note.created_at),
        "updated_at_display": local_datetime(note.updated_at),
    }


def _note_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Note.id)).where(Note.deleted_at.is_(None))) or 0)


def _clean_title(value: str) -> str:
    title = html.unescape(str(value or ""))
    title = re.sub(r"<[^>]+>", "", title)
    title = " ".join(title.split())
    return title[:500] or "Untitled"


def _ensure_markdown_h1(markdown: str, title: str) -> str:
    clean_title = _clean_title(title)
    lines = str(markdown or "").splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        if line.lstrip().startswith("# "):
            lines[index] = f"# {clean_title}"
            return "\n".join(lines).strip() + "\n"
        return f"# {clean_title}\n\n{str(markdown or '').strip()}\n"
    return f"# {clean_title}\n"


def _clean_optional(value: str | None) -> str | None:
    clean = (value or "").strip()
    return clean or None


def _clean_folder_path(value: str | None) -> str:
    clean = normalize_directory_path(value or "")
    return "" if clean == "未分类" else clean[:600]


def _clean_source_type(value: str, source_url: str | None) -> str:
    clean = (value or "").strip().lower()
    if clean in ALLOWED_SOURCE_TYPES:
        return clean
    return "url" if _clean_optional(source_url) else "manual"


def _clean_directory_name(value: str) -> str:
    return normalize_directory_path(value).split("/")[-1][:120]


def _get_directory(db: Session, directory_id: int | None) -> Directory:
    directory = db.get(Directory, directory_id)
    if directory is None:
        raise HTTPException(status_code=404, detail="Directory not found")
    return directory


def _directory_from_note_payload(db: Session, directory_id: int | None, folder_path: str | None) -> Directory | None:
    if directory_id:
        return _get_directory(db, directory_id)
    clean_folder = _clean_folder_path(folder_path)
    if not clean_folder:
        return None
    return get_or_create_directory_path(db, clean_folder)


def _directory_payload(directory: Directory, *, count: int = 0, direct_count: int = 0) -> dict:
    return {
        "id": directory.id,
        "name": directory.name,
        "path": directory.path,
        "parent_id": directory.parent_id,
        "depth": directory.depth,
        "count": count,
        "direct_count": direct_count,
    }


def _directory_payloads(db: Session) -> list[dict]:
    directories = list(db.scalars(select(Directory).order_by(Directory.path)).all())
    counts, direct_counts = _directory_note_counts(db, directories)
    return [_directory_payload(directory, count=counts.get(directory.id, 0), direct_count=direct_counts.get(directory.id, 0)) for directory in directories]


def _tree_payload(db: Session, *, query: str = "", deleted: bool = False) -> dict:
    directories = list(db.scalars(select(Directory).order_by(Directory.depth, Directory.name)).all())
    nodes = {
        directory.id: {
            **_directory_payload(directory),
            "children": [],
            "notes": [],
        }
        for directory in directories
    }
    directory_by_id = {directory.id: directory for directory in directories}
    notes = _query_notes(db, query=query, deleted=deleted)
    root_notes: list[dict] = []
    direct_counts: dict[int, int] = {}
    subtree_counts: dict[int, int] = {}

    for note in notes:
        payload = _note_payload(note)
        if note.directory_id in nodes:
            nodes[note.directory_id]["notes"].append(payload)
            direct_counts[note.directory_id] = direct_counts.get(note.directory_id, 0) + 1
        else:
            root_notes.append(payload)

    for directory_id, count in direct_counts.items():
        current = directory_by_id.get(directory_id)
        while current:
            subtree_counts[current.id] = subtree_counts.get(current.id, 0) + count
            current = directory_by_id.get(current.parent_id)

    for directory in directories:
        node = nodes[directory.id]
        node["direct_count"] = direct_counts.get(directory.id, 0)
        node["count"] = subtree_counts.get(directory.id, 0)

    def visible(node: dict) -> bool:
        return not query.strip() or node["count"] > 0

    roots: list[dict] = []
    for directory in directories:
        node = nodes[directory.id]
        if not visible(node):
            continue
        if directory.parent_id and directory.parent_id in nodes:
            parent = nodes[directory.parent_id]
            if visible(parent):
                parent["children"].append(node)
        else:
            roots.append(node)

    return {
        "root_notes": root_notes,
        "directories": roots,
        "total_count": _note_count(db),
        "visible_count": len(notes),
    }


def _directory_note_counts(db: Session, directories: list[Directory]) -> tuple[dict[int, int], dict[int, int]]:
    directory_by_id = {directory.id: directory for directory in directories}
    direct_counts = {
        directory_id: int(count)
        for directory_id, count in db.execute(
            select(Note.directory_id, func.count(Note.id))
            .where(Note.deleted_at.is_(None), Note.directory_id.is_not(None))
            .group_by(Note.directory_id)
        ).all()
        if directory_id is not None
    }
    counts: dict[int, int] = {}
    for directory_id, count in direct_counts.items():
        current = directory_by_id.get(directory_id)
        while current:
            counts[current.id] = counts.get(current.id, 0) + count
            current = directory_by_id.get(current.parent_id)
    return counts, direct_counts


def _descendant_directory_ids(db: Session, directory: Directory) -> list[int]:
    rows = db.execute(select(Directory.id, Directory.path)).all()
    prefix = f"{directory.path}/"
    return [directory_id for directory_id, path in rows if path == directory.path or path.startswith(prefix)]


def _refresh_note_folder_paths(db: Session) -> None:
    notes = list(db.scalars(select(Note).options(joinedload(Note.directory))).all())
    for note in notes:
        if note.directory:
            note.folder_path = note.directory.path
        else:
            note.directory_id = None
            note.folder_path = ""


def _latest_bilibili_session(db: Session) -> BilibiliSession | None:
    return db.scalar(
        select(BilibiliSession)
        .where(BilibiliSession.is_valid.is_(True))
        .order_by(BilibiliSession.last_active_at.desc(), BilibiliSession.created_at.desc())
    )


def _require_bilibili_session(db: Session) -> BilibiliSession:
    session = _latest_bilibili_session(db)
    if not session:
        raise HTTPException(status_code=401, detail="请先登录 Bilibili")
    session.last_active_at = datetime.utcnow()
    return session


def _bilibili_service_from_session(session: BilibiliSession) -> BilibiliService:
    return BilibiliService(sessdata=session.sessdata, bili_jct=session.bili_jct, dedeuserid=session.dedeuserid)


def _bilibili_user_payload(session: BilibiliSession) -> dict:
    return {
        "mid": session.bili_mid,
        "uname": session.bili_uname,
        "face": session.bili_face,
    }


def _bilibili_note_is_current(note: Note | None) -> bool:
    if not note:
        return False
    meta = note.source_meta or {}
    return (
        meta.get("note_version") == BILIBILI_NOTE_VERSION
        and meta.get("content_trust") in {"trusted", "untrusted", "basic"}
        and bool(meta.get("content_source"))
    )


def _repair_stale_bilibili_notes(db: Session) -> int:
    stale_notes = [
        note
        for note in db.scalars(select(Note).where(Note.source_type == "bilibili")).all()
        if note.source_id and not _bilibili_note_is_current(note)
    ]
    if not stale_notes:
        return 0

    repaired = 0
    now = datetime.utcnow()
    for note in stale_notes:
        meta = note.source_meta or {}
        if meta.get("content_trust") == "stale" and meta.get("legacy_body_hidden"):
            continue
        original_updated_at = note.updated_at
        cache = db.scalar(select(BilibiliVideoCache).where(BilibiliVideoCache.bvid == note.source_id))
        note.title = _clean_title((cache.title if cache else "") or note.title or note.source_id)
        note.body_md = _stale_bilibili_note_body(note, cache)
        note.source_url = note.source_url or f"https://www.bilibili.com/video/{note.source_id}"
        note.source_meta = {
            **meta,
            "content_source": "legacy_note",
            "content_trust": "stale",
            "trust_reason": "旧版 Bilibili 笔记缺少可信度标记，正文已隐藏，请重新生成。",
            "legacy_content_source": meta.get("content_source"),
            "legacy_body_hidden": True,
            "legacy_body_hidden_at": now.isoformat(),
            "note_version": 1,
        }
        note.updated_at = original_updated_at
        repaired += 1
    if repaired:
        db.commit()
    return repaired


def _stale_bilibili_note_body(note: Note, cache: BilibiliVideoCache | None = None) -> str:
    bvid = note.source_id or ""
    title = _clean_title((cache.title if cache else "") or note.title or bvid or "Bilibili 视频")
    url = note.source_url or f"https://www.bilibili.com/video/{bvid}"
    meta = note.source_meta or {}
    owner = (cache.owner_name if cache else "") or meta.get("owner_name") or "未知"
    folder_title = meta.get("folder_title") or note.folder_path or "未命名收藏夹"
    description = ((cache.description if cache else "") or "").strip()
    body = [
        f"# {title}",
        "",
        f"> 来源：[Bilibili 视频]({url})",
        f"> BVID：{bvid}",
        f"> UP主：{owner}",
        f"> 收藏夹：{folder_title}",
        "> 内容来源：旧版笔记已隔离",
        "> 内容提示：这条笔记由旧版逻辑生成，未经过字幕相关性校验，旧正文可能与标题不匹配，已隐藏。请在学习页重新读取收藏夹并生成。",
    ]
    if description:
        body.append(f"> 视频简介：{description}")
    body.extend(
        [
            "",
            "## 一句话总结",
            "",
            "这是一条旧版 Bilibili 笔记，当前只保留可信来源信息，等待重新生成。",
            "",
            "## 核心要点",
            "",
            "- 原笔记正文没有可信度标记，可能来自错配字幕或摘要。",
            "- 为避免误导，旧版 AI 正文已隐藏，不再作为正常笔记展示或参与搜索。",
            "- 在学习页重新读取收藏夹后生成，系统会先校验字幕/摘要与标题、简介是否匹配。",
            "",
            "## 原始信息",
            "",
            f"- 标题：{title}",
            f"- 链接：{url}",
            f"- BVID：{bvid}",
        ]
    )
    if description:
        body.extend(["", "### 视频简介", "", description])
    return "\n".join(body).strip() + "\n"


def _bilibili_import_job_payload(job: dict) -> dict:
    return {
        "id": job.get("id"),
        "status": job.get("status"),
        "media_id": job.get("media_id"),
        "folder_title": job.get("folder_title"),
        "total": job.get("total", 0),
        "done": job.get("done", 0),
        "current_title": job.get("current_title", ""),
        "message": job.get("message", ""),
        "items": job.get("items", []),
        "overwrite": bool(job.get("overwrite")),
        "cancel_requested": bool(job.get("cancel_requested")),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
    }


def _update_bilibili_import_job(job_id: str, **values) -> None:
    job = BILIBILI_IMPORT_JOBS.get(job_id)
    if not job:
        return
    job.update(values)
    job["updated_at"] = datetime.utcnow().isoformat()


async def _run_bilibili_import_job(*, job_id: str, session_payload: dict, payload_data: dict) -> None:
    payload = BilibiliImportRequest(**payload_data)
    _update_bilibili_import_job(job_id, status="running", message="正在解析并生成笔记...")
    db = SessionLocal()
    bili = BilibiliService(
        sessdata=session_payload.get("sessdata"),
        bili_jct=session_payload.get("bili_jct"),
        dedeuserid=session_payload.get("dedeuserid"),
    )
    try:
        runtime = get_effective_settings(db, settings)
        directory = get_or_create_directory_path(db, f"Bilibili/{payload.folder_title or str(payload.media_id)}")
        total = len(payload.videos)
        for index, incoming in enumerate(payload.videos):
            job = BILIBILI_IMPORT_JOBS.get(job_id)
            if not job or job.get("cancel_requested"):
                _update_bilibili_import_job(job_id, status="canceled", message="已中断", total=total)
                return
            title = incoming.title or incoming.bvid
            _update_bilibili_import_job(
                job_id,
                status="running",
                total=total,
                done=index,
                current_title=title,
                message=f"正在处理：{title}",
            )
            result = await _import_bilibili_video(
                db=db,
                bili=bili,
                directory=directory,
                video=incoming.model_dump(),
                media_id=payload.media_id,
                folder_title=payload.folder_title or str(payload.media_id),
                overwrite=payload.overwrite,
                runtime=runtime,
            )
            db.commit()
            job = BILIBILI_IMPORT_JOBS.get(job_id)
            if not job:
                return
            job.setdefault("items", []).append(result)
            _update_bilibili_import_job(job_id, done=index + 1, current_title=title)
        _update_bilibili_import_job(job_id, status="done", done=total, current_title="", message="解析完成")
    except asyncio.CancelledError:
        db.rollback()
        _update_bilibili_import_job(job_id, status="canceled", message="已中断当前解析")
        raise
    except Exception as exc:
        db.rollback()
        _update_bilibili_import_job(job_id, status="failed", message=str(exc), current_title="")
    finally:
        await bili.close()
        db.close()


async def _import_bilibili_video(
    *,
    db: Session,
    bili: BilibiliService,
    directory: Directory,
    video: dict,
    media_id: int,
    folder_title: str,
    overwrite: bool,
    runtime,
) -> dict:
    bvid = str(video.get("bvid") or "").strip()
    if not bvid:
        return {"status": "failed", "error": "缺少 BVID"}

    try:
        existing_note = db.scalar(
            select(Note).where(Note.source_type == "bilibili", Note.source_id == bvid, Note.deleted_at.is_(None))
        )
        if existing_note and not overwrite and _bilibili_note_is_current(existing_note):
            return {"status": "skipped", "bvid": bvid, "title": existing_note.title, "note_id": existing_note.id, "reason": "已生成笔记"}

        detail = await bili.get_video_info(bvid)
        video = _merge_bilibili_detail(video, detail)
        canonical_title = _clean_title(video.get("title") or bvid)
        video["title"] = canonical_title
        content = await _fetch_bilibili_video_content(bili, video)
        body_md, provider, summary_error = await generate_video_note_markdown(
            video=video,
            content=content,
            folder_title=folder_title,
            settings=runtime,
        )
        body_md = compose_video_note(video=video, content=content, folder_title=folder_title, generated_md=body_md)
        cache = _upsert_bilibili_cache(db, video, content, summary_error)
        note = db.scalar(
            select(Note).where(Note.source_type == "bilibili", Note.source_id == bvid).order_by(Note.updated_at.desc())
        )
        if note is None:
            note = Note(source_type="bilibili", source_id=bvid)
            db.add(note)
        note.deleted_at = None
        note.title = canonical_title
        note.body_md = body_md
        note.directory = directory
        note.folder_path = directory.path
        note.source_url = f"https://www.bilibili.com/video/{bvid}"
        note.source_meta = {
            "media_id": media_id,
            "folder_title": folder_title,
            "cid": video.get("cid"),
            "aid": video.get("aid"),
            "owner_name": video.get("owner_name"),
            "owner_mid": video.get("owner_mid"),
            "duration": video.get("duration"),
            "cover": video.get("cover"),
            "content_source": content.source,
            "content_trust": content.trust,
            "trust_reason": content.trust_reason,
            "summary_provider": provider,
            "summary_error": summary_error,
            "note_version": BILIBILI_NOTE_VERSION,
        }
        note.updated_at = datetime.utcnow()
        db.flush()
        cache.note_id = note.id
        return {
            "status": "imported",
            "bvid": bvid,
            "title": note.title,
            "note_id": note.id,
            "content_source": content.source,
            "content_trust": content.trust,
            "trust_reason": content.trust_reason,
            "summary_provider": provider,
            "summary_error": summary_error,
        }
    except Exception as exc:
        cache = _upsert_bilibili_cache(db, video, VideoContentResult("", "failed", str(exc)), str(exc))
        return {"status": "failed", "bvid": bvid, "title": cache.title, "error": str(exc)}


async def _fetch_bilibili_video_content(bili: BilibiliService, video: dict) -> VideoContentResult:
    bvid = video.get("bvid")
    cid = _safe_int(video.get("cid"))
    aid = _safe_int(video.get("aid"))
    rejected: list[VideoContentResult] = []
    if cid:
        player = await bili.get_player_info(bvid, cid, aid)
        subtitle_url = subtitle_url_from_player(player)
        if subtitle_url:
            try:
                subtitle = await bili.download_subtitle(subtitle_url)
                if subtitle.strip():
                    trust, reason = assess_content_trust(video, subtitle, "字幕")
                    if trust == "trusted":
                        return VideoContentResult(subtitle, "subtitle", trust=trust, trust_reason=reason)
                    rejected.append(VideoContentResult(subtitle, "untrusted_subtitle", trust=trust, trust_reason=reason, error=reason))
            except Exception as exc:
                subtitle_error = str(exc)
            else:
                subtitle_error = None
        else:
            subtitle_error = None
        summary = extract_bilibili_summary_text(await bili.get_video_summary(bvid, cid, _safe_int(video.get("owner_mid"))))
        if summary:
            trust, reason = assess_content_trust(video, summary, "Bilibili AI 摘要")
            if trust == "trusted":
                return VideoContentResult(summary, "ai_summary", trust=trust, trust_reason=reason)
            rejected.append(VideoContentResult(summary, "untrusted_ai_summary", trust=trust, trust_reason=reason, error=reason))
    else:
        subtitle_error = "缺少 cid，无法读取字幕或摘要"
    basic = _basic_bilibili_content(video)
    if rejected:
        first = rejected[0]
        return VideoContentResult(basic, first.source, first.error or subtitle_error, trust=first.trust, trust_reason=first.trust_reason)
    return VideoContentResult(basic, "basic_info", subtitle_error, trust="basic", trust_reason=subtitle_error or "未找到可信字幕或摘要，已使用基础信息")


def _merge_bilibili_detail(video: dict, detail: dict) -> dict:
    owner = detail.get("owner") or {}
    pages = detail.get("pages") or []
    first_page = pages[0] if pages else {}
    merged = dict(video)
    detail_cid = detail.get("cid") or first_page.get("cid")
    merged.update(
        {
            "title": detail.get("title") or video.get("title"),
            "description": detail.get("desc") or video.get("description") or "",
            "cid": detail_cid or video.get("cid"),
            "aid": detail.get("aid") or video.get("aid"),
            "cover": detail.get("pic") or video.get("cover"),
            "duration": detail.get("duration") or video.get("duration"),
            "owner_name": owner.get("name") or video.get("owner_name"),
            "owner_mid": owner.get("mid") or video.get("owner_mid"),
        }
    )
    return merged


def _upsert_bilibili_cache(db: Session, video: dict, content: VideoContentResult, error: str | None) -> BilibiliVideoCache:
    bvid = str(video.get("bvid") or "")
    cache = db.scalar(select(BilibiliVideoCache).where(BilibiliVideoCache.bvid == bvid))
    now = datetime.utcnow()
    if cache is None:
        cache = BilibiliVideoCache(bvid=bvid, title=_clean_title(video.get("title") or bvid), created_at=now, updated_at=now)
        db.add(cache)
    cache.title = _clean_title(video.get("title") or bvid)
    cache.cid = _safe_int(video.get("cid"))
    cache.aid = _safe_int(video.get("aid"))
    cache.description = video.get("description") or ""
    cache.owner_name = video.get("owner_name")
    cache.owner_mid = _safe_int(video.get("owner_mid"))
    cache.duration = _safe_int(video.get("duration"))
    cache.cover_url = video.get("cover")
    cache.content_text = content.text
    cache.content_source = content.source
    cache.process_error = error or content.error
    cache.meta = {
        "raw": {key: video.get(key) for key in ["bvid", "title", "cid", "aid", "cover"]},
        "content_trust": content.trust,
        "trust_reason": content.trust_reason,
        "note_version": BILIBILI_NOTE_VERSION,
    }
    cache.updated_at = now
    return cache


def _basic_bilibili_content(video: dict) -> str:
    parts = [f"视频标题：{video.get('title') or video.get('bvid')}"]
    if video.get("owner_name"):
        parts.append(f"UP主：{video.get('owner_name')}")
    if video.get("description"):
        parts.append(f"视频简介：{video.get('description')}")
    return "\n\n".join(parts)


def _is_invalid_bilibili_video(media: dict) -> bool:
    title = str(media.get("title") or "").strip()
    return media.get("attr") == 9 or title in {"已失效视频", "已删除视频"}


def _is_default_bilibili_folder(folder: dict) -> bool:
    if any(bool(folder.get(key)) for key in ["is_default", "default", "isDefault"]):
        return True
    return folder.get("type") == 1 or folder.get("fav_state") == 1 or folder.get("attr") == 1 or folder.get("title") == "默认收藏夹"


def _safe_int(value) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None
