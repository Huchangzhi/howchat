import asyncio
import os
import tempfile

from howchat import identity as identity_mod
from howchat.core import Core
from howchat.routing import Router
from howchat.store import Store
from howchat.transport.lan import LANTransport


async def run_core_test():
    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))

    ra = Router(ida.peer_id)
    rb = Router(idb.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40201)
    tb = LANTransport(idb, rb, host="127.0.0.1", tcp_port=40202)
    ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
    cb = Core(idb, Store(os.path.join(d2, "store")), rb, tb)

    got = {}

    def msg_a(conv, entry):
        got.setdefault("a", []).append((conv, entry))

    def msg_b(conv, entry):
        got.setdefault("b", []).append((conv, entry))

    ca.on_message = msg_a
    cb.on_message = msg_b

    await ca.start()
    await cb.start()
    await ca.connect_host("127.0.0.1", 40202)
    for _ in range(100):
        if idb.peer_id in ca.transport.neighbors():
            break
        await asyncio.sleep(0.05)
    assert idb.peer_id in ca.transport.neighbors(), "未建立连接"

    err = ca.send_text(idb.peer_id, "你好，这是加密消息")
    assert err is None, err
    for _ in range(100):
        if got.get("b"):
            break
        await asyncio.sleep(0.05)
    assert got["b"], "B 未收到消息"
    conv, entry = got["b"][0]
    assert conv == ida.peer_id
    assert entry["text"] == "你好，这是加密消息"

    src = tempfile.mktemp()
    with open(src, "wb") as f:
        f.write(os.urandom(200 * 1024))
    err = ca.send_file(idb.peer_id, src)
    assert err is None, err
    for _ in range(300):
        if "已接收文件" in " ".join(e["text"] for _, e in got.get("b", [])):
            break
        await asyncio.sleep(0.05)
    texts = " ".join(e["text"] for _, e in got.get("b", []))
    assert "已接收文件" in texts, texts
    saved = list(cb.store.files_path().glob("*"))
    assert saved, "文件未落盘"
    assert saved[0].read_bytes() == open(src, "rb").read()

    await ca.stop()
    await cb.stop()


def test_core_text_and_file():
    asyncio.run(run_core_test())


async def run_relay_test():
    d1, d2, dr = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))
    idr = identity_mod.load_or_create(os.path.join(dr, "data"))

    ra = Router(ida.peer_id)
    rb = Router(idb.peer_id)
    rr = Router(idr.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40501)
    tr = LANTransport(idr, rr, host="127.0.0.1", tcp_port=40502)
    tb = LANTransport(idb, rb, host="127.0.0.1", tcp_port=40503)
    ca = Core(ida, Store(os.path.join(d1, "store")), ra, ta)
    cr = Core(idr, Store(os.path.join(dr, "store")), rr, tr)
    cb = Core(idb, Store(os.path.join(d2, "store")), rb, tb)

    got_b = []

    def msg_b(conv, entry):
        got_b.append((conv, entry))

    cb.on_message = msg_b
    await ca.start(broadcast=False)
    await cr.start(broadcast=False)
    await cb.start(broadcast=False)

    await ca.connect_host("127.0.0.1", 40502)
    await cb.connect_host("127.0.0.1", 40502)
    for _ in range(200):
        if idr.peer_id in ca.transport.neighbors() and idr.peer_id in cb.transport.neighbors():
            break
        await asyncio.sleep(0.05)
    assert idr.peer_id in ca.transport.neighbors() and idr.peer_id in cb.transport.neighbors()

    for _ in range(200):
        if ca.store.get_contact(idb.peer_id):
            break
        await asyncio.sleep(0.05)
    assert ca.store.get_contact(idb.peer_id), "A 未能通过中继获取 B 的公钥"

    ca.discover(idb.peer_id)
    for _ in range(200):
        entry = ra.route_to(idb.peer_id)
        if entry and entry.next_hop == idr.peer_id:
            break
        await asyncio.sleep(0.05)
    entry = ra.route_to(idb.peer_id)
    assert entry is not None and entry.next_hop == idr.peer_id, "A 未发现经由中继到 B 的路由"

    err = ca.send_text(idb.peer_id, "通过中继的加密消息")
    assert err is None, err
    for _ in range(200):
        if got_b:
            break
        await asyncio.sleep(0.05)
    assert got_b, "B 未收到经中继的消息"
    assert got_b[0][1]["text"] == "通过中继的加密消息"

    await ca.stop()
    await cr.stop()
    await cb.stop()


def test_core_relay():
    asyncio.run(run_relay_test())


async def run_group_test():
    d1, d2, d3 = tempfile.mkdtemp(), tempfile.mkdtemp(), tempfile.mkdtemp()
    nodes = []
    for i, dd in enumerate([d1, d2, d3]):
        ident = identity_mod.load_or_create(os.path.join(dd, "data"))
        router = Router(ident.peer_id)
        transport = LANTransport(ident, router, host="127.0.0.1", tcp_port=40601 + i)
        core = Core(ident, Store(os.path.join(dd, "store")), router, transport)
        nodes.append((ident, core))

    got = {n[0].peer_id: [] for n in nodes}

    def make_handler(peer_id):
        def h(conv, entry):
            got[peer_id].append((conv, entry))

        return h

    for ident, core in nodes:
        core.on_message = make_handler(ident.peer_id)

    for ident, core in nodes:
        await core.start(broadcast=False)

    hub = nodes[0]
    for i in (1, 2):
        await nodes[i][1].connect_host("127.0.0.1", 40601)
    for _ in range(200):
        if all(n[1].transport.neighbors() for n in nodes[1:]):
            break
        await asyncio.sleep(0.05)
    for n in nodes[1:]:
        assert hub[1].transport.neighbors(), "节点未连接到中心节点"

    sender = nodes[0]
    others = [n[0].peer_id for n in nodes[1:]]
    sender[1].store.add_channel_member("#office", others)
    err = sender[1].send_group("#office", "大家好，群聊消息")
    assert err is None, err
    for _ in range(300):
        if all(got[n[0].peer_id] for n in nodes[1:]):
            break
        await asyncio.sleep(0.05)
    for ident, _ in nodes[1:]:
        assert got[ident.peer_id], f"{ident.nick} 未收到群聊消息"
        assert got[ident.peer_id][0][1]["text"] == "大家好，群聊消息"

    for ident, core in nodes:
        await core.stop()


def test_core_group():
    asyncio.run(run_group_test())
