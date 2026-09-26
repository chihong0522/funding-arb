import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const read = (path) => readFileSync(resolve(root, path), 'utf8')

test('API token contract is validated and retained in memory only', () => {
  const auth = read('web/src/composables/apiAuth.ts')
  assert.match(auth, /\[A-Za-z0-9\._~-\]\{32,\}/)
  assert.match(auth, /ref<string \| null>\(null\)/)
  assert.match(auth, /function clearApiToken\s*\(/)
  assert.doesNotMatch(auth, /\b(?:localStorage|sessionStorage|indexedDB|document\.cookie)\b/)
})

test('HTTP API requests use a Bearer authorization header, never a token URL parameter', () => {
  const auth = read('web/src/composables/apiAuth.ts')
  const api = read('web/src/composables/useApi.ts')
  assert.match(auth, /headers\.set\(['"]Authorization['"],\s*`Bearer \$\{token\}`\)/)
  assert.match(api, /apiFetch\(`/)
  assert.doesNotMatch(auth, /searchParams\.set\([^)]*token|[?&]token=\$\{/i)
})

test('WebSocket sends the token and event protocols and handles auth/configuration failures', () => {
  const auth = read('web/src/composables/apiAuth.ts')
  const api = read('web/src/composables/useApi.ts')
  assert.match(auth, /funding-arb-token\./)
  assert.match(auth, /funding-arb-events\.v1/)
  assert.match(api, /new WebSocket\(url,\s*protocols\)/)
  assert.match(api, /4401/)
  assert.match(api, /1013/)
})

test('the app exposes a memory-only token control with a clear action and safe 401/503 status', () => {
  const authUi = read('web/src/components/ApiAuthControl.vue')
  const auth = read('web/src/composables/apiAuth.ts')
  const app = read('web/src/App.vue')
  assert.match(authUi, /type="password"/)
  assert.match(authUi, /autocomplete="off"/)
  assert.match(authUi, /clearApiToken\(/)
  assert.match(auth, /401/)
  assert.match(auth, /503/)
  assert.match(app, /ApiAuthControl/)
  assert.match(app, /v-if="!isDemoMode"[^>]*ApiAuthControl|<ApiAuthControl[^>]*v-if="!isDemoMode"/s)
})

test('Scanner paper opens bind to a bounded forward same-venue Binance/Bybit carry scan', () => {
  const scanner = read('web/src/views/Scanner.vue')
  assert.match(scanner, /MAX_CARRY_NOTIONAL_USD\s*=\s*500/)
  assert.match(scanner, /carryNotionalUsd\s*=\s*ref<number\s*\|\s*null>\(100\)/)
  assert.match(scanner, /:max="MAX_CARRY_NOTIONAL_USD"/)
  assert.match(scanner, /SUPPORTED_CEX_UI_VENUES/)
  assert.match(scanner, /function paperOpenBlock/)
  assert.match(scanner, /row\._direction\s*!==\s*'forward'/)
  assert.match(scanner, /direction:\s*'forward'/)
  assert.match(scanner, /dry_run:\s*true/)
  assert.match(scanner, /amount_usd:\s*Number\(row\.notional_usd_requested\)/)
  assert.match(scanner, /carryDataMatchesInputs/)
  assert.match(scanner, /MAX_CARRY_SNAPSHOT_AGE_MS/)
  assert.doesNotMatch(scanner, /openAmount|v-model:value="openAmount"/)
  assert.doesNotMatch(scanner, /openDryRun\.value|liveExecutionAllowed|confirmOpenWallet|useWalletTrade|openMode/)
  assert.doesNotMatch(scanner, /strategy:\s*['"](?:pure_futures|unified)['"]/)
  assert.match(scanner, /paperOnlyWarning/)
})

test('App, Scanner, and Backtest route their API fetches through the authenticated HTTP adapter', () => {
  const app = read('web/src/App.vue')
  const scanner = read('web/src/views/Scanner.vue')
  const backtest = read('web/src/views/Backtest.vue')
  assert.match(app, /apiFetch\(['"]\/api\/settings\/trading-mode['"]\)/)
  assert.match(scanner, /apiFetch\(/)
  assert.match(scanner, /subscribeApiToken/)
  assert.match(backtest, /apiFetch\(['"]\/api\/settings\/strategy['"]\)/)
  assert.doesNotMatch(app, /fetch\(['"]\/api\//)
  assert.doesNotMatch(scanner, /fetch\(/)
  assert.doesNotMatch(backtest, /fetch\(['"]\/api\//)
})
