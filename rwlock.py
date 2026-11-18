# rwlock.py
# 읽기/쓰기 락 (Readers-Writer Lock)
#
# 규칙
#   - 읽기(SELECT)끼리는 동시에 여러 개 가능  → 서로 데이터를 안 바꾸니까 안전
#   - 쓰기(INSERT)는 혼자만                   → 쓰는 동안 아무도 읽거나 쓰지 못함
#
# 왜 필요한가 (락이 없으면 생기는 일)
#   - 두 INSERT가 동시에 next_id를 읽음 → 둘 다 id 5를 받음 → id 중복
#   - INSERT가 B+ 트리 노드를 나누는 중에 SELECT가 트리를 따라 내려감 → 잘못된 위치로 감
#
# 쓰기 우선: 쓰기가 기다리고 있으면 새 읽기는 들어오지 못하게 함
#   → 읽기가 끝없이 들어와서 쓰기가 영원히 못 들어가는 일(기아)을 막음

import threading
from contextlib import contextmanager


class RWLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0               # 지금 읽고 있는 수
        self._writer = False            # 지금 쓰고 있는지
        self._writers_waiting = 0       # 쓰려고 기다리는 수

    def acquire_read(self):
        with self._cond:
            while self._writer or self._writers_waiting > 0:
                self._cond.wait()
            self._readers += 1

    def release_read(self):
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()     # 기다리던 쓰기를 깨움

    def acquire_write(self):
        with self._cond:
            self._writers_waiting += 1
            while self._writer or self._readers > 0:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer = True

    def release_write(self):
        with self._cond:
            self._writer = False
            self._cond.notify_all()         # 기다리던 읽기·쓰기를 모두 깨움

    @contextmanager
    def read(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()
