import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const read = (path) => readFileSync(resolve(root, path), 'utf8')
const expectMatch = (source, pattern, message) => assert.ok(pattern.test(source), message)

const locales = ['en', 'zh-CN', 'zh-TW'].map((locale) => ({
  locale,
  messages: JSON.parse(read(`web/src/locales/${locale}.json`)),
}))

test('carry scanner inputs are bounded and sent explicitly with each authenticated scan', () => {
  const scanner = read('web/src/views/Scanner.vue')
  expectMatch(scanner, /MAX_CARRY_NOTIONAL_USD\s*=\s*500/, 'notional cap is $500')
  expectMatch(scanner, /carryNotionalUsd\s*=\s*ref<number\s*\|\s*null>\(100\)/, 'notional defaults to $100')
  expectMatch(scanner, /carryHorizonHours\s*=\s*ref<number\s*\|\s*null>\(24\)/, 'horizon defaults to 24 hours')
  expectMatch(scanner, /:max="MAX_CARRY_NOTIONAL_USD"/, 'input enforces notional cap')
  expectMatch(scanner, /notional_usd:\s*String\(carryNotionalUsd\.value\)/, 'trigger passes notional')
  expectMatch(scanner, /horizon_hours:\s*String\(carryHorizonHours\.value\)/, 'trigger passes horizon')
  expectMatch(scanner, /apiFetch\(url,\s*\{\s*method:\s*'POST'\s*\}\)/, 'trigger uses authenticated API adapter')
})

test('carry candidate economics, ROI bases, cost assumptions, and snapshot identity are visible', () => {
  const scanner = read('web/src/views/Scanner.vue')
  for (const field of [
    'notional_usd_requested',
    'gross_funding_usd',
    'net_horizon_earnings_usd',
    'total_estimated_cost_usd',
    'entry_fee_usd',
    'exit_fee_usd',
    'observed_round_trip_book_cost_usd',
    'exit_slippage_assumption_usd',
    'basis_buffer_usd',
    'breakeven_funding_payments',
    'snapshot_ts_ms',
    'spot_book_observed_at_ms',
    'perp_book_observed_at_ms',
    'history_latest_ts',
    'history_first_ts',
    'funding_observed_at_ms',
    'snapshot_observed_at_ms',
    'capital_required_usd',
    'net_horizon_notional_roi_pct',
    'net_horizon_capital_roi_pct',
    'snapshot_id',
    'spot_book_snapshot_id',
    'perp_book_snapshot_id',
    'spot_instrument_snapshot_id',
    'perp_instrument_snapshot_id',
  ]) assert.ok(scanner.includes(field), `Scanner.vue should use ${field}`)
  expectMatch(scanner, /snapshotIdentity\(/, 'snapshot identity is rendered')
  expectMatch(scanner, /row\.snapshot_id/, 'canonical snapshot identity is included')
  expectMatch(scanner, /oneLegNotionalRoi/, 'one-leg notional ROI is labeled')
  expectMatch(scanner, /illustrativeCapitalRoi/, 'capital ROI is labeled')
  expectMatch(read('web/src/locales/en.json'), /illustrativeCapitalRoi[^\n]*prefunded 1x/i, 'capital ROI is explicitly illustrative and prefunded 1x')
})

test('paper opens use exactly the candidate scanned size and reject stale or mismatched economics', () => {
  const scanner = read('web/src/views/Scanner.vue')
  expectMatch(scanner, /function paperOpenBlock\(/, 'paper-open gate exists')
  expectMatch(scanner, /carryCandidateBlock\(/, 'carry candidate validation is applied')
  expectMatch(scanner, /MAX_CARRY_SNAPSHOT_AGE_MS/, 'candidate freshness is bounded')
  expectMatch(scanner, /maximum_instrument_snapshot_age_sec/, 'instrument snapshots honor backend freshness limits')
  assert.match(scanner, /notional_usd_requested/)
  assert.match(scanner, /horizon_hours/)
  expectMatch(scanner, /row\._direction !== 'forward' \|\| row\.direction !== 'forward'/, 'forward direction is required explicitly')
  expectMatch(scanner, /row\.venue !== row\._venue/, 'candidate venue must exactly match its venue bucket')
  expectMatch(scanner, /Math\.min\([\s\S]*maximum_history_gap_intervals[\s\S]*MAX_CARRY_HISTORY_GAP_INTERVALS/, 'frontend never permits a wider gap than the scanner')
  expectMatch(scanner, /snapshotTs - historyTs > intervalHours \* maxHistoryGapIntervals \* 3_600_000/, 'latest settlement freshness compares scan time with latest settlement')
  expectMatch(scanner, /MAX_CARRY_HISTORY_GAP_INTERVALS\s*=\s*1\.5/, 'pre-settlement settlement tail allows the server 1.5-interval boundary')
  expectMatch(scanner, /amount_usd:\s*Number\(row\.notional_usd_requested\)/, 'paper open uses scanned notional')
  expectMatch(scanner, /horizon_hours:\s*Number\(row\.horizon_hours\)/, 'paper open sends scanned horizon')
  expectMatch(scanner, /symbol:\s*row\.symbol/, 'paper open sends the exact scanned symbol')
  expectMatch(scanner, /scan_snapshot_id:\s*scan\.snapshot_id/, 'paper open sends server scan identity')
  expectMatch(scanner, /candidate_snapshot_id:\s*row\.snapshot_id/, 'paper open sends candidate identity')
  expectMatch(scanner, /openCarryPaperPosition\(/, 'paper open uses the typed snapshot-bound API helper')
  expectMatch(scanner, /dry_run:\s*true/, 'paper open explicitly requests dry-run')
  expectMatch(scanner, /scanner\.paperSnapshotOneUse/, 'one-use replay semantics are visible before opening')
  assert.doesNotMatch(scanner, /openAmount|v-model:value="openAmount"/)
})

test('the complete carry scanner response schema is represented in useApi', () => {
  const api = read('web/src/composables/useApi.ts')
  for (const field of [
    'schema_version',
    'notional_usd',
    'max_notional_usd',
    'horizon_hours',
    'max_horizon_hours',
    'scan_started_at_ms',
    'completed_at_ms',
    'snapshot_id',
    'funding_observation_min_ms',
    'funding_observation_max_ms',
    'instrument_snapshots',
    'instrument_snapshot_observation_times_ms',
    'assumptions',
    'disclaimer',
    'timestamp_ms',
    'notional_usd_requested',
    'snapshot_ts_ms',
    'capital_required_usd',
    'borrowing_used',
    'snapshot_id',
    'history_first_ts',
    'funding_observed_at_ms',
    'snapshot_observed_at_ms',
    'net_horizon_notional_roi_pct',
    'net_horizon_capital_roi_pct',
    'spot_book_snapshot_id',
    'perp_book_snapshot_id',
    'spot_instrument_snapshot_id',
    'perp_instrument_snapshot_id',
    'source_timestamp_skew_ms',
    'maximum_source_timestamp_skew_sec',
    'maximum_instrument_snapshot_age_sec',
    'maximum_history_gap_intervals',
    "capital_model",
    "short_margin_multiplier",
    "capital_fee_buffer_method",
    "leverage_or_live_execution_approved",
    "scan_snapshot_id",
    "candidate_snapshot_id",
    "CarryPaperOpenRequest",
    "openCarryPaperPosition",
  ]) assert.ok(api.includes(field), `useApi.ts should type or expose ${field}`)
})

test('Scanner visibly requires the memory-only API token before protected actions', () => {
  const scanner = read('web/src/views/Scanner.vue')
  expectMatch(scanner, /getApiToken/, 'scanner checks API token')
  expectMatch(scanner, /hasApiToken/, 'scanner tracks API token status')
  expectMatch(scanner, /scanner\.authRequired/, 'scanner shows localized auth requirement')
  expectMatch(scanner, /!getApiToken\(\)/, 'protected action blocks without a token')
})

test('wallet balance requests use the authenticated API adapter rather than raw fetch', () => {
  const wallet = read('web/src/composables/wallet/useWallet.ts')
  assert.match(wallet, /import\s+\{[^}]*apiFetch[^}]*\}\s+from\s+['"]@\/composables\/apiAuth['"]/s)
  assert.match(wallet, /apiFetch\(/)
  assert.doesNotMatch(wallet, /\bfetch\(/)
})

test('all locales describe Scanner execution as paper-only, not live-enabled', () => {
  for (const { locale, messages } of locales) {
    const scanner = messages.scanner
    assert.match(scanner.scanOnlyStrategy, /paper|模拟|模擬/i, `${locale}: scanner scan-only notice`)
    assert.match(scanner.authRequired, /API|令牌|token/i, `${locale}: auth requirement`)
    assert.match(scanner.carryRescanRequired, /scan|rescan|重新|扫描|掃描/i, `${locale}: stale scan warning`)
    assert.match(scanner.illustrativeCapitalRoi, /1x|1 倍|1倍/i, `${locale}: illustrative capital assumption`)
    assert.match(scanner.paperSnapshotOneUse, /once|one|最多|一次|僅可|仅可/i, `${locale}: paper candidate replay rule`)
    assert.doesNotMatch(scanner.scanOnlyStrategy, /live orders need|实盘需|實盤需/i, `${locale}: no live-enabled scanner copy`)
    assert.doesNotMatch(scanner.liveCexOnly, /Live orders are restricted|实盘订单仅|實盤訂單僅/i, `${locale}: no live-enabled order copy`)
    assert.doesNotMatch(scanner.openLive, /Open LIVE|实盘开仓|實盤開倉/i, `${locale}: live-open label retired`)
  }
})
