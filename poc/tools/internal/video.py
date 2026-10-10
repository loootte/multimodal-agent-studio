"""本机用途：文生视频和图生视频。时长、帧率和运动幅度先过白名单。"""

from __future__ import annotations

import json
import os

import artifacts
import comfy_client
import comfy_poc
import template_fill

import tools._support as common


def fields() -> dict:
    path = os.path.join(common.SCRIPT_DIR, "workflows", "video.fields.json")
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def spec(aspects: list[str]) -> dict:
    job = fields()
    return {
        "name": "generate_video",
        "description": "根据画面描述生成一段视频。传入 artifact_id 时走图生视频，否则走文生视频。",
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["prompt", "duration_sec", "aspect_ratio"],
            "properties": {
                "prompt": {"type": "string", "minLength": 1, "description": "画面描述。"},
                "duration_sec": {
                    "type": "number",
                    "enum": template_fill.allowed_durations(job),
                    "description": "时长（秒）。只能是白名单里的值，超出或对不齐都不会提交。",
                },
                "aspect_ratio": {"type": "string", "enum": aspects, "description": "画幅，只能是白名单里的值。"},
                "image_ref": {"type": "string", "description": "可选。上一张图的 artifact_id。传入则图生视频，不传则文生视频。"},
                "fps": {
                    "type": "number",
                    "enum": template_fill.allowed_fps(job),
                    "description": "可选。帧率，只能是白名单里的值。不传则用模板帧率。",
                },
                "motion": {
                    "type": "number",
                    "enum": template_fill.allowed_motion(job),
                    "description": "可选。运动幅度，只能是白名单里的值。当前模板没有对应节点，合法值不会写入工作流。",
                },
            },
        },
    }


def validate(arguments: dict) -> dict | None:
    tool = "generate_video"
    problem = common.unexpected(tool, arguments) or common.require_prompt(tool, arguments)
    if problem:
        return problem
    if "duration_sec" not in arguments:
        return common.tool_error(tool, "invalid_duration", "duration_sec 是必填项。", "duration_sec")
    duration = arguments["duration_sec"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return common.tool_error(tool, "invalid_duration", "duration_sec 必须是大于 0 的秒数。", "duration_sec")
    aspect, problem = common.parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    image_ref = arguments.get("image_ref")
    config = comfy_poc.load_config()
    if image_ref is not None and (not isinstance(image_ref, str) or not image_ref.strip()):
        return common.tool_error(tool, "image_not_found", "image_ref 必须是 artifact_id。", "image_ref")
    kind = "video_i2v" if image_ref else "video"
    _template, job, _timeout = template_fill.load_job(config, kind)
    if aspect not in (job.get("aspects") or {}):
        return common.tool_error(tool, "invalid_aspect_ratio", "这张视频模板没有这个画幅。", "aspect_ratio")
    problem = _policy(arguments, job)
    if problem:
        return problem
    if image_ref is not None:
        try:
            artifacts.resolve_image_ref(config, image_ref)
        except artifacts.ArtifactError as exc:
            return common.tool_error(tool, "image_not_found", exc.message, "image_ref")
    return None


def generate(arguments: dict) -> dict:
    tool = "generate_video"
    problem = common.unexpected(tool, arguments) or common.require_prompt(tool, arguments)
    if problem:
        return problem
    if "duration_sec" not in arguments:
        return common.tool_error(tool, "invalid_duration", "duration_sec 是必填项。", "duration_sec")
    duration = arguments["duration_sec"]
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or duration <= 0:
        return common.tool_error(tool, "invalid_duration", "duration_sec 必须是大于 0 的秒数。", "duration_sec")
    aspect, problem = common.parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    seed, problem = common.parse_seed(tool, arguments)
    if problem:
        return problem
    image_ref = arguments.get("image_ref")
    config = comfy_poc.load_config()
    if image_ref is not None and (not isinstance(image_ref, str) or not image_ref.strip()):
        return common.tool_error(tool, "image_not_found", "image_ref 必须是 artifact_id。", "image_ref")
    kind = "video_i2v" if image_ref else "video"
    template, job, timeout = template_fill.load_job(config, kind)
    if aspect not in (job.get("aspects") or {}):
        return common.tool_error(tool, "invalid_aspect_ratio", "这张视频模板没有这个画幅。", "aspect_ratio")
    problem = _policy(arguments, job)
    if problem:
        return problem
    local_image = None
    ref_id = None
    if image_ref is not None:
        try:
            local_image, ref_id = artifacts.resolve_image_ref(config, image_ref)
        except artifacts.ArtifactError as exc:
            return common.tool_error(tool, "image_not_found", exc.message, "image_ref")
    frames = template_fill.frames_for_duration(job, float(duration))
    workflow = template_fill.copy_template(template)
    template_fill.fill_common(workflow, job, arguments["prompt"], aspect, seed, None)
    template_fill.fill_frames(workflow, job, frames)
    if local_image:
        template_fill.assert_image_to_video(workflow, job)
        uploaded = comfy_client.upload(config["comfy_url"], local_image)
        template_fill.set_reference_name(workflow, job, uploaded)
        template_fill.assert_image_to_video(workflow, job)
        mode = "image_to_video"
    else:
        template_fill.assert_text_to_video(workflow, job)
        mode = "text_to_video"
    problem = common.values_match(tool, workflow, job, arguments["prompt"], aspect, seed, frames)
    if problem:
        return problem
    template_fill.assert_template_edit(template, workflow, job)
    files = comfy_client.run(
        config["comfy_url"],
        template,
        workflow,
        job,
        timeout,
        config["output_dir"],
        on_event=common.current_listener(),
        kind=kind,
        preview=comfy_poc.preview_enabled(config, kind),
    )
    records = artifacts.remember_outputs(
        config,
        files,
        kind=kind,
        user_prompt=arguments["prompt"],
        workflow_prompt=template_fill.read_back(workflow, job, "positive_prompt"),
        seed=int(template_fill.read_back(workflow, job, "seed")),
        width=int(template_fill.read_back(workflow, job, "width")),
        height=int(template_fill.read_back(workflow, job, "height")),
        duration_sec=float(frames) / float(job.get("fps") or 16),
    )
    extra = {"aspect_ratio": aspect, "frames": frames, "mode": mode}
    if ref_id:
        extra["image_ref"] = ref_id
    return common.published_result(tool, records, extra)


def _shown(values: list[float]) -> str:
    return "、".join(f"{item:g}" for item in values)


def _optional_number(arguments: dict, key: str, allowed: list[float], code: str, label: str) -> dict | None:
    if key not in arguments or arguments[key] is None:
        return None
    value = arguments[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not template_fill.value_allowed(float(value), allowed):
        return common.tool_error("generate_video", code, f"{label}不在白名单内。允许的值：{_shown(allowed)}。", key)
    return None


def _policy(arguments: dict, job: dict) -> dict | None:
    tool = "generate_video"
    duration = float(arguments["duration_sec"])
    limit = template_fill.max_duration_sec(job)
    if duration > limit:
        return common.tool_error(tool, "duration_too_long", f"duration_sec 超过模板上限 {limit:g} 秒。", "duration_sec")
    if not template_fill.duration_allowed(job, duration):
        return common.tool_error(
            tool,
            "invalid_duration",
            f"duration_sec 不在白名单内。允许的值：{_shown(template_fill.allowed_durations(job))}。",
            "duration_sec",
        )
    problem = _optional_number(arguments, "fps", template_fill.allowed_fps(job), "invalid_fps", "帧率")
    if problem:
        return problem
    return _optional_number(arguments, "motion", template_fill.allowed_motion(job), "invalid_motion", "运动幅度")


def tools():
    from tools.api import Tool

    aspects = list(comfy_poc.ASPECTS)
    spec_body = spec(aspects)
    return [Tool(
        name=spec_body["name"],
        description=spec_body["description"],
        parameters=spec_body["parameters"],
        order=20,
        validate=validate,
        call=generate,
    )]
