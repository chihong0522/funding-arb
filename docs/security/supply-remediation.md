# Supply-chain and frontend security remediation

**Updated:** 2026-09-26
**Audit baseline:** `789f2684701df1d0a268dbbe7a052f50cb5906a2`
**Status:** CI/frontend mitigations applied; this is not a security certification.

This change addresses the CI injection/credential-boundary findings and browser-side DEX/Test Order risks from the read-only audit. It does not replace server-side authorization or resolve the Python/Rust lockfile gaps.

## Applied

### GitHub Actions and publishing

- `telegram-push.yml` declares typed numeric inputs, passes them through environment variables, validates their format/range in Python, and quotes their shell use. No dispatch expression is interpolated into a `run` script.
- Split the pipeline into read-only `scan`, secret-scoped `notify`, and write-scoped `publish` jobs. The scan job has no Telegram credentials or write token; the notification job has no checkout, dependency install, or project-script execution; only the publisher has `contents: write`.
- The notifier consumes a short-lived artifact, validates the message bounds and fixed dashboard-button URLs, and uses only Python's standard library. The publisher checks the snapshot is non-empty, at most 1 MiB, and valid JSON before copying it; it runs no project code or dependency installation.
- Every checkout uses `persist-credentials: false`. Git authentication is supplied through a runner-temporary askpass helper only in the read-only fetch and publisher steps; no credential helper is configured for persistence.
- CI permissions default to `contents: read`. GitHub Actions references are pinned to full commit SHAs; the listed refs were checked read-only against the corresponding official action tag refs on 2026-09-26.
- Added weekly Dependabot updates for GitHub Actions, npm, and pip. npm minor/patch updates are grouped; major updates remain individually reviewable.
- The CI workflow is configured to run the frontend static security contracts and a lockfile-based frontend install/build on a disposable hosted runner with no secrets.

### Frontend trading and wallet surface

- The active CEX connection UI is limited to Binance and Bybit. Scanner live opens fail closed unless every venue is on that allowlist and reports `trade: true`; unsupported venue pairs cannot use the live path. Existing positions can only be live-closed through the same allowlist.
- DEX navigation is removed and `/dex` redirects to `/cex`. DEX connection and browser-wallet trade composables are disabled; the Test Order component is informational/disabled and cannot place an order.
- Removed the browser-wallet signing implementations that stored an agent private key in `sessionStorage`. Static search of `web/src` found no remaining `sessionStorage` use.
- These frontend checks are defense in depth only. They do not secure direct calls to backend APIs.

### Content Security Policy

- Vercel sends a CSP with `default-src 'self'`, `script-src 'self'`, `object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`, and a restricted `connect-src` for the public snapshot host.
- Tauri now has a restrictive CSP rather than `null`; only the app/IPC origins, public snapshot host, and explicit loopback API/dev-server ports are allowed. The HTML meta policy provides the corresponding local-browser fallback.
- `style-src 'unsafe-inline'` remains for the UI framework's inline styles. No `unsafe-inline` or `unsafe-eval` is allowed for scripts.

### npm lock usage

- Vercel and the start scripts use `npm ci` instead of `npm install` when installing dependencies. CI builds from the committed lockfile.
- Corrected the lockfile root metadata so `esbuild` is recorded as a devDependency, matching `web/package.json`.
- The audit found 173 non-root package entries with registry URLs and integrity hashes. This is a lock-integrity observation, not a current vulnerability scan.

## Residual risks and bounded follow-up

1. **Backend live-order authorization remains a separate gate.** The UI does not prevent direct requests to `/api/positions/open`; the backend owner must independently verify authentication, fixed Binance/Bybit and strategy allowlists, process-level live enablement, amount/exposure limits, and network binding before any live credentials are used.
2. **Python dependencies are still not reproducibly locked.** Root/server requirements and workflow/startup `pip install` commands use version ranges or unpinned packages. Do not invent pins: create reviewed runtime/CI lockfiles with hashes in an isolated, no-secrets resolver environment, then switch installs to `--require-hashes`. The scan job currently installs `requests` without a lock, but has no Telegram secrets or write permission.
3. **npm ranges and install scripts remain.** `npm ci` uses the lockfile, but install scripts still run. The audit identified `esbuild`, `fsevents`, and `vue-demi` entries with install scripts. Review those scripts in an isolated environment before considering `--ignore-scripts` or a narrow allowlist. `ethers` and `@nktkas/hyperliquid` remain direct frontend dependencies even though the browser signing path is disabled; remove them only after a full reference check and lock regeneration in an isolated environment.
4. **Tauri reproducibility is not complete.** No `Cargo.lock` was created or resolved here, and the Tauri build was not run. Generate and review a lockfile in an isolated desktop-build environment; separately verify the bundled Python path and Tauri capabilities configuration.
5. **Plaintext credential fallback is outside this file scope.** The audit's `~/.funding-arb/credentials.json` fallback finding was not changed by this frontend/CI remediation. Treat it as unresolved until its owner removes or tightly gates it.
6. **Repository governance is not configured here.** Confirm branch protection and required maintainer review for workflow changes in GitHub settings; no CODEOWNERS rule or protected publishing environment was added.
7. No `npm audit`, `pip-audit`, SBOM, install-script behavior review, or current CVE conclusion was produced. Dependency installation and repository execution were explicitly prohibited for this work.

## Checks and safe verification instructions

Performed here: static source/config review, read-only action-tag ref checks, and `git diff --check`. The contract test and frontend build were **not run**, and no dependencies were installed, repository code/tests/builds/servers were executed, exchange or Telegram requests were made, workflow was dispatched, or remote writes/commits were performed.

On an isolated ephemeral runner with no credentials and no exchange/API egress:

1. Run `node --test web/tests/security-contract.test.mjs` (built-in Node test runner; no project dependency install required).
2. Validate workflow YAML/shell with a pinned `actionlint` binary, without dispatching the workflow.
3. Run `npm ci --prefix web` and `npm run build --prefix web`; review any lifecycle scripts before allowing this in a developer environment.
4. In a staging preview without credentials, inspect the browser console/network panel for CSP violations and confirm only the required snapshot/API connections are allowed.
5. Have the backend owner test direct unauthorized live-order requests and fail-closed limits in their isolated mock/testnet environment.

**Do not use the Telegram workflow as a test harness:** dispatching it fetches live market data, can send a Telegram message, and can push the `gh-pages` branch.
