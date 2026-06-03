import asyncio

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import get_db
import app.main as main_module
from app.main import app
from app.migrations import ensure_runtime_schema
from app.models import Base, BilibiliSession, Directory, Note
from app.services.bilibili_notes import VideoContentResult, compose_video_note, video_payload_from_media


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
        assert "Bilibili 登录" in page.text
        assert 'data-learning-app' in page.text
        assert 'data-bili-load-folders' in page.text
        assert 'data-bili-import-selected' in page.text
        assert 'data-active-nav="learning"' in page.text
    finally:
        app.dependency_overrides.clear()


def test_bilibili_session_api_returns_logged_out_by_default():
    try:
        client = _client_with_db()
        response = client.get("/api/bilibili/session")
        assert response.status_code == 200
        assert response.json() == {"logged_in": False, "user": None}
    finally:
        app.dependency_overrides.clear()


def test_bilibili_favorites_api_uses_saved_session(monkeypatch):
    async def fake_favorites(self, mid=None):
        return [{"id": 123, "title": "学习资料", "media_count": 2}]

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_user_favorites", fake_favorites)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.commit()

        response = client.get("/api/bilibili/favorites")
        assert response.status_code == 200
        assert response.json()["folders"][0]["title"] == "学习资料"
    finally:
        app.dependency_overrides.clear()


def test_bilibili_videos_api_reads_all_pages(monkeypatch):
    calls = []

    async def fake_content(self, media_id, pn=1, ps=20):
        calls.append(pn)
        if pn == 1:
            return {
                "info": {"title": "学习资料"},
                "medias": [
                    {"bvid": "BV1", "title": "视频 1", "upper": {"name": "UP"}},
                ],
                "has_more": True,
            }
        return {
            "info": {"title": "学习资料"},
            "medias": [
                {"bvid": "BV2", "title": "视频 2", "upper": {"name": "UP"}},
            ],
            "has_more": False,
        }

    async def fake_close(self):
        return None

    async def fake_sleep(delay):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_favorite_content", fake_content)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)
    monkeypatch.setattr("app.main.asyncio.sleep", fake_sleep)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.commit()

        response = client.get("/api/bilibili/favorites/123/videos")
        assert response.status_code == 200
        assert calls == [1, 2]
        assert response.json()["total"] == 2
        assert [item["bvid"] for item in response.json()["videos"]] == ["BV1", "BV2"]
    finally:
        app.dependency_overrides.clear()


def test_bilibili_video_payload_does_not_treat_media_id_as_cid():
    payload = video_payload_from_media({"id": 12345, "bvid": "BV1", "title": "视频"})

    assert payload["aid"] == 12345
    assert payload["cid"] is None


def test_bilibili_videos_marks_stale_notes_for_regeneration(monkeypatch):
    async def fake_content(self, media_id, pn=1, ps=20):
        return {
            "info": {"title": "学习资料"},
            "medias": [{"bvid": "BVSTALE", "title": "旧视频", "upper": {"name": "UP"}}],
            "has_more": False,
        }

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_favorite_content", fake_content)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.add(Note(title="旧视频", body_md="# 旧视频", source_type="bilibili", source_id="BVSTALE"))
            session.commit()

        response = client.get("/api/bilibili/favorites/123/videos?page=1")
        assert response.status_code == 200
        video = response.json()["videos"][0]
        assert video["note_id"] is not None
        assert video["needs_regeneration"] is True
    finally:
        app.dependency_overrides.clear()


def test_stale_bilibili_notes_are_quarantined_before_listing():
    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                Note(
                    title="装机视频",
                    body_md="# 装机视频\n\n这是一段完全不相关的锅包肉内容。",
                    source_type="bilibili",
                    source_id="BVSTALE",
                    source_url="https://www.bilibili.com/video/BVSTALE",
                    source_meta={"content_source": "subtitle"},
                )
            )
            session.commit()

        response = client.get("/api/notes")
        assert response.status_code == 200
        note = response.json()["notes"][0]
        assert note["source_meta"]["content_trust"] == "stale"
        assert note["source_meta"]["legacy_body_hidden"] is True
        assert "旧版 Bilibili 笔记" in note["body_md"]
        assert "锅包肉" not in note["body_md"]

        search = client.get("/api/notes?query=锅包肉")
        assert search.status_code == 200
        assert search.json()["notes"] == []
    finally:
        app.dependency_overrides.clear()


def test_bilibili_import_job_can_be_started_polled_and_canceled(monkeypatch):
    async def fake_runner(**kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr("app.main._run_bilibili_import_job", fake_runner)
    main_module.BILIBILI_IMPORT_JOBS.clear()
    main_module.BILIBILI_IMPORT_TASKS.clear()

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.commit()

        response = client.post(
            "/api/bilibili/import-jobs",
            json={"media_id": 123, "folder_title": "学习资料", "videos": [{"bvid": "BVJOB", "title": "后台任务"}]},
        )
        assert response.status_code == 202
        job = response.json()["job"]
        assert job["total"] == 1
        assert job["status"] == "queued"

        current = client.get("/api/bilibili/import-jobs/current")
        assert current.status_code == 200
        assert current.json()["job"]["id"] == job["id"]

        canceled = client.post(f"/api/bilibili/import-jobs/{job['id']}/cancel", json={})
        assert canceled.status_code == 200
        assert canceled.json()["job"]["status"] in {"canceling", "canceled"}
    finally:
        for task in list(main_module.BILIBILI_IMPORT_TASKS.values()):
            task.cancel()
        main_module.BILIBILI_IMPORT_JOBS.clear()
        main_module.BILIBILI_IMPORT_TASKS.clear()
        app.dependency_overrides.clear()


def test_bilibili_import_creates_markdown_note_from_subtitle(monkeypatch):
    async def fake_video_info(self, bvid):
        return {
            "bvid": bvid,
            "aid": 9,
            "cid": 8,
            "title": "测试视频",
            "desc": "视频简介",
            "pic": "https://example.com/pic.jpg",
            "duration": 60,
            "owner": {"name": "UP", "mid": 7},
        }

    async def fake_player(self, bvid, cid, aid=None):
        assert cid == 8
        assert aid == 9
        return {"subtitle": {"subtitles": [{"subtitle_url": "https://example.com/subtitle.json"}]}}

    async def fake_download(self, url):
        return "测试视频 视频简介 第一件事情\n测试视频 视频简介 第二件事情\n测试视频 视频简介 第三件事情"

    async def fake_summary(self, bvid, cid, up_mid=None):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_video_info", fake_video_info)
    monkeypatch.setattr("app.main.BilibiliService.get_player_info", fake_player)
    monkeypatch.setattr("app.main.BilibiliService.download_subtitle", fake_download)
    monkeypatch.setattr("app.main.BilibiliService.get_video_summary", fake_summary)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.commit()

        response = client.post(
            "/api/bilibili/import",
            json={
                "media_id": 123,
                "folder_title": "学习资料",
                "videos": [{"bvid": "BVTEST", "title": "列表标题", "cid": 999, "aid": 111}],
            },
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "imported"
        assert response.json()["items"][0]["content_source"] == "subtitle"

        notes = client.get("/api/notes").json()["notes"]
        assert notes[0]["title"] == "测试视频"
        assert notes[0]["body_md"].startswith("# 测试视频")
        assert notes[0]["source_type"] == "bilibili"
        assert notes[0]["source_id"] == "BVTEST"
        assert notes[0]["folder_path"] == "Bilibili/学习资料"
        assert "测试视频 视频简介 第一件事情" in notes[0]["body_md"]
        assert notes[0]["source_meta"]["cid"] == 8
        assert notes[0]["source_meta"]["aid"] == 9
        assert notes[0]["source_meta"]["content_source"] == "subtitle"
        assert notes[0]["source_meta"]["content_trust"] == "trusted"
    finally:
        app.dependency_overrides.clear()


def test_bilibili_import_downgrades_untrusted_subtitle(monkeypatch):
    async def fake_video_info(self, bvid):
        return {
            "bvid": bvid,
            "aid": 9,
            "cid": 8,
            "title": "纯白 ITX 装机教程",
            "desc": "CPU i5 主板 B760 显卡 4060TI 纯白 ITX 装机配置",
            "owner": {"name": "UP", "mid": 7},
        }

    async def fake_player(self, bvid, cid, aid=None):
        return {"subtitle": {"subtitles": [{"subtitle_url": "https://example.com/subtitle.json"}]}}

    async def fake_download(self, url):
        return "黄磊老师做锅包肉 徒手搅鸡蛋 山楂 糖醋里脊 味道很酸 评论区都在吐槽 明星做菜 厨艺复刻 口感评价"

    async def fake_summary(self, bvid, cid, up_mid=None):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_video_info", fake_video_info)
    monkeypatch.setattr("app.main.BilibiliService.get_player_info", fake_player)
    monkeypatch.setattr("app.main.BilibiliService.download_subtitle", fake_download)
    monkeypatch.setattr("app.main.BilibiliService.get_video_summary", fake_summary)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.commit()

        response = client.post(
            "/api/bilibili/import",
            json={"media_id": 123, "folder_title": "学习资料", "videos": [{"bvid": "BVITX", "title": "列表标题"}]},
        )
        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["content_source"] == "untrusted_subtitle"
        assert item["content_trust"] == "untrusted"

        note = client.get("/api/notes").json()["notes"][0]
        assert note["title"] == "纯白 ITX 装机教程"
        assert note["source_meta"]["content_trust"] == "untrusted"
        assert "字幕 与视频标题/简介关联过低" in note["source_meta"]["trust_reason"]
        assert "CPU i5 主板 B760" in note["body_md"]
        assert "锅包肉" not in note["body_md"]
        assert "已跳过自动总结" in note["body_md"] or "关联过低" in note["body_md"]
    finally:
        app.dependency_overrides.clear()


def test_compose_video_note_strips_ai_source_sections():
    markdown = """# AI 改的标题

> 来源：AI 编的来源

## 来源信息
不应该保留

## 一句话总结
这是可信正文。
"""

    note = compose_video_note(
        video={"title": "真实标题", "bvid": "BVREAL", "owner_name": "真实UP", "description": "真实简介"},
        content=VideoContentResult("真实标题 可信正文", "subtitle"),
        folder_title="学习资料",
        generated_md=markdown,
    )

    assert note.startswith("# 真实标题")
    assert "真实简介" in note
    assert "AI 编的来源" not in note
    assert "不应该保留" not in note
    assert "这是可信正文" in note


def test_bilibili_import_skips_existing_note_without_calling_video_api(monkeypatch):
    async def fail_video_info(self, bvid):
        raise AssertionError("existing notes should be skipped before fetching video info")

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_video_info", fail_video_info)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.add(
                Note(
                    title="已有笔记",
                    body_md="# 已有笔记",
                    source_type="bilibili",
                    source_id="BVEXIST",
                    source_meta={"content_source": "subtitle", "content_trust": "trusted", "note_version": 2},
                )
            )
            session.commit()

        response = client.post(
            "/api/bilibili/import",
            json={
                "media_id": 123,
                "folder_title": "学习资料",
                "videos": [{"bvid": "BVEXIST", "title": "已有笔记"}],
            },
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "skipped"
        assert response.json()["items"][0]["reason"] == "已生成笔记"
    finally:
        app.dependency_overrides.clear()


def test_bilibili_import_regenerates_stale_existing_note(monkeypatch):
    async def fake_video_info(self, bvid):
        return {
            "bvid": bvid,
            "aid": 9,
            "cid": 8,
            "title": "新版标题",
            "desc": "新版简介 测试视频",
            "owner": {"name": "UP", "mid": 7},
        }

    async def fake_player(self, bvid, cid, aid=None):
        return {"subtitle": {"subtitles": [{"subtitle_url": "https://example.com/subtitle.json"}]}}

    async def fake_download(self, url):
        return "新版标题 新版简介 测试视频 可信内容 第一段 第二段 第三段"

    async def fake_summary(self, bvid, cid, up_mid=None):
        return None

    async def fake_close(self):
        return None

    monkeypatch.setattr("app.main.BilibiliService.get_video_info", fake_video_info)
    monkeypatch.setattr("app.main.BilibiliService.get_player_info", fake_player)
    monkeypatch.setattr("app.main.BilibiliService.download_subtitle", fake_download)
    monkeypatch.setattr("app.main.BilibiliService.get_video_summary", fake_summary)
    monkeypatch.setattr("app.main.BilibiliService.close", fake_close)

    try:
        client = _client_with_db()
        with client.TestingSession() as session:
            session.add(
                BilibiliSession(
                    session_id="test",
                    bili_mid=1,
                    bili_uname="me",
                    sessdata="s",
                    bili_jct="j",
                    dedeuserid="1",
                )
            )
            session.add(Note(title="旧错笔记", body_md="# 旧错笔记", source_type="bilibili", source_id="BVSTALE"))
            session.commit()

        response = client.post(
            "/api/bilibili/import",
            json={"media_id": 123, "folder_title": "学习资料", "videos": [{"bvid": "BVSTALE", "title": "旧错笔记"}]},
        )
        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "imported"

        note = client.get("/api/notes").json()["notes"][0]
        assert note["title"] == "新版标题"
        assert note["source_meta"]["note_version"] == 2
        assert note["source_meta"]["content_trust"] == "trusted"
        assert "旧错笔记" not in note["body_md"]
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
        assert "openai_base_url" in page.text
        assert "https://api.openai.com/v1" in page.text
    finally:
        app.dependency_overrides.clear()


def test_settings_page_saves_openai_base_url():
    try:
        client = _client_with_db()
        response = client.post(
            "/settings",
            data={
                "llm_provider": "openai",
                "openai_model": "gpt-5-mini",
                "openai_base_url": "https://proxy.example.com/v1/responses",
                "deepseek_model": "deepseek-v4-flash",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        page = client.get("/settings")
        assert "https://proxy.example.com/v1" in page.text
    finally:
        app.dependency_overrides.clear()
