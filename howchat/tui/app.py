import time
from pathlib import Path

from rich.markup import escape as escape_markup

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, Input, Label, ListItem, ListView, RichLog, Static

from howchat import identity as identity_mod

HELP_TEXT = """\
可用命令：
  /connect <IP[:端口]>        直连指定节点（默认端口 41235）
  /peers                      查看在线用户
  /msg <昵称或ID>             切换到私聊会话（◇ 陌生联系人直接发消息即可，自动成为好友）
  /verify <ID或昵称>          确认对端公钥指纹（防中间人，建议双方都确认）
  /reject <ID或昵称>          拒绝并屏蔽对方
  /join <#频道> [成员ID...]    加入/创建群聊（不带成员时加入当前在线好友）
  /leave <#频道>              离开群聊
  /sendfile <目标> <路径>      发送文件（支持中继转发）
  /hop <目标> <跳板IP[:端口]>  通过跳板节点连接目标
  /bt scan                    扫描附近蓝牙 howchat 设备（约 4 秒）
  /bt connect <MAC[:频道]>     手动连接蓝牙设备（默认频道 4）
  /bt peers                   查看蓝牙邻居
  /nick <新昵称>              修改自己的昵称
  /finger <目标>              查看对端公钥指纹（用于身份校验）
  /whoami                     查看自己的 ID 与指纹
  /help                       显示本帮助
  /quit                       退出
提示：直连的设备自动成为好友；陌生联系人（◇）直接输入消息即可，会自动发送好友请求并排队，对方接受后自动送达。"""


class IncomingMessage(Message):
    def __init__(self, conv, entry):
        super().__init__()
        self.conv = conv
        self.entry = entry


class PeerEvent(Message):
    def __init__(self, peer_id, connected, info):
        super().__init__()
        self.peer_id = peer_id
        self.connected = connected
        self.info = info


class StatusEvent(Message):
    def __init__(self, text):
        super().__init__()
        self.text = text


class ConversationUpdated(Message):
    def __init__(self, conv):
        super().__init__()
        self.conv = conv


class HowchatApp(App):
    TITLE = "howchat"
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #contacts { width: 28; border: round $primary; }
    #contacts-label { padding: 0 1; background: $panel; color: $text; }
    #contacts-list { height: 1fr; }
    #chat { width: 1fr; }
    #messages { height: 1fr; border: round $secondary; }
    #input-bar { height: 3; }
    Input { border: round $accent; }
    #status { height: 1; color: $text-muted; padding: 0 1; }
    """

    def __init__(self, core, broadcast=True):
        super().__init__()
        self.core = core
        self.broadcast = broadcast
        self.current = None
        self.contacts_list = ListView(id="contacts-list")
        self.messages = RichLog(highlight=True, markup=True, id="messages")
        self.input = Input(placeholder="输入消息，/help 查看命令", id="input")
        self.status = Static("", id="status")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="contacts"):
                yield Label("会话与联系人", id="contacts-label")
                yield self.contacts_list
            with Vertical(id="chat"):
                yield self.messages
                with Horizontal(id="input-bar"):
                    yield self.input
        yield self.status
        yield Footer()

    async def on_mount(self):
        self.core.on_message = self._notify_message
        self.core.on_peer = self._notify_peer
        self.core.on_status = self._notify_status
        self.core.on_message_updated = self._notify_message_updated
        await self.core.start(broadcast=self.broadcast)
        port = getattr(self.core.transport, "tcp_port", None)
        port_str = f"  端口：{port}" if port else ""
        self._status(f"已启动。本机 ID：{self.core.identity.peer_id}  昵称：{self.core.identity.nick}{port_str}")
        self._status(f"本机指纹：{self.core.identity.fingerprint}")
        self._refresh_contacts()
        self._switch_conv(None)

    async def on_unmount(self):
        await self.core.stop()

    def _notify_message(self, conv, entry):
        self.post_message(IncomingMessage(conv, entry))

    def _notify_peer(self, peer_id, connected, info):
        self.post_message(PeerEvent(peer_id, connected, info))

    def _notify_status(self, text):
        self.post_message(StatusEvent(text))

    def _notify_message_updated(self, conv):
        self.post_message(ConversationUpdated(conv))

    def on_incoming_message(self, msg: IncomingMessage):
        self._render_entry(msg.entry)
        if msg.entry.get("conv") != self.current:
            self._refresh_contacts()

    def on_peer_event(self, msg: PeerEvent):
        self._refresh_contacts()
        if msg.connected and msg.info:
            self._status(f"用户 {msg.info.get('nick', msg.peer_id)} 已上线")
        else:
            self._status(f"用户 {msg.peer_id[:8]} 已离线")

    def on_status_event(self, msg: StatusEvent):
        self._status(msg.text)

    def on_conversation_updated(self, msg: ConversationUpdated):
        if self.current == msg.conv:
            self._switch_conv(self.current)

    def _status(self, text):
        self.status.update(escape_markup(text))

    def _contact_display(self, peer_id):
        c = self.core.store.get_contact(peer_id)
        if c and c.nick:
            return c.nick
        return peer_id[:8]

    def _refresh_contacts(self):
        online = self.core.router.neighbors()
        contacts = self.core.store.contacts()
        items = []
        for peer_id in sorted(contacts):
            c = contacts[peer_id]
            if c.status == "blocked":
                continue
            nick = self._contact_display(peer_id)
            if c.status == "friend":
                mark = "●" if peer_id in online else "○"
                label = f"{mark} {nick}"
                if c.confirmed_fingerprint:
                    label += " ✓"
            else:
                label = f"◇ {nick}"
            items.append((peer_id, label))
        channels = self.core.store.channels()
        for ch in sorted(channels):
            items.append((ch, f"#{ch.lstrip('#')}"))
        if not items:
            items.append(("", "(空)"))
        self.contacts_list.clear()
        for conv_id, label in items:
            item = ListItem(Label(label), name=conv_id)
            self.contacts_list.append(item)

    async def on_list_view_selected(self, event):
        self._switch_conv(event.item.name or "")

    def _switch_conv(self, conv_id):
        self.current = conv_id
        self.messages.clear()
        if not conv_id:
            self._write_system("选择一个会话开始聊天，或输入 /connect <IP> 连接其他用户")
            return
        for entry in self.core.store.history(conv_id):
            self._render_entry(entry)
        if conv_id.startswith("#"):
            members = self.core.store.channel_members(conv_id)
            self._write_system(f"当前频道 {conv_id} 成员：{'、'.join(self._contact_display(m) for m in members) or '无'}")
        else:
            fp = self.core.store.get_contact(conv_id)
            if fp and fp.status == "stranger":
                self._write_system("陌生联系人：直接输入消息即可，会自动发送好友请求，对方接受后送达")
            elif fp and fp.fingerprint:
                if fp.confirmed_fingerprint:
                    self._write_system(f"对端指纹：{fp.fingerprint}（已确认 ✓）")
                else:
                    self._write_system(f"对端指纹：{fp.fingerprint}（未确认，请线下核对后用 /verify 确认）")

    def _render_entry(self, entry):
        ts = time.strftime("%H:%M", time.localtime(entry.get("ts", 0)))
        role = escape_markup("我" if entry.get("role") == "me" else entry.get("nick", "?"))
        text = escape_markup(entry.get("text", ""))
        mark = ""
        if entry.get("role") == "me" and entry.get("status") == "queued":
            mark = " [orange3][排队中][/]"
        self.messages.write(f"[b]{ts} {role}:[/]{mark}\n    {text}")

    def _write_system(self, text):
        ts = time.strftime("%H:%M", time.localtime())
        self.messages.write(f"[dim]{ts} [系统][/dim] {escape_markup(text)}")

    async def on_input_submitted(self, event):
        text = event.value.strip()
        self.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self._run_command(text)
        elif self.current:
            await self._send_current(text)
        else:
            self._status("请先在左侧选择一个会话，或使用 /msg <目标> 指定私聊对象")

    async def _send_current(self, text):
        if self.current.startswith("#"):
            err = self.core.send_group(self.current, text)
        else:
            err = self.core.send_text(self.current, text)
        if err:
            self._status(err)

    async def _run_command(self, raw):
        parts = raw[1:].split()
        cmd = parts[0].lower()
        args = parts[1:]
        try:
            await self._dispatch(cmd, args)
        except Exception as e:
            self._status(f"命令出错：{e}")

    async def _dispatch(self, cmd, args):
        if cmd == "connect":
            if not args:
                return self._status("用法：/connect <IP[:端口]>")
            target = args[0]
            if ":" in target:
                ip, port = target.rsplit(":", 1)
                port = int(port)
            else:
                ip, port = target, 41235
            ok = await self.core.connect_host(ip, port)
            self._status("正在连接……" if ok else f"连接 {ip}:{port} 失败")
        elif cmd == "peers":
            online = self.core.router.neighbors()
            if not online:
                self._status("当前没有在线用户")
            else:
                names = ", ".join(self._contact_display(p) for p in sorted(online))
                self._status(f"在线用户：{names}")
        elif cmd == "add":
            if not args:
                return self._status("用法：/add <ID或昵称>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            err = self.core.request_friend(peer_id)
            if err:
                return self._status(err)
            self._status(f"已向 {self._contact_display(peer_id)} 发送好友请求，等待对方接受")
            self._refresh_contacts()
        elif cmd == "accept":
            if not args:
                return self._status("用法：/accept <ID或昵称>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            err = self.core.accept_friend(peer_id)
            if err:
                return self._status(err)
            self._refresh_contacts()
            if self.current == peer_id:
                self._switch_conv(peer_id)
        elif cmd == "reject":
            if not args:
                return self._status("用法：/reject <ID或昵称>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            err = self.core.decline_friend(peer_id)
            if err:
                return self._status(err)
            self._status(f"已拒绝并屏蔽 {self._contact_display(peer_id)}")
            self._refresh_contacts()
        elif cmd == "verify":
            if not args:
                return self._status("用法：/verify <ID或昵称>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            msg = self.core.verify_friend(peer_id)
            self._status(msg)
            if not msg.startswith("只能"):
                self._refresh_contacts()
                if self.current == peer_id:
                    self._switch_conv(peer_id)
        elif cmd == "bt":
            return await self._bt_command(args)
        elif cmd == "msg":
            if not args:
                return self._status("用法：/msg <昵称或ID>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            self._switch_conv(peer_id)
        elif cmd == "join":
            if not args:
                return self._status("用法：/join <#频道> [成员ID...]")
            channel = args[0] if args[0].startswith("#") else "#" + args[0]
            members = args[1:]
            if not members:
                members = list(self.core.router.neighbors())
            self.core.store.add_channel_member(channel, members)
            self._status(f"已加入频道 {channel}，成员：{', '.join(members) or '无'}")
            self._refresh_contacts()
            self._switch_conv(channel)
        elif cmd == "leave":
            if not args:
                return self._status("用法：/leave <#频道>")
            channel = args[0] if args[0].startswith("#") else "#" + args[0]
            self.core.store.remove_channel_member(channel, [self.core.identity.peer_id])
            self._status(f"已离开频道 {channel}")
            self._refresh_contacts()
            if self.current == channel:
                self._switch_conv(None)
        elif cmd == "sendfile":
            if len(args) < 2:
                return self._status("用法：/sendfile <目标> <文件路径>")
            peer_id = self._resolve(args[0])
            if not peer_id:
                return self._status("找不到该联系人")
            err = self.core.send_file(peer_id, args[1])
            if err:
                self._status(err)
        elif cmd == "hop":
            if len(args) < 2:
                return self._status("用法：/hop <目标ID> <跳板IP[:端口]>")
            target = args[0]
            hop = args[1]
            if ":" in hop:
                ip, port = hop.rsplit(":", 1)
                port = int(port)
            else:
                ip, port = hop, 41235
            ok = await self.core.connect_host(ip, port)
            self._status("正在连接跳板……" if ok else f"连接跳板 {ip}:{port} 失败")
            if ok:
                self.core.discover(target)
                self._status(f"已向 {target} 发起路由发现，稍后可尝试发送")
        elif cmd == "nick":
            if not args:
                return self._status("用法：/nick <新昵称>")
            nick = " ".join(args)
            identity_mod.set_nick(self.core.identity, self.core.store.data_dir.parent, nick)
            self._status(f"昵称已改为：{nick}（重启后对新连接生效）")
        elif cmd == "finger":
            if not args:
                return self._status("用法：/finger <目标>")
            peer_id = self._resolve(args[0])
            c = self.core.store.get_contact(peer_id) if peer_id else None
            if c and c.fingerprint:
                self._status(f"{c.nick}（{peer_id}）指纹：{c.fingerprint}")
            else:
                self._status("该联系人暂无指纹信息")
        elif cmd == "whoami":
            self._status(
                f"ID：{self.core.identity.peer_id}  昵称：{self.core.identity.nick}  指纹：{self.core.identity.fingerprint}"
            )
        elif cmd == "help":
            self._write_system(HELP_TEXT)
        elif cmd == "quit":
            self.exit()
        else:
            self._status(f"未知命令：{cmd}，输入 /help 查看帮助")

    def _resolve(self, name):
        c = self.core.store.get_contact(name)
        if c:
            return name
        for pid, contact in self.core.store.contacts().items():
            if contact.nick == name:
                return pid
        if name.startswith("#"):
            return None
        return None

    async def _bt_command(self, args):
        if not args:
            return self._status("用法：/bt scan | /bt connect <MAC[:频道]> | /bt peers")
        sub = args[0].lower()
        bt = self._bt_transport()
        if bt is None:
            return self._status("蓝牙不可用（未安装 pybluez 或没有蓝牙适配器）")
        if sub == "scan":
            self._status("正在扫描蓝牙设备（约 4 秒）……")
            devices = await bt.scan()
            if not devices:
                return self._status("未发现运行 howchat 的蓝牙设备")
            lines = [f"{name or mac}（{mac[:12]}, 频道 {port}）" for mac, port, name, _pid in devices]
            self._status("发现设备：" + " | ".join(lines))
            return None
        if sub == "connect":
            if len(args) < 2:
                return self._status("用法：/bt connect <MAC[:频道]>")
            target = args[1].lower()
            if ":" in target:
                mac, port = target.rsplit(":", 1)
                port = int(port)
            else:
                mac, port = target, 4
            ok = await self.core.connect_bluetooth(mac, port)
            return self._status("正在连接蓝牙……" if ok else f"连接蓝牙 {mac} 失败")
        if sub == "peers":
            peers = bt.neighbors() if bt.available else set()
            if not peers:
                return self._status("当前没有蓝牙邻居")
            names = ", ".join(self._contact_display(p) for p in sorted(peers))
            return self._status(f"蓝牙邻居：{names}")
        return self._status("用法：/bt scan | /bt connect <MAC[:频道]> | /bt peers")

    def _bt_transport(self):
        for t in self.core.transports:
            if getattr(t, "kind", "") == "bluetooth":
                return t
        return None
