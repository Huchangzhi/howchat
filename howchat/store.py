import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path


STATUS_STRANGER = "stranger"
STATUS_PENDING = "pending"
STATUS_FRIEND = "friend"
STATUS_BLOCKED = "blocked"


@dataclass
class Contact:
    peer_id: str
    nick: str
    x_pub_b64: str = ""
    ed_pub_b64: str = ""
    fingerprint: str = ""
    last_seen: float = 0.0
    status: str = STATUS_STRANGER
    confirmed_fingerprint: str = ""

    def to_dict(self):
        return {
            "peer_id": self.peer_id,
            "nick": self.nick,
            "x_pub_b64": self.x_pub_b64,
            "ed_pub_b64": self.ed_pub_b64,
            "fingerprint": self.fingerprint,
            "last_seen": self.last_seen,
            "status": self.status,
            "confirmed_fingerprint": self.confirmed_fingerprint,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            peer_id=d["peer_id"],
            nick=d.get("nick", d["peer_id"]),
            x_pub_b64=d.get("x_pub_b64", ""),
            ed_pub_b64=d.get("ed_pub_b64", ""),
            fingerprint=d.get("fingerprint", ""),
            last_seen=d.get("last_seen", 0.0),
            status=d.get("status", STATUS_STRANGER),
            confirmed_fingerprint=d.get("confirmed_fingerprint", ""),
        )


class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.contacts_dir = self.data_dir / "contacts"
        self.history_dir = self.data_dir / "history"
        self.files_dir = self.data_dir / "files"
        for d in (self.contacts_dir, self.history_dir, self.files_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.channels_path = self.data_dir / "channels.json"
        self.queued_path = self.data_dir / "queued.json"
        self._contacts = {}
        self._channels = {}
        self._queued = []
        self._load()

    def _load(self):
        for path in self.contacts_dir.glob("*.json"):
            try:
                c = Contact.from_dict(json.loads(path.read_text(encoding="utf-8")))
                self._contacts[c.peer_id] = c
            except (ValueError, OSError, KeyError):
                continue
        if self.channels_path.exists():
            try:
                self._channels = json.loads(self.channels_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._channels = {}
        if self.queued_path.exists():
            try:
                self._queued = json.loads(self.queued_path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                self._queued = []

    def contacts(self):
        return dict(self._contacts)

    def get_contact(self, peer_id):
        return self._contacts.get(peer_id)

    def save_contact(self, contact):
        self._contacts[contact.peer_id] = contact
        path = self.contacts_dir / f"{contact.peer_id}.json"
        self._atomic_write(path, contact.to_dict())

    def update_contact_keys(self, peer_id, nick, x_pub_b64, ed_pub_b64, fingerprint="", status=None):
        c = self._contacts.get(peer_id) or Contact(peer_id, nick)
        if nick:
            c.nick = nick
        if x_pub_b64:
            c.x_pub_b64 = x_pub_b64
        if ed_pub_b64:
            c.ed_pub_b64 = ed_pub_b64
        if fingerprint:
            c.fingerprint = fingerprint
        if status is not None:
            c.status = status
        c.last_seen = time.time()
        self.save_contact(c)

    def set_friend_status(self, peer_id, status):
        c = self._contacts.get(peer_id)
        if c is None:
            c = Contact(peer_id, peer_id[:8])
            self._contacts[peer_id] = c
        c.status = status
        self.save_contact(c)

    def mark_verified(self, peer_id, fingerprint):
        c = self._contacts.get(peer_id)
        if c is None:
            return
        c.confirmed_fingerprint = fingerprint
        self.save_contact(c)

    def channels(self):
        return dict(self._channels)

    def channel_members(self, channel):
        return set(self._channels.get(channel, []))

    def add_channel_member(self, channel, peer_ids):
        members = set(self._channels.get(channel, []))
        members.update(peer_ids)
        self._channels[channel] = sorted(members)
        self._atomic_write(self.channels_path, self._channels)

    def remove_channel_member(self, channel, peer_ids):
        members = set(self._channels.get(channel, []))
        for p in peer_ids:
            members.discard(p)
        self._channels[channel] = sorted(members)
        self._atomic_write(self.channels_path, self._channels)

    def queued(self):
        return [self._rec_to_env(r) for r in self._queued]

    def queued_uids(self):
        return [r.get("uid") for r in self._queued]

    def queue_outbound(self, envelope, uid=None):
        self._queued.append({"env": envelope, "uid": uid})
        self._atomic_write(self.queued_path, self._queued)

    def clear_queued(self, envelopes):
        dropped = [e for e in envelopes]
        remaining = [r for r in self._queued if self._rec_to_env(r) not in dropped]
        self._queued = remaining
        self._atomic_write(self.queued_path, self._queued)

    @staticmethod
    def _rec_to_env(rec):
        if isinstance(rec, dict) and "env" in rec:
            return rec["env"]
        return rec

    def history(self, conv_id):
        path = self.history_dir / f"{conv_id}.json"
        if not path.exists():
            return []
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []

    def append_history(self, conv_id, entry):
        items = self.history(conv_id)
        items.append(entry)
        items = items[-2000:]
        self._atomic_write(self.history_dir / f"{conv_id}.json", items)

    def mark_history_sent(self, conv_id, uid):
        items = self.history(conv_id)
        changed = False
        for it in items:
            if it.get("uid") == uid and it.get("status") == "queued":
                it["status"] = "sent"
                changed = True
        if changed:
            self._atomic_write(self.history_dir / f"{conv_id}.json", items)

    def find_history_conv(self, uid):
        for path in self.history_dir.glob("*.json"):
            try:
                items = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            for it in items:
                if it.get("uid") == uid:
                    return path.stem
        return None

    def files_path(self):
        return self.files_dir

    @staticmethod
    def _atomic_write(path, data):
        tmp = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=os.path.dirname(path), delete=False
        )
        try:
            json.dump(data, tmp, ensure_ascii=False, indent=2)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp.close()
            os.replace(tmp.name, path)
        finally:
            if os.path.exists(tmp.name):
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
