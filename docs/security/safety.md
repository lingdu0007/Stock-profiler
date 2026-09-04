# Safety Invariants

The official application is single-user decision support. It is not a public
recommendation service and does not contain a path that creates, pre-fills,
submits, changes, or cancels an order.

Only original synthetic fixtures are permitted in this repository. Never
commit credentials, runtime state, databases, logs, backups, account facts,
market data, provider responses, or transformed copies of those materials.

`make repo-guard` checks repository role boundaries, forbidden data paths and
types, synthetic metadata, and non-JSON synthetic-fixture sidecars.
`make secrets-scan` requires the fixed Gitleaks 8.18.4 scanner. Public
automation also scans pull requests, main, release preparation, and scheduled
full history.
