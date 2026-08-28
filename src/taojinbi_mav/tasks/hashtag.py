from . import TaskProfile


HASHTAG_TASK = TaskProfile(
    key="hashtag",
    strategy="feed_browse",
    title_prefix="看看#",
    exact_title=None,
    description_required=False,
    allow_dynamic_total=True,
    rotating_title=True,
)
