import asyncio
import os
import tempfile

from pathlib import Path

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
    for _ in range(100):
        if ca.is_friend(idb.peer_id) and cb.is_friend(ida.peer_id):
            break
        await asyncio.sleep(0.05)
    assert ca.is_friend(idb.peer_id), "直连后应自动成为好友"

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
        if ca.is_friend(idb.peer_id) and cb.is_friend(ida.peer_id):
            break
        await asyncio.sleep(0.05)
    assert ca.is_friend(idb.peer_id) and cb.is_friend(ida.peer_id), "发消息应自动完成好友流程"
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
    for _ in range(200):
        if all(hub[1].is_friend(n[0].peer_id) for n in nodes[1:]):
            break
        await asyncio.sleep(0.05)
    assert all(hub[1].is_friend(n[0].peer_id) for n in nodes[1:]), "直连后未自动成为好友"

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

    # 群聊发送方历史应只有一条记录（不能每个成员各写一条）
    hist = sender[1].store.history("#office")
    me_texts = [e.get("text") for e in hist if e.get("role") == "me"]
    assert me_texts.count("大家好，群聊消息") == 1, f"群聊历史出现重复：{me_texts}"

    # 成员名单应随消息传播：所有节点（含未直连者）都应知道完整成员
    ids = {n[0].peer_id for n in nodes}
    for ident, core in nodes:
        members = core.store.channel_members("#office")
        assert ids <= members, f"{ident.peer_id} 的群成员缺失：{members}"

    for ident, core in nodes:
        await core.stop()


def test_core_group():
    asyncio.run(run_group_test())


async def run_queued_sent_test():
    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))

    class ToggleRouter(Router):
        def __init__(self, peer_id):
            super().__init__(peer_id)
            self.online = False

        def send(self, env):
            return self.online

    ra = ToggleRouter(ida.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40701)
    store_a = Store(os.path.join(d1, "store"))
    ca = Core(ida, store_a, ra, ta)

    updated = []

    def on_updated(conv):
        updated.append(conv)

    ca.on_message_updated = on_updated
    await ca.start(broadcast=False)

    import base64

    store_a.update_contact_keys(
        idb.peer_id,
        "乙",
        base64.b64encode(ida.x_pub_bytes()).decode(),
        base64.b64encode(ida.ed_pub_bytes()).decode(),
    )
    store_a.set_friend_status(idb.peer_id, "friend")

    ra.online = False
    err = ca.send_text(idb.peer_id, "离线排队消息")
    assert err is None, err
    assert store_a.queued(), "消息应进入离线队列"
    entry = store_a.history(idb.peer_id)[-1]
    assert entry["status"] == "queued", "历史条目应为排队状态"
    assert not updated, "离线时不应触发更新回调"

    ra.online = True
    ca.flush_queued()
    assert not store_a.queued(), "连接后应清空排队队列"
    entry = store_a.history(idb.peer_id)[-1]
    assert entry["status"] == "sent", "连接后排队状态应更新为已发送"
    assert updated == [idb.peer_id], "应通知 TUI 刷新会话"

    await ca.stop()


def test_core_queued_history_status():
    asyncio.run(run_queued_sent_test())


async def run_friend_verify_test():
    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))

    ra = Router(ida.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40801)
    store_a = Store(os.path.join(d1, "store"))
    ca = Core(ida, store_a, ra, ta)
    status_a = []

    ca.on_status = status_a.append
    await ca.start(broadcast=False)

    import base64

    x_pub = base64.b64encode(ida.x_pub_bytes()).decode()
    ed_pub = base64.b64encode(ida.ed_pub_bytes()).decode()
    store_a.update_contact_keys(idb.peer_id, "乙", x_pub, ed_pub, ca._fingerprint(x_pub))

    err = ca.request_friend(idb.peer_id)
    assert err is None, err
    assert store_a.get_contact(idb.peer_id).status == "pending"

    err = ca.accept_friend(idb.peer_id)
    assert err is None, err
    assert ca.is_friend(idb.peer_id)

    msg = ca.verify_friend(idb.peer_id)
    assert "已确认" in msg, msg
    assert store_a.get_contact(idb.peer_id).confirmed_fingerprint

    ca._on_peer_change(
        idb.peer_id,
        True,
        {
            "nick": "乙",
            "x_pub": base64.b64encode(ida.x_pub_bytes()).decode(),
            "ed_pub": base64.b64encode(ida.ed_pub_bytes()).decode(),
        },
    )
    assert not any("安全警告" in s for s in status_a), "相同密钥不应告警"

    store_a.mark_verified(idb.peer_id, "AA BB CC DD")
    status_a.clear()
    ca._on_peer_change(
        idb.peer_id,
        True,
        {
            "nick": "乙",
            "x_pub": base64.b64encode(ida.x_pub_bytes()).decode(),
            "ed_pub": base64.b64encode(ida.ed_pub_bytes()).decode(),
        },
    )
    assert any("安全警告" in s for s in status_a), "密钥变化应触发中间人警告"

    await ca.stop()


def test_core_friend_verify_and_keychange():
    asyncio.run(run_friend_verify_test())


async def run_pending_auto_flush_test():
    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))

    class CountingRouter(Router):
        def __init__(self, peer_id):
            super().__init__(peer_id)
            self.online = False
            self.sent = 0

        def send(self, env):
            if self.online:
                self.sent += 1
                return True
            return False

    ra = CountingRouter(ida.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40901)
    store_a = Store(os.path.join(d1, "store"))
    ca = Core(ida, store_a, ra, ta)
    await ca.start(broadcast=False)

    import base64

    x_pub = base64.b64encode(ida.x_pub_bytes()).decode()
    ed_pub = base64.b64encode(ida.ed_pub_bytes()).decode()
    # B 以陌生联系人身份出现（经中继介绍），有关键但未连接
    store_a.update_contact_keys(idb.peer_id, "乙", x_pub, ed_pub, ca._fingerprint(x_pub))

    ra.online = False
    err = ca.send_text(idb.peer_id, "离线待达消息")
    assert err is None, err
    assert idb.peer_id in ca._pending_auto, "陌生人的消息应进入待达队列"
    assert store_a.get_contact(idb.peer_id).status == "pending"

    ra.online = True
    # 对方直连上线：应立即刷新好友请求 + 待达消息
    ca._on_peer_change(idb.peer_id, True, {"nick": "乙", "x_pub": x_pub, "ed_pub": ed_pub})
    assert ca.is_friend(idb.peer_id), "直连后应成为好友"
    assert ra.sent >= 2, f"应发送好友请求+待达消息，实际发送 {ra.sent}"
    assert not ca._pending_auto, "待达消息应在成为好友后清空"

    await ca.stop()


def test_core_pending_auto_flushed_on_direct_connect():
    asyncio.run(run_pending_auto_flush_test())


async def run_malicious_channel_test():
    import base64

    d1 = tempfile.mkdtemp()
    d2 = tempfile.mkdtemp()
    ida = identity_mod.load_or_create(os.path.join(d1, "data"))
    idb = identity_mod.load_or_create(os.path.join(d2, "data"))
    ra, rb = Router(ida.peer_id), Router(idb.peer_id)
    ta = LANTransport(ida, ra, host="127.0.0.1", tcp_port=40951)
    tb = LANTransport(idb, rb, host="127.0.0.1", tcp_port=40952)
    store_a = Store(os.path.join(d1, "store"))
    store_b = Store(os.path.join(d2, "store"))
    ca = Core(ida, store_a, ra, ta)
    cb = Core(idb, store_b, rb, tb)
    await ca.start(broadcast=False)
    await cb.start(broadcast=False)

    x_pub = base64.b64encode(ida.x_pub_bytes()).decode()
    ed_pub = base64.b64encode(ida.ed_pub_bytes()).decode()
    store_b.update_contact_keys(ida.peer_id, "甲", x_pub, ed_pub)
    store_b.set_friend_status(ida.peer_id, "friend")

    # 恶意频道名（路径穿越）不得写入任意路径，应退化为私聊
    evil = "../../evil"
    (Path(d2).parent / "evil.json").unlink(missing_ok=True)
    cb._handle_body(ida.peer_id, {"type": "text", "text": "x", "group": evil})
    assert evil not in store_b.channels(), "恶意频道名不应写入频道表"
    assert not (Path(d2).parent / "evil.json").exists(), "路径穿越历史文件不应被创建"
    assert not (Path(d2) / "evil.json").exists()
    hist = store_b.history(ida.peer_id)
    assert hist and hist[-1]["text"] == "x", "不合法频道名的消息应落到私聊历史"

    # 非法频道名 /join 应被拒绝
    err = ca.send_group(evil, "hi")
    assert err == "频道名不合法", err

    await ca.stop()
    await cb.stop()


def test_core_malicious_channel_name_sanitized():
    asyncio.run(run_malicious_channel_test())


def test_safe_channel_validation():
    from howchat.store import is_safe_channel

    assert is_safe_channel("#abc")
    assert is_safe_channel("#办公室")
    assert is_safe_channel("#a-b_c1")
    assert not is_safe_channel("abc")
    assert not is_safe_channel("")
    assert not is_safe_channel("#a/b")
    assert not is_safe_channel("../etc")
    assert not is_safe_channel("#..")
    assert not is_safe_channel("#" + "x" * 65)
