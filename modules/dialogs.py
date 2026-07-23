#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dialogs.py - 詳細設定ダイアログ SettingsDialog
"""

import copy
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import cv2
import numpy as np
from PIL import Image, ImageTk

from .constants import (
    COLOR_BG_MAIN, COLOR_BG_PANEL, COLOR_BG_INPUT,
    COLOR_TEXT_MAIN, COLOR_TEXT_SUB, COLOR_ACCENT, COLOR_OK, COLOR_NG, COLOR_WARNING, COLOR_BORDER,
    FONT_FAMILY, FONT_NORMAL, FONT_BOLD, FONT_SET_TAB, FONT_SET_LBL, FONT_SET_VAL,
    RES_OPTIONS, RES_OPTIONS_PREVIEW, VALID_BCM_PINS
)
from .widgets import create_card, Tooltip, HelpWindow, configure_modal_toplevel, release_modal_toplevel


RES_MAP = {
    "320x240": "320x240 (QVGA)",
    "640x480": "640x480 (VGA)",
    "1280x720": "1280x720 (HD)",
    "1920x1080": "1920x1080 (Full HD)",
    "3840x2160": "3840x2160 (4K)",
    "3840x2880": "3840x2880 (カスタム)",
    "8000x6000": "8000x6000 (8K)"
}

def _to_friendly(s):
    return RES_MAP.get(s, s)

def _to_raw(s):
    return s.split(" ")[0] if "x" in s else s


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, settings, on_close_callback=None):
        super().__init__(parent)
        self.settings = settings
        self.on_close_callback = on_close_callback
        self.title("詳細設定")
        self.geometry("1400x900")
        self.configure(bg=COLOR_BG_MAIN)
        self.transient(parent)
        self.grab_set()

        # 設定データをクローンして作業用にする
        self.temp_data = copy.deepcopy(self.settings.data)
        self.has_changes = False
        self.active_entry = (None, None)  # 現在フォーカスのあるEntry (widget, var)

        # UIスタイル設定
        style = ttk.Style(self)
        style.theme_use("default")
        style.configure("TNotebook", background=COLOR_BG_MAIN, borderwidth=0)
        style.configure("TNotebook.Tab", background=COLOR_BG_PANEL,
                        foreground=COLOR_TEXT_MAIN, font=FONT_SET_TAB,
                        padding=[20, 10], focuscolor=COLOR_BG_MAIN)
        style.map("TNotebook.Tab",
                  background=[("selected", COLOR_ACCENT)],
                  foreground=[("selected", "black")])

        # 下部ボタンエリア
        btn_f = tk.Frame(self, pady=20, bg=COLOR_BG_MAIN)
        btn_f.pack(side=tk.BOTTOM, fill=tk.X, padx=20)

        self.btn_save = tk.Button(btn_f, text="保存して閉じる", font=FONT_BOLD, bg=COLOR_BG_INPUT,
                                  fg="white", relief="flat", width=22,
                                  command=self.save_and_close)
        self.btn_save.pack(side=tk.RIGHT, padx=5)

        tk.Button(btn_f, text="キャンセル", font=FONT_BOLD, bg="#546E7A",
                  fg="white", relief="flat", width=10,
                  command=self.on_cancel).pack(side=tk.RIGHT, padx=5)

        btn_help = tk.Button(btn_f, text="ヘルプ", font=FONT_SET_LBL,
                             bg=COLOR_BG_INPUT, fg=COLOR_ACCENT,
                             relief="flat", command=self.show_settings_help)
        btn_help.pack(side=tk.LEFT, padx=20)

        # タブコンテナ
        nb = ttk.Notebook(self)
        nb.pack(side=tk.TOP, fill=tk.BOTH, expand=True, padx=20, pady=20)

        self.t_cam = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_cam, text=" カメラ ")
        self.t_res = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_res, text=" 画素数 ")
        self.t_gpio = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_gpio, text=" GPIOピン ")
        self.t_sys = tk.Frame(nb, bg=COLOR_BG_MAIN)
        nb.add(self.t_sys, text=" システム ")

        # トレース用変数初期化と設定構築
        self.init_variables()
        self.setup_cam()
        self.setup_res()
        self.setup_gpio()
        self.setup_sys()

        self.protocol("WM_DELETE_WINDOW", self.on_cancel)
        self.option_add("*TCombobox*Listbox.font", FONT_SET_VAL)

        configure_modal_toplevel(self, parent)

    def init_variables(self):
        # カメラ関連
        self.active_camera_index = 0
        self.v_active_camera = tk.StringVar()
        active_camera = self.temp_data["cameras"][self.active_camera_index]
        self.v_camera_name = tk.StringVar(value=active_camera.get("name", "カメラ 1"))
        self.v_capture_device = tk.StringVar(value=str(active_camera.get("capture_device", 0)))
        # 画素数関連 (初期値は raw 値を friendly 形式で表示)
        self.v_capture_resolution = tk.StringVar(value=_to_friendly(active_camera.get("capture_resolution", "1920x1080")))
        self.v_preview_resolution = tk.StringVar(value=_to_friendly(active_camera.get("preview_resolution", "640x480")))
        # カメラプロパティ関連
        cp = active_camera.get("camera_props", {})
        self.v_af              = tk.BooleanVar(value=bool(cp.get("autofocus", True)))
        self.v_auto_gain       = tk.BooleanVar(value=bool(cp.get("auto_gain", False)))
        self.v_auto_exposure   = tk.BooleanVar(value=bool(cp.get("auto_exposure", False)))
        self.v_auto_brightness = tk.BooleanVar(value=bool(cp.get("auto_brightness", False)))
        self.v_auto_contrast   = tk.BooleanVar(value=bool(cp.get("auto_contrast", False)))

        self.v_focus     = tk.StringVar(value=str(cp.get("focus",      -1)))
        self.v_gain      = tk.StringVar(value=str(cp.get("gain",       -1)))
        self.v_exposure  = tk.StringVar(value=str(cp.get("exposure",   -1)))
        self.v_brightness= tk.StringVar(value=str(cp.get("brightness", -1)))
        self.v_contrast  = tk.StringVar(value=str(cp.get("contrast",   -1)))

        self._prop_vars = {
            "focus":      self.v_focus,
            "gain":       self.v_gain,
            "exposure":   self.v_exposure,
            "brightness": self.v_brightness,
            "contrast":   self.v_contrast,
        }
        self._auto_vars = {
            "focus":      self.v_af,
            "gain":       self.v_auto_gain,
            "exposure":   self.v_auto_exposure,
            "brightness": self.v_auto_brightness,
            "contrast":   self.v_auto_contrast,
        }
        # タブ内プレビュー・共有ROI
        self._cam_preview_cap     = None
        self._cam_preview_running = False
        self._cam_preview_thread  = None
        self._roi_last_frame      = None
        self._preview_canvas      = None
        self._roi_rect_id         = None
        self._roi_state = {"x0": 0, "y0": 0, "x1": 0, "y1": 0, "set": False, "scale": 1.0}
        self._roi_status_var  = tk.StringVar(value="エリア未指定")
        self._tune_status_var = tk.StringVar(value="プレビューを開始して調整エリアを指定してください")
        # GPIO関連
        self.v_trigger_pin = tk.StringVar(value=str(self.temp_data["gpio"].get("trigger_pin", 22)))
        self.v_system_running_pin = tk.StringVar(value=str(self.temp_data["gpio"].get("system_running_pin", 5)))
        self.v_output_ok_pin = tk.StringVar(value=str(self.temp_data["gpio"].get("output_ok_pin", 16)))
        self.v_output_ng_pin = tk.StringVar(value=str(self.temp_data["gpio"].get("output_ng_pin", 20)))
        self.v_output_ok_duration = tk.StringVar(value=str(self.temp_data["gpio"].get("output_ok_duration", 0.5)))
        self.v_output_ng_duration = tk.StringVar(value=str(self.temp_data["gpio"].get("output_ng_duration", 0.5)))
        # システム・撮影関連
        self.v_capture_count = tk.StringVar(value=str(self.temp_data["system"].get("capture_count", 5)))
        self.v_burst_interval = tk.StringVar(value=str(self.temp_data["system"].get("burst_interval", 0.2)))
        self.v_result_display_time = tk.StringVar(value=str(self.temp_data["system"].get("result_display_time", 2.0)))
        self.v_log_dir = tk.StringVar(value=self.temp_data["paths"].get("log_dir", "./logs"))
        self.v_results_dir = tk.StringVar(value=self.temp_data["paths"].get("results_dir", "./results"))
        self.v_auto_delete_enabled = tk.BooleanVar(value=bool(self.temp_data["system"].get("auto_delete_enabled", True)))
        self.v_max_results_gb = tk.StringVar(value=str(self.temp_data["system"].get("max_results_gb", 10.0)))
        self.v_commit_half_step = tk.BooleanVar(value=bool(self.temp_data["system"].get("commit_half_step", False)))
        self.v_preview_fps = tk.StringVar(value=str(self.temp_data["system"].get("preview_fps", 15)))

    def _load_camera_to_ui(self, idx):
        """指定したインデックスのカメラ設定をUI変数にロードする"""
        if idx < 0 or idx >= len(self.temp_data["cameras"]):
            return
        cam = self.temp_data["cameras"][idx]
        self.v_camera_name.set(cam.get("name", f"カメラ {idx + 1}"))
        self.v_capture_device.set(str(cam.get("capture_device", 0)))
        self.v_capture_resolution.set(_to_friendly(cam.get("capture_resolution", "1920x1080")))
        self.v_preview_resolution.set(_to_friendly(cam.get("preview_resolution", "640x480")))
        
        cp = cam.get("camera_props", {})
        self.v_af.set(bool(cp.get("autofocus", True)))
        self.v_auto_gain.set(bool(cp.get("auto_gain", False)))
        self.v_auto_exposure.set(bool(cp.get("auto_exposure", False)))
        self.v_auto_brightness.set(bool(cp.get("auto_brightness", False)))
        self.v_auto_contrast.set(bool(cp.get("auto_contrast", False)))

        self.v_focus.set(str(cp.get("focus", -1)))
        self.v_gain.set(str(cp.get("gain", -1)))
        self.v_exposure.set(str(cp.get("exposure", -1)))
        self.v_brightness.set(str(cp.get("brightness", -1)))
        self.v_contrast.set(str(cp.get("contrast", -1)))
        self._update_all_prop_states()

    def _save_camera_from_ui(self, idx):
        """現在のUI変数の設定を指定したインデックスのカメラ設定にセーブする"""
        if idx < 0 or idx >= len(self.temp_data["cameras"]):
            return
        cam = self.temp_data["cameras"][idx]
        cam["name"] = self.v_camera_name.get()
        try:
            cam["capture_device"] = int(self.v_capture_device.get())
        except ValueError:
            cam["capture_device"] = 0
        cam["capture_resolution"] = _to_raw(self.v_capture_resolution.get().strip())
        cam["preview_resolution"] = _to_raw(self.v_preview_resolution.get().strip())
        
        cam["camera_props"] = {
            "autofocus": self.v_af.get(),
            "auto_gain": self.v_auto_gain.get(),
            "auto_exposure": self.v_auto_exposure.get(),
            "auto_brightness": self.v_auto_brightness.get(),
            "auto_contrast": self.v_auto_contrast.get(),
            "focus": int(self.v_focus.get()) if self.v_focus.get() else -1,
            "gain": int(self.v_gain.get()) if self.v_gain.get() else -1,
            "exposure": int(self.v_exposure.get()) if self.v_exposure.get() else -1,
            "brightness": int(self.v_brightness.get()) if self.v_brightness.get() else -1,
            "contrast": int(self.v_contrast.get()) if self.v_contrast.get() else -1
        }

    def _mark_changed(self, *args):
        if not self.has_changes:
            self.has_changes = True
            if hasattr(self, "btn_save") and self.btn_save.winfo_exists():
                self.btn_save.config(bg=COLOR_OK, fg="black", text="変更を適用して保存")

    def _entry(self, parent, var, width=None, key_path=None):
        ent = tk.Entry(parent, textvariable=var, font=FONT_SET_VAL,
                        width=width, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN,
                        insertbackground="white", relief="flat")
        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return ent

    def _spinbox(self, parent, var, from_, to, increment=1, width=6, key_path=None):
        sb = tk.Spinbox(parent, from_=from_, to=to, increment=increment, textvariable=var,
                        font=FONT_SET_VAL, width=width, bg=COLOR_BG_INPUT, fg="white",
                        buttonbackground="#78909C", bd=1, relief="solid")
        if key_path:
            def _trace(*args):
                self._mark_changed()
            var.trace_add("write", _trace)
        return sb

    def create_scrollable_panel(self, parent):
        canvas = tk.Canvas(parent, bg=COLOR_BG_MAIN, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        scrollable_frame = tk.Frame(canvas, bg=COLOR_BG_MAIN)

        canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")

        def _on_frame_configure(_event):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def _on_canvas_configure(event):
            canvas.itemconfig(canvas_window, width=event.width)

        scrollable_frame.bind("<Configure>", _on_frame_configure)
        canvas.bind("<Configure>", _on_canvas_configure)
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if not self.winfo_exists():
                return
            canvas.yview_scroll(-1 * (event.delta // 120), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel, add="+")

        return canvas, scrollable_frame

    def _camera_display_names(self):
        return [f"{i + 1}: {camera.get('name', camera['id'])}" for i, camera in enumerate(self.temp_data["cameras"])]

    def _store_active_camera(self):
        camera = self.temp_data["cameras"][self.active_camera_index]
        camera["name"] = self.v_camera_name.get().strip() or f"カメラ {self.active_camera_index + 1}"
        camera["capture_device"] = int(self.v_capture_device.get())
        camera["capture_resolution"] = _to_raw(self.v_capture_resolution.get().strip())
        camera["preview_resolution"] = _to_raw(self.v_preview_resolution.get().strip())
        camera["camera_props"] = {
            "autofocus": self.v_af.get(),
            "auto_gain": self.v_auto_gain.get(),
            "auto_exposure": self.v_auto_exposure.get(),
            "auto_brightness": self.v_auto_brightness.get(),
            "auto_contrast": self.v_auto_contrast.get(),
            "focus": int(self.v_focus.get()), "gain": int(self.v_gain.get()),
            "exposure": int(self.v_exposure.get()), "brightness": int(self.v_brightness.get()),
            "contrast": int(self.v_contrast.get()),
        }

    def _load_active_camera(self):
        camera = self.temp_data["cameras"][self.active_camera_index]
        self.v_camera_name.set(camera.get("name", f"カメラ {self.active_camera_index + 1}"))
        self.v_capture_device.set(str(camera.get("capture_device", 0)))
        self.v_capture_resolution.set(_to_friendly(camera.get("capture_resolution", "1920x1080")))
        self.v_preview_resolution.set(_to_friendly(camera.get("preview_resolution", "640x480")))
        props = camera.get("camera_props", {})
        self.v_af.set(bool(props.get("autofocus", True)))
        self.v_auto_gain.set(bool(props.get("auto_gain", False)))
        self.v_auto_exposure.set(bool(props.get("auto_exposure", False)))
        self.v_auto_brightness.set(bool(props.get("auto_brightness", False)))
        self.v_auto_contrast.set(bool(props.get("auto_contrast", False)))
        for key, var in (("focus", self.v_focus), ("gain", self.v_gain),
                         ("exposure", self.v_exposure), ("brightness", self.v_brightness),
                         ("contrast", self.v_contrast)):
            var.set(str(props.get(key, -1)))
        self._update_all_prop_states()

    def _select_camera(self, _event=None):
        self._store_active_camera()
        self.active_camera_index = self.camera_selector.current()
        self._load_active_camera()
        self._mark_changed()

    def _add_camera(self):
        if len(self.temp_data["cameras"]) >= 4:
            messagebox.showinfo("カメラ", "登録できるカメラは最大4台です。", parent=self)
            return
        self._store_active_camera()
        number = len(self.temp_data["cameras"]) + 1
        self.temp_data["cameras"].append({
            "id": f"cam_{number}", "name": f"カメラ {number}", "capture_device": number - 1,
            "capture_resolution": "1920x1080", "preview_resolution": "640x480",
            "camera_props": {"autofocus": True, "focus": -1, "gain": -1, "exposure": -1, "brightness": -1, "contrast": -1},
        })
        self.camera_selector["values"] = self._camera_display_names()
        self.camera_selector.current(len(self.temp_data["cameras"]) - 1)
        self._select_camera()

    def _remove_camera(self):
        if len(self.temp_data["cameras"]) <= 1:
            messagebox.showwarning("カメラ", "少なくとも1台のカメラが必要です。", parent=self)
            return
        self.temp_data["cameras"].pop(self.active_camera_index)
        self.active_camera_index = max(0, self.active_camera_index - 1)
        self.camera_selector["values"] = self._camera_display_names()
        self.camera_selector.current(self.active_camera_index)
        self._load_active_camera()
        self._mark_changed()

    def setup_cam(self):
        pane = tk.PanedWindow(self.t_cam, orient=tk.HORIZONTAL,
                              bg=COLOR_BG_MAIN, sashwidth=5, sashrelief="flat")
        pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # =================== 左パネル: 設定 ===================
        left_f = tk.Frame(pane, bg=COLOR_BG_MAIN)
        pane.add(left_f, minsize=520, stretch="always")
        sc_canvas, sc = self.create_scrollable_panel(left_f)
        outer, inner = create_card(sc, "カメラ設定")
        outer.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # --- カメラインデックス + プレビュー操作 ---
        idx_block = tk.Frame(inner, bg=COLOR_BG_PANEL)
        idx_block.pack(fill=tk.X, pady=6)

        camera_row = tk.Frame(idx_block, bg=COLOR_BG_PANEL)
        camera_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(camera_row, text="対象カメラ:", font=FONT_SET_LBL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=10, anchor="w").pack(side=tk.LEFT)
        self.camera_selector = ttk.Combobox(camera_row, textvariable=self.v_active_camera,
                                            values=self._camera_display_names(), state="readonly", width=22)
        self.camera_selector.current(self.active_camera_index)
        self.camera_selector.pack(side=tk.LEFT, padx=(6, 8))
        self.camera_selector.bind("<<ComboboxSelected>>", self._select_camera)
        tk.Button(camera_row, text="+ カメラ追加", command=self._add_camera, font=FONT_BOLD,
                  bg=COLOR_OK, fg="black", relief="flat", padx=12, pady=4).pack(side=tk.LEFT, padx=4)
        tk.Button(camera_row, text="カメラ削除", command=self._remove_camera, font=FONT_BOLD,
                  bg=COLOR_NG, fg="white", relief="flat", padx=12, pady=4).pack(side=tk.LEFT, padx=4)

        name_row = tk.Frame(idx_block, bg=COLOR_BG_PANEL)
        name_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(name_row, text="表示名:", font=FONT_SET_LBL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=10, anchor="w").pack(side=tk.LEFT)
        self._entry(name_row, self.v_camera_name, width=22, key_path="cameras.name").pack(side=tk.LEFT, padx=(6, 0))

        row_idx = tk.Frame(idx_block, bg=COLOR_BG_PANEL)
        row_idx.pack(fill=tk.X)
        tk.Label(row_idx, text="カメラNo:", font=FONT_SET_LBL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=10, anchor="w").pack(side=tk.LEFT)
        sb = self._spinbox(row_idx, self.v_capture_device, 0, 99, 1, width=8,
                           key_path="camera.capture_device")
        sb.pack(side=tk.LEFT, padx=(6, 0))
        Tooltip(sb, "カメラの接続番号（通常は 0 または 1）")

        row_prev = tk.Frame(idx_block, bg=COLOR_BG_PANEL)
        row_prev.pack(fill=tk.X, pady=(8, 0))
        btn_start_prev = tk.Button(
            row_prev, text="▶ プレビュー開始", font=FONT_NORMAL,
            bg=COLOR_OK, fg="black", relief="flat", padx=10, pady=4,
            command=self._start_tab_preview)
        btn_start_prev.pack(side=tk.LEFT, padx=(0, 6))
        tk.Button(row_prev, text="■ 停止", font=FONT_NORMAL,
                  bg="#546E7A", fg="white", relief="flat", padx=10, pady=4,
                  command=self._stop_tab_preview).pack(side=tk.LEFT)

        # --- 区切り線 ---
        tk.Frame(inner, bg=COLOR_BORDER, height=1).pack(fill=tk.X, pady=10)

        # --- 調整エリアステータス ---
        roi_row = tk.Frame(inner, bg=COLOR_BG_PANEL)
        roi_row.pack(fill=tk.X, pady=(0, 8))
        tk.Label(roi_row, text="指定エリア:", font=FONT_SET_LBL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, width=10, anchor="w").pack(side=tk.LEFT)
        tk.Label(roi_row, textvariable=self._roi_status_var, font=FONT_SET_VAL,
                 bg=COLOR_BG_PANEL, fg=COLOR_ACCENT, anchor="w").pack(side=tk.LEFT)
        tk.Button(roi_row, text="指定解除", font=FONT_NORMAL,
                  bg="#546E7A", fg="white", relief="flat", padx=8,
                  command=self._reset_roi).pack(side=tk.RIGHT, padx=4)

        # --- プロパティ設定 ---
        tk.Label(inner,
                 text="カメラプロパティ  ('-1' = カメラデフォルト)",
                 font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w").pack(
                 fill=tk.X, pady=(0, 6))

        props_f = tk.Frame(inner, bg=COLOR_BG_PANEL)
        props_f.pack(fill=tk.X)
        props_f.columnconfigure(1, weight=1)

        for c, txt in enumerate(("プロパティ", "値", "操作")):
            tk.Label(props_f, text=txt, font=FONT_SET_LBL, bg=COLOR_BG_PANEL,
                     fg=COLOR_TEXT_SUB, anchor="w").grid(row=0, column=c, sticky="w", padx=(0, 8), pady=(0, 4))

        # プロパティ行: (key, label, from_, to, inc, tooltip)
        _props = [
            ("focus",      "フォーカス",   0,   1024, 1,
             "レンズの焦点。オート時はカメラ側が自動制御。(0≤1024)"),
            ("gain",       "ゲイン",       0,   255, 1,
             "センサー感度。オート時はカメラ側が自動制御。"),
            ("exposure",   "露出",         -20, 20000, 1,
             "シャッター速度。オート時はカメラ側が自動制御。"),
            ("brightness", "明るさ",       0,   255, 1,
             "画像全体の明るさ。オート時はカメラ側が自動制御。"),
            ("contrast",   "コントラスト", 0,   255, 1,
             "画像の明暗差。オート時はカメラ側が自動制御。"),
        ]

        self._prop_spinboxes = {}
        self._prop_autobuttons = {}

        for row_idx, (key, lbl, fr, to, inc, tip) in enumerate(_props, start=1):
            var = self._prop_vars[key]
            auto_var = self._auto_vars[key]

            tk.Label(props_f, text=f"{lbl}:", font=FONT_SET_LBL,
                     bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w").grid(
                     row=row_idx, column=0, sticky="w", pady=4)

            sp = self._spinbox(props_f, var, fr, to, inc, width=8)
            sp.grid(row=row_idx, column=1, sticky="w", padx=(0, 8), pady=4)
            Tooltip(sp, tip)
            self._prop_spinboxes[key] = sp

            # 値の変更時に即時カメラへ反映
            def _make_prop_tracer(k=key):
                return lambda *a: (self._mark_changed(), self._apply_prop_to_cap(k))
            var.trace_add("write", _make_prop_tracer())

            ctrl_f = tk.Frame(props_f, bg=COLOR_BG_PANEL)
            ctrl_f.grid(row=row_idx, column=2, sticky="w", pady=4)

            btn_a = tk.Button(
                ctrl_f, text="自動調整", font=FONT_NORMAL,
                bg="#455A64", fg="white", relief="flat", padx=8, pady=2,
                command=lambda k=key: self._run_auto_tune(k))
            btn_a.pack(side=tk.LEFT)
            Tooltip(btn_a, f"右パネルの指定エリアを使って{lbl}を自動最適化")
            self._prop_autobuttons[key] = btn_a

            def _make_auto_toggle(k=key):
                def _on_toggle():
                    self._mark_changed()
                    self._update_prop_state(k)
                    self._apply_prop_to_cap(k)
                return _on_toggle

            cb_auto = tk.Checkbutton(
                ctrl_f, text="オート", variable=auto_var,
                font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
                selectcolor=COLOR_BG_INPUT, command=_make_auto_toggle(key))
            cb_auto.pack(side=tk.LEFT, padx=(8, 0))
            Tooltip(cb_auto, f"{lbl}をカメラの自動制御にします（数値入力・自動調整は無効になります）。")

        self._update_all_prop_states()

        # =================== 右パネル: プレビュー + 範囲指定 ===================
        right_f = tk.Frame(pane, bg=COLOR_BG_MAIN)
        pane.add(right_f, minsize=480, stretch="always")
        r_outer, r_inner = create_card(right_f, "プレビュー / 調整エリア指定")
        r_outer.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # ステータスバー（固定高さで折り返しによるガタつきを防止）
        stat_f = tk.Frame(r_inner, bg=COLOR_BG_PANEL, height=24)
        stat_f.pack(fill=tk.X, pady=(0, 4))
        stat_f.pack_propagate(False)
        tk.Label(stat_f, textvariable=self._tune_status_var, font=FONT_SET_VAL,
                 bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w").pack(
                 side=tk.LEFT, fill=tk.BOTH, expand=True)

        # プレビューキャンバス
        self._preview_canvas = tk.Canvas(r_inner, bg="#1a1a1a", cursor="crosshair")
        self._preview_canvas.pack(fill=tk.BOTH, expand=True)

        # マウスイベント (調整エリア選択)
        self._preview_canvas.bind("<ButtonPress-1>",   self._roi_on_press)
        self._preview_canvas.bind("<B1-Motion>",       self._roi_on_drag)
        self._preview_canvas.bind("<ButtonRelease-1>", self._roi_on_release)

        # ガイドラベル
        tk.Label(r_inner,
                 text="▶ドラッグで調整エリア指定 → 左の「自動調整」ボタンで最適化 ",
                 font=FONT_NORMAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w"
                 ).pack(fill=tk.X, pady=(4, 0))

    # ---- 画素数設定タブ ----
    def setup_res(self):
        outer, inner = create_card(self.t_res, "解像度・画素数設定")
        outer.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)

        # 基本撮影設定
        f_cap = tk.Frame(inner, bg=COLOR_BG_PANEL)
        f_cap.pack(fill=tk.X, pady=10)
        tk.Label(f_cap, text="本撮影時の静止画解像度:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=25, anchor="w").pack(side=tk.LEFT)
        
        friendly_opts = [_to_friendly(o) for o in RES_OPTIONS]
        cb_cap = ttk.Combobox(f_cap, textvariable=self.v_capture_resolution, values=friendly_opts, font=FONT_SET_VAL, state="normal", width=25)
        cb_cap.pack(side=tk.LEFT, padx=10)
        Tooltip(cb_cap, "トリガー検知後にカメラから取得する高解像度の静止画サイズを設定します。リストにない場合は直接手入力（例: 3840x2880）も可能です。")
        def _on_cap_change(*a):
            self._mark_changed()
        self.v_capture_resolution.trace_add("write", _on_cap_change)

        # プレビュー表示設定
        f_prev = tk.Frame(inner, bg=COLOR_BG_PANEL)
        f_prev.pack(fill=tk.X, pady=20)
        tk.Label(f_prev, text="常時プレビュー解像度:", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, width=25, anchor="w").pack(side=tk.LEFT)
        
        friendly_prev_opts = [_to_friendly(o) for o in RES_OPTIONS_PREVIEW]
        cb_prev = ttk.Combobox(f_prev, textvariable=self.v_preview_resolution, values=friendly_prev_opts, font=FONT_SET_VAL, state="readonly", width=25)
        cb_prev.pack(side=tk.LEFT, padx=10)
        Tooltip(cb_prev, "待機時のカメラプレビュー描画サイズ。軽く設定することで動作の遅延を防ぎます。")
        def _on_prev_change(*a):
            self._mark_changed()
        self.v_preview_resolution.trace_add("write", _on_prev_change)

    # ---- GPIO設定タブ ----
    def setup_gpio(self):
        main_f = tk.Frame(self.t_gpio, bg=COLOR_BG_MAIN)
        main_f.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 3カラム構造： 左(ピン番号設定), 中(40Pinマップ), 右(出力テスト/時間)
        col_left = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        col_mid = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_mid.pack(side=tk.LEFT, fill=tk.Y, padx=5)

        col_right = tk.Frame(main_f, bg=COLOR_BG_MAIN)
        col_right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=5)

        # 1. 左カラム: ピン番号入力
        outer_p, inner_p = create_card(col_left, "GPIOピン定義 (BCM番号)")
        outer_p.pack(fill=tk.X, pady=(0, 10))

        # フォーカス監視用のフックを仕込んだ行生成関数
        def _make_pin_row(parent, label, var, tip):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6)
            lbl = tk.Label(row, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=18)
            lbl.pack(side=tk.LEFT)
            sp = self._spinbox(row, var, 2, 27, 1, width=8)
            sp.pack(side=tk.RIGHT, padx=5)
            Tooltip(lbl, tip)

            # フォーカス時に active_entry をセット
            def _on_focus(e, w=sp, v=var):
                self.active_entry = (w, v)
            sp.bind("<FocusIn>", _on_focus)

        _make_pin_row(inner_p, "トリガー入力:", self.v_trigger_pin, "撮影を起動するための入力ピンです。")
        _make_pin_row(inner_p, "運転中信号出力:", self.v_system_running_pin, "アプリが起動して稼働している間、常時出力されます。")
        _make_pin_row(inner_p, "OK出力:", self.v_output_ok_pin, "撮影が正常に完了したことを外部に伝える出力ピンです。")
        _make_pin_row(inner_p, "NG出力:", self.v_output_ng_pin, "カメラエラーなどの撮影異常が発生した際の出力ピンです。")

        # 2. 中カラム: Raspberry Pi 40Pin Map
        self.show_gpio_map(col_mid)

        # 3. 右カラム: パルス出力時間とテスト出力
        outer_t, inner_t = create_card(col_right, "パルス時間と出力テスト")
        outer_t.pack(fill=tk.X, pady=(0, 10))

        def _make_duration_row(parent, label, var, tip):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6)
            lbl = tk.Label(row, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=18)
            lbl.pack(side=tk.LEFT)
            sp = self._spinbox(row, var, 0.1, 5.0, 0.1, width=8)
            sp.pack(side=tk.RIGHT, padx=5)
            Tooltip(lbl, tip)

        _make_duration_row(inner_t, "OK出力時間 (秒):", self.v_output_ok_duration, "OK出力信号をONにし続ける時間です。")
        _make_duration_row(inner_t, "NG出力時間 (秒):", self.v_output_ng_duration, "NG出力信号をONにし続ける時間です。")

        # 模擬出力テストボタン
        tk.Label(inner_t, text="▼ GPIO手動テスト (システム停止中のみ機能)", font=FONT_SET_LBL, bg=COLOR_BG_PANEL, fg=COLOR_ACCENT).pack(anchor="w", pady=(15, 5))
        
        btn_grid = tk.Frame(inner_t, bg=COLOR_BG_PANEL)
        btn_grid.pack(fill=tk.X, pady=5)

        def _trigger_test_pulse(pin_var, duration_var):
            # GPIOのOK/NGのテストパルスを送る (ハードウェアがある場合のみ)
            # app_instance を呼び出してパルスを模擬実行する
            app = getattr(self.master, "app_instance", None)
            if app:
                try:
                    pin = int(pin_var.get())
                    sec = float(duration_var.get())
                    app.pulse_test_output(pin, sec)
                except Exception as e:
                    messagebox.showerror("テストエラー", f"出力テスト失敗: {e}", parent=self)

        b_ok = tk.Button(btn_grid, text="OK出力テスト", font=FONT_BOLD, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", width=13,
                         command=lambda: _trigger_test_pulse(self.v_output_ok_pin, self.v_output_ok_duration))
        b_ok.pack(side=tk.LEFT, padx=5, pady=5)

        b_ng = tk.Button(btn_grid, text="NG出力テスト", font=FONT_BOLD, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", width=13,
                         command=lambda: _trigger_test_pulse(self.v_output_ng_pin, self.v_output_ng_duration))
        b_ng.pack(side=tk.LEFT, padx=5, pady=5)

    def show_gpio_map(self, parent):
        outer, inner = create_card(parent, "Pi 40Pin Map (クリックで入力)")
        outer.pack(fill=tk.BOTH, expand=True)

        def _on_pin_clicked(bcm_val):
            widget, var = self.active_entry
            if widget and var and bcm_val is not None:
                var.set(str(bcm_val))
                self._mark_changed()
                widget.focus_set()

        pins = [
            (1, "3.3V", None),   (2, "5V", None),
            (3, "GPIO 2", 2),    (4, "5V", None),
            (5, "GPIO 3", 3),    (6, "GND", None),
            (7, "GPIO 4", 4),    (8, "GPIO 14", 14),
            (9, "GND", None),    (10, "GPIO 15", 15),
            (11, "GPIO 17", 17), (12, "GPIO 18", 18),
            (13, "GPIO 27", 27), (14, "GND", None),
            (15, "GPIO 22", 22), (16, "GPIO 23", 23),
            (17, "3.3V", None),  (18, "GPIO 24", 24),
            (19, "GPIO 10", 10), (20, "GND", None),
            (21, "GPIO 9", 9),   (22, "GPIO 25", 25),
            (23, "GPIO 11", 11), (24, "GPIO 8", 8),
            (25, "GND", None),   (26, "GPIO 7", 7),
            (27, "ID_SD", None), (28, "ID_SC", None),
            (29, "GPIO 5", 5),   (30, "GND", None),
            (31, "GPIO 6", 6),   (32, "GPIO 12", 12),
            (33, "GPIO 13", 13), (34, "GND", None),
            (35, "GPIO 19", 19), (36, "GPIO 16", 16),
            (37, "GPIO 26", 26), (38, "GPIO 20", 20),
            (39, "GND", None),   (40, "GPIO 21", 21)
        ]

        mf = tk.Frame(inner, bg=COLOR_BG_PANEL)
        mf.pack(pady=5, padx=5)

        for i, (pno, name, bcm) in enumerate(pins):
            col_idx = 0 if i % 2 == 0 else 2
            row_idx = i // 2
            
            lbl_no = tk.Label(mf, text=str(pno), font=(FONT_FAMILY, 9, "bold"),
                              width=3, bg="#222", fg="white")
            
            lbl_color = "#444"
            if "V" in name: lbl_color = "#8D6E63"
            if "GND" in name: lbl_color = "#212121"
            
            lbl_name = tk.Label(mf, text=name, font=(FONT_FAMILY, 9),
                                width=10, bg=lbl_color, fg=COLOR_TEXT_MAIN,
                                padx=2, pady=1, relief="flat")

            if i % 2 == 0:
                lbl_no.grid(row=row_idx, column=0, padx=1, pady=1)
                lbl_name.grid(row=row_idx, column=1, padx=(1, 5), pady=1, sticky="w")
            else:
                lbl_name.grid(row=row_idx, column=2, padx=(5, 1), pady=1, sticky="e")
                lbl_no.grid(row=row_idx, column=3, padx=1, pady=1)

            if bcm is not None:
                def make_handler(b=bcm): return lambda e: _on_pin_clicked(b)
                lbl_no.bind("<Button-1>", make_handler())
                lbl_name.bind("<Button-1>", make_handler())
                lbl_no.config(cursor="hand2")
                lbl_name.config(cursor="hand2")
                Tooltip(lbl_name, f"クリックして選択中のピン項目に BCM {bcm} を自動入力します。")

    # ---- システム設定タブ ----
    def setup_sys(self):
        scroll_canvas, scroll_f = self.create_scrollable_panel(self.t_sys)

        # 1. 撮影・タイミングパラメータ
        outer_param, inner_param = create_card(scroll_f, "撮影パラメータ")
        outer_param.pack(fill=tk.X, padx=20, pady=(10, 10))

        def _make_sys_row(parent, label, var, tip, from_=1, to=99):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6, padx=10)
            lbl = tk.Label(row, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=25)
            lbl.pack(side=tk.LEFT)
            sp = self._spinbox(row, var, from_, to, 1 if isinstance(from_, int) else 0.1, width=8)
            sp.pack(side=tk.RIGHT, padx=5)
            Tooltip(lbl, tip)

        _make_sys_row(inner_param, "撮影回数 (回):", self.v_capture_count, "トリガー受信時に連続して撮影する最大枚数です。", 1, 99)
        _make_sys_row(inner_param, "撮影間隔 (秒):", self.v_burst_interval, "連続撮影時の撮影フレーム間の時間間隔です。", 0.1, 10.0)
        _make_sys_row(inner_param, "結果画像表示時間 (秒):", self.v_result_display_time, "撮影結果を画面に表示し続ける時間（秒）です。", 0.5, 10.0)
        _make_sys_row(inner_param, "プレビュー更新レート (FPS):", self.v_preview_fps, "リアルタイムプレビューの表示更新フレームレートです (1〜60 fps)。", 1, 60)

        # 2. 保存パス設定
        outer_path, inner_path = create_card(scroll_f, "保存フォルダパス設定")
        outer_path.pack(fill=tk.X, padx=20, pady=10)

        def _make_path_row(parent, label, var, tip, key_p="paths.log"):
            row = tk.Frame(parent, bg=COLOR_BG_PANEL)
            row.pack(fill=tk.X, pady=6, padx=10)
            lbl = tk.Label(row, text=label, font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN, anchor="w", width=22)
            lbl.pack(side=tk.LEFT)
            
            btn_browse = tk.Button(row, text="参照...", font=FONT_NORMAL, bg=COLOR_BG_INPUT, fg=COLOR_TEXT_MAIN, relief="flat", padx=10,
                                   command=lambda: self.browse_folder(var))
            btn_browse.pack(side=tk.RIGHT, padx=5)
            
            ent = self._entry(row, var, width=32, key_path=key_p)
            ent.pack(side=tk.RIGHT, padx=5)
            Tooltip(lbl, tip)

        _make_path_row(inner_path, "ログフォルダ:", self.v_log_dir, "アプリログファイルを保存するディレクトリ。", "paths.log")
        _make_path_row(inner_path, "結果保存フォルダ:", self.v_results_dir, "撮影された高解像度静止画を保存するディレクトリ。", "paths.results")

        # 3. 容量監視 / 自動削除
        outer_cap, inner_cap = create_card(scroll_f, "容量監視 / 自動削除")
        outer_cap.pack(fill=tk.X, padx=20, pady=10)

        r_ad = tk.Frame(inner_cap, bg=COLOR_BG_PANEL)
        r_ad.pack(fill=tk.X, pady=6, padx=10)
        cb = tk.Checkbutton(
            r_ad, text="古い結果画像を自動削除する",
            variable=self.v_auto_delete_enabled, onvalue=True, offvalue=False,
            font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, relief="flat"
        )
        cb.pack(side=tk.LEFT)
        Tooltip(cb, "容量が上限を超えると、保存フォルダ内の古い画像から順番に自動削除します。")
        
        def _on_ad_change(*a):
            self._mark_changed()
        self.v_auto_delete_enabled.trace_add("write", _on_ad_change)

        r_mg = tk.Frame(inner_cap, bg=COLOR_BG_PANEL)
        r_mg.pack(fill=tk.X, pady=6, padx=10)
        tk.Label(r_mg, text="容量上限 (GB):", font=FONT_SET_VAL, bg=COLOR_BG_PANEL,
                 fg=COLOR_TEXT_MAIN, anchor="w", width=20).pack(side=tk.LEFT)
        sp_mg = self._spinbox(r_mg, self.v_max_results_gb, 1.0, 9999.0, 0.5, width=8,
                              key_path="system.max_results_gb")
        sp_mg.pack(side=tk.LEFT, padx=(6, 0))
        Tooltip(sp_mg, "結果保存フォルダの容量上限(GB)。超えると古いファイルから自動削除します。")
        def _on_mg_change(*a):
            self._mark_changed()
        self.v_max_results_gb.trace_add("write", _on_mg_change)

        v_used = tk.StringVar(value="現在の使用量: 計算中...")
        lbl_used = tk.Label(inner_cap, textvariable=v_used, font=FONT_SET_VAL,
                            bg=COLOR_BG_PANEL, fg=COLOR_TEXT_SUB, anchor="w")
        lbl_used.pack(fill=tk.X, pady=(4, 0), padx=10)

        def _calc_storage():
            _res_dir = self.v_results_dir.get()
            try:
                if _res_dir and os.path.exists(_res_dir):
                    from pathlib import Path as _Path
                    _used = sum(f.stat().st_size for f in _Path(_res_dir).rglob('*') if f.is_file())
                    _used_gb = _used / (1024**3)
                    v_used.set(f"現在の使用量: {_used_gb:.2f} GB")
                else:
                    v_used.set("現在の使用量: 0.00 GB (フォルダ未作成)")
            except Exception as _e:
                v_used.set(f"現在の使用量: エラー ({_e})")

        import threading as _threading
        _threading.Thread(target=_calc_storage, daemon=True).start()

        # 4. コミット設定（ドアライン対応）
        outer_cm, inner_cm = create_card(scroll_f, "コミット設定")
        outer_cm.pack(fill=tk.X, padx=20, pady=10)

        r_hs = tk.Frame(inner_cm, bg=COLOR_BG_PANEL)
        r_hs.pack(fill=tk.X, pady=6, padx=10)
        cb_hs = tk.Checkbutton(
            r_hs, text="コミット番号を 0.5 刻みで進める（ドアライン対応）",
            variable=self.v_commit_half_step, onvalue=True, offvalue=False,
            font=FONT_SET_VAL, bg=COLOR_BG_PANEL, fg=COLOR_TEXT_MAIN,
            activebackground=COLOR_BG_PANEL, activeforeground=COLOR_TEXT_MAIN,
            selectcolor=COLOR_BG_INPUT, relief="flat",
            command=self._mark_changed
        )
        cb_hs.pack(side=tk.LEFT)
        Tooltip(cb_hs, "有効にすると、撮影完了ごとにコミット番号が +0.5 ずつ増えます。\n"
                       "ドアライン撮影など奇数・偶数を区別する運用に使用します。")

    def browse_folder(self, var):
        folder = filedialog.askdirectory(parent=self, initialdir=var.get())
        if folder:
            var.set(folder)
            self._mark_changed()

    def validate_pins(self):
        used_pins = {}
        def _get_val(v):
            if v is None: return -1
            s_val = str(v).strip()
            if not s_val:
                return -1
            try:
                return int(s_val)
            except:
                return -1

        all_pins = [
            (_get_val(self.v_trigger_pin.get()), "トリガー入力"),
            (_get_val(self.v_system_running_pin.get()), "運転中信号出力"),
            (_get_val(self.v_output_ok_pin.get()), "OK出力"),
            (_get_val(self.v_output_ng_pin.get()), "NG出力")
        ]

        for p, name in all_pins:
            if p == -1:
                messagebox.showerror("バリデーションエラー", f"「{name}」のピン番号が空白または無効な数値です。", parent=self)
                return False
            if p not in VALID_BCM_PINS:
                messagebox.showerror("バリデーションエラー",
                                     f"「{name}」のピン番号 {p} は有効なBCMピン番号ではありません。\n"
                                     f"使用可能なピン: {sorted(VALID_BCM_PINS)}", parent=self)
                return False
            if p in used_pins:
                messagebox.showerror("バリデーションエラー", f"ピン {p} が重複して設定されています：\n「{name}」と「{used_pins[p]}」", parent=self)
                return False
            used_pins[p] = name

        return True

    def _safe_status(self, msg):
        """バックグラウンドスレッドから安全にステータスラベルを更新する"""
        try:
            if self.winfo_exists():
                self.after(0, lambda m=msg: self._tune_status_var.set(m))
        except Exception:
            pass

    # ----------------------------------------------------------------
    # タブ内プレビュー管理
    # ----------------------------------------------------------------
    def _start_tab_preview(self):
        """rightパネルのキャンバスにカメラ映像を表示する"""
        self._stop_tab_preview()
        try:
            c_idx = int(self.v_capture_device.get())
        except ValueError:
            messagebox.showerror("エラー", "カメラインデックスの値が不正です。", parent=self)
            return
        if sys.platform.startswith("linux"):
            backend = cv2.CAP_V4L2
        elif sys.platform.startswith("win"):
            backend = cv2.CAP_DSHOW
        else:
            backend = cv2.CAP_ANY
        cap = cv2.VideoCapture(c_idx, backend)
        if not cap or not cap.isOpened():
            messagebox.showerror("エラー",
                                 f"カメラ (インデックス: {c_idx}) を開けませんでした。", parent=self)
            return
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._cam_preview_cap     = cap
        self._cam_preview_running = True
        # プレビュー開始時に現在の設定プロパティを全適用
        self._apply_all_props_to_cap()
        self._tune_status_var.set("プレビュー映像上でドラッグして調整エリアを指定してください")
        self._cam_preview_thread = threading.Thread(
            target=self._tab_preview_worker, daemon=True)
        self._cam_preview_thread.start()

    def _update_prop_state(self, key):
        """オート選択状態に応じてスピンボックスと自動調整ボタンの有効/無効を切り替える"""
        is_auto = self._auto_vars[key].get()
        sp = getattr(self, "_prop_spinboxes", {}).get(key)
        btn = getattr(self, "_prop_autobuttons", {}).get(key)
        if sp:
            sp.config(state="disabled" if is_auto else "normal")
        if btn:
            btn.config(state="disabled" if is_auto else "normal")

    def _update_all_prop_states(self):
        """全プロパティの有効/無効状態を一括更新"""
        for key in ("focus", "gain", "exposure", "brightness", "contrast"):
            self._update_prop_state(key)

    def _apply_prop_to_cap(self, key):
        """プレビュー実行中のカメラに指定プロパティをリアルタイム適用する"""
        cap = self._cam_preview_cap
        if not cap or not cap.isOpened():
            return
        prop_map = {
            "focus":      cv2.CAP_PROP_FOCUS,
            "gain":       cv2.CAP_PROP_GAIN,
            "exposure":   cv2.CAP_PROP_EXPOSURE,
            "brightness": cv2.CAP_PROP_BRIGHTNESS,
            "contrast":   cv2.CAP_PROP_CONTRAST,
        }
        auto_prop_map = {
            "focus":      cv2.CAP_PROP_AUTOFOCUS,
            "gain":       getattr(cv2, "CAP_PROP_AUTOGAIN", 22),
            "exposure":   cv2.CAP_PROP_AUTO_EXPOSURE,
        }
        try:
            is_auto = self._auto_vars.get(key, tk.BooleanVar(value=False)).get()
            auto_id = auto_prop_map.get(key)
            if auto_id is not None:
                if key == "exposure":
                    cap.set(auto_id, 0.75 if is_auto else 0.25)
                else:
                    cap.set(auto_id, 1 if is_auto else 0)

            # オート有効時は手動設定を適用しない
            if is_auto:
                return

            prop_id = prop_map.get(key)
            if prop_id is not None:
                var = self._prop_vars.get(key)
                if var:
                    val_str = var.get().strip()
                    if val_str and val_str != "-1":
                        val = int(val_str)
                        cap.set(prop_id, val)
        except Exception:
            pass

    def _apply_all_props_to_cap(self):
        """プレビューカメラに全プロパティを即時適用"""
        for key in ("focus", "gain", "exposure", "brightness", "contrast"):
            self._apply_prop_to_cap(key)

    def _stop_tab_preview(self):
        self._cam_preview_running = False
        if self._cam_preview_cap:
            self._cam_preview_cap.release()
            self._cam_preview_cap = None
        th = self._cam_preview_thread
        if th and th.is_alive():
            th.join(timeout=0.5)
        self._cam_preview_thread = None
        # キャンバスをクリア
        try:
            if self._preview_canvas and self._preview_canvas.winfo_exists():
                self._preview_canvas.delete("preview")
        except Exception:
            pass

    def _tab_preview_worker(self):
        while self._cam_preview_running:
            cap = self._cam_preview_cap
            if cap is None:
                break
            try:
                ret, frame = cap.read()
            except Exception:
                break
            if ret and frame is not None and frame.size > 0:
                self._roi_last_frame = frame.copy()
                try:
                    canvas = self._preview_canvas
                    cw = canvas.winfo_width()
                    ch = canvas.winfo_height()
                except Exception:
                    break
                if cw > 1 and ch > 1:
                    h, w = frame.shape[:2]
                    scale = min(cw / w, ch / h)
                    self._roi_state["scale"] = scale
                    disp = cv2.resize(frame, (int(w * scale), int(h * scale)))
                    img = ImageTk.PhotoImage(
                        Image.fromarray(cv2.cvtColor(disp, cv2.COLOR_BGR2RGB)))
                    try:
                        if self.winfo_exists():
                            self.after(0, lambda i=img: self._update_tab_canvas(i))
                    except Exception:
                        break
            time.sleep(0.04)

    def _update_tab_canvas(self, img):
        try:
            canvas = self._preview_canvas
            if not canvas or not canvas.winfo_exists():
                return
        except Exception:
            return
        canvas.delete("preview")
        canvas.create_image(0, 0, anchor="nw", image=img, tags="preview")
        canvas.img_ref = img  # GC防止
        if self._roi_rect_id:
            try:
                canvas.tag_raise(self._roi_rect_id)
            except Exception:
                self._roi_rect_id = None

    # ----------------------------------------------------------------
    # 調整エリア描画イベント
    # ----------------------------------------------------------------
    def _roi_on_press(self, e):
        canvas = self._preview_canvas
        self._roi_state.update({"x0": e.x, "y0": e.y, "x1": e.x, "y1": e.y, "set": False})
        if self._roi_rect_id:
            try:
                canvas.delete(self._roi_rect_id)
            except Exception:
                pass
        self._roi_rect_id = canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#FFD600", width=2, tags="roi")

    def _roi_on_drag(self, e):
        self._roi_state["x1"] = e.x
        self._roi_state["y1"] = e.y
        if self._roi_rect_id:
            try:
                self._preview_canvas.coords(
                    self._roi_rect_id,
                    self._roi_state["x0"], self._roi_state["y0"],
                    self._roi_state["x1"], self._roi_state["y1"])
            except Exception:
                pass

    def _roi_on_release(self, e):
        self._roi_state["x1"] = e.x
        self._roi_state["y1"] = e.y
        w = abs(self._roi_state["x1"] - self._roi_state["x0"])
        h = abs(self._roi_state["y1"] - self._roi_state["y0"])
        if w > 10 and h > 10:
            self._roi_state["set"] = True
            self._roi_status_var.set(f"エリア指定済: {w}x{h}px")
            self._tune_status_var.set(f"エリア: {w}x{h}px  ←左の「自動調整」で最適化")
        else:
            self._roi_state["set"] = False
            self._roi_status_var.set("エリア未選択（小さすぎ）")
            self._tune_status_var.set("もう少し大きくドラッグしてください")

    def _reset_roi(self):
        self._roi_state["set"] = False
        if self._roi_rect_id:
            try:
                self._preview_canvas.delete(self._roi_rect_id)
            except Exception:
                pass
            self._roi_rect_id = None
        self._roi_status_var.set("エリア未指定")
        self._tune_status_var.set("プレビュー映像上でドラッグして調整エリアを指定してください")

    # ----------------------------------------------------------------
    # 自動調整 (共有調整エリアを使い回し)
    # ----------------------------------------------------------------
    def _run_auto_tune(self, prop_key):
        if self._cam_preview_cap is None or not self._cam_preview_cap.isOpened():
            messagebox.showwarning("警告",
                                   "先に「▶ プレビュー開始」を押してください。", parent=self)
            return
        if not self._roi_state.get("set", False):
            messagebox.showwarning("警告",
                                   "右パネルのプレビュー上で調整エリアをドラッグ指定してください。", parent=self)
            return
        label_map = {"focus": "フォーカス", "gain": "ゲイン",
                     "exposure": "露出", "brightness": "明るさ", "contrast": "コントラスト"}
        self._tune_status_var.set(f"{label_map.get(prop_key, prop_key)} のスイープ開始...")
        threading.Thread(target=self._do_sweep, args=(prop_key,), daemon=True).start()

    def _do_sweep(self, prop_key):
        """指定エリアを使ってプロパティ値を2段階（ラフ探索 → 詳細探索）で最適化"""
        frame = self._roi_last_frame
        if frame is None:
            self._safe_status("フレームを取得できません。プレビューを開始してください。")
            return

        scale = self._roi_state.get("scale", 1.0)
        x0 = int(min(self._roi_state["x0"], self._roi_state["x1"]) / scale)
        y0 = int(min(self._roi_state["y0"], self._roi_state["y1"]) / scale)
        x1 = int(max(self._roi_state["x0"], self._roi_state["x1"]) / scale)
        y1 = int(max(self._roi_state["y0"], self._roi_state["y1"]) / scale)
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(fw, x1), min(fh, y1)
        if x1 <= x0 or y1 <= y0:
            self._safe_status("指定エリアが無効です。再選択してください。")
            return

        # プロパティ設定: (prop_id, min_val, max_val, coarse_step, fine_step, margin, eval_type)
        prop_cfg_map = {
            "focus":      (cv2.CAP_PROP_FOCUS,      0,   1024, 40, 4, 40, "laplacian"),
            "gain":       (cv2.CAP_PROP_GAIN,       0,   255,  20, 2, 20, "std"),
            "exposure":   (cv2.CAP_PROP_EXPOSURE,   -13, 0,    2,  1, 2,  "std"),
            "brightness": (cv2.CAP_PROP_BRIGHTNESS, 0,   255,  20, 4, 20, "std"),
            "contrast":   (cv2.CAP_PROP_CONTRAST,   0,   255,  20, 4, 20, "std"),
        }
        if prop_key not in prop_cfg_map:
            return

        prop_id, min_val, max_val, coarse_step, fine_step, margin, eval_type = prop_cfg_map[prop_key]
        cap = self._cam_preview_cap

        def _evaluate_roi(patch):
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            if eval_type == "laplacian":
                return float(cv2.Laplacian(gray, cv2.CV_64F).var())
            return float(np.std(gray))

        # 1. ラフ探索 (Coarse Scan)
        coarse_vals = list(range(min_val, max_val + 1, coarse_step))
        if coarse_vals[-1] != max_val:
            coarse_vals.append(max_val)

        best_coarse_val = coarse_vals[0]
        best_coarse_score = -1.0
        total_coarse = len(coarse_vals)

        for i, v in enumerate(coarse_vals):
            if not self._cam_preview_running:
                break
            cap.set(prop_id, v)
            time.sleep(0.1)
            for _ in range(3):
                cap.grab()
            ret, fr = cap.read()
            if not ret or fr is None or fr.size == 0:
                continue
            roi_patch = fr[y0:y1, x0:x1]
            score = _evaluate_roi(roi_patch)
            if score > best_coarse_score:
                best_coarse_score = score
                best_coarse_val = v
            pct = int((i + 1) / total_coarse * 50)
            self._safe_status(f"[ラフ探索] {pct}%  値={v}  スコア={score:.1f}  (暫定={best_coarse_val})")

        if not self._cam_preview_running:
            return

        # 2. 詳細探索 (Fine Scan)
        fine_start = max(min_val, best_coarse_val - margin)
        fine_end = min(max_val, best_coarse_val + margin)
        fine_vals = list(range(fine_start, fine_end + 1, fine_step))
        if fine_vals[-1] != fine_end:
            fine_vals.append(fine_end)

        final_best_val = best_coarse_val
        final_best_score = best_coarse_score
        total_fine = len(fine_vals)

        for i, v in enumerate(fine_vals):
            if not self._cam_preview_running:
                break
            cap.set(prop_id, v)
            time.sleep(0.1)
            for _ in range(3):
                cap.grab()
            ret, fr = cap.read()
            if not ret or fr is None or fr.size == 0:
                continue
            roi_patch = fr[y0:y1, x0:x1]
            score = _evaluate_roi(roi_patch)
            if score > final_best_score:
                final_best_score = score
                final_best_val = v
            pct = 50 + int((i + 1) / total_fine * 50)
            self._safe_status(f"[詳細探索] {pct}%  値={v}  スコア={score:.1f}  (最良={final_best_val})")

        # 最終最良値を設定してスピンボックスに反映
        cap.set(prop_id, final_best_val)
        label_map = {"focus": "フォーカス", "gain": "ゲイン",
                     "exposure": "露出", "brightness": "明るさ", "contrast": "コントラスト"}
        var = self._prop_vars[prop_key]
        msg = (f"{label_map.get(prop_key, prop_key)} 自動調整完了！　"
               f"最適値 = {final_best_val}  (スコア: {final_best_score:.1f})")
        try:
            if self.winfo_exists():
                def _apply(bv=final_best_val, m=msg):
                    var.set(str(bv))
                    self._mark_changed()
                    self._tune_status_var.set(m)
                self.after(0, _apply)
        except Exception:
            pass

    # ----------------------------------------------------------------
    # 保存・閉じる
    # ----------------------------------------------------------------
    def save_and_close(self):
        # バリデーション実行
        if not self.validate_pins():
            return

        # 各種入力値チェック
        try:
            int(self.v_capture_device.get())
            int(self.v_focus.get())
            int(self.v_gain.get())
            int(self.v_exposure.get())
            int(self.v_brightness.get())
            int(self.v_contrast.get())
            int(self.v_capture_count.get())
            float(self.v_burst_interval.get())
            float(self.v_result_display_time.get())
            float(self.v_output_ok_duration.get())
            float(self.v_output_ng_duration.get())
            float(self.v_max_results_gb.get())
        except ValueError:
            messagebox.showerror("バリデーションエラー", "入力された数値フィールドに不正な文字が含まれています。", parent=self)
            return

        # 変更された値を一時データに書き戻し
        self._store_active_camera()
        indices = [camera["capture_device"] for camera in self.temp_data["cameras"]]
        if len(indices) != len(set(indices)):
            messagebox.showerror("カメラ設定", "カメラ番号が重複しています。各カメラに異なる番号を設定してください。", parent=self)
            return

        self.temp_data["gpio"]["trigger_pin"] = int(self.v_trigger_pin.get())
        self.temp_data["gpio"]["system_running_pin"] = int(self.v_system_running_pin.get())
        self.temp_data["gpio"]["output_ok_pin"] = int(self.v_output_ok_pin.get())
        self.temp_data["gpio"]["output_ng_pin"] = int(self.v_output_ng_pin.get())
        self.temp_data["gpio"]["output_ok_duration"] = float(self.v_output_ok_duration.get())
        self.temp_data["gpio"]["output_ng_duration"] = float(self.v_output_ng_duration.get())

        self.temp_data["system"]["capture_count"] = int(self.v_capture_count.get())
        self.temp_data["system"]["burst_interval"] = float(self.v_burst_interval.get())
        self.temp_data["system"]["result_display_time"] = float(self.v_result_display_time.get())
        self.temp_data["system"]["auto_delete_enabled"] = self.v_auto_delete_enabled.get()
        self.temp_data["system"]["max_results_gb"] = float(self.v_max_results_gb.get())
        self.temp_data["system"]["commit_half_step"] = self.v_commit_half_step.get()
        try:
            self.temp_data["system"]["preview_fps"] = max(1, min(60, int(self.v_preview_fps.get())))
        except ValueError:
            self.temp_data["system"]["preview_fps"] = 15

        self.temp_data["paths"]["log_dir"] = self.v_log_dir.get().strip()
        self.temp_data["paths"]["results_dir"] = self.v_results_dir.get().strip()

        # クローンデータから実際の設定を更新・保存
        self.settings.data = self.temp_data
        self.settings.save_settings()

        # タブ内プレビューを確実に停止して解放
        self._stop_tab_preview()

        release_modal_toplevel(self)
        if self.on_close_callback:
            self.on_close_callback()
        self.destroy()

    def on_cancel(self):
        self._stop_tab_preview()
        release_modal_toplevel(self)
        # キャンセル時もコールバックを呼んでメイン画面のカメラ・GPIOを再起動させる
        if self.on_close_callback:
            self.on_close_callback()
        self.destroy()

    def show_settings_help(self):
        help_data = {
            "1. カメラ設定": "撮影に使用するUSBカメラの接続確認と設定を行います。\n"
                           "・インデックス: カメラの識別番号（通常は 0, 1 など）です。\n"
                           "・プレビュー / 範囲指定: 右パネルで映像を表示し、ドラッグで調整エリアを1回指定します。"
                           "指定したエリアは全プロパティの「自動調整」で共通利用されます。\n"
                           "・自動調整: 各プロパティ行のボタンで、指定エリア内のコントラストが最大になる値を探索します。",
            "2. 画素数設定": "取得・表示・保存する画像のサイズを設定します。\n"
                           "・本撮影時の静止画解像度: トリガー検知した本撮影で保存する画素数です。大きいほど高精細になります。\n"
                           "・常時プレビュー解像度: メイン画面で常時描画する解像度です。低くすると動作が軽くなります。",
            "3. GPIOピン設定": "Raspberry Piへの配線に適合するBCMピン番号を指定します。\n"
                            "・Pi 40Pin Map: 右図のピンをクリックすると、現在選択中の入力欄にBCMピン番号が自動設定されます。\n"
                            "・出力テスト: 模擬的にパルス信号を外部へ出力するテストボタンです。",
            "4. システム設定": "撮影の動作パラメータとデータ保存フォルダパスの設定を行います。\n"
                           "・撮影回数: トリガー1回に対して何枚連続で撮影するかを設定します。\n"
                           "・撮影間隔: 連続して撮影する際の間隔（秒）です。\n"
                           "・フォルダパス: 各種データ（ログ・画像など）の保存先を「参照」ボタンから絶対パスで指定できます。"
        }
        HelpWindow(self, "詳細設定 操作ガイド", help_data)
