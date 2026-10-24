# 原生 Windows：Codex CLI / App

Windows 使用 PowerShell 安装入口，目前只配置 Codex。其他宿主继续按各自文档接入；安装器不因为发现其目录就宣称支持。无需 WSL、Git Bash、管理员权限、符号链接或 pip 依赖。Python 需要 3.9 以上，Codex CLI 需要支持插件和 `commandWindows` hook。

```powershell
git clone https://github.com/namelessssssssssss/everos-bridge.git "$env:USERPROFILE\.everos-bridge"
Set-Location "$env:USERPROFILE\.everos-bridge"
.\install.ps1 -DryRun
.\install.ps1
```

未自动找到 Python 时，给 `-Python` 传实际 `python.exe` 的绝对路径；安装器会探测解释器版本并跳过 Windows Store 的占位入口。凭据沿用环境变量或 `$EVEROS_HOME/config.env`；缺失时在交互终端输入，API key 隐藏输入。不要在提交、文档或命令示例里填实际值。

平台隔离：Windows 安装、配置编辑、锁、运行快照生成和测试全部位于 `windows/`，不导入 Unix 安装器。`install.sh`、`scripts/`、`plugin/`、`bin/`、`cloud/`、原有测试与 CI 保持主干版本；`windows/linux-baseline.json` 为 47 个现有文件保存源码指纹，测试逐项比对。Linux 主干有主动变更时，须审阅后显式更新 Windows 指纹基线；本次基线包含已合并的 Doubao 接入。Windows 只读复用检索和写入算法，在仓外生成自己的运行副本；平台适配只应用到这个副本。每个适配点校验原文锚点和出现次数，上游结构变化时拒绝构建，要求重新审阅。Windows 不会修改 Linux 的源码、hook 定义或安装配置。

安装动作：

- 创建或复用 `%USERPROFILE%\.everos`（可用 `EVEROS_HOME` 覆盖），移除目录的继承 ACL，并为当前用户和 SYSTEM 授权；拒绝数据根位于源码 checkout 内或穿过 reparse point。
- 生成配置前固定 `EVEROS_HOME`、`EVEROS_INSTALL_HOME` 和显式 `CODEX_HOME` 的绝对路径；绝对化保留 reparse point 的词面组件，检查前不调用 `resolve()` 隐藏 junction。跨工作目录启动时仍使用同一数据根。
- Codex 命令先解析实际 CLI 路径；支持 `.exe` 与 npm `.cmd`/`.bat` shim。批处理参数经独立环境槽一次展开，关闭 delayed expansion，不把路径或参数拼入 shell 程序正文。
- Windows 独立实现唯一备份、原子替换与 EverOS 原生规则管理块；同步后回读 Codex 全局入口，只管理当前宿主。
- 将插件生成到 `.everos/runtime/windows-<内容摘要>/`，以该快照注册本地插件市场。hook 的 `commandWindows` 使用编码的 PowerShell 命令，绑定绝对 Python、hook 和数据路径；所有 Python 入口启用 UTF-8。每个快照版本独立，旧快照和旧插件缓存保留给正在运行的聊天。
- MCP 使用同一快照和同一 `EVEROS_HOME`。提问 hook 与 MCP 的外层超时至少为 `EVEROS_AGENTIC_TIMEOUT + 10` 秒，保留更大的现有 MCP 值。
- `PROTOCOL.md` 以普通文件副本提供稳定入口；只读 CLI 安装为 ASCII `everos.cmd` 与 UTF-8 BOM `everos.ps1`，不需要符号链接。batch 用 `%~dp0` 定位同目录脚本，避免代码页解码非 ASCII 路径；PowerShell 使用明确的 Windows argv 引用保留空参数、引号和末尾反斜杠。命令目录未在 PATH 时可以使用完整路径。
- 运行真实只读 doctor；失败返回非零。没有凭据、没有加载新会话或尚未信任 hook 时，不能将接线完成等同于完整验收。

```powershell
.\install.ps1 -CliOnly          # 不改凭据/宿主，不访问 API
.\install.ps1 -RulesOnly        # 只刷新 Codex 原生规则，不访问 API
.\install.ps1 -DryRun           # 不创建文件、不启动宿主 CLI、不访问 API
& "$env:USERPROFILE\.local\bin\everos.cmd" search '历史问题' --method agentic
```

更新源码后重跑安装器。新会话按 Codex 的提示审阅并信任当前 hook 定义；安装器不写信任记录，也不绕过信任。随后分别验证画像注入、每条提问检索和真实一轮对话采集。旧聊天不会自动重载新的 MCP 工具目录。

Linux 保持原有 `fcntl.flock` 和打开方式。Windows 生成副本使用 `windows/windows_lock.py` 的 `msvcrt.locking` 首字节排他锁和追加打开方式。非阻塞冲突统一返回 `BlockingIOError`；阻塞获取等待持有者释放；持有进程关闭文件或退出时由系统释放。

功能逐项对齐：

| 功能 | Windows 验证 | Linux 边界 |
| --- | --- | --- |
| L1 MCP：9 个工具、参数校验、stdio、正文展开 | 在 Windows 副本运行未修改的 `test_mcp.py` | 原 MCP 与全部测试不变 |
| L2：配置、备份、规则管理块、回读 | Windows 独立配置与规则测试 | 原安装器与规则源不变 |
| L3：画像、每轮 agentic、回退、去重与总预算 | 未修改的 `test_core_regressions.py` 加 Windows hook 命令测试 | 原 hook 与召回核心不变 |
| L4：增量采集、失败保留偏移、禁写开关、flush | 同一组核心回归，加 Windows 后台 worker loopback 验收 | 原采集与后台启动方式不变 |
| v1/v2、做法校验与回读、配置优先级 | 同一组核心/MCP 回归 | 共用业务源只读 |
| 手动 search/open CLI | Windows launcher 安装、帮助和调用 | Unix launcher 不变 |
| 进程锁、ACL、Unicode 路径、旧缓存保留 | Windows 专项测试 | 全部属于 Windows 独立目录 |

离线验证：

```powershell
$env:PYTHONUTF8 = '1'
python -X utf8 -m unittest discover -s windows/tests -v
```

Windows 测试仅使用临时目录、合成消息及本机 loopback API；将未修改的核心/MCP 测试复制到临时 Windows 副本运行，不改原测试或降低其断言。Linux 继续运行原命令 `python3 -m unittest discover -s tests -v` 和 `bash -n install.sh`，不加载 Windows 目录。宿主内新会话信任与真实采集仍须单独验收。
