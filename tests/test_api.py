from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import app
from app.migrations import ensure_runtime_schema
from app.models import Base, Directory, Note


def _client_with_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    Base.metadata.create_all(engine)
    ensure_runtime_schema(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    def override_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    client.TestingSession = TestingSession
    return client


def test_home_renders_empty_notes_workspace():
    try:
        client = _client_with_db()
        page = client.get("/")
        assert page.status_code == 200
        assert "Markdown Notes" in page.text
        assert 'data-notes-app' in page.text
        assert 'data-note-form' in page.text
        assert 'id="notes-data"' in page.text
        assert 'id="tree-data"' in page.text
        assert 'data-feature-menu' in page.text
        assert "新目录" in page.text
        assert "学习" in page.text
        assert "功能" in page.text
        assert "回收站" not in page.text
        assert "设置" not in page.text
        assert "从一条笔记开始" in page.text
    finally:
        app.dependency_overrides.clear()


def test_home_serializes_existing_notes_for_frontend():
    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(Note(title="Existing", body_md="# Hello"))
            session.commit()

        page = client.get("/")
        assert page.status_code == 200
        assert "Existing" in page.text
        assert "# Hello" in page.text
    finally:
        app.dependency_overrides.clear()


def test_notes_api_creates_empty_and_url_notes_then_lists_by_updated_desc():
    try:
        client = _client_with_db()
        empty = client.post("/api/notes", json={})
        assert empty.status_code == 201
        assert empty.json()["note"]["title"] == "Untitled"
        assert empty.json()["note"]["source_type"] == "manual"

        url_note = client.post(
            "/api/notes",
            json={
                "title": "Bilibili later",
                "body_md": "# 视频笔记\n\n先保存链接。",
                "folder_path": "视频/Bilibili",
                "source_url": "https://www.bilibili.com/video/BV123",
                "source_type": "url",
            },
        )
        assert url_note.status_code == 201
        assert url_note.json()["note"]["source_type"] == "url"
        assert url_note.json()["note"]["folder_path"] == "视频/Bilibili"

        notes = client.get("/api/notes").json()["notes"]
        assert [note["title"] for note in notes] == ["Bilibili later", "Untitled"]
        payload = client.get("/api/notes").json()
        folders = payload["folders"]
        assert any(folder["path"] == "视频" and folder["count"] == 1 and folder["direct_count"] == 0 for folder in folders)
        assert any(folder["path"] == "视频/Bilibili" and folder["count"] == 1 and folder["direct_count"] == 1 for folder in folders)
        assert payload["tree"]["directories"][0]["path"] == "视频"
        assert payload["tree"]["directories"][0]["children"][0]["notes"][0]["title"] == "Bilibili later"
    finally:
        app.dependency_overrides.clear()


def test_notes_api_searches_title_body_and_source_url():
    try:
        client = _client_with_db()
        client.post(
            "/api/notes",
            json={
                "title": "Python",
                "body_md": "async notes",
                "source_url": "https://example.com/python",
                "source_type": "url",
            },
        )
        client.post("/api/notes", json={"title": "Cooking", "body_md": "salt and butter"})

        assert [item["title"] for item in client.get("/api/notes?query=async").json()["notes"]] == ["Python"]
        assert [item["title"] for item in client.get("/api/notes?query=example.com").json()["notes"]] == ["Python"]
        assert [item["title"] for item in client.get("/api/notes?query=Cook").json()["notes"]] == ["Cooking"]
    finally:
        app.dependency_overrides.clear()


def test_notes_api_filters_by_folder_and_descendants():
    try:
        client = _client_with_db()
        client.post("/api/notes", json={"title": "Root", "folder_path": "技术"})
        client.post("/api/notes", json={"title": "Child", "folder_path": "技术/AI"})
        client.post("/api/notes", json={"title": "Other", "folder_path": "生活"})

        filtered = client.get("/api/notes?folder=技术").json()["notes"]
        assert {note["title"] for note in filtered} == {"Root", "Child"}
        nested = client.get("/api/notes?folder=技术/AI").json()["notes"]
        assert [note["title"] for note in nested] == ["Child"]
    finally:
        app.dependency_overrides.clear()


def test_directory_api_creates_renames_moves_and_deletes_without_deleting_notes():
    try:
        client = _client_with_db()
        parent = client.post("/api/directories", json={"name": "技术"})
        assert parent.status_code == 201
        parent_id = parent.json()["directory"]["id"]

        child = client.post("/api/directories", json={"name": "AI", "parent_id": parent_id})
        assert child.status_code == 201
        child_id = child.json()["directory"]["id"]
        created = client.post("/api/notes", json={"title": "Prompt", "directory_id": child_id}).json()["note"]
        assert created["folder_path"] == "技术/AI"

        renamed = client.put(f"/api/directories/{child_id}", json={"name": "机器学习"})
        assert renamed.status_code == 200
        assert renamed.json()["directory"]["path"] == "技术/机器学习"
        assert client.get("/api/notes").json()["notes"][0]["folder_path"] == "技术/机器学习"

        research = client.post("/api/directories", json={"name": "研究"}).json()["directory"]
        moved = client.post(f"/api/directories/{child_id}/move", json={"parent_id": research["id"]})
        assert moved.status_code == 200
        assert moved.json()["directory"]["path"] == "研究/机器学习"

        deleted = client.delete(f"/api/directories/{research['id']}")
        assert deleted.status_code == 200
        note = client.get("/api/notes").json()["notes"][0]
        assert note["title"] == "Prompt"
        assert note["directory_id"] is None
        assert note["folder_path"] == ""
    finally:
        app.dependency_overrides.clear()


def test_note_move_api_moves_note_between_directory_and_uncategorized():
    try:
        client = _client_with_db()
        directory = client.post("/api/directories", json={"name": "视频"}).json()["directory"]
        note = client.post("/api/notes", json={"title": "BV note"}).json()["note"]

        moved = client.post(f"/api/notes/{note['id']}/move", json={"directory_id": directory["id"]})
        assert moved.status_code == 200
        assert moved.json()["note"]["folder_path"] == "视频"
        assert moved.json()["tree"]["directories"][0]["notes"][0]["title"] == "BV note"

        root = client.post(f"/api/notes/{note['id']}/move", json={"directory_id": None})
        assert root.status_code == 200
        assert root.json()["note"]["directory_id"] is None
        assert root.json()["tree"]["root_notes"][0]["title"] == "BV note"
    finally:
        app.dependency_overrides.clear()


def test_notes_api_updates_soft_deletes_restores_and_purges():
    try:
        client = _client_with_db()
        created = client.post("/api/notes", json={"title": "Draft", "body_md": "old"}).json()["note"]
        note_id = created["id"]

        updated = client.put(
            f"/api/notes/{note_id}",
            json={
                "title": "Final",
                "body_md": "new body",
                "folder_path": "Archive",
                "source_url": "https://example.com/final",
                "source_type": "url",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["note"]["title"] == "Final"
        assert updated.json()["note"]["body_md"] == "new body"
        assert updated.json()["note"]["folder_path"] == "Archive"

        deleted = client.delete(f"/api/notes/{note_id}")
        assert deleted.status_code == 200
        assert deleted.json()["note"]["deleted_at"] is not None
        assert client.get("/api/notes").json()["notes"] == []
        assert client.get(f"/api/notes/{note_id}").status_code == 404

        trash = client.get("/api/notes?status=trash").json()["notes"]
        assert [note["title"] for note in trash] == ["Final"]

        restored = client.post(f"/api/notes/{note_id}/restore")
        assert restored.status_code == 200
        assert restored.json()["note"]["deleted_at"] is None
        assert client.get("/api/notes").json()["notes"][0]["title"] == "Final"

        client.delete(f"/api/notes/{note_id}")
        purged = client.delete(f"/api/notes/{note_id}/purge")
        assert purged.status_code == 200
        assert purged.json() == {"deleted": True, "id": note_id}
        assert client.get("/api/notes?status=trash").json()["notes"] == []
    finally:
        app.dependency_overrides.clear()


def test_trash_page_renders_deleted_notes_and_actions():
    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            note = Note(title="Deleted", body_md="gone", deleted_at=__import__("datetime").datetime.utcnow())
            session.add(note)
            session.commit()

        page = client.get("/trash")
        assert page.status_code == 200
        assert "回收站" in page.text
        assert "Deleted" in page.text
        assert "显示删除按钮" in page.text
        assert 'data-trash-delete-toggle' in page.text
        assert 'data-restore-note=' in page.text
        assert 'data-purge-note=' in page.text
    finally:
        app.dependency_overrides.clear()


def test_learning_page_renders_bilibili_import_placeholder():
    try:
        client = _client_with_db()
        page = client.get("/learning")
        assert page.status_code == 200
        assert "收藏夹到 Markdown 笔记" in page.text
        assert "扫码登录" in page.text
        assert 'data-active-nav="learning"' in page.text
    finally:
        app.dependency_overrides.clear()


def test_bookmarks_redirects_to_notes_home():
    try:
        client = _client_with_db()
        for path in ["/bookmarks", "/jobs", "/directories"]:
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 307
            assert response.headers["location"] == "/"
    finally:
        app.dependency_overrides.clear()


def test_settings_page_keeps_model_configuration_form():
    try:
        client = _client_with_db()
        page = client.get("/settings")
        assert page.status_code == 200
        assert "默认 Provider" in page.text
        assert "DeepSeek" in page.text
        assert "OpenAI" in page.text
    finally:
        app.dependency_overrides.clear()
