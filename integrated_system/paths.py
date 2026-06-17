"""integrated_system 根路径常量。"""

import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

ROOT = APP_DIR.parent
MODELS_DIR = APP_DIR / "models"
LOGS_DIR = APP_DIR / "logs"
WEB_TEMPLATES = APP_DIR / "web" / "templates"
CENTER_CONTROL_ROOT = ROOT / "center_control"
