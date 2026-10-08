"""复制模板，并只按对照表里的白名单字段写入。

提交前核对两件事：对照表指向的节点必须已经在模板里；
准备提交的图必须仍是这份模板，只允许白名单输入发生变化。
模型拼出来的自由 JSON 不能进入 POST /prompt。
"""

from __future__ import annotations

import copy

WRITE_FIELDS = (
    "positive_prompt",
    "negative_prompt",
    "width",
    "height",
    "seed",
    "steps",
    "frames",
    "reference_image",
)
META_FIELDS = ("aspects", "max_frames", "frame_step", "fps")


class TemplateError(Exception):
    def __init__(self, message: str, payload=None):
        super().__init__(message)
        self.message = message
        self.payload = payload


def targets(spec) -> list[dict]:
    if isinstance(spec, list):
        return spec
    if isinstance(spec, dict):
        return [spec]
    raise TemplateError("对照表的字段格式不对。")


def copy_template(workflow: dict) -> dict:
    return copy.deepcopy(workflow)


def validate_fields(workflow: dict, fields: dict) -> None:
    if not isinstance(workflow, dict) or not workflow:
        raise TemplateError("模板不是工作流对象，未提交。")
    if not isinstance(fields, dict):
        raise TemplateError("字段对照不是对象，未提交。")
    unknown = sorted(set(fields) - set(WRITE_FIELDS) - set(META_FIELDS))
    if unknown:
        raise TemplateError(f"对照表里有白名单之外的字段：{', '.join(unknown)}。未提交。")
    for name in ("positive_prompt", "width", "height", "seed"):
        if name not in fields:
            raise TemplateError(f"对照表缺少字段 {name}，未提交。")
    for name, spec in fields.items():
        if name in META_FIELDS:
            continue
        if name == "reference_image":
            _check_reference(workflow, spec)
            continue
        for target in targets(spec):
            _check_target(workflow, name, target)


def slots(fields: dict) -> set[tuple[str, str]]:
    found = set()
    for name, spec in fields.items():
        if name in META_FIELDS:
            continue
        if name == "reference_image":
            found.add((str(spec["node"]), spec["input"]))
            continue
        for target in targets(spec):
            found.add((str(target["node"]), target["input"]))
    return found


def set_reference_name(workflow: dict, fields: dict, uploaded_name: str) -> None:
    spec = fields.get("reference_image")
    if not isinstance(spec, dict):
        raise TemplateError("这张模板的对照表没有 reference_image，不能提交参考图。")
    node_id = str(spec["node"])
    field = spec["input"]
    node = workflow.get(node_id)
    if node is None or field not in (node.get("inputs") or {}):
        raise TemplateError(f"对照表字段 reference_image 的节点 {node_id} 不在模板里，未提交。")
    node["inputs"][field] = uploaded_name


def assert_template_edit(template: dict, filled: dict, fields: dict) -> None:
    if not isinstance(filled, dict):
        raise TemplateError("拒绝提交非模板 JSON。")
    if set(template) != set(filled):
        raise TemplateError("拒绝提交非模板 JSON：节点集合不一致。")
    allowed = slots(fields)
    for node_id, original in template.items():
        current = filled.get(node_id)
        if not isinstance(current, dict):
            raise TemplateError(f"拒绝提交非模板 JSON：节点 {node_id} 不是对象。")
        if current.get("class_type") != original.get("class_type"):
            raise TemplateError(f"拒绝提交非模板 JSON：节点 {node_id} 的类型被改了。")
        original_inputs = original.get("inputs") or {}
        current_inputs = current.get("inputs") or {}
        if set(original_inputs) != set(current_inputs):
            raise TemplateError(f"拒绝提交非模板 JSON：节点 {node_id} 的输入集合被改了。")
        for key, value in original_inputs.items():
            if (str(node_id), key) in allowed:
                continue
            if current_inputs.get(key) != value:
                raise TemplateError(f"拒绝提交非模板 JSON：节点 {node_id} 的输入 {key} 不在白名单里。")


def run_check(config: dict) -> None:
    import urllib.request

    import comfy_poc

    calls = {"count": 0}
    original = urllib.request.urlopen

    def trap(*_args, **_kwargs):
        calls["count"] += 1
        raise RuntimeError("校验不该访问 ComfyUI")

    urllib.request.urlopen = trap
    try:
        shapes = {
            "image": {"positive_prompt", "negative_prompt", "width", "height", "seed", "steps"},
            "image_i2i": {
                "positive_prompt",
                "negative_prompt",
                "width",
                "height",
                "seed",
                "steps",
                "reference_image",
            },
            "video": {
                "positive_prompt",
                "negative_prompt",
                "width",
                "height",
                "seed",
                "steps",
                "frames",
            },
            "video_i2v": {
                "positive_prompt",
                "negative_prompt",
                "width",
                "height",
                "seed",
                "steps",
                "frames",
                "reference_image",
            },
        }
        for kind, required in shapes.items():
            workflow, fields, _timeout = comfy_poc.job_paths(config, kind)
            missing = sorted(required - set(fields))
            if missing:
                raise TemplateError(f"{kind} 对照表缺少 {', '.join(missing)}。")
            if kind == "video" and "reference_image" in fields:
                raise TemplateError("文生视频对照表不应包含 reference_image。")
            if kind in {"video", "video_i2v"} and int(fields.get("max_frames") or 0) < 1:
                raise TemplateError(f"{kind} 对照表没有帧数上限。")
            validate_fields(workflow, fields)
            filled = copy_template(workflow)
            comfy_poc.set_inputs(filled, fields["positive_prompt"], "a red ceramic teapot")
            assert_template_edit(workflow, filled, fields)
            if filled is workflow:
                raise TemplateError("写入前没有复制模板。")

            bad = copy.deepcopy(fields)
            seed_spec = bad["seed"]
            if isinstance(seed_spec, list):
                seed_spec[0]["node"] = "999"
            else:
                seed_spec["node"] = "999"
            try:
                validate_fields(workflow, bad)
            except TemplateError as exc:
                if "seed" not in exc.message or "999" not in exc.message:
                    raise TemplateError(f"错误节点的提示没有写明字段：{exc.message}") from exc
            else:
                raise TemplateError("错误节点没有在提交前失败。")

            extra = copy.deepcopy(fields)
            extra["sampler_name"] = {"node": "1", "input": "sampler_name"}
            try:
                validate_fields(workflow, extra)
            except TemplateError as exc:
                if "白名单之外" not in exc.message:
                    raise TemplateError(f"白名单之外的字段没有被拒绝：{exc.message}") from exc
            else:
                raise TemplateError("白名单之外的字段没有被拒绝。")

            try:
                assert_template_edit(workflow, {"1": {"class_type": "Note", "inputs": {}}}, fields)
            except TemplateError as exc:
                if "非模板" not in exc.message:
                    raise TemplateError(f"自由 JSON 没有被拒绝：{exc.message}") from exc
            else:
                raise TemplateError("自由 JSON 没有被拒绝。")

            dirty = _dirty_copy(workflow, fields)
            try:
                assert_template_edit(workflow, dirty, fields)
            except TemplateError as exc:
                if "非模板" not in exc.message:
                    raise TemplateError(f"白名单之外的修改没有被拒绝：{exc.message}") from exc
            else:
                raise TemplateError("白名单之外的修改没有被拒绝。")

            try:
                comfy_poc.submit_prompt(config["comfy_url"], dirty, "check", workflow, fields)
            except TemplateError:
                pass
            else:
                raise TemplateError("被拒绝的图仍然进入了提交。")
        if calls["count"]:
            raise TemplateError("校验阶段访问了 ComfyUI。")
    finally:
        urllib.request.urlopen = original


def _check_target(workflow: dict, name: str, target: dict) -> None:
    if not isinstance(target, dict) or "node" not in target or "input" not in target:
        raise TemplateError(f"对照表字段 {name} 格式不对，未提交。")
    node_id = str(target["node"])
    field = target["input"]
    node = workflow.get(node_id)
    if node is None:
        raise TemplateError(f"对照表字段 {name} 的节点 {node_id} 不在模板里，未提交。")
    inputs = node.get("inputs") or {}
    if field not in inputs:
        raise TemplateError(f"对照表字段 {name} 的输入 {field} 不在节点 {node_id} 上，未提交。")


def _check_reference(workflow: dict, spec) -> None:
    if not isinstance(spec, dict):
        raise TemplateError("对照表字段 reference_image 格式不对，未提交。")
    _check_target(workflow, "reference_image", spec)
    node_id = str(spec["node"])
    expected_class = spec.get("class_type")
    if expected_class and workflow[node_id].get("class_type") != expected_class:
        raise TemplateError(
            f"对照表字段 reference_image 的节点 {node_id} 不是 {expected_class}，未提交。"
        )
    link = spec.get("link_to")
    if not isinstance(link, dict):
        return
    target = str(link.get("node") or "")
    key = link.get("input")
    if target not in workflow:
        raise TemplateError(f"对照表字段 reference_image 的节点 {target} 不在模板里，未提交。")
    current = (workflow[target].get("inputs") or {}).get(key)
    expected = [node_id, int(link.get("output") or 0)]
    if current != expected:
        raise TemplateError("对照表字段 reference_image 在模板里没有接上，未提交。")


def _dirty_copy(workflow: dict, fields: dict) -> dict:
    filled = copy.deepcopy(workflow)
    allowed = slots(fields)
    for node_id, node in filled.items():
        inputs = node.get("inputs") or {}
        for key, value in inputs.items():
            if (str(node_id), key) in allowed:
                continue
            if isinstance(value, str):
                inputs[key] = value + " "
                return filled
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                inputs[key] = value + 1
                return filled
    raise TemplateError("没有找到白名单之外的输入可做测试。")
