"""Portable Windows tray app for legitimate Wuwa Tappo chest claiming."""
import argparse
import ctypes
from ctypes import wintypes
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import pystray
from PIL import Image

from windows_auth import DATA_DIR, AUTH_FILE, AuthError, load_session, pair_existing_session
from core import ApiClient, ApiError, TransportError, Engine, AUTH_CODES
from ui_sync import set_chests_hidden, reload_game_ui, ensure_webview2_cdp_env

VERSION = '1.0.2'
STOP_FILE = DATA_DIR / 'stop.flag'
SHOW_FILE = DATA_DIR / 'show.flag'
CONFIG_FILE = DATA_DIR / 'config.json'

def resource_path(name):
    """Find bundled resources in source runs and PyInstaller one-directory builds."""
    candidates = []
    frozen_root = getattr(sys, '_MEIPASS', None)
    if frozen_root:
        candidates.append(Path(frozen_root) / name)
    source_dir = Path(__file__).resolve().parent
    candidates.extend((source_dir / name, source_dir.parent / name))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f'Bundled resource not found: {name}')

def write_config(enabled):
    CONFIG_FILE.write_text(json.dumps({'enabled': bool(enabled)}), encoding='utf8')

def app_running():
    import psutil
    return any((p.info['name'] or '').lower() == 'wuwatappo.exe' for p in psutil.process_iter(['name']))

def current_cuid():
    # Read-only account association, never use this local file as a click source.
    path = Path(os.environ.get('APPDATA', '')) / 'com.kuro.wuwatappo.global' / 'cumulative_click_global-global-prod.dat'
    key = b'kuro-mc-cumulative-click-v1'
    try:
        raw = path.read_bytes()
        decoded = bytes(v ^ key[i % len(key)] for i, v in enumerate(raw))
        return str(json.loads(decoded).get('cuid', '')).strip() or None
    except (OSError, ValueError, UnicodeError):
        return None

def configure_log():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('tappo-auto-claim')
    log.setLevel(logging.INFO)
    handler = RotatingFileHandler(DATA_DIR / 'activity.log', maxBytes=1_000_000, backupCount=2, encoding='utf8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    log.addHandler(handler)
    return log

class TrayApp:
    def __init__(self, enabled=False, minimized=False):
        self.log = configure_log()
        self.messages = queue.Queue()
        self.exit_event, self.wake = threading.Event(), threading.Event()
        self.active = threading.Event()
        self.session_generation = 0
        self.worker_generation = 0
        self.client = None
        self.engine = None
        self.pairing = False
        self.pair_started = 0
        self.claim_count = 0
        self.countdowns = None
        self.status_data = {}
        self.stopping = False
        self.root = tk.Tk()
        self.root.title('Wuwa Tappo • Chest Auto-Claim')
        self.window_icon = tk.PhotoImage(file=str(resource_path('Zani.png')))
        self.root.iconphoto(True, self.window_icon)
        self.root.geometry('600x540')
        self.root.minsize(560, 540)
        self.root.configure(bg='#121b2a')
        style = ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('TFrame', background='#121b2a')
        style.configure('TLabel', background='#121b2a', foreground='#e4edf6', font=('Segoe UI', 10))
        style.configure('Title.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Muted.TLabel', foreground='#a7bacd')
        style.configure('Count.TLabel', font=('Segoe UI', 17, 'bold'), foreground='#63d6b2')
        style.configure('TButton', font=('Segoe UI', 10), padding=(12, 8))
        
        frame = ttk.Frame(self.root, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Chest Auto-Claim', style='Title.TLabel').pack(anchor='w')
        ttk.Label(frame, text='Wuwa Tappo  ·  Synchronized with official server cooldown', style='Muted.TLabel').pack(anchor='w', pady=(4, 18))
        
        self.status = tk.StringVar(value='Ready' if AUTH_FILE.exists() else 'Please pair with a logged-in Wuwa Tappo session to start.')
        ttk.Label(frame, textvariable=self.status, wraplength=510).pack(anchor='w', pady=(0, 14))
        
        row = ttk.Frame(frame)
        row.pack(fill='x')
        self.left = tk.StringVar(value='—')
        self.right = tk.StringVar(value='—')
        for title, var in [('LEFT CHEST', self.left), ('RIGHT CHEST', self.right)]:
            box = ttk.Frame(row)
            box.pack(side='left', expand=True, fill='x')
            ttk.Label(box, text=title, style='Muted.TLabel').pack(anchor='w')
            ttk.Label(box, textvariable=var, style='Count.TLabel').pack(anchor='w', pady=(6, 12))
            
        self.balance = tk.StringVar(value='Available server clicks: —')
        self.latest = tk.StringVar(value='No chests claimed in this session yet.')
        ttk.Label(frame, textvariable=self.balance).pack(anchor='w')
        ttk.Label(frame, textvariable=self.latest, wraplength=510, style='Muted.TLabel').pack(anchor='w', pady=(8, 16))
        
        controls = ttk.Frame(frame)
        controls.pack(fill='x')
        self.toggle_button = ttk.Button(controls, text='Enable Auto-Claim', command=self.toggle)
        self.toggle_button.pack(side='left')
        self.pair_button = ttk.Button(controls, text='Re-pair Session', command=self.pair)
        self.pair_button.pack(side='left', padx=8)
        ttk.Button(controls, text='Minimize to Tray', command=self.hide).pack(side='left')
        
        footer = ttk.Frame(frame)
        footer.pack(fill='x', pady=(18, 0))
        ttk.Button(footer, text='Open Activity Log', command=self.open_log_file).pack(side='left')
        ttk.Button(footer, text='Exit', command=self.quit).pack(side='right')
        
        ttk.Label(frame, text='Keep Wuwa Tappo running. Never changes system time or fabricates clicks.\nClosing this window minimizes to the system tray.', style='Muted.TLabel').pack(anchor='w', pady=(12, 0))
        
        self.tray_icon_image = Image.open(resource_path('Zani.png')).convert('RGBA')
        self.tray_icon_image.thumbnail((64, 64), Image.Resampling.LANCZOS)
        
        self.tray = pystray.Icon(
            'WuwaTappoAutoClaim', self.tray_icon_image, 'Wuwa Tappo • Chest Auto-Claim',
            menu=pystray.Menu(
                pystray.MenuItem('Open Dashboard', lambda *_: self.messages.put(('show', {})), default=True),
                pystray.MenuItem('Enable / Pause', lambda *_: self.messages.put(('toggle', {}))),
                pystray.MenuItem('Exit', lambda *_: self.messages.put(('quit', {})))
            )
        )
        self.tray.run_detached()
        self.root.protocol('WM_DELETE_WINDOW', self.hide)
        self.root.update_idletasks()
        self.root.geometry(f'600x{max(540, self.root.winfo_reqheight() + 20)}')
        
        self.thread = threading.Thread(target=self.worker, name='chest-worker', daemon=True)
        self.thread.start()
        if enabled:
            self.set_enabled(True)
        if minimized:
            self.root.withdraw()
        self.root.after(300, self.tick)
        (DATA_DIR / 'startup-error.txt').unlink(missing_ok=True)
        ensure_webview2_cdp_env()
        self.log.info('Tool started v%s', VERSION)

    def open_log_file(self):
        log_file = DATA_DIR / 'activity.log'
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not log_file.exists():
            log_file.touch()
        try:
            os.startfile(str(log_file))
        except Exception:
            pass

    def emit(self, event, **data):
        self.messages.put((event, data))
        if event not in ('status', 'need_clicks'):
            self.log.info('%s %s', event, json.dumps(data, ensure_ascii=False))

    def set_enabled(self, enabled):
        if enabled and not AUTH_FILE.exists():
            self.status.set('Need to pair with Wuwa Tappo first.')
            return
        if enabled:
            self.active.set()
            self.status.set('Checking server time...')
            threading.Thread(target=set_chests_hidden, args=(True,), daemon=True).start()
        else:
            self.active.clear()
            self.status.set('Paused. No new claim requests will be sent.')
            threading.Thread(target=set_chests_hidden, args=(False,), daemon=True).start()
        write_config(enabled)
        self.toggle_button.configure(text='Pause Auto-Claim' if enabled else 'Enable Auto-Claim')
        self.tray.title = 'Wuwa Tappo • ' + ('Auto-Claim Active' if enabled else 'Paused')
        self.wake.set()

    def toggle(self):
        if not self.pairing:
            self.set_enabled(not self.active.is_set())

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.attributes('-topmost', True)
        self.root.after_idle(self.root.attributes, '-topmost', False)
        self.root.focus_force()

    def pair(self):
        if self.pairing or self.stopping:
            return
        from tkinter import messagebox
        if not messagebox.askokcancel(
            'Pair Wuwa Tappo',
            'Pairing will restart Wuwa Tappo once to read the session cookie.\n'
            'Please log into the app first. Session is securely saved using Windows DPAPI.\n\nContinue?',
            parent=self.root
        ):
            return
        self.set_enabled(False)
        self.pairing = True
        self.pair_started = time.time()
        self.pair_button.configure(state='disabled')
        self.status.set('Pairing in progress; Wuwa Tappo will restart...')
        executable = sys.executable
        args = ['--pair'] if getattr(sys, 'frozen', False) else [str(Path(__file__).resolve()), '--pair']
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.ShellExecuteW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        result = shell.ShellExecuteW(None, 'runas', executable, subprocess.list2cmdline(args), None, 0)
        if not result or result <= 32:
            self.pairing = False
            self.pair_button.configure(state='normal')
            self.status.set('Could not launch elevated pairing process.')

    def reset_client(self):
        if self.client:
            self.client.close()
        self.client = None
        self.engine = None

    def worker(self):
        backoff = 30
        while not self.exit_event.is_set():
            if not self.active.is_set():
                self.wake.wait(1)
                self.wake.clear()
                continue
            delay = 30
            try:
                if not app_running():
                    self.emit('app_closed')
                    delay = 10
                else:
                    if self.worker_generation != self.session_generation:
                        self.reset_client()
                        self.worker_generation = self.session_generation
                    if self.client is None:
                        self.client = ApiClient(load_session()['token'])
                        self.engine = Engine(
                            self.client, DATA_DIR / 'claim-journal.json', self.emit, current_cuid,
                            should_claim=lambda: self.active.is_set() and not self.exit_event.is_set()
                        )
                    delay = self.engine.step()
                    backoff = 30
                    if self.active.is_set():
                        threading.Thread(target=set_chests_hidden, args=(True,), daemon=True).start()
            except AuthError:
                self.active.clear()
                self.reset_client()
                self.emit('auth_needed')
            except ApiError as exc:
                if exc.code in AUTH_CODES:
                    self.active.clear()
                    self.reset_client()
                    self.emit('auth_needed')
                else:
                    self.emit('api_error', code=exc.code)
                    delay = min(backoff, 300)
                    backoff = min(backoff * 2, 300)
            except TransportError as exc:
                self.emit('network_error', detail=str(exc))
                delay = min(backoff, 300)
                backoff = min(backoff * 2, 300)
            except Exception as exc:
                # Never log response bodies, request headers, or exception locals.
                self.emit('internal_error', kind=type(exc).__name__)
                delay = 60
            self.wake.wait(delay)
            self.wake.clear()
        self.reset_client()

    def tick(self):
        if self.stopping:
            return
        if STOP_FILE.exists():
            STOP_FILE.unlink(missing_ok=True)
            self.quit()
            return
        if SHOW_FILE.exists():
            SHOW_FILE.unlink(missing_ok=True)
            self.show()
        if self.pairing:
            try:
                data = json.loads((DATA_DIR / 'pair-status.json').read_text('utf8'))
                if data.get('at', 0) >= self.pair_started and data['state'] != 'pairing':
                    self.pairing = False
                    self.pair_button.configure(state='normal')
                    self.messages.put(('paired' if data['state'] == 'paired' else 'pair_error', data))
            except (OSError, ValueError, KeyError):
                pass
            if time.time() - self.pair_started > 90:
                self.pairing = False
                self.pair_button.configure(state='normal')
                self.status.set('Pairing timed out; please check Wuwa Tappo.')
        while not self.messages.empty():
            event, data = self.messages.get()
            if event == 'show':
                self.show()
            elif event == 'toggle':
                self.toggle()
            elif event == 'quit':
                self.quit()
                return
            elif event == 'paired':
                self.session_generation += 1
                self.status.set('Pairing successful. Click Enable Auto-Claim to start.')
            elif event == 'pair_error':
                self.status.set(data.get('detail', 'Pairing failed.'))
            elif event == 'status':
                self.countdowns = (time.monotonic(), data['left'], data['right'])
                self.balance.set(f"Available server clicks: {data['balance']:,}")
                if self.active.is_set():
                    self.status.set('Waiting for server cooldown...')
            elif event == 'need_clicks' and self.active.is_set():
                self.status.set('Not enough clicks (need >= 1,000). Keep clicking in the app.')
            elif event == 'claimed':
                self.claim_count += 1
                kind = {'character_skin': 'Character', 'accessory': 'Accessory', 'emote': 'Emote'}.get(data.get('asset_type'), 'New Item')
                self.latest.set(f'Claimed {self.claim_count} chest(s) in this session • {kind}')
                self.status.set('Chest claimed; updating next cooldown...')
                def _sync_claimed():
                    reload_game_ui()
                    time.sleep(1.0)
                    set_chests_hidden(True)
                threading.Thread(target=_sync_claimed, daemon=True).start()
            elif event == 'app_closed':
                self.status.set('Waiting for Wuwa Tappo to open...')
            elif event == 'auth_needed':
                self.set_enabled(False)
                self.status.set('Session expired or account changed. Please re-pair.')
            elif event == 'claim_rejected':
                self.status.set(f"Server returned code {data['code']}; waiting before retry.")
            elif event == 'claim_uncertain':
                self.status.set('Claim status uncertain; checking server before retrying.')
            elif event == 'network_error':
                self.status.set('Connection error. Tool will retry automatically with backoff.')
            elif event in ('api_error', 'internal_error'):
                self.status.set('Data temporarily unavailable. Check activity log.')
        if self.countdowns:
            elapsed = time.monotonic() - self.countdowns[0]
            def fmt(value):
                seconds = max(0, int(value - elapsed + 0.999))
                return 'Ready' if seconds == 0 else f'{seconds // 60:02d}:{seconds % 60:02d}'
            self.left.set(fmt(self.countdowns[1]))
            self.right.set(fmt(self.countdowns[2]))
        self.status_data = {
            'enabled': self.active.is_set(),
            'status': self.status.get(),
            'claims_this_session': self.claim_count,
            'updated_at': time.time(),
            'window_height': self.root.winfo_height(),
            'required_height': self.root.winfo_reqheight()
        }
        try:
            (DATA_DIR / 'status.json').write_text(json.dumps(self.status_data, ensure_ascii=False), encoding='utf8')
        except OSError:
            pass
        self.root.after(500, self.tick)

    def quit(self):
        if self.stopping:
            return
        self.stopping = True
        self.active.clear()
        self.exit_event.set()
        self.wake.set()
        threading.Thread(target=set_chests_hidden, args=(False,), daemon=True).start()
        self.status.set('Finishing current request before exit...')
        def finish():
            if self.thread.is_alive():
                self.root.after(200, finish)
                return
            self.log.info('Tool stopped')
            (DATA_DIR / 'status.json').write_text(
                json.dumps({'enabled': False, 'status': 'Exited', 'updated_at': time.time()}, ensure_ascii=False),
                encoding='utf8'
            )
            try:
                self.tray.stop()
            finally:
                self.root.destroy()
        finish()

    def run(self):
        self.root.mainloop()

def main():
    parser = argparse.ArgumentParser(description='Wuwa Tappo Chest Auto-Claim')
    parser.add_argument('--pair', action='store_true', help='Pair session with running Wuwa Tappo')
    parser.add_argument('--stop', action='store_true', help='Signal running instance to exit')
    parser.add_argument('--enable', action='store_true', help='Enable auto-claim immediately on launch')
    parser.add_argument('--start-minimized', action='store_true', help='Start minimized in system tray')
    parser.add_argument('--diagnose', action='store_true', help='Perform read-only diagnostic check')
    args = parser.parse_args()
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if args.pair:
        try:
            pair_existing_session()
            return 0
        except AuthError:
            return 1
    if args.stop:
        STOP_FILE.write_text('stop', encoding='ascii')
        return 0
    if args.diagnose:
        api = ApiClient(load_session()['token'])
        state = Engine(api, DATA_DIR / 'claim-journal.json', expected_cuid=current_cuid).inspect()
        result = {
            'authenticated': True,
            'balance': state.balance,
            'left_seconds': round(state.deadlines['left'] - api.clock.now()),
            'right_seconds': round(state.deadlines['right'] - api.clock.now())
        }
        (DATA_DIR / 'diagnostics.json').write_text(json.dumps(result), encoding='utf8')
        api.close()
        return 0

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    handle = kernel.CreateMutexW(None, False, 'Local\\WuwaTappoAutoClaim-v1')
    if not handle:
        return 1
    if ctypes.get_last_error() == 183:
        SHOW_FILE.write_text('show', encoding='ascii')
        return 0
    STOP_FILE.unlink(missing_ok=True)
    enabled = args.enable
    if not enabled:
        try:
            enabled = bool(json.loads(CONFIG_FILE.read_text('utf8')).get('enabled', False))
        except (OSError, ValueError):
            pass
    try:
        TrayApp(enabled, args.start_minimized).run()
    finally:
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle(handle)
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        import traceback
        (DATA_DIR / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf8')
        sys.exit(1)

