# Multimodal Agent Studio

多模态 Agent 产品的工程蓝图。这个仓库定义目标、边界、架构和路线图。

当前没有运行时代码，也没有界面实现。下一阶段按 [ROADMAP.md](ROADMAP.md) 逐段落地。

## 要做成什么

一个带 Web 界面的 Agent。用户可以用文字、图片、音频、视频和文档与它协作。模型供应商可以更换，Agent 循环、工具和界面不跟着供应商走。

界面不是附属页，而是产品本身：能看对话、能看附件、能看模型正在调用什么工具。

## 不做什么

- 不在这个阶段写服务、前端或脚本。
- 不把某一家模型的 SDK 当成整个系统的中心。
- 不默认上多 Agent 群聊。默认是一个有边界的循环，加上类型明确的工具。
- 不把上传的媒体长期塞进提示词。媒体进工件库，提示词里只放引用。

## 文档

| 文档 | 内容 |
| --- | --- |
| [ROADMAP.md](ROADMAP.md) | 分阶段路线图和每阶段的完成标准 |
| [docs/architecture.md](docs/architecture.md) | 运行时分层和请求怎么走 |
| [docs/model-gateway.md](docs/model-gateway.md) | 多模态模型网关要遵守的契约 |
| [docs/interface.md](docs/interface.md) | 界面范围和首版画面 |
| [docs/repository-layout.md](docs/repository-layout.md) | 以后写代码时的目录，现在不创建 |
| [docs/adr/0001-bootstrap-without-code.md](docs/adr/0001-bootstrap-without-code.md) | 为什么先只提交文档 |

## 状态

路线图第 0 阶段：工程基线。本仓库就是这一阶段的交付物。
