#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
settings.py - 設定管理 (SettingsManager)
"""

import json
import os
import copy
from pathlib import Path

from .constants import SETTINGS_FILE


class SettingsManager:
    def __init__(self):
        self.defaults = {
            "paths": {
                "log_dir": "./logs",
                "results_dir": "./results"
            },
            "gpio": {
                "trigger_pin": 22,
                "trigger_pull_up": True,
                "output_ok_pin": 16,
                "output_ng_pin": 20,
                "system_running_pin": 5,
                "output_ok_duration": 0.5,
                "output_ng_duration": 0.5
            },
            "cameras": [self._default_camera()],
            "system": {
                "capture_count": 5,
                "burst_interval": 0.2,
                "result_display_time": 2.0,
                "commit_half_step": False,
                "auto_delete_enabled": True,
                "max_results_gb": 10.0,
                "preview_fps": 15
            }
        }
        self.data = self.load_settings()

    @staticmethod
    def _default_camera(camera_id="cam_1", name="カメラ 1", device=0):
        return {
            "id": camera_id,
            "name": name,
            "capture_device": device,
            "capture_resolution": "1920x1080",
            "preview_resolution": "640x480",
            "camera_props": {
                "autofocus": True, "auto_gain": False, "auto_exposure": False,
                "auto_brightness": False, "auto_contrast": False,
                "focus": -1, "gain": -1, "exposure": -1, "brightness": -1, "contrast": -1,
            },
        }

    def load_settings(self):
        def merge(a, b):
            for k, v in b.items():
                if isinstance(v, dict):
                    a[k] = merge(a.get(k, {}), v)
                else:
                    if k not in a:
                        a[k] = v
            return a

        try:
            if Path(SETTINGS_FILE).exists():
                with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    # 既存の古いconfig構造からの移行をサポート
                    # 例：camera.capture_deviceなどが無い場合はdefaultsからマージ
                    if "cameras" not in data:
                        legacy = data.pop("camera", {})
                        camera = self._default_camera()
                        camera.update({key: legacy[key] for key in
                                       ("capture_device", "capture_resolution", "preview_resolution")
                                       if key in legacy})
                        if isinstance(legacy.get("camera_props"), dict):
                            camera["camera_props"].update(legacy["camera_props"])
                        data["cameras"] = [camera]
                    for index, camera in enumerate(data["cameras"]):
                        default = self._default_camera(f"cam_{index + 1}", f"カメラ {index + 1}")
                        data["cameras"][index] = merge(camera, default)
                    return merge(data, copy.deepcopy(self.defaults))
            
            # 設定ファイルがない場合は新規作成
            config_data = copy.deepcopy(self.defaults)
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(config_data, f, indent=4, ensure_ascii=False)
            return config_data
        except Exception as e:
            print(f"Error loading settings: {e}")
            return copy.deepcopy(self.defaults)

    def save_settings(self):
        try:
            with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")
