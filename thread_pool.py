# thread_pool.py
# 스레드 풀 (Thread Pool)
#
#   요청 ──▶ [작업 큐] ──▶ 워커 1 ─┐
#   요청 ──▶   (줄 서기)  ──▶ 워커 2  ├─ 미리 만들어 둔 스레드들이 큐에서 하나씩 꺼내 처리
#   요청 ──▶            ──▶ 워커 3 ─┘
#
# 요청마다 스레드를 새로 만들지 않는 이유
#   - 스레드를 만들고 없애는 데도 비용이 듦 → 미리 만들어 두고 재사용
#   - 요청이 1만 개 몰리면 스레드 1만 개 → 메모리 부족, 서로 CPU를 뺏느라 전부 느려짐
#     → 워커 수를 고정하면 동시에 처리하는 양이 일정하게 유지됨
#   - 큐에도 최대 크기를 둠 → 감당 못 할 만큼 몰리면 줄 세우지 않고 바로 거절(PoolFull)

import queue
import threading
import time
from concurrent.futures import Future

_STOP = object()          # 워커에게 "그만" 신호


class PoolFull(Exception):
    """작업 큐가 가득 차서 더 받을 수 없음"""


class ThreadPool:
    def __init__(self, workers=8, queue_size=100, name="worker"):
        if workers < 1:
            raise ValueError("워커는 1개 이상이어야 해요")
        self.max_workers = workers
        self.queue_size = queue_size
        self._tasks = queue.Queue(maxsize=queue_size)
        self._stats_lock = threading.Lock()
        self._status = {}                      # 워커 이름 → 지금 하는 일
        self._local = threading.local()        # 워커마다 따로 가지는 값 (지금 작업의 대기 시간 등)
        self.completed = 0
        self.rejected = 0
        self._shutdown = False
        self._threads = []
        for i in range(1, workers + 1):
            t = threading.Thread(target=self._worker, name=f"{name}-{i}", daemon=True)
            self._status[t.name] = None
            t.start()
            self._threads.append(t)

    # ── 일 맡기기 ──

    def submit(self, fn, *args, info=""):
        """작업을 큐에 넣고 Future(나중에 결과를 받을 표)를 돌려줌. 큐가 가득 차면 PoolFull"""
        if self._shutdown:
            raise RuntimeError("스레드 풀이 종료됐어요")
        future = Future()
        try:
            self._tasks.put_nowait((fn, args, future, info, time.perf_counter()))
        except queue.Full:
            with self._stats_lock:
                self.rejected += 1
            raise PoolFull(f"작업 큐가 가득 찼어요 ({self.queue_size}개)")
        return future

    # ── 워커: 큐에서 꺼내서 처리하기를 반복 ──

    def _worker(self):
        name = threading.current_thread().name
        while True:
            item = self._tasks.get()               # 큐가 비어 있으면 여기서 잠들어 기다림
            if item is _STOP:
                self._tasks.task_done()
                return
            fn, args, future, info, submitted = item
            started = time.perf_counter()
            self._local.wait_ms = round((started - submitted) * 1000, 3)   # 큐에서 기다린 시간
            with self._stats_lock:
                self._status[name] = {"info": info, "since": time.time()}
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        future.set_result(fn(*args))
                    except BaseException as e:          # 작업이 실패해도 워커는 죽지 않고 다음 일을 함
                        future.set_exception(e)
            finally:
                with self._stats_lock:
                    self._status[name] = None
                    self.completed += 1
                self._tasks.task_done()

    # ── 작업 안에서 쓰는 도우미 ──

    def set_info(self, info):
        """지금 이 워커가 하는 일 설명을 바꿈 (예: 실행 중인 SQL) → 상태 화면에 표시"""
        name = threading.current_thread().name
        with self._stats_lock:
            if self._status.get(name) is not None:
                self._status[name]["info"] = info

    def current_wait_ms(self):
        return getattr(self._local, "wait_ms", 0.0)

    # ── 상태 ──

    def stats(self):
        now = time.time()
        with self._stats_lock:
            workers = [{"name": n,
                        "busy": s is not None,
                        "info": s["info"] if s else "",
                        "busy_ms": round((now - s["since"]) * 1000) if s else 0}
                       for n, s in self._status.items()]
            return {"workers": workers,
                    "max_workers": self.max_workers,
                    "busy": sum(1 for w in workers if w["busy"]),
                    "queued": self._tasks.qsize(),
                    "queue_size": self.queue_size,
                    "completed": self.completed,
                    "rejected": self.rejected}

    # ── 종료 ──

    def shutdown(self, wait=True):
        """새 작업은 안 받고, 이미 큐에 있는 작업은 끝까지 처리한 뒤 워커를 멈춤"""
        if self._shutdown:
            return
        self._shutdown = True
        for _ in self._threads:
            self._tasks.put(_STOP)                 # 큐 맨 뒤에 넣음 → 앞의 작업이 먼저 처리됨
        if wait:
            for t in self._threads:
                t.join()
