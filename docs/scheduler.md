# 定时任务（14.E）

对应 [#82](https://github.com/loootte/multimodal-agent-studio/issues/82)，按 [ADR 0011](adr/0011-external-capabilities-isolation.md)「无人值守」一节实现。默认关闭。

一句话：**到点只把任务放进「待开始」队列，每次都要用户在窗口里点「开始本次」才会跑**。跑起来以后和普通单 Agent 对话一样，要确认的照样弹确认。唯一的例外是用户自己在面板里打开的「常驻授权」，它只免掉「内置 `generate_image` → 本机 ComfyUI」这一种确认。

## 结构

| 位置 | 作用 |
| --- | --- |
| `services/scheduler/spec.py` | 周期：`once` / `every`（1 分钟–7 天）/ `daily` / `weekly`（`days` 0=周一）/ `cron`（5 段）；`next_fire`、中文描述 |
| `services/scheduler/jobs.py` | `JobStore`：任务、正文、`scheduled` 句柄、常驻授权、运行历史（每个任务最近 30 条） |
| `services/scheduler/runner.py` | `Scheduler`：后台线程每 15 秒 `tick` 一次；待开始队列、运行中表、超时、重叠、错过、补跑 |
| `services/runtime/scheduler.py` | 设置默认值和上限（`data/scheduler_settings.json`） |
| `apps/web/schedule_api.py` | 面板接口、模型工具、启动一次运行、常驻授权判定 |
| `toolpacks/builtin/schedule_{create,list,update,delete}` | 给模型的 4 个宿主工具 |
| `apps/web/static/schedule.js` | 网页「定时」面板 |

## 存储

`data/schedules/schedules.json`，跟随 12.F 存储模式（[storage-encryption.md](storage-encryption.md)）：

| 存储模式 | 任务文件 |
| --- | --- |
| 加密 | `mas.enc/1` 信封，`scope: "schedule"`，独立密钥 `HKDF(主密钥, "mas/schedule/store")` |
| 明文（默认） | 明文 JSON；**默认不收出图任务**（提示词是本地正文），要在面板勾「明文存储模式下也收出图任务」 |
| 仅内存 | 只在进程里，重启清空 |

文件读不出来时进入 `corrupt`：不覆盖，拒绝写入（`schedule_corrupt`）。正文最长 8000 字。

## 句柄 `scheduled`

- 每个任务建好时签发一枚 `sh_` + 32 位十六进制的句柄，purpose `scheduled`，**绑定任务 id**，TTL 由 `MAS_SCHEDULED_HANDLE_TTL_SEC` 决定（默认 604800 秒 = 7 天）。
- 别的任务拿这枚句柄是 `handle_forbidden`；过期、被撤销是 `handle_expired`。
- 到点时先查句柄：过期就不进队列，任务变成「待重新授权」（`needs_reauth`，停止原因 `handle_expired`）。
- **改正文、改类型、改张数**都算改内容：旧句柄作废、常驻授权作废、这个任务的待开始项被丢弃、正在跑的这一轮被停、每次运行在会话里签发过的 `image_prompt` 句柄全部撤销，任务进入 `needs_reauth`。只改名字、周期、上限、补跑、启用开关不算改内容。
- 面板「重新授权」签发新句柄，任务回到 `active`。
- 删任务：正文、句柄记录、常驻授权、待开始项一起清掉，会话句柄撤销。
- 每次运行时，宿主在任务自己的会话里把正文登记成一枚普通的 `image_prompt` 句柄（12.B，会话作用域），模型只见这枚句柄，正文不进模型请求。

## 到点以后

1. `tick` 发现到点 → 检查：总开关、任务启用、句柄有效、当天次数没用完、上一轮没有在跑 / 等待中。
2. 通过 → 放进待开始队列，发桌面通知（`desktop_notify` 的 `schedule` 模板），面板「待开始」里出现一项。**这时不碰模型。**
3. 用户点「开始本次」→ 在任务自己的会话里开一轮单 Agent 运行；点「跳过」→ 记 `dismissed`。
4. 超过 `confirm_timeout_min`（默认 30 分钟）没点 → 移出队列，历史记 `confirm_timeout`。
5. 运行里的工具确认也用这个等待上限；超时就停下这一轮，历史记 `confirm_timeout`。

上一轮还在等或还在跑时又到点：记 `skipped_overlap`，不排队。

错过（到点后超过 120 秒才检查到，比如电脑睡眠或程序没开）：**不补**，记 `missed`，排下一次。勾了「补跑」的任务在程序启动后最多补一次。

## 上限

每个任务有自己的上限（面板可改，夹在范围里）：

| 键 | 默认 | 范围 |
| --- | --- | --- |
| `steps` | 12 | 1–24 |
| `time_min` | 10 | 1–120 |
| `tokens` | 50000 | 1000–200000 |
| `daily_runs` | 4 | 1–48 |

出图任务每次 1–4 张（`images`）。步数 / 时间 / token 超了，运行停下，历史记 `budget_exceeded`；当天次数用完记 `daily_runs`，当天不再进队列。每条历史都有状态和停止原因（`completed` / `failed` / `stopped` + `budget_exceeded` / `cancelled` / `confirm_timeout` / …）。

## 常驻授权（用户选项 C）

- 只对**出图任务**；只能在面板里由用户打开，两步：服务端给一张宿主模板确认卡（`POST jobs/<id>/standing`，5 分钟有效），用户确认后带 `acknowledge: "standing"` 提交（`standing-confirm`）。模型工具和面板新建 / 修改接口都不收 `standing` / `preauthorize` 字段（模型传了是 `standing_by_model`）。
- 绑定：任务 id + 当前句柄 + ComfyUI 主机。句柄换了（改内容、过期、重新授权）、删任务、ComfyUI 地址变了，都立即作废。
- 只给本机（scope `local`：127.0.0.1 / localhost）的 ComfyUI。局域网 ComfyUI 不能开（比「局域网算本地」更严）。
- 免确认的条件全部满足才成立：这一轮是该任务的运行、工具是内置包的 `generate_image`、`backend` 缺省或 `local`、`content_handle` 是这一轮宿主登记的那枚句柄且句柄里的正文就是任务正文、这一轮已用次数小于 `images`、ComfyUI 是本机、授权有效。
- 不满足就照常弹确认：远端出图（`backend: remote`，高危）、其它 `confirm: always` 的工具、MCP 工具、第 `images+1` 张。
- 即使有常驻授权，每次到点仍要点「开始本次」。张数只由 `plan_images` 决定，每次 `generate_image` 一张。

## 模型工具

只有总开关打开时，单 Agent 文字轮才提供（多智能体、出图切片、任务自己的运行里都不提供——任务不能改自己）：

- `schedule_list`：只给编号、名字、类型、周期、状态、上限，不给正文和任务句柄。`confirm: never`。
- `schedule_create` / `schedule_update` / `schedule_delete`：每次调用都弹宿主模板确认卡（`confirm_every_call`）。出图任务只收当前会话的 `content_handle`（文件句柄拒收）；文字任务的 `instruction` 发前过本地正文检查（当前会话、其它会话、记忆、其它任务的正文和指纹），命中是 `local_content` 并记 `security.egress_denied`（通道 `schedule`）。模型建的任务 `source: model`，没有常驻授权。

总开关关着（默认）：这 4 个工具不出现在任何请求里，请求和以前逐字节一致；`tick` 什么都不做；`run-now` 返回 `scheduler_disabled`。

## 设置（`data/scheduler_settings.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关 |
| `confirm_timeout_min` | 30 | 「开始本次」和运行里确认的等待上限（1–1440） |
| `max_jobs` | 20 | 最多几个任务（1–100） |
| `allow_plaintext_image` | `false` | 明文存储模式下是否收出图任务 |

## 接口（只给本机，写操作记配置审计）

`GET /api/schedule`、`PUT /api/schedule/settings`、`POST /api/schedule/jobs`、`PUT /api/schedule/jobs/<id>`、`GET jobs/<id>/preview`（本机预览正文）、`POST jobs/<id>/{delete,reauthorize,standing,standing-confirm,standing-revoke,cancel,run-now}`、`POST pending/<id>/{start,dismiss}`。「现在跑一次」等同于到点，同样进待开始队列。

## 审计

遥测事件只有编号和原因码：`schedule.due` / `started` / `finished` / `stopped` / `timeout` / `handle_revoked` / `standing_granted` / `standing_used` / `tick_failed`。

## 会话

每个任务有自己的会话（第一次运行时创建，之后复用），面板「打开会话」可以看每次运行的过程；运行中可「取消运行」。第 9 阶段 #43 的定时入口复用这个调度器（`app.scheduler` + `schedule_api.launch`）。

## 测试

```bash
python3 evals/run.py --only runtime.schedule_off_no_tools security.schedule_no_auto_confirm \
  security.schedule_handle_expiry security.schedule_cross_job_handle runtime.schedule_budget_stop \
  runtime.schedule_overlap_missed security.schedule_model_create_confirm
```

手动：设置 → 打开「定时」面板 → 打开总开关 → 新建一个每 5 分钟的出图任务 → 「现在跑一次」→ 看到桌面通知和「待开始」→ 点「开始本次」→ 生成时弹确认；再「打开常驻授权…」后重跑，本机出图不再弹确认。

## 没做

- 任务带附件 / 文件（现在只收文字正文和文字句柄）。
- 多智能体任务（任务运行强制单 Agent）。
- 待开始队列不持久化：程序重启后队列清空（错过的按「补跑」规则处理）。
- 局域网 ComfyUI 不能开常驻授权。
- #43 的具体入口还没接，只留了复用点。
