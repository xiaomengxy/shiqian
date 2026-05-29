from app.services.tags import normalize_tags


def test_normalize_tags_filters_uncategorized_and_generic_labels():
    assert normalize_tags("未分类, AI, 资料, AI, Prompt") == ["AI", "Prompt"]


def test_normalize_tags_falls_back_to_pending_when_only_generic():
    assert normalize_tags(["未分类", "网页"]) == ["待整理"]
