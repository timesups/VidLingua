"""Show the native folder dialog in a separate process for the local web UI."""

from __future__ import annotations

import json
from tkinter import Tk, filedialog


def main() -> None:
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update()
    try:
        selected = filedialog.askdirectory(parent=root, title="选择批处理视频文件夹", mustexist=True)
        print(json.dumps(selected or None, ensure_ascii=True), flush=True)
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
