"""Raw TCP socket SCPI transport (line-based + IEEE-488.2 binary block).

Most LXI-class oscilloscopes expose SCPI on a raw TCP port (RIGOL: 5555,
Siglent: 5025, Keysight: 5025). This module implements that transport
with zero third-party dependencies — just :mod:`socket`.

Why not ``pyvisa`` / ``python-vxi11``? They work, but the raw-socket path
is small (≈120 lines), transparent, and easy to reason about for the
project's trusted-local scope (CLAUDE.md §利用想定).

Connection model: each call opens a fresh connection. This is sub-
optimal for high-throughput streaming but is simple, avoids stale-state
bugs, and aligns with the P1 phasing decision (open/close per call,
revisit in P4 if a ``bench_session`` abstraction is needed).
"""

from __future__ import annotations

import socket
import time
from dataclasses import dataclass
from typing import Callable, TypeVar


class ScpiError(RuntimeError):
    """Raised on SCPI transport errors (timeout, malformed response, etc.)."""


class ScpiTransientError(ScpiError):
    """A *transient* transport failure — connection refused/dropped or a
    recv timeout. These are retried (see :attr:`ScpiLan.retries`) because
    the connection-per-call model churns sockets and a busy single-session
    instrument occasionally drops one. Protocol/validation errors are plain
    :class:`ScpiError` and are NOT retried.
    """


_T = TypeVar("_T")
# Raw socket failures that mean "try again" (subclasses of OSError).
_TRANSIENT = (ScpiTransientError, TimeoutError, ConnectionError)


@dataclass
class ScpiLan:
    """Line-based SCPI client over raw TCP.

    Parameters
    ----------
    host : str
        Instrument IP or hostname.
    port : int
        TCP port (5555 for RIGOL DS1000Z, 5025 for many others).
    timeout_s : float
        Socket timeout for both connect and recv.
    retries : int
        Extra attempts on a *transient* failure (default 2 → up to 3 tries).
        Absorbs dropped commands under the connection-per-call churn that
        happens when many SCPI ops run back-to-back.
    retry_backoff_s : float
        Base sleep between attempts; grows linearly per attempt.
    """

    host: str
    port: int = 5555
    timeout_s: float = 3.0
    retries: int = 2
    retry_backoff_s: float = 0.2

    def write(self, cmd: str) -> None:
        """Send a SCPI command that expects no response."""
        self._retry(lambda: self._write_once(cmd), cmd)

    def write_many(self, cmds: list[str]) -> None:
        """Send several SCPI commands on a SINGLE connection, in order,
        then block on ``*OPC?`` until the instrument finishes applying
        them.

        This is essential for a configuration burst (e.g. set trigger mode
        then its parameters): the connection-per-call path reconnects
        between every write, and a busy single-session instrument drops
        commands that arrive while it is still processing the previous one
        — so writes appear to succeed (sendall returns) but never take
        effect. Batching keeps ordering and lets ``*OPC?`` confirm
        completion before the next read-back. Commands are idempotent
        setpoints, so the whole batch is safe to retry.
        """
        if not cmds:
            return
        self._retry(lambda: self._write_many_once(cmds), "; ".join(cmds))

    def query(self, cmd: str, recv_max: int = 65536) -> str:
        """Send a SCPI query (``?``-terminated command) and return the
        ASCII response with the trailing newline stripped.

        Raises :class:`ScpiError` if the query does not contain ``?``,
        which usually indicates a programmer typo.
        """
        if "?" not in cmd:
            raise ScpiError(f"query() requires a '?' in command, got: {cmd!r}")
        data = self._retry(lambda: self._query_once(cmd, recv_max), cmd)
        return data.decode("ascii", errors="replace").rstrip("\r\n")

    def query_binary(self, cmd: str, recv_max: int = 4 * 1024 * 1024) -> bytes:
        """Send a SCPI query whose response is an IEEE-488.2 definite-
        length binary block: ``#<n><n-digits-of-length><payload>\\n``.

        Returns the raw payload bytes (header + trailing newline stripped).
        Used for :DISP:DATA? (screenshot PNG), :WAV:DATA? (raw samples).
        """
        if "?" not in cmd:
            raise ScpiError(f"query_binary() requires a '?' in command, got: {cmd!r}")
        return self._retry(lambda: self._query_binary_once(cmd, recv_max), cmd)

    # -- single-attempt I/O ------------------------------------------------

    def _write_once(self, cmd: str) -> None:
        with self._connect() as sock:
            sock.sendall(_encode_line(cmd))

    def _write_many_once(self, cmds: list[str]) -> None:
        with self._connect() as sock:
            blob = b"".join(_encode_line(c) for c in cmds)
            blob += _encode_line("*OPC?")  # sync: returns "1" when applied
            sock.sendall(blob)
            _recv_until_newline(sock, 64)  # block until the batch completes

    def _query_once(self, cmd: str, recv_max: int) -> bytes:
        with self._connect() as sock:
            sock.sendall(_encode_line(cmd))
            return _recv_until_newline(sock, recv_max)

    def _query_binary_once(self, cmd: str, recv_max: int) -> bytes:
        with self._connect() as sock:
            sock.sendall(_encode_line(cmd))
            return _recv_binary_block(sock, recv_max)

    def _retry(self, fn: Callable[[], _T], cmd: str) -> _T:
        last: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                return fn()
            except _TRANSIENT as e:
                last = e
                if attempt < self.retries:
                    time.sleep(self.retry_backoff_s * (attempt + 1))
                    continue
                raise ScpiError(
                    f"SCPI {cmd!r} failed after {attempt + 1} attempt(s): {e}"
                ) from e
        raise AssertionError("unreachable")  # pragma: no cover

    def _connect(self) -> socket.socket:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self.timeout_s)
        except OSError as e:
            raise ScpiTransientError(
                f"failed to connect to SCPI host {self.host}:{self.port}: {e}"
            ) from e
        sock.settimeout(self.timeout_s)
        return sock


def _encode_line(cmd: str) -> bytes:
    if not cmd.endswith("\n"):
        cmd = cmd + "\n"
    return cmd.encode("ascii")


def _recv_until_newline(sock: socket.socket, recv_max: int) -> bytes:
    """Read from socket until a newline or recv_max bytes."""
    buf = bytearray()
    while len(buf) < recv_max:
        chunk = sock.recv(min(4096, recv_max - len(buf)))
        if not chunk:
            break
        buf.extend(chunk)
        if b"\n" in chunk:
            break
    if not buf:
        raise ScpiTransientError("empty SCPI response (instrument closed connection?)")
    return bytes(buf)


def _recv_binary_block(sock: socket.socket, recv_max: int) -> bytes:
    """Parse IEEE-488.2 definite-length binary block: ``#N<digits><payload>``.

    ``#`` marker → 1 digit ``N`` (length of length field) → ``N`` digits
    of payload byte count → payload → optional ``\\n`` terminator.
    """
    header = _recv_exactly(sock, 2)
    if header[0:1] != b"#":
        raise ScpiError(
            f"binary block missing '#' marker, got header={header!r}"
        )
    try:
        n_digits = int(header[1:2])
    except ValueError as e:
        raise ScpiError(f"binary block length-of-length not a digit: {header!r}") from e
    if n_digits == 0:
        raise ScpiError("binary block with indefinite length (N=0) is not supported")
    length_bytes = _recv_exactly(sock, n_digits)
    try:
        payload_len = int(length_bytes.decode("ascii"))
    except ValueError as e:
        raise ScpiError(f"binary block payload length not numeric: {length_bytes!r}") from e
    if payload_len > recv_max:
        raise ScpiError(
            f"binary block payload {payload_len} bytes exceeds recv_max {recv_max}"
        )
    payload = _recv_exactly(sock, payload_len)
    # Some firmware appends a trailing newline; consume it best-effort.
    try:
        sock.settimeout(0.2)
        sock.recv(1)
    except OSError:
        pass
    return payload


def _recv_exactly(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ScpiTransientError(f"connection closed after {len(buf)}/{n} bytes")
        buf.extend(chunk)
    return bytes(buf)
