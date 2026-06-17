"""兼容 paho-mqtt 1.x 与 2.x API 差异。"""

from __future__ import annotations

import paho.mqtt.client as mqtt

PAHO_V2 = hasattr(mqtt, "CallbackAPIVersion")


def create_mqtt_client(client_id: str | None = None) -> mqtt.Client:
    if PAHO_V2:
        kwargs: dict = {"callback_api_version": mqtt.CallbackAPIVersion.VERSION2}
        if client_id is not None:
            kwargs["client_id"] = client_id
        return mqtt.Client(**kwargs)
    if client_id is not None:
        return mqtt.Client(client_id=client_id)
    return mqtt.Client()


def connect_succeeded(reason_code) -> bool:
    if hasattr(reason_code, "value"):
        return int(reason_code.value) == 0
    return int(reason_code) == 0
