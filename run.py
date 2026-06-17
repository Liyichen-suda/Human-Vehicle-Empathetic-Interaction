#!/usr/bin/env python3
"""
人车共情闭环系统 — 一键启动

  python run.py

自动启动: 5 个设备模拟器 + Flask 主应用
Ctrl+C 退出时会停止本次启动的设备进程（复用已有设备时不误杀）。

前置条件: MQTT Broker 运行在 localhost:1883
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INTEGRATED = ROOT / "integrated_system"
SIMULATORS = ROOT / "center_control" / "simulators"
RUNTIME_CFG = ROOT / "center_control" / "config" / "runtime.yaml"

for path in (str(INTEGRATED), str(SIMULATORS)):
    if path not in sys.path:
        sys.path.insert(0, path)


def _load_mqtt_endpoint() -> tuple[str, int]:
    try:
        import yaml

        with open(RUNTIME_CFG, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        mqtt = cfg.get("mqtt", {})
        host = str(mqtt.get("broker", "127.0.0.1"))
        port = int(mqtt.get("port", 1883))
        return host, port
    except Exception:
        return "127.0.0.1", 1883


def _mqtt_ready(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _watch_device_processes(procs: list) -> None:
    from infra.service_lifecycle import shutdown_event

    time.sleep(3)
    warned: set[int] = set()
    while not shutdown_event.is_set():
        for idx, proc in enumerate(procs):
            if idx in warned:
                continue
            code = proc.poll()
            if code is not None:
                names = ["music", "ac", "light", "aroma", "massage"]
                name = names[idx] if idx < len(names) else str(idx)
                print(f"\n[Devices] 警告: {name} 设备进程已退出 (code={code})")
                warned.add(idx)
        time.sleep(2)


def main() -> None:
    print("=" * 60)
    print("  人车共情闭环系统 — 一键启动")
    print("  设备模拟器 + Web 主应用")
    print("=" * 60)

    mqtt_host, mqtt_port = _load_mqtt_endpoint()
    if not _mqtt_ready(mqtt_host, mqtt_port):
        print(f"\n[警告] MQTT Broker 未在 {mqtt_host}:{mqtt_port} 监听")
        print("  请先启动 Mosquitto，否则设备控制无法下发。\n")
    else:
        print(f"\n[MQTT] Broker 就绪: {mqtt_host}:{mqtt_port}\n")

    from run_all_devices import launch_all_devices, stop_all_devices
    from infra.service_lifecycle import register_cleanup

    device_procs = launch_all_devices(reuse_existing=True)
    register_cleanup(lambda _reason: stop_all_devices(device_procs))
    if device_procs:
        threading.Thread(
            target=_watch_device_processes,
            args=(device_procs,),
            daemon=True,
            name="device-watcher",
        ).start()
    else:
        print("[Devices] 复用已有设备进程；退出 run.py 时仍会停止 devices.pid 中的进程\n")

    from app import main as run_app

    run_app()


if __name__ == "__main__":
    main()
