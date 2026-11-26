# Minis GPT -> Gemini Pro Web Agent -> GitHub 协作工作流

本工具（`bridge/minis_gemini_bridge.py`）运行在 Minis（iOS Alpine aarch64）环境，仅依赖 Python 3 标准库，通过现有已认证的 `minis-browser-use` 和 `minis-mcp-cli`（MCP 服务名 `GitHub`）在固定仓库 `yinanan77/gemini-bridge` 上实现 fail-closed（失败即关闭）的任务派发、状态核查与产物回收。

## 重要说明与边界限制

- **调用方式**：通过官方 Antigravity 网页端 UI（`https://antigravity.google.com/remote?project=561adc82-06e6-4c8c-862f-3ac493b947c1`）与现有 Google Pro 登录态协作。
- **明确不宣称**：
  - 不是原生模型 Provider/API 集成；
  - 不提供自动 GitHub Issue 触发；
  - 不保证 iOS App 后台/挂起期间持续运行；
  - 不宣称零成本（消耗现有 Pro 订阅包含额度，不开启付费加购或账单变更；额度不可用即停并报告）。
- **人工介入前提**：首次或偶发会话失效、账号风控、人机验证（如 `Verify your account to continue.` / CAPTCHA / 2FA）时，需人工在官方页面完成验证；工具不会绕过验证、不会提取 Cookie/Token、不会解除禁用按钮、不会调用未公开接口。
- **有界单次执行**：`validate` / `submit` / `status` / `collect` 均执行有界动作后立即退出，内置**无轮询循环、无 `sleep`**；如需等待，由 GPT 在 Minis shell 层显式间隔后再次调用 `status`。
- **绝不持久保存 `tab_id`**：本地日志只保存官方 `conversation_url`、各 `attempt` 状态、错误与哈希；每次调用动态发现官方标签页或按 `--tab-id <数字ID>` 临时指定。
- **状态目录与并发安全**：`--state-dir` 本身及其下的 `jobs/`、`collected/` 路径严禁使用符号链接（拒绝跟随 symlink 写入外部路径），临时文件使用随机名 + `O_EXCL|O_NOFOLLOW` + `fsync` 原子替换，并对同一 `task_id` 使用非阻塞文件锁（`fcntl.flock`）防止并发写冲突。

---

## GPT 标准操作步骤

以下示例假设状态目录为 `.bridge-state`，契约文件为 `bridge/task.example.json`。

### 1. 编写并本地校验任务契约（`validate` + `--dry-run`）

```bash
# 纯本地校验（不调用 GitHub/浏览器，不写状态目录）
python3 bridge/minis_gemini_bridge.py validate \
  --contract bridge/task.example.json

# 提交前干跑预览（绝不修改 GitHub、不发送消息、不写日志）
python3 bridge/minis_gemini_bridge.py submit \
  --contract bridge/task.example.json \
  --state-dir .bridge-state \
  --dry-run
```

契约必须包含：`task_id`、`attempt`（正整数，非布尔值）、`goal`、`base_branch`、`allowed_write_paths`（显式相对文件路径，拒绝绝对路径/`..`/`.git`/`.github`/workflow/secrets/`jobs/`（大小写不敏感）、重复项及文件/目录前缀冲突）、`acceptance`（非空列表），且拒绝含重复 JSON key 的契约/快照。仓库固定为 `yinanan77/gemini-bridge`，任务分支自动推导为 `gpt-gemini/jobs/<task_id>`。

### 2. 提交任务（`submit`）

```bash
python3 bridge/minis_gemini_bridge.py submit \
  --contract bridge/task.example.json \
  --state-dir .bridge-state
```

如需临时指定当前官方标签页，可附加严格数字 `--tab-id <数字ID>`（如 `--tab-id 4`，仅本次生效，不写入日志）。

`submit` 执行顺序：
1. 仅在 `attempt=1` 时校验 `base_branch` 上不存在同路径快照并从 `base_branch` 创建隔离分支 `gpt-gemini/jobs/<task_id>`；`attempt > 1` 时校验历史所有 `attempt-01..attempt-(N-1)/task.json` 均完整未篡改；写入不可变快照 `jobs/<task_id>/attempt-<NN>/task.json`（若远端已存在不同 `contract_sha256` 则直接失败，绝不覆盖；若远端已存在同契约快照且本地为全新 `--state-dir`，则仅执行只读核销，绝不盲目重发）。
2. 打开/导航官方项目页 `https://antigravity.google.com/remote?project=561adc82-06e6-4c8c-862f-3ac493b947c1` 新建对话。
3. 浏览器调用 1：聚焦输入框并执行 `execCommand('selectAll')`。
4. 浏览器调用 2：通过 `ClipboardEvent('paste')` + `DataTransfer('text/plain')` 粘贴完整提示词。
5. 浏览器调用 3（独立只读检查）：校验完整草稿与预期提示词逐字一致、URL 查询参数/项目选择器与模型按钮（默认 `Gemini 4 Neon High`，可用 `--expected-model` 覆盖）匹配、无阻塞提示且发送按钮非禁用（若页面已出现当前 `task_marker` 则直接只读核销为已提交，不再点击）。
6. **点击发送前**先原子写入本地日志 `state=submit_uncertain`（防超时重发），随后点击未禁用发送按钮并做一次只读确认；**仅当确认未发生点击**（明确 `send_button_not_found` 或 `send_button_disabled`）才会清除 `send_initiated`，若点击返回 `null`/格式异常/未知原因或未观测到当前 `task_marker`，则保持 `submit_uncertain`（退出码 `3`）。

### 3. 有界检查状态（`status`）

由 GPT 自行决定等待间隔（例如在 Minis shell 中暂停一段时间后），单次调用：

```bash
python3 bridge/minis_gemini_bridge.py status \
  --contract bridge/task.example.json \
  --state-dir .bridge-state
```

`status` 会严格校对 `--contract` 与 `--attempt`（不匹配即拒绝），绝不回退到无关对话标签页，且仅在当前页面确认为含目标 `task_marker` 的精确对话时才会更新 `conversation_url`。

输出分类（始终 `"accepted": false`）：
- `running`（退出码 `0`）：官方页面仍在运行；
- `returned`（退出码 `0`）：官方页面已返回精确匹配 `task_marker` 且非回显的助手回复（**不代表已验收**，下一步执行 `collect`）；
- `blocked`（退出码 `2`）：出现身份/人机验证、配额/账单限制、权限错误或页面报错（`running=false` 且有错误一律判为 `blocked`）。

### 4. 回收并校验远端产物（`collect`）

```bash
# 可选：先 dry-run 预览将要读取的远端路径
python3 bridge/minis_gemini_bridge.py collect \
  --contract bridge/task.example.json \
  --state-dir .bridge-state \
  --dry-run

# 实际回收（只读 GitHub MCP，不自动执行任何下载代码或测试命令）
python3 bridge/minis_gemini_bridge.py collect \
  --contract bridge/task.example.json \
  --state-dir .bridge-state
```

`collect` 通过 GitHub MCP 获取 `gpt-gemini/jobs/<task_id>` 的**权威分支 HEAD SHA**，固定在该 commit 拉取并校验：
- 历史所有 `attempt-01..attempt-(N-1)/task.json` 与当前 `attempt-<NN>/task.json` 均未被篡改且 `contract_sha256`/路径元数据一致；
- `jobs/<task_id>/attempt-<NN>/result.json` 存在且 `task_id`/`attempt`/`contract_sha256`/`status`/`summary`/`artifacts`/`tests` 合法，拒绝重复键与自指 commit SHA 字段（如 `commit`/`commit_sha`/`head_sha`/`sha`），并对 `tests` 递归脱敏与限深限长；
- 所有声明的 `artifacts` 均在 `allowed_write_paths` 白名单内且具有真实非空内容；
- 分支写入范围审计：若 GitHub MCP 支持 `get_commit` 返回变更文件列表，会校验任务分支上相对基线的所有变更文件均在允许路径或对应 `attempt` 快照路径内（越界直接失败，`branch_scope_status="verified"`）；若底层 MCP 不支持变更文件列表，则显式返回 `branch_scope_status="scope_unverified"` 与 `evidence.branch_write_scope_verified=false`，不伪造“已验证”。

校验通过后产物以防符号链接方式原子保存在 `<state-dir>/collected/<task_id>/attempt-<NN>/`，返回 `status="collected_not_accepted"`、`accepted=false`、`head_sha`、`branch_scope_status`、各文件 SHA-256 及本地路径。

### 5. GPT 独立审查与本地测试

`collect` 绝不会自动执行下载的脚本或 `result.json` 中的命令字符串。GPT 必须独立审查回收文件并在本地执行编译与测试，例如：

```bash
python3 -m py_compile .bridge-state/collected/example-text-stats-v1/attempt-01/examples/text_stats.py
python3 -m unittest discover -s .bridge-state/collected/example-text-stats-v1/attempt-01/examples -p "test_*.py"
```

### 6. 验收或发起修订（`attempt + 1`）

- **验收**：仅由 GPT 在独立测试全部通过后决定接受，后续是否合并由人工/既定流程处理。
- **修订**：不要覆盖 `attempt-01`。新建 `attempt: 2` 的契约文件（保持相同 `task_id`，说明修订目标），复用同一 `--state-dir` 执行 `validate` -> `submit`：
  - 工具会校验本地日志与远端任务分支、以及所有历史 `attempt-01..attempt-(N-1)/task.json` 均完好存在且哈希一致；
  - 新增不可变快照 `jobs/<task_id>/attempt-02/task.json`，保留之前所有 attempt 的状态、错误与结果哈希。

---

## App 中断 / 超时后的恢复（防止重复提交）

iOS Minis 挂起或网络中断可能导致 `submit` 在点击发送后超时，或点击后脚本返回 `null`/未知状态，此时本地日志 `<state-dir>/jobs/<task_id>.json` 保持为 `state: "submit_uncertain"` 且 `send_initiated: true`。

安全恢复步骤：
1. **不要删除本地日志**，也不要把该契约复制到新的状态目录重跑（即使更换 `--state-dir`，若远端已存在同契约快照，工具也会进入只读核销而非盲目重发）。
2. 先运行 `status` 或使用**同一契约和同一 `--state-dir`** 再次运行 `submit`：
   - 工具会优先通过 GitHub 检查 `jobs/<task_id>/attempt-<NN>/result.json` 是否已生成；
   - 再跨官方标签页只读检查是否已出现该次尝试的唯一 `task_marker`；
   - 若确认已发出，日志自动核销为 `submitted` 并返回 `reconciled_already_submitted`（`sent: false`），**绝不二次发送**；
   - 若仍未看到 `task_marker`，`submit` 会返回退出码 `3`（`uncertain_no_double_submit`，`sent: false`），拒绝自动重发。
3. 遇到退出码 `3` 时，由人工或 GPT 在官方页面确认是否确实未发送：
   - 若确认已发送，等待完成并用 `status`/`collect` 接续；
   - 若确认未发送且需重新派发，请推进到 `attempt + 1`（保留上一轮不确定记录），避免同一 attempt 产生歧义。
