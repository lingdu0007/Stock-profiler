# Saved historical probability evidence

The `historical-probability.1.0.0` frozen-case contract extends the existing host,
ledger, formal report and scoped report route. It accepts credential-free D0
synthetic inputs. Reports contain historical evidence and always retain
`actionable=false` and `authorization_granted=false`.

A `REGISTER` command commits an immutable version identity, monthly window,
source version bundle, raw-score model, capability, selection strategy, market
calendar, calibration family and statistical policy before that window's
predictions. An `EVALUATE` command names the saved registration and the latest
previous evaluation event. Repeated submission replays the same report. Later
outcomes append a new report linked to the previous saved version; old versions
remain readable within their original access scope.

The host reads all original candidate events in the registered monthly window,
without accepting caller-picked cohorts. Every frozen probability remains in the
population, including low scores, risk vetoes, recommendation abstentions and
securities repeated across months. Standard outcome evaluation identities join
those predictions to saved labels. Due missing labels remain unavailable;
immature labels remain pending. Failed attempts remain in monthly availability
and cannot become effective abstentions after a successful retry. A complete
selection abstention is a valid zero-recommendation month.

The host replays the original candidate calibration command and checks its saved
snapshot and member probabilities. It uses the candidate release module's
monotone Firth Logistic implementation and its ordered, disjoint selection,
fit and recent diagnostic windows. Initial fits expand to at least 60 mature
months, 500 records and 50 records of each outcome class; subsequent fits use
60 mature months. The current month is outside those fitting windows. Frozen
raw scores and probabilities are retained instead of refitting historical
predictions with later outcomes. Changing a model, calendar or capability
requires a distinct registered source identity.

High-band inference uses `p >= 0.80`, a one-sided success lower bound and a
one-sided upper bound on mean prediction minus observed success. Complete
calendar moving blocks of 6, 9 and 12 months carry monthly records and market
state together. All scopes and both metrics share each sampled timeline.
Reports retain the sampling digest, repetition count, individual block bounds,
undefined resamples and worst directional bound. Undefined bounds cannot pass.
Conservative probabilities do not receive a symmetric calibration penalty.

Overall and BULL, BEAR and SIDEWAYS watermarks are separate. Counts of mature
months, high-band records, nonoverlapping six-month result windows and separated
state episodes cannot replace each other. Missing state evidence prevents state
qualification. Market-state evidence must be selection-visible at the original
cutoff and agree with the original command. Coverage divides recommendation
months by valid monthly opportunities; failed months contribute to availability
instead. Preregistration can tighten, but cannot weaken, the fixed gates.

Log Loss, Brier Score, reliability bins, calibration intercept and slope describe
the complete evaluable probability population. The recent diagnostic slice
keeps the latest 24 label-mature calendar months, requires 200 records, and cannot
skip a missing label by reaching back. These diagnostics never fit or update a
production calibrator.

The read-only report page renders saved counts, independent scope watermarks,
directional gates, diagnostics, sampling metadata and the monthly timeline.
The host remains the business decision authority; neither a model draft nor a
report renderer can manufacture historical evidence or activation authority.
