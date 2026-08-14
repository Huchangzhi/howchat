import asyncio
import base64
import socket
import struct
import time

from howchat import protocol
from howchat.routing import ReplayError
from howchat.transport import Transport

try:
    import bluetooth
    BT_NAME = getattr(bluetooth, "__name__", "bluetooth")
except ImportError:
    bluetooth = None
    BT_NAME = None

SERVICE_NAME = "howchat"
SERVICE_UUID = "8f3b6a1e-2f4a-4b7c-9d0e-1a2b3c4d5e6f"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
SCAN_INTERVAL = 8.0
IDLE_TIMEOUT = 30.0
HANDSHAKE_TIMEOUT = 5.0


class BluetoothTransport(Transport):
    kind = "bluetooth"

    def __init__(self, identity, router, channel=4, uuid=SERVICE_UUID, enable_scan=True):
        super().__init__(identity, router)
        self.channel = channel
        self.uuid = uuid
        self.enable_scan = enable_scan
        self.available = bluetooth is not None
        self._connections = {}
        self._cooldown = {}
        self._server_sock = None
        self._stop = False
        self._tasks = []
        if self.available:
            router.add_transport(self)

    def neighbors(self):
        return set(self._connections.keys())

    def known_peers(self):
        return set(self._cooldown.keys())

    async def start(self, broadcast=True):
        if not self.available:
            return
        try:
            self._server_sock, ch = await asyncio.to_thread(_bt_listen, self.channel)
            self.channel = ch
            await asyncio.to_thread(_bt_advertise, self._server_sock, SERVICE_NAME, self.uuid)
        except Exception:
            self._server_sock = None
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._accept_loop()))
        if self.enable_scan:
            self._tasks.append(loop.create_task(self._scan_loop()))

    async def stop(self):
        self._stop = True
        for conn in list(self._connections.values()):
            conn.queue.put_nowait(None)
            try:
                conn.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                conn.sock.close()
            except Exception:
                pass
        for task in self._tasks:
            task.cancel()
        try:
            if self._server_sock is not None:
                await asyncio.to_thread(_bt_stop_advertise, self._server_sock)
                self._server_sock.close()
        except Exception:
            pass

    def send_frame(self, peer_id, data):
        conn = self._connections.get(peer_id)
        if not conn:
            return False
        conn.queue.put_nowait(protocol.encode_frame(data))
        return True

    async def connect_host(self, addr, port):
        if not self.available:
            return False
        try:
            sock = await asyncio.to_thread(_bt_connect, addr, port)
        except Exception:
            return False
        loop = asyncio.get_running_loop()
        loop.create_task(self._handshake(sock, mac=addr))
        return True

    async def scan(self, duration=4):
        if not self.available:
            return []
        try:
            found = await asyncio.to_thread(_bt_discover, self.uuid, duration)
        except Exception:
            return []
        verified = []
        for mac, port, name in found:
            if self._stop:
                break
            result = await asyncio.to_thread(_bt_probe_identity, self.identity, mac, port)
            if not result:
                continue
            pid, sock, info = result
            if pid in self._connections:
                self._close_sock(sock)
            else:
                self._register(pid, sock, info)
            verified.append((mac, port, name, pid))
        return verified

    async def _accept_loop(self):
        if self._server_sock is None:
            return
        while not self._stop:
            try:
                sock, addr = await asyncio.to_thread(_bt_accept, self._server_sock)
            except Exception:
                await asyncio.sleep(0.2)
                continue
            loop = asyncio.get_running_loop()
            loop.create_task(self._handshake(sock, mac=addr[0]))

    async def _scan_loop(self):
        while not self._stop:
            try:
                found = await asyncio.to_thread(_bt_discover, self.uuid)
            except Exception:
                found = ()
            now = time.time()
            for mac, chan, _name in found:
                if now - self._cooldown.get(mac, 0) < SCAN_INTERVAL:
                    continue
                self._cooldown[mac] = now
                loop = asyncio.get_running_loop()
                loop.create_task(self._autoconnect(mac, chan))
            await asyncio.sleep(SCAN_INTERVAL)

    async def _autoconnect(self, mac, chan):
        try:
            sock = await asyncio.to_thread(_bt_connect, mac, chan)
        except Exception:
            return
        loop = asyncio.get_running_loop()
        loop.create_task(self._handshake(sock, mac=mac))

    async def _handshake(self, sock, mac=None):
        hello = protocol.make_clear_envelope(
            protocol.TYPE_HELLO,
            self.identity.peer_id,
            "",
            {"nick": self.identity.nick, "x_pub": _b64(self.identity.x_pub_bytes()), "ed_pub": _b64(self.identity.ed_pub_bytes())},
        )
        try:
            await asyncio.to_thread(sock.sendall, protocol.encode_frame(protocol.pack_envelope(hello)))
            frame = await asyncio.to_thread(_bt_read_frame, sock, HANDSHAKE_TIMEOUT)
        except (socket.timeout, ConnectionError, OSError):
            self._close_sock(sock)
            return
        except Exception:
            self._close_sock(sock)
            return
        if frame is None:
            self._close_sock(sock)
            return
        try:
            env = protocol.unpack_envelope(frame)
        except ValueError:
            self._close_sock(sock)
            return
        if env.get("type") != protocol.TYPE_HELLO:
            self._close_sock(sock)
            return
        peer_id = env["src"]
        body = env.get("body", {})
        info = {
            "nick": body.get("nick", peer_id),
            "x_pub": body.get("x_pub", ""),
            "ed_pub": body.get("ed_pub", ""),
        }
        self._register(peer_id, sock, info)

    def _register(self, peer_id, sock, info):
        if peer_id == self.identity.peer_id or peer_id in self._connections:
            self._close_sock(sock)
            return
        conn = _BtConn(peer_id, sock)
        self._connections[peer_id] = conn
        self.router.add_neighbor(peer_id, 1)
        if self.on_peer_change:
            self.on_peer_change(peer_id, True, info)
        loop = asyncio.get_running_loop()
        loop.create_task(self._read_loop(peer_id, sock))
        loop.create_task(self._write_loop(peer_id, conn))

    async def _read_loop(self, peer_id, sock):
        try:
            while not self._stop:
                try:
                    frame = await asyncio.to_thread(_bt_read_frame, sock, IDLE_TIMEOUT)
                except socket.timeout:
                    continue
                if frame is None:
                    break
                try:
                    env = protocol.unpack_envelope(frame)
                except ValueError:
                    continue
                try:
                    self.router.forward(env, via=peer_id)
                except ReplayError:
                    continue
        finally:
            self._drop(peer_id, sock)

    async def _write_loop(self, peer_id, conn):
        try:
            while not self._stop:
                data = await conn.queue.get()
                if data is None:
                    break
                await asyncio.to_thread(conn.sock.sendall, data)
        finally:
            self._drop(peer_id, conn.sock)

    def _drop(self, peer_id, sock):
        conn = self._connections.get(peer_id)
        if conn is not None and conn.sock is sock:
            self._connections.pop(peer_id, None)
            self.router.remove_neighbor(peer_id)
            if self.on_peer_change:
                self.on_peer_change(peer_id, False, None)
        self._close_sock(sock)

    @staticmethod
    def _close_sock(sock):
        try:
            sock.close()
        except Exception:
            pass


class _BtConn:
    def __init__(self, peer_id, sock):
        self.peer_id = peer_id
        self.sock = sock
        self.queue = asyncio.Queue()


def _b64(data):
    return base64.b64encode(data).decode()


def _bt_listen(channel):
    for ch in range(channel, channel + 8):
        sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
        sock.settimeout(1.0)
        try:
            sock.bind(("", ch))
            sock.listen(1)
            return sock, ch
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
    raise RuntimeError(f"无法绑定蓝牙 RFCOMM 频道 {channel}~{channel + 7}")


def _bt_advertise(sock, name, uuid):
    if hasattr(bluetooth, "advertise_service"):
        bluetooth.advertise_service(sock, name, service_id=uuid, service_classes=[uuid])


def _bt_stop_advertise(sock):
    if hasattr(bluetooth, "stop_advertising"):
        bluetooth.stop_advertising(sock)


def _bt_accept(sock):
    sock.settimeout(1.0)
    client, addr = sock.accept()
    client.settimeout(None)
    sock.settimeout(None)
    return client, addr


def _bt_connect(addr, port):
    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    sock.settimeout(10.0)
    sock.connect((addr, port))
    sock.settimeout(5.0)
    return sock


def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _bt_read_frame(sock, timeout=IDLE_TIMEOUT):
    sock.settimeout(timeout)
    head = _recv_exact(sock, 4)
    if head is None:
        return None
    (length,) = struct.unpack(">I", head)
    body = _recv_exact(sock, length)
    if body is None:
        return None
    return body


def _bt_discover(uuid, duration=4):
    found = bluetooth.discover_devices(duration=duration, lookup_names=True, flush_cache=True)
    candidates = []
    seen = set()
    for mac, name in found:
        addr = mac.lower()
        ports = set()
        for svc_uuid in (uuid, SPP_UUID):
            try:
                services = bluetooth.find_service(uuid=svc_uuid, address=mac)
            except Exception:
                continue
            for svc in services:
                port = svc.get("port")
                if not port:
                    continue
                svc_name = (svc.get("name") or "").strip()
                if svc_name and "howchat" not in svc_name.lower():
                    continue
                ports.add(port)
        for port in sorted(ports):
            key = (addr, port)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((addr, port, name or mac))
    return candidates


def _bt_probe_identity(identity, mac, port, timeout=HANDSHAKE_TIMEOUT):
    try:
        sock = _bt_connect(mac, port)
    except Exception:
        return None
    try:
        hello = protocol.make_clear_envelope(
            protocol.TYPE_HELLO,
            identity.peer_id,
            "",
            {"nick": identity.nick, "x_pub": _b64(identity.x_pub_bytes()), "ed_pub": _b64(identity.ed_pub_bytes())},
        )
        sock.sendall(protocol.encode_frame(protocol.pack_envelope(hello)))
        frame = _bt_read_frame(sock, timeout)
        if frame is None:
            return None
        env = protocol.unpack_envelope(frame)
        if env.get("type") != protocol.TYPE_HELLO:
            return None
        pid = env.get("src")
        if not pid or pid == identity.peer_id:
            return None
        body = env.get("body", {})
        info = {
            "nick": body.get("nick", pid),
            "x_pub": body.get("x_pub", ""),
            "ed_pub": body.get("ed_pub", ""),
        }
        sock.settimeout(None)
        return pid, sock, info
    except Exception:
        try:
            sock.close()
        except Exception:
            pass
        return None
