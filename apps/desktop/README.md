# 桌面壳

见 [docs/desktop.md](../../docs/desktop.md) 与 [ADR 0010](../../docs/adr/0010-desktop-client.md)。

```
python apps/desktop/check.py   # 无 GUI 冒烟
python apps/desktop/host.py    # 只起 sidecar
python apps/desktop/launcher.py  # 本机 Python + 可选 pywebview 窗口
cd apps/desktop && npm install && npm run dev   # Tauri 窗口（需 Rust）
```

Windows 双击入口（**不内嵌 Python DLL**）：

```
powershell -ExecutionPolicy Bypass -File apps\desktop\build-launcher.ps1
```

会在桌面放「Multimodal Agent Studio」快捷方式，指向本机 `pythonw.exe` + `launcher.py`；`dist\` 下另有 `.cmd` 兜底。不要用旧的 PyInstaller onefile exe。

解释器查找（`find-python.cmd`，所有入口共用，不依赖 PATH 里有 `python`）：
`MAS_PYTHON` → 仓库 `.venv` / `venv` → `py -3` → `D:\ProgramData\miniconda3`、`%USERPROFILE%\miniconda3`、`%USERPROFILE%\anaconda3`、`C:\ProgramData\miniconda3` → PATH 上的 `python`。
快捷方式写入解析出的 `pythonw.exe` 绝对路径；都找不到时请设置 `MAS_PYTHON` 指向 `python.exe`。
