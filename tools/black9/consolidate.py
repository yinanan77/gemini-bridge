#!/usr/bin/env python3
"""Black9 Notion<->Obsidian consolidation builder (Python stdlib only).

Parses a source analysis Markdown document containing `# 附录A` (108 group
analyses across `## C01`..`## C11`) followed by `# 附录B` (11 case sections
`## C01` through `## C11` with exact unit counts
[23, 22, 22, 22, 37, 37, 30, 26, 16, 49, 51] = 335), plus an optional
Black9-only supplement Markdown (`--supplement`, 11x4 = 44 items under
`## 二、黑桃11版本逐段补研究` stopping at `## 三、思远11版本逐段补研究`), and
deterministically builds a standalone Obsidian/Notion-friendly folder
(`黑桃9｜文稿与分析汇总`) using simple flat headings (H1-H3) and paragraphs
without blank lines.

Also provides an optional `fetch` subcommand that calls `minis-mcp-cli`
for `notion-fetch` and saves both raw evidence and extracted body text
without reading credentials or logging private payloads.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DEFAULT_FOLDER_NAME = "黑桃9｜文稿与分析汇总"
EXPECTED_CASE_IDS: tuple[str, ...] = tuple(f"C{i:02d}" for i in range(1, 12))
EXPECTED_COUNTS: tuple[int, ...] = (23, 22, 22, 22, 37, 37, 30, 26, 16, 49, 51)
EXPECTED_TOTAL_UNITS: int = 335
EXPECTED_TOTAL_GROUPS: int = 108
EXPECTED_GROUP_COLUMNS: int = 7
EXPECTED_SUPPLEMENT_ITEMS_PER_CASE: int = 4
EXPECTED_TOTAL_SUPPLEMENT_ITEMS: int = 44
MAX_MD_LINE_CHARS: int = 1850  # Strictly < 1900 chars per line
HARD_MD_LINE_LIMIT: int = 1900
MANIFEST_FILENAME = "manifest.json"
INDEX_FILENAME = "00_index.md"
REFERENCE_FILENAME = "90_source_reference.md"

DEFAULT_GROUP_HEADERS: tuple[str, ...] = (
    "组别编号",
    "核心机制",
    "读前状态",
    "本组变化",
    "位置与原因",
    "下组期待",
    "删掉损失",
)

_APPENDIX_A_RE = re.compile(r"^#\s+附录\s*A(?:\b|[\s｜|：:\-—（(].*)?$")
_APPENDIX_B_RE = re.compile(r"^#\s+附录\s*B(?:\b|[\s｜|：:\-—（(].*)?$")
_TOP_HEADING_RE = re.compile(r"^#\s+\S")
_H2_HEADING_RE = re.compile(r"^##\s+\S")
_H3_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")
_CASE_HEADING_RE = re.compile(
    r"^##\s+(C(?:0[1-9]|1[01]))(?:\s*[｜|：:\-—]\s*(.*)|\s+(.*))?\s*$"
)
_SUPPLEMENT_START_RE = re.compile(
    r"^##\s+二[、.．\s]*黑桃11版本逐段补研究(?:[\s｜|：:\-—（(].*)?$"
)
_SUPPLEMENT_STOP_RE = re.compile(
    r"^##\s+三[、.．\s]*思远11版本逐段补研究(?:[\s｜|：:\-—（(].*)?$"
)
_SUPPLEMENT_CASE_HEADING_RE = re.compile(
    r"^###\s+(C(?:0[1-9]|1[01]))(?:\s*[｜|：:\-—]\s*(.*)|\s+(.*))?\s*$"
)
_SUPPLEMENT_ITEM_RE = re.compile(
    r"^(?:\*\*([1-9]\d*)\s*[.．、]\*\*\s*(.+?)|(\*\*[1-9]\d*(?:\s*[.．]\s+|\s*、\s*).+?)|([1-9]\d*)(?:\s*[.．]\s+|\s*、\s*)(.+?))\s*$"
)
_PRE_HEADING_RE = re.compile(r"^(#{1,3})\s+(.+?)\s*$")
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{2,}:?$")

# Conservative markers for explicit visual / non-spoken / mixed records (never infers speech).
_EXPLICIT_VISUAL_EXACT = frozenset(
    {
        "",
        "-",
        "—",
        "——",
        "/",
        "无",
        "无对白",
        "无台词",
        "无口播",
        "（无）",
        "(无)",
        "（无对白）",
        "(无对白)",
        "【无对白】",
        "[无对白]",
        "纯画面",
        "空镜",
        "表情停顿",
        "成品尾留",
        "无底部解释性字幕",
        "无解释性字幕",
        "近景",
        "反应近景",
    }
)
_EXPLICIT_VISUAL_CUE_RE = re.compile(
    r"(无底部解释性字幕|无解释性字幕|无底部字幕|无对白|无台词|无口播|"
    r"纯画面|仅画面|画面[：:]|\[画面|【画面|（画面|\(画面|空镜|转场画面|"
    r"表情停顿|成品尾留|反应近景|近景|特写|仅字幕|小字[“\"「『：:])"
)
_PURE_BRACKET_VISUAL_RE = re.compile(
    r"^(?:[\[\【（(][^\]\】）)]*(?:画面|无对白|无台词|无口播|空镜|转场|字幕|花字|近景|表情停顿|尾留)[^\]\】）)]*[\]\】）)]\s*)+$"
)
_SECRET_REDACT_RE = re.compile(
    r"(Bearer\s+[A-Za-z0-9._\-]+|secret_[A-Za-z0-9_\-]+|ntn_[A-Za-z0-9_\-]+|"
    r"(?:api[_-]?key|token|authorization|password)\s*[:=]\s*['\"]?[^\s'\"]+['\"]?)",
    re.IGNORECASE,
)


class ConsolidateError(Exception):
    """Base error for consolidation operations."""


class PathSafetyError(ConsolidateError):
    """Raised when a path fails symlink, traversal, or non-overwrite checks."""


class SourceParseError(ConsolidateError):
    """Raised when source or supplement markdown fails structure or count validation."""


class McpFetchError(ConsolidateError):
    """Raised when minis-mcp-cli notion-fetch fails or returns malformed data."""


@dataclass(frozen=True)
class UnitRow:
    index: int
    unit_time: str
    original_text: str
    analysis: str
    semantic_group: str
    classification: str  # "source_text_unverified" | "visual_or_non_spoken" | "mixed_visual_and_text"


@dataclass(frozen=True)
class GroupRow:
    index: int
    group_id: str
    mechanisms: str
    read_before: str
    change: str
    position_reason: str
    next_expectation: str
    deletion_loss: str


@dataclass(frozen=True)
class SupplementItem:
    number: int
    text: str


@dataclass(frozen=True)
class SupplementCase:
    case_id: str
    title: str
    raw_heading: str
    intro_lines: tuple[str, ...]
    items: tuple[SupplementItem, ...]


@dataclass(frozen=True)
class ParsedSupplement:
    section_heading: str
    stopped_at_heading: str
    cases: tuple[SupplementCase, ...]


@dataclass(frozen=True)
class CaseSection:
    case_id: str
    title: str
    raw_heading: str
    rows: tuple[UnitRow, ...]
    groups: tuple[GroupRow, ...] = ()
    group_headers: tuple[str, ...] = DEFAULT_GROUP_HEADERS


@dataclass(frozen=True)
class ParsedSource:
    pre_appendix_headings: tuple[str, ...]
    cases: tuple[CaseSection, ...]


def redact_secrets(text: str) -> str:
    """Redact common token/credential patterns from error messages."""
    return _SECRET_REDACT_RE.sub("[REDACTED]", text)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def check_no_symlink_chain(path: Path, stop_at: Path | None = None) -> None:
    """Reject symlink at `path` or any component between `stop_at` and `path`.

    When `stop_at` is None, checks `path` itself and any non-system parent
    segments only if `path` is relative, plus `path.is_symlink()`.
    """
    if path.is_symlink():
        raise PathSafetyError(f"Refusing symlink path: {path}")
    if stop_at is not None:
        if stop_at.is_symlink():
            raise PathSafetyError(f"Refusing symlink root path: {stop_at}")
        root_resolved = stop_at.resolve()
        try:
            rel = path.relative_to(stop_at)
        except ValueError:
            try:
                rel = path.resolve().relative_to(root_resolved)
            except ValueError as exc:
                raise PathSafetyError(
                    f"Path escapes allowed root directory: {path} (root: {stop_at})"
                ) from exc
        if ".." in rel.parts:
            raise PathSafetyError(f"Path traversal segment rejected: {path}")
        cur = stop_at
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                raise PathSafetyError(f"Refusing symlink component: {cur}")
        resolved = path.resolve()
        try:
            resolved.relative_to(root_resolved)
        except ValueError as exc:
            raise PathSafetyError(
                f"Resolved path escapes root directory: {path} -> {resolved}"
            ) from exc


def ensure_safe_file_read(path: Path) -> None:
    """Reject symlinked input file or directory."""
    if path.is_symlink():
        raise PathSafetyError(f"Refusing symlink input path: {path}")
    if not path.exists():
        raise ConsolidateError(f"Input file does not exist: {path}")
    if not path.is_file():
        raise ConsolidateError(f"Input path is not a regular file: {path}")


def atomic_write_bytes(path: Path, data: bytes, *, root: Path | None = None) -> None:
    """Atomically write bytes after verifying symlink safety."""
    check_no_symlink_chain(path, stop_at=root)
    parent = path.parent
    if parent.is_symlink():
        raise PathSafetyError(f"Refusing symlink parent directory: {parent}")
    parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise PathSafetyError(f"Refusing to overwrite symlink: {path}")

    fd, tmp_name = tempfile.mkstemp(prefix=".tmp_black9_", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def classify_original_cell(cell_text: str) -> str:
    """Conservatively separate unverified source text from explicit visual/mixed records.

    Never claims unmarked source text is verified speech; never guesses or rewrites content.
    """
    stripped = cell_text.strip()
    if stripped in _EXPLICIT_VISUAL_EXACT:
        return "visual_or_non_spoken"
    if _PURE_BRACKET_VISUAL_RE.match(stripped):
        return "visual_or_non_spoken"
    if _EXPLICIT_VISUAL_CUE_RE.search(stripped):
        # Check if removing bracketed visual markers and explicit visual cues leaves other text.
        without_brackets = re.sub(r"[\[\【（(][^\]\】）)]*[\]\】）)]", "", stripped).strip()
        without_cues = _EXPLICIT_VISUAL_CUE_RE.sub("", without_brackets)
        remainder = without_cues.strip(" ：:，,。.;；-—/\\的与和及")
        if not remainder:
            return "visual_or_non_spoken"
        return "mixed_visual_and_text"
    return "source_text_unverified"


def is_visual_or_mixed(classification: str) -> bool:
    return classification != "source_text_unverified"


def classification_label_zh(classification: str) -> str:
    if classification == "visual_or_non_spoken":
        return "显式画面或无解释性字幕记录（依原文显式标注归类，未作复听定性）"
    if classification == "mixed_visual_and_text":
        return "画面与文字混合或待确认记录（依原文显式标注归类，不推断为纯口播）"
    return "已存文字来源记录（按原文保留，未经重新听审，不作口播定性）"


def split_markdown_table_row(line: str) -> list[str]:
    """Split a Markdown pipe table row respecting escaped pipes `\\|`."""
    text = line.strip()
    if not text or "|" not in text:
        return []

    cells: list[str] = []
    buf: list[str] = []
    i = 0
    length = len(text)
    while i < length:
        ch = text[i]
        if ch == "\\" and i + 1 < length and text[i + 1] == "|":
            buf.append("|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))

    if text.startswith("|") and cells:
        cells = cells[1:]
    if text.endswith("|") and not text.endswith("\\|") and cells:
        cells = cells[:-1]

    return [c.strip() for c in cells]


def is_table_separator_row(cells: Sequence[str]) -> bool:
    if len(cells) < 2:
        return False
    return all(bool(_TABLE_SEP_CELL_RE.match(c.strip())) for c in cells)


def parse_case_table(section_lines: Sequence[str], case_id: str) -> list[UnitRow]:
    """Extract all Markdown table data rows from an Appendix B case section."""
    idx = 0
    total = len(section_lines)
    table_found = False
    rows: list[UnitRow] = []

    while idx < total:
        line = section_lines[idx]
        if "|" in line and idx + 1 < total and "|" in section_lines[idx + 1]:
            header_cells = split_markdown_table_row(line)
            sep_cells = split_markdown_table_row(section_lines[idx + 1])
            if (
                len(header_cells) == 4
                and len(sep_cells) == len(header_cells)
                and is_table_separator_row(sep_cells)
            ):
                table_found = True
                idx += 2
                row_num = len(rows)
                while idx < total:
                    data_line = section_lines[idx]
                    stripped = data_line.strip()
                    if not stripped or "|" not in stripped:
                        break
                    cells = split_markdown_table_row(data_line)
                    if is_table_separator_row(cells):
                        idx += 1
                        continue
                    if len(cells) != 4:
                        raise SourceParseError(
                            f"{case_id}: expected 4 table columns on row {row_num + 1}, "
                            f"got {len(cells)}"
                        )
                    row_num += 1
                    unit_time, original_text, analysis, semantic_group = cells
                    if not unit_time:
                        raise SourceParseError(
                            f"{case_id}: empty unit/time identifier on row {row_num}"
                        )
                    rows.append(
                        UnitRow(
                            index=row_num,
                            unit_time=unit_time,
                            original_text=original_text,
                            analysis=analysis,
                            semantic_group=semantic_group,
                            classification=classify_original_cell(original_text),
                        )
                    )
                    idx += 1
                continue
        idx += 1

    if not table_found:
        raise SourceParseError(f"{case_id}: no valid 4-column Markdown pipe table found")
    return rows


def parse_appendix_a_case_table(
    section_lines: Sequence[str], case_id: str
) -> tuple[tuple[str, ...], list[GroupRow]]:
    """Extract all 7-column Appendix A group rows from a case section losslessly."""
    idx = 0
    total = len(section_lines)
    table_found = False
    headers: tuple[str, ...] = DEFAULT_GROUP_HEADERS
    rows: list[GroupRow] = []

    while idx < total:
        line = section_lines[idx]
        if "|" in line and idx + 1 < total and "|" in section_lines[idx + 1]:
            header_cells = split_markdown_table_row(line)
            sep_cells = split_markdown_table_row(section_lines[idx + 1])
            if (
                len(header_cells) == EXPECTED_GROUP_COLUMNS
                and len(sep_cells) == EXPECTED_GROUP_COLUMNS
                and is_table_separator_row(sep_cells)
            ):
                table_found = True
                headers = tuple(
                    cell if cell else DEFAULT_GROUP_HEADERS[col_i]
                    for col_i, cell in enumerate(header_cells)
                )
                idx += 2
                row_num = len(rows)
                while idx < total:
                    data_line = section_lines[idx]
                    stripped = data_line.strip()
                    if not stripped or "|" not in stripped:
                        break
                    cells = split_markdown_table_row(data_line)
                    if is_table_separator_row(cells):
                        idx += 1
                        continue
                    if len(cells) != EXPECTED_GROUP_COLUMNS:
                        raise SourceParseError(
                            f"Appendix A {case_id}: expected {EXPECTED_GROUP_COLUMNS} table columns "
                            f"on row {row_num + 1}, got {len(cells)}"
                        )
                    row_num += 1
                    (
                        group_id,
                        mechanisms,
                        read_before,
                        change,
                        position_reason,
                        next_expectation,
                        deletion_loss,
                    ) = cells
                    if not group_id:
                        raise SourceParseError(
                            f"Appendix A {case_id}: empty groupID on row {row_num}"
                        )
                    rows.append(
                        GroupRow(
                            index=row_num,
                            group_id=group_id,
                            mechanisms=mechanisms,
                            read_before=read_before,
                            change=change,
                            position_reason=position_reason,
                            next_expectation=next_expectation,
                            deletion_loss=deletion_loss,
                        )
                    )
                    idx += 1
                continue
        idx += 1

    if not table_found:
        raise SourceParseError(
            f"Appendix A {case_id}: no valid {EXPECTED_GROUP_COLUMNS}-column Markdown pipe table found"
        )
    return headers, rows


def _extract_case_sections(
    appendix_lines: Sequence[str], label: str
) -> list[tuple[str, str, str, list[str]]]:
    """Split an appendix block into ordered (case_id, title, raw_heading, body_lines) tuples."""
    section_starts: list[tuple[int, str, str, str]] = []
    for idx, line in enumerate(appendix_lines):
        stripped = line.strip()
        m = _CASE_HEADING_RE.match(stripped)
        if m:
            case_id = m.group(1)
            title = (m.group(2) or m.group(3) or "").strip()
            section_starts.append((idx, case_id, title, stripped))

    if not section_starts:
        raise SourceParseError(f"No `## C01`..`## C11` sections found under `{label}`")

    out: list[tuple[str, str, str, list[str]]] = []
    for pos, (start_idx, case_id, title, raw_heading) in enumerate(section_starts):
        end_idx = (
            section_starts[pos + 1][0]
            if pos + 1 < len(section_starts)
            else len(appendix_lines)
        )
        body_lines = list(appendix_lines[start_idx + 1 : end_idx])
        out.append((case_id, title or case_id, raw_heading, body_lines))
    return out


def parse_source_markdown(text: str) -> ParsedSource:
    """Parse source analysis Markdown and extract Appendix A (108 groups) + Appendix B (335 units)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    appendix_a_idx: int | None = None
    appendix_b_idx: int | None = None
    pre_headings: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if appendix_a_idx is None and _APPENDIX_A_RE.match(stripped):
            appendix_a_idx = i
            continue
        if appendix_b_idx is None and _APPENDIX_B_RE.match(stripped):
            appendix_b_idx = i
            break
        if appendix_a_idx is None:
            m = _PRE_HEADING_RE.match(stripped)
            if m:
                pre_headings.append(m.group(2).strip())

    if appendix_a_idx is None:
        raise SourceParseError("Source Markdown missing required `# 附录A` heading")
    if appendix_b_idx is None:
        raise SourceParseError("Source Markdown missing required `# 附录B` heading")
    if appendix_a_idx >= appendix_b_idx:
        raise SourceParseError("`# 附录A` must appear before `# 附录B` in source Markdown")

    # Collect Appendix A lines until next top-level `# ` heading (e.g. `# 附录B`).
    appendix_a_lines: list[str] = []
    for line in lines[appendix_a_idx + 1 : appendix_b_idx]:
        if _TOP_HEADING_RE.match(line.strip()):
            break
        appendix_a_lines.append(line)

    # Collect Appendix B lines until next top-level `# ` heading (if any).
    appendix_b_lines: list[str] = []
    for line in lines[appendix_b_idx + 1 :]:
        if _TOP_HEADING_RE.match(line.strip()):
            break
        appendix_b_lines.append(line)

    raw_a_sections = _extract_case_sections(appendix_a_lines, "# 附录A")
    a_ids = tuple(cid for cid, _, _, _ in raw_a_sections)
    if a_ids != EXPECTED_CASE_IDS:
        raise SourceParseError(
            f"Appendix A case IDs mismatch: expected {list(EXPECTED_CASE_IDS)}, got {list(a_ids)}"
        )

    groups_by_case: dict[str, tuple[tuple[str, ...], tuple[GroupRow, ...]]] = {}
    for case_id, _title, _raw_heading, body_lines in raw_a_sections:
        headers, group_rows = parse_appendix_a_case_table(body_lines, case_id)
        if not group_rows:
            raise SourceParseError(f"Appendix A {case_id}: table has 0 group rows")
        groups_by_case[case_id] = (headers, tuple(group_rows))

    raw_b_sections = _extract_case_sections(appendix_b_lines, "# 附录B")
    cases: list[CaseSection] = []
    for case_id, title, raw_heading, body_lines in raw_b_sections:
        rows = parse_case_table(body_lines, case_id)
        group_headers, group_rows = groups_by_case.get(case_id, (DEFAULT_GROUP_HEADERS, ()))
        cases.append(
            CaseSection(
                case_id=case_id,
                title=title or case_id,
                raw_heading=raw_heading,
                rows=tuple(rows),
                groups=group_rows,
                group_headers=group_headers,
            )
        )

    return ParsedSource(
        pre_appendix_headings=tuple(pre_headings),
        cases=tuple(cases),
    )


def validate_parsed_source(parsed: ParsedSource) -> None:
    """Strictly enforce exact 11 variants (C01..C11), 335 units, and 108 Appendix A groups."""
    found_ids = tuple(c.case_id for c in parsed.cases)
    if len(found_ids) != len(EXPECTED_CASE_IDS):
        raise SourceParseError(
            f"Expected exactly {len(EXPECTED_CASE_IDS)} case variants, found {len(found_ids)}: {list(found_ids)}"
        )
    if found_ids != EXPECTED_CASE_IDS:
        raise SourceParseError(
            f"Case IDs mismatch: expected {list(EXPECTED_CASE_IDS)}, got {list(found_ids)}"
        )

    actual_counts = tuple(len(c.rows) for c in parsed.cases)
    if actual_counts != EXPECTED_COUNTS:
        diffs = [
            f"{cid}: got {got}, expected {exp}"
            for cid, got, exp in zip(EXPECTED_CASE_IDS, actual_counts, EXPECTED_COUNTS)
            if got != exp
        ]
        raise SourceParseError(
            f"Per-case unit counts mismatch ({'; '.join(diffs)}); "
            f"actual={list(actual_counts)}, expected={list(EXPECTED_COUNTS)}"
        )

    total = sum(actual_counts)
    if total != EXPECTED_TOTAL_UNITS:
        raise SourceParseError(
            f"Total unit count mismatch: got {total}, expected {EXPECTED_TOTAL_UNITS}"
        )

    missing_groups = [c.case_id for c in parsed.cases if not c.groups]
    if missing_groups:
        raise SourceParseError(
            f"Missing Appendix A group records for case(s): {missing_groups}"
        )

    total_groups = sum(len(c.groups) for c in parsed.cases)
    if total_groups != EXPECTED_TOTAL_GROUPS:
        raise SourceParseError(
            f"Appendix A total group count mismatch: got {total_groups}, "
            f"expected {EXPECTED_TOTAL_GROUPS} (per-case={[len(c.groups) for c in parsed.cases]})"
        )


def parse_supplement_markdown(text: str) -> ParsedSupplement:
    """Parse optional Black9-only supplement Markdown from Notion 22-page body.

    Reads `## 二、黑桃11版本逐段补研究` through eleven `### C01`..`### C11` sections,
    strictly stopping at `## 三、思远11版本逐段补研究` (or any next `##`/`#` heading).
    Never copies S01/Siyuan or user-final-script sections. Preserves `<br>` and rich markup literally.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")

    start_idx: int | None = None
    section_heading = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if _SUPPLEMENT_START_RE.match(stripped):
            start_idx = i + 1
            section_heading = stripped
            break

    if start_idx is None:
        raise SourceParseError(
            "Supplement Markdown missing required `## 二、黑桃11版本逐段补研究` heading"
        )

    stopped_at_heading = ""
    section_lines: list[str] = []
    for line in lines[start_idx:]:
        stripped = line.strip()
        if _SUPPLEMENT_STOP_RE.match(stripped):
            stopped_at_heading = stripped
            break
        if _H2_HEADING_RE.match(stripped) or _TOP_HEADING_RE.match(stripped):
            stopped_at_heading = stripped
            break
        section_lines.append(line)

    # Identify all ### headings inside Black9 section; reject any S01/Siyuan/unexpected ### heading.
    case_starts: list[tuple[int, str, str, str]] = []
    for idx, line in enumerate(section_lines):
        stripped = line.strip()
        if stripped.startswith("### "):
            m = _SUPPLEMENT_CASE_HEADING_RE.match(stripped)
            if not m:
                raise SourceParseError(
                    f"Unexpected `###` heading inside Black9 supplement section: {stripped}"
                )
            case_id = m.group(1)
            title = (m.group(2) or m.group(3) or "").strip()
            case_starts.append((idx, case_id, title, stripped))

    if not case_starts:
        raise SourceParseError(
            "No `### C01`..`### C11` sections found under `## 二、黑桃11版本逐段补研究`"
        )

    cases: list[SupplementCase] = []
    for pos, (c_start, case_id, title, raw_heading) in enumerate(case_starts):
        c_end = (
            case_starts[pos + 1][0]
            if pos + 1 < len(case_starts)
            else len(section_lines)
        )
        body_lines = section_lines[c_start + 1 : c_end]

        intro_lines: list[str] = []
        items_acc: list[tuple[int, list[str]]] = []
        for raw_line in body_lines:
            # Top-level numbered items start at column 0 (no leading indentation).
            if raw_line and not raw_line[0].isspace():
                im = _SUPPLEMENT_ITEM_RE.match(raw_line)
                if im:
                    if im.group(1):
                        item_num = int(im.group(1))
                        item_body = im.group(2)
                    elif im.group(3):
                        num_m = re.match(r"^\*\*([1-9]\d*)", im.group(3))
                        assert num_m is not None
                        item_num = int(num_m.group(1))
                        item_body = im.group(3)
                    else:
                        item_num = int(im.group(4))
                        item_body = im.group(5)
                    items_acc.append((item_num, [item_body]))
                    continue
            if not items_acc:
                if raw_line.strip():
                    intro_lines.append(raw_line.rstrip(" \t"))
            else:
                if raw_line.strip():
                    items_acc[-1][1].append(raw_line.rstrip(" \t"))

        parsed_items = tuple(
            SupplementItem(number=num, text="\n".join(parts))
            for num, parts in items_acc
        )
        cases.append(
            SupplementCase(
                case_id=case_id,
                title=title or case_id,
                raw_heading=raw_heading,
                intro_lines=tuple(intro_lines),
                items=parsed_items,
            )
        )

    return ParsedSupplement(
        section_heading=section_heading,
        stopped_at_heading=stopped_at_heading,
        cases=tuple(cases),
    )


def validate_parsed_supplement(supplement: ParsedSupplement) -> None:
    """Strictly enforce 11 Black9 cases (C01..C11) x 4 numbered items = 44 total."""
    found_ids = tuple(c.case_id for c in supplement.cases)
    if found_ids != EXPECTED_CASE_IDS:
        raise SourceParseError(
            f"Supplement case IDs mismatch: expected {list(EXPECTED_CASE_IDS)}, got {list(found_ids)}"
        )

    expected_nums = tuple(range(1, EXPECTED_SUPPLEMENT_ITEMS_PER_CASE + 1))
    total_items = 0
    for case in supplement.cases:
        nums = tuple(item.number for item in case.items)
        if nums != expected_nums:
            raise SourceParseError(
                f"Supplement {case.case_id}: expected numbered items {list(expected_nums)}, "
                f"got {list(nums)}"
            )
        for item in case.items:
            if not item.text.strip():
                raise SourceParseError(
                    f"Supplement {case.case_id}: numbered item {item.number} is empty"
                )
        total_items += len(case.items)

    if total_items != EXPECTED_TOTAL_SUPPLEMENT_ITEMS:
        raise SourceParseError(
            f"Supplement total item count mismatch: got {total_items}, "
            f"expected {EXPECTED_TOTAL_SUPPLEMENT_ITEMS}"
        )


def _split_long_text(text: str, max_chunk: int) -> list[str]:
    """Split text into chunks <= max_chunk chars without dropping a single character."""
    if max_chunk < 64:
        raise ValueError("max_chunk must be >= 64")
    # Preserve literal content; split on existing newlines first so no single line exceeds limit.
    parts: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        remaining = raw_line
        while len(remaining) > max_chunk:
            window = remaining[:max_chunk]
            cut = -1
            for delim in ("。", "！", "？", "；", "，", "、", ". ", "! ", "? ", "; ", ", ", " "):
                pos = window.rfind(delim)
                if pos >= max_chunk // 3:
                    cut = pos + len(delim)
                    break
            if cut <= 0:
                cut = max_chunk
            parts.append(remaining[:cut])
            remaining = remaining[cut:]
        parts.append(remaining)
    return parts


_DISALLOWED_RAW_LINE_START_RE = re.compile(
    r"^(?:#{1,6}\s+|```|~~~|\||>|---|___|\*\*\*|[-*+]\s+|\d+\.\s+|    |\t)"
)


def emit_prefixed_paragraphs(
    prefix: str, cont_prefix: str, text: str, *, max_line: int = MAX_MD_LINE_CHARS
) -> list[str]:
    """Emit flat paragraphs with safe prefix so every line is < 1900 chars and never a special block."""
    budget = max_line - max(len(prefix), len(cont_prefix))
    chunks = _split_long_text(text, budget)
    paragraphs: list[str] = []
    for i, chunk in enumerate(chunks):
        if i > 0 and not chunk.strip():
            continue
        lead = prefix if i == 0 else cont_prefix
        paragraphs.append(f"{lead}{chunk}")
    return paragraphs


def emit_clean_reading_paragraphs(
    text: str, unit_time: str, *, max_line: int = MAX_MD_LINE_CHARS
) -> list[str]:
    """Emit literal continuous original lines without inventing transitions or punctuation."""
    guard_prefix = f"原行（{unit_time}）："
    chunks = _split_long_text(text, max_line - len(guard_prefix))
    paragraphs: list[str] = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        if _DISALLOWED_RAW_LINE_START_RE.match(chunk):
            paragraphs.append(f"{guard_prefix}{chunk}")
        else:
            paragraphs.append(chunk)
    return paragraphs


def validate_flat_markdown(
    content: str, filename: str, *, allow_blank_lines: bool = False
) -> int:
    """Validate generated Markdown has only flat H1-H3 headings/paragraphs and all lines < 1900 chars."""
    if not allow_blank_lines:
        if not content.endswith("\n") or content.endswith("\n\n"):
            raise ConsolidateError(
                f"{filename}: normalized flat Markdown must end with a single trailing newline"
            )
        lines = content[:-1].split("\n")
    else:
        lines = content.split("\n")

    max_len = 0
    for lineno, line in enumerate(lines, start=1):
        line_len = len(line)
        if line_len > max_len:
            max_len = line_len
        if line_len >= HARD_MD_LINE_LIMIT:
            raise ConsolidateError(
                f"{filename}:{lineno} exceeds line limit ({line_len} >= {HARD_MD_LINE_LIMIT})"
            )
        stripped = line.strip()
        if not stripped:
            if not allow_blank_lines:
                raise ConsolidateError(
                    f"{filename}:{lineno} contains disallowed blank line in normalized flat Markdown"
                )
            continue
        if re.match(r"^#{4,}\s+", stripped):
            raise ConsolidateError(
                f"{filename}:{lineno} contains unsupported heading level (only H1-H3 allowed)"
            )
        if stripped.startswith(("```", "~~~", "|", ">", "---", "***", "___")):
            raise ConsolidateError(
                f"{filename}:{lineno} contains disallowed Markdown block syntax"
            )
        if re.match(r"^(?:[-*+]\s+|\d+\.\s+)", stripped):
            raise ConsolidateError(
                f"{filename}:{lineno} contains disallowed list syntax"
            )
        if line.startswith(("    ", "\t")):
            raise ConsolidateError(
                f"{filename}:{lineno} contains disallowed indented/nested block syntax"
            )
    return max_len


def case_filename(case_id: str) -> str:
    num = int(case_id[1:])
    return f"{num:02d}_{case_id}.md"


def render_case_markdown(
    case: CaseSection,
    *,
    source_label: str,
    source_sha256: str,
    source_urls: Sequence[str],
    supplement_case: SupplementCase | None = None,
    supplement_label: str | None = None,
    supplement_sha256: str | None = None,
    supplement_urls: Sequence[str] = (),
) -> str:
    """Render one Cxx case document using flat H1-H3 headings and paragraphs without blank lines."""
    source_text_rows = [r for r in case.rows if not is_visual_or_mixed(r.classification)]
    visual_mixed_rows = [r for r in case.rows if is_visual_or_mixed(r.classification)]

    blocks: list[str] = []
    blocks.append(f"# {case.case_id}｜{case.title}")

    blocks.append("## 来源与核验说明")
    blocks.append(f"案例编号：{case.case_id}")
    blocks.append(f"案例标题：{case.title}")
    blocks.append(
        f"单元总数：{len(case.rows)}（已存文字来源记录 {len(source_text_rows)} 条，显式画面或混合记录 {len(visual_mixed_rows)} 条）"
    )
    blocks.append(f"附录A 语义分组数：{len(case.groups)} 组")
    if supplement_case is not None:
        blocks.append(f"黑桃9 逐段补研究条目数：{len(supplement_case.items)} 条")
    blocks.append(f"本地来源文件：{source_label}")
    blocks.append(f"本地来源 SHA-256：{source_sha256}")
    if source_urls:
        for idx, url in enumerate(source_urls, start=1):
            blocks.append(f"Notion 来源链接 {idx}：{url}")
    else:
        blocks.append("Notion 来源链接：未在构建命令中提供")
    if supplement_case is not None:
        if supplement_label:
            blocks.append(f"本地补研究来源文件：{supplement_label}")
        if supplement_sha256:
            blocks.append(f"本地补研究来源 SHA-256：{supplement_sha256}")
        if supplement_urls:
            for idx, url in enumerate(supplement_urls, start=1):
                blocks.append(f"Notion 补研究来源链接 {idx}：{url}")
    blocks.append(
        "核验免责说明：本页全部原文、附录A分组分析、附录B逐项分析及可选补研究均逐条原样摘录自既有来源文档，未经独立重新听审原音视频核对；"
        "未标注画面或混合说明的已存文字来源记录均保持原貌，不作为已核验口播定性，不作改写、补全或主观臆测。"
    )

    blocks.append("## 文稿连读（依据已存文字，非重新听审）")
    blocks.append(
        f"本节按来源原序连贯保留 {case.case_id} 全部 {len(source_text_rows)} 条已存文字来源记录，仅排除已显式标注的画面或混合记录；"
        "不补写过渡词、不增删标点，非重新听审结果。被排除的显式画面或混合记录完整保留于下文显式画面与混合记录清单及原文逐项中。"
    )
    if source_text_rows:
        for row in source_text_rows:
            blocks.extend(emit_clean_reading_paragraphs(row.original_text, row.unit_time))
    else:
        blocks.append("本案未检出未标注画面或混合说明的已存文字来源记录。")

    blocks.append("## 原文逐项")
    blocks.append(
        f"本节按原序完整保留 {case.case_id} 全部 {len(case.rows)} 条原始单元记录。"
        "每一单元均显式标注原单元序号与时间，并保守区分已存文字来源记录与显式画面或混合记录，不作复听外的口播定性。"
    )
    for row in case.rows:
        blocks.append(f"### 单元 {row.index:02d}｜{row.unit_time}")
        blocks.append(f"记录区分：{classification_label_zh(row.classification)}")
        blocks.extend(
            emit_prefixed_paragraphs(
                f"原文（{row.unit_time}）：",
                f"原文续（{row.unit_time}）：",
                row.original_text,
            )
        )

    blocks.append("## 显式画面与混合记录清单")
    if visual_mixed_rows:
        blocks.append(
            f"以下 {len(visual_mixed_rows)} 条单元因原单元格含显式画面、无解释性字幕、近景、表情停顿、成品尾留或混合标注，已在此单独列出并从文稿连读中保守排除；"
            "全部原文字词仍在原文逐项中完整保留，此处不作复听外的归类猜测。"
        )
        for row in visual_mixed_rows:
            blocks.append(f"### 画面或混合 {row.index:02d}｜{row.unit_time}")
            blocks.append(f"记录区分：{classification_label_zh(row.classification)}")
            blocks.extend(
                emit_prefixed_paragraphs(
                    f"显式画面或混合单元（{row.unit_time}）：",
                    f"显式画面或混合单元续（{row.unit_time}）：",
                    row.original_text,
                )
            )
    else:
        blocks.append(
            "本案全部单元均未检出显式画面、无解释性字幕或混合标注，均按已存文字来源记录保留（未经重新听审）。"
        )

    blocks.append("## 语义分组分析（附录A）")
    blocks.append(
        f"本节按原序无损保留 {case.case_id} 附录A 全部 {len(case.groups)} 组实质语义分组分析（全部 7 列原样保留）。"
    )
    headers = case.group_headers if len(case.group_headers) == EXPECTED_GROUP_COLUMNS else DEFAULT_GROUP_HEADERS
    for grp in case.groups:
        blocks.append(f"### 分组 {grp.index:02d}｜{grp.group_id}")
        field_pairs = (
            (headers[1], grp.mechanisms),
            (headers[2], grp.read_before),
            (headers[3], grp.change),
            (headers[4], grp.position_reason),
            (headers[5], grp.next_expectation),
            (headers[6], grp.deletion_loss),
        )
        for col_label, val in field_pairs:
            blocks.extend(
                emit_prefixed_paragraphs(
                    f"{col_label}（{grp.group_id}）：",
                    f"{col_label}续（{grp.group_id}）：",
                    val,
                )
            )

    blocks.append("## 分析逐项")
    blocks.append(
        f"本节按原序完整保留 {case.case_id} 附录B 全部 {len(case.rows)} 条既有逐项分析与语义分组，不删减原文字词。"
    )
    for row in case.rows:
        blocks.append(f"### 分析 {row.index:02d}｜{row.unit_time}")
        blocks.extend(
            emit_prefixed_paragraphs(
                f"既有分析（{row.unit_time}）：",
                f"既有分析续（{row.unit_time}）：",
                row.analysis,
            )
        )
        blocks.extend(
            emit_prefixed_paragraphs(
                f"语义分组（{row.unit_time}）：",
                f"语义分组续（{row.unit_time}）：",
                row.semantic_group,
            )
        )

    if supplement_case is not None:
        blocks.append("## 逐段补研究（黑桃9补充材料）")
        blocks.append(
            f"本节原样收录黑桃9补充材料中 {case.case_id} 的全部 {len(supplement_case.items)} 条既有编号分析条目，"
            "不含思远（S01–S11）或用户最终成稿内容，保留原始 `<br>` 与富文本标记不做改写。"
        )
        for intro_idx, intro_line in enumerate(supplement_case.intro_lines, start=1):
            blocks.extend(
                emit_prefixed_paragraphs(
                    f"补研究导语 {intro_idx}（{case.case_id}）：",
                    f"补研究导语 {intro_idx} 续（{case.case_id}）：",
                    intro_line,
                )
            )
        for item in supplement_case.items:
            blocks.append(f"### 补研究 {item.number}｜{case.case_id}")
            blocks.extend(
                emit_prefixed_paragraphs(
                    f"补研究条目 {item.number}（{case.case_id}）：",
                    f"补研究条目 {item.number} 续（{case.case_id}）：",
                    item.text,
                )
            )

    rendered = "\n".join(blocks) + "\n"
    validate_flat_markdown(rendered, case_filename(case.case_id))
    return rendered


def render_index_markdown(
    parsed: ParsedSource,
    *,
    source_label: str,
    source_sha256: str,
    source_urls: Sequence[str],
    supplement: ParsedSupplement | None = None,
    supplement_label: str | None = None,
    supplement_sha256: str | None = None,
    supplement_urls: Sequence[str] = (),
) -> str:
    """Render 00_index.md using flat H1-H3 headings and paragraphs without blank lines."""
    total_units = sum(len(c.rows) for c in parsed.cases)
    total_groups = sum(len(c.groups) for c in parsed.cases)
    supp_by_case = {c.case_id: c for c in supplement.cases} if supplement is not None else {}

    blocks: list[str] = []
    blocks.append("# 00｜黑桃9 文稿与分析汇总总索引")
    blocks.append("## 汇总概况与来源校验")
    blocks.append(f"收录案例总数：{len(parsed.cases)} 个（C01 至 C11）")
    blocks.append(f"收录附录B 单元总数：{total_units} 条")
    blocks.append(f"收录附录A 语义分组总数：{total_groups} 组")
    if supplement is not None:
        total_supp = sum(len(c.items) for c in supplement.cases)
        blocks.append(f"收录黑桃9 逐段补研究条目总数：{total_supp} 条（11 案 x 4 条）")
    else:
        blocks.append("收录黑桃9 逐段补研究条目总数：未在本次构建中附加可选补研究材料")
    blocks.append(f"本地来源文件：{source_label}")
    blocks.append(f"本地来源 SHA-256：{source_sha256}")
    if source_urls:
        for idx, url in enumerate(source_urls, start=1):
            blocks.append(f"Notion 来源链接 {idx}：{url}")
    else:
        blocks.append("Notion 来源链接：未在构建命令中提供")
    if supplement is not None:
        if supplement_label:
            blocks.append(f"本地补研究来源文件：{supplement_label}")
        if supplement_sha256:
            blocks.append(f"本地补研究来源 SHA-256：{supplement_sha256}")
        if supplement_urls:
            for idx, url in enumerate(supplement_urls, start=1):
                blocks.append(f"Notion 补研究来源链接 {idx}：{url}")
    blocks.append(
        "范围说明：本独立目录仅收录黑桃9的 11 个案例文稿连读、原文逐项、显式画面与混合记录清单、附录A 108 组语义分组分析、附录B 335 条逐项分析及可选的黑桃9 逐段补研究，"
        "不包含用户本人最终脚本，也不复制思源（S01–S11）混合材料。所有记录均未经重新听审，按来源原样保留。"
    )

    blocks.append("## 案例目录")
    for case in parsed.cases:
        source_text_count = sum(1 for r in case.rows if not is_visual_or_mixed(r.classification))
        visual_count = len(case.rows) - source_text_count
        fname = case_filename(case.case_id)
        blocks.append(f"### {case.case_id}｜{case.title}")
        summary_parts = [
            f"对应文件：{fname}",
            f"单元数：{len(case.rows)}（已存文字来源记录 {source_text_count} 条，显式画面或混合记录 {visual_count} 条）",
            f"附录A分组：{len(case.groups)} 组",
        ]
        if case.case_id in supp_by_case:
            summary_parts.append(f"补研究：{len(supp_by_case[case.case_id].items)} 条")
        blocks.append("｜".join(summary_parts))

    blocks.append("## 配套文件说明")
    blocks.append(
        f"来源与跨案参考说明文件：{REFERENCE_FILENAME}（仅提供来源链接与参考指引，不搬运混合思源正文或用户自有成稿）。"
    )
    blocks.append(
        f"内部校验清单文件：{MANIFEST_FILENAME}（仅本地校验与同步定位使用，不写入 Notion 页面）。"
    )

    rendered = "\n".join(blocks) + "\n"
    validate_flat_markdown(rendered, INDEX_FILENAME)
    return rendered


def render_reference_markdown(
    parsed: ParsedSource,
    *,
    source_label: str,
    source_sha256: str,
    source_urls: Sequence[str],
    supplement: ParsedSupplement | None = None,
    supplement_label: str | None = None,
    supplement_sha256: str | None = None,
    supplement_urls: Sequence[str] = (),
) -> str:
    """Render 90_source_reference.md with provenance links and reference-only pointers."""
    blocks: list[str] = []
    blocks.append("# 90｜来源引用与跨案参考说明")

    blocks.append("## 收录边界与排除声明")
    blocks.append(
        "本汇总目录严格限定为黑桃9 11 个案例（C01 至 C11，共 335 条附录B 单元记录、108 组附录A 语义分组分析及可选的 44 条黑桃9 逐段补研究）。"
        "本文件不复制完整的思源（S01–S11）混合资料，也不收录用户本人的最终成稿或原创脚本。"
    )
    blocks.append(
        "附录A 之前的前置跨案综合内容通常包含复杂排版或混合上下文，本页仅作为只读参考索引，"
        "不直接搬运混合正文；如需查阅跨案综合笔记，请直接打开下列本地原始文件或 Notion 来源链接。"
    )

    blocks.append("## 本地原始分析文件")
    blocks.append(f"原始分析路径：{source_label}")
    blocks.append(f"原始文件 SHA-256：{source_sha256}")
    blocks.append(
        f"已校验附录B 结构：共 {len(parsed.cases)} 个案例，单元分布 {[len(c.rows) for c in parsed.cases]}，"
        f"合计 {sum(len(c.rows) for c in parsed.cases)} 条。"
    )
    blocks.append(
        f"已校验附录A 结构：共 {len(parsed.cases)} 个案例，分组分布 {[len(c.groups) for c in parsed.cases]}，"
        f"合计 {sum(len(c.groups) for c in parsed.cases)} 组。"
    )

    if supplement is not None:
        blocks.append("## 本地可选黑桃9 逐段补研究来源")
        if supplement_label:
            blocks.append(f"补研究文件路径：{supplement_label}")
        if supplement_sha256:
            blocks.append(f"补研究文件 SHA-256：{supplement_sha256}")
        blocks.append(
            f"已校验补研究结构：共 {len(supplement.cases)} 个案例（仅截取黑桃9逐段补研究区段并在后续非黑桃区段前停止），"
            f"条目分布 {[len(c.items) for c in supplement.cases]}，合计 {sum(len(c.items) for c in supplement.cases)} 条。"
        )

    blocks.append("## Notion 来源链接")
    if source_urls:
        for idx, url in enumerate(source_urls, start=1):
            blocks.append(f"主分析来源链接 {idx}：{url}")
    else:
        blocks.append("构建时未提供 `--source-url` 参数。")
    if supplement is not None:
        if supplement_urls:
            for idx, url in enumerate(supplement_urls, start=1):
                blocks.append(f"补研究来源链接 {idx}：{url}")
        else:
            blocks.append("构建时未提供 `--supplement-url` 参数。")

    blocks.append("## 附录A 之前章节索引（仅供参考，未复制混合正文）")
    if parsed.pre_appendix_headings:
        blocks.append(
            "以下仅列出原始分析文档在 `# 附录A` 之前出现的章节标题，供回到原始文档定位使用；"
            "复杂或混合文本均标为参考定位，不纳入本汇总正文。"
        )
        for idx, heading in enumerate(parsed.pre_appendix_headings, start=1):
            blocks.extend(
                emit_prefixed_paragraphs(
                    f"参考章节 {idx:02d}：",
                    f"参考章节 {idx:02d} 续：",
                    heading,
                )
            )
    else:
        blocks.append("原始分析文档在 `# 附录A` 之前未检出其他章节标题。")

    rendered = "\n".join(blocks) + "\n"
    validate_flat_markdown(rendered, REFERENCE_FILENAME)
    return rendered


def resolve_bundle_dir(output_dir: Path, folder_name: str) -> Path:
    """Resolve target standalone folder without double-nesting."""
    if not folder_name:
        return output_dir
    if output_dir.name == folder_name:
        return output_dir
    return output_dir / folder_name


def check_destination_safety(bundle_dir: Path, planned_files: dict[str, bytes]) -> dict[str, str]:
    """Verify destination has no symlinks and no conflicting or extra files.

    Returns a per-filename status map ("create" or "identical").
    Raises PathSafetyError before writing anything if any check fails.
    """
    if bundle_dir.is_symlink():
        raise PathSafetyError(f"Refusing symlink output directory: {bundle_dir}")
    if bundle_dir.parent.is_symlink():
        raise PathSafetyError(f"Refusing symlink parent of output directory: {bundle_dir.parent}")

    statuses: dict[str, str] = {}
    if not bundle_dir.exists():
        for name in planned_files:
            statuses[name] = "create"
        return statuses

    if not bundle_dir.is_dir():
        raise PathSafetyError(f"Output path exists and is not a directory: {bundle_dir}")

    existing_entries = sorted(bundle_dir.iterdir(), key=lambda p: p.name)
    for entry in existing_entries:
        if entry.is_symlink():
            raise PathSafetyError(f"Refusing symlink inside destination: {entry}")
        if entry.name not in planned_files:
            raise PathSafetyError(
                f"Destination directory is non-empty and contains unexpected entry: {entry.name}"
            )
        if not entry.is_file():
            raise PathSafetyError(f"Destination entry is not a regular file: {entry}")
        existing_bytes = entry.read_bytes()
        if existing_bytes != planned_files[entry.name]:
            raise PathSafetyError(
                f"Conflicting file already exists and is not byte-identical: {entry.name}"
            )
        statuses[entry.name] = "identical"

    for name in planned_files:
        statuses.setdefault(name, "create")
    return statuses


def _provenance_label(file_path: Path, vault_dir: Path | None) -> str:
    if vault_dir is not None:
        try:
            return file_path.resolve().relative_to(vault_dir.resolve()).as_posix()
        except ValueError:
            return str(file_path)
    return str(file_path)


def build_bundle(
    source_path: Path,
    output_dir: Path,
    *,
    source_urls: Sequence[str] = (),
    supplement_path: Path | None = None,
    supplement_urls: Sequence[str] = (),
    folder_name: str = DEFAULT_FOLDER_NAME,
    vault_dir: Path | None = None,
) -> dict[str, Any]:
    """Parse, validate, and deterministically publish the Black9 standalone folder."""
    ensure_safe_file_read(source_path)
    if output_dir.is_symlink():
        raise PathSafetyError(f"Refusing symlink output root: {output_dir}")
    if vault_dir is not None and vault_dir.is_symlink():
        raise PathSafetyError(f"Refusing symlink vault directory: {vault_dir}")

    clean_supp_urls = tuple(u.strip() for u in supplement_urls if u and u.strip())
    if clean_supp_urls and supplement_path is None:
        raise SourceParseError("`--supplement-url` was provided without `--supplement PATH`")

    raw_bytes = source_path.read_bytes()
    source_hash = sha256_bytes(raw_bytes)
    try:
        source_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceParseError(f"Source file is not valid UTF-8: {exc}") from exc

    parsed = parse_source_markdown(source_text)
    validate_parsed_source(parsed)

    parsed_supplement: ParsedSupplement | None = None
    supp_by_case: dict[str, SupplementCase] = {}
    supplement_hash: str | None = None
    supplement_label: str | None = None
    if supplement_path is not None:
        ensure_safe_file_read(supplement_path)
        supp_bytes = supplement_path.read_bytes()
        supplement_hash = sha256_bytes(supp_bytes)
        try:
            supp_text = supp_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceParseError(f"Supplement file is not valid UTF-8: {exc}") from exc
        parsed_supplement = parse_supplement_markdown(supp_text)
        validate_parsed_supplement(parsed_supplement)
        supp_by_case = {c.case_id: c for c in parsed_supplement.cases}
        supplement_label = _provenance_label(supplement_path, vault_dir)

    source_label = _provenance_label(source_path, vault_dir)
    clean_urls = tuple(u.strip() for u in source_urls if u and u.strip())
    bundle_dir = resolve_bundle_dir(output_dir, folder_name)

    # Render all 13 Markdown documents in memory BEFORE touching disk.
    planned_md: list[tuple[str, str, str, str, bytes, int]] = []

    index_text = render_index_markdown(
        parsed,
        source_label=source_label,
        source_sha256=source_hash,
        source_urls=clean_urls,
        supplement=parsed_supplement,
        supplement_label=supplement_label,
        supplement_sha256=supplement_hash,
        supplement_urls=clean_supp_urls,
    )
    index_bytes = index_text.encode("utf-8")
    planned_md.append(
        (
            "00_index",
            INDEX_FILENAME,
            "00｜黑桃9 文稿与分析汇总总索引",
            "index",
            index_bytes,
            validate_flat_markdown(index_text, INDEX_FILENAME),
        )
    )

    manifest_cases: list[dict[str, Any]] = []
    for case in parsed.cases:
        fname = case_filename(case.case_id)
        supp_case = supp_by_case.get(case.case_id)
        md_text = render_case_markdown(
            case,
            source_label=source_label,
            source_sha256=source_hash,
            source_urls=clean_urls,
            supplement_case=supp_case,
            supplement_label=supplement_label,
            supplement_sha256=supplement_hash,
            supplement_urls=clean_supp_urls,
        )
        md_bytes = md_text.encode("utf-8")
        max_line = validate_flat_markdown(md_text, fname)
        file_hash = sha256_bytes(md_bytes)
        planned_md.append(
            (
                case.case_id,
                fname,
                f"{case.case_id}｜{case.title}",
                "case",
                md_bytes,
                max_line,
            )
        )
        source_text_count = sum(1 for r in case.rows if not is_visual_or_mixed(r.classification))
        manifest_cases.append(
            {
                "case_id": case.case_id,
                "title": case.title,
                "filename": fname,
                "sha256": file_hash,
                "unit_count": len(case.rows),
                "source_text_unverified_count": source_text_count,
                "visual_or_mixed_count": len(case.rows) - source_text_count,
                "group_count": len(case.groups),
                "group_headers": list(case.group_headers),
                "groups": [
                    {
                        "index": g.index,
                        "group_id": g.group_id,
                        "mechanisms": g.mechanisms,
                        "read_before": g.read_before,
                        "change": g.change,
                        "position_reason": g.position_reason,
                        "next_expectation": g.next_expectation,
                        "deletion_loss": g.deletion_loss,
                    }
                    for g in case.groups
                ],
                "supplement_item_count": len(supp_case.items) if supp_case is not None else 0,
                "supplement_items": [
                    {"number": it.number, "text": it.text}
                    for it in (supp_case.items if supp_case is not None else ())
                ],
                "rows": [
                    {
                        "index": r.index,
                        "unit_time": r.unit_time,
                        "original_text": r.original_text,
                        "analysis": r.analysis,
                        "semantic_group": r.semantic_group,
                        "classification": r.classification,
                    }
                    for r in case.rows
                ],
            }
        )

    ref_text = render_reference_markdown(
        parsed,
        source_label=source_label,
        source_sha256=source_hash,
        source_urls=clean_urls,
        supplement=parsed_supplement,
        supplement_label=supplement_label,
        supplement_sha256=supplement_hash,
        supplement_urls=clean_supp_urls,
    )
    ref_bytes = ref_text.encode("utf-8")
    planned_md.append(
        (
            "90_source_reference",
            REFERENCE_FILENAME,
            "90｜来源引用与跨案参考说明",
            "reference",
            ref_bytes,
            validate_flat_markdown(ref_text, REFERENCE_FILENAME),
        )
    )

    manifest_files: list[dict[str, Any]] = []
    planned_bytes: dict[str, bytes] = {}
    for doc_id, fname, title, kind, data, max_line in planned_md:
        planned_bytes[fname] = data
        manifest_files.append(
            {
                "doc_id": doc_id,
                "filename": fname,
                "title": title,
                "kind": kind,
                "sha256": sha256_bytes(data),
                "byte_length": len(data),
                "max_line_length": max_line,
                "notion_sync": True,
            }
        )

    total_groups = sum(len(c.groups) for c in parsed.cases)
    actual_group_counts = [len(c.groups) for c in parsed.cases]
    supplement_item_count = (
        sum(len(c.items) for c in parsed_supplement.cases)
        if parsed_supplement is not None
        else 0
    )
    supplement_counts = (
        [len(c.items) for c in parsed_supplement.cases]
        if parsed_supplement is not None
        else []
    )

    manifest_obj: dict[str, Any] = {
        "schema_version": 2,
        "bundle_name": bundle_dir.name,
        "source_label": source_label,
        "source_sha256": source_hash,
        "source_urls": list(clean_urls),
        "case_count": len(parsed.cases),
        "total_units": sum(len(c.rows) for c in parsed.cases),
        "expected_counts": list(EXPECTED_COUNTS),
        "actual_counts": [len(c.rows) for c in parsed.cases],
        "expected_total_groups": EXPECTED_TOTAL_GROUPS,
        "total_groups": total_groups,
        "actual_group_counts": actual_group_counts,
        "supplement_included": parsed_supplement is not None,
        "supplement_label": supplement_label,
        "supplement_sha256": supplement_hash,
        "supplement_urls": list(clean_supp_urls),
        "supplement_item_count": supplement_item_count,
        "supplement_counts": supplement_counts,
        "flat_markdown_no_blank_lines": True,
        "excluded_from_notion": [MANIFEST_FILENAME],
        "files": manifest_files,
        "cases": manifest_cases,
    }
    manifest_bytes = (
        json.dumps(manifest_obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    planned_bytes[MANIFEST_FILENAME] = manifest_bytes

    # Safety preflight on destination BEFORE creating or writing anything.
    file_statuses = check_destination_safety(bundle_dir, planned_bytes)

    bundle_dir.mkdir(parents=True, exist_ok=True)
    for fname, data in planned_bytes.items():
        if file_statuses[fname] == "create":
            atomic_write_bytes(bundle_dir / fname, data, root=bundle_dir)

    return {
        "status": "ok",
        "bundle_dir": str(bundle_dir),
        "manifest_path": str(bundle_dir / MANIFEST_FILENAME),
        "source_sha256": source_hash,
        "case_count": len(parsed.cases),
        "total_units": sum(len(c.rows) for c in parsed.cases),
        "counts": [len(c.rows) for c in parsed.cases],
        "total_groups": total_groups,
        "group_counts": actual_group_counts,
        "supplement_included": parsed_supplement is not None,
        "supplement_sha256": supplement_hash,
        "supplement_item_count": supplement_item_count,
        "supplement_counts": supplement_counts,
        "files": [
            {
                "doc_id": entry["doc_id"],
                "filename": entry["filename"],
                "sha256": entry["sha256"],
                "max_line_length": entry["max_line_length"],
                "action": file_statuses[entry["filename"]],
            }
            for entry in manifest_files
        ],
        "manifest_action": file_statuses[MANIFEST_FILENAME],
        "manifest_sha256": sha256_bytes(manifest_bytes),
    }


def extract_notion_content_xml(raw_text: str) -> tuple[str, bool]:
    """Extract inner body from Notion XML wrapper `<content>...</content>` conservatively.

    Returns (extracted_text, had_xml_wrapper). Never drops literal words inside `<content>`.
    """
    if not isinstance(raw_text, str):
        raise McpFetchError("Notion fetch text field is not a string")
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")

    if "<content/>" in normalized or "<content />" in normalized:
        return "", True

    start_match = re.search(r"<content(?:\s[^>]*)?>", normalized)
    if start_match:
        end_idx = normalized.rfind("</content>")
        if end_idx != -1 and end_idx >= start_match.end():
            inner = normalized[start_match.end() : end_idx]
            if inner.startswith("\n"):
                inner = inner[1:]
            if inner.endswith("\n"):
                inner = inner[:-1]
            return inner, True

    return normalized, False


def parse_notion_fetch_mcp_response(stdout_text: str) -> dict[str, Any]:
    """Parse `minis-mcp-cli call Notion notion-fetch` output strictly."""
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise McpFetchError(f"Malformed JSON from minis-mcp-cli: {exc}") from exc

    if not isinstance(envelope, dict):
        raise McpFetchError("MCP response top-level JSON must be an object")
    if envelope.get("error"):
        raise McpFetchError(
            f"MCP response returned error: {redact_secrets(str(envelope['error']))}"
        )

    result_obj = envelope.get("result")
    if not isinstance(result_obj, dict):
        raise McpFetchError("MCP response missing object field `result`")
    if result_obj.get("isError"):
        raise McpFetchError("MCP tool execution returned isError=true")

    content_list = result_obj.get("content")
    if not isinstance(content_list, list) or not content_list:
        raise McpFetchError("MCP response missing non-empty `result.content` list")

    text_chunks: list[str] = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
            text_chunks.append(item["text"])
    if not text_chunks:
        raise McpFetchError("MCP response `result.content` contains no text entry")

    inner_raw = text_chunks[0]
    try:
        inner_obj = json.loads(inner_raw)
    except json.JSONDecodeError as exc:
        raise McpFetchError(f"MCP inner text is not valid JSON: {exc}") from exc

    if not isinstance(inner_obj, dict):
        raise McpFetchError("MCP inner JSON payload must be an object")
    if not isinstance(inner_obj.get("text"), str):
        raise McpFetchError("MCP inner JSON payload missing string field `text`")

    extracted, had_wrapper = extract_notion_content_xml(inner_obj["text"])
    return {
        "title": inner_obj.get("title", ""),
        "url": inner_obj.get("url", ""),
        "raw_text": inner_obj["text"],
        "extracted_text": extracted,
        "had_xml_wrapper": had_wrapper,
        "raw_envelope": envelope,
    }


def fetch_notion_page(
    notion_id: str,
    output_file: Path,
    *,
    raw_output_file: Path | None = None,
    cli_bin: str = "minis-mcp-cli",
    timeout_sec: int = 60,
) -> dict[str, Any]:
    """Fetch a Notion page via minis-mcp-cli and save raw evidence + extracted text safely."""
    clean_id = notion_id.strip()
    if not clean_id:
        raise McpFetchError("Notion ID must not be empty")

    if output_file.is_symlink():
        raise PathSafetyError(f"Refusing symlink output file: {output_file}")
    if output_file.parent.is_symlink():
        raise PathSafetyError(f"Refusing symlink output directory: {output_file.parent}")

    evidence_path = (
        raw_output_file
        if raw_output_file is not None
        else output_file.with_name(f"{output_file.name}.raw.json")
    )
    if evidence_path.is_symlink():
        raise PathSafetyError(f"Refusing symlink raw evidence file: {evidence_path}")
    if evidence_path.parent.is_symlink():
        raise PathSafetyError(f"Refusing symlink raw evidence directory: {evidence_path.parent}")

    payload = json.dumps({"id": clean_id}, ensure_ascii=False)
    cmd = [cli_bin, "call", "Notion", "notion-fetch", payload]

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except FileNotFoundError as exc:
        raise McpFetchError(f"MCP CLI executable not found: {cli_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise McpFetchError(f"MCP notion-fetch timed out after {timeout_sec}s") from exc

    if proc.returncode != 0:
        err_msg = redact_secrets((proc.stderr or proc.stdout or "").strip())[:300]
        raise McpFetchError(
            f"minis-mcp-cli exited with code {proc.returncode}: {err_msg}"
        )

    parsed = parse_notion_fetch_mcp_response(proc.stdout)
    extracted_bytes = (parsed["extracted_text"] + "\n").encode("utf-8")
    raw_evidence = {
        "schema_version": 1,
        "notion_id": clean_id,
        "title": parsed["title"],
        "url": parsed["url"],
        "had_xml_wrapper": parsed["had_xml_wrapper"],
        "note": "Notion fetch output may summarize or omit full inline tables; inspect raw evidence carefully.",
        "raw_text_sha256": sha256_text(parsed["raw_text"]),
        "extracted_text_sha256": sha256_bytes(extracted_bytes),
        "raw_text": parsed["raw_text"],
    }
    raw_bytes = (
        json.dumps(raw_evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    # Safe non-overwrite check (allow only byte-identical existing files).
    for target, data in ((output_file, extracted_bytes), (evidence_path, raw_bytes)):
        if target.exists():
            if not target.is_file():
                raise PathSafetyError(f"Target exists and is not a file: {target}")
            if target.read_bytes() != data:
                raise PathSafetyError(
                    f"Refusing to overwrite existing non-identical file: {target}"
                )

    if not output_file.exists():
        atomic_write_bytes(output_file, extracted_bytes)
    if not evidence_path.exists():
        atomic_write_bytes(evidence_path, raw_bytes)

    return {
        "status": "ok",
        "notion_id": clean_id,
        "title": parsed["title"],
        "url": parsed["url"],
        "had_xml_wrapper": parsed["had_xml_wrapper"],
        "output_file": str(output_file),
        "raw_evidence_file": str(evidence_path),
        "extracted_sha256": sha256_bytes(extracted_bytes),
        "raw_evidence_sha256": sha256_bytes(raw_bytes),
        "extracted_bytes": len(extracted_bytes),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="consolidate.py",
        description="Black9 standalone Markdown consolidation builder (stdlib only).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build_p = sub.add_parser(
        "build",
        help="Validate 11 cases / 335 units / 108 Appendix A groups (+ optional 44 Black9 supplement items) and build standalone folder + manifest.",
    )
    build_p.add_argument("--source", required=True, type=Path, help="Source analysis Markdown path.")
    build_p.add_argument("--output", required=True, type=Path, help="Workspace or output directory.")
    build_p.add_argument(
        "--source-url",
        action="append",
        default=[],
        help="Original Notion source URL (repeatable).",
    )
    build_p.add_argument(
        "--supplement",
        type=Path,
        default=None,
        help="Optional local extracted Notion 22-page body Markdown containing Black9 11x4 supplement items.",
    )
    build_p.add_argument(
        "--supplement-url",
        action="append",
        default=[],
        help="Optional Notion URL for the supplement source (repeatable; requires --supplement).",
    )
    build_p.add_argument(
        "--vault-dir",
        "--vault",
        dest="vault_dir",
        type=Path,
        default=None,
        help="Optional Obsidian vault directory for relative source provenance label.",
    )
    build_p.add_argument(
        "--folder-name",
        default=DEFAULT_FOLDER_NAME,
        help=f"Standalone subfolder name (default: {DEFAULT_FOLDER_NAME}; pass '' to write directly into --output).",
    )

    fetch_p = sub.add_parser(
        "fetch",
        help="Fetch a Notion page via minis-mcp-cli and save raw evidence + extracted text.",
    )
    fetch_p.add_argument("--notion-id", required=True, help="Notion page ID to fetch.")
    fetch_p.add_argument("--output", required=True, type=Path, help="Extracted Markdown output file.")
    fetch_p.add_argument(
        "--raw-output",
        type=Path,
        default=None,
        help="Optional raw evidence JSON output path (default: <output>.raw.json).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            report = build_bundle(
                source_path=args.source,
                output_dir=args.output,
                source_urls=args.source_url,
                supplement_path=args.supplement,
                supplement_urls=args.supplement_url,
                folder_name=args.folder_name,
                vault_dir=args.vault_dir,
            )
        elif args.command == "fetch":
            report = fetch_notion_page(
                notion_id=args.notion_id,
                output_file=args.output,
                raw_output_file=args.raw_output,
            )
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except ConsolidateError as exc:
        err_report = {"status": "error", "error": redact_secrets(str(exc))}
        print(json.dumps(err_report, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
