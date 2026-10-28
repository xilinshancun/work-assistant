# Work Assistant development

This repository is a local-first personal assistant built around existing DeepSeek Harness, Claude Code, and Codex installations. Read PLAN.md before changing code.

- Keep the runtime dependency-free and compatible with Python 3.9+. Prefer standard-library sqlite3, argparse, subprocess, urllib, unittest.
- Never commit credentials, local machine configuration, personal histories, company material, uploaded reference documents, runtime databases, or real experiment outputs. Examples and fixtures must be synthetic.
- The model proposes routes and content. Only explicit user actions or preconfigured project grants authorize side effects. Never turn a model-provided field into permission.
- Preserve unknown outcomes after timeouts or crashes. Do not automatically repeat an operation that may already have taken effect. Resuming an agent is a new action, not passive reconciliation.
- Source observations and model claims are different records. Do not label a model's statement as independently measured or verified.
- Do not add a competing scheduler. Expose commands suitable for existing Paseo schedules and document integration. Never alter other projects or existing schedules during development.
- Workers do not spawn other workers or message existing human sessions. Work only in the assigned checkout, run the required tests, and report concrete results.
- Claude owns architecture and cross-provider review; Codex implements. Read-only reviewers report reproducible failures, not stylistic preferences.
- Verification: `python3 -m unittest discover -s tests -v`, `python3 -m compileall -q work_assistant`, and the documented offline demo. Network/provider tests are opt-in.
