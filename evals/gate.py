"""发布门禁：跑评测，和存好的基线比，有回退就失败（退出码 1）。

回退的定义：
- 基线里通过的任务现在失败；
- 基线里有的任务现在没有了；
- 新加的任务失败（新任务进来时就该通过，否则先修再更新基线）。
基线里本来就失败的任务（已知问题）继续失败不算回退，但会列出来；它们变成通过时提示更新基线。

    python evals/gate.py                    # 和 evals/baseline.json 比
    python evals/gate.py --update-baseline  # 全部通过时把这次结果写成新基线
    python evals/gate.py --live             # 用 live 结果和基线里 live 那一份比
"""

from __future__ import annotations

import argparse
import json
import os
import sys

EVAL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, EVAL_DIR)

import run as evals  # noqa: E402

BASELINE = os.path.join(EVAL_DIR, "baseline.json")


def load_baseline(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def compare(baseline: dict, report: dict) -> dict:
    before = baseline.get("tasks") or {}
    now = {item["id"]: item for item in report["tasks"]}
    regressed, missing, new_failing, known, fixed = [], [], [], [], []
    for task_id, ok in before.items():
        if task_id not in now:
            missing.append(task_id)
        elif ok and not now[task_id]["ok"]:
            regressed.append(task_id)
        elif not ok and not now[task_id]["ok"]:
            known.append(task_id)
        elif not ok and now[task_id]["ok"]:
            fixed.append(task_id)
    for task_id, item in now.items():
        if task_id not in before and not item["ok"]:
            new_failing.append(task_id)
    return {
        "regressed": regressed,
        "missing": missing,
        "new_failing": new_failing,
        "known_failing": known,
        "fixed": fixed,
        "ok": not (regressed or missing or new_failing),
    }


def snapshot(report: dict) -> dict:
    return {
        "mode": report["mode"],
        "tasks": {item["id"]: bool(item["ok"]) for item in report["tasks"]},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="发布门禁")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--baseline", default=BASELINE)
    parser.add_argument("--update-baseline", action="store_true", help="全部通过时写入新基线")
    parser.add_argument("--report", help="把评测结果和比较写到这个 JSON 文件")
    args = parser.parse_args(argv)
    report = evals.run_all(live=args.live)
    evals.print_report(report)
    stored = load_baseline(args.baseline)
    mode = report["mode"]
    baseline = (stored.get("modes") or {}).get(mode) or {}
    if not baseline:
        print(f"gate: 基线里没有 {mode} 模式的记录，只要求全部通过")
    result = compare(baseline, report)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"report": report, "gate": result}, handle, ensure_ascii=False, indent=2)
    for label, key in (("回退", "regressed"), ("缺少", "missing"), ("新任务失败", "new_failing"),
                       ("已知失败", "known_failing"), ("已修好，可更新基线", "fixed")):
        if result[key]:
            print(f"gate: {label}：{', '.join(result[key])}")
    if args.update_baseline:
        if report["failed"]:
            print("gate: 有失败的任务，不更新基线")
            return 1
        stored.setdefault("modes", {})[mode] = snapshot(report)
        with open(args.baseline, "w", encoding="utf-8") as handle:
            json.dump(stored, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        print(f"gate: 已更新 {mode} 基线（{report['total']} 项）")
        return 0
    print("gate ok" if result["ok"] else "gate FAILED")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
