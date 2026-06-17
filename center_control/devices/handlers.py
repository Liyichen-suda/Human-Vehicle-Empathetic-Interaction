"""各模拟设备的 MQTT 命令处理逻辑（带持久状态）。"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict

from devices.runtime_state import get_runtime_state

AC_MAP = {
    "轻微降温": {"mode": "cool", "temp": 22, "fan": "auto"},
    "轻微升温": {"mode": "heat", "temp": 26, "fan": "auto"},
    "维持不变": {"mode": "auto", "temp": 24, "fan": "auto"},
    "增强送风": {"mode": "fan", "temp": 24, "fan": "high"},
    "减弱送风": {"mode": "fan", "temp": 24, "fan": "low"},
    "关闭": {"mode": "off", "temp": None, "fan": "off"},
}

LIGHT_MAP = {
    "提亮冷光": {"brightness": 85, "color_temp": 6500, "on": True},
    "提亮暖光": {"brightness": 80, "color_temp": 3000, "on": True},
    "降暗暖光": {"brightness": 30, "color_temp": 3000, "on": True},
    "降暗冷光": {"brightness": 25, "color_temp": 6500, "on": True},
    "柔和动态": {"brightness": 50, "effect": "colorloop", "on": True},
    "维持不变": None,
    "关闭": {"on": False},
}

AROMA_MAP = {
    "舒缓": {"scent": "lavender", "seconds": 3},
    "平静": {"scent": "chamomile", "seconds": 3},
    "提神": {"scent": "mint", "seconds": 2},
    "关闭": None,
}

MASSAGE_MAP = {
    "轻柔颈肩放松": {"zone": "neck", "intensity": "light", "minutes": 3},
    "稳定背部舒缓": {"zone": "back", "intensity": "medium", "minutes": 3},
    "全身恢复": {"zone": "full", "intensity": "medium", "minutes": 3},
    "深层缓解": {"zone": "full", "intensity": "deep", "minutes": 3},
    "轻度提神": {"zone": "shoulder", "intensity": "light", "minutes": 2},
    "关闭": None,
}


def _base(device: str, action: str, **extra: Any) -> Dict[str, Any]:
    return {"device": device, "action": action, **extra}


def _run_timed_task(device: str, action: str, seconds: int, label: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """在后台线程执行定时任务，handler 立即返回「运行中」。"""
    state = get_runtime_state(device)

    def worker() -> None:
        state.busy = True
        state.runtime = "运行中"
        for i in range(seconds):
            if state.current_action != action:
                return
            print(f"  [{device}设备] {label}... {i + 1}/{seconds}")
            time.sleep(1)
        state.busy = False
        state.runtime = "运行中"
        state.message = f"{label}完成"

    if state.busy and action != state.current_action:
        state.message = f"已切换任务，停止前一动作"
    threading.Thread(target=worker, daemon=True, name=f"{device}-task").start()
    return _base(
        device,
        action,
        status="active",
        runtime="运行中",
        message=f"已开始{label}（约{seconds}秒）",
        params=params,
    )


def handle_ac(action: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = get_runtime_state("ac")
    cfg = AC_MAP.get(action)
    if not cfg:
        return _base("ac", action, status="unknown", runtime="未知", message=f"未知动作: {action}")

    if action == "维持不变" and state.params:
        print("[ac设备] 保持当前空调")
        return _base("ac", action, status="idle", runtime="待机", message="保持当前空调不变", params=state.params)

    is_off = action == "关闭"
    is_idle = action == "维持不变"
    runtime = "已关闭" if is_off else ("待机" if is_idle else "运行中")
    msg = f"空调 mode={cfg['mode']}, temp={cfg['temp']}, fan={cfg['fan']}"
    print(f"[ac设备] {msg}")
    state.params = cfg
    state.current_action = action
    return _base(
        "ac",
        action,
        status="stopped" if is_off else ("idle" if is_idle else "active"),
        runtime=runtime,
        message=msg,
        params=cfg,
    )


def handle_light(action: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = get_runtime_state("light")
    if action == "维持不变" and state.params:
        print("[light设备] 保持当前灯光")
        return _base("light", action, status="idle", runtime="待机", message="保持当前灯光不变", params=state.params)

    cfg = LIGHT_MAP.get(action)
    if cfg is None:
        return _base("light", action, status="unknown", runtime="未知", message=f"未知动作: {action}")

    is_off = not cfg.get("on", True)
    runtime = "已关闭" if is_off else "运行中"
    msg = f"灯光 brightness={cfg.get('brightness')}, temp={cfg.get('color_temp', '-')}"
    print(f"[light设备] {msg}")
    state.params = cfg
    state.current_action = action
    return _base(
        "light",
        action,
        status="stopped" if is_off else "active",
        runtime=runtime,
        message=msg,
        params=cfg,
    )


def handle_aroma(action: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = get_runtime_state("aroma")
    if action == "关闭":
        state.busy = False
        state.current_action = "关闭"
        print("[aroma设备] 停止喷香")
        return _base("aroma", action, status="stopped", runtime="已关闭", message="香薰已关闭")

    cfg = AROMA_MAP.get(action)
    if not cfg:
        return _base("aroma", action, status="unknown", runtime="未知", message=f"未知动作: {action}")

    state.current_action = action
    print(f"[aroma设备] 喷香 {cfg['scent']}，{cfg['seconds']}秒")
    return _run_timed_task("aroma", action, cfg["seconds"], f"喷香 {cfg['scent']}", cfg)


def handle_massage(action: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
    state = get_runtime_state("massage")
    if action == "关闭":
        state.busy = False
        state.current_action = "关闭"
        print("[massage设备] 停止按摩")
        return _base("massage", action, status="stopped", runtime="已关闭", message="按摩已关闭")

    cfg = MASSAGE_MAP.get(action)
    if not cfg:
        return _base("massage", action, status="unknown", runtime="未知", message=f"未知动作: {action}")

    state.current_action = action
    seconds = cfg["minutes"]
    print(f"[massage设备] 按摩 {cfg['zone']}/{cfg['intensity']}，演示 {seconds}秒")
    return _run_timed_task(
        "massage",
        action,
        seconds,
        f"按摩 {cfg['zone']}/{cfg['intensity']}",
        cfg,
    )
