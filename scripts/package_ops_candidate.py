"""Build a deterministic stdlib/source-only Lambda candidate; never upload or deploy."""
from pathlib import Path
import base64
import hashlib
import json
import subprocess
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def candidate_files():
    files = [ROOT / 'lambda_function.py']
    for package in ('swing_core', 'swing_v5', 'swing_ops', 'runtime_v5'):
        files.extend(sorted((ROOT / package).glob('*.py')))
    files.extend(sorted((ROOT / 'config').rglob('*.json')))
    files.extend(sorted((ROOT / 'universes').glob('*.csv')))
    files.append(ROOT / 'artifacts/v5/forward/instrument_resolution.csv')
    shadow = ROOT / 'backtest/artifacts/v4/model.candidate.json'
    if shadow.exists():
        files.append(shadow)
    return sorted(set(files))


def main():
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    bundle = destination / 'roe_fii_ops_candidate.zip'
    entries = []
    with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in candidate_files():
            body = path.read_bytes()
            relative = path.relative_to(ROOT).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, body)
            entries.append({'path': relative, 'sha256': hashlib.sha256(body).hexdigest()})
    sha = hashlib.sha256(bundle.read_bytes()).digest()
    manifest = {'deployed': False,
                'base_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'working_tree_dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'code_sha256_base64': base64.b64encode(sha).decode(), 'zip_sha256': sha.hex(),
                'files': entries}
    (destination / 'roe_fii_ops_candidate.manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(json.dumps({'zip': str(bundle), 'files': len(entries), 'deployed': False,
                      'code_sha256_base64': manifest['code_sha256_base64']}, indent=2))


if __name__ == '__main__':
    main()
