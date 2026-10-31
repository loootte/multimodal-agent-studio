# 从图片读出嵌入提示词（issue #101）

内置宿主工具 `prompt_from_image`：在**对话里**唤醒，弹出本机选图，从图片元数据抽出**正向提示词**，登记为本地 `image_prompt` 句柄，并**在本机打开编辑面板**：你在面板里改提示词，然后选「出图」或「保存」。模型只看到 `content_handle` / `source` / `found`，**永远看不到提示词正文**（原文和你改过的都看不到）。

v1 **不做**：侧栏入口（面板只由对话唤起）、负向提示词 / 采样参数编辑、完整 ComfyUI workflow 图解析、从本地资源或已生成文件再读入（本工具只接受流程里用户新选/上传的文件）。

## 怎么用

对话里说「从这张图读出提示词」「把图里的 A1111 / ComfyUI 提示词抽出来」等。模型调用 `prompt_from_image`（无参数）→ 本机文件选择框 → 宿主在本机解析 → 有提示词则登记句柄并告诉模型；没有则 `found=false`，模型再调用 `accept_local_content`（`purpose=image_prompt`）让你手写。

读到提示词后，宿主推一条 `tool.panel` 事件（只有工具名、句柄、来源），网页自动打开「编辑读出的提示词」面板；这条回复下面也有「编辑提示词」按钮，关掉后可以再打开。即使你这句话里说了要出图，也不会自动出图：出图由你在面板里点。

面板里：

- 文本框里是读出的正向提示词，可以随便改（最多 8000 字）。
- **保存**：原句柄原地更新正文（句柄不变，后面用这枚句柄出图就是新正文）。改之前的正文只留 sha256 指纹给出口检查，不留正文。
- **出图**：先保存，再按这枚句柄出图（张数 1–20、画幅、本机 ComfyUI / 第三方）。走的是和表单工具 `privacy=local` 同一条宿主出图路径：对话里只记一行「读图提示词出图（本地提示词，N 张，本机 ComfyUI）」，不经对话模型；选第三方时照旧按 `remote_local_prompt`（默认 `refuse`；`ask` 时逐句柄逐主机确认）。
- 要等读图那一轮结束才能点「出图」（可以先保存）；只认读图所在的会话。

### 面板怎么接进来（通用机制）

| 部分 | 作用 |
| --- | --- |
| 清单 `ui.panel.launcher: "chat"` | 没有侧栏入口，只能由对话里的 `tool.panel` 事件打开（`panels.js` 的 `MASPanels.openFor`） |
| 清单 `ui.panel.local_draft: true` | 只对**内置**、非 iframe 的面板生效：面板拿到 `api.draft`（`read` / `save` / `generate`），宿主接口另外核对句柄来源 |
| `GET/POST /api/sessions/<id>/tools/<name>/drafts/<c_…>` | 读 / 保存草稿正文；只认本会话、`purpose=image_prompt`、`origin=<name>`、仍有效的文字句柄，别的一律 404 |
| `POST …/drafts/<c_…>/generate` | `{text?, count, aspect_ratio, backend}`，先保存再出图，返回事件流 |

面板脚本本身（`toolpacks/builtin/prompt_from_image/ui/panel.js`）不 `fetch`、不写 `/api/`，只经宿主给的 `api.draft` 说话；`apps/web/static/` 里没有这个工具的代码。

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
- 出图仍走宿主的 `generate_image` 路径：`remote_local_prompt` 默认 `refuse`、第三方逐句柄逐主机确认、句柄范围等闸门不变；面板出图不经对话模型。
- 正文只在本机网页 ↔ 本机宿主之间往返（面板读/存草稿），会话视图、事件、对话历史、供应商请求里都没有。
- 读到后模型要的任何工具调用（比如 `generate_image`）都不执行，出图只由用户在面板里点。
- 不把选中的图存成 `keep` 文件；只把抽出的正向文字登记为 `image_prompt`。

## 实现位置

| 路径 | 作用 |
| --- | --- |
| `toolpacks/builtin/prompt_from_image/tool.json` | 宿主工具清单（无参数、`entry.kind=host`） |
| `toolpacks/builtin/prompt_from_image/extract.py` | 纯函数抽取，供宿主与单测 |
| `toolpacks/builtin/prompt_from_image/ui/panel.js`、`panel.css` | 编辑面板（`launcher=chat`、`local_draft`） |
| `apps/web/chat_run.py` `draft_read` / `draft_save` / `start_tool_draft` | 草稿读写与面板出图 |
| `apps/web/store.py` `update_content` / `content_origin` | 原句柄改正文、句柄来源 |
| `apps/web/static/panels.js`、`app.js` | 通用：`tool.panel` 事件打开面板、`api.draft`、`launcher=chat` 不进侧栏 |
| `apps/web/chat_run.py` `_continue_prompt_from_image` | 选图等待、登记句柄（`origin=prompt_from_image`）、推 `tool.panel`、把无正文结果交回模型 |
| `services/tools/packs.py` `HOST_TOOLS` | 内置宿主名单 |
| `services/runtime/agents.py` `ORCHESTRATOR_ONLY` | 子 Agent 不可见 |
| `agents/builtin/orchestrator/agent.json` | 编排者工具列表 |

相关：出图技能 [image-skill.md](image-skill.md)、本地资源 [local-resources.md](local-resources.md)、角色定制面板样板 [character-image.md](character-image.md)（同样的面板机制；本工具的面板只由对话唤起）。

## 检查与评测

- `apps/web/check.py` `_prompt_from_image_case` / `_prompt_draft_case`：读到就推 `tool.panel`、说要出图也不自动出图、草稿接口拒绝不相干句柄、编辑+保存原句柄更新、编辑+出图按新正文提交 ComfyUI 且不经模型；原文和改后的正文都不在事件、会话视图、后续对话历史和任何供应商请求里。
- `evals/tasks.json`：`security.prompt_from_image_no_body`、`runtime.prompt_from_image_extract`、`security.prompt_draft_edit_save`、`security.prompt_draft_edit_generate`。
