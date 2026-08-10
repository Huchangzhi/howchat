import asyncio

from howchat import protocol
from howchat.routing import Router
from howchat.transport.lan import LANTransport


class FakeIdentity:
    def __init__(self, peer_id, nick):
        self.peer_id = peer_id
        self.nick = nick

    def x_pub_bytes(self):
        return b"x" * 32

    def ed_pub_bytes(self):
        return b"e" * 32


async def run_pair():
    a_id = FakeIdentity("aaaa", "甲")
    b_id = FakeIdentity("bbbb", "乙")
    ra, rb = Router("aaaa"), Router("bbbb")

    received_b = []

    def deliver_b(env):
        received_b.append(env)

    rb.on_deliver = deliver_b

    ta = LANTransport(a_id, ra, host="127.0.0.1", tcp_port=40001)
    tb = LANTransport(b_id, rb, host="127.0.0.1", tcp_port=40002)

    def on_peer_a(peer_id, connected, nick):
        pass

    ta.on_peer_change = on_peer_a
    await ta.start(broadcast=False)
    await tb.start(broadcast=False)

    await ta.connect_host("127.0.0.1", 40002)
    for _ in range(100):
        if "bbbb" in ta.neighbors():
            break
        await asyncio.sleep(0.05)

    assert "bbbb" in ta.neighbors(), "未建立连接"
    assert "aaaa" in tb.neighbors(), "对端未注册"

    env = protocol.make_clear_envelope(
        protocol.TYPE_MSG, "aaaa", "bbbb", {"text": "你好，乙"}, route=[], ttl=5, seq=1
    )
    ok = ra.send(env)
    assert ok
    for _ in range(100):
        if received_b:
            break
        await asyncio.sleep(0.05)
    assert received_b and received_b[0]["body"]["text"] == "你好，乙"

    await ta.stop()
    await tb.stop()


def test_lan_pair_exchange():
    asyncio.run(run_pair())
