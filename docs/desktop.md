# 桌面壳（第 13 阶段 / ADR 0010）

对应 [#72](https://github.com/loootte/multimodal-agent-studio/issues/72)、[#74](https://github.com/loootte/multimodal-agent-studio/issues/74)。选型：**Tauri 2 + Python sidecar**。业务逻辑仍在 `apps/web`；壳只负责窗口和本机服务生命周期。直接跑 Web 服务的行为不变。

## 13.A 已交付

- 启动令牌：壳每次启动生成随机令牌；sidecar 启用后无令牌请求返回 `401 desktop_token_required`。令牌可通过请求头 `X-MAS-Desktop-Token`、Cookie `mas_desktop_token`，或首次打开 `/?desktop_token=…`（写 Cookie 后重定向到 `/`）。
- 只绑 `127.0.0.1`；默认随机空闲端口（不抢占开发用的 8766）。
- 单实例锁：默认 `~/.mas/desktop.lock`（可用 `MAS_DESKTOP_LOCK` 改）。
- 关窗 / 停宿主：先 `POST /api/desktop/stop` 停止运行中的任务，再结束 Python 进程。

钥匙串、默认加密、托盘、打包安装、自动更新见 13.B / 13.C；CSP / 导航白名单加固见 13.D。

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
- 现有 `apps/web/check.py`、`poc/tools/check.py`、`evals/run.py`、`evals/gate.py` 在 Web 模式（无令牌）下行为不变。

## Windows 启动器 exe（无 Rust / 无 MSVC）

若本机缺少 Visual C++ Build Tools，可用 PyInstaller 打出 **pywebview 启动器**（不是 Tauri 壳，但同样拉起 `host.py`、随机端口、启动令牌）：

```
cd apps/desktop
powershell -ExecutionPolicy Bypass -File .\build-launcher.ps1
```

产物：`apps/desktop/dist/MultimodalAgentStudio.exe`。双击即可；需本机 Python 3 与完整仓库源码（exe 会向上找 `apps/desktop/host.py`）。有 `pywebview` 时用 WebView2 窗口，否则回退系统浏览器。

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

正式安装包分发流程、代码签名、自动更新、钥匙串、托盘、移动端。Windows 下可先用上面的 release exe / NSIS 包本地双击。
