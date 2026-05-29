from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings


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


@dataclass
class SuggestionResult:
    suggestion: dict[str, Any]
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
            return _openai_suggestion(extracted, existing_directories, settings)
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return SuggestionResult(fallback, "openai", settings.openai_model, "OPENAI_API_KEY is not configured")
    if selected_provider == "deepseek":
        if settings.deepseek_api_key:
            return _deepseek_suggestion(extracted, existing_directories, settings)
        fallback = _heuristic_suggestion(extracted, existing_directories)
        return SuggestionResult(fallback, "deepseek", settings.deepseek_model, "DEEPSEEK_API_KEY is not configured")
    fallback = _heuristic_suggestion(extracted, existing_directories)
    return SuggestionResult(fallback, "rules", "local", f"Unknown provider: {selected_provider}")


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
                '"content_type":"webpage","recommended_directory_path":"技术/AI","directory_reason":"...",'
                '"confidence":0.8}\n\n'
                f"Item data:\n{json.dumps(extracted, ensure_ascii=False)}"
            ),
        },
    ]
    payload = {
        "model": settings.deepseek_model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
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


def _system_prompt(existing_directories: list[str]) -> str:
    directories = "\n".join(f"- {path}" for path in existing_directories[:120]) or "- 当前没有目录"
    return (
        "你是一个本地收藏管理助手。根据收藏内容生成一个清晰短名称、简短中文简介、"
        "短中文标签、3到8个关键词、内容类型，以及一个可多级的目录路径。"
        "URL 项目基于网页信息整理；词和段落按整理+适度扩展处理，不写长文。"
        "优先复用已有目录；如果没有合适目录，可以推荐新目录。"
        "目录路径用 / 分隔，例如 技术/AI/提示词。不要输出 Markdown。\n\n"
        f"已有目录:\n{directories}"
    )


def _load_suggestion_json(content: str) -> dict[str, Any]:
    parsed = json.loads(content)
    return {
        "name": str(parsed.get("name") or parsed.get("title") or "").strip()[:120],
        "summary": str(parsed.get("summary") or "").strip()[:1000],
        "tags": [str(tag).strip()[:40] for tag in parsed.get("tags", []) if str(tag).strip()][:8] or ["待整理"],
        "keywords": [str(keyword).strip()[:32] for keyword in parsed.get("keywords", []) if str(keyword).strip()][:8]
        or ["待整理"],
        "content_type": str(parsed.get("content_type") or "webpage").strip()[:80],
        "recommended_directory_path": str(parsed.get("recommended_directory_path") or "未分类").strip()[:600],
        "directory_reason": str(parsed.get("directory_reason") or "").strip()[:500],
        "confidence": float(parsed.get("confidence") or 0.5),
    }


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
    directory = "未分类"
    reason = "未配置 API key，使用本地规则生成建议。"

    if source_type in {"term", "text"}:
        summary = _text_summary(str(extracted.get("raw_input") or title), source_type)
        content_type = source_type
        tags = ["词条" if source_type == "term" else "笔记"]
        directory = "未分类"
        if not keywords:
            keywords = [name]
    elif content_type == "video":
        tags = ["视频", domain.split(".")[0] if domain else "媒体"]
        directory = "视频"
    elif "github.com" in domain:
        tags = ["代码", "开源"]
        directory = "技术/代码"
    elif any(token in domain for token in ["arxiv", "nature", "science", "cell"]):
        tags = ["论文", "研究"]
        directory = "论文"
    elif any(token in f"{title} {description}".lower() for token in ["ai", "openai", "llm", "prompt"]):
        tags = ["AI", "工具"]
        directory = "技术/AI"

    if existing_directories and directory not in existing_directories:
        for path in existing_directories:
            if path.startswith(directory.split("/")[0]):
                directory = path
                reason = "未配置 API key，使用本地规则并优先复用已有目录。"
                break

    return {
        "name": name,
        "summary": summary,
        "tags": tags,
        "keywords": keywords or tags,
        "content_type": content_type,
        "recommended_directory_path": directory,
        "directory_reason": reason,
        "confidence": 0.35,
    }


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
    import re

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
