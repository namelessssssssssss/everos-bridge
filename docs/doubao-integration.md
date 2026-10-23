# Doubao Work（豆包办公）接入

Doubao Work 是云端 Linux 会话：agent 运行在受控 VM 里，**不能自己注册 stdio MCP，也没有
生命周期 hook、全局指令文件或插件市场**。它与 Claude Code / Codex / TraeX 这类可接线宿主
不同，接入只能做到「工具 + 用户级 skill + 模型按 skill 主动执行」。

四层映射：

| 层 | 能力 | Doubao Work 上的实现 |
| --- | --- | --- |
| L1 MCP | 8 个 `memory_*` 工具 | `bin/everos-mcp`（命令行 stdio 客户端，调未改动的 `plugin/everos/mcp_server.py`） |
| L2 指令 | 每轮读写规则 | 用户级 skill `everos-memory`（`plugin/skills/everos-memory-doubao`） |
| L3 提问前检索 | 画像/记忆注入 | 无 hook；由 agent 按 skill 每轮主动调 `memory_profile` / `memory_search`（每条消息首次 agentic），任务中按缺口补查并用 `memory_open` 展开 |
| L4 回合结束采集 | 对话原文入库 | 无 hook；每轮回复前主动跑 `everos-doubao-ingest` |

## 安装

前置：`python3 >= 3.9`、`git`、可访问 `api.evermind.ai`。私有仓需要 gh 登录。

```bash
gh repo clone namelessssssssssss/everos-bridge ~/.everos-bridge   # 已 clone 则 git -C 拉取更新
EVEROS_USER_ID=… EVEROS_API_KEY=… EVEROS_READ_V1=1 ~/.everos-bridge/install.sh
```

完整安装会检测其他宿主（Claude Code / Codex / TraeX / Kimi / MiMo），Doubao Work 部分无论
其他宿主是否存在都会执行：

- `~/.local/bin/everos-mcp`、`~/.local/bin/everos-doubao-ingest` 链到本仓 `bin/`；
- 检测到 `~/.doubao/agent_mode/workspace/.user_skills` 时，把
  `plugin/skills/everos-memory-doubao` 同步为该目录下的 `everos-memory`（含 `scripts/check.sh`）。
  该目录不存在时只装命令并给出提示，不会擅自创建。

只想装命令、不配置其他宿主、不需要凭据自检：`install.sh --cli-only`（两个 Doubao 命令也会装）。
Doubao skill 目录可用环境变量 `DOUBAO_USER_SKILLS` 覆盖（测试用）。

凭据只写 `~/.everos/config.env`（0600），不进仓库、不进 skill。

## 首次建立入库时间点

`--all` 只补采「接入时间点」之后更新的会话，避免把接入前的历史对话一次性回灌。第一次使用
前按接入时刻建立标记（安装器不自动做，是否回灌历史由 owner 决定）：

```bash
mkdir -p ~/.everos/state
python3 -c "import time;open('$HOME/.everos/state/doubao_ingest_since','w').write(str(time.time()))"
```

需要显式回灌某时间点之后的会话时，用 `--since 2026-10-16T19:15:00+08:00` 覆盖。

## 日常用法

```bash
bash ~/.doubao/agent_mode/workspace/.user_skills/everos-memory/scripts/check.sh  # 环境自检

# L3（由 agent 每轮主动执行；每条消息首次检索用 agentic，最长约 180 秒）
everos-mcp memory_profile '{}'
everos-mcp memory_search '{"query":"…","kind":"all","top_k":5,"method":"agentic","session_id":"dw_<会话id>"}'
# 任务中补查 / 展开 / 阶段上下文
everos-mcp memory_search '{"query":"…","context":"新线索","method":"hybrid","session_id":"dw_<会话id>"}'
everos-mcp memory_open '{"memory_id":"em_…","offset":0,"limit":4000}'
everos-mcp memory_context '{"session_id":"dw_<会话id>","stage":"new_evidence","clues":"…"}'

# 结构化写入（交付 / 纠错 / 决策 / 排障闭环）
everos-mcp memory_remember @/abs/path/args.json
everos-mcp memory_record_practice @/abs/path/args.json

# L4：每轮最终回复前
everos-doubao-ingest --all       # 增量采集接入点之后有更新的会话（账本去重，可反复跑）
everos-doubao-ingest --current   # 只采集当前会话
everos-doubao-ingest --current --dry-run   # 只预览将写入的消息，不调 API
```

`everos-mcp` 客户端等待 200 秒（`EVEROS_MCP_CLIENT_TIMEOUT`），大于 agentic 默认预算；agent 在
Doubao Work 的 Bash 工具里调用时要把工具超时设到 240 秒以上，否则默认 120 秒会先打断检索。

退出码：`0` 成功（含无新增），`1` 写入/服务失败或参数之外的错误，`2` 参数错误。
`EVEROS_WRITE=0` 时写工具与采集全部拒绝入库。

## 采集的会话格式与边界

Doubao Work 会话记录在
`~/.doubao/agent_mode/workspace/.sessions/<sid>/agents/<agent>/system/`：

- `trajectory.jsonl`：OpenAI 风格消息，一行一个 JSON，**行内无时间戳**；
- `assignment.md`：每轮用户需求的时间戳（`## [ISO] 需求` 标题），与 role=user 行按顺序对齐。

采集器只读 `assignment.md` 里标记 `Role: main` 的 agent；只写：

- 用户原话（自动剥掉 `<system-reminder>`；同一条请求被记录两次自动去重）；
- 助手最终回复（没有 `tool_calls` 的 assistant 文本）。

不写工具调用、工具结果、图片、过程旁白和 reasoning。assistant 最终回复的时间戳取「下一条
用户消息前 1 毫秒」；当轮最终回复要到下一轮执行采集时才补写，这是无 Stop hook 的固有限制。

账本在 `~/.everos/state/sessions/doubao_<sid>.json`，语义与其他宿主一致：只有 add 返回已
识别成功状态才推进 offset；失败可重试。首行被改写或 offset 越界时停止并报错，不静默重放；
最后一行写了一半（没有换行）时留给下一轮。

session_id 规则：`dw_<首日>_<首句slug>_<完整sid>`，前缀与其他宿主区分。

## 验证清单

- [ ] `bash plugin/doubao/check.sh`（或 skill 内副本）各项 OK；
- [ ] `python3 -m unittest discover -s tests -v` 全绿，含 `tests/test_doubao.py`；
- [ ] `everos-mcp list` 列出 8 个 `memory_*` 工具，`memory_profile` 能返回画像；
- [ ] `memory_search` 不带 method 时走 agentic，结果行的 `em_` 标识能被 `memory_open` 展开；
- [ ] `everos-doubao-ingest --current --dry-run` 解析出的角色只有 user/assistant 最终回复，
      不含脚手架与 key/token；
- [ ] 真实写入后重复执行 `--current` 的 `new_messages=0`（幂等）；
- [ ] v2 按 session_id 回读得到 episode，`sender_ids` 归属正确（owner vs `doubao_work`）；
- [ ] `EVEROS_WRITE=0` 时采集与写工具均被拒绝。

## 已知局限

- L3/L4 是「skill 要求模型主动做」，没有平台级强制；漏做一轮时下一轮 `--all` 会补。
- 会话记录格式属于 harness 内部布局，harness 改版（字段、目录、时间戳位置）时需要同步更新
  `plugin/doubao/doubao.py` 与其测试。
- 云端 VM 的 home 持久化范围由平台决定；`check.sh` 用于新会话快速发现缺失并给出修复提示。
- harness 工具通道偶发直接返回 `http timeout`（不是 bridge 输出，长耗时非 everos 命令也会出现）；
  agentic 首查遇到时改为后台运行再取结果，不把它当作无记忆。
