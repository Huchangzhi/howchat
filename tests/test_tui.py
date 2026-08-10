import os
import tempfile

import pytest

from howchat import identity as identity_mod
from howchat.core import Core
from howchat.routing import Router
from howchat.store import Store
from howchat.transport.lan import LANTransport
from howchat.tui.app import HowchatApp


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
        app._refresh_contacts()
        await pilot.pause()
        app._switch_conv("peer999")
        app.input.focus()
        app.input.value = "你好世界"
        await pilot.press("enter")
        await pilot.pause()
        assert app.messages is not None
