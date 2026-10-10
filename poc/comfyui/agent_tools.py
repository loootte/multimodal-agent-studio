"""模型能调用的本机生成工具。

实现放在 poc/tools/internal。这个文件保留列表、校验、调用和命令行入口。
目录里多一个符合规范的文件，这里的列表会跟着变。
工具列表里没有 ComfyUI 节点。参数先校验，再交给模板填充和客户端。
标准输出只有工具结果，没有 workflow JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import artifacts
import comfy_poc
import template_fill

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
POC_DIR = os.path.dirname(SCRIPT_DIR)
if POC_DIR not in sys.path:
    sys.path.append(POC_DIR)

from tools._support import listen_run, load_styles, public_comfy_message, tool_error
from tools.registry import call_tool, tool_names, tool_specs, validate_tool

TOOL_NAMES = tool_names()

__all__ = [
    "TOOL_NAMES",
    "call_tool",
    "generate_image",
    "generate_video",
    "listen_run",
    "load_styles",
    "public_comfy_message",
    "tool_error",
    "tool_specs",
    "validate_tool",
]


def generate_image(prompt: str, aspect_ratio: str, seed: int | None = None, style: str | None = None) -> dict:
    arguments = {"prompt": prompt, "aspect_ratio": aspect_ratio, "seed": seed, "style": style}
    return call_tool("generate_image", {key: value for key, value in arguments.items() if value is not None or key in {"prompt", "aspect_ratio"}})


def generate_video(
    prompt: str,
    duration_sec: float,
    aspect_ratio: str,
    image_ref: str | None = None,
    fps: float | None = None,
    motion: float | None = None,
) -> dict:
    arguments = {
        "prompt": prompt,
        "duration_sec": duration_sec,
        "aspect_ratio": aspect_ratio,
        "image_ref": image_ref,
        "fps": fps,
        "motion": motion,
    }
    return call_tool(
        "generate_video",
        {key: value for key, value in arguments.items() if value is not None},
    )


def configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="调用已注册的本机工具。")
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
            from tools.check import self_check

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
