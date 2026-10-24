"""从 mas.trajectory/1 挖掘重复子流程，产出 mas.skill-candidate/1（ADR 0007 / #40）。

    python -m services.meta.mine [--data-dir DIR] [--min-count 3] [--min-sessions 2] [--model]
    python -m services.meta.mine list
    python -m services.meta.mine show <id>

只读轨迹，只写 data/meta/skill-candidates.json。从不改 skills/。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from services.meta.candidate import (  # noqa: E402
    SCHEMA,
    STORE_SCHEMA,
    validate,
    validate_store,
)
from services.meta.export import Exporter  # noqa: E402
from services.meta.trajectory import build_tree  # noqa: E402
from services.runtime import skills as skill_catalog  # noqa: E402

MIN_COUNT = 3
MIN_SESSIONS = 2
MAX_N = 5
MIN_N = 2
MAX_SCENARIOS = 5
MAX_RUN_IDS = 40
_lock = threading.Lock()
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{12,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9-]{8,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[=:]\s*\S{6,}"),
]
_PATHS = [
    re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|]*"),
    re.compile(r"\\\\[^\s\\\"']+\\[^\s\"'<>|]*"),
    re.compile(r"(?<![\w.])/(?:home|Users|root|tmp|workspace|mnt|var|opt|private|data|srv|etc|media)(?:/[^\s\"'<>|]*)?"),
]
_TOKEN = re.compile(r"[a-z0-9\u4e00-\u9fff]{2,}", re.I)


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _scrub_text(value: str) -> str:
    value = value or ""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub("[secret]", value)
    for pattern in _PATHS:
        value = pattern.sub("[path]", value)
    return value


def _slug(parts: list[str], kind: str) -> str:
    raw = "-then-".join(re.sub(r"[^a-z0-9]+", "-", p.lower()).strip("-") or "step" for p in parts)
    if kind == "agent_sequence":
        raw = "agents-" + raw
    raw = re.sub(r"-{2,}", "-", raw).strip("-")[:60] or "pattern"
    if not re.match(r"^[a-z0-9]", raw):
        raw = "p-" + raw
    return raw


def _candidate_id(signature: str) -> str:
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:32]
    return "sc_" + digest


def _tokens(text: str) -> set[str]:
    return {m.group(0).lower() for m in _TOKEN.finditer(text or "")}


def _tool_sequence(record: dict) -> list[str]:
    names = []
    for step in record.get("steps") or []:
        if not isinstance(step, dict) or step.get("kind") != "tool":
            continue
        name = step.get("name")
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _ngrams(seq: list[str], min_n: int = MIN_N, max_n: int = MAX_N) -> list[tuple[str, ...]]:
    out = []
    upper = min(max_n, len(seq))
    for n in range(min_n, upper + 1):
        for i in range(len(seq) - n + 1):
            out.append(tuple(seq[i:i + n]))
    return out


def _scenario(record: dict, parents: dict[str, dict]) -> str | None:
    top = record
    parent_id = record.get("parent_run_id")
    while parent_id and parent_id in parents:
        top = parents[parent_id]
        parent_id = top.get("parent_run_id")
    for block in (top.get("input"), top.get("output"), record.get("output")):
        if isinstance(block, dict):
            summary = block.get("summary")
            if isinstance(summary, str) and summary.strip():
                return _scrub_text(summary.strip())[:120]
    return None


def _overlap(signature: str, description: str, installed: list[dict]) -> tuple[str, list[dict], str]:
    cand_tokens = _tokens(signature + " " + description)
    hits = []
    for skill in installed:
        skill_tokens = _tokens(skill.get("name", "") + " " + skill.get("description", ""))
        if not skill_tokens or not cand_tokens:
            continue
        score = len(cand_tokens & skill_tokens) / len(cand_tokens | skill_tokens)
        if score >= 0.08:
            hits.append({
                "name": skill["name"],
                "score": round(score, 3),
                "reason": f"与已安装技能 {skill['name']} 词元重叠约 {score:.0%}",
            })
    hits.sort(key=lambda h: -h["score"])
    if hits and hits[0]["score"] >= 0.25:
        form = "existing_skill"
        consolidation = (
            f"与「{hits[0]['name']}」重叠较高：建议对照已有 SKILL.md 决定是扩展描述、"
            f"合并场景，还是保持独立（#41 巩固时由用户批准）。"
        )
    else:
        form = "implicit"
        consolidation = "轨迹里隐含的新模式：建议写成独立技能草稿，澄清适用场景与步骤边界后再批准写入 skills/。"
    return form, hits[:5], consolidation


def _describe(kind: str, steps: list[str], count: int, sessions: int, rate: float) -> str:
    joined = " → ".join(steps)
    if kind == "agent_sequence":
        head = f"多智能体委托序列「{joined}」"
    else:
        head = f"工具序列「{joined}」"
    return (
        f"{head}在轨迹里出现 {count} 次、跨越 {sessions} 个会话，成功率约 {rate:.0%}。"
        f"可作为可复用子流程沉淀成技能（候选，未经批准不写入 skills/）。"
    )


def _steps_draft(kind: str, steps: list[str]) -> list[str]:
    if kind == "agent_sequence":
        return [f"委托给 {name}，等待其完成后再进入下一步" for name in steps]
    return [f"调用工具 {name}" for name in steps]


def build_model_prompt(bucket: dict) -> str:
    """脱敏后的归纳输入。不含本地正文、密钥或绝对路径；check.py 会断言。"""
    scenarios = [_scrub_text(s) for s in bucket["scenarios"][:MAX_SCENARIOS]]
    payload = {
        "task": "skill_candidate_draft",
        "pattern": {"kind": bucket["kind"], "signature": bucket["signature"], "steps": list(bucket["steps"])},
        "evidence": {
            "count": bucket["count"],
            "sessions": bucket["sessions"],
            "success_rate": bucket["success_rate"],
            "typical_scenarios": scenarios,
            "run_id_count": len(bucket["run_ids"]),
        },
        "ask": ["name_slug", "description", "steps_draft", "when_to_use"],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    return _scrub_text(text)


def _try_model(prompt: str, call_model) -> dict | None:
    if call_model is None:
        return None
    try:
        raw = call_model(prompt)
    except Exception:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None
    return None



def _is_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    if len(short) >= len(long):
        return False
    for i in range(len(long) - len(short) + 1):
        if long[i:i + len(short)] == short:
            return True
    return False


def _prefer_longer(buckets: dict[str, dict], min_count: int, min_sessions: int) -> set[str]:
    """达到门槛的签名里，若被更长且也达标的序列完整覆盖，则丢掉短的，避免 n-gram 碎片。"""
    eligible = [
        (sig, b) for sig, b in buckets.items()
        if b["count"] >= min_count and b["sessions"] >= min_sessions
    ]
    eligible.sort(key=lambda pair: (-len(pair[1]["steps"]), -pair[1]["count"], pair[0]))
    kept: list[tuple[str, tuple]] = []
    dropped = set()
    for sig, bucket in eligible:
        steps = tuple(bucket["steps"])
        if any(_is_subsequence(steps, longer) for _sig, longer in kept):
            dropped.add(sig)
            continue
        kept.append((sig, steps))
    return dropped

class Miner:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.meta_dir = os.path.join(data_dir, "meta")
        self.store_path = os.path.join(self.meta_dir, "skill-candidates.json")
        self.exporter = Exporter(data_dir)

    def load_store(self) -> dict:
        if not os.path.isfile(self.store_path):
            return {"schema": STORE_SCHEMA, "mined_at": None, "params": {}, "candidates": [], "insufficient": []}
        with open(self.store_path, encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("schema") != STORE_SCHEMA:
            return {"schema": STORE_SCHEMA, "mined_at": None, "params": {}, "candidates": [], "insufficient": []}
        return data

    def _save_store(self, store: dict) -> None:
        os.makedirs(self.meta_dir, exist_ok=True)
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(store, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, self.store_path)

    def records(self, export_first: bool = True) -> list[dict]:
        if export_first:
            self.exporter.export()
        return self.exporter.query(limit=0)

    def mine(
        self,
        min_count: int = MIN_COUNT,
        min_sessions: int = MIN_SESSIONS,
        use_model: bool = False,
        call_model=None,
        export_first: bool = True,
        records: list[dict] | None = None,
    ) -> dict:
        """跑一次挖掘，覆盖写入 skill-candidates.json。返回摘要。"""
        with _lock:
            rows = records if records is not None else self.records(export_first=export_first)
            buckets = self._cluster(rows)
            suppressed = _prefer_longer(buckets, min_count, min_sessions)
            installed = skill_catalog.installed()
            candidates = []
            insufficient = []
            model_prompts = []
            mined_at = _iso_now()
            for signature, bucket in sorted(buckets.items(), key=lambda kv: (-kv[1]["count"], kv[0])):
                meets = bucket["count"] >= min_count and bucket["sessions"] >= min_sessions
                if meets and signature in suppressed:
                    continue
                status = "candidate" if meets else "insufficient"
                description = _describe(
                    bucket["kind"], list(bucket["steps"]), bucket["count"], bucket["sessions"], bucket["success_rate"]
                )
                form, overlap, consolidation = _overlap(signature, description, installed)
                name = _slug(list(bucket["steps"]), bucket["kind"])
                steps_draft = _steps_draft(bucket["kind"], list(bucket["steps"]))
                model_assisted = False
                prompt_chars = None
                if use_model and meets:
                    prompt = build_model_prompt(bucket)
                    model_prompts.append(prompt)
                    prompt_chars = len(prompt)
                    drafted = _try_model(prompt, call_model)
                    if isinstance(drafted, dict):
                        model_assisted = True
                        if isinstance(drafted.get("name"), str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", drafted["name"]):
                            name = drafted["name"]
                        if isinstance(drafted.get("description"), str) and drafted["description"].strip():
                            description = _scrub_text(drafted["description"].strip())[:2000]
                        if isinstance(drafted.get("steps_draft"), list) and drafted["steps_draft"]:
                            steps_draft = [_scrub_text(str(s))[:200] for s in drafted["steps_draft"] if str(s).strip()][:20]
                record = {
                    "schema": SCHEMA,
                    "id": _candidate_id(signature),
                    "name": name,
                    "description": description,
                    "status": status,
                    "pattern": {
                        "kind": bucket["kind"],
                        "signature": signature,
                        "n": len(bucket["steps"]),
                        "steps": list(bucket["steps"]),
                    },
                    "evidence": {
                        "count": bucket["count"],
                        "sessions": bucket["sessions"],
                        "success_rate": round(bucket["success_rate"], 4),
                        "run_ids": bucket["run_ids"][:MAX_RUN_IDS],
                        "typical_scenarios": bucket["scenarios"][:MAX_SCENARIOS],
                        "outcomes": dict(sorted(bucket["outcomes"].items())),
                    },
                    "existing": {
                        "form": form,
                        "overlap": overlap,
                        "consolidation": consolidation,
                    },
                    "steps_draft": steps_draft,
                    "mined_at": mined_at,
                    "model_assisted": model_assisted,
                    "model_prompt_chars": prompt_chars,
                }
                record = self._assert_clean(record)
                if record is None:
                    continue
                problems = validate(record)
                if problems:
                    continue
                if status == "candidate":
                    candidates.append(record)
                else:
                    insufficient.append({
                        "signature": signature,
                        "count": bucket["count"],
                        "sessions": bucket["sessions"],
                        "reason": "证据不足",
                        "run_ids": bucket["run_ids"][:10],
                    })
            store = {
                "schema": STORE_SCHEMA,
                "mined_at": mined_at,
                "params": {
                    "min_count": min_count,
                    "min_sessions": min_sessions,
                    "use_model": bool(use_model),
                    "records": len(rows),
                    "patterns_seen": len(buckets),
                },
                "candidates": candidates,
                "insufficient": insufficient,
            }
            problems = validate_store(store)
            if problems:
                raise RuntimeError("候选库没通过校验：" + "; ".join(problems[:5]))
            self._save_store(store)
            return {
                "candidates": len(candidates),
                "insufficient": len(insufficient),
                "patterns_seen": len(buckets),
                "records": len(rows),
                "path": self.store_path,
                "mined_at": mined_at,
                "model_prompts": model_prompts if use_model else [],
            }

    def _assert_clean(self, record: dict) -> dict | None:
        record = self._map_scrub(record)
        blob = json.dumps(record, ensure_ascii=False)
        for pattern in _SECRET_PATTERNS:
            if pattern.search(blob):
                return None
        for pattern in _PATHS:
            if pattern.search(blob):
                return None
        if os.path.abspath(self.data_dir) in blob:
            return None
        return record

    def _map_scrub(self, value):
        if isinstance(value, str):
            return _scrub_text(value)
        if isinstance(value, dict):
            return {k: self._map_scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._map_scrub(v) for v in value]
        return value

    def _cluster(self, records: list[dict]) -> dict[str, dict]:
        by_id = {r["run_id"]: r for r in records if isinstance(r.get("run_id"), str)}
        buckets: dict[str, dict] = {}

        def add(kind: str, steps: tuple[str, ...], record: dict):
            if not steps:
                return
            signature = ("agents:" if kind == "agent_sequence" else "") + ">".join(steps)
            bucket = buckets.get(signature)
            if bucket is None:
                bucket = {
                    "kind": kind,
                    "signature": signature,
                    "steps": steps,
                    "count": 0,
                    "sessions": set(),
                    "run_ids": [],
                    "seen_runs": set(),
                    "outcomes": defaultdict(int),
                    "ok": 0,
                    "scenarios": [],
                    "scenario_set": set(),
                }
                buckets[signature] = bucket
            run_id = record["run_id"]
            # 同一 run 里同一 n-gram 只计一次，避免长序列重复膨胀
            key = (run_id, signature)
            if key in bucket["seen_runs"]:
                return
            bucket["seen_runs"].add(key)
            bucket["count"] += 1
            if record.get("session_id"):
                bucket["sessions"].add(record["session_id"])
            if run_id not in bucket["run_ids"]:
                bucket["run_ids"].append(run_id)
            status = ((record.get("outcome") or {}).get("status") or "unknown")
            bucket["outcomes"][status] += 1
            if status == "completed":
                bucket["ok"] += 1
            scenario = _scenario(record, by_id)
            if scenario and scenario not in bucket["scenario_set"]:
                bucket["scenario_set"].add(scenario)
                bucket["scenarios"].append(scenario)

        for record in records:
            tools = _tool_sequence(record)
            for gram in _ngrams(tools):
                add("tool_sequence", gram, record)

        for root in build_tree(records):
            children = root.get("children") or []
            agents = tuple(
                c["run"]["agent"]
                for c in children
                if isinstance(c.get("run"), dict) and isinstance(c["run"].get("agent"), str)
            )
            if len(agents) >= MIN_N:
                for gram in _ngrams(list(agents)):
                    add("agent_sequence", gram, root["run"])

        # 收尾：把 set 换成数量和成功率
        finished = {}
        for signature, bucket in buckets.items():
            sessions = len(bucket["sessions"])
            count = bucket["count"]
            finished[signature] = {
                "kind": bucket["kind"],
                "signature": signature,
                "steps": bucket["steps"],
                "count": count,
                "sessions": sessions,
                "success_rate": (bucket["ok"] / count) if count else 0.0,
                "run_ids": bucket["run_ids"],
                "outcomes": dict(bucket["outcomes"]),
                "scenarios": bucket["scenarios"],
            }
        return finished

    def list_candidates(self, status: str | None = None) -> list[dict]:
        store = self.load_store()
        items = store.get("candidates") or []
        if status:
            items = [c for c in items if c.get("status") == status]
        return items

    def get(self, candidate_id: str) -> dict | None:
        for item in self.list_candidates():
            if item.get("id") == candidate_id:
                return item
        return None


def main(argv=None) -> int:
    default_dir = os.path.join(ROOT, "apps", "web", "data")
    parser = argparse.ArgumentParser(description="从轨迹挖掘技能候选（#40）")
    parser.add_argument("command", nargs="?", default="mine", choices=("mine", "list", "show"))
    parser.add_argument("id", nargs="?", help="show 时的候选 id")
    parser.add_argument("--data-dir", default=default_dir)
    parser.add_argument("--min-count", type=int, default=MIN_COUNT)
    parser.add_argument("--min-sessions", type=int, default=MIN_SESSIONS)
    parser.add_argument("--model", action="store_true", help="打开可选的模型归纳（默认关）")
    parser.add_argument("--json", action="store_true", help="list/show 输出完整 JSON")
    args = parser.parse_args(argv)
    miner = Miner(args.data_dir)
    if args.command == "mine":
        result = miner.mine(min_count=args.min_count, min_sessions=args.min_sessions, use_model=args.model)
        print(json.dumps({k: v for k, v in result.items() if k != "model_prompts"}, ensure_ascii=False))
        return 0
    if args.command == "list":
        items = miner.list_candidates()
        if args.json:
            print(json.dumps(items, ensure_ascii=False, indent=2))
        else:
            for item in items:
                ev = item["evidence"]
                print(f"{item['id']}  {item['name']}  ×{ev['count']}/{ev['sessions']}sess  {ev['success_rate']:.0%}  {item['pattern']['signature']}")
            if not items:
                print("(no candidates)", file=sys.stderr)
        return 0
    if args.command == "show":
        if not args.id:
            print("show 需要候选 id", file=sys.stderr)
            return 2
        item = miner.get(args.id)
        if not item:
            print("找不到候选", file=sys.stderr)
            return 1
        print(json.dumps(item, ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
