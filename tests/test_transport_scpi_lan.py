"""Unit tests for :mod:`bench.transport.scpi_lan`.

The transport is exercised against a tiny in-process TCP server that
plays back a recorded script of responses. This avoids both real
hardware and library mocks while still exercising the wire format.
"""

from __future__ import annotations

import socket
import threading
import time
from typing import Callable

import pytest

from oscilloscope_mcp.transport.scpi_lan import (
    ScpiError,
    ScpiLan,
    ScpiTransientError,
)


class _FakeScpiServer:
    """Thread-based TCP server that yields responses from a handler.

    The handler receives each received line (bytes, newline-stripped)
    and returns the bytes to send back, or ``None`` for write-only.
    """

    def __init__(self, handler: Callable[[bytes], bytes | None]) -> None:
        self.handler = handler
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        try:
            while True:
                client, _ = self.sock.accept()
                try:
                    buf = bytearray()
                    while True:
                        chunk = client.recv(4096)
                        if not chunk:
                            break
                        buf.extend(chunk)
                        while b"\n" in buf:
                            line, _, rest = buf.partition(b"\n")
                            buf = bytearray(rest)
                            response = self.handler(line)
                            if response is not None:
                                client.sendall(response)
                                # one-line response then close
                                client.shutdown(socket.SHUT_WR)
                                break
                finally:
                    client.close()
        except OSError:
            return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


@pytest.fixture
def fake_server() -> _FakeScpiServer:
    """Yields a fake server; auto-closed by test teardown."""
    server = _FakeScpiServer(lambda _: None)
    yield server
    server.close()


def test_query_returns_ascii_response_stripped() -> None:
    server = _FakeScpiServer(lambda line: b"RIGOL TECHNOLOGIES,DS1104Z\n"
                              if line.strip() == b"*IDN?" else None)
    try:
        client = ScpiLan(host="127.0.0.1", port=server.port, timeout_s=1.0)
        assert client.query("*IDN?") == "RIGOL TECHNOLOGIES,DS1104Z"
    finally:
        server.close()


def test_query_without_question_mark_raises() -> None:
    client = ScpiLan(host="127.0.0.1", port=1, timeout_s=0.1)
    with pytest.raises(ScpiError, match="requires a '\\?'"):
        client.query(":TRIG:MODE")


def test_write_sends_command_no_recv() -> None:
    received: list[bytes] = []

    def handler(line: bytes) -> bytes | None:
        received.append(line.strip())
        return None  # write-only; client should not block waiting

    server = _FakeScpiServer(handler)
    try:
        client = ScpiLan(host="127.0.0.1", port=server.port, timeout_s=1.0)
        client.write(":STOP")
        # Allow server thread to record.
        for _ in range(20):
            if received:
                break
            time.sleep(0.05)
        assert received == [b":STOP"]
    finally:
        server.close()


def test_connect_failure_raises_scpi_error() -> None:
    # Port 1 is privileged + almost certainly unbound. retries=0 keeps the
    # test fast (no backoff sleeps); the message still names the cause.
    client = ScpiLan(host="127.0.0.1", port=1, timeout_s=0.2, retries=0)
    with pytest.raises(ScpiError, match="failed to connect"):
        client.query("*IDN?")


def test_query_retries_after_transient_drop() -> None:
    """First connection is closed with no response (transient drop); the
    transport must retry and succeed on the next attempt.
    """
    state = {"n": 0}

    class _FlakyServer(_FakeScpiServer):
        def _serve(self) -> None:
            try:
                while True:
                    client, _ = self.sock.accept()
                    state["n"] += 1
                    if state["n"] == 1:
                        client.close()  # drop the first attempt
                        continue
                    try:
                        buf = bytearray()
                        while True:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            buf.extend(chunk)
                            if b"\n" in buf:
                                client.sendall(b"RIGOL,DS1104Z\n")
                                client.shutdown(socket.SHUT_WR)
                                break
                    finally:
                        client.close()
            except OSError:
                return

    server = _FlakyServer(lambda _: None)
    try:
        client = ScpiLan(
            host="127.0.0.1", port=server.port, timeout_s=1.0,
            retries=2, retry_backoff_s=0.01,
        )
        assert client.query("*IDN?") == "RIGOL,DS1104Z"
        assert state["n"] == 2  # one drop + one success
    finally:
        server.close()


def test_write_many_uses_single_connection_and_opc_sync() -> None:
    """write_many must send every command on ONE connection, in order, and
    block on a trailing *OPC? — the fix for dropped writes under churn.
    """
    received: list[bytes] = []
    conns = {"n": 0}

    class _BatchServer(_FakeScpiServer):
        def _serve(self) -> None:
            try:
                while True:
                    client, _ = self.sock.accept()
                    conns["n"] += 1
                    buf = bytearray()
                    try:
                        while True:
                            chunk = client.recv(4096)
                            if not chunk:
                                break
                            buf.extend(chunk)
                            while b"\n" in buf:
                                line, _, rest = buf.partition(b"\n")
                                buf = bytearray(rest)
                                if line.strip() == b"*OPC?":
                                    client.sendall(b"1\n")
                                    client.shutdown(socket.SHUT_WR)
                                else:
                                    received.append(line.strip())
                    finally:
                        client.close()
            except OSError:
                return

    server = _BatchServer(lambda _: None)
    try:
        client = ScpiLan(host="127.0.0.1", port=server.port, timeout_s=1.0)
        client.write_many([":TRIG:MODE NEDG", ":TRIG:NEDG:EDGE 2"])
        assert conns["n"] == 1  # all commands on a single connection
        assert received == [b":TRIG:MODE NEDG", b":TRIG:NEDG:EDGE 2"]
    finally:
        server.close()


def test_write_many_empty_is_noop() -> None:
    client = ScpiLan(host="127.0.0.1", port=1, timeout_s=0.1, retries=0)
    client.write_many([])  # must not connect / raise


def test_protocol_error_is_not_retried() -> None:
    """A malformed binary block is a deterministic protocol error — it must
    raise immediately without burning retries.
    """
    attempts = {"n": 0}

    def handler(line: bytes) -> bytes | None:
        if b"?" in line:
            attempts["n"] += 1
            return b"not a binary block\n"
        return None

    server = _FakeScpiServer(handler)
    try:
        client = ScpiLan(
            host="127.0.0.1", port=server.port, timeout_s=1.0,
            retries=3, retry_backoff_s=0.01,
        )
        with pytest.raises(ScpiError, match="missing '#' marker"):
            client.query_binary(":DISP:DATA?")
        assert attempts["n"] == 1  # not retried
    finally:
        server.close()


def test_query_binary_parses_definite_length_block() -> None:
    payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # 108 bytes
    header = b"#3" + f"{len(payload)}".encode() + payload

    def handler(line: bytes) -> bytes | None:
        if line.strip().startswith(b":DISP:DATA?"):
            return header
        return None

    server = _FakeScpiServer(handler)
    try:
        client = ScpiLan(host="127.0.0.1", port=server.port, timeout_s=1.0)
        data = client.query_binary(":DISP:DATA? ON,0,PNG")
        assert data == payload
    finally:
        server.close()


def test_query_binary_rejects_missing_header() -> None:
    def handler(line: bytes) -> bytes | None:
        if b"?" in line:
            return b"oops not a binary block\n"
        return None

    server = _FakeScpiServer(handler)
    try:
        client = ScpiLan(host="127.0.0.1", port=server.port, timeout_s=1.0)
        with pytest.raises(ScpiError, match="missing '#' marker"):
            client.query_binary(":DISP:DATA?")
    finally:
        server.close()
