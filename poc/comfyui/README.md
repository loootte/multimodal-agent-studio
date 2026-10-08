# ComfyUI 出图 / 出视频 POC

这是 [issue #1](https://github.com/loootte/multimodal-agent-studio/issues/1) 的本地脚本，加上 [issue #3](https://github.com/loootte/multimodal-agent-studio/issues/3) 的可更换模板，[issue #2](https://github.com/loootte/multimodal-agent-studio/issues/2) 的 `generate_image` / `generate_video`，[issue #4](https://github.com/loootte/multimodal-agent-studio/issues/4) 的 ComfyUI 客户端，以及 [issue #5](https://github.com/loootte/multimodal-agent-studio/issues/5) 的会话工件库。一条命令复制模板，只写白名单字段，提交后等进度，再把文件收进当前会话。

它不是聊天界面，也不让模型改工作流。换模板只换配置里的 JSON 和旁边的字段对照，提交代码不动。模型拼出的自由 JSON 不会被 `POST /prompt`。

## 准备

本机的 ComfyUI 和模型需要已经能手工跑通。这个脚本不安装模型。

```
copy config.example.json config.json
```

`config.json` 不进 Git。服务器地址和工作流路径写在这个文件里，也可以用环境变量盖掉：

| 变量 | 作用 |
| --- | --- |
| `COMFY_CONFIG` | 配置文件路径 |
| `COMFY_URL` | ComfyUI 地址 |
| `COMFY_IMAGE_WORKFLOW` | 文生图工作流 JSON |
| `COMFY_VIDEO_WORKFLOW` | 视频工作流 JSON |
| `COMFY_OUTPUT_DIR` | 下载目录 |
| `COMFY_ARTIFACT_DIR` | 工件库目录 |
| `COMFY_SESSION` | 当前会话。不传则用配置里的 `session_id` |

依赖：Python 3.10+，以及 `websocket-client`（`import websocket`）。没有 WebSocket 时会退回轮询 `/history`。

## 客户端

[issue #4](https://github.com/loootte/multimodal-agent-studio/issues/4) 的提交层在 `comfy_client.py`。默认地址是 `http://127.0.0.1:8188`，配置里的 `comfy_url` 或环境变量 `COMFY_URL` 可以换掉它。

`submit` 调用 `POST /prompt`。它拿到 `prompt_id` 后立即返回，不等采样结束。WebSocket `/ws` 接收 `progress`、`executing` 和 `executed`。进度事件带有这次的 `prompt_id`、当前步 `value`、总步 `max`，以及采样百分比。两个 `prompt_id` 的事件分开放，不会写进同一个列表。

WebSocket 断开或连不上时，改查 `GET /history/{prompt_id}`。文件用 `GET /view?filename=&subfolder=&type=output` 下载。`cancel` 先把这次任务移出等待队列，再 `POST /interrupt`，请求里带这次的 `prompt_id`。取消和执行失败都不会记为成功。

缺节点或缺模型时，异常里的句子和载荷都保留 ComfyUI 返回的原文，这里不翻译。

```
python poc/comfyui/comfy_client.py check
```

`check` 不连接 ComfyUI。`comfy_poc.py` 的 `image` 和 `video` 命令仍会等文件下完再退出：它们先调用 `submit`，再等待进度。

## 命令

在仓库根目录：

```
python poc/comfyui/comfy_poc.py check
python poc/comfyui/comfy_poc.py image --prompt "a red ceramic teapot on a wooden table, soft window light"
python poc/comfyui/comfy_poc.py i2i --prompt "a red ceramic teapot on a wooden table, soft window light" --image path\to\reference.png
python poc/comfyui/comfy_poc.py video --prompt "a red ceramic teapot on a wooden table, the camera slowly pushes in" --frames 17
python poc/comfyui/comfy_poc.py video --prompt "a red ceramic teapot on a wooden table, the camera slowly pushes in" --image path\to\reference.png --frames 17
```

`check` 不连接 ComfyUI。它确认对照表里的节点都在，故意写错的节点不会发出请求，自由 JSON 和白名单之外的改动也会在提交前被拒绝。

可选参数：`--negative`、`--aspect`（只允许 `1:1`、`16:9`、`9:16`）、`--seed`、`--steps`。不传 `--steps` 就保留模板里的步数。视频还可以传 `--seconds`。`--frames` 和 `--seconds` 同时存在时用帧数。视频不传 `--image` 用文生视频模板；传入则用图生视频模板，只替换参考图文件名，不往图里加节点。

`--print-prompt` 只打印替换后的工作流，不提交。这是这条脚本的调试口。Agent 工具不会打印 workflow。

## Agent 工具

[issue #2](https://github.com/loootte/multimodal-agent-studio/issues/2) 在这层只暴露两个工具：`generate_image` 和 `generate_video`。模型看不到 ComfyUI 节点。

参数放在 JSON 文件里再调用。`generate_image` 可以带可选的 `seed` 和 `style`。`generate_video` 不接收 `seed`，种子由运行时生成并写回结果。

文生图：

```json
{"prompt":"a red ceramic teapot on a wooden table, soft window light","aspect_ratio":"1:1","seed":7,"style":"photograph"}
```

文生视频不传 `image_ref`。图生视频在同一结构上增加 `image_ref`，值是上一张图的 `artifact_id`。

```json
{"prompt":"a red ceramic teapot on a wooden table, the camera slowly pushes in","duration_sec":1,"aspect_ratio":"16:9"}
```

```
python poc/comfyui/agent_tools.py list
python poc/comfyui/agent_tools.py check
python poc/comfyui/agent_tools.py call generate_image --json-file image-call.json
python poc/comfyui/agent_tools.py call generate_video --json-file video-call.json
```

`style` 只从 `styles.json` 选一句负向风格句，不改正向提示词。不传 `image_ref` 用文生视频模板 `workflows/video_api.json`；传入则用图生视频模板 `workflows/video_i2v_api.json`。标准输出是工具结果 JSON。失败时 `ok` 为 false，退出码为 1，不能当成已经生成。进度和 `prompt_id` 写到标准错误，不带节点名。图片和视频各有超时，写在配置的 `timeout_sec`。

`comfy_poc.py` 失败时把 ComfyUI 的错误原文打到标准错误，退出码非 0。客户端不把这段原文改写成另一句话。

成功的工具结果里有 `artifact_id`，没有服务器上的绝对路径，也没有像素或 base64。`image_ref` 用上一轮的 `artifact_id`。运行时按当前会话把文件取出来，再交给图生视频。

## 工件

[issue #5](https://github.com/loootte/multimodal-agent-studio/issues/5) 把每次成功的生成按会话收进 `artifact_dir`（默认 `artifacts/`，不进 Git）。一条记录有文件、类型、宽高、视频时长、seed、用过的模板、用户原句，以及真正写进工作流的 prompt。

模型和聊天只引用 `artifact_id`。展示地址是 `/sessions/{session_id}/artifacts/{artifact_id}`。会话不对，或者拿着 ComfyUI 的 `/view` 文件名来要，都读不到文件。

```
python poc/comfyui/artifacts.py check
python poc/comfyui/artifacts.py serve
```

`check` 不连接 ComfyUI。`serve` 只听 `127.0.0.1:8765`。会话可以用配置里的 `session_id`，或环境变量 `COMFY_SESSION`。

## 工作流里会被替换的字段

节点编号以这两张 API 工作流为准。调用方只认字段名。

### 文生图 `workflows/image_api.json`

SDXL checkpoint `waiIllustriousSDXL_v160.safetensors`。20 步，euler / normal，CFG 6。

| 字段 | 节点 | 输入 | 说明 |
| --- | --- | --- | --- |
| positive_prompt | 2 CLIPTextEncode | text | 正向提示词 |
| negative_prompt | 3 CLIPTextEncode | text | 不传则保留模板默认句 |
| width / height | 4 EmptyLatentImage | width, height | 由画幅决定 |
| seed | 5 KSampler | seed | |
| steps | 5 KSampler | steps | 不传则保留模板里的 20 |

画幅：`1:1` 1024×1024，`16:9` 1344×768，`9:16` 768×1344。

节点 1 是模型，6 是 VAEDecode，7 是 SaveImage。这三个不在白名单里。

### 图生图 `workflows/image_i2i_api.json`

同一张 SDXL checkpoint。参考图先按画幅缩放，再以 denoise 0.45 重绘。denoise 写死在模板里，不在白名单中。

| 字段 | 节点 | 输入 | 说明 |
| --- | --- | --- | --- |
| positive_prompt | 2 CLIPTextEncode | text | 正向提示词 |
| negative_prompt | 3 CLIPTextEncode | text | 不传则保留模板默认句 |
| width / height | 5 ImageScale | width, height | 由画幅决定 |
| seed | 7 KSampler | seed | |
| steps | 7 KSampler | steps | 不传则保留模板里的 20 |
| reference_image | 4 LoadImage | image | 模板里已经接到节点 5。只替换文件名 |

### 文生视频 `workflows/video_api.json`

Wan 2.2 I2V A14B Q4 GGUF，高噪和低噪各 2 步，一共 4 步，CFG 1，euler / simple。文本编码器是 `umt5_xxl_fp8_e4m3fn_scaled.safetensors`，类型 `wan`，放在 CPU。这张模板没有参考图输入。

| 字段 | 节点 | 输入 | 说明 |
| --- | --- | --- | --- |
| positive_prompt | 8 CLIPTextEncode | text | 正向提示词 |
| negative_prompt | 9 CLIPTextEncode | text | 不传则保留模板默认句 |
| width / height | 11 WanImageToVideo | width, height | 宽高都是 16 的倍数 |
| frames | 11 WanImageToVideo | length | 对齐到 4n+1，不超过 max_frames |
| seed | 12 和 13 KSamplerAdvanced | noise_seed | 两个采样节点用同一个种子 |
| steps | 12 和 13 KSamplerAdvanced | steps | 模板按 4 步准备。采样区间 0–2 和 2–4 不在白名单里 |

画幅：`1:1` 384×384，`16:9` 512×288，`9:16` 288×512。帧数上限 33，16 fps，最长约 2.06 秒。超过上限的请求会在对齐时被截到 33 帧。

### 图生视频 `workflows/video_i2v_api.json`

和文生视频同一套 Wan 节点。节点 20 LoadImage 已经在模板里，并接到节点 11 的 `start_image`。对照表是 `workflows/video_i2v.fields.json`，白名单与文生视频相同，另加 `reference_image`。时长上限同样是 33 帧。

其余节点不替换：1 和 4 是 GGUF UNet，2 和 5 是 4 步 LoRA，3 和 6 是 ModelSamplingSD3，7 是 CLIPLoader，10 是 VAE，14 是 VAEDecode，15 是 CreateVideo，16 是 SaveVideo。

模型文件名写在工作流 JSON 里，因为那是导出的内容。权重文件不进仓库。换成本机另一张模板时，同时换配置和 `*.fields.json` 里的节点号。提交代码不用改。对照表里的节点不存在时，请求不会发到 ComfyUI，错误里写明缺的字段。
