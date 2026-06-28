"""Unit tests for :mod:`oscilloscope_mcp.helpers.measure`.

Pure functions over the capability profile — no hardware. We load the
real DS1104Z profile so these tests also guard the profile↔helper wiring
(the measurement-item list lives in the shared family fragment).
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers import measure as mh
from oscilloscope_mcp.instruments import _load_profile


@pytest.fixture(scope="module")
def profile() -> dict:
    return _load_profile("rigol_ds1104z.yaml")


# --- normalize_item -------------------------------------------------------


@pytest.mark.parametrize("requested,expected_name", [
    ("FREQUENCY", "FREQUENCY"),
    ("frequency", "FREQUENCY"),
    ("FREQuency", "FREQUENCY"),   # SCPI keyword spelling
    ("FREQ", "FREQUENCY"),        # RIGOL short form
    ("vpp", "VPP"),
    ("VPP", "VPP"),
    ("vbase", "VBASE"),
    ("VBASe", "VBASE"),           # scpi keyword for VBASE
    ("overshoot", "OVERSHOOT"),
    ("rdelay", "RDELAY"),
])
def test_normalize_item_accepts_name_keyword_short_and_case(
    profile, requested, expected_name
):
    name, _spec = mh.normalize_item(profile, requested)
    assert name == expected_name


def test_normalize_item_rejects_unknown_with_valid_list(profile):
    with pytest.raises(mh.MeasureValidationError) as ei:
        mh.normalize_item(profile, "BOGUS")
    msg = str(ei.value)
    assert "BOGUS" in msg
    assert "FREQUENCY" in msg and "VPP" in msg  # enumerates valid options


def test_normalize_item_rejects_blank(profile):
    with pytest.raises(mh.MeasureValidationError):
        mh.normalize_item(profile, "   ")


# --- normalize_source -----------------------------------------------------


@pytest.mark.parametrize("src,expected", [
    ("1", "CHANnel1"),
    (2, "CHANnel2"),
    ("CHAN3", "CHANnel3"),
    ("channel4", "CHANnel4"),
    ("CHANnel1", "CHANnel1"),
])
def test_normalize_source_to_channel(profile, src, expected):
    assert mh.normalize_source(profile, src) == expected


def test_normalize_source_out_of_range_rejected(profile):
    with pytest.raises(mh.MeasureValidationError, match="out of range"):
        mh.normalize_source(profile, "9")


@pytest.mark.parametrize("bad", ["D0", "MATH", "AC", "xyz"])
def test_normalize_source_rejects_non_channel(profile, bad):
    with pytest.raises(mh.MeasureValidationError):
        mh.normalize_source(profile, bad)


# --- resolve_items / dual-source / units ----------------------------------


def test_resolve_items_preserves_order_and_normalizes(profile):
    resolved = mh.resolve_items(profile, ["freq", "VPP", "rtime"])
    assert [name for name, _ in resolved] == ["FREQUENCY", "VPP", "RTIME"]


def test_resolve_items_rejects_empty(profile):
    with pytest.raises(mh.MeasureValidationError, match="at least one"):
        mh.resolve_items(profile, [])


def test_resolve_items_rejects_unknown(profile):
    with pytest.raises(mh.MeasureValidationError, match="not supported"):
        mh.resolve_items(profile, ["VPP", "NOPE"])


@pytest.mark.parametrize("item,dual", [
    ("FREQUENCY", False), ("VPP", False),
    ("RDELAY", True), ("FDELAY", True), ("RPHASE", True), ("FPHASE", True),
])
def test_is_dual_source(profile, item, dual):
    _name, spec = mh.normalize_item(profile, item)
    assert mh.is_dual_source(spec) is dual


def test_units_for(profile):
    resolved = mh.resolve_items(profile, ["FREQUENCY", "VPP", "PERIOD", "RPHASE"])
    units = mh.units_for(resolved)
    assert units == {
        "FREQUENCY": "Hz", "VPP": "V", "PERIOD": "s", "RPHASE": "deg",
    }


# --- parse_measure_value --------------------------------------------------


@pytest.mark.parametrize("resp,expected", [
    ("1.000000e+03", 1000.0),
    ("1.234560E-06", 1.23456e-06),
    ("-2.5", -2.5),
    ("0.5", 0.5),
])
def test_parse_measure_value_parses_floats(resp, expected):
    assert mh.parse_measure_value(resp) == pytest.approx(expected)


@pytest.mark.parametrize("resp", [
    "9.9E37", "9.9e+37", "9.91E37", "1.0E38", "9.900000e+37",
])
def test_parse_measure_value_sentinel_to_none(resp):
    assert mh.parse_measure_value(resp) is None


@pytest.mark.parametrize("resp", ["", "  ", "NONE", "junk"])
def test_parse_measure_value_empty_or_junk_to_none(resp):
    assert mh.parse_measure_value(resp) is None


# --- validate_stat_type ---------------------------------------------------


@pytest.mark.parametrize("requested,expected", [
    ("CURRent", "CURRent"),
    ("current", "CURRent"),
    ("CURR", "CURRent"),       # short form
    ("MAXimum", "MAXimum"),
    ("maximum", "MAXimum"),
    ("MAX", "MAXimum"),
    ("MINimum", "MINimum"),
    ("MIN", "MINimum"),
    ("AVERages", "AVERages"),
    ("AVER", "AVERages"),
    ("averages", "AVERages"),
    ("DEViation", "DEViation"),
    ("DEV", "DEViation"),
    ("COUNt", "COUNt"),
    ("COUN", "COUNt"),
    ("count", "COUNt"),
])
def test_validate_stat_type_accepts_canonical_short_and_case(
    profile, requested, expected
):
    assert mh.validate_stat_type(profile, requested) == expected


def test_validate_stat_type_rejects_unknown(profile):
    with pytest.raises(mh.MeasureValidationError, match="not supported"):
        mh.validate_stat_type(profile, "BOGUS")


def test_validate_stat_type_rejects_blank(profile):
    with pytest.raises(mh.MeasureValidationError):
        mh.validate_stat_type(profile, "   ")


# --- validate_stat_mode ---------------------------------------------------


@pytest.mark.parametrize("requested,expected", [
    ("DIFFerence", "DIFFerence"),
    ("difference", "DIFFerence"),
    ("DIFF", "DIFFerence"),       # short form
    ("EXTRemum", "EXTRemum"),
    ("extremum", "EXTRemum"),
    ("EXTR", "EXTRemum"),
])
def test_validate_stat_mode_accepts_canonical_short_and_case(
    profile, requested, expected
):
    assert mh.validate_stat_mode(profile, requested) == expected


def test_validate_stat_mode_rejects_unknown(profile):
    with pytest.raises(mh.MeasureValidationError, match="not supported"):
        mh.validate_stat_mode(profile, "BOGUS")


def test_validate_stat_mode_rejects_blank(profile):
    with pytest.raises(mh.MeasureValidationError):
        mh.validate_stat_mode(profile, "  ")


# --- stat_types_list ------------------------------------------------------


def test_stat_types_list_returns_all_six(profile):
    types = mh.stat_types_list(profile)
    assert len(types) == 6
    assert "CURRent" in types
    assert "COUNt" in types


# --- missing measure block ------------------------------------------------


def test_missing_measure_block_raises():
    bare = {"model": "X", "capability": {}}
    with pytest.raises(mh.MeasureValidationError, match="no capability.measure"):
        mh.normalize_item(bare, "FREQUENCY")


# --- profile assertion (item list present in the family fragment) ---------


def test_profile_declares_full_measure_item_list(profile):
    """The shared DS1000Z family fragment must declare every measurement
    item the tool advertises — guards profile↔tool drift without hardware.
    """
    items = mh.measure_items(profile)
    expected = {
        # single-source
        "VMAX", "VMIN", "VPP", "VTOP", "VBASE", "VAMP", "VAVG", "VRMS",
        "OVERSHOOT", "PRESHOOT", "PERIOD", "FREQUENCY", "RTIME", "FTIME",
        "PWIDTH", "NWIDTH", "PDUTY", "NDUTY",
        # two-source
        "RDELAY", "FDELAY", "RPHASE", "FPHASE",
    }
    assert expected <= set(items)
    # The four delay/phase items are the only dual-source ones.
    dual = {name for name, spec in items.items() if mh.is_dual_source(spec)}
    assert dual == {"RDELAY", "FDELAY", "RPHASE", "FPHASE"}
