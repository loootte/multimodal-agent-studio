# 桌面壳（第 13 阶段 / ADR 0010）

对应 [#72](https://github.com/loootte/multimodal-agent-studio/issues/72)、[#74](https://github.com/loootte/multimodal-agent-studio/issues/74)。选型：**Tauri 2 + Python sidecar**。业务逻辑仍在 `apps/web`；壳只负责窗口和本机服务生命周期。直接跑 Web 服务的行为不变。

## 13.A 已交付

- 启动令牌：壳每次启动生成随机令牌；sidecar 启用后无令牌请求返回 `401 desktop_token_required`。令牌可通过请求头 `X-MAS-Desktop-Token`、Cookie `mas_desktop_token`，或首次打开 `/?desktop_token=…`（写 Cookie 后重定向到 `/`）。
- 只绑 `127.0.0.1`；默认随机空闲端口（不抢占开发用的 8766）。
- 单实例锁：默认 `~/.mas/desktop.lock`（可用 `MAS_DESKTOP_LOCK` 改）。
- 关窗 / 停宿主：先 `POST /api/desktop/stop` 停止运行中的任务，再结束 Python 进程。

打包安装、自动更新见 13.C；CSP / 导航白名单加固见 13.D。

## 13.B 系统集成（[#75](https://github.com/loootte/multimodal-agent-studio/issues/75)）

所在层：全部在**宿主侧**（`apps/desktop/host.py`、`launcher.py`、`integration.py`）和 **sidecar 进程内**（`packages/keychain.py`、`apps/web/store.py`、`apps/web/tool_config.py`、`packages/artifacts/crypto.py`、`apps/web/desktop_notify.py`）。模型侧、编排、工具协议都没改；本地内容仍只以句柄出现。

### 密钥进系统钥匙串

- 后端：Windows 凭据管理器（ctypes 调 `CredWriteW/CredReadW`，类型 Generic，目标名 `multimodal-agent-studio/<条目>`）；macOS 钥匙串 / Linux Secret Service 通过 `keyring` 包（装了才用）。
- 条目：`storage.master_key`（12.F 主密钥）、`provider.api_key`（对话模型 key）、`tool.<工具>.<字段>`（工具包里 `secret: true` 的字段，例如 `tool.generate_image.remote_api_key`）。
- 优先级：环境变量 > 系统钥匙串 > 已保存文件（`provider.json` / `tools.json` / `master.key`）。
- 桌面宿主默认 `MAS_KEYCHAIN=on`。在设置里保存 key 时**只写钥匙串**（写完读回校验），文件里不留原值；写失败直接报错、不落盘。
- 只有 sidecar 进程读钥匙串。页面接口只回 `configured`、`key_source`、末四位；`js_api` 没有任何读密钥的方法（`security.desktop_keychain_not_in_webview`）。
- 从钥匙串删掉出图 key → 第三方出图返回 `remote_missing_key`，消息里没有 key。
- 钥匙串不可用（例如 Linux 没装 keyring）时退回文件，并在就绪信息 `notes` 里说明；`MAS_KEYCHAIN=off` 关掉。
- 迁移 / 回滚已有的文件密钥：

```
python apps/desktop/keychain_tool.py status
python apps/desktop/keychain_tool.py migrate              # master.key / provider.json / tools.json 里的密钥复制进钥匙串（先不删文件）
python apps/desktop/keychain_tool.py migrate --purge-files  # 确认能用后再删文件里的原值
python apps/desktop/keychain_tool.py rollback --purge-keychain  # 写回文件并删钥匙串条目
```

输出只有条目名和末四位，不打印原值。

### 默认加密

- 桌面宿主默认 `MAS_STORAGE_ENCRYPTION=on`，数据目录默认 `%LOCALAPPDATA%\MultimodalAgentStudio\data`（macOS `~/Library/Application Support/MultimodalAgentStudio/data`，Linux `$XDG_DATA_HOME/multimodal-agent-studio/data`；`MAS_DESKTOP_DATA_DIR` 可改）。全新安装首次启动生成主密钥并放进钥匙串，盘上只有密文。
- 数据目录里已经有明文会话 / 文件时，**宿主在拉起服务前拒绝**（`storage_migration_required`），输出 `MAS_ERROR {...}` 并退出 2；启动器弹窗给出命令，不悄悄转换：

```
python apps/web/storage_tool.py --keychain --data-dir <目录> encrypt
python apps/web/storage_tool.py --keychain --data-dir <目录> export-key --out <U 盘上的备份文件>
```

- 显式 `MAS_STORAGE_ENCRYPTION=off` 仍可明文运行（就绪信息 `notes` 会提示）。

### 原生文件对话框与拖放

- 启动器给 pywebview 的 `js_api` 只有 `capabilities`、`pick_local_file(run_id)`、`save_egress_export(session_id)`；令牌、地址都是私有属性。
- 「用系统对话框选择…」：宿主 Python 读字节，走现有 `POST /api/runs/<id>/content`（和网页 `<input type=file>` 同一接口、同样 256 KB 上限），服务端存进本地工件库、只给模型句柄。返回给页面的只有 `{ok, bytes}` 或错误码，不含路径和文件名；上传里也不带文件名。
- 拖放：页面全局拦下 drop（WebView 不再跳去打开 `file://`）；有等待中的「本机文件」请求就交给它，否则只提示，不读文件。
- 出站审计导出改用系统「另存为」对话框。

### 托盘与通知

- 有 `pystray` + `Pillow` 时托盘常驻：关窗口只是隐藏，托盘菜单「显示窗口 / 通知（开关）/ 退出」。`MAS_DESKTOP_TRAY=0` 关托盘（关窗口即退出）。
- 没有托盘时用系统通知（Windows PowerShell toast、macOS `osascript`、Linux `notify-send`），文本经环境变量传给子进程。
- 通知由 sidecar 的宿主模板生成（`apps/web/desktop_notify.py`）：待确认 / 待提供本机内容 / 已完成（张数）/ 未完成；只有工具名（白名单内的，否则写「工具」）和状态，不含提示词、回答正文、文件名、错误详情。用户点「停止」的不通知。`GET /api/desktop/notifications?after=N` 只在桌面模式、带令牌可读。`MAS_DESKTOP_NOTIFY=0` 关掉。

### 环境变量

| 变量 | 默认（桌面宿主） | 说明 |
| --- | --- | --- |
| `MAS_KEYCHAIN` | `on` | `on` / `off`；`memory`、`file:<路径>` 只给测试用 |
| `MAS_KEYCHAIN_NAMESPACE` | `multimodal-agent-studio` | 冒烟时换个名字，避免碰真实条目 |
| `MAS_STORAGE_ENCRYPTION` | `on` | 同 12.F |
| `MAS_DESKTOP_DATA_DIR` | 见上 | 数据目录 |
| `MAS_DESKTOP_TRAY` | 开 | `0` 关托盘 |
| `MAS_DESKTOP_NOTIFY` | 开 | `0` 关通知 |

直接跑 `apps/web/server.py`（Web 模式）不受影响：不读钥匙串（除非显式设 `MAS_KEYCHAIN`）、默认仍明文、没有通知接口数据。

## 无窗口冒烟（不需要 Rust）

在仓库根目录：

```
python apps/desktop/check.py
```

覆盖：Web 模式无令牌、令牌 401/200、sidecar 生命周期、端口占用与单实例锁、`--ready-file`。

只起带令牌的服务（浏览器自己开 URL）：

```
python apps/desktop/host.py --print-url
```

进程会打印 `MAS_READY {…}` 和带令牌的 URL。Ctrl+C 退出。

## Windows 上从 Tauri 壳启动

需要：

- Python 3（能在仓库根跑 `python apps/web/server.py`）
- [Rust](https://rustup.rs/)（stable）
- Node.js + npm
- WebView2（Win10/11 通常已带；没有就装 Evergreen Runtime）
- 本机 ComfyUI（若要测出图；默认 `http://127.0.0.1:8188`）

```
cd apps/desktop
npm install
npm run dev
```

`npm run dev` 会编 Tauri 并拉起 `apps/desktop/host.py` → `apps/web/server.py`，窗口打开带令牌的本机页。关窗即停。

可选环境变量：

| 变量 | 作用 |
| --- | --- |
| `MAS_PYTHON` | 指定 Python 可执行文件（默认 `python` / `python3`） |
| `MAS_DESKTOP_LOCK` | 单实例锁路径 |
| `CHAT_DATA_DIR` | 数据目录（传给 Web 服务） |

不要用桌面壳去抢已经在跑的 `8766` 开发服务：壳默认另挑端口。

## 服务端开关

```
python apps/web/server.py --port 0 --desktop-token <secret> --ready-file ready.json
```

或环境变量 `MAS_DESKTOP_TOKEN`。不设令牌时与原来的 Web 模式完全一致。

## 评测

- `security.desktop_token_required`：无令牌访问本地资源 / 设置类接口被拒。
- `security.desktop_notify_no_body`：通知只有宿主模板（工具名 + 状态 + 张数），提示词 / 正文 / 文件名 / 注入文本都不出现。
- `security.desktop_keychain_not_in_webview`：key 只在钥匙串，不落数据目录、页面接口拿不到；`js_api` 只有白名单方法；删掉钥匙串条目后 `remote_missing_key` 且消息不含 key。
- `security.desktop_default_encrypted`：桌面默认加密，全新数据目录是密文、主密钥在钥匙串；已有明文时预检拒绝并给迁移命令。
- `security.desktop_file_handle_only`：原生对话框选文件只回 `{ok, bytes}`，提交给运行的只有字节和媒体类型。
- 现有 `apps/web/check.py`、`python -m services.tools.check`、`evals/run.py`、`evals/gate.py` 在 Web 模式（无令牌）下行为不变。

## Windows 双击入口（本机 Python，无内嵌 DLL）

**不要**再用 PyInstaller `--onefile` 打 `MultimodalAgentStudio.exe`：会把 `python312.dll` 解到 `%TEMP%\_MEI*`，本机上已出现 `LoadLibrary: 内存资源不足` 一类失败。

推荐做法：快捷方式 / `.cmd` 直接调用本机 `pythonw` 跑 `apps/desktop/launcher.py`（同样拉起 `host.py`、随机端口、启动令牌；有 `pywebview` 则 WebView2 窗口，否则系统浏览器）：

```
cd apps/desktop
powershell -ExecutionPolicy Bypass -File .\build-launcher.ps1
```

产物：

- 桌面快捷方式「Multimodal Agent Studio」→ `pythonw.exe` + `apps\desktop\launcher.py`（工作目录为仓库根）
- `apps/desktop/dist/start-mas.cmd` 与 `apps/desktop/dist/启动 Multimodal Agent Studio.cmd`（兜底）
- 仓库内也可双击 / 运行 `apps/desktop/start-mas.cmd`

仍需本机 Python 3 与完整仓库源码。旧的 onefile exe 会被脚本改名为 `MultimodalAgentStudio.exe.BROKEN-onefile.bak`。

## Windows 打 exe（完整 Tauri 壳）

在仓库根、分支含本桌面壳的前提下，于 **Windows**（需 Rust MSVC、Node、WebView2、本机 Python）：

```
cd apps/desktop
npm install
npm run build
```

产物：

- 免安装可执行文件：`apps/desktop/src-tauri/target/release/mas-desktop.exe`
- 可选 NSIS 安装包：`apps/desktop/src-tauri/target/release/bundle/nsis/*.exe`
- 便捷副本：构建脚本会复制到 `apps/desktop/dist/MultimodalAgentStudio.exe`

双击 `mas-desktop.exe` / `dist` 下副本即可；exe 会向上查找含 `apps/desktop/host.py` 的仓库根并拉起本机 Python sidecar（随机端口，不抢 8766）。仍需要：

- 本机已装 Python 3，且能在仓库根运行 `python apps/web/server.py`
- 出图需本机 ComfyUI（默认 `http://127.0.0.1:8188`）
- WebView2 Runtime（Win10/11 通常自带）

也可用环境变量 `MAS_REPO_ROOT` 显式指定仓库根；`MAS_PYTHON` 指定解释器。

打包安装、代码签名、自动更新仍属 13.C；本说明只覆盖「能双击的壳 exe」。

## 不做（本阶段）

正式安装包分发流程、代码签名、自动更新、移动端、云同步、密钥托管、多用户。Windows 下可先用上面的 release exe / NSIS 包本地双击。
Tauri 壳（Rust）这一版还没接 13.B 的原生对话框 / 托盘 / 通知轮询（钥匙串、默认加密在 sidecar 里，Tauri 壳同样生效）；pywebview 启动器已接。
