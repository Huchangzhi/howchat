import asyncio
import os
import queue
import socket
import struct
import tempfile

import pytest

from howchat import identity as identity_mod
from howchat.core import Core
from howchat.routing import Router
from howchat.store import Store
from howchat.transport import bluetooth as btmod
from howchat.transport.bluetooth import (
    BluetoothTransport,
    SERVICE_UUID,
    _bt_read_frame,
    _recv_exact,
)


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


def test_bt_discover_filters_services_and_keeps_ports():
    class FakeBT:
        RFCOMM = 1

        def __init__(self):
            self.found = []

        def discover_devices(self, **kw):
            return self.found

        def find_service(self, uuid=None, address=None):
            if address != "AA:BB:CC:DD:EE:FF":
                return []
            if uuid == SERVICE_UUID:
                return [{"port": 5, "name": "howchat"}]
            if uuid == btmod.SPP_UUID:
                return [{"port": 3, "name": "Serial Port"}]
            return []

    fake = FakeBT()
    fake.found = [("AA:BB:CC:DD:EE:FF", "Phone")]
    old = btmod.bluetooth
    btmod.bluetooth = fake
    try:
        out = btmod._bt_discover(SERVICE_UUID, duration=0)
        # 只保留 howchat 服务端口，且串口服务按名字被过滤
        assert out == [("aa:bb:cc:dd:ee:ff", 5, "Phone")]
    finally:
        btmod.bluetooth = old


class FakeBluetoothModule:
    """用真实 socket.socketpair 模拟 RFCOMM：listener 按 channel 排队 accept。"""

    RFCOMM = 1

    def __init__(self):
        self._listeners = {}
        self.discover_result = []
        self.services = {}
        self.advertised = []
        self.stopped = []

    def BluetoothSocket(self, *a, **k):
        return _FakeBTSock(self)

    def discover_devices(self, *a, **k):
        return self.discover_result

    def find_service(self, *a, **k):
        uuid = k.get("uuid")
        address = k.get("address")
        return self.services.get((uuid, address), [])

    def advertise_service(self, *a, **k):
        self.advertised.append((a, k))

    def stop_advertising(self, *a, **k):
        self.stopped.append((a, k))


class _FakeBTSock:
    def __init__(self, module):
        self.module = module
        self._channel = None
        self.timeout = None
        self._socket = None

    def settimeout(self, t):
        self.timeout = t
        if self._socket is not None:
            self._socket.settimeout(t)

    def bind(self, addr):
        self._channel = addr[1]

    def listen(self, n):
        self.module._listeners.setdefault(self._channel, queue.Queue())

    def accept(self):
        q = self.module._listeners.get(self._channel)
        try:
            end = q.get(timeout=self.timeout)
        except queue.Empty:
            raise socket.timeout("accept timeout")
        return end, ("00:00:00:00:00:00", 0)

    def connect(self, addr):
        port = addr[1]
        server_end, client_end = socket.socketpair()
        self.module._listeners.setdefault(port, queue.Queue()).put(server_end)
        self._socket = client_end
        return 0

    def sendall(self, data):
        return self._socket.sendall(data)

    def recv(self, n):
        return self._socket.recv(n)

    def shutdown(self, how):
        return self._socket.shutdown(how)

    def close(self):
        try:
            self._socket.close()
        except Exception:
            pass


async def run_bt_pair():
    fake = FakeBluetoothModule()
    old = btmod.bluetooth
    btmod.bluetooth = fake
    try:
        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        ida = identity_mod.load_or_create(os.path.join(d1, "data"))
        idb = identity_mod.load_or_create(os.path.join(d2, "data"))

        ra = Router(ida.peer_id)
        rb = Router(idb.peer_id)
        ta = BluetoothTransport(ida, ra, channel=4)
        tb = BluetoothTransport(idb, rb, channel=5)

        got = {}

        def on_a(conv, entry):
            got.setdefault("a", []).append((conv, entry))

        def on_b(conv, entry):
            got.setdefault("b", []).append((conv, entry))

        ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
        cb = Core(idb, Store(os.path.join(d2, "store")), rb, tb)
        ca.on_message = on_a
        cb.on_message = on_b

        await ca.start(broadcast=False)
        await cb.start(broadcast=False)
        assert ta.available and tb.available, "fake bluetooth 应可用"

        ok = await ta.connect_host("aa:bb:cc:dd:ee:ff", 5)
        assert ok
        for _ in range(300):
            if ida.peer_id in tb.neighbors() and idb.peer_id in ta.neighbors():
                break
            await asyncio.sleep(0.02)
        assert idb.peer_id in ta.neighbors(), "A 未注册 B"
        assert ida.peer_id in tb.neighbors(), "B 未注册 A"
        assert not ca.is_friend(idb.peer_id), "蓝牙直连不应自动成为好友"

        err = ca.send_text(idb.peer_id, "蓝牙消息你好")
        assert err is None, err
        for _ in range(300):
            if got.get("b"):
                break
            await asyncio.sleep(0.02)
        assert got["b"], "B 未收到蓝牙消息"
        assert got["b"][0][1]["text"] == "蓝牙消息你好"

        await ca.stop()
        await cb.stop()
    finally:
        btmod.bluetooth = old


def test_bt_pair_exchange():
    asyncio.run(run_bt_pair())


async def run_bt_scan_adopts_connection():
    fake = FakeBluetoothModule()
    old = btmod.bluetooth
    btmod.bluetooth = fake
    try:
        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        ida = identity_mod.load_or_create(os.path.join(d1, "data"))
        idb = identity_mod.load_or_create(os.path.join(d2, "data"))
        ra = Router(ida.peer_id)
        rb = Router(idb.peer_id)
        ta = BluetoothTransport(ida, ra, channel=4)
        tb = BluetoothTransport(idb, rb, channel=5)
        ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
        cb = Core(idb, Store(os.path.join(d2, "store")), rb, tb)

        await ca.start(broadcast=False)
        await cb.start(broadcast=False)

        # 发现阶段：B 的 SDP 服务被扫描到
        mac = "aa:bb:cc:dd:ee:ff"
        fake.discover_result = [(mac.upper(), "Phone")]
        fake.services[(SERVICE_UUID, mac.upper())] = [{"port": 5, "name": "howchat"}]

        devices = await ta.scan(duration=0)
        assert any(pid == idb.peer_id for _m, _p, _n, pid in devices), "scan 应识别出 B"

        for _ in range(300):
            if idb.peer_id in ta.neighbors() and ida.peer_id in tb.neighbors():
                break
            await asyncio.sleep(0.02)
        assert idb.peer_id in ta.neighbors(), "scan 后应建立稳定连接（无竞态丢连接）"
        assert ida.peer_id in tb.neighbors()

        err = ca.send_text(idb.peer_id, "扫描后消息")
        assert err is None, err
        received = []
        cb.on_message = lambda conv, entry: received.append((conv, entry))
        for _ in range(300):
            if received:
                break
            await asyncio.sleep(0.02)
        assert received and received[0][1]["text"] == "扫描后消息", "scan 建立的连接应可收发消息"

        await ca.stop()
        await cb.stop()
    finally:
        btmod.bluetooth = old


def test_bt_scan_adopts_connection():
    asyncio.run(run_bt_scan_adopts_connection())


async def run_bt_drop_on_eof():
    fake = FakeBluetoothModule()
    old = btmod.bluetooth
    btmod.bluetooth = fake
    try:
        d1 = tempfile.mkdtemp()
        d2 = tempfile.mkdtemp()
        ida = identity_mod.load_or_create(os.path.join(d1, "data"))
        idb = identity_mod.load_or_create(os.path.join(d2, "data"))
        ra = Router(ida.peer_id)
        rb = Router(idb.peer_id)
        ta = BluetoothTransport(ida, ra, channel=4)
        tb = BluetoothTransport(idb, rb, channel=5)
        ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
        cb = Core(idb, Store(os.path.join(d2, "store")), rb, tb)
        await ca.start(broadcast=False)
        await cb.start(broadcast=False)

        await ta.connect_host("aa:bb:cc:dd:ee:ff", 5)
        for _ in range(300):
            if idb.peer_id in ta.neighbors():
                break
            await asyncio.sleep(0.02)
        assert idb.peer_id in ta.neighbors()

        # 强制关闭 B 端连接：A 应在读循环检测到 EOF 后移除邻居
        conn = tb._connections[ida.peer_id]
        conn.sock.shutdown(socket.SHUT_RDWR)
        conn.sock.close()
        for _ in range(300):
            if idb.peer_id not in ta.neighbors():
                break
            await asyncio.sleep(0.02)
        assert idb.peer_id not in ta.neighbors(), "EOF 后应移除蓝牙邻居"

        await ca.stop()
        await cb.stop()
    finally:
        btmod.bluetooth = old


def test_bt_drop_on_eof():
    asyncio.run(run_bt_drop_on_eof())


async def run_bt_register_dedup():
    fake = FakeBluetoothModule()
    old = btmod.bluetooth
    btmod.bluetooth = fake
    try:
        d1 = tempfile.mkdtemp()
        ida = identity_mod.load_or_create(os.path.join(d1, "data"))
        ra = Router(ida.peer_id)
        ta = BluetoothTransport(ida, ra, channel=4)
        ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
        await ca.start(broadcast=False)

        a, b = socket.socketpair()
        info = {"nick": "乙", "x_pub": "", "ed_pub": ""}
        ta._register("peer-1", a, info)
        assert "peer-1" in ta.neighbors()
        # 同一 peer 再次注册应关闭新连接而不是覆盖旧连接
        c, d = socket.socketpair()
        ta._register("peer-1", c, info)
        assert ta._connections["peer-1"].sock is a
        assert ta._connections["peer-1"].sock is not c

        await ca.stop()
    finally:
        btmod.bluetooth = old


def test_bt_register_dedup():
    asyncio.run(run_bt_register_dedup())
