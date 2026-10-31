# 规划工具：任务清单 / plan 模式 / 目标（14.C，#80）

运行时自带三个**宿主工具**，加上 plan 模式专用的 `plan_submit`。它们只改宿主里的规划状态，不读写文件、不联网、不出图，所以写在 `services/runtime/planning.py` 里，没有做成工具包（工具包放的是会碰外部世界的工具代码和前端）。

| 工具 | 作用 | 结果 |
| --- | --- | --- |
| `todo_write(items)` | 整份重写任务清单，每项 `{text, status}`，status 为 `pending / in_progress / completed / cancelled`，最多 20 项、每项 200 字 | 网页上助手消息旁出现清单面板，逐项打勾 |
| `goal_set(objective, done_when?)` | 设一个跨步骤的目标 | 面板显示「目标：… · 进行中」 |
| `goal_done(status, note?)` | `achieved` 或 `blocked`（做不到，说明原因） | 面板显示结果 |
| `plan_submit(summary, steps)` | 只在 plan 模式下给出；交计划后这一轮结束 | 助手消息里写出计划，并给一个「按计划执行」按钮 |

结果按工具结果（不可信通道）回给模型；事件 `planning.todo`、`planning.goal`、`plan.proposed` 存进会话，下一轮从事件里恢复没做完的清单和进行中的目标（不加新的存储格式）。

## 句柄隔离

- 清单和目标的文字在**存下之前**过一次本地正文检查（`egress.inspect`，含 12.G 指纹）。命中就不记、返回 `local_content` / `local_fingerprint` 原因码，并记一条 `planning` 渠道的安全事件；正文不进事件流、不回到模型请求。重新注入系统指令前会再查一次（本地内容可能是之后才登记的）。
- 清单里写的数量（例如「画 5 张」）**不算**出图张数。张数仍只由 `plan_images` 决定。
- 没有 Code 模式。

## plan 模式（先出计划）

在输入框旁勾「先出计划」后发送：

1. 运行时只把**只读工具**给模型，并加上 `plan_submit`。只读的判断条件是：工具包能找到、不是 MCP 工具、不是敏感工具、`mutating` 为 false、`confirm` 为 `never`、不需要确认，而且声明的权限和运行时实际需要的权限里都没有 `network / write_files / ui / send_local_to_model`。此外 `delegate`、`stage_prompt`、`accept_local_content`、`forget_local_content` 一律不给。运行时自带的只读工具只有 `session_query`。
2. 模型即使硬调写工具，运行时也不执行、**不弹确认卡**，只回 `plan_mode_readonly`，并记遥测 `planning.denied`。
3. 模型调 `plan_submit` 交出计划，这一轮就结束。模型想不交计划直接收尾时，会被提醒一次。
4. 用户点「按计划执行」后，会以 `approve_plan=<消息 id>` 发起下一轮。服务端会核对这条消息确实存着 `plan.proposed` 事件，伪造的 id 返回 400 `plan_not_found`。执行轮按计划步骤生成清单，然后正常执行，写操作照常确认。

plan 模式和执行轮都不会被当成「直接出图」请求。

## 目标续跑（goal）

模型不调工具想收尾时，如果还有进行中的目标：

- **默认（`goal_auto_continue=false`）**：不续跑，面板写明「自动续跑没开」（`auto_continue_off`）。
- 开了续跑：在下面三个预算内各续一轮，续跑指令只写目标文字（同样复查过）。任一预算用完就停下，并在面板上写明原因：`goal_rounds`、`goal_time`、`goal_tokens`。
- 运行被步数 / 时间 / token 预算（`Budget`）截断或被取消时，目标同样带原因停下（`budget_exceeded` / `cancelled`）。
- 续跑**从不替用户确认**。写操作仍走原来的确认卡和工具策略。

## 多智能体

- 勾「多智能体」时，编排者默认有 `todo_write`（`orchestrator_todo=true`）。文字和研究类任务的第一步会被强制先调 `todo_write` 拆分，之后才按 #38 的规则强制 `delegate`（planner → writer → reviewer）。
- 每次委托成功，宿主把清单里第一项没完成的标成 completed，下一项标成 in_progress，网页上逐项打勾。委托失败时清单不动。
- 子 Agent 拿不到规划工具。
- 图片类任务不受影响。

## 单 Agent

默认**不给**规划工具（`single_agent_tools=false`）。没开 plan 模式时，请求和以前逐字节一致。

## 设置

在网页「设置 → 规划与目标」里改，存到 `data/planning.json`。改设置走 `PUT /api/planning`，写入受同源检查保护，审计只记字段名。非法值回退默认值，并在 `warnings` 里列出。

| 字段 | 默认 | 范围 |
| --- | --- | --- |
| `single_agent_tools` | `false` | 单 Agent 也给 todo / goal 工具 |
| `orchestrator_todo` | `true` | 编排者有 todo，文字 / 研究任务先拆分 |
| `goal_auto_continue` | `false` | 目标没完成时自动续跑 |
| `goal_max_rounds` | 3 | 1–20 |
| `goal_time_sec` | 600 | 30–3600 |
| `goal_tokens` | 200000 | 1000–2000000 |

## 评测

| 评测 | 验证什么 |
| --- | --- |
| `runtime.todo_progress` | 清单逐项完成；没开规划时请求不变 |
| `runtime.plan_mode_readonly` | 写工具不给、不执行、不弹确认，交计划后结束 |
| `runtime.plan_mode_approve` | 网页端 plan 模式 → 确认 → 执行，伪造的确认被拒 |
| `runtime.goal_budget_stop` | 按轮数停下并写明原因；默认不续跑；`Budget` 截断时同样写明原因 |
| `runtime.todo_quantity_ignored` | 张数只认 `plan_images` |
| `security.planning_no_local_body` | 带本地正文的清单 / 目标不存、不外发 |
| `multi.text_task_delegates` | 长文字任务先 `todo_write` 再委托，清单逐项完成 |
