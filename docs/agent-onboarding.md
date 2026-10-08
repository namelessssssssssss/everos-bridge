# 新 Agent 接入：自查清单与方法

要把一个新的 agent（或新版本的宿主）接进 EverOS 时，按本文做。已经接好的宿主列在文末的速查表里。

## 方法：四层，从通用到专用

| 层级 | 做什么 | 必做 | 得到什么 |
| --- | --- | --- | --- |
| L1 MCP | 注册 `plugin/everos/mcp_server.py`（stdio） | 是 | 6 个 `memory_*` 工具，模型可以主动读写 |
| L2 指令规则 | 把 AGENTS.md「日常读写」那段写进宿主的全局指令文件 | 是 | 没有 hook 时，靠模型自觉在每条提问前检索 |
| L3 提问前注入 | 挂「提问前」hook（如 UserPromptSubmit），会话开始的 hook 能注入时也挂上 | 宿主支持就做 | 自动读画像，每条提问自动检索 |
| L4 回合结束采集 | 挂「回合结束」hook（如 Stop），并给它的会话记录写解析器 | 宿主支持就做 | 对话原文自动入库 |

## 自查清单（逐项填）

| # | 要查清的问题 | 怎么查 | 答案 |
| --- | --- | --- | --- |
| 1 | 能不能跑 stdio MCP？配置写在哪，格式是什么（JSON `mcpServers`、TOML，还是 CLI 子命令）？ | 宿主文档，或 `<cli> mcp --help` | |
| 2 | 有没有全局指令文件？位置在哪？ | 宿主文档（AGENTS.md / CLAUDE.md / GEMINI.md / rules） | |
| 3 | 有没有生命周期 hook？有哪些事件？ | 宿主文档，或 `<cli> --help` | |
| 4 | 哪些事件能注入上下文？输出格式是什么？ | 文档里的 hook 输出说明 | |
| 5 | hook 的超时字段叫什么、单位是什么？有没有总预算？ | 文档 | |
| 6 | hook 的输入里有哪些字段（session_id、transcript_path、prompt、cwd、session_title、client_type）？ | 写一个把 stdin 原样存到文件的 hook，实测一次 | |
| 7 | 会话记录存在哪？什么格式？用户原话、助手回复、工具调用分别怎么识别？要剔除哪些脚手架？ | 找最近一个会话的文件，统计条目类型 | |
| 8 | 有没有插件或市场机制？插件根目录用什么变量？原地加载还是拷贝安装？ | `<cli> plugin --help`，装一次看缓存目录 | |
| 9 | 密钥怎么给（环境变量 / 配置文件 / 安全存储）？hook 子进程能不能拿到环境变量？ | 实测 | |
| 10 | 云端或沙箱：有没有网络白名单？能不能读本机的 `~/.everos`？ | 实测 `--doctor` | |

## 实现步骤

1. **L1**：在 `install.sh` 里加上这个宿主的 MCP 注册方式。能用 CLI 就用 CLI（如 `<cli> mcp add everos -- python3 <abs>/mcp_server.py`），否则合并 JSON，并且先备份。
2. **L2**：在 `install.sh` 里给它的全局指令文件追加规则段。追加前先检查是否已经有了，保证幂等。
3. **L3 / L4**：
   - 在 `core.py` 加 `messages_<host>()` 解析器，并在 `detect_host()` 里加上识别逻辑；
   - hook 输入不带会话记录路径时，像 Kimi 那样按 session_id 去定位；
   - 在 `hook.py` 里适配它的输出格式：JSON 的 `additionalContext`，或者纯文本 stdout；
   - 在 `session_name()` 里给它分配 session 前缀（cc_ / cx_ / km_ …）；
   - 在 `install.sh` 里挂上 hook，超时按宿主自己的单位填：提问前 20 秒，回合结束 10 秒。
4. **文档**：更新 README 的覆盖表、PROTOCOL.md 的接线表、本文的速查表；新踩的坑记进 `docs/methodology.md`。
5. **提交并推送**，再在另一台机器上按 AGENTS.md 走一遍，确认能复现。

## 验证清单（全部通过才算接入完成）

- [ ] `python3 plugin/everos/hook.py --doctor` 各项 OK。
- [ ] `hook.py --dry-run <会话记录文件>`：解析出来的消息角色正确、没有混进脚手架、时间戳单调递增。
- [ ] 真实会话里，让模型复述「EverOS 相关记忆」或「用户画像」，确认注入生效。
- [ ] 一轮对话结束后，v2 里出现这个会话的 episode，`sender_ids` 归属正确（用户是 owner，同事是 `feishu_*`）。
- [ ] 断网或填错 key 时，会话照常进行（失败放行），`state/hook.log` 里有错误记录。
- [ ] 提问前 hook 的超时设置生效：最慢 20 秒，超时后不注入，也不卡住会话。
- [ ] `install.sh` 重复运行不会重复追加配置（幂等）。

## 已接入宿主速查

| 宿主 | L1 MCP | L2 指令 | L3 提问前注入 | L4 采集 | 关键差异 |
| --- | --- | --- | --- | --- | --- |
| Claude Code | 插件内 `claude-mcp.json` | `~/.claude/CLAUDE.md`（可选） | SessionStart（画像）+ UserPromptSubmit，输出 JSON `additionalContext` | Stop，读 transcript_path（JSONL） | 本地市场原地加载；SessionEnd 只有约 1.5 秒；注入单条上限 1 万字，对用户不可见 |
| Codex | `codex mcp add`（绝对路径） | `~/.codex/AGENTS.md` | 同 Claude Code（Codex 也会设置 `CLAUDE_PLUGIN_ROOT`） | Stop，读 rollout JSONL（`response_item`） | 插件是拷贝安装；hooks.json 必须放在 `hooks/` 下；需要 `[features] hooks = true` |
| Kimi Code | `~/.kimi-code/mcp.json` | — | 只有 UserPromptSubmit 能注入，输出纯文本；画像放到首条提问 | Stop，按 session_id 找 `wire.jsonl` | `[[hooks]]` 只允许 event / matcher / command / timeout 四个字段 |
| TraeCode CLI（coco） | `coco mcp add` | — | 未接（有 hook，待按清单评估） | 未接 | 和 Codex 同源 |
| Claude Desktop / Cowork | `claude_desktop_config.json` | claude.ai 上的 skill | — | — | 需要重启 App；Cowork 靠 skill |
| Cursor、Gemini CLI | `mcp.json` / `settings.json` | `~/.gemini/GEMINI.md` | — | — | 只有 MCP |
| Claude Code on the web | 仓库里的 `.mcp.json` | 仓库 CLAUDE.md、claude.ai 上的 skill | 仓库 `.claude/settings.json` 里的 hook | 同左 | 不装插件；要用网络密钥放行 `api.evermind.ai`，见 README「云端」 |
