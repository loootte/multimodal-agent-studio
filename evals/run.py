"""固定评测：一条命令跑完，给出每项通过与否，全部通过退出码为 0。

默认用 apps/web/check.py 里的假供应商和假 ComfyUI，不访问外网，结果可重复。
--live 时核心任务改用真实供应商和真实 ComfyUI（读环境变量，见 evals/README.md），
期望放宽到 live_expect；只能用假件验证的任务（例如安全项）仍然用假件跑。

    python evals/run.py                 # 假件
    python evals/run.py --json          # 机器可读结果
    python evals/run.py --report out.json
    python evals/run.py --live
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from http.server import ThreadingHTTPServer

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(EVAL_DIR, ".."))
WEB_DIR = os.path.join(ROOT, "apps", "web")
sys.path.insert(0, WEB_DIR)
sys.path.insert(0, ROOT)

import check  # noqa: E402  假件和工具函数都在这里
import server  # noqa: E402

TASKS_PATH = os.path.join(EVAL_DIR, "tasks.json")
FAKE_MODEL = "demo-vision"
LIVE_ENV = ("EVAL_BASE_URL", "EVAL_API_KEY", "EVAL_MODEL")


def load_tasks(path: str = TASKS_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    tasks = data.get("tasks") or []
    seen = set()
    for task in tasks:
        if task["id"] in seen:
            raise SystemExit(f"评测任务 id 重复：{task['id']}")
        seen.add(task["id"])
    return tasks


class Harness:
    """一套站点 + 供应商 + ComfyUI。假件模式下全在 127.0.0.1。"""

    def __init__(self, live: bool):
        self.live = live
        self.directory = tempfile.mkdtemp(prefix="evals-")
        self.saved_env = {name: os.environ.get(name) for name in check._COMFY_ENV}
        self.provider = None
        self.comfy = None
        self.box = None
        self.app = None
        self.site = None
        self.base = ""
        self.upstream = ""

    def __enter__(self):
        self.provider = ThreadingHTTPServer(("127.0.0.1", 0), check.ProviderHandler)
        self.provider.daemon_threads = True
        self.provider.mode = "ok"
        self.provider.requests = []
        self.provider.gate = check.threading.Event()
        self.provider.started = check.threading.Event()
        check.serve(self.provider)
        self.comfy = ThreadingHTTPServer(("127.0.0.1", 0), check.ComfyHandler)
        self.comfy.daemon_threads = True
        self.comfy.box = check.ComfyState()
        self.box = self.comfy.box
        check.serve(self.comfy)
        if not self.live:
            self._fake_comfy_env()
        self.app = server.App(self.directory, timeout_sec=8)
        self.site = server.make_httpd(self.app, "127.0.0.1", 0)
        check.serve(self.site)
        self.base = f"http://127.0.0.1:{self.site.server_address[1]}"
        self.upstream = f"http://127.0.0.1:{self.provider.server_address[1]}/v1"
        self.use_fake_provider()
        return self

    def _fake_comfy_env(self) -> None:
        port = self.comfy.server_address[1]
        config_path = os.path.join(self.directory, "comfy-config.json")
        workflow = os.path.join(ROOT, "poc", "comfyui", "workflows", "image_api.json")
        fields = os.path.join(ROOT, "poc", "comfyui", "workflows", "image.fields.json")
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump({
                "comfy_url": f"http://127.0.0.1:{port}",
                "output_dir": os.path.join(self.directory, "comfy-output"),
                "artifact_dir": os.path.join(self.directory, "comfy-artifacts"),
                "session_id": "local",
                "image": {"workflow": workflow, "fields": fields, "timeout_sec": 20},
            }, handle)
        for name in check._COMFY_ENV:
            os.environ.pop(name, None)
        os.environ["COMFY_CONFIG"] = config_path
        os.environ["COMFY_URL"] = f"http://127.0.0.1:{port}"

    def use_fake_provider(self) -> None:
        self._put_provider({"base_url": self.upstream, "model": FAKE_MODEL, "api_key": check.SECRET})

    def use_live_provider(self) -> None:
        self._put_provider({
            "base_url": os.environ["EVAL_BASE_URL"],
            "model": os.environ["EVAL_MODEL"],
            "api_key": os.environ["EVAL_API_KEY"],
        })

    def _put_provider(self, payload: dict) -> None:
        status, body = check.request("PUT", self.base + "/api/provider", payload)
        if status != 200:
            raise RuntimeError(f"设置供应商失败：{status}")

    def __exit__(self, *_exc):
        for httpd in (self.site, self.provider, self.comfy):
            if httpd is not None:
                try:
                    httpd.shutdown()
                    httpd.server_close()
                except Exception:
                    pass
        for name, value in self.saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.directory, ignore_errors=True)
        return False


class Failed(Exception):
    pass


def expect(condition, message: str) -> None:
    if not condition:
        raise Failed(message)


def _status(events: list[dict]) -> str:
    if not events:
        return "none"
    last = events[-1]
    if last.get("type") == "message.completed":
        return "completed"
    return str(last.get("code") or last.get("type") or "unknown")


def _attach_png(h: Harness, session_id: str) -> dict:
    status, body = check.request("POST", h.base + f"/api/sessions/{session_id}/attachments", {
        "file": base64.b64encode(check._PNG).decode("ascii"),
        "media_type": "image/png",
        "name": "eval.png",
    })
    expect(status == 200, f"附件没有收下：{status}")
    return {"handle": json.loads(body)["handle"]}


def run_chat(h: Harness, task: dict, live: bool) -> dict:
    """一轮网页对话：发消息、按需确认和提交本地内容，按 expect 判定。"""
    want = task.get("live_expect") if live and task.get("live_expect") else task.get("expect") or {}
    if not live:
        h.provider.mode = task.get("fake_mode") or "ok"
        h.box.set_mode("ok")
    session_id = check._fresh_session(h.base)
    attachments = [_attach_png(h, session_id)] if task.get("attachment") == "png" else None
    sent = len(h.provider.requests)
    prompts = len(h.box.prompts)
    try:
        events = check._speak(h.base, session_id, task["prompt"], timeout=float(task.get("timeout", 30 if live else 12)), attachments=attachments)
    except SystemExit as exc:
        raise Failed(str(exc)) from None
    text = check._assistant_text(events)
    status = _status(events)
    outgoing = h.provider.requests[sent:]
    artifacts = [event for event in events if event.get("type") == "artifact"]
    detail = {"status": status, "events": len(events), "artifacts": len(artifacts)}
    if "status" in want:
        expect(status == want["status"], f"状态是 {status}，应为 {want['status']}")
    if "text_equals" in want:
        expect(text == want["text_equals"], f"回答是 {text!r}")
    if want.get("text_nonempty"):
        expect(bool(text.strip()), "回答是空的")
    if "artifacts" in want:
        expect(len(artifacts) == want["artifacts"], f"出了 {len(artifacts)} 张图")
    if "artifacts_min" in want:
        expect(len(artifacts) >= want["artifacts_min"], f"只出了 {len(artifacts)} 张图")
    if "comfy_prompts" in want and not live:
        expect(len(h.box.prompts) - prompts == want["comfy_prompts"], "ComfyUI 提交次数不对")
    if want.get("no_content_request"):
        expect(not any(event.get("type") == "content_request" for event in events), "弹出了本地内容窗口")
    if want.get("image_sent_to_provider"):
        blob = json.dumps([item["body"] for item in outgoing], ensure_ascii=False)
        expect("image_url" in blob, "图片没有送到供应商")
    if want.get("prompt_not_in_provider") and not live:
        blob = json.dumps([item["body"] for item in outgoing], ensure_ascii=False)
        expect(task["prompt"] not in blob, "本地画面描述进入了供应商请求")
    return detail


RUNNERS = {
    "chat": run_chat,
}


def run_all(live: bool = False, only=None, tasks=None) -> dict:
    if live:
        missing = [name for name in LIVE_ENV if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"--live 需要环境变量：{', '.join(missing)}")
    tasks = tasks if tasks is not None else load_tasks()
    if only:
        tasks = [task for task in tasks if task["id"] in only or task.get("category") in only]
    results = []
    started_all = time.monotonic()
    with Harness(live) as h:
        for task in tasks:
            live_task = live and not task.get("fake_only")
            if live:
                h.use_live_provider() if live_task else h.use_fake_provider()
            runner = RUNNERS.get(task.get("kind"))
            started = time.monotonic()
            entry = {"id": task["id"], "category": task.get("category", ""), "mode": "live" if live_task else "fake"}
            try:
                if runner is None:
                    raise Failed(f"不认识的任务种类：{task.get('kind')}")
                entry["detail"] = runner(h, task, live_task)
                entry["ok"] = True
            except Failed as exc:
                entry["ok"] = False
                entry["error"] = str(exc)
            except Exception as exc:  # 评测器自身的问题也算失败，但要看得见
                entry["ok"] = False
                entry["error"] = f"{type(exc).__name__}: {exc}"
                entry["trace"] = traceback.format_exc(limit=3)
            entry["ms"] = int((time.monotonic() - started) * 1000)
            results.append(entry)
            if not live:
                check.release(h.provider)
    passed = sum(1 for item in results if item["ok"])
    return {
        "mode": "live" if live else "fake",
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "ms": int((time.monotonic() - started_all) * 1000),
        "tasks": results,
    }


def print_report(report: dict) -> None:
    for item in report["tasks"]:
        mark = "PASS" if item["ok"] else "FAIL"
        line = f"{mark}  {item['id']:<36} {item['ms']:>6} ms  [{item['mode']}]"
        if not item["ok"]:
            line += f"  {item.get('error')}"
        print(line)
    print(f"evals {report['passed']}/{report['total']} passed ({report['mode']})")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="固定评测")
    parser.add_argument("--live", action="store_true", help="核心任务用真实供应商和 ComfyUI")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    parser.add_argument("--report", help="把结果写到这个 JSON 文件")
    parser.add_argument("--only", nargs="*", help="只跑这些 id 或类别")
    args = parser.parse_args(argv)
    report = run_all(live=args.live, only=set(args.only) if args.only else None)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_report(report)
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
