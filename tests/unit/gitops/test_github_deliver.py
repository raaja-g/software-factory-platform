"""GitHubGit: prepare, rebase, keep_branch and idempotent deliver (Review focus #2).

Real git on the host (a local bare repo plays GitHub's git, FakeGitops runs the gitops argv),
FakeGitHub plays the API, Postgres holds the job row (pushed_sha, merge_request_id).
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.fakes.github_git import DeliveryGitHub, FakeAuth, FakeGitops, SimulatedCrash
from tests.fakes.host_sandbox import HostSandbox

from swfactory.db import repo as dbrepo
from swfactory.db.tables import App, Job, JobEvent
from swfactory.definition.models import AppDef, GitHubAppSettings, MergePolicy
from swfactory.events import record_event
from swfactory.github.auth import PERMS_FETCH, PERMS_PUSH
from swfactory.github.checks import CHECK_NAME
from swfactory.gitops.github import GitHubGit
from swfactory.gitops.local import LocalGit
from swfactory.gitops.service import (
    REASON_HEAD_MISMATCH,
    REASON_NOTHING_TO_DELIVER,
    GitDispatcher,
)

pytestmark = pytest.mark.db

SEED = Path(__file__).resolve().parents[2] / "fixtures" / "repos" / "pyapp"
BRANCH = "factory/001-x"


def sh(cwd: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


class RecBox(HostSandbox):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.exec_calls: list[list[str]] = []

    async def exec(self, argv, **kw):  # type: ignore[no-untyped-def]
        self.exec_calls.append(list(argv))
        return await super().exec(argv, **kw)


async def commit_in(box: HostSandbox, name: str = "NEW.md") -> str:
    await box.write_text(f"/work/repo/{name}", "hello\n")
    for args in (["add", "-A"], ["commit", "-q", "-m", f"add {name}"]):
        r = await box.exec(["git", *args])
        assert r.exit_code == 0, r.stderr
    return (await box.exec(["git", "rev-parse", "HEAD"])).stdout.strip()


@dataclass
class World:
    tmp: Path
    sessions: object
    origin: Path
    gh: DeliveryGitHub
    auth: FakeAuth
    gitops: FakeGitops
    git: GitHubGit
    job: Job
    box: RecBox
    head: str

    def app(self, policy: MergePolicy = MergePolicy.PR_AUTO_MERGE, *, ci_safe: bool = False):
        return AppDef(
            repo_kind="github",
            repo_url="acme/web",
            merge_policy=policy,
            github=GitHubAppSettings(ci_safe=ci_safe),
        )

    def make_git(self, **kw) -> GitHubGit:  # type: ignore[no-untyped-def]
        async def no_sleep(_: float) -> None:
            return None

        return GitHubGit(
            auth=self.auth,
            gitops=self.gitops,  # type: ignore[arg-type]
            sessions=self.sessions,  # type: ignore[arg-type]
            repos_dir=self.tmp / "repos",
            client_for=lambda repo: self.gh,  # type: ignore[arg-type,return-value]
            dashboard_url="http://dash",
            sleep=no_sleep,
            **kw,
        )

    async def decision(self, decision: str, rnd: int = 1) -> int:
        async with self.sessions.begin() as s:  # type: ignore[attr-defined]
            return await record_event(
                s,
                self.job.id,
                "decision.made",
                {"decision": decision, "round": rnd, "head_sha": self.head, "reason": "r"},
            )

    async def set_event_time(self, event_id: int, at: datetime) -> None:
        async with self.sessions.begin() as s:  # type: ignore[attr-defined]
            await s.execute(
                text("update app.job_events set at = :at where id = :i"), {"at": at, "i": event_id}
            )

    async def state(self) -> tuple[str | None, str | None]:
        async with self.sessions() as s:  # type: ignore[attr-defined]
            return await dbrepo.delivery_state(s, self.job.id)

    async def set_pushed(self, sha: str) -> None:
        async with self.sessions.begin() as s:  # type: ignore[attr-defined]
            await s.execute(
                text("update app.jobs set pushed_sha = :p where id = :i"),
                {"p": sha, "i": self.job.id},
            )

    async def new_box(self, name: str) -> RecBox:
        return RecBox(self.tmp / name)


@pytest.fixture
async def w(tmp_path, sessions) -> World:
    origin = LocalGit(tmp_path / "origin").init_bare("web", seed=SEED)
    gh = DeliveryGitHub()
    auth = FakeAuth()
    gitops = FakeGitops(tmp_path / "gitops", origin, gh, auth)
    job_id = uuid.uuid4()
    async with sessions.begin() as s:
        app = App(slug="web", repo_kind="github", repo_url="acme/web")
        s.add(app)
        await s.flush()
        s.add(
            Job(
                id=job_id,
                app_id=app.id,
                seq=1,
                slug="x",
                title="Add x",
                feature="Add x\nplease",
                requested_by="me",
                budget_usd=1,
            )
        )
    job = Job(id=job_id, seq=1, slug="x", title="Add x", feature="Add x\nplease")
    world = World(tmp_path, sessions, origin, gh, auth, gitops, None, job, None, "")  # type: ignore[arg-type]
    world.git = world.make_git()
    world.box = await world.new_box("box")
    await world.git.prepare(job, world.app(), world.box)
    world.head = await commit_in(world.box)
    return world


async def test_base_head_is_the_default_branch_last_bundled(w):
    assert await w.git.base_head(w.job, w.app()) == w.gh.branches["main"]
    assert await w.git.prepare(w.job, w.app(), w.box) == w.gh.branches["main"]


async def test_prepare_mints_a_fetch_token_only(w):
    assert w.gitops.handed == [PERMS_FETCH]
    branch = (await w.box.exec(["git", "branch", "--show-current"])).stdout.strip()
    assert branch == BRANCH
    name = (await w.box.exec(["git", "config", "user.name"])).stdout.strip()
    assert name == "fac[bot]"


async def test_escalate_leaves_draft_with_one_comment(w):
    await w.decision("ESCALATE")
    app = w.app(MergePolicy.PR_MANUAL)
    for _ in range(2):
        d = await w.git.deliver(w.job, app, w.box, draft=True, expected_head=w.head)
        assert d.status == "pr_open"
    pr = w.gh.pr_for(BRANCH)
    n = w.gh.number_of(BRANCH)
    assert pr["draft"] and "factory:needs-human" in w.gh.labels_of(BRANCH)
    assert "factory" in w.gh.labels_of(BRANCH)
    assert len(w.gh.comments[n]) == 1
    assert w.gh.check_runs[(w.head, CHECK_NAME)]["conclusion"] == "action_required"
    assert w.gh.counts["create_pr"] == 1 and w.gh.counts["push"] == 1


async def test_pr_manual_returns_pr_open(w):
    d = await w.git.deliver(
        w.job, w.app(MergePolicy.PR_MANUAL), w.box, draft=False, expected_head=w.head
    )
    assert d.status == "pr_open" and d.pr_url
    assert not w.gh.pr_for(BRANCH)["draft"]


async def test_keep_branch_never_pushes_when_ci_is_not_safe(w):
    await w.git.keep_branch(w.job, w.app(MergePolicy.PR_MANUAL), w.box)
    assert w.gh.counts["push"] == 0 and w.auth.minted == [PERMS_FETCH]
    kept = sh(w.tmp / "repos" / "github" / "acme" / "web.git", "rev-parse", f"refs/heads/{BRANCH}")
    assert kept == w.head
    assert (await w.state())[0] is None


async def test_keep_branch_pushes_once_when_ci_is_safe(w):
    app = w.app(MergePolicy.PR_MANUAL, ci_safe=True)
    await w.git.keep_branch(w.job, app, w.box)
    await w.git.keep_branch(w.job, app, w.box)
    assert w.gh.counts["push"] == 1 and w.gh.branches[BRANCH] == w.head
    assert (await w.state())[0] == w.head


async def test_prepare_restores_kept_branch_from_the_platform_bare_repo(w):
    app = w.app(MergePolicy.PR_MANUAL)
    await w.git.keep_branch(w.job, app, w.box)
    fresh = await w.new_box("fresh")
    await w.git.prepare(w.job, app, fresh)
    assert (await fresh.exec(["git", "rev-parse", "HEAD"])).stdout.strip() == w.head


async def test_prepare_restores_kept_branch_from_github_once_pushed(w):
    app = w.app(MergePolicy.PR_MANUAL, ci_safe=True)
    await w.git.keep_branch(w.job, app, w.box)
    fresh = await w.new_box("fresh")
    await w.git.prepare(w.job, app, fresh)
    assert (await fresh.exec(["git", "rev-parse", "HEAD"])).stdout.strip() == w.head


async def test_rebase_carries_only_new_base_commits(w):
    work = w.tmp / "adv"
    sh(w.tmp, "clone", "-q", str(w.origin), str(work))
    (work / "other.txt").write_text("x")
    sh(work, "add", "-A")
    sh(work, "commit", "-q", "-m", "advance")
    sh(work, "push", "-q", "origin", "main")
    app = w.app()
    assert await w.git.rebase(w.job, app, w.box) is True
    new_main = sh(work, "rev-parse", "HEAD")
    assert await w.git.base_head(w.job, app) == new_main
    assert (
        await w.box.exec(["git", "merge-base", "--is-ancestor", new_main, "HEAD"])
    ).exit_code == 0
    assert await w.box.read_text("/work/repo/NEW.md") == "hello\n"


async def test_human_approval_turns_the_check_green_only_for_the_latest_decision(w):
    app = w.app(MergePolicy.PR_MANUAL)
    t0 = datetime.now(UTC)
    first = await w.decision("ESCALATE")
    await w.set_event_time(first, t0)
    async with w.sessions.begin() as s:
        cid = await dbrepo.insert_command(
            s, job_id=w.job.id, action="approve", note=None, requested_by="github:ana"
        )
        await dbrepo.consume_command(s, cid, "approved")
        await s.execute(
            text("update app.commands set consumed_at = :t where id = :i"),
            {"t": t0 + timedelta(seconds=5), "i": cid},
        )
    await w.git.deliver(w.job, app, w.box, draft=False, expected_head=w.head)
    assert w.gh.check_runs[(w.head, CHECK_NAME)]["conclusion"] == "success"
    assert "factory:needs-human" not in w.gh.labels_of(BRANCH)
    second = await w.decision("ESCALATE", rnd=2)
    await w.set_event_time(second, t0 + timedelta(seconds=10))
    await w.git.deliver(w.job, app, w.box, draft=True, expected_head=w.head)
    assert w.gh.check_runs[(w.head, CHECK_NAME)]["conclusion"] == "action_required"
    assert "factory:needs-human" in w.gh.labels_of(BRANCH)


async def test_branch_equal_to_base_is_failed_not_pushed(w):
    box = await w.new_box("plain")
    await w.git.prepare(w.job, w.app(), box)
    main = w.gh.branches["main"]
    d = await w.git.deliver(w.job, w.app(), box, draft=False, expected_head=main)
    assert (d.status, d.detail) == ("failed", REASON_NOTHING_TO_DELIVER)
    assert w.gh.counts["push"] == 0


async def test_auto_merge_pins_reviewed_sha(w):
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert w.gh.merge_calls == [(1, w.head, "squash")] and d.status == "merged"
    assert d.merge_sha == "m" * 40
    assert (await w.state())[1] == "u-1"


async def test_auto_merge_blocked_uses_github_auto_merge(w):
    w.gh.merge_blocked = True
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert w.gh.auto_merge == {1: (w.head, "SQUASH")} and d.status == "pr_open"


async def test_merge_conflict_from_github_is_conflict(w):
    w.gh.prs.clear()
    await w.git.deliver(
        w.job, w.app(MergePolicy.PR_MANUAL), w.box, draft=True, expected_head=w.head
    )
    w.gh.prs[1]["head_sha"] = "d" * 40  # GitHub's head moved: merge-async answers 409
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "conflict"


async def test_pending_merge_is_polled_by_the_persisted_request_not_repeated(w):
    w.gh.defer_merge = True
    w.gh.status_override = "enqueued"
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "pr_open" and (await w.state())[1] == "u-1"
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "pr_open" and w.gh.counts["merge_async"] == 1
    w.gh.status_override = "failed"
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "failed" and (await w.state())[1] is None


async def test_head_changed_by_someone_else_is_conflict(w):
    w.gh.branches[BRANCH] = "c" * 40
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "conflict" and w.gh.counts["push"] == 0
    assert d.detail == "branch changed on GitHub"


async def test_head_not_the_approved_head_is_conflict_before_any_push(w):
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head="e" * 40)
    assert (d.status, d.detail) == ("conflict", REASON_HEAD_MISMATCH)
    assert w.gh.counts["push"] == 0 and w.gh.counts["create_pr"] == 0
    assert w.gh.counts["merge_async"] == 0


async def test_lease_uses_persisted_sha_not_live_remote(w):
    persisted = "b" * 40
    await w.set_pushed(persisted)
    w.gh.branches[BRANCH] = persisted
    d = await w.git.deliver(
        w.job, w.app(MergePolicy.PR_MANUAL), w.box, draft=True, expected_head=w.head
    )
    assert w.gh.push_leases == [persisted] and d.status == "pr_open"
    assert (await w.state())[0] == w.head


async def test_gitops_never_gets_a_checks_token(w):
    await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert w.gitops.handed == [PERMS_FETCH, PERMS_PUSH]
    assert all("checks" not in p for p in w.gitops.handed)


async def test_deliver_moves_only_the_job_commits(w):
    await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    calls = [c[-4:] for c in w.box.exec_calls]
    assert [
        "bundle",
        "create",
        "/work/.factory/git/job.bundle",
        "origin/main..factory/001-x",
    ] in calls


async def test_deliver_bundle_over_the_limit_fails_clearly(w):
    small = w.make_git(max_bundle_mb=0)
    d = await small.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "failed" and "raise FACTORY_MAX_BUNDLE_MB" in d.detail
    assert w.gh.counts["push"] == 0


async def test_lost_gitops_volume_is_refetched_and_the_import_retried(w):
    w.gitops.wipe_volume(w.job.id)
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "merged" and w.gh.counts["push"] == 1


async def test_dispatcher_routes_github_apps(w):
    disp = GitDispatcher({"github": w.git})
    d = await disp.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "merged"
    assert await disp.base_head(w.job, w.app()) == w.gh.branches["main"]


async def test_record_pushed_and_merge_request_emit_one_stage_event_each(w):
    async with w.sessions.begin() as s:
        await dbrepo.record_pushed(s, w.job.id, "a" * 40)
        await dbrepo.record_pushed(s, w.job.id, "a" * 40)
        await dbrepo.record_merge_request(s, w.job.id, "u-9")
    async with w.sessions() as s:
        rows = (
            await s.execute(
                text(
                    "select data from app.job_events "
                    "where job_id=:i and kind='job.stage' order by id"
                ),
                {"i": w.job.id},
            )
        ).all()
    assert [r[0]["step"] for r in rows] == ["pushed", "merge_requested"]
    assert all(r[0]["stage"] == "delivering" for r in rows)
    assert await w.state() == ("a" * 40, "u-9")
    assert JobEvent is not None


@pytest.mark.parametrize(
    "crash_after",
    ["push", "create_pr", "upsert_check_run", "merge_async", "record_merge_request"],
)
async def test_review_focus_2_retry_after_crash_has_no_duplicates(w, crash_after):
    if crash_after == "record_merge_request":
        # the request id is committed, then the process dies at the first poll
        w.gh.crash_once_after("merge_async_status")
    else:
        w.gh.crash_once_after(crash_after)
    with pytest.raises(SimulatedCrash):
        await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    if crash_after == "record_merge_request":
        assert (await w.state())[1] == "u-1"
    d = await w.git.deliver(w.job, w.app(), w.box, draft=False, expected_head=w.head)
    assert d.status == "merged" and w.gh.counts["create_pr"] == 1
    assert w.gh.counts["push"] == 1 and w.gh.counts["merge_async"] == 1
    assert w.gh.merged_times == 1
