"""本机用途：文生图。参数里有画面描述，校验通过后才提交。"""

from __future__ import annotations

import artifacts
import comfy_client
import comfy_poc
import template_fill

import tools._support as common


def spec(aspects: list[str], styles: list[str]) -> dict:
    return {
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
    }


def validate(arguments: dict) -> dict | None:
    tool = "generate_image"
    problem = common.unexpected(tool, arguments) or common.require_prompt(tool, arguments)
    if problem:
        return problem
    styles = common.load_styles()
    aspect, problem = common.parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    if "seed" in arguments and arguments["seed"] is not None:
        _seed, problem = common.parse_seed(tool, arguments)
        if problem:
            return problem
    _style, problem = common.parse_style(tool, arguments, styles)
    if problem:
        return problem
    config = comfy_poc.load_config()
    _template, fields, _timeout = template_fill.load_job(config, "image")
    if aspect not in (fields.get("aspects") or {}):
        return common.tool_error(tool, "invalid_aspect_ratio", "这张文生图模板没有这个画幅。", "aspect_ratio")
    return None


def generate(arguments: dict) -> dict:
    tool = "generate_image"
    problem = common.unexpected(tool, arguments) or common.require_prompt(tool, arguments)
    if problem:
        return problem
    styles = common.load_styles()
    aspect, problem = common.parse_aspect(tool, arguments, set(comfy_poc.ASPECTS))
    if problem:
        return problem
    seed, problem = common.parse_seed(tool, arguments)
    if problem:
        return problem
    style, problem = common.parse_style(tool, arguments, styles)
    if problem:
        return problem
    config = comfy_poc.load_config()
    template, fields, timeout = template_fill.load_job(config, "image")
    if aspect not in (fields.get("aspects") or {}):
        return common.tool_error(tool, "invalid_aspect_ratio", "这张文生图模板没有这个画幅。", "aspect_ratio")
    negative = styles[style] if style else None
    workflow = template_fill.copy_template(template)
    template_fill.fill_common(workflow, fields, arguments["prompt"], aspect, seed, negative)
    problem = common.values_match(tool, workflow, fields, arguments["prompt"], aspect, seed)
    if problem:
        problem["tool"] = tool
        return problem
    if style and template_fill.read_back(workflow, fields, "negative_prompt") != styles[style]:
        return common.tool_error(tool, "internal_error", "风格句没有写进模板。")
    template_fill.assert_template_edit(template, workflow, fields)
    files = comfy_client.run(
        config["comfy_url"], template, workflow, fields, timeout, config["output_dir"], on_event=common.current_listener(),
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
    return common.published_result(tool, records, {"aspect_ratio": aspect, "style": style})


def tools():
    from tools.api import Tool

    styles = sorted(common.load_styles())
    aspects = list(comfy_poc.ASPECTS)
    spec_body = spec(aspects, styles)
    return [Tool(
        name=spec_body["name"],
        description=spec_body["description"],
        parameters=spec_body["parameters"],
        order=10,
        validate=validate,
        call=generate,
    )]
