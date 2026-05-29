from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.services.urls import URL_RE, extract_urls


TERM_MAX_LENGTH = 40


@dataclass(frozen=True)
class InputItem:
    raw_input: str
    source_type: str


def parse_mixed_input(text: str) -> list[InputItem]:
    urls = extract_urls(text)
    remainder = text
    for url in urls:
        remainder = remainder.replace(url, "\n")

    items = [InputItem(raw_input=url, source_type="url") for url in urls]
    for block in _split_text_blocks(remainder):
        source_type = "term" if _is_term(block) else "text"
        items.append(InputItem(raw_input=block, source_type=source_type))

    seen: set[tuple[str, str]] = set()
    unique: list[InputItem] = []
    for item in items:
        key = (item.source_type, normalize_text_for_hash(item.raw_input))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def content_fingerprint(source_type: str, raw_input: str) -> str:
    normalized = normalize_text_for_hash(raw_input)
    return hashlib.sha256(f"{source_type}:{normalized}".encode("utf-8")).hexdigest()


def normalize_text_for_hash(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def normalize_keywords(raw_keywords: list[str] | str) -> list[str]:
    if isinstance(raw_keywords, str):
        parts = re.split(r"[,，、\n]+", raw_keywords)
    else:
        parts = raw_keywords
    seen: set[str] = set()
    keywords: list[str] = []
    for item in parts:
        clean = re.sub(r"\s+", " ", str(item).strip())
        if clean and clean not in seen:
            seen.add(clean)
            keywords.append(clean[:32])
    return keywords[:8]


def extract_local_keywords(text: str) -> list[str]:
    candidates = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_+-]{2,30}", text)
    stopwords = {"https", "http", "www", "com", "the", "and", "for", "with"}
    ranked: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        clean = candidate.strip()
        key = clean.casefold()
        if key in stopwords or key in seen:
            continue
        seen.add(key)
        ranked.append(clean)
    return ranked[:8]


def _split_text_blocks(text: str) -> list[str]:
    text = URL_RE.sub("\n", text)
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]
    if len(blocks) <= 1:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) > 1 and all(len(line) <= 80 for line in lines):
            blocks = lines
    return [re.sub(r"\s+", " ", block) for block in blocks if block.strip()]


def _is_term(text: str) -> bool:
    clean = text.strip()
    if len(clean) <= TERM_MAX_LENGTH and "\n" not in clean:
        return len(re.split(r"\s+", clean)) <= 6
    return False

