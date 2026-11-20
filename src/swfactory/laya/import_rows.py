"""Import synthetic training rows for one router question (M11.10)."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Collection
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from swfactory.db.tables import LayaDataset, LayaImportedRow
from swfactory.definition.models import Definition
from swfactory.laya.export import question_ids
from swfactory.models.triage import TOUCHES_UI_LABELS, TRIAGE_QUESTIONS

SOURCE_NAME_RE = r"[a-z0-9][a-z0-9._-]{0,39}"
MAX_IMPORT_BYTES = 64 * 1024 * 1024


class ImportRefused(Exception):
    """The file was not imported; nothing was stored."""


def question_labels(defn: Definition, question: str) -> set[str]:
    """The labels Laya is asked to choose between for ``question``."""
    if question == "touches_ui":
        return set(TOUCHES_UI_LABELS)
    if question in TRIAGE_QUESTIONS:
        return set(TRIAGE_QUESTIONS[question]["criteria"])
    labels: set[str] = set()
    for profile in defn.profiles.values():
        for role, rp in profile.roles.items():
            if f"tier.{role.value}" == question:
                labels |= {t.label for t in rp.tiers or []}
    return labels


def _parse(raw: bytes, question: str, labels: Collection[str]) -> list[tuple[str, str]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise ImportRefused("refused: the file is not UTF-8 text") from e
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        header = next(reader)
    except StopIteration:
        raise ImportRefused(f"refused: header must be state,{question}") from None
    except csv.Error as e:
        raise ImportRefused(f"refused: line 1: {e}") from None
    if header != ["state", question]:
        raise ImportRefused(f"refused: header must be state,{question}")
    allowed = ", ".join(sorted(labels))
    rows: list[tuple[str, str]] = []
    try:
        for row in reader:
            line = reader.line_num
            if len(row) != 2:
                raise ImportRefused(f"refused: line {line}: expected 2 columns, got {len(row)}")
            state, label = row
            try:
                ok = isinstance(json.loads(state), dict)
            except ValueError:
                ok = False
            if not ok:
                raise ImportRefused(f"refused: line {line}: state is not a JSON object")
            if label not in labels:
                raise ImportRefused(f"refused: line {line}: label {label} is not one of {allowed}")
            rows.append((state, label))
    except csv.Error as e:
        raise ImportRefused(f"refused: line {reader.line_num}: {e}") from None
    if not rows:
        raise ImportRefused("refused: the file has no rows")
    return rows


def read_import_file(path: Path, question: str, labels: Collection[str]) -> list[tuple[str, str]]:
    """Check the whole file; returns (state, label) rows or raises ImportRefused."""
    return _parse(_read_bytes(path), question, labels)


def _read_bytes(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise ImportRefused("refused: the file is larger than 64 MiB")
        return path.read_bytes()
    except OSError as e:
        raise ImportRefused(f"refused: cannot read {path}: {e.strerror or e}") from None


async def import_rows(
    s: AsyncSession,
    defn: Definition,
    *,
    question: str,
    path: Path,
    source: str,
    origin_url: str,
    licence: str,
    imported_by: str,
) -> LayaDataset:
    if question not in question_ids(defn):
        raise ImportRefused(f"refused: unknown question {question}")
    if not re.fullmatch(SOURCE_NAME_RE, source):
        raise ImportRefused(f"refused: source name must match {SOURCE_NAME_RE}")
    if not origin_url.strip() or not licence.strip():
        raise ImportRefused("refused: --origin and --licence must not be empty")
    raw = _read_bytes(path)
    rows = _parse(raw, question, question_labels(defn, question))
    sha = hashlib.sha256(raw).hexdigest()
    prior = await s.scalar(select(LayaDataset.name).where(LayaDataset.sha256 == sha))
    if prior is not None:
        raise ImportRefused(f"refused: this file is already imported as {prior}")
    taken = await s.scalar(
        select(LayaDataset.id).where(LayaDataset.question == question, LayaDataset.name == source)
    )
    if taken is not None:
        raise ImportRefused(f"refused: a dataset named {source} already exists for {question}")
    ds = LayaDataset(
        name=source,
        question=question,
        origin_url=origin_url.strip(),
        licence=licence.strip(),
        rows=len(rows),
        sha256=sha,
        imported_by=imported_by,
    )
    s.add(ds)
    await s.flush()
    s.add_all(
        LayaImportedRow(dataset_id=ds.id, question=question, state=st, label=lb) for st, lb in rows
    )
    await s.flush()
    return ds
