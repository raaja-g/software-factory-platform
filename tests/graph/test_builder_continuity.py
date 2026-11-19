"""M6.8: builder continuity, fresh (default) and resume."""

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.fakes.deps import make_test_deps, seed_demo
from tests.support.db_helpers import enq

from swfactory.db.repo import insert_model_decision
from swfactory.db.tables import AgentRun
from swfactory.definition.loader import init_definition_repo, load_definition
from swfactory.definition.models import Defaults
from swfactory.domain.types import ModelRef, Role, SelectionMode, ToolPolicy
from swfactory.engines import registry
from swfactory.engines.fake import FakeEngine, FakeScript
from swfactory.graph.steps import run_agent_step
from swfactory.models.profiles import resolve_profile
from swfactory.models.selection import ModelChoice
from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.paths import REPO

B = Role.BUILDER


def _defn(defaults_continuity, profile_continuity):
    with tempfile.TemporaryDirectory() as d:
        init_definition_repo(Path(d))
        r = load_definition(d)
    assert r.definition is not None
    defn = r.definition
    name = next(iter(defn.profiles))
    prof = defn.profiles[name].model_copy(update={"continuity": profile_continuity})
    fac = defn.factory.model_copy(
        update={
            "defaults": defn.factory.defaults.model_copy(
                update={"builder_continuity": defaults_continuity}
            )
        }
    )
    defn = defn.model_copy(update={"profiles": {**defn.profiles, name: prof}, "factory": fac})
    return defn, name, next(iter(defn.factory.apps.values()))


def test_continuity_resolves_profile_then_defaults():
    def cont(d, p):
        defn, name, app = _defn(d, p)
        return resolve_profile(defn, app, override=name, triage_profile=None)[0].continuity

    assert cont("resume", None) == "resume"
    assert cont("resume", "fresh") == "fresh"
    assert cont("fresh", "resume") == "resume"
    assert Defaults().builder_continuity == "fresh"


@pytest.fixture
async def cont(sessions):
    await seed_demo(sessions, defaults={"engine_retry_backoff_s": [0, 0]})
    job = await enq(sessions, app="demo")

    async def make(*, fail_first=None, fail_tokens=0):
        turns = [{"role": "builder", "round": 1, "output": "r1", "session_id": "s-1"}]
        if fail_first is not None:
            turns.append(
                {
                    "role": "builder",
                    "round": 2,
                    "attempt": 1,
                    "status": "failed",
                    "error": fail_first,
                    "usage": {"input_tokens": fail_tokens},
                }
            )
            turns.append({"role": "builder", "round": 2, "attempt": 2, "output": "r2"})
        else:
            turns.append({"role": "builder", "round": 2, "output": "r2", "session_id": "s-2"})
        script = FakeScript.from_dict({"turns": turns})
        deps = make_test_deps(sessions, script)
        await deps.sandboxes.ensure(job.id, RunnerSpec("standard", "img", 1.0, 1, 1, ()))
        engine = registry.get_engine("fake")
        return SimpleNamespace(deps=deps, job=job.id, fake_engine=engine, script=script)

    return make


async def builder_step(env, rnd, sessions, *, resume, **kw):
    out = await run_agent_step(
        env.deps,
        job_id=env.job,
        role=B,
        round=rnd,
        inputs=[],
        output_name="build.md",
        workdir=REPO,
        tool_policy=ToolPolicy.WRITE,
        resume=resume,
        **kw,
    )
    async with sessions() as s:
        return await s.get(AgentRun, out.run_id)


async def _two_rounds(env, sessions, *, resume=True, **kw):
    await builder_step(env, 1, sessions, resume=False)
    return await builder_step(env, 2, sessions, resume=resume, **kw)


@pytest.mark.db
async def test_fresh_is_default(cont, sessions):
    env = await cont()
    run = await _two_rounds(env, sessions, resume=False)
    assert env.fake_engine.tasks[-1].resume_session is None and run.meta["continuity"] == "fresh"


@pytest.mark.db
async def test_resume_reuses_session(cont, sessions):
    env = await cont()
    run = await _two_rounds(env, sessions)
    assert env.fake_engine.tasks[-1].resume_session == "s-1"
    assert run.meta["continuity"] == "resume" and run.session_id == "s-2"


@pytest.mark.db
async def test_resume_falls_back_to_fresh_when_model_changed(cont, sessions):
    env = await cont()
    async with sessions() as s, s.begin():
        decision_id = await insert_model_decision(
            s,
            job_id=env.job,
            role="builder",
            round=2,
            mode="fixed",
            options=[],
            chosen="gemini-flash@vertex",
            reason="test",
            latency_ms=0,
        )
    choice = ModelChoice(
        "fake",
        ModelRef("gemini-flash@vertex", "vertex", "gemini-flash"),
        SelectionMode.FIXED,
        None,
        decision_id,
        "test",
    )
    run = await _two_rounds(env, sessions, choice=choice)
    assert env.fake_engine.tasks[-1].resume_session is None
    assert run.meta["continuity"] == "fresh: engine or model changed"


@pytest.mark.db
async def test_direct_never_resumes(cont, sessions, monkeypatch):
    env = await cont()
    direct = FakeEngine(env.script, name="direct")
    direct.tasks = env.fake_engine.tasks
    monkeypatch.setitem(registry._engines, "direct", direct)
    run = await _two_rounds(env, sessions, engine="direct")
    assert env.fake_engine.tasks[-1].resume_session is None
    assert run.meta["continuity"] == "fresh: direct"


@pytest.mark.db
async def test_resume_load_failure_retries_fresh_once(cont, sessions):
    env = await cont(fail_first="No conversation found with session ID: s-1")
    run = await _two_rounds(env, sessions)
    assert [t.resume_session for t in env.fake_engine.tasks[-2:]] == ["s-1", None]
    assert (run.status, run.attempt, run.meta["continuity"]) == ("ok", 2, "fresh: resume failed")


@pytest.mark.db
async def test_failure_after_model_turns_keeps_session(cont, sessions):
    env = await cont(fail_first="lint failed", fail_tokens=900)
    run = await _two_rounds(env, sessions)
    assert env.fake_engine.tasks[-1].resume_session == "s-1"
    assert (run.status, run.meta["continuity"]) == ("failed", "resume")
