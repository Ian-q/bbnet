"""A breakout lying flat beside its header (footprint `body_cols:`)
buries the hole columns its PCB covers on its pin rows, just as a can
does. Found building INA226 group boards, whose PCBs cover two columns
toward column j."""
import pytest

from bbnet import drc, model
from helpers import registry

LIB = {"brk": {"kind": "sil", "pins": ["A", "B", "C"], "body_cols": 2},
       "brk-left": {"kind": "sil", "pins": ["A", "B", "C"], "body_cols": -1}}


def _isl(extra=(), part="brk", pin1="5c"):
    d = {"island": "t", "board": "mini-170",
         "parts": [{"ref": "M1", "part": part, "pin1": pin1}],
         "passives": list(extra)}
    lib = model.parts_lib_from(LIB)
    islands = {"t": model.island_from(d, lib)}
    return islands, model.derive(islands, registry())


def test_cover_is_the_columns_beside_the_pins():
    islands, _d = _isl()
    m = islands["t"].parts[0]
    assert {(r, h) for r, _hf, h in model.body_cover(m)} == {
        (r, h) for r in (5, 6, 7) for h in "de"}


def test_negative_cols_go_toward_column_a():
    islands, _d = _isl(part="brk-left")
    m = islands["t"].parts[0]
    assert {h for _r, _hf, h in model.body_cover(m)} == {"b"}


def test_a_leg_in_a_buried_hole_is_an_error():
    _i, d = _isl([{"ref": "R1", "kind": "resistor", "value": "1k",
                   "from": "6d", "to": "9d"}])
    assert any("t:6d:" in v.message and "M1 body" in v.message
               for v in drc.rule_occupancy(d, {}, {}))


def test_a_resistor_lying_under_the_pcb_is_an_error():
    _i, d = _isl([{"ref": "R2", "kind": "resistor", "value": "1k",
                   "from": "4e", "to": "8e"}])
    assert any("R2 lies under M1's body" in v.message
               for v in drc.rule_passive_overlap(d, {}, {}))


def test_body_cols_only_on_sil():
    with pytest.raises(model.ModelError, match="body_cols"):
        model.parts_lib_from({"x": {"kind": "dip", "pins": ["1", "2"],
                                    "body_cols": 2}})
