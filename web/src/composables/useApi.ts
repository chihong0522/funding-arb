import type { Ref } from "vue";
import { ref, onUnmounted } from "vue";
import {
  apiFetch,
  getApiToken,
  getWebSocketProtocols,
  reportApiAuthFailure,
  subscribeApiToken,
} from "@/composables/apiAuth";
import {
  isDemoMode,
  resolveDemoRoute,
  useDemoSnapshot,
} from "@/composables/useDemoSnapshot";

const API_BASE = "/api";

// In demo mode, prime the snapshot cache on module load so the very first
// `useApi(...)` call already has data to resolve against.
if (isDemoMode) {
  useDemoSnapshot()
    .ensure()
    .catch(() => {
      /* surfaced via useDemoSnapshot().error */
    });
}

// ─── Types (aligned with backend) ───────────────────────────────────

export interface ScannerStatus {
  scanning: boolean;
  last_scan_time: string | null;
  has_data: boolean;
  live: boolean;
}

export interface OpportunityItem {
  base: string;
  direction?: string;
  long_venue: string;
  short_venue: string;
  long_rate_pct: number;
  short_rate_pct: number;
  spread_pct: number;
  net_edge_pct: number;
  real_edge_pct?: number;
  annual_apy_pct?: number;
  net_apy_pct?: number;
  long_symbol?: string;
  short_symbol?: string;
  fee_pct?: number;
  round_trip_fee_pct?: number;
  long_mark?: number;
  short_mark?: number;
  mark_spread_pct?: number;
  settle_mismatch?: boolean;
  same_interval?: boolean;
  long_interval_h?: number;
  short_interval_h?: number;
  /** clean | caution | high — mark/basis risk vs strategy real-edge bar */
  basis_risk_level?: "clean" | "caution" | "high";
}

export interface ScannerOpportunities {
  venues: string[];
  total_assets_scanned: number;
  total_spreads_found: number;
  forward: OpportunityItem[];
  reverse: OpportunityItem[];
  venue_pair_stats: Array<{ pair: string; count: number }>;
  timestamp: string;
}

export interface PositionItem {
  id: string;
  base: string;
  direction: string;
  long_venue: string;
  short_venue: string;
  status: string;
  // Real data fields
  qty?: number;
  long_price?: number;
  short_price?: number;
  trade_usd?: number;
  amount_usd?: number; // legacy field name
  pnl_usd?: number;
  unrealized_pnl_usd?: number;
  mark_spread_pct?: number;
  open_spread_pct?: number; // legacy field name
  open_edge_pct?: number; // legacy field name
  opened_at?: number; // ms timestamp
  open_time?: string; // legacy ISO string
  closed_at?: number;
  dry_run?: boolean;
  strategy?: string;
  quote?: string;
  long_symbol?: string;
  short_symbol?: string;
  // ─── Detailed metrics (added for enhanced positions view) ──────
  /** Close information from executor (close prices, spreads) */
  close_info?: {
    long_price?: number;
    short_price?: number;
    futures_price?: number;
    spot_price?: number;
    open_mark_spread?: number;
    close_mark_spread?: number;
    dry_run?: boolean;
    [key: string]: unknown;
  };
  /** Long leg fill quantity (pure futures) */
  long_qty?: number;
  /** Short leg fill quantity (pure futures) */
  short_qty?: number;
  /** Futures venue (carry/unified strategies) */
  futures_venue?: string;
  /** Spot venue (carry/unified strategies) */
  spot_venue?: string;
  /** Futures open price (carry/unified) */
  futures_price?: number;
  /** Spot open price (carry/unified) */
  spot_price?: number;
  /** Whether legs were opened in parallel */
  parallel_legs?: boolean;
}

export interface BacktestSummary {
  total_pnl_usd: number;
  total_pnl_pct: number;
  annualized_pct: number;
  max_drawdown_pct: number;
  sharpe: number;
  win_rate: number;
  total_trades: number;
  avg_hold_days: number;
}

export interface BacktestTrade {
  base: string;
  direction: string;
  long_venue: string;
  short_venue: string;
  open_time: string;
  close_time: string;
  hold_days: number;
  pnl_usd: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  open_pairs?: number;
  capital_free?: number;
}

export interface BacktestResult {
  id: string;
  params: Record<string, any>;
  summary: BacktestSummary;
  trades: BacktestTrade[];
  equity_curve?: EquityPoint[];
  run_time: string;
  live: boolean;
}

export interface VenueConfig {
  id: string;
  name: string;
  type: string;
  configured: boolean;
  missing_keys: string[];
  status: string;
  scan_capable?: boolean;
  trade_capable?: boolean;
  trade_reason?: string;
  live_ready?: boolean;
  live_reason?: string;
}

export interface StrategyParams {
  min_spread_annual: number;
  min_edge_annual: number;
  max_mark_spread_pct: number;
  trade_usd: number;
  max_positions: number;
  scan_interval_sec: number;
  scan_venues?: string[];
  min_edge_1h?: number;
  min_edge_mismatch?: number;
  fee_mode?: "auto" | "api" | "vip_tier";
  venue_fee_tiers?: Record<string, string>;
}

export interface FeeTierOption {
  id: string;
  label: string;
  spot_taker_pct: number;
  futures_taker_pct: number;
}

export interface ResolvedVenueFee {
  has_credentials: boolean;
  uses_api: boolean;
  tier: string | null;
  spot_taker_pct: number;
  futures_taker_pct: number;
  spot_source: "api" | "tier" | "default";
  futures_source: "api" | "tier" | "default";
}

export interface ResolvedFees {
  fee_mode: string;
  venue_fee_tiers: Record<string, string>;
  venues: Record<string, ResolvedVenueFee>;
}

export interface CredentialsStatus {
  backends: Record<
    string,
    {
      available: boolean;
      description: string;
      path?: string;
    }
  >;
  venues_configured: string[];
  venues_missing: string[];
}

export interface ApiResponse<T> {
  data: Ref<T | null>;
  error: Ref<string | null>;
  loading: Ref<boolean>;
  refresh: () => Promise<void>;
}

// ─── Request helpers ────────────────────────────────────────────────

async function request<T>(url: string): Promise<T> {
  // Demo mode: short-circuit known GET paths against the snapshot cache.
  // Falls through for unknown paths (POST endpoints, write APIs, etc.) so
  // they fail loudly against the static host rather than silently stubbing.
  if (isDemoMode) {
    // Make sure the snapshot is loaded (no-op if already cached).
    await useDemoSnapshot().ensure();
    const demoData = resolveDemoRoute(url);
    if (demoData !== undefined) {
      return demoData as T;
    }
  }
  const response = await apiFetch(`${API_BASE}${url}`);
  if (!response.ok) {
    throw new Error(`API ${response.status}: ${response.statusText}`);
  }
  const json = await response.json();
  if (json && typeof json === "object" && "success" in json && "data" in json) {
    if (!json.success) {
      throw new Error(
        json.error || json.message || "API returned success=false",
      );
    }
    return json.data as T;
  }
  return json as T;
}

export async function post<T>(
  url: string,
  body: Record<string, any>,
): Promise<T> {
  const response = await apiFetch(`${API_BASE}${url}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const responseError = await response.json().catch(() => null);
    const detail = responseError && typeof responseError === "object"
      ? responseError.detail || responseError.error || responseError.message
      : null;
    throw new Error(
      typeof detail === "string"
        ? detail
        : `API ${response.status}: ${response.statusText}`,
    );
  }
  const json = await response.json();
  if (json && typeof json === "object" && "success" in json && "data" in json) {
    if (!json.success) {
      throw new Error(
        json.error || json.message || "API returned success=false",
      );
    }
    return json.data as T;
  }
  return json as T;
}

// ─── Composable ─────────────────────────────────────────────────────

function useApi<T>(url: string, initialData: T | null = null): ApiResponse<T> {
  const data = ref<T | null>(initialData) as Ref<T | null>;
  const error = ref<string | null>(null);
  const loading = ref(false);

  async function refresh() {
    loading.value = true;
    error.value = null;
    try {
      const result = await request<T>(url);
      if (result !== null && result !== undefined) {
        data.value = result;
      }
    } catch (e) {
      error.value = e instanceof Error ? e.message : "Unknown error";
    } finally {
      loading.value = false;
    }
  }

  const unsubscribeToken = subscribeApiToken((token) => {
    if (token && !isDemoMode) void refresh();
  });
  onUnmounted(unsubscribeToken);

  return { data, error, loading, refresh };
}

// ─── Composables for each API endpoint ──────────────────────────────

export function getScannerStatus(strategy: string = "pure") {
  return useApi<ScannerStatus>(`/scanner/status?strategy=${strategy}`);
}

export function getScannerOpportunities(strategy: string = "pure") {
  return useApi<ScannerOpportunities>(
    `/scanner/opportunities?strategy=${strategy}`,
  );
}

export function getPositions() {
  return useApi<PositionItem[]>("/positions");
}

export function getBacktestHistory() {
  return useApi<BacktestResult[]>("/backtest/history");
}

export function getVenues() {
  return useApi<VenueConfig[]>("/settings/venues");
}

export function getCredentialsStatus() {
  return useApi<CredentialsStatus>("/settings/credentials/status");
}

export function getStrategy() {
  return useApi<StrategyParams>("/settings/strategy");
}

export function getFeeTiers() {
  return useApi<Record<string, FeeTierOption[]>>("/settings/fee-tiers");
}

export function getResolvedFees() {
  return useApi<ResolvedFees>("/settings/fees");
}

// ─── Cash-and-Carry snapshot schema (scripts/market/carry_scanner.py) ──

export interface CarryCand {
  venue: string;
  base: string;
  symbol: string;
  direction: "forward" | "reverse";
  rate_pct: number;
  historical_median_rate_pct?: number;
  expected_rate_pct?: number;
  funding_estimator?: string;
  history_samples?: number;
  history_first_ts?: number;
  history_latest_ts?: number;
  funding_observed_at_ms?: number;
  snapshot_ts_ms?: number;
  snapshot_observed_at_ms?: number;
  next_funding_ts?: number;
  interval_h: number;
  funding_payments_estimated?: number;
  horizon_hours?: number;
  notional_usd_requested?: number;
  quantity_base?: number;
  spot_notional_usd?: number;
  perp_notional_usd?: number;
  mark_price?: number;
  spot_entry_vwap?: number;
  spot_exit_vwap?: number;
  perp_entry_vwap?: number;
  perp_exit_vwap?: number;
  spot_fee_pct?: number;
  perp_fee_pct?: number;
  futures_fee_pct?: number;
  fee_pct?: number;
  short_margin_multiplier?: number;
  short_margin_usd?: number;
  capital_fee_buffer_usd?: number;
  entry_fee_usd?: number;
  exit_fee_usd?: number;
  observed_round_trip_book_cost_usd?: number;
  exit_slippage_bps_per_leg?: number;
  exit_slippage_assumption_usd?: number;
  basis_buffer_bps?: number;
  basis_buffer_usd?: number;
  gross_funding_usd?: number;
  total_estimated_cost_usd?: number;
  net_horizon_earnings_usd?: number;
  net_horizon_roi_pct?: number;
  net_horizon_notional_roi_pct?: number;
  capital_required_usd?: number;
  net_horizon_capital_roi_pct?: number;
  breakeven_rate_pct?: number;
  breakeven_funding_payments?: number;
  breakeven_hours_from_snapshot?: number;
  spot_book_observed_at_ms?: number;
  perp_book_observed_at_ms?: number;
  spot_book_source_ts_ms?: number;
  perp_book_source_ts_ms?: number;
  spot_book_request_started_at_ms?: number;
  perp_book_request_started_at_ms?: number;
  spot_book_snapshot_id?: string | number | null;
  perp_book_snapshot_id?: string | number | null;
  spot_instrument_snapshot_observed_at_ms?: number;
  perp_instrument_snapshot_observed_at_ms?: number;
  spot_instrument_snapshot_id?: string | number | null;
  perp_instrument_snapshot_id?: string | number | null;
  source_timestamp_skew_ms?: number;
  snapshot_estimate?: boolean;
  borrowing_used?: boolean;
  snapshot_id?: string;
  // Legacy carry fields retained for older cached snapshots.
  annual_pct?: number;
  next_ts?: number;
  has_spot?: boolean;
  borrowable?: boolean;
  spot_price?: number;
  net_edge_pct?: number;
  borrow_daily_pct?: number;
  borrow_annual_pct?: number;
}

export interface CarryScanAssumptions {
  spot_taker_fee_pct?: number;
  perp_taker_fee_pct?: number;
  fees_are_taker?: boolean;
  fee_application?: string;
  source?: string;
  tier?: string | null;
  private_fee_api_used?: boolean;
  funding_rate_estimator?: string;
  minimum_history_samples?: number;
  exit_slippage_bps_per_leg?: number;
  basis_buffer_bps_per_position?: number;
  maximum_order_book_age_sec?: number;
  maximum_funding_snapshot_age_sec?: number;
  maximum_source_timestamp_skew_sec?: number;
  maximum_instrument_snapshot_age_sec?: number;
  maximum_history_gap_intervals?: number;
  capital_model?: string;
  short_margin_multiplier?: number;
  borrowing_used?: boolean;
  capital_fee_buffer_method?: string;
  leverage_or_live_execution_approved?: boolean;
  public_market_data_only?: boolean;
}

export interface CarryExclusion {
  symbol: string;
  reason: string;
  detail?: string;
}

export interface CarryInstrumentSnapshot {
  observed_at_ms?: number;
  snapshot_id?: string | number | null;
  [key: string]: unknown;
}

export interface CarryVenue {
  schema_version?: number;
  venue: string;
  direction?: "forward_only" | string;
  total_pairs?: number;
  intersection_pairs?: number;
  notional_usd?: number;
  max_notional_usd?: number;
  horizon_hours?: number;
  max_horizon_hours?: number;
  timestamp_ms?: number;
  scan_started_at_ms?: number;
  completed_at_ms?: number;
  snapshot_id?: string;
  funding_observation_min_ms?: number | null;
  funding_observation_max_ms?: number | null;
  instrument_snapshots?: Record<string, CarryInstrumentSnapshot>;
  instrument_snapshot_observation_times_ms?: Record<string, number | null>;
  disclaimer?: string;
  assumptions?: CarryScanAssumptions;
  forward: CarryCand[];
  forward_candidates?: CarryCand[];
  near_forward?: CarryCand[];
  reverse: CarryCand[];
  forward_no_spot?: CarryExclusion[];
  reverse_candidates?: CarryCand[];
  reverse_not_borrowable?: CarryExclusion[];
  excluded?: CarryExclusion[];
  exclusion_counts?: Record<string, number>;
  spot_fee_pct?: number;
  futures_fee_pct?: number;
  two_leg_fee_pct?: number;
  fee_source?: string;
  fee_tier?: string | null;
  market_data_error?: string;
  error?: string;
}

export interface CarryPaperOpenRequest {
  strategy: "carry";
  base: string;
  symbol: string;
  futures_venue: string;
  spot_venue: string;
  amount_usd: number;
  horizon_hours: number;
  direction: "forward";
  dry_run: true;
  scan_snapshot_id: string;
  candidate_snapshot_id: string;
}

export function openCarryPaperPosition(request: CarryPaperOpenRequest) {
  return post("/positions/open", { ...request });
}

export interface UnifiedCarryCand {
  base: string;
  direction: string;
  futures_venue: string;
  spot_venue: string;
  same_venue: boolean;
  funding_rate_pct: number;
  annual_pct: number;
  spot_fee_pct: number;
  futures_fee_pct: number;
  fee_pct: number;
  net_edge_pct: number;
  borrow_daily_pct?: number;
}

// ─── Wallet & Trading Mode types ────────────────────────────────

export interface WalletFieldSchema {
  key: string;
  label: string;
  type: "text" | "password" | "number" | "select";
  placeholder?: string;
  options?: string[];
  default?: string;
}

export interface WalletVenueSchema {
  name: string;
  chain: string;
  fields: WalletFieldSchema[];
  extra_fields: WalletFieldSchema[];
  live_flag: string | null;
}

export interface WalletVenueStatus {
  connected: boolean;
  chain: string;
  live_enabled: boolean;
  live_flag: string | null;
  fields_masked: Record<string, string>;
  balance_usdc: number;
}

export interface TradingModeVenue {
  mode: "backtest" | "dry_run" | "live";
  wallet_connected: boolean;
  live_enabled: boolean;
}

export interface TradingMode {
  mode: "backtest" | "dry_run" | "live";
  venues: Record<string, TradingModeVenue>;
}

export function getWalletSchemas() {
  return useApi<Record<string, WalletVenueSchema>>("/settings/wallet/schema");
}

export function getWalletStatus(venue?: string) {
  const url = venue
    ? `/settings/wallet/status?venue=${venue}`
    : "/settings/wallet/status";
  return useApi<Record<string, WalletVenueStatus>>(url);
}

export function getTradingMode() {
  return useApi<TradingMode>("/settings/trading-mode");
}

export async function connectWallet(
  venue: string,
  credentials: Record<string, string>,
) {
  return post<{ venue: string; connected: boolean }>(
    "/settings/wallet/connect",
    { venue, credentials },
  );
}

export async function disconnectWallet(venue: string) {
  return post<{ venue: string; connected: boolean }>(
    "/settings/wallet/disconnect",
    { venue },
  );
}

// ─── WebSocket (shared singleton) ─────────────────────────────────
//
// Only one WS connection is maintained per browser tab. Multiple callers
// (App.vue for connection status, Scanner.vue for scanner updates) share
// the same connection via a subscribe/unsubscribe pattern.

export interface WsMessage {
  event: string;
  data: Record<string, any>;
}

type WsSubscriber = (msg: WsMessage) => void;

const _wsState = {
  ws: null as WebSocket | null,
  connected: ref(false),
  subscribers: new Set<WsSubscriber>(),
  connectCallbacks: new Set<() => void>(),
  disconnectCallbacks: new Set<() => void>(),
  reconnectTimer: null as ReturnType<typeof setTimeout> | null,
  pingTimer: null as ReturnType<typeof setInterval> | null,
};

function _wsConnect() {
  const token = getApiToken();
  const protocols = getWebSocketProtocols(token);
  if (!protocols) return;
  if (_wsState.ws && _wsState.ws.readyState <= WebSocket.OPEN) return;

  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const host = window.location.host;
  const url = `${protocol}//${host}/ws/events`;
  const ws = new WebSocket(url, protocols);
  _wsState.ws = ws;

  ws.onopen = () => {
    if (_wsState.ws !== ws) return;
    _wsState.connected.value = true;
    _wsState.connectCallbacks.forEach((cb) => cb());
    _wsState.pingTimer = setInterval(() => {
      if (_wsState.ws === ws && ws.readyState === WebSocket.OPEN) {
        ws.send("ping");
      }
    }, 30000);
  };

  ws.onmessage = (event) => {
    if (_wsState.ws !== ws) return;
    try {
      const msg = JSON.parse(event.data) as WsMessage;
      if (msg.event === "pong") return;
      _wsState.subscribers.forEach((cb) => cb(msg));
    } catch {
      // ignore non-JSON messages
    }
  };

  ws.onclose = (event) => {
    if (_wsState.ws !== ws) return;
    _wsState.ws = null;
    _wsState.connected.value = false;
    if (_wsState.pingTimer) {
      clearInterval(_wsState.pingTimer);
      _wsState.pingTimer = null;
    }
    _wsState.disconnectCallbacks.forEach((cb) => cb());
    if (event.code === 4401) {
      reportApiAuthFailure(401);
      return;
    }
    if (event.code === 1013) {
      reportApiAuthFailure(503);
      return;
    }
    if (getApiToken()) {
      _wsState.reconnectTimer = setTimeout(() => _wsConnect(), 3000);
    }
  };

  ws.onerror = () => {
    if (_wsState.ws === ws) ws.close();
  };
}

subscribeApiToken((token) => {
  if (_wsState.reconnectTimer) {
    clearTimeout(_wsState.reconnectTimer);
    _wsState.reconnectTimer = null;
  }
  if (_wsState.pingTimer) {
    clearInterval(_wsState.pingTimer);
    _wsState.pingTimer = null;
  }

  const previous = _wsState.ws;
  const wasConnected = _wsState.connected.value;
  _wsState.ws = null;
  _wsState.connected.value = false;
  if (previous && previous.readyState < WebSocket.CLOSING) previous.close();
  if (wasConnected) _wsState.disconnectCallbacks.forEach((cb) => cb());
  if (token) _wsConnect();
});

function _wsDisconnect() {
  // Don't actually disconnect — the shared singleton stays alive as long
  // as the app is running. Individual components just unsubscribe via
  // onUnmounted. Real cleanup happens on page unload via the browser.
}

export function useWebSocket(
  onMessage?: WsSubscriber,
  onConnect?: () => void,
  onDisconnect?: () => void,
) {
  // Subscribe if callbacks provided
  if (onMessage) {
    _wsState.subscribers.add(onMessage);
  }
  if (onConnect) {
    _wsState.connectCallbacks.add(onConnect);
  }
  if (onDisconnect) {
    _wsState.disconnectCallbacks.add(onDisconnect);
  }

  // Cleanup on unmount
  onUnmounted(() => {
    if (onMessage) _wsState.subscribers.delete(onMessage);
    if (onConnect) _wsState.connectCallbacks.delete(onConnect);
    if (onDisconnect) _wsState.disconnectCallbacks.delete(onDisconnect);
  });

  return {
    connected: _wsState.connected,
    connect: _wsConnect,
    disconnect: _wsDisconnect,
  };
}
