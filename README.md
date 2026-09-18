# ChatGPT Local Files MCP

**Connect ChatGPT Web to your local project files.**

让 ChatGPT 网页端读取你的本地项目资料

English | [简体中文](README.zh-CN.md)

Connect ChatGPT Web to a local Windows folder through MCP. Read-only Markdown and image access, with a folder picker and Secure MCP Tunnel.

## What it does

- Select one local project folder in a Windows GUI.
- Let a compatible web model browse folders, search Markdown, and read relevant passages with source locations.
- Inspect PNG, JPEG, WebP and TIFF images, including crops and TIFF pages.
- Stop the connection to revoke access and terminate the tunnel process tree.

No file-writing or arbitrary command tools. No model API calls from this application. You can optionally hand the model's results to Codex for subsequent work; Codex is not required for reading files.

```text
ChatGPT Web → OpenAI Secure MCP Tunnel → local read-only MCP server → selected folder
```

Requested text and images are transmitted to the remote service. The app does not automatically upload the entire project, but local execution does not mean your data stays on your computer.

## Supported today

| Area | Support |
| --- | --- |
| Platform | Windows; current environment uses Python 3.14 and the Windows amd64 tunnel client |
| Text | UTF-8 `.md` and `.markdown`, literal search and line-range reads |
| Images | PNG, JPEG, WebP, TIFF; crop, resize and TIFF page selection |
| Word / PDF / Excel | Not directly supported; convert relevant material to Markdown or images first |
| Models | Users report successful Pro and sol access; other models and accounts require verification |
| Local changes | No write tools; one selected project root at a time |

The launcher uses OpenAI Secure MCP Tunnel and Windows credential/process APIs. It is not a universal installer for every model platform. Account eligibility, workspace permissions and possible tunnel fees must be checked against the official service and your account; this project does not promise free access.

## Installation

Prerequisites: Python 3.14 with Tkinter, [uv](https://docs.astral.sh/uv/getting-started/installation/), and an account with the required tunnel and web-plugin access.

From PowerShell in the repository directory:

```powershell
uv venv --python 3.14 .venv
uv pip sync --python .venv/Scripts/python.exe requirements.lock.txt
```

Download the official [tunnel-client v0.0.14 Windows amd64 ZIP](https://github.com/openai/tunnel-client/releases/download/v0.0.14/tunnel-client-v0.0.14-windows-amd64.zip). Executables are not included in this repository.

Verify the ZIP before extracting:

```powershell
Get-FileHash -Algorithm SHA256 '<full path to downloaded ZIP>'
```

Expected SHA256:

```text
784ab8da7b5a88f0109f1fd8aaf0a1c86067430b896dddf307ef7e3cc49fa1a5
```

Release metadata and checksums are retained under `vendor/`. After verification, extract and place `tunnel-client.exe` at `vendor/tunnel/tunnel-client.exe`. Retain the upstream license notices. The Cloudflare runtime is not used by this application.

## First connection

1. Follow the [official Secure MCP Tunnel guide](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) to create a tunnel and associate the appropriate ChatGPT workspace. The runtime key needs Tunnels Read + Use permissions; creating a tunnel requires management permissions.
2. Double-click `启动.cmd`. The current GUI is in Chinese. Select `账号 A` or `账号 B`: these are independent configuration slots, not shared accounts. One slot is sufficient.
3. Enter your own tunnel ID and runtime key locally. Never paste credentials into an issue, chat or commit.
4. Choose `使用测试项目` (use sample project), start the connection, and confirm the directory.
5. In the corresponding web account, create a private plugin using the Tunnel connection and select your tunnel. Available UI and permissions depend on your account.
6. Start a new chat with the plugin and a compatible model. Ask it to read the sample Markdown and actually inspect the referenced image.
7. After verification, stop the connection and select your own project folder.

Keep the control window running while in use. Stopping or closing it revokes access. Use a new chat when switching projects; stopping does not erase content already present in a remote conversation.

Try asking:

> Read the project overview, locate the relevant Markdown and images, and summarize the project goals and next steps. Cite the files and lines you actually read. If a tool fails or a file is missing, say so.

## Tools

| Tool | Purpose |
| --- | --- |
| `project_info` | Project, version, access ID, and opening sections of root AGENTS.md / PROJECT_STATUS.md |
| `list_files` | Paginated browsing of supported files and subdirectories |
| `search_markdown` | Literal search of Markdown contents and supported filenames |
| `read_markdown` | Line-range reads and local image references |
| `read_image` | Image pixels, with crop, page and size options |

Project-discovery guidance encourages browsing and source-based answers, but the model must actually call tools. It does not guarantee identical behavior across models. Review root project instructions before sharing a folder: `project_info` reads their opening content.

Detailed limits and troubleshooting: [中文使用说明](使用说明.md). Privacy boundaries: [SECURITY.md](SECURITY.md).

## Development

Install the official tunnel executable before running tests:

```powershell
& ./.venv/Scripts/python.exe -m pytest tests -q -p no:cacheprovider --basetemp work/tests-manual
```

Tests use temporary directories, fake credentials and localhost mock control endpoints. Never target real runtime state. Pytest clears its specified temporary directory, so do not point it at user data. Local tests do not replace web-model acceptance testing.

The repository uses an explicit public-file allowlist in `.gitignore`. Review new files before adding allowlist entries. Do not force-add credentials, logs or private project material. Sample images are synthetic test figures.

## License

[MIT](LICENSE) for this project's code. Dependencies retain their upstream licenses; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
