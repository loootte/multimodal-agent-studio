# 本机聊天

这是 [issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 的页面，也是 [issue #10](https://github.com/loootte/multimodal-agent-studio/issues/10) 的多模态 Agent 界面。用户把自己的第三方大模型 API key 填进设置，即可进行文字对话。要一张图时，第三方先调用 `accept_local_content`。对话框收集画面描述，正文留在本机内容库，再继续调用 `poc/comfyui` 里现成的 `generate_image`。模型填来的提示词、拒绝，或别的会话的句柄，都不会替换这次正文。请求体里一旦出现正文，这次请求不会发出。用户用自己的说法要求留下内容时，同一个工具可以收文字或文件。`list_local_content` 和 `forget_local_content` 只看本会话。文件句柄不会拿去出图。页面不接收工作流 JSON。

手动连本机 ComfyUI 时，沿用 poc 的配置：把 `poc/comfyui/config.example.json` 复制为 `poc/comfyui/config.json`，或设置 `COMFY_URL`、`COMFY_IMAGE_WORKFLOW`、`COMFY_SESSION`、`COMFY_ARTIFACT_DIR`、`COMFY_CONFIG`、`COMFY_OUTPUT_DIR`。默认地址是 `http://127.0.0.1:8188`。可以用 `python poc/comfyui/comfy_client.py check` 看客户端自己的离线检查。这一页不另写一套客户端。

协议是 OpenAI Chat Completions 的流式接口。默认基址 `https://api.x.ai/v1`，默认模型 `grok-4.7`。换成别的兼容服务时，只改基址和模型标识。基址可以不写 `https://`，不要把 `/chat/completions` 写进去。保存后密钥栏会清空，页面上方只留末四位。

## 启动

在仓库根目录：

```
python apps/web/server.py
```

浏览器打开 `http://127.0.0.1:8766`。服务只听本机。

设置里的密钥写到 `apps/web/data/provider.json`，这个目录不进 Git。响应里只剩末四位。也可以不在页面里填：

| 变量 | 作用 |
| --- | --- |
| `LLM_API_KEY` | 任意基址都可用的密钥。页面里保存过的密钥优先 |
| `XAI_API_KEY` | 只在基址主机是 `api.x.ai` 时使用 |
| `CHAT_DATA_DIR` | 换数据目录 |

## 检查

```
python apps/web/check.py
```

这条命令不访问外网，也不连接真正的 ComfyUI。它用本机的假供应商和假 ComfyUI 确认：没有密钥不会发请求、响应里没有完整密钥、流式增量会留下、停止后不再追加、页面断开不会重新提交。文生图还会确认：同一次消息里有工具调用、进度和图片，提交体是白名单工作流，失败没有成片，停止会取消那一次 `prompt_id`，断开后不会再提交一次，纯文字不会调用 ComfyUI。出图前对话框会收下画面描述。供应商请求里没有这段描述，只有句柄；之后的普通聊天也不会把这段描述再发出去。没有出图关键词、但要求把内容留在本机时，对话框也会出现。上传的文件字节不进供应商请求，别的会话不能读取，文件句柄不会提交 ComfyUI。
