# Multimodal Agent Studio

多模态 Agent 产品的工程蓝图。这个仓库定义目标、边界、架构和路线图。

产品仍按 [ROADMAP.md](ROADMAP.md) 逐段落地。[issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 提前做了本机文字聊天，用来填写第三方大模型的 API key。[issue #10](https://github.com/loootte/multimodal-agent-studio/issues/10) 让这个聊天页成为多模态 Agent 的界面，第一刀只做文生图。用法见 [`apps/web`](apps/web/README.md)。[issue #1](https://github.com/loootte/multimodal-agent-studio/issues/1) 另有一个本地脚本 [`poc/comfyui`](poc/comfyui/README.md)，用来向本机 ComfyUI 提交固定工作流。

## 要做成什么

一个带 Web 界面的 Agent。用户可以用文字、图片、音频、视频和文档与它协作。模型供应商可以更换，Agent 循环、工具和界面不跟着供应商走。

界面不是附属页，而是产品本身：能看对话、能看附件、能看模型正在调用什么工具。

## 不做什么

- 不在这一阶段写完整的网关、运行时或界面。`apps/web` 是 issue #9 的文字聊天，加上 issue #10 的文生图。出图仍调用 `poc/comfyui` 里现成的 `generate_image`，不另写一套 Comfy 客户端。issue #11 让模型调用 `accept_local_content` 收集画面描述，正文留在本机，第三方只看到工具契约和句柄。issue #12 让同一套工具按语义收集文字或上传文件，并列出、删除本会话内容。文件句柄不送进 ComfyUI。这些都不计入路线图任一阶段的完成。`poc/comfyui` 是 issue #1 的提交脚本、issue #2 的两个生成工具、issue #3 的白名单模板、issue #4 的 ComfyUI 客户端、issue #5 的会话工件库和 issue #6 的聊天事件。
- 不把某一家模型的 SDK 当成整个系统的中心。
- 不默认上多 Agent 群聊。默认是一个有边界的循环，加上类型明确的工具。
- 不把上传的媒体长期塞进提示词。媒体进工件库，提示词里只放引用。

## 文档

| 文档 | 内容 |
| --- | --- |
| [ROADMAP.md](ROADMAP.md) | 分阶段路线图和每阶段的完成标准 |
| [docs/architecture.md](docs/architecture.md) | 运行时分层、出图和本地内容怎么走，以及每层现在对应的代码文件 |
| [docs/model-gateway.md](docs/model-gateway.md) | 多模态模型网关要遵守的契约 |
| [docs/interface.md](docs/interface.md) | 界面范围和首版画面 |
| [docs/repository-layout.md](docs/repository-layout.md) | 以后写代码时的目录，现在不创建 |
| [docs/adr/0001-bootstrap-without-code.md](docs/adr/0001-bootstrap-without-code.md) | 为什么先只提交文档 |
| [docs/adr/0002-implementation-stack.md](docs/adr/0002-implementation-stack.md) | 文字聊天这一刀的语言和进程 |
| [docs/adr/0003-local-data-isolation.md](docs/adr/0003-local-data-isolation.md) | 本地数据隔离：第三方只见工具契约 |
| [docs/agents.md](docs/agents.md) | 多智能体：Agent 清单（mas.agent/1）怎么写、安装导出、运行时边界 |
| [docs/tool-pack-guide.md](docs/tool-pack-guide.md) | 工具包使用和编写指南：能装什么、入口函数、安装配置导出、常见错误 |
| [docs/tool-packs.md](docs/tool-packs.md) | 工具包规范：`tool.json` 清单、后端、权限、安装导出 |
| [docs/tool-code-contract.md](docs/tool-code-contract.md) | 表单工具（module）的代码契约、宿主分级守卫、安装确认 |
| [docs/local-resources.md](docs/local-resources.md) | 本地资源面板：查看、筛选、预览、标签、下载、复用、引用感知删除 |
| [docs/character-image.md](docs/character-image.md) | 角色图片定制：十二项预设/自定义（含人种、画面风格、参考角色）、默认年轻东亚女性、表单与对话出图、安全规则 |
| [docs/adr/0004-tool-governance.md](docs/adr/0004-tool-governance.md) | 工具规范：`toolpacks/*/tool.json` 清单注册，引擎在 `services/tools`，MCP 是进程内投影 |

## 状态

工具参数（ComfyUI 地址、工作流模板和字段对照、默认画幅、步数、种子策略、超时、输出目录）可以在网页右上角“工具”里配置，保存后下一次运行生效，不用重启；`COMFY_*` 环境变量仍然优先。见 [docs/architecture.md](docs/architecture.md) 里的 `apps/web/tool_config.py`。

工具按统一清单封装（[issue #31](https://github.com/loootte/multimodal-agent-studio/issues/31)）：每个工具一份 `toolpacks/*/tool.json`，“工具”对话框按实际工具名列出，可从本机目录或 zip 安装、导出、卸载工具包，配置和密钥不随包走。见 [docs/tool-packs.md](docs/tool-packs.md)；什么样的工具能装、怎么写一个，见 [docs/tool-pack-guide.md](docs/tool-pack-guide.md)。

左侧会话列表每项可以删除（先确认）。会话记录、只属于它的附件字节和生成工件一起删掉；还在回答的会话拒绝删除，要先停下。

路线图第 0、1、2、3、4、7 阶段已完成。[issue #22](https://github.com/loootte/multimodal-agent-studio/issues/22) 补齐工件库、检索和会话记忆。[issue #20](https://github.com/loootte/multimodal-agent-studio/issues/20) 补齐网页附件、工具时间线、确认和失败重试。issue #9 按 [ADR 0002](docs/adr/0002-implementation-stack.md) 提前做了文字聊天，issue #10 让同一页面调用已有的文生图工具。`poc/comfyui` 包含 issue #1 的提交脚本、issue #2 的 `generate_image` / `generate_video`、issue #3 的工作流模板、issue #4 的 ComfyUI 客户端、issue #5 的会话工件库和 issue #6 的聊天事件。
