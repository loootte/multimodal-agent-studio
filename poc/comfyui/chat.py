"""同一条助手消息上的工具、进度、成片和错误。

事件顺序固定为 tool_call、progress、artifact 或 error。
断线后按 run_id 接回同一次运行，不再提交 ComfyUI。
卡片只引用 artifact_id。这不是网页，也不做队列隔离。
"""

from __future__ import annotations

import argparse
import json
import os
import uuid

import agent_tools
import artifacts
import comfy_poc

EVENT_TYPES = ("tool_call", "progress", "artifact", "error")
ACTIONS = ("regenerate", "change_aspect", "use_as_reference")


class ChatError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class StreamDisconnected(Exception):
    """流式通道断了。这次运行保持未完成，供以后接回。"""


def open_transcript(config: dict | None = None) -> "Transcript":
    if config is None:
        config = comfy_poc.load_config()
    store, session_id = artifacts.open_store(config)
    return Transcript(store.root, session_id)


class Transcript:
    def __init__(self, root: str, session_id: str):
        self.root = os.path.abspath(root)
        self.session_id = artifacts.check_token(session_id, "会话")
        self.path = os.path.join(self.root, "sessions", self.session_id, "transcript.json")
        self._load()

    def messages(self) -> list[dict]:
        return list(self.payload["messages"])

    def get(self, message_id: str) -> dict:
        for message in self.payload["messages"]:
            if message.get("message_id") == message_id:
                return message
        raise ChatError("找不到这条消息。")

    def attach(self, run_id: str) -> dict:
        """读回同一次运行。不提交 ComfyUI。"""
        self._load()
        for message in self.payload["messages"]:
            if message.get("run_id") == run_id:
                return message
        raise ChatError("找不到这次运行。")

    def run_tool(self, tool: str, arguments: dict) -> dict:
        problem = agent_tools.validate_tool(tool, arguments)
        if problem is None and tool == "generate_video":
            problem = _reference_id(arguments)
        if problem:
            return self._fail_new(problem)
        message = self._open_assistant()
        self._append(message["message_id"], {
            "type": "tool_call",
            "tool": tool,
            "arguments": _public_arguments(arguments),
        })
        try:
            with agent_tools.listen_run(lambda event: self._on_run_event(message["message_id"], event)):
                result = agent_tools.call_tool(tool, arguments)
        except StreamDisconnected:
            self._save()
            return self.get(message["message_id"])
        if not result.get("ok"):
            error = result.get("error") or {}
            self._append(message["message_id"], {
                "type": "error",
                "code": str(error.get("code") or "comfyui_error"),
                "message": str(error.get("message") or "生成失败。"),
            })
            return self.get(message["message_id"])
        self._append(message["message_id"], _card(self.session_id, result))
        return self.get(message["message_id"])

    def resume(self, run_id: str, follow) -> dict:
        """接回未完成的运行。follow 只能读这次的 prompt_id，不能再提交。"""
        message = self.attach(run_id)
        if message["status"] != "streaming":
            return message
        prompt_id = message.get("prompt_id")
        if not prompt_id:
            raise ChatError("这次运行还没有提交，不能接回。")

        def note(event: dict) -> None:
            if event.get("type") == "submitted":
                raise ChatError("接回时不能再次提交。")
            if event.get("type") == "progress":
                self._on_run_event(message["message_id"], event)

        try:
            result = follow(prompt_id, note)
        except StreamDisconnected:
            self._save()
            return self.get(message["message_id"])
        if not isinstance(result, dict):
            raise ChatError("接回没有带回这次运行的结果。")
        if not result.get("ok"):
            error = result.get("error") or {}
            self._append(message["message_id"], {
                "type": "error",
                "code": str(error.get("code") or "comfyui_error"),
                "message": str(error.get("message") or "生成失败。"),
            })
            return self.get(message["message_id"])
        self._append(message["message_id"], _card(self.session_id, result))
        return self.get(message["message_id"])

    def apply_action(self, message_id: str, action: str, aspect_ratio: str | None = None) -> dict:
        """把卡片动作接到已有参数上。除「用作参考」外，不提交生成。"""
        self._load()
        if action not in ACTIONS:
            raise ChatError("卡片上没有这个动作。")
        message = self.get(message_id)
        tool_call = next((item for item in message.get("events") or [] if item.get("type") == "tool_call"), None)
        card = next((item for item in message.get("events") or [] if item.get("type") == "artifact"), None)
        if tool_call is None or card is None:
            raise ChatError("这次生成没有成片，不能使用卡片动作。")
        arguments = dict(tool_call.get("arguments") or {})
        tool = tool_call.get("tool")
        if action == "use_as_reference":
            artifact_id = card["artifact_id"]
            user = {
                "message_id": _message_id(),
                "role": "user",
                "text": "",
                "artifact_ids": [artifact_id],
                "image_ref": artifact_id,
            }
            self.payload["messages"].append(user)
            self._save()
            return user
        if action == "regenerate":
            arguments.pop("seed", None)
            return {"action": action, "tool": tool, "arguments": arguments}
        if not aspect_ratio:
            raise ChatError("改比例需要指定画幅。")
        if aspect_ratio not in comfy_poc.ASPECTS:
            raise ChatError("画幅只能是 1:1、16:9 或 9:16。")
        if aspect_ratio == arguments.get("aspect_ratio"):
            raise ChatError("画幅没有变化。")
        arguments["aspect_ratio"] = aspect_ratio
        return {"action": action, "tool": tool, "arguments": arguments}

    def _fail_new(self, problem: dict) -> dict:
        message = self._open_assistant()
        error = problem.get("error") or {}
        self._append(message["message_id"], {
            "type": "error",
            "code": str(error.get("code") or "invalid_parameter"),
            "message": str(error.get("message") or "参数不合法。"),
        })
        return self.get(message["message_id"])

    def _open_assistant(self) -> dict:
        self._load()
        message = {
            "message_id": _message_id(),
            "role": "assistant",
            "status": "streaming",
            "run_id": "run_" + uuid.uuid4().hex,
            "prompt_id": None,
            "events": [],
        }
        self.payload["messages"].append(message)
        self._save()
        return message

    def _on_run_event(self, message_id: str, event: dict) -> None:
        self._load()
        if event.get("type") == "submitted":
            prompt_id = event.get("prompt_id")
            if not isinstance(prompt_id, str) or not prompt_id:
                raise ChatError("提交结果没有 prompt_id。")
            message = self.get(message_id)
            if message.get("prompt_id") not in (None, prompt_id):
                raise ChatError("接回时不能再次提交。")
            message["prompt_id"] = prompt_id
            self._save()
            return
        if event.get("type") != "progress":
            return
        progress = {"type": "progress"}
        if event.get("max"):
            progress["value"] = int(round(float(event.get("value") or 0)))
            progress["max"] = int(round(float(event["max"])))
        if event.get("percent") is not None:
            progress["percent"] = int(event["percent"])
        if "value" not in progress and "percent" not in progress:
            return
        message = self.get(message_id)
        previous = message["events"][-1] if message["events"] else None
        if previous and previous.get("type") == "progress" and all(
            previous.get(key) == progress.get(key) for key in ("value", "max", "percent")
        ):
            return
        self._append(message_id, progress)

    def _append(self, message_id: str, event: dict) -> None:
        self._load()
        message = self.get(message_id)
        if message.get("role") != "assistant":
            raise ChatError("事件只能写在助手消息上。")
        if message.get("status") != "streaming":
            raise ChatError("这条消息已经结束。")
        kind = event.get("type")
        if kind not in EVENT_TYPES:
            raise ChatError("未知事件。")
        seen = [item.get("type") for item in message["events"]]
        if kind == "tool_call":
            if seen:
                raise ChatError("tool_call 必须是这条消息的第一个事件。")
        elif kind == "progress":
            if "tool_call" not in seen or "artifact" in seen or "error" in seen:
                raise ChatError("进度必须写在同一次 tool_call 后面。")
        elif kind == "artifact":
            if "tool_call" not in seen or "error" in seen:
                raise ChatError("成片必须接在同一次 tool_call 后面。")
            message["status"] = "completed"
        elif kind == "error":
            if "artifact" in seen:
                raise ChatError("已经有成片的消息不能再记成失败。")
            message["status"] = "failed"
        message["events"].append(event)
        self._save()

    def _load(self) -> None:
        if not os.path.isfile(self.path):
            self.payload = {"session_id": self.session_id, "messages": []}
            return
        with open(self.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict) or payload.get("session_id") != self.session_id:
            raise ChatError("聊天记录不属于这个会话。")
        if not isinstance(payload.get("messages"), list):
            raise ChatError("聊天记录无法读取。")
        self.payload = payload

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        temporary = self.path + ".tmp"
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(self.payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)


def _message_id() -> str:
    return "msg_" + uuid.uuid4().hex


def _public_arguments(arguments: dict) -> dict:
    copied = {}
    for key, value in arguments.items():
        if value is None:
            continue
        copied[key] = value
    return copied


def _reference_id(arguments: dict) -> dict | None:
    image_ref = arguments.get("image_ref")
    if image_ref is None:
        return None
    if not artifacts.is_artifact_id(image_ref):
        return agent_tools.tool_error("generate_video", "image_not_found", "image_ref 必须是 artifact_id。", "image_ref")
    return None


def _actions() -> list[dict]:
    return [
        {"action": "regenerate", "label": "重新生成"},
        {"action": "change_aspect", "label": "改比例", "aspects": list(comfy_poc.ASPECTS)},
        {"action": "use_as_reference", "label": "用作参考"},
    ]


def _card(session_id: str, result: dict) -> dict:
    media = result.get("type")
    artifact_id = result.get("artifact_id")
    if media not in ("image", "video") or not artifacts.is_artifact_id(artifact_id or ""):
        raise ChatError("生成结果没有可展示的 artifact_id。")
    url = result.get("url") or f"/sessions/{session_id}/artifacts/{artifact_id}"
    card = {
        "type": "artifact",
        "artifact_id": artifact_id,
        "media": media,
        "media_type": result.get("media_type"),
        "width": result.get("width"),
        "height": result.get("height"),
        "url": url,
        "actions": _actions(),
    }
    if media == "video":
        duration = result.get("duration_sec")
        if not duration:
            raise ChatError("视频卡片没有时长。")
        card["duration_sec"] = duration
    return card


def _check_fail(message: str) -> None:
    raise ChatError(message)


def self_check() -> None:
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
    original_run = comfy_client_run()
    calls = {"n": 0}
    try:
        log = open_transcript()
        comfy_client_set_run(lambda *_args, **_kwargs: calls.__setitem__("n", calls["n"] + 1))
        rejected = log.run_tool("generate_image", {"prompt": "teapot", "aspect_ratio": "2:1"})
        if calls["n"] != 0 or rejected["status"] != "failed":
            _check_fail("非法画幅仍然提交了，或没有记在一条失败消息上。")
        if [item["type"] for item in rejected["events"]] != ["error"]:
            _check_fail(f"非法参数不该先记 tool_call：{rejected['events']}")
        if _assistant_count(log) != 1:
            _check_fail("参数失败另起了消息。")

        def fail_run(*_args, **kwargs):
            calls["n"] += 1
            kwargs["on_event"]({"type": "submitted", "prompt_id": "prompt-fail"})
            kwargs["on_event"]({"type": "progress", "value": 1, "max": 20, "percent": 5, "node": "5"})
            raise comfy_poc.ComfyFailure(
                "Node 'MissingNodeForIssue6' not found. The custom node may not be installed.",
                {"exception_message": "Node 'MissingNodeForIssue6' not found. The custom node may not be installed."},
            )

        comfy_client_set_run(fail_run)
        before = _assistant_count(log)
        failed = log.run_tool(
            "generate_image",
            {"prompt": "a red ceramic teapot on a wooden table", "aspect_ratio": "1:1", "seed": 7},
        )
        failed_types = [item["type"] for item in failed["events"]]
        if failed_types != ["tool_call", "progress", "error"]:
            _check_fail(f"失败事件顺序不对：{failed_types}")
        if failed["status"] != "failed" or _assistant_count(log) != before + 1:
            _check_fail("失败后另起了一条消息。")
        if any(item["type"] == "artifact" for item in failed["events"]):
            _check_fail("失败消息里出现了成片。")
        if "MissingNodeForIssue6" not in failed["events"][-1]["message"]:
            _check_fail("失败消息没有保留 ComfyUI 的原文。")
        if "已经生成" in json.dumps(failed, ensure_ascii=False):
            _check_fail("失败消息声称已经生成。")
        try:
            log.apply_action(failed["message_id"], "use_as_reference")
            _check_fail("失败消息仍能当作参考图。")
        except ChatError:
            pass

        def image_run(*_args, **kwargs):
            calls["n"] += 1
            kwargs["on_event"]({"type": "submitted", "prompt_id": "prompt-image"})
            kwargs["on_event"]({"type": "progress", "value": 1, "max": 20, "percent": 5, "node": "5"})
            kwargs["on_event"]({"type": "executing", "node": "5", "class_type": "KSampler"})
            return [source]

        comfy_client_set_run(image_run)
        prompt = "a red ceramic teapot on a wooden table, soft window light"
        image = log.run_tool(
            "generate_image",
            {"prompt": prompt, "aspect_ratio": "1:1", "seed": 7, "style": "photograph"},
        )
        image_types = [item["type"] for item in image["events"]]
        if image_types != ["tool_call", "progress", "artifact"] or image["status"] != "completed":
            _check_fail(f"出图没有写在同一条消息上：{image_types}")
        if image["prompt_id"] != "prompt-image":
            _check_fail("消息没有留下这次的 prompt_id。")
        card = image["events"][-1]
        if card["media"] != "image" or "duration_sec" in card:
            _check_fail("图片卡片带了视频字段。")
        if "node" in image["events"][1] or "class_type" in json.dumps(image, ensure_ascii=False):
            _check_fail("聊天事件里出现了节点。")
        dumped = json.dumps(image, ensure_ascii=False)
        if source in dumped or source.replace("\\", "\\\\") in dumped or "base64" in dumped.lower():
            _check_fail("聊天消息里出现了文件或像素。")
        labels = [item["label"] for item in card["actions"]]
        if labels != ["重新生成", "改比例", "用作参考"]:
            _check_fail(f"卡片动作不对：{labels}")

        refreshed = Transcript(root, "session-a")
        again = refreshed.get(image["message_id"])
        if again["events"][-1]["artifact_id"] != card["artifact_id"]:
            _check_fail("刷新后卡片不在了。")
        if again["events"][-1]["url"] != f"/sessions/session-a/artifacts/{card['artifact_id']}":
            _check_fail("刷新后的卡片没有按 artifact_id 显示。")

        reference = refreshed.apply_action(image["message_id"], "use_as_reference")
        if reference["role"] != "user" or reference["text"] or reference["image_ref"] != card["artifact_id"]:
            _check_fail("用作参考没有把 artifact_id 带入下一轮。")
        if prompt in reference["text"] or "workflow_prompt" in reference:
            _check_fail("用作参考把图片写成了一段文字。")
        regen = refreshed.apply_action(image["message_id"], "regenerate")
        if "seed" in regen["arguments"] or regen["arguments"].get("prompt") != prompt:
            _check_fail("重新生成没有接上原来的参数。")
        if regen["tool"] != "generate_image":
            _check_fail("重新生成没有接回原来的工具。")
        changed = refreshed.apply_action(image["message_id"], "change_aspect", aspect_ratio="16:9")
        if changed["arguments"]["aspect_ratio"] != "16:9" or changed["arguments"].get("seed") != 7:
            _check_fail("改比例没有接上原来的参数。")
        if calls["n"] != 2:
            _check_fail("卡片动作重新提交了 ComfyUI。")

        def break_run(*_args, **kwargs):
            calls["n"] += 1
            kwargs["on_event"]({"type": "submitted", "prompt_id": "prompt-resume"})
            kwargs["on_event"]({"type": "progress", "value": 1, "max": 20, "percent": 5, "node": "5"})
            raise StreamDisconnected()

        comfy_client_set_run(break_run)
        partial = log.run_tool(
            "generate_image",
            {"prompt": prompt, "aspect_ratio": "1:1", "seed": 7, "style": "photograph"},
        )
        if partial["status"] != "streaming" or partial["prompt_id"] != "prompt-resume":
            _check_fail("断线后这次运行被当成了新的失败或成功。")
        posted = calls["n"]

        def follow(prompt_id: str, note) -> dict:
            if prompt_id != "prompt-resume":
                _check_fail("接回时丢了原来的 prompt_id。")
            note({"type": "progress", "value": 20, "max": 20, "percent": 100})
            store, session_id = artifacts.open_store(comfy_poc.load_config())
            record = store.save(
                session_id=session_id,
                source_path=source,
                seed=7,
                template="workflows/image_api.json",
                user_prompt=prompt,
                workflow_prompt=prompt,
                width=8,
                height=6,
            )
            return {"ok": True, "tool": "generate_image", **record}

        resumed = Transcript(root, "session-a").resume(partial["run_id"], follow)
        if calls["n"] != posted:
            _check_fail("接回时又提交了一次 ComfyUI。")
        resumed_types = [item["type"] for item in resumed["events"]]
        if resumed["message_id"] != partial["message_id"] or resumed_types != ["tool_call", "progress", "progress", "artifact"]:
            _check_fail(f"接回没有写回同一条消息：{resumed_types}")
        def refuse(_prompt_id, _note):
            _check_fail("已经完成的运行又被接回去执行了。")

        Transcript(root, "session-a").resume(partial["run_id"], refuse)

        def video_run(*_args, **kwargs):
            calls["n"] += 1
            kwargs["on_event"]({"type": "submitted", "prompt_id": "prompt-video"})
            kwargs["on_event"]({"type": "progress", "value": 1, "max": 2, "percent": 50})
            return [rendered]

        comfy_client_set_run(video_run)
        video = log.run_tool(
            "generate_video",
            {"prompt": "the camera slowly pushes in", "duration_sec": 1, "aspect_ratio": "16:9"},
        )
        video_card = video["events"][-1]
        if video["message_id"] == image["message_id"] or video_card.get("media") != "video":
            _check_fail("视频没有单独成卡。")
        if abs(float(video_card["duration_sec"]) - (17 / 16)) > 0.001:
            _check_fail(f"视频卡片没有时长：{video_card}")

        kept = [item for item in Transcript(root, "session-a").messages() if item.get("role") == "user"]
        if len(kept) != 1 or kept[0].get("image_ref") != card["artifact_id"]:
            _check_fail("后来的生成把用作参考的下一轮输入冲掉了。")
        if Transcript(root, "session-b").messages():
            _check_fail("别的会话读到了这条聊天记录。")
        try:
            log._append(image["message_id"], {"type": "error", "code": "late", "message": "late"})
            _check_fail("已经完成的消息还能再写失败。")
        except ChatError:
            pass
    finally:
        comfy_client_set_run(original_run)
        if old_dir is None:
            os.environ.pop("COMFY_ARTIFACT_DIR", None)
        else:
            os.environ["COMFY_ARTIFACT_DIR"] = old_dir
        if old_session is None:
            os.environ.pop("COMFY_SESSION", None)
        else:
            os.environ["COMFY_SESSION"] = old_session
        temporary.cleanup()
    print("chat_check ok")


def comfy_client_run():
    import comfy_client

    return comfy_client.run


def comfy_client_set_run(fn) -> None:
    import comfy_client

    comfy_client.run = fn


def _assistant_count(log: Transcript) -> int:
    return sum(1 for item in log.messages() if item.get("role") == "assistant")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="把一次生成的事件记在同一条助手消息上。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check", help="校验事件顺序、断线接回和卡片动作。不连接 ComfyUI。")
    subparsers.add_parser("show", help="打印当前会话的聊天记录。")
    run = subparsers.add_parser("run", help="调用一个生成工具，并把事件写入同一条消息。")
    run.add_argument("name", choices=agent_tools.TOOL_NAMES)
    run.add_argument("--json-file", required=True)
    action = subparsers.add_parser("action", help="使用卡片上的动作。重新生成和改比例只返回参数。")
    action.add_argument("message_id")
    action.add_argument("name", choices=ACTIONS)
    action.add_argument("--aspect")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "check":
            self_check()
            return
        log = open_transcript()
        if args.command == "show":
            print(json.dumps(log.payload, ensure_ascii=False, indent=2))
            return
        if args.command == "action":
            result = log.apply_action(args.message_id, args.name, aspect_ratio=args.aspect)
        else:
            with open(args.json_file, encoding="utf-8") as handle:
                arguments = json.load(handle)
            result = log.run_tool(args.name, arguments)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ChatError, artifacts.ArtifactError) as exc:
        print(exc.message)
        raise SystemExit(1)
    except comfy_poc.ComfyFailure as exc:
        print(exc.message)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
