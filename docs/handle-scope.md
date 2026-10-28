# 作用域句柄（12.B，#58）

决策见 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md)。本文件写实现：句柄记录里多了什么、谁来校验、失败码、怎么撤销、怎么验。

## 一句话

本地内容句柄（`c_` + 32 位随机十六进制）从「会话字典主键」变成**短时能力令牌**：
绑定会话、用途（`purpose`）、可选运行、过期时间；只有本机宿主能把它解析回正文。
模型和页面看到的仍然只有句柄串和公开元数据。

## 记录字段（只在本机会话 JSON 的 `contents` 里）

| 字段 | 说明 |
| --- | --- |
| `handle_schema` | `mas.handle/1` |
| `scope.session_id` | 签发它的会话。消费时必须一致 |
| `scope.purpose` | `image_prompt` / `keep` |
| `scope.run_id` | 可选：只给这次运行用（`bind_run`，只能收窄不能改绑） |
| `allowed_tools` | 能消费正文的工具。默认按用途：`image_prompt` → `generate_image`；`keep` → 无 |
| `issued_at`、`expires_at` | UTC ISO 时间。默认 4 小时，`MAS_HANDLE_TTL_SEC` 可改（夹在 60 秒到 7 天） |
| `revoked_at` | 撤销时间（资源面板「撤销句柄」或 `revoke_run`） |
| `single_use`、`used_at` | 可选：一次性句柄，消费一次后作废 |
| `migrated_at` | 12.B 之前的旧记录读时升级的时间 |

公开字段（`list_local_content`、`retrieve_local` 结果、资源面板）在原来的
`handle/type/kind/media_type/size/purpose` 之外只加 `expires_at` 和 `state`（`active` / `expired` / `revoked` / `used`），
**不**带会话映射、运行绑定和正文。

## 校验

唯一入口：`SessionStore.resolve_content(session_id, handle, tool=, run_id=, consume=True)` → `(本机记录, "ok")` 或 `(None, 错误码)`。

| 情况 | 错误码 |
| --- | --- |
| 格式不对、伪造、本会话没有、**跨会话** | `handle_not_found`（跨会话和伪造不可区分，不给存在性预言机） |
| 过期、已撤销、一次性已用过 | `handle_expired` |
| 运行绑定不符、用途不允许这个工具（例如拿 `keep` 句柄出图） | `handle_forbidden` |

错误码登记在 `packages/contracts/events.py` 的 `HANDLE_ERRORS`，同时进 `ERROR_CODES` / `HUMAN` / `RETRYABLE` 和 `EGRESS_REASONS`。

消费点（`apps/web/chat_run.py`）：`_handle_block`（只检查）、`_bind_image`（消费）。它们覆盖
`generate_image` 的网页切片（`_generate_image_turn`、`_run_bound`）和运行时循环（`_execute_generate_image`，多 Agent 的 image 子 Agent 也走这里）。
本轮暂存的 `chat` / `model` 提示词（`stage_prompt`）只在这次运行的内存里，天然是运行作用域，不经过这里。

宿主自己读正文的地方（资源面板预览/下载/复用、确认卡摘要、出口检查 `local_bodies`）**不**做作用域校验：
它们不出本机；而且过期、撤销的正文仍要参与网关出口检查，免得被换个说法发出去。

## 被拒时

1. 不读正文、不提交 ComfyUI、不发第三方出图。
2. 出站审计记一条 `egress.blocked`：`channel` 是本来要去的地方（`comfyui_local` / `remote_image`），
   `reason_code` 是上面的错误码，`contains_local_derived=true`，`handle_id_prefix` 只有前 6 个字符。
3. 遥测记一条安全事件 `security.handle_denied`（`error_code`、`tool`、`handle_id_prefix`、`session_id`、`run_id`）。
4. 返回给模型 / 页面的错误带固定错误码和白话提示（`_HINTS`），不回显正文或完整的别的句柄。

## 签发、续期与撤销

- `add_content` / `put_content` / `add_file`：新记录签发作用域。同一正文（或同一文件字节）再次收下时，句柄还有效就复用；
  已过期、撤销或用过，就**换一枚新句柄**（旧串作废，不复活）。
- 用户这一句本来就要出图（例如「定时出图」收下的 `keep` 文字），宿主用 `allow_tool(…, "generate_image")` 放开；
  只能放开 `_GRANTABLE` 里登记的工具，模型没有这个入口。
- 资源面板：`POST /api/resources/revoke {id}`（只限本机）撤销；清单项带 `state`、`expires_at`；
  过期或撤销的内容点「用在当前对话」会拿到新句柄。
- 旧句柄（12.B 之前、没有 `expires_at`）：第一次被消费或再次收下时读时升级，句柄串不变，从那一刻起算 TTL（双读一期）。

## 验证

- `python apps/web/check.py`：`_handle_units`（会话、用途、过期、撤销、运行绑定、一次性、旧记录升级、伪造、TTL 夹取、公开字段白名单）
  和 `_handle_scope_case`（端到端：越权 purpose、过期、跨会话、资源面板撤销，四种都不出图、记审计、正文和完整句柄不进审计）。
- `python evals/run.py`：`security.handle_expired`、`security.handle_forbidden_purpose`、`security.handle_cross_session`，进 `evals/gate.py` 基线。

## 已知限制

- 运行绑定和一次性目前只在宿主 API（`bind_run`）提供，默认签发的句柄是会话级的；多 Agent 子运行共用父运行的 `run_id`，没有再细到子 Agent。
- 过期按本机时钟；改系统时间能延长或提前过期。
- 会话结束不自动作废全部句柄，靠 TTL；需要时用资源面板撤销或删除。
