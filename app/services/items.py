from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings
from app.services.urls import URL_RE, extract_urls


TERM_MAX_LENGTH = 40
AI_GROUPING_THRESHOLD = 0.75
AI_ACCEPT_THRESHOLD = 0.65
SEPARATOR_RE = re.compile(r"^\s*(?:-{3,}|#{3,}|\*{3,})\s*$")
BULLET_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
ACCESS_CODE_RE = re.compile(r"(提取码|访问码|验证码|密码|passcode|code|pwd)\s*[:：]?\s*[\w-]+", re.IGNORECASE)
GROUPING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "groups": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "source_type": {"type": "string", "enum": ["url", "term", "text"]},
                    "primary_url": {"type": ["string", "null"]},
                    "raw_input": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["source_type", "primary_url", "raw_input", "reason", "confidence"],
            },
        }
    },
    "required": ["groups"],
}


@dataclass(frozen=True)
class InputItem:
    raw_input: str
    source_type: str
    primary_url: str | None = None
    group_confidence: float = 1.0
    group_reason: str = "本地规则拆分"
    grouping_source: str = "rules"


def group_mixed_input(text: str, provider: str | None, settings: Settings) -> list[InputItem]:
    local_items = parse_mixed_input(text)
    if not local_items or not _needs_ai_grouping(local_items):
        return local_items
    selected_provider = (provider or settings.llm_provider or "deepseek").lower()
    if selected_provider == "openai" and settings.openai_api_key:
        return _ai_group_input(text, local_items, selected_provider, settings.openai_model, settings.openai_api_key)
    if selected_provider == "deepseek" and settings.deepseek_api_key:
        return _ai_group_input(text, local_items, selected_provider, settings.deepseek_model, settings.deepseek_api_key)
    return local_items


def parse_mixed_input(text: str) -> list[InputItem]:
    items: list[InputItem] = []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    for section in _split_sections(normalized):
        items.extend(_items_from_section(section))
    seen: set[tuple[str, str]] = set()
    unique: list[InputItem] = []
    for item in items:
        key = (item.source_type, normalize_text_for_hash(item.primary_url or item.raw_input))
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


def _split_sections(text: str) -> list[str]:
    sections: list[str] = []
    current: list[str] = []
    blank_count = 0
    for line in text.split("\n"):
        if SEPARATOR_RE.match(line):
            if current:
                sections.append("\n".join(current).strip())
                current = []
            blank_count = 0
            continue
        if not line.strip():
            blank_count += 1
            if blank_count >= 2 and current:
                sections.append("\n".join(current).strip())
                current = []
            continue
        if blank_count == 1 and current:
            current.append("")
        blank_count = 0
        current.append(line.rstrip())
    if current:
        sections.append("\n".join(current).strip())
    return [section for section in sections if section.strip()]


def _items_from_section(section: str) -> list[InputItem]:
    lines = [line.strip() for line in section.splitlines() if line.strip()]
    if not lines:
        return []
    urls = extract_urls(section)
    if not urls:
        return _text_items_from_lines(lines)
    if _is_pure_url_list(lines):
        return [
            InputItem(raw_input=url, source_type="url", primary_url=url, group_confidence=0.98, group_reason="纯链接列表，每个链接独立处理")
            for url in urls
        ]
    if _is_url_bullet_list(lines):
        return [_url_item(line, "项目列表中的独立链接", 0.92) for line in lines if extract_urls(line)]
    if len(urls) == 1:
        reason = "链接与相邻说明合并为同一条"
        confidence = 0.88
        if ACCESS_CODE_RE.search(section):
            reason = "链接下方提取码或访问说明已合并"
            confidence = 0.96
        return [InputItem(raw_input=_compact_block(section), source_type="url", primary_url=urls[0], group_confidence=confidence, group_reason=reason)]
    return _ambiguous_multi_url_items(lines)


def _text_items_from_lines(lines: list[str]) -> list[InputItem]:
    if _looks_like_term_list(lines):
        return [
            InputItem(raw_input=line, source_type="term", group_confidence=0.86, group_reason="短词清单按词条拆分")
            for line in lines
        ]
    block = _compact_block("\n".join(lines))
    source_type = "term" if _is_term(block) else "text"
    return [InputItem(raw_input=block, source_type=source_type, group_confidence=0.9, group_reason="普通换行按同一段内容合并")]


def _ambiguous_multi_url_items(lines: list[str]) -> list[InputItem]:
    groups: list[list[str]] = []
    current: list[str] = []
    pending_prefix: list[str] = []
    for line in lines:
        if extract_urls(line):
            if current:
                groups.append(current)
            current = pending_prefix + [line]
            pending_prefix = []
        elif current:
            current.append(line)
        else:
            pending_prefix.append(line)
    if current:
        groups.append(current)
    if pending_prefix and groups:
        groups[-1].extend(pending_prefix)
    return [
        _url_item("\n".join(group), "多个链接和说明混排，本地候选分组，等待 AI 判疑", 0.55)
        for group in groups
        if extract_urls("\n".join(group))
    ]


def _url_item(raw: str, reason: str, confidence: float, source: str = "rules") -> InputItem:
    urls = extract_urls(raw)
    return InputItem(
        raw_input=_compact_block(BULLET_RE.sub("", raw).strip()),
        source_type="url",
        primary_url=urls[0] if urls else None,
        group_confidence=confidence,
        group_reason=reason,
        grouping_source=source,
    )


def _is_pure_url_list(lines: list[str]) -> bool:
    return len(lines) > 1 and all(len(extract_urls(line)) == 1 and URL_RE.sub("", line).strip() == "" for line in lines)


def _is_url_bullet_list(lines: list[str]) -> bool:
    return len(lines) > 1 and all(BULLET_RE.match(line) and extract_urls(line) for line in lines)


def _looks_like_term_list(lines: list[str]) -> bool:
    if len(lines) <= 1:
        return False
    return all(_is_term(line) and not re.search(r"[。！？.!?，,；;：:]", line) for line in lines)


def _compact_block(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text.strip())


def _needs_ai_grouping(items: list[InputItem]) -> bool:
    return any(item.group_confidence < AI_GROUPING_THRESHOLD for item in items)


def _ai_group_input(
    text: str,
    local_items: list[InputItem],
    provider: str,
    model: str,
    api_key: str,
) -> list[InputItem]:
    payload = _grouping_payload(text, local_items)
    try:
        if provider == "openai":
            content = _call_openai_grouping(payload, model, api_key)
        else:
            content = _call_deepseek_grouping(payload, model, api_key)
        items = _parse_ai_grouping(content)
    except Exception:
        return local_items
    if not items or any(item.group_confidence < AI_ACCEPT_THRESHOLD for item in items):
        return local_items
    return _dedupe_items(items)


def _grouping_payload(text: str, local_items: list[InputItem]) -> dict[str, Any]:
    return {
        "input": text,
        "local_candidates": [
            {
                "source_type": item.source_type,
                "primary_url": item.primary_url,
                "raw_input": item.raw_input,
                "reason": item.group_reason,
                "confidence": item.group_confidence,
            }
            for item in local_items
        ],
    }


def _grouping_system_prompt() -> str:
    return (
        "你是拾签的输入分组助手。根据用户粘贴的原文和本地候选分组，判断哪些链接和文字属于同一条收藏。\n"
        "只做分组，不要总结内容，不要抓取网页。保留用户原文，不要编造不存在的内容。\n"
        "如果不确定，保持本地候选分组或给出较低 confidence。\n"
        "输出 JSON：{\"groups\":[{\"source_type\":\"url|term|text\",\"primary_url\":\"...或null\",\"raw_input\":\"...\",\"reason\":\"...\",\"confidence\":0.0到1.0}]}"
    )


def _call_openai_grouping(payload: dict[str, Any], model: str, api_key: str) -> str:
    request_json = {
        "model": model,
        "input": [
            {"role": "system", "content": _grouping_system_prompt()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "input_grouping",
                "strict": True,
                "schema": GROUPING_SCHEMA,
            }
        },
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_json,
        )
        response.raise_for_status()
    data = response.json()
    if data.get("output_text"):
        return data["output_text"]
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if content.get("text"):
                chunks.append(content["text"])
    return "\n".join(chunks)


def _call_deepseek_grouping(payload: dict[str, Any], model: str, api_key: str) -> str:
    request_json = {
        "model": model,
        "messages": [
            {"role": "system", "content": _grouping_system_prompt()},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
    }
    with httpx.Client(timeout=30) as client:
        response = client.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json=request_json,
        )
        response.raise_for_status()
    return response.json()["choices"][0]["message"].get("content") or ""


def _parse_ai_grouping(content: str) -> list[InputItem]:
    parsed = json.loads(content)
    groups = parsed.get("groups", [])
    items: list[InputItem] = []
    for group in groups:
        raw_input = _compact_block(str(group.get("raw_input") or ""))
        if not raw_input:
            continue
        source_type = str(group.get("source_type") or "text").strip()
        if source_type not in {"url", "term", "text"}:
            source_type = "text"
        primary_url = str(group.get("primary_url") or "").strip() or None
        if source_type == "url":
            urls = extract_urls(primary_url or raw_input)
            if not urls:
                continue
            primary_url = urls[0]
        confidence = _clamp_confidence(group.get("confidence"))
        items.append(
            InputItem(
                raw_input=raw_input,
                source_type=source_type,
                primary_url=primary_url,
                group_confidence=confidence,
                group_reason=str(group.get("reason") or "AI 判定分组").strip()[:240],
                grouping_source="ai",
            )
        )
    return items


def _dedupe_items(items: list[InputItem]) -> list[InputItem]:
    seen: set[tuple[str, str]] = set()
    unique: list[InputItem] = []
    for item in items:
        key = (item.source_type, normalize_text_for_hash(item.primary_url or item.raw_input))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return max(0.0, min(1.0, confidence))


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

def _is_term(text: str) -> bool:
    clean = text.strip()
    if len(clean) <= TERM_MAX_LENGTH and "\n" not in clean:
        return len(re.split(r"\s+", clean)) <= 6
    return False
