import time
from dataclasses import dataclass

from howchat import protocol

DEFAULT_TTL = 10
DISCOVERY_TTL = 5


@dataclass
class RoutingEntry:
    next_hop: str
    hops: int
    last_seen: float


class Router:
    def __init__(self, peer_id):
        self.peer_id = peer_id
        self.transport = None
        self.table = {}
        self._seen = set()
        self.on_deliver = None
        self._seq = 0

    def set_transport(self, transport):
        self.transport = transport

    def next_seq(self):
        self._seq += 1
        return self._seq

    def add_neighbor(self, peer_id, hops=1):
        self.table[peer_id] = RoutingEntry(peer_id, hops, time.time())

    def remove_neighbor(self, peer_id):
        self.table.pop(peer_id, None)

    def route_to(self, dst):
        return self.table.get(dst)

    def neighbors(self):
        return self.transport.neighbors() if self.transport else set()

    def send(self, envelope):
        dst = envelope["dst"]
        if dst == self.peer_id:
            self._deliver(envelope)
            return True
        if envelope.get("route"):
            return self._forward(envelope)
        entry = self.table.get(dst)
        if entry is None:
            self.discover(dst)
            return False
        envelope["route"] = []
        return self._forward(envelope)

    def _forward(self, envelope):
        if envelope["ttl"] <= 0:
            return False
        if envelope.get("route"):
            next_hop = envelope["route"][0]
        else:
            entry = self.table.get(envelope["dst"])
            if entry is None:
                return False
            next_hop = entry.next_hop
        out = dict(envelope)
        out["ttl"] = envelope["ttl"] - 1
        if out.get("route"):
            out["route"] = envelope["route"][1:]
        self.transport.send_frame(next_hop, protocol.pack_envelope(out))
        return True

    def forward(self, envelope, via=None):
        self._replay_check(envelope)
        etype = envelope["type"]
        if etype == protocol.TYPE_ROUTE_REQUEST:
            self._handle_route_request(envelope, via)
            return
        if etype == protocol.TYPE_ROUTE_REPLY:
            if envelope["dst"] == self.peer_id:
                self._handle_route_reply(envelope, via)
            else:
                self._forward(envelope)
            return
        if envelope["dst"] == self.peer_id:
            self._deliver(envelope)
        else:
            self._forward(envelope)

    def _deliver(self, envelope):
        if self.on_deliver:
            self.on_deliver(envelope)

    def discover(self, dst):
        if not self.transport:
            return
        env = {
            "v": 1,
            "type": protocol.TYPE_ROUTE_REQUEST,
            "src": self.peer_id,
            "dst": dst,
            "route": [self.peer_id],
            "ttl": DISCOVERY_TTL,
            "seq": self.next_seq(),
            "ts": int(time.time()),
        }
        for n in self.neighbors():
            self.transport.send_frame(n, protocol.pack_envelope(env))

    def _handle_route_request(self, req, via):
        if req["ttl"] <= 0:
            return
        target = req["dst"]
        if target == self.peer_id:
            self._reply_route(via, req, target, 0)
            return
        entry = self.table.get(target)
        if entry is not None:
            self._reply_route(via, req, target, entry.hops)
            return
        visited = list(req["route"])
        if self.peer_id in visited:
            return
        out = dict(req)
        out["route"] = visited + [self.peer_id]
        out["ttl"] = req["ttl"] - 1
        for n in self.neighbors():
            if n == via or n in visited:
                continue
            self.transport.send_frame(n, protocol.pack_envelope(out))

    def _reply_route(self, via, req, target, hops):
        return_route = list(reversed(req["route"]))[1:]
        env = {
            "v": 1,
            "type": protocol.TYPE_ROUTE_REPLY,
            "src": self.peer_id,
            "dst": req["src"],
            "target": target,
            "hops": hops,
            "route": return_route,
            "ttl": DISCOVERY_TTL,
            "seq": self.next_seq(),
            "ts": int(time.time()),
        }
        self.transport.send_frame(via, protocol.pack_envelope(env))

    def _handle_route_reply(self, reply, via):
        target = reply["target"]
        hops = reply["hops"] + 1
        current = self.table.get(target)
        if current is None or hops < current.hops:
            self.table[target] = RoutingEntry(via, hops, time.time())

    def _replay_check(self, envelope):
        key = (envelope["src"], envelope["seq"])
        if key in self._seen:
            raise ReplayError("重复的消息")
        self._seen.add(key)
        if len(self._seen) > 100000:
            self._seen = set(list(self._seen)[-50000:])


class ReplayError(Exception):
    pass
