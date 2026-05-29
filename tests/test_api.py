from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.models import Base


def _client_with_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    return TestClient(app)


def test_confirm_bookmark_api_creates_directory_and_bookmark(monkeypatch):
    def fake_extract(url):
        class Extracted:
            def as_dict(self):
                return {
                    "url": url,
                    "canonical_url": "https://example.com/a",
                    "title": "Example",
                    "description": "Desc",
                    "content": "Content",
                    "content_type": "webpage",
                    "source_type": "url",
                    "source_domain": "example.com",
                    "raw_input": url,
                    "fetch_status": "ok",
                }

        return Extracted()

    def fake_generate(extracted, directories, provider, settings):
        class Result:
            suggestion = {
                "name": "Example",
                "summary": "Summary",
                "tags": ["resource"],
                "keywords": ["keyword"],
                "content_type": "webpage",
                "recommended_directory_path": "Resources/Web",
                "directory_reason": "test",
                "confidence": 1,
            }
            provider = "rules"
            model = "local"
            error = None

        return Result()

    monkeypatch.setattr("app.main.fetch_and_extract", fake_extract)
    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/links/parse", json={"urls": ["https://example.com/a"], "provider": "deepseek"})
        job_id = parsed.json()["jobs"][0]["id"]

        response = client.post(
            "/api/bookmarks/confirm",
            json={
                "job_id": job_id,
                "directory_path": "Resources/Web",
                "tags": ["resource"],
                "keywords": ["keyword"],
                "summary": "Summary",
                "name": "Example",
            },
        )

        assert response.status_code == 200
        bookmarks = client.get("/api/bookmarks").json()["bookmarks"]
        assert bookmarks[0]["directory"] == "Resources/Web"
        assert bookmarks[0]["tags"] == ["resource"]
        assert bookmarks[0]["keywords"] == ["keyword"]
    finally:
        app.dependency_overrides.clear()


def test_parse_items_api_handles_mixed_input_and_keyword_search(monkeypatch):
    def fake_extract(url):
        class Extracted:
            def as_dict(self):
                return {
                    "url": url,
                    "canonical_url": "https://example.com/a",
                    "title": "Example",
                    "description": "Desc",
                    "content": "Content",
                    "content_type": "webpage",
                    "source_type": "url",
                    "source_domain": "example.com",
                    "raw_input": url,
                    "fetch_status": "ok",
                }

        return Extracted()

    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": extracted["title"],
                    "summary": "Summary",
                    "tags": ["resource"],
                    "keywords": ["mixed-keyword", extracted["source_type"]],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "Resources/Mixed",
                    "directory_reason": "test",
                    "confidence": 1,
                }

        return Result()

    monkeypatch.setattr("app.main.fetch_and_extract", fake_extract)
    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post(
            "/api/items/parse",
            json={
                "input": (
                    "https://example.com/a\n\n"
                    "prompt engineering\n\n"
                    "This longer paragraph should be stored as a text item for later lookup."
                )
            },
        )
        jobs = parsed.json()["jobs"]
        assert [job["source_type"] for job in jobs] == ["url", "term", "text"]

        for job in jobs:
            detail = client.get(f"/api/jobs/{job['id']}").json()
            client.post(
                "/api/bookmarks/confirm",
                json={
                    "job_id": job["id"],
                    "directory_path": "Resources/Mixed",
                    "tags": ["resource"],
                    "keywords": detail["suggestion"]["keywords"],
                    "summary": detail["suggestion"]["summary"],
                    "name": detail["suggestion"]["name"],
                },
            )

        found = client.get("/api/bookmarks?query=mixed-keyword").json()["bookmarks"]
        assert len(found) == 3
        assert any(item["url"] is None and item["source_type"] == "term" for item in found)
        assert any(item["url"] is None and item["source_type"] == "text" for item in found)
    finally:
        app.dependency_overrides.clear()
