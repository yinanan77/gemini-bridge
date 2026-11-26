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

契约必须包含：`task_id`、`attempt`（正整数，非布尔值）、`goal`、`base_branch`、`allowed_write_paths`（显式相对文件路径，拒绝绝对路径/`..`/`.git`/`.github`/workflow/secrets/`jobs/` 及重复项）、`acceptance`（非空列表）。仓库固定为 `yinanan77/gemini-bridge`，任务分支自动推导为 `gpt-gemini/jobs/<task_id>`。

### 2. 提交任务（`submit`）

```bash
python3 bridge/minis_gemini_bridge.py submit \
  --contract bridge/task.example.json \
  --state-dir .bridge-state
```

如需临时指定当前官方标签页，可附加严格数字 `--tab-id <数字ID>`（如 `--tab-id 4`，仅本次生效，不写入日志）。

`submit` 执行顺序：
1. 仅在 `attempt=1` 时从 `base_branch` 创建隔离分支 `gpt-gemini/jobs/<task_id>`；写入不可变快照 `jobs/<task_id>/attempt-<NN>/task.json`（若远端已存在不同 `contract_sha256` 则直接失败，绝不覆盖）。
2. 打开/导航官方项目页 `https://antigravity.google.com/remote?project=561adc82-06e6-4c8c-862f-3ac493b947c1` 新建对话。
3. 浏览器调用 1：聚焦输入框并执行 `execCommand('selectAll')`。
4. 浏览器调用 2：通过 `ClipboardEvent('paste')` + `DataTransfer('text/plain')` 粘贴完整提示词。
5. 浏览器调用 3（独立只读检查）：校验完整草稿与预期提示词逐字一致、URL 查询参数/项目选择器与模型按钮（默认 `Gemini 4 Neon High`，可用 `--expected-model` 覆盖）匹配、无阻塞提示且发送按钮非禁用。
6. **点击发送前**先原子写入本地日志 `state=submit_uncertain`（防超时重发），随后点击未禁用发送按钮并做一次只读确认。

### 3. 有界检查状态（`status`）

由 GPT 自行决定等待间隔（例如在 Minis shell 中暂停一段时间后），单次调用：

```bash
python3 bridge/minis_gemini_bridge.py status \
  --contract bridge/task.example.json \
  --state-dir .bridge-state
```

输出分类（始终 `"accepted": false`）：
- `running`（退出码 `0`）：官方页面仍在运行；
- `returned`（退出码 `0`）：官方页面已返回非回显的助手回复（**不代表已验收**，下一步执行 `collect`）；
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
- `jobs/<task_id>/attempt-<NN>/task.json` 未被篡改且 `contract_sha256` 一致；
- `jobs/<task_id>/attempt-<NN>/result.json` 存在且 `task_id`/`attempt`/`contract_sha256`/`status`/`summary`/`artifacts`/`tests` 合法；
- 所有声明的 `artifacts` 均在 `allowed_write_paths` 白名单内且具有真实非空内容。

校验通过后产物保存在 `<state-dir>/collected/<task_id>/attempt-<NN>/`，返回 `status="collected_not_accepted"`、`accepted=false`、`head_sha`、各文件 SHA-256 及本地路径。

### 5. GPT 独立审查与本地测试

`collect` 绝不会自动执行下载的脚本或 `result.json` 中的命令字符串。GPT 必须独立审查回收文件并在本地执行编译与测试，例如：

```bash
python3 -m py_compile .bridge-state/collected/example-text-stats-v1/attempt-01/examples/text_stats.py
python3 -m unittest discover -s .bridge-state/collected/example-text-stats-v1/attempt-01/examples -p "test_*.py"
```

### 6. 验收或发起修订（`attempt + 1`）

- **验收**：仅由 GPT 在独立测试全部通过后决定接受，后续是否合并由人工/既定流程处理。
- **修订**：不要覆盖 `attempt-01`。新建 `attempt: 2` 的契约文件（保持相同 `task_id`，说明修订目标），复用同一 `--state-dir` 执行 `validate` -> `submit`：
  - 工具会校验本地日志与远端任务分支、以及 `attempt-01/task.json` 均完好存在；
  - 新增不可变快照 `jobs/<task_id>/attempt-02/task.json`，保留之前所有 attempt 的状态、错误与结果哈希。

---

## App 中断 / 超时后的恢复（防止重复提交）

iOS Minis 挂起或网络中断可能导致 `submit` 在点击发送后超时，此时本地日志 `<state-dir>/jobs/<task_id>.json` 已在点击前落盘为 `state: "submit_uncertain"` 且 `send_initiated: true`。

安全恢复步骤：
1. **不要删除本地日志**，也不要把该契约复制到新的状态目录重跑。
2. 先运行 `status` 或使用**同一契约和同一 `--state-dir`** 再次运行 `submit`：
   - 工具会优先通过 GitHub 检查 `jobs/<task_id>/attempt-<NN>/result.json` 是否已生成；
   - 再只读检查官方对话页是否已出现该次尝试的唯一 `task_marker`；
   - 若确认已发出，日志自动核销为 `submitted` 并返回 `reconciled_already_submitted`（`sent: false`），**绝不二次发送**；
   - 若仍未看到 `task_marker`，`submit` 会返回退出码 `3`（`uncertain_no_double_submit`，`sent: false`），拒绝自动重发。
3. 遇到退出码 `3` 时，由人工或 GPT 在官方页面确认是否确实未发送：
   - 若确认已发送，等待完成并用 `status`/`collect` 接续；
   - 若确认未发送且需重新派发，请推进到 `attempt + 1`（保留上一轮不确定记录），避免同一 attempt 产生歧义。
