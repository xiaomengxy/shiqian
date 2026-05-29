import httpx

from app.services.parser import fetch_and_extract


def test_fetch_and_extract_html(monkeypatch):
    def fake_get(self, url, headers=None):
        html = """
        <html>
          <head>
            <meta property="og:title" content="测试标题">
            <meta name="description" content="测试简介">
          </head>
          <body><article><p>这是一段足够长的正文内容，用来测试网页正文抽取是否工作。</p></article></body>
        </html>
        """
        return httpx.Response(
            200,
            text=html,
            headers={"content-type": "text/html; charset=utf-8"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.Client, "get", fake_get)

    extracted = fetch_and_extract("https://example.com/post?utm_source=x")

    assert extracted.title == "测试标题"
    assert extracted.description == "测试简介"
    assert extracted.canonical_url == "https://example.com/post"
    assert "正文内容" in extracted.content

