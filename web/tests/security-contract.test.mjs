import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import test from 'node:test'
import { fileURLToPath } from 'node:url'

const root = resolve(fileURLToPath(new URL('../..', import.meta.url)))
const read = (path) => readFileSync(resolve(root, path), 'utf8')

function workflowRunBlocks(source) {
  const lines = source.split(/\r?\n/)
  const blocks = []
  for (let i = 0; i < lines.length; i += 1) {
    const match = /^(\s*)run:\s*\|\s*$/.exec(lines[i])
    if (!match) continue
    const indent = match[1].length
    const body = []
    for (let j = i + 1; j < lines.length; j += 1) {
      const line = lines[j]
      if (line.trim() && line.match(/^\s*/)[0].length <= indent) break
      body.push(line)
    }
    blocks.push(body.join('\n'))
  }
  return blocks
}

test('workflow dispatch values are typed, env-passed, and never interpolated into shell blocks', () => {
  const workflow = read('.github/workflows/telegram-push.yml')
  assert.match(workflow, /min_edge:\n\s+description:[^\n]*\n\s+type: number/)
  assert.match(workflow, /top_n:\n\s+description:[^\n]*\n\s+type: number/)
  assert.match(workflow, /MIN_EDGE:\s*\$\{\{\s*inputs\.min_edge\s*\}\}/)
  assert.match(workflow, /TOP_N:\s*\$\{\{\s*inputs\.top_n\s*\}\}/)
  assert.match(workflow, /--min-edge "\$MIN_EDGE"/)
  assert.match(workflow, /--top "\$TOP_N"/)
  assert.doesNotMatch(workflow, /github\.event\.inputs\.(?:min_edge|top_n)/)
  assert.doesNotMatch(workflow, /^\s+run:[^\n]*\$\{\{/m)
  for (const block of workflowRunBlocks(workflow)) {
    assert.doesNotMatch(block, /\$\{\{/)
  }
})

test('workflow actions are SHA-pinned and checkout credentials are not persisted', () => {
  for (const path of ['.github/workflows/ci.yml', '.github/workflows/telegram-push.yml']) {
    const workflow = read(path)
    const uses = workflow.split(/\r?\n/).filter((line) => /^\s+-\s+uses: actions\//.test(line))
    assert.ok(uses.length > 0, `${path} should use official actions`)
    for (const line of uses) assert.match(line, /@(?:[a-f0-9]{40})\b/)
    const checkoutCount = uses.filter((line) => line.includes('actions/checkout@')).length
    const persistenceCount = (workflow.match(/persist-credentials:\s*false/g) ?? []).length
    assert.equal(persistenceCount, checkoutCount, `${path} must disable checkout credential persistence`)
    assert.match(workflow, /^permissions:\n  contents: read/m)
  }

  const push = read('.github/workflows/telegram-push.yml')
  const scan = push.split('\n  notify:\n')[0]
  const notify = push.split('\n  notify:\n')[1].split('\n  publish:\n')[0]
  const publisher = push.split('\n  publish:\n')[1].split('\n  alert-on-failure:\n')[0]
  const alert = push.split('\n  alert-on-failure:\n')[1]
  assert.match(scan, /permissions:\n\s+contents: read/)
  assert.doesNotMatch(scan, /contents: write|TELEGRAM_BOT_TOKEN|TELEGRAM_CHAT_ID/)
  assert.match(notify, /TELEGRAM_BOT_TOKEN/)
  assert.match(notify, /TELEGRAM_CHAT_ID/)
  assert.doesNotMatch(notify, /actions\/checkout|python -m pip install|scripts\/notify\//)
  assert.match(publisher, /permissions:\n\s+contents: write/)
  assert.doesNotMatch(publisher, /python -m pip install|scripts\/notify\//)
  assert.match(alert, /permissions:\n\s+contents: read/)
  assert.doesNotMatch(alert, /contents: write/)
})

test('only supported Binance/Bybit forward carry paper opens remain in the frontend', () => {
  const venues = read('web/src/constants/venueOrder.ts')
  assert.match(venues, /SUPPORTED_CEX_UI_VENUES\s*=\s*\["binance",\s*"bybit"\]/)

  const router = read('web/src/router/index.ts')
  const dexRoute = router.match(/\{\s*path:\s*["']\/dex["'][^}]*\}/s)?.[0]
  assert.ok(dexRoute, 'the /dex route should exist')
  assert.match(dexRoute, /redirect:\s*["']\/cex["']/)
  assert.doesNotMatch(dexRoute, /component:/)
  assert.doesNotMatch(read('web/src/App.vue'), /key:\s*["']\/dex["']/)

  const cex = read('web/src/views/CexConnection.vue')
  assert.match(cex, /SUPPORTED_CEX_UI_VENUES/)
  assert.doesNotMatch(cex, /hyperliquid|dydx|walletTradeCapable/i)

  const scanner = read('web/src/views/Scanner.vue')
  assert.match(scanner, /paperOpenBlock/)
  assert.match(scanner, /SUPPORTED_CEX_UI_VENUES/)
  assert.match(scanner, /direction:\s*['"]forward['"]/)
  assert.match(scanner, /dry_run:\s*true/)
  assert.doesNotMatch(scanner, /confirmOpenWallet|useWalletTrade|WALLET_TRADE_VENUES|openMode|openDryRun\.value|liveExecutionAllowed/)
  assert.doesNotMatch(scanner, /strategy:\s*['"](?:pure_futures|unified)['"]/)

  const positions = read('web/src/views/Positions.vue')
  assert.match(positions, /function canClosePosition/)
  assert.match(positions, /SUPPORTED_CEX_UI_VENUES/)
  assert.match(positions, /!canClosePosition\(row\)/)
})

test('browser-wallet signing and private-key browser storage are disabled', () => {
  const walletFiles = [
    'web/src/composables/wallet/useHyperliquidTrade.ts',
    'web/src/composables/wallet/useDydxTrade.ts',
    'web/src/composables/wallet/useWalletTrade.ts',
  ]
  const source = walletFiles.map(read).join('\n')
  assert.doesNotMatch(source, /sessionStorage|localStorage|privateKey|signAmino|new ethers\.Wallet|ExchangeClient/)
  assert.match(source, /Browser-wallet DEX signing is disabled/)
  assert.match(source, /success:\s*false/)
  assert.doesNotMatch(read('web/src/views/DexConnection.vue'), /useWallet|useWalletTrade|signAmino|ExchangeClient/)
  assert.doesNotMatch(read('web/src/components/connection/TestOrderModal.vue'), /testnet|mainnet|placeOrder|signAmino/i)
})

test('deployed web and Tauri configurations use restrictive script CSPs', () => {
  const vercel = JSON.parse(read('web/vercel.json'))
  const vercelCsp = vercel.headers.flatMap((rule) => rule.headers)
    .find((header) => header.key === 'Content-Security-Policy')?.value
  const tauri = JSON.parse(read('web/src-tauri/tauri.conf.json'))
  const csp = [vercelCsp, tauri.tauri.security.csp, read('web/index.html')].join('\n')
  assert.ok(vercelCsp)
  assert.match(csp, /script-src 'self'/)
  assert.doesNotMatch(csp, /script-src[^;]*(?:unsafe-inline|unsafe-eval)/)
  assert.match(vercelCsp, /object-src 'none'/)
  assert.match(vercelCsp, /base-uri 'self'/)
  assert.match(vercelCsp, /frame-ancestors 'none'/)
  assert.doesNotMatch(tauri.tauri.security.csp, /script-src[^;]*(?:unsafe-inline|unsafe-eval)/)
  assert.match(read('web/index.html'), /http-equiv="Content-Security-Policy"/)
  assert.equal(vercel.buildCommand, 'npm ci && npm run build')
})

test('npm manifest and lock root metadata agree and registry entries have integrity hashes', () => {
  const manifest = JSON.parse(read('web/package.json'))
  const lock = JSON.parse(read('web/package-lock.json'))
  const rootPackage = lock.packages['']
  assert.deepEqual(rootPackage.dependencies, manifest.dependencies)
  assert.deepEqual(rootPackage.devDependencies, manifest.devDependencies)
  for (const [name, pkg] of Object.entries(lock.packages)) {
    if (name === '') continue
    assert.match(pkg.resolved ?? '', /^https:\/\/registry\.npmjs\.org\//, `${name} registry URL`)
    assert.ok(pkg.integrity, `${name} integrity hash`)
  }
})
