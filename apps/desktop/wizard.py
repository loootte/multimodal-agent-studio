"""首次运行向导（13.C / #76）。

步骤：
1. 选择数据目录（默认 desktop_env.default_data_dir()）
2. 确认落盘加密开启（桌面默认 on；用户必须明确确认）
3. 探测 ComfyUI：只探测用户填写的地址，或本机默认 http://127.0.0.1:8188；绝不扫描网段
4. 模型 API key 可选：跳过则标记 local_only，只用本机能力

完成后把 wizard_completed 写入 desktop.json，下次启动不再弹出。
无 GUI 时可用 apply() 直接写入（安装器 / 检查用）；有 tkinter 时 run_gui()。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
for _p in (DESKTOP_DIR, ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import desktop_config  # noqa: E402
import desktop_env  # noqa: E402

DEFAULT_COMFY = "http://127.0.0.1:8188"
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class WizardError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def public(self) -> dict:
        return {"code": self.code, "message": self.message}


def needs_wizard(data_dir: str | None = None) -> bool:
    cfg = desktop_config.load(data_dir)
    return not bool(cfg.get("wizard_completed"))


def _normalize_comfy_url(raw: str) -> str:
    text = (raw or "").strip() or DEFAULT_COMFY
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise WizardError("comfy_bad_scheme", "ComfyUI 地址只支持 http 或 https。")
    host = (parsed.hostname or "").lower()
    if not host:
        raise WizardError("comfy_bad_host", "ComfyUI 地址缺少主机名。")
    # 允许本机回环或用户显式填写的任意主机；禁止空、禁止用通配暗示扫描。
    if host in ("0.0.0.0", "*", "::"):
        raise WizardError("comfy_scan_refused", "不能用通配地址；请填写具体主机，例如 127.0.0.1。")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path if parsed.path not in ("", "/") else ""
    return f"{parsed.scheme}://{host}:{port}{path}".rstrip("/")


def probe_comfy(url: str, *, timeout: float = 2.0) -> dict:
    """只探测调用方给出的那一个地址。不扫网段、不扫端口范围。"""
    target = _normalize_comfy_url(url)
    parsed = urlparse(target)
    host = parsed.hostname or ""
    port = parsed.port or 80
    result = {"url": target, "host": host, "port": port, "reachable": False, "detail": ""}
    # 先 TCP，再 HTTP /system_stats（ComfyUI 常见探活）
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as exc:
        result["detail"] = f"tcp_refused:{type(exc).__name__}"
        return result
    probe_url = target + "/system_stats"
    try:
        req = urllib.request.Request(probe_url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result["reachable"] = True
            result["detail"] = f"http_{resp.status}"
            return result
    except urllib.error.HTTPError as exc:
        # 服务在、路径可能 404，仍算可达
        result["reachable"] = True
        result["detail"] = f"http_{exc.code}"
        return result
    except Exception as exc:
        result["reachable"] = True  # TCP 通了就算探到了服务
        result["detail"] = f"http_error:{type(exc).__name__}"
        return result


def apply(
    *,
    data_dir: str | None = None,
    encryption_confirmed: bool = True,
    comfy_url: str = DEFAULT_COMFY,
    api_key: str | None = None,
    skip_api_key: bool = False,
    auto_update: bool = False,
    update_url: str = "",
    probe: bool = True,
) -> dict:
    """写入向导结果。api_key 非空时进钥匙串，不进 desktop.json。"""
    if not encryption_confirmed:
        raise WizardError("encryption_not_confirmed", "桌面版需要确认开启落盘加密才能完成向导。")
    data = os.path.abspath(os.path.expanduser(data_dir or desktop_env.default_data_dir()))
    Path(data).mkdir(parents=True, exist_ok=True)
    comfy = _normalize_comfy_url(comfy_url)
    probe_result = probe_comfy(comfy) if probe else {"url": comfy, "reachable": None, "detail": "skipped"}
    local_only = bool(skip_api_key) or not (api_key or "").strip()
    if (api_key or "").strip():
        from packages import keychain

        # 只写钥匙串；失败则向导失败，不把 key 落到文件。
        keychain.set(keychain.PROVIDER_ENTRY, api_key.strip())
        local_only = False
    cfg = desktop_config.load(data)
    cfg.update({
        "wizard_completed": True,
        "encryption_confirmed": True,
        "comfy_url": comfy,
        "local_only": local_only,
        "auto_update": bool(auto_update),
        "update_url": (update_url or cfg.get("update_url") or "").strip(),
    })
    path = desktop_config.save(cfg, data)
    # 把数据目录告诉后续启动（本进程 + 可选写进用户环境由安装器做）
    os.environ["MAS_DESKTOP_DATA_DIR"] = data
    # 把 Comfy 地址写进工具配置默认值的旁路：环境变量，供 sidecar 读
    os.environ.setdefault("COMFY_URL", comfy)
    return {
        "ok": True,
        "data_dir": data,
        "config_path": str(path),
        "comfy": probe_result,
        "local_only": local_only,
        "auto_update": bool(auto_update),
    }


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAS 首次运行向导")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--comfy-url", default=DEFAULT_COMFY)
    parser.add_argument("--api-key", default=None, help="可选；不传或 --skip-api-key 则只用本机能力")
    parser.add_argument("--skip-api-key", action="store_true")
    parser.add_argument("--auto-update", action="store_true", help="默认关闭；显式打开才检查更新")
    parser.add_argument("--update-url", default="")
    parser.add_argument("--no-probe", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply(
            data_dir=args.data_dir,
            encryption_confirmed=True,
            comfy_url=args.comfy_url,
            api_key=args.api_key,
            skip_api_key=args.skip_api_key or not args.api_key,
            auto_update=args.auto_update,
            update_url=args.update_url,
            probe=not args.no_probe,
        )
    except WizardError as exc:
        payload = {"ok": False, **exc.public()}
        print(json.dumps(payload, ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("向导完成。")
        print("数据目录：", result["data_dir"])
        print("ComfyUI：", result["comfy"])
        print("仅本机能力：" if result["local_only"] else "已配置模型 key：", result["local_only"])
        print("自动更新：", "开" if result["auto_update"] else "关（默认）")
    return 0


def run_gui(data_dir: str | None = None) -> dict:
    """简易 tkinter 向导。没有 tkinter 时退回 CLI 默认值（跳过 key、确认加密）。"""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox
    except Exception:
        return apply(data_dir=data_dir, encryption_confirmed=True, skip_api_key=True)

    result_holder: dict = {}

    root = tk.Tk()
    root.title("Multimodal Agent Studio — 首次运行")
    root.geometry("560x420")
    default_data = data_dir or desktop_env.default_data_dir()

    tk.Label(root, text="数据目录", anchor="w").pack(fill="x", padx=12, pady=(12, 0))
    data_var = tk.StringVar(value=default_data)
    row = tk.Frame(root)
    row.pack(fill="x", padx=12)
    tk.Entry(row, textvariable=data_var).pack(side="left", fill="x", expand=True)
    tk.Button(row, text="浏览…", command=lambda: data_var.set(
        filedialog.askdirectory(initialdir=data_var.get()) or data_var.get()
    )).pack(side="left", padx=6)

    enc_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        root,
        text="确认开启落盘加密（桌面版默认；主密钥进系统钥匙串）",
        variable=enc_var,
    ).pack(anchor="w", padx=12, pady=8)

    tk.Label(root, text="ComfyUI 地址（只探测这一处，不扫描网段）", anchor="w").pack(fill="x", padx=12)
    comfy_var = tk.StringVar(value=DEFAULT_COMFY)
    tk.Entry(root, textvariable=comfy_var).pack(fill="x", padx=12)
    probe_label = tk.Label(root, text="", anchor="w", fg="#555")
    probe_label.pack(fill="x", padx=12, pady=4)

    def do_probe():
        try:
            info = probe_comfy(comfy_var.get())
            probe_label.config(
                text=("可达：" if info["reachable"] else "未响应：") + info["url"] + " (" + info["detail"] + ")"
            )
        except WizardError as exc:
            probe_label.config(text=exc.message)

    tk.Button(root, text="探测 ComfyUI", command=do_probe).pack(anchor="w", padx=12)

    tk.Label(root, text="模型 API key（可留空，跳过后只能用本机能力）", anchor="w").pack(fill="x", padx=12, pady=(8, 0))
    key_var = tk.StringVar()
    tk.Entry(root, textvariable=key_var, show="*").pack(fill="x", padx=12)

    update_var = tk.BooleanVar(value=False)
    tk.Checkbutton(root, text="开启自动更新（默认关闭；开启后只发版本号和平台）", variable=update_var).pack(
        anchor="w", padx=12, pady=8
    )

    def finish():
        try:
            result_holder.update(apply(
                data_dir=data_var.get(),
                encryption_confirmed=bool(enc_var.get()),
                comfy_url=comfy_var.get(),
                api_key=key_var.get(),
                skip_api_key=not key_var.get().strip(),
                auto_update=bool(update_var.get()),
                probe=True,
            ))
            root.destroy()
        except WizardError as exc:
            messagebox.showerror("向导", exc.message)

    tk.Button(root, text="完成", command=finish).pack(pady=16)
    root.mainloop()
    if not result_holder:
        raise WizardError("wizard_cancelled", "向导已取消。")
    return result_holder


def ensure_done(data_dir: str | None = None, *, gui: bool = True) -> dict | None:
    """启动器调用：未完成则跑向导；已完成返回 None。"""
    if not needs_wizard(data_dir):
        return None
    if gui:
        return run_gui(data_dir)
    return apply(data_dir=data_dir, encryption_confirmed=True, skip_api_key=True)


if __name__ == "__main__":
    raise SystemExit(run_cli())
