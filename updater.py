"""Self-update: find the latest GitHub release, verify it and stage it for install.

Nothing here touches Tk or the claim worker; every failure surfaces as UpdateError.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urljoin, urlsplit
import zipfile

import requests

REPO = 'AAA-Battery/WuwaTappo-Auto-Claim'
LATEST_URL = f'https://api.github.com/repos/{REPO}/releases/latest'
DOWNLOAD_PATH_PREFIX = f'/{REPO}/releases/download/'
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_EXTRACTED_BYTES = 600 * 1024 * 1024
MAX_HASH_FILE_BYTES = 4096
APP_EXE = 'WuwaTappoAutoClaim.exe'
APP_LIBS = '_internal'
ROLLBACK_FLAG = 'rollback-failed.flag'
ZIP_PREFIX = 'WuwaTappoAutoClaim-v'


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str      # e.g. '1.3.0', no leading 'v'
    zip_name: str
    zip_url: str
    sha256_url: str
    page_url: str


def parse_version(tag):
    """'v1.2.10' -> (1, 2, 10); anything that is not dotted numbers -> None."""
    if not isinstance(tag, str):
        return None
    match = re.fullmatch(r'v?(\d+)\.(\d+)(?:\.(\d+))?', tag.strip())
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def parse_release(release, current):
    """Return UpdateInfo when `release` is a newer, published release; else None."""
    if not isinstance(release, dict) or release.get('draft') or release.get('prerelease'):
        return None
    latest = parse_version(release.get('tag_name'))
    installed = parse_version(current)
    if latest is None or installed is None or latest <= installed:
        return None
    version = '.'.join(str(part) for part in latest)
    zip_name = f'{ZIP_PREFIX}{version}.zip'
    urls = {}
    for asset in release.get('assets') or []:
        if isinstance(asset, dict) and asset.get('name') in (zip_name, zip_name + '.sha256'):
            urls[asset['name']] = asset.get('browser_download_url')
    if not urls.get(zip_name):
        raise UpdateError(f'Release v{version} has no {zip_name} file.')
    if not urls.get(zip_name + '.sha256'):
        raise UpdateError(f'Release v{version} has no {zip_name}.sha256 file; update refused.')
    return UpdateInfo(version, zip_name, urls[zip_name], urls[zip_name + '.sha256'],
                      str(release.get('html_url') or ''))


def check_for_update(current, get=requests.get):
    try:
        response = get(LATEST_URL, headers={'Accept': 'application/vnd.github+json'}, timeout=(5, 15))
    except requests.RequestException:
        raise UpdateError('Could not reach GitHub. Check your internet connection.') from None
    if response.status_code in (403, 429):
        raise UpdateError('GitHub rate limit reached; try again later.')
    if response.status_code != 200:
        raise UpdateError(f'GitHub returned HTTP {response.status_code}.')
    try:
        payload = response.json()
    except ValueError:
        raise UpdateError('GitHub returned an unreadable response.') from None
    if not isinstance(payload, dict):
        raise UpdateError('GitHub returned an unexpected response.')
    return parse_release(payload, current)


def is_allowed_url(url, first_hop):
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != 'https' or parts.port not in (None, 443) or parts.username or parts.password:
        return False
    host = (parts.hostname or '').lower()
    if first_hop:
        return host == 'github.com' and parts.path.startswith(DOWNLOAD_PATH_PREFIX)
    return host == 'github.com' or host.endswith('.githubusercontent.com')


def parse_sha256(text, zip_name):
    fields = str(text or '').strip().split()
    if not fields or len(fields) > 2:
        raise UpdateError('The hash file is empty or malformed.')
    digest = fields[0].lower()
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise UpdateError('The hash file does not contain a valid SHA-256.')
    if len(fields) == 2 and fields[1].lstrip('*') != zip_name:
        raise UpdateError('The hash file is for a different file.')
    return digest


def verify_sha256(path, expected):
    sha = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            sha.update(chunk)
    if sha.hexdigest() != str(expected).lower():
        raise UpdateError('The downloaded update failed its integrity check (SHA-256 mismatch).')


def download_file(url, dest, max_bytes=MAX_DOWNLOAD_BYTES, progress=None, get=requests.get):
    """Download `url` to `dest`, following only allowed redirects, capped at `max_bytes`."""
    dest = Path(dest)
    part = dest.with_name(dest.name + '.part')
    try:
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            if not is_allowed_url(current, first_hop=(hop == 0)):
                raise UpdateError('The update download address is not trusted; update refused.')
            try:
                response = get(current, stream=True, allow_redirects=False, timeout=(5, 30))
            except requests.RequestException:
                raise UpdateError('The update download failed. Check your internet connection.') from None
            try:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get('Location')
                    if not location:
                        raise UpdateError('The update download was redirected without a destination.')
                    current = urljoin(current, location)
                    continue
                if response.status_code != 200:
                    raise UpdateError(f'The update download failed (HTTP {response.status_code}).')
                total = response.headers.get('Content-Length')
                total = int(total) if total and total.isdigit() else None
                if total is not None and total > max_bytes:
                    raise UpdateError('The update file is larger than allowed; update refused.')
                done = 0
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    with open(part, 'wb') as handle:
                        for chunk in response.iter_content(chunk_size=65536):
                            if not chunk:
                                continue
                            done += len(chunk)
                            if done > max_bytes:
                                raise UpdateError('The update file is larger than allowed; update refused.')
                            handle.write(chunk)
                            if progress:
                                progress(done, total)
                except requests.RequestException:
                    raise UpdateError('The update download was interrupted.') from None
                os.replace(part, dest)
                return
            finally:
                close = getattr(response, 'close', None)
                if close:
                    close()
        raise UpdateError('The update download was redirected too many times.')
    except OSError as exc:
        raise UpdateError(f'Could not save the update file: {exc.strerror or type(exc).__name__}') from None
    finally:
        try:
            part.unlink()
        except OSError:
            pass


def _clean_member_name(name):
    normalized = name.replace('\\', '/')
    if (not normalized or normalized.startswith('/') or ':' in normalized
            or any(part in ('..', '') for part in normalized.rstrip('/').split('/'))):
        raise UpdateError(f'The update archive contains an unsafe path: {name!r}')
    return normalized


def safe_extract(zip_path, dest):
    """Extract a release zip into `dest` after validating every entry. Returns `dest`."""
    dest = Path(dest)
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError):
        raise UpdateError('The update file is not a valid zip archive.') from None
    with archive:
        members = []
        total = 0
        for info in archive.infolist():
            clean = _clean_member_name(info.filename)
            total += info.file_size
            if total > MAX_EXTRACTED_BYTES:
                raise UpdateError('The update archive expands to more than the allowed size.')
            members.append((info, clean))
        names = {clean for _, clean in members}
        if APP_EXE not in names or not any(name.startswith(APP_LIBS + '/') for name in names):
            raise UpdateError(f'The update archive does not contain {APP_EXE} and {APP_LIBS}.')
        try:
            dest.mkdir(parents=True, exist_ok=True)
            root = dest.resolve()
            written = 0
            for info, clean in members:
                target = (root / clean).resolve()
                if root != target and root not in target.parents:
                    raise UpdateError(f'The update archive contains an unsafe path: {info.filename!r}')
                if clean.endswith('/') or info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, open(target, 'wb') as out:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        written += len(chunk)
                        if written > MAX_EXTRACTED_BYTES:
                            raise UpdateError('The update archive expands to more than the allowed size.')
                        out.write(chunk)
        except (UpdateError, OSError) as exc:
            shutil.rmtree(dest, ignore_errors=True)
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f'Could not unpack the update: {exc.strerror or type(exc).__name__}') from None
    return dest


def stage_update(info, work_dir, progress=None, get=requests.get):
    """Download, verify and unpack `info` under `work_dir`. Returns the unpacked folder."""
    version_dir = Path(work_dir) / f'v{info.version}'
    try:
        shutil.rmtree(version_dir, ignore_errors=True)
        version_dir.mkdir(parents=True, exist_ok=True)
        hash_file = version_dir / (info.zip_name + '.sha256')
        download_file(info.sha256_url, hash_file, max_bytes=MAX_HASH_FILE_BYTES, get=get)
        try:
            expected = parse_sha256(hash_file.read_text(encoding='utf-8', errors='replace'), info.zip_name)
        except OSError:
            raise UpdateError('Could not read the downloaded hash file.') from None
        zip_path = version_dir / info.zip_name
        download_file(info.zip_url, zip_path, progress=progress, get=get)
        verify_sha256(zip_path, expected)
        staged = safe_extract(zip_path, version_dir / 'staged')
        zip_path.unlink(missing_ok=True)
        hash_file.unlink(missing_ok=True)
        return staged
    except BaseException:
        shutil.rmtree(version_dir, ignore_errors=True)
        raise


APPLY_SCRIPT = r'''param(
    [Parameter(Mandatory = $true)][int]$ProcessId,
    [Parameter(Mandatory = $true)][string]$Staged,
    [Parameter(Mandatory = $true)][string]$Install,
    [Parameter(Mandatory = $true)][string]$Log,
    [string]$RelaunchArgs = '',
    [switch]$NoLaunch,
    [switch]$SimulateFailure,
    [switch]$SimulateRollbackExeFailure
)
$ErrorActionPreference = 'Stop'
$Exe = 'WuwaTappoAutoClaim.exe'
$Libs = '_internal'

function Write-Log([string]$Message) {
    try {
        Add-Content -LiteralPath $Log -Value ('{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message) -Encoding UTF8
    } catch { }
}

function Start-App {
    if ($NoLaunch) { Write-Log 'launch skipped'; return }
    try {
        # Process.Start takes a literal path; Start-Process would treat [ ] in it as wildcards.
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = (Join-Path $Install $Exe)
        $psi.WorkingDirectory = $Install
        $psi.UseShellExecute = $true
        if ($RelaunchArgs) { $psi.Arguments = $RelaunchArgs }
        [void][System.Diagnostics.Process]::Start($psi)
        Write-Log 'app started'
    } catch {
        Write-Log ('could not start app: ' + $_.Exception.Message)
    }
}

function Rename-WithRetry([string]$Path, [string]$NewName) {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try { Rename-Item -LiteralPath $Path -NewName $NewName; return }
        catch { if ($attempt -eq 5) { throw }; Start-Sleep -Seconds 1 }
    }
}

function Remove-WithRetry([string]$Path) {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try { Remove-Item -LiteralPath $Path -Recurse -Force; return }
        catch { if ($attempt -eq 5) { throw }; Start-Sleep -Seconds 1 }
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Log) | Out-Null
Write-Log "update started (waiting for process $ProcessId)"

$limit = 60
if ($env:WTAC_WAIT_SECONDS) { $limit = [int]$env:WTAC_WAIT_SECONDS }
$stop = (Get-Date).AddSeconds($limit)
while ((Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) -and ((Get-Date) -lt $stop)) {
    Start-Sleep -Milliseconds 250
}
if (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue) {
    Write-Log 'app is still running; update aborted and nothing was changed'
    exit 1
}

$curExe = Join-Path $Install $Exe
$curLibs = Join-Path $Install $Libs
$oldExe = $curExe + '.old'
$oldLibs = $curLibs + '.old'
$renamedExe = $false
$renamedLibs = $false
$flag = Join-Path (Split-Path -Parent $Log) 'rollback-failed.flag'
try {
    Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
    foreach ($stale in @($oldExe, $oldLibs)) {
        if (Test-Path -LiteralPath $stale) { Remove-Item -LiteralPath $stale -Recurse -Force }
    }
    Rename-WithRetry $curExe ($Exe + '.old')
    $renamedExe = $true
    Rename-WithRetry $curLibs ($Libs + '.old')
    $renamedLibs = $true
    Write-Log 'old files renamed to .old'
    Get-ChildItem -LiteralPath $Staged -Force | Copy-Item -Destination $Install -Recurse -Force
    Write-Log 'new files copied'
    if ($SimulateFailure) { throw 'simulated failure' }
    Write-Log 'update succeeded'
    Start-App
    exit 0
} catch {
    Write-Log ('update failed: ' + $_.Exception.Message + '; rolling back')
    # Each restore has its own try so one failure never skips the other.
    $rollbackOk = $true
    if ($renamedExe) {
        try {
            if ($SimulateRollbackExeFailure) { throw 'simulated rollback failure' }
            if (Test-Path -LiteralPath $curExe) { Remove-WithRetry $curExe }
            Rename-WithRetry $oldExe $Exe
        } catch {
            $rollbackOk = $false
            Write-Log ('rollback of the app file failed: ' + $_.Exception.Message)
        }
    }
    if ($renamedLibs) {
        try {
            if (Test-Path -LiteralPath $curLibs) { Remove-WithRetry $curLibs }
            Rename-WithRetry $oldLibs $Libs
        } catch {
            $rollbackOk = $false
            Write-Log ('rollback of the libraries failed: ' + $_.Exception.Message)
        }
    }
    if ($rollbackOk) {
        Write-Log 'rollback complete'
    } else {
        Write-Log 'rollback failed; backups (.old) are kept for manual recovery'
        try { Set-Content -LiteralPath $flag -Value 'rollback failed' } catch { }
    }
    Start-App
    exit 1
}
'''

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008


def write_apply_script(work_dir):
    work_dir = Path(work_dir)
    script = work_dir / 'apply-update.ps1'
    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        script.write_text(APPLY_SCRIPT, encoding='utf-8-sig')
    except OSError as exc:
        raise UpdateError(f'Could not prepare the update helper: {exc.strerror or type(exc).__name__}') from None
    return script


def launch_apply(staged, install_dir, pid, work_dir, relaunch_args=None):
    """Start the swap script detached; it waits for this process (`pid`) to exit."""
    work_dir = Path(work_dir)
    script = write_apply_script(work_dir)
    command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-WindowStyle', 'Hidden',
               '-File', str(script), '-ProcessId', str(pid), '-Staged', str(staged),
               '-Install', str(install_dir), '-Log', str(work_dir / 'update.log')]
    if relaunch_args:
        command += ['-RelaunchArgs', ' '.join(relaunch_args)]
    try:
        subprocess.Popen(command, creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise UpdateError(f'Could not start the update helper: {exc.strerror or type(exc).__name__}') from None


def preflight_install_dir(install_dir):
    """Fail early (before the app quits) when the install folder cannot be modified."""
    install_dir = Path(install_dir)
    probe = install_dir / f'.wtac-write-test-{os.getpid()}'
    try:
        probe.write_bytes(b'x')
        probe.unlink()
    except OSError:
        raise UpdateError(f'The install folder is not writable: {install_dir}') from None


def cleanup_after_update(install_dir, work_dir):
    """Best effort: drop backups and staging left by a finished update. Never raises.

    After a failed rollback the .old backups may be the only good copy, so they stay.
    """
    keep_backups = (Path(work_dir) / ROLLBACK_FLAG).exists()
    for name in (APP_EXE + '.old', APP_LIBS + '.old'):
        if keep_backups:
            break
        target = Path(install_dir) / name
        try:
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
        except OSError:
            pass
    try:
        for entry in Path(work_dir).iterdir():
            if entry.name in ('update.log', ROLLBACK_FLAG):
                continue
            try:
                if entry.is_dir():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                pass
    except OSError:
        pass
