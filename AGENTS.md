# EverOS Bridge · Agent 接入说明

用户让你「读这个仓库，接入 EverOS 记忆」时，从这里开始。

目标：在当前机器上重建默认的记忆读写流程，包括四件事：
- 会话开始读用户画像；
- 每条提问首次检索使用 agentic，失败或未命中按轨道回退 hybrid；
- 每轮对话自动入库；
- 关键节点用 MCP 工具补写结构化记录。

先读这几份文件：
- [PROTOCOL.md](PROTOCOL.md)：读写规则，唯一依据；
- [docs/methodology.md](docs/methodology.md)：为什么这样做，以及踩过的坑；
- [docs/everos-notes.md](docs/everos-notes.md)：EverOS 服务本身的诀窍、局限与坑点，例如状态怎么读、做法 case 怎么生成；
- [docs/agent-onboarding.md](docs/agent-onboarding.md)：接入一个新 agent 的自查清单和方法。

## 接入步骤（按顺序做，每步核对结果）

1. **环境与仓库**：确认有 `python3`（≥ 3.9）和 `git`。仓库放在 `~/.everos-bridge`，下文记作 `$REPO`。
   - 仓库是私有的，网页链接和 raw 链接都读不到，直接用 gh 拉取：`gh repo clone namelessssssssssss/everos-bridge ~/.everos-bridge`；
   - `gh auth status` 显示未登录时，请用户先执行 `gh auth login`；
   - 已经拉过的，执行 `git -C ~/.everos-bridge pull`；如果报 `could not read Username`，先执行 `gh auth setup-git`。
   - 仓库不在默认路径也可以（例如 `~/.everos/source`）：`install.sh` 以自身所在路径为准，直接在原地 pull 后重跑即可，不必搬家。
2. **配置**：检查环境变量或 `~/.everos/config.env` 里是否有 `EVEROS_USER_ID` 和 `EVEROS_API_KEY`。
   - 没有就停下来问用户，不要猜，也不要去别处搜。
   - 推荐让用户自己写这个文件（权限 0600），key 就不会出现在聊天记录里。模板如下：
     ```dotenv
     EVEROS_API_BASE=https://api.evermind.ai
     EVEROS_USER_ID=<用户的记忆空间>
     EVEROS_API_KEY=<EverOS API key>
     EVEROS_OWNER_ALIASES=<botmux 邮箱前缀，可留空>
     EVEROS_API_VERSION=v2
     EVEROS_READ_V1=<官方迁完 v1 旧数据前填 1，迁完填 0>
     ```
   - 用户在聊天里直接给了值的话，用环境变量传给安装脚本，由脚本写文件：`EVEROS_USER_ID=… EVEROS_API_KEY=… EVEROS_READ_V1=1 $REPO/install.sh`。
   - agent 不能代替用户回答交互输入；没有终端时脚本不会进入交互，而是提示后退出。
3. **接线**：运行 `$REPO/install.sh`。脚本会检测本机装了哪些 agent，逐个接上，改动前先备份原配置；重复运行不会重复追加。
   - 各宿主的原生默认指令在首次接入、更新时都同步Bridge管理块及版本（AGENTS/CLAUDE/GEMINI、Cursor常驻规则、MiMo instructions等）；用户其它内容保留。接入Agent要读 `memory_rules(section="changes")` 理解增删改，再读当前规则并回读原生入口；仅刷新规则可用 `install.sh --rules-only`。基线缺失、仅MCP宿主的原生记忆工具缺失须如实报告，见 [docs/rule-sync.md](docs/rule-sync.md)。
   - TraeX ≥ 0.209.1 已覆盖 L3/L4；只刷新它用 `install.sh --only trae`。用户级 hook 在 `$TRAECLI_HOME/hooks.json`，默认 `~/.trae/cli/hooks.json`。安装后在 `/hooks` 审阅并信任当前定义，再验收新会话的自动注入和采集。
   - Claude Desktop 运行时会用内存里的配置覆盖外部改动，脚本检测到它在运行就不写，会给出指引：退出 App 后重跑，或在 App 设置里手动加。
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
  - 有 hook 的 agent 会自动注入：会话开始注入画像，每条提问先用 agentic 检索并注入相关记忆；
  - 没有 hook 的 agent，每条提问首次调 `memory_search(method="agentic")`，会话开始调 `memory_profile`；
  - 任务中发现新线索、失败或历史决定缺口时主动定向补查；摘要不足用 memory_open 展开。memory_context 可保存本机任务线索，压缩恢复后清除过期覆盖。已有注入仅覆盖对应问题。
  - 首轮结果中的实体、旧决定或依赖若影响当前未解问题，展开正文后按“原问题＋新标识＋待核实关系”二次搜索；保留上下层证据链，充分覆盖、无新增或重复命中时停止该分支。
  - 记忆只是背景参考，和当前对话冲突时以当前为准。
- **写**：
  - 对话原文由 hook 自动入库；
  - 交付、纠错、偏好、决策这类关键节点，调 `memory_remember`；
  - 排障或工具用法闭环，调 `memory_record_practice`：至少 3 个真实步骤，没走通或出乎意料的步骤标 `outcome`，用户纠正的原话填 `user_correction`；返回里会说明 v2 是否生成做法 case；
  - 画像写错了或过期了，调 `memory_fix_profile`。
- **交付自检**：写了就在回复里列出写了什么；没写就在结尾输出「记忆写入：无」。

## 给用户的一句话启动指令

> 用 gh 把私有仓库 namelessssssssssss/everos-bridge 拉到 ~/.everos-bridge，读里面的 AGENTS.md，按步骤把 EverOS 记忆接到你自己和这台机器上的其他 agent。

## 不要做

- 不要把 key 写进仓库、提交记录或回复。
- 不要删除记忆，除非用户明确要求。
- 不要绕过 `install.sh` 去改其他 agent 的配置；确实需要手改时，先备份。
- 不要在各 agent 加载的仓库目录里切分支或改代码，那等于直接改线上；开发用 `git worktree`，合并后再在原目录 pull 并重跑 `install.sh`。
- 改了读写规则，要同步更新 PROTOCOL.md 和 docs/，提交并推送。
