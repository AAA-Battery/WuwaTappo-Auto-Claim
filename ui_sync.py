"""Synchronize the Wuwa Tappo WebView2 UI through its local CDP endpoint."""
import asyncio
import json
import os
import re
import subprocess
import time
import urllib.request
import uuid
from urllib.parse import urlsplit
from itertools import count
from pathlib import Path

import psutil
import websockets
import winreg

from windows_auth import DATA_DIR, find_app_exe

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CDP_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
CDP_POLICY_KEY = r"SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments"
CDP_POLICY_BACKUP = DATA_DIR / "webview2-debug-policy-backup.json"
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
    try {
        let style = document.getElementById('wuwa-auto-claim-hide-chests');
        if (!style) {
            style = document.createElement('style');
            style.id = 'wuwa-auto-claim-hide-chests';
            (document.head || document.documentElement).appendChild(style);
        }
        style.textContent = __HIDE_CSS__;
        const elements = [...document.querySelectorAll(__HIDE_SELECTOR__)];
        elements.forEach(el => {
            el.style.setProperty('display', 'none', 'important');
            el.style.setProperty('visibility', 'hidden', 'important');
            el.style.setProperty('pointer-events', 'none', 'important');
            el.style.setProperty('opacity', '0', 'important');
        });
        const hidden = elements.every(el => {
            const computed = window.getComputedStyle(el);
            return computed.display === 'none'
                && computed.visibility === 'hidden'
                && computed.pointerEvents === 'none';
        });
        const info = document.querySelector('#app .pet-info');
        const dashboardReady = !!info && info.getClientRects().length > 0;
        const styleInstalled = style.isConnected && style.textContent.includes('display: none');
        return {
            dashboardReady,
            styleInstalled,
            matchedElements: elements.length,
            hidden: hidden && styleInstalled
        };
    } catch (e) {
        return false;
    }
})();
""".replace("__HIDE_CSS__", json.dumps(HIDE_CSS)).replace(
    "__HIDE_SELECTOR__", json.dumps(HIDE_SELECTOR)
)

SHOW_SCRIPT = """
(() => {
    try {
        const style = document.getElementById('wuwa-auto-claim-hide-chests');
        if (style) style.remove();
        const elements = [...document.querySelectorAll(__HIDE_SELECTOR__)];
        elements.forEach(el => {
            for (const property of ['display', 'visibility', 'pointer-events', 'opacity']) {
                el.style.removeProperty(property);
            }
        });
        const info = document.querySelector('#app .pet-info');
        return {
            dashboardReady: !!info && info.getClientRects().length > 0,
            restored: !document.getElementById('wuwa-auto-claim-hide-chests')
            && elements.every(el =>
                ['display', 'visibility', 'pointer-events', 'opacity'].every(
                    property => !el.style.getPropertyValue(property)
                )
            )
        };
    } catch (e) {
        return false;
    }
})();
""".replace("__HIDE_SELECTOR__", json.dumps(HIDE_SELECTOR))


def _with_cdp_argument(value):
    value = (value or "").strip()
    pattern = rf"(?:^|\s)--remote-debugging-port(?:=|\s+){CDP_PORT}(?:\s|$)"
    if not re.search(pattern, value):
        value = (value + f" --remote-debugging-port={CDP_PORT}").strip()
    return value


def ensure_webview2_cdp_env():
    """Set CDP only in this helper's environment; policy handles elevated Tappo."""
    combined = _with_cdp_argument(os.environ.get(CDP_ENV, ""))
    os.environ[CDP_ENV] = combined
    return True


def _policy_registry_access():
    access = winreg.KEY_READ | winreg.KEY_SET_VALUE
    access |= getattr(winreg, "KEY_WOW64_64KEY", 0)
    return access


def _read_policy_value(key, value_name):
    try:
        value, value_type = winreg.QueryValueEx(key, value_name)
        return True, value, value_type
    except FileNotFoundError:
        return False, None, None


def _restore_machine_cdp_policy():
    """Restore a prior machine policy left by an interrupted restart attempt."""
    if not CDP_POLICY_BACKUP.is_file():
        return True, ""

    try:
        state = json.loads(CDP_POLICY_BACKUP.read_text(encoding="utf-8"))
        value_name = state["value_name"]
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, CDP_POLICY_KEY, 0, _policy_registry_access()
        ) as key:
            exists, current_value, current_type = _read_policy_value(key, value_name)
            applied_value = state["applied_value"]
            applied_type = state["applied_type"]
            if exists and current_type == applied_type and current_value == applied_value:
                if state["previous_exists"]:
                    winreg.SetValueEx(
                        key, value_name, 0, state["previous_type"], state["previous_value"]
                    )
                else:
                    winreg.DeleteValue(key, value_name)
                if state.get("created_key"):
                    try:
                        subkeys, values, _ = winreg.QueryInfoKey(key)
                        if not subkeys and not values:
                            winreg.DeleteKeyEx(
                                winreg.HKEY_LOCAL_MACHINE,
                                CDP_POLICY_KEY,
                                getattr(winreg, "KEY_WOW64_64KEY", 0),
                                0,
                            )
                    except OSError:
                        pass
    except FileNotFoundError:
        pass
    except (OSError, ValueError, KeyError, TypeError) as exc:
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        detail = f"Windows error {code}" if code else type(exc).__name__
        return False, f"Could not restore the previous WebView2 machine policy ({detail})."

    try:
        CDP_POLICY_BACKUP.unlink(missing_ok=True)
    except OSError as exc:
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        detail = f"Windows error {code}" if code else type(exc).__name__
        return False, f"WebView2 policy was restored, but its recovery marker could not be removed ({detail})."
    return True, ""


def _enable_machine_cdp_policy(app_exe):
    """Temporarily configure this elevated WebView2 host to expose CDP."""
    recovered, detail = _restore_machine_cdp_policy()
    if not recovered:
        return None, detail

    value_name = Path(app_exe).name
    key_existed = True
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, CDP_POLICY_KEY, 0, _policy_registry_access()
        ):
            pass
    except FileNotFoundError:
        key_existed = False

    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_LOCAL_MACHINE, CDP_POLICY_KEY, 0, _policy_registry_access()
        ) as key:
            previous_exists, previous_value, previous_type = _read_policy_value(key, value_name)
            if previous_exists and previous_type not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ):
                return None, "The existing Wuwa Tappo WebView2 machine policy uses an unsupported registry type."
            previous_text = str(previous_value) if previous_exists else ""
            applied_value = _with_cdp_argument(previous_text)
            if previous_exists and applied_value == previous_value:
                return _restore_machine_cdp_policy, ""

            state = {
                "value_name": value_name,
                "previous_exists": previous_exists,
                "previous_value": previous_value if previous_exists else None,
                "previous_type": previous_type,
                "applied_value": applied_value,
                "applied_type": previous_type if previous_exists else winreg.REG_SZ,
                "created_key": not key_existed,
            }
            CDP_POLICY_BACKUP.parent.mkdir(parents=True, exist_ok=True)
            temporary = CDP_POLICY_BACKUP.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf-8")
            os.replace(temporary, CDP_POLICY_BACKUP)
            winreg.SetValueEx(
                key, value_name, 0, state["applied_type"], applied_value
            )
    except (OSError, ValueError, TypeError) as exc:
        CDP_POLICY_BACKUP.unlink(missing_ok=True)
        code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        detail = f"Windows error {code}" if code else type(exc).__name__
        return None, f"Could not set the temporary WebView2 policy for elevated Tappo ({detail})."

    return _restore_machine_cdp_policy, ""


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
        url = target.get("url", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme in ("http", "https")
            and "tauri.localhost" in parsed.netloc
            and parsed.path in ("", "/", "/index.html")
        ):
            frag = parsed.fragment.strip("/").lower()
            if not any(sub in frag for sub in ("login", "toast", "update", "notice", "about", "oss")):
                matches.append(target)
    return matches


async def _evaluate_script_values(targets, script):
    responses = await asyncio.gather(*(
        _send_cdp(target["webSocketDebuggerUrl"], "Runtime.evaluate", {
            "expression": script,
            "returnByValue": True,
            "awaitPromise": True,
        })
        for target in targets
    ), return_exceptions=True)
    return [
        _result_value(response) if isinstance(response, dict) else None
        for response in responses
    ]


async def _apply_dashboard_script(targets, script, state_key):
    results = await _evaluate_script_values(targets, script)
    dashboards = [
        result for result in results
        if isinstance(result, dict) and result.get("dashboardReady") is True
    ]
    return bool(dashboards) and all(result.get(state_key) is True for result in dashboards)


async def _apply_click_state_script(targets, script):
    results = await _evaluate_script_values(targets, script)
    stores = [
        result for result in results
        if isinstance(result, dict)
        and result.get("dashboardReady") is True
        and result.get("storeReady") is True
    ]
    return bool(stores) and all(
        result.get("clickCountMatches") is True
        and result.get("pendingStatePreserved") is True
        for result in stores
    )


async def _apply_dashboard_deduction(targets, script):
    results = await _evaluate_script_values(targets, script)
    stores = [
        result for result in results
        if isinstance(result, dict)
        and result.get("dashboardReady") is True
        and result.get("storeReady") is True
    ]
    return bool(stores) and all(result.get("deducted") is True for result in stores)


def set_chests_hidden(hidden=True, timeout=5.0):
    """Hide or restore chest controls and report success only after DOM confirmation."""
    script = HIDE_SCRIPT if hidden else SHOW_SCRIPT
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        targets = _main_targets(get_page_targets())
        if targets:
            try:
                state_key = "hidden" if hidden else "restored"
                if asyncio.run(_apply_dashboard_script(targets, script, state_key)):
                    return True
            except RuntimeError:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.35)




def deduct_tappo_clicks(amount=1000, timeout=5.0):
    """Immediately decrement available clicks in the live Vue/Pinia store upon claim."""
    if type(amount) is not int or amount <= 0:
        return False
    claim_id = uuid.uuid4().hex

    script = """
    (() => {
        try {
            const app = document.querySelector('#app');
            const info = document.querySelector('#app .pet-info');
            const dashboardReady = !!info && info.getClientRects().length > 0;
            const pinia = app?.__vue_app__?.config?.globalProperties?.$pinia;
            const store = pinia?._s?.get('GlobalState');
            if (!dashboardReady) return {dashboardReady: false};
            if (
                !store || !store.userInfo || typeof store.userInfo !== 'object'
                || typeof store.$patch !== 'function'
            ) return {dashboardReady: true, storeReady: false};

            const appliedIds = Array.isArray(store.__wuwaAutoClaimDeductionIds)
                ? store.__wuwaAutoClaimDeductionIds
                : [];
            if (appliedIds.includes(__CLAIM_ID__)) {
                return {dashboardReady: true, storeReady: true, deducted: true, alreadyApplied: true};
            }

            const currentConsume = Number(store.userInfo.total_click_consume || 0);
            if (!Number.isFinite(currentConsume)) {
                return {dashboardReady: true, storeReady: false};
            }
            const newConsume = currentConsume + __AMOUNT__;
            store.$patch({
                userInfo: {
                    ...store.userInfo,
                    total_click_consume: newConsume
                }
            });
            const deducted = Number(store.userInfo.total_click_consume) === newConsume;
            if (deducted) {
                store.__wuwaAutoClaimDeductionIds = [...appliedIds, __CLAIM_ID__].slice(-32);
            }
            return {dashboardReady: true, storeReady: true, deducted};
        } catch (e) {
            return false;
        }
    })();
    """.replace('__AMOUNT__', json.dumps(amount)).replace(
        '__CLAIM_ID__', json.dumps(claim_id)
    )

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        targets = _main_targets(get_page_targets())
        if targets:
            try:
                if asyncio.run(_apply_dashboard_deduction(targets, script)):
                    return True
            except RuntimeError:
                return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.35)

def set_tappo_click_state(gross, consumed, timeout=5.0):
    """Patch server-confirmed click totals into the live Vue/Pinia store."""
    if (
        type(gross) is not int or type(consumed) is not int
        or gross < 0 or consumed < 0 or gross < consumed
    ):
        return False

    script = """
    (async () => {
        const app = document.querySelector('#app');
        const info = document.querySelector('#app .pet-info');
        const dashboardReady = !!info && info.getClientRects().length > 0;
        const pinia = app?.__vue_app__?.config?.globalProperties?.$pinia;
        const store = pinia?._s?.get('GlobalState');
        if (!dashboardReady) return {dashboardReady: false};
        if (
            !store
            || !store.userInfo
            || typeof store.userInfo !== 'object'
            || typeof store.$patch !== 'function'
        ) return {dashboardReady: true, storeReady: false};

        const previousGross = Number(store.userInfo.total_click_count ?? 0);
        const previousConsumed = Number(store.userInfo.total_click_consume ?? 0);
        if (
            !Number.isFinite(previousGross)
            || !Number.isFinite(previousConsumed)
            || __GROSS__ < previousGross
            || __CONSUMED__ < previousConsumed
        ) return {dashboardReady: true, storeReady: false, serverTotalsRegressed: true};

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
        ) return {dashboardReady: true, storeReady: false};

        // Let Vue flush the Pinia update before checking Tappo's visible counter.
        await new Promise(resolve => setTimeout(resolve, 80));
        const expected = Number(store.totalClickCount);
        if (!Number.isFinite(expected)) return {dashboardReady: true, storeReady: false};
        const expectedInteger = Math.trunc(expected);
        const expectedDigits = String(expectedInteger);

        const representsCount = text => {
            const compact = String(text || '').trim().replace(/[\\s,，]/g, '');
            const digits = compact.replace(/[^0-9]/g, '');
            if (digits === expectedDigits) return true;
            const match = compact.match(/^([0-9]+(?:\\.[0-9]+)?)([kKmM万亿])?$/);
            if (!match) return false;
            const scale = {
                '': 1, k: 1e3, m: 1e6, '万': 1e4, '亿': 1e8
            }[match[2] || ''];
            const decimals = (match[1].split('.')[1] || '').length;
            const displayed = Number(match[1]) * scale;
            const roundingTolerance = 0.5 * scale / (10 ** decimals);
            return Math.abs(displayed - expectedInteger) <= roundingTolerance;
        };
        const countElements = [info, ...info.querySelectorAll('*')].filter(
            el => el.children.length === 0
        );
        const visibleCount = countElements.some(el => {
            const style = window.getComputedStyle(el);
            const rect = el.getBoundingClientRect();
            return style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0
                && representsCount(el.textContent);
        });
        return {
            dashboardReady: true,
            storeReady: true,
            clickCountMatches: visibleCount,
            pendingStatePreserved: store.localClickCount === pendingClicks
                && store.serverClickSpend === pendingSpend
        };
    })();
    """.replace('__GROSS__', json.dumps(gross)).replace('__CONSUMED__', json.dumps(consumed))

    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        targets = _main_targets(get_page_targets())
        if targets:
            try:
                if asyncio.run(_apply_click_state_script(targets, script)):
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




def _is_admin_process():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _os_error_detail(exc):
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    detail = getattr(exc, "strerror", None) or type(exc).__name__
    return f"Windows error {code}: {detail}" if code else detail


def _tappo_processes():
    found = []
    for process in psutil.process_iter(["name"]):
        try:
            if (process.info.get("name") or "").lower() == "wuwatappo.exe":
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return found


def _webview2_cdp_process_state():
    """Return safe counts only; never include browser command lines in logs."""
    runtime_count = 0
    cdp_argument_seen = False
    for process in psutil.process_iter(["name", "cmdline"]):
        try:
            if (process.info.get("name") or "").lower() != "msedgewebview2.exe":
                continue
            runtime_count += 1
            command_line = process.info.get("cmdline") or []
            if any(
                argument == f"--remote-debugging-port={CDP_PORT}"
                or argument == f"--remote-debugging-port {CDP_PORT}"
                for argument in command_line
            ):
                cdp_argument_seen = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return runtime_count, cdp_argument_seen


def launch_tappo_process(app_exe):
    """Launch the verified installed Tappo executable with the CDP environment."""
    ensure_webview2_cdp_env()
    if not _is_admin_process():
        return False, "WuwaTappoAutoClaim is not elevated. Close it, run it again, and accept the Administrator prompt."

    try:
        app_exe = Path(app_exe).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return False, f"Could not locate Wuwa Tappo to reopen ({_os_error_detail(exc)}). Open Tappo manually and retry."
    if not app_exe.is_file():
        return False, "The detected Wuwa Tappo executable is no longer present. Open Tappo manually and retry UI sync."

    run_env = os.environ.copy()
    run_env[CDP_ENV] = _with_cdp_argument(run_env.get(CDP_ENV, ""))
    try:
        subprocess.Popen([str(app_exe)], cwd=str(app_exe.parent), env=run_env)
        return True, ""
    except OSError as exc:
        popen_error = _os_error_detail(exc)

    # Some installers mark the Tappo executable as administrator-only even when
    # this helper is already elevated. Ask ShellExecute to apply that manifest.
    shell_code = 0
    try:
        import ctypes
        from ctypes import wintypes
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        shell.ShellExecuteW.argtypes = [
            wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int
        ]
        shell.ShellExecuteW.restype = ctypes.c_void_p
        result = shell.ShellExecuteW(
            None, "runas", str(app_exe), None, str(app_exe.parent), 1
        )
        shell_code = int(result or 0)
        if shell_code > 32:
            return True, ""
    except Exception as exc:
        shell_code = ctypes.get_last_error() if "ctypes" in locals() else 0
        shell_error = type(exc).__name__
    else:
        shell_error = ""

    if shell_code == 1223:
        return False, "Windows cancelled the UAC request to reopen Wuwa Tappo. Accept the prompt and retry UI sync."
    extra = f"; ShellExecute code {shell_code}" if shell_code else ""
    if shell_error:
        extra += f" ({shell_error})"
    return False, f"Could not relaunch Wuwa Tappo ({popen_error}{extra}). Check its installation and allow the UAC prompt."


def elevated_kill_tappo():
    """Stop Tappo from the elevated helper and return actionable error details."""
    if not _is_admin_process():
        return False, "WuwaTappoAutoClaim is not running as Administrator. Restart it and accept the UAC prompt."
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/IM", "WuwaTappo.exe", "/T"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Windows could not stop Wuwa Tappo ({_os_error_detail(exc)}). Close it manually, then retry."

    if not _tappo_processes():
        return True, ""
    detail = (result.stderr or result.stdout or "").strip()
    if len(detail) > 180:
        detail = detail[:177] + "..."
    suffix = f"; {detail}" if detail else ""
    return False, f"Windows could not stop Wuwa Tappo (taskkill exit {result.returncode}{suffix}). Close it manually, then retry."


def restart_tappo_for_ui_sync(timeout=45.0, hide_chests=True):
    """Enable CDP and restart Tappo only when the endpoint is unavailable."""
    if not _is_admin_process():
        return False, "WuwaTappoAutoClaim must run as Administrator to restart Wuwa Tappo. Close and reopen it, then accept UAC."
    recovered, detail = _restore_machine_cdp_policy()
    if not recovered:
        return False, detail
    ensure_webview2_cdp_env()

    existing_targets = get_page_targets()
    main_targets = _main_targets(existing_targets)
    if main_targets:
        if set_chests_hidden(hide_chests, timeout=2.0):
            return True, "Tappo interface is synchronized."
        return False, "Tappo is connected, but its dashboard did not confirm the chest-control change. Check the Tappo version and retry."
    if existing_targets:
        return False, "Tappo is connected, but its main dashboard page was not recognized. Open the Tappo dashboard and retry UI sync."

    running = []
    found_running_app = False
    for process in psutil.process_iter(["name", "exe"]):
        try:
            if (process.info.get("name") or "").lower() != "wuwatappo.exe":
                continue
            found_running_app = True
            running_path = process.info.get("exe")
            if running_path:
                candidate = Path(running_path)
                if candidate.is_file():
                    running.append((process, candidate))
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    app_exe = running[0][1] if running else find_app_exe()
    if app_exe is None or not app_exe.is_file():
        if found_running_app:
            return False, "Windows found Wuwa Tappo but could not identify its installed EXE. Reinstall or open Tappo from its installed shortcut, then retry."
        return False, "Could not find the Wuwa Tappo executable. Open Tappo once and retry UI sync."
    try:
        app_exe = Path(app_exe).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        return False, f"Could not verify the Wuwa Tappo executable ({_os_error_detail(exc)}). Open Tappo manually and retry."

    roots = [
        process for process, running_path in running
        if running_path.resolve() == app_exe
    ]
    if found_running_app:
        # Tappo saves pending clicks periodically; allow that write to finish.
        time.sleep(5.5)
        killed = False
        if roots:
            children_by_pid = {}
            try:
                for process in roots:
                    for child in process.children(recursive=True):
                        children_by_pid[child.pid] = child
                for process in roots:
                    if process.is_running():
                        process.terminate()
                _, alive_roots = psutil.wait_procs(roots, timeout=8)
                if not alive_roots:
                    for child in children_by_pid.values():
                        try:
                            if child.is_running():
                                child.terminate()
                        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                            pass
                    psutil.wait_procs(list(children_by_pid.values()), timeout=4)
                    killed = True
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                killed = False

        if not killed:
            killed, detail = elevated_kill_tappo()
            if not killed:
                return False, detail

    if _tappo_processes():
        killed, detail = elevated_kill_tappo()
        if not killed:
            return False, detail

    restore_policy, detail = _enable_machine_cdp_policy(app_exe)
    if restore_policy is None:
        return False, detail

    success = False
    result_detail = ""
    last_target_count = 0
    try:
        ok, err_msg = launch_tappo_process(app_exe)
        if not ok:
            result_detail = err_msg
        else:
            deadline = time.monotonic() + max(0.0, timeout)
            while time.monotonic() < deadline:
                targets = get_page_targets()
                last_target_count = max(last_target_count, len(targets))
                if _main_targets(targets) and set_chests_hidden(hide_chests, timeout=1.0):
                    success = True
                    result_detail = "Tappo restarted and interface synchronized."
                    break
                time.sleep(0.5)

            if not success and last_target_count:
                result_detail = "Tappo reopened, but its dashboard did not confirm the UI sync. Check the Tappo version and activity log."
            elif not success:
                if not _tappo_processes():
                    result_detail = "Wuwa Tappo closed after launch before its dashboard became available. Reopen it manually and check its update or startup prompt."
                else:
                    runtime_count, cdp_argument_seen = _webview2_cdp_process_state()
                    if cdp_argument_seen:
                        result_detail = (
                            f"Tappo is open and WebView2 received the debugging flag, but port {CDP_PORT} did not expose its dashboard. "
                            "Check whether another app is using that port or whether the Tappo/WebView2 version changed."
                        )
                    else:
                        result_detail = (
                            f"Tappo is open, but WebView2 did not receive the debugging flag "
                            f"({runtime_count} WebView2 process(es) found). Check the WebView2 Runtime and Tappo version."
                        )
    finally:
        restored, restore_detail = restore_policy()

    if not restored:
        return False, restore_detail
    return success, result_detail
