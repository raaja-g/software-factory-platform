import pytest
from langgraph.checkpoint.memory import InMemorySaver
from tests.fakes.deps import make_unit_deps
from tests.unit.test_graph_build import run_timeout, spec_of

from swfactory.definition.models import Defaults
from swfactory.graph.build import (
    REQUIREMENTS_NODE_NAMES,
    build_job_graph,
    build_requirements_graph,
    graph_for,
)
from swfactory.graph.routing import REQUIREMENTS_GATE_REASON, route_requirements_gate
from swfactory.graph.steps import step_budget_s


@pytest.mark.parametrize(
    "action,retry_node,want",
    [
        ("approve", None, "finalize"),
        ("reject", None, "finalize"),
        ("rework", None, "requirements"),
        ("retry", "requirements", "requirements"),
        ("retry", "finalize", "finalize"),
    ],
)
def test_route_requirements_gate(action, retry_node, want):
    state = {"human": {"action": action}, "retry_node": retry_node}
    assert route_requirements_gate(state) == want  # type: ignore[arg-type]


def test_gate_reason():
    assert REQUIREMENTS_GATE_REASON == "approve the backlog"


def test_graph_for_picks_the_graph_by_kind():
    assert graph_for("requirements") is build_requirements_graph
    assert graph_for("normal") is build_job_graph and graph_for("benchmark") is build_job_graph


def test_requirements_graph_shape():
    g = build_requirements_graph(make_unit_deps(), InMemorySaver())
    nodes = set(g.get_graph().nodes)
    assert set(REQUIREMENTS_NODE_NAMES) <= nodes
    assert not {"triage", "build", "review", "deliver", "score"} & nodes
    assert run_timeout(g, "requirements") == step_budget_s(Defaults(), "requirements") + 60
    assert spec_of(g, "requirements").timeout.run_timeout == (  # type: ignore[union-attr]
        step_budget_s(Defaults(), "requirements") + 60
    )
    assert spec_of(g, "finalize").error_handler_node is not None
