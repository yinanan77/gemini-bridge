# Independent Security & Correctness Audit: `AUDIT-BRIDGE-V1`

- **Audit ID**: `AUDIT-BRIDGE-V1`
- **Repository**: `yinanan77/gemini-bridge`
- **Review Branch**: `review/gpt-gemini-v1`
- **Audited Commits**:
  - Source implementation: `4d2e3bfc6a0656b1e8c463b1a4ce2ada20e9f5ce`
  - Live acceptance evidence & fixtures: `7e4630eb62ba4c24718d02957703906817f79fca`
- **Inspected Files**:
  - `bridge/minis_gemini_bridge.py` (2264 lines)
  - `bridge/test_bridge.py` (1232 lines)
  - `bridge/WORKFLOW.md` (125 lines)
  - `bridge/task.example.json` (18 lines)
  - `orchestration/bootstrap/task.json` (32 lines)
  - `orchestration/bootstrap/revision-01.md` (49 lines)
  - `orchestration/bootstrap/protocol-fixtures.json` (45 lines)
  - `orchestration/bootstrap/result.json` (32 lines)
- **Conclusion**: **`needs-fix`**

---

## 1. Executive Summary

Although the existing mocked unit test suite passes (`27/27`), the audited baseline (`4d2e3bfc6a0656b1e8c463b1a4ce2ada20e9f5ce`) is **not production-ready and fails fail-closed guarantees**.

This independent audit:
1. **Confirms all P0/P1 defects recorded in `orchestration/bootstrap/revision-01.md`** directly against source code and `orchestration/bootstrap/protocol-fixtures.json` — and identifies a stricter severity on one of them (Google's real `"Verify your account to continue."` reply is actively misclassified as **`returned`**, exit `0`, rather than `blocked`).
2. **Identifies 10 categories of additional concrete defects** not yet documented in `revision-01.md`, including **3 new P0 (Critical)** and **4 new P1 (High)** defects affecting:
   - Double-submit / uncertain-send wipeout (`cmd_submit` resets `send_initiated=False` on unparsed post-click responses),
   - Stale assistant reply and wrong-tab status contamination (including overwriting `conversation_url` in the journal and cross-attempt marker mismatch),
   - Detached `document.body.cloneNode(true).innerText` leakage of `<script>`/`<style>`/hidden DOM templates in WebKit/Chromium,
   - Local symlink escape & arbitrary file overwrite in `_safe_write_collected_file` and `JobJournal.save`,
   - Collector scope blindness (unauthorized branch writes like `.github/workflows/*` and tampered prior attempt snapshots go undetected) plus unsanitized secret leakage in `result.json["tests"]`,
   - GitHub MCP error misclassification (`"not found"` tool/server errors treated as missing branch/file) and directory/symlink/JSON-collision extraction bugs.
3. **Notes explicit runtime-unverified boundaries**: No real `minis-browser-use` or `minis-mcp-cli` binaries exist in this cloud audit environment. While real `minis-browser-use` fixtures exist in `protocol-fixtures.json`, **no real `minis-mcp-cli` fixtures exist**, so live `minis-mcp-cli` argv syntax, tool/parameter names, and `create_or_update_file` overwrite semantics remain unverified until exercised on iOS Minis.

---

## 2. Test Execution & Reproduction Evidence

### 2.1 Existing Tests Executed (Cloud Sandbox, Mocked CLI I/O Only)

| Check | Command | Exit Code | Result |
| --- | --- | --- | --- |
| Bytecode compile | `python3 -m py_compile bridge/minis_gemini_bridge.py bridge/test_bridge.py` | `0` | Passed (syntax valid) |
| Unit tests | `python3 -m unittest discover -s bridge -p test_bridge.py -v` | `0` | **27 passed, 0 failed, 0 errors** (0.231s; all `minis-browser-use` and `minis-mcp-cli` calls mocked) |

> **Runtime limitation**: Neither `minis-browser-use` nor `minis-mcp-cli` exists in this cloud environment. No live Minis CLI calls or network calls were performed or faked.

### 2.2 Minimal Non-Network Reproduction Probes

All findings below were verified via non-network Python reproduction probes executed directly against `bridge/minis_gemini_bridge.py` and `orchestration/bootstrap/protocol-fixtures.json` (no production files edited). Key reproduced outputs are cited per finding below.

---

## 3. Confirmed `revision-01.md` Blockers (Fixes Required)

| ID | Severity | Location | Confirmed Defect & Evidence |
| --- | --- | --- | --- |
| **R1** | **P0 (Critical)** | `BrowserUseClient.list_tabs` (`L1216-1218`), `open_url` (`L1220-1231`), `navigate` (`L1233-1235`), `eval_js` (`L1237-1238`); JS scripts (`L1038-1181`) | Invokes nonexistent verbs `["tabs"]`, `["open", url]`, `["navigate", "--tab-id", id, url]` (missing `--url`), and `["eval", "--tab-id", id, js]` instead of `list_tabs`, `new_tab --url`, `navigate --tab-id --url`, `execute_js --tab-id --script`. All 5 JS scripts (`JS_FOCUS_SELECT_ALL`, `build_paste_js`, `build_inspect_draft_js`, `JS_CLICK_SEND`, `build_status_inspect_js`) lack a top-level `return`, yielding `null\n  tab_id: 4` inside `execute_js`'s async wrapper (`browser-iife-real.json`). |
| **R2** | **P0 (Critical)** | `parse_browser_envelope` (`L902-957`, esp. `L917-955`) | Fails to parse JSON in `data.text` when followed by real `\n  tab_id: N` suffix (`browser-object-real.json` drops `{"probe": true}` into `human_text`). Ignores `data.page_url` (`L943` only checks `container.get("url")`). **Never checks `data.success is False`** (`L917` only checks outer `parsed.get("ok") is False`): probe with `{"ok": true, "data": {"success": false, "text": "ReferenceError: ..."}}` succeeded without raising `BrowserError`. |
| **R3** | **P0 (Critical)** | `parse_tabs_from_envelope` (`L960-991`, esp. `L988`) | Against `browser-tabs-real.json`, regex `(?:tab[_-]?id\s*[:=]\s*\|\[|#)?([A-Za-z0-9_-]{1,64})` extracts `tab_id: "Tab"` instead of `"4"`. Also fails on `new_tab` text `"Opened new tab N at URL"` (would extract `"Opened"`). Must strictly parse numeric tab IDs and reject ambiguous output. |
| **R4** | **P0 (Critical)** | `DEFAULT_PROJECT_URL` (`L39`), `DEFAULT_EXPECTED_MODEL` (`L40`), `_verify_pre_send_ui` (`L1513-1527`) | Uses `/remote/projects/<id>` instead of `/remote?project=<id>` and `"Pro"` instead of live `"Gemini 4 Neon High"`. Worse, `_verify_pre_send_ui` concatenates `visible_text[:1000]` into both `project_haystack` (`L1514`) and `model_haystack` (`L1522`): probe passed `_verify_pre_send_ui` with `model_text="Gemini Ultra Paid Credits"` and `project_text="WRONG-PROJECT"` solely because `visible_text` mentioned the project UUID and `"Pro"`. |
| **R5** | **P1 (High)** | `JS_FIND_EDITOR_SNIPPET` (`L994-1015`), `build_inspect_draft_js` (`L1101-1103`), `build_status_inspect_js` (`L1150-1163`) | DOM selectors miss real Antigravity elements (`[contenteditable=true][aria-label="Message input"]`, `button[data-testid="model-selector-trigger"]`, `button[aria-label^="Select project, current:"]`, `[data-testid="user-message"]`, `[data-testid="agent-response"]`, `button[aria-label="Cancel (Ctrl+D)"]`). In `build_status_inspect_js` (`L1153`), non-raw `f"""... /\b(?:stop|cancel)\b/i ..."""` embeds literal `\x08` backspace bytes (`"\x08" in build_status_inspect_js("probe") == True`), permanently breaking stop/cancel button detection. |
| **R6** | **P1 (High)** | `AUTH_BLOCK_PATTERNS` (`L97-99`), `detect_blocking_reason` (`L1287-1305`), `classify_ui_status` (`L1354-1415`) | Misses Google's real `"Verify your account to continue."` — **probe confirms `classify_ui_status` classifies this real account-verification failure in `assistant_messages` as `classification="returned"` (exit `0`)** instead of `blocked`. Conversely, scanning all of `visible_text` (`L1294`) causes a genuine completed assistant reply mentioning `"sign in"`, `"rate limit"`, or `"network error"` to be falsely classified as `blocked` (`identity_or_verification`). |

---

## 4. Additional Concrete Defects Discovered in Independent Audit

### A1. [P0 / Critical] `cmd_submit` Wipes `submit_uncertain` / `send_initiated` on Non-Structured Post-Click Output, Enabling Automatic Double-Submit
- **Location**: `cmd_submit` (`bridge/minis_gemini_bridge.py:1862-1869`)
- **Defect**:
  After persisting `state = "submit_uncertain"` and `send_initiated = True` (`L1823-1828`) and executing `browser.eval_js(tab_id, JS_CLICK_SEND)` (`L1832`), lines `1862-1869` treat **any** response where `click_parsed` is not a dict with `clicked=True` (including `reason = "unknown_click_result"`) as proof that send was NOT clicked, and explicitly reset:
  ```python
  attempt_entry["state"] = "blocked_before_send"
  attempt_entry["send_initiated"] = False
  ```
  If `btn.click()` actually ran in the browser, but the CLI returned `data.text` with `\n  tab_id: 4` appended, `null` after immediate navigation, or any malformed/partial envelope, the uncertain-send guard is wiped out. A subsequent `submit` call passes the guard at `L1651` and clicks Send a second time.
- **Reproduction Evidence**:
  Probe 3 simulated `btn.click()` succeeding and returning real `minis-browser-use` envelope `{"ok": true, "data": {"success": true, "text": "{\"clicked\": true, \"url\": \"...\"}\n  tab_id: 4"}}`:
  - First `submit` raised `send_not_clicked`, overwrote journal to `state="blocked_before_send"`, `send_initiated=False`.
  - Second `submit` with identical contract & `--state-dir` **clicked Send again** (`total_click_send_calls == 2`).
- **Required Fix**: Only clear `send_initiated` when `click_parsed` is a verified structured object explicitly proving pre-click refusal (`clicked is False` AND `reason in {"send_button_not_found", "send_button_disabled"}`). All other post-click anomalies must preserve `state="submit_uncertain"`, `send_initiated=True` and exit `3`.

---

### A2. [P0 / Critical] Stale Assistant Reply, Wrong-Tab Fallback, Cross-Attempt Marker Mismatch & Journal `conversation_url` Corruption
- **Location**:
  - `classify_ui_status` (`bridge/minis_gemini_bridge.py:1388-1405`)
  - `build_status_inspect_js` (`bridge/minis_gemini_bridge.py:1160-1178`)
  - `cmd_status` (`bridge/minis_gemini_bridge.py:1923-1927, 1951-1982`)
  - `BrowserUseClient.resolve_official_tab` (`bridge/minis_gemini_bridge.py:1254-1270`)
- **Defects**:
  1. **`marker_in_conversation` not required for `returned`**: `classify_ui_status` (`L1388-1405`) classifies `returned` whenever `distinct_assistant` is non-empty, even when `marker_in_conversation` is `False`.
  2. **No DOM turn ordering**: `build_status_inspect_js` (`L1160-1163`) scrapes `assistantNodes` and `userNodes` independently without recording DOM order relative to the user message containing `task_marker`. Any prior assistant turn in the same conversation falsely marks a newly submitted attempt as `returned`.
  3. **Weak prompt-echo filter**: `L1389-1395` checks only exact string equality against `user_messages` (both truncated to 1000 chars at `L1162-1163`, whereas prompts from `build_gemini_prompt` are ~1200+ chars) and `startswith(f"[{task_marker}]")`. Any assistant text quoting/echoing the prompt with a prefix (e.g., `"Echo: [GPT-GEMINI-TASK:...]"`) passes as `returned`.
  4. **Wrong-tab fallback corrupts journal `conversation_url`**: `cmd_status` (`L1956-1960`) calls `resolve_official_tab(..., navigate_to_target=False)`, which falls back to `project_match` (`OFFICIAL_PROJECT_ID in t["url"]`, `L1264`) — selecting any other open conversation tab in the same project. `cmd_status` then classifies `returned` from that unrelated tab AND unconditionally overwrites `attempt_entry["conversation_url"]` (`L1978-1982`) with the wrong conversation URL.
  5. **Cross-attempt `task_marker` bug**: When `cmd_status` receives both `--contract` (e.g., attempt 1) and `--attempt 2` (`L1923-1927, L1951`), it sets `task_marker = validated["task_marker"]` (attempt 1's marker) instead of `attempt_entry.get("task_marker")` (attempt 2's marker).
- **Reproduction Evidence** (Probe 4):
  - `stale_reply_without_marker_classification == "returned"` (even with `marker_in_conversation=False`).
  - `prefixed_echo_classification == "returned"`.
  - `cmd_status` for attempt 2 saved with `conv-aaa`, when browser had tab at `conv-bbb`, returned `classification="returned"`, overwrote `journal["attempts"]["2"]["conversation_url"]` to `conv-bbb`, and searched for attempt 1's `task_marker` instead of attempt 2's (`cmd_status_contract_attempt1_override_attempt2_used_attempt1_marker == True`).
- **Required Fix**: Require `marker_in_conversation` verified strictly inside `[data-testid="user-message"]` elements; extract ordered conversation turns and consider only `[data-testid="agent-response"]` nodes appearing **after** the matching user turn; reject echoes containing `task_marker` or high overlap with the user prompt; never silently inspect a different conversation URL or overwrite `conversation_url` unless the target tab contains the attempt's `task_marker`; when `--attempt` differs from `--contract`'s attempt, reject mismatch or load `task_marker` from the selected `attempt_entry`.

---

### A3. [P0 / Critical] Detached `document.body.cloneNode(true).innerText` Leaks `<script>`/`<style>`/Hidden DOM and Sidebar Text
- **Location**:
  - `build_inspect_draft_js` (`bridge/minis_gemini_bridge.py:1107-1111, 1121-1122`)
  - `build_status_inspect_js` (`bridge/minis_gemini_bridge.py:1164-1168, 1177-1178`)
- **Defect**:
  Both JS scripts clone `document.body` (`const bodyClone = document.body.cloneNode(true)`) without attaching it to the document, then read `bodyClone.innerText`. Per the HTML/CSSOM specification implemented by WebKit (iOS Minis WKWebView) and Chromium, **`innerText` on a disconnected node falls back to `textContent`**, returning all `<script>`, `<style>`, hydration JSON, and hidden (`display: none`) modal/dialog templates.
  - Any bundled SPA string or hidden error dialog template containing `"Sign in"`, `"Verify you are human"`, `"Permission denied"`, `"Something went wrong"`, or `"Internal error"` leaks into `visible_text` and permanently blocks `submit` and `status`.
  - Scanning `nonEditorText.includes(marker)` (`L1121, L1177`) across the whole body also matches conversation history sidebars listing previous chats by their first line (`[GPT-GEMINI-TASK:...]`), causing false-positive `marker_in_conversation=True` on unrelated tabs/pages.
- **Required Fix**: Never read `innerText` from a detached clone or scan the entire `document.body`. Query only specific visible live DOM regions (e.g., active conversation `[data-testid="user-message"]` / `[data-testid="agent-response"]`, explicit visible error banners, and actual model/project trigger buttons).

---

### A4. [P1 / High] Local Symlink Escape & Arbitrary File Overwrite in `_safe_write_collected_file` and `JobJournal.save`
- **Location**:
  - `_safe_write_collected_file` (`bridge/minis_gemini_bridge.py:2006-2018`)
  - `JobJournal.save` (`bridge/minis_gemini_bridge.py:543-552`)
- **Defects**:
  1. **Root symlink escape**: `_safe_write_collected_file` calls `root_resolved = root.resolve()` (`L2008`) before verifying containment. If `state_dir`, `state_dir/collected`, or `collect_root` is a symlink pointing outside the state directory, `target.relative_to(root_resolved)` succeeds and files are written outside `state_dir`.
  2. **Internal symlink target overwrite**: `target = (root_resolved / cleaned).resolve()` (`L2009`) follows a pre-existing symlink at `target` pointing to another file inside `root_resolved` (e.g., `src/calc.py -> ../jobs/<task>/attempt-01/result.json`), so `os.replace(tmp, target)` overwrites the resolved target (`result.json`) instead of rejecting the symlink.
  3. **Predictable `.tmp.<pid>` symlink follow**: Both `_safe_write_collected_file` (`L2015-2016`) and `JobJournal.save` (`L548-550`) write to `f"{name}.tmp.{os.getpid()}"` using `Path.write_text()`, which follows symlinks. A pre-existing symlink at the predictable temp filename causes arbitrary file overwrite anywhere on disk.
  4. **Journal directory symlink & durability/locking**: `JobJournal.save` performs zero symlink checks on `state_dir`/`jobs_dir`, does not `fsync` before `os.replace` (risking zero-byte/lost `submit_uncertain` journal on iOS suspension/crash), and does not acquire a lock (`fcntl.flock`) to prevent concurrent `submit` races.
- **Reproduction Evidence** (Probe 8):
  - `escaped_via_collect_root_symlink == True` (wrote `outside_a/src/calc.py`).
  - `internal_symlink_overwrote_result_json == "OVERWRITTEN_VIA_INTERNAL_SYMLINK"`.
  - `tmp_pid_symlink_overwrote_outside_victim == "PWNED_VIA_TMP_SYMLINK"`.
  - `journal_escaped_via_jobs_symlink == True` (wrote `outside_jobs/probe-task-v1.json`).
- **Required Fix**: Reject any symlink component along `state_dir` -> `jobs`/`collected` -> target path (`is_symlink()` checks on every parent and leaf, plus `O_NOFOLLOW`); open temp files via `os.open(..., os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)` with random suffixes (`secrets.token_hex(8)`); `fsync` file + directory before `os.replace`; reject writing over symlinks; use an advisory lock around journal read-modify-write in `submit`.

---

### A5. [P1 / High] Collector Scope Blindness, Missing Prior-Snapshot Hash Verification & Unsanitized Secret Leakage in `tests`
- **Location**:
  - `cmd_collect` (`bridge/minis_gemini_bridge.py:2064-2172`)
  - `cmd_submit` (`bridge/minis_gemini_bridge.py:1748-1754`)
  - `validate_result_manifest` (`bridge/minis_gemini_bridge.py:1451-1453, 1483-1486`)
- **Defects**:
  1. **Unauthorized branch modifications undetected**: `cmd_collect` only fetches paths listed in `result.json["artifacts"]`. It never compares `base_branch...head_sha` (or commit changed files) to verify that the job branch modified ONLY `effective_allowed_paths`. An agent can commit `.github/workflows/evil.yml`, modify `README.md` or `bridge/minis_gemini_bridge.py`, omit them from `result.json["artifacts"]`, and `cmd_collect` exits `0` (`collected_not_accepted`).
  2. **Prior snapshots never integrity-checked**: On `attempt > 1`, `cmd_submit` (`L1748-1754`) only checks `prev_snapshot is None`, never validating `prev_snapshot`'s `contract_sha256` against the journal or its own hash. `cmd_collect` for `attempt > 1` does not check prior attempt snapshots (`attempt-01..attempt-(N-1)` `task.json` / `result.json`) at all.
  3. **Unsanitized & unbounded `tests` output**: `validate_result_manifest` (`L1483`) passes `result.json["tests"]` verbatim to `cmd_collect` stdout without `sanitize_text()` or size limits. Credentials/tokens returned inside `tests` leak directly to stdout, violating the contract requirement to avoid outputting secrets.
  4. **Self-referential commit SHA allowed**: `L1484-1486` only sets an informational flag `has_self_referential_commit_sha` for 3 key names (`commit_sha`, `final_commit_sha`, `head_sha`) and still returns exit `0` instead of rejecting or strictly flagging all commit-SHA fields.
- **Reproduction Evidence** (Probe 7):
  - Seeded `.github/workflows/evil.yml` on `job_branch` + `"final_commit_sha"` + `"ghp_1234567890abcdef12345678"` inside `result.json["tests"]`: `cmd_collect` returned exit `0` (`collected_not_accepted`) and leaked `ghp_1234567890abcdef12345678` unsanitized in stdout JSON.
  - Replaced `attempt-01/task.json` on GitHub with `{"tampered": true}`: `cmd_submit` for attempt 2 passed prior-snapshot validation and proceeded to write `attempt-02/task.json`.
- **Required Fix**: Verify changed files between `base_branch` and `head_sha` via GitHub MCP (or fail closed / explicitly mark scope-unverified if MCP lacks compare/commit-files tool); verify all prior `attempt-<k>/task.json` hashes on `attempt > 1` in both `submit` and `collect`; recursively sanitize and size-bound `tests` before output/storage; reject self-referential commit SHA fields in `result.json`.

---

### A6. [P1 / High] Additional Uncertain-Send, Restart & Idempotency Defects
- **Location**:
  - `cmd_submit` (`bridge/minis_gemini_bridge.py:1673-1680, 1731-1736, 1774-1796, 1809-1828, 1881`)
  - `build_status_inspect_js` (`bridge/minis_gemini_bridge.py:1155`)
- **Defects**:
  1. **False post-send confirmation without `marker_in_conversation`**: `L1881` marks `state = "submitted"` if `post_status.get("marker_in_conversation") or post_status.get("classification") == "running"`. Combined with `busyNode = document.querySelector('[aria-busy="true"], ...')` (`L1155`), any unrelated loading spinner causes `running=True` and falsely marks the task `submitted` even when `marker_in_conversation` is `False` (Probe 5: exit `0`, `status="submitted"`).
  2. **Pre-send `marker_in_conversation` computed but ignored**: `build_inspect_draft_js` (`L1121`) computes `marker_in_conversation`, but neither `_verify_pre_send_ui` nor `cmd_submit` checks it. If the tab already contains `task_marker`, `cmd_submit` clicks send again (Probe 3).
  3. **Restart / fresh `--state-dir` double-submit while attempt 1 is still running**: If `attempt-01/task.json` already exists on GitHub with matching `contract_sha256` and local journal is missing (e.g., cleared/new `--state-dir`), `L1731-1736` marks `task_snapshot_written = True`, sees no `result.json` yet (`L1775`), navigates to `project_url` (`navigate_to_target=True`, `L1795`), and double-submits without checking open tabs for `task_marker` (Probe 9: `clicks == 1`, `status == "submitted"`).
  4. **Single-tab reconciliation**: When reconciling `submit_uncertain` (`L1673-1680`), `cmd_submit` inspects only one tab returned by `resolve_official_tab(..., navigate_to_target=False)` (whose `conversation_url` was saved pre-send as `project_url`, so `resolve_official_tab` picks the first matching tab, potentially an older conversation) rather than scanning all open official tabs for `task_marker`.
- **Required Fix**: Require `marker_in_conversation=True` (in `[data-testid="user-message"]`) before transitioning `submit_uncertain -> submitted`; refuse to send if pre-send inspection already detects `marker_in_conversation`; when an existing remote `task.json` is discovered without local journal state, treat as uncertain/reconcile across all open official tabs (or require explicit reconciliation/attempt+1) rather than blindly submitting; scan all official tabs during `submit_uncertain` reconciliation.

---

### A7. [P1 / High] GitHub MCP Error Misclassification & File-Content Parser Vulnerabilities
- **Location**:
  - `unwrap_mcp_envelope` (`bridge/minis_gemini_bridge.py:630-691`)
  - `_raise_classified_mcp_error` (`bridge/minis_gemini_bridge.py:709-713`)
  - `GitHubMcpClient.get_branch_head_sha` (`L753-780`), `get_file_text` (`L807-829`)
  - `extract_file_text_from_mcp_envelope` (`bridge/minis_gemini_bridge.py:850-899`)
- **Defects**:
  1. **Broad `"not found"` misclassification**: `_raise_classified_mcp_error` (`L711`) maps *any* error containing `"not found"` (including `"Tool 'list_commits' not found"`, `"MCP server 'GitHub' not found"`, `"Repository not found"`) to `McpNotFoundError`. Both `get_branch_head_sha` (`L760`) and `get_file_text` (`L814`) catch `McpNotFoundError` and silently return `None`, disguising broken MCP configuration as "branch/file does not exist" (can skip `result.json` existence check before submit or trigger unintended `create_branch`).
  2. **Directory JSON array accepted as file content**: `extract_file_text_from_mcp_envelope` (`L867`) checks `isinstance(parsed, dict) and parsed.get("type") == "dir"`, but GitHub's Contents API returns a **JSON list** (`[{"type": "file", ...}]`) for directories. A list falls through to `L887` and is returned as valid file text.
  3. **Git symlink / submodule accepted as normal file**: `L867-881` does not reject `parsed.get("type") in ("symlink", "submodule")`; a git symlink object with base64 content is decoded and accepted as a normal artifact.
  4. **Valid JSON artifact corruption / rejection**: Any real JSON artifact file containing `{"content": "...", "path": "...", "sha": "..."}` or `{"content": "...", "encoding": "base64"}` is misinterpreted as a GitHub Contents API wrapper (`L869-881`), returning only `parsed["content"]`; a valid JSON artifact containing `{"type": "dir"}` is rejected as `mcp_unexpected_directory`.
  5. **Status/error text returned as file content & `data.success=False` ignored**: `unwrap_mcp_envelope` never checks `inner.get("success") is False` (`L645-651`) and only unwraps `"data"` then `"result"` (not `"result"` then `"data"`). If an MCP response contains only status text (e.g., `"successfully downloaded text file"`) or an error message without both `"404"` and `"not found"`, `L883-887` returns that text verbatim as file content.
- **Reproduction Evidence** (Probe 6):
  - `tool_not_found_treated_as_branch_none == True`.
  - `github_dir_array_returned_as_file_text == '[{"type": "file", "name": "a.py"}]'`.
  - `github_symlink_returned_as_file_text == "/etc/passwd"`.
  - `valid_json_file_mangled_to == "INNER_ONLY"`.
  - `mcp_data_success_false_returned_as_file_text == "successfully downloaded text file"`.
- **Required Fix**: Check `success is False` at all envelope levels; restrict `McpNotFoundError` to GitHub HTTP 404 / resource-not-found responses (never tool/server/binary/repository-not-found); reject `list` directory payloads and `type != "file"` (`dir`/`symlink`/`submodule`); distinguish MCP resource/contents envelopes strictly rather than blindly returning fallback status text.

---

### A8. [P2 / Medium] Immutable Snapshot Metadata Tampering & Canonical Contract Parsing Gaps
- **Location**:
  - `validate_snapshot_matches_contract` (`bridge/minis_gemini_bridge.py:450-478`)
  - `load_and_validate_contract` (`bridge/minis_gemini_bridge.py:426-438`)
  - `cmd_submit` (`bridge/minis_gemini_bridge.py:1726-1729, 1761-1771`)
- **Defects**:
  1. **Missing `contract_sha256` & tampered snapshot paths accepted**: `validate_snapshot_matches_contract` (`L459-471`) allows `recorded_hash is None` and strips `task_snapshot_path`/`result_snapshot_path` without verifying they equal `revalidated["task_snapshot_path"]` and `revalidated["result_snapshot_path"]`. A tampered remote snapshot with `"result_snapshot_path": "evil/hijacked_result.json"` and omitted `contract_sha256` passes validation (Probe 7: `tampered_snapshot_missing_hash_and_bad_result_path_accepted == True`).
  2. **Duplicate JSON keys silently accepted**: `load_and_validate_contract` (`L435`) uses `json.loads` without `object_pairs_hook`, accepting duplicate keys with last-key-wins semantics (Probe 9: duplicate `"goal"` silently became `"evil"`), which can cause hash disagreement across JSON parsers.
  3. **Existing snapshot on `base_branch` unchecked**: On `attempt == 1` when `branch_head is None` (`L1726-1729`), `cmd_submit` creates `job_branch` from `base_branch` and immediately calls `create_file_immutable` without checking whether `task_snapshot_path` already exists on `base_branch`. Additionally, some GitHub MCP `create_or_update_file` implementations auto-resolve existing file SHA when `sha` is omitted and silently overwrite.
- **Required Fix**: Require `recorded_hash == computed` (non-null) and exact equality of `task_snapshot_path` and `result_snapshot_path` in `validate_snapshot_matches_contract`; reject duplicate JSON keys via `object_pairs_hook` in contract/snapshot/result loaders; verify `task_snapshot_path` does not exist on `base_branch` before writing attempt 1.

---

### A9. [P2 / Medium] Path Validation Case Bypass, Prefix Collision Crash, Unvalidated `--tab-id`, URL Hardening & Journal `tab_id` Leak
- **Location**:
  - `validate_relative_path` (`bridge/minis_gemini_bridge.py:240-280`, esp. `L275`)
  - `validate_contract_dict` (`L347-354`)
  - `_safe_write_collected_file` / `cmd_collect` (`L2006-2018, L2124-2136`)
  - `BrowserUseClient.navigate` / `eval_js` / `resolve_official_tab` (`L1233-1252`)
  - `validate_official_url` (`L178-195`)
  - `JobJournal._strip_forbidden_fields` (`L554-565`)
- **Defects**:
  1. **Case-sensitive `jobs/` & duplicate check**: `L275` checks `segments[0] == "jobs"` without `.lower()`, allowing `Jobs/<task>/attempt-01/task.json` into `allowed_write_paths` (Probe 8). `L347-354` also permits case-colliding entries `["src/Calc.py", "src/calc.py"]` (Probe 9). On case-insensitive filesystems (iOS APFS mounts), these collide with bridge snapshots or each other.
  2. **File/directory prefix collision causes unhandled crash**: `validate_contract_dict` accepts `["src/a", "src/a/b.py"]`; `cmd_collect` then crashes mid-write with unhandled `FileExistsError` (`L2014`) outside `except BridgeError`, leaving partial files on disk and emitting a Python traceback instead of structured JSON (Probe 8).
  3. **Stale files in `collect_root` on re-collect**: `cmd_collect` (`L2124-2136`) does not stage into a fresh directory or clean stale artifacts from a previous collection of the same attempt.
  4. **Unvalidated `tab_id` allows CLI option injection**: `BrowserUseClient` (`L1233-1252`) passes `--tab-id` directly into `argv` without validating `^[0-9]+$` (Probe 9: `--tab-id --json` produces `["minis-browser-use", "eval", "--tab-id", "--json", ...]`).
  5. **URL validation gaps**: `validate_official_url` (`L178-195`) accepts non-default port `https://antigravity.google.com:8443/remote`, empty-user authority `https://@antigravity.google.com/remote`, and path traversal `https://antigravity.google.com/remote/../signin` (Probe 8).
  6. **Permanent journal leaks `tab_id: 4` via `human_text` evidence**: When `parse_browser_envelope` falls back to `human_text` on `browser-object-real.json`, `cmd_status` stores `human_text` (containing `\n  tab_id: 4`) in `status_history[].evidence`, bypassing `JobJournal._strip_forbidden_fields` (which only strips dict keys `tab_id`/`tabId`) (Probe 4: `journal_contains_leaked_tab_id_4 == True`).
- **Required Fix**: Lowercase `segments[0]` in `jobs` check and enforce case-insensitive uniqueness + non-prefix-collision across `allowed_write_paths`; wrap filesystem `OSError` in `BridgeError` and stage collection writes cleanly; strictly validate `tab_id` as positive ASCII digits (`^[0-9]+$`); reject non-default ports, `"@" in parsed.netloc`, whitespace/control chars, and `".."` path segments in `validate_official_url`; strip `\n  tab_id: \d+` envelope trailer before building `evidence`.

---

### A10. [P3 / Low] Documentation Drift & Unverified Live `minis-mcp-cli` Protocol Risk
- **Location**: `bridge/WORKFLOW.md` (`L7, L54`), `orchestration/bootstrap/protocol-fixtures.json`
- **Defects / Risks**:
  - `bridge/WORKFLOW.md` still documents `/remote` without `?project=<id>` and states `"预期项目与 Pro 模型匹配"` (`L54`), conflicting with real live UI (`Gemini 4 Neon High`).
  - `protocol-fixtures.json` only captures `minis-browser-use` responses. **Real `minis-mcp-cli` CLI invocation syntax (`minis-mcp-cli call GitHub <tool> <json>`), actual GitHub MCP tool/parameter names (`list_commits` `perPage` vs `per_page`, `get_file_contents` `ref` vs `branch`, `create_branch` `from_branch`), `create_or_update_file` overwrite semantics without `sha`, and actual MCP output envelope shapes have not been captured or verified on Minis.**

---

## 5. Summary of Residual Risks

1. **Live `minis-mcp-cli` protocol mismatch risk (High)**: Just as `minis-browser-use` had real-CLI verb/flag/envelope mismatches hidden by unit-test mocks, `minis-mcp-cli` has not yet been fixture-verified against Minis. Capture real read-only `minis-mcp-cli` fixtures on Minis before declaring end-to-end readiness.
2. **Live Antigravity DOM/clipboard behavior risk (Medium)**: Real `ClipboardEvent('paste')` with `DataTransfer('text/plain')` into `[contenteditable="true"][aria-label="Message input"]` (`role="combobox"`), exact `innerText` newline normalization vs `expected_prompt`, and `button[aria-label="Cancel (Ctrl+D)"]` timing must be verified on live Minis UI after fixing R1–R6 and A1–A9.
3. **iOS Minis suspension/filesystem risk (Medium)**: Without `fsync` + file locking + symlink checks + APFS case-collision checks, app suspension or concurrent runs can still corrupt local journal state or cause duplicate dispatches.

---

## 6. Final Verdict

**Conclusion: `needs-fix`**

Do **not** accept or merge `4d2e3bfc6a0656b1e8c463b1a4ce2ada20e9f5ce`. Require remediation of all confirmed `revision-01.md` blockers (**R1–R6**) and all newly identified **P0/P1** defects (**A1–A7**), plus **P2** hardening (**A8–A9**), with regression unit tests covering every reproduced probe and captured real `minis-browser-use` + `minis-mcp-cli` protocol fixtures before re-auditing on live Minis.
