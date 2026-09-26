<template>
  <div class="scanner-page">
    <n-card class="table-card">
      <template #header>
        <div class="filter-toolbar">
          <!-- 标题独占一行居中 -->
          <div class="toolbar-title-row">
            <n-text class="toolbar-title">{{ t('scanner.title') }}</n-text>
          </div>

          <!-- 策略：下划线 Tab（主导航，与下方筛选区分） -->
          <div class="toolbar-tabs-row">
            <n-tabs
              :value="strategy"
              type="line"
              size="medium"
              class="strategy-tabs"
              @update:value="onStrategyChange"
            >
              <n-tab name="pure" :tab="t('scanner.pureFutures')" />
              <n-tab name="carry" :tab="t('scanner.cashAndCarry')" />
              <n-tab name="unified" :tab="t('scanner.unifiedCC')" />
            </n-tabs>
          </div>

          <!-- 筛选区：左侧可换行，右侧固定扫描/状态 -->
          <div class="toolbar-filters-row">
            <div class="filters-left">
              <div class="filter-group">
                <n-text depth="3" class="filter-label">{{ t('scanner.venues') }}</n-text>
                <div class="venue-filter-row">
                  <div class="venue-presets">
                    <button
                      type="button"
                      class="preset-chip"
                      :class="{ active: isVenuePresetActive('cex') }"
                      @click="applyVenuePreset('cex')"
                    >
                      {{ t('scanner.venuePresetCex') }}
                    </button>
                    <n-tooltip :disabled="strategy === 'pure'" trigger="hover">
                      <template #trigger>
                        <button
                          type="button"
                          class="preset-chip"
                          :class="{ active: isVenuePresetActive('dex') }"
                          :disabled="strategy !== 'pure'"
                          @click="applyVenuePreset('dex')"
                        >
                          {{ t('scanner.venuePresetDex') }}
                        </button>
                      </template>
                      {{ t('scanner.dexPureOnly') }}
                    </n-tooltip>
                    <button
                      v-if="strategy === 'pure'"
                      type="button"
                      class="preset-chip"
                      :class="{ active: isVenuePresetActive('all') }"
                      @click="applyVenuePreset('all')"
                    >
                      {{ t('scanner.venuePresetAll') }}
                    </button>
                  </div>
                  <n-select
                    :value="selectedVenues"
                    :options="venueOptions"
                    :render-label="renderVenueOptionLabel"
                    :render-tag="renderVenueTag"
                    :style="venueSelectStyle"
                    multiple
                    size="small"
                    class="venue-filter"
                    :placeholder="t('scanner.selectVenues')"
                    @update:value="handleVenuesChange"
                  />
                </div>
              </div>

              <template v-if="strategy === 'pure'">
                <div class="filter-group filter-group-bordered">
                  <n-text depth="3" class="filter-label">{{ t('scanner.minRealEdge') }}</n-text>
                  <n-input-number
                    v-model:value="minEdgeFilter"
                    :min="0"
                    :max="100"
                    :step="0.05"
                    :show-button="false"
                    size="small"
                    class="edge-input"
                    :style="edgeInputStyle"
                    @input="onEdgeInput"
                  >
                    <template #suffix>%</template>
                  </n-input-number>
                </div>

                <div class="filter-group filter-group-bordered">
                  <n-text depth="3" class="filter-label">{{ t('scanner.interval') }}</n-text>
                  <div class="interval-row">
                    <n-select
                      v-model:value="intervalFilter"
                      :options="intervalOptions"
                      size="small"
                      class="interval-select"
                      :consistent-menu-width="false"
                    />
                    <router-link to="/docs/cross-interval#ci-blend" class="docs-link">{{ t('scanner.crossIntervalDocs') }}</router-link>
                  </div>
                </div>
              </template>

              <template v-if="strategy === 'carry'">
                <div class="filter-group filter-group-bordered">
                  <n-text depth="3" class="filter-label">{{ t('scanner.carryNotional') }}</n-text>
                  <n-input-number
                    v-model:value="carryNotionalUsd"
                    :min="MIN_CARRY_NOTIONAL_USD"
                    :max="MAX_CARRY_NOTIONAL_USD"
                    :step="10"
                    :precision="2"
                    size="small"
                    class="carry-input"
                  >
                    <template #prefix>$</template>
                  </n-input-number>
                </div>
                <div class="filter-group filter-group-bordered">
                  <n-text depth="3" class="filter-label">{{ t('scanner.carryHorizon') }}</n-text>
                  <n-input-number
                    v-model:value="carryHorizonHours"
                    :min="MIN_CARRY_HORIZON_HOURS"
                    :max="MAX_CARRY_HORIZON_HOURS"
                    :step="1"
                    :precision="0"
                    size="small"
                    class="carry-input"
                  >
                    <template #suffix>h</template>
                  </n-input-number>
                </div>
              </template>

              <div class="filter-group actions-inline">
                <n-tag v-if="refreshing" size="small" type="info" :bordered="false" class="status-tag">{{ t('scanner.scanningFromExchanges') }}</n-tag>
                <n-tag v-else-if="lastScanLabel" size="small" type="success" :bordered="false" class="status-tag">{{ lastScanLabel }}</n-tag>
                <n-button size="small" type="primary" ghost @click="handleTriggerScan" :loading="refreshing" :disabled="!isDemoMode && !hasApiToken" class="action-btn">
                  <template #icon><n-icon size="14"><SearchOutline /></n-icon></template>
                  {{ t('scanner.scanNow') }}
                </n-button>
              </div>
            </div>
          </div>
        </div>
      </template>

      <n-alert v-if="!isDemoMode && !hasApiToken" type="warning" :bordered="false" style="margin-bottom: 12px">
        {{ t('scanner.authRequired') }}
      </n-alert>

      <n-spin :show="loading || refreshing">
        <!-- PURE FUTURES -->
        <template v-if="strategy === 'pure'">
          <n-grid :cols="4" :x-gap="16" :y-gap="16" style="margin-bottom:16px">
            <n-gi v-for="(card, i) in pureStatCards" :key="i">
              <n-card size="small">
                <div class="stat-card-inner">
                  <div class="stat-icon" :style="{ background: card.color + '22', color: card.color }">
                    <n-icon size="24"><component :is="card.icon" /></n-icon>
                  </div>
                  <div class="stat-info">
                    <n-text depth="3" style="font-size:12px">{{ card.label }}</n-text>
                    <n-text style="font-size:22px;font-weight:700">{{ card.value }}</n-text>
                  </div>
                </div>
              </n-card>
            </n-gi>
          </n-grid>
          <n-data-table v-if="pureRows.length > 0" :columns="pureColumns" :data="pureRows" :bordered="false" :scroll-x="1480" :max-height="600" virtual size="small" striped />
          <n-empty v-else :description="t('scanner.noPureFutures')" style="padding:40px 0" />
        </template>

        <!-- CASH & CARRY -->
        <template v-if="strategy === 'carry'">
          <n-alert type="info" :bordered="false" style="margin-bottom: 12px" :show-icon="false">
            {{ t('scanner.scanOnlyStrategy') }}
            <router-link to="/docs/cash-and-carry" class="docs-link">{{ t('scanner.strategyDocsLink') }}</router-link>
          </n-alert>
          <n-alert v-if="carryVenues.length > 0 && carryRequiresRescan" type="warning" :bordered="false" style="margin-bottom: 12px">
            {{ t('scanner.carryRescanRequired') }}
          </n-alert>
          <n-grid :cols="4" :x-gap="16" :y-gap="16" style="margin-bottom:16px">
            <n-gi v-for="(card, i) in carryStatCards" :key="i">
              <n-card size="small">
                <div class="stat-card-inner">
                  <div class="stat-icon" :style="{ background: card.color + '22', color: card.color }">
                    <n-icon size="24"><component :is="card.icon" /></n-icon>
                  </div>
                  <div class="stat-info">
                    <n-text depth="3" style="font-size:12px">{{ card.label }}</n-text>
                    <n-text style="font-size:22px;font-weight:700">{{ card.value }}</n-text>
                  </div>
                </div>
              </n-card>
            </n-gi>
          </n-grid>
          <n-empty v-if="carryVenues.length === 0 && !loading" :description="t('scanner.carryRequiresScan')" style="padding:40px 0" />
          <div v-for="ven in carryVenues" :key="ven.venue" style="margin-bottom:12px">
            <n-card :title="ven.venue.toUpperCase()" size="small">
              <template #header-extra>
                <n-space size="small" align="center">
                  <n-tag size="small" :bordered="false">{{ ven.total_pairs }} {{ t('scanner.pairs') }}</n-tag>
                  <n-tag size="small" type="info" :bordered="false">
                    {{ formatUsd(ven.notional_usd) }} · {{ ven.horizon_hours ?? '—' }}h
                  </n-tag>
                  <n-tag size="small" :bordered="false">
                    {{ formatTimestamp(ven.timestamp_ms) }}
                  </n-tag>
                </n-space>
              </template>
              <n-text v-if="ven.disclaimer" depth="3" style="display:block;font-size:12px;margin-bottom:8px">
                {{ ven.disclaimer }}
              </n-text>
              <n-data-table
                :columns="carryColumns"
                :data="carryRowsForVenue(ven)"
                :bordered="false" :scroll-x="1740" size="small" striped
              />
            </n-card>
          </div>
        </template>

        <!-- UNIFIED C&C -->
        <template v-if="strategy === 'unified'">
          <n-alert type="info" :bordered="false" style="margin-bottom: 12px" :show-icon="false">
            {{ t('scanner.scanOnlyStrategy') }}
            <router-link to="/docs/unified-carry" class="docs-link">{{ t('scanner.strategyDocsLink') }}</router-link>
          </n-alert>
          <n-grid :cols="4" :x-gap="16" :y-gap="16" style="margin-bottom:16px">
            <n-gi v-for="(card, i) in unifiedStatCards" :key="i">
              <n-card size="small">
                <div class="stat-card-inner">
                  <div class="stat-icon" :style="{ background: card.color + '22', color: card.color }">
                    <n-icon size="24"><component :is="card.icon" /></n-icon>
                  </div>
                  <div class="stat-info">
                    <n-text depth="3" style="font-size:12px">{{ card.label }}</n-text>
                    <n-text style="font-size:22px;font-weight:700">{{ card.value }}</n-text>
                  </div>
                </div>
              </n-card>
            </n-gi>
          </n-grid>
          <n-data-table v-if="unifiedRows.length > 0" :columns="unifiedColumns" :data="unifiedRows" :bordered="false" :scroll-x="800" :max-height="600" virtual size="small" striped />
          <n-empty v-else :description="t('scanner.unifiedRequiresScan')" style="padding:40px 0" />
        </template>
      </n-spin>
    </n-card>

    <n-modal v-model:show="showOpenModal" preset="card" :title="t('scanner.openPosition')" style="width: 560px;max-width:calc(100vw - 32px)">
      <n-form label-placement="left" label-width="110" size="small">
        <n-form-item :label="t('scanner.pair')">
          <n-text strong>{{ openModalSummary }}</n-text>
        </n-form-item>
        <n-form-item :label="t('scanner.scannedNotional')">
          <n-text strong>{{ openScannedNotional }}</n-text>
        </n-form-item>

        <n-form-item :label="t('scanner.executionMode')">
          <n-tag size="small" type="info" :bordered="false">{{ t('scanner.paperOnly') }}</n-tag>
        </n-form-item>
        <n-alert type="info" :bordered="false" style="margin-top: 8px">
          {{ t('scanner.paperOnlyWarning') }}
        </n-alert>
        <n-alert v-if="openTarget?.kind === 'carry'" type="warning" :bordered="false" style="margin-top: 8px">
          {{ t('scanner.illustrativeCapitalRoi') }}: {{ t('scanner.capitalRoiAssumption', { capital: openCapitalRequired }) }}
          <br />{{ t('scanner.paperSnapshotOneUse') }}
        </n-alert>

        <n-alert
          v-if="openTarget?.kind === 'pure' && openTarget.row.basis_risk_level !== 'clean'"
          :type="openTarget.row.basis_risk_level === 'high' ? 'error' : 'warning'"
          :title="openTarget.row.basis_risk_level === 'high' ? t('scanner.riskHigh') : t('scanner.riskCaution')"
          style="margin-top: 8px"
        >
          {{ pureRowRiskHint(openTarget.row) }}
          <template v-if="openTarget.row.basis_risk_level === 'high'">
            <br />
            <n-text depth="3" style="font-size: 12px">
              {{ t('scanner.riskHighDetail', { mark: openTarget.row.mark_spread_pct.toFixed(4), net: openTarget.row.net_edge_pct.toFixed(4), real: openTarget.row.real_edge_pct.toFixed(4) }) }}
            </n-text>
          </template>
        </n-alert>
      </n-form>
      <template #footer>
        <n-space justify="end">
          <n-button size="small" @click="showOpenModal = false">{{ t('scanner.cancel') }}</n-button>
          <n-button size="small" type="primary" :loading="opening" @click="confirmOpen">
            {{ t('scanner.openDryRun') }}
          </n-button>
        </n-space>
      </template>
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed, h, watch } from 'vue'
import { NCard, NGrid, NGi, NDataTable, NButton, NSpace, NText, NIcon, NSpin, NTag, NEmpty, NInputNumber, NModal, NForm, NFormItem, NSelect, NTooltip, NTabs, NTab, NAlert, useMessage, type DataTableColumns, type SelectOption, type SelectGroupOption } from 'naive-ui'
import { RouterLink, useRoute } from 'vue-router'
import { SearchOutline, TrendingUpOutline, FlashOutline, AnalyticsOutline } from '@vicons/ionicons5'
import { openCarryPaperPosition, useWebSocket, type ScannerOpportunities, type CarryVenue, type CarryCand, type UnifiedCarryCand, type WsMessage } from '@/composables/useApi'
import { apiFetch, getApiToken, subscribeApiToken } from '@/composables/apiAuth'
import { isDemoMode, useDemoSnapshot } from '@/composables/useDemoSnapshot'
import { useI18n } from 'vue-i18n'
import { CEX_VENUE_RANK, DEX_VENUE_RANK, SUPPORTED_CEX_UI_VENUES } from '@/constants/venueOrder'

const { t } = useI18n()
const route = useRoute()

const CEX_VENUES = CEX_VENUE_RANK
const PERP_DEX_VENUES = DEX_VENUE_RANK
const DEX_VENUES = new Set<string>(DEX_VENUE_RANK)
// Carry / Unified need spot + borrow — perp-only DEX venues are excluded server-side
const CARRY_CAPABLE = new Set(['binance', 'bitget', 'bybit', 'okx'])

const message = useMessage()

function colTitle(labelKey: string, tipKey: string, docsPath?: string) {
  return () => h(NTooltip, { trigger: 'hover' }, {
    trigger: () => h('span', { class: 'col-title-tip' }, t(labelKey)),
    default: () => [
      h('span', null, t(tipKey)),
      docsPath
        ? h(RouterLink, { to: docsPath, class: 'docs-link', style: 'margin-left: 8px' }, () => t('scanner.columnDocsLink'))
        : null,
    ],
  })
}

type Strategy = 'pure' | 'carry' | 'unified'
const SCANNER_STRATEGY_KEY = 'scanner_strategy'

function loadSavedStrategy(): Strategy {
  // Deep-link support (e.g. Telegram digest buttons: /?strategy=carry) takes
  // priority over the locally remembered tab so a shared link always opens
  // where it points, regardless of what the visitor last had open.
  const q = route.query.strategy
  if (q === 'pure' || q === 'carry' || q === 'unified') return q
  try {
    const v = localStorage.getItem(SCANNER_STRATEGY_KEY)
    if (v === 'pure' || v === 'carry' || v === 'unified') return v
  } catch { /* private mode / SSR */ }
  return 'pure'
}

const strategy = ref<Strategy>(loadSavedStrategy())
const loading = ref(false)
const refreshing = ref(false)
const lastScanLabel = ref('')

// Data stores per strategy
const pureData = ref<ScannerOpportunities | null>(null)
const carryData = ref<CarryVenue[]>([])
const unifiedData = ref<UnifiedCarryCand[]>([])

const MIN_CARRY_NOTIONAL_USD = 10
const MAX_CARRY_NOTIONAL_USD = 500
const MIN_CARRY_HORIZON_HOURS = 1
const MAX_CARRY_HORIZON_HOURS = 720
const MAX_CARRY_SNAPSHOT_AGE_MS = 60_000
const MAX_CARRY_BOOK_AGE_MS = 60_000
const MAX_CARRY_BOOK_SKEW_MS = 10_000
const MAX_CARRY_SOURCE_SKEW_MS = 60_000
const MAX_CARRY_FUTURE_SKEW_MS = 30_000
const MAX_CARRY_INSTRUMENT_AGE_MS = 3_600_000
const MIN_CARRY_HISTORY_SAMPLES = 3
const MAX_CARRY_HISTORY_GAP_INTERVALS = 1.5
const carryNotionalUsd = ref<number | null>(100)
const carryHorizonHours = ref<number | null>(24)
const carryScanInputsValid = computed(() =>
  Number.isFinite(carryNotionalUsd.value)
  && carryNotionalUsd.value !== null
  && carryNotionalUsd.value >= MIN_CARRY_NOTIONAL_USD
  && carryNotionalUsd.value <= MAX_CARRY_NOTIONAL_USD
  && Number.isFinite(carryHorizonHours.value)
  && carryHorizonHours.value !== null
  && carryHorizonHours.value >= MIN_CARRY_HORIZON_HOURS
  && carryHorizonHours.value <= MAX_CARRY_HORIZON_HOURS,
)
const hasApiToken = ref(Boolean(getApiToken()))
const currentTimeMs = ref(Date.now())

const minEdgeFilter = ref<number>(0)
const intervalFilter = ref<'all' | 'same' | 'cross'>('all')

const intervalOptions = computed<SelectOption[]>(() => [
  { label: t('scanner.all'), value: 'all' },
  { label: t('scanner.sameInterval'), value: 'same' },
  { label: `${t('scanner.cross')} ⚠`, value: 'cross' },
])
// Default to CEX; DEX venues are opt-in via preset buttons
const DEFAULT_VENUES = [...CEX_VENUES]
const selectedVenues = ref<string[]>([...DEFAULT_VENUES])
const lastScannedVenues = ref<string[]>([])

// Track raw input text so the width adapts while typing (e.g. "0." before it becomes a valid number)
const edgeInputText = ref('0')

function onEdgeInput(val: string | null) {
  edgeInputText.value = val || '0'
}

// Keep text in sync when the value changes via +/- buttons or programmatic updates
watch(minEdgeFilter, (v) => {
  edgeInputText.value = (v ?? 0).toString()
})

// Adaptive width: ch units track digit count; no stepper buttons (show-button=false)
// so the inner vertical divider cannot overlap the typed number.
const edgeInputStyle = computed(() => {
  const chars = Math.max(edgeInputText.value.length, 1)
  const widthCh = chars + 3 // room for "%" suffix + padding
  return {
    width: `max(4.5rem, ${widthCh}ch)`,
    minWidth: '4.5rem',
    maxWidth: '12rem',
  }
})

const venueOptions = computed<Array<SelectOption | SelectGroupOption>>(() => [
  {
    label: 'CEX',
    type: 'group',
    children: CEX_VENUES.map((v) => ({
      label: v.charAt(0).toUpperCase() + v.slice(1),
      value: v,
      disabled: strategy.value !== 'pure' && !CARRY_CAPABLE.has(v),
    })),
  },
  {
    label: 'DEX',
    type: 'group',
    children: PERP_DEX_VENUES.map((v) => ({
      label: v.charAt(0).toUpperCase() + v.slice(1),
      value: v,
      disabled: strategy.value !== 'pure' && !CARRY_CAPABLE.has(v),
    })),
  },
])

// Dynamically resize the venue select based on selected labels so every chip is visible and easy to close
const venueSelectStyle = computed(() => {
  const labels = selectedVenues.value.map((v) => v.charAt(0).toUpperCase() + v.slice(1))
  const charW = 7 // approximate per-char width at 11px font
  const chipW = labels.reduce((sum, l) => sum + l.length * charW + 32, 0) // 32 = close button + padding
  const gap = Math.max(labels.length - 1, 0) * 4
  const width = labels.length === 0 ? 180 : Math.min(560, Math.max(180, chipW + gap + 34)) // 34 = arrow + inner padding
  return { width: `${width}px`, maxWidth: '100%' }
})

// Render option label inside the dropdown — group headers are styled differently
function renderVenueOptionLabel(option: SelectOption | SelectGroupOption) {
  if ('type' in option && option.type === 'group') {
    return h('div', { class: 'venue-section-label' }, String(option.label))
  }
  return h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '6px' } }, [
    option.label as string,
    DEX_VENUES.has(option.value as string)
      ? h('span', { class: 'dex-mini-tag' }, 'DEX')
      : null,
  ])
}

// Compact tag renderer for the selected chips in the input
function renderVenueTag({ option, handleClose }: { option: SelectOption; handleClose: () => void }) {
  const isDex = DEX_VENUES.has(option.value as string)
  return h('div', { class: 'venue-chip' }, [
    h('span', { class: isDex ? 'venue-chip-name dex' : 'venue-chip-name' }, option.label as string),
    h('span', { class: 'venue-chip-close', onClick: handleClose }, '×'),
  ])
}

function closeEnough(value: unknown, expected: unknown): boolean {
  return typeof value === 'number'
    && typeof expected === 'number'
    && Number.isFinite(value)
    && Number.isFinite(expected)
    && Math.abs(value - expected) <= Math.max(1e-7, Math.abs(expected) * 1e-9)
}

function carryDataMatchesInputs(data: unknown, notionalUsd: number, horizonHours: number): boolean {
  if (!Array.isArray(data) || data.length === 0) return false
  return data.every((venue) => {
    const row = venue as CarryVenue
    return typeof row?.venue === 'string'
      && !row.error
      && closeEnough(row.notional_usd, notionalUsd)
      && closeEnough(row.horizon_hours, horizonHours)
  })
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function positiveNumber(value: unknown): number | null {
  return isFiniteNumber(value) && value > 0 ? value : null
}

async function loadStrategyVenues() {
  // Demo mode: no backend to ask — seed from the snapshot's venue list so the
  // venue filter and pure-futures default selection match what was scanned.
  if (isDemoMode) {
    const snap = useDemoSnapshot().snapshot.value
    const venues = snap?.scanner_opportunities?.venues
    if (Array.isArray(venues) && venues.length > 0) {
      selectedVenues.value = venuesForStrategy(strategy.value, venues)
    }
    return
  }
  if (!getApiToken()) return
  try {
    const resp = await apiFetch('/api/settings/strategy')
    const json = await resp.json()
    const venues = json.data?.scan_venues
    if (Array.isArray(venues) && venues.length > 0) {
      selectedVenues.value = venuesForStrategy(strategy.value, venues)
    }
  } catch { /* ignore */ }
}

function venuesForStrategy(st: Strategy, venues: string[]): string[] {
  if (st === 'pure') return venues.length > 0 ? [...venues] : [...DEFAULT_VENUES]
  const cex = venues.filter((v) => CARRY_CAPABLE.has(v))
  return cex.length > 0 ? cex : [...DEFAULT_VENUES]
}

function effectiveVenues(): string[] {
  const v = selectedVenues.value
  return v.length > 0 ? v : [...DEFAULT_VENUES]
}

function venuesMatch(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false
  const set = new Set(a)
  return b.every((x) => set.has(x))
}

function scanVenuesForStrategy(st: Strategy): string[] {
  return venuesForStrategy(st, effectiveVenues())
}

function cacheMatchesSelection(st: Strategy): boolean {
  if (lastScannedVenues.value.length === 0) return false
  return venuesMatch(lastScannedVenues.value, scanVenuesForStrategy(st))
}

let _venuesWatchTimer: ReturnType<typeof setTimeout> | null = null

type VenuePreset = 'cex' | 'dex' | 'all'

function venuesForPreset(preset: VenuePreset): string[] {
  if (strategy.value !== 'pure') {
    return preset === 'dex' ? [] : [...CEX_VENUES]
  }
  if (preset === 'cex') return [...CEX_VENUES]
  if (preset === 'dex') return [...PERP_DEX_VENUES]
  return [...CEX_VENUES, ...PERP_DEX_VENUES]
}

function isVenuePresetActive(preset: VenuePreset): boolean {
  const expected = venuesForPreset(preset)
  if (expected.length === 0) return false
  const current = [...selectedVenues.value].sort()
  const exp = [...expected].sort()
  return current.length === exp.length && current.every((v, i) => v === exp[i])
}

function applyVenuePreset(preset: VenuePreset) {
  const next = venuesForPreset(preset)
  if (next.length === 0) return
  handleVenuesChange(next)
}

// Handles manual changes in the venue dropdown (both selection and removal of tags)
function handleVenuesChange(val: string[]) {
  if (val.length === 0) {
    message.warning(t('scanner.venuesRequired'))
    return
  }
  selectedVenues.value = val

  if (strategy.value === 'pure' && val.length < 2) {
    message.info(t('scanner.venuesNeedTwo'))
    return
  }

  // Demo mode: venue filter is applied client-side (pureRows filters by
  // selectedVenues). No backend to re-scan — the snapshot is the snapshot.
  if (isDemoMode) return

  // Rescan only the selected venues (debounced)
  if (refreshing.value) return
  if (_venuesWatchTimer) clearTimeout(_venuesWatchTimer)
  _venuesWatchTimer = setTimeout(() => {
    handleTriggerScan()
  }, 400)
}

// venue id → trade_capable (scan-only venues get a disabled Open button)
const venueCaps = ref<Record<string, { trade: boolean; reason: string }>>({})

async function loadVenueCapabilities() {
  // Demo mode: every venue in the snapshot is treated as scan-capable but
  // not trade-capable (no backend to execute through). This disables the
  // "Open position" button in the Scanner table — demo is read-only.
  if (isDemoMode) {
    const snap = useDemoSnapshot().snapshot.value
    const venues = snap?.scanner_opportunities?.venues ?? []
    const caps: Record<string, { trade: boolean; reason: string }> = {}
    for (const v of venues) {
      caps[v] = { trade: false, reason: 'demo mode — read-only' }
    }
    venueCaps.value = caps
    return
  }
  if (!getApiToken()) {
    venueCaps.value = {}
    return
  }
  try {
    const resp = await apiFetch('/api/settings/venues')
    const json = await resp.json()
    if (json.success && Array.isArray(json.data)) {
      const caps: Record<string, { trade: boolean; reason: string }> = {}
      for (const v of json.data) {
        caps[v.id] = { trade: v.trade_capable !== false, reason: v.trade_reason || '' }
      }
      venueCaps.value = caps
    }
  } catch { /* ignore */ }
}

function venuesQuery(st?: Strategy): string {
  return scanVenuesForStrategy(st ?? strategy.value).join(',')
}

function applyScanData(st: Strategy, data: unknown) {
  if (st === 'pure') {
    const d = data as ScannerOpportunities
    pureData.value = d
    lastScannedVenues.value = Array.isArray(d?.venues) ? [...d.venues] : scanVenuesForStrategy(st)
  } else if (st === 'carry') {
    const rows = Array.isArray(data) ? data as CarryVenue[] : []
    carryData.value = rows
    lastScannedVenues.value = rows.map((v) => v.venue).filter(Boolean)
  } else {
    unifiedData.value = Array.isArray(data) ? data as UnifiedCarryCand[] : []
    lastScannedVenues.value = scanVenuesForStrategy(st)
  }
}

function formatScanTime(iso: string | null | undefined) {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    return t('scanner.lastScan', { time: d.toLocaleTimeString() })
  } catch {
    return ''
  }
}

async function refreshScanLabel(st: Strategy) {
  // Demo mode: derive the label from the snapshot's status block, not the API.
  if (isDemoMode) {
    const snap = useDemoSnapshot().snapshot.value
    const ts = (snap?.scanner_status as any)?.last_scan_time
    lastScanLabel.value = formatScanTime(ts)
    return
  }
  try {
    const resp = await apiFetch(`/api/scanner/status?strategy=${st}`)
    const json = await resp.json()
    lastScanLabel.value = formatScanTime(json.data?.last_scan_time)
  } catch {
    lastScanLabel.value = ''
  }
}

/**
 * Demo-mode data loader. Reads the cached snapshot from raw.githubusercontent.com (via
 * useDemoSnapshot) and feeds it into the same applyScanData path the live
 * backend would use, so the Scanner table renders identically.
 *
 * Only the `pure` strategy is backed by the snapshot — carry / unified have
 * no demo answer (they need spot/borrow data), so we surface an empty state
 * and let the UI's "carryRequiresScan" / "unifiedRequiresScan" notices show.
 */
async function loadDemoData(st: Strategy) {
  const demo = useDemoSnapshot()
  await demo.ensure()
  const snap = demo.snapshot.value
  if (!snap) {
    // Snapshot fetch failed — let the empty-state UI surface the error.
    if (st === 'pure') pureData.value = null
    else if (st === 'carry') carryData.value = []
    else unifiedData.value = []
    lastScanLabel.value = ''
    return
  }
  if (st === 'pure') {
    applyScanData('pure', snap.scanner_opportunities)
  } else if (st === 'carry') {
    // Carry: snapshot holds per-venue buckets; feed them straight in.
    const carryVenues = (snap as any).scanner_carry_venues ?? []
    carryData.value = carryVenues as CarryVenue[]
    lastScannedVenues.value = carryVenues.map((v: any) => v.venue).filter(Boolean)
  } else {
    // Unified: snapshot holds a {venues, forward, reverse} route list.
    const routes = (snap as any).scanner_unified_routes ?? { forward: [], reverse: [] }
    unifiedData.value = [...(routes.forward ?? []), ...(routes.reverse ?? [])] as UnifiedCarryCand[]
    lastScannedVenues.value = routes.venues ?? scanVenuesForStrategy(st)
  }
  await refreshScanLabel(st)
}

async function waitForScanData(
  st: Strategy,
  maxMs = 120000,
  expectedCarryInputs?: { notionalUsd: number; horizonHours: number },
) {
  const start = Date.now()
  const vq = venuesQuery(st)
  while (Date.now() - start < maxMs) {
    await new Promise((r) => setTimeout(r, 2000))
    const resp = await apiFetch(
      `/api/scanner/opportunities?strategy=${st}&venues=${encodeURIComponent(vq)}`,
    )
    const json = await resp.json()
    const carryMatches = st !== 'carry'
      || (!!expectedCarryInputs && carryDataMatchesInputs(
        json.data,
        expectedCarryInputs.notionalUsd,
        expectedCarryInputs.horizonHours,
      ))
    if (json.success && json.has_data && carryMatches) {
      applyScanData(st, json.data)
      await refreshScanLabel(st)
      return true
    }
    const status = await apiFetch(`/api/scanner/status?strategy=${st}`).then((r) => r.json())
    if (!status.data?.scanning) break
  }
  return false
}

async function loadData(s?: Strategy | MouseEvent, autoScan = true) {
  const st = (s && typeof s !== 'object') ? s : strategy.value
  if (!isDemoMode && !getApiToken()) return
  loading.value = true
  try {
    // ─── Demo mode: read from the static snapshot, skip the backend entirely.
    // The snapshot holds scanner_opportunities + scanner_status in the exact
    // shape applyScanData expects — no {success, data} envelope, no live flag.
    if (isDemoMode) {
      await loadDemoData(st)
      return
    }
    const vq = venuesQuery(st)
    const resp = await apiFetch(
      `/api/scanner/opportunities?strategy=${st}&venues=${encodeURIComponent(vq)}`,
    )
    const json = await resp.json()
    const available = json.live ?? false
    const hasData = json.has_data ?? false
    const expectedCarryInputs = st === 'carry' && carryScanInputsValid.value
      ? {
          notionalUsd: Number(carryNotionalUsd.value),
          horizonHours: Number(carryHorizonHours.value),
        }
      : undefined
    const carryMatches = st !== 'carry'
      || (!!expectedCarryInputs && carryDataMatchesInputs(
        json.data,
        expectedCarryInputs.notionalUsd,
        expectedCarryInputs.horizonHours,
      ))
    if (!available) {
      message.warning(t('scanner.scannerUnavailable'))
      return
    }
    if (json.success && hasData && !json.venues_mismatch) {
      // A size/horizon mismatch is still shown with an explicit stale warning,
      // but the paper-open gate below refuses to use those economics.
      applyScanData(st, json.data)
      await refreshScanLabel(st)
    } else if (json.venues_mismatch) {
      // Cached scan was for different venues — don't show those rows.
      if (st === 'pure') pureData.value = null
      else if (st === 'carry') carryData.value = []
      else unifiedData.value = []
      lastScannedVenues.value = []
    }
    const needsScan = !hasData || json.venues_mismatch || !carryMatches
    if (autoScan && needsScan && !refreshing.value) {
      if (st === 'pure' && scanVenuesForStrategy(st).length < 2) return
      const status = await apiFetch(`/api/scanner/status?strategy=${st}`).then((r) => r.json())
      if (status.data?.scanning) {
        refreshing.value = true
        try {
          const ok = await waitForScanData(st, 120000, expectedCarryInputs)
          if (!ok) message.warning(t('scanner.scanFailed'))
        } finally {
          refreshing.value = false
        }
      } else {
        await handleTriggerScan()
      }
    }
  } catch { /* ignore */ }
  finally { loading.value = false }
}

async function handleTriggerScan() {
  const st = strategy.value
  if (!isDemoMode && !getApiToken()) {
    message.warning(t('scanner.authRequired'))
    return
  }
  if (st === 'carry' && !carryScanInputsValid.value) {
    message.warning(t('scanner.carryInvalidInputs'))
    return
  }
  const expectedCarryInputs = st === 'carry'
    ? {
        notionalUsd: Number(carryNotionalUsd.value),
        horizonHours: Number(carryHorizonHours.value),
      }
    : undefined
  // Demo mode: there's no backend to trigger — just re-fetch the snapshot
  // (forces a fresh snapshot fetch) and re-apply. Gives the user a sense that
  // the "Scan Now" button does something in the live demo.
  if (isDemoMode) {
    refreshing.value = true
    try {
      await useDemoSnapshot().refresh()
      await loadDemoData(st)
      message.success(t('scanner.scanComplete'))
    } catch {
      message.warning(t('scanner.scanFailed'))
    } finally {
      refreshing.value = false
    }
    return
  }
  if (st === 'pure' && scanVenuesForStrategy(st).length < 2) {
    message.info(t('scanner.venuesNeedTwo'))
    return
  }
  refreshing.value = true
  try {
    const vq = venuesQuery(st)
    const params = new URLSearchParams({
      strategy: st,
      venues: vq,
      ...(st === 'carry' ? {
        notional_usd: String(carryNotionalUsd.value),
        horizon_hours: String(carryHorizonHours.value),
      } : {}),
    })
    const url = `/api/scanner/trigger?${params.toString()}`
    const resp = await apiFetch(url, { method: 'POST' })
    const json = await resp.json()
    if (json.success) {
      applyScanData(st, json.data)
      await refreshScanLabel(st)
      const resultMatches = st !== 'carry' || (!!expectedCarryInputs && carryDataMatchesInputs(
        json.data,
        expectedCarryInputs.notionalUsd,
        expectedCarryInputs.horizonHours,
      ))
      if (resultMatches) message.success(t('scanner.scanComplete'))
      else message.warning(t('scanner.carryRescanRequired'))
    } else if (json.error === 'Scan already in progress') {
      const ok = await waitForScanData(st, 120000, expectedCarryInputs)
      if (ok) message.success(t('scanner.scanComplete'))
      else message.warning(st === 'carry' ? t('scanner.carryRescanRequired') : t('scanner.scanFailed'))
    } else {
      message.error(json.error || t('scanner.scanFailed'))
    }
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('scanner.scanFailed'))
  }
  finally { refreshing.value = false }
}

function onStrategyChange(val: Strategy) {
  strategy.value = val
  try {
    localStorage.setItem(SCANNER_STRATEGY_KEY, val)
  } catch { /* ignore */ }
  selectedVenues.value = venuesForStrategy(val, selectedVenues.value)
  loadData(val, true)
}

// ---- WebSocket live updates (pushed by background scanner) ----
// Debounce WS messages: coalesce rapid updates into one render per ~200ms
// to avoid re-rendering hundreds of table rows on every push.
let _pendingWsMsg: WsMessage | null = null
let _wsDebounceTimer: ReturnType<typeof setTimeout> | null = null
function onWsMessage(msg: WsMessage) {
  if (msg.event !== 'scanner.update') return
  _pendingWsMsg = msg
  if (_wsDebounceTimer) return
  _wsDebounceTimer = setTimeout(() => {
    _wsDebounceTimer = null
    const m = _pendingWsMsg
    _pendingWsMsg = null
    if (m) _applyWsMessage(m)
  }, 200)
}
function _applyWsMessage(msg: WsMessage) {
  const d = msg.data as Record<string, any>
  if (d.recalc_fees && d.data && typeof d.data === 'object') {
    const payload = d.data as Record<string, unknown>
    if (payload.pure) applyScanData('pure', payload.pure)
    if (payload.carry) applyScanData('carry', payload.carry)
    if (payload.unified) applyScanData('unified', payload.unified)
    return
  }
  if (d.strategy === 'carry') {
    const rows = Array.isArray(d.data) ? d.data as CarryVenue[] : []
    const scanned = rows.map((v) => v.venue).filter(Boolean)
    if (strategy.value === 'carry' && venuesMatch(scanned, scanVenuesForStrategy('carry'))) {
      carryData.value = rows
      lastScannedVenues.value = [...scanned]
      refreshScanLabel('carry')
    }
  } else if (d.strategy === 'unified') {
    unifiedData.value = Array.isArray(d.data) ? d.data : []
    refreshScanLabel('unified')
  } else if (d.forward || d.reverse) {
    const scanned = Array.isArray(d.venues) ? d.venues as string[] : []
    if (strategy.value === 'pure' && venuesMatch(scanned, scanVenuesForStrategy('pure'))) {
      pureData.value = d as ScannerOpportunities
      lastScannedVenues.value = [...scanned]
      refreshScanLabel('pure')
    }
  }
}
const ws = useWebSocket(onWsMessage)

// ---- Open position dialog ----
type OpenTarget =
  | { kind: 'pure'; row: PureRow }
  | { kind: 'carry'; row: CarryRow }
  | { kind: 'unified'; row: UnifiedCarryCand }

const showOpenModal = ref(false)
const opening = ref(false)
const openTarget = ref<OpenTarget | null>(null)

const openModalSummary = computed(() => {
  const tgt = openTarget.value
  if (!tgt) return ''
  if (tgt.kind === 'pure') {
    const r = tgt.row
    return `${r.base}/USDT — long ${r.long_venue}, short ${r.short_venue}`
  }
  if (tgt.kind === 'carry') {
    const r = tgt.row
    return `${r.base}/USDT — ${r._direction} @ ${r._venue}`
  }
  const r = tgt.row
  return `${r.base}/USDT — ${r.direction} fut@${r.futures_venue} spot@${r.spot_venue}`
})

const openScannedNotional = computed(() =>
  openTarget.value?.kind === 'carry'
    ? formatUsd(openTarget.value.row.notional_usd_requested)
    : '—',
)
const openCapitalRequired = computed(() =>
  openTarget.value?.kind === 'carry'
    ? formatUsd(openTarget.value.row.capital_required_usd)
    : '—',
)

function venueTradeBlock(...venueIds: string[]): string {
  for (const vid of venueIds) {
    const cap = venueCaps.value[vid]
    if (!cap) return `${vid}: venue trading capability is unavailable`
    if (!cap.trade) return `${vid}: scan-only${cap.reason ? ` (${cap.reason})` : ''}`
  }
  return ''
}

function carryCandidateIdentity(row: CarryCand, venue: string): string {
  const symbol = row.symbol || `${row.base}USDT`
  const snapshotIds = [
    row.snapshot_id,
    row.spot_book_snapshot_id,
    row.perp_book_snapshot_id,
    row.spot_instrument_snapshot_id,
    row.perp_instrument_snapshot_id,
  ].map((id) => id == null ? '' : String(id)).join(':')
  return `${venue}:${symbol}:${row.snapshot_ts_ms ?? 'missing-time'}:${snapshotIds}`
}

function snapshotIdentity(row: CarryRow): string {
  const ids: Array<readonly [string, string | number | null | undefined]> = [
    ['candidate', row.snapshot_id],
    ['spot book', row.spot_book_snapshot_id],
    ['perp book', row.perp_book_snapshot_id],
    ['spot instrument', row.spot_instrument_snapshot_id],
    ['perp instrument', row.perp_instrument_snapshot_id],
  ]
  const availableIds = ids.filter(([, id]) => id !== null && id !== undefined && id !== '')
  if (availableIds.length > 0) return availableIds.map(([label, id]) => `${label}=${String(id)}`).join(' · ')
  return `${row._venue}:${row.symbol || `${row.base}USDT`}:${row.snapshot_ts_ms ?? '—'}`
}

function carryCandidateBlock(row: CarryRow): string {
  if (!carryScanInputsValid.value) return t('scanner.carryInvalidInputs')
  const venue = carryData.value.find((item) => item.venue === row._venue)
  if (!venue || !carryDataMatchesInputs(
    carryData.value,
    Number(carryNotionalUsd.value),
    Number(carryHorizonHours.value),
  )) return t('scanner.carryRescanRequired')
  if (row._direction !== 'forward' || row.direction !== 'forward') {
    return t('scanner.paperCarryOnly')
  }
  if (row.venue !== row._venue || row.symbol !== `${row.base}USDT`) {
    return t('scanner.carryCandidateMismatch')
  }
  if (!closeEnough(venue.notional_usd, Number(carryNotionalUsd.value))
    || !closeEnough(venue.horizon_hours, Number(carryHorizonHours.value))
    || !closeEnough(row.notional_usd_requested, Number(carryNotionalUsd.value))
    || !closeEnough(row.horizon_hours, Number(carryHorizonHours.value))) {
    return t('scanner.carryRescanRequired')
  }

  const rows = venue.forward ?? []
  const current = rows.find((candidate) =>
    carryCandidateIdentity(candidate, venue.venue) === carryCandidateIdentity(row, venue.venue),
  )
  if (!current) return t('scanner.carryCandidateMismatch')
  const snapshotTs = positiveNumber(row.snapshot_ts_ms)
  const snapshotObservedTs = positiveNumber(row.snapshot_observed_at_ms)
  const fundingObservedTs = positiveNumber(row.funding_observed_at_ms)
  const spotBookTs = positiveNumber(row.spot_book_observed_at_ms)
  const perpBookTs = positiveNumber(row.perp_book_observed_at_ms)
  const spotInstrumentTs = positiveNumber(row.spot_instrument_snapshot_observed_at_ms)
  const perpInstrumentTs = positiveNumber(row.perp_instrument_snapshot_observed_at_ms)
  const spotSourceTs = positiveNumber(row.spot_book_source_ts_ms)
  const perpSourceTs = positiveNumber(row.perp_book_source_ts_ms)
  const historyTs = positiveNumber(row.history_latest_ts)
  const intervalHours = positiveNumber(row.interval_h)
  const maxHistoryGapIntervals = Math.min(
    positiveNumber(venue.assumptions?.maximum_history_gap_intervals)
      ?? MAX_CARRY_HISTORY_GAP_INTERVALS,
    MAX_CARRY_HISTORY_GAP_INTERVALS,
  )
  const sampleCount = row.history_samples
  if (!snapshotTs || !snapshotObservedTs || !fundingObservedTs
    || !spotBookTs || !perpBookTs || !spotInstrumentTs || !perpInstrumentTs
    || !spotSourceTs || !perpSourceTs || !historyTs || !intervalHours
    || !isFiniteNumber(sampleCount) || sampleCount < MIN_CARRY_HISTORY_SAMPLES
    || !isFiniteNumber(row.source_timestamp_skew_ms)
    || row.snapshot_estimate !== true) {
    return t('scanner.carryCandidateMismatch')
  }

  const now = currentTimeMs.value
  const snapshotAge = now - snapshotTs
  const fundingMaxAgeMs = (positiveNumber(venue.assumptions?.maximum_funding_snapshot_age_sec)
    ?? MAX_CARRY_BOOK_AGE_MS / 1000) * 1000
  const bookMaxAgeMs = (positiveNumber(venue.assumptions?.maximum_order_book_age_sec)
    ?? MAX_CARRY_BOOK_AGE_MS / 1000) * 1000
  const sourceSkewMaxMs = (positiveNumber(venue.assumptions?.maximum_source_timestamp_skew_sec)
    ?? MAX_CARRY_SOURCE_SKEW_MS / 1000) * 1000
  const instrumentMaxAgeMs = (positiveNumber(venue.assumptions?.maximum_instrument_snapshot_age_sec)
    ?? MAX_CARRY_INSTRUMENT_AGE_MS / 1000) * 1000
  const timestamps = [snapshotObservedTs, fundingObservedTs, spotBookTs, perpBookTs, spotInstrumentTs, perpInstrumentTs]
  if (snapshotAge < -MAX_CARRY_FUTURE_SKEW_MS || snapshotAge > MAX_CARRY_SNAPSHOT_AGE_MS
    || timestamps.some((stamp) => stamp - now > MAX_CARRY_FUTURE_SKEW_MS)
    || now - snapshotObservedTs > MAX_CARRY_SNAPSHOT_AGE_MS
    || now - fundingObservedTs > fundingMaxAgeMs
    || [spotInstrumentTs, perpInstrumentTs].some((stamp) => now - stamp > instrumentMaxAgeMs)) {
    return t('scanner.carryCandidateStale')
  }
  if (Math.abs(spotBookTs - perpBookTs) > MAX_CARRY_BOOK_SKEW_MS
    || [spotBookTs, perpBookTs].some((stamp) =>
      now - stamp > bookMaxAgeMs || stamp - now > MAX_CARRY_FUTURE_SKEW_MS,
    )) {
    return t('scanner.carryCandidateStale')
  }
  if ([spotSourceTs, perpSourceTs].some((stamp) =>
    now - stamp > bookMaxAgeMs || stamp - now > MAX_CARRY_FUTURE_SKEW_MS,
  )) return t('scanner.carryCandidateStale')
  const sourceTimestamps = [fundingObservedTs, spotSourceTs, perpSourceTs]
  const measuredSourceSkew = Math.max(...sourceTimestamps) - Math.min(...sourceTimestamps)
  if (row.source_timestamp_skew_ms < 0
    || row.source_timestamp_skew_ms > sourceSkewMaxMs
    || Math.abs(measuredSourceSkew - row.source_timestamp_skew_ms) > 1_000) {
    return t('scanner.carryCandidateStale')
  }
  // Compare the latest settled sample to the scan observation, not to the next
  // scheduled settlement: a valid scan may happen before that settlement.
  if (historyTs - snapshotTs > MAX_CARRY_FUTURE_SKEW_MS
    || snapshotTs - historyTs > intervalHours * maxHistoryGapIntervals * 3_600_000) {
    return t('scanner.carryCandidateStale')
  }

  const requiredMetrics: Array<keyof CarryCand> = [
    'gross_funding_usd',
    'total_estimated_cost_usd',
    'net_horizon_earnings_usd',
    'net_horizon_roi_pct',
    'capital_required_usd',
    'breakeven_funding_payments',
    'breakeven_hours_from_snapshot',
  ]
  if (requiredMetrics.some((field) => !isFiniteNumber(row[field]) || !isFiniteNumber(current[field])
    || !closeEnough(row[field], current[field] as number))) {
    return t('scanner.carryRescanRequired')
  }
  const snapshotFields: Array<keyof CarryCand> = [
    'snapshot_ts_ms',
    'snapshot_observed_at_ms',
    'funding_observed_at_ms',
    'history_first_ts',
    'history_latest_ts',
    'history_samples',
    'next_funding_ts',
    'interval_h',
    'funding_payments_estimated',
    'spot_book_observed_at_ms',
    'perp_book_observed_at_ms',
    'spot_book_source_ts_ms',
    'perp_book_source_ts_ms',
    'spot_book_request_started_at_ms',
    'perp_book_request_started_at_ms',
    'spot_instrument_snapshot_observed_at_ms',
    'perp_instrument_snapshot_observed_at_ms',
    'source_timestamp_skew_ms',
  ]
  if (snapshotFields.some((field) => row[field] !== current[field]
    && !closeEnough(row[field], current[field]))) {
    return t('scanner.carryCandidateMismatch')
  }
  if (carryCandidateIdentity(row, row._venue) !== carryCandidateIdentity(current, venue.venue)) {
    return t('scanner.carryCandidateMismatch')
  }
  return ''
}

function paperOpenBlock(target: OpenTarget): string {
  if (target.kind !== 'carry') {
    return t('scanner.paperCarryOnly')
  }
  if (!hasApiToken.value || !getApiToken()) return t('scanner.authRequired')
  if (target.row._direction !== 'forward') {
    return t('scanner.paperCarryOnly')
  }
  if (!(SUPPORTED_CEX_UI_VENUES as readonly string[]).includes(target.row._venue)) {
    return t('scanner.paperCarryOnly')
  }
  const candidateBlock = carryCandidateBlock(target.row)
  if (candidateBlock) return candidateBlock
  return venueTradeBlock(target.row._venue)
}

function showOpenDialog(target: OpenTarget) {
  const block = paperOpenBlock(target)
  if (block) {
    message.warning(block)
    return
  }
  openTarget.value = target
  showOpenModal.value = true
}

async function confirmOpen() {
  const target = openTarget.value
  if (!target) return
  const block = paperOpenBlock(target)
  if (block) {
    message.error(block)
    showOpenModal.value = false
    return
  }
  if (target.kind !== 'carry') return

  opening.value = true
  try {
    const row = target.row
    const scan = carryData.value.find((venue) => venue.venue === row._venue)
    if (!scan?.snapshot_id || !row.snapshot_id) {
      throw new Error(t('scanner.carryCandidateMismatch'))
    }
    await openCarryPaperPosition({
      strategy: 'carry',
      base: row.base,
      symbol: row.symbol,
      futures_venue: row._venue,
      spot_venue: row._venue,
      amount_usd: Number(row.notional_usd_requested),
      horizon_hours: Number(row.horizon_hours),
      direction: 'forward',
      dry_run: true,
      scan_snapshot_id: scan.snapshot_id,
      candidate_snapshot_id: row.snapshot_id,
    })
    message.success(t('scanner.opened', { base: row.base, mode: 'paper' }))
    showOpenModal.value = false
  } catch (e) {
    message.error(e instanceof Error ? e.message : t('scanner.failedToOpen'))
  } finally {
    opening.value = false
  }
}


// ---- Pure Futures ----
type BasisRiskLevel = 'clean' | 'caution' | 'high'
interface PureRow { base: string; direction: string; long_venue: string; short_venue: string; net_edge_pct: number; mark_spread_pct: number; real_edge_pct: number; annual_apy_pct: number; net_apy_pct: number; long_interval_h: number; short_interval_h: number; settle_mismatch: boolean; basis_risk_level: BasisRiskLevel }

function toPureRow(i: import('@/composables/useApi').OpportunityItem, direction: string): PureRow {
  return {
    base: i.base, direction, long_venue: i.long_venue, short_venue: i.short_venue,
    net_edge_pct: i.net_edge_pct ?? 0, mark_spread_pct: i.mark_spread_pct ?? 0,
    real_edge_pct: i.real_edge_pct ?? ((i.net_edge_pct ?? 0) - (i.mark_spread_pct ?? 0)), annual_apy_pct: i.annual_apy_pct ?? 0,
    net_apy_pct: i.net_apy_pct ?? 0,
    long_interval_h: i.long_interval_h ?? 8, short_interval_h: i.short_interval_h ?? 8,
    settle_mismatch: i.settle_mismatch ?? (i.same_interval === false),
    basis_risk_level: inferBasisRiskLevel(i),
  }
}

function inferBasisRiskLevel(i: { basis_risk_level?: BasisRiskLevel; real_edge_pct?: number; net_edge_pct?: number; mark_spread_pct?: number }): BasisRiskLevel {
  if (i.basis_risk_level === 'clean' || i.basis_risk_level === 'caution' || i.basis_risk_level === 'high') {
    return i.basis_risk_level
  }
  const real = i.real_edge_pct ?? ((i.net_edge_pct ?? 0) - (i.mark_spread_pct ?? 0))
  if (real >= 0) return real > 0.05 ? 'clean' : 'caution'
  return 'high'
}

function basisRiskTag(level: BasisRiskLevel) {
  if (level === 'clean') {
    return h(NTooltip, { trigger: 'hover' }, {
      trigger: () => h(NTag, { size: 'small', type: 'success', bordered: false }, { default: () => t('scanner.riskClean') }),
      default: () => t('scanner.riskCleanTip'),
    })
  }
  if (level === 'caution') {
    return h(NTooltip, { trigger: 'hover' }, {
      trigger: () => h(NTag, { size: 'small', type: 'warning', bordered: false }, { default: () => t('scanner.riskCaution') }),
      default: () => t('scanner.riskCautionTip'),
    })
  }
  return h(NTooltip, { trigger: 'hover' }, {
    trigger: () => h(NTag, { size: 'small', type: 'error', bordered: false }, { default: () => t('scanner.riskHigh') }),
    default: () => t('scanner.riskHighTip'),
  })
}

function pureRowRiskHint(row: PureRow): string {
  if (row.basis_risk_level === 'high') return t('scanner.riskHighTip')
  if (row.basis_risk_level === 'caution') return t('scanner.riskCautionTip')
  return ''
}

const pureRows = computed<PureRow[]>(() => {
  const d = pureData.value
  if (!d) return []
  // Rows are already scoped to the venues used in the last scan — only apply UI filters here.
  // In demo mode the snapshot contains all scanned venues, so we additionally
  // filter by the user's venue selection client-side (no backend to rescan).
  let all = [
    ...(d.forward || []).map((i) => toPureRow(i, 'Forward')),
    ...(d.reverse || []).map((i) => toPureRow(i, 'Reverse')),
  ]
  if (isDemoMode && selectedVenues.value.length > 0) {
    const sel = new Set(selectedVenues.value)
    all = all.filter((r) => sel.has(r.long_venue) && sel.has(r.short_venue))
  }
  if (minEdgeFilter.value > 0) all = all.filter((r) => r.real_edge_pct >= minEdgeFilter.value)
  if (intervalFilter.value === 'same') all = all.filter((r) => !r.settle_mismatch)
  else if (intervalFilter.value === 'cross') all = all.filter((r) => r.settle_mismatch)
  return all
})

function fmtInterval(h: number): string {
  return h >= 1 ? `${Math.round(h)}h` : `${Math.round(h * 60)}m`
}

function renderVenue(venue: string) {
  if (!DEX_VENUES.has(venue)) return venue
  return h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '4px' } }, [
    venue,
    h(NTag, { size: 'tiny', type: 'info', bordered: false }, { default: () => 'DEX' }),
  ])
}

const genuine = computed(() => pureRows.value.filter((r) => r.real_edge_pct > 0).length)
const bestReal = computed(() => pureRows.value.length > 0 ? Math.max(...pureRows.value.map((r) => r.real_edge_pct)) : 0)
const pureStatCards = computed(() => [
  { label: t('scanner.scannedPairs'), value: cacheMatchesSelection('pure') ? (pureData.value?.total_assets_scanned ?? 0) : '—', icon: SearchOutline, color: '#2080f0' },
  { label: t('scanner.opportunities'), value: pureRows.value.length, icon: FlashOutline, color: '#18a058' },
  { label: t('scanner.genuineArb'), value: genuine.value, icon: TrendingUpOutline, color: genuine.value > 0 ? '#18a058' : '#d03050' },
  { label: t('scanner.bestRealEdge'), value: bestReal.value.toFixed(4) + '%', icon: AnalyticsOutline, color: bestReal.value > 0 ? '#18a058' : '#d03050' },
])

const pureColumns = computed<DataTableColumns<PureRow>>(() => [
  { title: t('scanner.pair'), key: 'base', width: 90, render: (row) => `${row.base}/USDT` },
  { title: t('scanner.dir'), key: 'direction', width: 75, render: (row) => h(NTag, { size: 'small', type: row.direction === 'Forward' ? 'success' : 'warning', bordered: false }, { default: () => row.direction }) },
  { title: t('scanner.long'), key: 'long_venue', width: 105, render: (row) => renderVenue(row.long_venue) },
  { title: t('scanner.short'), key: 'short_venue', width: 105, render: (row) => renderVenue(row.short_venue) },
  { title: t('scanner.interval'), key: 'interval', width: 90, render: (row) => {
    const label = `${fmtInterval(row.long_interval_h)}/${fmtInterval(row.short_interval_h)}`
    return h(NTag, { size: 'small', type: row.settle_mismatch ? 'warning' : 'default', bordered: false },
      { default: () => row.settle_mismatch ? `⚠ ${label}` : label })
  } },
  { title: colTitle('scanner.fundingEdge', 'scanner.fundingEdgeTip', '/docs/fees-and-edge#fe-edges'), key: 'net_edge_pct', width: 105, sorter: (a, b) => a.net_edge_pct - b.net_edge_pct,
    render: (row) => h(NText, { type: row.net_edge_pct > 0 ? 'success' : 'error', strong: true }, { default: () => row.net_edge_pct.toFixed(4) + '%' }) },
  { title: colTitle('scanner.markSpread', 'scanner.markSpreadTip', '/docs/pure-futures#pf-mechanics'), key: 'mark_spread_pct', width: 105, sorter: (a, b) => a.mark_spread_pct - b.mark_spread_pct,
    render: (row) => { const v = row.mark_spread_pct; const c = v > row.net_edge_pct ? '#d03050' : v > row.net_edge_pct * 0.5 ? '#f0a020' : undefined; return h('span', { style: { color: c } }, v.toFixed(4) + '%') } },
  { title: colTitle('scanner.realEdge', 'scanner.realEdgeTip', '/docs/fees-and-edge#fe-edges'), key: 'real_edge_pct', width: 105, sorter: (a, b) => a.real_edge_pct - b.real_edge_pct, defaultSortOrder: 'descend',
    render: (row) => h(NText, { type: row.real_edge_pct > 0.05 ? 'success' : row.real_edge_pct > 0 ? 'warning' : 'error', strong: true }, { default: () => (row.real_edge_pct > 0 ? '+' : '') + row.real_edge_pct.toFixed(4) + '%' }) },
  { title: colTitle('scanner.basisRisk', 'scanner.basisRiskTip', '/docs/fees-and-edge#fe-edges'), key: 'basis_risk_level', width: 100,
    render: (row) => basisRiskTag(row.basis_risk_level) },
  { title: t('scanner.apy'), key: 'annual_apy_pct', width: 75, sorter: (a, b) => a.annual_apy_pct - b.annual_apy_pct,
    render: (row) => h(NText, { strong: true }, { default: () => row.annual_apy_pct.toFixed(0) + '%' }) },
  { title: colTitle('scanner.netApy', 'scanner.netApyTip'), key: 'net_apy_pct', width: 90, sorter: (a, b) => a.net_apy_pct - b.net_apy_pct,
    render: (row) => h(NText, { type: row.net_apy_pct > 0 ? 'success' : 'error' }, { default: () => row.net_apy_pct.toFixed(0) + '%' }) },
  { title: t('scanner.action'), key: 'actions', width: 80,
    render: (row) => {
      const block = paperOpenBlock({ kind: 'pure', row })
      const riskHint = pureRowRiskHint(row)
      return h(NButton, {
        size: 'tiny', type: 'primary', secondary: true,
        disabled: !!block,
        title: block || riskHint || undefined,
        onClick: block ? undefined : () => showOpenDialog({ kind: 'pure', row }),
      }, { default: () => t('scanner.open') })
    } },
])

// ---- Cash & Carry ----
type CarryRow = CarryCand & { _venue: string; _direction: 'forward' | 'reverse' }

function carryRowsForVenue(ven: CarryVenue): CarryRow[] {
  return [
    ...(ven.forward || []).map((r) => ({ ...r, _venue: ven.venue, _direction: 'forward' as const })),
    ...(ven.reverse || []).map((r) => ({ ...r, _venue: ven.venue, _direction: 'reverse' as const })),
  ]
}

function formatUsd(value: number | null | undefined): string {
  if (!isFiniteNumber(value)) return '—'
  const amount = Math.abs(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })
  return `${value < 0 ? '-$' : '$'}${amount}`
}

function formatPercent(value: number | null | undefined): string {
  return isFiniteNumber(value) ? `${value.toFixed(4)}%` : '—'
}

function formatTimestamp(value: number | null | undefined): string {
  const timestamp = positiveNumber(value)
  if (timestamp === null) return '—'
  try {
    return new Date(timestamp).toISOString().replace('T', ' ').replace('Z', ' UTC')
  } catch {
    return '—'
  }
}

function carryRoi(row: CarryRow): { oneLeg: number | null; capital: number | null } {
  const net = isFiniteNumber(row.net_horizon_earnings_usd) ? row.net_horizon_earnings_usd : null
  const oneLeg = isFiniteNumber(row.net_horizon_notional_roi_pct)
    ? row.net_horizon_notional_roi_pct
    : isFiniteNumber(row.net_horizon_roi_pct)
      ? row.net_horizon_roi_pct
    : net !== null && positiveNumber(row.notional_usd_requested)
      ? net / Number(row.notional_usd_requested) * 100
      : null
  const capital = isFiniteNumber(row.net_horizon_capital_roi_pct)
    ? row.net_horizon_capital_roi_pct
    : net !== null && positiveNumber(row.capital_required_usd)
      ? net / Number(row.capital_required_usd) * 100
      : null
  return { oneLeg, capital }
}

function carryCostAssumptions(row: CarryRow): string {
  const spotFee = isFiniteNumber(row.spot_fee_pct) ? row.spot_fee_pct : null
  const perpFee = isFiniteNumber(row.perp_fee_pct) ? row.perp_fee_pct : row.futures_fee_pct ?? null
  return [
    `${t('scanner.carryEntryFees')}: ${formatUsd(row.entry_fee_usd)} (spot ${formatPercent(spotFee)}, perp ${formatPercent(perpFee)})`,
    `${t('scanner.carryExitFees')}: ${formatUsd(row.exit_fee_usd)}`,
    `${t('scanner.carryBookCost')}: ${formatUsd(row.observed_round_trip_book_cost_usd)}`,
    `${t('scanner.carryExitSlippage')}: ${formatUsd(row.exit_slippage_assumption_usd)} (${row.exit_slippage_bps_per_leg ?? '—'} bps/leg)`,
    `${t('scanner.carryBasisBuffer')}: ${formatUsd(row.basis_buffer_usd)} (${row.basis_buffer_bps ?? '—'} bps)`,
  ].join('\n')
}

function carrySnapshotCell(row: CarryRow) {
  return h('div', { class: 'carry-snapshot-cell' }, [
    h('strong', { title: snapshotIdentity(row) }, `${t('scanner.carrySnapshotIdentity')}: ${snapshotIdentity(row)}`),
    h('small', `${t('scanner.carrySnapshotAt')}: ${formatTimestamp(row.snapshot_ts_ms)}`),
    h('small', `${t('scanner.carrySnapshotObservedAt')}: ${formatTimestamp(row.snapshot_observed_at_ms)}`),
    h('small', `${t('scanner.carryFundingObservedAt')}: ${formatTimestamp(row.funding_observed_at_ms)}`),
    h('small', `${t('scanner.carrySpotBookAt')}: ${formatTimestamp(row.spot_book_observed_at_ms)}`),
    h('small', `${t('scanner.carryPerpBookAt')}: ${formatTimestamp(row.perp_book_observed_at_ms)}`),
    h('small', `${t('scanner.carrySpotInstrumentAt')}: ${formatTimestamp(row.spot_instrument_snapshot_observed_at_ms)}`),
    h('small', `${t('scanner.carryPerpInstrumentAt')}: ${formatTimestamp(row.perp_instrument_snapshot_observed_at_ms)}`),
    h('small', `${t('scanner.carryHistoryRange')}: ${formatTimestamp(row.history_first_ts)}`),
    h('small', `${t('scanner.carryHistoryAt')}: ${formatTimestamp(row.history_latest_ts)}`),
    h('small', `${t('scanner.carrySourceSkew')}: ${row.source_timestamp_skew_ms ?? '—'} ms`),
  ])
}

function carryCostCell(row: CarryRow) {
  return h(NTooltip, { trigger: 'hover' }, {
    trigger: () => h('span', { class: 'carry-cost-value', tabindex: 0 }, formatUsd(row.total_estimated_cost_usd)),
    default: () => h('pre', { class: 'carry-cost-breakdown' }, carryCostAssumptions(row)),
  })
}

function carryRoiCell(row: CarryRow) {
  const roi = carryRoi(row)
  return h(NTooltip, { trigger: 'hover' }, {
    trigger: () => h('div', { class: 'carry-roi-cell' }, [
      h('small', t('scanner.oneLegNotionalRoi')),
      h('strong', formatPercent(roi.oneLeg)),
      h('small', t('scanner.illustrativeCapitalRoi')),
      h('strong', formatPercent(roi.capital)),
    ]),
    default: () => h('span', t('scanner.capitalRoiAssumption', {
      capital: formatUsd(row.capital_required_usd),
    })),
  })
}

// In demo mode, hide venues the user has deselected (the snapshot holds every
// scanned venue; client-side filtering is the only way to narrow the view). In
// live mode the backend rescans only the selected venues, so the data is
// already scoped and we pass it through unchanged.
const carryVenues = computed(() => {
  if (!isDemoMode || selectedVenues.value.length === 0) return carryData.value
  const sel = new Set(selectedVenues.value)
  return carryData.value.filter((v) => sel.has(v.venue))
})
const carryScanMatchesInputs = computed(() => carryScanInputsValid.value && carryDataMatchesInputs(
  carryData.value,
  Number(carryNotionalUsd.value),
  Number(carryHorizonHours.value),
))
const carryRequiresRescan = computed(() => {
  if (!carryData.value.length || !carryScanMatchesInputs.value) return carryData.value.length > 0
  return carryVenues.value.some((venue) =>
    (venue.forward ?? []).some((candidate) =>
      !!carryCandidateBlock({ ...candidate, _venue: venue.venue, _direction: 'forward' }),
    ),
  )
})
const carryTotalFwd = computed(() => carryVenues.value.reduce((s, v) => s + (v.forward?.length ?? 0), 0))
const carryTotalRev = computed(() => carryVenues.value.reduce((s, v) => s + (v.reverse?.length ?? 0), 0))
const carryStatCards = computed(() => [
  { label: t('scanner.venuesScanned'), value: carryVenues.value.length, icon: SearchOutline, color: '#2080f0' },
  { label: t('scanner.forwardSpotPerp'), value: carryTotalFwd.value, icon: TrendingUpOutline, color: '#18a058' },
  { label: t('scanner.reverseBorrowPerp'), value: carryTotalRev.value, icon: FlashOutline, color: '#f0a020' },
  { label: t('scanner.strategy'), value: 'Cash & Carry', icon: AnalyticsOutline, color: '#8a2be2' },
])

const carryColumns = computed<DataTableColumns<CarryRow>>(() => [
  { title: t('scanner.pair'), key: 'base', width: 90, render: (row) => `${row.base}/USDT` },
  { title: t('scanner.scannedNotional'), key: 'notional_usd_requested', width: 130,
    render: (row) => h('div', [
      h('strong', formatUsd(row.notional_usd_requested)),
      h('small', `${t('scanner.actualSpotNotional')}: ${formatUsd(row.spot_notional_usd)}`),
    ]) },
  { title: t('scanner.carryGrossUsd'), key: 'gross_funding_usd', width: 120,
    render: (row) => h(NText, { type: 'success', strong: true }, { default: () => formatUsd(row.gross_funding_usd) }) },
  { title: t('scanner.carryCosts'), key: 'total_estimated_cost_usd', width: 130,
    render: (row) => carryCostCell(row) },
  { title: t('scanner.carryNetUsd'), key: 'net_horizon_earnings_usd', width: 120,
    render: (row) => h(NText, { type: (row.net_horizon_earnings_usd ?? 0) >= 0 ? 'success' : 'error', strong: true }, { default: () => formatUsd(row.net_horizon_earnings_usd) }) },
  { title: t('scanner.carryRoi'), key: 'net_horizon_roi_pct', width: 205, render: (row) => carryRoiCell(row) },
  { title: t('scanner.carryBreakeven'), key: 'breakeven_funding_payments', width: 150,
    render: (row) => h('div', [
      h('strong', `${row.breakeven_funding_payments ?? '—'} ${t('scanner.carryPayments')}`),
      h('small', `${row.breakeven_hours_from_snapshot?.toFixed(2) ?? '—'}h · ${formatPercent(row.breakeven_rate_pct)}`),
    ]) },
  { title: t('scanner.rate'), key: 'expected_rate_pct', width: 100,
    render: (row) => formatPercent(row.expected_rate_pct ?? row.rate_pct) },
  { title: t('scanner.carrySnapshot'), key: 'snapshot_ts_ms', width: 300,
    render: (row) => carrySnapshotCell(row) },
  { title: t('scanner.action'), key: 'actions', width: 80,
    render: (row) => {
      const block = paperOpenBlock({ kind: 'carry', row })
      return h(NButton, {
        size: 'tiny', type: 'primary', secondary: true,
        disabled: !!block,
        title: block || undefined,
        onClick: block ? undefined : () => showOpenDialog({ kind: 'carry', row }),
      }, { default: () => t('scanner.open') })
    } },
])

// ---- Unified C&C ----
const unifiedRows = computed(() => {
  // In demo mode the snapshot holds every route; narrow to the selected venues
  // client-side. Unified routes involve two venues (futures + spot), so both
  // must be selected for the route to show.
  if (!isDemoMode || selectedVenues.value.length === 0) return unifiedData.value
  const sel = new Set(selectedVenues.value)
  return unifiedData.value.filter(
    (u) => sel.has(u.futures_venue) && sel.has(u.spot_venue),
  )
})
const unifiedCrossVenue = computed(() => unifiedRows.value.filter((u) => !u.same_venue).length)
const unifiedSameVenue = computed(() => unifiedRows.value.filter((u) => u.same_venue).length)
const unifiedStatCards = computed(() => [
  { label: t('scanner.routes'), value: unifiedRows.value.length, icon: SearchOutline, color: '#2080f0' },
  { label: t('scanner.crossVenue'), value: unifiedCrossVenue.value, icon: FlashOutline, color: '#18a058' },
  { label: t('scanner.sameVenue'), value: unifiedSameVenue.value, icon: AnalyticsOutline, color: '#f0a020' },
  { label: t('scanner.mode'), value: 'Unified', icon: TrendingUpOutline, color: '#8a2be2' },
])

const unifiedColumns = computed<DataTableColumns<UnifiedCarryCand>>(() => [
  { title: t('scanner.pair'), key: 'base', width: 90, render: (row) => `${row.base}/USDT` },
  { title: t('scanner.dir'), key: 'direction', width: 75, render: (row) => h(NTag, { size: 'small', type: row.direction === 'forward' ? 'success' : 'warning', bordered: false }, { default: () => row.direction }) },
  { title: t('scanner.futures'), key: 'futures_venue', width: 90 },
  { title: t('scanner.spot'), key: 'spot_venue', width: 90 },
  { title: t('scanner.funding'), key: 'funding_rate_pct', width: 90, render: (row) => (row.funding_rate_pct ?? 0).toFixed(4) + '%' },
  { title: t('scanner.fee'), key: 'fee_pct', width: 75, render: (row) => (row.fee_pct ?? 0).toFixed(3) + '%' },
  { title: t('scanner.netEdge'), key: 'net_edge_pct', width: 100, sorter: (a, b) => (a.net_edge_pct ?? 0) - (b.net_edge_pct ?? 0), defaultSortOrder: 'descend',
    render: (row) => h(NText, { type: (row.net_edge_pct ?? 0) > 0 ? 'success' : 'error', strong: true }, { default: () => (row.net_edge_pct ?? 0).toFixed(4) + '%' }) },
  { title: t('scanner.annual'), key: 'annual_pct', width: 80, render: (row) => (row.annual_pct ?? 0).toFixed(0) + '%' },
  { title: t('scanner.action'), key: 'actions', width: 80,
    render: (row) => {
      const block = paperOpenBlock({ kind: 'unified', row })
      return h(NButton, {
        size: 'tiny', type: 'primary', secondary: true,
        disabled: !!block,
        title: block || undefined,
        onClick: block ? undefined : () => showOpenDialog({ kind: 'unified', row }),
      }, { default: () => t('scanner.open') })
    } },
])

const unsubscribeApiToken = subscribeApiToken((token) => {
  hasApiToken.value = Boolean(token)
  if (isDemoMode) return
  if (!token) {
    venueCaps.value = {}
    showOpenModal.value = false
    return
  }
  void loadStrategyVenues()
  void loadVenueCapabilities()
  void loadData(strategy.value)
})

let _candidateClockTimer: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  _candidateClockTimer = setInterval(() => {
    currentTimeMs.value = Date.now()
  }, 5000)
  // In demo mode, prime the snapshot first so loadStrategyVenues() /
  // loadVenueCapabilities() have data to read from.
  if (isDemoMode) {
    await useDemoSnapshot().ensure()
  }
  if (isDemoMode || getApiToken()) {
    await loadStrategyVenues()
    loadVenueCapabilities()
    loadData()
  }
  // WebSocket is backend-only — skip in demo mode to avoid the endless
  // reconnect loop against a static host.
  if (!isDemoMode) ws.connect()
})
onUnmounted(() => {
  ws.disconnect()
  unsubscribeApiToken()
  if (_venuesWatchTimer) clearTimeout(_venuesWatchTimer)
  if (_candidateClockTimer) clearInterval(_candidateClockTimer)
})
</script>

<style scoped>
.scanner-page { display: flex; flex-direction: column; gap: 16px; height: 100%; }
.stat-card-inner { display: flex; align-items: center; gap: 16px; }
.stat-icon { width: 48px; height: 48px; border-radius: 10px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
.stat-info { display: flex; flex-direction: column; min-width: 0; flex: 1; overflow: hidden; }
.stat-info :deep(.n-text) { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.table-card { flex: 1; min-height: 0; }
.table-card :deep(.n-card__content) { padding: 16px 20px; }
.table-card :deep(.n-card-header) { padding: 12px 20px; }

/* ---- Filter toolbar layout ---- */
.filter-toolbar {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.toolbar-title-row {
  display: flex;
  justify-content: center;
  align-items: center;
  width: 100%;
}

.toolbar-title {
  font-size: 17px;
  font-weight: 600;
  white-space: nowrap;
  letter-spacing: -0.01em;
}

.toolbar-tabs-row {
  display: flex;
  justify-content: center;
  align-items: center;
  width: 100%;
}

/* 策略 Tab：下划线指示，无实心绿块 */
.strategy-tabs {
  width: auto;
  max-width: 100%;
}
.strategy-tabs :deep(.n-tabs-nav) {
  justify-content: center;
}
.strategy-tabs :deep(.n-tabs-pane-wrapper) {
  display: none;
}
.strategy-tabs :deep(.n-tabs-tab) {
  font-size: 13px;
  font-weight: 500;
  padding: 8px 16px;
}
.strategy-tabs :deep(.n-tabs-tab--active) {
  font-weight: 600;
}

.toolbar-filters-row {
  display: flex;
  width: 100%;
  padding-top: 4px;
  border-top: 1px solid rgba(255, 255, 255, 0.06);
}

.filters-left {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px 14px;
  width: 100%;
}

.actions-inline {
  margin-left: auto;
  flex-shrink: 0;
}

@media (max-width: 900px) {
  .actions-inline {
    margin-left: 0;
    width: 100%;
    justify-content: flex-end;
    padding-top: 4px;
  }
}

.status-tag {
  flex-shrink: 0;
  max-width: 260px;
}
.status-tag :deep(.n-tag__content) {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: 8px;
}

.venue-filter-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.venue-presets {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-shrink: 0;
}

/* 交易所预设：圆角轮廓 Chip，与策略 Tab / 周期下拉区分 */
.preset-chip {
  appearance: none;
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: rgba(255, 255, 255, 0.55);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  padding: 4px 11px;
  line-height: 1.2;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s, background 0.15s;
}
.preset-chip:hover:not(:disabled) {
  border-color: rgba(255, 255, 255, 0.28);
  color: rgba(255, 255, 255, 0.85);
}
.preset-chip.active {
  border-color: rgba(24, 160, 88, 0.55);
  background: rgba(24, 160, 88, 0.1);
  color: #63e2b7;
}
.preset-chip:disabled {
  opacity: 0.35;
  cursor: not-allowed;
}

.filter-label {
  font-size: 11px;
  font-weight: 500;
  color: rgba(255, 255, 255, 0.45);
  white-space: nowrap;
  text-transform: uppercase;
  letter-spacing: 0.4px;
  line-height: 1;
}

/* 用左边框代替竖线分隔符，避免 flex 换行时竖线叠在输入框上 */
.filter-group-bordered {
  padding-left: 10px;
  border-left: 1px solid rgba(255, 255, 255, 0.08);
}

.action-btn {
  font-weight: 500;
  line-height: 1;
  flex-shrink: 0;
}
.action-btn :deep(.n-button) {
  min-height: 32px;
}

/* ---- Venue select ---- */
.venue-filter {
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
.venue-filter:hover {
  border-color: rgba(24, 160, 88, 0.4) !important;
}
.venue-filter :deep(.n-base-selection) {
  border-radius: 6px;
  min-height: 32px;
}
/* Force single-line tags; don't let long names wrap the whole input */
.venue-filter :deep(.n-base-selection-tags) {
  flex-wrap: nowrap;
  overflow: hidden;
  gap: 3px;
}
.venue-filter :deep(.n-base-selection-tag-wrapper) {
  flex-shrink: 0;
}
/* Hide the default tag border, use our own chip */
.venue-filter :deep(.n-tag) {
  border: none !important;
  background: transparent !important;
  padding: 0 !important;
  height: 22px !important;
}

/* Custom compact chip — width adapts to label length */
:deep(.venue-chip) {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 22px;
  padding: 0 4px 0 8px;
  border-radius: 4px;
  background: rgba(24, 160, 88, 0.15);
  border: 1px solid rgba(24, 160, 88, 0.3);
  font-size: 11px;
  line-height: 1;
  color: #18a058;
  white-space: nowrap;
  overflow: visible;
}
:deep(.venue-chip-name) {
  white-space: nowrap;
}
:deep(.venue-chip-name.dex) {
  font-style: italic;
  opacity: 0.9;
}
:deep(.venue-chip-close) {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px;
  height: 14px;
  border-radius: 3px;
  font-size: 14px;
  line-height: 1;
  color: rgba(24, 160, 88, 0.6);
  cursor: pointer;
  transition: all 0.15s ease;
  flex-shrink: 0;
}
:deep(.venue-chip-close:hover) {
  background: rgba(24, 160, 88, 0.2);
  color: #18a058;
}

/* DEX mini tag in dropdown options */
:deep(.dex-mini-tag) {
  display: inline-block;
  padding: 1px 5px;
  font-size: 9px;
  font-weight: 600;
  letter-spacing: 0.3px;
  border-radius: 3px;
  background: rgba(64, 158, 255, 0.15);
  color: #409eff;
  line-height: 1;
}

/* Edge input — width via ch units; show-button=false removes stepper divider */
.edge-input {
  flex-shrink: 0;
}
.edge-input :deep(.n-input__suffix) {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.4);
  padding-left: 2px;
}
.edge-input :deep(.n-input-wrapper) {
  min-height: 32px;
}
.edge-input :deep(.n-input__input-el) {
  min-width: 1.5ch;
  text-align: left;
}

.carry-input { width: 128px; }
:deep(.carry-snapshot-cell) {
  display: flex;
  flex-direction: column;
  gap: 2px;
  font-size: 11px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
:deep(.carry-snapshot-cell small),
:deep(.carry-roi-cell small) {
  color: rgba(255, 255, 255, 0.55);
}
:deep(.carry-roi-cell) {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 2px 8px;
  font-size: 11px;
}
:deep(.carry-cost-value) { cursor: help; text-decoration: underline dotted; }
:deep(.carry-cost-breakdown) {
  margin: 0;
  max-width: 360px;
  white-space: pre-wrap;
  font: inherit;
  line-height: 1.5;
}

/* 周期：下拉选择，与 Tab / Chip 形态区分 */
.interval-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.docs-link {
  font-size: 12px;
  color: #63e2b7;
  text-decoration: none;
  white-space: nowrap;
}
.docs-link:hover {
  text-decoration: underline;
}
.interval-select {
  width: 148px;
}
.interval-select :deep(.n-base-selection) {
  border-radius: 6px;
  min-height: 32px;
}

/* ---- Dropdown panel ---- */
:deep(.n-base-select-menu) {
  border-radius: 10px;
  margin-top: 4px;
  border: 1px solid rgba(255, 255, 255, 0.08);
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.4);
  overflow: hidden;
}
:deep(.n-base-select-option) {
  border-radius: 6px;
  margin: 2px 4px;
  padding: 8px 10px;
  font-size: 13px;
  transition: background-color 0.15s ease;
}
:deep(.n-base-select-option:hover) {
  background-color: rgba(24, 160, 88, 0.08);
}
:deep(.n-base-select-option--selected) {
  background-color: rgba(24, 160, 88, 0.15) !important;
  color: #18a058 !important;
  font-weight: 500;
}

/* Section divider inside the dropdown */
.venue-section-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: rgba(255, 255, 255, 0.35);
  font-weight: 600;
  padding: 4px 10px 2px;
  pointer-events: none;
}

.col-title-tip {
  cursor: help;
  border-bottom: 1px dashed rgba(255, 255, 255, 0.25);
}
</style>
