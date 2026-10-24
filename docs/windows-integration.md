# 原生 Windows：Codex CLI / App

Windows 使用 PowerShell 安装入口，目前只配置 Codex。其他宿主继续按各自文档接入；安装器不因为发现其目录就宣称支持。无需 WSL、Git Bash、管理员权限、符号链接或 pip 依赖。Python 需要 3.9 以上，Codex CLI 需要支持插件和 `commandWindows` hook。

```powershell
git clone https://github.com/namelessssssssssss/everos-bridge.git "$env:USERPROFILE\.everos-bridge"
Set-Location "$env:USERPROFILE\.everos-bridge"
.\install.ps1 -DryRun
.\install.ps1
```

未自动找到 Python 时，给 `-Python` 传实际 `python.exe` 的绝对路径；安装器会探测解释器版本并跳过 Windows Store 的占位入口。凭据沿用环境变量或 `$EVEROS_HOME/config.env`；缺失时在交互终端输入，API key 隐藏输入。不要在提交、文档或命令示例里填实际值。

安装动作：

- 创建或复用 `%USERPROFILE%\.everos`（可用 `EVEROS_HOME` 覆盖），移除目录的继承 ACL，并为当前用户和 SYSTEM 授权；拒绝数据根位于源码 checkout 内或穿过 reparse point。
- 复用安装器的唯一备份、原子替换与 EverOS 原生规则管理块；同步后回读 Codex 全局入口，只管理当前宿主。
- 将插件生成到 `.everos/runtime/windows-<内容摘要>/`，以该快照注册本地插件市场。hook 的 `commandWindows` 使用编码的 PowerShell 命令，绑定绝对 Python、hook 和数据路径；所有 Python 入口启用 UTF-8。每个快照版本独立，旧快照和旧插件缓存保留给正在运行的聊天。
- MCP 使用同一快照和同一 `EVEROS_HOME`。提问 hook 与 MCP 的外层超时至少为 `EVEROS_AGENTIC_TIMEOUT + 10` 秒，保留更大的现有 MCP 值。
- `PROTOCOL.md` 以普通文件副本提供稳定入口；只读 CLI 安装为 `.local/bin/everos.cmd` 和 `everos.ps1`，不需要符号链接。命令目录未在 PATH 时可以使用完整路径。
- 运行真实只读 doctor；失败返回非零。没有凭据、没有加载新会话或尚未信任 hook 时，不能将接线完成等同于完整验收。

```powershell
.\install.ps1 -CliOnly          # 不改凭据/宿主，不访问 API
.\install.ps1 -RulesOnly        # 只刷新 Codex 原生规则，不访问 API
.\install.ps1 -DryRun           # 不创建文件、不启动宿主 CLI、不访问 API
& "$env:USERPROFILE\.local\bin\everos.cmd" search '历史问题' --method agentic
```

更新源码后重跑安装器。新会话按 Codex 的提示审阅并信任当前 hook 定义；安装器不写信任记录，也不绕过信任。随后分别验证画像注入、每条提问检索和真实一轮对话采集。旧聊天不会自动重载新的 MCP 工具目录。

运行时锁在 Unix 使用 `flock`，Windows 使用 `msvcrt.locking` 的首字节排他锁。非阻塞冲突统一返回 `BlockingIOError`；阻塞获取等待持有者释放；持有进程关闭文件或退出时由系统释放。锁文件以追加模式打开，不能因另一进程打开锁文件而截断。

离线验证：

```powershell
$env:PYTHONUTF8 = '1'
python -m unittest discover -s tests -v
```

测试仅使用临时目录、合成消息及本机 loopback API。Windows 跳过 Unix Bash 安装和未适配宿主的用例；保留其 Unix 回归测试，Windows 单独覆盖跨进程互斥、中文及特殊字符路径、stdin 传递、ACL、只读预览、快照完整性和 CLI 冲突。
