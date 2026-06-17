"""设备端 MQTT 公共工具。"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mqtt_compat import connect_succeeded, create_mqtt_client

HandlerResult = Optional[Dict[str, Any]]
DeviceHandler = Callable[[str, Dict[str, Any], mqtt.Client], HandlerResult]


def load_runtime(path: str = "../config/runtime.yaml") -> Dict[str, Any]:
    import yaml

    cfg_path = Path(__file__).resolve().parent / path
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_music_root(cfg: Dict[str, Any]) -> Path:
    devices_cfg = cfg.get("devices", {})
    raw = devices_cfg.get("music_root", "assets/music")
    path = Path(raw)
    if not path.is_absolute():
        path = (ROOT / path).resolve()
    return path


def control_topic(prefix: str, device: str) -> str:
    return f"{prefix.rstrip('/')}/{device}/control"


def status_topic(prefix: str, device: str) -> str:
    return f"{prefix.rstrip('/')}/{device}/status"


def publish_status(client: mqtt.Client, prefix: str, device: str, payload: Dict[str, Any], qos: int = 1) -> None:
    topic = status_topic(prefix, device)
    payload.setdefault("online", True)
    payload.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    client.publish(topic, json.dumps(payload, ensure_ascii=False), qos=qos)


def _start_heartbeat(
    client: mqtt.Client,
    prefix: str,
    device_name: str,
    qos: int,
    state: Dict[str, Any],
    interval_s: float = 5.0,
) -> None:
    """定期上报在线状态，避免 app.py 晚于设备启动时收不到 status。"""

    def loop() -> None:
        while True:
            last = state.get("last_status") or {}
            payload: Dict[str, Any] = {
                "device": device_name,
                "online": True,
                "status": last.get("status", "online"),
                "runtime": last.get("runtime", "在线"),
                "message": last.get("message", "设备已上线，等待控制命令"),
            }
            if last.get("action"):
                payload["action"] = last["action"]
            if last.get("selected_track"):
                payload["selected_track"] = last["selected_track"]
            publish_status(client, prefix, device_name, payload, qos=qos)
            time.sleep(interval_s)

    threading.Thread(target=loop, daemon=True, name=f"{device_name}-heartbeat").start()


def _parse_command_time(data: Dict[str, Any]) -> Optional[datetime]:
    raw = data.get("timestamp")
    if not raw:
        return None
    try:
        text = str(raw).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _should_ignore_command(
    device_name: str,
    msg: mqtt.MQTTMessage,
    data: Dict[str, Any],
    connected_at: datetime,
) -> bool:
    """忽略 Broker 上的 retained 消息，以及设备上线之前的历史命令。"""
    if getattr(msg, "retain", False):
        print(f"[{device_name}设备] 忽略 retained 历史消息: {data.get('action', '')}")
        return True

    cmd_time = _parse_command_time(data)
    if cmd_time is not None and cmd_time < connected_at:
        print(
            f"[{device_name}设备] 忽略过期命令: {data.get('action', '')} "
            f"(命令时间 {cmd_time.isoformat()} < 上线时间 {connected_at.isoformat()})"
        )
        return True

    return False


def run_device(
    device_name: str,
    handler: DeviceHandler,
    config_path: str = "../config/runtime.yaml",
) -> None:
    cfg = load_runtime(config_path)
    mqtt_cfg = cfg["mqtt"]
    prefix = mqtt_cfg.get("topic_prefix", "cabin")
    topic = control_topic(prefix, device_name)
    qos = int(mqtt_cfg.get("qos", 1))
    exec_cfg = cfg.get("devices", {}).get("execution", {})
    max_queue = int(exec_cfg.get("max_queue_size", 5))
    state: Dict[str, Any] = {"connected_at": datetime.now(timezone.utc)}

    from devices.executor import DeviceCommandExecutor

    def _handler_wrapper(action: str, data: Dict[str, Any]) -> HandlerResult:
        return handler(action, data, client)

    executor: Optional[DeviceCommandExecutor] = None

    def on_connect(client, userdata, flags, reason_code, *args):
        nonlocal executor
        if connect_succeeded(reason_code):
            state["connected_at"] = datetime.now(timezone.utc)
            client.subscribe(topic, qos=qos)
            print(f"[{device_name}设备] 已连接 Broker，订阅: {topic}")
            print(f"[{device_name}设备] 命令队列已启用，语音命令可强制打断")

            def _publish(payload: Dict[str, Any]) -> None:
                state["last_status"] = payload
                publish_status(client, prefix, device_name, payload, qos=qos)

            executor = DeviceCommandExecutor(device_name, _handler_wrapper, _publish, max_queue=max_queue)

            publish_status(
                client,
                prefix,
                device_name,
                {
                    "device": device_name,
                    "online": True,
                    "status": "online",
                    "runtime": "在线",
                    "message": "设备已上线，等待控制命令",
                },
                qos=qos,
            )
            state["last_status"] = {
                "status": "online",
                "runtime": "在线",
                "message": "设备已上线，等待控制命令",
            }
            _start_heartbeat(client, prefix, device_name, qos, state)
        else:
            print(f"[{device_name}设备] 连接失败: {reason_code}")

    def on_message(client, userdata, msg):
        if executor is None:
            return
        try:
            data = json.loads(msg.payload.decode("utf-8"))
            action = data.get("action", "")
        except json.JSONDecodeError:
            data = {"raw": msg.payload.decode("utf-8", errors="replace")}
            action = data["raw"]

        if _should_ignore_command(device_name, msg, data, state["connected_at"]):
            return

        print(f"\n[{device_name}设备] 收到 MQTT 命令: {data}")
        ack = executor.submit(action, data)
        print(f"[{device_name}设备] 受理结果: {ack.get('message')}")

    client = create_mqtt_client()
    username = mqtt_cfg.get("username", "")
    if username:
        client.username_pw_set(username, mqtt_cfg.get("password", "") or None)
    client.on_connect = on_connect
    client.on_message = on_message

    print(f"[{device_name}设备] 连接 {mqtt_cfg['broker']}:{mqtt_cfg.get('port', 1883)} ...")
    client.connect(mqtt_cfg["broker"], int(mqtt_cfg.get("port", 1883)), keepalive=60)
    client.loop_forever()
