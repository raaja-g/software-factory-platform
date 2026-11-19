import subprocess
from pathlib import Path

from swfactory.graph.steps import instruction_cleanup_argv
from swfactory.logging import safe_error


def _touch(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_cleanup_removes_every_instruction_file_and_config_dir(tmp_path):
    for rel in (
        "AGENTS.md", "sub/agents.MD", "CLAUDE.md", "CLAUDE.local.md", "a/b/GEMINI.md",
        "opencode.json", "x/OpenCode.jsonc", ".claude/settings.json", "pkg/.opencode/agent/a.md",
        ".gemini/.env", "weird\nname/CLAUDE.md", ".git/CLAUDE.md",
        "src/app.py", "docs/notes.md",
    ):  # fmt: skip
        _touch(tmp_path, rel)
    done = subprocess.run(instruction_cleanup_argv(str(tmp_path)), capture_output=True, text=True)
    assert done.returncode == 0
    left = sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file())
    assert left == [".git/CLAUDE.md", "docs/notes.md", "src/app.py"]  # .git is never touched


def test_safe_error_never_carries_the_request_url():
    class Resp:
        status_code = 500

    class Boom(Exception):
        response = Resp()

    text = safe_error(Boom("GET http://gateway/key/info?key=sk-secret1234567890abcdef failed"))
    assert text == "Boom (HTTP 500)" and "sk-" not in text
    assert safe_error(ValueError("sk-secret1234567890abcdef")) == "ValueError"
