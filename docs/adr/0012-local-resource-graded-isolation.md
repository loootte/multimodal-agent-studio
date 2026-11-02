# ADR 0012：本地资源分级隔离（第 15 阶段）

## 状态

提议（草稿）。对应路线图第 15 阶段父 issue [#109](https://github.com/loootte/multimodal-agent-studio/issues/109)，路线图写入 [#103](https://github.com/loootte/multimodal-agent-studio/issues/103)。子 issue [#105](https://github.com/loootte/multimodal-agent-studio/issues/105)–[#108](https://github.com/loootte/multimodal-agent-studio/issues/108)。依赖 [ADR 0003](0003-local-data-isolation.md)、[ADR 0009](0009-handle-scope-and-egress-audit.md)、[ADR 0011](0011-external-capabilities-isolation.md)。

本文件随 15.C（[#107](https://github.com/loootte/multimodal-agent-studio/issues/107)）先落库，因用户选择先做文字可控披露；**15.A 媒体工具实现前仍以本决定为准**，媒体细则可在 15.A 合并时补一节实现说明，状态仍保持提议直到阶段完成标准达成。

## 背景

第 12 阶段已把「第三方模型默认只看见工具契约 + 不透明句柄」做成底线。继续做工具应用时需要分级：

- **文字**若完全盲盒，校验、摘要、结构化提取和一致性检查几乎无法用模型能力。
- **图片 / 音频 / 视频**隔离价值更高（版权、隐私、原始像素 / 波形），更适合本机理解后再把描述或标签交给模型。

本决定规定：文字可控披露与媒体强隔离各自约束哪些出站路径；不放松第 12 阶段已有机制。

## 决定（提议）

### 分级原则

| 资源 | 默认 | 打开 / 派生路径 | 原始内容进第三方 |
| --- | --- | --- | --- |
| 文字 | 句柄 | 用户确认 + 审计 + purpose-scoped 消费；优先本机校验 / 抽取 / 摘要，只把脱敏或结构化结果交给对话模型 | 默认否；仅经「打开盲盒」且确认后，且只限宿主放进工具结果的那一段（见 15.C） |
| 图片 / 音频 / 视频 | 句柄 | 本机理解（描述、嵌入、OCR、转写摘要）、受控生成与变体；模型只见描述 / 标签 / 新句柄 | **否**（原始字节默认不进任何第三方；导出需用户明确允许） |

### 文字披露约束的出站路径

- **`chat_completion`**：未确认时，已登记正文、文件字节、入库指纹仍按 12.C / 12.G 拦截。确认后的工具结果里只允许宿主生成的脱敏 / 结构化字段（及可选的短预览）；**完整正文默认仍不进供应商请求**（完整正文披露列为待用户决定的放宽，见下）。
- **`remote_image` / `comfyui_local`**：继续只经 `image_prompt` 句柄与现有 `remote_local_prompt`（默认 `refuse`）；文字「打开盲盒」**不**自动放开出图通道。
- **`toolpack` / MCP**：第三方包仍拿不到本地正文；`send_local_to_model` 权限保持硬禁止（[#93](https://github.com/loootte/multimodal-agent-studio/issues/93)）。
- **确认卡 / 审计 / 遥测**：只含主机、用途、句柄前缀、模式、字数等元数据，**永不**含正文。

### 媒体隔离约束的出站路径

- **`chat_completion`**：不得出现原始媒体字节、data URL 像素 / 波形、未脱敏的完整 OCR / 转写正文（除非另有与文字同等的确认披露——本阶段不做媒体原文披露）。
- **`remote_image` / `comfyui_local`**：参考图 / 变体以本机或用户已授权的句柄消费；原始文件导出独立确认。
- **`toolpack`**：媒体句柄对外部 server 不透明；本机理解工具在宿主或受信内置路径执行，结果只回公开字段或新句柄。

### 职责不重叠（沿用 #93）

- **`send_local_to_model`**：只回答「能不能把本地内容交给第三方模型」——清单层硬禁止为 true。
- **`content_guard`**：只回答内容是否 / 如何按露骨、未成年、真人、版权规则处理；不承担出站开关。
- **文字打开盲盒**：由宿主工具（15.C 的 `disclose_local_text`）在确认后**自己**生成脱敏 / 结构化工具结果；不新增 `send_local_to_model=true` 的包。

### 本机能力与延后

- 15.C 使用**现有本机能力**（长度 / 结构统计、正则抽取与占位符脱敏、截断预览），不引入新的本地小模型推理栈。
- **完整本地小模型推理栈**（本机 LLM / VLM 服务、模型下载与 GPU 编排）明确延后；需要时另开 ADR 或在本决定接受时补节。
- **可逆伪名化代理**（占位符发出、回复后再映射）：若实现成本低可做邮箱 / 电话级替换；完整双向代理列为缺口，见 15.C 文档。

### 不变量（不因本阶段改变）

- 一次 `generate_image` 出一张；张数只由 `plan_images` 决定。
- 单 Agent 默认；子 Agent 拿不到收下本地内容 / 打开盲盒类工具。
- `remote_local_prompt` 默认 `refuse`。
- 不放宽未成年、出站策略、句柄作用域、沙箱。

## 后果

- 文档：[docs/local-resources.md](../local-resources.md)、安全清单补充「打开盲盒」条目；实现说明随 15.C / 15.A 写入。
- 15.A 落地第一个媒体理解工具时，在本文件追加「媒体实现说明」小节，并更新评测基线。
- 任何把**完整**本地正文默认或一键送进 `chat_completion` 的设计，必须先经用户明确选择，不能由实现方自行放宽。

## 15.C 实现说明（#107，本分支）

- 内置宿主工具 `disclose_local_text`：`confirm=always`，每次都问；参数 `content_handle` + `mode`（`validate` / `extract` / `summarize`）。
- 确认卡为 `mas.confirm/1`；拒绝记 `security.disclose_denied`，结果 `rejected`，正文不进任何供应商请求。
- 同意后本机处理：`validate` 只回统计；`extract` 只回结构化标签与占位符；`summarize` 可回短预览（过短则拒绝预览以免整段正文出门），并签发 `purpose=disclose` 的句柄指向脱敏预览。
- `summarize` 因会把预览放进工具结果（进而进下一轮 `chat_completion`），确认卡标高危并要求 `acknowledge=high_risk`。
- 评测：`security.disclose_confirm` / `security.disclose_denied` / `security.disclose_no_leak`（及 check 单元）。
