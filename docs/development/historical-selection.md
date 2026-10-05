# Synthetic historical selection evidence

`historical-selection.1.0.0` uses the existing frozen-case execution and immutable
formal-report journey. It accepts original fictional inputs only, with generator
provenance and a seed. Historical evidence never changes recommendation,
execution, account or activation permissions.

## Preregistration and source identity

A `REGISTER` command commits the monthly window, exact source version bundle,
selection policy, strategy, calendar, standard quantity, success counts, terminal
return target, drawdown limit, maturity and state watermarks, confidence level,
block lengths, repetitions, random trial count and availability floor. These
parameters have no engine defaults. An `EVALUATE` command references that saved
registration and the latest evaluation event. A registration cannot follow an
already committed selection in its strategy window.

Every evaluated selection must be an original event in the same owner, account
and visibility scope, committed before the evaluation cutoff and after
registration. The host replays the retained universe and screening evidence at
its original cutoff; version bundles and selection parameters must match the
registration. The same source event cannot move to a different calendar month.
Missing past months remain explicit. Future planned months are retained separately
and do not enter the observed resampling window. Uncommitted framework, validation
and commit failures remain in the availability timeline, including when a later
retry succeeds. A missing or late prerequisite cannot manufacture a performance
label.

## Wealth paths and same-month baselines

Formed cohorts reference their latest compatible standard-outcome report.
Primary outcomes are recomputed from the retained first buyable session, VWAP,
standard costs, corporate actions and individual six-month terminal wealth.
Exact rational arithmetic determines positive and target counts and the
conjunctive batch result. An abstained mature month has a failed batch result
and no drawdown result.

Daily wealth evidence covers every authoritative calendar session from each
entry through its own terminal close. Cohort sleeves begin as cash, retain cash
when entry expires, and hold terminal proceeds after individual maturity. The
common horizon begins at the first entry-window session and ends six calendar
months after its fifth session. The fixed equal sleeve weights determine NAV;
maximum peak-to-trough drawdown is compared before display rounding. Missing
marks or inconsistent terminal wealth remain unresolved evidence.

All eligible universe members use the same entry, cost and outcome rules.
`UNIVERSE` records equal opportunity positive and target rates without a fixed
cohort batch label. `RANDOM` makes the registered number of deterministic
permutations, seeded from registration version and selection event identity.
Every permutation scans with the source industry, capitalization and correlation
constraints. An unformed trial contributes zero and is never redrawn. Pairwise
correlation conflicts are computed once per month; identical formed memberships
reuse their exact wealth calculation.

`FOUR_FACTOR` orders equal-weight cross-sectional value, quality, momentum and
low-volatility percentiles, with deterministic security-ID ties. The host checks
selection-visible publication/acquisition/validation clocks, complete universe
coverage, saved trading dates, momentum endpoints and return windows. Invalid or
missing individual factor values contribute zero; a missing monthly source is
a saved system availability failure, preserving independently valid primary outcomes.
The baseline then applies the same constrained scan and standard wealth rules.

## Inference and saved reports

Overlapping moving calendar blocks are sampled to the observed timeline length.
The registered block lengths each use a reproducible seed and save a common
sampling digest. A resample carries whole monthly records together: primary
outcomes, availability, member identities, market states and paired baselines.
One-sided percentile bounds use the registered confidence level and repetitions;
the worst bound across block lengths controls each gate. A resample with an empty
metric denominator makes that bound unavailable. No independent-stock confidence
claim is made from overlapping or repeated cohorts.

Overall pass and drawdown gates, same-month positive/target increments against
all baselines, batch increments against formed-cohort baselines and state-specific
pass/drawdown gates are conjunctive. Increment gates use a strict positive bound.
Insufficient mature samples produce insufficient or exploratory evidence.
Insufficient state watermarks or unresolved due evidence produce an indeterminate
formal result, rather than a performance failure.

Market states use the selection-visible total-return index window. Watermarks
retain mature and formed months, nonoverlapping outcome windows and separated
state periods. Reports also retain unique security count, repetition concentration,
effective security count, composition turnover, calendar-lag dependence, a
stock/month cluster sensitivity diagnostic and retrospective index stress groups.
Retrospective index returns require separate immutable evidence at the common
terminal date; they never define the selection-visible state or a hard gate.
Diagnostics do not replace the conjunctive gates.

Later reports merge retained monthly evidence. Reusing an evidence identity with
a different payload is rejected. Outcome/path replacements require an
authoritative correction identity, predecessor and reason. Selection-visible
index/factor context is frozen. Prior reports remain separately readable through
the existing authentication and account gates. The generated API and browser show
saved dispositions, counts, bounds, state watermarks, paired baselines and
diagnostics, with no action authorization.

## Verification

Focused checks are `tests/integration/test_historical_selection_evaluation.py`
and `tests/unit/test_historical_inference.py`. The original fixture
`tests/fixtures/synthetic/historical_selection.json` is generated from a fictional
frozen-case journey; it is not observed historical performance. Browser coverage
is `web/src/HistoricalSelectionEvidence.test.tsx` and
`web/e2e/historical-selection.spec.ts`, including narrow screens. `make verify`
and protected CI retain the complete repository checks.
