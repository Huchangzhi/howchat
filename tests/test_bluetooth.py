import socket
import struct

from howchat.transport.bluetooth import _bt_read_frame, _recv_exact


class FakeSock:
    def __init__(self, data, frag=1):
        self.data = bytearray(data)
        self.eof = False
        self.timeout = None
        self.frag = frag

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if self.eof:
            return b""
        if not self.data:
            raise socket.timeout("idle")
        chunk = self.data[: min(n, self.frag)]
        del self.data[: len(chunk)]
        return bytes(chunk)


def _frame(payload):
    return struct.pack(">I", len(payload)) + payload


def test_recv_exact_handles_partial_reads():
    frame = _frame(b"hello")
    sock = FakeSock(frame[:2] + frame[2:])
    assert _recv_exact(sock, 4) == frame[:4]


def test_read_frame_full():
    sock = FakeSock(_frame(b"abcdef"))
    assert _bt_read_frame(sock, 5.0) == b"abcdef"


def test_read_frame_fragmented_head_loops():
    payload = b"x" * 100
    frame = _frame(payload)
    sock = FakeSock(frame[:2] + frame[2:])
    assert _bt_read_frame(sock, 5.0) == payload


def test_read_frame_eof_returns_none():
    sock = FakeSock(b"")
    sock.eof = True
    assert _bt_read_frame(sock, 5.0) is None


def test_read_frame_idle_raises_timeout():
    sock = FakeSock(b"")
    try:
        _bt_read_frame(sock, 5.0)
        assert False, "空闲超时应抛出 socket.timeout 供上层保持连接"
    except socket.timeout:
        pass


def test_read_frame_short_body_returns_none():
    sock = FakeSock(b"\x00\x00\x00\x10" + b"abc")
    sock.eof = True
    assert _bt_read_frame(sock, 5.0) is None
