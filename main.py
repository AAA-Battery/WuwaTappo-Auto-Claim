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
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from datetime import datetime
import pystray
from PIL import Image

from chest_autoclaim_ui import ChestAutoClaimUI

from windows_auth import DATA_DIR, AUTH_FILE, AuthError, load_session, pair_existing_session, find_app_exe
from core import ApiClient, ApiError, TransportError, Engine, AUTH_CODES
from ui_sync import set_chests_hidden, set_tappo_click_state, deduct_tappo_clicks, sync_after_claim, ensure_webview2_cdp_env, restart_tappo_for_ui_sync, launch_tappo_process
from idle_activity import MANUAL_CLICK_INTERVAL_SECONDS, run_idle_activity, add_f15_clicks

VERSION = '1.2.2'
STOP_FILE = DATA_DIR / 'stop.flag'
SHOW_FILE = DATA_DIR / 'show.flag'
CONFIG_FILE = DATA_DIR / 'config.json'
STATUS_FILE = DATA_DIR / 'status.json'
STARTUP_MUTEX_NAME = 'Local\\WuwaTappoAutoClaim-Startup-v1'

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

def read_config():
    try:
        config = json.loads(CONFIG_FILE.read_text(encoding='utf8'))
        return config if isinstance(config, dict) else {}
    except (OSError, ValueError):
        return {}

def write_config(enabled=None, click_when_idle=None):
    config = read_config()
    if enabled is not None:
        config['enabled'] = bool(enabled)
    if click_when_idle is not None:
        config['click_when_idle'] = bool(click_when_idle)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(config), encoding='utf8')
    os.replace(temporary, CONFIG_FILE)

def read_instance_status():
    try:
        value = json.loads(STATUS_FILE.read_text(encoding='utf8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}

def version_key(value):
    try:
        parts = tuple(int(part) for part in str(value).split('.'))
        return parts + (0,) * max(0, 3 - len(parts))
    except (TypeError, ValueError):
        return ()

def write_instance_status(value):
    status = dict(value)
    status.update(version=VERSION, pid=os.getpid(), updated_at=time.time())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(status, ensure_ascii=False), encoding='utf8')
    os.replace(temporary, STATUS_FILE)

def _current_session_id(pid):
    session_id = wintypes.DWORD()
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    if not kernel.ProcessIdToSessionId(pid, ctypes.byref(session_id)):
        return None
    return int(session_id.value)

def _running_helper_pids():
    import psutil

    current_session = _current_session_id(os.getpid())
    if current_session is None:
        return set()
    pids = set()
    for process in psutil.process_iter(['pid', 'name']):
        try:
            pid = int(process.info['pid'])
            name = (process.info.get('name') or '').lower()
            if pid == os.getpid() or name != 'wuwatappoautoclaim.exe':
                continue
            if _current_session_id(pid) == current_session:
                pids.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, TypeError, ValueError):
            continue

    # Also recognize source-mode instances, but only trust a fresh status marker.
    status = read_instance_status()
    try:
        pid = int(status.get('pid', 0))
        fresh = 0 <= time.time() - float(status.get('updated_at', 0)) <= 3.0
    except (TypeError, ValueError):
        pid, fresh = 0, False
    if fresh and pid and pid != os.getpid() and pid not in pids:
        try:
            process = psutil.Process(pid)
            if (
                current_session is not None
                and _current_session_id(pid) == current_session
                and process.name().lower() in ('python.exe', 'pythonw.exe')
                and any(Path(arg).name.lower() == 'main.py' for arg in process.cmdline())
            ):
                pids.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    # New builds publish their PID/version heartbeat. Trust that PID even if a
    # user renamed the EXE, while still limiting it to this Windows session.
    status = read_instance_status()
    try:
        pid = int(status.get('pid', 0))
        has_version = bool(version_key(status.get('version')))
    except (TypeError, ValueError):
        pid, has_version = 0, False
    if has_version and pid and pid != os.getpid() and pid not in pids:
        try:
            if current_session is not None and _current_session_id(pid) == current_session:
                psutil.Process(pid)
                pids.add(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    return pids

def _legacy_logged_version():
    """Read the latest version banner written by a pre-handoff build."""
    pattern = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) .*Tool started v([0-9]+(?:\.[0-9]+)+)')
    latest = None
    for path in (DATA_DIR / 'activity.log', DATA_DIR / 'activity.log.1', DATA_DIR / 'activity.log.2'):
        try:
            lines = path.read_text(encoding='utf8', errors='replace').splitlines()
        except OSError:
            continue
        for line in lines:
            match = pattern.match(line)
            if not match:
                continue
            try:
                stamp = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S,%f').timestamp()
            except ValueError:
                continue
            if latest is None or stamp > latest[0]:
                latest = (stamp, match.group(2))
    return latest[1] if latest else None

def _running_instance_version(pids):
    status = read_instance_status()
    try:
        pid = int(status.get('pid', 0))
        version = status.get('version')
        if pid in pids and version_key(version):
            return str(version)
    except (TypeError, ValueError):
        pass
    return _legacy_logged_version()

def _stop_previous_helpers(grace_seconds=60.0):
    import psutil

    pids = _running_helper_pids()
    if not pids:
        return True

    STOP_FILE.write_text('upgrade', encoding='ascii')
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _running_helper_pids():
            STOP_FILE.unlink(missing_ok=True)
            return True
        time.sleep(0.1)

    # A legacy build may be hung and unable to process stop.flag. Close only
    # helper processes identified in this Windows session, then let the new one run.
    remaining = _running_helper_pids()
    processes = []
    for pid in remaining:
        try:
            process = psutil.Process(pid)
            process.terminate()
            processes.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    _, alive = psutil.wait_procs(processes, timeout=4.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            pass
    _, alive = psutil.wait_procs(alive, timeout=3.0)
    STOP_FILE.unlink(missing_ok=True)
    return not alive and not _running_helper_pids()

def show_startup_error(message):
    try:
        ctypes.windll.user32.MessageBoxW(None, message, 'Wuwa Tappo Auto-Claim', 0x10)
    except Exception:
        pass

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

def elevate_before_start():
    """Run the helper with a full Administrator token before touching Tappo."""
    try:
        if ctypes.windll.shell32.IsUserAnAdmin():
            return 'continue'
    except Exception:
        pass

    executable = sys.executable
    if getattr(sys, 'frozen', False):
        parameters = subprocess.list2cmdline(sys.argv[1:])
        working_directory = str(Path(executable).resolve().parent)
    else:
        script = str(Path(__file__).resolve())
        parameters = subprocess.list2cmdline([script, *sys.argv[1:]])
        working_directory = str(Path(script).parent)

    try:
        shell = ctypes.WinDLL('shell32', use_last_error=True)
        shell.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        result = shell.ShellExecuteW(
            None, 'runas', executable, parameters, working_directory, 1
        )
        if result and result > 32:
            return 'relaunched'
        error = ctypes.get_last_error() or int(result or 0)
    except Exception as exc:
        error = getattr(exc, 'winerror', None) or ctypes.get_last_error()

    if error == 1223:
        message = 'Administrator permission was cancelled. Run WuwaTappoAutoClaim again and choose Yes in the Windows prompt.'
    else:
        message = f'WuwaTappoAutoClaim could not request Administrator permission (Windows error {error or "unknown"}).'
    try:
        ctypes.windll.user32.MessageBoxW(None, message, 'Wuwa Tappo Auto-Claim', 0x10)
    except Exception:
        pass
    return 'failed'

class TrayApp:
    def __init__(self, enabled=False, minimized=False, click_when_idle=False):
        ensure_webview2_cdp_env()
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
        self.server_balance = None
        self.last_server_click_totals = None
        self.status_data = {}
        self.ui_sync_warned = False
        self.ui_restart_attempted = False
        self.ui_restart_running = False
        self.ui_sync_failure = threading.Event()
        self.tappo_click_sync_pending = False
        self.tappo_click_sync_inflight = False
        self.tappo_click_sync_generation = 0
        self.tappo_claim_generation = 0
        self.tappo_click_sync_lock = threading.Lock()
        self.ui_exit_sync_done = threading.Event()
        self.stopping = False
        self.click_when_idle = bool(click_when_idle)
        self.idle_click_stop = threading.Event()
        self.idle_click_thread = None
        self.add_clicks_running = False
        self.add_click_stop = threading.Event()
        self.add_click_thread = None
        self.root = tk.Tk()
        self.window_icon = tk.PhotoImage(file=str(resource_path('Zani.png')))
        self.root.iconphoto(True, self.window_icon)
        self.status_text = 'Ready' if AUTH_FILE.exists() else 'Please pair with a logged-in Wuwa Tappo session to start.'
        self.rarity_counts = {'epic': 0, 'rare': 0, 'uncommon': 0, 'common': 0}
        self.ui = ChestAutoClaimUI(
            self.root,
            on_toggle_pause=self.toggle,
            on_repair=self.pair,
            on_minimize=self.hide,
            on_open_log=self.open_log_file,
            on_resync=self.prompt_tappo_restart,
            on_exit=self.quit,
            on_toggle_idle_click=self.toggle_click_when_idle,
            on_add_clicks=self.add_1000_clicks,
        )
        self.ui.set_auto_claim_state(False)
        self.ui.set_click_when_idle(self.click_when_idle)
        self.ui.set_status(self.status_text, 'paused' if AUTH_FILE.exists() else 'error')
        self.ui.set_available_clicks(None)
        self.ui.set_claimed(0)
        self.ui.set_last_claim(None)
        self.ui.set_timers('—:—', '—:—')
        
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
        self.root.update_idletasks()
        
        self.thread = threading.Thread(target=self.worker, name='chest-worker', daemon=True)
        self.thread.start()
        if self.click_when_idle:
            self._start_idle_clicker()
        if enabled:
            self.set_enabled(True)
        if minimized:
            self.root.withdraw()
        self.root.after(300, self.tick)
        (DATA_DIR / 'startup-error.txt').unlink(missing_ok=True)
        self.log.info('Tool started v%s', VERSION)

    def set_status(self, text, state=None):
        self.status_text = str(text)
        if state is None:
            lower = self.status_text.lower()
            if any(term in lower for term in (
                'error', 'failed', 'could not', 'unavailable', 'not enough',
                'expired', 'rejected', 'uncertain', 'temporarily', 'need to pair',
                'please pair', 'timed out'
            )):
                state = 'error'
            elif any(term in lower for term in ('waiting', 'checking', 'syncing', 'pairing', 'finishing')):
                state = 'waiting'
            elif self.active.is_set():
                state = 'active'
            else:
                state = 'paused'
        self.ui.set_status(self.status_text, state)

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

    def set_enabled(self, enabled, paused=False):
        if enabled and not AUTH_FILE.exists():
            self.set_status('Need to pair with Wuwa Tappo first.', 'error')
            self.ui.set_auto_claim_state(False)
            return
        if enabled:
            self.active.set()
            self.set_status('Checking server time...', 'waiting')
            self.sync_chests(True)
        else:
            self.active.clear()
            self.set_status('Paused. No new claim requests will be sent.', 'paused')
            self.sync_chests(False)
        write_config(enabled)
        self.ui.set_auto_claim_state(enabled, paused=paused)
        self.tray.title = 'Wuwa Tappo • ' + ('Auto-Claim Active' if enabled else 'Paused')
        self.wake.set()

    def toggle_click_when_idle(self):
        if self.stopping:
            return
        self.click_when_idle = not self.click_when_idle
        self.ui.set_click_when_idle(self.click_when_idle)
        write_config(click_when_idle=self.click_when_idle)
        if self.click_when_idle:
            self._start_idle_clicker()
            self.log.info('Click when idle enabled (F15 after 15 seconds without input).')
        else:
            self.idle_click_stop.set()
            self.log.info('Click when idle disabled.')

    def _start_idle_clicker(self):
        if self.add_clicks_running:
            return
        if self.idle_click_thread and self.idle_click_thread.is_alive():
            if not self.idle_click_stop.is_set():
                return
            self.idle_click_thread.join(timeout=0.25)
            if self.idle_click_thread.is_alive():
                return
        self.idle_click_stop.clear()
        self.idle_click_thread = threading.Thread(
            target=self._run_idle_clicker,
            name='click-when-idle',
            daemon=True,
        )
        self.idle_click_thread.start()

    def _run_idle_clicker(self):
        try:
            run_idle_activity(self.idle_click_stop)
        except Exception:
            self.log.exception('Click when idle stopped after an input error.')
            self.messages.put(('idle_click_failed', {}))

    def add_1000_clicks(self):
        if self.stopping or self.add_clicks_running:
            return
        self.add_clicks_running = True
        self.ui.set_add_clicks_running(True)
        self.add_click_stop.clear()
        self.idle_click_stop.set()
        self.log.info(
            'Manual F15 batch started (1000 presses, %.2f-second interval).',
            MANUAL_CLICK_INTERVAL_SECONDS,
        )
        self.add_click_thread = threading.Thread(
            target=self._run_manual_click_batch,
            name='manual-1000-clicks',
            daemon=True,
        )
        self.add_click_thread.start()

    def _run_manual_click_batch(self):
        completed = 0
        error = None
        try:
            if self.idle_click_thread and self.idle_click_thread.is_alive():
                self.idle_click_thread.join(timeout=1.0)
            if self.idle_click_thread and self.idle_click_thread.is_alive():
                raise RuntimeError('The idle click worker did not stop before the manual batch.')
            completed = add_f15_clicks(
                self.add_click_stop,
                count=1000,
                interval=MANUAL_CLICK_INTERVAL_SECONDS,
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            self.log.exception('Manual F15 batch stopped after %s presses.', completed)
        self.messages.put((
            'add_clicks_finished',
            {'completed': completed, 'error': error},
        ))

    def sync_chests(self, hidden=True, after_claim=False):
        def run():
            if after_claim:
                success = sync_after_claim(should_hide=lambda: self.active.is_set())
                should_hide = self.active.is_set()
            else:
                success = set_chests_hidden(hidden)
                should_hide = hidden
            if success:
                if self.ui_sync_failure.is_set():
                    self.ui_sync_failure.clear()
                    self.messages.put(('ui_sync_available', {}))
            elif should_hide and not self.ui_sync_failure.is_set():
                self.ui_sync_failure.set()
                self.messages.put(('ui_sync_unavailable', {}))
        threading.Thread(target=run, name='tappo-ui-sync', daemon=True).start()

    def deduct_clicks_ui(self, amount=1000):
        def run():
            success = deduct_tappo_clicks(amount, timeout=3.0)
            if success:
                self.log.info('Immediate Tappo click deduction applied once (%s clicks).', amount)
            else:
                self.log.warning('Immediate Tappo click deduction could not be confirmed (%s clicks).', amount)
        threading.Thread(target=run, name='tappo-click-deduct', daemon=True).start()

    def sync_tappo_click_state(self, gross, consumed):
        self.tappo_click_sync_generation += 1
        generation = self.tappo_click_sync_generation
        claim_generation = self.tappo_claim_generation
        self.tappo_click_sync_inflight = True
        self.log.info(
            'Tappo click display sync requested (server gross=%s consumed=%s available=%s)',
            gross,
            consumed,
            max(0, gross - consumed),
        )

        def run():
            with self.tappo_click_sync_lock:
                if generation != self.tappo_click_sync_generation:
                    return
                success = set_tappo_click_state(gross, consumed, timeout=5.0)
            self.messages.put(('tappo_click_sync_result', {
                'generation': generation,
                'claim_generation': claim_generation,
                'success': success,
            }))

        threading.Thread(target=run, name='tappo-click-sync', daemon=True).start()

    def prompt_tappo_restart(self):
        """Re-sync now; restart Tappo only if its live CDP endpoint is unavailable."""
        self.start_tappo_restart(automatic=False)

    def start_tappo_restart(self, automatic=False):
        if self.ui_restart_running or self.stopping:
            return
        self.ui_restart_running = True
        self.ui_restart_attempted = True
        self.ui.set_resyncing(True)
        if automatic:
            self.set_status('Tappo UI sync is unavailable; restarting Tappo after its save interval...')
        else:
            self.set_status('Re-syncing Tappo; it will restart only if needed...')
        threading.Thread(target=self.restart_app_for_ui_sync, name='tappo-ui-restart', daemon=True).start()

    def restart_app_for_ui_sync(self):
        success, detail = restart_tappo_for_ui_sync(hide_chests=self.active.is_set())
        self.log.info('UI sync restart %s: %s', 'succeeded' if success else 'failed', detail)
        self.messages.put((
            'ui_sync_restart_succeeded' if success else 'ui_sync_restart_failed',
            {'detail': detail},
        ))

    def toggle(self):
        if not self.pairing:
            enabled = not self.active.is_set()
            self.set_enabled(enabled, paused=not enabled)

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
        self.ui.set_pairing(True)
        self.set_status('Pairing in progress; Wuwa Tappo will restart...', 'waiting')
        executable = sys.executable
        args = ['--pair'] if getattr(sys, 'frozen', False) else [str(Path(__file__).resolve()), '--pair']
        try:
            subprocess.Popen(
                [executable, *args],
                cwd=str(Path(executable).resolve().parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except OSError as exc:
            self.pairing = False
            self.ui.set_pairing(False)
            code = getattr(exc, 'winerror', None) or getattr(exc, 'errno', None)
            detail = f'Windows error {code}: {exc.strerror}' if code else type(exc).__name__
            self.set_status(f'Could not start pairing ({detail}).', 'error')

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
                        self.sync_chests(True)
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
                    self.ui.set_pairing(False)
                    self.messages.put(('paired' if data['state'] == 'paired' else 'pair_error', data))
            except (OSError, ValueError, KeyError):
                pass
            if time.time() - self.pair_started > 90:
                self.pairing = False
                self.ui.set_pairing(False)
                self.set_status('Pairing timed out; please check Wuwa Tappo.', 'error')
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
                self.ui.set_pairing(False)
                app_exe_str = data.get('app_exe') or data.get('detail')
                app_exe = Path(app_exe_str) if app_exe_str else find_app_exe()
                if app_exe and app_exe.is_file() and not app_running():
                    launched, detail = launch_tappo_process(app_exe)
                    if not launched:
                        self.set_status(f'Pairing successful, but Wuwa Tappo did not reopen: {detail}', 'error')
                    else:
                        self.set_status('Pairing successful. Click Enable Auto-Claim to start.', 'active')
                else:
                    self.set_status('Pairing successful. Click Enable Auto-Claim to start.', 'active')
            elif event == 'pair_error':
                self.ui.set_pairing(False)
                app_exe_str = data.get('app_exe')
                app_exe = Path(app_exe_str) if app_exe_str else None
                restart_note = ''
                if app_exe and app_exe.is_file() and not app_running():
                    launched, launch_detail = launch_tappo_process(app_exe)
                    if not launched:
                        restart_note = f' Wuwa Tappo also failed to reopen: {launch_detail}'
                self.set_status(data.get('detail', 'Pairing failed.') + restart_note, 'error')
            elif event == 'status':
                self.countdowns = (time.monotonic(), data['left'], data['right'])
                self.server_balance = data['balance']
                self.ui.set_available_clicks(self.server_balance)
                click_totals = (data['gross'], data['consumed'])
                if click_totals != self.last_server_click_totals:
                    self.last_server_click_totals = click_totals
                    self.log.info(
                        'Server click totals received (gross=%s consumed=%s available=%s)',
                        data['gross'], data['consumed'], data['balance'],
                    )
                if self.tappo_click_sync_pending and not self.tappo_click_sync_inflight:
                    self.sync_tappo_click_state(data['gross'], data['consumed'])
                if self.active.is_set() and not self.ui_sync_warned:
                    self.set_status('Waiting for server cooldown...')
            elif event == 'tappo_click_sync_result':
                if data['generation'] == self.tappo_click_sync_generation:
                    self.tappo_click_sync_inflight = False
                    if data['success'] and data['claim_generation'] == self.tappo_claim_generation:
                        self.tappo_click_sync_pending = False
                        self.log.info('Tappo click display sync confirmed.')
                    elif not data['success']:
                        self.log.warning(
                            'Could not confirm the Tappo click display update for server totals gross=%s consumed=%s; it will be retried.',
                            self.last_server_click_totals[0] if self.last_server_click_totals else None,
                            self.last_server_click_totals[1] if self.last_server_click_totals else None,
                        )
                        if self.tappo_click_sync_pending and self.active.is_set():
                            self.set_status('Tappo click display update is pending; retrying...')
            elif event == 'idle_click_failed':
                self.click_when_idle = False
                self.idle_click_stop.set()
                write_config(click_when_idle=False)
                self.ui.set_click_when_idle(False)
            elif event == 'add_clicks_finished':
                self.add_clicks_running = False
                self.ui.set_add_clicks_running(False)
                if data.get('error'):
                    self.log.error(
                        'Manual F15 batch stopped at %s/1000 presses: %s',
                        data.get('completed', 0), data['error'],
                    )
                else:
                    self.log.info(
                        'Manual F15 batch completed (%s/1000 presses).',
                        data.get('completed', 0),
                    )
                if self.click_when_idle and not self.stopping:
                    self._start_idle_clicker()
            elif event == 'need_clicks' and self.active.is_set():
                self.set_status('Not enough clicks (need >= 1,000). Keep clicking in the app.')
            elif event == 'ui_sync_unavailable':
                self.ui_sync_warned = True
                if not self.ui_restart_attempted and not self.ui_restart_running:
                    self.set_status('Tappo UI sync is unavailable; restarting Tappo automatically...')
                    self.root.after(100, lambda: self.start_tappo_restart(automatic=True))
                else:
                    self.set_status('Tappo UI sync is unavailable. Click Re-sync Tappo UI to try again.')
            elif event == 'ui_sync_available':
                self.ui_sync_warned = False
                self.ui_restart_attempted = False
                if self.active.is_set():
                    self.set_status('Tappo interface synchronized; waiting for server cooldown...')
            elif event == 'ui_sync_restart_succeeded':
                self.ui_sync_failure.clear()
                self.ui_restart_running = False
                self.ui_restart_attempted = False
                self.ui.set_resyncing(False)
                self.ui_sync_warned = False
                self.set_status(data.get('detail', 'Tappo interface synchronized.'))
            elif event == 'ui_sync_restart_failed':
                self.ui_restart_running = False
                self.ui.set_resyncing(False)
                self.ui_sync_warned = True
                self.set_status(data.get('detail', 'Could not synchronize Tappo. Click Re-sync Tappo UI to try again.'))
            elif event == 'claimed':
                self.claim_count += 1
                self.tappo_claim_generation += 1
                self.tappo_click_sync_pending = True
                if self.server_balance is not None:
                    self.server_balance = max(0, self.server_balance - 1000)
                    self.ui.set_available_clicks(self.server_balance)
                self.deduct_clicks_ui(1000)
                self.wake.set()
                kind = {'character_skin': 'Character', 'accessory': 'Accessory', 'emote': 'Emote'}.get(data.get('asset_type'), 'New Item')
                rarity = str(data.get('rarity', '')).strip().lower()
                if rarity in self.rarity_counts:
                    self.rarity_counts[rarity] += 1
                self.ui.set_claimed(self.claim_count, **self.rarity_counts)
                slot = {'left': 'Left chest', 'right': 'Right chest'}.get(data.get('slot'), 'Chest')
                item_name = str(data.get('asset_name', '')).strip()
                detail = f'{slot} · {item_name}' if item_name else slot
                self.ui.set_last_claim(rarity or None, kind, detail)
                self.set_status('Chest claimed; syncing clicks and chest display without reloading...')
                self.sync_chests(after_claim=True)
            elif event == 'app_closed':
                self.set_status('Waiting for Wuwa Tappo to open...')
            elif event == 'auth_needed':
                self.set_enabled(False)
                self.set_status('Session expired or account changed. Please re-pair.')
            elif event == 'claim_rejected':
                self.set_status(f"Server returned code {data['code']}; waiting before retry.")
            elif event == 'claim_uncertain':
                self.set_status('Claim status uncertain; checking server before retrying.')
            elif event == 'network_error':
                self.set_status('Connection error. Tool will retry automatically with backoff.')
            elif event in ('api_error', 'internal_error'):
                self.set_status('Data temporarily unavailable. Check activity log.')
        if self.countdowns:
            elapsed = time.monotonic() - self.countdowns[0]
            def fmt(value):
                seconds = max(0, int(value - elapsed + 0.999))
                return 'Ready' if seconds == 0 else f'{seconds // 60:02d}:{seconds % 60:02d}'
            self.ui.set_timers(fmt(self.countdowns[1]), fmt(self.countdowns[2]))
        self.status_data = {
            'enabled': self.active.is_set(),
            'status': self.status_text,
            'claims_this_session': self.claim_count,
            'window_height': self.root.winfo_height(),
            'required_height': self.root.winfo_reqheight()
        }
        try:
            write_instance_status(self.status_data)
        except OSError:
            pass
        self.root.after(500, self.tick)

    def quit(self):
        if self.stopping:
            return
        self.stopping = True
        self.active.clear()
        self.idle_click_stop.set()
        self.add_click_stop.set()
        self.exit_event.set()
        self.wake.set()
        def restore_chests_before_exit():
            try:
                set_chests_hidden(False, timeout=3.0)
            finally:
                self.ui_exit_sync_done.set()
        threading.Thread(target=restore_chests_before_exit, name='tappo-ui-restore', daemon=True).start()
        self.set_status('Finishing current request before exit...')
        def finish():
            if (
                self.thread.is_alive()
                or (self.idle_click_thread and self.idle_click_thread.is_alive())
                or (self.add_click_thread and self.add_click_thread.is_alive())
                or not self.ui_exit_sync_done.is_set()
            ):
                self.root.after(200, finish)
                return
            self.log.info('Tool stopped')
            write_instance_status({'enabled': False, 'status': 'Exited'})
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
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.ReleaseMutex.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL

    startup_lock = kernel.CreateMutexW(None, False, STARTUP_MUTEX_NAME)
    if not startup_lock:
        show_startup_error('Could not coordinate WuwaTappoAutoClaim startup. Please try again.')
        return 1
    singleton = None
    owns_startup_lock = False
    try:
        wait_result = kernel.WaitForSingleObject(startup_lock, 90_000)
        if wait_result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            show_startup_error('Another WuwaTappoAutoClaim upgrade is still running. Please try again shortly.')
            return 1
        owns_startup_lock = True

        ctypes.set_last_error(0)
        singleton = kernel.CreateMutexW(None, False, 'Local\\WuwaTappoAutoClaim-v1')
        if not singleton:
            show_startup_error('Could not create the WuwaTappoAutoClaim single-instance lock.')
            return 1
        existing_instance = ctypes.get_last_error() == 183
        if existing_instance:
            previous_pids = _running_helper_pids()
            previous_version = _running_instance_version(previous_pids)
            if previous_version and version_key(previous_version) >= version_key(VERSION):
                SHOW_FILE.write_text('show', encoding='ascii')
                return 0
            if not previous_pids:
                show_startup_error(
                    'An older WuwaTappoAutoClaim session is holding the single-instance lock, '
                    'but Windows could not identify its process. Close it manually, then start this version again.'
                )
                return 1
            if not _stop_previous_helpers():
                show_startup_error(
                    'The older WuwaTappoAutoClaim session did not close. Close it manually, then start this version again.'
                )
                return 1

        STOP_FILE.unlink(missing_ok=True)
        SHOW_FILE.unlink(missing_ok=True)
        write_instance_status({'enabled': False, 'status': f'Starting v{VERSION}'})
        if not kernel.ReleaseMutex(startup_lock):
            show_startup_error('Could not release the WuwaTappoAutoClaim startup lock.')
            return 1
        owns_startup_lock = False

        config = read_config()
        enabled = args.enable or bool(config.get('enabled', False))
        click_when_idle = bool(config.get('click_when_idle', False))
        TrayApp(enabled, args.start_minimized, click_when_idle).run()
    finally:
        if owns_startup_lock:
            kernel.ReleaseMutex(startup_lock)
        if singleton:
            kernel.CloseHandle(singleton)
        kernel.CloseHandle(startup_lock)
    return 0

if __name__ == '__main__':
    elevation = elevate_before_start()
    if elevation == 'relaunched':
        sys.exit(0)
    if elevation != 'continue':
        sys.exit(1)
    try:
        sys.exit(main())
    except Exception as exc:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        import traceback
        (DATA_DIR / 'startup-error.txt').write_text(traceback.format_exc(), encoding='utf8')
        sys.exit(1)
