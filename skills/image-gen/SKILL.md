---
name: image-gen
description: 可配置的出图流程。用户要生成图片时，按三个维度决定怎么出图：提示词来源（聊天原文 / 模型撰写或优化 / 本地提示词）、出图后端（本机 ComfyUI / 第三方 OpenAI 兼容图片 API）、张数（默认 1）。用于任何“画一张”“生成图片”“出图”“用第三方模型画”“帮我优化提示词再出图”之类的请求。
---

# 出图技能（image-gen）

这份技能只声明“怎么选”，真正执行的是工具：`stage_prompt`、`accept_local_content`、`plan_images`、`generate_image`。隐私规则见 [ADR 0008](../../docs/adr/0008-image-skill.md)。

## 1. 三个维度

| 维度 | 取值 | 怎么做 |
| --- | --- | --- |
| 提示词来源 | 1.1 聊天原文 | 用户明说“直接用这句/原样/用我刚才说的”。调用 `stage_prompt`，`source=chat`，`visible_prompt` 填用户原话（不改写）。 |
| | 1.2 模型撰写/优化 | 用户说“帮我写/优化/润色/扩写提示词”。你先写好提示词，再调用 `stage_prompt`，`source=model`。 |
| | 1.3 本地提示词 | **默认**。调用 `accept_local_content`（`purpose=image_prompt`，`kind=text`），正文留在用户本机，你只拿到句柄。 |
| 出图后端 | 2.1 本机 ComfyUI | **默认**。`generate_image` 不填 `backend`（或填 `local`）。 |
| | 2.2 第三方图片 API | 用户要“第三方/云端/在线模型/OpenAI/gpt-image”出图时，`backend=remote`。 |
| 角色定制 | 画一个角色/人物 | 用户描述角色（性别、年龄、人种、体型、发型、发色、瞳色、服装、姿势、背景、画面风格、参考已有角色）时调用 `character_image`，只填说到的字段，其余用默认值（年轻东亚女性、二次元风格）；返回的 `content_handle` 再交给 `generate_image`。见 [character-image](../../docs/character-image.md)。 |
| 张数 | 默认 1 | 用户说了几张就先 `plan_images`（`count`）；没说就出 1 张。张数只由 `plan_images` 决定，每次 `generate_image` 只出一张。 |

## 2. 歧义怎么处理

- 来源或后端说不清时：按默认（1.3 + 本机 ComfyUI + 1 张）直接做，回复里说明用了默认值、怎么换；
  或者先用**一句话**确认（例如“用你这句话原样出图，还是我帮你优化一下？”），不要连问多轮。
- “帮我画一只猫”这类没提来源的请求，服务端默认把聊天正文当本地提示词处理，不发给你。
- 只有用户提到“优化提示词 / 原样 / 第三方”等明确说法时，你才会看到聊天正文并按上表决定。

## 3. 隐私（必须遵守）

- 1.3 本地提示词的正文**永远不进入对话模型请求**；你只见句柄，网关会拦下任何带本地正文的请求。
- 不要把本地正文抄进 `stage_prompt`：服务端会拒绝（`local_content`）。
- 1.3 + 第三方后端：默认拒绝（`local_prompt_remote_refused`）。用户在 设置 → 工具 → generate_image 把 `remote_local_prompt` 设为 `confirm` 后，每轮会先弹窗请用户确认，确认后正文只发给第三方图片服务，仍不发给对话模型。

## 4. 输出

- 成片会作为工件显示在消息里；服务端会附上一行“本次配置：提示词来源 … · 出图 … · 张数 …”。
- 你的回复用一句话说明出了几张、用了哪种来源和后端；用了默认值时说清楚。

## 5. 错误

`generate_image` 失败时结果里有 `error.code`、`error.message`，通常还有 `error.hint`。把 hint 照实告诉用户，不要编造成功。

| code | 含义 | 建议 |
| --- | --- | --- |
| `remote_not_configured` | 没配第三方地址 | 设置 `MAS_IMAGE_API_BASE` 或在工具设置里填 `remote_base_url` |
| `remote_missing_key` | 没配 API key | 设置 `MAS_IMAGE_API_KEY` 或在工具设置里填 `remote_api_key` |
| `remote_auth` | key 被拒 | 检查 key 和模型权限 |
| `remote_unreachable` | 连不上第三方服务 | 检查网络/地址，或改用本机 ComfyUI |
| `remote_error` | 第三方返回错误 | 看 message，换模型或稍后重试 |
| `local_prompt_remote_refused` | 本地提示词不能发第三方 | 用本机 ComfyUI，或改用 1.1/1.2 |
| `comfyui_error` | 本机 ComfyUI 出错 | 确认 ComfyUI 已启动，或改用第三方 |
