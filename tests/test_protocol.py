from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from howchat import crypto, protocol


def test_frame_roundtrip():
    buf = bytearray()
    buf += protocol.encode_frame(b"abc")
    buf += protocol.encode_frame(b"def")
    assert protocol.decode_frame_buffer(buf) == [b"abc", b"def"]


def test_encrypted_envelope_roundtrip():
    alice_x = x25519.X25519PrivateKey.generate()
    bob_x = x25519.X25519PrivateKey.generate()
    alice_ed = ed25519.Ed25519PrivateKey.generate()

    key = crypto.derive_shared_key(alice_x, bob_x.public_key())
    env = protocol.make_encrypted_envelope(
        protocol.TYPE_MSG,
        "alice",
        "bob",
        key,
        alice_ed,
        '{"text": "你好"}'.encode("utf-8"),
        route=["relay"],
        seq=1,
        ts=1700000000,
    )
    assert "cipher" in env and "nonce" in env and "sig" in env
    assert "body" not in env

    bob_key = crypto.derive_shared_key(bob_x, alice_x.public_key())
    payload = protocol.decrypt_envelope(env, bob_key, alice_ed.public_key())
    assert payload == '{"text": "你好"}'.encode("utf-8")


def test_tampered_envelope_fails():
    alice_x = x25519.X25519PrivateKey.generate()
    bob_x = x25519.X25519PrivateKey.generate()
    alice_ed = ed25519.Ed25519PrivateKey.generate()

    key = crypto.derive_shared_key(alice_x, bob_x.public_key())
    env = protocol.make_encrypted_envelope(
        protocol.TYPE_MSG, "alice", "bob", key, alice_ed, b"secret", seq=3, ts=1700000000
    )
    env["dst"] = "mallory"
    bob_key = crypto.derive_shared_key(bob_x, alice_x.public_key())
    try:
        protocol.decrypt_envelope(env, bob_key, alice_ed.public_key())
        assert False, "篡改应导致校验失败"
    except ValueError:
        pass
