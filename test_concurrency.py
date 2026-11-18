# test_concurrency.py — [Phase 3] 스레드 풀, 읽기/쓰기 락, 엔진 동시성 단위 테스트
# 실행: python -m unittest -v

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from executor import Database
from rwlock import RWLock
from thread_pool import PoolFull, ThreadPool


class ThreadPoolTest(unittest.TestCase):
    def test_results_returned(self):
        pool = ThreadPool(4)
        futures = [pool.submit(lambda x: x * x, i) for i in range(20)]
        self.assertEqual([f.result() for f in futures], [i * i for i in range(20)])
        pool.shutdown()

    def test_at_most_n_workers_at_once(self):
        pool = ThreadPool(3)
        lock, state = threading.Lock(), {"now": 0, "peak": 0}

        def job():
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time.sleep(0.03)
            with lock:
                state["now"] -= 1

        for f in [pool.submit(job) for _ in range(12)]:
            f.result()
        pool.shutdown()
        self.assertEqual(state["peak"], 3)         # 워커 수보다 많이 동시에 돌지 않음

    def test_work_spread_over_workers(self):
        pool = ThreadPool(4)
        names = {f.result() for f in [pool.submit(lambda: (time.sleep(0.02), threading.current_thread().name)[1])
                                      for _ in range(8)]}
        pool.shutdown()
        self.assertEqual(len(names), 4)

    def test_exception_does_not_kill_worker(self):
        pool = ThreadPool(1)
        with self.assertRaises(ZeroDivisionError):
            pool.submit(lambda: 1 / 0).result()
        self.assertEqual(pool.submit(lambda: "살아 있음").result(), "살아 있음")
        pool.shutdown()

    def test_full_queue_rejects(self):
        pool = ThreadPool(1, queue_size=1)
        started = threading.Event()
        release = threading.Event()
        pool.submit(lambda: (started.set(), release.wait()))
        started.wait()                              # 워커 하나가 바쁨
        pool.submit(lambda: None)                   # 큐 1칸 채움
        with self.assertRaises(PoolFull):
            pool.submit(lambda: None)               # 더는 못 받음
        self.assertEqual(pool.stats()["rejected"], 1)
        release.set()
        pool.shutdown()

    def test_shutdown_finishes_queued_work(self):
        pool = ThreadPool(1)
        done = []
        for i in range(5):
            pool.submit(lambda i=i: (time.sleep(0.01), done.append(i)))
        pool.shutdown(wait=True)
        self.assertEqual(done, [0, 1, 2, 3, 4])     # 큐에 있던 것까지 순서대로 끝냄
        with self.assertRaises(RuntimeError):
            pool.submit(lambda: None)               # 종료 후엔 안 받음

    def test_stats(self):
        pool = ThreadPool(2, queue_size=5)
        release = threading.Event()
        f = pool.submit(release.wait, info="느린 작업")
        time.sleep(0.05)
        s = pool.stats()
        self.assertEqual((s["max_workers"], s["busy"], s["queue_size"]), (2, 1, 5))
        self.assertIn("느린 작업", [w["info"] for w in s["workers"]])
        release.set()
        f.result()
        pool.shutdown()

    def test_invalid_worker_count(self):
        with self.assertRaises(ValueError):
            ThreadPool(0)


class RWLockTest(unittest.TestCase):
    def test_readers_share(self):
        lock, inside, peak = RWLock(), [0], [0]
        guard = threading.Lock()

        def reader():
            with lock.read():
                with guard:
                    inside[0] += 1
                    peak[0] = max(peak[0], inside[0])
                time.sleep(0.05)
                with guard:
                    inside[0] -= 1

        threads = [threading.Thread(target=reader) for _ in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertGreater(peak[0], 1)              # 읽기는 동시에 여럿

    def test_writer_exclusive(self):
        lock, log = RWLock(), []

        def writer(n):
            with lock.write():
                log.append(("start", n))
                time.sleep(0.01)
                log.append(("end", n))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(5)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        for i in range(0, len(log), 2):             # 쓰기는 항상 start → end 가 붙어 있음 (겹치지 않음)
            self.assertEqual((log[i][0], log[i + 1][0], log[i][1]), ("start", "end", log[i + 1][1]))

    def test_writer_waits_for_reader(self):
        lock, order = RWLock(), []
        lock.acquire_read()
        t = threading.Thread(target=lambda: (lock.acquire_write(), order.append("write"), lock.release_write()))
        t.start()
        time.sleep(0.05)
        order.append("read done")
        lock.release_read()
        t.join()
        self.assertEqual(order, ["read done", "write"])

    def test_waiting_writer_blocks_new_readers(self):
        lock, order = RWLock(), []
        lock.acquire_read()
        w = threading.Thread(target=lambda: (lock.acquire_write(), order.append("write"), lock.release_write()))
        w.start()
        time.sleep(0.05)                            # 쓰기가 기다리는 중
        r = threading.Thread(target=lambda: (lock.acquire_read(), order.append("new read"), lock.release_read()))
        r.start()
        time.sleep(0.05)
        lock.release_read()
        w.join()
        r.join()
        self.assertEqual(order, ["write", "new read"])   # 쓰기 우선 (기아 방지)


class EngineConcurrencyTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        with open(os.path.join(self.dir, "schema.json"), "w", encoding="utf-8") as f:
            json.dump({"users": {"name": "TEXT", "age": "INT"}, "products": {"name": "TEXT", "price": "INT"}}, f)
        self.db = Database(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_concurrent_inserts_unique_ids(self):
        errors = []

        def writer(n):
            for i in range(50):
                r = self.db.execute(f"INSERT INTO users VALUES ('t{n}-{i}', {i})")[0]
                if r.kind != "insert":
                    errors.append(r.message)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(10)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        ids = [r[0] for r in self.db.execute("SELECT id FROM users")[0].rows]
        self.assertEqual(errors, [])
        self.assertEqual(sorted(ids), list(range(1, 501)))          # 500개, 1~500, 중복·빠짐 없음
        self.assertEqual(len(Database(self.dir).tables["users"].index), 500)   # 파일도 정확함

    def test_reads_during_writes_always_consistent(self):
        stop, bad = threading.Event(), []

        def writer():
            for i in range(300):
                self.db.execute(f"INSERT INTO users VALUES ('w{i}', {i})")
            stop.set()

        def reader():
            while not stop.is_set():
                r = self.db.execute("SELECT id FROM users WHERE id >= 1")[0]
                ids = [row[0] for row in r.rows]
                if r.kind != "select" or ids != list(range(1, len(ids) + 1)):
                    bad.append(r)                   # 중간에 빠진 id가 있으면 안 됨

        threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(bad, [])

    def test_different_tables_do_not_block_each_other(self):
        lock = self.db.locks["users"]
        lock.acquire_write()                        # users 쓰기를 붙잡고 있어도
        try:
            r = self.db.execute("INSERT INTO products VALUES ('키보드', 1)")[0]   # products는 바로 됨
            self.assertEqual(r.kind, "insert")
        finally:
            lock.release_write()


if __name__ == "__main__":
    unittest.main(verbosity=2)
