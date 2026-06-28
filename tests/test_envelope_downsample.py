"""Unit tests for the envelope_downsample helper."""

from __future__ import annotations

import pytest

from oscilloscope_mcp.helpers.envelope_downsample import envelope_downsample


def test_empty_input() -> None:
    result = envelope_downsample([], t0_us=0.0, dt_us=1.0)
    assert result == {"n_blocks": 0, "block_size": 0, "envelope": []}


def test_single_sample() -> None:
    result = envelope_downsample([2.5], t0_us=10.0, dt_us=1.0)
    assert result["n_blocks"] == 1
    assert result["block_size"] == 1
    assert len(result["envelope"]) == 1
    assert result["envelope"][0] == {"t_us": 10.0, "min_v": 2.5, "max_v": 2.5}


def test_shorter_than_target_passthrough() -> None:
    """If len(volts) <= target_points, each sample becomes its own block."""
    volts = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=2.0, target_points=10)
    assert result["n_blocks"] == 5
    assert result["block_size"] == 1
    # Each block is a single sample: min == max
    for i, env in enumerate(result["envelope"]):
        assert env["min_v"] == volts[i]
        assert env["max_v"] == volts[i]
        assert env["t_us"] == pytest.approx(i * 2.0)


def test_exact_equal_to_target_passthrough() -> None:
    """If len(volts) == target_points, still passthrough."""
    volts = [float(i) for i in range(5)]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=5)
    assert result["n_blocks"] == 5
    assert result["block_size"] == 1


def test_exact_division() -> None:
    """1000 samples with target_points=10 → blocks of 100."""
    volts = [float(i) for i in range(1000)]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=10)
    assert result["block_size"] == 100
    assert result["n_blocks"] == 10

    # First block: samples 0..99
    assert result["envelope"][0]["t_us"] == pytest.approx(0.0)
    assert result["envelope"][0]["min_v"] == pytest.approx(0.0)
    assert result["envelope"][0]["max_v"] == pytest.approx(99.0)

    # Last block: samples 900..999
    assert result["envelope"][9]["t_us"] == pytest.approx(900.0)
    assert result["envelope"][9]["min_v"] == pytest.approx(900.0)
    assert result["envelope"][9]["max_v"] == pytest.approx(999.0)


def test_non_exact_division() -> None:
    """1050 samples with target_points=10 → block_size=ceil(1050/10)=105."""
    volts = [float(i) for i in range(1050)]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=10)
    assert result["block_size"] == 105
    # 1050 / 105 = 10 blocks exactly
    assert result["n_blocks"] == 10

    # Verify all samples are accounted for
    total_samples = sum(1 for _ in range(0, 1050, 105))
    # Actually check by summing block spans
    assert result["n_blocks"] * result["block_size"] >= 1050


def test_non_exact_division_remainder() -> None:
    """When n % block_size != 0, the last block is smaller."""
    # 13 samples, target 4 → block_size = ceil(13/4) = 4
    # Blocks: [0..3], [4..7], [8..11], [12] (last block has 1 sample)
    volts = [float(i) for i in range(13)]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=4)
    assert result["block_size"] == 4  # ceil(13/4) = 4
    # With block_size=4: blocks at 0,4,8,12 → 4 blocks
    assert result["n_blocks"] == 4
    # Last block is just sample 12
    assert result["envelope"][-1]["min_v"] == pytest.approx(12.0)
    assert result["envelope"][-1]["max_v"] == pytest.approx(12.0)


def test_time_offset_applied() -> None:
    """t0_us offset should shift all block times."""
    volts = [1.0, 2.0, 3.0, 4.0]
    result = envelope_downsample(volts, t0_us=100.0, dt_us=5.0, target_points=2)
    # block_size = ceil(4/2) = 2
    assert result["envelope"][0]["t_us"] == pytest.approx(100.0)
    assert result["envelope"][1]["t_us"] == pytest.approx(110.0)  # 100 + 2*5


def test_min_max_captures_extremes() -> None:
    """Envelope should capture the actual min and max within each block."""
    # Sawtooth that goes up then down within blocks
    volts = [0.0, 5.0, -1.0, 3.0,   # block 1: min=-1, max=5
             2.0, 2.0, 2.0, 2.0]    # block 2: min=2, max=2
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=2)
    assert result["block_size"] == 4
    assert result["envelope"][0]["min_v"] == pytest.approx(-1.0)
    assert result["envelope"][0]["max_v"] == pytest.approx(5.0)
    assert result["envelope"][1]["min_v"] == pytest.approx(2.0)
    assert result["envelope"][1]["max_v"] == pytest.approx(2.0)


def test_negative_voltages() -> None:
    """Works with negative voltage values."""
    volts = [-3.0, -1.0, -2.0, -4.0]
    result = envelope_downsample(volts, t0_us=0.0, dt_us=1.0, target_points=2)
    assert result["envelope"][0]["min_v"] == pytest.approx(-3.0)
    assert result["envelope"][0]["max_v"] == pytest.approx(-1.0)
