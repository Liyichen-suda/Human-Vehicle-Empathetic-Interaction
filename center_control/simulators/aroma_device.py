"""香薰设备 — 订阅 cabin/aroma/control"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device_base import run_device
from devices.handlers import handle_aroma


def handle(action: str, data: dict, client) -> dict:
    return handle_aroma(action, data)


if __name__ == "__main__":
    run_device("aroma", handle)
