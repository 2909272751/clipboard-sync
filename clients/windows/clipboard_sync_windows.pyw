import ctypes
import ctypes.wintypes as wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import sys
import threading
import time
import uuid

import requests
import socketio


VERSION = "1.2.0"
WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
ERROR_ALREADY_EXISTS = 183
HWND_MESSAGE = -3
WM_APP = 0x8000
NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002
NIM_SETVERSION = 0x00000004
NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010
NIIF_INFO = 0x00000001
NIIF_NOSOUND = 0x00000010
NOTIFYICON_VERSION_4 = 4
IDI_APPLICATION = 32512

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.GetLastError.restype = wintypes.DWORD
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.LoadIconW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadIconW.restype = wintypes.HICON


def app_dir():
    executable = Path(sys.executable if getattr(sys, "frozen", False) else __file__)
    return executable.resolve().parent


def load_config():
    path = app_dir() / "config.json"
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    for key in ("server_url", "device_token", "device_id", "device_name"):
        if not config.get(key):
            raise ValueError(f"config.json missing {key}")
    config["server_url"] = config["server_url"].rstrip("/")
    config["device_id"] = int(config["device_id"])
    show_notifications = config.get("show_notifications", True)
    config["show_notifications"] = str(show_notifications).lower() not in {"0", "false", "no"}
    return config


def configure_logging():
    log_dir = Path(os.environ.get("LOCALAPPDATA", app_dir())) / "ClipboardSync"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_dir / "clipboard-sync.log", maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[handler],
    )


def content_hash(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def notification_preview(text, limit=40):
    clean = " ".join(str(text).replace("\r", " ").replace("\n", " ").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def open_clipboard(retries=10):
    for _ in range(retries):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.03)
    return False


def read_clipboard_text():
    if not open_clipboard():
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.wstring_at(pointer)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def write_clipboard_text(text):
    encoded = (text + "\0").encode("utf-16-le")
    memory = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
    if not memory:
        return False
    pointer = kernel32.GlobalLock(memory)
    if not pointer:
        kernel32.GlobalFree(memory)
        return False
    ctypes.memmove(pointer, encoded, len(encoded))
    kernel32.GlobalUnlock(memory)

    if not open_clipboard():
        kernel32.GlobalFree(memory)
        return False
    transferred = False
    try:
        user32.EmptyClipboard()
        transferred = bool(user32.SetClipboardData(CF_UNICODETEXT, memory))
        return transferred
    finally:
        user32.CloseClipboard()
        if not transferred:
            kernel32.GlobalFree(memory)


LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uTimeoutOrVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


shell32 = ctypes.windll.shell32
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]
shell32.Shell_NotifyIconW.restype = wintypes.BOOL


class WindowsNotifier:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.hwnd = None
        self.added = False
        self.pending = None
        self.lock = threading.Lock()

    @staticmethod
    def _data(hwnd):
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = hwnd
        data.uID = 1
        return data

    def attach(self, hwnd):
        if not self.enabled:
            return
        pending = None
        with self.lock:
            data = self._data(hwnd)
            data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
            data.uCallbackMessage = WM_APP + 1
            data.hIcon = user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
            data.szTip = "Clipboard Sync"
            self.added = bool(shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)))
            if self.added:
                data.uTimeoutOrVersion = NOTIFYICON_VERSION_4
                shell32.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data))
                self.hwnd = hwnd
                pending = self.pending
                self.pending = None
            else:
                logging.warning("Unable to register Windows notification icon")
        if pending:
            self.show(*pending)

    def show(self, title, message):
        if not self.enabled:
            return
        with self.lock:
            if not self.added or not self.hwnd:
                self.pending = (str(title), str(message))
                logging.info("Notification queued until Windows listener is ready")
                return
            data = self._data(self.hwnd)
            data.uFlags = NIF_INFO
            data.szInfoTitle = str(title)[:63]
            data.szInfo = str(message)[:255]
            data.dwInfoFlags = NIIF_INFO | NIIF_NOSOUND
            if not shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)):
                logging.warning("Unable to show Windows notification")

    def detach(self):
        with self.lock:
            if self.added and self.hwnd:
                data = self._data(self.hwnd)
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
            self.added = False
            self.hwnd = None


user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
user32.RegisterClassW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
user32.AddClipboardFormatListener.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int


def run_clipboard_listener(on_change, on_ready=None, on_destroy=None):
    class_name = f"ClipboardSyncListener_{os.getpid()}"

    @WNDPROC
    def window_proc(hwnd, message, wparam, lparam):
        if message == WM_CLIPBOARDUPDATE:
            on_change()
            return 0
        if message == WM_DESTROY:
            if on_destroy:
                on_destroy()
            user32.PostQuitMessage(0)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    instance = kernel32.GetModuleHandleW(None)
    window_class = WNDCLASSW()
    window_class.lpfnWndProc = window_proc
    window_class.hInstance = instance
    window_class.lpszClassName = class_name
    if not user32.RegisterClassW(ctypes.byref(window_class)):
        raise ctypes.WinError()

    hwnd = user32.CreateWindowExW(
        0, class_name, class_name, 0, 0, 0, 0, 0,
        wintypes.HWND(HWND_MESSAGE), None, instance, None,
    )
    if not hwnd:
        raise ctypes.WinError()
    if not user32.AddClipboardFormatListener(hwnd):
        raise ctypes.WinError()
    if on_ready:
        on_ready(hwnd)

    message = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(message))
        user32.DispatchMessageW(ctypes.byref(message))


class ClipboardSyncClient:
    def __init__(self, config):
        self.config = config
        self.events = queue.Queue(maxsize=1)
        self.state_lock = threading.Lock()
        initial = read_clipboard_text()
        self.last_uploaded_hash = content_hash(initial) if initial else ""
        self.last_remote_hash = ""
        self.notifier = WindowsNotifier(config.get("show_notifications", True))
        self.sio = socketio.Client(reconnection=True, logger=False, engineio_logger=False)
        self.sio.on("clipboard_update", self.on_remote_clipboard)

    def notify_change(self):
        try:
            self.events.put_nowait(True)
        except queue.Full:
            pass

    def on_remote_clipboard(self, data):
        try:
            if int(data.get("device_id", -1)) == self.config["device_id"]:
                return
            text = data.get("pure_code") if data.get("type") == "code" else data.get("content")
            if not isinstance(text, str) or not text:
                return
            digest = content_hash(text)
            with self.state_lock:
                if digest == self.last_remote_hash:
                    return
                self.last_remote_hash = digest
                self.last_uploaded_hash = digest
            if write_clipboard_text(text):
                device = str(data.get("device") or "其他设备")
                logging.info("Received clipboard from device %s", device)
                self.notifier.show(
                    "Clipboard Sync 已接收",
                    f"来自 {notification_preview(device)}：{notification_preview(text)}",
                )
            else:
                logging.warning("Unable to write remote clipboard")
        except Exception:
            logging.exception("Remote clipboard handler failed")

    def upload_worker(self):
        while True:
            self.events.get()
            time.sleep(0.15)
            while True:
                try:
                    self.events.get_nowait()
                except queue.Empty:
                    break

            text = read_clipboard_text()
            if not text:
                continue
            digest = content_hash(text)
            with self.state_lock:
                if digest in {self.last_uploaded_hash, self.last_remote_hash}:
                    continue

            try:
                response = requests.post(
                    f"{self.config['server_url']}/api/push",
                    json={"content": text, "event_id": uuid.uuid4().hex},
                    headers={"Authorization": f"Bearer {self.config['device_token']}"},
                    timeout=8,
                )
                result = response.json() if response.ok else {}
                status = result.get("status")
                if response.ok and status == "ok":
                    with self.state_lock:
                        self.last_uploaded_hash = digest
                    logging.info("Uploaded clipboard, length=%d", len(text))
                    self.notifier.show(
                        "Clipboard Sync 已上传",
                        notification_preview(text),
                    )
                elif response.ok and status == "ignored":
                    with self.state_lock:
                        self.last_uploaded_hash = digest
                    logging.info("Upload ignored by server as duplicate")
                elif response.ok:
                    logging.warning("Upload returned status=%s", status)
                else:
                    logging.warning("Upload failed with HTTP %s", response.status_code)
            except Exception:
                logging.exception("Clipboard upload failed")

    def socket_worker(self):
        delay = 2
        while True:
            try:
                self.sio.connect(
                    self.config["server_url"],
                    auth={"token": self.config["device_token"]},
                    transports=["websocket"],
                    wait_timeout=10,
                )
                delay = 2
                logging.info("Realtime connection established")
                self.sio.wait()
            except Exception:
                logging.exception("Realtime connection failed; retrying in %ss", delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)

    def run(self):
        threading.Thread(target=self.upload_worker, daemon=True).start()
        threading.Thread(target=self.socket_worker, daemon=True).start()
        run_clipboard_listener(
            self.notify_change,
            self.notifier.attach,
            self.notifier.detach,
        )


def acquire_single_instance():
    handle = kernel32.CreateMutexW(None, False, "Local\\ClipboardSyncWindows_v1")
    if not handle or kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return None
    return handle


def main():
    configure_logging()
    mutex = acquire_single_instance()
    if not mutex:
        return
    try:
        config = load_config()
        logging.info("Clipboard Sync Windows v%s starting", VERSION)
        ClipboardSyncClient(config).run()
    except Exception as exc:
        logging.exception("Startup failed")
        user32.MessageBoxW(None, str(exc), "Clipboard Sync 启动失败", 0x10)


if __name__ == "__main__":
    main()
