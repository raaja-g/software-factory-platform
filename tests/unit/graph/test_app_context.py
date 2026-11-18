from swfactory.definition.models import AppDef
from swfactory.graph.app_context import render_app_md, render_feature_md, stack_from_agents_md


def test_render_app_md():
    demo = AppDef.model_validate(
        {"repo_kind": "local", "repo_url": "demo", "test_command": "uv run pytest"}
    )
    lines = render_app_md("demo", demo, stack="Python 3.13, FastAPI").splitlines()
    assert {
        "slug: demo",
        "repo kind: local",
        "default branch: main",
        "test command: uv run pytest",
        "smoke: none",
        "stack: Python 3.13, FastAPI",
    } <= set(lines)
    assert any(ln.startswith("ui globs: **/*.html, **/*.htm") for ln in lines)
    assert "stack: unknown" in render_app_md("demo", demo, stack=None).splitlines()


def test_render_feature_md():
    assert render_feature_md("T", "F") == "# T\n\nF\n"


def test_stack_section_of_agents_md():
    md = "# Proj\n\nintro\n\n## Stack\n\n- Python 3.13\n- FastAPI\n\n## Tests\n\nrun them\n"
    assert stack_from_agents_md(md) == "Python 3.13 FastAPI"
    assert stack_from_agents_md("# Proj\n\nno section\n") is None
    assert stack_from_agents_md(None) is None
