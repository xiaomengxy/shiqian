import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Tag


def slugify_tag(name: str) -> str:
    slug = re.sub(r"\s+", "-", name.strip().lower())
    slug = re.sub(r"[^\w\-\u4e00-\u9fff]", "", slug)
    return slug[:120] or "tag"


def normalize_tags(raw_tags: list[str] | str) -> list[str]:
    if isinstance(raw_tags, str):
        parts = re.split(r"[,，\n]+", raw_tags)
    else:
        parts = raw_tags
    seen: set[str] = set()
    tags: list[str] = []
    for item in parts:
        clean = re.sub(r"\s+", " ", item.strip())
        if clean and clean not in seen:
            seen.add(clean)
            tags.append(clean[:40])
    return tags[:12]


def get_or_create_tags(db: Session, names: list[str]) -> list[Tag]:
    tags: list[Tag] = []
    for name in normalize_tags(names):
        existing = db.scalar(select(Tag).where(Tag.name == name))
        if existing:
            tags.append(existing)
            continue
        base_slug = slugify_tag(name)
        slug = base_slug
        suffix = 2
        while db.scalar(select(Tag).where(Tag.slug == slug)):
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        tag = Tag(name=name, slug=slug)
        db.add(tag)
        db.flush()
        tags.append(tag)
    return tags

