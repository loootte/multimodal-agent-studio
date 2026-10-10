# Multimodal Agent Studio

多模态 Agent 产品的工程蓝图。这个仓库定义目标、边界、架构和路线图。

产品仍按 [ROADMAP.md](ROADMAP.md) 逐段落地。[issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 提前做了本机文字聊天，用来填写第三方大模型的 API key。[issue #10](https://github.com/loootte/multimodal-agent-studio/issues/10) 让这个聊天页成为多模态 Agent 的界面，第一刀只做文生图。用法见 [`apps/web`](apps/web/README.md)。[issue #1](https://github.com/loootte/multimodal-agent-studio/issues/1) 另有一个本地脚本 [`poc/comfyui`](poc/comfyui/README.md)，用来向本机 ComfyUI 提交固定工作流。

## 要做成什么

一个带 Web 界面的 Agent。用户可以用文字、图片、音频、视频和文档与它协作。模型供应商可以更换，Agent 循环、工具和界面不跟着供应商走。

界面不是附属页，而是产品本身：能看对话、能看附件、能看模型正在调用什么工具。

## 不做什么

- 不在这一阶段写完整的网关、运行时或界面。`apps/web` 是 issue #9 的文字聊天，加上 issue #10 的文生图。出图仍调用 `poc/comfyui` 里现成的 `generate_image`，不另写一套 Comfy 客户端。issue #11 让模型调用 `accept_local_content` 收集画面描述，正文留在本机，第三方只看到工具契约和句柄。`poc/comfyui` 是 issue #1 的提交脚本、issue #2 的两个生成工具、issue #3 的白名单模板、issue #4 的 ComfyUI 客户端、issue #5 的会话工件库和 issue #6 的聊天事件。
- 不把某一家模型的 SDK 当成整个系统的中心。
- 不默认上多 Agent 群聊。默认是一个有边界的循环，加上类型明确的工具。
- 不把上传的媒体长期塞进提示词。媒体进工件库，提示词里只放引用。

## 文档

| 文档 | 内容 |
| --- | --- |
| [ROADMAP.md](ROADMAP.md) | 分阶段路线图和每阶段的完成标准 |
| [docs/architecture.md](docs/architecture.md) | 运行时分层、请求怎么走，以及每层现在对应的代码文件 |
| [docs/model-gateway.md](docs/model-gateway.md) | 多模态模型网关要遵守的契约 |
| [docs/interface.md](docs/interface.md) | 界面范围和首版画面 |
| [docs/repository-layout.md](docs/repository-layout.md) | 以后写代码时的目录，现在不创建 |
| [docs/adr/0001-bootstrap-without-code.md](docs/adr/0001-bootstrap-without-code.md) | 为什么先只提交文档 |
| [docs/adr/0002-implementation-stack.md](docs/adr/0002-implementation-stack.md) | 文字聊天这一刀的语言和进程 |

## 状态

路线图第 0 阶段仍在进行。issue #9 按 [ADR 0002](docs/adr/0002-implementation-stack.md) 提前做了文字聊天，issue #10 让同一页面调用已有的文生图工具。这两条都不把第 0、1、3 阶段标成完成。`poc/comfyui` 包含 issue #1 的提交脚本、issue #2 的 `generate_image` / `generate_video`、issue #3 的工作流模板、issue #4 的 ComfyUI 客户端、issue #5 的会话工件库和 issue #6 的聊天事件。
