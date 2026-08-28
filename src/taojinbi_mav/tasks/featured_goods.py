from . import TaskProfile


FEATURED_GOODS_TASK = TaskProfile(
    key="featured_goods",
    strategy="feed_browse",
    title_prefix=None,
    exact_title="发现精选好物",
    description_required=True,
    allow_dynamic_total=False,
    rotating_title=False,
)
