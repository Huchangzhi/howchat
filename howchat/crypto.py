import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

CONTEXT = b"howchat-v1"


def derive_shared_key(x_private: x25519.X25519PrivateKey, x_public: x25519.X25519PublicKey) -> bytes:
    shared = x_private.exchange(x_public)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=CONTEXT,
    ).derive(shared)


def encrypt(shared_key: bytes, plaintext: bytes) -> tuple[bytes, bytes]:
    aesgcm = AESGCM(shared_key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return nonce, ciphertext


def decrypt(shared_key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    aesgcm = AESGCM(shared_key)
    return aesgcm.decrypt(nonce, ciphertext, None)


def sign(ed_private: ed25519.Ed25519PrivateKey, data: bytes) -> bytes:
    return ed_private.sign(data)


def verify(ed_public: ed25519.Ed25519PublicKey, data: bytes, signature: bytes) -> bool:
    try:
        ed_public.verify(signature, data)
        return True
    except Exception:
        return False


def x_public_from_bytes(raw: bytes) -> x25519.X25519PublicKey:
    return x25519.X25519PublicKey.from_public_bytes(raw)


def x_public_to_bytes(pub: x25519.X25519PublicKey) -> bytes:
    return pub.public_bytes_raw() if hasattr(pub, "public_bytes_raw") else pub.public_bytes_raw()


def ed_public_from_bytes(raw: bytes) -> ed25519.Ed25519PublicKey:
    return ed25519.Ed25519PublicKey.from_public_bytes(raw)