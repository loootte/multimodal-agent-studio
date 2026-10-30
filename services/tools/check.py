"""本机工具的离线检查。不提交 ComfyUI。

在仓库根运行：python -m services.tools.check
（也可以 python poc/comfyui/agent_tools.py check，跑的是同一个 self_check。）
"""

from __future__ import annotations

import json
from types import SimpleNamespace
import os
import sys

# _support 会把 poc/comfyui 放上路径，下面的本机 ComfyUI 脚本才能导入。
from services.tools._support import FORBIDDEN_IN_SCHEMA, load_styles

import artifacts
import comfy_client
import comfy_poc
import template_fill

from services.tools.api import ToolSpecError
from services.tools.mcp import internal_handle as handle
from services.tools.registry import call_tool, collect, load_modules, tool_specs
import services.tools.registry as registry


def _check_discovery() -> None:
    import tempfile

    folder = tempfile.mkdtemp(prefix="tool-discover-")
    try:
        with open(os.path.join(folder, "_hidden.py"), "w", encoding="utf-8") as handle:
            handle.write("raise RuntimeError('不应装载')\n")
        with open(os.path.join(folder, "ping.py"), "w", encoding="utf-8") as handle:
            handle.write(
                "from services.tools.api import Tool\n"
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
                "from services.tools.api import Tool\n"
                "def validate(arguments):\n"
                "    return None\n"
                "def call(arguments):\n"
                "    return {'ok': True, 'tool': 'extra'}\n"
                "def tools():\n"
                "    return [Tool(name='extra', description='extra', order=10, validate=validate, call=call,\n"
                "        parameters={'type': 'object', 'additionalProperties': False, 'properties': {}})]\n"
            )
        found = collect(load_modules(folder, "_mas_probe_ok"), "internal")
        if [item.name for item in found] != ["extra", "ping"]:
            comfy_poc.fail(f"新文件没有按规范注册：{[item.name for item in found]}")
        leaked = tempfile.mkdtemp(prefix="tool-discover-bad-")
        try:
            with open(os.path.join(leaked, "leak.py"), "w", encoding="utf-8") as handle:
                handle.write(
                    "from services.tools.api import Tool\n"
                    "def tools():\n"
                    "    return [Tool(name='leak', description='leak',\n"
                    "        parameters={'type': 'object', 'additionalProperties': False,\n"
                    "            'properties': {'body': {'type': 'string'}}})]\n"
                )
            try:
                collect(load_modules(leaked, "_mas_probe_bad"), "external")
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
    _check_mcp_client()
    _check_tool_policy()
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


def _check_mcp_client() -> None:
    """14.A #78：MCP 客户端（entry.kind=mcp）。清单校验、默认停用、摘要绑定、参数黑名单、出站方法白名单、
    stdio 走沙箱、HTTP 不连回环、安装时不带启用记录、项目名不能当包名卸载。"""
    import base64
    import importlib.util
    import shutil
    import tempfile

    from services.gateway import egress
    from services.tools import mcp_client, packs, sandbox
    from services.runtime.tools import confirm_every_call, openai_tools, tool_mutates

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("mas_mcp_fake", os.path.join(root, "evals", "fixtures", "mcp_fake.py"))
    fake_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fake_mod)

    def problems(**change):
        data = fake_mod.manifest()
        for key, value in change.items():
            data[key] = value
        return packs.validate_manifest(data)

    if problems():
        comfy_poc.fail(f"合格的 HTTP MCP 清单被拒：{problems()}")
    http = fake_mod.manifest()["entry"]
    for label, change in (
        ("confirm 不是 always", {"confirm": "never"}),
        ("带 parameters", {"parameters": {"type": "object", "properties": {}}}),
        ("http 没有联网权限", {"permissions": dict(fake_mod.manifest()["permissions"], network=False)}),
        ("http:// 地址", {"entry": dict(http, url="http://203.0.113.10/mcp")}),
        ("带用户名的地址", {"entry": dict(http, url="https://u:p@203.0.113.10/mcp")}),
        ("stdio 指向包外", {"entry": {"kind": "mcp", "transport": "stdio", "module": "../x.py"}}),
        ("stdio 带 command", {"entry": {"kind": "mcp", "transport": "stdio", "module": "s.py", "command": "npx"}}),
        ("不认识的传输", {"entry": {"kind": "mcp", "transport": "ws", "url": "https://x.example/"}}),
        ("名字带 __", {"name": "bad__name"}),
        ("带 config", {"config": [{"name": "api_key", "type": "secret"}]}),
    ):
        if not problems(**change):
            comfy_poc.fail(f"MCP 清单{label}也通过了")
    if not packs.validate_manifest(fake_mod.manifest(), builtin=True):
        comfy_poc.fail("内置工具声明 kind=mcp 也通过了")

    # tools/list 投影：参数黑名单、坏 schema、坏名字都标 blocked；additionalProperties 一律 false；摘要随说明变。
    listed = [
        {"name": "ok", "description": "d", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        {"name": "leak", "description": "d", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}}},
        {"name": "bad name!", "inputSchema": {"type": "object"}},
        {"name": "noschema", "inputSchema": {"type": "array"}},
    ]
    items = mcp_client.project("mcp_unit", listed)
    if [i["blocked"] for i in items] != [None, None, "bad_name", "bad_schema"]:
        comfy_poc.fail(f"MCP 工具投影不对：{[i['blocked'] for i in items]}")
    # #93：参数黑名单改由策略决定（投影只管结构问题），默认策略下 prompt 仍然被拦。
    unit = SimpleNamespace(name="mcp_unit")
    if [mcp_client.blocked_reason(unit, i) for i in items] != [None, "blocked_param", "bad_name", "bad_schema"]:
        comfy_poc.fail(f"默认策略下参数黑名单不对：{[mcp_client.blocked_reason(unit, i) for i in items]}")
    if items[0]["schema"].get("additionalProperties") is not False or items[0]["name"] != "mcp_unit__ok":
        comfy_poc.fail(f"MCP 工具 schema 没收紧：{items[0]}")
    if mcp_client.digest_of(listed[0]) == mcp_client.digest_of(dict(listed[0], description="d2")):
        comfy_poc.fail("工具说明变了摘要没变")

    # 出站方法白名单：toolpack 通道只放 MCP 的四个方法。
    if egress.check_policy("toolpack", {"jsonrpc": "2.0", "id": 1, "method": "resources/read", "params": {}}) is None:
        comfy_poc.fail("toolpack 通道放行了白名单外的 MCP 方法")
    if egress.check_policy("toolpack", {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {}}) is not None:
        comfy_poc.fail("toolpack 通道拦了 tools/call")
    if "mcp" not in sandbox.FUNCTIONS:
        comfy_poc.fail("沙箱没有 mcp 函数")

    saved_dir = os.environ.get("MAS_TOOLPACKS_DIR")
    saved_post = mcp_client.HTTP_POST
    temp = tempfile.mkdtemp(prefix="mas-mcp-check-")
    source = tempfile.mkdtemp(prefix="mas-mcp-src-")
    os.environ["MAS_TOOLPACKS_DIR"] = temp
    try:
        fake = fake_mod.FakeMcpHttp()
        mcp_client.HTTP_POST = fake
        folder = fake_mod.write_pack(source)
        with open(os.path.join(folder, mcp_client.ENABLED_FILE), "w", encoding="utf-8") as handle:
            json.dump({"schema": "mas.mcp.enabled/1", "tools": {"lookup": {"digest": "x", "name": "mcp_fake__lookup"}}}, handle)
        packs.install(folder, ["network"])
        pack = packs.find("mcp_fake")
        if mcp_client.enabled(pack):
            comfy_poc.fail("安装把包里自带的启用记录也带进来了")
        if not packs.review(pack.manifest).get("confirm_every_call") or not packs.review(pack.manifest).get("mcp"):
            comfy_poc.fail("MCP 包审阅没有说明每次确认")
        view = mcp_client.refresh(pack)
        row = view["tools"][0]
        names = lambda: [t["function"]["name"] for t in openai_tools()]
        if row["state"] != "disabled" or "mcp_fake__lookup" in names():
            comfy_poc.fail(f"远端工具默认就启用了：{row}")
        try:
            mcp_client.enable(pack, "lookup", "0" * 64)
            comfy_poc.fail("摘要不符也启用了")
        except mcp_client.McpError as exc:
            if exc.code != "digest_mismatch":
                comfy_poc.fail(f"摘要不符的错误码不对：{exc.code}")
        mcp_client.enable(pack, "lookup", row["digest"])
        if "mcp_fake__lookup" not in names() or not confirm_every_call("mcp_fake__lookup") \
                or not tool_mutates("mcp_fake__lookup", {"query": "x"}):
            comfy_poc.fail("启用后 MCP 工具不在工具表，或者不是每次确认")
        for bad in ("mcp_fake__lookup",):
            for action in (packs.uninstall, packs.export):
                try:
                    action(bad)
                    comfy_poc.fail(f"{action.__name__} 接受了投影工具名 {bad}")
                except packs.PackError:
                    pass
        result = mcp_client.call(packs.find_tool("mcp_fake__lookup"), {"query": "abc"})
        if not result.get("ok") or result.get("trust") != "untrusted" or result.get("omitted") != 1:
            comfy_poc.fail(f"MCP 结果没标不可信或没丢非文字内容：{result}")
        # 本地正文（含 base64）在连接前拦下。
        body = "本地正文 LOCAL-MCP-UNIT-77"
        before = len(fake.calls)
        encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
        blocked = _mcp_call_with_bodies(mcp_client, egress, packs, body, {"query": encoded})
        if blocked.get("ok") or (blocked.get("error") or {}).get("code") != "egress_blocked" or len(fake.calls) != before:
            comfy_poc.fail(f"本地正文（base64）没在连接前拦下：{blocked} {fake.calls[before:]}")
        # 改了说明 -> reauth_required，tools/call 不发。
        fake.tools[0] = dict(fake.tools[0], description="变了")
        before = fake.calls.count("tools/call")
        changed = mcp_client.call(packs.find_tool("mcp_fake__lookup"), {"query": "abc"})
        if (changed.get("error") or {}).get("code") != "reauth_required" or fake.calls.count("tools/call") != before \
                or "mcp_fake__lookup" in names():
            comfy_poc.fail(f"清单变了没要求重新授权：{changed}")
        # 回环地址不连。
        os.environ.pop("MAS_TOOLPACKS_DIR", None)
        allowed, _ = sandbox.decide("mcp_fake", {"network"}, "127.0.0.1", 443)
        if allowed:
            comfy_poc.fail("MCP HTTP 能连回环地址")
        os.environ["MAS_TOOLPACKS_DIR"] = temp
        # stdio：示例 echo 在沙箱子进程里跑。
        packs.install(os.path.join(root, "toolpacks", "examples", "mcp_echo"), ["code"])
        echo = packs.find("mcp_echo")
        rows = {r["remote"]: r for r in mcp_client.refresh(echo)["tools"]}
        mcp_client.enable(echo, "echo", rows["echo"]["digest"])
        said = mcp_client.call(packs.find_tool("mcp_echo__echo"), {"message": "hi"})
        if not said.get("ok") or "hi" not in json.dumps(said, ensure_ascii=False) or said.get("trust") != "untrusted":
            comfy_poc.fail(f"stdio MCP 调用不对：{said}")
        if packs.find_tool("mcp_echo__reverse") is not None and "mcp_echo__reverse" in names():
            comfy_poc.fail("没启用的 stdio 工具进了工具表")
    finally:
        mcp_client.HTTP_POST = saved_post
        if saved_dir is None:
            os.environ.pop("MAS_TOOLPACKS_DIR", None)
        else:
            os.environ["MAS_TOOLPACKS_DIR"] = saved_dir
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


def _check_tool_policy() -> None:
    """#93：工具安全策略。未配置 = 最严格；坏值（整个字段）回默认并给 warnings；tools > packs > global；地址分类；
    放宽要在安装 / 启用时确认；策略变了已启用的要重新确认；认证头的值不出现在结果、记录和策略文件里。"""
    import shutil
    import sys
    import tempfile

    from services.tools import mcp_client, packs, policy

    fake_mod = _mcp_fake()
    # 1) 默认 + 校验
    base = policy.resolve("p", "p__t", {"global": {}, "packs": {}, "tools": {}})
    if base["stdio_commands"] or base["http_loopback"] or base["http_link_local"] or base["http_private"] \
            or base["auth"] is not None or base["confirm"] != "manifest" or "prompt" not in base["blocked_params"]:
        comfy_poc.fail(f"默认策略不是最严格：{base}")
    normalized, warnings = policy.normalize({
        "global": {"http_private": "yes", "local_body_check": "off", "remote_local_prompt_default": "allow",
                   "stdio_commands": ["npx", "../evil", "a b"], "result_max_chars": 10**9, "nope": 1},
        "packs": {"Bad Name": {}, "ok_pack": {"auth": {"source": "env", "env": "OPENAI_API_KEY", "header": "Authorization"}}},
        "tools": {"ok_pack__t": {"stdio_commands": ["npx"]}}})
    for path in ("global.http_private", "global.local_body_check", "global.remote_local_prompt_default",
                 "global.stdio_commands", "global.result_max_chars", "global.nope", "packs.ok_pack.auth.env",
                 "tools.ok_pack__t.stdio_commands"):
        if path not in warnings:
            comfy_poc.fail(f"坏值 {path} 没给 warning：{warnings}")
    if "stdio_commands" in normalized["global"] or "http_private" in normalized["global"] \
            or "local_body_check" in normalized["global"] or normalized["packs"].get("ok_pack", {}).get("auth"):
        comfy_poc.fail(f"坏值没回默认：{normalized}")
    for key in ("local_body_check", "remote_local_prompt_default", "send_local_to_model"):
        if key not in policy.LOCKED:
            comfy_poc.fail(f"底线 {key} 不在锁定项里")
    # 2) 优先级 tools > packs > global
    layered = {"global": {"confirm": "always", "params_block": ["q"], "result_max_chars": 8000},
               "packs": {"p": {"params_allow": ["prompt"], "result_max_chars": 4000}},
               "tools": {"p__t": {"params_block": ["prompt"], "params_allow": ["q"], "result_max_chars": 1000}}}
    tool_level = policy.resolve("p", "p__t", layered)
    pack_level = policy.resolve("p", "p__other", layered)
    global_level = policy.resolve("x", "x__t", layered)
    if tool_level["result_max_chars"] != 1000 or pack_level["result_max_chars"] != 4000 \
            or global_level["result_max_chars"] != 8000 or global_level["confirm"] != "always":
        comfy_poc.fail("策略覆盖顺序不对（result_max_chars / confirm）")
    if "prompt" not in tool_level["blocked_params"] or "q" in tool_level["blocked_params"] \
            or "prompt" in pack_level["blocked_params"] or "q" not in pack_level["blocked_params"] \
            or "prompt" not in global_level["blocked_params"]:
        comfy_poc.fail("参数黑名单的覆盖顺序不对")
    if policy.digest(tool_level) == policy.digest(pack_level):
        comfy_poc.fail("策略不同，指纹一样")
    # 3) 地址分类：默认回环 / 链路本地 / 局域网都拒；放开哪类只放哪类；本程序端口和 0.0.0.0 永远拒。
    for host in ("127.0.0.1", "::1", "localhost", "169.254.169.254", "fe80::1", "10.0.0.5", "192.168.1.2",
                 "172.16.0.1", "fd00::1", "0.0.0.0", "::ffff:127.0.0.1"):
        if policy.address_decision(base, host, 443)[0]:
            comfy_poc.fail(f"默认策略放行了 {host}")
    if not policy.address_decision(base, "203.0.113.10", 443)[0]:
        comfy_poc.fail("默认策略拦了公网地址")
    lan = dict(base, http_private=True)
    if not policy.address_decision(lan, "192.168.1.2", 443)[0] or policy.address_decision(lan, "127.0.0.1", 443)[0] \
            or policy.address_decision(lan, "169.254.169.254", 443)[0]:
        comfy_poc.fail("只放开局域网时分类不对")
    saved_ports = set(policy.HOST_PORTS)
    policy.HOST_PORTS.add(48765)
    loop = dict(base, http_loopback=True)
    if not policy.address_decision(loop, "127.0.0.1", 8188)[0] or policy.address_decision(loop, "127.0.0.1", 48765)[0] \
            or policy.address_decision(loop, "0.0.0.0", 8188)[0]:
        comfy_poc.fail("放开回环后，本程序端口或 0.0.0.0 也能连")
    policy.HOST_PORTS.clear()
    policy.HOST_PORTS.update(saved_ports)
    if policy.scrub({"a": ["x SECRET-1 y"]}, ["SECRET-1"]) != {"a": ["x [已隐去] y"]}:
        comfy_poc.fail("scrub 没隐去密钥")

    # 4) 端到端：命令形式的 stdio、回环 HTTP、认证头。
    saved = {k: os.environ.get(k) for k in ("MAS_TOOLPACKS_DIR", "MAS_MCP_CHECK_TOKEN")}
    saved_path, saved_post = policy.PATH, mcp_client.HTTP_POST
    temp = tempfile.mkdtemp(prefix="mas-policy-check-")
    source = tempfile.mkdtemp(prefix="mas-policy-src-")
    os.environ["MAS_TOOLPACKS_DIR"] = os.path.join(temp, "installed")
    policy.PATH = os.path.join(temp, "tool_policy.json")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    command = os.path.splitext(os.path.basename(sys.executable))[0]
    saved_env_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.path.dirname(sys.executable) + os.pathsep + saved_env_path
    try:
        # confirm：策略只能收紧成每次确认（对非 MCP 工具同样生效）
        policy.save({"tools": {"some_tool": {"confirm": "always"}}, "global": {"confirm": "never"}})
        if not packs.policy_confirm_always(SimpleNamespace(name="some_tool")) \
                or packs.policy_confirm_always(SimpleNamespace(name="other_tool")) \
                or "global.confirm" not in policy.normalize({"global": {"confirm": "never"}})[1]:
            comfy_poc.fail("confirm 策略不对（只能收紧成 always）")
        policy.save({})
        from services.runtime import injection

        with injection.tool_scope("evil_tool"):
            for attempt in (lambda: policy.save({"global": {"http_private": True}}), policy.reset):
                try:
                    attempt()
                    comfy_poc.fail("工具执行期间写策略没被拒")
                except injection.ConfigWriteDenied:
                    pass
        if policy.current()["global"]:
            comfy_poc.fail("工具执行期间的策略写入生效了")
        folder = os.path.join(source, "mcp_cmd")
        os.makedirs(folder)
        shutil.copy(os.path.join(root, "toolpacks", "examples", "mcp_echo", "server.py"), folder)
        data = json.load(open(os.path.join(root, "toolpacks", "examples", "mcp_echo", "tool.json"), encoding="utf-8"))
        data.update(name="mcp_cmd", entry={"kind": "mcp", "transport": "stdio", "command": command,
                                           "args": ["-B", "-u", "server.py"]})
        with open(os.path.join(folder, "tool.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        summary = packs.inspect(folder)
        if not summary.get("policy_problems"):
            comfy_poc.fail("默认策略下外部命令没被标成策略不允许")
        try:
            packs.install(folder, summary["accept"])
            comfy_poc.fail("默认策略下外部命令也装上了")
        except packs.PackError as exc:
            if exc.code != "policy_denied":
                comfy_poc.fail(f"外部命令被拒的错误码不对：{exc.code}")
        policy.save({"packs": {"mcp_cmd": {"stdio_commands": [command]}}})
        summary = packs.inspect(folder)
        if summary.get("policy_problems") or packs.ACCEPT_COMMAND not in summary["accept"] \
                or packs.ACCEPT_POLICY not in summary["accept"]:
            comfy_poc.fail(f"放开命令后审阅没要求确认外部命令 / 放宽项：{summary.get('accept')}")
        try:
            packs.install(folder, [a for a in summary["accept"] if a != packs.ACCEPT_POLICY])
            comfy_poc.fail("没确认放宽项也装上了")
        except packs.PackError:
            pass
        packs.install(folder, summary["accept"])
        pack = packs.find("mcp_cmd")
        rows = {r["remote"]: r for r in mcp_client.refresh(pack)["tools"]}
        try:
            mcp_client.enable(pack, "echo", rows["echo"]["digest"])
            comfy_poc.fail("没确认放宽项也启用了")
        except mcp_client.AcknowledgeRequired as exc:
            if "stdio_commands" not in exc.fields:
                comfy_poc.fail(f"启用时要确认的放宽项不对：{exc.fields}")
        mcp_client.enable(pack, "echo", rows["echo"]["digest"], acknowledge=["stdio_commands"])
        said = mcp_client.call(packs.find_tool("mcp_cmd__echo"), {"message": "hi-cmd"})
        if not said.get("ok") or "hi-cmd" not in json.dumps(said, ensure_ascii=False):
            comfy_poc.fail(f"命令形式的 stdio MCP 调用不对：{said}")
        # 策略变了（即使是收紧） -> 已启用的要重新确认
        policy.save({"packs": {"mcp_cmd": {"stdio_commands": [command], "result_max_chars": 2000}}})
        again = mcp_client.call(packs.find_tool("mcp_cmd__echo"), {"message": "hi"}) if packs.find_tool("mcp_cmd__echo") \
            else {"error": {"code": "reauth_required"}}
        if (again.get("error") or {}).get("code") != "reauth_required":
            comfy_poc.fail(f"策略变了，已启用的工具没要求重新确认：{again}")
        state = {r["remote"]: r for r in mcp_client.view(pack)["tools"]}["echo"]
        if state["state"] != "changed" or state.get("changed_reason") != "policy_changed":
            comfy_poc.fail(f"策略变了，状态没标出来：{state}")
        # 去掉命令放行 -> 包整体不可用
        policy.save({})
        if packs.find_tool("mcp_cmd__echo") is not None and mcp_client.model_tools(pack):
            comfy_poc.fail("撤掉命令放行后工具还在")

        # HTTP：回环地址（http://127.0.0.1）+ 环境变量认证头
        token = "tok-CHECK-" + "9" * 24
        os.environ["MAS_MCP_CHECK_TOKEN"] = token
        fake = fake_mod.FakeMcpHttp("ok")
        fake.echo_auth = True
        mcp_client.HTTP_POST = fake
        hfolder = fake_mod.write_pack(source, "mcp_loop", "http://127.0.0.1:18080/mcp")
        if not packs.inspect(hfolder).get("policy_problems"):
            comfy_poc.fail("默认策略下回环 MCP 地址没被标出来")
        policy.save({"packs": {"mcp_loop": {"http_loopback": True, "auth": {
            "source": "env", "env": "MAS_MCP_CHECK_TOKEN", "header": "Authorization", "scheme": "bearer"}}}})
        summary = packs.inspect(hfolder)
        packs.install(hfolder, summary["accept"])
        loop_pack = packs.find("mcp_loop")
        rows = {r["remote"]: r for r in mcp_client.refresh(loop_pack)["tools"]}
        if fake.headers[0].get("Authorization") != "Bearer " + token:
            comfy_poc.fail("认证头没注入")
        relax = sorted(rows["lookup"].get("relaxations") or [])
        mcp_client.enable(loop_pack, "lookup", rows["lookup"]["digest"], acknowledge=relax)
        result = mcp_client.call(packs.find_tool("mcp_loop__lookup"), {"query": "abc"})
        if not result.get("ok") or token in json.dumps(result, ensure_ascii=False):
            comfy_poc.fail(f"MCP 结果里带出了认证密钥：{result}")
        public = json.dumps(policy.public(), ensure_ascii=False)
        stored = open(policy.PATH, encoding="utf-8").read()
        records = ""
        for base_dir, _, files in os.walk(temp):
            for name in files:
                try:
                    records += open(os.path.join(base_dir, name), encoding="utf-8", errors="ignore").read()
                except OSError:
                    pass
        if token in public or token in stored or token in records:
            comfy_poc.fail("认证密钥写进了策略 / 记录 / 接口输出")
        os.environ.pop("MAS_MCP_CHECK_TOKEN")
        failed = mcp_client.call(packs.find_tool("mcp_loop__lookup"), {"query": "abc"})
        if failed.get("ok"):
            comfy_poc.fail("取不到密钥时也调用成功了")
    finally:
        os.environ["PATH"] = saved_env_path
        policy.PATH, mcp_client.HTTP_POST = saved_path, saved_post
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(source, ignore_errors=True)


def _mcp_fake():
    import importlib.util

    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("mas_mcp_fake_policy", os.path.join(root, "evals", "fixtures", "mcp_fake.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _mcp_call_with_bodies(mcp_client, egress, packs, body: str, arguments: dict) -> dict:
    """在一条登记了本地正文的运行里调 MCP 工具（模拟网页 chat_run 的 egress.attach）。"""
    from services import telemetry

    trace_id = "t_mcp_unit"
    previous = telemetry.current()
    egress.attach(trace_id, session_id="s_mcp_unit", local_bodies=lambda: egress.LocalBodies([body]))
    telemetry.bind(trace_id)
    try:
        return mcp_client.call(packs.find_tool("mcp_fake__lookup"), arguments)
    finally:
        telemetry.bind(previous)
        egress.detach(trace_id)


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
    job = registry.module("generate_video").fields()
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


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    # 离线检查只读模板，不连 ComfyUI；没有本机 config.json 时用仓库里的示例配置。
    if not os.environ.get("COMFY_CONFIG"):
        from services.tools._support import COMFY_DIR

        if not os.path.isfile(os.path.join(COMFY_DIR, "config.json")):
            os.environ["COMFY_CONFIG"] = os.path.join(COMFY_DIR, "config.example.json")
    self_check()
    print("tools check ok")


if __name__ == "__main__":
    main()
