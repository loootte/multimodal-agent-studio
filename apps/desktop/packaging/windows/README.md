# Windows 打包（13.C / #76）

## 目标

干净的 Windows 机器不装系统 Python 也能用。内嵌运行时 + 应用文件，可选 NSIS 安装包。

**不要**再用 PyInstaller `--onefile`：会把 `python312.dll` 解到 `%TEMP%\_MEI*`，常见 `LoadLibrary` 失败。本目录走「目录布局 + 启动脚本」路线，和现有 `launcher.py` / `find-python.cmd` / `start-mas.cmd` 一致。

## 构建

在仓库根目录（PowerShell）：

```powershell
# 冒烟布局：复用本机 Python（开发机验证向导 / 卸载逻辑）
powershell -ExecutionPolicy Bypass -File apps\desktop\packaging\windows\build.ps1

# 正式内嵌：先下载官方 Windows embeddable 包
# https://www.python.org/ftp/python/3.12.x/python-3.12.x-embed-amd64.zip
powershell -File apps\desktop\packaging\windows\build.ps1 `
  -PythonEmbedZip D:\downloads\python-3.12.10-embed-amd64.zip
```

产物：

| 路径 | 说明 |
| --- | --- |
| `dist/mas-windows/` | 可拷贝的目录布局；双击 `start-mas.cmd` |
| `dist/MultimodalAgentStudio-<ver>-Setup.exe` | 有 `makensis` 时才生成 |

## 安装 / 卸载

- 安装目录默认 `%LOCALAPPDATA%\MultimodalAgentStudio`（当前用户，不需管理员）。
- 卸载**默认保留**数据目录和钥匙串条目；勾选「删除数据目录和钥匙串条目」才删。

## 缺口（本环境）

- 本构建机未必有 `makensis` 或 embed zip：脚本仍产出可跑的冒烟布局，并在日志里标明缺口。
- **未做代码签名**（不买证书）。SmartScreen 会警告；风险记在 ADR 0010。
- macOS / Linux 构建见同级 `../macos`、`../linux` 占位脚本。
