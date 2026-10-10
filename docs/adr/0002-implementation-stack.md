# ADR 0002：文字聊天这一刀的实现栈

## 状态

已接受。

## 背景

[issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 需要一个能打开的聊天页，并让用户填入第三方大模型的 API key。路线图本来要等第 0 阶段完成后再选语言。这一刀提前做，是因为没有页面就没法把密钥接进去对话。

## 决定

- 语言继续用 Python 3.10+。聊天进程只用标准库，不引入供应商 SDK，也不引入前端构建工具。
- 一个进程同时提供静态页面和本机 API，只监听 `127.0.0.1`。目录按 `docs/repository-layout.md`：`apps/web`、`services/gateway`、`packages/contracts`。
- 供应商协议用 OpenAI Chat Completions 的流式接口，`POST {base_url}/chat/completions`。默认基址 `https://api.x.ai/v1`，默认模型 `grok-4.7`。换基址和模型标识即换兼容服务。
- 密钥由用户在页面提交，写进不进 Git 的本地文件。未填写时，`LLM_API_KEY` 可用于任意基址；`XAI_API_KEY` 只用于 `api.x.ai`。源码、日志和接口响应里不放完整密钥。

## 后果

- 第 0、1、3 阶段的完成标准都不因这一刀而勾掉。图片和音频输入、Agent 循环、工具确认、登录仍未做。
- [issue #10](https://github.com/loootte/multimodal-agent-studio/issues/10) 沿用这个进程和这条协议。聊天页要图时调用 `poc/comfyui` 里现成的 `generate_image`，不另写 Comfy 客户端，也不把 poc 搬进 `services/`。这仍不勾掉任一阶段。
- [issue #11](https://github.com/loootte/multimodal-agent-studio/issues/11) 仍用这个进程。模型用 `accept_local_content` 收集画面描述，正文留在本机，第三方只看到工具契约和句柄。这仍不勾掉任一阶段。
- [issue #12](https://github.com/loootte/multimodal-agent-studio/issues/12) 仍用这个进程。语义唤起可以收集文字或上传文件，列出和删除只作用于本会话。文件句柄不提交 ComfyUI。这仍不勾掉任一阶段。
- 本地数据隔离见 [ADR 0003](0003-local-data-isolation.md)。第三方模型只使用工具契约，本地数据由专用工具管理，外部访问没有授权就拒绝。这仍不勾掉任一阶段。
- 以后若要换框架或增加供应商专用协议，另写架构决定。适配器仍只放在 `services/gateway`。
