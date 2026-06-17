"""加载 RL 模型并执行推理。"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import numpy as np
import torch
from torch.distributions import Categorical


class ModelEngine:
    def __init__(self, project_root: Path, script: str, checkpoint: str):
        self.project_root = project_root
        self.script_path = project_root / script
        self.checkpoint_path = project_root / checkpoint

        self.model_mod = self._load_module(self.script_path)
        self.simulator = self.model_mod.UserStateSimulator()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self.trainer = self.model_mod.FactorizedPPOTrainer(self.simulator, device=device)

        if not self.checkpoint_path.exists():
            online_ckpt = project_root / "logs" / "checkpoints" / "UserState_online.pth"
            if online_ckpt.exists():
                self.checkpoint_path = online_ckpt
            else:
                raise FileNotFoundError(f"未找到模型权重: {self.checkpoint_path}")

        self.trainer.load_model(str(self.checkpoint_path))
        self.trainer.policy.eval()
        self._preference_applier = None

    def set_preference_applier(self, applier) -> None:
        """注入在线学习的动作偏好偏置函数。"""
        self._preference_applier = applier

    @staticmethod
    def _load_module(model_file: Path):
        spec = importlib.util.spec_from_file_location("stateflow_v31", str(model_file))
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载模型: {model_file}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @property
    def device_names(self) -> list[str]:
        return list(self.simulator.device_names)

    @property
    def device_actions(self) -> dict[str, list[str]]:
        return dict(self.simulator.device_actions)

    def build_full_state(
        self,
        state_9d: list[float],
        device_action_names: dict[str, str] | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        if len(state_9d) != 9:
            raise ValueError("state_9d 必须是 9 维")

        self.simulator.reset_device_states()
        if device_action_names:
            for dev, action_name in device_action_names.items():
                if dev in self.simulator.device_actions and action_name in self.simulator.device_actions[dev]:
                    self.simulator.device_state[dev] = action_name

        base_state = np.array(state_9d, dtype=np.float32)
        device_encoding = self.simulator.encode_device_states()
        current_state = np.concatenate([base_state, device_encoding])
        ideal_state = self.simulator.compute_ideal_state(current_state)
        return current_state, ideal_state

    def infer(
        self,
        state_9d: list[float],
        sample_mode: bool = True,
        return_details: bool = False,
    ) -> dict[str, dict[str, Any]]:
        if len(state_9d) != 9:
            raise ValueError("推理输入必须是 9 维状态")

        current_state, ideal_state = self.build_full_state(state_9d)

        device = self.trainer.device
        current_norm = torch.FloatTensor(self.trainer.normalize_state(current_state)).unsqueeze(0).to(device)
        ideal_norm = torch.FloatTensor(self.trainer.normalize_state(ideal_state)).unsqueeze(0).to(device)

        with torch.no_grad():
            device_probs, device_values = self.trainer.policy(current_norm, ideal_norm)

        results: dict[str, dict[str, Any]] = {}
        device_details: dict[str, dict[str, Any]] = {}

        for dev in self.simulator.device_names:
            probs_tensor = device_probs[dev].squeeze(0)
            probs = probs_tensor.detach().cpu().numpy()

            if self._preference_applier is not None:
                probs = self._preference_applier(dev, probs, state_9d)
                probs_tensor = torch.FloatTensor(probs).unsqueeze(0).to(device)

            dist = Categorical(probs_tensor)
            if sample_mode:
                action_idx = int(dist.sample().item())
            else:
                action_idx = int(np.argmax(probs))

            log_prob_tensor = dist.log_prob(torch.tensor(action_idx, device=device))
            action_name = self.simulator.device_actions[dev][action_idx]
            results[dev] = {
                "action_idx": action_idx,
                "action_name": action_name,
                "action_prob": float(probs[action_idx]),
                "value": float(device_values[dev].item()),
            }

            if return_details:
                device_details[dev] = {
                    "log_prob": float(log_prob_tensor.item()),
                    "value": float(device_values[dev].item()),
                }

        if return_details:
            results["_meta"] = {
                "state_9d": list(state_9d),
                "current_state_38d": current_state.tolist(),
                "ideal_state_38d": ideal_state.tolist(),
                "device_details": device_details,
            }

        return results
