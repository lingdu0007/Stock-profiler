# Saved synthetic standard exit evidence

`exit-evaluation.1.0.0` extends the existing frozen host, append-only ledger,
formal report and scoped read route. It accepts credential-free synthetic
inputs and produces historical evidence with `actionable=false` and
`authorization_granted=false`.

An executable candidate standard entry automatically acquires a SYSTEM exit
lifecycle. It shares the retained entry economics and six-month maturity while
keeping its own identity, exposure, paired paths and forecasts. Personal fills,
prices and quantities are not inputs. Hold-to-end recommendation outcomes keep
their existing report identity. Residual exposure at the first maturity remains
attached to that lifecycle; renewal is a separate evaluation and never restores
sold exposure.

A `REGISTER` commits a complete policy, calendar, model, action, cost and baseline
version bundle, monthly window, targets, confidence and seed before the entry
window. `FREEZE` commits predictions for the currently completed market session,
without outcomes. `OBSERVE` supplies complete session paths and can only use
those original committed forecasts. Neither late outcome evidence nor an
authoritative market correction may change, remove or backfill a forecast.
`EVALUATE` reads the complete scoped retained entry population without accepting
a caller-selected cohort. New evidence appends a linked report; original saved
versions and replay remain stable.

The primary path can only move from full initial exposure to half and then zero.
A repeated reduction is idempotent; later HOLD decisions do not buy exposure back.
Signals execute on the first subsequent sellable session at the frozen VWAP and
cost curve, including minimum commission, fees, tax and slippage. Suspensions,
no bids and restrictions retain exposure and its market path. Corporate share
multipliers and cash distributions follow the same sessions for all policies.
Blocked residual value at maturity is marked without inventing a sale.

HOLD_END, HARD_ONLY, FIXED_LOSS and TRAILING_DRAWDOWN use the same entry, calendar,
authoritative hard facts and costs. The transparent policies use entry loss and
running drawdown staircases; the running peak cannot see later prices. A hard
fact requires retained exchange identity, content and clocks available at the
prediction cutoff. A bare hard-exit declaration cannot trigger it.

D20 uses the minimum net counterfactual return in the following twenty market
sessions; V60 uses terminal incremental holding value over exiting into cash in
the following sixty sessions. Both horizons are independent of six-month strategy
wealth. Forecasts retained during abstention, data failure, or after sold exposure
remain in the appropriate evidence population. Due missing paths, labels,
calibration comparisons or continuous forecasts make affected evidence
indeterminate. Immature observations do not become failed performance.

Historical acceptance conjoins paired net improvement over each baseline,
drawdown improvement and safety, upside retention, exit costs, directional
calibration, statistical coverage, action stability and exact replay. Calibration
reports include Log Loss, Brier Score, reliability bins, intercept, slope and
preregistered quantile loss. Independent lifecycle counts, mature entry months,
nonoverlapping windows, direction counts, event counts and separated state
episodes are separate watermarks. Many daily records cannot substitute for many
independent positions.

Every resample carries an entire lifecycle and all its paired baselines. All
metrics and market scopes share the same sampled complete entry-month timeline,
including empty months, with 6, 9 and 12 month moving blocks. Reports retain each
block's bound, undefined draws, sampling digest and worst directional bound.
Undefined required bounds cannot pass. Historical evidence supplies no execution
or activation authority.

Focused checks are `tests/integration/test_standard_exit_outcomes.py`,
`tests/unit/test_exit_history.py` and `web/src/ExitEvaluationEvidence.test.tsx`.
They use independently generated fictional facts. Repository verification remains
`make verify` and the protected CI workflow.
