from dataclasses import dataclass


@dataclass(frozen=True)
class TaskProfile:
    key: str
    strategy: str
    title_prefix: str | None
    exact_title: str | None
    description_required: bool
    allow_dynamic_total: bool
    rotating_title: bool

    def __post_init__(self):
        if (
            isinstance(self.title_prefix, str)
            and self.title_prefix
            and self.exact_title is None
        ):
            return
        if (
            isinstance(self.exact_title, str)
            and self.exact_title
            and self.title_prefix is None
        ):
            return
        raise ValueError(
            "exactly one non-empty title_prefix or exact_title is required"
        )

    def matches_title(self, title):
        if not isinstance(title, str) or not title:
            return False
        if self.exact_title is not None:
            return title == self.exact_title
        return title.startswith(self.title_prefix)

    def safe_label(self):
        """返回标准化脱敏标签：精确标题原样，前缀标题截断为 ``前缀…``。"""
        if self.exact_title is not None:
            return self.exact_title
        return f"{self.title_prefix}…"

    def accepts_row(self, row_text):
        if not self.description_required:
            return True
        return isinstance(row_text, str) and "浏览" in row_text

    def same_identity(self, left, right):
        if not self.matches_title(left) or not self.matches_title(right):
            return False
        if self.rotating_title:
            return True
        return left == right
