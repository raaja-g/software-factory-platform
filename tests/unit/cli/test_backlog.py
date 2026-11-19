import pytest
from tests.unit.cli.conftest import run

from swfactory.cli.backlog import BacklogItem, parse_backlog

V1_BACKLOG_ALL_CHECKED = "# Backlog\n- [x] one\n- [x] two\n  - [~] three\n"


def test_parse_backlog_v1_file():
    assert parse_backlog(V1_BACKLOG_ALL_CHECKED) == []


def test_parse_backlog_open_items():
    text = "- [ ] a\n- [x] b\n- [~] c\n  - [ ] d\n"
    assert parse_backlog(text) == [BacklogItem(1, "a"), BacklogItem(4, "d")]


def test_backlog_import_posts_source_ref(mock_api, backlog_file):
    r = run("backlog", "import", "todo", "b.md")
    assert r.exit_code == 0, r.output
    assert "backlog todo:b001 running, 1 items" in r.output
    assert mock_api.paths[0] == "/api/backlogs/import" and mock_api.bodies[0]["app"] == "todo"
    assert mock_api.bodies[0]["title"] == "b.md"
    got = [(i["id"], i["title"], i["feature"]) for i in mock_api.bodies[0]["items"]]
    assert got == [("3", "a", "a")]


def test_backlog_import_dry_run_posts_nothing(mock_api, backlog_file):
    r = run("backlog", "import", "todo", "b.md", "--dry-run")
    assert mock_api.paths == [] and "1 item" in r.output


def test_backlog_list_and_show(mock_api):
    assert "todo:b001" in run("backlog", "list").output
    assert "paused" in run("backlog", "list").output
    out = run("backlog", "show", "todo:b001").output
    assert out.index("todo:001") < out.index("todo:002") and "merged" in out
    assert "blocked on todo:002" in out


@pytest.mark.parametrize("action", ["resume", "skip", "cancel"])
def test_backlog_actions_post_the_action(mock_api, action):
    run("backlog", action, "todo:b001")
    assert mock_api.paths[-1] == "/api/backlogs/todo:b001/actions"
    assert mock_api.bodies[-1] == {"action": action}


def test_backlog_action_409_exits_1_with_detail(mock_api_409):
    r = run("backlog", "resume", "todo:b001")
    assert r.exit_code == 1 and "skip it or cancel the backlog" in r.output
