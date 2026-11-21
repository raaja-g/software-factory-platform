"""The model-free verify gate: rebase, diff, protected paths, setup and tests."""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from swfactory.domain.types import VerifyStatus
from swfactory.gitops.service import SAFE_GIT_ENV, GitService, safe_git
from swfactory.sandbox.base import RunnerSpec, Sandbox, SandboxProvider
from swfactory.sandbox.paths import OUT, REPO
from swfactory.verify.smoke import screenshot_path

if TYPE_CHECKING:
    from swfactory.artifacts import ArtifactStore
    from swfactory.verify.smoke import SmokeRunner

TEST_TIMEOUT_S = 900
SETUP_TIMEOUT_S = 1800
REASON_REBASE_CONFLICT = "rebase conflict"
REASON_TEST_COMMAND_CHANGED = "test command changed"
REASON_HEAD_NOT_ON_BRANCH = "HEAD is not on the job branch"
REASON_NOT_ON_BASE = "branch does not contain the default branch"

_COMMAND_RE = re.compile(
    r"^[\s>*_-]*test command[\s*_]*:[\s*_]*`?([^`\n]+)`?", re.IGNORECASE | re.MULTILINE
)
_DEFAULT_TRAILERS = {"Factory-Role": "builder"}
_DIFF_PATH = f"{OUT}/diff.patch"


def resolve_test_command(app_cmd: str | None, agents_md: str | None) -> str | None:
    if app_cmd:
        return app_cmd
    if not agents_md:
        return None
    m = _COMMAND_RE.search(agents_md)
    if m is None:
        return None
    return m.group(1).strip() or None


def _matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/**"):
        return path.startswith(pattern[:-2])
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return fnmatch.fnmatchcase(path, pattern)


def protected_hits(paths: Sequence[str], patterns: Sequence[str]) -> tuple[str, ...]:
    return tuple(p for p in paths if any(_matches(p, pat) for pat in patterns))


def verify_json_name(round: int, attempt: int) -> str:
    return f"verify/r{round}-a{attempt}.json"


@dataclass(frozen=True, slots=True)
class GateResult:
    status: VerifyStatus
    reason: str | None
    test_command: str | None
    exit_code: int | None
    protected: tuple[str, ...]
    screenshots: tuple[str, ...]
    report: str
    paths: tuple[str, ...] = ()


def _git(box: Sandbox, *args: str) -> Any:
    return box.exec(safe_git(*args), cwd=REPO, env=SAFE_GIT_ENV)


# Build output and caches that never belong in a commit, left out of the safety-net commit even
# when the repo has no .gitignore (the demo repo had none and merged its __pycache__/*.pyc).
_JUNK = (
    "**/__pycache__/**",
    "**/*.pyc",
    "**/node_modules/**",
    "**/.venv/**",
    "**/.pytest_cache/**",
    "**/.mypy_cache/**",
    "**/.ruff_cache/**",
    "**/.DS_Store",
)
JUNK_PATHSPECS = tuple(f":(exclude,glob){g}" for g in _JUNK)


async def commit_leftovers(box: Sandbox, trailers: Mapping[str, str]) -> bool:
    """Commit what the builder left uncommitted, as "factory: builder changes".

    The repo's own .gitignore applies, and so does the junk list above. True when a commit was made;
    a clean tree, or one with only junk, makes none."""
    dirty = await _git(box, "status", "--porcelain")
    if not dirty.stdout.strip():
        return False
    await _git(box, "add", "-A", "--", ".", *JUNK_PATHSPECS)
    # `git status`, not `git diff`: nothing that can run a configured program is used in this box
    after = await _git(box, "status", "--porcelain")
    if not any(line[:1] in "AMDRCT" for line in after.stdout.splitlines()):
        return False  # nothing but junk was left
    await _git(
        box,
        "-c",
        "user.name=Software Factory",
        "-c",
        "user.email=factory@localhost",
        "commit",
        "-q",
        "-m",
        "factory: builder changes",
        *[f"--trailer={k}: {v}" for k, v in trailers.items()],
    )
    return True


class VerifyGate:
    def __init__(
        self,
        git: GitService,
        artifacts: ArtifactStore,
        sandboxes: SandboxProvider,
        smoke: SmokeRunner | None,
    ) -> None:
        self.git = git
        self.artifacts = artifacts
        self.sandboxes = sandboxes
        self.smoke = smoke

    async def _early(self, job: Any, round: int, attempt: int, reason: str) -> GateResult:
        report = f"# Verify\n\nStatus: {VerifyStatus.ERROR.value.upper()} ({reason})\n"
        r = GateResult(VerifyStatus.ERROR, reason, None, None, (), (), report)
        await self._store(job, round, attempt, r, "", None)
        return r

    async def _store(
        self,
        job: Any,
        round: int,
        attempt: int,
        r: GateResult,
        log: str,
        head_sha: str | None,
    ) -> None:
        put = self.artifacts.put
        await put(job.id, "verify.md", r.report, kind="md")
        if log:
            await put(job.id, f"logs/verify-r{round}-a{attempt}.log", log, kind="log")
        body = {
            "status": r.status.value,
            "reason": r.reason,
            "exit_code": r.exit_code,
            "protected": list(r.protected),
            "screenshots": list(r.screenshots),
            "test_command": r.test_command,
            "head_sha": head_sha,
        }
        await put(job.id, verify_json_name(round, attempt), json.dumps(body, indent=2), kind="json")

    async def run(
        self,
        job: Any,
        app: Any,
        box: Sandbox,
        *,
        runner: RunnerSpec,
        round: int,
        attempt: int,
        setup: Sequence[str] = (),
        test_timeout_s: int = TEST_TIMEOUT_S,
        trailers: Mapping[str, str] | None = None,
    ) -> GateResult:
        ref = f"refs/heads/{job.branch}"
        sym = await _git(box, "symbolic-ref", "HEAD")
        if sym.exit_code != 0 or sym.stdout.strip() != ref:
            return await self._early(job, round, attempt, REASON_HEAD_NOT_ON_BRANCH)
        await commit_leftovers(box, trailers or _DEFAULT_TRAILERS)
        if not await self.git.rebase(job, app, box):
            return await self._early(job, round, attempt, REASON_REBASE_CONFLICT)
        head = (await _git(box, "rev-parse", ref)).stdout.strip()
        base = await self.git.base_head(job, app)
        name = f"gate-r{round}-a{attempt}"
        async with self.sandboxes.verify_box(job.id, runner, name=name, head=head) as vbox:
            anc = await _git(vbox, "merge-base", "--is-ancestor", base, "HEAD")
            if anc.exit_code != 0:
                return await self._early(job, round, attempt, REASON_NOT_ON_BASE)
            stat = await self.git.diff_stat(vbox, base)
            paths = tuple(stat.paths)
            hits = protected_hits(paths, app.protected_paths)
            await _git(
                vbox,
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                f"--output={_DIFF_PATH}",
                f"{base}...HEAD",
            )
            patch = await vbox.read_bytes(_DIFF_PATH) or b""
            await self.artifacts.put(
                job.id, "diff.patch", patch.decode("utf-8", "replace"), kind="log"
            )
            base_md = await _git(vbox, "show", f"{base}:AGENTS.md")
            base_text = base_md.stdout if base_md.exit_code == 0 else None
            if base_text is not None:
                await self.artifacts.put(job.id, "base/AGENTS.md", base_text, kind="md")
            head_md = await _git(vbox, "show", "HEAD:AGENTS.md")
            head_text = head_md.stdout if head_md.exit_code == 0 else None
            cmd = resolve_test_command(app.test_command, base_text)
            changed = not app.test_command and resolve_test_command(None, head_text) != cmd

            checks: list[tuple[str, str, str]] = []
            failure: str | None = None
            exit_code: int | None = None
            log = ""
            if changed:
                checks.append(("Test command", "error", REASON_TEST_COMMAND_CHANGED))
            setup_failed = False
            if cmd is not None:
                for c in setup:
                    res = await vbox.exec(["bash", "-lc", c], cwd=REPO, timeout_s=SETUP_TIMEOUT_S)
                    if res.exit_code != 0:
                        failure = f"setup failed: {c} (exit {res.exit_code})"
                        exit_code = res.exit_code
                        log = res.stdout + res.stderr
                        checks.append(("Setup", "failed", failure))
                        setup_failed = True
                        break
                if not setup_failed:
                    res = await vbox.exec(["bash", "-lc", cmd], cwd=REPO, timeout_s=test_timeout_s)
                    exit_code, log = res.exit_code, res.stdout + res.stderr
                    if res.timed_out:
                        failure = f"tests timed out after {test_timeout_s // 60} min"
                    elif res.exit_code != 0:
                        failure = f"tests failed (exit {res.exit_code})"
                    checks.append(
                        ("Tests", "failed" if failure else "passed", failure or f"`{cmd}`")
                    )
            else:
                checks.append(("Tests", "skipped", "No test command found"))
            if hits:
                checks.append(("Protected paths", "failed", ", ".join(hits)))
            else:
                checks.append(("Protected paths", "passed", "none touched"))
            smoke_failure: str | None = None
            shot_names: list[str] = []
            if app.smoke is not None and self.smoke is not None:
                sr = await self.smoke.run(vbox, app.smoke)
                for sname, png in sr.shots:
                    await box.write_bytes(screenshot_path(sname), png)
                    await vbox.write_bytes(screenshot_path(sname), png)
                    await self.artifacts.put(job.id, f"screenshots/{sname}.png", png, kind="image")
                    shot_names.append(sname)
                if not sr.passed:
                    smoke_failure = f"smoke failed: {sr.detail}"
                note = sr.detail
                if sr.console_errors:
                    note += f" ({len(sr.console_errors)} console errors)"
                checks.append(("Smoke", "passed" if sr.passed else "failed", note))

        if changed:
            status, reason = VerifyStatus.ERROR, REASON_TEST_COMMAND_CHANGED
        elif failure is not None or hits or smoke_failure is not None:
            status = VerifyStatus.FAILED
            reason = failure or smoke_failure or "protected paths changed: " + ", ".join(hits)
        elif cmd is None:
            status, reason = VerifyStatus.SKIPPED, "no test command"
        else:
            status, reason = VerifyStatus.PASSED, None
        report = _render(status, reason, cmd, checks, log)
        r = GateResult(status, reason, cmd, exit_code, hits, tuple(shot_names), report, paths)
        await self._store(job, round, attempt, r, log, head)
        return r


def _cell(result: str) -> str:
    """A table result: problems stand out in capitals."""
    return result.upper() if result in ("failed", "error") else result


def _render(
    status: VerifyStatus,
    reason: str | None,
    cmd: str | None,
    checks: Sequence[tuple[str, str, str]],
    log: str,
) -> str:
    lines = [
        "# Verify",
        "",
        f"Status: {status.value.upper()}" + (f" ({reason})" if reason else ""),
        "",
    ]
    if cmd is None:
        lines += ["No test command was found (set `test_command` or add one to AGENTS.md).", ""]
    lines += ["| Check | Result | Detail |", "| --- | --- | --- |"]
    lines += [f"| {c} | {_cell(res)} | {d} |" for c, res, d in checks]
    tail = "\n".join(log.splitlines()[-200:])
    if tail:
        lines += ["", "Test output (last 200 lines):", "", "```", tail, "```"]
    return "\n".join(lines) + "\n"
