"""
一键启动所有本地模拟设备（各设备独立进程，通过 MQTT 接收控制命令）。

用法:
  cd center_control/simulators
  python run_all_devices.py

或由项目根目录: python run.py
"""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEVICES = [
    "music_device.py",
    "ac_device.py",
    "light_device.py",
    "aroma_device.py",
    "massage_device.py",
]

SIMULATORS_DIR = Path(__file__).resolve().parent
PID_FILE = SIMULATORS_DIR.parent / "logs" / "devices.pid"
procs: list[subprocess.Popen] = []


def _is_running(pid: int) -> bool:
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
    except (OSError, ProcessLookupError, SystemError):
        return False
    return True


def launch_all_devices(
    *,
    check_existing: bool = True,
    reuse_existing: bool = False,
) -> list[subprocess.Popen]:
    """启动 5 个设备模拟进程，返回 Popen 列表（供 run.py 一键启动）。"""
    global procs

    if check_existing and PID_FILE.exists():
        try:
            old_pids = [int(x) for x in PID_FILE.read_text(encoding="utf-8").split() if x.strip()]
        except ValueError:
            old_pids = []
        alive = [p for p in old_pids if _is_running(p)]
        if alive:
            if reuse_existing and len(alive) >= len(DEVICES):
                print(f"[Devices] 检测到 {len(alive)} 个设备进程已在运行，复用现有进程\n")
                return []
            print(f"错误: 设备进程可能仍在运行 (PIDs: {alive})")
            print("请先停止: taskkill /PID <pid> /F")
            print("或在 run.py 中会自动复用已运行的设备（一键启动时 reuse_existing=True）")
            sys.exit(1)
        PID_FILE.unlink(missing_ok=True)

    print("=" * 60)
    print("  启动本地模拟设备（MQTT 订阅端）")
    print("=" * 60)

    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    started: list[subprocess.Popen] = []
    for script in DEVICES:
        path = SIMULATORS_DIR / script
        print(f"  启动: {script}")
        proc = subprocess.Popen(
            [sys.executable, str(path)],
            cwd=str(SIMULATORS_DIR),
            creationflags=creationflags,
        )
        started.append(proc)

    procs = started
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(" ".join(str(p.pid) for p in procs), encoding="utf-8")
    print(f"\n[Devices] 已启动 {len(procs)} 个设备进程\n")
    return procs


def _terminate_pid(pid: int) -> None:
    if pid <= 0 or not _is_running(pid):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                check=False,
            )
        else:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            if _is_running(pid):
                os.kill(pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _read_pid_file() -> list[int]:
    if not PID_FILE.exists():
        return []
    try:
        return [int(x) for x in PID_FILE.read_text(encoding="utf-8").split() if x.strip()]
    except ValueError:
        return []


def stop_devices_from_pid_file(reason: str = "exit") -> None:
    """按 devices.pid 停止设备进程（含 reuse_existing 时遗留的孤儿进程）。"""
    pids = _read_pid_file()
    if not pids:
        return
    alive = [pid for pid in pids if _is_running(pid)]
    if not alive:
        PID_FILE.unlink(missing_ok=True)
        return
    print(f"\n[Devices] 正在停止 PID 文件中的设备进程 ({reason}): {alive}")
    for pid in alive:
        _terminate_pid(pid)
    time.sleep(0.3)
    PID_FILE.unlink(missing_ok=True)
    print("[Devices] PID 文件中的设备已停止")


def stop_all_devices(active: list[subprocess.Popen] | None = None, reason: str = "exit") -> None:
    global procs
    targets = active if active is not None else procs
    if targets:
        print(f"\n[Devices] 正在停止本次启动的设备 ({reason})...")
        for proc in targets:
            if proc.poll() is None:
                proc.terminate()
        for proc in targets:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if active is None or active is procs:
            procs.clear()
    stop_devices_from_pid_file(reason=reason)
    print("[Devices] 已全部停止")


def _signal_handler(signum, frame) -> None:  # noqa: ARG001
    stop_all_devices(reason=f"signal:{signum}")
    sys.exit(0)


def main() -> None:
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)
    atexit.register(lambda: stop_all_devices(reason="atexit"))

    launch_all_devices()
    print("所有设备已运行。按 Ctrl+C 停止全部。\n")

    try:
        while True:
            for proc in procs:
                code = proc.poll()
                if code is not None:
                    print(f"[Devices] 子进程异常退出 code={code}")
                    stop_all_devices(reason="child-exit")
                    sys.exit(code)
            time.sleep(1)
    except KeyboardInterrupt:
        stop_all_devices(reason="keyboard")


if __name__ == "__main__":
    main()
