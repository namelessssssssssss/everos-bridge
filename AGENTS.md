# EverOS Bridge · Agent 接入说明

用户让你「读这个仓库，接入 EverOS 记忆」时，从这里开始。

目标：在当前机器上重建默认的记忆读写流程，包括四件事：
- 会话开始读用户画像；
- 每条提问前检索相关记忆；
- 每轮对话自动入库；
- 关键节点用 MCP 工具补写结构化记录。

先读三份文件：
- [PROTOCOL.md](PROTOCOL.md)：读写规则，唯一依据；
- [docs/methodology.md](docs/methodology.md)：为什么这样做，以及踩过的坑；
- [docs/agent-onboarding.md](docs/agent-onboarding.md)：接入一个新 agent 的自查清单和方法。

## 接入步骤（按顺序做，每步核对结果）

1. **环境**：确认有 `python3`（≥ 3.9）和 `git`。仓库建议放在 `~/.everos-bridge`，下文记作 `$REPO`。
2. **配置**：检查环境变量或 `~/.everos/config.env` 里是否有 `EVEROS_USER_ID` 和 `EVEROS_API_KEY`。
   - 没有就直接问用户，不要猜，也不要去别处搜；
   - 由 `install.sh` 交互写入 `~/.everos/config.env`，权限 0600。
   - 默认 `EVEROS_API_VERSION=v2`。官方还没把用户的 v1 旧数据迁到 v2 之前，加上 `EVEROS_READ_V1=1`，读取时会合并旧库。
3. **接线**：运行 `$REPO/install.sh`。脚本会检测本机装了哪些 agent，逐个接上，改动前先备份原配置。
4. **验证**：
   - `python3 $REPO/plugin/everos/hook.py --doctor`，各项都是 OK；
   - 提问前检索：执行下面这条命令，应输出相关记忆：
     ```bash
     printf '{"session_id":"selftest","hook_event_name":"UserPromptSubmit","prompt":"<一句跟用户工作相关的话>"}' | python3 $REPO/plugin/everos/hook.py
     ```
     测完删掉 `~/.everos/state/recall/selftest.json`；
   - 写入：调一次 MCP `memory_remember`，再用 `memory_search` 能搜到；
   - 真实会话：新开一个会话，问模型「上下文里有没有 EverOS 用户画像」，它应该能复述出来。
5. **未覆盖的 agent**：你所在的 agent 不在 `install.sh` 覆盖范围内时，按 [docs/agent-onboarding.md](docs/agent-onboarding.md) 接入。至少要做到两件事：
   - 接上 MCP；
   - 在它的全局指令文件里写入下面「日常读写」的规则。
6. **汇报**：列出这几项：
   - 接上了哪些 agent；
   - 每个 agent 的能力：自动读、自动写、记忆工具；
   - 自检结果；
   - 没完成的事项。

## 日常读写（接入后的默认行为）

- **读**：
  - 有 hook 的 agent 会自动注入：会话开始注入画像，每条提问注入相关记忆；
  - 没有 hook 的 agent，每条提问先调 `memory_search`，会话开始调 `memory_profile`；
  - 记忆只是背景参考，和当前对话冲突时以当前为准。
- **写**：
  - 对话原文由 hook 自动入库；
  - 交付、纠错、偏好、决策这类关键节点，调 `memory_remember`；
  - 排障或工具用法闭环，调 `memory_record_practice`；
  - 画像写错了或过期了，调 `memory_fix_profile`。
- **交付自检**：写了就在回复里列出写了什么；没写就在结尾输出「记忆写入：无」。

## 给用户的一句话启动指令

> 读取 https://github.com/namelessssssssssss/everos-bridge 的 AGENTS.md，按步骤把 EverOS 记忆接到你自己和这台机器上的其他 agent。

## 不要做

- 不要把 key 写进仓库、提交记录或回复。
- 不要删除记忆，除非用户明确要求。
- 不要绕过 `install.sh` 去改其他 agent 的配置；确实需要手改时，先备份。
- 改了读写规则，要同步更新 PROTOCOL.md 和 docs/，提交并推送。
