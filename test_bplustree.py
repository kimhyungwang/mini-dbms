# test_bplustree.py — B+ 트리 단위 테스트
# 실행: python -m unittest -v

import random
import unittest

from bplustree import BPlusTree


class BPlusTreeTest(unittest.TestCase):
    def test_empty(self):
        t = BPlusTree()
        self.assertEqual(len(t), 0)
        self.assertIsNone(t.search(1))
        self.assertEqual(list(t.range()), [])
        self.assertIsNone(t.max_key())
        self.assertEqual(t.height(), 1)

    def test_insert_and_search(self):
        t = BPlusTree(max_keys=4)
        for k in [5, 1, 9, 3, 7]:
            t.insert(k, k * 10)
        for k in [5, 1, 9, 3, 7]:
            self.assertEqual(t.search(k), k * 10)
        self.assertIsNone(t.search(4))
        self.assertIsNone(t.search(100))

    def test_duplicate_key_replaces_value(self):
        t = BPlusTree()
        t.insert(1, "a")
        t.insert(1, "b")
        self.assertEqual(t.search(1), "b")
        self.assertEqual(len(t), 1)

    def test_many_random_keys_with_small_nodes(self):
        # 노드를 작게(3개) 해서 분할이 아주 많이 일어나게 → 구조 검사
        for max_keys in (3, 4, 5, 8):
            with self.subTest(max_keys=max_keys):
                t = BPlusTree(max_keys)
                keys = list(range(1, 2001))
                random.Random(max_keys).shuffle(keys)
                for k in keys:
                    t.insert(k, -k)
                t.check()
                self.assertEqual(len(t), 2000)
                self.assertTrue(all(t.search(k) == -k for k in range(1, 2001)))

    def test_sequential_keys(self):
        t = BPlusTree(max_keys=4)
        for k in range(1, 1001):            # id처럼 1, 2, 3 … 순서대로
            t.insert(k, k)
        t.check()
        self.assertEqual(t.max_key(), 1000)
        self.assertEqual(t.min_key(), 1)

    def test_height_grows_slowly(self):
        t = BPlusTree(max_keys=128)
        for k in range(1, 100_001):
            t.insert(k, k)
        self.assertLessEqual(t.height(), 4)   # 10만 개도 높이 3~4

    def test_split_creates_new_root(self):
        t = BPlusTree(max_keys=3)
        for k in range(1, 4):
            t.insert(k, k)
        self.assertEqual(t.height(), 1)       # 3개까지는 잎 하나
        t.insert(4, 4)                        # 4개째 → 잎이 나뉘고 루트가 생김
        self.assertEqual(t.height(), 2)
        t.check()

    def test_range_inclusive_exclusive(self):
        t = BPlusTree(max_keys=3)
        for k in range(1, 51):
            t.insert(k, k)
        r = lambda *a: [k for k, _ in t.range(*a)]
        self.assertEqual(r(10, 13), [10, 11, 12, 13])
        self.assertEqual(r(10, 13, False, False), [11, 12])
        self.assertEqual(r(None, 3), [1, 2, 3])
        self.assertEqual(r(48, None), [48, 49, 50])
        self.assertEqual(r(), list(range(1, 51)))
        self.assertEqual(r(60, 70), [])
        self.assertEqual(r(13, 10), [])

    def test_range_across_leaves(self):
        # 범위가 여러 잎에 걸쳐도 잎 연결(next)을 따라 순서대로 나와야 함
        t = BPlusTree(max_keys=3)
        for k in range(100, 0, -1):           # 거꾸로 넣어도
            t.insert(k, str(k))
        self.assertEqual([v for _, v in t.range(20, 40)], [str(k) for k in range(20, 41)])

    def test_range_with_gaps(self):
        t = BPlusTree(max_keys=3)
        for k in range(0, 100, 10):           # 0, 10, 20, …
            t.insert(k, k)
        self.assertEqual([k for k, _ in t.range(15, 45)], [20, 30, 40])

    def test_invalid_max_keys(self):
        with self.assertRaises(ValueError):
            BPlusTree(max_keys=2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
