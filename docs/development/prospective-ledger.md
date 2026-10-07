# Saved synthetic prospective accounting

The `prospective.1.0.0` frozen-case contract records an immutable synthetic
monthly calendar and append-only operations observations through the existing
host and decision-event ledger. It accepts original credential-free D0 inputs
only. It requires SHADOW visibility, and never publishes a user report or grants
source qualification, statistical qualification or execution authorization.

`REGISTER` freezes consecutive original plan months, knowledge clocks on the
registered calendar's last trading date, five-session entry and disclosure
boundaries, six-month trading-calendar maturity floors, source
scopes and the complete implementation bundle before the first cutoff. Renaming
the registration or capability cannot register the same lock again. A population
with unchanged scientific selection, model, calibrator, calendar, quantity and
source scope cannot start a new sequence by changing delivery provenance,
check dates or gate parameters. Older accounting-only registrations without a
population keep their civil month-end clocks and create no maturity evidence. `OBSERVE`
appends one original observation per month. `INCIDENT` records or closes a stable incident identity against its original
plan month without replacing the monthly outcome. `REVIEW` recomputes accounting from
the saved registration and original observations. Every successor names the
latest saved event; a stale predecessor or earlier cutoff is rejected. Repeating
the identical case replays its original event. Observations cannot replace
failures, backfill a late success, change a source cutoff, or extend delivery
and notification windows.

The registered operations policy sets the supervision window, completion floor
and coverage floor; the source requirement and formal evidence floors are also
frozen parameters. Operations use the last consecutive registered number of scheduled months that have reached their
knowledge cutoff, including missing, blocked, unknown and failed months. Data,
pipeline, batch and report rates retain those monthly denominators. A complete
saved abstention is a valid pipeline result, with zero recommendation coverage.
Notification and timeliness rates use their natural obligations; a known empty
obligation set is N/A with an undefined rate. Unknown obligations and current
opportunity-sensitive metrics are withheld with no disclosed counts. Coverage
is withheld until the pipeline rate passes its mechanical floor. Adjacent data
and system failures combine across causes. Consecutive delivery failures are evaluated by original plan month, independently
for each obligation family. Saved failure reports count toward report completion
while their failed pipeline and batch remain failures.
Original failure reasons, window incident counts and retained unclosed incidents
remain separate. Any retained privacy or shadow incident keeps safety false,
even after closure or after its month leaves the rolling window.

`gates_passed` describes the complete synthetic operations conjunction, never
qualification. Incomplete windows cannot pass. Each source scope has an
independent consecutive-month watermark; a missing, late, incomplete or critical
month resets that streak. A synthetic streak reaching the registered floor still grants no source
qualification. Snapshot clocks and integrity digests are synthetic inputs, not
proof of production acquisition.

An optional preregistered population policy freezes exact selection, research,
candidate and standard-outcome implementation bundles, the cohort size, the
selection strategy and constraints, score-model and calibrator versions,
standard quantity, observation milestone and high-probability threshold. Its source bundles
must use the registered host commit and runtime artifact. When that policy is
present, a valid monthly observation must name its original saved selection and,
when the pool formed, its candidate event. An original valid selection abstention
retains a failed batch with no downstream probability population. Their scope, cutoff, research ancestry, calendar, capability,
commit deadline and saved admission population must agree with the original
plan. The monthly observation cannot later replace those links.

Reviews join the original admission identities to the latest compatible saved
standard-outcome report known at the cutoff. A batch becomes mature only after
its preregistered maturity date and complete, evaluable selection and probability
populations. Individual outcomes may legitimately mature earlier or later; both
the batch floor and every individual outcome clock must have passed. Missing members or changed frozen probabilities
leave a due-missing batch. Nonoverlapping six-month windows keep their original
calendar slots, including gaps. High-band counts include all identity-matched,
individually mature original probability members, including those in incomplete
batches, independently of recommendation or personal action.
The registered observation milestone does not run a formal check.

Without a population policy, valid months remain pending and then due missing;
they produce no mature counts. Neither elapsed time nor repeated reviews creates
outcomes.

An optional formal policy preregisters original monthly check nodes, cumulative
maturity increments, total alpha, moving-block lengths, outer and inner replicate
counts, cohort and paired-baseline parameters, directional hard gates and state
watermarks. It must preserve the population's selection, calendar and quantity
locks. `CHECK` visits the next original node. Insufficient watermarks or a missed
node save a skipped look without incrementing the actual ordinal or spending
alpha. A consumed node cannot be retried, and a new node cannot bypass its
predecessor. Every executed look requires the registered increment beyond the
last executed look, even if intermediate nodes were skipped. Look `k` receives
`total_alpha / (k * (k + 1))`; cumulative spending is
`total_alpha * k / (k + 1)`. Ordinary reviews retain that sequence.

Formal inputs reference original monthly selections and their latest compatible
saved standard outcomes. The shared cohort resolver verifies the original
universe and selection replay, selection-visible index and factor context,
standard outcome identity, daily wealth paths and paired exact baseline counts.
Inputs cannot move to another selection or silently replace previously retained
context or outcome evidence. Corrections retain authoritative evidence lineage.
The full calendar retains failed, absent and immature slots.

All gates share whole-month moving-block samples, with original state labels,
probability records and paired baselines kept together. Nested common block
samples estimate the variance of each original and resampled statistic. The
bootstrap-t pivots use those variances; lower and upper gates take their matching
one-sided quantiles and the least favorable bound across registered block
lengths. Undefined statistics, zero variance and unreliable endpoint order
statistics yield explicit indeterminate gates. No resample is silently discarded.
The variance and tail requirements follow the standard
[bootstrap-t interval definitions](https://stat.ethz.ch/R-manual/R-devel/library/boot/html/boot.ci.html).

The conjunction includes pool success, drawdown, all preregistered paired
increments, high-band terminal success and directional overconfidence. Probability
rates weight individual frozen records, including repeated securities and records
in incomplete batches. State gates join the same look once their own original
watermarks are met; an initial overall look does not invent state evidence.
Results remain D0 synthetic conclusions with qualification and execution
authorization false. Production source acquisition, scheduling and actual
qualification decisions remain outside this credential-free contract.

The internal aggregate projection is saved in the immutable decision event.
Existing SHADOW publication and principal-scoped read denials apply to it. No
new HTTP route, browser action, notification provider or order adapter is added.
The generated public API types describe the optional aggregate shape without
making a SHADOW event user-readable.
