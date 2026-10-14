# Aster sustained-momentum holdout report

**Verdict: FAIL**

The verdict applies to the preregistered primary counterparty-BBO model only.

## Frozen configuration

- Sustain: `{"flow_min": 0.55, "impulse_ms": 1000, "quantile": 0.995, "retain": 1.0, "sustain_ms": 500}`
- Trigger: `{"offset_bps": 5.0, "ttl_ms": 1000}`
- Exit: `{"activation_bps": 20.0, "hard_stop_bps": 30.0, "max_hold_ms": 30000, "trail_bps": 10.0}`

## Five-second layer ablation

| layer | clustered n | net mean bps | 95% day-block CI |
|---|---:|---:|---:|
| impulse_only_5s | 14107 | -9.772 | [-9.862, -9.681] |
| sustain_immediate_5s | 7867 | -9.523 | [-9.615, -9.420] |
| triggered_5s | 567 | -10.116 | [-10.849, -9.288] |

## Frozen exit by entry mechanism

| mechanism | clustered n | gross mean bps | net mean bps | 95% day-block CI |
|---|---:|---:|---:|---:|
| counterparty_bbo | 599 | -1.806 | -9.682 | [-10.500, -8.826] |
| stop_market | 599 | -2.780 | -10.642 | [-11.447, -9.787] |
| queue_bbo | 220 | -0.847 | -5.398 | [-6.594, -4.263] |
| matched_random | 574 | -1.783 | -9.536 | [-9.995, -9.106] |

## Gate

- PASS: `n_at_least_100`
- FAIL: `net_mean_positive`
- FAIL: `ci95_lower_positive`
- FAIL: `both_halves_positive`
- FAIL: `beats_matched_random`

## Diagnostics

- arms: 17013
- triggered: 1130
- counterparty_fills: 709
- queue_fills: 239

See `EXPERIMENT.md` for assumptions and known limitations.
