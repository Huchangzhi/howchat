import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

IDENTITY_FILE = "identity.json"


class Identity:
    def __init__(self, peer_id, nick, x_private, ed_private):
        self.peer_id = peer_id
        self.nick = nick
        self.x_private = x_private
        self.ed_private = ed_private

    @property
    def x_public(self):
        return self.x_private.public_key()

    @property
    def ed_public(self):
        return self.ed_private.public_key()

    @property
    def fingerprint(self):
        pub = self.x_public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        h = hashlib.sha256(pub).digest()[:8]
        return " ".join(f"{b:02X}" for b in h)

    def x_pub_bytes(self):
        return self.x_public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def ed_pub_bytes(self):
        return self.ed_public.public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )


def _random_peer_id():
    return hashlib.sha256(os.urandom(32)).hexdigest()[:16]


def load_or_create(data_dir):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / IDENTITY_FILE
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        x_private = x25519.X25519PrivateKey.from_private_bytes(bytes.fromhex(raw["x"]))
        ed_private = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw["ed"]))
        return Identity(raw["peer_id"], raw["nick"], x_private, ed_private)
    peer_id = _random_peer_id()
    nick = "用户" + peer_id[:6]
    x_private = x25519.X25519PrivateKey.generate()
    ed_private = ed25519.Ed25519PrivateKey.generate()
    raw = {
        "peer_id": peer_id,
        "nick": nick,
        "x": x_private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ).hex(),
        "ed": ed_private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        ).hex(),
    }
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    return Identity(raw["peer_id"], nick, x_private, ed_private)


def set_nick(identity, data_dir, nick):
    identity.nick = nick
    data_dir = Path(data_dir)
    path = data_dir / IDENTITY_FILE
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    else:
        raw = {}
    raw["nick"] = nick
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")