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

- 用真实模型时，多智能体模式下的文字任务仍可能一步答完；研究类任务还没有委托流程。见 [issue #38](https://github.com/loootte/multimodal-agent-studio/issues/38)。14.C（#80，`issue-80` 分支）让文字 / 研究任务先强制 `todo_write` 拆分再强制委托，仍待真实模型验证。
- reviewer 看不到图片内容；只有编排者和 planner 能读取技能全文。
- 评测只用假的模型服务和假的 ComfyUI；生图编排在真实环境手动试过。

## 9. Meta-Agent：轨迹挖掘、技能巩固与工具化

[issue #36](https://github.com/loootte/multimodal-agent-studio/issues/36)。建立在第 7 阶段的工具清单（#31）和第 8 阶段的 Agent 清单、span（#34）之上。开工前先写 ADR 0006：Meta-Agent 只观察和提议，不执行业务任务，对技能、工具和 Agent 的任何改动都要用户批准。

按顺序：

0. （前置，已完成）[#38](https://github.com/loootte/multimodal-agent-studio/issues/38) 多智能体：研究类和长文字任务必须走委托，为挖掘提供真实的多 Agent 轨迹。
1. （已完成）[#39](https://github.com/loootte/multimodal-agent-studio/issues/39) 轨迹导出与索引（PR #47）：规范化的 `mas.trajectory/1`，来源为会话事件和 `telemetry.jsonl`，本地内容、密钥和路径不进轨迹。ADR 0006、`services/meta/`、`GET /api/trajectories`，说明见 [docs/trajectory.md](docs/trajectory.md)。
2. （已完成）[#40](https://github.com/loootte/multimodal-agent-studio/issues/40) 技能挖掘：统计重复子流程，生成带证据（run_id、次数、成功率）的技能候选，不写 `skills/`。已合并（PR #48）：ADR 0007、`services/meta/mine.py`、`POST /api/meta/mine`，说明见 [docs/mining.md](docs/mining.md)。
3. [#41](https://github.com/loootte/multimodal-agent-studio/issues/41) 记忆巩固：合并、重写、淘汰技能的提议，附 diff 和证据；用户批准才写入，可回滚。
4. [#42](https://github.com/loootte/multimodal-agent-studio/issues/42) 工具化评估：起草 `mas.tool/1` 清单、桩代码和测试；用户批准后才走工具包安装。
5. [#43](https://github.com/loootte/multimodal-agent-studio/issues/43) Meta-Agent 定义、手动和定时入口、报告界面（#36 的六段输出格式）。
6. [#44](https://github.com/loootte/multimodal-agent-studio/issues/44) 评测与发布门槛：挖掘召回、批准前零写入、隐私和越权的反向用例。

完成标准：

- 手动触发一次分析，得到 #36 格式的六段报告，事实条目能追溯到 run_id。
- 任何供应商请求、轨迹和报告里都没有本地正文、文件字节和密钥。
- 未经用户批准，`skills/`、已安装工具和 Agent 清单不变；批准的改动可以回滚。
- 现有检查和评测全部通过。

状态：进行中。9.1（#39，PR #47）和 9.2（#40，PR #48）已完成并合并；9.3–9.6（#41–#44）未开始，沿用原 issue。第 14 阶段的调度器（14.E，#82）建好后，#43 的定时入口复用它。

## 10. 可配置出图技能

[#49](https://github.com/loootte/multimodal-agent-studio/issues/49)（已完成，PR #50）：提示词来源（聊天原文 / 模型撰写 / 本地）× 后端（本机 ComfyUI / 第三方 OpenAI 兼容 API）× 张数（默认 1）。新增 `stage_prompt` 工具、`generate_image` 的 `backend` 参数和第三方配置、`skills/image-gen/SKILL.md`、[ADR 0008](docs/adr/0008-image-skill.md)、[docs/image-skill.md](docs/image-skill.md)。本地提示词始终不进对话模型；发第三方默认拒绝，可改为逐轮确认。

## 11. 本地资源管理

[#51](https://github.com/loootte/multimodal-agent-studio/issues/51)（已完成，PR #52）：侧边栏「本地资源」面板，统一查看本地提示词/文字、本地文件、附件和生成文件：类型、时间、大小、引用会话、本机预览、总占用；筛选搜索、改标签、下载、删除/批量删除（引用提示、共享字节、运行中拒删、轨迹重建）、复用到当前对话（只给句柄）。接口只给本机，数据不进模型。见 [docs/local-resources.md](docs/local-resources.md)。

## 12. 隐私隔离强化

[#56](https://github.com/loootte/multimodal-agent-studio/issues/56)。建立在 [ADR 0003](docs/adr/0003-local-data-isolation.md)、[安全清单](docs/security-checklist.md) 与第 7 阶段工具清单之上。路线图入库见 [#55](https://github.com/loootte/multimodal-agent-studio/issues/55)。**开工前先写 ADR 0009**（句柄作用域与出站审计事件），草稿见 [docs/adr/0009-handle-scope-and-egress-audit.md](docs/adr/0009-handle-scope-and-egress-audit.md)。

按顺序（上一子阶段完成标准未达不开始下一阶段功能；12.E 可与 12.A 合并 MVP）：

1. [#57](https://github.com/loootte/multimodal-agent-studio/issues/57) **12.A** 出站审计与确认可见（P0，售前优先）：网关 `egress.planned` / `blocked` / `sent` 事件（无正文）、确认卡附出站摘要、运行过程「数据出站」时间线。
2. [#58](https://github.com/loootte/multimodal-agent-studio/issues/58) **12.B** 作用域句柄（P0）：不透明、短时、带 session/purpose/expiry；跨会话与过期不可消费。
3. [#61](https://github.com/loootte/multimodal-agent-studio/issues/61) **12.E** 敏感操作强制确认与数据流可视化（P1，可与 A 合并 MVP）：`network`/本地读组合不可 `confirm=never`；会话级数据流图。
4. [#59](https://github.com/loootte/multimodal-agent-studio/issues/59) **12.C** 网关拦截硬化（P1）：结构化出站策略、编码规范化、适配器统一出口中间件。
5. [#63](https://github.com/loootte/multimodal-agent-studio/issues/63) **12.G** 提示注入导致的数据外泄防护（P2）：确认文案由宿主模板生成；配置变更不走工具。
6. [#60](https://github.com/loootte/multimodal-agent-studio/issues/60) **12.D** 工具权限最小化与执行沙箱（P2）：声明权限可强制；第三方 pack 降权执行；ComfyUI 仍为本机白名单客户端。
7. [#62](https://github.com/loootte/multimodal-agent-studio/issues/62) **12.F** 本地存储加密与会话隔离（P3）：落盘加密、密钥不进 Git；可与第 6 阶段登录衔接。

完成标准：

- 任意一次供应商调用可在界面回放出站摘要与拦截原因，审计导出无正文/密钥。
- 句柄具备 session/purpose/expiry；跨会话与过期句柄不可消费。
- `send_local_to_model` 仍为硬禁止；`remote_local_prompt` 默认 `refuse` 且变更可审计。
- 安全类评测进入 `evals/gate.py`，回退则不发。

本阶段明确不做：多租户 IAM、远程授权、插件市场签名、把 ComfyUI 塞进通用容器沙箱、云端密钥托管。

状态：已完成。12.A（#57）已合并：出站审计事件 `egress.planned/blocked/sent/failed`（无正文）、`<data_dir>/egress.jsonl` 与本机 `/api/egress`、确认卡出站摘要、「数据出站」时间线和导出、`security.egress_audit_*` 评测进门禁，说明见 [docs/egress-audit.md](docs/egress-audit.md)。12.B（#58）已合并：句柄绑定会话 / 用途 / 可选运行 / 过期（默认 4 小时）、撤销与一次性、旧句柄读时升级；过期、越权 purpose、跨会话、伪造的句柄不能出图，固定错误码 `handle_expired` / `handle_forbidden` / `handle_not_found`，被拒记 `egress.blocked` 与 `security.handle_denied`；资源面板显示句柄状态并可撤销；`security.handle_*` 评测进门禁，说明见 [docs/handle-scope.md](docs/handle-scope.md)。12.C（#59）已合并：每个渠道一张字段白名单（`EGRESS_POLICIES`，`other` 默认拒绝、第三方出图一次一张），出站前做 NFKC 规范化、分片拼接、base64 / 百分号解码比对，短于 8 字的已登记正文按指纹整段匹配，错误码拆为 `local_content` / `local_content_encoded` / `egress_policy_denied`；适配器不挂统一出口中间件就不能注册、运行时没开 ticket 会被拦；`security.egress_encoded_variants` / `egress_short_body` / `egress_policy_denied` 进门禁，说明见 [docs/egress-policy.md](docs/egress-policy.md)。12.D（#60）已合并：第三方工具包在子进程沙箱里跑（环境变量白名单、配置显式注入；没 `network` 联网即拦、有也不能连本机；只能写临时目录和自己的数据目录；数据目录禁读，本地内容只经句柄；起进程、ctypes 拒绝；超时停止），越权返回 `permission_denied`（带 `violations`）并记 `toolpack` 渠道出站审计；授权记录绑定包内容摘要，装后改包要重装；内置工具收紧为名单 `HOST_TOOLS` / `BUILTIN_PYTHON`；安装时网页逐项权限审阅；本机 ComfyUI 仍由内置工具访问，是白名单例外；`security.sandbox_*` / `permission_manifest_changed` 进门禁，说明见 [docs/tool-sandbox.md](docs/tool-sandbox.md)。12.E（#61）在 `issue-61` 分支实现：`network` / 读本地的包只能 `confirm=always`（检查、安装、发现时拒绝 `never` / `by_args`，内置只读句柄工具例外）；敏感第三方包每次调用都确认，本地提示词发第三方的同意绑定（轮, 句柄, 主机）；确认卡固定区块（出站主机、含本地派生、句柄用途）；`remote_local_prompt=confirm` 要显式确认（409 `acknowledge_required`）并记 `security.policy_changed`；会话级数据流图 `/api/dataflow` 与侧栏面板；`security.confirm_never_network_rejected` / `sensitive_tool_confirm_every_call` / `remote_local_prompt_ack_required` / `dataflow_no_body` 进门禁，说明和三分钟演示见 [docs/sensitive-confirm.md](docs/sensitive-confirm.md)，已合并。12.F（#62）在 `issue-62` 分支实现：`MAS_STORAGE_ENCRYPTION=on` 时会话文件和本地内容 / 附件字节按会话加密落盘（AES-256-GCM，HKDF 会话密钥，AAD 绑会话，字节按会话存、文件名不暴露哈希），拷走 `data/` 没有主密钥读不出正文；主密钥在数据目录和仓库之外（`~/.mas/master.key` 或 `MAS_MASTER_KEY`），首次启动生成，沙箱禁读；口令备份 / 恢复 / 明文迁移 / verify 走 `apps/web/storage_tool.py`；`memory` 为仅内存会话演示模式；默认仍明文；`security.storage_*` 进门禁，备份与恢复手册见 [docs/storage-encryption.md](docs/storage-encryption.md)，已合并。12.G（#63）已合并（PR #71）：确认卡由宿主模板生成（`mas.confirm/1`，`by: "host"`，模型说的话和工具结果文字不进卡）；工具结果通道标 `untrusted`；对话通道禁止已入库指纹外传（`local_fingerprint`）；读本地 + 联网的高危组合要二次确认（缺 `acknowledge=high_risk` 返回 409）；配置变更只走设置 API + 本机界面（工具执行期间写配置被拒，跨站写配置 403，写入记 `security.config_changed`）；`security.injection_*` 评测进门禁，安全清单第 3 节全部自动，说明见 [docs/injection-guard.md](docs/injection-guard.md)。第 12 阶段完成。

## 13. 桌面应用

[#72](https://github.com/loootte/multimodal-agent-studio/issues/72)。从「明确延后」里的「桌面或移动客户端」提前桌面客户端，紧接第 12 阶段。**开工前先写 ADR 0010**，草稿见 [docs/adr/0010-desktop-client.md](docs/adr/0010-desktop-client.md)：第 3 阶段「首版是 Web」已经兑现，本阶段不替换它，只推迟移动端。现有本机 Web 界面装进桌面壳，壳托管本机 Python 服务；直接跑 Web 服务的行为不变。

按顺序（上一子阶段完成标准未达不开始下一阶段功能；13.D 可与 13.B / 13.C 并行）：

1. [#74](https://github.com/loootte/multimodal-agent-studio/issues/74) **13.A** 桌面壳与本机服务托管（P0）
   - 层：新 `apps/desktop/`（草案倾向 Tauri 2 + Python sidecar，选型由 ADR 0010 定）；`apps/web` 加启动令牌。
   - 句柄隔离：无变化。新增的一层是只绑 `127.0.0.1` 的随机端口加一次性令牌，防止同机其他进程或网页借本机接口读本地资源。
   - 验收：Windows 上从壳启动，完成一次本机出图；端口冲突自动换端口；无令牌请求 401；关窗后没有残留进程；Web 模式不变。
2. [#75](https://github.com/loootte/multimodal-agent-studio/issues/75) **13.B** 系统集成：钥匙串、默认加密、原生文件、通知（P1）
   - 层：密钥（系统钥匙串保存 API key 和 12.F 主密钥）、存储（桌面版默认 `MAS_STORAGE_ENCRYPTION=on`）、原生选文件和拖放、托盘与通知。
   - 句柄隔离：收紧。原生文件只产生句柄；通知由宿主模板生成，不含提示词、正文和文件名；WebView 拿不到密钥。
   - 验收：删掉钥匙串里的 key 后返回 `remote_missing_key`；新数据目录默认是密文；通知文本检查不到本地正文指纹。
3. [#76](https://github.com/loootte/multimodal-agent-studio/issues/76) **13.C** 打包、首次运行向导与更新（P1）
   - 层：Windows 安装包（内嵌 Python）、首次运行向导（只探测用户填写的或本机默认的 ComfyUI 地址）、自动更新（默认关闭、校验签名）。
   - 句柄隔离：不破坏。更新检查是新出站渠道 `update`，按 12.C 加字段白名单，只有版本号和平台。
   - 验收：干净机器上装完能出图；不开更新时没有任何更新出站；卸载默认保留数据。
4. [#77](https://github.com/loootte/multimodal-agent-studio/issues/77) **13.D** 桌面壳安全加固与发布门禁（P1）
   - 层：WebView（严格 CSP、禁止外部导航、release 关 devtools）、IPC 白名单、外链经宿主确认后用系统浏览器打开。
   - 句柄隔离：不破坏，防止注入内容让 WebView 带着本地内容导航出去。
   - 验收：`security.desktop_*` 进 `evals/gate.py`；注入跳转不生效；非白名单 IPC 全部被拒并有记录。

完成标准：

- 干净的 Windows 机器上安装、启动，完成一次本机出图对话。
- 桌面版隐私默认比 Web 更严：启动令牌、钥匙串、默认加密、通知无正文、禁止外部导航。
- `security.desktop_*` 进门禁；Web 模式行为不变，现有检查和评测全部通过。
- 不变量不变：一次 `generate_image` 出一张，张数由 `plan_images` 决定，本地内容不进 LLM，单 Agent 模式不变，`remote_local_prompt` 默认 `refuse`。

本阶段明确不做：移动端、应用商店上架、代码签名证书采购、云同步、多用户。

状态：未开始。

## 14. 对标 DeepSeek Harness 的能力补齐

[#73](https://github.com/loootte/multimodal-agent-studio/issues/73)。对照 DeepSeek Harness（DSH）补齐 MAS 落后的通用 agent 能力，前提是第 12 阶段的隔离一条都不放松。**开工前先写 ADR 0011**，草稿见 [docs/adr/0011-external-capabilities-isolation.md](docs/adr/0011-external-capabilities-isolation.md)。第 9 阶段剩下的 #41–#44 沿用原 issue，不在这里重复；14.E 建好后，#43 的定时入口复用它。

按顺序（上一子阶段完成标准未达不开始下一阶段功能；14.G 可提前做回归底座）：

1. [#78](https://github.com/loootte/multimodal-agent-studio/issues/78) **14.A** 受控接入外部 MCP server（P0）
   - 层：工具平面（`services/tools/mcp.py` 扩成客户端，MCP server 作为 `kind: mcp` 的工具包，stdio 跑在 12.D 沙箱）和网关出站（`toolpack` 渠道）。
   - 句柄隔离：有风险，可控。参数只给句柄和模型可见文字，出站前过字段白名单、编码检测和 `local_fingerprint`；默认 `confirm: always`；确认卡用宿主模板；结果标 `untrusted`；远端工具默认不启用，描述或 schema 变了要重新授权。
   - 验收：接通一个 stdio server 和一个 HTTP server，每次调用都有确认和审计；本地正文及其编码变体被拦；schema 变化后要重新授权；`security.mcp_*` 进门禁。
   - 进度：实现在 `issue-78` 分支，见 [docs/mcp-client.md](docs/mcp-client.md)。客户端是新的 `services/tools/mcp_client.py`，`mcp.py` 仍然只做 server。
   - 后续 [#93](https://github.com/loootte/multimodal-agent-studio/issues/93)：MCP 出站和工具安全检查改成可配置（全局 / 包 / 工具三层，默认最严）。见 [docs/tool-policy.md](docs/tool-policy.md)。实现在 `issue-93-simple`，网页「设置」里可以改。ADR 0011 仍是提议。
2. [#79](https://github.com/loootte/multimodal-agent-studio/issues/79) **14.B** 上下文压缩与工具结果裁剪（P0）
   - 层：运行时（`loop.py` 按上下文上限设阈值压缩，大工具结果只保留结构字段）。
   - 句柄隔离：不破坏。摘要发出前过 `local_fingerprint`；句柄不展开；`plan_images` 计划、未完成确认和句柄清单固定保留；原始会话留在本机。
   - 验收：超过阈值的长对话能继续，token 下降；压缩前后出图张数不变；阈值以下请求逐字节不变。
   - 进度：实现在 `issue-79` 分支，见 [docs/context-compaction.md](docs/context-compaction.md)。压缩在 `services/runtime/compaction.py`；默认模型摘要（只发结构字段和句柄，前后都过本地正文检查，命中或失败退回确定性抽取并记审计）；压缩后才出现的 `session_query` 按 `ctx_N` 引用取回原文（兜底手段，本地正文只给句柄）；参数可在设置页配置（`data/context.json`，非法值回退默认）；被裁剪 / 折叠的原文存进本机会话记录（随 12.F 明文 / 加密 / 只在内存，不进会话视图和导出），下一次运行也能按引用取回。
   - 后续（未做）：
     - 确定性抽取作为可选默认：现在默认是模型摘要、确定性抽取只做退回手段；以后在设置里允许把确定性抽取设为默认（不调用摘要模型、零额外出站），并补对比评测（摘要质量、token、延迟）。
     - 摘要模型可配：现在摘要走当前聊天供应商和模型；以后让摘要单独配置供应商和模型（比如更便宜的模型或本机模型），配置进 `data/context.json` 或供应商设置，出站仍走同一个网关和 `egress.inspect`，摘要调用的 token 计入预算。
3. [#80](https://github.com/loootte/multimodal-agent-studio/issues/80) **14.C** 通用规划工具：todo / plan 模式 / goal（P1）
   - 层：运行时宿主工具和网页 todo 面板；编排者用 todo 拆分文字和研究任务。
   - 句柄隔离：不破坏。todo 和 goal 文字过指纹；todo 里的数量不算张数，张数仍只由 `plan_images` 决定；不做 Code 模式。
   - 验收：plan 模式下改状态的工具被拒；goal 碰到预算就停并写明原因；真实模型下长文字任务先拆分再委托（#38 遗留关闭）。
   - 进度：实现在 `issue-80` 分支，见 [docs/planning.md](docs/planning.md)。宿主工具 `todo_write` / `goal_set` / `goal_done` 和 plan 模式的 `plan_submit` 在 `services/runtime/planning.py`；plan 模式只给只读工具（按工具包声明和权限判定，MCP / 敏感 / 要确认的都不算），写工具被拒且不弹确认，用户点「按计划执行」才执行；目标续跑默认关，开了也受轮数 / 时间 / token 预算约束，停下时写明原因，从不替用户确认；多智能体的文字 / 研究任务先强制 `todo_write` 再强制委托，每次委托完成宿主推进一项；单 Agent 默认不给规划工具；设置在「设置 → 规划与目标」（`data/planning.json`）。
   - 后续（未做）：用真实模型验证编排者拆分质量后再关闭 #38 的遗留项；plan 模式里是否允许只读 MCP 工具（要先有可信的只读声明）；目标跨会话保留。
4. [#81](https://github.com/loootte/multimodal-agent-studio/issues/81) **14.D** 只存本机的长期记忆（P1）
   - 层：存储（`mas.memory/1`，分 preference 和 content）、运行时（`remember` / `recall` / `forget`）、网页记忆面板（默认关闭，每条写入要确认）。
   - 句柄隔离：容易破坏，约束最严。只有 preference 的文字能进上下文并要过指纹；content 类只存句柄形态（purpose `memory`）并加密，跨会话由宿主重新签发句柄，12.B 的跨会话不可消费规则不变。
   - 验收：B 会话只看到 A 会话的偏好文字和新签发的句柄，看不到正文；伪装成 preference 的正文被拦；关闭时没有记忆进上下文。
5. [#82](https://github.com/loootte/multimodal-agent-studio/issues/82) **14.E** 调度与后台任务（P1）
   - 层：新 `services/scheduler/`（once / every / daily / weekly / cron，后台 jobs）、运行时、网页任务列表和待确认队列。
   - 句柄隔离：需要设计。任务引用本地内容时用绑定任务 id 的 `scheduled` 句柄，有 TTL，可撤销，到期或任务修改后要重新授权；无人值守**不自动确认**，`confirm: always` 和 `high_risk` 的调用永远不能预授权，遇到就挂起为待确认。
   - 验收：定时出图按时运行并留审计；需要确认的调用挂起而不是执行；句柄过期后任务停止，重新授权后能继续；有预算上限。
6. [#83](https://github.com/loootte/multimodal-agent-studio/issues/83) **14.F** 实验性浏览器 / computer use 工具包（P2，依赖 14.A）
   - 层：通过 MCP 接入的沙箱工具包，声明 `network`，`confirm: always`，域名白名单默认为空，默认关闭。
   - 句柄隔离：风险高。网页内容全部标 `untrusted`，不进确认卡；往网页输入前过 `local_fingerprint`；禁止访问 `file://`、本机和局域网地址。
   - 验收：白名单域名上读一次页面，有确认和审计；往表单填本地正文、导航到 `127.0.0.1` 都被拦；网页注入不能触发配置写入。
7. [#84](https://github.com/loootte/multimodal-agent-studio/issues/84) **14.G** 评测录制回放（P2）
   - 层：网关（录制和回放 cassette）、`evals/run.py --record` / `--replay`、`gate.py`。
   - 句柄隔离：不破坏。录的是本来就发往供应商的载荷；入库前扫描密钥、本地正文指纹和绝对路径。
   - 验收：真实模型录一次多 Agent 出图任务，离线回放稳定且张数一致；提示词或 schema 改动后回放失败并给出差异。

完成标准：

- 七个子阶段各自验收通过，新增的 `security.*` 用例进门禁。
- 不变量不变：一次 `generate_image` 出一张，张数由 `plan_images` 决定，本地内容不进 LLM，单 Agent 模式不变，`remote_local_prompt` 默认 `refuse`，隐私默认最严。
- 现有检查和评测全部通过。

本阶段明确不做：插件市场、社区插件与官方同权、Code 模式批量调用、Agent Teams 式自由消息协作、通用编码能力（bash / LSP / worktree）进核心、整机桌面控制、云端常驻执行。

状态：未开始。

## 支线：角色图片定制工具（原第 13 阶段）

[#53](https://github.com/loootte/multimodal-agent-studio/issues/53)（已完成，PR #54；原编号第 13 阶段，因桌面应用提前改为支线）：内置工具包 `character_image`，九个字段（性别、年龄、体型、发型、发色、瞳色、服装、姿势、背景）都有预设也能自填，默认是年轻女性角色；拼成英文提示词后走现有出图流程（`generate_image` 一次一张、`plan_images` 张数、本机/第三方按 #49）。网页「定制角色」对话框和对话调用两条路；本地提示词模式按 ADR 0008；未成年与暴露内容的安全规则。见 [docs/character-image.md](docs/character-image.md)。

## 明确延后

这些不进前六个阶段：

- 多 Agent 互相委托（已由第 8 阶段 / issue #34 提前，需 ADR 0005）
- 移动客户端（桌面客户端已由第 13 阶段 / [#72](https://github.com/loootte/multimodal-agent-studio/issues/72) 提前，见 ADR 0010）
- 自训练模型
- 插件市场
- 语音实时通话

要提前做其中一项，先写一条新的架构决定，说明它替换或推迟哪一条现有承诺。
