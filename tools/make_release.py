"""Build the two files a GitHub Release needs for in-app updates.

    python tools/make_release.py 1.3.0

Creates WuwaTappoAutoClaim-v1.3.0.zip and WuwaTappoAutoClaim-v1.3.0.zip.sha256 in
release-assets/. Upload both to a GitHub Release tagged v1.3.0; the app refuses to
update from a release that has no .sha256 file.
"""
import argparse
import hashlib
from pathlib import Path
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parent.parent
APP_EXE = 'WuwaTappoAutoClaim.exe'
APP_LIBS = '_internal'
DEFAULT_EXTRAS = ('LICENSE', 'README.md', 'Run-Background.vbs', 'Stop-Tool.vbs')


def build_release(dist_dir, extras, version, out_dir):
    """Zip `dist_dir` (+ `extras` at the zip root) and write its .sha256. Returns (zip, sha)."""
    if not re.fullmatch(r'\d+\.\d+\.\d+', str(version)):
        raise ValueError(f'version must look like 1.3.0, got {version!r}')
    dist_dir, out_dir = Path(dist_dir), Path(out_dir)
    if not (dist_dir / APP_EXE).is_file() or not (dist_dir / APP_LIBS).is_dir():
        raise ValueError(f'{dist_dir} must contain {APP_EXE} and {APP_LIBS}/ (run build.bat first)')
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f'WuwaTappoAutoClaim-v{version}.zip'
    sha_path = out_dir / (zip_path.name + '.sha256')
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(dist_dir.rglob('*')):
            if path.is_file():
                archive.write(path, path.relative_to(dist_dir).as_posix())
        for extra in extras:
            extra = Path(extra)
            if extra.is_file():
                archive.write(extra, extra.name)
            else:
                print(f'warning: {extra} not found; left out of the zip', file=sys.stderr)
    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    sha_path.write_text(f'{digest}  {zip_path.name}\n', encoding='ascii')
    return zip_path, sha_path


def read_app_version(source):
    """The VERSION the app reports about itself, read from main.py."""
    match = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", Path(source).read_text(encoding='utf-8'), re.M)
    if not match:
        raise ValueError(f'no VERSION = "..." line found in {source}')
    return match.group(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('version', help='release version, e.g. 1.3.0 (no leading v)')
    parser.add_argument('--dist', default=str(ROOT / 'dist' / 'WuwaTappoAutoClaim'))
    parser.add_argument('--out', default=str(ROOT / 'release-assets'))
    parser.add_argument('--app-source', default=str(ROOT / 'main.py'),
                        help='file holding VERSION (must equal the release version)')
    args = parser.parse_args(argv)
    extras = [ROOT / name for name in DEFAULT_EXTRAS]
    try:
        app_version = read_app_version(args.app_source)
        if app_version != args.version:
            # An update that still reports the old version would be offered again forever.
            raise ValueError(f'release version {args.version} does not match VERSION {app_version} in '
                             f'{args.app_source}; raise VERSION and rebuild first')
        zip_path, sha_path = build_release(args.dist, extras, args.version, args.out)
    except ValueError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    print(zip_path)
    print(sha_path)
    print(f'Upload both files to a GitHub Release tagged v{args.version}.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
