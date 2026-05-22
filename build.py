#!/usr/bin/env python3
import platform
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def main():
    name = "oi-ftp"
    if platform.system().lower().startswith("win"):
        name += ".exe"
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--name",
        "oi-ftp",
        str(ROOT / "server.py"),
    ]
    print("将执行：", " ".join(cmd))
    print("如提示缺少 PyInstaller，请先运行：python -m pip install pyinstaller")
    subprocess.check_call(cmd, cwd=ROOT)
    print()
    print("打包完成：", ROOT / "dist" / name)
    print("把该文件放到任意目录双击运行，会在同目录创建 ftp_data。")


if __name__ == "__main__":
    main()
