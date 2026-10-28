# 出图技能配置

技能说明见 [skills/image-gen/SKILL.md](../skills/image-gen/SKILL.md)，决定见 [ADR 0008](adr/0008-image-skill.md)。

## 第三方图片 API（OpenAI 兼容）

在 设置 → 工具 → generate_image 填写，或用环境变量（优先级更高）：

| 字段 | 环境变量 | 默认 | 说明 |
| --- | --- | --- | --- |
| `remote_base_url` | `MAS_IMAGE_API_BASE` | 空 | 例如 `https://api.openai.com/v1`，会请求 `{base}/images/generations` |
| `remote_api_key` | `MAS_IMAGE_API_KEY` | 空 | 密钥，界面不回显 |
| `remote_model` | `MAS_IMAGE_API_MODEL` | `gpt-image-1` | 例如 `dall-e-3` |
| `remote_local_prompt` | — | `refuse` | 本地提示词（1.3）能否发第三方：`refuse` / `confirm`（每轮弹窗确认）。改成 `confirm` 要在设置页再确认一次（接口要带 `acknowledge`，否则 409），并记 `security.policy_changed`（12.E） |
| `remote_timeout_sec` | — | `120` | 5–600 |

PowerShell 示例：

```powershell
$env:MAS_IMAGE_API_BASE="https://api.openai.com/v1"
$env:MAS_IMAGE_API_KEY="<你的 key>"
python apps/web/server.py
```

## 怎么试

- 「画一只茶壶」→ 默认：本地提示词 + 本机 ComfyUI + 1 张。
- 「直接用这句话出图：月光下的茶壶，水墨风」→ 1.1 聊天原文。
- 「帮我优化提示词，画三张雨夜街景」→ 1.2 模型撰写，3 张。
- 「用第三方模型，原样用这句：赛博朋克猫」→ 1.1 + 第三方。
- 「用第三方模型画」并在弹窗里输入本地提示词 → 默认拒绝；`remote_local_prompt=confirm` 时先确认再发。同意只管这一轮里同一个句柄发给同一个主机，换句柄、换主机或下一轮都重新问；确认卡固定写出站主机、含本地派生、句柄用途。
