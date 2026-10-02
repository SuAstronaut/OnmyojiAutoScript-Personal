"""RyouToppa-only settlement coordinates, calibrated for 1280 x 720.

Rectangles use inclusive (left, top, right, bottom) pixel bounds.
No device, application imports, global point sampler or resolution conversion.
"""
from tasks.RealmRaid.settlement_safe import (
    MAX_SAMPLE_ATTEMPTS, point_in_rect, sample_safe_point,
)


BASE_SAFE_AREA = (20, 70, 1260, 700)
REWARD_EXCLUDE = (20, 125, 1260, 325)
TOP_RIGHT_EXCLUDE = (825, 0, 1270, 70)
BOTTOM_LEFT_BUFF_EXCLUDE = (125, 625, 315, 715)
EXCLUDED_AREAS = (
    REWARD_EXCLUDE, TOP_RIGHT_EXCLUDE, BOTTOM_LEFT_BUFF_EXCLUDE,
)


def get_settlement_safe_point(rng=None):
    """Uniform rejection sampling; exhaustion never returns a forbidden point."""
    return sample_safe_point(BASE_SAFE_AREA, EXCLUDED_AREAS, rng,
                             max_attempts=MAX_SAMPLE_ATTEMPTS)
