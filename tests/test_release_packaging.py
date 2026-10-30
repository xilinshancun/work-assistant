"""Synthetic distribution/release checks; never upload or contact a service."""
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('release_smoke', ROOT / 'scripts/package_smoke.py')
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class ReleaseMetadataTests(unittest.TestCase):
    def test_description_absolute_links(self):
        smoke.validate_description('[Guide](https://example.invalid/guide) '
                                   '![Image](<https://example.invalid/image.png>) '
                                   '<img src="https://example.invalid/image.png"> '
                                   '[contact](mailto:maintainer@example.invalid)')

    def test_relative_markdown_and_html_rejected(self):
        for text in ('[Guide](docs/guide.md)', '![Image](./image.png)',
                     '[Guide](<../guide.md>)', '[ref]: docs/guide.md',
                     '<img src="docs/image.png">', '<a href="/guide">Guide</a>'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'relative/unsupported'):
                smoke.validate_description(text)

    def test_changelog_unreleased_and_release_date(self):
        for suffix in ('Unreleased', '2026-10-30'):
            smoke.validate_changelog('## [0.1.0a1] - ' + suffix + '\n', '0.1.0a1')
        for text in ('## [0.1.0] - 2026-10-30', '## [0.1.0a1] - 2026-02-30'):
            with self.assertRaises(ValueError):
                smoke.validate_changelog(text, '0.1.0a1')
        with self.assertRaisesRegex(ValueError, 'valid release date'):
            smoke.validate_changelog('## [0.1.0a1] - Unreleased\n', '0.1.0a1', released=True)

    def test_wheel_resources_and_description_are_audited(self):
        with tempfile.TemporaryDirectory() as temporary:
            repo = Path(temporary)
            (repo / 'CHANGELOG.md').write_text('## [0.1.0a1] - 2026-10-30\n', encoding='utf-8')
            wheel = repo / 'synthetic.whl'
            dist = 'local_work_assistant-0.1.0a1.dist-info/'
            files = {name: b'synthetic resource\n' for name in smoke.PACKAGE_RESOURCES}
            files.update({'work_assistant/__init__.py': b'__version__="0.1.0a1"\n',
                          dist + 'licenses/LICENSE': b'MIT',
                          dist + 'entry_points.txt': b'[console_scripts]\nwa = work_assistant.cli:main\n',
                          dist + 'METADATA': b'Metadata-Version: 2.4\nName: local-work-assistant\nVersion: 0.1.0a1\n'
                          b'Requires-Python: >=3.9\nLicense-Expression: MIT\n'
                          b'Classifier: Development Status :: 3 - Alpha\n'
                          b'Classifier: Operating System :: MacOS\nClassifier: Operating System :: POSIX :: Linux\n'
                          b'Project-URL: Repository, https://github.com/xilinshancun/work-assistant\n'
                          b'Description-Content-Type: text/markdown\n\n[Guide](https://example.invalid/guide)\n'})

            def audit():
                with zipfile.ZipFile(wheel, 'w') as archive:
                    for name, raw in files.items():
                        archive.writestr(name, raw)
                with patch.object(smoke, 'REPO', repo):
                    return smoke.audit(wheel, {'work_assistant/__init__.py'})

            self.assertEqual(audit()['version'], '0.1.0a1')
            removed = files.pop('work_assistant/resources/first_project/records.txt')
            with self.assertRaisesRegex(ValueError, 'practice resources'):
                audit()
            files['work_assistant/resources/first_project/records.txt'] = removed
            files['examples/should-not-be-in-wheel.txt'] = b'synthetic'
            with self.assertRaisesRegex(ValueError, 'unexpected non-runtime'):
                audit()
            del files['examples/should-not-be-in-wheel.txt']
            files[dist + 'METADATA'] = files[dist + 'METADATA'].replace(b'https://example.invalid/guide', b'docs/guide.md')
            with self.assertRaisesRegex(ValueError, 'relative/unsupported'):
                audit()


class ReleaseIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        (self.repo / 'work_assistant').mkdir()
        (self.repo / 'work_assistant/__init__.py').write_text('__version__ = "0.1.0a1"\n')
        (self.repo / 'pyproject.toml').write_text('[project]\nversion = "0.1.0a1"\n[other]\nx = 1\n')
        (self.repo / 'CHANGELOG.md').write_text('## [0.1.0a1] - 2026-10-30\n')
        self.git('init', '-b', 'main')
        self.git('add', '.')
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                 'commit', '-m', 'Synthetic release fixture')
        self.git('tag', 'v0.1.0a1')
        self.git('update-ref', 'refs/remotes/origin/main', 'HEAD')

    def git(self, *args):
        return subprocess.check_output(['git'] + list(args), cwd=self.repo,
                                       env=smoke.clean_environment(), stderr=subprocess.PIPE, text=True).strip()

    def validate(self, **kwargs):
        values = dict(version='0.1.0a1', tag='v0.1.0a1', ref='refs/tags/v0.1.0a1', repo=self.repo)
        values.update(kwargs)
        return smoke.validate_release(**values)

    def test_exact_tag_version_main_history(self):
        self.assertEqual(self.validate()['commit'], self.git('rev-parse', 'HEAD'))
        self.git('checkout', '--detach', 'v0.1.0a1')
        self.assertEqual(self.validate()['version'], '0.1.0a1')

    def test_branch_dispatch_and_mismatched_input_rejected(self):
        for values in ({'ref': 'refs/heads/main'}, {'tag': 'v0.1.0'}, {'version': '0.1.0;echo bad'}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.validate(**values)

    def test_runtime_and_project_version_must_match(self):
        for name in ('work_assistant/__init__.py', 'pyproject.toml'):
            path = self.repo / name
            original = path.read_text()
            path.write_text(original.replace('0.1.0a1', '0.1.0'))
            with self.assertRaisesRegex(ValueError, 'must agree'):
                self.validate()
            path.write_text(original)

    def test_release_rejects_unreleased_changelog(self):
        (self.repo / 'CHANGELOG.md').write_text('## [0.1.0a1] - Unreleased\n')
        with self.assertRaisesRegex(ValueError, 'valid release date'):
            self.validate()

    def test_tag_must_be_checkout_and_in_main_history(self):
        (self.repo / 'synthetic.txt').write_text('newer branch commit')
        self.git('add', '.')
        self.git('-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                 'commit', '-m', 'Synthetic non-main commit')
        with self.assertRaisesRegex(ValueError, 'differs from release tag'):
            self.validate()
        self.git('tag', '-f', 'v0.1.0a1')
        with self.assertRaises(subprocess.CalledProcessError):
            self.validate()


class ReleaseWorkflowTests(unittest.TestCase):
    def test_permissions_dispatch_and_publish_are_separated(self):
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn('workflow_dispatch:', text)
        self.assertNotIn('\n  push:', text)
        self.assertEqual(text.count('id-token: write'), 1)
        publish = text.split('\n  publish:\n', 1)[1]
        self.assertIn('needs: [build, validate]', publish)
        self.assertIn('if: ${{ inputs.publish }}', publish)
        self.assertIn('name: pypi', publish)
        self.assertNotIn('actions/checkout@', publish)
        self.assertNotIn('secrets.', text)
        self.assertIn("skip-existing: 'false'", publish)
        self.assertIn('7245906fb61fa33a13554f9d5fb856fe61758086', publish)
        self.assertIn('test "$GITHUB_RUN_ATTEMPT" = 1', publish)
        self.assertIn("python: ['3.9', '3.12']", text)
        self.assertIn('os: [ubuntu-latest, macos-14]', text)

    def test_both_download_verifiers_reject_tampering(self):
        text = (ROOT / '.github/workflows/release.yml').read_text(encoding='utf-8')
        snippets = [textwrap.dedent(part.split("\n          PY", 1)[0])
                    for part in text.split("<<'PY'\n")[1:] if "root = Path('candidate')" in part.split('\n          PY', 1)[0]]
        self.assertEqual(len(snippets), 2)
        with tempfile.TemporaryDirectory() as temporary:
            cwd = Path(temporary)
            dist = cwd / 'candidate/dist'
            dist.mkdir(parents=True)
            entries = []
            for name in ('synthetic.whl', 'synthetic.tar.gz'):
                raw = b'synthetic artifact'
                (dist / name).write_bytes(raw)
                entries.append({'artifact': name, 'version': '0.1.0a1', 'sha256': hashlib.sha256(raw).hexdigest()})
            manifest = {'schema': 'wa.release.bundle.v1', 'version': '0.1.0a1', 'tag': 'v0.1.0a1',
                        'commit': 'synthetic-commit', 'artifacts': entries}
            path = cwd / 'candidate/release-manifest.json'
            path.write_text(json.dumps(manifest))
            environment = dict(smoke.clean_environment(), RELEASE_VERSION='0.1.0a1', RELEASE_TAG='v0.1.0a1',
                               RELEASE_COMMIT='synthetic-commit', GITHUB_REF='refs/tags/v0.1.0a1')
            for code in snippets:
                with self.subTest(code=code[:50]):
                    def execute():
                        return subprocess.run([sys.executable, '-I', '-c', code], cwd=cwd, env=environment,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode
                    self.assertEqual(execute(), 0)
                    (dist / 'synthetic.whl').write_bytes(b'tampered')
                    self.assertNotEqual(execute(), 0)
                    (dist / 'synthetic.whl').write_bytes(b'synthetic artifact')
                    (dist / 'extra.whl').write_bytes(b'extra')
                    self.assertNotEqual(execute(), 0)
                    (dist / 'extra.whl').unlink()
                    environment['RELEASE_COMMIT'] = 'wrong-commit'
                    self.assertNotEqual(execute(), 0)
                    environment['RELEASE_COMMIT'] = 'synthetic-commit'


if __name__ == '__main__':
    unittest.main()
