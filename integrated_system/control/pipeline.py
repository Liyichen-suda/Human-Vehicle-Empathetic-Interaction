"""情绪检测 -> RL 推理 -> MQTT 下发的闭环管道。"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from paths import CENTER_CONTROL_ROOT

if str(CENTER_CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CENTER_CONTROL_ROOT))


def _load_state_converter():
    module_path = CENTER_CONTROL_ROOT / "core" / "state_converter.py"
    spec = importlib.util.spec_from_file_location("state_converter", str(module_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_state_converter = _load_state_converter()
normalize_emotion_probs = _state_converter.normalize_emotion_probs
emotion_probs_to_state = _state_converter.emotion_probs_to_state

from device_control_center import DeviceControlCenter  # noqa: E402


def format_prediction_for_frontend(
    pipeline_result: dict[str, Any],
    emotion_input: dict[str, float] | None = None,
    device_status: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """将控制中心输出转换为前端 API 格式，并合并设备 MQTT 状态反馈。"""
    infer_result = pipeline_result.get("infer_result", {})
    predicted_actions: dict[str, dict[str, Any]] = {}
    status_map = device_status or {}

    for device, info in infer_result.items():
        if not isinstance(info, dict) or "action_name" not in info:
            continue
        mqtt_dev_status = status_map.get(device, {})
        mqtt_info = pipeline_result.get("mqtt_result", {}).get(device, {})
        predicted_actions[device] = {
            "action": info["action_name"],
            "action_idx": info.get("action_idx"),
            "probability": float(info.get("action_prob", 0.0)),
            "value": float(info.get("value", 0.0)),
            "runtime": mqtt_dev_status.get("runtime", "等待设备"),
            "exec_status": mqtt_dev_status.get("status", ""),
            "message": mqtt_dev_status.get("message", ""),
            "selected_track": mqtt_dev_status.get("selected_track"),
            "device_online": mqtt_dev_status.get("online") if mqtt_dev_status else None,
            "mqtt_status": mqtt_info.get("status", ""),
        }

    state = pipeline_result.get("state")
    fatigue = float(state[8]) if state and len(state) >= 9 else None

    return {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "emotion_input": emotion_input or {},
        "state_9d": state,
        "predicted_actions": predicted_actions,
        "device_status": status_map,
        "mqtt_result": pipeline_result.get("mqtt_result", {}),
        "fatigue": fatigue,
        "mqtt_enabled": pipeline_result.get("mqtt_enabled", False),
        "online_learning": pipeline_result.get("online_learning"),
    }


class EmotionControlPipeline:
    """
    控制链路：情绪概率 -> 9维状态 -> RL 推理 -> MQTT 下发
    支持在线学习适应与语音强制控制。
    """

    def __init__(
        self,
        center_control_root: Path | str | None = None,
        default_fatigue: float = 50.0,
        sample_mode: bool = False,
        publish_mqtt: bool = True,
        online_learning_cfg: dict[str, Any] | None = None,
    ):
        self.center_root = Path(center_control_root) if center_control_root else CENTER_CONTROL_ROOT
        self.default_fatigue = default_fatigue
        self.sample_mode = sample_mode
        self.publish_mqtt = publish_mqtt

        self.center = DeviceControlCenter(project_root=self.center_root)
        self._mqtt_connected = False
        self.online_learner = None

        if online_learning_cfg and online_learning_cfg.get("enabled", True):
            from learning.online_learner import OnlineLearningManager

            cfg = dict(online_learning_cfg)
            cfg.setdefault(
                "checkpoint_path",
                str(self.center_root / "logs" / "checkpoints" / "UserState_online.pth"),
            )
            self.online_learner = OnlineLearningManager(
                self.center.engine,
                cfg,
                logs_dir=self.center_root / "logs",
            )
            self.center.engine.set_preference_applier(self.online_learner.apply_preference_bias)

    @property
    def mqtt_connected(self) -> bool:
        return self._mqtt_connected

    def connect_mqtt(self) -> bool:
        if not self.publish_mqtt:
            return False
        try:
            self.center.connect()
            self._mqtt_connected = True
            return True
        except Exception as exc:
            print(f"[Pipeline] MQTT 连接失败: {exc}")
            self._mqtt_connected = False
            return False

    def disconnect_mqtt(self) -> None:
        if self._mqtt_connected:
            try:
                self.center.disconnect()
            except Exception:
                pass
            self._mqtt_connected = False

    def start_online_learning(
        self,
        get_emotion: Callable[[], Optional[dict[str, float]]],
        get_fatigue: Callable[[], float],
    ) -> None:
        if self.online_learner:
            self.online_learner.set_emotion_provider(get_emotion, get_fatigue)
            self.online_learner.start()

    def stop_online_learning(self) -> None:
        if self.online_learner:
            self.online_learner.stop()

    def _register_learning_feedback(
        self,
        result: dict[str, Any],
        source: str = "auto",
    ) -> None:
        if not self.online_learner or not self.online_learner.enabled:
            return

        mqtt_result = result.get("mqtt_result", {})
        sent_devices = [
            dev for dev, info in mqtt_result.items() if info.get("status") == "sent"
        ]
        if not sent_devices:
            return

        meta = dict(result.get("inference_meta") or {})
        meta["sent_devices"] = sent_devices
        self.online_learner.register_action_batch(
            result["state"],
            result["infer_result"],
            meta,
            source=source,
        )

    def run_from_emotion(
        self,
        emotion_probs: dict[str, float],
        fatigue: float | None = None,
        publish: bool | None = None,
        source: str = "auto",
    ) -> dict[str, Any]:
        normalized = normalize_emotion_probs(emotion_probs)
        should_publish = self.publish_mqtt if publish is None else publish
        if should_publish and not self._mqtt_connected:
            should_publish = False

        use_details = bool(self.online_learner and self.online_learner.enabled)
        result = self.center.run_from_emotion(
            normalized,
            fatigue=fatigue if fatigue is not None else self.default_fatigue,
            sample_mode=self.sample_mode,
            publish=should_publish,
            return_details=use_details,
            source=source,
        )
        result["emotion_input"] = normalized
        result["mqtt_enabled"] = should_publish and self._mqtt_connected

        if should_publish:
            self._register_learning_feedback(result, source=source)

        if self.online_learner:
            result["online_learning"] = self.online_learner.get_stats()

        return result

    def run_direct_action(
        self,
        device: str,
        action: str,
        source: str = "manual",
        force: bool = False,
    ) -> dict[str, Any]:
        if not self._mqtt_connected:
            raise RuntimeError("MQTT 未连接，无法下发控制命令")
        sent = self.center.run_direct({device: action}, source=source, force=force)
        return sent.get(device, {"status": "error", "action": action})

    def run_direct_actions(
        self,
        actions: dict[str, str],
        source: str = "manual",
        force: bool = False,
    ) -> dict[str, dict[str, Any]]:
        if not self._mqtt_connected:
            raise RuntimeError("MQTT 未连接，无法下发控制命令")
        return self.center.run_direct(actions, source=source, force=force)

    def run_voice_command(self, text: str) -> dict[str, Any]:
        from voice.voice_controller import parse_voice_text

        if not self._mqtt_connected:
            raise RuntimeError("MQTT 未连接，无法执行语音命令")

        cmd = parse_voice_text(text)
        if cmd is None:
            return {
                "success": False,
                "message": f"听到「{text}」但无法匹配命令，请说：播放稳定音乐 / 空调降温 / 关闭所有",
                "recognized_text": text,
            }

        if cmd.device == "all":
            actions = {dev: "关闭" for dev in self.center.engine.device_names}
            sent = self.center.run_direct(actions, source="voice", force=True)
            return {
                "success": True,
                "message": "已语音关闭全部设备",
                "parsed": {"device": "all", "action": "关闭", "raw_text": cmd.raw_text},
                "mqtt_result": sent,
            }

        sent = self.center.run_direct(
            {cmd.device: cmd.action},
            source="voice",
            force=True,
        )
        return {
            "success": True,
            "message": f"已语音执行: {cmd.device} -> {cmd.action}",
            "parsed": {
                "device": cmd.device,
                "action": cmd.action,
                "raw_text": cmd.raw_text,
            },
            "mqtt_result": sent,
        }

    def get_devices_info(self) -> dict[str, Any]:
        return {
            dev: {
                "actions": self.center.engine.device_actions[dev],
                "action_count": len(self.center.engine.device_actions[dev]),
            }
            for dev in self.center.engine.device_names
        }

    def get_emotion_keys(self) -> list[str]:
        from core.state_converter import EMOTION_KEYS

        return list(EMOTION_KEYS)

    def get_online_learning_stats(self) -> dict[str, Any]:
        if not self.online_learner:
            return {"enabled": False}
        return {"enabled": True, **self.online_learner.get_stats()}
