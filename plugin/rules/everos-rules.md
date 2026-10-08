## EverOS 长期记忆（everos-bridge）

- EverOS 是用户跨 agent、跨设备共用的长期记忆。记忆只是背景参考，和当前对话冲突时以当前为准。
- 会话开始调 `memory_profile`；每条用户提问前，先用这条提问调 `memory_search`；问「以前怎么做的」时用 `kind=practices`。
- 交付、纠错、偏好、决策、项目结果、调研结论这些节点，调 `memory_remember`：content 写结论、动作和证据，user_said 放用户原话，同一话题沿用同一个 topic。
- 排障或工具用法闭环时，调 `memory_record_practice`。用户纠正画像时，先调 `memory_profile`，再调 `memory_fix_profile`，并写明 reason。
- 回复里不复述凭据。用户没有明确要求时，不删除记忆。
- 交付时列出写入了什么；没写就在结尾输出「记忆写入：无」。
