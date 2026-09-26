import { reactive } from "vue";

export interface DydxOrderState {
  connected: boolean;
  ordering: boolean;
  address: string;
  error: string | null;
  lastTxHash: string | null;
  subaccountNumber: number;
}

export interface DydxPlaceOrderParams {
  coin: string;
  isBuy: boolean;
  size: number;
  slippage?: number;
  testnet?: boolean;
  subaccountNumber?: number;
}

export interface DydxOrderResult {
  success: boolean;
  txHash?: string;
  error?: string;
}

const DISABLED = "Browser-wallet DEX signing is disabled in this frontend.";
const state = reactive<DydxOrderState>({
  connected: false,
  ordering: false,
  address: "",
  error: DISABLED,
  lastTxHash: null,
  subaccountNumber: 0,
});

/** Compatibility stub: browser-wallet DEX signing is intentionally unavailable. */
export function useDydxTrade() {
  async function placeOrder(_params: DydxPlaceOrderParams): Promise<DydxOrderResult> {
    state.error = DISABLED;
    return { success: false, error: DISABLED };
  }

  function disconnect(): void {
    state.connected = false;
    state.address = "";
    state.error = DISABLED;
    state.lastTxHash = null;
  }

  function checkConnection(): void {
    state.connected = false;
    state.address = "";
    state.error = DISABLED;
  }

  return {
    dydxTradeState: state,
    placeOrder,
    disconnect,
    checkConnection,
  };
}
