import re
from dataclasses import dataclass, fields
from enum import Enum, IntEnum


class RunMode(str, Enum):
    DRY_RUN = "dry_run"
    EXECUTE = "execute"


class RunStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    STARTUP_FAILED = "startup_failed"
    SAFETY_STOPPED = "safety_stopped"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


class ExitCode(IntEnum):
    SUCCESS = 0
    PARTIAL = 1
    ARGUMENT_ERROR = 2
    STARTUP_FAILED = 3
    SAFETY_STOPPED = 4
    TIMED_OUT = 5
    CANCELLED = 130


_STATUS_EXIT_CODES = {
    RunStatus.SUCCESS: ExitCode.SUCCESS,
    RunStatus.PARTIAL: ExitCode.PARTIAL,
    RunStatus.STARTUP_FAILED: ExitCode.STARTUP_FAILED,
    RunStatus.SAFETY_STOPPED: ExitCode.SAFETY_STOPPED,
    RunStatus.TIMED_OUT: ExitCode.TIMED_OUT,
    RunStatus.CANCELLED: ExitCode.CANCELLED,
}
_REASON_RE = re.compile(r"^[a-z0-9_]+$")


@dataclass(frozen=True)
class RunCounts:
    detected: int = 0
    supported: int = 0
    skipped: int = 0
    attempted: int = 0
    completed: int = 0
    likely_completed: int = 0
    unfinished: int = 0

    def __post_init__(self):
        if any(getattr(self, item.name) < 0 for item in fields(self)):
            raise ValueError("run counts must be non-negative")


@dataclass(frozen=True)
class RunOutcome:
    mode: RunMode
    status: RunStatus
    reason: str
    counts: RunCounts = RunCounts()

    def __post_init__(self):
        if not isinstance(self.mode, RunMode):
            raise TypeError("mode must be RunMode")
        if not isinstance(self.status, RunStatus):
            raise TypeError("status must be RunStatus")
        if not isinstance(self.counts, RunCounts):
            raise TypeError("counts must be RunCounts")
        if not isinstance(self.reason, str) or _REASON_RE.fullmatch(self.reason) is None:
            raise ValueError("reason must be a stable identifier")

    @property
    def exit_code(self):
        return _STATUS_EXIT_CODES[self.status]
