import asyncio
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from howchat import crypto, protocol

FILE_CHUNK = 64 * 1024
FLUSH_INTERVAL = 5.0


class Core:
    def __init__(self, identity, store, router, transport):
        self.identity = identity
        self.store = store
        self.router = router
        self.transport = transport
        self.on_message = None
        self.on_peer = None
        self.on_status = None
        self._file_rcv = {}
        self.router.on_deliver = self._on_delivered
        self.transport.on_peer_change = self._on_peer_change
        self._flush_task = None

    async def start(self, broadcast=True):
        await self.transport.start(broadcast=broadcast)
        self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop())

    async def stop(self):
        if self._flush_task:
            self._flush_task.cancel()

    async def connect_host(self, addr, port):
        return await self.transport.connect_host(addr, port)

    def discover(self, dst):
        self.router.discover(dst)

    def _next_seq(self):
        return self.router.next_seq()

    def _shared_key(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c or not c.x_pub_b64:
            return None
        try:
            x_pub = crypto.x_public_from_bytes(base64.b64decode(c.x_pub_b64))
        except ValueError:
            return None
        return crypto.derive_shared_key(self.identity.x_private, x_pub)

    def _ed_pub(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c or not c.ed_pub_b64:
            return None
        try:
            return crypto.ed_public_from_bytes(base64.b64decode(c.ed_pub_b64))
        except ValueError:
            return None

    def _fingerprint(self, x_pub_b64):
        try:
            pub = base64.b64decode(x_pub_b64)
            h = hashlib.sha256(pub).digest()[:8]
            return " ".join(f"{b:02X}" for b in h)
        except (ValueError, TypeError):
            return ""

    def _encrypted_env(self, etype, dst, body, route=None):
        key = self._shared_key(dst)
        if key is None:
            return None
        return protocol.make_encrypted_envelope(
            etype,
            self.identity.peer_id,
            dst,
            key,
            self.identity.ed_private,
            json.dumps(body, ensure_ascii=False).encode("utf-8"),
            route=route,
            seq=self._next_seq(),
            ts=int(time.time()),
        )

    def _dispatch(self, peer_id, env):
        if self.router.send(env):
            return "sent"
        self.store.queue_outbound(env)
        return "queued"

    def send_text(self, peer_id, text, group=None):
        if not text:
            return "消息不能为空"
        body = {"type": "text", "text": text}
        if group:
            body["group"] = group
        env = self._encrypted_env(protocol.TYPE_MSG, peer_id, body)
        if env is None:
            return "找不到该联系人的密钥，请先建立连接或添加联系人"
        status = self._dispatch(peer_id, env)
        conv = group or peer_id
        entry = self._me_entry(conv, body, status)
        self.store.append_history(conv, entry)
        self._emit_message(conv, entry)
        return None

    def send_group(self, channel, text):
        members = self.store.channel_members(channel)
        if not members:
            return f"频道 {channel} 还没有成员，请先用 /join 添加"
        for m in members:
            err = self.send_text(m, text, group=channel)
            if err:
                return err
        return None

    def send_file(self, peer_id, path):
        path = Path(path)
        if not path.exists() or not path.is_file():
            return "文件不存在"
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        chunks = [data[i : i + FILE_CHUNK] for i in range(0, len(data), FILE_CHUNK)]
        if not chunks:
            chunks = [b""]
        file_id = uuid.uuid4().hex
        meta = {
            "type": "file_meta",
            "file_id": file_id,
            "name": path.name,
            "size": len(data),
            "sha256": sha,
            "chunks": len(chunks),
        }
        meta_env = self._encrypted_env(protocol.TYPE_FILE_META, peer_id, meta)
        if meta_env is None:
            return "找不到该联系人的密钥，请先建立连接"
        self._dispatch(peer_id, meta_env)
        for i, chunk in enumerate(chunks):
            body = {
                "type": "file_chunk",
                "file_id": file_id,
                "index": i,
                "data": base64.b64encode(chunk).decode(),
            }
            env = self._encrypted_env(protocol.TYPE_FILE_CHUNK, peer_id, body)
            self._dispatch(peer_id, env)
        entry = self._me_entry(peer_id, {"type": "text", "text": f"发送文件：{path.name}（{len(data)} 字节）"}, "sent")
        self.store.append_history(peer_id, entry)
        self._emit_message(peer_id, entry)
        return None

    def _me_entry(self, conv, body, status):
        text = body.get("text", "")
        if body.get("type") == "file":
            text = body.get("name", "")
        return {
            "role": "me",
            "nick": self.identity.nick,
            "type": body.get("type", "text"),
            "text": text,
            "ts": time.time(),
            "conv": conv,
            "status": status,
        }

    def _on_peer_change(self, peer_id, connected, info):
        if connected and info:
            fp = self._fingerprint(info.get("x_pub", ""))
            self.store.update_contact_keys(
                peer_id,
                info.get("nick", ""),
                info.get("x_pub", ""),
                info.get("ed_pub", ""),
                fp,
            )
            self.flush_queued()
            self._send_peer_list(peer_id)
            for n in self.transport.neighbors():
                if n != peer_id:
                    self._send_peer_list(n, peers=[peer_id])
        if self.on_peer:
            self.on_peer(peer_id, connected, info)

    def _send_peer_list(self, to_peer, peers=None):
        entries = []
        for pid, c in self.store.contacts().items():
            if pid == to_peer:
                continue
            if peers is not None and pid not in peers:
                continue
            entries.append(
                {
                    "id": pid,
                    "nick": c.nick,
                    "x_pub": c.x_pub_b64,
                    "ed_pub": c.ed_pub_b64,
                }
            )
        if not entries:
            return
        env = self._encrypted_env(
            protocol.TYPE_PEER_LIST, to_peer, {"type": "peer_list", "peers": entries}
        )
        if env is not None:
            self.router.send(env)

    def _on_delivered(self, env):
        etype = env["type"]
        if etype not in (
            protocol.TYPE_MSG,
            protocol.TYPE_GROUP_MSG,
            protocol.TYPE_FILE_META,
            protocol.TYPE_FILE_CHUNK,
            protocol.TYPE_FILE_ACK,
            protocol.TYPE_PEER_LIST,
        ):
            return
        key = self._shared_key(env["src"])
        ed_pub = self._ed_pub(env["src"])
        if key is None or ed_pub is None:
            self._status(f"无法解密来自 {env['src']} 的消息：缺少公钥")
            return
        try:
            payload = protocol.decrypt_envelope(env, key, ed_pub)
            body = json.loads(payload.decode("utf-8"))
        except Exception as e:
            self._status(f"解密失败：{e}")
            return
        self._handle_body(env["src"], body)

    def _handle_body(self, sender, body):
        c = self.store.get_contact(sender)
        nick = c.nick if c else sender[:8]
        t = body.get("type")
        if t == "peer_list":
            got = 0
            for p in body.get("peers", []):
                pid = p.get("id")
                if not pid or pid == self.identity.peer_id:
                    continue
                fp = self._fingerprint(p.get("x_pub", ""))
                self.store.update_contact_keys(
                    pid, p.get("nick", ""), p.get("x_pub", ""), p.get("ed_pub", ""), fp
                )
                got += 1
            if got:
                self._status(f"通过 {nick} 获取了 {got} 位联系人的公钥")
            return
        if t == "text":
            conv = body.get("group") or sender
            entry = {
                "role": "them",
                "nick": nick,
                "type": "text",
                "text": body.get("text", ""),
                "ts": time.time(),
                "conv": conv,
            }
            self.store.append_history(conv, entry)
            self._emit_message(conv, entry)
        elif t == "file_meta":
            self._file_rcv[body["file_id"]] = {
                "name": body["name"],
                "size": body["size"],
                "sha256": body["sha256"],
                "total": body["chunks"],
                "parts": {},
                "sender": sender,
                "nick": nick,
            }
            entry = {
                "role": "them", "nick": nick, "type": "file",
                "text": f"正在接收文件：{body['name']}（{body['size']} 字节）",
                "ts": time.time(), "conv": sender,
            }
            self.store.append_history(sender, entry)
            self._emit_message(sender, entry)
        elif t == "file_chunk":
            st = self._file_rcv.get(body["file_id"])
            if st is None:
                return
            st["parts"][body["index"]] = base64.b64decode(body["data"])
            if len(st["parts"]) >= st["total"]:
                self._assemble_file(st)

    def _assemble_file(self, st):
        data = b"".join(st["parts"][i] for i in range(st["total"]))
        sha = hashlib.sha256(data).hexdigest()
        if sha != st["sha256"]:
            entry = {
                "role": "them", "nick": st["nick"], "type": "file",
                "text": f"文件 {st['name']} 校验失败，已丢弃", "ts": time.time(),
                "conv": st["sender"],
            }
        else:
            dest = self.store.files_path() / st["name"]
            dest.write_bytes(data)
            entry = {
                "role": "them", "nick": st["nick"], "type": "file",
                "text": f"已接收文件：{st['name']}（{len(data)} 字节），保存到 {dest}",
                "ts": time.time(), "conv": st["sender"],
            }
        self.store.append_history(st["sender"], entry)
        self._emit_message(st["sender"], entry)
        key = next((k for k, v in self._file_rcv.items() if v is st), None)
        if key:
            self._file_rcv.pop(key, None)

    def flush_queued(self):
        sent = []
        for env in self.store.queued():
            if self.router.send(env):
                sent.append(env)
        if sent:
            self.store.clear_queued(sent)

    async def _flush_loop(self):
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            self.flush_queued()

    def _emit_message(self, conv, entry):
        if self.on_message:
            self.on_message(conv, entry)

    def _status(self, text):
        if self.on_status:
            self.on_status(text)
