import uuid

import pytest

from swfactory.backlogs import (
    RequirementItem,
    format_backlog_ref,
    parse_backlog_ref,
    render_item_feature,
)


def test_backlog_refs():
    assert format_backlog_ref("todo", 1) == "todo:b001"
    assert parse_backlog_ref("todo:b001") == ("todo", 1)
    assert parse_backlog_ref("todo:b7") == ("todo", 7)
    u = uuid.uuid4()
    assert parse_backlog_ref(str(u)) == u
    for bad in ("todo:001", "todo:b", "b001", ""):
        with pytest.raises(ValueError):
            parse_backlog_ref(bad)


def test_render_item_feature():
    it = RequirementItem(
        "a", "Add todos", "Add and list todos.", (), ("POST creates one", "GET lists them")
    )
    assert render_item_feature(it) == (
        "Add todos\n\nAdd and list todos.\n\n## Acceptance criteria\n"
        "- [ ] POST creates one\n- [ ] GET lists them"
    )
    assert render_item_feature(RequirementItem("a", "t", "f", (), ())) == "t\n\nf"
