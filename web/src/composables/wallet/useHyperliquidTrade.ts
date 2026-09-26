import { reactive } from "vue";

export interface AgentState {
  connected: boolean;
  approving: boolean;
  ordering: boolean;
  address: string;
  agentAddress: string;
  error: string | null;
  lastTxHash: string | null;
}

const DISABLED = "Browser-wallet DEX signing is disabled in this frontend.";
const state = reactive<AgentState>({
  connected: false,
  approving: false,
  ordering: false,
  address: "",
  agentAddress: "",
  error: DISABLED,
  lastTxHash: null,
});

/** Compatibility stub: browser-wallet DEX signing is intentionally unavailable. */
export function useHyperliquidTrade() {
  async function approveAgent(_testnet = false): Promise<boolean> {
    state.error = DISABLED;
    return false;
  }

  async function placeOrder(_params: {
    coin: string;
    isBuy: boolean;
    size: number;
    slippage?: number;
    testnet?: boolean;
  }): Promise<{ success: boolean; txHash?: string; error?: string }> {
    state.error = DISABLED;
    return { success: false, error: DISABLED };
  }

  async function closePosition(_params: {
    coin: string;
    testnet?: boolean;
  }): Promise<{ success: boolean; txHash?: string; error?: string }> {
    state.error = DISABLED;
    return { success: false, error: DISABLED };
  }

  function disconnect() {
    state.connected = false;
    state.address = "";
    state.agentAddress = "";
    state.error = DISABLED;
    state.lastTxHash = null;
  }

  function checkExistingAgent() {
    state.connected = false;
    state.error = DISABLED;
  }

  return {
    hlTradeState: state,
    approveAgent,
    placeOrder,
    closePosition,
    disconnect,
    checkExistingAgent,
  };
}
