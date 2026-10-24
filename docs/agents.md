# 编写和安装 Agent（mas.agent/1）

多智能体（Phase 8，见 [ADR 0005](adr/0005-multi-agent.md)）让内置**编排者**把一轮对话拆给几个子 Agent：
编排者 → 子 Agent → 工具调用。默认仍是单 Agent；只有在输入框旁勾上「多智能体」时，这一轮才交给编排者。

一个 Agent 就是一个目录，里面只有一份 `agent.json`。**清单里没有代码**：Agent 只能引用已经注册的工具
（内置工具或装好的工具包），每个工具仍按自己的 `tools.json` 走权限和确认。想要新能力，先写工具包
（见 [tool-pack-guide.md](tool-pack-guide.md)），再写一个用它的 Agent。

## 内置 Agent

| 名字 | 角色 | 工具 | 用途 |
| --- | --- | --- | --- |
| orchestrator | orchestrator | delegate、accept_local_content、list_local_content、retrieve_local、read_skill | 面对用户，拆任务、委托、汇总 |
| planner | planner | plan_images、list_local_content、read_skill | 定张数和分工，不出图 |
| image | image | generate_image、list_local_content | 用内容句柄逐张出图（一次调用一张） |
| reviewer | reviewer | retrieve_local、list_local_content | 只读检查，给“通过/需要修改” |

## 清单字段

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| schema | 是 | 固定 `mas.agent/1` |
| name | 是 | 小写字母开头，2-48 位 `[a-z0-9_]`，不能和内置重名 |
| version | 是 | `主.次.修` |
| description | 是 | 一句话，编排者靠它决定委托给谁 |
| role | 是 | `planner`、`image`、`reviewer`、`worker`（`orchestrator` 只给内置） |
| instructions | 是 | 系统指令，最多 4000 字 |
| model | 否 | 不写就跟会话模型；写的话是同一服务商下的模型名。环境变量 `MAS_AGENT_MODEL_<NAME>` 可覆盖 |
| tools | 是 | 已注册的工具名列表 |
| skills | 否 | 技能名列表；配合 `read_skill` 使用 |
| budget | 否 | `steps`(1-64)、`time_sec`(5-1800)、`tokens`、`images`(0-64) |

校验规则（`services/runtime/agents.py` 的 `validate_manifest`，安装和 `check.py` 都用它）：

- 不认识的字段直接拒绝；`tools` 里每个名字都必须已经注册。
- 第三方 Agent 不能用 `delegate`（不能再委托），也不能用 `accept_local_content`——本地内容对话框只能由编排者打开。
- `budget.tree_steps`、`budget.delegations` 只给编排者。

## 示例

一个只读的检查 Agent（下面每个 ```` ```json agent ```` 代码块都会被 `apps/web/check.py` 拿去校验）：

```json agent
{
  "schema": "mas.agent/1",
  "name": "count_checker",
  "version": "0.1.0",
  "description": "只读核对本会话出图张数是否和计划一致",
  "role": "reviewer",
  "instructions": "用 list_local_content 看本会话的工件，对照任务里的张数。第一行写“通过”或“需要修改”，再写一句理由。不要调用会改东西的工具。",
  "tools": ["list_local_content", "retrieve_local"],
  "budget": {"steps": 4, "time_sec": 120}
}
```

一个固定用另一个模型、只出两张图的出图 Agent：

```json agent
{
  "schema": "mas.agent/1",
  "name": "pair_painter",
  "version": "1.0.0",
  "description": "用给定内容句柄出两张不同 seed 的图",
  "role": "image",
  "instructions": "只用任务里的内容句柄调用 generate_image，一次一张，共两张，seed 不同。不要写画面描述。",
  "model": "grok-4-fast",
  "tools": ["generate_image"],
  "budget": {"steps": 6, "images": 2}
}
```

## 安装、导出、卸载

和工具包一样：设置 → Agent，填目录或 `.zip` 路径，先「检查」看问题，再「安装」。也可以用接口：

- `GET /api/agents`：列出内置和已安装的 Agent
- `POST /api/agents/inspect`、`POST /api/agents/install`：`{"source": "<目录或 zip 路径>"}`
- `POST /api/agents/<name>/uninstall`：内置的不能卸载
- `GET /api/agents/<name>/export`：下载 zip

装到 `agents/installed/`（或 `MAS_AGENTS_DIR`）。同名再装就是升级。

## 运行时的边界

- **只传句柄**：委托时 `inputs` 只能放内容句柄或工件标识，不能放正文；子 Agent 收到的也只有句柄，本地正文不发给服务商。
- **越权在运行时拒绝**：模型调了清单外的工具，返回 `tool_not_allowed`，不会执行。
- **层数和成环**：最多 2 层；`A → B → A` 返回 `delegation_cycle`。
- **预算分三层**：编排者自己的 `steps/time_sec/tokens`；整棵树共享 `tree_steps` 和 `delegations`；每个子 Agent 用自己的 `budget`，并从树预算里预扣。
- **并行**：一次 `delegate` 最多 4 个任务，`parallel: true` 时最多同时跑 2 个；同一后端（如 ComfyUI）上的工具调用串行。
- **确认**：子 Agent 要确认的工具会弹到对话里，卡片标出是哪个 Agent；同一轮同一 Agent 同一工具确认过一次就不再问。
- **停止**：点停止会同时停下编排者和所有子 Agent。
- **只回摘要**：子 Agent 交回状态、最多 1500 字的文字、产出的工件标识和步数。
