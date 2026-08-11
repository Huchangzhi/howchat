import asyncio
import base64
import json
import socket
import struct

from howchat import protocol
from howchat.routing import ReplayError
from howchat.transport import Transport

DISCOVERY_PORT = 41234
TCP_PORT = 41235
BEACON_INTERVAL = 5.0
DISCOVERY_ADDR = "255.255.255.255"


class LANTransport(Transport):
    def __init__(self, identity, router, host="0.0.0.0", tcp_port=TCP_PORT, discovery_port=DISCOVERY_PORT):
        super().__init__(identity, router)
        self.host = host
        self.tcp_port = tcp_port
        self.discovery_port = discovery_port
        self._connections = {}
        self._known = {}
        self._server = None
        self._discovery = None
        self._tasks = []
        router.set_transport(self)

    def neighbors(self):
        return set(self._connections.keys())

    def known_peers(self):
        return dict(self._known)

    async def start(self, broadcast=True):
        self.tcp_port = await self._bind_tcp()
        self._discovery = self._open_discovery()
        loop = asyncio.get_running_loop()
        tasks = []
        if broadcast:
            tasks.extend(
                [
                    loop.create_task(self._beacon_loop()),
                    loop.create_task(self._discovery_loop()),
                ]
            )
        self._tasks = tasks

    async def _bind_tcp(self):
        for port in range(self.tcp_port, self.tcp_port + 50):
            try:
                self._server = await asyncio.start_server(self._accept, self.host, port)
                return port
            except OSError:
                continue
        raise OSError(f"无法监听端口 {self.tcp_port}~{self.tcp_port + 49}")

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        for conn in self._connections.values():
            conn.queue.put_nowait(None)
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if self._discovery:
            self._discovery.close()

    def send_frame(self, peer_id, data):
        conn = self._connections.get(peer_id)
        if not conn:
            return False
        conn.queue.put_nowait(protocol.encode_frame(data))
        return True

    async def connect_host(self, addr, port):
        try:
            reader, writer = await asyncio.open_connection(addr, port)
        except OSError:
            return False
        loop = asyncio.get_running_loop()
        loop.create_task(self._handshake(reader, writer))
        return True

    def _accept(self, reader, writer):
        loop = asyncio.get_running_loop()
        loop.create_task(self._handshake(reader, writer))

    def _open_discovery(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", self.discovery_port))
        sock.setblocking(False)
        return sock

    def _beacon_bytes(self):
        return json.dumps(
            {"id": self.identity.peer_id, "nick": self.identity.nick, "tcp_port": self.tcp_port}
        ).encode("utf-8")

    def _broadcast_targets(self):
        targets = [DISCOVERY_ADDR]
        sb = self._subnet_broadcast()
        if sb:
            targets.append(sb)
        return targets

    def _subnet_broadcast(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
        except OSError:
            return None
        if "." not in ip:
            return None
        return ".".join(ip.split(".")[:3]) + ".255"

    def _send_broadcast(self):
        beacon = self._beacon_bytes()
        for target in self._broadcast_targets():
            try:
                self._discovery.sendto(beacon, (target, self.discovery_port))
            except OSError:
                pass

    async def _beacon_loop(self):
        while True:
            for _ in range(3):
                self._send_broadcast()
                await asyncio.sleep(0.15)
            await asyncio.sleep(BEACON_INTERVAL)

    async def _discovery_loop(self):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data, addr = await loop.sock_recvfrom(self._discovery, 1024)
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError):
                continue
            try:
                info = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            peer_id = info.get("id")
            if not peer_id or peer_id == self.identity.peer_id:
                continue
            peer_port = info.get("tcp_port", self.tcp_port)
            self._known[peer_id] = (addr[0], peer_port)
            if peer_id not in self._connections and self.identity.peer_id < peer_id:
                loop.create_task(self.connect_host(addr[0], peer_port))

    def _peer_info(self):
        return {
            "nick": self.identity.nick,
            "tcp_port": self.tcp_port,
            "x_pub": base64.b64encode(self.identity.x_pub_bytes()).decode(),
            "ed_pub": base64.b64encode(self.identity.ed_pub_bytes()).decode(),
        }

    async def _handshake(self, reader, writer):
        hello = protocol.make_clear_envelope(
            protocol.TYPE_HELLO,
            self.identity.peer_id,
            "",
            self._peer_info(),
        )
        try:
            writer.write(protocol.encode_frame(protocol.pack_envelope(hello)))
            await writer.drain()
            frame = await _read_frame(reader)
        except (ConnectionError, OSError):
            writer.close()
            return
        if frame is None:
            writer.close()
            return
        try:
            env = protocol.unpack_envelope(frame)
        except ValueError:
            writer.close()
            return
        if env.get("type") != protocol.TYPE_HELLO:
            writer.close()
            return
        peer_id = env["src"]
        if peer_id == self.identity.peer_id:
            writer.close()
            return
        if peer_id in self._connections:
            writer.close()
            return
        body = env.get("body", {})
        peer_info = {
            "nick": body.get("nick", peer_id),
            "x_pub": body.get("x_pub", ""),
            "ed_pub": body.get("ed_pub", ""),
        }
        conn = _Conn(peer_id, reader, writer)
        self._connections[peer_id] = conn
        self.router.add_neighbor(peer_id, 1)
        if self.on_peer_change:
            self.on_peer_change(peer_id, True, peer_info)
        loop = asyncio.get_running_loop()
        loop.create_task(self._read_loop(peer_id, reader, writer))
        loop.create_task(self._write_loop(peer_id, conn))

    async def _read_loop(self, peer_id, reader, writer):
        try:
            while True:
                frame = await _read_frame(reader)
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
            self._drop(peer_id, writer)

    async def _write_loop(self, peer_id, conn):
        try:
            while True:
                data = await conn.queue.get()
                if data is None:
                    break
                conn.writer.write(data)
                await conn.writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._drop(peer_id, conn.writer)

    def _drop(self, peer_id, writer):
        conn = self._connections.get(peer_id)
        if conn is not None and conn.writer is writer:
            self._connections.pop(peer_id, None)
            self.router.remove_neighbor(peer_id)
            if self.on_peer_change:
                self.on_peer_change(peer_id, False, None)
        try:
            writer.close()
        except Exception:
            pass


class _Conn:
    def __init__(self, peer_id, reader, writer):
        self.peer_id = peer_id
        self.reader = reader
        self.writer = writer
        self.queue = asyncio.Queue()


async def _read_exact(reader, n):
    buf = b""
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


async def _read_frame(reader):
    head = await _read_exact(reader, 4)
    if head is None:
        return None
    (length,) = struct.unpack(">I", head)
    body = await _read_exact(reader, length)
    return body
