"""In-memory B+ tree for the mini DBMS index."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Generic, Iterator, TypeVar, Union

K = TypeVar("K")
V = TypeVar("V")


@dataclass
class _Leaf(Generic[K, V]):
    keys: list[K] = field(default_factory=list)
    values: list[list[V]] = field(default_factory=list)
    next_leaf: _Leaf[K, V] | None = None


@dataclass
class _Internal(Generic[K, V]):
    keys: list[K]
    children: list[_Node[K, V]]


_Node = Union[_Leaf[K, V], _Internal[K, V]]


class BPlusTree(Generic[K, V]):
    """Map ordered keys to values such as row positions. Keys may repeat."""

    def __init__(self, max_keys: int = 31) -> None:
        if not isinstance(max_keys, int) or max_keys < 2:
            raise ValueError("max_keys must be an integer of at least 2")
        self._max_keys = max_keys
        self._root: _Node[K, V] = _Leaf()

    def insert(self, key: K, value: V) -> None:
        """Insert a key and value, keeping previous values for the same key."""
        split = self._insert(self._root, key, value)
        if split is not None:
            separator, right = split
            self._root = _Internal(keys=[separator], children=[self._root, right])

    def search(self, key: K) -> list[V]:
        """Return all values for key, or an empty list if it is absent."""
        leaf = self._find_leaf(key)
        index = bisect_left(leaf.keys, key)
        if index < len(leaf.keys) and leaf.keys[index] == key:
            return leaf.values[index].copy()
        return []

    def range_search(self, start: K, end: K) -> Iterator[tuple[K, V]]:
        """Yield key-value pairs in key order, including both endpoints."""
        leaf: _Leaf[K, V] | None = self._find_leaf(start)
        index = bisect_left(leaf.keys, start)
        while leaf is not None:
            stop = bisect_right(leaf.keys, end, lo=index)
            for position in range(index, stop):
                for value in leaf.values[position]:
                    yield leaf.keys[position], value
            if stop < len(leaf.keys):
                return
            leaf = leaf.next_leaf
            index = 0

    def _find_leaf(self, key: K) -> _Leaf[K, V]:
        node = self._root
        while isinstance(node, _Internal):
            # A separator is the smallest key in its right subtree.
            node = node.children[bisect_right(node.keys, key)]
        return node

    def _insert(
        self, node: _Node[K, V], key: K, value: V
    ) -> tuple[K, _Node[K, V]] | None:
        if isinstance(node, _Leaf):
            index = bisect_left(node.keys, key)
            if index < len(node.keys) and node.keys[index] == key:
                node.values[index].append(value)
                return None

            node.keys.insert(index, key)
            node.values.insert(index, [value])
            if len(node.keys) <= self._max_keys:
                return None

            middle = len(node.keys) // 2
            right_leaf = _Leaf(
                keys=node.keys[middle:],
                values=node.values[middle:],
                next_leaf=node.next_leaf,
            )
            del node.keys[middle:]
            del node.values[middle:]
            node.next_leaf = right_leaf
            # Copy the separator up while keeping the entry in the leaf.
            return right_leaf.keys[0], right_leaf

        index = bisect_right(node.keys, key)
        split = self._insert(node.children[index], key, value)
        if split is None:
            return None

        separator, right = split
        node.keys.insert(index, separator)
        node.children.insert(index + 1, right)
        if len(node.keys) <= self._max_keys:
            return None

        middle = len(node.keys) // 2
        separator = node.keys[middle]
        right_internal = _Internal(
            keys=node.keys[middle + 1 :],
            children=node.children[middle + 1 :],
        )
        # Move the middle separator to the parent of the two internal nodes.
        del node.keys[middle:]
        del node.children[middle + 1 :]
        return separator, right_internal
