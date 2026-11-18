from copy import deepcopy

from swfactory.graph.state import ReviewOutcome, merge_reviews


def o(kind: str, verdict: str = "PASS") -> ReviewOutcome:
    return ReviewOutcome(kind=kind, verdict=verdict, artifact=f"{kind}.md", run_id="r")


a, b, c = o("security"), o("ux"), o("code")
err, ok = o("ux", "ERROR"), o("ux")


def test_merge_reviews_keeps_kinds_and_rounds():
    assert merge_reviews({"1": {"security": a}}, {"1": {"ux": b}, "2": {"code": c}}) == {
        "1": {"security": a, "ux": b},
        "2": {"code": c},
    }


def test_merge_reviews_right_wins_and_none_is_empty():
    assert merge_reviews({"1": {"ux": err}}, {"1": {"ux": ok}})["1"]["ux"] is ok
    assert merge_reviews(None, None) == {}


def test_merge_reviews_does_not_mutate_inputs():
    left, right = {"1": {"security": a}}, {"1": {"ux": b}}
    lc, rc = deepcopy(left), deepcopy(right)
    merge_reviews(left, right)
    assert (left, right) == (lc, rc)
