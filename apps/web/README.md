# 本机文字聊天

这是 [issue #9](https://github.com/loootte/multimodal-agent-studio/issues/9) 的页面。用户把自己的第三方大模型 API key 填进设置，即可进行文字对话。

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

这条命令不访问外网。它用本机的假供应商确认：没有密钥不会发请求、响应里没有完整密钥、流式增量会留下、停止后不再追加、页面断开不会重新提交。
