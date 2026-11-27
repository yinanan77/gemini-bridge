#!/usr/bin/env python3
"""Comprehensive mocked unit tests for Black9 consolidate.py and sync.py (stdlib only).

All Notion MCP interactions are mocked locally; no live Notion API, credentials,
`/var/minis`, or private user data are touched.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Sequence
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import consolidate  # noqa: E402
import deploy  # noqa: E402
import sync  # noqa: E402

ROOT_PARENT_ID = "11111111-2222-3333-4444-555555555555"
PRIVATE_SENTINEL = "PRIVATE_MANUSCRIPT_SENTINEL_XYZ987"
SIYUAN_NOISE = "SIYUAN_MIXED_ARCHIVE_DO_NOT_COPY_999"
USER_SCRIPT_NOISE = "USER_OWN_FINAL_SCRIPT_DO_NOT_COPY_888"
DEFAULT_GROUP_COUNTS: tuple[int, ...] = (10, 10, 10, 10, 10, 10, 10, 10, 8, 10, 10)  # sum = 108

REAL_C11_B06 = "表情停顿，无底部解释性字幕"
REAL_C11_B51 = "无解释性字幕的成品尾留"
REAL_C10_A05 = "反应近景；小字“这得挨不少电”；随后“哈哈”"


def build_synthetic_source_markdown(
    *,
    counts: tuple[int, ...] = consolidate.EXPECTED_COUNTS,
    group_counts: tuple[int, ...] = DEFAULT_GROUP_COUNTS,
    include_appendix_a: bool = True,
    include_c11: bool = True,
    long_cell_chars: int = 2400,
) -> str:
    """Build a realistic synthetic source analysis Markdown fixture with Appendix A (108 groups) + Appendix B (335 units)."""
    lines: list[str] = [
        "# 跨案总览与前置材料",
        "",
        "## 思源混合归档摘录",
        f"这段是思源混合材料：{SIYUAN_NOISE}",
        "",
        "## 用户本人最终脚本草稿",
        f"这段是用户成稿：{USER_SCRIPT_NOISE}",
        "",
    ]

    case_ids = consolidate.EXPECTED_CASE_IDS if include_c11 else consolidate.EXPECTED_CASE_IDS[:-1]

    if include_appendix_a:
        lines.append("# 附录A｜黑桃9 十一案语义分组分析（108组）")
        lines.append("")
        for case_idx, case_id in enumerate(case_ids):
            g_count = group_counts[case_idx]
            lines.append(f"## {case_id}｜黑桃9案例标题{case_idx + 1}")
            lines.append("")
            lines.append("| groupID | mechanisms | read-before | change | position/reason | next-expectation | deletion-loss |")
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for g in range(1, g_count + 1):
                gid = f"{case_id}-G{g:02d}"
                mech = f"{gid} 机制A\\|机制B"
                read_before = f"{gid} 读到这里前状态"
                change = f"{gid} 本组造成的认知变化"
                pos_reason = f"{gid} 放置在此位置的原因"
                next_exp = f"{gid} 引发的下一组期待"
                del_loss = f"{gid} 删掉后的结构损失"
                lines.append(
                    f"| {gid} | {mech} | {read_before} | {change} | {pos_reason} | {next_exp} | {del_loss} |"
                )
            lines.append("")

    lines.append("# 附录B｜黑桃9 十一案逐单元原文与分析")
    lines.append("")

    for case_idx, case_id in enumerate(case_ids):
        row_count = counts[case_idx]
        lines.append(f"## {case_id}｜黑桃9案例标题{case_idx + 1}")
        lines.append("")
        lines.append("| 单元与时间 | 原文与画面记录 | 既有分析 | 语义分组 |")
        lines.append("| :--- | --- | --- | ---: |")
        for r in range(1, row_count + 1):
            sec_start = (r - 1) * 3
            sec_end = r * 3
            ts = f"00:{sec_start:02d}-00:{sec_end:02d}"
            if case_id == "C10":
                # C10 variant: non-digit prefix A01／time (49 rows)
                unit_id = f"A{r:02d}／{ts}"
            elif case_id == "C11":
                # C11 variant: mix of 1／time, V02／time, B06/B51 numeric/variant (51 rows)
                if r in (6, 51):
                    unit_id = f"B{r:02d}／{ts}"
                elif r % 3 == 1:
                    unit_id = f"{r}／{ts}"
                elif r % 3 == 2:
                    unit_id = f"V{r:02d}／{ts}"
                else:
                    unit_id = f"{r:02d} / {ts}"
            else:
                unit_id = f"{r} · {ts}"

            if case_id == "C01" and r == 1:
                orig = f"第一句已存文字含ASR误差（{PRIVATE_SENTINEL}）与转义竖线 A\\|B"
            elif case_id == "C01" and r == 2:
                orig = "[画面：纯画面转场，无对白]"
            elif case_id == "C01" and r == 3:
                orig = "[画面：举起道具] 这里直接说核心卖点"
            elif case_id == "C02" and r == 1:
                orig = "超长原文测试：" + ("这是用于验证单行长度上限的原文句子。" * (long_cell_chars // 16 + 1))
            elif case_id == "C10" and r == 5:
                orig = REAL_C10_A05
            elif case_id == "C10" and r == 49:
                orig = f"C10末尾单元原文（{PRIVATE_SENTINEL}）"
            elif case_id == "C11" and r == 6:
                orig = REAL_C11_B06
            elif case_id == "C11" and r == 51:
                orig = REAL_C11_B51
            else:
                orig = f"{case_id} 第{r}条原始文字记录，保留原貌"

            analysis = f"{case_id} 第{r}条既有分析，含原始评注"
            group = f"{case_id}-G{((r - 1) % max(1, group_counts[case_idx])) + 1:02d}"
            lines.append(f"| {unit_id} | {orig} | {analysis} | {group} |")
        lines.append("")

    return "\n".join(lines)


def build_synthetic_supplement_markdown(
    *,
    truncate_after_case: int | None = None,
    drop_row_in_case: str | None = None,
    extra_row_in_case: str | None = None,
    wrong_col_count_in_case: str | None = None,
    inject_siyuan_inside_black9: bool = False,
) -> str:
    """Build a realistic extracted BODY from Notion 22-page: 11 four-column Markdown tables x 4 data rows = 44 rows."""
    lines: list[str] = [
        "# 22版本逐段补研究",
        "",
        "## 一、研究背景与说明",
        "前置说明段落，不纳入单案补研究。",
        "",
        "## 二、黑桃11版本逐段补研究",
        "",
    ]

    limit = truncate_after_case if truncate_after_case is not None else len(consolidate.EXPECTED_CASE_IDS)
    for idx, case_id in enumerate(consolidate.EXPECTED_CASE_IDS[:limit], start=1):
        lines.append(f"### {case_id}｜黑桃9案例标题{idx}")
        lines.append("")
        if case_id == wrong_col_count_in_case:
            lines.append("| 连续原段／阅读摘要 | 措辞与对象为什么这样组织 | 与前后段的接口 |")
            lines.append("| --- | --- | --- |")
            for row_i in range(1, 5):
                lines.append(f"| {case_id} 摘要{row_i} | 组织{row_i} | 接口{row_i} |")
            lines.append("")
            continue

        lines.append(
            "| 连续原段／阅读摘要 | 措辞与对象为什么这样组织 | 与前后段的接口 | 机械迁移怎样失效 |"
        )
        lines.append("| --- | --- | --- | --- |")

        row_nums = [1, 2, 3] if case_id == drop_row_in_case else [1, 2, 3, 4]
        if case_id == extra_row_in_case:
            row_nums = [1, 2, 3, 4, 5]

        for num in row_nums:
            if case_id == "C01" and num == 1:
                c1 = f"**开场连续原段**：第一段<br>第二段含私密锚点（{PRIVATE_SENTINEL}）"
                c2 = "先给对象再压反差，含转义竖线 措辞A\\|措辞B 与 **加粗强调**"
                c3 = "承接开场悬念<br/>自然过渡到第2段演示"
                c4 = "若机械套用公式，会丢失具体场景锚点与 `inline_code` 细节"
            else:
                c1 = f"{case_id} 第{num}段连续原段摘要（保留原词）"
                c2 = f"{case_id} 第{num}段措辞与对象组织分析<br>含补充细节"
                c3 = f"{case_id} 第{num}段与前后段接口关系"
                c4 = f"{case_id} 第{num}段机械迁移失效原因"
            lines.append(f"| {c1} | {c2} | {c3} | {c4} |")
        lines.append("")

    if inject_siyuan_inside_black9:
        lines.append("### S01｜思远误混入黑桃区")
        lines.append(
            "| 连续原段／阅读摘要 | 措辞与对象为什么这样组织 | 与前后段的接口 | 机械迁移怎样失效 |"
        )
        lines.append("| --- | --- | --- | --- |")
        lines.append(f"| {SIYUAN_NOISE} | 误混入 | 误混入 | 误混入 |")
        lines.append("")

    lines.extend(
        [
            "## 三、思远11版本逐段补研究",
            "",
            "### S01｜思远案例1",
            "| 连续原段／阅读摘要 | 措辞与对象为什么这样组织 | 与前后段的接口 | 机械迁移怎样失效 |",
            "| --- | --- | --- | --- |",
            f"| 思远内容绝不进入黑桃9汇总：{SIYUAN_NOISE} | 思远组织 | 思远接口 | 思远失效 |",
            "| 思远第2行 | 思远组织2 | 思远接口2 | 思远失效2 |",
            "| 思远第3行 | 思远组织3 | 思远接口3 | 思远失效3 |",
            "| 思远第4行 | 思远组织4 | 思远接口4 | 思远失效4 |",
            "",
            "## 四、用户最终成稿",
            f"用户最终成稿内容绝不进入黑桃9汇总：{USER_SCRIPT_NOISE}",
            "",
        ]
    )
    return "\n".join(lines)


def build_synthetic_supplement_numbered_markdown(
    *,
    drop_item_in_case: str | None = None,
) -> str:
    """Build the numbered-list supplement test variant (11 cases x 4 numbered items = 44)."""
    lines: list[str] = [
        "# 22版本逐段补研究（编号变体）",
        "",
        "## 二、黑桃11版本逐段补研究",
        "",
    ]
    for idx, case_id in enumerate(consolidate.EXPECTED_CASE_IDS, start=1):
        lines.append(f"### {case_id}｜黑桃9案例标题{idx}")
        item_nums = [1, 2, 3] if case_id == drop_item_in_case else [1, 2, 3, 4]
        for num in item_nums:
            if case_id == "C01" and num == 1:
                lines.append(
                    f"1. **开场结构复盘**：保留 `<br>` 标记示例第一段<br>第二段含私密锚点（{PRIVATE_SENTINEL}）与 **加粗强调**。"
                )
            elif num == 2:
                lines.append(f"2. **节奏拆解（{case_id}）**：第一行含<br/>软换行。")
                lines.append("    - 缩进子项原样保留不丢弃")
            else:
                lines.append(f"{num}. **{case_id} 补研究项 {num}**：完整原样分析文本，含 `inline_code` 与 *斜体*。")
        lines.append("")

    lines.extend(
        [
            "## 三、思远11版本逐段补研究",
            "",
            "### S01｜思远案例1",
            f"1. 思远内容绝不进入黑桃9汇总：{SIYUAN_NOISE}",
            "",
        ]
    )
    return "\n".join(lines)


def standard_github_tools_schema() -> dict[str, Any]:
    """Realistic GitHub MCP tool schemas for get_file_contents and push_files."""
    return {
        "tools": [
            {
                "name": "get_file_contents",
                "description": "Get file contents from a GitHub repository",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "path": {"type": "string"},
                        "branch": {"type": "string"},
                    },
                    "required": ["owner", "repo", "path"],
                },
            },
            {
                "name": "push_files",
                "description": "Push multiple files in a single commit",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "owner": {"type": "string"},
                        "repo": {"type": "string"},
                        "branch": {"type": "string"},
                        "message": {"type": "string"},
                        "files": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {"type": "string"},
                                    "content": {"type": "string"},
                                },
                                "required": ["path", "content"],
                            },
                        },
                    },
                    "required": ["owner", "repo", "branch", "message", "files"],
                },
            },
            {
                "name": "unrelated_github_tool",
                "description": "Should not appear in inspect relevant output",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
    }


def build_existing_emade_sync_fixtures(existing_count: int = 101) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build realistic yinanan77/E-Made .sync/state.json (101 items) and sync-config.json."""
    items: dict[str, Any] = {}
    canonical: dict[str, str] = {}
    for i in range(1, existing_count + 1):
        rel = f"existing_notes/doc_{i:03d}.md"
        pid = f"22222222-3333-4444-8555-{i:012d}"
        items[f"source::{rel}"] = {
            "id": pid,
            "kind": "source",
            "hash": f"{i:064x}",
            "source": rel,
        }
        canonical[rel] = pid

    state_obj: dict[str, Any] = {
        "version": 1,
        "last_synced_at": "2026-11-26T12:00:00Z",
        "items": items,
    }
    config_obj: dict[str, Any] = {
        "root_page_id": ROOT_PARENT_ID,
        "sync_interval_minutes": 30,
        "canonical_page_ids": canonical,
        "manually_managed_source_paths": [
            "archive/black9_original_analysis.md",
            "archive/manual_only_page.md",
        ],
        "extra_preserved_setting": {"keep_intact": True, "tags": ["v1", "prod"]},
    }
    return state_obj, config_obj


class MockGitHubServer:
    """In-memory fake GitHub MCP server supporting get_file_contents (and tracking push_files)."""

    def __init__(
        self,
        *,
        files: dict[str, str] | None = None,
        tools_schema: dict[str, Any] | None = None,
        use_base64: bool = True,
    ) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.tools_schema = tools_schema or standard_github_tools_schema()
        self.use_base64 = use_base64
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.push_files_call_count = 0

    def _wrap_mcp(self, text: str) -> str:
        return json.dumps(
            {"result": {"content": [{"type": "text", "text": text}]}},
            ensure_ascii=False,
        )

    def subprocess_run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(cmd) >= 3 and cmd[1] == "tools" and cmd[2] == "GitHub":
            self.calls.append(("tools", None))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(self.tools_schema, ensure_ascii=False), stderr=""
            )

        if len(cmd) >= 5 and cmd[1] == "call" and cmd[2] == "GitHub":
            tool_name = cmd[3]
            payload = json.loads(cmd[4])
            self.calls.append((tool_name, payload))

            if tool_name == "get_file_contents":
                path = payload["path"]
                if path not in self.files:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="", stderr=f"Not Found: {path}"
                    )
                raw = self.files[path]
                if self.use_base64:
                    b64 = base64.b64encode(raw.encode("utf-8")).decode("ascii")
                    body = json.dumps(
                        {
                            "name": Path(path).name,
                            "path": path,
                            "sha": consolidate.sha256_text(raw)[:40],
                            "encoding": "base64",
                            "content": b64,
                        },
                        ensure_ascii=False,
                    )
                else:
                    body = raw
                return subprocess.CompletedProcess(cmd, 0, stdout=self._wrap_mcp(body), stderr="")

            if tool_name == "push_files":
                self.push_files_call_count += 1
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=self._wrap_mcp('{"status":"pushed"}'), stderr=""
                )

        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"Unsupported GitHub cmd: {cmd}")


def standard_notion_tools_schema() -> dict[str, Any]:
    """Realistic Notion MCP tool schemas matching official Notion MCP."""
    return {
        "tools": [
            {
                "name": "notion-fetch",
                "description": "Fetch a Notion page by ID",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
            {
                "name": "notion-create-pages",
                "description": "Create Notion pages under parent",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "parent": {
                            "type": "object",
                            "properties": {
                                "page_id": {"type": "string"},
                                "type": {"type": "string"},
                            },
                            "required": ["page_id"],
                        },
                        "pages": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "properties": {"type": "object"},
                                    "content": {"type": "string"},
                                },
                                "required": ["properties", "content"],
                            },
                        },
                    },
                    "required": ["parent", "pages"],
                },
            },
            {
                "name": "notion-update-page",
                "description": "Update Notion page content",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "data": {
                            "type": "object",
                            "properties": {
                                "page_id": {"type": "string"},
                                "command": {
                                    "type": "string",
                                    "enum": ["update_properties", "replace_content"],
                                },
                                "new_str": {"type": "string"},
                            },
                            "required": ["page_id", "command", "new_str"],
                        }
                    },
                    "required": ["data"],
                },
            },
            {
                "name": "notion-search",
                "description": "Search Notion workspace",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "unrelated-notion-tool",
                "description": "Should not appear in inspect relevant output",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]
    }


OLD_PARENT_ID = "22222222-3333-4444-8555-666666666666"


def runtime_flat_notion_tools_schema(
    *, allow_async_required: bool = False, include_search: bool = True
) -> dict[str, Any]:
    """Runtime-accurate flat Notion MCP schema (matching live `minis-mcp-cli tools Notion`)."""
    update_required = ["page_id", "command", "new_str"]
    if allow_async_required:
        update_required.append("allow_async")
    tools: list[dict[str, Any]] = [
        {
            "name": "notion-fetch",
            "description": "Fetch a Notion page by ID",
            "inputSchema": {
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
        },
        {
            "name": "notion-create-pages",
            "description": "Create Notion pages under parent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "parent": {
                        "type": "object",
                        "properties": {"page_id": {"type": "string"}},
                        "required": ["page_id"],
                    },
                    "pages": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "properties": {"type": "object"},
                                "content": {"type": "string"},
                            },
                            "required": ["properties", "content"],
                        },
                    },
                },
                "required": ["pages"],
            },
        },
        {
            "name": "notion-update-page",
            "description": "Update Notion page content (flat schema with allow_async)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string"},
                    "command": {
                        "type": "string",
                        "enum": ["update_properties", "replace_content", "update_content"],
                    },
                    "new_str": {"type": "string"},
                    "allow_async": {"type": "boolean"},
                },
                "required": update_required,
            },
        },
    ]
    if include_search:
        tools.append(
            {
                "name": "notion-search",
                "description": "Search Notion workspace",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        )
    return {"tools": tools}


class MockNotionServer:
    """In-memory fake Notion MCP server driven via mocked `subprocess.run`."""

    def __init__(
        self,
        *,
        tools_schema: dict[str, Any] | None = None,
        autolink_urls_on_fetch: bool = False,
        return_async_task_on_update: bool = False,
        fail_fetch_after_create_id: bool = False,
        extra_roots: Sequence[str] = (),
    ) -> None:
        self.tools_schema = tools_schema or standard_notion_tools_schema()
        self.autolink_urls_on_fetch = autolink_urls_on_fetch
        self.return_async_task_on_update = return_async_task_on_update
        self.fail_fetch_after_create_id = fail_fetch_after_create_id
        self.pages: dict[str, dict[str, Any]] = {
            ROOT_PARENT_ID: {
                "id": ROOT_PARENT_ID,
                "parent_id": None,
                "title": "Existing User Root Page",
                "content": "# Root\n\nExisting user content.",
                "url": f"https://www.notion.so/{ROOT_PARENT_ID.replace('-', '')}",
            },
        }
        for rid in extra_roots:
            self.pages[rid] = {
                "id": rid,
                "parent_id": None,
                "title": f"Extra Root {rid[:8]}",
                "content": "# Extra Root\n\nArchive.",
                "url": f"https://www.notion.so/{rid.replace('-', '')}",
            }
        self._seq = 1
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.create_call_count = 0
        self.update_call_count = 0
        self.create_payloads: list[dict[str, Any]] = []
        self.update_payloads: list[dict[str, Any]] = []
        # Failure injection hooks
        self.uncertain_on_create_index: int | None = None
        self.uncertain_mode: str = "malformed_after_create"  # or "timeout_after_create"
        self.fail_create_before_remote_at: int | None = None
        self.truncate_on_next_update: bool = False
        self.timeout_on_next_update: bool = False
        self.next_fetch_raw_override: dict[str, str] = {}

    def _next_id(self) -> str:
        n = self._seq
        self._seq += 1
        return f"aaaaaaaa-bbbb-4ccc-8ddd-{n:012d}"

    def _wrap_mcp(self, inner_obj: Any) -> str:
        return json.dumps(
            {
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(inner_obj, ensure_ascii=False),
                        }
                    ]
                }
            },
            ensure_ascii=False,
        )

    def _render_fetch_xml(self, page: dict[str, Any]) -> str:
        # Include child links in parent XML just like real Notion fetch.
        children = [p for p in self.pages.values() if p.get("parent_id") == page["id"]]
        body = page["content"]
        if self.autolink_urls_on_fetch:
            body = re.sub(r"(?<!\()(?<!\[)(https://[^\s)>\]]+)", r"[\1](\1)", body)
        if children:
            child_tags = "\n".join(
                f'<page url="{c["url"]}">{c["title"]}</page>' for c in children
            )
            body = f"{body}\n\n{child_tags}"
        return (
            f'<page url="{page["url"]}">\n'
            f"<properties>{json.dumps({'title': page['title']}, ensure_ascii=False)}</properties>\n"
            f"<content>\n{body}\n</content>\n"
            f"</page>"
        )

    def subprocess_run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(cmd) >= 3 and cmd[1] == "tools" and cmd[2] == "Notion":
            self.calls.append(("tools", None))
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(self.tools_schema, ensure_ascii=False), stderr=""
            )

        if len(cmd) >= 5 and cmd[1] == "call" and cmd[2] == "Notion":
            tool_name = cmd[3]
            payload = json.loads(cmd[4])
            self.calls.append((tool_name, payload))

            if tool_name == "notion-fetch":
                pid = payload.get("id") or payload.get("page_id")
                if (
                    self.fail_fetch_after_create_id
                    and self.create_call_count >= 2
                    and pid not in (ROOT_PARENT_ID, OLD_PARENT_ID)
                ):
                    self.fail_fetch_after_create_id = False
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Transient 503 on fetch")
                if pid not in self.pages:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Page not found")
                page = self.pages[pid]
                raw_text = self.next_fetch_raw_override.pop(pid, None)
                if raw_text is None:
                    raw_text = self._render_fetch_xml(page)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_mcp(
                        {"title": page["title"], "url": page["url"], "text": raw_text}
                    ),
                    stderr="",
                )

            if tool_name == "notion-create-pages":
                self.create_call_count += 1
                self.create_payloads.append(payload)
                if (
                    self.fail_create_before_remote_at is not None
                    and self.create_call_count == self.fail_create_before_remote_at
                ):
                    self.fail_create_before_remote_at = None
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
                parent_id = payload["parent"]["page_id"]
                page_spec = payload["pages"][0]
                title = page_spec["properties"]["title"]
                content = page_spec["content"]
                new_id = self._next_id()
                url = f"https://www.notion.so/{new_id.replace('-', '')}"
                self.pages[new_id] = {
                    "id": new_id,
                    "parent_id": parent_id,
                    "title": title,
                    "content": content,
                    "url": url,
                }
                if (
                    self.uncertain_on_create_index is not None
                    and self.create_call_count == self.uncertain_on_create_index
                ):
                    self.uncertain_on_create_index = None
                    if self.uncertain_mode == "timeout_after_create":
                        raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
                    # Page WAS created remotely, but response is malformed/missing ID!
                    return subprocess.CompletedProcess(cmd, 0, stdout="{not valid json", stderr="")

                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_mcp({"pages": [{"id": new_id, "url": url}]}),
                    stderr="",
                )

            if tool_name == "notion-update-page":
                self.update_call_count += 1
                self.update_payloads.append(payload)
                data = payload["data"] if isinstance(payload.get("data"), dict) else payload
                pid = data["page_id"]
                new_str = data["new_str"]
                if self.timeout_on_next_update:
                    self.timeout_on_next_update = False
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
                if self.return_async_task_on_update:
                    self.return_async_task_on_update = False
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=self._wrap_mcp(
                            {"async_task": {"id": "async-task-98765", "status": "in_progress"}}
                        ),
                        stderr="",
                    )
                if self.truncate_on_next_update:
                    self.truncate_on_next_update = False
                    new_str = new_str[:80]
                self.pages[pid]["content"] = new_str
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=self._wrap_mcp({"id": pid, "status": "updated"}), stderr=""
                )

            if tool_name == "notion-search":
                query = payload["query"]
                matches = []
                for p in self.pages.values():
                    if query in p["title"] or query in p["content"]:
                        matches.append(
                            {
                                "id": p["id"],
                                "title": p["title"],
                                "url": p["url"],
                                "highlight": p["title"],
                            }
                        )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=self._wrap_mcp({"results": matches}), stderr=""
                )

        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"Unsupported cmd: {cmd}")


class Black9ConsolidateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source_file = self.tmp_path / "source_analysis.md"
        self.source_file.write_text(build_synthetic_source_markdown(), encoding="utf-8")
        self.output_root = self.tmp_path / "vault"
        self.output_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_build_preserves_335_units_variants_c10_c11_and_flat_format(self) -> None:
        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
            source_urls=["https://www.notion.so/source-page-1"],
            vault_dir=self.tmp_path,
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["case_count"], 11)
        self.assertEqual(report["total_units"], 335)
        self.assertEqual(report["total_groups"], 108)
        self.assertEqual(report["counts"], list(consolidate.EXPECTED_COUNTS))
        self.assertEqual(report["group_counts"], list(DEFAULT_GROUP_COUNTS))
        self.assertFalse(report["supplement_included"])
        self.assertEqual(report["supplement_item_count"], 0)

        bundle_dir = Path(report["bundle_dir"])
        self.assertEqual(bundle_dir.name, consolidate.DEFAULT_FOLDER_NAME)
        self.assertEqual(len(report["files"]), 13)

        manifest = json.loads((bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["case_count"], 11)
        self.assertEqual(manifest["total_units"], 335)
        self.assertEqual(manifest["total_groups"], 108)
        self.assertEqual(manifest["actual_counts"][9], 49)  # C10
        self.assertEqual(manifest["actual_counts"][10], 51)  # C11
        self.assertEqual(manifest["excluded_from_notion"], ["manifest.json"])
        self.assertTrue(manifest["flat_markdown_no_blank_lines"])

        # Verify C01 preserves verbatim ASR, escaped pipe, visual separation, continuous reading, Appendix A, and analysis
        c01_text = (bundle_dir / "01_C01.md").read_text(encoding="utf-8")
        self.assertIn("## 来源与核验说明", c01_text)
        self.assertIn("未经独立重新听审原音视频核对", c01_text)
        self.assertIn("## 文稿连读（依据已存文字，非重新听审）", c01_text)
        self.assertIn("## 原文逐项", c01_text)
        self.assertIn("## 显式画面与混合记录清单", c01_text)
        self.assertIn("## 语义分组分析（附录A）", c01_text)
        self.assertIn("## 分析逐项", c01_text)
        self.assertIn("A|B", c01_text)
        self.assertIn("[画面：纯画面转场，无对白]", c01_text)
        self.assertIn("既有分析（1 · 00:00-00:03）：", c01_text)
        self.assertIn("语义分组（1 · 00:00-00:03）：", c01_text)
        self.assertIn("C01-G01 机制A|机制B", c01_text)
        self.assertNotIn("口播正文记录", c01_text)

        # Verify C10 and C11 exact row identifiers preserved
        c10_text = (bundle_dir / "10_C10.md").read_text(encoding="utf-8")
        self.assertIn("A01／00:00-00:03", c10_text)
        self.assertIn("A49／", c10_text)
        c11_text = (bundle_dir / "11_C11.md").read_text(encoding="utf-8")
        self.assertIn("1／00:00-00:03", c11_text)
        self.assertIn("V02／00:03-00:06", c11_text)
        self.assertIn("B06／", c11_text)
        self.assertIn("B51／", c11_text)

        # Verify ALL generated .md files strictly < 1900 chars/line, NO blank lines, and never copy Siyuan/user scripts
        for entry in manifest["files"]:
            md_path = bundle_dir / entry["filename"]
            text = md_path.read_text(encoding="utf-8")
            self.assertNotIn(SIYUAN_NOISE, text)
            self.assertNotIn(USER_SCRIPT_NOISE, text)
            self.assertNotIn("\n\n", text)
            max_len = consolidate.validate_flat_markdown(text, entry["filename"])
            self.assertLess(max_len, 1900)
            self.assertEqual(consolidate.sha256_text(text), entry["sha256"])

        # Verify 90_source_reference.md has provenance links and no mixed Siyuan body
        ref_text = (bundle_dir / consolidate.REFERENCE_FILENAME).read_text(encoding="utf-8")
        self.assertIn("https://www.notion.so/source-page-1", ref_text)
        self.assertIn("source_analysis.md", ref_text)
        self.assertNotIn(SIYUAN_NOISE, ref_text)
        self.assertNotIn(USER_SCRIPT_NOISE, ref_text)

    def test_real_format_visual_mixed_regression_c10_c11_and_no_spoken_count(self) -> None:
        # Direct unit regressions for real-format C11 B06, B51 and C10 A05
        self.assertEqual(consolidate.classify_original_cell(REAL_C11_B06), "visual_or_non_spoken")
        self.assertEqual(consolidate.classify_original_cell(REAL_C11_B51), "visual_or_non_spoken")
        self.assertEqual(consolidate.classify_original_cell(REAL_C10_A05), "mixed_visual_and_text")
        self.assertEqual(consolidate.classify_original_cell("普通未标注已存文字"), "source_text_unverified")

        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
            source_urls=["https://www.notion.so/source-page-1"],
        )
        bundle_dir = Path(report["bundle_dir"])
        manifest = json.loads((bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8"))

        # Ensure spoken_count is removed from manifest and conservative counts are accurate
        cases_by_id = {c["case_id"]: c for c in manifest["cases"]}
        for c in manifest["cases"]:
            self.assertNotIn("spoken_count", c)
            self.assertIn("source_text_unverified_count", c)
            self.assertIn("visual_or_mixed_count", c)

        # C11 must NOT claim 51 spoken / 0 visual; B06 & B51 are visual_or_non_spoken
        c11_meta = cases_by_id["C11"]
        self.assertEqual(c11_meta["unit_count"], 51)
        self.assertEqual(c11_meta["visual_or_mixed_count"], 2)
        self.assertEqual(c11_meta["source_text_unverified_count"], 49)
        c11_rows = {r["index"]: r for r in c11_meta["rows"]}
        self.assertEqual(c11_rows[6]["classification"], "visual_or_non_spoken")
        self.assertEqual(c11_rows[51]["classification"], "visual_or_non_spoken")

        # C10 A05 must be mixed_visual_and_text (never inferred spoken)
        c10_meta = cases_by_id["C10"]
        self.assertEqual(c10_meta["unit_count"], 49)
        self.assertEqual(c10_meta["visual_or_mixed_count"], 1)
        self.assertEqual(c10_meta["source_text_unverified_count"], 48)
        c10_rows = {r["index"]: r for r in c10_meta["rows"]}
        self.assertEqual(c10_rows[5]["classification"], "mixed_visual_and_text")

        # Check rendered markdown contains no "口播正文" or fictitious "纯音乐" assertions
        for fname in ("00_index.md", "10_C10.md", "11_C11.md"):
            text = (bundle_dir / fname).read_text(encoding="utf-8")
            self.assertNotIn("口播正文", text)
            self.assertNotIn("纯音乐", text)

    def test_continuous_clean_reading_section_before_raw_original_no_loss(self) -> None:
        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
        )
        bundle_dir = Path(report["bundle_dir"])

        # Inspect C11: continuous section appears BEFORE ## 原文逐项 and excludes B06 & B51 ONLY
        c11_text = (bundle_dir / "11_C11.md").read_text(encoding="utf-8")
        idx_clean = c11_text.index("## 文稿连读（依据已存文字，非重新听审）")
        idx_raw = c11_text.index("## 原文逐项")
        idx_vis = c11_text.index("## 显式画面与混合记录清单")
        idx_grp = c11_text.index("## 语义分组分析（附录A）")
        self.assertLess(idx_clean, idx_raw)
        self.assertLess(idx_raw, idx_vis)
        self.assertLess(idx_vis, idx_grp)

        clean_section = c11_text[idx_clean:idx_raw]
        raw_section = c11_text[idx_raw:idx_vis]
        vis_section = c11_text[idx_vis:idx_grp]

        # Excluded visual rows NOT in clean reading, but ARE preserved literally in both raw and visual sections
        self.assertNotIn(REAL_C11_B06, clean_section)
        self.assertNotIn(REAL_C11_B51, clean_section)
        self.assertIn(REAL_C11_B06, raw_section)
        self.assertIn(REAL_C11_B51, raw_section)
        self.assertIn(REAL_C11_B06, vis_section)
        self.assertIn(REAL_C11_B51, vis_section)

        # Unmarked source text rows appear verbatim in source order with no invented transitions
        self.assertIn("C11 第1条原始文字记录，保留原貌", clean_section)
        self.assertIn("C11 第5条原始文字记录，保留原貌", clean_section)
        self.assertIn("C11 第7条原始文字记录，保留原貌", clean_section)
        self.assertLess(
            clean_section.index("C11 第5条原始文字记录，保留原貌"),
            clean_section.index("C11 第7条原始文字记录，保留原貌"),
        )

        # C10 A05 (ambiguous mixed) also excluded from clean reading, preserved in raw + visual/mixed sections
        c10_text = (bundle_dir / "10_C10.md").read_text(encoding="utf-8")
        c10_clean = c10_text[
            c10_text.index("## 文稿连读（依据已存文字，非重新听审）") : c10_text.index("## 原文逐项")
        ]
        c10_raw = c10_text[c10_text.index("## 原文逐项") : c10_text.index("## 显式画面与混合记录清单")]
        c10_vis = c10_text[
            c10_text.index("## 显式画面与混合记录清单") : c10_text.index("## 语义分组分析（附录A）")
        ]
        self.assertNotIn(REAL_C10_A05, c10_clean)
        self.assertIn(REAL_C10_A05, c10_raw)
        self.assertIn(REAL_C10_A05, c10_vis)

    def test_appendix_a_108_groups_included_and_invalid_appendix_a_fails_before_write(self) -> None:
        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
        )
        bundle_dir = Path(report["bundle_dir"])
        manifest = json.loads((bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["total_groups"], 108)
        self.assertEqual(sum(c["group_count"] for c in manifest["cases"]), 108)

        # Verify all 7 columns of Appendix A are losslessly rendered in C09 (8 groups)
        c09_text = (bundle_dir / "09_C09.md").read_text(encoding="utf-8")
        for g in range(1, 9):
            gid = f"C09-G{g:02d}"
            self.assertIn(f"### 分组 {g:02d}｜{gid}", c09_text)
            self.assertIn(f"mechanisms（{gid}）：{gid} 机制A|机制B", c09_text)
            self.assertIn(f"read-before（{gid}）：{gid} 读到这里前状态", c09_text)
            self.assertIn(f"change（{gid}）：{gid} 本组造成的认知变化", c09_text)
            self.assertIn(f"position/reason（{gid}）：{gid} 放置在此位置的原因", c09_text)
            self.assertIn(f"next-expectation（{gid}）：{gid} 引发的下一组期待", c09_text)
            self.assertIn(f"deletion-loss（{gid}）：{gid} 删掉后的结构损失", c09_text)

        # Missing Appendix A must fail before creating output
        no_a = self.tmp_path / "no_appendix_a.md"
        no_a.write_text(build_synthetic_source_markdown(include_appendix_a=False), encoding="utf-8")
        never_out = self.tmp_path / "never_appendix_a"
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(source_path=no_a, output_dir=never_out)
        self.assertFalse(never_out.exists())

        # Wrong Appendix A group count (107 instead of 108) must fail before creating output
        bad_groups = list(DEFAULT_GROUP_COUNTS)
        bad_groups[8] = 7  # 107 total
        bad_a = self.tmp_path / "bad_groups_107.md"
        bad_a.write_text(build_synthetic_source_markdown(group_counts=tuple(bad_groups)), encoding="utf-8")
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(source_path=bad_a, output_dir=never_out)
        self.assertFalse(never_out.exists())

    def test_optional_supplement_44_items_markup_preserved_and_siyuan_excluded(self) -> None:
        """Primary real Notion 22-page export format: 11 four-column Markdown tables x 4 data rows = 44 rows."""
        supp_file = self.tmp_path / "supplement_22.md"
        supp_file.write_text(build_synthetic_supplement_markdown(), encoding="utf-8")

        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
            source_urls=["https://www.notion.so/source-main"],
            supplement_path=supp_file,
            supplement_urls=["https://www.notion.so/supplement-22"],
            vault_dir=self.tmp_path,
        )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["total_units"], 335)
        self.assertEqual(report["total_groups"], 108)
        self.assertTrue(report["supplement_included"])
        self.assertEqual(report["supplement_format"], "markdown_table")
        self.assertEqual(report["supplement_table_count"], 11)
        self.assertEqual(report["supplement_item_count"], 44)
        self.assertEqual(report["supplement_counts"], [4] * 11)
        self.assertEqual(report["supplement_sha256"], consolidate.sha256_bytes(supp_file.read_bytes()))

        bundle_dir = Path(report["bundle_dir"])
        manifest = json.loads((bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertTrue(manifest["supplement_included"])
        self.assertEqual(manifest["supplement_format"], "markdown_table")
        self.assertEqual(manifest["supplement_table_count"], 11)
        self.assertEqual(manifest["supplement_item_count"], 44)
        self.assertEqual(manifest["supplement_urls"], ["https://www.notion.so/supplement-22"])
        self.assertEqual(manifest["supplement_sha256"], report["supplement_sha256"])

        expected_headers = [
            "连续原段／阅读摘要",
            "措辞与对象为什么这样组织",
            "与前后段的接口",
            "机械迁移怎样失效",
        ]
        # Verify ALL 11 cases x 4 rows x 4 columns (176 cells) + column headers & titles are preserved in flat paragraphs
        total_cells_checked = 0
        for case_meta in manifest["cases"]:
            cid = case_meta["case_id"]
            self.assertEqual(case_meta["supplement_format"], "markdown_table")
            self.assertEqual(case_meta["supplement_table_headers"], expected_headers)
            self.assertEqual(case_meta["supplement_item_count"], 4)
            case_text = (bundle_dir / case_meta["filename"]).read_text(encoding="utf-8")
            self.assertIn("## 逐段补研究（黑桃9补充材料）", case_text)
            self.assertIn(f"补研究原小节标题：{cid}｜{case_meta['supplement_title']}", case_text)
            for item_meta in case_meta["supplement_items"]:
                self.assertEqual(len(item_meta["cells"]), 4)
                self.assertEqual(len(item_meta["fields"]), 4)
                for field in item_meta["fields"]:
                    hdr = field["header"]
                    val = field["text"]
                    self.assertIn(f"{hdr}（{cid}-补{item_meta['number']}）：{val}", case_text)
                    total_cells_checked += 1
        self.assertEqual(total_cells_checked, 176)

        # Check C01 preserves <br>, <br/>, escaped pipe, rich markup literally as flat paragraphs
        c01_text = (bundle_dir / "01_C01.md").read_text(encoding="utf-8")
        self.assertIn("第一段<br>第二段", c01_text)
        self.assertIn("措辞A|措辞B", c01_text)
        self.assertIn("**加粗强调**", c01_text)
        self.assertIn("承接开场悬念<br/>自然过渡到第2段演示", c01_text)
        self.assertIn("`inline_code`", c01_text)
        self.assertIn("https://www.notion.so/supplement-22", c01_text)
        self.assertIn(manifest["supplement_sha256"], c01_text)

        # Verify 00_index.md and 90_source_reference.md report 11 tables / 44 rows
        self.assertIn("11 表 / 44 行", (bundle_dir / consolidate.INDEX_FILENAME).read_text(encoding="utf-8"))
        self.assertIn("共 11 表", (bundle_dir / consolidate.REFERENCE_FILENAME).read_text(encoding="utf-8"))

        # Verify ALL 13 generated .md files strictly exclude S01/Siyuan/user-final-script and pass flat validation
        for entry in manifest["files"]:
            text = (bundle_dir / entry["filename"]).read_text(encoding="utf-8")
            self.assertNotIn(SIYUAN_NOISE, text)
            self.assertNotIn(USER_SCRIPT_NOISE, text)
            self.assertNotIn("S01｜", text)
            self.assertNotIn("思远11版本", text)
            max_len = consolidate.validate_flat_markdown(text, entry["filename"])
            self.assertLess(max_len, 1900)

        # Verify CLI --vault alias and --supplement / --supplement-url work end to end
        cli_out = self.tmp_path / "cli_supp_out"
        cli_out.mkdir()
        out_buf = io.StringIO()
        with contextlib.redirect_stdout(out_buf):
            rc = consolidate.main(
                [
                    "build",
                    "--source",
                    str(self.source_file),
                    "--output",
                    str(cli_out),
                    "--vault",
                    str(self.tmp_path),
                    "--supplement",
                    str(supp_file),
                    "--supplement-url",
                    "https://www.notion.so/supplement-22",
                ]
            )
        self.assertEqual(rc, 0)
        cli_report = json.loads(out_buf.getvalue())
        self.assertEqual(cli_report["supplement_format"], "markdown_table")
        self.assertEqual(cli_report["supplement_table_count"], 11)
        self.assertEqual(cli_report["supplement_item_count"], 44)
        self.assertEqual(cli_report["total_groups"], 108)

    def test_optional_supplement_numbered_variant_also_supported(self) -> None:
        """Also support the 11x4 numbered-list supplement variant alongside the primary 11-table format."""
        supp_num = self.tmp_path / "supplement_numbered.md"
        supp_num.write_text(build_synthetic_supplement_numbered_markdown(), encoding="utf-8")
        out_dir = self.tmp_path / "numbered_variant_out"
        out_dir.mkdir()

        report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=out_dir,
            supplement_path=supp_num,
            supplement_urls=["https://www.notion.so/supplement-numbered"],
        )
        self.assertEqual(report["supplement_format"], "numbered_list")
        self.assertEqual(report["supplement_table_count"], 0)
        self.assertEqual(report["supplement_item_count"], 44)
        self.assertEqual(report["supplement_counts"], [4] * 11)

        bundle_dir = Path(report["bundle_dir"])
        c01_text = (bundle_dir / "01_C01.md").read_text(encoding="utf-8")
        self.assertIn("第一段<br>第二段", c01_text)
        self.assertIn("缩进子项原样保留不丢弃", c01_text)
        self.assertNotIn(SIYUAN_NOISE, c01_text)

    def test_optional_supplement_malformed_truncated_or_siyuan_leak_fails(self) -> None:
        never_out = self.tmp_path / "never_supp"

        # 1. Truncated table supplement (stops at C09 -> only 9 tables / 36 rows)
        trunc = self.tmp_path / "supp_trunc.md"
        trunc.write_text(build_synthetic_supplement_markdown(truncate_after_case=9), encoding="utf-8")
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=trunc
            )
        self.assertFalse(never_out.exists())

        # 2. Missing 4th data row in C11 table (43 != 44)
        missing_row = self.tmp_path / "supp_missing_row.md"
        missing_row.write_text(
            build_synthetic_supplement_markdown(drop_row_in_case="C11"), encoding="utf-8"
        )
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=missing_row
            )
        self.assertFalse(never_out.exists())

        # 3. Extra 5th data row in C10 table (45 != 44)
        extra_row = self.tmp_path / "supp_extra_row.md"
        extra_row.write_text(
            build_synthetic_supplement_markdown(extra_row_in_case="C10"), encoding="utf-8"
        )
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=extra_row
            )
        self.assertFalse(never_out.exists())

        # 4. Wrong column count (3 columns instead of 4) in C05 table
        bad_cols = self.tmp_path / "supp_bad_cols.md"
        bad_cols.write_text(
            build_synthetic_supplement_markdown(wrong_col_count_in_case="C05"), encoding="utf-8"
        )
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=bad_cols
            )
        self.assertFalse(never_out.exists())

        # 5. Unexpected S01/Siyuan ### heading leaked inside Black9 section
        leaked = self.tmp_path / "supp_leaked_siyuan.md"
        leaked.write_text(
            build_synthetic_supplement_markdown(inject_siyuan_inside_black9=True), encoding="utf-8"
        )
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=leaked
            )
        self.assertFalse(never_out.exists())

        # 6. Numbered-list variant missing 4th item in C11 also fails
        bad_num = self.tmp_path / "supp_bad_num.md"
        bad_num.write_text(
            build_synthetic_supplement_numbered_markdown(drop_item_in_case="C11"), encoding="utf-8"
        )
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file, output_dir=never_out, supplement_path=bad_num
            )
        self.assertFalse(never_out.exists())

        # 7. --supplement-url without --supplement fails cleanly
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(
                source_path=self.source_file,
                output_dir=never_out,
                supplement_urls=["https://www.notion.so/orphan-url"],
            )

    def test_idempotent_builder_and_conflict_protection(self) -> None:
        first = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
            source_urls=["https://www.notion.so/source-page-1"],
        )
        second = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.output_root,
            source_urls=["https://www.notion.so/source-page-1"],
        )
        self.assertEqual(second["manifest_action"], "identical")
        self.assertTrue(all(f["action"] == "identical" for f in second["files"]))
        self.assertEqual(first["manifest_sha256"], second["manifest_sha256"])

        # Unexpected extra file in destination must be rejected without deletion.
        bundle_dir = Path(first["bundle_dir"])
        user_note = bundle_dir / "user_existing_note.md"
        user_note.write_text("keep me", encoding="utf-8")
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(
                source_path=self.source_file,
                output_dir=self.output_root,
                source_urls=["https://www.notion.so/source-page-1"],
            )
        self.assertTrue(user_note.exists())
        user_note.unlink()

        # Modified existing file must fail instead of overwriting.
        c01 = bundle_dir / "01_C01.md"
        c01.write_text("user edited content\n", encoding="utf-8")
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(
                source_path=self.source_file,
                output_dir=self.output_root,
                source_urls=["https://www.notion.so/source-page-1"],
            )
        self.assertEqual(c01.read_text(encoding="utf-8"), "user edited content\n")

    def test_preflight_count_mismatch_fails_before_publishing(self) -> None:
        bad_counts = list(consolidate.EXPECTED_COUNTS)
        bad_counts[9] = 48  # C10 missing 1 row
        bad_src = self.tmp_path / "bad_counts.md"
        bad_src.write_text(build_synthetic_source_markdown(counts=tuple(bad_counts)), encoding="utf-8")

        target_out = self.tmp_path / "never_created"
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(source_path=bad_src, output_dir=target_out)
        self.assertFalse(target_out.exists())

        # Missing C11 entirely also fails before publishing
        missing_c11 = self.tmp_path / "missing_c11.md"
        missing_c11.write_text(build_synthetic_source_markdown(include_c11=False), encoding="utf-8")
        with self.assertRaises(consolidate.SourceParseError):
            consolidate.build_bundle(source_path=missing_c11, output_dir=target_out)
        self.assertFalse(target_out.exists())

    def test_symlink_traversal_rejected(self) -> None:
        real_dir = self.tmp_path / "real_dir"
        real_dir.mkdir()
        link_out = self.tmp_path / "link_out"
        link_out.symlink_to(real_dir)

        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(source_path=self.source_file, output_dir=link_out)

        # Symlinked source file rejected
        link_src = self.tmp_path / "link_source.md"
        link_src.symlink_to(self.source_file)
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(source_path=link_src, output_dir=self.output_root)

        # Symlink inside destination directory rejected
        bundle_dir = self.output_root / consolidate.DEFAULT_FOLDER_NAME
        bundle_dir.mkdir()
        outside = self.tmp_path / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (bundle_dir / "01_C01.md").symlink_to(outside)
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(source_path=self.source_file, output_dir=self.output_root)

    def test_consolidate_fetch_mocked_and_malformed(self) -> None:
        server = MockNotionServer()
        out_md = self.tmp_path / "fetched.md"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            res = consolidate.fetch_notion_page(ROOT_PARENT_ID, out_md)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["had_xml_wrapper"])
        self.assertIn("Existing user content.", out_md.read_text(encoding="utf-8"))
        raw_ev = json.loads(Path(res["raw_evidence_file"]).read_text(encoding="utf-8"))
        self.assertEqual(raw_ev["notion_id"], ROOT_PARENT_ID)

        # Malformed MCP response fails cleanly
        def bad_run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout='{"result":{"content":[]}}', stderr="")

        with mock.patch("subprocess.run", side_effect=bad_run):
            with self.assertRaises(consolidate.McpFetchError):
                consolidate.fetch_notion_page(ROOT_PARENT_ID, self.tmp_path / "bad.md")


class Black9SyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source_file = self.tmp_path / "source_analysis.md"
        self.source_file.write_text(build_synthetic_source_markdown(), encoding="utf-8")
        self.vault_dir = self.tmp_path / "vault"
        self.vault_dir.mkdir()
        build_report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.vault_dir,
            source_urls=["https://www.notion.so/source-doc"],
        )
        self.bundle_dir = Path(build_report["bundle_dir"])
        self.state_dir = self.tmp_path / "state"
        self.state_dir.mkdir()
        self.state_path = self.state_dir / "black9_state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_inspect_schemas_and_incompatible_schema_rejected(self) -> None:
        server = MockNotionServer()
        client = sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            inspected = client.inspect_relevant_schemas()
        self.assertEqual(inspected["status"], "ok")
        self.assertIn("notion-fetch", inspected["tools"])
        self.assertIn("notion-create-pages", inspected["tools"])
        self.assertIn("notion-update-page", inspected["tools"])
        self.assertNotIn("unrelated-notion-tool", inspected["tools"])

        # Missing required tool fails before sending writes
        bad_schema = {"tools": [standard_notion_tools_schema()["tools"][0]]}
        bad_server = MockNotionServer(tools_schema=bad_schema)
        bad_client = sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=bad_server.subprocess_run):
            with self.assertRaises(sync.SchemaValidationError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=bad_client,
                )
        self.assertEqual(bad_server.create_call_count, 0)

    def test_init_dry_run_writes_nothing(self) -> None:
        server = MockNotionServer()
        client = sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            res = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.state_path,
                client=client,
                dry_run=True,
            )
        self.assertEqual(res["status"], "dry_run")
        self.assertEqual(len(res["planned_pages"]), 13)
        self.assertEqual(server.create_call_count, 0)
        self.assertFalse(self.state_path.exists())
        self.assertFalse(sync.journal_path_for(self.state_path).exists())

    def test_safe_init_no_duplicate_on_restart_after_uncertain_send_and_recover(self) -> None:
        server = MockNotionServer()
        # Fail with malformed response AFTER remote creation on the 3rd create call (1=parent, 2=00_index, 3=C01)
        server.uncertain_on_create_index = 3
        server.uncertain_mode = "malformed_after_create"
        client = sync.MinisMcpNotionClient()

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=client,
                )

        # Must NOT have retried after the 3rd create call
        self.assertEqual(server.create_call_count, 3)

        # Restart WITHOUT --recover must refuse immediately with 0 new create calls!
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=client,
                    recover=False,
                )
        self.assertEqual(server.create_call_count, 3)

        # Explicit --recover finds the already-created C01 page via notion-search and creates ONLY the remaining 11 pages
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            recovered = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.state_path,
                client=client,
                recover=True,
            )

        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["total_tracked_pages"], 13)
        self.assertEqual(len(recovered["recovered_ops"]), 1)
        # Total creates = 1 collection parent + 13 child pages = 14 (ZERO duplicates!)
        self.assertEqual(server.create_call_count, 14)
        self.assertEqual(len(server.pages), 15)  # 1 existing root + 1 collection + 13 children
        # Original root page must remain completely unmodified
        self.assertEqual(server.pages[ROOT_PARENT_ID]["content"], "# Root\n\nExisting user content.")
        # Manifest must never be created as a Notion page
        self.assertFalse(any("manifest" in p["title"].lower() for p in server.pages.values()))

    def test_sync_lifecycle_noop_baseline_pull_push_conflict_only_and_dryrun(self) -> None:
        server = MockNotionServer()
        client = sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            init_res = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.state_path,
                client=client,
            )
        self.assertEqual(init_res["total_tracked_pages"], 13)

        # 1. No-op sync
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            noop_res = sync.run_sync(state_path=self.state_path, client=client)
        self.assertEqual(noop_res["status"], "ok")
        self.assertEqual(noop_res["summary"]["noop"], 13)

        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        c01_id = state["pages"]["C01"]["page_id"]
        c02_id = state["pages"]["C02"]["page_id"]
        c03_id = state["pages"]["C03"]["page_id"]
        c04_id = state["pages"]["C04"]["page_id"]
        c05_id = state["pages"]["C05"]["page_id"]

        # 2. Remote serialization drift (canonically equal) -> update_baseline only
        server.pages[c01_id]["content"] = (
            server.pages[c01_id]["content"].replace("\n\n", "\r\n\r\n\r\n") + "<br/>"
        )
        c01_local_before = (self.bundle_dir / "01_C01.md").read_bytes()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            base_res = sync.run_sync(state_path=self.state_path, client=client, only=["C01"])
        self.assertEqual(base_res["summary"]["update_baseline"], 1)
        self.assertEqual((self.bundle_dir / "01_C01.md").read_bytes(), c01_local_before)
        self.assertEqual(server.update_call_count, 0)
        # Immediate follow-up is noop
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            base_noop = sync.run_sync(state_path=self.state_path, client=client, only=["C01"])
        self.assertEqual(base_noop["summary"]["noop"], 1)

        # 3. Pull-only (remote edited, local matches baseline)
        server.pages[c02_id]["content"] += "\n\n远端人工补充说明。"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            pull_res = sync.run_sync(state_path=self.state_path, client=client, only=["C02"])
        self.assertEqual(pull_res["summary"]["pull"], 1)
        self.assertIn("远端人工补充说明。", (self.bundle_dir / "02_C02.md").read_text(encoding="utf-8"))

        # 4. Push-only (local edited, remote matches baseline) + post-push re-read verify
        c03_path = self.bundle_dir / "03_C03.md"
        c03_path.write_text(
            c03_path.read_text(encoding="utf-8") + "\n本地人工校对备注。\n",
            encoding="utf-8",
        )
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            push_res = sync.run_sync(state_path=self.state_path, client=client, only=["C03"])
        self.assertEqual(push_res["summary"]["push"], 1)
        self.assertEqual(server.update_call_count, 1)
        self.assertIn("本地人工校对备注。", server.pages[c03_id]["content"])

        # 4b. Push post-read verification mismatch must fail and NOT advance baseline
        c03_state_before = json.loads(self.state_path.read_text(encoding="utf-8"))["pages"]["C03"]
        c03_path.write_text(
            c03_path.read_text(encoding="utf-8") + "\n第二次本地编辑。\n",
            encoding="utf-8",
        )
        server.truncate_on_next_update = True
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.SyncError):
                sync.run_sync(state_path=self.state_path, client=client, only=["C03"])
        c03_state_after = json.loads(self.state_path.read_text(encoding="utf-8"))["pages"]["C03"]
        self.assertEqual(
            c03_state_before["base_local_canon_sha256"],
            c03_state_after["base_local_canon_sha256"],
        )
        # Recover the push_verify_mismatch using real `sync --recover` (F05):
        # preserves truncated remote & local in conflicts/ without advancing baseline, and unlocks journal!
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            rec_res = sync.run_sync(
                state_path=self.state_path, client=client, only=["C03"], recover=True
            )
        self.assertEqual(rec_res["status"], "conflict")
        self.assertEqual(len(rec_res["recovered_ops"]), 1)
        self.assertEqual(rec_res["recovered_ops"][0]["outcome"], "conflict_preserved")
        self.assertEqual(rec_res["unresolved_journal_ops"], 0)
        # Clean up C03 conflict snapshot directory so step 5 can cleanly verify dry-run writes nothing to conflicts/
        c03_conf_dir = sync.conflicts_dir_for(self.state_path)
        if c03_conf_dir.exists():
            import shutil

            shutil.rmtree(c03_conf_dir)

        # 5. Conflict (both local and remote changed differently)
        c04_path = self.bundle_dir / "04_C04.md"
        c04_local_edit = c04_path.read_text(encoding="utf-8") + "\n本地冲突修改。\n"
        c04_remote_edit = server.pages[c04_id]["content"] + "\n\n远端冲突修改。"
        c04_path.write_text(c04_local_edit, encoding="utf-8")
        server.pages[c04_id]["content"] = c04_remote_edit

        # First verify --dry-run and --status write NOTHING (no state, no journal, no conflict files!)
        state_bytes_before = self.state_path.read_bytes()
        journal_bytes_before = sync.journal_path_for(self.state_path).read_bytes()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            dry_res = sync.run_sync(
                state_path=self.state_path, client=client, only=["C04"], dry_run=True
            )
            status_res = sync.run_sync(
                state_path=self.state_path, client=client, only=["C04"], status_only=True
            )
        self.assertEqual(dry_res["summary"]["conflict"], 1)
        self.assertEqual(status_res["summary"]["conflict"], 1)
        self.assertEqual(self.state_path.read_bytes(), state_bytes_before)
        self.assertEqual(sync.journal_path_for(self.state_path).read_bytes(), journal_bytes_before)
        self.assertFalse(sync.conflicts_dir_for(self.state_path).exists())

        # Now run real sync on C04 -> both sides untouched, snapshots saved under conflicts/C04/
        updates_before = server.update_call_count
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            conf_res = sync.run_sync(state_path=self.state_path, client=client, only=["C04"])
        self.assertEqual(conf_res["status"], "conflict")
        self.assertEqual(conf_res["summary"]["conflict"], 1)
        self.assertEqual(server.update_call_count, updates_before)
        self.assertEqual(c04_path.read_text(encoding="utf-8"), c04_local_edit)
        self.assertEqual(server.pages[c04_id]["content"], c04_remote_edit)
        cfiles = conf_res["pages"][0]["conflict_files"]
        self.assertIn("本地冲突修改。", Path(cfiles["local_snapshot"]).read_text(encoding="utf-8"))
        self.assertIn("远端冲突修改。", Path(cfiles["remote_snapshot"]).read_text(encoding="utf-8"))

        # 6. Local missing -> no hard deletion propagation to Notion
        (self.bundle_dir / "05_C05.md").unlink()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            miss_res = sync.run_sync(state_path=self.state_path, client=client, only=["C05"])
        self.assertEqual(miss_res["summary"]["local_missing"], 1)
        self.assertIn(c05_id, server.pages)

    def test_sync_symlink_and_malformed_mcp_handling(self) -> None:
        # Symlink state path rejected
        real_state = self.state_dir / "real.json"
        link_state = self.state_dir / "link.json"
        link_state.symlink_to(real_state)
        server = MockNotionServer()
        client = sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(consolidate.PathSafetyError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=link_state,
                    client=client,
                )

        # Malformed tools response fails cleanly
        def malformed_tools(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, 0, stdout="NOT JSON", stderr="")

        with mock.patch("subprocess.run", side_effect=malformed_tools):
            with self.assertRaises(sync.McpCallError):
                sync.MinisMcpNotionClient().discover_schemas()

        # isError=true response fails cleanly
        def is_error_call(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if cmd[1] == "tools":
                return server.subprocess_run(cmd, **kwargs)
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"result": {"isError": True, "content": []}}),
                stderr="",
            )

        with mock.patch("subprocess.run", side_effect=is_error_call):
            with self.assertRaises(sync.McpCallError):
                sync.MinisMcpNotionClient().fetch_page(ROOT_PARENT_ID)

    def test_no_private_payload_or_secret_in_cli_logs(self) -> None:
        server = MockNotionServer()
        out_buf = io.StringIO()
        err_buf = io.StringIO()

        out_root = self.tmp_path / "log_check_vault"
        out_root.mkdir()
        state_file = self.tmp_path / "log_check_state.json"

        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc_build = consolidate.main(
                [
                    "build",
                    "--source",
                    str(self.source_file),
                    "--output",
                    str(out_root),
                    "--source-url",
                    "https://www.notion.so/source-log-check",
                ]
            )
        self.assertEqual(rc_build, 0)

        bundle = out_root / consolidate.DEFAULT_FOLDER_NAME
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc_init = sync.main(
                    [
                        "init",
                        "--folder",
                        str(bundle),
                        "--parent",
                        ROOT_PARENT_ID,
                        "--state",
                        str(state_file),
                    ]
                )
                rc_sync = sync.main(["sync", "--state", str(state_file)])
        self.assertEqual(rc_init, 0)
        self.assertEqual(rc_sync, 0)

        # Also test secret redaction on CLI error stderr
        def leaking_error(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="",
                stderr="Auth failed with Bearer secret_tok_ABC123 and ntn_999888777",
            )

        with mock.patch("subprocess.run", side_effect=leaking_error):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc_err = sync.main(["inspect"])
        self.assertEqual(rc_err, 1)

        combined_logs = out_buf.getvalue() + "\n" + err_buf.getvalue()
        self.assertNotIn(PRIVATE_SENTINEL, combined_logs)
        self.assertNotIn(SIYUAN_NOISE, combined_logs)
        self.assertNotIn(USER_SCRIPT_NOISE, combined_logs)
        self.assertNotIn("secret_tok_ABC123", combined_logs)
        self.assertNotIn("ntn_999888777", combined_logs)
        self.assertIn("[REDACTED]", combined_logs)

    def test_uncertain_collection_parent_timeout_restart_and_recover(self) -> None:
        server = MockNotionServer()
        # Simulate timeout AFTER collection parent was actually created on call #1
        server.uncertain_on_create_index = 1
        server.uncertain_mode = "timeout_after_create"
        client = sync.MinisMcpNotionClient()

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=client,
                )
        self.assertEqual(server.create_call_count, 1)

        # Restart without --recover refuses immediately
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=client,
                )
        self.assertEqual(server.create_call_count, 1)

        # Explicit --recover discovers collection parent by unique task marker and creates ONLY 13 children
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            res = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.state_path,
                client=client,
                recover=True,
            )
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["total_tracked_pages"], 13)
        self.assertEqual(server.create_call_count, 14)

    def test_init_preflight_hash_tamper_and_sync_folder_symlink_rejection(self) -> None:
        server = MockNotionServer()
        client = sync.MinisMcpNotionClient()

        # Tamper with C10 after build -> init preflight must reject before creating any Notion page
        c10 = self.bundle_dir / "10_C10.md"
        original_c10 = c10.read_bytes()
        c10.write_bytes(original_c10 + b"\ntampered\n")
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(sync.SyncError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=self.state_path,
                    client=client,
                )
        self.assertEqual(server.create_call_count, 0)
        c10.write_bytes(original_c10)

        # Clean init succeeds
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.state_path,
                client=client,
            )

        # Path traversal in --only rejected
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(consolidate.PathSafetyError):
                sync.run_sync(state_path=self.state_path, client=client, only=["../etc/passwd"])

        # Replacing a tracked file with a symlink inside folder rejected on sync
        outside = self.tmp_path / "outside_secret.md"
        outside.write_text("outside content\n", encoding="utf-8")
        c11 = self.bundle_dir / "11_C11.md"
        c11.unlink()
        c11.symlink_to(outside)
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(consolidate.PathSafetyError):
                sync.run_sync(state_path=self.state_path, client=client, only=["C11"])


class Black9DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)

        self.source_file = self.tmp_path / "source_analysis.md"
        self.source_file.write_text(build_synthetic_source_markdown(), encoding="utf-8")
        self.supp_file = self.tmp_path / "supplement_22.md"
        self.supp_file.write_text(build_synthetic_supplement_markdown(), encoding="utf-8")

        # Build bundle in staging directory outside vault (safest workflow)
        self.staging_dir = self.tmp_path / "staging"
        self.staging_dir.mkdir()
        build_report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.staging_dir,
            source_urls=["https://www.notion.so/source-main"],
            supplement_path=self.supp_file,
            supplement_urls=["https://www.notion.so/supplement-22"],
        )
        self.bundle_dir = Path(build_report["bundle_dir"])

        # Prepare realistic local Obsidian vault with existing originals and existing .sync metadata
        self.vault_dir = self.tmp_path / "E-Made"
        self.vault_dir.mkdir()
        (self.vault_dir / "archive").mkdir()
        self.existing_original = self.vault_dir / "archive" / "black9_original_analysis.md"
        self.existing_original.write_text("ORIGINAL USER ANALYSIS DO NOT TOUCH\n", encoding="utf-8")
        (self.vault_dir / ".sync").mkdir()
        self.existing_state, self.existing_config = build_existing_emade_sync_fixtures(101)
        self.local_state_text = json.dumps(self.existing_state, ensure_ascii=False, indent=2) + "\n"
        self.local_config_text = json.dumps(self.existing_config, ensure_ascii=False, indent=2) + "\n"
        (self.vault_dir / ".sync" / "state.json").write_text(self.local_state_text, encoding="utf-8")
        (self.vault_dir / "sync-config.json").write_text(self.local_config_text, encoding="utf-8")

        # Init Notion pages via sync.py init against MockNotionServer
        self.notion_server = MockNotionServer()
        self.sync_state_path = self.tmp_path / "sync_work" / "black9_state.json"
        self.sync_state_path.parent.mkdir()
        with mock.patch("subprocess.run", side_effect=self.notion_server.subprocess_run):
            init_res = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=self.sync_state_path,
                client=sync.MinisMcpNotionClient(),
            )
        self.assertEqual(init_res["total_tracked_pages"], 13)

        # Simulate realistic Notion fetch blank-line formatting on remote child pages
        sync_state = json.loads(self.sync_state_path.read_text(encoding="utf-8"))
        for meta in sync_state["pages"].values():
            pid = meta["page_id"]
            self.notion_server.pages[pid]["content"] = self.notion_server.pages[pid]["content"].replace(
                "\n", "\n\n"
            )

        self.github_server = MockGitHubServer(
            files={
                ".sync/state.json": self.local_state_text,
                "sync-config.json": self.local_config_text,
            }
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _combined_mcp_run(self, cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if len(cmd) >= 3 and cmd[2] == "GitHub":
            return self.github_server.subprocess_run(cmd, **kwargs)
        return self.notion_server.subprocess_run(cmd, **kwargs)

    def test_deploy_prepare_preserves_101_records_verifies_remote_and_exports_push_payload(self) -> None:
        out_dir = self.tmp_path / "deploy_out"
        with mock.patch("subprocess.run", side_effect=self._combined_mcp_run):
            res = deploy.run_prepare(
                bundle_dir=self.bundle_dir,
                sync_state_path=self.sync_state_path,
                vault_dir=self.vault_dir,
                out_dir=out_dir,
                github_client=deploy.MinisMcpGitHubClient(),
                notion_client=sync.MinisMcpNotionClient(),
            )

        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["auto_published"])
        self.assertEqual(self.github_server.push_files_call_count, 0)  # NEVER auto-publishes!
        self.assertTrue(res["push_schema_validated"])
        self.assertEqual(res["registered_pages_count"], 13)
        self.assertEqual(res["push_files_count"], 16)  # 13 .md + manifest.json + .sync/state.json + sync-config.json

        # Check merge stats and full preservation of all 101 existing records + all config fields
        stats = res["merge_stats"]
        self.assertEqual(stats["existing_state_items_count"], 101)
        self.assertEqual(stats["preserved_unrelated_state_items_count"], 101)
        self.assertEqual(stats["added_state_items_count"], 13)
        self.assertEqual(stats["total_merged_state_items_count"], 114)
        self.assertEqual(stats["added_canonical_ids_count"], 13)
        self.assertEqual(stats["preserved_manually_managed_count"], 2)

        merged_state = json.loads((out_dir / "merged_state.json").read_text(encoding="utf-8"))
        self.assertEqual(merged_state["version"], 1)
        self.assertEqual(merged_state["last_synced_at"], "2026-11-26T12:00:00Z")
        self.assertEqual(len(merged_state["items"]), 114)
        for old_key, old_val in self.existing_state["items"].items():
            self.assertEqual(merged_state["items"][old_key], old_val)

        # Ensure 00_index and 90_source_reference are synced; manifest.json and old originals are NOT registered
        folder = consolidate.DEFAULT_FOLDER_NAME
        self.assertIn(f"source::{folder}/00_index.md", merged_state["items"])
        self.assertIn(f"source::{folder}/01_C01.md", merged_state["items"])
        self.assertIn(f"source::{folder}/11_C11.md", merged_state["items"])
        self.assertIn(f"source::{folder}/90_source_reference.md", merged_state["items"])
        self.assertNotIn(f"source::{folder}/manifest.json", merged_state["items"])
        self.assertNotIn("source::archive/black9_original_analysis.md", merged_state["items"])

        # Verify baseline hash matches local blank-line-free flat bytes
        c01_local_sha = consolidate.sha256_bytes((self.bundle_dir / "01_C01.md").read_bytes())
        self.assertEqual(
            merged_state["items"][f"source::{folder}/01_C01.md"]["hash"], c01_local_sha
        )
        self.assertEqual(
            merged_state["items"][f"source::{folder}/01_C01.md"]["kind"], "source"
        )
        self.assertEqual(
            merged_state["items"][f"source::{folder}/01_C01.md"]["source"], f"{folder}/01_C01.md"
        )

        # Verify sync-config.json preserves all existing fields, canonical_page_ids, and manually_managed_source_paths
        merged_config = json.loads((out_dir / "merged_sync-config.json").read_text(encoding="utf-8"))
        self.assertEqual(merged_config["root_page_id"], ROOT_PARENT_ID)
        self.assertEqual(merged_config["sync_interval_minutes"], 30)
        self.assertEqual(merged_config["extra_preserved_setting"], self.existing_config["extra_preserved_setting"])
        self.assertEqual(
            merged_config["manually_managed_source_paths"],
            self.existing_config["manually_managed_source_paths"],
        )
        self.assertEqual(len(merged_config["canonical_page_ids"]), 114)
        for old_k, old_v in self.existing_config["canonical_page_ids"].items():
            self.assertEqual(merged_config["canonical_page_ids"][old_k], old_v)
        self.assertIn(f"{folder}/00_index.md", merged_config["canonical_page_ids"])
        self.assertIn(f"{folder}/90_source_reference.md", merged_config["canonical_page_ids"])

        # Verify exported GitHub push_files payload JSON
        payload = json.loads((out_dir / "push_files_payload.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["owner"], "yinanan77")
        self.assertEqual(payload["repo"], "E-Made")
        self.assertEqual(payload["branch"], "main")
        self.assertTrue(payload["message"])
        pushed_paths = [f["path"] for f in payload["files"]]
        self.assertEqual(len(pushed_paths), 16)
        self.assertIn(f"{folder}/manifest.json", pushed_paths)
        self.assertIn(".sync/state.json", pushed_paths)
        self.assertIn("sync-config.json", pushed_paths)
        self.assertFalse(any(p.startswith(".github/") or p.startswith("scripts/") for p in pushed_paths))

        # Backups of both GitHub and local vault metadata exist and match originals
        self.assertEqual((out_dir / "backups" / "github_state.json").read_text(encoding="utf-8"), self.local_state_text)
        self.assertEqual((out_dir / "backups" / "local_state.json").read_text(encoding="utf-8"), self.local_state_text)

        # Ensure child pages created by sync.py init contain NO injected task_marker body content
        init_state = json.loads(self.sync_state_path.read_text(encoding="utf-8"))
        task_marker = init_state["task_marker"]
        for doc_id, meta in init_state["pages"].items():
            pid = meta["page_id"]
            self.assertNotIn(task_marker, self.notion_server.pages[pid]["content"], f"Marker leaked into {doc_id}")

    def test_deploy_prepare_rejects_remote_mismatch_collision_and_never_auto_publishes(self) -> None:
        init_state = json.loads(self.sync_state_path.read_text(encoding="utf-8"))
        c10_pid = init_state["pages"]["C10"]["page_id"]
        orig_c10_remote = self.notion_server.pages[c10_pid]["content"]

        # 1. Remote Notion page diverges from local canonical full content -> MUST fail before writing payload!
        self.notion_server.pages[c10_pid]["content"] = orig_c10_remote + "\n\n未同步的远端改动"
        bad_out = self.tmp_path / "fail_mismatch_out"
        with mock.patch("subprocess.run", side_effect=self._combined_mcp_run):
            with self.assertRaises(deploy.DeployError):
                deploy.run_prepare(
                    bundle_dir=self.bundle_dir,
                    sync_state_path=self.sync_state_path,
                    vault_dir=self.vault_dir,
                    out_dir=bad_out,
                    github_client=deploy.MinisMcpGitHubClient(),
                    notion_client=sync.MinisMcpNotionClient(),
                )
        self.assertFalse((bad_out / "push_files_payload.json").exists())
        self.assertEqual(self.github_server.push_files_call_count, 0)
        self.notion_server.pages[c10_pid]["content"] = orig_c10_remote

        # 2. Existing .sync/state.json has colliding key pointing to a different Notion ID -> MUST fail!
        colliding_state = json.loads(self.local_state_text)
        folder = consolidate.DEFAULT_FOLDER_NAME
        colliding_state["items"][f"source::{folder}/01_C01.md"] = {
            "id": "99999999-9999-4999-8999-999999999999",
            "kind": "source",
            "hash": "0" * 64,
            "source": f"{folder}/01_C01.md",
        }
        self.github_server.files[".sync/state.json"] = json.dumps(colliding_state, ensure_ascii=False)
        with mock.patch("subprocess.run", side_effect=self._combined_mcp_run):
            with self.assertRaises(deploy.DeployError):
                deploy.run_prepare(
                    bundle_dir=self.bundle_dir,
                    sync_state_path=self.sync_state_path,
                    vault_dir=self.vault_dir,
                    out_dir=self.tmp_path / "fail_collision_out",
                    github_client=deploy.MinisMcpGitHubClient(),
                    notion_client=sync.MinisMcpNotionClient(),
                )
        self.assertEqual(self.github_server.push_files_call_count, 0)

    def test_deploy_install_copies_only_new_folder_protects_originals_and_metadata(self) -> None:
        backup_out = self.tmp_path / "install_backups"

        # 1. Dry run writes nothing
        dry = deploy.run_install(
            bundle_dir=self.bundle_dir,
            vault_dir=self.vault_dir,
            out_dir=backup_out,
            dry_run=True,
        )
        self.assertEqual(dry["status"], "dry_run")
        self.assertEqual(len(dry["files"]), 14)  # 13 .md + manifest.json
        target_dir = self.vault_dir / consolidate.DEFAULT_FOLDER_NAME
        self.assertFalse(target_dir.exists())
        self.assertFalse(backup_out.exists())

        # 2. Real install copies ONLY new folder files, backs up metadata, does NOT overwrite local sync metadata!
        res = deploy.run_install(
            bundle_dir=self.bundle_dir,
            vault_dir=self.vault_dir,
            out_dir=backup_out,
        )
        self.assertEqual(res["status"], "ok")
        self.assertFalse(res["local_sync_metadata_overwritten"])
        self.assertTrue(target_dir.is_dir())
        self.assertEqual(len(list(target_dir.iterdir())), 14)
        # Local original and old sync metadata remain completely untouched
        self.assertEqual(
            self.existing_original.read_text(encoding="utf-8"),
            "ORIGINAL USER ANALYSIS DO NOT TOUCH\n",
        )
        self.assertEqual(
            (self.vault_dir / ".sync" / "state.json").read_text(encoding="utf-8"),
            self.local_state_text,
        )
        self.assertEqual(
            (self.vault_dir / "sync-config.json").read_text(encoding="utf-8"),
            self.local_config_text,
        )
        self.assertEqual(
            (backup_out / "backups" / "local_state.json").read_text(encoding="utf-8"),
            self.local_state_text,
        )

        # 3. Byte-identical re-install is idempotent
        again = deploy.run_install(
            bundle_dir=self.bundle_dir,
            vault_dir=self.vault_dir,
            out_dir=backup_out,
        )
        self.assertTrue(all(f["action"] == "identical" for f in again["files"]))

        # 4. Non-identical existing file in target folder is rejected without overwrite
        c01_dst = target_dir / "01_C01.md"
        c01_dst.write_text("LOCAL USER EDIT\n", encoding="utf-8")
        with self.assertRaises(consolidate.PathSafetyError):
            deploy.run_install(bundle_dir=self.bundle_dir, vault_dir=self.vault_dir)
        self.assertEqual(c01_dst.read_text(encoding="utf-8"), "LOCAL USER EDIT\n")

        # 5. Path traversal in --folder-relpath rejected
        with self.assertRaises(consolidate.PathSafetyError):
            deploy.run_install(
                bundle_dir=self.bundle_dir,
                vault_dir=self.vault_dir,
                folder_relpath="../escape",
            )

        # 6. Explicit --install-metadata-from requires --out backup dir and backs up BEFORE writing
        c01_dst.write_bytes((self.bundle_dir / "01_C01.md").read_bytes())
        prep_out = self.tmp_path / "prep_for_meta"
        with mock.patch("subprocess.run", side_effect=self._combined_mcp_run):
            deploy.run_prepare(
                bundle_dir=self.bundle_dir,
                sync_state_path=self.sync_state_path,
                vault_dir=self.vault_dir,
                out_dir=prep_out,
                github_client=deploy.MinisMcpGitHubClient(),
                notion_client=sync.MinisMcpNotionClient(),
            )
        with self.assertRaises(deploy.DeployError):
            deploy.run_install(
                bundle_dir=self.bundle_dir,
                vault_dir=self.vault_dir,
                install_metadata_from=prep_out,
            )
        meta_backup_dir = self.tmp_path / "meta_install_backup"
        meta_res = deploy.run_install(
            bundle_dir=self.bundle_dir,
            vault_dir=self.vault_dir,
            out_dir=meta_backup_dir,
            install_metadata_from=prep_out,
        )
        self.assertTrue(meta_res["local_sync_metadata_overwritten"])
        self.assertEqual(
            (meta_backup_dir / "backups" / "local_state.json").read_text(encoding="utf-8"),
            self.local_state_text,
        )
        self.assertEqual(
            len(json.loads((self.vault_dir / ".sync" / "state.json").read_text(encoding="utf-8"))["items"]),
            114,
        )

    def test_deploy_cli_inspect_prepare_install_no_private_payload_or_secret_in_logs(self) -> None:
        out_buf = io.StringIO()
        err_buf = io.StringIO()
        cli_out = self.tmp_path / "cli_deploy_out"

        with mock.patch("subprocess.run", side_effect=self._combined_mcp_run):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc_insp = deploy.main(["inspect"])
                rc_prep = deploy.main(
                    [
                        "prepare",
                        "--bundle",
                        str(self.bundle_dir),
                        "--sync-state",
                        str(self.sync_state_path),
                        "--vault",
                        str(self.vault_dir),
                        "--out",
                        str(cli_out),
                    ]
                )
                rc_inst = deploy.main(
                    [
                        "install",
                        "--bundle",
                        str(self.bundle_dir),
                        "--vault",
                        str(self.vault_dir),
                        "--out",
                        str(cli_out),
                    ]
                )

        self.assertEqual(rc_insp, 0)
        self.assertEqual(rc_prep, 0)
        self.assertEqual(rc_inst, 0)

        # Verify secret redaction on error stderr
        def leaking_gh_err(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd, 1, stdout="", stderr="GitHub failed Bearer secret_gh_XYZ123"
            )

        with mock.patch("subprocess.run", side_effect=leaking_gh_err):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc_err = deploy.main(["inspect"])
        self.assertEqual(rc_err, 1)

        logs = out_buf.getvalue() + "\n" + err_buf.getvalue()
        self.assertNotIn(PRIVATE_SENTINEL, logs)
        self.assertNotIn(SIYUAN_NOISE, logs)
        self.assertNotIn(USER_SCRIPT_NOISE, logs)
        self.assertNotIn("secret_gh_XYZ123", logs)
        self.assertIn("[REDACTED]", logs)


class Black9AuditRegressionTests(unittest.TestCase):
    """Regression protection suite verifying every independent audit finding (F01-F10) is FIXED."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self.source_file = self.tmp_path / "source_analysis.md"
        self.source_file.write_text(build_synthetic_source_markdown(), encoding="utf-8")
        self.vault_dir = self.tmp_path / "vault"
        self.vault_dir.mkdir()
        build_report = consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.vault_dir,
            source_urls=["https://www.notion.so/source-audit-doc"],
        )
        self.bundle_dir = Path(build_report["bundle_dir"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_f01_update_page_explicit_allow_async_false_and_async_task_handling(self) -> None:
        """[F01] Flat runtime notion-update-page explicitly sends allow_async=False; async_task is detected and recovered safely."""
        # Part A: Schema with optional allow_async AND schema with required allow_async both send allow_async=False.
        for allow_async_req in (False, True):
            server = MockNotionServer(
                tools_schema=runtime_flat_notion_tools_schema(allow_async_required=allow_async_req)
            )
            client = sync.MinisMcpNotionClient()
            state_path = self.tmp_path / f"state_f01_{allow_async_req}" / "state.json"
            with mock.patch("subprocess.run", side_effect=server.subprocess_run):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_path,
                    client=client,
                )
                c01_path = self.bundle_dir / "01_C01.md"
                orig_c01 = c01_path.read_text(encoding="utf-8")
                c01_path.write_text(orig_c01 + f"\nF01同步校验_{allow_async_req}。\n", encoding="utf-8")
                res = sync.run_sync(state_path=state_path, client=client, only=["C01"])
                c01_path.write_text(orig_c01, encoding="utf-8")

            self.assertEqual(res["summary"]["push"], 1)
            self.assertEqual(len(server.update_payloads), 1)
            self.assertIn("allow_async", server.update_payloads[0])
            self.assertIs(server.update_payloads[0]["allow_async"], False)

        # Part B: If Notion still returns {"async_task": ...}, raise AsyncUpdatePendingError,
        # journal push_async_pending (NOT push_verify_mismatch), block normal sync, and recover cleanly via sync --recover!
        server_async = MockNotionServer(
            tools_schema=runtime_flat_notion_tools_schema(),
            return_async_task_on_update=True,
        )
        client_async = sync.MinisMcpNotionClient()
        state_async = self.tmp_path / "state_f01_async" / "state.json"
        with mock.patch("subprocess.run", side_effect=server_async.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_async,
                client=client_async,
            )
            c02_id = json.loads(state_async.read_text(encoding="utf-8"))["pages"]["C02"]["page_id"]
            c02_path = self.bundle_dir / "02_C02.md"
            updated_c02 = c02_path.read_text(encoding="utf-8") + "\n异步任务测试段落。\n"
            c02_path.write_text(updated_c02, encoding="utf-8")

            with self.assertRaises(sync.AsyncUpdatePendingError):
                sync.run_sync(state_path=state_async, client=client_async, only=["C02"])

            unresolved = sync.find_unresolved_journal_ops(sync.load_journal(state_async))
            self.assertEqual(len(unresolved), 1)
            self.assertEqual(unresolved[0]["status"], "push_async_pending")

            # Normal mutating sync refuses until explicit recovery.
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_sync(state_path=state_async, client=client_async, only=["C02"])

            # Simulate Notion finishing the async task in the background, then run `sync --recover`.
            server_async.pages[c02_id]["content"] = updated_c02
            rec = sync.run_sync(
                state_path=state_async, client=client_async, only=["C02"], recover=True
            )
            self.assertEqual(rec["status"], "ok")
            self.assertEqual(rec["unresolved_journal_ops"], 0)
            self.assertEqual(rec["recovered_ops"][0]["outcome"], "push_confirmed")
            self.assertEqual(rec["summary"]["noop"], 1)

    def test_f03_ancestor_symlink_escape_rejected_across_consolidate_sync_deploy(self) -> None:
        """[F03] Symlinked ancestor directories are rejected across consolidate, sync, and deploy with ZERO outside writes."""
        outside = self.tmp_path / "outside_target"
        outside.mkdir()
        link_dir = self.tmp_path / "link_dir"
        link_dir.symlink_to(outside)

        real_src_dir = self.tmp_path / "real_src_dir"
        real_src_dir.mkdir()
        (real_src_dir / "s.md").write_text(self.source_file.read_text(encoding="utf-8"), encoding="utf-8")
        link_src_dir = self.tmp_path / "link_src_dir"
        link_src_dir.symlink_to(real_src_dir)

        # 1. consolidate.build_bundle rejects symlinked ancestor on source AND output
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(source_path=link_src_dir / "s.md", output_dir=self.tmp_path / "out_ok")
        with self.assertRaises(consolidate.PathSafetyError):
            consolidate.build_bundle(source_path=self.source_file, output_dir=link_dir / "nested_out")

        # 2. consolidate.fetch_notion_page rejects symlinked ancestor output
        server = MockNotionServer()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(consolidate.PathSafetyError):
                consolidate.fetch_notion_page(ROOT_PARENT_ID, link_dir / "nested" / "fetched.md")

        # 3. sync.run_init rejects symlinked ancestor state path
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with self.assertRaises(consolidate.PathSafetyError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=link_dir / "nested" / "black9_state.json",
                    client=sync.MinisMcpNotionClient(),
                )
        self.assertEqual(server.create_call_count, 0)

        # 4. sync.main inspect --output rejects symlinked ancestor
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            err_buf = io.StringIO()
            with contextlib.redirect_stderr(err_buf):
                rc = sync.main(["inspect", "--output", str(link_dir / "nested" / "schemas.json")])
        self.assertEqual(rc, 1)

        # 5. deploy.run_install and deploy.run_prepare reject symlinked ancestor vault / out
        with self.assertRaises(consolidate.PathSafetyError):
            deploy.run_install(bundle_dir=self.bundle_dir, vault_dir=link_dir / "nested_vault")
        with self.assertRaises(consolidate.PathSafetyError):
            deploy.run_prepare(
                bundle_dir=self.bundle_dir,
                sync_state_path=self.tmp_path / "nonexistent_state.json",
                vault_dir=self.vault_dir,
                out_dir=link_dir / "nested_prepare",
                github_client=deploy.MinisMcpGitHubClient(),
                notion_client=sync.MinisMcpNotionClient(),
            )

        # ZERO files or directories created inside outside_target!
        self.assertEqual(list(outside.iterdir()), [])

    def test_f04_recover_strict_parent_child_no_hijack_and_saved_page_id_without_search(self) -> None:
        """[F04] init --recover enforces strict parent-child ownership (never hijacks another parent's page) and prioritizes saved page_id."""
        # Scenario A: Earlier collection exists under OLD_PARENT_ID; new init under ROOT_PARENT_ID
        # times out BEFORE creating C01 remotely.
        server = MockNotionServer(
            tools_schema=runtime_flat_notion_tools_schema(),
            extra_roots=[OLD_PARENT_ID],
        )
        client = sync.MinisMcpNotionClient()
        old_state = self.tmp_path / "old_state" / "state.json"
        prod_state = self.tmp_path / "prod_state" / "state.json"

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=OLD_PARENT_ID,
                state_path=old_state,
                client=client,
            )
            old_c01_id = json.loads(old_state.read_text(encoding="utf-8"))["pages"]["C01"]["page_id"]

            # 3rd create call (1=collection parent, 2=00_index, 3=C01) times out BEFORE Notion creates C01.
            server.create_call_count = 0
            server.fail_create_before_remote_at = 3
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=prod_state,
                    client=client,
                )

            # Default `init --recover` MUST NOT hijack old_c01_id from OLD_PARENT_ID, and refuses auto-recreate without opt-in!
            with self.assertRaises(sync.SyncError) as ctx_no_recreate:
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=prod_state,
                    client=client,
                    recover=True,
                )
            self.assertIn("--allow-recreate-missing", str(ctx_no_recreate.exception))

            # Explicit `--recover --allow-recreate-missing` creates a fresh C01 under ROOT_PARENT_ID's collection parent!
            rec = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=prod_state,
                client=client,
                recover=True,
                allow_recreate_missing=True,
            )
            self.assertEqual(rec["status"], "ok")
            prod_data = json.loads(prod_state.read_text(encoding="utf-8"))
            prod_c01_id = prod_data["pages"]["C01"]["page_id"]
            self.assertNotEqual(prod_c01_id, old_c01_id)
            self.assertEqual(
                server.pages[prod_c01_id]["parent_id"], prod_data["collection_parent_id"]
            )

        # Scenario B: Post-create fetch_page fails transiently AFTER page_id is persisted in state.json,
        # AND notion-search is completely absent from schema -> `--recover` MUST succeed using saved page_id!
        server_b = MockNotionServer(
            tools_schema=runtime_flat_notion_tools_schema(include_search=False),
            fail_fetch_after_create_id=True,
        )
        client_b = sync.MinisMcpNotionClient()
        state_b = self.tmp_path / "state_f04_b" / "state.json"
        with mock.patch("subprocess.run", side_effect=server_b.subprocess_run):
            with self.assertRaises(sync.McpCallError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_b,
                    client=client_b,
                )
            st_mid = json.loads(state_b.read_text(encoding="utf-8"))
            saved_index_id = st_mid["pages"]["00_index"]["page_id"]
            self.assertTrue(saved_index_id)

            rec_b = sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_b,
                client=client_b,
                recover=True,
            )
            self.assertEqual(rec_b["status"], "ok")
            self.assertEqual(rec_b["total_tracked_pages"], 13)
            st_after = json.loads(state_b.read_text(encoding="utf-8"))
            self.assertEqual(st_after["pages"]["00_index"]["page_id"], saved_index_id)
            self.assertTrue(st_after["pages"]["00_index"]["base_remote_canon_sha256"])
            # Zero duplicate pages created (1 collection + 13 children = 14)
            self.assertEqual(server_b.create_call_count, 14)

    def test_f05_notion_autolink_canonical_match_schema_preflight_and_sync_recover_outcomes(self) -> None:
        """[F05] Notion URL autolinking passes post-push verify; SchemaValidationError never poisons journal; sync --recover handles all 3 outcomes."""
        # Part A: Notion read-back autolinks bare URLs as [url](url) -> post-push verify MUST succeed!
        server = MockNotionServer(
            tools_schema=runtime_flat_notion_tools_schema(),
            autolink_urls_on_fetch=True,
        )
        client = sync.MinisMcpNotionClient()
        state_path = self.tmp_path / "state_f05_auto" / "state.json"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c01_path = self.bundle_dir / "01_C01.md"
            orig_c01_bytes = c01_path.read_bytes()
            c01_path.write_text(
                c01_path.read_text(encoding="utf-8") + "\n正常校对备注（含自动链接兼容）。\n",
                encoding="utf-8",
            )
            push_res = sync.run_sync(state_path=state_path, client=client, only=["C01"])
            self.assertEqual(push_res["status"], "ok")
            self.assertEqual(push_res["summary"]["push"], 1)
            self.assertEqual(sync.find_unresolved_journal_ops(sync.load_journal(state_path)), [])

            # Also test `sync --recover` push_not_applied outcome (timeout BEFORE remote write)
            c02_path = self.bundle_dir / "02_C02.md"
            orig_c02_bytes = c02_path.read_bytes()
            c02_path.write_text(
                c02_path.read_text(encoding="utf-8") + "\n推送超时未落库测试。\n",
                encoding="utf-8",
            )
            server.timeout_on_next_update = True
            with self.assertRaises(sync.UncertainWriteError):
                sync.run_sync(state_path=state_path, client=client, only=["C02"])
            updates_before_rec = server.update_call_count
            rec_not_applied = sync.run_sync(
                state_path=state_path, client=client, only=["C02"], recover=True
            )
            self.assertEqual(rec_not_applied["status"], "ok")
            self.assertEqual(rec_not_applied["unresolved_journal_ops"], 0)
            self.assertEqual(rec_not_applied["recovered_ops"][0]["outcome"], "push_not_applied")
            # No surprise automatic re-push during --recover
            self.assertEqual(server.update_call_count, updates_before_rec)
            # Subsequent normal sync pushes cleanly!
            retry_push = sync.run_sync(state_path=state_path, client=client, only=["C02"])
            self.assertEqual(retry_push["summary"]["push"], 1)
            # Restore original bundle bytes before Part B preflight init tests
            c01_path.write_bytes(orig_c01_bytes)
            c02_path.write_bytes(orig_c02_bytes)

        # Part B: SchemaValidationError during init OR sync MUST leave ZERO send_started / push_started in journal!
        bad_create_schema = runtime_flat_notion_tools_schema()
        for t in bad_create_schema["tools"]:
            if t["name"] == "notion-create-pages":
                t["inputSchema"] = {
                    "type": "object",
                    "properties": {"unexpected_only": {"type": "string"}},
                    "required": ["unexpected_only"],
                }
        bad_server = MockNotionServer(tools_schema=bad_create_schema)
        state_bad_create = self.tmp_path / "state_f05_bad_create" / "state.json"
        with mock.patch("subprocess.run", side_effect=bad_server.subprocess_run):
            with self.assertRaises(sync.SchemaValidationError):
                sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_bad_create,
                    client=sync.MinisMcpNotionClient(),
                )
        self.assertEqual(bad_server.create_call_count, 0)
        self.assertEqual(sync.find_unresolved_journal_ops(sync.load_journal(state_bad_create)), [])

        # Also verify SchemaValidationError during sync push leaves ZERO push_started in journal!
        bad_update_schema = runtime_flat_notion_tools_schema()
        for t in bad_update_schema["tools"]:
            if t["name"] == "notion-update-page":
                t["inputSchema"] = {
                    "type": "object",
                    "properties": {"unknown_req": {"type": "string"}},
                    "required": ["unknown_req"],
                }
        server_bad_upd = MockNotionServer(tools_schema=bad_update_schema)
        client_bad_upd = sync.MinisMcpNotionClient()
        state_bad_upd = self.tmp_path / "state_f05_bad_upd" / "state.json"
        with mock.patch("subprocess.run", side_effect=server_bad_upd.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_bad_upd,
                client=client_bad_upd,
            )
            c03_path = self.bundle_dir / "03_C03.md"
            c03_path.write_text(c03_path.read_text(encoding="utf-8") + "\n模式错误前检查。\n", encoding="utf-8")
            with self.assertRaises(sync.SchemaValidationError):
                sync.run_sync(state_path=state_bad_upd, client=client_bad_upd, only=["C03"])
        self.assertEqual(server_bad_upd.update_call_count, 0)
        self.assertEqual(sync.find_unresolved_journal_ops(sync.load_journal(state_bad_upd)), [])

    def test_f06_xml_literal_content_self_close_preserved_and_unwrapped_or_empty_pull_refused(self) -> None:
        """[F06] Literal <content/> in body does NOT wipe page; unwrapped warnings and sudden empty remote body refuse pull."""
        # Part A: Literal <content/> inside valid <content>...</content> preserves full body!
        raw_with_literal = (
            '<page url="https://www.notion.so/x">\n'
            '<properties>{"title":"C01"}</properties>\n'
            "<content>\n# C01\n\n注意不要误用空标签 <content/> 在正文中。\n</content>\n</page>"
        )
        extracted, had_wrapper = consolidate.extract_notion_content_xml(raw_with_literal)
        self.assertTrue(had_wrapper)
        self.assertIn("注意不要误用空标签 <content/> 在正文中。", extracted)

        # Standalone empty <content/> with no paired <content>...</content> still returns ("", True)
        raw_empty = (
            '<page url="https://www.notion.so/empty">\n'
            '<properties>{"title":"Empty"}</properties>\n'
            "<content/>\n</page>"
        )
        empty_body, empty_wrap = consolidate.extract_notion_content_xml(raw_empty)
        self.assertEqual(empty_body, "")
        self.assertTrue(empty_wrap)

        # Part B: Unwrapped non-XML warning text is rejected by fetch_page and MUST NOT overwrite local file!
        server = MockNotionServer(tools_schema=runtime_flat_notion_tools_schema())
        client = sync.MinisMcpNotionClient()
        state_path = self.tmp_path / "state_f06" / "state.json"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c02_id = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["C02"]["page_id"]
            c02_path = self.bundle_dir / "02_C02.md"
            orig_c02_bytes = c02_path.read_bytes()

            server.next_fetch_raw_override[c02_id] = "Warning: page temporarily truncated by upstream gateway"
            with self.assertRaises(sync.McpCallError):
                sync.run_sync(state_path=state_path, client=client, only=["C02"])
            self.assertEqual(c02_path.read_bytes(), orig_c02_bytes)

            # Part C: Sudden empty wrapped <content/> on remote MUST NOT wipe non-empty local file!
            server.next_fetch_raw_override[c02_id] = (
                f'<page url="https://www.notion.so/{c02_id.replace("-", "")}">\n'
                '<properties>{"title":"C02"}</properties>\n<content/>\n</page>'
            )
            with self.assertRaises(sync.SyncError):
                sync.run_sync(state_path=state_path, client=client, only=["C02"])
            self.assertEqual(c02_path.read_bytes(), orig_c02_bytes)

    def test_f07_audit_visual_and_ambiguous_samples_never_classified_as_unverified_text(self) -> None:
        """[F07] Visually ambiguous and pure visual cells from audit are never labeled source_text_unverified."""
        samples = [
            "画面展示产品对比",
            "[特写镜头，推近]",
            "（纯动作演示，未说话）",
            "【片头LOGO】",
        ]
        for s in samples:
            cls = consolidate.classify_original_cell(s)
            self.assertIn(
                cls,
                ("visual_or_non_spoken", "mixed_visual_and_text"),
                f"Expected visual/mixed classification for {s!r}, got {cls!r}",
            )

    def test_f08_sync_push_rejects_non_flat_markdown_before_journal_or_update(self) -> None:
        """[F08] sync push invokes validate_flat_markdown and blocks tables/codeblocks/lists/H4+/long lines BEFORE journaling or updating Notion."""
        server = MockNotionServer(tools_schema=runtime_flat_notion_tools_schema())
        client = sync.MinisMcpNotionClient()
        state_path = self.tmp_path / "state_f08" / "state.json"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c03_path = self.bundle_dir / "03_C03.md"
            base_c03 = c03_path.read_text(encoding="utf-8")
            bad_additions = [
                "\n| Col1 | Col2 |\n| --- | --- |\n| A | B |\n",
                "\n```python\nprint(1)\n```\n",
                "\n- 无序列表项\n",
                "\n#### 不支持的四级标题\n",
                "\n" + ("长" * 1950) + "\n",
            ]
            for addition in bad_additions:
                c03_path.write_text(base_c03 + addition, encoding="utf-8")
                with self.assertRaises(sync.SyncError):
                    sync.run_sync(state_path=state_path, client=client, only=["C03"])
            self.assertEqual(server.update_call_count, 0)
            self.assertEqual(sync.find_unresolved_journal_ops(sync.load_journal(state_path)), [])

    def test_f09_strict_heading_column_and_duplicate_table_validation_explicit(self) -> None:
        """[F09] Strict parser explicitly rejects wrong header column count and duplicate tables per case."""
        # 5-column header rejected immediately with explicit header error
        five_col_lines = [
            "| A | B | C | D | E |",
            "| --- | --- | --- | --- | --- |",
            "| 1 | 2 | 3 | 4 | 5 |",
        ]
        with self.assertRaises(consolidate.SourceParseError) as ctx_5:
            consolidate.parse_case_table(five_col_lines, "C01")
        self.assertIn("expected 4 table columns in header", str(ctx_5.exception))

        # Multiple tables inside a single Appendix B case rejected explicitly
        dup_table_lines = [
            "| 序号／时间 | 原文 | 逐句分析 | 语义组 |",
            "| --- | --- | --- | --- |",
            "| 1 · 00:00 | 甲 | 析1 | 组1 |",
            "",
            "| 序号／时间 | 原文 | 逐句分析 | 语义组 |",
            "| --- | --- | --- | --- |",
            "| 2 · 00:02 | 乙 | 析2 | 组2 |",
        ]
        with self.assertRaises(consolidate.SourceParseError) as ctx_dup:
            consolidate.parse_case_table(dup_table_lines, "C01")
        self.assertIn("found multiple", str(ctx_dup.exception))

        # 8-column Appendix A table rejected immediately in header check
        eight_col_app_a = [
            "| 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            "| a | b | c | d | e | f | g | h |",
        ]
        with self.assertRaises(consolidate.SourceParseError) as ctx_a:
            consolidate.parse_appendix_a_case_table(eight_col_app_a, "C01")
        self.assertIn("expected 7 table columns in header", str(ctx_a.exception))

    def test_f10_conflict_cli_nonzero_exit_and_subprocess_errors_never_echo_body(self) -> None:
        """[F10] sync CLI returns exit code 2 on conflict; subprocess errors never echo private body snippets."""
        server = MockNotionServer(tools_schema=runtime_flat_notion_tools_schema())
        state_path = self.tmp_path / "state_f10" / "state.json"
        init_buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with contextlib.redirect_stdout(init_buf):
                rc_init = sync.main(
                    [
                        "init",
                        "--folder",
                        str(self.bundle_dir),
                        "--parent",
                        ROOT_PARENT_ID,
                        "--state",
                        str(state_path),
                    ]
                )
            self.assertEqual(rc_init, 0)

            c04_id = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["C04"]["page_id"]
            c04_path = self.bundle_dir / "04_C04.md"
            c04_path.write_text(c04_path.read_text(encoding="utf-8") + "\n本地冲突行。\n", encoding="utf-8")
            server.pages[c04_id]["content"] += "\n\n远端冲突行。"

            sync_buf = io.StringIO()
            with contextlib.redirect_stdout(sync_buf):
                rc_sync = sync.main(["sync", "--state", str(state_path), "--only", "C04"])

        parsed = json.loads(sync_buf.getvalue())
        self.assertEqual(parsed["status"], "conflict")
        self.assertEqual(rc_sync, 2)  # Non-zero exit code on conflict!

        # Subprocess failure echoing private body + token MUST leak ZERO characters of either!
        def leaking_body_cli(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout=f"Echoed request body: {PRIVATE_SENTINEL}",
                stderr=f"401 Unauthorized Bearer secret_body_leak_999 body={PRIVATE_SENTINEL}",
            )

        with mock.patch("subprocess.run", side_effect=leaking_body_cli):
            with self.assertRaises(sync.McpCallError) as ctx_sync_err:
                sync.MinisMcpNotionClient().discover_schemas()
            with self.assertRaises(consolidate.McpFetchError) as ctx_fetch_err:
                consolidate.fetch_notion_page(ROOT_PARENT_ID, self.tmp_path / "f10_out.md")
            with self.assertRaises(sync.McpCallError) as ctx_dep_err:
                deploy.MinisMcpGitHubClient().discover_schemas()

        for err in (ctx_sync_err.exception, ctx_fetch_err.exception, ctx_dep_err.exception):
            msg = str(err)
            self.assertNotIn(PRIVATE_SENTINEL, msg)
            self.assertNotIn("secret_body_leak_999", msg)
            self.assertIn("[REDACTED]", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
