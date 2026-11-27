#!/usr/bin/env python3
"""Independent read-only audit tests & defect reproducers for Black9 toolkit.

Audited developer commit:
  37aac5aac646cd679f10e0a430a6bb8e1841408f (gpt-gemini/jobs/black9-consolidate-20261127)

How this test suite loads the audited code without modifying the repo:
  1. If env var BLACK9_TOOLS_DIR is set, imports consolidate.py / sync.py from there.
  2. Else extracts tools/black9/{consolidate,sync,test_black9}.py from the exact
     audited git commit into a temporary directory (cleaned up in tearDownModule)
     so running tests creates ZERO untracked files in the repository.

All Notion MCP interactions are mocked locally using the known runtime shapes.
No live Notion calls, no private source files, no credentials, and no writes
outside temporary test directories are performed.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest import mock

AUDITED_COMMIT = "37aac5aac646cd679f10e0a430a6bb8e1841408f"
AUDITED_SHA256 = {
    "consolidate.py": "6e812bac2f282637435edc8fd0c191b47e90eb8ec0c2a72a71842938a03bab86",
    "sync.py": "bd737c8d840486d689a93268f5721123bf88e28c4b185d0d0bfcbedf158e4c23",
    "test_black9.py": "e1c5ae3dd1ed3f46d98f630aacaed274b3dde4a8735c3f4345c224a0ca7c2af7",
}

ROOT_PARENT_ID = "11111111-2222-3333-4444-555555555555"
OLD_PARENT_ID = "99999999-8888-7777-6666-555555555555"
PRIVATE_SENTINEL = "AUDIT_PRIVATE_MANUSCRIPT_SENTINEL_4242"

_TEMP_TOOLS_DIR: tempfile.TemporaryDirectory[str] | None = None
_LOADED_TOOLS_PATH: Path | None = None
consolidate: ModuleType | None = None
sync: ModuleType | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_module_from_path(name: str, file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load spec for {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _ensure_audited_modules_loaded() -> tuple[ModuleType, ModuleType, Path]:
    global _TEMP_TOOLS_DIR, _LOADED_TOOLS_PATH, consolidate, sync
    if consolidate is not None and sync is not None and _LOADED_TOOLS_PATH is not None:
        return consolidate, sync, _LOADED_TOOLS_PATH

    override = os.environ.get("BLACK9_TOOLS_DIR")
    if override:
        tools_dir = Path(override).resolve()
    else:
        _TEMP_TOOLS_DIR = tempfile.TemporaryDirectory(prefix="black9_audit_37aac5a_")
        tools_dir = Path(_TEMP_TOOLS_DIR.name)
        repo = _repo_root()
        for fname, expected_sha in AUDITED_SHA256.items():
            proc = subprocess.run(
                ["git", "show", f"{AUDITED_COMMIT}:tools/black9/{fname}"],
                cwd=str(repo),
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"Unable to read {AUDITED_COMMIT}:tools/black9/{fname} via git show: "
                    f"{proc.stderr.decode('utf-8', errors='replace')}"
                )
            actual_sha = hashlib.sha256(proc.stdout).hexdigest()
            if actual_sha != expected_sha:
                raise RuntimeError(
                    f"Audited file hash mismatch for {fname}: got {actual_sha}, expected {expected_sha}"
                )
            (tools_dir / fname).write_bytes(proc.stdout)

    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    consolidate = _load_module_from_path("consolidate", tools_dir / "consolidate.py")
    sync = _load_module_from_path("sync", tools_dir / "sync.py")
    _LOADED_TOOLS_PATH = tools_dir
    return consolidate, sync, tools_dir


def setUpModule() -> None:
    _ensure_audited_modules_loaded()


def tearDownModule() -> None:
    global _TEMP_TOOLS_DIR
    if _TEMP_TOOLS_DIR is not None:
        _TEMP_TOOLS_DIR.cleanup()
        _TEMP_TOOLS_DIR = None


def build_realistic_source_with_appendix_a_and_b(
    *,
    include_appendix_a: bool = True,
    c11_use_b_ids: bool = True,
    appendix_b_heading: str = "# 附录B｜黑桃9 十一案逐单元原文与分析",
    case_heading_fmt: str = "## {cid}｜案例{idx}",
) -> str:
    """Build synthetic source matching real structure: Appendix A (108 rows) + Appendix B (335 rows)."""
    lines: list[str] = [
        "# 黑桃9 综合研究总表",
        "",
        "## 思源对照参考（混排说明）",
        "SIYUAN_MIXED_CONTENT_MUST_NOT_COPY",
        "",
    ]

    if include_appendix_a:
        lines.append("# 附录A｜十一案语义分组深度分析表（108行）")
        lines.append("")
        lines.append("| 案例 | 语义分组 | 核心策略与结构深度分析 | 备注 |")
        lines.append("| --- | --- | --- | --- |")
        # 108 rows spread across C01..C11 (9*10 + 18 = 108)
        for row_idx in range(1, 109):
            cid = f"C{((row_idx - 1) % 11) + 1:02d}"
            lines.append(
                f"| {cid} | 分组G{row_idx:03d} | APPENDIX_A_GROUP_ANALYSIS_ROW_{row_idx:03d}：逐组拆解转化逻辑 | 重要 |"
            )
        lines.append("")

    lines.append(appendix_b_heading)
    lines.append("")

    counts = (23, 22, 22, 22, 37, 37, 30, 26, 16, 49, 51)
    for idx, (cid, cnt) in enumerate(zip([f"C{i:02d}" for i in range(1, 12)], counts), start=1):
        lines.append(case_heading_fmt.format(cid=cid, idx=idx))
        lines.append("")
        lines.append("| 单元与时间 | 原文与画面记录 | 既有分析 | 语义分组 |")
        lines.append("| --- | --- | --- | --- |")
        for r in range(1, cnt + 1):
            ts = f"00:{(r - 1) * 2:02d}-00:{r * 2:02d}"
            if cid == "C10":
                uid = f"A{r:02d}／{ts}"
            elif cid == "C11":
                uid = f"B{r:02d}／{ts}" if c11_use_b_ids else f"{r}／{ts}"
            else:
                uid = f"{r} · {ts}"

            if cid == "C01" and r == 1:
                orig = f"口播含秘密占位符（{PRIVATE_SENTINEL}）与转义竖线 X\\|Y"
            elif cid == "C01" and r == 2:
                orig = "画面展示产品对比"
            elif cid == "C01" and r == 3:
                orig = "[特写镜头，推近]"
            elif cid == "C01" and r == 4:
                orig = "（纯动作演示，未说话）"
            elif cid == "C01" and r == 5:
                orig = "【片头LOGO】"
            else:
                orig = f"{cid} 第{r}条原始记录"
            analysis = f"{cid} 第{r}条既有逐单元分析"
            group = f"语义组{(r % 5) + 1}"
            lines.append(f"| {uid} | {orig} | {analysis} | {group} |")
        lines.append("")
    return "\n".join(lines)


def runtime_notion_tools_schema(*, allow_async_required: bool = False) -> dict[str, Any]:
    """Runtime `minis-mcp-cli tools Notion` shape documented in audit contract."""
    update_required = ["page_id", "command", "new_str"]
    if allow_async_required:
        update_required.append("allow_async")
    return {
        "server": "Notion",
        "tools": [
            {
                "name": "notion-fetch",
                "description": "Fetch a Notion page",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
            {
                "name": "notion-create-pages",
                "description": "Create Notion pages",
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
                    "required": ["parent", "pages"],
                },
            },
            {
                "name": "notion-update-page",
                "description": "Update a Notion page",
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
            {
                "name": "notion-search",
                "description": "Search Notion pages",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        ],
    }


class RuntimeMockNotionServer:
    """Simulates the actual runtime `minis-mcp-cli` envelopes from the audit contract."""

    def __init__(
        self,
        *,
        tools_schema: dict[str, Any] | None = None,
        return_async_task_on_update: bool = False,
        autolink_urls_on_fetch: bool = False,
        fail_create_before_remote_at: int | None = None,
        fail_fetch_after_create_id: bool = False,
        disable_search: bool = False,
    ) -> None:
        self.tools_schema = tools_schema or runtime_notion_tools_schema()
        if disable_search:
            self.tools_schema = {
                "server": self.tools_schema.get("server", "Notion"),
                "tools": [t for t in self.tools_schema["tools"] if t["name"] != "notion-search"],
            }
        self.return_async_task_on_update = return_async_task_on_update
        self.autolink_urls_on_fetch = autolink_urls_on_fetch
        self.fail_create_before_remote_at = fail_create_before_remote_at
        self.fail_fetch_after_create_id = fail_fetch_after_create_id
        self.pages: dict[str, dict[str, Any]] = {
            ROOT_PARENT_ID: {
                "id": ROOT_PARENT_ID,
                "parent_id": None,
                "title": "Root Parent",
                "content": "# Root\n\nDo not modify.",
                "url": f"https://www.notion.so/{ROOT_PARENT_ID.replace('-', '')}",
            },
            OLD_PARENT_ID: {
                "id": OLD_PARENT_ID,
                "parent_id": None,
                "title": "Old Staging Parent",
                "content": "# Old Staging",
                "url": f"https://www.notion.so/{OLD_PARENT_ID.replace('-', '')}",
            },
        }
        self._seq = 1
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.create_attempts = 0
        self.update_payloads: list[dict[str, Any]] = []
        self.create_payloads: list[dict[str, Any]] = []
        self.fetch_raw_override: dict[str, str] = {}

    def _next_id(self) -> str:
        n = self._seq
        self._seq += 1
        return f"bbbbbbbb-cccc-4ddd-8eee-{n:012d}"

    def _wrap_call(self, tool_name: str, inner_obj: Any) -> str:
        return json.dumps(
            {
                "server": "Notion",
                "tool": tool_name,
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(inner_obj, ensure_ascii=False),
                        }
                    ]
                },
            },
            ensure_ascii=False,
        )

    def _render_fetch_text(self, page: dict[str, Any]) -> str:
        body = page["content"]
        if self.autolink_urls_on_fetch:
            # Realistic Notion Markdown read-back wraps bare https URLs as markdown links.
            import re

            body = re.sub(
                r"(?<!\()(?<!\[)(https://[^\s)>\]]+)",
                r"[\1](\1)",
                body,
            )
        children = [p for p in self.pages.values() if p.get("parent_id") == page["id"]]
        if children:
            child_tags = "\n".join(
                f'<page url="{c["url"]}">{c["title"]}</page>' for c in children
            )
            body = f"{body}\n\n{child_tags}"
        return (
            f'Here is the result of "view" for the Page with URL {page["url"]}:\n'
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
                if self.fail_fetch_after_create_id and self.create_attempts >= 2 and pid != ROOT_PARENT_ID:
                    self.fail_fetch_after_create_id = False
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Transient 503 on fetch")
                if pid not in self.pages:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not found")
                page = self.pages[pid]
                raw = self.fetch_raw_override.pop(pid, None)
                if raw is None:
                    raw = self._render_fetch_text(page)
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_call(
                        "notion-fetch",
                        {"title": page["title"], "url": page["url"], "text": raw},
                    ),
                    stderr="",
                )

            if tool_name == "notion-create-pages":
                self.create_attempts += 1
                self.create_payloads.append(payload)
                if (
                    self.fail_create_before_remote_at is not None
                    and self.create_attempts == self.fail_create_before_remote_at
                ):
                    self.fail_create_before_remote_at = None
                    # Network timeout BEFORE Notion creates the page remotely.
                    raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
                parent_id = payload["parent"]["page_id"]
                spec = payload["pages"][0]
                title = spec["properties"]["title"]
                content = spec["content"]
                new_id = self._next_id()
                url = f"https://www.notion.so/{new_id.replace('-', '')}"
                self.pages[new_id] = {
                    "id": new_id,
                    "parent_id": parent_id,
                    "title": title,
                    "content": content,
                    "url": url,
                }
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_call("notion-create-pages", {"pages": [{"id": new_id, "url": url}]}),
                    stderr="",
                )

            if tool_name == "notion-update-page":
                self.update_payloads.append(payload)
                pid = payload.get("page_id") or payload.get("data", {}).get("page_id")
                new_str = payload.get("new_str") or payload.get("data", {}).get("new_str")
                if self.return_async_task_on_update:
                    # Simulate Notion returning async_task before remote write finishes.
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=self._wrap_call(
                            "notion-update-page",
                            {"async_task": {"id": "async-task-98765", "status": "in_progress"}},
                        ),
                        stderr="",
                    )
                self.pages[pid]["content"] = new_str
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_call("notion-update-page", {"id": pid, "status": "updated"}),
                    stderr="",
                )

            if tool_name == "notion-search":
                q = payload["query"]
                matches = [
                    {"id": p["id"], "title": p["title"], "url": p["url"], "highlight": p["title"]}
                    for p in self.pages.values()
                    if q in p["title"] or q in p["content"]
                ]
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout=self._wrap_call("notion-search", {"results": matches}),
                    stderr="",
                )

        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=f"Unexpected cmd: {cmd}")


class Black9IndependentVerificationTests(unittest.TestCase):
    """Independent verification of properties that DO hold on commit 37aac5a."""

    def setUp(self) -> None:
        self.consolidate, self.sync, self.tools_dir = _ensure_audited_modules_loaded()
        self._tmp = tempfile.TemporaryDirectory(prefix="black9_verify_")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_verify_appendix_b_a01_b01_ids_and_lossless_335_units(self) -> None:
        src = self.tmp / "source.md"
        src.write_text(build_realistic_source_with_appendix_a_and_b(c11_use_b_ids=True), encoding="utf-8")
        out = self.tmp / "out"
        out.mkdir()
        report = self.consolidate.build_bundle(
            source_path=src,
            output_dir=out,
            source_urls=["https://www.notion.so/source123"],
        )
        self.assertEqual(report["case_count"], 11)
        self.assertEqual(report["total_units"], 335)
        self.assertEqual(report["counts"], [23, 22, 22, 22, 37, 37, 30, 26, 16, 49, 51])
        bundle = Path(report["bundle_dir"])
        self.assertIn("A01／00:00-00:02", (bundle / "10_C10.md").read_text(encoding="utf-8"))
        self.assertIn("A49／", (bundle / "10_C10.md").read_text(encoding="utf-8"))
        self.assertIn("B01／00:00-00:02", (bundle / "11_C11.md").read_text(encoding="utf-8"))
        self.assertIn("B51／", (bundle / "11_C11.md").read_text(encoding="utf-8"))
        # Verify Siyuan mixed body is not copied into any generated md
        for md_file in bundle.glob("*.md"):
            self.assertNotIn("SIYUAN_MIXED_CONTENT_MUST_NOT_COPY", md_file.read_text(encoding="utf-8"))

    def test_verify_runtime_mcp_envelopes_tools_fetch_create(self) -> None:
        server = RuntimeMockNotionServer()
        client = self.sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            tools = client.discover_schemas()
            self.assertIn("notion-fetch", tools)
            self.assertIn("notion-create-pages", tools)
            self.assertIn("notion-update-page", tools)

            fetched = client.fetch_page(ROOT_PARENT_ID)
            self.assertEqual(fetched.title, "Root Parent")
            self.assertEqual(fetched.extracted_body, "# Root\n\nDo not modify.")

            pid, _ = client.create_page(
                parent_id=ROOT_PARENT_ID,
                title="Probe Page",
                content="# Probe\n\nBody.",
            )
            self.assertTrue(pid)
            self.assertEqual(
                server.create_payloads[-1],
                {
                    "parent": {"page_id": ROOT_PARENT_ID},
                    "pages": [{"properties": {"title": "Probe Page"}, "content": "# Probe\n\nBody."}],
                },
            )

    def test_verify_no_credential_reads_or_background_services(self) -> None:
        forbidden_imports = {"urllib", "http", "socket", "requests", "httpx", "aiohttp"}
        for fname in ("consolidate.py", "sync.py"):
            source = (self.tools_dir / fname).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=fname)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        imported.add(alias.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertTrue(imported.isdisjoint(forbidden_imports), f"{fname} imported {imported & forbidden_imports}")
            self.assertNotIn("os.environ", source)
            self.assertNotIn("os.getenv", source)
            self.assertNotIn("crontab", source)
            self.assertNotIn("systemd", source)

    def test_verify_no_private_payload_leak_in_state_journal_or_stdout(self) -> None:
        src = self.tmp / "source.md"
        src.write_text(build_realistic_source_with_appendix_a_and_b(), encoding="utf-8")
        out = self.tmp / "vault"
        out.mkdir()
        build_rep = self.consolidate.build_bundle(source_path=src, output_dir=out)
        bundle = Path(build_rep["bundle_dir"])
        state_path = self.tmp / "state" / "black9_state.json"
        server = RuntimeMockNotionServer()
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                rc1 = self.sync.main(["init", "--folder", str(bundle), "--parent", ROOT_PARENT_ID, "--state", str(state_path)])
                rc2 = self.sync.main(["sync", "--state", str(state_path)])
        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        for text in (
            out_buf.getvalue(),
            err_buf.getvalue(),
            state_path.read_text(encoding="utf-8"),
            self.sync.journal_path_for(state_path).read_text(encoding="utf-8"),
        ):
            self.assertNotIn(PRIVATE_SENTINEL, text)


class Black9AuditDefectReproducers(unittest.TestCase):
    """Executable reproducer probes for every Critical/High/Medium/Low audit finding on 37aac5a.

    Each test deterministically reproduces the reported defect on commit 37aac5a and
    asserts the exact buggy behavior observed during the audit.
    """

    def setUp(self) -> None:
        self.consolidate, self.sync, self.tools_dir = _ensure_audited_modules_loaded()
        self._tmp = tempfile.TemporaryDirectory(prefix="black9_repro_")
        self.tmp = Path(self._tmp.name)
        self.source_file = self.tmp / "source.md"
        self.source_file.write_text(build_realistic_source_with_appendix_a_and_b(), encoding="utf-8")
        self.vault_dir = self.tmp / "vault"
        self.vault_dir.mkdir()
        build_rep = self.consolidate.build_bundle(
            source_path=self.source_file,
            output_dir=self.vault_dir,
            source_urls=["https://www.notion.so/origsource"],
        )
        self.bundle_dir = Path(build_rep["bundle_dir"])

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_repro_F01_update_page_omits_allow_async_and_ignores_async_task(self) -> None:
        """[CRITICAL F01] notion-update-page omits allow_async=False and ignores async_task response."""
        # Part A: Against runtime flat schema with optional allow_async, payload omits allow_async=False.
        server = RuntimeMockNotionServer(return_async_task_on_update=True)
        client = self.sync.MinisMcpNotionClient()
        state_path = self.tmp / "state_f01" / "state.json"

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c01_path = self.bundle_dir / "01_C01.md"
            c01_path.write_text(c01_path.read_text(encoding="utf-8") + "\n本地新增核对。\n", encoding="utf-8")

            # Push receives {"async_task": ...}; update_page ignores it, immediately fetches stale page,
            # raises SyncError (push_verify_mismatch), and leaves journal permanently locked!
            with self.assertRaises(self.sync.SyncError) as ctx:
                self.sync.run_sync(state_path=state_path, client=client, only=["C01"])
            self.assertIn("Post-push verification mismatch", str(ctx.exception))

            # Confirm allow_async was omitted in sent update payload
            self.assertEqual(len(server.update_payloads), 1)
            self.assertNotIn("allow_async", server.update_payloads[0])

            # Confirm journal is now permanently locked on subsequent mutating sync
            with self.assertRaises(self.sync.UncertainWriteError):
                self.sync.run_sync(state_path=state_path, client=client, only=["C02"])

        # Part B: If runtime schema marks allow_async required, _build_update_page_payload fails outright.
        req_server = RuntimeMockNotionServer(
            tools_schema=runtime_notion_tools_schema(allow_async_required=True)
        )
        req_client = self.sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=req_server.subprocess_run):
            with self.assertRaises(self.sync.SchemaValidationError) as ctx_req:
                req_client._build_update_page_payload(page_id=ROOT_PARENT_ID, new_content="hello")
            self.assertIn("allow_async", str(ctx_req.exception))

    def test_repro_F02_missing_appendix_a_108_rows_and_supplement_c01_c11(self) -> None:
        """[CRITICAL F02] consolidate.py drops all 108 Appendix A rows and lacks supplement parser."""
        all_md = "\n".join(p.read_text(encoding="utf-8") for p in self.bundle_dir.glob("*.md"))
        manifest_text = (self.bundle_dir / "manifest.json").read_text(encoding="utf-8")
        # None of the 108 Appendix A detailed group-analysis rows are preserved anywhere.
        self.assertNotIn("APPENDIX_A_GROUP_ANALYSIS_ROW_001", all_md)
        self.assertNotIn("APPENDIX_A_GROUP_ANALYSIS_ROW_108", all_md)
        self.assertNotIn("APPENDIX_A_GROUP_ANALYSIS_ROW_001", manifest_text)

        # CLI parser has no --supplement flag for separate supplement source (## 二、黑桃11版本逐段补研究).
        parser = self.consolidate.build_arg_parser()
        help_text = parser.format_help()
        self.assertNotIn("supplement", help_text.lower())

    def test_repro_F03_symlink_ancestor_escape_state_bundle_source(self) -> None:
        """[HIGH F03] Symlinked ancestor directory escapes state, journal, conflict, source, and build paths."""
        outside = self.tmp / "outside_target"
        outside.mkdir()
        link_dir = self.tmp / "symlink_gateway"
        link_dir.symlink_to(outside)

        # 1) State + journal + conflict snapshot escape via symlinked ancestor (even with non-existent nested dir!)
        escaped_state = link_dir / "nested_state" / "black9_state.json"
        server = RuntimeMockNotionServer()
        client = self.sync.MinisMcpNotionClient()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            # BUG: check_state_paths_safety does NOT reject symlinked ancestor `link_dir`!
            res = self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=escaped_state,
                client=client,
            )
        self.assertEqual(res["status"], "ok")
        self.assertTrue((outside / "nested_state" / "black9_state.json").exists())
        self.assertTrue((outside / "nested_state" / "black9_state.json.journal.jsonl").exists())

        # Trigger conflict -> conflict snapshots also written inside `outside`!
        st = json.loads((outside / "nested_state" / "black9_state.json").read_text(encoding="utf-8"))
        c01_id = st["pages"]["C01"]["page_id"]
        (self.bundle_dir / "01_C01.md").write_text(
            (self.bundle_dir / "01_C01.md").read_text(encoding="utf-8") + "\nlocal edit\n",
            encoding="utf-8",
        )
        server.pages[c01_id]["content"] += "\n\nremote edit"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            self.sync.run_sync(state_path=escaped_state, client=client, only=["C01"])
        self.assertTrue((outside / "nested_state" / "conflicts" / "C01").exists())

        # 2) consolidate.build_bundle escapes via symlinked source parent AND symlinked output ancestor!
        real_src_dir = self.tmp / "real_src_dir"
        real_src_dir.mkdir()
        (real_src_dir / "s.md").write_text(self.source_file.read_text(encoding="utf-8"), encoding="utf-8")
        link_src_dir = self.tmp / "link_src_dir"
        link_src_dir.symlink_to(real_src_dir)

        escaped_out = link_dir / "escaped_vault"
        rep = self.consolidate.build_bundle(
            source_path=link_src_dir / "s.md",  # immediate parent is symlink!
            output_dir=escaped_out,  # ancestor is symlink!
        )
        self.assertEqual(rep["status"], "ok")
        self.assertTrue((outside / "escaped_vault" / self.consolidate.DEFAULT_FOLDER_NAME / "01_C01.md").exists())

    def test_repro_F04_uncertain_create_recover_cross_collection_hijack_and_fetch_crash(self) -> None:
        """[HIGH F04] Child pages omit task_marker -> --recover hijacks unrelated page from another parent;
        and post-create fetch failure ignores saved page_id in state.json."""
        # Scenario A: Cross-collection hijack!
        server = RuntimeMockNotionServer()
        client = self.sync.MinisMcpNotionClient()
        old_state = self.tmp / "old_state" / "state.json"
        prod_state = self.tmp / "prod_state" / "state.json"

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            # Step 1: Earlier collection initialized under OLD_PARENT_ID from same source
            self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=OLD_PARENT_ID,
                state_path=old_state,
                client=client,
            )
            old_c01_id = json.loads(old_state.read_text(encoding="utf-8"))["pages"]["C01"]["page_id"]

            # Confirm child page payload NEVER embeds task_marker / page_marker!
            c01_create_payload = next(
                p for p in server.create_payloads if p["pages"][0]["properties"]["title"].startswith("C01")
            )
            self.assertNotIn("BLACK9-SYNC-", c01_create_payload["pages"][0]["content"])

            # Step 2: New init under ROOT_PARENT_ID times out BEFORE creating C01 (3rd create call: 1=parent, 2=00_index, 3=C01)
            server.create_attempts = 0
            server.fail_create_before_remote_at = 3
            with self.assertRaises(self.sync.UncertainWriteError):
                self.sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=prod_state,
                    client=client,
                )

            # Step 3: Operator runs --recover -> BUG: hijacks old_c01_id from OLD_PARENT_ID!
            rec = self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=prod_state,
                client=client,
                recover=True,
            )
            self.assertEqual(rec["status"], "ok")
            prod_c01_id = json.loads(prod_state.read_text(encoding="utf-8"))["pages"]["C01"]["page_id"]
            self.assertEqual(prod_c01_id, old_c01_id)  # Hijacked page belonging to OLD_PARENT_ID!

        # Scenario B: Post-create fetch_page crash leaves send_started even though page_id IS in state.json;
        # --recover fails if notion-search is unavailable instead of using persisted state["pages"][doc_id]["page_id"].
        server_b = RuntimeMockNotionServer(fail_fetch_after_create_id=True, disable_search=True)
        client_b = self.sync.MinisMcpNotionClient()
        state_b = self.tmp / "state_b" / "state.json"
        with mock.patch("subprocess.run", side_effect=server_b.subprocess_run):
            with self.assertRaises(self.sync.McpCallError):
                self.sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_b,
                    client=client_b,
                )
            st_b = json.loads(state_b.read_text(encoding="utf-8"))
            self.assertIn("00_index", st_b["pages"])
            self.assertTrue(st_b["pages"]["00_index"]["page_id"])  # page_id ALREADY saved!
            self.assertEqual(st_b["pages"]["00_index"]["base_remote_canon_sha256"], "")

            # --recover fails with SchemaValidationError (notion-search missing) instead of fetching saved page_id!
            with self.assertRaises(self.sync.SchemaValidationError):
                self.sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_b,
                    client=client_b,
                    recover=True,
                )

    def test_repro_F05_unrecoverable_push_journal_lock_and_notion_autolink_drift(self) -> None:
        """[HIGH F05] Notion URL autolinking fails post-push verify & bricks sync; SchemaValidationError poisons journal."""
        # Part A: Notion read-back converts bare `--source-url` (in every page) to markdown link [url](url).
        server = RuntimeMockNotionServer(autolink_urls_on_fetch=True)
        client = self.sync.MinisMcpNotionClient()
        state_path = self.tmp / "state_f05" / "state.json"

        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            # init succeeds because init stores base_local_canon and base_remote_canon separately!
            self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            # Immediate sync is noop!
            noop = self.sync.run_sync(state_path=state_path, client=client, only=["C01"])
            self.assertEqual(noop["summary"]["noop"], 1)

            # Now make a normal local edit and push -> FAILS post-push verification because
            # push requires verified_canon_sha == local_canon_sha despite autolinked --source-url!
            c01_path = self.bundle_dir / "01_C01.md"
            original_c01_bytes = c01_path.read_bytes()
            c01_path.write_text(c01_path.read_text(encoding="utf-8") + "\n正常校对备注。\n", encoding="utf-8")
            with self.assertRaises(self.sync.SyncError) as ctx:
                self.sync.run_sync(state_path=state_path, client=client, only=["C01"])
            self.assertIn("Post-push verification mismatch", str(ctx.exception))

            # Both sync and init --recover now refuse to proceed!
            with self.assertRaises(self.sync.UncertainWriteError):
                self.sync.run_sync(state_path=state_path, client=client, only=["C02"])
            # Even if the local file is restored to match manifest.json, init --recover rejects non-create ops.
            c01_path.write_bytes(original_c01_bytes)
            with self.assertRaises(self.sync.SyncError) as ctx_rec:
                self.sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_path,
                    client=client,
                    recover=True,
                )
            self.assertIn("Cannot auto-recover non-create operation", str(ctx_rec.exception))

        # Part B: Local SchemaValidationError poisons journal with send_started BEFORE any network call!
        bad_schema = runtime_notion_tools_schema()
        for t in bad_schema["tools"]:
            if t["name"] == "notion-create-pages":
                t["inputSchema"] = {
                    "type": "object",
                    "properties": {"unexpected_only": {"type": "string"}},
                    "required": ["unexpected_only"],
                }
        bad_server = RuntimeMockNotionServer(tools_schema=bad_schema)
        bad_client = self.sync.MinisMcpNotionClient()
        state_schema = self.tmp / "state_f05b" / "state.json"
        with mock.patch("subprocess.run", side_effect=bad_server.subprocess_run):
            with self.assertRaises(self.sync.SchemaValidationError):
                self.sync.run_init(
                    folder=self.bundle_dir,
                    parent_id=ROOT_PARENT_ID,
                    state_path=state_schema,
                    client=bad_client,
                )
        # ZERO create calls were sent, yet journal has unresolved send_started!
        self.assertEqual(bad_server.create_attempts, 0)
        unresolved = self.sync.find_unresolved_journal_ops(self.sync.load_journal(state_schema))
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]["status"], "send_started")

    def test_repro_F06_xml_content_self_closing_wipe_and_unwrapped_pull_overwrite(self) -> None:
        """[HIGH F06] Literal <content/> in body wipes extracted text; unwrapped fetch overwrites local file on pull."""
        # Part A: Literal <content/> inside valid <content>...</content> wipes entire body!
        raw_with_literal = (
            '<page url="https://www.notion.so/x">\n'
            "<properties>{\"title\":\"C01\"}</properties>\n"
            "<content>\n# C01\n\n注意不要误用空标签 <content/> 在正文中。\n</content>\n</page>"
        )
        extracted, had_wrapper = self.consolidate.extract_notion_content_xml(raw_with_literal)
        self.assertEqual(extracted, "")  # Entire body wiped!
        self.assertTrue(had_wrapper)

        # Part B: Non-XML warning text without <content> wrapper overwrites local file on pull!
        server = RuntimeMockNotionServer()
        client = self.sync.MinisMcpNotionClient()
        state_path = self.tmp / "state_f06" / "state.json"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c02_id = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["C02"]["page_id"]
            server.fetch_raw_override[c02_id] = "Warning: page temporarily truncated by upstream gateway"
            pull_res = self.sync.run_sync(state_path=state_path, client=client, only=["C02"])
        self.assertEqual(pull_res["summary"]["pull"], 1)
        self.assertEqual(
            (self.bundle_dir / "02_C02.md").read_text(encoding="utf-8"),
            "Warning: page temporarily truncated by upstream gateway\n",
        )

    def test_repro_F07_visually_ambiguous_rows_mislabeled_spoken_transcript(self) -> None:
        """[HIGH F07] Visually ambiguous / non-spoken cells are mislabeled as spoken_transcript ('口播正文记录')."""
        samples = [
            "画面展示产品对比",
            "[特写镜头，推近]",
            "（纯动作演示，未说话）",
            "【片头LOGO】",
        ]
        for s in samples:
            self.assertEqual(self.consolidate.classify_original_cell(s), "spoken_transcript")
        c01_md = (self.bundle_dir / "01_C01.md").read_text(encoding="utf-8")
        self.assertIn("记录区分：口播正文记录（按原文保留，未独立复听核对）\n\n原文（2 · 00:02-00:04）：画面展示产品对比", c01_md)

    def test_repro_F08_sync_push_allows_non_flat_markdown_breaking_safe_two_way(self) -> None:
        """[MEDIUM F08] sync.py push skips validate_flat_markdown and pushes tables/codeblocks/lists."""
        server = RuntimeMockNotionServer()
        client = self.sync.MinisMcpNotionClient()
        state_path = self.tmp / "state_f08" / "state.json"
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            self.sync.run_init(
                folder=self.bundle_dir,
                parent_id=ROOT_PARENT_ID,
                state_path=state_path,
                client=client,
            )
            c03_path = self.bundle_dir / "03_C03.md"
            non_flat_addition = "\n| Col1 | Col2 |\n| --- | --- |\n| A | B |\n\n```python\nprint(1)\n```\n"
            c03_path.write_text(c03_path.read_text(encoding="utf-8") + non_flat_addition, encoding="utf-8")
            res = self.sync.run_sync(state_path=state_path, client=client, only=["C03"])
        self.assertEqual(res["summary"]["push"], 1)
        c03_id = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["C03"]["page_id"]
        self.assertIn("| Col1 | Col2 |", server.pages[c03_id]["content"])

    def test_repro_F09_parser_heading_rigidity_and_column_check_inconsistency(self) -> None:
        """[MEDIUM F09] Parser rejects '## C10（标题）' and '## 附录B', and accepts >=4 header then crashes on !=4."""
        # 1) Heading '## C10（标题）' fails to match _CASE_HEADING_RE
        src_paren = build_realistic_source_with_appendix_a_and_b(case_heading_fmt="## {cid}（案例{idx}）")
        with self.assertRaises(self.consolidate.SourceParseError):
            self.consolidate.parse_source_markdown(src_paren)

        # 2) Heading '## 附录B' (H2) fails to match _APPENDIX_B_RE
        src_h2 = build_realistic_source_with_appendix_a_and_b(appendix_b_heading="## 附录B｜黑桃9")
        with self.assertRaises(self.consolidate.SourceParseError):
            self.consolidate.parse_source_markdown(src_h2)

        # 3) 5-column header accepted by len(header_cells) >= 4 then crashes on len(cells) != 4
        five_col_lines = [
            "| A | B | C | D | E |",
            "| --- | --- | --- | --- | --- |",
            "| 1 | 2 | 3 | 4 | 5 |",
        ]
        with self.assertRaises(self.consolidate.SourceParseError) as ctx:
            self.consolidate.parse_case_table(five_col_lines, "C01")
        self.assertIn("expected 4 table columns", str(ctx.exception))

    def test_repro_F10_conflict_cli_returns_zero_exit_code(self) -> None:
        """[LOW F10] sync.py CLI returns exit code 0 even when status is 'conflict'."""
        server = RuntimeMockNotionServer()
        state_path = self.tmp / "state_f10" / "state.json"
        out_buf = io.StringIO()
        with mock.patch("subprocess.run", side_effect=server.subprocess_run):
            with contextlib.redirect_stdout(out_buf):
                rc_init = self.sync.main(
                    ["init", "--folder", str(self.bundle_dir), "--parent", ROOT_PARENT_ID, "--state", str(state_path)]
                )
            self.assertEqual(rc_init, 0)
            c04_id = json.loads(state_path.read_text(encoding="utf-8"))["pages"]["C04"]["page_id"]
            (self.bundle_dir / "04_C04.md").write_text(
                (self.bundle_dir / "04_C04.md").read_text(encoding="utf-8") + "\nlocal conflict\n",
                encoding="utf-8",
            )
            server.pages[c04_id]["content"] += "\n\nremote conflict"
            sync_buf = io.StringIO()
            with contextlib.redirect_stdout(sync_buf):
                rc_sync = self.sync.main(["sync", "--state", str(state_path), "--only", "C04"])
        parsed = json.loads(sync_buf.getvalue())
        self.assertEqual(parsed["status"], "conflict")
        self.assertEqual(rc_sync, 0)  # Exit code 0 despite conflict!


if __name__ == "__main__":
    unittest.main(verbosity=2)
