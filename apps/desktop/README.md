# 桌面壳

见 [docs/desktop.md](../../docs/desktop.md) 与 [ADR 0010](../../docs/adr/0010-desktop-client.md)。

```
python apps/desktop/check.py   # 无 GUI 冒烟
python apps/desktop/host.py    # 只起 sidecar
cd apps/desktop && npm install && npm run dev   # Tauri 窗口（需 Rust）
```
