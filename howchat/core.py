import asyncio
import base64
import hashlib
import json
import time
import uuid
from pathlib import Path

from howchat import crypto, protocol
from howchat.store import STATUS_BLOCKED, STATUS_FRIEND, STATUS_PENDING, STATUS_STRANGER, Contact, is_safe_channel

FILE_CHUNK = 64 * 1024
FLUSH_INTERVAL = 5.0


class Core:
    def __init__(self, identity, store, router, transport, extra_transports=None):
        self.identity = identity
        self.store = store
        self.router = router
        self.transport = transport
        self.extra_transports = list(extra_transports or [])
        self.transports = [self.transport] + self.extra_transports
        self.on_message = None
        self.on_peer = None
        self.on_status = None
        self.on_message_updated = None
        self.on_file_request = None
        self._file_rcv = {}
        self._file_out = {}
        self._pending_file_req = {}
        self._uid_conv = {}
        self._pending_auto = {}
        self.router.on_deliver = self._on_delivered
        self.router.on_route = lambda _target: self.flush_queued()
        for t in self.transports:
            t.on_peer_change = self._on_peer_change
        self._flush_task = None
        self.data_dir = None

    async def start(self, broadcast=True):
        for t in self.transports:
            await t.start(broadcast=broadcast)
        self._flush_task = asyncio.get_running_loop().create_task(self._flush_loop())

    async def stop(self):
        if self._flush_task:
            self._flush_task.cancel()
        for t in self.transports:
            await t.stop()

    async def connect_host(self, addr, port):
        return await self.transport.connect_host(addr, port)

    async def connect_bluetooth(self, mac, port=None):
        for t in self.transports:
            if getattr(t, "kind", "") == "bluetooth" and t.available:
                return await t.connect_host(mac, port or getattr(t, "channel", 4))
        return False

    def discover(self, dst):
        self.router.discover(dst)

    def is_friend(self, peer_id):
        c = self.store.get_contact(peer_id)
        return bool(c and c.status == STATUS_FRIEND)

    def _friends(self):
        return [
            pid for pid, c in self.store.contacts().items() if c.status == STATUS_FRIEND
        ]

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

    def _dispatch(self, peer_id, env, conv=None):
        if self.router.send(env):
            return "sent", None
        uid = uuid.uuid4().hex
        self.store.queue_outbound(env, uid=uid)
        self._uid_conv[uid] = conv
        return "queued", uid

    # ---------- 好友机制 ----------

    def request_friend(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c:
            return "找不到该联系人"
        if c.status == STATUS_BLOCKED:
            return "你已屏蔽该用户"
        if c.status == STATUS_FRIEND:
            return "你们已经是好友"
        body = {"type": "friend_request", "nick": self.identity.nick}
        env = self._encrypted_env(protocol.TYPE_FRIEND_REQUEST, peer_id, body)
        if env is None:
            return "尚无对方公钥，请先连接或经中继获取"
        self.store.set_friend_status(peer_id, STATUS_PENDING)
        self._dispatch(peer_id, env, conv=peer_id)
        return None

    def accept_friend(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c:
            return "找不到该联系人"
        if c.status == STATUS_BLOCKED:
            return "你已屏蔽该用户"
        if c.status == STATUS_FRIEND:
            return "你们已经是好友"
        env = self._encrypted_env(protocol.TYPE_FRIEND_ACCEPT, peer_id, {"type": "friend_accept"})
        if env is None:
            return "尚无对方公钥，无法确认好友关系"
        self.store.set_friend_status(peer_id, STATUS_FRIEND)
        self._dispatch(peer_id, env, conv=peer_id)
        self._send_peer_list(peer_id)
        for n in self._friends():
            if n != peer_id:
                self._send_peer_list(n, peers=[peer_id])
        self._flush_pending_auto(peer_id)
        self._status(f"已与 {c.nick} 成为好友")
        return None

    def decline_friend(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c:
            return "找不到该联系人"
        env = self._encrypted_env(protocol.TYPE_FRIEND_DECLINE, peer_id, {"type": "friend_decline"})
        if env is not None:
            self._dispatch(peer_id, env, conv=peer_id)
        self.store.set_friend_status(peer_id, STATUS_BLOCKED)
        return None

    def verify_friend(self, peer_id):
        c = self.store.get_contact(peer_id)
        if not c or c.status != STATUS_FRIEND:
            return "只能验证好友的指纹"
        if not c.fingerprint:
            return "该联系人暂无指纹信息"
        self.store.mark_verified(peer_id, c.fingerprint)
        return f"已确认 {c.nick}（{peer_id}）的指纹：{c.fingerprint}"

    def _send_friend_accept(self, peer_id):
        env = self._encrypted_env(protocol.TYPE_FRIEND_ACCEPT, peer_id, {"type": "friend_accept"})
        if env is not None:
            self._dispatch(peer_id, env, conv=peer_id)

    def _become_friends(self, peer_id, notify=True, send_accept=False):
        was_friend = self.is_friend(peer_id)
        self.store.set_friend_status(peer_id, STATUS_FRIEND)
        if send_accept:
            self._send_friend_accept(peer_id)
        if not was_friend:
            self._send_peer_list(peer_id)
            for n in self._friends():
                if n != peer_id:
                    self._send_peer_list(n, peers=[peer_id])
        self._flush_pending_auto(peer_id)
        if notify and not was_friend:
            c = self.store.get_contact(peer_id)
            nick = c.nick if c else peer_id[:8]
            self._status(f"已与 {nick} 成为好友")

    def _flush_pending_auto(self, peer_id):
        pending = self._pending_auto.pop(peer_id, [])
        for env, conv, uid in pending:
            status, _ = self._dispatch(peer_id, env, conv=conv)
            if status == "sent":
                self.store.mark_history_sent(conv, uid)
                if self.on_message_updated:
                    self.on_message_updated(conv)

    def _handle_friend_request(self, sender):
        c = self.store.get_contact(sender)
        if c and c.status == STATUS_BLOCKED:
            self._status(f"已忽略被屏蔽用户 {sender[:8]} 的好友请求")
            return
        if c and c.status == STATUS_FRIEND:
            self._send_friend_accept(sender)
            return
        self._become_friends(sender, send_accept=True)

    def _handle_friend_accept(self, sender):
        self._become_friends(sender, notify=True)

    def _handle_friend_decline(self, sender):
        c = self.store.get_contact(sender)
        nick = c.nick if c else sender[:8]
        self.store.set_friend_status(sender, STATUS_STRANGER)
        self._status(f"{nick} 拒绝了你的好友请求")

    # ---------- 消息收发 ----------

    def send_text(self, peer_id, text, group=None):
        if not text:
            return "消息不能为空"
        c = self.store.get_contact(peer_id)
        if not c or not c.x_pub_b64:
            return "尚无对方公钥，请先连接或经中继获取"
        if c.status == STATUS_BLOCKED:
            return "你已屏蔽该用户，无法发送消息"
        body = {"type": "text", "text": text}
        if group:
            body["group"] = group
        env = self._encrypted_env(protocol.TYPE_MSG, peer_id, body)
        if env is None:
            return "找不到该联系人的密钥，请先建立连接或添加联系人"
        conv = group or peer_id
        if self.is_friend(peer_id):
            status, uid = self._dispatch(peer_id, env, conv=conv)
        else:
            self.request_friend(peer_id)
            uid = uuid.uuid4().hex
            self._pending_auto.setdefault(peer_id, []).append((env, conv, uid))
            status = "queued"
            self._status(f"已自动向 {c.nick} 发送好友请求，消息将在对方接受后送达")
        entry = self._me_entry(conv, body, status)
        if uid:
            entry["uid"] = uid
        self.store.append_history(conv, entry)
        self._emit_message(conv, entry)
        return None

    def send_group(self, channel, text):
        if not text:
            return "消息不能为空"
        if not is_safe_channel(channel):
            return "频道名不合法"
        members = set(self.store.channel_members(channel))
        members.add(self.identity.peer_id)
        if members != set(self.store.channel_members(channel)):
            self.store.add_channel_member(channel, sorted(members))
        friends = [m for m in members if self.is_friend(m)]
        if not friends:
            return f"频道 {channel} 没有可发送的好友成员"
        body = {
            "type": "text",
            "text": text,
            "group": channel,
            "members": sorted(members),
        }
        queued = False
        first_uid = None
        for m in friends:
            env = self._encrypted_env(protocol.TYPE_MSG, m, body)
            if env is None:
                continue
            status, uid = self._dispatch(m, env, conv=channel)
            if status == "queued":
                queued = True
                if first_uid is None:
                    first_uid = uid
        entry = self._me_entry(channel, body, "queued" if queued else "sent")
        if first_uid:
            entry["uid"] = first_uid
        self.store.append_history(channel, entry)
        self._emit_message(channel, entry)
        return None

    def send_file(self, peer_id, path):
        c = self.store.get_contact(peer_id)
        if not c or not c.x_pub_b64:
            return "尚无对方公钥，请先连接或经中继获取"
        if c.status == STATUS_BLOCKED:
            return "你已屏蔽该用户，无法发送文件"
        if not self.is_friend(peer_id):
            err = self.request_friend(peer_id)
            if err:
                return err
            return "已自动发送好友请求，对方接受后请重新发送文件"
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
        self._dispatch(peer_id, meta_env, conv=peer_id)
        # 保存待确认的文件：等待对方同意后再发送数据块
        self._file_out[file_id] = {
            "peer_id": peer_id,
            "name": path.name,
            "chunks": chunks,
        }
        entry = self._me_entry(
            peer_id,
            {"type": "text", "text": f"发送文件：{path.name}（{len(data)} 字节），等待对方接受…"},
            "waiting",
        )
        entry["uid"] = file_id
        self.store.append_history(peer_id, entry)
        self._emit_message(peer_id, entry)
        return None

    def _handle_file_ack(self, sender, body):
        st = self._file_out.get(body.get("file_id"))
        if not st or st["peer_id"] != sender:
            return
        self._file_out.pop(body["file_id"], None)
        if body.get("accept"):
            for i, chunk in enumerate(st["chunks"]):
                chunk_body = {
                    "type": "file_chunk",
                    "file_id": body["file_id"],
                    "index": i,
                    "data": base64.b64encode(chunk).decode(),
                }
                env = self._encrypted_env(protocol.TYPE_FILE_CHUNK, sender, chunk_body)
                if env is not None:
                    self._dispatch(sender, env, conv=sender)
            self.store.mark_history_sent(sender, body["file_id"])
            entry = self._me_entry(
                sender,
                {"type": "text", "text": f"对方已接受文件：{st['name']}，正在发送"},
                "sent",
            )
            self.store.append_history(sender, entry)
            self._emit_message(sender, entry)
        else:
            entry = self._me_entry(
                sender,
                {"type": "text", "text": f"对方拒绝了文件：{st['name']}"},
                "sent",
            )
            self.store.append_history(sender, entry)
            self._emit_message(sender, entry)
        if self.on_message_updated:
            self.on_message_updated(sender)

    def accept_file(self, file_id):
        req = self._pending_file_req.pop(file_id, None)
        if not req:
            return "没有待确认的文件请求"
        self._file_rcv[file_id] = {
            "name": req["name"],
            "size": req["size"],
            "sha256": req["sha256"],
            "total": req["total"],
            "parts": {},
            "sender": req["sender"],
            "nick": req["nick"],
        }
        ack = {"type": "file_ack", "file_id": file_id, "accept": True}
        env = self._encrypted_env(protocol.TYPE_FILE_ACK, req["sender"], ack)
        if env is not None:
            self._dispatch(req["sender"], env, conv=req["sender"])
        entry = {
            "role": "them",
            "nick": req["nick"],
            "type": "file",
            "text": f"正在接收文件：{req['name']}（{req['size']} 字节）",
            "ts": time.time(),
            "conv": req["sender"],
        }
        self.store.append_history(req["sender"], entry)
        self._emit_message(req["sender"], entry)
        return None

    def reject_file(self, file_id):
        req = self._pending_file_req.pop(file_id, None)
        if not req:
            return "没有待确认的文件请求"
        ack = {"type": "file_ack", "file_id": file_id, "accept": False}
        env = self._encrypted_env(protocol.TYPE_FILE_ACK, req["sender"], ack)
        if env is not None:
            self._dispatch(req["sender"], env, conv=req["sender"])
        entry = {
            "role": "them",
            "nick": req["nick"],
            "type": "file",
            "text": f"已拒绝接收文件：{req['name']}",
            "ts": time.time(),
            "conv": req["sender"],
        }
        self.store.append_history(req["sender"], entry)
        self._emit_message(req["sender"], entry)
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
            c = self.store.get_contact(peer_id)
            old_status = c.status if c else STATUS_STRANGER
            old_confirmed = c.confirmed_fingerprint if c else ""
            fp = self._fingerprint(info.get("x_pub", ""))
            self.store.update_contact_keys(
                peer_id,
                info.get("nick", ""),
                info.get("x_pub", ""),
                info.get("ed_pub", ""),
                fp,
                status=old_status,
            )
            if old_status not in (STATUS_FRIEND, STATUS_BLOCKED):
                nick = info.get("nick") or peer_id[:8]
                self._status(
                    f"已与 {nick} 建立直连（向对方发送消息即可自动发送好友申请）"
                )
            if old_confirmed and fp and fp != old_confirmed:
                nick = info.get("nick") or peer_id[:8]
                self._status(
                    f"【安全警告】好友 {nick} 的公钥指纹已变化（{fp}），"
                    "可能是中间人攻击！请用 /finger 线下核对"
                )
            self.flush_queued()
            if self.is_friend(peer_id):
                self._flush_pending_auto(peer_id)
                self._send_peer_list(peer_id)
                for n in self._friends():
                    if n != peer_id:
                        self._send_peer_list(n, peers=[peer_id])
        if self.on_peer:
            self.on_peer(peer_id, connected, info)

    def _send_peer_list(self, to_peer, peers=None):
        if not self.is_friend(to_peer):
            return
        entries = []
        for pid, c in self.store.contacts().items():
            if pid == to_peer or c.status != STATUS_FRIEND:
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
            protocol.TYPE_FRIEND_REQUEST,
            protocol.TYPE_FRIEND_ACCEPT,
            protocol.TYPE_FRIEND_DECLINE,
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
        if t == "friend_request":
            self._handle_friend_request(sender)
            return
        if t == "friend_accept":
            self._handle_friend_accept(sender)
            return
        if t == "friend_decline":
            self._handle_friend_decline(sender)
            return
        if not self.is_friend(sender):
            self._status(f"已忽略来自非好友 {nick} 的消息（可先 /add {sender[:8]}）")
            return
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
                self._status(f"通过好友 {nick} 获取了 {got} 位联系人的公钥")
            return
        if t == "text":
            conv = sender
            if body.get("group") and is_safe_channel(body["group"]):
                self._ensure_channel(body["group"], sender, body.get("members", []))
                conv = body["group"]
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
            fid = body["file_id"]
            self._pending_file_req[fid] = {
                "sender": sender,
                "nick": nick,
                "name": body["name"],
                "size": body["size"],
                "sha256": body["sha256"],
                "total": body["chunks"],
            }
            if self.on_file_request:
                self.on_file_request(sender, fid, body["name"], body["size"])
            else:
                self.accept_file(fid)
        elif t == "file_ack":
            self._handle_file_ack(sender, body)
        elif t == "file_chunk":
            st = self._file_rcv.get(body["file_id"])
            if st is None:
                return
            st["parts"][body["index"]] = base64.b64decode(body["data"])
            if len(st["parts"]) >= st["total"] and all(
                i in st["parts"] for i in range(st["total"])
            ):
                self._assemble_file(st)

    def _ensure_channel(self, channel, sender, members=()):
        if not is_safe_channel(channel):
            return
        known = self.store.channel_members(channel)
        merged = set(known)
        merged.add(self.identity.peer_id)
        merged.add(sender)
        merged.update(members)
        merged.discard("")
        self.store.add_channel_member(channel, sorted(merged))

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
        queued = self.store.queued()
        uids = self.store.queued_uids()
        if not queued:
            return
        sent = []
        updated_convs = set()
        for i, env in enumerate(queued):
            if self.router.send(env):
                sent.append(env)
                uid = uids[i] if i < len(uids) else None
                if uid:
                    conv = self._uid_conv.get(uid) or self.store.find_history_conv(uid)
                    if conv:
                        self.store.mark_history_sent(conv, uid)
                        updated_convs.add(conv)
                    self._uid_conv.pop(uid, None)
        if sent:
            self.store.clear_queued(sent)
        for conv in updated_convs:
            if self.on_message_updated:
                self.on_message_updated(conv)

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
