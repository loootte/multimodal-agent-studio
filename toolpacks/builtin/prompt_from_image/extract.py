"""从图片字节里尽力抽出正向提示词（v1）。

支持：
- PNG tEXt / iTXt / zTXt：A1111 `parameters`（`Negative prompt:` / `Steps:` 之前为正向）；ComfyUI API 格式 `prompt`（沿 KSampler 等节点的 `positive` 边找到 CLIPTextEncode.text）
- JPEG / WebP EXIF：UserComment / ImageDescription（尽力；失败当无提示词）

不做：NovelAI 隐写、LSB、视频、PSD、ComfyUI UI 版 workflow（`workflow` 键）深解；不抽负向。
返回 {"positive": str|None, "source": str|None, "found": bool}；正文只给宿主登记句柄，不回模型。
"""

from __future__ import annotations

import json
import struct
import zlib

# PNG
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SOI = b"\xff\xd8"
_WEBP_RIFF = b"RIFF"
_WEBP_WEBP = b"WEBP"

# EXIF tag numbers
_TAG_IMAGE_DESCRIPTION = 0x010E
_TAG_USER_COMMENT = 0x9286


def extract(data: bytes, media_type: str = "") -> dict:
    """从图片字节抽出正向提示词。data 不能为空。"""
    if not isinstance(data, bytes) or not data:
        return _miss()
    kind = _kind(data, media_type)
    if kind == "png":
        return _from_png(data)
    if kind in ("jpeg", "webp"):
        return _from_exif_container(data, kind)
    return _miss()


def _miss() -> dict:
    return {"positive": None, "source": None, "found": False}


def _hit(positive: str, source: str) -> dict:
    text = positive.strip()
    if not text:
        return _miss()
    return {"positive": text, "source": source, "found": True}


def _kind(data: bytes, media_type: str) -> str:
    mt = (media_type or "").lower().split(";", 1)[0].strip()
    if data.startswith(_PNG_SIG) or mt == "image/png":
        return "png"
    if data.startswith(_JPEG_SOI) or mt in ("image/jpeg", "image/jpg"):
        return "jpeg"
    if (
        len(data) >= 12
        and data.startswith(_WEBP_RIFF)
        and data[8:12] == _WEBP_WEBP
    ) or mt == "image/webp":
        return "webp"
    return ""


# ---------- PNG ----------

def _from_png(data: bytes) -> dict:
    texts = _png_text_chunks(data)
    if not texts:
        return _miss()
    # A1111 / Forge：parameters
    for key in ("parameters", "Parameters"):
        if key in texts:
            positive = _a1111_positive(texts[key])
            if positive:
                return _hit(positive, "a1111_parameters")
    # ComfyUI API prompt（沿采样器 positive 边；不做 UI 版 workflow 深解）
    for key in ("prompt", "Prompt"):
        raw = texts.get(key)
        if raw is None:
            continue
        positive = _comfy_api_positive(raw)
        if positive:
            return _hit(positive, "comfyui_prompt")
        # 纯文本 prompt
        if raw.strip() and not raw.strip().startswith(("{", "[")):
            return _hit(raw, "png_prompt")
    # v1：不把 Description/Comment 等普通图注当提示词（避免误抽）。
    # 有 workflow 但抽不出正向：算没找到（v1 不深解）。
    return _miss()


def _png_text_chunks(data: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    if not data.startswith(_PNG_SIG):
        return out
    pos = 8
    length = len(data)
    while pos + 8 <= length:
        size = struct.unpack_from(">I", data, pos)[0]
        tag = data[pos + 4: pos + 8]
        start = pos + 8
        end = start + size
        if size < 0 or end + 4 > length:
            break
        payload = data[start:end]
        pos = end + 4  # skip CRC
        if tag == b"IEND":
            break
        if tag == b"tEXt":
            key, value = _decode_text(payload)
            if key and key not in out:
                out[key] = value
        elif tag == b"zTXt":
            key, value = _decode_ztxt(payload)
            if key and key not in out:
                out[key] = value
        elif tag == b"iTXt":
            key, value = _decode_itxt(payload)
            if key and key not in out:
                out[key] = value
    return out


def _decode_text(payload: bytes) -> tuple[str, str]:
    nul = payload.find(b"\x00")
    if nul <= 0:
        return "", ""
    key = payload[:nul].decode("latin-1", errors="replace")
    value = payload[nul + 1:].decode("latin-1", errors="replace")
    return key, value


def _decode_ztxt(payload: bytes) -> tuple[str, str]:
    nul = payload.find(b"\x00")
    if nul <= 0 or nul + 2 > len(payload):
        return "", ""
    key = payload[:nul].decode("latin-1", errors="replace")
    method = payload[nul + 1]
    raw = payload[nul + 2:]
    if method != 0:
        return key, ""
    try:
        value = zlib.decompress(raw).decode("latin-1", errors="replace")
    except zlib.error:
        return key, ""
    return key, value


def _decode_itxt(payload: bytes) -> tuple[str, str]:
    # keyword\0 compression_flag compression_method language\0 translated\0 text
    nul = payload.find(b"\x00")
    if nul <= 0 or nul + 2 > len(payload):
        return "", ""
    key = payload[:nul].decode("latin-1", errors="replace")
    flag = payload[nul + 1]
    method = payload[nul + 2]
    rest = payload[nul + 3:]
    nul2 = rest.find(b"\x00")
    if nul2 < 0:
        return key, ""
    rest = rest[nul2 + 1:]
    nul3 = rest.find(b"\x00")
    if nul3 < 0:
        return key, ""
    text = rest[nul3 + 1:]
    if flag == 1:
        if method != 0:
            return key, ""
        try:
            text = zlib.decompress(text)
        except zlib.error:
            return key, ""
    try:
        value = text.decode("utf-8")
    except UnicodeDecodeError:
        value = text.decode("latin-1", errors="replace")
    return key, value


def _a1111_positive(raw: str) -> str | None:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return None
    lower = text
    # Positive 是 Negative prompt: 或 Steps: 之前的部分
    cut = len(text)
    for marker in ("\nNegative prompt:", "\nNegative Prompt:", "\nSteps:"):
        idx = lower.find(marker)
        if idx >= 0:
            cut = min(cut, idx)
    positive = text[:cut].strip()
    return positive or None


# ComfyUI API prompt 里常见采样器；positive / negative 输入是 [node_id, slot]。
_SAMPLER_MARKERS = (
    "KSampler",
    "SamplerCustom",
    "SamplerCustomAdvanced",
    "Sampler",
)
# 顺着 conditioning 边再往回找 CLIP 时允许经过的包装节点名片段
_COND_PASSTHRU = (
    "Conditioning",
    "CLIPTextEncode",
)
# 看起来像「只有负向」的短句（有另一条候选时绝不选这种）
_NEG_ONLY_HINTS = (
    "bad hands",
    "bad anatomy",
    "worst quality",
    "low quality",
    "lowres",
    "jpeg artifacts",
    "extra fingers",
    "mutated",
    "deformed",
    "blurry",
    "nsfw",
    "negative prompt",
)


def _comfy_api_positive(raw: str) -> str | None:
    """从 ComfyUI API 格式 prompt JSON 抽出正向提示词。

    优先：找到采样器（KSampler 等）的 `positive` 输入，沿边解析到 CLIPTextEncode.text。
    回退：排除已被标成 `negative` 的 CLIP；多条时不选「像负向专用词表」的那条；
    仍歧义才取最短非空（负向词表往往更长——旧逻辑取最长会抽错）。
    """
    raw = raw.strip()
    if not raw.startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None

    by_id = {str(nid): node for nid, node in obj.items() if isinstance(node, dict)}

    # 1) 沿采样器 positive 边
    for node in by_id.values():
        class_type = str(node.get("class_type") or "")
        if not any(marker in class_type for marker in _SAMPLER_MARKERS):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        text = _comfy_follow_text(by_id, inputs.get("positive"))
        if text:
            return text

    # 2) 收集 CLIP 文本，标出被 negative 边引用的节点
    negative_ids: set[str] = set()
    for node in by_id.values():
        class_type = str(node.get("class_type") or "")
        if not any(marker in class_type for marker in _SAMPLER_MARKERS):
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        neg = inputs.get("negative")
        if isinstance(neg, list) and neg:
            negative_ids.add(str(neg[0]))
            # 再往回一层（ConditioningCombine 等）
            for hop in _comfy_upstream_ids(by_id, neg, depth=3):
                negative_ids.add(hop)

    clips: list[tuple[str, str]] = []  # (node_id, text)
    for nid, node in by_id.items():
        class_type = str(node.get("class_type") or "")
        if "CLIPTextEncode" not in class_type:
            continue
        inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
        text = inputs.get("text")
        if isinstance(text, str) and text.strip():
            clips.append((nid, text.strip()))
    if not clips:
        return None

    positive_candidates = [text for nid, text in clips if nid not in negative_ids]
    pool = positive_candidates or [text for _nid, text in clips]
    clean = [text for text in pool if not _looks_negative_only(text)]
    chosen = clean or pool
    # 歧义时取最短：负向词表常更长；绝不再用「最长即正向」
    return min(chosen, key=len)


def _comfy_link_id(link) -> str | None:
    if isinstance(link, list) and link:
        return str(link[0])
    if isinstance(link, (str, int)):
        return str(link)
    return None


def _comfy_upstream_ids(by_id: dict, link, depth: int) -> list[str]:
    nid = _comfy_link_id(link)
    if not nid or depth < 0 or nid not in by_id:
        return []
    out = [nid]
    node = by_id[nid]
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    class_type = str(node.get("class_type") or "")
    if "CLIPTextEncode" in class_type:
        return out
    for key, value in inputs.items():
        if key in ("clip", "model", "vae", "pixels", "image", "latent"):
            continue
        if isinstance(value, list) and value and str(value[0]) in by_id:
            out.extend(_comfy_upstream_ids(by_id, value, depth - 1))
    return out


def _comfy_follow_text(by_id: dict, link, depth: int = 6) -> str | None:
    """从 conditioning 边走到 CLIPTextEncode.text；允许经过普通 Conditioning* 包装。"""
    nid = _comfy_link_id(link)
    if not nid or depth < 0 or nid not in by_id:
        return None
    node = by_id[nid]
    class_type = str(node.get("class_type") or "")
    inputs = node.get("inputs") if isinstance(node.get("inputs"), dict) else {}
    if "CLIPTextEncode" in class_type:
        text = inputs.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        return None
    # 常见：ConditioningCombine / ConditioningConcat / ConditioningSetArea …
    if any(marker in class_type for marker in _COND_PASSTHRU) or class_type.startswith("Conditioning"):
        for key in ("conditioning", "conditioning_1", "conditioning_2", "positive", "text"):
            if key in inputs:
                hit = _comfy_follow_text(by_id, inputs.get(key), depth - 1)
                if hit:
                    return hit
        for key, value in inputs.items():
            if key in ("clip", "model", "vae", "strength", "width", "height", "x", "y"):
                continue
            if isinstance(value, list) and value:
                hit = _comfy_follow_text(by_id, value, depth - 1)
                if hit:
                    return hit
    # 未知节点：仍尝试唯一的 list 边
    links = [value for value in inputs.values() if isinstance(value, list) and value and str(value[0]) in by_id]
    if len(links) == 1:
        return _comfy_follow_text(by_id, links[0], depth - 1)
    return None


def _looks_negative_only(text: str) -> bool:
    lower = text.lower()
    hits = sum(1 for hint in _NEG_ONLY_HINTS if hint in lower)
    # 多条负向词、几乎没有场景描述时，当作负向专用
    return hits >= 2 and len(text) < 4000


# ---------- EXIF (JPEG / WebP) ----------

def _from_exif_container(data: bytes, kind: str) -> dict:
    exif = _exif_bytes(data, kind)
    if not exif:
        return _miss()
    fields = _parse_exif(exif)
    for key in ("user_comment", "image_description"):
        raw = fields.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        positive = _a1111_positive(raw)
        if positive:
            return _hit(positive, "exif_a1111")
        # 纯描述
        if raw.strip():
            return _hit(raw.strip(), "exif")
    return _miss()


def _exif_bytes(data: bytes, kind: str) -> bytes | None:
    if kind == "jpeg":
        return _jpeg_exif(data)
    if kind == "webp":
        return _webp_exif(data)
    return None


def _jpeg_exif(data: bytes) -> bytes | None:
    if not data.startswith(_JPEG_SOI):
        return None
    pos = 2
    length = len(data)
    while pos + 4 <= length:
        if data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xDA:  # SOS
            break
        if marker == 0xD9:  # EOI
            break
        if marker in (0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0x01) or marker == 0xFF:
            pos += 2
            continue
        size = struct.unpack_from(">H", data, pos + 2)[0]
        if size < 2 or pos + 2 + size > length:
            break
        payload = data[pos + 4: pos + 2 + size]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            return payload[6:]
        pos += 2 + size
    return None


def _webp_exif(data: bytes) -> bytes | None:
    if len(data) < 12 or not data.startswith(_WEBP_RIFF) or data[8:12] != _WEBP_WEBP:
        return None
    pos = 12
    length = len(data)
    while pos + 8 <= length:
        tag = data[pos: pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        start = pos + 8
        end = start + size
        if end > length:
            break
        if tag == b"EXIF":
            chunk = data[start:end]
            if chunk.startswith(b"Exif\x00\x00"):
                return chunk[6:]
            return chunk
        pos = end + (size & 1)  # pad to even
    return None


def _parse_exif(tiff: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    if len(tiff) < 8:
        return out
    endian = tiff[0:2]
    if endian == b"II":
        end = "<"
    elif endian == b"MM":
        end = ">"
    else:
        return out
    try:
        magic = struct.unpack_from(end + "H", tiff, 2)[0]
        if magic != 42:
            return out
        ifd0 = struct.unpack_from(end + "I", tiff, 4)[0]
    except struct.error:
        return out
    entries = _read_ifd(tiff, ifd0, end)
    if _TAG_IMAGE_DESCRIPTION in entries:
        text = _exif_string(tiff, entries[_TAG_IMAGE_DESCRIPTION], end)
        if text:
            out["image_description"] = text
    # Exif IFD pointer 0x8769
    if 0x8769 in entries:
        typ, count, value = entries[0x8769]
        if typ == 4 and count == 1:
            exif_ifd = value if isinstance(value, int) else struct.unpack(end + "I", value[:4])[0]
            sub = _read_ifd(tiff, exif_ifd, end)
            if _TAG_USER_COMMENT in sub:
                text = _exif_user_comment(tiff, sub[_TAG_USER_COMMENT], end)
                if text:
                    out["user_comment"] = text
    elif _TAG_USER_COMMENT in entries:
        text = _exif_user_comment(tiff, entries[_TAG_USER_COMMENT], end)
        if text:
            out["user_comment"] = text
    return out


def _read_ifd(tiff: bytes, offset: int, end: str) -> dict[int, tuple]:
    out: dict[int, tuple] = {}
    if offset < 0 or offset + 2 > len(tiff):
        return out
    try:
        count = struct.unpack_from(end + "H", tiff, offset)[0]
    except struct.error:
        return out
    pos = offset + 2
    for _ in range(min(count, 256)):
        if pos + 12 > len(tiff):
            break
        try:
            tag, typ, cnt = struct.unpack_from(end + "HHI", tiff, pos)
            raw = tiff[pos + 8: pos + 12]
        except struct.error:
            break
        value: int | bytes
        type_size = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1}.get(typ, 1)
        nbytes = cnt * type_size
        if nbytes <= 4:
            value = raw
            if typ == 3 and cnt == 1:
                value = struct.unpack_from(end + "H", raw, 0)[0]
            elif typ == 4 and cnt == 1:
                value = struct.unpack_from(end + "I", raw, 0)[0]
        else:
            try:
                ptr = struct.unpack_from(end + "I", raw, 0)[0]
            except struct.error:
                pos += 12
                continue
            if ptr < 0 or ptr + nbytes > len(tiff):
                pos += 12
                continue
            value = tiff[ptr: ptr + nbytes]
        out[tag] = (typ, cnt, value)
        pos += 12
    return out


def _exif_string(tiff: bytes, entry: tuple, end: str) -> str:
    typ, count, value = entry
    if typ != 2:
        return ""
    raw = value if isinstance(value, bytes) else struct.pack(end + "I", int(value))[:count]
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()


def _exif_user_comment(tiff: bytes, entry: tuple, end: str) -> str:
    typ, count, value = entry
    raw = value if isinstance(value, bytes) else b""
    if not raw:
        return ""
    # ASCII\0\0\0 / UNICODE\0 / JIS\0\0\0\0 / \0\0\0\0\0\0\0\0
    if len(raw) >= 8:
        charset = raw[:8]
        body = raw[8:]
        if charset.startswith(b"ASCII"):
            return body.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()
        if charset.startswith(b"UNICODE"):
            # UTF-16 with same endian as TIFF
            enc = "utf-16-le" if end == "<" else "utf-16-be"
            try:
                return body.decode(enc, errors="replace").split("\x00", 1)[0].strip()
            except Exception:
                return ""
        if charset == b"\x00" * 8:
            return body.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
    return raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
