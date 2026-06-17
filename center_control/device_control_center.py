"""

MQTT 设备控制中心



流程: 9 维用户状态 -> RL 推理 -> MQTT 发布 -> 各设备本地订阅执行



Topic: cabin/{device}/control

Payload: {"device":"music","action":"稳定","action_idx":2,"timestamp":"...","source":"center_control"}

"""

from __future__ import annotations



import argparse

import json

import sys

import time

from datetime import datetime

from pathlib import Path

from typing import Any



import yaml

try:
    import numpy as np
except ImportError:
    np = None

try:
    import torch
except ImportError:
    torch = None



from core.model_engine import ModelEngine

from core.mqtt_publisher import MqttPublisher

from core.state_converter import (
    emotion_probs_to_state,
    get_scene_state,
    load_scene_samples,
    validate_state,
)



# 默认场景「过度焦虑」对应的 9 维状态

DEFAULT_STATE_9D = [0.1515, 0.1714, 0.0844, 0.1291, 0.1361, 0.0844, 0.1726, 0.0705, 65.0]





class DeviceControlCenter:

    def __init__(self, project_root: Path | str | None = None):

        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parent

        self.runtime_cfg = self._load_yaml(self.project_root / "config" / "runtime.yaml")

        self.topics_cfg = self._load_yaml(self.project_root / "config" / "mqtt_topics.yaml")



        model_cfg = self.runtime_cfg["model"]

        self.engine = ModelEngine(

            self.project_root,

            script=model_cfg["script"],

            checkpoint=model_cfg["checkpoint"],

        )



        mqtt_cfg = self.runtime_cfg["mqtt"]

        self.mqtt = MqttPublisher(

            broker=mqtt_cfg["broker"],

            port=int(mqtt_cfg.get("port", 1883)),

            username=mqtt_cfg.get("username", ""),

            password=mqtt_cfg.get("password", ""),

            topic_prefix=mqtt_cfg.get("topic_prefix", "cabin"),

            qos=int(mqtt_cfg.get("qos", 1)),

            retain=bool(mqtt_cfg.get("retain", False)),

        )



        self.cooldown_s: dict[str, int] = self.runtime_cfg.get("cooldown_s", {})

        self._last_sent: dict[str, tuple[str, float]] = {}

        self.log_enabled = bool(self.runtime_cfg.get("logging", {}).get("enabled", True))

        self.log_file = self.project_root / self.runtime_cfg.get("logging", {}).get("log_file", "logs/control.log")

        if self.log_enabled:

            self.log_file.parent.mkdir(parents=True, exist_ok=True)



    @staticmethod

    def _load_yaml(path: Path) -> dict[str, Any]:

        with open(path, encoding="utf-8") as f:

            return yaml.safe_load(f) or {}



    def connect(self) -> None:

        self.mqtt.connect()



    def disconnect(self) -> None:

        self.mqtt.disconnect()



    def _in_cooldown(self, device: str, action: str) -> bool:

        last = self._last_sent.get(device)

        if not last:

            return False

        last_action, last_ts = last

        if last_action != action:

            return False

        return (time.time() - last_ts) < self.cooldown_s.get(device, 0)



    def _sanitize_for_json(self, obj: Any) -> Any:
        """移除 Tensor/numpy 等不可 JSON 序列化的对象。"""
        if torch is not None and isinstance(obj, torch.Tensor):
            if obj.numel() == 1:
                return float(obj.detach().cpu().item())
            return obj.detach().cpu().tolist()
        if isinstance(obj, dict):
            return {k: self._sanitize_for_json(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self._sanitize_for_json(v) for v in obj]
        if np is not None and isinstance(obj, (np.floating, np.integer)):
            return obj.item()
        if np is not None and isinstance(obj, np.ndarray):
            return obj.tolist()
        return obj



    def _log(self, record: dict[str, Any]) -> None:

        if not self.log_enabled:

            return

        safe_record = self._sanitize_for_json(dict(record))
        safe_record["logged_at"] = datetime.now().isoformat(timespec="seconds")

        with open(self.log_file, "a", encoding="utf-8") as f:

            f.write(json.dumps(safe_record, ensure_ascii=False) + "\n")



    def infer(self, state: list[float], sample_mode: bool = True, return_details: bool = False) -> dict[str, Any]:

        state_9d = validate_state(state)

        infer_raw = self.engine.infer(state_9d, sample_mode=sample_mode, return_details=return_details)

        inference_meta = infer_raw.pop("_meta", None) if "_meta" in infer_raw else None

        out = {"state": state_9d, "infer_result": infer_raw}

        if inference_meta is not None:

            out["inference_meta"] = inference_meta

        return out



    def run_from_emotion(

        self,

        emotion_probs: dict[str, float],

        fatigue: float = 50.0,

        sample_mode: bool = True,

        publish: bool = True,

        return_details: bool = False,

        source: str = "center_control",

        force: bool = False,

    ) -> dict[str, Any]:

        """从情绪检测结果直接推理并可选 MQTT 下发。"""

        state_9d = emotion_probs_to_state(emotion_probs, fatigue=fatigue)

        out = self.infer(state_9d, sample_mode=sample_mode, return_details=return_details)

        mqtt_result = (

            self.publish_actions(

                out["infer_result"],

                source=source,

                force=force,

            )

            if publish

            else {}

        )

        result = {**out, "emotion_input": emotion_probs, "mqtt_result": mqtt_result}

        self._log(result)

        return result



    def publish_actions(

        self,

        infer_result: dict[str, dict[str, Any]],

        skip_cooldown: bool = False,

        skip_unchanged: bool = True,

        source: str = "center_control",

        force: bool = False,

    ) -> dict[str, dict[str, Any]]:

        sent: dict[str, dict[str, Any]] = {}

        for device, info in infer_result.items():

            if device.startswith("_") or not isinstance(info, dict):

                continue

            action = info["action_name"]

            if not skip_cooldown and not force and self._in_cooldown(device, action):

                sent[device] = {"status": "cooldown", "action": action, "skipped": True}

                continue

            if skip_unchanged and action in ("维持不变",):

                sent[device] = {"status": "skip", "action": action, "skipped": True}

                continue



            result = self.mqtt.publish_action(

                device,

                action,

                info.get("action_idx"),

                extra={"source": source, "force": force},

            )

            self._last_sent[device] = (action, time.time())

            sent[device] = {"status": "sent", "action": action, **result}



        broadcast = self.topics_cfg.get("broadcast_topic")

        if broadcast:

            self.mqtt.publish_broadcast(broadcast, {"actions": {d: v.get("action") for d, v in sent.items()}})



        return sent



    def run_once(

        self,

        state: list[float],

        sample_mode: bool = True,

        publish: bool = True,

    ) -> dict[str, Any]:

        out = self.infer(state, sample_mode=sample_mode)

        mqtt_result = self.publish_actions(out["infer_result"]) if publish else {}

        result = {**out, "mqtt_result": mqtt_result}

        self._log(result)

        return result



    def run_scene(self, scene_name: str, sample_mode: bool = True) -> dict[str, Any]:

        scenes = load_scene_samples(self.project_root / "config" / "scene_samples.json")

        matched = next((s for s in scenes if s.get("name") == scene_name), None)

        if not matched:

            raise ValueError(f"未找到场景: {scene_name}")

        state = get_scene_state(matched)

        infer_result = self.engine.infer(state, sample_mode=sample_mode)

        mqtt_result = self.publish_actions(infer_result)

        result = {

            "scene": scene_name,

            "state": state,

            "infer_result": infer_result,

            "mqtt_result": mqtt_result,

        }

        self._log(result)

        return result



    def run_direct(

        self,

        actions: dict[str, str],

        source: str = "manual",

        force: bool = False,

    ) -> dict[str, dict[str, Any]]:

        infer_result = {

            dev: {"action_name": act, "action_idx": None, "action_prob": 1.0, "value": 0.0}

            for dev, act in actions.items()

        }

        return self.publish_actions(

            infer_result,

            skip_cooldown=True,

            skip_unchanged=False,

            source=source,

            force=force,

        )





def _print_result(result: dict[str, Any]) -> None:

    print("\n[推理结果]")

    for dev, info in result.get("infer_result", {}).items():

        print(f"  {dev:<8} -> {info['action_name']:<14} p={info['action_prob']:.3f} V={info['value']:+.2f}")



    print("\n[MQTT 下发]")

    for dev, info in result.get("mqtt_result", {}).items():

        status = info.get("status", "?")

        topic = info.get("topic", "")

        print(f"  {dev:<8} -> {info.get('action', ''):<14} [{status}] {topic}")





def main():

    parser = argparse.ArgumentParser(description="MQTT 设备控制中心")

    parser.add_argument("command", nargs="?", default="run", choices=["run", "scene", "direct", "infer"])

    parser.add_argument(

        "--state",

        type=str,

        help="逗号分隔 9 维状态：8 维情绪 + 疲劳，如 0.15,0.17,0.08,0.13,0.14,0.08,0.17,0.07,65",

    )

    parser.add_argument("--scene", type=str, default="过度焦虑")

    parser.add_argument("--no-publish", action="store_true", help="仅推理，不发 MQTT")

    parser.add_argument("--deterministic", action="store_true", help="取最大概率动作")

    args = parser.parse_args()



    center = DeviceControlCenter()

    center.connect()



    try:

        if args.command == "run":

            state = list(DEFAULT_STATE_9D)

            if args.state:

                state = [float(x.strip()) for x in args.state.split(",")]

            result = center.run_once(

                state,

                sample_mode=not args.deterministic,

                publish=not args.no_publish,

            )

            _print_result(result)



        elif args.command == "scene":

            result = center.run_scene(args.scene, sample_mode=not args.deterministic)

            _print_result(result)



        elif args.command == "direct":

            actions = {

                "music": "稳定",

                "ac": "轻微降温",

                "light": "提亮暖光",

                "aroma": "舒缓",

                "massage": "轻柔颈肩放松",

            }

            sent = center.run_direct(actions)

            print("\n[MQTT 直接下发]")

            for dev, info in sent.items():

                print(f"  {dev}: {info}")



        elif args.command == "infer":

            state = (

                [float(x.strip()) for x in args.state.split(",")]

                if args.state

                else list(DEFAULT_STATE_9D)

            )

            result = center.infer(state, sample_mode=not args.deterministic)

            _print_result({"infer_result": result["infer_result"], "mqtt_result": {}})



    finally:

        center.disconnect()





if __name__ == "__main__":

    main()


