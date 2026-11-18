# -*- coding: utf-8 -*-
"""
Builds MidasUplink.exe (PyInstaller, one file, no console window).

    pip install -r requirements.txt pyinstaller
    python build.py

Output: dist\\MidasUplink.exe, work files in build\\.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
NAME = "MidasUplink"


def data_args():
    """Icon -> assets, window files -> ui (the folders tray.py and ui_server.py look in)."""
    out = ["--add-data", f"{HERE / 'assets' / 'midas_uplink.ico'}{os.pathsep}assets"]
    for f in sorted((HERE / "ui").iterdir()):
        if f.is_file() and f.suffix in (".html", ".css", ".js", ".png"):
            out += ["--add-data", f"{f}{os.pathsep}ui"]
    return out


def main():
    import PyInstaller.__main__
    PyInstaller.__main__.run([
        str(HERE / "tray.py"),
        "--name", NAME,
        "--onefile", "--windowed", "--noconfirm", "--clean",
        "--icon", str(HERE / "assets" / "midas_uplink.ico"),
        *data_args(),
        "--hidden-import", "pystray._win32",
        "--hidden-import", "ui_server",
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pydoc",
        "--paths", str(HERE),
        "--distpath", str(HERE / "dist"),
        "--workpath", str(HERE / "build" / "work"),
        "--specpath", str(HERE / "build"),
    ])
    print(HERE / "dist" / f"{NAME}.exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
