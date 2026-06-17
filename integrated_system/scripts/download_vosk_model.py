"""下载 Vosk 中文模型（仅需运行一次）。

用法:
  python scripts/download_vosk_model.py          # 小模型 ~42MB
  python scripts/download_vosk_model.py --model cn   # 标准模型 ~1.3GB，识别更准
"""

from __future__ import annotations

import argparse
import shutil
import sys
import zipfile
from pathlib import Path
from urllib.request import urlretrieve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import APP_DIR

MODELS = {
    "small": ("vosk-model-small-cn-0.22", "约 42MB"),
    "cn": ("vosk-model-cn-0.22", "约 1.3GB，识别准确率更高"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description="下载 Vosk 中文语音模型")
    parser.add_argument(
        "--model",
        choices=list(MODELS.keys()),
        default="small",
        help="small=小模型(默认), cn=标准中文模型(推荐识别差时使用)",
    )
    args = parser.parse_args()

    model_name, size_hint = MODELS[args.model]
    target_dir = APP_DIR / "models" / model_name
    model_url = f"https://alphacephei.com/vosk/models/{model_name}.zip"

    target_dir.parent.mkdir(parents=True, exist_ok=True)
    if target_dir.is_dir() and any(target_dir.glob("**/final.mdl")):
        print(f"模型已存在: {target_dir}")
        print(f"请在 config.yaml 设置 vosk_model_path: models/{model_name}")
        return

    zip_path = target_dir.parent / f"{model_name}.zip"
    print(f"正在下载: {model_url}")
    print(f"（{size_hint}，只需下载一次）")

    def progress(block_num: int, block_size: int, total_size: int) -> None:
        if total_size <= 0:
            return
        pct = min(100, block_num * block_size * 100 / total_size)
        print(f"\r进度: {pct:.1f}%", end="", flush=True)

    try:
        urlretrieve(model_url, zip_path, reporthook=progress)
        print("\n下载完成，正在解压...")
    except Exception as exc:
        print(f"\n下载失败: {exc}")
        print("请手动下载并解压到:", target_dir)
        sys.exit(1)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(target_dir.parent)

    if not target_dir.is_dir():
        for child in target_dir.parent.iterdir():
            if child.is_dir() and child.name.startswith("vosk-model") and child != target_dir:
                if child.name == model_name:
                    break
                shutil.move(str(child), str(target_dir))
                break

    zip_path.unlink(missing_ok=True)
    print(f"完成: {target_dir}")
    print(f"请在 config.yaml 设置:")
    print(f'  vosk_model_path: "models/{model_name}"')


if __name__ == "__main__":
    main()
