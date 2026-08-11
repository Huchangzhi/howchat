import abc


class Transport(abc.ABC):
    kind = "base"

    def __init__(self, identity, router):
        self.identity = identity
        self.router = router
        self.on_peer_change = None

    @abc.abstractmethod
    async def start(self):
        pass

    @abc.abstractmethod
    async def stop(self):
        pass

    @abc.abstractmethod
    def neighbors(self):
        pass

    @abc.abstractmethod
    def send_frame(self, peer_id, data):
        pass

    @abc.abstractmethod
    async def connect_host(self, addr, port):
        pass
