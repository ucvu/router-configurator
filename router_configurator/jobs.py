from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from .config import Config
from .lists import parse_routing_list
from .router import RouterClient, RouterError
from .storage import Store

logger = logging.getLogger(__name__)


class ServiceLock:
    """Hold an OS lock for the worker lifetime; a second process must fail at startup."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.stream = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            raise RuntimeError("Другой процесс уже обрабатывает этот каталог данных.") from None
        self.stream = stream

    def release(self) -> None:
        if self.stream is not None:
            if os.name == "nt":
                import msvcrt
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()
            self.stream = None


class JobWorker:
    def __init__(self, config: Config, store: Store, client_factory=RouterClient) -> None:
        self.config = config
        self.store = store
        self.client_factory = client_factory
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lifecycle = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._remaining_workers = 0
        self._lock = ServiceLock(config.data_dir / "worker.lock")

    def start(self) -> None:
        if any(thread.is_alive() for thread in self._threads):
            return
        self._lock.acquire()
        try:
            self.store.initialize()
            self.store.interrupt_running()
            self._stop.clear()
            self._threads = [
                threading.Thread(target=self._run, name=f"router-update-worker-{number + 1}", daemon=True)
                for number in range(self.config.max_parallel_jobs)
            ]
            self._remaining_workers = len(self._threads)
            for thread in self._threads:
                thread.start()
        except BaseException:
            self.stop()
            raise

    def notify(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 885) -> None:
        # Once this lock is released the worker cannot claim another job.
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
        deadline = time.monotonic() + max(0, timeout)
        for thread in self._threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self._threads):
            logger.warning("Завершение активных задач не подтверждено; они будут отмечены прерванными при следующем запуске.")
            return  # Keep the OS lock until every active worker has actually exited.
        self._lock.release()

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._wake.clear()
                with self._lifecycle:
                    if self._stop.is_set():
                        return
                    job = self.store.claim_next()
                if job is None:
                    self._wake.wait(0.5)
                    continue
                self._execute(job)
                self.notify()  # Wake workers waiting on a busy router, including after a failure.
        except Exception:
            # Failure of the queue itself must restart the service, not leave a dead worker accepting jobs.
            logger.critical("Обработчик очереди остановился из-за ошибки хранилища.")
            os._exit(1)
        finally:
            with self._lifecycle:
                self._remaining_workers -= 1
                if self._remaining_workers == 0:
                    self._lock.release()

    def _execute(self, job: dict) -> None:
        client = None

        def emit(message: str) -> None:
            self.store.event(job["id"], self.config.redact(message))

        try:
            routing = parse_routing_list(job["snapshot"])
            client = self.client_factory(
                job["hostname"], self.config.router_username, self.config.router_password, emit=emit,
            )
            client.update(routing)
        except RouterError as exc:
            self.store.finish(job["id"], self.config.redact(str(exc)))
        except Exception:
            # No traceback/response dumps: unexpected exceptions may embed router credentials.
            self.store.finish(job["id"], "Внутренняя ошибка выполнения задачи.")
        else:
            self.store.finish(job["id"])
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    logger.warning("Не удалось закрыть сессию роутера.")
