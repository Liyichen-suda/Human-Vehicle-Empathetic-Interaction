"""各设备运行时状态（跨命令持久化）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class DeviceRuntimeState:
    device: str
    current_action: str = "关闭"
    busy: bool = False
    runtime: str = "待机"
    message: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    selected_track: Optional[str] = None

    def snapshot(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "device": self.device,
            "action": self.current_action,
            "status": "executing" if self.busy else "idle",
            "runtime": self.runtime,
            "message": self.message,
            "params": dict(self.params),
        }
        if self.selected_track:
            payload["selected_track"] = self.selected_track
        return payload


# 进程内单例（每个设备进程独立）
_states: Dict[str, DeviceRuntimeState] = {}


def get_runtime_state(device: str) -> DeviceRuntimeState:
    if device not in _states:
        _states[device] = DeviceRuntimeState(device=device)
    return _states[device]
