# Build, verify, and publish an Alpha release

This is an alpha source project. The distribution name is
`local-work-assistant`; imports use `work_assistant`, and the console command is
`wa`. The first release target is `0.1.0a1`. A version heading or workflow file
does not establish that a package was uploaded: verify the PyPI version and its
file hashes before announcing availability. Runtime dependencies remain empty.
Build tools are separate.

Supported execution platforms are macOS and Linux, with Python 3.9+; CI targets
3.9 and 3.12 on each. Native Windows execution is unsupported because of POSIX
process locks. WSL2 is unverified. A `py3-none-any` wheel describes the artifact
format; it does not establish working concurrency or demo support on Windows.

## Build and inspect

Use a clean checkout of the intended commit. Review tracked source and synthetic
fixtures; never copy private runtime/config/report directories into the source
tree. Normal test CI never publishes. The separate `release.yml` workflow is
explicitly dispatched on a release tag; it validates that tag, builds once,
tests and installs those artifacts, then reaches the protected publishing job.

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
The wheel contains runtime modules, metadata/license and the three bundled
first-project resources used by `wa example`. Full documentation, tests, and
maintenance scripts belong in the sdist and repository. The PyPI long description
comes from `docs/pypi.md`; its links and image URL point to the release tag,
so the package page does not depend on relative links or moving `main` content.

Each artifact is installed with `--no-deps --no-index` in its own new venv,
including an isolated PEP 517 rebuild for the sdist. The script removes inherited
provider/credential environment overlays, runs outside the checkout, verifies
the imported module comes from that venv, checks `wa --help` and `wa --version`,
and runs the deterministic offline demo. It also exercises installed
`wa evaluate` and `wa example` without a source checkout, checks the three
practice files, and runs their counter to reproduce `5`. It checks all seven report files including
`report_path/index.html`, parses JSON payloads and records the demo hash. Venvs
and outside-source workspaces are temporary; logs, SHA-256 receipts and copies
of the synthetic report files remain under the selected output directory.

The sdist is also unpacked into a fresh temporary directory and its own
`scripts/evaluate.py` is run there. Its receipt must pass and match stdout;
`sdist-evaluation/receipt.json` and its synthetic artifacts remain in the smoke
output. This checks source-distribution completeness separately from installed
wheel/sdist behavior. The unpacked first-project line counter must also reproduce
its documented count of five, without a model call.

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

1. Compare `pyproject.toml` version, `work_assistant.__version__`, `wa --version`,
   the dated changelog heading and `vVERSION` tag. Review license, minimum Python, project URLs and alpha
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
5. Review the distribution's rendered long description and every outgoing link.
   Twine validates markup, not link availability. `package_smoke` rejects
   relative links in the distribution description.

## One-time Trusted Publisher setup

Use the maintainer's own PyPI account. On
[PyPI account publishing](https://pypi.org/manage/account/publishing/), add a
GitHub pending publisher with these exact values:

| Field | Value |
| --- | --- |
| PyPI project | `local-work-assistant` |
| GitHub owner | `xilinshancun` |
| Repository | `work-assistant` |
| Workflow filename | `release.yml` |
| Environment | `pypi` |

The GitHub `pypi` environment must restrict deployments to release tags (`v*`)
and require a maintainer review. A pending publisher does not reserve the name.
Check availability immediately before the first upload. If someone else owns
the name, stop and choose a reviewed new name; do not upload under a lookalike.

No PyPI password or long-lived API token belongs in repository secrets. The
publish job obtains a short-lived token through OIDC and retains the publishing
action's default attestations. See the official
[pending publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)
and [GitHub Actions publishing guide](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/).

## Explicit release procedure

1. Merge the reviewed version, dated changelog, package description and runtime
   into `main`. Wait for that exact commit's test and installation checks.
2. Create `v0.1.0a1` at the checked commit. Do not move an existing release tag.
   The workflow requires a tag matching the package version and a commit in
   the `main` history; a branch dispatch must fail before publication.
3. Dispatch **release.yml on that tag**, supplying the identical version.
   The build job creates one wheel/sdist pair. The install matrix downloads
   that pair; publishing downloads it again, without rebuilding or checkout.
4. Inspect all test/install outcomes, the recorded SHA-256 hashes, and the exact
   commit before approving the `pypi` environment. Only the publish job has
   `id-token: write`; no model or Feishu credentials are used.
5. After upload, inspect the version's PyPI JSON response. Both expected filenames
   and their SHA-256 values must match the validated artifact manifest. Confirm
   the rendered page and version-tag documentation links.
6. In a fresh environment outside the checkout, install the exact version from
   PyPI and run `wa --version`, `wa demo`, `wa evaluate`, and `wa example`.
   Only then announce availability and make the pinned PyPI command the primary
   README installation path. Attach the validated distributions to the matching
   GitHub prerelease; do not rebuild different files for it.

To run validation without uploading:

```sh
gh workflow run release.yml --ref v0.1.0a1 \
  -f version=0.1.0a1 -f tag=v0.1.0a1 -f publish=false
```

After the account binding and release review, explicitly publish:

```sh
gh workflow run release.yml --ref v0.1.0a1 \
  -f version=0.1.0a1 -f tag=v0.1.0a1 -f publish=true
```

The latter dispatch performs its own build and full validation before it can
upload. Inspect that run's manifest: do not substitute hashes from an earlier
validation-only run. The publish job rejects workflow re-runs; inspect any
uncertain upload before starting a new dispatch.

The Alpha command is `pipx install local-work-assistant==0.1.0a1`; a virtual
environment can instead use `python -m pip install local-work-assistant==0.1.0a1`.
Pinning the version avoids relying on a resolver's default prerelease policy.

## Interrupted or failed publication

A failed upload step is not proof that no file reached PyPI. Inspect the registry
before any further write. PyPI never permits reusing an uploaded filename, even
after deletion. Do not enable `skip-existing` to conceal an unexamined mismatch,
replace release files, or move the tag. If the artifacts are wrong, yank the
affected release and prepare a new version such as `0.1.0a2`; if an upload is
partial, compare each file's hash and use a deliberate recovery plan.

Installation failure is separate from publication failure. Retain the version,
Python/platform, exact command and sanitized output. Back up users' local state
before recommending upgrades; Alpha does not promise automatic state migrations.
