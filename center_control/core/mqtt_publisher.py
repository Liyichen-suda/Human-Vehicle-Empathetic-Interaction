"""MQTT 发布与订阅封装。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Callable

import paho.mqtt.client as mqtt

from mqtt_compat import connect_succeeded, create_mqtt_client


class MqttPublisher:
    def __init__(
        self,
        broker: str,
        port: int = 1883,
        username: str = "",
        password: str = "",
        topic_prefix: str = "cabin",
        qos: int = 1,
        retain: bool = False,
    ):
        self.broker = broker
        self.port = port
        self.topic_prefix = topic_prefix.rstrip("/")
        self.qos = qos
        self.retain = retain

        self._client = create_mqtt_client()
        if username:
            self._client.username_pw_set(username, password or None)

        self._connected = False
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def _on_connect(self, client, userdata, flags, reason_code, *args):
        self._connected = connect_succeeded(reason_code)

    def _on_disconnect(self, client, userdata, *args):
        self._connected = False

    def connect(self) -> None:
        self._client.connect(self.broker, self.port, keepalive=60)
        self._client.loop_start()
        for _ in range(30):
            if self._connected:
                return
            time.sleep(0.1)
        raise ConnectionError(f"无法连接 MQTT Broker: {self.broker}:{self.port}")

    def disconnect(self) -> None:
        try:
            self._client.loop_stop()
        except Exception:
            pass
        try:
            self._client.disconnect()
        except Exception:
            pass

    def control_topic(self, device: str) -> str:
        return f"{self.topic_prefix}/{device}/control"

    def status_topic(self, device: str) -> str:
        return f"{self.topic_prefix}/{device}/status"

    def publish_action(
        self,
        device: str,
        action: str,
        action_idx: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "device": device,
            "action": action,
            "action_idx": action_idx,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "center_control",
        }
        if extra:
            payload.update(extra)

        topic = self.control_topic(device)
        body = json.dumps(payload, ensure_ascii=False)
        info = self._client.publish(topic, body, qos=self.qos, retain=self.retain)
        info.wait_for_publish(timeout=5.0)
        return {"topic": topic, "payload": payload, "mid": info.mid}

    def publish_batch(self, infer_result: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        sent: dict[str, dict[str, Any]] = {}
        for device, result in infer_result.items():
            sent[device] = self.publish_action(
                device=device,
                action=result["action_name"],
                action_idx=result.get("action_idx"),
            )
        return sent

    def subscribe_status(self, devices: list[str], callback: Callable[[str, dict], None]) -> None:
        def _on_message(client, userdata, msg):
            device = msg.topic.split("/")[-2] if msg.topic.count("/") >= 2 else "unknown"
            try:
                data = json.loads(msg.payload.decode("utf-8"))
            except json.JSONDecodeError:
                data = {"raw": msg.payload.decode("utf-8", errors="replace")}
            callback(device, data)

        self._client.on_message = _on_message
        for device in devices:
            self._client.subscribe(self.status_topic(device), qos=self.qos)

    def publish_broadcast(self, topic: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False)
        self._client.publish(topic, body, qos=self.qos, retain=False)
