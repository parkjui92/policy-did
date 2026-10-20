# policy-did — ask "may we estimate this?" before estimating a policy effect

[한국어](README.md)

## Why

Program evaluations and policy reports routinely claim "the program raised outcome Y by n%". The number usually comes from difference-in-differences (DiD) or synthetic control, and in practice the trouble is not the computation but what happens before it: two-way fixed effects run on staggered adoption, "effects" reported with no comparison group, hypotheses rewritten after seeing the numbers, outcome variables that turn out not to exist in the data. Estimation code is everywhere; a tool that first asks whether this design may produce that number, and stops when it may not, was missing (GitHub, 2026-10: policy-evaluation DiD repositories are all coursework-level).

policy-did wires that order into code. **A design file comes first, estimation runs only on designs that pass an identification gate, and the report prints "what we cannot say" before anything else.** It is the pre-registration and control-group review of a clinical trial, applied to policy data.

## How

No installation: copy `policy_did.py` (standard library only). Estimators are optional R/Python packages called at the estimation step; if missing, the tool prints the install command and stops.

```bash
python3 policy_did.py init --out design.yaml                 # 1. design template — fill it before looking at the data
python3 policy_did.py check design.yaml --data panel.csv --out out      # 2. gate: PASS / WARN / FAIL
python3 policy_did.py estimate design.yaml --data panel.csv --out out   # 3. estimate — only if the gate did not FAIL
python3 policy_did.py report design.yaml --data panel.csv --out out     # 4. report + event-study plot
```

`run` does steps 2–4 at once; `simulate` creates synthetic data with a known effect for practice.

### Input 1: data (`panel.csv`)

Long format, unit × period. Column names are declared in the design file.

```
unit,year,exec_100m_krw
Gyeonggi,2011,19023
Gyeonggi,2012,20111
…
```

### Input 2: design (`design.yaml`)

Five things: which units were treated when, what is measured, who is the comparison, which direction is expected, and why the comparison is credible.

```yaml
data:
  unit: unit
  time: year
  outcome: exec_100m_krw
  outcome_source: "KOSIS DT_127003_009, retrieved 2026-10-20"
  log_outcome: true

treatment:
  name: "INNOTOWN designation"
  timing:               # first treatment year per unit; units not listed are untreated
    Gyeonggi: 2019
    Seoul: 2020
    Incheon: 2022
  comparison: not_yet_treated

hypotheses:             # pre-registered; a missing direction is a design error
  - id: H1
    direction: positive
    horizon: [0, 3]
    statement: "Government R&D execution rises after designation"

assumptions:
  sutva:
    spillover_risk: medium
    justification: "why resources do not move between units"   # empty → FAIL
  no_other_policy: "concurrent policies and how they are handled"

design:
  method: cs            # cs (Callaway–Sant'Anna) | sa | twfe | synthdid | rdd
```

### Output 1: the gate (`check`)

```
# Identification gate — WARN  (cs, 16 units · 222 rows)
| G3  | PASS | treatment structure | 3 cohorts (2019, 2020, 2022), 6 never-treated units, staggered |
| G4  | WARN | number of clusters  | 16 (threshold 20) |
| G5  | PASS | pre-trend slope test | pooled z=-1.28, p=0.200 |
| G2g | WARN | series-break footnote | 2024: R&D budget reclassification (~1.8 trillion KRW) |
```

On FAIL, `estimate` exits with code 3 and writes nothing. Design and data are hashed; change either and the gate must be re-run.

### Output 2: the report (`report.md`)

After the estimate table and an event-study SVG, three sections follow: a verdict per pre-registered hypothesis, "what we can say", and "what we cannot say" — every gate WARN copied verbatim with its ID. With few clusters the report refuses to use significance language.

## Terms, one line each

- **DiD**: the change in treated units minus the change in untreated units; rests on the parallel-trends assumption.
- **Staggered**: units adopt at different times. Classic two-way fixed effects is biased here (already-treated units act as controls), so the tool FAILs that combination.
- **ATT**: average treatment effect on the treated; reported overall and by event time.
- **Gate**: the identification checklist run before estimation (PASS / WARN / FAIL).
- **synthdid / RDD**: synthetic difference-in-differences (single adoption date) and regression discontinuity (score cutoff).

## Checks

G1 pre-registration · G2 data (outcome exists, missingness, timing table vs data, series-break footnotes) · G3 treatment structure (comparison group, staggered + TWFE, method fit) · G4 sample (clusters, cohort size, pre/post periods) · G5 pre-trends (per-cohort slope comparison over the same window, pooled) · G6 SUTVA justification and concurrent policies · G8 estimator availability · G9 RDD (cutoff range, two-sided support, density). A passing pre-trend test is not proof of parallel trends (Roth 2022); the report keeps that sentence even on PASS.

## Estimators (called, not re-implemented)

| method | call | condition |
|---|---|---|
| `cs` | R `did::att_gt`, else Python `differences` | staggered OK |
| `sa` | R `fixest::sunab` | staggered OK |
| `twfe` | Python `statsmodels` | single adoption date only |
| `synthdid` | R `synthdid` | single adoption date, untreated donors |
| `rdd` | R `rdrobust` | running variable + cutoff |

`python3 policy_did.py engines` lists what is installed.

## Examples

- `examples/synthetic/` — known effect (2.0 + 0.5/yr); R `did` and Python `differences` agree and recover it.
- `examples/innotown_region/` — Korea's INNOTOWN (small R&D-zone) designations 2019/2020/2022 × provincial government R&D execution (KOSIS). Gate WARN; effect has the registered sign but the interval includes zero — as the design file predicted.
- `examples/kosis_budget_cut_2024/` — the 2024 R&D budget cut × ministry execution. Every ministry treated in the same year, no comparison group, concurrent reclassification → **gate FAIL**, no estimate.

## Not in scope · limits

Continuous treatment, triple differences, HonestDiD sensitivity; covariates are declared but not yet passed to estimators; the pre-trend test is a simple slope comparison (use R `pretrends`/`HonestDiD` for formal versions); RDD density check is a stand-in for `rddensity`; the built-in YAML reader covers the template subset (PyYAML is used when present).

## Honesty rules (printed in the output)

1. No estimate when assumptions fail. 2. Only registered hypotheses, outcomes and horizons. 3. Gate WARNs are copied, not summarized. 4. Pre-trend PASS ≠ parallel trends. 5. Few clusters → no significance language. 6. No interpretation below the unit level. 7. Design and data hashes travel with every output. 8. No estimator is invented; missing packages stop the run.

## Verification (2026-10-20)

Synthetic staggered panel: `r:did` = `py:differences`, e=0 2.05 (truth 2.0) … e=4 4.07 (truth 4.0). `sa` 4.30 vs `cs` 4.29. Single cohort: `twfe` 3.52, `synthdid` 3.54 (truth 3.5). RDD `rdrobust` 3.25 (SE 1.06, truth 5 inside the robust CI). Five refusal paths confirmed. One real-data run. 18 tests (`selftest`). Design notes: [docs/design.md](docs/design.md).

## License

MIT. Estimation packages keep their own licenses.
