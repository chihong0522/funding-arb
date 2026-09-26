# Binance/Bybit public-market smallcoin paper validation

**Date:** September 26, 2026 UTC
**Baseline:** `c85a48e`
**Mode:** public-data acquisition → local snapshot replay → deterministic paper lifecycle
**Scope:** high-positive-funding non-BTC/ETH/SOL symbols selected from each venue's all-market spot/perpetual intersection

## Result

- Acquisition completed from Binance and Bybit public market-data endpoints with **three distinct snapshots**:
  - `snapshot-000.json`: `2026-09-26T18:28:28Z`
  - `snapshot-001.json`: `2026-09-26T18:28:37Z`
  - `snapshot-002.json`: `2026-09-26T18:28:47Z`
- Each snapshot has `79` raw response files, `errors: []`, and `complete: true` in the manifest.
- Discovery ranked the positive-current-funding symbols in the same-venue spot/perpetual intersection, excluding `BTCUSDT`, `ETHUSDT`, and `SOLUSDT`, and bounded selection to **12 symbols per venue per snapshot**.
- Examples from the captured top of the ranking: Binance `TSTUSDT` (`0.039699%`), `XVGUSDT` (`0.034657%`), `BANKUSDT` (`0.024151%`); Bybit `EGLDUSDT` (`0.33%`), `BILLUSDT` (`0.093534%`), `FIGHTUSDT` (`0.077767%`). These are observed funding inputs, not predicted profit.
- The real carry scanner ran over **6 same-venue scans** (3 snapshots × Binance/Bybit): **63 forward candidates, 0 positive net-horizon estimates, 63 non-positive estimates**. The strategy decision was `no_trade` for all 63. No threshold was relaxed and no positive result was fabricated.
- The normal strategy replay attempted **0 paper lifecycles**, because the strategy gate rejects every candidate with predicted net earnings `<= 0`.
- The separate engine-plumbing replay explicitly enabled `--run-negative-plumbing-lifecycle`: **30/30 simulated open/close lifecycles completed**, all labeled `plumbing_only_negative_estimate`; **0 real orders submitted** and `realized_pnl_claimed: false`.
- The plumbing report contains **120 simulated fills**. Side counts are exactly:
  - spot asks (buy/open): `30`
  - spot bids (sell/close): `30`
  - perpetual bids (short/open): `30`
  - perpetual asks (short/close): `30`

These short snapshots validate public acquisition, candidate provenance, scanner gates, side-correct quantity-dependent VWAP replay, and lifecycle plumbing. They do **not** establish sustained profitability, live execution, or obtainable fills.

## Security and execution boundary

- `scripts/tools/paper_validation_acquire.py` is the only network-capable validation path. It is standard-library-only, HTTPS GET-only, disables environment proxies, validates exact host/path/query-key allowlists before opening requests, validates allowlisted redirects, bounds response size and timeout, never reads credentials, and URL-quotes raw labels so non-ASCII symbols cannot collide on disk.
- The allowlist contains only Binance spot/fapi and Bybit public market-data endpoints for instruments, order books, current funding/tickers, funding history, and Binance funding-interval metadata. Private/account/order/withdrawal/signed endpoints are absent.
- `scripts/tools/paper_validation_replay.py` consumes only captured JSON files. Its transport refuses non-paper execution and binds each fill to the captured instrument and order-book identities.
- No production scanner or execution files were changed. No live orders, API keys, secrets, or exchange credentials were used.

## Isolation status and blocker

The acquisition code is designed to run in a non-root Docker container with a read-only source mount, no credential/SSH/Docker-socket mounts, dropped capabilities, `no-new-privileges`, resource limits, and a bounded writable output mount. The replay is designed to run separately with `--network none`.

In this environment, Docker verification was blocked before a container could start:

```text
docker version
permission denied while trying to connect to the docker API at unix:///var/run/docker.sock
```

The host also rejected an unshare-based network namespace (`Operation not permitted`), so this report does not claim that the collection ran inside Docker or that the replay ran in a kernel-enforced `network none` namespace. The public acquisition itself completed directly against the allowlisted endpoints; replay used only local files and produced the outputs below.

## Durable artifacts

Raw responses, request evidence, normalized snapshots, and both replay modes are preserved outside the repository at:

`/mnt/vol-1/funding-arb-validation-scratch-20260926T182815Z/`

- `manifest.json`: all-market smallcoin discovery configuration, 3 snapshots, 11 allowlisted endpoint forms, and request error list.
- `snapshot-000.json`, `snapshot-001.json`, `snapshot-002.json`: normalized timestamped snapshots.
- `raw/snapshot-000/`, `raw/snapshot-001/`, `raw/snapshot-002/`: original public JSON response bodies.
- `replay-report.json`: strategy-gated replay; no lifecycle was run because all estimates were non-positive.
- `plumbing-replay-report.json`: explicitly labeled negative-estimate engine lifecycle replay.

Artifact SHA-256:

```text
09376ab4d49f76abdf661522e6e19b74bfd8def759894eeba61d8ea46fa56b63  manifest.json
d76d2661aef530985b815217ee31b94514a050324a3d3f8c630b1a96f8fb218b  snapshot-000.json
d0245694b20a691f79d680e9e4cca35a7619eda3880335d3dd5d1ea42b87917d  snapshot-001.json
5bf2b8f06238ab3e40654893f0e1c25b3d930b99ea43e657abed5844bbd944d2  snapshot-002.json
26363009dcba5c2c8b541e9b2f02282ef89fc1e8557c8e1125046f3ae0c2f5bb  replay-report.json
4981ea0ddf70aff251ac2996af6b9f3407c3f52fb78b9a0b27e433df8e4ff0d6  plumbing-replay-report.json
```

## Tests and checks

Run with the temporary isolated Python environment already present at `/mnt/vol-1/hermes/cache/scratch/funding-arb-test-venv`:

```text
12 passed in 0.26s
compileall: passed
git diff --check: passed
replay CLI `--help` without the optional live transport dependency: passed
```

The replay tests cover endpoint rejection, GET-only bounded fetching, collision-safe raw provenance, all-market smallcoin ranking, snapshot-fed execution of the real scanner, stale books, insufficient depth, fee sensitivity, snapshot binding, side-correct quantity-dependent VWAP, strategy no-trade gating, deterministic paper open/close, and honest zero-candidate reporting.

## Files in this change

- `scripts/tools/paper_validation_acquire.py`
- `scripts/tools/paper_validation_replay.py`
- `scripts/tests/test_paper_validation_replay.py`
- `docs/validation/2026-09-26-binance-bybit-paper-validation.md`
