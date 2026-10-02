"""Pure settlement sampling explicitly used by the two breakthrough tasks.

Pixel rectangles are inclusive and calibrated for a full 1280 x 720 frame.
RealmRaid uses this geometry; RyouToppa keeps its own thin configuration wrapper.
"""
import random


BASE_SAFE_AREA = (20, 70, 1260, 700)
REWARD_EXCLUDE = (20, 125, 1260, 325)
TOP_RIGHT_EXCLUDE = (825, 0, 1270, 70)
BOTTOM_LEFT_BUFF_EXCLUDE = (125, 625, 315, 715)
EXCLUDED_AREAS = (
    REWARD_EXCLUDE, TOP_RIGHT_EXCLUDE, BOTTOM_LEFT_BUFF_EXCLUDE,
)
MAX_SAMPLE_ATTEMPTS = 64


def point_in_rect(x, y, rect):
    left, top, right, bottom = rect
    return left <= x <= right and top <= y <= bottom


def sample_safe_point(base_area, excluded_areas, rng=None,
                      max_attempts=MAX_SAMPLE_ATTEMPTS):
    """Independent uniform axes with bounded rejection; no unsafe fallback."""
    rng = random if rng is None else rng
    left, top, right, bottom = base_area
    for _ in range(max_attempts):
        x = rng.randint(left, right)
        y = rng.randint(top, bottom)
        if not any(point_in_rect(x, y, rect) for rect in excluded_areas):
            return x, y
    raise RuntimeError('Breakthrough settlement safe sampling exhausted')


def get_settlement_safe_point(rng=None):
    return sample_safe_point(BASE_SAFE_AREA, EXCLUDED_AREAS, rng)
