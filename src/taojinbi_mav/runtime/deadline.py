import time
from dataclasses import dataclass
from typing import Callable


class DeadlineExceeded(TimeoutError):
    def __init__(self, scope):
        super().__init__(scope)
        self.scope = scope


@dataclass(frozen=True)
class Deadline:
    scope: str
    expires_at: float
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep

    @classmethod
    def after(cls, seconds, scope, clock=time.monotonic, sleeper=time.sleep):
        if seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        return cls(scope, clock() + float(seconds), clock, sleeper)

    def remaining(self):
        return max(0.0, self.expires_at - self.clock())

    def checkpoint(self):
        if self.remaining() <= 0:
            raise DeadlineExceeded(self.scope)

    def child(self, seconds, scope):
        if seconds <= 0:
            raise ValueError("deadline seconds must be positive")
        return Deadline(
            scope,
            min(self.expires_at, self.clock() + float(seconds)),
            self.clock,
            self.sleeper,
        )

    def sleep(self, seconds):
        if seconds < 0:
            raise ValueError("sleep seconds must be non-negative")
        self.checkpoint()
        self.sleeper(min(float(seconds), self.remaining()))
        self.checkpoint()
