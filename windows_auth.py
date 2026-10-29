"""Use only the existing Wuwa Tappo session; credentials never enter logs."""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

DATA_DIR = Path(os.environ['LOCALAPPDATA']) / 'WuwaTappoAutoClaim'
PROFILE = Path(os.environ['LOCALAPPDATA']) / 'com.kuro.wuwatappo.global' / 'EBWebView'
AUTH_FILE = DATA_DIR / 'session.dpapi'

class AuthError(Exception):
    pass

class DATA_BLOB(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]

def dpapi(data: bytes, protect=False) -> bytes:
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(DATA_BLOB)]
    fn.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    buf = ctypes.create_string_buffer(data)
    incoming = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = DATA_BLOB()
    if not fn(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise AuthError('Windows could not decrypt the session for this account.')
    try:
        return ctypes.string_at(outgoing.pbData, outgoing.cbData)
    finally:
        kernel.LocalFree(outgoing.pbData)

def load_session() -> dict:
    try:
        value = json.loads(dpapi(AUTH_FILE.read_bytes()))
        if not isinstance(value.get('token'), str) or not value['token']:
            raise ValueError()
        return value
    except (OSError, ValueError, AuthError):
        raise AuthError('Need to pair with a logged-in Wuwa Tappo session.') from None

def save_session(token: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps({'token': token, 'saved_at': time.time()}).encode()
    temp = AUTH_FILE.with_suffix('.tmp')
    temp.write_bytes(dpapi(data, protect=True))
    os.replace(temp, AUTH_FILE)

def read_cookie_session() -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    local_state_file = PROFILE / 'Local State'
    if not local_state_file.exists():
        raise AuthError('Wuwa Tappo WebView2 profile not found. Please run and log into the game first.')
    state = json.loads(local_state_file.read_text('utf-8'))
    wrapped = base64.b64decode(state['os_crypt']['encrypted_key'])
    if not wrapped.startswith(b'DPAPI'):
        raise AuthError('WebView2 session protection format not supported.')
    master = dpapi(wrapped[5:])
    cookie_db = PROFILE / 'Default' / 'Network' / 'Cookies'
    if not cookie_db.exists():
        raise AuthError('Cookie database not found. Please log into Wuwa Tappo first.')
    uri = cookie_db.as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True, timeout=5) as db:
        version_row = db.execute("select value from meta where key='version'").fetchone()
        version = int(version_row[0]) if version_row else 0
        rows = db.execute("select host_key,value,encrypted_value from cookies where name='kuro_infoToken'").fetchall()
    for host, plain, encrypted in rows:
        if host.lstrip('.') not in ('tauri.localhost', 'localhost'):
            continue
        if plain:
            return plain
        if encrypted[:3] not in (b'v10', b'v11'):
            raise AuthError('Unsupported WebView2 encryption format.')
        decoded = AESGCM(master).decrypt(encrypted[3:15], encrypted[15:], None)
        if version >= 24:
            expected = hashlib.sha256(host.encode()).digest()
            if decoded[:32] != expected:
                raise AuthError('Session domain mismatch.')
            decoded = decoded[32:]
        token = decoded.decode('utf-8')
        if token:
            return token
    raise AuthError('Wuwa Tappo session token not found. Please log into the app first.')

def find_app_exe() -> Path | None:
    candidates = [
        Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'WuwaTappoGlobal' / 'WuwaTappo.exe',
        Path(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)')) / 'WuwaTappoGlobal' / 'WuwaTappo.exe',
        Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs' / 'WuwaTappoGlobal' / 'WuwaTappo.exe',
    ]
    for c in candidates:
        if c.exists():
            return c
    return None

def pair_existing_session():
    """Elevated, bounded pairing. Restart only the verified installed Tappo."""
    import psutil
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise AuthError('Pairing requires Administrator privileges to restart Wuwa Tappo.')
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    status = DATA_DIR / 'pair-status.json'
    def record(state, detail=''):
        status.write_text(json.dumps({'state': state, 'detail': detail, 'at': time.time()}), encoding='utf-8')
    record('pairing')
    roots = []
    stopped = False
    app_exe_path = None
    try:
        for proc in psutil.process_iter(['name', 'exe']):
            try:
                if (proc.info['name'] or '').lower() == 'wuwatappo.exe':
                    exe = proc.info.get('exe') or proc.exe()
                    if exe:
                        app_exe_path = Path(exe)
                    roots.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if not app_exe_path:
            app_exe_path = find_app_exe()

        descendants = [child for proc in roots for child in proc.children(recursive=True)]
        if roots:
            # The app persists pending clicks every five seconds.
            time.sleep(5.5)
            stopped = True
            for proc in roots:
                proc.terminate()
            psutil.wait_procs(roots, timeout=5)
            _, alive = psutil.wait_procs(descendants, timeout=4)
            for child in alive:
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            psutil.wait_procs(alive, timeout=3)
        token = read_cookie_session()
        save_session(token)
        from ui_sync import ensure_webview2_cdp_env
        ensure_webview2_cdp_env()
        record('paired')
    except AuthError as exc:
        record('error', str(exc))
        raise
    except Exception:
        record('error', 'Could not read login session; please try pairing again.')
        raise AuthError('Could not read login session; please try pairing again.') from None
    finally:
        if stopped and app_exe_path and app_exe_path.exists():
            try:
                run_env = os.environ.copy()
                run_env['WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS'] = '--remote-debugging-port=9222'
                subprocess.Popen([str(app_exe_path)], cwd=str(app_exe_path.parent), env=run_env)
            except Exception:
                record('restart-error', 'Pairing complete. Please reopen Wuwa Tappo.')
