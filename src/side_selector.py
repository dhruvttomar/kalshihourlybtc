"""
Side selection for cases where both YES (above) and NO (below) sides qualify.

Strategy: pick the side with the larger buffer from spot.
If buffers are within 10% of each other, treat as symmetric and return "either".

No directional trend signals are used. See spec Section 4.3 for rationale.
"""
from __future__ import annotations

_SYMMETRY_THRESHOLD = 0.10  # 10%


def select_side(
    spot: float,
    above_strike: float,
    below_strike: float,
    symmetry_threshold: float = _SYMMETRY_THRESHOLD,
) -> str:
    """
    Choose between a YES-above opportunity and a NO-below opportunity.

    Args:
        spot: Current BTC spot price.
        above_strike: Strike of the YES candidate (spot is above this).
        below_strike: Strike of the NO candidate (spot is below this).
        symmetry_threshold: If |buffer_above - buffer_below| / max(both) is
            less than this, return "either" instead of picking a side.

    Returns:
        "above"  → take the YES position (above above_strike)
        "below"  → take the NO position (below below_strike)
        "either" → buffers are symmetric; either is acceptable
    """
    buffer_above = spot - above_strike
    buffer_below = below_strike - spot

    if buffer_above <= 0 and buffer_below <= 0:
        return "either"  # neither side is actually in the money

    max_buffer = max(buffer_above, buffer_below)
    if max_buffer <= 0:
        return "either"

    relative_diff = abs(buffer_above - buffer_below) / max_buffer
    if relative_diff < symmetry_threshold:
        return "either"

    return "above" if buffer_above > buffer_below else "below"
