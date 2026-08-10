import time

from howchat import protocol
from howchat.routing import Router, ReplayError


class FakeTransport:
    def __init__(self, peer_id):
        self.peer_id = peer_id
        self.sent = {}
        self._neighbors = set()

    def set_neighbors(self, *peers):
        self._neighbors = set(peers)

    def neighbors(self):
        return self._neighbors

    def send_frame(self, peer_id, frame):
        env = protocol.unpack_envelope(frame)
        self.sent.setdefault(peer_id, []).append(env)


def deliver_into(router):
    received = []

    def _deliver(env):
        received.append(env)

    router.on_deliver = _deliver
    return received


def make_msg(src, dst, route=None, seq=1):
    return {
        "v": 1, "type": protocol.TYPE_MSG, "src": src, "dst": dst,
        "route": route or [], "ttl": 10, "seq": seq, "ts": int(time.time()),
    }


def test_direct_delivery():
    r = Router("a")
    received = deliver_into(r)
    env = make_msg("b", "a")
    r.forward(env, via="b")
    assert received == [env]


def test_forward_via_explicit_route():
    a, b, c = Router("a"), Router("b"), Router("c")
    fa, fb, fc = FakeTransport("a"), FakeTransport("b"), FakeTransport("c")
    a.set_transport(fa)
    b.set_transport(fb)
    c.set_transport(fc)
    fa.set_neighbors("b")
    fb.set_neighbors("a", "c")
    fc.set_neighbors("b")
    b.add_neighbor("c")
    received_c = deliver_into(c)

    a.send(make_msg("a", "c", route=["b"]))
    env_at_b = fa.sent["b"][0]
    assert env_at_b["route"] == []
    b.forward(env_at_b, via="a")
    assert len(fb.sent.get("c", [])) == 1
    c.forward(fb.sent["c"][0], via="b")
    assert len(received_c) == 1
    assert received_c[0]["dst"] == "c"
    assert received_c[0]["type"] == protocol.TYPE_MSG


def test_forward_using_table():
    a, b, c = Router("a"), Router("b"), Router("c")
    fa, fb = FakeTransport("a"), FakeTransport("b")
    a.set_transport(fa)
    b.set_transport(fb)
    fb.set_neighbors("a", "c")
    b.add_neighbor("c")
    received_c = deliver_into(c)

    env = make_msg("a", "c")
    b.forward(env, via="a")
    assert fb.sent["c"][0]["dst"] == "c"
    c.forward(fb.sent["c"][0], via="b")
    assert len(received_c) == 1
    assert received_c[0]["dst"] == "c"


def test_discovery_chain():
    a, b, c = Router("a"), Router("b"), Router("c")
    fa, fb, fc = FakeTransport("a"), FakeTransport("b"), FakeTransport("c")
    a.set_transport(fa)
    b.set_transport(fb)
    c.set_transport(fc)
    fa.set_neighbors("b")
    fb.set_neighbors("a", "c")
    fc.set_neighbors("b")

    a.discover("c")
    req_to_b = fa.sent["b"][0]
    assert req_to_b["type"] == protocol.TYPE_ROUTE_REQUEST

    b.forward(req_to_b, via="a")
    req_to_c = fb.sent["c"][0]
    assert req_to_c["dst"] == "c"

    c.forward(req_to_c, via="b")
    reply_to_b = fc.sent["b"][0]
    assert reply_to_b["type"] == protocol.TYPE_ROUTE_REPLY
    assert reply_to_b["target"] == "c"

    b.forward(reply_to_b, via="c")
    reply_to_a = fb.sent["a"][0]
    assert reply_to_a["dst"] == "a"

    a.forward(reply_to_a, via="b")
    entry = a.route_to("c")
    assert entry is not None
    assert entry.next_hop == "b"
    assert entry.hops == 1


def test_replay_detection():
    r = Router("a")
    env = make_msg("b", "a", seq=42)
    r.forward(env, via="b")
    try:
        r.forward(env, via="b")
        assert False, "应检测到重放"
    except ReplayError:
        pass
