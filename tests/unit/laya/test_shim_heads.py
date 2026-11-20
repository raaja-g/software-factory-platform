"""M11.8: the router shim's head store, pins and per-question head application (no laya needed)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from swfactory.laya.fit import questions_sha256 as client_sha
from swfactory.models.triage import TRIAGE_QUESTIONS

_spec = importlib.util.spec_from_file_location(
    "router_shim", Path(__file__).parents[3] / "router" / "shim.py"
)
assert _spec and _spec.loader
shim = importlib.util.module_from_spec(_spec)
sys.modules["router_shim"] = shim
_spec.loader.exec_module(shim)

Q = {"type": "choice", "instructions": "i", "criteria": {"A": "a", "B": "b"}}


def save(store, q="touches_ui", v=1, definition=Q, payload=None):
    return store.save(
        q, v, csv_text="state,x\n", questions={q: definition},
        payload=payload or {"temperature": [1, 1, 1], "tag": f"{q}{v}"},
        metrics={"rows": 60}, router_version="laya 0.4.31",
    )  # fmt: skip


def test_hash_matches_the_orchestrator():
    for d in (*TRIAGE_QUESTIONS.values(), Q):
        assert shim.questions_sha256(d) == client_sha(d)


def test_candidate_is_not_served_until_pinned_or_activated(tmp_path):
    store = shim.HeadStore(tmp_path)
    sha = save(store)
    assert len(sha) == 64
    assert store.resolve({"touches_ui": Q}) == {"touches_ui": None}  # a candidate serves nothing
    assert store.resolve({"touches_ui": Q}, {"touches_ui": 1}) == {"touches_ui": 1}
    store.activate("touches_ui", 1, sha)
    assert shim.HeadStore(tmp_path).resolve({"touches_ui": Q}) == {"touches_ui": 1}  # a restart
    assert (tmp_path / "touches_ui" / "active").read_text() == "1"
    assert shim.head_labels({"touches_ui": 1, "kind": None}) == {
        "touches_ui": "touches_ui@v1", "kind": None,
    }  # fmt: skip


def test_activate_checks_the_hash_and_the_files(tmp_path):
    store = shim.HeadStore(tmp_path)
    sha = save(store)
    with pytest.raises(shim.HeadError, match="does not hash"):
        store.activate("touches_ui", 1, "0" * 64)
    with pytest.raises(shim.HeadError, match="no head"):
        store.activate("touches_ui", 2, sha)
    (tmp_path / "touches_ui" / "v1" / "head.json").write_text('{"tampered": 1}')
    with pytest.raises(shim.HeadError):
        store.activate("touches_ui", 1, sha)
    assert store.resolve({"touches_ui": Q}, {"touches_ui": 1}) == {"touches_ui": None}


def test_edited_question_is_served_unfitted(tmp_path):
    store = shim.HeadStore(tmp_path)
    sha = save(store)
    store.activate("touches_ui", 1, sha)
    edited = {**Q, "criteria": {"A": "changed", "B": "b"}}
    assert store.resolve({"touches_ui": edited}) == {"touches_ui": None}


def test_bad_names_are_refused(tmp_path):
    store = shim.HeadStore(tmp_path)
    with pytest.raises(shim.HeadError):
        save(store, q="../x")
    with pytest.raises(shim.HeadError):
        save(store, v=0)
    assert store.resolve({"../x": Q}, {"../x": 1}) == {"../x": None}


def test_listing(tmp_path):
    store = shim.HeadStore(tmp_path)
    s1 = save(store, v=1)
    save(store, v=2)
    store.activate("touches_ui", 1, s1)
    rows = store.listing()
    assert [(r["version"], r["active"]) for r in rows] == [(1, True), (2, False)]


def test_add_head_version():
    payload = {"results": [{"answers": {"a": {"choice": "A"}, "b": {"choice": "B"}}}]}
    out = shim.add_head_version(payload, {"a": "a@v2"})
    r = out["results"][0]
    assert r["head_version"] is None
    assert r["answers"]["a"]["head_version"] == "a@v2" and r["answers"]["b"]["head_version"] is None
    single = shim.add_head_version({"answers": {"a": {}}}, {"a": "a@v1"})
    assert single["answers"]["a"]["head_version"] == "a@v1"


class FakeAgent:
    def __init__(self):
        self.cal = {"tag": "base"}


class FakeRouter:
    def __init__(self):
        self._agents = {"english": FakeAgent()}
        self.seen: list[tuple[str, tuple[str, ...]]] = []

    def predict(self, state, questions, **kw):
        a = self._agents["english"]
        self.seen.append((a.cal["tag"], tuple(questions)))
        return {"answers": {q: {"choice": "A", "tag": a.cal["tag"]} for q in questions}}

    def predict_batch(self, requests, **kw):
        return [self.predict(r["state"], r["questions"]) for r in requests]


class FakeServing(shim.Serving):
    def export(self, agent):
        return dict(agent.cal)

    def install(self, agent, payload):
        agent.cal = dict(payload)


def test_each_question_is_answered_by_its_own_head_and_baseline_returns(tmp_path):
    store = shim.HeadStore(tmp_path)
    other = {**Q, "instructions": "j"}
    save(store, "touches_ui", 1, Q, {"tag": "ui1"})
    save(store, "size", 1, other, {"tag": "size1"})
    save(store, "size", 2, other, {"tag": "size2"})
    router = FakeRouter()
    serving = FakeServing(router, store)
    serving.wrap()
    qs = {"touches_ui": Q, "size": other, "risk": Q}
    serving.plan = store.resolve(qs, {"touches_ui": 1, "size": 2})
    res = router.predict_batch([{"state": "s1", "questions": qs}, {"state": "s2", "questions": qs}])
    assert [{q: a["tag"] for q, a in r["answers"].items()} for r in res] == [
        {"risk": "base", "touches_ui": "ui1", "size": "size2"}
    ] * 2
    assert router._agents["english"].cal == {"tag": "base"}  # nothing leaks to the next request
    serving.plan = None  # nothing pinned or active: everything unfitted
    res = router.predict_batch([{"state": "s1", "questions": qs}])
    assert {a["tag"] for a in res[0]["answers"].values()} == {"base"}
    assert router.predict("s", qs)["answers"]["size"]["tag"] == "base"


def test_json_roundtrip_of_a_payload(tmp_path):
    store = shim.HeadStore(tmp_path)
    save(
        store,
        payload={
            "temperature": [0.5, 1.5, 2.0],
            "binning_map": {"2": {"bins": 1, "values": [0.5]}},
        },
    )
    assert json.dumps(store.payload("touches_ui", 1), sort_keys=True).startswith('{"binning_map"')
