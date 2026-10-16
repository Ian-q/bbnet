"""Movable labels: stable ids, labels.yaml offsets, and `bbnet serve`."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from bbnet import cli as bbnet
from bbnet import render
from bbnet.model import ModelError
from test_cli import SNAKE_YAML, mkdata

EXTRA = "  - {from: 16R, to: 17R, colour: GRN}\n"


def _ids(d):
    return render.label_ids(bbnet.layout_html(d))


def _with_jumper(yaml_text, line):
    return yaml_text.replace("jumpers:\n", "jumpers:\n" + line, 1)


def test_the_corpus_labels_carry_ids(tmp_path):
    ids = _ids(mkdata(tmp_path))
    assert {"bb1/p/C1", "bb1/p/C2"} <= ids
    # edge labels are keyed by the endpoint as written, not a wire key
    assert {"bb1/w/2R", "bb1/w/3R", "bb1/w/12R"} <= ids
    assert not any(":0" in i for i in ids), ids


def test_ids_survive_an_unrelated_wire(tmp_path):
    """The router's wire keys are sequence-numbered and renumber when a
    wire is added in front of them; a saved offset must not."""
    before = _ids(mkdata(tmp_path))
    d2 = tmp_path / "b"
    d2.mkdir()
    after = _ids(mkdata(d2, island=_with_jumper(SNAKE_YAML, EXTRA)))
    assert before <= after


def test_a_lead_id_survives_its_hole_being_taken(tmp_path):
    """A lead written as a half-row (2R) is keyed on that, not on the hole
    the router happened to land it in."""
    before = _ids(mkdata(tmp_path))
    d2 = tmp_path / "b"
    d2.mkdir()
    taken = _with_jumper(SNAKE_YAML, "  - {from: 2j, to: 16j, colour: YEL}\n")
    assert before <= _ids(mkdata(d2, island=taken))


def test_offsets_apply_as_a_translate_on_the_label_group(tmp_path):
    d = mkdata(tmp_path)
    (d / "labels.yaml").write_text(
        'offsets:\n  "bb1/p/C1": [12, -8]\n  "bb1/w/2R": [-3.26, 0]\n')
    page = bbnet.layout_html(d)
    assert 'data-lbl="bb1/p/C1" transform="translate(12,-8)"' in page
    assert 'data-lbl="bb1/w/2R" transform="translate(-3.3,0)"' in page
    # only the named labels move
    assert page.count('" transform="translate(') == 2
    assert 'data-lbl="bb1/p/C2" transform' not in page


@pytest.mark.parametrize("content", [None, "", "offsets:\n",
                                     "offsets: {}\n", "# nothing\n"])
def test_an_empty_labels_yaml_renders_byte_identically(tmp_path, content):
    d = mkdata(tmp_path)
    plain = bbnet.layout_html(d)
    if content is not None:
        (d / "labels.yaml").write_text(content)
    assert bbnet.layout_html(d) == plain
    # and the render API's default is the same page
    _p, colours, rules, islands, pm, panels = bbnet.load_data(d)
    from bbnet import model
    assert render.render_html(islands, model.derive(islands, pm), rules,
                              panels, colours=colours) == plain


def test_labels_yaml_is_reserved(tmp_path):
    assert "labels.yaml" in bbnet.RESERVED
    d = mkdata(tmp_path)
    (d / "labels.yaml").write_text('offsets:\n  "bb1/p/C1": [1, 2]\n')
    _p, _c, _r, islands, _pm, _pn = bbnet.load_data(d)
    assert set(islands) == {"bb1"}


def test_a_malformed_labels_yaml_raises(tmp_path):
    d = mkdata(tmp_path)
    (d / "labels.yaml").write_text('offsets:\n  "bb1/p/C1": [1]\n')
    with pytest.raises(ModelError, match="bb1/p/C1"):
        bbnet.load_label_offsets(d)


def test_check_warns_on_a_stale_offset_but_passes(tmp_path, capsys):
    d = mkdata(tmp_path)
    (d / "labels.yaml").write_text(
        'offsets:\n  "bb1/p/C1": [4, 4]\n  "bb1/p/GONE": [1, 1]\n')
    bbnet.main(["report", "--data-dir", str(d)])
    bbnet.main(["render", "--data-dir", str(d)])
    capsys.readouterr()
    assert bbnet.main(["check", "--data-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "WARNING: labels.yaml offset 'bb1/p/GONE'" in out
    assert "'bb1/p/C1'" not in out
    assert "check OK" in out


def test_check_guards_labels_yaml_like_any_other_input(tmp_path, capsys):
    """LAYOUT.html is a pure function of the YAML, labels.yaml included:
    moving a label without re-rendering is drift."""
    d = mkdata(tmp_path)
    bbnet.main(["report", "--data-dir", str(d)])
    bbnet.main(["render", "--data-dir", str(d)])
    (d / "labels.yaml").write_text('offsets:\n  "bb1/p/C1": [4, 4]\n')
    assert bbnet.main(["check", "--data-dir", str(d)]) == 1
    assert "DRIFT" in capsys.readouterr().out


def test_colliding_ids_are_made_unique_in_paint_order():
    labs = ['<g data-lbl="i/w/2R">a</g>', '<g data-lbl="i/w/2R">b</g>',
            '<text>no id</text>']
    out = render._finish_labels(labs, {"i/w/2R#2": (5, 0)})
    assert out[0] == '<g data-lbl="i/w/2R">a</g>'
    assert out[1] == '<g data-lbl="i/w/2R#2" transform="translate(5,0)">b</g>'
    assert out[2] == labs[2]


# --------------------------------------------------------- merge (pure)

def test_a_drag_adds_to_the_stored_offset():
    assert bbnet.merge_label_offsets({"a": (10, -5)}, {"a": [2, 3]}) == \
        {"a": (12.0, -2.0)}


def test_a_new_drag_is_stored_as_is_and_others_are_kept():
    assert bbnet.merge_label_offsets({"a": (1, 1)}, {"b": [0.04, -7.06]}) \
        == {"a": (1.0, 1.0), "b": (0.0, -7.1)}


def test_reset_drops_the_key_and_a_later_drag_starts_from_zero():
    stored = {"a": (10, 10), "b": (3, 3)}
    assert bbnet.merge_label_offsets(stored, {}, ["a"]) == {"b": (3.0, 3.0)}
    assert bbnet.merge_label_offsets(stored, {"a": [1, 2]}, ["a"]) == \
        {"a": (1.0, 2.0), "b": (3.0, 3.0)}


def test_a_drag_back_to_zero_drops_the_key():
    assert bbnet.merge_label_offsets({"a": (4, -2)}, {"a": [-4, 2]}) == {}


def test_merge_rejects_a_malformed_offset():
    with pytest.raises(ValueError):
        bbnet.merge_label_offsets({}, {"a": [1, "x"]})
    with pytest.raises(ValueError):
        bbnet.merge_label_offsets({}, {"a": [True, 1]})


def test_dump_round_trips_through_load(tmp_path):
    offs = {"z/p/R1": (1.5, -2.0), "a/w/rail:5V": (0.0, 9.0)}
    (tmp_path / "labels.yaml").write_text(bbnet.dump_label_offsets(offs))
    assert bbnet.load_label_offsets(tmp_path) == offs
    assert bbnet.dump_label_offsets({}).endswith("offsets: {}\n")


# --------------------------------------------------------------- serve

def test_the_served_page_is_the_file_plus_one_marker(tmp_path):
    d = mkdata(tmp_path)
    served = bbnet.served_html(d)
    assert bbnet.SERVE_MARKER in served
    assert served.replace(bbnet.SERVE_MARKER + "\n", "", 1) == \
        bbnet.layout_html(d)
    assert bbnet.SERVE_MARKER not in bbnet.layout_html(d)


@pytest.fixture
def server(tmp_path):
    d = mkdata(tmp_path)
    srv = bbnet.make_server(d, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield d, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _post(url, body):
    req = urllib.request.Request(
        url + "/labels", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())


def test_serve_get_post_round_trip(server, capsys):
    d, url = server
    assert bbnet.make_server.__defaults__[-1] == "127.0.0.1"
    with urllib.request.urlopen(url + "/", timeout=30) as r:
        page = r.read().decode()
    assert bbnet.SERVE_MARKER in page and 'data-lbl="bb1/p/C1"' in page

    assert _post(url, {"offsets": {"bb1/p/C1": [12, -8]}}) == \
        (200, {"ok": True, "offsets": 1})
    assert bbnet.load_label_offsets(d) == {"bb1/p/C1": (12.0, -8.0)}
    # the committed sheet is rewritten from it, so check stays in sync
    assert (d / "LAYOUT.html").read_text() == bbnet.layout_html(d)
    assert 'data-lbl="bb1/p/C1" transform="translate(12,-8)"' in \
        (d / "LAYOUT.html").read_text()

    # a second drag adds to the first; a reset removes
    _post(url, {"offsets": {"bb1/p/C1": [3, 3], "bb1/w/2R": [0, 5]}})
    assert bbnet.load_label_offsets(d) == {"bb1/p/C1": (15.0, -5.0),
                                           "bb1/w/2R": (0.0, 5.0)}
    _post(url, {"reset": ["bb1/p/C1"]})
    assert bbnet.load_label_offsets(d) == {"bb1/w/2R": (0.0, 5.0)}

    bbnet.main(["report", "--data-dir", str(d)])
    capsys.readouterr()
    assert bbnet.main(["check", "--data-dir", str(d)]) == 0


def test_serve_rejects_a_bad_body(server):
    d, url = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(url, {"offsets": {"bb1/p/C1": "left a bit"}})
    assert exc.value.code == 400
    assert not (d / "labels.yaml").exists()
