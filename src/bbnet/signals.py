"""Signal-name sources for footprint pin seeding.

A footprint may declare `pin_signals: <source>:<mcu>`, which seeds a
pin's net name from an external pin-allocation table. The engine knows
nothing about where that table comes from -- a host application
registers a named source, and footprints name it.

Three fields is the entire contract. Anything with .mcu, .pin, and
.signal attributes can be registered.
"""
from __future__ import annotations

from dataclasses import dataclass


class UnknownSignalSource(ValueError):
    """A footprint named a signal source nobody registered."""


@dataclass(frozen=True)
class SignalRow:
    mcu: str
    pin: str
    signal: str


class SignalRegistry:
    """Named signal sources, looked up by the prefix of `pin_signals`."""

    def __init__(self):
        self._sources = {}

    def register(self, name, rows):
        self._sources[str(name)] = [
            r if isinstance(r, SignalRow)
            else SignalRow(str(r.mcu), str(r.pin), str(r.signal or ""))
            for r in rows]

    def rows(self, name):
        """Rows for a registered source.

        An unregistered source raises rather than returning []. Returning
        an empty list would quietly disable signal seeding AND mute DRC
        B3 (signal-short) and B7 (pinmap-xcheck) -- a run that checks
        less than it claims to. A DRC that finds nothing must be
        distinguishable from a DRC that did not run.
        """
        try:
            return self._sources[str(name)]
        except KeyError:
            have = ", ".join(sorted(self._sources)) or "none"
            raise UnknownSignalSource(
                f"footprint declares signal source {name!r}, but no such "
                f"source is registered (registered: {have})") from None

    def all_rows(self):
        """Every row from every source, source name order."""
        out = []
        for name in sorted(self._sources):
            out.extend(self._sources[name])
        return out
