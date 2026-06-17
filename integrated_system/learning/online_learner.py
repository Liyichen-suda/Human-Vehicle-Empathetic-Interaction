"""在线学习：根据动作后真实情绪变化微调策略，适应个人习惯。"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import sys
import torch

from paths import CENTER_CONTROL_ROOT

if str(CENTER_CONTROL_ROOT) not in sys.path:
    sys.path.insert(0, str(CENTER_CONTROL_ROOT))

from core.state_converter import emotion_probs_to_state


@dataclass
class PendingFeedback:
    feedback_id: str
    pre_state_9d: List[float]
    infer_result: Dict[str, Dict[str, Any]]
    inference_meta: Dict[str, Any]
    recorded_at: float
    source: str = "auto"


class OnlineLearningManager:
    """记录动作下发后的情绪反馈，周期性 PPO 微更新 + 偏好偏置。"""

    def __init__(
        self,
        model_engine,
        config: Dict[str, Any],
        logs_dir: Path,
    ):
        self.engine = model_engine
        self.enabled = bool(config.get("enabled", True))
        self.feedback_delay_s = float(config.get("feedback_delay_s", 20.0))
        self.update_batch_size = int(config.get("update_batch_size", 4))
        self.min_improvement = float(config.get("min_improvement", 0.001))
        self.save_every_updates = int(config.get("save_every_updates", 2))
        self.preference_alpha = float(config.get("preference_alpha", 0.15))

        self.logs_dir = Path(logs_dir)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.feedback_log = self.logs_dir / "online_feedback.jsonl"
        self.preference_file = self.logs_dir / "action_preferences.json"
        self.checkpoint_path = Path(
            config.get(
                "checkpoint_path",
                str(logs_dir / "checkpoints" / "UserState_online.pth"),
            )
        )

        self._pending: List[PendingFeedback] = []
        self._trajectory_buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._update_count = 0
        self._stats = {
            "feedback_completed": 0,
            "policy_updates": 0,
            "last_update_at": None,
            "last_avg_reward": 0.0,
        }
        self._preferences: Dict[str, Dict[str, float]] = self._load_preferences()

        self._get_emotion_fn: Optional[Callable[[], Optional[Dict[str, float]]]] = None
        self._get_fatigue_fn: Optional[Callable[[], float]] = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="online-learner")

    def set_emotion_provider(
        self,
        get_emotion: Callable[[], Optional[Dict[str, float]]],
        get_fatigue: Callable[[], float],
    ) -> None:
        self._get_emotion_fn = get_emotion
        self._get_fatigue_fn = get_fatigue

    def start(self) -> None:
        if self.enabled:
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def get_preferences(self) -> Dict[str, Dict[str, float]]:
        with self._lock:
            return json.loads(json.dumps(self._preferences))

    def register_action_batch(
        self,
        pre_state_9d: List[float],
        infer_result: Dict[str, Dict[str, Any]],
        inference_meta: Dict[str, Any],
        source: str = "auto",
    ) -> None:
        if not self.enabled:
            return
        sent_devices = inference_meta.get("sent_devices") or list(infer_result.keys())
        if not sent_devices:
            return

        with self._lock:
            self._pending.append(
                PendingFeedback(
                    feedback_id=str(uuid.uuid4())[:8],
                    pre_state_9d=list(pre_state_9d),
                    infer_result={k: infer_result[k] for k in sent_devices if k in infer_result},
                    inference_meta=inference_meta,
                    recorded_at=time.time(),
                    source=source,
                )
            )

    def apply_preference_bias(
        self,
        device: str,
        action_probs: np.ndarray,
        state_9d: List[float],
    ) -> np.ndarray:
        key = self._state_cluster_key(state_9d)
        pref = self._preferences.get(key, {}).get(device, {})
        if not pref:
            return action_probs

        biased = action_probs.copy()
        actions = self.engine.device_actions[device]
        for action_name, score in pref.items():
            if action_name in actions:
                idx = actions.index(action_name)
                biased[idx] += score * self.preference_alpha
        biased = np.clip(biased, 1e-6, None)
        biased /= biased.sum()
        return biased

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            try:
                self._process_pending()
            except Exception as exc:
                print(f"[OnlineLearner] 处理反馈失败: {exc}")

    def _process_pending(self) -> None:
        if self._get_emotion_fn is None:
            return

        now = time.time()
        ready: List[PendingFeedback] = []
        remaining: List[PendingFeedback] = []

        with self._lock:
            for item in self._pending:
                if now - item.recorded_at >= self.feedback_delay_s:
                    ready.append(item)
                else:
                    remaining.append(item)
            self._pending = remaining

        for item in ready:
            self._evaluate_feedback(item)

    def _evaluate_feedback(self, item: PendingFeedback) -> None:
        emotion = self._get_emotion_fn()
        if not emotion:
            return

        fatigue = self._get_fatigue_fn() if self._get_fatigue_fn else 50.0
        post_state_9d = emotion_probs_to_state(emotion, fatigue=fatigue)
        action_names = {dev: info["action_name"] for dev, info in item.infer_result.items()}

        pre_full, ideal_full = self.engine.build_full_state(item.pre_state_9d, action_names)
        post_full, _ = self.engine.build_full_state(post_state_9d, action_names)

        trainer = self.engine.trainer
        sim = self.engine.simulator
        device_actions_idx = {
            dev: int(info["action_idx"])
            for dev, info in item.infer_result.items()
            if info.get("action_idx") is not None
        }
        _, device_effects = sim.state_transition(pre_full.copy(), device_actions_idx)

        device_rewards: Dict[str, float] = {}
        total_reward = 0.0
        for dev, info in item.infer_result.items():
            effect = device_effects.get(dev, {"active": False, "cost": 0.0})
            reward = trainer.compute_device_reward(dev, pre_full, post_full, ideal_full, effect)
            device_rewards[dev] = reward
            total_reward += reward
            self._update_preference(item.pre_state_9d, dev, info["action_name"], reward)

        avg_reward = total_reward / max(len(device_rewards), 1)
        meta = item.inference_meta.get("device_details", {})

        step = {
            "state": pre_full,
            "ideal_state": ideal_full,
            "device_actions": device_actions_idx,
            "device_log_probs": {
                dev: torch.tensor(
                    meta.get(dev, {}).get("log_prob", 0.0),
                    device=self.engine.trainer.device,
                )
                for dev in device_actions_idx
            },
            "device_rewards": device_rewards,
            "device_values": {
                dev: meta.get(dev, {}).get("value", 0.0)
                for dev in device_actions_idx
            },
            "next_state": post_full,
            "done": False,
            "terminated": False,
        }

        if any(dev not in meta or meta.get(dev, {}).get("log_prob") is None for dev in device_actions_idx):
            self._log_feedback(item, post_state_9d, device_rewards, avg_reward, skipped=True)
            return

        with self._lock:
            self._stats["feedback_completed"] += 1
            self._stats["last_avg_reward"] = avg_reward
            if avg_reward >= self.min_improvement:
                self._trajectory_buffer.append(step)
            should_update = len(self._trajectory_buffer) >= self.update_batch_size

        self._log_feedback(item, post_state_9d, device_rewards, avg_reward, skipped=False)
        if should_update:
            self._run_policy_update()

    def _run_policy_update(self) -> None:
        with self._lock:
            batch = list(self._trajectory_buffer)
            self._trajectory_buffer.clear()

        if not batch:
            return

        try:
            trainer = self.engine.trainer
            trainer.policy.train()
            trainer.update_policy(batch, epochs=2)
            trainer.policy.eval()
            self._update_count += 1

            with self._lock:
                self._stats["policy_updates"] = self._update_count
                self._stats["last_update_at"] = datetime.now().isoformat(timespec="seconds")

            if self._update_count % self.save_every_updates == 0:
                self.checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
                trainer.save_model(str(self.checkpoint_path))
                print(f"[OnlineLearner] 已保存在线适应权重: {self.checkpoint_path}")

            print(f"[OnlineLearner] 策略微更新完成 (#{self._update_count}, batch={len(batch)})")
        except Exception as exc:
            print(f"[OnlineLearner] 策略更新失败: {exc}")

    def _update_preference(
        self,
        state_9d: List[float],
        device: str,
        action: str,
        reward: float,
    ) -> None:
        key = self._state_cluster_key(state_9d)
        with self._lock:
            bucket = self._preferences.setdefault(key, {}).setdefault(device, {})
            old = bucket.get(action, 0.0)
            bucket[action] = float(old * 0.85 + reward * 0.15)
            self._save_preferences()

    @staticmethod
    def _state_cluster_key(state_9d: List[float]) -> str:
        emotions = state_9d[:8]
        top_idx = int(np.argmax(emotions))
        fatigue_bucket = int(state_9d[8] // 20) if len(state_9d) > 8 else 2
        return f"e{top_idx}_f{fatigue_bucket}"

    def _load_preferences(self) -> Dict[str, Dict[str, float]]:
        if not self.preference_file.exists():
            return {}
        try:
            with open(self.preference_file, encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_preferences(self) -> None:
        try:
            with open(self.preference_file, "w", encoding="utf-8") as f:
                json.dump(self._preferences, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"[OnlineLearner] 偏好保存失败: {exc}")

    def _log_feedback(
        self,
        item: PendingFeedback,
        post_state_9d: List[float],
        rewards: Dict[str, float],
        avg_reward: float,
        skipped: bool,
    ) -> None:
        record = {
            "feedback_id": item.feedback_id,
            "source": item.source,
            "pre_state_9d": item.pre_state_9d,
            "post_state_9d": post_state_9d,
            "actions": {d: v["action_name"] for d, v in item.infer_result.items()},
            "device_rewards": rewards,
            "avg_reward": avg_reward,
            "skipped_update": skipped,
            "logged_at": datetime.now().isoformat(timespec="seconds"),
        }
        try:
            with open(self.feedback_log, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass
