"""Decoder for Vectra v3 binary snapshots (``structure.vg``).

Wire format (little-endian), mirror of ``vectra/internal/store``::

    "VCTR" | u16 version(3) | u32 node_count | u32 edge_count
      | node{ key, label, props }* | edge{ srcKey, dstKey, label, props }*
      | u32 indexKey_count | key string* | u32 CRC32(IEEE)

``string`` is ``u32 len + utf-8 bytes``; ``props`` is ``u32 len + JSON`` with
length 0 meaning nil (decoded as an empty mapping). The CRC is IEEE
(``zlib.crc32``), not the Castagnoli variant the WAL uses, and covers every
byte before the 4-byte trailer.

Tier 0 is read-only: this module never writes, seals or mutates a snapshot.
"""
from __future__ import annotations

import json
import struct
import zlib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field

from .base import GraphEdge, GraphNode, SnapshotError

__all__ = ["SnapshotData", "SnapshotError", "decode_snapshot"]

_MAGIC = b"VCTR"
_VERSION = 3
_HEADER_SIZE = 6  # magic(4) + version(2)
_U32 = struct.Struct("<I")
_U16 = struct.Struct("<H")


class _LazyProps(Mapping):
    """Read-only mapping over snapshot props, JSON-decoded on first access.

    Decoding every node's props eagerly costs roughly 40% of total load time
    on real repositories, while callers typically touch a handful of nodes.
    """

    __slots__ = ("_raw", "_loaded")

    def __init__(self, raw: bytes | None) -> None:
        # raw None mirrors the writer's nil props
        self._raw = raw
        self._loaded: dict | None = None

    def _materialize(self) -> dict:
        if self._loaded is None:
            self._loaded = json.loads(self._raw) if self._raw else {}
        return self._loaded

    def __getitem__(self, key: str) -> object:
        return self._materialize()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._materialize())

    def __len__(self) -> int:
        return len(self._materialize())

    def __repr__(self) -> str:
        return f"_LazyProps({self._materialize()!r})"


class _Reader:
    """Bounds-checked cursor over the snapshot body."""

    __slots__ = ("b", "i")

    def __init__(self, body: bytes) -> None:
        self.b = body
        self.i = 0

    def take(self, n: int) -> bytes:
        """Return the next ``n`` bytes, refusing to read past the end."""
        if n < 0 or self.i + n > len(self.b):
            raise SnapshotError("snapshot truncated")
        p = self.b[self.i : self.i + n]
        self.i += n
        return p

    def u32(self) -> int:
        return _U32.unpack(self.take(4))[0]

    def string(self) -> str:
        return self.take(self.u32()).decode("utf-8")

    def props(self) -> _LazyProps:
        n = self.u32()
        return _LazyProps(None if n == 0 else self.take(n))


@dataclass(frozen=True)
class SnapshotData:
    """Decoded content of one snapshot file.

    Attributes
    ----------
    nodes:
        Every node, in file order.
    edges:
        Every directed edge, in file order.
    index_keys:
        Property names the writer declared an index for, in file order.
        Returned as a ``list`` (unlike :class:`GraphStats.index_keys`) because
        the snapshot section is positional and callers re-register it as-is.
    """

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    index_keys: list[str] = field(default_factory=list)


def decode_snapshot(buf: bytes) -> SnapshotData:
    """Decode Vectra v3 snapshot bytes.

    Parameters
    ----------
    buf:
        Raw ``structure.vg`` content.

    Returns
    -------
    SnapshotData
        Nodes, edges and declared index keys. Node/edge props are lazy: they
        are JSON-decoded on first read.

    Raises
    ------
    SnapshotError
        On bad magic, an unsupported version, a CRC mismatch, or a
        truncated / malformed payload. Callers must catch it and degrade to
        an empty graph.
    """
    if len(buf) < _HEADER_SIZE + 4:
        raise SnapshotError("snapshot truncated")
    if buf[:4] != _MAGIC:
        raise SnapshotError("bad snapshot magic")
    version = _U16.unpack(buf[4:6])[0]
    if version != _VERSION:
        # Porto narrows to v3 on purpose: SCV only emits v3, and every extra
        # format costs another test suite.
        raise SnapshotError(f"unsupported snapshot version {version}")

    body, trailer = buf[:-4], buf[-4:]
    rd = _Reader(body[_HEADER_SIZE:])
    try:
        node_count, edge_count = rd.u32(), rd.u32()
        # Guard against forged counts: each record costs a known minimum of bytes,
        # so an out-of-range count is truncation, not a long empty walk.
        if node_count * 12 + edge_count * 16 > len(rd.b):
            raise SnapshotError("snapshot truncated")

        nodes = [
            GraphNode(key=rd.string(), label=rd.string(), props=rd.props())
            for _ in range(node_count)
        ]
        edges = [
            GraphEdge(src=rd.string(), dst=rd.string(), label=rd.string(), props=rd.props())
            for _ in range(edge_count)
        ]

        key_count = rd.u32()
        if key_count * 4 > len(rd.b) - rd.i:
            raise SnapshotError("snapshot truncated")
        index_keys = [rd.string() for _ in range(key_count)]
    except ValueError as exc:  # bad utf-8 / json / struct input -> SnapshotError
        raise SnapshotError("malformed snapshot") from exc

    if rd.i != len(rd.b):
        raise SnapshotError("trailing garbage in snapshot")
    # CRC runs after parsing: a truncated file must report truncated (diagnosable),
    # not a generic crc mismatch.
    if zlib.crc32(body) != _U32.unpack(trailer)[0]:
        raise SnapshotError("snapshot crc mismatch")
    return SnapshotData(nodes=nodes, edges=edges, index_keys=index_keys)
