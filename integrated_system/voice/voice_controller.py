"""语音/文本命令解析与设备控制。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# device -> 动作列表（与 RL 模型 v3.1 一致）
DEVICE_ACTIONS: Dict[str, List[str]] = {
    "music": ["镇静", "提振", "稳定", "共情", "专注", "关闭"],
    "ac": ["轻微降温", "轻微升温", "维持不变", "增强送风", "减弱送风", "关闭"],
    "light": ["提亮冷光", "提亮暖光", "降暗暖光", "降暗冷光", "柔和动态", "维持不变", "关闭"],
    "aroma": ["舒缓", "平静", "提神", "关闭"],
    "massage": ["轻柔颈肩放松", "稳定背部舒缓", "全身恢复", "深层缓解", "轻度提神", "关闭"],
}

DEVICE_ALIASES = {
    "music": ["音乐", "歌曲", "播放"],
    "ac": ["空调", "冷气", "温度"],
    "light": ["灯", "灯光", "照明", "光"],
    "aroma": ["香薰", "香氛", "气味"],
    "massage": ["按摩", "座椅"],
}

ACTION_ALIASES: Dict[str, str] = {
    "降温": "轻微降温",
    "升温": "轻微升温",
    "送风": "增强送风",
    "开灯": "提亮暖光",
    "关灯": "关闭",
    "停止": "关闭",
    "关掉": "关闭",
    "关闭所有": "关闭",
    "播放": "稳定",
}


@dataclass
class VoiceCommand:
    device: str
    action: str
    raw_text: str
    confidence: float = 1.0


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text.strip().lower())


def _find_device(text: str) -> Optional[str]:
    for device, aliases in DEVICE_ALIASES.items():
        for alias in aliases:
            if alias in text:
                return device
    return None


def _find_action(device: str, text: str) -> Optional[str]:
    for action in DEVICE_ACTIONS.get(device, []):
        if action in text:
            return action
    for alias, action in ACTION_ALIASES.items():
        if alias in text:
            if action == "关闭" or action in DEVICE_ACTIONS.get(device, []):
                return action if action in DEVICE_ACTIONS.get(device, []) else "关闭"
    return None


VOSK_KNOWN_WORDS: Tuple[str, ...] = (
    "播放",
    "稳定",
    "镇静",
    "提振",
    "专注",
    "共情",
    "关闭",
    "关掉",
    "停止",
    "降温",
    "升温",
    "送风",
    "开灯",
    "关灯",
    "舒缓",
    "平静",
    "提神",
)


def build_command_grammar() -> List[str]:
    """仅使用 Vosk 小模型词表内存在的词，否则语法模式无效。"""
    phrases: set[str] = set(VOSK_KNOWN_WORDS)
    # 两词组合（模型支持时才有效）
    for w1, w2 in (
        ("播放", "稳定"),
        ("播放", "镇静"),
        ("播放", "关闭"),
    ):
        phrases.add(w1 + w2)
    return sorted(phrases)


VOICE_KEYWORDS: Dict[str, Tuple[str, str]] = {
    "播放稳定音乐": ("music", "稳定"),
    "播放稳定": ("music", "稳定"),
    "音乐关闭": ("music", "关闭"),
    "音乐稳定": ("music", "稳定"),
    "空调降温": ("ac", "轻微降温"),
    "空调升温": ("ac", "轻微升温"),
    "关闭所有": ("all", "关闭"),
    "全部关闭": ("all", "关闭"),
    "稳定": ("music", "稳定"),
    "镇静": ("music", "镇静"),
    "提振": ("music", "提振"),
    "专注": ("music", "专注"),
    "共情": ("music", "共情"),
    "播放": ("music", "稳定"),
    "降温": ("ac", "轻微降温"),
    "升温": ("ac", "轻微升温"),
    "送风": ("ac", "增强送风"),
    "开灯": ("light", "提亮暖光"),
    "关灯": ("light", "关闭"),
    "关闭": ("all", "关闭"),
    "关掉": ("all", "关闭"),
    "停止": ("all", "关闭"),
    "舒缓": ("aroma", "舒缓"),
    "平静": ("aroma", "平静"),
    "提神": ("aroma", "提神"),
}


def _match_voice_keywords(normalized: str, raw: str) -> Optional[VoiceCommand]:
    for kw in sorted(VOICE_KEYWORDS.keys(), key=len, reverse=True):
        if kw in normalized:
            dev, action = VOICE_KEYWORDS[kw]
            return VoiceCommand(device=dev, action=action, raw_text=raw)
    return None


def parse_voice_text(text: str) -> Optional[VoiceCommand]:
    """解析中文语音/文本为 (device, action)。"""
    if not text or not text.strip():
        return None

    raw = text.strip()
    normalized = _normalize(raw)

    if any(k in normalized for k in ("全部关闭", "关闭所有", "全关", "停止所有")):
        return VoiceCommand(device="all", action="关闭", raw_text=raw)

    matched = _match_voice_keywords(normalized, raw)
    if matched:
        return matched

    device = _find_device(normalized)
    if device is None:
        matches = []
        for dev, actions in DEVICE_ACTIONS.items():
            for action in actions:
                if action in normalized:
                    matches.append((dev, action))
        if len(matches) == 1:
            dev, action = matches[0]
            return VoiceCommand(device=dev, action=action, raw_text=raw)
        if len(matches) > 1:
            unique_actions = {a for _, a in matches}
            if len(unique_actions) == 1:
                only = next(iter(unique_actions))
                if only == "关闭":
                    return VoiceCommand(device="all", action="关闭", raw_text=raw)
                dev, _ = matches[0]
                return VoiceCommand(device=dev, action=only, raw_text=raw)
        return _match_voice_keywords(normalized, raw)

    action = _find_action(device, normalized)
    if action is None:
        if "关闭" in normalized or "关" in normalized:
            action = "关闭"
        elif device == "music" and any(k in normalized for k in ("播放", "放", "来首")):
            action = "稳定"
        else:
            return _match_voice_keywords(normalized, raw)

    return VoiceCommand(device=device, action=action, raw_text=raw)


def diagnose_voice_setup(model_path: Path | None = None, voice_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """检测离线语音环境。"""
    from .voice_offline import default_model_dir, diagnose_offline_voice

    cfg = voice_cfg or {}
    path = model_path or default_model_dir()
    return diagnose_offline_voice(
        path,
        stt_engine=str(cfg.get("stt_engine", "whisper")),
        whisper_model=str(cfg.get("whisper_model", "base")),
    )


def transcribe_audio_wav(
    wav_bytes: bytes,
    model_path: Path | None = None,
    voice_cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[str, str]:
    """离线 WAV → 文本（Whisper 主引擎 + Vosk 备用）。"""
    from .voice_offline import default_model_dir, transcribe_wav_bytes, _wav_audio_stats

    cfg = voice_cfg or {}
    path = model_path or default_model_dir()
    grammar = build_command_grammar()
    text, mode = transcribe_wav_bytes(
        wav_bytes,
        path,
        grammar=grammar,
        stt_engine=str(cfg.get("stt_engine", "whisper")),
        whisper_model=str(cfg.get("whisper_model", "base")),
        whisper_compute_type=str(cfg.get("whisper_compute_type", "int8")),
    )
    if not text:
        stats = _wav_audio_stats(wav_bytes)
        peak_pct = stats["peak"] * 100
        if stats["peak"] < 0.008:
            raise RuntimeError(
                f"录音几乎无声（音量 {peak_pct:.1f}%），请先点「测麦克风」确认设备正常"
            )
        clip_hint = ""
        if stats["peak"] >= 0.98:
            clip_hint = "（音量 100% 可能失真，请降低系统麦克风音量到 70% 左右）"
        raise RuntimeError(
            f"已录到声音（音量 {peak_pct:.0f}%，{stats['duration_s']:.1f}秒）{clip_hint}但未识别出文字。"
            "建议：① 把系统麦克风音量调到 70% 左右 ② 运行 python scripts/download_vosk_model.py --model cn 下载大模型 "
            "③ 或 pip install faster-whisper 后 config.yaml 设 stt_engine: whisper "
            "④ 直接用文本框输入「播放稳定音乐」。"
        )
    return text, mode


def get_command_help() -> Dict[str, Any]:
    examples = [
        "播放稳定音乐",
        "空调降温",
        "关闭所有",
        "稳定",
        "开灯",
    ]
    return {
        "examples": examples,
        "examples_text": [
            "播放稳定音乐",
            "空调降温",
            "音乐关闭",
            "关闭所有",
        ],
        "devices": DEVICE_ALIASES,
        "actions": DEVICE_ACTIONS,
        "diagnose_hint": "GET /api/voice/diagnose 可检测离线模型是否就绪",
        "flow": "浏览器录音 → Whisper 离线识别 → 强制控制设备",
        "tip": "可说完整命令：播放稳定音乐、空调降温、关闭所有",
    }
