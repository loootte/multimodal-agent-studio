"""把工具参数填进模板副本。

这里只改字段对照里的白名单。不新建节点，也不接受调用方传来的 workflow。
#3 会把模板库和「拒绝自由 JSON」放在这一层；#2 的工具只调用这里。
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import comfy_poc


def load_job(config: dict, kind: str):
    workflow, fields, timeout = comfy_poc.job_paths(config, kind)
    return copy.deepcopy(workflow), fields, timeout


def max_duration_sec(fields: dict) -> float:
    fps = float(fields.get("fps") or 16)
    limit = int(fields.get("max_frames") or 33)
    if fps <= 0:
        comfy_poc.fail("模板的 fps 必须大于 0。")
    return limit / fps


def frames_for_duration(fields: dict, duration_sec: float) -> int:
    fps = float(fields.get("fps") or 16)
    step = int(fields.get("frame_step") or 4)
    limit = int(fields.get("max_frames") or 33)
    requested = int(round(float(duration_sec) * fps))
    if requested > limit:
        comfy_poc.fail(
            f"duration_sec 超过模板上限 {max_duration_sec(fields):g} 秒。"
        )
    return comfy_poc.snap_frames(requested, step, limit)


def fill_common(workflow: dict, fields: dict, prompt: str, aspect: str, seed: int, negative: str | None):
    comfy_poc.apply_common(
        workflow,
        fields,
        SimpleNamespace(prompt=prompt, aspect=aspect, seed=seed, negative=negative),
    )
    return workflow


def fill_frames(workflow: dict, fields: dict, length: int) -> None:
    comfy_poc.apply_frames(
        workflow,
        fields,
        SimpleNamespace(frames=length, seconds=None),
    )


def assert_text_to_video(workflow: dict, fields: dict) -> None:
    link = (fields.get("reference_image") or {}).get("link_to") or {}
    node_id = str(link.get("node") or "")
    node = workflow.get(node_id) or {}
    if link.get("input") in (node.get("inputs") or {}):
        comfy_poc.fail("文生视频模板不应连接参考图。")


def assert_image_to_video(workflow: dict, fields: dict) -> None:
    spec = fields.get("reference_image")
    if not isinstance(spec, dict):
        comfy_poc.fail("图生视频模板没有 reference_image 字段。")
    node_id = str(spec["node"])
    if node_id not in workflow:
        comfy_poc.fail("图生视频模板里没有参考图节点。")
    link = spec.get("link_to") or {}
    if not link:
        return
    current = ((workflow.get(str(link["node"])) or {}).get("inputs") or {}).get(link["input"])
    expected = [node_id, int(link.get("output") or 0)]
    if current != expected:
        comfy_poc.fail("图生视频模板没有接上参考图。")


def set_reference_name(workflow: dict, fields: dict, uploaded_name: str) -> None:
    spec = fields.get("reference_image")
    if not isinstance(spec, dict):
        comfy_poc.fail("图生视频模板没有 reference_image 字段。")
    comfy_poc.set_inputs(workflow, {"node": spec["node"], "input": spec["input"]}, uploaded_name)


def read_back(workflow: dict, fields: dict, key: str):
    spec = fields[key]
    target = spec[0] if isinstance(spec, list) else spec
    return workflow[str(target["node"])]["inputs"][target["input"]]
