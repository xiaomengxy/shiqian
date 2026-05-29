import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Directory


PATH_SPLIT_RE = re.compile(r"[\\/]+")


def normalize_directory_path(path: str) -> str:
    parts = [part.strip() for part in PATH_SPLIT_RE.split(path or "") if part.strip()]
    cleaned: list[str] = []
    for part in parts:
        safe = re.sub(r"\s+", " ", part)
        safe = safe.replace("\x00", "")
        if safe:
            cleaned.append(safe[:80])
    return "/".join(cleaned) or "未分类"


def list_directory_paths(db: Session) -> list[str]:
    return list(db.scalars(select(Directory.path).order_by(Directory.path)).all())


def get_or_create_directory_path(db: Session, path: str) -> Directory:
    normalized = normalize_directory_path(path)
    existing = db.scalar(select(Directory).where(Directory.path == normalized))
    if existing:
        return existing

    parent: Directory | None = None
    current_parts: list[str] = []
    for depth, name in enumerate(normalized.split("/")):
        current_parts.append(name)
        current_path = "/".join(current_parts)
        current = db.scalar(select(Directory).where(Directory.path == current_path))
        if current is None:
            current = Directory(name=name, parent=parent, path=current_path, depth=depth)
            db.add(current)
            db.flush()
        parent = current
    return parent


def rename_directory(db: Session, directory_id: int, new_name: str) -> Directory:
    directory = db.get(Directory, directory_id)
    if directory is None:
        raise ValueError("Directory not found")
    old_path = directory.path
    parent_path = directory.parent.path if directory.parent else ""
    normalized_name = normalize_directory_path(new_name).split("/")[-1]
    new_path = "/".join(part for part in [parent_path, normalized_name] if part)
    if new_path != old_path and db.scalar(select(Directory).where(Directory.path == new_path)):
        raise ValueError("Directory path already exists")
    directory.name = normalized_name
    _rewrite_subtree_paths(directory, old_path, new_path)
    return directory


def move_directory(db: Session, directory_id: int, new_parent_id: int | None) -> Directory:
    directory = db.get(Directory, directory_id)
    if directory is None:
        raise ValueError("Directory not found")
    new_parent = db.get(Directory, new_parent_id) if new_parent_id else None
    if new_parent and _is_descendant(new_parent, directory):
        raise ValueError("Cannot move a directory into its own subtree")
    old_path = directory.path
    new_path = "/".join(part for part in [new_parent.path if new_parent else "", directory.name] if part)
    if new_path != old_path and db.scalar(select(Directory).where(Directory.path == new_path)):
        raise ValueError("Directory path already exists")
    directory.parent = new_parent
    _rewrite_subtree_paths(directory, old_path, new_path, new_parent.depth + 1 if new_parent else 0)
    return directory


def _is_descendant(candidate: Directory, ancestor: Directory) -> bool:
    current: Directory | None = candidate
    while current:
        if current.id == ancestor.id:
            return True
        current = current.parent
    return False


def _rewrite_subtree_paths(directory: Directory, old_path: str, new_path: str, depth: int | None = None) -> None:
    if depth is not None:
        directory.depth = depth
    directory.path = new_path
    prefix = f"{old_path}/"
    for child in directory.children:
        child_old_path = child.path
        if child_old_path.startswith(prefix):
            child_new_path = f"{new_path}/{child_old_path[len(prefix):]}"
        else:
            child_new_path = f"{new_path}/{child.name}"
        _rewrite_subtree_paths(child, child_old_path, child_new_path, directory.depth + 1)

