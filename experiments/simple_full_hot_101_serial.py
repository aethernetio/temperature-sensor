"""Single-owner ESP32 serial capture for simple_full_hot_101 runner."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Protocol


class SerialBackend(Protocol):
    def open(self, port: str) -> object: ...
    def close(self, handle: object) -> None: ...
    def read(self, handle: object, size: int) -> bytes: ...


def default_serial_backend():
    import serial

    class _Backend:
        def open(self, port: str):
            return serial.Serial(
                port,
                115200,
                timeout=0.4,
                dsrdtr=False,
                rtscts=False,
            )

        def close(self, handle) -> None:
            try:
                handle.close()
            except Exception:
                pass

        def read(self, handle, size: int) -> bytes:
            return handle.read(size)

    return _Backend()


def discover_espressif_port(
    wait_fn: Callable[[float], str | None] | None = None,
    *,
    timeout_s: float = 45.0,
) -> str | None:
    if wait_fn is None:
        import run_thermometer2_sleep_bisect as sb

        wait_fn = sb.wait_espressif_com
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        port = wait_fn(min(5.0, max(0.5, deadline - time.time())))
        if port:
            return port
        time.sleep(0.5)
    return None


class SerialCapture:
    """One open handle at a time; rediscover COM after disconnect."""

    def __init__(
        self,
        log_path: Path,
        *,
        backend: SerialBackend | None = None,
        port_hint: str | None = None,
        discover: Callable[[], str | None] | None = None,
    ) -> None:
        self.log_path = log_path
        self._backend = backend or default_serial_backend()
        self._port_hint = port_hint
        self._discover = discover
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle = None
        self._handle_lock = threading.Lock()

    @property
    def handle_open(self) -> bool:
        with self._handle_lock:
            return self._handle is not None

    def start(self, port: str | None = None) -> None:
        if port:
            self._port_hint = port
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text("", encoding="utf-8")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._handle_lock:
            if self._handle is not None:
                self._backend.close(self._handle)
                self._handle = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def _resolve_port(self) -> str | None:
        if self._port_hint:
            return self._port_hint
        if self._discover is not None:
            return self._discover()
        return discover_espressif_port()

    def _run(self) -> None:
        open_deadline = time.time() + 45.0
        with self.log_path.open("a", encoding="utf-8", errors="replace") as log:
            while not self._stop.is_set() and time.time() < open_deadline:
                port = self._resolve_port()
                if not port:
                    log.write("SERIAL_OPEN_RETRY=no_port\n")
                    log.flush()
                    time.sleep(1.0)
                    continue
                try:
                    ser = self._backend.open(port)
                except Exception as e:
                    log.write(f"SERIAL_OPEN_RETRY={e}\n")
                    log.flush()
                    time.sleep(1.0)
                    continue
                with self._handle_lock:
                    self._handle = ser
                try:
                    self._read_loop(log, ser, port)
                finally:
                    with self._handle_lock:
                        if self._handle is ser:
                            self._handle = None
                    self._backend.close(ser)
                if self._stop.is_set():
                    break
                self._port_hint = None
                log.write("SERIAL_REDISCOVER\n")
                log.flush()
                time.sleep(0.5)
            if not self._stop.is_set():
                log.write("SERIAL_CAPTURE_ERROR=open_timeout\n")
                log.flush()

    def _read_loop(self, log, ser, port: str) -> None:
        while not self._stop.is_set():
            try:
                data = self._backend.read(ser, 4096)
            except Exception as e:
                log.write(f"SERIAL_READ_ERROR={e}\n")
                log.flush()
                break
            if data:
                log.write(data.decode("utf-8", errors="replace"))
                log.flush()
                continue
            if not getattr(ser, "is_open", True):
                log.write(f"SERIAL_DISCONNECT port={port}\n")
                log.flush()
                break
