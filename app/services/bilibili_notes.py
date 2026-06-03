from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from app.services.settings_store import RuntimeSettings


@dataclass
class VideoContentResult:
    text: str
    source: str
    error: str | None = None
    trust: str = "trusted"
    trust_reason: str = ""


def video_payload_from_media(media: dict[str, Any]) -> dict[str, Any]:
    owner = media.get("upper") or {}
    ugc = media.get("ugc") or {}
    return {
        "bvid": media.get("bvid") or media.get("bv_id"),
        "title": media.get("title") or "",
        "cid": ugc.get("first_cid") or media.get("cid"),
        "aid": media.get("id") or media.get("aid"),
        "cover": media.get("cover"),
        "duration": media.get("duration"),
        "owner_name": owner.get("name"),
        "owner_mid": owner.get("mid"),
        "description": media.get("intro") or "",
    }


def extract_bilibili_summary_text(data: dict[str, Any] | None) -> str:
    if not data:
        return ""
    candidates = [data]
    model_result = data.get("model_result")
    if isinstance(model_result, dict):
        candidates.append(model_result)
    if isinstance(model_result, str):
        try:
            candidates.append(json.loads(model_result))
        except Exception:
            if model_result.strip():
                return model_result.strip()
    for source in candidates:
        for key in ["summary", "content", "result", "text"]:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        outline = source.get("outline")
        if isinstance(outline, list):
            lines = []
            for item in outline:
                title = item.get("title") if isinstance(item, dict) else ""
                part = item.get("part_outline") if isinstance(item, dict) else ""
                if title:
                    lines.append(str(title))
                if part:
                    lines.append(str(part))
            if lines:
                return "\n".join(lines)
    return ""


def subtitle_url_from_player(player: dict[str, Any] | None) -> str:
    subtitles = ((player or {}).get("subtitle") or {}).get("subtitles") or []
    for item in subtitles:
        url = item.get("subtitle_url") or item.get("url")
        if url:
            return url
    return ""


def assess_content_trust(video: dict[str, Any], text: str, source: str) -> tuple[str, str]:
    clean_text = _normalize_for_match(text)
    if len(clean_text) < 24:
        return "untrusted", "候选内容过短，无法确认与视频匹配"

    keywords = _metadata_keywords(video)
    if len(keywords) < 2:
        return "trusted", "视频元信息关键词较少，跳过严格相关性校验"

    matched = [keyword for keyword in keywords if keyword in clean_text]
    ratio = len(matched) / max(1, len(keywords))
    if len(matched) >= 2 or ratio >= 0.28:
        return "trusted", f"{source} 与标题/简介关键词匹配：{', '.join(matched[:5])}"
    return "untrusted", f"{source} 与视频标题/简介关联过低，匹配关键词 {len(matched)}/{len(keywords)}"


async def generate_video_note_markdown(
    *,
    video: dict[str, Any],
    content: VideoContentResult,
    folder_title: str,
    settings: RuntimeSettings,
) -> tuple[str, str, str | None]:
    fallback = build_fallback_video_note(video=video, content=content, folder_title=folder_title)
    source_text = (content.text or "").strip()
    if not source_text or content.source == "basic_info" or content.trust != "trusted":
        return fallback, "rules", content.error
    if settings.llm_provider == "openai" and settings.openai_api_key:
        return await _openai_video_note(video, source_text, folder_title, settings, fallback)
    if settings.llm_provider == "deepseek" and settings.deepseek_api_key:
        return await _deepseek_video_note(video, source_text, folder_title, settings, fallback)
    return fallback, "rules", "未配置模型 API key，已生成基础笔记"


def build_fallback_video_note(*, video: dict[str, Any], content: VideoContentResult, folder_title: str) -> str:
    title = video.get("title") or video.get("bvid") or "Bilibili 视频"
    bvid = video.get("bvid") or ""
    owner = video.get("owner_name") or "未知"
    description = (video.get("description") or "").strip()
    source_label = {
        "subtitle": "字幕",
        "ai_summary": "Bilibili AI 摘要",
        "basic_info": "基础信息",
        "untrusted_subtitle": "字幕疑似跑题",
        "untrusted_ai_summary": "Bilibili AI 摘要疑似跑题",
    }.get(content.source, content.source or "基础信息")
    body = [
        f"# {title}",
        "",
        f"> 来源：[Bilibili 视频](https://www.bilibili.com/video/{bvid})",
        f"> BVID：{bvid}",
        f"> UP主：{owner}",
        f"> 收藏夹：{folder_title or '未命名收藏夹'}",
        f"> 内容来源：{source_label}",
    ]
    if description:
        body.append(f"> 视频简介：{description}")
    if content.trust != "trusted":
        body.append(f"> 内容提示：{content.trust_reason or '未找到可信字幕或摘要，已使用基础信息。'}")
    body.extend([
        "",
        "## 一句话总结",
        "",
        _one_line_summary(title, description, "" if content.trust != "trusted" else content.text),
        "",
        "## 核心要点",
        "",
    ])
    point_source = (description or title) if content.trust != "trusted" else (content.text or description or title)
    body.extend(f"- {point}" for point in _points_from_text(point_source))
    body.extend(["", "## 原始信息", ""])
    if description:
        body.extend(["### 视频简介", "", description, ""])
    if content.trust != "trusted" and content.trust_reason:
        body.extend(["### 处理提示", "", content.trust_reason])
    elif content.text and content.source != "basic_info":
        body.extend(["### 可用文本", "", _clamp(content.text, 5000)])
    elif content.error:
        body.extend(["### 处理提示", "", content.error])
    return "\n".join(body).strip() + "\n"


async def _openai_video_note(
    video: dict[str, Any],
    content: str,
    folder_title: str,
    settings: RuntimeSettings,
    fallback: str,
) -> tuple[str, str, str | None]:
    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": _note_prompt()},
            {"role": "user", "content": _prompt_payload(video, content, folder_title)},
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                _openai_responses_url(settings),
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        text = data.get("output_text") or _extract_openai_output_text(data)
        return _normalize_markdown(text, fallback), "openai", None
    except Exception as exc:
        return fallback, "openai", str(exc)


async def _deepseek_video_note(
    video: dict[str, Any],
    content: str,
    folder_title: str,
    settings: RuntimeSettings,
    fallback: str,
) -> tuple[str, str, str | None]:
    payload = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": _note_prompt()},
            {"role": "user", "content": _prompt_payload(video, content, folder_title)},
        ],
        "temperature": 0.25,
    }
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.deepseek.com/chat/completions",
                headers={"Authorization": f"Bearer {settings.deepseek_api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        text = data["choices"][0]["message"].get("content") or ""
        return _normalize_markdown(text, fallback), "deepseek", None
    except Exception as exc:
        return fallback, "deepseek", str(exc)


def _note_prompt() -> str:
    return (
        "你是拾签的学习笔记整理助手。请把 Bilibili 视频字幕或摘要整理为 Markdown 笔记。\n"
        "只生成 ## 一句话总结、## 核心要点、## 可复用知识 三个部分。\n"
        "不要生成 # 标题，不要生成来源信息，不要改写视频标题、UP主、链接或简介。\n"
        "不要编造原文没有的信息；如果文本很散，就提炼可确认的要点。只输出 Markdown。"
    )


def _prompt_payload(video: dict[str, Any], content: str, folder_title: str) -> str:
    return json.dumps(
        {
            "title": video.get("title"),
            "bvid": video.get("bvid"),
            "url": f"https://www.bilibili.com/video/{video.get('bvid')}",
            "owner": video.get("owner_name"),
            "folder": folder_title,
            "description": video.get("description"),
            "content": _clamp(content, 12000),
        },
        ensure_ascii=False,
    )


def _extract_openai_output_text(data: dict[str, Any]) -> str:
    chunks = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _openai_responses_url(settings: RuntimeSettings) -> str:
    base_url = (getattr(settings, "openai_base_url", "") or "https://api.openai.com/v1").strip().rstrip("/")
    if base_url.endswith("/responses"):
        return base_url
    return f"{base_url}/responses"


def _normalize_markdown(text: str, fallback: str) -> str:
    clean = (text or "").strip()
    if len(clean) < 20:
        return fallback
    return _strip_ai_source_sections(clean) + "\n"


def _one_line_summary(title: str, description: str, content: str) -> str:
    base = description or content or title
    compact = " ".join(str(base).split())
    return compact[:180] or f"{title} 的视频学习笔记。"


def _points_from_text(text: str) -> list[str]:
    compact = [line.strip(" -\t") for line in str(text or "").splitlines() if line.strip()]
    points = []
    for line in compact:
        if len(line) < 8:
            continue
        points.append(_clamp(line, 120))
        if len(points) >= 5:
            break
    return points or ["保留视频来源信息，后续可继续补充整理。"]


def _clamp(text: str, limit: int) -> str:
    return str(text or "").strip()[:limit]


def compose_video_note(*, video: dict[str, Any], content: VideoContentResult, folder_title: str, generated_md: str) -> str:
    title = video.get("title") or video.get("bvid") or "Bilibili 视频"
    bvid = video.get("bvid") or ""
    owner = video.get("owner_name") or "未知"
    description = (video.get("description") or "").strip()
    source_label = {
        "subtitle": "字幕可信",
        "ai_summary": "Bilibili AI 摘要可信",
        "basic_info": "基础信息",
        "untrusted_subtitle": "字幕疑似跑题",
        "untrusted_ai_summary": "Bilibili AI 摘要疑似跑题",
    }.get(content.source, content.source or "基础信息")
    header = [
        f"# {title}",
        "",
        f"> 来源：[Bilibili 视频](https://www.bilibili.com/video/{bvid})",
        f"> BVID：{bvid}",
        f"> UP主：{owner}",
        f"> 收藏夹：{folder_title or '未命名收藏夹'}",
        f"> 内容来源：{source_label}",
    ]
    if description:
        header.append(f"> 视频简介：{description}")
    if content.trust != "trusted":
        header.append(f"> 内容提示：{content.trust_reason or '未找到可信字幕或摘要，已使用基础信息。'}")
    body = _strip_ai_source_sections(generated_md)
    if not body.lstrip().startswith("## "):
        body = "\n".join(["## 自动笔记", "", body.strip()])
    return "\n".join(header).strip() + "\n\n" + body.strip() + "\n"


def _metadata_keywords(video: dict[str, Any]) -> list[str]:
    source = " ".join(str(video.get(key) or "") for key in ["title", "description", "owner_name"])
    text = re.sub(r"\s+", " ", source.lower())
    raw = re.findall(r"[a-z0-9+#.]{2,}|[\u4e00-\u9fff]{2,}", text)
    stopwords = {
        "视频", "教程", "全局", "回放", "分享", "一个", "这个", "可以", "大家", "小白", "使用",
        "支持", "链接", "下载", "地址", "感谢", "一键", "详细", "新版", "全新",
    }
    keywords: list[str] = []
    for item in raw:
        if item in stopwords:
            continue
        if len(item) >= 12 and re.fullmatch(r"[\u4e00-\u9fff]+", item):
            keywords.extend(item[index : index + 4] for index in range(0, min(len(item), 20) - 3, 4))
        else:
            keywords.append(item)
    seen = set()
    unique = []
    for keyword in keywords:
        if keyword not in seen:
            seen.add(keyword)
            unique.append(keyword)
    return unique[:18]


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _strip_ai_source_sections(markdown: str) -> str:
    kept: list[str] = []
    skipping = False
    seen_section = False
    for line in str(markdown or "").strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            continue
        if not seen_section and not stripped.startswith("## "):
            if stripped.startswith(">") or not stripped:
                continue
            kept.append(line)
            continue
        if stripped.startswith("## "):
            seen_section = True
            heading = stripped.lstrip("#").strip()
            skipping = any(word in heading for word in ["来源", "原始信息", "视频信息"])
            if skipping:
                continue
        if not skipping:
            kept.append(line)
    clean = "\n".join(kept).strip()
    return clean or str(markdown or "").strip()
