# Synthetic Fixtures

Every fixture in this directory is generated from explicit, original rules.
Each structured fixture declares `synthetic: true`, a generator version, and a
fixed seed. These fixtures are not copied, transformed, anonymized, sampled,
or derived from securities, accounts, providers, announcements, market paths,
or personal facts.

`qualification_policy.json` is an independently chosen D0 demonstration,
identified by its synthetic marker, generator version and fixed seed. Its
parameters, including the two-node diagnostic-clear demonstration, and the
accompanying scenario clocks are not derived from a personal policy. Tests load
it explicitly; the engine never loads it as a fallback or treats it as
permission for personal actions.
