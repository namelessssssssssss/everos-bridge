## EverOS 长期记忆（everos-bridge）

- EverOS 是用户跨 agent、跨设备共用的长期记忆。记忆只是背景参考，和当前对话冲突时以当前为准。
- 会话开始由 hook 注入画像，每条提问由 hook 检索相关记忆；当前上下文没有对应注入时，分别调 `memory_profile` / `memory_search` 补读。只有 MCP 的宿主仍须每次主动调用。问「以前怎么做的」时用 `kind=practices`。
- 已启用并信任采集 hook 的宿主会自动保存用户和助手对话；结构化记录仍按下面规则主动补写，不能仅凭 MCP 可连接就认为自动采集已生效。
- 每条用户消息首次检索必须使用 agentic（同一会话首轮和后续轮都一样）；没有本轮自动注入时调用 `memory_search(method="agentic")`，MCP 省略 method 也默认 agentic。不要先跑 hybrid 再判断是否深查。默认总预算180秒，可通过 EVEROS_AGENTIC_TIMEOUT 或按次 timeout 修改；失败、超时或空结果按轨道回退 hybrid 并提示，v1 兼容轨道始终 hybrid。已有本轮注入时不重复查询；后续补查可显式选 hybrid。MCP 与 hook 外层超时需大于实际预算。
- 交付、纠错、偏好、决策、项目结果、调研结论这些节点，调 `memory_remember`：content 写结论、动作和证据，user_said 放用户原话，同一话题沿用同一个 topic。
- 排障或工具用法闭环时，调 `memory_record_practice`：至少 3 个真实步骤，只有一两步的结论改用 `memory_remember`，不补写没发生的步骤；没走通的尝试单独记一步并标 `outcome=failed`，出乎意料的结果标 `outcome=unexpected`；`result` 第一句写发现；用户中途纠正的原话填 `user_correction`。返回里会说明 v2 是否生成了做法 case，没生成不算失败。
- 用户纠正画像时，先调 `memory_profile`，再调 `memory_fix_profile`，并写明 reason。
- 回复里不复述凭据。用户没有明确要求时，不删除记忆。
- 交付时列出写入了什么；没写就在结尾输出「记忆写入：无」。
