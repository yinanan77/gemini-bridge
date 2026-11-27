#!/usr/bin/env python3
"""Black9 Notion<->Obsidian conservative 3-way sync via Minis MCP (Python stdlib only).

All Notion API access goes strictly through `minis-mcp-cli` subprocess calls with
runtime schema discovery (`minis-mcp-cli tools Notion`). Never reads OAuth tokens,
credentials, or `/var/minis`. Never logs private page payloads or secrets.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

try:
    from . import consolidate
except ImportError:  # pragma: no cover - direct script execution
    _SCRIPT_DIR = str(Path(__file__).resolve().parent)
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    import consolidate  # type: ignore[no-redef]


RELEVANT_NOTION_TOOLS: tuple[str, ...] = (
    "notion-fetch",
    "notion-create-pages",
    "notion-update-page",
    "notion-search",
)
REQUIRED_NOTION_TOOLS: tuple[str, ...] = (
    "notion-fetch",
    "notion-create-pages",
    "notion-update-page",
)
_BR_TAG_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_NOTION_ID_RE = re.compile(r"^[0-9a-fA-F\-]{32,36}$")
_UUID_EXTRACT_RE = re.compile(
    r"([0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12})"
)


class SyncError(Exception):
    """Base error for Black9 sync operations."""


class SchemaValidationError(SyncError):
    """Raised when discovered Notion MCP tool schemas fail validation."""


class McpCallError(SyncError):
    """Raised when minis-mcp-cli fails or returns malformed data."""


class UncertainWriteError(SyncError):
    """Raised when a mutating Notion request was sent but the outcome is uncertain."""


class ConflictError(SyncError):
    """Raised or recorded when 3-way sync encounters concurrent local/remote edits."""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_notion_id(raw_id: str) -> str:
    clean = raw_id.strip()
    if not clean or not _NOTION_ID_RE.match(clean):
        raise SyncError(f"Invalid Notion page ID format: {consolidate.redact_secrets(clean)}")
    return clean


def canonicalize_markdown(text: str, *, strip_notion_xml: bool = False) -> str:
    """Conservatively canonicalize Markdown without dropping any literal words.

    - Optionally strips Notion XML wrapper (`<page>...<content>...</content>...</page>`).
    - Normalizes line endings (`\\r\\n`, `\\r` -> `\\n`).
    - Normalizes `<br/>`, `<br />`, `<br>` -> `\\n`.
    - Strips trailing horizontal whitespace per line.
    - Omits blank lines (matching flat builder output and scripts/notion_sync.py simple renderer).
    """
    working = text
    if strip_notion_xml:
        working, _ = consolidate.extract_notion_content_xml(working)
    working = working.replace("\r\n", "\n").replace("\r", "\n")
    working = _BR_TAG_RE.sub("\n", working)
    lines = [line.rstrip(" \t") for line in working.split("\n")]
    non_empty = [line for line in lines if line.strip()]
    return "\n".join(non_empty)


def _extract_schema(tool_def: dict[str, Any]) -> dict[str, Any]:
    for key in ("inputSchema", "input_schema", "parameters"):
        val = tool_def.get(key)
        if isinstance(val, dict):
            return val
    return {}


def _parse_tools_list(stdout_text: str) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise McpCallError(f"Malformed JSON from `minis-mcp-cli tools Notion`: {exc}") from exc

    tools_raw: Any = None
    if isinstance(data, list):
        tools_raw = data
    elif isinstance(data, dict):
        if data.get("error"):
            raise McpCallError(
                f"MCP tools discovery error: {consolidate.redact_secrets(str(data['error']))}"
            )
        if isinstance(data.get("tools"), list):
            tools_raw = data["tools"]
        elif isinstance(data.get("result"), dict) and isinstance(data["result"].get("tools"), list):
            tools_raw = data["result"]["tools"]

    if not isinstance(tools_raw, list):
        raise McpCallError("Unexpected shape from `minis-mcp-cli tools Notion` (missing tools list)")

    out: dict[str, dict[str, Any]] = {}
    for item in tools_raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name:
            out[name] = {
                "name": name,
                "description": item.get("description", ""),
                "inputSchema": _extract_schema(item),
            }
    return out


def _validate_required_fields(schema: dict[str, Any], payload: dict[str, Any], tool_name: str) -> None:
    """Validate that payload satisfies all `required` keys in top-level schema."""
    req = schema.get("required")
    if isinstance(req, list):
        missing = [k for k in req if k not in payload or payload[k] is None]
        if missing:
            raise SchemaValidationError(
                f"{tool_name}: payload missing required schema field(s): {missing}"
            )
    props = schema.get("properties")
    if isinstance(props, dict):
        for key, value in payload.items():
            sub_schema = props.get(key)
            if isinstance(sub_schema, dict) and isinstance(value, dict):
                sub_req = sub_schema.get("required")
                if isinstance(sub_req, list):
                    sub_missing = [k for k in sub_req if k not in value or value[k] is None]
                    if sub_missing:
                        raise SchemaValidationError(
                            f"{tool_name}.{key}: missing required schema field(s): {sub_missing}"
                        )


def _extract_mcp_result_payload(stdout_text: str, tool_name: str) -> Any:
    """Parse standard MCP call envelope `{result: {content: [{type: 'text', text: ...}]}}`."""
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise McpCallError(f"{tool_name}: malformed JSON response: {exc}") from exc

    if not isinstance(envelope, dict):
        raise McpCallError(f"{tool_name}: top-level MCP response must be an object")
    if envelope.get("error"):
        raise McpCallError(
            f"{tool_name}: MCP error: {consolidate.redact_secrets(str(envelope['error']))}"
        )

    result_obj = envelope.get("result")
    if not isinstance(result_obj, dict):
        raise McpCallError(f"{tool_name}: MCP response missing `result` object")
    if result_obj.get("isError"):
        raise McpCallError(f"{tool_name}: MCP tool returned isError=true")

    content_list = result_obj.get("content")
    if not isinstance(content_list, list) or not content_list:
        raise McpCallError(f"{tool_name}: MCP response missing non-empty `result.content`")

    texts = [
        item["text"]
        for item in content_list
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    if not texts:
        raise McpCallError(f"{tool_name}: MCP response `result.content` has no text item")

    raw_inner = texts[0]
    try:
        return json.loads(raw_inner)
    except json.JSONDecodeError as exc:
        raise McpCallError(f"{tool_name}: inner MCP text is not valid JSON: {exc}") from exc


def _extract_created_page_id(inner_obj: Any) -> tuple[str, str]:
    """Extract (page_id, url) from notion-create-pages inner JSON strictly."""
    candidates: list[dict[str, Any]] = []
    if isinstance(inner_obj, dict):
        if isinstance(inner_obj.get("pages"), list) and inner_obj["pages"]:
            first = inner_obj["pages"][0]
            if isinstance(first, dict):
                candidates.append(first)
        if isinstance(inner_obj.get("results"), list) and inner_obj["results"]:
            first = inner_obj["results"][0]
            if isinstance(first, dict):
                candidates.append(first)
        candidates.append(inner_obj)
    elif isinstance(inner_obj, list) and inner_obj and isinstance(inner_obj[0], dict):
        candidates.append(inner_obj[0])

    for cand in candidates:
        for id_key in ("id", "page_id"):
            val = cand.get(id_key)
            if isinstance(val, str) and _NOTION_ID_RE.match(val.strip()):
                url = cand.get("url", "")
                return val.strip(), url if isinstance(url, str) else ""
        # Some responses only return a Notion URL containing the 32-hex page ID.
        url_val = cand.get("url")
        if isinstance(url_val, str):
            m = _UUID_EXTRACT_RE.search(url_val)
            if m:
                return m.group(1), url_val

    raise UncertainWriteError(
        "notion-create-pages response did not contain a valid created page ID"
    )


@dataclass
class FetchedPage:
    page_id: str
    title: str
    url: str
    raw_text: str
    extracted_body: str
    canonical_body: str


class MinisMcpNotionClient:
    """Subprocess adapter for Notion via `minis-mcp-cli` with runtime schema discovery."""

    def __init__(self, cli_bin: str = "minis-mcp-cli", timeout_sec: int = 60) -> None:
        self.cli_bin = cli_bin
        self.timeout_sec = timeout_sec
        self._schemas: dict[str, dict[str, Any]] | None = None

    def _run_cli(self, args: Sequence[str], *, mutating: bool = False) -> str:
        cmd = [self.cli_bin, *args]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
            )
        except FileNotFoundError as exc:
            raise McpCallError(f"MCP CLI executable not found: {self.cli_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            err_cls = UncertainWriteError if mutating else McpCallError
            raise err_cls(f"minis-mcp-cli timed out after {self.timeout_sec}s") from exc

        if proc.returncode != 0:
            err_cls = UncertainWriteError if mutating else McpCallError
            snippet = consolidate.redact_secrets((proc.stderr or proc.stdout or "").strip())[:300]
            raise err_cls(f"minis-mcp-cli exited with code {proc.returncode}: {snippet}")
        return proc.stdout

    def discover_schemas(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        if self._schemas is not None and not force:
            return self._schemas
        stdout = self._run_cli(["tools", "Notion"], mutating=False)
        all_tools = _parse_tools_list(stdout)
        self._schemas = all_tools
        return all_tools

    def require_tools(self, names: Sequence[str] = REQUIRED_NOTION_TOOLS) -> dict[str, dict[str, Any]]:
        tools = self.discover_schemas()
        missing = [name for name in names if name not in tools]
        if missing:
            raise SchemaValidationError(
                f"Required Notion MCP tool(s) missing from discovered schema: {missing}"
            )
        return {name: tools[name] for name in names}

    def inspect_relevant_schemas(self) -> dict[str, Any]:
        tools = self.discover_schemas()
        relevant = {
            name: tools[name] for name in RELEVANT_NOTION_TOOLS if name in tools
        }
        missing_required = [name for name in REQUIRED_NOTION_TOOLS if name not in relevant]
        return {
            "status": "ok" if not missing_required else "incomplete",
            "discovered_relevant_tools": sorted(relevant.keys()),
            "missing_required_tools": missing_required,
            "tools": relevant,
        }

    def _build_fetch_payload(self, page_id: str) -> dict[str, Any]:
        tool = self.require_tools(["notion-fetch"])["notion-fetch"]
        schema = tool["inputSchema"]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if "id" in props or not props:
            payload: dict[str, Any] = {"id": page_id}
        elif "page_id" in props:
            payload = {"page_id": page_id}
        else:
            raise SchemaValidationError(
                f"notion-fetch schema has unsupported properties: {sorted(props.keys())}"
            )
        _validate_required_fields(schema, payload, "notion-fetch")
        return payload

    def _build_create_page_payload(
        self, *, parent_id: str, title: str, content: str
    ) -> dict[str, Any]:
        tool = self.require_tools(["notion-create-pages"])["notion-create-pages"]
        schema = tool["inputSchema"]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}

        if "pages" in props:
            parent_schema = props.get("parent") if isinstance(props.get("parent"), dict) else {}
            parent_props = (
                parent_schema.get("properties")
                if isinstance(parent_schema.get("properties"), dict)
                else {}
            )
            parent_req = (
                parent_schema.get("required")
                if isinstance(parent_schema.get("required"), list)
                else []
            )
            parent_obj: dict[str, Any] = {"page_id": parent_id}
            if "type" in parent_props or "type" in parent_req:
                parent_obj["type"] = "page_id"

            pages_schema = props["pages"] if isinstance(props["pages"], dict) else {}
            item_schema = pages_schema.get("items") if isinstance(pages_schema.get("items"), dict) else {}
            item_props = (
                item_schema.get("properties")
                if isinstance(item_schema.get("properties"), dict)
                else {}
            )

            page_entry: dict[str, Any] = {}
            if "properties" in item_props or not item_props:
                page_entry["properties"] = {"title": title}
            elif "title" in item_props:
                page_entry["title"] = title
            else:
                raise SchemaValidationError(
                    f"notion-create-pages item schema unsupported: {sorted(item_props.keys())}"
                )

            if "content" in item_props or not item_props:
                page_entry["content"] = content
            elif "markdown" in item_props:
                page_entry["markdown"] = content
            else:
                raise SchemaValidationError(
                    f"notion-create-pages item schema missing content field: {sorted(item_props.keys())}"
                )

            _validate_required_fields(item_schema, page_entry, "notion-create-pages.pages[]")
            payload: dict[str, Any] = {"parent": parent_obj, "pages": [page_entry]}
        elif "title" in props and ("content" in props or "markdown" in props):
            payload = {"title": title}
            if "parent_id" in props:
                payload["parent_id"] = parent_id
            elif "parent" in props:
                payload["parent"] = {"page_id": parent_id}
            content_key = "content" if "content" in props else "markdown"
            payload[content_key] = content
        else:
            raise SchemaValidationError(
                f"notion-create-pages schema unsupported: {sorted(props.keys())}"
            )

        _validate_required_fields(schema, payload, "notion-create-pages")
        return payload

    def _build_update_page_payload(self, *, page_id: str, new_content: str) -> dict[str, Any]:
        tool = self.require_tools(["notion-update-page"])["notion-update-page"]
        schema = tool["inputSchema"]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}

        def _populate_update_obj(obj_schema: dict[str, Any], label: str) -> dict[str, Any]:
            # Inspect direct properties and optional oneOf/anyOf variants.
            obj_props = (
                dict(obj_schema["properties"])
                if isinstance(obj_schema.get("properties"), dict)
                else {}
            )
            variants = obj_schema.get("oneOf") or obj_schema.get("anyOf") or []
            if isinstance(variants, list):
                for variant in variants:
                    if isinstance(variant, dict) and isinstance(variant.get("properties"), dict):
                        v_props = variant["properties"]
                        cmd_spec = v_props.get("command")
                        if isinstance(cmd_spec, dict):
                            const_val = cmd_spec.get("const")
                            enum_vals = cmd_spec.get("enum") if isinstance(cmd_spec.get("enum"), list) else []
                            if const_val == "replace_content" or "replace_content" in enum_vals:
                                obj_props.update(v_props)
                                break

            body: dict[str, Any] = {}
            if "page_id" in obj_props or not obj_props:
                body["page_id"] = page_id
            elif "id" in obj_props:
                body["id"] = page_id
            else:
                raise SchemaValidationError(
                    f"{label}: schema has no page_id/id property: {sorted(obj_props.keys())}"
                )

            if "command" in obj_props:
                cmd_schema = obj_props["command"] if isinstance(obj_props["command"], dict) else {}
                enum_vals = cmd_schema.get("enum") if isinstance(cmd_schema.get("enum"), list) else []
                if enum_vals and "replace_content" not in enum_vals:
                    raise SchemaValidationError(
                        f"{label}: command enum does not support replace_content: {enum_vals}"
                    )
                body["command"] = "replace_content"

            if "new_str" in obj_props:
                body["new_str"] = new_content
            elif "new_content" in obj_props:
                body["new_content"] = new_content
            elif "content" in obj_props:
                body["content"] = new_content
            elif "markdown" in obj_props:
                body["markdown"] = new_content
            else:
                raise SchemaValidationError(
                    f"{label}: schema has no recognized content field: {sorted(obj_props.keys())}"
                )

            _validate_required_fields(obj_schema, body, label)
            return body

        if "data" in props and isinstance(props["data"], dict):
            payload: dict[str, Any] = {
                "data": _populate_update_obj(props["data"], "notion-update-page.data")
            }
        else:
            payload = _populate_update_obj(schema, "notion-update-page")

        _validate_required_fields(schema, payload, "notion-update-page")
        return payload

    def _build_search_payload(self, query: str) -> dict[str, Any]:
        tools = self.discover_schemas()
        if "notion-search" not in tools:
            raise SchemaValidationError("notion-search tool is not available in discovered Notion schemas")
        schema = tools["notion-search"]["inputSchema"]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        if "query" in props or not props:
            payload: dict[str, Any] = {"query": query}
        elif "q" in props:
            payload = {"q": query}
        else:
            raise SchemaValidationError(
                f"notion-search schema unsupported: {sorted(props.keys())}"
            )
        _validate_required_fields(schema, payload, "notion-search")
        return payload

    def fetch_page(self, page_id: str) -> FetchedPage:
        clean_id = normalize_notion_id(page_id)
        payload = self._build_fetch_payload(clean_id)
        stdout = self._run_cli(
            ["call", "Notion", "notion-fetch", json.dumps(payload, ensure_ascii=False)],
            mutating=False,
        )
        inner = _extract_mcp_result_payload(stdout, "notion-fetch")
        if not isinstance(inner, dict) or not isinstance(inner.get("text"), str):
            raise McpCallError("notion-fetch inner payload missing string `text` field")
        raw_text = inner["text"]
        extracted, _ = consolidate.extract_notion_content_xml(raw_text)
        canonical = canonicalize_markdown(raw_text, strip_notion_xml=True)
        return FetchedPage(
            page_id=clean_id,
            title=inner.get("title", "") if isinstance(inner.get("title"), str) else "",
            url=inner.get("url", "") if isinstance(inner.get("url"), str) else "",
            raw_text=raw_text,
            extracted_body=extracted,
            canonical_body=canonical,
        )

    def create_page(self, *, parent_id: str, title: str, content: str) -> tuple[str, str]:
        clean_parent = normalize_notion_id(parent_id)
        payload = self._build_create_page_payload(
            parent_id=clean_parent,
            title=title,
            content=content,
        )
        stdout = self._run_cli(
            ["call", "Notion", "notion-create-pages", json.dumps(payload, ensure_ascii=False)],
            mutating=True,
        )
        try:
            inner = _extract_mcp_result_payload(stdout, "notion-create-pages")
        except McpCallError as exc:
            raise UncertainWriteError(
                f"Uncertain notion-create-pages outcome after send: {exc}"
            ) from exc
        return _extract_created_page_id(inner)

    def update_page(self, *, page_id: str, new_content: str) -> None:
        clean_id = normalize_notion_id(page_id)
        payload = self._build_update_page_payload(page_id=clean_id, new_content=new_content)
        stdout = self._run_cli(
            ["call", "Notion", "notion-update-page", json.dumps(payload, ensure_ascii=False)],
            mutating=True,
        )
        try:
            _extract_mcp_result_payload(stdout, "notion-update-page")
        except McpCallError as exc:
            raise UncertainWriteError(
                f"Uncertain notion-update-page outcome after send: {exc}"
            ) from exc

    def search_pages(self, query: str) -> list[dict[str, Any]]:
        payload = self._build_search_payload(query)
        stdout = self._run_cli(
            ["call", "Notion", "notion-search", json.dumps(payload, ensure_ascii=False)],
            mutating=False,
        )
        inner = _extract_mcp_result_payload(stdout, "notion-search")
        results: Any = []
        if isinstance(inner, dict):
            results = inner.get("results") or inner.get("pages") or []
        elif isinstance(inner, list):
            results = inner
        if not isinstance(results, list):
            raise McpCallError("notion-search returned unexpected results shape")
        normalized: list[dict[str, Any]] = []
        for item in results:
            if not isinstance(item, dict):
                continue
            pid = item.get("id") or item.get("page_id")
            if not isinstance(pid, str) and isinstance(item.get("url"), str):
                m = _UUID_EXTRACT_RE.search(item["url"])
                if m:
                    pid = m.group(1)
            if isinstance(pid, str) and _NOTION_ID_RE.match(pid.strip()):
                normalized.append(
                    {
                        "id": pid.strip(),
                        "title": item.get("title", "") if isinstance(item.get("title"), str) else "",
                        "url": item.get("url", "") if isinstance(item.get("url"), str) else "",
                        "highlight": item.get("highlight", "")
                        if isinstance(item.get("highlight"), str)
                        else "",
                    }
                )
        return normalized


def journal_path_for(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.name}.journal.jsonl")


def conflicts_dir_for(state_path: Path) -> Path:
    return state_path.parent / "conflicts"


def check_state_paths_safety(state_path: Path) -> None:
    if state_path.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink state file: {state_path}")
    if state_path.parent.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink state directory: {state_path.parent}")
    jpath = journal_path_for(state_path)
    if jpath.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink journal file: {jpath}")
    cdir = conflicts_dir_for(state_path)
    if cdir.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink conflicts directory: {cdir}")


def load_journal(state_path: Path) -> list[dict[str, Any]]:
    jpath = journal_path_for(state_path)
    if not jpath.exists():
        return []
    if jpath.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink journal file: {jpath}")
    entries: list[dict[str, Any]] = []
    for lineno, line in enumerate(jpath.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SyncError(f"Corrupted journal line {lineno} in {jpath}: {exc}") from exc
        if not isinstance(obj, dict):
            raise SyncError(f"Journal line {lineno} is not a JSON object")
        entries.append(obj)
    return entries


def append_journal(state_path: Path, entry: dict[str, Any]) -> None:
    """Append a journal record durably by atomic rewrite (small per-operation log)."""
    check_state_paths_safety(state_path)
    entries = load_journal(state_path)
    record = dict(entry)
    record.setdefault("ts", utc_now_iso())
    entries.append(record)
    payload = "".join(
        json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries
    ).encode("utf-8")
    consolidate.atomic_write_bytes(journal_path_for(state_path), payload)


def find_unresolved_journal_ops(entries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return all journal operations whose latest status is `send_started` or `uncertain`."""
    latest_by_op: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for entry in entries:
        op_id = entry.get("op_id")
        if not isinstance(op_id, str) or not op_id:
            continue
        if op_id not in latest_by_op:
            order.append(op_id)
        latest_by_op[op_id] = entry

    unresolved: list[dict[str, Any]] = []
    for op_id in order:
        st = latest_by_op[op_id].get("status")
        if st in ("send_started", "uncertain", "push_started", "push_uncertain", "push_verify_mismatch"):
            unresolved.append(latest_by_op[op_id])
    return unresolved


def load_state(state_path: Path) -> dict[str, Any] | None:
    check_state_paths_safety(state_path)
    if not state_path.exists():
        return None
    if not state_path.is_file():
        raise SyncError(f"State path is not a regular file: {state_path}")
    try:
        obj = json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"State file is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise SyncError("State file root must be a JSON object")
    return obj


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    check_state_paths_safety(state_path)
    data = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    consolidate.atomic_write_bytes(state_path, data)


def preflight_bundle_folder(folder: Path) -> dict[str, Any]:
    """Validate bundle directory, manifest counts (11/335), file hashes, flat format, and symlinks."""
    if folder.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink bundle folder: {folder}")
    if not folder.exists() or not folder.is_dir():
        raise SyncError(f"Bundle folder does not exist or is not a directory: {folder}")

    manifest_path = folder / consolidate.MANIFEST_FILENAME
    consolidate.check_no_symlink_chain(manifest_path, stop_at=folder)
    if not manifest_path.exists() or not manifest_path.is_file():
        raise SyncError(f"Missing required manifest file: {manifest_path}")

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"Invalid manifest JSON: {exc}") from exc

    if not isinstance(manifest, dict):
        raise SyncError("Manifest JSON must be an object")

    if manifest.get("case_count") != len(consolidate.EXPECTED_CASE_IDS):
        raise SyncError(
            f"Manifest case_count mismatch: got {manifest.get('case_count')}, expected 11"
        )
    if manifest.get("total_units") != consolidate.EXPECTED_TOTAL_UNITS:
        raise SyncError(
            f"Manifest total_units mismatch: got {manifest.get('total_units')}, expected 335"
        )
    if manifest.get("actual_counts") != list(consolidate.EXPECTED_COUNTS):
        raise SyncError(
            f"Manifest actual_counts mismatch: got {manifest.get('actual_counts')}, "
            f"expected {list(consolidate.EXPECTED_COUNTS)}"
        )

    if "total_groups" in manifest:
        if manifest.get("total_groups") != consolidate.EXPECTED_TOTAL_GROUPS:
            raise SyncError(
                f"Manifest total_groups mismatch: got {manifest.get('total_groups')}, "
                f"expected {consolidate.EXPECTED_TOTAL_GROUPS}"
            )
        group_counts = manifest.get("actual_group_counts")
        if (
            not isinstance(group_counts, list)
            or len(group_counts) != len(consolidate.EXPECTED_CASE_IDS)
            or any(not isinstance(v, int) or v <= 0 for v in group_counts)
            or sum(group_counts) != consolidate.EXPECTED_TOTAL_GROUPS
        ):
            raise SyncError(
                f"Manifest actual_group_counts invalid: {group_counts}"
            )

    if manifest.get("supplement_included"):
        if manifest.get("supplement_item_count") != consolidate.EXPECTED_TOTAL_SUPPLEMENT_ITEMS:
            raise SyncError(
                f"Manifest supplement_item_count mismatch: got {manifest.get('supplement_item_count')}, "
                f"expected {consolidate.EXPECTED_TOTAL_SUPPLEMENT_ITEMS}"
            )
        supp_counts = manifest.get("supplement_counts")
        expected_supp = [consolidate.EXPECTED_SUPPLEMENT_ITEMS_PER_CASE] * len(
            consolidate.EXPECTED_CASE_IDS
        )
        if supp_counts != expected_supp:
            raise SyncError(
                f"Manifest supplement_counts mismatch: got {supp_counts}, expected {expected_supp}"
            )
        if manifest.get("supplement_format") == "markdown_table":
            if manifest.get("supplement_table_count") != consolidate.EXPECTED_SUPPLEMENT_TABLES:
                raise SyncError(
                    f"Manifest supplement_table_count mismatch: got {manifest.get('supplement_table_count')}, "
                    f"expected {consolidate.EXPECTED_SUPPLEMENT_TABLES}"
                )

    allow_blanks = not bool(manifest.get("flat_markdown_no_blank_lines", False))

    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 13:
        raise SyncError(f"Manifest must list exactly 13 Notion-syncable .md files, got {len(files) if isinstance(files, list) else 'invalid'}")

    verified_docs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for entry in files:
        if not isinstance(entry, dict):
            raise SyncError("Manifest file entry must be an object")
        fname = entry.get("filename")
        doc_id = entry.get("doc_id")
        title = entry.get("title")
        expected_hash = entry.get("sha256")
        if not all(isinstance(v, str) and v for v in (fname, doc_id, title, expected_hash)):
            raise SyncError(f"Incomplete manifest file entry: {entry}")
        if fname == consolidate.MANIFEST_FILENAME or not fname.endswith(".md"):
            raise SyncError(f"Disallowed Notion target filename in manifest: {fname}")
        if "/" in fname or "\\" in fname or ".." in fname:
            raise consolidate.PathSafetyError(f"Invalid filename in manifest: {fname}")
        if doc_id in seen_ids:
            raise SyncError(f"Duplicate doc_id in manifest: {doc_id}")
        seen_ids.add(doc_id)

        fpath = folder / fname
        consolidate.check_no_symlink_chain(fpath, stop_at=folder)
        if not fpath.exists() or not fpath.is_file():
            raise SyncError(f"Manifest target file missing on disk: {fpath}")
        raw_bytes = fpath.read_bytes()
        actual_hash = consolidate.sha256_bytes(raw_bytes)
        if actual_hash != expected_hash:
            raise SyncError(
                f"Preflight hash mismatch for {fname}: disk={actual_hash}, manifest={expected_hash}"
            )
        text = raw_bytes.decode("utf-8")
        consolidate.validate_flat_markdown(text, fname, allow_blank_lines=allow_blanks)
        verified_docs.append(
            {
                "doc_id": doc_id,
                "filename": fname,
                "title": title,
                "kind": entry.get("kind", "case"),
                "sha256": actual_hash,
                "text": text,
            }
        )

    expected_doc_ids = {"00_index", "90_source_reference", *consolidate.EXPECTED_CASE_IDS}
    if seen_ids != expected_doc_ids:
        raise SyncError(
            f"Manifest doc_ids mismatch: missing={sorted(expected_doc_ids - seen_ids)}, "
            f"extra={sorted(seen_ids - expected_doc_ids)}"
        )

    return {
        "manifest": manifest,
        "docs": verified_docs,
    }


def _collection_marker(source_sha256: str, parent_id: str) -> str:
    clean_parent = parent_id.replace("-", "")[:8]
    return f"BLACK9-SYNC-{source_sha256[:12]}-{clean_parent}"


def _collection_parent_body(manifest: dict[str, Any], task_marker: str) -> str:
    blocks = [
        f"# {manifest.get('bundle_name', consolidate.DEFAULT_FOLDER_NAME)}",
        "本页为黑桃9文稿与分析汇总集合父页，下辖 00 总索引、C01 至 C11 案例页及 90 来源引用页。",
        f"同步集合标识：{task_marker}",
        f"来源文件 SHA-256：{manifest.get('source_sha256', '')}",
        f"校验计数：{manifest.get('case_count', 0)} 个案例，共 {manifest.get('total_units', 0)} 条单元。",
    ]
    return "\n".join(blocks) + "\n"


def _recover_uncertain_create(
    client: MinisMcpNotionClient,
    *,
    op: dict[str, Any],
    state: dict[str, Any],
) -> tuple[str, str] | None:
    """Explicit recovery lookup via notion-search using unique task marker / title.

    Returns (page_id, url) if uniquely identified, or raises SyncError if ambiguous/unverified.
    """
    target = op.get("target")
    marker = op.get("task_marker", "")
    expected_title = op.get("title", "")

    if target == "__collection_parent__":
        matches = client.search_pages(marker)
        verified: list[tuple[str, str]] = []
        for m in matches:
            fetched = client.fetch_page(m["id"])
            if marker in fetched.raw_text or marker in fetched.canonical_body:
                verified.append((m["id"], fetched.url or m.get("url", "")))
        if len(verified) == 1:
            return verified[0]
        if len(verified) > 1:
            raise SyncError(
                f"Recovery found {len(verified)} pages matching collection marker {marker}; manual resolution required"
            )
        raise SyncError(
            f"Recovery could not verify whether collection parent ({marker}) was created; refusing duplicate creation"
        )

    # Child page recovery: search by exact title (or task marker), then verify via parent link or fetched content.
    search_query = expected_title or marker
    matches = client.search_pages(search_query)
    collection_id = state.get("collection_parent_id", "")
    source_sha = state.get("source_sha256", "")
    parent_body = ""
    if collection_id:
        try:
            parent_body = client.fetch_page(collection_id).raw_text
        except McpCallError:
            parent_body = ""

    verified_children: list[tuple[str, str]] = []
    for m in matches:
        pid = m["id"]
        if expected_title and m.get("title") and m["title"].strip() != expected_title.strip():
            continue
        compact_id = pid.replace("-", "")
        if parent_body and (pid in parent_body or compact_id in parent_body):
            verified_children.append((pid, m.get("url", "")))
            continue
        fetched = client.fetch_page(pid)
        title_ok = (not expected_title) or (fetched.title.strip() == expected_title.strip())
        content_ok = bool(source_sha and source_sha in fetched.raw_text) or bool(
            marker and marker in fetched.raw_text
        )
        if title_ok and content_ok:
            verified_children.append((pid, fetched.url or m.get("url", "")))

    if len(verified_children) == 1:
        return verified_children[0]
    if len(verified_children) > 1:
        raise SyncError(
            f"Recovery found {len(verified_children)} matching pages for {target}; manual resolution required"
        )
    raise SyncError(
        f"Recovery could not confirm creation status for {target} ({expected_title}); refusing automatic duplicate creation"
    )


def run_init(
    *,
    folder: Path,
    parent_id: str,
    state_path: Path,
    client: MinisMcpNotionClient,
    dry_run: bool = False,
    recover: bool = False,
) -> dict[str, Any]:
    """Safely initialize ONE collection parent + 13 child pages in Notion."""
    clean_parent = normalize_notion_id(parent_id)
    check_state_paths_safety(state_path)
    preflight = preflight_bundle_folder(folder)
    manifest = preflight["manifest"]
    docs: list[dict[str, Any]] = preflight["docs"]

    # Runtime discover and validate required Notion schemas before any write.
    client.require_tools(REQUIRED_NOTION_TOOLS)

    # Confirm passed parent page exists and is readable (never modify it).
    parent_page = client.fetch_page(clean_parent)

    collection_title = manifest.get("bundle_name") or consolidate.DEFAULT_FOLDER_NAME
    task_marker = _collection_marker(manifest["source_sha256"], clean_parent)

    existing_state = load_state(state_path)
    journal_entries = load_journal(state_path)
    unresolved = find_unresolved_journal_ops(journal_entries)

    if dry_run:
        return {
            "status": "dry_run",
            "mode": "init",
            "folder": str(folder),
            "parent_id": clean_parent,
            "parent_title": parent_page.title,
            "state_path": str(state_path),
            "task_marker": task_marker,
            "unresolved_journal_ops": len(unresolved),
            "would_create_collection_parent": not bool(
                existing_state and existing_state.get("collection_parent_id")
            ),
            "collection_title": collection_title,
            "planned_pages": [
                {
                    "doc_id": d["doc_id"],
                    "filename": d["filename"],
                    "title": d["title"],
                    "sha256": d["sha256"],
                    "already_tracked": bool(
                        existing_state
                        and isinstance(existing_state.get("pages"), dict)
                        and d["doc_id"] in existing_state["pages"]
                    ),
                }
                for d in docs
            ],
            "excluded_from_notion": [consolidate.MANIFEST_FILENAME],
        }

    # Refuse restart if uncertain writes exist unless explicit --recover was requested.
    if unresolved and not recover:
        raise UncertainWriteError(
            "Refusing init because unresolved uncertain journal operation(s) exist: "
            + ", ".join(f"{u.get('op_id')}({u.get('target')}:{u.get('status')})" for u in unresolved)
            + ". Re-run with explicit --recover to search by unique task marker before creating pages."
        )

    state: dict[str, Any] = existing_state or {
        "schema_version": 1,
        "folder": str(folder.resolve()),
        "bundle_name": collection_title,
        "source_sha256": manifest["source_sha256"],
        "root_parent_id": clean_parent,
        "task_marker": task_marker,
        "collection_parent_id": None,
        "collection_parent_url": "",
        "pages": {},
    }
    if state.get("root_parent_id") != clean_parent:
        raise SyncError(
            f"State root_parent_id ({state.get('root_parent_id')}) does not match --parent ({clean_parent})"
        )

    docs_by_id = {d["doc_id"]: d for d in docs}
    recovered_ops: list[str] = []

    if unresolved and recover:
        for op in unresolved:
            op_id = op["op_id"]
            target = op.get("target")
            if op.get("op") != "create_page":
                raise SyncError(f"Cannot auto-recover non-create operation: {op_id}")
            recovered_id, recovered_url = _recover_uncertain_create(
                client, op=op, state=state
            ) or ("", "")
            if target == "__collection_parent__":
                state["collection_parent_id"] = recovered_id
                state["collection_parent_url"] = recovered_url
                save_state(state_path, state)
                append_journal(
                    state_path,
                    {
                        "op_id": op_id,
                        "op": "create_page",
                        "target": target,
                        "task_marker": op.get("task_marker", task_marker),
                        "page_id": recovered_id,
                        "status": "recovered",
                    },
                )
                recovered_ops.append(op_id)
            elif isinstance(target, str) and target in docs_by_id:
                doc = docs_by_id[target]
                fetched = client.fetch_page(recovered_id)
                local_canon = canonicalize_markdown(doc["text"], strip_notion_xml=False)
                state["pages"][target] = {
                    "doc_id": target,
                    "filename": doc["filename"],
                    "title": doc["title"],
                    "kind": doc["kind"],
                    "page_id": recovered_id,
                    "url": recovered_url or fetched.url,
                    "base_local_raw_sha256": doc["sha256"],
                    "base_local_canon_sha256": sha256_text(local_canon),
                    "base_remote_raw_sha256": sha256_text(fetched.extracted_body),
                    "base_remote_canon_sha256": sha256_text(fetched.canonical_body),
                    "updated_at": utc_now_iso(),
                }
                save_state(state_path, state)
                append_journal(
                    state_path,
                    {
                        "op_id": op_id,
                        "op": "create_page",
                        "target": target,
                        "task_marker": op.get("task_marker", ""),
                        "page_id": recovered_id,
                        "status": "recovered",
                    },
                )
                recovered_ops.append(op_id)
            else:
                raise SyncError(f"Unknown recovery target in journal: {target}")

    # Step 1: Create ONE collection parent under --parent if not yet created.
    created_parent = False
    if not state.get("collection_parent_id"):
        op_id = f"init:collection_parent:{task_marker}"
        append_journal(
            state_path,
            {
                "op_id": op_id,
                "op": "create_page",
                "target": "__collection_parent__",
                "title": collection_title,
                "task_marker": task_marker,
                "parent_id": clean_parent,
                "status": "send_started",
            },
        )
        try:
            col_id, col_url = client.create_page(
                parent_id=clean_parent,
                title=collection_title,
                content=_collection_parent_body(manifest, task_marker),
            )
        except UncertainWriteError:
            append_journal(
                state_path,
                {
                    "op_id": op_id,
                    "op": "create_page",
                    "target": "__collection_parent__",
                    "title": collection_title,
                    "task_marker": task_marker,
                    "parent_id": clean_parent,
                    "status": "uncertain",
                },
            )
            raise

        state["collection_parent_id"] = col_id
        state["collection_parent_url"] = col_url
        save_state(state_path, state)
        append_journal(
            state_path,
            {
                "op_id": op_id,
                "op": "create_page",
                "target": "__collection_parent__",
                "title": collection_title,
                "task_marker": task_marker,
                "page_id": col_id,
                "status": "completed",
            },
        )
        created_parent = True

    collection_parent_id = state["collection_parent_id"]
    created_pages: list[str] = []
    skipped_pages: list[str] = []

    # Step 2: Create 00 index + 11 cases + 90 reference under collection parent.
    for doc in docs:
        doc_id = doc["doc_id"]
        if doc_id in state["pages"] and state["pages"][doc_id].get("page_id"):
            skipped_pages.append(doc_id)
            continue

        page_marker = f"{task_marker}:{doc_id}"
        op_id = f"init:page:{doc_id}:{manifest['source_sha256'][:12]}"
        append_journal(
            state_path,
            {
                "op_id": op_id,
                "op": "create_page",
                "target": doc_id,
                "title": doc["title"],
                "task_marker": page_marker,
                "parent_id": collection_parent_id,
                "status": "send_started",
            },
        )
        try:
            page_id, page_url = client.create_page(
                parent_id=collection_parent_id,
                title=doc["title"],
                content=doc["text"],
            )
        except UncertainWriteError:
            append_journal(
                state_path,
                {
                    "op_id": op_id,
                    "op": "create_page",
                    "target": doc_id,
                    "title": doc["title"],
                    "task_marker": page_marker,
                    "parent_id": collection_parent_id,
                    "status": "uncertain",
                },
            )
            raise

        # Persist page_id IMMEDIATELY before fetching/baseline update or next operation.
        local_canon = canonicalize_markdown(doc["text"], strip_notion_xml=False)
        state["pages"][doc_id] = {
            "doc_id": doc_id,
            "filename": doc["filename"],
            "title": doc["title"],
            "kind": doc["kind"],
            "page_id": page_id,
            "url": page_url,
            "base_local_raw_sha256": doc["sha256"],
            "base_local_canon_sha256": sha256_text(local_canon),
            "base_remote_raw_sha256": "",
            "base_remote_canon_sha256": "",
            "updated_at": utc_now_iso(),
        }
        save_state(state_path, state)

        # Fetch back initial remote representation to establish separate remote baseline.
        fetched = client.fetch_page(page_id)
        state["pages"][doc_id]["url"] = fetched.url or page_url
        state["pages"][doc_id]["base_remote_raw_sha256"] = sha256_text(fetched.extracted_body)
        state["pages"][doc_id]["base_remote_canon_sha256"] = sha256_text(fetched.canonical_body)
        state["pages"][doc_id]["updated_at"] = utc_now_iso()
        save_state(state_path, state)

        append_journal(
            state_path,
            {
                "op_id": op_id,
                "op": "create_page",
                "target": doc_id,
                "title": doc["title"],
                "task_marker": page_marker,
                "page_id": page_id,
                "status": "completed",
            },
        )
        created_pages.append(doc_id)

    return {
        "status": "ok",
        "mode": "init",
        "folder": str(folder),
        "state_path": str(state_path),
        "root_parent_id": clean_parent,
        "collection_parent_id": collection_parent_id,
        "collection_parent_created": created_parent,
        "task_marker": task_marker,
        "recovered_ops": recovered_ops,
        "created_pages": created_pages,
        "skipped_existing_pages": skipped_pages,
        "total_tracked_pages": len(state["pages"]),
        "excluded_from_notion": [consolidate.MANIFEST_FILENAME],
    }


def _parse_only_targets(only_args: Sequence[str], pages: dict[str, dict[str, Any]]) -> list[str]:
    if not only_args:
        return list(pages.keys())

    requested_tokens: list[str] = []
    for raw in only_args:
        for part in raw.split(","):
            tok = part.strip()
            if tok:
                requested_tokens.append(tok)

    if not requested_tokens:
        return list(pages.keys())

    selected: list[str] = []
    for tok in requested_tokens:
        if ".." in tok or "/" in tok or "\\" in tok:
            raise consolidate.PathSafetyError(f"Invalid --only target selector: {tok}")
        matched: str | None = None
        for doc_id, meta in pages.items():
            fname = meta.get("filename", "")
            stem = Path(fname).stem if fname else ""
            if tok in (doc_id, fname, stem) or (tok == "00" and doc_id == "00_index") or (
                tok == "90" and doc_id == "90_source_reference"
            ):
                matched = doc_id
                break
        if matched is None:
            raise SyncError(f"Unknown --only target: {tok}")
        if matched not in selected:
            selected.append(matched)
    return selected


def _write_conflict_snapshot(
    state_path: Path,
    *,
    doc_id: str,
    page_id: str,
    local_text: str,
    remote_text: str,
    local_canon_sha: str,
    remote_canon_sha: str,
) -> dict[str, str]:
    """Save both local and remote versions under `<state_dir>/conflicts/<doc_id>/` without overwriting."""
    base_conflicts = conflicts_dir_for(state_path)
    if base_conflicts.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink conflicts dir: {base_conflicts}")
    doc_dir = base_conflicts / doc_id
    consolidate.check_no_symlink_chain(doc_dir, stop_at=state_path.parent)
    doc_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    seq = 1
    while True:
        prefix = f"{stamp}_{seq:02d}"
        local_path = doc_dir / f"{prefix}_local.md"
        remote_path = doc_dir / f"{prefix}_remote.md"
        meta_path = doc_dir / f"{prefix}_conflict.json"
        if not (local_path.exists() or remote_path.exists() or meta_path.exists()):
            break
        seq += 1

    consolidate.atomic_write_bytes(local_path, local_text.encode("utf-8"), root=state_path.parent)
    consolidate.atomic_write_bytes(
        remote_path, (remote_text.rstrip("\n") + "\n").encode("utf-8"), root=state_path.parent
    )
    meta_obj = {
        "doc_id": doc_id,
        "page_id": page_id,
        "recorded_at": utc_now_iso(),
        "local_file": str(local_path),
        "remote_file": str(remote_path),
        "local_canon_sha256": local_canon_sha,
        "remote_canon_sha256": remote_canon_sha,
    }
    consolidate.atomic_write_bytes(
        meta_path,
        (json.dumps(meta_obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        root=state_path.parent,
    )
    return {
        "local_snapshot": str(local_path),
        "remote_snapshot": str(remote_path),
        "metadata": str(meta_path),
    }


def run_sync(
    *,
    state_path: Path,
    client: MinisMcpNotionClient,
    folder_override: Path | None = None,
    only: Sequence[str] = (),
    dry_run: bool = False,
    status_only: bool = False,
) -> dict[str, Any]:
    """Run conservative 3-way sync (or read-only `--status` / `--dry-run`)."""
    check_state_paths_safety(state_path)
    state = load_state(state_path)
    if state is None:
        raise SyncError(f"State file does not exist: {state_path}. Run `init` first.")

    folder = folder_override if folder_override is not None else Path(state["folder"])
    if folder.is_symlink():
        raise consolidate.PathSafetyError(f"Refusing symlink folder: {folder}")
    if not folder.exists() or not folder.is_dir():
        raise SyncError(f"Synced folder does not exist or is not a directory: {folder}")

    pages = state.get("pages")
    if not isinstance(pages, dict) or not pages:
        raise SyncError("State file contains no tracked pages")

    journal_entries = load_journal(state_path)
    unresolved = find_unresolved_journal_ops(journal_entries)
    read_only = dry_run or status_only

    if unresolved and not read_only:
        raise UncertainWriteError(
            "Refusing mutating sync because unresolved journal operation(s) exist: "
            + ", ".join(f"{u.get('op_id')}({u.get('target')}:{u.get('status')})" for u in unresolved)
        )

    # Validate required Notion tools via runtime schema discovery.
    required = ["notion-fetch"] if read_only else ["notion-fetch", "notion-update-page"]
    client.require_tools(required)

    target_doc_ids = _parse_only_targets(only, pages)
    results: list[dict[str, Any]] = []
    counts: dict[str, int] = {
        "noop": 0,
        "update_baseline": 0,
        "pull": 0,
        "push": 0,
        "conflict": 0,
        "local_missing": 0,
    }

    for doc_id in target_doc_ids:
        entry = pages[doc_id]
        fname = entry["filename"]
        page_id = normalize_notion_id(entry["page_id"])
        local_path = folder / fname
        consolidate.check_no_symlink_chain(local_path, stop_at=folder)

        if not local_path.exists():
            # Never propagate hard deletions to Notion!
            counts["local_missing"] += 1
            results.append(
                {
                    "doc_id": doc_id,
                    "filename": fname,
                    "page_id": page_id,
                    "action": "local_missing",
                    "note": "Local file missing; hard deletion is never propagated to Notion.",
                }
            )
            continue

        if not local_path.is_file():
            raise consolidate.PathSafetyError(f"Local target is not a regular file: {local_path}")

        local_raw = local_path.read_text(encoding="utf-8")
        local_raw_sha = sha256_text(local_raw)
        local_canon = canonicalize_markdown(local_raw, strip_notion_xml=False)
        local_canon_sha = sha256_text(local_canon)

        fetched = client.fetch_page(page_id)
        remote_raw_sha = sha256_text(fetched.extracted_body)
        remote_canon_sha = sha256_text(fetched.canonical_body)

        base_local_canon = entry.get("base_local_canon_sha256", "")
        base_remote_canon = entry.get("base_remote_canon_sha256", "")
        base_remote_raw = entry.get("base_remote_raw_sha256", "")

        local_changed = local_canon_sha != base_local_canon
        remote_changed = remote_canon_sha != base_remote_canon
        remote_raw_changed = remote_raw_sha != base_remote_raw
        canonically_equal = local_canon_sha == remote_canon_sha

        if not local_changed and not remote_changed and not (remote_raw_changed and canonically_equal):
            counts["noop"] += 1
            results.append(
                {
                    "doc_id": doc_id,
                    "filename": fname,
                    "page_id": page_id,
                    "action": "noop",
                    "local_canon_sha256": local_canon_sha,
                    "remote_canon_sha256": remote_canon_sha,
                }
            )
            continue

        if canonically_equal:
            # Remote (or local formatting) changed, but canonical text is identical -> update baseline only.
            counts["update_baseline"] += 1
            if not read_only:
                entry["base_local_raw_sha256"] = local_raw_sha
                entry["base_local_canon_sha256"] = local_canon_sha
                entry["base_remote_raw_sha256"] = remote_raw_sha
                entry["base_remote_canon_sha256"] = remote_canon_sha
                entry["updated_at"] = utc_now_iso()
                save_state(state_path, state)
                append_journal(
                    state_path,
                    {
                        "op_id": f"sync:baseline:{doc_id}:{local_canon_sha[:12]}",
                        "op": "update_baseline",
                        "target": doc_id,
                        "page_id": page_id,
                        "status": "completed",
                    },
                )
            results.append(
                {
                    "doc_id": doc_id,
                    "filename": fname,
                    "page_id": page_id,
                    "action": "update_baseline",
                    "applied": not read_only,
                    "local_canon_sha256": local_canon_sha,
                    "remote_canon_sha256": remote_canon_sha,
                }
            )
            continue

        if not local_changed and remote_changed:
            # Pull-only: remote edited while local still matches baseline.
            counts["pull"] += 1
            if not read_only:
                pulled_text = fetched.extracted_body.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"
                pulled_raw_sha = sha256_text(pulled_text)
                pulled_canon_sha = sha256_text(
                    canonicalize_markdown(pulled_text, strip_notion_xml=False)
                )
                consolidate.atomic_write_bytes(
                    local_path, pulled_text.encode("utf-8"), root=folder
                )
                entry["base_local_raw_sha256"] = pulled_raw_sha
                entry["base_local_canon_sha256"] = pulled_canon_sha
                entry["base_remote_raw_sha256"] = remote_raw_sha
                entry["base_remote_canon_sha256"] = remote_canon_sha
                entry["updated_at"] = utc_now_iso()
                save_state(state_path, state)
                append_journal(
                    state_path,
                    {
                        "op_id": f"sync:pull:{doc_id}:{remote_canon_sha[:12]}",
                        "op": "pull",
                        "target": doc_id,
                        "page_id": page_id,
                        "status": "completed",
                    },
                )
            results.append(
                {
                    "doc_id": doc_id,
                    "filename": fname,
                    "page_id": page_id,
                    "action": "pull",
                    "applied": not read_only,
                    "local_canon_sha256": local_canon_sha,
                    "remote_canon_sha256": remote_canon_sha,
                }
            )
            continue

        if local_changed and not remote_changed:
            # Push-only: protected baseline confirms remote is unchanged.
            counts["push"] += 1
            if not read_only:
                # Enforce line length safety before pushing to Notion.
                for lineno, line in enumerate(local_raw.split("\n"), start=1):
                    if len(line) >= consolidate.HARD_MD_LINE_LIMIT:
                        raise SyncError(
                            f"Refusing push for {fname}:{lineno}: line length {len(line)} >= {consolidate.HARD_MD_LINE_LIMIT}"
                        )
                op_id = f"sync:push:{doc_id}:{local_canon_sha[:12]}"
                append_journal(
                    state_path,
                    {
                        "op_id": op_id,
                        "op": "push",
                        "target": doc_id,
                        "page_id": page_id,
                        "expected_canon_sha256": local_canon_sha,
                        "status": "push_started",
                    },
                )
                try:
                    client.update_page(page_id=page_id, new_content=local_raw)
                except UncertainWriteError:
                    append_journal(
                        state_path,
                        {
                            "op_id": op_id,
                            "op": "push",
                            "target": doc_id,
                            "page_id": page_id,
                            "expected_canon_sha256": local_canon_sha,
                            "status": "push_uncertain",
                        },
                    )
                    raise

                # Mandatory re-read after push and verify normalized full text.
                verified = client.fetch_page(page_id)
                verified_canon_sha = sha256_text(verified.canonical_body)
                if verified_canon_sha != local_canon_sha:
                    append_journal(
                        state_path,
                        {
                            "op_id": op_id,
                            "op": "push",
                            "target": doc_id,
                            "page_id": page_id,
                            "expected_canon_sha256": local_canon_sha,
                            "actual_remote_canon_sha256": verified_canon_sha,
                            "status": "push_verify_mismatch",
                        },
                    )
                    raise SyncError(
                        f"Post-push verification mismatch for {doc_id}: "
                        f"expected={local_canon_sha}, remote={verified_canon_sha}"
                    )

                entry["base_local_raw_sha256"] = local_raw_sha
                entry["base_local_canon_sha256"] = local_canon_sha
                entry["base_remote_raw_sha256"] = sha256_text(verified.extracted_body)
                entry["base_remote_canon_sha256"] = verified_canon_sha
                entry["updated_at"] = utc_now_iso()
                save_state(state_path, state)
                append_journal(
                    state_path,
                    {
                        "op_id": op_id,
                        "op": "push",
                        "target": doc_id,
                        "page_id": page_id,
                        "verified_canon_sha256": verified_canon_sha,
                        "status": "completed",
                    },
                )
            results.append(
                {
                    "doc_id": doc_id,
                    "filename": fname,
                    "page_id": page_id,
                    "action": "push",
                    "applied": not read_only,
                    "local_canon_sha256": local_canon_sha,
                    "remote_canon_sha256": remote_canon_sha,
                }
            )
            continue

        # Both changed and canonically different -> 3-way conflict; leave BOTH sides untouched!
        counts["conflict"] += 1
        conflict_files: dict[str, str] | None = None
        if not read_only:
            conflict_files = _write_conflict_snapshot(
                state_path,
                doc_id=doc_id,
                page_id=page_id,
                local_text=local_raw,
                remote_text=fetched.extracted_body,
                local_canon_sha=local_canon_sha,
                remote_canon_sha=remote_canon_sha,
            )
            append_journal(
                state_path,
                {
                    "op_id": f"sync:conflict:{doc_id}:{local_canon_sha[:8]}:{remote_canon_sha[:8]}",
                    "op": "conflict",
                    "target": doc_id,
                    "page_id": page_id,
                    "local_canon_sha256": local_canon_sha,
                    "remote_canon_sha256": remote_canon_sha,
                    "status": "conflict_recorded",
                },
            )
        results.append(
            {
                "doc_id": doc_id,
                "filename": fname,
                "page_id": page_id,
                "action": "conflict",
                "applied": False,
                "local_canon_sha256": local_canon_sha,
                "remote_canon_sha256": remote_canon_sha,
                "conflict_files": conflict_files,
            }
        )

    mode = "status" if status_only else ("dry_run" if dry_run else "sync")
    overall_status = "conflict" if counts["conflict"] > 0 else "ok"
    return {
        "status": overall_status,
        "mode": mode,
        "state_path": str(state_path),
        "folder": str(folder),
        "unresolved_journal_ops": len(unresolved),
        "summary": counts,
        "pages": results,
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync.py",
        description="Black9 Notion<->Obsidian conservative 3-way sync via Minis MCP (stdlib only).",
    )
    parser.add_argument(
        "--cli-bin",
        default="minis-mcp-cli",
        help="Path or name of Minis MCP CLI binary (default: minis-mcp-cli).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser(
        "inspect",
        help="Discover and dump relevant Notion MCP tool schemas (no secrets).",
    )
    inspect_p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON file to write discovered schemas.",
    )

    init_p = sub.add_parser(
        "init",
        help="Create ONE collection parent + 13 child pages under an existing Notion page.",
    )
    init_p.add_argument("--folder", required=True, type=Path, help="Built bundle folder path.")
    init_p.add_argument("--parent", required=True, help="Existing Notion parent page ID.")
    init_p.add_argument("--state", required=True, type=Path, help="Sync state JSON file path.")
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preflight and plan without writing state, journal, or Notion pages.",
    )
    init_p.add_argument(
        "--recover",
        action="store_true",
        help="Explicitly search by unique task marker to recover from an uncertain send.",
    )

    sync_p = sub.add_parser(
        "sync",
        help="Run conservative 3-way sync between local bundle folder and Notion pages.",
    )
    sync_p.add_argument("--state", required=True, type=Path, help="Sync state JSON file path.")
    sync_p.add_argument(
        "--folder",
        type=Path,
        default=None,
        help="Optional override path for local bundle folder.",
    )
    sync_p.add_argument(
        "--only",
        action="append",
        default=[],
        help="Targeted doc/case selector (e.g. C01 or C01,C10; repeatable).",
    )
    sync_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute planned actions without writing files, state, or Notion.",
    )
    sync_p.add_argument(
        "--status",
        action="store_true",
        help="Read-only status check comparing local, remote, and baseline.",
    )

    status_p = sub.add_parser(
        "status",
        help="Alias for `sync --status` (read-only status check).",
    )
    status_p.add_argument("--state", required=True, type=Path, help="Sync state JSON file path.")
    status_p.add_argument("--folder", type=Path, default=None, help="Optional folder override.")
    status_p.add_argument("--only", action="append", default=[], help="Targeted selector.")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    client = MinisMcpNotionClient(cli_bin=args.cli_bin)

    try:
        if args.command == "inspect":
            report = client.inspect_relevant_schemas()
            if args.output is not None:
                if args.output.is_symlink():
                    raise consolidate.PathSafetyError(f"Refusing symlink output: {args.output}")
                data = (
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                consolidate.atomic_write_bytes(args.output, data)
        elif args.command == "init":
            report = run_init(
                folder=args.folder,
                parent_id=args.parent,
                state_path=args.state,
                client=client,
                dry_run=args.dry_run,
                recover=args.recover,
            )
        elif args.command == "sync":
            report = run_sync(
                state_path=args.state,
                client=client,
                folder_override=args.folder,
                only=args.only,
                dry_run=args.dry_run,
                status_only=args.status,
            )
        elif args.command == "status":
            report = run_sync(
                state_path=args.state,
                client=client,
                folder_override=args.folder,
                only=args.only,
                dry_run=False,
                status_only=True,
            )
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (SyncError, consolidate.ConsolidateError) as exc:
        err_obj = {"status": "error", "error": consolidate.redact_secrets(str(exc))}
        print(json.dumps(err_obj, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
