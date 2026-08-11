# howchat

离线去中心化的端到端加密即时通讯工具（TUI，中文界面）。

- **离线**：完全不需要互联网，走局域网（UDP 广播发现 + TCP 消息）。
- **去中心化**：没有服务器，每个节点身份对等。
- **端到端加密**：X25519 密钥交换 + AES-256-GCM 加密 + Ed25519 签名，中继节点只能转发、无法读取内容。
- **中继跳转**：可通过中间用户作为跳板，向未直连的目标发送消息。
- **文字 + 文件**：私聊、公共群聊，支持分块传输文件。
- **支持 Windows / Linux**，局域网 + 蓝牙双传输、中继跳转。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -U pip
.venv/bin/pip install -e .
```

> 国内网络可先配置镜像源：`~/.pip/pip.conf`
> ```
> [global]
> index-url = https://pypi.tuna.tsinghua.edu.cn/simple
> trusted-host = pypi.tuna.tsinghua.edu.cn
> ```

## 运行

```bash
.venv/bin/howchat                      # 默认监听 0.0.0.0:41235
.venv/bin/howchat --port 5000          # 自定义端口
.venv/bin/howchat --data-dir ~/chat    # 自定义数据目录（身份密钥、联系人、历史、收到的文件）
```

同一局域网内多台机器运行后会自动发现并连接（默认每 5 秒 UDP 广播一次）。若未自动发现，可用 `/connect` 手动连接。

## 命令

| 命令 | 说明 |
| --- | --- |
| `/connect <IP[:端口]>` | 直连指定节点（默认端口 41235） |
| `/peers` | 查看在线用户 |
| `/msg <昵称或ID>` | 切换到私聊会话 |
| `/join <#频道> [成员ID...]` | 加入/创建群聊（不带成员时加入当前在线用户） |
| `/leave <#频道>` | 离开群聊 |
| `/sendfile <目标> <文件路径>` | 发送文件（支持中继转发） |
| `/hop <目标ID> <跳板IP[:端口]>` | 通过跳板节点连接目标 |
| `/nick <新昵称>` | 修改自己的昵称 |
| `/finger <目标>` | 查看对端公钥指纹（用于身份校验） |
| `/whoami` | 查看自己的 ID 与指纹 |
| `/help` | 显示帮助 |
| `/quit` | 退出 |

普通输入视为向当前会话发送消息。左侧选择会话（联系人 / 群聊），群聊消息会对每个成员单独端到端加密。

## 安全模型

- 每个节点持久化身份密钥对（Ed25519 签名 + X25519 加密），存放于数据目录 `identity.json`。
- 私聊时双方通过 X25519 派生共享密钥，用 AES-256-GCM 加密载荷，Ed25519 对内容签名，防止篡改与重放（按发送方序号去重）。
- **中继只转发**：信封只含路由头（源/目标/下一跳/ttl），内容密文中继无法解密。
- 公钥在直连握手时交换，也会经中继同步给其联系人，用于首次经中继发消息。
- **建议**：首次建立关系时用 `/finger` 线下比对指纹，确认对端身份（防中间人）。

## 中继转发示例

```text
  A ────> 中继R ────> B      （A 与 B 无法直连，但都能连到 R）

在 A 上：
  /connect <R 的 IP>           # 连接跳板
  /hop <B 的 ID> <R 的 IP>     # 通过 R 发现到 B 的路由
  /msg <B 的 ID>               # 切换到与 B 的私聊
  输入消息即可                   # 消息经 R 转发，全程端到端加密
```

## 数据目录结构

```
~/.howchat/
├── identity.json          # 身份密钥（勿外泄）
├── contacts/<id>.json     # 联系人昵称与公钥
├── history/<会话>.json    # 聊天历史
├── queued.json            # 离线待发消息（加密）
├── files/                 # 收到的文件
└── channels.json          # 群聊频道与成员
```

## 架构

```
howchat/
├── identity.py     # 身份生成 / 持久化 / 指纹
├── crypto.py       # X25519 + AES-GCM + Ed25519
├── protocol.py     # 信封编解码、分帧、签名
├── routing.py      # 路由表、中继转发、路由发现、防重放
├── transport/
│   ├── __init__.py # 传输抽象基类
│   ├── lan.py      # UDP 发现 + TCP 消息
│   └── bluetooth.py# RFCOMM 蓝牙传输（pybluez / pybluez2）
├── store.py        # 联系人 / 群组 / 离线队列 / 历史 / 文件
├── core.py         # 会话逻辑：加解密收发、群聊、文件分块
└── tui/app.py      # textual 界面 + 命令
```

## 蓝牙传输

howchat 已内置蓝牙传输（`transport/bluetooth.py`），与局域网传输同时运行，消息自动选择可达链路。

- Linux：安装 `pybluez`（需要 BlueZ 与系统蓝牙库）
- Windows：安装 `pybluez2`

```bash
pip install -e '.[bluetooth]'
```

启动后默认自动启用蓝牙（无 pybluez 时自动跳过），可用 `--no-bluetooth` 禁用。

TUI 命令：

```
/bt scan                扫描附近运行 howchat 的蓝牙设备（约 4 秒）
/bt connect <MAC[:频道]> 手动连接蓝牙设备（默认频道 4）
/bt peers               查看当前蓝牙邻居
```

蓝牙与局域网互不干扰：`transport_for` 按传输注册顺序优先返回局域网，只有局域网不可达时才回退到蓝牙；同一邻居经任一链路可达即视为在线。路由发现/中继请求会同时发往两个传输的所有邻居。

## 测试

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest tests -q
```

覆盖：加密/签名、信封篡改与重放检测、路由发现与中继、UDP 发现 + TCP 传输、
双节点文字/文件端到端、三节点中继、群聊、存储持久化、TUI 启动与命令。
