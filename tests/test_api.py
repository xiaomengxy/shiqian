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
        detail = client.get(f"/api/jobs/{job_id}").json()

        assert detail["suggestion"]["raw_recommended_directory_path"] == "Resources/Web"
        assert detail["suggestion"]["directory_confidence"] == 1
        assert detail["suggestion"]["directory_policy"] == "accepted_new"

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
        saved_job = client.get(f"/api/jobs/{job_id}").json()
        assert saved_job["status"] == "saved"
        assert saved_job["stage"] == "已保存到收藏"

        bookmarks = client.get("/api/bookmarks").json()["bookmarks"]
        assert bookmarks[0]["directory"] == "Resources/Web"
        assert bookmarks[0]["tags"] == ["resource"]
        assert bookmarks[0]["keywords"] == ["keyword"]
        assert bookmarks[0]["opened_count"] == 0

        opened = client.get(f"/bookmarks/{bookmarks[0]['id']}/open", follow_redirects=False)
        assert opened.status_code == 302
        assert opened.headers["location"] == "https://example.com/a"

        bookmarks = client.get("/api/bookmarks?sort=open_count").json()["bookmarks"]
        assert bookmarks[0]["opened_count"] == 1
        assert bookmarks[0]["last_opened_at"] is not None
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


def test_frontend_settings_update_runtime_provider(monkeypatch):
    captured = {}

    def fake_generate(extracted, directories, provider, settings):
        captured["provider"] = provider
        captured["settings_provider"] = settings.llm_provider
        captured["deepseek_key"] = settings.deepseek_api_key
        captured["deepseek_model"] = settings.deepseek_model

        class Result:
            suggestion = {
                "name": "Saved term",
                "summary": "Summary",
                "tags": ["term"],
                "keywords": ["keyword"],
                "content_type": "term",
                "recommended_directory_path": "Inbox",
                "directory_reason": "test",
                "confidence": 1,
            }
            provider = "deepseek"
            model = "custom-deepseek"
            error = None

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        response = client.post(
            "/settings",
            data={
                "llm_provider": "deepseek",
                "deepseek_model": "custom-deepseek",
                "deepseek_api_key": "sk-deepseek-test",
                "openai_model": "gpt-test",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303

        parsed = client.post("/api/items/parse", json={"input": "runtime setting term"})
        assert parsed.status_code == 200
        assert captured == {
            "provider": None,
            "settings_provider": "deepseek",
            "deepseek_key": "sk-deepseek-test",
            "deepseek_model": "custom-deepseek",
        }
    finally:
        app.dependency_overrides.clear()


def test_rewrite_job_summary_api_updates_review_draft(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            suggestion = {
                "name": "Saved term",
                "summary": "Short draft",
                "tags": ["term"],
                "keywords": ["keyword"],
                "content_type": "term",
                "recommended_directory_path": "Inbox",
                "directory_reason": "test",
                "confidence": 1,
            }
            provider = "rules"
            model = "local"
            error = None

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "prompt engineering"})
        job_id = parsed.json()["jobs"][0]["id"]

        rewritten = client.post(
            f"/api/jobs/{job_id}/summary/rewrite",
            json={
                "length": "detailed",
                "style": "beginner",
                "current_summary": "Short draft",
                "provider": "rules",
            },
        )

        assert rewritten.status_code == 200
        body = rewritten.json()
        assert body["provider"] == "rules"
        assert body["error"] is None
        assert "Saved term" in body["summary"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        assert detail["suggestion"]["summary"] == body["summary"]
    finally:
        app.dependency_overrides.clear()


def test_bookmark_soft_delete_restore_purge_and_edit(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": "Original term",
                    "summary": "Original summary",
                    "tags": ["old"],
                    "keywords": ["old-keyword"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "Inbox",
                    "directory_reason": "test",
                    "confidence": 1,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "editable term"})
        job_id = parsed.json()["jobs"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        created = client.post(
            "/api/bookmarks/confirm",
            json={
                "job_id": job_id,
                "directory_path": "Inbox",
                "tags": detail["suggestion"]["tags"],
                "keywords": detail["suggestion"]["keywords"],
                "summary": detail["suggestion"]["summary"],
                "name": detail["suggestion"]["name"],
            },
        )
        bookmark_id = created.json()["id"]

        updated = client.post(
            f"/api/bookmarks/{bookmark_id}",
            json={
                "name": "Edited term",
                "summary": "Edited searchable summary",
                "directory_path": "Research/Terms",
                "tags": ["new"],
                "keywords": ["edited-keyword"],
                "raw_input": "editable term raw",
            },
        )
        assert updated.status_code == 200
        found = client.get("/api/bookmarks?query=edited-keyword").json()["bookmarks"]
        assert len(found) == 1
        assert found[0]["name"] == "Edited term"
        assert found[0]["directory"] == "Research/Terms"
        assert found[0]["tags"] == ["new"]

        deleted = client.delete(f"/api/bookmarks/{bookmark_id}")
        assert deleted.status_code == 200
        assert client.get("/api/bookmarks").json()["bookmarks"] == []

        restored = client.post(f"/api/bookmarks/{bookmark_id}/restore")
        assert restored.status_code == 200
        assert len(client.get("/api/bookmarks").json()["bookmarks"]) == 1

        client.delete(f"/api/bookmarks/{bookmark_id}/purge")
        assert client.get("/api/bookmarks").json()["bookmarks"] == []
    finally:
        app.dependency_overrides.clear()


def test_job_soft_delete_restore_and_cleanup(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            suggestion = {
                "name": "Saved term",
                "summary": "Summary",
                "tags": ["term"],
                "keywords": ["keyword"],
                "content_type": "term",
                "recommended_directory_path": "Inbox",
                "directory_reason": "test",
                "confidence": 1,
            }
            provider = "rules"
            model = "local"
            error = None

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "job term\n\nsecond job"})
        jobs = parsed.json()["jobs"]
        first_id = jobs[0]["id"]

        deleted = client.delete(f"/api/jobs/{first_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/jobs/{first_id}").status_code == 404

        restored = client.post(f"/api/jobs/{first_id}/restore")
        assert restored.status_code == 200
        assert client.get(f"/api/jobs/{first_id}").status_code == 200

        cleanup = client.post("/jobs/cleanup", follow_redirects=False)
        assert cleanup.status_code == 303
        assert client.get(f"/api/jobs/{first_id}").status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_running_job_cannot_be_deleted(monkeypatch):
    monkeypatch.setattr("app.main._process_job_in_background", lambda job_id, provider: None)

    try:
        client = _client_with_db()
        response = client.post("/items/parse", data={"input_text": "pending term"}, follow_redirects=False)
        assert response.status_code == 303

        jobs_page = client.get("/jobs")
        assert jobs_page.status_code == 200

        deleted = client.delete("/api/jobs/1")
        assert deleted.status_code == 400
        assert deleted.json()["detail"] == "Running jobs cannot be deleted"
    finally:
        app.dependency_overrides.clear()


def test_bookmark_partials_filter_directory_and_tag(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": extracted["title"],
                    "summary": "Summary",
                    "tags": ["AI"] if "ai" in extracted["raw_input"].lower() else ["Other"],
                    "keywords": ["partial-keyword"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "技术/AI" if "ai" in extracted["raw_input"].lower() else "技术/数据库",
                    "directory_reason": "test",
                    "confidence": 1,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "ai note\n\ndatabase note"})
        for job in parsed.json()["jobs"]:
            detail = client.get(f"/api/jobs/{job['id']}").json()
            client.post(
                "/api/bookmarks/confirm",
                json={
                    "job_id": job["id"],
                    "directory_path": detail["suggestion"]["recommended_directory_path"],
                    "tags": detail["suggestion"]["tags"],
                    "keywords": detail["suggestion"]["keywords"],
                    "summary": detail["suggestion"]["summary"],
                    "name": detail["suggestion"]["name"],
                },
            )

        response = client.get("/bookmarks/partials?directory=技术&tag=AI")
        assert response.status_code == 200
        assert 'id="bookmark-browser"' in response.text
        assert "ai note" in response.text
        assert "database note" not in response.text

        directories = client.get("/directories/partials")
        assert directories.status_code == 200
        assert 'id="directory-workspace"' in directories.text

        trash = client.get("/trash/partials")
        assert trash.status_code == 200
        assert 'id="trash-lists"' in trash.text
    finally:
        app.dependency_overrides.clear()
