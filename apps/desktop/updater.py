"""桌面自动更新（13.C / #76）。

默认关闭（desktop.json auto_update=false / MAS_AUTO_UPDATE 未开）。
开启后：
- 只向配置的发布地址发检查请求；
- 载荷只有 version + platform，走 egress 渠道 `update`；
- 响应必须带签名，用配置的公钥校验；校验失败不下载、不安装。

本模块不负责静默替换正在运行的进程；检查通过后返回清单，由安装器 / 用户确认后再应用。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DESKTOP_DIR = Path(__file__).resolve().parent
ROOT = DESKTOP_DIR.parent.parent
for _p in (str(DESKTOP_DIR), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import desktop_config  # noqa: E402

ALLOWED_PAYLOAD_FIELDS = frozenset({"version", "platform"})


class UpdateError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def public(self) -> dict:
        return {"code": self.code, "message": self.message}


def platform_tag() -> str:
    if sys.platform == "win32":
        return "windows-x86_64"
    if sys.platform == "darwin":
        return "macos-universal"
    return "linux-x86_64"


def build_check_payload(version: str | None = None, platform: str | None = None) -> dict:
    """唯一允许出站的字段。多一个字段都会被 egress 策略拒绝。"""
    return {
        "version": version or desktop_config.app_version(),
        "platform": platform or platform_tag(),
    }


def assert_payload_safe(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise UpdateError("update_bad_payload", "更新检查载荷必须是对象。")
    extra = set(payload) - ALLOWED_PAYLOAD_FIELDS
    if extra:
        raise UpdateError(
            "update_payload_extra_fields",
            "更新检查载荷含有不允许的字段：" + ",".join(sorted(extra)),
        )
    missing = ALLOWED_PAYLOAD_FIELDS - set(payload)
    if missing:
        raise UpdateError(
            "update_payload_missing_fields",
            "更新检查载荷缺少字段：" + ",".join(sorted(missing)),
        )
    for key in ("version", "platform"):
        value = payload[key]
        if not isinstance(value, str) or not value or len(value) > 64:
            raise UpdateError("update_bad_field", f"字段 {key} 必须是不超过 64 字的字符串。")


def _verify_ed25519(pubkey_hex: str, message: bytes, signature_hex: str) -> bool:
    try:
        pub = bytes.fromhex(pubkey_hex)
        sig = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise UpdateError("update_bad_pubkey", "更新公钥或签名不是合法十六进制。") from exc
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        from cryptography.exceptions import InvalidSignature
    except ImportError as exc:
        raise UpdateError("update_crypto_unavailable", "校验更新签名需要 cryptography 包。") from exc
    try:
        key = Ed25519PublicKey.from_public_bytes(pub)
        key.verify(sig, message)
        return True
    except (ValueError, InvalidSignature):
        return False


def verify_manifest(manifest: dict, pubkey_hex: str) -> dict:
    """清单至少含 version / url / sha256 / signature；signature 对 canonical JSON（去掉 signature）做 Ed25519。"""
    if not isinstance(manifest, dict):
        raise UpdateError("update_bad_manifest", "更新清单格式不对。")
    sig = manifest.get("signature")
    if not isinstance(sig, str) or not sig:
        raise UpdateError("update_unsigned", "更新清单没有签名。")
    body = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if not pubkey_hex:
        raise UpdateError("update_no_pubkey", "未配置更新公钥（desktop.json update_pubkey / MAS_UPDATE_PUBKEY）。")
    if not _verify_ed25519(pubkey_hex, canonical, sig):
        raise UpdateError("update_bad_signature", "更新清单签名校验失败。")
    for key in ("version", "url", "sha256"):
        if not isinstance(body.get(key), str) or not body[key]:
            raise UpdateError("update_incomplete_manifest", f"更新清单缺少 {key}。")
    return body


def check_for_update(
    *,
    data_dir: str | None = None,
    force: bool = False,
    transport=None,
) -> dict:
    """检查更新。默认尊重 auto_update=false；force=True 时仍要求已配置 URL（给手动「检查更新」用）。

    transport: 可选 callable(url, body_bytes, headers) -> (status, raw_bytes)，测试可注入，避免真出网。
    """
    cfg = desktop_config.load(data_dir)
    if not force and not cfg.get("auto_update"):
        return {"ok": True, "skipped": True, "reason": "auto_update_off", "egress": None}
    url = (cfg.get("update_url") or "").strip()
    if not url:
        raise UpdateError("update_no_url", "未配置发布地址（desktop.json update_url / MAS_UPDATE_URL）。")
    payload = build_check_payload()
    assert_payload_safe(payload)

    from services.gateway import egress

    ticket = egress.guard("update", url, payload)
    if ticket.hit:
        raise UpdateError(
            ticket.reason_code or "egress_policy_denied",
            "更新检查被出站策略拦下（载荷只能有 version 和 platform）。",
        )
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "User-Agent": "MAS-Updater/1"}
    try:
        if transport is not None:
            status, raw = transport(url, body, headers)
        else:
            req = urllib.request.Request(url, data=body, method="POST", headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                ticket.failed("http_error", http_status=exc.code)
                raise UpdateError("update_http_error", f"更新检查 HTTP {exc.code}") from exc
            except Exception as exc:
                ticket.failed("connect_error")
                raise UpdateError("update_connect_error", f"更新检查连接失败：{type(exc).__name__}") from exc
        ticket.sent(http_status=status)
    except UpdateError:
        raise
    except Exception as exc:
        if not ticket.done:
            ticket.failed("connect_error")
        raise UpdateError("update_failed", str(exc)) from exc

    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise UpdateError("update_bad_response", "更新接口返回的不是 JSON。") from exc
    verified = verify_manifest(manifest, cfg.get("update_pubkey") or "")
    current = desktop_config.app_version()
    newer = verified["version"] != current
    return {
        "ok": True,
        "skipped": False,
        "current": current,
        "newer": newer,
        "manifest": {
            "version": verified["version"],
            "url": verified["url"],
            "sha256": verified["sha256"],
        },
        "egress_id": ticket.egress_id,
    }


def maybe_check(data_dir: str | None = None) -> dict:
    """启动时调用：关着就什么也不做、不建连接。"""
    cfg = desktop_config.load(data_dir)
    if not cfg.get("auto_update"):
        return {"ok": True, "skipped": True, "reason": "auto_update_off"}
    try:
        return check_for_update(data_dir=data_dir, force=False)
    except UpdateError as exc:
        return {"ok": False, **exc.public()}
