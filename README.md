# Wuwa Tappo Auto Claim

An unofficial Windows system-tray helper that claims a Wuwa Tappo chest after the server says its cooldown has ended. It does not shorten or bypass the server cooldown.

**Latest release:** [WuwaTappoAutoClaim-v1.2.2.zip](https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/v1.2.2/WuwaTappoAutoClaim-v1.2.2.zip)

**License:** [MIT](LICENSE)

## What it does

- Reads the two chest deadlines and eligible click balance from the Wuwa Tappo service.
- Waits for the server deadline, then sends one claim request for a ready chest when at least 1,000 server-recorded clicks are available.
- Processes the two chest slots one at a time and records outcomes locally to avoid repeating a successful claim.
- Shows chest timers, server clicks, session claim totals, rarity counts, and the latest reward in a refreshed Windows dashboard. You can pause or resume, re-pair, open the activity log, re-sync Tappo, minimize to the tray, or exit.
- Hides chest controls while auto-claim is active and updates Tappo's displayed click balance from the server after a claim, without reloading the page.
- Optional **Click when idle** starts after 15 seconds without keyboard or mouse input, stops as soon as user input resumes, and starts a fresh idle period after activity. Only a continuous long idle stretch is capped at 1,000 F15 presses followed by a 5-minute cooldown. Automatic F15 timing varies randomly. F15 goes to the foreground app.
- Settings includes **Add 1000 clicks**, which sends 1,000 F15 presses 0.15 seconds apart and disables its button until the batch completes.
- Uses the server's response time and a monotonic timer, so changing the Windows clock does not make a chest ready sooner.

The service remains authoritative. It may reject a claim during cooldown, after a session expires, or when the server has not recorded enough clicks. The helper backs off and checks again. It polls at intervals of up to about 30 seconds, so a claim can happen a few seconds later than the exact deadline.

## Requirements

- Windows 10 or 11, 64-bit.
- The same Windows account that runs Wuwa Tappo must be an Administrator and approve the UAC prompt when starting the helper. Elevating with credentials from a different account uses a different Windows profile and cannot read the Tappo session.
- Wuwa Tappo installed, open, and logged into the intended account.
- Internet access and at least 1,000 eligible clicks recorded by the service for each claim.
- Keep the PC awake and Wuwa Tappo running while waiting. The helper does not run while the PC is shut down or asleep.

## Install and run the prebuilt app

1. Download `WuwaTappoAutoClaim-v1.2.2.zip` from the [v1.2.2 GitHub Release](https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/tag/v1.2.2).
2. Extract the whole ZIP to a folder. Keep `_internal` beside `WuwaTappoAutoClaim.exe`.
3. Open Wuwa Tappo and log in.
4. Run `WuwaTappoAutoClaim.exe` and approve the Administrator prompt each time it starts. Choose **Re-pair Session** if needed. Pairing restarts Wuwa Tappo once to read its own WebView2 session cookie; log in again if needed.
5. Choose **Enable Auto-Claim**. If UI sync is unavailable, the helper automatically waits for Tappo to save pending clicks, then restarts it once with the WebView2 setting needed for UI synchronization. Use **Re-sync Tappo UI** to try again; it first checks the live connection and only restarts Tappo if needed. Close or minimize the dashboard to keep the helper in the notification area.

You can instead run `Run-Background.vbs` to start minimized with auto-claim enabled. Use **Exit** in the tray menu or `Stop-Tool.vbs` to stop it. It is not added to Windows Startup or Task Scheduler; launch it again after a Windows restart.

When a newer helper version starts, it asks older running helper processes to close and waits for them before opening its dashboard. If an older process is unresponsive, the new version closes that identified helper process before continuing. If the same or a newer version is already running, the new launch brings its existing window forward instead of starting a duplicate.

## Version 1.2.2

- Show the 15-second idle click explanation as a tooltip when hovering over **Click when idle**; restore the general footer notice.
- Increase the **Add 1000 clicks** interval from 0.05 to 0.15 seconds between presses.
- Vary the automatic F15 timing randomly between idle presses.

## Version 1.2.1 (local test build)

- On launch, close an older running helper before starting this version; if this or a newer version is already running, show its window instead of creating a duplicate.
- Coordinate near-simultaneous launches so they do not stop each other during an upgrade.
- Reset the idle click batch and its 5-minute cooldown when keyboard or mouse input resumes. A new batch starts only after the next 15 seconds of inactivity.

This package has not been published as a GitHub Release.

## Version 1.2.0 (local test build)

- Limit **Click when idle** to 1,000 F15 presses per long idle stretch, then pause for 5 minutes.
- Add **Add 1000 clicks** under Settings. Its Add button stays disabled while 1,000 F15 presses are sent at 0.05-second start-to-start intervals.
- Replace the idle explanation in the main window with the requested 15-second, 1,000-click, 5-minute-cooldown sentence.

This package has not been published as a GitHub Release.

## Version 1.1.7 (local test build)

- Add a **Click when idle** toggle beside the three main action buttons; green means on and gray means off.
- When enabled, send F15 after 15 seconds without keyboard or mouse input until user input resumes.
- Save the toggle state across restarts and stop the worker cleanly when the helper exits.
- F15 is sent to the foreground application; disable the option if that application uses F15.

This package has not been published as a GitHub Release.

## Version 1.1.6

- Apply the immediate 1,000-click display deduction only to a loaded Tappo dashboard with its click store ready; auxiliary WebViews no longer cause repeated deductions while retries run.
- Attach an in-memory claim ID to the deduction and make retries idempotent, so one successful claim is reflected only once.
- Log whether the immediate display deduction was applied or could not be confirmed.

## Version 1.1.5 (local test build)

- Keep v1.1.3's API User-Agent identifier while retaining the v1.1.4 UI confirmation fixes.
- Refuse to overwrite Tappo's cumulative click totals with impossible or lower server totals, preventing a stale zero response from resetting an existing counter.
- Record server gross/consumed/available totals and whether Tappo confirmed the display update in `activity.log`.

This package has not been published as a GitHub Release.

## Version 1.1.4

- Verify chest hiding and click counts against ready dashboard WebViews, ignoring Tappo pages that have not finished loading or do not contain the dashboard.
- Treat the injected chest stylesheet as effective even when no chest anchor is currently rendered, so future chest controls are hidden without a false restart failure.
- Check the click total on the dashboard node itself as well as its text children, and accept rounded compact counters such as `1.2K` within their displayed precision.

This is a local test build and has not been published as a GitHub Release.

## Version 1.1.3

- Fix the v1.1.2 elevated restart path: WebView2 ignores per-user environment and HKCU debugging overrides when its host is elevated, which left Tappo open without a CDP endpoint.
- Temporarily set an app-specific HKLM WebView2 debugging policy for `WuwaTappo.exe`, then restore the exact previous registry value after UI sync or a failed launch. A recovery marker allows restoration after an interrupted restart.
- Keep the debugging argument in the helper's process environment instead of adding it permanently to the user's Windows environment.
- Report whether Tappo exited, WebView2 received the debugging flag, or the endpoint failed to expose the dashboard.

This package has not been published as a GitHub Release.

## Version 1.1.2

- Require Administrator permission at startup, including source runs, so process restart and UI synchronization use a consistent Windows permission level.
- Reuse the elevated helper process for session pairing instead of opening another UAC flow. Record the installed Tappo path and reopen Tappo after pairing, including when pairing fails.
- Improve installed-app lookup and restart error details so missing paths, denied stops, and failed relaunches report the Windows error.
- Confirm chest controls are actually hidden in the live dashboard and that Tappo's displayed click count matches the server-confirmed value before reporting success.
- Rebuild the portable ZIP from the current source so the EXE includes the same chest and click synchronization code as the source tree.

This is a local test build and has not been published as a GitHub Release.

## Version 1.1.1

- Restrict chest and click synchronization to Tappo's main dashboard WebView; report chest hiding as successful only when matching controls were found and computed as hidden.
- Update the main dashboard's Pinia store and confirm that Tappo's visible click counter reflects its calculated total, preserving locally pending clicks and spends.
- Verify UI synchronization results to prevent false success when chest controls remain visible or the click counter does not refresh.

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

**WebView2 debugging notice:** To hide/restore chest controls and refresh Tappo's click display, the helper enables WebView2 remote debugging on port `9222`. On an elevated restart, it temporarily writes an app-specific value under `HKLM\SOFTWARE\Policies\Microsoft\Edge\WebView2\AdditionalBrowserArguments`, then restores the previous value after synchronization or a failed launch. A recovery marker is stored under `%LOCALAPPDATA%\WuwaTappoAutoClaim` if the helper is interrupted during restart. The debugging endpoint has no login of its own, so other software running locally may connect while Tappo is open. Review `ui_sync.py` before using this feature, and do not run untrusted software alongside it. The service may change or restrict its undocumented interfaces, so UI sync can stop working after a Tappo or WebView2 update.

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
