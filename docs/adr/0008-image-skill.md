# ADR 0008：可配置出图技能（提示词来源 × 后端 × 张数）

## 状态

已接受。对应 [issue #49](https://github.com/loootte/multimodal-agent-studio/issues/49)。依赖 [ADR 0003](0003-local-data-isolation.md)、[ADR 0004](0004-tool-governance.md)。

## 背景

此前出图只有一条路：聊天里的出图请求被服务端拦下，正文存成本地提示词（1.3），模型只见句柄，交给本机 ComfyUI。#49 要求三个维度可配置：提示词来源（1.1 聊天原文、1.2 模型撰写/优化、1.3 本地）、后端（本机 / 第三方 API）、张数（默认 1），且 1.3 对第三方模型始终不可见。

## 决定

### 技能是声明，工具是执行

- `skills/image-gen/SKILL.md` 只写决策表、歧义规则、隐私和错误码；执行全靠工具，不新增隐式代码路径。
- 张数只由模型通过 `plan_images` 报告，服务端不做正则数张数。

### 句柄统一

- `generate_image` 的契约仍然只收句柄（`content_handle`），不加 `prompt`、`count`、`n`，现有契约测试不变。
- 新增宿主工具 `stage_prompt(visible_prompt, source=chat|model)`：把**模型本来就看得见**的文字登记成本轮句柄（`run._staged`，只在内存、只活一轮，不写会话内容，因此不会进入网关的本地正文黑名单而误杀后续请求）。
- `visible_prompt` 参数名是对 `BLOCKED_EXTERNAL`（禁止 `text`/`prompt` 等参数名）的**有意例外**：这个工具的输入本来就是模型可见的文字，不是本地正文。为防“洗白”，服务端拒绝与本会话任何本地正文相同或互相包含的文字（`local_content`）。
- 1.3 仍用 `accept_local_content` 或服务端拦截得到的本地句柄。

### 路由闸门（隐私优先）

- 默认行为不变：出图请求的聊天正文当本地提示词，不发给对话模型。
- 只有用户明说“优化提示词/原样/用我刚才说的/第三方/云端/openai…”时，闸门放行，消息进入模型循环，由模型按技能决定来源、后端、张数。闸门只决定“聊天正文能不能给模型看”，不替模型选维度。

### 第三方后端

- OpenAI 兼容 `POST {base}/images/generations`，`n=1`、一次一张；配置在 `generate_image` 工具包：`remote_base_url`（`MAS_IMAGE_API_BASE`）、`remote_api_key`（密钥，`MAS_IMAGE_API_KEY`）、`remote_model`（默认 `gpt-image-1`）、`remote_timeout_sec`、`remote_local_prompt`。优先级：环境变量 > 已保存 > 默认。
- 错误统一为 `remote_not_configured`、`remote_missing_key`、`remote_auth`、`remote_unreachable`、`remote_error`，都带 `hint`，消息里不出现 key。

### 1.3 + 第三方

- `remote_local_prompt=refuse`（默认）：直接拒绝（`local_prompt_remote_refused`），提示改用本机或 1.1/1.2。
- `confirm`：每轮第一次需要时弹出专门的确认（带说明：正文发给第三方出图服务，不发给对话模型），确认后本轮不再问；拒绝返回 `user_declined`。
- 无论哪种，本地正文都不进入对话模型请求；网关的本地正文拦截仍是最后一道防线。

### 输出

- 每轮有出图尝试时，服务端追加一个 `image_config` 事件（`prompt_source`、`backend`、`count`、`planned`、`summary`），界面在消息下显示一行“本次配置：…”。放在单独事件里而不是改写回复文字，避免影响模型的原话。

## 后果

- 默认体验和隐私边界不变；需要 1.1/1.2/第三方时用户明说即可。
- 第三方出图需要用户自己提供 OpenAI 兼容地址和 key。
- 闸门是关键词表，漏判时退回默认（1.3 + 本机），只会更保守。
