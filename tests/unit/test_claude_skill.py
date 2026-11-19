import re
from pathlib import Path

import yaml
from typer.testing import CliRunner

from swfactory.cli.main import app

SKILL = Path(__file__).parents[2] / "claude-skill" / "factory" / "SKILL.md"


def frontmatter(path: Path) -> dict:
    return yaml.safe_load(path.read_text().split("---")[1])


def skill_command_lines() -> list[list[str]]:
    out = []
    for m in re.finditer(r"`factory ([^`]+)`", SKILL.read_text()):
        argv = []
        for tok in m.group(1).split():
            if tok[0] in '<-["$':
                break
            argv.append(tok)
        out.append(argv)
    return out


def test_skill_commands_exist():
    lines = skill_command_lines()
    assert len(lines) >= 14
    for argv in lines:
        r = CliRunner().invoke(app, [*argv, "--help"])
        assert r.exit_code == 0, (argv, r.output)


def test_skill_frontmatter():
    fm = frontmatter(SKILL)
    assert fm["name"] == "factory" and "argument-hint" in fm
