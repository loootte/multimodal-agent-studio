"""步数、时间和 token 预算。"""

from __future__ import annotations

import time


class Budget:
    def __init__(self, steps: int = 8, time_sec: float = 120, tokens: int = 100000):
        self.steps = int(steps)
        self.time_sec = float(time_sec)
        self.tokens = int(tokens)


class Clock:
    def __init__(self, budget: Budget):
        self.budget = budget
        self.started = time.monotonic()
        self.steps_used = 0
        self.tokens_used = 0

    def check(self) -> str | None:
        if self.steps_used >= self.budget.steps:
            return "超过步数预算，已经停下。"
        if time.monotonic() - self.started >= self.budget.time_sec:
            return "超过时间预算，已经停下。"
        if self.tokens_used >= self.budget.tokens:
            return "超过 token 预算，已经停下。"
        return None

    def add_step(self) -> None:
        self.steps_used += 1

    def add_tokens(self, count: int) -> None:
        if count > 0:
            self.tokens_used += count
