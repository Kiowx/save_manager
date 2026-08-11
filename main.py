#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Steam 游戏存档备份管理器 v1.5.6 — 通用版"""

import os
import sys
import json
import re
import shutil
import logging
import threading
import concurrent.futures
import datetime
import zipfile
import glob
import hashlib
import base64
import fnmatch
import queue
import time
import tempfile
import locale
import struct
import ctypes
import subprocess
import urllib.request
import urllib.error
import urllib.parse
import webbrowser
import html
import tkinter
import weakref
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Optional
from collections import OrderedDict

try:
    import winreg
except ImportError:
    winreg = None

import customtkinter as ctk
from tkinter import filedialog, messagebox
from PIL import Image

try:
    import pystray
    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False

if HAS_TRAY and sys.platform == "win32":
    from pystray._util import win32 as _pystray_win32

    class _DoubleClickTrayIcon(pystray.Icon):
        _WM_LBUTTONUP = getattr(_pystray_win32, "WM_LBUTTONUP", 0x0202)
        _WM_LBUTTONDBLCLK = getattr(_pystray_win32, "WM_LBUTTONDBLCLK", 0x0203)

        def _on_notify(self, wparam, lparam):
            if lparam == self._WM_LBUTTONDBLCLK:
                self()
                return
            if lparam == self._WM_LBUTTONUP:
                return
            return super()._on_notify(wparam, lparam)
else:
    _DoubleClickTrayIcon = None

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler as _WatchdogHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    Observer = None
    _WatchdogHandler = None

    class _WatchdogHandler:
        """空实现，当 watchdog 不可用时的占位类"""
        def on_modified(self, event):
            pass

        def on_created(self, event):
            pass

        def on_deleted(self, event):
            pass

        def on_moved(self, event):
            pass


FileSystemEventHandler = _WatchdogHandler

try:
    from webdav3.client import Client as WebDAVClient
    HAS_WEBDAV = True
    WEBDAV_IMPORT_ERROR = ""
except ImportError as exc:
    HAS_WEBDAV = False
    WEBDAV_IMPORT_ERROR = str(exc)
#  常量与路径
# ══════════════════════════════════════════════

APP_NAME = "Steam Save Manager"
VERSION = "1.5.6"
APP_DIR = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
LEGACY_CONFIG_DIR = Path.home() / ".steam_save_manager"
STARTUP_AUTOSTART_FLAG = "--startup-launch"
STARTUP_LAUNCH = STARTUP_AUTOSTART_FLAG in sys.argv

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def _can_write_dir(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ssm_write_test.tmp"
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        probe.unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.debug("检查目录写入权限失败 %s: %s", path, e)
        return False


def _resolve_config_dir() -> Path:
    portable_cfg = APP_DIR / "config.json"
    legacy_cfg = LEGACY_CONFIG_DIR / "config.json"
    if portable_cfg.exists():
        return APP_DIR
    if legacy_cfg.exists():
        return LEGACY_CONFIG_DIR
    if _can_write_dir(APP_DIR):
        return APP_DIR
    return LEGACY_CONFIG_DIR


def _build_autostart_command(script_path: str, python_path: str) -> str:
    if getattr(sys, "frozen", False):
        return f'"{script_path}" {STARTUP_AUTOSTART_FLAG}'
    pythonw = python_path.replace("python.exe", "pythonw.exe")
    if not os.path.isfile(pythonw):
        pythonw = python_path
    return f'"{pythonw}" "{script_path}" {STARTUP_AUTOSTART_FLAG}'


def _join_thread(thread: Optional[threading.Thread], timeout: float = 1.0):
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)


def _path_is_within(child: Path | str, parent: Path | str) -> bool:
    try:
        child_abs = os.path.normcase(os.path.abspath(str(child)))
        parent_abs = os.path.normcase(os.path.abspath(str(parent)))
        return os.path.commonpath([child_abs, parent_abs]) == parent_abs
    except Exception:
        return False


def _config_batch_depth() -> int:
    return int(getattr(_CONFIG_BATCH_STATE, "depth", 0) or 0)


CONFIG_DIR = _resolve_config_dir()
CONFIG_FILE = CONFIG_DIR / "config.json"
BACKUP_ROOT = APP_DIR / "backups"
LOCK_FILE = CONFIG_DIR / ".lock"
UPDATE_MANIFEST_URL = "https://raw.githubusercontent.com/Kiowx/save_manager/refs/heads/main/update/update.json"
CONFIG_SAVE_LOCK = threading.Lock()
SAVE_IO_LOCK = threading.RLock()
_CONFIG_BATCH_STATE = threading.local()
SAVE_SPEC_SNAPSHOT_CACHE_LOCK = threading.Lock()
SAVE_SPEC_SNAPSHOT_CACHE: dict[tuple, dict] = {}
SAVE_SPEC_SNAPSHOT_CACHE_MAX = 256

ZIP_EXTRACT_MAX_FILES = 200_000
ZIP_EXTRACT_MAX_MEMBER_BYTES = 10 * 1024 * 1024 * 1024
ZIP_EXTRACT_MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024
RESTORE_SAFETY_KEEP_PER_TARGET = 3
LOCAL_CLOUD_REFRESH_TIMEOUT = 6.0
LOCAL_CLOUD_MISSING_PAYLOAD_TIMEOUT = 18.0
LOCAL_CLOUD_REFRESH_INTERVAL = 0.75


class SyncPendingError(RuntimeError):
    """Raised for non-fatal sync states that should be retried later."""

SUPPORTED_LANGUAGES = ("zh-CN", "en")
LANGUAGE_NAMES = {
    "zh-CN": "简体中文",
    "en": "English",
}
WEBDAV_PRESET_OPTIONS = ("generic", "synology", "qnap", "truenas", "nextcloud", "openmediavault")
WEBDAV_PRESET_SUFFIXES = {
    "generic": "",
    "synology": "",
    "qnap": "/webdav",
    "truenas": "/dav",
    "nextcloud": "",
    "openmediavault": "",
}
TRANSLATIONS = {
    "zh-CN": {
        "product_title": "Steam 存档管家",
        "nav_section": "导航",
        "nav_home": "主页",
        "nav_scan": "扫描游戏",
        "nav_games": "游戏列表",
        "nav_backup": "备份记录",
        "nav_settings": "设置",
        "status_auto_on": "▶ 定时备份：{minutes}分钟",
        "status_auto_off": "⏸ 自动备份：关闭",
        "home_title": "欢迎使用 Steam 存档管家",
        "home_subtitle": "自动扫描、备份和还原你的游戏存档",
        "stat_games": "已添加",
        "stat_backups": "总备份",
        "stat_auto": "定时备份",
        "stat_watch": "文件监控",
        "value_games": "{count} 款",
        "value_backups": "{count} 个",
        "switch_on": "开启",
        "switch_off": "关闭",
        "home_backup_all": "🔄  一键备份全部",
        "home_sync_all": "🔄  立即同步全部",
        "home_scan": "🔍  扫描并添加游戏",
        "home_recent": "  📋 最近备份",
        "home_no_backups": "暂无备份记录，快去扫描游戏吧 →",
        "scan_title": "🔍 扫描 Steam 游戏库",
        "scan_start": "开始扫描",
        "scan_add_all": "全部添加",
        "scan_hint": "点击「开始扫描」自动检测已安装的 Steam 游戏",
        "scan_missing_steam": "⚠️ 未检测到 Steam 库路径\n请在设置中配置 Steam 安装路径",
        "backup_title": "💾 备份记录",
        "backup_all_games": "全部游戏",
        "backup_empty": "暂无备份",
        "settings_title": "🔧 设置",
        "settings_subtitle": "根据你的系统环境调整语言、同步和备份行为，修改后会立即生效。",
        "section_general": "基础设置",
        "section_general_sub": "语言、主题与 Steam 路径",
        "section_automation": "自动化与同步",
        "section_automation_sub": "定时备份、文件监控与云同步",
        "section_system": "系统与存储",
        "section_system_sub": "通知、托盘、自启和备份轮转",
        "steam_path": "Steam 安装路径",
        "steam_account": "Steam 账号",
        "steamdb_detection": "SteamDB 存档识别",
        "steamdb_detection_desc": "启用后优先使用 SteamDB 识别；若无结果则回退到普通本地识别。",
        "browse": "浏览",
        "language": "界面语言",
        "language_hint": "默认自动跟随当前系统语言，也可以手动覆盖。",
        "theme": "界面主题",
        "theme_light": "浅色",
        "theme_dark": "深色",
        "auto_backup": "定时自动备份",
        "enable": "启用",
        "interval_minutes": "间隔(分钟)",
        "file_watch": "文件变动监控",
        "cooldown_seconds": "冷却(秒)",
        "watchdog_missing": "⚠ 未安装 watchdog，pip install watchdog",
        "auto_sync": "存档自动同步",
        "sync_folder": "同步文件夹",
        "sync_folder_placeholder": "选择云盘文件夹 (OneDrive / Dropbox / Google Drive)",
        "auto_detect": "自动检测",
        "sync_mode": "同步模式",
        "sync_mode_smart": "智能云存档",
        "sync_mode_bidirectional": "双向同步",
        "sync_mode_upload": "仅上传",
        "sync_mode_download": "仅下载",
        "sync_hint": "智能云存档（推荐）：检测到游戏启动时自动下载并解压云端 ZIP，游戏关闭后自动打包为 ZIP 上传\n双向同步：基于上次同步快照判断单边改动；两边都改时弹出冲突处理，失败任务会自动重试",
        "sync_archive_keep": "云端同步包保留数量",
        "sync_archive_keep_suffix": "个最新 ZIP（0 = 不限）",
        "sync_notify": "同步完成后发送 Windows 桌面通知",
        "webdav_enable": "WebDAV 远程同步",
        "webdav_preset": "NAS / WebDAV 预设",
        "webdav_preset_generic": "通用 WebDAV",
        "webdav_preset_synology": "群晖 Synology",
        "webdav_preset_qnap": "威联通 QNAP",
        "webdav_preset_truenas": "TrueNAS",
        "webdav_preset_nextcloud": "Nextcloud",
        "webdav_preset_openmediavault": "OpenMediaVault",
        "webdav_preset_hint": "常见地址：{hint}",
        "webdav_url": "WebDAV 服务器地址",
        "webdav_url_ph": "https://your-server.com/dav",
        "webdav_base_path": "WebDAV 云端目录",
        "webdav_base_path_ph": "/SteamSaveSync",
        "webdav_username": "用户名",
        "webdav_password": "密码",
        "webdav_verify_ssl": "证书校验",
        "webdav_verify_ssl_desc": "开启 HTTPS 证书校验；NAS 使用自签名证书时可关闭",
        "webdav_test": "测试连接",
        "webdav_testing": "正在测试连接…",
        "webdav_test_ok": "连接成功！",
        "webdav_test_fail": "连接失败",
        "webdav_missing": "WebDAV 组件不可用，请运行 pip install webdavclient3",
        "minimize_tray": "关闭时最小化到托盘",
        "minimize_tray_desc": "关闭窗口时最小化到系统托盘后台运行",
        "tray_missing": "⚠ 未安装 pystray，pip install pystray",
        "autostart": "开机自启",
        "autostart_desc": "开启后登录 Windows 时自动启动",
        "autostart_minimize": "开机自启时最小化",
        "autostart_minimize_desc": "通过开机自启启动时默认最小化到托盘或任务栏",
        "backup_rotation": "备份轮转策略",
        "max_backups": "每个游戏最多保留",
        "max_backups_suffix": "个备份（0 = 不限）",
        "max_backup_size": "总备份大小上限",
        "max_backup_size_suffix": "GB（0 = 不限，超出自动删除最旧备份）",
        "display_page_size": "列表显示数量",
        "games_page_size": "游戏列表每页显示",
        "games_page_size_suffix": "个游戏（5-200）",
        "backup_page_size": "备份记录每页显示",
        "backup_page_size_suffix": "条备份（5-200）",
        "backup_storage": "备份存储位置",
        "backup_storage_placeholder": "当前软件目录/backups（留空使用默认路径）",
        "current_path": "当前路径：{path}",
        "detect_success_title": "检测成功",
        "detect_success_body": "已检测到云盘文件夹：\n{path}",
        "detect_fail_title": "未检测到",
        "detect_fail_body": "未找到 OneDrive / Dropbox / Google Drive / iCloud / 坚果云\n请手动浏览选择同步文件夹",
        "settings_saved_title": "成功",
        "settings_saved_body": "设置已保存！",
        "settings_failed_title": "设置失败",
        "migrate_backups_title": "迁移备份",
        "migrate_backups_prompt": "检测到备份路径变更：\n旧路径：{old}\n新路径：{new}\n\n是否将旧备份迁移到新路径？",
        "migrate_backups_done": "成功迁移 {count} 个条目",
    },
    "en": {
        "product_title": "Steam Save Manager",
        "nav_section": "Navigation",
        "nav_home": "Home",
        "nav_scan": "Scan Games",
        "nav_games": "Games",
        "nav_backup": "Backups",
        "nav_settings": "Settings",
        "status_auto_on": "▶ Scheduled Backup: {minutes} min",
        "status_auto_off": "⏸ Scheduled Backup: Off",
        "home_title": "Welcome to Steam Save Manager",
        "home_subtitle": "Scan, back up, and restore your game saves automatically",
        "stat_games": "Tracked Games",
        "stat_backups": "Backups",
        "stat_auto": "Scheduled Backup",
        "stat_watch": "File Watch",
        "value_games": "{count} games",
        "value_backups": "{count} items",
        "switch_on": "On",
        "switch_off": "Off",
        "home_backup_all": "🔄  Back Up All",
        "home_sync_all": "🔄  Sync All Now",
        "home_scan": "🔍  Scan and Add Games",
        "home_recent": "  📋 Recent Backups",
        "home_no_backups": "No backups yet. Try scanning your games →",
        "scan_title": "🔍 Scan Steam Libraries",
        "scan_start": "Start Scan",
        "scan_add_all": "Add All",
        "scan_hint": "Click “Start Scan” to detect installed Steam games automatically",
        "scan_missing_steam": "⚠️ No Steam library path was detected.\nPlease configure your Steam install path in Settings.",
        "backup_title": "💾 Backup History",
        "backup_all_games": "All Games",
        "backup_empty": "No backups yet",
        "settings_title": "🔧 Settings",
        "settings_subtitle": "Tune language, sync, and backup behavior for this PC. Changes apply immediately.",
        "section_general": "General",
        "section_general_sub": "Language, theme, and Steam path",
        "section_automation": "Automation & Sync",
        "section_automation_sub": "Scheduled backup, file watch, and cloud sync",
        "section_system": "System & Storage",
        "section_system_sub": "Notifications, tray behavior, startup, and retention",
        "steam_path": "Steam Install Path",
        "steam_account": "Steam Account",
        "steamdb_detection": "SteamDB Save Detection",
        "steamdb_detection_desc": "When enabled, the app prioritizes SteamDB for save detection. If no result is found, it falls back to normal local detection.",
        "browse": "Browse",
        "language": "Interface Language",
        "language_hint": "Defaults to your current system language, but you can override it here.",
        "theme": "Theme",
        "theme_light": "Light",
        "theme_dark": "Dark",
        "auto_backup": "Scheduled Backup",
        "enable": "Enable",
        "interval_minutes": "Interval (min)",
        "file_watch": "File Change Monitor",
        "cooldown_seconds": "Cooldown (sec)",
        "watchdog_missing": "⚠ watchdog is not installed. Run: pip install watchdog",
        "auto_sync": "Automatic Save Sync",
        "sync_folder": "Sync Folder",
        "sync_folder_placeholder": "Choose a cloud folder (OneDrive / Dropbox / Google Drive)",
        "auto_detect": "Auto Detect",
        "sync_mode": "Sync Mode",
        "sync_mode_smart": "Smart Cloud Save",
        "sync_mode_bidirectional": "Bidirectional",
        "sync_mode_upload": "Upload Only",
        "sync_mode_download": "Download Only",
        "sync_hint": "Smart Cloud Save (recommended): download and extract the latest cloud ZIP when a game starts, then package local saves into a ZIP archive after it closes.\nBidirectional mode uses the last sync snapshot to detect one-sided changes; if both sides changed, you'll get a conflict dialog and failed tasks will retry automatically.",
        "sync_archive_keep": "Cloud Sync Archive Retention",
        "sync_archive_keep_suffix": "latest ZIP archives (0 = unlimited)",
        "sync_notify": "Send a Windows desktop notification after sync completes",
        "webdav_enable": "WebDAV Remote Sync",
        "webdav_preset": "NAS / WebDAV Preset",
        "webdav_preset_generic": "Generic WebDAV",
        "webdav_preset_synology": "Synology",
        "webdav_preset_qnap": "QNAP",
        "webdav_preset_truenas": "TrueNAS",
        "webdav_preset_nextcloud": "Nextcloud",
        "webdav_preset_openmediavault": "OpenMediaVault",
        "webdav_preset_hint": "Typical endpoint: {hint}",
        "webdav_url": "Server URL",
        "webdav_url_ph": "https://your-server.com/dav",
        "webdav_base_path": "Remote Folder",
        "webdav_base_path_ph": "/SteamSaveSync",
        "webdav_username": "Username",
        "webdav_password": "Password",
        "webdav_verify_ssl": "Certificate Verification",
        "webdav_verify_ssl_desc": "Enable HTTPS certificate verification; disable this for self-signed NAS certificates",
        "webdav_test": "Test Connection",
        "webdav_testing": "Testing connection...",
        "webdav_test_ok": "Connection successful!",
        "webdav_test_fail": "Connection failed",
        "webdav_missing": "WebDAV component unavailable. Run: pip install webdavclient3",
        "minimize_tray": "Close to System Tray",
        "minimize_tray_desc": "When closing the window, keep the app running in the tray",
        "tray_missing": "⚠ pystray is not installed. Run: pip install pystray",
        "autostart": "Launch at Startup",
        "autostart_desc": "Start automatically when you sign in to Windows",
        "autostart_minimize": "Minimize on Startup Launch",
        "autostart_minimize_desc": "Start minimized to the tray or taskbar when launched at sign-in",
        "backup_rotation": "Backup Retention",
        "max_backups": "Keep at most",
        "max_backups_suffix": "backups per game (0 = unlimited)",
        "max_backup_size": "Maximum total backup size",
        "max_backup_size_suffix": "GB (0 = unlimited, oldest backups are removed first)",
        "display_page_size": "List Page Size",
        "games_page_size": "Games per page",
        "games_page_size_suffix": "games (5-200)",
        "backup_page_size": "Backup records per page",
        "backup_page_size_suffix": "records (5-200)",
        "backup_storage": "Backup Storage Path",
        "backup_storage_placeholder": "Uses the app folder /backups when left empty",
        "current_path": "Current path: {path}",
        "detect_success_title": "Detected",
        "detect_success_body": "Cloud sync folder detected:\n{path}",
        "detect_fail_title": "Not Detected",
        "detect_fail_body": "No OneDrive / Dropbox / Google Drive / iCloud / JG Cloud folder was found.\nPlease browse and choose a sync folder manually.",
        "settings_saved_title": "Saved",
        "settings_saved_body": "Settings have been saved.",
        "settings_failed_title": "Settings Error",
        "migrate_backups_title": "Migrate Backups",
        "migrate_backups_prompt": "The backup path changed:\nOld: {old}\nNew: {new}\n\nDo you want to move existing backups to the new path?",
        "migrate_backups_done": "{count} items were migrated successfully.",
    },
}


def normalize_language(code: str) -> str:
    code = (code or "").strip().lower()
    if code.startswith("zh"):
        return "zh-CN"
    if code.startswith("en"):
        return "en"
    return "en"


def clamp_int(value, default: int, min_value: int, max_value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(min_value, min(max_value, parsed))


def detect_system_language() -> str:
    if sys.platform == "win32":
        try:
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            code = locale.windows_locale.get(lang_id, "")
            return normalize_language(code)
        except Exception:
            pass
    try:
        code = locale.getlocale()[0]
        if code:
            return normalize_language(str(code))
    except Exception:
        pass
    try:
        code = locale.getdefaultlocale()[0] if hasattr(locale, "getdefaultlocale") else ""
        if code:
            return normalize_language(str(code))
    except Exception:
        pass
    return normalize_language(os.environ.get("LANG", "")) or "en"


def translate(lang: str, key: str, **kwargs) -> str:
    lang = normalize_language(lang)
    table = TRANSLATIONS.get(lang, TRANSLATIONS["en"])
    text = table.get(key, TRANSLATIONS["en"].get(key, key))
    return text.format(**kwargs) if kwargs else text


def cfg_language(cfg: Optional[dict]) -> str:
    if cfg:
        return normalize_language(cfg.get("language") or detect_system_language())
    return detect_system_language()


def bilingual_text(lang: str, zh: str, en: str) -> str:
    return zh if normalize_language(lang) == "zh-CN" else en


def bilingual_cfg(cfg: Optional[dict], zh: str, en: str) -> str:
    return bilingual_text(cfg_language(cfg), zh, en)


def localize_backup_note(note: str, lang: str = "") -> str:
    note = (note or "").strip()
    if not note:
        return ""
    target_lang = normalize_language(lang)
    system_note_pairs = [
        ("定时自动备份", "Scheduled Backup"),
        ("文件变动自动备份", "File change auto backup"),
        ("游戏退出后自动备份", "Auto backup after game exit"),
        ("同步前自动备份", "Auto backup before sync"),
        ("自动同步成功", "Auto sync completed"),
        ("同步成功", "Sync completed"),
    ]
    for zh, en in system_note_pairs:
        if note == zh or note == en:
            return bilingual_text(target_lang, zh, en)
    return note


def translate_cfg(cfg: Optional[dict], key: str, **kwargs) -> str:
    return translate(cfg_language(cfg), key, **kwargs)


def version_key(version: str) -> tuple[int, ...]:
    parts = [int(x) for x in re.findall(r"\d+", version or "")]
    return tuple(parts) if parts else (0,)


def is_remote_version_newer(remote_version: str, local_version: str = VERSION) -> bool:
    remote = list(version_key(remote_version))
    local = list(version_key(local_version))
    size = max(len(remote), len(local))
    remote.extend([0] * (size - len(remote)))
    local.extend([0] * (size - len(local)))
    return tuple(remote) > tuple(local)


def fetch_update_manifest(url: str = UPDATE_MANIFEST_URL, timeout: int = 10) -> dict:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_NAME}/{VERSION}"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Update manifest must be a JSON object")
    version = str(data.get("version", "")).strip()
    download_url = str(data.get("url", "")).strip()
    notes = str(data.get("notes", "")).strip()
    sha256 = str(data.get("sha256", "")).strip().lower()
    if not version:
        raise ValueError("Missing version in update manifest")
    if not download_url:
        raise ValueError("Missing url in update manifest")
    return {
        "version": version,
        "notes": notes,
        "url": download_url,
        "sha256": sha256,
    }


def download_update_package(manifest: dict, dest_dir: Path, timeout: int = 30) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    download_url = str(manifest.get("url", "")).strip()
    parsed = urllib.parse.urlparse(download_url)
    filename = Path(parsed.path).name or f"{APP_NAME}-{manifest.get('version', 'latest')}.exe"
    safe_name = re.sub(r'[^\w.\-]+', '_', filename)
    target = dest_dir / safe_name
    temp_target = dest_dir / f"{safe_name}.part"

    req = urllib.request.Request(
        download_url,
        headers={"User-Agent": f"{APP_NAME}/{VERSION}"}
    )
    hasher = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=timeout) as resp, open(temp_target, "wb") as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)
            hasher.update(chunk)

    expected = str(manifest.get("sha256", "")).strip().lower()
    if expected and hasher.hexdigest().lower() != expected:
        try:
            temp_target.unlink()
        except OSError:
            pass
        raise ValueError("Downloaded update failed SHA256 verification")

    if target.exists():
        try:
            target.unlink()
        except OSError:
            pass
    temp_target.replace(target)
    if sys.platform == "win32" and target.suffix.lower() == ".exe":
        try:
            with open(target, "rb") as fh:
                if fh.read(2) != b"MZ":
                    target.unlink(missing_ok=True)
                    raise ValueError("Downloaded update is not a valid Windows executable")
        except ValueError:
            raise
        except Exception:
            pass
    return target


def get_update_download_dir() -> Path:
    preferred = APP_DIR / "updates"
    if _can_write_dir(preferred):
        return preferred
    fallback = CONFIG_DIR / "updates"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _current_executable_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return Path(os.path.abspath(sys.argv[0])).resolve()


def _ps_quote(value: Path | str) -> str:
    return str(value).replace("'", "''")


def _validate_windows_update_replacement_paths(downloaded_path: Path, current_path: Path):
    src = Path(downloaded_path).resolve(strict=False)
    dst = Path(current_path).resolve(strict=False)
    update_root = get_update_download_dir().resolve(strict=False)
    app_root = APP_DIR.resolve(strict=False)
    if src == dst:
        raise ValueError("Update source and target must be different files")
    if src.suffix.lower() != ".exe" or dst.suffix.lower() != ".exe":
        raise ValueError("Windows self-update can only replace executable files")
    if not src.exists():
        raise FileNotFoundError(f"Downloaded update does not exist: {src}")
    if not _path_is_within(src, update_root):
        raise ValueError(f"Downloaded update is outside the update directory: {src}")
    if not _path_is_within(dst, app_root):
        raise ValueError(f"Current executable is outside the application directory: {dst}")


def _build_windows_replace_and_launch_command(downloaded_path: Path, current_path: Path, *,
                                              current_pid: int = 0,
                                              backup_old: bool = True) -> str:
    _validate_windows_update_replacement_paths(downloaded_path, current_path)
    src = str(downloaded_path).replace("'", "''")
    dst = str(current_path).replace("'", "''")
    bak = str(current_path.with_suffix(current_path.suffix + ".old")).replace("'", "''")
    log = _ps_quote(Path(downloaded_path).with_suffix(Path(downloaded_path).suffix + ".update-error.log"))
    backup_block = (
        "if (Test-Path -LiteralPath $dst) { "
        "if (Test-Path -LiteralPath $bak) { Remove-Item -LiteralPath $bak -Force } "
        "Move-Item -LiteralPath $dst -Destination $bak -Force } "
        if backup_old else
        "if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force } "
    )
    return (
        "$ErrorActionPreference='Stop'; "
        f"$src='{src}'; $dst='{dst}'; $bak='{bak}'; $log='{log}'; $pidToWait={int(current_pid or 0)}; "
        "$deadline=(Get-Date).AddSeconds(60); "
        "if ($pidToWait -gt 0) { try { Wait-Process -Id $pidToWait -Timeout 45 -ErrorAction SilentlyContinue } catch {} } "
        "$lastError=''; "
        "while ((Get-Date) -lt $deadline) { "
        "try { "
        f"{backup_block}"
        "Move-Item -LiteralPath $src -Destination $dst -Force; "
        "Start-Process -FilePath $dst; exit 0 "
        "} catch { $lastError=$_.Exception.Message; Start-Sleep -Milliseconds 700 } "
        "} "
        "try { ('Update replacement failed: ' + $lastError) | Out-File -LiteralPath $log -Encoding UTF8 } catch {} "
        "if (Test-Path -LiteralPath $src) { Start-Process -FilePath $src }"
    )

# 系统环境路径
APPDATA = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
LOCAL_LOW = Path.home() / "AppData" / "LocalLow"
DOCUMENTS = Path.home() / "Documents"
SAVED_GAMES = Path.home() / "Saved Games"
USER_HOME = Path.home()
PROGRAMDATA = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
PUBLIC_HOME = Path(os.environ.get("PUBLIC", str(USER_HOME.parent / "Public")))
PUBLIC_DOCUMENTS = PUBLIC_HOME / "Documents"
LOCAL_PACKAGES = LOCAL_APPDATA / "Packages"


# ══════════════════════════════════════════════
#  内置热门游戏存档路径数据库（AppID → 路径列表）
#  路径支持变量: {APPDATA}, {LOCAL}, {LOCALLOW},
#               {DOCS}, {SAVED}, {HOME}, {INSTALL}
# ══════════════════════════════════════════════

KNOWN_SAVE_PATHS: dict[str, list[str]] = {
    # --- 魂系列 / FromSoftware ---
    "1245620": ["{APPDATA}/EldenRing"],                              # Elden Ring
    "374320":  ["{APPDATA}/DarkSoulsIII"],                            # Dark Souls III
    "236430":  ["{APPDATA}/DarkSoulsII"],                             # Dark Souls II
    "570940":  ["{APPDATA}/DarkSouls"],                               # Dark Souls Remastered
    "814380":  ["{APPDATA}/Sekiro"],                                  # Sekiro
    "2358720": ["{APPDATA}/ArmoredCore6"],                            # Armored Core VI
    # --- 开放世界 / RPG ---
    "1091500": ["{SAVED}/CD Projekt Red/Cyberpunk 2077"],             # Cyberpunk 2077
    "292030":  ["{DOCS}/The Witcher 3"],                              # The Witcher 3
    "489830":  ["{LOCAL}/Larian Studios/Baldur's Gate 3/PlayerProfiles",
                "{LOCAL}/Larian Studios/Baldur's Gate 3"],                  # BG3
    "1174180": ["{SAVED}/Red Dead Redemption 2/Profiles"],            # RDR2
    "1593500": ["{LOCAL}/Nimble Giant/GTA V/Profiles"],               # GTA V (single)
    "271590":  ["{DOCS}/Rockstar Games/GTA V/Profiles"],              # GTA V
    "377160":  ["{DOCS}/My Games/Fallout4/Saves"],                    # Fallout 4
    "611670":  ["{DOCS}/My Games/Starfield/Saves"],                   # Starfield
    "22380":   ["{DOCS}/My Games/FalloutNV/Saves"],                   # Fallout NV
    "22330":   ["{DOCS}/My Games/Oblivion/Saves"],                    # Oblivion
    "72850":   ["{DOCS}/My Games/Skyrim/Saves",
                "{DOCS}/My Games/Skyrim Special Edition/Saves"],       # Skyrim
    "1716740": ["{LOCAL}/Elden Ring Nightreign"],                     # Nightreign
    # --- 独立游戏 ---
    "413150":  ["{APPDATA}/StardewValley/Saves"],                     # Stardew Valley
    "367520":  ["{LOCALLOW}/Team Cherry/Hollow Knight"],              # Hollow Knight
    "1030300": ["{APPDATA}/../LocalLow/Team Cherry/Hollow Knight Silksong"], # Silksong
    "105600":  ["{DOCS}/My Games/Terraria",
                "{DOCS}/My Games/Terraria/Players"],                   # Terraria
    "1145360": ["{APPDATA}/Hades II"],                                # Hades II
    "1100600": ["{APPDATA}/SupergiantGames/Hades"],                   # Hades
    "251570":  ["{HOME}/.klei/DoNotStarve"],                          # Don't Starve
    "322330":  ["{HOME}/.klei/DoNotStarveTogether"],                  # Don't Starve Together
    "460950":  ["{APPDATA}/Katana ZERO"],                             # Katana ZERO
    "311690":  ["{INSTALL}/saves"],                                   # Enter the Gungeon
    "257850":  ["{SAVED}/Hyper Light Drifter"],                       # Hyper Light Drifter
    "391540":  ["{APPDATA}/Undertale"],                               # Undertale
    "1382330": ["{LOCAL}/UNDERTALE_YELLOW_DATA"],                     # Undertale Yellow
    "2717750": ["{APPDATA}/DELTARUNE"],                               # Deltarune
    "524220":  ["{APPDATA}/Raft"],                                    # NieR:Automata
    "1113560": ["{DOCS}/NieR Replicant"],                             # NieR Replicant
    "1172470": ["{LOCAL}/ApexLegends/Saved/SaveGames"],               # Apex Legends
    "548430":  ["{LOCALLOW}/DeepSilver/DeadIsland2"],                 # Deep Rock Galactic
    "526870":  ["{LOCAL}/Satisfactory/Saved/SaveGames"],              # Satisfactory
    "585420":  ["{LOCAL}/Celeste/Saves"],                             # Celeste
    "632360":  ["{APPDATA}/RiskOfRain2"],                             # Risk of Rain 2
    "427520":  ["{INSTALL}/Factorio/saves"],                          # Factorio
    "1966720": ["{LOCAL}/Lethal Company/saves"],                      # Lethal Company
    "892970":  ["{LOCAL}/Valheim/worlds_local",
                "{APPDATA}/../LocalLow/IronGate/Valheim"],             # Valheim
    "1623730": ["{LOCAL}/PalServer/Saved/SaveGames",
                "{LOCAL}/Pal/Saved/SaveGames"],                        # Palworld
    # --- 生存 / 建造 ---
    "275850":  ["{INSTALL}/ShooterGame/Saved"],                       # ARK
    "346110":  ["{INSTALL}/saves"],                                   # ARK: SE
    "252490":  ["{INSTALL}/server/RustDedicated_Data"],               # Rust
    "108600":  ["{APPDATA}/7DaysToDie/Saves"],                        # 7 Days to Die
    "304930":  ["{INSTALL}/Saves"],                                   # Unturned
    # --- 策略 ---
    "289070":  ["{DOCS}/Paradox Interactive/Civilization VI/Saves",
                "{DOCS}/My Games/Sid Meier's Civilization VI/Saves"],  # Civ VI
    "8930":    ["{DOCS}/Paradox Interactive/Civilization V/Saves",
                "{DOCS}/My Games/Sid Meier's Civilization V/Saves"],   # Civ V
    "236850":  ["{DOCS}/Paradox Interactive/Europa Universalis IV"],   # EU4
    "281990":  ["{DOCS}/Paradox Interactive/Stellaris"],               # Stellaris
    "394360":  ["{DOCS}/Paradox Interactive/Hearts of Iron IV"],       # HOI4
    "203770":  ["{DOCS}/Paradox Interactive/Crusader Kings II"],       # CK2
    "1158310": ["{DOCS}/Paradox Interactive/Crusader Kings III"],      # CK3
    "362960":  ["{DOCS}/Klei/OxygenNotIncluded/save_files"],          # ONI
    "294100":  ["{LOCAL}/RimWorld by Ludeon Studios/Saves"],           # RimWorld
    "1030830": ["{DOCS}/Paradox Interactive/Victoria 3"],              # Victoria 3
    # --- 动作 / 冒险 ---
    "1446780": ["{APPDATA}/MonsterHunterRise"],                       # MH Rise
    "582010":  ["{INSTALL}/remote"],                                  # MH World
    "1888160": ["{APPDATA}/capcom/MHWILDS"],                          # MH Wilds
    "230410":  ["{LOCAL}/Warframe"],                                  # Warframe
    "812140":  ["{APPDATA}/Capcom/RESIDENT EVIL 2"],                  # RE2
    "883710":  ["{APPDATA}/Capcom/RESIDENT EVIL 3"],                  # RE3
    "1196590": ["{APPDATA}/Capcom/RESIDENT EVIL VILLAGE"],            # RE Village
    "2050650": ["{APPDATA}/Capcom/RESIDENT EVIL 4"],                  # RE4 Remake
    "601150":  ["{APPDATA}/Capcom/DEVIL MAY CRY 5"],                  # DMC5
    "1817070": ["{APPDATA}/miHoYo/Genshin Impact"],                   # 原神 (非 Steam 但很多人搜)
    "1172380": ["{APPDATA}/SpaceWar",
                "{LOCAL}/STAR WARS Jedi Fallen Order"],                 # Jedi: Fallen Order
    "620":     ["{INSTALL}/portal2/save"],                            # Portal 2
    "400":     ["{INSTALL}/portal/save"],                             # Portal
    "220":     ["{INSTALL}/hl2/save"],                                # Half-Life 2
    "546560":  ["{INSTALL}/game/save"],                               # Half-Life: Alyx
    # --- 恐怖 ---
    "268050":  ["{LOCALLOW}/KinematicGames/PhaseTwo"],                # Phasmophobia
    "381210":  ["{APPDATA}/DeadByDaylight/Saved/SaveGames"],          # Dead by Daylight
    # --- 体育 / 竞速 ---
    "1811260": ["{DOCS}/EA SPORTS FC 24"],                            # EA FC 24
    "1547000": ["{DOCS}/EA SPORTS FC 25"],                            # EA FC 25
    "1551360": ["{DOCS}/Forza Horizon 5/savegames"],                  # Forza Horizon 5
    # --- 沙盒 ---
    "431960":  ["{INSTALL}/saves"],                                   # Wallpaper Engine (saves)
    "1426210": ["{APPDATA}/../Roaming/Godot/app_userdata/It Takes Two"], # It Takes Two
}


# ══════════════════════════════════════════════
#  VDF 简易解析器（解析 Steam 的 libraryfolders.vdf）
# ══════════════════════════════════════════════

def parse_vdf(text: str) -> dict:
    """极简 VDF → dict 解析（处理 Valve 配置文件格式）"""
    result = {}
    stack = [result]
    # 使用 finditer 正确区分引号内容和大括号
    tokens = []
    for m in re.finditer(r'"([^"]*)"|([{}])', text):
        if m.group(1) is not None:
            tokens.append(m.group(1))       # 引号内的字符串
        else:
            tokens.append(m.group(2))       # { 或 }
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "{":
            pass  # 已在上一步处理
        elif token == "}":
            if len(stack) > 1:
                stack.pop()
        elif i + 1 < len(tokens):
            next_token = tokens[i + 1]
            if next_token == "{":
                child = {}
                stack[-1][token] = child
                stack.append(child)
                i += 1  # 跳过 {
            else:
                stack[-1][token] = next_token
                i += 1  # 跳过值
        i += 1
    return result


# ══════════════════════════════════════════════
#  Steam 游戏库扫描
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
#  开机自启（Windows 注册表 / macOS launchd / Linux autostart）
# ══════════════════════════════════════════════

AUTOSTART_REG_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_REG_NAME = "SteamSaveManager"
AUTOSTART_STARTUP_CMD = "SteamSaveManager.cmd"


def _autostart_script_path() -> str:
    return str(_current_executable_path())


def _windows_startup_cmd_path() -> Path:
    return APPDATA / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / AUTOSTART_STARTUP_CMD


def _quoted_command_paths_exist(command: str) -> bool:
    paths = re.findall(r'"([^"]+)"', str(command or ""))
    if not paths:
        return False
    return all(Path(path).exists() for path in paths)


def _write_windows_startup_cmd(command: str):
    cmd_path = _windows_startup_cmd_path()
    cmd_path.parent.mkdir(parents=True, exist_ok=True)
    cmd_path.write_text(f'@echo off\r\nchcp 65001 >nul\r\nstart "" {command}\r\n', encoding="utf-8")


def _remove_windows_startup_cmd():
    _windows_startup_cmd_path().unlink(missing_ok=True)


def get_autostart_enabled() -> bool:
    """检查当前是否已设置开机自启"""
    if sys.platform == "win32" and winreg:
        key = None
        registry_enabled = False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY,
                                 0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, AUTOSTART_REG_NAME)
            registry_enabled = bool(val) and _quoted_command_paths_exist(val)
        except FileNotFoundError:
            registry_enabled = False
        except Exception as e:
            logger.warning("检查自启状态失败: %s", e)
            registry_enabled = False
        finally:
            if key is not None:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
        return registry_enabled or _windows_startup_cmd_path().exists()
    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.steamsavemanager.plist"
        return plist.exists()
    else:
        desktop = Path.home() / ".config" / "autostart" / "steam-save-manager.desktop"
        return desktop.exists()


def set_autostart_enabled(enable: bool):
    """设置或取消开机自启"""
    script_path = _autostart_script_path()
    python_path = sys.executable

    if sys.platform == "win32" and winreg:
        errors = []
        cmd = _build_autostart_command(script_path, python_path)
        key = None
        try:
            key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, AUTOSTART_REG_KEY,
                                     0, winreg.KEY_SET_VALUE)
            if enable:
                winreg.SetValueEx(key, AUTOSTART_REG_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, AUTOSTART_REG_NAME)
                except FileNotFoundError:
                    pass
        except Exception as e:
            errors.append(f"registry: {e}")
        finally:
            if key is not None:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
        try:
            if enable:
                _write_windows_startup_cmd(cmd)
            else:
                _remove_windows_startup_cmd()
        except Exception as e:
            errors.append(f"startup folder: {e}")
        if enable and errors and not get_autostart_enabled():
            raise RuntimeError(f"设置自启失败：{'; '.join(errors)}")
        if not enable and errors and get_autostart_enabled():
            raise RuntimeError(f"取消自启失败：{'; '.join(errors)}")

    elif sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / "com.steamsavemanager.plist"
        if enable:
            plist.parent.mkdir(parents=True, exist_ok=True)
            content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.steamsavemanager</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
        <string>{STARTUP_AUTOSTART_FLAG}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
            plist.write_text(content, encoding="utf-8")
        else:
            if plist.exists():
                plist.unlink()

    else:  # Linux
        desktop = Path.home() / ".config" / "autostart" / "steam-save-manager.desktop"
        if enable:
            desktop.parent.mkdir(parents=True, exist_ok=True)
            content = f"""[Desktop Entry]
Type=Application
Name=Steam Save Manager
Exec={python_path} {script_path} {STARTUP_AUTOSTART_FLAG}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
"""
            desktop.write_text(content, encoding="utf-8")
        else:
            if desktop.exists():
                desktop.unlink()


def detect_steam_path() -> str:
    """从注册表或常见路径检测 Steam 主安装位置"""
    if winreg:
        for reg_path in [
            r"SOFTWARE\WOW6432Node\Valve\Steam",
            r"SOFTWARE\Valve\Steam",
        ]:
            key = None
            try:
                key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, reg_path)
                path, _ = winreg.QueryValueEx(key, "InstallPath")
                if os.path.isdir(path):
                    return path
            except Exception as e:
                logger.warning("读取注册表 %s 失败: %s", reg_path, e)
            finally:
                if key is not None:
                    try:
                        winreg.CloseKey(key)
                    except Exception:
                        pass
        # 也尝试 HKEY_CURRENT_USER
        key = None
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                 r"SOFTWARE\Valve\Steam")
            path, _ = winreg.QueryValueEx(key, "SteamPath")
            if path and os.path.isdir(path):
                return os.path.normpath(path)
        except Exception as e:
            logger.warning("读取注册表 HKEY_CURRENT_USER 失败: %s", e)
        finally:
            if key is not None:
                try:
                    winreg.CloseKey(key)
                except Exception:
                    pass
    for d in [r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam",
              r"D:\Steam", r"E:\Steam",
              str(Path.home() / ".steam" / "steam"),
              str(Path.home() / ".local" / "share" / "Steam")]:
        if os.path.isdir(d):
            return d
    return ""


_REGISTRY_INSTALL_CACHE: dict[str, list[str]] = {}
_REGISTRY_INSTALL_CACHE_LOCK = threading.Lock()


def _detect_install_paths_from_registry(game_name: str, appid: str = "") -> list[str]:
    """
    从 Windows 注册表 Uninstall 键中按游戏名模糊匹配，
    返回匹配到的 InstallLocation 路径列表。
    """
    if not winreg or sys.platform != "win32":
        return []
    cache_key = _normalize_recognition_name(game_name) + "|" + str(appid or "").strip()
    with _REGISTRY_INSTALL_CACHE_LOCK:
        cached = _REGISTRY_INSTALL_CACHE.get(cache_key)
    if cached is not None:
        return list(cached)

    keywords = _extract_search_keywords(game_name)
    name_norm = _normalize_recognition_name(game_name)
    results = []
    seen = set()

    reg_paths = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, base_path in reg_paths:
        try:
            base_key = winreg.OpenKey(hive, base_path, 0, winreg.KEY_READ)
        except Exception:
            continue
        try:
            i = 0
            while True:
                try:
                    sub_name = winreg.EnumKey(base_key, i)
                    i += 1
                except OSError:
                    break
                try:
                    sub_key = winreg.OpenKey(base_key, sub_name, 0, winreg.KEY_READ)
                except Exception:
                    continue
                try:
                    display_name = ""
                    install_loc = ""
                    try:
                        display_name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                    except Exception:
                        pass
                    try:
                        install_loc, _ = winreg.QueryValueEx(sub_key, "InstallLocation")
                    except Exception:
                        pass
                    if not display_name or not install_loc or not os.path.isdir(install_loc):
                        continue
                    display_norm = _normalize_recognition_name(display_name)
                    matched = False
                    if name_norm and (name_norm in display_norm or display_norm in name_norm):
                        matched = True
                    elif len(keywords) >= 2 and sum(
                        1 for kw in keywords[:5]
                        if len(kw) > 2 and kw in display_name.lower()
                    ) >= 2:
                        matched = True
                    if matched:
                        norm = os.path.normpath(install_loc)
                        if norm not in seen:
                            seen.add(norm)
                            results.append(norm)
                finally:
                    winreg.CloseKey(sub_key)
        finally:
            winreg.CloseKey(base_key)

    with _REGISTRY_INSTALL_CACHE_LOCK:
        _REGISTRY_INSTALL_CACHE[cache_key] = results
    return list(results)


def scan_drive_steam_libraries() -> list[str]:
    """
    扫描所有盘符根目录，自动发现 SteamLibrary / Steam 文件夹
    覆盖用户在 D:\\SteamLibrary、E:\\SteamLibrary 等位置的安装
    """
    found = []
    if sys.platform != "win32":
        # Linux/macOS 常见路径
        for p in [
            Path.home() / ".steam" / "steam",
            Path.home() / ".local" / "share" / "Steam",
            Path("/usr/share/steam"),
        ]:
            if p.is_dir():
                found.append(str(p))
        return found

    # Windows: 遍历所有盘符 A-Z
    import string
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        # 常见的 Steam 库文件夹名
        for folder_name in [
            "SteamLibrary",
            "Steam",
            "Steam Library",
            "steam",
            "steamlibrary",
            "Games\\SteamLibrary",
            "Games\\Steam",
        ]:
            candidate = os.path.join(drive, folder_name)
            steamapps = os.path.join(candidate, "steamapps")
            # 只有包含 steamapps 子目录的才是合法的 Steam 库
            if os.path.isdir(steamapps):
                norm = os.path.normpath(candidate)
                if norm not in found:
                    found.append(norm)
    return found


def get_steam_library_folders(steam_path: str) -> list[str]:
    """
    获取所有 Steam 库文件夹路径（三重检测）：
    1. Steam 主安装目录
    2. libraryfolders.vdf 中配置的库
    3. 全盘扫描发现的 SteamLibrary 文件夹
    """
    folders = []
    seen = set()

    def _add(p: str):
        norm = os.path.normpath(p)
        if norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            folders.append(norm)

    # 1) 主安装目录
    if steam_path:
        _add(steam_path)

    # 2) 解析 libraryfolders.vdf
    vdf_path = os.path.join(steam_path, "steamapps", "libraryfolders.vdf")
    if os.path.isfile(vdf_path):
        try:
            with open(vdf_path, "r", encoding="utf-8") as f:
                data = parse_vdf(f.read())
            lf = data.get("libraryfolders", data.get("LibraryFolders", {}))
            for key, val in lf.items():
                if isinstance(val, dict) and "path" in val:
                    _add(val["path"])
                elif isinstance(val, str):
                    _add(val)
        except Exception:
            pass

    # 3) 全盘扫描 SteamLibrary 目录
    for lib in scan_drive_steam_libraries():
        _add(lib)

    return folders


def get_steam_user_ids(steam_path: str) -> list[str]:
    """获取 Steam userdata 下的所有用户 ID"""
    userdata = os.path.join(steam_path, "userdata")
    if not os.path.isdir(userdata):
        return []
    return sorted(
        [
            d for d in os.listdir(userdata)
            if os.path.isdir(os.path.join(userdata, d)) and d.isdigit() and d != "0"
        ],
        key=lambda item: int(item),
    )


def _accountid_from_steam64(steamid64: str) -> str:
    try:
        return str(int(str(steamid64).strip()) - 76561197960265728)
    except Exception:
        return ""


def get_steam_accounts(steam_path: str) -> list[dict]:
    """读取 Steam 多账号列表，并尽量补齐账号昵称。"""
    accounts: dict[str, dict] = {}
    for uid in get_steam_user_ids(steam_path):
        accounts[uid] = {
            "accountid": uid,
            "steamid64": _steam64_from_accountid(uid),
            "persona_name": "",
            "account_name": "",
            "most_recent": False,
        }

    loginusers_path = os.path.join(steam_path, "config", "loginusers.vdf")
    if os.path.isfile(loginusers_path):
        try:
            data = parse_vdf(Path(loginusers_path).read_text(encoding="utf-8", errors="ignore"))
            users = data.get("users") or data.get("Users") or {}
            if isinstance(users, dict):
                for steam64, info in users.items():
                    if not isinstance(info, dict):
                        continue
                    accountid = _accountid_from_steam64(steam64)
                    if not accountid or accountid == "0":
                        continue
                    item = accounts.setdefault(accountid, {
                        "accountid": accountid,
                        "steamid64": str(steam64).strip(),
                        "persona_name": "",
                        "account_name": "",
                        "most_recent": False,
                    })
                    item["steamid64"] = str(steam64).strip() or item.get("steamid64", "")
                    item["persona_name"] = str(
                        info.get("PersonaName") or info.get("personaname") or item.get("persona_name", "")
                    ).strip()
                    item["account_name"] = str(
                        info.get("AccountName") or info.get("accountname") or item.get("account_name", "")
                    ).strip()
                    item["most_recent"] = str(info.get("MostRecent") or info.get("mostrecent") or "0").strip() == "1"
        except Exception:
            pass

    result = list(accounts.values())
    result.sort(key=lambda item: (
        0 if item.get("most_recent") else 1,
        (item.get("persona_name") or item.get("account_name") or item.get("accountid") or "").lower(),
    ))
    return result


def parse_acf(acf_path: str) -> dict:
    """解析 appmanifest_xxx.acf"""
    try:
        with open(acf_path, "r", encoding="utf-8", errors="ignore") as f:
            return parse_vdf(f.read())
    except Exception:
        return {}


# 已知的非游戏系统级 AppID，不应出现在扫描结果中
_NON_GAME_APPIDS = {
    "7",       # Steam Client
    "228980",  # Steamworks Common Redistributables
    "1007",    # Steam Client Windows
    "1070560", # Steam Linux Runtime
    "1391110", # Steam Linux Runtime - Soldier
    "1628350", # Steam Linux Runtime - Sniper
    "250820",  # SteamVR
    "323180",  # SteamVR Workshop Tools
    "744350",  # Steam Chat Images
}


# 被视为"游戏"的 app type（来自 appinfo.vdf common.type 字段）
# 不在此集合中的类型（如 Tool, DLC, Demo, Config, Music）将被过滤
_APPINFO_GAME_TYPES = {"game", "Game"}


def scan_installed_games(steam_path: str) -> list[dict]:
    """
    扫描所有 Steam 库文件夹，返回已安装游戏列表
    每项: {appid, name, install_dir, library_path}
    通过三层过滤排除非游戏应用:
      1. appinfo.vdf 中的 common.type 字段（最可靠）
      2. 已知的非游戏 AppID 黑名单
      3. 名称模式匹配（兜底）
    """
    # 预加载 appinfo 以获取 app type 信息
    if steam_path:
        _load_appinfo_vdf(steam_path)

    games = []
    seen_ids = set()
    for lib_folder in get_steam_library_folders(steam_path):
        steamapps = os.path.join(lib_folder, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        for fname in os.listdir(steamapps):
            if fname.startswith("appmanifest_") and fname.endswith(".acf"):
                acf_path = os.path.join(steamapps, fname)
                data = parse_acf(acf_path)
                app_state = data.get("AppState", {})
                appid = app_state.get("appid", "")
                name = app_state.get("name", "")
                install_dir = app_state.get("installdir", "")
                if not appid or not name or appid in seen_ids:
                    continue
                # 过滤已知的非游戏 AppID
                if appid in _NON_GAME_APPIDS:
                    continue
                # 通过 appinfo.vdf 的 common.type 过滤非游戏应用（最可靠）
                app_type = _APPINFO_CACHE.get_app_type(appid)
                if app_type and app_type not in _APPINFO_GAME_TYPES:
                    continue
                # 通过名称模式过滤常见的非游戏应用（兜底，处理 appinfo 未覆盖的情况）
                name_lower = name.lower()
                if any(pattern in name_lower for pattern in (
                    "redistributable", "redist",
                    "dedicated server", "dedicated_server",
                    "sdk", "runtime",
                    "proton ", "proton-",
                )):
                    continue
                seen_ids.add(appid)
                full_install = os.path.join(steamapps, "common", install_dir)
                games.append({
                    "appid": appid,
                    "name": name,
                    "install_dir": full_install if os.path.isdir(full_install) else "",
                    "library_path": lib_folder,
                })
    games.sort(key=lambda g: g["name"].lower())
    return games


def _get_volume_root(path: str) -> str:
    norm = os.path.abspath(path or "")
    if not norm:
        return ""
    drive, _ = os.path.splitdrive(norm)
    if drive:
        return drive.rstrip("\\/") + "\\"
    if norm.startswith("\\\\"):
        parts = [p for p in norm.split("\\") if p]
        if len(parts) >= 2:
            return f"\\\\{parts[0]}\\{parts[1]}\\"
    return ""


def classify_storage_path(path: str) -> str:
    root = _get_volume_root(path)
    if not root:
        return "unknown"
    with _STORAGE_KIND_CACHE_LOCK:
        cached = _STORAGE_KIND_CACHE.get(root)
        if cached:
            return cached
    if sys.platform != "win32":
        with _STORAGE_KIND_CACHE_LOCK:
            _STORAGE_KIND_CACHE[root] = "unknown"
        return "unknown"
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetDriveTypeW.restype = ctypes.c_uint
        kernel32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        kernel32.CreateFileW.restype = ctypes.c_void_p
        kernel32.DeviceIoControl.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        class STORAGE_PROPERTY_QUERY(ctypes.Structure):
            _fields_ = [
                ("PropertyId", ctypes.c_int),
                ("QueryType", ctypes.c_int),
                ("AdditionalParameters", ctypes.c_byte * 1),
            ]

        class DEVICE_SEEK_PENALTY_DESCRIPTOR(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_uint32),
                ("Size", ctypes.c_uint32),
                ("IncursSeekPenalty", ctypes.c_byte),
            ]

        class DEVICE_TRIM_DESCRIPTOR(ctypes.Structure):
            _fields_ = [
                ("Version", ctypes.c_uint32),
                ("Size", ctypes.c_uint32),
                ("TrimEnabled", ctypes.c_byte),
            ]

        IOCTL_STORAGE_QUERY_PROPERTY = 0x002D1400
        STORAGE_DEVICE_SEEK_PENALTY_PROPERTY = 7
        STORAGE_DEVICE_TRIM_PROPERTY = 8
        PROPERTY_STANDARD_QUERY = 0
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS = 0x00560000

        def _query_storage_property(handle, property_id: int, descriptor):
            query = STORAGE_PROPERTY_QUERY(
                PropertyId=property_id,
                QueryType=PROPERTY_STANDARD_QUERY,
            )
            returned = ctypes.c_uint32()
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_STORAGE_QUERY_PROPERTY,
                ctypes.byref(query),
                ctypes.sizeof(query),
                ctypes.byref(descriptor),
                ctypes.sizeof(descriptor),
                ctypes.byref(returned),
                None,
            )
            return bool(ok)

        def _open_handle(device_path: str):
            return kernel32.CreateFileW(
                device_path,
                GENERIC_READ,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                0,
                None,
            )

        def _query_kind_from_handle(handle) -> str:
            trim_desc = DEVICE_TRIM_DESCRIPTOR()
            seek_desc = DEVICE_SEEK_PENALTY_DESCRIPTOR()
            trim_ok = _query_storage_property(handle, STORAGE_DEVICE_TRIM_PROPERTY, trim_desc)
            seek_ok = _query_storage_property(handle, STORAGE_DEVICE_SEEK_PENALTY_PROPERTY, seek_desc)
            if trim_ok and bool(trim_desc.TrimEnabled):
                return "ssd"
            if seek_ok:
                return "hdd" if bool(seek_desc.IncursSeekPenalty) else "ssd"
            if trim_ok:
                return "ssd" if bool(trim_desc.TrimEnabled) else "fixed"
            return "fixed"

        def _get_disk_number_from_volume(handle):
            buf = ctypes.create_string_buffer(256)
            returned = ctypes.c_uint32()
            ok = kernel32.DeviceIoControl(
                handle,
                IOCTL_VOLUME_GET_VOLUME_DISK_EXTENTS,
                None,
                0,
                buf,
                ctypes.sizeof(buf),
                ctypes.byref(returned),
                None,
            )
            if not ok or returned.value < 4:
                return None
            number_of_extents = struct.unpack_from("<I", buf.raw, 0)[0]
            if number_of_extents < 1 or returned.value < 8:
                return None
            return struct.unpack_from("<I", buf.raw, 4)[0]

        drive_type = kernel32.GetDriveTypeW(root)
        if drive_type == 4:
            kind = "network"
        elif drive_type == 2:
            kind = "removable"
        elif drive_type != 3:
            kind = "unknown"
        else:
            volume_name = root.rstrip("\\/")
            device_path = f"\\\\.\\{volume_name}"
            volume_handle = _open_handle(device_path)
            if volume_handle == INVALID_HANDLE_VALUE:
                kind = "fixed"
            else:
                try:
                    kind = "fixed"
                    disk_number = _get_disk_number_from_volume(volume_handle)
                    if disk_number is not None:
                        physical_handle = _open_handle(f"\\\\.\\PhysicalDrive{disk_number}")
                        if physical_handle != INVALID_HANDLE_VALUE:
                            try:
                                kind = _query_kind_from_handle(physical_handle)
                            finally:
                                kernel32.CloseHandle(physical_handle)
                    if kind == "fixed":
                        kind = _query_kind_from_handle(volume_handle)
                finally:
                    kernel32.CloseHandle(volume_handle)
    except Exception as e:
        logger.warning("分类存储路径失败 %s: %s", path, e)
        kind = "unknown"
    with _STORAGE_KIND_CACHE_LOCK:
        _STORAGE_KIND_CACHE[root] = kind
    return kind


def recommend_scan_workers(paths: list[str], task_count: int) -> tuple[int, str]:
    task_count = max(1, int(task_count or 1))
    cpu = max(2, os.cpu_count() or 4)
    roots = []
    seen = set()
    for path in paths:
        root = _get_volume_root(path)
        if root and root not in seen:
            seen.add(root)
            roots.append(root)

    kinds = [classify_storage_path(root) for root in roots] or ["unknown"]
    if any(k == "network" for k in kinds):
        cap = 2
        profile = "network"
    elif any(k == "removable" for k in kinds):
        cap = 2
        profile = "removable"
    elif all(k == "ssd" for k in kinds):
        cap = min(12, max(4, cpu))
        profile = "ssd"
    elif all(k == "hdd" for k in kinds):
        cap = min(4, max(2, cpu // 3))
        profile = "hdd"
    elif all(k in {"fixed", "unknown"} for k in kinds):
        cap = min(8, max(3, cpu // 2))
        profile = "fixed"
    elif "hdd" in kinds:
        cap = min(6, max(3, cpu // 2))
        profile = "mixed"
    else:
        cap = min(8, max(3, cpu // 2))
        profile = "mixed" if len(set(kinds)) > 1 else kinds[0]

    return max(1, min(task_count, cap)), profile


# ══════════════════════════════════════════════
#  通用存档路径探测（核心）
# ══════════════════════════════════════════════

SAVE_DIR_PATTERNS = [
    "save", "saves", "savegame", "savegames", "savedata",
    "save_data", "savefile", "savefiles", "gamesave", "gamesaves",
    "sav", "slot*", "profile*", "userdata", "user_data",
    "playerdata", "progress",
]

SAVE_FILE_HINTS = (
    "save", "autosave", "quicksave", "slot", "profile", "player",
    "world", "checkpoint", "progress", "career", "campaign", "story",
    "game", "backup", "manual"
)
STRONG_SAVE_FILE_HINTS = ("save", "autosave", "quicksave", "slot", "profile")
WEAK_SAVE_FILE_HINTS = tuple(h for h in SAVE_FILE_HINTS if h not in STRONG_SAVE_FILE_HINTS)
SAVE_FILE_EXTENSIONS = {
    ".sav", ".save", ".dat", ".bin", ".json", ".xml", ".db", ".sqlite",
    ".slot", ".profile", ".bak", ".savemeta", ".bson",
    ".sav2", ".bak2", ".rpgsave",          # RPG Maker / 多版本备份
    ".ess", ".skse",                        # Creation Engine (Skyrim/Fallout)
    ".wld", ".plr",                         # Terraria
    ".world", ".player", ".chunked",        # 沙盒类 / UE 打包存档
}
STRONG_SAVE_FILE_EXTENSIONS = {
    ".sav", ".save", ".slot", ".profile", ".savemeta",
    ".sav2", ".bak2", ".rpgsave", ".ess", ".skse",
    ".wld", ".plr", ".world", ".player", ".chunked",
}
WEAK_SAVE_FILE_EXTENSIONS = SAVE_FILE_EXTENSIONS - STRONG_SAVE_FILE_EXTENSIONS
NEGATIVE_FILE_HINTS = {
    "log", "logs", "cache", "shader", "screenshot", "crash", "dump",
    "telemetry", "analytics", "temp", "tmp"
}
SAVE_PAYLOAD_IGNORED_SUFFIXES = {
    ".log", ".tmp", ".temp",
}
SAVE_PAYLOAD_IGNORED_NAMES = {
    ".ds_store", "desktop.ini", "thumbs.db",
}
ENGINE_SAVE_DIR_SEQUENCES = [
    ("saved", "savegames"),
    ("saved", "savedgames"),
    ("saved", "profiles"),
    ("saved", "userdata"),
    ("saved", "config"),
    ("userdata",),
    ("profiles",),
    ("profile",),
    ("saves",),
    ("savegames",),
    ("savedata",),
    ("save",),
    ("_data", "saves"),
    ("www", "save"),
    ("data", "save"),
    # Godot
    ("user_data",),
    ("app_userdata",),
    # GameMaker
    ("local_data",),
    ("localdata",),
    # CryEngine
    ("user", "profiles"),
    ("user", "savegames"),
]

COMMON_SAVE_BASES = [
    APPDATA,
    LOCAL_APPDATA,
    LOCAL_LOW,
    LOCAL_PACKAGES,
    DOCUMENTS,
    DOCUMENTS / "My Games",
    DOCUMENTS / "Saved Games",
    SAVED_GAMES,
    PROGRAMDATA,
    PUBLIC_DOCUMENTS,
]
_STEAM_AUTOCLOUD_CACHE: Optional[list[dict]] = None
_STEAMDB_UFS_LOCK = threading.Lock()
_STEAMDB_UFS_SEMAPHORE = threading.Semaphore(2)
_STORAGE_KIND_CACHE: dict[str, str] = {}
_STORAGE_KIND_CACHE_LOCK = threading.Lock()
_SAVE_DETECTION_CACHE: dict[str, list[dict]] = {}
_SAVE_DETECTION_CACHE_LOCK = threading.Lock()
_STEAMDB_UFS_CACHE: dict[str, list[str]] = {}
_STEAMDB_UFS_ENTRY_CACHE: dict[str, list[dict]] = {}
_INSTALLED_GAME_INFO_CACHE: dict[str, dict[str, dict]] = {}
_INSTALLED_GAME_INFO_CACHE_LOCK = threading.Lock()


class _AppInfoCache:
    """封装 appinfo.vdf 的解析与缓存，替代分散的 _APPINFO_* 全局状态。

    - ``_data`` / ``_type_map``：整份 appinfo.vdf 的单次快照，按 steam_path 失效重载。
    - ``_ufs_cache``：按 appid 派生的 UFS 模板，使用 LRU 控制内存上限。
    所有公开方法均在内部锁保护下访问，线程安全。
    """

    UFS_CACHE_MAX = 512

    def __init__(self):
        self._lock = threading.RLock()
        self._loaded = False
        self._loaded_path = ""
        self._data: dict[str, list[dict]] = {}          # appid -> savefiles 规格
        self._type_map: dict[str, str] = {}             # appid -> app type
        self._ufs_cache: "OrderedDict[str, list[dict]]" = OrderedDict()

    def ensure_loaded(self, steam_path: str) -> None:
        """确保指定 Steam 路径的 appinfo.vdf 已解析到缓存（按路径幂等）。"""
        normalized = os.path.normpath(steam_path) if steam_path else ""
        with self._lock:
            if self._loaded and normalized == self._loaded_path:
                return
            self._data = {}
            self._type_map = {}
            self._ufs_cache.clear()
            self._loaded = True
            self._loaded_path = normalized
            self._parse(steam_path)

    def reset(self) -> None:
        """清空全部缓存并标记为未加载，下次访问时重新解析。"""
        with self._lock:
            self._loaded = False
            self._loaded_path = ""
            self._data = {}
            self._type_map = {}
            self._ufs_cache.clear()

    def get_app_type(self, appid: str) -> str:
        with self._lock:
            return self._type_map.get(str(appid), "")

    def get_savefiles(self, appid: str) -> list[dict]:
        with self._lock:
            return self._data.get(str(appid), [])

    def get_ufs_templates(self, appid: str) -> Optional[list[dict]]:
        """返回缓存的 UFS 模板副本；未命中返回 None。命中时刷新 LRU 顺序。"""
        with self._lock:
            cached = self._ufs_cache.get(appid)
            if cached is None:
                return None
            self._ufs_cache.move_to_end(appid)
            return list(cached)

    def store_ufs_templates(self, appid: str, templates: list[dict]) -> None:
        """写入 UFS 模板并按 LRU 上限淘汰最旧条目。"""
        with self._lock:
            self._ufs_cache[appid] = templates
            self._ufs_cache.move_to_end(appid)
            while len(self._ufs_cache) > self.UFS_CACHE_MAX:
                self._ufs_cache.popitem(last=False)

    def _parse(self, steam_path: str) -> None:
        """解析 appinfo.vdf 二进制文件，提取 UFS savefiles 与 common.type。

        格式: magic(4B) + universe(4B) + records...
        每条记录: appid(4B LE) + size(4B LE) + ... + binary KV data + 0x00 sentinel
        appinfo.vdf v28/v29 格式（Steam 2024+）：
          header: magic(4B) + universe(4B)
          record: appid(4B) + size(4B) + state(4B) + last_updated(4B) + token(8B)
                  + sha1(20B) + change_number(4B) + sha1_2(20B) + binary_kv_data
        """
        appinfo_path = os.path.join(steam_path, "appcache", "appinfo.vdf")
        if not os.path.isfile(appinfo_path):
            return
        try:
            with open(appinfo_path, "rb") as f:
                data = f.read()

            if len(data) < 8:
                return
            magic = struct.unpack_from("<I", data, 0)[0]
            # Magic: 0x07564428 (v28) or 0x07564429 (v29)
            if magic not in (0x07564428, 0x07564429):
                return

            pos = 8  # skip magic + universe
            while pos + 4 <= len(data):
                appid = struct.unpack_from("<I", data, pos)[0]
                if appid == 0:
                    break
                pos += 4
                if pos + 4 > len(data):
                    break
                size = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                if pos + size > len(data):
                    break
                record_data = data[pos:pos + size]
                pos += size

                # 提取 app type（快速字节搜索，开销极低）
                app_type = _extract_app_type_fast(record_data)
                if app_type:
                    self._type_map[str(appid)] = app_type

                # 快速检查：只处理包含 "savefiles" 的记录
                if b"savefiles" not in record_data:
                    continue

                # 从二进制 KV 数据中提取 UFS savefiles
                savefiles = _extract_ufs_savefiles(record_data)
                if savefiles:
                    self._data[str(appid)] = savefiles
            del data  # 释放可能 30-80 MB 的 appinfo.vdf 内存
        except Exception:
            pass


_APPINFO_CACHE = _AppInfoCache()


def _extract_app_type_fast(record_data: bytes) -> str:
    """
    从 appinfo 记录的二进制 KV 数据中快速提取 common.type 字段。
    利用字节模式搜索避免完整解析，性能开销极低。
    二进制 KV 中 string 值的格式: 0x01 + key_null_terminated + value_null_terminated
    在 common dict 内部查找 type 键的 string 值。
    """
    # 搜索 "type\x00" 后跟一个 null-terminated 字符串
    # 在 common dict 的上下文中，type 字段的值为 "Game", "Tool", "DLC", "Demo", "Config", "Music" 等
    kv_start = 60
    if kv_start >= len(record_data):
        return ""
    kv_data = record_data[kv_start:]

    # 快速搜索: 找到 common 节内的 type 字段
    # binary KV 中 string value 格式: 0x01 + "type\x00" + "Game\x00"
    marker = b"\x01type\x00"
    # 先确认在 common 节内
    common_marker = b"\x00common\x00"
    common_pos = kv_data.find(common_marker)
    if common_pos < 0:
        return ""

    # 从 common 节开始搜索 type 字段
    search_start = common_pos
    # 限制搜索范围到 common 节结束前（最多搜索 2KB）
    search_end = min(search_start + 2048, len(kv_data))
    type_pos = kv_data.find(marker, search_start, search_end)
    if type_pos < 0:
        return ""

    # 读取 type 值（null-terminated string）
    val_start = type_pos + len(marker)
    val_end = kv_data.find(b'\x00', val_start)
    if val_end < 0 or val_end - val_start > 30:  # type 值不应超过 30 字符
        return ""
    value = kv_data[val_start:val_end].decode("utf-8", errors="ignore")
    # 校验：仅返回已知的 Steam app type 值，防止误匹配其他 type 键
    _KNOWN_APP_TYPES = {
        "Game", "game", "Tool", "tool", "DLC", "dlc", "Demo", "demo",
        "Config", "config", "Music", "music", "Video", "video",
        "Series", "series", "Hardware", "hardware", "Application", "application",
    }
    return value if value in _KNOWN_APP_TYPES else ""


def _load_appinfo_vdf(steam_path: str):
    """兼容旧调用：确保指定路径的 appinfo.vdf 已解析到全局缓存。"""
    _APPINFO_CACHE.ensure_loaded(steam_path)


def _extract_ufs_savefiles(record_data: bytes) -> list[dict]:
    """
    从 appinfo 记录的二进制 KV 数据中提取 ufs.savefiles 条目。
    二进制 KV 格式:
      0x00 = nested dict start, key follows as null-terminated string
      0x01 = string value
      0x02 = int32 value
      0x08 = end of dict
    """
    results = []
    try:
        # 在 record header 之后找到 binary KV 数据
        # v28: header is state(4) + last_updated(4) + token(8) + sha1(20) + change_number(4) + sha1_2(20) = 60 bytes
        # v29 同理
        kv_start = 60
        if kv_start >= len(record_data):
            return []

        kv_data = record_data[kv_start:]
        # 解析嵌套结构，寻找 ufs > savefiles
        pos = [0]

        def read_string():
            start = pos[0]
            end = kv_data.find(b'\x00', start)
            if end < 0:
                pos[0] = len(kv_data)
                return ""
            s = kv_data[start:end].decode("utf-8", errors="ignore")
            pos[0] = end + 1
            return s

        def skip_value(vtype):
            if vtype == 0x01:  # string
                end = kv_data.find(b'\x00', pos[0])
                pos[0] = end + 1 if end >= 0 else len(kv_data)
            elif vtype == 0x02:  # int32
                pos[0] += 4
            elif vtype == 0x07:  # uint64
                pos[0] += 8
            elif vtype == 0x03:  # float
                pos[0] += 4
            elif vtype == 0x00:  # nested dict
                read_string()  # key
                parse_dict(None, 0)

        def parse_dict(target_key, depth):
            collected = {}
            while pos[0] < len(kv_data):
                vtype = kv_data[pos[0]]
                pos[0] += 1
                if vtype == 0x08:  # end
                    break
                key = read_string()
                if vtype == 0x00:  # nested dict
                    if depth == 0 and key == "ufs":
                        parse_dict("ufs", 1)
                    elif depth == 1 and target_key == "ufs" and key == "savefiles":
                        parse_dict("savefiles", 2)
                    elif depth == 2 and target_key == "savefiles" and key.isdigit():
                        entry = parse_savefile_entry()
                        if entry:
                            results.append(entry)
                    else:
                        parse_dict(None, depth + 1)
                elif vtype == 0x01:  # string
                    val = read_string()
                    collected[key] = val
                elif vtype == 0x02:  # int32
                    if pos[0] + 4 <= len(kv_data):
                        val = struct.unpack_from("<i", kv_data, pos[0])[0]
                        collected[key] = val
                    pos[0] += 4
                elif vtype == 0x07:  # uint64
                    pos[0] += 8
                elif vtype == 0x03:  # float
                    pos[0] += 4
                else:
                    break
            return collected

        def parse_savefile_entry():
            entry = {}
            while pos[0] < len(kv_data):
                vtype = kv_data[pos[0]]
                pos[0] += 1
                if vtype == 0x08:
                    break
                key = read_string()
                if vtype == 0x01:
                    entry[key] = read_string()
                elif vtype == 0x02:
                    if pos[0] + 4 <= len(kv_data):
                        entry[key] = struct.unpack_from("<i", kv_data, pos[0])[0]
                    pos[0] += 4
                elif vtype == 0x00:
                    parse_dict(None, 99)
                elif vtype == 0x07:
                    pos[0] += 8
                elif vtype == 0x03:
                    pos[0] += 4
                else:
                    break
            if "root" in entry and ("path" in entry or "pattern" in entry):
                return entry
            return None

        parse_dict(None, 0)
    except Exception:
        pass
    return results


# Root ID 映射 — 来源: Steam SDK ERemoteStorageFileRoot 枚举
# https://github.com/emily33901/SteamStructs/blob/master/ERemoteStorageFileRoot.h
# Root 0 (Default) = userdata/{UID}/{AppID}/remote/，由 remotecache 步骤处理，不在此映射
_APPINFO_ROOT_MAP = {
    "1": "[Game Install]",         # k_ERemoteStorageFileRootGameInstall
    "2": "[WinMyDocuments]",       # k_ERemoteStorageFileRootWinMyDocuments
    "3": "[WinAppDataLocal]",      # k_ERemoteStorageFileRootWinAppDataLocal
    "4": "[WinAppDataRoaming]",    # k_ERemoteStorageFileRootWinAppDataRoaming
    # "5": SteamUserBaseStorage — 用途待验证，暂不映射
    "9": "[WinSavedGames]",        # k_ERemoteStorageFileRootWinSavedGames
    "10": "[WinProgramData]",      # k_ERemoteStorageFileRootWinProgramData
    "12": "[WinAppDataLocalLow]",  # k_ERemoteStorageFileRootWinAppDataLocalLow (Unity 常用)
    "18": "%USERPROFILE%",         # WindowsHome
}

_STEAM_TEMPLATE_PATH_HINTS = tuple(token.lower() for token in (
    "%userprofile%", "%appdata%", "%localappdata%", "%programdata%",
    "%public%", "%documents%", "%mydocuments%", "%savedgames%",
    "%saved games%", "%locallow%",
    "[steam install]", "[steam library]", "[game install]",
    "[winmydocuments]", "[windocuments]", "[documents]", "[my documents]",
    "[winuserprofile]", "[userprofile]",
    "[winappdata]", "[appdata]", "[appdataroaming]", "[winappdataroaming]",
    "[localappdata]", "[winappdatalocal]", "[locallow]", "[winappdatalocallow]",
    "[saved games]", "[winsavedgames]", "[programdata]", "[winprogramdata]",
    "[public]", "[public documents]",
    "appdata", "documents", "saved games", "programdata", "userdata",
    "{steam3accountid}", "{64bitsteamid}",
))


def _get_steam_library_root(install_dir: str = "", steam_path: str = "",
                            library_path: str = "") -> str:
    candidates = []
    seen = set()

    norm_library = os.path.normpath(library_path) if library_path else ""
    if norm_library and norm_library not in seen and os.path.isdir(norm_library):
        seen.add(norm_library)
        candidates.append(norm_library)

    norm_install = os.path.normpath(install_dir) if install_dir else ""
    if norm_install:
        parts = Path(norm_install).parts
        lower_parts = [part.lower() for part in parts]
        for idx in range(len(lower_parts) - 1):
            if lower_parts[idx] == "steamapps" and idx + 1 < len(lower_parts) and lower_parts[idx + 1] == "common":
                lib_root = Path(*parts[:idx])
                lib_str = os.path.normpath(str(lib_root)) if str(lib_root) else ""
                if lib_str and lib_str not in seen:
                    seen.add(lib_str)
                    candidates.append(lib_str)
                break

    steam_norm = os.path.normpath(steam_path) if steam_path else ""
    if steam_norm:
        defaults = [steam_norm, os.path.dirname(steam_norm)]
        for candidate in defaults:
            norm = os.path.normpath(candidate) if candidate else ""
            if not norm or norm in seen:
                continue
            if os.path.isdir(os.path.join(norm, "steamapps")):
                seen.add(norm)
                candidates.append(norm)

    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return ""


def _split_ufs_path_and_patterns(path_value: str, pattern_value: str) -> tuple[str, list[str]]:
    raw_path = str(path_value or "").replace("\\", "/").strip().strip("/")
    raw_pattern = str(pattern_value or "").replace("\\", "/").strip().strip("/")
    includes = []
    clean_path = raw_path

    tail = raw_path.rsplit("/", 1)[-1] if raw_path else ""
    if any(ch in tail for ch in "*?"):
        clean_path = raw_path.rsplit("/", 1)[0] if "/" in raw_path else ""
        includes.append(tail)
    elif tail and "." in tail and "/" not in tail and not raw_pattern:
        clean_path = raw_path.rsplit("/", 1)[0] if "/" in raw_path else ""
        includes.append(tail)

    if raw_pattern and raw_pattern not in {"*", "*.*"}:
        includes.append(raw_pattern)
    elif raw_pattern in {"*", "*.*"}:
        includes = []

    deduped = []
    seen = set()
    for item in includes:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return clean_path, deduped


def parse_appinfo_ufs_entries(steam_path: str, appid: str) -> list[dict]:
    """
    从本地 appinfo.vdf 中提取指定 appid 的 UFS savefiles 条目。
    返回带模板和文件匹配规则的结构，便于精准识别根目录中的存档文件。
    """
    appid = str(appid or "").strip()
    if not appid or not steam_path:
        return []

    cached = _APPINFO_CACHE.get_ufs_templates(appid)
    if cached is not None:
        return cached

    _APPINFO_CACHE.ensure_loaded(steam_path)

    savefiles = _APPINFO_CACHE.get_savefiles(appid)
    templates = []
    seen = set()
    for entry in savefiles:
        root = str(entry.get("root", "0"))
        path_pattern = entry.get("path", "")
        file_pattern = entry.get("pattern", "")
        # 只取 Windows 相关条目
        platforms = str(entry.get("platforms", entry.get("platform", "")))
        if platforms and "windows" not in platforms.lower() and "all" not in platforms.lower():
            continue

        clean_path, includes = _split_ufs_path_and_patterns(path_pattern, file_pattern)
        recursive_value = entry.get("recursive", None)
        if recursive_value is None or str(recursive_value).strip() == "":
            recursive = not (includes and not clean_path)
        else:
            recursive_raw = str(recursive_value).strip().lower()
            recursive = recursive_raw not in {"0", "false", "no"}

        root_prefix = _APPINFO_ROOT_MAP.get(root)
        if root_prefix is None:
            if root == "0":
                # Root 0 (Default) = userdata/{UID}/{AppID}/remote/
                # 不生成路径模板（由 remotecache 步骤处理），但保留 includes/pattern 信息
                # 这样 remotecache 候选可以继承精确的文件匹配规则
                if includes:
                    root0_key = (
                        "__root0__",
                        clean_path.lower(),
                        tuple(p.lower() for p in includes),
                        recursive,
                    )
                    if root0_key not in seen:
                        seen.add(root0_key)
                        templates.append({
                            "template": "__root0__",
                            "relative_path": clean_path,
                            "includes": includes,
                            "recursive": recursive,
                            "root_id": "0",
                        })
            continue
        template = f"{root_prefix}/{clean_path}" if clean_path else root_prefix
        key = (template, tuple(p.lower() for p in includes), recursive)
        if key not in seen:
            seen.add(key)
            templates.append({
                "template": template,
                "includes": includes,
                "recursive": recursive,
            })

    _APPINFO_CACHE.store_ufs_templates(appid, templates)
    return list(templates)


def parse_appinfo_ufs(steam_path: str, appid: str) -> list[str]:
    """
    兼容旧调用：仅返回路径模板列表。
    """
    return [item["template"] for item in parse_appinfo_ufs_entries(steam_path, appid)]


def expand_path(template: str, install_dir: str = "") -> str:
    """将路径模板中的变量替换为实际系统路径"""
    return (template
            .replace("{APPDATA}", str(APPDATA))
            .replace("{LOCAL}", str(LOCAL_APPDATA))
            .replace("{LOCALLOW}", str(LOCAL_LOW))
            .replace("{DOCS}", str(DOCUMENTS))
            .replace("{SAVED}", str(SAVED_GAMES))
            .replace("{HOME}", str(USER_HOME))
            .replace("{INSTALL}", install_dir if install_dir else "__NO_INSTALL__"))


SAVE_PATH_VAR_ROOTS = (
    ("%APPDATA%", APPDATA),
    ("%LOCALAPPDATA%", LOCAL_APPDATA),
    ("%LOCALLOW%", LOCAL_LOW),
    ("%USERPROFILE%\\Documents", DOCUMENTS),
    ("%USERPROFILE%\\Saved Games", SAVED_GAMES),
    ("%USERPROFILE%", USER_HOME),
    ("%PUBLIC%", PUBLIC_HOME),
    ("%PROGRAMDATA%", PROGRAMDATA),
)

SAVE_PATH_VAR_ALIASES = (
    ("%APPDATA%", APPDATA),
    ("%LOCALAPPDATA%", LOCAL_APPDATA),
    ("%LOCALLOW%", LOCAL_LOW),
    ("%USERPROFILE%", USER_HOME),
    ("%HOME%", USER_HOME),
    ("%PUBLIC%", PUBLIC_HOME),
    ("%PROGRAMDATA%", PROGRAMDATA),
    ("{APPDATA}", APPDATA),
    ("{LOCAL}", LOCAL_APPDATA),
    ("{LOCALLOW}", LOCAL_LOW),
    ("{DOCS}", DOCUMENTS),
    ("{SAVED}", SAVED_GAMES),
    ("{HOME}", USER_HOME),
)


def _replace_token_ci(text: str, token: str, value: str) -> str:
    return re.sub(re.escape(token), lambda _m: value, text, flags=re.IGNORECASE)


def _contains_save_path_var(path: str) -> bool:
    raw = str(path or "")
    return any(re.search(re.escape(token), raw, flags=re.IGNORECASE) for token, _ in SAVE_PATH_VAR_ALIASES)


def expand_save_path_vars(path: str) -> str:
    """Expand portable save-path variables to the current machine's absolute path."""
    clean = str(path or "").strip()
    if not clean:
        return ""
    expanded = clean
    for token, value in SAVE_PATH_VAR_ALIASES:
        expanded = _replace_token_ci(expanded, token, str(value))
    expanded = os.path.expandvars(os.path.expanduser(expanded))
    return os.path.normpath(expanded)


def _path_has_prefix(path: str, root: str) -> bool:
    try:
        path_norm = os.path.normcase(os.path.normpath(path))
        root_norm = os.path.normcase(os.path.normpath(root))
        return os.path.commonpath([path_norm, root_norm]) == root_norm
    except Exception:
        return False


def compact_save_path_vars(path: str) -> str:
    """Convert local absolute save paths into portable variables for config storage."""
    clean = str(path or "").strip()
    if not clean:
        return ""
    if _contains_save_path_var(clean):
        return os.path.normpath(clean)
    expanded = expand_save_path_vars(clean)
    expanded_norm = os.path.normpath(expanded)
    for token, root in SAVE_PATH_VAR_ROOTS:
        root_norm = os.path.normpath(str(root))
        if not root_norm or not _path_has_prefix(expanded_norm, root_norm):
            continue
        if os.path.normcase(expanded_norm) == os.path.normcase(root_norm):
            return os.path.normpath(token)
        suffix = expanded_norm[len(root_norm):].lstrip("\\/")
        return os.path.normpath(os.path.join(token, suffix))
    return os.path.normpath(clean)


_RE_RECOGNITION_NAME = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def _normalize_recognition_name(name: str) -> str:
    return _RE_RECOGNITION_NAME.sub("", (name or "").lower())


_ROMAN_MAP = {
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6",
    "vii": "7", "viii": "8", "ix": "9", "x": "10", "xi": "11",
    "xii": "12", "xiii": "13", "xiv": "14", "xv": "15", "xvi": "16",
}
_NOISE_WORDS = {"the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "by"}


def _extract_search_keywords(game_name: str) -> list[str]:
    """
    从游戏名中提取增强关键词列表，包括：
    - 原始词（去标点）
    - 罗马数字转阿拉伯数字
    - 首字母缩写
    - 去掉噪音词后的连续形式
    """
    if not game_name:
        return []
    raw_words = re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", game_name)
    keywords = []
    seen = set()

    def _add(w: str):
        w = w.lower()
        if w and len(w) > 1 and w not in seen:
            seen.add(w)
            keywords.append(w)

    for w in raw_words:
        wl = w.lower()
        _add(wl)
        if wl in _ROMAN_MAP:
            _add(_ROMAN_MAP[wl])

    meaningful = [w for w in raw_words if w.lower() not in _NOISE_WORDS and len(w) > 1]
    if len(meaningful) >= 2:
        acronym = "".join(w[0] for w in meaningful).lower()
        if len(acronym) >= 2:
            _add(acronym)

    return keywords


def _recognition_key(appid: str, game_name: str) -> str:
    appid = str(appid or "").strip()
    if appid:
        return f"appid:{appid}"
    return f"name:{_normalize_recognition_name(game_name)}"


def get_recognition_cache(cfg: Optional[dict]) -> dict:
    if not cfg:
        return {}
    cache = cfg.setdefault("recognition_cache", {})
    if not isinstance(cache, dict):
        cfg["recognition_cache"] = {}
        cache = cfg["recognition_cache"]
    return cache


def get_recognition_excludes(cfg: Optional[dict]) -> dict:
    if not cfg:
        return {}
    excludes = cfg.setdefault("recognition_excludes", {})
    if not isinstance(excludes, dict):
        cfg["recognition_excludes"] = {}
        excludes = cfg["recognition_excludes"]
    return excludes


def get_recognition_blacklist(cfg: Optional[dict], appid: str, game_name: str) -> set[str]:
    key = _recognition_key(appid, game_name)
    excludes = get_recognition_excludes(cfg).get(key, [])
    if not isinstance(excludes, list):
        return set()
    return {expand_save_path_vars(p) for p in excludes if isinstance(p, str) and p}


def get_cached_recognition_path(cfg: Optional[dict], appid: str, game_name: str) -> str:
    cache = get_recognition_cache(cfg)
    key = _recognition_key(appid, game_name)
    entry = cache.get(key, {})
    if isinstance(entry, dict):
        path = entry.get("path", "")
    elif isinstance(entry, str):
        path = entry
    else:
        path = ""
    path = expand_save_path_vars(path) if path else ""
    if path and os.path.exists(path):
        return path
    return ""


def remember_recognition_path(cfg: Optional[dict], appid: str, game_name: str, path: str):
    if not cfg or not path:
        return
    key = _recognition_key(appid, game_name)
    norm = expand_save_path_vars(path)
    get_recognition_cache(cfg)[key] = {
        "path": norm,
        "updated_at": time.time(),
    }
    excludes = get_recognition_excludes(cfg).get(key, [])
    if isinstance(excludes, list):
        get_recognition_excludes(cfg)[key] = [p for p in excludes if expand_save_path_vars(p) != norm]
    with _SAVE_DETECTION_CACHE_LOCK:
        _SAVE_DETECTION_CACHE.clear()


def exclude_recognition_path(cfg: Optional[dict], appid: str, game_name: str, path: str):
    if not cfg or not path:
        return
    key = _recognition_key(appid, game_name)
    norm = expand_save_path_vars(path)
    excludes = get_recognition_excludes(cfg).setdefault(key, [])
    if norm not in [expand_save_path_vars(p) for p in excludes if isinstance(p, str)]:
        excludes.append(norm)
    cached = get_recognition_cache(cfg).get(key)
    if isinstance(cached, dict) and expand_save_path_vars(cached.get("path", "")) == norm:
        get_recognition_cache(cfg).pop(key, None)
    with _SAVE_DETECTION_CACHE_LOCK:
        _SAVE_DETECTION_CACHE.clear()


def get_confirmed_game_path(cfg: Optional[dict], appid: str, game_name: str) -> str:
    if not cfg:
        return ""
    target_appid = str(appid or "").strip()
    target_name = (game_name or "").strip().lower()
    for game in cfg.get("games", []):
        game_appid = str(game.get("appid") or "").strip()
        game_name_norm = str(game.get("name") or "").strip().lower()
        if (target_appid and game_appid == target_appid) or (not target_appid and target_name and game_name_norm == target_name):
            save_paths = get_game_save_paths(game, existing_only=True)
            if save_paths:
                return save_paths[0]
    return ""


def _normalize_unique_paths(paths: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        clean = path.strip()
        if not clean:
            continue
        norm = expand_save_path_vars(clean)
        key = os.path.normcase(norm)
        if key in seen:
            continue
        seen.add(key)
        normalized.append(norm)
    return normalized


def _normalize_unique_storage_paths(paths: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for path in paths:
        if not isinstance(path, str):
            continue
        clean = path.strip()
        if not clean:
            continue
        norm = compact_save_path_vars(clean)
        key = os.path.normcase(expand_save_path_vars(norm))
        if key in seen:
            continue
        seen.add(key)
        normalized.append(norm)
    return normalized


def _normalize_save_spec(spec: dict, *, for_storage: bool = False) -> Optional[dict]:
    if not isinstance(spec, dict):
        return None
    base = str(spec.get("base", "") or "").strip()
    if not base:
        return None
    base = compact_save_path_vars(base) if for_storage else expand_save_path_vars(base)
    includes = spec.get("includes", [])
    if isinstance(includes, str):
        includes = [includes]
    normalized_includes = []
    seen = set()
    for pattern in includes if isinstance(includes, list) else []:
        if not isinstance(pattern, str):
            continue
        clean = pattern.replace("\\", "/").strip().lstrip("./")
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized_includes.append(clean)
    return {
        "base": os.path.normpath(base),
        "includes": normalized_includes,
        "recursive": bool(spec.get("recursive", True)),
    }


def _save_spec_fully_covers(parent: dict, child: dict) -> bool:
    """Return True only when parent provably includes every file in child."""
    if parent.get("includes") or not parent.get("recursive", True):
        return False
    parent_base = expand_save_path_vars(parent.get("base", ""))
    child_base = expand_save_path_vars(child.get("base", ""))
    if not parent_base or not child_base:
        return False
    if os.path.normcase(parent_base) == os.path.normcase(child_base):
        return False
    return _path_is_within(child_base, parent_base)


def _normalize_unique_save_specs(specs: list[dict], *, for_storage: bool = False) -> list[dict]:
    normalized = []
    seen = set()
    for spec in specs:
        clean = _normalize_save_spec(spec, for_storage=for_storage)
        if not clean:
            continue
        key = (
            os.path.normcase(expand_save_path_vars(clean["base"])),
            tuple(p.lower() for p in clean["includes"]),
            clean["recursive"],
        )
        if key in seen:
            continue
        seen.add(key)
        normalized.append(clean)
    filtered_bases = {
        os.path.normcase(expand_save_path_vars(spec["base"]))
        for spec in normalized
        if spec.get("includes")
    }
    if filtered_bases:
        normalized = [
            spec for spec in normalized
            if not (
                os.path.normcase(expand_save_path_vars(spec["base"])) in filtered_bases
                and not spec.get("includes")
            )
        ]
    return normalized


def _collapse_fully_covered_save_specs(
        specs: list[dict], *, for_storage: bool = False) -> list[dict]:
    """Collapse scanner-produced parent/child specs without changing legacy mappings."""
    normalized = _normalize_unique_save_specs(specs, for_storage=for_storage)
    return [
        spec for idx, spec in enumerate(normalized)
        if not any(
            other_idx != idx and _save_spec_fully_covers(other, spec)
            for other_idx, other in enumerate(normalized)
        )
    ]


def _merge_save_specs_with_selected_files(specs: list[dict], file_paths,
                                           *, for_storage: bool = True) -> list[dict]:
    """Add only the explicitly selected files while preserving existing rule order."""
    normalized = _normalize_unique_save_specs(specs, for_storage=for_storage)
    selected_by_base: OrderedDict[str, dict] = OrderedDict()
    for file_path in file_paths or []:
        clean = str(file_path or "").strip()
        if not clean:
            continue
        expanded_file = expand_save_path_vars(clean)
        file_name = os.path.basename(expanded_file)
        parent = os.path.dirname(expanded_file)
        if not file_name or not parent:
            continue
        base = compact_save_path_vars(parent) if for_storage else os.path.normpath(parent)
        base_key = os.path.normcase(expand_save_path_vars(base))
        group = selected_by_base.setdefault(base_key, {"base": base, "includes": []})
        pattern = glob.escape(file_name).replace("\\", "/")
        if pattern.lower() not in {item.lower() for item in group["includes"]}:
            group["includes"].append(pattern)

    for base_key, selected in selected_by_base.items():
        selected_spec = {
            "base": selected["base"],
            "includes": list(selected["includes"]),
            "recursive": False,
        }
        unfiltered_indexes = [
            idx for idx, spec in enumerate(normalized)
            if os.path.normcase(expand_save_path_vars(spec["base"])) == base_key
            and not spec.get("includes")
        ]
        if not unfiltered_indexes:
            normalized.append(selected_spec)
            continue

        insert_at = unfiltered_indexes[0]
        normalized = [
            spec for idx, spec in enumerate(normalized)
            if idx not in set(unfiltered_indexes)
        ]
        normalized.insert(insert_at, selected_spec)
    return _normalize_unique_save_specs(normalized, for_storage=for_storage)


def _default_save_spec(path: str, *, for_storage: bool = False) -> dict:
    return {
        "base": os.path.normpath(compact_save_path_vars(path) if for_storage else expand_save_path_vars(path)),
        "includes": [],
        "recursive": True,
    }


def get_game_save_specs(game: Optional[dict], existing_only: bool = False) -> list[dict]:
    if not isinstance(game, dict):
        return []
    raw_specs = game.get("save_specs", [])
    specs = _normalize_unique_save_specs(raw_specs if isinstance(raw_specs, list) else [])
    if not specs:
        specs = [_default_save_spec(path) for path in get_game_save_paths(game, existing_only=False)]
    if existing_only:
        specs = [spec for spec in specs if os.path.isdir(spec["base"])]
    return specs


def get_game_save_paths(game: Optional[dict], existing_only: bool = False) -> list[str]:
    if not isinstance(game, dict):
        return []
    raw_specs = game.get("save_specs", [])
    normalized_specs = _normalize_unique_save_specs(raw_specs if isinstance(raw_specs, list) else [])
    if normalized_specs:
        result = _normalize_unique_paths([spec.get("base", "") for spec in normalized_specs])
    else:
        paths = []
        raw_paths = game.get("save_paths", [])
        if isinstance(raw_paths, list):
            paths.extend(raw_paths)
        primary = game.get("save_path", "")
        if isinstance(primary, str) and primary.strip():
            paths.append(primary)
        result = _normalize_unique_paths(paths)
    if existing_only:
        result = [p for p in result if os.path.isdir(p)]
    return result


def get_game_save_path_templates(game: Optional[dict], existing_only: bool = False) -> list[str]:
    if not isinstance(game, dict):
        return []
    raw_specs = game.get("save_specs", [])
    storage_specs = _normalize_unique_save_specs(raw_specs if isinstance(raw_specs, list) else [], for_storage=True)
    if storage_specs:
        result = _normalize_unique_storage_paths([spec.get("base", "") for spec in storage_specs])
    else:
        paths = []
        raw_paths = game.get("save_paths", [])
        if isinstance(raw_paths, list):
            paths.extend(raw_paths)
        primary = game.get("save_path", "")
        if isinstance(primary, str) and primary.strip():
            paths.append(primary)
        result = _normalize_unique_storage_paths(paths)
    if existing_only:
        result = [p for p in result if os.path.isdir(expand_save_path_vars(p))]
    return result


def set_game_save_specs(game: dict, specs: list[dict]):
    normalized_specs = _normalize_unique_save_specs(specs, for_storage=True)
    game["save_specs"] = normalized_specs
    bases = _normalize_unique_storage_paths([spec["base"] for spec in normalized_specs])
    game["save_paths"] = bases
    game["save_path"] = bases[0] if bases else ""


def set_game_save_paths(game: dict, paths: list[str]):
    normalized = _normalize_unique_storage_paths(paths)
    existing_specs = get_game_save_specs(game, existing_only=False)
    grouped: dict[str, list[dict]] = {}
    for spec in existing_specs:
        grouped.setdefault(os.path.normcase(expand_save_path_vars(spec["base"])), []).append(spec)
    rebuilt_specs = []
    for path in normalized:
        specs_for_base = grouped.get(os.path.normcase(expand_save_path_vars(path)), [])
        if specs_for_base:
            for spec in specs_for_base:
                rebuilt = dict(spec)
                rebuilt["base"] = path
                rebuilt_specs.append(rebuilt)
        else:
            rebuilt_specs.append(_default_save_spec(path, for_storage=True))
    set_game_save_specs(game, rebuilt_specs)


def get_game_uid(game: Optional[dict]) -> str:
    if not isinstance(game, dict):
        return ""
    return str(game.get("game_uid", "") or "").strip()


def _build_game_storage_key(name: str, uid: str) -> str:
    base = sanitize(str(name or "")).strip() or "game"
    uid_part = re.sub(r"[^a-zA-Z0-9]+", "", str(uid or "").strip())[:8]
    if uid_part:
        return f"{base}__{uid_part}"
    return base


def ensure_game_storage_identity(game: Optional[dict]) -> str:
    if not isinstance(game, dict):
        return ""
    uid = get_game_uid(game)
    if not uid:
        uid = uuid.uuid4().hex
        game["game_uid"] = uid
    storage_key = str(game.get("storage_key", "") or "").strip()
    if not storage_key:
        storage_key = _build_game_storage_key(game.get("name", ""), uid)
        game["storage_key"] = storage_key
    return storage_key


def get_game_storage_key(game_or_name, game_uid: str = "", storage_key: str = "") -> str:
    if isinstance(game_or_name, dict):
        existing = str(game_or_name.get("storage_key", "") or "").strip()
        if existing:
            return existing
        uid = get_game_uid(game_or_name)
        if uid:
            return _build_game_storage_key(game_or_name.get("name", ""), uid)
        return sanitize(str(game_or_name.get("name", "") or "")).strip() or "game"
    name = str(game_or_name or "").strip()
    if storage_key:
        return str(storage_key).strip()
    if game_uid:
        return _build_game_storage_key(name, game_uid)
    return sanitize(name).strip() or "game"


def get_game_runtime_key(game: Optional[dict]) -> str:
    if not isinstance(game, dict):
        return ""
    appid = str(game.get("appid", "") or "").strip()
    storage_key = get_game_storage_key(game)
    if appid:
        return f"appid:{appid}|storage:{storage_key}"
    return f"storage:{storage_key}"


def _normalize_monitor_process_name(value: str) -> str:
    raw = str(value or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    return os.path.basename(raw).lower()


def _monitor_process_variants(value: str) -> set[str]:
    clean = _normalize_monitor_process_name(value)
    if not clean:
        return set()
    root, ext = os.path.splitext(clean)
    if ext:
        return {clean, root}
    return {clean, f"{clean}.exe"}


def normalize_monitor_processes(values) -> list[str]:
    if isinstance(values, str):
        raw_items = re.split(r"[\r\n,;|]+", values)
    elif isinstance(values, list):
        raw_items = values
    else:
        raw_items = []
    normalized = []
    seen = set()
    for item in raw_items:
        clean = _normalize_monitor_process_name(str(item or ""))
        if not clean or clean in seen:
            continue
        seen.add(clean)
        normalized.append(clean)
    return normalized


def get_game_monitor_processes(game: Optional[dict]) -> list[str]:
    if not isinstance(game, dict):
        return []
    return normalize_monitor_processes(game.get("monitor_processes", []))


def set_game_monitor_processes(game: dict, values):
    processes = normalize_monitor_processes(values)
    if processes:
        game["monitor_processes"] = processes
    else:
        game.pop("monitor_processes", None)


def is_game_sync_enabled(game: Optional[dict]) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(game.get("sync_enabled", True))


def is_game_scheduled_backup_enabled(game: Optional[dict]) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(game.get("auto_backup", True))


def is_game_watch_backup_enabled(game: Optional[dict]) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(game.get("watch_backup", game.get("auto_backup", True)))


def is_game_exit_backup_enabled(game: Optional[dict]) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(game.get("backup_on_exit", False))


def is_game_favorite(game: Optional[dict]) -> bool:
    if not isinstance(game, dict):
        return False
    return bool(game.get("favorite", False))


def format_monitor_processes(values) -> str:
    return ", ".join(normalize_monitor_processes(values))


def _normalize_spec_signature(specs: list[dict]) -> tuple:
    return tuple(
        (
            os.path.normcase(spec["base"]),
            tuple(p.lower() for p in spec.get("includes", [])),
            bool(spec.get("recursive", True)),
        )
        for spec in _normalize_unique_save_specs(specs)
    )


def game_matches_save_target(existing_game: Optional[dict], appid: str,
                             save_specs: Optional[list[dict]] = None,
                             save_paths: Optional[list[str]] = None) -> bool:
    if not isinstance(existing_game, dict):
        return False
    if str(existing_game.get("appid", "") or "").strip() != str(appid or "").strip():
        return False
    existing_specs = get_game_save_specs(existing_game, existing_only=False)
    if save_specs:
        return _normalize_spec_signature(existing_specs) == _normalize_spec_signature(save_specs)
    return _normalize_unique_paths(get_game_save_paths(existing_game, existing_only=False)) == _normalize_unique_paths(save_paths or [])


_SCAN_AUTOMATION_TRUSTED_SOURCES = {
    "confirmed", "manual", "known-path", "remotecache", "steamdb", "appinfo",
}
_SCAN_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _scan_absolute_path(path: str) -> str:
    expanded = expand_save_path_vars(path)
    if not expanded:
        return ""
    return os.path.normcase(os.path.realpath(os.path.abspath(expanded)))


def _scan_paths_equal(left: str, right: str) -> bool:
    left_key = _scan_absolute_path(left)
    right_key = _scan_absolute_path(right)
    return bool(left_key and right_key and left_key == right_key)


def _literal_save_spec_patterns(spec: dict) -> Optional[list[tuple[str, str, bool]]]:
    includes = spec.get("includes", [])
    if not includes:
        return None
    patterns = []
    for pattern in includes:
        clean = str(pattern or "").replace("\\", "/").strip().lstrip("./").lower()
        if not clean or glob.has_magic(clean):
            return None
        patterns.append((clean, clean.rsplit("/", 1)[-1], "/" not in clean))
    return patterns or None


def _save_specs_proven_disjoint_same_base(left: dict, right: dict) -> bool:
    """Prove that two literal include sets on one directory cannot select one file."""
    if not _scan_paths_equal(left.get("base", ""), right.get("base", "")):
        return False
    left_patterns = _literal_save_spec_patterns(left)
    right_patterns = _literal_save_spec_patterns(right)
    if left_patterns is None or right_patterns is None:
        return False
    left_recursive = bool(left.get("recursive", True))
    right_recursive = bool(right.get("recursive", True))
    for left_path, left_name, left_is_name in left_patterns:
        for right_path, right_name, right_is_name in right_patterns:
            if left_path == right_path:
                return False
            if left_recursive and left_is_name and left_name == right_name:
                return False
            if right_recursive and right_is_name and left_name == right_name:
                return False
    return True


def _save_specs_block_scan_merge(incoming: dict, existing: dict) -> bool:
    if _scan_paths_equal(incoming.get("base", ""), existing.get("base", "")):
        incoming_key = (
            tuple(sorted(pattern.lower() for pattern in incoming.get("includes", []))),
            bool(incoming.get("recursive", True)),
        )
        existing_key = (
            tuple(sorted(pattern.lower() for pattern in existing.get("includes", []))),
            bool(existing.get("recursive", True)),
        )
        if incoming_key == existing_key:
            return True
        return not incoming.get("includes") or not existing.get("includes")
    return bool(
        _save_spec_fully_covers(incoming, existing)
        or _save_spec_fully_covers(existing, incoming)
    )


def _steam_userdata_relative_parts(path: str, steam_path: str) -> Optional[list[str]]:
    steam_norm = os.path.normpath(steam_path) if steam_path else ""
    roots = [
        os.path.join(steam_norm, "userdata") if steam_norm else "",
        steam_norm if steam_norm and os.path.basename(steam_norm).lower() == "userdata" else "",
        os.path.join(os.path.dirname(steam_norm), "userdata") if steam_norm else "",
        os.path.join(os.path.dirname(os.path.dirname(steam_norm)), "userdata") if steam_norm else "",
        os.path.join(str(LOCAL_APPDATA), "Steam", "userdata"),
        os.path.join(r"C:\Program Files (x86)\Steam", "userdata"),
        os.path.join(r"C:\Program Files\Steam", "userdata"),
    ]
    path_abs = _scan_absolute_path(path)
    for root in roots:
        root_abs = _scan_absolute_path(root)
        if not root_abs or not _path_is_within(path_abs, root_abs):
            continue
        relative = os.path.relpath(path_abs, root_abs)
        if relative == ".":
            return []
        return [part.lower() for part in Path(relative).parts]
    return None


def _scan_path_safety_reason(path: str, spec: dict, *,
                             install_dir: str = "", steam_path: str = "",
                             library_path: str = "",
                             cfg: Optional[dict] = None) -> str:
    path_abs = _scan_absolute_path(path)
    if not path_abs:
        return "empty-path"

    path_parts = [part.lower() for part in Path(path_abs).parts]
    userdata_tail = _steam_userdata_relative_parts(path_abs, steam_path)
    if userdata_tail is not None:
        if userdata_tail and userdata_tail[0] == "0":
            return "steam-userdata-uid-0"
        if len(userdata_tail) <= 1:
            return "steam-userdata-root"
        if len(userdata_tail) == 2 and not spec.get("includes") and spec.get("recursive", True):
            return "steam-app-root-unfiltered"

    if any(re.search(r"_pre_restore_\d{8}_\d{6}", part) for part in path_parts):
        return "restore-safety-path"

    descendant_roots = [
        str(CONFIG_DIR),
        str(BACKUP_ROOT),
        str((cfg or {}).get("backup_path", "") or ""),
        str((cfg or {}).get("sync_folder", "") or ""),
    ]
    for root in descendant_roots:
        root_abs = _scan_absolute_path(root)
        if root_abs and (
            _path_is_within(path_abs, root_abs)
            or _path_is_within(root_abs, path_abs)
        ):
            return "managed-storage-root"

    exact_roots = [
        str(APP_DIR),
        steam_path,
        library_path,
        os.path.join(steam_path, "userdata") if steam_path else "",
        os.path.join(steam_path, "steamapps") if steam_path else "",
        os.path.join(steam_path, "steamapps", "common") if steam_path else "",
        os.path.join(library_path, "steamapps") if library_path else "",
        os.path.join(library_path, "steamapps", "common") if library_path else "",
        str(USER_HOME),
        str(APPDATA),
        str(LOCAL_APPDATA),
        str(LOCAL_LOW),
        str(DOCUMENTS),
        str(SAVED_GAMES),
        str(PROGRAMDATA),
        str(PUBLIC_DOCUMENTS),
    ]
    for root in exact_roots:
        if root and _scan_paths_equal(path_abs, root):
            return "broad-system-root"

    volume_root = _get_volume_root(path_abs)
    if volume_root and _scan_paths_equal(path_abs, volume_root):
        return "volume-root"

    if install_dir:
        if _scan_paths_equal(path_abs, install_dir):
            includes = {
                str(pattern or "").replace("\\", "/").strip().lower()
                for pattern in spec.get("includes", [])
                if str(pattern or "").strip()
            }
            broad_patterns = {"*", "*.*", "**", "**/*"}
            if (
                not includes
                or includes & broad_patterns
                or spec.get("recursive", True)
            ):
                return "install-root-requires-precise-includes"
        elif _path_is_within(install_dir, path_abs):
            return "install-ancestor"
    return ""


def find_scan_path_conflict(games: list[dict], appid: str,
                            specs: list[dict]) -> Optional[dict]:
    target_appid = str(appid or "").strip()
    target_specs = _normalize_unique_save_specs(specs)
    for game in games or []:
        if not isinstance(game, dict):
            continue
        existing_appid = str(game.get("appid", "") or "").strip()
        if not existing_appid or existing_appid == target_appid:
            continue
        for existing_spec in get_game_save_specs(game, existing_only=False):
            for target_spec in target_specs:
                same_base = _scan_paths_equal(
                    existing_spec.get("base", ""),
                    target_spec.get("base", ""),
                )
                if same_base and _save_specs_proven_disjoint_same_base(
                        existing_spec, target_spec):
                    continue
                if same_base or _save_spec_fully_covers(
                        existing_spec, target_spec) or _save_spec_fully_covers(
                            target_spec, existing_spec):
                    return game
    return None


def validate_scan_save_specs(*, appid: str, selected_path: str,
                             save_specs: Optional[list[dict]] = None,
                             save_paths: Optional[list[str]] = None,
                             install_dir: str = "", steam_path: str = "",
                             library_path: str = "",
                             cfg: Optional[dict] = None) -> tuple[list[dict], str]:
    raw_specs = list(save_specs or [])
    if not raw_specs:
        raw_specs = [_default_save_spec(path) for path in (save_paths or []) if path]
    normalized = _normalize_unique_save_specs(raw_specs)
    if not normalized:
        return [], "no-save-specs"

    selected_probe = _default_save_spec(selected_path) if selected_path else None
    selected_covered = not selected_probe
    safe_specs = []
    for spec in normalized:
        reason = _scan_path_safety_reason(
            spec.get("base", ""),
            spec,
            install_dir=install_dir,
            steam_path=steam_path,
            library_path=library_path,
            cfg=cfg,
        )
        covers_selected = bool(
            selected_probe and (
                _scan_paths_equal(spec.get("base", ""), selected_path)
                or _save_spec_fully_covers(spec, selected_probe)
            )
        )
        if reason:
            if covers_selected:
                return [], reason
            continue
        safe_specs.append(spec)
        selected_covered = selected_covered or covers_selected

    safe_specs = _collapse_fully_covered_save_specs(safe_specs)
    if not safe_specs or not selected_covered:
        return [], "selected-path-rejected"
    conflict = find_scan_path_conflict(
        list((cfg or {}).get("games", [])),
        appid,
        safe_specs,
    )
    if conflict:
        return [], f"path-used-by-appid:{str(conflict.get('appid', '') or '').strip()}"
    return safe_specs, ""


def scan_candidate_enables_automation(candidate: Optional[dict],
                                      user_confirmed: bool) -> bool:
    candidate = candidate or {}
    return bool(
        user_confirmed
        and candidate.get("confidence") == "high"
        and candidate.get("source") in _SCAN_AUTOMATION_TRUSTED_SOURCES
        and candidate.get("materialized", True)
    )


def apply_scanned_game_metadata(game: dict, candidate: Optional[dict],
                                user_confirmed: bool) -> None:
    candidate = candidate or {}
    automation_enabled = scan_candidate_enables_automation(candidate, user_confirmed)
    game["scan_generated"] = True
    game["scan_source"] = str(candidate.get("source", "") or "")
    game["scan_confidence"] = str(candidate.get("confidence", "low") or "low")
    game["scan_confirmed"] = bool(user_confirmed)
    game["auto_backup"] = automation_enabled
    game["watch_backup"] = automation_enabled
    game["backup_on_exit"] = False
    game["sync_enabled"] = automation_enabled


def is_scan_generated_game(game: Optional[dict]) -> bool:
    if not isinstance(game, dict) or game.get("manual_save_specs"):
        return False
    if game.get("scan_generated"):
        return True
    return bool(game.get("appid") and game.get("library_path"))


def merge_scanned_game_record(games: list[dict], incoming: dict,
                              cfg: Optional[dict] = None) -> tuple[str, dict]:
    appid = str(incoming.get("appid", "") or "").strip()
    existing = next(
        (
            game for game in games
            if str(game.get("appid", "") or "").strip() == appid
            and is_scan_generated_game(game)
        ),
        None,
    )
    if existing is None:
        duplicate = next(
            (
                game for game in games
                if game_matches_save_target(
                    game,
                    appid,
                    save_specs=get_game_save_specs(incoming, existing_only=False),
                )
            ),
            None,
        )
        if duplicate is not None:
            return "unchanged", duplicate
        games.append(incoming)
        return "added", incoming

    old_game = dict(existing)
    old_specs = get_game_save_specs(existing, existing_only=False)
    incoming_specs = get_game_save_specs(incoming, existing_only=False)
    mergeable_specs = [
        spec for spec in incoming_specs
        if not any(_save_specs_block_scan_merge(spec, old_spec) for old_spec in old_specs)
    ]
    if not mergeable_specs:
        return "unchanged", existing
    merged_specs = _collapse_fully_covered_save_specs(
        old_specs + mergeable_specs
    )
    changed = _normalize_spec_signature(old_specs) != _normalize_spec_signature(merged_specs)
    if changed:
        set_game_save_specs(existing, merged_specs)

    for key in ("library_path", "scan_source"):
        if not existing.get(key) and incoming.get(key):
            existing[key] = incoming[key]
            changed = True
    if not existing.get("scan_generated"):
        existing["scan_generated"] = True
        changed = True
    combined_confirmed = bool(
        existing.get("scan_confirmed", False) and incoming.get("scan_confirmed", False)
    )
    if existing.get("scan_confirmed") != combined_confirmed:
        existing["scan_confirmed"] = combined_confirmed
        changed = True
    confidence = min(
        (
            str(existing.get("scan_confidence", "low") or "low"),
            str(incoming.get("scan_confidence", "low") or "low"),
        ),
        key=lambda value: _SCAN_CONFIDENCE_RANK.get(value, 0),
    )
    if existing.get("scan_confidence") != confidence:
        existing["scan_confidence"] = confidence
        changed = True
    for key in ("auto_backup", "watch_backup", "backup_on_exit", "sync_enabled"):
        combined = bool(existing.get(key, False) and incoming.get(key, False))
        if existing.get(key) != combined:
            existing[key] = combined
            changed = True
    ensure_game_storage_identity(existing)
    if changed and cfg is not None:
        clear_game_sync_state(cfg, old_game)
    return ("merged" if changed else "unchanged"), existing


def get_game_cache_key(game_or_name) -> str:
    if isinstance(game_or_name, dict):
        storage_key = str(game_or_name.get("storage_key", "") or "").strip()
        if storage_key:
            return f"storage:{storage_key}"
        uid = get_game_uid(game_or_name)
        if uid:
            return f"uid:{uid}"
        return get_game_sync_key(game_or_name)
    return f"name:{str(game_or_name or '').strip()}"


def _save_spec_covers_entire_dir(spec: dict) -> bool:
    includes = spec.get("includes", [])
    return bool(spec.get("recursive", True)) and not includes


def _save_spec_match_relpath(spec: dict, rel_path: str) -> bool:
    rel_norm = rel_path.replace("\\", "/").lstrip("./")
    includes = spec.get("includes", [])
    recursive = bool(spec.get("recursive", True))
    if not includes:
        return recursive or "/" not in rel_norm
    if not recursive and "/" in rel_norm:
        return False
    rel_name = rel_norm.rsplit("/", 1)[-1]
    for pattern in includes:
        pat = pattern.replace("\\", "/").lstrip("./")
        if not recursive and "/" in pat:
            if fnmatch.fnmatch(rel_norm, pat):
                return True
            continue
        if recursive:
            if fnmatch.fnmatch(rel_norm, pat) or fnmatch.fnmatch(rel_name, pat):
                return True
        elif fnmatch.fnmatch(rel_name, pat) or fnmatch.fnmatch(rel_norm, pat):
            return True
    return False


def _is_ignored_save_payload_file(path_or_name: str | Path) -> bool:
    rel = str(path_or_name or "").replace("\\", "/")
    name = PurePosixPath(rel).name.lower()
    if not name:
        return False
    return name in SAVE_PAYLOAD_IGNORED_NAMES or Path(name).suffix.lower() in SAVE_PAYLOAD_IGNORED_SUFFIXES


def _save_specs_match_path(specs: list[dict], abs_path: str) -> bool:
    target = Path(abs_path)
    for spec in _normalize_unique_save_specs(specs):
        try:
            base = Path(spec.get("base", ""))
            if not base:
                continue
            rel = target.relative_to(base).as_posix()
        except Exception:
            continue
        if _is_ignored_save_payload_file(rel):
            continue
        if _save_spec_match_relpath(spec, rel):
            return True
    return False


def iter_save_spec_files(specs: list[dict]):
    seen_files = set()
    for idx, spec in enumerate(_normalize_unique_save_specs(specs), start=1):
        base = Path(spec["base"])
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            rel_root = os.path.relpath(root, base)
            if not spec.get("recursive", True) and rel_root != ".":
                dirs[:] = []
                continue
            for file_name in files:
                abs_f = Path(root) / file_name
                rel = abs_f.relative_to(base).as_posix()
                if _is_ignored_save_payload_file(rel):
                    continue
                if _save_spec_match_relpath(spec, rel):
                    file_key = os.path.normcase(os.path.realpath(str(abs_f)))
                    if file_key in seen_files:
                        continue
                    seen_files.add(file_key)
                    yield idx, spec, abs_f, rel


def save_specs_metadata_signature(specs: list[dict]) -> tuple:
    """Return a lightweight, deterministic signature without reading file contents."""
    entries = []
    for idx, _, abs_f, rel in iter_save_spec_files(specs):
        st = abs_f.stat()
        entries.append((
            idx,
            rel,
            int(st.st_size),
            int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))),
        ))
    entries.sort(key=lambda item: (item[0], item[1].lower(), item[1]))
    return tuple(entries)


def wait_for_save_metadata_stable(
        specs: list[dict], *,
        stable_samples: int = 3,
        interval: float = 1.0,
        timeout: float = 20.0,
        stop_event: Optional[threading.Event] = None,
        signature_func=None,
        clock=None) -> Optional[tuple]:
    """Wait until consecutive metadata-only snapshots agree."""
    signature_func = signature_func or save_specs_metadata_signature
    clock = clock or time.monotonic
    required = max(2, int(stable_samples))
    interval = max(0.0, float(interval))
    deadline = clock() + max(0.0, float(timeout))
    previous = signature_func(specs)
    stable_count = 1

    while stable_count < required:
        remaining = deadline - clock()
        if remaining <= 0:
            return None
        wait_s = min(interval, remaining)
        if stop_event is not None:
            if stop_event.wait(wait_s):
                return None
        elif wait_s > 0:
            time.sleep(wait_s)
        current = signature_func(specs)
        if current == previous:
            stable_count += 1
        else:
            previous = current
            stable_count = 1
    return previous


class SaveSourceChangedError(OSError):
    """Raised when a save set changes while an archive is being created."""


def _streaming_file_hash(file_path: str, hasher) -> None:
    """以 64KB 分块读取文件并更新 hasher；读取失败时写入哨兵值。"""
    try:
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                hasher.update(chunk)
    except (OSError, PermissionError):
        hasher.update(b"__UNREADABLE__")


def compute_save_spec_snapshot(specs: list[dict]) -> dict:
    """Single-pass computation of hash, file_count, and latest_mtime for save specs.

    Replaces the previous pattern of calling compute_save_spec_hash,
    compute_save_spec_file_count, and compute_save_spec_latest_mtime
    separately (3 full directory walks) with a single traversal.
    """
    h = hashlib.md5()
    all_files = []
    latest_mtime = 0.0
    file_count = 0
    for idx, spec, abs_f, rel in iter_save_spec_files(specs):
        file_count += 1
        try:
            st = abs_f.stat()
            latest_mtime = max(latest_mtime, st.st_mtime)
            size = int(st.st_size)
            mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        except OSError:
            size = -1
            mtime_ns = -1
        all_files.append((idx, rel, str(abs_f), size, mtime_ns))
    all_files.sort(key=lambda item: (item[0], item[1].lower(), item[2].lower()))
    cache_key = tuple(
        (idx, rel, os.path.normcase(os.path.abspath(file_path)), size, mtime_ns)
        for idx, rel, file_path, size, mtime_ns in all_files
    )
    with SAVE_SPEC_SNAPSHOT_CACHE_LOCK:
        cached = SAVE_SPEC_SNAPSHOT_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)
    for idx, rel, file_path, _, _ in all_files:
        h.update(f"{idx}:{rel}".encode("utf-8", errors="ignore"))
        _streaming_file_hash(file_path, h)
    snapshot = {
        "hash": h.hexdigest(),
        "file_count": file_count,
        "latest_mtime": latest_mtime,
    }
    with SAVE_SPEC_SNAPSHOT_CACHE_LOCK:
        SAVE_SPEC_SNAPSHOT_CACHE[cache_key] = dict(snapshot)
        while len(SAVE_SPEC_SNAPSHOT_CACHE) > SAVE_SPEC_SNAPSHOT_CACHE_MAX:
            SAVE_SPEC_SNAPSHOT_CACHE.pop(next(iter(SAVE_SPEC_SNAPSHOT_CACHE)), None)
    return snapshot


def compute_save_spec_hash(specs: list[dict]) -> str:
    return compute_save_spec_snapshot(specs)["hash"]


def compute_save_spec_file_count(specs: list[dict]) -> int:
    return compute_save_spec_snapshot(specs)["file_count"]


def has_any_matching_files(specs: list[dict]) -> bool:
    """轻量检查：是否存在至少一个匹配文件，找到即返回，不做哈希计算。"""
    for _, _, _, _ in iter_save_spec_files(specs):
        return True
    return False


def compute_save_spec_latest_mtime(specs: list[dict]) -> float:
    return compute_save_spec_snapshot(specs)["latest_mtime"]


def _remove_matching_spec_files(spec: dict):
    base = Path(spec["base"])
    if not base.is_dir():
        return
    if _save_spec_covers_entire_dir(spec):
        _remove_tree_contents(base, preserve_ignored_payload_files=True)
        return
    failures = []
    for _, _, abs_f, _ in list(iter_save_spec_files([spec])):
        try:
            abs_f.unlink()
        except Exception as e:
            failures.append(f"{abs_f}: {type(e).__name__}: {e}")
    if failures:
        raise OSError("Failed to remove existing save files before restore/sync: " + "; ".join(failures[:5]))


def get_installed_game_info(steam_path: str, appid: str) -> Optional[dict]:
    appid = str(appid or "").strip()
    if not appid or not steam_path:
        return None
    norm_steam_path = os.path.normpath(steam_path)
    cached = _INSTALLED_GAME_INFO_CACHE.get(norm_steam_path)
    if cached is None:
        cached = {}
        for game in scan_installed_games(steam_path):
            game_appid = str(game.get("appid", "") or "").strip()
            if game_appid:
                cached[game_appid] = game
        _INSTALLED_GAME_INFO_CACHE[norm_steam_path] = cached
    return cached.get(appid)


def _resolve_metadata_entry_specs(entry: dict, appid: str, primary_path: str,
                                  install_dir: str, steam_path: str,
                                  library_path: str = "",
                                  preferred_accountids: Optional[list[str]] = None) -> list[dict]:
    includes = list(entry.get("includes", []))
    if not includes:
        return []
    template = str(entry.get("template", "") or "").strip()
    if not template:
        return []
    expanded_paths = expand_steamdb_template(
        template, appid, install_dir, steam_path, library_path, preferred_accountids
    )
    if not expanded_paths:
        inferred_install = infer_install_dir_from_steamdb_template(template, install_dir, library_path)
        if inferred_install:
            expanded_paths = [inferred_install]
    primary_norm = os.path.normcase(os.path.normpath(primary_path))
    resolved = []
    for path in expanded_paths:
        if os.path.normcase(os.path.normpath(path)) != primary_norm:
            continue
        resolved.append({
            "base": primary_path,
            "includes": includes,
            "recursive": entry.get("recursive", True),
        })
    return _normalize_unique_save_specs(resolved)


def _infer_precise_metadata_specs_for_game(game: Optional[dict], steam_path: str,
                                           cfg: Optional[dict] = None) -> list[dict]:
    if not isinstance(game, dict):
        return []
    appid = str(game.get("appid", "") or "").strip()
    if not appid or not steam_path:
        return []
    current_specs = get_game_save_specs(game, existing_only=False)
    if not current_specs:
        return []
    current_paths = get_game_save_paths(game, existing_only=False)
    if not current_paths:
        return []
    primary_path = current_paths[0]
    installed_game = get_installed_game_info(steam_path, appid)
    install_dir = str(installed_game.get("install_dir", "") or "").strip() if installed_game else ""
    library_path = str(
        game.get("library_path", "")
        or (installed_game.get("library_path", "") if installed_game else "")
        or ""
    ).strip()
    if not install_dir:
        return []
    if os.path.normcase(os.path.normpath(primary_path)) != os.path.normcase(os.path.normpath(install_dir)):
        return []
    candidate_specs = []
    preferred_accountids = _preferred_steam_account_ids(cfg, steam_path)
    for entry in parse_appinfo_ufs_entries(steam_path, appid):
        candidate_specs.extend(
            _resolve_metadata_entry_specs(
                entry, appid, primary_path, install_dir, steam_path, library_path, preferred_accountids
            )
        )
    if cfg and cfg.get("steamdb_detection_enabled"):
        for entry in fetch_steamdb_ufs_entries(appid):
            candidate_specs.extend(
                _resolve_metadata_entry_specs(
                    entry, appid, primary_path, install_dir, steam_path, library_path, preferred_accountids
                )
            )
    candidate_specs = _normalize_unique_save_specs(candidate_specs)
    if candidate_specs:
        return [
            spec for spec in candidate_specs
            if has_any_matching_files([spec])
        ]
    return []


def try_upgrade_game_save_specs_from_appinfo(game: Optional[dict], steam_path: str,
                                             cfg: Optional[dict] = None) -> bool:
    if not isinstance(game, dict):
        return False
    if game.get("manual_save_specs"):
        return False
    current_paths = get_game_save_paths(game, existing_only=False)
    if not current_paths:
        return False
    candidate_specs = _infer_precise_metadata_specs_for_game(game, steam_path, cfg)
    if candidate_specs:
        rebuilt_specs = list(candidate_specs)
        for extra_path in current_paths[1:]:
            rebuilt_specs.append(_default_save_spec(extra_path))
        set_game_save_specs(game, rebuilt_specs)
        return True
    return False



def _steam64_from_accountid(accountid: str) -> str:
    try:
        return str(76561197960265728 + int(str(accountid).strip()))
    except Exception:
        return ""


def _preferred_steam_account_ids(cfg: Optional[dict], steam_path: str) -> list[str]:
    if not steam_path:
        return []
    accounts = get_steam_accounts(steam_path)
    ordered = [
        str(item.get("accountid", "") or "").strip()
        for item in accounts
        if str(item.get("accountid", "") or "").strip()
    ]
    if ordered:
        return ordered
    return get_steam_user_ids(steam_path)




def _steamdb_strip_html(fragment: str) -> str:
    text = re.sub(r"(?is)<br\s*/?>", "\n", fragment or "")
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return html.unescape(text).strip()


def _extract_steamdb_path_strings(text: str) -> list[str]:
    cleaned = (text or "").replace("\r", "\n")
    results = []
    seen = set()
    for raw_line in cleaned.splitlines():
        line = raw_line.strip().strip("`").strip()
        if not line:
            continue
        pieces = [line]
        if "|" in line:
            cells = [cell.strip().strip("`").strip() for cell in line.split("|")]
            pieces = [cell for cell in cells if cell]
        for piece in pieces:
            candidate = piece
            if " likely means save files are in " in candidate.lower():
                match = re.search(r"(?i)likely means save files are in (.+?) folder", candidate)
                if match:
                    candidate = match.group(1).strip()
            candidate_lower = candidate.lower()
            if candidate_lower in {"path", "pattern", "platform", "all oses", "windows"}:
                continue
            if not any(token in candidate_lower for token in _STEAM_TEMPLATE_PATH_HINTS):
                continue
            if candidate not in seen:
                seen.add(candidate)
                results.append(candidate)
    return results


def _steamdb_cells_to_entry(cells: list[str]) -> Optional[dict]:
    path_cell = ""
    pattern_cell = ""
    for cell in cells:
        cell_clean = str(cell or "").strip().strip("`").strip()
        if not cell_clean:
            continue
        lower = cell_clean.lower()
        if not path_cell and any(token in lower for token in _STEAM_TEMPLATE_PATH_HINTS):
            path_cell = cell_clean
            continue
        if not pattern_cell and any(ch in cell_clean for ch in "*?") and not any(token in lower for token in _STEAM_TEMPLATE_PATH_HINTS):
            pattern_cell = cell_clean
    if not path_cell:
        return None
    clean_path, includes = _split_ufs_path_and_patterns(path_cell, pattern_cell)
    template = clean_path or path_cell
    recursive = not (
        includes
        and all("/" not in pattern.replace("\\", "/") for pattern in includes)
    )
    return {
        "template": template,
        "includes": includes,
        "recursive": recursive,
    }


def fetch_steamdb_ufs_entries(appid: str) -> list[dict]:
    appid = str(appid or "").strip()
    if not appid:
        return []
    with _STEAMDB_UFS_LOCK:
        cached = _STEAMDB_UFS_ENTRY_CACHE.get(appid)
        if cached is not None:
            return list(cached)

    url = f"https://steamdb.info/app/{appid}/ufs/"
    headers = {
        "User-Agent": f"{APP_NAME}/{VERSION} (+https://github.com/Kiowx/save_manager)",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(url, headers=headers)
    entries: list[dict] = []
    try:
        with _STEAMDB_UFS_SEMAPHORE:
            with urllib.request.urlopen(req, timeout=4) as resp:
                html_text = resp.read().decode("utf-8", errors="ignore")
    except Exception:
        with _STEAMDB_UFS_LOCK:
            _STEAMDB_UFS_ENTRY_CACHE[appid] = []
            _STEAMDB_UFS_CACHE[appid] = []
        return []

    rows = re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", html_text)
    for row in rows:
        cells = [_steamdb_strip_html(cell) for cell in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", row)]
        if not cells:
            continue
        row_text = " | ".join(cells).lower()
        if not any(token in row_text for token in ("windows", "all oses", "all operating systems", "all platforms")):
            continue
        entry = _steamdb_cells_to_entry(cells)
        if entry:
            entries.append(entry)

    if not entries:
        full_text = _steamdb_strip_html(html_text)
        for template in _extract_steamdb_path_strings(full_text):
            entries.append({
                "template": template,
                "includes": [],
                "recursive": True,
            })

    deduped = []
    seen = set()
    for entry in entries:
        template = str(entry.get("template", "") or "").strip()
        includes = tuple(p.lower() for p in entry.get("includes", []) if isinstance(p, str))
        recursive = bool(entry.get("recursive", True))
        key = (template, includes, recursive)
        if not template or key in seen:
            continue
        seen.add(key)
        deduped.append({
            "template": template,
            "includes": list(entry.get("includes", [])),
            "recursive": recursive,
        })

    with _STEAMDB_UFS_LOCK:
        _STEAMDB_UFS_ENTRY_CACHE[appid] = deduped
        _STEAMDB_UFS_CACHE[appid] = [item["template"] for item in deduped]
    return list(deduped)


def fetch_steamdb_ufs_templates(appid: str) -> list[str]:
    return [item["template"] for item in fetch_steamdb_ufs_entries(appid)]


def expand_steamdb_template(template: str, appid: str,
                            install_dir: str = "", steam_path: str = "",
                            library_path: str = "",
                            preferred_accountids: Optional[list[str]] = None,
                            include_missing: bool = False) -> list[str]:
    if not template:
        return []
    library_root = _get_steam_library_root(install_dir, steam_path, library_path)
    values = {
        "%USERPROFILE%": str(USER_HOME),
        "%APPDATA%": str(APPDATA),
        "%LOCALAPPDATA%": str(LOCAL_APPDATA),
        "%PROGRAMDATA%": str(PROGRAMDATA),
        "%PUBLIC%": str(PUBLIC_HOME),
        "%DOCUMENTS%": str(DOCUMENTS),
        "%MYDOCUMENTS%": str(DOCUMENTS),
        "%SAVEDGAMES%": str(SAVED_GAMES),
        "%SAVED GAMES%": str(SAVED_GAMES),
        "%LOCALLOW%": str(LOCAL_LOW),
        "[Steam Install]": steam_path or "",
        "[Steam Library]": library_root,
        "[Game Install]": install_dir or "",
        "[WinMyDocuments]": str(DOCUMENTS),
        "[WinDocuments]": str(DOCUMENTS),
        "[Documents]": str(DOCUMENTS),
        "[My Documents]": str(DOCUMENTS),
        "[WinUserProfile]": str(USER_HOME),
        "[UserProfile]": str(USER_HOME),
        "[WinAppData]": str(APPDATA),
        "[AppData]": str(APPDATA),
        "[AppDataRoaming]": str(APPDATA),
        "[WinAppDataRoaming]": str(APPDATA),
        "[LocalAppData]": str(LOCAL_APPDATA),
        "[WinAppDataLocal]": str(LOCAL_APPDATA),
        "[LocalLow]": str(LOCAL_LOW),
        "[WinAppDataLocalLow]": str(LOCAL_LOW),
        "[Saved Games]": str(SAVED_GAMES),
        "[WinSavedGames]": str(SAVED_GAMES),
        "[ProgramData]": str(PROGRAMDATA),
        "[WinProgramData]": str(PROGRAMDATA),
        "[Public]": str(PUBLIC_HOME),
        "[Public Documents]": str(PUBLIC_DOCUMENTS),
        "{AppID}": str(appid or "").strip(),
        "{appid}": str(appid or "").strip(),
    }
    normalized = template.replace("\\", os.sep).replace("/", os.sep)
    normalized = normalized.replace("%USERPROFILE%\\AppData\\LocalLow", str(LOCAL_LOW))
    normalized = normalized.replace("%USERPROFILE%/AppData/LocalLow", str(LOCAL_LOW))
    candidates = [normalized]
    for token, replacement in values.items():
        next_candidates = []
        for candidate in candidates:
            if token in candidate and replacement:
                next_candidates.append(candidate.replace(token, replacement))
            else:
                next_candidates.append(candidate)
        candidates = next_candidates
    candidates = [os.path.expandvars(candidate) for candidate in candidates]

    account_ids = get_steam_user_ids(steam_path) if steam_path else []
    preferred_accountids = [
        str(aid or "").strip()
        for aid in (preferred_accountids or [])
        if str(aid or "").strip()
    ]
    prioritized = [aid for aid in preferred_accountids if aid in account_ids]
    if prioritized:
        account_ids = prioritized + [aid for aid in account_ids if aid not in prioritized]
    expanded = []
    for candidate in candidates:
        queue_items = [candidate]
        if "{Steam3AccountID}" in candidate:
            queue_items = [candidate.replace("{Steam3AccountID}", aid) for aid in account_ids]
        if "{64BitSteamID}" in candidate:
            replaced = []
            for item in queue_items:
                if "{64BitSteamID}" in item:
                    for aid in account_ids:
                        steam64 = _steam64_from_accountid(aid)
                        if steam64:
                            replaced.append(item.replace("{64BitSteamID}", steam64))
                else:
                    replaced.append(item)
            queue_items = replaced
        expanded.extend(queue_items)

    final = []
    seen = set()
    for candidate in expanded:
        if any(token in candidate for token in ("{Steam3AccountID}", "{64BitSteamID}")):
            continue
        if re.search(r"%[^%]+%|\[[^\]]+\]|\{[^}]+\}", candidate):
            continue
        norm = os.path.normpath(candidate)
        if norm in seen:
            continue
        if not include_missing and not os.path.exists(norm):
            continue
        seen.add(norm)
        final.append(norm)
    return final


def infer_install_dir_from_steamdb_template(template: str, install_dir: str = "",
                                            library_path: str = "") -> str:
    template = str(template or "").replace("\\", "/").strip().strip("/")
    install_dir = os.path.normpath(install_dir or "")
    library_path = os.path.normpath(library_path or "")
    if not template:
        return ""

    lowered = template.lower()
    if install_dir and os.path.isdir(install_dir):
        install_name = os.path.basename(install_dir).lower()
        install_norm = install_dir.replace("\\", "/").lower().strip("/")

        if "[game install]" in lowered:
            return install_dir
        if install_name and lowered.endswith(f"/{install_name}"):
            return install_dir
        if install_name and f"/steamapps/common/{install_name}" in lowered:
            return install_dir
        if install_name and install_norm.endswith(install_name) and install_name in lowered:
            return install_dir

    if library_path and os.path.isdir(library_path) and "[steam library]" in lowered:
        rel = re.sub(r"(?i)^.*?\[steam library\]\s*", "", template).lstrip("/").strip()
        if rel:
            candidate = os.path.normpath(os.path.join(library_path, *rel.replace("\\", "/").split("/")))
            if os.path.isdir(candidate):
                return candidate
    return ""


def get_steam_userdata_roots(steam_path: str) -> list[str]:
    """收集可能存在 Steam userdata 的根目录。"""
    roots = []
    seen = set()
    steam_norm = os.path.normpath(steam_path) if steam_path else ""
    candidates = [
        os.path.join(steam_norm, "userdata") if steam_norm else "",
        steam_norm if steam_norm and os.path.basename(steam_norm).lower() == "userdata" else "",
        os.path.join(os.path.dirname(steam_norm), "userdata") if steam_norm else "",
        os.path.join(os.path.dirname(os.path.dirname(steam_norm)), "userdata") if steam_norm else "",
        os.path.join(str(LOCAL_APPDATA), "Steam", "userdata"),
        os.path.join(r"C:\Program Files (x86)\Steam", "userdata"),
        os.path.join(r"C:\Program Files\Steam", "userdata"),
    ]
    for p in candidates:
        norm = os.path.normpath(p) if p else ""
        if norm and norm not in seen and os.path.isdir(norm):
            seen.add(norm)
            roots.append(norm)

    search_bases = []
    for base in [steam_norm, os.path.dirname(steam_norm), os.path.dirname(os.path.dirname(steam_norm))]:
        norm = os.path.normpath(base) if base else ""
        if norm and os.path.isdir(norm) and norm not in search_bases:
            search_bases.append(norm)

    for base in search_bases:
        try:
            for root, dirs, _ in _walk_limited(base, max_depth=2):
                if os.path.basename(root).lower() == "userdata":
                    norm = os.path.normpath(root)
                    if norm not in seen and os.path.isdir(norm):
                        seen.add(norm)
                        roots.append(norm)
                        dirs[:] = []
        except Exception:
            continue
    return roots


def _walk_limited(base: str, max_depth: int = 6):
    """限制深度的目录遍历，避免全盘深扫过慢。"""
    if not os.path.isdir(base):
        return
    base = os.path.normpath(base)
    base_depth = base.count(os.sep)
    for root, dirs, files in os.walk(base):
        depth = os.path.normpath(root).count(os.sep) - base_depth
        if depth >= max_depth:
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.lower() not in {
                "cache", "caches", "temp", "tmp", "logs", "log",
                "crashdumps", "dumpcache", "webcache", "__pycache__",
                "node_modules", "venv", ".venv", "site-packages",
                ".git", ".svn", ".hg",
                "shader", "shaders", "shadercache", "gpucache",
                "thumbnails", "screenshots", "crash", "crashreports",
                "telemetry", "analytics",
                "_commonredist", "redist", "directx", "dotnet",
                "__macosx", "bin", "obj", ".vs", ".idea",
                "localization", "locales", "i18n",
                "video", "videos", "movies", "cinematics",
                "music", "soundtrack",
            }]
        yield root, dirs, files


def _read_account_id_from_autocloud(vdf_path: str) -> str:
    try:
        with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
            data = parse_vdf(f.read())
        return str(data.get("steam_autocloud.vdf", {}).get("accountid", "")).strip()
    except Exception:
        return ""


def discover_steam_autocloud_entries() -> list[dict]:
    """
    扫描常见本地目录中的 steam_autocloud.vdf，
    为 Auto-Cloud 游戏提供真实存档目录线索。
    """
    global _STEAM_AUTOCLOUD_CACHE
    if _STEAM_AUTOCLOUD_CACHE is not None:
        return list(_STEAM_AUTOCLOUD_CACHE)

    found = []
    seen = set()
    for base in COMMON_SAVE_BASES:
        for root, _, files in _walk_limited(str(base), max_depth=4):
            if "steam_autocloud.vdf" not in files:
                continue
            vdf_path = os.path.join(root, "steam_autocloud.vdf")
            norm = os.path.normpath(vdf_path)
            if norm in seen:
                continue
            seen.add(norm)
            accountid = _read_account_id_from_autocloud(vdf_path)
            save_root = root
            parent = os.path.dirname(root)
            if os.path.basename(root).isdigit() and parent:
                save_root = parent
            found.append({
                "file": norm,
                "accountid": accountid,
                "account_root": os.path.normpath(root),
                "save_root": os.path.normpath(save_root),
                "mtime": os.path.getmtime(norm) if os.path.exists(norm) else 0.0,
            })
    _STEAM_AUTOCLOUD_CACHE = found
    return list(found)


def _gather_candidate_file_signals(path: str, max_depth: int = 2,
                                   max_files: int = 80) -> dict:
    signals = {
        "positive_names": 0,
        "positive_exts": 0,
        "strong_names": 0,
        "weak_names": 0,
        "strong_exts": 0,
        "weak_exts": 0,
        "negative_names": 0,
        "steam_autocloud": False,
        "config_like": 0,
        "total_files": 0,
    }
    seen = 0
    for root, _, files in _walk_limited(path, max_depth=max_depth):
        for filename in files:
            seen += 1
            lower = filename.lower()
            stem, ext = os.path.splitext(lower)
            signals["total_files"] += 1
            if lower == "steam_autocloud.vdf":
                signals["steam_autocloud"] = True
            if any(hint in stem for hint in STRONG_SAVE_FILE_HINTS):
                signals["strong_names"] += 1
                signals["positive_names"] += 1
            elif any(hint in stem for hint in WEAK_SAVE_FILE_HINTS):
                signals["weak_names"] += 1
                signals["positive_names"] += 1
            if ext in STRONG_SAVE_FILE_EXTENSIONS:
                signals["strong_exts"] += 1
                signals["positive_exts"] += 1
            elif ext in WEAK_SAVE_FILE_EXTENSIONS:
                signals["weak_exts"] += 1
                signals["positive_exts"] += 1
            if any(hint in stem for hint in NEGATIVE_FILE_HINTS):
                signals["negative_names"] += 1
            if ext in {".ini", ".cfg", ".config", ".yaml", ".yml", ".toml"}:
                signals["config_like"] += 1
            if seen >= max_files:
                return signals
    return signals


def inspect_save_candidate(path: str, game_name: str = "",
                           install_dir: str = "") -> dict:
    norm = os.path.normpath(path)
    lower = norm.lower()
    leaf = os.path.basename(norm).lower()
    path_parts = [p.lower() for p in Path(norm).parts]
    normalized_parts = [_normalize_recognition_name(p) for p in path_parts]
    reasons = []
    score = 0

    if leaf in {"save", "saves", "savegame", "savegames", "savedata"}:
        score += 14
        reasons.append("save-dir")
    elif leaf.startswith("slot") or leaf.startswith("profile"):
        score += 10
        reasons.append("slot-profile")
    elif leaf.isdigit():
        score += 4
        reasons.append("account-dir")

    game_norm = _normalize_recognition_name(game_name)
    name_keywords = _extract_search_keywords(game_name)
    exact_keyword_hits = 0
    partial_keyword_hits = 0
    if game_norm and game_norm in normalized_parts:
        score += 18
        reasons.append("exact-name-part")
    if name_keywords:
        for kw in name_keywords[:5]:
            if len(kw) <= 2:
                continue
            kw_norm = _normalize_recognition_name(kw)
            if kw_norm and kw_norm in normalized_parts:
                exact_keyword_hits += 1
            elif len(kw) > 4 and any(kw in part for part in path_parts):
                partial_keyword_hits += 1
        if exact_keyword_hits >= 2:
            score += 14
            reasons.append("name-match")
        elif exact_keyword_hits == 1:
            score += 10
            reasons.append("name-match")
        elif partial_keyword_hits >= 2:
            score += 5
            reasons.append("partial-name-match")

    if install_dir:
        try:
            rel_parts = [p.lower() for p in Path(norm).relative_to(Path(install_dir)).parts]
        except Exception:
            rel_parts = []
        if rel_parts:
            for seq in ENGINE_SAVE_DIR_SEQUENCES:
                seq_len = len(seq)
                if any(tuple(rel_parts[i:i + seq_len]) == seq for i in range(len(rel_parts) - seq_len + 1)):
                    score += 10 if seq_len > 1 else 6
                    reasons.append("engine-layout")
                    break

    signals = _gather_candidate_file_signals(norm)
    strong_signal_score = min(18, signals["strong_names"] * 5 + signals["strong_exts"] * 4)
    weak_signal_score = min(6, signals["weak_names"] * 2 + signals["weak_exts"])
    positive_total = strong_signal_score + weak_signal_score
    score += positive_total
    if positive_total:
        reasons.append("save-files")
    if signals["negative_names"] >= max(3, signals["positive_names"] + signals["positive_exts"]):
        score -= 8
        reasons.append("noise-heavy")
    if (
        signals["config_like"] >= 3
        and signals["strong_names"] == 0
        and signals["strong_exts"] == 0
        and signals["weak_names"] <= 1
    ):
        score -= 6
        reasons.append("config-like")
    if (
        signals["strong_names"] == 0
        and signals["strong_exts"] == 0
        and signals["positive_names"] == 0
        and signals["weak_exts"] >= 6
    ):
        score -= 6
        reasons.append("generic-data-dir")
    if (
        leaf in {"save", "saves", "savedata", "savegames", "savegame"}
        and positive_total == 0
        and "exact-name-part" not in reasons
        and "engine-layout" not in reasons
    ):
        score -= 6
        reasons.append("generic-save-dir")

    if score >= 28:
        confidence = "high"
    elif score >= 14:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "score": score,
        "confidence": confidence,
        "reasons": reasons,
        "signals": signals,
    }


def has_install_root_save_files(path: str) -> bool:
    return bool(infer_install_root_file_specs(path))


def infer_install_root_file_specs(path: str) -> list[dict]:
    """从安装目录根文件中推断精确文件规则，避免整目录打包。"""
    if not path or not os.path.isdir(path):
        return []
    includes = []
    try:
        for item in os.scandir(path):
            if not item.is_file():
                continue
            lower = item.name.lower()
            stem, ext = os.path.splitext(lower)
            has_strong_name = any(hint in stem for hint in STRONG_SAVE_FILE_HINTS)
            if ext not in STRONG_SAVE_FILE_EXTENSIONS and not (
                has_strong_name and ext in SAVE_FILE_EXTENSIONS
            ):
                continue
            includes.append(glob.escape(item.name).replace("\\", "/"))
    except OSError:
        return []

    if not includes:
        return []
    includes.sort(key=str.lower)
    return _normalize_unique_save_specs([{
        "base": os.path.normpath(path),
        "includes": includes,
        "recursive": False,
    }])


def score_autocloud_candidate(auto: dict, remotecache_entry: Optional[dict] = None,
                              game_name: str = "", install_dir: str = "") -> int:
    score = 82
    if auto.get("accountid"):
        score += 3
    if remotecache_entry:
        if auto.get("accountid") and auto.get("accountid") == remotecache_entry.get("accountid"):
            score += 8
        if auto.get("mtime") and remotecache_entry.get("mtime"):
            time_gap = abs(float(auto["mtime"]) - float(remotecache_entry["mtime"]))
            if time_gap <= 5 * 60:
                score += 18
            elif time_gap <= 30 * 60:
                score += 10
    detail = inspect_save_candidate(auto.get("save_root", ""), game_name, install_dir)
    score += max(0, min(18, detail["score"]))
    return score


def should_accept_candidate(source: str, base_score: int, detail: dict) -> bool:
    reasons = set(detail.get("reasons", []))
    confidence = detail.get("confidence", "low")
    signals = detail.get("signals", {})
    positive_hits = int(signals.get("positive_names", 0)) + int(signals.get("positive_exts", 0))

    if source in {"confirmed", "known-path", "remotecache", "steam-autocloud", "steamdb", "appinfo"}:
        return True

    # steam-remote / steam-app-root 仅作为 userdata 目录线索，需要有实际存档信号才接受
    if source in {"steam-remote", "steam-app-root"}:
        return (
            confidence != "low"
            or "save-files" in reasons
            or positive_hits >= 1
        )

    if source == "install-root-files":
        return (
            confidence != "low"
            or int(signals.get("strong_exts", 0)) >= 1
            or int(signals.get("strong_names", 0)) >= 1
            or positive_hits >= 2
        )

    if source == "cache":
        return (
            confidence != "low"
            or "save-files" in reasons
            or "exact-name-part" in reasons
            or "steam-autocloud" in reasons
            or positive_hits >= 2
        )

    if source in {"install-dir", "registry"}:
        return (
            confidence == "high"
            or "engine-layout" in reasons
            or "save-dir" in reasons
            or "slot-profile" in reasons
            or positive_hits >= 3
        )

    if source == "system-search":
        return (
            confidence == "high"
            or ("exact-name-part" in reasons and ("save-files" in reasons or "save-dir" in reasons or "slot-profile" in reasons))
            or ("name-match" in reasons and positive_hits >= 3)
            or positive_hits >= 4
        )

    return base_score + detail.get("score", 0) >= 55


def prune_save_candidates(candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    top_score = candidates[0]["score"]
    pruned = []
    for candidate in candidates:
        keep = False
        confidence = candidate.get("confidence", "low")
        source = candidate.get("source", "")
        score = int(candidate.get("score", 0))
        if source in {"confirmed", "cache", "known-path", "remotecache", "steam-autocloud", "steamdb", "appinfo"}:
            keep = True
        elif source in {"steam-remote", "steam-app-root"}:
            # userdata 目录仅在有存档信号或高分时保留
            keep = confidence != "low" or score >= top_score - 8
        elif top_score >= 105:
            keep = score >= top_score - 12 and confidence != "low"
        elif top_score >= 90:
            keep = score >= top_score - 16 and confidence != "low"
        elif top_score >= 70:
            keep = score >= top_score - 14 and confidence == "high"
        else:
            keep = confidence != "low" and score >= 45
        if keep:
            pruned.append(candidate)

    if not pruned:
        pruned = candidates[:1]
    return pruned[:6]


def _guess_remotecache_bases(root_id: str, install_dir: str) -> list[str]:
    """
    根据 remotecache.vdf 中的 root 编号猜测本地根目录。
    猜不准时回退到常见存档根目录全尝试。
    """
    # Root ID 映射 — 与 _APPINFO_ROOT_MAP 保持一致 (Steam SDK ERemoteStorageFileRoot)
    root_map = {
        # Root 0 (Default) = userdata/remote，由 remotecache 步骤自身处理
        "1": [install_dir] if install_dir else [],              # GameInstall
        "2": [str(DOCUMENTS), str(SAVED_GAMES), str(DOCUMENTS / "My Games")],  # WinMyDocuments
        "3": [str(LOCAL_APPDATA)],                               # WinAppDataLocal
        "4": [str(APPDATA)],                                     # WinAppDataRoaming
        "9": [str(SAVED_GAMES)],                                 # WinSavedGames
        "10": [str(PROGRAMDATA)],                                # WinProgramData
        "12": [str(LOCAL_LOW)],                                  # WinAppDataLocalLow
    }
    bases = list(root_map.get(str(root_id), []))
    for base in [str(b) for b in COMMON_SAVE_BASES]:
        if base not in bases:
            bases.append(base)
    return [b for b in bases if b and os.path.isdir(b)]


def extract_local_candidates_from_remotecache(remotecache_path: str,
                                              install_dir: str = "") -> list[str]:
    """
    从 remotecache.vdf 里提取本地相对路径，并还原成候选真实存档目录。
    """
    try:
        with open(remotecache_path, "r", encoding="utf-8", errors="ignore") as f:
            data = parse_vdf(f.read())
    except Exception:
        return []

    if not isinstance(data, dict) or not data:
        return []
    top = next(iter(data.values()))
    if not isinstance(top, dict):
        return []

    candidates = []
    seen = set()
    for rel_path, meta in top.items():
        if not isinstance(meta, dict):
            continue
        root_id = str(meta.get("root", "")).strip()
        rel_norm = rel_path.replace("/", os.sep).replace("\\", os.sep)
        for base in _guess_remotecache_bases(root_id, install_dir):
            abs_path = os.path.normpath(os.path.join(base, rel_norm))
            if not os.path.isfile(abs_path):
                continue
            parent = os.path.dirname(abs_path)
            grandparent = os.path.dirname(parent)
            greatgrandparent = os.path.dirname(grandparent) if grandparent else ""
            parent_leaf = os.path.basename(parent).lower() if parent else ""
            grand_leaf = os.path.basename(grandparent).lower() if grandparent else ""
            preferred_candidates = []
            fallback_candidates = []

            # 优先使用带账号 ID 的目录，例如 Pearl Abyss/CD/save/1007873524
            if grandparent and grand_leaf.isdigit():
                preferred_candidates.append(grandparent)
            elif parent and parent_leaf.isdigit():
                preferred_candidates.append(parent)

            if grandparent and (
                parent_leaf.startswith("slot")
                or parent_leaf.startswith("profile")
                or parent_leaf.startswith("account")
            ):
                if grandparent not in preferred_candidates:
                    preferred_candidates.append(grandparent)

            if parent:
                fallback_candidates.append(parent)

            if grandparent and grandparent not in preferred_candidates:
                if (
                    parent_leaf.startswith("slot")
                    or parent_leaf.startswith("profile")
                    or parent_leaf in {"save", "saves", "savedata", "savegames", "savegame"}
                    or grand_leaf in {"save", "saves", "savedata", "savegames", "savegame"}
                ):
                    fallback_candidates.append(grandparent)

            if greatgrandparent and grandparent and (
                grand_leaf.isdigit()
                and os.path.basename(greatgrandparent).lower() in {"save", "saves", "savedata", "savegames", "savegame"}
            ):
                fallback_candidates.append(greatgrandparent)

            candidates_to_try = preferred_candidates + [
                candidate for candidate in fallback_candidates
                if candidate not in preferred_candidates
            ]
            for candidate in candidates_to_try:
                norm = os.path.normpath(candidate) if candidate else ""
                if norm and norm not in seen and os.path.isdir(norm):
                    seen.add(norm)
                    candidates.append(norm)
    return candidates


def score_remotecache_candidate(path: str) -> int:
    """对 remotecache 还原出的候选目录做层级排序。"""
    norm = os.path.normpath(path)
    leaf = os.path.basename(norm).lower()
    parent_leaf = os.path.basename(os.path.dirname(norm)).lower()
    if leaf.isdigit():
        if parent_leaf in {"save", "saves", "savedata", "savegames", "savegame"}:
            return 128
        return 122
    if leaf.startswith("slot") or leaf.startswith("profile"):
        return 108
    if leaf in {"save", "saves", "savedata", "savegames", "savegame"}:
        return 96
    return 100


def get_remotecache_entries(appid: str, steam_path: str,
                            install_dir: str = "", preferred_accountids: Optional[list[str]] = None) -> list[dict]:
    """收集某个 AppID 在各 userdata 根中的 remotecache/remote 线索。"""
    entries = []
    preferred_accountids = [
        str(aid or "").strip()
        for aid in (preferred_accountids or [])
        if str(aid or "").strip()
    ]
    for userdata_root in get_steam_userdata_roots(steam_path):
        user_ids = [d for d in os.listdir(userdata_root)
                    if os.path.isdir(os.path.join(userdata_root, d)) and d.isdigit() and d != "0"]
        prioritized = [aid for aid in preferred_accountids if aid in user_ids]
        if prioritized:
            user_ids = prioritized + [uid for uid in user_ids if uid not in prioritized]
        for uid in user_ids:
            app_root = os.path.join(userdata_root, uid, appid)
            if not os.path.isdir(app_root):
                continue
            remotecache = os.path.join(app_root, "remotecache.vdf")
            remote_dir = os.path.join(app_root, "remote")
            entries.append({
                "accountid": uid,
                "app_root": os.path.normpath(app_root),
                "remotecache": os.path.normpath(remotecache) if os.path.isfile(remotecache) else "",
                "remote_dir": os.path.normpath(remote_dir) if os.path.isdir(remote_dir) else "",
                "mtime": os.path.getmtime(remotecache) if os.path.isfile(remotecache) else 0.0,
                "local_candidates": extract_local_candidates_from_remotecache(remotecache, install_dir)
                if os.path.isfile(remotecache) else [],
            })
    return entries


def find_save_in_directory(base: str, game_name: str) -> list[str]:
    """在某个基目录下递归有限深度地通过游戏名模糊搜索存档文件夹"""
    scored: dict[str, tuple[int, int]] = {}
    if not os.path.isdir(base):
        return []
    name_lower = game_name.lower()
    name_norm = _normalize_recognition_name(game_name)
    keywords = _extract_search_keywords(game_name)
    base_norm = os.path.normpath(base)
    base_depth = base_norm.count(os.sep)
    top_hits = 0

    for root, dirs, _ in _walk_limited(base_norm, max_depth=4):
        for dirname in dirs:
            path = os.path.join(root, dirname)
            entry_lower = dirname.lower()
            entry_norm = _normalize_recognition_name(dirname)
            depth = os.path.normpath(path).count(os.sep) - base_depth
            match_score = 0
            # 防止短名游戏（如 "Rust"、"Apex"）误匹配大量无关目录
            if name_norm and entry_norm == name_norm:
                match_score = 3
            elif len(name_lower) >= 4 and len(entry_lower) >= 4 and (
                name_lower in entry_lower or entry_lower in name_lower
            ):
                # 子串匹配需要较短的一方至少达到较长方长度的 50%
                # 例: "Rust"(4) vs "RustConfig"(10) → 40% < 50% → 不匹配
                # 例: "Hades"(5) vs "Hades II"(8) → 62% > 50% → 匹配
                shorter, longer = min(len(name_lower), len(entry_lower)), max(len(name_lower), len(entry_lower))
                if shorter * 2 >= longer:
                    match_score = 3
            elif len(keywords) >= 2 and sum(1 for kw in keywords[:5] if len(kw) > 2 and kw in entry_lower) >= 2:
                match_score = 2
            elif len(keywords) >= 1 and len(keywords[0]) > 5 and (
                entry_norm == _normalize_recognition_name(keywords[0])
                or entry_lower.startswith(keywords[0])
            ):
                match_score = 1

            if match_score <= 0:
                continue

            if match_score == 3:
                top_hits += 1

            norm = os.path.normpath(path)
            prev = scored.get(norm)
            rank = (match_score, -depth)
            if prev is None or rank > prev:
                scored[norm] = rank

        if top_hits >= 5:
            break

    return [
        path for path, _ in sorted(
            scored.items(),
            key=lambda item: (-item[1][0], -item[1][1], len(item[0]), item[0].lower())
        )
    ]


def find_save_in_install_dir(install_dir: str) -> list[str]:
    """在游戏安装目录里搜索典型和引擎常见的存档文件夹"""
    results = set()
    if not install_dir or not os.path.isdir(install_dir):
        return []
    normalized_patterns = {p.replace("*", "").lower() for p in SAVE_DIR_PATTERNS}
    for root, dirs, _ in _walk_limited(install_dir, max_depth=5):
        for dirname in dirs:
            path = os.path.join(root, dirname)
            lower = dirname.lower()
            rel_parts = [p.lower() for p in Path(path).relative_to(Path(install_dir)).parts]
            if lower in normalized_patterns or any(lower.startswith(p) for p in ("slot", "profile")):
                results.add(os.path.normpath(path))
                continue
            for seq in ENGINE_SAVE_DIR_SEQUENCES:
                seq_len = len(seq)
                if any(tuple(rel_parts[i:i + seq_len]) == seq for i in range(len(rel_parts) - seq_len + 1)):
                    results.add(os.path.normpath(path))
                    break
    return sorted(results)


def detect_save_candidates(appid: str, game_name: str,
                           install_dir: str, steam_path: str,
                           library_path: str = "",
                           cfg: Optional[dict] = None) -> list[dict]:
    """
    综合检测某游戏的存档路径，返回按分数排序后的候选项。
    """
    preferred_accountids = _preferred_steam_account_ids(cfg, steam_path)
    cache_key = "||".join([
        str(appid or "").strip(),
        _normalize_recognition_name(game_name),
        os.path.normpath(install_dir or ""),
        os.path.normpath(steam_path or ""),
        os.path.normpath(library_path or ""),
        "steamdb:on" if cfg and cfg.get("steamdb_detection_enabled") else "steamdb:off",
        get_cached_recognition_path(cfg, appid, game_name),
        "|".join(sorted(get_recognition_blacklist(cfg, appid, game_name))),
    ])
    cached_candidates = _SAVE_DETECTION_CACHE.get(cache_key)
    if cached_candidates:
        valid = [
            c for c in cached_candidates
            if os.path.exists(c.get("path", "")) or not c.get("materialized", True)
        ]
        if valid:
            return [dict(c) for c in valid]

    scored: dict[str, dict] = {}
    blacklist = get_recognition_blacklist(cfg, appid, game_name)

    def _add(path: str, score: int, source: str,
             save_specs: Optional[list[dict]] = None,
             allow_missing: bool = False,
             accountid: str = ""):
        norm = os.path.normpath(path)
        exists_flag = os.path.exists(norm)
        if not norm or norm in blacklist or (not exists_flag and not allow_missing):
            return
        safe_specs, safety_issue = validate_scan_save_specs(
            appid=appid,
            selected_path=norm,
            save_specs=save_specs,
            save_paths=[norm],
            install_dir=install_dir,
            steam_path=steam_path,
            library_path=library_path,
            cfg=cfg,
        )
        if safety_issue:
            return
        if exists_flag:
            detail = inspect_save_candidate(norm, game_name, install_dir)
        else:
            detail = {
                "score": -6,
                "confidence": "medium" if source in {"steamdb", "appinfo"} else "low",
                "reasons": ["known-template", "not-materialized"],
                "signals": {},
            }
        if not should_accept_candidate(source, score, detail):
            return
        total = score + detail["score"]
        existing = scored.get(norm)
        entry = {
            "path": norm,
            "score": total,
            "source": source,
            "confidence": detail["confidence"],
            "reasons": [source] + detail["reasons"],
            "materialized": exists_flag,
        }
        if accountid:
            entry["accountid"] = str(accountid).strip()
        if safe_specs:
            entry["save_specs"] = safe_specs
        if existing is None:
            scored[norm] = entry
            return
        existing["reasons"] = list(dict.fromkeys(list(existing.get("reasons", [])) + entry["reasons"]))
        existing["materialized"] = bool(existing.get("materialized", True) or exists_flag)
        if accountid and not existing.get("accountid"):
            existing["accountid"] = str(accountid).strip()
        if safe_specs:
            existing["save_specs"] = _normalize_unique_save_specs(
                list(existing.get("save_specs", []) or []) + list(safe_specs)
            )
        if total > existing["score"]:
            existing["score"] = total
            existing["source"] = source
            existing["confidence"] = detail["confidence"]

    steamdb_enabled = bool(cfg and cfg.get("steamdb_detection_enabled"))

    confirmed_path = get_confirmed_game_path(cfg, appid, game_name)
    if confirmed_path:
        _add(confirmed_path, 108, "confirmed")
    cached_path = get_cached_recognition_path(cfg, appid, game_name)

    steamdb_candidates: dict[str, dict] = {}

    def _collect_steamdb_candidate(path: str, score: int = 92,
                                   save_specs: Optional[list[dict]] = None,
                                   allow_missing: bool = False):
        norm = os.path.normpath(path)
        exists_flag = os.path.exists(norm)
        if not norm or norm in blacklist or (not exists_flag and not allow_missing):
            return
        safe_specs, safety_issue = validate_scan_save_specs(
            appid=appid,
            selected_path=norm,
            save_specs=save_specs,
            save_paths=[norm],
            install_dir=install_dir,
            steam_path=steam_path,
            library_path=library_path,
            cfg=cfg,
        )
        if safety_issue:
            return
        if exists_flag:
            detail = inspect_save_candidate(norm, game_name, install_dir)
        else:
            detail = {
                "score": -6,
                "confidence": "medium",
                "reasons": ["known-template", "not-materialized"],
                "signals": {},
            }
        if not should_accept_candidate("steamdb", score, detail):
            return
        total = score + detail["score"]
        existing = steamdb_candidates.get(norm)
        entry = {
            "path": norm,
            "score": total,
            "source": "steamdb",
            "confidence": detail["confidence"],
            "reasons": ["steamdb"] + detail["reasons"],
            "materialized": exists_flag,
        }
        if safe_specs:
            entry["save_specs"] = safe_specs
        if existing is None:
            steamdb_candidates[norm] = entry
            return
        existing["reasons"] = list(dict.fromkeys(list(existing.get("reasons", [])) + entry["reasons"]))
        existing["materialized"] = bool(existing.get("materialized", True) or exists_flag)
        if safe_specs:
            existing["save_specs"] = _normalize_unique_save_specs(
                list(existing.get("save_specs", []) or []) + list(safe_specs)
            )
        if total > existing["score"]:
            existing["score"] = total
            existing["confidence"] = detail["confidence"]

    appinfo_root0_entries = []

    # 1a) 本地 appinfo.vdf UFS 线索（离线，无需联网）
    if str(appid or "").strip() and steam_path:
        for appinfo_entry in parse_appinfo_ufs_entries(steam_path, appid):
            if appinfo_entry.get("root_id") == "0":
                appinfo_root0_entries.append(appinfo_entry)
                continue
            template = appinfo_entry.get("template", "")
            for path in expand_steamdb_template(
                template, appid, install_dir, steam_path, library_path, preferred_accountids, include_missing=True
            ):
                save_specs = [{
                    "base": path,
                    "includes": list(appinfo_entry.get("includes", [])),
                    "recursive": appinfo_entry.get("recursive", True),
                }]
                _add(path, 94, "appinfo", save_specs=save_specs, allow_missing=True)

    inferred_root_specs = infer_install_root_file_specs(install_dir) if install_dir else []
    if inferred_root_specs:
        _add(
            install_dir,
            96 if steamdb_enabled else 90,
            "install-root-files",
            save_specs=inferred_root_specs,
        )

    # 1b) SteamDB Cloud Save / UFS 线索（可选，需联网，优先模式）
    if steamdb_enabled and str(appid or "").strip():
        for steamdb_entry in fetch_steamdb_ufs_entries(appid):
            template = steamdb_entry.get("template", "")
            expanded_paths = expand_steamdb_template(
                template, appid, install_dir, steam_path, library_path, preferred_accountids, include_missing=True
            )
            if not expanded_paths:
                inferred_install = infer_install_dir_from_steamdb_template(template, install_dir, library_path)
                if inferred_install:
                    expanded_paths = [inferred_install]
            for path in expanded_paths:
                save_specs = [{
                    "base": path,
                    "includes": list(steamdb_entry.get("includes", [])),
                    "recursive": steamdb_entry.get("recursive", True),
                }]
                _collect_steamdb_candidate(path, save_specs=save_specs, allow_missing=True)

        for item in steamdb_candidates.values():
            existing = scored.get(item["path"])
            if existing is None:
                scored[item["path"]] = dict(item)
                continue
            existing["reasons"] = list(dict.fromkeys(list(existing.get("reasons", [])) + list(item.get("reasons", []))))
            existing["materialized"] = bool(existing.get("materialized", True) or item.get("materialized", True))
            if item.get("save_specs"):
                existing["save_specs"] = _normalize_unique_save_specs(
                    list(existing.get("save_specs", []) or []) + list(item.get("save_specs", []) or [])
                )
            if item["score"] > existing["score"]:
                existing["score"] = item["score"]
                existing["source"] = "steamdb"
                existing["confidence"] = item.get("confidence", existing.get("confidence", "low"))

    if cached_path:
        _add(cached_path, 104, "cache")

    # 2) 内置数据库
    if appid in KNOWN_SAVE_PATHS:
        for tmpl in KNOWN_SAVE_PATHS[appid]:
            p = expand_path(tmpl, install_dir)
            if "__NO_INSTALL__" not in p and os.path.exists(p):
                _add(p, 92, "known-path")

    # 3) 系统常见目录关键词搜索
    # 本地识别优先依赖游戏名关键词，而不是 Auto-Cloud 元数据。
    for base in COMMON_SAVE_BASES:
        for path in find_save_in_directory(str(base), game_name):
            _add(path, 78, "system-search")

    # 4) remotecache.vdf 联动线索 + Steam userdata/<uid>/<appid>/remote
    remotecache_entries = get_remotecache_entries(appid, steam_path, install_dir, preferred_accountids)
    for entry in remotecache_entries:
        local_candidates = entry.get("local_candidates", [])
        has_local_candidates = bool(local_candidates)
        for path in local_candidates:
            _add(path, score_remotecache_candidate(path), "remotecache", accountid=entry.get("accountid", ""))
        if entry["remote_dir"]:
            for root0_entry in appinfo_root0_entries:
                relative_path = str(root0_entry.get("relative_path", "") or "").strip()
                root0_path = os.path.normpath(
                    os.path.join(entry["remote_dir"], relative_path)
                    if relative_path else entry["remote_dir"]
                )
                if not _path_is_within(root0_path, entry["remote_dir"]):
                    continue
                root0_specs = [{
                    "base": root0_path,
                    "includes": list(root0_entry.get("includes", [])),
                    "recursive": root0_entry.get("recursive", True),
                }]
                _add(
                    root0_path,
                    96,
                    "appinfo",
                    save_specs=root0_specs,
                    accountid=entry.get("accountid", ""),
                )
            # 如果已还原出真实本地路径，remote 目录只作低优先级备选
            # 如果没有本地候选，remote 目录可能是 API 型游戏的真实存档位置
            remote_score = 78 if has_local_candidates else 108
            _add(entry["remote_dir"], remote_score, "steam-remote", accountid=entry.get("accountid", ""))
        # app_root (含 remotecache.vdf 的目录) 仅作低优先级参考
        _add(entry["app_root"], 68, "steam-app-root", accountid=entry.get("accountid", ""))

    # 6) 安装目录搜索
    for path in find_save_in_install_dir(install_dir):
        _add(path, 60, "install-dir")

    # 7) 注册表发现的安装路径
    for reg_dir in _detect_install_paths_from_registry(game_name, appid):
        if reg_dir and os.path.normpath(reg_dir) != os.path.normpath(install_dir or ""):
            for path in find_save_in_install_dir(reg_dir):
                _add(path, 58, "registry")

    all_candidates = sorted(
        scored.values(),
        key=lambda item: (-item["score"], len(item["path"]), item["path"].lower())
    )
    preferred_candidates = prune_save_candidates(all_candidates)
    preferred_paths = {c["path"] for c in preferred_candidates}
    candidates = list(preferred_candidates)
    candidates.extend(c for c in all_candidates if c["path"] not in preferred_paths)
    _SAVE_DETECTION_CACHE[cache_key] = [dict(c) for c in candidates[:20]]
    return candidates[:20]


def detect_save_paths(appid: str, game_name: str,
                      install_dir: str, steam_path: str,
                      library_path: str = "",
                      cfg: Optional[dict] = None) -> list[str]:
    return [c["path"] for c in detect_save_candidates(appid, game_name, install_dir, steam_path, library_path, cfg)]


# ══════════════════════════════════════════════
#  配置管理
# ══════════════════════════════════════════════

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    changed = False
    defaults = {
        "steam_path": "",
        "games": [],
        "theme": "light",
        "language": "",
        "auto_backup_enabled": False,
        "auto_backup_interval": 30,
        "watch_enabled": False,
        "watch_cooldown": 60,
        "autostart": False,
        "autostart_minimize": True,
        "sync_enabled": False,
        "sync_folder": "",
        "sync_folder_source": "",
        "sync_interval": 10,
        "sync_mode": "smart",
        "sync_archive_keep": 3,
        "steamdb_detection_enabled": True,
        "sync_state": {},
        "sync_retry_queue": [],
        "recognition_cache": {},
        "recognition_excludes": {},
        "minimize_to_tray": True,
        "sync_notify": True,
        "webdav_enabled": False,
        "webdav_preset": "generic",
        "webdav_url": "",
        "webdav_base_path": "/SteamSaveSync",
        "webdav_username": "",
        "webdav_password": "",
        "webdav_verify_ssl": True,
        "max_backups_per_game": 20,
        "max_backup_size_gb": 10.0,
        "games_page_size": 25,
        "backup_page_size": 30,
        "backup_path": "",
    }
    if CONFIG_FILE.exists():
        try:
            cfg = load_json_file_tolerant(CONFIG_FILE, default={}) or {}
            # 用 defaults 补齐缺失的键
            for k, v in defaults.items():
                if k not in cfg:
                    cfg[k] = v
                    changed = True
        except Exception:
            cfg = defaults.copy()
    else:
        cfg = defaults.copy()
        changed = True

    if "sync_folder_source" not in cfg:
        cfg["sync_folder_source"] = "manual" if str(cfg.get("sync_folder", "") or "").strip() else ""
        changed = True

    for legacy_key in ("steam_account_id", "steam_account_ids"):
        if legacy_key in cfg:
            cfg.pop(legacy_key, None)
            changed = True

    games = cfg.get("games", [])
    if isinstance(games, list):
        for game in games:
            if not isinstance(game, dict):
                continue
            if game.get("game_uid") and not game.get("storage_key"):
                ensure_game_storage_identity(game)
                changed = True
            old_primary = game.get("save_path", "")
            old_specs = list(game.get("save_specs", [])) if isinstance(game.get("save_specs", []), list) else []
            old_monitor = list(game.get("monitor_processes", [])) if isinstance(game.get("monitor_processes", []), list) else game.get("monitor_processes", "")
            old_storage_specs = _normalize_unique_save_specs(old_specs, for_storage=True)
            if "auto_backup" not in game:
                game["auto_backup"] = True
                changed = True
            if "watch_backup" not in game:
                game["watch_backup"] = bool(game.get("auto_backup", True))
                changed = True
            if "backup_on_exit" not in game:
                game["backup_on_exit"] = False
                changed = True
            if "sync_enabled" not in game:
                game["sync_enabled"] = True
                changed = True
            if "favorite" not in game:
                game["favorite"] = False
                changed = True
            normalized_specs = get_game_save_specs(game, existing_only=False)
            set_game_save_specs(game, normalized_specs)
            set_game_monitor_processes(game, old_monitor)
            if (
                game.get("save_path", "") != old_primary
                or "save_paths" not in game
                or old_storage_specs != game.get("save_specs", [])
                or old_monitor != game.get("monitor_processes", [])
            ):
                changed = True

    detected_lang = normalize_language(cfg.get("language") or detect_system_language())
    if cfg.get("language") != detected_lang:
        cfg["language"] = detected_lang
        changed = True
    actual_autostart = get_autostart_enabled()
    if cfg.get("autostart") != actual_autostart:
        cfg["autostart"] = actual_autostart
        changed = True
    # 如果 steam_path 为空或不存在，自动重新检测
    if not cfg.get("steam_path") or not os.path.isdir(cfg["steam_path"]):
        detected = detect_steam_path()
        if detected:
            cfg["steam_path"] = detected
            changed = True
    steam_path_for_upgrade = cfg.get("steam_path", "")
    if isinstance(games, list) and steam_path_for_upgrade:
        for game in games:
            if not isinstance(game, dict):
                continue
            if try_upgrade_game_save_specs_from_appinfo(game, steam_path_for_upgrade, cfg):
                changed = True
    sync_folder_value = str(cfg.get("sync_folder", "") or "").strip()
    sync_folder_source = str(cfg.get("sync_folder_source", "") or "").strip().lower()
    # 仅在首次使用或上次为自动探测来源时，才自动补回同步文件夹。
    if (not sync_folder_value or not os.path.isdir(sync_folder_value)) and sync_folder_source != "manual":
        cloud = detect_cloud_folder()
        if cloud:
            cfg["sync_folder"] = cloud
            cfg["sync_folder_source"] = "auto"
            changed = True
    # 自定义备份路径
    global BACKUP_ROOT
    custom_bp = cfg.get("backup_path", "").strip()
    if custom_bp:
        BACKUP_ROOT = Path(custom_bp)
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    if changed:
        save_config(cfg)
    return cfg


def save_config(cfg: dict):
    # 如果当前线程处于批量写入上下文，仅标记脏位，延迟实际写盘。
    # 使用 thread-local 状态，避免后台同步线程和 UI 线程互相吞掉保存。
    if _config_batch_depth() > 0:
        _CONFIG_BATCH_STATE.dirty = True
        _CONFIG_BATCH_STATE.cfg_ref = cfg
        return
    _save_config_now(cfg)


def _save_config_now(cfg: dict):
    ensure_dirs()
    data = json.dumps(cfg, ensure_ascii=False, indent=2)
    tmp_path = CONFIG_FILE.with_name(
        f"{CONFIG_FILE.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    with CONFIG_SAVE_LOCK:
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CONFIG_FILE)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


class batch_config_save:
    """上下文管理器：将多次 save_config 调用合并为单次写盘。

    用法::

        with batch_config_save():
            set_game_sync_state(cfg, game, ...)    # 标记脏位但不写盘
            clear_game_sync_conflict(cfg, game)    # 标记脏位但不写盘
            clear_sync_retry(cfg, game)            # 标记脏位但不写盘
        # 退出 with 块时统一写一次
    """

    def __enter__(self):
        _CONFIG_BATCH_STATE.depth = _config_batch_depth() + 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        depth = max(0, _config_batch_depth() - 1)
        _CONFIG_BATCH_STATE.depth = depth
        if depth == 0 and bool(getattr(_CONFIG_BATCH_STATE, "dirty", False)):
            cfg_ref = getattr(_CONFIG_BATCH_STATE, "cfg_ref", None)
            _CONFIG_BATCH_STATE.dirty = False
            _CONFIG_BATCH_STATE.cfg_ref = None
            if cfg_ref is not None:
                _save_config_now(cfg_ref)
        return False  # 不抑制异常


def clear_startup_caches(cfg: Optional[dict] = None):
    global _STEAM_AUTOCLOUD_CACHE, _INSTALLED_GAME_INFO_CACHE

    _STEAM_AUTOCLOUD_CACHE = None
    with _STORAGE_KIND_CACHE_LOCK:
        _STORAGE_KIND_CACHE.clear()
    with _SAVE_DETECTION_CACHE_LOCK:
        _SAVE_DETECTION_CACHE.clear()
    _STEAMDB_UFS_CACHE.clear()
    _STEAMDB_UFS_ENTRY_CACHE.clear()
    _APPINFO_CACHE.reset()
    with _INSTALLED_GAME_INFO_CACHE_LOCK:
        _INSTALLED_GAME_INFO_CACHE.clear()
    with _REGISTRY_INSTALL_CACHE_LOCK:
        _REGISTRY_INSTALL_CACHE.clear()

    if cfg is not None:
        cache = cfg.get("recognition_cache")
        if isinstance(cache, dict) and cache:
            cfg["recognition_cache"] = {}
            save_config(cfg)


# ══════════════════════════════════════════════
#  备份/还原核心
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
#  存档同步核心
# ══════════════════════════════════════════════

def detect_cloud_folder() -> str:
    """
    自动检测本地云盘同步文件夹，按优先级返回第一个找到的路径。
    支持：OneDrive、Dropbox、Google Drive、iCloud、坚果云
    """
    candidates = []

    # ─── OneDrive ───
    # 1) 环境变量（最可靠）
    for env_key in ["OneDrive", "OneDriveConsumer", "OneDriveCommercial"]:
        p = os.environ.get(env_key, "")
        if p and os.path.isdir(p):
            candidates.append(("OneDrive", p))
    # 2) 注册表
    if winreg:
        for reg_path in [
            r"SOFTWARE\Microsoft\OneDrive",
            r"SOFTWARE\Microsoft\OneDrive\Accounts\Personal",
        ]:
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_path, 0, winreg.KEY_READ)
                val, _ = winreg.QueryValueEx(key, "UserFolder")
                winreg.CloseKey(key)
                if val and os.path.isdir(val):
                    candidates.append(("OneDrive", val))
            except Exception:
                pass
    # 3) 常见路径
    for name in ["OneDrive", "OneDrive - Personal"]:
        p = str(USER_HOME / name)
        if os.path.isdir(p):
            candidates.append(("OneDrive", p))

    # ─── Dropbox ───
    # 1) Dropbox info.json
    for base in [APPDATA, LOCAL_APPDATA]:
        info_json = base / "Dropbox" / "info.json"
        if info_json.is_file():
            try:
                with open(info_json, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for acct in ["personal", "business"]:
                    if acct in data and "path" in data[acct]:
                        dp = data[acct]["path"]
                        if os.path.isdir(dp):
                            candidates.append(("Dropbox", dp))
            except Exception:
                pass
    # 2) 常见路径
    for name in ["Dropbox"]:
        p = str(USER_HOME / name)
        if os.path.isdir(p):
            candidates.append(("Dropbox", p))

    # ─── Google Drive ───
    # Google Drive for Desktop 默认挂载为虚拟盘符或 ~/Google Drive
    for name in ["Google Drive", "My Drive", "GoogleDrive"]:
        p = str(USER_HOME / name)
        if os.path.isdir(p):
            candidates.append(("Google Drive", p))
    # Windows: Google Drive 虚拟盘符 (G:\My Drive 等)
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            for sub in ["My Drive", "我的云端硬盘"]:
                gd = f"{letter}:\\{sub}"
                if os.path.isdir(gd):
                    candidates.append(("Google Drive", gd))
                    break

    # ─── iCloud (Windows) ───
    icloud = USER_HOME / "iCloudDrive"
    if icloud.is_dir():
        candidates.append(("iCloud", str(icloud)))

    # ─── 坚果云 (Nutstore) ───
    for name in ["Nutstore", "Nutstore Files", "坚果云"]:
        p = str(USER_HOME / name)
        if os.path.isdir(p):
            candidates.append(("坚果云", p))

    # 去重，返回第一个
    seen = set()
    for provider, path in candidates:
        norm = os.path.normpath(path)
        if norm not in seen:
            seen.add(norm)
            return norm
    return ""


def send_desktop_notification(title: str, message: str):
    """
    发送 Windows 10/11 桌面 Toast 通知（右下角弹窗）。
    方案 A：PowerShell WinRT Toast API（Windows 10/11 原生，最可靠）
    方案 B：PowerShell NotifyIcon + Application.DoEvents 消息泵
    方案 C（回退）：pystray.Icon.notify()
    不需要管理员权限。
    """
    if sys.platform != "win32":
        return

    def _try_toast() -> bool:
        """使用 Windows Runtime Toast 通知 API（Windows 10+）"""
        try:
            import subprocess as _sp
            # & 转义必须在其他 XML 实体替换之前执行
            t = title.replace('&', '&amp;').replace("'", "''").replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;')
            m = message.replace('&', '&amp;').replace("'", "''").replace('"', '&quot;').replace('<', '&lt;').replace('>', '&gt;').replace("\n", " ")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null; "
                "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime] | Out-Null; "
                f"$xml = '<toast><visual><binding template=\"ToastGeneric\"><text>{t}</text><text>{m}</text></binding></visual></toast>'; "
                "$doc = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                "$doc.LoadXml($xml); "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($doc); "
                "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Steam Save Manager'); "
                "$notifier.Show($toast)"
            )
            result = _sp.run(
                ["powershell", "-ExecutionPolicy", "Bypass",
                 "-NoProfile", "-WindowStyle", "Hidden",
                 "-Command", ps],
                creationflags=0x08000000,
                stdout=_sp.DEVNULL, stderr=_sp.PIPE,
                timeout=10,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _try_notifyicon() -> bool:
        """使用 NotifyIcon + DoEvents 消息泵（兼容旧系统）"""
        try:
            import subprocess as _sp
            t = title.replace("'", "''").replace('"', '`"')
            m = message.replace("'", "''").replace('"', '`"').replace("\n", " ")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms; "
                "Add-Type -AssemblyName System.Drawing; "
                "$n = New-Object System.Windows.Forms.NotifyIcon; "
                "$n.Icon = [System.Drawing.SystemIcons]::Information; "
                "$n.BalloonTipIcon = 'Info'; "
                f"$n.BalloonTipTitle = '{t}'; "
                f"$n.BalloonTipText = '{m}'; "
                "$n.Visible = $true; "
                "$n.ShowBalloonTip(5000); "
                "$end = (Get-Date).AddSeconds(6); "
                "while ((Get-Date) -lt $end) { [System.Windows.Forms.Application]::DoEvents(); Start-Sleep -Milliseconds 100 }; "
                "$n.Dispose()"
            )
            _sp.Popen(
                ["powershell", "-ExecutionPolicy", "Bypass",
                 "-NoProfile", "-WindowStyle", "Hidden",
                 "-Command", ps],
                creationflags=0x08000000,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
            )
            return True
        except Exception:
            return False

    def _try_pystray() -> bool:
        if not HAS_TRAY:
            return False
        try:
            from PIL import ImageDraw as _IDraw
            _img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
            _d = _IDraw.Draw(_img)
            _d.rounded_rectangle([0, 0, 63, 63], radius=12,
                                 fill=(99, 102, 241, 255))
            _d.rounded_rectangle([14, 22, 50, 42], radius=6,
                                 fill=(255, 255, 255, 255))
            def _run():
                try:
                    import time as _t
                    _ic = pystray.Icon("ssm_notify", _img, APP_NAME)
                    def _setup(ic):
                        ic.visible = True
                        _t.sleep(0.3)
                        ic.notify(message, title)
                        _t.sleep(6)
                        ic.stop()
                    _ic.run(setup=_setup)
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()
            return True
        except Exception:
            return False

    # 按优先级尝试：Toast API → NotifyIcon+DoEvents → pystray
    if not _try_toast():
        if not _try_notifyicon():
            _try_pystray()


def get_game_sync_folder_key(game_ref) -> str:
    """跨设备稳定的同步目录名，不带本机生成的 game_uid 后缀。"""
    appid = ""
    if isinstance(game_ref, dict):
        name = str(game_ref.get("name", "") or "").strip()
        appid = str(game_ref.get("appid", "") or "").strip()
    else:
        name = str(game_ref or "").strip()
    appid_key = re.sub(r"[^A-Za-z0-9._-]+", "_", appid).strip("._-")
    if appid_key:
        return f"appid_{appid_key}"
    key = sanitize(name).strip()
    if key:
        return key
    return "game"


def get_game_legacy_name_sync_folder_key(game_ref) -> str:
    if isinstance(game_ref, dict):
        name = str(game_ref.get("name", "") or "").strip()
    else:
        name = str(game_ref or "").strip()
    return sanitize(name).strip() or "game"


def get_sync_game_dir(sync_folder: str, game_ref) -> Path:
    """获取同步目标中该游戏的跨设备共享子文件夹"""
    return Path(sync_folder) / "SteamSaveSync" / get_game_sync_folder_key(game_ref)


def _is_legacy_device_sync_key(shared_key: str, candidate_key: str) -> bool:
    shared = str(shared_key or "").strip()
    candidate = str(candidate_key or "").strip()
    if not shared or not candidate:
        return False
    suffixes = []
    for prefix in (shared, _webdav_safe_segment(shared)):
        if prefix and candidate.startswith(f"{prefix}__"):
            suffixes.append(candidate[len(prefix) + 2:])
    return any(re.fullmatch(r"[0-9a-fA-F]{8}", suffix or "") for suffix in suffixes)


def _legacy_sync_name_prefixes(game_ref) -> list[str]:
    prefixes = []
    legacy_name_key = get_game_legacy_name_sync_folder_key(game_ref)
    for key in (legacy_name_key,):
        if key and key not in prefixes:
            prefixes.append(key)
    safe_legacy_name_key = _webdav_safe_segment(legacy_name_key)
    if safe_legacy_name_key and (safe_legacy_name_key == legacy_name_key or len(safe_legacy_name_key) >= 4):
        if safe_legacy_name_key not in prefixes:
            prefixes.append(safe_legacy_name_key)
    storage_key = get_game_storage_key(game_ref)
    for key in (storage_key, _webdav_safe_segment(storage_key)):
        raw = str(key or "").strip()
        match = re.fullmatch(r"(.+)__([0-9a-fA-F]{8})", raw)
        if match:
            prefix = match.group(1)
            if prefix and prefix not in prefixes:
                prefixes.append(prefix)
    return prefixes


def get_legacy_sync_game_dirs(sync_folder: str, game_ref) -> list[Path]:
    root = Path(sync_folder) / "SteamSaveSync"
    shared_dir = get_sync_game_dir(sync_folder, game_ref)
    candidates = []
    shared_key = get_game_sync_folder_key(game_ref)
    name_prefixes = _legacy_sync_name_prefixes(game_ref)
    keys = []
    for key in (get_game_storage_key(game_ref), _webdav_safe_segment(get_game_storage_key(game_ref))):
        if any(_is_legacy_device_sync_key(prefix, key) for prefix in name_prefixes):
            keys.append(key)
    for key in name_prefixes:
        if key and key != shared_key:
            keys.append(key)
    try:
        if root.is_dir() and name_prefixes:
            for item in root.iterdir():
                if item.is_dir() and any(_is_legacy_device_sync_key(prefix, item.name) for prefix in name_prefixes):
                    keys.append(item.name)
    except Exception as e:
        logger.debug("扫描旧同步目录失败 %s: %s", root, e)
    for key in keys:
        if not key:
            continue
        legacy_dir = root / key
        try:
            if legacy_dir.resolve(strict=False) == shared_dir.resolve(strict=False):
                continue
        except Exception:
            if os.path.normcase(os.path.normpath(str(legacy_dir))) == os.path.normcase(os.path.normpath(str(shared_dir))):
                continue
        if legacy_dir not in candidates:
            candidates.append(legacy_dir)
    return candidates


def _merge_sync_game_dir_contents(src: Path, dst: Path):
    if not src.is_dir():
        return
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        target_dir = dst if rel == "." else dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for dir_name in dirs:
            (target_dir / dir_name).mkdir(parents=True, exist_ok=True)
        for file_name in files:
            src_file = Path(root) / file_name
            dst_file = target_dir / file_name
            try:
                if dst_file.exists() and dst_file.stat().st_mtime >= src_file.stat().st_mtime:
                    continue
                shutil.copy2(src_file, dst_file)
            except Exception as e:
                logger.debug("迁移旧同步目录文件失败 %s -> %s: %s", src_file, dst_file, e)


def ensure_shared_sync_game_dir(sync_folder: str, game_ref) -> Path:
    shared_dir = get_sync_game_dir(sync_folder, game_ref)
    for legacy_dir in get_legacy_sync_game_dirs(sync_folder, game_ref):
        if legacy_dir.exists():
            _merge_sync_game_dir_contents(legacy_dir, shared_dir)
    return shared_dir


def get_sync_archive_root(sync_game_dir: Path) -> Path:
    return sync_game_dir / "archives"


def get_sync_archive_meta_path(archive_path: Path) -> Path:
    return Path(str(archive_path) + ".meta.json")


def _safe_file_stat(path: Path | str):
    try:
        return Path(path).stat()
    except Exception as e:
        logger.debug("无法读取文件状态 %s: %s", path, e)
        return None


def _iter_local_sync_archives(archive_root: Path) -> list[Path]:
    try:
        candidates = list(archive_root.rglob("*.zip"))
    except Exception as e:
        logger.debug("扫描同步归档目录失败 %s: %s", archive_root, e)
        return []
    archives = []
    for candidate in candidates:
        try:
            if candidate.is_dir():
                continue
        except Exception:
            pass
        archives.append(candidate)
    return archives


def _sync_archive_sort_key(path: Path) -> tuple:
    st = _safe_file_stat(path)
    mtime = float(st.st_mtime) if st else 0.0
    return (mtime, path.name.lower())


def compute_file_sha256(file_path: Path | str) -> str:
    hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower()


def validate_zip_archive(zip_path: Path | str) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            bad_member = zf.testzip()
        if bad_member:
            return False, f"zip_crc_failed:{bad_member}"
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _validate_zip_extract_limits(zf: zipfile.ZipFile, context: str = "archive"):
    file_count = 0
    total_size = 0
    for info in zf.infolist():
        if not info.filename or info.is_dir() or _is_ignored_save_payload_file(info.filename):
            continue
        file_count += 1
        if file_count > ZIP_EXTRACT_MAX_FILES:
            raise ValueError(
                f"{context} contains too many files: {file_count} > {ZIP_EXTRACT_MAX_FILES}"
            )
        member_size = max(0, int(info.file_size or 0))
        if member_size > ZIP_EXTRACT_MAX_MEMBER_BYTES:
            raise ValueError(
                f"{context} member is too large: {info.filename} ({member_size} bytes)"
            )
        total_size += member_size
        if total_size > ZIP_EXTRACT_MAX_TOTAL_BYTES:
            raise ValueError(
                f"{context} expands to too much data: {total_size} > {ZIP_EXTRACT_MAX_TOTAL_BYTES}"
            )


def enforce_sync_archive_limits(sync_game_dir: Path, keep_count: int = 3):
    if keep_count <= 0:
        return
    archive_root = get_sync_archive_root(sync_game_dir)
    if not archive_root.is_dir():
        return
    archives = sorted(
        _iter_local_sync_archives(archive_root),
        key=_sync_archive_sort_key,
        reverse=True,
    )
    for archive_path in archives[keep_count:]:
        meta_path = get_sync_archive_meta_path(archive_path)
        try:
            archive_path.unlink()
        except Exception:
            pass
        try:
            if meta_path.exists():
                meta_path.unlink()
        except Exception:
            pass
    for folder in sorted(archive_root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if folder.is_dir():
            try:
                next(folder.iterdir())
            except StopIteration:
                try:
                    folder.rmdir()
                except Exception:
                    pass
            except Exception:
                pass


def enforce_all_sync_archive_limits(cfg: Optional[dict]):
    if not cfg:
        return
    sync_folder = get_effective_sync_root(str(cfg.get("sync_folder", "") or "").strip(), cfg, ensure=True)
    if not sync_folder:
        return
    try:
        keep_count = max(0, int(cfg.get("sync_archive_keep", 3)))
    except Exception:
        keep_count = 3
    games = cfg.get("games", [])
    if not isinstance(games, list):
        return
    seen_dirs = set()
    for game in games:
        if not isinstance(game, dict):
            continue
        game_name = str(game.get("name", "") or "").strip()
        if not game_name:
            continue
        sync_game_dir = get_sync_game_dir(sync_folder, game)
        dir_key = str(sync_game_dir).lower()
        if dir_key in seen_dirs:
            continue
        seen_dirs.add(dir_key)
        enforce_sync_archive_limits(sync_game_dir, keep_count)
        if cfg.get("webdav_enabled") and HAS_WEBDAV:
            try:
                webdav_enforce_archive_limits(cfg, game, keep_count)
            except Exception:
                pass


def _remove_tree_contents(path: Path, preserve_ignored_payload_files: bool = False):
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)
        return
    failures = []
    if preserve_ignored_payload_files:
        for root, dirs, files in os.walk(path, topdown=False):
            root_path = Path(root)
            rel_root = os.path.relpath(root, path)
            for file_name in files:
                file_path = root_path / file_name
                rel = file_name if rel_root == "." else str(PurePosixPath(rel_root.replace("\\", "/")) / file_name)
                if _is_ignored_save_payload_file(rel):
                    continue
                try:
                    file_path.unlink()
                except Exception as e:
                    failures.append(f"{file_path}: {type(e).__name__}: {e}")
            for dir_name in dirs:
                dir_path = root_path / dir_name
                try:
                    dir_path.rmdir()
                except OSError:
                    pass
                except Exception as e:
                    failures.append(f"{dir_path}: {type(e).__name__}: {e}")
        if failures:
            raise OSError("Failed to remove existing save directory contents before restore/sync: " + "; ".join(failures[:5]))
        return
    for item in path.iterdir():
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except Exception as e:
            failures.append(f"{item}: {type(e).__name__}: {e}")
    if failures:
        raise OSError("Failed to remove existing save directory contents before restore/sync: " + "; ".join(failures[:5]))


def _iter_sync_payload_files(save_specs: list[dict]):
    multi = len(save_specs) > 1
    for idx, _, abs_f, rel in iter_save_spec_files(save_specs):
        prefix = Path("_paths") / f"p{idx}" if multi else Path()
        arcname = prefix / Path(rel)
        yield idx, abs_f, arcname.as_posix()


def create_sync_archive(game: dict, sync_game_dir: Path,
                        save_specs: list[dict], snapshot: dict,
                        keep_count: int = 3) -> tuple[Path, Path]:
    now = datetime.datetime.now()
    month_dir = get_sync_archive_root(sync_game_dir) / now.strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    archive_path = month_dir / f"{now.strftime('%Y%m%d_%H%M%S_%f')}.zip"
    temp_archive_path = _atomic_zip_path(archive_path)
    file_count = 0
    try:
        with _open_zip_for_write(temp_archive_path) as archive:
            for _, abs_f, arcname in _iter_sync_payload_files(save_specs):
                _zip_write_file(archive, abs_f, arcname)
                file_count += 1
        if file_count <= 0:
            raise FileNotFoundError("No readable save files were found for sync archive")
        temp_archive_path.replace(archive_path)
    except Exception:
        try:
            temp_archive_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    archive_size = int(archive_path.stat().st_size) if archive_path.exists() else 0
    archive_sha256 = compute_file_sha256(archive_path) if archive_size else ""
    meta = {
        "version": 1,
        "game": game.get("name", ""),
        "appid": str(game.get("appid", "") or "").strip(),
        "created_at": now.isoformat(timespec="seconds"),
        "timestamp": now.timestamp(),
        "hash": snapshot.get("hash", ""),
        "file_count": snapshot.get("file_count", 0),
        "latest_mtime": snapshot.get("latest_mtime", 0.0),
        "path_count": len(save_specs),
        "paths": [os.path.normpath(spec["base"]) for spec in save_specs],
        "save_specs": _normalize_unique_save_specs(save_specs, for_storage=True),
        "manual_save_specs": bool(game.get("manual_save_specs", False)),
        "archive_size": archive_size,
        "archive_sha256": archive_sha256,
    }
    meta_path = get_sync_archive_meta_path(archive_path)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    try:
        enforce_sync_archive_limits(sync_game_dir, keep_count)
    except Exception as e:
        logger.debug("清理旧同步归档失败 %s: %s", sync_game_dir, e)
    return archive_path, meta_path


def get_latest_sync_archive(sync_game_dir: Path) -> Optional[dict]:
    archive_root = get_sync_archive_root(sync_game_dir)
    if not archive_root.is_dir():
        return None
    archives = sorted(
        _iter_local_sync_archives(archive_root),
        key=_sync_archive_sort_key,
        reverse=True,
    )
    for archive_path in archives:
        meta_path = get_sync_archive_meta_path(archive_path)
        meta = {}
        if meta_path.exists():
            try:
                meta = load_json_file_tolerant(meta_path, default={}) or {}
            except Exception:
                meta = {}
        st = _safe_file_stat(archive_path)
        archive_size = int(meta.get("archive_size", 0) or 0)
        if not archive_size and st:
            archive_size = int(st.st_size)
        archive_mtime = float(st.st_mtime) if st else 0.0
        return {
            "kind": "archive",
            "archive_path": archive_path,
            "meta_path": meta_path if meta_path.exists() else None,
            "hash": str(meta.get("hash", "") or ""),
            "archive_size": archive_size,
            "archive_sha256": str(meta.get("archive_sha256", "") or "").lower(),
            "file_count": int(meta.get("file_count", 0) or 0),
            "latest_mtime": float(meta.get("latest_mtime", 0.0) or 0.0),
            "timestamp": float(meta.get("timestamp", archive_mtime) or archive_mtime),
            "path_count": int(meta.get("path_count", 1) or 1),
            "paths": meta.get("paths", []),
            "save_specs": meta.get("save_specs", []),
            "manual_save_specs": bool(meta.get("manual_save_specs", False)),
        }
    return None


def get_legacy_sync_snapshot(sync_game_dir: Path, path_count: int) -> Optional[dict]:
    if path_count <= 1:
        targets = [str(sync_game_dir)]
    else:
        targets = [str(sync_game_dir / f"path_{idx}") for idx in range(1, path_count + 1)]
    snapshot = snapshot_sync_paths(targets, str(sync_game_dir))
    if not snapshot.get("file_count"):
        return None
    return {
        "kind": "legacy",
        "path": str(sync_game_dir),
        "hash": snapshot.get("hash", ""),
        "file_count": snapshot.get("file_count", 0),
        "latest_mtime": snapshot.get("latest_mtime", 0.0),
        "timestamp": snapshot.get("latest_mtime", 0.0),
        "path_count": path_count,
    }


def get_remote_sync_payload(sync_game_dir: Path, path_count: int) -> Optional[dict]:
    archive_info = get_latest_sync_archive(sync_game_dir)
    if archive_info:
        return archive_info
    return get_legacy_sync_snapshot(sync_game_dir, path_count)


def _remote_sync_payload_signature(payload: Optional[dict]) -> tuple:
    if not payload:
        return ("none", "", 0, "", 0.0, 0)
    archive_name = ""
    if payload.get("kind") == "archive":
        archive_name = Path(str(payload.get("archive_path", "") or "")).name
    return (
        str(payload.get("kind", "") or ""),
        archive_name,
        int(payload.get("file_count", 0) or 0),
        str(payload.get("hash", "") or ""),
        float(payload.get("latest_mtime", payload.get("timestamp", 0.0)) or 0.0),
        int(payload.get("path_count", 0) or 0),
    )


def _cloud_sync_probe_path(sync_root: str) -> Path:
    return Path(sync_root) / "SteamSaveSync" / ".sync_probe.json"


def _poke_cloud_sync_folder(sync_root: str):
    try:
        probe = _cloud_sync_probe_path(sync_root)
        probe.parent.mkdir(parents=True, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            json.dump({
                "timestamp": datetime.datetime.now().isoformat(timespec="seconds"),
                "pid": os.getpid(),
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def refresh_local_sync_payload(sync_root: str, sync_game_dir: Path, path_count: int,
                               current_payload: Optional[dict] = None,
                               timeout: float = LOCAL_CLOUD_REFRESH_TIMEOUT,
                               interval: float = LOCAL_CLOUD_REFRESH_INTERVAL) -> Optional[dict]:
    if not sync_root or not os.path.isdir(sync_root):
        return current_payload if current_payload is not None else get_remote_sync_payload(sync_game_dir, path_count)
    baseline = current_payload if current_payload is not None else get_remote_sync_payload(sync_game_dir, path_count)
    baseline_signature = _remote_sync_payload_signature(baseline)
    _poke_cloud_sync_folder(sync_root)
    deadline = time.time() + max(0.0, float(timeout))
    latest = baseline
    while time.time() < deadline:
        time.sleep(max(0.1, float(interval)))
        latest = get_remote_sync_payload(sync_game_dir, path_count)
        if _remote_sync_payload_signature(latest) != baseline_signature:
            return latest
    return latest


def _prune_webdav_cache_payload(sync_game_dir: Path, latest_name: Optional[str] = None):
    """仅在 WebDAV 独立缓存模式下使用，使本地缓存贴近云端实际状态。"""
    archive_root = get_sync_archive_root(sync_game_dir)
    keep_names = set()
    if latest_name:
        keep_names.add(latest_name)
        keep_names.add(f"{latest_name}.meta.json")

    if archive_root.exists():
        for item in list(archive_root.rglob("*")):
            if item.is_dir():
                continue
            if keep_names and item.name in keep_names:
                continue
            try:
                item.unlink()
            except Exception:
                pass
        for folder in sorted(
            [p for p in archive_root.rglob("*") if p.is_dir()],
            key=lambda p: len(p.parts),
            reverse=True,
        ):
            try:
                if not any(folder.iterdir()):
                    folder.rmdir()
            except Exception:
                pass
        try:
            if archive_root.exists() and not any(archive_root.iterdir()):
                archive_root.rmdir()
        except Exception:
            pass

    for item in list(sync_game_dir.iterdir()) if sync_game_dir.exists() else []:
        if item.name == "archives":
            continue
        try:
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        except Exception:
            pass


def extract_sync_archive(archive_path: Path, target_specs_or_paths):
    if isinstance(target_specs_or_paths, list) and target_specs_or_paths and isinstance(target_specs_or_paths[0], dict):
        target_specs = get_game_save_specs({"save_specs": target_specs_or_paths}, existing_only=False)
    else:
        targets = target_specs_or_paths if isinstance(target_specs_or_paths, list) else [target_specs_or_paths]
        target_specs = [_default_save_spec(str(t)) for t in _normalize_unique_paths([str(t) for t in targets if t])]
    if not target_specs:
        return
    temp_parent = Path(os.path.dirname(os.path.abspath(sys.argv[0]))) / ".sync_extract_tmp"
    temp_parent.mkdir(parents=True, exist_ok=True)
    temp_root = temp_parent / f"steam_sync_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    temp_root.mkdir(parents=True, exist_ok=True)
    try:
        extracted_count = 0
        with zipfile.ZipFile(archive_path, "r") as archive:
            _validate_zip_extract_limits(archive, "sync archive")
            for info in archive.infolist():
                if not info.filename:
                    continue
                if not info.is_dir() and _is_ignored_save_payload_file(info.filename):
                    continue
                dest = _zip_safe_restore_path(temp_root, info.filename)
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted_count += 1
        if extracted_count <= 0:
            return
        multi_root = temp_root / "_paths"
        if multi_root.is_dir():
            for idx, spec in enumerate(target_specs, start=1):
                source_dir = multi_root / f"p{idx}"
                target_dir = Path(spec["base"])
                target_dir.mkdir(parents=True, exist_ok=True)
                if not source_dir.exists():
                    continue
                if not any(source_dir.iterdir()):
                    continue
                _remove_matching_spec_files(spec)
                for item in source_dir.iterdir():
                    dest = target_dir / item.name
                    if item.is_dir():
                        shutil.copytree(item, dest, dirs_exist_ok=True)
                    else:
                        shutil.copy2(item, dest)
            return
        spec = target_specs[0]
        target_dir = Path(spec["base"])
        target_dir.mkdir(parents=True, exist_ok=True)
        _remove_matching_spec_files(spec)
        for item in temp_root.iterdir():
            if item.name == "_paths":
                continue
            dest = target_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


# ══════════════════════════════════════════════
#  WebDAV 远程同步辅助函数
# ══════════════════════════════════════════════

def _webdav_encode_password(plain: str) -> str:
    if not plain:
        return ""
    return base64.b64encode(plain.encode("utf-8")).decode("ascii")


def _webdav_decode_password(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        return base64.b64decode(encoded.encode("ascii")).decode("utf-8")
    except Exception:
        return ""


_webdav_client_cache: dict = {"fingerprint": None, "client": None}


def _webdav_cfg_fingerprint(cfg: dict) -> str:
    """Derive a fingerprint from WebDAV-related config fields for cache invalidation."""
    parts = [
        str(cfg.get("webdav_url", "") or "").strip(),
        str(cfg.get("webdav_username", "") or "").strip(),
        str(cfg.get("webdav_password", "") or ""),
        str(cfg.get("webdav_preset", "generic") or "generic").strip(),
        str(cfg.get("webdav_verify_ssl", True)),
    ]
    return "|".join(parts)


def _webdav_make_client(cfg: dict):
    if not HAS_WEBDAV or not cfg.get("webdav_enabled"):
        return None
    fp = _webdav_cfg_fingerprint(cfg)
    if _webdav_client_cache["fingerprint"] == fp and _webdav_client_cache["client"] is not None:
        return _webdav_client_cache["client"]
    url = _webdav_normalize_url(
        str(cfg.get("webdav_url", "") or "").strip(),
        str(cfg.get("webdav_preset", "generic") or "generic").strip(),
        str(cfg.get("webdav_username", "") or "").strip(),
    )
    if not url:
        return None
    hostname, root = _webdav_split_url(url)
    client = WebDAVClient({
        "webdav_hostname": hostname,
        "webdav_root": root,
        "webdav_login": str(cfg.get("webdav_username", "") or "").strip(),
        "webdav_password": _webdav_decode_password(cfg.get("webdav_password", "")),
    })
    _webdav_apply_client_options(client, cfg)
    _webdav_client_cache["fingerprint"] = fp
    _webdav_client_cache["client"] = client
    return client


def _webdav_base_path(cfg: Optional[dict]) -> str:
    raw = str((cfg or {}).get("webdav_base_path", "") or "").strip().replace("\\", "/")
    if not raw:
        raw = "/SteamSaveSync"
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or "/SteamSaveSync"


def _webdav_safe_segment(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "game"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", raw)
    safe = safe.strip("._-")
    return safe or "game"


def _webdav_legacy_game_dir_key(game_ref) -> str:
    return get_game_legacy_name_sync_folder_key(game_ref)


def _webdav_remote_archive_dirs(cfg: Optional[dict], game_ref) -> list[str]:
    base_path = _webdav_base_path(cfg)
    storage_key = get_game_storage_key(game_ref)
    candidates = []
    keys = [get_game_sync_folder_key(game_ref)]
    for key in _legacy_sync_name_prefixes(game_ref):
        keys.append(key)
    for key in (str(storage_key or "").strip(), _webdav_safe_segment(storage_key)):
        if any(_is_legacy_device_sync_key(prefix, key) for prefix in _legacy_sync_name_prefixes(game_ref)):
            keys.append(key)
    for key in keys:
        if not key:
            continue
        remote_dir = f"{base_path}/{key}/archives"
        if remote_dir not in candidates:
            candidates.append(remote_dir)
    return candidates or [f"{base_path}/game/archives"]


def _webdav_discover_legacy_remote_archive_dirs(client, cfg: Optional[dict], game_ref) -> list[str]:
    name_prefixes = _legacy_sync_name_prefixes(game_ref)
    if not name_prefixes:
        return []
    base_path = _webdav_base_path(cfg)
    try:
        existing_base = _webdav_find_existing_variant(client, base_path)
        items = client.list(existing_base)
    except Exception as e:
        logger.debug("扫描 WebDAV 旧同步目录失败 %s: %s", base_path, e)
        return []
    result = []
    for item in items:
        name = str(item or "").strip("/").rsplit("/", 1)[-1]
        if not any(_is_legacy_device_sync_key(prefix, name) for prefix in name_prefixes):
            continue
        remote_dir = f"{existing_base.rstrip('/')}/{name}/archives"
        if remote_dir not in result:
            result.append(remote_dir)
    return result


def _webdav_archive_dirs_for_lookup(client, cfg: Optional[dict], game_ref) -> list[str]:
    dirs = list(_webdav_remote_archive_dirs(cfg, game_ref))
    for remote_dir in _webdav_discover_legacy_remote_archive_dirs(client, cfg, game_ref):
        if remote_dir not in dirs:
            dirs.append(remote_dir)
    return dirs


def _webdav_preferred_archive_dir(client, cfg: Optional[dict], game_ref) -> str:
    candidates = _webdav_remote_archive_dirs(cfg, game_ref)
    if not candidates:
        return f"{_webdav_base_path(cfg)}/game/archives"
    shared_dir = candidates[0]
    try:
        return _webdav_find_existing_variant(client, shared_dir)
    except Exception:
        return shared_dir


def _webdav_normalize_url(url: str, preset: str = "generic", username: str = "") -> str:
    raw = str(url or "").strip().rstrip("/")
    if not raw:
        return ""
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception:
        return raw
    if not parts.scheme or not parts.netloc:
        return raw
    path = parts.path or ""
    preset_key = preset if preset in WEBDAV_PRESET_OPTIONS else "generic"
    if not path or path == "/":
        suffix = WEBDAV_PRESET_SUFFIXES.get(preset_key, "")
        if preset_key in ("nextcloud", "openmediavault") and username:
            suffix = f"/remote.php/dav/files/{urllib.parse.quote(username)}"
        if suffix:
            parts = parts._replace(path=suffix)
            return urllib.parse.urlunsplit(parts).rstrip("/")
    return raw


def _webdav_split_url(url: str) -> tuple[str, str]:
    raw = str(url or "").strip()
    parts = urllib.parse.urlsplit(raw)
    if not parts.scheme or not parts.netloc:
        return raw.rstrip("/"), "/"
    hostname = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/")
    root = parts.path or "/"
    if not root.startswith("/"):
        root = "/" + root
    root = root.rstrip("/") or "/"
    return hostname, root


def _webdav_apply_client_options(client, cfg: Optional[dict]):
    verify_ssl = bool((cfg or {}).get("webdav_verify_ssl", True))
    preset = str((cfg or {}).get("webdav_preset", "generic") or "generic").strip()
    try:
        client.verify = verify_ssl
    except Exception:
        pass
    try:
        session = getattr(client, "session", None)
        if session is not None:
            session.verify = verify_ssl
    except Exception:
        pass
    if preset != "generic":
        try:
            client.webdav.disable_check = True
        except Exception:
            pass


def webdav_is_ready(cfg: Optional[dict]) -> bool:
    if not cfg or not HAS_WEBDAV or not cfg.get("webdav_enabled"):
        return False
    if not str(cfg.get("webdav_url", "") or "").strip():
        return False
    username = str(cfg.get("webdav_username", "") or "").strip()
    password = _webdav_decode_password(str(cfg.get("webdav_password", "") or ""))
    if (username and not password) or (password and not username):
        return False
    return True


def get_effective_sync_root(sync_folder: str, cfg: Optional[dict] = None,
                            ensure: bool = False) -> str:
    local_root = str(sync_folder or "").strip()
    if local_root and os.path.isdir(local_root):
        return local_root
    if webdav_is_ready(cfg):
        cache_root = CONFIG_DIR / "webdav_sync_cache"
        if ensure:
            cache_root.mkdir(parents=True, exist_ok=True)
        return str(cache_root)
    return ""


def has_sync_backend(sync_folder: str, cfg: Optional[dict] = None) -> bool:
    return bool(get_effective_sync_root(sync_folder, cfg, ensure=False))


def get_sync_backend_issue(sync_folder: str, cfg: Optional[dict] = None) -> str:
    local_root = str(sync_folder or "").strip()
    if local_root and os.path.isdir(local_root):
        return ""
    if cfg and cfg.get("webdav_enabled"):
        if not HAS_WEBDAV:
            return "webdav_component_missing"
        if not str(cfg.get("webdav_url", "") or "").strip():
            return "webdav_url_missing"
        username = str(cfg.get("webdav_username", "") or "").strip()
        password = _webdav_decode_password(str(cfg.get("webdav_password", "") or ""))
        if (username and not password) or (password and not username):
            return "webdav_credentials_incomplete"
    if local_root:
        return "sync_folder_missing"
    return "not_configured"


def _webdav_enhance_error_message(cfg: Optional[dict], message: str,
                                  operation: str = "") -> str:
    raw = str(message or "").strip()
    if not raw:
        return raw
    lowered = raw.lower()
    if not re.search(r"(?:^|\D)403(?:\D|$)", lowered):
        return raw
    url = str((cfg or {}).get("webdav_url", "") or "").strip().lower()
    is_alist_like = ":5244/" in url and "/dav" in url
    operation_key = str(operation or "").strip().lower()
    is_download = operation_key in {"download", "read"}
    is_delete = operation_key in {"cleanup", "delete", "remove"}
    if is_delete and is_alist_like:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403，云端探针清理被拒绝。若使用 AList，请确认该账号已开启 WebDAV 管理和删除权限。",
            "Server returned 403 and denied cloud probe cleanup. If this is AList, enable WebDAV Manage and delete permissions.",
        )
    elif is_delete:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403，云端探针清理被拒绝。请检查当前 WebDAV 账号是否具备目标文件和目录的删除/管理权限。",
            "Server returned 403 and denied cloud probe cleanup. Check whether this WebDAV account has delete/manage permission for the target file and folder.",
        )
    elif is_download and is_alist_like:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403，云端文件读取/下载被拒绝。若使用 AList，请确认该账号已开启 WebDAV 读取权限，并可读取目标目录和文件。",
            "Server returned 403 and denied reading/downloading the cloud file. If this is AList, enable WebDAV Read and allow access to the target folder and file.",
        )
    elif is_download:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403，云端文件读取/下载被拒绝。请检查当前 WebDAV 账号是否具备目标目录和文件的读取权限。",
            "Server returned 403 and denied reading/downloading the cloud file. Check whether this WebDAV account can read the target folder and file.",
        )
    elif is_alist_like:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403。若使用 AList，请确认该账号已开启 WebDAV 读取、WebDAV 管理，以及创建目录或上传、删除、重命名、复制等权限。",
            "Server returned 403. If this is AList, make sure the account has WebDAV Read, WebDAV Manage, and the Create/Upload, Delete, Rename, and Copy permissions enabled.",
        )
    else:
        hint = bilingual_cfg(
            cfg,
            "服务器返回 403。请检查当前 WebDAV 账号是否对云端目标目录具备写入权限。",
            "Server returned 403. Check whether the current WebDAV account has write permission to the target cloud folder.",
        )
    if hint in raw:
        return raw
    return f"{raw} | {hint}"


def webdav_test_connection(url: str, username: str, password: str,
                           preset: str = "generic", verify_ssl: bool = True,
                           base_path: str = "/SteamSaveSync") -> tuple:
    """测试 WebDAV 上传、下载、内容校验和清理，返回 (success, message)。"""
    if not HAS_WEBDAV:
        return False, f"WebDAV import failed: {WEBDAV_IMPORT_ERROR or 'webdavclient3 not installed'}"
    temp_file_paths = []
    client = None
    probe_root = ""
    probe_file = ""
    probe_file_cleaned = False
    probe_root_cleaned = False
    phase = "connect"
    temp_cfg = {
        "language": cfg_language(None),
        "webdav_url": url,
    }
    try:
        normalized_url = _webdav_normalize_url(url.strip(), preset, username)
        temp_cfg["webdav_url"] = normalized_url
        hostname, root = _webdav_split_url(normalized_url)
        client = WebDAVClient({
            "webdav_hostname": hostname,
            "webdav_root": root,
            "webdav_login": username.strip(),
            "webdav_password": password,
        })
        _webdav_apply_client_options(client, {
            "webdav_verify_ssl": verify_ssl,
            "webdav_preset": preset,
        })

        base_dir = _webdav_base_path({"webdav_base_path": base_path})
        probe_name = f".ssm_probe_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        probe_root = f"{base_dir.rstrip('/')}/Steam_Save_Manager_Probe/archives/{probe_name}"
        probe_file = f"{probe_root}/probe.txt"
        phase = "write"
        _webdav_ensure_remote_dir(client, probe_root)

        probe_payload = f"Steam Save Manager WebDAV probe: {probe_name}".encode("utf-8")
        with tempfile.NamedTemporaryFile("wb", delete=False, suffix=".txt") as tmp:
            tmp.write(probe_payload)
            upload_temp_path = tmp.name
            temp_file_paths.append(upload_temp_path)

        _webdav_upload_with_variants(client, probe_file, upload_temp_path)
        with tempfile.NamedTemporaryFile("w+b", delete=False, suffix=".txt") as tmp:
            download_temp_path = tmp.name
            temp_file_paths.append(download_temp_path)
        phase = "download"
        _webdav_download_with_variants(client, probe_file, download_temp_path)
        phase = "verify"
        with open(download_temp_path, "rb") as downloaded:
            if downloaded.read() != probe_payload:
                raise RuntimeError(bilingual_cfg(
                    temp_cfg,
                    "WebDAV 云端探针下载内容校验失败。",
                    "The downloaded WebDAV cloud probe did not match the uploaded content.",
                ))
        phase = "cleanup"
        try:
            _webdav_clean_with_variants(client, probe_file)
            if _webdav_path_exists(client, probe_file):
                raise RuntimeError("probe file still exists after deletion")
            probe_file_cleaned = True
            _webdav_clean_with_variants(client, probe_root)
            if _webdav_path_exists(client, probe_root):
                raise RuntimeError("probe directory still exists after deletion")
            probe_root_cleaned = True
        except Exception as cleanup_error:
            raise RuntimeError(bilingual_cfg(
                temp_cfg,
                f"WebDAV 云端探针清理失败；请检查删除/管理权限：{cleanup_error}",
                f"WebDAV cloud probe cleanup failed; check delete/manage permissions: {cleanup_error}",
            )) from cleanup_error
        return True, "OK"
    except Exception as e:
        if phase in {"download", "verify"}:
            operation = "download"
        elif phase == "cleanup":
            operation = "delete"
        else:
            operation = "write"
        return False, _webdav_enhance_error_message(
            temp_cfg, f"{type(e).__name__}: {e}", operation=operation)
    finally:
        if client is not None and probe_file and not probe_file_cleaned:
            try:
                _webdav_clean_with_variants(client, probe_file)
            except Exception:
                pass
        if client is not None and probe_root and not probe_root_cleaned:
            try:
                _webdav_clean_with_variants(client, probe_root)
            except Exception:
                pass
        for temp_file_path in temp_file_paths:
            try:
                os.remove(temp_file_path)
            except Exception:
                pass


def _webdav_remote_archive_dir(cfg: Optional[dict], game_ref) -> str:
    return _webdav_remote_archive_dirs(cfg, game_ref)[0]


def _webdav_archive_entries_from_existing_dir(client, existing_dir: str) -> list[dict]:
    entries = []
    seen = set()

    def _add_zip_entry(archive_dir: str, name: str):
        if not name.endswith(".zip"):
            return
        entry_key = (archive_dir, name)
        if entry_key in seen:
            return
        seen.add(entry_key)
        entries.append({"dir": archive_dir, "name": name})

    items = client.list(existing_dir)
    for item in items:
        name = item.strip("/").rsplit("/", 1)[-1] if "/" in item else item.strip("/")
        if not name:
            continue
        if name.endswith(".zip"):
            _add_zip_entry(existing_dir, name)
            continue
        # 兼容本地云盘缓存形态：archives/YYYY-MM/*.zip
        nested_dir = f"{existing_dir.rstrip('/')}/{name}"
        try:
            nested_existing_dir = _webdav_find_existing_variant(client, nested_dir)
            nested_items = client.list(nested_existing_dir)
        except Exception:
            continue
        for nested_item in nested_items:
            nested_name = nested_item.strip("/").rsplit("/", 1)[-1] if "/" in nested_item else nested_item.strip("/")
            _add_zip_entry(nested_existing_dir, nested_name)
    entries.sort(key=lambda item: item["name"])
    return entries


def _webdav_list_archive_entries(client, cfg: dict, game_ref, strict: bool = False) -> list[dict]:
    entries = []
    seen = set()
    errors = []
    for remote_dir in _webdav_archive_dirs_for_lookup(client, cfg, game_ref):
        try:
            existing_dir = _webdav_find_existing_variant(client, remote_dir)
        except FileNotFoundError:
            continue
        except Exception as e:
            errors.append(f"{remote_dir}: {type(e).__name__}: {e}")
            continue
        try:
            dir_entries = _webdav_archive_entries_from_existing_dir(client, existing_dir)
        except Exception as e:
            errors.append(f"{existing_dir}: {type(e).__name__}: {e}")
            continue
        for entry in dir_entries:
            entry_key = (entry["dir"], entry["name"])
            if entry_key in seen:
                continue
            seen.add(entry_key)
            entries.append(entry)
    entries.sort(key=lambda item: item["name"])
    if strict and not entries and errors:
        raise RuntimeError("Failed to list WebDAV archives: " + "; ".join(errors[:3]))
    return entries


def _webdav_path_variants(remote_path: str) -> list[str]:
    raw = str(remote_path or "").strip().replace("\\", "/")
    if not raw:
        return ["/", ""]
    absolute = "/" + raw.strip("/")
    relative = raw.strip("/")
    variants = []
    for item in (absolute, relative):
        if item not in variants:
            variants.append(item)
    return variants


def _webdav_try_variants(func, remote_path: str):
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            result = func(candidate)
            if result is False:
                last_error = RuntimeError(f"operation returned False for {candidate}")
                continue
            return candidate, result
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("invalid_webdav_path")


def _webdav_find_existing_variant(client, remote_path: str) -> str:
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            if client.check(candidate):
                return candidate
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise FileNotFoundError(remote_path)


def _webdav_path_exists(client, remote_path: str) -> bool:
    raw_path = str(remote_path or "").replace("\\", "/")
    file_name = PurePosixPath(raw_path).name
    parent_dir = str(PurePosixPath(raw_path).parent)
    if not file_name:
        return False
    last_error = None
    parent_missing = False
    for candidate in _webdav_path_variants(parent_dir):
        try:
            items = client.list(candidate)
        except Exception as e:
            lowered = str(e).lower()
            if any(token in lowered for token in ("404", "not found", "does not exist")):
                parent_missing = True
                continue
            last_error = e
            continue
        for item in items:
            item_name = str(item or "").strip("/").rsplit("/", 1)[-1]
            if item_name == file_name:
                return True
        return False
    if parent_missing and last_error is None:
        return False
    if last_error:
        raise last_error
    return False


def _webdav_upload_with_variants(client, remote_path: str, local_path: str):
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            result = client.upload_sync(remote_path=candidate, local_path=local_path)
            if result is False:
                last_error = RuntimeError(f"upload returned False for {candidate}")
                continue
            return candidate
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("invalid_webdav_upload_path")


def _webdav_download_with_variants(client, remote_path: str, local_path: str):
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            result = client.download_sync(remote_path=candidate, local_path=local_path)
            if result is False:
                last_error = RuntimeError(f"download returned False for {candidate}")
                continue
            return candidate
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("invalid_webdav_download_path")


def _webdav_clean_with_variants(client, remote_path: str):
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            result = client.clean(candidate)
            if result is False:
                last_error = RuntimeError(f"clean returned False for {candidate}")
                continue
            return candidate
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("invalid_webdav_clean_path")


def _webdav_info_with_variants(client, remote_path: str) -> dict:
    last_error = None
    for candidate in _webdav_path_variants(remote_path):
        try:
            return client.info(candidate)
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    raise RuntimeError("invalid_webdav_info_path")


def _webdav_list_dir_names(client, remote_dir: str) -> tuple[str, set[str]]:
    existing_dir = _webdav_find_existing_variant(client, remote_dir)
    items = client.list(existing_dir)
    names = set()
    for item in items:
        name = item.strip("/").rsplit("/", 1)[-1] if "/" in item else item.strip("/")
        if name:
            names.add(name)
    return existing_dir, names


def _webdav_ensure_remote_dir(client, remote_dir: str):
    normalized = str(remote_dir or "").strip().strip("/")
    if not normalized:
        return
    current = ""
    for part in [p for p in normalized.split("/") if p]:
        current = f"{current}/{part}" if current else part
        last_error = None
        created = False
        for candidate in _webdav_path_variants(current):
            try:
                result = client.mkdir(candidate)
                if result is False:
                    last_error = RuntimeError(f"mkdir returned False for {candidate}")
                    continue
                created = True
                break
            except Exception as e:
                err_text = str(e).lower()
                if any(token in err_text for token in (
                    "already exists",
                    "file exists",
                    "405",
                    "301",
                    "302",
                    "method not allowed",
                    "conflict",
                )):
                    created = True
                    break
                last_error = e
        if not created:
            if last_error:
                raise last_error
            raise RuntimeError(f"unable to create remote directory: {current}")


def _webdav_verify_remote_file(client, remote_path: str, expected_size: int = 0) -> tuple[bool, str]:
    parent_dir = str(PurePosixPath(str(remote_path or "").replace("\\", "/")).parent)
    file_name = PurePosixPath(str(remote_path or "").replace("\\", "/")).name
    listed = False
    try:
        _, names = _webdav_list_dir_names(client, parent_dir)
        listed = file_name in names
    except Exception:
        listed = False
    try:
        info = _webdav_info_with_variants(client, remote_path)
    except Exception as e:
        if listed:
            return True, ""
        return False, f"{type(e).__name__}: {e}"
    try:
        remote_size = int(info.get("size", 0) or 0)
    except Exception:
        remote_size = 0
    if expected_size and remote_size and remote_size != expected_size:
        return False, f"remote_size_mismatch:{remote_size}!={expected_size}"
    if listed:
        return True, ""
    return True, ""


def webdav_upload_archive(cfg: dict, local_zip: str, local_meta: str, game_ref) -> tuple[bool, str]:
    """上传 ZIP + meta.json 到 WebDAV，返回 (success, message)。"""
    client = _webdav_make_client(cfg)
    if not client:
        return False, "client_unavailable"
    try:
        archive_dir = _webdav_preferred_archive_dir(client, cfg, game_ref)
        _webdav_ensure_remote_dir(client, archive_dir)
        zip_name = Path(local_zip).name
        meta_name = Path(local_meta).name
        expected_zip_size = int(Path(local_zip).stat().st_size) if Path(local_zip).exists() else 0
        archive_dir_variant = _webdav_upload_with_variants(
            client,
            f"{archive_dir.rstrip('/')}/{zip_name}",
            local_zip,
        ).rsplit("/", 1)[0]
        remote_zip = f"{archive_dir_variant.rstrip('/')}/{zip_name}"
        ok, verify_msg = _webdav_verify_remote_file(client, remote_zip, expected_zip_size)
        if not ok:
            return False, verify_msg or "remote_zip_verify_failed"
        try:
            _webdav_upload_with_variants(
                client,
                f"{archive_dir_variant.rstrip('/')}/{meta_name}",
                local_meta,
            )
            expected_meta_size = int(Path(local_meta).stat().st_size) if Path(local_meta).exists() else 0
            ok, verify_msg = _webdav_verify_remote_file(
                client,
                f"{archive_dir_variant.rstrip('/')}/{meta_name}",
                expected_meta_size,
            )
            if not ok:
                return False, _webdav_enhance_error_message(cfg, verify_msg or "remote_meta_verify_failed")
        except Exception as e:
            return False, _webdav_enhance_error_message(cfg, f"{type(e).__name__}: {e}")
        return True, ""
    except Exception as e:
        return False, _webdav_enhance_error_message(cfg, f"{type(e).__name__}: {e}")


def webdav_list_archives(cfg: dict, game_ref) -> list:
    """列出 WebDAV 上的 .zip 存档文件名（排序后）"""
    client = _webdav_make_client(cfg)
    if not client:
        return []
    try:
        return [entry["name"] for entry in _webdav_list_archive_entries(client, cfg, game_ref)]
    except Exception:
        return []


def webdav_enforce_archive_limits(cfg: dict, game_ref, keep_count: int = 3):
    """执行 WebDAV 云端归档轮转，只保留最近 keep_count 个 ZIP。"""
    if keep_count <= 0:
        return
    client = _webdav_make_client(cfg)
    if not client:
        return
    try:
        archive_entries = sorted(
            _webdav_list_archive_entries(client, cfg, game_ref),
            key=lambda item: item["name"],
            reverse=True,
        )
    except Exception:
        archive_entries = []
    if len(archive_entries) <= keep_count:
        return

    for entry in archive_entries[keep_count:]:
        archive_dir = entry["dir"]
        name = entry["name"]
        try:
            _webdav_clean_with_variants(client, f"{archive_dir.rstrip('/')}/{name}")
        except Exception:
            pass
        try:
            _webdav_clean_with_variants(client, f"{archive_dir.rstrip('/')}/{name}.meta.json")
        except Exception:
            pass


def webdav_download_latest(cfg: dict, game_ref, local_sync_game_dir: Path,
                           cache_mirror_remote: bool = False) -> str | None:
    """从 WebDAV 下载最新存档到本地缓存，返回本地 ZIP 路径或 None"""
    client = _webdav_make_client(cfg)
    if not client:
        raise RuntimeError("WebDAV client is unavailable")
    try:
        archive_entries = _webdav_list_archive_entries(client, cfg, game_ref, strict=True)
    except Exception as e:
        raw_message = f"{type(e).__name__}: {e}"
        enhanced = _webdav_enhance_error_message(cfg, raw_message, operation="download")
        if enhanced != raw_message:
            raise RuntimeError(enhanced) from e
        raise
    if not archive_entries:
        if cache_mirror_remote:
            _prune_webdav_cache_payload(local_sync_game_dir, None)
        return None
    latest_entry = archive_entries[-1]
    latest_name = latest_entry["name"]
    archive_dir = latest_entry["dir"]
    meta_name = latest_name + ".meta.json"
    remote_zip = f"{archive_dir.rstrip('/')}/{latest_name}"
    remote_zip_info = {}
    try:
        remote_zip_info = _webdav_info_with_variants(client, remote_zip) or {}
    except Exception:
        remote_zip_info = {}

    # 解析时间戳 -> 本地 YYYY-MM 子目录
    ts_part = latest_name.replace(".zip", "")
    month_str = ts_part[:7].replace("_", "-", 1) if "_" in ts_part[:7] else ts_part[:4] + "-" + ts_part[4:6]
    month_dir = get_sync_archive_root(local_sync_game_dir) / month_str
    month_dir.mkdir(parents=True, exist_ok=True)
    local_zip = month_dir / latest_name
    local_meta = month_dir / meta_name

    def _read_archive_meta(meta_path: Path) -> dict:
        if not meta_path.exists():
            return {}
        try:
            return load_json_file_tolerant(meta_path, default={}) or {}
        except Exception:
            return {}

    def _validate_local_archive(candidate: Path, meta_data: Optional[dict] = None,
                                allow_missing_size: bool = True) -> tuple[bool, str]:
        meta_obj = meta_data if isinstance(meta_data, dict) else _read_archive_meta(local_meta)
        try:
            expected_size = int(meta_obj.get("archive_size", 0) or 0)
        except Exception:
            expected_size = 0
        expected_sha256 = str(meta_obj.get("archive_sha256", "") or "").lower()
        if expected_size:
            try:
                if candidate.stat().st_size != expected_size:
                    return False, f"archive_size_mismatch:{candidate.stat().st_size}!={expected_size}"
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
        elif not allow_missing_size:
            return False, "archive_size_missing"
        ok, zip_msg = validate_zip_archive(candidate)
        if not ok:
            return False, zip_msg or "invalid_zip"
        if expected_sha256 and compute_file_sha256(candidate) != expected_sha256:
            return False, "archive_sha256_mismatch"
        return True, ""

    if local_zip.exists():
        ok, _ = _validate_local_archive(local_zip, allow_missing_size=True)
        if ok:
            if cache_mirror_remote:
                _prune_webdav_cache_payload(local_sync_game_dir, latest_name)
            return None

    temp_zip = _atomic_zip_path(local_zip)
    temp_meta = _atomic_zip_path(local_meta)
    downloaded_meta = {}
    try:
        _webdav_download_with_variants(client, remote_zip, str(temp_zip))

        # meta 文件缺失不视为致命；ZIP 本体会继续用云端 size 和 zip 完整性校验。
        try:
            _webdav_download_with_variants(
                client,
                f"{archive_dir.rstrip('/')}/{meta_name}",
                str(temp_meta))
            downloaded_meta = _read_archive_meta(temp_meta)
        except Exception:
            downloaded_meta = {}
            try:
                temp_meta.unlink(missing_ok=True)
            except Exception:
                pass
        if not downloaded_meta and remote_zip_info.get("size"):
            try:
                downloaded_meta = {"archive_size": int(remote_zip_info.get("size", 0) or 0)}
            except Exception:
                downloaded_meta = {}
        ok, zip_msg = _validate_local_archive(temp_zip, downloaded_meta, allow_missing_size=True)
        if not ok:
            raise RuntimeError(f"Downloaded WebDAV archive failed validation: {zip_msg}")
        temp_zip.replace(local_zip)
        if temp_meta.exists():
            temp_meta.replace(local_meta)
        if cache_mirror_remote:
            _prune_webdav_cache_payload(local_sync_game_dir, latest_name)
        return str(local_zip)
    except Exception as e:
        for temp_path in (temp_zip, temp_meta):
            try:
                temp_path.unlink(missing_ok=True)
            except Exception:
                pass
        raw_message = f"{type(e).__name__}: {e}"
        enhanced = _webdav_enhance_error_message(cfg, raw_message, operation="download")
        if enhanced != raw_message:
            raise RuntimeError(enhanced) from e
        raise


def _webdav_download_json_meta(client, remote_meta_path: str) -> dict:
    temp_path = ""
    try:
        with tempfile.NamedTemporaryFile("w+b", delete=False, suffix=".json") as tmp:
            temp_path = tmp.name
        _webdav_download_with_variants(client, remote_meta_path, temp_path)
        return load_json_file_tolerant(temp_path, default={}) or {}
    except Exception:
        return {}
    finally:
        if temp_path:
            try:
                os.remove(temp_path)
            except Exception:
                pass


def webdav_discover_remote_games(cfg: Optional[dict], local_sync_root: str) -> list[dict]:
    """Discover WebDAV archive folders and rebuild game entries from their meta files."""
    client = _webdav_make_client(cfg or {})
    if not client or not local_sync_root:
        return []
    try:
        existing_base = _webdav_find_existing_variant(client, _webdav_base_path(cfg))
        items = client.list(existing_base)
    except Exception as e:
        logger.debug("扫描 WebDAV 云端游戏目录失败: %s", e)
        return []

    discovered = []
    seen_keys = set()
    for item in items:
        folder_name = item.strip("/").rsplit("/", 1)[-1] if "/" in item else item.strip("/")
        if not folder_name or folder_name in {".", "..", "Steam_Save_Manager_Probe"}:
            continue
        remote_archive_dir = f"{existing_base.rstrip('/')}/{folder_name}/archives"
        try:
            existing_archive_dir = _webdav_find_existing_variant(client, remote_archive_dir)
            archive_entries = _webdav_archive_entries_from_existing_dir(client, existing_archive_dir)
        except Exception:
            continue
        if not archive_entries:
            continue
        latest_entry = archive_entries[-1]
        meta = _webdav_download_json_meta(
            client,
            f"{latest_entry['dir'].rstrip('/')}/{latest_entry['name']}.meta.json",
        )
        if not meta:
            continue
        game_name = str(meta.get("game", "") or "").strip()
        appid = str(meta.get("appid", "") or "").strip()
        if not game_name:
            game_name = appid and f"AppID {appid}" or folder_name
        game = {
            "name": game_name,
            "appid": appid,
            "auto_backup": True,
            "watch_backup": True,
            "backup_on_exit": False,
            "sync_enabled": True,
            "favorite": False,
        }
        save_specs = meta.get("save_specs", [])
        if isinstance(save_specs, list) and save_specs:
            set_game_save_specs(game, save_specs)
        else:
            paths = meta.get("paths", [])
            if isinstance(paths, list) and paths:
                set_game_save_paths(game, [str(p) for p in paths if p])
        if meta.get("manual_save_specs"):
            game["manual_save_specs"] = True
        if not get_game_save_paths(game, existing_only=False):
            continue
        ensure_game_storage_identity(game)
        sync_key = get_game_sync_folder_key(game)
        dedupe_key = (appid or "").strip() or sync_key.lower()
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        try:
            cache_dir = get_sync_game_dir(local_sync_root, game)
            cache_dir.mkdir(parents=True, exist_ok=True)
            webdav_download_latest(cfg or {}, game, cache_dir, cache_mirror_remote=True)
        except Exception as e:
            logger.debug("预取 WebDAV 云端存档缓存失败 %s: %s", game_name, e)
        discovered.append(game)
    return discovered


def snapshot_dir(directory: str) -> dict:
    """Single-pass directory snapshot: hash, file_count, latest_mtime in one os.walk.

    Replaces the previous pattern of calling compute_dir_hash, compute_dir_file_count,
    and compute_dir_latest_mtime separately (3 full directory walks).
    """
    h = hashlib.md5()
    file_count = 0
    latest_mtime = 0.0
    try:
        all_files = []
        for root, _, files in os.walk(directory):
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, directory)
                file_count += 1
                try:
                    latest_mtime = max(latest_mtime, os.path.getmtime(fp))
                except OSError:
                    pass
                all_files.append((rel, fp))
        # 按相对路径排序，保证顺序一致
        all_files.sort(key=lambda x: x[0])
        for rel, fp in all_files:
            h.update(rel.encode("utf-8"))
            _streaming_file_hash(fp, h)
    except OSError:
        pass
    return {
        "hash": h.hexdigest(),
        "file_count": file_count,
        "latest_mtime": latest_mtime,
    }


def compute_dir_hash(directory: str) -> str:
    """递归计算目录内所有文件的内容 MD5 校验和。"""
    return snapshot_dir(directory)["hash"]


def compute_dir_file_count(directory: str) -> int:
    """统计目录内文件数量"""
    return snapshot_dir(directory)["file_count"]


def compute_dir_latest_mtime(directory: str) -> float:
    """返回目录内最新文件修改时间；目录为空时返回 0。"""
    return snapshot_dir(directory)["latest_mtime"]


def snapshot_sync_side(directory: str) -> dict:
    """采集目录的同步摘要，用于冲突展示。"""
    if not os.path.isdir(directory):
        return {
            "path": directory,
            "file_count": 0,
            "hash": "",
            "latest_mtime": 0.0,
        }
    snap = snapshot_dir(directory)
    return {
        "path": directory,
        "file_count": snap["file_count"],
        "hash": snap["hash"],
        "latest_mtime": snap["latest_mtime"],
    }


def combine_sync_hash(parts: list[tuple[str, str]]) -> str:
    """
    组合多个目录 hash，得到稳定总 hash。
    parts: [(key, hash), ...]
    """
    if not parts:
        return ""
    h = hashlib.md5()
    for key, value in sorted(parts, key=lambda item: item[0].lower()):
        h.update(key.encode("utf-8", errors="ignore"))
        h.update(b"\0")
        h.update((value or "").encode("utf-8", errors="ignore"))
        h.update(b"\0")
    return h.hexdigest()


def snapshot_sync_paths(paths: list[str], label: str) -> dict:
    """采集多个目录的同步摘要。"""
    existing = [os.path.normpath(p) for p in paths if os.path.isdir(p)]
    if not existing:
        return {
            "path": label,
            "file_count": 0,
            "hash": "",
            "latest_mtime": 0.0,
        }
    hash_parts = []
    total_files = 0
    latest_mtime = 0.0
    for idx, path in enumerate(existing, start=1):
        snap = snapshot_dir(path)
        total_files += snap["file_count"]
        latest_mtime = max(latest_mtime, snap["latest_mtime"])
        hash_parts.append((f"path_{idx}", snap["hash"] if snap["file_count"] else ""))
    return {
        "path": label,
        "file_count": total_files,
        "hash": combine_sync_hash(hash_parts),
        "latest_mtime": latest_mtime,
    }


def snapshot_sync_specs(specs: list[dict], label: str) -> dict:
    existing = get_game_save_specs({"save_specs": specs}, existing_only=True)
    if not existing:
        return {
            "path": label,
            "file_count": 0,
            "hash": "",
            "latest_mtime": 0.0,
        }
    hash_parts = []
    total_files = 0
    latest_mtime = 0.0
    for idx, spec in enumerate(existing, start=1):
        snap = compute_save_spec_snapshot([spec])
        total_files += snap["file_count"]
        latest_mtime = max(latest_mtime, snap["latest_mtime"])
        hash_parts.append((f"path_{idx}", snap["hash"] if snap["file_count"] else ""))
    return {
        "path": label,
        "file_count": total_files,
        "hash": combine_sync_hash(hash_parts),
        "latest_mtime": latest_mtime,
    }


def format_sync_time(timestamp: float) -> str:
    if not timestamp:
        return "—"
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def get_game_sync_key(game: dict) -> str:
    """为游戏生成稳定的同步状态键。"""
    appid = str(game.get("appid", "")).strip() or "-"
    name = sanitize(game.get("name", "")).lower() or "-"
    specs = get_game_save_specs(game, existing_only=False)
    if specs:
        spec_sig = "|".join(
            f"{os.path.normcase(spec['base'])}::{','.join(p.lower() for p in spec.get('includes', []))}::{int(bool(spec.get('recursive', True)))}"
            for spec in specs
        )
    else:
        paths = get_game_save_paths(game, existing_only=False)
        if not paths:
            paths = [str(game.get("save_path", "") or "")]
        spec_sig = "|".join(os.path.normcase(os.path.normpath(p)) for p in paths if p)
    return f"appid:{appid}|name:{name}|paths:{spec_sig}"


def get_game_sync_state(cfg: Optional[dict], game: dict) -> dict:
    if not cfg:
        return {}
    store = cfg.setdefault("sync_state", {})
    if not isinstance(store, dict):
        cfg["sync_state"] = {}
        store = cfg["sync_state"]
    return store.get(get_game_sync_key(game), {})


def set_game_sync_state(cfg: Optional[dict], game: dict,
                        local_hash: str, remote_hash: str,
                        last_action: str = ""):
    """记录某个游戏最近一次确认一致的本地/云端哈希基线。"""
    if not cfg:
        return
    store = cfg.setdefault("sync_state", {})
    if not isinstance(store, dict):
        cfg["sync_state"] = {}
        store = cfg["sync_state"]
    key = get_game_sync_key(game)
    prev = store.get(key, {})
    entry = {
        "local_hash": local_hash,
        "remote_hash": remote_hash,
    }
    if last_action:
        entry["last_action"] = last_action
        entry["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    elif "last_action" in prev:
        entry["last_action"] = prev["last_action"]
    elif "updated_at" in prev:
        entry["updated_at"] = prev["updated_at"]
    if entry != prev:
        store[key] = entry
        save_config(cfg)


def mark_game_sync_conflict(cfg: Optional[dict], game: dict, reason: str,
                            local_info: dict, remote_info: dict):
    if not cfg:
        return
    store = cfg.setdefault("sync_state", {})
    if not isinstance(store, dict):
        cfg["sync_state"] = {}
        store = cfg["sync_state"]
    key = get_game_sync_key(game)
    prev = store.get(key, {})
    entry = dict(prev)
    entry["pending_conflict"] = {
        "reason": reason,
        "detected_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "local": local_info,
        "remote": remote_info,
    }
    store[key] = entry
    save_config(cfg)


def snooze_game_sync_conflict(cfg: Optional[dict], game: dict, seconds: int = 300):
    if not cfg:
        return
    store = cfg.get("sync_state", {})
    if not isinstance(store, dict):
        return
    key = get_game_sync_key(game)
    entry = store.get(key)
    if not isinstance(entry, dict):
        return
    conflict = entry.get("pending_conflict")
    if not isinstance(conflict, dict):
        return
    entry = dict(entry)
    conflict = dict(conflict)
    conflict["snoozed_until"] = time.time() + max(0, int(seconds))
    entry["pending_conflict"] = conflict
    store[key] = entry
    save_config(cfg)


def is_game_sync_conflict_snoozed(cfg: Optional[dict], game: dict) -> bool:
    if not cfg:
        return False
    state = get_game_sync_state(cfg, game)
    conflict = state.get("pending_conflict")
    if not isinstance(conflict, dict):
        return False
    try:
        snoozed_until = float(conflict.get("snoozed_until", 0) or 0)
    except Exception:
        snoozed_until = 0.0
    return snoozed_until > time.time()


def clear_game_sync_conflict(cfg: Optional[dict], game: dict):
    if not cfg:
        return
    store = cfg.get("sync_state", {})
    if not isinstance(store, dict):
        return
    key = get_game_sync_key(game)
    entry = store.get(key)
    if not isinstance(entry, dict) or "pending_conflict" not in entry:
        return
    entry = dict(entry)
    entry.pop("pending_conflict", None)
    store[key] = entry
    save_config(cfg)


def get_sync_retry_queue(cfg: Optional[dict]) -> list:
    if not cfg:
        return []
    q = cfg.setdefault("sync_retry_queue", [])
    if not isinstance(q, list):
        cfg["sync_retry_queue"] = []
        q = cfg["sync_retry_queue"]
    return q


def find_game_by_sync_key(cfg: Optional[dict], game_key: str) -> Optional[dict]:
    if not cfg:
        return None
    for g in cfg.get("games", []):
        if get_game_sync_key(g) == game_key:
            return g
    return None


def enqueue_sync_retry(cfg: Optional[dict], game: dict, mode: str, reason: str):
    if not cfg:
        return
    q = get_sync_retry_queue(cfg)
    game_key = get_game_sync_key(game)
    for item in q:
        if item.get("game_key") == game_key and item.get("mode") == mode:
            item["reason"] = reason
            item["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            item["attempts"] = int(item.get("attempts", 0)) + 1
            save_config(cfg)
            return
    q.append({
        "game_key": game_key,
        "mode": mode,
        "reason": reason,
        "attempts": 1,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    })
    save_config(cfg)


def clear_sync_retry(cfg: Optional[dict], game: dict, mode: Optional[str] = None):
    if not cfg:
        return
    q = get_sync_retry_queue(cfg)
    game_key = get_game_sync_key(game)
    new_q = [item for item in q
             if not (item.get("game_key") == game_key
                     and (mode is None or item.get("mode") == mode))]
    if len(new_q) != len(q):
        cfg["sync_retry_queue"] = new_q
        save_config(cfg)


def run_sync_retries(cfg: Optional[dict], sync_folder: str,
                     log_cb=None) -> list[tuple[str, str]]:
    """重试此前排队的自动同步任务。"""
    effective_root = get_effective_sync_root(sync_folder, cfg, ensure=True)
    if not cfg or not effective_root:
        return []
    q = list(get_sync_retry_queue(cfg))
    if not q:
        return []
    keep = []
    results = []
    changed = False
    for item in q:
        game = find_game_by_sync_key(cfg, item.get("game_key", ""))
        if not game:
            changed = True
            continue
        mode = item.get("mode", "upload")
        try:
            result = sync_game_save(game, effective_root, mode, auto=True, cfg=cfg)
            results.append((game["name"], result))
            if log_cb:
                log_cb(bilingual_cfg(
                    cfg,
                    f"↻ 重试 {game['name']} ({mode}): {result}",
                    f"↻ Retried {game['name']} ({mode}): {result}",
                ))
            changed = True
        except SyncPendingError as e:
            item = dict(item)
            item["reason"] = str(e)
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            keep.append(item)
            changed = True
            if log_cb:
                log_cb(bilingual_cfg(
                    cfg,
                    f"⏳ 等待云盘同步 {game['name']} ({mode}): {e}",
                    f"⏳ Waiting for cloud sync for {game['name']} ({mode}): {e}",
                ))
        except Exception as e:
            item = dict(item)
            item["reason"] = str(e)
            item["attempts"] = int(item.get("attempts", 0)) + 1
            item["updated_at"] = datetime.datetime.now().isoformat(timespec="seconds")
            keep.append(item)
            changed = True
            if log_cb:
                log_cb(bilingual_cfg(
                    cfg,
                    f"❌ 重试失败 {game['name']} ({mode}): {e}",
                    f"❌ Retry failed for {game['name']} ({mode}): {e}",
                ))
    if changed or len(keep) != len(q):
        cfg["sync_retry_queue"] = keep
        save_config(cfg)
    return results


def clear_game_sync_state(cfg: Optional[dict], game: dict):
    if not cfg:
        return
    store = cfg.get("sync_state", {})
    if not isinstance(store, dict):
        return
    key = get_game_sync_key(game)
    changed = False
    if key in store:
        store.pop(key, None)
        changed = True
    q = get_sync_retry_queue(cfg)
    new_q = [item for item in q if item.get("game_key") != key]
    if len(new_q) != len(q):
        cfg["sync_retry_queue"] = new_q
        changed = True
    if changed:
        save_config(cfg)


def sync_game_save(game: dict, sync_folder: str, mode: str = "smart",
                   auto: bool = False, cfg: Optional[dict] = None) -> str:
    with SAVE_IO_LOCK:
        return _sync_game_save_impl(game, sync_folder, mode, auto=auto, cfg=cfg)


def _sync_game_save_impl(game: dict, sync_folder: str, mode: str = "smart",
                         auto: bool = False, cfg: Optional[dict] = None) -> str:
    """
    同步单个游戏的存档。
    mode:
      - 'smart':  智能云存档（由 GameProcessMonitor 触发，手动调用时等同 bidirectional）
      - 'bidirectional': 基于上次同步快照判断单边变更；双边都改时标记冲突
      - 'upload':  本地 → 同步文件夹
      - 'download': 同步文件夹 → 本地
    auto: 是否为自动触发（用于区分备份标记）
    返回操作描述。
    """
    lang = cfg_language(cfg)
    if not is_game_sync_enabled(game):
        return bilingual_text(lang, "跳过：此游戏已关闭存档自动同步", "Skipped: save sync is disabled for this game")
    sync_tag = bilingual_text(lang, "自动同步成功", "Auto sync completed") if auto else bilingual_text(lang, "同步成功", "Sync completed")
    pre_sync_backup_tag = bilingual_text(lang, "同步前自动备份", "Auto backup before sync")
    if mode == "smart":
        mode = "bidirectional"

    steam_path_for_upgrade = str(cfg.get("steam_path", "") or "").strip() if cfg else ""
    effective_specs = []
    if steam_path_for_upgrade and not game.get("manual_save_specs"):
        precise_specs = _infer_precise_metadata_specs_for_game(game, steam_path_for_upgrade, cfg)
        if precise_specs:
            current_paths = get_game_save_paths(game, existing_only=False)
            rebuilt_specs = list(precise_specs)
            for extra_path in current_paths[1:]:
                rebuilt_specs.append(_default_save_spec(extra_path))
            rebuilt_specs = _normalize_unique_save_specs(rebuilt_specs)
            effective_specs = rebuilt_specs
            if rebuilt_specs != get_game_save_specs(game, existing_only=False):
                set_game_save_specs(game, rebuilt_specs)
                save_config(cfg)

    configured_specs = effective_specs or get_game_save_specs(game, existing_only=False)
    configured_paths = [spec["base"] for spec in configured_specs]
    if not configured_paths:
        fallback = str(game.get("save_path", "") or "").strip()
        configured_specs = [_default_save_spec(fallback)] if fallback else []
    configured_paths = [spec["base"] for spec in configured_specs]
    if not configured_paths:
        return bilingual_text(lang, "跳过：存档路径不存在", "Skipped: save path does not exist")
    effective_sync_root = get_effective_sync_root(sync_folder, cfg, ensure=True)
    if not effective_sync_root:
        issue = get_sync_backend_issue(sync_folder, cfg)
        if issue == "webdav_component_missing":
            return bilingual_text(
                lang,
                "跳过：WebDAV 已启用，但当前运行环境缺少 webdavclient3 组件",
                "Skipped: WebDAV is enabled, but webdavclient3 is unavailable in the current runtime",
            )
        if issue == "webdav_url_missing":
            return bilingual_text(
                lang,
                "跳过：WebDAV 已启用，但服务器地址为空",
                "Skipped: WebDAV is enabled, but the server URL is empty",
            )
        if issue == "webdav_credentials_incomplete":
            return bilingual_text(
                lang,
                "跳过：WebDAV 用户名或密码不完整",
                "Skipped: WebDAV username or password is incomplete",
            )
        return bilingual_text(
            lang,
            "跳过：同步目录与 WebDAV 均不可用",
            "Skipped: neither a sync folder nor WebDAV is available",
        )

    dest_dir = ensure_shared_sync_game_dir(effective_sync_root, game)
    dest_dir.mkdir(parents=True, exist_ok=True)
    webdav_active = bool(cfg and cfg.get("webdav_enabled"))
    webdav_cache_root = (CONFIG_DIR / "webdav_sync_cache")
    using_webdav_cache = Path(effective_sync_root).resolve() == webdav_cache_root.resolve()
    webdav_error = ""
    webdav_prefetch_error = ""

    def _with_webdav_status(message: str) -> str:
        nonlocal webdav_error
        if not webdav_error:
            return message
        return message + bilingual_text(
            lang,
            f"；但 WebDAV 上传失败（{webdav_error}）",
            f"; however, WebDAV upload failed ({webdav_error})",
        )

    # WebDAV: 同步前从远程拉取最新存档到本地缓存
    if cfg and cfg.get("webdav_enabled") and HAS_WEBDAV:
        try:
            webdav_download_latest(
                cfg,
                game,
                dest_dir,
                cache_mirror_remote=using_webdav_cache,
            )
        except Exception as e:
            webdav_prefetch_error = f"{type(e).__name__}: {e}"

    local_info = snapshot_sync_specs(configured_specs, configured_paths[0] if len(configured_paths) == 1 else bilingual_text(lang, f"{len(configured_paths)} 个本地目录", f"{len(configured_paths)} local folders"))
    local_count = int(local_info.get("file_count", 0) or 0)
    local_hash = local_info.get("hash", "") if local_count else ""
    state = get_game_sync_state(cfg, game)
    base_local = state.get("local_hash", "")
    base_remote = state.get("remote_hash", "")
    def _build_remote_state(payload: Optional[dict]):
        remote_hash_value = payload.get("hash", "") if payload else ""
        remote_count_value = int(payload.get("file_count", 0) or 0) if payload else 0
        if payload:
            if payload.get("kind") == "archive":
                remote_label_value = str(payload.get("archive_path", dest_dir))
            else:
                remote_label_value = str(dest_dir)
            remote_info_value = {
                "path": remote_label_value,
                "file_count": remote_count_value,
                "hash": remote_hash_value,
                "latest_mtime": float(payload.get("latest_mtime", payload.get("timestamp", 0.0)) or 0.0),
            }
        else:
            remote_label_value = str(dest_dir)
            remote_info_value = {
                "path": remote_label_value,
                "file_count": 0,
                "hash": "",
                "latest_mtime": 0.0,
            }
        return remote_hash_value, remote_count_value, remote_label_value, remote_info_value

    remote_payload = None if (using_webdav_cache and webdav_prefetch_error) else get_remote_sync_payload(dest_dir, len(configured_paths))
    remote_hash, remote_count, remote_label, remote_info = _build_remote_state(remote_payload)
    should_refresh_local_mirror = (
        not using_webdav_cache
        and mode in {"download", "bidirectional"}
        and remote_payload
        and local_hash == remote_hash
    )
    if should_refresh_local_mirror:
        refreshed_payload = refresh_local_sync_payload(
            effective_sync_root,
            dest_dir,
            len(configured_paths),
            current_payload=remote_payload,
        )
        if _remote_sync_payload_signature(refreshed_payload) != _remote_sync_payload_signature(remote_payload):
            remote_payload = refreshed_payload
            remote_hash, remote_count, remote_label, remote_info = _build_remote_state(remote_payload)

    should_wait_for_cloud_payload = (
        not using_webdav_cache
        and mode in {"download", "bidirectional"}
        and remote_count == 0
        and not (mode == "bidirectional" and local_count > 0)
    )
    if should_wait_for_cloud_payload:
        refreshed_payload = refresh_local_sync_payload(
            effective_sync_root,
            dest_dir,
            len(configured_paths),
            current_payload=remote_payload,
            timeout=LOCAL_CLOUD_MISSING_PAYLOAD_TIMEOUT if auto else LOCAL_CLOUD_REFRESH_TIMEOUT,
            interval=LOCAL_CLOUD_REFRESH_INTERVAL,
        )
        if _remote_sync_payload_signature(refreshed_payload) != _remote_sync_payload_signature(remote_payload):
            remote_payload = refreshed_payload
            remote_hash, remote_count, remote_label, remote_info = _build_remote_state(remote_payload)

    # 预检：云端是 ZIP 归档但本地副本无效（云盘占位文件尚未同步完整）时，
    # 触发云盘刷新并等待；仍无效则按 auto 决定重试或返回友好提示，
    # 避免 extract_sync_archive 直接抛 BadZipFile。
    if (not using_webdav_cache
            and remote_payload
            and remote_payload.get("kind") == "archive"
            and mode in {"download", "bidirectional"}
            and local_hash != remote_hash):
        archive_path = Path(remote_payload["archive_path"])
        ok, zip_msg = validate_zip_archive(archive_path)
        if not ok and effective_sync_root and os.path.isdir(effective_sync_root):
            _poke_cloud_sync_folder(effective_sync_root)
            wait_timeout = LOCAL_CLOUD_MISSING_PAYLOAD_TIMEOUT if auto else LOCAL_CLOUD_REFRESH_TIMEOUT
            deadline = time.time() + max(0.0, wait_timeout)
            while time.time() < deadline:
                time.sleep(LOCAL_CLOUD_REFRESH_INTERVAL)
                ok, zip_msg = validate_zip_archive(archive_path)
                if ok:
                    break
            if not ok:
                not_ready_msg = bilingual_text(
                    lang,
                    f"等待：云端存档尚未完整同步到本机，请稍后重试（{zip_msg}）",
                    f"Waiting: the cloud save has not fully synced locally yet, please retry later ({zip_msg})",
                )
                if auto and not using_webdav_cache:
                    raise SyncPendingError(not_ready_msg)
                return not_ready_msg

    def _mirror(src: str, dst: str):
        """将 src 目录完整镜像到 dst（兼容 OneDrive/Dropbox 等云盘锁定）"""
        dst_p = Path(dst)
        dst_p.mkdir(parents=True, exist_ok=True)
        failures = []
        # 单次遍历 src：同时复制文件和收集源文件集合
        src_files = set()
        def _record_walk_error(err):
            failures.append(f"{getattr(err, 'filename', src)}: {type(err).__name__}: {err}")

        for root, _, files in os.walk(src, onerror=_record_walk_error):
            rel = os.path.relpath(root, src)
            target_dir = dst_p / rel if rel != "." else dst_p
            target_dir.mkdir(parents=True, exist_ok=True)
            for file_name in files:
                src_files.add(os.path.normpath(os.path.join(rel, file_name)))
                src_file = os.path.join(root, file_name)
                dst_file = target_dir / file_name
                try:
                    shutil.copy2(src_file, dst_file)
                except OSError:
                    time.sleep(0.5)
                    try:
                        shutil.copy2(src_file, dst_file)
                    except OSError as e:
                        failures.append(f"{src_file} -> {dst_file}: {type(e).__name__}: {e}")
        # 单次遍历 dst：删除 src 中不存在的文件
        for root, _, files in os.walk(str(dst_p), onerror=_record_walk_error):
            rel = os.path.relpath(root, str(dst_p))
            for file_name in files:
                rel_path = os.path.normpath(os.path.join(rel, file_name))
                if rel_path not in src_files:
                    try:
                        os.remove(os.path.join(root, file_name))
                    except OSError as e:
                        failures.append(f"{os.path.join(root, file_name)}: {type(e).__name__}: {e}")
        if failures:
            raise OSError("Mirror copy did not complete: " + "; ".join(failures[:5]))

    def _create_remote_archive():
        nonlocal webdav_error
        keep_count = 3
        if cfg:
            try:
                keep_count = max(0, int(cfg.get("sync_archive_keep", 3)))
            except Exception:
                keep_count = 3
        archive_path, meta_path = create_sync_archive(
            game, dest_dir, configured_specs, local_info, keep_count=keep_count
        )
        upload_ok = True
        # WebDAV: 上传存档到远程
        if webdav_active and archive_path:
            if not HAS_WEBDAV:
                webdav_error = WEBDAV_IMPORT_ERROR or "webdavclient3 not installed"
                upload_ok = False
            else:
                ok, upload_msg = webdav_upload_archive(cfg, str(archive_path), str(meta_path), game)
                if not ok:
                    webdav_error = upload_msg or "upload_failed"
                    upload_ok = False
                else:
                    webdav_error = ""
                    try:
                        webdav_enforce_archive_limits(cfg, game, keep_count)
                    except Exception:
                        pass
        return archive_path, upload_ok

    def _download_remote_payload(current_remote: Optional[dict] = None):
        if current_remote is None:
            current_remote = get_remote_sync_payload(dest_dir, len(configured_paths))
        if not current_remote:
            return
        if current_remote.get("kind") == "archive":
            archive_specs = current_remote.get("save_specs", [])
            restore_specs = (
                _normalize_unique_save_specs(archive_specs)
                if isinstance(archive_specs, list) and archive_specs
                else configured_specs
            )
            extract_sync_archive(Path(current_remote["archive_path"]), restore_specs)
            return
        if len(configured_paths) == 1:
            legacy_pairs = [(configured_paths[0], str(dest_dir))]
        else:
            legacy_pairs = [
                (local_path, str(dest_dir / f"path_{idx}"))
                for idx, local_path in enumerate(configured_paths, start=1)
            ]
        for local_path, remote_path in legacy_pairs:
            if os.path.isdir(remote_path):
                Path(local_path).mkdir(parents=True, exist_ok=True)
                _mirror(remote_path, local_path)

    def _record_synced(hash_value: str, action: str):
        with batch_config_save():
            set_game_sync_state(cfg, game, hash_value, hash_value, action)
            clear_game_sync_conflict(cfg, game)
            clear_sync_retry(cfg, game)

    def _record_current(action: str = ""):
        with batch_config_save():
            set_game_sync_state(cfg, game, local_hash, remote_hash, action)
            clear_game_sync_conflict(cfg, game)

    if using_webdav_cache and webdav_prefetch_error and mode in {"download", "bidirectional"}:
        return bilingual_text(
            lang,
            f"跳过：WebDAV 云端状态刷新失败（{webdav_prefetch_error}）",
            f"Skipped: failed to refresh WebDAV remote state ({webdav_prefetch_error})",
        )

    if mode == "upload":
        if local_count == 0:
            return bilingual_text(
                lang,
                "跳过：本地存档为空",
                "Skipped: local save folders are empty",
            )
        if local_hash == remote_hash:
            _record_current()
            return bilingual_text(lang, "跳过：本地与同步文件夹已一致", "Skipped: local and sync folders are already identical")
        _, upload_ok = _create_remote_archive()
        if using_webdav_cache and webdav_active and not upload_ok:
            return bilingual_text(
                lang,
                f"上传失败：WebDAV 上传未完成（{webdav_error or 'upload_failed'}）",
                f"Upload failed: WebDAV upload did not complete ({webdav_error or 'upload_failed'})",
            )
        create_backup(game, sync_tag)
        _record_synced(local_hash, "upload")
        return _with_webdav_status(bilingual_text(
            lang,
            f"↑ 已上传 ZIP（本地 → 云端），共 {local_count} 个文件",
            f"↑ Uploaded ZIP archive (local → cloud), {local_count} files",
        ))
    if mode == "download":
        if remote_count == 0:
            pending_message = bilingual_text(
                lang,
                "等待：本地同步文件夹尚未出现云端 ZIP，已请求云盘客户端刷新",
                "Waiting: the cloud ZIP is not available locally yet; requested the sync client to refresh",
            )
            if auto and not using_webdav_cache:
                raise SyncPendingError(pending_message)
            return bilingual_text(
                lang,
                "跳过：同步文件夹中无存档",
                "Skipped: no save files were found in the sync folder",
            )
        if local_hash == remote_hash:
            _record_current()
            return bilingual_text(lang, "跳过：本地与同步文件夹已一致", "Skipped: local and sync folders are already identical")
        create_backup(game, pre_sync_backup_tag)
        _download_remote_payload(remote_payload)
        create_backup(game, sync_tag)
        _record_synced(remote_hash, "download")
        return bilingual_text(lang, "↓ 已下载并解压 ZIP（云端 → 本地）", "↓ Downloaded and extracted ZIP archive (cloud → local)")
    if mode == "bidirectional":
        if local_count == 0 and remote_count == 0:
            _record_current()
            return bilingual_text(lang, "跳过：两端存档均为空", "Skipped: both sides are empty")
        if local_count == 0:
            create_backup(game, pre_sync_backup_tag)
            _download_remote_payload(remote_payload)
            create_backup(game, sync_tag)
            _record_synced(remote_hash, "download")
            return bilingual_text(lang, "↓ 已下载并解压 ZIP（云端 → 本地）", "↓ Downloaded and extracted ZIP archive (cloud → local)")
        if remote_count == 0:
            _, upload_ok = _create_remote_archive()
            if using_webdav_cache and webdav_active and not upload_ok:
                return bilingual_text(
                    lang,
                    f"上传失败：WebDAV 上传未完成（{webdav_error or 'upload_failed'}）",
                    f"Upload failed: WebDAV upload did not complete ({webdav_error or 'upload_failed'})",
                )
            create_backup(game, sync_tag)
            _record_synced(local_hash, "upload")
            return _with_webdav_status(bilingual_text(
                lang,
                f"↑ 已上传 ZIP（本地 → 云端），共 {local_count} 个文件",
                f"↑ Uploaded ZIP archive (local → cloud), {local_count} files",
            ))
        if local_hash == remote_hash:
            _record_current()
            return bilingual_text(lang, "跳过：两端存档已一致", "Skipped: both sides are already identical")

        if state:
            local_changed = local_hash != base_local
            remote_changed = remote_hash != base_remote
            if local_changed and not remote_changed:
                _, upload_ok = _create_remote_archive()
                if using_webdav_cache and webdav_active and not upload_ok:
                    return bilingual_text(
                        lang,
                        f"上传失败：WebDAV 上传未完成（{webdav_error or 'upload_failed'}）",
                        f"Upload failed: WebDAV upload did not complete ({webdav_error or 'upload_failed'})",
                    )
                create_backup(game, sync_tag)
                _record_synced(local_hash, "upload")
                return _with_webdav_status(bilingual_text(
                    lang,
                    f"↑ 已上传 ZIP（本地 → 云端），共 {local_count} 个文件",
                    f"↑ Uploaded ZIP archive (local → cloud), {local_count} files",
                ))
            if remote_changed and not local_changed:
                create_backup(game, pre_sync_backup_tag)
                _download_remote_payload(remote_payload)
                create_backup(game, sync_tag)
                _record_synced(remote_hash, "download")
                return bilingual_text(lang, "↓ 已下载并解压 ZIP（云端 → 本地）", "↓ Downloaded and extracted ZIP archive (cloud → local)")
            if local_changed and remote_changed:
                if local_hash == remote_hash:
                    _record_current()
                    return bilingual_text(lang, "跳过：两端存档已一致", "Skipped: both sides are already identical")
                mark_game_sync_conflict(
                    cfg, game,
                    bilingual_text(lang, "本地和同步文件夹都发生了变更", "Both the local and sync copies changed"),
                    local_info, remote_info)
                return bilingual_text(
                    lang,
                    "冲突：本地和同步文件夹都发生了变更，请先选择“仅上传”或“仅下载”",
                    "Conflict: both the local and sync copies changed. Please choose Upload Only or Download Only first.",
                )

        mark_game_sync_conflict(
            cfg, game,
            bilingual_text(lang, "首次同步检测到两端内容不同", "The first sync found different content on both sides"),
            local_info, remote_info)
        return bilingual_text(
            lang,
            "冲突：首次同步检测到两端内容不同，请先选择“仅上传”或“仅下载”建立基线",
            "Conflict: the first sync found different content on both sides. Please choose Upload Only or Download Only to establish a baseline.",
        )
    return bilingual_text(lang, "同步完成", "Sync completed")


def sync_all_games(cfg: dict, auto: bool = False) -> list[tuple[str, str]]:
    """同步所有已添加游戏，返回 [(game_name, result_msg), ...]"""
    results = []
    sf = cfg.get("sync_folder", "")
    mode = cfg.get("sync_mode", "bidirectional")
    for g in cfg.get("games", []):
        if not is_game_sync_enabled(g):
            results.append((g.get("name", ""), bilingual_cfg(
                cfg,
                "跳过：此游戏已关闭存档自动同步",
                "Skipped: save sync is disabled for this game",
            )))
            continue
        try:
            r = sync_game_save(g, sf, mode, auto=auto, cfg=cfg)
        except SyncPendingError as e:
            r = str(e)
            if auto and mode in ("upload", "download"):
                enqueue_sync_retry(cfg, g, mode, str(e))
        except Exception as e:
            r = bilingual_cfg(
                cfg,
                f"失败：{type(e).__name__}: {e}",
                f"Failed: {type(e).__name__}: {e}",
            )
            if auto and mode in ("upload", "download"):
                enqueue_sync_retry(cfg, g, mode, str(e))
        results.append((g["name"], r))
    return results


def sanitize(name: str) -> str:
    return "".join(
        c if c.isalnum() or c in (" ", "-", "_") else "_" for c in name
    ).strip()[:80]


def fmt_size(size: int) -> str:
    s = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if s < 1024:
            return f"{s:.1f} {unit}"
        s /= 1024
    return f"{s:.1f} TB"


def _zip_safe_datetime(timestamp: float) -> tuple[int, int, int, int, int, int]:
    try:
        dt = datetime.datetime.fromtimestamp(max(float(timestamp), 0.0))
    except Exception:
        dt = datetime.datetime.now()
    if dt.year < 1980:
        dt = datetime.datetime(1980, 1, 1, 0, 0, 0)
    elif dt.year > 2107:
        dt = datetime.datetime(2107, 12, 31, 23, 59, 58)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def _open_zip_for_write(zip_path: Path | str):
    return zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, strict_timestamps=False)


def _atomic_zip_path(final_path: Path | str) -> Path:
    final = Path(final_path)
    return final.with_name(f".{final.stem}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp")


def _is_network_path(path: Path | str) -> bool:
    raw = str(path or "").strip()
    if raw.startswith("\\\\") or raw.startswith("//"):
        return True
    if sys.platform != "win32":
        return False
    try:
        return classify_storage_path(raw) == "network"
    except Exception:
        return False


def _local_zip_stage_path(final_path: Path | str) -> Path:
    final = Path(final_path)
    stage_dir = Path(tempfile.gettempdir()) / "SteamSaveManager" / "backup_staging"
    stage_dir.mkdir(parents=True, exist_ok=True)
    fd, stage_path = tempfile.mkstemp(
        prefix=f".{final.stem}.",
        suffix=".zip.tmp",
        dir=str(stage_dir),
    )
    os.close(fd)
    return Path(stage_path)


def _commit_staged_zip(staged_zip: Path, final_zip: Path):
    target_temp = _atomic_zip_path(final_zip)
    try:
        shutil.copyfile(staged_zip, target_temp)
        if target_temp.stat().st_size != staged_zip.stat().st_size:
            raise OSError(f"staged ZIP size mismatch for {final_zip}")
        target_temp.replace(final_zip)
    finally:
        try:
            target_temp.unlink(missing_ok=True)
        except Exception:
            pass


def _write_json_atomic(path: Path | str, payload: dict):
    final_path = Path(path)
    temp_path = _atomic_zip_path(final_path)
    try:
        with open(temp_path, "w", encoding="utf-8") as file_obj:
            json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        temp_path.replace(final_path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


def _zip_write_file(zf: zipfile.ZipFile, file_path: Path | str, arcname: Path | str):
    src_path = Path(file_path)
    arcname_str = Path(arcname).as_posix()
    st = src_path.stat()
    info = zipfile.ZipInfo(filename=arcname_str, date_time=_zip_safe_datetime(st.st_mtime))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (st.st_mode & 0xFFFF) << 16
    with src_path.open("rb") as src, zf.open(info, "w") as dst:
        shutil.copyfileobj(src, dst, length=1024 * 1024)


def package_save_folder_to_zip(folder: Path | str, zip_path: Path | str) -> int:
    source_dir = Path(folder)
    target_zip = Path(zip_path)
    temp_zip = _atomic_zip_path(target_zip)
    file_count = 0
    try:
        with _open_zip_for_write(temp_zip) as zf:
            for root, _, files in os.walk(source_dir):
                for file in files:
                    abs_file = Path(root) / file
                    rel = abs_file.relative_to(source_dir).as_posix()
                    if _is_ignored_save_payload_file(rel):
                        continue
                    _zip_write_file(zf, abs_file, rel)
                    file_count += 1
        if file_count <= 0:
            temp_zip.unlink(missing_ok=True)
            return 0
        temp_zip.replace(target_zip)
        return file_count
    except Exception:
        try:
            temp_zip.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def load_json_file_tolerant(path: Path | str, default=None, repair: bool = True):
    """宽容读取 JSON，允许尾部出现额外字符，并可自动修复文件。"""
    file_path = Path(path)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        pass
    except Exception:
        return default

    try:
        raw = file_path.read_text(encoding="utf-8")
    except Exception:
        return default

    cleaned = raw.lstrip("\ufeff")
    decoder = json.JSONDecoder()
    try:
        obj, end = decoder.raw_decode(cleaned)
    except Exception:
        return default

    trailing = cleaned[end:].strip()
    if trailing and repair:
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
    return obj


def create_backup(game: dict, note: str = "", extra_meta: Optional[dict] = None, *,
                  expected_source_signature: Optional[tuple] = None) -> Optional[str]:
    with SAVE_IO_LOCK:
        return _create_backup_impl(
            game,
            note,
            extra_meta,
            expected_source_signature=expected_source_signature,
        )


def _create_backup_impl(game: dict, note: str = "", extra_meta: Optional[dict] = None, *,
                        expected_source_signature: Optional[tuple] = None) -> Optional[str]:
    save_specs = get_game_save_specs(game, existing_only=True)
    if not save_specs:
        return None
    save_paths = [spec["base"] for spec in save_specs]
    if isinstance(game, dict) and (game.get("game_uid") or game.get("storage_key")):
        storage_key = ensure_game_storage_identity(game)
    else:
        storage_key = get_game_storage_key(game)
    game_dir = BACKUP_ROOT / storage_key
    game_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now()
    ts = now.strftime("%Y%m%d_%H%M%S_%f")
    zip_path = game_dir / f"{ts}.zip"
    suffix = 1
    while zip_path.exists() or zip_path.with_suffix(".meta.json").exists():
        zip_path = game_dir / f"{ts}_{suffix}.zip"
        suffix += 1
    is_multi = len(save_specs) > 1
    network_target = _is_network_path(zip_path)
    temp_zip_path = _local_zip_stage_path(zip_path) if network_target else _atomic_zip_path(zip_path)
    meta_path = zip_path.with_suffix(".meta.json")
    file_count = 0
    try:
        with _open_zip_for_write(temp_zip_path) as zf:
            for idx, _, abs_f, rel in iter_save_spec_files(save_specs):
                arc_prefix = Path("__multi__") / f"p{idx}" if is_multi else Path()
                arcname = arc_prefix / Path(rel) if is_multi else Path(rel)
                _zip_write_file(zf, abs_f, arcname)
                file_count += 1
        if file_count <= 0:
            try:
                temp_zip_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        if expected_source_signature is not None:
            current_signature = save_specs_metadata_signature(save_specs)
            if current_signature != expected_source_signature:
                raise SaveSourceChangedError("save files changed while the backup was being created")
        meta = {
            "game": game["name"], "appid": game.get("appid", ""),
            "game_uid": get_game_uid(game),
            "storage_key": storage_key,
            "timestamp": ts, "note": note,
            "source": str(save_paths[0]), "sources": save_paths,
            "save_specs": _normalize_unique_save_specs(save_specs),
            "multi_path": is_multi,
            "size": temp_zip_path.stat().st_size,
        }
        if isinstance(extra_meta, dict):
            meta.update(extra_meta)
        _write_json_atomic(meta_path, meta)
        try:
            if network_target:
                _commit_staged_zip(temp_zip_path, zip_path)
            else:
                temp_zip_path.replace(zip_path)
        except Exception:
            try:
                meta_path.unlink(missing_ok=True)
            except Exception as cleanup_error:
                logger.warning("清理未完成备份元数据失败 %s: %s", meta_path, cleanup_error)
            raise
    finally:
        try:
            temp_zip_path.unlink(missing_ok=True)
        except Exception:
            pass
    enforce_backup_limits(game)
    return str(zip_path)


def enforce_backup_limits(game_ref=None):
    """
    执行备份轮转策略：
    1. 每个游戏最多保留 max_backups_per_game 个备份（超出删除最旧的）
    2. 所有备份总大小不超过 max_backup_size_gb GB（超出从最旧开始删除）
    设为 0 表示不限制。
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = load_json_file_tolerant(CONFIG_FILE, default={}) or {}
    except Exception:
        return
    max_per_game = cfg.get("max_backups_per_game", 20)
    max_total_gb = cfg.get("max_backup_size_gb", 10.0)
    max_total_bytes = max_total_gb * 1024 * 1024 * 1024
    games = [g for g in cfg.get("games", []) if isinstance(g, dict)]
    targets = [game_ref] if game_ref else games
    for target in targets:
        if not target:
            continue
        backups = get_backups(target)  # 已按时间倒序
        if max_per_game > 0 and len(backups) > max_per_game:
            for b in backups[max_per_game:]:
                delete_backup(b["path"])
    # 2) 总容量限制
    if max_total_gb <= 0:
        return
    all_backups = []
    for game in games:
        for b in get_backups(game):
            all_backups.append(b)
    total_size = sum(b["size"] for b in all_backups)
    if total_size > max_total_bytes:
        # 从最旧的开始删除，直到总大小低于上限
        all_backups.sort(key=lambda x: x["timestamp"])
        for b in all_backups:
            if total_size <= max_total_bytes:
                break
            total_size -= b["size"]
            delete_backup(b["path"])


def migrate_backups(old_root: Path, new_root: Path) -> int:
    """
    将旧备份目录下的所有游戏备份迁移到新目录。
    返回成功迁移的条目数量。
    """
    if not old_root.exists() or os.path.normpath(str(old_root)) == os.path.normpath(str(new_root)):
        return 0
    items = list(old_root.iterdir())
    if not items:
        return 0
    new_root.mkdir(parents=True, exist_ok=True)
    moved = 0
    for item in items:
        dest = new_root / item.name
        try:
            if item.is_dir():
                if dest.exists():
                    # 目标已存在同名文件夹 → 逐文件合并
                    for root, dirs, files in os.walk(str(item)):
                        rel = os.path.relpath(root, str(item))
                        target_dir_p = dest / rel if rel != '.' else dest
                        target_dir_p.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            src_f = os.path.join(root, f)
                            dst_f = str(target_dir_p / f)
                            shutil.move(src_f, dst_f)
                    shutil.rmtree(str(item))
                else:
                    shutil.move(str(item), str(dest))
            else:
                shutil.move(str(item), str(dest))
            moved += 1
        except Exception:
            pass  # 跳过迁移失败的条目
    return moved


def _restore_safety_dir(target: Path) -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = target.parent / f"{target.name}_pre_restore_{stamp}"
    suffix = 1
    while candidate.exists():
        candidate = target.parent / f"{target.name}_pre_restore_{stamp}_{suffix}"
        suffix += 1
    return candidate


def _cleanup_old_restore_safety_dirs(target: Path, keep: int = RESTORE_SAFETY_KEEP_PER_TARGET):
    if keep <= 0:
        keep = 0
    parent = target.parent
    prefix = f"{target.name}_pre_restore_"
    try:
        candidates = [
            item for item in parent.iterdir()
            if item.is_dir() and item.name.startswith(prefix)
        ]
    except Exception as e:
        logger.debug("无法枚举还原安全备份目录 %s: %s", parent, e)
        return
    def _safe_mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except Exception:
            return 0.0

    candidates.sort(key=_safe_mtime, reverse=True)
    for item in candidates[keep:]:
        try:
            if _path_is_within(item, parent):
                shutil.rmtree(item)
        except Exception as e:
            logger.debug("清理旧还原安全备份目录失败 %s: %s", item, e)


def _copy_matching_spec_files_to_safety(specs: list[dict], safety_dir: Path) -> int:
    copied = 0
    seen = set()
    for _, spec, abs_f, rel in iter_save_spec_files(specs):
        try:
            key = os.path.normcase(os.path.abspath(str(abs_f)))
            if key in seen:
                continue
            seen.add(key)
            dest = _zip_safe_restore_path(safety_dir, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(abs_f, dest)
            copied += 1
        except Exception as e:
            raise OSError(f"Failed to create restore safety copy for {abs_f}: {e}") from e
    return copied


def _copy_safety_files_to_target(safety_dir: Path, target: Path):
    if not safety_dir.is_dir():
        return
    for root, _, files in os.walk(safety_dir):
        rel_root = os.path.relpath(root, safety_dir)
        target_dir = target if rel_root == "." else target / rel_root
        target_dir.mkdir(parents=True, exist_ok=True)
        for file_name in files:
            src_file = Path(root) / file_name
            dest_file = target_dir / file_name
            dest_file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, dest_file)


def _zip_safe_restore_path(base_dir: Path, relative_name: str) -> Path:
    normalized = (relative_name or "").replace("\\", "/")
    rel_path = PurePosixPath(normalized)
    if not normalized or rel_path.is_absolute():
        raise ValueError(f"Invalid archive entry: {relative_name}")
    parts = [part for part in rel_path.parts if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe archive entry: {relative_name}")
    base_resolved = base_dir.resolve(strict=False)
    dest = base_dir.joinpath(*parts)
    try:
        dest.resolve(strict=False).relative_to(base_resolved)
    except ValueError as exc:
        raise ValueError(f"Archive entry escapes target directory: {relative_name}") from exc
    return dest


def _plan_backup_restore_entries(zf: zipfile.ZipFile, target_specs: list[dict]) -> list[tuple[int, dict, str, Path]]:
    members = [
        info.filename for info in zf.infolist()
        if info.filename and not info.is_dir() and not _is_ignored_save_payload_file(info.filename)
    ]
    entries: list[tuple[int, dict, str, Path]] = []
    is_multi = any(name.startswith("__multi__/p") for name in members)
    if not is_multi:
        spec = target_specs[0]
        base_dir = Path(spec["base"])
        for member in members:
            if not _save_spec_match_relpath(spec, member):
                continue
            entries.append((0, spec, member, _zip_safe_restore_path(base_dir, member)))
        return entries
    for member in members:
        match = re.match(r"^__multi__/p(\d+)/(.*)$", member)
        if not match:
            continue
        idx = int(match.group(1)) - 1
        rel = match.group(2)
        if idx < 0 or not rel:
            continue
        if idx >= len(target_specs):
            raise RuntimeError(
                "Restore target is missing save path groups required by this backup. "
                "Please restore after reconfiguring the game's save paths."
            )
        target_idx = idx
        spec = target_specs[target_idx]
        if not _save_spec_match_relpath(spec, rel):
            continue
        entries.append((target_idx, spec, member, _zip_safe_restore_path(Path(spec["base"]), rel)))
    return entries


def restore_backup(zip_path: str, target_dir):
    backup_meta = load_json_file_tolerant(Path(zip_path).with_suffix(".meta.json"), default={}) or {}
    backup_specs = backup_meta.get("save_specs", []) if isinstance(backup_meta, dict) else []
    if isinstance(backup_specs, list) and backup_specs:
        target_specs = _normalize_unique_save_specs(backup_specs)
    elif isinstance(target_dir, list) and target_dir and isinstance(target_dir[0], dict):
        target_specs = get_game_save_specs({"save_specs": target_dir}, existing_only=False)
    else:
        targets = target_dir if isinstance(target_dir, list) else [target_dir]
        target_paths = _normalize_unique_paths([str(t) for t in targets if t])
        target_specs = [_default_save_spec(path) for path in target_paths]
    if not target_specs:
        return

    with zipfile.ZipFile(zip_path, "r") as zf:
        _validate_zip_extract_limits(zf, "backup archive")
        restore_entries = _plan_backup_restore_entries(zf, target_specs)
        if not restore_entries:
            return
        touched_dirs: dict[str, Path] = {}
        specs_by_target: dict[str, list[dict]] = {}
        for _, spec, _, _ in restore_entries:
            target = Path(spec["base"])
            target_key = os.path.normcase(os.path.normpath(str(target)))
            touched_dirs[target_key] = target
            specs_for_target = specs_by_target.setdefault(target_key, [])
            if not any(
                os.path.normcase(os.path.normpath(str(existing.get("base", "")))) == os.path.normcase(os.path.normpath(str(spec.get("base", ""))))
                and existing.get("includes", []) == spec.get("includes", [])
                and bool(existing.get("recursive", True)) == bool(spec.get("recursive", True))
                for existing in specs_for_target
            ):
                specs_for_target.append(spec)
        restore_records: dict[str, tuple[Path, Optional[Path], bool, list[dict]]] = {}
        for target_key, target in touched_dirs.items():
            specs_for_target = specs_by_target.get(target_key, [])
            if target.exists():
                safety_dir = _restore_safety_dir(target)
                safety_dir.mkdir(parents=True, exist_ok=True)
                _copy_matching_spec_files_to_safety(specs_for_target, safety_dir)
                restore_records[target_key] = (target, safety_dir, True, specs_for_target)
            else:
                target.mkdir(parents=True, exist_ok=True)
                restore_records[target_key] = (target, None, False, specs_for_target)
        try:
            prepared_targets = set()
            for target_idx, spec, _, _ in restore_entries:
                if target_idx in prepared_targets:
                    continue
                _remove_matching_spec_files(spec)
                prepared_targets.add(target_idx)
            for _, _, member, dest in restore_entries:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            for target, _, _, _ in restore_records.values():
                _cleanup_old_restore_safety_dirs(target)
        except Exception as restore_error:
            rollback_errors = []
            for target, safety_dir, existed_before, specs_for_target in restore_records.values():
                try:
                    if not existed_before:
                        if target.exists():
                            shutil.rmtree(target)
                        continue
                    target.mkdir(parents=True, exist_ok=True)
                    for spec in specs_for_target:
                        _remove_matching_spec_files(spec)
                    if safety_dir is not None and safety_dir.exists():
                        _copy_safety_files_to_target(safety_dir, target)
                except Exception as rollback_error:
                    rollback_errors.append(f"{target}: {type(rollback_error).__name__}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    f"Restore failed and rollback was incomplete: {restore_error}; "
                    + "; ".join(rollback_errors[:5])
                ) from restore_error
            raise RuntimeError(f"Restore failed and the previous save was restored: {restore_error}") from restore_error


def _get_backup_candidate_dirs(game_ref) -> list[Path]:
    if isinstance(game_ref, dict):
        candidate_dirs = [BACKUP_ROOT / get_game_storage_key(game_ref)]
        if not get_game_uid(game_ref):
            legacy_dir = BACKUP_ROOT / sanitize(str(game_ref.get("name", "") or ""))
            if legacy_dir not in candidate_dirs:
                candidate_dirs.append(legacy_dir)
    else:
        candidate_dirs = [BACKUP_ROOT / get_game_storage_key(str(game_ref or ""))]
    return candidate_dirs


def count_backups(game_ref) -> int:
    count = 0
    seen_paths = set()
    for game_dir in _get_backup_candidate_dirs(game_ref):
        if not game_dir.exists():
            continue
        for f in game_dir.glob("*.zip"):
            norm = os.path.normcase(str(f))
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            count += 1
    return count


def get_backups(game_ref, limit: Optional[int] = None) -> list:
    candidate_dirs = _get_backup_candidate_dirs(game_ref)
    backups = []
    seen_paths = set()
    for game_dir in candidate_dirs:
        if not game_dir.exists():
            continue
        for f in sorted(game_dir.glob("*.zip"), reverse=True):
            norm = os.path.normcase(str(f))
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            meta_file = f.with_suffix(".meta.json")
            meta = {}
            if meta_file.exists():
                meta = load_json_file_tolerant(meta_file, default={}) or {}
            raw_save_specs = meta.get("save_specs", []) if isinstance(meta, dict) else []
            save_specs = (
                _normalize_unique_save_specs(raw_save_specs)
                if isinstance(raw_save_specs, list) and raw_save_specs
                else []
            )
            backups.append({
                "path": str(f), "filename": f.name,
                "timestamp": meta.get("timestamp", f.stem),
                "note": meta.get("note", ""),
                "size": meta.get("size", f.stat().st_size),
                "backup_kind": meta.get("backup_kind", ""),
                "linked_sync_archive": meta.get("linked_sync_archive", ""),
                "game_uid": meta.get("game_uid", ""),
                "storage_key": meta.get("storage_key", ""),
                "save_specs": save_specs,
            })
            if limit is not None and len(backups) >= limit:
                break
    backups.sort(key=lambda item: str(item.get("timestamp", "")), reverse=True)
    if limit is not None:
        backups = backups[:max(0, int(limit))]
    return backups


def delete_backup(zip_path: str):
    p = Path(zip_path)
    if p.exists(): p.unlink()
    m = p.with_suffix(".meta.json")
    if m.exists(): m.unlink()


def delete_linked_sync_archive(cfg: Optional[dict], game_ref, archive_name: str) -> dict:
    result = {"local_deleted": False, "webdav_deleted": False, "errors": []}
    storage_key = get_game_storage_key(game_ref)
    if not cfg or not storage_key or not archive_name:
        return result

    local_root = get_effective_sync_root(str(cfg.get("sync_folder", "") or "").strip(), cfg, ensure=True)
    if local_root and os.path.isdir(local_root):
        archive_root = get_sync_archive_root(get_sync_game_dir(local_root, game_ref))
        try:
            matches = list(archive_root.rglob(archive_name)) if archive_root.exists() else []
        except Exception:
            matches = []
        for archive_path in matches:
            try:
                archive_path.unlink()
                result["local_deleted"] = True
            except Exception as e:
                result["errors"].append(f"local:{type(e).__name__}: {e}")
            try:
                meta_path = get_sync_archive_meta_path(archive_path)
                if meta_path.exists():
                    meta_path.unlink()
            except Exception as e:
                result["errors"].append(f"local-meta:{type(e).__name__}: {e}")

    if cfg.get("webdav_enabled") and HAS_WEBDAV:
        client = _webdav_make_client(cfg)
        if client:
            deleted = False
            for remote_dir in _webdav_archive_dirs_for_lookup(client, cfg, game_ref):
                try:
                    _webdav_clean_with_variants(client, f"{remote_dir.rstrip('/')}/{archive_name}")
                    deleted = True
                except Exception as e:
                    result["errors"].append(f"webdav:{type(e).__name__}: {e}")
                try:
                    _webdav_clean_with_variants(client, f"{remote_dir.rstrip('/')}/{archive_name}.meta.json")
                except Exception:
                    pass
            result["webdav_deleted"] = deleted

    return result


# ══════════════════════════════════════════════
#  智能云存档 —— 游戏进程监控
# ══════════════════════════════════════════════

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class GameProcessMonitor:
    """
    监控游戏进程的启动和关闭，实现智能云存档：
    - 检测到游戏启动 → 自动下载云端存档
    - 检测到游戏关闭 → 自动上传本地存档

    检测策略（优先级从高到低）：
    1. Steam 注册表 RunningAppID / Apps Running 值（最可靠，零开销）
    2. gameoverlayui.exe 命令行参数中的 AppID（支持多游戏并行）
    3. 用户手动指定的进程名（精确匹配）
    4. 无 AppID 游戏的保守进程名关键词匹配（兜底）
    """

    def __init__(self, cfg: dict, poll_interval: int = 10, *,
                 on_backups_changed=None, on_game_stopped=None):
        self.cfg = cfg
        self.poll_interval = poll_interval  # 秒
        self._stop_event = threading.Event()
        self._running_games: set[str] = set()  # 当前正在运行的 appid 集合
        self._running_games_lock = threading.Lock()
        self._state_ready = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.sync_log: list[str] = []  # 同步活动日志（最近50条）
        self._upload_guards: dict[str, dict] = {}
        self._on_backups_changed_cb = on_backups_changed
        self._on_game_stopped_cb = on_game_stopped
        self._fuzzy_scan_cache: tuple[float, set[str]] = (0.0, set())

    def start(self):
        _join_thread(self._thread, timeout=0.2)
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        thread = self._thread
        self._stop_event.set()
        _join_thread(thread, timeout=2.0)
        if self._thread is thread and (thread is None or not thread.is_alive()):
            self._thread = None
        with self._running_games_lock:
            self._running_games.clear()
        self._state_ready.clear()
        self._upload_guards.clear()

    def add_activity(self, zh: str, en: str):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        message = bilingual_cfg(self.cfg, zh, en)
        self.sync_log.append(f"[{timestamp}] {message}")
        self.sync_log = self.sync_log[-50:]

    def is_game_running(self, game: Optional[dict]) -> bool:
        runtime_key = get_game_runtime_key(game)
        if not runtime_key:
            return False
        if not self._state_ready.is_set():
            try:
                return runtime_key in self._find_running_games()
            except Exception:
                return False
        with self._running_games_lock:
            return runtime_key in self._running_games

    def _build_sync_snapshots(self, game: dict, sync_folder: str) -> tuple[dict, dict, Optional[dict]]:
        save_specs = get_game_save_specs(game, existing_only=False)
        save_paths = get_game_save_paths(game, existing_only=False)
        if not save_specs:
            fallback = str(game.get("save_path", "") or "").strip()
            save_specs = [_default_save_spec(fallback)] if fallback else []
            save_paths = [fallback] if fallback else []
        local_label = save_paths[0] if len(save_paths) == 1 and save_paths else bilingual_cfg(
            self.cfg,
            f"{len(save_paths)} 个本地目录",
            f"{len(save_paths)} local folders",
        )
        local_info = snapshot_sync_specs(save_specs, local_label)
        dest_dir = ensure_shared_sync_game_dir(sync_folder, game)
        remote_payload = get_remote_sync_payload(dest_dir, len(save_paths))
        if remote_payload:
            if remote_payload.get("kind") == "archive":
                remote_label = str(remote_payload.get("archive_path", dest_dir))
            else:
                remote_label = str(dest_dir)
            remote_info = {
                "path": remote_label,
                "file_count": int(remote_payload.get("file_count", 0) or 0),
                "hash": str(remote_payload.get("hash", "") or ""),
                "latest_mtime": float(remote_payload.get("latest_mtime", remote_payload.get("timestamp", 0.0)) or 0.0),
            }
        else:
            remote_info = {
                "path": str(dest_dir),
                "file_count": 0,
                "hash": "",
                "latest_mtime": 0.0,
            }
        return local_info, remote_info, remote_payload

    def _arm_upload_guard_after_launch(self, game: dict, sync_folder: str) -> bool:
        local_info, remote_info, remote_payload = self._build_sync_snapshots(game, sync_folder)
        remote_count = int(remote_info.get("file_count", 0) or 0)
        if remote_count <= 0:
            return False
        local_hash = str(local_info.get("hash", "") or "")
        remote_hash = str(remote_info.get("hash", "") or "")
        if local_hash == remote_hash:
            return False
        key = get_game_sync_key(game)
        self._upload_guards[key] = {
            "reason": bilingual_cfg(
                self.cfg,
                "游戏启动后才检测到云端存档更新，为避免覆盖云端，已保护本轮会话并禁止自动上传",
                "A newer cloud save was detected only after the game had already started. Automatic upload is blocked for this session to avoid overwriting the cloud copy.",
            ),
            "armed_at": datetime.datetime.now().isoformat(timespec="seconds"),
            "local": dict(local_info),
            "remote": dict(remote_info),
        }
        return True

    def _consume_upload_guard(self, game: dict) -> Optional[dict]:
        return self._upload_guards.pop(get_game_sync_key(game), None)

    def _wait_for_save_settle(self, game: dict,
                              timeout: float = 12.0,
                              interval: float = 2.0) -> float:
        """
        等待游戏退出后存档目录稳定，避免游戏延迟落盘导致过早上传。
        返回实际等待秒数。
        """
        save_paths = get_game_save_paths(game, existing_only=True)
        save_specs = get_game_save_specs(game, existing_only=True)
        if not save_specs:
            return 0.0

        def _sig():
            snapshot = snapshot_sync_specs(save_specs, save_paths[0] if save_paths else "")
            return (int(snapshot.get("file_count", 0) or 0), snapshot.get("hash", ""))

        start = time.monotonic()
        deadline = start + max(0.0, timeout)
        last_sig = _sig()

        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= deadline:
                break
            wait_s = min(interval, deadline - now)
            if wait_s <= 0:
                break
            self._stop_event.wait(wait_s)
            if self._stop_event.is_set():
                break
            current_sig = _sig()
            if current_sig == last_sig:
                break
            last_sig = current_sig

        return max(0.0, time.monotonic() - start)

    def _get_tracked_games(self) -> list[tuple[str, dict]]:
        tracked = []
        for game in self.cfg.get("games", []):
            if not isinstance(game, dict):
                continue
            key = get_game_runtime_key(game)
            if not key:
                continue
            tracked.append((key, game))
        return tracked

    @staticmethod
    def _process_name_candidates(proc) -> set[str]:
        candidates = set()
        try:
            name = (proc.info.get("name") or "").strip()
            clean = _normalize_monitor_process_name(name)
            if clean:
                candidates.update(_monitor_process_variants(clean))
        except Exception:
            pass
        return candidates

    # ── 策略 1: Steam 注册表 RunningAppID ──
    @staticmethod
    def _get_running_appid_from_registry() -> str:
        """
        读取 Steam 注册表中的 RunningAppID。
        Steam 运行游戏时会将此值设为当前游戏的 AppID，
        没有游戏运行时为 0。
        这是最可靠、开销最低的检测方式。
        """
        if not winreg:
            return ""
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Valve\Steam",
                0, winreg.KEY_READ)
            val, _ = winreg.QueryValueEx(key, "RunningAppID")
            winreg.CloseKey(key)
            appid = str(val).strip()
            return appid if appid and appid != "0" else ""
        except Exception:
            return ""

    # ── 策略 1b: Steam Apps 注册表逐个检查 Running 状态 ──
    @staticmethod
    def _get_running_appids_from_apps_registry(known_appids: set[str]) -> set[str]:
        r"""
        遍历 HKCU\Software\Valve\Steam\Apps\{appid}，
        检查每个已知游戏的 Running 值（DWORD）。
        Running = 1 表示游戏正在运行。
        这是 RunningAppID 的补充方案，更可靠。
        """
        running = set()
        if not winreg:
            return running
        for appid in known_appids:
            try:
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    rf"SOFTWARE\Valve\Steam\Apps\{appid}",
                    0, winreg.KEY_READ)
                try:
                    val, _ = winreg.QueryValueEx(key, "Running")
                    if val and int(val) == 1:
                        running.add(appid)
                except (FileNotFoundError, ValueError, OSError):
                    pass
                winreg.CloseKey(key)
            except Exception:
                continue
        return running

    # ── 策略 2: gameoverlayui.exe 命令行提取 AppID ──
    @staticmethod
    def _get_appids_from_overlay() -> set[str]:
        """
        扫描所有 gameoverlayui.exe 进程的命令行参数，
        提取其中的 AppID。Steam 为每个运行中的游戏都会
        启动一个 overlay 进程，命令行中包含 -appid <id>。
        支持检测多个同时运行的游戏。
        """
        appids = set()
        if not HAS_PSUTIL:
            return appids
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if name != "gameoverlayui.exe":
                    continue
                try:
                    cmdline = proc.cmdline()
                except (psutil.AccessDenied, psutil.NoSuchProcess):
                    continue
                # 查找 -appid 参数
                for i, arg in enumerate(cmdline):
                    if arg.lower() in ("-appid", "--appid") and i + 1 < len(cmdline):
                        aid = cmdline[i + 1].strip()
                        if aid.isdigit() and aid != "0":
                            appids.add(aid)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
        return appids

    # ── 策略 3: 进程名关键词匹配（兜底） ──
    SKIP_PROCESSES = {
        "steam.exe", "steamwebhelper.exe", "steamerrorreporter.exe",
        "steamservice.exe", "gameoverlayui.exe", "uninstall.exe",
        "crashhandler.exe", "vc_redist", "dxsetup.exe",
        "dotnetfx", "directx", "setup.exe", "installer.exe",
        "redist.exe", "vcredist.exe", "update.exe", "updater.exe",
        "launcher.exe",
    }
    SKIP_PREFIXES = (
        "svchost", "csrss", "wininit", "services", "lsass",
        "explorer", "dwm", "taskhostw", "runtimebroker",
        "searchhost", "ctfmon", "conhost", "cmd", "powershell",
        "python", "node", "code", "chrome", "firefox", "edge",
        "msedge", "discord", "spotify", "wechat", "qq",
    )

    @staticmethod
    def _extract_keywords(game_name: str) -> list[str]:
        GENERIC_WORDS = {
            "the", "of", "and", "for", "in", "on", "at", "to",
            "a", "an", "is", "it", "by", "or", "as", "be",
            "game", "edition", "remastered", "remake", "deluxe",
            "goty", "ultimate", "complete", "definitive",
            "iii", "ii", "iv", "vi", "vii", "viii", "ix",
        }
        raw = re.findall(r'[a-zA-Z0-9\u4e00-\u9fff]+', game_name.lower())
        return [w for w in raw
                if (len(w) >= 3 or w.isdigit()) and w not in GENERIC_WORDS]

    @staticmethod
    def _process_name_tokens(process_name: str) -> set[str]:
        proc_base = os.path.splitext(str(process_name or "").lower())[0]
        return set(re.findall(r'[a-zA-Z0-9\u4e00-\u9fff]+', proc_base))

    @classmethod
    def _process_name_matches_keywords(cls, keywords: list[str], process_name: str) -> bool:
        if not keywords:
            return False
        proc_base = os.path.splitext(str(process_name or "").lower())[0]
        tokens = cls._process_name_tokens(proc_base)
        hits = sum(1 for kw in keywords if kw in tokens)
        if len(keywords) >= 2:
            return hits >= 2
        keyword = keywords[0]
        if re.search(r"[\u4e00-\u9fff]", keyword):
            return keyword == proc_base or keyword in tokens
        return len(keyword) >= 5 and (keyword == proc_base or keyword in tokens)

    def _get_runtime_keys_from_process_names(self, tracked_games: list[tuple[str, dict]]) -> set[str]:
        """
        通过进程名关键词匹配检测正在运行的无 AppID 游戏。
        Steam 游戏只走 AppID / overlay / 手动进程名，避免短关键词误判。
        """
        matched: set[str] = set()
        if not HAS_PSUTIL:
            return matched
        now = time.monotonic()
        cache_at, cache_result = self._fuzzy_scan_cache
        if now - cache_at < 30.0:
            return set(cache_result)

        # 构建关键词映射。只给无 AppID 且未手动指定进程名的游戏兜底，
        # 避免 Steam 游戏被短关键词（如 Cat Quest II -> cat）误识别。
        game_kw_map = []
        for runtime_key, g in tracked_games:
            if str(g.get("appid", "") or "").strip():
                continue
            if get_game_monitor_processes(g):
                continue
            kws = self._extract_keywords(g["name"])
            if kws:
                game_kw_map.append((kws, runtime_key))
        if not game_kw_map:
            self._fuzzy_scan_cache = (now, set())
            return matched

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if not name or name in self.SKIP_PROCESSES:
                    continue
                if any(name.startswith(p) for p in self.SKIP_PREFIXES):
                    continue
                for kws, runtime_key in game_kw_map:
                    if self._process_name_matches_keywords(kws, name):
                        matched.add(runtime_key)
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied,
                    psutil.ZombieProcess):
                continue
        self._fuzzy_scan_cache = (now, set(matched))
        return matched

    def _get_runtime_keys_from_custom_processes(self, tracked_games: list[tuple[str, dict]]) -> set[str]:
        matched: set[str] = set()
        if not HAS_PSUTIL:
            return matched
        process_map: list[tuple[set[str], str]] = []
        for runtime_key, game in tracked_games:
            variants = set()
            for process_name in get_game_monitor_processes(game):
                variants.update(_monitor_process_variants(process_name))
            if variants:
                process_map.append((variants, runtime_key))
        if not process_map:
            return matched

        for proc in psutil.process_iter(["pid", "name"]):
            try:
                candidates = self._process_name_candidates(proc)
                if not candidates:
                    continue
                for variants, runtime_key in process_map:
                    if candidates & variants:
                        matched.add(runtime_key)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
        return matched

    def _find_running_games(self) -> set[str]:
        """
        综合多层检测策略，返回当前正在运行的游戏记录键集合。
        """
        tracked_games = self._get_tracked_games()
        appid_to_keys: dict[str, set[str]] = {}
        for runtime_key, game in tracked_games:
            appid = str(game.get("appid", "") or "").strip()
            if appid:
                appid_to_keys.setdefault(appid, set()).add(runtime_key)
        known_appids = set(appid_to_keys.keys())
        running: set[str] = set()

        # 策略 1a: 注册表 RunningAppID
        reg_appid = self._get_running_appid_from_registry()
        if reg_appid and reg_appid in known_appids:
            running.update(appid_to_keys.get(reg_appid, set()))

        # 策略 1b: Steam Apps 注册表逐个检查 Running 值
        apps_running = self._get_running_appids_from_apps_registry(known_appids)
        for appid in apps_running:
            running.update(appid_to_keys.get(appid, set()))

        # 策略 2: gameoverlayui.exe 命令行（支持多游戏并行）
        overlay_appids = self._get_appids_from_overlay()
        for appid in overlay_appids & known_appids:
            running.update(appid_to_keys.get(appid, set()))

        # 策略 3: 手动指定的进程名（支持非 Steam 游戏和进程误识别修正）
        running.update(self._get_runtime_keys_from_custom_processes(tracked_games))

        # 策略 4: 进程名关键词匹配（兜底）
        if not running:
            running.update(self._get_runtime_keys_from_process_names(tracked_games))

        return running

    def diagnose(self) -> str:
        """诊断进程检测，返回详细调试信息"""
        zh = cfg_language(self.cfg) == "zh-CN"
        lines = []
        lines.append("=== 游戏进程诊断 ===" if zh else "=== Game Process Diagnostics ===")
        lines.append(f"{'winreg 可用' if zh else 'winreg available'}: {winreg is not None}")
        lines.append(f"{'psutil 可用' if zh else 'psutil available'}: {HAS_PSUTIL}")
        lines.append("")

        # 已添加游戏
        games = [g for g in self.cfg.get("games", []) if isinstance(g, dict)]
        tracked_games = self._get_tracked_games()
        known = {get_game_runtime_key(g): g.get("name", "?") for g in games}
        known_appids = {str(g.get("appid", "") or "").strip(): g.get("name", "?") for g in games if str(g.get("appid", "") or "").strip()}
        lines.append(f"已添加游戏 ({len(games)} 个):" if zh else f"Tracked games ({len(games)}):")
        for runtime_key, game in tracked_games:
            name = game.get("name", "?")
            kws = self._extract_keywords(name)
            monitor_hint = get_game_monitor_processes(game)
            suffix = (
                f" | 自定义进程: {monitor_hint}" if zh and monitor_hint else
                f" | custom processes: {monitor_hint}" if monitor_hint else
                ""
            )
            lines.append(
                f"  🎮 {name} ({runtime_key}) -> 关键词: {kws}{suffix}"
                if zh else
                f"  🎮 {name} ({runtime_key}) -> keywords: {kws}{suffix}"
            )
        lines.append("")

        # 策略 1a: 注册表 RunningAppID
        reg_appid = self._get_running_appid_from_registry()
        if reg_appid:
            reg_name = known_appids.get(reg_appid, "未添加" if zh else "Not tracked")
            lines.append(
                f"✅ 注册表 RunningAppID = {reg_appid} ({reg_name})"
                if zh else
                f"✅ Registry RunningAppID = {reg_appid} ({reg_name})"
            )
        else:
            lines.append("❌ 注册表 RunningAppID = 0（无游戏运行）" if zh else "❌ Registry RunningAppID = 0 (no game running)")
        lines.append("")

        # 策略 1b: Steam Apps 注册表 Running 值
        apps_running = self._get_running_appids_from_apps_registry(set(known_appids.keys()))
        if apps_running:
            lines.append(
                f"✅ Steam Apps 注册表检测到 {len(apps_running)} 个游戏运行中:"
                if zh else
                f"✅ Steam Apps registry detected {len(apps_running)} running game(s):"
            )
            for aid in apps_running:
                name = known_appids.get(aid, "未添加" if zh else "Not tracked")
                lines.append(f"  AppID: {aid} ({name})")
        else:
            lines.append("❌ Steam Apps 注册表未检测到游戏运行" if zh else "❌ Steam Apps registry did not detect any running game")
        lines.append("")

        # 策略 2: overlay
        overlay_ids = self._get_appids_from_overlay()
        if overlay_ids:
            lines.append(
                f"✅ gameoverlayui.exe 检测到 {len(overlay_ids)} 个游戏:"
                if zh else
                f"✅ gameoverlayui.exe detected {len(overlay_ids)} game(s):"
            )
            for aid in overlay_ids:
                name = known_appids.get(aid, "未添加" if zh else "Not tracked")
                lines.append(f"  AppID: {aid} ({name})")
        else:
            lines.append("❌ 未检测到 gameoverlayui.exe 或无 AppID 参数" if zh else "❌ gameoverlayui.exe was not found, or it had no AppID argument")
        lines.append("")

        # 策略 3: 自定义进程匹配
        custom_ids = self._get_runtime_keys_from_custom_processes(tracked_games)
        if custom_ids:
            lines.append(
                f"✅ 自定义进程匹配到 {len(custom_ids)} 个游戏:"
                if zh else
                f"✅ Custom process matching found {len(custom_ids)} game(s):"
            )
            for runtime_key in custom_ids:
                lines.append(f"  {runtime_key} ({known.get(runtime_key, '未添加' if zh else 'Not tracked')})")
        else:
            lines.append("❌ 自定义进程未匹配到游戏" if zh else "❌ Custom process matching did not find any game")
        lines.append("")

        # 策略 4: 进程名匹配
        proc_ids = self._get_runtime_keys_from_process_names(tracked_games)
        if proc_ids:
            lines.append(
                f"✅ 进程名关键词匹配到 {len(proc_ids)} 个游戏:"
                if zh else
                f"✅ Process-name matching found {len(proc_ids)} game(s):"
            )
            for runtime_key in proc_ids:
                lines.append(f"  {runtime_key} ({known.get(runtime_key, '未添加' if zh else 'Not tracked')})")
        else:
            lines.append("❌ 进程名关键词未匹配到游戏" if zh else "❌ Process-name matching did not find any game")
        lines.append("")

        # 综合结果
        total = self._find_running_games()
        if total:
            lines.append(
                f"🎯 综合结果：检测到 {len(total)} 个游戏正在运行:"
                if zh else
                f"🎯 Final result: {len(total)} game(s) detected as running:"
            )
            for runtime_key in total:
                name = known.get(runtime_key, "未添加" if zh else "Not tracked")
                lines.append(f"  ✅ {name} ({runtime_key})")
        else:
            lines.append("🎯 综合结果：未检测到游戏运行" if zh else "🎯 Final result: no running game detected")
            lines.append("")
            lines.append("排查建议:" if zh else "Troubleshooting:")
            lines.append("  1. 确认游戏是否通过 Steam 启动" if zh else "  1. Make sure the game was launched through Steam")
            lines.append("  2. 确认 Steam 客户端正在运行" if zh else "  2. Make sure the Steam client is running")
            lines.append("  3. 确认同步模式为「智能云存档」且同步已启用" if zh else "  3. Make sure sync is enabled and the mode is set to Smart Cloud Save")
            lines.append("  4. 如果游戏不通过 Steam 启动，进程名需包含游戏名关键词" if zh else "  4. If the game is launched outside Steam, its process name should still contain the game keywords")

        return "\n".join(lines)

    def _monitor_loop(self):
        all_game_by_runtime_key = {
            get_game_runtime_key(g): g
            for g in self.cfg.get("games", [])
            if isinstance(g, dict) and get_game_runtime_key(g)
        }
        game_by_runtime_key = {
            get_game_runtime_key(g): g
            for g in self.cfg.get("games", [])
            if isinstance(g, dict) and is_game_sync_enabled(g) and get_game_runtime_key(g)
        }
        sync_folder = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=True)
        smart_sync_active = bool(
            self.cfg.get("sync_enabled") and self.cfg.get("sync_mode") == "smart"
        )
        def _log(msg: str):
            _ts_log = datetime.datetime.now().strftime("%H:%M:%S")
            self.sync_log.append(f"[{_ts_log}] {msg}")
            self.sync_log = self.sync_log[-50:]

        retry_results = (
            run_sync_retries(self.cfg, self.cfg.get("sync_folder", ""), log_cb=_log)
            if smart_sync_active and sync_folder else []
        )
        if retry_results:
            cb = self._on_backups_changed_cb
            if cb:
                try:
                    cb()
                except Exception:
                    pass

        # 初始扫描，记录已在运行的游戏并立即触发一次下载同步
        initial_running = self._find_running_games()
        with self._running_games_lock:
            self._running_games = set(initial_running)
        self._state_ready.set()
        _ts = datetime.datetime.now().strftime("%H:%M:%S")
        if smart_sync_active:
            self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                self.cfg,
                f"📂 同步后端: {sync_folder or '未设置'}",
                f"📂 Sync backend: {sync_folder or 'Not set'}",
            ))
        if initial_running:
            _names = [all_game_by_runtime_key.get(a, {}).get("name", a) for a in initial_running]
            self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                self.cfg,
                f"🟢 监控启动，已在运行: {', '.join(_names)}",
                f"🟢 Monitor started, already running: {', '.join(_names)}",
            ))
            # 对已在运行的游戏也触发一次下载（确保云端存档已拉取到本地）
            for aid in initial_running:
                g = game_by_runtime_key.get(aid)
                if smart_sync_active and g and sync_folder:
                    try:
                        guarded = self._arm_upload_guard_after_launch(g, sync_folder)
                        r = sync_game_save(g, sync_folder, "download",
                                           auto=True, cfg=self.cfg)
                        _cb = self._on_backups_changed_cb
                        if _cb:
                            try:
                                _cb(dict(g))
                            except Exception:
                                pass
                        _ts2 = datetime.datetime.now().strftime("%H:%M:%S")
                        self.sync_log.append(f"[{_ts2}] " + bilingual_cfg(
                            self.cfg,
                            f"↓ {g['name']}(初始同步): {r}",
                            f"↓ {g['name']} (initial sync): {r}",
                        ))
                        if guarded:
                            self.sync_log.append(f"[{_ts2}] " + bilingual_cfg(
                                self.cfg,
                                f"🛡 {g['name']} 已启用启动保护：本轮运行结束前将跳过自动上传",
                                f"🛡 Startup protection enabled for {g['name']}: automatic upload will be skipped for this run",
                            ))
                        if self.cfg.get("sync_notify", True):
                            send_desktop_notification(
                                bilingual_cfg(self.cfg, "存档管家 · 初始同步", "Steam Save Manager · Initial Sync"),
                                bilingual_cfg(self.cfg, f"「{g['name']}」{r}", f"{g['name']}: {r}"),
                            )
                    except SyncPendingError as e:
                        enqueue_sync_retry(self.cfg, g, "download", str(e))
                        _ts2 = datetime.datetime.now().strftime("%H:%M:%S")
                        self.sync_log.append(f"[{_ts2}] " + bilingual_cfg(
                            self.cfg,
                            f"⏳ {g['name']} 初始下载等待云盘同步: {e}",
                            f"⏳ Initial download for {g['name']} is waiting for cloud sync: {e}",
                        ))
                    except Exception as e:
                        enqueue_sync_retry(self.cfg, g, "download", str(e))
                        _ts2 = datetime.datetime.now().strftime("%H:%M:%S")
                        self.sync_log.append(f"[{_ts2}] " + bilingual_cfg(
                            self.cfg,
                            f"❌ {g['name']} 初始下载失败: {e}",
                            f"❌ Initial download failed for {g['name']}: {e}",
                        ))
                    self.sync_log = self.sync_log[-50:]
        else:
            self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                self.cfg,
                "🟢 监控启动，当前无游戏运行",
                "🟢 Monitor started, no game is currently running",
            ))

        while not self._stop_event.is_set():
            self._stop_event.wait(self.poll_interval)
            if self._stop_event.is_set():
                break

            # 重新加载配置（可能有新游戏添加）
            sync_folder = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=True)
            smart_sync_active = bool(
                self.cfg.get("sync_enabled") and self.cfg.get("sync_mode") == "smart"
            )
            retry_results = (
                run_sync_retries(self.cfg, self.cfg.get("sync_folder", ""), log_cb=_log)
                if smart_sync_active and sync_folder else []
            )
            if retry_results:
                _cb = self._on_backups_changed_cb
                if _cb:
                    try:
                        _cb()
                    except Exception:
                        pass
            game_by_runtime_key = {
                get_game_runtime_key(g): g
                for g in self.cfg.get("games", [])
                if isinstance(g, dict) and is_game_sync_enabled(g) and get_game_runtime_key(g)
            }
            all_game_by_runtime_key = {
                get_game_runtime_key(g): g
                for g in self.cfg.get("games", [])
                if isinstance(g, dict) and get_game_runtime_key(g)
            }

            current = self._find_running_games()
            with self._running_games_lock:
                prev_ids = set(self._running_games)
                self._running_games = set(current)
            curr_ids = current

            # 新启动的游戏 → 下载云端存档
            for aid in curr_ids - prev_ids:
                g = game_by_runtime_key.get(aid)
                _ts = datetime.datetime.now().strftime("%H:%M:%S")
                if smart_sync_active and g and sync_folder:
                    try:
                        r = sync_game_save(g, sync_folder, "download",
                                           auto=True, cfg=self.cfg)
                        _cb = self._on_backups_changed_cb
                        if _cb:
                            try:
                                _cb(dict(g))
                            except Exception:
                                pass
                        self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                            self.cfg,
                            f"↓ {g['name']}: {r}",
                            f"↓ {g['name']}: {r}",
                        ))
                        if self.cfg.get("sync_notify", True):
                            send_desktop_notification(
                                bilingual_cfg(self.cfg, "存档管家 · 云存档下载", "Steam Save Manager · Cloud Save Download"),
                                bilingual_cfg(self.cfg, f"「{g['name']}」{r}", f"{g['name']}: {r}"),
                            )
                    except SyncPendingError as e:
                        enqueue_sync_retry(self.cfg, g, "download", str(e))
                        self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                            self.cfg,
                            f"⏳ {g['name']} 下载等待云盘同步: {e}",
                            f"⏳ Download for {g['name']} is waiting for cloud sync: {e}",
                        ))
                    except Exception as e:
                        enqueue_sync_retry(self.cfg, g, "download", str(e))
                        self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                            self.cfg,
                            f"❌ {g['name']} 下载失败: {e}",
                            f"❌ Download failed for {g['name']}: {e}",
                        ))
                elif smart_sync_active and g:
                    self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                        self.cfg,
                        f"⚠ {g['name']} 启动，但同步目录未设置",
                        f"⚠ {g['name']} started, but no sync backend is available",
                    ))
                self.sync_log = self.sync_log[-50:]

            # 关闭的游戏 → 上传本地存档
            for aid in prev_ids - curr_ids:
                runtime_game = all_game_by_runtime_key.get(aid)
                callback = self._on_game_stopped_cb
                if runtime_game and callable(callback):
                    try:
                        callback(dict(runtime_game))
                    except Exception:
                        logger.exception("处理游戏退出后的自动备份失败: %s", runtime_game.get("name", ""))
                g = game_by_runtime_key.get(aid)
                _ts = datetime.datetime.now().strftime("%H:%M:%S")
                if smart_sync_active and g and sync_folder:
                    try:
                        guard = self._consume_upload_guard(g)
                        if guard:
                            local_info, remote_info, _ = self._build_sync_snapshots(g, sync_folder)
                            mark_game_sync_conflict(
                                self.cfg,
                                g,
                                guard.get("reason", bilingual_cfg(
                                    self.cfg,
                                    "本轮运行中云端存档是在游戏启动后才同步到本地，已为安全起见跳过自动上传",
                                    "The cloud save was synchronized only after the game had already started in this session, so automatic upload was skipped for safety.",
                                )),
                                local_info,
                                remote_info,
                            )
                            self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                                self.cfg,
                                f"🛡 {g['name']}：已跳过自动上传，避免覆盖云端存档",
                                f"🛡 {g['name']}: automatic upload skipped to avoid overwriting the cloud save",
                            ))
                            if self.cfg.get("sync_notify", True):
                                send_desktop_notification(
                                    bilingual_cfg(self.cfg, "存档管家 · 上传已保护", "Steam Save Manager · Upload Protected"),
                                    bilingual_cfg(
                                        self.cfg,
                                        f"「{g['name']}」本轮运行已跳过自动上传，请确认后手动同步",
                                        f"Automatic upload was skipped for {g['name']} in this session. Please sync manually after checking the save.",
                                    ),
                                )
                            self.sync_log = self.sync_log[-50:]
                            continue
                        waited = self._wait_for_save_settle(g)
                        if self._stop_event.is_set():
                            break
                        if waited >= 1.0:
                            self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                                self.cfg,
                                f"⏳ {g['name']} 退出，等待存档落盘 {waited:.1f} 秒",
                                f"⏳ {g['name']} exited, waiting {waited:.1f}s for saves to settle",
                            ))
                        r = sync_game_save(g, sync_folder, "upload",
                                           auto=True, cfg=self.cfg)
                        _cb = self._on_backups_changed_cb
                        if _cb:
                            try:
                                _cb(dict(g))
                            except Exception:
                                pass
                        self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                            self.cfg,
                            f"↑ {g['name']}: {r}",
                            f"↑ {g['name']}: {r}",
                        ))
                        if self.cfg.get("sync_notify", True):
                            send_desktop_notification(
                                bilingual_cfg(self.cfg, "存档管家 · 云存档上传", "Steam Save Manager · Cloud Save Upload"),
                                bilingual_cfg(self.cfg, f"「{g['name']}」{r}", f"{g['name']}: {r}"),
                            )
                    except Exception as e:
                        enqueue_sync_retry(self.cfg, g, "upload", str(e))
                        self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                            self.cfg,
                            f"❌ {g['name']} 上传失败: {e}",
                            f"❌ Upload failed for {g['name']}: {e}",
                        ))
                elif smart_sync_active and g:
                    self.sync_log.append(f"[{_ts}] " + bilingual_cfg(
                        self.cfg,
                        f"⚠ {g['name']} 关闭，但同步目录未设置",
                        f"⚠ {g['name']} closed, but no sync backend is available",
                    ))
                self.sync_log = self.sync_log[-50:]

# ══════════════════════════════════════════════
#  文件监控（watchdog）
# ══════════════════════════════════════════════

class SaveChangeHandler(FileSystemEventHandler):
    def __init__(self, game: dict, cooldown: int = 60, *, cfg: Optional[dict] = None,
                 on_backup_created=None, is_game_running=None, on_status=None):
        super().__init__()
        self.game = game
        self.cfg = cfg
        self.cooldown = cooldown
        self.on_backup_created = on_backup_created
        self._is_game_running_cb = is_game_running
        self._on_status_cb = on_status
        self._last_backup = 0
        self._quiet_delay = 2.5
        self._timer = None
        self._timer_generation = 0
        self._timer_lock = threading.Lock()
        self._specs = get_game_save_specs(game, existing_only=False)
        self._pending = False
        self._pending_note = "文件变动自动备份"
        self._backup_running = False
        self._deferred_for_running = False
        self._bypass_cooldown = False
        self._closed = False
        self._last_event_at = 0.0
        self._stop_event = threading.Event()
        self._stable_samples = 3
        self._stable_interval = 1.0
        self._settle_timeout = 20.0
        self._backup_attempts = 3
        self._retry_backoff = (1.0, 2.0)
        self._failure_retry_delay = 30.0
        self._running_retry_delay = 5.0
        self._retryable_failure = False
        self._clock = time.monotonic
        self._timer_factory = threading.Timer

    def on_modified(self, event):
        self._handle_event(event)

    def on_created(self, event):
        self._handle_event(event)

    def on_moved(self, event):
        self._handle_event(event)

    def on_deleted(self, event):
        self._handle_event(event)

    def _handle_event(self, event):
        if getattr(event, "is_directory", False):
            return
        paths = [
            getattr(event, "src_path", "") or "",
            getattr(event, "dest_path", "") or "",
        ]
        paths = [path for path in paths if path]
        if not paths:
            return
        if self._specs and not any(_save_specs_match_path(self._specs, path) for path in paths):
            return
        self._schedule_backup("文件变动自动备份")

    def _emit_status(self, zh: str, en: str):
        callback = self._on_status_cb
        if not callable(callback):
            return
        try:
            callback(zh, en)
        except Exception:
            pass

    def _is_game_running(self) -> bool:
        callback = self._is_game_running_cb
        if not callable(callback):
            return False
        try:
            return bool(callback(self.game))
        except Exception:
            logger.exception("检查游戏运行状态失败: %s", self.game.get("name", ""))
            return False

    def _schedule_backup(self, note: str = "文件变动自动备份"):
        now = self._clock()
        with self._timer_lock:
            if self._closed:
                return
            first_pending_event = not self._pending
            self._pending = True
            self._pending_note = note or self._pending_note
            self._last_event_at = now
            if first_pending_event and note == "文件变动自动备份":
                self._emit_status(
                    f"📝 检测到 {self.game.get('name', '')} 存档变化，已加入自动备份队列",
                    f"📝 Save changes detected for {self.game.get('name', '')}; automatic backup queued",
                )
            if self._backup_running:
                return
            self._arm_timer_locked(self._quiet_delay)

    def request_scheduled_backup(self):
        self._schedule_backup("定时自动备份")

    def on_game_stopped(self, *, force: bool = False) -> bool:
        now = self._clock()
        with self._timer_lock:
            if self._closed or (not force and not self._pending):
                return False
            self._pending = True
            self._pending_note = "游戏退出后自动备份"
            self._deferred_for_running = False
            self._bypass_cooldown = True
            self._last_event_at = now
            if not self._backup_running:
                self._arm_timer_locked(self._quiet_delay)
        self._emit_status(
            f"⏳ {self.game.get('name', '')} 已退出，正在等待存档稳定后备份",
            f"⏳ {self.game.get('name', '')} exited; waiting for the save to settle before backup",
        )
        return True

    def _arm_timer_locked(self, delay: float):
        if self._closed:
            return
        if self._timer:
            try:
                self._timer.cancel()
            except Exception:
                pass
        self._timer_generation += 1
        generation = self._timer_generation
        self._timer = self._timer_factory(
            max(0.0, float(delay)),
            lambda: self._try_backup(generation),
        )
        self._timer.daemon = True
        self._timer.start()

    def _next_eligible_delay_locked(self, now: float) -> float:
        quiet_until = self._last_event_at + self._quiet_delay
        cooldown_until = (
            now if self._bypass_cooldown
            else self._last_backup + self.cooldown if self._last_backup else now
        )
        return max(0.0, quiet_until - now, cooldown_until - now)

    def _perform_backup_with_retry(self) -> bool:
        note = localize_backup_note(self._pending_note, cfg_language(self.cfg))
        last_error = None
        self._retryable_failure = False
        self._emit_status(
            f"⏳ 正在确认 {self.game.get('name', '')} 存档文件已稳定",
            f"⏳ Checking that save files for {self.game.get('name', '')} have settled",
        )
        for attempt in range(max(1, int(self._backup_attempts))):
            if self._stop_event.is_set():
                return False
            try:
                specs = get_game_save_specs(self.game, existing_only=True)
                if not specs:
                    return False
                stable_signature = wait_for_save_metadata_stable(
                    specs,
                    stable_samples=self._stable_samples,
                    interval=self._stable_interval,
                    timeout=self._settle_timeout,
                    stop_event=self._stop_event,
                )
                if stable_signature is None:
                    if self._stop_event.is_set():
                        return False
                    raise SaveSourceChangedError("save files did not settle before the backup deadline")
                if self._stop_event.is_set():
                    return False
                result = create_backup(
                    self.game,
                    note,
                    expected_source_signature=stable_signature,
                )
                if not result:
                    return False
                if not self._stop_event.is_set() and callable(self.on_backup_created):
                    try:
                        self.on_backup_created(self.game)
                    except Exception:
                        pass
                return True
            except OSError as exc:
                last_error = exc
                if attempt + 1 >= max(1, int(self._backup_attempts)):
                    break
                delay = (
                    self._retry_backoff[min(attempt, len(self._retry_backoff) - 1)]
                    if self._retry_backoff else 0.0
                )
                logger.debug(
                    "文件监控备份暂时失败，%.1f 秒后重试（%s/%s）：%s",
                    delay, attempt + 1, self._backup_attempts, exc,
                )
                self._emit_status(
                    f"⏳ {self.game.get('name', '')} 备份读取失败，{delay:.1f} 秒后重试",
                    f"⏳ Backup read failed for {self.game.get('name', '')}; retrying in {delay:.1f}s",
                )
                if self._stop_event.wait(max(0.0, float(delay))):
                    return False
        if last_error is not None:
            self._retryable_failure = not self._stop_event.is_set()
            logger.warning(
                "文件监控备份在 %s 次尝试后失败 %s: %s",
                self._backup_attempts,
                self.game.get("name", ""),
                last_error,
            )
            self._emit_status(
                f"❌ {self.game.get('name', '')} 自动备份失败: {last_error}",
                f"❌ Automatic backup failed for {self.game.get('name', '')}: {last_error}",
            )
        return False

    def _try_backup(self, timer_generation: Optional[int] = None):
        deferred_now = False
        with self._timer_lock:
            if timer_generation is not None and timer_generation != self._timer_generation:
                return
            self._timer = None
            if self._closed or not self._pending or self._backup_running:
                return
            if self._is_game_running():
                deferred_now = not self._deferred_for_running
                self._deferred_for_running = True
                self._arm_timer_locked(self._running_retry_delay)
                if deferred_now:
                    self._emit_status(
                        f"⏸ {self.game.get('name', '')} 正在运行，自动备份已推迟到游戏退出后",
                        f"⏸ {self.game.get('name', '')} is running; automatic backup was deferred until exit",
                    )
                return
            now = self._clock()
            delay = self._next_eligible_delay_locked(now)
            if delay > 0:
                self._arm_timer_locked(delay)
                return
            self._pending = False
            self._backup_running = True
            self._bypass_cooldown = False

        success = False
        try:
            success = self._perform_backup_with_retry()
        except Exception:
            self._retryable_failure = False
            logger.exception("文件监控备份发生非预期错误: %s", self.game.get("name", ""))
        finally:
            with self._timer_lock:
                self._backup_running = False
                now = self._clock()
                if success:
                    self._last_backup = now
                    self._deferred_for_running = False
                    self._emit_status(
                        f"✅ {self.game.get('name', '')} 自动备份完成",
                        f"✅ Automatic backup completed for {self.game.get('name', '')}",
                    )
                retry_failed_backup = bool(
                    not success
                    and self._retryable_failure
                    and not self._closed
                    and not self._stop_event.is_set()
                    and get_game_save_specs(self.game, existing_only=True)
                )
                if retry_failed_backup:
                    self._pending = True
                    delay = max(
                        self._next_eligible_delay_locked(now),
                        max(0.0, float(self._failure_retry_delay)),
                    )
                    self._arm_timer_locked(delay)
                elif self._pending and not self._closed:
                    self._arm_timer_locked(self._next_eligible_delay_locked(now))

    def close(self):
        self._stop_event.set()
        with self._timer_lock:
            self._closed = True
            self._pending = False
            self._timer_generation += 1
            if self._timer:
                try:
                    self._timer.cancel()
                except Exception:
                    pass
                self._timer = None


# ══════════════════════════════════════════════
#  主应用 GUI
# ══════════════════════════════════════════════

# ══════════════════════════════════════════════
#  全局字体 & 配色
# ══════════════════════════════════════════════

if sys.platform == "win32":
    FONT_FAMILY = "Microsoft YaHei UI"
elif sys.platform == "darwin":
    FONT_FAMILY = "PingFang SC"
else:
    FONT_FAMILY = "WenQuanYi Micro Hei"


def font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    return ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)


# 配色常量（浅色, 深色）
C_SIDEBAR_BG   = ("#ffffff", "#181926")
C_SIDEBAR_SEP  = ("#ececec", "#2c2d40")
C_NAV_TEXT     = ("#667085", "#a1a1aa")
C_NAV_HOVER    = ("#eef2ff", "#26283d")
C_NAV_ACTIVE   = ("#e0e7ff", "#2d3055")
C_NAV_ACT_TEXT = ("#4f46e5", "#a5b4fc")
C_CARD_BG      = ("#ffffff", "#1e1f33")
C_MAIN_BG      = ("#f8fafc", "#111218")
C_SUBTLE_TEXT  = ("#94a3b8", "#71717a")
C_BODY_TEXT    = ("#334155", "#e2e8f0")

# 按钮色
BTN_PRIMARY    = "#6366f1"   # indigo
BTN_PRIMARY_H  = "#4f46e5"
BTN_SUCCESS    = "#10b981"   # emerald
BTN_SUCCESS_H  = "#059669"
BTN_WARN       = "#f59e0b"   # amber
BTN_WARN_H     = "#d97706"
BTN_DANGER     = "#ef4444"   # red
BTN_DANGER_H   = "#dc2626"
BTN_BLUE       = "#3b82f6"
BTN_BLUE_H     = "#2563eb"
BTN_SECONDARY  = ("#e2e8f0", "#303247")
BTN_SECONDARY_H = ("#cbd5e1", "#3f4158")


# ══════════════════════════════════════════════
#  主应用 GUI
# ══════════════════════════════════════════════

class SteamSaveManager(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        clear_startup_caches(self.cfg)
        self.lang = normalize_language(self.cfg.get("language"))
        self.auto_backup_running = False
        self._auto_stop_event = threading.Event()
        self._auto_thread: Optional[threading.Thread] = None
        self._sync_running = False
        self._sync_stop = threading.Event()
        self._sync_thread: Optional[threading.Thread] = None
        self._watchers: list[Any] = []
        self._watch_handlers: list[SaveChangeHandler] = []
        self._watch_handlers_by_game: dict[str, SaveChangeHandler] = {}
        self._game_monitor: Optional[GameProcessMonitor] = None
        self._detail_refresh_job = None  # 详情页自动刷新定时器
        self._sync_ui_queue: queue.Queue = queue.Queue()
        self._open_conflict_dialogs: set[str] = set()
        self._resolving_sync_conflicts: set[str] = set()
        self._current_frame = "home"
        self._about_update_status_label = None
        self._about_update_btn = None
        self._about_dialog = None
        self._sidebar_version_label = None
        self._sidebar_version_text = f"v{VERSION}"
        self._sidebar_version_color = ("#64748b", "#71717a")
        self._update_manifest_cache: Optional[dict] = None
        self._scan_results: list[dict] = []
        self._scan_lib_folders: list[str] = []
        self._scan_choice_vars: dict[str, ctk.StringVar] = {}
        self._scan_choice_labels: dict[str, dict[str, str]] = {}
        self._scan_cards: dict[str, dict] = {}
        self._steam_account_lookup_cache = {"steam_path": None, "accountid_map": {}, "steam64_map": {}}
        self._scan_info_banner = None
        self._scan_empty_label = None
        self._scan_search_job = None
        self._games_search_job = None
        self._games_page = 0
        self._games_page_size = clamp_int(self.cfg.get("games_page_size", 25), 25, 5, 200)
        self._game_backup_count_cache: dict[str, int] = {}
        self._game_backups_cache: dict[str, list] = {}
        self._recent_backups_cache: dict[int, list] = {}
        self._game_detail_metrics_cache: dict[str, dict] = {}
        self._game_cards: dict[str, dict] = {}
        self._games_empty_label = None
        self._games_page_label = None
        self._games_prev_btn = None
        self._games_next_btn = None
        self._home_recent_rows: dict[str, dict] = {}
        self._home_recent_empty_label = None
        self._backup_page = 0
        self._backup_page_size = clamp_int(self.cfg.get("backup_page_size", 30), 30, 5, 200)
        self._backup_rows: dict[str, dict] = {}
        self._backup_empty_label = None
        self._backup_page_label = None
        self._backup_prev_btn = None
        self._backup_next_btn = None
        self._scan_in_progress = False
        self._scan_worker_count = 0
        self._scan_storage_profile = "unknown"
        self._scan_start_btn = None
        self._scan_add_all_btn = None
        self._scan_cancel_btn = None
        self._scan_progress = None
        self._scan_cancel_event = threading.Event()
        self._io_busy = False
        self._busy_widgets: weakref.WeakSet = weakref.WeakSet()
        self._responsive_labels: list[tuple[Any, float, int]] = []
        self._responsive_job = None
        self._settings_section_positions: dict[str, Any] = {}
        self._popup_windows: weakref.WeakSet = weakref.WeakSet()
        self._popup_restore_grab: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()

        self.title(self.t("product_title"))
        self.geometry("1080x720")
        self.minsize(920, 620)
        self._center_main_window()
        ctk.set_appearance_mode(self.cfg.get("theme", "light"))
        ctk.set_default_color_theme("blue")
        self.configure(fg_color=C_MAIN_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Map>", self._on_root_map, add="+")
        self.bind("<Configure>", self._schedule_responsive_update, add="+")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_sidebar()
        self._frames: dict[str, ctk.CTkFrame] = {}
        self._build_home_frame()
        self._build_scan_frame()
        self._build_games_frame()
        self._build_backup_frame()
        self._build_settings_frame()
        self._build_game_detail_frame()
        self._show_frame("home")

        if self.cfg.get("auto_backup_enabled"):
            self._start_auto_backup()
        if self.cfg.get("sync_enabled"):
            self._start_sync()

        self._ensure_game_monitor()
        if self._backup_handlers_should_run():
            self._start_watchers()
        self.after(1200, self._drain_sync_ui_queue)
        self.after(1800, self._check_for_updates_silent)
        if STARTUP_LAUNCH:
            self.after(200, self._apply_startup_launch_behavior)

    def t(self, key: str, **kwargs) -> str:
        return translate(self.lang, key, **kwargs)

    def bi(self, zh: str, en: str) -> str:
        return bilingual_text(self.lang, zh, en)

    def _theme_display(self, theme_code: str) -> str:
        return self.t("theme_dark" if theme_code == "dark" else "theme_light")

    def _theme_code(self, display: str) -> str:
        return "dark" if display == self.t("theme_dark") else "light"

    def _apply_startup_launch_behavior(self):
        if not STARTUP_LAUNCH:
            return
        if not self.cfg.get("autostart_minimize", True):
            return
        if self.cfg.get("minimize_to_tray", True) and HAS_TRAY:
            self.withdraw()
            self._create_tray()
            return
        self.iconify()

    def _sync_mode_options(self) -> list[tuple[str, str]]:
        return [
            ("smart", self.t("sync_mode_smart")),
            ("bidirectional", self.t("sync_mode_bidirectional")),
            ("upload", self.t("sync_mode_upload")),
            ("download", self.t("sync_mode_download")),
        ]

    def _sync_mode_display(self, mode_code: str) -> str:
        mapping = dict(self._sync_mode_options())
        return mapping.get(mode_code, mapping["smart"])

    def _sync_mode_code(self, display: str) -> str:
        reverse = {label: code for code, label in self._sync_mode_options()}
        return reverse.get(display, "smart")

    def _language_display(self, lang_code: str) -> str:
        return LANGUAGE_NAMES.get(normalize_language(lang_code), "English")

    def _language_code(self, display: str) -> str:
        for code, label in LANGUAGE_NAMES.items():
            if label == display:
                return code
        return "en"

    def _register_busy_widget(self, widget):
        try:
            self._busy_widgets.add(widget)
            if self._io_busy:
                widget.configure(state="disabled")
        except Exception:
            pass
        return widget

    def _register_wrap(self, widget, fraction: float = 0.7, minimum: int = 220):
        try:
            self._responsive_labels.append((widget, float(fraction), int(minimum)))
            self._schedule_responsive_update()
        except Exception:
            pass
        return widget

    def _schedule_responsive_update(self, _event=None):
        if self._responsive_job is not None:
            return
        try:
            self._responsive_job = self.after(80, self._update_responsive_text)
        except Exception:
            self._responsive_job = None

    def _update_responsive_text(self):
        self._responsive_job = None
        try:
            available = max(self.winfo_width() - 260, 360)
        except Exception:
            available = 720
        alive = []
        for widget, fraction, minimum in self._responsive_labels:
            try:
                if not widget.winfo_exists():
                    continue
                wrap = max(minimum, int(available * fraction))
                widget.configure(wraplength=wrap)
                alive.append((widget, fraction, minimum))
            except Exception:
                pass
        self._responsive_labels = alive

    def _copy_to_clipboard(self, text: str, message: Optional[str] = None):
        try:
            self.clipboard_clear()
            self.clipboard_append(str(text or ""))
            if message:
                self._show_info(self.bi("已复制", "Copied"), message)
        except Exception as e:
            self._show_error(self.bi("复制失败", "Copy Failed"), str(e))

    def _attach_tooltip(self, widget, text_getter):
        state = {"tip": None, "job": None}

        def _text():
            try:
                return text_getter() if callable(text_getter) else str(text_getter)
            except Exception:
                return ""

        def _hide(_event=None):
            if state["job"] is not None:
                try:
                    widget.after_cancel(state["job"])
                except Exception:
                    pass
                state["job"] = None
            tip = state.get("tip")
            if tip is not None:
                try:
                    tip.destroy()
                except Exception:
                    pass
                state["tip"] = None

        def _show():
            _hide()
            text = _text()
            if not text:
                return
            tip = ctk.CTkToplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            box = ctk.CTkFrame(tip, fg_color=("#ffffff", "#1f2937"), corner_radius=6)
            box.pack()
            label = ctk.CTkLabel(
                box,
                text=text,
                font=font(11),
                justify="left",
                wraplength=520,
                text_color=C_BODY_TEXT,
            )
            label.pack(padx=10, pady=7)
            try:
                x = widget.winfo_rootx() + 12
                y = widget.winfo_rooty() + widget.winfo_height() + 8
                tip.geometry(f"+{x}+{y}")
            except Exception:
                pass
            state["tip"] = tip

        def _enter(_event=None):
            _hide()
            state["job"] = widget.after(450, _show)

        widget.bind("<Enter>", _enter, add="+")
        widget.bind("<Leave>", _hide, add="+")
        widget.bind("<ButtonPress>", _hide, add="+")
        return widget

    def _rebuild_ui(self, target_frame: Optional[str] = None):
        current = target_frame or getattr(self, "_current_frame", "home")
        detail_idx = getattr(self, "_detail_idx", None)
        saved_state = {
            "scan_search": self._scan_search_var.get() if hasattr(self, "_scan_search_var") else "",
            "games_search": self._games_search_var.get() if hasattr(self, "_games_search_var") else "",
            "backup_filter": self._bk_var.get() if hasattr(self, "_bk_var") else "",
        }
        self._stop_detail_refresh()
        self.unbind_all("<MouseWheel>")
        for child in list(self.winfo_children()):
            child.destroy()
        self.title(self.t("product_title"))
        self._frames = {}
        self._nav_buttons = {}
        self._busy_widgets = weakref.WeakSet()
        self._responsive_labels = []
        self._settings_section_positions = {}
        self._build_sidebar()
        self._build_home_frame()
        self._build_scan_frame()
        self._build_games_frame()
        self._build_backup_frame()
        self._build_settings_frame()
        self._build_game_detail_frame()
        if saved_state["scan_search"] and hasattr(self, "_scan_search_var"):
            self._scan_search_var.set(saved_state["scan_search"])
        if saved_state["games_search"] and hasattr(self, "_games_search_var"):
            self._games_search_var.set(saved_state["games_search"])
        if saved_state["backup_filter"] and hasattr(self, "_bk_var"):
            self._bk_var.set(saved_state["backup_filter"])
        if current == "game_detail" and detail_idx is not None and 0 <= detail_idx < len(self.cfg.get("games", [])):
            self._show_game_detail(detail_idx)
        else:
            self._show_frame(current if current in self._frames else "home")
        self._update_status()
        self._apply_sidebar_version_state()
        self._set_io_busy(self._io_busy)
        self._schedule_responsive_update()

    def _center_main_window(self):
        try:
            self.update_idletasks()
            width = max(self.winfo_width(), self.winfo_reqwidth(), 1080)
            height = max(self.winfo_height(), self.winfo_reqheight(), 720)
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            x = max((screen_width - width) // 2, 0)
            y = max((screen_height - height) // 2, 0)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except Exception:
            pass

    def _center_window(self, window):
        try:
            self.update_idletasks()
            window.update_idletasks()
            if self.winfo_viewable():
                px = self.winfo_rootx()
                py = self.winfo_rooty()
                pw = max(self.winfo_width(), self.winfo_reqwidth())
                ph = max(self.winfo_height(), self.winfo_reqheight())
            else:
                sw = self.winfo_screenwidth()
                sh = self.winfo_screenheight()
                px = sw // 4
                py = sh // 4
                pw = sw // 2
                ph = sh // 2
            ww = max(window.winfo_width(), window.winfo_reqwidth())
            wh = max(window.winfo_height(), window.winfo_reqheight())
            x = px + max((pw - ww) // 2, 0)
            y = py + max((ph - wh) // 2, 0)
            window.geometry(f"+{x}+{y}")
            window.lift()
            window.focus_force()
        except Exception:
            pass

    def _prepare_dialog_parent(self):
        try:
            self.update_idletasks()
        except Exception:
            pass
        try:
            if self.state() == "iconic":
                self.deiconify()
        except Exception:
            pass
        try:
            self.lift()
        except Exception:
            pass
        try:
            self.focus_force()
        except Exception:
            pass
        self.after(60, self._restore_popup_windows)

    def _track_popup(self, window):
        try:
            self._popup_windows.add(window)
            self._popup_restore_grab[window] = False
        except Exception:
            pass
        window.bind("<Unmap>", lambda e, w=window: self._on_popup_unmap(w), add="+")
        window.bind("<Map>", lambda e, w=window: self._on_popup_map(w), add="+")
        window.bind("<Destroy>", lambda e, w=window: self._on_popup_destroy(w), add="+")
        return window

    def _on_popup_destroy(self, window):
        try:
            self._popup_restore_grab.pop(window, None)
        except Exception:
            pass

    def _on_popup_unmap(self, window):
        try:
            if not window.winfo_exists():
                return
            state = window.state()
            current = self.grab_current()
            if current == window and state == "iconic":
                self._popup_restore_grab[window] = True
                window.grab_release()
        except Exception:
            pass

    def _on_popup_map(self, window):
        self.after(80, lambda w=window: self._restore_single_popup(w))

    def _on_root_map(self, _event=None):
        self.after(80, self._restore_popup_windows)

    def _restore_single_popup(self, window):
        try:
            if not window.winfo_exists():
                return
            if self.state() == "iconic":
                return
            try:
                if window.state() == "iconic":
                    window.deiconify()
            except Exception:
                pass
            try:
                window.lift()
            except Exception:
                pass
            if self._popup_restore_grab.get(window):
                try:
                    window.grab_set()
                    self._popup_restore_grab[window] = False
                except Exception:
                    pass
        except Exception:
            pass

    def _restore_popup_windows(self):
        for window in list(self._popup_windows):
            self._restore_single_popup(window)

    def _prepare_popup(self, window):
        self._prepare_dialog_parent()
        self._track_popup(window)
        try:
            window.transient(self)
        except Exception:
            pass
        try:
            window.grab_set()
        except Exception:
            pass
        self.after(0, lambda w=window: self._center_window(w))
        self.after(120, lambda w=window: self._center_window(w) if w.winfo_exists() else None)
        return window

    def _create_popup(self, title: str, geometry: Optional[str] = None):
        window = ctk.CTkToplevel(self)
        window.title(title)
        if geometry:
            window.geometry(geometry)
        try:
            sw = self.winfo_screenwidth()
            sh = self.winfo_screenheight()
            window.maxsize(max(sw - 100, 400), max(sh - 100, 300))
        except Exception:
            pass
        self._prepare_popup(window)
        return window

    def _resolve_dialog_parent(self, parent=None):
        if parent is not None:
            try:
                if parent.winfo_exists():
                    return parent
            except Exception:
                return self
        about_dialog = getattr(self, "_about_dialog", None)
        try:
            if about_dialog is not None and about_dialog.winfo_exists():
                return about_dialog
        except Exception:
            pass
        return self

    def _show_info(self, title: str, message: str, parent=None):
        self._show_modal_message(title, message, "info", parent=parent)
        return "ok"

    def _show_warning(self, title: str, message: str, parent=None):
        self._show_modal_message(title, message, "warning", parent=parent)
        return "ok"

    def _show_error(self, title: str, message: str, parent=None):
        self._show_modal_message(title, message, "error", parent=parent)
        return "ok"

    def _ask_yes_no(self, title: str, message: str, parent=None):
        return self._show_modal_message(title, message, "question", parent=parent, buttons=[
            (self.bi("是", "Yes"), True, BTN_PRIMARY, BTN_PRIMARY_H),
            (self.bi("否", "No"), False, BTN_SECONDARY, BTN_SECONDARY_H),
        ])

    def _ask_yes_no_cancel(self, title: str, message: str, parent=None):
        return self._show_modal_message(title, message, "question", parent=parent, buttons=[
            (self.bi("是", "Yes"), True, BTN_PRIMARY, BTN_PRIMARY_H),
            (self.bi("否", "No"), False, BTN_SECONDARY, BTN_SECONDARY_H),
            (self.bi("取消", "Cancel"), None, BTN_DANGER, BTN_DANGER_H),
        ])

    def _show_modal_message(self, title: str, message: str, kind: str = "info", parent=None, buttons=None):
        owner = self._resolve_dialog_parent(parent)
        if owner is self:
            self._prepare_dialog_parent()
        else:
            try:
                owner.lift()
                owner.focus_force()
            except Exception:
                pass
        result = {"value": None}
        d = ctk.CTkToplevel(owner)
        d.title(title)
        d.geometry("520x260")
        self._prepare_popup(d)
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(1, weight=1)

        colors = {
            "info": ("i", BTN_BLUE),
            "warning": ("!", BTN_WARN),
            "error": ("!", BTN_DANGER),
            "question": ("?", BTN_PRIMARY),
        }
        glyph, color = colors.get(kind, colors["info"])
        hdr = ctk.CTkFrame(d, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=18, pady=(18, 8), sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            hdr, text=glyph, width=30, height=30, font=font(16, "bold"),
            fg_color=color, text_color="#ffffff", corner_radius=15,
        ).grid(row=0, column=0, padx=(0, 10), sticky="w")
        title_label = ctk.CTkLabel(
            hdr, text=title, font=font(16, "bold"),
            text_color=C_BODY_TEXT, anchor="w", justify="left",
        )
        title_label.grid(row=0, column=1, sticky="ew")
        self._register_wrap(title_label, 0.58, 280)

        body = ctk.CTkTextbox(d, font=font(12), wrap="word", fg_color=C_CARD_BG)
        body.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="nsew")
        body.insert("1.0", str(message or ""))
        body.configure(state="disabled")

        btns = ctk.CTkFrame(d, fg_color="transparent")
        btns.grid(row=2, column=0, padx=18, pady=(0, 18), sticky="e")
        if buttons is None:
            buttons = [(self.bi("确定", "OK"), "ok", BTN_PRIMARY, BTN_PRIMARY_H)]

        def _choose(value):
            result["value"] = value
            try:
                d.grab_release()
            except Exception:
                pass
            d.destroy()

        d.protocol("WM_DELETE_WINDOW", lambda: _choose(None if kind == "question" else "ok"))
        for label, value, fg, hover in buttons:
            ctk.CTkButton(
                btns, text=label, width=92, height=34, font=font(12, "bold"),
                corner_radius=8, fg_color=fg, hover_color=hover,
                command=lambda v=value: _choose(v),
            ).pack(side="left", padx=(8, 0))
        d.wait_window()
        return result["value"]

    def _ask_update_action(self, manifest: dict, parent=None) -> str:
        owner = self._resolve_dialog_parent(parent)
        notes = manifest.get("notes", "").strip() or self.bi("暂无更新说明", "No release notes")
        result = {"action": "cancel"}
        d = ctk.CTkToplevel(owner)
        d.title(self.bi("发现新版本", "Update Available"))
        d.geometry("620x420")
        self._prepare_popup(d)
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(d, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=18, pady=(18, 8), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr,
            text=self.bi(f"发现新版本 v{manifest['version']}", f"New Version v{manifest['version']}"),
            font=font(18, "bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            hdr,
            text=self.bi(f"当前版本 v{VERSION}", f"Current version v{VERSION}"),
            font=font(12),
            text_color=C_SUBTLE_TEXT,
        ).grid(row=1, column=0, pady=(4, 0), sticky="w")

        body = ctk.CTkTextbox(d, font=font(12), wrap="word")
        body.grid(row=1, column=0, padx=16, pady=(0, 12), sticky="nsew")
        body.insert(
            "1.0",
            self.bi(
                f"更新说明：\n{notes}\n\n请选择操作：\n- 立即下载：由软件直接下载更新包\n- 浏览器下载：跳转到浏览器打开下载链接",
                f"Release notes:\n{notes}\n\nChoose an action:\n- Download Now: download the update package in the app\n- Browser Download: open the download link in your browser",
            ),
        )
        body.configure(state="disabled")

        btns = ctk.CTkFrame(d, fg_color="transparent")
        btns.grid(row=2, column=0, padx=18, pady=(0, 18), sticky="e")

        def _set_action(action: str):
            result["action"] = action
            try:
                d.grab_release()
            except Exception:
                pass
            d.destroy()

        d.protocol("WM_DELETE_WINDOW", lambda: _set_action("cancel"))
        ctk.CTkButton(
            btns,
            text=self.bi("立即下载", "Download Now"),
            width=120, height=36, font=font(12, "bold"),
            corner_radius=8, fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
            command=lambda: _set_action("download"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text=self.bi("浏览器下载", "Browser Download"),
            width=140, height=36, font=font(12, "bold"),
            corner_radius=8, fg_color=BTN_BLUE, hover_color=BTN_BLUE_H,
            command=lambda: _set_action("browser"),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns,
            text=self.bi("稍后再说", "Later"),
            width=110, height=36, font=font(12, "bold"),
            corner_radius=8, fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
            command=lambda: _set_action("cancel"),
        ).pack(side="left")

        d.wait_window()
        return result["action"]

    def _ask_directory(self, **kwargs):
        self._prepare_dialog_parent()
        kwargs.setdefault("parent", self)
        return filedialog.askdirectory(**kwargs)

    def _ask_open_filename(self, **kwargs):
        self._prepare_dialog_parent()
        kwargs.setdefault("parent", self)
        return filedialog.askopenfilename(**kwargs)

    def _ask_open_filenames(self, **kwargs):
        self._prepare_dialog_parent()
        kwargs.setdefault("parent", self)
        return filedialog.askopenfilenames(**kwargs)

    def _input_dialog(self, title: str, text: str) -> str:
        self._prepare_dialog_parent()
        dialog = ctk.CTkInputDialog(text=text, title=title)
        self._prepare_popup(dialog)
        return dialog.get_input() or ""

    def _ensure_game_monitor(self):
        """确保智能同步或安全自动备份所需的进程监控正在运行。"""
        should_run = bool(
            (self.cfg.get("sync_enabled") and self.cfg.get("sync_mode") == "smart")
            or self._backup_handlers_should_run()
        )
        if should_run:
            if (self._game_monitor is None
                    or self._game_monitor._thread is None
                    or not self._game_monitor._thread.is_alive()):
                self._start_game_monitor()
        else:
            if self._game_monitor is not None:
                self._stop_game_monitor()

    # ─── 侧边栏 ───
    def _build_sidebar(self):
        sb = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=C_SIDEBAR_BG)
        sb.grid(row=0, column=0, sticky="nsew")
        sb.grid_columnconfigure(0, weight=1)
        sb.grid_rowconfigure(8, weight=1)
        sb.grid_propagate(False)

        # Logo 区域
        logo_frame = ctk.CTkFrame(sb, fg_color="transparent")
        logo_frame.grid(row=0, column=0, padx=0, pady=(22, 0), sticky="ew")
        logo_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(logo_frame, text="🎮", font=font(28)).grid(
            row=0, column=0, pady=(0, 2))
        ctk.CTkLabel(logo_frame, text=self.t("product_title"), font=font(16, "bold"),
                     text_color=C_BODY_TEXT).grid(row=1, column=0, pady=(0, 4))
        ctk.CTkLabel(logo_frame, text="Steam Save Manager",
                     font=font(9), text_color=C_SUBTLE_TEXT).grid(
            row=2, column=0, pady=(0, 12))

        ctk.CTkFrame(sb, height=1, fg_color=C_SIDEBAR_SEP).grid(
            row=1, column=0, padx=18, sticky="ew", pady=(0, 12))

        # 导航分组标签
        ctk.CTkLabel(sb, text=self.t("nav_section"), font=font(10),
                     text_color=C_SUBTLE_TEXT).grid(
            row=2, column=0, padx=22, pady=(0, 4), sticky="w")

        nav = [
            ("🏠", self.t("nav_home"),      "home"),
            ("🔍", self.t("nav_scan"),      "scan"),
            ("🎮", self.t("nav_games"),     "games"),
            ("💾", self.t("nav_backup"),    "backup"),
            ("🔧", self.t("nav_settings"),  "settings"),
        ]
        self._nav_buttons: dict[str, ctk.CTkButton] = {}
        for i, (icon, label, key) in enumerate(nav):
            btn = ctk.CTkButton(
                sb, text=f"  {icon}   {label}", anchor="w", font=font(13),
                fg_color="transparent", text_color=C_NAV_TEXT,
                hover_color=C_NAV_HOVER, height=40, corner_radius=8,
                command=lambda k=key: self._show_frame(k))
            btn.grid(row=i + 3, column=0, padx=12, pady=2, sticky="ew")
            self._nav_buttons[key] = btn

        # 底部状态区域
        bottom = ctk.CTkFrame(sb, fg_color="transparent")
        bottom.grid(row=9, column=0, padx=0, pady=(0, 16), sticky="sew")
        bottom.grid_columnconfigure(0, weight=1)
        ctk.CTkFrame(bottom, height=1, fg_color=C_SIDEBAR_SEP).pack(
            fill="x", padx=18, pady=(0, 10))
        self._status_label = ctk.CTkLabel(
            bottom, text=self.t("status_auto_off"), font=font(11), text_color=C_SUBTLE_TEXT)
        self._status_label.pack(pady=(0, 2))
        self._sidebar_version_label = ctk.CTkLabel(
            bottom, text="", font=font(9),
            text_color=self._sidebar_version_color, cursor="hand2")
        self._sidebar_version_label.pack(pady=(0, 0))
        self._sidebar_version_label.bind("<Button-1>", lambda _e: self._show_about_dialog())
        self._apply_sidebar_version_state()

    def _highlight_nav(self, active_key: str):
        for key, btn in self._nav_buttons.items():
            if key == active_key:
                btn.configure(fg_color=C_NAV_ACTIVE,
                              text_color=C_NAV_ACT_TEXT,
                              font=font(13, "bold"))
            else:
                btn.configure(fg_color="transparent",
                              text_color=C_NAV_TEXT,
                              font=font(13))

    def _show_frame(self, name: str):
        self._stop_detail_refresh()
        for f in self._frames.values():
            f.grid_forget()
        self._frames[name].grid(row=0, column=1, sticky="nsew")
        self._current_frame = name
        self._highlight_nav(name)
        if name == "backup":   self._refresh_backup_list()
        elif name == "home":   self._refresh_home()
        elif name == "games":  self._refresh_games_list()

    # ─── 主页 ───
    def _build_home_frame(self):
        frame = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["home"] = frame
        frame.grid_columnconfigure(0, weight=1)

        home_title = ctk.CTkLabel(frame, text=self.t("home_title"),
                                  font=font(24, "bold"), anchor="w", justify="left")
        home_title.grid(row=0, column=0, padx=32, pady=(30, 2), sticky="ew")
        self._register_wrap(home_title, 0.78, 360)
        home_subtitle = ctk.CTkLabel(
            frame, text=self.t("home_subtitle"),
            font=font(13), text_color=C_SUBTLE_TEXT, anchor="w", justify="left")
        home_subtitle.grid(row=1, column=0, padx=32, pady=(0, 18), sticky="ew")
        self._register_wrap(home_subtitle, 0.78, 360)

        # 统计卡片
        stats = ctk.CTkFrame(frame, fg_color="transparent")
        stats.grid(row=2, column=0, padx=28, sticky="ew")
        stats.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._stat_cards = {}
        defs = [
            ("games",   self.t("stat_games"),   self.t("value_games", count=0), "#eef2ff", "#2e2b6b", "#6366f1"),
            ("backups", self.t("stat_backups"), self.t("value_backups", count=0), "#ecfdf5", "#0c4535", "#10b981"),
            ("auto",    self.t("stat_auto"),    self.t("switch_off"),  "#fff7ed", "#5e1e0c", "#f97316"),
            ("watch",   self.t("stat_watch"),   self.t("switch_off"),  "#fdf2f8", "#6f0f33", "#ec4899"),
        ]
        for col, (key, title, default, lbg, dbg, accent) in enumerate(defs):
            card = ctk.CTkFrame(stats, fg_color=(lbg, dbg), corner_radius=12)
            card.grid(row=0, column=col, padx=5, pady=5, sticky="nsew")
            ctk.CTkLabel(card, text=title, font=font(12),
                         text_color=C_SUBTLE_TEXT).grid(
                row=0, column=0, padx=16, pady=(14, 0), sticky="w")
            vl = ctk.CTkLabel(card, text=default, font=font(22, "bold"),
                              text_color=(accent, accent))
            vl.grid(row=1, column=0, padx=16, pady=(2, 16), sticky="w")
            self._stat_cards[key] = vl

        # 按钮
        bf = ctk.CTkFrame(frame, fg_color="transparent")
        bf.grid(row=3, column=0, padx=32, pady=(16, 8), sticky="ew")
        bf.grid_columnconfigure((0, 1, 2), weight=1, uniform="home_actions")
        self._register_busy_widget(ctk.CTkButton(
            bf, text=self.t("home_backup_all"), height=42,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
            command=self._backup_all)).grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self._register_busy_widget(ctk.CTkButton(
            bf, text=self.t("home_sync_all"), height=42,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_BLUE, hover_color=BTN_BLUE_H,
            command=self._manual_sync_all)).grid(row=0, column=1, padx=8, sticky="ew")
        ctk.CTkButton(
            bf, text=self.t("home_scan"), height=42,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
            command=lambda: self._show_frame("scan")).grid(row=0, column=2, padx=(8, 0), sticky="ew")

        # 最近备份
        self._home_recent = ctk.CTkScrollableFrame(
            frame, label_text=self.t("home_recent"), label_font=font(15, "bold"),
            height=260, corner_radius=12, fg_color=C_CARD_BG)
        self._home_recent.grid(row=4, column=0, padx=30, pady=(18, 24), sticky="nsew")
        frame.grid_rowconfigure(4, weight=1)
        self._home_recent_rows = {}
        self._home_recent_empty_label = ctk.CTkLabel(
            self._home_recent,
            text="",
            text_color=C_SUBTLE_TEXT,
            font=font(13),
        )

    def _refresh_home(self):
        games = self.cfg.get("games", [])
        total = sum(self._get_game_backup_count_cached(g) for g in games)
        auto = self.t("switch_on") if self.cfg.get("auto_backup_enabled") else self.t("switch_off")
        watch = self.t("switch_on") if self.cfg.get("watch_enabled") else self.t("switch_off")
        self._stat_cards["games"].configure(text=self.t("value_games", count=len(games)))
        self._stat_cards["backups"].configure(text=self.t("value_backups", count=total))
        self._stat_cards["auto"].configure(text=auto)
        self._stat_cards["watch"].configure(text=watch)

        all_b = self._get_recent_backups_cached(15)
        for item in self._home_recent_rows.values():
            item["row"].pack_forget()
        if not all_b:
            self._home_recent_empty_label.configure(text=self.t("home_no_backups"))
            self._home_recent_empty_label.pack(pady=40)
            return
        self._home_recent_empty_label.pack_forget()
        active_keys = set()
        for b in all_b[:15]:
            key = f"{b['game']}::{b['timestamp']}::{b['filename']}"
            active_keys.add(key)
            item = self._home_recent_rows.get(key)
            if item is None:
                row = ctk.CTkFrame(self._home_recent,
                                   fg_color=("#f1f5f9", "#252640"), corner_radius=8)
                title = ctk.CTkLabel(
                    row, text="", font=font(12, "bold"), text_color=C_BODY_TEXT,
                    wraplength=240
                )
                title.pack(side="left", padx=(12, 6), pady=7)
                info_label = ctk.CTkLabel(
                    row, text="", font=font(11), text_color=C_SUBTLE_TEXT,
                    wraplength=320
                )
                info_label.pack(side="left", padx=4, pady=7)
                item = {"row": row, "title": title, "info": info_label}
                self._home_recent_rows[key] = item
            item["title"].configure(text=f"🎮 {b['game']}")
            info = f"{self._fmt_ts(b['timestamp'])}  ·  {fmt_size(b['size'])}"
            note_text = localize_backup_note(b.get("note", ""), cfg_language(self.cfg))
            if note_text:
                info += f"  ·  📝 {note_text}"
            item["info"].configure(text=info)
            item["row"].pack(fill="x", padx=4, pady=2)
        stale_keys = [key for key in self._home_recent_rows.keys() if key not in active_keys]
        for key in stale_keys:
            item = self._home_recent_rows.pop(key, None)
            if item:
                item["row"].destroy()

    # ─── 扫描游戏 ───
    def _build_scan_frame(self):
        frame = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["scan"] = frame
        frame.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=32, pady=(28, 8), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text=self.t("scan_title"),
                     font=font(20, "bold")).grid(row=0, column=0, sticky="w")
        self._scan_add_all_btn = ctk.CTkButton(
            hdr, text=self.t("scan_add_all"), width=120, height=38,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
            state="disabled", command=self._add_all_from_scan)
        self._scan_add_all_btn.grid(row=0, column=1, padx=(0, 10), sticky="e")
        self._scan_start_btn = ctk.CTkButton(
            hdr, text=self.t("scan_start"), width=120, height=38,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
            command=self._do_scan)
        self._scan_start_btn.grid(row=0, column=2, sticky="e")
        self._scan_cancel_btn = ctk.CTkButton(
            hdr, text=self.bi("取消", "Cancel"), width=92, height=38,
            font=font(13, "bold"), corner_radius=10,
            fg_color=BTN_DANGER, hover_color=BTN_DANGER_H,
            state="disabled", command=self._cancel_scan)
        self._scan_cancel_btn.grid(row=0, column=3, padx=(10, 0), sticky="e")

        self._scan_status = ctk.CTkLabel(
            frame, text=self.t("scan_hint"),
            font=font(13), text_color=C_SUBTLE_TEXT)
        self._scan_status.grid(row=1, column=0, padx=32, sticky="w")
        self._scan_progress = ctk.CTkProgressBar(frame, height=8, corner_radius=4)
        self._scan_progress.grid(row=2, column=0, padx=32, pady=(8, 0), sticky="ew")
        self._scan_progress.set(0)

        self._scan_search_var = ctk.StringVar()
        self._scan_search_var.trace_add("write", self._schedule_scan_search_render)
        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.grid(row=3, column=0, padx=32, pady=(8, 0), sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)
        self._scan_search_entry = ctk.CTkEntry(
            search_frame, textvariable=self._scan_search_var,
            placeholder_text=self.bi("搜索游戏名称或 AppID …", "Search by game name or AppID …"),
            height=34, corner_radius=8, font=font(13))
        self._scan_search_entry.grid(row=0, column=0, sticky="ew")

        self._scan_scroll = ctk.CTkScrollableFrame(
            frame, height=420, corner_radius=12, fg_color=C_CARD_BG)
        self._scan_scroll.grid(row=4, column=0, padx=30, pady=10, sticky="nsew")
        frame.grid_rowconfigure(4, weight=1)
        self._scan_cards = {}
        self._scan_info_banner = ctk.CTkFrame(
            self._scan_scroll, fg_color=("#eef2ff", "#2e2b6b"), corner_radius=12
        )
        self._scan_info_label = ctk.CTkLabel(
            self._scan_info_banner, text="", font=font(11), justify="left", text_color=C_BODY_TEXT
        )
        self._scan_info_label.pack(padx=14, pady=10, anchor="w")
        self._scan_empty_label = ctk.CTkLabel(
            self._scan_scroll, text="", font=font(13), text_color=C_SUBTLE_TEXT
        )

    def _scan_profile_label(self, profile: str) -> str:
        labels = {
            "ssd": self.bi("SSD", "SSD"),
            "hdd": self.bi("机械硬盘", "HDD"),
            "mixed": self.bi("混合磁盘", "Mixed"),
            "network": self.bi("网络磁盘", "Network"),
            "removable": self.bi("可移动磁盘", "Removable"),
            "fixed": self.bi("固定磁盘", "Fixed"),
            "unknown": self.bi("未知磁盘", "Unknown"),
        }
        return labels.get(profile, labels["unknown"])

    def _set_scan_busy(self, busy: bool):
        self._scan_in_progress = busy
        if self._scan_start_btn is not None:
            self._scan_start_btn.configure(state="disabled" if busy else "normal")
        if self._scan_cancel_btn is not None:
            self._scan_cancel_btn.configure(state="normal" if busy else "disabled")
        if self._scan_add_all_btn is not None:
            filtered_results = self._get_filtered_scan_results()
            can_add = bool(
                not busy and any(self._scan_result_has_addable_paths(r) for r in filtered_results)
            )
            self._scan_add_all_btn.configure(state="normal" if can_add else "disabled")
        if self._scan_progress is not None and not busy and not self._scan_results:
            try:
                self._scan_progress.set(0)
            except Exception:
                pass

    def _cancel_scan(self):
        if not self._scan_in_progress:
            return
        self._scan_cancel_event.set()
        if self._scan_cancel_btn is not None:
            self._scan_cancel_btn.configure(state="disabled")
        self._scan_status.configure(text=self.bi("正在取消扫描...", "Canceling scan..."))

    def _set_io_busy(self, busy: bool):
        """标记 IO 操作进行中，防止重复触发；同步更新相关按钮的启用状态"""
        self._io_busy = busy
        state = "disabled" if busy else "normal"
        for widget in list(self._busy_widgets):
            try:
                if widget.winfo_exists():
                    widget.configure(state=state)
            except Exception:
                pass
        for item in self._game_cards.values():
            try:
                item["backup_btn"].configure(state=state)
                item["delete_btn"].configure(state=state)
            except Exception:
                pass
        for item in self._backup_rows.values():
            try:
                item["restore_btn"].configure(state=state)
                item["delete_btn"].configure(state=state)
            except Exception:
                pass

    def _notify_io_busy(self):
        self._show_warning(
            self.bi("处理中", "Busy"),
            self.bi(
                "当前已有备份、恢复或同步任务正在进行，请稍候再试。",
                "A backup, restore, or sync task is already running. Please try again in a moment.",
            ),
        )

    def _scroll_settings_to_section(self, section_key: str):
        canvas = getattr(self, "_settings_scroll", None)
        target = self._settings_section_positions.get(section_key)
        if canvas is None or target is None:
            return
        try:
            canvas.update_idletasks()
            bbox = canvas.bbox("all")
            total_h = max((bbox[3] - bbox[1]) if bbox else 1, 1)
            view_h = max(canvas.winfo_height(), 1)
            y = max(target.winfo_y() - 8, 0)
            canvas.yview_moveto(min(y / max(total_h - view_h, 1), 1.0))
        except Exception:
            pass

    def _schedule_scan_search_render(self, *_args):
        if self._scan_search_job:
            try:
                self.after_cancel(self._scan_search_job)
            except Exception:
                pass
        self._scan_search_job = self.after(180, self._run_scan_search_render)

    def _run_scan_search_render(self):
        self._scan_search_job = None
        self._render_scan_results()

    def _get_scan_account_lookups(self):
        steam_path = str(self.cfg.get("steam_path", "") or "").strip()
        cached_steam_path = str(self._steam_account_lookup_cache.get("steam_path") or "").strip()
        if steam_path and steam_path == cached_steam_path:
            return (
                dict(self._steam_account_lookup_cache.get("accountid_map", {})),
                dict(self._steam_account_lookup_cache.get("steam64_map", {})),
            )
        accountid_map = {}
        steam64_map = {}
        if not steam_path:
            return accountid_map, steam64_map
        for item in get_steam_accounts(steam_path):
            display = (
                str(item.get("persona_name", "") or "").strip()
                or str(item.get("account_name", "") or "").strip()
                or str(item.get("accountid", "") or "").strip()
            )
            if not display:
                continue
            accountid = str(item.get("accountid", "") or "").strip()
            steam64 = str(item.get("steamid64", "") or "").strip()
            if accountid:
                accountid_map[accountid] = display
            if steam64:
                steam64_map[steam64] = display
        self._steam_account_lookup_cache = {
            "steam_path": steam_path,
            "accountid_map": dict(accountid_map),
            "steam64_map": dict(steam64_map),
        }
        return accountid_map, steam64_map

    def _get_scan_candidate_account_note(self, candidate: dict,
                                         accountid_map: dict[str, str],
                                         steam64_map: dict[str, str]) -> str:
        candidate_accountid = str(candidate.get("accountid", "") or "").strip()
        if candidate_accountid and candidate_accountid in accountid_map:
            return accountid_map[candidate_accountid]
        path = str(candidate.get("path", "") or "").strip()
        if not path:
            return ""
        segments = [seg for seg in re.split(r"[\\\\/]+", path) if seg]
        for segment in segments:
            if segment in accountid_map:
                return accountid_map[segment]
            if segment in steam64_map:
                return steam64_map[segment]
        return ""

    def _build_scan_choice_labels(self, appid: str, available_paths: list[str], candidates: list[dict]) -> tuple[list[str], dict[str, str]]:
        accountid_map, steam64_map = self._get_scan_account_lookups()
        labels = []
        label_to_path = {}
        path_to_candidate = {
            os.path.normpath(c.get("path", "")): c
            for c in (candidates or [])
            if c.get("path")
        }
        for path in available_paths:
            candidate = path_to_candidate.get(os.path.normpath(path), {"path": path})
            note = self._get_scan_candidate_account_note(candidate, accountid_map, steam64_map)
            label = path
            if note:
                label = f"{path}  ({note})"
            if label in label_to_path:
                suffix = 2
                base_label = label
                while label in label_to_path:
                    label = f"{base_label} ({suffix})"
                    suffix += 1
            label_to_path[label] = path
            labels.append(label)
        self._scan_choice_labels[str(appid)] = dict(label_to_path)
        return labels, label_to_path

    def _resolve_scan_selected_path(self, appid: str, selected_value: str) -> str:
        mapping = self._scan_choice_labels.get(str(appid), {})
        return mapping.get(selected_value, selected_value)

    def _schedule_games_refresh(self, *_args):
        self._games_page = 0
        if self._games_search_job:
            try:
                self.after_cancel(self._games_search_job)
            except Exception:
                pass
        self._games_search_job = self.after(180, self._run_games_refresh)

    def _run_games_refresh(self):
        self._games_search_job = None
        self._refresh_games_list()

    def _invalidate_game_backup_count_cache(self, game_ref=None):
        if game_ref is not None:
            key = get_game_cache_key(game_ref)
            self._game_backup_count_cache.pop(key, None)
            self._game_backups_cache.pop(key, None)
            self._recent_backups_cache.clear()
            self._game_detail_metrics_cache.pop(key, None)
            return
        self._game_backup_count_cache.clear()
        self._game_backups_cache.clear()
        self._recent_backups_cache.clear()
        self._game_detail_metrics_cache.clear()

    def _on_backups_changed(self, game_ref=None):
        self._invalidate_game_backup_count_cache(game_ref)
        current = getattr(self, "_current_frame", "home")
        if current == "backup":
            self._refresh_backup_list()
            return
        if current == "home":
            self._refresh_home()
            return
        if current == "games":
            self._refresh_games_list()
            return
        if current == "game_detail":
            detail_idx = getattr(self, "_detail_idx", None)
            if isinstance(detail_idx, int) and 0 <= detail_idx < len(self.cfg.get("games", [])):
                live_game = self.cfg["games"][detail_idx]
                if game_ref is None or get_game_cache_key(game_ref) == get_game_cache_key(live_game):
                    self._show_game_detail(detail_idx)

    def _get_game_backup_count_cached(self, game_ref) -> int:
        key = get_game_cache_key(game_ref)
        if key in self._game_backup_count_cache:
            return self._game_backup_count_cache[key]
        count = count_backups(game_ref)
        self._game_backup_count_cache[key] = count
        return count

    def _get_game_backups_cached(self, game_ref) -> list:
        key = get_game_cache_key(game_ref)
        cached = self._game_backups_cache.get(key)
        if cached is not None:
            return [dict(item) for item in cached]
        backups = get_backups(game_ref)
        self._game_backups_cache[key] = [dict(item) for item in backups]
        return [dict(item) for item in backups]

    def _get_recent_backups_cached(self, limit: int = 15) -> list:
        cached = self._recent_backups_cache.get(limit)
        if cached is not None:
            return [dict(item) for item in cached]
        all_b = []
        for g in self.cfg.get("games", []):
            for b in get_backups(g, limit=limit):
                item = dict(b)
                item["game"] = g["name"]
                all_b.append(item)
        all_b.sort(key=lambda x: x["timestamp"], reverse=True)
        recent = all_b[:limit]
        self._recent_backups_cache[limit] = [dict(item) for item in recent]
        return [dict(item) for item in recent]

    def _clamp_page(self, page: int, total: int, page_size: int) -> int:
        if total <= 0:
            return 0
        max_page = max(0, (total - 1) // max(1, page_size))
        return max(0, min(page, max_page))

    def _page_range(self, page: int, total: int, page_size: int) -> tuple[int, int, int]:
        page_size = max(1, page_size)
        page = self._clamp_page(page, total, page_size)
        start = page * page_size
        end = min(total, start + page_size)
        return page, start, end

    def _on_games_page_delta(self, delta: int):
        self._games_page += delta
        self._refresh_games_list()

    def _on_backup_page_delta(self, delta: int):
        self._backup_page += delta
        self._refresh_backup_list()

    def _on_backup_filter_changed(self):
        self._backup_page = 0
        self._refresh_backup_list()

    def _get_game_detail_metrics_cached(self, game: dict) -> dict:
        key = get_game_cache_key(game)
        now = time.time()
        cached = self._game_detail_metrics_cache.get(key)
        if cached and now - float(cached.get("ts", 0.0)) < 10.0:
            return dict(cached.get("data", {}))

        save_paths = get_game_save_paths(game, existing_only=False)
        display_paths = get_game_save_path_templates(game, existing_only=False)
        existing_paths = [p for p in save_paths if os.path.isdir(p)]
        path_ok = bool(existing_paths)
        file_count = 0
        for path in existing_paths:
            try:
                for _, _, fs in os.walk(path):
                    file_count += len(fs)
            except Exception:
                continue

        data = {
            "save_paths": save_paths,
            "display_paths": display_paths,
            "existing_paths": existing_paths,
            "path_ok": path_ok,
            "file_count": file_count,
        }
        self._game_detail_metrics_cache[key] = {"ts": now, "data": dict(data)}
        return dict(data)

    def _get_filtered_scan_results(self):
        search_text = self._scan_search_var.get().strip().lower() if hasattr(self, "_scan_search_var") else ""
        results = []
        for game in self._scan_results:
            appid = game.get("appid")
            haystack = game.get("_search_blob")
            if haystack is None:
                haystack = f"{str(game.get('name', '') or '').lower()} {str(appid or '').lower()}"
                game["_search_blob"] = haystack
            if search_text and search_text not in haystack:
                continue
            results.append(game)
        return results

    def _scan_result_has_addable_paths(self, result: Optional[dict]) -> bool:
        if not isinstance(result, dict):
            return False
        appid = str(result.get("appid", "") or "").strip()
        save_paths = result.get("save_paths", []) or []
        if not appid or not save_paths:
            return False
        tracked_paths = set()
        for tracked_game in self.cfg.get("games", []):
            if str(tracked_game.get("appid", "") or "").strip() != appid:
                continue
            tracked_paths.update(
                os.path.normpath(p)
                for p in get_game_save_paths(tracked_game, existing_only=False)
                if p
            )
        return any(os.path.normpath(path) not in tracked_paths for path in save_paths)

    def _render_scan_results(self):
        for item in self._scan_cards.values():
            item["card"].pack_forget()

        if self._scan_lib_folders:
            self._scan_info_banner.configure(fg_color=("#eef2ff", "#1e1b4b"))
            txt = self.bi("检测到的 Steam 库路径：\n", "Detected Steam library paths:\n") + "\n".join(
                f"  📂 {p}" for p in self._scan_lib_folders)
            self._scan_info_label.configure(text=txt, font=font(11), text_color=C_BODY_TEXT, justify="left")
            self._scan_info_banner.pack(fill="x", padx=4, pady=(0, 8))
        else:
            self._scan_info_banner.pack_forget()

        tracked_by_appid: dict[str, set[str]] = {}
        for tracked_game in self.cfg.get("games", []):
            tracked_appid = str(tracked_game.get("appid", "") or "").strip()
            if not tracked_appid:
                continue
            tracked_paths = tracked_by_appid.setdefault(tracked_appid, set())
            tracked_paths.update(
                os.path.normpath(p)
                for p in get_game_save_paths(tracked_game, existing_only=False)
                if p
            )
        found = 0
        addable = 0
        filtered_results = self._get_filtered_scan_results()
        active_keys = set()

        if not filtered_results:
            self._scan_empty_label.configure(
                text=self.bi("没有匹配的扫描结果", "No matching scan results")
            )
            self._scan_empty_label.pack(pady=40)
        else:
            self._scan_empty_label.pack_forget()
        for game in filtered_results:
            appid = game["appid"]
            name = game["name"]
            save_paths = game.get("save_paths", [])
            top_candidate = (game.get("save_candidates") or [{}])[0] if save_paths else {}
            key = str(appid)
            active_keys.add(key)
            tracked_paths = tracked_by_appid.get(str(appid or "").strip(), set())
            available_paths = [p for p in save_paths if os.path.normpath(p) not in tracked_paths]
            option_labels, label_to_path = self._build_scan_choice_labels(appid, available_paths, game.get("save_candidates", []))
            item = self._scan_cards.get(key)
            if item is None:
                card = ctk.CTkFrame(self._scan_scroll,
                                    fg_color=("#f1f5f9", "#252640"), corner_radius=12)
                card.grid_columnconfigure(1, weight=1)
                title = ctk.CTkLabel(card, text="", font=font(13, "bold"),
                                     text_color=C_BODY_TEXT)
                title.grid(row=0, column=0, padx=14, pady=(10, 2), sticky="w", columnspan=2)
                self._register_wrap(title, 0.62, 280)
                path_label = ctk.CTkLabel(card, text="", font=font(11),
                                          text_color=C_SUBTLE_TEXT)
                path_label.grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")
                self._register_wrap(path_label, 0.58, 260)
                self._attach_tooltip(path_label, lambda lbl=path_label: getattr(lbl, "_full_path_text", ""))
                path_label.bind(
                    "<Button-3>",
                    lambda _e=None, lbl=path_label: self._copy_to_clipboard(
                        getattr(lbl, "_full_path_text", ""),
                        self.bi("完整路径已复制到剪贴板", "Full path copied to clipboard"),
                    ),
                    add="+",
                )
                meta_label = ctk.CTkLabel(card, text="", font=font(10),
                                          text_color=C_SUBTLE_TEXT)
                meta_label.grid(row=2, column=0, padx=14, pady=(0, 8), sticky="w")
                right_top = ctk.CTkFrame(card, fg_color="transparent")
                right_top.grid(row=0, column=1, padx=6, pady=4, sticky="e")
                right_bottom = ctk.CTkFrame(card, fg_color="transparent")
                right_bottom.grid(row=1, column=1, padx=14, pady=(0, 10), sticky="e")
                item = {
                    "card": card,
                    "title": title,
                    "path": path_label,
                    "meta": meta_label,
                    "right_top": right_top,
                    "right_bottom": right_bottom,
                    "status": None,
                    "option": None,
                    "folder_btn": None,
                    "add_btn": None,
                }
                self._scan_cards[key] = item

            ico = "✔" if save_paths and not available_paths else ("✅" if save_paths else "❓")
            item["title"].configure(text=f"{ico} {name}")

            if save_paths:
                found += 1
                pd = save_paths[0]
                if len(pd) > 60:
                    pd = "..." + pd[-57:]
                extra = (
                    self.bi(f"  (+{len(save_paths)-1} 个候选)", f"  (+{len(save_paths)-1} candidates)")
                    if len(save_paths) > 1 else ""
                )
                item["path"].configure(
                    text=f"📁 {pd}{extra}",
                    text_color=C_SUBTLE_TEXT,
                )
                item["path"]._full_path_text = "\n".join(save_paths)
                confidence_map = {
                    "high": self.bi("高可信", "High confidence"),
                    "medium": self.bi("中可信", "Medium confidence"),
                    "low": self.bi("低可信", "Low confidence"),
                }
                source_map = {
                    "confirmed": self.bi("已确认路径", "Confirmed path"),
                    "manual": self.bi("手动指定", "Manual override"),
                    "cache": self.bi("历史缓存", "Cached result"),
                    "known-path": self.bi("内置规则", "Known rule"),
                    "steamdb": self.bi("SteamDB", "SteamDB"),
                    "remotecache": self.bi("remotecache", "remotecache"),
                    "steam-autocloud": self.bi("Auto-Cloud", "Auto-Cloud"),
                    "steam-autocloud-account": self.bi("Auto-Cloud 账号目录", "Auto-Cloud account root"),
                    "steam-remote": self.bi("Steam remote", "Steam remote"),
                    "steam-app-root": self.bi("Steam app root", "Steam app root"),
                    "install-dir": self.bi("安装目录", "Install dir"),
                    "system-search": self.bi("系统模糊搜索", "System search"),
                    "appinfo": self.bi("本地云存档配置", "Local cloud config"),
                    "registry": self.bi("注册表", "Registry"),
                }
                confidence_text = confidence_map.get(top_candidate.get("confidence", "low"), confidence_map["low"])
                source_text = source_map.get(top_candidate.get("source", ""), top_candidate.get("source", ""))
                reason_text = self.bi("文件特征匹配", "Save file signals") if "save-files" in top_candidate.get("reasons", []) else ""
                meta_parts = [p for p in [confidence_text, source_text, reason_text] if p]
                if meta_parts:
                    item["meta"].configure(text=" · ".join(meta_parts))
                    item["meta"].grid()
                else:
                    item["meta"].configure(text="")
                    item["meta"].grid_remove()
                if available_paths:
                    addable += 1
                    pv = self._scan_choice_vars.get(appid)
                    if pv is None:
                        pv = ctk.StringVar(value=option_labels[0])
                        self._scan_choice_vars[appid] = pv
                    if pv.get() not in label_to_path:
                        pv.set(option_labels[0])
                    if item["option"] is None:
                        item["option"] = ctk.CTkOptionMenu(
                            item["right_top"], variable=pv, values=option_labels, width=260, font=font(11)
                        )
                        item["option"].pack(side="left")
                    else:
                        item["option"].configure(variable=pv, values=option_labels)
                        if not item["option"].winfo_manager():
                            item["option"].pack(side="left")
                    if item["folder_btn"] is None:
                        item["folder_btn"] = ctk.CTkButton(
                            item["right_bottom"], text="📁", width=36, height=28,
                            font=font(14), corner_radius=6,
                            fg_color=BTN_WARN, hover_color=BTN_WARN_H,
                        )
                        item["folder_btn"].pack(side="left")
                    elif not item["folder_btn"].winfo_manager():
                        item["folder_btn"].pack(side="left")
                    item["folder_btn"].configure(command=lambda a=appid, n=name: self._override_scan_result_path(a, n))
                    if item["add_btn"] is None:
                        item["add_btn"] = ctk.CTkButton(
                            item["right_bottom"], text=self.bi("添加", "Add"), width=64, height=28,
                            font=font(12), corner_radius=6,
                            fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                        )
                        item["add_btn"].pack(side="left", padx=(8, 0))
                    elif not item["add_btn"].winfo_manager():
                        item["add_btn"].pack(side="left", padx=(8, 0))
                    item["add_btn"].configure(command=lambda a=appid, n=name, v=pv: self._add_from_scan(a, n, self._resolve_scan_selected_path(a, v.get())))
                    if item["status"] is not None:
                        item["status"].grid_remove()
                else:
                    if item["option"] is not None:
                        item["option"].pack_forget()
                    if item["folder_btn"] is not None:
                        item["folder_btn"].pack_forget()
                    if item["add_btn"] is not None:
                        item["add_btn"].pack_forget()
                    if item["status"] is None:
                        item["status"] = ctk.CTkLabel(
                            item["right_top"], text="", font=font(11), text_color=BTN_SUCCESS
                        )
                    item["status"].configure(
                        text=self.bi("✔ 已全部添加", "✔ All added") if save_paths else self.bi("✔ 已添加", "✔ Added")
                    )
                    item["status"].grid(row=0, column=0, padx=14, sticky="e")
            else:
                item["path"].configure(
                    text=self.bi("未检测到存档路径", "No save path detected"),
                    text_color=("#ea580c", "#fb923c"),
                )
                item["path"]._full_path_text = ""
                item["meta"].configure(text="")
                item["meta"].grid_remove()
                if item["option"] is not None:
                    item["option"].pack_forget()
                if item["add_btn"] is not None:
                    item["add_btn"].pack_forget()
                if item["folder_btn"] is None:
                    item["folder_btn"] = ctk.CTkButton(
                        item["right_bottom"], text="📁", width=36, height=28,
                        font=font(14), corner_radius=6,
                        fg_color=BTN_WARN, hover_color=BTN_WARN_H,
                    )
                    item["folder_btn"].pack(side="left")
                item["folder_btn"].configure(command=lambda a=appid, n=name: self._override_scan_result_path(a, n))
                if item["status"] is not None:
                    item["status"].grid_remove()

            item["card"].pack(fill="x", padx=4, pady=2)

        stale_keys = [key for key in self._scan_cards.keys() if key not in active_keys]
        for key in stale_keys:
            item = self._scan_cards.pop(key, None)
            if item:
                item["card"].destroy()
            self._scan_choice_vars.pop(key, None)
            self._scan_choice_labels.pop(key, None)

        if not self._scan_in_progress:
            self._scan_status.configure(
                text=self.bi(
                    f"扫描完成：发现 {len(self._scan_results)} 款已安装游戏，其中 {found} 款检测到存档路径 · 使用 {self._scan_worker_count} 个线程（{self._scan_profile_label(self._scan_storage_profile)}）",
                    f"Scan complete: found {len(self._scan_results)} installed games, detected save paths for {found}, using {self._scan_worker_count} threads ({self._scan_profile_label(self._scan_storage_profile)})",
                ))
        if self._scan_add_all_btn is not None:
            self._scan_add_all_btn.configure(state="normal" if addable and not self._scan_in_progress else "disabled")

    def _finish_scan_with_error(self, message: str):
        self._scan_results = []
        self._scan_lib_folders = []
        self._scan_worker_count = 0
        self._scan_storage_profile = "unknown"
        for item in self._scan_cards.values():
            item["card"].destroy()
        self._scan_cards.clear()
        self._scan_choice_vars = {}
        self._scan_choice_labels = {}
        self._scan_empty_label.pack_forget()
        self._scan_info_banner.configure(fg_color=("#fef2f2", "#450a0a"))
        self._scan_info_label.configure(
            text=self.t("scan_missing_steam"),
            font=font(12), text_color=("#dc2626", "#fca5a5"), justify="left",
        )
        self._scan_info_banner.pack(fill="x", padx=4, pady=(0, 8))
        self._scan_status.configure(text=message)
        self._set_scan_busy(False)

    def _finish_scan_cancelled(self, results: list[dict], lib_folders: list[str], worker_count: int, storage_profile: str):
        self._scan_results = results
        self._scan_lib_folders = lib_folders
        self._scan_worker_count = worker_count
        self._scan_storage_profile = storage_profile
        self._set_scan_busy(False)
        try:
            total = max(len(results), 1)
            self._scan_progress.set(1 if results else 0)
        except Exception:
            pass
        self._render_scan_results()
        self._scan_status.configure(
            text=self.bi(
                f"扫描已取消：保留已完成的 {len(results)} 条结果",
                f"Scan canceled: kept {len(results)} completed results",
            )
        )

    def _scan_worker(self, steam_path: str):
        lib_folders = get_steam_library_folders(steam_path)
        if not lib_folders:
            self.after(0, lambda: self._finish_scan_with_error(
                self.bi("扫描失败：未找到 Steam 库路径", "Scan failed: no Steam library path was found")))
            return

        installed = scan_installed_games(steam_path)
        discover_steam_autocloud_entries()
        scan_paths = lib_folders + [steam_path] + [str(base) for base in COMMON_SAVE_BASES]
        for game in installed:
            if game.get("install_dir"):
                scan_paths.append(game["install_dir"])
            if game.get("library_path"):
                scan_paths.append(game["library_path"])
        worker_count, storage_profile = recommend_scan_workers(scan_paths, len(installed))

        if not installed:
            def _finish_empty():
                self._scan_results = []
                self._scan_lib_folders = lib_folders
                self._scan_worker_count = worker_count
                self._scan_storage_profile = storage_profile
                self._set_scan_busy(False)
                if self._scan_progress is not None:
                    self._scan_progress.set(1)
                self._render_scan_results()
            self.after(0, _finish_empty)
            return

        results = []
        total = len(installed)
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    detect_save_candidates,
                    game["appid"], game["name"], game["install_dir"], steam_path, game.get("library_path", ""), self.cfg
                ): game
                for game in installed
            }
            for future in concurrent.futures.as_completed(future_map):
                if self._scan_cancel_event.is_set():
                    for pending in future_map:
                        pending.cancel()
                    break
                game = future_map[future]
                try:
                    save_candidates = future.result()
                except Exception:
                    save_candidates = []
                results.append({
                    **game,
                    "save_candidates": save_candidates,
                    "save_paths": [c["path"] for c in save_candidates],
                    "_search_blob": f"{str(game.get('name', '') or '').lower()} {str(game.get('appid', '') or '').lower()}",
                })
                completed += 1
                if completed == total or completed == 1 or completed % 5 == 0:
                    progress_value = completed / total if total else 0
                    self.after(
                        0,
                        lambda c=completed, t=total, wc=worker_count, sp=storage_profile, pv=progress_value:
                        (
                            self._scan_progress.set(pv) if self._scan_progress is not None else None,
                            self._scan_status.configure(
                                text=self.bi(
                                    f"扫描中... {c}/{t} · {wc} 线程（{self._scan_profile_label(sp)}）",
                                    f"Scanning... {c}/{t} · {wc} threads ({self._scan_profile_label(sp)})",
                                )
                            ),
                        )
                    )

        results.sort(key=lambda g: g["name"].lower())

        if self._scan_cancel_event.is_set():
            self.after(0, lambda r=results: self._finish_scan_cancelled(r, lib_folders, worker_count, storage_profile))
            return

        def _finish():
            self._scan_results = results
            self._scan_lib_folders = lib_folders
            self._scan_worker_count = worker_count
            self._scan_storage_profile = storage_profile
            self._set_scan_busy(False)
            if self._scan_progress is not None:
                self._scan_progress.set(1)
            self._render_scan_results()

        self.after(0, _finish)

    def _do_scan(self):
        if self._scan_in_progress:
            return
        _SAVE_DETECTION_CACHE.clear()
        self._scan_results = []
        self._scan_lib_folders = []
        self._scan_choice_vars = {}
        self._scan_choice_labels = {}
        for item in self._scan_cards.values():
            item["card"].destroy()
        self._scan_cards.clear()
        self._scan_worker_count = 0
        self._scan_storage_profile = "unknown"
        self._scan_cancel_event.clear()
        steam_path = self.cfg.get("steam_path", "")
        self._scan_info_banner.pack_forget()
        self._scan_empty_label.pack_forget()
        self._set_scan_busy(True)
        if self._scan_progress is not None:
            self._scan_progress.set(0)
        self._scan_status.configure(text=self.bi("扫描中...", "Scanning..."))
        self.update_idletasks()
        threading.Thread(target=self._scan_worker, args=(steam_path,), daemon=True).start()

    def _collect_scan_add_paths(self, appid: str, selected_path: str) -> list[str]:
        selected_norm = os.path.normpath(selected_path) if selected_path else ""
        if not selected_norm:
            return []
        result = next((r for r in self._scan_results if r.get("appid") == appid), None)
        if not result:
            return [selected_norm]
        candidates = result.get("save_candidates", [])
        selected_candidate = next(
            (c for c in candidates if os.path.normpath(c.get("path", "")) == selected_norm),
            None,
        )
        if selected_candidate and selected_candidate.get("source") in {"steamdb", "appinfo"}:
            same_source_paths = [
                c.get("path", "")
                for c in candidates
                if c.get("source") == selected_candidate.get("source")
            ]
            merged = _normalize_unique_paths([selected_norm] + same_source_paths)
            if len(merged) > 1:
                return merged
        return [selected_norm]

    def _collect_scan_add_specs(self, appid: str, selected_path: str) -> list[dict]:
        selected_norm = os.path.normpath(selected_path) if selected_path else ""
        if not selected_norm:
            return []
        result = next((r for r in self._scan_results if r.get("appid") == appid), None)
        if not result:
            return []
        candidates = result.get("save_candidates", [])
        selected_candidate = next(
            (c for c in candidates if os.path.normpath(c.get("path", "")) == selected_norm),
            None,
        )
        if not selected_candidate:
            return []
        if selected_candidate.get("source") in {"steamdb", "appinfo"}:
            merged_specs = []
            for candidate in candidates:
                if candidate.get("source") != selected_candidate.get("source"):
                    continue
                if candidate.get("save_specs"):
                    merged_specs.extend(candidate.get("save_specs", []))
            specs = _normalize_unique_save_specs(merged_specs)
            if specs:
                return specs
        if selected_candidate.get("save_specs"):
            return _normalize_unique_save_specs(selected_candidate.get("save_specs", []))
        return []

    def _get_scan_selected_candidate(self, appid: str, selected_path: str) -> dict:
        selected_norm = os.path.normpath(selected_path) if selected_path else ""
        result = next((r for r in self._scan_results if r.get("appid") == appid), None)
        if not result:
            return {
                "path": selected_norm,
                "source": "manual",
                "confidence": "high",
                "materialized": bool(selected_norm and os.path.exists(selected_norm)),
            }
        selected = next(
            (
                candidate for candidate in result.get("save_candidates", [])
                if os.path.normpath(candidate.get("path", "")) == selected_norm
            ),
            {
                "path": selected_norm,
                "source": "manual",
                "confidence": "high",
                "materialized": bool(selected_norm and os.path.exists(selected_norm)),
            },
        )
        if selected.get("source") not in {"steamdb", "appinfo"}:
            return dict(selected)
        related = [
            candidate for candidate in result.get("save_candidates", [])
            if candidate.get("source") == selected.get("source")
        ]
        effective = dict(selected)
        effective["confidence"] = min(
            (
                str(candidate.get("confidence", "low") or "low")
                for candidate in related
            ),
            key=lambda value: _SCAN_CONFIDENCE_RANK.get(value, 0),
            default="low",
        )
        effective["materialized"] = bool(
            related and all(candidate.get("materialized", True) for candidate in related)
        )
        return effective

    def _build_scanned_game(self, appid: str, name: str, selected_path: str,
                            *, user_confirmed: bool) -> tuple[Optional[dict], str]:
        result = next((r for r in self._scan_results if r.get("appid") == appid), None)
        save_paths = self._collect_scan_add_paths(appid, selected_path)
        save_specs = self._collect_scan_add_specs(appid, selected_path)
        safe_specs, safety_issue = validate_scan_save_specs(
            appid=appid,
            selected_path=selected_path,
            save_specs=save_specs,
            save_paths=save_paths if save_paths else [selected_path],
            install_dir=str((result or {}).get("install_dir", "") or ""),
            steam_path=str(self.cfg.get("steam_path", "") or ""),
            library_path=str((result or {}).get("library_path", "") or ""),
            cfg=self.cfg,
        )
        if safety_issue:
            return None, safety_issue

        game = {"appid": appid, "name": name, "favorite": False}
        set_game_save_specs(game, safe_specs)
        candidate = self._get_scan_selected_candidate(appid, selected_path)
        apply_scanned_game_metadata(game, candidate, user_confirmed)
        if result and result.get("library_path"):
            game["library_path"] = result.get("library_path", "")
        ensure_game_storage_identity(game)
        return game, ""

    def _add_from_scan(self, appid, name, save_path):
        games = self.cfg.setdefault("games", [])
        game, safety_issue = self._build_scanned_game(
            appid, name, save_path, user_confirmed=True
        )
        if game is None:
            self._show_warning(
                self.bi("无法添加", "Cannot Add"),
                self.bi(
                    f"该存档候选未通过安全检查：{safety_issue}",
                    f"The save candidate did not pass safety validation: {safety_issue}",
                ),
            )
            return
        action, stored_game = merge_scanned_game_record(games, game, self.cfg)
        if action == "unchanged":
            self._show_info(self.bi("提示", "Notice"), self.bi(f"「{name}」已添加过", f"{name} has already been added"))
            return
        remember_recognition_path(self.cfg, appid, name, stored_game.get("save_path", ""))
        save_config(self.cfg)
        self._refresh_games_list()
        self._render_scan_results()
        if len(stored_game.get("save_paths", [])) > 1:
            self._show_info(
                self.bi("成功", "Success"),
                self.bi(
                    f"已更新「{name}」，共 {len(stored_game['save_paths'])} 个存档目录",
                    f"Updated {name} with {len(stored_game['save_paths'])} save folders",
                ),
            )
        else:
            self._show_info(self.bi("成功", "Success"), self.bi(f"已添加「{name}」", f"Added {name}"))

    def _add_all_from_scan(self):
        if self._scan_in_progress:
            return
        games = self.cfg.setdefault("games", [])
        added = 0
        for result in self._get_filtered_scan_results():
            appid = result.get("appid")
            if not result.get("save_paths"):
                continue
            selected_value = self._scan_choice_vars.get(appid).get() if appid in self._scan_choice_vars else result["save_paths"][0]
            selected = self._resolve_scan_selected_path(appid, selected_value)
            game, _safety_issue = self._build_scanned_game(
                appid, result["name"], selected, user_confirmed=False
            )
            if game is None:
                continue
            action, stored_game = merge_scanned_game_record(games, game, self.cfg)
            if action == "unchanged":
                continue
            remember_recognition_path(
                self.cfg, appid, result["name"], stored_game.get("save_path", "")
            )
            added += 1
        if not added:
            self._show_info(
                self.bi("提示", "Notice"),
                self.bi("当前没有可添加的已识别游戏", "There are no detected games available to add right now"),
            )
            return
        save_config(self.cfg)
        self._refresh_games_list()
        self._render_scan_results()
        self._show_info(
            self.bi("成功", "Success"),
            self.bi(f"已批量添加 {added} 款游戏", f"Added {added} games in bulk"),
        )

    def _manual_add_from_scan(self, appid, name):
        d = self._ask_directory(title=self.bi(f"选择「{name}」的存档文件夹", f"Choose the save folder for {name}"))
        if d:
            self._add_from_scan(appid, name, d)

    def _override_scan_result_path(self, appid, name):
        d = self._ask_directory(title=self.bi(f"选择「{name}」的存档文件夹", f"Choose the save folder for {name}"))
        if not d:
            return
        norm = os.path.normpath(d)
        for result in self._scan_results:
            if result.get("appid") != appid:
                continue
            existing_paths = [os.path.normpath(p) for p in result.get("save_paths", [])]
            if norm in existing_paths:
                idx = existing_paths.index(norm)
                if idx != 0:
                    result["save_paths"].insert(0, result["save_paths"].pop(idx))
                    candidates = result.get("save_candidates", [])
                    if idx < len(candidates):
                        candidates.insert(0, candidates.pop(idx))
            else:
                result.setdefault("save_paths", [])
                result["save_paths"] = [norm] + [p for p in result["save_paths"] if os.path.normpath(p) != norm]
                result.setdefault("save_candidates", [])
                result["save_candidates"] = [{
                    "path": norm,
                    "score": 999,
                    "source": "manual",
                    "confidence": "high",
                    "reasons": ["manual"],
                }] + [c for c in result["save_candidates"] if os.path.normpath(c.get("path", "")) != norm]
            break
        self._render_scan_results()

    # ─── 游戏列表 ───
    def _build_games_frame(self):
        frame = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["games"] = frame
        frame.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=32, pady=(28, 10), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text=self.bi("🎮 游戏列表", "🎮 Games"), font=font(20, "bold")).grid(
            row=0, column=0, sticky="w")
        ctk.CTkButton(hdr, text=self.bi("+ 手动添加", "+ Add Manually"), width=110, height=36,
                      font=font(13), corner_radius=10,
                      fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                      command=self._add_game_dialog).grid(
            row=0, column=1, sticky="e")

        self._games_search_var = ctk.StringVar()
        self._games_search_var.trace_add("write", self._schedule_games_refresh)
        search_frame = ctk.CTkFrame(frame, fg_color="transparent")
        search_frame.grid(row=1, column=0, padx=32, pady=(4, 0), sticky="ew")
        search_frame.grid_columnconfigure(0, weight=1)
        self._games_search_entry = ctk.CTkEntry(
            search_frame, textvariable=self._games_search_var,
            placeholder_text=self.bi("搜索游戏名称或 AppID …", "Search by game name or AppID …"),
            height=34, corner_radius=8, font=font(13))
        self._games_search_entry.grid(row=0, column=0, sticky="ew")

        self._games_scroll = ctk.CTkScrollableFrame(
            frame, height=440, corner_radius=12, fg_color=C_CARD_BG)
        self._games_scroll.grid(row=2, column=0, padx=30, pady=4, sticky="nsew")
        frame.grid_rowconfigure(2, weight=1)
        self._game_cards = {}
        self._games_empty_label = ctk.CTkLabel(
            self._games_scroll,
            text="",
            text_color=C_SUBTLE_TEXT,
            font=font(13),
        )
        pager = ctk.CTkFrame(frame, fg_color="transparent")
        pager.grid(row=3, column=0, padx=30, pady=(2, 14), sticky="ew")
        pager.grid_columnconfigure(0, weight=1)
        self._games_page_label = ctk.CTkLabel(
            pager, text="", text_color=C_SUBTLE_TEXT, font=font(12)
        )
        self._games_page_label.grid(row=0, column=0, sticky="w")
        self._games_prev_btn = ctk.CTkButton(
            pager, text=self.bi("上一页", "Prev"), width=76, height=30,
            font=font(12), corner_radius=6,
            fg_color=BTN_SECONDARY, hover_color=BTN_SECONDARY_H,
            command=lambda: self._on_games_page_delta(-1))
        self._games_prev_btn.grid(row=0, column=1, padx=(8, 4), sticky="e")
        self._games_next_btn = ctk.CTkButton(
            pager, text=self.bi("下一页", "Next"), width=76, height=30,
            font=font(12), corner_radius=6,
            fg_color=BTN_SECONDARY, hover_color=BTN_SECONDARY_H,
            command=lambda: self._on_games_page_delta(1))
        self._games_next_btn.grid(row=0, column=2, padx=(4, 0), sticky="e")

    def _refresh_games_list(self):
        games = self.cfg.get("games", [])
        search_text = self._games_search_var.get().strip().lower() if hasattr(self, "_games_search_var") else ""
        active_keys = set()
        for item in self._game_cards.values():
            item["card"].pack_forget()
        if not games:
            self._games_empty_label.configure(
                text=self.bi(
                    "还没有添加游戏，请前往「扫描游戏」自动检测或手动添加",
                    "No games added yet. Go to Scan Games to detect them automatically or add one manually.",
                )
            )
            self._games_empty_label.pack(pady=40)
            if self._games_page_label:
                self._games_page_label.configure(text=self.bi("0 个游戏", "0 games"))
            if self._games_prev_btn:
                self._games_prev_btn.configure(state="disabled")
            if self._games_next_btn:
                self._games_next_btn.configure(state="disabled")
            return
        filtered_games = []
        for idx, g in enumerate(games):
            haystack = g.get("_search_blob")
            if haystack is None:
                haystack = f"{str(g.get('name', '') or '').lower()} {str(g.get('appid', '') or '').lower()}"
                g["_search_blob"] = haystack
            if search_text and search_text not in haystack:
                continue
            filtered_games.append((idx, g))

        filtered_games.sort(key=lambda item: (0 if is_game_favorite(item[1]) else 1, item[0]))

        total_games = len(filtered_games)
        self._games_page, start, end = self._page_range(
            self._games_page, total_games, self._games_page_size
        )
        page_games = filtered_games[start:end]
        total_pages = max(1, (total_games + self._games_page_size - 1) // self._games_page_size)
        if self._games_page_label:
            if total_games:
                self._games_page_label.configure(
                    text=self.bi(
                        f"第 {self._games_page + 1}/{total_pages} 页 · 显示 {start + 1}-{end} / {total_games} 个游戏",
                        f"Page {self._games_page + 1}/{total_pages} · Showing {start + 1}-{end} of {total_games} games",
                    )
                )
            else:
                self._games_page_label.configure(text=self.bi("0 个游戏", "0 games"))
        if self._games_prev_btn:
            self._games_prev_btn.configure(state="normal" if self._games_page > 0 else "disabled")
        if self._games_next_btn:
            self._games_next_btn.configure(state="normal" if end < total_games else "disabled")

        for idx, g in page_games:
            key = get_game_cache_key(g)
            active_keys.add(key)
            item = self._game_cards.get(key)
            if item is None:
                card = ctk.CTkFrame(self._games_scroll,
                                    fg_color=("#f1f5f9", "#252640"), corner_radius=12)
                card.grid_columnconfigure(0, weight=1)
                title = ctk.CTkLabel(card, text="", font=font(14, "bold"),
                                     text_color=C_BODY_TEXT, anchor="w", justify="left",
                                     wraplength=620)
                title.grid(row=0, column=0, padx=14, pady=(10, 4), sticky="ew")
                self._register_wrap(title, 0.66, 300)
                favorite_btn = ctk.CTkButton(
                    card, text="", width=34, height=28,
                    font=font(13, "bold"), corner_radius=6)
                favorite_btn.grid(row=0, column=1, padx=14, pady=(8, 4), sticky="e")
                path_label = ctk.CTkLabel(card, text="", font=font(11),
                                          text_color=C_SUBTLE_TEXT,
                                          wraplength=580, anchor="w",
                                          justify="left")
                path_label.grid(row=1, column=0, columnspan=2, padx=14, pady=(0, 4), sticky="w")
                self._register_wrap(path_label, 0.62, 280)
                self._attach_tooltip(path_label, lambda lbl=path_label: getattr(lbl, "_full_path_text", ""))
                backup_label = ctk.CTkLabel(card, text="", font=font(11),
                                            text_color=C_SUBTLE_TEXT)
                backup_label.grid(row=2, column=0, padx=14, pady=(0, 10), sticky="w")
                bb = ctk.CTkFrame(card, fg_color="transparent")
                bb.grid(row=2, column=1, padx=14, pady=(0, 10), sticky="e")
                detail_btn = ctk.CTkButton(
                    bb, text=self.bi("详情", "Details"), width=64, height=28,
                    font=font(12, "bold"), corner_radius=6,
                    fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H)
                detail_btn.pack(side="left", padx=2)
                backup_btn = self._register_busy_widget(ctk.CTkButton(
                    bb, text=self.bi("备份", "Backup"), width=56, height=28,
                    font=font(12), corner_radius=6,
                    fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H))
                backup_btn.pack(side="left", padx=2)
                delete_btn = self._register_busy_widget(ctk.CTkButton(
                    bb, text=self.bi("删除", "Delete"), width=56, height=28,
                    font=font(12), corner_radius=6,
                    fg_color=BTN_DANGER, hover_color=BTN_DANGER_H))
                delete_btn.pack(side="left", padx=2)
                item = {
                    "card": card,
                    "title": title,
                    "path": path_label,
                    "backup": backup_label,
                    "favorite_btn": favorite_btn,
                    "detail_btn": detail_btn,
                    "backup_btn": backup_btn,
                    "delete_btn": delete_btn,
                }
                self._game_cards[key] = item

            nm = g["name"]
            if g.get("appid"):
                nm += f"  (AppID: {g['appid']})"
            if is_game_favorite(g):
                nm = "★ " + nm
            item["title"].configure(text=nm)

            save_paths = get_game_save_path_templates(g, existing_only=False)
            p = save_paths[0] if save_paths else g.get("save_path", "")
            if len(save_paths) > 1:
                p += self.bi(f"  (+{len(save_paths)-1} 个目录)", f"  (+{len(save_paths)-1} folders)")
            item["path"]._full_path_text = "\n".join(save_paths) if save_paths else p
            item["path"].configure(text=f"📁 {p}")

            bc = self._get_game_backup_count_cached(g)
            item["backup"].configure(text=self.bi(f"备份数：{bc}", f"Backups: {bc}"))

            favorite_on = is_game_favorite(g)
            item["favorite_btn"].configure(
                text="★" if favorite_on else "☆",
                fg_color=BTN_WARN if favorite_on else BTN_SECONDARY,
                hover_color=BTN_WARN_H if favorite_on else BTN_SECONDARY_H,
                text_color="#ffffff" if favorite_on else C_BODY_TEXT,
                command=lambda i=idx: self._toggle_game_favorite(i),
            )
            item["detail_btn"].configure(command=lambda i=idx: self._show_game_detail(i))
            item["backup_btn"].configure(command=lambda i=idx: self._manual_backup(i))
            item["delete_btn"].configure(command=lambda i=idx: self._delete_game(i))

            click_handler = lambda _e=None, i=idx: self._show_game_detail(i)
            item["card"].bind("<Button-1>", click_handler)
            item["card"].configure(cursor="hand2")
            item["title"].bind("<Button-1>", click_handler)
            item["path"].bind("<Button-1>", click_handler)
            item["path"].bind(
                "<Button-3>",
                lambda _e=None, text=item["path"]._full_path_text: self._copy_to_clipboard(
                    text, self.bi("完整路径已复制到剪贴板", "Full path copied to clipboard")),
            )
            item["backup"].bind("<Button-1>", click_handler)
            item["title"].configure(cursor="hand2")
            item["path"].configure(cursor="hand2")
            item["backup"].configure(cursor="hand2")
            item["card"].pack(fill="x", padx=4, pady=3)

        stale_keys = [key for key in self._game_cards.keys() if key not in active_keys]
        for key in stale_keys:
            item = self._game_cards.pop(key, None)
            if item:
                item["card"].destroy()

        if page_games:
            self._games_empty_label.pack_forget()
        else:
            self._games_empty_label.configure(
                text=self.bi("没有匹配的游戏", "No matching games")
            )
            self._games_empty_label.pack(pady=40)

    def _create_save_paths_editor(self, parent, initial_specs=None, width=420):
        state = {"rows": []}
        initial = _normalize_unique_save_specs(initial_specs or [], for_storage=True)
        if not initial:
            initial = [{"base": "", "includes": [], "recursive": True}]

        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(wrap, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr,
            text=self.bi(
                "可添加文件夹或指定文件，支持 %USERPROFILE% / %APPDATA% / %LOCALAPPDATA%",
                "Add folders or specific files. %USERPROFILE% / %APPDATA% / %LOCALAPPDATA% are supported.",
            ),
            font=font(11),
            text_color=C_SUBTLE_TEXT,
            wraplength=330,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkButton(
            hdr,
            text=self.bi("+ 添加目录", "+ Add Folder"),
            width=100,
            height=30,
            font=font(12),
            corner_radius=8,
            fg_color=BTN_PRIMARY,
            hover_color=BTN_PRIMARY_H,
            command=lambda: _append_row({"base": "", "includes": [], "recursive": True}),
        ).grid(row=0, column=1, padx=(8, 6), sticky="e")
        ctk.CTkButton(
            hdr,
            text=self.bi("+ 添加文件", "+ Add Files"),
            width=100,
            height=30,
            font=font(12),
            corner_radius=8,
            fg_color=BTN_BLUE,
            hover_color=BTN_BLUE_H,
            command=lambda: _choose_files(),
        ).grid(row=0, column=2, sticky="e")

        rows_frame = ctk.CTkFrame(wrap, fg_color="transparent")
        rows_frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        rows_frame.grid_columnconfigure(0, weight=1)

        def _browse_row(row):
            chosen = self._ask_directory(
                title=self.bi("选择存档文件夹", "Choose a save folder")
            )
            if chosen:
                row["var"].set(chosen)

        def _collect_specs():
            specs = []
            for row in state["rows"]:
                base = row["var"].get().strip()
                if not base:
                    continue
                specs.append({
                    "base": base,
                    "includes": list(row.get("includes", [])),
                    "recursive": bool(row.get("recursive", True)),
                })
            return _normalize_unique_save_specs(specs, for_storage=True)

        def _replace_rows(specs):
            state["rows"] = []
            for spec in specs:
                state["rows"].append({
                    "var": ctk.StringVar(value=spec.get("base", "")),
                    "includes": list(spec.get("includes", [])),
                    "recursive": bool(spec.get("recursive", True)),
                })
            if not state["rows"]:
                state["rows"].append({
                    "var": ctk.StringVar(value=""),
                    "includes": [],
                    "recursive": True,
                })
            _render_rows()

        def _choose_files():
            chosen = self._ask_open_filenames(
                title=self.bi("选择一个或多个存档文件", "Choose one or more save files"),
                filetypes=[(self.bi("全部文件", "All Files"), "*.*")],
            )
            if chosen:
                _replace_rows(_merge_save_specs_with_selected_files(
                    _collect_specs(), chosen, for_storage=True))

        def _set_primary(index: int):
            rows = state["rows"]
            if 0 <= index < len(rows):
                rows.insert(0, rows.pop(index))
                _render_rows()

        def _remove_row(index: int):
            rows = state["rows"]
            if len(rows) == 1:
                rows[0]["var"].set("")
                rows[0]["includes"] = []
                rows[0]["recursive"] = True
            elif 0 <= index < len(rows):
                rows.pop(index)
            _render_rows()

        def _append_row(spec: dict):
            state["rows"].append({
                "var": ctk.StringVar(value=spec.get("base", "")),
                "includes": list(spec.get("includes", [])),
                "recursive": bool(spec.get("recursive", True)),
            })
            _render_rows()

        def _render_rows():
            for child in rows_frame.winfo_children():
                child.destroy()
            for idx, row in enumerate(state["rows"]):
                row_frame = ctk.CTkFrame(rows_frame, fg_color=("white", "#20223a"), corner_radius=10)
                row_frame.grid(row=idx, column=0, sticky="ew", pady=(0, 8))
                row_frame.grid_columnconfigure(1, weight=1)

                is_file_rule = bool(row.get("includes"))
                if idx == 0:
                    badge_text = self.bi("主文件规则", "Primary Files") if is_file_rule else self.bi("主目录", "Primary Folder")
                else:
                    badge_text = self.bi("指定文件", "Files") if is_file_rule else self.bi(f"目录 {idx+1}", f"Folder {idx+1}")
                badge_color = BTN_SUCCESS if idx == 0 else ("#cbd5e1", "#374151")
                ctk.CTkLabel(
                    row_frame,
                    text=badge_text,
                    width=78,
                    font=font(11, "bold" if idx == 0 else "normal"),
                    text_color=("white" if idx == 0 else C_BODY_TEXT),
                    fg_color=badge_color,
                    corner_radius=8,
                    padx=8,
                    pady=5,
                ).grid(row=0, column=0, padx=(10, 8), pady=10, sticky="w")

                entry = ctk.CTkEntry(
                    row_frame,
                    textvariable=row["var"],
                    width=width,
                    font=font(12),
                    placeholder_text=self.bi("存档文件夹或规则基准目录", "Save folder or rule base folder"),
                )
                entry.grid(row=0, column=1, padx=(0, 8), pady=10, sticky="ew")

                ctk.CTkButton(
                    row_frame,
                    text=self.t("browse"),
                    width=64,
                    height=30,
                    font=font(12),
                    corner_radius=8,
                    fg_color=BTN_PRIMARY,
                    hover_color=BTN_PRIMARY_H,
                    command=lambda r=row: _browse_row(r),
                ).grid(row=0, column=2, padx=(0, 8), pady=10)

                ctk.CTkButton(
                    row_frame,
                    text=self.bi("置顶", "Primary"),
                    width=66,
                    height=30,
                    font=font(11),
                    corner_radius=8,
                    fg_color=BTN_BLUE if idx != 0 else ("#d1fae5", "#064e3b"),
                    hover_color=BTN_BLUE_H if idx != 0 else ("#a7f3d0", "#065f46"),
                    text_color=("white" if idx != 0 else "#ecfdf5"),
                    state="normal" if idx != 0 else "disabled",
                    command=lambda i=idx: _set_primary(i),
                ).grid(row=0, column=3, padx=(0, 8), pady=10)

                ctk.CTkButton(
                    row_frame,
                    text=self.bi("删除", "Remove"),
                    width=66,
                    height=30,
                    font=font(11),
                    corner_radius=8,
                    fg_color=BTN_DANGER,
                    hover_color=BTN_DANGER_H,
                    command=lambda i=idx: _remove_row(i),
                ).grid(row=0, column=4, padx=(0, 10), pady=10)

                if is_file_rule:
                    mode_text = self.bi("包含子目录", "including subfolders") if row.get("recursive", True) else self.bi("仅此目录", "this folder only")
                    rule_text = self.bi("文件规则", "File rule") + ": " + ", ".join(row["includes"])
                    ctk.CTkLabel(
                        row_frame,
                        text=f"{rule_text}  ·  {mode_text}",
                        font=font(10),
                        text_color=C_SUBTLE_TEXT,
                        anchor="w",
                        justify="left",
                        wraplength=500,
                    ).grid(row=1, column=1, columnspan=4, padx=(0, 10), pady=(0, 9), sticky="w")

        def _get_specs():
            return _collect_specs()

        state["frame"] = wrap
        state["get_specs"] = _get_specs
        for spec in initial:
            state["rows"].append({
                "var": ctk.StringVar(value=spec.get("base", "")),
                "includes": list(spec.get("includes", [])),
                "recursive": bool(spec.get("recursive", True)),
            })
        _render_rows()
        return state

    def _game_form_dialog(self, idx: Optional[int] = None):
        """通用游戏添加/编辑对话框。idx=None 为添加模式，否则为编辑模式。"""
        is_edit = idx is not None
        g = self.cfg["games"][idx] if is_edit else None
        dlg_title = self.bi("编辑游戏", "Edit Game") if is_edit else self.bi("手动添加游戏", "Add Game Manually")
        d = self._create_popup(dlg_title, "700x600")
        d.grid_columnconfigure(0, weight=1)
        r = 0
        ctk.CTkLabel(d, text=self.bi("游戏名称", "Game Name"), font=font(13)).grid(
            row=r, column=0, padx=24, pady=(20, 4), sticky="w")
        r += 1
        ne = ctk.CTkEntry(d, width=620, placeholder_text=self.bi("例如：Elden Ring", "Example: Elden Ring"))
        if is_edit:
            ne.insert(0, g["name"])
        ne.grid(row=r, column=0, padx=24, sticky="ew")
        r += 1
        ae = None
        if not is_edit:
            ctk.CTkLabel(d, text=self.bi("AppID（可选）", "AppID (Optional)"), font=font(13)).grid(
                row=r, column=0, padx=24, pady=(12, 4), sticky="w")
            r += 1
            ae = ctk.CTkEntry(d, width=200, placeholder_text=self.bi("如 1245620", "For example: 1245620"))
            ae.grid(row=r, column=0, padx=24, sticky="w")
            r += 1
        ctk.CTkLabel(d, text=self.bi("存档位置", "Save Locations"), font=font(13)).grid(
            row=r, column=0, padx=24, pady=(12, 4), sticky="w")
        r += 1
        initial_specs = []
        if is_edit:
            raw_specs = g.get("save_specs", [])
            if isinstance(raw_specs, list):
                initial_specs = _normalize_unique_save_specs(raw_specs, for_storage=True)
            if not initial_specs:
                initial_specs = [
                    _default_save_spec(path, for_storage=True)
                    for path in get_game_save_path_templates(g, existing_only=False)
                ]
        editor = self._create_save_paths_editor(d, initial_specs, width=420)
        editor["frame"].grid(row=r, column=0, padx=24, sticky="ew")
        r += 1
        ctk.CTkLabel(d, text=self.bi("自定义监控程序（可选）", "Custom Monitor Process (Optional)"), font=font(13)).grid(
            row=r, column=0, padx=24, pady=(12, 4), sticky="w")
        r += 1
        pe = ctk.CTkEntry(
            d, width=620,
            placeholder_text=self.bi(
                "例如：game.exe, game-win64-shipping.exe",
                "For example: game.exe, game-win64-shipping.exe",
            ),
        )
        if is_edit:
            pe.insert(0, format_monitor_processes(g.get("monitor_processes", [])))
        pe.grid(row=r, column=0, padx=24, sticky="ew")
        r += 1
        ctk.CTkLabel(
            d,
            text=self.bi("填写进程名即可，支持多个，用逗号或换行分隔", "Enter process names only. Multiple names are supported, separated by commas or new lines."),
            font=font(11),
            text_color=C_SUBTLE_TEXT,
        ).grid(row=r, column=0, padx=24, pady=(4, 0), sticky="w")
        r += 1
        def _sv():
            if is_edit:
                old_game = dict(self.cfg["games"][idx])
                old_game["save_paths"] = list(get_game_save_paths(self.cfg["games"][idx], existing_only=False))
                new_name = ne.get().strip()
                new_specs = editor["get_specs"]()
                if not new_name or not new_specs:
                    self._show_warning(self.bi("提示", "Notice"), self.bi("请填写名称并至少保留一个存档文件夹或文件规则", "Please enter a name and keep at least one save folder or file rule")); return
                self.cfg["games"][idx]["name"] = new_name
                set_game_save_specs(self.cfg["games"][idx], new_specs)
                self.cfg["games"][idx]["manual_save_specs"] = True
                set_game_monitor_processes(self.cfg["games"][idx], pe.get().strip())
                clear_game_sync_state(self.cfg, old_game)
                if old_game.get("save_path") and old_game.get("save_path") != self.cfg["games"][idx].get("save_path"):
                    exclude_recognition_path(self.cfg, old_game.get("appid", ""), old_game.get("name", ""), old_game.get("save_path", ""))
                remember_recognition_path(self.cfg, self.cfg["games"][idx].get("appid", ""), new_name, self.cfg["games"][idx].get("save_path", ""))
            else:
                n = ne.get().strip()
                save_specs = editor["get_specs"]()
                if not n or not save_specs:
                    self._show_warning(self.bi("提示", "Notice"), self.bi("请填写名称并至少添加一个存档文件夹或文件", "Please enter a name and add at least one save folder or file")); return
                appid = ae.get().strip()
                game = {
                    "name": n,
                    "appid": appid,
                    "auto_backup": True,
                    "watch_backup": True,
                    "backup_on_exit": False,
                    "sync_enabled": True,
                    "favorite": False,
                }
                set_game_save_specs(game, save_specs)
                game["manual_save_specs"] = True
                set_game_monitor_processes(game, pe.get().strip())
                ensure_game_storage_identity(game)
                self.cfg.setdefault("games", []).append(game)
                remember_recognition_path(self.cfg, appid, n, game["save_path"])
            self._invalidate_game_backup_count_cache()
            save_config(self.cfg); self._refresh_games_list(); d.destroy()
        btn_text = self.bi("保存", "Save") if is_edit else self.bi("确认添加", "Add Game")
        ctk.CTkButton(d, text=btn_text, width=140, height=38, font=font(13, "bold"),
                      corner_radius=10, fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                      command=_sv).grid(row=r, column=0, padx=24, pady=16)

    def _add_game_dialog(self):
        self._game_form_dialog(idx=None)

    def _edit_game_dialog(self, idx):
        self._game_form_dialog(idx=idx)

    def _delete_game(self, idx):
        g = self.cfg["games"][idx]
        n = g["name"]
        if self._ask_yes_no(self.bi("确认", "Confirm"), self.bi(f"删除「{n}」？（备份保留）", f"Delete {n}? Existing backups will be kept.")):
            clear_game_sync_state(self.cfg, g)
            self.cfg["games"].pop(idx)
            self._invalidate_game_backup_count_cache()
            save_config(self.cfg); self._refresh_games_list()

    def _browse(self, entry, callback=None):
        f = self._ask_directory()
        if f:
            entry.delete(0, "end")
            entry.insert(0, f)
            if callback:
                callback()

    def _import_save(self, idx):
        g = self.cfg["games"][idx]
        choice = self._ask_yes_no_cancel(
            self.bi("导入存档", "Import Save"),
            self.bi(
                f"为「{g['name']}」导入存档\n\n是 → 导入 ZIP 备份文件\n否 → 导入存档文件夹\n取消 → 返回",
                f"Import a save for {g['name']}\n\nYes → Import a ZIP backup\nNo → Import a save folder\nCancel → Go back",
            ))
        if choice is None: return
        if choice:
            src = self._ask_open_filename(
                title=self.bi("选择 ZIP 备份", "Choose a ZIP backup"), filetypes=[("ZIP", "*.zip"), (self.bi("全部", "All"), "*.*")])
            if not src: return
            gd = BACKUP_ROOT / get_game_storage_key(g); gd.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = gd / f"{ts}_imported.zip"
            shutil.copy2(src, dest)
            meta = {"game": g["name"], "appid": g.get("appid", ""),
                    "game_uid": get_game_uid(g), "storage_key": str(g.get("storage_key", "") or ""),
                    "timestamp": ts, "note": self.bi("导入的备份", "Imported backup"),
                    "source": src, "size": dest.stat().st_size}
            with open(dest.with_suffix(".meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            if self._ask_yes_no(self.bi("导入成功", "Import Complete"), self.bi("已导入为备份！\n是否立即还原？", "Imported as a backup.\nRestore it now?")):
                try:
                    targets = get_game_save_specs(g, existing_only=False)
                    restore_backup(str(dest), targets if targets else g.get("save_path", ""))
                    self._show_info(self.bi("成功", "Success"), self.bi("已还原！", "Restored successfully!"))
                except Exception as e: self._show_error(self.bi("失败", "Failed"), str(e))
        else:
            folder = self._ask_directory(title=self.bi("选择存档文件夹", "Choose a save folder"))
            if not folder: return
            gd = BACKUP_ROOT / get_game_storage_key(g); gd.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            zp = gd / f"{ts}_imported.zip"
            fp = Path(folder)
            try:
                file_count = package_save_folder_to_zip(fp, zp)
                if file_count <= 0:
                    self._show_warning(
                        self.bi("导入失败", "Import Failed"),
                        self.bi("所选文件夹中没有可导入的存档文件。", "No importable save files were found in the selected folder."),
                    )
                    return
            except Exception as e:
                self._show_error(
                    self.bi("导入失败", "Import Failed"),
                    self.bi(f"打包导入文件夹失败：\n{type(e).__name__}: {e}",
                            f"Failed to package the imported folder:\n{type(e).__name__}: {e}"),
                )
                return
            meta = {"game": g["name"], "appid": g.get("appid", ""),
                    "game_uid": get_game_uid(g), "storage_key": str(g.get("storage_key", "") or ""),
                    "timestamp": ts, "note": self.bi("导入的存档文件夹", "Imported save folder"),
                    "source": folder, "size": zp.stat().st_size, "file_count": file_count}
            with open(zp.with_suffix(".meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)
            if self._ask_yes_no(self.bi("导入成功", "Import Complete"), self.bi("已打包为备份！\n是否立即还原？", "Packed as a backup.\nRestore it now?")):
                try:
                    targets = get_game_save_specs(g, existing_only=False)
                    restore_backup(str(zp), targets if targets else g.get("save_path", ""))
                    self._show_info(self.bi("成功", "Success"), self.bi("已还原！", "Restored successfully!"))
                except Exception as e: self._show_error(self.bi("失败", "Failed"), str(e))

    # ─── 游戏详情 ───
    def _build_game_detail_frame(self):
        outer = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["game_detail"] = outer
        outer.grid_columnconfigure(0, weight=1)
        outer.grid_rowconfigure(0, weight=1)
        # 使用可滚动容器包裹所有内容，避免小屏幕内容溢出
        frame = ctk.CTkScrollableFrame(outer, fg_color=C_MAIN_BG, corner_radius=0)
        frame.grid(row=0, column=0, sticky="nsew")
        frame.grid_columnconfigure(0, weight=1)

        # Row 0: 标题栏（卡片式）
        hdr = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=14)
        hdr.grid(row=0, column=0, padx=28, pady=(24, 10), sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkButton(hdr, text="←", width=36, height=36,
                      font=font(16, "bold"), corner_radius=8,
                      fg_color=("#e2e8f0", "#2d3055"),
                      hover_color=C_NAV_HOVER,
                      text_color=C_BODY_TEXT,
                      command=lambda: self._show_frame("games")).grid(
            row=0, column=0, padx=(14, 8), pady=14, sticky="w")
        self._detail_title = ctk.CTkLabel(hdr, text="", font=font(20, "bold"))
        self._detail_title.grid(row=0, column=1, pady=14, sticky="w")
        self._register_wrap(self._detail_title, 0.62, 300)
        self._detail_status_badge = ctk.CTkLabel(
            hdr, text="", font=font(12, "bold"))
        self._detail_status_badge.grid(row=0, column=2, padx=(0, 8), pady=14, sticky="e")

        # Row 1: 统计卡片 (4 列)
        stats_frame = ctk.CTkFrame(frame, fg_color="transparent")
        stats_frame.grid(row=1, column=0, padx=28, pady=(0, 8), sticky="ew")
        stats_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)
        self._detail_stats = {}
        stat_defs = [
            ("appid", "🔖 AppID", "—", "#eef2ff", "#2e2b6b", "#6366f1"),
            ("backups", self.bi("💾 备份数量", "💾 Backups"), "0", "#ecfdf5", "#0c4535", "#10b981"),
            ("size", self.bi("📦 备份大小", "📦 Backup Size"), "0 B", "#fff7ed", "#5e1e0c", "#f97316"),
            ("files", self.bi("📄 存档文件", "📄 Save Files"), "—", "#fdf2f8", "#6f0f33", "#ec4899"),
        ]
        for col, (key, title, default, lbg, dbg, accent) in enumerate(stat_defs):
            sc = ctk.CTkFrame(stats_frame, fg_color=(lbg, dbg), corner_radius=12)
            sc.grid(row=0, column=col, padx=4, pady=4, sticky="nsew")
            ctk.CTkLabel(sc, text=title, font=font(11),
                         text_color=C_SUBTLE_TEXT).grid(
                row=0, column=0, padx=14, pady=(14, 0), sticky="w")
            vl = ctk.CTkLabel(sc, text=default, font=font(18, "bold"),
                              text_color=(accent, accent))
            vl.grid(row=1, column=0, padx=14, pady=(4, 14), sticky="w")
            self._detail_stats[key] = vl

        # Row 2: 存档路径卡片
        path_card = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=12)
        path_card.grid(row=2, column=0, padx=28, pady=(0, 8), sticky="ew")
        path_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(path_card, text=self.bi("📁 存档路径", "📁 Save Path"), font=font(12, "bold"),
                     text_color=C_SUBTLE_TEXT).grid(
            row=0, column=0, padx=(16, 8), pady=14, sticky="w")
        self._detail_path = ctk.CTkLabel(
            path_card, text="", font=font(12), text_color=C_BODY_TEXT,
            wraplength=520, anchor="w", justify="left")
        self._detail_path.grid(row=0, column=1, padx=(0, 8), pady=14, sticky="w")
        self._register_wrap(self._detail_path, 0.58, 280)
        self._attach_tooltip(self._detail_path, lambda: getattr(self._detail_path, "_full_path_text", ""))
        self._detail_path.bind(
            "<Button-3>",
            lambda _e=None: self._copy_to_clipboard(
                getattr(self._detail_path, "_full_path_text", ""),
                self.bi("完整路径已复制到剪贴板", "Full path copied to clipboard"),
            ),
            add="+",
        )
        self._detail_open_btn = ctk.CTkButton(
            path_card, text=self.bi("打开", "Open"), width=56, height=28, font=font(11),
            corner_radius=6, fg_color=("#64748b", "#64748b"),
            hover_color=("#475569", "#475569"))
        self._detail_open_btn.grid(row=0, column=2, padx=(0, 14), pady=14)

        # Row 3: 进程状态 & 同步监控卡片
        proc_card = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=12)
        proc_card.grid(row=3, column=0, padx=28, pady=(0, 8), sticky="ew")
        proc_card.grid_columnconfigure(1, weight=1)
        # 进程状态行
        ctk.CTkLabel(proc_card, text=self.bi("🔍 进程状态", "🔍 Process Status"), font=font(12, "bold"),
                     text_color=C_SUBTLE_TEXT).grid(
            row=0, column=0, padx=(16, 8), pady=(14, 4), sticky="w")
        self._detail_proc_status = ctk.CTkLabel(
            proc_card, text=self.bi("检测中...", "Detecting..."), font=font(12), text_color=C_BODY_TEXT,
            anchor="w", justify="left")
        self._detail_proc_status.grid(row=0, column=1, padx=(0, 8), pady=(14, 4), sticky="w")
        self._detail_proc_badge = ctk.CTkLabel(
            proc_card, text="", font=font(11, "bold"))
        self._detail_proc_badge.grid(row=0, column=2, padx=(0, 4), pady=(14, 4), sticky="e")
        ctk.CTkButton(proc_card, text=self.bi("诊断", "Diagnose"), width=56, height=28, font=font(11),
                      corner_radius=6, fg_color=("#64748b", "#64748b"),
                      hover_color=("#475569", "#475569"),
                      command=self._show_process_diagnose).grid(
            row=0, column=3, padx=(0, 14), pady=(14, 4))
        # 同步监控行
        ctk.CTkLabel(proc_card, text=self.bi("📡 同步监控", "📡 Sync Monitor"), font=font(12, "bold"),
                     text_color=C_SUBTLE_TEXT).grid(
            row=1, column=0, padx=(16, 8), pady=(4, 4), sticky="w")
        self._detail_sync_monitor_status = ctk.CTkLabel(
            proc_card, text=self.bi("检测中...", "Detecting..."), font=font(12), text_color=C_BODY_TEXT,
            anchor="w", justify="left")
        self._detail_sync_monitor_status.grid(row=1, column=1, padx=(0, 8), pady=(4, 4), sticky="w")
        self._detail_sync_badge = ctk.CTkLabel(
            proc_card, text="", font=font(11, "bold"))
        self._detail_sync_badge.grid(row=1, column=2, padx=(0, 4), pady=(4, 4), sticky="e")
        ctk.CTkButton(proc_card, text=self.bi("日志", "Logs"), width=56, height=28, font=font(11),
                      corner_radius=6, fg_color=("#64748b", "#64748b"),
                      hover_color=("#475569", "#475569"),
                      command=self._show_sync_log).grid(
            row=1, column=3, padx=(0, 14), pady=(4, 4))
        # 最近同步活动
        self._detail_sync_recent = ctk.CTkLabel(
            proc_card, text="", font=font(11), text_color=C_SUBTLE_TEXT,
            anchor="w", justify="left", wraplength=700)
        self._detail_sync_recent.grid(row=2, column=0, columnspan=4,
                                       padx=16, pady=(0, 14), sticky="w")
        self._register_wrap(self._detail_sync_recent, 0.72, 320)

        # Row 4: 快捷操作卡片
        action_card = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=12)
        action_card.grid(row=4, column=0, padx=28, pady=(0, 10), sticky="ew")
        action_card.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(action_card, text=self.bi("⚡ 快捷操作", "⚡ Quick Actions"), font=font(12, "bold"),
                     text_color=C_SUBTLE_TEXT).grid(
            row=0, column=0, padx=16, pady=(12, 6), sticky="w")
        self._detail_btns = ctk.CTkFrame(action_card, fg_color="transparent")
        self._detail_btns.grid(row=1, column=0, padx=12, pady=(0, 14), sticky="ew")
        self._detail_btns.grid_columnconfigure((0, 1, 2, 3, 4), weight=1)

        # Row 5: 单独设置卡片
        settings_card = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=12)
        settings_card.grid(row=5, column=0, padx=28, pady=(0, 8), sticky="ew")
        settings_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(settings_card, text=self.bi("🔧 游戏设置", "🔧 Game Settings"), font=font(12, "bold"),
                     text_color=C_SUBTLE_TEXT).grid(
            row=0, column=0, padx=(16, 8), pady=14, sticky="w")
        self._detail_auto_var = ctk.StringVar(value="on")
        self._detail_auto_switch = ctk.CTkSwitch(
            settings_card, text=self.bi("定时自动备份此游戏", "Scheduled backup for this game"),
            variable=self._detail_auto_var,
            onvalue="on", offvalue="off", font=font(12),
            command=self._toggle_game_auto_backup)
        self._detail_auto_switch.grid(row=0, column=1, padx=(0, 16), pady=14, sticky="w")
        self._detail_watch_var = ctk.StringVar(value="on")
        self._detail_watch_switch = ctk.CTkSwitch(
            settings_card, text=self.bi("文件变动时自动备份此游戏", "File-change backup for this game"),
            variable=self._detail_watch_var,
            onvalue="on", offvalue="off", font=font(12),
            command=self._toggle_game_watch_backup)
        self._detail_watch_switch.grid(row=1, column=1, padx=(0, 16), pady=(0, 14), sticky="w")
        self._detail_exit_backup_var = ctk.StringVar(value="off")
        self._detail_exit_backup_switch = ctk.CTkSwitch(
            settings_card, text=self.bi("游戏退出后自动备份", "Back up automatically after game exit"),
            variable=self._detail_exit_backup_var,
            onvalue="on", offvalue="off", font=font(12),
            command=self._toggle_game_exit_backup)
        self._detail_exit_backup_switch.grid(row=2, column=1, padx=(0, 16), pady=(0, 14), sticky="w")
        self._detail_sync_var = ctk.StringVar(value="on")
        self._detail_sync_switch = ctk.CTkSwitch(
            settings_card, text=self.bi("存档自动同步此游戏（智能云存档 / 同步全部时包含此游戏）",
                                        "Sync saves for this game (included in Smart Cloud Save and Sync All)"),
            variable=self._detail_sync_var,
            onvalue="on", offvalue="off", font=font(12),
            command=self._toggle_game_sync_enabled)
        self._detail_sync_switch.grid(row=3, column=1, padx=(0, 16), pady=(0, 14), sticky="w")
        self._detail_favorite_var = ctk.StringVar(value="off")
        self._detail_favorite_switch = ctk.CTkSwitch(
            settings_card, text=self.bi("收藏此游戏（在游戏列表中优先显示）",
                                        "Favorite this game (show it first in the Games list)"),
            variable=self._detail_favorite_var,
            onvalue="on", offvalue="off", font=font(12),
            command=self._toggle_game_favorite_from_detail)
        self._detail_favorite_switch.grid(row=4, column=1, padx=(0, 16), pady=(0, 14), sticky="w")

        # Row 6: 备份历史按钮卡片
        bk_card = ctk.CTkFrame(frame, fg_color=C_CARD_BG, corner_radius=12)
        bk_card.grid(row=6, column=0, padx=28, pady=(0, 20), sticky="ew")
        bk_card.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(bk_card, text=self.bi("📋 备份历史", "📋 Backup History"), font=font(13, "bold"),
                     text_color=C_BODY_TEXT).grid(
            row=0, column=0, padx=(16, 8), pady=14, sticky="w")
        self._detail_bk_count_label = ctk.CTkLabel(
            bk_card, text=self.bi("0 条记录", "0 records"), font=font(12), text_color=C_SUBTLE_TEXT)
        self._detail_bk_count_label.grid(row=0, column=1, pady=14, sticky="w")
        ctk.CTkButton(bk_card, text=self.bi("查看全部", "View All"), width=110, height=32,
                      font=font(12, "bold"), corner_radius=8,
                      fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                      command=self._show_backup_history).grid(
            row=0, column=2, padx=(0, 14), pady=14, sticky="e")

    def _show_game_detail(self, idx):
        self._detail_idx = idx
        self._current_frame = "game_detail"
        g = self.cfg["games"][idx]
        self._detail_title.configure(text=f"🎮 {g['name']}")

        backups = self._get_game_backups_cached(g)
        total_size = sum(b["size"] for b in backups)
        detail_metrics = self._get_game_detail_metrics_cached(g)

        # 统计卡片
        self._detail_stats["appid"].configure(
            text=g.get("appid", "") or self.bi("未设置", "Not set"))
        self._detail_stats["backups"].configure(text=str(len(backups)))
        self._detail_stats["size"].configure(text=fmt_size(total_size))
        save_paths = detail_metrics.get("display_paths", []) or detail_metrics.get("save_paths", [])
        existing_paths = detail_metrics.get("existing_paths", [])
        path_ok = bool(detail_metrics.get("path_ok", False))
        file_count = int(detail_metrics.get("file_count", 0))
        self._detail_stats["files"].configure(
            text=self.bi(f"{file_count} 个", f"{file_count}") if path_ok else "—")

        # 标题栏状态徽章
        if path_ok:
            self._detail_status_badge.configure(
                text=self.bi("● 存档正常", "● Save OK"), text_color=("#16a34a", "#4ade80"))
        else:
            self._detail_status_badge.configure(
                text=self.bi("● 路径异常", "● Path Error"), text_color=("#dc2626", "#fca5a5"))

        # 路径
        primary_path = save_paths[0] if save_paths else g.get("save_path", "")
        if len(save_paths) > 1:
            path_text = self.bi(
                f"{primary_path}\n(+{len(save_paths) - 1} 个额外目录)",
                f"{primary_path}\n(+{len(save_paths) - 1} additional folders)",
            )
        else:
            path_text = primary_path
        self._detail_path._full_path_text = "\n".join(save_paths) if save_paths else primary_path
        self._detail_path.configure(text=path_text)
        self._detail_open_btn.configure(
            command=lambda: self._open_save_folder(idx))

        # 操作按钮 (grid 等宽排列)
        for w in self._detail_btns.winfo_children(): w.destroy()
        actions = [
            (self.bi("💾 立即备份", "💾 Back Up Now"), BTN_SUCCESS, BTN_SUCCESS_H,
             lambda: self._backup_from_detail(idx)),
            (self.bi("🔄 同步存档", "🔄 Sync Saves"), BTN_BLUE, BTN_BLUE_H,
             lambda: self._manual_sync_one(idx)),
            (self.bi("📥 导入存档", "📥 Import Save"), BTN_WARN, BTN_WARN_H,
             lambda: self._import_save(idx)),
            (self.bi("📝 编辑信息", "📝 Edit"), BTN_PRIMARY, BTN_PRIMARY_H,
             lambda: self._edit_game_dialog(idx)),
            (self.bi("❌ 删除游戏", "❌ Delete Game"), BTN_DANGER, BTN_DANGER_H,
             lambda: self._delete_game_from_detail(idx)),
        ]
        for i, (txt, clr, hclr, cmd) in enumerate(actions):
            self._register_busy_widget(ctk.CTkButton(
                self._detail_btns, text=txt, height=36,
                font=font(12, "bold"), corner_radius=8,
                fg_color=clr, hover_color=hclr,
                command=cmd)).grid(row=0, column=i, padx=3, sticky="ew")

        # 单独设置：自动备份开关
        auto_on = is_game_scheduled_backup_enabled(g)
        self._detail_auto_var.set("on" if auto_on else "off")
        self._detail_watch_var.set("on" if is_game_watch_backup_enabled(g) else "off")
        self._detail_exit_backup_var.set("on" if is_game_exit_backup_enabled(g) else "off")
        self._detail_sync_var.set("on" if is_game_sync_enabled(g) else "off")
        self._detail_favorite_var.set("on" if is_game_favorite(g) else "off")

        # 备份历史：只更新计数标签
        self._detail_bk_count_label.configure(
            text=self.bi(f"{len(backups)} 条记录  ·  {fmt_size(total_size)}",
                         f"{len(backups)} records  ·  {fmt_size(total_size)}") if backups else self.bi("暂无备份", "No backups"))

        self._ensure_game_monitor()

        # 进程状态
        self._refresh_proc_status()

        # 显示详情页
        for f in self._frames.values(): f.grid_forget()
        self._frames["game_detail"].grid(row=0, column=1, sticky="nsew")
        self._highlight_nav("games")
        self._start_detail_refresh()

    def _show_backup_history(self):
        """弹窗显示当前游戏的完整备份历史"""
        g = self.cfg["games"][self._detail_idx]
        backups = self._get_game_backups_cached(g)

        d = self._create_popup(self.bi(f"备份历史 — {g['name']}", f"Backup History — {g['name']}"), "680x520")
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(1, weight=1)

        # 标题栏
        hdr = ctk.CTkFrame(d, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=20, pady=(16, 8), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text=self.bi(f"📋 {g['name']} — 备份历史", f"📋 {g['name']} — Backup History"),
                     font=font(16, "bold")).grid(row=0, column=0, sticky="w")
        total_size = sum(b["size"] for b in backups)
        ctk.CTkLabel(hdr, text=self.bi(f"{len(backups)} 条  ·  {fmt_size(total_size)}",
                                       f"{len(backups)} items  ·  {fmt_size(total_size)}"),
                     font=font(12), text_color=C_SUBTLE_TEXT).grid(
            row=0, column=1, sticky="e")

        # 备份列表
        scroll = ctk.CTkScrollableFrame(d, corner_radius=12, fg_color=C_CARD_BG)
        scroll.grid(row=1, column=0, padx=16, pady=(0, 16), sticky="nsew")

        if not backups:
            ctk.CTkLabel(scroll, text=self.bi("暂无备份记录", "No backup history yet"),
                         text_color=C_SUBTLE_TEXT, font=font(13)).pack(pady=40)
        else:
            for b in backups:
                row = ctk.CTkFrame(scroll, fg_color=("#f1f5f9", "#252640"),
                                   corner_radius=12)
                row.pack(fill="x", padx=4, pady=3)
                row.grid_columnconfigure(0, weight=1)
                ctk.CTkLabel(row, text=f"🕐 {self._fmt_ts(b['timestamp'])}",
                             font=font(12, "bold"), text_color=C_BODY_TEXT).grid(
                    row=0, column=0, padx=14, pady=(10, 0), sticky="w")
                sub = fmt_size(b['size'])
                note_text = localize_backup_note(b.get("note", ""), cfg_language(self.cfg))
                if note_text: sub += f"  ·  📝 {note_text}"
                ctk.CTkLabel(row, text=sub, font=font(11),
                             text_color=C_SUBTLE_TEXT).grid(
                    row=1, column=0, padx=14, pady=(2, 10), sticky="w")
                bb = ctk.CTkFrame(row, fg_color="transparent")
                bb.grid(row=0, column=1, rowspan=2, padx=14, pady=8, sticky="e")
                ctk.CTkButton(bb, text=self.bi("还原", "Restore"), width=56, height=28, font=font(11),
                              corner_radius=6, fg_color=BTN_SUCCESS,
                              hover_color=BTN_SUCCESS_H,
                              command=lambda bp=b, gm=g, dlg=d:
                                  self._restore_from_popup(bp, gm, dlg)).pack(
                    side="left", padx=2)
                ctk.CTkButton(bb, text=self.bi("删除", "Delete"), width=56, height=28, font=font(11),
                              corner_radius=6, fg_color=BTN_DANGER,
                              hover_color=BTN_DANGER_H,
                              command=lambda bp=b, dlg=d:
                                  self._del_bk_from_popup(bp, dlg)).pack(
                    side="left", padx=2)

    def _restore_from_popup(self, backup, game, dialog):
        """从弹窗中还原备份"""
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认还原", "Confirm Restore"),
                self.bi(f"还原 {self._fmt_ts(backup['timestamp'])} 的备份？\n当前存档将被自动备份后覆盖",
                        f"Restore the backup from {self._fmt_ts(backup['timestamp'])}?\nYour current save will be backed up automatically before being overwritten.")):
            backup_path = backup["path"]
            game_copy = dict(game)
            detail_idx = self._detail_idx
            self._set_io_busy(True)
            def _worker():
                try:
                    targets = backup.get("save_specs", []) or get_game_save_specs(game_copy, existing_only=False)
                    restore_backup(backup_path, targets if targets else game_copy.get("save_path", ""))
                    self.after(0, lambda: self._on_restore_popup_done(detail_idx, dialog))
                except Exception as e:
                    self.after(0, lambda err=str(e): self._on_restore_popup_failed(err))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_restore_popup_done(self, idx, dialog):
        self._set_io_busy(False)
        self._show_info(self.bi("成功", "Success"), self.bi("存档已还原！", "Save restored successfully!"))
        dialog.destroy()
        self._show_game_detail(idx)

    def _on_restore_popup_failed(self, err):
        self._set_io_busy(False)
        self._show_error(self.bi("还原失败", "Restore Failed"), err)

    def _resolve_linked_sync_archive_deletion(self, backup: dict, game_ref) -> str:
        linked_archive = str(backup.get("linked_sync_archive", "") or "").strip()
        backup_kind = str(backup.get("backup_kind", "") or "").strip().lower()
        if not linked_archive or backup_kind != "sync_archive" or not get_game_storage_key(game_ref):
            return ""
        remove_linked = self._ask_yes_no(
            self.bi("删除同步包", "Delete Sync Archive"),
            self.bi(
                "这个备份关联了一个同步包，是否同时删除对应的本地或 WebDAV 远程同步包？",
                "This backup is linked to a sync archive. Do you also want to delete the corresponding local or WebDAV remote sync archive?",
            ),
        )
        return linked_archive if remove_linked else ""

    def _delete_backup_worker(self, backup_path: str, game_ref, linked_archive: str) -> dict:
        result = {"linked": None, "errors": []}
        if linked_archive:
            linked_result = delete_linked_sync_archive(self.cfg, game_ref, linked_archive)
            result["linked"] = linked_result
            result["errors"].extend(linked_result.get("errors", []))
        try:
            delete_backup(backup_path)
        except Exception as e:
            result["errors"].append(f"backup:{type(e).__name__}: {e}")
        return result

    def _show_delete_backup_followup(self, result: Optional[dict]):
        result = result or {}
        errors = result.get("errors", []) or []
        if errors:
            details = "\n".join(errors[:6])
            self._show_warning(
                self.bi("删除已完成", "Deletion Completed"),
                self.bi(
                    "备份已删除，但关联同步包删除时出现部分问题：\n" + details,
                    "The backup was deleted, but some problems occurred while deleting linked sync archives:\n" + details,
                ),
            )

    def _del_bk_from_popup(self, backup, dialog):
        """从弹窗中删除备份"""
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认", "Confirm"), self.bi("确定删除此备份？", "Delete this backup?")):
            backup_path = backup["path"]
            game_ref = self.cfg["games"][self._detail_idx] if self._detail_idx < len(self.cfg.get("games", [])) else {}
            linked_archive = self._resolve_linked_sync_archive_deletion(backup, game_ref)
            detail_idx = self._detail_idx
            self._set_io_busy(True)
            def _worker():
                result = self._delete_backup_worker(backup_path, game_ref, linked_archive)
                self.after(0, lambda r=result: self._on_del_bk_popup_done(detail_idx, dialog, r))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_del_bk_popup_done(self, idx, dialog, result=None):
        self._set_io_busy(False)
        dialog.destroy()
        self._show_delete_backup_followup(result)
        self._show_backup_history()
        self._show_game_detail(idx)

    def _refresh_proc_status(self):
        """检测当前游戏的进程运行状态和同步监控状态并更新 UI"""
        try:
            g = self.cfg["games"][self._detail_idx]
            runtime_key = get_game_runtime_key(g)
            appid = str(g.get("appid", "") or "").strip()
            custom_processes = get_game_monitor_processes(g)
            if not appid and not custom_processes:
                self._detail_proc_status.configure(text=self.bi("未设置 AppID 或自定义监控程序，无法检测", "No AppID or custom monitor process is configured, so detection is unavailable"))
                self._detail_proc_badge.configure(
                    text=self.bi("⚠ 未知", "⚠ Unknown"), text_color=("#d97706", "#fbbf24"))
            else:
                monitor = GameProcessMonitor(self.cfg)
                running = monitor._find_running_games()
                detail_suffix = (
                    self.bi(f"（自定义进程：{', '.join(custom_processes)}）", f"(custom process: {', '.join(custom_processes)})")
                    if custom_processes else
                    self.bi(f"(AppID: {appid})", f"(AppID: {appid})")
                )
                if runtime_key in running:
                    self._detail_proc_status.configure(
                        text=self.bi(f"游戏进程运行中 {detail_suffix}", f"Game process is running {detail_suffix}"))
                    self._detail_proc_badge.configure(
                        text=self.bi("● 运行中", "● Running"), text_color=("#16a34a", "#4ade80"))
                else:
                    self._detail_proc_status.configure(
                        text=self.bi(f"未检测到游戏进程 {detail_suffix}", f"No game process detected {detail_suffix}"))
                    self._detail_proc_badge.configure(
                        text=self.bi("○ 未运行", "○ Not Running"), text_color=C_SUBTLE_TEXT)
        except Exception:
            self._detail_proc_status.configure(text=self.bi("检测失败", "Detection failed"))
            self._detail_proc_badge.configure(
                text=self.bi("⚠ 错误", "⚠ Error"), text_color=("#dc2626", "#fca5a5"))
        # 更新同步监控状态
        try:
            sync_enabled = self.cfg.get("sync_enabled", False)
            sync_mode = self.cfg.get("sync_mode", "")
            sync_folder = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=False)
            sync_issue = get_sync_backend_issue(self.cfg.get("sync_folder", ""), self.cfg)
            current_game = self.cfg["games"][self._detail_idx] if hasattr(self, "_detail_idx") and self._detail_idx < len(self.cfg.get("games", [])) else None
            if current_game is not None and not is_game_sync_enabled(current_game):
                self._detail_sync_monitor_status.configure(text=self.bi("此游戏已关闭存档自动同步", "Save sync is disabled for this game."))
                self._detail_sync_badge.configure(
                    text=self.bi("⏸ 游戏关闭", "⏸ Game Off"), text_color=C_SUBTLE_TEXT)
            elif not sync_enabled:
                self._detail_sync_monitor_status.configure(text=self.bi("同步功能未启用，请在设置中开启", "Sync is disabled. Enable it in Settings."))
                self._detail_sync_badge.configure(
                    text=self.bi("⏸ 关闭", "⏸ Off"), text_color=C_SUBTLE_TEXT)
            elif sync_mode != "smart":
                mode_names = {
                    "upload": self._sync_mode_display("upload"),
                    "download": self._sync_mode_display("download"),
                    "bidirectional": self._sync_mode_display("bidirectional"),
                    "smart": self._sync_mode_display("smart"),
                }
                self._detail_sync_monitor_status.configure(
                    text=self.bi(
                        f"当前模式: {mode_names.get(sync_mode, sync_mode)}（非智能云存档，不监控进程）",
                        f"Current mode: {mode_names.get(sync_mode, sync_mode)} (not Smart Cloud Save, so no process monitoring)",
                    ))
                self._detail_sync_badge.configure(
                    text=self.bi("⏸ 非智能", "⏸ Non-smart"), text_color=("#d97706", "#fbbf24"))
            elif not sync_folder:
                if sync_issue == "webdav_component_missing":
                    status_text = self.bi("WebDAV 已启用，但当前运行环境缺少 webdavclient3 组件", "WebDAV is enabled, but webdavclient3 is unavailable in the current runtime")
                elif sync_issue == "webdav_url_missing":
                    status_text = self.bi("WebDAV 已启用，但服务器地址为空", "WebDAV is enabled, but the server URL is empty")
                elif sync_issue == "webdav_credentials_incomplete":
                    status_text = self.bi("WebDAV 用户名或密码不完整", "WebDAV username or password is incomplete")
                else:
                    status_text = self.bi("同步目录与 WebDAV 均未配置，请在设置中启用其中一种", "Neither a sync folder nor WebDAV is configured. Enable one in Settings.")
                self._detail_sync_monitor_status.configure(text=status_text)
                self._detail_sync_badge.configure(
                    text=self.bi("⚠ 未配置", "⚠ Not Configured"), text_color=("#dc2626", "#fca5a5"))
            elif self._game_monitor and self._game_monitor._thread and self._game_monitor._thread.is_alive():
                n_running = len(self._game_monitor._running_games)
                self._detail_sync_monitor_status.configure(
                    text=self.bi(f"智能监控运行中，正在追踪 {n_running} 个游戏进程",
                                 f"Smart monitoring is running and tracking {n_running} game process(es)"))
                self._detail_sync_badge.configure(
                    text=self.bi("● 运行中", "● Running"), text_color=("#16a34a", "#4ade80"))
            else:
                self._detail_sync_monitor_status.configure(text=self.bi("监控未启动（请尝试重启应用）", "Monitoring is not running. Try restarting the app."))
                self._detail_sync_badge.configure(
                    text=self.bi("⚠ 未启动", "⚠ Not Running"), text_color=("#dc2626", "#fca5a5"))
            # 显示最近同步日志
            if self._game_monitor and self._game_monitor.sync_log:
                recent = self._game_monitor.sync_log[-3:]
                self._detail_sync_recent.configure(text="\n".join(recent))
            else:
                self._detail_sync_recent.configure(text=self.bi("暂无同步活动记录", "No sync activity yet"))
        except Exception:
            self._detail_sync_monitor_status.configure(text=self.bi("状态未知", "Status unknown"))
            self._detail_sync_badge.configure(
                text=self.bi("⚠ 错误", "⚠ Error"), text_color=("#dc2626", "#fca5a5"))

    def _enqueue_sync_conflict_dialog(self, game: dict):
        self._sync_ui_queue.put(("conflict", get_game_sync_key(game)))

    def _drain_sync_ui_queue(self):
        try:
            while True:
                kind, payload = self._sync_ui_queue.get_nowait()
                if kind == "conflict":
                    game = find_game_by_sync_key(self.cfg, payload)
                    if game and not is_game_sync_conflict_snoozed(self.cfg, game):
                        self._show_sync_conflict_dialog(game)
        except queue.Empty:
            pass
        for game in self.cfg.get("games", []):
            if (
                get_game_sync_state(self.cfg, game).get("pending_conflict")
                and not is_game_sync_conflict_snoozed(self.cfg, game)
            ):
                self._show_sync_conflict_dialog(game)
                break
        self.after(1200, self._drain_sync_ui_queue)

    def _format_sync_side_text(self, title: str, info: dict) -> str:
        hash_short = (info.get("hash", "") or "—")[:12]
        return (
            f"{title}\n"
            f"{self.bi('路径', 'Path')}: {info.get('path', '—')}\n"
            f"{self.bi('文件数', 'Files')}: {info.get('file_count', 0)}\n"
            f"{self.bi('最后修改', 'Last Modified')}: {format_sync_time(info.get('latest_mtime', 0))}\n"
            f"{self.bi('内容摘要', 'Content Hash')}: {hash_short}"
        )

    def _show_sync_conflict_dialog(self, game: dict):
        game_key = get_game_sync_key(game)
        if game_key in self._open_conflict_dialogs or game_key in self._resolving_sync_conflicts:
            return
        state = get_game_sync_state(self.cfg, game)
        conflict = state.get("pending_conflict")
        if not isinstance(conflict, dict):
            return
        if is_game_sync_conflict_snoozed(self.cfg, game):
            return

        self._open_conflict_dialogs.add(game_key)
        d = self._create_popup(self.bi(f"同步冲突 - {game['name']}", f"Sync Conflict - {game['name']}"), "760x460")
        d.grid_columnconfigure((0, 1), weight=1)
        d.grid_rowconfigure(2, weight=1)

        def _close():
            self._open_conflict_dialogs.discard(game_key)
            d.destroy()

        d.protocol("WM_DELETE_WINDOW", lambda: self._defer_sync_conflict(game, d))

        ctk.CTkLabel(
            d,
            text=self.bi(f"「{game['name']}」检测到同步冲突", f"Sync conflict detected for {game['name']}"),
            font=font(16, "bold")
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(18, 6), sticky="w")
        reason = conflict.get("reason", self.bi("本地与云端内容不一致", "Local and cloud content differ"))
        ctk.CTkLabel(
            d,
            text=self.bi(
                f"{reason}。\n请选择要保留的版本。选择“仅下载”前会自动备份本地存档。",
                f"{reason}.\nChoose which version to keep. A local backup will be created automatically before Download Only runs.",
            ),
            justify="left", wraplength=700, text_color=C_SUBTLE_TEXT,
            font=font(12)
        ).grid(row=1, column=0, columnspan=2, padx=20, pady=(0, 10), sticky="w")

        local_card = ctk.CTkTextbox(d, font=font(12), wrap="word")
        local_card.grid(row=2, column=0, padx=(20, 10), pady=8, sticky="nsew")
        local_card.insert("1.0", self._format_sync_side_text(self.bi("本地存档", "Local Save"), conflict.get("local", {})))
        local_card.configure(state="disabled")

        remote_card = ctk.CTkTextbox(d, font=font(12), wrap="word")
        remote_card.grid(row=2, column=1, padx=(10, 20), pady=8, sticky="nsew")
        remote_card.insert("1.0", self._format_sync_side_text(self.bi("云端存档", "Cloud Save"), conflict.get("remote", {})))
        remote_card.configure(state="disabled")

        btns = ctk.CTkFrame(d, fg_color="transparent")
        btns.grid(row=3, column=0, columnspan=2, padx=20, pady=(8, 18), sticky="e")
        ctk.CTkButton(
            btns, text=self.bi("稍后处理", "Later"), width=100,
            fg_color=BTN_SUCCESS,
            hover_color=BTN_SUCCESS_H,
            command=lambda: self._defer_sync_conflict(game, d),
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btns, text=self.bi("仅下载云端", "Download Only"), width=120, fg_color=BTN_WARN,
            hover_color=BTN_WARN_H,
            command=lambda: self._resolve_sync_conflict(game, "download", d)
        ).pack(side="left", padx=6)
        ctk.CTkButton(
            btns, text=self.bi("仅上传本地", "Upload Only"), width=120, fg_color=BTN_PRIMARY,
            hover_color=BTN_PRIMARY_H,
            command=lambda: self._resolve_sync_conflict(game, "upload", d)
        ).pack(side="left", padx=6)

    def _defer_sync_conflict(self, game: dict, dialog, seconds: int = 300):
        game_key = get_game_sync_key(game)
        snooze_game_sync_conflict(self.cfg, game, seconds=seconds)
        self._open_conflict_dialogs.discard(game_key)
        dialog.destroy()

    def _resolve_sync_conflict(self, game: dict, mode: str, dialog):
        if self._io_busy:
            self._notify_io_busy()
            return
        game_key = get_game_sync_key(game)
        sync_root = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=True)
        if not sync_root:
            self._show_warning(
                self.bi("处理中断", "Sync Backend Missing"),
                self.bi("请先配置同步文件夹或启用 WebDAV 远程同步。", "Please configure a sync folder or enable WebDAV remote sync first."),
            )
            return

        self._set_io_busy(True)
        self._resolving_sync_conflicts.add(game_key)
        self._open_conflict_dialogs.discard(game_key)
        dialog.destroy()

        def _done_success(result: str):
            self._set_io_busy(False)
            self._resolving_sync_conflicts.discard(game_key)
            self._on_backups_changed(dict(game))
            self._show_info(
                self.bi("冲突已处理", "Conflict Resolved"),
                self.bi(f"「{game['name']}」{result}", f"{game['name']}: {result}"),
            )
            self._refresh_proc_status()

        def _done_error(err: str):
            self._set_io_busy(False)
            self._resolving_sync_conflicts.discard(game_key)
            self._show_error(
                self.bi("处理失败", "Resolution Failed"),
                self.bi(
                    f"「{game['name']}」处理冲突失败：\n{err}",
                    f"Failed to resolve conflict for {game['name']}:\n{err}",
                ),
            )

        def _worker():
            try:
                result = sync_game_save(game, sync_root, mode, cfg=self.cfg)
                self.after(0, lambda r=result: _done_success(r))
            except Exception as e:
                self.after(0, lambda err=f"{type(e).__name__}: {e}": _done_error(err))

        threading.Thread(target=_worker, daemon=True).start()

    def _show_process_diagnose(self):
        """弹出进程检测诊断窗口"""
        monitor = GameProcessMonitor(self.cfg)
        info = monitor.diagnose()
        d = self._create_popup(self.bi("进程检测诊断", "Process Diagnostics"), "640x520")
        tb = ctk.CTkTextbox(d, font=font(12), wrap="word")
        tb.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        tb.insert("1.0", info)
        tb.configure(state="disabled")
        ctk.CTkButton(d, text=self.bi("关闭", "Close"), width=100, height=36,
                      font=font(13, "bold"), corner_radius=8,
                      fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                      command=d.destroy).pack(pady=(0, 16))

    def _show_sync_log(self):
        """弹出同步监控日志窗口"""
        d = self._create_popup(self.bi("同步监控日志", "Sync Monitor Log"), "640x520")
        tb = ctk.CTkTextbox(d, font=font(12), wrap="word")
        tb.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        lines = []
        if self._game_monitor and self._game_monitor.sync_log:
            lines = list(self._game_monitor.sync_log)
        else:
            lines = [self.bi("暂无同步活动记录。", "No sync activity yet.")]
            if not self.cfg.get("sync_enabled"):
                lines.append("")
                lines.append(self.bi("⚠ 同步功能未启用，请在设置中开启。", "⚠ Sync is disabled. Enable it in Settings."))
            elif self.cfg.get("sync_mode") != "smart":
                lines.append("")
                lines.append(self.bi(f"⚠ 当前同步模式为「{self.cfg.get('sync_mode', '?')}」，",
                                     f"⚠ Current sync mode is \"{self.cfg.get('sync_mode', '?')}\","))
                lines.append(self.bi("   智能云存档需要设为「smart」模式。", "   Smart Cloud Save requires the \"smart\" mode."))
            elif not has_sync_backend(self.cfg.get("sync_folder", ""), self.cfg):
                lines.append("")
                issue = get_sync_backend_issue(self.cfg.get("sync_folder", ""), self.cfg)
                if issue == "webdav_component_missing":
                    lines.append(self.bi("⚠ WebDAV 已启用，但当前运行环境缺少 webdavclient3 组件。", "⚠ WebDAV is enabled, but webdavclient3 is unavailable in the current runtime."))
                elif issue == "webdav_url_missing":
                    lines.append(self.bi("⚠ WebDAV 已启用，但服务器地址为空。", "⚠ WebDAV is enabled, but the server URL is empty."))
                else:
                    lines.append(self.bi("⚠ 同步目录与 WebDAV 均未配置，请在设置中启用其中一种。", "⚠ Neither a sync folder nor WebDAV is configured. Enable one in Settings."))
            elif not self._game_monitor:
                lines.append("")
                lines.append(self.bi("⚠ 同步监控未启动，请尝试重启应用。", "⚠ Sync monitoring is not running. Try restarting the app."))
            else:
                lines.append("")
                lines.append(self.bi("监控已启动，等待游戏启动/关闭事件...", "Monitoring is running and waiting for game start/stop events..."))
        pending_conflicts = sum(
            1 for g in self.cfg.get("games", [])
            if get_game_sync_state(self.cfg, g).get("pending_conflict")
            and not is_game_sync_conflict_snoozed(self.cfg, g)
        )
        retry_count = len(get_sync_retry_queue(self.cfg))
        if pending_conflicts or retry_count:
            lines.append("")
            lines.append(self.bi(f"待处理冲突：{pending_conflicts} 个", f"Pending conflicts: {pending_conflicts}"))
            lines.append(self.bi(f"待重试任务：{retry_count} 个", f"Queued retries: {retry_count}"))
        tb.insert("1.0", "\n".join(lines))
        tb.configure(state="disabled")
        btn_frame = ctk.CTkFrame(d, fg_color="transparent")
        btn_frame.pack(pady=(0, 16))
        def _refresh_log():
            tb.configure(state="normal")
            tb.delete("1.0", "end")
            if self._game_monitor and self._game_monitor.sync_log:
                tb.insert("1.0", "\n".join(self._game_monitor.sync_log))
            else:
                tb.insert("1.0", self.bi("暂无同步活动记录。", "No sync activity yet."))
            tb.configure(state="disabled")
        ctk.CTkButton(btn_frame, text=self.bi("刷新", "Refresh"), width=100, height=36,
                      font=font(13, "bold"), corner_radius=8,
                      fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
                      command=_refresh_log).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=self.bi("关闭", "Close"), width=100, height=36,
                      font=font(13, "bold"), corner_radius=8,
                      fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
                      command=d.destroy).pack(side="left", padx=4)

    def _set_about_update_state(self, text: Optional[str] = None, busy: Optional[bool] = None):
        label = getattr(self, "_about_update_status_label", None)
        if label is not None and label.winfo_exists() and text is not None:
            label.configure(text=text)
        button = getattr(self, "_about_update_btn", None)
        if button is not None and button.winfo_exists() and busy is not None:
            button.configure(state="disabled" if busy else "normal")

    def _apply_sidebar_version_state(self):
        label = getattr(self, "_sidebar_version_label", None)
        if label is not None and label.winfo_exists():
            label.configure(text=self._sidebar_version_text, text_color=self._sidebar_version_color)

    def _set_sidebar_update_hint(self, text: str, color=None):
        self._sidebar_version_text = text
        if color is not None:
            self._sidebar_version_color = color
        self._apply_sidebar_version_state()

    def _check_for_updates(self):
        self._set_about_update_state(
            text=self.bi("更新状态：正在检查...", "Update status: checking..."),
            busy=True,
        )
        threading.Thread(target=self._check_for_updates_worker, kwargs={"silent": False}, daemon=True).start()

    def _check_for_updates_silent(self):
        self._set_sidebar_update_hint(
            self.bi(f"v{VERSION} · 检查更新中...", f"v{VERSION} · Checking updates..."),
            ("#cbd5e1", "#71717a"),
        )
        threading.Thread(target=self._check_for_updates_worker, kwargs={"silent": True}, daemon=True).start()

    def _check_for_updates_worker(self, silent: bool = False):
        try:
            manifest = fetch_update_manifest()
        except Exception as e:
            err_text = f"{type(e).__name__}: {e}"
            def _on_error():
                self._set_about_update_state(
                    text=self.bi("更新状态：检查失败", "Update status: failed to check"),
                    busy=False,
                )
                self._set_sidebar_update_hint(
                    self.bi(f"v{VERSION} · 更新检查失败", f"v{VERSION} · Update check failed"),
                    ("#f59e0b", "#fbbf24"),
                )
                if not silent:
                    self._show_error(
                        self.bi("检查更新失败", "Update Check Failed"),
                        self.bi(f"无法读取远程更新信息：\n{err_text}",
                                f"Could not read remote update info:\n{err_text}"),
                        parent=self._about_dialog,
                    )
            self.after(0, _on_error)
            return

        if not is_remote_version_newer(manifest["version"], VERSION):
            def _on_latest():
                self._update_manifest_cache = manifest
                self._set_about_update_state(
                    text=self.bi(f"更新状态：已是最新版本 v{VERSION}",
                                 f"Update status: already on the latest version v{VERSION}"),
                    busy=False,
                )
                self._set_sidebar_update_hint(
                    f"v{VERSION}",
                    ("#cbd5e1", "#3f3f46"),
                )
                if not silent:
                    self._show_info(
                        self.bi("检查更新", "Check for Updates"),
                        self.bi(f"当前已经是最新版本。\n\n当前版本：v{VERSION}",
                                f"You're already on the latest version.\n\nCurrent version: v{VERSION}"),
                        parent=self._about_dialog,
                    )
            self.after(0, _on_latest)
            return

        self.after(0, lambda m=manifest: self._handle_update_available(m, silent))

    def _handle_update_available(self, manifest: dict, silent: bool):
        self._update_manifest_cache = manifest
        self._set_about_update_state(
            text=self.bi(f"更新状态：发现新版本 v{manifest['version']}",
                         f"Update status: new version found v{manifest['version']}"),
            busy=False,
        )
        self._set_sidebar_update_hint(
            self.bi(f"v{VERSION} · 有更新 v{manifest['version']}",
                    f"v{VERSION} · Update v{manifest['version']}"),
            ("#10b981", "#4ade80"),
        )
        if not silent:
            self._prompt_update_download(manifest)

    def _prompt_update_download(self, manifest: dict):
        self._set_about_update_state(
            text=self.bi(f"更新状态：发现新版本 v{manifest['version']}",
                         f"Update status: new version found v{manifest['version']}"),
            busy=False,
        )
        action = self._ask_update_action(manifest, parent=self._about_dialog)
        if action == "download":
            self._download_update(manifest)
        elif action == "browser":
            self._open_update_in_browser(manifest)

    def _download_update(self, manifest: dict):
        self._set_about_update_state(
            text=self.bi(f"更新状态：正在下载 v{manifest['version']}...", f"Update status: downloading v{manifest['version']}..."),
            busy=True,
        )
        threading.Thread(target=self._download_update_worker, args=(manifest,), daemon=True).start()

    def _open_update_in_browser(self, manifest: dict):
        url = str(manifest.get("url", "") or "").strip()
        if not url:
            self._show_error(
                self.bi("打开浏览器失败", "Failed to Open Browser"),
                self.bi("更新下载链接为空。", "The update download link is empty."),
                parent=self._about_dialog,
            )
            return
        try:
            ok = webbrowser.open(url)
            if not ok:
                raise RuntimeError(self.bi("系统未返回成功状态", "The system did not report success"))
            self._set_about_update_state(
                text=self.bi("更新状态：已跳转浏览器下载", "Update status: opened in browser"),
                busy=False,
            )
            self._show_info(
                self.bi("浏览器下载", "Browser Download"),
                self.bi(f"已在浏览器中打开下载链接：\n{url}", f"The download link has been opened in your browser:\n{url}"),
                parent=self._about_dialog,
            )
        except Exception as e:
            self._show_error(
                self.bi("打开浏览器失败", "Failed to Open Browser"),
                self.bi(
                    f"无法在浏览器中打开下载链接：\n{type(e).__name__}: {e}",
                    f"Could not open the download link in your browser:\n{type(e).__name__}: {e}",
                ),
                parent=self._about_dialog,
            )

    def _download_update_worker(self, manifest: dict):
        try:
            target = download_update_package(manifest, get_update_download_dir())
        except Exception as e:
            err_text = f"{type(e).__name__}: {e}"
            self.after(0, lambda: (
                self._set_about_update_state(
                    text=self.bi("更新状态：下载失败", "Update status: download failed"),
                    busy=False,
                ),
                self._show_error(
                    self.bi("下载更新失败", "Update Download Failed"),
                    self.bi(f"下载更新时出错：\n{err_text}",
                            f"An error occurred while downloading the update:\n{err_text}"),
                    parent=self._about_dialog,
                )
            ))
            return
        self.after(0, lambda t=target, m=manifest: self._handle_update_downloaded(m, t))

    def _handle_update_downloaded(self, manifest: dict, target: Path):
        self._set_about_update_state(
            text=self.bi(f"更新状态：已下载 v{manifest['version']}", f"Update status: downloaded v{manifest['version']}"),
            busy=False,
        )
        if self._ask_yes_no(
                self.bi("下载完成", "Download Complete"),
                self.bi(
                    f"新版本已下载完成：\n{target}\n\n是否立即关闭当前程序并启动新版本？",
                    f"The new version has been downloaded:\n{target}\n\nClose the current app and launch the new version now?",
                ),
                parent=self._about_dialog):
            self._launch_downloaded_update(target)
        else:
            self._show_info(
                self.bi("下载完成", "Download Complete"),
                self.bi(f"更新文件已保存到：\n{target}",
                        f"The update package has been saved to:\n{target}"),
                parent=self._about_dialog,
            )

    def _launch_downloaded_update(self, target: Path):
        try:
            if sys.platform == "win32":
                current_exe = _current_executable_path()
                backup_old = True
                if getattr(sys, "frozen", False) and current_exe.resolve() != target.resolve():
                    backup_old = self._ask_yes_no(
                        self.bi("备份旧版本", "Back Up Current Version"),
                        self.bi(
                            f"是否在升级前备份当前版本？\n\n将保存为：\n{current_exe.with_suffix(current_exe.suffix + '.old')}",
                            f"Back up the current version before updating?\n\nIt will be saved as:\n{current_exe.with_suffix(current_exe.suffix + '.old')}",
                        ),
                        parent=self._about_dialog,
                    )
                    command = _build_windows_replace_and_launch_command(
                        target, current_exe, current_pid=os.getpid(), backup_old=backup_old
                    )
                else:
                    escaped_target = str(target).replace("'", "''")
                    command = f"Start-Sleep -Seconds 1; Start-Process -FilePath '{escaped_target}'"
                subprocess.Popen(
                    ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", command],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                subprocess.Popen([str(target)])
        except Exception as e:
            self._show_error(
                self.bi("启动更新失败", "Failed to Launch Update"),
                self.bi(f"无法启动下载的新版本：\n{type(e).__name__}: {e}",
                        f"Could not launch the downloaded update:\n{type(e).__name__}: {e}"),
                parent=self._about_dialog,
            )
            return

        if hasattr(self, "_tray") and self._tray:
            self._tray.stop()
            self._tray = None
        self._stop_auto_backup()
        self._stop_watchers()
        self._stop_game_monitor()
        self._stop_sync()
        self.after(0, self.destroy)

    def _show_about_dialog(self, *a):
        d = self._create_popup(self.bi("关于 Steam 存档管家", "About Steam Save Manager"), "560x360")
        self._about_dialog = d
        d.grid_columnconfigure(0, weight=1)
        d.grid_rowconfigure(1, weight=1)
        def _on_close():
            self._about_update_status_label = None
            self._about_update_btn = None
            self._about_dialog = None
            d.destroy()
        d.protocol("WM_DELETE_WINDOW", _on_close)

        hdr = ctk.CTkFrame(d, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=22, pady=(18, 10), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            hdr,
            text=self.bi("Steam 存档管家", "Steam Save Manager"),
            font=font(20, "bold"),
            text_color=C_BODY_TEXT,
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            hdr,
            text=f"v{VERSION}",
            font=font(12),
            text_color=C_SUBTLE_TEXT,
        ).grid(row=0, column=1, sticky="e")

        body = ctk.CTkTextbox(d, font=font(12), wrap="word")
        body.grid(row=1, column=0, padx=18, pady=(0, 14), sticky="nsew")
        body.insert(
            "1.0",
            self.bi(
                f"一个用于管理 Steam 游戏存档的桌面工具。\n\n"
                f"当前版本：v{VERSION}\n"
                f"配置目录：{CONFIG_DIR}\n"
                f"备份目录：{BACKUP_ROOT}\n"
                f"by：Kio\n\n"
                f"主要功能：\n"
                f"• 自动扫描常见存档目录\n"
                f"• 手动备份、恢复和导入存档\n"
                f"• 定时备份与文件变动监控\n"
                f"• 本地与云盘同步、冲突处理和重试\n"
                f"• 中英文界面与系统托盘运行",
                f"A desktop utility for managing Steam game saves.\n\n"
                f"Current version: v{VERSION}\n"
                f"Config folder: {CONFIG_DIR}\n"
                f"Backup folder: {BACKUP_ROOT}\n"
                f"by：Kio\n\n"
                f"Highlights:\n"
                f"• Automatically scans common save locations\n"
                f"• Manual backup, restore, and save import\n"
                f"• Scheduled backup and file change monitoring\n"
                f"• Local/cloud sync with conflict handling and retry\n"
                f"• Bilingual UI and system tray support",
            ),
        )
        body.configure(state="disabled")
        self._about_update_status_label = ctk.CTkLabel(
            d,
            text=self.bi("更新状态：未检查", "Update status: not checked"),
            font=font(11),
            text_color=C_SUBTLE_TEXT,
        )
        self._about_update_status_label.grid(row=2, column=0, padx=22, pady=(0, 8), sticky="w")

        btns = ctk.CTkFrame(d, fg_color="transparent")
        btns.grid(row=3, column=0, padx=22, pady=(0, 18), sticky="e")
        self._about_update_btn = ctk.CTkButton(
            btns, text=self.bi("检查更新", "Check for Updates"),
            width=150, height=36, font=font(13, "bold"),
            corner_radius=8, fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
            command=self._check_for_updates
        )
        self._about_update_btn.pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            btns, text=self.bi("关闭", "Close"),
            width=100, height=36, font=font(13, "bold"),
            corner_radius=8, fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
            command=_on_close
        ).pack(side="left")

    def _toggle_game_auto_backup(self):
        idx = self._detail_idx
        en = self._detail_auto_var.get() == "on"
        self.cfg["games"][idx]["auto_backup"] = en
        save_config(self.cfg)
        self._restart_watchers_runtime()

    def _toggle_game_watch_backup(self):
        idx = self._detail_idx
        self.cfg["games"][idx]["watch_backup"] = self._detail_watch_var.get() == "on"
        save_config(self.cfg)
        self._restart_watchers_runtime()

    def _toggle_game_exit_backup(self):
        idx = self._detail_idx
        self.cfg["games"][idx]["backup_on_exit"] = self._detail_exit_backup_var.get() == "on"
        save_config(self.cfg)
        self._restart_watchers_runtime()

    def _toggle_game_sync_enabled(self):
        idx = self._detail_idx
        en = self._detail_sync_var.get() == "on"
        self.cfg["games"][idx]["sync_enabled"] = en
        clear_game_sync_state(self.cfg, self.cfg["games"][idx])
        save_config(self.cfg)
        self._restart_sync_runtime()
        self._refresh_proc_status()

    def _toggle_game_favorite(self, idx: int):
        if idx < 0 or idx >= len(self.cfg.get("games", [])):
            return
        game = self.cfg["games"][idx]
        game["favorite"] = not is_game_favorite(game)
        save_config(self.cfg)
        self._refresh_games_list()
        if getattr(self, "_current_frame", "") == "game_detail" and getattr(self, "_detail_idx", None) == idx:
            self._detail_favorite_var.set("on" if is_game_favorite(game) else "off")

    def _toggle_game_favorite_from_detail(self):
        idx = self._detail_idx
        if idx < 0 or idx >= len(self.cfg.get("games", [])):
            return
        game = self.cfg["games"][idx]
        game["favorite"] = self._detail_favorite_var.get() == "on"
        save_config(self.cfg)
        if getattr(self, "_games_scroll", None) is not None:
            self._refresh_games_list()

    def _start_detail_refresh(self):
        """启动详情页自动刷新定时器（每 15 秒检查备份变化）"""
        self._stop_detail_refresh()
        def _tick():
            if hasattr(self, '_detail_idx'):
                try:
                    idx = self._detail_idx
                    g = self.cfg["games"][idx]
                    backups = get_backups(g)
                    current_text = self._detail_stats["backups"].cget("text")
                    if str(len(backups)) != current_text:
                        self._show_game_detail(idx)
                        return
                    # 同时刷新进程状态
                    self._refresh_proc_status()
                except Exception:
                    pass
            self._detail_refresh_job = self.after(15000, _tick)
        self._detail_refresh_job = self.after(15000, _tick)

    def _stop_detail_refresh(self):
        """停止详情页自动刷新定时器"""
        if self._detail_refresh_job:
            self.after_cancel(self._detail_refresh_job)
            self._detail_refresh_job = None

    def _backup_from_detail(self, idx):
        if self._io_busy:
            self._notify_io_busy()
            return
        g = dict(self.cfg["games"][idx])
        note = self._input_dialog(
            title=self.bi("备注", "Note"),
            text=self.bi(f"为「{g['name']}」备份添加备注（可选）：", f"Add an optional note for the backup of {g['name']}:"))
        detail_idx = self._detail_idx
        self._set_io_busy(True)
        def _worker():
            try:
                r = create_backup(g, note)
                self.after(0, lambda: self._on_backup_detail_done(r, detail_idx))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_backup_failed(err, detail_idx))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_backup_detail_done(self, result, idx):
        self._set_io_busy(False)
        if result:
            if idx < len(self.cfg.get("games", [])):
                self._invalidate_game_backup_count_cache(self.cfg["games"][idx])
            self._show_info(self.bi("成功", "Success"), self.bi(f"备份完成！\n{result}", f"Backup complete!\n{result}"))
            self._show_game_detail(idx)
        else:
            self._show_error(self.bi("失败", "Failed"), self.bi("存档路径不存在", "The save path does not exist"))

    def _open_save_folder(self, idx):
        g = self.cfg["games"][idx]
        save_paths = get_game_save_paths(g, existing_only=False)
        p = save_paths[0] if save_paths else g.get("save_path", "")
        if p and os.path.isdir(p):
            if sys.platform == "win32":
                os.startfile(p)
            else:
                import subprocess
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, p])
        else:
            self._show_warning(self.bi("提示", "Notice"), self.bi("存档路径不存在", "The save path does not exist"))

    def _delete_game_from_detail(self, idx):
        n = self.cfg["games"][idx]["name"]
        if self._ask_yes_no(self.bi("确认", "Confirm"), self.bi(f"删除「{n}」？（备份保留）", f"Delete {n}? Existing backups will be kept.")):
            self.cfg["games"].pop(idx)
            save_config(self.cfg)
            self._show_frame("games")

    def _restore_from_detail(self, b, game):
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认还原", "Confirm Restore"),
                self.bi(f"还原此存档？\n时间：{self._fmt_ts(b['timestamp'])}\n当前存档会自动安全备份",
                        f"Restore this save?\nTime: {self._fmt_ts(b['timestamp'])}\nYour current save will be backed up automatically first")):
            backup_path = b["path"]
            game_copy = dict(game)
            detail_idx = self._detail_idx
            self._set_io_busy(True)
            def _worker():
                try:
                    targets = b.get("save_specs", []) or get_game_save_specs(game_copy, existing_only=False)
                    restore_backup(backup_path, targets if targets else game_copy.get("save_path", ""))
                    self.after(0, lambda: self._on_restore_detail_done(detail_idx))
                except Exception as e:
                    self.after(0, lambda err=str(e): self._on_restore_failed(err))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_restore_detail_done(self, idx):
        self._set_io_busy(False)
        if idx < len(self.cfg.get("games", [])):
            self._invalidate_game_backup_count_cache(self.cfg["games"][idx])
        self._show_info(self.bi("成功", "Success"), self.bi("已还原！", "Restored successfully!"))
        self._show_game_detail(idx)

    def _del_bk_from_detail(self, b):
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认", "Confirm"), self.bi("删除此备份？不可撤销。", "Delete this backup? This cannot be undone.")):
            backup_path = b["path"]
            game_ref = self.cfg["games"][self._detail_idx] if self._detail_idx < len(self.cfg.get("games", [])) else {}
            linked_archive = self._resolve_linked_sync_archive_deletion(b, game_ref)
            detail_idx = self._detail_idx
            self._set_io_busy(True)
            def _worker():
                result = self._delete_backup_worker(backup_path, game_ref, linked_archive)
                self.after(0, lambda r=result: self._on_del_bk_detail_done(detail_idx, r))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_del_bk_detail_done(self, idx, result=None):
        self._set_io_busy(False)
        if idx < len(self.cfg.get("games", [])):
            self._invalidate_game_backup_count_cache(self.cfg["games"][idx])
        self._show_delete_backup_followup(result)
        self._show_game_detail(idx)

    def _manual_backup(self, idx):
        if self._io_busy:
            self._notify_io_busy()
            return
        g = dict(self.cfg["games"][idx])
        note = self._input_dialog(
            title=self.bi("备注", "Note"),
            text=self.bi(f"为「{g['name']}」备份添加备注（可选）：", f"Add an optional note for the backup of {g['name']}:"))
        self._set_io_busy(True)
        def _worker():
            try:
                r = create_backup(g, note)
                self.after(0, lambda: self._on_backup_done(r))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_backup_failed(err))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_backup_done(self, result):
        self._set_io_busy(False)
        if result:
            self._on_backups_changed()
            self._show_info(self.bi("成功", "Success"), self.bi(f"备份完成！\n{result}", f"Backup complete!\n{result}"))
        else:
            self._show_error(self.bi("失败", "Failed"), self.bi("存档路径不存在", "The save path does not exist"))

    def _on_backup_failed(self, err, detail_idx=None):
        self._set_io_busy(False)
        if detail_idx is not None and detail_idx < len(self.cfg.get("games", [])):
            self._invalidate_game_backup_count_cache(self.cfg["games"][detail_idx])
        self._show_error(self.bi("备份失败", "Backup Failed"), err)

    def _backup_all(self):
        if self._io_busy:
            self._notify_io_busy()
            return
        games = self.cfg.get("games", [])
        if not games: self._show_info(self.bi("提示", "Notice"), self.bi("请先添加游戏", "Please add a game first")); return
        game_copies = [dict(g) for g in games]
        default_note = self.bi("一键备份", "Back Up All")
        original_title = self.t("product_title")
        progress_tpl = self.bi("备份中 {cur}/{total}...", "Backing up {cur}/{total}...")
        done_title = self.bi("完成", "Done")
        self._set_io_busy(True)
        def _worker():
            ok = 0
            total = len(game_copies)
            failed: list[str] = []
            try:
                for i, g in enumerate(game_copies):
                    try:
                        r = create_backup(g, default_note)
                        if r:
                            ok += 1
                        else:
                            failed.append(f"{g.get('name', self.bi('未命名游戏', 'Unnamed game'))}: " + self.bi("存档路径不存在", "Save path does not exist"))
                    except Exception as e:
                        failed.append(f"{g.get('name', self.bi('未命名游戏', 'Unnamed game'))}: {type(e).__name__}: {e}")
                    label = progress_tpl.format(cur=i + 1, total=total)
                    self.after(0, lambda t=label: self.title(t))
            finally:
                self.after(0, lambda: self._on_backup_all_done(total, ok, original_title, done_title, failed))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_backup_all_done(self, total, ok, original_title, done_title, failed=None):
        self._set_io_busy(False)
        self.title(original_title)
        self._on_backups_changed()
        failed = failed or []
        message = self.bi(f"成功 {ok}/{total}", f"Succeeded: {ok}/{total}")
        if failed:
            details = "\n".join(failed[:8])
            if len(failed) > 8:
                details += "\n" + self.bi(f"……另有 {len(failed) - 8} 个失败项", f"...and {len(failed) - 8} more failures")
            message += "\n\n" + self.bi("失败项目：", "Failed items:") + "\n" + details
        self._show_info(done_title, message)

    # ─── 备份记录 ───
    def _build_backup_frame(self):
        frame = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["backup"] = frame
        frame.grid_columnconfigure(0, weight=1)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=32, pady=(28, 6), sticky="ew")
        hdr.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(hdr, text=self.t("backup_title"), font=font(20, "bold")).grid(
            row=0, column=0, sticky="w")
        self._bk_var = ctk.StringVar(value=self.t("backup_all_games"))
        self._bk_filter = ctk.CTkOptionMenu(
            hdr, variable=self._bk_var, values=[self.t("backup_all_games")],
            command=lambda _: self._on_backup_filter_changed(), width=200,
            font=font(12))
        self._bk_filter.grid(row=0, column=1, sticky="e")

        self._bk_scroll = ctk.CTkScrollableFrame(
            frame, height=440, corner_radius=12, fg_color=C_CARD_BG)
        self._bk_scroll.grid(row=1, column=0, padx=30, pady=10, sticky="nsew")
        frame.grid_rowconfigure(1, weight=1)
        self._backup_rows = {}
        self._backup_empty_label = ctk.CTkLabel(
            self._bk_scroll,
            text="",
            text_color=C_SUBTLE_TEXT,
            font=font(13),
        )
        pager = ctk.CTkFrame(frame, fg_color="transparent")
        pager.grid(row=2, column=0, padx=30, pady=(0, 14), sticky="ew")
        pager.grid_columnconfigure(0, weight=1)
        self._backup_page_label = ctk.CTkLabel(
            pager, text="", text_color=C_SUBTLE_TEXT, font=font(12)
        )
        self._backup_page_label.grid(row=0, column=0, sticky="w")
        self._backup_prev_btn = ctk.CTkButton(
            pager, text=self.bi("上一页", "Prev"), width=76, height=30,
            font=font(12), corner_radius=6,
            fg_color=BTN_SECONDARY, hover_color=BTN_SECONDARY_H,
            command=lambda: self._on_backup_page_delta(-1))
        self._backup_prev_btn.grid(row=0, column=1, padx=(8, 4), sticky="e")
        self._backup_next_btn = ctk.CTkButton(
            pager, text=self.bi("下一页", "Next"), width=76, height=30,
            font=font(12), corner_radius=6,
            fg_color=BTN_SECONDARY, hover_color=BTN_SECONDARY_H,
            command=lambda: self._on_backup_page_delta(1))
        self._backup_next_btn.grid(row=0, column=2, padx=(4, 0), sticky="e")

    def _refresh_backup_list(self):
        games = self.cfg.get("games", [])
        all_games_label = self.t("backup_all_games")
        self._bk_filter.configure(values=[all_games_label] + [g["name"] for g in games])

        sel = self._bk_var.get()
        targets = games if sel == all_games_label else [g for g in games if g["name"] == sel]
        total_backups = sum(self._get_game_backup_count_cached(g) for g in targets)
        self._backup_page, start, end = self._page_range(
            self._backup_page, total_backups, self._backup_page_size
        )
        total_pages = max(1, (total_backups + self._backup_page_size - 1) // self._backup_page_size)
        all_b = []
        for item in self._backup_rows.values():
            item["card"].pack_forget()
        if total_backups:
            for g in targets:
                for b in self._get_game_backups_cached(g):
                    b["game"] = g["name"]
                    b["_game_ref"] = g
                    all_b.append(b)
            all_b.sort(key=lambda x: x["timestamp"], reverse=True)
        page_backups = all_b[start:end]

        if self._backup_page_label:
            if total_backups:
                self._backup_page_label.configure(
                    text=self.bi(
                        f"第 {self._backup_page + 1}/{total_pages} 页 · 显示 {start + 1}-{end} / {total_backups} 条备份",
                        f"Page {self._backup_page + 1}/{total_pages} · Showing {start + 1}-{end} of {total_backups} backups",
                    )
                )
            else:
                self._backup_page_label.configure(text=self.bi("0 条备份", "0 backups"))
        if self._backup_prev_btn:
            self._backup_prev_btn.configure(state="normal" if self._backup_page > 0 else "disabled")
        if self._backup_next_btn:
            self._backup_next_btn.configure(state="normal" if end < total_backups else "disabled")

        if not page_backups:
            self._backup_empty_label.configure(text=self.t("backup_empty"))
            self._backup_empty_label.pack(pady=40)
            stale_keys = list(self._backup_rows.keys())
            for key in stale_keys:
                item = self._backup_rows.pop(key, None)
                if item:
                    item["card"].destroy()
            return
        self._backup_empty_label.pack_forget()
        active_keys = set()
        for b in page_backups:
            game_ref = b.get("_game_ref", {})
            if isinstance(game_ref, dict):
                save_paths = get_game_save_paths(game_ref, existing_only=False)
                primary_path = save_paths[0] if save_paths else game_ref.get("save_path", "")
            else:
                save_paths = []
                primary_path = ""
            b["game"] = game_ref.get("name", b.get("game", "")) if isinstance(game_ref, dict) else b.get("game", "")
            b["save_path"] = primary_path
            b["save_paths"] = save_paths
            key = f"{b['game']}::{b['timestamp']}::{b['filename']}"
            active_keys.add(key)
            item = self._backup_rows.get(key)
            if item is None:
                card = ctk.CTkFrame(self._bk_scroll,
                                    fg_color=("#f1f5f9", "#252640"), corner_radius=12)
                card.grid_columnconfigure(1, weight=1)
                title = ctk.CTkLabel(card, text="", font=font(13, "bold"),
                                     text_color=C_BODY_TEXT, wraplength=400)
                title.grid(row=0, column=0, padx=14, pady=(10, 0), sticky="w")
                self._register_wrap(title, 0.56, 260)
                info_label = ctk.CTkLabel(card, text="", font=font(11),
                                          text_color=C_SUBTLE_TEXT, wraplength=400)
                info_label.grid(row=1, column=0, padx=14, pady=(0, 10), sticky="w")
                self._register_wrap(info_label, 0.56, 260)
                bb = ctk.CTkFrame(card, fg_color="transparent")
                bb.grid(row=0, column=1, rowspan=2, padx=14, pady=10, sticky="e")
                restore_btn = self._register_busy_widget(ctk.CTkButton(
                    bb, text=self.bi("还原", "Restore"), width=60, height=28, font=font(12),
                    corner_radius=6, fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H))
                restore_btn.pack(side="left", padx=2)
                delete_btn = self._register_busy_widget(ctk.CTkButton(
                    bb, text=self.bi("删除", "Delete"), width=60, height=28, font=font(12),
                    corner_radius=6, fg_color=BTN_DANGER, hover_color=BTN_DANGER_H))
                delete_btn.pack(side="left", padx=2)
                item = {
                    "card": card,
                    "title": title,
                    "info": info_label,
                    "restore_btn": restore_btn,
                    "delete_btn": delete_btn,
                }
                self._backup_rows[key] = item
            item["title"].configure(text=f"🎮 {b['game']}")
            info = f"{self._fmt_ts(b['timestamp'])}  ·  {fmt_size(b['size'])}"
            note_text = localize_backup_note(b.get("note", ""), cfg_language(self.cfg))
            if note_text:
                info += f"  ·  📝 {note_text}"
            item["info"].configure(text=info)
            item["restore_btn"].configure(command=lambda bp=b: self._restore(bp))
            item["delete_btn"].configure(command=lambda bp=b: self._del_bk(bp))
            item["card"].pack(fill="x", padx=4, pady=2)
        stale_keys = [key for key in self._backup_rows.keys() if key not in active_keys]
        for key in stale_keys:
            item = self._backup_rows.pop(key, None)
            if item:
                item["card"].destroy()

    def _restore(self, b):
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认还原", "Confirm Restore"),
                self.bi(f"还原「{b['game']}」的存档？\n时间：{self._fmt_ts(b['timestamp'])}\n当前存档会自动安全备份",
                        f"Restore the save for {b['game']}?\nTime: {self._fmt_ts(b['timestamp'])}\nYour current save will be backed up automatically first")):
            backup_path = b["path"]
            game_ref = b.get("_game_ref", {})
            current_specs = get_game_save_specs(game_ref, existing_only=False) if isinstance(game_ref, dict) else []
            targets = b.get("save_specs", []) or current_specs
            if not targets:
                targets = [_default_save_spec(path) for path in b.get("save_paths", [])]
            restore_target = targets if targets else b.get("save_path", "")
            self._set_io_busy(True)
            def _worker():
                try:
                    restore_backup(backup_path, restore_target)
                    self.after(0, lambda: self._on_restore_done())
                except Exception as e:
                    self.after(0, lambda err=str(e): self._on_restore_failed(err))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_restore_done(self):
        self._set_io_busy(False)
        self._on_backups_changed()
        self._show_info(self.bi("成功", "Success"), self.bi("已还原！", "Restored successfully!"))

    def _on_restore_failed(self, err):
        self._set_io_busy(False)
        self._show_error(self.bi("失败", "Failed"), err)

    def _del_bk(self, b):
        if self._io_busy: return
        if self._ask_yes_no(self.bi("确认", "Confirm"), self.bi("删除此备份？不可撤销。", "Delete this backup? This cannot be undone.")):
            backup_path = b["path"]
            game_ref = {
                "name": b.get("game", ""),
                "game_uid": b.get("game_uid", ""),
                "storage_key": b.get("storage_key", ""),
            }
            linked_archive = self._resolve_linked_sync_archive_deletion(b, game_ref)
            self._set_io_busy(True)
            def _worker():
                result = self._delete_backup_worker(backup_path, game_ref, linked_archive)
                self.after(0, lambda r=result: self._on_del_bk_done(r))
            threading.Thread(target=_worker, daemon=True).start()

    def _on_del_bk_done(self, result=None):
        self._set_io_busy(False)
        self._on_backups_changed()
        self._show_delete_backup_followup(result)

    # ─── 设置 ───
    def _build_settings_frame(self):
        frame = ctk.CTkFrame(self, fg_color=C_MAIN_BG)
        self._frames["settings"] = frame
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(frame, fg_color="transparent")
        hdr.grid(row=0, column=0, padx=32, pady=(28, 12), sticky="ew")
        hdr.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(hdr, text=self.t("settings_title"), font=font(20, "bold")).grid(
            row=0, column=0, sticky="w")
        settings_subtitle = ctk.CTkLabel(
            hdr, text=self.t("settings_subtitle"), font=font(12),
            text_color=C_SUBTLE_TEXT, anchor="w", justify="left")
        settings_subtitle.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self._register_wrap(settings_subtitle, 0.58, 300)
        ctk.CTkSegmentedButton(
            hdr,
            values=[
                self.t("section_general"),
                self.t("section_automation"),
                self.t("section_system"),
            ],
            font=font(12),
            command=lambda value: self._scroll_settings_to_section({
                self.t("section_general"): "general",
                self.t("section_automation"): "automation",
                self.t("section_system"): "system",
            }.get(value, "general")),
        ).grid(row=0, column=1, rowspan=2, padx=(18, 0), sticky="e")

        settings_host = ctk.CTkFrame(frame, fg_color=C_MAIN_BG, corner_radius=0)
        settings_host.grid(row=1, column=0, padx=0, pady=(0, 16), sticky="nsew")
        settings_host.grid_columnconfigure(0, weight=1)
        settings_host.grid_rowconfigure(0, weight=1)

        def _current_canvas_bg():
            return C_MAIN_BG[0] if ctk.get_appearance_mode().lower() == "light" else C_MAIN_BG[1]

        settings_canvas = tkinter.Canvas(
            settings_host, bg=_current_canvas_bg(), highlightthickness=0, bd=0)
        settings_canvas.grid(row=0, column=0, sticky="nsew")

        def _apply_settings_moveto(fraction):
            fraction = max(0.0, min(float(fraction), 1.0))
            settings_canvas.yview_moveto(fraction)
            settings_canvas.update_idletasks()

        # ── Scrollbar colors (theme-aware, update dynamically) ──
        _SB_COLORS = {
            "light": dict(bg=C_MAIN_BG[0], track="#e2e2e2", thumb="#c0c0c0",
                          hover="#a0a0a0", press="#888888"),
            "dark":  dict(bg=C_MAIN_BG[1], track="#333348", thumb="#555568",
                          hover="#6e6e82", press="#8888a0"),
        }

        def _sb_colors():
            mode = "light" if ctk.get_appearance_mode().lower() == "light" else "dark"
            return _SB_COLORS[mode]

        settings_scrollbar = tkinter.Canvas(
            settings_host, width=12,
            bg=_sb_colors()["bg"],
            highlightthickness=0, bd=0, cursor="hand2")
        settings_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 6), pady=6)

        _settings_track_id = settings_scrollbar.create_line(
            6, 8, 6, 42, fill=_sb_colors()["track"],
            width=4, capstyle=tkinter.ROUND, state=tkinter.HIDDEN)
        _settings_thumb_id = settings_scrollbar.create_line(
            6, 8, 6, 42, fill=_sb_colors()["thumb"],
            width=7, capstyle=tkinter.ROUND, state=tkinter.HIDDEN)

        _settings_thumb_span = (0.0, 1.0)
        _settings_dragging = False
        _settings_drag_offset = 0.0
        _settings_pending_moveto = None
        _settings_drag_job = None
        _settings_scrollbar_visible = False
        _settings_hide_job = None
        _settings_hovering = False

        def _settings_show_scrollbar():
            nonlocal _settings_scrollbar_visible, _settings_hide_job
            if _settings_hide_job is not None:
                settings_scrollbar.after_cancel(_settings_hide_job)
                _settings_hide_job = None
            if not _settings_scrollbar_visible:
                _settings_scrollbar_visible = True
                settings_scrollbar.itemconfigure(_settings_track_id, state=tkinter.NORMAL)
                settings_scrollbar.itemconfigure(_settings_thumb_id, state=tkinter.NORMAL)

        def _settings_schedule_hide():
            nonlocal _settings_hide_job
            if _settings_hide_job is not None:
                settings_scrollbar.after_cancel(_settings_hide_job)
            _settings_hide_job = settings_scrollbar.after(1200, _settings_hide_scrollbar)

        def _settings_hide_scrollbar():
            nonlocal _settings_scrollbar_visible, _settings_hide_job
            _settings_hide_job = None
            if _settings_dragging or _settings_hovering:
                return
            _settings_scrollbar_visible = False
            settings_scrollbar.itemconfigure(_settings_track_id, state=tkinter.HIDDEN)
            settings_scrollbar.itemconfigure(_settings_thumb_id, state=tkinter.HIDDEN)

        def _settings_apply_thumb_color():
            colors = _sb_colors()
            if _settings_dragging:
                fill = colors["press"]
            elif _settings_hovering:
                fill = colors["hover"]
            else:
                fill = colors["thumb"]
            settings_scrollbar.itemconfigure(_settings_thumb_id, fill=fill)

        def _draw_settings_thumb(first, last):
            nonlocal _settings_thumb_span
            start = max(0.0, min(float(first), 1.0))
            end = max(start, min(float(last), 1.0))
            _settings_thumb_span = (start, end)
            if end - start >= 0.999:
                settings_scrollbar.itemconfigure(_settings_track_id, state=tkinter.HIDDEN)
                settings_scrollbar.itemconfigure(_settings_thumb_id, state=tkinter.HIDDEN)
                _settings_scrollbar_visible = False
                return
            _settings_show_scrollbar()
            _preview_settings_thumb(start)

        def _preview_settings_thumb(start):
            start = max(0.0, min(float(start), 1.0))
            span = max(_settings_thumb_span[1] - _settings_thumb_span[0], 0.0)
            height = max(settings_scrollbar.winfo_height(), 1)
            track_top = 8
            track_bottom = max(track_top + 1, height - 8)
            track_height = max(track_bottom - track_top, 1)
            thumb_min = 28
            thumb_height = max(thumb_min, int(span * track_height))
            thumb_height = min(thumb_height, track_height)
            max_top = track_bottom - thumb_height
            thumb_top = track_top + int(start * max(track_height - thumb_height, 0))
            thumb_top = max(track_top, min(thumb_top, max_top))
            thumb_bottom = thumb_top + thumb_height
            settings_scrollbar.coords(_settings_track_id, 6, track_top, 6, track_bottom)
            settings_scrollbar.coords(_settings_thumb_id, 6, thumb_top, 6, thumb_bottom)

        def _settings_scrollbar_set(first, last):
            _draw_settings_thumb(first, last)
            _settings_schedule_hide()

        settings_canvas.configure(yscrollcommand=_settings_scrollbar_set)

        def _settings_thumb_fraction_from_y(y):
            _x1, y1, _x2, y2 = settings_scrollbar.coords(_settings_thumb_id)
            thumb_height = max(y2 - y1, 1)
            height = max(settings_scrollbar.winfo_height(), 1)
            track_top = 8
            track_height = max(height - 16, 1)
            movable = max(track_height - thumb_height, 1)
            thumb_top = y - _settings_drag_offset
            thumb_top = max(track_top, min(thumb_top, track_top + movable))
            return (thumb_top - track_top) / movable

        def _settings_scrollbar_press(event):
            nonlocal _settings_dragging, _settings_drag_offset, _settings_pending_moveto, _settings_drag_job
            _x1, y1, _x2, y2 = settings_scrollbar.coords(_settings_thumb_id)
            if y1 <= event.y <= y2:
                _settings_dragging = True
                _settings_drag_offset = event.y - y1
                _settings_pending_moveto = None
                if _settings_drag_job is not None:
                    settings_scrollbar.after_cancel(_settings_drag_job)
                    _settings_drag_job = None
                _settings_apply_thumb_color()
            else:
                _apply_settings_moveto(_settings_thumb_fraction_from_y(event.y))

        def _flush_settings_drag():
            nonlocal _settings_drag_job
            _settings_drag_job = None
            if _settings_dragging and _settings_pending_moveto is not None:
                _apply_settings_moveto(_settings_pending_moveto)
                _settings_drag_job = settings_scrollbar.after(16, _flush_settings_drag)

        def _settings_scrollbar_drag(event):
            nonlocal _settings_pending_moveto, _settings_drag_job
            if not _settings_dragging:
                return
            _settings_pending_moveto = _settings_thumb_fraction_from_y(event.y)
            _preview_settings_thumb(_settings_pending_moveto)
            if _settings_drag_job is None:
                _settings_drag_job = settings_scrollbar.after(16, _flush_settings_drag)

        def _settings_scrollbar_release(_event):
            nonlocal _settings_dragging, _settings_pending_moveto, _settings_drag_job
            _settings_dragging = False
            if _settings_drag_job is not None:
                settings_scrollbar.after_cancel(_settings_drag_job)
                _settings_drag_job = None
            if _settings_pending_moveto is not None:
                _apply_settings_moveto(_settings_pending_moveto)
            _settings_pending_moveto = None
            _settings_apply_thumb_color()
            _settings_schedule_hide()

        def _settings_scrollbar_enter(_event):
            nonlocal _settings_hovering
            _settings_hovering = True
            _settings_show_scrollbar()
            _settings_apply_thumb_color()

        def _settings_scrollbar_leave(_event):
            nonlocal _settings_hovering
            _settings_hovering = False
            if not _settings_dragging:
                _settings_apply_thumb_color()
                _settings_schedule_hide()

        settings_scrollbar.bind("<ButtonPress-1>", _settings_scrollbar_press)
        settings_scrollbar.bind("<B1-Motion>", _settings_scrollbar_drag)
        settings_scrollbar.bind("<ButtonRelease-1>", _settings_scrollbar_release)
        settings_scrollbar.bind("<Enter>", _settings_scrollbar_enter)
        settings_scrollbar.bind("<Leave>", _settings_scrollbar_leave)

        settings_scroll = ctk.CTkFrame(settings_canvas, fg_color=C_MAIN_BG, corner_radius=0)
        settings_scroll.grid_columnconfigure(0, weight=1)
        settings_window = settings_canvas.create_window((0, 0), window=settings_scroll, anchor="nw")
        self._settings_scroll = settings_canvas

        def _update_settings_scrollregion(_event=None):
            settings_canvas.configure(scrollregion=settings_canvas.bbox("all"))

        def _fit_settings_width(_event):
            settings_canvas.itemconfigure(settings_window, width=_event.width)

        def _settings_mousewheel(event):
            try:
                focused_popup = any(
                    window.winfo_exists() and window.focus_displayof() is not None
                    for window in list(self._popup_windows)
                )
                if focused_popup:
                    return
            except Exception:
                pass
            try:
                target_widget = self.winfo_containing(event.x_root, event.y_root)
            except Exception:
                target_widget = None
            inside_settings = False
            while target_widget is not None:
                if target_widget == settings_host:
                    inside_settings = True
                    break
                try:
                    target_widget = target_widget.master
                except Exception:
                    target_widget = None
            if not inside_settings:
                return
            if sys.platform.startswith("win"):
                delta = -int(event.delta / 120) if event.delta else 0
            else:
                delta = -1 if event.delta > 0 else 1
            if delta:
                settings_canvas.yview_scroll(delta * 3, "units")

        settings_scroll.bind("<Configure>", _update_settings_scrollregion)
        settings_canvas.bind("<Configure>", _fit_settings_width)
        settings_canvas.bind_all("<MouseWheel>", _settings_mousewheel, add="+")

        def _section(parent, title_key: str, subtitle_key: str):
            card = ctk.CTkFrame(parent, fg_color=C_CARD_BG, corner_radius=16)
            card.pack(fill="x", padx=30, pady=(0, 14))
            card.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(card, text=self.t(title_key), font=font(15, "bold"),
                         text_color=C_BODY_TEXT).grid(row=0, column=0, padx=22, pady=(18, 2), sticky="w")
            subtitle = ctk.CTkLabel(
                card, text=self.t(subtitle_key), font=font(11),
                text_color=C_SUBTLE_TEXT, anchor="w", justify="left")
            subtitle.grid(row=1, column=0, padx=22, pady=(0, 12), sticky="ew")
            self._register_wrap(subtitle, 0.72, 320)
            return card

        def _label(parent, row, text_key):
            ctk.CTkLabel(parent, text=self.t(text_key), font=font(13, "bold"),
                         text_color=C_BODY_TEXT).grid(row=row, column=0, padx=22, pady=(6, 4), sticky="w")

        def _row(parent):
            return ctk.CTkFrame(parent, fg_color=C_CARD_BG, corner_radius=8)

        general = _section(settings_scroll, "section_general", "section_general_sub")
        automation = _section(settings_scroll, "section_automation", "section_automation_sub")
        system = _section(settings_scroll, "section_system", "section_system_sub")
        self._settings_section_positions = {
            "general": general,
            "automation": automation,
            "system": system,
        }

        # General
        general.grid_columnconfigure(0, weight=1)
        _label(general, 2, "language")
        self._language_var = ctk.StringVar(value=self._language_display(self.lang))
        lang_row = _row(general)
        lang_row.grid(row=3, column=0, padx=22, sticky="ew")
        self._language_menu = ctk.CTkOptionMenu(
            lang_row, variable=self._language_var,
            values=[self._language_display(code) for code in SUPPORTED_LANGUAGES],
            width=180, font=font(12), command=self._on_language_change)
        self._language_menu.pack(side="left")
        ctk.CTkLabel(lang_row, text=self.t("language_hint"), font=font(11),
                     text_color=C_SUBTLE_TEXT).pack(side="left", padx=(10, 0))

        _label(general, 4, "theme")
        self._theme_var = ctk.StringVar(value=self._theme_display(self.cfg.get("theme", "light")))
        ctk.CTkSegmentedButton(
            general,
            values=[self.t("theme_light"), self.t("theme_dark")],
            variable=self._theme_var, font=font(12),
            command=self._on_theme
        ).grid(row=5, column=0, padx=22, sticky="w")

        _label(general, 6, "steam_path")
        sf = _row(general)
        sf.grid(row=7, column=0, padx=22, pady=(0, 10), sticky="ew")
        sf.grid_columnconfigure(0, weight=1)
        self._steam_e = ctk.CTkEntry(sf, font=font(12))
        self._steam_e.insert(0, self.cfg.get("steam_path", ""))
        self._steam_e.grid(row=0, column=0, sticky="ew")
        self._bind_entry_apply(self._steam_e, self._apply_steam_path)
        ctk.CTkButton(sf, text=self.t("browse"), width=80, font=font(12),
                      command=lambda: self._browse(self._steam_e, self._apply_steam_path)).grid(row=0, column=1, padx=(8, 0))

        _label(general, 8, "steamdb_detection")
        self._steamdb_detection_var = ctk.StringVar(
            value="on" if self.cfg.get("steamdb_detection_enabled") else "off")
        ctk.CTkSwitch(
            general, text=self.t("steamdb_detection_desc"),
            variable=self._steamdb_detection_var, onvalue="on", offvalue="off",
            font=font(12), command=self._toggle_steamdb_detection
        ).grid(row=9, column=0, padx=22, pady=(0, 18), sticky="w")

        # Automation
        _label(automation, 2, "auto_backup")
        af = _row(automation)
        af.grid(row=3, column=0, padx=22, sticky="w")
        self._auto_var = ctk.StringVar(value="on" if self.cfg.get("auto_backup_enabled") else "off")
        ctk.CTkSwitch(af, text=self.t("enable"), variable=self._auto_var,
                      onvalue="on", offvalue="off", font=font(12),
                      command=self._toggle_auto).pack(side="left")
        ctk.CTkLabel(af, text=f"  {self.t('interval_minutes')}:", font=font(12)).pack(side="left")
        self._int_e = ctk.CTkEntry(af, width=64, font=font(12))
        self._int_e.insert(0, str(self.cfg.get("auto_backup_interval", 30)))
        self._int_e.pack(side="left", padx=(6, 0))
        self._bind_entry_apply(self._int_e, self._apply_auto_backup_interval)

        _label(automation, 4, "file_watch")
        wf = _row(automation)
        wf.grid(row=5, column=0, padx=22, sticky="w")
        self._watch_var = ctk.StringVar(value="on" if self.cfg.get("watch_enabled") else "off")
        ctk.CTkSwitch(wf, text=self.t("enable"), variable=self._watch_var,
                      onvalue="on", offvalue="off", font=font(12),
                      command=self._toggle_watch).pack(side="left")
        ctk.CTkLabel(wf, text=f"  {self.t('cooldown_seconds')}:", font=font(12)).pack(side="left")
        self._cd_e = ctk.CTkEntry(wf, width=64, font=font(12))
        self._cd_e.insert(0, str(self.cfg.get("watch_cooldown", 60)))
        self._cd_e.pack(side="left", padx=(6, 0))
        self._bind_entry_apply(self._cd_e, self._apply_watch_cooldown)
        if not HAS_WATCHDOG:
            ctk.CTkLabel(automation, text=self.t("watchdog_missing"),
                         font=font(11), text_color="#ea580c").grid(
                row=6, column=0, padx=22, pady=(4, 0), sticky="w")

        _label(automation, 7, "auto_sync")
        sy1 = _row(automation)
        sy1.grid(row=8, column=0, padx=22, sticky="w")
        self._sync_var = ctk.StringVar(value="on" if self.cfg.get("sync_enabled") else "off")
        ctk.CTkSwitch(sy1, text=self.t("enable"), variable=self._sync_var,
                      onvalue="on", offvalue="off", font=font(12),
                      command=self._toggle_sync).pack(side="left")
        ctk.CTkLabel(sy1, text=f"  {self.t('interval_minutes')}:", font=font(12)).pack(side="left")
        self._sync_int_e = ctk.CTkEntry(sy1, width=64, font=font(12))
        self._sync_int_e.insert(0, str(self.cfg.get("sync_interval", 10)))
        self._sync_int_e.pack(side="left", padx=(6, 0))
        self._bind_entry_apply(self._sync_int_e, self._apply_sync_interval)

        _label(automation, 9, "sync_folder")
        sy2 = _row(automation)
        sy2.grid(row=10, column=0, padx=22, sticky="ew")
        sy2.grid_columnconfigure(0, weight=1)
        self._sync_folder_e = ctk.CTkEntry(
            sy2, font=font(12),
            placeholder_text=self.t("sync_folder_placeholder"))
        self._sync_folder_e.insert(0, self.cfg.get("sync_folder", ""))
        self._sync_folder_e.grid(row=0, column=0, sticky="ew")
        self._bind_entry_apply(self._sync_folder_e, self._apply_sync_folder)
        ctk.CTkButton(sy2, text=self.t("browse"), width=80, font=font(12),
                      command=lambda: self._browse(self._sync_folder_e, self._apply_sync_folder)).grid(row=0, column=1, padx=(8, 0))
        ctk.CTkButton(sy2, text=self.t("auto_detect"), width=96, font=font(12),
                      fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
                      command=self._auto_detect_cloud).grid(row=0, column=2, padx=(8, 0))

        _label(automation, 11, "sync_mode")
        self._sync_mode_var = ctk.StringVar(value=self._sync_mode_display(self.cfg.get("sync_mode", "smart")))
        ctk.CTkSegmentedButton(
            automation,
            values=[label for _, label in self._sync_mode_options()],
            variable=self._sync_mode_var, font=font(12),
            command=self._on_sync_mode_change
        ).grid(row=12, column=0, padx=22, sticky="w")
        ctk.CTkLabel(automation, text=self.t("sync_hint"), font=font(11),
                     text_color=C_SUBTLE_TEXT, wraplength=680,
                     justify="left").grid(row=13, column=0, padx=22, pady=(6, 0), sticky="w")

        sync_keep_row = _row(automation)
        sync_keep_row.grid(row=14, column=0, padx=22, pady=(10, 0), sticky="w")
        ctk.CTkLabel(sync_keep_row, text=self.t("sync_archive_keep"), font=font(12)).pack(side="left")
        self._sync_archive_keep_e = ctk.CTkEntry(sync_keep_row, width=64, font=font(12))
        self._sync_archive_keep_e.insert(0, str(self.cfg.get("sync_archive_keep", 3)))
        self._sync_archive_keep_e.pack(side="left", padx=6)
        self._bind_entry_apply(self._sync_archive_keep_e, self._apply_sync_archive_keep)
        ctk.CTkLabel(sync_keep_row, text=self.t("sync_archive_keep_suffix"), font=font(12)).pack(side="left")

        self._sync_notify_var = ctk.StringVar(value="on" if self.cfg.get("sync_notify", True) else "off")
        ctk.CTkSwitch(automation, text=self.t("sync_notify"),
                      variable=self._sync_notify_var, onvalue="on", offvalue="off",
                      font=font(12), command=self._toggle_sync_notify).grid(row=15, column=0, padx=22, pady=(12, 0), sticky="w")

        # --- WebDAV ---
        webdav_row = _row(automation)
        webdav_row.grid(row=16, column=0, padx=22, pady=(10, 0), sticky="w")
        self._webdav_var = ctk.StringVar(value="on" if self.cfg.get("webdav_enabled") else "off")
        ctk.CTkSwitch(webdav_row, text=self.t("webdav_enable"),
                      variable=self._webdav_var, onvalue="on", offvalue="off",
                      font=font(12), command=self._toggle_webdav).pack(side="left")
        if not HAS_WEBDAV:
            ctk.CTkLabel(automation, text=self.t("webdav_missing"),
                         font=font(11), text_color="#ea580c").grid(
                row=17, column=0, padx=22, pady=(4, 0), sticky="w")

        self._webdav_fields = _row(automation)
        self._webdav_fields.grid(row=18, column=0, padx=22, pady=(6, 18), sticky="ew")
        self._webdav_fields.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self._webdav_fields, text=self.t("webdav_preset"), font=font(12, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2))
        self._webdav_preset_var = ctk.StringVar(
            value=self._webdav_preset_display(self.cfg.get("webdav_preset", "generic")))
        self._webdav_preset_menu = ctk.CTkOptionMenu(
            self._webdav_fields,
            variable=self._webdav_preset_var,
            values=[self._webdav_preset_display(code) for code in WEBDAV_PRESET_OPTIONS],
            width=220, font=font(12), command=self._on_webdav_preset_change)
        self._webdav_preset_menu.grid(row=1, column=0, sticky="w", pady=(0, 6))
        self._webdav_preset_hint = ctk.CTkLabel(
            self._webdav_fields,
            text="",
            font=font(11),
            text_color=C_SUBTLE_TEXT)
        self._webdav_preset_hint.grid(row=2, column=0, sticky="w", pady=(0, 8))

        ctk.CTkLabel(self._webdav_fields, text=self.t("webdav_url"), font=font(12, "bold")).grid(
            row=3, column=0, sticky="w", pady=(0, 2))
        self._webdav_url_e = ctk.CTkEntry(
            self._webdav_fields, font=font(12),
            placeholder_text=self.t("webdav_url_ph"))
        self._webdav_url_e.insert(0, self.cfg.get("webdav_url", ""))
        self._webdav_url_e.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self._bind_entry_apply(self._webdav_url_e, self._apply_webdav_url)

        ctk.CTkLabel(self._webdav_fields, text=self.t("webdav_base_path"), font=font(12, "bold")).grid(
            row=5, column=0, sticky="w", pady=(0, 2))
        self._webdav_base_path_e = ctk.CTkEntry(
            self._webdav_fields, font=font(12),
            placeholder_text=self.t("webdav_base_path_ph"))
        self._webdav_base_path_e.insert(0, self.cfg.get("webdav_base_path", "/SteamSaveSync"))
        self._webdav_base_path_e.grid(row=6, column=0, sticky="ew", pady=(0, 8))
        self._bind_entry_apply(self._webdav_base_path_e, self._apply_webdav_base_path)

        ctk.CTkLabel(self._webdav_fields, text=self.t("webdav_username"), font=font(12, "bold")).grid(
            row=7, column=0, sticky="w", pady=(0, 2))
        self._webdav_user_e = ctk.CTkEntry(self._webdav_fields, font=font(12))
        self._webdav_user_e.insert(0, self.cfg.get("webdav_username", ""))
        self._webdav_user_e.grid(row=8, column=0, sticky="ew", pady=(0, 8))
        self._bind_entry_apply(self._webdav_user_e, self._apply_webdav_user)

        ctk.CTkLabel(self._webdav_fields, text=self.t("webdav_password"), font=font(12, "bold")).grid(
            row=9, column=0, sticky="w", pady=(0, 2))
        self._webdav_pass_e = ctk.CTkEntry(self._webdav_fields, font=font(12), show="*")
        self._webdav_pass_e.insert(0, _webdav_decode_password(self.cfg.get("webdav_password", "")))
        self._webdav_pass_e.grid(row=10, column=0, sticky="ew", pady=(0, 8))
        self._bind_entry_apply(self._webdav_pass_e, self._apply_webdav_pass)

        self._webdav_verify_ssl_var = ctk.StringVar(
            value="on" if self.cfg.get("webdav_verify_ssl", True) else "off")
        ctk.CTkSwitch(
            self._webdav_fields,
            text=self.t("webdav_verify_ssl_desc"),
            variable=self._webdav_verify_ssl_var, onvalue="on", offvalue="off",
            font=font(12), command=self._toggle_webdav_verify_ssl
        ).grid(row=11, column=0, sticky="w", pady=(0, 10))

        webdav_btn_row = _row(self._webdav_fields)
        webdav_btn_row.grid(row=12, column=0, sticky="w")
        ctk.CTkButton(webdav_btn_row, text=self.t("webdav_test"), width=120, font=font(12),
                      fg_color=BTN_SUCCESS, hover_color=BTN_SUCCESS_H,
                      command=self._test_webdav_connection).pack(side="left")
        self._webdav_status = ctk.CTkLabel(
            webdav_btn_row, text="", font=font(11), text_color=C_SUBTLE_TEXT)
        self._webdav_status.pack(side="left", padx=(10, 0))
        self._refresh_webdav_preset_hint()

        if not self.cfg.get("webdav_enabled"):
            self._webdav_fields.grid_remove()

        # System
        _label(system, 2, "minimize_tray")
        self._minimize_tray_var = ctk.StringVar(value="on" if self.cfg.get("minimize_to_tray", True) else "off")
        ctk.CTkSwitch(system, text=self.t("minimize_tray_desc"),
                      variable=self._minimize_tray_var, onvalue="on", offvalue="off",
                      font=font(12), command=self._toggle_minimize_to_tray).grid(row=3, column=0, padx=22, sticky="w")
        if not HAS_TRAY:
            ctk.CTkLabel(system, text=self.t("tray_missing"), font=font(11),
                         text_color="#ea580c").grid(row=4, column=0, padx=22, pady=(4, 0), sticky="w")

        _label(system, 5, "autostart")
        self._autostart_var = ctk.StringVar(value="on" if self.cfg.get("autostart") else "off")
        ctk.CTkSwitch(system, text=self.t("autostart_desc"),
                      variable=self._autostart_var, onvalue="on", offvalue="off",
                      font=font(12), command=self._toggle_autostart).grid(
            row=6, column=0, padx=22, sticky="w")

        self._autostart_minimize_var = ctk.StringVar(
            value="on" if self.cfg.get("autostart_minimize", True) else "off"
        )
        ctk.CTkSwitch(
            system, text=self.t("autostart_minimize_desc"),
            variable=self._autostart_minimize_var, onvalue="on", offvalue="off",
            font=font(12), command=self._toggle_autostart_minimize
        ).grid(row=7, column=0, padx=22, pady=(6, 0), sticky="w")

        _label(system, 8, "backup_rotation")
        rf = _row(system)
        rf.grid(row=9, column=0, padx=22, sticky="w")
        ctk.CTkLabel(rf, text=self.t("max_backups"), font=font(12)).pack(side="left")
        self._max_bk_e = ctk.CTkEntry(rf, width=64, font=font(12))
        self._max_bk_e.insert(0, str(self.cfg.get("max_backups_per_game", 20)))
        self._max_bk_e.pack(side="left", padx=6)
        self._bind_entry_apply(self._max_bk_e, self._apply_max_backups)
        ctk.CTkLabel(rf, text=self.t("max_backups_suffix"), font=font(12)).pack(side="left")

        rf2 = _row(system)
        rf2.grid(row=10, column=0, padx=22, pady=(6, 0), sticky="w")
        ctk.CTkLabel(rf2, text=self.t("max_backup_size"), font=font(12)).pack(side="left")
        self._max_size_e = ctk.CTkEntry(rf2, width=64, font=font(12))
        self._max_size_e.insert(0, str(self.cfg.get("max_backup_size_gb", 10.0)))
        self._max_size_e.pack(side="left", padx=6)
        self._bind_entry_apply(self._max_size_e, self._apply_max_backup_size)
        ctk.CTkLabel(rf2, text=self.t("max_backup_size_suffix"), font=font(12)).pack(side="left")

        _label(system, 11, "display_page_size")
        pf = _row(system)
        pf.grid(row=12, column=0, padx=22, sticky="w")
        ctk.CTkLabel(pf, text=self.t("games_page_size"), font=font(12)).pack(side="left")
        self._games_page_size_e = ctk.CTkEntry(pf, width=64, font=font(12))
        self._games_page_size_e.insert(0, str(self._games_page_size))
        self._games_page_size_e.pack(side="left", padx=6)
        self._bind_entry_apply(self._games_page_size_e, self._apply_games_page_size)
        ctk.CTkLabel(pf, text=self.t("games_page_size_suffix"), font=font(12)).pack(side="left")

        pf2 = _row(system)
        pf2.grid(row=13, column=0, padx=22, pady=(6, 0), sticky="w")
        ctk.CTkLabel(pf2, text=self.t("backup_page_size"), font=font(12)).pack(side="left")
        self._backup_page_size_e = ctk.CTkEntry(pf2, width=64, font=font(12))
        self._backup_page_size_e.insert(0, str(self._backup_page_size))
        self._backup_page_size_e.pack(side="left", padx=6)
        self._bind_entry_apply(self._backup_page_size_e, self._apply_backup_page_size)
        ctk.CTkLabel(pf2, text=self.t("backup_page_size_suffix"), font=font(12)).pack(side="left")

        _label(system, 14, "backup_storage")
        bpf = _row(system)
        bpf.grid(row=15, column=0, padx=22, sticky="ew")
        bpf.grid_columnconfigure(0, weight=1)
        self._backup_path_e = ctk.CTkEntry(
            bpf, font=font(12),
            placeholder_text=self.t("backup_storage_placeholder"))
        self._backup_path_e.insert(0, self.cfg.get("backup_path", ""))
        self._backup_path_e.grid(row=0, column=0, sticky="ew")
        self._bind_entry_apply(self._backup_path_e, self._apply_backup_path)
        ctk.CTkButton(bpf, text=self.t("browse"), width=80, font=font(12),
                      command=lambda: self._browse(self._backup_path_e, self._apply_backup_path)).grid(row=0, column=1, padx=(8, 0))
        self._backup_current_path_label = ctk.CTkLabel(
            system, text=self.t("current_path", path=str(BACKUP_ROOT)),
            font=font(11), text_color=C_SUBTLE_TEXT)
        self._backup_current_path_label.grid(
            row=16, column=0, padx=22, pady=(6, 18), sticky="w")
        ctk.CTkButton(
            system, text=self.bi("关于软件", "About"),
            width=120, height=34, font=font(12, "bold"),
            corner_radius=8, fg_color=BTN_PRIMARY, hover_color=BTN_PRIMARY_H,
            command=self._show_about_dialog
        ).grid(row=17, column=0, padx=22, pady=(0, 18), sticky="w")



    def _set_entry_value(self, entry, value):
        entry.delete(0, "end")
        entry.insert(0, str(value))

    def _invoke_and_break(self, callback):
        callback()
        return "break"

    def _bind_entry_apply(self, entry, callback):
        entry.bind("<FocusOut>", lambda _e: callback())
        entry.bind("<Return>", lambda _e: self._invoke_and_break(callback))

    def _refresh_backup_storage_hint(self):
        if hasattr(self, "_backup_current_path_label"):
            self._backup_current_path_label.configure(
                text=self.t("current_path", path=str(BACKUP_ROOT)))

    def _restart_auto_backup_runtime(self):
        if self.cfg.get("auto_backup_enabled"):
            self._stop_auto_backup()
            self._start_auto_backup()
        else:
            self._stop_auto_backup()

    def _backup_handlers_should_run(self) -> bool:
        games = [g for g in self.cfg.get("games", []) if isinstance(g, dict)]
        return any(
            is_game_exit_backup_enabled(game)
            or (self.cfg.get("auto_backup_enabled") and is_game_scheduled_backup_enabled(game))
            or (HAS_WATCHDOG and self.cfg.get("watch_enabled") and is_game_watch_backup_enabled(game))
            for game in games
        )

    def _restart_watchers_runtime(self):
        if self._backup_handlers_should_run():
            self._stop_watchers()
            self._start_watchers()
        else:
            self._stop_watchers()
        self._ensure_game_monitor()

    def _restart_sync_runtime(self):
        if self.cfg.get("sync_enabled"):
            self._stop_sync()
            self._start_sync()
        else:
            self._stop_sync()
        self._stop_game_monitor()
        self._ensure_game_monitor()

    def _on_language_change(self, _value=None):
        new_lang = self._language_code(self._language_var.get())
        if new_lang == self.lang:
            return
        self.cfg["language"] = new_lang
        self.lang = new_lang
        save_config(self.cfg)
        self._rebuild_ui(self._current_frame)

    def _apply_steam_path(self):
        self.cfg["steam_path"] = self._steam_e.get().strip()
        save_config(self.cfg)
        _SAVE_DETECTION_CACHE.clear()
        self._steam_account_lookup_cache = {"steam_path": None, "accountid_map": {}, "steam64_map": {}}

    def _apply_auto_backup_interval(self):
        try:
            value = max(1, int(self._int_e.get().strip()))
        except ValueError:
            value = int(self.cfg.get("auto_backup_interval", 30))
        self._set_entry_value(self._int_e, value)
        self.cfg["auto_backup_interval"] = value
        save_config(self.cfg)
        self._restart_auto_backup_runtime()

    def _apply_watch_cooldown(self):
        try:
            value = max(10, int(self._cd_e.get().strip()))
        except ValueError:
            value = int(self.cfg.get("watch_cooldown", 60))
        self._set_entry_value(self._cd_e, value)
        self.cfg["watch_cooldown"] = value
        save_config(self.cfg)
        self._restart_watchers_runtime()

    def _apply_sync_interval(self):
        try:
            value = max(1, int(self._sync_int_e.get().strip()))
        except ValueError:
            value = int(self.cfg.get("sync_interval", 10))
        self._set_entry_value(self._sync_int_e, value)
        self.cfg["sync_interval"] = value
        save_config(self.cfg)
        self._restart_sync_runtime()

    def _apply_sync_folder(self):
        self.cfg["sync_folder"] = self._sync_folder_e.get().strip()
        self.cfg["sync_folder_source"] = "manual"
        save_config(self.cfg)

    def _apply_sync_archive_keep(self):
        try:
            value = max(0, int(self._sync_archive_keep_e.get().strip()))
        except ValueError:
            value = int(self.cfg.get("sync_archive_keep", 3))
        self._set_entry_value(self._sync_archive_keep_e, value)
        self.cfg["sync_archive_keep"] = value
        save_config(self.cfg)
        enforce_all_sync_archive_limits(self.cfg)

    def _on_sync_mode_change(self, _value=None):
        self.cfg["sync_mode"] = self._sync_mode_code(self._sync_mode_var.get())
        save_config(self.cfg)
        self._restart_sync_runtime()
        self._refresh_proc_status()

    def _toggle_sync_notify(self):
        self.cfg["sync_notify"] = self._sync_notify_var.get() == "on"
        save_config(self.cfg)

    # ─── WebDAV 设置 ───
    def _webdav_preset_display(self, preset_code: str) -> str:
        code = preset_code if preset_code in WEBDAV_PRESET_OPTIONS else "generic"
        return self.t(f"webdav_preset_{code}")

    def _webdav_preset_code(self, display_text: str) -> str:
        for code in WEBDAV_PRESET_OPTIONS:
            if display_text == self._webdav_preset_display(code):
                return code
        return "generic"

    def _webdav_hint_for_preset(self, preset_code: str, username: str = "") -> str:
        preset = preset_code if preset_code in WEBDAV_PRESET_OPTIONS else "generic"
        if preset == "synology":
            return "http://nas:5005 / https://nas:5006"
        if preset == "qnap":
            return "https://nas:5006/webdav"
        if preset == "truenas":
            return "https://nas/dav"
        if preset == "nextcloud":
            if username:
                return f"https://cloud.example.com/remote.php/dav/files/{username}"
            return "https://cloud.example.com/remote.php/dav/files/<username>"
        if preset == "openmediavault":
            if username:
                return f"https://nas/remote.php/dav/files/{username}"
            return "https://nas/remote.php/dav/files/<username>"
        return "https://your-server.com/dav"

    def _refresh_webdav_preset_hint(self):
        if not hasattr(self, "_webdav_preset_hint"):
            return
        preset = str(self.cfg.get("webdav_preset", "generic") or "generic")
        username = str(self.cfg.get("webdav_username", "") or "").strip()
        self._webdav_preset_hint.configure(
            text=self.t("webdav_preset_hint", hint=self._webdav_hint_for_preset(preset, username))
        )

    def _toggle_webdav(self):
        en = self._webdav_var.get() == "on"
        self.cfg["webdav_enabled"] = en
        save_config(self.cfg)
        if en:
            self._webdav_fields.grid()
        else:
            self._webdav_fields.grid_remove()

    def _on_webdav_preset_change(self, display_text):
        self.cfg["webdav_preset"] = self._webdav_preset_code(display_text)
        if hasattr(self, "_webdav_url_e"):
            self._apply_webdav_url()
        self._refresh_webdav_preset_hint()
        save_config(self.cfg)

    def _apply_webdav_url(self):
        normalized = _webdav_normalize_url(
            self._webdav_url_e.get().strip(),
            str(self.cfg.get("webdav_preset", "generic") or "generic"),
            str(self.cfg.get("webdav_username", "") or "").strip(),
        )
        self.cfg["webdav_url"] = normalized
        if hasattr(self, "_webdav_url_e"):
            self._webdav_url_e.delete(0, "end")
            self._webdav_url_e.insert(0, normalized)
        save_config(self.cfg)

    def _apply_webdav_base_path(self):
        self.cfg["webdav_base_path"] = _webdav_base_path({
            "webdav_base_path": self._webdav_base_path_e.get().strip()
        })
        if hasattr(self, "_webdav_base_path_e"):
            self._webdav_base_path_e.delete(0, "end")
            self._webdav_base_path_e.insert(0, self.cfg["webdav_base_path"])
        save_config(self.cfg)

    def _apply_webdav_user(self):
        self.cfg["webdav_username"] = self._webdav_user_e.get().strip()
        if hasattr(self, "_webdav_url_e"):
            self._apply_webdav_url()
        self._refresh_webdav_preset_hint()
        save_config(self.cfg)

    def _apply_webdav_pass(self):
        self.cfg["webdav_password"] = _webdav_encode_password(self._webdav_pass_e.get())
        save_config(self.cfg)

    def _toggle_webdav_verify_ssl(self):
        self.cfg["webdav_verify_ssl"] = self._webdav_verify_ssl_var.get() == "on"
        save_config(self.cfg)

    def _test_webdav_connection(self):
        url = self._webdav_url_e.get().strip()
        user = self._webdav_user_e.get().strip()
        password = self._webdav_pass_e.get()
        if not url:
            self._show_warning(
                self.bi("提示", "Notice"),
                self.bi("请先输入 WebDAV 服务器地址", "Please enter a WebDAV server URL"))
            return
        self._webdav_status.configure(
            text=self.t("webdav_testing"), text_color=C_SUBTLE_TEXT)
        def _worker():
            ok, msg = webdav_test_connection(
                url, user, password,
                preset=str(self.cfg.get("webdav_preset", "generic") or "generic"),
                verify_ssl=bool(self.cfg.get("webdav_verify_ssl", True)),
                base_path=str(self.cfg.get("webdav_base_path", "/SteamSaveSync") or "/SteamSaveSync"),
            )
            self.after(0, lambda: self._on_webdav_test_result(ok, msg))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_webdav_test_result(self, ok, msg):
        if ok:
            self._webdav_status.configure(
                text=self.t("webdav_test_ok"), text_color="#10b981")
        else:
            self._webdav_status.configure(
                text=f"{self.t('webdav_test_fail')}: {msg}", text_color="#ef4444")

    def _toggle_minimize_to_tray(self):
        self.cfg["minimize_to_tray"] = self._minimize_tray_var.get() == "on"
        save_config(self.cfg)

    def _apply_max_backups(self):
        try:
            value = max(0, int(self._max_bk_e.get().strip()))
        except ValueError:
            value = int(self.cfg.get("max_backups_per_game", 20))
        self._set_entry_value(self._max_bk_e, value)
        self.cfg["max_backups_per_game"] = value
        save_config(self.cfg)
        enforce_backup_limits()

    def _apply_max_backup_size(self):
        try:
            value = max(0.0, float(self._max_size_e.get().strip()))
        except ValueError:
            value = float(self.cfg.get("max_backup_size_gb", 10.0))
        self._set_entry_value(self._max_size_e, value)
        self.cfg["max_backup_size_gb"] = value
        save_config(self.cfg)
        enforce_backup_limits()

    def _apply_games_page_size(self):
        value = clamp_int(self._games_page_size_e.get().strip(), self._games_page_size, 5, 200)
        self._set_entry_value(self._games_page_size_e, value)
        current_cfg = clamp_int(self.cfg.get("games_page_size", value), value, 5, 200)
        if value == self._games_page_size and current_cfg == value:
            return
        self._games_page_size = value
        self._games_page = 0
        self.cfg["games_page_size"] = value
        save_config(self.cfg)
        if getattr(self, "_current_frame", "") == "games":
            self._refresh_games_list()

    def _apply_backup_page_size(self):
        value = clamp_int(self._backup_page_size_e.get().strip(), self._backup_page_size, 5, 200)
        self._set_entry_value(self._backup_page_size_e, value)
        current_cfg = clamp_int(self.cfg.get("backup_page_size", value), value, 5, 200)
        if value == self._backup_page_size and current_cfg == value:
            return
        self._backup_page_size = value
        self._backup_page = 0
        self.cfg["backup_page_size"] = value
        save_config(self.cfg)
        if getattr(self, "_current_frame", "") == "backup":
            self._refresh_backup_list()

    def _apply_backup_path(self):
        old_backup_root = globals()["BACKUP_ROOT"]
        custom_bp = self._backup_path_e.get().strip()
        new_backup_root = Path(custom_bp) if custom_bp else Path(os.path.dirname(os.path.abspath(sys.argv[0]))) / "backups"
        try:
            new_backup_root.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self._show_error(self.t("settings_failed_title"), str(e))
            self._set_entry_value(self._backup_path_e, self.cfg.get("backup_path", ""))
            return
        if os.path.normpath(str(old_backup_root)) != os.path.normpath(str(new_backup_root)):
            if old_backup_root.exists() and any(old_backup_root.iterdir()):
                if self._ask_yes_no(
                        self.t("migrate_backups_title"),
                        self.t("migrate_backups_prompt",
                               old=old_backup_root, new=new_backup_root)):
                    count = migrate_backups(old_backup_root, new_backup_root)
                    self._show_info(
                        self.t("migrate_backups_title"),
                        self.t("migrate_backups_done", count=count))
            globals()["BACKUP_ROOT"] = new_backup_root
        self.cfg["backup_path"] = custom_bp
        save_config(self.cfg)
        self._refresh_backup_storage_hint()
        enforce_backup_limits()

    def _toggle_autostart(self):
        en = self._autostart_var.get() == "on"
        try:
            set_autostart_enabled(en)
            self.cfg["autostart"] = en
            save_config(self.cfg)
        except Exception as e:
            self._show_error(self.t("settings_failed_title"), str(e))
            self._autostart_var.set("on" if not en else "off")

    def _toggle_autostart_minimize(self):
        self.cfg["autostart_minimize"] = self._autostart_minimize_var.get() == "on"
        save_config(self.cfg)

    def _on_theme(self, val):
        t = self._theme_code(val)
        ctk.set_appearance_mode(t)
        self.cfg["theme"] = t; save_config(self.cfg)
        # Update settings page canvas & scrollbar colors dynamically
        try:
            canvas = getattr(self, "_settings_scroll", None)
            if canvas is not None:
                mode = "light" if t == "light" else "dark"
                canvas.configure(bg=C_MAIN_BG[0 if t == "light" else 1])
                for child in canvas.master.winfo_children():
                    if isinstance(child, tkinter.Canvas) and child is not canvas:
                        child.configure(bg=C_MAIN_BG[0 if t == "light" else 1])
                        items = child.find_all()
                        if len(items) >= 2:
                            cols = {
                                "light": {"track": "#e2e2e2", "thumb": "#c0c0c0"},
                                "dark":  {"track": "#333348", "thumb": "#555568"},
                            }[mode]
                            child.itemconfigure(items[0], fill=cols["track"])
                            child.itemconfigure(items[1], fill=cols["thumb"])
        except Exception:
            pass

    def _toggle_auto(self):
        en = self._auto_var.get() == "on"
        self.cfg["auto_backup_enabled"] = en; save_config(self.cfg)
        self._restart_auto_backup_runtime()
        self._restart_watchers_runtime()
        self._update_status()

    def _toggle_watch(self):
        en = self._watch_var.get() == "on"
        self.cfg["watch_enabled"] = en; save_config(self.cfg)
        self._restart_watchers_runtime()

    def _toggle_steamdb_detection(self):
        self.cfg["steamdb_detection_enabled"] = self._steamdb_detection_var.get() == "on"
        _SAVE_DETECTION_CACHE.clear()
        save_config(self.cfg)

    def _auto_detect_cloud(self):
        path = detect_cloud_folder()
        if path:
            self._sync_folder_e.delete(0, "end")
            self._sync_folder_e.insert(0, path)
            self.cfg["sync_folder"] = path
            self.cfg["sync_folder_source"] = "auto"
            save_config(self.cfg)
            self._show_info(self.t("detect_success_title"),
                            self.t("detect_success_body", path=path))
        else:
            self._show_info(self.t("detect_fail_title"),
                            self.t("detect_fail_body"))

    def _toggle_sync(self):
        en = self._sync_var.get() == "on"
        self.cfg["sync_enabled"] = en; save_config(self.cfg)
        self._restart_sync_runtime()

    def _manual_sync_all(self):
        if self._io_busy:
            self._notify_io_busy()
            return
        sf = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=True)
        if not sf:
            issue = get_sync_backend_issue(self.cfg.get("sync_folder", ""), self.cfg)
            if issue == "webdav_component_missing":
                msg = self.bi("WebDAV 已启用，但当前运行环境缺少 webdavclient3 组件", "WebDAV is enabled, but webdavclient3 is unavailable in the current runtime")
            elif issue == "webdav_url_missing":
                msg = self.bi("WebDAV 已启用，但服务器地址为空", "WebDAV is enabled, but the server URL is empty")
            elif issue == "webdav_credentials_incomplete":
                msg = self.bi("WebDAV 用户名或密码不完整", "WebDAV username or password is incomplete")
            else:
                msg = self.bi("请先配置同步文件夹或启用 WebDAV 远程同步", "Please configure a sync folder or enable WebDAV remote sync")
            self._show_warning(
                self.bi("提示", "Notice"),
                msg,
            )
            return
        games = self.cfg.get("games", [])
        can_discover_webdav = webdav_is_ready(self.cfg)
        if not games and not can_discover_webdav:
            self._show_info(self.bi("提示", "Notice"), self.bi("请先添加游戏", "Please add a game first")); return
        self._set_io_busy(True)
        def _worker():
            try:
                discovered_count = 0
                if not self.cfg.get("games", []) and can_discover_webdav:
                    discovered = webdav_discover_remote_games(self.cfg, sf)
                    if discovered:
                        self.cfg.setdefault("games", []).extend(discovered)
                        save_config(self.cfg)
                        discovered_count = len(discovered)
                if not self.cfg.get("games", []):
                    results = [(
                        self.bi("云端存档", "Cloud saves"),
                        self.bi("未在 WebDAV 云端发现可恢复的同步包", "No recoverable sync archives were found on WebDAV"),
                    )]
                else:
                    results = sync_all_games(self.cfg)
                    if discovered_count:
                        results.insert(0, (
                            self.bi("云端存档", "Cloud saves"),
                            self.bi(
                                f"已从 WebDAV 云端发现并加入 {discovered_count} 个游戏",
                                f"Discovered and added {discovered_count} game(s) from WebDAV",
                            ),
                        ))
                self.after(0, lambda: self._on_sync_all_done(results))
            except Exception as e:
                self.after(0, lambda err=str(e): self._on_sync_failed(err))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_sync_all_done(self, results):
        self._set_io_busy(False)
        self._on_backups_changed()
        lines = [self.bi(f"{name}：{msg}", f"{name}: {msg}") for name, msg in results]
        self._show_info(self.bi("同步完成", "Sync Complete"), "\n".join(lines))
        conflict_keys = {
            get_game_sync_key(game)
            for game in self.cfg.get("games", [])
            if get_game_sync_state(self.cfg, game).get("pending_conflict")
        }
        for game in self.cfg.get("games", []):
            if get_game_sync_key(game) in conflict_keys:
                self._show_sync_conflict_dialog(game)

    def _on_sync_failed(self, err):
        self._set_io_busy(False)
        self._show_error(self.bi("同步失败", "Sync Failed"), self.bi(f"同步过程中出错：\n{err}", f"An error occurred during sync:\n{err}"))

    def _manual_sync_one(self, idx):
        if self._io_busy:
            self._notify_io_busy()
            return
        g = dict(self.cfg["games"][idx])
        if not is_game_sync_enabled(g):
            self._show_info(
                self.bi("同步已关闭", "Sync Disabled"),
                self.bi("此游戏已关闭存档自动同步，请先在游戏详情页开启。", "Save sync is disabled for this game. Enable it on the game detail page first."),
            )
            return
        sf = get_effective_sync_root(self.cfg.get("sync_folder", ""), self.cfg, ensure=True)
        if not sf:
            issue = get_sync_backend_issue(self.cfg.get("sync_folder", ""), self.cfg)
            if issue == "webdav_component_missing":
                msg = self.bi("WebDAV 已启用，但当前运行环境缺少 webdavclient3 组件", "WebDAV is enabled, but webdavclient3 is unavailable in the current runtime")
            elif issue == "webdav_url_missing":
                msg = self.bi("WebDAV 已启用，但服务器地址为空", "WebDAV is enabled, but the server URL is empty")
            elif issue == "webdav_credentials_incomplete":
                msg = self.bi("WebDAV 用户名或密码不完整", "WebDAV username or password is incomplete")
            else:
                msg = self.bi("请先配置同步文件夹或启用 WebDAV 远程同步", "Please configure a sync folder or enable WebDAV remote sync")
            self._show_warning(
                self.bi("提示", "Notice"),
                msg,
            )
            return
        mode = self.cfg.get("sync_mode", "bidirectional")
        game_name = g.get("name", "")
        self._set_io_busy(True)
        def _worker():
            try:
                r = sync_game_save(g, sf, mode, cfg=self.cfg)
                self.after(0, lambda: self._on_sync_one_done(game_name, r, idx))
            except Exception as e:
                self.after(0, lambda err=f"{type(e).__name__}: {e}", name=game_name:
                    self._on_sync_one_failed(name, err))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_sync_one_done(self, game_name, result, idx):
        self._set_io_busy(False)
        live_game = self.cfg["games"][idx] if idx < len(self.cfg.get("games", [])) else None
        self._on_backups_changed(live_game)
        if result.startswith("冲突：") or result.startswith("Conflict:"):
            if live_game:
                self._show_sync_conflict_dialog(live_game)
        else:
            self._show_info(self.bi("同步结果", "Sync Result"), self.bi(f"「{game_name}」{result}", f"{game_name}: {result}"))

    def _on_sync_one_failed(self, game_name, err):
        self._set_io_busy(False)
        self._show_error(self.bi("同步失败", "Sync Failed"), self.bi(f"「{game_name}」同步出错：\n{err}", f"Sync failed for {game_name}:\n{err}"))

    # ─── 定时备份 ───
    def _start_auto_backup(self):
        _join_thread(self._auto_thread, timeout=0.2)
        if self._auto_thread and self._auto_thread.is_alive():
            return
        self._auto_stop_event.clear()
        self.auto_backup_running = True
        self._auto_thread = threading.Thread(target=self._auto_loop, daemon=True)
        self._auto_thread.start()
        self._update_status()

    def _stop_auto_backup(self):
        thread = self._auto_thread
        self._auto_stop_event.set()
        _join_thread(thread, timeout=1.0)
        if self._auto_thread is thread and (thread is None or not thread.is_alive()):
            self._auto_thread = None
        self.auto_backup_running = False
        self._update_status()

    def _auto_loop(self):
        while not self._auto_stop_event.is_set():
            iv = self.cfg.get("auto_backup_interval", 30) * 60
            self._auto_stop_event.wait(iv)
            if self._auto_stop_event.is_set(): break
            running_game_keys = set()
            try:
                if self._game_monitor:
                    running_game_keys = self._game_monitor._find_running_games()
                else:
                    running_game_keys = GameProcessMonitor(self.cfg)._find_running_games()
            except Exception:
                running_game_keys = set()
            for g in self.cfg.get("games", []):
                if not is_game_scheduled_backup_enabled(g):
                    continue
                runtime_key = get_game_runtime_key(g)
                if not runtime_key or runtime_key not in running_game_keys:
                    continue
                handler = self._watch_handlers_by_game.get(runtime_key)
                if handler:
                    handler.request_scheduled_backup()

    def _update_status(self):
        if self.cfg.get("auto_backup_enabled"):
            iv = self.cfg.get("auto_backup_interval", 30)
            self._status_label.configure(
                text=self.t("status_auto_on", minutes=iv), text_color=BTN_SUCCESS)
        else:
            self._status_label.configure(
                text=self.t("status_auto_off"), text_color=C_SUBTLE_TEXT)

    # ─── 自动同步 ───
    def _start_sync(self):
        _join_thread(self._sync_thread, timeout=0.2)
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._sync_stop.clear()
        self._sync_running = True
        # smart 模式不需要定时同步，由进程监控触发
        if self.cfg.get("sync_mode") != "smart":
            self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
            self._sync_thread.start()
        else:
            self._sync_thread = None

    def _stop_sync(self):
        thread = self._sync_thread
        self._sync_stop.set()
        _join_thread(thread, timeout=1.0)
        if self._sync_thread is thread and (thread is None or not thread.is_alive()):
            self._sync_thread = None
        self._sync_running = False

    def _sync_loop(self):
        while not self._sync_stop.is_set():
            iv = self.cfg.get("sync_interval", 10) * 60
            self._sync_stop.wait(iv)
            if self._sync_stop.is_set():
                break
            try:
                retry_results = run_sync_retries(self.cfg, self.cfg.get("sync_folder", ""))
                if retry_results:
                    self.after(0, self._on_backups_changed)
                results = sync_all_games(self.cfg, auto=True)
                if results:
                    self.after(0, self._on_backups_changed)
                for game in self.cfg.get("games", []):
                    state = get_game_sync_state(self.cfg, game)
                    if state.get("pending_conflict"):
                        self._enqueue_sync_conflict_dialog(game)
                if self.cfg.get("sync_notify", True):
                    synced = [
                        self.bi(f"{n}：{m}", f"{n}: {m}")
                        for n, m in results
                        if "跳过" not in m
                        and "无需" not in m
                        and "Skipped:" not in m
                        and "No need" not in m
                        and not m.startswith("冲突：")
                        and not m.startswith("Conflict:")
                    ]
                    if synced:
                        send_desktop_notification(
                            self.bi("存档管家 · 自动同步完成", "Steam Save Manager · Auto Sync Complete"),
                            self.bi("；", "; ").join(synced[:3]))
            except Exception:
                pass

    # ─── 智能云存档进程监控 ───
    def _is_game_running_for_backup(self, game: dict) -> bool:
        monitor = self._game_monitor
        return bool(monitor and monitor.is_game_running(game))

    def _record_backup_activity(self, zh: str, en: str):
        monitor = self._game_monitor
        if monitor:
            monitor.add_activity(zh, en)
        logger.info("%s", bilingual_cfg(self.cfg, zh, en))

    def _handle_game_stopped_for_backup(self, game: dict):
        runtime_key = get_game_runtime_key(game)
        handler = self._watch_handlers_by_game.get(runtime_key)
        if handler:
            handler.on_game_stopped(force=is_game_exit_backup_enabled(game))

    def _start_game_monitor(self):
        self._stop_game_monitor()
        if self._game_monitor is not None:
            return
        self._game_monitor = GameProcessMonitor(
            self.cfg,
            on_backups_changed=lambda *a: self.after(0, lambda: self._on_backups_changed(*a) if a else self._on_backups_changed()),
            on_game_stopped=self._handle_game_stopped_for_backup,
        )
        self._game_monitor.start()

    def _stop_game_monitor(self):
        monitor = self._game_monitor
        if monitor:
            monitor.stop()
            if monitor._thread and monitor._thread.is_alive():
                return
            self._game_monitor = None

    # ─── 文件监控 ───
    def _start_watchers(self):
        self._stop_watchers()
        cd = self.cfg.get("watch_cooldown", 60)
        observer = Observer() if HAS_WATCHDOG and self.cfg.get("watch_enabled", True) else None
        if not hasattr(self, "_watch_handlers_by_game"):
            self._watch_handlers_by_game = {}
        handler_count = 0
        for g in self.cfg.get("games", []):
            scheduled_enabled = bool(
                self.cfg.get("auto_backup_enabled")
                and is_game_scheduled_backup_enabled(g)
            )
            watch_enabled = bool(
                observer is not None and is_game_watch_backup_enabled(g)
            )
            exit_enabled = is_game_exit_backup_enabled(g)
            if not (scheduled_enabled or watch_enabled or exit_enabled):
                continue
            save_paths = get_game_save_paths(g, existing_only=True)
            if not save_paths:
                continue
            handler = SaveChangeHandler(
                g, cd,
                cfg=self.cfg,
                on_backup_created=lambda game_ref, self=self: self.after(
                    0, lambda gr=dict(game_ref): self._on_backups_changed(gr)
                ),
                is_game_running=getattr(self, "_is_game_running_for_backup", None),
                on_status=getattr(self, "_record_backup_activity", None),
            )
            runtime_key = get_game_runtime_key(g)
            if runtime_key:
                self._watch_handlers_by_game[runtime_key] = handler
            if watch_enabled:
                for save_path in save_paths:
                    observer.schedule(handler, save_path, recursive=True)
                    handler_count += 1
            self._watch_handlers.append(handler)
        if observer is not None and handler_count:
            observer.start()
            self._watchers.append(observer)

    def _stop_watchers(self):
        for handler in self._watch_handlers:
            try:
                handler.close()
            except Exception:
                pass
        for obs in self._watchers:
            try:
                obs.stop()
            except Exception:
                pass
        for obs in self._watchers:
            try:
                obs.join(timeout=1.0)
            except Exception:
                pass
        self._watchers.clear()
        self._watch_handlers.clear()
        if hasattr(self, "_watch_handlers_by_game"):
            self._watch_handlers_by_game.clear()

    # ─── 系统托盘 & 关闭 ───
    def _on_close(self):
        if self.cfg.get("minimize_to_tray", True) and HAS_TRAY:
            self.withdraw()
            self._create_tray()
            return
        self._stop_auto_backup()
        self._stop_watchers()
        self._stop_sync()
        self._stop_game_monitor()
        self.destroy()

    def _create_tray(self):
        if not HAS_TRAY: return
        if hasattr(self, "_tray") and self._tray:
            return
        # 绘制一个精致的托盘图标：圆角 indigo 底 + 白色手柄图形
        from PIL import ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        # 圆角矩形背景
        draw.rounded_rectangle([0, 0, size - 1, size - 1], radius=14,
                               fill=(99, 102, 241, 255))
        # 白色游戏手柄简笔：中间椭圆 + 两侧手柄
        cx, cy = size // 2, size // 2 + 2
        # 主体
        draw.rounded_rectangle([cx - 18, cy - 10, cx + 18, cy + 10],
                               radius=8, fill=(255, 255, 255, 255))
        # 左手柄
        draw.ellipse([cx - 22, cy - 6, cx - 12, cy + 6],
                     fill=(255, 255, 255, 255))
        # 右手柄
        draw.ellipse([cx + 12, cy - 6, cx + 22, cy + 6],
                     fill=(255, 255, 255, 255))
        # 十字键（左侧）
        draw.rectangle([cx - 13, cy - 2, cx - 5, cy + 2],
                       fill=(99, 102, 241, 255))
        draw.rectangle([cx - 11, cy - 4, cx - 7, cy + 4],
                       fill=(99, 102, 241, 255))
        # AB 按钮（右侧）
        draw.ellipse([cx + 5, cy - 4, cx + 9, cy],
                     fill=(99, 102, 241, 255))
        draw.ellipse([cx + 10, cy, cx + 14, cy + 4],
                     fill=(99, 102, 241, 255))
        img = img.convert("RGB")
        menu = pystray.Menu(
            pystray.MenuItem(self.bi("显示窗口", "Show Window"), self._tray_show, default=True),
            pystray.MenuItem(self.bi("关于软件", "About"), self._tray_show_about),
            pystray.MenuItem(self.bi("退出", "Quit"), self._tray_quit))
        tray_cls = _DoubleClickTrayIcon or pystray.Icon
        self._tray = tray_cls(APP_NAME, img, APP_NAME, menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    def _tray_show(self, *a):
        if hasattr(self, "_tray") and self._tray:
            self._tray.stop()
            self._tray = None
        def _show():
            self.deiconify()
            self.lift()
            self.focus_force()
            self._restore_popup_windows()
        self.after(0, _show)

    def _tray_show_about(self, *a):
        self.after(0, self._show_about_dialog)

    def _tray_quit(self, *a):
        if hasattr(self, "_tray") and self._tray:
            self._tray.stop()
            self._tray = None
        self._stop_auto_backup(); self._stop_watchers()
        self._stop_game_monitor()
        self.after(0, self.destroy)

    @staticmethod
    def _fmt_ts(ts: str) -> str:
        for fmt in ("%Y%m%d_%H%M%S_%f", "%Y%m%d_%H%M%S"):
            try:
                return datetime.datetime.strptime(ts, fmt).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
        return ts


# ══════════════════════════════════════════════
#  单实例锁
# ══════════════════════════════════════════════

def acquire_lock() -> bool:
    """
    尝试获取单实例文件锁。
    Windows: 使用 msvcrt.locking 对锁文件加独占锁
    Unix: 使用 fcntl.flock 加独占锁
    返回 True 表示成功获取（当前无其他实例），False 表示已有实例运行。
    """
    ensure_dirs()
    try:
        # 以读写模式打开（不存在则创建），不要关闭——持有到进程退出
        lock_fd = open(LOCK_FILE, "w")
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        # 写入 PID 方便调试
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        # 保持引用，防止被 GC 关闭
        acquire_lock._fd = lock_fd
        return True
    except (OSError, IOError):
        return False


# ══════════════════════════════════════════════
if __name__ == "__main__":
    ensure_dirs()
    if not acquire_lock():
        if STARTUP_LAUNCH:
            sys.exit(0)
        # 已有实例在运行，弹出提示后退出
        boot_lang = normalize_language("")
        root = ctk.CTk()
        root.overrideredirect(True)
        root.attributes("-alpha", 0.0)
        root.update_idletasks()
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        root.geometry(f"1x1+{max(sw//2, 0)}+{max(sh//2, 0)}")
        root.deiconify()
        root.lift()
        messagebox.showinfo(
            bilingual_text(boot_lang, "提示", "Notice"),
            bilingual_text(boot_lang, "Steam 存档管家已在运行中，请勿重复启动。", "Steam Save Manager is already running. Please do not start another instance."),
            parent=root,
        )
        root.destroy()
        sys.exit(0)
    SteamSaveManager().mainloop()
