from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


UNCATEGORIZED = "未分类"
LOW_CONFIDENCE_THRESHOLD = 0.65
NEW_DIRECTORY_THRESHOLD = 0.8

GENERIC_DIRECTORY_PARTS = {
    "工具",
    "资料",
    "网页",
    "内容",
    "收藏",
    "分类",
    "其他",
    "默认",
    "资源",
    "待整理",
    "未分类",
    "general",
    "misc",
    "other",
}
GENERIC_TAGS = {
    "工具",
    "资料",
    "网页",
    "内容",
    "收藏",
    "资源",
    "链接",
    "文章",
    "页面",
    "其他",
    "默认",
    "未分类",
    "general",
    "misc",
    "web",
}

SUGGESTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {"type": "string"},
        "summary": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        "keywords": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
        "content_type": {"type": "string"},
        "recommended_directory_path": {"type": "string"},
        "directory_reason": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": [
        "name",
        "summary",
        "tags",
        "keywords",
        "content_type",
        "recommended_directory_path",
        "directory_reason",
        "confidence",
    ],
}

SUMMARY_REWRITE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
    },
    "required": ["summary"],
}


@dataclass
class SuggestionResult:
    suggestion: dict[str, Any]
    provider: str
    model: str
    error: str | None = None


@dataclass
class SummaryRewriteResult:
    summary: str
    provider: str
    model: str
    error: str | None = None


def generate_suggestion(
    extracted: dict[str, Any],
    existing_directories: list[str],
    provider: str | None,
    settings: Settings,
) -> SuggestionResult:
    selected_provider = (provider or settings.llm_provider or "deepseek").lower()
    if selected_provider == "openai":
        if settings.openai_api_key:
            return _finalize_result(_openai_suggestion(extracted, existing_directories, settings), existing_directories)
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return _finalize_result(
            SuggestionResult(fallback, "openai", settings.openai_model, "OPENAI_API_KEY is not configured"),
            existing_directories,
        )
    if selected_provider == "deepseek":
        if settings.deepseek_api_key:
            return _finalize_result(_deepseek_suggestion(extracted, existing_directories, settings), existing_directories)
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return _finalize_result(
            SuggestionResult(fallback, "deepseek", settings.deepseek_model, "DEEPSEEK_API_KEY is not configured"),
            existing_directories,
        )
    fallback = _heuristic_suggestion(extracted, existing_directories)
    return _finalize_result(SuggestionResult(fallback, "rules", "local", f"Unknown provider: {selected_provider}"), existing_directories)


def rewrite_summary(
    extracted: dict[str, Any],
    suggestion: dict[str, Any],
    current_summary: str,
    length: str,
    style: str,
    provider: str | None,
    settings: Settings,
) -> SummaryRewriteResult:
    length = length if length in {"brief", "normal", "detailed"} else "normal"
    style = style if style in {"note", "beginner", "expert", "neutral"} else "note"
    selected_provider = (provider or settings.llm_provider or "deepseek").lower()
    if selected_provider in {"rules", "local"}:
        return SummaryRewriteResult(
            _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
            "rules",
            "local",
        )
    if selected_provider == "openai":
        if settings.openai_api_key:
            return _openai_rewrite_summary(extracted, suggestion, current_summary, length, style, settings)
        return SummaryRewriteResult(
            _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
            "openai",
            settings.openai_model,
            "OPENAI_API_KEY is not configured",
        )
    if selected_provider == "deepseek":
        if settings.deepseek_api_key:
            return _deepseek_rewrite_summary(extracted, suggestion, current_summary, length, style, settings)
        return SummaryRewriteResult(
            _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
            "deepseek",
            settings.deepseek_model,
            "DEEPSEEK_API_KEY is not configured",
        )
    return SummaryRewriteResult(
        _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
        "rules",
        "local",
        f"Unknown provider: {selected_provider}",
    )


def normalize_suggestion(suggestion: dict[str, Any], existing_directories: list[str]) -> dict[str, Any]:
    normalized = dict(suggestion)
    existing_set = {_clean_directory_path(path) for path in existing_directories if _clean_directory_path(path)}
    raw_directory = _clean_directory_path(normalized.get("recommended_directory_path") or UNCATEGORIZED)
    confidence = _clamp_confidence(normalized.get("confidence"))
    notes: list[str] = []

    final_directory = raw_directory
    policy = "accepted"
    if raw_directory == UNCATEGORIZED:
        policy = "uncategorized"
        notes.append("模型没有给出明确目录，先放入未分类。")
    elif confidence < LOW_CONFIDENCE_THRESHOLD:
        final_directory = UNCATEGORIZED
        policy = "low_confidence_uncategorized"
        notes.append(f"目录置信度 {confidence:.2f} 低于 {LOW_CONFIDENCE_THRESHOLD:.2f}，先放入未分类。")
    elif raw_directory in existing_set:
        policy = "accepted_existing"
        notes.append("目录建议命中已有目录。")
    elif confidence < NEW_DIRECTORY_THRESHOLD:
        final_directory = UNCATEGORIZED
        policy = "new_directory_needs_review"
        notes.append(f"这是新目录建议，但置信度低于 {NEW_DIRECTORY_THRESHOLD:.2f}，先放入未分类。")
    elif _is_generic_directory(raw_directory):
        final_directory = UNCATEGORIZED
        policy = "generic_directory_uncategorized"
        notes.append("新目录名称过于泛化，先放入未分类。")
    else:
        policy = "accepted_new"
        notes.append("目录建议置信度较高，允许作为新目录候选。")

    tags = _normalize_tags(normalized.get("tags", []))
    keywords = _normalize_keywords(normalized.get("keywords", []), tags, normalized.get("name"))

    normalized["name"] = str(normalized.get("name") or normalized.get("title") or "").strip()[:120] or "未命名收藏"
    normalized["summary"] = str(normalized.get("summary") or "").strip()[:1000]
    normalized["tags"] = tags
    normalized["keywords"] = keywords
    normalized["content_type"] = str(normalized.get("content_type") or "webpage").strip()[:80]
    normalized["raw_recommended_directory_path"] = raw_directory
    normalized["recommended_directory_path"] = final_directory
    normalized["directory_reason"] = str(normalized.get("directory_reason") or "").strip()[:500]
    normalized["confidence"] = confidence
    normalized["directory_confidence"] = confidence
    normalized["directory_policy"] = policy
    normalized["classification_notes"] = notes
    return normalized


def _finalize_result(result: SuggestionResult, existing_directories: list[str]) -> SuggestionResult:
    result.suggestion = normalize_suggestion(result.suggestion, existing_directories)
    return result


def _openai_suggestion(extracted: dict[str, Any], existing_directories: list[str], settings: Settings) -> SuggestionResult:
    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": _system_prompt(existing_directories)},
            {"role": "user", "content": json.dumps(extracted, ensure_ascii=False)},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "bookmark_suggestion",
                "strict": True,
                "schema": SUGGESTION_SCHEMA,
            }
        },
    }
    try:
        with httpx.Client(timeout=40) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        content = data.get("output_text") or _extract_openai_output_text(data)
        return SuggestionResult(_load_suggestion_json(content), "openai", settings.openai_model)
    except Exception as exc:
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return SuggestionResult(fallback, "openai", settings.openai_model, str(exc))


def _deepseek_suggestion(extracted: dict[str, Any], existing_directories: list[str], settings: Settings) -> SuggestionResult:
    messages = [
        {"role": "system", "content": _system_prompt(existing_directories)},
        {
            "role": "user",
            "content": (
                "Return only a JSON object matching this example shape: "
                '{"name":"...","summary":"...","tags":["..."],"keywords":["..."],'
                '"content_type":"webpage","recommended_directory_path":"未分类",'
                '"directory_reason":"不确定，先放入未分类","confidence":0.4}\n\n'
                f"Item data:\n{json.dumps(extracted, ensure_ascii=False)}"
            ),
        },
    ]
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.15,
    }
    try:
        content = ""
        with httpx.Client(timeout=40) as client:
            for _ in range(2):
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"].get("content") or ""
                if content.strip():
                    break
        if not content.strip():
            raise ValueError("DeepSeek returned empty content")
        return SuggestionResult(_load_suggestion_json(content), "deepseek", settings.deepseek_model)
    except Exception as exc:
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return SuggestionResult(fallback, "deepseek", settings.deepseek_model, str(exc))


def _openai_rewrite_summary(
    extracted: dict[str, Any],
    suggestion: dict[str, Any],
    current_summary: str,
    length: str,
    style: str,
    settings: Settings,
) -> SummaryRewriteResult:
    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": _summary_prompt(length, style)},
            {
                "role": "user",
                "content": json.dumps(_summary_payload(extracted, suggestion, current_summary), ensure_ascii=False),
            },
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "summary_rewrite",
                "strict": True,
                "schema": SUMMARY_REWRITE_SCHEMA,
            }
        },
    }
    try:
        with httpx.Client(timeout=40) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
            response.raise_for_status()
        data = response.json()
        content = data.get("output_text") or _extract_openai_output_text(data)
        return SummaryRewriteResult(_load_summary_json(content), "openai", settings.openai_model)
    except Exception as exc:
        return SummaryRewriteResult(
            _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
            "openai",
            settings.openai_model,
            str(exc),
        )


def _deepseek_rewrite_summary(
    extracted: dict[str, Any],
    suggestion: dict[str, Any],
    current_summary: str,
    length: str,
    style: str,
    settings: Settings,
) -> SummaryRewriteResult:
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _summary_prompt(length, style)},
            {
                "role": "user",
                "content": (
                    'Return only JSON like {"summary":"..."}.\n\n'
                    f"Item data:\n{json.dumps(_summary_payload(extracted, suggestion, current_summary), ensure_ascii=False)}"
                ),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.25,
    }
    try:
        content = ""
        with httpx.Client(timeout=40) as client:
            for _ in range(2):
                response = client.post(
                    "https://api.deepseek.com/chat/completions",
                    headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"].get("content") or ""
                if content.strip():
                    break
        if not content.strip():
            raise ValueError("DeepSeek returned empty content")
        return SummaryRewriteResult(_load_summary_json(content), "deepseek", settings.deepseek_model)
    except Exception as exc:
        return SummaryRewriteResult(
            _heuristic_rewrite_summary(extracted, suggestion, current_summary, length, style),
            "deepseek",
            settings.deepseek_model,
            str(exc),
        )


def _system_prompt(existing_directories: list[str]) -> str:
    directories = "\n".join(f"- {path}" for path in existing_directories[:120]) or "- 当前没有目录"
    return (
        "你是本地收藏管理器“拾签”的分类助手。请为收藏项生成短名称、中文简介、标签、关键词、内容类型和目录建议。\n"
        "重要规则：目录是主归档位置；标签是跨目录主题；关键词只用于搜索。\n"
        "目录必须谨慎：优先复用已有目录。如果不确定，recommended_directory_path 必须写“未分类”，不要强行分类。\n"
        "只有内容主题非常明确，且已有目录都不合适时，才建议新目录，并给出较高 confidence。\n"
        "标签要少而准，通常 2-4 个；避免宽泛标签，如“工具、资料、网页、内容、收藏、资源”。\n"
        "关键词保留 3-8 个便于检索，可以比标签更具体。目录路径用 / 分隔，例如 技术/AI/提示词。\n"
        "不要输出 Markdown，只输出 JSON。\n\n"
        f"已有目录:\n{directories}"
    )


def _load_suggestion_json(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    return {
        "name": str(parsed.get("name") or parsed.get("title") or "").strip()[:120],
        "summary": str(parsed.get("summary") or "").strip()[:1000],
        "tags": [str(tag).strip()[:40] for tag in parsed.get("tags", []) if str(tag).strip()][:8],
        "keywords": [str(keyword).strip()[:32] for keyword in parsed.get("keywords", []) if str(keyword).strip()][:8],
        "content_type": str(parsed.get("content_type") or "webpage").strip()[:80],
        "recommended_directory_path": str(parsed.get("recommended_directory_path") or UNCATEGORIZED).strip()[:600],
        "directory_reason": str(parsed.get("directory_reason") or "").strip()[:500],
        "confidence": _clamp_confidence(parsed.get("confidence")),
    }


def _load_summary_json(content: str) -> str:
    parsed = json.loads(content)
    return _clamp_summary(parsed.get("summary") or "")


def _extract_openai_output_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _heuristic_suggestion(extracted: dict[str, Any], existing_directories: list[str]) -> dict[str, Any]:
    domain = extracted.get("source_domain") or ""
    title = extracted.get("title") or extracted.get("raw_input") or domain or "未命名收藏"
    description = extracted.get("description") or extracted.get("content") or ""
    content_type = extracted.get("content_type") or "webpage"
    source_type = extracted.get("source_type") or "url"
    keywords = _local_keywords(f"{title} {description}")
    name = _short_name(title)
    summary = (description.strip().replace("\n", " ")[:260] or f"{title}，来自 {domain}。").strip()
    tags = ["待整理"]
    directory = UNCATEGORIZED
    confidence = 0.35
    reason = "未配置 API key，使用本地规则生成建议；不确定时先放入未分类。"

    if source_type in {"term", "text"}:
        summary = _text_summary(str(extracted.get("raw_input") or title), source_type)
        content_type = source_type
        tags = ["词条" if source_type == "term" else "笔记"]
        if not keywords:
            keywords = [name]
    elif content_type == "video":
        tags = ["视频", domain.split(".")[0] if domain else "媒体"]
        directory = _prefer_existing_directory("视频", existing_directories)
        confidence = 0.82
        reason = "内容类型明确为视频，本地规则给出较高置信度。"
    elif "github.com" in domain:
        tags = ["代码", "开源"]
        directory = _prefer_existing_directory("技术/代码", existing_directories)
        confidence = 0.86
        reason = "来源为 GitHub，明确归入代码或开源资料。"
    elif any(token in domain for token in ["arxiv", "nature", "science", "cell"]):
        tags = ["论文", "研究"]
        directory = _prefer_existing_directory("论文", existing_directories)
        confidence = 0.86
        reason = "来源域名明显属于论文或学术出版。"
    elif any(token in f"{title} {description}".lower() for token in ["openai", "llm", "prompt", "deepseek"]):
        tags = ["AI"]
        directory = _prefer_existing_directory("技术/AI", existing_directories)
        confidence = 0.55
        reason = "文本包含 AI 相关词，但本地规则置信度不足，默认会先放入未分类。"

    return {
        "name": name,
        "summary": summary,
        "tags": tags,
        "keywords": keywords or tags,
        "content_type": content_type,
        "recommended_directory_path": directory,
        "directory_reason": reason,
        "confidence": confidence,
    }


def _summary_prompt(length: str, style: str) -> str:
    length_text = {
        "brief": "简略：1 句话，约 40-80 个中文字符。",
        "normal": "适中：2-3 句话，约 100-180 个中文字符。",
        "detailed": "详细：3-5 句话，约 220-360 个中文字符。",
    }[length]
    style_text = {
        "note": "收藏笔记风：说明这是什么、为什么值得存、以后怎么回看。",
        "beginner": "小白友好：少术语，用容易理解的说法解释价值。",
        "expert": "业内人士：保留关键概念和判断，信息密度高一些。",
        "neutral": "中性客观：只描述内容和用途，不夸张不营销。",
    }[style]
    return (
        "你是“拾签”的收藏简介编辑。只重写简介，不改名称、标签、关键词或目录。\n"
        f"长度要求：{length_text}\n"
        f"风格要求：{style_text}\n"
        "简介要便于日后检索和快速判断价值；不能编造原内容没有的信息。\n"
        "不要输出 Markdown，只输出 JSON。"
    )


def _summary_payload(extracted: dict[str, Any], suggestion: dict[str, Any], current_summary: str) -> dict[str, Any]:
    return {
        "title": suggestion.get("name") or extracted.get("title"),
        "current_summary": current_summary or suggestion.get("summary"),
        "tags": suggestion.get("tags") or [],
        "keywords": suggestion.get("keywords") or [],
        "content_type": suggestion.get("content_type") or extracted.get("content_type"),
        "source_type": extracted.get("source_type"),
        "source_domain": extracted.get("source_domain"),
        "url": extracted.get("url"),
        "raw_input": extracted.get("raw_input"),
        "description": extracted.get("description"),
        "content": str(extracted.get("content") or "")[:2000],
    }


def _heuristic_rewrite_summary(
    extracted: dict[str, Any],
    suggestion: dict[str, Any],
    current_summary: str,
    length: str,
    style: str,
) -> str:
    title = str(suggestion.get("name") or extracted.get("title") or extracted.get("raw_input") or "这个收藏").strip()
    base = str(current_summary or suggestion.get("summary") or extracted.get("description") or extracted.get("content") or "").strip()
    raw = str(extracted.get("raw_input") or extracted.get("url") or "").strip()
    keywords = "、".join([str(item) for item in suggestion.get("keywords", [])[:4] if str(item).strip()])
    if not base:
        base = raw or title

    if style == "beginner":
        prefix = f"{title}：这是一条适合先存起来慢慢看的内容"
    elif style == "expert":
        prefix = f"{title}：可作为后续检索的高密度资料"
    elif style == "neutral":
        prefix = f"{title}：内容围绕该主题展开"
    else:
        prefix = f"{title}：值得保存，方便之后回看"

    if length == "brief":
        summary = f"{prefix}，重点是{keywords or _short_name(base)}。"
    elif length == "detailed":
        summary = (
            f"{prefix}。原内容要点是：{base[:220]}。"
            f"{' 相关关键词包括：' + keywords + '。' if keywords else ''}"
            "后续可以用它辅助检索、归档或继续整理。"
        )
    else:
        summary = f"{prefix}。{base[:140]}{' 关键词：' + keywords + '。' if keywords else ''}"
    return _clamp_summary(summary)


def _clamp_summary(summary: Any) -> str:
    return str(summary or "").strip().replace("\r\n", "\n")[:1000]


def _prefer_existing_directory(candidate: str, existing_directories: list[str]) -> str:
    if candidate in existing_directories:
        return candidate
    root = candidate.split("/", 1)[0]
    for path in existing_directories:
        if path == root or path.startswith(f"{root}/"):
            return path
    return candidate


def _clean_directory_path(path: Any) -> str:
    parts = [re.sub(r"\s+", " ", str(part).strip())[:80] for part in re.split(r"[\\/]+", str(path or ""))]
    parts = [part for part in parts if part]
    return "/".join(parts) or UNCATEGORIZED


def _is_generic_directory(path: str) -> bool:
    if path == UNCATEGORIZED:
        return False
    parts = [part.casefold() for part in path.split("/")]
    return any(part in GENERIC_DIRECTORY_PARTS for part in parts)


def _normalize_tags(raw_tags: Any) -> list[str]:
    seen: set[str] = set()
    tags: list[str] = []
    for item in raw_tags or []:
        tag = re.sub(r"\s+", " ", str(item).strip())[:40]
        key = tag.casefold()
        if not tag or key in seen or key in GENERIC_TAGS:
            continue
        seen.add(key)
        tags.append(tag)
    return tags[:4] or ["待整理"]


def _normalize_keywords(raw_keywords: Any, tags: list[str], name: Any) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []
    for item in list(raw_keywords or []) + tags + _local_keywords(str(name or "")):
        keyword = re.sub(r"\s+", " ", str(item).strip())[:32]
        key = keyword.casefold()
        if not keyword or key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
    return keywords[:8] or ["待整理"]


def _clamp_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 0.5
    return max(0.0, min(1.0, confidence))


def _short_name(text: str) -> str:
    compact = " ".join(str(text).strip().split())
    if not compact:
        return "未命名收藏"
    return compact[:60]


def _text_summary(text: str, source_type: str) -> str:
    compact = " ".join(text.strip().split())
    if source_type == "term":
        return f"{compact}：一个待整理的词条，可用于后续检索和归类。"
    return compact[:260]


def _local_keywords(text: str) -> list[str]:
    candidates = re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_+-]{2,30}", text)
    seen: set[str] = set()
    keywords: list[str] = []
    for candidate in candidates:
        key = candidate.casefold()
        if key in seen or key in {"https", "http", "www", "com", "the", "and", "for"}:
            continue
        seen.add(key)
        keywords.append(candidate)
    return keywords[:8]
