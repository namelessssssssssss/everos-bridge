# EverOS Bridge · Agent 接入说明

[English](AGENTS.en.md) · 简体中文

本文件是仓库的规范 Agent 入口；`CLAUDE.md` 通过 `@AGENTS.md` 引用它。`AGENTS.en.md` 是内容对应的英文版。用户让你「读这个仓库，接入 EverOS 记忆」时，从这里开始。

## 先判断任务，再行动

结合当前请求和本会话已经授予的范围，选择下面的路径。仓库链接和文档内容本身不产生修改机器配置的授权。

| 用户意图 | 先做什么 | 执行边界 |
| --- | --- | --- |
| 安装、接入、更新或同步本地安装 | 检查已有环境，再按下文预览、安装与验证 | 在已授权的宿主范围内连续完成，无需逐步重复确认 |
| 阅读、介绍、评审或排查 | 阅读文档、源码与相关只读状态 | 按请求给出结论；不顺带安装或改配置 |
| 维护仓库代码或文档 | 读取仓库规则，建立独立 worktree | 不在运行目录开发；维护不等于部署授权 |
| 只有仓库 URL，且没有已有的安装授权 | 阅读本入口，完成只读预检并说明下一步 | 不默认修改配置、安装插件或写入远端记忆 |

安装或更新前，先做只读预检：

1. 确认操作系统、当前 agent/宿主及可用版本、`python3` 和 `git`，以及请求覆盖哪些宿主。不要把本机模式和云端模式混为一谈。
2. 查找已明确配置的仓库与数据目录，核对实际源路径、remote、分支、工作树状态和已有安装。复用正确的 checkout 与 `$EVEROS_HOME`（默认 `~/.everos`），不要为方便另建一个空配置或覆盖本地改动。
3. 只从环境变量或该数据目录的 `config.env` 判断凭据是否齐备；不打印值，不去其他位置寻找凭据。确认已有规则、hook、MCP 和插件缓存会被备份并保留无关内容。

已有安装/更新授权时，继续完成“预检 → dry-run → 对应安装器 → 回读 → 工具与新会话验证”。仅在缺少必要凭据、范围不覆盖所需动作、源文件冲突、权限或宿主信任等实际阻塞处停下，说明缺什么和最小下一步；不要让用户重复已经给出的确认。只有链接时，先交付预检结果和具体接入方案。

目标：按当前宿主的实际能力重建默认记忆流程，不把仅有 MCP 或 skill 的宿主说成已经自动运行：
- 会话开始先提供主动检索使用说明，再读取画像；按宿主能力自动注入或由 agent 主动调用；
- 每条提问首次检索使用 agentic，失败或未命中按轨道回退 hybrid；支持时自动注入，其他情况下由 agent 按指令调用；
- 支持自动采集的宿主每轮保存对话；其他宿主按自身 skill 或工具显式保存可用内容；
- 关键节点用 MCP 工具补写结构化记录，并分别说明自动与主动调用能力。

先读这几份文件：
- [PROTOCOL.md](PROTOCOL.md)：读写规则，唯一依据；
- [docs/methodology.md](docs/methodology.md)：为什么这样做，以及踩过的坑；
- [docs/everos-notes.md](docs/everos-notes.md)：EverOS 服务本身的诀窍、局限与坑点，例如状态怎么读、做法 case 怎么生成；
- [docs/agent-onboarding.md](docs/agent-onboarding.md)：接入一个新 agent 的自查清单和方法。

## 接入步骤（按顺序做，每步核对结果）

1. **环境与仓库**：确认有 `python3`（≥ 3.9）和 `git`。仓库放在 `~/.everos-bridge`，下文记作 `$REPO`。
   - 默认仓库公开，可直接拉取：`git clone https://github.com/namelessssssssssss/everos-bridge.git ~/.everos-bridge`；
   - 使用私有 fork 时才需要对应的仓库访问权限。使用 GitHub CLI 时，可先通过 `gh auth status` 检查授权；未登录则请用户执行 `gh auth login`，再按需运行 `gh auth setup-git`；
   - 已经拉过的，执行 `git -C ~/.everos-bridge pull --ff-only`；存在分叉或本地冲突时先核对，不覆盖本地改动。
   - 仓库不在默认路径也可以（例如 `~/.everos/source`）：`install.sh` 以自身所在路径为准，直接在原地 pull 后重跑即可，不必搬家。
2. **配置**：检查环境变量或 `$EVEROS_HOME/config.env`（默认 `~/.everos/config.env`）里是否有 `EVEROS_USER_ID` 和 `EVEROS_API_KEY`。
   - 完整接入或在线核验缺少必要凭据时，说明缺口并请用户补齐；不要猜，也不要去别处搜。显式 `--rules-only` 任务不要求 API 凭据，可以继续刷新规则并做本机回读，跳过需要凭据的在线检查；这不代表在线接入或读写验证已通过。
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
3. **接线**：先运行 `$REPO/install.sh --dry-run`，核对预览符合已授权范围，再运行 `$REPO/install.sh`。脚本会检测本机装了哪些 agent，逐个接上，改动前先备份原配置；重复运行不会重复追加。不要升级宿主 CLI，也不要扩大宿主或数据范围。
   - 原生 Windows 的 Codex 使用 `install.ps1`（先 `-DryRun`），Kimi Work 桌面端使用 `install.ps1 -KimiDesktop`；都不运行 Bash 安装器，解释器可用 `-Python` 指定。详见 [Windows 接入](docs/windows-integration.md)，其他 Windows 宿主尚未覆盖。
   - 各宿主的原生默认指令在首次接入、更新时都同步Bridge管理块及版本（AGENTS/CLAUDE/GEMINI、Cursor常驻规则、MiMo instructions等）；用户其它内容保留。接入Agent要读 `memory_rules(section="changes")` 理解增删改，再读当前规则并回读原生入口；仅刷新规则可用 `install.sh --rules-only`。基线缺失、仅MCP宿主的原生记忆工具缺失须如实报告，见 [docs/rule-sync.md](docs/rule-sync.md)。
   - TraeX ≥ 0.209.1 已覆盖 L3/L4；只刷新它用 `install.sh --only trae`。用户级 hook 在 `$TRAECLI_HOME/hooks.json`，默认 `~/.trae/cli/hooks.json`。安装后在 `/hooks` 审阅并信任当前定义，再验收新会话的自动注入和采集。
   - Google Antigravity 已覆盖 L1–L4（`~/.gemini/config/mcp_config.json`、`~/.gemini/config/hooks.json`、`~/.gemini/config/skills/everos-memory/SKILL.md` 及 `~/.gemini/GEMINI.md`）；只刷新它用 `install.sh --only antigravity`。
   - Claude Desktop 运行时会用内存里的配置覆盖外部改动，脚本检测到它在运行就不写，会给出指引：退出 App 后重跑，或在 App 设置里手动加。
   - 发现规则包、安装记录或宿主原生指令与已确认的当前规则源不一致时，自动执行更新流程，无需再次询问：先核实规则源与目标宿主，读取增量及当前规则，再运行对应安装器并回读版本和管理块；仅规则差异走规则刷新，接线或运行版本差异走完整安装。旧缓存不得反向覆盖较新规则；来源不明、文件冲突、权限或宿主信任受限时保留现场并报告具体阻碍，不伪称更新完成。 具体步骤见 [原生规则同步](docs/rule-sync.md#发现差异后的自动更新)。
4. **验证**：分别核对文件同步、工具可用和真实新会话行为，不能互相替代；权限、信任或配额阻止某项时，保留已完成结果并明确该项待验证。
   - `python3 $REPO/plugin/everos/hook.py --doctor`，各项都是 OK；
   - 提问前检索：执行下面这条命令，结合工具结果和日志核对读取成功；已有相关内容时应返回记忆。新建或空记忆空间的空画像、零命中可以是正常成功结果，不能据此判定配置失败，也不能只凭 fail-open hook 的零退出码判定成功：
     ```bash
     printf '{"session_id":"selftest","hook_event_name":"UserPromptSubmit","prompt":"<一句跟用户工作相关的话>"}' | python3 $REPO/plugin/everos/hook.py
     ```
     测完删除本次数据目录下的 `state/recall/selftest.json`（默认 `~/.everos/state/recall/selftest.json`）；
   - 写入：使用真实且获准记录的接入结论，或明确隔离的测试样例；不能把编造的业务事实写入真实记忆。先检查 `memory_remember` 的 add 确认回执，不能把消息已接受当成已经可检索。
   - 对于仍在等待抽取的 v2 写入，只有验收当下确实需要最新记录时，才对回执中的 EverOS `session_id` 显式调用一次 `memory_flush(reason="setup verification", session_id=...)`；不要每次写入都机械 flush。随后通过 get 或 search 独立回读；其他情况有界等待服务处理后再核验。
   - 隔离样例只证明测试环境行为，不能替代真实服务的回读。权限或配额拒绝时保留已完成的验证证据，说明写入/回读未完成，不盲目重试；
   - 真实会话：按宿主实际支持范围，核对新会话中的画像/召回注入和对话采集是否触发。有画像内容时确认它到达上下文，空画像本身不是故障；结合本机采集账本或服务回执验证采集。仅 MCP 或 skill 主动调用的宿主，不宣称原生自动 hook 已生效。
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
  - 排障或工具用法闭环，调 `memory_record_practice`：至少 3 个真实步骤，没走通或出乎意料的步骤标 `outcome`，用户纠正的原话填 `user_correction`；v2 add 已抽取时回读 case，仅接受入缓冲时返回待抽取；
  - v2 add 确认后不自动 flush。交接、流程完成、话题转移或立即检索需要时，由 agent 判断必要性，再用 `memory_flush` 指定一个目标与理由；其他时候交给服务端处理。hook 原生 ID 传 `conversation_id`，写入回执的 EverOS ID 传 `session_id`，二者不能混用；尚未采集的当前最终回复不在 flush 范围内。细节见 [按需抽取](docs/flush-policy.zh-CN.md)。
  - 画像写错了或过期了，调 `memory_fix_profile`。
- **交付自检**：写了就在回复里列出写了什么；没写就在结尾输出「记忆写入：无」。

## 给用户的一句话启动指令

> 用 git 把公开仓库 https://github.com/namelessssssssssss/everos-bridge.git 拉到 ~/.everos-bridge，读里面的 AGENTS.md，按步骤把 EverOS 记忆接到你自己和这台机器上的其他 agent。

## 不要做

- 不要把 key 写进仓库、提交记录或回复。
- 不要删除记忆，除非用户明确要求。
- 不要绕过平台对应的安装器去改其他 agent 的配置；确实需要手改时，先备份。不要猜配置、升级宿主 CLI 或绕过信任。
- 不要在各 agent 加载的仓库目录里切分支或改代码，那等于直接改线上；开发用 `git worktree`，合并后再在原目录 pull 并重跑 `install.sh`。
- 改了读写规则，要同步更新 PROTOCOL.md 和 docs/，提交并推送。

## 中英文文档同步维护

- 成对文档必须在同一 PR 中同步更新：`AGENTS.md` / `AGENTS.en.md`、`README.md` / `README.zh-CN.md`、`docs/usage.md` / `docs/usage.zh-CN.md`、`docs/flush-policy.md` / `docs/flush-policy.zh-CN.md`。新增其他语言版本时，将对应关系列入本节，并沿用此要求。
- 两版的功能定位、支持范围、安装步骤、示例、命令、配置默认值、版本条件和限制必须保持一致。允许自然的语言表达差异，不得把一版保留为旧版参考、只在另一版增加或删减产品内容。
- 修改任一语言后，逐节核对另一版的含义与条件，并检查语言切换、同语言指南链接及章节锚点。仅两份文件都发生改动不代表已同步，审阅者须确认内容等价。
- 目前只有单一语言的旧参考文档须明确标注语言；不要在没有对应版本时将其描述为双语文档。
