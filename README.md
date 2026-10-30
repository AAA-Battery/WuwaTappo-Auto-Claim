# Wuwa Tappo Auto Claim

An unofficial Windows system-tray helper that claims a Wuwa Tappo chest after the server says its cooldown has ended. It does not shorten or bypass the server cooldown.

**Latest release:** [WuwaTappoAutoClaim-v1.1.0.zip](https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/v1.1.0/WuwaTappoAutoClaim-v1.1.0.zip)

**Current public release:** [GitHub Releases](https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/latest)

**License:** [MIT](LICENSE)

## What it does

- Reads the two chest deadlines and eligible click balance from the Wuwa Tappo service.
- Waits for the server deadline, then sends one claim request for a ready chest when at least 1,000 server-recorded clicks are available.
- Processes the two chest slots one at a time and records outcomes locally to avoid repeating a successful claim.
- Shows chest timers, server clicks, session claim totals, rarity counts, and the latest reward in a refreshed Windows dashboard. You can pause or resume, re-pair, open the activity log, re-sync Tappo, minimize to the tray, or exit.
- Hides chest controls while auto-claim is active and updates Tappo's displayed click balance from the server after a claim, without reloading the page.
- Uses the server's response time and a monotonic timer, so changing the Windows clock does not make a chest ready sooner.

The service remains authoritative. It may reject a claim during cooldown, after a session expires, or when the server has not recorded enough clicks. The helper backs off and checks again. It polls at intervals of up to about 30 seconds, so a claim can happen a few seconds later than the exact deadline.

## Requirements

- Windows 10 or 11, 64-bit.
- Wuwa Tappo installed, open, and logged into the intended account.
- Internet access and at least 1,000 eligible clicks recorded by the service for each claim.
- Keep the PC awake and Wuwa Tappo running while waiting. The helper does not run while the PC is shut down or asleep.

## Install and run the prebuilt app

1. Download `WuwaTappoAutoClaim-v1.1.0.zip` from the [v1.1.0 GitHub Release](https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/tag/v1.1.0).
2. Extract the whole ZIP to a folder. Keep `_internal` beside `WuwaTappoAutoClaim.exe`.
3. Open Wuwa Tappo and log in.
4. Run `WuwaTappoAutoClaim.exe`, choose **Re-pair Session**, and approve the one-time Administrator prompt. Pairing restarts Wuwa Tappo once to read its own WebView2 session cookie; log in again if needed.
5. Choose **Enable Auto-Claim**. If UI sync is unavailable, the helper automatically waits for Tappo to save pending clicks, then restarts it once with the WebView2 setting needed for UI synchronization. Use **Re-sync Tappo UI** to try again; it first checks the live connection and only restarts Tappo if needed. Close or minimize the dashboard to keep the helper in the notification area.

You can instead run `Run-Background.vbs` to start minimized with auto-claim enabled. Use **Exit** in the tray menu or `Stop-Tool.vbs` to stop it. It is not added to Windows Startup or Task Scheduler; launch it again after a Windows restart.

## Version 1.1.0

- Replace the older compact window with the new dashboard layout, including timer cards, server-click balance, session claim and rarity totals, last-claim details, action buttons, and a settings dialog.
- Keep the existing tray controls, session pairing, cooldown handling, chest visibility sync, and live click-count refresh connected to the new UI.
- Improve the window title bar appearance and adjust the footer layout while keeping the main action buttons and Settings/Exit button sizes unchanged.

## Version 1.0.5

- Chest visibility is reapplied directly to the live page after a claim; the helper no longer reloads Tappo.
- After the server confirms a claim, the helper updates Tappo's reactive click totals from a fresh server response while preserving locally pending clicks and spends.
- If UI sync is unavailable when Auto-Claim starts, Tappo is restarted once automatically after a 5.5-second save interval; the manual re-sync button retries without an extra confirmation and restarts only when required.

## Chest rules

Wuwa Tappo's service creates a chest after its 30-minute active-time cycle. Claiming consumes 1,000 eligible input counts and starts the next cycle. Up to two unopened chests can be stored; the timer pauses while both slots are full. This helper only claims a chest after its server deadline and the required click balance are present. It does not fabricate clicks, edit the Wuwa Tappo executable, or change the system clock.

## Security and privacy

The helper reads only the `kuro_infoToken` session cookie in Wuwa Tappo's own WebView2 profile when you choose to pair. It does not read Chrome, Edge, or Firefox profiles and does not store your password. The saved session is protected with Windows DPAPI for the current Windows account. API requests go to the Wuwa Tappo service configured in the source; logs omit session tokens and authorization headers. Tool data is stored under `%LOCALAPPDATA%\WuwaTappoAutoClaim`.

**WebView2 debugging notice:** To hide and restore the chest controls and refresh the in-app click display, the helper sets the per-user `WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS` environment variable to enable WebView2 remote debugging on port `9222`. If UI sync is unavailable when you enable Auto-Claim, the helper waits 5.5 seconds for pending clicks to save, then restarts Tappo once automatically. The **Re-sync Tappo UI** button checks the current connection first and restarts Tappo only when needed; the endpoint is intended for local access and has no login of its own. Other software running on your PC may be able to connect while Wuwa Tappo is open. Review `ui_sync.py` (or `source/ui_sync.py` in the nested source layout) before using this feature, and do not run untrusted software alongside it. The setting remains in your Windows user environment after the helper exits; remove only the added `--remote-debugging-port=9222` argument from that variable if you no longer want it enabled. Existing arguments may belong to other software.

The prebuilt executable is not code-signed. Review the source and release checksum before running it. This project is independent and is not affiliated with, endorsed by, or sponsored by KURO GAMES or Irox Games. The service may change or restrict its undocumented interfaces, so the helper may stop working. Use it in accordance with the app's applicable rules.

## Build from source

1. Install Python 3.10 or later on Windows.
2. Clone this repository and open its folder in PowerShell or Command Prompt.
3. Install dependencies:

   ```powershell
   python -m pip install -r requirements.txt  # GitHub source ZIP
   # For the nested source layout instead: python -m pip install -r source/requirements.txt
   ```

4. Run the UI:

   ```powershell
   python main.py  # GitHub source ZIP
   # For the nested source layout instead: python source/main.py
   ```

5. To build the portable folder, run `build.bat`. The script accepts both the GitHub source archive layout (Python files at the root) and the nested `source/` layout. It installs dependencies into the same Python environment used by PyInstaller, verifies the tray dependencies, and embeds `Zani.ico` plus `Zani.png`. The output is placed in `dist/WuwaTappoAutoClaim/`. Copy `README.md`, `LICENSE`, `Run-Background.vbs`, and `Stop-Tool.vbs` beside the executable before making a ZIP for distribution.

The included character artwork is supplied as the app icon and is separate from the MIT-licensed project code; image rights remain with its respective owner. The source includes a unit test module at `source/test_core.py` (or `test_core.py` in the root layout); dependencies are listed in `source/requirements.txt` (or `requirements.txt` in the root layout).

## Command-line options

| Option | Purpose |
| --- | --- |
| `--enable` | Start with auto-claim enabled. |
| `--start-minimized` | Start hidden in the tray. |
| `--pair` | Run the session pairing routine (requires Administrator). |
| `--stop` | Ask a running instance to exit gracefully. |
| `--diagnose` | Read authentication, click balance, and cooldowns without claiming. |

## License

The project's original code and documentation are provided under the [MIT License](LICENSE). Third-party dependencies and their bundled components remain under their respective licenses.
