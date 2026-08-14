import time
from pathlib import Path

from rich.markup import escape as escape_markup

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import Command, DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    OptionList,
    RichLog,
    SelectionList,
    Static,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from howchat import identity as identity_mod
from howchat.store import STATUS_BLOCKED, STATUS_FRIEND, is_safe_channel

HELP_TEXT = """\
可用命令（所有命令也可通过 Ctrl+P 命令面板 / 顶部快捷方式完成）：

  会话操作
    Ctrl+F   发送文件（文件选择器直接挑选）
    Ctrl+O   当前会话操作（发文件 / 验证指纹 / 屏蔽 / 离开群聊…）
    Ctrl+D   查看在线用户

  设备与传输
    Ctrl+L   连接设备（IP[:端口]）
    Ctrl+B   蓝牙操作（扫描 / 手动连接 / 查看邻居）
    Ctrl+G   加入/创建群聊

  个人
    Ctrl+N   修改昵称
    F2       我的 ID 与指纹
    F1       帮助

  会话切换：左侧列表点击联系人 / 群聊；陌生联系人直接发消息即可自动成为好友。

  常用命令（等价于以上快捷键）
    /connect <IP[:端口]>    /join <#频道>    /leave <#频道>
    /nick <昵称>            /hop <目标> <跳板>
    /verify <目标>          /finger <目标>   /reject <目标>
    /sendfile <目标> <路径> /bt scan|connect|peers
    /peers /whoami /help /quit

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


class FileRequestMessage(Message):
    def __init__(self, sender, file_id, name, size):
        super().__init__()
        self.sender = sender
        self.file_id = file_id
        self.name = name
        self.size = size


# ---------------------------------------------------------------------------
# 命令面板
# ---------------------------------------------------------------------------


class HowchatCommands(Provider):
    """把核心功能全部注册进命令面板，所有操作无需手输命令。"""

    async def discover(self) -> list:
        app = self.app
        for name, help_text, handler in app._palette_commands():
            yield DiscoveryHit(name, handler, text=name, help=help_text)

    async def search(self, query: str):
        app = self.app
        matcher = self.matcher(query)
        for name, help_text, handler in app._palette_commands():
            score = matcher.match(name)
            if score is not None:
                yield Hit(score, name, handler, text=name, help=help_text)


# ---------------------------------------------------------------------------
# 通用对话框
# ---------------------------------------------------------------------------


class DialogScreen(ModalScreen):
    CSS = """
    DialogScreen { align: center middle; }
    #dialog, #file-picker, #menu {
        width: 72;
        max-width: 92%;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #dlg-title { text-style: bold; color: $primary; margin-bottom: 1; }
    #dlg-buttons { height: 3; align: center middle; }
    #dlg-scroll { height: 1fr; }
    #dlg-body { height: auto; }
    #file-picker { height: 80%; width: 90; }
    #menu { width: 56; height: auto; }
    #file-path { height: 1; color: $text-muted; padding: 0 1; }
    """


class InputDialog(DialogScreen):
    BINDINGS = [Binding("escape", "dismiss", "取消", show=False)]

    def __init__(self, title, prompt, placeholder="", default=""):
        super().__init__()
        self._title = title
        self._prompt = prompt
        self._placeholder = placeholder
        self._default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dlg-title")
            yield Label(self._prompt)
            yield Input(placeholder=self._placeholder, value=self._default, id="dlg-input")
            with Horizontal(id="dlg-buttons"):
                yield Button("确定", variant="primary", id="ok")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self):
        self.query_one("#dlg-input", Input).focus()

    def on_input_submitted(self, event):
        self._submit(event.value)

    def on_button_pressed(self, event):
        if event.button.id == "ok":
            self._submit(self.query_one("#dlg-input", Input).value)
        elif event.button.id == "cancel":
            self.dismiss(None)

    def _submit(self, value):
        value = value.strip()
        if not value:
            self.notify("输入不能为空", severity="warning")
            return
        self.dismiss(value)

    def action_dismiss(self):
        self.dismiss(None)


class TextDialog(DialogScreen):
    BINDINGS = [Binding("escape", "dismiss", "关闭", show=False)]

    def __init__(self, title, text):
        super().__init__()
        self._title = title
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dlg-title")
            with VerticalScroll(id="dlg-scroll"):
                yield Static(escape_markup(self._text), id="dlg-body")
            with Horizontal(id="dlg-buttons"):
                yield Button("关闭", variant="primary", id="close")

    def on_button_pressed(self, event):
        if event.button.id == "close":
            self.dismiss(None)

    def action_dismiss(self):
        self.dismiss(None)


class OptionMenuDialog(DialogScreen):
    """带标题 + 选项列表的菜单对话框。"""

    BINDINGS = [Binding("escape", "dismiss", "返回", show=False)]

    def __init__(self, title, options, hints=None):
        super().__init__()
        self._title = title
        self._options = options  # list of (prompt, id, disabled)
        self._hints = hints or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="menu"):
            yield Label(self._title, id="dlg-title")
            opts = OptionList(id="menu-options")
            for prompt, oid, disabled in self._options:
                opts.add_option(Option(prompt, id=oid, disabled=disabled))
            if self._hints:
                hint = "  ".join(f"{k}：{v}" for k, v in self._hints.items())
                yield Label(hint, id="menu-hint")
            yield opts

    def on_mount(self):
        self.query_one("#menu-options", OptionList).focus()

    def on_option_list_option_selected(self, event):
        self.dismiss(event.option.id)

    def action_dismiss(self):
        self.dismiss(None)


class JoinChannelDialog(DialogScreen):
    BINDINGS = [Binding("escape", "dismiss", "取消", show=False)]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("加入 / 创建群聊", id="dlg-title")
            yield Label("频道名（以 # 开头，如 #办公室）")
            yield Input(placeholder="#频道", id="channel-input")
            yield Label("选择要加入的成员（好友）")
            yield SelectionList(id="members")
            with Horizontal(id="dlg-buttons"):
                yield Button("确定", variant="primary", id="ok")
                yield Button("取消", variant="default", id="cancel")

    def on_mount(self):
        sel = self.query_one("#members", SelectionList)
        contacts = self.app.core.store.contacts()
        for pid, c in sorted(contacts.items()):
            if c.status == STATUS_FRIEND:
                sel.add_option(Selection(f"{c.nick}（{pid[:8]}）", pid))
        self.query_one("#channel-input", Input).focus()

    def on_input_submitted(self, event):
        self._submit(self.query_one("#members", SelectionList))

    def on_button_pressed(self, event):
        if event.button.id == "ok":
            self._submit(self.query_one("#members", SelectionList))
        elif event.button.id == "cancel":
            self.dismiss(None)

    def _submit(self, sel):
        channel = self.query_one("#channel-input", Input).value.strip()
        if not channel.startswith("#"):
            channel = "#" + channel
        if not is_safe_channel(channel):
            self.notify("频道名不合法（需 # 开头，长度 1-64，仅限字母数字下划线中文）", severity="warning")
            return
        members = list(sel.selected)
        self.dismiss((channel, members))

    def action_dismiss(self):
        self.dismiss(None)


class FilePickDialog(DialogScreen):
    """文件选择器：直接用目录树挑选要发送的文件。"""

    BINDINGS = [Binding("escape", "dismiss", "取消", show=False)]

    def __init__(self, start="~"):
        super().__init__()
        self._start = str(Path(start).expanduser())
        self._pending = None

    def compose(self) -> ComposeResult:
        with Vertical(id="file-picker"):
            yield Label("选择要发送的文件", id="dlg-title")
            yield DirectoryTree(self._start, id="tree")
            yield Static("未选择文件", id="file-path")
            with Horizontal(id="dlg-buttons"):
                yield Button("发送", variant="primary", id="ok", disabled=True)
                yield Button("取消", variant="default", id="cancel")

    def on_directory_tree_file_selected(self, event):
        self._pending = event.path
        self.query_one("#file-path", Static).update(f"已选择：{event.path}")
        self.query_one("#ok", Button).disabled = False

    def on_button_pressed(self, event):
        if event.button.id == "ok":
            self.dismiss(self._pending)
        elif event.button.id == "cancel":
            self.dismiss(None)

    def action_dismiss(self):
        self.dismiss(None)


class FileConfirmDialog(DialogScreen):
    """收到文件时询问用户是否接受。"""

    BINDINGS = [Binding("escape", "dismiss", "拒绝", show=False)]

    def __init__(self, sender, file_id, name, size):
        super().__init__()
        self._sender = sender
        self._file_id = file_id
        self._name = name
        self._file_size = size

    def compose(self) -> ComposeResult:
        nick = self.app._contact_display(self._sender)
        with Vertical(id="dialog"):
            yield Label("收到文件请求", id="dlg-title")
            yield Label(f"{nick} 想给你发送文件：\n\n  {self._name}（{self._file_size} 字节）\n\n是否接收？")
            with Horizontal(id="dlg-buttons"):
                yield Button("接受", variant="primary", id="accept")
                yield Button("拒绝", variant="error", id="reject")

    def on_button_pressed(self, event):
        if event.button.id == "accept":
            self.dismiss((self._file_id, True))
        elif event.button.id == "reject":
            self.dismiss((self._file_id, False))

    def action_dismiss(self):
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 主界面
# ---------------------------------------------------------------------------


class HowchatApp(App):
    TITLE = "howchat"
    COMMANDS = {HowchatCommands}
    BINDINGS = [
        Binding("ctrl+f", "send_file", "发送文件"),
        Binding("ctrl+l", "connect", "连接设备"),
        Binding("ctrl+g", "join_channel", "加入群聊"),
        Binding("ctrl+n", "nick", "修改昵称"),
        Binding("ctrl+b", "bluetooth", "蓝牙"),
        Binding("ctrl+o", "conv_actions", "会话操作"),
        Binding("ctrl+d", "peers", "在线用户"),
        Binding("f1", "show_help", "帮助"),
        Binding("f2", "my_info", "我的信息"),
    ]
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #contacts { width: 30; border: round $primary; }
    #contacts-label { padding: 0 1; background: $panel; color: $text; }
    #contacts-list { height: 1fr; }
    #chat { width: 1fr; }
    #conv-title { height: 1; background: $panel; padding: 0 1; color: $text; }
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
        self.input = Input(placeholder="输入消息，Ctrl+P 打开命令面板，/help 查看帮助", id="input")
        self.status = Static("", id="status")

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="main"):
            with Vertical(id="contacts"):
                yield Label("会话与联系人", id="contacts-label")
                yield self.contacts_list
            with Vertical(id="chat"):
                yield Static("", id="conv-title")
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
        self.core.on_file_request = self._notify_file_request
        await self.core.start(broadcast=self.broadcast)
        port = getattr(self.core.transport, "tcp_port", None)
        port_str = f"  端口：{port}" if port else ""
        self._status(f"已启动。本机 ID：{self.core.identity.peer_id}  昵称：{self.core.identity.nick}{port_str}")
        self._status(f"本机指纹：{self.core.identity.fingerprint}")
        self._refresh_contacts()
        self._switch_conv(None)

    async def on_unmount(self):
        await self.core.stop()

    # ---- 回调 -> 事件消息 ----

    def _notify_message(self, conv, entry):
        self.post_message(IncomingMessage(conv, entry))

    def _notify_peer(self, peer_id, connected, info):
        self.post_message(PeerEvent(peer_id, connected, info))

    def _notify_status(self, text):
        self.post_message(StatusEvent(text))

    def _notify_message_updated(self, conv):
        self.post_message(ConversationUpdated(conv))

    def _notify_file_request(self, sender, file_id, name, size):
        self.post_message(FileRequestMessage(sender, file_id, name, size))

    def on_file_request_message(self, msg: FileRequestMessage):
        self.push_screen(
            FileConfirmDialog(msg.sender, msg.file_id, msg.name, msg.size),
            self._on_file_confirm,
        )

    def _on_file_confirm(self, result):
        if not result:
            return
        file_id, accept = result
        if accept:
            self.core.accept_file(file_id)
            self._status("已接受文件，开始接收")
        else:
            self.core.reject_file(file_id)
            self._status("已拒绝接收文件")

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

    # ---- 会话列表 ----

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
            if c.status == STATUS_BLOCKED:
                continue
            nick = self._contact_display(peer_id)
            if c.status == STATUS_FRIEND:
                mark = "●" if peer_id in online else "○"
                label = f"{mark} {nick}"
                if c.confirmed_fingerprint:
                    label += " ✓"
            else:
                label = f"◇ {nick}"
            items.append((peer_id, label))
        channels = self.core.store.channels()
        for ch in sorted(channels):
            count = len(self.core.store.channel_members(ch))
            items.append((ch, f"# {ch.lstrip('#')}（{count} 人）"))
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
            self._update_conv_title("")
            self._write_system("选择一个会话开始聊天，或 Ctrl+P 打开命令面板、Ctrl+L 连接其他用户")
            return
        if conv_id.startswith("#"):
            self._update_conv_title(f"  {conv_id}（群聊）")
        else:
            self._update_conv_title(f"  {self._contact_display(conv_id)}（私聊）")
        for entry in self.core.store.history(conv_id):
            self._render_entry(entry)
        if conv_id.startswith("#"):
            members = self.core.store.channel_members(conv_id)
            self._write_system(
                f"当前频道 {conv_id} 成员：{'、'.join(self._contact_display(m) for m in members) or '无'}"
            )
        else:
            fp = self.core.store.get_contact(conv_id)
            if fp and fp.status == "stranger":
                self._write_system("陌生联系人：直接输入消息即可，会自动发送好友请求，对方接受后送达")
            elif fp and fp.fingerprint:
                if fp.confirmed_fingerprint:
                    self._write_system(f"对端指纹：{fp.fingerprint}（已确认 ✓）")
                else:
                    self._write_system(f"对端指纹：{fp.fingerprint}（未确认，请线下核对后确认）")

    def _update_conv_title(self, text):
        try:
            self.query_one("#conv-title", Static).update(text)
        except Exception:
            pass

    def _render_entry(self, entry):
        ts = time.strftime("%H:%M", time.localtime(entry.get("ts", 0)))
        role = escape_markup("我" if entry.get("role") == "me" else entry.get("nick", "?"))
        text = escape_markup(entry.get("text", ""))
        mark = ""
        if entry.get("role") == "me":
            if entry.get("status") == "queued":
                mark = " [orange3][排队中][/]"
            elif entry.get("status") == "waiting":
                mark = " [orange3][等待对方接受][/]"
        self.messages.write(f"[b]{ts} {role}:[/]{mark}\n    {text}")

    def _write_system(self, text):
        ts = time.strftime("%H:%M", time.localtime())
        self.messages.write(f"[dim]{ts} [系统][/dim] {escape_markup(text)}")

    # ---- 输入处理 ----

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
            self._status("请先在左侧选择一个会话，或使用 Ctrl+P 命令面板 / /msg <目标> 指定私聊对象")

    async def _send_current(self, text):
        if self.current.startswith("#"):
            err = self.core.send_group(self.current, text)
        else:
            err = self.core.send_text(self.current, text)
        if err:
            self._status(err)

    # ---- 快捷键动作 ----

    def _palette_commands(self):
        cmds = [
            ("发送文件", "选择文件发送给当前私聊对象", self.action_send_file),
            ("连接设备", "通过局域网地址直连", self.action_connect),
            ("加入/创建群聊", "加入或创建频道", self.action_join_channel),
            ("修改昵称", "更改自己的昵称", self.action_nick),
            ("蓝牙操作", "扫描 / 手动连接 / 查看邻居", self.action_bluetooth),
            ("会话操作", "对当前会话执行操作", self.action_conv_actions),
            ("查看在线用户", "列出当前在线用户", self.action_peers),
            ("我的信息", "查看自己的 ID 与指纹", self.action_my_info),
            ("帮助", "显示帮助", self.action_show_help),
        ]
        for pid, c in sorted(self.core.store.contacts().items()):
            if c.status == STATUS_BLOCKED:
                continue
            cmds.append(
                (f"聊天：{c.nick}", f"切换到与 {c.nick} 的私聊", lambda pid=pid: self._switch_conv(pid))
            )
        for ch in sorted(self.core.store.channels()):
            cmds.append((f"群聊：{ch}", f"切换到频道 {ch}", lambda ch=ch: self._switch_conv(ch)))
        return cmds

    def action_connect(self):
        self.push_screen(
            InputDialog("连接设备", "输入 IP[:端口]，默认端口 41235", placeholder="192.168.1.10:41235"),
            self._do_connect,
        )

    async def _do_connect(self, value):
        if not value:
            return
        target = value.strip()
        if ":" in target:
            ip, port = target.rsplit(":", 1)
            try:
                port = int(port)
            except ValueError:
                return self._status("端口不合法")
        else:
            ip, port = target, 41235
        ok = await self.core.connect_host(ip, port)
        self._status("正在连接……" if ok else f"连接 {ip}:{port} 失败")

    def action_join_channel(self):
        self.push_screen(JoinChannelDialog(), self._do_join_channel)

    def _do_join_channel(self, result):
        if not result:
            return
        channel, members = result
        members = list(dict.fromkeys([self.core.identity.peer_id] + members))
        self.core.store.add_channel_member(channel, members)
        self._status(f"已加入频道 {channel}，成员：{', '.join(members) or '无'}")
        self._refresh_contacts()
        self._switch_conv(channel)

    def action_nick(self):
        self.push_screen(
            InputDialog("修改昵称", "输入新昵称", default=self.core.identity.nick),
            self._do_nick,
        )

    def _do_nick(self, value):
        if not value:
            return
        nick = value.strip()
        if not nick:
            return self._status("昵称不能为空")
        identity_mod.set_nick(self.core.identity, self.core.data_dir, nick)
        self._status(f"昵称已改为：{nick}（对新连接生效）")

    def action_peers(self):
        online = self.core.router.neighbors()
        if not online:
            self._status("当前没有在线用户")
        else:
            names = ", ".join(self._contact_display(p) for p in sorted(online))
            self._status(f"在线用户：{names}")

    def action_my_info(self):
        self.push_screen(
            TextDialog(
                "我的信息",
                f"ID：{self.core.identity.peer_id}\n昵称：{self.core.identity.nick}\n指纹：{self.core.identity.fingerprint}",
            )
        )

    def action_show_help(self):
        self.push_screen(TextDialog("帮助", HELP_TEXT))

    def action_send_file(self):
        if not self.current:
            return self._status("请先选择私聊会话目标（文件只发送给单个联系人）")
        if self.current.startswith("#"):
            return self._status("文件发送到私聊，请先在左侧选择联系人会话")
        self.push_screen(FilePickDialog(), self._do_send_file)

    def _do_send_file(self, result):
        if not result:
            return
        path = str(result)
        err = self.core.send_file(self.current, path)
        if err:
            self._status(err)

    def action_bluetooth(self):
        bt = self._bt_transport()
        if bt is None or not bt.available:
            return self._status("蓝牙不可用（未安装 pybluez 或没有蓝牙适配器）")
        options = [
            ("扫描附近 howchat 设备", "scan", False),
            ("手动连接设备（MAC）", "connect", False),
            ("查看当前蓝牙邻居", "peers", False),
        ]
        self.push_screen(OptionMenuDialog("蓝牙操作", options), self._do_bt_action)

    def _do_bt_action(self, result):
        if result == "scan":
            self._status("正在扫描蓝牙设备（约 4 秒）……")
            self.run_worker(self._bt_scan())
        elif result == "connect":
            self.push_screen(
                InputDialog("蓝牙手动连接", "输入 MAC[:频道]，默认频道 4", placeholder="AA:BB:CC:DD:EE:FF"),
                self._do_bt_connect,
            )
        elif result == "peers":
            bt = self._bt_transport()
            peers = bt.neighbors() if bt else set()
            if not peers:
                self._status("当前没有蓝牙邻居")
            else:
                names = ", ".join(self._contact_display(p) for p in sorted(peers))
                self._status(f"蓝牙邻居：{names}")

    async def _bt_scan(self):
        bt = self._bt_transport()
        if bt is None or not bt.available:
            self._status("蓝牙不可用")
            return
        devices = await bt.scan()
        if not devices:
            self._status("未发现运行 howchat 的蓝牙设备")
            return
        lines = [f"{name or mac}（{mac[:12]}, 频道 {port}）" for mac, port, name, _pid in devices]
        self._status("发现设备：" + " | ".join(lines))

    async def _do_bt_connect(self, value):
        if not value:
            return
        target = value.strip().lower()
        if ":" in target:
            mac, port = target.rsplit(":", 1)
            try:
                port = int(port)
            except ValueError:
                return self._status("频道不合法")
        else:
            mac, port = target, 4
        ok = await self.core.connect_bluetooth(mac, port)
        self._status("正在连接蓝牙……" if ok else f"连接蓝牙 {mac} 失败")

    def action_conv_actions(self):
        if not self.current:
            return self._status("请先选择一个会话")
        is_group = self.current.startswith("#")
        if is_group:
            options = [
                ("离开群聊", "leave", False),
                ("查看频道成员", "members", False),
            ]
        else:
            c = self.core.store.get_contact(self.current)
            is_friend = bool(c and c.status == STATUS_FRIEND)
            options = []
            if is_friend:
                options.append(("发送文件", "sendfile", False))
                options.append(("验证对端指纹（防中间人）", "verify", False))
            else:
                options.append(("发送好友申请", "friend", False))
            options.append(("查看对端指纹", "finger", False))
            options.append(("屏蔽联系人", "reject", False))
        self.push_screen(OptionMenuDialog(f"会话操作：{self._conv_label()}", options), self._do_conv_action)

    def _conv_label(self):
        if not self.current:
            return ""
        if self.current.startswith("#"):
            return self.current
        return self._contact_display(self.current)

    def _do_conv_action(self, result):
        if not result:
            return
        peer_id = self.current if not self.current.startswith("#") else None
        if result == "sendfile":
            self.action_send_file()
        elif result == "verify":
            if peer_id:
                msg = self.core.verify_friend(peer_id)
                self._status(msg)
                self._refresh_contacts()
                if self.current == peer_id:
                    self._switch_conv(peer_id)
        elif result == "finger":
            if peer_id:
                c = self.core.store.get_contact(peer_id)
                if c and c.fingerprint:
                    self._status(f"{c.nick}（{peer_id}）指纹：{c.fingerprint}")
                else:
                    self._status("该联系人暂无指纹信息")
        elif result == "reject":
            if peer_id:
                err = self.core.decline_friend(peer_id)
                if err:
                    self._status(err)
                else:
                    self._status(f"已拒绝并屏蔽 {self._contact_display(peer_id)}")
                    self._refresh_contacts()
                    if self.current == peer_id:
                        self._switch_conv(None)
        elif result == "friend":
            if peer_id:
                err = self.core.request_friend(peer_id)
                if err:
                    self._status(err)
                else:
                    self._status(f"已向 {self._contact_display(peer_id)} 发送好友申请")
                    self._refresh_contacts()
        elif result == "leave":
            self.core.store.remove_channel_member(self.current, [self.core.identity.peer_id])
            self._status(f"已离开频道 {self.current}")
            self._refresh_contacts()
            self._switch_conv(None)
        elif result == "members":
            members = self.core.store.channel_members(self.current)
            self._status(
                f"频道 {self.current} 成员：{'、'.join(self._contact_display(m) for m in members) or '无'}"
            )

    def _bt_transport(self):
        for t in self.core.transports:
            if getattr(t, "kind", "") == "bluetooth":
                return t
        return None

    # ---- 斜杠命令（保留 / 兼容） ----

    async def _run_command(self, raw):
        parts = raw[1:].split()
        if not parts:
            return self._status("命令不能为空")
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
                try:
                    port = int(port)
                except ValueError:
                    return self._status("端口不合法")
            else:
                ip, port = target, 41235
            ok = await self.core.connect_host(ip, port)
            self._status("正在连接……" if ok else f"连接 {ip}:{port} 失败")
        elif cmd == "peers":
            self.action_peers()
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
            if not is_safe_channel(channel):
                return self._status("频道名不合法")
            members = args[1:]
            if not members:
                members = list(self.core.router.neighbors())
            members = list(dict.fromkeys([self.core.identity.peer_id] + members))
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
                try:
                    port = int(port)
                except ValueError:
                    return self._status("端口不合法")
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
            identity_mod.set_nick(self.core.identity, self.core.data_dir, nick)
            self._status(f"昵称已改为：{nick}（对新连接生效）")
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
            self._status(f"未知命令：{cmd}，Ctrl+P 打开命令面板或输入 /help 查看帮助")

    def _resolve(self, name):
        c = self.core.store.get_contact(name)
        if c:
            return name
        for pid, contact in self.core.store.contacts().items():
            if contact.nick == name:
                return pid
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
            self.run_worker(self._bt_scan())
            return None
        if sub == "connect":
            if len(args) < 2:
                return self._status("用法：/bt connect <MAC[:频道]>")
            target = args[1].lower()
            if ":" in target:
                mac, port = target.rsplit(":", 1)
                try:
                    port = int(port)
                except ValueError:
                    return self._status("频道不合法")
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
