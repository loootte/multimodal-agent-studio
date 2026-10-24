# ADR 0005：多智能体（编排者委托子 Agent）

## 状态

已接受。对应 [issue #34](https://github.com/loootte/multimodal-agent-studio/issues/34) 和路线图第 8 阶段。

## 背景

路线图把“多 Agent 互相委托”放在“明确延后”里，并要求提前做之前先写一条 ADR，说明替换或推迟哪条承诺。第 7 阶段（[issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31)）已经把工具做成清单、可安装、带权限和确认策略，多 Agent 可以建在它上面。

## 决定

把“多 Agent 互相委托”从明确延后提前为第 8 阶段，只做**编排者向子 Agent 委托**这一种形态，不做 Agent 之间自由对话。下列承诺不变：

- 运行时是唯一可以调用工具的地方。子 Agent 也是 `services/runtime/loop.run` 的一次运行。
- 本地内容不进任何供应商请求。Agent 之间只传任务说明、句柄和摘要。每个子 Agent 的网关请求都带本会话的 `local_bodies` 再查一次载荷。
- 一次 `generate_image` 调用只出一张，张数由 `plan_images` 决定。工具配置照旧只来自 `tools.json`。
- 默认仍是单 Agent。不打开多智能体开关时，行为和第 7 阶段完全一样。

被推迟或替换的承诺：无。“明确延后”里删去这一项，其余不动。

### Agent 清单

Agent 写成 `mas.agent/1` 清单（`agent.json`），内置的放 `agents/builtin/<name>/`，安装的放 `agents/installed/<name>/`（可用 `MAS_AGENTS_DIR` 改）。字段和校验见 [docs/agents.md](../agents.md)。安装、导出和工具包同一套：目录或 zip，导出不带本机记录。Agent 清单里没有代码，所以没有权限确认这一步；它能用的工具受工具自己的权限和确认约束。

### 开放问题的决定

1. **模式怎么选**：用户在输入框旁边勾“多智能体”，按每一轮发送。编排者固定是内置的 `orchestrator`，不能换成安装的 Agent。不勾就是单 Agent。
2. **子 Agent 用什么模型**：清单里 `model` 留空就跟会话模型走。写了就用同一个供应商（同一个 base_url 和 API key）下的那个模型名。不为子 Agent 单独配第二套密钥；多供应商留到以后。
3. **子 Agent 的轨迹回不回编排者**：只回结构化摘要：状态、最后一段文字（截到 1500 字）、产出的工件句柄和用掉的步数。完整轨迹只进 trace 和界面，不进编排者的上下文。
4. **并行和共享后端**：一次 `delegate` 最多 4 个任务，同时最多跑 2 个（`agents.MAX_PARALLEL`）。工具清单 `requires.backends` 里声明的后端按后端加锁串行，所以 ComfyUI 一次只出一张。出图张数预算不预留，按调用计数，超过就拒绝那一次调用。
5. **reviewer 能否打回**：reviewer 只给意见，不直接调 image。编排者可以再委托一次 image 重做。一轮里委托总次数上限 6 次（编排者清单的 `budget.delegations`），用完就拒绝再委托。
6. **Agent 能否自带工具**：不能。清单只能引用已注册的工具名。要新工具先装工具包。
7. **确认能否按角色预先批准**：不能预先批准。子 Agent 要确认的调用照样弹确认，确认卡上标出是哪个 Agent。同一轮里同一个 Agent 确认过的同一个工具不再重复问（和单 Agent 时一样）。

### 结构

- `services/runtime/agents.py`：清单校验、发现、安装、导出；每个 Agent 的工具集合（清单允许 ∩ 已注册）；`delegate` 工具契约；委托器（深度上限 2、禁止环、分层预算、有限并行、按后端串行、停止传播、遥测 span）。
- `services/runtime/loop.run` 增加可选的 `validate` 参数，让 `delegate` 这样不在工具包里的运行时工具也能被校验。默认行为不变。
- `apps/web/chat_run.py` 在多智能体模式下给编排者 `delegate`，子 Agent 的工具调用、确认和工件照旧走网页切片，事件带上 `agent`、`agent_run_id`、`parent_run_id`。
- 只有编排者（面对用户的那一层）能调用 `accept_local_content`。子 Agent 的清单不能列它，运行时也会拒绝。

### 预算

分三层：

- 编排者自己的预算（步数、时间、token）照旧。
- 整轮委托池：`budget.tree_steps` 是所有子 Agent 步数之和的上限，`budget.delegations` 是委托次数上限。
- 每个子 Agent：清单 `budget.steps/time_sec/tokens/images`，实际给的步数是清单值和池里剩余的较小者。

### 遥测

每个子 Agent 一个 span：`trace` 里记 `{"type": "agent", "run_id", "parent_run_id", "agent", "role", "model", "depth", "ms", "status"}`，遥测记 `agent.run`。子 Agent 自己的模型和工具调用记在它的 `run_id` 下，能按 `parent_run_id` 串成树。

## 后果

- 多智能体模式下一轮回答更慢、更贵，所以默认关闭。
- 子 Agent 的上下文更短、工具更少，越权调用会被运行时拒绝，而不是靠提示词约束。
- 没有沙箱、没有分布式、没有 Agent 市场，这些仍然延后。
