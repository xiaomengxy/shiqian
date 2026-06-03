import httpx

from app.config import Settings
from app.services.llm import generate_suggestion, normalize_suggestion


def test_generate_suggestion_falls_back_without_key():
    settings = Settings(llm_provider="deepseek", deepseek_api_key="")

    result = generate_suggestion(
        {
            "title": "OpenAI prompt guide",
            "description": "AI prompt notes",
            "content_type": "webpage",
            "source_type": "url",
            "source_domain": "example.com",
        },
        [],
        "deepseek",
        settings,
    )

    assert result.provider == "deepseek"
    assert result.error == "DEEPSEEK_API_KEY is not configured"
    assert result.suggestion["recommended_directory_path"] == "未分类"
    assert result.suggestion["raw_recommended_directory_path"] == "技术/AI"
    assert result.suggestion["directory_policy"] == "low_confidence_uncategorized"
    assert result.suggestion["name"]
    assert result.suggestion["keywords"]


def test_text_suggestion_falls_back_without_key():
    settings = Settings(llm_provider="deepseek", deepseek_api_key="")

    result = generate_suggestion(
        {
            "title": "prompt engineering",
            "raw_input": "prompt engineering",
            "content": "prompt engineering",
            "content_type": "term",
            "source_type": "term",
            "source_domain": "",
        },
        [],
        "deepseek",
        settings,
    )

    assert result.suggestion["content_type"] == "term"
    assert result.suggestion["tags"] == ["词条"]
    assert result.suggestion["recommended_directory_path"] == "未分类"
    assert result.suggestion["keywords"]


def test_deepseek_json_response_is_normalized(monkeypatch):
    def fake_post(self, url, headers=None, json=None):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"name":"Name","summary":"Summary","tags":["web"],"keywords":["keyword"],'
                                '"content_type":"webpage","recommended_directory_path":"Research/Web",'
                                '"directory_reason":"match","confidence":0.9}'
                            )
                        }
                    }
                ]
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    settings = Settings(llm_provider="deepseek", deepseek_api_key="sk-test", deepseek_model="deepseek-v4-flash")

    result = generate_suggestion({"title": "Title", "source_domain": "example.com"}, [], "deepseek", settings)

    assert result.error is None
    assert result.suggestion["name"] == "Name"
    assert result.suggestion["tags"] == ["待整理"]
    assert result.suggestion["keywords"][:2] == ["keyword", "待整理"]
    assert result.suggestion["recommended_directory_path"] == "Research/Web"


def test_openai_uses_configured_base_url(monkeypatch):
    seen = {}

    def fake_post(self, url, headers=None, json=None):
        seen["url"] = url
        return httpx.Response(
            200,
            json={
                "output_text": (
                    '{"name":"Name","summary":"Summary","tags":["AI"],"keywords":["openai"],'
                    '"content_type":"webpage","recommended_directory_path":"技术/AI",'
                    '"directory_reason":"match","confidence":0.9}'
                )
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", fake_post)
    settings = Settings(
        llm_provider="openai",
        openai_api_key="sk-test",
        openai_model="gpt-5-mini",
        openai_base_url="https://proxy.example.com/v1",
    )

    result = generate_suggestion({"title": "Title", "source_domain": "example.com"}, [], "openai", settings)

    assert result.error is None
    assert seen["url"] == "https://proxy.example.com/v1/responses"
    assert result.suggestion["name"] == "Name"


def test_low_confidence_directory_is_uncategorized():
    suggestion = normalize_suggestion(
        {
            "name": "Prompt Guide",
            "summary": "Notes",
            "tags": ["AI", "工具"],
            "keywords": ["prompt"],
            "content_type": "webpage",
            "recommended_directory_path": "技术/AI",
            "directory_reason": "maybe related",
            "confidence": 0.64,
        },
        ["技术/AI"],
    )

    assert suggestion["recommended_directory_path"] == "未分类"
    assert suggestion["raw_recommended_directory_path"] == "技术/AI"
    assert suggestion["directory_policy"] == "low_confidence_uncategorized"
    assert suggestion["tags"] == ["AI"]


def test_high_confidence_existing_directory_is_accepted():
    suggestion = normalize_suggestion(
        {
            "name": "Paper",
            "summary": "Research note",
            "tags": ["论文", "研究"],
            "keywords": ["transformer"],
            "content_type": "webpage",
            "recommended_directory_path": "论文",
            "directory_reason": "source is academic",
            "confidence": 0.65,
        },
        ["论文"],
    )

    assert suggestion["recommended_directory_path"] == "论文"
    assert suggestion["directory_policy"] == "accepted_existing"


def test_new_directory_needs_higher_confidence():
    suggestion = normalize_suggestion(
        {
            "name": "Rust Ownership",
            "summary": "Rust note",
            "tags": ["Rust", "编程"],
            "keywords": ["ownership"],
            "content_type": "webpage",
            "recommended_directory_path": "技术/Rust",
            "directory_reason": "new topic",
            "confidence": 0.79,
        },
        ["技术/AI"],
    )

    assert suggestion["recommended_directory_path"] == "未分类"
    assert suggestion["raw_recommended_directory_path"] == "技术/Rust"
    assert suggestion["directory_policy"] == "new_directory_needs_review"


def test_high_confidence_specific_new_directory_is_accepted():
    suggestion = normalize_suggestion(
        {
            "name": "Rust Ownership",
            "summary": "Rust note",
            "tags": ["Rust", "编程"],
            "keywords": ["ownership"],
            "content_type": "webpage",
            "recommended_directory_path": "技术/Rust",
            "directory_reason": "clear programming topic",
            "confidence": 0.9,
        },
        ["技术/AI"],
    )

    assert suggestion["recommended_directory_path"] == "技术/Rust"
    assert suggestion["directory_policy"] == "accepted_new"


def test_generic_new_directory_and_tags_are_filtered():
    suggestion = normalize_suggestion(
        {
            "name": "Saved Page",
            "summary": "A page",
            "tags": ["工具", "资料", "AI", "AI", "Prompt", "网页", "教程"],
            "keywords": ["prompt", "AI"],
            "content_type": "webpage",
            "recommended_directory_path": "资料/网页",
            "directory_reason": "too broad",
            "confidence": 0.95,
        },
        [],
    )

    assert suggestion["recommended_directory_path"] == "未分类"
    assert suggestion["directory_policy"] == "generic_directory_uncategorized"
    assert suggestion["tags"] == ["AI", "Prompt", "教程"]
    assert len(suggestion["tags"]) <= 4
