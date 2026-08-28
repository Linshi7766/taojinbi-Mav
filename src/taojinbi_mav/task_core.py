"""Pure task selection and scan-state helpers for the Taobao coin task."""

import re
import time
from dataclasses import dataclass

from taojinbi_mav.runtime.deadline import DeadlineExceeded


EXACT_PURE_BROWSE_TITLES = (
    "发现精选好物",
)

EXTERNAL_APP_MARKERS = (
    "头条",
    "支付宝",
    "蚂蚁森林",
    "蚂蚁庄园",
    "闲鱼",
    "百度",
    "淘宝特价版",
    "点淘",
    "饿了么",
    "菜鸟",
    "微博",
    "一淘",
    "趣头条",
    "天猫",
    "京东",
    "抖音",
)

UNSAFE_ACTION_MARKERS = (
    "视频",
    "下单",
    "购买",
    "邀请",
    "充值",
    "助力",
    "游戏",
    "红包",
    "外卖",
    "拉好友",
    "砸蛋",
    "闯关",
    "消消乐",
    "试玩",
    "捐",
    "评价",
    "抢购",
    "农场",
    "收藏",
    "关注",
    "加购",
    "购物车",
    "领券",
)

EXACT_TASK_ROW_CONTRACTS = {
    ("发现精选好物", "浏览"): "default",
}

TASK_PROGRESS_RE = re.compile(r"(?<!\d)(\d+/\d+)(?!\d)")
REWARD_RE = re.compile(r"^[+＋]\s*\d+")
GENERIC_DESCRIPTION_PREFIXES = (
    "浏览",
    "点击去逛",
    "逛一逛",
)


@dataclass(frozen=True)
class TaskDecision:
    allowed: bool
    reason: str
    handler: str = "default"


@dataclass(frozen=True)
class TaskRowText:
    title: str | None
    description: str
    reward: str
    progress_text: str
    full_text: str


@dataclass(frozen=True)
class TaskSnapshot:
    text: str | None
    progress_text: str = ""
    attempts: int = 0
    legacy_blocked: bool = False


@dataclass(frozen=True)
class ImmersiveRunResult:
    completed: bool
    progress: int
    successful_steps: int
    reason: str
    transitions: tuple[tuple[int, int], ...]
    total_changes: tuple[tuple[int, int], ...] = ()


@dataclass
class TaskScanState:
    error_count: int = 0
    previous_task_ids: frozenset = frozenset()
    unchanged_swipes: int = 0

    def observe_screen(self, task_ids, after_swipe=False):
        current = frozenset(task_ids)
        if not after_swipe:
            self.previous_task_ids = current
            self.unchanged_swipes = 0
            return False
        if current == self.previous_task_ids:
            self.unchanged_swipes += 1
        else:
            self.unchanged_swipes = 0
        self.previous_task_ids = current
        return self.unchanged_swipes >= 2

    def record_success(self):
        self.error_count = 0

    def record_loading(self):
        return self.error_count

    def record_exhausted(self):
        self.error_count += 1
        return self.error_count


def package_contains(package_name, fragment):
    """Return whether a non-empty package name contains ``fragment``."""
    return (
        isinstance(package_name, str)
        and bool(package_name)
        and fragment in package_name
    )


def package_allowed(package_name, allowed_packages=None):
    """Apply an optional exact package allowlist to a non-empty name."""
    if not isinstance(package_name, str) or not package_name:
        return False
    if allowed_packages is None:
        return True
    return package_name in allowed_packages


def _progress_pair(progress_text):
    match = re.fullmatch(r"\s*(\d+)/(\d+)\s*", progress_text or "")
    if not match:
        return None
    value, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or not 0 <= value <= total:
        return None
    return value, total


def _progress_value(progress_text, target):
    pair = _progress_pair(progress_text)
    if pair is None or pair[1] != target:
        return None
    return pair[0]


def run_verified_immersive_progress(
    read_progress,
    perform_one,
    still_allowed,
    target=5,
    max_stalls=2,
    progress_read_retries=2,
    progress_read_delay=0.5,
    allow_dynamic_total=False,
    max_total_changes=2,
    missing_progress_reason=None,
    sleeper=time.sleep,
    checkpoint=lambda: None,
):
    """Run bounded browse rounds and verify progress after each return.

    The task list can render its anchor before the task row/progress text.  A
    bounded retry after a browse round prevents one transient OCR miss from
    terminating the task, while keeping the fail-closed behavior when the
    progress remains unreadable.

    ``checkpoint`` runs before and after every read/enter/return/wait so a
    ``DeadlineExceeded`` raised from it (or from a callback) always bubbles up
    instead of being relabeled as a device error.
    """
    checkpoint()
    try:
        initial_text = read_progress()
    except DeadlineExceeded:
        raise
    except Exception:
        return ImmersiveRunResult(False, 0, 0, "device_io_error", ())
    checkpoint()
    initial_pair = _progress_pair(initial_text)
    if initial_pair is None:
        return ImmersiveRunResult(False, 0, 0, "missing_progress", ())
    if allow_dynamic_total:
        current, current_target = initial_pair
    else:
        current = initial_pair[0] if initial_pair[1] == target else None
        current_target = target
    if current is None:
        return ImmersiveRunResult(False, 0, 0, "missing_progress", ())

    transitions = []
    total_changes = []
    successful_steps = 0
    stalls = 0
    max_total_changes = max(0, max_total_changes)
    while current < current_target:
        checkpoint()
        try:
            allowed = still_allowed()
        except DeadlineExceeded:
            raise
        except Exception:
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "device_io_error",
                tuple(transitions),
            )
        checkpoint()
        if not allowed:
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "unsafe_package",
                tuple(transitions),
            )
        checkpoint()
        try:
            performed = perform_one()
        except DeadlineExceeded:
            raise
        except Exception:
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "device_io_error",
                tuple(transitions),
            )
        checkpoint()
        if not performed:
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "no_safe_control",
                tuple(transitions),
            )
        observed = None
        observed_target = current_target
        read_error = False
        saw_total_mismatch = False
        for attempt in range(max(0, progress_read_retries) + 1):
            checkpoint()
            try:
                progress_text = read_progress()
            except DeadlineExceeded:
                raise
            except Exception:
                read_error = True
                pair = None
            else:
                pair = _progress_pair(progress_text)
            checkpoint()
            if pair is not None:
                if allow_dynamic_total or pair[1] == target:
                    observed, observed_target = pair
                    break
                saw_total_mismatch = True
            if attempt < progress_read_retries and progress_read_delay > 0:
                checkpoint()
                sleeper(progress_read_delay)
                checkpoint()
        if observed is None:
            if read_error:
                reason = "device_io_error"
            elif saw_total_mismatch:
                reason = "progress_total_mismatch"
            else:
                reason = "missing_progress"
            if reason == "missing_progress" and callable(missing_progress_reason):
                checkpoint()
                try:
                    candidate = missing_progress_reason()
                except DeadlineExceeded:
                    raise
                except Exception:
                    candidate = None
                checkpoint()
                if (
                    isinstance(candidate, str)
                    and candidate not in {"", "ok", "not_started"}
                ):
                    reason = candidate
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                reason,
                tuple(transitions),
            )
        if allow_dynamic_total and observed_target != current_target:
            total_changes.append((current_target, observed_target))
            if len(total_changes) > max_total_changes:
                return ImmersiveRunResult(
                    False,
                    observed,
                    successful_steps,
                    "task_rotated",
                    tuple(transitions),
                    tuple(total_changes),
                )
            # The row may have rotated to another random denominator.  Start
            # from the latest observed pair instead of accumulating cycles.
            current = observed
            current_target = observed_target
            stalls = 0
            continue
        transitions.append((current, observed))
        if observed > current:
            successful_steps += 1
            current = observed
            stalls = 0
            continue
        if observed < current:
            # 进度回落（常见于任务完成后计数重置进入下一周期）：视为疑似完成，
            # 停止而非误判停滞（本模型内进度只升不降，降了就说明本周期已结）。
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "progress_reset",
                tuple(transitions),
            )
        stalls += 1
        if stalls >= max_stalls:
            return ImmersiveRunResult(
                False,
                current,
                successful_steps,
                "stalled",
                tuple(transitions),
            )
    return ImmersiveRunResult(
        True,
        current,
        successful_steps,
        "completed",
        tuple(transitions),
        tuple(total_changes),
    )


def wait_for_package_name(
    getter,
    timeout=5,
    poll_interval=0.2,
    clock=time.monotonic,
    sleeper=time.sleep,
):
    """Poll a current-app getter until it yields a non-empty package name."""
    deadline = clock() + timeout
    while clock() < deadline:
        current_app = getter()
        if isinstance(current_app, str):
            package_name = current_app
        elif isinstance(current_app, (tuple, list)) and current_app:
            package_name = current_app[0]
        else:
            package_name = None
        if isinstance(package_name, str) and package_name:
            return package_name
        sleeper(poll_interval)
    return None


def _ordered_unique_text(values, button_text=""):
    result = []
    for value in values:
        text = value.strip() if isinstance(value, str) else ""
        if not text or text == button_text or text in result:
            continue
        result.append(text)
    return result


def parse_task_row_texts(values, button_text=""):
    texts = _ordered_unique_text(values, button_text=button_text)
    progress_text = ""
    cleaned = []
    for text in texts:
        match = TASK_PROGRESS_RE.search(text)
        if match and not progress_text:
            progress_text = match.group(1)
        without_progress = TASK_PROGRESS_RE.sub("", text)
        without_progress = without_progress.replace("()", "").strip()
        if without_progress:
            cleaned.append(without_progress)

    reward = next((text for text in cleaned if REWARD_RE.match(text)), "")
    content = [text for text in cleaned if text != reward]
    first = content[0] if content else ""
    title = (
        None
        if not first
        or any(first.startswith(prefix) for prefix in GENERIC_DESCRIPTION_PREFIXES)
        else first
    )
    description_parts = content[1:] if title else content
    description = " ".join(description_parts)
    full_parts = [part for part in (title, description, reward) if part]
    return TaskRowText(
        title=title,
        description=description,
        reward=reward,
        progress_text=progress_text,
        full_text=" ".join(full_parts),
    )


def classify_task(task_text, attempts=0, legacy_blocked=False):
    """Classify a task using fail-closed, exclusion-first rules."""
    text = task_text.strip() if isinstance(task_text, str) else ""
    if not text:
        return TaskDecision(False, "missing_text")
    if attempts >= 2:
        return TaskDecision(False, "retry_limit")
    if any(marker in text for marker in EXTERNAL_APP_MARKERS):
        return TaskDecision(False, "external_app")
    if any(marker in text for marker in UNSAFE_ACTION_MARKERS):
        return TaskDecision(False, "unsafe_action")
    if legacy_blocked:
        return TaskDecision(False, "safety_blacklist")
    if text not in EXACT_PURE_BROWSE_TITLES:
        return TaskDecision(False, "not_pure_browse")
    return TaskDecision(True, "allowed")


def classify_task_row(row, attempts=0, legacy_blocked=False):
    if not isinstance(row, TaskRowText):
        return TaskDecision(False, "missing_text")
    if not row.title or not row.description:
        return TaskDecision(False, "missing_text")
    text = row.full_text
    if attempts >= 2:
        return TaskDecision(False, "retry_limit")
    if any(marker in text for marker in EXTERNAL_APP_MARKERS):
        return TaskDecision(False, "external_app")
    if any(marker in text for marker in UNSAFE_ACTION_MARKERS):
        return TaskDecision(False, "unsafe_action")
    if legacy_blocked:
        return TaskDecision(False, "safety_blacklist")
    handler = EXACT_TASK_ROW_CONTRACTS.get(
        (row.title, row.description)
    )
    if handler is None:
        return TaskDecision(False, "not_pure_browse")
    return TaskDecision(True, "allowed", handler)


def consume_task_attempt(attempts, key, limit=2):
    """Consume one bounded task attempt before any refresh or click."""
    if not isinstance(key, str) or not key or attempts.get(key, 0) >= limit:
        return False
    attempts[key] = attempts.get(key, 0) + 1
    return True


def legacy_blacklist_text(task_text):
    """Return normalized text; no task bypasses the legacy blacklist."""
    text = task_text if isinstance(task_text, str) else ""
    return " ".join(text.split())


def choose_first_allowed(snapshots):
    """Return the first allowed index and all fail-closed decisions."""
    decisions = [
        classify_task(
            snapshot.text,
            attempts=snapshot.attempts,
            legacy_blocked=snapshot.legacy_blocked,
        )
        for snapshot in snapshots
    ]
    first_index = next(
        (
            index
            for index, decision in enumerate(decisions)
            if decision.allowed
        ),
        None,
    )
    return first_index, decisions


def task_identifier(task_text, progress_text=""):
    """Build a stable visible identifier without using screen coordinates."""
    text = task_text.strip() if isinstance(task_text, str) else ""
    if not text:
        return None
    progress = (
        progress_text.strip()
        if isinstance(progress_text, str)
        else ""
    )
    return f"{text}|{progress}" if progress else text
