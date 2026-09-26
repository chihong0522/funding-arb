import { reactive } from "vue";
import { useHyperliquidTrade } from "./useHyperliquidTrade";
import { useDydxTrade } from "./useDydxTrade";
import {
  WALLET_TRADE_VENUES,
  type WalletTradeVenue,
} from "@/constants/walletTrade";

export { WALLET_TRADE_VENUES, type WalletTradeVenue };

export interface PlaceOrderParams {
  venue: string;
  coin: string;
  isBuy: boolean;
  size: number;
  slippage?: number;
  testnet?: boolean;
}

export interface OrderResult {
  success: boolean;
  txHash?: string;
  error?: string;
}

interface WalletTradeState {
  ready: Record<string, boolean>;
  ordering: boolean;
  lastResult: OrderResult | null;
  errors: Record<string, string | null>;
}

const DISABLED = "Browser-wallet DEX signing is disabled in this frontend.";
const state = reactive<WalletTradeState>({
  ready: {},
  ordering: false,
  lastResult: null,
  errors: {},
});

/** Compatibility stub: all wallet-signed DEX order paths fail closed. */
export function useWalletTrade() {
  const { hlTradeState } = useHyperliquidTrade();
  const { dydxTradeState } = useDydxTrade();

  function isVenueReady(_venue: string): boolean { return false; }
  function supportsWalletTrade(_venue: string): boolean { return false; }
  function isWalletConnected(_venue: string): boolean { return false; }
  function isAgentReady(_venue: string): boolean { return false; }
  async function ensureAgent(_venue: string, _testnet = false): Promise<boolean> { return false; }

  async function placeOrder(params: PlaceOrderParams): Promise<OrderResult> {
    const result = { success: false, error: DISABLED };
    state.ordering = false;
    state.lastResult = result;
    state.errors[params.venue] = DISABLED;
    return result;
  }

  function init() {
    state.ready = {};
  }

  return {
    walletTradeState: state,
    supportsWalletTrade,
    isWalletConnected,
    isAgentReady,
    isVenueReady,
    ensureAgent,
    placeOrder,
    init,
    hlTradeState,
    dydxTradeState,
  };
}
