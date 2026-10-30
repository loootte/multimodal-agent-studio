"""兼容入口（#93）：内容守卫已移到 services/runtime/content_guard.py（运行时的一部分，不属于网页宿主）。
旧的 `import content_guard` 拿到的就是同一个模块对象（GuardError 等是同一个类）。"""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.runtime import content_guard as _guard  # noqa: E402

sys.modules[__name__] = _guard
