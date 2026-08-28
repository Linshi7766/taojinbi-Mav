from . import TaskProfile


SEARCH_TASK = TaskProfile(
    key="search",
    strategy="search_discovery_browse",
    title_prefix="搜一搜",
    exact_title=None,
    description_required=False,
    allow_dynamic_total=False,
    rotating_title=False,
)
