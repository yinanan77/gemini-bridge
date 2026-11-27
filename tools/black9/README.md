# Black9（黑桃9）文稿与分析汇总及双向同步接入工具

纯 Python 标准库工具集，用于在授权用户的本地 / Minis 环境中，将黑桃9来源分析文档中的 **11 个案例（`C01`–`C11`）**：
- `# 附录A` 全部 **108 组** 7 列语义分组分析（`groupID / mechanisms / read-before / change / position/reason / next-expectation / deletion-loss`）
- `# 附录B` 全部 **335 条** 逐单元原文与分析（`[23,22,22,22,37,37,30,26,16,49,51]`）
- 可选 `--supplement` 中 `## 二、黑桃11版本逐段补研究` 下的 **44 条**（`11 × 4`）黑桃9专属补研究条目（严格止于 `## 三、思远11版本逐段补研究` 之前）

确定性整理为独立目录 `黑桃9｜文稿与分析汇总`（扁平 H1–H3 标题与段落、无空行、每行 `< 1900` 字符），并通过现有 `minis-mcp-cli` 完成 Notion 初始化、保守三方同步，以及向现有 `yinanan77/E-Made` GitHub Actions（`scripts/notion_sync.py`，已有 `*/30` 定时任务）注册所需的安全合并 payload 准备与本地安装。

## 使用边界与真实状态说明

- **复用既有 GitHub Actions 双向同步，不新建/不改动定时任务与工作流**：本工具集**绝不创建或编辑任何 workflow、schedule、后台服务或 `scripts/notion_sync.py`**；仅通过 `deploy.py prepare` 生成审阅用 `push_files_payload.json`，由主控显式执行发布。
- **不自动发布、不自动覆盖本地旧同步元数据**：`deploy.py prepare` 只读远端并导出合并产物与备份；`deploy.py install` 默认仅复制新建黑桃9目录文件，拒绝覆盖非字节一致文件，绝不自动覆盖本地 `.sync/state.json` 或 `sync-config.json`。
- **保守区分，绝不把未复听文字定性为已核验口播**：原文单元格保守分为 `source_text_unverified`（已存文字来源记录）、`visual_or_non_spoken`（如 `表情停顿，无底部解释性字幕`、`无解释性字幕的成品尾留`、`无对白`、`[画面：...]`）与 `mixed_visual_and_text`（如 `反应近景；小字“这得挨不少电”；随后“哈哈”`）；不输出 `spoken_count` 数值口播定性，不编造“纯音乐/无声”等未经复听断言。
- **Mock 测试与线上运行区分**：本分支中全部单元测试均基于本地 Mock `minis-mcp-cli`（模拟 Notion 与 GitHub MCP 及 101 条既有 `.sync/state.json` 记录）完成；真实 Notion 写入与 GitHub Actions 联调由主控环境负责验证。
- **零凭据读取与零原稿破坏**：所有 Notion 与 GitHub 访问均经由 `minis-mcp-cli` 子进程与运行时 schema 发现；绝不读取 OAuth/token 文件、`/var/minis` 或环境变量凭据，绝不修改原始分析文件或既有 Notion/本地页面，日志中不输出私密正文或凭据。

## CLI 快速使用

### 1. 构建独立汇总目录（`consolidate.py build`）

仅使用主分析文件（含 `# 附录A` 108 组与 `# 附录B` 335 单元）：

```bash
python3 tools/black9/consolidate.py build \
  --source /path/to/source_analysis.md \
  --output /path/to/staging_or_workspace \
  --vault /path/to/E-Made \
  --source-url https://www.notion.so/<source-page-id>
```

附加可选的 22 版本 Notion 补研究提取正文（自动仅截取黑桃9 `11 × 4 = 44` 条，严格排除思远 `S01`–`S11` 与用户最终成稿）：

```bash
python3 tools/black9/consolidate.py build \
  --source /path/to/source_analysis.md \
  --output /path/to/staging_or_workspace \
  --vault /path/to/E-Made \
  --source-url https://www.notion.so/<source-page-id> \
  --supplement /path/to/notion_22_body.md \
  --supplement-url https://www.notion.so/<supplement-page-id>
```

（`--vault-dir` 与 `--vault` 等价保留向后兼容。）

每篇案例页（`01_C01.md`–`11_C11.md`）按固定顺序输出扁平 H1–H3 标题与段落（构建期已省略空行，兼容现有 `scripts/notion_sync.py` 简单渲染器）：
1. `## 来源与核验说明`（含主来源及可选补研究的相对路径、SHA-256、Notion URL 与非重新听审说明）
2. `## 文稿连读（依据已存文字，非重新听审）`（按原序连贯输出已存文字来源记录，**仅排除**显式画面/混合记录，不补写过渡词或标点）
3. `## 原文逐项`（无损保留全部 335 条原始单元与保守分类说明）
4. `## 显式画面与混合记录清单`（集中列出所有被排除出文稿连读的显式画面/混合单元，零丢失）
5. `## 语义分组分析（附录A）`（无损保留本案对应附录A 全部 7 列分组分析，合计 108 组）
6. `## 分析逐项`（无损保留本案附录B 全部逐项分析与语义分组）
7. `## 逐段补研究（黑桃9补充材料）`（提供 `--supplement` 时输出，原样保留 4 条编号分析、`<br>` 与富文本标记，不含思远/用户成稿）

### 2. 可选抓取 Notion 页面正文与原始证据（`consolidate.py fetch`）

```bash
python3 tools/black9/consolidate.py fetch \
  --notion-id <NOTION_PAGE_ID> \
  --output /path/to/extracted.md
```

通过 `minis-mcp-cli call Notion notion-fetch` 抓取页面，保存 `.raw.json` 原始证据及提取 `<content>...</content>` 后的正文。

### 3. 运行时检查 Notion / GitHub MCP Schema

```bash
python3 tools/black9/sync.py inspect
python3 tools/black9/deploy.py inspect
```

仅输出相关工具 schema 定义（Notion: `notion-fetch` / `notion-create-pages` / `notion-update-page` / `notion-search`；GitHub: `get_file_contents` / `push_files`），不包含任何密钥。

### 4. 初始化 Notion 集合父页与 13 个子页（`sync.py init`）

先预演（零写入）：

```bash
python3 tools/black9/sync.py init \
  --folder "/path/to/staging/黑桃9｜文稿与分析汇总" \
  --parent <EXISTING_PARENT_NOTION_ID> \
  --state /path/to/black9_sync_state.json \
  --dry-run
```

确认无误后初始化（在已有父页下新建 1 个集合父页 + `00_index`、`01_C01`–`11_C11`、`90_source_reference` 共 13 个子页，自动排除 `manifest.json`；任务标识仅写入集合父页及本地 journal，**绝不向 13 个子页正文注入未匹配的隐藏标记**）：

```bash
python3 tools/black9/sync.py init \
  --folder "/path/to/staging/黑桃9｜文稿与分析汇总" \
  --parent <EXISTING_PARENT_NOTION_ID> \
  --state /path/to/black9_sync_state.json
```

若遇超时或异常响应，重启默认拒绝继续创建；仅在显式指定 `--recover` 时才按唯一 `task_marker` / 标题调用 `notion-search` 核对并续传剩余项，避免重复建页。

### 5. 准备现有 GitHub Actions 双向同步注册 Payload（`deploy.py prepare`）

```bash
python3 tools/black9/deploy.py prepare \
  --bundle "/path/to/staging/黑桃9｜文稿与分析汇总" \
  --sync-state /path/to/black9_sync_state.json \
  --vault /path/to/E-Made \
  --out /path/to/deploy_out
```

默认目标仓库为 `yinanan77/E-Made` 分支 `main`（可通过 `--owner` / `--repo` / `--branch` / `--folder-relpath` / `--commit-message` 覆写）。执行流程：
1. 校验 bundle（11 案 / 335 单元 / 108 附录A分组 / 可选 44 补研究 / 无空行扁平结构）及 `sync.py init` 状态无未决 journal。
2. 逐页调用 Notion MCP `notion-fetch`，**强校验 13 个远端页面与本地全文规范化内容完全一致**，任意偏差即中止。
3. 通过 GitHub MCP `get_file_contents` 拉取最新 `.sync/state.json` 与 `sync-config.json`，先将远端与本地已有元数据原样备份至 `--out/backups/`。
4. **保守增量合并**：完整保留 `.sync/state.json` 中全部既有字段与记录（如全部 101 条既有项）以及 `sync-config.json` 中全部既有配置、`canonical_page_ids` 与 `manually_managed_source_paths`；**仅新增**本目录 13 个 Markdown 页面（含 `00_index.md` 与 `90_source_reference.md`，不含 `manifest.json`、无关页面或旧原稿）的 `source::<rel_path>` 映射与基线哈希（与本地扁平字节一致）。
5. 导出 `--out/push_files_payload.json`（含 `owner`、`repo`、`branch: "main"`、`message` 及仅包含新建目录 14 文件 + `.sync/state.json` + `sync-config.json` 共 16 项的 `files` 数组）、`merged_state.json`、`merged_sync-config.json` 与 `prepare_report.json`；**绝不自动调用 `push_files`**，由主控审阅后显式执行。

### 6. 安装新建黑桃9目录到本地 Vault（`deploy.py install`）

```bash
# 先预演（零写入）
python3 tools/black9/deploy.py install \
  --bundle "/path/to/staging/黑桃9｜文稿与分析汇总" \
  --vault /path/to/E-Made \
  --out /path/to/deploy_out \
  --dry-run

# 仅复制新建黑桃9目录文件到本地 Vault（保护所有既有页面与原稿，不自动覆盖旧同步元数据）
python3 tools/black9/deploy.py install \
  --bundle "/path/to/staging/黑桃9｜文稿与分析汇总" \
  --vault /path/to/E-Made \
  --out /path/to/deploy_out
```

默认仅复制新建目录的 14 个文件（13 个 `.md` + `manifest.json`），遇到非字节一致文件或多余文件立即中止；若提供 `--out` 则只读备份本地既有 `.sync/state.json` 与 `sync-config.json`，且**不自动覆盖**本地旧同步元数据（正常情况下应在主控提交 `push_files_payload.json` 后由 `git pull` 同步元数据；仅当显式传入 `--install-metadata-from <PREPARED_OUT> --out <BACKUP_DIR>` 时才在完成备份后写入合并元数据）。

### 7. 手工保守三方同步与状态检查（`sync.py sync`）

```bash
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json --status
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json --only C01,C10 --dry-run
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json
```

支持独立本地/远端规范化基线（保守忽略空行与 `<br/>` 序列化差异，不丢弃原文字词）、推送后强回读校验、双边冲突保存至 `conflicts/` 不互相覆盖，且绝不向 Notion 传播本地删除。

## 运行本地 Mock 单元测试

```bash
python3 -B -m py_compile tools/black9/consolidate.py tools/black9/sync.py tools/black9/deploy.py tools/black9/test_black9.py
python3 -B -m unittest discover -s tools/black9 -p 'test_*.py' -v
```
