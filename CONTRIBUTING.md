# Contributing

Work Assistant is an alpha project. Small, reproducible fixes, documentation
improvements and synthetic examples are welcome. Discuss a substantial feature
in an issue before investing in a large change. No contribution requires access
to a model subscription, a Feishu tenant, or a maintainer's machine.

## Local checks

Use macOS or Linux with Python 3.9+. CI targets Python 3.9 and 3.12 on both
platforms. Native Windows task execution is unsupported because the journal's
process locks use POSIX `flock`; WSL2 has not been validated.

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q work_assistant
python3 -m work_assistant demo --output out/contributing-demo
python3 scripts/evaluate.py --output out/contributing-evaluation
```

These checks are offline and use synthetic fixtures, including loopback HTTP
servers. The runtime uses only the standard library. Optional development builds
need packaging tools installed in a venv:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install build twine
.venv/bin/python -m build
.venv/bin/python -m twine check --strict dist/*
.venv/bin/python scripts/package_smoke.py --artifacts dist --output out/package-smoke
```

The last command installs the wheel and sdist in separate temporary venvs, runs
outside the checkout, and keeps synthetic reports/logs under the chosen output.
It may download build tools; installed application checks do not contact models
or Feishu. Use a new output directory on each run. See
[releasing.md](docs/releasing.md) for offline build-tool preparation and artifact
inspection. Tests and smoke commands never authorize live operations.

## Changes and review

- Keep the runtime dependency-free and Python 3.9 compatible; use the standard
  library and existing journal/adapters before adding new layers.
- Preserve explicit authorization and unknown outcomes. A timeout is not proof
  that an external effect failed. Source observations and model claims must
  remain distinct; do not turn reported assertions into measured facts.
- Add focused synthetic regressions for behavioral changes. Describe the trigger,
  resulting behavior, checks run and remaining limits in the pull request.
- Keep changes scoped. Update applicable documentation and the unreleased
  changelog; do not combine unrelated rewrites with a fix.
- Do not commit local configs, `.local/`, `.wa/`, databases, credentials,
  organization material, personal histories or real report output. Avoid raw
  provider/CLI logs in issues; replace identifiers and data with synthetic values.

Read [AGENTS.md](AGENTS.md) and [PLAN.md](PLAN.md) before changing code. A maintainer
reviews contributions before merge; CI success is evidence of fixture behavior,
not proof of a live integration. Be respectful and focus reviews on reproducible
behavior. AI-assisted contributions are welcome under the same review and
privacy requirements; authors remain responsible for the submitted change.

## Security and support

Follow the repository's [security policy](SECURITY.md) for sensitive reports.
If GitHub offers **Report a vulnerability**, use its private reporting channel.
If no private channel is available, open only a sanitized request for a private
contact route, without exploit details, credentials or user data. Do not attach
private state directories to a public issue. This project does not promise an
incident-response SLA.

Use ordinary issues for reproducible bugs and feature discussions. Installation
reports should include Python/OS version, source/wheel/sdist installation method,
package version and a minimal synthetic reproduction. Never include secrets.

Contributions are made under this repository's [MIT license](LICENSE). No
separate CLA is required; contributors must have the right to submit their work.
