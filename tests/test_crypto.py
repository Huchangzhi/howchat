from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from howchat import crypto


def test_shared_key_symmetry():
    a = x25519.X25519PrivateKey.generate()
    b = x25519.X25519PrivateKey.generate()
    ka = crypto.derive_shared_key(a, b.public_key())
    kb = crypto.derive_shared_key(b, a.public_key())
    assert ka == kb


def test_encrypt_decrypt_roundtrip():
    a = x25519.X25519PrivateKey.generate()
    b = x25519.X25519PrivateKey.generate()
    key = crypto.derive_shared_key(a, b.public_key())
    nonce, ct = crypto.encrypt(key, "hello 世界".encode("utf-8"))
    assert crypto.decrypt(key, nonce, ct) == "hello 世界".encode("utf-8")


def test_decrypt_wrong_key_fails():
    a = x25519.X25519PrivateKey.generate()
    b = x25519.X25519PrivateKey.generate()
    c = x25519.X25519PrivateKey.generate()
    key = crypto.derive_shared_key(a, b.public_key())
    wrong = crypto.derive_shared_key(a, c.public_key())
    nonce, ct = crypto.encrypt(key, b"secret")
    try:
        crypto.decrypt(wrong, nonce, ct)
        assert False, "应解密失败"
    except Exception:
        pass


def test_sign_verify():
    sk = ed25519.Ed25519PrivateKey.generate()
    sig = crypto.sign(sk, b"data")
    assert crypto.verify(sk.public_key(), b"data", sig)
    assert not crypto.verify(sk.public_key(), b"tampered", sig)
