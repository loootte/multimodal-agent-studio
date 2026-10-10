"""本机工具的离线检查。不提交 ComfyUI。"""

from __future__ import annotations

import json
import os

import artifacts
import comfy_client
import comfy_poc
import template_fill

from tools._support import FORBIDDEN_IN_SCHEMA, load_styles
from tools.api import ToolSpecError
from tools.mcp import internal_handle as handle
from tools.registry import call_tool, collect, load_modules, tool_specs
import tools.registry as registry


def _check_discovery() -> None:
    import tempfile

    folder = tempfile.mkdtemp(prefix="tool-discover-")
    try:
        with open(os.path.join(folder, "_hidden.py"), "w", encoding="utf-8") as handle:
            handle.write("raise RuntimeError('不应装载')\n")
        with open(os.path.join(folder, "ping.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "from tools.api import Tool\n"
                "def validate(arguments):\n"
                "    return None\n"
                "def call(arguments):\n"
                "    return {'ok': True, 'tool': 'ping'}\n"
                "def tools():\n"
                "    return [Tool(name='ping', description='ping', order=20, validate=validate, call=call,\n"
                "        parameters={'type': 'object', 'additionalProperties': False, 'properties': {}})]\n"
            )
        with open(os.path.join(folder, "extra.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "from tools.api import Tool\n"
                "def validate(arguments):\n"
                "    return None\n"
                "def call(arguments):\n"
                "    return {'ok': True, 'tool': 'extra'}\n"
                "def tools():\n"
                "    return [Tool(name='extra', description='extra', order=10, validate=validate, call=call,\n"
                "        parameters={'type': 'object', 'additionalProperties': False, 'properties': {}})]\n"
            )
        found = collect(load_modules(folder, "tools._probe_ok"), "internal")
        if [item.name for item in found] != ["extra", "ping"]:
            comfy_poc.fail(f"新文件没有按规范注册：{[item.name for item in found]}")
        leaked = tempfile.mkdtemp(prefix="tool-discover-bad-")
        try:
            with open(os.path.join(leaked, "leak.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "from tools.api import Tool\n"
                    "def tools():\n"
                    "    return [Tool(name='leak', description='leak',\n"
                    "        parameters={'type': 'object', 'additionalProperties': False,\n"
                    "            'properties': {'body': {'type': 'string'}}})]\n"
                )
            try:
                collect(load_modules(leaked, "tools._probe_bad"), "external")
            except ToolSpecError as exc:
                if "body" not in exc.message:
                    comfy_poc.fail(f"外部工具的正文参数没有被规范拒绝：{exc.message}")
            else:
                comfy_poc.fail("外部工具带了正文参数，规范没有拒绝。")
        finally:
            import shutil
            shutil.rmtree(leaked, ignore_errors=True)
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


def self_check() -> None:
    _check_discovery()
    specs = tool_specs()
    names = [item["name"] for item in specs]
    if "generate_image" not in names or "generate_video" not in names:
        comfy_poc.fail(f"工具列表不对：{names}")
    if names.index("generate_image") > names.index("generate_video"):
        comfy_poc.fail(f"工具顺序不对：{names}")
    image_spec = next(item for item in specs if item["name"] == "generate_image")
    if "prompt" not in image_spec["parameters"]["required"]:
        comfy_poc.fail("本机 generate_image 应要求 prompt。")
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
    _check_mcp(specs)
    _check_video_whitelist()
    artifacts.self_check()
    _check_artifact_handoff()


def _check_mcp(specs: list[dict]) -> None:
    listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = (listed or {}).get("result", {}).get("tools")
    if [item["name"] for item in tools or []] != [item["name"] for item in specs]:
        comfy_poc.fail(f"MCP 工具列表不对：{tools}")
    for spec, tool in zip(specs, tools):
        if tool["description"] != spec["description"] or tool["inputSchema"] != spec["parameters"]:
            comfy_poc.fail("MCP inputSchema 和本机工具参数不一致。")
    published = json.dumps(listed, ensure_ascii=False)
    for word in FORBIDDEN_IN_SCHEMA:
        if word in published:
            comfy_poc.fail(f"MCP schema 里出现了 {word}")
    direct = call_tool("generate_image", {"prompt": "  ", "aspect_ratio": "1:1"})
    via = handle({
        "jsonrpc": "2.0",
        "id": "a",
        "method": "tools/call",
        "params": {"name": "generate_image", "arguments": {"prompt": "  ", "aspect_ratio": "1:1"}},
    })
    if not via or via.get("id") != "a" or via.get("result", {}).get("isError") is not True:
        comfy_poc.fail(f"本机 MCP 没有执行失败的调用：{via}")
    body = json.loads(via["result"]["content"][0]["text"])
    if body.get("error", {}).get("code") != direct["error"]["code"]:
        comfy_poc.fail("本机 MCP 的错误码和 call_tool 不一致。")
    missing = handle({"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    if not missing or missing.get("error", {}).get("code") != -32601:
        comfy_poc.fail(f"未知 MCP 方法没有拒绝：{missing}")


def _check_video_whitelist() -> None:
    seen = {"run": 0, "upload": 0}

    def fake_upload(*_args, **_kwargs):
        seen["upload"] += 1
        return "ref.png"

    def fake_run(*_args, **_kwargs):
        seen["run"] += 1
        return []

    original_upload = comfy_client.upload
    original_run = comfy_client.run
    comfy_client.upload = fake_upload
    comfy_client.run = fake_run
    try:
        rejected = [
            call_tool("generate_video", {"prompt": "teapot", "duration_sec": 1.2, "aspect_ratio": "16:9"}),
            call_tool("generate_video", {"prompt": "teapot", "duration_sec": 1, "aspect_ratio": "16:9", "fps": 24}),
            call_tool("generate_video", {"prompt": "teapot", "duration_sec": 1, "aspect_ratio": "16:9", "motion": 2}),
        ]
    finally:
        comfy_client.upload = original_upload
        comfy_client.run = original_run
    codes = [item["error"]["code"] for item in rejected]
    if codes != ["invalid_duration", "invalid_fps", "invalid_motion"]:
        comfy_poc.fail(f"视频白名单错误码不对：{codes}")
    if seen["run"] or seen["upload"]:
        comfy_poc.fail("不在白名单的视频参数仍然提交了 ComfyUI。")
    if comfy_poc.preview_enabled({"video": {}}, "video") or comfy_poc.preview_enabled({"video": {"preview": False}}, "video"):
        comfy_poc.fail("视频预览默认应关闭。")
    if not comfy_poc.preview_enabled({"video": {"preview": True}}, "video"):
        comfy_poc.fail("显式打开的视频预览没有被认出来。")
    job = registry.module("internal", "video").fields()
    if template_fill.preview_choice(True, job) != "skipped":
        comfy_poc.fail("没有预览节点时打开预览应跳过，而不是失败。")


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
    with open(source, "wb") as file_handle:
        file_handle.write(png)
    with open(rendered, "wb") as file_handle:
        file_handle.write(artifacts._mp4(0, 512, 288, 16, 17))
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
        with open(path, "rb") as file_handle:
            seen["upload"] = file_handle.read()
        return "ref.png"

    def fake_run(*args, **_kwargs):
        workflow = args[2]
        job = args[3]
        seen["load_image"] = template_fill.read_back(workflow, job, "reference_image")
        seen["prompt"] = template_fill.read_back(workflow, job, "positive_prompt")
        node_id = str(job["reference_image"]["node"])
        seen["class_type"] = workflow[node_id]["class_type"]
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
        if seen.get("load_image") != "ref.png" or seen.get("class_type") != "LoadImage":
            comfy_poc.fail(f"LoadImage 没有指向这次上传的文件：{seen}")
        if seen.get("prompt") != "the camera slowly pushes in":
            comfy_poc.fail("图生视频的提示词没有留在文本节点里。")
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
