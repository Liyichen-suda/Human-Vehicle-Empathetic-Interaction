"""音乐设备 — 队列播放、语音强制切歌、同动作去重。"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np

try:
    import pygame  # type: ignore
except Exception:
    pygame = None

from devices.runtime_state import get_runtime_state


class MusicPlayer:
    def __init__(self, music_root: Path | str):
        self.music_root = Path(music_root)
        self.audio_exts = {".mp3", ".wav", ".flac", ".m4a", ".ogg"}
        self.state = get_runtime_state("music")
        self.pygame_available = pygame is not None
        self._play_lock = threading.Lock()
        self._monitor_stop = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def _is_playing(self) -> bool:
        if self.pygame_available and pygame is not None and pygame.mixer.get_init():
            return bool(pygame.mixer.music.get_busy())
        return self.state.runtime == "播放中" and self.state.current_action not in ("关闭",)

    def _stop_playback(self) -> None:
        if self.pygame_available and pygame is not None and pygame.mixer.get_init():
            pygame.mixer.music.stop()
        self.state.selected_track = None
        self._monitor_stop.set()

    def _start_monitor(self) -> None:
        self._monitor_stop.clear()

        def loop() -> None:
            while not self._monitor_stop.wait(1.0):
                if not self._is_playing() and self.state.runtime == "播放中":
                    self.state.runtime = "播放完成"
                    self.state.message = "当前曲目播放完毕"
                    break

        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_stop.set()
        self._monitor_thread = threading.Thread(target=loop, daemon=True, name="music-monitor")
        self._monitor_thread.start()

    def _play(self, track_path: str, force: bool = False) -> bool:
        if not track_path or not os.path.exists(track_path):
            return False

        with self._play_lock:
            if (
                not force
                and track_path == self.state.selected_track
                and self._is_playing()
            ):
                return True

            if self.pygame_available and pygame is not None:
                try:
                    if not pygame.mixer.get_init():
                        pygame.mixer.init()
                    pygame.mixer.music.stop()
                    pygame.mixer.music.load(track_path)
                    pygame.mixer.music.play(loops=0)
                    self.state.selected_track = track_path
                    self._start_monitor()
                    return True
                except Exception as exc:
                    print(f"[music设备] pygame 播放失败: {exc}")
                    self.pygame_available = False

            try:
                os.startfile(track_path)
                self.state.selected_track = track_path
                self._start_monitor()
                return True
            except Exception as exc:
                print(f"[music设备] 播放失败: {track_path}, err={exc}")
                return False

    def execute(self, action: str, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = data or {}
        force = bool(data.get("force"))
        info: Dict[str, Any] = {
            "device": "music",
            "action": action,
            "status": "idle",
            "runtime": "待机",
            "message": "",
        }

        if action == "关闭":
            self._stop_playback()
            self.state.current_action = "关闭"
            info["status"] = "stopped"
            info["runtime"] = "已关闭"
            info["message"] = "音乐设备已停止播放"
            print("[music设备] 停止播放")
            return info

        if (
            not force
            and action == self.state.current_action
            and self._is_playing()
        ):
            info["status"] = "playing"
            info["runtime"] = "播放中"
            info["message"] = "相同动作，继续当前播放"
            if self.state.selected_track:
                info["selected_track"] = Path(self.state.selected_track).name
            print(f"[music设备] 继续播放: {action}")
            return info

        if force and self._is_playing():
            self._stop_playback()
            time.sleep(0.15)

        target_dir = self.music_root / action
        if not target_dir.exists():
            info["status"] = "missing_dir"
            info["runtime"] = "未配置"
            info["message"] = f"请创建目录: {target_dir}"
            print(f"[music设备] {info['message']}")
            return info

        tracks = [p for p in target_dir.iterdir() if p.is_file() and p.suffix.lower() in self.audio_exts]
        if not tracks:
            info["status"] = "empty_dir"
            info["runtime"] = "无音频"
            info["message"] = f"目录为空: {target_dir}"
            print(f"[music设备] {info['message']}")
            return info

        selected = np.random.choice(tracks)
        info["selected_track"] = selected.name
        info["message"] = f"正在播放: {selected.name}"

        if self._play(str(selected), force=force):
            self.state.current_action = action
            info["status"] = "playing"
            info["runtime"] = "播放中"
            print(f"[music设备] 播放: {selected.name} (动作={action}, force={force})")
        else:
            info["status"] = "play_failed"
            info["runtime"] = "播放失败"
            print(f"[music设备] 播放失败: {selected}")

        return info
