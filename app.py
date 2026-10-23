"""WordPress 站点批量备份工具 - 入口。"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from ui.main_window import MainWindow  # noqa: E402


def main():
    app = MainWindow()
    app.mainloop()


if __name__ == "__main__":
    main()
