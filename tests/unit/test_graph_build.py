from types import SimpleNamespace
from uuid import uuid4

from langgraph.checkpoint.memory import InMemorySaver
from tests.fakes.deps import make_unit_deps

from swfactory.definition.models import AppDef, Defaults
from swfactory.graph.build import NODE_NAMES, build_job_graph, initial_state, node_timeouts

g = build_job_graph(make_unit_deps(), InMemorySaver())


def spec_of(graph, name):
    return graph.builder.nodes[name]


def run_timeout(graph, name) -> float:
    t = spec_of(graph, name).timeout
    assert t is not None and t.run_timeout is not None
    return t.run_timeout


def spec(name):
    return spec_of(g, name)


def test_graph_shape():
    assert set(NODE_NAMES) <= set(g.get_graph().nodes) and spec("after_reviews").defer is True
    assert {n: run_timeout(g, n) for n in ("triage", "spec", "build", "review", "approve")} == {
        n: v + 60 for n, v in node_timeouts(Defaults()).items()
    }
    assert run_timeout(g, "build") == 10930  # 3 x 3600 + 10 + 60, plus 60


def test_timeouts_follow_definition():
    g2 = build_job_graph(
        make_unit_deps(),
        InMemorySaver(),
        timeouts={**node_timeouts(Defaults()), "build": 600},
    )
    assert run_timeout(g2, "build") == 660


def defn_with(app, defaults):
    base = AppDef(repo_kind="github", repo_url="https://example.com/x.git").model_copy(update=app)
    fac = SimpleNamespace(apps={"demo": base}, defaults=Defaults.model_validate(defaults))
    return SimpleNamespace(factory=fac)


def test_initial_state_reads_pinned_definition():
    job = SimpleNamespace(id=uuid4(), kind="normal", app_slug="demo")
    st = initial_state(job, defn_with({"max_rounds": 3}, {"max_fix_attempts": 2}))  # type: ignore[arg-type]
    assert {k: st.get(k) for k in ("round", "max_rounds", "fix_attempts", "max_fix_attempts")} == {
        "round": 1,
        "max_rounds": 3,
        "fix_attempts": 0,
        "max_fix_attempts": 2,
    }
