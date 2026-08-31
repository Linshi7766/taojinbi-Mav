from . import TaskProfile


IMMERSIVE_GOODS_TASK = TaskProfile(
    key="immersive",
    strategy="feed_browse",
    title_prefix=None,
    exact_title="好物沉浸看",
    description_required=True,
    allow_dynamic_total=True,
    rotating_title=False,
)
