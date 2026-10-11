# Security exit assessment

The `security-exit.1.0.0` frozen decision-case contract has two host commands:
`REGISTER_THESIS` and `ASSESS_SECURITY`. Both use the existing immutable case,
decision event, formal report, authenticated delivery and replay boundaries.
The framework output cannot supply either host result.

Registration binds a thesis to a reconciled, positive position and its recorded
security, lifecycle and origin. It freezes the propositions, their authoritative
sources, numerical falsification predicates, validity period and liquidation
direction. The original registration cannot be replaced within that lifecycle.
For this synthetic contract, registration, snapshot cutoff and host observation
time must agree. A delayed case cannot declare a historical registration time.
Facts published before registration cannot falsify the newly registered thesis.

Assessment consumes security, market and risk evidence with explicit publication,
acquisition, validation and expiry times. Exchange evidence owns security status
and the standard market price; disclosures or exchange evidence own risk facts.
Incomplete, conflicting, late or expired inputs close the statistical result.
The assessment command has no cost basis, quantity, account routing or personal
risk budget. Access metadata remains an authorization boundary and is not a
portfolio input.

The two targets share the assessment cutoff, standard price and market calendar:

| Target | Forward return definition                                                                                       | Probability event                              |
| ------ | --------------------------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| `D20`  | Minimum of daily net total returns over the next 20 market sessions                                             | Loss reaches each of `5%`, `10%`, `15%`, `20%` |
| `V60`  | Incremental net total return at session 60 versus exiting at the assessment time and retaining zero-return cash | Terminal incremental return is positive        |

These definitions describe future outcomes; realized future prices are not inputs
to a current assessment. This contract consumes frozen predictions and does not
fit models or generate outcome labels.

Each prediction binds the security and a canonical SHA-256 digest of the complete
assessment command excluding predictions. That digest includes cutoff, standard
price, calendar, thesis, market state, board, model version and input evidence.
Changing the assessment basis invalidates transplanted predictions.

Each probability retains its point, target, loss boundary, directional calibration
envelope, model version, market state, qualification identity and freshness. The
host requires the applicable state, board, origin, initial holding-age domain,
target and probability grid to match a current committed qualification. The
qualification's passing evidence digest must bind the exact calibration artifact
and calendar. Overall-state qualification cannot substitute for state evidence.
Predictions must have been produced at this assessment cutoff and remain current.
Downside probabilities must decrease monotonically as the loss boundary rises.

The protected lower bound is `max(0, point - lower_error)`; the upper bound is
`min(1, point + upper_error)`. Missing target evidence removes that target's guards
and makes the overall statistical result `NON_ACTIONABLE`, while retaining the
independent evidence for other targets. Data failure removes all statistical
guards. Points remain evidence and cannot substitute for missing guards.

An exchange termination fact or a registered proposition falsified by its named
authority creates a separately saved liquidation gate. Model or qualification
failure cannot remove a gate established by usable authoritative evidence.
Model and news evidence cannot establish these gates. The result supplies no
position action, sale quantity, routing or order instruction.

Run the synthetic contract cases with:

```bash
uv run pytest tests/integration/test_security_exit_assessment.py
```
