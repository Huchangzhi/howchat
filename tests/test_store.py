import os
import tempfile

from howchat.store import Store


def test_store_contact_roundtrip(tmp_path):
    s = Store(tmp_path)
    s.update_contact_keys("peer1", "小明", "aGVsbG8=", "d29ybGQ=", "AA BB CC DD")
    s2 = Store(tmp_path)
    c = s2.get_contact("peer1")
    assert c is not None
    assert c.nick == "小明"
    assert c.x_pub_b64 == "aGVsbG8="
    assert c.fingerprint == "AA BB CC DD"


def test_store_offline_queue(tmp_path):
    s = Store(tmp_path)
    env = {"type": "msg", "src": "a", "dst": "b", "seq": 1}
    s.queue_outbound(env)
    s2 = Store(tmp_path)
    assert s2.queued() == [env]
    s2.clear_queued([env])
    assert Store(tmp_path).queued() == []


def test_store_channels(tmp_path):
    s = Store(tmp_path)
    s.add_channel_member("#work", ["p1", "p2"])
    s2 = Store(tmp_path)
    assert s2.channel_members("#work") == {"p1", "p2"}
    s2.remove_channel_member("#work", ["p1"])
    assert Store(tmp_path).channel_members("#work") == {"p2"}


def test_store_history(tmp_path):
    s = Store(tmp_path)
    s.append_history("peer1", {"role": "me", "text": "你好"})
    s.append_history("peer1", {"role": "them", "text": "你好呀"})
    assert len(Store(tmp_path).history("peer1")) == 2


def test_atomic_write_non_ascii(tmp_path):
    s = Store(tmp_path)
    s.update_contact_keys("p", "测试昵称", "", "")
    raw = (tmp_path / "contacts" / "p.json").read_text(encoding="utf-8")
    assert "测试昵称" in raw
