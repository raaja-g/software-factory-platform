import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END
from tests.fakes.deps import make_unit_deps

from swfactory.db.repo import LeaseLost
from swfactory.graph.build import NODE_NAMES, build_job_graph, make_error_handler
from swfactory.graph.routing import failure_update

g = build_job_graph(make_unit_deps(), InMemorySaver())
state = {"job_id": "x", "round": 1}


def handler_of(name):
    """The node's error handler function (langgraph keeps it as its own node)."""
    handler_node = g.builder.nodes[name].error_handler_node
    assert handler_node is not None
    return g.builder.nodes[handler_node].runnable.func  # pyright: ignore[reportAttributeAccessIssue]


def test_error_handler_routes_to_gate():
    cmd = make_error_handler("build")(state, RuntimeError("x"))  # type: ignore[arg-type]
    assert (cmd.goto, cmd.update) == (
        "human_gate",
        {**failure_update("build"), "gate_scope": None},
    )


def test_review_failure_retries_verify():
    cmd = make_error_handler("review")(state, RuntimeError("x"))  # type: ignore[arg-type]
    assert cmd.update == {
        "gate_reason": "step failed: review",
        "retry_node": "verify",
        "gate_scope": None,
    }


def test_every_node_has_an_error_handler():
    assert [n for n in NODE_NAMES if g.builder.nodes[n].error_handler_node is None] == []


@pytest.mark.parametrize("node", ["score", "finalize"])
def test_score_and_finalize_handlers_end_graph(node):  # L13, L14
    cmd = handler_of(node)(state, RuntimeError("x"))
    assert cmd.goto == END and "gate_reason" not in (cmd.update or {})


@pytest.mark.parametrize("node", ["deliver", "score", "finalize"])
def test_handlers_reraise_lease_lost(node):
    with pytest.raises(LeaseLost):
        handler_of(node)(state, LeaseLost())


def test_error_handler_logs_why_the_step_failed():
    # the gate reason only names the node; the log carries the cause so an operator can find it
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        make_error_handler("prepare")(state, FileNotFoundError("no such repo /repos/demo.git"))  # type: ignore[arg-type]
    failed = [x for x in logs if x["event"] == "node.failed"]
    assert len(failed) == 1
    assert (failed[0]["node"], failed[0]["cause"]) == ("prepare", "FileNotFoundError")
    assert "no such repo" in failed[0]["detail"] and failed[0]["job_id"] == "x"
    assert isinstance(
        failed[0]["exc_info"], FileNotFoundError
    )  # rendered as a traceback by the log


def test_error_handler_log_detail_is_truncated():
    from structlog.testing import capture_logs

    with capture_logs() as logs:
        make_error_handler("prepare")(state, RuntimeError("x" * 5000))  # type: ignore[arg-type]
    assert len(next(x for x in logs if x["event"] == "node.failed")["detail"]) == 300
