"""Signal-source registry: the whole contract between the engine and an
external pin-allocation table."""
import pytest

from bbnet import signals


def test_registered_source_returns_its_rows():
    reg = signals.SignalRegistry()
    reg.register("pinmap", [signals.SignalRow("mcu1", "6", "DRDY")])
    assert reg.rows("pinmap") == [signals.SignalRow("mcu1", "6", "DRDY")]


def test_unknown_source_raises_and_names_what_is_registered():
    """A silent empty list would disable signal seeding AND mute DRC B3
    and B7 while still reporting a clean run -- the exact failure this
    registry exists to prevent."""
    reg = signals.SignalRegistry()
    reg.register("pinmap", [])
    with pytest.raises(signals.UnknownSignalSource) as e:
        reg.rows("nope")
    assert "nope" in str(e.value)
    assert "pinmap" in str(e.value)


def test_empty_registry_is_still_an_error_not_an_empty_result():
    reg = signals.SignalRegistry()
    with pytest.raises(signals.UnknownSignalSource):
        reg.rows("pinmap")


def test_register_accepts_any_row_with_the_three_attributes():
    """Hosts supply their own row type; only mcu/pin/signal are read."""
    class HostRow:
        mcu, pin, signal = "mcu1", 6, "DRDY"

    reg = signals.SignalRegistry()
    reg.register("host", [HostRow()])
    assert reg.rows("host") == [signals.SignalRow("mcu1", "6", "DRDY")]


def test_all_rows_is_stable_across_sources():
    reg = signals.SignalRegistry()
    reg.register("b", [signals.SignalRow("m", "1", "B")])
    reg.register("a", [signals.SignalRow("m", "2", "A")])
    assert [r.signal for r in reg.all_rows()] == ["A", "B"]


def test_footprint_naming_an_unregistered_source_raises_from_derive():
    """The loud failure must reach real derivation, not just the
    registry -- this is the path that would otherwise mute B3/B7."""
    from bbnet import model

    lib = model.parts_lib_from({
        "demo": {"kind": "sil", "pins": ["1", "2"],
                 "pin_signals": "nosuch:mcu1"}})
    isl = model.island_from({
        "island": "t", "board": "half-400", "rails": {},
        "parts": [{"ref": "U1", "part": "demo", "pin1": "5c"}]}, lib)
    with pytest.raises(signals.UnknownSignalSource):
        model.derive({"t": isl}, signals.SignalRegistry())


def test_unregistered_source_raises_even_when_every_pin_is_seeded():
    """The lookup at signal_map(source) is only ever reached via `elif
    mcu and (mcu, pn) in signal_map(source)`, which a fully-seeded
    footprint never takes -- every pin resolves through the `if pn in
    part.seeds` branch first. Without a validation call independent of
    that per-pin path, this footprint would derive cleanly and DRC would
    find nothing to flag: the exact "reports clean while checking less
    than it claims" failure the registry exists to prevent."""
    from bbnet import model

    lib = model.parts_lib_from({
        "demo": {"kind": "sil", "pins": ["1", "2"],
                 "pin_signals": "nosuch:mcu1",
                 "seeds": {"1": "GND", "2": "GND"}}})
    isl = model.island_from({
        "island": "t", "board": "half-400", "rails": {},
        "parts": [{"ref": "U1", "part": "demo", "pin1": "5c"}]}, lib)
    with pytest.raises(signals.UnknownSignalSource):
        model.derive({"t": isl}, signals.SignalRegistry())
