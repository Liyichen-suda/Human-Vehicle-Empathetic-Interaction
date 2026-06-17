"""设备命令队列执行器：串行执行、优先级、语音强制打断。"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from devices.runtime_state import get_runtime_state

HandlerFn = Callable[[str, Dict[str, Any]], Dict[str, Any]]
StatusFn = Callable[[Dict[str, Any]], None]


@dataclass(order=True)
class _QueuedCommand:
    priority: int
    seq: int
    action: str = field(compare=False)
    data: Dict[str, Any] = field(compare=False, default_factory=dict)


class DeviceCommandExecutor:
    """单设备命令执行器。"""

    def __init__(
        self,
        device_name: str,
        handler: HandlerFn,
        publish_status: StatusFn,
        max_queue: int = 5,
    ):
        self.device_name = device_name
        self.handler = handler
        self.publish_status = publish_status
        self.max_queue = max_queue
        self.state = get_runtime_state(device_name)
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = 0
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run_worker,
            daemon=True,
            name=f"{device_name}-executor",
        )
        self._worker.start()

    def submit(self, action: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = dict(data or {})
        force = bool(data.get("force")) or data.get("source") in ("voice", "manual_force")
        priority = -10 if force else 0

        with self._lock:
            if (
                not force
                and self.state.busy
                and action == self.state.current_action
                and action not in ("关闭", "维持不变")
            ):
                return {
                    "status": "skipped",
                    "runtime": self.state.runtime,
                    "message": f"相同动作执行中，已忽略: {action}",
                }

            if force:
                self._drain_queue()

            queued = self.state.busy or not self._queue.empty()
            if not force and queued and self._queue.qsize() >= self.max_queue:
                return {
                    "status": "rejected",
                    "runtime": "队列已满",
                    "message": f"命令队列已满({self.max_queue})，请稍后再试",
                }

            self._seq += 1
            self._queue.put(_QueuedCommand(priority, self._seq, action, data))

        if force:
            msg = f"语音/强制命令已受理: {action}"
            runtime = "执行中"
        elif queued:
            msg = f"已排队: {action}（当前: {self.state.current_action}）"
            runtime = "排队中"
        else:
            msg = f"命令已受理: {action}"
            runtime = "执行中"

        return {
            "status": "queued" if queued and not force else "accepted",
            "runtime": runtime,
            "message": msg,
            "queue_size": self._queue.qsize(),
        }

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except queue.Empty:
                break

    def _execute_now(self, action: str, data: Dict[str, Any]) -> None:
        self.state.busy = True
        self.state.current_action = action
        self.state.runtime = "执行中"
        self.state.message = f"正在执行: {action}"
        self.publish_status(
            {
                "device": self.device_name,
                "online": True,
                "action": action,
                "status": "executing",
                "runtime": "执行中",
                "message": self.state.message,
                "source": data.get("source", "mqtt"),
            }
        )

        try:
            data["force"] = bool(data.get("force")) or data.get("source") in ("voice", "manual_force")
            result = self.handler(action, data) or {}
        except Exception as exc:
            result = {
                "status": "error",
                "runtime": "执行失败",
                "message": str(exc),
            }

        self.state.busy = False
        self.state.runtime = result.get("runtime", "完成")
        self.state.message = result.get("message", f"已执行: {action}")
        self.state.params = result.get("params", {})
        if result.get("selected_track"):
            self.state.selected_track = result["selected_track"]
        if action == "关闭":
            self.state.current_action = "关闭"

        payload = {
            "device": self.device_name,
            "online": True,
            "action": action,
            "status": result.get("status", "done"),
            "runtime": self.state.runtime,
            "message": self.state.message,
            "source": data.get("source", "mqtt"),
            **{k: v for k, v in result.items() if k not in ("device", "action", "status", "runtime", "message")},
        }
        self.publish_status(payload)

    def _run_worker(self) -> None:
        while True:
            try:
                item: _QueuedCommand = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._execute_now(item.action, item.data)
            self._queue.task_done()
