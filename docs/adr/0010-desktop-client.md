# ADR 0010：桌面客户端（第 13 阶段）

## 状态

已接受（13.A / #74 定稿）。对应路线图第 13 阶段父 issue [#72](https://github.com/loootte/multimodal-agent-studio/issues/72)，子 issue [#74](https://github.com/loootte/multimodal-agent-studio/issues/74)–[#77](https://github.com/loootte/multimodal-agent-studio/issues/77)。依赖 [ADR 0002](0002-implementation-stack.md)、[ADR 0003](0003-local-data-isolation.md)、[ADR 0009](0009-handle-scope-and-egress-audit.md)。选型已在 13.A 定稿为 **Tauri 2 + Python sidecar**。

## 背景

路线图「明确延后」里有「桌面或移动客户端」，规定提前其中一项要先写架构决定。第 12 阶段已经把本地数据隔离做完整，MAS 的卖点是隐私优先、本地优先，桌面形态最贴合：数据、密钥和 ComfyUI 都在本机，用户不用自己装 Python、开端口、记地址。第 14 阶段的调度和后台任务也需要一个常驻入口。

## 决定

### 替换或推迟哪条承诺

- 第 3 阶段「首版是 Web，不是桌面壳」已经兑现，本决定**不替换**它：Web 仍是基础界面，桌面壳只是装载同一套界面。
- 「明确延后」里的「桌面或移动客户端」拆开：桌面提前到第 13 阶段，**移动端继续延后**。
- 第 6 阶段（登录、租户、限流）不受影响，桌面版仍是单用户本机。

### 形态：壳 + 本机服务 sidecar

- 壳负责窗口、托盘、钥匙串、原生文件对话框和安装更新；业务逻辑全部留在现有 Python 服务里，不在壳里复制一份。
- 壳启动 sidecar：只绑 `127.0.0.1`，端口随机挑空闲的（不抢占用户已有的开发端口）；单实例锁；关窗时先向运行中的任务发停止，再停服务。
- 启动令牌：壳每次启动生成一次性随机令牌，只交给自己的 WebView；sidecar 对无令牌请求返回 401。直接跑 Web 服务时不启用令牌，行为不变。

### 选型（13.A 定稿）

选用 **Tauri 2 + Python sidecar**。对比结论：

| 方案 | 优点 | 缺点 | 结论 |
| --- | --- | --- | --- |
| **Tauri 2 + Python sidecar** | 体积小，用系统 WebView；IPC 可按命令白名单开放；CSP 和导航限制是一等配置 | 需要 Rust 工具链；各平台 WebView 差异 | **选用** |
| Electron | 生态成熟、渲染一致 | 体积大；Node 集成面广，要额外关掉 | 不用 |
| pywebview | 全 Python，最省事 | 托盘、更新、钥匙串要自己拼；安全配置粒度粗 | 不用作产品壳；`apps/desktop/host.py` 仅作无窗口的生命周期冒烟入口 |

业务逻辑全部留在现有 `apps/web` Python 服务；壳只负责窗口、进程生命周期和（后续 13.B/C/D 的）系统集成。开发期可用 `python apps/desktop/host.py` 拉起带令牌的 sidecar 做冒烟，不必每次编 Tauri。

### 隐私默认比 Web 更严

- 密钥（第三方 API key、12.F 主密钥）存系统钥匙串，由 sidecar 读取，不经过 WebView JS。
- 桌面版默认 `MAS_STORAGE_ENCRYPTION=on`；发现明文数据时引导迁移，不静默转换。
- 通知由宿主模板生成，只含工具名和状态。
- WebView 严格 CSP、禁止导航到 sidecar 以外的地址、release 关闭 devtools；外链经宿主确认卡后交给系统浏览器。
- 自动更新默认关闭；开启后作为新出站渠道 `update` 记审计，按 12.C 加字段白名单（只有版本号和平台）。

13.B 实现记录：密钥条目由 sidecar 进程内的 `packages/keychain.py` 读写（Windows 直接用凭据管理器 API，其它平台用 `keyring`）；`js_api` 只有选文件 / 另存审计两个方法，令牌不暴露；通知文本只来自 `apps/web/desktop_notify.py` 的固定模板；默认加密的明文目录由宿主启动前预检拒绝，不做自动迁移。

### 不变

一次 `generate_image` 出一张、张数由 `plan_images` 决定、本地内容不进 LLM、单 Agent 模式不变、`remote_local_prompt` 默认 `refuse`。桌面壳不新增任何能把本地内容发给模型的路径。


13.C 实现记录（[#76](https://github.com/loootte/multimodal-agent-studio/issues/76)）：Windows 安装布局用 `apps/desktop/packaging/windows/`（NSIS + 可选 embeddable Python），**不用**已废弃的 PyInstaller onefile。首次运行向导写 `desktop.json`（无密钥）；ComfyUI 探测只打用户填写或本机默认 `127.0.0.1:8188`，不扫网段。自动更新默认关，开启后走出站渠道 `update`，载荷与审计仅 `version`+`platform`，清单需 Ed25519 验签。卸载默认保留数据目录与钥匙串。未采购代码签名证书；SmartScreen 警告风险仍适用。`--desktop-token` CLI 改仅环境变量留给 13.D / #77。

## 后果

- 新增 `apps/desktop/` 和对应构建流水线；Windows 优先，macOS / Linux 随后。
- 安全清单增加「桌面壳」一节，`security.desktop_*` 进门禁。
- 不买代码签名证书前，安装时会有系统警告；在发布说明与本 ADR 写明风险（13.C 已落地安装脚本与向导，仍不签名）。
- 13.C 评测 `security.desktop_update_egress_no_user_data` 进门禁。
