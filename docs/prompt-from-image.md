# 从图片读出嵌入提示词（issue #101）

内置宿主工具 `prompt_from_image`：在**对话里**唤醒，弹出本机选图，从图片元数据抽出**正向提示词**，登记为本地 `image_prompt` 句柄。模型只看到 `content_handle` / `source` / `found`，**永远看不到提示词正文**。用户可在「本地资源」里改提示词，再调用现有 `generate_image` 出图。

v1 **不做**：侧栏面板、负向提示词 / 采样参数编辑、完整 ComfyUI workflow 图解析、从本地资源或已生成文件再读入（本地资源浏览器已能出图；本工具只接受流程里用户新选/上传的文件）。

## 怎么用

对话里说「从这张图读出提示词」「把图里的 A1111 / ComfyUI 提示词抽出来」等。模型调用 `prompt_from_image`（无参数）→ 本机文件选择框 → 宿主在本机解析 → 有提示词则登记句柄并告诉模型；没有则 `found=false`，模型再调用 `accept_local_content`（`purpose=image_prompt`）让你手写。

抽出后若你这句话本身就要出图，模型会接着用句柄调用 `generate_image`（多张先 `plan_images`）。只想抽提示词时说清楚「只抽 / 不要出图」。

## 支持的格式（v1）

| 来源 | 说明 |
| --- | --- |
| PNG tEXt / iTXt / zTXt `parameters` | Automatic1111 / Forge 风格；只取「Negative prompt:」之前的正向段 |
| PNG `prompt` | ComfyUI API 格式 JSON；只扫 `CLIPTextEncode` 的 `text`，取最长一段作为正向；**不做** UI workflow 深解 |
| JPEG / WebP EXIF | `UserComment` / `ImageDescription` 尽力；失败当无提示词 |

不做：NovelAI 隐写、LSB、视频、PSD。

## 隐私

- 图片字节与提示词正文只在本机；工具结果、事件流、供应商请求都不得出现正文或原图字节。
- 子 Agent 拿不到本工具（与 `accept_local_content` 同属编排者专用）。
- 出图仍走内置 `generate_image`：确认卡、`remote_local_prompt` 默认 `refuse`、句柄范围等闸门不变。
- 不把选中的图存成 `keep` 文件；只把抽出的正向文字登记为 `image_prompt`。

## 实现位置

| 路径 | 作用 |
| --- | --- |
| `toolpacks/builtin/prompt_from_image/tool.json` | 宿主工具清单（无参数、`entry.kind=host`） |
| `toolpacks/builtin/prompt_from_image/extract.py` | 纯函数抽取，供宿主与单测 |
| `apps/web/chat_run.py` | 选图等待、登记句柄、把无正文结果交回模型 |
| `services/tools/packs.py` `HOST_TOOLS` | 内置宿主名单 |
| `services/runtime/agents.py` `ORCHESTRATOR_ONLY` | 子 Agent 不可见 |
| `agents/builtin/orchestrator/agent.json` | 编排者工具列表 |

相关：出图技能 [image-skill.md](image-skill.md)、本地资源 [local-resources.md](local-resources.md)、角色定制面板样板 [character-image.md](character-image.md)（本工具 v1 **无**面板）。
