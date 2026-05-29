from app.services.urls import canonicalize_url, extract_urls


def test_extract_urls_deduplicates_and_trims_punctuation():
    text = "看这个 https://Example.com/a?utm_source=x，以及 https://Example.com/a?utm_source=x。"

    assert extract_urls(text) == ["https://Example.com/a?utm_source=x"]


def test_canonicalize_url_removes_common_tracking_params():
    url = "HTTPS://www.Example.com/path/?utm_source=x&ok=1#section"

    assert canonicalize_url(url) == "https://example.com/path?ok=1"

