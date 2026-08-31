from .featured_goods import FEATURED_GOODS_TASK
from .hashtag import HASHTAG_TASK
from .immersive import IMMERSIVE_GOODS_TASK
from .search import SEARCH_TASK


_PROFILES = (
    SEARCH_TASK,
    HASHTAG_TASK,
    FEATURED_GOODS_TASK,
    IMMERSIVE_GOODS_TASK,
)


def registered_profiles():
    return _PROFILES


def profile_for_title(title):
    for profile in _PROFILES:
        if profile.matches_title(title):
            return profile
    return None


def profile_for_row(title, row_text):
    profile = profile_for_title(title)
    if profile is None or not profile.accepts_row(row_text):
        return None
    return profile
