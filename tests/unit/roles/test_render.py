from pathlib import Path
from uuid import UUID

from swfactory.domain.types import DEFAULT_REVIEWERS, Role
from swfactory.roles.library import RoleLibrary
from swfactory.roles.render import (
    PromptContext,
    diff_was_cut,
    diffstat,
    estimate_tokens,
    render_prompt,
)
from swfactory.sandbox.paths import REPO

lib = RoleLibrary()
J = UUID("00000000-0000-0000-0000-000000000001")
FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "v1"
APPROVER, SPEC = (
    lib.get(Role.APPROVER, definition_sha="x"),
    lib.get(Role.SPEC_WRITER, definition_sha="x"),
)


def ctx(inputs, *, output_path=None, workdir=None, context_tokens=32_000):
    return PromptContext(
        job_id=J,
        app_slug="demo",
        round=1,
        inputs=inputs,
        output_path=output_path,
        workdir=workdir,
        context_tokens=context_tokens,
    )


def v1(path):
    return (FIXTURES / path).read_text()


def diff_of(files):
    out = []
    for path, (added, removed) in files.items():
        out.append(
            f"diff --git a/{path} b/{path}\nindex 111..222 100644\n--- a/{path}\n+++ b/{path}"
        )
        out.append(f"@@ -1,{removed} +1,{added} @@")
        out += [f"-old {i}" for i in range(removed)]
        out += [f"+new {i}" for i in range(added)]
    return "\n".join(out) + "\n"


def make_diff(*, files, total_bytes):
    per = max(1, total_bytes // files)
    lines = max(1, per // 40)
    return "".join(
        diff_of({f"src/pkg{i}/module_{i}.py": (lines, 0)}).replace(
            "new ", "new line of padding text "
        )
        for i in range(files)
    )


def omitted_section(prompt):
    tail = prompt[prompt.rfind("<<<END UNTRUSTED DATA") :].split("\n")[1:]
    return [ln for ln in tail if ln.startswith("omitted:")]


def test_review_focus_5_huge_diff_and_six_rounds_fit_32k():
    inputs = {
        "spec.md": v1("003-todo-web/spec.md"),
        "build.md": v1("003-todo-web/build.md"),
        "diff.patch": make_diff(files=400, total_bytes=2_000_000),
        **{
            f"round-{n}/review-{k}.md": v1(f"003-todo-web/round-1/review-{k}.md")
            for n in range(1, 7)
            for k in DEFAULT_REVIEWERS
        },
    }
    system, prompt = render_prompt(APPROVER, ctx(inputs, context_tokens=32_000))
    assert estimate_tokens(system) + estimate_tokens(prompt) <= 24_000
    assert "round-1/review-security.md" in "\n".join(omitted_section(prompt))
    assert "## round-6/review-security.md" in prompt and "400 files changed" in prompt


def test_small_inputs_have_no_omitted_note():
    assert "omitted:" not in render_prompt(SPEC, ctx({"feature.md": "Add a page"}))[1]


def test_every_input_is_fenced():
    assert (
        "<<<UNTRUSTED DATA: feature.md>>>"
        in render_prompt(SPEC, ctx({"feature.md": "Add a page"}))[1]
    )


def test_drops_oldest_rounds_before_touching_diff():
    diff = diff_of({"a.py": (3, 1)})
    inputs = {
        "diff.patch": diff,
        **{f"round-{n}/review-code.md": "VERDICT: CHANGES\n" + "x" * 60_000 for n in (1, 2, 3)},
    }
    _, prompt = render_prompt(APPROVER, ctx(inputs, context_tokens=64_000))
    assert omitted_section(prompt) == ["omitted: round-1/review-code.md (older review rounds)"]
    assert diff in prompt and "## round-3/review-code.md" in prompt


def test_agentic_note_points_at_full_files():
    _, prompt = render_prompt(
        APPROVER, ctx({"diff.patch": make_diff(files=50, total_bytes=500_000)}, workdir=REPO)
    )
    assert "Full files: /work/.factory/in/diff.patch" in prompt


def test_placeholders_substituted():
    builder = lib.get(Role.BUILDER, definition_sha="x")
    system, _ = render_prompt(
        builder, ctx({}, workdir=REPO, output_path="/work/.factory/out/b1.md")
    )
    assert (
        "Write your output to /work/.factory/out/b1.md and nothing else." in system
        and "{{" not in system
    )
    assert (
        "Reply with the output document only, no preamble." in render_prompt(APPROVER, ctx({}))[0]
    )


def test_huge_single_input_never_raises():
    system, prompt = render_prompt(SPEC, ctx({"feature.md": "a" * 5_000_000}, context_tokens=8_000))
    assert estimate_tokens(system) + estimate_tokens(prompt) <= 6_000
    assert "omitted: feature.md (truncated to " in prompt


def test_diffstat_format():
    assert diffstat(diff_of({"a.py": (3, 1), "b/c.html": (2, 0)})).splitlines() == [
        "a.py | 4",
        "b/c.html | 2",
        "2 files changed, 5 insertions(+), 1 deletions(-)",
    ]


def test_estimate_tokens():
    assert (estimate_tokens(""), estimate_tokens("abc"), estimate_tokens("é" * 3)) == (1, 2, 3)


def test_diff_was_cut_reads_only_the_trailing_note():
    _, cut = render_prompt(
        APPROVER, ctx({"diff.patch": make_diff(files=400, total_bytes=2_000_000)})
    )
    _, whole = render_prompt(
        APPROVER,
        ctx(
            {
                "diff.patch": diff_of({"a.py": (3, 1)}),
                "feature.md": "omitted: diff.patch cut to nothing",
            }
        ),
    )
    assert diff_was_cut(cut) is True and diff_was_cut(whole) is False
