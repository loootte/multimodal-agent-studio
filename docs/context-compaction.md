# 上下文压缩与工具结果裁剪（14.B，#79）

长对话、连续出图时不再撞上下文上限。实现在 `services/runtime/compaction.py`，由 `services/runtime/loop.py` 每一步发请求前调用。

## 什么时候压缩

- 阈值 = 模型能力声明的 `context_limit`（`context_unit` 为 `tokens`）× `ratio`，默认 70%；设了 `limit` 就用 `limit` 代替 `context_limit`。
- 每一步估一次：`system` + 工具契约 + 消息的字数 ÷ 4（和 `Budget` 的 token 估算同一口径）。
- **没超过阈值什么都不做**：请求与不压缩时逐字节一致，工具契约里也没有 `session_query`（评测 `runtime.compaction_keeps_plan`、`runtime.compaction_session_query` 比对）。
- `loop.run(..., compaction=...)`：`None` 默认开启；`False` 关闭；也可以传 `Compaction(...)` 或设置 dict（见下面「配置」）。

## 怎么压缩

1. **裁剪**：最近 `keep_tail` 条以外、超过 `trim_chars` 字的工具结果只留结构字段
   （`ok`、`tool`、`executed`、`status`、`artifact_id`、句柄、`images_*`、错误码），另列结果里出现过的句柄，
   并带上 `ref: ctx_N` 和「需要原文时用 session_query 按 ref 取回」。
2. **摘要**：裁剪后还超，就把较早的消息压成一条摘要消息（`channel=context_summary`、`trust=untrusted`，网关只发 role/content）。
   摘要正文有两种来源（`summary` 设置）：
   - `model`（默认）：见下面「模型摘要」。
   - `deterministic`：确定性抽取，不额外调用模型：每条一行（用户 / 助手截断到 120 字，工具结果只留结构字段），最多 40 行，每行带 `ctx_N`。
   不管哪种，宿主都在末尾追加 `[句柄清单]` 和 `[可取回] ctx_…`（后者只在 session_query 开着时）。再次压缩时上一条摘要并入新摘要，旧的引用保留。
3. 压缩后 token 没下降就放弃这次压缩。

### 固定保留

- 系统指令和技能目录（在 `system` 里，压缩不碰）；
- 每个固定工具最近一条结果：`plan_images`（计划张数）、`read_skill`（技能正文）、`accept_local_content` / `list_local_content` / `retrieve_local`（句柄）；
- 最近一条用户消息（本轮请求）和最近 `keep_tail` 条消息；
- 摘要里的句柄清单。

张数的权威在宿主（`chat_run` 的 `run._image_plan` / `_images_done`），不在消息里，压缩改不了；
`generate_image` 契约和执行路径不变，一次调用一张。

## 模型摘要（`summary: model`）

- 发给摘要模型的是一份**转写稿**：用户 / 助手文字（每条最多 1200 字），工具结果**只有结构字段和句柄**（长正文不发），总长最多 16000 字；
  系统提示 `SUMMARIZER_SYSTEM` 要求只概括、句柄原样保留、不展开、不编造。
- 走同一个网关（照常做出站检查），不带工具，`max_output` 800；模型用 `summary_model`，没设就用本次请求的模型。
- **发出前**和**回来后**都过 `egress.inspect`（明文、规范化、编码窗、`local_fingerprint`）。命中、调用出错或回复为空，
  就**退回确定性抽取**，记 `context.summary_fallback`（只有原因码：`local_*` / `summary_failed` / `summary_empty`）；
  命中本地正文时另记 `security.egress_denied`（`channel=context_summary`）。
- 模型摘要前缀 `[模型摘要] 以下由模型根据较早轮次生成。`；句柄清单和可取回引用由宿主追加，不信模型。
- 整条压缩结果最后再过一次检查，命中就这一步不压缩（`context.compaction_blocked`）。
- 每次调用记 `context.summary_call`（耗时、成功与否、字数）。这次调用的 token **不计入** `Budget`。

## session_query（兜底取回原文）

- 被裁剪 / 折叠的原始消息记进档案（`compaction.Archive`），引用 `ctx_N`（不是句柄格式，不能当句柄用）。网页聊天里档案会存进本机会话记录，下一次运行也能取回（见下面「本地保留」）。
- **只在压缩实际发生、档案非空之后**，运行时才把 `session_query` 加进这一步的工具契约；阈值以下请求不变。
- 参数：`ref`（`ctx_N`）、`offset`（非负整数，分页）。返回一段最多 2000 字的原文和 `offset` / `total_chars` / `more` / `handles` / `attachments`。
  未知引用回 `not_found`；参数不对回 `invalid_tool_call`。
- 结果用 `injection.tool_result_message` 包成 `trust=untrusted` 的工具结果。
- 取回的文字先过 `egress.inspect`：含本地正文就**不给正文**，只回句柄和原因码，记 `security.egress_denied`（`channel=session_query`）；网关之后照常再查。
- **放在哪**：放在运行时（`services/runtime`），和 `delegate` 一样是运行时自己的工具契约、由 `loop.py` 直接处理，不走工具平面、不需要确认。
  原因：档案由运行时持有、由宿主读写（工具包拿不到运行的原始消息）；做成工具包会一直出现在 `openai_tools()` 里，破坏「阈值以下逐字节不变」；
  它只读本次运行自己的档案，不改状态、不出站。请求本来就提供了同名工具时不覆盖。

### 本地保留（跨运行取回）

- 网页聊天（`apps/web/chat_run.py`）每次运行开始从会话记录读出档案（`SessionStore.context_archive`）传给 `loop.run(archive=...)`，
  运行结束（成功、出错都一样）如果这次新增了条目就写回（`save_context_archive`）。写回失败只记 `context.archive_save_failed`（错误码），不影响回复。
- 档案就是会话记录里的 `context_archive` 字段（`{"version": 1, "items": {"ctx_N": 原始消息}}`），所以和会话走同一套 12.F 存储：
  明文模式是本机 JSON，加密模式是 `mas.enc/1` 信封（按会话的密钥），只在内存模式不落盘。**不出本机**，不发给任何远端。
- 会话视图（`view`）和轨迹导出只取消息、本地内容、附件这几个字段，不带档案；删会话一起删；丢掉本地内容（`forget_content`）时档案里含它的原文一并抹掉。
- 同一条原文（按角色和内容的 sha256）在后面的运行里再被压缩时复用原来的引用，不重复存；新引用接着编号，不和旧的冲突。
- 压缩时「可取回」那一行会列出之前运行存下的引用（最多 60 个），模型能直接按引用取回。
- 上限：400 条、200 万字，超了丢最旧的。版本不对或内容损坏时当空档案处理。
- 取回规则不变：仍过 `egress.inspect`，含本地正文只给句柄，结果标 untrusted。
- 工具仍然**只在本次运行压缩之后**才提供：之前存下的引用要等这次运行也发生压缩才能取回（为了保住阈值以下逐字节不变）。

## 配置

设置页「上下文压缩」一节，或 `GET / PUT /api/context`（只收本机界面，同 12.G 的 Origin / Sec-Fetch-Site 规则；写入记 `security.config_changed`，只有字段名）。
存在 `data/context.json`，只存和默认值不同的项。`PUT {"values": {...}}` 合并保存；值为 `null` / 空串清掉该项；`{"reset": true}` 恢复默认。

| 字段 | 默认 | 合法范围 |
| --- | --- | --- |
| `enabled` | `true` | 布尔 |
| `ratio` | `0.7` | 0.1–0.95 |
| `limit` | 空（用模型 `context_limit`） | 1000–10000000 的整数 |
| `keep_tail` | `6` | 2–50 |
| `trim_chars` | `600` | 80–100000 |
| `summary` | `model` | `model` / `deterministic` |
| `summary_model` | 空（用请求的模型） | 模型名（字母数字开头，可含 `._:/@+-`，最长 128） |
| `session_query` | `true` | 布尔 |

校验逐项进行（`compaction.settings`）：非法值不存、回退默认值并列进返回的 `warnings`；不认识的键忽略并警告；文件坏了整个用默认值。
网页聊天从这里读设置传给 `loop.run`；子 Agent 用默认设置。

## 隔离

- 压缩只处理本来就发给过模型的消息；句柄不展开成正文。
- 摘要（两种来源）、裁剪结果、session_query 取回的文字都过 `egress.inspect`，和网关同一套；网关照常再查整个请求。
- 原始会话消息不改：压缩只换本次运行内存里的消息列表；档案只追加到本机会话记录的 `context_archive` 字段。
- 事件 `context.compacted`（运行时产出并写遥测）只有 `turns_compacted`、`trimmed`、`messages_before/after`、
  `tokens_before/after`、`threshold` 和 `summary_by`（`model` / `host` / `none`），没有正文。

## 评测

- `runtime.compaction_keeps_plan`：长历史超过阈值 → 压缩后跑完，token 明显下降；`plan_images` 计划固定保留，4 张计划出 4 张、每次一张；
  同样的 token 预算不压缩会 `budget_exceeded`；阈值以下请求逐字节一致；事件字段只有数字和 `summary_by`。
- `security.compaction_no_local_body`：替换摘要器把本地正文（明文和 base64）塞进摘要 → 被拦、记录、退回不压缩；默认摘要照常压缩。
- `runtime.compaction_session_query`：压缩后才提供 `session_query`；按引用取回原文、分页、未知引用 `not_found`；结果 untrusted；
  关掉开关没有工具也没有引用；归档原文含本地正文时只给句柄、记 `session_query` 安全事件，正文不进任何请求。
- `security.compaction_model_summary`：用上模型摘要（前缀、宿主句柄清单和可取回引用）；发给摘要模型的只有结构字段和句柄；
  摘要回来含本地正文 → 退回 + `context.summary_fallback` + `context_summary` 安全事件；折叠输入含本地正文 → 不调用摘要模型；
  空回复 → `summary_empty` 退回；确定性模式不调用。
- `security.compaction_cross_run`：加密会话存储里，第一次运行压缩后档案写回（落盘是密文、视图里没有）；换存储实例读回后，
  第二次运行（历史里已没有那条工具结果）压缩后按旧引用取回原文（untrusted），本地正文只给句柄并记安全事件；新引用不冲突；
  同一条原文复用引用；丢掉本地内容后档案里也没了。
- `runtime.compaction_settings`：默认值、合法值、非法值逐项回退和警告、非字典、`enabled=false`、运行时按 dict 设置跑。
- `apps/web/check.py`：`/api/context` 读写、非法值回退、reset、跨站写入被拒。

## 未做

- 摘要调用的 token 不计入 `Budget`（只记遥测）。
- token 是字数 ÷ 4 的估算，不是供应商分词。
- 之前运行存下的引用要等本次运行也发生压缩才能取回；不跨会话取回。
- 档案上限 400 条 / 200 万字，超了丢最旧的。
- 默认是模型摘要；确定性抽取作为可选默认、摘要模型单独配置供应商都还没做（见 ROADMAP 14.B 后续）。
- 子 Agent 用默认压缩设置，不读设置页。
- 最近 `keep_tail` 条消息原样保留，其中的大工具结果不裁，所以压缩后仍可能略高于阈值。
- 网页没有显示「已压缩」提示。
