# Changelog

This file records reviewable source changes. A version heading is not evidence of
a PyPI upload, Git tag or GitHub release. No distribution is published by CI.

## [0.1.0] - Unreleased

Alpha baseline; interfaces, local state formats and provider compatibility may
change. Back up private state before upgrading; do not assume automatic migration
or production support.

### Available in source

- Local project/task records, explicit read/write grants, two-stage provider
  routing and subprocess execution through installed CLIs.
- Durable evidence/claim history, immutable report artifacts and a deterministic
  synthetic offline demo.
- Explicit Feishu document/card publication with durable effect checkpoints,
  unknown-outcome inspection, native timeline sections and opt-in message/intake
  adapters. Availability in source does not enable a service or grant sending
  authority.

### Packaging and community

- Installable `local-work-assistant` wheel and sdist with the `wa` console entry
  point; the installed runtime has no third-party dependencies.
- Complete synthetic daily-bundle source fixtures, contribution/release guidance
  and packaging smoke script in the sdist.
- SPDX MIT metadata, project links, explicit alpha status and macOS/Linux platform
  scope. Native Windows is unsupported; WSL2 is unverified.
- Python 3.9/3.12 macOS/Linux CI, artifact installation outside the source checkout,
  issue forms and a lightweight pull-request template.

### Verification limits

- Offline fixtures do not validate real model subscriptions, Feishu identities,
  tenant permissions, external delivery or visible card rendering.
- Packaging tools may need an index connection during build preparation. The
  application demo requires no credentials or external connection.
