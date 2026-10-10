"""用一条提示词调用本地 ComfyUI，生成一张图片或一段视频。

地址、工作流路径和字段对照都来自配置文件或环境变量。
换模板时改 JSON 和 *.fields.json，不要改这个脚本。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys

import artifacts
import comfy_client
import template_fill
from comfy_client import ComfyFailure

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASPECTS = ("1:1", "16:9", "9:16")
_talk = True


class talk_off:
    """工具调用时不把节点和 workflow 打到标准输出。"""

    def __enter__(self):
        global _talk
        self._previous = _talk
        _talk = False
        return self

    def __exit__(self, exc_type, exc, tb):
        global _talk
        _talk = self._previous
        return False


def note(message: str) -> None:
    if _talk:
        print(message, flush=True)


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def fail(message: str, payload=None) -> None:
    raise ComfyFailure(message, payload)


def load_json(path: str):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def resolve_path(base: str, value: str) -> str:
    if os.path.isabs(value):
        return value
    return os.path.normpath(os.path.join(base, value))


def load_config() -> dict:
    path = os.environ.get("COMFY_CONFIG")
    if not path:
        path = os.path.join(SCRIPT_DIR, "config.json")
    if not os.path.isfile(path):
        fail(
            "找不到配置文件。把 config.example.json 复制为 config.json，"
            "或设置 COMFY_CONFIG。"
        )
    config = load_json(path)
    base = os.path.dirname(os.path.abspath(path))
    config["_base"] = base
    if os.environ.get("COMFY_URL"):
        config["comfy_url"] = os.environ["COMFY_URL"]
    if os.environ.get("COMFY_OUTPUT_DIR"):
        config["output_dir"] = os.environ["COMFY_OUTPUT_DIR"]
    for key, env_name in (
        ("image", "COMFY_IMAGE_WORKFLOW"),
        ("video", "COMFY_VIDEO_WORKFLOW"),
    ):
        override = os.environ.get(env_name)
        if override:
            config.setdefault(key, {})["workflow"] = override
    url = str(config.get("comfy_url") or "").rstrip("/")
    if not url:
        fail("配置里没有 comfy_url，也没有环境变量 COMFY_URL。")
    config["comfy_url"] = url
    output_dir = config.get("output_dir")
    if not output_dir:
        fail("配置里没有 output_dir，也没有环境变量 COMFY_OUTPUT_DIR。")
    config["output_dir"] = resolve_path(base, output_dir)
    return config


def preview_enabled(config: dict, kind: str) -> bool:
    section = config.get(kind) or {}
    return section.get("preview") is True


def job_paths(config: dict, kind: str) -> tuple[dict, dict, int]:
    section = config.get(kind) or {}
    workflow_path = section.get("workflow")
    fields_path = section.get("fields")
    if not workflow_path or not fields_path:
        fail(f"配置的 {kind} 需要 workflow 和 fields。")
    base = config["_base"]
    workflow_path = resolve_path(base, workflow_path)
    fields_path = resolve_path(base, fields_path)
    if not os.path.isfile(workflow_path):
        fail(f"工作流不存在：{workflow_path}")
    if not os.path.isfile(fields_path):
        fail(f"字段对照不存在：{fields_path}")
    timeout = int(section.get("timeout_sec") or (900 if kind == "image" else 2700))
    return load_json(workflow_path), load_json(fields_path), timeout


def targets(spec) -> list[dict]:
    if isinstance(spec, list):
        return spec
    return [spec]


def set_inputs(prompt: dict, spec, value) -> None:
    for target in targets(spec):
        node_id = str(target["node"])
        field = target["input"]
        node = prompt.get(node_id)
        if node is None:
            fail(f"工作流里没有节点 {node_id}，字段对照和模板不一致。")
        if "inputs" not in node:
            fail(f"节点 {node_id} 没有 inputs。")
        node["inputs"][field] = value


def snap_frames(raw: int, step: int, limit: int) -> int:
    if step < 1:
        fail("frame_step 必须大于 0。")
    if limit < 1:
        fail("max_frames 必须大于 0。")
    count = max(1, int(raw))
    steps = int(round((count - 1) / step))
    snapped = steps * step + 1
    max_steps = (limit - 1) // step
    capped = max_steps * step + 1
    if snapped > capped:
        snapped = capped
    return max(1, snapped)


def apply_common(prompt: dict, fields: dict, args) -> None:
    aspects = fields.get("aspects") or {}
    size = aspects.get(args.aspect)
    if not isinstance(size, dict):
        fail(f"字段对照里没有画幅 {args.aspect}。允许的值：{', '.join(ASPECTS)}。")
    set_inputs(prompt, fields["positive_prompt"], args.prompt)
    if args.negative is not None:
        if "negative_prompt" not in fields:
            fail("这张工作流的字段对照没有 negative_prompt。")
        set_inputs(prompt, fields["negative_prompt"], args.negative)
    set_inputs(prompt, fields["width"], int(size["width"]))
    set_inputs(prompt, fields["height"], int(size["height"]))
    seed = args.seed if args.seed is not None else random.randrange(0, 2**32)
    if seed < 0:
        fail("seed 不能是负数。")
    set_inputs(prompt, fields["seed"], int(seed))
    positive = targets(fields["positive_prompt"])[0]
    written = prompt[str(positive["node"])]["inputs"][positive["input"]]
    if written != args.prompt:
        fail("替换后的正向提示词和输入不一致。", {"node": positive, "text": written})
    note(f"正向提示词写入节点 {positive['node']} 的 {positive['input']}：{written}")
    note(f"画幅 {args.aspect} → {int(size['width'])}x{int(size['height'])}，seed {seed}")


def apply_frames(prompt: dict, fields: dict, args) -> None:
    if "frames" not in fields:
        fail("视频字段对照里没有 frames。")
    frames = template_fill.allowed_frame_counts(fields)
    durations = template_fill.allowed_durations(fields)
    if args.frames is not None:
        if int(args.frames) not in frames:
            shown = "、".join(str(item) for item in frames)
            fail(f"帧数不在白名单内。允许的值：{shown}。")
        length = int(args.frames)
        requested = length
    elif args.seconds is not None:
        limit = template_fill.max_duration_sec(fields)
        if float(args.seconds) > limit:
            fail(f"时长超过模板上限 {limit:g} 秒。")
        if not template_fill.duration_allowed(fields, float(args.seconds)):
            shown = "、".join(f"{item:g}" for item in durations)
            fail(f"时长不在白名单内。允许的值：{shown}。")
        length = template_fill.frames_for_duration(fields, float(args.seconds))
        requested = args.seconds
    else:
        length = 17 if 17 in frames else frames[0]
        requested = length
    set_inputs(prompt, fields["frames"], length)
    fps = float(fields.get("fps") or 16)
    note(f"帧数 {length}（请求 {requested}，白名单帧 {frames[0]}–{frames[-1]}，fps {fps:g}）")


def reject_rate_and_motion(fields: dict, args) -> None:
    fps_allowed = template_fill.allowed_fps(fields)
    motion_allowed = template_fill.allowed_motion(fields)
    if getattr(args, "fps", None) is not None and not template_fill.value_allowed(float(args.fps), fps_allowed):
        shown = "、".join(f"{item:g}" for item in fps_allowed)
        fail(f"帧率不在白名单内。允许的值：{shown}。")
    if getattr(args, "motion", None) is not None and not template_fill.value_allowed(float(args.motion), motion_allowed):
        shown = "、".join(f"{item:g}" for item in motion_allowed)
        fail(f"运动幅度不在白名单内。允许的值：{shown}。")


def apply_steps(prompt: dict, fields: dict, steps: int | None) -> None:
    if steps is None:
        return
    if steps < 1:
        fail("steps 必须大于 0。")
    if "steps" not in fields:
        fail("这张模板的对照表没有 steps。")
    set_inputs(prompt, fields["steps"], int(steps))
    note(f"步数 {int(steps)}")


def upload_image(base: str, path: str) -> str:
    return comfy_client.ComfyClient(base).upload_image(path)


def submit_prompt(base: str, prompt: dict, client_id: str, template: dict, fields: dict) -> str:
    template_fill.assert_template_edit(template, prompt, fields)
    client = comfy_client.ComfyClient(base, client_id=client_id)
    try:
        return client.submit(prompt)
    finally:
        client.close()


def report_event(event: dict, workflow: dict) -> None:
    kind = event.get("type")
    if kind == "progress":
        value = event.get("value")
        maximum = event.get("max") or 0
        percent = event.get("percent")
        node = event.get("node")
        if maximum:
            if _talk:
                prefix = f"节点 {node} " if node else ""
                print(f"进度 {prefix}{value}/{maximum} {percent}%", flush=True)
            elif percent is not None:
                print(f"进度 {percent}%", file=sys.stderr, flush=True)
    elif kind == "executing" and _talk:
        node = event.get("node")
        if node is None:
            note("执行结束，正在取结果")
        else:
            class_type = (workflow.get(str(node)) or {}).get("class_type") or ""
            label = f"{node}（{class_type}）" if class_type else str(node)
            note(f"执行节点 {label}")


def confirm_history_prompt(entry: dict, prompt: dict, fields: dict) -> None:
    stored = entry.get("prompt")
    graph = stored[2] if isinstance(stored, list) and len(stored) >= 3 else None
    if not isinstance(graph, dict):
        note("历史记录里没有完整工作流，提交前已经核对过提示词。")
        return
    positive = targets(fields["positive_prompt"])[0]
    node_id = str(positive["node"])
    field = positive["input"]
    actual = ((graph.get(node_id) or {}).get("inputs") or {}).get(field)
    expected = prompt[node_id]["inputs"][field]
    if actual != expected:
        fail(
            "历史记录里的提示词和本次提交不一致。",
            {"node": node_id, "input": field, "expected": expected, "actual": actual},
        )
    note(f"已核对历史记录：节点 {node_id}.{field} 与本次提示词一致。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="用一条提示词调用本地 ComfyUI，生成图片或视频。")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--prompt", required=True, help="正向提示词，会写入 CLIP 文本节点。")
        command.add_argument("--negative", default=None, help="负向提示词。不传则保留模板里的默认值。")
        command.add_argument("--aspect", default="1:1", choices=ASPECTS, help="画幅。只允许这三项。")
        command.add_argument("--seed", type=int, default=None, help="随机种子。不传则本次随机生成。")
        command.add_argument("--steps", type=int, default=None, help="采样步数。不传则保留模板里的值。")
        command.add_argument("--print-prompt", action="store_true", help="只打印替换后的工作流，不提交。")

    image = subparsers.add_parser("image", help="文生图。")
    add_common(image)
    i2i = subparsers.add_parser("i2i", help="图生图。使用 image_i2i 模板和本地参考图。")
    add_common(i2i)
    i2i.add_argument("--image", required=True, help="本地参考图。")
    video = subparsers.add_parser("video", help="文生视频。传入 --image 时改用 video_i2v 模板。")
    add_common(video)
    video.add_argument("--frames", type=int, default=None, help="帧数。只允许白名单里的 4n+1，不超过上限。")
    video.add_argument("--seconds", type=float, default=None, help="时长（秒）。只允许白名单。同时传了 --frames 时以帧数为准。")
    video.add_argument("--fps", type=float, default=None, help="帧率。只允许白名单，不传则用模板里的值。")
    video.add_argument("--motion", type=float, default=None, help="运动幅度。只允许白名单。当前模板没有对应节点，合法值不会写入工作流。")
    video.add_argument("--image", default=None, help="本地参考图。传入则使用图生视频模板。")
    subparsers.add_parser("check", help="校验模板对照表。不连接 ComfyUI。")
    return parser


def template_kind(args) -> str:
    if args.command == "image":
        return "image"
    if args.command == "i2i":
        return "image_i2i"
    if args.image:
        return "video_i2v"
    return "video"


def execute_workflow(
    base: str,
    template: dict,
    workflow: dict,
    fields: dict,
    timeout: int,
    output_dir: str,
    on_event=None,
    kind: str = "image",
    preview: bool = False,
) -> list[str]:
    client = comfy_client.ComfyClient(base)
    lane = comfy_client.lane_for(kind)

    def relay(event: dict) -> None:
        report_event(event, workflow)
        if on_event is not None:
            on_event(event)

    def started(prompt_id: str) -> None:
        if on_event is not None:
            on_event({"type": "submitted", "prompt_id": prompt_id})
        stream = sys.stdout if _talk else sys.stderr
        print(f"prompt_id {prompt_id}", file=stream, flush=True)
        if client.queued(prompt_id):
            print(f"队列中 {prompt_id}", file=sys.stderr, flush=True)

    try:
        template_fill.assert_template_edit(template, workflow, fields)
        if lane == "video" and template_fill.preview_choice(preview, fields) == "skipped":
            note("预览帧已跳过：模板没有预览节点，这次仍提交成片。")
        entry = comfy_client.run_isolated(
            lane, client, workflow, timeout, on_event=relay, on_submitted=started,
        )
        confirm_history_prompt(entry, workflow, fields)
        return client.save_outputs(entry, output_dir)
    finally:
        client.close()


def main(argv: list[str] | None = None) -> None:
    configure_stdio()
    saved = None
    try:
        saved = _main(argv)
    except template_fill.TemplateError as exc:
        _exit_failure(exc.message, exc.payload)
    except artifacts.ArtifactError as exc:
        _exit_failure(exc.message, None)
    except ComfyFailure as exc:
        _exit_failure(exc.message, exc.payload)
    for path in (saved or {}).get("paths") or []:
        print(f"输出 {path}", flush=True)
    for record in (saved or {}).get("records") or []:
        print(f"artifact_id {record['artifact_id']}", flush=True)
        print(f"url {record['url']}", flush=True)


def _exit_failure(message: str, payload) -> None:
    print(message, file=sys.stderr)
    if payload is not None:
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=sys.stderr)
    raise SystemExit(1)


def _main(argv: list[str] | None) -> dict | None:
    args = build_parser().parse_args(argv)
    config = load_config()
    if args.command == "check":
        template_fill.run_check(config)
        print("template_check ok")
        return None
    template, fields, timeout = job_paths(config, template_kind(args))
    template_fill.validate_fields(template, fields)
    prompt = template_fill.copy_template(template)
    apply_common(prompt, fields, args)
    apply_steps(prompt, fields, args.steps)
    if args.command == "video":
        apply_frames(prompt, fields, args)
        reject_rate_and_motion(fields, args)
    image_path = getattr(args, "image", None)
    if image_path:
        if "reference_image" not in fields:
            raise template_fill.TemplateError("这张模板的对照表没有 reference_image，不能提交参考图。")
        uploaded = os.path.basename(image_path) if args.print_prompt else upload_image(config["comfy_url"], image_path)
        template_fill.set_reference_name(prompt, fields, uploaded)
        spec = fields["reference_image"]
        note(f"参考图写入节点 {spec['node']} 的 {spec['input']}：{uploaded}")
    elif "reference_image" in fields:
        raise template_fill.TemplateError("这张模板需要参考图，未提交。")
    template_fill.assert_template_edit(template, prompt, fields)
    if args.print_prompt:
        print(json.dumps(prompt, ensure_ascii=False, indent=2))
        return None
    paths = execute_workflow(
        config["comfy_url"],
        template,
        prompt,
        fields,
        timeout,
        config["output_dir"],
        kind=template_kind(args),
        preview=preview_enabled(config, template_kind(args)),
    )
    duration = None
    if args.command == "video":
        frame_count = int(template_fill.read_back(prompt, fields, "frames"))
        duration = frame_count / float(fields.get("fps") or 16)
    records = artifacts.remember_outputs(
        config,
        paths,
        kind=template_kind(args),
        user_prompt=args.prompt,
        workflow_prompt=template_fill.read_back(prompt, fields, "positive_prompt"),
        seed=int(template_fill.read_back(prompt, fields, "seed")),
        width=int(template_fill.read_back(prompt, fields, "width")),
        height=int(template_fill.read_back(prompt, fields, "height")),
        duration_sec=duration,
    )
    return {"paths": paths, "records": records}


if __name__ == "__main__":
    main()
