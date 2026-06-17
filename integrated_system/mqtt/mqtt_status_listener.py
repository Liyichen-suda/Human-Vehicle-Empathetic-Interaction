"""订阅各设备 MQTT 状态 topic，供可视化前端展示。"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from paths import CENTER_CONTROL_ROOT

if str(CENTER_CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CENTER_CONTROL_ROOT))

from mqtt_compat import connect_succeeded, create_mqtt_client  # noqa: E402

DEVICE_NAMES = ["music", "ac", "light", "aroma", "massage"]


class DeviceStatusListener:
    """订阅 cabin/{device}/status，缓存各设备最新状态。"""

    def __init__(self, center_control_root: Path | str | None = None):
        root = Path(center_control_root) if center_control_root else CENTER_CONTROL_ROOT
        cfg_path = root / "config" / "runtime.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        mqtt_cfg = cfg.get("mqtt", {})
        self.broker = mqtt_cfg.get("broker", "localhost")
        self.port = int(mqtt_cfg.get("port", 1883))
        self.prefix = mqtt_cfg.get("topic_prefix", "cabin").rstrip("/")
        self.qos = int(mqtt_cfg.get("qos", 1))
        self.username = mqtt_cfg.get("username", "")
        self.password = mqtt_cfg.get("password", "")

        self._status: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._connected = False
        self._client = create_mqtt_client()
        if self.username:
            self._client.username_pw_set(self.username, self.password or None)

        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _status_topic(self, device: str) -> str:
        return f"{self.prefix}/{device}/status"

    def _on_connect(self, client, userdata, flags, reason_code, *args):
        if connect_succeeded(reason_code):
            self._connected = True
            for device in DEVICE_NAMES:
                client.subscribe(self._status_topic(device), qos=self.qos)
            print(f"[StatusListener] 已订阅 {len(DEVICE_NAMES)} 个设备状态 topic")
        else:
            print(f"[StatusListener] 连接失败: {reason_code}")

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except json.JSONDecodeError:
            return

        parts = msg.topic.split("/")
        device = parts[-2] if len(parts) >= 2 else "unknown"
        data["received_at"] = datetime.now().isoformat(timespec="seconds")

        with self._lock:
            self._status[device] = data

    def start(self) -> bool:
        try:
            self._client.connect(self.broker, self.port, keepalive=60)
            self._client.loop_start()
            for _ in range(30):
                if self._connected:
                    return True
                time.sleep(0.1)
            return False
        except Exception as exc:
            print(f"[StatusListener] 启动失败: {exc}")
            return False

    def stop(self) -> None:
        try:
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    def get_status(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._status)

    @property
    def connected(self) -> bool:
        return self._connected
