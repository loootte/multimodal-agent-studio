"""聊天页用到的统一事件。

文字回答是 run.started、若干 message.delta，然后 message.completed 或 run.failed。
网关的工具事件是 tool.requested。运行时在写操作前发出 tool.confirm。网页把确认画在工具时间线上，并继续把步骤写成 tool_call。
runtime.step 是聊天页运行过程窗口用的步骤，不结束这一轮。
budget_exceeded 表示步数、时间或 token 预算用尽。
文生图写在同一条助手消息上：content_request，用户提交后再是 tool_call、若干 progress，然后 artifact 或 error。
content_request 也可以收集留在本机的文字或文件。文件不提交给出图。
这条消息再以 message.completed 或 run.failed 结束，流才算读完。content_request 不是结束。
missing_key 表示还没配置 API key。
local_content 表示请求体里出现了本地正文，这次没有发给模型。
egress.* 是出站审计事件（ADR 0009，路线图 12.A #57）：每次要离开本进程的请求（对话模型、第三方出图、
本机 ComfyUI 提交）先记 egress.planned，被拦下记 egress.blocked，发出后记 egress.sent，发出失败记 egress.failed。
同一次请求的几条记录共用 egress_id。字段只有 EGRESS_FIELDS 里这些：只记主机名（不记路径、端口以外的地址部分和密钥）、
渠道、模态、字数字节数、消息条数、工具契约字节数、是否含本地派生内容、sha256 指纹前缀、运行标识和拦截原因码。
绝不记正文、提示词、密钥和本地路径。
出站策略（路线图 12.C #59）：每个渠道在 EGRESS_POLICIES 里声明允许的顶层字段；没登记的渠道（other）默认拒绝。
网关先做策略检查，再对规范化后的载荷（NFKC、大小写、去空白和零宽字符、跨片段拼接、常见 base64 / 百分号编码窗）查本地正文。
拦截原因码：local_content（明文或规范化后命中）、local_content_encoded（解码后命中）、egress_policy_denied（字段或渠道不在策略里）。
"""

EVENT_RUN_STARTED = "run.started"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_MESSAGE_COMPLETED = "message.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_CONTENT_REQUEST = "content_request"
EVENT_TOOL_REQUESTED = "tool.requested"
EVENT_TOOL_CONFIRM = "tool.confirm"
EVENT_RUNTIME_STEP = "runtime.step"
EVENT_TOOL_CALL = "tool_call"
EVENT_PROGRESS = "progress"
EVENT_ARTIFACT = "artifact"
EVENT_ERROR = "error"
EVENT_EGRESS_PLANNED = "egress.planned"
EVENT_EGRESS_BLOCKED = "egress.blocked"
EVENT_EGRESS_SENT = "egress.sent"
EVENT_EGRESS_FAILED = "egress.failed"
# 14.B（#79）：运行时压缩了上下文。只带条数和 token 数（compaction.EVENT_FIELDS），不带正文；消费方可以忽略。
EVENT_CONTEXT_COMPACTED = "context.compacted"

EGRESS_EVENTS = (EVENT_EGRESS_PLANNED, EVENT_EGRESS_BLOCKED, EVENT_EGRESS_SENT, EVENT_EGRESS_FAILED)
EGRESS_SCHEMA = "mas.egress/1"
# 出站渠道：对话模型、第三方出图、ComfyUI 提交（通常在本机）。
# other 是给未登记出口留的渠道名：策略为空，一律 egress_policy_denied（12.C #59）。
# toolpack（12.D #60）：第三方工具包在沙箱子进程里自己发起的连接。宿主看不到载荷，只按权限和目的地裁决、记主机名。
# update（13.C #76）：桌面更新检查；策略只放行 version + platform。
EGRESS_CHANNELS = ("chat_completion", "remote_image", "comfyui_local", "toolpack", "update", "other")
# 目的地范围：本机回环、局域网（私有地址）、第三方。
EGRESS_SCOPES = ("local", "lan", "third_party")
# 审计记录允许出现的全部字段。新增字段先在这里登记，并确认它不是正文、提示词、密钥或路径。
EGRESS_FIELDS = (
    "schema", "type", "egress_id", "ts", "channel", "destination_host", "destination_scope",
    "modality", "char_count", "byte_count", "message_count", "tool_contract_bytes",
    "contains_local_derived", "content_fingerprint", "handle_id_prefix", "model",
    "session_id", "trace_id", "run_id", "agent_run_id", "reason_code", "http_status",
    "policy", "exempt_bytes", "tool",
)

# 出站策略（12.C #59）。fields：允许出现的顶层字段；exempt：豁免本地正文检查的字段（工具契约），字节数记进 exempt_bytes；
# message_fields：对话消息允许的字段；max_images：一次请求最多几张图（一次 generate_image 只出一张）；
# raw_upload：允许只有字节、没有 JSON 载荷的请求（ComfyUI 上传参考图）。策略为 None 的渠道一律拒绝。
EGRESS_POLICY_SCHEMA = "mas.egress.policy/1"
EGRESS_POLICIES = {
    "chat_completion": {
        "fields": ("model", "stream", "messages", "tools", "tool_choice", "temperature", "max_tokens", "stop"),
        "exempt": ("tools",),
        "message_fields": ("role", "content", "tool_calls", "tool_call_id", "name"),
        "local_refs": "handle_only",
    },
    "remote_image": {
        "fields": ("model", "prompt", "n", "size", "response_format"),
        "exempt": (),
        "max_images": 1,
    },
    "comfyui_local": {
        "fields": ("prompt", "client_id"),
        "exempt": (),
        "raw_upload": True,
    },
    # 不透明出口：载荷在子进程里，宿主只裁决「有没有 network 授权、是不是回环地址」。没有 network 授权的包一律拒绝。
    # 14.A（#78）：宿主替 MCP 工具包发的 JSON-RPC（HTTP 请求、喂给 stdio server 的消息）也走这个渠道，
    # 只许 jsonrpc / id / method / params 四个顶层字段和下面四个方法；参数照常查本地正文、编码变体和指纹。
    "toolpack": {
        "fields": ("jsonrpc", "id", "method", "params"),
        "methods": ("initialize", "notifications/initialized", "tools/list", "tools/call"),
        "exempt": (),
        "opaque": True,
        "requires_permission": "network",
        "deny_loopback": True,
    },
    # 13.C / #76：桌面更新检查。载荷只允许版本号和平台，不含会话、提示词、密钥、路径。
    "update": {
        "fields": ("version", "platform"),
        "exempt": (),
    },
    "other": None,
}

# 这些键的值是结构标记（角色、类型、模型名、编号），不当作正文参与短正文全文匹配。
EGRESS_STRUCTURAL_KEYS = (
    "role", "type", "model", "id", "tool_call_id", "name", "detail", "size", "stream", "tool_choice",
    "client_id", "class_type", "response_format", "media_type", "version", "platform",
)
# 拦截或失败原因的白话说明（界面只显示这个，不回显正文）。
EGRESS_REASONS = {
    "local_content": "请求里混进了本地内容，已在发出前拦下；本地内容没有离开本机。",
    "local_prompt_remote_refused": "本地提示词默认不发给第三方出图服务，已拦下。",
    "user_declined": "你没有同意把本地提示词发给第三方出图服务，已拦下。",
    "http_error": "对方返回了错误。",
    "connect_error": "连接失败，请求没有送达。",
    "timeout": "等待超时。",
    "cancelled": "已停止。",
    "rejected": "对方拒绝了这次请求。",
    "handle_not_found": "句柄不存在或不属于本会话，已拦下；没有读取任何本地内容。",
    "handle_expired": "本地内容句柄已过期或已撤销，已拦下；本地内容没有离开本机。",
    "handle_forbidden": "本地内容句柄的用途或作用域不允许这次调用，已拦下。",
    "local_content_encoded": "请求里有经过编码（如 base64）的本地内容，已在发出前拦下；本地内容没有离开本机。",
    "egress_policy_denied": "这次请求的字段或出口不在出站策略里，已拦下，没有打开连接。",
    "permission_denied": "这个工具包没有联网权限（或试图连本机地址），已在沙箱里拦下，没有打开连接。",
    "local_fingerprint": "请求里有本地内容的入库指纹，已在发出前拦下；本地内容和它的指纹都没有离开本机。",
}

# 句柄（ADR 0009，路线图 12.B #58）：本地内容句柄是短时能力令牌，绑定会话、用途（purpose）、可选运行和过期时间。
# 消费失败的错误码固定为下面三个；跨会话和伪造的句柄一律当作 handle_not_found（不暴露它在别的会话里存在）。
HANDLE_SCHEMA = "mas.handle/1"
HANDLE_ERRORS = ("handle_not_found", "handle_expired", "handle_forbidden")
HANDLE_STATES = ("active", "expired", "revoked", "used")
# 每种用途默认允许哪些工具消费正文。keep 只存不用：模型拿它出图会得到 handle_forbidden。
HANDLE_PURPOSE_TOOLS = {"image_prompt": ("generate_image",), "keep": ()}

TERMINAL_EVENTS = (EVENT_MESSAGE_COMPLETED, EVENT_RUN_FAILED)

ERROR_CODES = (
    "modality_unsupported",
    "payload_too_large",
    "rate_limited",
    "timeout",
    "cancelled",
    "provider_error",
    "invalid_tool_call",
    "missing_key",
    "local_content",
    "budget_exceeded",
    "handle_not_found",
    "handle_expired",
    "handle_forbidden",
    "local_content_encoded",
    "egress_policy_denied",
    "local_fingerprint",
)

HUMAN = {
    "modality_unsupported": "这个模型不接受这种输入。",
    "payload_too_large": "这条消息太长了。",
    "rate_limited": "请求太频繁，请稍后再试。",
    "timeout": "模型超时了，没有返回完整回答。",
    "cancelled": "已停止。",
    "provider_error": "模型服务返回错误。",
    "invalid_tool_call": "模型返回了无法识别的工具调用。",
    "missing_key": "还没有配置 API key。请在设置里填写。",
    "local_content": "本地内容留在本机，没有发给模型。",
    "budget_exceeded": "超过预算，已经停下。",
    "handle_not_found": "这个内容句柄不属于本会话。",
    "handle_expired": "这个本地内容句柄已过期或已撤销，不能再用。",
    "handle_forbidden": "这个本地内容句柄的用途不允许这样用。",
    "local_content_encoded": "请求里有编码过的本地内容，留在本机，没有发给模型。",
    "egress_policy_denied": "这次请求不在出站策略允许的范围里，没有发出。",
    "local_fingerprint": "请求里有本地内容的指纹，留在本机，没有发给模型。",
}

RETRYABLE = {
    "modality_unsupported": False,
    "payload_too_large": False,
    "rate_limited": True,
    "timeout": True,
    "cancelled": False,
    "provider_error": True,
    "invalid_tool_call": False,
    "missing_key": False,
    "local_content": False,
    "budget_exceeded": False,
    "handle_not_found": False,
    "handle_expired": False,
    "handle_forbidden": False,
    "local_content_encoded": False,
    "egress_policy_denied": False,
    "local_fingerprint": False,
}
