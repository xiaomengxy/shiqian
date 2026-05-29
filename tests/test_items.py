from app.services.items import content_fingerprint, normalize_keywords, parse_mixed_input


def test_parse_mixed_input_splits_urls_terms_and_paragraphs():
    text = """https://example.com/a

prompt engineering

This is a longer note that should be treated as a text item instead of a short term."""

    items = parse_mixed_input(text)

    assert [item.source_type for item in items] == ["url", "term", "text"]
    assert items[1].raw_input == "prompt engineering"


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

