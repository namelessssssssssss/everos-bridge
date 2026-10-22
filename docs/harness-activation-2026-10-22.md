# 跨 harness 接线与验收（0.8.1，2026-10-22）

范围是bridge已经支持的harness，不包含尚未入仓的第三方云端适配。统一的八工具、问题驱动补查规则和正文展开由共享Python核心提供；宿主可用事件不同，不把MCP连通、配置已落盘或客户端名称相同当作真实会话行为证据。

## 补齐的接线

- Cursor已安装而首次mcp.json不存在时，现在创建全局MCP配置并同步个人everos-memory skill；已安装Gemini及Claude Desktop也补齐首次文件缺失的情况。保留其他配置，修改前备份。
- CodeX/Trae CLI使用其实时help确认支持的--env，JSON MCP客户端使用env，MiMo使用其SDK定义的environment，显式绑定同一个EVEROS_HOME。只保存非秘密目录，不将API key写到客户端配置。旧CLI不支持env且使用非默认home时明确阻断，而不先删除已有MCP。
- MiMo 0.1.15实际Plugin API的tool.execute.before/after已接入无网络共享提醒；before提示在下次模型请求注入，after提示追加工具输出。压缩事件清除本机覆盖并恢复规则，不篡改或取消工具输入，不替换压缩prompt。
- TraeX 0.210.1真实hooks/list与模型执行确认支持八类事件；按版本接入两个工具事件，较旧版本保留六类生命周期和共享规则。
- 旧聊天工具目录仍只有六工具时，skill明确使用现有CLI search/open临时只读补查，并要求重连或新会话；不把旧目录当作已经热更新。

## 验收层级

| harness | 本次证据 | 边界 |
| --- | --- | --- |
| Claude Code | 真CLI＋隔离Anthropic固定模型网关，真实调用memory_search与memory_open，正文回到模型 | 验证调度与协议，不评价实际模型检索决策 |
| Codex CLI / App | 前轮原生Codex工具事件与20次合成试跑；本轮实际配置入口再做八工具/展开/恢复验证 | App旧会话目录需新会话或重连 |
| Kimi Code | 真CLI、其当前模型、真实搜索和展开正文验收标记 | 实际工具调用从role/tool_calls/function结构核实，不仅看退出码或最终文本 |
| Kimi Work桌面端 | 实际runtime home的MCP入口、hook与daimon skill同步核验，八工具与展开链路 | 不将此写成GUI模型会话已逐项实测 |
| MiMoCode / MiMo Desktop | 真MiMo CLI＋隔离OpenAI兼容固定网关，真实搜索/展开；Node调用实际Plugin回调与Python桥接验证提醒、恢复 | Desktop共享配置；旧Plugin实例需重载 |
| TraeX / 当前coco别名 | 真TraeX 0.210.1、原生hook信任API、Code Mode嵌套MCP调用、L3注入、L4采集与展开正文到模型 | 本地固定模型，不依赖真实默认模型账户 |
| 旧Coco | MCP＋规则的安装回归与共享协议 | 不宣称旧版有TraeX新事件或采集能力 |
| Claude Desktop / Cowork | 读取实际MCP配置，启动其真实stdio程序做八工具/展开/恢复 | 运行中的Desktop仍按既有规则保护；需应用重载后使用新目录 |
| Cursor | 首次配置与skill补齐、安装回归、配置入口验收 | 官方要求重启以加载MCP；不宣称测试了当前GUI聊天 |
| Gemini CLI | MCP配置入口与首次安装回归 | 本机没有该CLI时只报告已配置/协议就绪，不报告真实Agent实测 |
| Claude Code on the web | 共享核心在Linux CI与云端模板、同Claude协议入口兼容 | 未取得活动远端环境，不宣称当前远端旧会话已经更新 |

已配置入口的`audit_harnesses.py --exercise`仅启动实际配置程序，并用本地合成API检查initialize、tools/list、search、open、context recover。输出明确native_client_verified=false。机器实际路径、模型/账号状态与原始轨迹仅保存在仓外本机；本文件不发布这些配置或真实记忆内容。

原生协议验收使用新home和固定本地模型时不使用真实模型凭据，验证的是客户端调度，而不是模型能力。所有外部默认模型失败与固定网关成功分别保存，未把外部依赖错误算作bridge质量零分。早期Trae隔离试跑发现MCP没有继承父环境，曾读取默认记忆库而失败；该次没有写入用例，输出留本机，不作通过证据。此后显式环境与非loopback请求拦截均在最终验证中启用。

## 复验

```bash
python3 -m unittest discover -s tests -v
python3 scripts/audit_harnesses.py --exercise --output /tmp/everos-harness-audit.json
python3 scripts/validate_clients.py --host claude --out /tmp/new-claude-probe
python3 scripts/validate_clients.py --host mimo --out /tmp/new-mimo-probe
python3 scripts/validate_trae.py --out /tmp/new-trae-probe
```

原生验证目标目录必须是新目录，实际轨迹保留仓外。安装更新后应重新运行入口审计；活动会话的已缓存定义、未安装客户端和未提供的云端运行环境不能从配置文件反推已经生效。

参考：[Cursor全局MCP与重启](https://prod.cursor.com/help/customization/mcp)、[Cursor个人skill路径](https://prod.cursor.com/help/customization/skills)。MiMo事件与environment字段按本机已安装的官方SDK类型核对；Trae事件按真实宿主RPC与触发结果确认。
