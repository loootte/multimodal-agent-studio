# 轨迹（mas.trajectory/1）

Meta-Agent ① 的数据底座（#39，决策见 [ADR 0006](adr/0006-trajectories.md)）。每次运行（顶层回合或一个子 Agent）导出成**一条**规范化记录，来源是会话事件 + `telemetry.jsonl`。Meta-Agent 只读这些记录、只提建议；任何改动都要用户批准。本期不做挖掘、同步、外发给模型提供方。

## 存储

| 文件 | 内容 |
| --- | --- |
| `data/trajectories/trajectories.jsonl` | 一行一条记录 |
| `data/trajectories/index.json` | 按工具 / Agent / 技能 / 结果 / 时间查询用的摘要索引 |

- 导出是**增量、幂等**的：同一会话没变就跳过，重复导出 `written == 0`；正在运行的回合不导出。
- 保留 90 天（`--keep-days`）；删除会话会清掉它的轨迹；“忘记”内容会让该会话重新生成轨迹。

## 字段

| 字段 | 说明 |
| --- | --- |
| `schema` | 固定 `mas.trajectory/1` |
| `run_id` / `parent_run_id` / `trace_id` | 本次运行、父运行（顶层为 null）、顶层运行；按 `parent_run_id` 能还原委托树 |
| `session_id` / `turn_id` | 会话和助手消息 |
| `agent` / `role` / `mode` / `depth` | 单智能体顶层为 `assistant`，多智能体顶层为 `orchestrator`；`mode` 为 `single`/`multi` |
| `model` | 实际用的模型 |
| `started_at` / `ended_at` / `ms` | UTC ISO 时间与耗时 |
| `input` | 顶层才有：`summary`（≤120 字，已脱敏）、`chars`、`local_handle`、`attachments`（只有句柄/种类/类型/大小）；子运行为 null |
| `output` | `summary`、`chars`、`artifacts`（句柄） |
| `steps[]` | `kind`(model/tool)、`name`、`ok`、`ms`、`at`、`tokens`、`args`（参数摘要，不含原文）、`error_code`、`failure_class` |
| `tools` / `skills` | 用到的工具和技能 |
| `budget` | 步数与 token 合计 |
| `outcome` | `status`(completed/failed/cancelled/unknown)、`error_code`、`failure_class` |
| `feedback` | `retried`、`reseeded`、`stopped` |
| `delegations[]` | 子运行 `run_id` 列表 |

版本：加可选字段不升版本；破坏性改动升到 `/2`；读取方忽略不认识的字段。校验函数是 `services.meta.trajectory.validate(record)`，返回问题列表，空表示通过。

## 隐私

1. 本地内容只留句柄（`c_…`），不留正文；2. 摘要截到 120 字，再过 `_cover_local` 和 `_scrub_value`；3. 不带附件字节；4. 工具参数只留摘要（句柄、数字、键名、长度）；5. 密钥形态替换成 `[secret]`；6. 绝对路径替换成 `[path]`（URL 保留）；7. 写出前最后再断言一次，有泄漏的记录直接丢弃并计入 `dropped`。

## 用法

```
python -m services.meta.export                      # 增量导出
python -m services.meta.export query --agent planner --brief
python -m services.meta.export query --tool image.generate --outcome failed --since 2026-10-01
python -m services.meta.export --data-dir D:\tmp\data-copy query --mode multi --limit 5
```

HTTP（只接受本机访问）：`GET /api/trajectories?agent=planner&outcome=completed&limit=20`，参数有 `tool agent skill outcome since until session mode limit full`。请求时会先做一次增量导出。

## 示例（check.py 会用 validate 校验这一段）

一次多智能体文字任务里 writer 子运行的记录：

```json
{
  "schema": "mas.trajectory/1",
  "run_id": "r_89d06690f7bf483c8bec195e769651ec",
  "parent_run_id": "r_1b2a878734394bd0a4d2926df37a9ed9",
  "trace_id": "r_1b2a878734394bd0a4d2926df37a9ed9",
  "session_id": "s_2ed09bdf39db449fb1bf7ffab60f5b3a",
  "turn_id": "m_4efe2d5c3a5a46239a0539131c8681b6",
  "agent": "writer",
  "role": "writer",
  "mode": "multi",
  "depth": 1,
  "model": "demo-vision",
  "started_at": "2026-10-24T12:14:52Z",
  "ended_at": "2026-10-24T12:14:52Z",
  "ms": 1,
  "input": null,
  "output": {"summary": "## 草稿 正文", "chars": 8, "artifacts": []},
  "outcome": {"status": "completed", "error_code": null, "failure_class": null},
  "feedback": {"retried": false, "reseeded": false, "stopped": false},
  "steps": [
    {"kind": "model", "name": "demo-vision", "ok": true, "ms": 0,
     "at": "2026-10-24T12:14:52Z", "tokens": {"estimated": 4}}
  ],
  "tools": [],
  "skills": [],
  "budget": {"steps": 1, "tokens": {"estimated": 4}},
  "delegations": [],
  "exported_at": "2026-10-24T12:14:52Z"
}
```
