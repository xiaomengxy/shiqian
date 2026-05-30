from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.migrations import ensure_runtime_schema
from app.models import Base, Bookmark, Directory, Tag


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
    client = TestClient(app)
    client.test_engine = engine
    client.TestingSession = TestingSession
    return client


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
        assert saved_job["created_at"]
        assert saved_job["parsed_at"]

        bookmarks = client.get("/api/bookmarks").json()["bookmarks"]
        assert bookmarks[0]["directory"] == "Resources/Web"
        assert bookmarks[0]["tags"] == ["resource"]
        assert bookmarks[0]["keywords"] == ["keyword"]
        assert bookmarks[0]["opened_count"] == 0
        assert bookmarks[0]["parsed_at"]
        assert bookmarks[0]["created_at"]
        assert bookmarks[0]["updated_at"]
        assert bookmarks[0]["parsed_at_display"]
        assert bookmarks[0]["created_at_display"]
        assert bookmarks[0]["updated_at_display"]

        opened = client.get(f"/bookmarks/{bookmarks[0]['id']}/open", follow_redirects=False)
        assert opened.status_code == 302
        assert opened.headers["location"] == "https://example.com/a"

        bookmarks = client.get("/api/bookmarks?sort=open_count").json()["bookmarks"]
        assert bookmarks[0]["opened_count"] == 1
        assert bookmarks[0]["last_opened_at"] is not None
    finally:
        app.dependency_overrides.clear()


def test_uncategorized_confirm_and_edit_use_virtual_directory(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": "Loose note",
                    "summary": "Summary",
                    "tags": ["待整理"],
                    "keywords": ["loose"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "未分类",
                    "directory_reason": "low confidence",
                    "confidence": 0.4,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "loose term"})
        job_id = parsed.json()["jobs"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()

        created = client.post(
            "/api/bookmarks/confirm",
            json={
                "job_id": job_id,
                "directory_path": "未分类",
                "tags": detail["suggestion"]["tags"],
                "keywords": detail["suggestion"]["keywords"],
                "summary": detail["suggestion"]["summary"],
                "name": detail["suggestion"]["name"],
            },
        )
        assert created.status_code == 200
        bookmark_id = created.json()["id"]

        bookmarks = client.get("/api/bookmarks").json()["bookmarks"]
        assert bookmarks[0]["directory"] is None
        assert len(client.get("/api/bookmarks?directory=__none__").json()["bookmarks"]) == 1

        updated = client.post(
            f"/api/bookmarks/{bookmark_id}",
            json={
                "name": "Loose note edited",
                "summary": "Edited summary",
                "directory_path": "未分类/AI",
                "tags": ["待整理"],
                "keywords": ["loose"],
                "raw_input": "loose term",
            },
        )
        assert updated.status_code == 200
        assert client.get("/api/bookmarks").json()["bookmarks"][0]["directory"] is None

        with client.TestingSession() as session:
            assert session.scalar(select(Directory).where(Directory.path == "未分类")) is None
            assert session.scalar(select(Directory).where(Directory.path.like("未分类/%"))) is None
    finally:
        app.dependency_overrides.clear()


def test_runtime_schema_merges_legacy_uncategorized_directory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    with TestingSession() as session:
        directory = Directory(name="未分类", path="未分类", depth=0)
        bookmark = Bookmark(
            source_type="term",
            raw_input="legacy term",
            keywords=[],
            title="Legacy term",
            summary="Summary",
            content_type="term",
            source_domain="",
            directory=directory,
            status="saved",
        )
        session.add(bookmark)
        session.commit()

    ensure_runtime_schema(engine)

    with TestingSession() as session:
        bookmark = session.scalar(select(Bookmark).where(Bookmark.title == "Legacy term"))
        assert bookmark.directory_id is None
        assert session.scalar(select(Directory).where(Directory.path == "未分类")) is None


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
                    "https://example.com/a\n\n\n"
                    "prompt engineering\n\n\n"
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


def test_parse_items_api_groups_url_with_nearby_text_and_fetches_primary_url(monkeypatch):
    captured = {}

    def fake_extract(url):
        captured["url"] = url

        class Extracted:
            def as_dict(self):
                return {
                    "url": url,
                    "canonical_url": url,
                    "title": "Grouped Link",
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
        captured["raw_input"] = extracted["raw_input"]

        class Result:
            suggestion = {
                "name": "Grouped Link",
                "summary": "Summary",
                "tags": ["resource"],
                "keywords": ["grouped"],
                "content_type": "webpage",
                "recommended_directory_path": "未分类",
                "directory_reason": "test",
                "confidence": 0.4,
            }
            provider = "rules"
            model = "local"
            error = None

        return Result()

    monkeypatch.setattr("app.main.fetch_and_extract", fake_extract)
    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post(
            "/api/items/parse",
            json={"input": "资料标题\nhttps://example.com/file\n提取码：abcd"},
        )

        assert parsed.status_code == 200
        jobs = parsed.json()["jobs"]
        assert len(jobs) == 1
        assert jobs[0]["source_type"] == "url"
        assert jobs[0]["group_confidence"] >= 0.95
        assert "提取码" in jobs[0]["group_reason"]
        assert captured["url"] == "https://example.com/file"
        assert "资料标题" in captured["raw_input"]
        assert "提取码" in captured["raw_input"]

        detail = client.get(f"/api/jobs/{jobs[0]['id']}").json()
        assert detail["input_url"] == "https://example.com/file"
        assert detail["raw_input"].startswith("资料标题")
        assert detail["extracted"]["raw_input"] == detail["raw_input"]
    finally:
        app.dependency_overrides.clear()


def test_links_parse_api_keeps_url_batch_pure(monkeypatch):
    def fake_extract(url):
        class Extracted:
            def as_dict(self):
                return {
                    "url": url,
                    "canonical_url": url,
                    "title": "Link",
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
                "name": "Link",
                "summary": "Summary",
                "tags": ["resource"],
                "keywords": ["link"],
                "content_type": "webpage",
                "recommended_directory_path": "未分类",
                "directory_reason": "test",
                "confidence": 0.4,
            }
            provider = "rules"
            model = "local"
            error = None

        return Result()

    monkeypatch.setattr("app.main.fetch_and_extract", fake_extract)
    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post(
            "/api/links/parse",
            json={"urls": ["https://example.com/a", "https://example.com/b"]},
        )

        assert parsed.status_code == 200
        jobs = parsed.json()["jobs"]
        assert [job["url"] for job in jobs] == ["https://example.com/a", "https://example.com/b"]
        assert all(job["grouping_source"] == "rules" for job in jobs)
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


def test_review_page_shows_directory_alternatives_and_tag_options(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": "AI note",
                    "summary": "Summary",
                    "tags": ["AI"],
                    "keywords": ["prompt"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "技术/AI",
                    "directory_reason": "test",
                    "confidence": 1,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(Directory(name="技术", path="技术", depth=0))
            session.add(Directory(name="AI", path="技术/AI", depth=1))
            session.add(Tag(name="AI", slug="ai"))
            session.commit()

        parsed = client.post("/api/items/parse", json={"input": "ai prompt note"})
        job_id = parsed.json()["jobs"][0]["id"]
        page = client.get(f"/review/{job_id}")

        assert page.status_code == 200
        assert "目录备选" in page.text
        assert "技术/AI · 当前推荐" in page.text
        assert "常用标签" in page.text
        assert 'data-summary-preset' in page.text
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
        parsed = client.post("/api/items/parse", json={"input": "job term\n\n\nsecond job"})
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


def test_failed_job_can_be_retried(monkeypatch):
    calls = {"count": 0}

    def flaky_generate(extracted, directories, provider, settings):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary failure")

        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": "Retry term",
                    "summary": "Summary",
                    "tags": ["term"],
                    "keywords": ["retry"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "未分类",
                    "directory_reason": "test",
                    "confidence": 0.4,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", flaky_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "retry term"})
        job_id = parsed.json()["jobs"][0]["id"]
        assert client.get(f"/api/jobs/{job_id}").json()["status"] == "failed"

        retried = client.post(f"/api/jobs/{job_id}/retry")
        assert retried.status_code == 200
        body = retried.json()
        assert body["status"] == "completed"
        assert body["stage"] == "等待确认"
        assert body["error"] is None
    finally:
        app.dependency_overrides.clear()


def test_jobs_page_hides_saved_by_default(monkeypatch):
    def fake_generate(extracted, directories, provider, settings):
        class Result:
            provider = "rules"
            model = "local"
            error = None

            def __init__(self):
                self.suggestion = {
                    "name": "Hidden saved term",
                    "summary": "Summary",
                    "tags": ["term"],
                    "keywords": ["hidden"],
                    "content_type": extracted["content_type"],
                    "recommended_directory_path": "Inbox",
                    "directory_reason": "test",
                    "confidence": 1,
                }

        return Result()

    monkeypatch.setattr("app.main.generate_suggestion", fake_generate)

    try:
        client = _client_with_db()
        parsed = client.post("/api/items/parse", json={"input": "hidden saved term"})
        job_id = parsed.json()["jobs"][0]["id"]
        detail = client.get(f"/api/jobs/{job_id}").json()
        client.post(
            "/api/bookmarks/confirm",
            json={
                "job_id": job_id,
                "directory_path": detail["suggestion"]["recommended_directory_path"],
                "tags": detail["suggestion"]["tags"],
                "keywords": detail["suggestion"]["keywords"],
                "summary": detail["suggestion"]["summary"],
                "name": detail["suggestion"]["name"],
            },
        )

        default_page = client.get("/jobs")
        assert "hidden saved term" not in default_page.text
        assert "已隐藏 1 个已保存任务" in default_page.text

        show_saved_page = client.get("/jobs?show_saved=1")
        assert "hidden saved term" in show_saved_page.text
    finally:
        app.dependency_overrides.clear()


def test_directory_json_api_manages_tree_from_bookmarks():
    try:
        client = _client_with_db()
        created = client.post("/api/directories", json={"path": "资料/AI"})
        assert created.status_code == 200
        ai_id = created.json()["id"]
        assert created.json()["path"] == "资料/AI"

        renamed = client.post(f"/api/directories/{ai_id}/rename", json={"name": "Prompt"})
        assert renamed.status_code == 200
        assert renamed.json()["path"] == "资料/Prompt"

        child = client.post(f"/api/directories/{ai_id}/children", json={"name": "案例"})
        assert child.status_code == 200
        assert child.json()["path"] == "资料/Prompt/案例"

        target = client.post("/api/directories", json={"path": "技术"})
        target_id = target.json()["id"]
        moved = client.post(f"/api/directories/{ai_id}/move", json={"parent_id": target_id})
        assert moved.status_code == 200
        assert moved.json()["path"] == "技术/Prompt"

        moved_root = client.post(f"/api/directories/{ai_id}/move", json={"parent_id": None})
        assert moved_root.status_code == 200
        assert moved_root.json()["path"] == "Prompt"

        bad = client.post("/api/directories", json={"path": "未分类"})
        assert bad.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_directory_delete_moves_bookmarks_to_uncategorized():
    try:
        client = _client_with_db()
        parent = client.post("/api/directories", json={"path": "资料/AI"}).json()
        child = client.post(f"/api/directories/{parent['id']}/children", json={"name": "提示词"}).json()

        with client.TestingSession() as session:
            session.add(
                Bookmark(
                    url=None,
                    canonical_url=None,
                    content_hash="delete-dir-hash",
                    source_type="text",
                    raw_input="prompt note",
                    keywords=["prompt"],
                    title="Prompt note",
                    summary="Summary",
                    content_type="text",
                    source_domain="",
                    directory_id=child["id"],
                )
            )
            session.commit()

        preview = client.get(f"/api/directories/{parent['id']}/delete-preview")
        assert preview.status_code == 200
        assert preview.json()["directory_count"] == 2
        assert preview.json()["children_count"] == 1
        assert preview.json()["bookmark_count"] == 1

        deleted = client.delete(f"/api/directories/{parent['id']}")
        assert deleted.status_code == 200
        assert deleted.json()["redirect_directory"] == "__none__"

        with client.TestingSession() as session:
            assert session.scalar(select(Directory).where(Directory.path.like("资料/AI%"))) is None
            bookmark = session.scalar(select(Bookmark).where(Bookmark.content_hash == "delete-dir-hash"))
            assert bookmark.directory_id is None
    finally:
        app.dependency_overrides.clear()


def test_directory_bulk_delete_dedupes_parent_child_selection():
    try:
        client = _client_with_db()
        root = client.post("/api/directories", json={"path": "资料/AI"}).json()
        child = client.post(f"/api/directories/{root['id']}/children", json={"name": "案例"}).json()
        other = client.post("/api/directories", json={"path": "数据库"}).json()

        with client.TestingSession() as session:
            session.add_all(
                [
                    Bookmark(
                        url=None,
                        canonical_url=None,
                        content_hash="bulk-dir-a",
                        source_type="text",
                        raw_input="ai case",
                        keywords=[],
                        title="AI case",
                        summary="Summary",
                        content_type="text",
                        source_domain="",
                        directory_id=child["id"],
                    ),
                    Bookmark(
                        url=None,
                        canonical_url=None,
                        content_hash="bulk-dir-b",
                        source_type="text",
                        raw_input="database note",
                        keywords=[],
                        title="Database note",
                        summary="Summary",
                        content_type="text",
                        source_domain="",
                        directory_id=other["id"],
                    ),
                ]
            )
            session.commit()

        preview = client.post(
            "/api/directories/bulk-delete",
            json={"ids": [root["id"], child["id"], other["id"]], "preview": True},
        )
        assert preview.status_code == 200
        assert preview.json()["root_count"] == 2
        assert preview.json()["directory_count"] == 3
        assert preview.json()["bookmark_count"] == 2

        deleted = client.post("/api/directories/bulk-delete", json={"ids": [root["id"], child["id"], other["id"]]})
        assert deleted.status_code == 200

        with client.TestingSession() as session:
            assert session.scalar(select(Directory).where(Directory.path.like("资料/AI%"))) is None
            assert session.scalar(select(Directory).where(Directory.path.like("数据库%"))) is None
            assert all(
                bookmark.directory_id is None
                for bookmark in session.scalars(select(Bookmark).where(Bookmark.content_hash.in_(["bulk-dir-a", "bulk-dir-b"]))).all()
            )
    finally:
        app.dependency_overrides.clear()


def test_bookmark_move_api_updates_directory():
    try:
        client = _client_with_db()
        target = client.post("/api/directories", json={"path": "资料/AI"}).json()
        with client.TestingSession() as session:
            session.add(
                Bookmark(
                    url=None,
                    canonical_url=None,
                    content_hash="move-bookmark-hash",
                    source_type="text",
                    raw_input="move note",
                    keywords=[],
                    title="Move note",
                    summary="Summary",
                    content_type="text",
                    source_domain="",
                    directory_id=None,
                )
            )
            session.commit()
            bookmark_id = session.scalar(select(Bookmark.id).where(Bookmark.content_hash == "move-bookmark-hash"))

        moved = client.post(f"/api/bookmarks/{bookmark_id}/move", json={"directory_id": target["id"]})
        assert moved.status_code == 200
        assert moved.json()["directory"] == "资料/AI"

        moved_uncategorized = client.post(f"/api/bookmarks/{bookmark_id}/move", json={"directory_id": None})
        assert moved_uncategorized.status_code == 200
        assert moved_uncategorized.json()["directory_id"] is None

        with client.TestingSession() as session:
            bookmark = session.get(Bookmark, bookmark_id)
            assert bookmark.directory_id is None
    finally:
        app.dependency_overrides.clear()


def test_bookmarks_sidebar_renders_directory_management_controls():
    try:
        client = _client_with_db()
        client.post("/api/directories", json={"path": "资料/AI"})

        page = client.get("/bookmarks/partials?directory=资料")
        assert page.status_code == 200
        assert 'data-directory-form' in page.text
        assert 'data-directory-draggable' in page.text
        assert 'data-directory-drop' in page.text
        assert 'data-directory-delete' in page.text
        assert 'data-directory-manage-toggle' in page.text
        assert 'data-directory-manage-done' in page.text
        assert 'data-root-directory-toggle' in page.text
        assert 'data-bookmark-drop' in page.text
        assert '<svg aria-hidden="true" viewBox="0 0 24 24"' in page.text
        assert 'inline-create-form' not in page.text
        assert 'data-directory-bulk-delete' in page.text
        assert "拖到这里成为根目录" in page.text
        assert "完整目录管理" not in page.text
        assert "Selected" not in page.text
        assert "新增子目录" in page.text
        assert ">改<" not in page.text
    finally:
        app.dependency_overrides.clear()


def test_directories_page_redirects_to_bookmarks():
    try:
        client = _client_with_db()
        response = client.get("/directories", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/bookmarks"

        partial = client.get("/directories/partials")
        assert partial.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_items_form_opens_grouping_preview_before_jobs():
    try:
        client = _client_with_db()
        response = client.post(
            "/items/parse",
            data={"input_text": "same topic line one\n\n\nsame topic line two"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith("/grouping/")

        page = client.get(response.headers["location"])
        assert page.status_code == 200
        assert "Grouping Preview" in page.text
        assert page.text.count('name="raw_input"') == 1
        assert "全部作为一条" in page.text
        assert "确认分组" in page.text
    finally:
        app.dependency_overrides.clear()


def test_grouping_all_in_one_and_confirm_creates_jobs(monkeypatch):
    monkeypatch.setattr("app.main._process_job_in_background", lambda job_id, provider: None)

    try:
        client = _client_with_db()
        response = client.post(
            "/items/parse",
            data={"input_text": "topic intro\n\n\nstill same topic"},
            follow_redirects=False,
        )
        session_url = response.headers["location"]

        merged = client.post(f"{session_url}/all-in-one", follow_redirects=False)
        assert merged.status_code == 303

        confirmed = client.post(
            f"{session_url}/confirm",
            data={"source_type": "text", "raw_input": "topic intro\n\n\nstill same topic"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert confirmed.headers["location"] == "/jobs"

        detail = client.get("/api/jobs/1").json()
        assert detail["raw_input"] == "topic intro\n\n\nstill same topic"
        assert detail["grouping_source"] == "user"
    finally:
        app.dependency_overrides.clear()


def test_running_job_cannot_be_deleted(monkeypatch):
    monkeypatch.setattr("app.main._process_job_in_background", lambda job_id, provider: None)

    try:
        client = _client_with_db()
        response = client.post("/items/parse", data={"input_text": "pending term"}, follow_redirects=False)
        assert response.status_code == 303
        session_url = response.headers["location"]

        confirmed = client.post(
            f"{session_url}/confirm",
            data={"source_type": "term", "raw_input": "pending term"},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303

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
        parsed = client.post("/api/items/parse", json={"input": "ai note\n\n\ndatabase note"})
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

        trash = client.get("/trash/partials")
        assert trash.status_code == 200
        assert 'id="trash-lists"' in trash.text
    finally:
        app.dependency_overrides.clear()
