# 工具安全策略（#93）

MCP 出站和几项工具安全检查原来写死在代码里。现在读一份本机策略，没配就是原来最严的行为。ADR 0011 仍是提议。

网页「设置」里改的是同一份文件。可以改全局，也可以按包名或「包名__工具名」改一层。比当前更松的项会先列出名称，确认后才写入。页面上没有密钥输入框。策略变了之后，打不开的工具面板只显示原因，对话框上方有关闭。

文件：数据目录下的 `tool_policy.json`，或环境变量 `MAS_TOOL_POLICY` 指到的路径。三层，后面的盖过前面的：

工具 > 包 > 全局 > 默认值

```json
{"schema": "mas.toolpolicy/1",
 "global": {"allow_lan": false, "stdio_commands": [], "content_guard": "strict"},
 "packs": {"mcp_echo": {}},
 "tools": {"mcp_echo__echo": {}}}
```

不认识的字段、类型不对、或出现 `token` / `secret` / `password` / `value` 这类密钥字段，保存会被拒绝，文件不会被改。读到坏文件时按默认值，不会放宽。

## 字段

| 字段 | 默认 | 放宽时 |
| --- | --- | --- |
| `stdio_commands` | `[]`（只许包内 Python，在沙箱里跑） | 允许这些命令名，例如 `npx`。不能写路径。不走 shell，工作目录是包目录 |
| `allow_loopback` | `false` | HTTP MCP 可以连回环地址 |
| `allow_link_local` | `false` | 可以连链路本地地址 |
| `allow_lan` | `false` | 可以连局域网地址 |
| `auth` | 无 | `{"header": "Authorization", "env": "MAS_MCP_TOKEN"}` 或 `"keychain": "tool.某包.token"`。值不写进文件 |
| `params_block` / `params_allow` | 拦住 `prompt` `text` `body` `content` `bytes` `path` `filename` | 少拦一个默认名字算放宽。多拦不算 |
| `confirm_every_call` | `true` | MCP 调用不再每次弹确认卡 |
| `local_body_check` | `true` | 发给 MCP 的参数不再查本地正文 |
| `untrusted_results` | `true` | 工具结果不再标不可信 |
| `schema_reauth` | `true` | 远端说明或参数变了也继续调用 |
| `content_guard` | `strict` | `off` 跳过内容审核。实现在 `services/runtime/content_guard.py` |

解析不了的主机、`0.0.0.0` 和 `::` 永远拒绝，策略改不了。

## 放宽和重新确认

相对默认值放宽的项，会进安装审阅（`policy:<字段名>`）和 MCP 启用请求的 `acknowledge`。少一项就不装、不启用，并记 `security.policy_relaxed`（只有字段名）。

启用记录绑了当时的策略摘要。策略变了，工具状态变成 `changed`，调用返回 `reauth_required`，要重新启用。已安装的包如果策略摘要对不上，要重新安装。旧的授权记录在策略仍是最严时继续有效。

## 密钥

认证头的值只在发出那一下放进请求头。stdio 只把策略点名的那一个环境变量传给子进程。结果正文里如果带出这个值，会换成 `…`。策略文件和审计里没有这个值。
