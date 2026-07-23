#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py - エントリーポイント

使い方:
    python main.py
"""

import sys
import os

# main.py のある場所（アプリのルートフォルダ）を基準に解決する
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    _app_dir = sys._MEIPASS
else:
    _app_dir = os.path.dirname(os.path.abspath(__file__))

if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

# カレントディレクトリをアプリルートに設定
# (config.json 等の相対パスがどこから実行しても正しく解決される)
os.chdir(_app_dir)

from modules.app import ClearViewApp  # noqa: E402


if __name__ == "__main__":
    app = ClearViewApp()
    app.root.mainloop()