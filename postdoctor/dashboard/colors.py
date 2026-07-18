"""色計算ユーティリティ（dataviz skill の sequential ramp 準拠）。

light は palette.md で定義済みの13段階(blue, 100->700)を区分線形補間する。
dark は palette.md にsequential版が無いため、暗いサーフェス色から
カテゴリカルパレットのdark blue(#3987e5)へ補間して生成する。
"""

from __future__ import annotations

LIGHT_RAMP = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7",
    "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]
DARK_SURFACE = "#1a1a19"
DARK_MAX = "#3987e5"

EMPTY_LIGHT = "#f3f2ef"
EMPTY_DARK = "#242422"


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c))):02x}" for c in rgb)


def _lerp_hex(c1: str, c2: str, t: float) -> str:
    r1, g1, b1 = _hex_to_rgb(c1)
    r2, g2, b2 = _hex_to_rgb(c2)
    return _rgb_to_hex((r1 + (r2 - r1) * t, g1 + (g2 - g1) * t, b1 + (b2 - b1) * t))


def sequential_light(t: float) -> str:
    t = max(0.0, min(1.0, t))
    n = len(LIGHT_RAMP) - 1
    pos = t * n
    lo = int(pos)
    hi = min(lo + 1, n)
    return _lerp_hex(LIGHT_RAMP[lo], LIGHT_RAMP[hi], pos - lo)


def sequential_dark(t: float) -> str:
    t = max(0.0, min(1.0, t))
    return _lerp_hex(DARK_SURFACE, DARK_MAX, 0.25 + 0.75 * t)
