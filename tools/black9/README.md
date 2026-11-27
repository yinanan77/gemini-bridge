# Black9（黑桃9）文稿与分析汇总及保守同步工具

纯 Python 标准库工具集，用于在授权用户的本地 / Minis 环境中，将来源分析文档 `# 附录B` 下的 **11 个案例（`C01`–`C11`）共 335 条单元** 确定性整理为独立目录 `黑桃9｜文稿与分析汇总`，并通过现有 `minis-mcp-cli` 子进程适配器执行手工、保守的 Notion ↔ Obsidian 三方同步。

## 使用边界与真实状态说明

- **仅支持手工运行（非自动同步）**：本工具集需在 Minis 环境中手工触发，**不提供定时任务、后台服务或自动同步**；在另行完成与现有 GitHub Actions 的单独集成评审之前，不会自动运行。
- **Mock 测试与线上运行区分**：本分支中全部单元测试均基于本地 Mock `minis-mcp-cli` 完成，**尚未在真实 Notion 工作区执行线上联调或实测写入**；真实同步需由主控环境在 Minis 中使用真实 `minis-mcp-cli` 自行验证。
- **零凭据读取与零原稿破坏**：所有 Notion 访问严格经由 `minis-mcp-cli` 子进程并在运行时调用 `minis-mcp-cli tools Notion` 发现和校验 schema；绝不读取 OAuth/token 文件、`/var/minis` 或环境变量凭据，绝不修改原始分析文件或原始 Notion 页面，也不向日志输出私密正文或凭据。

## CLI 快速使用

### 1. 构建独立汇总目录（`consolidate.py build`）

```bash
python3 tools/black9/consolidate.py build \
  --source /path/to/source_analysis.md \
  --output /path/to/obsidian_vault_or_workspace \
  --source-url https://www.notion.so/<source-page-id>
```

主要保证：
- 按表格数据行计数（兼容 `C01–C09` 的 `1 · time`、`C10` 的 `A01／time`、`C11` 的 `1／time` 及数字/变体格式），发布前强校验必须为精确 **11 案 / `[23,22,22,22,37,37,30,26,16,49,51]` = 335 单元**，不匹配则在写盘前中止。
- 输出 `黑桃9｜文稿与分析汇总/`，包含：`00_index.md`、`01_C01.md`–`11_C11.md`、`90_source_reference.md` 及仅本地使用的 `manifest.json`（不写入 Notion）。
- 每篇案例页保留独立 `来源与核验说明`（注明未经独立复听原音视频核对）、`原文逐项`、`画面与无对白记录区分清单` 与 `分析逐项`；仅使用扁平标题与段落（无表格、代码块、分割线或嵌套块），每行严格 `< 1900` 字符。
- 不复制完整思源混合材料，不收录用户自有最终成稿；拒绝符号链接与非空/冲突目标覆盖，相同输入重复构建字节级幂等。

### 2. 可选抓取 Notion 页面原始证据（`consolidate.py fetch`）

```bash
python3 tools/black9/consolidate.py fetch \
  --notion-id <NOTION_PAGE_ID> \
  --output /path/to/extracted.md
```

通过 `minis-mcp-cli call Notion notion-fetch` 抓取页面，保存 `.raw.json` 原始证据及提取 `<content>...</content>` 后的文本（注意 Notion fetch 摘要未必包含完整内嵌表格）。

### 3. 运行时检查 Notion MCP Schema（`sync.py inspect`）

```bash
python3 tools/black9/sync.py inspect
```

仅输出 `notion-fetch`、`notion-create-pages`、`notion-update-page`（及可用的 `notion-search`）schema 定义，不包含任何密钥。

### 4. 初始化 Notion 集合页与 13 个子页（`sync.py init`）

先预演（不写入任何状态、日志或远端页面）：

```bash
python3 tools/black9/sync.py init \
  --folder "/path/to/黑桃9｜文稿与分析汇总" \
  --parent <EXISTING_PARENT_NOTION_ID> \
  --state /path/to/black9_sync_state.json \
  --dry-run
```

确认无误后执行初始化（在已有父页下新建 **1 个集合父页** + `00_index` + `C01–C11` + `90_source_reference` 共 13 个子页，自动排除 `manifest.json`）：

```bash
python3 tools/black9/sync.py init \
  --folder "/path/to/黑桃9｜文稿与分析汇总" \
  --parent <EXISTING_PARENT_NOTION_ID> \
  --state /path/to/black9_sync_state.json
```

**不确定写入保护**：每次创建前先落盘 `send_started` journal；若遇超时/异常响应绝不自动重试，重启默认拒绝继续创建。仅在显式指定 `--recover` 时，才按唯一 `task_marker` / 标题调用 `notion-search` 核对已建页面并续传剩余项，避免重复建页。

### 5. 保守三方同步与状态检查（`sync.py sync`）

```bash
# 只读状态检查（零写入）
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json --status

# 仅针对指定案例预演（零写入）
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json --only C01,C10 --dry-run

# 执行同步
python3 tools/black9/sync.py sync --state /path/to/black9_sync_state.json
```

三方规则：
- 分别记录本地与远端规范化基线（仅去除 Notion XML 外壳，保守归一化换行、`<br/>` 与空行，不丢弃任何原文字词）。
- 仅远端改动 → 原子拉取到本地；仅本地改动且远端符合受保护基线 → 整页更新并**强制回读校验**全文规范化哈希；远端格式变化但规范化全文与本地一致 → 仅更新基线。
- 双边并发修改 → **冲突保护**，双边原文均不覆盖，并将本地与远端版本同时保存至 `<state同级目录>/conflicts/<doc_id>/`。
- 本地缺失文件仅告警，绝不向 Notion 传播删除或归档。

## 现有 `.sync/state.json` 集成提案（仅供参考，未修改现有代码或状态）

以下为将本目录生成的扁平页面接入现有仓库 `.sync/state.json` 时的**建议结构示例**；本工具不会自动修改未知的既有状态文件或 GitHub Actions 配置。其中：
- 简单正文页（`00_index.md`、`01_C01.md`–`11_C11.md`）以 `kind: "source"` 注册，配置相对路径 `source`，并将 `hash` 设为 `SHA256(rendered remote string)`。
- `manually_managed_source_paths` 仅用于归档或只读参考路径（如原始综合分析归档与 `90_source_reference.md`）。

```json
{
  "proposal_only": true,
  "note": "仅供人工评审后接入现有同步系统；切勿未核对直接覆盖现有 .sync/state.json。",
  "pages": {
    "<NOTION_PAGE_ID_00_INDEX>": {
      "kind": "source",
      "source": "黑桃9｜文稿与分析汇总/00_index.md",
      "hash": "<SHA256_OF_RENDERED_REMOTE_STRING>"
    },
    "<NOTION_PAGE_ID_C01>": {
      "kind": "source",
      "source": "黑桃9｜文稿与分析汇总/01_C01.md",
      "hash": "<SHA256_OF_RENDERED_REMOTE_STRING>"
    },
    "<NOTION_PAGE_ID_C10>": {
      "kind": "source",
      "source": "黑桃9｜文稿与分析汇总/10_C10.md",
      "hash": "<SHA256_OF_RENDERED_REMOTE_STRING>"
    },
    "<NOTION_PAGE_ID_C11>": {
      "kind": "source",
      "source": "黑桃9｜文稿与分析汇总/11_C11.md",
      "hash": "<SHA256_OF_RENDERED_REMOTE_STRING>"
    }
  },
  "manually_managed_source_paths": [
    "黑桃9｜文稿与分析汇总/90_source_reference.md",
    "<ARCHIVED_ORIGINAL_SOURCE_ANALYSIS_RELATIVE_PATH>"
  ]
}
```

## 运行本地 Mock 单元测试

```bash
python3 -B -m py_compile tools/black9/consolidate.py tools/black9/sync.py tools/black9/test_black9.py
python3 -B -m unittest discover -s tools/black9 -p 'test_*.py' -v
```
