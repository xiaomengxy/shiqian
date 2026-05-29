from app.models import Directory
from app.services.directories import get_or_create_directory_path, move_directory, normalize_directory_path, rename_directory


def test_normalize_directory_path_supports_nested_paths():
    assert normalize_directory_path(" 技术 \\ AI / 提示词 ") == "技术/AI/提示词"
    assert normalize_directory_path("") == "未分类"


def test_get_or_create_directory_path_creates_tree(db):
    leaf = get_or_create_directory_path(db, "技术/AI/提示词")
    db.commit()

    assert leaf.path == "技术/AI/提示词"
    assert leaf.depth == 2
    assert db.query(Directory).count() == 3


def test_rename_directory_rewrites_descendants(db):
    parent = get_or_create_directory_path(db, "技术/AI/提示词")
    db.commit()

    rename_directory(db, parent.parent_id, "机器学习")
    db.commit()

    assert get_or_create_directory_path(db, "技术/机器学习/提示词").path == "技术/机器学习/提示词"


def test_move_directory_rewrites_descendants(db):
    leaf = get_or_create_directory_path(db, "技术/AI/提示词")
    target = get_or_create_directory_path(db, "研究")
    db.commit()

    move_directory(db, leaf.parent_id, target.id)
    db.commit()

    assert leaf.path == "研究/AI/提示词"
    assert leaf.depth == 2

