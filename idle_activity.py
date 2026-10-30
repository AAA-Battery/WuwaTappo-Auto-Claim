"""Optional F15 activity simulation for the Windows desktop."""

import ctypes
import random
import threading
import time
from ctypes import wintypes


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


VK_F15 = 0x7E
KEYEVENTF_KEYUP = 0x0002
IDLE_THRESHOLD_SECONDS = 15.0
IDLE_BATCH_CLICKS = 1000
IDLE_BATCH_COOLDOWN_SECONDS = 5 * 60
MANUAL_BATCH_CLICKS = 1000
MANUAL_CLICK_INTERVAL_SECONDS = 0.15


def _last_input_tick(user32):
    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    if not user32.GetLastInputInfo(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.dwTime)


def _press_f15(user32, stop_event):
    if stop_event.is_set():
        return False
    user32.keybd_event(VK_F15, 0, 0, 0)
    try:
        stop_event.wait(0.02)
    finally:
        user32.keybd_event(VK_F15, 0, KEYEVENTF_KEYUP, 0)
    stop_event.wait(0.02)
    return not stop_event.is_set()


def run_idle_activity(stop_event):
    """Send up to 1,000 randomized F15 presses per idle batch, then cool down."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetLastInputInfo.argtypes = [ctypes.POINTER(LASTINPUTINFO)]
    user32.GetLastInputInfo.restype = wintypes.BOOL
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.keybd_event.restype = None
    kernel32.GetTickCount.argtypes = []
    kernel32.GetTickCount.restype = wintypes.DWORD

    human_last_tick = _last_input_tick(user32)
    last_known_tick = human_last_tick
    next_action_time = 0.0
    cooldown_until = 0.0
    clicks_in_batch = 0

    while not stop_event.is_set():
        current_tick = _last_input_tick(user32)
        if current_tick != last_known_tick:
            human_last_tick = current_tick
            last_known_tick = current_tick
            clicks_in_batch = 0
            cooldown_until = 0.0
            next_action_time = 0.0

        idle_time = (
            (int(kernel32.GetTickCount()) - human_last_tick) & 0xFFFFFFFF
        ) / 1000.0

        now = time.monotonic()
        if (
            idle_time >= IDLE_THRESHOLD_SECONDS
            and now >= cooldown_until
            and now >= next_action_time
        ):
            if _press_f15(user32, stop_event):
                clicks_in_batch += 1
                last_known_tick = _last_input_tick(user32)
                if clicks_in_batch >= IDLE_BATCH_CLICKS:
                    clicks_in_batch = 0
                    cooldown_until = time.monotonic() + IDLE_BATCH_COOLDOWN_SECONDS
                    next_action_time = cooldown_until
                else:
                    next_action_time = time.monotonic() + random.uniform(0.02, 0.35)

        stop_event.wait(0.05)


def add_f15_clicks(stop_event, count=MANUAL_BATCH_CLICKS,
                   interval=MANUAL_CLICK_INTERVAL_SECONDS):
    """Send a fixed F15 batch with a 0.15-second start-to-start interval."""
    if type(count) is not int or count <= 0 or interval < 0:
        raise ValueError("count must be positive and interval cannot be negative")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.keybd_event.argtypes = [
        wintypes.BYTE,
        wintypes.BYTE,
        wintypes.DWORD,
        ctypes.c_size_t,
    ]
    user32.keybd_event.restype = None

    completed = 0
    next_press_time = time.monotonic()
    while completed < count and not stop_event.is_set():
        wait_time = next_press_time - time.monotonic()
        if wait_time > 0 and stop_event.wait(wait_time):
            break
        if not _press_f15(user32, stop_event):
            break
        completed += 1
        next_press_time += interval
    return completed
