"""Read a markdown backlog file: only open ``- [ ]`` items are work."""

import re
from dataclasses import dataclass

_OPEN = re.compile(r"^\s*[-*+]\s+\[ \]\s+(\S.*?)\s*$")


@dataclass(frozen=True, slots=True)
class BacklogItem:
    line: int
    text: str


def parse_backlog(text: str) -> list[BacklogItem]:
    items: list[BacklogItem] = []
    for n, raw in enumerate(text.splitlines(), start=1):
        m = _OPEN.match(raw)
        if m:
            items.append(BacklogItem(n, m.group(1)))
    return items
