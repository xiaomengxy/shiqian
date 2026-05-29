from app.config import Settings
from app.services.items import content_fingerprint, group_mixed_input, normalize_keywords, parse_mixed_input


def test_parse_mixed_input_splits_on_strong_blank_separators():
    text = """https://example.com/a


prompt engineering


This is a longer note that should be treated as a text item instead of a short term."""

    items = parse_mixed_input(text)

    assert [item.source_type for item in items] == ["url", "term", "text"]
    assert items[1].raw_input == "prompt engineering"


def test_parse_mixed_input_merges_title_url_and_access_code():
    text = """资料标题
https://example.com/file
提取码：abcd"""

    items = parse_mixed_input(text)

    assert len(items) == 1
    assert items[0].source_type == "url"
    assert items[0].primary_url == "https://example.com/file"
    assert "资料标题" in items[0].raw_input
    assert "提取码" in items[0].raw_input
    assert items[0].group_confidence >= 0.95
    assert "提取码" in items[0].group_reason


def test_parse_mixed_input_keeps_pure_url_list_separate():
    items = parse_mixed_input("https://example.com/a\nhttps://example.com/b")

    assert [item.primary_url for item in items] == ["https://example.com/a", "https://example.com/b"]
    assert all(item.group_confidence > 0.9 for item in items)


def test_parse_mixed_input_keeps_plain_wrapped_paragraph_together():
    text = "This paragraph wraps over\nmultiple visual lines\nwithout being separate items."

    items = parse_mixed_input(text)

    assert len(items) == 1
    assert items[0].source_type == "text"
    assert "multiple visual lines" in items[0].raw_input


def test_parse_mixed_input_marks_multi_link_notes_as_ambiguous():
    text = """Alpha resource
https://example.com/a
first note
Beta resource
https://example.com/b
second note"""

    items = parse_mixed_input(text)

    assert [item.source_type for item in items] == ["url", "url"]
    assert all(item.group_confidence < 0.75 for item in items)


def test_group_mixed_input_without_api_key_uses_local_candidates():
    settings = Settings(llm_provider="deepseek", deepseek_api_key="")
    text = "Alpha\nhttps://example.com/a\nBeta\nhttps://example.com/b"

    items = group_mixed_input(text, None, settings)

    assert len(items) == 2
    assert all(item.grouping_source == "rules" for item in items)


def test_group_mixed_input_uses_ai_for_ambiguous_groups(monkeypatch):
    def fake_grouping(payload, model, api_key):
        return (
            '{"groups": ['
            '{"source_type": "url", "primary_url": "https://example.com/a", "raw_input": "Alpha\\nhttps://example.com/a", "reason": "AI 判断为同一资料", "confidence": 0.9},'
            '{"source_type": "url", "primary_url": "https://example.com/b", "raw_input": "Beta\\nhttps://example.com/b", "reason": "AI 判断为同一资料", "confidence": 0.88}'
            "]} "
        )

    monkeypatch.setattr("app.services.items._call_deepseek_grouping", fake_grouping)
    settings = Settings(llm_provider="deepseek", deepseek_api_key="sk-test", deepseek_model="mock")

    items = group_mixed_input("Alpha\nhttps://example.com/a\nBeta\nhttps://example.com/b", None, settings)

    assert [item.primary_url for item in items] == ["https://example.com/a", "https://example.com/b"]
    assert all(item.grouping_source == "ai" for item in items)
    assert "AI 判断" in items[0].group_reason


def test_group_mixed_input_falls_back_when_ai_confidence_is_low(monkeypatch):
    def fake_grouping(payload, model, api_key):
        return (
            '{"groups": ['
            '{"source_type": "url", "primary_url": "https://example.com/a", "raw_input": "Alpha\\nhttps://example.com/a", "reason": "不确定", "confidence": 0.4}'
            "]}"
        )

    monkeypatch.setattr("app.services.items._call_deepseek_grouping", fake_grouping)
    settings = Settings(llm_provider="deepseek", deepseek_api_key="sk-test", deepseek_model="mock")

    items = group_mixed_input("Alpha\nhttps://example.com/a\nBeta\nhttps://example.com/b", None, settings)

    assert len(items) == 2
    assert all(item.grouping_source == "rules" for item in items)


def test_parse_mixed_input_deduplicates_same_text_block():
    items = parse_mixed_input("prompt engineering\nprompt engineering")

    assert len(items) == 1
    assert items[0].source_type == "term"


def test_content_fingerprint_normalizes_whitespace_and_case():
    assert content_fingerprint("term", "  OpenAI   Prompt ") == content_fingerprint("term", "openai prompt")


def test_normalize_keywords_deduplicates_and_limits():
    keywords = normalize_keywords(
        "AI, tools, AI\nprompt, automation, veryveryveryveryveryveryveryveryverylong"
    )

    assert keywords[:4] == ["AI", "tools", "prompt", "automation"]
    assert len(keywords) == 5
    assert len(keywords[-1]) == 32
