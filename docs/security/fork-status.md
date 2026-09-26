# Fork delivery status

This fork is paper-only. It is not a ready-to-trade release.

## Current scope

- Security hardening and authenticated backend/frontend controls.
- Real orders, margin borrowing, transfers, withdrawals and browser-wallet signing disabled.
- Binance/Bybit same-venue forward spot/perpetual scanner with explicit costs, freshness checks and capital/notional return denominators.
- Paper execution is simulated, not evidence of obtainable fills or profits.

## Incomplete requirements and release gates

- Phase 2 live execution and verified exchange reconciliation/recovery are NOT complete. Recording an incident and blocking further actions is not a recovery loop.
- No live trading authorization has been given. Engineering caps and illustrative 1x prefunded short margin are not approved allocations or leverage policy.
- Jev integration and automated rotation are outside this delivery.
- Complete dependency vulnerability assessment, reproducible Python hash locks and Rust lock coverage remain outstanding; no guarantee of absence of vulnerabilities/backdoors.
- Deployment hardening and real exchange execution tests have not been accepted.

## Verification checkpoint, 2026-09-26

Parent Astra independently executed the final full Python suite in a non-root Docker container with network disabled, read-only scripts/server mounts, no host secrets or Docker socket, and temporary runtime data: **514 passed in 21.52s**.

Parent independently verified **19 frontend/CI contract tests passed**, and `npm run build` (vue-tsc plus Vite production build) passed in a network-disabled non-root container. The build needed an executable temporary filesystem for native bundler modules; the root filesystem remained read-only and no secrets/socket were mounted. Vue I18n deprecation and large bundle warnings remain. Static contract tests are not browser end-to-end tests.

Specification review passed for the named fixes. Targeted quality re-review approved the atomic cross-process durable claim, economics revision identities and separated estimate/simulation provenance. Parent reviewed the claim implementation and independently ran the full regression suite. Acceptance is limited to this paper-only development checkpoint, not a complete security certification or completion of all Phase 1–3 requirements. Directory fsync is best-effort; power-loss durability is not verified.

## Upstream documentation

Remaining upstream README examples of real trading, borrowing, transfers, cross-venue execution and browser wallets describe upstream/historical functionality and are not operational instructions for this fork. The paper-only boundary here takes precedence.
