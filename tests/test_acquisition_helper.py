"""Unit tests for :mod:`oscilloscope_mcp.helpers.acquisition`.

Pure functions over the capability profile — no hardware. We load the
real DS1104Z profile so these tests also guard the profile↔helper wiring
for the new ``capability.acquisition`` block.
"""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers import acquisition as ah
from oscilloscope_mcp.instruments import _load_profile


@pytest.fixture(scope="module")
def profile() -> dict:
    return _load_profile("rigol_ds1104z.yaml")


# --- channel number -------------------------------------------------------


@pytest.mark.parametrize("inp,expected", [
    (1, 1), (4, 4), ("2", 2), ("CHAN3", 3), ("chan1", 1), ("CHANnel4", 4),
])
def test_validate_channel_number_accepts_forms(profile, inp, expected):
    assert ah.validate_channel_number(profile, inp) == expected


@pytest.mark.parametrize("bad", [0, 5, 9, "CHAN5", "0"])
def test_validate_channel_number_out_of_range(profile, bad):
    with pytest.raises(ah.AcquisitionValidationError, match="out of range"):
        ah.validate_channel_number(profile, bad)


@pytest.mark.parametrize("bad", ["abc", "CHANX", 2.5])
def test_validate_channel_number_bad_type(profile, bad):
    with pytest.raises(ah.AcquisitionValidationError):
        ah.validate_channel_number(profile, bad)


# --- coupling / units / bw_limit ------------------------------------------


@pytest.mark.parametrize("inp,expected", [("ac", "AC"), ("DC", "DC"), ("gnd", "GND")])
def test_validate_coupling_normalizes(profile, inp, expected):
    assert ah.validate_coupling(profile, inp) == expected


def test_validate_coupling_rejects_unknown(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="valid:"):
        ah.validate_coupling(profile, "LFReject")  # trigger-only, not a CH coupling


@pytest.mark.parametrize("inp,expected", [
    ("VOLTage", "VOLTage"), ("volt", "VOLTage"), ("watt", "WATT"),
    ("AMPere", "AMPere"), ("amp", "AMPere"), ("UNKNown", "UNKNown"),
])
def test_validate_units_normalizes(profile, inp, expected):
    assert ah.validate_units(profile, inp) == expected


def test_validate_units_rejects_unknown(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="valid:"):
        ah.validate_units(profile, "DECIBEL")


@pytest.mark.parametrize("inp,expected", [("20M", "20M"), ("off", "OFF"), ("OFF", "OFF")])
def test_validate_bw_limit_normalizes(profile, inp, expected):
    assert ah.validate_bw_limit(profile, inp) == expected


def test_validate_bw_limit_rejects_unknown(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="valid:"):
        ah.validate_bw_limit(profile, "100M")


# --- probe ----------------------------------------------------------------


@pytest.mark.parametrize("inp,expected", [
    (1, 1.0), (10, 10.0), (0.1, 0.1), ("0.01", 0.01), (1000, 1000.0), (0.5, 0.5),
])
def test_validate_probe_accepts_listed_ratios(profile, inp, expected):
    assert ah.validate_probe(profile, inp) == pytest.approx(expected)


@pytest.mark.parametrize("bad", [3, 0.3, 1500, 7])
def test_validate_probe_rejects_unlisted(profile, bad):
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_probe(profile, bad)


def test_validate_probe_rejects_non_number(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="must be a number"):
        ah.validate_probe(profile, "ten")


# --- display / invert booleans --------------------------------------------


@pytest.mark.parametrize("inp,expected", [
    (True, True), (False, False), ("ON", True), ("off", False),
    (1, True), (0, False), ("yes", True), ("no", False),
])
def test_validate_display_coerces_bool(inp, expected):
    assert ah.validate_display(inp) is expected


def test_validate_invert_rejects_garbage():
    with pytest.raises(ah.AcquisitionValidationError, match="boolean"):
        ah.validate_invert("maybe")


# --- channel scale / offset range -----------------------------------------


def test_validate_channel_scale_in_range(profile):
    assert ah.validate_channel_scale(profile, 0.5) == pytest.approx(0.5)


def test_validate_channel_scale_rejects_non_positive(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="positive"):
        ah.validate_channel_scale(profile, 0)


def test_validate_channel_scale_above_max(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="above maximum"):
        ah.validate_channel_scale(profile, 1000.0)


def test_validate_channel_offset_in_range(profile):
    assert ah.validate_channel_offset(profile, -3.0) == pytest.approx(-3.0)


def test_validate_channel_offset_out_of_range(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="below minimum"):
        ah.validate_channel_offset(profile, -500.0)


# --- timebase -------------------------------------------------------------


@pytest.mark.parametrize("inp,expected", [
    ("MAIN", "MAIN"), ("main", "MAIN"), ("XY", "XY"), ("roll", "ROLL"),
])
def test_validate_timebase_mode_normalizes(profile, inp, expected):
    assert ah.validate_timebase_mode(profile, inp) == expected


def test_validate_timebase_mode_rejects_unknown(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="valid:"):
        ah.validate_timebase_mode(profile, "ZOOM")


def test_validate_timebase_scale_in_range(profile):
    assert ah.validate_timebase_scale(profile, 1e-6) == pytest.approx(1e-6)


def test_validate_timebase_scale_below_min(profile):
    # DS1104Z min is 5 ns/div; 1 ns is below.
    with pytest.raises(ah.AcquisitionValidationError, match="below minimum"):
        ah.validate_timebase_scale(profile, 1e-9)


def test_validate_timebase_scale_above_max(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="above maximum"):
        ah.validate_timebase_scale(profile, 100.0)


def test_validate_timebase_offset_in_range(profile):
    assert ah.validate_timebase_offset(profile, 1.0) == pytest.approx(1.0)


# --- missing acquisition block --------------------------------------------


def test_missing_acquisition_block_raises():
    bare = {"model": "X", "capability": {}}
    with pytest.raises(ah.AcquisitionValidationError, match="no capability.acquisition"):
        ah.validate_coupling(bare, "DC")


# --- acquire type ---------------------------------------------------------


@pytest.mark.parametrize("inp,expected", [
    ("NORMal", "NORMal"), ("normal", "NORMal"), ("NORM", "NORMal"),
    ("AVERages", "AVERages"), ("AVER", "AVERages"), ("averages", "AVERages"),
    ("PEAK", "PEAK"), ("peak", "PEAK"),
    ("HRESolution", "HRESolution"), ("HRES", "HRESolution"), ("hresolution", "HRESolution"),
])
def test_validate_acquire_type_normalizes(profile, inp, expected):
    assert ah.validate_acquire_type(profile, inp) == expected


@pytest.mark.parametrize("bad", ["BOGUS", "FAST", "SAMPLE", ""])
def test_validate_acquire_type_rejects_unknown(profile, bad):
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_acquire_type(profile, bad)


# --- averages -------------------------------------------------------------


@pytest.mark.parametrize("inp", [2, 4, 8, 16, 32, 64, 128, 256, 512, 1024])
def test_validate_averages_accepts_powers_of_2(profile, inp):
    assert ah.validate_averages(profile, inp) == inp


@pytest.mark.parametrize("bad", [1, 3, 5, 7, 2048, 0, -1])
def test_validate_averages_rejects_invalid(profile, bad):
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_averages(profile, bad)


def test_validate_averages_rejects_non_int(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="must be an integer"):
        ah.validate_averages(profile, "many")


# --- memory depth ---------------------------------------------------------


def test_validate_memory_depth_auto_1ch(profile):
    assert ah.validate_memory_depth(profile, 1, "AUTO") == "AUTO"


def test_validate_memory_depth_auto_4ch(profile):
    assert ah.validate_memory_depth(profile, 4, "auto") == "AUTO"


@pytest.mark.parametrize("channels,value", [
    (1, 12000), (1, 120000), (1, 1200000), (1, 12000000), (1, 24000000),
    (2, 6000), (2, 60000), (2, 600000), (2, 6000000), (2, 12000000),
    (4, 3000), (4, 30000), (4, 300000), (4, 3000000), (4, 6000000),
])
def test_validate_memory_depth_numeric_per_channel_count(profile, channels, value):
    assert ah.validate_memory_depth(profile, channels, value) == value


def test_validate_memory_depth_3ch_uses_4ch_table(profile):
    # 3 active channels → use the "4" bucket.
    assert ah.validate_memory_depth(profile, 3, 3000) == 3000
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_memory_depth(profile, 3, 12000)  # 12000 is 1-ch only


def test_validate_memory_depth_rejects_wrong_value_for_channel(profile):
    # 24000000 is only valid for 1 channel.
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_memory_depth(profile, 2, 24000000)


def test_validate_memory_depth_rejects_arbitrary_number(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="invalid"):
        ah.validate_memory_depth(profile, 1, 99999)


def test_validate_memory_depth_rejects_non_numeric(profile):
    with pytest.raises(ah.AcquisitionValidationError, match="must be"):
        ah.validate_memory_depth(profile, 1, "DEEP")
