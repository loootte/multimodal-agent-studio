# 上下文压缩与工具结果裁剪（14.B，#79）

长对话、连续出图时不再撞上下文上限。实现在 `services/runtime/compaction.py`，由 `services/runtime/loop.py` 每一步发请求前调用。

## 什么时候压缩

- 阈值 = 模型能力声明的 `context_limit`（`context_unit` 为 `tokens`）× `ratio`，默认 70%。
- 每一步估一次：`system` + 工具契约 + 消息的字数 ÷ 4（和 `Budget` 的 token 估算同一口径）。
- **没超过阈值什么都不做**：请求与不压缩时逐字节一致（评测 `runtime.compaction_keeps_plan` 比对）。
- `loop.run(..., compaction=...)`：`None` 默认开启；`False` 关闭；也可以传 `Compaction(ratio=, limit=, keep_tail=, trim_chars=, summarize=)`。

## 怎么压缩

1. **裁剪**：最近 `keep_tail`（默认 6）条以外、超过 `trim_chars`（默认 600 字）的工具结果只留结构字段
   （`ok`、`tool`、`executed`、`status`、`artifact_id`、句柄、`images_*`、错误码），另列结果里出现过的句柄，
   写明「结果较长，已裁剪：只保留结构字段，完整结果留在本机会话记录里」。
2. **摘要**：裁剪后还超，就把较早的消息压成一条摘要消息（`channel=context_summary`、`trust=untrusted`，网关只发 role/content）。
   默认摘要器是**确定性抽取**，不额外调用模型：每条一行（用户 / 助手截断到 120 字，工具结果只留结构字段），最多 40 行，
   末尾列句柄清单。再次压缩时上一条摘要并入新摘要。
3. 压缩后 token 没下降就放弃这次压缩。

### 固定保留

- 系统指令和技能目录（在 `system` 里，压缩不碰）；
- 每个固定工具最近一条结果：`plan_images`（计划张数）、`read_skill`（技能正文）、`accept_local_content` / `list_local_content` / `retrieve_local`（句柄）；
- 最近一条用户消息（本轮请求）和最近 `keep_tail` 条消息；
- 摘要里的句柄清单。

张数的权威在宿主（`chat_run` 的 `run._image_plan` / `_images_done`），不在消息里，压缩改不了；
`generate_image` 契约和执行路径不变，一次调用一张。确认在运行时里同步完成，没有跨步挂起的确认需要保留。

## 隔离

- 压缩只处理本来就发给过模型的消息；句柄不展开成正文。
- 摘要和裁剪结果在替换前先过 `egress.inspect`（明文、规范化、分片、编码窗、`local_fingerprint`，和网关同一套）；
  命中就记 `context.compaction_blocked`（只有原因码、条数、token 数）和 `security.egress_denied`，**这一步退回不压缩**；
  网关照常再查整个请求。
- 原始会话不改：压缩只换本次运行内存里的消息列表，会话存储（含 12.F 加密）不动。
- 事件 `context.compacted`（运行时产出并写遥测）只有 `turns_compacted`、`trimmed`、`messages_before/after`、
  `tokens_before/after`、`threshold`，没有正文。消费方（网页、子 Agent）忽略它也行。

## 评测

- `runtime.compaction_keeps_plan`：36 条长历史超过阈值 → 压缩后跑完，首个请求 token 降到一半以下；`plan_images` 计划固定保留，
  4 张计划出 4 张、每次 `generate_image` 一张；同样的 token 预算不压缩会 `budget_exceeded`；阈值以下请求逐字节一致；事件字段只有数字。
- `security.compaction_no_local_body`：替换摘要器把本地正文（明文和 base64）塞进摘要 → 被拦、记录、退回不压缩，任何请求里都没有本地正文；
  默认摘要器照常压缩，不误拦。

## 未做

- `session_query`（按引用取回被裁剪的原始结果）还没有：要加一个模型可见的新工具，会改变默认工具契约，待定。
- 模型生成的摘要（多一次远端调用）没有做；摘要器接口留好了，默认确定性抽取。
- token 是字数 ÷ 4 的估算，不是供应商分词。
- 最近 `keep_tail` 条消息原样保留，其中的大工具结果不裁，所以压缩后仍可能略高于阈值（只要不超过 `context_limit` 就不影响请求）。
- 网页没有显示「已压缩」提示。
