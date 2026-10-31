# 长期记忆（14.D）

对应 [#81](https://github.com/loootte/multimodal-agent-studio/issues/81)。跨会话记住用户偏好和要复用的本地内容，只存在本机。默认关闭。

## 记录格式 `mas.memory/1`

存在 `data/memory/memory.json`，跟随 12.F 的存储模式（[storage-encryption.md](storage-encryption.md)）：

| 存储模式 | 记忆文件 |
| --- | --- |
| 明文（默认） | 明文 JSON；**默认不收 content 条目**（`content_needs_encryption`），用户在面板里勾「明文存储模式下也收本地内容记忆」才收 |
| 加密 | 整个文件用 `mas.enc/1` 信封封装，`scope: "memory"`，独立密钥 `HKDF(主密钥, "mas/memory/store")` |
| 仅内存 | 只在进程里，重启清空，不写盘 |

文件读不出来（损坏或密钥不对）时进入 `corrupt` 状态：不覆盖、拒绝写入（`memory_corrupt`），面板上有提示。

两类条目（最多 200 条）：

- **preference**：短文字（≤200 字，标签 ≤40 字），会进模型上下文。写入（模型或面板）和每次注入前都过本地正文检查：当前会话、本机其它会话的文字内容、记忆里的 content 正文，以及它们的指纹（`egress.inspect`，含 base64 等编码和 `local_fingerprint`）。命中就拒绝 / 不注入，记 `security.egress_denied`（只有原因码，通道 `memory`）。
- **content**：本地文字正文（≤32000 字），purpose `memory`，记下原用途 `origin_purpose`。对模型只显示 `memory_id` 和标签；正文永不进模型请求。v1 只收文字（文件返回 `memory_file_unsupported`）。

## 工具

三个宿主工具（`toolpacks/builtin/remember|recall|forget`），权限 `read_local_content` + `write_files`：

- `remember`：`kind=preference` 写偏好文字；`kind=content` 用当前会话的 `content_handle` + 标签把那段本地内容存进记忆（结果里没有正文）。`confirm: always`。
- `recall`：按 `memory_id` 或 `query` 查（最多 10 条）。content 条目由宿主**在当前会话重新签发一枚新句柄**（新串、当前会话作用域、原用途，最多重发 5 枚），12.B「句柄不能跨会话用」不变：会话 A 的句柄在 B 里仍然无效。`confirm: never`（只读，句柄形态，在 `HANDLE_ONLY_READERS` 里）。
- `forget`：删一条记忆，同时作废它在各会话里重发过的句柄。不存在时返回 `handle_not_found`。`confirm: always`。

`remember` / `forget` 每次调用都弹宿主模板确认卡（`confirm_every_call`，不记进「本轮已确认」）。

## 什么时候给模型

- 设置 `enabled=false`（默认）：三个工具不出现在任何模型请求里，系统提示里没有任何记忆，请求和以前逐字节一致。
- 打开后：单 Agent 文字轮提供三个工具；多智能体编排者、出图切片、MCP server 不提供。
- 系统提示（`inject_preferences=true` 时）每轮从记忆库重新注入：偏好文字（最多 `max_inject` 条）和 content 条目的 `memory_id`「标签」。压缩只动对话消息，所以记忆不会被压掉。

## 设置（`data/memory_settings.json`）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | `false` | 总开关 |
| `inject_preferences` | `true` | 每轮把偏好放进系统提示 |
| `max_inject` | `20` | 1–50 |
| `allow_plaintext_content` | `false` | 明文存储模式下是否收 content 记忆 |

非法值回退默认值。写入走 12.G 同源检查并记 `security.config_changed`（只有字段名）。

## 记忆面板

左栏「记忆」。只给本机（loopback）：

- `GET /api/memory`：设置、存储模式、条目（偏好含文字，content 只有标签和大小）
- `PUT /api/memory/settings`
- `POST /api/memory`：手写一条偏好（同样过本地正文检查）
- `PUT /api/memory/<id>`：改标签 / 偏好文字
- `POST /api/memory/<id>/delete`：删除并作废重发的句柄
- `GET /api/memory/<id>/preview`：本机预览 content 正文（只进浏览器）
- `GET /api/memory/export`：导出元数据（`bodies_included: false`，不含 content 正文）

面板里的写入是用户自己的操作，不弹工具确认卡。

## 评测

`evals/run.py` 的 `memory` 任务（全程替身模型、走真实网页对话路径）：

- `security.memory_content_handle_only`：content 从 A 记下、在 B recall，正文从不进模型请求，B 拿到新句柄；明文存储默认拒收；加密模式下盘上读不到正文和标签
- `security.memory_preference_fingerprint`：原文 / base64 / 别的会话的正文伪装成偏好都被拦，面板手写也被拦
- `security.memory_cross_session_reissue`：新句柄只在 B 有效，A 的旧句柄在 B 无效；偏好出现在 B 的请求里
- `runtime.memory_off_no_context`：关着时请求里没有记忆工具和记忆文字
- `runtime.memory_forget_recall`：删除后 recall 返回 `handle_not_found`，重发的句柄被作废；`forget` 弹确认
- `runtime.memory_preference_cross_session`：A 经确认记下的偏好出现在 B 的系统提示里

## 不做

无确认的自动抽取；云同步。
