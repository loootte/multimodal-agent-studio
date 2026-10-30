# 敏感操作强制确认与数据流图（12.E）

[#61](https://github.com/loootte/multimodal-agent-studio/issues/61)，父 issue [#56](https://github.com/loootte/multimodal-agent-studio/issues/56)，约定见 [ADR 0009](adr/0009-handle-scope-and-egress-audit.md)。建立在 12.A 出站审计（[egress-audit.md](egress-audit.md)）、12.B 句柄作用域（[handle-scope.md](handle-scope.md)）、12.C 出站策略（[egress-policy.md](egress-policy.md)）和 12.D 沙箱（[tool-sandbox.md](tool-sandbox.md)）之上，不新增出站路径。

## 1. 确认不可跳过

### 清单校验（装包 / 注册时）

`services/tools/packs.py` 的 `validate_manifest`：

| 清单 | 结果 |
| --- | --- |
| `permissions.network = true` 且 `confirm` 不是 `always`（`never` / `by_args`） | 拒绝，`invalid_manifest`，问题文字 `SENSITIVE_CONFIRM_PROBLEM` |
| `permissions.read_local_content = true` 且 `confirm` 不是 `always` | 同上 |
| 内置的只读句柄工具 `list_local_content` / `retrieve_local`（`HANDLE_ONLY_READERS`） | 例外：只返回句柄元数据、不出网，保持 `never` |

- 生效点：`POST /api/toolpacks/inspect` 的 `problems`、`install`（`invalid_manifest`，不落盘）、`discover`（安装目录里的坏清单让发现失败，不悄悄跳过）。
- 第三方包冒用内置名（如 `retrieve_local`）不享受例外：名单只对内置包生效，且重名本来就会被拒。
- 纵深：`needs_confirm` 对敏感包一律返回需要确认，即使将来清单校验被绕过。

### 运行时确认粒度

| 工具 | 一轮里确认几次 |
| --- | --- |
| 内置工具（如 `generate_image`，一轮连出 N 张） | 一次（不变；N 张 = N 次调用，只问一次） |
| 第三方包且 `network` 或 `read_local_content`（`confirm_every_call`） | 每次调用都问，不复用同一轮里之前的同意 |
| 本地提示词（1.3）发第三方出图（`remote_local_prompt=confirm`） | 按（这一轮, 句柄, 目的主机）问一次；换句柄或换主机重问；拒绝不记住；下一轮重问 |

子 Agent 冒泡的确认同样遵守 `confirm_every_call`。审阅（`review`）里多 `confirm_every_call`，工具列表的 `pack.sensitive` 标出敏感包。

## 2. 确认卡固定区块

确认事件的 `egress`（宿主生成，`by: "host"`）总带三项，顺序固定：

| `key` | `label` | 值 |
| --- | --- | --- |
| `destination_host` | 出站主机 | 主机名加范围说明（如 `127.0.0.1（本机）`）；第三方包写「由工具包运行时决定（每次连接记进「数据出站」）」；不联网写「无（不联网）」 |
| `contains_local_derived` | 含本地派生内容 | 是 / 否（本地句柄出图，或沙箱包同时读本地和联网） |
| `handle_purpose` | 句柄用途 | 如 `出图提示词（image_prompt）`；没有句柄写「不用句柄」 |

同时平铺 `destination_host`、`contains_local_derived`（bool）、`handle_purpose`、`local_to_third_party`。本地派生发第三方时，卡片再加一行红字「本地提示词将发往第三方出图服务（不会发给对话模型）」。页面用 `textContent` 渲染，不吃模型文字。12.G 起确认事件另带宿主模板卡 `card`（`mas.confirm/1`），高危组合要二次确认，见 [injection-guard.md](injection-guard.md)。

## 3. 放宽隐私的设置要显式确认

受保护项登记在 `apps/web/tool_config.py` 的 `GUARDED`，目前只有 `generate_image.remote_local_prompt = confirm`：

- `GET /api/tools/generate_image/config` 多一个 `warnings`：`{"remote_local_prompt": {"confirm": "本地提示词将发往第三方出图服务（正文不发给对话模型）。每一轮出图前仍会单独请你确认。"}}`。设置页选中该值时显示红框警示。
- `PUT …/config` 把它从别的值改成 `confirm` 时，必须带 `"acknowledge": ["remote_local_prompt"]`；否则 **409** `{"code": "acknowledge_required", "warnings": {...}}`，配置不变。网页弹窗确认后带上 `acknowledge` 重发。改回 `refuse` 不需要确认。
- 每次受保护项真的变了，记遥测安全事件 `security.policy_changed`（`trace_id` 为 `cfg_…`，字段只有 `tool` / `field` / `from_value` / `to_value` / `via`），在 `/api/telemetry` 可查。
- 配置变更不走工具：模型没有改配置的工具，只能由人在设置页或直接调接口改。12.G 起工具执行期间写配置一律拒绝，跨站写配置 403，成功写入记 `security.config_changed`，见 [injection-guard.md](injection-guard.md)。

## 4. 会话级数据流图

`GET /api/dataflow?session_id=s_…`（只给本机回环，`session_id` 不合法 400，会话不存在 404），由 `apps/web/dataflow.py` 从 12.A 审计记录和句柄公开字段拼出，`schema: "mas.dataflow/1"`：

```
本地内容 ──holds──▶ 句柄 abc123… ──uses──▶ generate_image ──egress──▶ 本机 ComfyUI（127.0.0.1）
                                                          └──egress──▶ 第三方出图服务（img.example）
                                      对话模型请求（只带句柄名） ──egress──▶ 第三方对话模型（llm.example）
```

- `lanes`：`local` / `handle` / `tool` / `destination`。
- `nodes`：句柄只给前 6 位（`handle_id_prefix`）和 `purpose` / `state`；工具按审计里的 `tool` 字段（12.E 起 `generate_image`、沙箱包都标），对话模型请求单列；目的地按 `destination_scope` 分 本机 / 局域网 / 第三方，本机 ComfyUI 单列。
- `edges`：`holds`、`uses`、`egress`，各带 `counts{sent,blocked,failed}`、`local_derived`、拦截原因码分布；`planned` 不计数。
- `summary`：总数和 `local_derived_to_third_party`。
- 没有正文、提示词、密钥、路径、完整句柄。`build` 只挑白名单字段，输入里混进别的字段也带不出来。
- 页面：运行过程侧栏「数据流图」，每轮结束和切会话时刷新，第三方目的地描红，拦下的边标底色。

## 5. 三分钟演示：拦 / 放行 / 确认

自动版（假供应商，几秒跑完）：

```
python evals/run.py --only security.confirm_never_network_rejected security.egress_audit_blocked security.egress_audit_normal security.dataflow_no_body security.remote_local_prompt_ack_required security.sensitive_tool_confirm_every_call
```

网页版讲稿（`python apps/web/server.py --port <空闲端口>`）：

1. **拦（约 1 分钟）**
   - 工具 → 安装一个 `network: true`、`confirm: "never"` 的包 → 检查结果列出「联网或读本地内容的工具必须 confirm=always」，装不上。
   - 设置里 `remote_local_prompt` 保持默认 `refuse`，说「用第三方出图，提示词我在本机输入」 → 输入本地提示词 → 不出图；「数据出站」一行红色 `local_prompt_remote_refused`；数据流图 `generate_image → 第三方出图服务` 的边记「拦下 1」。
2. **放行（约 1 分钟）**
   - 说「画一只茶壶」并输入本地提示词 → 本机 ComfyUI 出图；数据流图：本地内容 → 句柄 → `generate_image` → 本机 ComfyUI（含本地派生），对话模型那条边只带句柄名。
3. **确认（约 1 分钟）**
   - 设置 → `generate_image` → `remote_local_prompt` 选 `confirm` → 红框「本地提示词将发往第三方出图…」→ 保存时再弹一次确认；`/api/telemetry` 里有 `security.policy_changed`。
   - 再走第三方本地提示词出图：确认卡固定区块写出站主机、含本地派生「是」、句柄用途；确认后才发，下一轮再问一次。
   - 装一个 `network: true`、`confirm: "always"` 的包，让模型一轮调两次 → 弹两次确认。

## 6. 局限

- 已经装好的旧包若是 `network` + `never`，升级后发现阶段报 `invalid_manifest`，要改清单重装。
- 数据流图只画经过 12.A 审计的出站（网关、出图客户端、沙箱连接）；沙箱包的载荷对宿主不透明，图里只有「连了哪台主机」。
- 轨迹里的隐私边类型（#44 反向用例，issue 里标可选）没做。
