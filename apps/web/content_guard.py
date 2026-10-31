"""兼容入口。内容审核在 services.runtime.content_guard（#93）。"""

from services.runtime.content_guard import (  # noqa: F401
    EXPLICIT,
    RATING_ADULT,
    RATING_GENERAL,
    GuardError,
    check_inputs,
    check_output,
    consent,
    content,
    explicit,
    filter_catalog,
    looks_adult,
    rated,
    under_age,
)
