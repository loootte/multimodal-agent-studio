# 本地资源管理（#51）

网页左侧栏“本地资源”打开一个面板，列出这台机器上存着的本地资源：

| 类型 | 是什么 |
| --- | --- |
| `prompt` | 出图用的本地提示词（`accept_local_content`，purpose=image_prompt 或随消息提交） |
| `text` | 其它本地文字 |
| `file` | 本地文件 |
| `attachment` | 聊天附件（字节按校验和共享） |
| `artifact` | 生成的图片等产物（ComfyUI 产物库） |

每条显示类型、创建时间、大小、引用它的会话和标签；面板顶上显示总占用。工具和 Agent 的管理仍在右上角“工具”/“Agent”，这里不重复。

## 能做什么

- **筛选和搜索**：按类型、按会话（包括与该会话共享字节的条目），按标签/文件名/句柄搜索。
- **预览**：文字在浏览器本地显示，图片和文件走 `/api/resources/raw`。预览内容**不会**发给模型，也不进轨迹。
- **标签**：给资源起个名字，方便搜索。
- **下载**：`/api/resources/raw?id=…&download=1`。
- **复用**：把提示词/文字/文件复制到当前会话，聊天框里只插入句柄（如“用本地提示词 c_… 出图”），模型只看得到句柄。
- **删除 / 批量删除**：先返回 409 `needs_confirm` 和警告（例如字节被别的会话共享、产物被消息引用），确认后才删。共享的字节在最后一个引用删除时才回收。正在运行的会话里的资源会被拒绝删除。删除后该会话的轨迹会被清掉并重新生成，不会留下已删正文的痕迹。

## 接口

全部只接受 127.0.0.1 的请求，其它来源返回 403。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/resources?type=&session=&q=` | 清单和总占用 |
| GET | `/api/resources/preview?id=` | 文字预览 |
| GET | `/api/resources/raw?id=[&download=1]` | 原始字节 |
| POST | `/api/resources/label` | `{id, label}` |
| POST | `/api/resources/delete` | `{ids, confirm}` |
| POST | `/api/resources/reuse` | `{id, session_id}` → `{handle, snippet}` |

资源 id 形如 `type:会话id:句柄`。代码在 `apps/web/resources.py`，测试在 `apps/web/check.py` 的 `_resources_case`。
