"""Synchronizes and controls Wuwa Tappo frontend UI via WebView2 CDP."""
import asyncio
import json
import logging
from pathlib import Path
import urllib.request
import urllib.error
import winreg
import websockets

CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"

HIDE_CSS = (
    ".reward-anchor, .reward-anchor--left, .reward-anchor--right, .reward-btn, "
    "[class*='reward-anchor'], [class*='reward-btn'] { "
    "display: none !important; "
    "visibility: hidden !important; "
    "pointer-events: none !important; "
    "opacity: 0 !important; "
    "width: 0 !important; "
    "height: 0 !important; "
    "}"
)

HIDE_SCRIPT = f"""
(() => {{
    let style = document.getElementById('wuwa-auto-claim-hide-chests');
    if (!style) {{
        style = document.createElement('style');
        style.id = 'wuwa-auto-claim-hide-chests';
        style.textContent = `{HIDE_CSS}`;
        (document.head || document.documentElement).appendChild(style);
    }}
    document.querySelectorAll('.reward-anchor, .reward-btn').forEach(el => {{
        el.style.setProperty('display', 'none', 'important');
        el.style.setProperty('pointer-events', 'none', 'important');
    }});
    return true;
}})();
"""

SHOW_SCRIPT = """
(() => {
    let style = document.getElementById('wuwa-auto-claim-hide-chests');
    if (style) {
        style.remove();
    }
    document.querySelectorAll('.reward-anchor, .reward-btn').forEach(el => {
        el.style.removeProperty('display');
        el.style.removeProperty('pointer-events');
        el.style.removeProperty('visibility');
    });
    return true;
})();
"""

def ensure_webview2_cdp_env():
    """Ensure the user environment variable enables CDP port 9222 for WebView2."""
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Environment') as key:
            try:
                val, _ = winreg.QueryValueEx(key, 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS')
                if f'--remote-debugging-port={CDP_PORT}' not in val:
                    new_val = (val + f' --remote-debugging-port={CDP_PORT}').strip()
                    winreg.SetValueEx(key, 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', 0, winreg.REG_SZ, new_val)
            except FileNotFoundError:
                winreg.SetValueEx(key, 'WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS', 0, winreg.REG_SZ, f'--remote-debugging-port={CDP_PORT}')
    except Exception:
        pass

def get_page_targets():
    """Retrieve all open page targets belonging to Wuwa Tappo."""
    try:
        req = urllib.request.Request(f"{CDP_URL}/json", headers={"User-Agent": "WuwaTappoAutoClaim"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            targets = json.loads(resp.read().decode('utf-8'))
            return [t for t in targets if t.get("type") == "page" and "tauri.localhost" in t.get("url", "")]
    except Exception:
        return []

async def _send_cdp(ws_url, method, params=None):
    try:
        async with websockets.connect(ws_url, close_timeout=1.0) as ws:
            payload = {"id": 1, "method": method, "params": params or {}}
            await ws.send(json.dumps(payload))
            raw = await asyncio.wait_for(ws.recv(), timeout=1.5)
            return json.loads(raw)
    except Exception:
        return None

def set_chests_hidden(hidden=True):
    """
    Hide or show the chests inside Wuwa Tappo desktop pet.
    Injects CSS to make reward buttons unclickable and completely invisible.
    """
    targets = get_page_targets()
    if not targets:
        return False
    script = HIDE_SCRIPT if hidden else SHOW_SCRIPT
    async def _run():
        tasks = []
        for t in targets:
            ws = t.get("webSocketDebuggerUrl")
            if ws:
                tasks.append(_send_cdp(ws, "Runtime.evaluate", {"expression": script}))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    try:
        asyncio.run(_run())
        return True
    except Exception:
        return False

def reload_game_ui():
    """
    Reload the game's UI via CDP so that local cooldowns and click counts
    are cleanly synchronized with the server after a chest claim.
    """
    targets = get_page_targets()
    main_targets = [t for t in targets if t.get("url") == "http://tauri.localhost/#/"]
    if not main_targets:
        main_targets = targets
    async def _run():
        tasks = []
        for t in main_targets:
            ws = t.get("webSocketDebuggerUrl")
            if ws:
                tasks.append(_send_cdp(ws, "Runtime.evaluate", {"expression": "location.reload()"}))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    try:
        asyncio.run(_run())
        return True
    except Exception:
        return False
