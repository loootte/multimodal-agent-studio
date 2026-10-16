"""聊天页用到的统一事件。

文字回答是 run.started、若干 message.delta，然后 message.completed 或 run.failed。
网关的工具事件是 tool.requested。运行时在写操作前发出 tool.confirm。网页这一刀仍写成 tool_call。
budget_exceeded 表示步数、时间或 token 预算用尽。
文生图写在同一条助手消息上：content_request，用户提交后再是 tool_call、若干 progress，然后 artifact 或 error。
content_request 也可以收集留在本机的文字或文件。文件不提交给出图。
这条消息再以 message.completed 或 run.failed 结束，流才算读完。content_request 不是结束。
missing_key 表示还没配置 API key。
local_content 表示请求体里出现了本地正文，这次没有发给模型。
"""

EVENT_RUN_STARTED = "run.started"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_MESSAGE_COMPLETED = "message.completed"
EVENT_RUN_FAILED = "run.failed"
EVENT_CONTENT_REQUEST = "content_request"
EVENT_TOOL_REQUESTED = "tool.requested"
EVENT_TOOL_CONFIRM = "tool.confirm"
EVENT_TOOL_CALL = "tool_call"
EVENT_PROGRESS = "progress"
EVENT_ARTIFACT = "artifact"
EVENT_ERROR = "error"

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
}
