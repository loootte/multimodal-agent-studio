# 固定评测

一条命令，给出每个任务通过与否；全部通过退出码为 0，否则为 1。

```
python evals/run.py              # 默认：假供应商 + 假 ComfyUI，不访问外网，可重复
python evals/run.py --json       # 机器可读
python evals/run.py --report evals/last.json
python evals/run.py --only text_to_image security
python evals/run.py --live       # 核心任务走真实供应商和真实 ComfyUI
```

任务定义在 `tasks.json`。每个任务有 `id`、`category`、`kind`（由 `run.py` 里的 `RUNNERS` 执行）、
假件模式的期望 `expect`，以及 `--live` 时放宽后的 `live_expect`。

假件来自 `apps/web/check.py`（`ProviderHandler`、`ComfyHandler`），评测和检查用的是同一套假件。

## 类别

| 类别 | 内容 |
| --- | --- |
| text | 纯文字一轮，不碰 ComfyUI |
| image_input | 带图片附件，图片送到供应商，不弹本地内容窗口 |
| text_to_image | 画面描述只在本机，ComfyUI 恰好提交一次，出一张图 |
| security | 本地内容不出本机、写工具必须确认、工具结果里的注入不触发工具（见 `docs/security-checklist.md`），只用假件 |

## --live

需要环境变量：

- `EVAL_BASE_URL`、`EVAL_API_KEY`、`EVAL_MODEL`：真实供应商（OpenAI 兼容）；
- `COMFY_URL` 或 `COMFY_CONFIG`：真实 ComfyUI。

live 下模型输出不固定，所以只看状态、是否有回答、是否出图；标了 `fake_only` 的任务仍用假件。
