"""Tests for breadboard geometry presets and address parsing."""
import pytest

from bbnet.geometry import AddrError, BOARDS, HoleAddr, RailAddr, parse_local

MINI = BOARDS["mini-170"]
FULL = BOARDS["full-830"]
RAILS = {"top+": "5V", "top-": "GND", "bot+": "3V3", "bot-": "GND"}


def test_presets_exist():
    assert MINI.rows == 17 and MINI.rails == ()
    assert FULL.rows == 63 and FULL.split_rails
    assert BOARDS["half-400"].rows == 30


def test_node_level_addresses():
    a = parse_local("43L", "main", FULL, RAILS)
    assert a == HoleAddr("main", 43, "L", None)
    assert parse_local("43R", "main", FULL, RAILS).half == "R"


def test_hole_letter_canonicalizes_to_half():
    a = parse_local("43c", "main", FULL, RAILS)
    assert (a.row, a.half, a.hole) == (43, "L", "c")
    b = parse_local("43j", "main", FULL, RAILS)
    assert (b.half, b.hole) == ("R", "j")
    # same electrical node regardless of which hole was named
    assert a.node_key() == parse_local("43L", "main", FULL, RAILS).node_key()
    assert a.node_key() != b.node_key()


def test_row_out_of_range_rejected():
    with pytest.raises(AddrError):
        parse_local("18L", "bb1", MINI, {})
    with pytest.raises(AddrError):
        parse_local("0a", "bb1", MINI, {})


def test_garbage_rejected():
    for bad in ("43", "L43", "43k", "row43", ""):
        with pytest.raises(AddrError):
            parse_local(bad, "main", FULL, RAILS)


def test_rail_by_position_and_by_name():
    assert parse_local("rail:top+", "main", FULL, RAILS) == RailAddr("main", "top+")
    assert parse_local("rail:5V", "main", FULL, RAILS) == RailAddr("main", "top+")
    assert parse_local("rail:3V3", "main", FULL, RAILS) == RailAddr("main", "bot+")


def test_ambiguous_rail_name_rejected():
    with pytest.raises(AddrError, match="ambiguous"):
        parse_local("rail:GND", "main", FULL, RAILS)


def test_unknown_rail_rejected():
    with pytest.raises(AddrError):
        parse_local("rail:1V8", "main", FULL, RAILS)
