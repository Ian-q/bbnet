"""Tests for bbnet DRC rules. Every rule encodes a real breadboard bug
class; each test reproduces the bug synthetically or asserts the clean
twin passes."""
from collections import namedtuple

import pytest

from bbnet import drc
from bbnet import model
from bbnet.model import ModelError, island_from
from helpers import registry

EMPTY_RULES = {"ties": [], "pins": {}}
EMPTY_COLOURS = {"vocabulary": [], "classes": []}


def build(*island_dicts, lib=None, pinmap=(), rules=None, colours=None):
    islands = {}
    for d in island_dicts:
        isl = island_from(d, lib or {})
        islands[isl.name] = isl
    design = model.derive(islands, registry(pinmap))
    v, t = drc.run_all(design, rules or EMPTY_RULES, colours or EMPTY_COLOURS)
    return design, v, t


def rules_hit(v):
    return sorted(x.rule for x in v)


def base_island(**over):
    d = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"OUT": "12R"}}],
         "leads": [{"at": "7R", "net": "GND", "colour": "BLK"},
                   {"at": "3R", "net": "3V3", "colour": "RED"}]}
    d.update(over)
    return d


# --------------------------------------------------- value normalization

def test_res_ohms():
    assert drc.res_ohms("10k") == 10000
    assert drc.res_ohms("4.7K") == 4700
    assert drc.res_ohms("470") == 470
    assert drc.res_ohms("470R") == 470
    assert drc.res_ohms("1M") == 1e6
    assert drc.res_ohms("100n") is None


def test_cap_farads():
    assert drc.cap_farads("100n") == 1e-7
    assert drc.cap_farads("100nF") == 1e-7
    assert drc.cap_farads("4.7u") == 4.7e-6
    assert drc.cap_farads("22p") == 2.2e-11
    assert drc.cap_farads("10k") is None


def test_value_matches():
    assert drc.value_matches("10k", "10K", "resistor")
    assert drc.value_matches("10000", "10k", "resistor")
    assert not drc.value_matches("10k", "4.7k", "resistor")
    assert drc.value_matches("100n", "100nF", "ceramic")
    assert drc.value_matches(None, "", "resistor")      # unspecified: any
    assert not drc.value_matches("10k", "", "resistor")  # spec'd but blank


# ------------------------------------------------- B5 requirements engine

def test_pullup_satisfied_through_the_snake():
    d = base_island(
        jumpers=[{"from": "12R", "to": "11R", "colour": "BLU"},
                 {"from": "9R", "to": "3R", "colour": "RED"}],
        passives=[{"ref": "R1", "kind": "resistor", "value": "10k",
                   "from": "11R", "to": "9R"}])
    rules = {"ties": [], "pins": {
        "U1.OUT": [{"pullup": {"value": "10k", "to": "3V3"}}]}}
    _, v, t = build(d, rules=rules)
    assert rules_hit(v) == [] and t == []


def test_pullup_wrong_rail_unmet_and_becomes_todo():
    d = base_island(
        leads=[{"at": "7R", "net": "GND", "colour": "BLK"},
               {"at": "3R", "net": "5V", "colour": "RED"}],
        passives=[{"ref": "R1", "kind": "resistor", "value": "10k",
                   "from": "12R", "to": "3R"}])
    rules = {"ties": [], "pins": {
        "U1.OUT": [{"pullup": {"value": "10k", "to": "3V3"}}]}}
    _, v, t = build(d, rules=rules)
    assert rules_hit(v) == ["requirements"]
    assert len(t) == 1 and "10k pull-up → 3V3" in t[0].instruction


def test_decouple_kind_constraint():
    ok = base_island(passives=[{"ref": "C1", "kind": "tantalum",
                                "from": "12R", "to": "7R"}])
    rules = {"ties": [], "pins": {"U1.OUT": [
        {"decouple": {"to": "GND", "kind": ["tantalum", "electrolytic"]}}]}}
    _, v, t = build(ok, rules=rules)
    assert v == [] and t == []
    bad = base_island(passives=[{"ref": "C1", "kind": "ceramic",
                                 "from": "12R", "to": "7R"}])
    _, v, t = build(bad, rules=rules)
    assert rules_hit(v) == ["requirements"] and len(t) == 1


def test_no_power_and_logic_domain():
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "rails": {"top+": "5V"},
         "parts": [{"ref": "U1", "pins": {"SIG": "10L"}}],
         "jumpers": [{"from": "10L", "to": "rail:5V", "colour": "RED"}]}
    rules = {"ties": [], "pins": {"U1.SIG": ["no-power"]}}
    _, v, _ = build(d, rules=rules)
    assert "requirements" in rules_hit(v)
    # logic: 3V3 rejects a pull-up resistor that targets the 5V rail
    d2 = {"island": "big", "board": "full-830", "rails_bridged": True,
          "rails": {"top+": "5V"},
          "parts": [{"ref": "U1", "pins": {"OUT": "12L"}}],
          "passives": [{"ref": "R1", "kind": "resistor", "value": "10k",
                        "from": "12L", "to": "rail:5V"}]}
    rules2 = {"ties": [], "pins": {"U1.OUT": [{"logic": "3V3"}]}}
    _, v2, _ = build(d2, rules=rules2)
    assert "requirements" in rules_hit(v2)


def test_series_requirement():
    d = base_island(
        passives=[{"ref": "R1", "kind": "resistor", "value": "1k",
                   "from": "12R", "to": "9R"}],
        leads=[{"at": "9R", "net": "COOL_FLOW_RAW", "colour": "WHT"},
               {"at": "7R", "net": "GND", "colour": "BLK"}])
    rules = {"ties": [], "pins": {"U1.OUT": [
        {"series": {"value": "1k", "to": "COOL_FLOW_RAW"}}]}}
    _, v, t = build(d, rules=rules)
    assert v == [] and t == []


def test_wildcard_and_unknown_ref():
    d = base_island()
    rules = {"ties": [], "pins": {"U1.*": ["must-connect"],
                                  "U9.1": ["must-connect"]}}
    _, v, _ = build(d, rules=rules)
    kinds = rules_hit(v)
    assert "floating" in kinds           # U1.OUT alone on 12R
    assert "requirements" in kinds       # unknown ref U9 warned


def test_must_connect_satisfied_by_passive_edge():
    """A pin whose only connection is a pull-up resistor is connected."""
    d = base_island(passives=[{"ref": "R1", "kind": "resistor",
                               "value": "10k", "from": "12R", "to": "3R"}])
    rules = {"ties": [], "pins": {"U1.OUT": ["must-connect"]}}
    _, v, _ = build(d, rules=rules)
    assert "floating" not in rules_hit(v)


def test_norm_req_multi_key_dict_is_error_not_crash():
    d = base_island()
    rules = {"ties": [], "pins": {
        "U1.OUT": [{"pullup": {"to": "3V3"}, "decouple": {"to": "GND"}}]}}
    _, v, _ = build(d, rules=rules)
    assert "requirements" in rules_hit(v)


# ------------------------------------------------------------ B1 occupancy

def test_two_legs_in_one_hole_flagged():
    d = {"island": "bb1", "board": "mini-170",
         "passives": [
             {"ref": "R1", "kind": "resistor", "from": "4c", "to": "9L"},
             {"ref": "R2", "kind": "resistor", "from": "4c", "to": "8L"}]}
    _, v, _ = build(d)
    assert "occupancy" in rules_hit(v)


def test_more_than_five_members_on_half_row_flagged():
    d = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"A": "4L"}}],
         "jumpers": [{"from": "4L", "to": str(r) + "L", "colour": "GRN"}
                     for r in (5, 6, 7, 8, 9)]}
    _, v, _ = build(d)
    assert "occupancy" in rules_hit(v)


# ------------------------------------------------------------ B2 rail rules

def test_rail_short_two_rail_names_one_net():
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "rails": {"top+": "5V", "bot+": "3V3"},
         "jumpers": [{"from": "rail:5V", "to": "rail:3V3", "colour": "RED"}]}
    _, v, _ = build(d)
    assert "rail-short" in rules_hit(v)
    assert any(x.severity == "error" for x in v if x.rule == "rail-short")


def test_rail_split_same_name_disconnected_warns():
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "rails": {"top-": "GND", "bot-": "GND"}}
    _, v, _ = build(d)
    hits = [x for x in v if x.rule == "rail-split"]
    assert hits and all(x.severity == "warning" for x in hits)


def test_split_rail_board_requires_bridged_assertion():
    d = {"island": "big", "board": "full-830", "rails": {"top+": "5V"}}
    _, v, _ = build(d)
    assert any(x.rule == "rail-split" and "rails_bridged" in x.message
               for x in v)


# --------------------------------------------------------- B3 signal-short

PmRow = namedtuple("PmRow", "mcu pin signal")
MCU_LIB = {"mcu2": {"kind": "dip", "pins": ["1", "2", "3", "4"],
                    "pin_signals": "pinmap:mcu2"}}
MCU_PM = [PmRow("mcu2", "1", "SIG_A"), PmRow("mcu2", "2", "SIG_B")]


def test_signal_short_two_signals_one_net():
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "parts": [{"ref": "M1", "part": "mcu2", "pin1": "10L"}],
         "jumpers": [{"from": "M1.1", "to": "M1.2", "colour": "WHT"}]}
    _, v, _ = build(d, lib=model.parts_lib_from(MCU_LIB), pinmap=MCU_PM)
    assert "signal-short" in rules_hit(v)


def test_signal_short_whitelisted_by_tie():
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "parts": [{"ref": "M1", "part": "mcu2", "pin1": "10L"}],
         "jumpers": [{"from": "M1.1", "to": "M1.2", "colour": "WHT"}]}
    rules = {"ties": [["SIG_A", "SIG_B"]], "pins": {}}
    _, v, _ = build(d, lib=model.parts_lib_from(MCU_LIB), pinmap=MCU_PM,
                    rules=rules)
    assert "signal-short" not in rules_hit(v)


# --------------------------------------------------------- B6 colour code

COLOURS = {"vocabulary": ["RED", "BLK", "YEL", "BLU", "GRN"],
           "classes": [{"match": "^GND$", "colours": ["BLK", "BLU"]},
                       {"match": "^5V$", "colours": ["RED"]}]}


def test_colour_unknown_token_warns():
    d = base_island(jumpers=[{"from": "12R", "to": "9R",
                              "colour": "TEAL"}])
    _, v, _ = build(d, colours=COLOURS)
    assert any(x.rule == "colour" and x.severity == "warning"
               and "TEAL" in x.message for x in v)


def test_colour_class_mismatch_warns():
    # 5V on a blue wire: the exact "ran out of red jumpers" bug
    d = base_island(
        leads=[{"at": "3R", "net": "5V", "colour": "RED"}],
        jumpers=[{"from": "3R", "to": "9R", "colour": "BLU"}])
    _, v, _ = build(d, colours=COLOURS)
    assert any(x.rule == "colour" and "5V" in x.message and "BLU"
               in x.message for x in v)


def test_colour_missing_warns_and_correct_passes():
    d = base_island(jumpers=[{"from": "7R", "to": "9R", "colour": "BLK"},
                             {"from": "12R", "to": "11R"}])
    _, v, _ = build(d, colours=COLOURS)
    msgs = [x for x in v if x.rule == "colour"]
    assert len(msgs) == 1 and "missing colour" in msgs[0].message


# --------------------------------------------------------- B7 pinmap-xcheck

def test_b7_unallocated_pin_and_rail_tied_signal_pin():
    lib = model.parts_lib_from(MCU_LIB)
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "rails": {"top+": "5V"},
         "parts": [{"ref": "M1", "part": "mcu2", "pin1": "10L"}],
         "jumpers": [
             {"from": "M1.3", "to": "20L", "colour": "GRN"},   # pin 3: no
             {"from": "M1.1", "to": "rail:5V", "colour": "RED"}]}  # signal->rail
    _, v, _ = build(d, lib=lib, pinmap=MCU_PM)
    assert any(x.rule == "pinmap-xcheck" and "unallocated" in x.message
               for x in v)
    assert any(x.rule == "pinmap-xcheck" and "rail" in x.message
               for x in v)


def test_b7_waived_by_bench_only():
    lib = model.parts_lib_from(MCU_LIB)
    d = {"island": "big", "board": "full-830", "rails_bridged": True,
         "bench_only": ["M1.3", "M1.1"],
         "rails": {"top+": "5V"},
         "parts": [{"ref": "M1", "part": "mcu2", "pin1": "10L"}],
         "jumpers": [
             {"from": "M1.3", "to": "20L", "colour": "GRN"},
             {"from": "M1.1", "to": "rail:5V", "colour": "RED"}]}
    _, v, _ = build(d, lib=lib, pinmap=MCU_PM)
    assert not any(x.rule == "pinmap-xcheck" for x in v)


# ------------------------------------------------------------ seed-short

def test_lead_short_on_railless_board_is_flagged():
    """Final-review critical: 12V lead jumpered to GND lead on a mini-170
    (no rail strips) must not pass check green."""
    d = {"island": "bb1", "board": "mini-170",
         "leads": [{"at": "2R", "net": "12V", "colour": "YEL"},
                   {"at": "7R", "net": "GND", "colour": "BLU"}],
         "jumpers": [{"from": "2R", "to": "7R", "colour": "YEL"}]}
    _, v, _ = build(d)
    hits = [x for x in v if x.rule == "seed-short"]
    assert hits and all(x.severity == "error" for x in hits)


def test_separate_named_nets_no_seed_short():
    d = {"island": "bb1", "board": "mini-170",
         "leads": [{"at": "2R", "net": "12V", "colour": "YEL"},
                   {"at": "7R", "net": "GND", "colour": "BLU"}]}
    _, v, _ = build(d)
    assert "seed-short" not in rules_hit(v)


def test_seed_short_waived_by_tie():
    d = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"OUT+": "5R"},
                    "seeds": {"OUT+": "5V"}}],
         "leads": [{"at": "7R", "net": "VIN", "colour": "RED"}],
         "jumpers": [{"from": "5R", "to": "7R", "colour": "RED"}]}
    rules = {"ties": [["5V", "VIN"]], "pins": {}}
    _, v, _ = build(d, rules=rules)
    assert "seed-short" not in rules_hit(v)


def test_requirement_net_lookup_scoped_to_connected_islands():
    """Two islands each with their own GND: a requirement's to: target
    resolves within the pin's island group — a standalone supply board
    carries its own GND/3V3 names."""
    a = {"island": "bb1", "board": "mini-170",
         "parts": [{"ref": "U1", "pins": {"OUT": "12R"}}],
         "leads": [{"at": "7R", "net": "GND", "colour": "BLK"}],
         "passives": [{"ref": "C1", "kind": "ceramic",
                       "from": "12R", "to": "7R"}]}
    b = {"island": "bb2", "board": "mini-170",
         "leads": [{"at": "3L", "net": "GND", "colour": "BLK"}]}
    rules = {"ties": [], "pins": {"U1.OUT": [
        {"decouple": {"to": "GND", "kind": ["ceramic"]}}]}}
    _, v, t = build(a, b, rules=rules)
    assert rules_hit(v) == [] and t == []


# ---------------------------------------------------- B8 passive-span

def test_passive_span_min_bends():
    """Through-hole passives can't bend tighter than their leads allow:
    resistors need 'down 1 over 2'; small ceramics one diagonal;
    electrolytics are radial (native 0.1") and exempt; rail endpoints
    leave the span to the builder — never flagged."""
    def spans(passives, rails=None):
        d = {"island": "bb1", "board": "full-830" if rails else "mini-170",
             "passives": passives}
        if rails:
            d["rails"] = rails
            d["rails_bridged"] = True
        _, v, _ = build(d)
        return [x for x in v if x.rule == "passive-span"]

    r = {"ref": "R1", "kind": "resistor", "value": "10k"}
    c = {"ref": "C1", "kind": "ceramic", "value": "100p"}
    e = {"ref": "C2", "kind": "electrolytic", "value": "100u"}
    # resistor: vertical 1 and diagonal (1,1) too tight; (1,2) ok
    assert spans([{**r, "from": "5a", "to": "6a"}])
    assert spans([{**r, "from": "5a", "to": "6b"}])
    assert not spans([{**r, "from": "5a", "to": "6c"}])
    assert not spans([{**r, "from": "5e", "to": "5f"}])   # ravine = 3
    # ceramic: vertical 1 too tight; one diagonal ok
    assert spans([{**c, "from": "5a", "to": "6a"}])
    assert not spans([{**c, "from": "5a", "to": "6b"}])
    # electrolytic: radial, vertical 1 fine
    assert not spans([{**e, "from": "5a", "to": "6a"}])
    # rail endpoint: builder picks the strip hole — exempt
    assert not spans([{**r, "from": "5a", "to": "rail:top-"}],
                     rails={"top+": "3V3", "top-": "GND",
                            "bot+": "5V", "bot-": "GND"})


def test_passive_span_waiver():
    d = {"island": "bb1", "board": "mini-170",
         "passives": [{"ref": "C1", "kind": "ceramic", "value": "470n",
                       "from": "5a", "to": "6a"}]}
    rules = {"ties": [], "pins": {},
             "passive_span_waivers": ["bb1:C1"]}
    _, v, _ = build(d, rules=rules)
    assert "passive-span" not in rules_hit(v)


# -------------------------------------------------- B9 passive-overlap

def test_passive_overlap_same_face():
    """Two same-face passive bodies crossing or lying along each other:
    two passives both running row 48 to the left rails; the fix is
    mounting one on the underside."""
    def overlaps(passives):
        d = {"island": "bb1", "board": "full-830",
             "rails": {"top+": "3V3", "top-": "GND",
                       "bot+": "5V", "bot-": "GND"},
             "rails_bridged": True, "passives": passives}
        _, v, _ = build(d)
        return [x for x in v if x.rule == "passive-overlap"]

    r5 = {"ref": "R5", "kind": "resistor", "value": "10k",
          "from": "48c", "to": "rail:top+"}
    c5 = {"ref": "C5", "kind": "ceramic", "value": "100p",
          "from": "48a", "to": "rail:top-"}
    # collinear along row 48 on one face -> overlap
    assert overlaps([r5, c5])
    # underside mounting clears it
    assert not overlaps([{**r5, "side": "bottom"}, c5])
    # crossing diagonals on one face -> overlap; split faces -> clean
    x1 = {"ref": "C1", "kind": "ceramic", "value": "10p",
          "from": "10a", "to": "11b"}
    x2 = {"ref": "C2", "kind": "ceramic", "value": "100p",
          "from": "11a", "to": "10b"}
    assert overlaps([x1, x2])
    assert not overlaps([x1, {**x2, "side": "bottom"}])
    # parallel on neighbouring rows -> clean
    assert not overlaps([{**x1, "from": "10a", "to": "11c"},
                         {**x2, "ref": "C3", "from": "12a", "to": "13c"}])
    # rail-to-rail passives sit anywhere along the strips -> exempt
    rr = {"ref": "C6", "kind": "ceramic", "value": "100n",
          "from": "rail:top+", "to": "rail:top-"}
    assert not overlaps([rr, {**rr, "ref": "C7"}])


# --------------------------------------------------- B10 cap-polarity

def test_cap_polarity_reversed_electrolytic():
    """from = + terminal by convention; + on GND with - on a power net
    is a reversed (venting) electrolytic. Ceramics don't care."""
    def caps(frm, to, kind="electrolytic"):
        d = {"island": "bb1", "board": "mini-170",
             "leads": [{"at": "3a", "net": "GND", "colour": "BLK"},
                       {"at": "7a", "net": "3V3", "colour": "RED"}],
             "passives": [{"ref": "C1", "kind": kind, "value": "10u",
                           "from": frm, "to": to}]}
        _, v, _ = build(d)
        return [x for x in v if x.rule == "cap-polarity"]

    assert caps("3b", "7b")                    # + on GND, - on 3V3
    assert not caps("7b", "3b")                # correct orientation
    assert not caps("3b", "7b", kind="ceramic")  # non-polar exempt


# ------------------------------------------------- B11 voltage-rating

def test_voltage_rating():
    """rating: vs the working voltage of voltage-known nets: over the
    rating errors, thin (<1.5x) derating warns, signal nets skip."""
    def volts(rating, hi_net="12V"):
        d = {"island": "bb1", "board": "mini-170",
             "leads": [{"at": "3a", "net": "GND", "colour": "BLK"},
                       {"at": "7a", "net": hi_net, "colour": "YEL"}],
             "passives": [{"ref": "C1", "kind": "electrolytic",
                           "value": "100u", "rating": rating,
                           "from": "7b", "to": "3b"}]}
        _, v, _ = build(d)
        return [(x.severity, x.rule) for x in v
                if x.rule == "voltage-rating"]

    assert volts("10V") == [("error", "voltage-rating")]    # 10V < 12V
    assert volts("16V") == [("warning", "voltage-rating")]  # < 1.5x
    assert volts("25V") == []                               # >= 2x
    assert volts("") == []                                  # unrated
    assert volts("25V", hi_net="DRDY") == []                # signal net
    assert volts("junk") == [("warning", "voltage-rating")]


def test_cap_polarity_and_rating_work_on_a_24v_board():
    """B10/B11 previously matched only 3V3/5V/12V, so every rule silently
    found nothing on any other supply while still reporting a clean run."""
    lib = model.parts_lib_from({})
    isl = model.island_from({
        "island": "hv", "board": "half-400",
        "rails": {"top+": "24V", "top-": "GND"},
        "passives": [
            # reversed electrolytic: + terminal on GND
            {"ref": "C9", "kind": "electrolytic", "value": "100u",
             "rating": "16V", "from": "rail:top-", "to": "rail:top+"},
        ]}, lib)
    rules = {"net_voltages": [{"match": "^24V$", "volts": 24.0},
                              {"match": "^GND$", "volts": 0.0}]}
    design = model.derive({"hv": isl}, registry())
    violations, _todos = drc.run_all(design, rules, {})
    kinds = {v.rule for v in violations}
    assert "cap-polarity" in kinds, "reversed electrolytic must be caught"
    assert "voltage-rating" in kinds, "16V part on a 24V rail must be caught"


def test_net_voltages_defaults_preserve_the_builtin_table():
    """rules.yaml without net_voltages must behave exactly as before."""
    assert drc._net_volts("3V3", {}) == 3.3
    assert drc._net_volts("GND", {}) == 0.0
    assert drc._net_volts("SPI_SCK", {}) is None


def test_cap_polarity_fires_when_ground_is_not_named_gnd():
    """B10 used to decide the '+' side by the literal string 'GND', so a
    project whose ground is declared under a different name via
    net_voltages (AGND, 0V, VSS, ...) lost the rule entirely -- even
    though B11 (voltage-rating), which is table-driven, still fired on
    the very same reversed part. Ground must be decided by voltage
    (0.0 V), not by name."""
    lib = model.parts_lib_from({})
    isl = model.island_from({
        "island": "hv", "board": "half-400",
        "rails": {"top+": "12V", "top-": "AGND"},
        "passives": [
            # reversed electrolytic: + terminal (from=) on AGND
            {"ref": "C9", "kind": "electrolytic", "value": "100u",
             "rating": "5V", "from": "rail:top-", "to": "rail:top+"},
        ]}, lib)
    rules = {"net_voltages": [{"match": "^AGND$", "volts": 0.0},
                              {"match": "^12V", "volts": 12.0}]}
    design = model.derive({"hv": isl}, registry())
    violations, _todos = drc.run_all(design, rules, {})
    kinds = {v.rule for v in violations}
    assert "cap-polarity" in kinds, \
        "reversed electrolytic across a non-GND-named ground must be caught"
    assert "voltage-rating" in kinds  # sanity: B11 already caught this half


def test_cap_polarity_message_names_the_actual_ground_net():
    """B10's verdict went name-agnostic (voltage-based) but the message
    text still hardcoded the literal string 'GND', so a board whose
    ground is declared as AGND got told its '+' terminal 'lands on GND'
    -- a net that doesn't exist on that board. Someone chasing that
    message at the bench looks for a net that isn't there. The message
    must name the real net."""
    lib = model.parts_lib_from({})
    isl = model.island_from({
        "island": "hv", "board": "half-400",
        "rails": {"top+": "12V", "top-": "AGND"},
        "passives": [
            # reversed electrolytic: + terminal (from=) on AGND
            {"ref": "C9", "kind": "electrolytic", "value": "100u",
             "rating": "5V", "from": "rail:top-", "to": "rail:top+"},
        ]}, lib)
    rules = {"net_voltages": [{"match": "^AGND$", "volts": 0.0},
                              {"match": "^12V", "volts": 12.0}]}
    design = model.derive({"hv": isl}, registry())
    violations, _todos = drc.run_all(design, rules, {})
    cap = [v for v in violations if v.rule == "cap-polarity"]
    assert cap, "reversed electrolytic must be caught"
    msg = cap[0].message
    assert "lands on AGND" in msg
    # AGND contains the substring GND, so a naive "GND" not in msg would
    # be spuriously satisfied here regardless of the bug. Assert on the
    # precise phrase the literal-string bug actually produced.
    assert "lands on GND " not in msg


def test_cap_polarity_unknown_net_is_not_mistaken_for_ground():
    """An unmatched plus-side net must stay 'unknown', not silently pass
    as ground: _net_volts returns None for it, and None == 0.0 is False."""
    assert drc._net_volts("SPI_SCK", {}) != 0.0


# --------------------------------------------- net_voltages: empty/null

def test_net_voltages_absent_key_still_uses_builtin_table():
    """No net_voltages: key at all -- the ordinary case -- must keep
    getting the built-in 3V3/5V/12V/GND table (already covered by
    test_net_voltages_defaults_preserve_the_builtin_table; repeated here
    as the control twin for the explicitly-empty cases below)."""
    assert drc._net_volts("GND", {}) == 0.0
    assert drc._net_volts("GND", {"ties": []}) == 0.0


@pytest.mark.parametrize("bad_spec", [None, [], {}])
def test_net_voltages_explicitly_empty_is_rejected(bad_spec):
    """net_voltages: [] / {} / (bare, i.e. null) must not silently
    collapse to the built-in table -- that's the same silent-substitution
    bug as B10's hardcoded 'GND' string, just at the config layer. A
    user who really wants the default omits the key; one who writes it
    empty almost certainly forgot to fill it in, so this raises rather
    than quietly re-enabling defaults they tried to turn off."""
    with pytest.raises(ModelError, match="net_voltages"):
        drc._net_volts("GND", {"net_voltages": bad_spec})


# ------------------------------------------- B12 in-node detour (routed)

def _routed_build(*island_dicts, lib=None, pinmap=(), rules=None,
                  colours=None):
    """build(), but with the autorouter run — B12 and B13 measure real
    geometry, so they need the routed paths the way the CLI supplies
    them."""
    from bbnet import router
    islands = {}
    for d in island_dicts:
        isl = island_from(d, lib or {})
        islands[isl.name] = isl
    design = model.derive(islands, registry(pinmap))
    rules = rules or EMPTY_RULES
    routed = router.route_design(islands, design, rules)
    v, t = drc.run_all(design, rules, colours or EMPTY_COLOURS, routed)
    return design, v, t


def _detours(violations):
    return [x.message for x in violations if x.rule == "in-node detour"]


def test_in_node_detour_is_silent_without_routing():
    """B12 is geometry-dependent; the connectivity-only commands must not
    trip over its absence."""
    _d, v, _t = build(base_island())
    assert _detours(v) == []


RAILED = {"top+": "3V3", "top-": "GND", "bot+": "5V", "bot-": "GND"}


def _railed(**over):
    """A full-830 with rails, so a wire can be aimed at the far side and
    forced to leave its half-row toward the right-hand gutter."""
    d = {"island": "bb1", "board": "full-830", "rails": dict(RAILED)}
    d.update(over)
    return d


def test_wire_landing_short_of_its_exit_hole_is_flagged():
    """A jumper that lands mid-half-row and crawls to the far side to
    leave has picked the wrong hole — every hole in the row is the same
    conductor, so the crawl is redundant copper over unused holes.
    20h aimed at the RIGHT rail must cross i and j to get out."""
    _d, v, _t = _routed_build(_railed(
        jumpers=[{"from": "20h", "to": "rail:bot-", "colour": "BLK"}]))
    hits = _detours(v)
    assert len(hits) == 1, hits
    assert "lands at 20h" in hits[0] and "land at 20j instead" in hits[0]
    assert "frees 20h, 20i" in hits[0]


def test_landing_on_the_exit_hole_is_clean():
    """The same wire, landed where it actually leaves the node."""
    _d, v, _t = _routed_build(_railed(
        jumpers=[{"from": "20j", "to": "rail:bot-", "colour": "BLK"}]))
    assert _detours(v) == []


def test_detour_never_suggests_an_occupied_hole():
    """A hole with a leg already in it is not available no matter how
    much wire it would save (B1 owns that) — the suggestion falls back
    to the furthest FREE hole along the crawl.

    The occupant here is a LEAD: it holds the hole without blocking the
    surface, so the wire still crawls over it and the fallback branch is
    the one under test. A part pin instead makes the router dodge a row
    early, and then there is no in-row crawl left to report."""
    _d, v, _t = _routed_build(_railed(
        leads=[{"at": "20j", "colour": "RED", "label": "off-board feed"}],
        jumpers=[{"from": "20h", "to": "rail:bot-", "colour": "BLK"}]))
    hits = _detours(v)
    assert len(hits) == 1, hits
    assert "land at 20i instead" in hits[0], hits[0]


def test_detour_silent_when_every_better_hole_is_taken():
    _d, v, _t = _routed_build(_railed(
        leads=[{"at": "20i", "colour": "RED", "label": "a"},
               {"at": "20j", "colour": "RED", "label": "b"}],
        jumpers=[{"from": "20h", "to": "rail:bot-", "colour": "BLK"}]))
    assert _detours(v) == []


def test_ravine_crossing_should_land_on_the_inner_holes():
    """Same rule, the other common shape: a row-20 L↔R strap soldered at
    h and c sprawls across six holes when e/f would do."""
    _d, v, _t = _routed_build(_railed(
        jumpers=[{"from": "20h", "to": "20c", "colour": "GRN"}]))
    hits = sorted(_detours(v))
    assert len(hits) == 2, hits
    assert any("land at 20f instead" in m for m in hits)
    assert any("land at 20e instead" in m for m in hits)


def test_in_node_waiver_silences_a_deliberate_landing():
    isl = _railed(
        jumpers=[{"from": "20h", "to": "rail:bot-", "colour": "BLK"}])
    _d, v, _t = _routed_build(isl)
    assert _detours(v), "expected the un-waived case to fire"
    _d, v2, _t = _routed_build(
        isl, rules=dict(EMPTY_RULES, in_node_waivers=["bb1:20h"]))
    assert _detours(v2) == []


# ------------------------------------------ B13 half-row landing (routed)

def _landings(violations):
    return [x.message for x in violations if x.rule == "half-row landing"]


def test_halfrow_endpoint_prefers_a_free_hole_over_an_occupied_one():
    """`20L` means "any hole in this node" — but not one that already has
    a leg in it. Before this, the picker took the geometric end of the
    row and drew the wire straight through the resistor's leg."""
    from bbnet import router
    isl = island_from({
        "island": "bb1", "board": "full-830",
        "rails": dict(RAILED),
        "passives": [{"ref": "R1", "kind": "resistor", "value": "10k",
                      "from": "20a", "to": "rail:top+"}],
        "jumpers": [{"from": "20L", "to": "40L", "colour": "GRN"}]}, {})
    islands = {isl.name: isl}
    design = model.derive(islands, registry())
    routed = router.route_design(islands, design, EMPTY_RULES)
    wires, _stats, lat = routed["bb1"]
    landed = {f"{lat.name(w.path[0].x)}{w.path[0].y}" for w in wires
              if w.kind == "jumper"}
    assert "a20" not in landed, f"landed on R1's leg: {landed}"


def test_halfrow_with_only_body_covered_holes_is_reported():
    """A click straddling the ravine covers every hole between its two
    pin rows, so a node under it can only be landed from beneath — B13
    says so instead of letting the picker pretend otherwise."""
    from bbnet import router
    lib = model.parts_lib_from({
        "clickish": {"kind": "dip", "span": 9,
                     "pins": ["P1", "P2", "P3", "P4"]}})
    isl = island_from({
        "island": "bb1", "board": "full-830", "rails": dict(RAILED),
        "parts": [{"ref": "U1", "part": "clickish", "pin1": "20b"}],
        "passives": [{"ref": "R1", "kind": "resistor", "value": "10k",
                      "from": "20a", "to": "rail:top+"}],
        "jumpers": [{"from": "20L", "to": "40L", "colour": "GRN"}]}, lib)
    islands = {isl.name: isl}
    design = model.derive(islands, registry())
    routed = router.route_design(islands, design, EMPTY_RULES)
    v = drc.rule_halfrow_landing(design, EMPTY_RULES, EMPTY_COLOURS, routed)
    msgs = _landings(v)
    assert any("20L" in m and "underside: true" in m for m in msgs), msgs


def test_pinned_hole_is_never_a_halfrow_landing_finding():
    """B13 is about the model leaving the hole unsaid; once it is pinned
    the decision has been made and the rule has nothing to say."""
    _d, v, _t = _routed_build(_railed(
        passives=[{"ref": "R1", "kind": "resistor", "value": "10k",
                   "from": "20a", "to": "rail:top+"}],
        jumpers=[{"from": "20c", "to": "40c", "colour": "GRN"}]))
    assert _landings(v) == []


def test_body_span_survives_an_anchor_pinned_to_a_bare_halfrow():
    """A part anchored at `20L` names no column, so there is no
    footprint-derived body span to compute. `_body` used to reach for
    the anchor's hole unconditionally and die on None — invisible until
    DRC gained geometry-dependent rules and started routing every
    build()."""
    from bbnet import router
    lib = model.parts_lib_from({
        "clickish": {"kind": "dip", "span": 9,
                     "pins": ["P1", "P2", "P3", "P4"]}})
    isl = island_from({
        "island": "bb1", "board": "full-830", "rails": dict(RAILED),
        "parts": [{"ref": "U1", "part": "clickish", "pin1": "20L"}]}, lib)
    islands = {isl.name: isl}
    design = model.derive(islands, registry())
    routed = router.route_design(islands, design, EMPTY_RULES)
    assert "bb1" in routed


# --------------------------------------------- B14-B16 link bars (levels)

def _link_island(**over):
    d = {"island": "bb1", "board": "full-830", "rails": dict(RAILED)}
    d.update(over)
    return d


def _risers(*specs):
    return [{"at": at, "level": lv} for at, lv in specs]


def _link_rule(v, rule):
    return [x.message for x in v if x.rule == rule]


def test_link_bar_fans_one_net_across_every_bonded_position():
    """The reason link bars exist: a 1xN is ONE conductor, so one 3V3
    tap feeds every position it bonds. All five holes must land on the
    same net, and that net must be 3V3 — not a fresh unnamed one."""
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("22a", 1), ("24a", 1)),
        jumpers=[{"from": "20b", "to": "rail:top+", "colour": "RED"}],
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "22a", "24a"],
                "clipped": ["21a", "23a"], "stock": 5}]))
    assert [x for x in v if x.severity == "error"] == []
    design, _v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("22a", 1), ("24a", 1)),
        jumpers=[{"from": "20b", "to": "rail:top+", "colour": "RED"}],
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "22a", "24a"],
                "clipped": ["21a", "23a"], "stock": 5}]))
    nets = {design.nid_of_key[("row", "bb1", r, "L")] for r in (20, 22, 24)}
    assert len(nets) == 1, "a 1xN bar is one conductor"
    assert design.nets[nets.pop()].name == "3V3"


def test_link_without_a_riser_under_a_bonded_position_is_an_error():
    """B14. The bar has nothing to plug into there, so the netlist would
    be claiming a bond the hardware does not have."""
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1)),
        links=[{"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
                "clipped": ["21a"]}]))
    hits = _link_rule(v, "link support")
    assert len(hits) == 1 and "22a" in hits[0], hits
    assert "no riser at all" in hits[0]


def test_link_riser_at_the_wrong_level_is_an_error():
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("22a", 2)),
        links=[{"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
                "clipped": ["21a"]}]))
    hits = _link_rule(v, "link support")
    assert len(hits) == 1 and "only [2]" in hits[0], hits


def test_unlisted_position_with_no_riser_is_fine():
    """A float is the common case and it is harmless — all pins are the
    same length, so one with nothing beneath it hangs in free air."""
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("22a", 1)),
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "22a"]}]))
    assert _link_rule(v, "link stray pin") == []
    assert [x for x in v if x.severity == "error"] == []


def test_unlisted_position_over_a_riser_is_a_stray_pin():
    """B15 — the one way this system bites. The riser at 21a was put
    there for something else; the bar's own pin now bonds it in."""
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("21a", 1), ("22a", 1)),
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "22a"]}]))
    hits = _link_rule(v, "link stray pin")
    assert len(hits) == 1 and "21a" in hits[0], hits
    assert [x for x in v if x.rule == "link stray pin"][0].severity \
        == "error"


def test_clipping_the_stray_pin_silences_it():
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("21a", 1), ("22a", 1)),
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "22a"], "clipped": ["21a"]}]))
    assert _link_rule(v, "link stray pin") == []


def test_declaring_the_stray_pin_silences_it_and_bonds_it():
    """The other honest answer: you did mean it, so say so."""
    design, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("21a", 1), ("22a", 1)),
        links=[{"ref": "LK1", "level": 1,
                "connects": ["20a", "21a", "22a"]}]))
    assert _link_rule(v, "link stray pin") == []
    nets = {design.nid_of_key[("row", "bb1", r, "L")] for r in (20, 21, 22)}
    assert len(nets) == 1


def test_link_cut_longer_than_stock_warns():
    """B16 — electrically fine, just not buildable from that bag."""
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("24a", 1)),
        links=[{"ref": "LK1", "level": 1, "connects": ["20a", "24a"],
                "clipped": ["21a", "22a", "23a"], "stock": 3}]))
    hits = _link_rule(v, "link stock")
    assert len(hits) == 1 and "spans 5" in hits[0], hits
    assert [x for x in v if x.rule == "link stock"][0].severity == "warning"


def test_link_within_stock_is_silent():
    _d, v, _t = build(_link_island(
        risers=_risers(("20a", 1), ("22a", 1)),
        links=[{"ref": "LK1", "level": 1, "connects": ["20a", "22a"],
                "clipped": ["21a"], "stock": 5}]))
    assert _link_rule(v, "link stock") == []


# ------------------------------------- B17 switched sets / level-aware B9

SW_RAILS = {"top+": "5V", "top-": "GND", "bot+": "3V3", "bot-": "GND"}


def _sw_island(**over):
    d = {"island": "bb1", "board": "full-830", "rails": dict(SW_RAILS)}
    d.update(over)
    return d


def test_normally_closed_switch_is_wire_in_the_derived_netlist():
    """The de-energized-state rule. On an unpowered board an NC switch
    IS a piece of wire, which is what a multimeter would tell you — so
    the netlist says the same thing."""
    design, _v, _t = build(_sw_island(devices=[
        {"ref": "SW1", "kind": "switch", "normally": "closed",
         "pins": {"A": "20a", "B": "24a"}}]))
    nids = design.device_nids["SW1"]
    assert nids["A"] == nids["B"]


def test_normally_open_switch_keeps_its_sides_apart():
    design, _v, _t = build(_sw_island(devices=[
        {"ref": "SW1", "kind": "switch", "normally": "open",
         "pins": {"A": "20a", "B": "24a"}}]))
    nids = design.device_nids["SW1"]
    assert nids["A"] != nids["B"]


def test_relay_contacts_follow_their_physical_default():
    """COM-NC is closed with the coil unpowered and COM-NO is not; that
    is a property of the part, not a choice."""
    design, _v, _t = build(_sw_island(devices=[
        {"ref": "K1", "kind": "relay",
         "pins": {"A1": "20a", "A2": "21a", "COM": "22a",
                  "NO": "23a", "NC": "24a"}}]))
    n = design.device_nids["K1"]
    assert n["COM"] == n["NC"], "COM-NC is closed de-energized"
    assert n["COM"] != n["NO"]
    assert n["A1"] != n["A2"], "the coil is its own pair either way"
    assert n["A1"] != n["COM"], "coil is isolated from the contacts"


def test_mosfet_channel_defaults_open():
    """Enhancement-mode parts are off with no gate drive, so D and S
    stay on separate nets."""
    design, _v, _t = build(_sw_island(devices=[
        {"ref": "Q1", "kind": "mosfet", "value": "2N7000",
         "pins": {"G": "20a", "D": "21a", "S": "22a"}}]))
    n = design.device_nids["Q1"]
    assert len({n["G"], n["D"], n["S"]}) == 3


def test_normally_only_applies_to_a_switch():
    with pytest.raises(ModelError, match="only applies to a switch"):
        build(_sw_island(devices=[
            {"ref": "Q1", "kind": "mosfet", "normally": "closed",
             "pins": {"G": "20a", "D": "21a", "S": "22a"}}]))


def test_normally_must_be_open_or_closed():
    with pytest.raises(ModelError, match="normally"):
        build(_sw_island(devices=[
            {"ref": "SW1", "kind": "switch", "normally": "maybe",
             "pins": {"A": "20a", "B": "24a"}}]))


def test_closed_switch_bridging_two_rails_warns():
    """B17. Closed with nothing driving it, between 5V and 3V3 — the
    board is shorted the moment it is powered, before any firmware runs
    and before anything can open the contact."""
    _d, v, _t = build(_sw_island(
        devices=[{"ref": "SW1", "kind": "switch", "normally": "closed",
                  "pins": {"A": "20a", "B": "24a"}}],
        jumpers=[{"from": "20b", "to": "rail:top+", "colour": "RED"},
                 {"from": "24b", "to": "rail:bot+", "colour": "RED"}]))
    hits = [x for x in v if x.rule == "closed-by-default"]
    assert len(hits) == 1, [x.message for x in v]
    assert "3V3 + 5V" in hits[0].message
    assert hits[0].severity == "warning"


def test_open_switch_between_two_rails_is_silent():
    """Same wiring, switch open — that is just a user-operated link."""
    _d, v, _t = build(_sw_island(
        devices=[{"ref": "SW1", "kind": "switch", "normally": "open",
                  "pins": {"A": "20a", "B": "24a"}}],
        jumpers=[{"from": "20b", "to": "rail:top+", "colour": "RED"},
                 {"from": "24b", "to": "rail:bot+", "colour": "RED"}]))
    assert [x for x in v if x.rule == "closed-by-default"] == []


def test_bodies_crossing_at_different_levels_do_not_collide():
    """B9 made correct rather than weaker. Crossing at different heights
    is the entire point of building upward; flagging it would fight the
    design it exists to enable."""
    crossing = dict(_sw_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "24a"},
        {"ref": "R2", "kind": "resistor", "value": "1k",
         "from": "22a", "to": "22e", "level": 1}]))
    _d, v, _t = build(crossing)
    assert [x for x in v if x.rule == "passive-overlap"] == []


def test_bodies_crossing_at_the_same_level_still_collide():
    same = dict(_sw_island(passives=[
        {"ref": "R1", "kind": "resistor", "value": "1k",
         "from": "20a", "to": "24a", "level": 1},
        {"ref": "R2", "kind": "resistor", "value": "1k",
         "from": "22a", "to": "22e", "level": 1}]))
    _d, v, _t = build(same)
    hits = [x for x in v if x.rule == "passive-overlap"]
    assert len(hits) == 1 and "level 1" in hits[0].message, hits


def test_requirements_see_device_pins():
    """A regulator's OUT wanting decoupling is exactly the shape
    rules.yaml exists for. Leaving devices out of the ref table would
    silently STOP enforcing a requirement the moment a part was retyped
    from `parts:` to `devices:` — a migration that changes no holes
    must not change what is checked."""
    isl = dict(_sw_island(
        devices=[{"ref": "U2", "kind": "regulator",
                  "pins": {"IN": "20a", "GND": "21a", "OUT": "22a"},
                  "seeds": {"OUT": "3V3"}}]))
    rules = dict(EMPTY_RULES,
                 pins={"U2.OUT": [{"decouple": {"to": "GND",
                                                "kind": ["ceramic"]}}]})
    _d, v, todos = build(isl, rules=rules)
    assert not [x for x in v if "not on the bench" in x.message], \
        [x.message for x in v]
    assert [t for t in todos if t.pin == "U2.OUT"], \
        "the unmet decouple requirement must still surface as a todo"


def test_requirements_wildcard_expands_over_device_pins():
    isl = dict(_sw_island(
        devices=[{"ref": "Q9", "kind": "mosfet",
                  "pins": {"G": "20a", "D": "21a", "S": "22a"}}]))
    rules = dict(EMPTY_RULES, pins={"Q9.*": ["must-connect"]})
    _d, v, _t = build(isl, rules=rules)
    floating = [x.message for x in v if x.rule == "floating"]
    assert len(floating) == 3, floating


def test_requirement_on_an_unplaced_device_pin_still_warns():
    isl = dict(_sw_island(
        devices=[{"ref": "Q9", "kind": "mosfet",
                  "pins": {"G": "20a", "D": "21a", "S": "22a"}}]))
    rules = dict(EMPTY_RULES, pins={"Q9.GATE": ["must-connect"]})
    _d, v, _t = build(isl, rules=rules)
    assert [x for x in v if "no placed pin" in x.message]
