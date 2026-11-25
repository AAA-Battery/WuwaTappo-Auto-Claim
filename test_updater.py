import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import updater
from updater import UpdateError, UpdateInfo

REPO_URL = 'https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download'


def release(tag, assets='both', draft=False, prerelease=False):
    version = tag.lstrip('v')
    zip_name = f'WuwaTappoAutoClaim-v{version}.zip'
    available = {
        'zip': {'name': zip_name, 'browser_download_url': f'{REPO_URL}/{tag}/{zip_name}'},
        'sha': {'name': zip_name + '.sha256', 'browser_download_url': f'{REPO_URL}/{tag}/{zip_name}.sha256'},
    }
    chosen = {'both': ['zip', 'sha'], 'zip': ['zip'], 'sha': ['sha'], 'none': []}[assets]
    return {
        'tag_name': tag, 'draft': draft, 'prerelease': prerelease,
        'html_url': f'https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/tag/{tag}',
        'assets': [available[key] for key in chosen],
    }


class FakeJsonResponse:
    def __init__(self, status=200, payload=None, text=None):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class VersionAndReleaseTests(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(updater.parse_version('v1.2.10'), (1, 2, 10))
        self.assertEqual(updater.parse_version('1.3'), (1, 3, 0))
        for bad in ('v1.3.0-beta', '', 'latest', None, 'v1.x.0', '1.2.3.4'):
            self.assertIsNone(updater.parse_version(bad), bad)

    def test_newer_release_returns_info(self):
        info = updater.parse_release(release('v1.3.0'), '1.2.2')
        self.assertEqual(info.version, '1.3.0')
        self.assertEqual(info.zip_name, 'WuwaTappoAutoClaim-v1.3.0.zip')
        self.assertEqual(info.zip_url, f'{REPO_URL}/v1.3.0/WuwaTappoAutoClaim-v1.3.0.zip')
        self.assertEqual(info.sha256_url, info.zip_url + '.sha256')
        self.assertTrue(info.page_url.endswith('/tag/v1.3.0'))

    def test_numeric_not_lexical_comparison(self):
        self.assertIsNotNone(updater.parse_release(release('v1.2.10'), '1.2.9'))
        self.assertIsNone(updater.parse_release(release('v1.2.9'), '1.2.10'))

    def test_not_newer_returns_none(self):
        for rel, current in (
            (release('v1.2.2'), '1.2.2'),
            (release('v1.2.1'), '1.2.2'),
            (release('v1.3.0', draft=True), '1.2.2'),
            (release('v1.3.0', prerelease=True), '1.2.2'),
            (release('v1.3.0-beta'), '1.2.2'),
            ({'tag_name': None, 'assets': []}, '1.2.2'),
            ({}, '1.2.2'),
        ):
            self.assertIsNone(updater.parse_release(rel, current))

    def test_missing_assets_raise(self):
        for assets, needle in (('zip', '.sha256'), ('sha', '.zip'), ('none', '.zip')):
            with self.subTest(assets=assets):
                with self.assertRaises(UpdateError) as ctx:
                    updater.parse_release(release('v1.3.0', assets=assets), '1.2.2')
                self.assertIn(needle, str(ctx.exception))

    def test_check_for_update_errors_become_update_error(self):
        def raising(*args, **kwargs):
            raise requests.ConnectionError('offline')

        cases = {
            'offline': raising,
            'rate limit': lambda *a, **k: FakeJsonResponse(403, {'message': 'rate limit'}),
            'too many': lambda *a, **k: FakeJsonResponse(429, {}),
            'server error': lambda *a, **k: FakeJsonResponse(500, {}),
            'not json': lambda *a, **k: FakeJsonResponse(200, None),
            'list json': lambda *a, **k: FakeJsonResponse(200, [1, 2]),
        }
        for name, get in cases.items():
            with self.subTest(name):
                with self.assertRaises(UpdateError):
                    updater.check_for_update('1.2.2', get=get)

    def test_check_for_update_success_and_uptodate(self):
        calls = []

        def get(url, **kwargs):
            calls.append((url, kwargs))
            return FakeJsonResponse(200, release('v1.3.0'))

        self.assertEqual(updater.check_for_update('1.2.2', get=get).version, '1.3.0')
        self.assertIsNone(updater.check_for_update('1.3.0', get=get))
        self.assertEqual(calls[0][0], updater.LATEST_URL)
        self.assertEqual(calls[0][1]['timeout'], (5, 15))

    def test_rate_limit_message_is_friendly(self):
        with self.assertRaises(UpdateError) as ctx:
            updater.check_for_update('1.2.2', get=lambda *a, **k: FakeJsonResponse(403, {}))
        self.assertIn('rate limit', str(ctx.exception).lower())

    def test_is_allowed_url(self):
        good = f'{REPO_URL}/v1.3.0/x.zip'
        self.assertTrue(updater.is_allowed_url(good, first_hop=True))
        for bad in (
            good.replace('https://', 'http://'),
            good.replace('AAA-Battery', 'someone-else'),
            'https://evil.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/v1/x.zip',
            'https://github.com.evil.com/AAA-Battery/WuwaTappo-Auto-Claim/releases/download/v1/x.zip',
            'https://github.com/AAA-Battery/WuwaTappo-Auto-Claim/archive/main.zip',
            '',
        ):
            self.assertFalse(updater.is_allowed_url(bad, first_hop=True), bad)
        for ok in ('https://objects.githubusercontent.com/a/b', 'https://github.com/x/y'):
            self.assertTrue(updater.is_allowed_url(ok, first_hop=False), ok)
        for bad in ('https://evil.com/', 'http://objects.githubusercontent.com/a',
                    'https://githubusercontent.com.evil.com/a', 'https://evilgithubusercontent.com/a'):
            self.assertFalse(updater.is_allowed_url(bad, first_hop=False), bad)

    def test_parse_sha256(self):
        digest = hashlib.sha256(b'x').hexdigest()
        name = 'WuwaTappoAutoClaim-v1.3.0.zip'
        self.assertEqual(updater.parse_sha256(digest + '\n', name), digest)
        self.assertEqual(updater.parse_sha256(f'{digest}  {name}\n', name), digest)
        self.assertEqual(updater.parse_sha256(digest.upper(), name), digest)
        for bad in ('', 'abc', 'z' * 64, digest[:-1], f'{digest}  other.zip'):
            with self.assertRaises(UpdateError, msg=bad):
                updater.parse_sha256(bad, name)

    def test_verify_sha256(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'f.bin'
            path.write_bytes(b'hello')
            updater.verify_sha256(path, hashlib.sha256(b'hello').hexdigest())
            with self.assertRaises(UpdateError):
                updater.verify_sha256(path, hashlib.sha256(b'other').hexdigest())


class FakeResponse:
    def __init__(self, status=200, body=b'', headers=None):
        self.status_code = status
        self.headers = dict(headers or {})
        self._body = body
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for start in range(0, len(self._body), chunk_size):
            yield self._body[start:start + chunk_size]

    def close(self):
        self.closed = True


def make_zip(path, members):
    import zipfile
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(name, data)


GOOD_MEMBERS = {
    'WuwaTappoAutoClaim.exe': b'exe-bytes',
    '_internal/lib.dll': b'dll-bytes',
    'README.md': b'readme',
}
FIRST_URL = f'{REPO_URL}/v1.3.0/x.zip'


class DownloadAndExtractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_follows_allowed_redirect_and_reports_progress(self):
        body = b'a' * 150000
        seen = []

        def get(url, **kwargs):
            seen.append((url, kwargs.get('allow_redirects')))
            if url == FIRST_URL:
                return FakeResponse(302, headers={'Location': 'https://objects.githubusercontent.com/blob/1'})
            return FakeResponse(200, body, {'Content-Length': str(len(body))})

        progress = []
        dest = self.dir / 'x.zip'
        updater.download_file(FIRST_URL, dest, progress=lambda done, total: progress.append((done, total)), get=get)
        self.assertEqual(dest.read_bytes(), body)
        self.assertEqual(progress[-1], (len(body), len(body)))
        self.assertEqual([flag for _, flag in seen], [False, False])

    def test_download_rejects_redirect_to_other_host(self):
        def get(url, **kwargs):
            return FakeResponse(302, headers={'Location': 'https://evil.com/x'})

        dest = self.dir / 'x.zip'
        with self.assertRaises(UpdateError):
            updater.download_file(FIRST_URL, dest, get=get)
        self.assertFalse(dest.exists())

    def test_download_rejects_first_url_outside_the_release_path(self):
        with self.assertRaises(UpdateError):
            updater.download_file('https://evil.com/x.zip', self.dir / 'x.zip',
                                  get=lambda *a, **k: FakeResponse(200, b'x'))

    def test_download_rejects_oversize_body_and_header(self):
        dest = self.dir / 'x.zip'
        with self.assertRaises(UpdateError):
            updater.download_file(FIRST_URL, dest, max_bytes=10,
                                  get=lambda *a, **k: FakeResponse(200, b'x' * 100))
        self.assertFalse(dest.exists())
        with self.assertRaises(UpdateError):
            updater.download_file(FIRST_URL, dest, max_bytes=10,
                                  get=lambda *a, **k: FakeResponse(200, b'x', {'Content-Length': '999'}))
        self.assertFalse(dest.exists())

    def test_download_rejects_too_many_redirects(self):
        def get(url, **kwargs):
            return FakeResponse(302, headers={'Location': 'https://github.com/loop'})

        with self.assertRaises(UpdateError):
            updater.download_file(FIRST_URL, self.dir / 'x.zip', get=get)

    def test_download_rejects_http_error(self):
        with self.assertRaises(UpdateError):
            updater.download_file(FIRST_URL, self.dir / 'x.zip', get=lambda *a, **k: FakeResponse(404))

    def test_safe_extract_good_zip(self):
        archive = self.dir / 'ok.zip'
        make_zip(archive, GOOD_MEMBERS)
        dest = self.dir / 'out'
        self.assertEqual(updater.safe_extract(archive, dest), dest)
        for name, data in GOOD_MEMBERS.items():
            self.assertEqual((dest / name).read_bytes(), data)

    def test_safe_extract_rejects_unsafe_and_wrong_layout(self):
        cases = {
            'dotdot': {**GOOD_MEMBERS, '../evil.txt': b'x'},
            'absolute': {**GOOD_MEMBERS, '/abs.txt': b'x'},
            'drive': {**GOOD_MEMBERS, 'C:/x.txt': b'x'},
            'nested dotdot': {**GOOD_MEMBERS, '_internal/../../evil.txt': b'x'},
            'backslash dotdot': {**GOOD_MEMBERS, '..\\evil.txt': b'x'},
            'no _internal': {'WuwaTappoAutoClaim.exe': b'x'},
            'no exe': {'_internal/lib.dll': b'x'},
        }
        for name, members in cases.items():
            with self.subTest(name):
                archive = self.dir / 'bad.zip'
                make_zip(archive, members)
                dest = self.dir / 'out' / name.replace(' ', '_')
                with self.assertRaises(UpdateError):
                    updater.safe_extract(archive, dest)
                self.assertEqual([p for p in dest.rglob('*')] if dest.exists() else [], [])
                self.assertFalse((self.dir / 'evil.txt').exists())
                self.assertFalse((self.dir / 'out' / 'evil.txt').exists())

    def test_safe_extract_rejects_zip_bomb(self):
        archive = self.dir / 'bomb.zip'
        make_zip(archive, GOOD_MEMBERS)
        original = updater.MAX_EXTRACTED_BYTES
        updater.MAX_EXTRACTED_BYTES = 5
        try:
            with self.assertRaises(UpdateError):
                updater.safe_extract(archive, self.dir / 'out')
        finally:
            updater.MAX_EXTRACTED_BYTES = original

    def _info(self):
        return UpdateInfo('1.3.0', 'WuwaTappoAutoClaim-v1.3.0.zip',
                          f'{REPO_URL}/v1.3.0/WuwaTappoAutoClaim-v1.3.0.zip',
                          f'{REPO_URL}/v1.3.0/WuwaTappoAutoClaim-v1.3.0.zip.sha256', 'https://x')

    def _serve(self, zip_bytes, sha_text):
        info = self._info()

        def get(url, **kwargs):
            if url == info.zip_url:
                return FakeResponse(200, zip_bytes, {'Content-Length': str(len(zip_bytes))})
            if url == info.sha256_url:
                return FakeResponse(200, sha_text.encode())
            raise AssertionError(url)
        return get

    def test_stage_update_end_to_end(self):
        archive = self.dir / 'src.zip'
        make_zip(archive, GOOD_MEMBERS)
        data = archive.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        work = self.dir / 'work'
        staged = updater.stage_update(
            self._info(), work, get=self._serve(data, f'{digest}  WuwaTappoAutoClaim-v1.3.0.zip\n'))
        self.assertEqual((staged / 'WuwaTappoAutoClaim.exe').read_bytes(), b'exe-bytes')
        self.assertEqual(list(work.rglob('*.zip')), [])

    def test_stage_update_wrong_hash_leaves_nothing(self):
        archive = self.dir / 'src.zip'
        make_zip(archive, GOOD_MEMBERS)
        work = self.dir / 'work'
        with self.assertRaises(UpdateError):
            updater.stage_update(self._info(), work,
                                 get=self._serve(archive.read_bytes(), hashlib.sha256(b'other').hexdigest()))
        self.assertEqual([p for p in work.rglob('*') if p.is_file()], [])

    def test_stage_update_bad_zip_layout_leaves_nothing(self):
        archive = self.dir / 'src.zip'
        make_zip(archive, {'WuwaTappoAutoClaim.exe': b'x'})
        data = archive.read_bytes()
        work = self.dir / 'work'
        with self.assertRaises(UpdateError):
            updater.stage_update(self._info(), work,
                                 get=self._serve(data, hashlib.sha256(data).hexdigest()))
        self.assertEqual([p for p in work.rglob('*') if p.is_file()], [])


import os
import shutil
import subprocess
import time

POWERSHELL = shutil.which('powershell')


def write_tree(base, files):
    for name, data in files.items():
        target = Path(base) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


class ApplyScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        # Spaces and a non-ASCII character exercise PowerShell argument quoting.
        self.install = base / 'inst é dir'
        self.staged = base / 'stg é dir'
        self.work = base / 'wrk é dir'
        write_tree(self.install, {'WuwaTappoAutoClaim.exe': b'old', '_internal/old.dll': b'o', 'user.txt': b'u'})
        write_tree(self.staged, {'WuwaTappoAutoClaim.exe': b'new', '_internal/new.dll': b'n', 'README.md': b'r'})
        self.script = updater.write_apply_script(self.work)
        self.log = self.work / 'update.log'

    def tearDown(self):
        # An app the script just launched may hold its exe open for a moment.
        for attempt in range(20):
            try:
                self.tmp.cleanup()
                return
            except PermissionError:
                time.sleep(0.25)
        self.tmp.cleanup()

    def run_script(self, *flags, pid=99999999, env=None):
        command = [POWERSHELL, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(self.script),
                   '-ProcessId', str(pid), '-Staged', str(self.staged), '-Install', str(self.install),
                   '-Log', str(self.log), *flags]
        return subprocess.run(command, capture_output=True, text=True, timeout=90, env=env)

    def log_text(self):
        return self.log.read_text(encoding='utf-8-sig') if self.log.exists() else ''

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_success_swaps_files(self):
        result = self.run_script('-NoLaunch')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe').read_bytes(), b'new')
        self.assertTrue((self.install / '_internal' / 'new.dll').exists())
        self.assertFalse((self.install / '_internal' / 'old.dll').exists())
        self.assertEqual((self.install / 'user.txt').read_bytes(), b'u')
        self.assertEqual((self.install / 'README.md').read_bytes(), b'r')
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe.old').read_bytes(), b'old')
        self.assertTrue((self.install / '_internal.old' / 'old.dll').exists())
        self.assertIn('update succeeded', self.log_text())

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_failure_rolls_back(self):
        result = self.run_script('-NoLaunch', '-SimulateFailure')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe').read_bytes(), b'old')
        self.assertTrue((self.install / '_internal' / 'old.dll').exists())
        self.assertFalse((self.install / '_internal' / 'new.dll').exists())
        self.assertEqual((self.install / 'user.txt').read_bytes(), b'u')
        self.assertFalse((self.install / 'WuwaTappoAutoClaim.exe.old').exists())
        self.assertFalse((self.install / '_internal.old').exists())
        self.assertIn('rolling back', self.log_text())

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_rollback_continues_when_the_exe_restore_fails(self):
        result = self.run_script('-NoLaunch', '-SimulateFailure', '-SimulateRollbackExeFailure')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # The libraries were still restored and the old exe backup is kept for recovery.
        self.assertTrue((self.install / '_internal' / 'old.dll').exists())
        self.assertFalse((self.install / '_internal' / 'new.dll').exists())
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe.old').read_bytes(), b'old')
        self.assertTrue((self.work / 'rollback-failed.flag').exists())
        self.assertIn('rollback failed', self.log_text())

    def test_cleanup_keeps_backups_after_a_failed_rollback(self):
        write_tree(self.install, {'WuwaTappoAutoClaim.exe.old': b'o', '_internal.old/x.dll': b'x'})
        write_tree(self.work, {'rollback-failed.flag': b'x', 'v1.3.0/staged/a.txt': b'a', 'update.log': b'l'})
        updater.cleanup_after_update(self.install, self.work)
        self.assertTrue((self.install / 'WuwaTappoAutoClaim.exe.old').exists())
        self.assertTrue((self.install / '_internal.old' / 'x.dll').exists())
        self.assertTrue((self.work / 'rollback-failed.flag').exists())
        self.assertFalse((self.work / 'v1.3.0').exists())

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_successful_update_clears_a_stale_rollback_marker(self):
        write_tree(self.work, {'rollback-failed.flag': b'x'})
        result = self.run_script('-NoLaunch')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.work / 'rollback-failed.flag').exists())

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_relaunch_works_when_the_install_path_contains_brackets(self):
        whoami = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'whoami.exe'
        if not whoami.exists():
            self.skipTest('whoami.exe not available')
        self.install = Path(self.tmp.name) / "inst [x] it's"
        write_tree(self.install, {'WuwaTappoAutoClaim.exe': b'old', '_internal/old.dll': b'o'})
        (self.staged / 'WuwaTappoAutoClaim.exe').write_bytes(whoami.read_bytes())
        result = self.run_script()  # no -NoLaunch: the script must really start the app
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('app started', self.log_text())
        self.assertNotIn('could not start app', self.log_text())

    def test_launch_apply_reports_script_write_errors_as_update_error(self):
        blocker = Path(self.tmp.name) / 'blocker'
        blocker.write_text('a file where the work folder should be')
        with self.assertRaises(UpdateError):
            updater.launch_apply(self.staged, self.install, 1, blocker)

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_failure_before_any_rename_does_not_delete_the_install(self):
        shutil.rmtree(self.install / '_internal')  # second rename will fail
        result = self.run_script('-NoLaunch')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe').read_bytes(), b'old')
        self.assertEqual((self.install / 'user.txt').read_bytes(), b'u')

    @unittest.skipUnless(POWERSHELL, 'powershell not available')
    def test_running_process_aborts_without_changes(self):
        child = subprocess.Popen(['cmd', '/c', 'ping -n 30 127.0.0.1 > nul'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            env = dict(os.environ, WTAC_WAIT_SECONDS='2')
            result = self.run_script('-NoLaunch', pid=child.pid, env=env)
        finally:
            child.kill()
            child.wait()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((self.install / 'WuwaTappoAutoClaim.exe').read_bytes(), b'old')
        self.assertFalse((self.install / 'WuwaTappoAutoClaim.exe.old').exists())
        self.assertIn('still running', self.log_text())

    def test_cleanup_after_update_removes_old_and_staging(self):
        write_tree(self.install, {'WuwaTappoAutoClaim.exe.old': b'o', '_internal.old/x.dll': b'x'})
        write_tree(self.work, {'v1.3.0/staged/a.txt': b'a', 'v1.3.0/x.zip': b'z', 'update.log': b'log'})
        updater.cleanup_after_update(self.install, self.work)
        self.assertFalse((self.install / 'WuwaTappoAutoClaim.exe.old').exists())
        self.assertFalse((self.install / '_internal.old').exists())
        self.assertFalse((self.work / 'v1.3.0').exists())
        self.assertTrue((self.work / 'update.log').exists())
        self.assertTrue((self.install / 'WuwaTappoAutoClaim.exe').exists())

    def test_cleanup_never_raises(self):
        updater.cleanup_after_update(self.install / 'missing', self.work / 'missing')

    def test_preflight_install_dir(self):
        updater.preflight_install_dir(self.install)
        self.assertEqual(sorted(p.name for p in self.install.iterdir()),
                         ['WuwaTappoAutoClaim.exe', '_internal', 'user.txt'])
        with self.assertRaises(UpdateError):
            updater.preflight_install_dir(self.install / 'missing')

    def test_launch_apply_builds_expected_command(self):
        captured = {}

        def fake_popen(command, **kwargs):
            captured['command'] = command
            captured['kwargs'] = kwargs
            return object()

        original = updater.subprocess.Popen
        updater.subprocess.Popen = fake_popen
        try:
            updater.launch_apply(self.staged, self.install, 4321, self.work, relaunch_args=['--enable'])
        finally:
            updater.subprocess.Popen = original
        command = captured['command']
        self.assertEqual(command[:4], ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass'])
        for flag, value in (('-ProcessId', '4321'), ('-Staged', str(self.staged)), ('-Install', str(self.install)),
                            ('-Log', str(self.work / 'update.log')), ('-RelaunchArgs', '--enable')):
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(command[command.index('-File') + 1], str(self.work / 'apply-update.ps1'))
        self.assertTrue(captured['kwargs']['creationflags'] & 0x08000000)  # CREATE_NO_WINDOW

    def test_script_file_has_bom_and_parameters(self):
        raw = self.script.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        text = raw.decode('utf-8-sig')
        for name in ('ProcessId', 'Staged', 'Install', 'Log', 'RelaunchArgs', 'NoLaunch', 'SimulateFailure'):
            self.assertIn('$' + name, text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
