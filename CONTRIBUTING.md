# Contributing

Public issues are limited to reproducible software defects using original
synthetic inputs and documentation corrections. Do not include credentials,
runtime output, market information, provider responses, account facts, or
personal information.

Before opening a pull request, run:

```bash
make verify
```

Contributions must preserve the official safety invariants: the application is
single-user decision support, is not a public recommendation service, and has
no order-writing capability. Proposals for product scope, investment strategy,
broker integration, or authentication flows are not accepted through public
issues.
