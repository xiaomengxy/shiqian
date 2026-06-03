from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import engine, get_db
from app.migrations import ensure_runtime_schema
from app.models import Base, Directory, Note
from app.schemas import (
    CreateDirectoryRequest,
    CreateNoteRequest,
    MoveDirectoryRequest,
    MoveNoteRequest,
    RenameDirectoryRequest,
    UpdateNoteRequest,
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
    title = " ".join((value or "").split())
    return title[:500] or "Untitled"


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
