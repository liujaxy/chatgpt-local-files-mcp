# ChatGPT Local Files MCP

**让 ChatGPT 网页端读取你的本地项目资料**

[English](README.md) | 简体中文

让支持 MCP 插件的网页端模型，按需只读访问你主动选择的 Windows 本机项目中的 Markdown 和图片，并将完整分析报告回传到本机，由你在 Codex 中发出读取和后续处理指令。

Read-only access to selected local Markdown/images, with append-only report delivery to a fixed project directory. No arbitrary file edits, command execution or model API calls.

## 工作方式

读取：网页模型 → OpenAI Secure MCP Tunnel → 本机 MCP 服务 → 用户选定的一个项目目录。

回传：网页模型 → submit_web_result → 所选项目 outputs/web-results/ → 用户让 Codex 读取并继续处理。

程序不调用模型 API，也不自动上传整个项目。模型调用工具时，请求的正文和图片会通过隧道传给远端服务；本机运行不代表内容永不离开电脑。

## 支持范围

| 项目 | 当前能力 |
| --- | --- |
| 平台 | Windows；当前使用 Python 3.14、Windows amd64 隧道客户端 |
| 文本 | UTF-8 `.md`、`.markdown`，分段读取、字面搜索 |
| 图片 | PNG、JPEG、WebP、TIFF；裁剪、缩放、多页 TIFF |
| Word / PDF | **不支持直接读取**，需自行转换为 Markdown 或图片 |
| 网页模型 | 用户报告 Pro 和 sol 可连接；不限定 Pro，不承诺所有模型或账号可用 |
| 文件修改 | 源文件只读；仅允许在固定结果目录新增报告及回执，由用户让 Codex 继续处理 |
| Codex 聊天 | 可选，只读取用户指定且属于同一项目的已保存用户 / 助手文字 |
| 本机技能 | 可选，个人 skills 和两套学术写作插件的说明、Markdown 参考资料及触发规则 |

MCP 是连接工具的协议。现有图形界面、凭据保护和隧道配置面向 Windows 与 OpenAI Secure MCP Tunnel，并非所有网页模型平台的通用安装器。

## 安装

需要 Python 3.14（含 Tkinter）、[uv](https://docs.astral.sh/uv/getting-started/installation/)，以及具有相应隧道与网页插件权限的账号。在项目根目录打开 PowerShell：

```powershell
uv venv --python 3.14 .venv
uv pip sync --python .venv/Scripts/python.exe requirements.lock.txt
```

仓库不附带可执行程序。下载[官方 v0.0.14 Windows amd64 ZIP](https://github.com/openai/tunnel-client/releases/download/v0.0.14/tunnel-client-v0.0.14-windows-amd64.zip)，计算文件校验值：

```powershell
Get-FileHash -Algorithm SHA256 '<下载的 ZIP 完整路径>'
```

预期 SHA256 为 `784ab8da7b5a88f0109f1fd8aaf0a1c86067430b896dddf307ef7e3cc49fa1a5`，记录见 `vendor/tunnel-release.json` 与 `vendor/SHA256SUMS.txt`。一致后解压，将 `tunnel-client.exe` 放到 `vendor/tunnel/tunnel-client.exe`。保留官方发行包许可证和通知。当前程序不使用 Cloudflare 运行时。

## 首次连接

1. 按[官方隧道说明](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels)创建隧道并关联 ChatGPT 工作区，创建具有 Tunnels Read + Use 权限的运行密钥。创建隧道另需管理权限。
2. 双击 `启动.cmd`，选择“账号 A”或“账号 B”。它们只是两份独立配置槽位，不是共享账号；只配置一个也可以。
3. 在本机界面录入自己的隧道 ID 和运行密钥。不要提交到仓库、Issue 或聊天。
4. 首次选择“使用测试项目”，启动连接并确认路径。
5. 在对应网页账号中创建私有插件，Connection 选择 Tunnel，再选择自己的隧道。入口与权限以账号实际功能为准。
6. 新建聊天，选择插件及支持工具调用的模型，请它读取测试说明并实际查看引用图片。成功后停止连接，再选择自己的资料项目。

使用期间保持控制窗口运行。停止或关闭窗口会撤销访问并结束连接。切换项目使用新聊天；停止不会删除远端聊天里已经读取的内容。

当前无法确认的内容：不同套餐、地区及工作区的可用性、隧道额外收费，以及未测试模型的兼容性。请按官方说明和目标账号实际验证，不承诺免费。

## 工具

| 工具 | 用途 |
| --- | --- |
| `project_info` | 项目、版本、访问标识，读取根目录 AGENTS.md 和 PROJECT_STATUS.md 的开头 |
| `list_files` | 分页浏览支持文件和子目录 |
| `search_markdown` | 搜索 Markdown 正文及支持文件名称 |
| `read_markdown` | 按行读取并解析项目内图片引用 |
| `read_image` | 返回图片像素，支持页码、裁剪和尺寸限制 |
| `read_codex_context` | 分页读取本机指定的 Codex 聊天可见文字历史 |
| `list_skills` | 已启用且在开放范围内的技能及触发政策 |
| `list_skill_files` | 浏览单个注册技能的 Markdown 参考文件 |
| `read_skill_file` | 读取技能正文或其 Markdown 参考文件 |
| `submit_web_result` | 把完整 Markdown 报告新增保存到所选项目的固定结果目录，返回回执 |
| `list_web_results` | 按接收时间列出本项目的回传记录 |
| `read_web_result` | 读取指定或最新回传，核对内容哈希，支持分页 |

默认指引帮助模型自主查找资料，仍需模型实际调用工具，不保证每次完整遵循。详见[使用说明](使用说明.md)和[隐私说明](SECURITY.md)。

1.3.0 新增 3 个结果工具，共 12 个。重开本机控制窗口，在对应账号隧道运行时刷新原插件工具目录并新建聊天；账号 A/B 分别操作。无需重新创建隧道或密钥。若需要共享同项目 Codex 聊天或本机技能，启动前仍须主动选择这两项默认关闭的功能。

本机已核实 Codex 桌面版内置 CLI 0.160.1 的历史 / 技能元数据接口。上下文仅限已保存的用户与助手文字，不含工具日志、附件、隐藏推理或完整宿主上下文，不自动总结。技能按真实启用状态与当前版本发现，限个人技能及已批准的两套学术插件，不包含插件工具执行。实际插件内容不会打包到本仓库。新增功能尚需目标网页模型实际验收。

## 将网页结果交给 Codex

在控制窗口点击“复制网页任务模板”，补充任务后发给已选好插件的网页模型。模型应完成资料分析，再调用 `submit_web_result` 提交完整正文。只有返回 `status=received` 才表示本机收到了结果；界面中的普通聊天回复不会自动保存。

每份报告对应 `outputs/web-results/<result_id>/report.md` 和 `receipt.json`。报告正文保留原始 UTF-8 字节；回执包含标题、UTC 接收时间、内容 SHA-256 和 `review_status=not_reviewed`。自报模型名称不等于经过认证的模型身份。相同 `submission_id` 和内容重试会返回原回执，修订稿用新编号，不覆盖已有报告。每份最多 200000 字符 / 1 MiB，每个项目最多 1000 份。

完成后点击“复制 Codex 读取指令”，发给对应项目中的 Codex。它可以直接读取固定目录，即使隧道已经停止也可读取已保存文件；若通过 `list_web_results` / `read_web_result` 工具读取，则连接必须仍在线。省略 `result_id` 会选接收时间最新的报告，续读固定使用首次返回的 ID。哈希核对保存一致性，不证明结论正确；Codex 按你的当前要求核验并继续处理。

此版本提供手动继续流程；不派发网页模型任务，也不自动唤起 Codex。读工具仍标记为只读，提交工具按实际行为标记为写入。

## 开发与验证

```powershell
& ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider --basetemp work/tests-manual
```

先安装官方隧道客户端。测试使用临时目录、假凭据和本机模拟端点，不连接真实隧道。本地测试通过不等于网页验收通过。pytest 会清理指定临时目录，不要指定用户数据目录。

公开文件采用白名单；新文件需显式加入 `.gitignore` 的允许项。请勿强制添加凭据、日志或本机维护资料。示例图片为程序生成的图形测试，不是私人研究数据。

## 许可证

项目自身采用 [MIT](LICENSE)。第三方组件保留各自许可证，见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
