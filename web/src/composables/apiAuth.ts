import { computed, readonly, ref } from 'vue'

export const API_TOKEN_PATTERN = /[A-Za-z0-9._~-]{32,}/
export const WS_AUTH_PROTOCOL_PREFIX = 'funding-arb-token.'
export const WS_EVENTS_SUBPROTOCOL = 'funding-arb-events.v1'

export type ApiAuthStatus = 'missing' | 'token-entered' | 'unauthorized' | 'unavailable'

const apiToken = ref<string | null>(null)
const authStatus = ref<ApiAuthStatus>('missing')
const authMessage = ref<string | null>(null)
const tokenListeners = new Set<(token: string | null) => void>()

function notifyTokenListeners() {
  tokenListeners.forEach((listener) => listener(apiToken.value))
}

export function isValidApiToken(value: string): boolean {
  return API_TOKEN_PATTERN.exec(value)?.[0] === value
}

export function setApiToken(value: string): void {
  if (!isValidApiToken(value)) {
    throw new Error('API token must contain at least 32 URL-safe characters.')
  }
  if (apiToken.value !== value) {
    apiToken.value = value
    notifyTokenListeners()
  }
  authStatus.value = 'token-entered'
  authMessage.value = null
}

export function clearApiToken(): void {
  const hadToken = apiToken.value !== null
  apiToken.value = null
  authStatus.value = 'missing'
  authMessage.value = null
  if (hadToken) notifyTokenListeners()
}

export function getApiToken(): string | null {
  return apiToken.value
}

export function subscribeApiToken(listener: (token: string | null) => void): () => void {
  tokenListeners.add(listener)
  return () => {
    tokenListeners.delete(listener)
  }
}

export function getWebSocketProtocols(token = apiToken.value): string[] | null {
  if (!token) return null
  return [`${WS_AUTH_PROTOCOL_PREFIX}${token}`, WS_EVENTS_SUBPROTOCOL]
}

export function reportApiAuthFailure(status: 401 | 503): void {
  if (status === 401) {
    const hadToken = apiToken.value !== null
    apiToken.value = null
    authStatus.value = 'unauthorized'
    authMessage.value = 'API authentication failed (401). The in-memory token was cleared; enter a valid token.'
    if (hadToken) notifyTokenListeners()
    return
  }
  authStatus.value = 'unavailable'
  authMessage.value = 'Control API is disabled (503). Configure FUNDING_ARB_API_TOKEN on the server.'
}

function isSameOriginApiRequest(input: RequestInfo | URL): boolean {
  const rawUrl = input instanceof Request
    ? input.url
    : input instanceof URL
      ? input.href
      : String(input)
  try {
    const currentUrl = new URL(rawUrl, window.location.href)
    return currentUrl.origin === window.location.origin
      && (currentUrl.pathname === '/api' || currentUrl.pathname.startsWith('/api/'))
  } catch {
    return false
  }
}

export async function apiFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  if (!isSameOriginApiRequest(input)) return fetch(input, init)

  const headers = new Headers(input instanceof Request ? input.headers : undefined)
  new Headers(init?.headers).forEach((value, name) => headers.set(name, value))
  const token = apiToken.value
  if (token) headers.set('Authorization', `Bearer ${token}`)
  else headers.delete('Authorization')

  const response = await fetch(input, { ...init, headers })
  if (response.status === 401 || response.status === 503) {
    reportApiAuthFailure(response.status)
  }
  return response
}

export function useApiAuth() {
  return {
    hasToken: computed(() => apiToken.value !== null),
    status: readonly(authStatus),
    message: readonly(authMessage),
  }
}
