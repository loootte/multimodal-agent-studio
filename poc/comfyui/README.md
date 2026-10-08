# ComfyUI 出图 / 出视频 POC

这是 [issue #1](https://github.com/loootte/multimodal-agent-studio/issues/1) 的本地脚本。一条命令提交一张固定的 ComfyUI API 工作流，把提示词写进 CLIP 文本节点，等进度，再把文件下载到本地。

它不是聊天界面，也不让模型改工作流。换模板只换 JSON 和旁边的字段对照，命令不变。

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

依赖：Python 3.10+，以及 `websocket-client`（`import websocket`）。没有 WebSocket 时会退回轮询 `/history`。

## 命令

在仓库根目录：

```
python poc/comfyui/comfy_poc.py image --prompt "a red ceramic teapot on a wooden table, soft window light"
python poc/comfyui/comfy_poc.py video --prompt "a red ceramic teapot on a wooden table, the camera slowly pushes in" --frames 17
```

可选参数：`--negative`、`--aspect`（只允许 `1:1`、`16:9`、`9:16`）、`--seed`。视频还可以传 `--seconds`。`--frames` 和 `--seconds` 同时存在时用帧数。`--image 本地图片` 会上传到 ComfyUI 的 input，并连到图生视频的起始帧；不传就是文生视频。

`--print-prompt` 只打印替换后的工作流，不提交。

失败时把 ComfyUI 的 `node_errors` 或执行错误原样打到标准错误，退出码非 0。图片和视频各有超时，写在配置的 `timeout_sec`。

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

画幅：`1:1` 1024×1024，`16:9` 1344×768，`9:16` 768×1344。

节点 1 是模型，6 是 VAEDecode，7 是 SaveImage。这三个不在白名单里。

### 视频 `workflows/video_api.json`

Wan 2.2 I2V A14B Q4 GGUF，高噪和低噪各 2 步，一共 4 步，CFG 1，euler / simple。文本编码器是 `umt5_xxl_fp8_e4m3fn_scaled.safetensors`，类型 `wan`，放在 CPU。默认不接参考图。

| 字段 | 节点 | 输入 | 说明 |
| --- | --- | --- | --- |
| positive_prompt | 8 CLIPTextEncode | text | 正向提示词 |
| negative_prompt | 9 CLIPTextEncode | text | 不传则保留模板默认句 |
| width / height | 11 WanImageToVideo | width, height | 宽高都是 16 的倍数 |
| frames | 11 WanImageToVideo | length | 对齐到 4n+1，不超过 max_frames |
| seed | 12 和 13 KSamplerAdvanced | noise_seed | 两个采样节点用同一个种子 |
| reference_image | 20 LoadImage | image | 只有传入本地参考图时才把节点 20 放进提交图，并让节点 11 的 start_image 指向它。采样用的是节点 11 输出的条件和 latent |

画幅：`1:1` 384×384，`16:9` 512×288，`9:16` 288×512。帧数上限 33，约 2 秒（16 fps）。

其余节点不替换：1 和 4 是 GGUF UNet，2 和 5 是 4 步 LoRA，3 和 6 是 ModelSamplingSD3，7 是 CLIPLoader，10 是 VAE，14 是 VAEDecode，15 是 CreateVideo，16 是 SaveVideo。

模型文件名写在工作流 JSON 里，因为那是导出的内容。换成本机另一张模板时，同时换 `*.fields.json` 里的节点号。命令不用改。
