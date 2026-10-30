"""Synchronize the Wuwa Tappo WebView2 UI through its local CDP endpoint."""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.request
from urllib.parse import urlsplit
from itertools import count
from pathlib import Path

import psutil
import websockets
import winreg

from windows_auth import find_app_exe

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CDP_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
_COMMAND_IDS = count(1)

HIDE_SELECTOR = (
    ".reward-anchor, .reward-anchor--left, .reward-anchor--right, .reward-btn, "
    ".chest-reward-popup-layer, [class*=\"chest-reward-popup\"], "
    "[class*=\"reward-anchor\"], [class*=\"reward-btn\"]"
)

HIDE_CSS = HIDE_SELECTOR + (
    " { "
    "display: none !important; visibility: hidden !important; "
    "pointer-events: none !important; opacity: 0 !important; "
    "width: 0 !important; height: 0 !important; }"
)

HIDE_SCRIPT = """
(() => {
    let style = document.getElementById('wuwa-auto-claim-hide-chests');
    if (!style) {
        style = document.createElement('style');
        style.id = 'wuwa-auto-claim-hide-chests';
        (document.head || document.documentElement).appendChild(style);
    }
    style.textContent = __HIDE_CSS__;
    const elements = [...document.querySelectorAll(__HIDE_SELECTOR__)];
    if (!elements.length) return false;
    elements.forEach(el => {
        el.style.setProperty('display', 'none', 'important');
        el.style.setProperty('visibility', 'hidden', 'important');
        el.style.setProperty('pointer-events', 'none', 'important');
        el.style.setProperty('opacity', '0', 'important');
    });
    return elements.every(el => {
        const computed = window.getComputedStyle(el);
        return computed.display === 'none'
            && computed.visibility === 'hidden'
            && computed.pointerEvents === 'none';
    });
})();
""".replace("__HIDE_CSS__", json.dumps(HIDE_CSS)).replace(
    "__HIDE_SELECTOR__", json.dumps(HIDE_SELECTOR)
)

SHOW_SCRIPT = """
(() => {
    const style = document.getElementById('wuwa-auto-claim-hide-chests');
    if (style) style.remove();
    const elements = [...document.querySelectorAll(__HIDE_SELECTOR__)];
    elements.forEach(el => {
        for (const property of ['display', 'visibility', 'pointer-events', 'opacity']) {
            el.style.removeProperty(property);
        }
    });
    return !document.getElementById('wuwa-auto-claim-hide-chests')
        && elements.every(el =>
            ['display', 'visibility', 'pointer-events', 'opacity'].every(
                property => !el.style.getPropertyValue(property)
            )
        );
})()
""".replace("__HIDE_SELECTOR__", json.dumps(HIDE_SELECTOR))


def _with_cdp_argument(value):
    value = (value or "").strip()
    pattern = rf"(?:^|\s)--remote-debugging-port(?:=|\s+){CDP_PORT}(?:\s|$)"
    if not re.search(pattern, value):
        value = (value + f" --remote-debugging-port={CDP_PORT}").strip()
    return value


def ensure_webview2_cdp_env():
    """Set CDP for this helper and future Wuwa Tappo launches."""
    values = [os.environ.get(CDP_ENV, "").strip()]
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            try:
                stored, _ = winreg.QueryValueEx(key, CDP_ENV)
                values.append(str(stored).strip())
            except FileNotFoundError:
                pass
            combined = " ".join(dict.fromkeys(value for value in values if value))
            combined = _with_cdp_argument(combined)
            winreg.SetValueEx(key, CDP_ENV, 0, winreg.REG_SZ, combined)
    except OSError:
        combined = _with_cdp_argument(" ".join(dict.fromkeys(value for value in values if value)))
    os.environ[CDP_ENV] = combined

    # Refresh the environment block used by long-running Windows shells such as Explorer.
    try:
        import ctypes
        result = ctypes.c_size_t()
        send_message = ctypes.windll.user32.SendMessageTimeoutW
        send_message.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p,
            ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_size_t),
        ]
        send_message.restype = ctypes.c_ssize_t
        send_message(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))
    except Exception:
        pass
    return True


def get_page_targets():
    """Return Tappo page targets exposed by its loopback-only WebView2 endpoint."""
    try:
        request = urllib.request.Request(
            f"{CDP_URL}/json", headers={"User-Agent": "WuwaTappoAutoClaim"}
        )
        with urllib.request.urlopen(request, timeout=1.0) as response:
            targets = json.loads(response.read().decode("utf-8"))
        return [
            target for target in targets
            if target.get("type") == "page"
            and "tauri.localhost" in target.get("url", "")
            and target.get("webSocketDebuggerUrl")
        ]
    except (OSError, ValueError, TypeError):
        return []


async def _send_cdp(ws_url, method, params=None, timeout=3.0):
    """Send one CDP command and wait for its matching response, skipping events."""
    command_id = next(_COMMAND_IDS)
    try:
        async with websockets.connect(
            ws_url, close_timeout=1.0, open_timeout=1.5, max_size=4 * 1024 * 1024
        ) as socket:
            await socket.send(json.dumps({
                "id": command_id,
                "method": method,
                "params": params or {},
            }))
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
                response = json.loads(raw)
                if response.get("id") == command_id:
                    return response
    except Exception:
        return None


def _command_succeeded(response):
    return isinstance(response, dict) and "error" not in response


def _result_value(response):
    if not _command_succeeded(response):
        return None
    return response.get("result", {}).get("result", {}).get("value")


def _main_targets(targets):
    """Select only the main dashboard WebView, never login/toast/update routes."""
    matches = []
    for target in targets:
        parsed = urlsplit(target.get("url", ""))
        if (
            parsed.scheme == "http"
            and parsed.netloc == "tauri.localhost"
            and parsed.path in ("", "/", "/index.html")
            and parsed.fragment.strip("/") == ""
        ):
            matches.append(target)
    return matches


async def _apply_script(targets, script):
    responses = await asyncio.gather(*(
        _send_cdp(target["webSocketDebuggerUrl"], "Runtime.evaluate", {
            "expression": script,
            "returnByValue": True,
            "awaitPromise": True,
        })
        for target in targets
    ), return_exceptions=True)
    return bool(responses) and all(
        isinstance(response, dict) and _result_value(response) is True
        for response in responses
    )


def set_chests_hidden(hidden=True, timeout=5.0):
    """Hide or restore chest controls and report success only after DOM confirmation."""
    script = HIDE_SCRIPT if hidden else SHOW_SCRIPT
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        targets = _main_targets(get_page_targets())
        if targets:
            try:
                if asyncio.run(_apply_script(targets, script)):
                    return True
            except RuntimeError:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.35)


def set_tappo_click_state(gross, consumed, timeout=5.0):
    """Patch server-confirmed click totals into the live Vue/Pinia store."""
    if type(gross) is not int or type(consumed) is not int or gross < 0 or consumed < 0:
        return False

    script = """
    (async () => {
        const app = document.querySelector('#app');
        const pinia = app?.__vue_app__?.config?.globalProperties?.$pinia;
        const store = pinia?._s?.get('GlobalState');
        if (
            !store
            || !store.userInfo
            || typeof store.userInfo !== 'object'
            || typeof store.$patch !== 'function'
        ) return false;

        const pendingClicks = store.localClickCount;
        const pendingSpend = store.serverClickSpend;
        store.$patch({
            userInfo: {
                ...store.userInfo,
                total_click_count: __GROSS__,
                total_click_consume: __CONSUMED__
            }
        });
        if (
            store.userInfo.total_click_count !== __GROSS__
            || store.userInfo.total_click_consume !== __CONSUMED__
        ) return false;

        // Let Vue flush the Pinia update before checking Tappo's visible counter.
        await new Promise(resolve => setTimeout(resolve, 80));
        const expected = Number(store.totalClickCount);
        if (!Number.isFinite(expected)) return false;
        const expectedInteger = Math.trunc(expected);
        const expectedDigits = String(expectedInteger);
        const info = document.querySelector('#app .pet-info');
        if (!info) return false;

        const representsCount = text => {
            const compact = String(text || '').trim().replace(/[\\s,，]/g, '');
            const digits = compact.replace(/[^0-9]/g, '');
            if (digits === expectedDigits) return true;
            const match = compact.match(/^([0-9]+(?:\\.[0-9]+)?)([kKmM万亿])?$/);
            if (!match) return false;
            const scale = {
                '': 1, k: 1e3, m: 1e6, '万': 1e4, '亿': 1e8
            }[match[2] || ''];
            return Math.round(Number(match[1]) * scale) === expectedInteger;
        };
        const visibleCount = [...info.querySelectorAll('*')].some(el => {
            if (el.children.length) return false;
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && representsCount(el.textContent);
        });
        return visibleCount
            && store.localClickCount === pendingClicks
            && store.serverClickSpend === pendingSpend;
    })();
    """.replace('__GROSS__', json.dumps(gross)).replace('__CONSUMED__', json.dumps(consumed))

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        targets = _main_targets(get_page_targets())
        if targets:
            try:
                if asyncio.run(_apply_script(targets, script)):
                    return True
            except RuntimeError:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.35)


async def _command(socket, method, params=None, pending_events=None, timeout=3.0):
    command_id = next(_COMMAND_IDS)
    await socket.send(json.dumps({
        "id": command_id,
        "method": method,
        "params": params or {},
    }))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return None
        raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
        response = json.loads(raw)
        if response.get("id") == command_id:
            return response
        if pending_events is not None and response.get("method"):
            pending_events.append(response)


async def _reload_one_and_wait(ws_url, timeout):
    """Reload one WebView and wait for the new document's load lifecycle event."""
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        async with websockets.connect(
            ws_url, close_timeout=1.0, open_timeout=1.5, max_size=4 * 1024 * 1024
        ) as socket:
            pending_events = []

            def remaining():
                return max(0.0, deadline - asyncio.get_running_loop().time())

            if not _command_succeeded(await _command(socket, "Page.enable", timeout=remaining())):
                return False
            if not _command_succeeded(await _command(
                socket, "Page.setLifecycleEventsEnabled", {"enabled": True},
                pending_events=pending_events, timeout=remaining()
            )):
                return False
            tree_response = await _command(
                socket, "Page.getFrameTree",
                pending_events=pending_events, timeout=remaining()
            )
            if not _command_succeeded(tree_response):
                return False
            frame_tree = tree_response.get("result", {}).get("frameTree", {})
            main_frame = frame_tree.get("frame", {})
            main_frame_id = main_frame.get("id")
            old_loader = main_frame.get("loaderId")
            if not _command_succeeded(await _command(
                socket, "Page.reload", {"ignoreCache": False},
                pending_events=pending_events, timeout=remaining()
            )):
                return False

            def loaded_event(event):
                method = event.get("method")
                params = event.get("params", {})
                if method == "Page.frameNavigated":
                    frame = params.get("frame", {})
                    if frame.get("id") == main_frame_id and not frame.get("parentId"):
                        loader = frame.get("loaderId")
                        if loader and loader != old_loader:
                            return ("new_loader", loader)
                if (
                    method == "Page.lifecycleEvent"
                    and params.get("name") == "load"
                    and params.get("frameId") == main_frame_id
                ):
                    loader = params.get("loaderId")
                    if loader and loader != old_loader:
                        return ("loaded", loader)
                return None

            new_loader = None
            for event in pending_events:
                result = loaded_event(event)
                if result:
                    if result[0] == "loaded":
                        return True
                    new_loader = result[1]

            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                event = json.loads(await asyncio.wait_for(socket.recv(), timeout=remaining))
                result = loaded_event(event)
                if result:
                    if result[0] == "loaded":
                        return True
                    new_loader = result[1]
                elif (
                    event.get("method") == "Page.lifecycleEvent"
                    and event.get("params", {}).get("name") == "load"
                    and new_loader
                    and event.get("params", {}).get("loaderId") == new_loader
                ):
                    return True
    except Exception:
        return False


def reload_game_ui(timeout=12.0):
    """Reload Tappo and wait for its fresh page-load event."""
    targets = _main_targets(get_page_targets())
    if not targets:
        return False
    selected = targets

    async def _reload_all():
        results = await asyncio.gather(*(
            _reload_one_and_wait(target["webSocketDebuggerUrl"], timeout)
            for target in selected
        ), return_exceptions=True)
        return any(result is True for result in results)

    try:
        return asyncio.run(_reload_all())
    except RuntimeError:
        return False


def sync_after_claim(timeout=15.0, should_hide=lambda: True):
    """Reapply chest visibility after claim without reloading the Tappo page."""
    return set_chests_hidden(bool(should_hide()), timeout=timeout)


def restart_tappo_for_ui_sync(timeout=45.0, hide_chests=True):
    """Ensure Tappo exposes CDP, restarting it only when the live endpoint is unavailable."""
    ensure_webview2_cdp_env()

    # Do not restart an already synchronized app just because the user requested a re-sync.
    if get_page_targets() and set_chests_hidden(hide_chests, timeout=2.0):
        return True, 'Tappo interface is synchronized.'

    running = []
    found_running_app = False
    unresolved_process = False
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").lower() != "wuwatappo.exe":
                continue
            found_running_app = True
            running_path = process.info.get("exe")
            if running_path and Path(running_path).is_file():
                running.append((process, Path(running_path)))
            else:
                unresolved_process = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    if unresolved_process:
        return False, 'Windows found Wuwa Tappo but would not reveal its executable path. Close Tappo manually, then retry.'

    app_exe = running[0][1] if running else find_app_exe()
    if app_exe is None or not app_exe.is_file():
        return False, 'Could not find the installed Wuwa Tappo executable. Open Tappo and retry UI sync.'

    roots = [
        process for process, running_path in running
        if running_path.resolve() == app_exe.resolve()
    ]
    if found_running_app and not roots:
        return False, 'Wuwa Tappo is running but could not be safely restarted. Close it manually, then retry UI sync.'

    if roots:
        # Tappo persists pending clicks every five seconds; allow that write to finish.
        time.sleep(5.5)
        children_by_pid = {}
        try:
            for process in roots:
                for child in process.children(recursive=True):
                    children_by_pid[child.pid] = child
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False, 'Windows denied access to Tappo processes. Close Tappo manually, then retry UI sync.'
        children = list(children_by_pid.values())

        try:
            for process in roots:
                if process.is_running():
                    process.terminate()
            _, alive_roots = psutil.wait_procs(roots, timeout=8)
            if alive_roots:
                if get_page_targets() and set_chests_hidden(hide_chests, timeout=2.0):
                    return True, 'Tappo was already synchronized; leaving it open.'
                return False, 'Windows could not close Wuwa Tappo. Close it manually, then retry UI sync.'

            for child in children:
                try:
                    if child.is_running():
                        child.terminate()
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                    continue
            psutil.wait_procs(children, timeout=4)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            return False, 'Windows denied permission to restart Wuwa Tappo. Close it manually, then retry UI sync.'

    run_env = os.environ.copy()
    run_env[CDP_ENV] = _with_cdp_argument(run_env.get(CDP_ENV, ""))
    try:
        subprocess.Popen([str(app_exe)], cwd=str(app_exe.parent), env=run_env)
    except OSError as exc:
        return False, f'Could not relaunch Wuwa Tappo ({type(exc).__name__}). Open it manually, then retry UI sync.'

    deadline = time.monotonic() + max(0.0, timeout)
    last_target_count = 0
    while time.monotonic() < deadline:
        targets = get_page_targets()
        last_target_count = max(last_target_count, len(targets))
        if targets and set_chests_hidden(hide_chests, timeout=1.0):
            return True, 'Tappo restarted and interface synchronized.'
        time.sleep(0.5)

    if last_target_count:
        return False, 'Tappo reopened, but its page did not accept the UI sync command. Close it and retry.'
    return False, 'Tappo reopened without exposing CDP on port 9222. Check the WebView2 connection setting, then retry.'
