"""进程单例、信号处理与资源释放。"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from pathlib import Path
from typing import Callable

shutdown_event = threading.Event()
_cleanup_lock = threading.Lock()
_cleaned_up = False
_cleanup_hooks: list[Callable[[str], None]] = []


def is_process_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            return bool(ok and exit_code.value == STILL_ACTIVE)
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def register_cleanup(hook: Callable[[str], None]) -> None:
    _cleanup_hooks.append(hook)


def shutdown_all(reason: str = "normal") -> None:
    global _cleaned_up
    with _cleanup_lock:
        if _cleaned_up:
            return
        _cleaned_up = True

    shutdown_event.set()
    print(f"\n[Shutdown] 正在释放资源 ({reason})...")

    for hook in reversed(_cleanup_hooks):
        try:
            hook(reason)
        except Exception as exc:
            print(f"[Shutdown] 清理回调失败: {exc}")

    print("[Shutdown] 服务已完全停止")


def _signal_handler(signum, frame) -> None:  # noqa: ARG001
    name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    shutdown_all(reason=f"signal:{name}")
    sys.exit(0)


def install_signal_handlers() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)
    atexit.register(lambda: shutdown_all(reason="atexit"))


class PidFile:
    """防止重复启动 app.py，并在退出时删除 PID 文件。"""

    def __init__(self, path: Path, service_name: str = "app.py"):
        self.path = path
        self.service_name = service_name
        self.pid = os.getpid()

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                old_pid = int(self.path.read_text(encoding="utf-8").strip())
            except ValueError:
                old_pid = 0
            if old_pid and old_pid != self.pid and is_process_running(old_pid):
                print(f"错误: {self.service_name} 已在运行 (PID {old_pid})")
                print("  直接访问: http://127.0.0.1:5000")
                print(f"  若要重启: taskkill /PID {old_pid} /F")
                print("  然后从项目根目录运行: python run.py")
                sys.exit(1)
            if old_pid and not is_process_running(old_pid):
                print(f"[提示] 清除过期 PID 记录 ({old_pid})")
                try:
                    self.path.unlink()
                except OSError:
                    pass
        self.path.write_text(str(self.pid), encoding="utf-8")
        register_cleanup(lambda _reason: self.release())

    def release(self) -> None:
        if not self.path.exists():
            return
        try:
            if int(self.path.read_text(encoding="utf-8").strip()) == self.pid:
                self.path.unlink()
        except (ValueError, OSError):
            pass


def check_port_available(host: str, port: int) -> bool:
    import socket

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host if host != "0.0.0.0" else "127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()
