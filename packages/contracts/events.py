"""文字聊天这一刀用到的统一事件。

顺序是 run.started、若干 message.delta，然后 message.completed 或 run.failed。
missing_key 是这一刀多出来的稳定错误码，用来表示还没配置 API key。
"""

EVENT_RUN_STARTED = "run.started"
EVENT_MESSAGE_DELTA = "message.delta"
EVENT_MESSAGE_COMPLETED = "message.completed"
EVENT_RUN_FAILED = "run.failed"

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
)

HUMAN = {
    "modality_unsupported": "这个模型目前只接受文字。",
    "payload_too_large": "这条消息太长了。",
    "rate_limited": "请求太频繁，请稍后再试。",
    "timeout": "模型超时了，没有返回完整回答。",
    "cancelled": "已停止。",
    "provider_error": "模型服务返回错误。",
    "invalid_tool_call": "模型返回了无法识别的工具调用。",
    "missing_key": "还没有配置 API key。请在设置里填写。",
}
