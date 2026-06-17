"""情绪概率多帧平滑，降低单帧噪声对推理/在线学习的影响。"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict, Optional


class EmotionSmoother:
    def __init__(self, window_size: int = 8, min_samples: int = 2):
        self.window_size = max(1, window_size)
        self.min_samples = max(1, min_samples)
        self._buffer: Deque[Dict[str, float]] = deque(maxlen=self.window_size)

    def push(self, probs: Dict[str, float]) -> None:
        if probs:
            self._buffer.append(dict(probs))

    def get_smoothed(self) -> Optional[Dict[str, float]]:
        if len(self._buffer) < self.min_samples:
            if self._buffer:
                return dict(self._buffer[-1])
            return None

        keys = set()
        for item in self._buffer:
            keys.update(item.keys())

        averaged: Dict[str, float] = {}
        n = len(self._buffer)
        for key in keys:
            averaged[key] = sum(item.get(key, 0.0) for item in self._buffer) / n
        return averaged

    def clear(self) -> None:
        self._buffer.clear()

    @property
    def sample_count(self) -> int:
        return len(self._buffer)
