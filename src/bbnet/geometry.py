#!/usr/bin/env python3
"""geometry.py — breadboard geometry presets and tie-point addressing.

A breadboard is connectivity implied by geometry: every half-row (holes
a-e = L, f-j = R) is one electrical node; every rail strip is one node.
Addresses:

    43L / 43R    half-row node (canonical)
    43c          hole address — canonicalized to 43L; the letter is kept
                 only for hole-occupancy checking (DRC B1)
    rail:5V      rail strip by declared net name (must be unambiguous)
    rail:top+    rail strip by position
    rail:top+@5  rail strip pinned at a row height (geometry only)

Cross-island prefixes ("gps-imu:4L") are stripped by model.py before the
local parse here. Split rails (full-830 mid-board break) are modeled as
ONE node per strip plus a `rails_bridged` assertion checked by DRC B2 —
node-level addressing cannot disambiguate segments.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Board:
    name: str
    rows: int
    rails: tuple[str, ...]   # rail strip positions
    split_rails: bool        # physical mid-board break in the rail strips
    # Mounting-hole keep-out rows. Solderable full-size boards (e.g.
    # ElectroCookie) place mounting screws IN THE RAVINE (between
    # columns e/f) — these rows are that pattern; override per board if
    # yours differs. A screw there means no ravine-straddling part body
    # may cover these rows, and no wire may cross the ravine at them,
    # on either layer.
    ravine_keepouts: frozenset = frozenset()


BOARDS = {
    "full-830": Board("full-830", 63, ("top+", "top-", "bot+", "bot-"), True,
                      frozenset({1, 2, 31, 32, 33, 62, 63})),
    "half-400": Board("half-400", 30, ("top+", "top-", "bot+", "bot-"),
                      False),
    "mini-170": Board("mini-170", 17, (), False),
}

LEFT_HOLES = "abcde"
RIGHT_HOLES = "fghij"

# Physical x of each hole column in 0.1-inch units: adjacent columns are
# 0.1" apart, and the DIP ravine between e and f is 0.3". Lets footprints
# declare their true pin-row span (e.g. Teensy 4.1 = 0.6" -> pins land at
# d+h, covering two columns on the left half and three on the right —
# widths a symmetric mirror around the ravine cannot express).
HOLE_TENTHS = {"a": 0, "b": 1, "c": 2, "d": 3, "e": 4,
               "f": 7, "g": 8, "h": 9, "i": 10, "j": 11}
_TENTHS_HOLE = {v: k for k, v in HOLE_TENTHS.items()}


def col_at_span(left_hole, tenths):
    """Hole column landing `tenths` x 0.1" right of left_hole, or None
    if that lands in the ravine or off the grid."""
    base = HOLE_TENTHS.get(left_hole)
    if base is None:
        return None
    return _TENTHS_HOLE.get(base + tenths)

_ADDR_RE = re.compile(r"(\d+)([a-jLR])")


class AddrError(ValueError):
    pass


@dataclass(frozen=True)
class HoleAddr:
    island: str
    row: int
    half: str          # "L" | "R"
    hole: str | None   # 'a'-'j', or None for node-level addresses

    def node_key(self):
        return ("row", self.island, self.row, self.half)

    def __str__(self):
        return f"{self.island}:{self.row}{self.hole or self.half}"


@dataclass(frozen=True)
class RailAddr:
    island: str
    strip: str         # rail position, e.g. "top+"
    row: int = 0       # optional physical height along the strip
                       # ("rail:top+@5"); 0 = anywhere. Electrically
                       # irrelevant (node_key ignores it) — geometry
                       # for the build sheet, router and B9 only.

    def node_key(self):
        return ("rail", self.island, self.strip)

    def __str__(self):
        at = f"@{self.row}" if self.row else ""
        return f"{self.island}:rail:{self.strip}{at}"


def parse_local(text, island, board, rails):
    """Parse a single-island address. rails maps position -> net name."""
    t = str(text).strip()
    if t.lower().startswith("rail:"):
        name = t[5:].strip()
        row = 0
        if "@" in name:
            name, rtxt = (s.strip() for s in name.split("@", 1))
            try:
                row = int(rtxt)
            except ValueError:
                raise AddrError(f"{island}: bad rail row {rtxt!r} in "
                                f"{text!r} (want e.g. rail:top+@5)")
            if not 1 <= row <= board.rows:
                raise AddrError(f"{island}: rail row {row} out of range "
                                f"1..{board.rows} ({board.name})")
        if name in rails:
            return RailAddr(island, name, row)
        strips = [pos for pos, net in rails.items() if net == name]
        if len(strips) == 1:
            return RailAddr(island, strips[0], row)
        if len(strips) > 1:
            raise AddrError(
                f"{island}: rail:{name} is ambiguous ({', '.join(strips)}) — "
                f"reference by position, e.g. rail:{strips[0]}")
        raise AddrError(f"{island}: no rail named {name!r} "
                        f"(declared: {dict(rails)!r})")
    m = _ADDR_RE.fullmatch(t)
    if not m:
        raise AddrError(f"{island}: bad address {text!r} "
                        "(want e.g. 43L, 43R, 43c, rail:5V)")
    row, tail = int(m.group(1)), m.group(2)
    if not 1 <= row <= board.rows:
        raise AddrError(f"{island}: row {row} out of range 1..{board.rows} "
                        f"({board.name})")
    if tail in ("L", "R"):
        return HoleAddr(island, row, tail, None)
    half = "L" if tail in LEFT_HOLES else "R"
    return HoleAddr(island, row, half, tail)
