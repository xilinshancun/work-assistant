#!/usr/bin/env python3
"""Build/audit both distributions and install them in separate clean venvs.

Only build tools may be downloaded. Artifact installation is --no-index; demo
and CLI checks use synthetic data outside the checkout, with no provider calls.
"""
import argparse
import ast
import configparser
from datetime import date
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[1]
DIST_NAME = 'local-work-assistant'
EXAMPLE_FILES = {'README.md', 'count_records.py', 'records.txt'}
PACKAGE_RESOURCES = {'work_assistant/resources/first_project/' + name for name in EXAMPLE_FILES}
EXPECTED_DEMO = '9b64c0da68f8d0e975160f084331662c07f902458d04afcf480296667d65f043'
REPORT_FILES = {'index.html', 'daily-sync.md', 'evidence.html', 'evidence.json',
                'card.json', 'document.json', 'card-preview.html'}
SOURCE_FILES = {'README.md', 'LICENSE', 'SECURITY.md', 'pyproject.toml', 'MANIFEST.in',
                'README.en.md', 'docs/README.md', 'docs/quickstart.md', 'docs/quickstart.en.md',
                'docs/capabilities.md', 'docs/positioning.md', 'docs/troubleshooting.md',
                'docs/guide.md', 'docs/assets/demo-report.png', 'docs/evidence-map.md', 'docs/routing.md', 'llms.txt',
                'examples/first-task.md', 'examples/first-project/README.md',
                'examples/first-project/count_records.py', 'examples/first-project/records.txt',
                'CONTRIBUTING.md', 'CHANGELOG.md', 'docs/releasing.md', 'docs/pypi.md',
                'scripts/package_smoke.py', 'scripts/evaluate.py', 'examples/daily-bundle/state.json',
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


def validate_description(description):
    """PyPI cannot resolve checkout-relative Markdown or HTML destinations."""
    destinations = []

    class Links(HTMLParser):
        def handle_starttag(self, tag, attrs):
            destinations.extend(value for key, value in attrs if key in {'href', 'src'} and value is not None)

    Links().feed(description)
    for pattern in (r'!?\[[^\]\n]*\]\(\s*(?:<([^>]+)>|([^\s)]+))',
                    r'^\s{0,3}\[[^\]\n]+\]:\s*(?:<([^>]+)>|(\S+))'):
        for match in re.finditer(pattern, description, re.MULTILINE):
            destinations.append(next(value for value in match.groups() if value is not None))
    for destination in destinations:
        check(urlsplit(destination).scheme in {'https', 'http', 'mailto'},
              'PyPI long description contains a relative/unsupported link: ' + destination)


def validate_changelog(changelog, version, released=False):
    match = re.search(r'^## \[' + re.escape(version) + r'\] - (Unreleased|\d{4}-\d{2}-\d{2})\s*$',
                      changelog, re.MULTILINE)
    check(match is not None, 'version/changelog heading mismatch')
    check(not released or match.group(1) != 'Unreleased', 'release changelog heading requires a valid release date')
    if match.group(1) != 'Unreleased':
        date.fromisoformat(match.group(1))


def validate_release(version, tag, ref, repo=REPO):
    """Validate the checked-out tag, not a separately supplied source branch."""
    check(re.fullmatch(r'\d+\.\d+\.\d+(?:(?:a|b|rc)\d+)?', version) is not None,
          'release version must be an explicit X.Y.Z or prerelease version')
    check(tag == 'v' + version and ref == 'refs/tags/' + tag,
          'dispatch must run on the exact vVERSION tag (--ref vVERSION)')
    project = re.search(r'^\[project\]\s*$(.*?)(?=^\[|\Z)',
                        (repo / 'pyproject.toml').read_text(encoding='utf-8'), re.MULTILINE | re.DOTALL)
    check(project is not None, 'project metadata missing')
    configured = re.search(r'^version\s*=\s*"([^"\n]+)"\s*$', project.group(1), re.MULTILINE)
    runtime = ast.parse((repo / 'work_assistant/__init__.py').read_text(encoding='utf-8'))
    versions = [ast.literal_eval(node.value) for node in runtime.body if isinstance(node, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == '__version__' for target in node.targets)]
    check(configured is not None and configured.group(1) == version and versions == [version],
          'dispatch version, project version and runtime version must agree')

    def git(*args):
        return subprocess.check_output(['git'] + list(args), cwd=repo, env=clean_environment(),
                                       stderr=subprocess.PIPE, text=True).strip()

    commit = git('rev-parse', 'HEAD')
    check(git('rev-parse', '--verify', 'refs/tags/' + tag + '^{commit}') == commit,
          'checked-out commit differs from release tag')
    # The workflow fetches origin/main explicitly before this check. A missing
    # main ref or a non-ancestor is a hard failure, never a fallback to HEAD.
    git('merge-base', '--is-ancestor', commit, 'refs/remotes/origin/main')
    validate_changelog((repo / 'CHANGELOG.md').read_text(encoding='utf-8'), version, released=True)
    return {'version': version, 'tag': tag, 'commit': commit}


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
    check(PACKAGE_RESOURCES <= files.keys(), 'distribution is missing bundled practice resources')
    if artifact.suffix == '.whl':
        check(all(name in modules | PACKAGE_RESOURCES or '.dist-info/' in name for name in files), 'wheel contains unexpected non-runtime files')
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
    validate_changelog(changelog, metadata['Version'])
    check(metadata['Description-Content-Type'] == 'text/markdown', 'PyPI description must be Markdown')
    validate_description(metadata.get_payload())
    urls = metadata.get_all('Project-URL', [])
    check(any(u.startswith('Repository, https://github.com/xilinshancun/work-assistant') for u in urls), 'repository URL missing')
    return {'artifact': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
            'files': len(files), 'version': metadata['Version']}


def audit_distributions(artifacts):
    wheels, sdists = sorted(artifacts.glob('*.whl')), sorted(artifacts.glob('*.tar.gz'))
    check(len(wheels) == len(sdists) == 1, 'provide exactly one wheel and one sdist in the artifact directory')
    check({p.name for p in artifacts.iterdir()} == {p.name for p in wheels + sdists},
          'artifact directory must contain only the wheel and sdist')
    modules = {str(p.relative_to(REPO)) for p in (REPO / 'work_assistant').rglob('*.py')}
    results = [audit(p, modules) for p in wheels + sdists]
    check(results[0]['version'] == results[1]['version'], 'wheel/sdist versions differ')
    return wheels + sdists, results


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
    candidates, results = audit_distributions(artifacts)
    with tempfile.TemporaryDirectory(prefix='wa-package-smoke-') as temporary:
        scratch = Path(temporary).resolve()
        check(REPO not in scratch.parents and scratch != REPO, 'temporary installation must be outside the checkout')
        wheelhouse = build_wheelhouse or scratch / 'build-wheels'
        if build_wheelhouse is None:
            wheelhouse.mkdir()
            run([sys.executable, '-I', '-m', 'pip', '--isolated', 'download', '--index-url', 'https://pypi.org/simple',
                 '--only-binary=:all:', '--dest', wheelhouse, 'setuptools>=77.0.3', 'wheel'], scratch, 'build-tool-download')
        for result, artifact, kind in zip(results, candidates, ('wheel', 'sdist')):
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
            version_text = run([wa, '--version'], cwd, kind + '-version')
            check(version_text.strip() == result['version'], 'installed wa --version mismatch')
            help_text = run([wa, '--help'], cwd, kind + '-help')
            check('usage:' in help_text and 'demo' in help_text, 'installed wa help is incomplete')
            run([python, '-I', '-m', 'work_assistant', '--help'], cwd, kind + '-module-help')
            evaluation_dir = output / (kind + '-evaluation')
            evaluation = json.loads(run([wa, 'evaluate', '--output', evaluation_dir], cwd, kind + '-installed-evaluation'))
            check(evaluation.get('schema') == 'wa.evaluation.v1' and evaluation.get('status') == 'passed',
                  'installed evaluation failed')
            check(evaluation == json.loads((evaluation_dir / 'receipt.json').read_text(encoding='utf-8')),
                  'installed evaluation receipt differs from stdout')
            example = cwd / 'practice-project'
            run([wa, 'example', '--output', example], cwd, kind + '-installed-example')
            check({p.name for p in example.iterdir()} == EXAMPLE_FILES, 'installed example files differ')
            for name in EXAMPLE_FILES:
                check((example / name).read_bytes() == archive_files(artifact)['work_assistant/resources/first_project/' + name],
                      'installed example differs from bundled resource: ' + name)
            count = run([python, '-I', example / 'count_records.py', example / 'records.txt'], cwd, kind + '-installed-count')
            check(count.strip() == '5', 'installed example no longer reproduces its documented mismatch')
            result['installed_evaluation'] = {'status': evaluation['status'], 'summary': evaluation['summary']}
            result['installed_example_count'] = 5
            demo = json.loads(run([wa, 'demo', '--output', cwd / 'synthetic-demo'], cwd, kind + '-demo'))
            check(demo['hash'] == EXPECTED_DEMO and demo['preview'] == 'dry_run', 'offline demo changed/failed')
            report = Path(demo['report_path'])
            check(REPORT_FILES <= {p.name for p in report.iterdir()}, 'installed demo report files missing')
            for name in ('evidence.json', 'card.json', 'document.json'):
                json.loads((report / name).read_text(encoding='utf-8'))
            shutil.copytree(report, output / (kind + '-report'))
            result.update(python=sys.version.split()[0], demo_hash=demo['hash'], installed_module=details['module'],
                          outside_cwd=str(cwd), install_mode='clean venv; --no-index; --no-deps')
            if kind == 'sdist':
                # audit() already rejected unsafe paths, links and special files.
                # Evaluate the unpacked source too: a checkout-only test cannot
                # establish that the source distribution contains the full tool.
                source = scratch / 'unpacked-source'
                source.mkdir()
                for name, raw in archive_files(artifact).items():
                    target = source / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(raw)
                evaluation = json.loads(run([python, '-I', source / 'scripts/evaluate.py',
                    '--output', output / 'sdist-evaluation'], cwd, 'sdist-evaluation'))
                check(evaluation.get('schema') == 'wa.evaluation.v1' and evaluation.get('status') == 'passed',
                      'unpacked sdist evaluation failed')
                saved = json.loads((output / 'sdist-evaluation/receipt.json').read_text(encoding='utf-8'))
                check(saved == evaluation, 'saved source evaluation differs from stdout')
                result['source_evaluation'] = {'status': evaluation['status'], 'summary': evaluation['summary']}
                first_project = source / 'examples/first-project'
                count = run([python, '-I', first_project / 'count_records.py', first_project / 'records.txt'],
                            cwd, 'sdist-first-project')
                check(count.strip() == '5', 'unpacked practice fixture no longer reproduces its documented mismatch')
                result['first_project_count'] = 5
    receipt = {'status': 'passed', 'scope': 'synthetic offline runtime; no model/Feishu calls; build tools may download',
               'artifacts': results}
    (output / 'receipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--artifacts', type=Path, help='Already built directory containing one wheel and one sdist')
    parser.add_argument('--build-wheelhouse', type=Path, help='Existing compatible setuptools/wheel build wheels; skips download')
    parser.add_argument('--output', type=Path, default=REPO / 'out/package-smoke', help='New directory for logs and synthetic reports')
    parser.add_argument('--audit-only', action='store_true', help='Audit existing distributions without installing/building')
    parser.add_argument('--release-version', help='Also validate this checkout as a release tag')
    parser.add_argument('--release-tag', help='Exact tag, vVERSION')
    parser.add_argument('--release-ref', help='Dispatch ref, refs/tags/vVERSION')
    parser.add_argument('--manifest', type=Path, help='Write a NEW audited release bundle manifest (audit-only)')
    args = parser.parse_args()
    if os.name == 'nt':
        parser.error('runtime locking/demo smoke is supported on macOS/Linux, not Windows')
    try:
        identity = None
        if any((args.release_version, args.release_tag, args.release_ref)):
            check(all((args.release_version, args.release_tag, args.release_ref)), 'provide version, tag and ref together')
            identity = validate_release(args.release_version, args.release_tag, args.release_ref)
        if args.audit_only:
            check(args.artifacts is not None, '--audit-only requires --artifacts')
            _, results = audit_distributions(args.artifacts.resolve())
            receipt = {'schema': 'wa.release.bundle.v1', 'artifacts': results}
            if identity:
                check(all(r['version'] == identity['version'] for r in results), 'release artifact version mismatch')
                receipt.update(identity)
            if args.manifest:
                check(identity is not None, '--manifest requires a validated release identity')
                with args.manifest.open('x', encoding='utf-8') as stream:
                    json.dump(receipt, stream, indent=2); stream.write('\n')
        else:
            check(args.manifest is None, '--manifest requires --audit-only')
            receipt = smoke(args.artifacts.resolve() if args.artifacts else None, args.output.resolve(),
                            args.build_wheelhouse.resolve() if args.build_wheelhouse else None)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
