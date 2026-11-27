#!/usr/bin/env python3
"""Comprehensive mocked unit tests for Black9 consolidate.py and sync.py (stdlib only).

All Notion MCP interactions are mocked locally; no live Notion API, credentials,
`/var/minis`, or private user data are touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

import consolidate  # noqa: E402
import sync  # noqa: E402

ROOT_PARENT_ID = "11111111-2222-3333-4444-555555555555"
PRIVATE_SENTINEL = "PRIVATE_MANUSCRIPT_SENTINEL_XYZ987"
SIYUAN_NOISE = "SIYUAN_MIXED_ARCHIVE_DO_NOT_COPY_999"
USER_SCRIPT_NOISE = "USER_OWN_FINAL_SCRIPT_DO_NOT_COPY_888"


def build_synthetic_source_markdown(
    *,
    counts: tuple[int, ...] = consolidate.EXPECTED_COUNTS,
    include_c11: bool = True,
    long_cell_chars: int = 2400,
) -> str:
    """Build a realistic synthetic source analysis Markdown fixture with all 11 variants."""
    lines: list[str] = [
        "# 跨案总览与前置材料",
        "",
        "## 思源混合归档摘录",
        f"这段是思源混合材料：{SIYUAN_NOISE}",
        "",
        "## 用户本人最终脚本草稿",
        f"这段是用户成稿：{USER_SCRIPT_NOISE}",
        "",
        "# 附录B｜黑桃9 十一案逐单元原文与分析",
        "",
    ]

    case_ids = consolidate.EXPECTED_CASE_IDS if include_c11 else consolidate.EXPECTED_CASE_IDS[:-1]
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
                # C11 variant: mix of 1／time, V02／time, numeric/variant (51 rows)
                if r % 3 == 1:
                    unit_id = f"{r}／{ts}"
                elif r % 3 == 2:
                    unit_id = f"V{r:02d}／{ts}"
                else:
                    unit_id = f"{r:02d} / {ts}"
            else:
                unit_id = f"{r} · {ts}"

            if case_id == "C01" and r == 1:
                orig = f"口播原话含ASR误差（{PRIVATE_SENTINEL}）与转义竖线 A\\|B"
            elif case_id == "C01" and r == 2:
                orig = "[画面：纯画面转场，无对白]"
            elif case_id == "C01" and r == 3:
                orig = "[画面：举起道具] 这里直接说核心卖点"
            elif case_id == "C02" and r == 1:
                orig = "超长原文测试：" + ("这是用于验证单行长度上限的原文句子。" * (long_cell_chars // 16 + 1))
            elif case_id == "C10" and r == 49:
                orig = f"C10末尾单元原文（{PRIVATE_SENTINEL}）"
            elif case_id == "C11" and r == 51:
                orig = "无对白"
            else:
                orig = f"{case_id} 第{r}条原始口播记录，保留原貌"

            analysis = f"{case_id} 第{r}条既有分析，含原始评注"
            group = f"分组-{(r % 4) + 1}"
            lines.append(f"| {unit_id} | {orig} | {analysis} | {group} |")
        lines.append("")

    return "\n".join(lines)


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


class MockNotionServer:
    """In-memory fake Notion MCP server driven via mocked `subprocess.run`."""

    def __init__(self, *, tools_schema: dict[str, Any] | None = None) -> None:
        self.tools_schema = tools_schema or standard_notion_tools_schema()
        self.pages: dict[str, dict[str, Any]] = {
            ROOT_PARENT_ID: {
                "id": ROOT_PARENT_ID,
                "parent_id": None,
                "title": "Existing User Root Page",
                "content": "# Root\n\nExisting user content.",
                "url": f"https://www.notion.so/{ROOT_PARENT_ID.replace('-', '')}",
            }
        }
        self._seq = 1
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.create_call_count = 0
        self.update_call_count = 0
        # Failure injection hooks
        self.uncertain_on_create_index: int | None = None
        self.uncertain_mode: str = "malformed_after_create"  # or "timeout_after_create"
        self.truncate_on_next_update: bool = False
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
                data = payload["data"]
                pid = data["page_id"]
                new_str = data["new_str"]
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
        self.assertEqual(report["counts"], list(consolidate.EXPECTED_COUNTS))

        bundle_dir = Path(report["bundle_dir"])
        self.assertEqual(bundle_dir.name, consolidate.DEFAULT_FOLDER_NAME)
        self.assertEqual(len(report["files"]), 13)

        manifest = json.loads((bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8"))
        self.assertEqual(manifest["case_count"], 11)
        self.assertEqual(manifest["total_units"], 335)
        self.assertEqual(manifest["actual_counts"][9], 49)  # C10
        self.assertEqual(manifest["actual_counts"][10], 51)  # C11
        self.assertEqual(manifest["excluded_from_notion"], ["manifest.json"])

        # Verify C01 preserves verbatim ASR, escaped pipe, visual separation, provenance, and analysis
        c01_text = (bundle_dir / "01_C01.md").read_text(encoding="utf-8")
        self.assertIn("## 来源与核验说明", c01_text)
        self.assertIn("未经独立复听原音视频核对", c01_text)
        self.assertIn("## 原文逐项", c01_text)
        self.assertIn("## 画面与无对白记录区分清单", c01_text)
        self.assertIn("## 分析逐项", c01_text)
        self.assertIn("A|B", c01_text)
        self.assertIn("[画面：纯画面转场，无对白]", c01_text)
        self.assertIn("既有分析（1 · 00:00-00:03）：", c01_text)
        self.assertIn("语义分组（1 · 00:00-00:03）：", c01_text)

        # Verify C10 and C11 exact row identifiers preserved
        c10_text = (bundle_dir / "10_C10.md").read_text(encoding="utf-8")
        self.assertIn("A01／00:00-00:03", c10_text)
        self.assertIn("A49／", c10_text)
        c11_text = (bundle_dir / "11_C11.md").read_text(encoding="utf-8")
        self.assertIn("1／00:00-00:03", c11_text)
        self.assertIn("V02／00:03-00:06", c11_text)
        self.assertIn("51 / ", c11_text)

        # Verify ALL generated .md files strictly < 1900 chars/line and never copy Siyuan/user scripts
        for entry in manifest["files"]:
            md_path = bundle_dir / entry["filename"]
            text = md_path.read_text(encoding="utf-8")
            self.assertNotIn(SIYUAN_NOISE, text)
            self.assertNotIn(USER_SCRIPT_NOISE, text)
            max_len = consolidate.validate_flat_markdown(text, entry["filename"])
            self.assertLess(max_len, 1900)
            self.assertEqual(consolidate.sha256_text(text), entry["sha256"])

        # Verify 90_source_reference.md has provenance links and no mixed Siyuan body
        ref_text = (bundle_dir / consolidate.REFERENCE_FILENAME).read_text(encoding="utf-8")
        self.assertIn("https://www.notion.so/source-page-1", ref_text)
        self.assertIn("source_analysis.md", ref_text)
        self.assertNotIn(SIYUAN_NOISE, ref_text)
        self.assertNotIn(USER_SCRIPT_NOISE, ref_text)

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
        # Clear the test-induced unresolved push_verify_mismatch journal entry to test conflict next
        sync.append_journal(
            self.state_path,
            {
                "op_id": sync.find_unresolved_journal_ops(sync.load_journal(self.state_path))[0]["op_id"],
                "status": "manually_resolved_in_test",
            },
        )

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
