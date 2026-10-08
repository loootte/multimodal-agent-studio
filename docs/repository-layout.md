# 以后的目录

这些目录现在不创建，避免空的代码树被当成已经开工。第 1 阶段开始时再按这张表加进去。

```
apps/web/                 Web 界面
services/gateway/         模型网关和供应商适配器
services/runtime/         Agent 循环、预算、确认
services/tools/           工具实现
packages/contracts/       网关事件、工具模式、错误码
packages/artifacts/       工件读写
evals/                    固定任务
ops/                      运行手册和部署说明
```

规则：

- `packages/contracts` 是界面、运行时和网关之间的唯一共享语言。不要在每一边各写一套事件名。
- 供应商 SDK 只允许出现在 `services/gateway`。
- 界面不要导入运行时的内部模块。它通过运行 API 说话。
- `evals/` 调用对外的运行 API，不调用模型适配器的私有函数。
- 密钥文件、本地数据库和上传的原始媒体不进 Git。

语言和框架留到第 1 阶段的架构决定里再定。这份蓝图不绑定某一门语言。选定之后补 `docs/adr/0002-implementation-stack.md`，并说明如何满足模型契约和界面路径。

## 例外：issue #1

`poc/comfyui/` 是一张本地脚本，用来把提示词打进固定的 ComfyUI API 工作流，并按会话把生成结果收成工件。它不是 `apps/` 或 `services/`，也不提前决定第 1 阶段的语言。换工作流 JSON 时，调用命令不变。工件文件在 `poc/comfyui/artifacts/`，不进 Git。
