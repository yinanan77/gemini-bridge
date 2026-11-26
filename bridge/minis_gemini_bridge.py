#!/usr/bin/env python3
"""Fail-closed Minis GPT -> official Gemini Pro web agent -> GitHub -> GPT bridge CLI.

Uses Python 3 standard library only and invokes existing authenticated Minis CLIs
(`minis-browser-use`, `minis-mcp-cli`) via explicit argv lists (never shell=True).
Each command performs a bounded action with no polling loops or sleeps.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlparse


FIXED_REPO = "yinanan77/gemini-bridge"
REPO_OWNER = "yinanan77"
REPO_NAME = "gemini-bridge"
DEFAULT_MCP_SERVER = "GitHub"
DEFAULT_MCP_BIN = "minis-mcp-cli"
DEFAULT_BROWSER_BIN = "minis-browser-use"
DEFAULT_STATE_DIR = ".bridge-state"
DEFAULT_TIMEOUT_SECONDS = 30
MIN_TIMEOUT_SECONDS = 1
MAX_TIMEOUT_SECONDS = 120

OFFICIAL_HOST = "antigravity.google.com"
OFFICIAL_REMOTE_BASE = "https://antigravity.google.com/remote"
OFFICIAL_PROJECT_ID = "561adc82-06e6-4c8c-862f-3ac493b947c1"
DEFAULT_PROJECT_URL = f"{OFFICIAL_REMOTE_BASE}?project={OFFICIAL_PROJECT_ID}"
DEFAULT_EXPECTED_MODEL = "Gemini 4 Neon High"

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
TAB_ID_RE = re.compile(r"^[0-9]+$")
TAB_SUFFIX_RE = re.compile(r"\r?\n[ \t]*tab_id:[ \t]*([0-9]+)[ \t]*$")
LIST_TAB_LINE_RE = re.compile(r"^\s*Tab\s+([0-9]+)\s*:\s*(.*?)\s+(?:—|-|\|)\s+(https?://\S+?)(?:\s+\*)?\s*$")
NEW_TAB_LINE_RE = re.compile(r"^\s*Opened\s+new\s+tab\s+([0-9]+)\s+at\s+(https?://\S+)\s*$", re.IGNORECASE)
OFFICIAL_URL_IN_TEXT_RE = re.compile(r"https://antigravity\.google\.com/remote[^\s\"'<>]*")

RESERVED_TASK_IDS = {"main", "master", "head"}
FORBIDDEN_PATH_SEGMENTS = {
    ".git",
    ".github",
    ".gitlab",
    ".gitmodules",
    ".ssh",
    ".aws",
    ".gnupg",
    "workflow",
    "workflows",
    "secret",
    "secrets",
}
FORBIDDEN_FILENAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    ".netrc",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
}
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
ALLOWED_CONTRACT_KEYS = {
    "schema_version",
    "repository",
    "task_id",
    "attempt",
    "goal",
    "base_branch",
    "job_branch",
    "branch",
    "allowed_write_paths",
    "acceptance",
    "notes",
    "context_files",
}
ALLOWED_RESULT_STATUSES = {"completed", "blocked"}

REDACT_PATTERNS = [
    (re.compile(r"https?://[^/\s:@]+:[^/\s@]+@"), "https://[REDACTED]@"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{8,}\b"), "[REDACTED_GH_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{8,}\b"), "[REDACTED_GH_PAT]"),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]{8,}\b"), "[REDACTED_OAUTH_TOKEN]"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"(?i)\bAuthorization\s*:\s*[^\r\n]+"), "Authorization: [REDACTED]"),
    (re.compile(r"(?i)\b(?:Set-)?Cookie\s*:\s*[^\r\n]+"), "Cookie: [REDACTED]"),
]

AUTH_BLOCK_PATTERNS = [
    re.compile(
        r"(?i)\b(?:verify\s+your\s+account(?:\s+to\s+continue)?|verify\s+you\s+are\s+human|"
        r"sign\s*in(?:\s+to\s+continue|\s+required)?|log\s*in(?:\s+to\s+continue|\s+required)?|"
        r"captcha|recaptcha|hcaptcha|turnstile|two-factor|2fa|"
        r"session\s+expired|re-authenticate|authentication\s+required)\b"
    ),
]
QUOTA_BLOCK_PATTERNS = [
    re.compile(r"(?i)\b(?:quota\s+exceeded|rate\s+limit|usage\s+limit|out\s+of\s+credits|add\s+credits|buy\s+credits|upgrade\s+plan|billing\s+required|payment\s+required|pro\s+subscription\s+unavailable|limit\s+reached)\b"),
]
PERMISSION_BLOCK_PATTERNS = [
    re.compile(r"(?i)\b(?:permission\s+denied|access\s+denied|forbidden|not\s+authorized|unauthorized|repository\s+not\s+found|cannot\s+access\s+repository)\b"),
]
ERROR_BLOCK_PATTERNS = [
    re.compile(r"(?i)\b(?:something\s+went\s+wrong|failed\s+to\s+send|unable\s+to\s+complete|internal\s+error|network\s+error|request\s+failed|error\s+occurred|timed\s+out|unexpected\s+error)\b"),
]


class BridgeError(Exception):
    """Base error with a machine-readable code and sanitized message."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(sanitize_text(message, max_len=600))
        self.code = code
        self.message = sanitize_text(message, max_len=600)
        self.details = details or {}


class ValidationError(BridgeError):
    """Raised when contract, path, URL, or result validation fails."""


class McpError(BridgeError):
    """Raised when a GitHub MCP call fails or returns malformed output."""


class McpNotFoundError(McpError):
    """Raised when a GitHub file or branch does not exist."""


class BrowserError(BridgeError):
    """Raised when a browser CLI call fails or returns malformed output."""


class BrowserTimeoutError(BrowserError):
    """Raised when a browser CLI call times out."""


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sanitize_text(value: Any, max_len: int = 1000) -> str:
    text = "" if value is None else str(value)
    for pattern, repl in REDACT_PATTERNS:
        text = pattern.sub(repl, text)
    text = text.replace("\x00", "")
    if len(text) > max_len:
        return text[: max_len - 14] + "...[truncated]"
    return text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def validate_timeout(timeout: int) -> int:
    if isinstance(timeout, bool) or not isinstance(timeout, int):
        raise ValidationError("invalid_timeout", "Timeout must be an integer.")
    if timeout < MIN_TIMEOUT_SECONDS or timeout > MAX_TIMEOUT_SECONDS:
        raise ValidationError(
            "invalid_timeout",
            f"Timeout must be between {MIN_TIMEOUT_SECONDS} and {MAX_TIMEOUT_SECONDS} seconds.",
        )
    return timeout


def validate_official_url(url: str, label: str = "URL") -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("invalid_url", f"{label} must be a non-empty string.")
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme != "https" or parsed.hostname != OFFICIAL_HOST:
        raise ValidationError(
            "non_official_url",
            f"{label} must use https://{OFFICIAL_HOST}/remote.",
        )
    if parsed.username or parsed.password:
        raise ValidationError("credential_url", f"{label} must not contain credentials.")
    if not (parsed.path == "/remote" or parsed.path.startswith("/remote/")):
        raise ValidationError(
            "non_official_url",
            f"{label} path must stay under /remote.",
        )
    return cleaned


def validate_tab_id(tab_id: Any) -> str:
    if isinstance(tab_id, bool):
        raise ValidationError("invalid_tab_id", "tab_id must be a numeric tab identifier, not boolean.")
    if isinstance(tab_id, int):
        if tab_id < 0:
            raise ValidationError("invalid_tab_id", "tab_id must be a non-negative integer.")
        return str(tab_id)
    if isinstance(tab_id, str):
        cleaned = tab_id.strip()
        if TAB_ID_RE.match(cleaned):
            return cleaned
    raise ValidationError("invalid_tab_id", f"tab_id must be a strict numeric identifier (got {tab_id!r}).")


def extract_url_project_id(url: str) -> Optional[str]:
    if not isinstance(url, str) or not url.strip():
        return None
    parsed = urlparse(url.strip())
    vals = parse_qs(parsed.query).get("project")
    if vals and vals[0].strip():
        return vals[0].strip()
    return None


def validate_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise ValidationError(
            "invalid_task_id",
            "task_id must be a safe ASCII identifier matching ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$.",
        )
    if task_id.lower() in RESERVED_TASK_IDS:
        raise ValidationError("reserved_task_id", f"task_id '{task_id}' is reserved.")
    return task_id


def validate_attempt(attempt: Any) -> int:
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1 or attempt > 9999:
        raise ValidationError(
            "invalid_attempt",
            "attempt must be a positive integer (1..9999) and must not be a boolean.",
        )
    return attempt


def validate_branch_name(branch: Any, field_name: str = "base_branch") -> str:
    if not isinstance(branch, str) or not BRANCH_RE.match(branch):
        raise ValidationError(
            f"invalid_{field_name}",
            f"{field_name} must be a safe ASCII git branch name.",
        )
    if (
        ".." in branch
        or "//" in branch
        or "/." in branch
        or "@{" in branch
        or branch.endswith("/")
        or branch.endswith(".")
        or branch.endswith(".lock")
    ):
        raise ValidationError(
            f"invalid_{field_name}",
            f"{field_name} contains forbidden git ref syntax.",
        )
    return branch


def validate_relative_path(raw_path: Any, *, allow_jobs_prefix: bool = False) -> str:
    if not isinstance(raw_path, str):
        raise ValidationError("invalid_path", "Path must be a string.")
    if not raw_path or raw_path != raw_path.strip():
        raise ValidationError("invalid_path", "Path must be non-empty without leading/trailing whitespace.")
    if "\x00" in raw_path or "\\" in raw_path:
        raise ValidationError("invalid_path", "Path must use forward slashes without NUL bytes.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_path):
        raise ValidationError("invalid_path", "Path contains control characters.")
    if raw_path.startswith("/") or raw_path.startswith("~") or re.match(r"^[A-Za-z]:", raw_path):
        raise ValidationError("absolute_path", f"Absolute or home path rejected: {raw_path}")
    if raw_path.endswith("/"):
        raise ValidationError("directory_path", f"Path must be an explicit file, not a directory: {raw_path}")
    if any(ch in raw_path for ch in ("*", "?", "[", "]")):
        raise ValidationError("glob_path", f"Wildcard/glob paths are not allowed: {raw_path}")

    segments = raw_path.split("/")
    for seg in segments:
        if not seg or seg in (".", ".."):
            raise ValidationError("traversal_path", f"Empty or traversal path segment rejected: {raw_path}")
        if seg != seg.strip():
            raise ValidationError("invalid_path", f"Path segment has surrounding whitespace: {raw_path}")
        low = seg.lower()
        if low in FORBIDDEN_PATH_SEGMENTS:
            raise ValidationError("forbidden_path", f"Forbidden path segment '{seg}' in: {raw_path}")
        if "secret" in low:
            raise ValidationError("forbidden_secret_path", f"Secret-like path rejected: {raw_path}")

    filename_low = segments[-1].lower()
    if filename_low in FORBIDDEN_FILENAMES or filename_low.startswith(".env."):
        raise ValidationError("forbidden_secret_path", f"Sensitive filename rejected: {raw_path}")
    if filename_low.endswith(FORBIDDEN_SUFFIXES):
        raise ValidationError("forbidden_secret_path", f"Sensitive key/certificate suffix rejected: {raw_path}")
    if re.match(r"^workflow.*\.(?:ya?ml)$", filename_low):
        raise ValidationError("forbidden_workflow_path", f"Workflow manifest rejected: {raw_path}")
    if not allow_jobs_prefix and segments[0] == "jobs":
        raise ValidationError(
            "reserved_snapshot_path",
            f"Path under jobs/ is reserved for bridge snapshots: {raw_path}",
        )
    return raw_path


def derive_job_branch(task_id: str) -> str:
    branch = f"gpt-gemini/jobs/{task_id}"
    if branch == "main" or not branch.startswith("gpt-gemini/jobs/"):
        raise ValidationError("invalid_job_branch", "Derived job branch is invalid.")
    return branch


def derive_snapshot_paths(task_id: str, attempt: int) -> Tuple[str, str]:
    attempt_dir = f"jobs/{task_id}/attempt-{attempt:02d}"
    return f"{attempt_dir}/task.json", f"{attempt_dir}/result.json"


def validate_contract_dict(raw: Any) -> Dict[str, Any]:
    """Validate contract dictionary and return normalized contract metadata + canonical hash."""
    if not isinstance(raw, dict):
        raise ValidationError("malformed_contract", "Contract must be a JSON object.")

    unknown_keys = sorted(set(raw.keys()) - ALLOWED_CONTRACT_KEYS)
    if unknown_keys:
        raise ValidationError(
            "unknown_contract_fields",
            f"Unsupported contract field(s): {', '.join(unknown_keys)}",
        )

    required = ("task_id", "attempt", "goal", "base_branch", "allowed_write_paths", "acceptance")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValidationError("missing_contract_fields", f"Missing required field(s): {', '.join(missing)}")

    repo = raw.get("repository", FIXED_REPO)
    if not isinstance(repo, str) or repo != FIXED_REPO:
        raise ValidationError(
            "unsupported_repository",
            f"Only repository '{FIXED_REPO}' is supported.",
        )

    task_id = validate_task_id(raw["task_id"])
    attempt = validate_attempt(raw["attempt"])

    goal = raw["goal"]
    if not isinstance(goal, str) or not goal.strip() or "\x00" in goal:
        raise ValidationError("invalid_goal", "goal must be a non-empty string without NUL bytes.")
    if len(goal) > 16000:
        raise ValidationError("invalid_goal", "goal exceeds maximum length of 16000 characters.")

    base_branch = validate_branch_name(raw["base_branch"], "base_branch")
    job_branch = derive_job_branch(task_id)
    if base_branch == job_branch:
        raise ValidationError("invalid_base_branch", "base_branch must not equal the derived job branch.")

    for branch_field in ("job_branch", "branch"):
        if branch_field in raw:
            provided = raw[branch_field]
            if not isinstance(provided, str) or provided == "main" or provided == "master":
                raise ValidationError("forbidden_job_branch", f"{branch_field} cannot target main/master.")
            if provided != job_branch:
                raise ValidationError(
                    "mismatched_job_branch",
                    f"{branch_field} must match derived job branch '{job_branch}'.",
                )

    raw_paths = raw["allowed_write_paths"]
    if not isinstance(raw_paths, list) or not raw_paths:
        raise ValidationError("invalid_allowed_write_paths", "allowed_write_paths must be a non-empty list.")
    seen_paths = set()
    allowed_write_paths: List[str] = []
    for item in raw_paths:
        cleaned = validate_relative_path(item, allow_jobs_prefix=False)
        if cleaned in seen_paths:
            raise ValidationError("duplicate_path", f"Duplicate allowed_write_paths entry: {cleaned}")
        seen_paths.add(cleaned)
        allowed_write_paths.append(cleaned)

    raw_acceptance = raw["acceptance"]
    if not isinstance(raw_acceptance, list) or not raw_acceptance:
        raise ValidationError("invalid_acceptance", "acceptance must be a non-empty list of strings.")
    acceptance: List[str] = []
    for idx, item in enumerate(raw_acceptance):
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValidationError("invalid_acceptance", f"acceptance[{idx}] must be a non-empty string.")
        acceptance.append(item)

    canonical: Dict[str, Any] = {
        "schema_version": 1,
        "repository": FIXED_REPO,
        "task_id": task_id,
        "attempt": attempt,
        "goal": goal,
        "base_branch": base_branch,
        "job_branch": job_branch,
        "allowed_write_paths": sorted(allowed_write_paths),
        "acceptance": acceptance,
    }

    if "notes" in raw:
        notes = raw["notes"]
        if not isinstance(notes, str) or "\x00" in notes or len(notes) > 8000:
            raise ValidationError("invalid_notes", "notes must be a string up to 8000 chars without NUL bytes.")
        canonical["notes"] = notes

    if "context_files" in raw:
        ctx = raw["context_files"]
        if not isinstance(ctx, list):
            raise ValidationError("invalid_context_files", "context_files must be a list of relative paths.")
        ctx_seen = set()
        ctx_paths: List[str] = []
        for item in ctx:
            cleaned = validate_relative_path(item, allow_jobs_prefix=True)
            if cleaned in ctx_seen:
                raise ValidationError("duplicate_context_path", f"Duplicate context_files entry: {cleaned}")
            ctx_seen.add(cleaned)
            ctx_paths.append(cleaned)
        canonical["context_files"] = sorted(ctx_paths)

    if "schema_version" in raw:
        sv = raw["schema_version"]
        if isinstance(sv, bool) or sv != 1:
            raise ValidationError("invalid_schema_version", "schema_version must be integer 1.")

    canonical_json = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    contract_sha256 = sha256_text(canonical_json)
    task_snapshot_path, result_snapshot_path = derive_snapshot_paths(task_id, attempt)
    task_marker = f"GPT-GEMINI-TASK:{task_id}:attempt-{attempt:02d}:{contract_sha256[:12]}"

    return {
        "contract": canonical,
        "canonical_json": canonical_json,
        "contract_sha256": contract_sha256,
        "repository": FIXED_REPO,
        "task_id": task_id,
        "attempt": attempt,
        "base_branch": base_branch,
        "job_branch": job_branch,
        "task_snapshot_path": task_snapshot_path,
        "result_snapshot_path": result_snapshot_path,
        "allowed_write_paths": sorted(allowed_write_paths),
        "effective_allowed_paths": sorted(
            [task_snapshot_path, result_snapshot_path] + allowed_write_paths
        ),
        "task_marker": task_marker,
    }


def load_and_validate_contract(contract_path: str) -> Dict[str, Any]:
    path = Path(contract_path)
    if not path.is_file():
        raise ValidationError("contract_not_found", f"Contract file not found: {contract_path}")
    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError("contract_read_error", f"Failed to read contract: {exc}") from exc
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValidationError("malformed_json", f"Invalid JSON in contract: {exc}") from exc
    return validate_contract_dict(parsed)


def build_snapshot_document(validated: Dict[str, Any]) -> str:
    """Serialize immutable task snapshot written to GitHub."""
    doc = dict(validated["contract"])
    doc["contract_sha256"] = validated["contract_sha256"]
    doc["task_snapshot_path"] = validated["task_snapshot_path"]
    doc["result_snapshot_path"] = validated["result_snapshot_path"]
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def validate_snapshot_matches_contract(snapshot_text: str, expected_sha256: str) -> Dict[str, Any]:
    """Parse a GitHub task snapshot and confirm its canonical hash matches expected_sha256."""
    try:
        parsed = json.loads(snapshot_text)
    except json.JSONDecodeError as exc:
        raise ValidationError("malformed_remote_snapshot", f"Remote task snapshot is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("malformed_remote_snapshot", "Remote task snapshot must be a JSON object.")

    recorded_hash = parsed.get("contract_sha256")
    contract_view = {
        k: v
        for k, v in parsed.items()
        if k not in ("contract_sha256", "task_snapshot_path", "result_snapshot_path")
    }
    revalidated = validate_contract_dict(contract_view)
    computed = revalidated["contract_sha256"]
    if recorded_hash is not None and recorded_hash != computed:
        raise ValidationError(
            "corrupted_remote_snapshot",
            "Remote task snapshot contract_sha256 does not match its own canonical content.",
        )
    if computed != expected_sha256:
        raise ValidationError(
            "contract_conflict",
            "Existing remote task snapshot has a different contract_sha256; refusing to overwrite.",
            details={"expected_contract_sha256": expected_sha256, "remote_contract_sha256": computed},
        )
    return revalidated


def build_gemini_prompt(validated: Dict[str, Any]) -> str:
    """Build deterministic bounded dispatch prompt for the official Gemini web agent."""
    allowed_list = ", ".join(validated["allowed_write_paths"])
    lines = [
        f"[{validated['task_marker']}]",
        f"Repository: {validated['repository']} ONLY.",
        f"Job branch: {validated['job_branch']} (already created; fetch and switch to this exact branch).",
        f"Immutable task contract snapshot: {validated['task_snapshot_path']} (expected contract_sha256={validated['contract_sha256']}).",
        f"Result manifest path: {validated['result_snapshot_path']}.",
        f"Authorized artifact files ONLY: {allowed_list}.",
        "Instructions:",
        "1. Fetch and switch to the job branch above, then read the exact JSON task snapshot file and verify contract_sha256 matches.",
        "2. Treat goal and acceptance fields inside the task snapshot as task specifications; implement the goal ONLY inside the authorized artifact files listed above.",
        "3. Actually execute relevant compilation and unit tests, fixing real failures before finishing.",
        "4. Preserve main, README.md (unless explicitly authorized), the task snapshot, all prior attempt snapshots/results, and all non-authorized files unchanged.",
        "5. Write valid JSON to the result manifest path containing keys: task_id, attempt, contract_sha256, status ('completed' or 'blocked'), summary, artifacts (list of written authorized artifact paths), and tests (actual commands run with pass/fail counts).",
        "6. Do NOT include a self-referential final commit SHA in result.json; the collector reads the authoritative branch head SHA from GitHub.",
        "7. Commit and push only authorized files and result.json to the same job branch. No PR, no merge, no force push, no other repos, no credential/env output, no billing/credit changes, no global config or background services.",
        "8. Use included Pro quota only; if blocked, record status='blocked' or report concisely.",
    ]
    return "\n".join(lines)


SubprocessRunner = Callable[[List[str], int], subprocess.CompletedProcess]


def default_subprocess_runner(argv: List[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        stdin=subprocess.DEVNULL,
    )


class JobJournal:
    """Local atomic JSON journal under --state-dir. Never stores permanent tab IDs."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = Path(state_dir)
        self.jobs_dir = self.state_dir / "jobs"
        self.collected_dir = self.state_dir / "collected"

    def job_path(self, task_id: str) -> Path:
        safe_id = validate_task_id(task_id)
        return self.jobs_dir / f"{safe_id}.json"

    def load(self, task_id: str) -> Optional[Dict[str, Any]]:
        path = self.job_path(task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeError("journal_corrupt", f"Failed to read journal for {task_id}: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("attempts"), dict):
            raise BridgeError("journal_corrupt", f"Malformed journal structure for {task_id}.")
        return data

    def save(self, task_id: str, data: Dict[str, Any]) -> Path:
        sanitized = self._strip_forbidden_fields(data)
        sanitized["updated_at"] = utc_now_iso()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        path = self.job_path(task_id)
        tmp_path = path.with_name(f"{path.name}.tmp.{os.getpid()}")
        payload = json.dumps(sanitized, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, path)
        return path

    @classmethod
    def _strip_forbidden_fields(cls, obj: Any) -> Any:
        if isinstance(obj, dict):
            cleaned: Dict[str, Any] = {}
            for key, val in obj.items():
                if key in ("tab_id", "tabId"):
                    continue
                cleaned[key] = cls._strip_forbidden_fields(val)
            return cleaned
        if isinstance(obj, list):
            return [cls._strip_forbidden_fields(item) for item in obj]
        return obj

    def ensure_job(self, validated: Dict[str, Any]) -> Dict[str, Any]:
        existing = self.load(validated["task_id"])
        if existing is not None:
            return existing
        now = utc_now_iso()
        return {
            "schema_version": 1,
            "repository": validated["repository"],
            "task_id": validated["task_id"],
            "job_branch": validated["job_branch"],
            "created_at": now,
            "updated_at": now,
            "attempts": {},
        }

    def get_attempt(self, job: Dict[str, Any], attempt: int) -> Optional[Dict[str, Any]]:
        return job.get("attempts", {}).get(str(attempt))

    def ensure_attempt(self, job: Dict[str, Any], validated: Dict[str, Any]) -> Dict[str, Any]:
        key = str(validated["attempt"])
        attempts = job.setdefault("attempts", {})
        if key in attempts:
            entry = attempts[key]
            if entry.get("contract_sha256") != validated["contract_sha256"]:
                raise ValidationError(
                    "journal_contract_conflict",
                    f"Journal attempt {key} already exists with a different contract_sha256.",
                )
            return entry
        entry = {
            "attempt": validated["attempt"],
            "contract_sha256": validated["contract_sha256"],
            "task_marker": validated["task_marker"],
            "task_snapshot_path": validated["task_snapshot_path"],
            "result_snapshot_path": validated["result_snapshot_path"],
            "state": "created",
            "send_initiated": False,
            "branch_created": False,
            "task_snapshot_written": False,
            "conversation_url": None,
            "status_history": [],
            "collection_history": [],
            "errors": [],
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        attempts[key] = entry
        return entry

    def record_error(self, attempt_entry: Dict[str, Any], stage: str, message: str) -> None:
        errors = attempt_entry.setdefault("errors", [])
        errors.append(
            {
                "timestamp": utc_now_iso(),
                "stage": stage,
                "error": sanitize_text(message, max_len=500),
            }
        )
        if len(errors) > 50:
            del errors[:-50]
        attempt_entry["updated_at"] = utc_now_iso()


def unwrap_mcp_envelope(raw_stdout: str) -> Dict[str, Any]:
    """Parse minis-mcp-cli JSON output and normalize MCP text/resource envelopes."""
    if not raw_stdout or not raw_stdout.strip():
        raise McpError("mcp_empty_output", "GitHub MCP returned empty output.")
    try:
        parsed = json.loads(raw_stdout)
    except json.JSONDecodeError as exc:
        raise McpError("mcp_malformed_json", f"GitHub MCP returned non-JSON output: {exc}") from exc
    if not isinstance(parsed, dict):
        return {"raw": parsed, "texts": [], "resources": []}

    if parsed.get("ok") is False or parsed.get("isError") is True:
        err_msg = _extract_mcp_error_message(parsed)
        _raise_classified_mcp_error(err_msg)

    inner = parsed
    for wrapper_key in ("data", "result"):
        if isinstance(inner.get(wrapper_key), dict):
            inner = inner[wrapper_key]
            if inner.get("ok") is False or inner.get("isError") is True:
                err_msg = _extract_mcp_error_message(inner)
                _raise_classified_mcp_error(err_msg)

    if isinstance(inner.get("error"), (str, dict)):
        err_msg = _extract_mcp_error_message(inner)
        _raise_classified_mcp_error(err_msg)

    texts: List[str] = []
    resources: List[Dict[str, Any]] = []
    content = inner.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text" and isinstance(item.get("text"), str):
                texts.append(item["text"])
            elif item_type == "resource" and isinstance(item.get("resource"), dict):
                res = item["resource"]
                entry: Dict[str, Any] = {
                    "uri": res.get("uri"),
                    "mimeType": res.get("mimeType"),
                }
                if isinstance(res.get("text"), str):
                    entry["text"] = res["text"]
                elif isinstance(res.get("blob"), str):
                    try:
                        decoded = base64.b64decode(res["blob"], validate=True)
                    except Exception as exc:
                        raise McpError("mcp_invalid_blob", f"Invalid base64 blob in MCP resource: {exc}") from exc
                    try:
                        entry["text"] = decoded.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise McpError("mcp_non_utf8_blob", f"MCP resource blob is not valid UTF-8: {exc}") from exc
                    entry["raw_bytes_len"] = len(decoded)
                else:
                    raise McpError("mcp_malformed_resource", "MCP resource lacks both text and blob fields.")
                resources.append(entry)
    elif isinstance(inner.get("text"), str):
        texts.append(inner["text"])

    return {"raw": inner, "texts": texts, "resources": resources}


def _extract_mcp_error_message(obj: Dict[str, Any]) -> str:
    if isinstance(obj.get("error"), str):
        return obj["error"]
    if isinstance(obj.get("error"), dict):
        return str(obj["error"].get("message") or obj["error"])
    if isinstance(obj.get("message"), str):
        return obj["message"]
    content = obj.get("content")
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return " | ".join(parts)
    return "Unknown GitHub MCP error"


def _raise_classified_mcp_error(message: str) -> None:
    low = message.lower()
    if "404" in low or "not found" in low or "no commit found" in low or "branch not found" in low:
        raise McpNotFoundError("mcp_not_found", message)
    raise McpError("mcp_tool_error", message)


class GitHubMcpClient:
    """Bounded subprocess wrapper around `minis-mcp-cli` for the fixed GitHub repo."""

    def __init__(
        self,
        mcp_bin: str = DEFAULT_MCP_BIN,
        server_name: str = DEFAULT_MCP_SERVER,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        runner: SubprocessRunner = default_subprocess_runner,
    ) -> None:
        self.mcp_bin = mcp_bin
        self.server_name = server_name
        self.timeout = validate_timeout(timeout)
        self.runner = runner

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        argv = [
            self.mcp_bin,
            "call",
            self.server_name,
            tool_name,
            json.dumps(arguments, separators=(",", ":"), ensure_ascii=False),
        ]
        try:
            proc = self.runner(argv, self.timeout)
        except FileNotFoundError as exc:
            raise McpError("mcp_binary_missing", f"MCP executable not found: {self.mcp_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise McpError("mcp_timeout", f"GitHub MCP tool '{tool_name}' timed out after {self.timeout}s.") from exc
        except OSError as exc:
            raise McpError("mcp_exec_error", f"Failed to execute MCP CLI: {exc}") from exc

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            _raise_classified_mcp_error(f"MCP tool '{tool_name}' failed: {err}")
        return unwrap_mcp_envelope(proc.stdout)

    def get_branch_head_sha(self, branch: str) -> Optional[str]:
        """Return authoritative 40-hex commit SHA for branch, or None if branch does not exist."""
        try:
            env = self.call_tool(
                "list_commits",
                {"owner": REPO_OWNER, "repo": REPO_NAME, "sha": branch, "perPage": 1},
            )
        except McpNotFoundError:
            return None

        payload = self._extract_json_payload(env)
        if isinstance(payload, list):
            if not payload:
                return None
            first = payload[0]
            sha = first.get("sha") if isinstance(first, dict) else None
        elif isinstance(payload, dict):
            if isinstance(payload.get("commits"), list) and payload["commits"]:
                first = payload["commits"][0]
                sha = first.get("sha") if isinstance(first, dict) else None
            else:
                sha = payload.get("sha")
        else:
            sha = None

        if not isinstance(sha, str) or not SHA40_RE.match(sha):
            raise McpError("mcp_invalid_sha", f"GitHub MCP did not return a valid 40-hex commit SHA for '{branch}'.")
        return sha

    def create_branch(self, branch: str, from_branch: str) -> Dict[str, Any]:
        if branch == "main" or not branch.startswith("gpt-gemini/jobs/"):
            raise ValidationError("forbidden_branch_mutation", f"Refusing to create non-job branch: {branch}")
        env = self.call_tool(
            "create_branch",
            {"owner": REPO_OWNER, "repo": REPO_NAME, "branch": branch, "from_branch": from_branch},
        )
        return env

    def create_file_immutable(self, branch: str, path: str, content: str, message: str) -> Dict[str, Any]:
        """Create a new file on the job branch (no sha parameter, so existing files are not overwritten)."""
        if branch == "main" or not branch.startswith("gpt-gemini/jobs/"):
            raise ValidationError("forbidden_branch_mutation", f"Refusing to write to non-job branch: {branch}")
        return self.call_tool(
            "create_or_update_file",
            {
                "owner": REPO_OWNER,
                "repo": REPO_NAME,
                "path": path,
                "content": content,
                "message": message,
                "branch": branch,
            },
        )

    def get_file_text(self, path: str, ref: str) -> Optional[str]:
        """Fetch UTF-8 text of a file at ref (branch or commit SHA). Returns None if 404."""
        try:
            env = self.call_tool(
                "get_file_contents",
                {"owner": REPO_OWNER, "repo": REPO_NAME, "path": path, "ref": ref},
            )
        except McpNotFoundError:
            return None
        except McpError as exc:
            # Compatibility fallback for MCP servers expecting 'branch' instead of 'ref'
            if "branch" in exc.message.lower() and "ref" in exc.message.lower():
                try:
                    env = self.call_tool(
                        "get_file_contents",
                        {"owner": REPO_OWNER, "repo": REPO_NAME, "path": path, "branch": ref},
                    )
                except McpNotFoundError:
                    return None
            else:
                raise

        return extract_file_text_from_mcp_envelope(env, expected_path=path)

    @staticmethod
    def _extract_json_payload(env: Dict[str, Any]) -> Any:
        for res in env.get("resources", []):
            if isinstance(res.get("text"), str):
                try:
                    return json.loads(res["text"])
                except json.JSONDecodeError:
                    pass
        for txt in env.get("texts", []):
            try:
                return json.loads(txt)
            except json.JSONDecodeError:
                pass
        raw = env.get("raw")
        if isinstance(raw, (list, dict)) and "content" not in raw:
            return raw
        raise McpError("mcp_malformed_payload", "Expected JSON payload in GitHub MCP response.")


def extract_file_text_from_mcp_envelope(env: Dict[str, Any], expected_path: str) -> str:
    """Extract real file text from either MCP resource or text/GitHub Contents API envelopes."""
    resources = env.get("resources", [])
    if resources:
        text = resources[0].get("text")
        if isinstance(text, str):
            return text
        raise McpError("mcp_malformed_resource", f"Missing text in resource envelope for {expected_path}.")

    texts = env.get("texts", [])
    if texts:
        # Prefer a GitHub Contents API JSON wrapper if present
        for candidate in texts:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("type") == "dir":
                raise McpError("mcp_unexpected_directory", f"Expected file, got directory for {expected_path}.")
            if isinstance(parsed, dict) and isinstance(parsed.get("content"), str) and (
                parsed.get("encoding") == "base64" or ("sha" in parsed and "path" in parsed)
            ):
                if parsed.get("encoding") == "base64":
                    raw_b64 = "".join(parsed["content"].split())
                    try:
                        return base64.b64decode(raw_b64, validate=True).decode("utf-8")
                    except Exception as exc:
                        raise McpError(
                            "mcp_invalid_file_base64",
                            f"Failed to decode base64 file content for {expected_path}: {exc}",
                        ) from exc
                return parsed["content"]
        # Otherwise, if text looks like a GitHub error string, fail closed
        joined = texts[-1]
        low = joined.lower()
        if "404" in low and "not found" in low:
            raise McpNotFoundError("mcp_not_found", joined)
        return joined

    raw = env.get("raw")
    if isinstance(raw, dict) and isinstance(raw.get("content"), str):
        if raw.get("encoding") == "base64":
            raw_b64 = "".join(raw["content"].split())
            try:
                return base64.b64decode(raw_b64, validate=True).decode("utf-8")
            except Exception as exc:
                raise McpError("mcp_invalid_file_base64", f"Invalid base64 content: {exc}") from exc
        return raw["content"]

    raise McpError("mcp_missing_file_content", f"No file content found in MCP response for {expected_path}.")


def _strip_tab_id_suffix(text: str) -> Tuple[str, Optional[str]]:
    match = TAB_SUFFIX_RE.search(text)
    if not match:
        return text, None
    return text[: match.start()], match.group(1)


def parse_browser_envelope(raw_stdout: str) -> Dict[str, Any]:
    """Parse minis-browser-use stdout tolerating real JSON envelope, page_url, and trailing tab_id suffix."""
    if not raw_stdout or not raw_stdout.strip():
        raise BrowserError("browser_empty_output", "minis-browser-use returned empty output.")
    stripped = raw_stdout.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError as exc:
        if stripped.startswith("{") or stripped.startswith("["):
            raise BrowserError("browser_malformed_json", f"minis-browser-use returned malformed JSON: {exc}") from exc
        return {"parsed": None, "parsed_is_null": False, "human_text": stripped, "page_url": None, "tab_id": None, "raw": stripped}

    if not isinstance(parsed, dict):
        return {"parsed": parsed, "parsed_is_null": parsed is None, "human_text": None, "page_url": None, "tab_id": None, "raw": parsed}

    if parsed.get("ok") is False:
        err = parsed.get("error") or parsed.get("message") or "Browser action failed."
        raise BrowserError("browser_action_failed", str(err))
    if isinstance(parsed.get("error"), (str, dict)) and parsed["error"]:
        raise BrowserError("browser_action_failed", str(parsed["error"]))

    container = parsed
    for key in ("data", "result"):
        if key in parsed and parsed[key] is not None:
            container = parsed[key]
            break

    if isinstance(container, str):
        body, suffix_tid = _strip_tab_id_suffix(container)
        try:
            inner_json = json.loads(body)
            return {
                "parsed": inner_json,
                "parsed_is_null": inner_json is None,
                "human_text": None,
                "page_url": None,
                "tab_id": suffix_tid,
                "raw": parsed,
            }
        except json.JSONDecodeError as exc:
            if body.lstrip().startswith("{") or body.lstrip().startswith("["):
                raise BrowserError("browser_malformed_json", f"Malformed JSON in browser text: {exc}") from exc
            return {
                "parsed": None,
                "parsed_is_null": False,
                "human_text": body,
                "page_url": None,
                "tab_id": suffix_tid,
                "raw": parsed,
            }

    if isinstance(container, dict):
        if container.get("success") is False:
            err = container.get("error") or container.get("message") or container.get("text") or "Browser data.success is false."
            raise BrowserError("browser_action_failed", str(err))

        page_url = container.get("page_url") if isinstance(container.get("page_url"), str) else None
        if page_url is None and isinstance(container.get("url"), str):
            page_url = container["url"]

        container_tid: Optional[str] = None
        for tk in ("tab_id", "tabId"):
            if container.get(tk) is not None:
                container_tid = validate_tab_id(container[tk])
                break

        if isinstance(container.get("text"), str):
            body, suffix_tid = _strip_tab_id_suffix(container["text"])
            effective_tid = suffix_tid or container_tid
            try:
                inner_json = json.loads(body)
                if inner_json is None:
                    return {
                        "parsed": None,
                        "parsed_is_null": True,
                        "human_text": None,
                        "page_url": page_url,
                        "tab_id": effective_tid,
                        "raw": parsed,
                    }
                if isinstance(inner_json, dict):
                    merged = dict(inner_json)
                    if page_url is not None:
                        if "page_url" not in merged:
                            merged["page_url"] = page_url
                        if "url" not in merged:
                            merged["url"] = page_url
                    return {
                        "parsed": merged,
                        "parsed_is_null": False,
                        "human_text": None,
                        "page_url": page_url,
                        "tab_id": effective_tid,
                        "raw": parsed,
                    }
                return {
                    "parsed": inner_json,
                    "parsed_is_null": False,
                    "human_text": None,
                    "page_url": page_url,
                    "tab_id": effective_tid,
                    "raw": parsed,
                }
            except json.JSONDecodeError as exc:
                if body.lstrip().startswith("{") or body.lstrip().startswith("["):
                    raise BrowserError("browser_malformed_json", f"Malformed JSON in browser data.text: {exc}") from exc
                return {
                    "parsed": {k: v for k, v in container.items() if k != "text"},
                    "parsed_is_null": False,
                    "human_text": body,
                    "page_url": page_url,
                    "tab_id": effective_tid,
                    "raw": parsed,
                }
        return {
            "parsed": container,
            "parsed_is_null": False,
            "human_text": None,
            "page_url": page_url,
            "tab_id": container_tid,
            "raw": parsed,
        }

    return {
        "parsed": container,
        "parsed_is_null": container is None,
        "human_text": None,
        "page_url": None,
        "tab_id": None,
        "raw": parsed,
    }


def parse_tabs_from_envelope(env: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract normalized [{tab_id, url, title}] using strict numeric tab IDs."""
    tabs: List[Dict[str, str]] = []
    parsed = env.get("parsed")
    candidates: List[Any] = []
    if isinstance(parsed, list):
        candidates = parsed
    elif isinstance(parsed, dict):
        for key in ("tabs", "items"):
            if isinstance(parsed.get(key), list):
                candidates = parsed[key]
                break
        if (
            not candidates
            and (parsed.get("tab_id") is not None or parsed.get("tabId") is not None or parsed.get("id") is not None)
            and (isinstance(parsed.get("url"), str) or isinstance(parsed.get("page_url"), str))
        ):
            candidates = [parsed]

    for item in candidates:
        if not isinstance(item, dict):
            raise BrowserError("malformed_tab_output", "Tab list entry must be an object.")
        tid = item.get("tab_id") if item.get("tab_id") is not None else item.get("tabId")
        if tid is None:
            tid = item.get("id")
        url = item.get("url") if isinstance(item.get("url"), str) else item.get("page_url")
        if tid is None or not isinstance(url, str):
            raise BrowserError("malformed_tab_output", "Structured tab entry missing numeric tab_id or url.")
        tabs.append({"tab_id": validate_tab_id(tid), "url": url.strip(), "title": str(item.get("title") or "")})

    if not tabs and isinstance(env.get("human_text"), str):
        seen_ids: Dict[str, str] = {}
        for raw_line in env["human_text"].splitlines():
            line = raw_line.strip()
            if not line or line.lower() == "open tabs:":
                continue
            m_list = LIST_TAB_LINE_RE.match(raw_line)
            if m_list:
                tid = validate_tab_id(m_list.group(1))
                title = m_list.group(2).strip()
                url = m_list.group(3).strip()
                if tid in seen_ids and seen_ids[tid] != url:
                    raise BrowserError("ambiguous_tab_output", f"Conflicting duplicate tab_id {tid} in list_tabs.")
                seen_ids[tid] = url
                tabs.append({"tab_id": tid, "url": url, "title": title})
                continue
            m_new = NEW_TAB_LINE_RE.match(raw_line)
            if m_new:
                tid = validate_tab_id(m_new.group(1))
                url = m_new.group(2).strip()
                seen_ids[tid] = url
                tabs.append({"tab_id": tid, "url": url, "title": ""})
                continue
            if re.match(r"(?i)^(?:tab\b|opened\s+new\s+tab\b)", line):
                raise BrowserError("malformed_tab_output", f"Unrecognized tab line format: {sanitize_text(line, max_len=160)}")
    return tabs


JS_FIND_EDITOR_SNIPPET = r"""
function findEditor() {
  const selectors = [
    '[contenteditable="true"][aria-label="Message input"]',
    '[contenteditable="true"][role="combobox"]',
    '[contenteditable="true"][role="textbox"]',
    'textarea[aria-label="Message input"]:not([disabled])',
    'textarea:not([disabled])'
  ];
  for (const sel of selectors) {
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const el of nodes) {
      const r = el.getBoundingClientRect();
      const style = window.getComputedStyle(el);
      if (r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none') {
        return el;
      }
    }
  }
  return null;
}
"""

JS_FIND_SEND_SNIPPET = r"""
function findSendButton() {
  const selectors = [
    'button[aria-label="Send message"]',
    'button[data-testid="send-button"]'
  ];
  for (const sel of selectors) {
    const nodes = Array.from(document.querySelectorAll(sel));
    for (const btn of nodes) {
      const r = btn.getBoundingClientRect();
      const style = window.getComputedStyle(btn);
      const visible = r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
      if (!visible) continue;
      const label = ((btn.getAttribute('aria-label') || '') + ' ' + (btn.innerText || btn.textContent || '')).trim();
      return { btn, label, visible };
    }
  }
  return null;
}
"""

JS_FOCUS_SELECT_ALL = (
    "return (() => {"
    + JS_FIND_EDITOR_SNIPPET
    + r"""
  const el = findEditor();
  if (!el) return JSON.stringify({ok: false, found: false, reason: 'editor_not_found'});
  el.focus();
  const selected = document.execCommand('selectAll', false, null);
  return JSON.stringify({
    ok: true,
    found: true,
    selected: Boolean(selected),
    tag: el.tagName.toLowerCase(),
    role: el.getAttribute('role') || '',
    content_editable: Boolean(el.isContentEditable)
  });
})();"""
)


def build_paste_js(prompt_text: str) -> str:
    encoded = json.dumps(prompt_text).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    body = r"""
  const text = __PROMPT__;
  const active = document.activeElement;
  const activeRole = active ? (active.getAttribute('role') || '') : '';
  const target = (active && (active.tagName === 'TEXTAREA' || active.isContentEditable || activeRole === 'combobox' || activeRole === 'textbox'))
    ? active
    : findEditor();
  if (!target) return JSON.stringify({ok: false, dispatched: false, reason: 'editor_not_found'});
  const dt = new DataTransfer();
  dt.setData('text/plain', text);
  const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
  target.dispatchEvent(ev);
  return JSON.stringify({ok: true, dispatched: true, expected_length: text.length});
})();"""
    return "return (() => {" + JS_FIND_EDITOR_SNIPPET + body.replace("__PROMPT__", encoded)


def build_inspect_draft_js(task_marker: str) -> str:
    encoded_marker = json.dumps(task_marker)
    body = r"""
  const marker = __MARKER__;
  const editor = findEditor();
  let draftText = '';
  if (editor) {
    draftText = (editor.tagName === 'TEXTAREA' || editor.tagName === 'INPUT')
      ? String(editor.value || '')
      : String(editor.innerText || editor.textContent || '');
  }
  const sendFound = findSendButton();
  const sendButton = sendFound ? {
    found: true,
    visible: sendFound.visible,
    disabled: Boolean(sendFound.btn.disabled),
    aria_disabled: String(sendFound.btn.getAttribute('aria-disabled') || '').toLowerCase() === 'true',
    label: sendFound.label.slice(0, 120)
  } : {found: false, visible: false, disabled: true, aria_disabled: true, label: ''};
  const modelEl = document.querySelector('button[data-testid="model-selector-trigger"], button[aria-label^="Select model"]');
  const modelButton = modelEl ? {
    found: true,
    text: String(modelEl.innerText || modelEl.textContent || '').trim().slice(0, 200),
    aria_label: String(modelEl.getAttribute('aria-label') || '').trim().slice(0, 200)
  } : {found: false, text: '', aria_label: ''};
  const projectEl = document.querySelector('button[aria-label^="Select project, current:"]');
  const projectButton = projectEl ? {
    found: true,
    text: String(projectEl.innerText || projectEl.textContent || '').trim().slice(0, 200),
    aria_label: String(projectEl.getAttribute('aria-label') || '').trim().slice(0, 240)
  } : {found: false, text: '', aria_label: ''};
  const isInsideUserOrEditor = (n) => Boolean(n && n.closest && n.closest('[data-testid="user-message"], [data-testid="user-message-content"], [contenteditable="true"], textarea'));
  const alertTexts = Array.from(document.querySelectorAll('[role="alert"], [data-testid*="error-banner"], [data-testid="error-message"], .error-banner'))
    .filter(n => !isInsideUserOrEditor(n))
    .map(n => String(n.innerText || n.textContent || '').trim())
    .filter(Boolean)
    .slice(0, 5);
  const dialogTexts = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
    .filter(n => !isInsideUserOrEditor(n))
    .map(n => String(n.innerText || n.textContent || '').trim().slice(0, 500))
    .filter(Boolean)
    .slice(0, 3);
  const userNodes = Array.from(document.querySelectorAll('[data-testid="user-message-content"], [data-testid="user-message"]'));
  const markerInConv = Boolean(marker && userNodes.some(n => String(n.innerText || n.textContent || '').includes(marker)));
  return JSON.stringify({
    url: window.location.href,
    title: document.title || '',
    editor_found: Boolean(editor),
    draft_text: draftText,
    send_button: sendButton,
    model_button: modelButton,
    project_button: projectButton,
    error_banners: alertTexts,
    dialog_texts: dialogTexts,
    marker_in_conversation: markerInConv
  });
})();"""
    return "return (() => {" + JS_FIND_EDITOR_SNIPPET + JS_FIND_SEND_SNIPPET + body.replace("__MARKER__", encoded_marker)


JS_CLICK_SEND = (
    "return (() => {"
    + JS_FIND_SEND_SNIPPET
    + r"""
  const sendFound = findSendButton();
  if (!sendFound) return JSON.stringify({clicked: false, reason: 'send_button_not_found'});
  const btn = sendFound.btn;
  if (btn.disabled || String(btn.getAttribute('aria-disabled') || '').toLowerCase() === 'true') {
    return JSON.stringify({clicked: false, reason: 'send_button_disabled'});
  }
  btn.click();
  return JSON.stringify({clicked: true, url: window.location.href});
})();"""
)


def build_status_inspect_js(task_marker: str) -> str:
    encoded_marker = json.dumps(task_marker)
    body = r"""return (() => {
  const marker = __MARKER__;
  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const s = window.getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const cancelBtn = Array.from(document.querySelectorAll('button[aria-label="Cancel (Ctrl+D)"]')).find(isVisible) || null;
  const isInsideUserOrEditor = (n) => Boolean(n && n.closest && n.closest('[data-testid="user-message"], [data-testid="user-message-content"], [contenteditable="true"], textarea'));
  const errorBanners = Array.from(document.querySelectorAll('[role="alert"], [data-testid*="error-banner"], [data-testid="error-message"], .error-banner'))
    .filter(n => !isInsideUserOrEditor(n) && isVisible(n))
    .map(n => String(n.innerText || n.textContent || '').trim().slice(0, 500))
    .filter(Boolean)
    .slice(0, 5);
  const dialogTexts = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"]'))
    .filter(n => !isInsideUserOrEditor(n) && isVisible(n))
    .map(n => String(n.innerText || n.textContent || '').trim().slice(0, 500))
    .filter(Boolean)
    .slice(0, 3);
  const turnNodes = Array.from(document.querySelectorAll('[data-testid="user-message"], [data-testid="agent-response"]'));
  const turns = [];
  for (const node of turnNodes) {
    const tid = node.getAttribute('data-testid');
    if (tid === 'user-message') {
      const contentNode = node.querySelector('[data-testid="user-message-content"]') || node;
      const txt = String(contentNode.innerText || contentNode.textContent || '').trim();
      if (txt) turns.push({role: 'user', text: txt.slice(0, 2000)});
    } else if (tid === 'agent-response') {
      const txt = String(node.innerText || node.textContent || '').trim();
      if (txt) turns.push({role: 'assistant', text: txt.slice(0, 2000)});
    }
  }
  let markerUserIndex = -1;
  if (marker) {
    for (let i = 0; i < turns.length; i++) {
      if (turns[i].role === 'user' && turns[i].text.includes(marker)) {
        markerUserIndex = i;
      }
    }
  }
  const userMessages = turns.filter(t => t.role === 'user').map(t => t.text).slice(-5);
  const assistantMessages = turns.filter(t => t.role === 'assistant').map(t => t.text).slice(-5);
  const assistantAfterMarker = marker
    ? (markerUserIndex >= 0 ? turns.slice(markerUserIndex + 1).filter(t => t.role === 'assistant').map(t => t.text).slice(-5) : [])
    : assistantMessages;
  return JSON.stringify({
    url: window.location.href,
    title: document.title || '',
    running: Boolean(cancelBtn),
    has_stop_button: Boolean(cancelBtn),
    error_banners: errorBanners,
    dialog_texts: dialogTexts,
    turns: turns.slice(-20),
    user_messages: userMessages,
    assistant_messages: assistantMessages,
    assistant_after_marker: assistantAfterMarker,
    marker_in_conversation: Boolean(marker && markerUserIndex >= 0)
  });
})();"""
    return body.replace("__MARKER__", encoded_marker)


class BrowserUseClient:
    """Bounded subprocess wrapper around real `minis-browser-use` CLI commands."""

    def __init__(
        self,
        browser_bin: str = DEFAULT_BROWSER_BIN,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        runner: SubprocessRunner = default_subprocess_runner,
    ) -> None:
        self.browser_bin = browser_bin
        self.timeout = validate_timeout(timeout)
        self.runner = runner

    def _run(self, args: List[str]) -> Dict[str, Any]:
        argv = [self.browser_bin] + args
        try:
            proc = self.runner(argv, self.timeout)
        except FileNotFoundError as exc:
            raise BrowserError("browser_binary_missing", f"Browser executable not found: {self.browser_bin}") from exc
        except subprocess.TimeoutExpired as exc:
            raise BrowserTimeoutError(
                "browser_timeout",
                f"minis-browser-use timed out after {self.timeout}s.",
            ) from exc
        except OSError as exc:
            raise BrowserError("browser_exec_error", f"Failed to execute browser CLI: {exc}") from exc

        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip() or f"exit code {proc.returncode}"
            raise BrowserError("browser_nonzero_exit", f"minis-browser-use failed: {err}")
        return parse_browser_envelope(proc.stdout)

    def list_tabs(self) -> List[Dict[str, str]]:
        env = self._run(["list_tabs"])
        return parse_tabs_from_envelope(env)

    def open_url(self, url: str) -> Optional[str]:
        safe_url = validate_official_url(url, "open URL")
        env = self._run(["new_tab", "--url", safe_url])
        tabs = parse_tabs_from_envelope(env)
        if tabs:
            return validate_tab_id(tabs[0]["tab_id"])
        if env.get("tab_id") is not None:
            return validate_tab_id(env["tab_id"])
        parsed = env.get("parsed")
        if isinstance(parsed, dict):
            tid = parsed.get("tab_id") if parsed.get("tab_id") is not None else parsed.get("tabId")
            if tid is None:
                tid = parsed.get("id")
            if tid is not None:
                return validate_tab_id(tid)
        return None

    def navigate(self, tab_id: str, url: str) -> Dict[str, Any]:
        safe_tid = validate_tab_id(tab_id)
        safe_url = validate_official_url(url, "navigate URL")
        return self._run(["navigate", "--tab-id", safe_tid, "--url", safe_url])

    def eval_js(self, tab_id: str, js_code: str) -> Dict[str, Any]:
        safe_tid = validate_tab_id(tab_id)
        return self._run(["execute_js", "--tab-id", safe_tid, "--script", js_code])

    def resolve_official_tab(
        self,
        *,
        tab_id_override: Optional[str],
        target_url: str,
        navigate_to_target: bool,
    ) -> str:
        """Discover a matching official tab or open it normally; never persisted."""
        safe_target = validate_official_url(target_url, "target URL")
        if tab_id_override is not None:
            safe_tid = validate_tab_id(tab_id_override)
            if navigate_to_target:
                self.navigate(safe_tid, safe_target)
            return safe_tid

        tabs = self.list_tabs()
        official_tabs = []
        for tab in tabs:
            try:
                validate_official_url(tab["url"], "tab URL")
                official_tabs.append(tab)
            except ValidationError:
                continue

        exact_match = next((t for t in official_tabs if t["url"] == safe_target), None)
        project_match = next(
            (t for t in official_tabs if extract_url_project_id(t["url"]) == OFFICIAL_PROJECT_ID),
            None,
        )
        chosen = exact_match or project_match or (official_tabs[0] if official_tabs else None)

        if chosen is not None:
            if navigate_to_target:
                self.navigate(chosen["tab_id"], safe_target)
            return chosen["tab_id"]

        opened_id = self.open_url(safe_target)
        if opened_id:
            return opened_id

        refreshed = self.list_tabs()
        for tab in refreshed:
            try:
                validate_official_url(tab["url"], "tab URL")
                return tab["tab_id"]
            except ValidationError:
                continue
        raise BrowserError("official_tab_not_found", "Could not discover or open an official Antigravity tab.")


def detect_blocking_reason(
    url: str,
    scoped_texts: Sequence[str],
    error_banners: Sequence[str],
) -> Optional[str]:
    """Detect identity/quota/permission/error blockers from URL, banners, and scoped non-user text."""
    try:
        validate_official_url(url, "current URL")
    except ValidationError:
        return "identity_or_non_official_url"

    combined = "\n".join(list(error_banners) + [t for t in scoped_texts if t])
    if any(p.search(combined) for p in AUTH_BLOCK_PATTERNS):
        return "identity_or_verification"
    if any(p.search(combined) for p in QUOTA_BLOCK_PATTERNS):
        return "quota_or_billing"
    if any(p.search(combined) for p in PERMISSION_BLOCK_PATTERNS):
        return "permission"
    if any(b.strip() for b in error_banners):
        return "ui_error_banner"
    if any(p.search(combined) for p in ERROR_BLOCK_PATTERNS):
        return "ui_error"
    return None


def _select_relevant_assistant_messages(
    parsed: Dict[str, Any],
    task_marker: Optional[str],
) -> Tuple[bool, List[str], List[str]]:
    """Extract (marker_in_conversation, user_messages, assistant_messages_after_marker)."""
    turns = parsed.get("turns")
    if isinstance(turns, list) and turns:
        user_messages = [
            str(t.get("text") or "").strip()
            for t in turns
            if isinstance(t, dict) and t.get("role") == "user" and str(t.get("text") or "").strip()
        ]
        if task_marker:
            marker_idx = -1
            for idx, item in enumerate(turns):
                if isinstance(item, dict) and item.get("role") == "user" and task_marker in str(item.get("text") or ""):
                    marker_idx = idx
            if marker_idx < 0:
                return False, user_messages, []
            after = [
                str(t.get("text") or "").strip()
                for t in turns[marker_idx + 1 :]
                if isinstance(t, dict) and t.get("role") == "assistant" and str(t.get("text") or "").strip()
            ]
            return True, user_messages, after
        all_assistant = [
            str(t.get("text") or "").strip()
            for t in turns
            if isinstance(t, dict) and t.get("role") == "assistant" and str(t.get("text") or "").strip()
        ]
        return bool(parsed.get("marker_in_conversation")), user_messages, all_assistant

    user_messages = [str(x).strip() for x in (parsed.get("user_messages") or []) if str(x).strip()]
    marker_in_conv = bool(parsed.get("marker_in_conversation")) or (
        bool(task_marker) and any(task_marker in u for u in user_messages)
    )
    if "assistant_after_marker" in parsed and isinstance(parsed.get("assistant_after_marker"), list):
        after = [str(x).strip() for x in parsed["assistant_after_marker"] if str(x).strip()]
        if task_marker and not marker_in_conv:
            return False, user_messages, []
        return marker_in_conv, user_messages, after

    assistant_messages = [str(x).strip() for x in (parsed.get("assistant_messages") or []) if str(x).strip()]
    if task_marker and not marker_in_conv:
        return False, user_messages, []
    return marker_in_conv, user_messages, assistant_messages


def classify_ui_status(
    ui_envelope: Dict[str, Any],
    *,
    task_marker: Optional[str] = None,
    fallback_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Classify official UI state into running, blocked, or returned (never accepted)."""
    parsed = ui_envelope.get("parsed")
    human_text = ui_envelope.get("human_text")
    env_page_url = ui_envelope.get("page_url") if isinstance(ui_envelope.get("page_url"), str) else None

    if ui_envelope.get("parsed_is_null") is True:
        return {
            "classification": "blocked",
            "reason": "null_js_return",
            "url": sanitize_text(env_page_url or fallback_url or "", max_len=300),
            "running": False,
            "marker_in_conversation": False,
            "accepted": False,
            "evidence": "execute_js returned null (missing explicit return or empty page state).",
        }

    structured_keys = ("url", "page_url", "running", "turns", "assistant_messages", "assistant_after_marker", "error_banners", "error")
    if isinstance(parsed, dict) and any(k in parsed for k in structured_keys):
        url = str(parsed.get("url") or parsed.get("page_url") or env_page_url or fallback_url or "")
        running = bool(parsed.get("running") or parsed.get("has_stop_button"))
        error_banners = [str(x).strip() for x in (parsed.get("error_banners") or []) if str(x).strip()]
        if parsed.get("error"):
            error_banners.append(sanitize_text(parsed.get("error"), max_len=240))
        if parsed.get("has_error") is True and not error_banners:
            error_banners.append("UI reported has_error=true")
        dialog_texts = [str(x).strip() for x in (parsed.get("dialog_texts") or []) if str(x).strip()]
        marker_in_conv, user_messages, relevant_assistant = _select_relevant_assistant_messages(parsed, task_marker)
        # Never scan user_messages or full page body containing user task goals for blocking keywords!
        scoped_texts = dialog_texts + relevant_assistant
        evidence_fallback = "; ".join(error_banners or dialog_texts or relevant_assistant[-1:]) or "No assistant response yet."
    elif isinstance(human_text, str) and human_text.strip():
        url_match = OFFICIAL_URL_IN_TEXT_RE.search(human_text)
        url = url_match.group(0) if url_match else (env_page_url or fallback_url or "")
        running = bool(re.search(r"(?i)\b(?:running\s*[:=]\s*true|generating|cancel\s*\(ctrl\+d\)|in\s+progress)\b", human_text))
        error_banners = []
        if re.search(r"(?i)\b(?:error\s*:|failed\s*:|exception\s*:|status\s*[:=]\s*(?:error|blocked|failed))\b", human_text):
            error_banners.append(sanitize_text(human_text, max_len=240))
        user_messages = []
        relevant_assistant = []
        scoped_texts = [human_text]
        marker_in_conv = bool(task_marker) and task_marker in human_text
        evidence_fallback = human_text
    else:
        return {
            "classification": "blocked",
            "reason": "malformed_ui_state",
            "url": sanitize_text(env_page_url or fallback_url or "", max_len=300),
            "running": False,
            "marker_in_conversation": False,
            "accepted": False,
            "evidence": "UI parser received empty or unrecognized payload.",
        }

    blocker = detect_blocking_reason(url, scoped_texts, error_banners)
    if blocker in ("identity_or_non_official_url", "identity_or_verification", "quota_or_billing", "permission"):
        return {
            "classification": "blocked",
            "reason": blocker,
            "url": sanitize_text(url, max_len=300),
            "running": running,
            "marker_in_conversation": marker_in_conv,
            "accepted": False,
            "evidence": sanitize_text("; ".join(error_banners) or evidence_fallback, max_len=500),
        }

    if not running and blocker is not None:
        return {
            "classification": "blocked",
            "reason": blocker,
            "url": sanitize_text(url, max_len=300),
            "running": False,
            "marker_in_conversation": marker_in_conv,
            "accepted": False,
            "evidence": sanitize_text("; ".join(error_banners) or evidence_fallback, max_len=500),
        }

    if running:
        return {
            "classification": "running",
            "reason": "agent_running",
            "url": sanitize_text(url, max_len=300),
            "running": True,
            "marker_in_conversation": marker_in_conv,
            "accepted": False,
            "evidence": sanitize_text(evidence_fallback, max_len=400),
        }

    # Not running and no error banners: require genuine assistant response AFTER task_marker distinct from prompt echo
    user_norm = {u.strip() for u in user_messages if u.strip()}
    distinct_assistant = [
        msg
        for msg in relevant_assistant
        if msg.strip() not in user_norm
        and (not task_marker or not msg.strip().startswith(f"[{task_marker}]"))
    ]
    if distinct_assistant:
        return {
            "classification": "returned",
            "reason": "assistant_turn_returned_unverified",
            "url": sanitize_text(url, max_len=300),
            "running": False,
            "marker_in_conversation": marker_in_conv,
            "accepted": False,
            "evidence": sanitize_text(distinct_assistant[-1], max_len=500),
        }

    return {
        "classification": "blocked",
        "reason": "no_assistant_response_or_unconfirmed",
        "url": sanitize_text(url, max_len=300),
        "running": False,
        "marker_in_conversation": marker_in_conv,
        "accepted": False,
        "evidence": sanitize_text(evidence_fallback, max_len=400),
    }


def validate_result_manifest(raw_text: str, validated_contract: Dict[str, Any]) -> Dict[str, Any]:
    """Validate attempt result.json strictly against contract identity, hash, and artifact allowlist."""
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ValidationError("malformed_result_json", f"result.json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("malformed_result_json", "result.json must be a JSON object.")

    required = ("task_id", "attempt", "contract_sha256", "status", "summary", "artifacts", "tests")
    missing = [k for k in required if k not in parsed]
    if missing:
        raise ValidationError("missing_result_fields", f"result.json missing field(s): {', '.join(missing)}")

    if parsed["task_id"] != validated_contract["task_id"]:
        raise ValidationError("result_task_id_mismatch", "result.json task_id does not match contract.")
    res_attempt = parsed["attempt"]
    if isinstance(res_attempt, bool) or not isinstance(res_attempt, int) or res_attempt != validated_contract["attempt"]:
        raise ValidationError("result_attempt_mismatch", "result.json attempt does not match contract.")
    if parsed["contract_sha256"] != validated_contract["contract_sha256"]:
        raise ValidationError("result_contract_hash_mismatch", "result.json contract_sha256 does not match contract.")

    status = parsed["status"]
    if not isinstance(status, str) or status not in ALLOWED_RESULT_STATUSES:
        raise ValidationError(
            "invalid_result_status",
            f"result.json status must be one of {sorted(ALLOWED_RESULT_STATUSES)}.",
        )

    summary = parsed["summary"]
    if not isinstance(summary, str) or not summary.strip():
        raise ValidationError("invalid_result_summary", "result.json summary must be a non-empty string.")

    tests = parsed["tests"]
    if not isinstance(tests, (dict, list)) or len(tests) == 0:
        raise ValidationError("invalid_result_tests", "result.json tests must be a non-empty object or list.")

    raw_artifacts = parsed["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise ValidationError("invalid_result_artifacts", "result.json artifacts must be a list of relative paths.")
    if status == "completed" and len(raw_artifacts) == 0:
        raise ValidationError("empty_result_artifacts", "Completed result must declare at least one artifact.")

    allowlist = set(validated_contract["allowed_write_paths"])
    seen = set()
    validated_artifacts: List[str] = []
    for item in raw_artifacts:
        cleaned = validate_relative_path(item, allow_jobs_prefix=False)
        if cleaned in seen:
            raise ValidationError("duplicate_result_artifact", f"Duplicate artifact in result.json: {cleaned}")
        if cleaned not in allowlist:
            raise ValidationError(
                "artifact_out_of_scope",
                f"Artifact '{cleaned}' is outside contract allowed_write_paths.",
            )
        seen.add(cleaned)
        validated_artifacts.append(cleaned)

    return {
        "task_id": parsed["task_id"],
        "attempt": parsed["attempt"],
        "contract_sha256": parsed["contract_sha256"],
        "status": status,
        "summary": sanitize_text(summary, max_len=1000),
        "artifacts": validated_artifacts,
        "tests": tests,
        "has_self_referential_commit_sha": any(
            k in parsed for k in ("commit_sha", "final_commit_sha", "head_sha")
        ),
    }


def _verify_pre_send_ui(
    inspect_env: Dict[str, Any],
    *,
    expected_prompt: str,
    expected_project: str,
    expected_model: str,
) -> Dict[str, Any]:
    if inspect_env.get("parsed_is_null") is True:
        raise BrowserError("null_js_return", "Pre-send UI inspection returned null.")
    parsed = inspect_env.get("parsed")
    if not isinstance(parsed, dict):
        raise BrowserError("malformed_draft_inspection", "Pre-send UI inspection did not return structured state.")

    url = str(parsed.get("url") or parsed.get("page_url") or inspect_env.get("page_url") or "")
    validate_official_url(url, "pre-send page URL")

    error_banners = [str(x).strip() for x in (parsed.get("error_banners") or []) if str(x).strip()]
    dialog_texts = [str(x).strip() for x in (parsed.get("dialog_texts") or []) if str(x).strip()]
    blocker = detect_blocking_reason(url, dialog_texts, error_banners)
    if blocker is not None:
        raise BrowserError("ui_blocked", f"Official UI is blocked before send ({blocker}).")

    if not parsed.get("editor_found"):
        raise BrowserError("editor_not_found", "Prompt editor not found during pre-send verification.")

    # Project verification strictly from URL ?project= query param OR official project selector button (never body/prompt text)
    if expected_project:
        url_project = extract_url_project_id(url)
        proj_btn = parsed.get("project_button") if isinstance(parsed.get("project_button"), dict) else {}
        proj_btn_found = bool(proj_btn.get("found"))
        proj_btn_label = " ".join(
            [str(proj_btn.get("aria_label") or ""), str(proj_btn.get("text") or "")]
        ).strip()
        expected_low = expected_project.strip().lower()
        is_uuid_expected = bool(re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", expected_low))
        if is_uuid_expected:
            if url_project is None or url_project.lower() != expected_low:
                raise BrowserError(
                    "unexpected_project",
                    f"Expected project query ID '{expected_project}' not verified in official URL.",
                )
        else:
            matched_query = url_project is not None and url_project.lower() == expected_low
            matched_btn = proj_btn_found and expected_low in proj_btn_label.lower()
            if not (matched_query or matched_btn):
                raise BrowserError(
                    "unexpected_project",
                    f"Expected project '{expected_project}' not verified in URL query or official project selector.",
                )

    # Model verification strictly from official model selector button (never body/prompt text)
    if expected_model:
        model_btn = parsed.get("model_button")
        if not isinstance(model_btn, dict) or not model_btn.get("found"):
            raise BrowserError(
                "model_selector_not_found",
                "Official model selector button not found; refusing to verify model from page text.",
            )
        actual_model = " ".join(str(model_btn.get("text") or "").split())
        expected_model_norm = " ".join(expected_model.split())
        if actual_model.lower() != expected_model_norm.lower():
            raise BrowserError(
                "unexpected_model",
                f"Expected model '{expected_model_norm}', but model selector button shows '{actual_model}'.",
            )

    actual_draft = normalize_newlines(str(parsed.get("draft_text") or ""))
    expected_norm = normalize_newlines(expected_prompt)
    if actual_draft != expected_norm:
        raise BrowserError(
            "draft_mismatch",
            "Live draft text does not exactly match expected full prompt; refusing to send.",
            details={"expected_len": len(expected_norm), "actual_len": len(actual_draft)},
        )

    send_btn = parsed.get("send_button")
    if not isinstance(send_btn, dict) or not send_btn.get("found") or not send_btn.get("visible"):
        raise BrowserError("send_button_missing", "Send button is missing or not visible; refusing to send.")
    if send_btn.get("disabled") is True or send_btn.get("aria_disabled") is True:
        raise BrowserError("send_button_disabled", "Send button is disabled; refusing to modify disabled or click.")

    return parsed


def cmd_validate(args: argparse.Namespace) -> Tuple[int, Dict[str, Any]]:
    validated = load_and_validate_contract(args.contract)
    return 0, {
        "command": "validate",
        "valid": True,
        "repository": validated["repository"],
        "task_id": validated["task_id"],
        "attempt": validated["attempt"],
        "base_branch": validated["base_branch"],
        "job_branch": validated["job_branch"],
        "task_snapshot_path": validated["task_snapshot_path"],
        "result_snapshot_path": validated["result_snapshot_path"],
        "allowed_write_paths": validated["allowed_write_paths"],
        "effective_allowed_paths": validated["effective_allowed_paths"],
        "contract_sha256": validated["contract_sha256"],
        "task_marker": validated["task_marker"],
    }


def cmd_submit(
    args: argparse.Namespace,
    *,
    mcp_client: Optional[GitHubMcpClient] = None,
    browser_client: Optional[BrowserUseClient] = None,
) -> Tuple[int, Dict[str, Any]]:
    validated = load_and_validate_contract(args.contract)
    project_url = validate_official_url(args.project_url, "project URL")
    timeout = validate_timeout(args.timeout)
    if args.tab_id is not None:
        validate_tab_id(args.tab_id)
    journal = JobJournal(args.state_dir)
    prompt_text = build_gemini_prompt(validated)

    task_id = validated["task_id"]
    attempt = validated["attempt"]
    job_branch = validated["job_branch"]
    task_snapshot_path = validated["task_snapshot_path"]
    result_snapshot_path = validated["result_snapshot_path"]
    contract_sha256 = validated["contract_sha256"]
    task_marker = validated["task_marker"]

    existing_job = journal.load(task_id)
    if attempt > 1:
        if existing_job is None:
            raise ValidationError(
                "missing_prior_journal",
                f"Attempt {attempt} requires an existing local job journal for '{task_id}'.",
            )
        prior_attempts = [int(k) for k in existing_job.get("attempts", {}).keys() if k.isdigit() and int(k) < attempt]
        if not prior_attempts:
            raise ValidationError(
                "missing_prior_attempt",
                f"Attempt {attempt} requires at least one prior attempt recorded in local journal.",
            )

    if args.dry_run:
        return 0, {
            "command": "submit",
            "dry_run": True,
            "mutated_github": False,
            "sent_message": False,
            "repository": validated["repository"],
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "task_snapshot_path": task_snapshot_path,
            "result_snapshot_path": result_snapshot_path,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
            "project_url": project_url,
            "planned_github_actions": [
                f"check branch {job_branch}",
                f"create branch {job_branch} from {validated['base_branch']} (attempt 1 only)" if attempt == 1 else f"verify existing branch {job_branch} and prior snapshot",
                f"persist immutable snapshot {task_snapshot_path}",
            ],
            "planned_browser_actions": [
                "discover/open official tab and navigate to project URL",
                "focus editor + execCommand('selectAll')",
                "dispatch ClipboardEvent('paste') with DataTransfer text/plain",
                "read-only inspect draft, project, model, and send button state",
                "journal submit_uncertain before clicking non-disabled send button",
            ],
            "prompt_preview": prompt_text,
        }

    mcp = mcp_client or GitHubMcpClient(args.mcp_bin, args.mcp_server, timeout)
    browser = browser_client or BrowserUseClient(args.browser_bin, timeout)

    job = journal.ensure_job(validated)
    attempt_entry = journal.ensure_attempt(job, validated)

    # Idempotency & uncertain-state reconciliation BEFORE touching GitHub writes or sending
    if attempt_entry["state"] in ("submitted", "collected_not_accepted"):
        return 0, {
            "command": "submit",
            "status": "already_submitted",
            "sent": False,
            "reconciled": False,
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
            "conversation_url": attempt_entry.get("conversation_url"),
        }

    if attempt_entry["state"] == "submit_uncertain" or attempt_entry.get("send_initiated") is True:
        # Never automatically double-submit: reconcile via GitHub result or official conversation UI
        remote_head = mcp.get_branch_head_sha(job_branch)
        if remote_head:
            existing_res = mcp.get_file_text(result_snapshot_path, remote_head)
            if existing_res is not None:
                attempt_entry["state"] = "submitted"
                attempt_entry["updated_at"] = utc_now_iso()
                journal.save(task_id, job)
                return 0, {
                    "command": "submit",
                    "status": "reconciled_already_submitted",
                    "sent": False,
                    "reconciled": True,
                    "reconciled_via": "github_result_snapshot",
                    "task_id": task_id,
                    "attempt": attempt,
                    "job_branch": job_branch,
                    "contract_sha256": contract_sha256,
                    "task_marker": task_marker,
                }

        reconcile_url = attempt_entry.get("conversation_url") or project_url
        tab_id = browser.resolve_official_tab(
            tab_id_override=args.tab_id,
            target_url=reconcile_url,
            navigate_to_target=False,
        )
        ui_env = browser.eval_js(tab_id, build_status_inspect_js(task_marker))
        status_info = classify_ui_status(ui_env, task_marker=task_marker, fallback_url=reconcile_url)
        if status_info.get("marker_in_conversation"):
            if status_info.get("url"):
                try:
                    attempt_entry["conversation_url"] = validate_official_url(status_info["url"])
                except ValidationError:
                    pass
            attempt_entry["state"] = "submitted"
            attempt_entry["updated_at"] = utc_now_iso()
            journal.save(task_id, job)
            return 0, {
                "command": "submit",
                "status": "reconciled_already_submitted",
                "sent": False,
                "reconciled": True,
                "reconciled_via": "official_conversation_marker",
                "task_id": task_id,
                "attempt": attempt,
                "job_branch": job_branch,
                "contract_sha256": contract_sha256,
                "task_marker": task_marker,
                "conversation_url": attempt_entry.get("conversation_url"),
            }

        journal.record_error(
            attempt_entry,
            "submit_reconcile",
            "Previous send attempt is uncertain and task marker was not confirmed; refusing automatic double submit.",
        )
        journal.save(task_id, job)
        return 3, {
            "command": "submit",
            "status": "uncertain_no_double_submit",
            "sent": False,
            "reconciled": False,
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
            "error": "Previous send attempt is uncertain; refusing to double-send. Inspect conversation or create attempt+1.",
        }

    # Step 1: GitHub branch & immutable task snapshot sync/reconciliation
    branch_head = mcp.get_branch_head_sha(job_branch)
    if attempt == 1:
        if branch_head is None:
            mcp.create_branch(job_branch, validated["base_branch"])
            attempt_entry["branch_created"] = True
            journal.save(task_id, job)
        else:
            existing_snapshot = mcp.get_file_text(task_snapshot_path, branch_head)
            if existing_snapshot is not None:
                validate_snapshot_matches_contract(existing_snapshot, contract_sha256)
                attempt_entry["branch_created"] = True
                attempt_entry["task_snapshot_written"] = True
                journal.save(task_id, job)
            elif not attempt_entry.get("branch_created"):
                raise ValidationError(
                    "job_branch_already_exists",
                    f"Job branch '{job_branch}' already exists on GitHub without matching attempt-01 snapshot.",
                )
    else:
        if branch_head is None:
            raise ValidationError(
                "missing_job_branch",
                f"Attempt {attempt} requires existing GitHub job branch '{job_branch}'.",
            )
        prev_task_path, _ = derive_snapshot_paths(task_id, attempt - 1)
        prev_snapshot = mcp.get_file_text(prev_task_path, branch_head)
        if prev_snapshot is None:
            raise ValidationError(
                "missing_prior_snapshot",
                f"Attempt {attempt} requires preserved prior snapshot '{prev_task_path}' on GitHub.",
            )
        existing_snapshot = mcp.get_file_text(task_snapshot_path, branch_head)
        if existing_snapshot is not None:
            validate_snapshot_matches_contract(existing_snapshot, contract_sha256)
            attempt_entry["task_snapshot_written"] = True
            journal.save(task_id, job)

    if not attempt_entry.get("task_snapshot_written"):
        snapshot_content = build_snapshot_document(validated)
        mcp.create_file_immutable(
            branch=job_branch,
            path=task_snapshot_path,
            content=snapshot_content,
            message=f"chore(bridge): persist immutable task snapshot {task_id} attempt-{attempt:02d}",
        )
        attempt_entry["task_snapshot_written"] = True
        attempt_entry["state"] = "github_synced"
        journal.save(task_id, job)

    # Check if result already exists on GitHub before touching browser
    current_head = mcp.get_branch_head_sha(job_branch)
    if current_head and mcp.get_file_text(result_snapshot_path, current_head) is not None:
        attempt_entry["state"] = "submitted"
        journal.save(task_id, job)
        return 0, {
            "command": "submit",
            "status": "reconciled_already_submitted",
            "sent": False,
            "reconciled": True,
            "reconciled_via": "github_result_snapshot",
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
        }

    # Step 2: Official browser UI navigation -> focus+selectAll -> paste -> separate inspect
    tab_id = browser.resolve_official_tab(
        tab_id_override=args.tab_id,
        target_url=project_url,
        navigate_to_target=True,
    )

    try:
        focus_env = browser.eval_js(tab_id, JS_FOCUS_SELECT_ALL)
        focus_parsed = focus_env.get("parsed")
        if not isinstance(focus_parsed, dict) or not focus_parsed.get("ok"):
            raise BrowserError("focus_select_failed", "Failed to focus prompt editor and run execCommand(selectAll).")

        paste_env = browser.eval_js(tab_id, build_paste_js(prompt_text))
        paste_parsed = paste_env.get("parsed")
        if not isinstance(paste_parsed, dict) or not paste_parsed.get("dispatched"):
            raise BrowserError("paste_dispatch_failed", "Failed to dispatch ClipboardEvent paste.")

        inspect_env = browser.eval_js(tab_id, build_inspect_draft_js(task_marker))
        inspected = _verify_pre_send_ui(
            inspect_env,
            expected_prompt=prompt_text,
            expected_project=args.expected_project,
            expected_model=args.expected_model,
        )
    except BridgeError as exc:
        attempt_entry["state"] = "blocked_before_send"
        journal.record_error(attempt_entry, "pre_send", exc.message)
        journal.save(task_id, job)
        raise

    # Step 3: Journal uncertain state BEFORE clicking send (prevents automatic double-submit)
    attempt_entry["state"] = "submit_uncertain"
    attempt_entry["send_initiated"] = True
    attempt_entry["send_initiated_at"] = utc_now_iso()
    attempt_entry["prompt_sha256"] = sha256_text(prompt_text)
    pre_send_url = str(inspected.get("url") or inspected.get("page_url") or inspect_env.get("page_url") or project_url)
    attempt_entry["conversation_url"] = validate_official_url(pre_send_url)
    journal.save(task_id, job)

    # Step 4: Click non-disabled send button
    try:
        click_env = browser.eval_js(tab_id, JS_CLICK_SEND)
    except BrowserTimeoutError as exc:
        journal.record_error(attempt_entry, "click_send_timeout", exc.message)
        journal.save(task_id, job)
        return 3, {
            "command": "submit",
            "status": "uncertain",
            "sent": None,
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
            "error": exc.message,
        }
    except BrowserError as exc:
        journal.record_error(attempt_entry, "click_send_error", exc.message)
        journal.save(task_id, job)
        return 3, {
            "command": "submit",
            "status": "uncertain",
            "sent": None,
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "task_marker": task_marker,
            "error": exc.message,
        }

    click_parsed = click_env.get("parsed")
    if not isinstance(click_parsed, dict) or not click_parsed.get("clicked"):
        reason = click_parsed.get("reason") if isinstance(click_parsed, dict) else "unknown_click_result"
        attempt_entry["state"] = "blocked_before_send"
        attempt_entry["send_initiated"] = False
        journal.record_error(attempt_entry, "click_send_refused", str(reason))
        journal.save(task_id, job)
        raise BrowserError("send_not_clicked", f"Send button was not clicked ({reason}).")

    # Step 5: Bounded post-send read-only confirmation
    post_url = str(
        click_parsed.get("url")
        or click_parsed.get("page_url")
        or click_env.get("page_url")
        or attempt_entry["conversation_url"]
    )
    try:
        post_env = browser.eval_js(tab_id, build_status_inspect_js(task_marker))
        post_status = classify_ui_status(post_env, task_marker=task_marker, fallback_url=post_url)
        if post_status.get("url"):
            try:
                attempt_entry["conversation_url"] = validate_official_url(post_status["url"])
            except ValidationError:
                pass
        if post_status.get("marker_in_conversation") or post_status.get("classification") == "running":
            attempt_entry["state"] = "submitted"
            attempt_entry["submitted_at"] = utc_now_iso()
            journal.save(task_id, job)
            return 0, {
                "command": "submit",
                "status": "submitted",
                "sent": True,
                "task_id": task_id,
                "attempt": attempt,
                "job_branch": job_branch,
                "task_snapshot_path": task_snapshot_path,
                "contract_sha256": contract_sha256,
                "task_marker": task_marker,
                "conversation_url": attempt_entry["conversation_url"],
            }
    except BrowserError as exc:
        journal.record_error(attempt_entry, "post_send_confirm", exc.message)

    journal.save(task_id, job)
    return 3, {
        "command": "submit",
        "status": "uncertain",
        "sent": True,
        "task_id": task_id,
        "attempt": attempt,
        "job_branch": job_branch,
        "contract_sha256": contract_sha256,
        "task_marker": task_marker,
        "conversation_url": attempt_entry.get("conversation_url"),
        "error": "Send click executed, but post-send confirmation did not yet verify task marker; reconcile via status/submit without double-sending.",
    }


def cmd_status(
    args: argparse.Namespace,
    *,
    browser_client: Optional[BrowserUseClient] = None,
) -> Tuple[int, Dict[str, Any]]:
    timeout = validate_timeout(args.timeout)
    journal = JobJournal(args.state_dir)

    if args.contract:
        validated = load_and_validate_contract(args.contract)
        task_id = validated["task_id"]
        attempt = args.attempt if args.attempt is not None else validated["attempt"]
        task_marker = validated["task_marker"]
    elif args.task_id:
        task_id = validate_task_id(args.task_id)
        attempt = validate_attempt(args.attempt) if args.attempt is not None else None
        task_marker = None
    else:
        raise ValidationError("missing_task_selector", "Provide either --contract or --task-id.")

    job = journal.load(task_id)
    if job is None:
        raise ValidationError("journal_not_found", f"No local job journal found for task '{task_id}'.")

    if attempt is None:
        numeric = sorted(int(k) for k in job.get("attempts", {}).keys() if k.isdigit())
        if not numeric:
            raise ValidationError("no_attempts_recorded", f"No attempts recorded in journal for '{task_id}'.")
        attempt = numeric[-1]
    else:
        attempt = validate_attempt(attempt)

    attempt_entry = journal.get_attempt(job, attempt)
    if attempt_entry is None:
        raise ValidationError("attempt_not_found", f"Attempt {attempt} not found in journal for '{task_id}'.")

    task_marker = task_marker or attempt_entry.get("task_marker")
    target_url = args.conversation_url or attempt_entry.get("conversation_url") or DEFAULT_PROJECT_URL
    target_url = validate_official_url(target_url, "conversation URL")

    browser = browser_client or BrowserUseClient(args.browser_bin, timeout)
    tab_id = browser.resolve_official_tab(
        tab_id_override=args.tab_id,
        target_url=target_url,
        navigate_to_target=False,
    )
    ui_env = browser.eval_js(tab_id, build_status_inspect_js(task_marker or ""))
    classified = classify_ui_status(ui_env, task_marker=task_marker, fallback_url=target_url)

    obs = {
        "timestamp": utc_now_iso(),
        "classification": classified["classification"],
        "reason": classified["reason"],
        "url": classified["url"],
        "running": classified["running"],
        "marker_in_conversation": classified["marker_in_conversation"],
        "evidence": classified["evidence"],
    }
    history = attempt_entry.setdefault("status_history", [])
    history.append(obs)
    if len(history) > 50:
        del history[:-50]

    if classified["url"]:
        try:
            attempt_entry["conversation_url"] = validate_official_url(classified["url"])
        except ValidationError:
            pass
    if classified["classification"] == "blocked":
        journal.record_error(attempt_entry, "status_blocked", f"{classified['reason']}: {classified['evidence']}")
    elif attempt_entry["state"] == "submit_uncertain" and classified["marker_in_conversation"]:
        attempt_entry["state"] = "submitted"
    attempt_entry["updated_at"] = utc_now_iso()
    journal.save(task_id, job)

    exit_code = 2 if classified["classification"] == "blocked" else 0
    return exit_code, {
        "command": "status",
        "task_id": task_id,
        "attempt": attempt,
        "job_branch": job["job_branch"],
        "classification": classified["classification"],
        "reason": classified["reason"],
        "accepted": False,
        "running": classified["running"],
        "marker_in_conversation": classified["marker_in_conversation"],
        "conversation_url": classified["url"],
        "evidence": classified["evidence"],
    }


def _safe_write_collected_file(root: Path, rel_path: str, content: str) -> Path:
    cleaned = validate_relative_path(rel_path, allow_jobs_prefix=True)
    root_resolved = root.resolve()
    target = (root_resolved / cleaned).resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise ValidationError("local_path_escape", f"Refusing to write outside collection directory: {rel_path}") from exc
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp.{os.getpid()}")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, target)
    return target


def cmd_collect(
    args: argparse.Namespace,
    *,
    mcp_client: Optional[GitHubMcpClient] = None,
) -> Tuple[int, Dict[str, Any]]:
    validated = load_and_validate_contract(args.contract)
    timeout = validate_timeout(args.timeout)
    journal = JobJournal(args.state_dir)

    task_id = validated["task_id"]
    attempt = validated["attempt"]
    job_branch = validated["job_branch"]
    task_snapshot_path = validated["task_snapshot_path"]
    result_snapshot_path = validated["result_snapshot_path"]
    contract_sha256 = validated["contract_sha256"]
    collect_root = journal.collected_dir / task_id / f"attempt-{attempt:02d}"

    if args.dry_run:
        return 0, {
            "command": "collect",
            "dry_run": True,
            "mutated_github": False,
            "repository": validated["repository"],
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "contract_sha256": contract_sha256,
            "planned_reads": [
                f"resolve authoritative head SHA of {job_branch}",
                f"fetch {task_snapshot_path} at head SHA",
                f"fetch {result_snapshot_path} at head SHA",
                "fetch each allowlisted artifact declared in result.json at head SHA",
            ],
            "local_collect_dir": str(collect_root),
        }

    mcp = mcp_client or GitHubMcpClient(args.mcp_bin, args.mcp_server, timeout)
    job = journal.ensure_job(validated)
    attempt_entry = journal.ensure_attempt(job, validated)

    head_sha: Optional[str] = None
    result_sha256: Optional[str] = None
    try:
        head_sha = mcp.get_branch_head_sha(job_branch)
        if not head_sha:
            raise ValidationError("missing_job_branch", f"Job branch '{job_branch}' not found on GitHub.")

        # Verify immutable task snapshot at authoritative head SHA
        remote_task_text = mcp.get_file_text(task_snapshot_path, head_sha)
        if remote_task_text is None:
            raise ValidationError("missing_remote_task_snapshot", f"Missing '{task_snapshot_path}' at {head_sha}.")
        validate_snapshot_matches_contract(remote_task_text, contract_sha256)

        # Fetch and validate result.json at authoritative head SHA
        raw_result_text = mcp.get_file_text(result_snapshot_path, head_sha)
        if raw_result_text is None:
            raise ValidationError("missing_result_manifest", f"Missing '{result_snapshot_path}' at {head_sha}.")
        result_sha256 = sha256_text(raw_result_text)
        manifest = validate_result_manifest(raw_result_text, validated)

        if manifest["status"] == "blocked":
            record = {
                "timestamp": utc_now_iso(),
                "status": "blocked",
                "head_sha": head_sha,
                "result_sha256": result_sha256,
                "summary": manifest["summary"],
                "artifacts": [],
            }
            attempt_entry.setdefault("collection_history", []).append(record)
            journal.record_error(attempt_entry, "collect_blocked", manifest["summary"])
            journal.save(task_id, job)
            return 2, {
                "command": "collect",
                "status": "blocked",
                "accepted": False,
                "task_id": task_id,
                "attempt": attempt,
                "job_branch": job_branch,
                "head_sha": head_sha,
                "contract_sha256": contract_sha256,
                "result_sha256": result_sha256,
                "summary": manifest["summary"],
                "tests": manifest["tests"],
            }

        # Fetch every allowlisted artifact at authoritative head_sha (read-only; never execute!)
        fetched_contents: List[Tuple[str, str]] = []
        for art_path in manifest["artifacts"]:
            art_text = mcp.get_file_text(art_path, head_sha)
            if art_text is None:
                raise ValidationError(
                    "missing_artifact",
                    f"Declared artifact '{art_path}' not found at commit {head_sha}.",
                )
            if not art_text.strip():
                raise ValidationError(
                    "empty_artifact_content",
                    f"Declared artifact '{art_path}' at commit {head_sha} is empty.",
                )
            fetched_contents.append((art_path, art_text))

        # Write result.json + fetched artifacts to isolated local collection directory
        local_result_path = _safe_write_collected_file(collect_root, result_snapshot_path, raw_result_text)
        artifact_records: List[Dict[str, Any]] = []
        for art_path, art_text in fetched_contents:
            local_path = _safe_write_collected_file(collect_root, art_path, art_text)
            raw_bytes = art_text.encode("utf-8")
            artifact_records.append(
                {
                    "path": art_path,
                    "local_path": str(local_path),
                    "sha256": sha256_bytes(raw_bytes),
                    "size_bytes": len(raw_bytes),
                }
            )

        record = {
            "timestamp": utc_now_iso(),
            "status": "collected_not_accepted",
            "head_sha": head_sha,
            "result_sha256": result_sha256,
            "local_result_path": str(local_result_path),
            "artifacts": artifact_records,
        }
        attempt_entry.setdefault("collection_history", []).append(record)
        attempt_entry["state"] = "collected_not_accepted"
        attempt_entry["updated_at"] = utc_now_iso()
        journal.save(task_id, job)

        return 0, {
            "command": "collect",
            "status": "collected_not_accepted",
            "accepted": False,
            "review_required": True,
            "repository": validated["repository"],
            "task_id": task_id,
            "attempt": attempt,
            "job_branch": job_branch,
            "head_sha": head_sha,
            "contract_sha256": contract_sha256,
            "result_sha256": result_sha256,
            "local_result_path": str(local_result_path),
            "artifacts": artifact_records,
            "summary": manifest["summary"],
            "tests": manifest["tests"],
            "evidence": {
                "task_snapshot_verified": True,
                "self_referential_commit_sha_present": manifest["has_self_referential_commit_sha"],
                "executed_downloaded_code": False,
            },
        }
    except BridgeError as exc:
        failure_record = {
            "timestamp": utc_now_iso(),
            "status": "collection_failed",
            "head_sha": head_sha,
            "result_sha256": result_sha256,
            "error_code": exc.code,
            "error": exc.message,
        }
        attempt_entry.setdefault("collection_history", []).append(failure_record)
        journal.record_error(attempt_entry, "collect", f"{exc.code}: {exc.message}")
        journal.save(task_id, job)
        raise


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="minis_gemini_bridge.py",
        description="Fail-closed Minis GPT -> official Gemini Pro web agent -> GitHub -> GPT bridge CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_val = sub.add_parser("validate", help="Pure local validation of a task contract JSON file.")
    p_val.add_argument("--contract", required=True, help="Path to task contract JSON file.")
    p_val.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local job journal directory (unused by validate).")

    p_sub = sub.add_parser("submit", help="Sync GitHub job branch/snapshot and submit prompt via official UI.")
    p_sub.add_argument("--contract", required=True, help="Path to task contract JSON file.")
    p_sub.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local job journal directory.")
    p_sub.add_argument("--tab-id", default=None, help="Ephemeral numeric browser tab ID override (never persisted).")
    p_sub.add_argument("--dry-run", action="store_true", help="Preview actions and prompt without mutating GitHub or sending.")
    p_sub.add_argument("--project-url", default=DEFAULT_PROJECT_URL, help="Official Antigravity project URL (?project=<id>).")
    p_sub.add_argument("--expected-project", default=OFFICIAL_PROJECT_ID, help="Expected project query ID or selector label verified before send.")
    p_sub.add_argument("--expected-model", default=DEFAULT_EXPECTED_MODEL, help="Expected exact model button text verified before send (default: Gemini 4 Neon High).")
    p_sub.add_argument("--browser-bin", default=DEFAULT_BROWSER_BIN, help="Browser CLI executable.")
    p_sub.add_argument("--mcp-bin", default=DEFAULT_MCP_BIN, help="MCP CLI executable.")
    p_sub.add_argument("--mcp-server", default=DEFAULT_MCP_SERVER, help="MCP server name.")
    p_sub.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command timeout in seconds (1..120).")

    p_st = sub.add_parser("status", help="Perform one bounded official UI status inspection (no polling/sleep).")
    p_st.add_argument("--contract", default=None, help="Path to task contract JSON file.")
    p_st.add_argument("--task-id", default=None, help="Task identifier (if --contract is omitted).")
    p_st.add_argument("--attempt", type=int, default=None, help="Attempt number (defaults to latest recorded).")
    p_st.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local job journal directory.")
    p_st.add_argument("--tab-id", default=None, help="Ephemeral numeric browser tab ID override (never persisted).")
    p_st.add_argument("--conversation-url", default=None, help="Official conversation URL override.")
    p_st.add_argument("--browser-bin", default=DEFAULT_BROWSER_BIN, help="Browser CLI executable.")
    p_st.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command timeout in seconds (1..120).")

    p_col = sub.add_parser("collect", help="Fetch and validate result.json + allowlisted artifacts at authoritative GitHub head.")
    p_col.add_argument("--contract", required=True, help="Path to task contract JSON file.")
    p_col.add_argument("--state-dir", default=DEFAULT_STATE_DIR, help="Local job journal directory.")
    p_col.add_argument("--dry-run", action="store_true", help="Preview planned GitHub MCP fetches without calling MCP or writing files.")
    p_col.add_argument("--mcp-bin", default=DEFAULT_MCP_BIN, help="MCP CLI executable.")
    p_col.add_argument("--mcp-server", default=DEFAULT_MCP_SERVER, help="MCP server name.")
    p_col.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="Per-command timeout in seconds (1..120).")

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            code, payload = cmd_validate(args)
        elif args.command == "submit":
            code, payload = cmd_submit(args)
        elif args.command == "status":
            code, payload = cmd_status(args)
        elif args.command == "collect":
            code, payload = cmd_collect(args)
        else:
            raise ValidationError("unknown_command", f"Unknown command: {args.command}")
    except BridgeError as exc:
        error_payload = {
            "command": getattr(args, "command", None),
            "ok": False,
            "error_code": exc.code,
            "error": exc.message,
        }
        if exc.details:
            error_payload["details"] = exc.details
        sys.stdout.write(json.dumps(error_payload, indent=2, ensure_ascii=False) + "\n")
        return 1

    sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
