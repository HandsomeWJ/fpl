import pytest

from copycat_core.fix import parse_reveal
from tests.conftest import TOM_BENCH, TOM_SQUAD, TOM_STARTERS


def test_full_squad_from_front_face_only(reveal_html):
    fix = parse_reveal(reveal_html, "Tom Dollimore")
    assert fix["squad"] == TOM_SQUAD
    # The consensus back face holds Kelleher/Saka; a bare .fffPitch would pick them up.
    assert "Kelleher" not in fix["squad"] and "Saka" not in fix["squad"]


def test_starters_and_bench_keep_dom_order(reveal_html):
    fix = parse_reveal(reveal_html, "Tom Dollimore")
    assert fix["starters"] == TOM_STARTERS and fix["starters"][0] == "Raya"
    assert fix["bench"] == TOM_BENCH and fix["bench"][0] == "Scherpen"


def test_armbands_are_title_attributes(reveal_html):
    fix = parse_reveal(reveal_html, "Tom Dollimore")
    assert fix["captain"] == "B.Fernandes"
    assert fix["vice"] == "Wissa"


def test_chips_transfer_log_and_gameweek(reveal_html):
    fix = parse_reveal(reveal_html, "Tom Dollimore")
    assert fix["chips"] == {"WC1": "available", "WC2": "available", "TC": "available",
                            "FH": "active", "BB": "gw 1"}
    assert fix["gw"] == 3
    assert fix["transfers"] == [{"out": "Calvert-Lewin", "in": "Wissa"},
                                {"out": "Maguire", "in": "Gvardiol"},
                                {"out": "Mbeumo", "in": "Elanga"}]
    assert fix["updated"].startswith("Aug. 30, 2026")


def test_other_manager_is_a_separate_section(reveal_html):
    fix = parse_reveal(reveal_html, "Other Manager")
    assert fix["squad"] == ["Kelleher"] and fix["transfers"] == []


def test_unknown_manager_raises(reveal_html):
    with pytest.raises(RuntimeError, match="not found on the reveal page"):
        parse_reveal(reveal_html, "Nobody")
