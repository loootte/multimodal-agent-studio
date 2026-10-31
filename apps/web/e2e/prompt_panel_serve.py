"""#101 浏览器端到端用的假环境：假供应商（promptimage 模式）+ 假 ComfyUI + 临时数据目录的网页服务。

用法：python apps/web/e2e/prompt_panel_serve.py  → 打印 MAS_URL=… 和 PNG=…，一直跑到 Ctrl+C / stdin 关闭。
apps/web/e2e/prompt_panel.cjs 会自己起它；别对正在用的服务（比如 8766）跑。
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "evals"))
sys.path.insert(0, os.path.join(ROOT, "apps", "web"))

import run as evals_run  # noqa: E402
import check  # noqa: E402

SECRET = "E2E-PANEL-PROMPT-4c1d amber fox in tall grass, golden hour"


def main() -> None:
    with evals_run.Harness(live=False) as h:
        h.provider.mode = "promptimage"
        png = check._png_with_text("prompt", json.dumps({"1": {"class_type": "CLIPTextEncode", "inputs": {"text": SECRET}}}))
        path = os.path.join(tempfile.mkdtemp(prefix="e2e-png-"), "comfy.png")
        with open(path, "wb") as handle:
            handle.write(png)
        print(f"MAS_URL={h.base}", flush=True)
        print(f"PNG={path}", flush=True)
        print(f"SECRET={SECRET}", flush=True)
        try:
            sys.stdin.read()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
