"""
兼容旧路径 center_control/example/ — 实际代码已迁至 simulators/。

请改用:
  cd center_control/simulators
  python run_all_devices.py

或项目根目录一条命令:

  python run.py

（旧路径 example/ 已弃用，请用 simulators/ 或 run.py）
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

_SIMULATORS = Path(__file__).resolve().parent.parent / "simulators" / "run_all_devices.py"

if not _SIMULATORS.is_file():
    print("错误: 未找到 simulators/run_all_devices.py")
    print("请进入 center_control/simulators 后运行: python run_all_devices.py")
    sys.exit(1)

print("[提示] example/ 已更名为 simulators/，正在转发启动...\n")
runpy.run_path(str(_SIMULATORS), run_name="__main__")
