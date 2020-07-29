from pprint import *
from typing import *
import trio
import bencode
import sqlite3
import logging
import typing
import functools
import sortedcontainers
import secrets
import struct
import socket
from dataclasses import dataclass, field
from typing import Union, Tuple

global_bootstrap_nodes = [
    ("router.utorrent.com", 6881),
    ("router.bittorrent.com", 6881),
    ("dht.transmissionbt.com", 6881)
]


def addr_for_db(addr: Tuple[str, int]) -> str:
    return addr[0] + ":" + str(addr[1])


@dataclass(unsafe_hash=True)
class PeerId:
    bytes: bytes

    def __lt__(self, other):
        if other is None:
            return False
        return self.bytes < other.bytes

    def __repr__(self):
        return self.bytes.hex()


@dataclass(unsafe_hash=True, order=True)
class NodeInfo:
    id: PeerId
    addr: Tuple[str, int]


class Bootstrap:
    active: Dict[bytes, None] = {}
    alpha: int = 3
    target: PeerId

    def __init__(self, socket, target: PeerId, nursery):
        self.socket = socket
        self.target = target
        self.backlog: Set[NodeInfo] = sortedcontainers.SortedSet()
        self.exhausted = trio.Event()
        self.nursery = nursery

    def new_transaction_id(self) -> bytes:
        return secrets.token_bytes(8)

    def add_candidate(self, addr):
        self.backlog.add(addr)

    def add_candidates(self, *addrs):
        self.backlog.update([NodeInfo(None, addr) for addr in addrs])
        self.try_do_sends()

    def on_reply(self, reply, src):
        msg: bencode.Dict = bencode.parse_one_from_bytes(reply)
        logging.debug("got reply:\n%s", pformat(msg))
        key = msg[b"t"]
        if key not in (active := self.active):
            logging.warning("got unexpected reply: %r", key)
            return
        del active[key]
        logging.debug("got reply from %s at %s", msg[b"r"][b"id"].hex(), src)
        for (id, packed_ip, port) in struct.iter_unpack("20s4sH", msg[b"r"][b"nodes"]):
            self.backlog.add(NodeInfo(PeerId(id), (socket.inet_ntoa(packed_ip), port),))
        self.try_do_sends()

    def start_query(self, addr, q, a=None, callback=None):
        if a is None:
            a = {}
        a["id"] = self.target
        tid = self.new_transaction_id()
        msg = {"t": tid, "y": "q", "q": q, "a": a}
        key = tid
        if key in self.active:
            raise ValueError(key)
        self.active[key] = callback
        self.nursery.start_soon(self.socket.sendto, b"".join(bencode.encode(msg)), addr)

    def find_node(self, addr):
        return self.start_query(addr, "find_node")

    def start_next(self):
        node_info = self.backlog.pop()
        logging.debug("doing find_node on %s", node_info)
        return self.find_node(node_info.addr)

    def try_do_sends(self):
        while len(self.active) < self.alpha and self.backlog:
            self.start_next()


class RoutingTable:
    def __init__(self, root):
        self.buckets = []


@dataclass
class Sender:
    socket: typing.Any
    db_conn: typing.Any

    async def sendto(self, bytes, addr):
        with self.db_conn:
            record_operation(self.db_conn, "send", addr_for_db(addr), bytes)
        try:
            await self.socket.sendto(
                bytes, addr,
            )
        except trio.socket.gaierror as exc:
            logging.warning(f"sending to {addr}: {exc}")
        except Exception:
            logging.exception(f"sending to {addr}")


async def ping_bootstrap_nodes(sender, db_conn):
    for addr in global_bootstrap_nodes:
        bytes = "".join(
            bencode.encode(
                {"t": "aa", "y": "q", "q": "ping", "a": {"id": "abcdefghij0123456789"},}
            )
        ).encode()
        sender.sendto(bytes, addr)


def new_message_id(db_conn: sqlite3.Connection) -> int:
    cursor = db_conn.cursor()
    cursor.execute("insert into messages default values")
    return cursor.lastrowid


def record_operation(db_conn, type: str, remote_addr: str, bytes: bytes):
    with db_conn:
        message_id = new_message_id(db_conn)
        db_conn.execute(
            "insert into operations (message_id, remote_addr, type) values (?, ?, ?)",
            [message_id, remote_addr, type],
        )
        record_packet(bytes, db_conn, message_id)


def record_packet(bytes, db_conn, top_id):
    bencode.StreamDecoder(bencode.BytesStreamReader(bytes)).visit(
        MessageWriter(db_conn.cursor(), top_id)
    )


@dataclass
class FieldContext:
    parent_id: typing.Union[None, int]
    index: int = 0


class MessageWriter:

    cursor: sqlite3.Cursor

    def __init__(self, cursor, top_id):
        self.cursor = cursor
        self.field_contexts = [FieldContext(top_id)]

    def _cur_parent_id(self) -> typing.Union[int, None]:
        return self.field_contexts[-1].parent_id

    def _cur_field_context(self) -> FieldContext:
        return self.field_contexts[-1]

    def _insert_code(self, code):
        self._insert(code, None)

    def _insert(self, code, value):
        parent_id = self._cur_parent_id()
        self.cursor.execute(
            """insert into messages (parent_id, "index", depth, type, value) values (?, ?, ?, ?, ?)""",
            [
                parent_id,
                self._cur_field_context().index,
                self._cur_depth(),
                code,
                value,
            ],
        )
        if parent_id is None:
            self.on_root_insert(self.cursor.lastrowid)
        self._cur_field_context().index += 1

    def _cur_depth(self):
        return len(self.field_contexts) - 1

    def _start(self, code):
        self._insert_code(code)
        self.field_contexts.append(FieldContext(self.cursor.lastrowid))

    def start_dict(self):
        self._start("d")

    def start_list(self):
        self._start("l")

    def end(self):
        self._insert_code("e")
        self.field_contexts.pop()

    def int(self, value):
        self._insert("i", value)

    def str(self, value):
        self._insert("s", value)


async def receiver(socket, db_conn):
    while True:
        bytes, addr = await socket.recvfrom(0x1000)
        record_operation(db_conn, "recv", addr_for_db(addr), bytes)
        yield bytes, addr


async def main():
    logging.basicConfig(level=logging.DEBUG)
    db_conn = sqlite3.connect("herp.db")
    socket = trio.socket.socket(type=trio.socket.SOCK_DGRAM)
    await socket.bind(("", 42069))
    async with trio.open_nursery() as nursery:
        target_id = secrets.token_bytes(20)
        logging.info("bootstrap target id: %s", target_id.hex())
        bootstrap = Bootstrap(Sender(socket, db_conn), target_id, nursery=nursery)
        bootstrap.add_candidates(*global_bootstrap_nodes)
        async for bytes, addr in receiver(socket, db_conn):
            bootstrap.on_reply(bytes, addr)


trio.run(main)
