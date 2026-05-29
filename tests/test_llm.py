import httpx

from app.config import Settings
from app.services.llm import generate_suggestion


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
    assert result.suggestion["recommended_directory_path"] == "技术/AI"
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
    assert result.suggestion["tags"] == ["web"]
    assert result.suggestion["keywords"] == ["keyword"]
    assert result.suggestion["recommended_directory_path"] == "Research/Web"

