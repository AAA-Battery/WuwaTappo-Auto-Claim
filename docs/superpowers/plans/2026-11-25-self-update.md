# Self-Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Check for updates" / "Update now" row to Settings that downloads the latest GitHub release, verifies its SHA-256, swaps the installed files and relaunches the app.

**Architecture:** One new module `updater.py` holds all update logic (release lookup, download, verification, safe extraction, apply-script launch) and is independent of Tk. A PowerShell script written to `%LOCALAPPDATA%\WuwaTappoAutoClaim\update\` does the file swap after the app exits (rename-to-`.old` backup, rollback on failure). `main.py` and `chest_autoclaim_ui.py` only wire buttons, threads and status text to `updater.py`.

**Tech Stack:** Python 3.13, `requests`, `zipfile`, `hashlib`, `unittest`, PowerShell 5.1, Tkinter UI already in the project, PyInstaller onedir builds.

**Spec:** `docs/superpowers/specs/2026-11-25-self-update-design.md`

## Global Constraints

- GitHub repo is `AAA-Battery/WuwaTappo-Auto-Claim`; lookup is `GET https://api.github.com/repos/AAA-Battery/WuwaTappo-Auto-Claim/releases/latest`, no token. Draft and prerelease releases are ignored.
- Release assets must be named `WuwaTappoAutoClaim-vX.Y.Z.zip` and `WuwaTappoAutoClaim-vX.Y.Z.zip.sha256`. No hash asset means no install.
- Download URLs: first hop must be `https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/...`; redirects only to `github.com` or `*.githubusercontent.com` over https; at most 5 redirects; zip size cap 300 MB (`300 * 1024 * 1024`).
- Zip is rejected on absolute paths, drive letters, `..` segments, or if it lacks `WuwaTappoAutoClaim.exe` and `_internal/` at its root; total uncompressed size cap 600 MB.
- Version comparison uses numeric `X.Y.Z` tuples (`1.2.10 > 1.2.9`); tags that are not purely numeric dotted versions (for example `v1.3.0-beta`) are ignored.
- Backup is by rename to `WuwaTappoAutoClaim.exe.old` and `_internal.old` in the install folder; app deletes `.old` files and the update work folder at its next start.
- Source runs (not `sys.frozen`) never self-replace; the button opens the release page instead.
- New app version is `1.3.0` (`VERSION` in `main.py`).
- Do not commit: the working tree holds unrelated uncommitted user changes. Commit only if the user asks.
- UI text is English, like the rest of the app. Network errors must never raise into the Tk loop or the claim worker.

## Review Focus

- Release exists but has no `.sha256` asset: clear error text, nothing downloaded or installed (Task 1).
- GitHub unreachable, rate-limited (403/429) or returns junk JSON: friendly message, app keeps working (Task 1).
- Tags like `v1.2.10`, `1.3`, `v1.3.0-beta`, empty or missing `tag_name`: compare correctly or ignore, never crash (Task 1).
- Malicious or wrong-layout zip (`../evil`, absolute path, missing `_internal`): rejected before anything in the install folder changes (Task 2).
- Install folder with spaces or non-ASCII characters, or not writable: update works in the first case; second case fails with an error before the app quits (Tasks 3 and 5).

---

### Task 1: Release lookup, version compare, URL and hash helpers

**Files:**
- Create: `updater.py`
- Test: `test_updater.py`

**Interfaces:**
- Produces (in `updater.py`):
  - constants `REPO`, `LATEST_URL`, `MAX_DOWNLOAD_BYTES`, `MAX_REDIRECTS`
  - `class UpdateError(Exception)`
  - `@dataclass(frozen=True) class UpdateInfo: version: str; zip_name: str; zip_url: str; sha256_url: str; page_url: str` (`version` has no leading `v`)
  - `parse_version(tag: str) -> tuple[int, int, int] | None`
  - `parse_release(release: dict, current: str) -> UpdateInfo | None`
  - `check_for_update(current: str, get=requests.get) -> UpdateInfo | None`
  - `is_allowed_url(url: str, first_hop: bool) -> bool`
  - `parse_sha256(text: str, zip_name: str) -> str` (lowercase hex)
  - `verify_sha256(path: Path, expected: str) -> None`

- [ ] **Step 1: Write failing tests in `test_updater.py`** (class `VersionAndReleaseTests`). Use a helper `release(tag, assets=..., draft=False, prerelease=False)` building the GitHub JSON shape (`tag_name`, `draft`, `prerelease`, `html_url`, `assets=[{name, browser_download_url}]`). Tests:
  - `test_parse_version`: `parse_version('v1.2.10') == (1, 2, 10)`, `'1.3' == (1, 3, 0)`, and `None` for `'v1.3.0-beta'`, `''`, `'latest'`, `None`.
  - `test_newer_release_returns_info`: `parse_release(release('v1.3.0'), '1.2.2')` returns `UpdateInfo` with `version == '1.3.0'`, `zip_name == 'WuwaTappoAutoClaim-v1.3.0.zip'`, correct `zip_url`/`sha256_url`.
  - `test_not_newer_returns_none`: same version, older version, `1.2.9` vs current `1.2.10`, draft, prerelease, unparsable tag all return `None`.
  - `test_missing_hash_asset_raises`: newer release with only the zip asset raises `UpdateError` whose message contains `.sha256`; missing zip asset raises too.
  - `test_check_for_update_errors_become_update_error`: injected `get` that raises `requests.ConnectionError`, returns status 403, returns status 200 with non-JSON body, or returns JSON that is a list: each raises `UpdateError` (no other exception type).
  - `test_is_allowed_url`: first hop true only for `https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/v1.3.0/x.zip`; false for `http://`, other repo, other host, `github.com.evil.com`; later hops true for `https://objects.githubusercontent.com/...` and `https://github.com/...`, false for `https://evil.com/` and `http://` ones.
  - `test_parse_sha256`: accepts `"<64hex>\n"` and `"<64hex>  WuwaTappoAutoClaim-v1.3.0.zip\n"`, uppercase input is lowercased; raises `UpdateError` for wrong length, non-hex, empty, or a filename that is not `zip_name`.
  - `test_verify_sha256`: write temp file; matching hash passes, different hash raises `UpdateError`.

- [ ] **Step 2: Run** `python -m unittest test_updater -v`. Expected: ImportError/FAIL (module missing).

- [ ] **Step 3: Implement the interfaces in `updater.py`.** `check_for_update` calls `get(LATEST_URL, headers={'Accept': 'application/vnd.github+json'}, timeout=(5, 15))`, maps every `requests.RequestException`, non-200 status and `ValueError`/non-dict JSON to `UpdateError` with a short human message (status 403/429 → "GitHub rate limit reached; try again later."). `is_allowed_url` uses `urllib.parse.urlsplit` and compares exact hostnames.

- [ ] **Step 4: Run** `python -m unittest test_updater -v`. Expected: all PASS.

---

### Task 2: Download, safe extraction and staging

**Files:**
- Modify: `updater.py`
- Test: `test_updater.py` (class `DownloadAndExtractTests`)

**Interfaces:**
- Consumes: Task 1 (`UpdateInfo`, `UpdateError`, `is_allowed_url`, `parse_sha256`, `verify_sha256`, `MAX_DOWNLOAD_BYTES`, `MAX_REDIRECTS`).
- Produces:
  - `download_file(url: str, dest: Path, max_bytes: int = MAX_DOWNLOAD_BYTES, progress=None, get=requests.get) -> None` — `progress(done: int, total: int | None)`; follows redirects manually with `allow_redirects=False`, checking `is_allowed_url` per hop; raises `UpdateError` on a disallowed hop, more than `MAX_REDIRECTS`, non-200, or more than `max_bytes` received (also checks `Content-Length` up front); deletes the partial file on any failure.
  - `safe_extract(zip_path: Path, dest: Path) -> Path` — returns `dest`.
  - `stage_update(info: UpdateInfo, work_dir: Path, progress=None, get=requests.get) -> Path` — returns the extracted directory.

- [ ] **Step 1: Write failing tests.** Test helpers: `FakeResponse(status, body=b'', headers=None)` with `status_code`, `headers`, `iter_content(chunk_size)`, `close()`; `make_zip(path, members: dict[str, bytes])`. Tests:
  - `test_download_follows_allowed_redirect_and_reports_progress`: fake `get` maps the first URL to a 302 with `Location` on `objects.githubusercontent.com`, then 200 with body; file content equals body; `progress` last call equals `(len(body), len(body))`.
  - `test_download_rejects_redirect_to_other_host`: 302 to `https://evil.com/x` raises `UpdateError`, no file left behind.
  - `test_download_rejects_oversize`: body larger than `max_bytes=10` raises `UpdateError` and removes the partial file; same for a `Content-Length` header above the cap.
  - `test_download_rejects_too_many_redirects`.
  - `test_safe_extract_good_zip`: members `WuwaTappoAutoClaim.exe`, `_internal/lib.dll`, `README.md` extract to `dest` with identical bytes.
  - `test_safe_extract_rejects_unsafe_and_wrong_layout`: parametrized cases `../evil.txt`, `/abs.txt`, `C:/x.txt`, `_internal/../../evil.txt`, zip missing `_internal/`, zip missing the exe each raise `UpdateError`; assert that nothing was written outside `dest` and `dest` is empty or removed.
  - `test_safe_extract_rejects_zip_bomb`: member whose declared uncompressed size exceeds 600 MB (use `ZipInfo.file_size` patched via a crafted `ZipFile` subclass or monkeypatched constant) raises `UpdateError`.
  - `test_stage_update_end_to_end`: fake `get` serves a valid zip and its `.sha256` text; `stage_update` returns a directory containing the exe; with a wrong hash it raises `UpdateError` and `work_dir` holds no extracted files and no zip.

- [ ] **Step 2: Run** `python -m unittest test_updater.DownloadAndExtractTests -v`. Expected: FAIL (functions missing).

- [ ] **Step 3: Implement** the three functions. `safe_extract`: validate every member name first (reject, then extract only after all pass), extract into `dest` using `ZipFile.open` per member with a resolved-path-inside-`dest` check, require `WuwaTappoAutoClaim.exe` and at least one `_internal/` entry. `stage_update`: work under `work_dir / f'v{info.version}'`; download hash file (small, cap 4096 bytes via `download_file` to a temp file or in-memory), download zip, `verify_sha256`, `safe_extract` into `.../staged`, delete the zip, and on any exception remove the whole version folder before re-raising.

- [ ] **Step 4: Run** `python -m unittest test_updater -v`. Expected: all PASS.

---

### Task 3: Apply script, launcher and cleanup

**Files:**
- Modify: `updater.py`
- Test: `test_updater.py` (class `ApplyScriptTests`)

**Interfaces:**
- Consumes: nothing from earlier tasks except module layout.
- Produces:
  - `APPLY_SCRIPT: str` (PowerShell source) and `write_apply_script(work_dir: Path) -> Path` (writes `work_dir/apply-update.ps1` UTF-8 with BOM, returns path)
  - `launch_apply(staged: Path, install_dir: Path, pid: int, work_dir: Path, relaunch_args: list[str] | None = None) -> None` — starts the script detached and hidden (`powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ...`, `CREATE_NO_WINDOW | DETACHED_PROCESS`), does not wait.
  - `preflight_install_dir(install_dir: Path) -> None` — raises `UpdateError` if a test file cannot be created and removed in `install_dir`.
  - `cleanup_after_update(install_dir: Path, work_dir: Path) -> None` — best effort, never raises: removes `WuwaTappoAutoClaim.exe.old`, `_internal.old` in `install_dir` and every staging folder/zip in `work_dir` except `update.log`.
- Script contract (parameters): `-ProcessId <int> -Staged <path> -Install <path> -Log <path> -RelaunchArgs <string> [-NoLaunch] [-SimulateFailure]`. Behaviour: wait up to 60 s for the process to exit (still running after that: log and exit 1 without touching files); rename `WuwaTappoAutoClaim.exe` and `_internal` to `.old` (delete a stale `.old` first; retry each rename up to 5 times, 1 s apart); copy everything from `-Staged` into `-Install` recursively; on any error (including `-SimulateFailure`, which throws after the copy) delete the copied exe and `_internal`, rename the `.old` items back, relaunch the old exe unless `-NoLaunch`, exit 1; on success start `WuwaTappoAutoClaim.exe` (working directory = install folder, arguments = `-RelaunchArgs`) unless `-NoLaunch`, exit 0. Every step appends a timestamped line to `-Log`. Files in `-Install` that are not in `-Staged` (user-added) are never deleted; `.old` items are left for the app to remove.

- [ ] **Step 1: Write failing tests.** Helper `run_script(staged, install, **flags)` runs the real script through `subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-ProcessId', '99999999', ...])` with `-NoLaunch`; skip the class if `powershell` is not found. Tests, each in a temp dir whose name includes a space and `é` (for example `"inst é dir"`):
  - `test_success_swaps_files`: install has old exe (`b'old'`), `_internal/old.dll`, `user.txt`; staged has new exe (`b'new'`), `_internal/new.dll`, `README.md`. After run: return code 0, exe is `b'new'`, `_internal/new.dll` exists, `_internal/old.dll` does not exist in `_internal`, `user.txt` still present, `WuwaTappoAutoClaim.exe.old` and `_internal.old` exist, log file mentions success.
  - `test_failure_rolls_back`: with `-SimulateFailure`: return code 1, exe is `b'old'`, `_internal/old.dll` exists, `_internal/new.dll` does not, no leftover `.old` items, log mentions rollback.
  - `test_running_process_aborts_without_changes`: pass the PID of a live child process (`subprocess.Popen(['cmd', '/c', 'ping -n 30 127.0.0.1 > nul'])`) with a shortened wait via environment variable `WTAC_WAIT_SECONDS=2`; expect return code 1 and install untouched; kill the child in cleanup.
  - `test_cleanup_after_update_removes_old_and_staging` and `test_cleanup_never_raises` (missing paths, locked-style errors simulated by passing a path that does not exist).
  - `test_preflight_install_dir`: writable temp dir passes; a path that does not exist raises `UpdateError`.
  - `test_launch_apply_builds_expected_command`: monkeypatch `subprocess.Popen`; assert argv starts with `powershell.exe -NoProfile -ExecutionPolicy Bypass`, contains the five parameters with the given paths and the PID, and that `creationflags` includes `CREATE_NO_WINDOW`.

- [ ] **Step 2: Run** `python -m unittest test_updater.ApplyScriptTests -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `APPLY_SCRIPT` (read `WTAC_WAIT_SECONDS`, default 60, as the wait limit), `write_apply_script`, `launch_apply`, `preflight_install_dir`, `cleanup_after_update` per the contract above.

- [ ] **Step 4: Run** `python -m unittest test_updater -v`. Expected: all PASS.

---

### Task 4: Settings row and dashboard hint in the UI

**Files:**
- Modify: `chest_autoclaim_ui.py` (`SettingsDialog.__init__` / `_row`, `ChestAutoClaimUI.__init__` / `open_settings` / public API; keep the gear button as `self._gear`)
- Test: `test_updater_ui.py`

**Interfaces:**
- Produces:
  - `SettingsDialog.__init__(self, master, on_open_log, on_resync, on_add_clicks, on_stop_add_clicks, on_check_update, on_install_update)`
  - `SettingsDialog.set_update_state(self, state: str, text: str)`; `state` is one of `'idle' | 'checking' | 'uptodate' | 'available' | 'downloading' | 'error'`. Row title "Check for updates"; description label shows `text`; button label/enabled per state: `idle`/`uptodate`/`error` → "Check" enabled; `checking` → "Checking..." disabled; `available` → "Update now" enabled; `downloading` → "Updating..." disabled. The button calls `on_install_update` only in state `available`, otherwise `on_check_update`.
  - `ChestAutoClaimUI.__init__(..., on_check_update=None, on_install_update=None)`; `ChestAutoClaimUI.set_update_state(state: str, text: str)` stores the state and text, forwards to an open dialog, re-applies them whenever the dialog is created, and sets the gear button text to `"\u2699 Update available"` while state is `available`, `"\u2699"` otherwise.
  - `_row` must return both the button and the description label (or the dialog keeps a reference to the description label for this row) so the text can change.

- [ ] **Step 1: Write failing tests** (`test_updater_ui.py`, skip all if `tk.Tk()` raises `TclError`; root withdrawn; call `root.update()` after changes): 
  - `test_row_states`: after `ui.set_update_state('available', 'v1.3.0 is available')` and `ui.open_settings()`, the update button text is "Update now" and the description reads `v1.3.0 is available`; `checking` disables the button; `downloading` shows "Updating..."; `uptodate` shows "Check".
  - `test_button_dispatch`: stub callbacks record calls; in `idle` clicking the button calls `on_check_update` only; in `available` it calls `on_install_update` only. Invoke the button's command through its `_command`/click handler used by `HoverButton`.
  - `test_state_survives_dialog_reopen`: set state `error` with text `X`, close dialog, reopen: description is `X`.
  - `test_gear_hint`: gear text is `"\u2699 Update available"` in `available`, back to `"\u2699"` in `uptodate`.

- [ ] **Step 2: Run** `python -m unittest test_updater_ui -v`. Expected: FAIL.

- [ ] **Step 3: Implement** the UI changes following the existing patterns in the file (the `set_add_clicks_running` / cached-state approach and the existing row styling). Initial state is `('idle', 'Check whether a newer version is available.')`.

- [ ] **Step 4: Run** `python -m unittest test_updater_ui test_core test_updater -v`. Expected: all PASS. Also run `python -c "import ast; ast.parse(open('chest_autoclaim_ui.py', encoding='utf8').read())"`.

---

### Task 5: Wire updater into the app, version bump

**Files:**
- Modify: `main.py` (imports, `VERSION`, `TrayApp.__init__`, new methods, `tick` message handler, startup cleanup and silent check)
- Test: `test_updater_wiring.py`

**Interfaces:**
- Consumes: Tasks 1-4 (`updater.check_for_update`, `updater.stage_update`, `updater.launch_apply`, `updater.preflight_install_dir`, `updater.cleanup_after_update`, `ChestAutoClaimUI.set_update_state`, constructor callbacks).
- Produces in `main.py`:
  - `VERSION = '1.3.0'`
  - module-level `install_dir() -> Path` = `Path(sys.executable).resolve().parent` when `sys.frozen`, else the folder of `main.py`
  - `UPDATE_DIR = DATA_DIR / 'update'`
  - `TrayApp.check_for_updates(self, silent: bool = False)` — background thread; posts `('update_checked', {'info': UpdateInfo | None, 'error': str | None, 'silent': bool})`.
  - `TrayApp.install_update(self)` — if not frozen: `webbrowser.open(info.page_url)`, status text, return. Otherwise: `preflight_install_dir` on the main thread (error → `set_update_state('error', msg)`, return), set state `downloading`, thread runs `stage_update` with progress posting `('update_progress', {'text': 'Downloading 12 / 38 MB'})`, then posts `('update_staged', {'staged': Path})` or `('update_failed', {'error': str})`.
  - message handling in `tick`: `update_checked` → ui state `available` (text `f'v{version} is available'`) / `uptodate` (text `f"You're up to date (v{VERSION})"`) / `error` (silent checks never show errors, they only log); `update_progress` → state `downloading` with text; `update_failed` → state `error`; `update_staged` → `updater.launch_apply(staged, install_dir(), os.getpid(), UPDATE_DIR, relaunch_args=['--enable'] if self.active.is_set() else [])`, log the line `Update to v%s staged; restarting`, then `self.quit()`; if `launch_apply` raises, state `error` and no quit.
  - startup: when frozen, `cleanup_after_update(install_dir(), UPDATE_DIR)` in a daemon thread; `self.root.after(8000, lambda: self.check_for_updates(silent=True))`.
- The existing single-instance logic already stops older running instances, so no change there.

- [ ] **Step 1: Write failing tests** (`test_updater_wiring.py`): avoid constructing the full `TrayApp` (it needs Tk, tray and Tappo). Extract the message-to-UI mapping into a small pure helper in `main.py`, `update_message_to_ui(event: str, data: dict, current: str) -> tuple[str, str] | None` returning `(state, text)`, and test it:
  - `('update_checked', {'info': info('1.3.0'), 'error': None, 'silent': False})` → `('available', 'v1.3.0 is available')`
  - info `None`, no error → `('uptodate', "You're up to date (v1.2.2)")` with `current='1.2.2'`
  - error text, `silent=False` → `('error', <text>)`; `silent=True` with an error → `None`
  - `('update_progress', {'text': 'Downloading 12 / 38 MB'})` → `('downloading', 'Downloading 12 / 38 MB')`
  - `('update_failed', {'error': 'bad hash'})` → `('error', 'bad hash')`
  - unknown event → `None`
  - `test_install_dir_when_not_frozen` returns the folder of `main.py`; with `sys.frozen=True` and `sys.executable` monkeypatched returns that executable's folder.

- [ ] **Step 2: Run** `python -m unittest test_updater_wiring -v`. Expected: FAIL.

- [ ] **Step 3: Implement** helper, constants, `check_for_updates`, `install_update`, tick handling, constructor wiring (`on_check_update=self.check_for_updates`, `on_install_update=self.install_update` passed to `ChestAutoClaimUI`), startup cleanup and silent check, `VERSION = '1.3.0'`. Log lines go through `self.log.info`/`warning`; no exception from a worker thread may escape (wrap in `try/except Exception` and post `update_failed`).

- [ ] **Step 4: Run** `python -m unittest test_core test_updater test_updater_ui test_updater_wiring -v` and `python -c "import ast; ast.parse(open('main.py', encoding='utf8').read())"`. Expected: all PASS.

- [ ] **Step 5: Manual smoke test** (needs Tappo not required): `python main.py`, open Settings, press Check. Expected with no newer release: text `You're up to date (v1.3.0)`; with the real repo (whose latest is `v1.2.2`, older than 1.3.0) also "up to date". Verify no traceback in `%LOCALAPPDATA%\WuwaTappoAutoClaim\activity.log`.

---

### Task 6: Release packaging tool, README, end-to-end local check

**Files:**
- Create: `tools/make_release.py`
- Test: `test_make_release.py`
- Modify: `README.md` (install/update section; release instructions)

**Interfaces:**
- Produces: `make_release.build_release(dist_dir: Path, extras: list[Path], version: str, out_dir: Path) -> tuple[Path, Path]` — creates `WuwaTappoAutoClaim-v{version}.zip` containing the contents of `dist_dir` (which must contain `WuwaTappoAutoClaim.exe` and `_internal/`) and each `extras` file at the zip root, and `WuwaTappoAutoClaim-v{version}.zip.sha256` containing `"<hex>  <zipname>\n"`; returns `(zip_path, sha_path)`. CLI: `python tools/make_release.py <version> [--dist dist/WuwaTappoAutoClaim] [--out release-assets]`, extras default to `LICENSE`, `README.md`, `Run-Background.vbs`, `Stop-Tool.vbs`; prints the two paths and reminds to upload both to the GitHub Release.

- [ ] **Step 1: Write failing tests** (`test_make_release.py`): `test_zip_layout_and_hash_roundtrip` builds a fake dist folder, calls `build_release`, then checks (a) zip root has `WuwaTappoAutoClaim.exe`, `_internal/x.dll` and the extras, (b) `updater.parse_sha256(sha_path.read_text(), zip_path.name)` equals the real SHA-256 of the zip, (c) `updater.safe_extract(zip_path, tmp)` succeeds on it. `test_rejects_dist_without_exe` raises `ValueError`. `test_rejects_bad_version` for `'1.3'` and `'v1.3.0'` (must be `X.Y.Z`).

- [ ] **Step 2: Run** `python -m unittest test_make_release -v`. Expected: FAIL.

- [ ] **Step 3: Implement** `tools/make_release.py` (add `sys.path` insert for the repo root so `import updater` works when run from anywhere; `tools/__init__.py` not needed — the test imports it via `importlib` from its path).

- [ ] **Step 4: Update `README.md`:** change install instructions to say the app updates itself from Settings > Check for updates (first install of v1.3.0 is manual), describe the `.sha256` requirement, and add a "Publishing a release" section: build with `build.bat`, run `python tools/make_release.py <version>`, upload both files to a new GitHub Release tagged `v<version>`.

- [ ] **Step 5: Run the full suite** `python -m unittest test_core test_updater test_updater_ui test_updater_wiring test_make_release -v`. Expected: all PASS.

- [ ] **Step 6: End-to-end local check of the real swap.** Build a fake "old install" folder and a fake release zip with `make_release.build_release`, run `updater.stage_update` with a fake `get` that serves them, then run the generated `apply-update.ps1` with `-NoLaunch` against the fake install folder (path containing a space). Expected: exe replaced, `.old` items present, then `updater.cleanup_after_update` removes them. Record the outcome in the final report (this is the same flow as the `ApplyScriptTests.test_success_swaps_files` test but through `stage_update` and `make_release`, so it checks the pieces fit together).

---

## Self-Review

- **Spec coverage:** source/API and asset names (Task 1), URL allowlist, redirects, size caps, safe extraction (Task 2), rename-based backup, rollback, relaunch, log, cleanup, preflight (Task 3), Settings row, startup silent check and "update available" hint (Tasks 4-5), source-run behavior (Task 5), release tool and README (Task 6), v1.3.0 bump (Task 5). Network via `requests` default settings (proxy-respecting) is used in Task 1 and 2 by not setting `trust_env=False`.
- **Type consistency:** `UpdateInfo`, `UpdateError`, `stage_update(info, work_dir, progress, get)`, `launch_apply(staged, install_dir, pid, work_dir, relaunch_args)` and `cleanup_after_update(install_dir, work_dir)` have the same names and signatures in every task that uses them.
- **Known limitation recorded in spec:** hash lives on the same GitHub release, so a compromised GitHub account is out of scope; no code signing.
