# -*- coding: utf-8 -*-
"""游匣 GameBox · 本地游戏库管理器"""
import os
import sys
import json


import time
import queue
import datetime
import tempfile
import subprocess
import threading
import urllib.request
import urllib.parse
import re
import html
import xml.etree.ElementTree as ET
import ctypes
from ctypes import wintypes
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import glob
import hashlib
import shutil
import store          # SQLite 单一数据源（src/store.py）
try:
    import winreg
except ImportError:
    winreg = None


def _gv_log(msg):
    """可选启动诊断：仅当 GV_DEBUG_LOG 指向文件路径时写入，正常运行零开销。"""
    p = os.environ.get("GV_DEBUG_LOG")
    if not p:
        return
    try:
        with open(p, "a", encoding="utf-8") as f:
            f.write("%.3f %s\n" % (time.time(), msg))
    except Exception:
        pass


# ---------------- Windows 窗口辅助（单实例 / 置顶 / 模态 / 关闭拦截） ----------------
_user32 = ctypes.windll.user32 if os.name == "nt" else None
_WND_ENUM_PROC = (ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
                  if _user32 else None)
SINGLE_MUTEX_NAME = os.environ.get("GV_TEST_MUTEX", "GameBox_SingleInstance_Mutex")
BRIDGE_PORT_FILE = os.environ.get("GV_TEST_PORT_FILE", os.path.join(tempfile.gettempdir(), "GameBox_Bridge.port"))
SHELL_PID_FILE = os.environ.get("GV_TEST_SHELL_PID", os.path.join(tempfile.gettempdir(), "GameBox_Shell.pid"))
BROWSER_TITLE_KEYS = ("游匣", "GameBox")
_MUTEX_HANDLE = None
_HWND_TOPMOST = (ctypes.c_void_p(-1) if _user32 else None)

if _user32:
    try:
        for _fn, _args in (
            ("IsWindowVisible", [wintypes.HWND]),
            ("GetWindowTextLengthW", [wintypes.HWND]),
            ("GetWindowTextW", [wintypes.HWND, ctypes.c_void_p, ctypes.c_int]),
            ("GetWindowRect", [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]),
            ("GetWindowThreadProcessId", [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
            ("EnableWindow", [wintypes.HWND, wintypes.BOOL]),
            ("ShowWindow", [wintypes.HWND, ctypes.c_int]),
            ("SetForegroundWindow", [wintypes.HWND]),
            ("IsWindow", [wintypes.HWND]),
            ("SetWindowPos", [wintypes.HWND, ctypes.c_void_p, ctypes.c_int,
                              ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]),
            ("PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]),
            ("GetWindowLongW", [wintypes.HWND, ctypes.c_int]),
            ("SetWindowLongW", [wintypes.HWND, ctypes.c_int, ctypes.c_long]),
            ("IsZoomed", [wintypes.HWND]),
            ("IsIconic", [wintypes.HWND]),
        ):
            getattr(_user32, _fn).argtypes = _args
        _user32.GetWindowLongW.restype = ctypes.c_long
        _user32.SetWindowLongW.restype = ctypes.c_long
        _user32.IsZoomed.restype = ctypes.c_int
        _user32.IsIconic.restype = ctypes.c_int
        _user32.EnumWindows.argtypes = [_WND_ENUM_PROC, wintypes.LPARAM]
    except Exception as e:
        _gv_log("user32 argtypes failed: %r" % (e,))


def enum_top_windows(pid=None, include_hidden=False):
    """列出顶层窗口：[(hwnd, area, title, visible)]。"""
    if not _user32:
        return []
    out = []

    def cb(hwnd, lp):
        d = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(d))
        if pid is not None and d.value != pid:
            return True
        vis = bool(_user32.IsWindowVisible(hwnd))
        if not vis and not include_hidden:
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            b = ctypes.create_unicode_buffer(n + 1)
            _user32.GetWindowTextW(hwnd, ctypes.cast(b, ctypes.c_void_p), n + 1)
            title = b.value
        r = wintypes.RECT()
        _user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        out.append((int(hwnd), area, title, vis))
        return True

    try:
        _user32.EnumWindows(_WND_ENUM_PROC(cb), 0)
    except Exception as e:
        _gv_log("EnumWindows failed: %r" % (e,))
    return out


def browser_main_hwnd(pid=None):
    """定位本程序拉起的浏览器窗口；pid 未知时返回 None。

    不做全局标题匹配：用户自己的 Edge/Chrome 里也可能开着标题含「游匣」的页面，
    按标题抓窗口会误认、误操作别人的浏览器。
    """
    if not _user32 or not pid:
        return None
    wins = enum_top_windows(pid, include_hidden=True)
    titled = [w for w in wins if any(k in w[2] for k in BROWSER_TITLE_KEYS)]
    if titled:
        return max(titled, key=lambda w: w[1])[0]
    return max(wins, key=lambda w: w[1])[0] if wins else None


def win_enable(hwnd, on):
    if _user32 and hwnd:
        try:
            _user32.EnableWindow(hwnd, bool(on))
        except Exception:
            pass


def win_show(hwnd, on):
    if _user32 and hwnd:
        try:
            _user32.ShowWindow(hwnd, 5 if on else 0)   # SW_SHOW / SW_HIDE
        except Exception:
            pass


def win_foreground(hwnd):
    if _user32 and hwnd:
        try:
            _user32.ShowWindow(hwnd, 9)                 # SW_RESTORE
            _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass


def win_topmost(hwnd):
    if _user32 and hwnd:
        try:
            _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0, 0x0003)
        except Exception:
            pass


# ---------------- 浏览器外壳生命周期（Job Object，随宿主退出自动回收） ----------------
# 旧做法：启动时按窗口标题全局扫描「游匣」并强杀窗口所在进程。只要用户自己的
# Edge/Chrome 恰好开着含该标题的页面（GitHub 仓库页、搜索页等），整只浏览器
# 会被连坐杀掉。现在改为把 --app 子进程挂进 Job Object：宿主无论正常退出还是被
# 任务管理器强杀，Job 句柄关闭时系统都会一并结束这个浏览器进程，既不留下孤儿，
# 也绝不会碰到用户自己的浏览器窗口。
_JOB_HANDLE = None
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def attach_browser_to_job(proc):
    """把浏览器子进程挂进 Job Object（KILL_ON_JOB_CLOSE）。

    只作用于本进程 Popen 出来的那一个浏览器进程及其子进程，完全不枚举系统窗口，
    因此不会影响用户正在使用的浏览器。
    """
    global _JOB_HANDLE
    if os.name != "nt" or proc is None:
        return
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                              ctypes.c_void_p, wintypes.DWORD]
        k.SetInformationJobObject.restype = wintypes.BOOL
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.AssignProcessToJobObject.restype = wintypes.BOOL
        if _JOB_HANDLE is None:
            h = k.CreateJobObjectW(None, None)
            if not h:
                _gv_log("job: create failed err=%s" % ctypes.get_last_error())
                return
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(
                    h, _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(info), ctypes.sizeof(info)):
                _gv_log("job: set limit failed err=%s" % ctypes.get_last_error())
            _JOB_HANDLE = h                      # 句柄持有到进程结束
        handle = int(getattr(proc, "_handle", 0) or 0)
        if not handle:
            return
        ok = k.AssignProcessToJobObject(_JOB_HANDLE, handle)
        _gv_log("job: assign pid=%s ok=%s" % (proc.pid, bool(ok)))
    except Exception as e:
        _gv_log("job: attach failed %r" % (e,))


def _close_previous_shell():
    """只关闭「上一次启动、本程序自己拉起且仍残留」的浏览器外壳。

    识别条件三层同时满足才动手：pid 来自本程序写的 pid 文件、该进程确实是
    msedge/chrome、且它名下存在标题含「游匣」的窗口。不做全局标题扫描，因此
    用户自己的浏览器即使开着同名页面也不会被关掉。
    """
    if os.name != "nt" or not _user32:
        return
    try:
        with open(SHELL_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        return
    if not pid:
        return
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenProcess.restype = wintypes.HANDLE
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD)]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        h = k.OpenProcess(0x1000, False, pid)      # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return
        try:
            code = wintypes.DWORD()
            if not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:
                return                                  # 259 = STILL_ACTIVE
            n = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return
            if os.path.basename(buf.value).lower() not in ("msedge.exe", "chrome.exe"):
                return
        finally:
            k.CloseHandle(h)
        wins = [w for w in enum_top_windows(pid, include_hidden=True)
                if any(key in w[2] for key in BROWSER_TITLE_KEYS)]
        if not wins:
            return
        for hwnd, _area, _title, _vis in wins:
            try:
                _user32.PostMessageW(hwnd, 0x0010, 0, 0)    # WM_CLOSE
            except Exception:
                pass
        _gv_log("closed previous shell pid=%s windows=%d" % (pid, len(wins)))
        time.sleep(0.4)                                 # 让旧 profile 锁释放
    except Exception as e:
        _gv_log("close previous shell failed: %r" % (e,))


def acquire_single_instance():
    """命名互斥体：已有实例时返回 False。句柄需全局持有直到进程结束。"""
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        k.CreateMutexW.restype = wintypes.HANDLE
        _MUTEX_HANDLE = k.CreateMutexW(None, False, SINGLE_MUTEX_NAME)
        return ctypes.get_last_error() != 183        # ERROR_ALREADY_EXISTS
    except Exception as e:
        _gv_log("mutex check failed: %r" % (e,))
        return True


def _release_mutex_probe():
    """关掉一次失败的探测所打开的 mutex 句柄。

    CreateMutexW 的 ERROR_ALREADY_EXISTS 只表示「调用时对象已存在」。若把
    失败时拿到的句柄留着，进程自己就持有该 mutex，之后无论旧进程是否退出，
    重试永远都是 ALREADY_EXISTS —— 自更新重启会卡在这里直到超时。
    """
    global _MUTEX_HANDLE
    h = _MUTEX_HANDLE
    _MUTEX_HANDLE = None
    if h and os.name == "nt":
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle.restype = wintypes.BOOL
            k.CloseHandle(ctypes.c_void_p(h))
        except Exception:
            pass


def activate_existing_instance():
    """二次启动时把已在运行的实例界面调回前台，而不是新开一份。"""
    try:
        with open(BRIDGE_PORT_FILE, encoding="utf-8") as f:
            port = int(f.read().strip())
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        op.open("http://127.0.0.1:%d/api/show" % port, timeout=1.5).read()
    except Exception:
        pass
    # 只用运行实例自己记录的浏览器 pid 定位窗口，不做全局标题匹配：
    # 用户自己的浏览器里可能有标题含「游匣」的页面，不能拿来置顶。
    pid = 0
    try:
        with open(SHELL_PID_FILE, encoding="utf-8") as f:
            pid = int(f.read().strip() or 0)
    except Exception:
        pid = 0
    hwnd = browser_main_hwnd(pid) if pid else None
    if hwnd:
        win_show(hwnd, True)
        win_foreground(hwnd)


from PyQt5.QtCore import (Qt, QSize, QRect, QRectF, QTimer, QPoint, QSettings, QObject,
                          QAbstractListModel, QModelIndex, pyqtSignal, QVariantAnimation,
                          QUrl, QUrlQuery)
from PyQt5.QtGui import (QPainter, QPixmap, QColor, QFont, QFontMetrics, QIcon, QKeySequence,
                         QPainterPath, QBrush, QPen, QImage, QCursor, QLinearGradient)
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QShortcut,
                             QHBoxLayout, QGridLayout, QLabel, QLineEdit, QComboBox, QPushButton,
                             QListView, QListWidget, QListWidgetItem, QStyledItemDelegate, QStyle, QMessageBox,
                             QDialog, QFrame, QSizePolicy, QAbstractItemView,
                             QMenu, QAction, QTableWidget, QTableWidgetItem, QHeaderView,
                             QFileDialog, QProgressDialog, QScrollArea, QGraphicsDropShadowEffect,
                             QStackedWidget, QCheckBox)

from PyQt5.QtWidgets import QSystemTrayIcon

WEB_UI_AVAILABLE = True  # 界面由系统浏览器渲染

APP_NAME = "游匣"
VERSION = "1.0"

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    here = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = os.path.dirname(here) if os.path.basename(here).lower() == "src" else here
DATA_DIR = os.path.join(APP_DIR, "data")
if not os.path.isdir(DATA_DIR) and hasattr(sys, "_MEIPASS"):
    DATA_DIR = os.path.join(sys._MEIPASS, "data")


def _config_dir():
    """配置文件目录：程序目录可写则用程序目录，否则回退到 %APPDATA%。"""
    base = APP_DIR
    try:
        probe = os.path.join(APP_DIR, ".write_test")
        with open(probe, "w") as f:
            f.write("")
        os.remove(probe)
    except Exception:
        base = os.path.join(os.environ.get("APPDATA", APP_DIR), APP_NAME)
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = APP_DIR
    return base


CONFIG_PATH = os.path.join(_config_dir(), "config.json")

if getattr(sys, "frozen", False):
    WEBUI_DIR = os.path.join(sys._MEIPASS, "webui")
else:
    WEBUI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui")

# ---------------- 扫描配置 ----------------
DEFAULT_ROOTS = [
    "F:\\",
    "F:\\xiao",
    "F:\\RTS",
    "F:\\SteamLibrary\\steamapps\\common",
]


def root_label(path):
    """从扫描根目录推导「分区」标签：盘符根 → 'F:\\'，其余取末级目录名。"""
    p = os.path.abspath(path).rstrip("\\/")
    if len(p) == 2 and p[1] == ":":
        return p + "\\"
    return os.path.basename(p) or p
EXCLUDE = {
    "$recycle.bin", "system volume information", "recovery", "workbuddy", "gamevault",
    "缓存", "i4tools9", "qqpcmgr_recyclebin", "gamepp wonderful moment",
    "dumpstack.log.tmp", "boottel.dat", "$windows.~ws",
    "rts", "xiao", "steamlibrary", "steamapps",
    "gamebox", "游匣", "gamevault",
}
EXE_SKIP = ("unins", "uninstall", "setup", "vcredist", "dxsetup", "dxwebsetup",
            "dotnet", "redist", "crashreport", "notification_helper", "oalinst",
            "easyanticheat", "battleye")

def human_size(n):
    if not n:
        return "—"
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}" if u != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.1f} PB"


def _wrap_cjk(text, n=8, maxl=3):
    """把标题拆成最多 maxl 行（每行约 n 字），供本地占位封面排版使用。"""
    text = (text or "").strip()
    if not text:
        return ["游戏"]
    lines = []
    while text and len(lines) < maxl:
        lines.append(text[:n])
        text = text[n:]
    if text:
        lines[-1] = lines[-1][:n - 1] + "…"
    return lines


def fmt_duration(sec):
    """把秒数格式化为可读时长。"""
    if not sec or sec < 0:
        return None
    if sec < 60:
        return "不足 1 分钟"
    m = sec / 60.0
    if m < 60:
        return f"{int(m)} 分钟"
    h = m / 60.0
    if h < 10:
        return f"{h:.1f} 小时"
    return f"{int(h)} 小时"


def fmt_last(ts):
    try:
        return datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def _last_iso(ts):
    """把游玩时间统一成可排序的 ISO 字符串；兼容旧数据里的数字时间戳。"""
    if not ts:
        return ""
    if isinstance(ts, (int, float)):
        try:
            return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
        except Exception:
            return ""
    return str(ts)


def dir_size(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except Exception:
        pass
    return total


def find_exe(path):
    best, best_sz = None, -1
    try:
        for root, dirs, files in os.walk(path):
            depth = root[len(path):].count(os.sep)
            if depth > 2:
                dirs[:] = []
                continue
            for f in files:
                if not f.lower().endswith(".exe"):
                    continue
                low = f.lower()
                if any(k in low for k in EXE_SKIP):
                    continue
                p = os.path.join(root, f)
                try:
                    sz = os.path.getsize(p)
                except OSError:
                    continue
                if sz > best_sz:
                    best, best_sz = p, sz
    except Exception:
        pass
    return best


# ---------------- 回收站删除 ----------------
class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND),
                ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16),
                ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", wintypes.LPCWSTR)]


def recycle(path):
    """移入回收站，可恢复"""
    path = os.path.abspath(path)
    if len(path) <= 4:
        raise ValueError("拒绝操作根目录")
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = 3  # FO_DELETE
    op.pFrom = path + "\0\0"
    op.pTo = None
    op.fFlags = 0x40 | 0x10 | 0x400 | 0x4  # ALLOWUNDO | NOCONFIRMATION | NOERRORUI | SILENT
    res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if res != 0:
        raise OSError(f"删除失败，错误码 {res}")
    if op.fAnyOperationsAborted:
        raise OSError("操作已取消")
    return True


# ---------------- 配置 ----------------
def load_config():
    """读取配置；兼容旧版结构，并给所有字段安全的默认值。"""
    cfg = {
        "scan_roots": list(DEFAULT_ROOTS), "launch_overrides": {}, "play_stats": {},
        "web_theme": "dark", "favorites": [], "categories": [], "game_categories": {},
        "manual_games": [], "wallpaper": None, "shots": {}, "view": "grid",
        "close_action": "ask", "steam_libs": [],
        "geometry": None, "cover_version": 1,
    }
    try:
        c = STORE.get_config_raw()
        if not isinstance(c, dict):
            c = {}
        roots = c.get("scan_roots")
        if roots:
            out = []
            for r in roots:
                if isinstance(r, str):
                    out.append(r)
                elif isinstance(r, dict) and r.get("path"):
                    out.append(r["path"])
            cfg["scan_roots"] = [p for p in out if p]
        for k in ("launch_overrides", "play_stats", "game_categories", "shots"):
            if isinstance(c.get(k), dict):
                cfg[k] = c[k]
        for k in ("favorites", "categories", "manual_games", "steam_libs"):
            if isinstance(c.get(k), list):
                cfg[k] = c[k]
        if isinstance(c.get("wallpaper"), dict):
            cfg["wallpaper"] = c["wallpaper"]
        cfg["web_theme"] = "light" if c.get("web_theme") == "light" else "dark"
        cfg["view"] = "list" if c.get("view") == "list" else "grid"
        ca = c.get("close_action")
        cfg["close_action"] = ca if ca in ("exit", "tray", "ask") else "ask"
        try:
            cfg["cover_version"] = max(1, int(c.get("cover_version", 1)))
        except (ValueError, TypeError):
            cfg["cover_version"] = 1
        g = c.get("geometry")
        if isinstance(g, dict) and all(k in g for k in ("x", "y", "w", "h")):
            try:
                cfg["geometry"] = {k: int(g[k]) for k in ("x", "y", "w", "h")}
            except Exception:
                cfg["geometry"] = None
    except Exception:
        pass
    return cfg


def save_config(cfg):
    keys = ("scan_roots", "launch_overrides", "play_stats", "web_theme", "favorites",
            "categories", "game_categories", "manual_games", "wallpaper", "shots",
            "view", "close_action", "steam_libs", "geometry", "cover_version")
    try:
        STORE.put_config_raw({k: cfg.get(k) for k in keys})
    except Exception:
        pass


# ---------------- OTA 更新（单文件便携版自更新） ----------------
GITHUB_REPO = "zxc441521/GameBox"
GITHUB_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
PORTABLE_NAME = "GameBox-Portable-{tag}.exe"   # 单文件免安装版：更新只用它
SETUP_NAME = "GameBox-Setup-{tag}.exe"         # 仅作为信息兜底，不用于自动更新
# 可选更新源覆盖：指向一个 JSON（tag/ver/url/name/body），用于自建镜像或本地演练
UPDATE_MANIFEST_URL = os.environ.get("GV_UPDATE_MANIFEST_URL")


def _proxy_opener(proxy):
    return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))


# 依次尝试：直连 → 系统/环境代理 → 常见本地代理端口（Clash/V2Ray 等）
_GITHUB_OPENERS = [
    urllib.request.build_opener(),
    _proxy_opener("http://127.0.0.1:7890"),
    _proxy_opener("http://127.0.0.1:7897"),
    _proxy_opener("http://127.0.0.1:10809"),
]
# 直连放在最前：本机/内网更新源不该被环境代理劫持；外网失败会自动回退
_ALL_OPENERS = (urllib.request.build_opener(urllib.request.ProxyHandler({})),) + tuple(_GITHUB_OPENERS)


def _open_url(url, timeout=20):
    """依次尝试各 opener，返回 response；全部失败则抛最后一个异常。

    整体耗时以 timeout 为上限：每个 opener 只分到剩余时间（单次最多 8s），
    避免「直连挂满 20s + 三个代理各挂 20s」把一次检查拖到一分多钟。
    """
    req = urllib.request.Request(url, headers=UA)
    last = None
    deadline = time.time() + max(1.0, float(timeout))
    for opener in _ALL_OPENERS:
        left = deadline - time.time()
        if left <= 0.3:
            break
        try:
            return opener.open(req, timeout=min(left, 8.0))
        except Exception as e:
            last = e
    raise last or OSError("cannot open " + url)


def _web_fetch(url, timeout=10):
    """带代理回退地请求网页/接口，返回 bytes 或 None。"""
    try:
        return _open_url(url, timeout).read()
    except Exception:
        return None


def _dir_writable(path):
    """目录是否可写（安装到 Program Files 时通常不可写）。"""
    try:
        probe = os.path.join(path, ".gb_write_test")
        with open(probe, "w") as f:
            f.write("")
        _win_delete_file(probe)
        return True
    except Exception:
        return False


def _win_delete_file(path):
    """直接调用 Win32 DeleteFileW，避免被 Python 层的 os.remove 包装影响。

    某些环境（安全软件 / 注入的 sitecustomize）会把 os.remove 改成「移到回收站」，
    对正在被替换的 exe 备份可能失败；自更新清理需要一次确定性的删除。
    """
    if os.name != "nt":
        os.remove(path)
        return
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.DeleteFileW.argtypes = [wintypes.LPCWSTR]
    k.DeleteFileW.restype = wintypes.BOOL
    if not k.DeleteFileW(path):
        raise ctypes.WinError(ctypes.get_last_error())


def parse_version(v):
    """把版本字符串解析为 (int,int,int) 元组，便于比较新旧版本。"""
    v = (v or "").strip().lstrip("vV")
    nums = []
    for part in v.split("."):
        try:
            nums.append(int(part.split("-")[0].split("+")[0]))
        except ValueError:
            break
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def _parse_atom(raw_text):
    """解析 releases.atom，返回 (tag, title, body)。失败返回 (None, None, None)。"""
    try:
        root = ET.fromstring(raw_text)
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        entries = root.findall("atom:entry", ns)
        if not entries:
            return None, None, None
        e = entries[0]
        eid = e.findtext("atom:id", "", ns) or ""
        tag = eid.rsplit("/", 1)[-1]
        title = e.findtext("atom:title", "", ns) or ""
        content = e.findtext("atom:content", "", ns) or ""
        body = re.sub(r"<[^>]+>", "", html.unescape(content)).strip()
        return tag, title, body
    except Exception:
        return None, None, None


def _latest_from_atom():
    """从 Atom 订阅源获取最新版本（不占用 REST API 额度）。

    返回 None 表示拿不到订阅源（网络/代理问题）；返回 {"empty": True} 表示订阅源
    正常但一条 entry 都没有 —— 即仓库还没发布过 Release。两者要分开，才能给用户
    准确的「网络不通」还是「暂无发布」提示。
    """
    raw = _web_fetch(f"https://github.com/{GITHUB_REPO}/releases.atom", timeout=8)
    if not raw:
        return None
    tag, title, body = _parse_atom(raw.decode("utf-8", "replace"))
    if not tag:
        return {"empty": True}
    name = PORTABLE_NAME.format(tag=tag)
    base = f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/"
    return {
        "tag": tag,
        "ver": parse_version(tag),
        "url": base + name,
        "name": name,
        "setupUrl": base + SETUP_NAME.format(tag=tag),
        "body": body or title,
        "portable": True,
    }


def _latest_from_api():
    """从 REST API 获取最新版本资产（Atom 失败或拼名 URL 404 时回退）。"""
    raw = _web_fetch(GITHUB_LATEST, timeout=12)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        tag = data.get("tag_name") or ""
        portable = setup = None
        for a in data.get("assets") or []:
            n = a.get("name") or ""
            low = n.lower()
            if not low.endswith(".exe"):
                continue
            if "portable" in low:
                portable = a
            elif "setup" in low:
                setup = a
            elif portable is None:
                portable = a
        exe = portable or setup
        if not exe or not exe.get("browser_download_url"):
            return None
        return {
            "tag": tag,
            "ver": parse_version(tag),
            "url": exe.get("browser_download_url"),
            "name": exe.get("name"),
            "setupUrl": (setup or {}).get("browser_download_url"),
            "body": data.get("body") or "",
            "portable": portable is not None,
        }
    except Exception:
        return None


def _latest_from_manifest():
    """可选更新源：GV_UPDATE_MANIFEST_URL 指向的 JSON（自建镜像 / 端到端演练）。"""
    if not UPDATE_MANIFEST_URL:
        return None
    raw = _web_fetch(UPDATE_MANIFEST_URL, timeout=8)
    if not raw:
        return None
    try:
        d = json.loads(raw.decode("utf-8", "replace"))
        url = d.get("url") or ""
        if not url:
            return None
        tag = d.get("tag") or ""
        return {
            "tag": tag,
            "ver": parse_version(d.get("ver") or tag),
            "url": url,
            "name": d.get("name") or os.path.basename(urllib.parse.urlsplit(url).path),
            "setupUrl": d.get("setupUrl"),
            "body": d.get("body") or "",
            "portable": d.get("portable", True) is not False,
        }
    except Exception:
        return None


def check_latest_ex():
    """查询最新 Release，返回 (info, status)。

    status 取值：ok / none（仓库还没有 Release）/ noasset（有 Release 但没 exe 资产）/
    offline（网络不通或接口异常）。区分这几种情况是为了给用户准确的提示，而不是
    一律报「检查失败」。
    """
    m = _latest_from_manifest()
    if m and m.get("url"):
        return m, "ok"
    atom = _latest_from_atom()
    if atom and atom.get("empty"):
        return None, "none"
    if atom and atom.get("url"):
        return atom, "ok"
    info = _latest_from_api()
    if info and info.get("url"):
        return info, "ok"
    if atom is None:
        # 订阅源都取不到，说明 GitHub 不可达（不必再加一次探测请求拖长时间）
        return None, "offline"
    return None, "noasset"


def check_latest():
    """查询最新 Release，返回 {tag, ver, url, name, body, portable} 或 None。"""
    return check_latest_ex()[0]


def _idle_update_progress():
    return {"phase": "idle", "percent": 0, "downloaded": 0, "total": 0,
            "speed": 0, "eta": 0, "message": "", "ver": ""}


# ---------------- 在线封面服务（内存缓存，不落盘） ----------------
STEAM_CDN = "https://cdn.akamai.steamstatic.com/steam/apps/{appid}/{fn}"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?term={term}&l=schinese&cc=cn"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131 Safari/537.36"}


COVER_CACHE_DIR = os.path.join(_config_dir(), "cover_cache")


class CoverService(QObject):
    """后台下载 Steam 封面/头图；磁盘 + 内存双缓存，下次启动秒开。"""
    changed = pyqtSignal()

    def __init__(self, workers=6):
        super().__init__()
        self.images = {}       # appid -> QImage 竖版封面
        self.headers = {}      # appid -> QImage 横版头图（详情页 hero）
        self._queued = set()
        self._failed = set()   # 本次会话内下载失败的，避免每次重绘重复入队
        self._lock = threading.Lock()
        self._q = queue.Queue()
        try:
            os.makedirs(COVER_CACHE_DIR, exist_ok=True)
        except Exception:
            pass
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()

    def _cache_path(self, typ, key):
        return os.path.join(COVER_CACHE_DIR,
                            ("h_" if typ == "header" else "c_") + str(key) + ".jpg")

    def _from_disk(self, typ, key):
        p = self._cache_path(typ, key)
        if not os.path.isfile(p):
            return None
        try:
            img = QImage(p)
            return img if not img.isNull() else None
        except Exception:
            return None

    def _to_disk(self, typ, key, raw):
        try:
            dst = self._cache_path(typ, key)
            tmp = dst + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, dst)
        except Exception:
            pass

    def _store(self, typ):
        return self.headers if typ == "header" else self.images

    def _get(self, typ, key):
        store = self._store(typ)
        with self._lock:
            img = store.get(key)
        if img is None:
            img = self._from_disk(typ, key)
            if img is not None:
                with self._lock:
                    store[key] = img
        return img

    def get_image(self, key):
        return self._get("cover", key)

    def get_header(self, key):
        return self._get("header", key)

    def request(self, key, appid):
        self._enqueue("cover", key, appid)

    def request_header(self, key, appid):
        self._enqueue("header", key, appid)

    def reset_memory(self):
        """清缓存后重置内存态，允许同一会话内重新下载（磁盘文件已删除）。"""
        with self._lock:
            self.images.clear()
            self.headers.clear()
            self._queued.clear()
            self._failed.clear()

    def _enqueue(self, typ, key, appid):
        k = (typ, key)
        with self._lock:
            if k in self._queued or k in self._failed:
                return
            if key in self._store(typ):
                return
            self._queued.add(k)
        if os.path.isfile(self._cache_path(typ, key)):
            with self._lock:
                self._queued.discard(k)
            return
        self._q.put((typ, key, appid))

    def _worker(self):
        while True:
            typ, key, appid = self._q.get()
            img = self._download(typ, key, appid)
            with self._lock:
                self._store(typ)[key] = img
                self._queued.discard((typ, key))
                if img is None or img.isNull():
                    self._failed.add((typ, key))
            try:
                self.changed.emit()
            except RuntimeError:
                pass  # 退出时 QObject 已析构，忽略后台线程的尾包
            self._q.task_done()

    def _download(self, typ, key, appid):
        fns = ("header.jpg",) if typ == "header" \
            else ("library_600x900_2x.jpg", "library_600x900.jpg", "header.jpg")
        for fn in fns:
            try:
                req = urllib.request.Request(STEAM_CDN.format(appid=appid, fn=fn), headers=UA)
                raw = urllib.request.urlopen(req, timeout=20).read()
                if len(raw) < 8000:
                    continue
                img = QImage()
                if img.loadFromData(raw):
                    self._to_disk(typ, key, raw)
                    return img
            except Exception:
                continue
        return None


_COVER_SVC = None


def cover_service():
    global _COVER_SVC
    if _COVER_SVC is None:
        _COVER_SVC = CoverService()
    return _COVER_SVC


def resolve_steam(name):
    """通过 Steam 商店搜索，返回 (appid, 中文名)。失败返回 (None, None)。"""
    url = STEAM_SEARCH.format(term=urllib.parse.quote(name))
    raw = _web_fetch(url, timeout=10)
    if not raw:
        return None, None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
        items = data.get("items") or []
        if not items:
            return None, None
        it = items[0]
        return it.get("id"), (it.get("name") or None)
    except Exception:
        return None, None


# ---------------- 数据扫描 ----------------
def _meta_candidates():
    """评分/资料库候选路径：优先用户可编辑的外部文件，回退到 exe 内置副本。

    PyInstaller 单文件版会把 data/games.json 解到 sys._MEIPASS；若只读
    APP_DIR/data/games.json，用户把 exe 放到任意目录时就会读不到评分库，
    导致所有游戏 score=None / tier=暂无收录。
    """
    cands = [os.path.join(DATA_DIR, "games.json"),
             os.path.join(APP_DIR, "data", "games.json")]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        cands.append(os.path.join(sys._MEIPASS, "data", "games.json"))
        cands.append(os.path.join(sys._MEIPASS, "games.json"))
    seen, out = set(), []
    for p in cands:
        ap = os.path.abspath(p)
        if ap not in seen:
            seen.add(ap)
            out.append(ap)
    return out


def load_meta():
    """加载游戏元数据（评分/评分档/年份等），来自 SQLite 的 game_meta 表。

    外部 data/games.json 在 Store 初始化时同步进库；文件缺失时 DB 副本仍有效，
    这正是「评分库漏打包 → 全库无分」的结构性兜底。
    """
    return STORE.load_meta()


def scan_games(roots=None):
    roots = roots or list(DEFAULT_ROOTS)
    meta = load_meta()
    items, seen = [], set()
    root_set = {os.path.abspath(r).lower() for r in roots}
    for root in roots:
        if not os.path.isdir(root):
            continue
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            full = os.path.join(root, name)
            if not os.path.isdir(full):
                continue
            low = name.lower()
            if low in EXCLUDE or low.startswith("found.") or low.startswith("$"):
                continue
            ap = os.path.abspath(full).lower()
            if ap in root_set or ap == os.path.abspath(APP_DIR).lower():
                continue
            if full.lower() in seen:
                continue
            seen.add(full.lower())
            m = meta.get(name)
            if m:
                it = dict(m)
                it["known"] = True
            else:
                it = {"folder": name, "cn": name, "en": name, "year": 0, "score": None,
                      "tier": "暂无收录", "reviews": None, "plat": "-",
                      "note": "等待在线识别", "known": False, "appid": None}
            it["path"] = full
            it["group"] = root_label(root)
            it["_root"] = root
            items.append(it)
    items.sort(key=lambda x: (-(x.get("score") or -1), x["cn"]))
    return items


# ---------------- 数据模型 ----------------

# ==================== 游匣 GameBox · 后端核心 ====================
AUTHOR_NAME = "zxc441521"
AUTHOR_URL = "https://github.com/" + AUTHOR_NAME
REPO_URL = "https://github.com/" + GITHUB_REPO
APP_EN = "GameBox"

BASE_DIR = _config_dir()
MEDIA_DIR = os.path.join(BASE_DIR, "media")
WALL_DIR = os.path.join(MEDIA_DIR, "wallpapers")
SHOT_DIR = os.path.join(MEDIA_DIR, "shots")
for _d in (MEDIA_DIR, WALL_DIR, SHOT_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except Exception:
        pass

# 本地缓存：游戏列表与 Steam 简介，用于加速下次启动与离线补全
GAMES_CACHE = os.path.join(BASE_DIR, "games_cache.json")
META_CACHE = os.path.join(BASE_DIR, "meta_cache.json")


def _migrate_legacy():
    """从旧数据目录迁移配置与封面缓存，避免升级时丢失扫描路径与游玩记录。"""
    try:
        need_cfg = not os.path.isfile(CONFIG_PATH)
        dst_cache = os.path.join(BASE_DIR, "cover_cache")
        need_cache = not os.path.isdir(dst_cache)
        if not need_cfg and not need_cache:
            return
        appdata = os.environ.get("APPDATA", "")
        cands = [
            os.path.join(appdata, "游戏收藏夹"),
            os.path.join(appdata, "GameVault"),
            os.path.join(os.path.dirname(APP_DIR), "GameVault"),
        ]
        for old in cands:
            if not os.path.isdir(old):
                continue
            if need_cfg:
                src = os.path.join(old, "config.json")
                if os.path.isfile(src):
                    shutil.copy2(src, CONFIG_PATH)
            if need_cache:
                src_cache = os.path.join(old, "cover_cache")
                if os.path.isdir(src_cache):
                    shutil.copytree(src_cache, dst_cache)
            if os.path.isfile(CONFIG_PATH) and os.path.isdir(dst_cache):
                _gv_log("migrated existing data from %s" % old)
                return
    except Exception as e:
        _gv_log("migrate legacy failed: %r" % (e,))


_migrate_legacy()

# ---------------- 单一数据源（SQLite） ----------------
# 旧版 config.json / games_cache.json / meta_cache.json 在 Store 初始化时幂等导入，
# 文件本身保留为备份；此后所有读写只走 gamebox.db。评分库 data/games.json 也会
# 落库，即使某次构建漏打包该文件，之前导入的评分仍可正常显示。
DB_PATH = os.path.join(BASE_DIR, "gamebox.db")
STORE = store.Store(
    DB_PATH,
    meta_candidates=_meta_candidates(),
    legacy={"config": CONFIG_PATH, "games": GAMES_CACHE, "steam": META_CACHE},
    log=_gv_log,
)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_NAME = "GameBox"


def autostart_enabled():
    if not winreg:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            v, _ = winreg.QueryValueEx(k, _RUN_NAME)
            return bool(v)
    except Exception:
        return False


def set_autostart(on):
    if not winreg:
        return False
    try:
        cmd = '"%s"' % sys.executable
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_WRITE) as k:
            if on:
                winreg.SetValueEx(k, _RUN_NAME, 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(k, _RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        _gv_log("set_autostart failed: %r" % (e,))
        return False


MEDIA_MIME = {
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
}


def media_file(rel):
    """把 /api/media?f= 限制在 MEDIA_DIR 内，防止任意文件读取。"""
    try:
        p = os.path.abspath(os.path.join(MEDIA_DIR, str(rel).replace("\\", "/")))
        base = os.path.abspath(MEDIA_DIR)
        if p.startswith(base + os.sep) and os.path.isfile(p):
            return p
    except Exception:
        pass
    return None


def import_media(src, subdir):
    """把用户选择的文件复制进媒体目录，返回相对路径。"""
    ext = os.path.splitext(src)[1].lower()
    tag = hashlib.md5((src + str(time.time())).encode("utf-8", "ignore")).hexdigest()[:8]
    rel = "%s/%d_%s%s" % (subdir, int(time.time() * 1000), tag, ext)
    dst = os.path.join(MEDIA_DIR, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return rel


def steam_libraries():
    """定位本机所有 Steam 库目录（注册表 + 常见盘符 + libraryfolders.vdf）。"""
    cands = []
    if winreg:
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
                v, _ = winreg.QueryValueEx(k, "SteamPath")
                cands.append(v)
        except Exception:
            pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        p = os.environ.get(env)
        if p:
            cands.append(os.path.join(p, "Steam"))
    for d in "CDEFGH":
        cands += [d + ":\\Steam", d + ":\\SteamLibrary"]
    base = []
    for c in cands:
        try:
            if os.path.isfile(os.path.join(c, "steamapps", "libraryfolders.vdf")) \
                    and os.path.abspath(c).lower() not in [os.path.abspath(x).lower() for x in base]:
                base.append(c)
        except Exception:
            pass
    libs = list(base)
    for c in base:
        try:
            with open(os.path.join(c, "steamapps", "libraryfolders.vdf"),
                      encoding="utf-8", errors="ignore") as f:
                txt = f.read()
        except Exception:
            continue
        for p in re.findall(r'"path"\s+"([^"]+)"', txt):
            p = p.replace("\\\\", "\\")
            if os.path.isdir(p) and os.path.abspath(p).lower() not in \
                    [os.path.abspath(x).lower() for x in libs]:
                libs.append(p)
    return libs


def steam_manifest_map(libs):
    """读 appmanifest_*.acf，得到 目录名 -> {appid, name}，用于封面与中文名识别。"""
    out = {}
    for lib in libs:
        try:
            files = glob.glob(os.path.join(lib, "steamapps", "appmanifest_*.acf"))
        except Exception:
            continue
        for fp in files:
            try:
                with open(fp, encoding="utf-8", errors="ignore") as f:
                    txt = f.read()
            except Exception:
                continue
            a = re.search(r'"appid"\s+"(\d+)"', txt)
            n = re.search(r'"name"\s+"([^"]+)"', txt)
            d = re.search(r'"installdir"\s+"([^"]+)"', txt)
            if not a:
                continue
            key = (d.group(1) if d else (n.group(1) if n else "")).replace("\\\\", "\\")
            if key:
                out[os.path.basename(key).lower()] = {
                    "appid": int(a.group(1)), "name": n.group(1) if n else ""}
    return out


# ---------------- 本地 HTTP 桥接 ----------------
class WebBridgeServer(threading.Thread):
    """只监听 127.0.0.1：静态界面 + JSON API + 封面/媒体代理。"""

    def __init__(self, win):
        super().__init__(daemon=True)
        self.win = win
        self.port = None
        self.httpd = None
        self.ready = threading.Event()

    def run(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._get(self)

            def do_POST(self):
                outer._post(self)

        class QuietHTTPServer(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):
                # 浏览器关闭页面时的连接重置属于正常断连，不需要堆栈噪音
                import sys as _sys
                err = _sys.exc_info()[1]
                if isinstance(err, (ConnectionResetError, ConnectionAbortedError,
                                    BrokenPipeError)):
                    return
                _gv_log("http error: %r" % (err,))

        try:
            self.httpd = QuietHTTPServer(("127.0.0.1", 0), Handler)
            self.port = self.httpd.server_address[1]
            try:
                with open(BRIDGE_PORT_FILE, "w", encoding="utf-8") as f:
                    f.write(str(self.port))
            except Exception:
                pass
            _gv_log("server listening port=%s" % self.port)
        except Exception as e:
            _gv_log("server FAILED: %r" % (e,))
            self.ready.set()
            return
        self.ready.set()
        try:
            self.httpd.serve_forever()
        except Exception as e:
            _gv_log("serve_forever stopped: %r" % (e,))

    @staticmethod
    def _send(h, code, body, mime="application/json; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        h.send_response(code)
        h.send_header("Content-Type", mime)
        h.send_header("Content-Length", str(len(body)))
        extras = dict(extra or {})
        # 允许调用方覆盖缓存策略：封面等静态资源用长缓存，避免每次刷新重新拉取
        h.send_header("Cache-Control", extras.pop("Cache-Control", "no-store"))
        for k, v in extras.items():
            h.send_header(k, v)
        h.end_headers()
        try:
            h.wfile.write(body)
        except Exception:
            pass

    def _get(self, h):
        u = urllib.parse.urlsplit(h.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        w = self.win
        if path == "/api/games":
            try:
                return self._send(h, 200, json.dumps(w._web_payload(), ensure_ascii=False))
            except Exception as e:
                return self._send(h, 200, json.dumps({"error": str(e)}))
        if path == "/api/settings":
            try:
                return self._send(h, 200, json.dumps(w._settings_payload(), ensure_ascii=False))
            except Exception as e:
                return self._send(h, 200, json.dumps({"error": str(e)}))
        if path == "/api/theme":
            return self._send(h, 200, json.dumps({"theme": w.config.get("web_theme", "dark")}))
        if path == "/api/update-progress":
            return self._send(h, 200, json.dumps(w.update_progress, ensure_ascii=False))
        if path == "/api/ping":
            return self._send(h, 200, '{"ok":true}')
        if path == "/api/show":
            self.win.ui_call.emit(lambda: self.win.show_ui())
            return self._send(h, 200, '{"ok":true}')
        if path == "/api/wingetpos":
            hwnd = self.win.shell.main_hwnd()
            if hwnd and _user32:
                r = wintypes.RECT()
                _user32.GetWindowRect(hwnd, ctypes.byref(r))
                return self._send(h, 200, json.dumps(
                    {"x": r.left, "y": r.top,
                     "w": r.right - r.left, "h": r.bottom - r.top}))
            return self._send(h, 200, json.dumps({"x": 0, "y": 0, "w": 0, "h": 0}))
        if path == "/api/winsc":
            cmd = (q.get("cmd") or ["minimize"])[0]
            try:
                self.win.shell.win_command(cmd)
            except Exception as e:
                _gv_log("winsc %s failed: %r" % (cmd, e))
            return self._send(h, 200, '{"ok":true}')
        if path == "/api/notify":
            return self._send(h, 200, json.dumps(
                {"messages": w.take_notices()}, ensure_ascii=False))
        if path == "/api/cover":
            appid = (q.get("appid") or [""])[0]
            typ = (q.get("type") or ["cover"])[0]
            name = (q.get("name") or [""])[0]
            en = (q.get("en") or [""])[0]
            key = (q.get("key") or [""])[0]
            grp = (q.get("group") or [""])[0]
            # Steam 官方 CDN：优先读本地磁盘缓存（长缓存，避免每次刷新重新拉取）
            if appid.isdigit():
                aid = int(appid)
                fn = ("h_" if typ == "header" else "c_") + str(aid) + ".jpg"
                fp = os.path.join(COVER_CACHE_DIR, fn)
                if os.path.isfile(fp) and os.path.getsize(fp) >= 8000:
                    try:
                        with open(fp, "rb") as f:
                            return self._send(h, 200, f.read(), "image/jpeg",
                                              {"Cache-Control":
                                               "public, max-age=31536000, immutable"})
                    except Exception:
                        pass
                # 本地未命中：把该 appid 交给后台下载队列落盘（同步完成后即缓存到
                # 本地），本次仍 302 让浏览器直连 CDN 即时显示，不必等待下载。
                try:
                    svc = cover_service()
                    if typ == "header":
                        svc.request_header(str(aid), aid)
                    else:
                        svc.request(str(aid), aid)
                except Exception:
                    pass
                cdn = STEAM_CDN.format(appid=aid,
                                       fn="header.jpg" if typ == "header"
                                       else "library_600x900.jpg")
                return self._send(h, 302, b"", "text/plain", {"Location": cdn})
            # 缺省处理：无 AppID 时直接给本地生成的 SVG 占位图，
            #    绝不返回 404 / 坏图；前端的 onerror 回退仍作为第二道保险。
            raw = w.generate_cover(name or "游戏", grp, key or name or "fallback")
            if raw:
                return self._send(h, 200, raw, "image/svg+xml")
            return self._send(h, 404, b"", "text/plain")
        if path == "/api/icon":
            fp = os.path.join(DATA_DIR, "icon.png")
            if os.path.isfile(fp):
                try:
                    with open(fp, "rb") as f:
                        return self._send(h, 200, f.read(), "image/png")
                except Exception:
                    pass
            return self._send(h, 404, b"", "text/plain")
        if path == "/api/cover-fallback":
            raw = self.win.generate_cover(
                (q.get("name") or [""])[0], (q.get("group") or [""])[0],
                (q.get("seed") or [""])[0])
            if raw:
                return self._send(h, 200, raw, "image/svg+xml")
            return self._send(h, 404, b"", "text/plain")
        if path == "/api/game-detail":
            p = (q.get("path") or [""])[0]
            try:
                data = self.win.game_detail(p)
                return self._send(h, 200, json.dumps(data, ensure_ascii=False))
            except Exception as e:
                return self._send(h, 200, json.dumps({"error": str(e)}))
        if path == "/api/media":
            fp = media_file((q.get("f") or [""])[0])
            if not fp:
                return self._send(h, 404, b"", "text/plain")
            mime = MEDIA_MIME.get(os.path.splitext(fp)[1].lower(), "application/octet-stream")
            size = os.path.getsize(fp)
            rng = h.headers.get("Range")
            try:
                if rng and rng.startswith("bytes="):
                    part = rng[6:].split(",")[0].strip().split("-")
                    start = int(part[0]) if part[0] else 0
                    end = int(part[1]) if len(part) > 1 and part[1] else size - 1
                    end = min(end, size - 1)
                    with open(fp, "rb") as f:
                        f.seek(start)
                        data = f.read(end - start + 1)
                    return self._send(h, 206, data, mime, {
                        "Content-Range": "bytes %d-%d/%d" % (start, end, size),
                        "Accept-Ranges": "bytes"})
                with open(fp, "rb") as f:
                    return self._send(h, 200, f.read(), mime, {"Accept-Ranges": "bytes"})
            except Exception:
                return self._send(h, 500, b"", "text/plain")
        name = "index.html" if path in ("", "/") else path.lstrip("/").replace("..", "")
        fp = os.path.join(WEBUI_DIR, name)
        if not os.path.isfile(fp):
            return self._send(h, 404, "not found", "text/plain")
        try:
            with open(fp, "rb") as f:
                data = f.read()
        except Exception:
            return self._send(h, 500, "error", "text/plain")
        mime = ("text/html; charset=utf-8" if fp.endswith(".html") else
                "text/javascript; charset=utf-8" if fp.endswith(".js") else
                "text/css; charset=utf-8" if fp.endswith(".css") else
                "application/octet-stream")
        return self._send(h, 200, data, mime)

    FILE_ACTIONS = frozenset(("chooseLaunch", "pickShots", "pickRoot",
                              "pickGame", "pickWallpaper"))

    def _post(self, h):
        path = urllib.parse.urlsplit(h.path).path
        try:
            n = int(h.headers.get("Content-Length", "0") or 0)
            raw = h.rfile.read(n) if n else b""
        except Exception as e:
            return self._send(h, 400, json.dumps({"error": str(e)}))

        # 窗口尺寸/位置：收到即刻落盘，下次启动恢复（来自网页 sendBeacon / fetch）
        if path == "/api/geometry":
            try:
                d = json.loads(raw.decode("utf-8") or "{}")
                g = d.get("geometry") or d
            except Exception:
                g = {}
            # 优先用 Win32 GetWindowRect 实测外框：网页的 screenX/outerWidth 是
            # CSS 逻辑像素，在高 DPI 缩放下与 SetWindowPos 的物理像素不一致。
            hwnd = None
            try:
                hwnd = self.win.shell._strict_hwnd()
            except Exception:
                pass
            if hwnd:
                self.win._save_geometry_from_window(hwnd)   # 内部已落盘
            elif all(k in g for k in ("x", "y", "w", "h")):
                try:
                    self.win.config["geometry"] = {
                        k: int(g[k]) for k in ("x", "y", "w", "h")}
                    save_config(self.win.config)
                except (ValueError, TypeError):
                    pass
            return self._send(h, 200, '{"ok":true}')

        # 页面内自定义标题栏拖动 / 缩放：直接移动窗口，缺失维度从当前矩形继承
        if path == "/api/winpos":
            try:
                d = json.loads(raw.decode("utf-8") or "{}")
                self.win.shell.set_window_rect(
                    int(d.get("x", -1)), int(d.get("y", -1)),
                    int(d.get("w", -1)), int(d.get("h", -1)))
            except Exception as e:
                _gv_log("winpos failed: %r" % (e,))
            return self._send(h, 200, '{"ok":true}')

        if path != "/api/action":
            return self._send(h, 404, "not found", "text/plain")
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
            action = str(body.get("action", ""))
            value = body.get("value", "")
        except Exception as e:
            return self._send(h, 400, json.dumps({"error": str(e)}))

        # 文件对话框需等用户选完再响应，网页遮罩与窗口禁用才可同步解除
        if action in self.FILE_ACTIONS:
            done = threading.Event()

            def run():
                try:
                    self.win._web_action(action, value)
                finally:
                    done.set()

            self.win.ui_call.emit(run)
            done.wait(180)
        else:
            self.win.ui_call.emit(lambda: self.win._web_action(action, value))
        return self._send(h, 200, '{"ok":true}')


class WebShell(QObject):
    """用系统 Edge / Chrome 的 --app 无地址栏窗口承载网页界面。"""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.server = None
        self.proc = None
        self.hidden = False
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._watch)
        self._profile = os.environ.get("GV_TEST_PROFILE") or os.path.join(tempfile.gettempdir(), "GameBoxWebShell")
        self._geo_pending = None    # 启动后待恢复的窗口外框 (x, y, w, h)
        self._geo_tries = 0
        self._geo_applied = 0

    def main_hwnd(self):
        pid = self.proc.pid if self.proc is not None else None
        return browser_main_hwnd(pid)

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            if self.hidden:
                hwnd = self.main_hwnd()
                if hwnd:
                    win_show(hwnd, True)
                    win_foreground(hwnd)
                    self.hidden = False
            return True
        if self.server is None:
            self.server = WebBridgeServer(self.win)
            self.server.start()
            self.server.ready.wait(5)
        if not self.server.port:
            return False
        browser = find_browser()
        if not browser:
            _gv_log("start: no browser found")
            return False
        url = "http://127.0.0.1:%d/index.html" % self.server.port
        geo = self.win.config.get("geometry") if self.win else None
        self._geo_pending = None
        self._geo_tries = 0
        self._geo_applied = 0
        if isinstance(geo, dict) and all(k in geo for k in ("x", "y", "w", "h")):
            try:
                g = tuple(int(geo[k]) for k in ("x", "y", "w", "h"))
                if g[2] >= 200 and g[3] >= 200:
                    self._geo_pending = g
            except (ValueError, TypeError):
                pass
        # 有记录时先按目标尺寸启动，窗口出现后再用 SetWindowPos 精确校正外框
        size = ("%d,%d" % (self._geo_pending[2], self._geo_pending[3])
                if self._geo_pending else "1440,900")
        args = [browser, "--app=" + url, "--window-size=" + size,
                "--user-data-dir=" + self._profile,
                "--no-first-run", "--no-default-browser-check",
                "--disable-sync", "--disable-account-consistency",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-features=msEdgeSidebarV2,msImplicitSignin,msEdgeSignIn"]
        try:
            # 上一次异常退出若留下自己的浏览器外壳，先按 pid 精确关掉，给新窗口腾出
            # profile 锁；只会命中本程序记录的进程，不会碰用户的浏览器。
            _close_previous_shell()
            self.proc = subprocess.Popen(args)
            self.hidden = False
            _gv_log("start: browser pid=%s port=%s" % (self.proc.pid, self.server.port))
        except Exception as e:
            _gv_log("start: Popen FAILED %r" % (e,))
            return False
        # 归属关系交给系统：宿主消失时由 Job 一并结束该浏览器进程（不再扫描/强杀窗口）
        attach_browser_to_job(self.proc)
        try:
            with open(SHELL_PID_FILE, "w", encoding="utf-8") as f:
                f.write(str(self.proc.pid))
        except Exception:
            pass
        self._timer.start()
        return True

    def _forget_pid_file(self):
        try:
            os.remove(SHELL_PID_FILE)
        except Exception:
            pass

    def stop(self):
        self._timer.stop()
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None
        self.hidden = False
        self._forget_pid_file()

    def _strict_hwnd(self):
        """只按本进程 pid 找窗口；不做全局标题回退，避免误动其它实例的窗口。"""
        if not _user32 or self.proc is None:
            return None
        wins = [w for w in enum_top_windows(self.proc.pid, include_hidden=True) if w[3]]
        if not wins:
            return None
        titled = [w for w in wins if any(k in w[2] for k in BROWSER_TITLE_KEYS)]
        return max(titled or wins, key=lambda w: w[1])[0]

    def _apply_saved_geometry(self, hwnd):
        """用 SetWindowPos 精确还原外框；与 _save_geometry_from_window 的
        GetWindowRect 同坐标系。Edge 会在窗口创建后异步应用 --window-size，
        因此在启动后的一段窗口期内持续校正，直到实际矩形稳定。"""
        x, y, w, h = self._geo_pending
        try:
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            cur = (r.left, r.top, r.right - r.left, r.bottom - r.top)
            if all(abs(a - b) <= 8 for a, b in zip(cur, (x, y, w, h))):
                self._geo_pending = None
                _gv_log("window geometry settled %s" % (cur,))
                return
            ok = _user32.SetWindowPos(hwnd, None, x, y, w, h,
                                      0x0004 | 0x0010)   # SWP_NOZORDER | SWP_NOACTIVATE
            self._geo_applied += 1
            _gv_log("restored window geometry %s ok=%s from=%s" %
                    ((x, y, w, h), bool(ok), cur))
            if self._geo_applied >= 12:      # 3s 窗口期后停止，避免长期干预用户操作
                self._geo_pending = None
        except Exception as e:
            _gv_log("restore geometry failed: %r" % (e,))
            self._geo_pending = None

    def win_command(self, cmd):
        """页面内窗口按钮：minimize / maximize / restore。"""
        hwnd = self.main_hwnd()
        if not hwnd or not _user32:
            return
        try:
            if cmd == "minimize":
                _user32.ShowWindow(hwnd, 6)        # SW_MINIMIZE
            elif cmd == "maximize":
                _user32.ShowWindow(hwnd, 3 if not _user32.IsZoomed(hwnd) else 9)
            elif cmd == "restore":
                _user32.ShowWindow(hwnd, 9)        # SW_RESTORE
        except Exception as e:
            _gv_log("win_command %s failed: %r" % (cmd, e))

    def set_window_rect(self, x, y, w, h):
        """移动 / 缩放窗口；传入 -1 的维度从当前矩形继承，避免覆盖未知值。"""
        hwnd = self.main_hwnd()
        if not hwnd or not _user32:
            return
        try:
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            cx, cy, cw, ch = r.left, r.top, r.right - r.left, r.bottom - r.top
            if w is None or w < 0:
                w = cw
            if h is None or h < 0:
                h = ch
            if x is None or x < 0:
                x = cx
            if y is None or y < 0:
                y = cy
            _user32.SetWindowPos(hwnd, None, int(x), int(y), int(w), int(h),
                                 0x0004 | 0x0010)   # SWP_NOZORDER | SWP_NOACTIVATE
        except Exception as e:
            _gv_log("set_window_rect failed: %r" % (e,))

    def _watch(self):
        if self.proc is None:
            self._timer.stop()
            return
        if self.proc.poll() is not None:
            self._timer.stop()
            self.proc = None
            self._forget_pid_file()
            self.win.on_window_closed()
            return
        if self._geo_pending:
            self._geo_tries += 1
            hwnd = self._strict_hwnd()
            if hwnd:
                self._apply_saved_geometry(hwnd)
            elif self._geo_tries > 80:      # 20s 内未出现窗口，放弃避免空转
                _gv_log("give up restoring geometry: window not found")
                self._geo_pending = None


# ---------------- 应用核心（纯后端控制器） ----------------
class AppCore(QObject):
    ui_call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.ui_call.connect(self._dispatch_ui)
        self.config = load_config()
        self.all_items = []
        self.last_scan = None
        self._scanning = False
        self._notices = []
        self._quitting = False
        self._cover_version = int(self.config.get("cover_version", 1) or 1)
        self._steam_map = {}
        self.update_info = None
        # 单文件自更新进度：前端每 250ms 轮询 /api/update-progress
        self.update_progress = _idle_update_progress()
        self._update_ver = ""
        self._update_busy = False
        self.tray = None
        self._sessions = {}        # path -> 启动时间戳，用于「正在游玩」实时计时
        self.shell = WebShell(self)
        self._build_tray()
        self._warm_from_cache()     # 先用本地缓存秒开界面，后台再重新扫描刷新
        QTimer.singleShot(60, self.rescan)
        QTimer.singleShot(150, self._start_ui)
        QTimer.singleShot(5000, lambda: self.check_for_updates(False))

    def _dispatch_ui(self, fn):
        try:
            fn()
        except Exception as e:
            _gv_log("ui dispatch error: %r" % (e,))

    def _notify(self, msg):
        self._notices.append(str(msg))
        if len(self._notices) > 30:
            del self._notices[:-30]

    def take_notices(self):
        out, self._notices = self._notices, []
        return out

    # ---------- 托盘 ----------
    def _build_tray(self):
        try:
            if not QSystemTrayIcon.isSystemTrayAvailable():
                return
            ip = os.path.join(DATA_DIR, "icon.png")
            icon = QIcon(ip) if os.path.isfile(ip) else \
                QApplication.style().standardIcon(QStyle.SP_ComputerIcon)
            self.tray = QSystemTrayIcon(icon, self)
            self.tray.setToolTip("%s v%s" % (APP_NAME, VERSION))
            menu = QMenu()
            self._tray_menu = menu          # 保持 Python 引用，避免菜单被回收
            a1 = menu.addAction("显示主界面")
            a1.triggered.connect(lambda: self.show_ui())
            a2 = menu.addAction("重新扫描")
            a2.triggered.connect(lambda: self.rescan())
            menu.addSeparator()
            a3 = menu.addAction("退出")
            a3.triggered.connect(lambda: self.quit_app())
            self.tray.setContextMenu(menu)
            self.tray.activated.connect(self._tray_activated)
            self.tray.show()
        except Exception as e:
            _gv_log("tray init failed: %r" % (e,))
            self.tray = None

    def _tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.show_ui()

    def _start_ui(self):
        if not self.shell.start():
            QMessageBox.warning(None, APP_NAME,
                                "未找到 Microsoft Edge 或 Chrome。\n\n"
                                "游匣的界面由系统浏览器的无地址栏窗口承载，"
                                "请安装 Edge 或 Chrome 后重新启动程序。")
            QApplication.instance().quit()

    # ---------- 关闭行为 ----------
    # 无边框模式：系统标题栏已在启动后去除，X 由页面内自定义标题栏承载。点 ✕ 只弹
    # 页面内三选一询问框（不关窗）；退出/托盘/取消均由页面经 /api 通知宿主执行。
    # 这里仅保留「最小化到托盘」的宿主侧逻辑，Alt+F4 等系统级关闭走 on_window_closed 兜底。
    def _to_tray(self):
        """最小化到托盘：只隐藏窗口、不退出进程，后台继续记录游玩时长。"""
        hwnd = self.shell.main_hwnd()
        alive = bool(hwnd) and self.shell.proc is not None and self.shell.proc.poll() is None
        if self.tray is None:
            # 没有托盘就重新打开界面，而不是静默退出
            if alive and hwnd:
                win_show(hwnd, True)
                win_foreground(hwnd)
            else:
                self.shell.start()
            return
        if alive and hwnd:
            self._save_geometry_from_window(hwnd)   # 收进托盘前记录窗口位置
            win_show(hwnd, False)      # 只隐藏，不销毁进程，托盘恢复无需重建
        self.shell.hidden = True
        self._tray_hint()

    def _save_geometry_from_window(self, hwnd):
        if not hwnd or not _user32:
            return
        try:
            r = wintypes.RECT()
            _user32.GetWindowRect(hwnd, ctypes.byref(r))
            self.config["geometry"] = {"x": int(r.left), "y": int(r.top),
                                       "w": int(r.right - r.left),
                                       "h": int(r.bottom - r.top)}
            save_config(self.config)
        except Exception:
            pass

    def _tray_hint(self):
        if self.tray is not None:
            self.tray.showMessage(APP_NAME, "游匣已在后台运行，双击托盘图标恢复界面",
                                 QSystemTrayIcon.Information, 3000)
            return True
        self._notify("系统托盘不可用，界面已重新打开")
        if self.shell.hidden:
            hwnd = self.shell.main_hwnd()
            if hwnd:
                win_show(hwnd, True)
                win_foreground(hwnd)
            self.shell.hidden = False
        else:
            self.shell.start()
        return False

    def on_window_closed(self):
        """系统标题栏下点 ✕ / Alt+F4 关闭界面窗口时，按设置里的关闭行为执行。

        - ask（默认）：弹出三选一询问框（退出程序 / 最小化到托盘 / 取消）；
        - exit：直接退出程序；
        - tray：界面关闭但进程驻留后台继续计时，双击托盘图标可恢复界面。
        本方法运行在 GUI 线程（由 _watch 定时回调触发），可安全弹出 Qt 模态框。
        """
        if self._quitting:
            return
        act = self.config.get("close_action", "ask")
        if act == "ask":
            _gv_log("window closed: ask mode -> show confirm dialog")
            self._ask_close()
            return
        if act == "tray":
            if self.tray is not None:
                _gv_log("window closed: keep running in tray")
                self.shell.hidden = True
                self._tray_hint()
                return
            # 托盘不可用时不让程序静默消失，退回重新打开界面
            _gv_log("window closed: tray unavailable, reopening UI")
            self.shell.start()
            self._notify("系统托盘不可用，界面已重新打开")
            return
        _gv_log("window closed: exiting app")
        self.quit_app()

    def _ask_close(self):
        """关闭询问框：退出程序 / 最小化到托盘 / 取消，可选「下次不再询问」。

        由于 ✕ 是系统标题栏，窗口已先行关闭，故点「取消」时重新拉起界面。
        勾选「下次不再询问」后，本次选择写入 close_action，下次按该行为直接执行、不再弹框；
        点「取消」则不记住选择。
        """
        mb = QMessageBox(None)
        mb.setIcon(QMessageBox.Question)
        mb.setWindowTitle("关闭游匣")
        mb.setText("您想如何关闭游匣？")
        mb.setInformativeText("退出程序会完全关闭；最小化到托盘后仍在后台运行并记录游玩时长；取消则重新打开界面。")
        chk = QCheckBox("下次不再询问")
        mb.setCheckBox(chk)
        btn_exit = mb.addButton("退出程序", QMessageBox.AcceptRole)
        btn_tray = mb.addButton("最小化到托盘", QMessageBox.ActionRole)
        btn_cancel = mb.addButton("取消", QMessageBox.RejectRole)
        mb.setDefaultButton(btn_tray)
        mb.exec_()
        clicked = mb.clickedButton()
        if clicked == btn_exit:
            _gv_log("ask close: exit chosen (remember=%s)" % chk.isChecked())
            if chk.isChecked():
                self.config["close_action"] = "exit"
                save_config(self.config)
            self.quit_app()
        elif clicked == btn_tray:
            _gv_log("ask close: tray chosen (remember=%s)" % chk.isChecked())
            if chk.isChecked():
                self.config["close_action"] = "tray"
                save_config(self.config)
            if self.tray is not None:
                self.shell.hidden = True
                self._tray_hint()
            else:
                self.shell.start()
                self._notify("系统托盘不可用，界面已重新打开")
        else:  # 取消：重新打开界面，不记住选择
            _gv_log("ask close: cancel chosen -> reopen UI")
            self.shell.start()

    def show_ui(self):
        self.shell.start()
        hwnd = self.shell.main_hwnd()
        if hwnd:
            win_show(hwnd, True)
            win_foreground(hwnd)

    def quit_app(self, keep_bridge=False):
        _gv_log("quit_app called")
        self._quitting = True
        self.shell.stop()
        if self.tray is not None:
            self.tray.hide()
        # 自更新时新进程已经接管同一个端口文件，旧进程不能把它删掉
        if not keep_bridge:
            try:
                os.remove(BRIDGE_PORT_FILE)
            except Exception:
                pass
        QApplication.instance().quit()

    # ---------- 扫描 ----------
    def rescan(self):
        if self._scanning:
            return
        self._scanning = True
        roots = list(self.config.get("scan_roots", []))
        libs = list(self.config.get("steam_libs", []))

        def worker():
            t0 = time.time()
            try:
                new = scan_games(roots)
            except Exception as e:
                _gv_log("scan failed: %r" % (e,))
                new = []
            try:
                found = steam_libraries()
                mp = steam_manifest_map(found or libs)
            except Exception:
                mp = {}
            self.ui_call.emit(lambda: self._on_scan_done(new, mp, t0))

        threading.Thread(target=worker, daemon=True).start()

    def _manual_items(self):
        out = []
        for g in self.config.get("manual_games", []):
            if not isinstance(g, dict):
                continue
            exe = g.get("path") or ""
            if not os.path.isfile(exe):
                continue
            name = g.get("cn") or os.path.splitext(os.path.basename(exe))[0]
            out.append({
                "folder": os.path.basename(exe), "cn": name, "en": name,
                "year": 0, "score": None, "tier": "暂无收录",
                "note": "手动添加的游戏 · " + exe, "known": True,
                "appid": g.get("appid"), "path": exe, "group": "手动添加",
                "manual": True,
            })
        return out

    def _on_scan_done(self, new, steam_map, t0):
        self._scanning = False
        self._steam_map = steam_map or {}
        # 扫描结果与库中识别成果的合并规则集中在 Store.merge_items：
        # 文件系统事实取本次扫描，appid / 中文名 / idChecked 取库里已有记录。
        # 合并后的列表就是唯一数据源，业务层不再维护第二份互相覆盖的副本。
        self.all_items = STORE.merge_items(list(new), self._manual_items())
        self._apply_steam_meta()
        self._annotate_playstats()
        self.last_scan = time.time()
        self._notify("扫描完成 · 共 %d 款游戏 · 耗时 %.1fs" %
                     (len(self.all_items), time.time() - t0))
        STORE.replace_games(self.all_items)
        self._request_covers()
        self._resolve_unknown()

    def _warm_from_cache(self):
        """启动先用本地库填充界面，避免大库重新扫描时的白屏等待。"""
        try:
            rows = STORE.load_games()
            if rows:
                self.all_items = rows
                self._annotate_playstats()
                self._request_covers()
                self._notify("已从本地缓存加载 %d 款游戏" % len(self.all_items))
        except Exception:
            pass

    def _save_games_cache(self):
        """把当前游戏列表整表 UPSERT 到 SQLite，供下次启动秒开。"""
        try:
            STORE.replace_games(self.all_items)
        except Exception:
            pass

    # ---------- 封面 / 简介补全 ----------
    def generate_cover(self, name, group, seed):
        """为没有 Steam 封面的游戏生成本地 SVG 占位图（确定性配色 + 标题），无需联网。"""
        import hashlib
        key = (name or "游戏") + (seed or "")
        hv = int(hashlib.md5(key.encode("utf-8", "ignore")).hexdigest(), 16)
        hue = hv % 360
        hue2 = (hue + 38) % 360
        c1 = "hsl(%d,52%%,40%%)" % hue
        c2 = "hsl(%d,58%%,20%%)" % hue2
        title = (name or "未命名游戏").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        sub = (group or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines = _wrap_cjk(title, 8, 3)
        ty = 380 - (len(lines) - 1) * 34
        text = "".join(
            '<text x="300" y="%d" text-anchor="middle" '
            'font-family="Segoe UI,Microsoft YaHei,sans-serif" '
            'font-size="50" font-weight="700" fill="#ffffff">%s</text>' % (ty + i * 64, ln)
            for i, ln in enumerate(lines))
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 900">'
               '<defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">'
               '<stop offset="0" stop-color="%s"/><stop offset="1" stop-color="%s"/>'
               '</linearGradient></defs>'
               '<rect width="600" height="900" fill="url(#g)"/>'
               '<circle cx="480" cy="150" r="160" fill="rgba(255,255,255,0.06)"/>'
               '<circle cx="90" cy="780" r="120" fill="rgba(0,0,0,0.10)"/>'
               '%s'
               '<text x="300" y="820" text-anchor="middle" '
               'font-family="Segoe UI,Microsoft YaHei,sans-serif" font-size="26" '
               'fill="rgba(255,255,255,0.72)">%s</text>'
               '</svg>') % (c1, c2, text, sub)
        return svg.encode("utf-8")

    def _load_meta_cache(self):
        try:
            return STORE.load_steam_meta()
        except Exception:
            return {}

    def _save_meta_cache(self, cache):
        try:
            STORE.save_steam_meta(cache)
        except Exception:
            pass

    def _fetch_appdetails(self, appid):
        """拉取 Steam 商店简介，失败返回 None。"""
        url = ("https://store.steampowered.com/api/appdetails?"
               "appids=%s&l=schinese&cc=cn&filters=basic,genres,developers,publishers,release_date"
               % appid)
        raw = _web_fetch(url, timeout=12)
        if not raw:
            return None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            node = data.get(str(appid)) or data.get(appid)
            if not node or not node.get("success"):
                return None
            return node.get("data") or {}
        except Exception:
            return None

    def game_detail(self, path):
        """详情页按需返回简介/类型等，Steam 资料本地缓存，减少重复请求。"""
        out = {"desc": "", "genres": [], "developers": [], "release": ""}
        it = self._item_by_path(path)
        if not it:
            return out
        appid = it.get("appid")
        if not appid:
            return out
        cache = self._load_meta_cache()
        d = cache.get(str(appid))
        if d is None:
            d = self._fetch_appdetails(appid)
            if d is not None:
                cache[str(appid)] = d
                self._save_meta_cache(cache)
        if d:
            out["desc"] = d.get("short_description", "") or ""
            out["genres"] = [g.get("description") for g in (d.get("genres") or [])][:4]
            out["developers"] = (d.get("developers") or [])[:3]
            out["release"] = (d.get("release_date") or {}).get("date", "")
        return out

    def _fetch_meta_all(self):
        """手动「补全封面与简介」：清除识别标记，强制重试所有未识别游戏。"""
        targets = [it for it in self.all_items if not it.get("appid")]
        if not targets:
            self._notify("所有游戏均已识别，无需补全")
            return
        for it in targets:
            it["idChecked"] = False

        def worker():
            changed = 0
            patches = []
            for it in targets:
                if it.get("appid"):
                    continue
                try:
                    appid, cn = resolve_steam(it.get("cn") or it.get("folder"))
                except Exception:
                    appid, cn = None, None
                it["idChecked"] = True
                if appid:
                    it["appid"] = appid
                    if cn:
                        it["cn"] = cn
                        it["en"] = cn
                    it["note"] = "已在线识别"
                    changed += 1
                patches.append((it.get("path"), {
                    "idChecked": True, "appid": it.get("appid"),
                    "cn": it.get("cn"), "en": it.get("en"), "note": it.get("note"),
                }))
            # 只 patch 本次涉及的几行，不整表重写，避免覆盖扫描/游玩统计等并发写入
            STORE.patch_games(patches)
            if changed:
                self.ui_call.emit(self._request_covers)
                self.ui_call.emit(lambda: self._notify(
                    "已为 %d 款游戏补全封面与资料" % changed))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_steam_meta(self):
        for it in self.all_items:
            if it.get("manual"):
                continue
            m = self._steam_map.get(os.path.basename(it["path"]).lower())
            if not m:
                continue
            if not it.get("appid") and m.get("appid"):
                it["appid"] = m["appid"]
            if not it.get("known") and m.get("name"):
                it["cn"] = m["name"]
                it["en"] = m["name"]
                it["note"] = "来自 Steam 库"

    def _annotate_playstats(self):
        ps = self.config.get("play_stats", {})
        ps_norm = {os.path.normcase(os.path.normpath(k)): v
                   for k, v in ps.items() if isinstance(k, str)}
        for it in self.all_items:
            p = it["path"]
            st = ps.get(p) or ps_norm.get(os.path.normcase(os.path.normpath(p))) or {}
            it["_total_play"] = st.get("total", 0.0)
            it["_last_play"] = st.get("last")

    def _request_covers(self):
        svc = cover_service()
        for it in self.all_items:
            if it.get("appid"):
                svc.request(str(it["appid"]), it["appid"])

    def _resolve_unknown(self):
        # idChecked：本游戏已经尝试过在线识别（无论成功或失败）。识别失败的通常是
        # 合集/汉化名/非 Steam 游戏，若不记标记，每次启动都会重新逐个请求 Steam。
        unknown = [it for it in self.all_items
                   if not it.get("appid") and not it.get("idChecked")
                   and (not it.get("known") or it.get("manual"))]
        if not unknown:
            return

        def worker():
            changed = 0
            patches = []
            for it in unknown:
                try:
                    appid, cn = resolve_steam(it.get("cn") or it.get("folder"))
                except Exception:
                    appid, cn = None, None
                it["idChecked"] = True     # 成功/失败都记，避免下次启动重复联网
                if appid:
                    it["appid"] = appid
                    if cn:
                        it["cn"] = cn
                        it["en"] = cn
                    it["note"] = "已在线识别"
                    changed += 1
                patches.append((it.get("path"), {
                    "idChecked": True, "appid": it.get("appid"),
                    "cn": it.get("cn"), "en": it.get("en"), "note": it.get("note"),
                }))
            # 无论是否识别成功都逐行落盘：成功保存 appid，失败保存 idChecked，
            # 这样下次启动只处理真正新增的游戏；不整表重写，避免覆盖并发写入。
            STORE.patch_games(patches)
            if changed:
                self.ui_call.emit(self._request_covers)
                self.ui_call.emit(lambda: self._notify("已在线识别 %d 款新游戏" % changed))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- 数据载荷 ----------
    def _web_payload(self):
        cfg = self.config
        ps = cfg.get("play_stats", {})
        # 路径大小写/斜杠差异不应让游玩记录「查不到」，建归一化索引兜底
        ps_norm = {os.path.normcase(os.path.normpath(k)): v
                   for k, v in ps.items() if isinstance(k, str)}
        favs = set(cfg.get("favorites", []))
        cats = cfg.get("game_categories", {})
        shots = cfg.get("shots", {})
        ovs = cfg.get("launch_overrides", {})
        now = time.time()
        out = []
        for it in self.all_items:
            p = it.get("path", "")
            appid = it.get("appid")
            cn = it.get("cn") or it.get("folder") or ""
            grp = it.get("group") or ""
            st = (ps.get(p) or
                  ps_norm.get(os.path.normcase(os.path.normpath(p))) or {})
            total = float(st.get("total", 0.0) or 0)
            playing = bool(self._sessions.get(p))
            if playing:
                total += now - self._sessions[p]      # 实时累加当前会话，托盘后台也不中断
            # 封面 URL 指向 /api/cover：有 AppID 走 Steam CDN，否则返回 SVG 占位图。
            args = ("appid=%s&name=%s&en=%s&key=%s&group=%s&v=%d" % (
                appid or "", urllib.parse.quote(cn),
                urllib.parse.quote(it.get("en") or ""), urllib.parse.quote(p),
                urllib.parse.quote(grp), self._cover_version))
            fb = "/api/cover-fallback?name=%s&group=%s&seed=%s" % (
                urllib.parse.quote(cn), urllib.parse.quote(grp), urllib.parse.quote(p))
            out.append({
                "cn": cn,
                "en": it.get("en") or "",
                "year": it.get("year") or 0,
                "score": it.get("score"),
                "tier": it.get("tier") or "暂无收录",
                "group": grp,
                "appid": appid,
                "path": p,
                "note": it.get("note") or "",
                "cover": "/api/cover?type=cover&" + args,
                "fallback": fb,
                "header": "/api/cover?type=header&" + args,
                "totalPlay": int(total),
                "totalPlayText": fmt_duration(total) or "",
                "lastPlay": fmt_last(_last_iso(st.get("last"))) if st.get("last") else "",
                "lastPlayTs": _last_iso(st.get("last")),
                "playCount": int(st.get("count", 0) or 0),
                "playing": playing,
                "sessionSec": int(now - self._sessions[p]) if playing else 0,
                "favorite": p in favs,
                "category": cats.get(p, ""),
                "shots": shots.get(p, []),
                "manual": bool(it.get("manual")),
                "launchExe": ovs.get(p, ""),
            })
        return out

    def _settings_payload(self):
        c = self.config
        ps = c.get("play_stats", {})
        total = sum(float((v or {}).get("total", 0) or 0) for v in ps.values())
        return {
            "app": APP_NAME, "appEn": APP_EN, "version": VERSION,
            "author": AUTHOR_NAME, "authorUrl": AUTHOR_URL, "repoUrl": REPO_URL,
            "dataDir": BASE_DIR,
            "roots": c.get("scan_roots", []),
            "manualGames": c.get("manual_games", []),
            "categories": c.get("categories", []),
            "launchOverrides": c.get("launch_overrides", {}),
            "favorites": c.get("favorites", []),
            "view": c.get("view", "grid"),
            "closeAction": c.get("close_action", "ask"),
            "closeMode": c.get("close_action", "ask"),
            "geometry": c.get("geometry"),
            "autostart": autostart_enabled(),
            "wallpaper": c.get("wallpaper"),
            "theme": c.get("web_theme", "dark"),
            "update": self.update_info or {},
            "stats": {
                "games": len(self.all_items),
                "played": sum(1 for v in ps.values() if float((v or {}).get("total", 0) or 0) > 0),
                "totalHours": round(total / 3600.0, 1),
                "favorites": len(c.get("favorites", [])),
            },
        }

    def _item_by_path(self, path):
        return next((i for i in self.all_items if i.get("path") == path), None)

    # ---------- 置顶模态文件选择框 ----------
    def _pick_file(self, caption, directory, flt, multi=False, only_dir=False,
                   preselect=None):
        """选择框强制置顶，期间禁用界面窗口，选完才允许继续操作软件。

        preselect：预先选中某个已存在的文件（用于「重新指定启动文件」时高亮当前 exe）。
        """
        hwnd = self.shell.main_hwnd()
        win_enable(hwnd, False)
        try:
            dlg = QFileDialog(None, caption, directory, flt)
            dlg.setWindowFlags(dlg.windowFlags() | Qt.WindowStaysOnTopHint)
            dlg.setOption(QFileDialog.DontUseNativeDialog, True)
            dlg.setFileMode(QFileDialog.Directory if only_dir
                            else QFileDialog.ExistingFiles if multi
                            else QFileDialog.ExistingFile)
            if preselect and os.path.isfile(preselect) and not only_dir:
                try:
                    dlg.selectFile(os.path.normpath(preselect))
                except Exception:
                    pass
            try:
                win_topmost(int(dlg.winId()))
            except Exception:
                pass
            if dlg.exec_() != QDialog.Accepted:
                return [] if multi else ""
            files = dlg.selectedFiles()
            return (files or []) if multi else (files[0] if files else "")
        finally:
            win_enable(hwnd, True)
            win_foreground(hwnd)

    # ---------- 动作分发 ----------
    def _web_action(self, action, value):
        c = self.config
        try:
            if action in ("launch", "openFolder", "uninstall", "favorite",
                          "chooseLaunch", "clearLaunch", "pickShots", "clearShots",
                          "removeManual"):
                it = self._item_by_path(str(value))
                if not it:
                    self._notify("未找到该游戏，请重新扫描")
                    return
                p = it["path"]
                if action == "launch":
                    self.launch(it)
                elif action == "openFolder":
                    self.open_folder(it)
                elif action == "uninstall":
                    self.remove_game(it)
                elif action == "favorite":
                    favs = c.setdefault("favorites", [])
                    if p in favs:
                        favs.remove(p)
                        self._notify("已取消收藏《%s》" % it["cn"])
                    else:
                        favs.append(p)
                        self._notify("已收藏《%s》" % it["cn"])
                    save_config(c)
                elif action == "chooseLaunch":
                    # 扫描得到的游戏 path 就是游戏文件夹本身；直接把选择框定位到
                    # 该文件夹内，避免打开到「包含所有游戏」的扫描根目录。
                    folder = p if os.path.isdir(p) else os.path.dirname(p)
                    if not os.path.isdir(folder):
                        folder = ""
                    # 若此前已指定过启动文件，预选它，便于核对/微调
                    current = c.get("launch_overrides", {}).get(p)
                    if not current and not it.get("manual"):
                        current = find_exe(p)
                    exe = self._pick_file(
                        "为《%s》选择启动文件（%s）" % (it["cn"], folder or "任意位置"),
                        folder,
                        "可执行文件 (*.exe *.bat *.cmd);;所有文件 (*.*)",
                        preselect=current)
                    if exe:
                        c.setdefault("launch_overrides", {})[p] = os.path.normpath(exe)
                        save_config(c)
                        self._notify("已设置启动文件：%s" % os.path.basename(exe))
                elif action == "clearLaunch":
                    if c.get("launch_overrides", {}).pop(p, None) is not None:
                        save_config(c)
                        self._notify("已恢复自动检测启动文件")
                elif action == "pickShots":
                    files = self._pick_file(
                        "为《%s》添加截图" % it["cn"], "",
                        "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;所有文件 (*.*)",
                        multi=True)
                    if files:
                        sub = "shots/" + hashlib.md5(p.encode("utf-8", "ignore")).hexdigest()[:10]
                        rels = list(c.setdefault("shots", {}).get(p, []))
                        for f in files[:20]:
                            try:
                                rels.append(import_media(f, sub))
                            except Exception:
                                pass
                        c["shots"][p] = rels
                        save_config(c)
                        self._notify("已添加 %d 张截图" % len(files))
                elif action == "clearShots":
                    c.setdefault("shots", {}).pop(p, None)
                    save_config(c)
                elif action == "removeManual":
                    c["manual_games"] = [g for g in c.get("manual_games", [])
                                         if (g or {}).get("path") != p]
                    save_config(c)
                    self.rescan()
                    self._notify("已从游戏库移除《%s》" % it["cn"])
                return

            if action == "rescan":
                self.rescan()
            elif action == "theme":
                c["web_theme"] = "light" if value == "light" else "dark"
                save_config(c)
            elif action == "view":
                c["view"] = "list" if value == "list" else "grid"
                save_config(c)
            elif action == "closeAction":
                v = value if str(value) in ("exit", "tray", "ask") else "ask"
                c["close_action"] = v
                save_config(c)
            elif action == "closeMode":
                # 三种关闭行为：直接退出 / 最小化到托盘 / 每次询问（三选一）
                v = value if str(value) in ("exit", "tray", "ask") else "ask"
                c["close_action"] = v
                save_config(c)
            elif action == "closeAsk":
                pass   # 兼容旧前端：已合并进 closeMode 的 ask 选项
            elif action == "wallOpacity":
                w = c.get("wallpaper")
                if isinstance(w, dict):
                    try:
                        w["opacity"] = max(0.05, min(0.9, float(value)))
                        save_config(c)
                    except Exception:
                        pass
            elif action == "autostart":
                ok = set_autostart(value in ("on", "1", True, 1))
                self._notify("已开启开机自启动" if ok and value in ("on", "1", True, 1)
                             else "已关闭开机自启动" if ok else "设置开机自启动失败")
            elif action == "setRoots":
                if isinstance(value, list):
                    c["scan_roots"] = [os.path.normpath(str(x)) for x in value if str(x).strip()]
                    save_config(c)
                    self.rescan()
                    self._notify("扫描路径已保存，正在重新扫描")
            elif action == "pickRoot":
                d = self._pick_file("选择游戏目录", "F:\\", "", only_dir=True)
                if d:
                    d = os.path.normpath(d)
                    if d not in c.setdefault("scan_roots", []):
                        c["scan_roots"].append(d)
                        save_config(c)
                        self.rescan()
                        self._notify("已添加扫描目录：%s" % d)
            elif action == "pickGame":
                exe = self._pick_file(
                    "选择游戏启动文件", "",
                    "程序 (*.exe *.lnk *.bat *.cmd);;所有文件 (*.*)")
                if exe:
                    exe = os.path.normpath(exe)
                    games = c.setdefault("manual_games", [])
                    if not any((g or {}).get("path") == exe for g in games):
                        games.append({"path": exe,
                                      "cn": os.path.splitext(os.path.basename(exe))[0]})
                        save_config(c)
                        self.rescan()
                        self._notify("已添加游戏：%s" % os.path.basename(exe))
            elif action == "importSteam":
                libs = steam_libraries()
                added = 0
                c["steam_libs"] = libs
                for lib in libs:
                    common = os.path.join(lib, "steamapps", "common")
                    if os.path.isdir(common) and common not in c.setdefault("scan_roots", []):
                        c["scan_roots"].append(common)
                        added += 1
                save_config(c)
                self.rescan()
                self._notify("检测到 %d 个 Steam 库，新增 %d 个扫描目录" % (len(libs), added))
            elif action == "pickWallpaper":
                kind = "video" if value == "video" else "image"
                flt = ("视频 (*.mp4 *.webm *.mov);;所有文件 (*.*)" if kind == "video"
                       else "图片 (*.png *.jpg *.jpeg *.webp *.gif *.bmp);;所有文件 (*.*)")
                fp = self._pick_file("选择背景壁纸", "", flt)
                if fp:
                    old = c.get("wallpaper")
                    if isinstance(old, dict) and old.get("file"):
                        try:
                            op = media_file(old["file"])
                            if op:
                                os.remove(op)
                        except Exception:
                            pass
                    c["wallpaper"] = {"type": kind, "file": import_media(fp, "wallpapers"),
                                      "opacity": (old or {}).get("opacity", 0.32)
                                      if isinstance(old, dict) else 0.32}
                    save_config(c)
                    self._notify("壁纸已应用")
            elif action == "clearWallpaper":
                old = c.get("wallpaper")
                if isinstance(old, dict) and old.get("file"):
                    try:
                        op = media_file(old["file"])
                        if op:
                            os.remove(op)
                    except Exception:
                        pass
                c["wallpaper"] = None
                save_config(c)
            elif action == "setCategory":
                if isinstance(value, dict):
                    pth, name = str(value.get("path", "")), str(value.get("name", ""))
                else:
                    pth, name = str(value), ""
                if pth:
                    cats = c.setdefault("game_categories", {})
                    if name:
                        cats[pth] = name
                        if name not in c.setdefault("categories", []):
                            c["categories"].append(name)
                    else:
                        cats.pop(pth, None)
                    save_config(c)
            elif action == "newCategory":
                name = str(value).strip()
                if name and name not in c.setdefault("categories", []):
                    c["categories"].append(name)
                    save_config(c)
            elif action == "delCategory":
                name = str(value)
                c["categories"] = [x for x in c.get("categories", []) if x != name]
                gc = c.get("game_categories", {})
                for k in [k for k, v in gc.items() if v == name]:
                    gc.pop(k, None)
                save_config(c)
            elif action == "fetchMeta":
                self._fetch_meta_all()
                self._notify("正在补全封面与简介…")
            elif action == "clearCache":
                # 只清封面图片与 Steam 简介缓存；games 表（含已识别 appid /
                # idChecked）保留，避免清缓存后又触发一轮全量在线识别。
                STORE.clear_steam_meta()
                try:
                    if os.path.isdir(COVER_CACHE_DIR):
                        for f in os.listdir(COVER_CACHE_DIR):
                            try:
                                os.remove(os.path.join(COVER_CACHE_DIR, f))
                            except Exception:
                                pass
                except Exception:
                    pass
                # 版本号落盘，配合 /api/cover 的长缓存头：清缓存后即使重启也能
                # 让浏览器重新取图，而不是继续用旧的 immutable 缓存。
                self._cover_version = int(c.get("cover_version", 1) or 1) + 1
                c["cover_version"] = self._cover_version
                save_config(c)
                try:
                    cover_service().reset_memory()
                except Exception:
                    pass
                self.ui_call.emit(self._request_covers)
                self._notify("封面缓存已清除，正在重新下载（游戏识别结果已保留）")
            elif action == "resetApp":
                STORE.reset_all()   # 清空用户态表，保留内置评分库与迁移标记
                for d in (COVER_CACHE_DIR, WALL_DIR, SHOT_DIR, MEDIA_DIR):
                    if os.path.isdir(d):
                        for root, _, files in os.walk(d):
                            for fn in files:
                                try:
                                    os.remove(os.path.join(root, fn))
                                except Exception:
                                    pass
                self.config = load_config()
                self.rescan()
                self._notify("已恢复初始设置，游戏库重新扫描完成")
            elif action == "quit":
                self.quit_app()
            elif action == "tray":
                self._to_tray()
            elif action == "checkUpdate":
                self.check_for_updates(True)
            elif action == "downloadUpdate":
                self.start_update()
            elif action == "openUrl":
                try:
                    os.startfile(str(value))
                except Exception:
                    pass
        except Exception as e:
            _gv_log("action %r failed: %r" % (action, e))
            self._notify("操作失败：%s" % e)

    # ---------- 游戏动作 ----------
    def get_launch_exe(self, it):
        if it.get("manual"):
            return it["path"]
        ov = self.config.get("launch_overrides", {}).get(it["path"])
        return ov if ov and os.path.isfile(ov) else find_exe(it["path"])

    def open_folder(self, it):
        folder = os.path.dirname(it["path"]) if it.get("manual") else it["path"]
        if not os.path.isdir(folder):
            return
        os.startfile(folder)
        # 新开的资源管理器窗口可能被主窗口挡住：稍后把它置顶并带到前台
        base = os.path.basename(os.path.normpath(folder)) or folder
        QTimer.singleShot(400, lambda: self._raise_folder_window(base))

    def _raise_folder_window(self, name):
        """把标题含 name 的资源管理器窗口置顶到最前（HWND_TOPMOST + 前台）。"""
        if not name:
            return
        try:
            for hwnd, _area, title, _vis in enum_top_windows(None, include_hidden=False):
                if title and name.lower() in title.lower():
                    win_topmost(hwnd)
                    win_foreground(hwnd)
                    return
        except Exception as e:
            _gv_log("raise folder window failed: %r" % (e,))

    def launch(self, it):
        exe = self.get_launch_exe(it)
        if not exe or not os.path.isfile(exe):
            self._notify("未找到可执行文件，可在详情里手动指定启动文件")
            return
        st = self.config.setdefault("play_stats", {}).setdefault(
            it["path"], {"total": 0.0, "last": None, "count": 0})
        st["last"] = datetime.datetime.now().isoformat(timespec="seconds")
        st["count"] = int(st.get("count", 0) or 0) + 1
        save_config(self.config)
        start = time.time()
        try:
            if exe.lower().endswith(".exe"):
                proc = subprocess.Popen([exe], cwd=os.path.dirname(exe) or None)
                self._sessions[it["path"]] = start   # 标记「正在游玩」，托盘后台也持续计时
                self._track_session(it, proc, start)
                self._notify("已启动《%s》，正在记录游玩时长" % it["cn"])
            else:
                os.startfile(exe)
                self._notify("已启动《%s》（该格式不统计时长）" % it["cn"])
        except Exception as e:
            self._notify("启动失败：%s" % e)

    def _track_session(self, it, proc, start):
        def worker():
            try:
                proc.wait()
            except Exception:
                return
            elapsed = time.time() - start
            self._sessions.pop(it["path"], None)    # 退出后停止「正在游玩」
            if elapsed < 10:
                return
            st = self.config.setdefault("play_stats", {}).setdefault(
                it["path"], {"total": 0.0, "last": None, "count": 0})
            st["total"] = float(st.get("total", 0.0) or 0) + elapsed
            save_config(self.config)
            self.ui_call.emit(lambda: self._notify(
                "《%s》本次游玩 %s，累计 %s" %
                (it["cn"], fmt_duration(elapsed), fmt_duration(st["total"]))))

        threading.Thread(target=worker, daemon=True).start()

    def remove_game(self, it):
        if it.get("manual"):
            self.config["manual_games"] = [
                g for g in self.config.get("manual_games", [])
                if (g or {}).get("path") != it["path"]]
            save_config(self.config)
            self.rescan()
            self._notify("已从游戏库移除《%s》" % it["cn"])
            return
        path = it["path"]
        if not os.path.isdir(path) or len(os.path.abspath(path)) <= 4:
            self._notify("目录不存在或路径无效，已刷新列表")
            self.rescan()
            return
        try:
            recycle(path)
        except Exception as e:
            self._notify("卸载失败：%s" % e)
            return
        self.rescan()
        self._notify("《%s》已移入回收站，可在回收站还原" % it["cn"])

    # ---------- 单文件自更新 ----------
    def _set_update_progress(self, phase, percent=0, downloaded=0, total=0,
                             speed=0, eta=0, message=""):
        """整体替换 dict（引用原子替换），供 HTTP 线程安全读取。"""
        self.update_progress = {
            "phase": phase, "percent": round(float(percent), 1),
            "downloaded": int(downloaded), "total": int(total),
            "speed": round(float(speed), 1), "eta": round(float(eta), 1),
            "message": message, "ver": self._update_ver,
        }

    def check_for_updates(self, manual=False):
        def worker():
            info, status = check_latest_ex()
            _gv_log("update check: status=%s ver=%s" %
                    (status, (info or {}).get("ver")))
            if not info or not info.get("url"):
                if manual:
                    msg = {
                        "none": "检查更新：仓库还没有发布版本（当前 v%s）" % VERSION,
                        "noasset": "检查更新：最新发布里没有可用的 exe 资产",
                        "offline": "检查更新失败：无法连接 GitHub，请检查网络或代理",
                    }.get(status, "检查更新失败，请稍后重试")
                    self.ui_call.emit(lambda: self._notify(msg))
                return
            if info["ver"] > parse_version(VERSION):
                ver = ".".join(map(str, info["ver"]))
                if info.get("portable", True) is False:
                    # 只有 Setup 安装包时绝不自动替换：否则便携版会被换成安装器
                    self.ui_call.emit(lambda: self._notify(
                        "发现新版本 v%s，但该发布没有单文件版，请到 Releases 手动下载" % ver))
                    return
                self.update_info = {
                    "ver": ver,
                    "tag": info.get("tag") or ("v" + ver),
                    "url": info.get("url"), "name": info.get("name"),
                    "setupUrl": info.get("setupUrl"),
                    "body": (info.get("body") or "")[:600],
                    "portable": info.get("portable", True) is not False,
                }
                self.ui_call.emit(lambda: self._notify(
                    "发现新版本 v%s，可在「设置 → 通用 → 版本更新」一键更新" % ver))
            else:
                # 之前查到过的版本可能已被撤销，或本机已换上新版：清掉过期提示
                self.update_info = None
                if manual:
                    self.ui_call.emit(lambda: self._notify(
                        "当前已是最新版本 v%s" % VERSION))

        threading.Thread(target=worker, daemon=True).start()

    def start_update(self):
        info = self.update_info
        if not info or not info.get("url"):
            self._notify("暂无可用更新")
            return
        if self._update_busy:
            self._notify("更新正在进行中，请稍候…")
            return
        self._update_busy = True
        self._update_ver = info.get("ver") or ""
        # 装到 Program Files 等只读目录时落盘到 %TEMP%，否则下载第一步就会 PermissionError
        tmp_dir = BASE_DIR if _dir_writable(BASE_DIR) else tempfile.gettempdir()
        tmp = os.path.join(tmp_dir, "GameBox.update.tmp")
        self._set_update_progress("downloading", 0, 0, 0, 0, 0, "正在连接更新服务器…")

        def download(url, dest):
            """分块下载并实时上报进度/速度；返回 (done, total, speed)。"""
            done = 0
            speed = 0.0
            with _open_url(url, timeout=30) as r:
                total = int(r.headers.get("Content-Length") or 0)
                t0 = last_t = time.time()
                last_b = 0
                with open(dest, "wb") as f:
                    while True:
                        chunk = r.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        now = time.time()
                        if now - last_t >= 0.25:
                            inst = (done - last_b) / max(now - last_t, 1e-6)
                            speed = inst if not speed else speed * 0.55 + inst * 0.45
                            last_t, last_b = now, done
                            pct = (done * 100.0 / total) if total else 0.0
                            eta = ((total - done) / speed) if (speed and total) else 0
                            self._set_update_progress(
                                "downloading", pct, done, total, speed, eta,
                                "正在下载单文件版…")
                if not speed:
                    speed = done / max(time.time() - t0, 1e-6)
            return done, total, speed

        def worker():
            try:
                try:
                    done, total, speed = download(info["url"], tmp)
                except Exception as first:
                    # 拼名 URL 变更或 404 时，用 REST API 重新解析真实资产再试一次
                    if UPDATE_MANIFEST_URL:
                        raise
                    alt = _latest_from_api()
                    if not alt or not alt.get("url") or alt["url"] == info["url"]:
                        raise
                    _gv_log("update: primary url failed (%r), retry with API asset" % (first,))
                    done, total, speed = download(alt["url"], tmp)
                # 基本完整性校验，避免把错误页当成 exe
                if done < 1024 * 1024 or (total and done != total):
                    raise OSError("下载文件不完整（%d/%d 字节）" % (done, total))
                with open(tmp, "rb") as f:
                    if f.read(2) != b"MZ":
                        raise OSError("下载内容不是有效的可执行文件")
                self._set_update_progress("installing", 100, done, total, speed, 0,
                                          "下载完成，正在应用更新…")
                self.ui_call.emit(lambda: self._finish_update(tmp))
            except Exception as e:
                _gv_log("update failed: %r" % (e,))
                try:
                    _win_delete_file(tmp)
                except Exception:
                    pass
                self._update_busy = False
                self._set_update_progress("error", 0, 0, 0, 0, 0, "更新失败：%s" % e)
                self.ui_call.emit(lambda: self._notify("更新失败：%s" % e))

        threading.Thread(target=worker, daemon=True).start()

    def _finish_update(self, tmp):
        """主线程：自替换 exe → 启动新版 → 退出当前进程（无任何确认）。"""
        try:
            target, old = self._replace_running_exe(tmp)
            env = dict(os.environ)
            env["GV_RESTART"] = "1"
            env["GV_OLD_EXE"] = old or ""
            subprocess.Popen([target], cwd=os.path.dirname(target) or None, env=env,
                             close_fds=True,
                             creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
            self._set_update_progress("restarting", 100,
                                      self.update_progress.get("downloaded", 0),
                                      self.update_progress.get("total", 0), 0, 0,
                                      "更新完成，正在自动重启…")
            _gv_log("update: replaced and relaunched %s" % target)

            def _bye():
                # 自更新退出必须确定、立即：只调用 QApplication.quit() 时，
                # onefile 的 bootloader / 残留嵌套事件循环可能让旧进程继续
                # 握着 .old 的映像句柄，新版就永远清不掉备份。
                _gv_log("update: old process exiting")
                self.quit_app(keep_bridge=True)
                _force_exit_after_update()

            QTimer.singleShot(1200, _bye)
        except Exception as e:
            _gv_log("update: apply failed: %r" % (e,))
            self._update_busy = False
            self._set_update_progress("error", 0, 0, 0, 0, 0, "应用更新失败：%s" % e)
            self._notify("应用更新失败：%s" % e)

    @staticmethod
    def _replace_running_exe(tmp):
        """把下载好的新版放到当前 exe 位置。

        Windows 允许重命名正在运行的 exe：先 rename 成 .old 腾出文件名，
        再把新版放进去，即可实现单文件自替换，无需安装程序、无需 UAC。
        目录不可写（安装版在 Program Files）时，退到可写的配置目录启动新版。
        """
        cur = sys.executable
        fallback = os.path.join(BASE_DIR, "GameBox-Portable.exe")
        if not getattr(sys, "frozen", False) or not _dir_writable(os.path.dirname(cur)):
            shutil.move(tmp, fallback)
            return fallback, None
        old = cur + ".old"
        for _ in range(4):
            try:
                if os.path.isfile(old):
                    _win_delete_file(old)
                break
            except Exception:
                time.sleep(0.25)
        renamed = False
        try:
            os.rename(cur, old)
            renamed = True
            shutil.move(tmp, cur)
            return cur, old
        except Exception as e:
            if renamed:
                try:
                    os.rename(old, cur)
                except Exception:
                    pass
            _gv_log("update: in-place replace failed (%r), fallback to %s" % (e, fallback))
            shutil.move(tmp, fallback)
            return fallback, None


def find_browser():
    """定位系统 Edge / Chrome 的可执行文件。"""
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        p = os.environ.get(env)
        if p:
            cands += [os.path.join(p, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(p, "Google", "Chrome", "Application", "chrome.exe")]
    for p in cands:
        if os.path.isfile(p):
            return p
    for p in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(p):
            return p
    return None


_old_cleanup_tries = 0


def _force_exit_after_update():
    """自更新旧进程立即结束，释放 .old 的映像句柄让新进程完成清理。

    刻意不动桥接端口文件：新进程已经接管它，旧进程删除会导致重启后失联。
    """
    _gv_log("update: force-exiting stale old process")
    os._exit(0)


def _cleanup_old_exe():
    """清理上一版留下的 .old 备份。

    onefile 的父进程可能仍占用被改名的旧 exe（释放时间受系统负载 / 安全软件
    扫描影响，实测几秒到一分钟不等），所以失败后每 1.5s 重试，最多约 2 分钟；
    即便本次没删掉，下次启动也会再清，不会残留无用备份。
    """
    global _old_cleanup_tries
    cands = []
    for p in (os.environ.get("GV_OLD_EXE"), sys.executable + ".old"):
        if p and p not in cands:
            cands.append(p)
    left = [p for p in cands if os.path.isfile(p)]
    if not left:
        return
    err = None
    for p in left:
        try:
            _win_delete_file(p)
        except Exception as e:
            err = e
    left = [p for p in cands if os.path.isfile(p)]
    if not left:
        _gv_log("cleaned old exe backup")
        return
    _old_cleanup_tries += 1
    if _old_cleanup_tries == 1:
        _gv_log("old exe cleanup pending: %r" % (err,))
    if _old_cleanup_tries < 80:
        QTimer.singleShot(1500, _cleanup_old_exe)
    else:
        _gv_log("give up cleaning old exe: %r" % (err,))


# ---------------- 更新后自动清理遗留安装包 ----------------
_INSTALLER_NAME_RE = re.compile(
    r"^(?:GameBox|GameVault)-(?:Setup|Portable)-v(\d+)(?:\.(\d+))?(?:\.(\d+))?\.exe$",
    re.IGNORECASE)


def _ver_tuple(s):
    try:
        return tuple(int(x) for x in str(s).split("."))
    except Exception:
        return (0,)


def _stale_installer_paths(dirs, current):
    """列出可安全清理的旧版安装包：命名严格匹配 + 版本低于当前 + 非运行中的 exe。"""
    cur = _ver_tuple(current)
    exe = os.path.abspath(sys.executable).lower()
    out = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for name in names:
            m = _INSTALLER_NAME_RE.match(name)
            # 兼容 v1 / v1.2 / v1.2.3 三种命名，缺失的段按 0 补齐
            if not m or _ver_tuple(".".join(g for g in m.groups() if g)) >= cur:
                continue
            p = os.path.join(d, name)
            if not os.path.isfile(p) or os.path.abspath(p).lower() == exe:
                continue
            out.append(p)
    return out


def _cleanup_stale_installers(dirs=None, current=None, reason="startup"):
    """更新后自动清理遗留的旧版安装包 / 便携包与未完成的下载临时包。

    安全边界：只扫描程序目录、配置目录、系统临时目录的**顶层**（不递归）；
    文件名必须严格为 GameBox|GameVault-Setup|Portable-vX.Y.Z.exe；
    只删版本号严格低于当前 VERSION 的文件；当前正在运行的 exe 永不删。
    返回被删除的路径列表。
    """
    if dirs is None:
        dirs = []
        for d in (os.path.dirname(os.path.abspath(sys.executable)),
                  BASE_DIR, tempfile.gettempdir()):
            if d and d not in dirs:
                dirs.append(d)
    removed = []
    for p in _stale_installer_paths(dirs, current or VERSION):
        try:
            _win_delete_file(p)
            removed.append(p)
        except Exception as e:
            _gv_log("stale installer cleanup failed: %r" % (e,))
    # 更新中断留下的半下载文件（正常流程里它已被 move 成正式 exe）
    for tmp_dir in dict.fromkeys((BASE_DIR, tempfile.gettempdir())):
        tmp = os.path.join(tmp_dir, "GameBox.update.tmp")
        if os.path.isfile(tmp):
            try:
                _win_delete_file(tmp)
                removed.append(tmp)
            except Exception:
                pass
    _gv_log("installer auto-clean (%s): %d removed" % (reason, len(removed)))
    return removed


def main():
    # 自更新重启：旧进程退出前 mutex 仍被占用，新进程在此轮询等待接管，
    # 不会误触发「已有实例 → 激活旧窗口」而启动失败。
    restart = os.environ.get("GV_RESTART") == "1"
    deadline = time.time() + 20
    while not acquire_single_instance():
        if not restart or time.time() >= deadline:
            _gv_log("single instance: second launch redirected to running app")
            _release_mutex_probe()
            activate_existing_instance()
            return
        _release_mutex_probe()   # 否则自己持有的句柄会让 mutex 永远「已存在」
        time.sleep(0.2)
    if restart:
        _gv_log("main: resumed after self-update")
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setQuitOnLastWindowClosed(False)
    ip = os.path.join(DATA_DIR, "icon.png")
    if os.path.isfile(ip):
        app.setWindowIcon(QIcon(ip))
    core = AppCore()
    QTimer.singleShot(900, _cleanup_old_exe)
    # 自动更新重启后优先清理旧安装包；普通启动也做一次幂等清理，
    # 把历史上手工下载/遗留的旧版本包一并收掉。
    QTimer.singleShot(1200 if restart else 2500,
                      lambda: _cleanup_stale_installers(
                          reason="after-update" if restart else "startup"))
    _gv_log("main: version=%s browser=%s" % (VERSION, find_browser()))
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
