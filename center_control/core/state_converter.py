"""9 维用户状态校验与场景样本加载。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

EMOTION_KEYS = ["平静", "恐惧", "惊讶", "开心", "愤怒", "轻蔑", "悲伤", "厌恶"]

# 英文 FER 标签 -> 中文（与可视化前端一致）
EMOTION_EN_TO_CN = {
    "happy": "开心",
    "happiness": "开心",
    "sad": "悲伤",
    "sadness": "悲伤",
    "angry": "愤怒",
    "anger": "愤怒",
    "fear": "恐惧",
    "surprise": "惊讶",
    "neutral": "平静",
    "disgust": "厌恶",
    "contempt": "轻蔑",
}


def normalize_emotion_probs(emotion_probs: dict[str, float]) -> dict[str, float]:
    """将情绪概率字典统一为中文键，值为 0-100 百分比。"""
    normalized: dict[str, float] = {key: 0.0 for key in EMOTION_KEYS}
    for key, value in emotion_probs.items():
        cn_key = EMOTION_EN_TO_CN.get(str(key).lower(), str(key))
        if cn_key in normalized:
            normalized[cn_key] += float(value)
    return normalized


def emotion_probs_to_state(emotion_probs: dict[str, float], fatigue: float = 50.0) -> list[float]:
    """
    将情绪检测结果转换为 9 维状态向量，供 RL 模型推理。

    Args:
        emotion_probs: 情绪概率，键为中文或英文，值为 0-100 或 0-1
        fatigue: 疲劳度 0-100
    """
    probs = normalize_emotion_probs(emotion_probs)
    state = [0.0] * 9

    for i, emotion_cn in enumerate(EMOTION_KEYS):
        raw = probs.get(emotion_cn, 0.0)
        state[i] = raw / 100.0 if raw > 1.0 else raw

    emotion_sum = sum(state[:8])
    if emotion_sum > 0:
        state[:8] = [x / emotion_sum for x in state[:8]]
    else:
        state[:8] = [1.0 / 8.0] * 8

    state[8] = max(0.0, min(100.0, float(fatigue)))
    return validate_state(state)


def validate_state(state: list[float]) -> list[float]:
    """校验 9 维状态：[8 维情绪分布 + 疲劳度]。"""
    if len(state) != 9:
        raise ValueError(
            "用户状态必须是 9 维：[平静, 恐惧, 惊讶, 开心, 愤怒, 轻蔑, 悲伤, 厌恶, 疲劳]"
        )
    return [float(x) for x in state]


def load_scene_samples(path: Path | str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_scene_state(scene: dict[str, Any]) -> list[float]:
    state = scene.get("state")
    if not state:
        raise ValueError(f"场景 {scene.get('name')} 缺少 state 字段")
    return validate_state(state)
