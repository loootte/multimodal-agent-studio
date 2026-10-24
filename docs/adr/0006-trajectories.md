# ADR 0006：轨迹导出与索引（Meta-Agent 的数据边界）

## 状态

已接受。对应 [issue #39](https://github.com/loootte/multimodal-agent-studio/issues/39)，父 issue [#36](https://github.com/loootte/multimodal-agent-studio/issues/36)（Meta-Agent）。

## 背景

#36 要做一个 Meta-Agent：从智能体自己的历史轨迹里挖掘技能、巩固记忆、评估工具化。第一步是“获取轨迹”。现在历史分散在三处：

- 会话文件（`data/sessions/*.json`）：每条助手消息的 `events`（工具调用、产物、Agent 节点）和 `runtime`（运行过程的模型、工具步骤）。持久，但不带 token、不带父子关系以外的委托细节。
- `data/telemetry.jsonl`：按 trace_id（等于顶层 run_id）记录 web.request、provider、tool、agent.run、run 等，含耗时、token、失败分类和 `parent_run_id`。持久，但子 Agent 的工具和模型调用以前分不出属于哪个子运行。
- `services/runtime/trace.py`：进程内的按运行追踪，重启就丢。

## 决定

### Meta-Agent 的定位

Meta-Agent 只观察、只提议。它读轨迹，产出技能候选、巩固建议和工具化草案；**任何对技能、工具、Agent 清单或配置的改动都要用户批准**，并走已有的安装路径（ADR 0004、0005）。本 ADR 只覆盖第一步：把轨迹导出成稳定、去敏的格式。挖掘、聚类、改动都不在范围内。

### schema 与版本

- 每次运行（顶层运行或子 Agent 运行）一条记录，`schema: "mas.trajectory/1"`。字段见 [docs/trajectory.md](../trajectory.md)，由 `services/meta/trajectory.py` 的 `validate()` 校验，导出器只写通过校验的记录。
- 树结构：会话 → 回合（一条用户消息和它的助手回答，`turn_id` = 助手消息 id）→ 运行（`run_id`、`parent_run_id`）→ 步骤（模型调用和工具调用，带结果、耗时、token）。委托关系从 `parent_run_id` 还原成树。
- 版本规则：只加可选字段时不改版本；删字段、改含义或改类型时升到 `mas.trajectory/2`，导出器同时能读旧索引并重建。读取方必须忽略不认识的字段。
- 为了把子 Agent 的工具和模型调用归到子运行，遥测记录新增 `run_id` 字段（运行时在每次 `loop.run` 期间把当前 run_id 绑在线程上）。旧的遥测没有这个字段时，记到顶层运行上。

### 存储位置

- `data/trajectories/trajectories.jsonl`：一行一条记录，只追加。
- `data/trajectories/index.json`：增量索引。记录每个已导出 run_id 的摘要（会话、回合、Agent、角色、模式、开始时间、结局、工具、技能、行号），以及每个会话已导出到的时间。重复导出幂等：已在索引里的 run_id 不再写。还在回答（`running`）的回合不导出，下次再导。
- 都在本机数据目录里，和会话、遥测同一个边界。不同步、不上传、不发给任何供应商。

### 隐私规则

轨迹是给模型看的候选输入，按“会被发出去”的标准处理：

1. 本地内容一律不进轨迹：句柄背后的正文和文件只留句柄、种类和长度。用户消息如果就是本地正文（有对应的 content），只记句柄。
2. 用户消息和助手文本只留截断的摘要（120 字），先用 `store._cover_local` 把出现的本地正文换成句柄说明，整条记录最后再过一遍 `store._scrub_value`（密钥集合 = 本会话所有本地正文 + 本地文件和附件字节的 utf-8 文本与 base64）。
3. 附件和产物只留句柄、种类、媒体类型和大小，不读入、不写出原始字节。
4. 工具参数只留摘要：句柄、数字、布尔原样；字符串只记长度（`read_skill` 的技能名例外）。
5. 密钥和令牌脱敏：provider.json 里的 API key，以及形如 `sk-…`、`ghp_…`、`Bearer …`、`api_key=…` 的串换成 `[secret]`。
6. 绝对路径脱敏：Windows 盘符路径、UNC 路径和 `/home`、`/Users`、`/root`、`/tmp`、`/workspace`、`/mnt` 等开头的路径以及数据目录本身换成 `[path]`。
7. 导出后再做一次断言式检查：记录序列化后如果仍包含任何本地正文、文件文本或密钥，这条记录丢弃并计数，不写入。

### 保留期

默认保留 90 天：每次导出时删掉开始时间早于 90 天的记录并重写文件和索引（`--keep-days` 可改，0 表示不删）。会话被删除（ADR 0003 的删除对话）后，下次导出把属于该会话的轨迹一并删掉；本地内容被“忘掉”后，下次导出重新生成该会话的轨迹，保证被忘掉的内容不会因为旧轨迹而留存。

### 内存里的 trace.py

保留，职责不变：它是一次运行内的步骤缓冲，给运行过程窗口和 Agent 树实时用。它不持久化，也不作为轨迹来源；持久的来源只有会话文件和遥测。为了不丢信息，运行时写进 trace 的工具和模型步骤本来就同步写进了会话的 `runtime` 和遥测。

### 入口

- 命令行：`python -m services.meta.export [--data-dir DIR] [--since 2026-10-01] [--keep-days 90]`，以及 `python -m services.meta.export query --tool generate_image --agent writer --skill anatomy --outcome failed --since ... --until ...`。
- 接口：`GET /api/trajectories?tool=&agent=&skill=&outcome=&since=&until=&limit=`，只读，只接受本机回环地址的请求；调用时先做一次增量导出再查询。

## 后果

- Meta-Agent 后续的挖掘只读 `trajectories/`，不直接读会话和遥测。
- 新增的遥测字段 `run_id` 只是附加字段，现有的 `/api/telemetry` 和汇总不受影响。
- 单 Agent 和多智能体的行为不变。
