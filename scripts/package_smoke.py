#!/usr/bin/env python3
"""Build/audit both distributions and install them in separate clean venvs.

Only build tools may be downloaded. Artifact installation is --no-index; demo
and CLI checks use synthetic data outside the checkout, with no provider calls.
"""
import argparse
import configparser
from email.parser import BytesParser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[1]
DIST_NAME = 'local-work-assistant'
EXPECTED_DEMO = '9b64c0da68f8d0e975160f084331662c07f902458d04afcf480296667d65f043'
REPORT_FILES = {'index.html', 'daily-sync.md', 'evidence.html', 'evidence.json',
                'card.json', 'document.json', 'card-preview.html'}
SOURCE_FILES = {'README.md', 'LICENSE', 'SECURITY.md', 'pyproject.toml', 'MANIFEST.in',
                'README.en.md', 'docs/README.md', 'docs/quickstart.md', 'docs/quickstart.en.md',
                'docs/capabilities.md', 'docs/positioning.md', 'docs/troubleshooting.md',
                'docs/guide.md', 'docs/assets/demo-report.png',
                'CONTRIBUTING.md', 'CHANGELOG.md', 'docs/releasing.md',
                'scripts/package_smoke.py', 'examples/daily-bundle/state.json',
                'examples/daily-bundle/daily/2026-01-15.html',
                'examples/daily-bundle/evidence/daily-2026-01-15.json'}


def clean_environment():
    keep = {'PATH', 'SystemRoot', 'WINDIR', 'TMP', 'TEMP', 'TMPDIR', 'LANG', 'LC_ALL',
            'SSL_CERT_FILE', 'SSL_CERT_DIR'}
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(PYTHONNOUSERSITE='1', PYTHONUTF8='1', PIP_CONFIG_FILE=os.devnull,
               PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    return env


def check(condition, message):
    if not condition:
        raise ValueError(message)


def archive_files(artifact):
    if artifact.suffix == '.whl':
        with zipfile.ZipFile(artifact) as archive:
            return {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}
    with tarfile.open(artifact) as archive:
        files = {}
        for member in archive.getmembers():
            check(member.isfile() or member.isdir(), 'sdist contains a link or special file')
            if member.isfile():
                files[member.name] = archive.extractfile(member).read()
        roots = {PurePosixPath(name).parts[0] for name in files}
        check(len(roots) == 1, 'sdist must have exactly one root directory')
        return {str(PurePosixPath(name).relative_to(next(iter(roots)))): raw for name, raw in files.items()}


def audit(artifact, modules):
    files = archive_files(artifact)
    forbidden = {'.git', '.local', '.wa', '.venv', '__pycache__', 'out', 'private-lark-cli'}
    for name in files:
        path = PurePosixPath(name)
        check(not path.is_absolute() and '..' not in path.parts and '\\' not in name, 'unsafe archive path')
        check(not forbidden.intersection(path.parts), 'private/runtime directory in archive: ' + name)
        check(not any(p.startswith('.env') or p == 'config.local.json' for p in path.parts), 'local config in archive')
        check(not name.endswith(('.pyc', '.pyo', '.db', '.sqlite', '.sqlite3')), 'runtime database/cache in archive')
    check(modules <= files.keys(), 'distribution is missing runtime modules: ' + str(sorted(modules - files.keys())))
    if artifact.suffix == '.whl':
        check(all(name in modules or '.dist-info/' in name for name in files), 'wheel contains unexpected non-runtime files')
        metadata_name = next(n for n in files if n.endswith('.dist-info/METADATA'))
        entry = next(n for n in files if n.endswith('.dist-info/entry_points.txt'))
        parser = configparser.ConfigParser()
        parser.read_string(files[entry].decode('utf-8'))
        check(parser['console_scripts']['wa'] == 'work_assistant.cli:main', 'wa entry point is missing/wrong')
        check(any(n.endswith('/LICENSE') for n in files), 'wheel license missing')
    else:
        required = SOURCE_FILES | {str(p.relative_to(REPO)) for p in (REPO / 'tests').glob('test_*.py')}
        check(required <= files.keys(), 'sdist omissions: ' + str(sorted(required - files.keys())))
        metadata_name = 'PKG-INFO'
    metadata = BytesParser().parsebytes(files[metadata_name])
    check(metadata['Name'] == DIST_NAME, 'unexpected distribution name')
    check(metadata['Requires-Python'] == '>=3.9', 'unexpected Python requirement')
    check(not metadata.get_all('Requires-Dist'), 'runtime must remain dependency-free')
    check('Development Status :: 3 - Alpha' in metadata.get_all('Classifier', []), 'alpha status missing')
    check({'Operating System :: MacOS', 'Operating System :: POSIX :: Linux'} <= set(metadata.get_all('Classifier', [])), 'supported platform metadata missing')
    check(metadata['License-Expression'] == 'MIT', 'SPDX license missing')
    changelog = files.get('CHANGELOG.md', (REPO / 'CHANGELOG.md').read_bytes()).decode('utf-8')
    check('## [%s] - Unreleased' % metadata['Version'] in changelog, 'version/changelog heading mismatch')
    urls = metadata.get_all('Project-URL', [])
    check(any(u.startswith('Repository, https://github.com/xilinshancun/work-assistant') for u in urls), 'repository URL missing')
    return {'artifact': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'files': len(files), 'version': metadata['Version']}


def smoke(artifacts, output, build_wheelhouse=None):
    output.mkdir(parents=True, exist_ok=False)
    (output / 'logs').mkdir()
    environment = clean_environment()
    sequence = 0

    def run(argv, cwd, label):
        nonlocal sequence
        sequence += 1
        log = output / 'logs' / ('%02d-%s.log' % (sequence, label))
        try:
            process = subprocess.run([str(a) for a in argv], cwd=cwd, env=environment,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        except subprocess.TimeoutExpired as exc:
            log.write_bytes(exc.output or b'')
            raise ValueError('%s timed out; see %s' % (label, log)) from None
        log.write_bytes(process.stdout)
        check(process.returncode == 0, '%s failed (exit %s); see %s' % (label, process.returncode, log))
        return process.stdout.decode('utf-8')

    if artifacts is None:
        artifacts = output / 'dist'
        run([sys.executable, '-I', '-m', 'build', '--outdir', artifacts, REPO], REPO, 'build')
    wheels, sdists = sorted(artifacts.glob('*.whl')), sorted(artifacts.glob('*.tar.gz'))
    check(len(wheels) == len(sdists) == 1, 'provide exactly one wheel and one sdist in the artifact directory')
    modules = {str(p.relative_to(REPO)) for p in (REPO / 'work_assistant').rglob('*.py')}
    results = [audit(p, modules) for p in wheels + sdists]
    check(results[0]['version'] == results[1]['version'], 'wheel/sdist versions differ')
    with tempfile.TemporaryDirectory(prefix='wa-package-smoke-') as temporary:
        scratch = Path(temporary).resolve()
        check(REPO not in scratch.parents and scratch != REPO, 'temporary installation must be outside the checkout')
        wheelhouse = build_wheelhouse or scratch / 'build-wheels'
        if build_wheelhouse is None:
            wheelhouse.mkdir()
            run([sys.executable, '-I', '-m', 'pip', '--isolated', 'download', '--index-url', 'https://pypi.org/simple',
                 '--only-binary=:all:', '--dest', wheelhouse, 'setuptools>=77.0.3', 'wheel'], scratch, 'build-tool-download')
        for result, artifact, kind in zip(results, wheels + sdists, ('wheel', 'sdist')):
            env_dir = scratch / (kind + '-venv')
            # The POSIX CLI defaults to symlinks. EnvBuilder's copy default can
            # break stdlib discovery for a directly launched uv-managed Python.
            run([sys.executable, '-I', '-m', 'venv', '--without-pip', env_dir], scratch, kind + '-venv')
            scripts = env_dir / ('Scripts' if os.name == 'nt' else 'bin')
            python, wa = scripts / ('python.exe' if os.name == 'nt' else 'python'), scripts / ('wa.exe' if os.name == 'nt' else 'wa')
            # A separate step keeps ensurepip's full output in its own log rather
            # than losing it inside venv's nested CalledProcessError diagnostic.
            run([python, '-I', '-m', 'ensurepip', '--upgrade', '--default-pip'], scratch, kind + '-ensurepip')
            cwd = scratch / (kind + '-outside')
            cwd.mkdir()
            run([python, '-I', '-m', 'pip', '--isolated', 'install', '--no-index', '--find-links', wheelhouse,
                 '--no-deps', '--no-cache-dir', artifact], cwd, kind + '-install')
            run([python, '-I', '-m', 'pip', 'check'], cwd, kind + '-pip-check')
            details = json.loads(run([python, '-I', '-c',
                'import importlib.metadata as m,json,pathlib,sys,work_assistant; '
                'print(json.dumps(dict(module=str(pathlib.Path(work_assistant.__file__).resolve()),'
                'prefix=str(pathlib.Path(sys.prefix).resolve()),version=m.version("local-work-assistant"),'
                'runtime_version=work_assistant.__version__,requires=m.requires("local-work-assistant") or [])))'], cwd, kind + '-origin'))
            check(Path(details['module']).is_relative_to(env_dir.resolve()), 'installed import escaped the clean venv')
            check(details['version'] == details['runtime_version'] == result['version'], 'package/runtime version mismatch')
            check(not details['requires'], 'installed runtime unexpectedly has dependencies')
            help_text = run([wa, '--help'], cwd, kind + '-help')
            check('usage:' in help_text and 'demo' in help_text, 'installed wa help is incomplete')
            run([python, '-I', '-m', 'work_assistant', '--help'], cwd, kind + '-module-help')
            demo = json.loads(run([wa, 'demo', '--output', cwd / 'synthetic-demo'], cwd, kind + '-demo'))
            check(demo['hash'] == EXPECTED_DEMO and demo['preview'] == 'dry_run', 'offline demo changed/failed')
            report = Path(demo['report_path'])
            check(REPORT_FILES <= {p.name for p in report.iterdir()}, 'installed demo report files missing')
            for name in ('evidence.json', 'card.json', 'document.json'):
                json.loads((report / name).read_text(encoding='utf-8'))
            shutil.copytree(report, output / (kind + '-report'))
            result.update(python=sys.version.split()[0], demo_hash=demo['hash'], installed_module=details['module'],
                          outside_cwd=str(cwd), install_mode='clean venv; --no-index; --no-deps')
    receipt = {'status': 'passed', 'scope': 'synthetic offline runtime; no model/Feishu calls; build tools may download',
               'artifacts': results}
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, help='Already built directory containing one wheel and one sdist')
    parser.add_argument('--build-wheelhouse', type=Path, help='Existing compatible setuptools/wheel build wheels; skips download')
    parser.add_argument('--output', type=Path, default=REPO / 'out/package-smoke', help='New directory for logs and synthetic reports')
    args = parser.parse_args()
    if os.name == 'nt':
        parser.error('runtime locking/demo smoke is supported on macOS/Linux, not Windows')
    try:
        receipt = smoke(args.artifacts.resolve() if args.artifacts else None, args.output.resolve(),
                        args.build_wheelhouse.resolve() if args.build_wheelhouse else None)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
