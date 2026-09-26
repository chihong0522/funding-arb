# npm dependency safety audit — `web`

**Audit date:** September 26, 2026
**Scope owner:** `web/package.json`, `web/package-lock.json`, chart callsites/tests, and this report
**Status:** ECharts XSS advisory remediated; final npm audit is clean.

## Scope and method

- Official advisory and release metadata were checked before changing versions. The affected Apache ECharts range is below `6.1.0`; the fixed version is `6.1.0`.[1]
- Official npm registry metadata confirms `vue-echarts@8.3.0` has peer dependencies `echarts: ^6.0.0` and `vue: ^3.3.0`, so it is the compatible wrapper major for ECharts 6.[2]
- The direct production dependencies are pinned to exact patched versions: `echarts@6.1.0` and `vue-echarts@8.3.0`. Existing approved root overrides remain unchanged: `brace-expansion@2.1.7`, `nanoid@3.3.19`, `postcss@8.5.28`, `valibot@1.4.2`, and `ws@8.21.0`.
- Lockfile generation and dependency acquisition ran in disposable `node:22-alpine` Docker containers without project secrets. Install hooks were disabled. The verification image digest was `sha256:0a7108bf6c7bf5de370ffb1a3ed6be93d405b43ff159f681a8d18c0e2bc2e402`.
- Build and tests ran as UID/GID `1001:1001`, with `--network=none`, dropped capabilities, `no-new-privileges`, an executable temporary filesystem, and no host credential/socket mounts. The source tree was copied into the disposable build tree; the host project was not run.
- No exchange/API request, trade, wallet signing, secret access, commit, or push was performed.

## Audit result

The baseline was captured from the current working tree before the ECharts migration. That tree already contained the other worker's npm override remediation, so it is not a clean `HEAD` baseline.

`npm audit --json --package-lock-only` and `npm audit --json --package-lock-only --omit=dev` were both run before and after the upgrade. npm totals are package nodes, not unique advisory counts.

| Lockfile state | Full tree | Production graph (`--omit=dev`) | Unique advisory |
| --- | ---: | ---: | ---: |
| Baseline before ECharts migration | 2 moderate, 0 high, 0 critical | 2 moderate, 0 high, 0 critical | 1 |
| Final exact lock | 0 total | 0 total | 0 |

The baseline's two package nodes were `echarts@5.6.0` and its direct wrapper `vue-echarts@7.0.3`, both propagated from `GHSA-fgmj-fm8m-jvvx`. The final audit JSON has no vulnerable package nodes and both audit commands exit 0.

Raw JSON evidence is retained under:

- `docs/validation/npm-audit-2026-09-26/before-full.json`
- `docs/validation/npm-audit-2026-09-26/before-production.json`
- `docs/validation/npm-audit-2026-09-26/after-full.json`
- `docs/validation/npm-audit-2026-09-26/after-production.json`
- `docs/validation/npm-audit-2026-09-26/after-exit.txt`

## Dependency and chart migration

| Dependency | Before | Final | Compatibility evidence |
| --- | --- | --- | --- |
| `echarts` | `5.6.0` resolved from `^5.5.0` | `6.1.0` exact | Official advisory fix floor; registry metadata resolves `zrender@6.1.0` and `tslib@2.3.0`.[1][3] |
| `vue-echarts` | `7.0.3` resolved from `^7.0.0` | `8.3.0` exact | Official registry peer range accepts ECharts `^6.0.0` and Vue `^3.3.0`.[2] |

The Vue callsites remain registered-component builds and continue using the `VChart` component:

- `web/src/views/Backtest.vue`
- `web/src/views/Positions.vue`

Their option builders now live in `web/src/charts/equity.ts`. This keeps the two real callsites on one typed migration surface and avoids duplicating ECharts 6 option behavior. The Backtest formatter HTML-escapes the dynamic timestamp before returning tooltip markup; this preserves the restrictive CSP and avoids turning chart data into tooltip markup. No CSP or security guard was weakened.

`web/tests/chart-migration.test.mjs` exercises both actual option builders against registered ECharts 6 chart components in SSR mode, verifies line/time-series data, and checks that a malicious timestamp cannot produce a raw `<img>` element. This is runtime chart validation, not only source-pattern matching.

## Exact commands

### Official metadata checks

```text
curl --fail --silent --show-error --location \
  https://registry.npmjs.org/echarts/6.1.0 \
  -o docs/validation/npm-audit-2026-09-26/echarts-6.1.0-registry.json

curl --fail --silent --show-error --location \
  https://registry.npmjs.org/vue-echarts/8.3.0 \
  -o docs/validation/npm-audit-2026-09-26/vue-echarts-8.3.0-registry.json
```

### Lock acquisition in Docker

```text
sudo -n docker run --rm --network=bridge \
  --cap-drop=ALL --security-opt no-new-privileges \
  --user 1001:1001 --tmpfs /tmp:rw,exec,nosuid,size=768m \
  -e NPM_CONFIG_CACHE=/tmp/npm-cache \
  -v /mnt/vol-1/funding-arb/web/package.json:/workspace/package.json:ro \
  -v /mnt/vol-1/funding-arb/web/package-lock.json:/workspace/package-lock.json:ro \
  -v /mnt/vol-1/funding-arb/web/node_modules:/workspace/node_modules:rw \
  -w /workspace node:22-alpine \
  npm install --package-lock-only --ignore-scripts --no-audit --no-fund --prefer-online
```

The final dependency acquisition was:

```text
sudo -n docker run --rm --network=bridge \
  --cap-drop=ALL --security-opt no-new-privileges \
  --user 1001:1001 --tmpfs /tmp:rw,exec,nosuid,size=768m \
  -e NPM_CONFIG_CACHE=/tmp/npm-cache \
  -v /mnt/vol-1/funding-arb/web/package.json:/workspace/package.json:ro \
  -v /mnt/vol-1/funding-arb/web/package-lock.json:/workspace/package-lock.json:ro \
  -v /mnt/vol-1/funding-arb/web/node_modules:/workspace/node_modules:rw \
  -w /workspace node:22-alpine \
  npm ci --ignore-scripts --no-audit --no-fund
```

### Audit commands

```text
npm audit --json --package-lock-only --no-fund
npm audit --json --package-lock-only --no-fund --omit=dev
```

Both were executed in the same isolated Docker pattern before and after the lock migration. The after artifacts are the authoritative final counts.

### Network-disabled frontend verification

```text
npm run build
npm test
```

`npm run build` runs `vue-tsc --noEmit && vite build`. `npm test` runs:

```text
node --experimental-strip-types --test tests/*.test.mjs
```

## Verification evidence

- Lock acquisition: `npm install --package-lock-only --ignore-scripts --no-audit --no-fund --prefer-online` succeeded.
- Clean install: `npm ci --ignore-scripts --no-audit --no-fund` succeeded; npm emitted only the pre-existing `vue-i18n@9.14.5` maintenance warning.
- Typecheck/build: passed; Vite transformed 4,395 modules and emitted only its existing large-chunk warning.
- Web tests: **21/21 passed**, including both ECharts runtime migration tests.
- Final npm audit: **0 total, 0 low, 0 moderate, 0 high, 0 critical** for both full and production-only graphs.
- Final lock SHA-256: `d832217eb5015ceb192f0ea55d02d357f506d4c29958d82f6df3b45039ebd9b3`.
- Full command output and statuses are retained under `docs/validation/npm-audit-2026-09-26/`.

## Remaining advisories and limitations

- **Remaining npm advisories:** none in the final full or production audit (`0` total).
- `vue-i18n@9.14.5` is deprecated/unsupported upstream, but npm did not report it as a vulnerability. Migrating to v11 is a separate major-version policy decision and was not mixed into this security upgrade.[4]
- The isolated checks prove dependency installation, type safety, production bundling, and chart option compatibility. They do not replace browser visual/E2E testing, deployment validation, CSP behavior in a real browser, backend integration, exchange reconciliation, or any live/paper trading authorization.
- The build still reports a non-blocking large JavaScript chunk warning. No chunk-splitting or unrelated performance scope was added to this security migration.

## Sources

[1] https://github.com/advisories/GHSA-fgmj-fm8m-jvvx — Apache ECharts XSS advisory and fixed version floor
[2] https://registry.npmjs.org/vue-echarts/8.3.0 — official `vue-echarts@8.3.0` metadata and ECharts/Vue peer ranges
[3] https://registry.npmjs.org/echarts/6.1.0 — official `echarts@6.1.0` metadata and `zrender@6.1.0` dependency
[4] https://vue-i18n.intlify.dev/guide/maintenance.html — vue-i18n maintenance policy
[5] https://github.com/ecomfe/vue-echarts/blob/main/README.md#upgrade-guide — official vue-echarts upgrade guidance
[6] https://echarts.apache.org/en/changelog.html — official Apache ECharts release/changelog index
