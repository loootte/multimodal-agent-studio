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

EGRESS_EVENTS = (EVENT_EGRESS_PLANNED, EVENT_EGRESS_BLOCKED, EVENT_EGRESS_SENT, EVENT_EGRESS_FAILED)
EGRESS_SCHEMA = "mas.egress/1"
# 出站渠道：对话模型、第三方出图、ComfyUI 提交（通常在本机）。
EGRESS_CHANNELS = ("chat_completion", "remote_image", "comfyui_local")
# 目的地范围：本机回环、局域网（私有地址）、第三方。
EGRESS_SCOPES = ("local", "lan", "third_party")
# 审计记录允许出现的全部字段。新增字段先在这里登记，并确认它不是正文、提示词、密钥或路径。
EGRESS_FIELDS = (
    "schema", "type", "egress_id", "ts", "channel", "destination_host", "destination_scope",
    "modality", "char_count", "byte_count", "message_count", "tool_contract_bytes",
    "contains_local_derived", "content_fingerprint", "handle_id_prefix", "model",
    "session_id", "trace_id", "run_id", "agent_run_id", "reason_code", "http_status",
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
}
