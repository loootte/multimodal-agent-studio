# ADR 0007：技能挖掘与候选（Meta-Agent ②）

## 状态

已接受。对应 [issue #40](https://github.com/loootte/multimodal-agent-studio/issues/40)，父 issue [#36](https://github.com/loootte/multimodal-agent-studio/issues/36)。依赖 [ADR 0006](0006-trajectories.md)。

## 背景

#39 已把会话和遥测导出成去敏的 `mas.trajectory/1`。轨迹里反复出现的子流程（例如「规划张数 → 多次 generate_image → 检查」、多智能体 `planner → writer → reviewer`）目前只存在于历史里，`skills/` 里只有手工安装的技能（如 anatomy）。#40 要把这些重复模式挖出来，变成带证据的技能候选，供 #41 巩固时由用户批准后写入。

## 决定

### 只读轨迹，只写候选

- 挖掘只读 `data/trajectories/`（必要时先做一次增量导出），**不读**会话正文、附件字节或遥测原文。
- 候选只写到 `data/meta/skill-candidates.json`（schema `mas.skill-candidates/1`），**绝不**写入或修改 `skills/`。自动安装属于 #41。
- Meta-Agent 仍只观察、只提议（ADR 0006）。

### 确定性挖掘优先

1. 从每条轨迹抽出工具步骤序列（`steps` 里 `kind=tool` 的 `name`）和多智能体委托序列（按 `parent_run_id` 还原的子 Agent 顺序）。
2. 对序列做 n-gram（默认 n=2..5），签名形如 `plan_image>generate_image>generate_image` 或 `agents:planner>writer>reviewer`。
3. 统计每个签名的出现次数、跨越的会话数、成功率（`outcome.status == completed`）、来源 `run_id`、脱敏后的典型场景摘要（来自顶层 `input.summary` / `output.summary`）。
4. 可配置门槛：默认最少出现 **3** 次、跨越 **2** 个会话；达不到的标成「证据不足」，**不**写成候选（避免噪声）。验收夹具用「同一模式出现 5 次 + 噪声」验证召回与拒识。
5. 与已安装技能（`services.runtime.skills.installed()`）做词元重叠，标出 `existing.form`（`implicit` / `existing_skill`）和巩固建议草稿。

### 模型归纳可选、默关

- `--model` 打开时，把**已经脱敏**的模式摘要（签名、次数、成功率、典型场景摘要，不含原文）交给模型，补全名称、描述和步骤草稿。
- 关掉时完全靠规则：名称由签名 slug 化，描述和步骤草稿由模板生成。验收要求关掉模型也能产出统计候选。

### 候选 schema `mas.skill-candidate/1`

字段见 [docs/mining.md](../mining.md)。每条候选带证据（`run_ids`、次数、成功率、典型场景）、与已有技能的重叠、巩固建议。版本规则同轨迹：加可选字段不升版本；破坏性改动升到 `/2`。

### 隐私

候选与模型归纳输入都按「会被发出去」处理：复用导出器的脱敏约定——不进本地正文、不进附件字节、密钥和绝对路径替换成占位符；写出前断言。check.py 对候选文件和归纳 prompt 做泄漏搜索。

### 入口

- 命令行：`python -m services.meta.mine [--data-dir DIR] [--min-count 3] [--min-sessions 2] [--model]`，以及 `list` / `show ID`。
- 接口：`POST /api/meta/mine`（跑一次挖掘）、`GET /api/meta/candidates`（列出）、`GET /api/meta/candidates/{id}`；只接受本机回环。

## 后果

- #41 巩固只消费 `skill-candidates.json`，不回头扫会话。
- 门槛调高会漏召回、调低会出噪声；默认值偏保守，夹具和 `--min-count` 可改。
- 模型归纳失败或未配置供应商时，静默退回纯统计候选，不阻断挖掘。
