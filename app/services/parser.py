from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.urls import canonicalize_url, source_domain


VIDEO_DOMAINS = {
    "youtube.com",
    "youtu.be",
    "bilibili.com",
    "vimeo.com",
    "douyin.com",
    "tiktok.com",
}


@dataclass
class ExtractedLink:
    url: str
    canonical_url: str
    title: str
    description: str
    content: str
    content_type: str
    source_domain: str
    image_url: str | None = None
    author: str | None = None
    site_name: str | None = None
    fetch_status: str = "ok"

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "title": self.title,
            "description": self.description,
            "content": self.content,
            "content_type": self.content_type,
            "source_domain": self.source_domain,
            "image_url": self.image_url,
            "author": self.author,
            "site_name": self.site_name,
            "fetch_status": self.fetch_status,
        }


def fetch_and_extract(url: str) -> ExtractedLink:
    canonical = canonicalize_url(url)
    domain = source_domain(canonical)
    try:
        with httpx.Client(follow_redirects=True, timeout=12) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 BookmarkManager/0.1"
                    )
                },
            )
            response.raise_for_status()
    except Exception as exc:
        return ExtractedLink(
            url=url,
            canonical_url=canonical,
            title=domain or url,
            description="",
            content="",
            content_type=_content_type_for_domain(domain),
            source_domain=domain,
            fetch_status=f"error: {exc}",
        )

    final_url = str(response.url)
    canonical = canonicalize_url(final_url)
    domain = source_domain(canonical)
    content_type_header = response.headers.get("content-type", "")
    if "text/html" not in content_type_header.lower():
        return ExtractedLink(
            url=final_url,
            canonical_url=canonical,
            title=domain or final_url,
            description=content_type_header,
            content="",
            content_type=_content_type_for_domain(domain),
            source_domain=domain,
            fetch_status="non_html",
        )

    soup = BeautifulSoup(response.text, "html.parser")
    title = _first_meta(soup, ["og:title", "twitter:title"]) or (soup.title.string.strip() if soup.title and soup.title.string else "")
    description = _first_meta(soup, ["description", "og:description", "twitter:description"]) or ""
    site_name = _first_meta(soup, ["og:site_name"])
    image_url = _first_meta(soup, ["og:image", "twitter:image"])
    author = _first_meta(soup, ["author", "article:author"])
    og_type = _first_meta(soup, ["og:type"]) or ""
    content_type = "video" if "video" in og_type.lower() else _content_type_for_domain(domain)
    body_text = _extract_body_text(soup)

    return ExtractedLink(
        url=final_url,
        canonical_url=canonical,
        title=title or domain or final_url,
        description=description,
        content=body_text,
        content_type=content_type,
        source_domain=domain,
        image_url=image_url,
        author=author,
        site_name=site_name,
    )


def _content_type_for_domain(domain: str) -> str:
    root = ".".join(domain.split(".")[-2:])
    if root in VIDEO_DOMAINS or domain in VIDEO_DOMAINS or any(domain.endswith(f".{item}") for item in VIDEO_DOMAINS):
        return "video"
    if "github.com" in domain:
        return "code"
    return "webpage"


def _first_meta(soup: BeautifulSoup, names: list[str]) -> str | None:
    for name in names:
        selectors = [
            {"name": name},
            {"property": name},
            {"itemprop": name},
        ]
        for selector in selectors:
            tag = soup.find("meta", attrs=selector)
            if tag and tag.get("content"):
                return str(tag["content"]).strip()
    return None


def _extract_body_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav", "form"]):
        tag.decompose()
    candidates = [node.get_text(" ", strip=True) for node in soup.find_all(["article", "main", "p", "h1", "h2", "li"])]
    text = "\n".join(item for item in candidates if len(item) > 20)
    if not text:
        text = soup.get_text("\n", strip=True)
    return text[:8000]

