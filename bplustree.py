# bplustree.py
# 메모리 기반 B+ 트리 (id → 테이블 파일 속 위치)
#
# 구조 (MAX_KEYS = 3 으로 작게 그린 예)
#
#                 [ 30 | 60 ]                 ← 내부 노드: 길잡이 키만 (어느 자식으로 갈지)
#               /      |      \
#     [10 20] → [30 40 50] → [60 70]          ← 잎 노드: 모든 키 + 값(파일 위치), 옆 잎과 연결
#
# 찾기:  루트에서 키를 비교해 자식으로 내려가기를 잎까지 반복 → 잎에서 키를 찾음
# 넣기:  들어갈 잎을 찾아 정렬 위치에 넣음 → 잎이 꽉 차면(MAX_KEYS 초과) 반으로 나눔(split)
#        → 나눈 오른쪽 잎의 첫 키를 부모에 올림 → 부모도 넘치면 또 나눔 → 루트까지 올라가면 높이 +1
# 범위:  시작 키가 있는 잎을 찾은 뒤, 잎끼리 연결된 next를 따라 옆으로 읽음
#
# 노드 하나에 키를 많이(기본 128개) 넣어서 트리가 옆으로 넓고 낮음
#   → 100만 개도 높이 3~4 (루트에서 잎까지 3~4번만 내려가면 찾음)

from bisect import bisect_left, bisect_right

DEFAULT_MAX_KEYS = 128


class Leaf:
    __slots__ = ("keys", "values", "next")

    def __init__(self):
        self.keys = []
        self.values = []
        self.next = None        # 오른쪽 옆 잎 (범위 검색용)


class Internal:
    __slots__ = ("keys", "children")

    def __init__(self):
        self.keys = []          # 길잡이 키
        self.children = []      # 자식 노드 (항상 키 개수 + 1)


class BPlusTree:
    def __init__(self, max_keys=DEFAULT_MAX_KEYS):
        if max_keys < 3:
            raise ValueError("max_keys는 3 이상이어야 해요")
        self.max_keys = max_keys
        self.root = Leaf()
        self.size = 0

    def __len__(self):
        return self.size

    # ── 찾기 ──

    def _find_leaf(self, key):
        """key가 있어야 할 잎까지 내려감. 지나온 내부 노드 목록도 같이 돌려줌(분할할 때 필요)"""
        node, path = self.root, []
        while isinstance(node, Internal):
            path.append(node)
            # 키가 길잡이와 같으면 오른쪽으로 (오른쪽 잎의 첫 키가 길잡이로 올라갔기 때문)
            node = node.children[bisect_right(node.keys, key)]
        return node, path

    def search(self, key):
        """key의 값(파일 위치). 없으면 None"""
        leaf, _ = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            return leaf.values[i]
        return None

    def __contains__(self, key):
        return self.search(key) is not None

    # ── 넣기 ──

    def insert(self, key, value):
        """이미 있는 key면 값만 바꿈"""
        leaf, path = self._find_leaf(key)
        i = bisect_left(leaf.keys, key)
        if i < len(leaf.keys) and leaf.keys[i] == key:
            leaf.values[i] = value
            return
        leaf.keys.insert(i, key)
        leaf.values.insert(i, value)
        self.size += 1
        if len(leaf.keys) > self.max_keys:
            self._split_leaf(leaf, path)

    def _split_leaf(self, leaf, path):
        mid = len(leaf.keys) // 2
        right = Leaf()
        right.keys, leaf.keys = leaf.keys[mid:], leaf.keys[:mid]
        right.values, leaf.values = leaf.values[mid:], leaf.values[:mid]
        right.next, leaf.next = leaf.next, right          # 잎 연결 유지
        self._insert_into_parent(leaf, right.keys[0], right, path)   # 오른쪽 첫 키를 길잡이로 올림

    def _insert_into_parent(self, left, key, right, path):
        if not path:                                      # 루트가 나뉨 → 새 루트 (높이 +1)
            root = Internal()
            root.keys = [key]
            root.children = [left, right]
            self.root = root
            return
        parent = path.pop()
        i = bisect_right(parent.keys, key)
        parent.keys.insert(i, key)
        parent.children.insert(i + 1, right)
        if len(parent.keys) > self.max_keys:
            self._split_internal(parent, path)

    def _split_internal(self, node, path):
        mid = len(node.keys) // 2
        up_key = node.keys[mid]                           # 가운데 키는 위로 올라가고, 양쪽엔 안 남음
        right = Internal()
        right.keys, node.keys = node.keys[mid + 1:], node.keys[:mid]
        right.children, node.children = node.children[mid + 1:], node.children[:mid + 1]
        self._insert_into_parent(node, up_key, right, path)

    # ── 범위 ──

    def range(self, lo=None, hi=None, lo_inclusive=True, hi_inclusive=True):
        """lo ~ hi 사이의 (키, 값)을 키 순서대로. None이면 끝까지"""
        if lo is None:
            node = self.root
            while isinstance(node, Internal):
                node = node.children[0]                   # 맨 왼쪽 잎
            leaf, i = node, 0
        else:
            leaf, _ = self._find_leaf(lo)
            i = bisect_left(leaf.keys, lo) if lo_inclusive else bisect_right(leaf.keys, lo)
        while leaf is not None:
            while i < len(leaf.keys):
                k = leaf.keys[i]
                if hi is not None and (k > hi or (k == hi and not hi_inclusive)):
                    return
                yield k, leaf.values[i]
                i += 1
            leaf, i = leaf.next, 0                        # 옆 잎으로

    # ── 정보 (화면 표시, 테스트용) ──

    def height(self):
        h, node = 1, self.root
        while isinstance(node, Internal):
            node = node.children[0]
            h += 1
        return h

    def stats(self):
        leaves = internals = 0
        stack = [self.root]
        while stack:
            node = stack.pop()
            if isinstance(node, Internal):
                internals += 1
                stack.extend(node.children)
            else:
                leaves += 1
        return {"keys": self.size, "height": self.height(), "leaves": leaves,
                "internal_nodes": internals, "max_keys": self.max_keys}

    def min_key(self):
        for k, _ in self.range():
            return k
        return None

    def max_key(self):
        node = self.root
        while isinstance(node, Internal):
            node = node.children[-1]
        return node.keys[-1] if node.keys else None

    def check(self):
        """구조가 올바른지 검사 (테스트용). 문제가 있으면 AssertionError"""
        leaf_depths = set()

        def walk(node, lo, hi, depth):
            assert node.keys == sorted(node.keys), "노드 안의 키가 정렬돼 있지 않음"
            assert len(node.keys) <= self.max_keys, "노드가 MAX_KEYS를 넘음"
            for k in node.keys:
                assert (lo is None or k >= lo) and (hi is None or k < hi), "키가 범위를 벗어남"
            if isinstance(node, Leaf):
                assert len(node.keys) == len(node.values)
                leaf_depths.add(depth)
                return
            assert len(node.children) == len(node.keys) + 1, "자식 수 = 키 수 + 1 이어야 함"
            bounds = [lo] + node.keys + [hi]
            for i, child in enumerate(node.children):
                walk(child, bounds[i], bounds[i + 1], depth + 1)

        walk(self.root, None, None, 1)
        assert len(leaf_depths) <= 1, "모든 잎의 깊이가 같아야 함"
        keys = [k for k, _ in self.range()]
        assert keys == sorted(keys) and len(keys) == self.size, "잎 연결 순서가 틀림"
