import contextlib
import hashlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import updater

spec = importlib.util.spec_from_file_location('make_release', ROOT / 'tools' / 'make_release.py')
make_release = importlib.util.module_from_spec(spec)


def load():
    spec.loader.exec_module(make_release)
    return make_release


class MakeReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.dist = base / 'dist' / 'WuwaTappoAutoClaim'
        (self.dist / '_internal').mkdir(parents=True)
        (self.dist / 'WuwaTappoAutoClaim.exe').write_bytes(b'exe')
        (self.dist / '_internal' / 'x.dll').write_bytes(b'dll')
        self.extras = []
        for name in ('LICENSE', 'README.md'):
            path = base / name
            path.write_text(name)
            self.extras.append(path)
        self.out = base / 'out'

    def tearDown(self):
        self.tmp.cleanup()

    def test_zip_layout_and_hash_roundtrip(self):
        module = load()
        zip_path, sha_path = module.build_release(self.dist, self.extras, '1.3.0', self.out)
        self.assertEqual(zip_path.name, 'WuwaTappoAutoClaim-v1.3.0.zip')
        self.assertEqual(sha_path.name, 'WuwaTappoAutoClaim-v1.3.0.zip.sha256')
        import zipfile
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
        self.assertTrue({'WuwaTappoAutoClaim.exe', '_internal/x.dll', 'LICENSE', 'README.md'} <= names)
        real = hashlib.sha256(zip_path.read_bytes()).hexdigest()
        self.assertEqual(updater.parse_sha256(sha_path.read_text(), zip_path.name), real)
        self.assertEqual(sha_path.read_text(), f'{real}  {zip_path.name}\n')
        extracted = updater.safe_extract(zip_path, Path(self.tmp.name) / 'extracted')
        self.assertEqual((extracted / 'WuwaTappoAutoClaim.exe').read_bytes(), b'exe')
        updater.verify_sha256(zip_path, real)

    def test_rejects_dist_without_exe_or_internal(self):
        module = load()
        (self.dist / 'WuwaTappoAutoClaim.exe').unlink()
        with self.assertRaises(ValueError):
            module.build_release(self.dist, self.extras, '1.3.0', self.out)

    def test_rejects_bad_version(self):
        module = load()
        for bad in ('1.3', 'v1.3.0', '1.3.0-beta', ''):
            with self.assertRaises(ValueError, msg=bad):
                module.build_release(self.dist, self.extras, bad, self.out)

    def test_read_app_version(self):
        module = load()
        source = Path(self.tmp.name) / 'main.py'
        source.write_text("import os\nVERSION = '1.3.0'\nX = 1\n")
        self.assertEqual(module.read_app_version(source), '1.3.0')
        source.write_text('nothing here')
        with self.assertRaises(ValueError):
            module.read_app_version(source)

    def test_cli_refuses_a_version_that_does_not_match_the_app(self):
        module = load()
        source = Path(self.tmp.name) / 'main.py'
        source.write_text("VERSION = '1.3.0'\n")
        argv = ['1.3.1', '--dist', str(self.dist), '--out', str(self.out), '--app-source', str(source)]
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            code = module.main(argv)
        self.assertEqual(code, 1)
        self.assertIn('1.3.0', captured.getvalue())
        self.assertFalse(any(self.out.glob('*.zip')) if self.out.exists() else False)

    def test_cli_builds_when_the_version_matches(self):
        module = load()
        source = Path(self.tmp.name) / 'main.py'
        source.write_text("VERSION = '1.3.0'\n")
        argv = ['1.3.0', '--dist', str(self.dist), '--out', str(self.out), '--app-source', str(source)]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = module.main(argv)
        self.assertEqual(code, 0)
        self.assertTrue((self.out / 'WuwaTappoAutoClaim-v1.3.0.zip').exists())

    def test_missing_extra_is_skipped_not_fatal(self):
        module = load()
        extras = self.extras + [Path(self.tmp.name) / 'Run-Background.vbs']
        with contextlib.redirect_stderr(io.StringIO()) as captured:
            zip_path, _ = module.build_release(self.dist, extras, '1.3.0', self.out)
        self.assertIn('Run-Background.vbs not found', captured.getvalue())
        import zipfile
        with zipfile.ZipFile(zip_path) as archive:
            self.assertNotIn('Run-Background.vbs', archive.namelist())


if __name__ == '__main__':
    unittest.main(verbosity=2)
