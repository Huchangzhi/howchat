import base64
import json
import struct

HEADER_LEN = 4
TYPE_HELLO = "hello"
TYPE_PEER_LIST = "peer_list"
TYPE_ROUTE_REQUEST = "route_request"
TYPE_ROUTE_REPLY = "route_reply"
TYPE_MSG = "msg"
TYPE_GROUP_MSG = "group_msg"
TYPE_FILE_META = "file_meta"
TYPE_FILE_CHUNK = "file_chunk"
TYPE_FILE_ACK = "file_ack"
TYPE_PING = "ping"
TYPE_PONG = "pong"


def encode_frame(payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + payload


def decode_frame_buffer(buf: bytearray) -> list[bytes]:
    frames = []
    while len(buf) >= HEADER_LEN:
        (length,) = struct.unpack(">I", buf[:HEADER_LEN])
        if len(buf) < HEADER_LEN + length:
            break
        frames.append(bytes(buf[HEADER_LEN : HEADER_LEN + length]))
        del buf[: HEADER_LEN + length]
    return frames


def pack_envelope(envelope: dict) -> bytes:
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def unpack_envelope(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


def make_clear_envelope(etype, src, dst, payload, route=None, ttl=10, seq=0, ts=None):
    env = {
        "v": 1,
        "type": etype,
        "src": src,
        "dst": dst,
        "route": route if route is not None else [],
        "ttl": ttl,
        "seq": seq,
        "ts": ts,
        "body": payload,
    }
    return env


def _signed_bytes(etype, src, dst, seq, ts, nonce, ciphertext):
    return b"".join(
        [
            etype.encode(),
            src.encode(),
            dst.encode(),
            struct.pack(">II", seq, 0 if ts is None else ts),
            nonce,
            ciphertext,
        ]
    )


def make_encrypted_envelope(etype, src, dst, shared_key, signer_ed, payload, route=None, ttl=10, seq=0, ts=None):
    from howchat import crypto

    nonce, ciphertext = crypto.encrypt(shared_key, payload)
    signed = _signed_bytes(etype, src, dst, seq, ts, nonce, ciphertext)
    sig = crypto.sign(signer_ed, signed)
    env = {
        "v": 1,
        "type": etype,
        "src": src,
        "dst": dst,
        "route": route if route is not None else [],
        "ttl": ttl,
        "seq": seq,
        "ts": ts,
        "nonce": base64.b64encode(nonce).decode(),
        "cipher": base64.b64encode(ciphertext).decode(),
        "sig": base64.b64encode(sig).decode(),
    }
    return env


def decrypt_envelope(env, shared_key, sender_ed_pub) -> bytes:
    from howchat import crypto

    nonce = base64.b64decode(env["nonce"])
    ciphertext = base64.b64decode(env["cipher"])
    signed = _signed_bytes(
        env["type"], env["src"], env["dst"], env["seq"], env["ts"], nonce, ciphertext
    )
    sig = base64.b64decode(env["sig"])
    if not crypto.verify(sender_ed_pub, signed, sig):
        raise ValueError("签名校验失败")
    return crypto.decrypt(shared_key, nonce, ciphertext)