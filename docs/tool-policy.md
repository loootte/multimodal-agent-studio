# 工具安全策略（#93）

MCP 出站控制和工具安全检查的开关，统一放在一份配置里：`<data_dir>/tool_policy.json`（`mas.toolpolicy/1`）。代码在 `services/tools/policy.py`。现有检查（MCP 主机判定、参数名黑名单、确认、结果截断）都读这一份配置。

**没有配置 = 最严格。** 文件不存在、坏了，或者某一项不合法，这一项就用默认值，并在设置页和 API 里给出 warning（只有字段路径，不含取值）。默认值就是 #93 之前的行为，局域网地址比以前更严（以前没拦）。

## 结构和优先级

```json
{"schema": "mas.toolpolicy/1",
 "global": {"params_block": ["query"]},
 "packs": {"my_mcp": {"stdio_commands": ["npx"], "http_loopback": true,
                      "auth": {"header": "Authorization", "scheme": "bearer", "source": "env", "env": "MAS_MCP_MY_TOKEN"}}},
 "tools": {"my_mcp__search": {"params_allow": ["text"], "confirm": "always", "result_max_chars": 4000}}}
```

逐个字段按 `tools` > `packs` > `global` > 默认值取值，更具体的一层说了算。`tools` 的键是模型看到的工具名（MCP 远端工具是 `<包名>__<远端名>`）。

| 字段 | 默认 | 可写在哪 | 放宽项 | 说明 |
| --- | --- | --- | --- | --- |
| `stdio_commands` | `[]` | global / packs | 是 | stdio MCP 可以用的裸命令名，例如 `npx`。默认只许包里的 `.py` 在 12.D 沙箱里跑 |
| `http_loopback` | `false` | global / packs | 是 | 允许连回环地址（127.0.0.0/8、::1、localhost）。本程序自己的端口永远不行 |
| `http_link_local` | `false` | global / packs | 是 | 允许连链路本地地址（169.254/16、fe80::/10，含云元数据地址） |
| `http_private` | `false` | global / packs | 是 | 允许连局域网地址（10/8、172.16/12、192.168/16、100.64/10、fc00::/7、fec0::/10） |
| `auth` | `null` | 只能 packs | 是 | HTTP MCP 的认证头，只存引用，不存值，见下文 |
| `params_allow` | `[]` | 三层都行 | 是 | 从参数名黑名单里放行的名字（只对 MCP 远端工具生效） |
| `params_block` | `[]` | 三层都行 | 否 | 加进黑名单的参数名 |
| `confirm` | `manifest` | 三层都行 | 否 | `always` 表示每次都确认。只能收紧（`never` 不合法），对非 MCP 工具也生效 |
| `result_max_chars` | `16000` | 三层都行 | 否 | MCP 结果回给模型的最大字数，范围 500–16000，只能收紧 |

参数名黑名单的基础值是 `prompt` / `text` / `body` / `content` / `bytes` / `path` / `filename`。生效的黑名单 = 基础值 + 各层 `params_block` − 各层 `params_allow`。它也是 MCP 工具在模型工具表里的参数检查（`registry._require_tool`）。非 MCP 包的清单静态检查（`BLOCKED_EXTERNAL`）不开放配置。

列表字段里只要有一个值不合法，整个字段作废并回退到默认值，不会只留下一部分。

### 命令模式（`stdio_commands`）

策略放行命令后，MCP 包可以写 `"entry": {"kind": "mcp", "transport": "stdio", "command": "npx", "args": ["-y", "some-server"]}`。

- 命令不是 Python，**在 12.D 沙箱外运行**：能读写用户能读写的文件，能自己联网。
- 宿主只做这几件事：清空环境变量（保留 `PATH` / `PATHEXT` / `COMSPEC` / `SYSTEMDRIVE`），工作目录设为包目录，超时，限制输出。
- 参数只许 `[A-Za-z0-9@._/:=+,~-]`，没有 `%` 和 shell / cmd 元字符。
- 发给它的每条消息照样先过本地正文检查，它的回复照样不可信。

安装审阅里会写明这一点，要勾 `external_command`。

### 回环、链路本地和局域网

- HTTP MCP 平时只收 `https://`。明文 `http://` 只许写 `localhost` / `127.0.0.1` / `::1`，而且要放行 `http_loopback`。
- 每次连接前都会解析主机并给每个地址分类。只要有一个地址落在没放行的类别里就拒绝，返回 `policy_denied` 并记出站审计。
- 未指定地址（0.0.0.0、::）、组播 / 保留地址、解析不了的主机、本程序自己的端口，这几类**永远拒绝**。

### 认证头（`auth`）

```json
{"header": "Authorization", "scheme": "bearer", "source": "env", "env": "MAS_MCP_MY_TOKEN"}
{"header": "X-Api-Key", "scheme": "none", "source": "keychain"}
```

- `source=env`：从环境变量读值，变量名必须以 `MAS_MCP_` 开头。
- `source=keychain`：从系统钥匙串的 `mcp.<包名>.auth` 读值。这个值只能经 `PUT /api/tool-policy/secret` 写进去，而且只在桌面壳里可用（网页模式返回 409 `keychain_unavailable`）。
- `Host`、`Cookie`、`Content-Type` 和 `Mcp-*` 这类请求头不能拿来当认证头。

密钥值**不写盘**：策略文件里只有引用。它也**不进日志和审计**：审计只记字段名。它**不给模型**：回给模型的结果、错误文字和 `public()` 都会扫一遍，把密钥换成 `[redacted]`。它也**不进确认卡**。拿不到值就不连接，返回 `auth_unavailable`。

## 锁定项（只显示，不能改）

下面这些项在设置页和 `GET /api/tool-policy` 的 `locked` 里只读显示。配置里写了它们会记 warning，并按锁定值处理。

| 键 | 值 | 说明 |
| --- | --- | --- |
| `local_body_check` | strict | 出站前检查本地正文（原文、截段、编码变体、指纹），连接前拦下。本地正文不出本机是底线 |
| `local_bodies_unavailable` | refuse | 拿不到本会话的本地内容登记时不连接 |
| `remote_local_prompt_default` | refuse | `generate_image` 默认拒绝把本地提示词发给第三方，放宽只能在该工具的设置里显式确认（#61） |
| `untrusted_results` | on | 第三方 / MCP 结果一律标为不可信 |
| `schema_reauth` | on | MCP 工具说明或 schema 变了要重新启用 |
| `mcp_confirm_every_call` | on | MCP 远端工具每次调用都确认 |
| `sensitive_confirm_every_call` | on | 联网 / 读本地内容的第三方工具每次调用都确认 |
| `send_local_to_model` | forbidden | 第三方包不能声明这项权限 |
| `content_guard` | on | 出图内容审核（`services/runtime/content_guard.py`） |
| `loopback_self_port` | deny | MCP 永远不能连本程序的端口 |
| `unspecified_unresolved` | deny | 0.0.0.0 / :: 和解析不了的主机永远拒绝 |

前两项和 `remote_local_prompt_default` 是 issue 里写明不能做成可关闭的底线。其余各项是实现时**暂定锁定**的，要不要开放由用户决定（见文末）。

## 放宽要过审查，改了要重新确认

- **安装**：审阅里列出这个包实际用到的放宽项。命令模式要勾 `external_command`，其他放宽项要勾 `policy_relaxed`，不勾就装不上。策略不允许的包（例如命令没放行、http 地址没放行回环）在检查阶段就报 `policy_problems`。
- **启用远端工具**：如果生效的策略里有放宽项，`POST /api/toolpacks/<包>/mcp/enable` 要带上 `"acknowledge": ["<字段>", …]`，否则返回 409 `acknowledge_required`（带 `fields`）。
- **改了策略**：启用记录里绑了生效策略的摘要。策略一变，已启用的工具就变成 `changed`（`changed_reason=policy_changed`），调用返回 `reauth_required`，要重新启用。
- 审计：放宽时记 `security.policy_relaxed`（`stage` 是 `policy` / `install` / `enable`，只记字段名）；写配置时记 `security.config_changed`（只记字段路径）。

## API 和界面

| 接口 | 说明 |
| --- | --- |
| `GET /api/tool-policy` | 当前配置、warnings、锁定项、默认值、各个 `auth` 的状态（是否有值，不含值） |
| `PUT /api/tool-policy` | body 是 `{"policy": {...}}` 或 `{"reset": true}`，返回值同 GET，另带 `changed` |
| `PUT /api/tool-policy/secret` | body 是 `{"pack": "<包名>", "value": "<值>"}`，或 `{"pack": "<包名>", "clear": true}`，写入或清掉钥匙串里的值。返回里不含值 |

- 写接口只接受本机同源请求，跨站一律 403 `config_write_forbidden`。工具执行期间也不能写。
- 设置页的「工具安全策略」一节可以编辑 JSON、恢复默认，查看锁定项，以及写入 / 清除钥匙串里的认证值。保存的配置里有放宽项时会先弹确认。

## 评测

- `evals/run.py` 里 kind 为 `tool_policy` 的五项：`security.tool_policy_default_block`、`security.tool_policy_explicit_relax`、`security.tool_policy_precedence`、`security.tool_policy_no_secret_leak`、`security.tool_policy_policy_change_reenable`。
- 单元检查：`python -m services.tools.check` 里的 `_check_tool_policy`。
- 网页：`apps/web/check.py` 里的 `_tool_policy_case`，以及跨站写入用例。

## 待用户决定

下面这些是实现时自己定下的默认，没有在 issue 里写明，请用户确认或改：

1. 命令模式在 12.D 沙箱外运行，工作目录是包目录。要不要禁用命令模式，或者再加一层隔离？
2. 放行回环后，只拒绝本程序自己的端口。ComfyUI（8188）等其他本机端口要不要也默认拒绝？
3. 环境变量名必须以 `MAS_MCP_` 开头。这个前缀要不要改，或者干脆取消限制？
4. 回环、链路本地、局域网的拦截只作用于 MCP。非 MCP 第三方包仍按 12.D 沙箱规则：回环永远拦，局域网不拦。要不要统一？
5. 非 MCP 包的清单参数名静态检查（`BLOCKED_EXTERNAL`）不开放配置。要不要开放？
6. 上表除了三条底线之外的各个锁定项，要不要开放其中哪几项？
7. `PUT /api/tool-policy` 写入放宽项时，只在界面上确认，服务端不要求 acknowledge，因为放宽在安装和启用时还会再确认一次。要不要在写入时也强制确认？

## 已知缺口

- HTTP 主机判定和实际连接之间仍有 DNS 重绑定窗口。
- stdio server 不支持注入认证 / 环境变量。
- 网页模式没有钥匙串，只能用环境变量。
- `npx` 每次调用都用新的临时 HOME / 缓存，首次调用会很慢。
- 命令模式的出站审计里，端点仍显示成本机 stdio。
- 密钥如果被拆开放在多个内容块里，可能扫不出来。
