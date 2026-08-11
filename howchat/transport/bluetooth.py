import asyncio
import base64
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
SERVICE_UUID = "00001101-0000-1000-8000-00805f9b34fb"
SCAN_INTERVAL = 8.0


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
        self._sock = None
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
            self._server_sock = _bt_listen(self.channel)
            _bt_advertise(self._server_sock, SERVICE_NAME, self.uuid)
        except Exception:
            self._server_sock = None
        loop = asyncio.get_running_loop()
        self._tasks.append(loop.create_task(self._accept_loop()))
        if self.enable_scan:
            self._tasks.append(loop.create_task(self._scan_loop()))

    async def stop(self):
        self._stop = True
        for task in self._tasks:
            task.cancel()
        try:
            if self._server_sock is not None:
                _bt_stop_advertise(self._server_sock)
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
            sock = _bt_connect(addr, port)
        except Exception:
            return False
        loop = asyncio.get_running_loop()
        loop.create_task(self._handshake(sock, mac=addr))
        return True

    async def scan(self, duration=4):
        if not self.available:
            return []
        try:
            return await asyncio.to_thread(_bt_discover, self.uuid, self.channel, duration)
        except Exception:
            return []

    async def _accept_loop(self):
        if self._server_sock is None:
            return
        while not self._stop:
            try:
                sock, addr = _bt_accept(self._server_sock)
            except Exception:
                await asyncio.sleep(0.2)
                continue
            loop = asyncio.get_running_loop()
            loop.create_task(self._handshake(sock, mac=addr[0]))

    async def _scan_loop(self):
        while not self._stop:
            try:
                found = await asyncio.to_thread(_bt_discover, self.uuid, self.channel)
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
            frame = await asyncio.to_thread(_bt_read_frame, sock)
        except Exception:
            try:
                sock.close()
            except Exception:
                pass
            return
        if frame is None:
            try:
                sock.close()
            except Exception:
                pass
            return
        try:
            env = protocol.unpack_envelope(frame)
        except ValueError:
            try:
                sock.close()
            except Exception:
                pass
            return
        if env.get("type") != protocol.TYPE_HELLO:
            try:
                sock.close()
            except Exception:
                pass
            return
        peer_id = env["src"]
        if peer_id == self.identity.peer_id:
            try:
                sock.close()
            except Exception:
                pass
            return
        if peer_id in self._connections:
            try:
                sock.close()
            except Exception:
                pass
            return
        body = env.get("body", {})
        info = {
            "nick": body.get("nick", peer_id),
            "x_pub": body.get("x_pub", ""),
            "ed_pub": body.get("ed_pub", ""),
        }
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
                frame = await asyncio.to_thread(_bt_read_frame, sock)
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
    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    sock.settimeout(1.0)
    sock.bind(("", channel))
    sock.listen(1)
    return sock


def _bt_advertise(sock, name, uuid):
    if hasattr(bluetooth, "advertise_service"):
        bluetooth.advertise_service(sock, name, service_id=uuid, service_classes=[uuid])


def _bt_stop_advertise(sock):
    if hasattr(bluetooth, "stop_advertising"):
        bluetooth.stop_advertising(sock)


def _bt_accept(sock):
    sock.settimeout(1.0)
    client, addr = sock.accept()
    sock.settimeout(None)
    return client, addr


def _bt_connect(addr, port):
    sock = bluetooth.BluetoothSocket(bluetooth.RFCOMM)
    sock.settimeout(10.0)
    sock.connect((addr, port))
    sock.settimeout(5.0)
    return sock


def _bt_read_frame(sock):
    try:
        sock.settimeout(5.0)
        head = sock.recv(4)
        if not head or len(head) < 4:
            return None
        import struct

        (length,) = struct.unpack(">I", head)
        body = b""
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                return None
            body += chunk
        return body
    except Exception:
        return None


def _bt_discover(uuid, channel, duration=4):
    addresses = bluetooth.discover_devices(duration=duration, lookup_names=False, flush_cache=True)
    result = []
    for mac in addresses:
        addr = mac.lower()
        try:
            services = bluetooth.find_service(uuid=uuid, address=mac)
        except Exception:
            continue
        ports = [svc.get("port") for svc in services if svc.get("port")]
        for p in ports:
            result.append((addr, p, mac))
    return result