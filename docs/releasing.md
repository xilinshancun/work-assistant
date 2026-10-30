# Reviewing a distribution candidate

This is an alpha source project. The distribution name is
`local-work-assistant`; imports use `work_assistant`, and the console command is
`wa`. The current source version is `0.1.0`, classified as alpha. This document
does not assert that the name/version is available on PyPI or that a release has
been published. Runtime dependencies remain empty. Build tools are separate.

Supported execution platforms are macOS and Linux, with Python 3.9+; CI targets
3.9 and 3.12 on each. Native Windows execution is unsupported because of POSIX
process locks. WSL2 is unverified. A `py3-none-any` wheel describes the artifact
format; it does not establish working concurrency or demo support on Windows.

## Build and inspect

Use a clean checkout of the intended commit. Review tracked source and synthetic
fixtures; never copy private runtime/config/report directories into the source
tree. There is no release/upload/tag automation.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install build twine
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q work_assistant
.venv/bin/python -m work_assistant demo --output out/release-demo
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
.venv/bin/python scripts/package_smoke.py --artifacts dist --output out/package-smoke
```

`python -m build` produces the sdist, then builds the wheel from that sdist. This
exposes source-manifest omissions. Use an empty `dist` directory/new output
directory to avoid mixing versions; do not delete unrelated artifacts. The smoke
script requires exactly one wheel and one sdist. It also supports building itself:

```sh
.venv/bin/python scripts/package_smoke.py --output out/package-smoke-build
```

The smoke script audits archive contents and metadata, rejects common private
directories/configs/databases, and checks the full runtime module set, license,
console entry point, alpha/version/platform metadata and source fixtures. These
checks cannot recognize every possible secret; maintainers must review content.
The wheel contains runtime modules and package metadata/license; documentation,
synthetic examples, tests and maintenance scripts are supplied in the sdist and
repository rather than being application runtime resources.

Each artifact is installed with `--no-deps --no-index` in its own new venv,
including an isolated PEP 517 rebuild for the sdist. The script removes inherited
provider/credential environment overlays, runs outside the checkout, verifies
the imported module comes from that venv, checks `wa --help`, and runs the
deterministic offline demo. It checks all seven report files including
`report_path/index.html`, parses JSON payloads and records the demo hash. Venvs
and outside-source workspaces are temporary; logs, SHA-256 receipts and copies
of the synthetic report files remain under the selected output directory.

The sdist is also unpacked into a fresh temporary directory and its own
`scripts/evaluate.py` is run there. Its receipt must pass and match stdout;
`sdist-evaluation/receipt.json` and its synthetic artifacts remain in the smoke
output. This checks source-distribution completeness separately from installed
wheel/sdist behavior.

Build preparation can download tools from the package index. For installation
checks without tool downloads, prepare compatible wheels using the same Python
interpreter and platform as the intended smoke run:

```sh
.venv/bin/python -m pip download --only-binary=:all: --dest out/build-wheels 'setuptools>=77.0.3' wheel
.venv/bin/python scripts/package_smoke.py --artifacts dist \
  --build-wheelhouse out/build-wheels --output out/offline-install-smoke
```

When building offline as well, pre-provision the frontend/backend into a build
venv and use `python -m build --no-isolation`; backend requirements must already
be present. A missing build tool is a preparation failure, not a runtime package
dependency. Do not change runtime dependencies to mask packaging-tool failures.

## CI and review evidence

CI uses read-only repository permissions, no persisted checkout credentials,
no release credentials and no live provider integration. It tests Python 3.9 and
3.12 on Ubuntu/macOS, builds a single artifact pair, checks it with Twine, then
downloads and installs that pair on the same four OS/Python combinations.
Actions are pinned to reviewed commit SHAs. Update pins deliberately after
checking upstream release notes. Synthetic package-smoke logs/receipts are CI
artifacts with short retention, not official releases. Each test-matrix job also
runs the credential-free evaluator and uploads its `receipt.json` for seven days.

Before approving a candidate:

1. Compare `pyproject.toml` version, `work_assistant.__version__` and the unreleased
   changelog heading. Review license, minimum Python, project URLs and alpha
   support statements. Keep the minimum setuptools version compatible with Python
   3.9; current resolvers select a compatible backend for that interpreter.
2. Inspect wheel/sdist file lists and hashes, the smoke receipt, demo artifacts
   and all matrix checks. Check the sdist includes the complete synthetic
   `examples/daily-bundle`, docs and smoke script. No personal data or `.local`,
   `.wa`, credential files, databases, build venvs or real outputs may ship.
3. Review platform evidence honestly: a local macOS result does not mean Linux CI
   has run. CI configuration is not proof of a successful hosted run.
4. Record the reviewed commit, tool/Python versions, commands, hashes and remaining
   limitations. Archive the exact candidate files locally for review.
5. A later version/tag, GitHub release, registry-name check or PyPI upload is a
   separate maintainer decision and action. This workflow never performs it.
