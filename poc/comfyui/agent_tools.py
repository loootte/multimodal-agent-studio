"""模型能调用的两个生成工具。

工具列表里没有 ComfyUI 节点。参数先校验，再交给模板填充和客户端。
标准输出只有工具结果，没有 workflow JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from contextlib import contextmanager

import artifacts
import comfy_client
import comfy_poc
import template_fill

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOOL_NAMES = ("generate_image", "generate_video")
_run_listener = None
FORBIDDEN_IN_SCHEMA = (
    "LoadImage",
    "KSampler",
    "CLIPTextEncode",
    "class_type",
    "UnetLoader",
    "SaveImage",
    "SaveVideo",
    "WanImageToVideo",
)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def load_styles() -> dict[str, str]:
    path = os.path.join(SCRIPT_DIR, "styles.json")
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or not data:
        comfy_poc.fail(f"风格表不可用：{path}")
    styles = {}
    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or not value.strip():
            comfy_poc.fail(f"风格表里的 {key!r} 不是一句可用的风格句。")
        styles[key] = value
    return styles


def tool_specs() -> list[dict]:
    aspects = list(comfy_poc.ASPECTS)
    styles = sorted(load_styles())
    return [
        {
            "name": "generate_image",
            "description": "根据画面描述生成一张图片。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "aspect_ratio"],
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "description": "画面描述。"},
                    "aspect_ratio": {"type": "string", "enum": aspects, "description": "画幅，只能是白名单里的值。"},
                    "seed": {"type": "integer", "minimum": 0, "description": "可选。不传则由运行时生成。"},
                    "style": {"type": "string", "enum": styles, "description": "可选。只映射到固定的风格句。"},
                },
            },
        },
        {
            "name": "generate_video",
            "description": "根据画面描述生成一段视频。传入 artifact_id 时走图生视频，否则走文生视频。",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["prompt", "duration_sec", "aspect_ratio"],
                "properties": {
                    "prompt": {"type": "string", "minLength": 1, "description": "画面描述。"},
                    "duration_sec": {"type": "number", "exclusiveMinimum": 0, "description": "时长（秒）。超过模板上限会被拒绝。"},
                    "aspect_ratio": {"type": "string", "enum": aspects, "description": "画幅，只能是白名单里的值。"},
                    "image_ref": {"type": "string", "description": "可选。上一张图的 artifact_id。传入则图生视频，不传则文生视频。"},
                },
            },
        },
    ]


def tool_error(tool: str, code: str, message: str, parameter: str | None = None) -> dict:
    error = {"code": code, "message": message}
    if parameter:
        error["parameter"] = parameter
    return {"ok": False, "tool": tool, "error": error}


def public_comfy_message(exc: comfy_poc.ComfyFailure) -> str:
    text = comfy_client.original_error_text(exc.payload)
    if text:
        return text
    return exc.message


def unexpected(tool: str, arguments: dict) -> dict | None:
    allowed = {
        "generate_image": {"prompt", "aspect_ratio", "seed", "style"},
        "generate_video": {"prompt", "duration_sec", "aspect_ratio", "image_ref"},
    }[tool]
    extra = sorted(set(arguments) - allowed)
    if extra:
        return tool_error(tool, "unexpected_parameter", f"不能传这些参数：{', '.join(extra)}。", extra[0])
    return None


def require_prompt(tool: str, arguments: dict) -> dict | None:
    if "prompt" not in arguments:
        return tool_error(tool, "missing_prompt", "prompt 是必填项。", "prompt")
    prompt = arguments["prompt"]
    if not isinstance(prompt, str) or not prompt.strip():
        return tool_error(tool, "missing_prompt", "prompt 必须是非空字符串。", "prompt")
    return None


def parse_aspect(tool: str, arguments: dict, allowed: set[str]) -> tuple[str | None, dict | None]:
    if "aspect_ratio" not in arguments:
        return None, tool_error(tool, "invalid_aspect_ratio", "aspect_ratio 是必填项。", "aspect_ratio")
    aspect = arguments["aspect_ratio"]
    if not isinstance(aspect, str) or aspect not in allowed:
        names = "、".join(comfy_poc.ASPECTS)
        return None, tool_error(tool, "invalid_aspect_ratio", f"aspect_ratio 只能是 {names}。", "aspect_ratio")
    return aspect, None


def parse_seed(tool: str, arguments: dict) -> tuple[int | None, dict | None]:
    if "seed" not in arguments or arguments["seed"] is None:
        return random.randrange(0, 2**32), None
    seed = arguments["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        return None, tool_error(tool, "invalid_seed", "seed 必须是大于等于 0 的整数。", "seed")
    return seed, None


def parse_style(tool: str, arguments: dict, styles: dict[str, str]) -> tuple[str | None, dict | None]:
    if "style" not in arguments or arguments["style"] is None:
        return None, None
    style = arguments["style"]
    if not isinstance(style, str) or style not in styles:
        names = "、".join(sorted(styles))
        return None, tool_error(tool, "unknown_style", f"style 只能是 {names}。", "style")
    return style, None


def values_match(tool: str, workflow: dict, fields: dict, prompt: str, aspect: str, seed: int, frames: int | None = None) -> dict | None:
    if template_fill.read_back(workflow, fields, "positive_prompt") != prompt:
        return tool_error(tool, "internal_error", "填进模板的提示词和参数不一致。")
    size = fields["aspects"][aspect]
    if template_fill.read_back(workflow, fields, "width") != int(size["width"]):
        return tool_error(tool, "internal_error", "填进模板的宽度和画幅不一致。")
    if template_fill.read_back(workflow, fields, "height") != int(size["height"]):
        return tool_error(tool, "internal_error", "填进模板的高度和画幅不一致。")
    if template_fill.read_back(workflow, fields, "seed") != seed:
        return tool_error(tool, "internal_error", "填进模板的 seed 和参数不一致。")
    if frames is not None and template_fill.read_back(workflow, fields, "frames") != frames:
        return tool_error(tool, "internal_error", "填进模板的帧数和时长不一致。")
    return None


@contextmanager
def listen_run(callback):
    """生成过程中把提交和进度交给聊天记录。不改变工具参数。"""
    global _run_listener
    previous = _run_listener
    _run_listener = callback
    try:
        yield
    finally:
        _run_listener = previous


def validate_tool(name: str, arguments: dict) -> dict | None:
    """提交前的参数校验。通过时返回 None，不调用 ComfyUI。"""
    if name not in TOOL_NAMES:
        return tool_error(name, "unknown_tool", "只能调用 generate_image 或 generate_video。")
    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_parameter", "工具参数必须是对象。")
    if name == "generate_image":
        return _validate_image(arguments)
    return _validate_video(arguments)


def _validate_image(arguments: dict) -> dict | None:
    tool = "generate_image"
    problem = unexpected(tool, arguments) or require_prompt(tool, arguments)
    if problem:
        return problem
    styles = load_styles()
    aspect, problem = parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    if "seed" in arguments and arguments["seed"] is not None:
        _seed, problem = parse_seed(tool, arguments)
        if problem:
            return problem
    _style, problem = parse_style(tool, arguments, styles)
    if problem:
        return problem
    config = comfy_poc.load_config()
    _template, fields, _timeout = template_fill.load_job(config, "image")
    if aspect not in (fields.get("aspects") or {}):
        return tool_error(tool, "invalid_aspect_ratio", "这张文生图模板没有这个画幅。", "aspect_ratio")
    return None


def _validate_video(arguments: dict) -> dict | None:
    tool = "generate_video"
    problem = unexpected(tool, arguments) or require_prompt(tool, arguments)
    if problem:
        return problem
    if "duration_sec" not in arguments:
        return tool_error(tool, "invalid_duration", "duration_sec 是必填项。", "duration_sec")
    duration = arguments["duration_sec"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return tool_error(tool, "invalid_duration", "duration_sec 必须是大于 0 的秒数。", "duration_sec")
    aspect, problem = parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    image_ref = arguments.get("image_ref")
    config = comfy_poc.load_config()
    if image_ref is not None:
        if not isinstance(image_ref, str) or not image_ref.strip():
            return tool_error(tool, "image_not_found", "image_ref 必须是 artifact_id。", "image_ref")
        try:
            artifacts.resolve_image_ref(config, image_ref)
        except artifacts.ArtifactError as exc:
            return tool_error(tool, "image_not_found", exc.message, "image_ref")
    kind = "video_i2v" if image_ref else "video"
    _template, fields, _timeout = template_fill.load_job(config, kind)
    if aspect not in (fields.get("aspects") or {}):
        return tool_error(tool, "invalid_aspect_ratio", "这张视频模板没有这个画幅。", "aspect_ratio")
    limit = template_fill.max_duration_sec(fields)
    if float(duration) > limit:
        return tool_error(tool, "duration_too_long", f"duration_sec 超过模板上限 {limit:g} 秒。", "duration_sec")
    return None


def _published_result(tool: str, records: list[dict], extra: dict) -> dict:
    result = {"ok": True, "tool": tool}
    result.update(records[0])
    if len(records) > 1:
        result["artifact_ids"] = [item["artifact_id"] for item in records]
    result.update(extra)
    return result


def generate_image(prompt: str, aspect_ratio: str, seed: int | None = None, style: str | None = None) -> dict:
    arguments = {"prompt": prompt, "aspect_ratio": aspect_ratio, "seed": seed, "style": style}
    return call_tool("generate_image", {key: value for key, value in arguments.items() if value is not None or key in {"prompt", "aspect_ratio"}})


def generate_video(prompt: str, duration_sec: float, aspect_ratio: str, image_ref: str | None = None) -> dict:
    arguments = {
        "prompt": prompt,
        "duration_sec": duration_sec,
        "aspect_ratio": aspect_ratio,
        "image_ref": image_ref,
    }
    return call_tool(
        "generate_video",
        {key: value for key, value in arguments.items() if value is not None or key != "image_ref"},
    )


def _generate_image(arguments: dict) -> dict:
    tool = "generate_image"
    problem = unexpected(tool, arguments) or require_prompt(tool, arguments)
    if problem:
        return problem
    styles = load_styles()
    aspect, problem = parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    seed, problem = parse_seed(tool, arguments)
    if problem:
        return problem
    style, problem = parse_style(tool, arguments, styles)
    if problem:
        return problem
    config = comfy_poc.load_config()
    template, fields, timeout = template_fill.load_job(config, "image")
    if aspect not in (fields.get("aspects") or {}):
        return tool_error(tool, "invalid_aspect_ratio", "这张文生图模板没有这个画幅。", "aspect_ratio")
    negative = styles[style] if style else None
    workflow = template_fill.copy_template(template)
    template_fill.fill_common(workflow, fields, arguments["prompt"], aspect, seed, negative)
    problem = values_match(tool, workflow, fields, arguments["prompt"], aspect, seed)
    if problem:
        problem["tool"] = tool
        return problem
    if style and template_fill.read_back(workflow, fields, "negative_prompt") != styles[style]:
        return tool_error(tool, "internal_error", "风格句没有写进模板。")
    template_fill.assert_template_edit(template, workflow, fields)
    files = comfy_client.run(
        config["comfy_url"], template, workflow, fields, timeout, config["output_dir"], on_event=_run_listener,
    )
    records = artifacts.remember_outputs(
        config,
        files,
        kind="image",
        user_prompt=arguments["prompt"],
        workflow_prompt=template_fill.read_back(workflow, fields, "positive_prompt"),
        seed=int(template_fill.read_back(workflow, fields, "seed")),
        width=int(template_fill.read_back(workflow, fields, "width")),
        height=int(template_fill.read_back(workflow, fields, "height")),
    )
    return _published_result(tool, records, {"aspect_ratio": aspect, "style": style})


def _generate_video(arguments: dict) -> dict:
    tool = "generate_video"
    problem = unexpected(tool, arguments) or require_prompt(tool, arguments)
    if problem:
        return problem
    if "duration_sec" not in arguments:
        return tool_error(tool, "invalid_duration", "duration_sec 是必填项。", "duration_sec")
    duration = arguments["duration_sec"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return tool_error(tool, "invalid_duration", "duration_sec 必须是大于 0 的秒数。", "duration_sec")
    aspect, problem = parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    seed, problem = parse_seed(tool, arguments)
    if problem:
        return problem
    image_ref = arguments.get("image_ref")
    config = comfy_poc.load_config()
    local_image = None
    ref_id = None
    if image_ref is not None:
        if not isinstance(image_ref, str) or not image_ref.strip():
            return tool_error(tool, "image_not_found", "image_ref 必须是 artifact_id。", "image_ref")
        try:
            local_image, ref_id = artifacts.resolve_image_ref(config, image_ref)
        except artifacts.ArtifactError as exc:
            return tool_error(tool, "image_not_found", exc.message, "image_ref")
    kind = "video_i2v" if image_ref else "video"
    template, fields, timeout = template_fill.load_job(config, kind)
    if aspect not in (fields.get("aspects") or {}):
        return tool_error(tool, "invalid_aspect_ratio", "这张视频模板没有这个画幅。", "aspect_ratio")
    limit = template_fill.max_duration_sec(fields)
    if float(duration) > limit:
        return tool_error(
            tool,
            "duration_too_long",
            f"duration_sec 超过模板上限 {limit:g} 秒。",
            "duration_sec",
        )
    frames = template_fill.frames_for_duration(fields, float(duration))
    workflow = template_fill.copy_template(template)
    template_fill.fill_common(workflow, fields, arguments["prompt"], aspect, seed, None)
    template_fill.fill_frames(workflow, fields, frames)
    if local_image:
        template_fill.assert_image_to_video(workflow, fields)
        uploaded = comfy_client.upload(config["comfy_url"], local_image)
        template_fill.set_reference_name(workflow, fields, uploaded)
        template_fill.assert_image_to_video(workflow, fields)
        mode = "image_to_video"
    else:
        template_fill.assert_text_to_video(workflow, fields)
        mode = "text_to_video"
    problem = values_match(tool, workflow, fields, arguments["prompt"], aspect, seed, frames)
    if problem:
        return problem
    template_fill.assert_template_edit(template, workflow, fields)
    files = comfy_client.run(
        config["comfy_url"], template, workflow, fields, timeout, config["output_dir"], on_event=_run_listener,
    )
    records = artifacts.remember_outputs(
        config,
        files,
        kind=kind,
        user_prompt=arguments["prompt"],
        workflow_prompt=template_fill.read_back(workflow, fields, "positive_prompt"),
        seed=int(template_fill.read_back(workflow, fields, "seed")),
        width=int(template_fill.read_back(workflow, fields, "width")),
        height=int(template_fill.read_back(workflow, fields, "height")),
        duration_sec=float(frames) / float(fields.get("fps") or 16),
    )
    extra = {"aspect_ratio": aspect, "frames": frames, "mode": mode}
    if ref_id:
        extra["image_ref"] = ref_id
    return _published_result(tool, records, extra)


def call_tool(name: str, arguments: dict) -> dict:
    if name not in TOOL_NAMES:
        return tool_error(name, "unknown_tool", "只能调用 generate_image 或 generate_video。")
    if not isinstance(arguments, dict):
        return tool_error(name, "invalid_parameter", "工具参数必须是对象。")
    try:
        with comfy_poc.talk_off():
            if name == "generate_image":
                return _generate_image(arguments)
            return _generate_video(arguments)
    except template_fill.TemplateError as exc:
        return tool_error(name, "template_error", exc.message)
    except artifacts.ArtifactError as exc:
        return tool_error(name, "artifact_error", exc.message)
    except comfy_poc.ComfyFailure as exc:
        return tool_error(name, "comfyui_error", public_comfy_message(exc))


def self_check() -> None:
    specs = tool_specs()
    names = [item["name"] for item in specs]
    if names != list(TOOL_NAMES):
        comfy_poc.fail(f"工具列表不对：{names}")
    published = json.dumps(specs, ensure_ascii=False)
    for word in FORBIDDEN_IN_SCHEMA:
        if word in published:
            comfy_poc.fail(f"工具 schema 里出现了 {word}")
    config = comfy_poc.load_config()
    styles = load_styles()
    with comfy_poc.talk_off():
        workflow, fields, _timeout = template_fill.load_job(config, "image")
        template_fill.fill_common(workflow, fields, "a red teapot", "1:1", 7, styles["photograph"])
        if template_fill.read_back(workflow, fields, "positive_prompt") != "a red teapot":
            comfy_poc.fail("文生图提示词没有原样写入。")
        if template_fill.read_back(workflow, fields, "negative_prompt") != styles["photograph"]:
            comfy_poc.fail("风格句没有写入负向提示词。")
        if template_fill.read_back(workflow, fields, "seed") != 7:
            comfy_poc.fail("seed 没有原样写入。")

        text_workflow, text_fields, _timeout = template_fill.load_job(config, "video")
        template_fill.assert_text_to_video(text_workflow, text_fields)
        frames = template_fill.frames_for_duration(text_fields, 1)
        if frames != 17:
            comfy_poc.fail(f"1 秒没有对齐到 17 帧，而是 {frames}。")
        template_fill.fill_common(text_workflow, text_fields, "camera push", "16:9", 8, None)
        template_fill.fill_frames(text_workflow, text_fields, frames)
        template_fill.assert_text_to_video(text_workflow, text_fields)

        image_workflow, image_fields, _timeout = template_fill.load_job(config, "video_i2v")
        template_fill.assert_image_to_video(image_workflow, image_fields)
        template_fill.set_reference_name(image_workflow, image_fields, "uploaded.png")
        template_fill.assert_image_to_video(image_workflow, image_fields)
        if template_fill.read_back(image_workflow, image_fields, "reference_image") != "uploaded.png":
            comfy_poc.fail("参考图文件名没有写入图生视频模板。")

    samples = [
        call_tool("generate_image", {"prompt": "  ", "aspect_ratio": "1:1"}),
        call_tool("generate_image", {"prompt": "teapot", "aspect_ratio": "4:3"}),
        call_tool("generate_image", {"prompt": "teapot", "aspect_ratio": "1:1", "style": "oil"}),
        call_tool("generate_image", {"prompt": "teapot", "aspect_ratio": "1:1", "seed": -3}),
        call_tool("generate_image", {"prompt": "teapot", "aspect_ratio": "1:1", "sampler": "euler"}),
        call_tool("generate_video", {"prompt": "teapot", "duration_sec": 30, "aspect_ratio": "16:9"}),
        call_tool("generate_video", {"prompt": "teapot", "duration_sec": 0, "aspect_ratio": "16:9"}),
        call_tool("generate_video", {"prompt": "teapot", "duration_sec": 1, "aspect_ratio": "16:9", "image_ref": r"D:\missing-ref.png"}),
        call_tool("KSampler", {"prompt": "teapot"}),
    ]
    codes = [item["error"]["code"] for item in samples]
    expected = [
        "missing_prompt",
        "invalid_aspect_ratio",
        "unknown_style",
        "invalid_seed",
        "unexpected_parameter",
        "duration_too_long",
        "invalid_duration",
        "image_not_found",
        "unknown_tool",
    ]
    if codes != expected:
        comfy_poc.fail(f"错误码不对：{codes}")
    for item in samples:
        if item["ok"] or "workflow" in json.dumps(item):
            comfy_poc.fail("失败结果不应表示成功，也不应带 workflow。")
    artifacts.self_check()
    _check_artifact_handoff()


def _check_artifact_handoff() -> None:
    import tempfile

    temporary = tempfile.TemporaryDirectory()
    root = temporary.name
    old_dir = os.environ.get("COMFY_ARTIFACT_DIR")
    old_session = os.environ.get("COMFY_SESSION")
    os.environ["COMFY_ARTIFACT_DIR"] = root
    os.environ["COMFY_SESSION"] = "session-a"
    png = artifacts._png(8, 6)
    source = os.path.join(root, "in.png")
    rendered = os.path.join(root, "out.mp4")
    with open(source, "wb") as handle:
        handle.write(png)
    with open(rendered, "wb") as handle:
        handle.write(artifacts._mp4(0, 512, 288, 16, 17))
    store = artifacts.ArtifactStore(root)
    image = store.save(
        session_id="session-a",
        source_path=source,
        seed=7,
        template="workflows/image_api.json",
        user_prompt="user says a red teapot",
        workflow_prompt="a red ceramic teapot on a wooden table",
        width=8,
        height=6,
    )
    seen = {}

    def fake_upload(_base, path):
        with open(path, "rb") as handle:
            seen["upload"] = handle.read()
        return "ref.png"

    def fake_run(*_args, **_kwargs):
        return [rendered]

    original_upload = comfy_client.upload
    original_run = comfy_client.run
    comfy_client.upload = fake_upload
    comfy_client.run = fake_run
    try:
        result = call_tool(
            "generate_video",
            {
                "prompt": "the camera slowly pushes in",
                "duration_sec": 1,
                "aspect_ratio": "16:9",
                "image_ref": image["artifact_id"],
            },
        )
    finally:
        comfy_client.upload = original_upload
        comfy_client.run = original_run
        if old_dir is None:
            os.environ.pop("COMFY_ARTIFACT_DIR", None)
        else:
            os.environ["COMFY_ARTIFACT_DIR"] = old_dir
        if old_session is None:
            os.environ.pop("COMFY_SESSION", None)
        else:
            os.environ["COMFY_SESSION"] = old_session
    try:
        if not result.get("ok"):
            comfy_poc.fail(f"artifact_id 没有交给 generate_video：{result}")
        if seen.get("upload") != png:
            comfy_poc.fail("generate_video 没有取回上一张图的文件。")
        if result.get("image_ref") != image["artifact_id"]:
            comfy_poc.fail("结果没有沿用上一张图的 artifact_id。")
        if "files" in result or source in json.dumps(result) or rendered in json.dumps(result):
            comfy_poc.fail("工具结果里出现了文件路径。")
        body = artifacts.model_request(
            "session-a",
            "用刚才那张图做视频",
            [image["artifact_id"], result["artifact_id"]],
        )
        artifacts.assert_model_text(
            body,
            [image["artifact_id"], result["artifact_id"]],
            [source, rendered, store.file_for("session-a", image["artifact_id"]) or ""],
            [png, open(rendered, "rb").read()],
        )
        if artifacts.ArtifactStore(root).file_for("session-b", result["artifact_id"]) is not None:
            comfy_poc.fail("下一轮视频工件能被别的会话读到。")
    finally:
        temporary.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用 generate_image 或 generate_video。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="打印模型侧工具列表。")
    subparsers.add_parser("check", help="校验工具列表、模板填充和非法参数。不提交 ComfyUI。")
    call = subparsers.add_parser("call", help="用一份 JSON 参数调用工具。")
    call.add_argument("name", choices=TOOL_NAMES)
    call.add_argument("--json", dest="payload", help="工具参数 JSON。")
    call.add_argument("--json-file", dest="payload_file", help="工具参数 JSON 文件。和 --json 二选一。")
    return parser


def main(argv: list[str] | None = None) -> None:
    configure_stdio()
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            print(json.dumps(tool_specs(), ensure_ascii=False, indent=2))
            return
        if args.command == "check":
            self_check()
            print("self_check ok")
            return
        if bool(args.payload) == bool(args.payload_file):
            result = tool_error(args.name, "invalid_parameter", "call 需要 --json 或 --json-file 其中一个。")
        else:
            raw = args.payload
            if args.payload_file:
                with open(args.payload_file, encoding="utf-8") as handle:
                    raw = handle.read()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                result = tool_error(args.name, "invalid_parameter", f"参数不是 JSON：{exc.msg}")
            else:
                result = call_tool(args.name, payload)
    except (comfy_poc.ComfyFailure, template_fill.TemplateError, artifacts.ArtifactError) as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
