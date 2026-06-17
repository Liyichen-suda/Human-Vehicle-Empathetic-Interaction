"""音乐设备 — 订阅 cabin/music/control，收到命令后本地播放。"""

from __future__ import annotations

import atexit
import signal
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_base import load_runtime, resolve_music_root, run_device
from devices.music_player import MusicPlayer

_player: Optional[MusicPlayer] = None


def _get_player() -> MusicPlayer:
    global _player
    if _player is None:
        cfg = load_runtime()
        _player = MusicPlayer(resolve_music_root(cfg))
    return _player


def _stop_music_on_exit(*_args) -> None:
    global _player
    if _player is None:
        return
    try:
        _player.execute("关闭", {})
    except Exception:
        pass


def handle(action: str, data: dict, client) -> dict:
    return _get_player().execute(action, data)


def _handle_signal(signum, frame) -> None:  # noqa: ARG001
    _stop_music_on_exit()
    sys.exit(0)


if __name__ == "__main__":
    atexit.register(_stop_music_on_exit)
    for sig_name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            signal.signal(sig, _handle_signal)
    run_device("music", handle)
