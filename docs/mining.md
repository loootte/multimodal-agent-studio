# 技能挖掘（mas.skill-candidate/1）

Meta-Agent ②（#40，决策见 [ADR 0007](adr/0007-skill-mining.md)）。从已导出的 `mas.trajectory/1` 里用规则统计重复的工具 / Agent 序列，产出带证据的技能候选。候选**只写**到 `data/meta/skill-candidates.json`，**绝不**改 `skills/`（写入属于 #41）。

## 怎么挖

1. （可选）先做一次增量轨迹导出。
2. 从每条运行抽出工具步骤序列；从委托树抽出子 Agent 序列。
3. 做 n-gram（默认 n=2..5），按签名聚类。
4. 统计次数、跨越的会话数、成功率、来源 `run_id`、脱敏后的典型场景摘要。
5. 达到门槛（默认 ≥3 次且 ≥2 个会话）的写成候选；被更长且也达标的序列覆盖的短 n-gram 丢掉，避免碎片。
6. 与已安装技能做词元重叠，给出 `existing.form` 和巩固建议草稿。
7. `--model` 打开时，把脱敏后的模式摘要交给模型补名称 / 描述 / 步骤；默认关闭，关着也能出统计候选。

## 存储

`data/meta/skill-candidates.json`，schema `mas.skill-candidates/1`：

| 字段 | 说明 |
| --- | --- |
| `mined_at` | 本次挖掘时间（UTC） |
| `params` | `min_count`、`min_sessions`、`use_model`、读入记录数等 |
| `candidates` | `mas.skill-candidate/1` 列表 |
| `insufficient` | 未达门槛的签名摘要（次数、会话、`run_ids`），不进候选 |

## 候选字段

| 字段 | 说明 |
| --- | --- |
| `id` | `sc_` + 签名哈希 |
| `name` | slug（模型归纳可改） |
| `description` | 描述草稿 |
| `status` | 写入库的都是 `candidate` |
| `pattern` | `kind`（tool_sequence / agent_sequence）、`signature`、`n`、`steps` |
| `evidence` | `count`、`sessions`、`success_rate`、`run_ids`、`typical_scenarios`、`outcomes` |
| `existing` | `form`（implicit / existing_skill）、`overlap[]`、`consolidation` |
| `steps_draft` | 步骤草稿 |
| `model_assisted` | 是否用了模型归纳 |

校验：`services.meta.candidate.validate` / `validate_store`。

## 隐私

候选和模型归纳输入都不含本地正文、附件字节、密钥和绝对路径；复用轨迹侧的脱敏约定，写出前再断言。check.py 对夹具候选和 prompt 做泄漏搜索。

## 用法

```
python -m services.meta.mine
python -m services.meta.mine --min-count 5 --min-sessions 3
python -m services.meta.mine list
python -m services.meta.mine show sc_a7f4863b06c79f33a33d5efe76a5aab7
python -m services.meta.mine --data-dir D:\tmp\data-copy
```

HTTP（只接受本机）：

- `POST /api/meta/mine?min_count=3&min_sessions=2`
- `GET /api/meta/candidates`
- `GET /api/meta/candidates/{id}`

## 示例（check.py 会用 validate 校验这一段）

夹具里「plan_image → generate_image ×2」出现 5 次、3 个会话时挖出的候选：

```json
{
  "schema": "mas.skill-candidate/1",
  "id": "sc_a7f4863b06c79f33a33d5efe76a5aab7",
  "name": "plan-image-then-generate-image-then-generate-image",
  "description": "工具序列「plan_image → generate_image → generate_image」在轨迹里出现 5 次、跨越 3 个会话，成功率约 100%。可作为可复用子流程沉淀成技能（候选，未经批准不写入 skills/）。",
  "status": "candidate",
  "pattern": {
    "kind": "tool_sequence",
    "signature": "plan_image>generate_image>generate_image",
    "n": 3,
    "steps": ["plan_image", "generate_image", "generate_image"]
  },
  "evidence": {
    "count": 5,
    "sessions": 3,
    "success_rate": 1.0,
    "run_ids": [
      "r_pat0000000000000000000000000000",
      "r_pat0000000000000000000000000001",
      "r_pat0000000000000000000000000002",
      "r_pat0000000000000000000000000003",
      "r_pat0000000000000000000000000004"
    ],
    "typical_scenarios": ["批量出图模式场景 0", "批量出图模式场景 1"],
    "outcomes": {"completed": 5}
  },
  "existing": {
    "form": "implicit",
    "overlap": [],
    "consolidation": "轨迹里隐含的新模式：建议写成独立技能草稿，澄清适用场景与步骤边界后再批准写入 skills/。"
  },
  "steps_draft": [
    "调用工具 plan_image",
    "调用工具 generate_image",
    "调用工具 generate_image"
  ],
  "mined_at": "2026-10-24T14:00:00Z",
  "model_assisted": false,
  "model_prompt_chars": null
}
```
