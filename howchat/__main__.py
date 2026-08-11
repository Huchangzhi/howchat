import argparse
from pathlib import Path

from howchat import identity as identity_mod
from howchat.core import Core
from howchat.routing import Router
from howchat.store import Store
from howchat.transport.bluetooth import BluetoothTransport
from howchat.transport.lan import LANTransport
from howchat.tui.app import HowchatApp


def main():
    parser = argparse.ArgumentParser(
        prog="howchat", description="离线去中心化端到端加密即时通讯（局域网/蓝牙中继跳转）"
    )
    parser.add_argument(
        "--data-dir",
        default="~/.howchat",
        help="数据目录（身份密钥、联系人、历史、收到的文件），默认 ~/.howchat",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=41235, help="监听端口，默认 41235")
    parser.add_argument(
        "--no-bluetooth",
        action="store_true",
        help="禁用蓝牙传输（无 pybluez 时自动禁用）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).expanduser()
    identity = identity_mod.load_or_create(data_dir)
    store = Store(data_dir / "store")
    router = Router(identity.peer_id)
    transport = LANTransport(identity, router, host=args.host, tcp_port=args.port)
    extra = []
    if not args.no_bluetooth:
        extra.append(BluetoothTransport(identity, router))
    core = Core(identity, store, router, transport, extra_transports=extra)
    core.data_dir = data_dir
    HowchatApp(core).run()


if __name__ == "__main__":
    main()
