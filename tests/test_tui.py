import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from howchat import identity as identity_mod
from howchat.core import Core
from howchat.routing import Router
from howchat.store import Store
from howchat.transport.lan import LANTransport
from howchat.tui.app import (
    FilePickDialog,
    HowchatApp,
    InputDialog,
    JoinChannelDialog,
    OptionMenuDialog,
)


def make_core(data_dir, port):
    identity = identity_mod.load_or_create(data_dir)
    store = Store(os.path.join(data_dir, "store"))
    router = Router(identity.peer_id)
    transport = LANTransport(identity, router, host="127.0.0.1", tcp_port=port)
    core = Core(identity, store, router, transport)
    core.data_dir = data_dir
    return core, identity


@pytest.mark.asyncio
async def test_app_boots_and_whoami():
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40401)
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert "指纹" in str(app.status._Static__content)
        app.input.focus()
        app.input.value = "/whoami"
        await pilot.press("enter")
        await pilot.pause()
        assert identity.peer_id in str(app.status._Static__content)
        await pilot.press("q")
        await pilot.pause()


@pytest.mark.asyncio
async def test_app_sends_text_self():
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40402)
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        core.store.update_contact_keys("peer999", "测试", "", "")
        core.store.set_friend_status("peer999", "friend")
        app._refresh_contacts()
        await pilot.pause()
        app._switch_conv("peer999")
        app.input.focus()
        app.input.value = "你好世界"
        await pilot.press("enter")
        await pilot.pause()
        assert app.messages is not None


@pytest.mark.asyncio
async def test_command_palette_lists_all_features():
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40403)
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        names = [n for n, _h, _cb in app._palette_commands()]
        for kw in ("发送文件", "连接设备", "加入/创建群聊", "修改昵称", "蓝牙操作", "会话操作", "在线用户", "我的信息", "帮助"):
            assert any(kw in n for n in names), f"命令面板缺少：{kw}"


@pytest.mark.asyncio
async def test_connect_dialog_and_join_channel():
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40404)
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.action_connect()
        await pilot.pause()
        assert isinstance(app.screen, InputDialog)

        app.action_join_channel()
        await pilot.pause()
        assert isinstance(app.screen, JoinChannelDialog)
        # 频道名校验
        dlg = app.screen
        dlg.query_one("#channel-input").value = "../../evil"
        dlg.query_one("#ok").press()
        await pilot.pause()
        assert isinstance(app.screen, JoinChannelDialog), "非法频道名不应关闭对话框"


@pytest.mark.asyncio
async def test_file_picker_sends_file(tmp_path):
    import base64

    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40405)
    payload = tmp_path / "payload.txt"
    payload.write_text("hello 你好", encoding="utf-8")
    core.store.update_contact_keys(
        "peer888",
        "目标",
        base64.b64encode(identity.x_pub_bytes()).decode(),
        base64.b64encode(identity.ed_pub_bytes()).decode(),
    )
    core.store.set_friend_status("peer888", "friend")
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._switch_conv("peer888")
        app.action_send_file()
        await pilot.pause()
        assert isinstance(app.screen, FilePickDialog)
        dlg = app.screen
        tree = dlg.query_one("#tree")
        tree.path = Path(tmp_path)
        await pilot.pause()
        # 模拟选中文件
        ok_btn = dlg.query_one("#ok")
        assert ok_btn.disabled
        dlg.on_directory_tree_file_selected(
            type("Evt", (), {"path": payload})()
        )
        await pilot.pause()
        assert not ok_btn.disabled
        # 确认发送 -> 生成私聊历史
        dlg.query_one("#ok").press()
        await pilot.pause()
        hist = core.store.history("peer888")
        assert hist and "发送文件" in hist[-1]["text"], hist


@pytest.mark.asyncio
async def test_conv_actions_dialog():
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40406)
    core.store.update_contact_keys("peer777", "好友", "", "")
    core.store.set_friend_status("peer777", "friend")
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._switch_conv("peer777")
        app.action_conv_actions()
        await pilot.pause()
        assert isinstance(app.screen, OptionMenuDialog)


@pytest.mark.asyncio
async def test_dialog_cancel_does_not_crash():
    """取消对话框（回调收到 None）不应崩溃。"""
    d = tempfile.mkdtemp()
    core, identity = make_core(d, 40407)
    app = HowchatApp(core, broadcast=False)
    async with app.run_test() as pilot:
        await pilot.pause()
        for opener in (
            app.action_nick,
            app.action_connect,
            app.action_bluetooth,
        ):
            opener()
            await pilot.pause()
            # 直接以 None 触发取消路径
            if isinstance(app.screen, InputDialog):
                app.screen.dismiss(None)
            await pilot.pause()
        assert app.core.identity.nick  # 昵称未被清空
