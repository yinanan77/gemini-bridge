# GPT 主控验收：首版必须返修（不是通过）

基线提交：4d2e3bfc6a0656b1e8c463b1a4ce2ada20e9f5ce。Minis 本地复跑编译和27个单元测试通过；真实只读接口调用已复现以下阻断，禁止只靠自测交付。

## P0：真实 CLI 协议错误（BrowserUseClient，约1214-1238行）
本机 `minis-browser-use --help` 实际支持：
```
minis-browser-use list_tabs
minis-browser-use new_tab --url URL
minis-browser-use navigate --tab-id ID --url URL
minis-browser-use execute_js --tab-id ID --script SCRIPT
minis-browser-use --json '{"action":"execute_js","tab_id":4,"script":"return {probe:true}"}'
```
首版却调用 tabs/open/eval，以及 navigate 后裸URL。这些都不是本机接口。已实际运行 BrowserUseClient().list_tabs()，返回 browser_nonzero_exit / Invalid browser_use input。

execute_js 的 SCRIPT 在 async 函数内运行，必须显式 `return` 才能取得返回值。已通过真实 CLI 运行 `(()=>({probe:"readonly-return-shape"}))()`，实际 data.text 为 `null\n tab_id: 4`。修正所有检查脚本和返回值解析，不得修改disabled或绕过校验。

## P0：网页地址和模型校验错误
真实项目地址是：
https://antigravity.google.com/remote?project=561adc82-06e6-4c8c-862f-3ac493b947c1
不是 /remote/projects/<id>。
模型按钮实际文本是 Gemini 4 Neon High；默认预期模型不应是 Pro，不能靠可见消息中包含Pro一词验证模型。应匹配真实模型按钮；项目验证应读取选择器或query项目ID，不能由提示词伪造的body文本满足。

## P0：真实标签输出解析错误（parse_tabs_from_envelope）
本机真实响应为JSON envelope，data包含 text / success / page_url；list_tabs 的 data.text 是：
```
Open tabs:
  Tab 2: CLI 故障排查 — https://antigravity.google/docs/cli/troubleshooting/
  Tab 4: Google Antigravity — https://antigravity.google.com/remote?project=561adc82-06e6-4c8c-862f-3ac493b947c1 *
```
已实测首版返回 tab_id='Tab' 而不是 '4'。请使用严格数字标签格式，支持 new_tab 返回文本中的 Opened new tab N at URL。拒绝错误/歧义而非猜测。
执行JS真实响应 data.text 可以是JSON字符串并在后面附 `\n  tab_id: N`；必须在受限格式下正确解析，不能eval。data.success=false也必须识别为失败。page_url是实际字段，不是只处理url。

## P1：真实DOM选择器和JS转义
实际网页：
- editor: [contenteditable=true][aria-label="Message input"]，role=combobox；
- model: button[data-testid="model-selector-trigger"] / button[aria-label^="Select model"]，按钮文本Gemini 4 Neon High；
- project: button[aria-label^="Select project, current:"]；
- sent user messages: [data-testid="user-message"] / [data-testid="user-message-content"]；
- actual assistant replies: [data-testid="agent-response"]；
- send: button[aria-label="Send message"] / button[data-testid="send-button"]；
- running: button[aria-label="Cancel (Ctrl+D)"]（独立于输入框里的文字）。
首版助手选择器不匹配真实网页。build_status_inspect_js 的Python字符串含真实 \x08 回退字符，主控实测 `'\x08' in build_status_inspect_js('probe')` 为True，原因是Python把正则\b当作转义，请修复并加入回归测试。

## P1：错误识别不能扫描用户任务文本作为阻断
Google前两轮真实错误是助手答复里的 `Verify your account to continue.`，不是现有regex的verify you are human。需覆盖这个真实错误，但只检查相关最新助手答复、明确错误横幅/官方账号URL，不把任务目标里提到sign in/rate limit/captcha等文字当真实阻断。旧助手回复不能让新任务被假判returned。提示词回显不能成为成功证据。

## 修复交付
在原workflow/gpt-gemini-v1分支定向修复并增加真实协议夹具回归；保留原task.json和全部失败记录，不改main，不另写框架。更新bootstrap/result.json说明本轮修复及实际测试数，提交推送。至少加入上述真实argv、tab文本、data.text JSON+tab后缀、page_url、JS无控制字符、真实DOM选择器、模型/项目不可由prompt伪造、用户消息中的错误词不误报、实际verify error阻断等回归。云端不能调用本机CLI的项目请如实标未实测，GPT会在真实Minis再次验收。不要声称只有mock就已接通。
