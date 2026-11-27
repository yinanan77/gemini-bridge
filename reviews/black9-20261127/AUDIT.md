# Black9 汇总与同步工具链独立只读审计报告

- **审计任务**：`black9-audit-20261127`（Attempt 01）
- **被审计对象**：`yinanan77/gemini-bridge` 分支 `gpt-gemini/jobs/black9-consolidate-20261127` 提交 **`37aac5aac646cd679f10e0a430a6bb8e1841408f`**
- **审计范围（只读，未修改开发分支与任何既有文件）**：
  - `tools/black9/consolidate.py`（1,185 行，`sha256: 6e812bac2f282637435edc8fd0c191b47e90eb8ec0c2a72a71842938a03bab86`）
  - `tools/black9/sync.py`（1,698 行，`sha256: bd737c8d840486d689a93268f5721123bf88e28c4b185d0d0bfcbedf158e4c23`）
  - `tools/black9/test_black9.py`（942 行，`sha256: e1c5ae3dd1ed3f46d98f630aacaed274b3dde4a8735c3f4345c224a0ca7c2af7`）
  - `tools/black9/README.md`（132 行，`sha256: 5b29ca061a1426cb86cf7e6f63047b393b3ad54bdbd9b19001e8bf9801de69e3`）
- **审计结论**：**不建议直接上线生产同步（Changes Requested）**。开发基线 13 项本地 Mock 单测全部通过，安全凭据卫生与“手工/Mock”诚实边界做得较好；但独立代码审阅与 14 项独立探针（4 项正向验证 + 10 项可执行缺陷复现）证实存在 **2 个 Critical、5 个 High、2 个 Medium、1 个 Low** 问题，涉及运行时 `notion-update-page` 参数与 `async_task` 不兼容、用户完整目标内容缺失（附录 A 108 行与独立补研究 C01–C11）、祖先符号链接逃逸、不确定建页恢复跨父页劫持、推送锁死无恢复路径、`<content/>` 误清正文及视觉模糊行误标为口播正文。

---

## 一、Mock 验证、已复现缺陷与未验证（真实部署）边界

| 类别 | 明确状态 | 核验明细 |
| :--- | :--- | :--- |
| **本地 Mock 基线测试** | **通过（13/13）** | 从精确提交 `37aac5a` 提取至临时目录执行 `py_compile`（3/3）与 `unittest discover`（13/13 全部 OK，耗时 ~3.2s）。 |
| **独立正向探针** | **通过（4/4）** | 1) 附录 B 11 案 `[23,22,22,22,37,37,30,26,16,49,51] = 335` 条且兼容 `A01` / `B01` ID；2) `tools Notion`、`notion-fetch`（含前缀 `Here...<page>...<content>`）、`notion-create-pages` 运行时包络正常路径；3) 纯 stdlib、零 `os.environ`/`/var/minis`/HTTP/后台定时任务；4) 正常 `init`+`sync` 不向 `state.json`/`.journal.jsonl`/stdout 泄漏私密正文。 |
| **独立缺陷复现探针** | **全部复现（10/10）** | `reviews/black9-20261127/test_independent.py` 中 `F01`–`F10` 均确定性复现（14/14 探针脚本可独立运行且零残留未跟踪文件）。 |
| **线上真实部署与私密源** | **未验证（Unverified）** | 本审计与开发者单测均未读取用户私密原文、未调用真实 `minis-mcp-cli` / Notion OAuth 工作区、未实测真实 Notion 双向回写序列化与 GitHub Actions `safe_two_way` `.sync/state.json` 联调；上述必须由主控环境在修复 Critical/High 阻断项后实测确认。 |

### 开发契约 vs. 用户完整目标差异说明

- **对开发契约（`black9-consolidate-20261127`）**：开发者按其 `task.json` 实现了 `# 附录B` 335 条单元构建器、手工 MCP 子进程适配器与保守边界说明（`README.md:5-9` 明确声明非自动同步、未实测真实 Notion）。
- **对用户原始目标**：用户目标是将黑桃 9 全部 11 案完整文稿与分析无损汇总至 Obsidian 独立目录并镜像到 Notion，验证安全真实双向同步。当前实现**缺失两大实质分析来源**（见 **F02**），且在真实运行时 `notion-update-page` / Notion Markdown 回读 / 恢复路径上存在阻断性缺陷（见 **F01、F04、F05、F06**）。

---

## 二、审计发现详单（全部绑定精确行号与可执行复现）

### [CRITICAL] F01：`notion-update-page` 遗漏 `allow_async: false` 且完全忽略 `async_task` 返回，导致写入竞态与永久锁库

- **代码位置**：
  - `tools/black9/sync.py:399-467`（`_build_update_page_payload`）
  - `tools/black9/sync.py:527-539`（`update_page`）
  - `tools/black9/sync.py:1445-1480`（`run_sync` 推送与立即回读校验）
  - `tools/black9/test_black9.py:146-166, 306-317`（Mock 仅覆盖包裹 `{"data": {...}}` 与同步返回，未覆盖真实扁平 schema 与 `async_task`）
- **问题分析**：
  1. 已知运行时 `notion-update-page` 参数形如 `{"page_id": ID, "command": "replace_content", "new_str": BODY, "allow_async": false}`。`_build_update_page_payload` 从未检查或填入 `allow_async: false`（若 schema 将其标为 `required` 则直接抛 `SchemaValidationError`；若为可选则允许 Notion 走异步任务）。
  2. `update_page` 在第 535 行调用 `_extract_mcp_result_payload(stdout, "notion-update-page")` 后直接丢弃返回值，**既不识别 `async_task` 也不轮询或安全失败**。
  3. 当 Notion 返回 `{"async_task": {...}}` 时，`run_sync` 立即执行第 1462 行 `client.fetch_page(page_id)`，读到尚未落盘的旧正文，触发 `push_verify_mismatch` 写入 journal（第 1474 行），继而**永久阻断后续全部可变 `sync` 操作**（第 1272-1276 行）。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F01_update_page_omits_allow_async_and_ignores_async_task`。
- **修复建议**：schema 包含 `allow_async` 时显式发送 `False`；解析 `update_page` 返回体，如含 `async_task` 则执行有界轮询或安全终止为可恢复的异步待确认状态。

---

### [CRITICAL] F02：仅解析 `# 附录B`，实质性遗漏 `# 附录A`（108 行分组深度分析）与独立补充源（`## 二、黑桃11版本逐段补研究` C01–C11）

- **代码位置**：
  - `tools/black9/consolidate.py:334-395`（`parse_source_markdown` 跳过 `# 附录B` 之前所有正文，仅收集标题）
  - `tools/black9/consolidate.py:636-691`（`render_reference_markdown` 仅列标题索引，未纳入附录 A 表格）
  - `tools/black9/consolidate.py:1108-1151`（CLI 无补充源参数）
- **问题分析**：
  1. 来源文档 `# 附录A` 包含 **108 行逐组深度分析表**，当前解析器在扫描至 `# 附录B` 前将其全部丢弃，11 篇案例页与 `manifest.json` 中 0 条保留。
  2. 独立补充源中 `## 二、黑桃11版本逐段补研究` 下 `### C01`–`C11` 各 4 问（至 `## 三、思远11版本逐段补研究` 前截止以排除思源内容）完全没有 CLI 入口与解析逻辑。
  3. 归因说明：该缺口源自开发 `task.json` 仅要求 `# 附录B`；但面向用户“汇总全部 11 案完整文稿与分析”的真实目标，属于关键实质性内容缺失，必须由主控决策补齐后再行正式交付。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F02_missing_appendix_a_108_rows_and_supplement_c01_c11`。
- **修复建议**：新增 `# 附录A`（108 行强校验）解析并扁平化并入对应 `01_C01.md`–`11_C11.md` 的分组深度分析小节；新增 `--supplement` 解析 `## 二、黑桃11版本逐段补研究`（`### C01`–`C11` 各 4 问）并在遇到 `## 三、思远11版本逐段补研究` 时严格截断。

---

### [HIGH] F03：祖先符号链接逃逸绕过状态文件、日志、冲突快照、源文件读取与构建输出路径保护

- **代码位置**：
  - `tools/black9/sync.py:586-597`（`check_state_paths_safety` 仅检查 `state_path` 与直接 `parent`）
  - `tools/black9/consolidate.py:135-170`（`check_no_symlink_chain` 在 `stop_at=None` 时跳过全部父目录检查，与 docstring 承诺不符；`stop_at` 非空时也不检查 `stop_at` 上级祖先）
  - `tools/black9/consolidate.py:172-180`（`ensure_safe_file_read` 甚至未检查 `path.parent.is_symlink()`）
  - `tools/black9/consolidate.py:182-206`（`atomic_write_bytes` 调用 `parent.mkdir(parents=True)` 在符号链接目标内建目录）
  - `tools/black9/consolidate.py:709-713, 754-758, 1025-1038`；`tools/black9/sync.py:676, 1258-1260, 1651-1657`
- **问题分析**：
  - 当 `--state <symlink_dir>/nested/black9_state.json`（即便 `nested` 尚未存在）传入 `sync.py init/sync` 时，`state_path.is_symlink()` 与 `state_path.parent.is_symlink()` 均为 `False`，`atomic_write_bytes` 直接在符号链接指向的外部目录创建 `nested/` 并写入 `state.json`、`.journal.jsonl` 与 `conflicts/<doc_id>/*`。
  - 同理，`consolidate.py build` 的 `--source <symlink_dir>/s.md` 与 `--output <symlink_dir>/nested` 均可经符号链接祖先逃逸读写。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F03_symlink_ancestor_escape_state_bundle_source`。
- **修复建议**：实现逐级祖先路径符号链接检测（遍历路径所有非系统根父段），并在 `build`、`fetch`、`init`、`sync`、`inspect --output` 入口统一强制调用。

---

### [HIGH] F04：不确定建页恢复（`--recover`）未向子页写入 `task_marker`，可静默劫持另一父页下的同名旧页；且建页后回读失败时忽略已落盘 `page_id`

- **代码位置**：
  - `tools/black9/sync.py:1064-1083`（构造了 `page_marker`，写入 journal，但 `create_page` 发送的 `content=doc["text"]` 根本不含该 marker）
  - `tools/black9/sync.py:814-851`（`_recover_uncertain_create` 在 `pid not in parent_body` 时回退到第 835-841 行，仅凭 `title` + `source_sha256` 匹配全工作区页面）
  - `tools/black9/sync.py:1099-1136`（保存 `page_id` 后执行 `fetch_page`，若回读失败则 journal 滞留 `send_started`，且 `--recover` 不检查 `state["pages"][doc_id]["page_id"]`）
- **问题分析**：
  1. **跨集合父页静默劫持**：若工作区中另一父页（例如历史测试/归档集合）已存在由同一源文件构建的 `C01` 页面（标题与 `source_sha256` 相同），新父页初始化在 `C01` 发送阶段超时且**远端实际未建页**时，执行 `--recover` 会搜到旧集合的 `C01`；第 832 行发现它不在当前 `parent_body` 中，却未拒绝，反而落入第 835-841 行通过 `title_ok and source_sha in fetched.raw_text` 校验，将该无关旧页绑定进生产 `state.json`，后续 `sync` 推送将直接覆盖错误页面！
  2. **建页成功但立即回读失败**：`state["pages"][doc_id]["page_id"]` 已落盘，但 journal 仍为 `send_started`；`--recover` 不复用已保存的 `page_id`，而是强依赖可选工具 `notion-search`，若 `notion-search` 缺失或索引延迟则恢复失败。
  3. **远端确实未建页时死锁**：若超时发生在服务端落盘前且无同名旧页，`--recover` 查无结果后持续报错，无安全的人工确认续建手段。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F04_uncertain_create_recover_cross_collection_hijack_and_fetch_crash`。
- **修复建议**：子页恢复必须严格验证父子归属（仅接受归属 `collection_parent_id` 的子页，或将唯一 `page_marker` 嵌入子页）；恢复前优先复用 `state.json` 中已持久化的 `page_id`；为“人工确认远端未建页”提供显式审计恢复子命令。

---

### [HIGH] F05：`push` 异常状态无恢复命令；推送回读严格哈希比对不兼容 Notion URL 自动转链；Schema 校验失败提前污染 journal

- **代码位置**：
  - `tools/black9/sync.py:648, 945-946, 1272-1276`（`push_started`/`push_uncertain`/`push_verify_mismatch` 锁死 `sync`，且 `init --recover` 拒绝非 `create_page` 操作）
  - `tools/black9/sync.py:86-104, 1461-1480`（`canonicalize_markdown` 与推送回读 `verified_canon_sha != local_canon_sha` 严格相等校验）
  - `tools/black9/sync.py:1003-1020, 1066-1083, 1433-1446`（先写 `send_started`/`push_started` 后才在 `create_page`/`update_page` 内部校验 schema）
  - `tools/black9/test_black9.py:702-709`（开发者自身单测也被迫手写伪造 journal 记录解锁）
- **问题分析**：
  1. `init` 刻意分别保存 `base_local_canon_sha256` 与 `base_remote_canon_sha256` 以容忍 Notion Markdown 序列化差异；但 `push` 第 1464 行却强制要求 `verified_canon_sha == local_canon_sha`。由于构建器在**全部 13 个页面**均写入裸 `--source-url`（`https://www.notion.so/...`），Notion 回读时若自动转为 `[https://...](https://...)` 或转义 `\[`/`\|`，将导致 **每次推送都触发 `push_verify_mismatch` 并永久锁死 `sync.py`**。
  2. `sync.py` 没有任何 `sync --recover` 或 journal 核销命令，线上一旦触发推送超时或回读不一致即陷入僵局。
  3. `run_init` / `run_sync` 在调用 `_build_*_payload` 校验 schema 之前就写入 `send_started` / `push_started`，导致连请求都未发出时因 `SchemaValidationError` 也会锁死 journal。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F05_unrecoverable_push_journal_lock_and_notion_autolink_drift`。
- **修复建议**：将 schema 构造与校验前移至写入 journal 之前；对 Notion 常见无损序列化差异（自动链接、标点转义）做保守规范化或明文保真校验；提供 `sync --recover` 核对远端实际状态并安全解封。

---

### [HIGH] F06：`extract_notion_content_xml` 遇正文含 `<content/>` 会整页清空；`fetch_page` 忽略无 XML 包裹标记导致 `pull` 覆盖本地原文

- **代码位置**：
  - `tools/black9/consolidate.py:933-957`（第 942 行 `if "<content/>" in normalized` 优先于 `<content>...</content>` 提取）
  - `tools/black9/sync.py:497-506`（`extracted, _ = consolidate.extract_notion_content_xml(raw_text)` 丢弃 `had_xml_wrapper`）
  - `tools/black9/sync.py:1382-1410`（`pull` 无防空/防截断护栏直接覆盖本地文件）
- **问题分析**：
  1. 只要正文中任何位置出现字面量 `<content/>` 或 `<content />`，第 942 行立即返回 `("", True)`，将整页正文清空；在 `sync` 拉取时直接把本地 20KB+ 案例文件覆写为 `"\n"`。
  2. 当 `notion-fetch` 返回不含 `<content>...</content>` 的非 XML 告警或截断提示（且未设 `isError: true`）时，`fetch_page` 照单全收，`pull` 直接用告警文本覆盖本地完整文稿。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F06_xml_content_self_closing_wipe_and_unwrapped_pull_overwrite`。
- **修复建议**：优先匹配外层 `<page>...<content>...</content></page>`，仅当不存在闭合块且 `<content/>` 位于页外壳层级时视为空页；`sync.py` 要求 `had_xml_wrapper=True`，并对骤降为空页的 `pull` 拒绝自动覆盖。

---

### [HIGH] F07：视觉模糊/纯画面/无对白单元格被默认误标为“口播正文记录”

- **代码位置**：
  - `tools/black9/consolidate.py:48-74, 208-234`（`classify_original_cell`、`classification_label_zh`）
  - `tools/black9/consolidate.py:516, 536, 560, 620`（案例页与总索引统计标签）
- **问题分析**：
  - `_VISUAL_CUE_RE` 仅覆盖极窄关键词（如要求 `画面[：:]` 带冒号或特定括号前缀），其余所有未命中项一律归类为 `spoken_transcript` 并明确输出 `记录区分：口播正文记录（按原文保留，未独立复听核对）`。
  - 实测 `画面展示产品对比`（无冒号）、`[特写镜头，推近]`、`（纯动作演示，未说话）`、`【片头LOGO】` 全部被标成**口播正文记录**，直接违背“视觉模糊行不可假装为口播转录”的要求。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F07_visually_ambiguous_rows_mislabeled_spoken_transcript`。
- **修复建议**：将默认标签改为中性保守表述（如 `原文单元记录（未独立复听，暂未检出显式无对白标记）`），并对括号动作/镜头/视觉描述等模糊条目单独归为 `疑似画面/动作或待复听核验记录`。

---

### [MEDIUM] F08：`sync.py` 推送阶段未校验扁平 Markdown，可能推入破坏 GitHub Actions `safe_two_way` 的表格/嵌套块

- **代码位置**：`tools/black9/sync.py:1428-1432`（对比 `sync.py:742` `preflight_bundle_folder` 调用了 `validate_flat_markdown`）。
- **问题分析**：推送前仅检查单行 `< 1900` 字符，未调用 `consolidate.validate_flat_markdown`。若用户在本地添加表格、代码块或嵌套列表，`sync.py` 会直接推入 Notion，破坏与现有 GitHub Actions `safe_two_way` 引擎（拒绝表格/嵌套）的兼容前提。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F08_sync_push_allows_non_flat_markdown_breaking_safe_two_way`。

---

### [MEDIUM] F09：`consolidate.py` 解析器标题正则偏窄且表头/数据列数校验不一致

- **代码位置**：`tools/black9/consolidate.py:39-43`（`_APPENDIX_B_RE`、`_CASE_HEADING_RE`）、`tools/black9/consolidate.py:287-308`（`parse_case_table`）。
- **问题分析**：
  1. `_CASE_HEADING_RE` 拒绝 `## C10（标题）`（紧接中文左括号）与 `### C01｜标题`；`_APPENDIX_B_RE` 拒绝 `## 附录B` 或 `# 附录 B`。
  2. `parse_case_table` 第 288 行允许 `len(header_cells) >= 4`，第 304 行却强制 `len(cells) != 4` 报错；且单案若含多个 4 列表会静默拼接。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F09_parser_heading_rigidity_and_column_check_inconsistency`。

---

### [LOW] F10：发生同步冲突时 CLI 仍返回退出码 `0`；非零错误日志片段仅脱敏凭据未过滤可能回显的私密正文

- **代码位置**：`tools/black9/sync.py:1551, 1693-1694`；`tools/black9/sync.py:282`；`tools/black9/consolidate.py:1057`。
- **问题分析**：`sync.py sync` 检测到冲突并输出 `"status": "conflict"` 时仍以 `exit_code=0` 退出，不利于调用脚本判别；另 `minis-mcp-cli` 非零退出时截取前 300 字符 stderr/stdout 仅对密钥做正则脱敏，若子进程回显参数体可能带出部分私密正文。
- **复现验证**：`test_independent.py::Black9AuditDefectReproducers::test_repro_F10_conflict_cli_returns_zero_exit_code`。

---

## 三、独立测试执行记录

| 序号 | 命令 | 模式 | 结果 |
| :--- | :--- | :--- | :--- |
| 1 | `python3 -B -m py_compile <37aac5a>/tools/black9/consolidate.py <37aac5a>/tools/black9/sync.py <37aac5a>/tools/black9/test_black9.py` | 本地编译 | 3 通过 / 0 失败（exit 0） |
| 2 | `python3 -B -m unittest discover -s <37aac5a>/tools/black9 -p 'test_*.py' -v` | 开发者基线 Mock 单测 | 13 通过 / 0 失败（exit 0） |
| 3 | `python3 -B -m py_compile reviews/black9-20261127/test_independent.py` | 独立测试编译 | 1 通过 / 0 失败（exit 0） |
| 4 | `python3 -B -m unittest -v reviews/black9-20261127/test_independent.py` | 独立审计正向验证 + 缺陷复现 | 14 通过 / 0 失败（exit 0；含 4 正向验证 + 10 缺陷复现） |

## 四、给主控的后续整改优先级建议

1. **P0（上线前必改）**：修复 **F01**（`allow_async: false` 与 `async_task` 轮询/安全失败）、**F02**（补齐附录 A 108 行与独立补研究 C01–C11）、**F04**（子页嵌入唯一 marker + 严格父页校验 + 优先复用已存 `page_id`）、**F05**（schema 校验前置 + 推送回读容忍 Notion 无损转链/转义 + `sync --recover`）、**F06**（修复 `<content/>` 误判与无 XML 包裹拒拉取）。
2. **P1（强烈建议）**：修复 **F03**（全祖先链符号链接校验）、**F07**（中性默认标签，停止将模糊/画面行标为口播正文）。
3. **P2（稳健性加固）**：修复 **F08**（推送前校验扁平 Markdown）、**F09**（放宽标题兼容并统一列数校验）、**F10**（冲突返回非零退出码并收紧错误日志回显）。
4. **实测收口**：上述修复并复审通过后，再由主控在 Minis 环境使用真实私密源文件与真实 `minis-mcp-cli` 执行 `--dry-run` → 测试父页 `init` → 真实双向 `sync`（含 `noop`/`pull`/`push`/`conflict`）闭环验证。
