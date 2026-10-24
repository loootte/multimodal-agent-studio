# 路线图

顺序固定。上一阶段的完成标准没达到，不开始下一阶段的功能。每一阶段只做列出的范围。

[issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 按 [ADR 0002](docs/adr/0002-implementation-stack.md) 提前做了文字聊天和第三方 API key 这一刀。[issue #10](https://github.com/loootte/multimodal-agent-studio/issues/10) 让这个聊天页调用已有的文生图工具。[issue #11](https://github.com/loootte/multimodal-agent-studio/issues/11) 让模型调用 `accept_local_content`，画面描述留在本机，第三方只看到工具契约。[issue #12](https://github.com/loootte/multimodal-agent-studio/issues/12) 把本地内容扩成语义唤起、文字或上传文件，并用专用工具列出和删除。这四条都不计入下面任一阶段的完成。[issue #15](https://github.com/loootte/multimodal-agent-studio/issues/15) 补齐第 1 阶段的网关。[issue #18](https://github.com/loootte/multimodal-agent-studio/issues/18) 补齐第 2 阶段的运行时。[issue #20](https://github.com/loootte/multimodal-agent-studio/issues/20) 补齐第 3 阶段的网页。[issue #22](https://github.com/loootte/multimodal-agent-studio/issues/22) 补齐第 4 阶段的工件库、检索和会话记忆。[issue #25](https://github.com/loootte/multimodal-agent-studio/issues/25) 把 Anatomy 装进这个 Agent。它不完成第 5 或第 6 阶段，也不开始插件市场。

## 0. 工程基线

交付物是这个仓库里的章程、架构、界面范围、模型契约和目录约定。

完成标准：

- 读完 README 能说出产品是什么、不是什么。
- 架构文档能指出一次用户请求经过哪些层。
- 模型契约写明文字、图像、音频、视频各自怎么进、怎么出。
- 仓库里没有产品应用源码。`poc/comfyui` 是 issue #1 的本地脚本，不算第 1 阶段的网关。

状态：已完成。交付物是章程、架构、界面范围、模型契约和目录约定。issue #9 起提前放入的 `apps/web`、`services/gateway` 和 `packages/contracts` 是后续阶段的切片，不回改这一阶段。

## 1. 模型网关

做一个供应商中立的网关，先接一个供应商，接口按多模态设计。

范围：

- 统一请求：系统指令、对话、工具模式、模态部件。
- 能力声明：每个模型支持哪些输入模态、是否流式、上下文上限。
- 文字流式输出。
- 图像输入。音频输入和视频输入先定义契约，可以用明确的「未实现」错误拒绝，不能静默丢掉。
- 超时、取消、可重试错误和不可重试错误分开。

完成标准：

- 同一条应用侧请求，换模型标识就能发给另一个已接入的适配器。
- 不支持的模态返回稳定错误码，界面以后能直接显示。
- 密钥只来自环境或密钥管理，不进仓库。

状态：已完成。[issue #15](https://github.com/loootte/multimodal-agent-studio/issues/15) 补齐统一请求、能力声明、图像输入和错误分类。网页附件由 [issue #20](https://github.com/loootte/multimodal-agent-studio/issues/20) 接上。

## 2. Agent 运行时

一个循环：观察、决定、调用工具或回答、记录、直到结束或碰到预算。

范围：

- 步数、时间和 token 预算。
- 工具用 JSON Schema 描述，调用参数先校验再执行。
- 会改外部状态的工具默认需要人确认。
- 每次运行有追踪：模型调用、工具调用、输入模态、耗时。
- 取消后不再发新的模型或工具请求。

完成标准：

- 用一个只读演示工具能跑完「模型决定调用 → 工具返回 → 模型回答」。
- 超过步数预算时停下来，并留下可读的停止原因。
- 追踪能按一次运行串起来。

状态：已完成。[issue #18](https://github.com/loootte/multimodal-agent-studio/issues/18)。网页确认按钮由 [issue #20](https://github.com/loootte/multimodal-agent-studio/issues/20) 接上。

## 3. Web 界面

首版是 Web，不是桌面壳，也不是只给开发者看的调试页。

范围见 [docs/interface.md](docs/interface.md)。最低包括：会话、带附件的输入、流式回答、工具时间线、失败后的重试。

完成标准：

- 不刷新页面能完成一轮带图片的对话。
- 工具调用在回答旁边按时间出现，能看到名字、状态和失败原因。
- 键盘能完成发送；上传失败有明确提示。

状态：已完成。[issue #20](https://github.com/loootte/multimodal-agent-studio/issues/20)。会话列表可删除对话，回答中的会话拒绝删除。真正的账号留给第 6 阶段。

## 4. 工件、记忆和检索

范围：

- 上传文件进工件库，按标识引用。
- 会话记录和工件分开存。
- 检索是一个工具，不是隐藏在系统提示里的副作用。
- 会话级记忆只保留这份会话需要的摘要和引用。

完成标准：

- 同一张图在同一会话里第二次被提到时，不再整文件重复上传。
- 检索结果带来源标识，界面能点开。

状态：已完成。[issue #22](https://github.com/loootte/multimodal-agent-studio/issues/22)。

## 5. 评测、安全和可观测性

范围：

- 每种已支持的模态至少一组固定任务。
- 工具权限和提示注入的检查清单。
- 追踪、延迟、token 和失败率接到同一套遥测。
- 发布前跑固定任务。退步则不发。

完成标准：

- 有一份能重复跑的评测入口，输出通过与失败。
- 一次线上风格的运行能在追踪里看到模型、工具和界面请求是同一次。

## 6. 生产化

范围：

- 登录和基本的租户隔离。
- 速率限制和每个租户的花费上限。
- 部署、健康检查、备份和恢复步骤写进运行手册。
- 可用性目标：界面和运行时分开扩容。

完成标准：

- 一个新环境只靠文档和环境变量能启动。
- 密钥轮换不需要改代码仓库里的文件。

## 7. 统一工具封装

[issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31)。建立在 [issue #28](https://github.com/loootte/multimodal-agent-studio/issues/28) 的工具配置之上。

范围：

- 每个工具按同一份清单封装：名字、说明、版本、参数 JSON Schema、是否改外部状态和确认策略、配置声明（含密钥）、依赖的后端、权限。
- 内置工具全部迁到清单，运行时只从清单注册。
- 共享后端（例如 ComfyUI 地址）单独声明，工具按依赖引用。
- 设置对话框按实际工具名列出工具。
- 工具包能像 `skills/` 一样从目录安装和分享。不做插件市场。

完成标准：

- 下拉框列的是 `plan_images`、`generate_image` 这样的工具名，不是后端名。
- 一个工具包目录拷到另一份仓库能装上并调用，配置和密钥不随包走。

状态：已完成。规范见 [docs/tool-packs.md](docs/tool-packs.md)。延后：沙箱隔离、MCP 投影字段、市场和签名、上传安装。

## 8. 多智能体

[issue #34](https://github.com/loootte/multimodal-agent-studio/issues/34)。建立在第 7 阶段（[issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31)）的工具清单、安装和权限之上。开工前先写 ADR 0005，说明把“多 Agent 互相委托”从明确延后提前到这一阶段。

范围：

- Agent 按清单（`mas.agent/1`）定义：角色、系统指令、模型、允许的工具和技能、预算。内置 Agent 由清单注册，能像工具包一样从目录安装和分享。
- 编排 Agent 通过内置委托工具把子任务交给子 Agent（首批 planner、image、reviewer），子 Agent 复用运行时循环。委托有深度上限，禁止环。
- Agent 之间只传任务说明、句柄和摘要。本地内容照旧不进任何供应商请求。本地内容对话框只由编排者发起。
- 分层预算、有限并行、停止向下传播。子 Agent 的确认请求冒泡到界面并标明是哪个 Agent。
- 界面按 Agent 分组显示进度。遥测按 Agent 出 span。评测覆盖委托、并行、预算、取消、越权和泄露。

完成标准：

- “规划 → 出图 → 检查”的多图任务由编排者委托完成，结果回到同一条助手消息。
- 子 Agent 越权调用工具被运行时拒绝。任何子 Agent 的请求里都没有本地正文或文件字节。
- 单 Agent 路径行为不变，现有检查和评测全部通过。

状态：已完成（PR #37）。设计见 [ADR 0005](docs/adr/0005-multi-agent.md) 和 [docs/agents.md](docs/agents.md)。交付：Agent 清单与校验、五个内置 Agent（编排者、planner、image、writer、reviewer）、delegate、越权拒绝、分层预算、有限并行与后端串行、停止传播、确认冒泡、Agent 树、agent span、Markdown 渲染、检查和评测。已知缺口：

- 用真实模型时，多智能体模式下的文字任务仍可能一步答完；研究类任务还没有委托流程。见 [issue #38](https://github.com/loootte/multimodal-agent-studio/issues/38)。
- reviewer 看不到图片内容；只有编排者和 planner 能读取技能全文。
- 评测只用假的模型服务和假的 ComfyUI；生图编排在真实环境手动试过。

## 9. Meta-Agent：轨迹挖掘、技能巩固与工具化

[issue #36](https://github.com/loootte/multimodal-agent-studio/issues/36)。建立在第 7 阶段的工具清单（#31）和第 8 阶段的 Agent 清单、span（#34）之上。开工前先写 ADR 0006：Meta-Agent 只观察和提议，不执行业务任务，对技能、工具和 Agent 的任何改动都要用户批准。

按顺序：

0. （前置，已完成）[#38](https://github.com/loootte/multimodal-agent-studio/issues/38) 多智能体：研究类和长文字任务必须走委托，为挖掘提供真实的多 Agent 轨迹。
1. （已完成）[#39](https://github.com/loootte/multimodal-agent-studio/issues/39) 轨迹导出与索引：规范化的 `mas.trajectory/1`，来源为会话事件和 `telemetry.jsonl`，本地内容、密钥和路径不进轨迹。ADR 0006、`services/meta/`、`GET /api/trajectories`，说明见 [docs/trajectory.md](docs/trajectory.md)。
2. （进行中）[#40](https://github.com/loootte/multimodal-agent-studio/issues/40) 技能挖掘：统计重复子流程，生成带证据（run_id、次数、成功率）的技能候选，不写 `skills/`。已实现于 `issue-40` 分支：ADR 0007、`services/meta/mine.py`、`POST /api/meta/mine`，说明见 [docs/mining.md](docs/mining.md)。
3. [#41](https://github.com/loootte/multimodal-agent-studio/issues/41) 记忆巩固：合并、重写、淘汰技能的提议，附 diff 和证据；用户批准才写入，可回滚。
4. [#42](https://github.com/loootte/multimodal-agent-studio/issues/42) 工具化评估：起草 `mas.tool/1` 清单、桩代码和测试；用户批准后才走工具包安装。
5. [#43](https://github.com/loootte/multimodal-agent-studio/issues/43) Meta-Agent 定义、手动和定时入口、报告界面（#36 的六段输出格式）。
6. [#44](https://github.com/loootte/multimodal-agent-studio/issues/44) 评测与发布门槛：挖掘召回、批准前零写入、隐私和越权的反向用例。

完成标准：

- 手动触发一次分析，得到 #36 格式的六段报告，事实条目能追溯到 run_id。
- 任何供应商请求、轨迹和报告里都没有本地正文、文件字节和密钥。
- 未经用户批准，`skills/`、已安装工具和 Agent 清单不变；批准的改动可以回滚。
- 现有检查和评测全部通过。

状态：未开始。

## 10. 可配置出图技能

[#49](https://github.com/loootte/multimodal-agent-studio/issues/49)（进行中，`issue-49` 分支）：提示词来源（聊天原文 / 模型撰写 / 本地）× 后端（本机 ComfyUI / 第三方 OpenAI 兼容 API）× 张数（默认 1）。新增 `stage_prompt` 工具、`generate_image` 的 `backend` 参数和第三方配置、`skills/image-gen/SKILL.md`、[ADR 0008](docs/adr/0008-image-skill.md)、[docs/image-skill.md](docs/image-skill.md)。本地提示词始终不进对话模型；发第三方默认拒绝，可改为逐轮确认。

## 明确延后

这些不进前六个阶段：

- 多 Agent 互相委托（已由第 8 阶段 / issue #34 提前，需 ADR 0005）
- 桌面或移动客户端
- 自训练模型
- 插件市场
- 语音实时通话

要提前做其中一项，先写一条新的架构决定，说明它替换或推迟哪一条现有承诺。
