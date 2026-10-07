# Retained invalidated experiments

The full original outcome records, compressed decision traces, provenance,
invalidation records and raw failed accounting audit are retained in
[`invalidated-runs-20261006.tar.gz`](invalidated-runs-20261006.tar.gz). The archive
uses deterministic member metadata and a zero-time gzip header. Its
[`SHA-256 checksum`](invalidated-runs-20261006.tar.gz.sha256) identifies the entire
archive. `MANIFEST.json` inside it lists the size and SHA-256 of every evidence
file; all 133 members were read back and independently matched after writing.

## Version 1: production source changed during evaluation

The production risk-evidence module changed while the initial experiment was
running. The run was stopped after 37 completed trials. All those trials remain
in the archive, along with an explicit source-drift invalidation record. No
result was selected or removed because of its financial outcome. The complete
84-trial plan was rerun against the stabilized production source.

## Version 2: independent accounting audit failed

The second run recorded 84 outcome traces. The strict independent auditor found
a research-executor cash error: adding binary floating-point carry cash to a
monthly contribution could produce an amount fractionally below its decimal
value. Flooring that representation to cents then erased one cent. A confirmed
case was €102.27 plus €100 becoming `202.26999999999998`; on 2024-10-01 the low-risk
strategic trial recorded €112.82 remaining cash instead of the exact €112.83.
The discrepancy propagated into later cash, wealth and unitized-return checks.
The full 94,529 reported discrepancies are retained; these are cascading audit
assertions, not 94,529 separate root causes.

The version-2 ledger also contained 83 records rather than 84. Record 061 was
absent, although its saved result and trace existed, and the following record's
predecessor hash matched its reconstructed hash. The cause of the missing append
was not established. The original ledger was preserved with the gap; it was not
backfilled and presented as valid.

The research executor now adds decimal currency amounts before conversion to
model inputs. This applies both to the available decision budget and the
recorded cash-flow update. Dedicated tests reproduce the penny-loss case. Ledger
writes now synchronize to disk and verify the complete chain and expected count
before and after every append, with a final completion check. An independent
auditor separately reconstructs all fills and balances without trusting planner
accounting labels.

## Subsequent evaluation

The economic plan, source price snapshot, strategy parameters and audit
tolerances were unchanged for version 3. Previous figures remain invalidated
regardless of whether the accounting correction changes them by cents or more.
Only a run with a complete source fingerprint, a verified ledger, and a passing
independent audit is suitable for interpreting the retrospective comparisons.
