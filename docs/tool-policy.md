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
| `untrusted_results` | `true` | 三层都行 | `false` 是 | `false` = 第三方 / MCP 工具的结果标 `policy_trusted`，不再附「不是指令」提示。仍是 user 角色的数据，不进系统指令；内置工具不受影响 |
| `schema_reauth` | `true` | 三层都行 | `false` 是 | `false` = MCP 工具说明 / 参数变了照样调用，不用重新启用（记 `security.mcp_schema_changed`）。新 schema 照样过参数黑名单和结构检查；工具没了照样 `reauth_required` |
| `mcp_confirm_every_call` | `true` | 三层都行 | `false` 是 | `false` = MCP 远端工具一轮对话里确认一次后复用；第一次照样弹宿主模板确认卡 |
| `sensitive_confirm_every_call` | `true` | 三层都行 | `false` 是 | `false` = 联网的第三方包一轮对话里确认一次后复用。**读本地内容（`read_local_content`）的包不受影响**，仍每次确认 |
| `adult_remote_hosts` | `[]` | 三层都行 | 非空是 | 成人内容路由：可以接收成人分级出图提示词的第三方出图服务主机名（小写，不含端口）。默认空 = 只走本机 ComfyUI。`tools` 层的键是分级表单工具名；提示词本身露骨、没有来源工具的用 `generate_image`。放行后**每次实际发送**都弹确认卡，卡上写目的主机，拒绝就不发 |
| `content_guard` | `strict` | 三层都行 | 非 `strict` 是 | 内容审核级别：`strict` 全部检查；`standard` 不查版权 / IP 角色；`off` 不审核（普通表单工具也能出露骨内容）。只决定审不审，不决定发到哪里；未成年 / 真人的性化内容拦截是锁定底线，任何级别都生效 |

`untrusted_results` 到 `sensitive_confirm_every_call` 四项是 #93 用户决定开放的开关（决定 6），默认 `true` 就是之前的锁定行为，写 `false` 才是放宽。写成非布尔值记 warning、回到默认。

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

下面这些项在设置页和 `GET /api/tool-policy` 的 `locked` 里只读显示。配置里写了它们会记 warning，并按锁定值处理（写它们不算放宽，不用确认）。

| 键 | 值 | 说明 |
| --- | --- | --- |
| `local_body_check` | strict | 出站前检查本地正文（原文、截段、编码变体、指纹），连接前拦下。本地正文不出本机是底线 |
| `local_bodies_unavailable` | refuse | 拿不到本会话的本地内容登记时不连接 |
| `remote_local_prompt_default` | refuse | `generate_image` 默认拒绝把本地提示词发给第三方，放宽只能在该工具的设置里显式确认（#61） |
| `send_local_to_model` | forbidden | 第三方包不能声明这项权限 |
| `content_safety_baseline` | on | 内容安全底线：性化内容（分级工具，或文字本身露骨）里不能有未成年 / 像未成年的角色（含年龄低于 `min_age`、幼态、校服），不能有真人（真人词、`@账号`、输入里的人名）。`content_guard=off` 也照样拦 |
| `loopback_self_port` | deny | MCP 永远不能连本程序的端口 |
| `unspecified_unresolved` | deny | 0.0.0.0 / :: 和解析不了的主机永远拒绝 |

### 本地出站、成人内容路由、内容审核：各管一件事

之前 `content_guard` 一项同时管「审不审」和「成人内容只走本机 ComfyUI」，所以只能锁定。用户决定把它拆开，控制权交给人：

| 管什么 | 由谁管 | 能不能改 |
| --- | --- | --- |
| 本地内容（本地正文、本地提示词）要不要离开本机，发给第三方模型或服务（含第三方出图） | `send_local_to_model`（锁定）+ 本地正文那一套：`local_body_check`、`generate_image` 的 `remote_local_prompt`（默认拒绝，显式 `confirm` 后每轮弹卡） | 锁定；`remote_local_prompt` 只能在 `generate_image` 设置里显式确认放宽 |
| 成人分级内容可以发给哪些第三方出图服务 | `adult_remote_hosts`（路由配置） | 可配，默认空；放宽要 409 确认、安装审阅、审计 |
| 做不做内容审核、审到什么程度 | `content_guard`（`strict` / `standard` / `off`） | 可配，默认 `strict`；放宽要 409 确认、安装审阅、审计 |
| 未成年 / 真人的性化内容 | `content_safety_baseline` | 锁定，任何配置都拦 |

**为什么路由做成独立的路由配置，而不是挂在 `send_local_to_model` 下。** 成人内容路由按内容分级判断，和内容是不是本地的无关：对话里打的成人提示词不是本地正文，本地正文也不一定是成人内容。`send_local_to_model` 是锁定的本地出站总闸，要么把路由跟着锁死（不符合「控制权交到人手里」），要么为了放开路由连本地出站一起放开（撞底线），两条都不行。所以路由单独成一个字段，挂在已有的工具安全策略里：同一套 global / packs / tools 层级、同一套 409 确认、安装审阅和 `security.policy_relaxed` 审计。本地提示词要发第三方，仍然先过本地出站那一套（`remote_local_prompt` 默认拒绝，`confirm` 后每轮一张本地出站卡），再过路由那一张写着主机的卡。两张卡各自独立，少一张都不发。

**关掉审核不会让任何内容出本机，也不会把成人内容发给第三方。** 审核关掉后，普通工具产生的露骨提示词照样在出图前被识别为成人分级（`looks_adult`），按 `adult_remote_hosts`（默认空）只走本机；本地提示词照样受 `remote_local_prompt` 管。放宽路由也不改变审核级别。

**默认等于旧行为。** `adult_remote_hosts=[]`、`content_guard=strict` 和拆分前的锁定行为完全一样：本地正文不出本机，成人内容不发第三方，未成年 / 真人 / 版权角色都拦。

`send_local_to_model` 仍不开放：这项权限的含义就是第三方包可以把本地内容放进结果交给模型，而模型请求会发给模型供应商。开放它等于让第三方包绕过「本地正文只以句柄出现」。

## 放宽要过审查，改了要重新确认

- **写策略**（决定 7）：`PUT /api/tool-policy` 里有新放宽的字段（和原来比新出现或换了值），body 要带 `"acknowledge": ["<层>.<字段>", …]` 逐项列出，例如 `["global.untrusted_results", "packs.http_loopback"]`。缺一项就返回 409 `acknowledge_required`（带 `fields` 和每项的说明 `texts`），策略不保存。收紧、原样保留已确认的放宽、恢复默认都不用确认。设置页收到 409 会把字段和说明弹给人看，同意了才带上 `acknowledge` 重发。
- **安装**：审阅里列出这个包实际用到的放宽项。命令模式要勾 `external_command`，其他放宽项要勾 `policy_relaxed`，不勾就装不上。普通第三方包（非 MCP）也一样：生效策略对它放宽了 `untrusted_results`，或者（联网、不读本地内容的包）`sensitive_confirm_every_call`，或者（分级包）`adult_remote_hosts`，或者（表单工具包）`content_guard`，安装时也要勾 `policy_relaxed`。策略不允许的包（例如命令没放行、http 地址没放行回环）在检查阶段就报 `policy_problems`。
- **启用远端工具**：如果生效的策略里有放宽项，`POST /api/toolpacks/<包>/mcp/enable` 要带上 `"acknowledge": ["<字段>", …]`，否则返回 409 `acknowledge_required`（带 `fields`）。
- **改了策略**：启用记录里绑了生效策略的摘要（包括上面四个开关）。策略一变，已启用的 MCP 工具就变成 `changed`（`changed_reason=policy_changed`），调用返回 `reauth_required`，要重新启用。普通第三方包没有启用这一步，已经装好的包在策略写入（带 acknowledge）后立即按新策略执行。
- **实际发第三方**：分级内容按 `adult_remote_hosts` 发出前，每次都弹确认卡（写明目的主机，不记住），记 `security.adult_remote_confirm`（工具名、目的主机、是否同意，不含提示词）。
- 审计：放宽时记 `security.policy_relaxed`（`stage` 是 `policy` / `install` / `enable`，只记字段名）；写配置时记 `security.config_changed`（只记字段路径）。

## API 和界面

| 接口 | 说明 |
| --- | --- |
| `GET /api/tool-policy` | 当前配置、warnings、锁定项、默认值、各个 `auth` 的状态（是否有值，不含值） |
| `PUT /api/tool-policy` | body 是 `{"policy": {...}, "acknowledge": [...]}` 或 `{"reset": true}`，返回值同 GET，另带 `changed`。新放宽的字段没逐项确认时返回 409 `acknowledge_required` |
| `PUT /api/tool-policy/secret` | body 是 `{"pack": "<包名>", "value": "<值>"}`，或 `{"pack": "<包名>", "clear": true}`，写入或清掉钥匙串里的值。返回里不含值 |

- 写接口只接受本机同源请求，跨站一律 403 `config_write_forbidden`。工具执行期间也不能写。
- 设置页的「工具安全策略」一节最上面是「可以配置的项（全局）」表单：内容审核级别（strict / standard / off）、成人内容第三方出图主机、四个开关（勾选 = 默认最严）、stdio 命令白名单、三类地址（回环 / 链路本地 / 局域网）、参数名黑名单追加 / 放行，以及按包的认证引用（包名、请求头、方案、来源、环境变量名）。表单只改 `global` 一层（认证只改 `packs.<包名>.auth`），其他层原样保留；要按包 / 按工具配置，展开「高级：直接编辑 JSON」。下面还有恢复默认、写入 / 清除钥匙串里的认证值。最下面折叠的是「不可配置的底线（只读）」，这些是故意锁定的，不会出现在表单里。
- 是否放宽由服务端判断：保存时先不带 `acknowledge` 发；收到 409 `acknowledge_required` 就用页内弹窗「确认放宽工具安全策略」列出每个字段和说明，点「确认放宽并保存」才带 `acknowledge` 重发，点「取消」不保存。读回来的 `auth.available`（密钥在不在）是只读的，写回时页面会去掉。

## 如何测试 409

先确认打开的是当前代码：用浏览器开 `python apps/web/server.py` 起的地址（默认 http://127.0.0.1:8766）。`dist/mas-windows` 里打包好的桌面版是打包时的旧代码，要重新打包才会有这些控件。改了代码要重启服务，再强制刷新页面（Ctrl+F5）。

1. 点右上角「设置」，滚到「工具安全策略」，看到「可以配置的项（全局）」表单，提示是「没改的项就是最严格的默认值。」。
2. 把「内容审核级别」改成 `off`，点「保存策略」。
3. 这时服务端回 409：页面弹出「确认放宽工具安全策略」，列出 `global.content_guard` 和说明。浏览器开发者工具的 Network 里能看到第一次 `PUT /api/tool-policy` 是 409，返回 `code: acknowledge_required`。
4. 点「取消」：提示「没有保存」，策略没变（重新打开设置，内容审核仍是 strict）。
5. 再保存一次，这次点「确认放宽并保存」：第二个 `PUT` 带 `"acknowledge": ["global.content_guard"]`，返回 200，提示「已保存」。服务端审计里多一条 `security.policy_relaxed`，只有字段名 `content_guard`。
6. 原样再点一次「保存策略」不会再弹（已确认过的放宽不变）；再改别的放宽项（例如勾上「允许连局域网地址」）会只列出新的那一项。
7. 点「恢复默认」回到最严格的默认值，不用确认。

命令行也能测：`curl -X PUT -H "Content-Type: application/json" -d "{\"policy\":{\"global\":{\"content_guard\":\"off\"}}}" http://127.0.0.1:8766/api/tool-policy` 返回 409；body 加上 `"acknowledge":["global.content_guard"]` 返回 200；`-d "{\"reset\":true}"` 恢复默认。

## 评测

- `evals/run.py` 里 kind 为 `tool_policy` 的七项：`security.tool_policy_default_block`、`security.tool_policy_explicit_relax`、`security.tool_policy_precedence`、`security.tool_policy_no_secret_leak`、`security.tool_policy_policy_change_reenable`、`security.tool_policy_unlocked_switches`（四个开关默认最严、放宽后行为、两项底线仍锁定）、`security.tool_policy_put_ack`（写策略的 409 确认）。
- `image.route_moderation_split`（拆分）：默认等于旧行为（分级内容不发第三方、审核 strict）；放宽路由 / 审核 409 → 确认 → 审计只有字段名；放宽路由后发到放行主机每张图一张写主机的卡，拒绝不发，名单外主机照样拒；放宽路由不改审核；放宽路由后本地提示词仍被 `remote_local_prompt` 拒；关审核后露骨内容只走本机、本地提示词不出本机；`strict` / `standard` / `off` 下未成年 / 真人底线都拦，`standard` / `off` 不查版权角色。
- 单元检查：`python -m services.tools.check` 里的 `_check_tool_policy`、`_check_policy_switches` 和 `_check_route_moderation_split`（各级别 × 分级 / 普通 × 底线矩阵）。
- 网页：`apps/web/check.py` 里的 `_tool_policy_case`、`_tool_policy_ui_case`（设置页有全部可配置控件、在高级 JSON 和只读底线上面、有确认弹窗；按表单写法放宽 `content_guard=off` 先 409、策略不变，带 acknowledge 后 200、审计只有字段名，再恢复默认），以及跨站写入用例。

## 用户决定（#93）

1. 命令模式在 12.D 沙箱外运行、工作目录是包目录：**保持**，不加隔离。
2. 放行回环后只拒绝本程序自己的端口：**保持**，ComfyUI（8188）等不额外拦。
3. 环境变量名前缀 `MAS_MCP_`：**保留**。
4. 回环 / 链路本地 / 局域网规则只作用于 MCP，和非 MCP 包的 12.D 规则统一：**暂不做**，记进 ROADMAP。
5. 非 MCP 包的清单参数名静态检查（`BLOCKED_EXTERNAL`）可配置：**暂不做**，记进 ROADMAP。
6. 之前暂定锁定的项：**开放配置**。`untrusted_results`、`schema_reauth`、`mcp_confirm_every_call`、`sensitive_confirm_every_call` 已开放，默认仍最严，放宽走同一套审查和审计；`send_local_to_model` 开放后会让本地内容出本机，**没有开放**。`content_guard` 原来混着管审核和成人内容路由，按后续决定**拆开**：路由归 `adult_remote_hosts`，审核级别 `content_guard` 开放，未成年 / 真人底线锁定（见上文）。
7. 写入放宽项时服务端强制确认：**已做**，缺 `acknowledge` 返回 409。
8. `content_guard` 不应该管内容发不发第三方，控制权要交到人手里：**已拆分**。`send_local_to_model` 只管本地内容出站（锁定）；`content_guard` 只管审不审（可配，默认最严）；成人内容路由是独立配置 `adult_remote_hosts`（可配，默认只走本机，实际发送每次确认）；未成年 / 真人底线不可关。

## 已知缺口

- HTTP 主机判定和实际连接之间仍有 DNS 重绑定窗口。
- stdio server 不支持注入认证 / 环境变量。
- 网页模式没有钥匙串，只能用环境变量。
- `npx` 每次调用都用新的临时 HOME / 缓存，首次调用会很慢。
- 命令模式的出站审计里，端点仍显示成本机 stdio。
- 密钥如果被拆开放在多个内容块里，可能扫不出来。
- `untrusted_results=false` 只去掉不可信标记和提示；结果仍是 user 角色的数据，宿主不会因此执行结果里的指令，但模型更容易被结果里的文字影响。
- 普通第三方包放宽后不需要重新启用（它们没有启用记录），只靠写策略时的确认和安装审阅。
- 内容审核只作用于表单工具（`entry.kind=module`）。对话里直接调 `generate_image` 写露骨提示词，一直没有审核和底线检查，只按 `looks_adult` 做路由（默认只走本机）；已记进 ROADMAP。
- `adult_remote_hosts` 按主机名匹配，不区分端口和路径；成人内容识别（`looks_adult`）是关键词规则，可能漏判。
- `sensitive_confirm_every_call` 只放宽「一轮内复用」，不能关掉第一次确认；`confirm=always` 的策略收紧优先于它。

### 保存反馈与浏览器端到端检查

- 设置页底部的主「保存」会一起保存模型、上下文压缩和工具安全策略：策略表单或高级 JSON 有改动才发 `PUT /api/tool-policy`。
  放宽时先弹页内确认（409）；取消或失败时设置页不关，红字写明「模型和上下文已保存；工具安全策略没有保存」和原因；
  成功时关闭设置页并在页面底部提示改了哪些字段；没改策略会提示「工具安全策略没有改动」。区块内的「保存策略」按钮照旧单独保存。
- 工具页每个工具的「保存」：保存中会禁用按钮，放宽隐私的设置（如 generate_image 的 `remote_local_prompt=confirm`）走同一个页内确认弹窗，
  结果提示「已保存 / 没有变化 / 没能保存：原因」。工具的安全策略不在工具页，在「设置 → 工具安全策略」，单个工具用高级 JSON 的 `tools.<工具名>`。
- 浏览器端到端：`apps/web/e2e/settings_policy.cjs`（需要 `npm i puppeteer-core` 和 Chrome；只对临时服务跑，它会改并恢复策略和工具配置）：
  `MAS_URL=http://127.0.0.1:18799 CHROME=<chrome 路径> node apps/web/e2e/settings_policy.cjs`。
