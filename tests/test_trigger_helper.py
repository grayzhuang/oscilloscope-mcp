"""Unit tests for :mod:`oscilloscope_mcp.helpers.trigger`.

Pure functions over the capability profile — no hardware. We load the
real DS1104Z profile so these tests also guard the profile↔helper wiring.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers import trigger as th
from oscilloscope_mcp.instruments import _load_profile


@pytest.fixture(scope="module")
def profile() -> dict:
    return _load_profile("rigol_ds1104z.yaml")


# --- normalize_trigger_keyword -------------------------------------------


@pytest.mark.parametrize("requested,expected_keyword", [
    ("edge", "EDGE"),
    ("EDGE", "EDGE"),
    ("PULSe", "PULSe"),
    ("pulse", "PULSe"),
    ("PULS", "PULSe"),     # short / query form
    ("spi", "SPI"),
    ("DURation", "DURation"),
    ("DUR", "DURation"),
])
def test_normalize_accepts_long_short_and_case(profile, requested, expected_keyword):
    assert th.normalize_trigger_keyword(profile, requested)["keyword"] == expected_keyword


def test_normalize_rejects_unknown_with_valid_list(profile):
    with pytest.raises(th.TriggerValidationError) as ei:
        th.normalize_trigger_keyword(profile, "BOGUS")
    msg = str(ei.value)
    assert "BOGUS" in msg
    assert "EDGE" in msg and "SPI" in msg  # enumerates valid options


def test_normalize_rejects_blank(profile):
    with pytest.raises(th.TriggerValidationError):
        th.normalize_trigger_keyword(profile, "   ")


# --- keyword_from_query ---------------------------------------------------


@pytest.mark.parametrize("query_resp,expected", [
    ("EDGE", "EDGE"),
    ("PULS", "PULSe"),
    ("RUNT", "RUNT"),
    ("DUR", "DURation"),
])
def test_keyword_from_query_maps_short_form(profile, query_resp, expected):
    assert th.keyword_from_query(profile, query_resp) == expected


def test_keyword_from_query_passes_through_unknown(profile):
    assert th.keyword_from_query(profile, "ZZZ") == "ZZZ"


# --- validate_sweep / validate_coupling ----------------------------------


@pytest.mark.parametrize("inp,expected", [
    ("AUTO", "AUTO"), ("auto", "AUTO"),
    ("NORMal", "NORMAL"), ("norm", "NORMAL"),
    ("SINGle", "SINGLE"), ("single", "SINGLE"),
])
def test_validate_sweep_normalizes(profile, inp, expected):
    assert th.validate_sweep(profile, inp) == expected


def test_validate_sweep_rejects_unknown(profile):
    with pytest.raises(th.TriggerValidationError):
        th.validate_sweep(profile, "CONTINUOUS")


@pytest.mark.parametrize("inp,expected", [
    ("ac", "AC"), ("DC", "DC"), ("lfreject", "LFReject"), ("HFREJECT", "HFReject"),
])
def test_validate_coupling_normalizes(profile, inp, expected):
    assert th.validate_coupling(profile, inp) == expected


def test_validate_coupling_rejects_unknown(profile):
    with pytest.raises(th.TriggerValidationError):
        th.validate_coupling(profile, "BANDPASS")


# --- caveats --------------------------------------------------------------


def test_standard_type_has_no_caveat(profile):
    edge = th.normalize_trigger_keyword(profile, "EDGE")
    assert th.caveats_for_trigger_type(edge) == []


def test_option_type_emits_license_caveat(profile):
    runt = th.normalize_trigger_keyword(profile, "RUNT")
    caveats = th.caveats_for_trigger_type(runt)
    assert len(caveats) == 1
    assert "option" in caveats[0].lower()


# --- missing trigger block ------------------------------------------------


def test_missing_trigger_block_raises():
    bare = {"model": "X", "capability": {}}
    with pytest.raises(th.TriggerValidationError, match="no capability.trigger"):
        th.normalize_trigger_keyword(bare, "EDGE")


# --- generic parameter engine --------------------------------------------


def _entry(profile, keyword):
    return th.normalize_trigger_keyword(profile, keyword)


def test_build_param_writes_edge(profile):
    writes = th.build_param_writes(
        profile, _entry(profile, "EDGE"),
        {"source": "2", "slope": "rising", "level_v": 1.5},
    )
    assert writes == [
        (":TRIGger:EDGe:SOURce", "CHAN2"),
        (":TRIGger:EDGe:SLOPe", "POSitive"),   # 'rising' alias → long form
        (":TRIGger:EDGe:LEVel", "1.5"),
    ]


def test_build_param_writes_nth_edge(profile):
    writes = dict(th.build_param_writes(
        profile, _entry(profile, "NEDG"),
        {"source": "CHAN1", "slope": "NEG", "nth_edge": 2, "idle_s": 2e-3},
    ))
    assert writes[":TRIGger:NEDGe:SOURce"] == "CHAN1"
    assert writes[":TRIGger:NEDGe:SLOPe"] == "NEGative"
    assert writes[":TRIGger:NEDGe:EDGE"] == "2"
    assert writes[":TRIGger:NEDGe:IDLE"] == "0.002"


def test_build_param_writes_rejects_unknown_param(profile):
    with pytest.raises(th.TriggerValidationError, match="not valid for trigger mode"):
        th.build_param_writes(profile, _entry(profile, "EDGE"), {"bogus": 1})


def test_enum_rejects_bad_value(profile):
    with pytest.raises(th.TriggerValidationError, match="valid:"):
        th.build_param_writes(profile, _entry(profile, "EDGE"), {"slope": "sideways"})


@pytest.mark.parametrize("src,expected", [("1", "CHAN1"), ("CHAN3", "CHAN3"), ("AC", "AC")])
def test_edge_source_accepts_channels_and_ac(profile, src, expected):
    w = th.build_param_writes(profile, _entry(profile, "EDGE"), {"source": src})
    assert w[0][1] == expected


def test_source_out_of_range_rejected(profile):
    with pytest.raises(th.TriggerValidationError, match="out of range"):
        th.build_param_writes(profile, _entry(profile, "EDGE"), {"source": "9"})


def test_named_source_rejected_when_not_allowed(profile):
    # PULSe source has no 'named: [AC]', so AC is invalid there.
    with pytest.raises(th.TriggerValidationError):
        th.build_param_writes(profile, _entry(profile, "PULSe"), {"source": "AC"})


def test_digital_source_rejected(profile):
    with pytest.raises(th.TriggerValidationError):
        th.build_param_writes(profile, _entry(profile, "EDGE"), {"source": "D0"})


def test_real_range_enforced(profile):
    # PULSe width_s min is 8ns; 1ns is below.
    with pytest.raises(th.TriggerValidationError, match="below minimum"):
        th.build_param_writes(profile, _entry(profile, "PULSe"), {"width_s": 1e-9})


@pytest.mark.parametrize("val,ok", [(2, True), (0, False), (70000, False)])
def test_int_range_and_type(profile, val, ok):
    call = lambda: th.build_param_writes(
        profile, _entry(profile, "NEDG"), {"nth_edge": val})
    if ok:
        assert call()[0] == (":TRIGger:NEDGe:EDGE", str(val))
    else:
        with pytest.raises(th.TriggerValidationError):
            call()


def test_int_rejects_non_whole(profile):
    with pytest.raises(th.TriggerValidationError, match="whole number"):
        th.build_param_writes(profile, _entry(profile, "NEDG"), {"nth_edge": 2.5})


def test_pattern_tokens(profile):
    w = th.build_param_writes(profile, _entry(profile, "PATTern"), {"pattern": "H,L,X,X"})
    assert w[0] == (":TRIGger:PATTern:PATTern", "H,L,X,X")
    # list form
    w2 = th.build_param_writes(profile, _entry(profile, "PATTern"), {"pattern": ["h", "f"]})
    assert w2[0] == (":TRIGger:PATTern:PATTern", "H,F")
    with pytest.raises(th.TriggerValidationError, match="symbol"):
        th.build_param_writes(profile, _entry(profile, "PATTern"), {"pattern": "H,Z"})


def test_chan_level(profile):
    w = th.build_param_writes(profile, _entry(profile, "PATTern"), {"level": "CHAN1,1.5"})
    assert w[0] == (":TRIGger:PATTern:LEVel", "CHAN1,1.5")


def test_numeric_enum_rs232(profile):
    w = dict(th.build_param_writes(
        profile, _entry(profile, "RS232"), {"stop_bits": 2, "width": 8, "baud": 115200}))
    assert w[":TRIGger:RS232:STOP"] == "2"
    assert w[":TRIGger:RS232:WIDTh"] == "8"
    assert w[":TRIGger:RS232:BAUD"] == "115200"


def test_param_read_queries_and_parse(profile):
    queries = th.param_read_queries(_entry(profile, "NEDG"))
    q, spec = queries["nth_edge"]
    assert q == ":TRIGger:NEDGe:EDGE?"
    assert th.parse_param_value(spec, "2") == 2
    qi, si = queries["idle_s"]
    assert th.parse_param_value(si, "1.600000e-08") == pytest.approx(1.6e-8)
    assert th.parse_param_value({"kind": "enum"}, "NEG") == "NEG"
    assert th.parse_param_value({"kind": "real"}, "") is None


def test_describe_mode_params(profile):
    desc = th.describe_mode_params(_entry(profile, "SPI"))
    assert "when" in desc and desc["when"]["kind"] == "enum"
    assert "CS" in desc["when"]["values"]
    assert desc["timeout_s"]["scpi"] == ":TRIGger:SPI:TIMeout"


# --- run control ----------------------------------------------------------


@pytest.mark.parametrize("action,scpi", [
    ("RUN", ":RUN"),
    ("stop", ":STOP"),
    ("Single", ":SINGle"),
    ("FORCE", ":TFORce"),
])
def test_resolve_run_action(profile, action, scpi):
    name, resolved = th.resolve_run_action(profile, action)
    assert resolved == scpi
    assert name.upper() == action.upper()


def test_resolve_run_action_rejects_unknown(profile):
    with pytest.raises(th.TriggerValidationError, match="valid:"):
        th.resolve_run_action(profile, "PAUSE")
