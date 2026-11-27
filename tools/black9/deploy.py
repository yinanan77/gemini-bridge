#!/usr/bin/env python3
"""Black9 safe GitHub Actions registration payload preparer and local vault installer (stdlib only).

Works with the existing `yinanan77/E-Made` repository and its existing GitHub Actions
workflow (`scripts/notion_sync.py`). Never creates or edits schedules, services,
workflows, or sync scripts. Never reads OAuth tokens, credentials, or `/var/minis`.
Never auto-publishes to GitHub; exports a reviewed `push_files_payload.json` for
explicit controller execution.
"""

from __future__ import annotations

import argparse
import base64
import copy
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

try:
    from . import consolidate, sync
except ImportError:  # pragma: no cover - direct script execution
    _SCRIPT_DIR = str(Path(__file__).resolve().parent)
    if _SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _SCRIPT_DIR)
    import consolidate  # type: ignore[no-redef]
    import sync  # type: ignore[no-redef]

DEFAULT_GITHUB_SERVER = "GitHub"
DEFAULT_OWNER = "yinanan77"
DEFAULT_REPO = "E-Made"
DEFAULT_BRANCH = "main"
DEFAULT_STATE_REPO_PATH = ".sync/state.json"
DEFAULT_CONFIG_REPO_PATH = "sync-config.json"
DEFAULT_COMMIT_MESSAGE = (
    "feat(black9): add Black9 consolidated folder and register Notion sync metadata"
)

RELEVANT_GITHUB_TOOLS: tuple[str, ...] = ("get_file_contents", "push_files")
REQUIRED_GITHUB_TOOLS: tuple[str, ...] = ("get_file_contents",)

_DISALLOWED_PUSH_PREFIXES: tuple[str, ...] = (
    ".github/",
    "scripts/",
)


class DeployError(Exception):
    """Base error for Black9 deployment preparation and installation."""


def _validate_rel_posix_path(rel_path: str, label: str) -> str:
    clean = rel_path.strip().strip("/")
    if not clean:
        raise DeployError(f"{label} must not be empty")
    if "\\" in clean or clean.startswith("/"):
        raise consolidate.PathSafetyError(f"Invalid relative POSIX path for {label}: {rel_path}")
    parts = clean.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise consolidate.PathSafetyError(f"Path traversal rejected in {label}: {rel_path}")
    return "/".join(parts)


def _extract_github_file_text(stdout_text: str, tool_name: str, repo_path: str) -> tuple[str, dict[str, Any]]:
    """Extract UTF-8 text content from a GitHub MCP `get_file_contents` response."""
    try:
        envelope = json.loads(stdout_text)
    except json.JSONDecodeError as exc:
        raise sync.McpCallError(f"{tool_name}({repo_path}): malformed JSON response: {exc}") from exc

    if not isinstance(envelope, dict):
        raise sync.McpCallError(f"{tool_name}({repo_path}): top-level MCP response must be an object")
    if envelope.get("error"):
        raise sync.McpCallError(
            f"{tool_name}({repo_path}): MCP error: {consolidate.redact_secrets(str(envelope['error']))}"
        )

    result_obj = envelope.get("result")
    if not isinstance(result_obj, dict):
        raise sync.McpCallError(f"{tool_name}({repo_path}): MCP response missing `result` object")
    if result_obj.get("isError"):
        raise sync.McpCallError(f"{tool_name}({repo_path}): MCP tool returned isError=true")

    content_list = result_obj.get("content")
    if not isinstance(content_list, list) or not content_list:
        raise sync.McpCallError(f"{tool_name}({repo_path}): missing non-empty `result.content`")

    # Prefer embedded resource text if present, else text items.
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "resource":
            res = item.get("resource")
            if isinstance(res, dict) and isinstance(res.get("text"), str):
                return res["text"], {"encoding": "resource_text"}

    text_items = [
        item["text"]
        for item in content_list
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ]
    if not text_items:
        raise sync.McpCallError(f"{tool_name}({repo_path}): no text content item in MCP response")

    # Inspect text items for GitHub API file metadata wrapper or direct file text.
    for raw in text_items:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return raw, {"encoding": "plain_text"}

        if isinstance(parsed, dict) and isinstance(parsed.get("content"), str):
            enc = str(parsed.get("encoding", "")).lower()
            meta = {
                "sha": parsed.get("sha", ""),
                "path": parsed.get("path", repo_path),
                "encoding": enc or "utf-8",
            }
            if enc == "base64":
                try:
                    decoded = base64.b64decode(parsed["content"]).decode("utf-8")
                except Exception as exc:
                    raise sync.McpCallError(
                        f"{tool_name}({repo_path}): failed to decode base64 content: {exc}"
                    ) from exc
                return decoded, meta
            return parsed["content"], meta

        # Direct JSON file contents (e.g. .sync/state.json or sync-config.json returned verbatim).
        if isinstance(parsed, dict):
            return raw, {"encoding": "direct_json"}

    return text_items[0], {"encoding": "plain_text"}


class MinisMcpGitHubClient:
    """Read-only subprocess adapter for GitHub via `minis-mcp-cli` with runtime schema discovery."""

    def __init__(
        self,
        *,
        cli_bin: str = "minis-mcp-cli",
        server_name: str = DEFAULT_GITHUB_SERVER,
        timeout_sec: int = 60,
    ) -> None:
        self.cli_bin = cli_bin
        self.server_name = server_name
        self.timeout_sec = timeout_sec
        self._schemas: dict[str, dict[str, Any]] | None = None

    def _run_cli(self, args: Sequence[str]) -> str:
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
            raise sync.McpCallError(f"MCP CLI executable not found: {self.cli_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise sync.McpCallError(f"minis-mcp-cli timed out after {self.timeout_sec}s") from exc

        if proc.returncode != 0:
            snippet = consolidate.redact_secrets((proc.stderr or proc.stdout or "").strip())[:300]
            raise sync.McpCallError(f"minis-mcp-cli exited with code {proc.returncode}: {snippet}")
        return proc.stdout

    def discover_schemas(self, *, force: bool = False) -> dict[str, dict[str, Any]]:
        if self._schemas is not None and not force:
            return self._schemas
        stdout = self._run_cli(["tools", self.server_name])
        all_tools = sync._parse_tools_list(stdout)
        self._schemas = all_tools
        return all_tools

    def require_tools(self, names: Sequence[str] = REQUIRED_GITHUB_TOOLS) -> dict[str, dict[str, Any]]:
        tools = self.discover_schemas()
        missing = [name for name in names if name not in tools]
        if missing:
            raise sync.SchemaValidationError(
                f"Required GitHub MCP tool(s) missing from discovered schema ({self.server_name}): {missing}"
            )
        return {name: tools[name] for name in names}

    def inspect_relevant_schemas(self) -> dict[str, Any]:
        tools = self.discover_schemas()
        relevant = {name: tools[name] for name in RELEVANT_GITHUB_TOOLS if name in tools}
        missing_required = [name for name in REQUIRED_GITHUB_TOOLS if name not in relevant]
        return {
            "server": self.server_name,
            "status": "ok" if not missing_required else "incomplete",
            "discovered_relevant_tools": sorted(relevant.keys()),
            "missing_required_tools": missing_required,
            "tools": relevant,
        }

    def _build_get_file_contents_payload(
        self, *, owner: str, repo: str, path: str, branch: str
    ) -> dict[str, Any]:
        tool = self.require_tools(["get_file_contents"])["get_file_contents"]
        schema = tool["inputSchema"]
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        payload: dict[str, Any] = {
            "owner": owner,
            "repo": repo,
            "path": path,
        }
        if "branch" in props or not props:
            payload["branch"] = branch
        elif "ref" in props:
            payload["ref"] = branch
        sync._validate_required_fields(schema, payload, "get_file_contents")
        return payload

    def get_file_contents(
        self, *, owner: str, repo: str, path: str, branch: str
    ) -> tuple[str, dict[str, Any]]:
        clean_path = _validate_rel_posix_path(path, "github path")
        payload = self._build_get_file_contents_payload(
            owner=owner, repo=repo, path=clean_path, branch=branch
        )
        stdout = self._run_cli(
            [
                "call",
                self.server_name,
                "get_file_contents",
                json.dumps(payload, ensure_ascii=False),
            ]
        )
        return _extract_github_file_text(stdout, "get_file_contents", clean_path)

    def validate_push_files_payload_if_discovered(self, payload: dict[str, Any]) -> bool:
        """If `push_files` schema is present on the GitHub MCP server, validate required fields."""
        tools = self.discover_schemas()
        tool = tools.get("push_files")
        if not tool:
            return False
        schema = tool.get("inputSchema")
        if isinstance(schema, dict):
            sync._validate_required_fields(schema, payload, "push_files")
        return True


def _resolve_folder_relpath(
    bundle_dir: Path, vault_dir: Path, manifest: dict[str, Any], override: str | None
) -> str:
    if override is not None and override.strip():
        return _validate_rel_posix_path(override, "folder_relpath")
    try:
        rel = bundle_dir.resolve().relative_to(vault_dir.resolve())
        if rel.parts:
            return _validate_rel_posix_path(rel.as_posix(), "bundle relative path")
    except ValueError:
        pass
    bundle_name = manifest.get("bundle_name") or bundle_dir.name or consolidate.DEFAULT_FOLDER_NAME
    return _validate_rel_posix_path(str(bundle_name), "bundle_name")


def _write_idempotent_bytes(path: Path, data: bytes, *, root: Path) -> str:
    consolidate.check_no_symlink_chain(path, stop_at=root)
    if path.exists():
        if not path.is_file():
            raise consolidate.PathSafetyError(f"Output path exists and is not a regular file: {path}")
        if path.read_bytes() != data:
            raise consolidate.PathSafetyError(
                f"Refusing to overwrite existing non-identical file: {path}"
            )
        return "identical"
    consolidate.atomic_write_bytes(path, data, root=root)
    return "create"


def _backup_local_vault_metadata(
    vault_dir: Path,
    backup_dir: Path,
    *,
    root: Path,
    state_repo_path: str = DEFAULT_STATE_REPO_PATH,
    config_repo_path: str = DEFAULT_CONFIG_REPO_PATH,
) -> dict[str, str]:
    """Read-only backup of any existing local vault `.sync/state.json` and `sync-config.json`."""
    backups: dict[str, str] = {}
    targets = (
        ("local_state_backup", vault_dir / Path(state_repo_path), backup_dir / "local_state.json"),
        (
            "local_config_backup",
            vault_dir / Path(config_repo_path),
            backup_dir / "local_sync-config.json",
        ),
    )
    for label, src, dst in targets:
        if src.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink vault metadata file: {src}")
        if src.exists():
            consolidate.check_no_symlink_chain(src, stop_at=vault_dir)
            if not src.is_file():
                raise consolidate.PathSafetyError(f"Vault metadata path is not a file: {src}")
            raw = src.read_bytes()
            _write_idempotent_bytes(dst, raw, root=root)
            backups[label] = str(dst)
    return backups


def _detect_canonical_key_prefix(existing_canonical: dict[str, Any]) -> str:
    """Detect whether existing `canonical_page_ids` uses `source::` keys or plain relative paths."""
    if existing_canonical and all(
        isinstance(k, str) and k.startswith("source::") for k in existing_canonical
    ):
        return "source::"
    return ""


def _parse_json_object(raw_text: str, label: str) -> dict[str, Any]:
    try:
        obj = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DeployError(f"{label} is not valid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise DeployError(f"{label} root must be a JSON object")
    return obj


def _merge_sync_state_and_config(
    *,
    existing_state: dict[str, Any],
    existing_config: dict[str, Any],
    registrations: Sequence[dict[str, str]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Safely merge ONLY our 13 Black9 bundle entries into existing state and config."""
    if "version" not in existing_state or not isinstance(existing_state.get("items"), dict):
        raise DeployError(
            "GitHub .sync/state.json must contain integer `version` and object `items`"
        )

    merged_state = copy.deepcopy(existing_state)
    merged_config = copy.deepcopy(existing_config)

    existing_items: dict[str, Any] = existing_state["items"]
    merged_items: dict[str, Any] = merged_state["items"]

    canonical_raw = merged_config.get("canonical_page_ids")
    if canonical_raw is None:
        canonical_map: dict[str, Any] = {}
        merged_config["canonical_page_ids"] = canonical_map
    elif isinstance(canonical_raw, dict):
        canonical_map = canonical_raw
    else:
        raise DeployError("GitHub sync-config.json field `canonical_page_ids` must be an object")

    manual_raw = merged_config.get("manually_managed_source_paths")
    if manual_raw is not None and not isinstance(manual_raw, list):
        raise DeployError(
            "GitHub sync-config.json field `manually_managed_source_paths` must be an array"
        )
    manual_set = {p for p in (manual_raw or []) if isinstance(p, str)}

    canonical_prefix = _detect_canonical_key_prefix(canonical_map)

    added_state_keys: list[str] = []
    updated_state_keys: list[str] = []
    added_canonical_keys: list[str] = []

    for reg in registrations:
        rel_path = reg["rel_path"]
        page_id = reg["page_id"]
        baseline_hash = reg["hash"]

        if rel_path in manual_set:
            raise DeployError(
                f"Refusing to register `{rel_path}` because it is listed in manually_managed_source_paths"
            )

        state_key = f"source::{rel_path}"
        new_item = {
            "id": page_id,
            "kind": "source",
            "hash": baseline_hash,
            "source": rel_path,
        }
        if state_key in merged_items:
            prev = merged_items[state_key]
            if not isinstance(prev, dict) or prev.get("id") != page_id:
                raise DeployError(
                    f"Collision in .sync/state.json for `{state_key}`: existing entry points to a different page ID"
                )
            merged_items[state_key] = new_item
            updated_state_keys.append(state_key)
        else:
            merged_items[state_key] = new_item
            added_state_keys.append(state_key)

        canon_key = f"{canonical_prefix}{rel_path}"
        if canon_key in canonical_map:
            if canonical_map[canon_key] != page_id:
                raise DeployError(
                    f"Collision in sync-config.json canonical_page_ids for `{canon_key}`: "
                    "existing mapping points to a different page ID"
                )
        else:
            canonical_map[canon_key] = page_id
            added_canonical_keys.append(canon_key)

    # Strict preservation invariant: every unrelated existing key must remain identical.
    our_state_keys = {f"source::{r['rel_path']}" for r in registrations}
    for old_key, old_val in existing_items.items():
        if old_key not in our_state_keys:
            if merged_items.get(old_key) != old_val:
                raise DeployError(f"Preservation check failed for existing state item: {old_key}")

    our_canon_keys = {f"{canonical_prefix}{r['rel_path']}" for r in registrations}
    for old_k, old_v in existing_config.items():
        if old_k == "canonical_page_ids":
            if isinstance(old_v, dict):
                for ck, cv in old_v.items():
                    if ck not in our_canon_keys and canonical_map.get(ck) != cv:
                        raise DeployError(
                            f"Preservation check failed for existing canonical_page_ids entry: {ck}"
                        )
        else:
            if merged_config.get(old_k) != old_v:
                raise DeployError(f"Preservation check failed for existing config field: {old_k}")

    stats = {
        "existing_state_items_count": len(existing_items),
        "preserved_unrelated_state_items_count": sum(
            1 for k in existing_items if k not in our_state_keys
        ),
        "added_state_items_count": len(added_state_keys),
        "updated_same_id_state_items_count": len(updated_state_keys),
        "total_merged_state_items_count": len(merged_items),
        "added_canonical_ids_count": len(added_canonical_keys),
        "preserved_manually_managed_count": len(manual_raw or []),
        "canonical_key_prefix": canonical_prefix,
    }
    return merged_state, merged_config, stats


def run_prepare(
    *,
    bundle_dir: Path,
    sync_state_path: Path,
    vault_dir: Path,
    out_dir: Path,
    github_client: MinisMcpGitHubClient,
    notion_client: sync.MinisMcpNotionClient,
    owner: str = DEFAULT_OWNER,
    repo: str = DEFAULT_REPO,
    branch: str = DEFAULT_BRANCH,
    state_repo_path: str = DEFAULT_STATE_REPO_PATH,
    config_repo_path: str = DEFAULT_CONFIG_REPO_PATH,
    folder_relpath: str | None = None,
    commit_message: str = DEFAULT_COMMIT_MESSAGE,
) -> dict[str, Any]:
    """Prepare a safe GitHub `push_files` payload and metadata backups without auto-publishing."""
    for label, p in (
        ("bundle", bundle_dir),
        ("sync-state", sync_state_path),
        ("vault", vault_dir),
        ("out", out_dir),
    ):
        if p.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink {label} path: {p}")
        if p.parent.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink parent of {label} path: {p.parent}")

    if not vault_dir.exists() or not vault_dir.is_dir():
        raise DeployError(f"Vault directory does not exist or is not a directory: {vault_dir}")

    # 1. Preflight bundle (11 cases, 335 units, 108 groups, optional 44 supplement, flat no-blank-line md).
    preflight = sync.preflight_bundle_folder(bundle_dir)
    manifest: dict[str, Any] = preflight["manifest"]
    docs: list[dict[str, Any]] = preflight["docs"]

    if manifest.get("total_groups") != consolidate.EXPECTED_TOTAL_GROUPS:
        raise DeployError(
            f"Bundle manifest must include verified {consolidate.EXPECTED_TOTAL_GROUPS} Appendix A groups "
            f"(got {manifest.get('total_groups')})"
        )

    # 2. Load and validate sync.py init state (reject unresolved uncertain journal ops).
    sync.check_state_paths_safety(sync_state_path)
    init_state = sync.load_state(sync_state_path)
    if init_state is None:
        raise DeployError(f"Sync state file does not exist: {sync_state_path}")
    unresolved = sync.find_unresolved_journal_ops(sync.load_journal(sync_state_path))
    if unresolved:
        raise sync.UncertainWriteError(
            "Refusing prepare because unresolved sync journal operation(s) exist: "
            + ", ".join(f"{u.get('op_id')}({u.get('target')}:{u.get('status')})" for u in unresolved)
        )

    if init_state.get("source_sha256") and init_state["source_sha256"] != manifest.get("source_sha256"):
        raise DeployError(
            "Sync state source_sha256 does not match bundle manifest source_sha256; "
            "re-init or point to matching bundle/state"
        )

    tracked_pages = init_state.get("pages")
    if not isinstance(tracked_pages, dict):
        raise DeployError("Sync state missing `pages` mapping")

    rel_folder = _resolve_folder_relpath(bundle_dir, vault_dir, manifest, folder_relpath)
    clean_state_repo_path = _validate_rel_posix_path(state_repo_path, "state_repo_path")
    clean_config_repo_path = _validate_rel_posix_path(config_repo_path, "config_repo_path")

    # 3. Verify EVERY known remote Notion page matches local canonical FULL content via notion-fetch.
    notion_client.require_tools(["notion-fetch"])
    registrations: list[dict[str, str]] = []
    verified_pages_report: list[dict[str, Any]] = []
    seen_page_ids: set[str] = set()

    for doc in docs:
        doc_id = doc["doc_id"]
        fname = doc["filename"]
        page_meta = tracked_pages.get(doc_id)
        if not isinstance(page_meta, dict) or not isinstance(page_meta.get("page_id"), str):
            raise DeployError(f"Sync state missing tracked Notion page_id for `{doc_id}`")
        if page_meta.get("filename") and page_meta["filename"] != fname:
            raise DeployError(
                f"Sync state filename mismatch for `{doc_id}`: state={page_meta.get('filename')}, bundle={fname}"
            )
        page_id = sync.normalize_notion_id(page_meta["page_id"])
        if page_id in seen_page_ids:
            raise DeployError(f"Duplicate Notion page_id across bundle docs: {page_id}")
        seen_page_ids.add(page_id)

        local_text = doc["text"]
        local_canon = sync.canonicalize_markdown(local_text, strip_notion_xml=False)
        local_canon_sha = sync.sha256_text(local_canon)

        fetched = notion_client.fetch_page(page_id)
        remote_canon_sha = sync.sha256_text(fetched.canonical_body)
        if remote_canon_sha != local_canon_sha:
            raise DeployError(
                f"Remote Notion page `{doc_id}` ({page_id}) failed canonical FULL content equality check: "
                f"local_canon={local_canon_sha}, remote_canon={remote_canon_sha}"
            )

        rel_path = f"{rel_folder}/{fname}"
        baseline_hash = doc["sha256"]
        registrations.append(
            {
                "doc_id": doc_id,
                "filename": fname,
                "rel_path": rel_path,
                "page_id": page_id,
                "hash": baseline_hash,
            }
        )
        verified_pages_report.append(
            {
                "doc_id": doc_id,
                "rel_path": rel_path,
                "page_id": page_id,
                "baseline_sha256": baseline_hash,
                "canonical_sha256": local_canon_sha,
                "remote_verified": True,
            }
        )

    # 4. Fetch latest GitHub .sync/state.json and sync-config.json via minis-mcp-cli get_file_contents.
    github_client.require_tools(["get_file_contents"])
    gh_state_raw, _ = github_client.get_file_contents(
        owner=owner, repo=repo, path=clean_state_repo_path, branch=branch
    )
    gh_config_raw, _ = github_client.get_file_contents(
        owner=owner, repo=repo, path=clean_config_repo_path, branch=branch
    )

    existing_state = _parse_json_object(gh_state_raw, f"GitHub {clean_state_repo_path}")
    existing_config = _parse_json_object(gh_config_raw, f"GitHub {clean_config_repo_path}")

    # 5. Safe additive merge preserving all existing state items and all config fields.
    merged_state, merged_config, merge_stats = _merge_sync_state_and_config(
        existing_state=existing_state,
        existing_config=existing_config,
        registrations=registrations,
    )

    merged_state_text = json.dumps(merged_state, ensure_ascii=False, indent=2) + "\n"
    merged_config_text = json.dumps(merged_config, ensure_ascii=False, indent=2) + "\n"

    # 6. Build GitHub push_files payload with ONLY new folder files (including manifest) + state + config.
    manifest_text = (bundle_dir / consolidate.MANIFEST_FILENAME).read_text(encoding="utf-8")
    push_files: list[dict[str, str]] = []
    for doc in docs:
        push_files.append(
            {
                "path": f"{rel_folder}/{doc['filename']}",
                "content": doc["text"],
            }
        )
    push_files.append(
        {
            "path": f"{rel_folder}/{consolidate.MANIFEST_FILENAME}",
            "content": manifest_text,
        }
    )
    push_files.append(
        {
            "path": clean_state_repo_path,
            "content": merged_state_text,
        }
    )
    push_files.append(
        {
            "path": clean_config_repo_path,
            "content": merged_config_text,
        }
    )

    # Guard: never emit workflows, scripts, or duplicate paths in push_files payload.
    seen_push_paths: set[str] = set()
    for entry in push_files:
        p = entry["path"]
        if any(p.startswith(bad) for bad in _DISALLOWED_PUSH_PREFIXES):
            raise DeployError(f"Refusing disallowed path in push_files payload: {p}")
        if p in seen_push_paths:
            raise DeployError(f"Duplicate path in push_files payload: {p}")
        seen_push_paths.add(p)

    push_payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "message": commit_message,
        "files": push_files,
    }
    push_schema_validated = github_client.validate_push_files_payload_if_discovered(push_payload)

    # 7. Write backups, merged previews, payload, and report into out_dir (no auto-publish!).
    out_dir.mkdir(parents=True, exist_ok=True)
    backup_dir = out_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    gh_state_backup_path = backup_dir / "github_state.json"
    gh_config_backup_path = backup_dir / "github_sync-config.json"
    _write_idempotent_bytes(gh_state_backup_path, gh_state_raw.encode("utf-8"), root=out_dir)
    _write_idempotent_bytes(gh_config_backup_path, gh_config_raw.encode("utf-8"), root=out_dir)

    local_backups = _backup_local_vault_metadata(
        vault_dir,
        backup_dir,
        root=out_dir,
        state_repo_path=clean_state_repo_path,
        config_repo_path=clean_config_repo_path,
    )

    payload_path = out_dir / "push_files_payload.json"
    merged_state_path = out_dir / "merged_state.json"
    merged_config_path = out_dir / "merged_sync-config.json"
    report_path = out_dir / "prepare_report.json"

    payload_bytes = (json.dumps(push_payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    merged_state_bytes = merged_state_text.encode("utf-8")
    merged_config_bytes = merged_config_text.encode("utf-8")

    _write_idempotent_bytes(payload_path, payload_bytes, root=out_dir)
    _write_idempotent_bytes(merged_state_path, merged_state_bytes, root=out_dir)
    _write_idempotent_bytes(merged_config_path, merged_config_bytes, root=out_dir)

    report: dict[str, Any] = {
        "status": "ok",
        "mode": "prepare",
        "auto_published": False,
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "folder_relpath": rel_folder,
        "push_schema_validated": push_schema_validated,
        "push_files_payload_path": str(payload_path),
        "push_files_payload_sha256": consolidate.sha256_bytes(payload_bytes),
        "push_files_count": len(push_files),
        "push_file_paths": [f["path"] for f in push_files],
        "merged_state_path": str(merged_state_path),
        "merged_config_path": str(merged_config_path),
        "backups": {
            "github_state_backup": str(gh_state_backup_path),
            "github_config_backup": str(gh_config_backup_path),
            **local_backups,
        },
        "merge_stats": merge_stats,
        "registered_pages_count": len(verified_pages_report),
        "registered_pages": verified_pages_report,
    }
    report_bytes = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_idempotent_bytes(report_path, report_bytes, root=out_dir)
    report["prepare_report_path"] = str(report_path)
    return report


def run_install(
    *,
    bundle_dir: Path,
    vault_dir: Path,
    out_dir: Path | None = None,
    folder_relpath: str | None = None,
    dry_run: bool = False,
    install_metadata_from: Path | None = None,
    state_repo_path: str = DEFAULT_STATE_REPO_PATH,
    config_repo_path: str = DEFAULT_CONFIG_REPO_PATH,
) -> dict[str, Any]:
    """Install ONLY the new Black9 folder files into the actual Obsidian vault safely.

    Rejects overwrites unless byte-identical. Protects all existing pages and local originals.
    Never overwrites existing local `.sync/state.json` or `sync-config.json` automatically;
    explicit `--install-metadata-from` requires `--out` backup directory and backs up first.
    """
    for label, p in (("bundle", bundle_dir), ("vault", vault_dir)):
        if p.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink {label} path: {p}")
        if p.parent.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink parent of {label} path: {p.parent}")
    if out_dir is not None:
        if out_dir.is_symlink() or out_dir.parent.is_symlink():
            raise consolidate.PathSafetyError(f"Refusing symlink out path: {out_dir}")
    if install_metadata_from is not None:
        if install_metadata_from.is_symlink():
            raise consolidate.PathSafetyError(
                f"Refusing symlink install-metadata-from path: {install_metadata_from}"
            )
        if out_dir is None:
            raise DeployError(
                "`--install-metadata-from` requires `--out BACKUP_DIR` so existing local sync metadata is backed up before installation"
            )

    if not vault_dir.exists() or not vault_dir.is_dir():
        raise DeployError(f"Vault directory does not exist or is not a directory: {vault_dir}")

    preflight = sync.preflight_bundle_folder(bundle_dir)
    manifest: dict[str, Any] = preflight["manifest"]
    docs: list[dict[str, Any]] = preflight["docs"]

    rel_folder = _resolve_folder_relpath(bundle_dir, vault_dir, manifest, folder_relpath)
    target_dir = vault_dir / Path(rel_folder)
    consolidate.check_no_symlink_chain(target_dir, stop_at=vault_dir)

    planned_bytes: dict[str, bytes] = {}
    for doc in docs:
        planned_bytes[doc["filename"]] = (bundle_dir / doc["filename"]).read_bytes()
    planned_bytes[consolidate.MANIFEST_FILENAME] = (
        bundle_dir / consolidate.MANIFEST_FILENAME
    ).read_bytes()

    # Strict non-overwrite / unexpected-file check BEFORE writing anything.
    file_statuses = consolidate.check_destination_safety(target_dir, planned_bytes)

    clean_state_repo_path = _validate_rel_posix_path(state_repo_path, "state_repo_path")
    clean_config_repo_path = _validate_rel_posix_path(config_repo_path, "config_repo_path")

    metadata_payloads: dict[str, tuple[Path, bytes]] = {}
    if install_metadata_from is not None:
        m_state = install_metadata_from / "merged_state.json"
        m_config = install_metadata_from / "merged_sync-config.json"
        consolidate.ensure_safe_file_read(m_state)
        consolidate.ensure_safe_file_read(m_config)
        st_bytes = m_state.read_bytes()
        cfg_bytes = m_config.read_bytes()
        _parse_json_object(st_bytes.decode("utf-8"), "merged_state.json")
        _parse_json_object(cfg_bytes.decode("utf-8"), "merged_sync-config.json")
        dst_state = vault_dir / Path(clean_state_repo_path)
        dst_config = vault_dir / Path(clean_config_repo_path)
        consolidate.check_no_symlink_chain(dst_state, stop_at=vault_dir)
        consolidate.check_no_symlink_chain(dst_config, stop_at=vault_dir)
        metadata_payloads = {
            clean_state_repo_path: (dst_state, st_bytes),
            clean_config_repo_path: (dst_config, cfg_bytes),
        }

    backups: dict[str, str] = {}
    metadata_installed: list[str] = []

    if not dry_run:
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            backup_dir = out_dir / "backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            backups = _backup_local_vault_metadata(
                vault_dir,
                backup_dir,
                root=out_dir,
                state_repo_path=clean_state_repo_path,
                config_repo_path=clean_config_repo_path,
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        for fname, data in planned_bytes.items():
            if file_statuses[fname] == "create":
                consolidate.atomic_write_bytes(target_dir / fname, data, root=vault_dir)

        if metadata_payloads:
            # Verify backups exist for any pre-existing local metadata before installing.
            for label, src_rel, bkey in (
                ("state", clean_state_repo_path, "local_state_backup"),
                ("config", clean_config_repo_path, "local_config_backup"),
            ):
                dst_path = vault_dir / Path(src_rel)
                if dst_path.exists() and bkey not in backups:
                    raise DeployError(f"Missing required pre-install backup for existing local {label}")
            for rel_key, (dst_path, data) in metadata_payloads.items():
                consolidate.atomic_write_bytes(dst_path, data, root=vault_dir)
                metadata_installed.append(rel_key)

    return {
        "status": "dry_run" if dry_run else "ok",
        "mode": "install",
        "bundle_dir": str(bundle_dir),
        "vault_dir": str(vault_dir),
        "target_dir": str(target_dir),
        "folder_relpath": rel_folder,
        "local_sync_metadata_overwritten": bool(metadata_installed),
        "metadata_installed": metadata_installed,
        "backups": backups,
        "files": [
            {
                "filename": fname,
                "sha256": consolidate.sha256_bytes(data),
                "action": file_statuses[fname],
            }
            for fname, data in sorted(planned_bytes.items())
        ],
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deploy.py",
        description=(
            "Black9 safe GitHub Actions registration payload preparer and local vault installer "
            "(uses minis-mcp-cli; no credentials; no workflow/schedule edits; no auto-publish)."
        ),
    )
    parser.add_argument(
        "--cli-bin",
        default="minis-mcp-cli",
        help="Path or name of Minis MCP CLI binary (default: minis-mcp-cli).",
    )
    parser.add_argument(
        "--github-server",
        default=DEFAULT_GITHUB_SERVER,
        help=f"Minis MCP server name for GitHub tools (default: {DEFAULT_GITHUB_SERVER}).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    inspect_p = sub.add_parser(
        "inspect",
        help="Discover and inspect GitHub (get_file_contents, push_files) and Notion (notion-fetch) MCP schemas.",
    )
    inspect_p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON file to write discovered schemas.",
    )

    prep_p = sub.add_parser(
        "prepare",
        help="Fetch GitHub .sync/state.json + sync-config.json, verify remote Notion pages, and export push_files payload JSON.",
    )
    prep_p.add_argument("--bundle", required=True, type=Path, help="Built Black9 bundle directory.")
    prep_p.add_argument(
        "--sync-state",
        required=True,
        type=Path,
        help="Sync state JSON file produced by `sync.py init`.",
    )
    prep_p.add_argument("--vault", required=True, type=Path, help="Local Obsidian vault directory.")
    prep_p.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Output directory for prepared payload JSON, merged previews, and metadata backups.",
    )
    prep_p.add_argument("--owner", default=DEFAULT_OWNER, help=f"GitHub owner (default: {DEFAULT_OWNER}).")
    prep_p.add_argument("--repo", default=DEFAULT_REPO, help=f"GitHub repo (default: {DEFAULT_REPO}).")
    prep_p.add_argument("--branch", default=DEFAULT_BRANCH, help=f"GitHub branch (default: {DEFAULT_BRANCH}).")
    prep_p.add_argument(
        "--state-repo-path",
        default=DEFAULT_STATE_REPO_PATH,
        help=f"Sync state path in repo (default: {DEFAULT_STATE_REPO_PATH}).",
    )
    prep_p.add_argument(
        "--config-repo-path",
        default=DEFAULT_CONFIG_REPO_PATH,
        help=f"Sync config path in repo (default: {DEFAULT_CONFIG_REPO_PATH}).",
    )
    prep_p.add_argument(
        "--folder-relpath",
        default=None,
        help="Optional relative POSIX path inside vault/repo for the Black9 folder.",
    )
    prep_p.add_argument(
        "--commit-message",
        default=DEFAULT_COMMIT_MESSAGE,
        help="Commit message embedded in exported push_files payload JSON.",
    )

    inst_p = sub.add_parser(
        "install",
        help="Copy ONLY new Black9 folder files into the actual vault (rejects non-identical overwrites; preserves existing sync metadata).",
    )
    inst_p.add_argument("--bundle", required=True, type=Path, help="Built Black9 bundle directory.")
    inst_p.add_argument("--vault", required=True, type=Path, help="Actual Obsidian vault directory.")
    inst_p.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional directory to write read-only backups of existing local sync metadata.",
    )
    inst_p.add_argument(
        "--folder-relpath",
        default=None,
        help="Optional relative POSIX path inside vault for the Black9 folder.",
    )
    inst_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Run safety checks and print planned copy actions without writing anything.",
    )
    inst_p.add_argument(
        "--install-metadata-from",
        type=Path,
        default=None,
        help="Optional explicit prepared out directory to install merged_state.json & merged_sync-config.json (OFF by default; requires --out backup dir).",
    )
    inst_p.add_argument(
        "--state-repo-path",
        default=DEFAULT_STATE_REPO_PATH,
        help=f"Local sync state relative path inside vault (default: {DEFAULT_STATE_REPO_PATH}).",
    )
    inst_p.add_argument(
        "--config-repo-path",
        default=DEFAULT_CONFIG_REPO_PATH,
        help=f"Local sync config relative path inside vault (default: {DEFAULT_CONFIG_REPO_PATH}).",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    gh_client = MinisMcpGitHubClient(cli_bin=args.cli_bin, server_name=args.github_server)
    notion_client = sync.MinisMcpNotionClient(cli_bin=args.cli_bin)

    try:
        if args.command == "inspect":
            report: dict[str, Any] = {
                "status": "ok",
                "github": gh_client.inspect_relevant_schemas(),
                "notion": notion_client.inspect_relevant_schemas(),
            }
            if args.output is not None:
                if args.output.is_symlink():
                    raise consolidate.PathSafetyError(f"Refusing symlink output: {args.output}")
                data = (
                    json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
                ).encode("utf-8")
                consolidate.atomic_write_bytes(args.output, data)
        elif args.command == "prepare":
            report = run_prepare(
                bundle_dir=args.bundle,
                sync_state_path=args.sync_state,
                vault_dir=args.vault,
                out_dir=args.out,
                github_client=gh_client,
                notion_client=notion_client,
                owner=args.owner,
                repo=args.repo,
                branch=args.branch,
                state_repo_path=args.state_repo_path,
                config_repo_path=args.config_repo_path,
                folder_relpath=args.folder_relpath,
                commit_message=args.commit_message,
            )
        elif args.command == "install":
            report = run_install(
                bundle_dir=args.bundle,
                vault_dir=args.vault,
                out_dir=args.out,
                folder_relpath=args.folder_relpath,
                dry_run=args.dry_run,
                install_metadata_from=args.install_metadata_from,
                state_repo_path=args.state_repo_path,
                config_repo_path=args.config_repo_path,
            )
        else:
            parser.error(f"Unknown command: {args.command}")
            return 2
    except (DeployError, sync.SyncError, consolidate.ConsolidateError) as exc:
        err_obj = {"status": "error", "error": consolidate.redact_secrets(str(exc))}
        print(json.dumps(err_obj, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
