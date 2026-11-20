from collections import Counter

from tests.unit.api.test_access import _app_server, _flat


def test_no_two_routes_share_a_method_and_path():
    # a second handler for the same method and path is silently shadowed (first one wins)
    seen = Counter(
        (m, r.path) for r in _flat(_app_server().routes) for m in getattr(r, "methods", None) or ()
    )
    assert [k for k, n in seen.items() if n > 1] == []


def test_score_routes_are_registered_once():
    seen = Counter(
        (m, r.path) for r in _flat(_app_server().routes) for m in getattr(r, "methods", None) or ()
    )
    assert seen[("POST", "/jobs/{ref}/score")] == 1
    assert seen[("GET", "/score-requests/{request_id}")] == 1
