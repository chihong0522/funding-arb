# Astra parent verification — paper-only dependency checkpoint

Parent independently reran the frozen integration tree after final workflow fixes:

- Full Python suite: **534 passed in 21.81s** in network-disabled, nonroot, read-only Docker (`funding-arb-audit-tests:local`). This image run is a source regression check, not an independent fresh-lock installation; the worker's separate hash-lock installation/audit evidence is documented in the dependency report.
- Frontend: **21/21 tests passed**, including ECharts SSR options and tooltip escaping; TypeScript and production build passed in network-disabled, nonroot Docker. Existing large-chunk warnings remain.
- Parent reviewed the workflow diff: DEX opt-in fails before checkout/install; default uses the paper hash lock and explicit Binance/Bybit venues. Setup and startup use the paper lock.
- No live trading enabled. No browser visual/E2E or native PowerShell execution was performed. Remote CI remains unverified.

Public-market validation remains short snapshot replay, not sustained forward paper performance: the latest worker run reported 63 candidate observations, no positive net estimates, and no strategy trades. Negative-economics plumbing simulations are not strategy-selected trades or realized profit. Parent independently passed all 12 replay regression tests, but did not independently repeat the full captured-market replay in this checkpoint.

Optional full DEX dependencies retain documented advisory/provenance findings and are excluded from the approved default runtime. Rust/Tauri remains unaudited. This checkpoint does not deliver Jev decision integration, autonomous rotation, live reconciliation/recovery, or permission to trade.
