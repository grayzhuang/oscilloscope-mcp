"""Scope ABC — vendor-agnostic oscilloscope interface.

Every supported oscilloscope (RIGOL DS1000Z, Siglent SDS, Keysight DSOX,
…) ships a concrete subclass in
``oscilloscope_mcp/instruments/<vendor>_<series>.py``. The driver implements
each abstract method using the vendor's SCPI dialect. The MCP tool
layer in :mod:`oscilloscope_mcp.server` only ever touches the ABC, never a
concrete driver — that is what allows new instruments to be added by
dropping in two files (driver + profile).

The interface is intentionally narrow: IDN, basic setup queries,
screenshot with optional cursor / channel-label annotation, a raw SCPI
passthrough escape hatch, and (P2) typed trigger get/set validated
against the profile. Waveform capture and reductions arrive in P1.5 / P2.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class CursorPair:
    """One pair of manual cursors (A/B) on a specified source channel.

    Either time-axis (``ax_t_s`` + ``bx_t_s``) or voltage-axis
    (``ay_v`` + ``by_v``) coordinates may be set. The driver translates
    seconds/volts into the instrument's native pixel units using the
    current timebase and channel scale.
    """

    label: str
    source_channel: int
    ax_t_s: float | None = None
    bx_t_s: float | None = None
    ay_v: float | None = None
    by_v: float | None = None


@dataclass
class ScreenshotPlan:
    """Declarative annotation plan for a screenshot."""

    cursor_pairs: list[CursorPair] = field(default_factory=list)
    channel_labels: dict[int, str] = field(default_factory=dict)
    display_labels: bool = False
    image_format: str = "PNG"


@dataclass
class ScreenshotResult:
    """Returned by :meth:`Scope.screenshot`."""

    image_bytes: bytes
    image_format: str
    cursors_set: list[CursorPair]
    channel_labels_applied: dict[int, str]


@dataclass
class Waveform:
    """A captured single-channel waveform plus the time base needed to
    place each sample on the time axis.

    ``volts`` is the per-sample voltage (already scaled out of raw ADC
    counts by the driver). ``t0_s`` is the time of sample index 0 and
    ``dt_s`` is the per-sample interval, both in seconds — derived from
    the instrument's waveform preamble. The quantize → RLE pipeline
    (:mod:`oscilloscope_mcp.helpers`) consumes these; it converts to
    microseconds for the agent-facing output.
    """

    source: str                       # e.g. "CHAN1"
    volts: list[float] = field(default_factory=list)
    t0_s: float = 0.0                 # time of volts[0], seconds
    dt_s: float = 0.0                 # per-sample interval, seconds

    @property
    def n_samples(self) -> int:
        return len(self.volts)


@dataclass
class TriggerSetup:
    """Declarative trigger configuration request.

    ``mode`` selects the trigger type and the global parameters
    (``sweep`` / ``coupling`` / ``holdoff_s``) apply to every type. All
    type-specific parameters go in ``params`` as ``{canonical_name: value}``
    (e.g. ``{"source": "CHAN1", "slope": "NEG", "nth_edge": 2}``); the
    driver validates each key/value against the profile's per-mode schema,
    so any of the instrument's trigger types can be fully configured
    without bespoke fields. A field left ``None`` / empty is not touched.
    """

    mode: str | None = None          # e.g. "EDGE", "NEDG" (profile-validated)
    sweep: str | None = None         # AUTO | NORMAL | SINGLE
    coupling: str | None = None      # AC | DC | LFReject | HFReject
    holdoff_s: float | None = None   # trigger holdoff in seconds
    params: dict[str, object] = field(default_factory=dict)  # mode-specific

    def has_setup(self) -> bool:
        """True if any field requests a change (else this is a pure read)."""
        return (
            self.mode is not None
            or self.sweep is not None
            or self.coupling is not None
            or self.holdoff_s is not None
            or bool(self.params)
        )


@dataclass
class TriggerState:
    """Snapshot of the instrument's current trigger configuration.

    ``params`` holds the mode-specific parameters read back for the
    current ``mode`` (keyed by the same canonical names accepted by
    :class:`TriggerSetup`).
    """

    mode: str                        # canonical long-form keyword
    status: str | None = None        # :TRIG:STAT? → TD/WAIT/RUN/AUTO/STOP
    sweep: str | None = None
    coupling: str | None = None
    holdoff_s: float | None = None
    params: dict[str, object] = field(default_factory=dict)


@dataclass
class ChannelSetup:
    """Declarative vertical (channel) configuration request.

    ``channel`` selects the analog input (1-based, validated against
    ``capability.channels``); every other field is optional and a field
    left ``None`` is not touched — mirroring :class:`TriggerSetup`. The
    driver validates each value against the profile's
    ``capability.acquisition.channel`` block before sending any SCPI.
    """

    channel: int
    scale_v_per_div: float | None = None   # :CHANnel<n>:SCALe
    offset_v: float | None = None          # :CHANnel<n>:OFFSet
    coupling: str | None = None            # AC | DC | GND
    display: bool | None = None            # :CHANnel<n>:DISPlay
    probe: float | None = None             # attenuation ratio (0.01..1000)
    bw_limit: str | None = None            # 20M | OFF
    invert: bool | None = None             # :CHANnel<n>:INVert
    units: str | None = None               # VOLTage | WATT | AMPere | UNKNown

    def has_setup(self) -> bool:
        """True if any field requests a change (else this is a pure read)."""
        return any(
            getattr(self, f) is not None
            for f in (
                "scale_v_per_div", "offset_v", "coupling", "display",
                "probe", "bw_limit", "invert", "units",
            )
        )


@dataclass
class ChannelState:
    """Snapshot of one channel's current vertical configuration."""

    channel: int
    scale_v_per_div: float | None = None
    offset_v: float | None = None
    coupling: str | None = None
    display: bool | None = None
    probe: float | None = None
    bw_limit: str | None = None
    invert: bool | None = None
    units: str | None = None


@dataclass
class TimebaseSetup:
    """Declarative horizontal (timebase) configuration request.

    All fields optional; a field left ``None`` is not touched (mirrors
    :class:`TriggerSetup`). Validated against the profile's
    ``capability.acquisition.timebase`` block before any SCPI is sent.
    """

    s_per_div: float | None = None   # :TIMebase:MAIN:SCALe
    offset_s: float | None = None    # :TIMebase:MAIN:OFFSet
    mode: str | None = None          # MAIN | XY | ROLL

    def has_setup(self) -> bool:
        """True if any field requests a change (else this is a pure read)."""
        return (
            self.s_per_div is not None
            or self.offset_s is not None
            or self.mode is not None
        )


@dataclass
class TimebaseState:
    """Snapshot of the instrument's current horizontal configuration."""

    s_per_div: float | None = None
    offset_s: float | None = None
    mode: str | None = None


@dataclass
class MeasureStatResult:
    """One measurement item's statistical results.

    Each field corresponds to a ``:MEASure:STATistic:ITEM`` stat-type
    query. A ``None`` value means the stat was either not requested or
    the instrument returned its un-measurable sentinel (~9.9e37).
    """

    item: str
    source: str
    current: float | None = None
    maximum: float | None = None
    minimum: float | None = None
    average: float | None = None
    deviation: float | None = None
    count: int | None = None


@dataclass
class AcquireSetup:
    """Declarative acquisition mode configuration request.

    All fields optional; a field left ``None`` is not touched (mirrors
    :class:`TriggerSetup`). Validated against the profile's
    ``capability.acquisition.acquire`` block before any SCPI is sent.
    """

    type: str | None = None             # NORMal | AVERages | PEAK | HRESolution
    averages: int | None = None         # 2..1024 (powers of 2)
    memory_depth: str | int | None = None  # AUTO or numeric

    def has_setup(self) -> bool:
        """True if any field requests a change (else this is a pure read)."""
        return (
            self.type is not None
            or self.averages is not None
            or self.memory_depth is not None
        )


@dataclass
class AcquireState:
    """Snapshot of the instrument's current acquisition configuration."""

    type: str | None = None             # NORMal | AVERages | PEAK | HRESolution
    averages: int | None = None         # 2..1024 (powers of 2)
    memory_depth: str | int | None = None  # AUTO or numeric
    sample_rate: float | None = None    # read-only (Sa/s)


class Scope(ABC):
    """Abstract base class for all supported oscilloscopes.

    A driver concretely implements every abstract method. The base class
    holds the transport handle and capability profile so subclasses can
    consult ``self.profile`` (e.g. screen pixel dimensions for cursor
    coordinate translation) without re-loading.
    """

    def __init__(self, transport, profile: dict) -> None:
        self.transport = transport
        self.profile = profile

    # ------------------------------------------------------------------
    # Identification & raw escape hatch
    # ------------------------------------------------------------------

    @abstractmethod
    def idn(self) -> str:
        """Return the response to ``*IDN?`` (vendor,model,sn,fw)."""

    @abstractmethod
    def query_raw(self, scpi: str) -> str:
        """Send an arbitrary SCPI command/query and return the raw ASCII
        response (or empty string for non-query commands).

        Skill-driven exploration uses this; tool consumers should prefer
        the typed methods below so that ``caveats[]`` can be computed.
        """

    # ------------------------------------------------------------------
    # State snapshot (used by caveat_calc)
    # ------------------------------------------------------------------

    @abstractmethod
    def active_channel_count(self) -> int:
        """Number of channels currently set ``:DISP ON``. Used by
        :mod:`bench.helpers.caveat_calc` to compute sample-rate caveats.
        """

    @abstractmethod
    def timebase_s_per_div(self) -> float:
        """Current main-window timebase in seconds per division."""

    # ------------------------------------------------------------------
    # Acquisition setup — vertical (channel) + horizontal (timebase) (P2)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_channel(self, channel: int) -> "ChannelState":
        """Read one channel's current vertical configuration.

        ``channel`` is validated against ``capability.channels``;
        implementations read scale / offset / coupling / display / probe /
        bandwidth-limit / invert / units into a :class:`ChannelState`.
        """

    @abstractmethod
    def set_channel(self, setup: "ChannelSetup") -> "ChannelState":
        """Apply ``setup`` then return the resulting :class:`ChannelState`.

        Implementations must validate every non-``None`` field against the
        profile's ``capability.acquisition.channel`` block before sending
        any SCPI, so an unsupported coupling / probe / unit / out-of-range
        scale is rejected rather than silently mis-sent.
        """

    @abstractmethod
    def get_timebase(self) -> "TimebaseState":
        """Read the current horizontal configuration into a TimebaseState."""

    @abstractmethod
    def set_timebase(self, setup: "TimebaseSetup") -> "TimebaseState":
        """Apply ``setup`` then return the resulting :class:`TimebaseState`.

        Implementations must validate ``mode`` and the numeric ranges
        against the profile before sending any SCPI.
        """

    # ------------------------------------------------------------------
    # Acquisition mode (acquire type / averages / memory depth)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_acquire(self) -> "AcquireState":
        """Read the current acquisition mode configuration.

        Implementations read ``:ACQ:TYPE?``, ``:ACQ:AVER?``,
        ``:ACQ:MDEP?``, and ``:ACQ:SRAT?`` into an :class:`AcquireState`.
        """

    @abstractmethod
    def set_acquire(self, setup: "AcquireSetup") -> "AcquireState":
        """Apply ``setup`` then return the resulting :class:`AcquireState`.

        Implementations must validate every non-``None`` field against the
        profile's ``capability.acquisition.acquire`` block before sending
        any SCPI, so an unsupported type / averages count / memory depth
        is rejected rather than silently mis-sent.
        """

    # ------------------------------------------------------------------
    # Trigger (P2)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_trigger(self) -> "TriggerState":
        """Read the current trigger configuration into a TriggerState.

        Implementations normalize the vendor's ``:TRIG:MODE?`` short form
        to the canonical long-form keyword declared in the profile.
        """

    @abstractmethod
    def set_trigger(self, setup: "TriggerSetup") -> "TriggerState":
        """Apply ``setup`` then return the resulting :class:`TriggerState`.

        Implementations must validate ``setup.mode`` (and sweep/coupling)
        against the profile before sending any SCPI, so an unsupported
        trigger type is rejected rather than silently mis-sent.
        """

    @abstractmethod
    def run_control(self, action: str) -> str:
        """Issue a run/arm control action (RUN / STOP / SINGLE / FORCE),
        resolved against the profile's ``trigger.run_control`` map. Returns
        the canonical action name applied; raises on an unknown action.
        """

    # ------------------------------------------------------------------
    # Automatic measurements (P2)
    # ------------------------------------------------------------------

    @abstractmethod
    def measure(
        self,
        items: list[str],
        source: str = "CHAN1",
        source2: str | None = None,
    ) -> dict[str, float | None]:
        """Read scope-side automatic measurements for ``items`` on a source.

        ``items`` are canonical measurement names (e.g. ``"FREQUENCY"``,
        ``"VPP"``) validated against the profile's
        ``capability.measure.items`` schema. ``source`` is the primary
        channel; ``source2`` is required for the two-source delay / phase
        items (``RDELAY`` / ``FDELAY`` / ``RPHASE`` / ``FPHASE``) and
        ignored for single-source items.

        Returns ``{canonical_name: value_or_None}`` keyed by the same
        canonical names, where a value the instrument reports as
        un-measurable (its ~9.9e37 sentinel) maps to ``None``.

        Implementations must validate ``items`` / ``source`` against the
        profile before issuing any SCPI, so an unknown item is rejected
        rather than silently mis-sent.
        """

    @abstractmethod
    def measure_statistics(
        self,
        items: list[str],
        source: str = "CHAN1",
        stat_types: list[str] | None = None,
    ) -> list["MeasureStatResult"]:
        """Read measurement statistics for ``items`` on a source channel.

        Queries ``:MEASure:STATistic:ITEM <stat_type>,<item>,<src>`` for
        each combination. ``stat_types`` defaults to all six if ``None``.
        The instrument must have statistics display enabled before this
        call (:MEASure:STATistic:DISPlay ON).

        Returns one :class:`MeasureStatResult` per item containing the
        requested stat-type values. Un-measurable values (~9.9e37) map to
        ``None``; COUNt is parsed as ``int``.
        """

    # ------------------------------------------------------------------
    # Waveform capture (P1.5)
    # ------------------------------------------------------------------

    @abstractmethod
    def read_waveform(self, source: str, mode: str = "NORMal") -> "Waveform":
        """Capture a single-channel waveform and return it as a
        :class:`Waveform` (scaled volts + time base).

        Implementations select ``source`` (e.g. ``"CHAN1"``), configure
        the vendor's waveform read mode/format, read the preamble to get
        the scaling factors, then pull the sample block and convert raw
        counts to volts. The result feeds the quantize → RLE → edges
        pipeline in :mod:`oscilloscope_mcp.helpers`; the driver returns
        raw volts (not a digital stream) so the threshold / hysteresis
        stay tool-level parameters.

        ``mode`` selects the waveform read strategy:

        - ``"NORMal"`` (default): reads the ~1200-point screen-memory
          (decimated). Works while the scope is running or stopped.
        - ``"RAW"``: reads the full ADC acquisition memory (up to 24M
          points on DS1000Z). The scope **must** be stopped (status
          STOP or TD) before calling — raises :class:`RuntimeError` if
          the scope is still running.
        """

    # ------------------------------------------------------------------
    # Screenshot + annotation (P1)
    # ------------------------------------------------------------------

    @abstractmethod
    def screenshot(self, plan: ScreenshotPlan) -> ScreenshotResult:
        """Apply ``plan`` annotations then return the screen as bytes.

        Implementations are expected to:

        1. Set channel labels (``:CHAN<n>:LAB`` + ``:DISP:LABS``) per
           ``plan.channel_labels``.
        2. Configure manual cursors per ``plan.cursor_pairs``.
        3. Dump the screen via the vendor's screenshot SCPI in the
           requested ``plan.image_format``.

        The returned :class:`ScreenshotResult` echoes back the cursors
        and labels that were actually applied (driver may quantize or
        truncate values to instrument resolution).
        """
