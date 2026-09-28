#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py - メインアプリケーション (ClearViewApp)
パターン判定なし・撮影特化型カメラシステム
"""

import cv2
import csv
import datetime
import logging
import os
import sys
import threading
import time
import queue
import re
import shutil
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

from PIL import Image, ImageTk

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT,
    COLOR_OK, COLOR_NG, COLOR_WARNING,
    FONT_BOLD, FONT_LARGE, FONT_HUGE, FONT_NORMAL, FONT_FAMILY,
    VERSION, PREVIEW_FPS
)
from .settings import SettingsManager
from .widgets import create_card, Tooltip, HelpWindow, TenKeyDialog, get_commit_display_style
from .dialogs import SettingsDialog

from .hardware import DigitalInputDevice, OutputDevice, is_gpio_available, MockManager


class ClearViewApp:
    """ClearView - 撮影専用カメラシステム"""

    def __init__(self):
        self.settings = SettingsManager()
        self.commit_number = 1.0
        self.running = True
        self.camera_lock = threading.Lock()
        self._save_lock = threading.Lock()
        self.trigger_queue = queue.Queue()
        self.save_queue = queue.Queue()
        self.caps = {}
        self.caps_opened_res = {}
        self.last_frames = {}
        self.preview_paused = False
        self.inspecting = False
        self.settings_open = False
        self.result_display_until = 0.0

        # 画像保存ワーカースレッドの起動 (8K時の多重スレッド乱立・メモリ枯渇防止)
        self.save_worker_thread = threading.Thread(target=self._save_worker_loop, daemon=True)
        self.save_worker_thread.start()

        # GPIO デバイス
        self.trig_device = None
        self.out_ok = None
        self.out_ng = None
        self.out_running = None

        self.setup_dirs()
        self.setup_logging()
        self.setup_hardware()
        self.setup_gui()

        # 起動時コミット番号入力 (起動後少し待って表示)
        self.root.after(500, self.manual_commit_set_initial)
        # 容量監視 (起動30秒後から開始、以降10分おき)
        self.root.after(30 * 1000, self._monitor_storage)

        # 仮想GPIOパネルの起動 (物理GPIOが無効な環境の場合のみ)
        if not is_gpio_available():
            self.root.after(1000, self.setup_mock_ui)

    # ------------------------------------------------------------------
    # 初期設定
    # ------------------------------------------------------------------
    def setup_dirs(self):
        paths = self.settings.data.get("paths", {})
        Path(paths.get("log_dir", "./logs")).mkdir(parents=True, exist_ok=True)
        Path(paths.get("results_dir", "./results")).mkdir(parents=True, exist_ok=True)

    def setup_logging(self):
        log_dir = Path(self.settings.data["paths"].get("log_dir", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"app_{datetime.datetime.now().strftime('%Y%m%d')}.log"
        # 既存ハンドラをクリアして再設定
        for handler in logging.root.handlers[:]:
            logging.root.removeHandler(handler)
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[
                logging.FileHandler(log_file, encoding="utf-8"),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    def setup_hardware(self):
        """GPIO & カメラの初期化"""
        prev_paused = getattr(self, "preview_paused", False)
        self.preview_paused = True
        try:
            # 旧リソースの解放
            if self.trig_device:
                try: self.trig_device.close()
                except Exception: pass
            if self.out_ok:
                try: self.out_ok.close()
                except Exception: pass
            if self.out_ng:
                try: self.out_ng.close()
                except Exception: pass
            if self.out_running:
                try: self.out_running.close()
                except Exception: pass
            self._release_cameras()

            gpio = self.settings.data.get("gpio", {})
            cameras = self.settings.data.get("cameras", [])

            # GPIO 入力 (内部プルアップ常時有効・Active Low)
            trig_pin = gpio.get("trigger_pin", 22)
            self.trig_device = DigitalInputDevice(trig_pin, pull_up=True, bounce_time=0.05)
            self.trig_device.when_activated = self._on_trigger

            # GPIO 出力
            self.out_ok = OutputDevice(gpio.get("output_ok_pin", 16))
            self.out_ng = OutputDevice(gpio.get("output_ng_pin", 20))
            self.out_running = OutputDevice(gpio.get("system_running_pin", 5))
            self.out_running.on()

            # Linux環境のカメラ by_path 自動マイグレーション（未設定の場合に物理USBポートを自動記憶）
            if sys.platform.startswith("linux"):
                by_path_dir = "/dev/v4l/by-path"
                if os.path.exists(by_path_dir):
                    by_path_map = {}
                    try:
                        for fname in sorted(os.listdir(by_path_dir)):
                            full_p = os.path.join(by_path_dir, fname)
                            real_p = os.path.realpath(full_p)
                            if "index0" in fname or real_p not in by_path_map:
                                by_path_map[real_p] = full_p
                    except Exception:
                        pass
                    
                    migrated = False
                    for c in cameras:
                        if not c.get("by_path"):
                            c_idx = c.get("capture_device", 0)
                            dev_node = f"/dev/video{c_idx}"
                            if dev_node in by_path_map:
                                c["by_path"] = by_path_map[dev_node]
                                migrated = True
                                self.logger.info(f"カメラ '{c.get('name')}' の物理USBポート(by_path)を自動登録しました: {c['by_path']}")
                    if migrated:
                        self.settings.save()

            for camera in cameras:
                self._open_camera(camera)
        except Exception as e:
            self.logger.error(f"ハードウェア初期化エラー: {e}")
        finally:
            self.preview_paused = prev_paused

    def _release_cameras(self):
        for cap in self.caps.values():
            try:
                if cap and cap.isOpened():
                    cap.release()
            except Exception:
                pass
        self.caps = {}
        self.caps_opened_res = {}

    def _camera_configs(self):
        """設定済みカメラの一覧を返す。設定未作成時も安全に空配列を返す。"""
        return self.settings.data.get("cameras", [])

    @staticmethod
    def _safe_camera_name(name, fallback):
        """ユーザー設定の名称をWindowsで使えるファイル名の要素へ変換する。"""
        safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(name)).strip(" .")
        return safe_name or fallback

    def _open_camera_device(self, camera, target_res_str=None):
        """指定された解像度で VideoCapture をオープンしプロパティを適用する。"""
        camera_id = camera["id"]
        device_idx = int(camera.get("capture_device", 0))

        # Linux (Raspberry Pi OS等) 環境で by_path が設定されている場合は動的に実ノードを解決
        by_path = camera.get("by_path")
        if sys.platform.startswith("linux") and by_path:
            if os.path.exists(by_path):
                try:
                    real_p = os.path.realpath(by_path)
                    bname = os.path.basename(real_p)
                    if bname.startswith("video") and bname[5:].isdigit():
                        resolved_idx = int(bname[5:])
                        if resolved_idx != device_idx:
                            self.logger.info(
                                f"カメラ '{camera.get('name', camera_id)}' のインデックスを物理ポートから動的解決: "
                                f"{device_idx} -> {resolved_idx} ({by_path})"
                            )
                        device_idx = resolved_idx
                except Exception as e:
                    self.logger.warning(f"by_path解決エラー: {e}")
            else:
                self.logger.warning(
                    f"カメラ '{camera.get('name', camera_id)}' の物理ポートが見つかりません: {by_path} "
                    f"(インデックス {device_idx} でフォールバック試行)"
                )

        if sys.platform.startswith("linux"):
            backend = cv2.CAP_V4L2
        elif sys.platform.startswith("win"):
            backend = cv2.CAP_DSHOW
        else:
            backend = cv2.CAP_ANY
        cap = cv2.VideoCapture(device_idx, backend)
        if not cap.isOpened():
            self.logger.warning(f"カメラ '{camera.get('name', camera_id)}' (インデックス {device_idx}) をオープンできませんでした。")
            return None

        res_str = target_res_str or camera.get("preview_resolution", "640x480")
        parsed = self._parse_resolution(res_str)

        # 先に MJPG をセットして帯域制限・非圧縮YUYVによる8K拒否を回避
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if parsed:
            cw, ch = parsed
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, cw)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, ch)

        # 解像度設定直後にも再度 MJPG フォーマットを強制（V4L2リセット防止）
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        self._apply_camera_props(cap, camera.get("camera_props", {}))
        return cap

    def _open_camera(self, camera):
        """カメラを初期オープンする。
        本撮影解像度が4K以下のストリーミング可能な解像度の場合は、
        inspection_app と同様に最初から本撮影解像度でオープンして再オープン（セグフォ要因）をゼロにする。
        8K (48MP) などストリーミング非対応の巨大解像度の場合のみプレビュー解像度で開く。
        """
        capt_res = camera.get("capture_resolution", "1920x1080").split(" ")[0]
        parsed = self._parse_resolution(capt_res)
        is_huge_8k = False
        if parsed:
            w, h = parsed
            if w * h >= 7680 * 4320:  # 8K以上
                is_huge_8k = True

        init_res = camera.get("preview_resolution", "1280x720").split(" ")[0] if is_huge_8k else capt_res
        cap = self._open_camera_device(camera, init_res)
        if cap:
            self.caps[camera["id"]] = cap
            self.caps_opened_res[camera["id"]] = init_res
            self.logger.info(f"カメラ '{camera.get('name', camera['id'])}' を初期化しました（インデックス: {camera.get('capture_device', 0)}, 解像度: {init_res}）。")

    def _on_trigger(self):
        """GPIOトリガー受信時のコールバック"""
        self.logger.info("トリガーイベント検知")
        self.trigger_queue.put("trigger")

    def test_trigger_input(self):
        """GPIO設定画面からの手動テスト入力"""
        self.logger.info("手動テスト・トリガー発火")
        if hasattr(self, "trig_device") and hasattr(self.trig_device, "toggle"):
            self.trig_device.toggle()
        else:
            self._on_trigger()

    def _apply_camera_props(self, cap, props: dict):
        """カメラプロパティを適用する（オート制御と手動値を連動）。"""
        prop_map = {
            "focus":      cv2.CAP_PROP_FOCUS,
            "gain":       cv2.CAP_PROP_GAIN,
            "exposure":   cv2.CAP_PROP_EXPOSURE,
            "brightness": cv2.CAP_PROP_BRIGHTNESS,
            "contrast":   cv2.CAP_PROP_CONTRAST,
        }
        auto_map = {
            "focus":      (cv2.CAP_PROP_AUTOFOCUS, "autofocus"),
            "gain":       (getattr(cv2, "CAP_PROP_AUTOGAIN", 22), "auto_gain"),
            "exposure":   (cv2.CAP_PROP_AUTO_EXPOSURE, "auto_exposure"),
        }
        for key, (auto_id, auto_key) in auto_map.items():
            is_auto = bool(props.get(auto_key, False if key != "focus" else True))
            try:
                if key == "exposure":
                    cap.set(auto_id, 0.75 if is_auto else 0.25)
                else:
                    cap.set(auto_id, 1 if is_auto else 0)
            except Exception:
                pass

        for key, prop_id in prop_map.items():
            auto_key = f"auto_{key}" if key != "focus" else "autofocus"
            is_auto = bool(props.get(auto_key, False if key != "focus" else True))
            if not is_auto:
                val = props.get(key, -1)
                if int(val) != -1:
                    try:
                        cap.set(prop_id, int(val))
                        self.logger.debug(f"CAM PROP {key}={val}")
                    except Exception:
                        pass

    # ------------------------------------------------------------------
    # GUI
    # ------------------------------------------------------------------
    def setup_gui(self):
        self.root = tk.Tk()
        self.root.title(f"ClearView 静止画撮影システム {VERSION}")
        self.root.geometry("1400x900")
        self.root.configure(bg=COLOR_BG_MAIN)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        # ウィンドウ最大化
        self.root.update_idletasks()
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                w = self.root.winfo_screenwidth()
                h = self.root.winfo_screenheight()
                self.root.geometry(f"{w}x{h}+0+0")

        # --- ヘッダー ---
        self.header = tk.Frame(self.root, bg=COLOR_BG_PANEL, height=80)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)

        self.lbl_status = tk.Label(
            self.header, text="トリガー待機中", font=FONT_LARGE,
            bg=COLOR_BG_PANEL, fg=COLOR_ACCENT
        )
        self.lbl_status.pack(side=tk.LEFT, padx=30, pady=15)

        self.lbl_clock = tk.Label(self.header, text="", font=FONT_LARGE,
                                  bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN)
        self.lbl_clock.pack(side=tk.RIGHT, padx=30)
        self.update_clock()

        btn_help = tk.Button(self.header, text="？", font=FONT_BOLD,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", width=3,
                             command=self.show_main_help)
        btn_help.pack(side=tk.RIGHT, padx=10)
        Tooltip(btn_help, "操作方法を表示します")

        # --- メインコンテンツ ---
        main = tk.Frame(self.root, bg=COLOR_BG_MAIN)
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # カメラプレビューエリア
        v_frm_outer, v_frm_inner = create_card(main, "カメラプレビュー")
        v_frm_outer.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        v_frm_outer.pack_propagate(False)

        self.camera_preview_frame = tk.Frame(v_frm_inner, bg="black")
        self.camera_preview_frame.pack(fill=tk.BOTH, expand=True)
        self.cam_labels = {}
        cameras = self._camera_configs()
        for index, camera in enumerate(cameras):
            row, col = divmod(index, 2)
            self.camera_preview_frame.grid_rowconfigure(row, weight=1)
            self.camera_preview_frame.grid_columnconfigure(col, weight=1)
            panel = tk.Frame(self.camera_preview_frame, bg="black")
            panel.grid(row=row, column=col, sticky="nsew", padx=2, pady=2)
            tk.Label(panel, text=camera.get("name", camera["id"]), bg="#202020",
                     fg=COLOR_TEXT_MAIN, font=FONT_NORMAL).pack(fill=tk.X)
            label = tk.Label(panel, bg="black")
            label.pack(fill=tk.BOTH, expand=True)
            label.is_updating = False
            self.cam_labels[camera["id"]] = label

        # 操作パネル (右側固定幅)
        pnl_outer, pnl = create_card(main, "操作パネル")
        pnl_outer.pack(side=tk.RIGHT, fill=tk.Y, padx=(20, 0))
        pnl_outer.config(width=420)
        pnl_outer.pack_propagate(False)

        # --- コミット番号 ---
        tk.Label(pnl, text="コミット番号", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(10, 2))

        cf = tk.Frame(pnl, bg=COLOR_BG_PANEL)
        cf.pack(pady=5)
        tk.Button(cf, text="－", font=FONT_LARGE, bg=COLOR_BG_INPUT,
                  fg=COLOR_TEXT_MAIN, width=3, relief="flat",
                  command=lambda: self.adjust_commit(-1)).pack(side=tk.LEFT)

        self.v_commit = tk.StringVar(value="0001")
        self.lbl_commit = tk.Label(cf, textvariable=self.v_commit,
                                   bg=COLOR_BG_INPUT, fg=COLOR_ACCENT)
        commit_font, commit_width = get_commit_display_style(
            bool(self.settings.data["system"].get("commit_half_step", False)))
        self.lbl_commit.config(font=commit_font, width=commit_width)
        self.lbl_commit.pack(side=tk.LEFT, padx=10)
        self.lbl_commit.bind("<Button-1>", lambda e: self.manual_commit_set())
        Tooltip(self.lbl_commit, "クリックしてコミット番号を手動設定")

        tk.Button(cf, text="＋", font=FONT_LARGE, bg=COLOR_BG_INPUT,
                  fg=COLOR_TEXT_MAIN, width=3, relief="flat",
                  command=lambda: self.adjust_commit(1)).pack(side=tk.LEFT)

        tk.Button(pnl, text="番号入力", font=FONT_NORMAL, bg="#546E7A",
                  fg="white", relief="flat",
                  command=self.manual_commit_set).pack(fill=tk.X, padx=10, pady=(0, 10))

        # --- 撮影統計 ---
        tk.Label(pnl, text="本日の撮影枚数", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(10, 2))
        self.v_count = tk.StringVar(value="0 枚")
        self.shot_count = 0
        tk.Label(pnl, textvariable=self.v_count, font=FONT_LARGE,
                 bg=COLOR_BG_INPUT, fg=COLOR_ACCENT, pady=5).pack(fill=tk.X, padx=10)

        # --- 最終撮影情報 ---
        tk.Label(pnl, text="最終撮影", font=FONT_BOLD,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB).pack(pady=(10, 2))
        self.v_last_shot = tk.StringVar(value="---")
        tk.Label(pnl, textvariable=self.v_last_shot, font=FONT_NORMAL,
                 bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, pady=5,
                 wraplength=380, justify=tk.LEFT).pack(fill=tk.X, padx=10)

        # 結果フォルダボタン
        tk.Button(pnl, text="結果フォルダ", font=FONT_BOLD, bg="#546E7A",
                  fg="white", height=2, relief="flat",
                  command=self.open_results_folder).pack(fill=tk.X, padx=10, pady=(30, 5))

        # 詳細設定ボタン
        tk.Button(pnl, text="詳細設定", font=FONT_BOLD, bg="#455A64",
                  fg="white", height=2, relief="flat",
                  command=self.open_settings).pack(fill=tk.X, padx=10, pady=5)

        # アプリインスタンス参照保持
        self.root.app_instance = self

        # バックグラウンドスレッド起動
        threading.Thread(target=self._preview_loop, daemon=True).start()
        threading.Thread(target=self._main_logic_loop, daemon=True).start()

    def update_status(self, text, bg=None):
        """ステータスバーのテキストと背景色を更新 (スレッドセーフ)"""
        def _upd():
            self.lbl_status.config(text=text)
            if bg:
                self.lbl_status.config(bg=bg, fg="black" if bg != COLOR_BG_PANEL else COLOR_ACCENT)
                self.header.config(bg=bg)
            else:
                self.lbl_status.config(bg=COLOR_BG_PANEL, fg=COLOR_ACCENT)
                self.header.config(bg=COLOR_BG_PANEL)
        self.root.after(0, _upd)

    def update_clock(self):
        # datetime オブジェクト生成を避け time.strftime を直接使用
        self.lbl_clock.config(text=time.strftime("%Y/%m/%d %H:%M:%S"))
        self.root.after(1000, self.update_clock)

    def on_closing(self):
        """アプリケーション終了時のクリーンアップ処理"""
        if getattr(self, "_is_closing_dialog_open", False):
            return
        self._is_closing_dialog_open = True

        try:
            if not hasattr(self, "root") or not self.root.winfo_exists():
                confirmed = True
            else:
                confirmed = messagebox.askokcancel("終了", "アプリケーションを終了しますか？", parent=self.root)
        except Exception:
            confirmed = True

        if confirmed:
            self.running = False
            if hasattr(self, "save_queue"):
                self.save_queue.put(None)
            self.logger.info("シャットダウン処理を開始します...")

            # GPIO 解放
            try:
                for dev in [self.trig_device, self.out_ok, self.out_ng, self.out_running]:
                    if dev:
                        if dev == self.out_running and hasattr(dev, "off"):
                            try:
                                dev.off()
                            except Exception:
                                pass
                        try:
                            dev.close()
                        except Exception:
                            pass
            except Exception as e:
                self.logger.error(f"GPIO解放エラー: {e}")

            # カメラ解放
            try:
                self._release_cameras()
            except Exception as e:
                self.logger.error(f"カメラ解放エラー: {e}")

            # 仮想GPIOパネル解放
            try:
                if hasattr(self, "mock_root") and self.mock_root.winfo_exists():
                    self.mock_root.destroy()
            except Exception:
                pass

            # ウィンドウ破棄
            try:
                if hasattr(self, "root") and self.root.winfo_exists():
                    self.root.destroy()
            except Exception as e:
                self.logger.error(f"ウィンドウ破棄エラー: {e}")

            self.logger.info("シャットダウン完了")
        else:
            self._is_closing_dialog_open = False

    # ------------------------------------------------------------------
    # コミット番号管理
    # ------------------------------------------------------------------
    def get_commit_str(self):
        if self.settings.data["system"].get("commit_half_step", False):
            return f"{self.commit_number:06.1f}"
        return f"{int(self.commit_number):04d}"

    def adjust_commit(self, delta):
        step = 0.5 if self.settings.data["system"].get("commit_half_step", False) else 1.0
        self.commit_number += delta * step
        if self.commit_number > 9999.0:
            self.commit_number = 1.0
        elif self.commit_number < 1.0:
            self.commit_number = 9999.0
        self.root.after(0, lambda: self.v_commit.set(self.get_commit_str()))

    def update_commit_display(self):
        is_half_step = bool(self.settings.data["system"].get("commit_half_step", False))
        font, width = get_commit_display_style(is_half_step)
        self.lbl_commit.config(font=font, width=width)
        self.v_commit.set(self.get_commit_str())

    def manual_commit_set(self):
        d = TenKeyDialog(self.root, "コミット番号設定", self.commit_number,
                         bool(self.settings.data["system"].get("commit_half_step", False)))
        if d.result is not None:
            self.commit_number = float(d.result)
            self.v_commit.set(self.get_commit_str())

    def manual_commit_set_initial(self):
        try:
            d = TenKeyDialog(self.root, "開始コミット番号", self.commit_number,
                             bool(self.settings.data["system"].get("commit_half_step", False)))
            if d.result is not None:
                self.commit_number = float(d.result)
                self.v_commit.set(self.get_commit_str())
        except Exception as e:
            self.logger.error(f"初期コミット番号設定エラー: {e}")

    # ------------------------------------------------------------------
    # プレビューループ
    # ------------------------------------------------------------------
    def _preview_loop(self):
        """バックグラウンドスレッドで常時カメラプレビューを更新"""
        while self.running:
            fps = int(self.settings.data.get("system", {}).get("preview_fps", PREVIEW_FPS))
            _interval = 1.0 / max(1, fps)
            try:
                self._preview_cameras()
            except Exception as e:
                self.logger.error(f"プレビューエラー: {e}")
            time.sleep(_interval)

    def _preview_cameras(self):
        """全カメラのフレームを順に取得し、対応するプレビューへ表示する。"""
        if self.preview_paused or self.inspecting or time.time() < self.result_display_until:
            return True
        for camera in self._camera_configs():
            if self.preview_paused or self.inspecting:
                break
            camera_id = camera["id"]
            ret, frame = False, None
            # grab() と retrieve() の両方を camera_lock 内で実行し、
            # 撮影時の release()/reopen による Use-After-Free セグフォを完全防止
            with self.camera_lock:
                cap = self.caps.get(camera_id)
                if cap is None or not cap.isOpened():
                    continue
                try:
                    grabbed = cap.grab()
                    if grabbed:
                        ret, frame = cap.retrieve()
                except Exception as exc:
                    self.logger.warning(f"プレビュー取得失敗 ({camera_id}): {exc}")
                    continue

            if not ret or frame is None or frame.size == 0:
                continue
            self._show_frame_on_preview(frame, camera)
        return bool(self.caps)

    @staticmethod
    def _parse_resolution(resolution_str):
        """解像度文字列から (width, height) を取得。不正な場合は None。"""
        raw = str(resolution_str).split(" ")[0]
        if "x" not in raw:
            return None
        try:
            width, height = map(int, raw.split("x"))
            return width, height
        except ValueError:
            return None

    @staticmethod
    def _capture_settle_seconds(width, height):
        """解像度に応じたカメラバッファ安定化待機時間（秒）"""
        pixels = width * height
        if pixels >= 8000 * 6000:
            return 2.0
        if pixels >= 3840 * 2160:
            return 1.0
        if pixels >= 1920 * 1080:
            return 0.5
        return 0.3

    def _read_camera_frame(self, cap, width=0, height=0):
        """grab/retrieve をリトライ付きで実行（高解像度向け）。
        grab() と retrieve() を同一の camera_lock 内で実行し、
        inspection_app 準拠の安全な排他制御を実現する。"""
        if not cap or not cap.isOpened():
            return False, None
        pixels = width * height if width and height else 0
        if pixels >= 8000 * 6000:
            retries, retry_delay = 10, 0.2
        elif pixels >= 3840 * 2160:
            retries, retry_delay = 6, 0.15
        else:
            retries, retry_delay = 3, 0.1
        for _ in range(retries):
            with self.camera_lock:
                grabbed = cap.grab()
                if not grabbed:
                    pass
                else:
                    ret, frame = cap.retrieve()
                    if ret and frame is not None and frame.size > 0:
                        return True, frame.copy()
            time.sleep(retry_delay)
        return False, None

    def _capture_all_cameras(self):
        """1トリガーで有効な全カメラを同じバースト回数だけ撮影する。異解像度の場合は安全にクローズ＆再オープンする。"""
        commit_str = self.get_commit_str()
        cameras = self._camera_configs()
        capture_count = int(self.settings.data["system"].get("capture_count", 5))
        interval = float(self.settings.data["system"].get("burst_interval", 0.2))
        result_dir = Path(self.settings.data["paths"].get("results_dir", "./results"))
        result_dir.mkdir(parents=True, exist_ok=True)
        saved_files, errors = [], []
        self.root.after(0, lambda: self.update_status("撮影中...", COLOR_ACCENT))

        prev_paused = self.preview_paused
        self.preview_paused = True  # プレビューループを停止

        reopened_caps = {}
        try:
            # プレビュースレッドが完全に停止するのを確実に待機
            time.sleep(0.1)

            # 各カメラについて、現在開いている解像度と本撮影解像度が異なる場合のみ再オープン
            # (4K以下の場合は最初から本撮影解像度で開かれているため再オープンはゼロ回＝セグフォゼロ)
            for camera in cameras:
                cid = camera["id"]
                capt_res = camera.get("capture_resolution", "1920x1080").split(" ")[0]
                cur_res = getattr(self, "caps_opened_res", {}).get(cid, "").split(" ")[0]

                # 現在の解像度と本撮影解像度が異なり、かつ 8K など再オープンが必要な場合のみ切り替え
                if cur_res and capt_res and cur_res != capt_res and "x" in capt_res:
                    with self.camera_lock:
                        old_cap = self.caps.get(cid)
                        if old_cap and old_cap.isOpened():
                            old_cap.release()
                        del old_cap
                    # ロック外で OS・V4L2カーネルバッファの完全解放を待機（セグフォ・timeout防止）
                    time.sleep(0.35)
                    import gc
                    gc.collect()

                    self.logger.info(f"【カメラ再オープン】'{cid}' 本撮影用解像度に切り替え: {cur_res} -> {capt_res}")
                    new_cap = self._open_camera_device(camera, capt_res)
                    if new_cap:
                        with self.camera_lock:
                            self.caps[cid] = new_cap
                        self.caps_opened_res[cid] = capt_res
                        reopened_caps[cid] = cur_res

            # 初回ショット前に古いバッファをクリア (inspection_app 準拠)
            with self.camera_lock:
                for camera in cameras:
                    cap = self.caps.get(camera["id"])
                    if cap and cap.isOpened():
                        for _ in range(2):
                            cap.grab()
            time.sleep(0.1)

            # 指定枚数の連続撮影
            for shot_idx in range(capture_count):
                for camera in cameras:
                    camera_id = camera["id"]
                    cap = self.caps.get(camera_id)
                    parsed = self._parse_resolution(camera.get("capture_resolution", "1920x1080"))
                    cap_w, cap_h = parsed if parsed else (0, 0)
                    ret, frame = self._read_camera_frame(cap, cap_w, cap_h)
                    if not ret or frame is None or frame.size == 0:
                        errors.append(camera.get("name", camera_id))
                        continue
                    camera_name = self._safe_camera_name(camera.get("name", camera_id), camera_id)
                    filename = f"{commit_str}_{camera_name}_{shot_idx + 1:02d}.jpg"
                    self._save_image_async(frame, result_dir / filename)
                    saved_files.append(filename)
                    self._show_frame_on_preview(frame, camera)
                    self.logger.info(f"【撮影成功】{camera_id} ({shot_idx + 1}/{capture_count}枚目): {filename}")
                if shot_idx < capture_count - 1 and interval > 0:
                    time.sleep(interval)
        except Exception as exc:
            self.logger.error(f"マルチカメラ撮影エラー: {exc}")
            errors.append(str(exc))
        finally:
            # 8K撮影等で再オープンしていたカメラのみ元のプレビュー解像度に安全復帰
            if reopened_caps:
                for camera in cameras:
                    cid = camera["id"]
                    if cid in reopened_caps:
                        prev_res = reopened_caps[cid]
                        with self.camera_lock:
                            capt_cap = self.caps.get(cid)
                            if capt_cap and capt_cap.isOpened():
                                capt_cap.release()
                            del capt_cap
                        # ロック外で OS・V4L2カーネルバッファの完全解放を待機
                        time.sleep(0.35)
                        import gc
                        gc.collect()

                        self.logger.info(f"【カメラ復帰】'{cid}' プレビュー用解像度に再オープン復帰: {prev_res}")
                        prev_cap = self._open_camera_device(camera, prev_res)
                        if prev_cap:
                            with self.camera_lock:
                                self.caps[cid] = prev_cap
                            self.caps_opened_res[cid] = prev_res
            self.preview_paused = prev_paused  # プレビュー再開

        expected = len(cameras) * capture_count
        success = not errors and len(saved_files) == expected
        if success:
            self.shot_count += len(saved_files)
            self.root.after(0, lambda: self.update_status(f"撮影成功 - {commit_str} ({len(saved_files)}枚)", COLOR_OK))
            self._pulse_gpio(self.out_ok, self.settings.data["gpio"].get("output_ok_duration", 0.5))
            now = datetime.datetime.now().strftime("%H:%M:%S")
            self.root.after(0, lambda: self.v_count.set(f"{self.shot_count} 枚"))
            self.root.after(0, lambda: self.v_last_shot.set(f"{now}  #{commit_str}  {len(saved_files)}枚\n{saved_files[0]}"))
        else:
            self.logger.error(f"Capture error - commit {commit_str}: {errors}")
            saved_count = len(saved_files)
            self.root.after(
                0,
                lambda: self.update_status(
                    f"撮影エラー - {commit_str} ({saved_count}/{expected}枚)" if saved_count else "撮影エラー",
                    COLOR_NG,
                ),
            )
            self._pulse_gpio(self.out_ng, self.settings.data["gpio"].get("output_ng_duration", 0.5))
            if saved_files:
                now = datetime.datetime.now().strftime("%H:%M:%S")
                self.root.after(0, lambda: self.v_last_shot.set(f"{now}  #{commit_str}  {saved_count}枚 (一部失敗)\n{saved_files[0]}"))

        # 1サイクル完了ごとにコミット番号を進める（撮影失敗時も進行）
        step = 0.5 if self.settings.data["system"].get("commit_half_step", False) else 1.0
        self.commit_number = 1.0 if self.commit_number >= 9999.0 else self.commit_number + step
        self.root.after(0, lambda: self.v_commit.set(self.get_commit_str()))
        self.result_display_until = time.time() + float(self.settings.data["system"].get("result_display_time", 2.0))
        self.inspecting = False

    # ------------------------------------------------------------------
    # メインロジックループ (撮影サイクル)
    # ------------------------------------------------------------------
    def _main_logic_loop(self):
        """トリガーを待機し、撮影・保存を行うメインループ"""
        self.root.after(0, lambda: self.update_status("トリガー待機中", None))
        while self.running:
            try:
                # トリガー待機 (タイムアウト 0.5秒でポーリング)
                try:
                    self.trigger_queue.get(timeout=0.5)
                except queue.Empty:
                    continue

                if not self.running:
                    break

                # 撮影シーケンス開始
                self.inspecting = True
                self._capture_all_cameras()
                time.sleep(0.3)
                self.root.after(0, lambda: self.update_status("トリガー待機中", None))

            except Exception as e:
                import traceback
                self.logger.error(f"メインロジックエラー: {e}\n{traceback.format_exc()}")
                self.inspecting = False
                time.sleep(0.5)

    def _save_worker_loop(self):
        """非同期画像保存を直列に安全処理する専用ワーカースレッド（メモリ枯渇・セグフォ防止）"""
        while self.running:
            try:
                task = self.save_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            frame, filepath = task
            try:
                ext = filepath.suffix.lower()  # 例: ".jpg"
                ret, buf = cv2.imencode(ext, frame)
                if ret:
                    with open(filepath, "wb") as fh:
                        fh.write(buf.tobytes())
                    self.logger.info(f"保存成功: {filepath.name}")
                else:
                    self.logger.error(f"保存失敗 (imencode): {filepath.name}")
            except Exception as e:
                self.logger.error(f"保存エラー ({filepath.name}): {e}")
            finally:
                del frame
                self.save_queue.task_done()

    def _save_image_async(self, frame, filepath):
        """画像保存キューにタスクを投入（単一ワーカースレッドで順次エンコード・書き込み）"""
        self.save_queue.put((frame, filepath))

    def _show_frame_on_preview(self, frame, camera):
        """撮影フレームをプレビューに表示 (8K等の高解像度はUIスレッド渡し前に縮小)"""
        preview_res = camera.get("preview_resolution", "640x480").split(" ")[0]
        if preview_res in ("プレビューなし", ""):
            return

        label = self.cam_labels.get(camera["id"])
        if label is None:
            return
        # 前フレームの描画中はスキップして Tkinter キューの詰まりを防止
        if getattr(label, "is_updating", False):
            return
        label.is_updating = True

        try:
            pw, ph = map(int, preview_res.split("x"))
            h, w = frame.shape[:2]
            scale = min(pw / w, ph / h)
            nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
            # 8Kプレビュー向けに高速かつ安全なリサイズ
            thumb = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
            rgb = cv2.cvtColor(thumb, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            del thumb
        except Exception:
            label.is_updating = False
            return

        def _upd(lbl=label, p_img=pil_img, nw=nw, nh=nh):
            try:
                cur_img = getattr(lbl, "img", None)
                if cur_img is None or cur_img.width() != nw or cur_img.height() != nh:
                    lbl.img = ImageTk.PhotoImage(p_img)
                    lbl.config(image=lbl.img)
                else:
                    lbl.img.paste(p_img)
            except Exception:
                pass
            finally:
                lbl.is_updating = False
        self.root.after(1, _upd)

    def _pulse_gpio(self, device, duration):
        """GPIO出力を指定時間パルスする (別スレッド)"""
        if device is None:
            return
        def _do(dev=device):
            try:
                if dev and hasattr(dev, "on"):
                    dev.on()
                    time.sleep(max(0.05, float(duration)))
                    if hasattr(dev, "off"):
                        dev.off()
            except Exception as e:
                self.logger.error(f"GPIO出力エラー: {e}")
        threading.Thread(target=_do, daemon=True).start()

    def pulse_test_output(self, pin, duration):
        """設定画面からのパルス出力テスト"""
        dev = OutputDevice(pin)
        self._pulse_gpio(dev, duration)

    def toggle_output_pin_by_num(self, pin: int, turn_on: bool) -> bool:
        """設定画面からのテスト点灯（ON/OFFトグル切替）"""
        try:
            # 既存の出力デバイスで一致するものがあれば使用、無ければ一時生成
            target_dev = None
            for dev in (getattr(self, "out_ok", None), getattr(self, "out_ng", None), getattr(self, "out_running", None)):
                if dev and hasattr(dev, "pin") and str(dev.pin) == str(pin):
                    target_dev = dev
                    break
            if target_dev is None:
                target_dev = OutputDevice(pin)
            
            if turn_on:
                target_dev.on()
            else:
                target_dev.off()
            self.logger.info(f"出力ピン BCM {pin} テスト点灯: {'ON' if turn_on else 'OFF'}")
            return True
        except Exception as e:
            self.logger.error(f"出力ピンテストエラー (BCM {pin}): {e}")
            return False

    # ------------------------------------------------------------------
    # 容量監視
    # ------------------------------------------------------------------
    def _monitor_storage(self):
        _INTERVAL_MS = 10 * 60 * 1000  # 10分

        def _thread():
            try:
                system_cfg = self.settings.data.get("system", {})
                if not system_cfg.get("auto_delete_enabled", True):
                    return

                max_gb = float(system_cfg.get("max_results_gb", 10.0))
                if max_gb <= 0:
                    return

                paths = self.settings.data.get("paths", {})
                results_dir = Path(paths.get("results_dir", "./results"))
                if not results_dir.exists():
                    return

                # results_dir 内の全画像 (*.jpg, *.png) を更新日時が古い順にソート
                img_files = sorted(
                    [f for f in results_dir.glob("*.jpg")],
                    key=lambda f: f.stat().st_mtime
                )
                if not img_files:
                    return

                total_size = sum(f.stat().st_size for f in img_files)
                max_bytes = max_gb * (1024 ** 3)

                # ディスクの空き容量も確認
                usage = shutil.disk_usage(results_dir)
                free_gb = usage.free / (1024 ** 3)

                needs_deletion = False
                target_bytes = total_size

                if total_size > max_bytes:
                    needs_deletion = True
                    target_bytes = max_bytes * 0.9  # 90%まで減らす
                elif free_gb < 1.0:
                    needs_deletion = True
                    target_bytes = max(0, total_size - int(1.0 * 1024**3))

                if needs_deletion:
                    self.logger.info(f"[容量監視] 削除開始。現在の画像サイズ: {total_size/(1024**3):.2f} GB / 空き容量: {free_gb:.2f} GB")
                    deleted_count = 0
                    for f in img_files:
                        if total_size <= target_bytes:
                            break
                        try:
                            file_size = f.stat().st_size
                            f.unlink()
                            total_size -= file_size
                            deleted_count += 1
                        except Exception as e:
                            self.logger.warning(f"[容量監視] 削除失敗: {f.name} - {e}")
                    if deleted_count > 0:
                        self.logger.info(f"[容量監視] {deleted_count} 件の古い画像を自動削除しました。")

            except Exception as e:
                self.logger.error(f"容量監視エラー: {e}")
            finally:
                if self.running:
                    self.root.after(_INTERVAL_MS, self._monitor_storage)

        threading.Thread(target=_thread, daemon=True).start()

    # ------------------------------------------------------------------
    # UI操作
    # ------------------------------------------------------------------
    def clear_history(self):
        if messagebox.askyesno("確認", "撮影統計情報をリセットしますか？"):
            self.shot_count = 0
            self.v_count.set("0 枚")
            self.v_last_shot.set("---")

    def open_results_folder(self):
        folder = Path(self.settings.data["paths"].get("results_dir", "./results"))
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))
            elif sys.platform.startswith("linux"):
                import subprocess
                subprocess.Popen(["xdg-open", str(folder)])
            else:
                import subprocess
                subprocess.Popen(["open", str(folder)])
        except Exception as e:
            self.logger.error(f"結果フォルダを開けませんでした: {e}")
            messagebox.showerror("エラー", f"フォルダを開けませんでした:\n{folder}", parent=self.root)

    def open_settings(self):
        if getattr(self, "inspecting", False):
            self.logger.warning("撮影処理中のため設定画面を開けません。完了後に再度お試しください。")
            messagebox.showwarning("撮影中", "撮影処理中のため設定画面を開けません。\n撮影完了後に再度開いてください。", parent=self.root)
            return

        self.settings_open = True
        self.preview_paused = True

        # 安全のため、設定画面表示前にアプリ本体のカメラおよびGPIOを一旦解放する
        with self.camera_lock:
            self._release_cameras()

        try:
            for dev in [self.trig_device, self.out_ok, self.out_ng, self.out_running]:
                if dev:
                    try:
                        dev.close()
                    except Exception:
                        pass
            self.trig_device = None
            self.out_ok = None
            self.out_ng = None
            self.out_running = None
        except Exception as e:
            self.logger.error(f"設定画面表示前のGPIO解放エラー: {e}")

        self.logger.info("設定画面を開きました。")
        self.root.update_idletasks()
        SettingsDialog(self.root, self.settings, self.on_settings_closed)

    def on_settings_closed(self):
        self.settings_open = False
        self.logger.info("設定画面が閉じられました。ハードウェアを再初期化します...")
        
        # 同期的にハードウェアを再初期化
        self.setup_hardware()
        
        # 再初期化完了後にプレビューを再開
        self.preview_paused = False
        self.update_commit_display()
        self.v_commit.set(self.get_commit_str())

    def show_main_help(self):
        help_data = {
            "1. 概要": "ClearView はトリガー信号を受けると静止画を指定枚数撮影・保存する専用システムです。",
            "2. コミット番号": "撮影された画像のファイル名管理番号です。\n"
                             "「番号入力」ボタンまたはコミット番号をクリックしてテンキーで入力してください。\n"
                             "「－」「＋」ボタンで±1 の調整もできます。\n"
                             "撮影が正常に完了すると自動的に+1されます。",
            "3. 撮影フロー": "1. トリガーピンに信号が入ると撮影を開始します。\n"
                           "2. 設定画面の「撮影回数」だけ、「撮影間隔」をあけて連続撮影します。\n"
                           "3. 「本撮影の静止画解像度」で設定した解像度で保存されます。\n"
                           "4. 正常完了時はOK出力ピンにパルス信号を出力します。\n"
                           "5. コミット番号が自動でインクリメントされます。",
            "4. 設定画面": "画面右下の「詳細設定」ボタンから各種設定を変更できます。\n"
                         "・カメラインデックス / 本撮影の解像度 / プレビュー解像度\n"
                         "・トリガーとOK/NG出力のGPIOピン番号\n"
                         "・撮影回数・間隔・保存フォルダパス"
        }
        HelpWindow(self.root, "操作ヘルプ", help_data)

    def setup_mock_ui(self):
        """仮想GPIOパネルの構築 (Windows等でのデバッグ用)"""
        try:
            self.mock_root = tk.Toplevel(self.root)
            self.mock_root.title("仮想GPIOパネル")
            self.mock_root.geometry("400x450")
            self.mock_root.configure(bg=COLOR_BG_MAIN)
            self.mock_root.attributes("-topmost", True)
            self.mock_root.resizable(False, False)

            container = tk.Frame(self.mock_root, bg=COLOR_BG_MAIN, padx=20, pady=20)
            container.pack(fill=tk.BOTH, expand=True)

            tk.Label(container, text="仮想GPIOコントロール", font=FONT_BOLD,
                     bg=COLOR_BG_MAIN, fg=COLOR_ACCENT).pack(pady=(0, 20))

            # --- 入力 (トリガー) ---
            f_in = tk.LabelFrame(container, text="入力テスト (センサー模擬)", font=FONT_NORMAL,
                                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, padx=10, pady=10)
            f_in.pack(fill=tk.X, pady=10)

            gpio = self.settings.data.get("gpio", {})
            t_pin = gpio.get("trigger_pin", 22)
            btn_trig = tk.Button(f_in, text=f"トリガー信号送信 (Pin {t_pin})", font=FONT_BOLD,
                                 bg=COLOR_ACCENT, fg="black", relief="flat", height=2,
                                 command=lambda: self._pulse_mock_input(t_pin))
            btn_trig.pack(fill=tk.X, pady=5)
            Tooltip(btn_trig, "ボタンを押すと、トリガーピンにパルス入力を送信します。")

            # --- 出力監視 (インジケータ) ---
            f_out = tk.LabelFrame(container, text="出力信号監視 (パトライト模擬)", font=FONT_NORMAL,
                                  bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, padx=10, pady=10)
            f_out.pack(fill=tk.BOTH, expand=True, pady=10)

            self.mock_indicators = {}
            outputs = [
                ("運転中 (RUN)", gpio.get("system_running_pin", 5), COLOR_WARNING),
                ("撮影完了 (OK)", gpio.get("output_ok_pin", 16), COLOR_OK),
                ("撮影異常 (NG)", gpio.get("output_ng_pin", 20), COLOR_NG)
            ]

            for label, pin, color in outputs:
                row = tk.Frame(f_out, bg=COLOR_BG_PANEL, pady=5)
                row.pack(fill=tk.X)

                # インジケータ用の丸を描画するキャンバス
                canvas = tk.Canvas(row, width=30, height=30, bg=COLOR_BG_PANEL, highlightthickness=0)
                canvas.pack(side=tk.LEFT, padx=10)
                ind = canvas.create_oval(5, 5, 25, 25, fill="#424242", outline="#616161")

                tk.Label(row, text=f"{label} (Pin {pin})", font=FONT_NORMAL,
                         bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN).pack(side=tk.LEFT)

                self.mock_indicators[str(pin)] = (canvas, ind, color)

            # 定期更新ループの開始
            self._update_mock_ui()

        except Exception as e:
            self.logger.error(f"仮想GPIOパネル起動エラー: {e}")

    def _pulse_mock_input(self, pin):
        """仮想入力をONにし、100ms後にOFFにする"""
        def _do():
            MockManager.set_input(pin, True)
            time.sleep(0.1)
            MockManager.set_input(pin, False)
        threading.Thread(target=_do, daemon=True).start()

    def _update_mock_ui(self):
        """定期的に仮想出力の状態を監視してインジケータを更新"""
        if not hasattr(self, "mock_indicators") or not hasattr(self, "mock_root"):
            return
        if not self.mock_root.winfo_exists():
            return

        try:
            for pin, (canvas, ind, active_color) in self.mock_indicators.items():
                is_active = MockManager.get_output_state(pin)
                fill_color = active_color if is_active else "#424242"
                canvas.itemconfig(ind, fill=fill_color)
            
            # 200msごとにセルフ更新
            self.mock_root.after(200, self._update_mock_ui)
        except Exception as e:
            self.logger.error(f"仮想GPIOパネル更新エラー: {e}")
