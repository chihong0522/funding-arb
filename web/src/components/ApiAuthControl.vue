<script setup lang="ts">
import { computed, ref } from 'vue'
import { NAlert, NButton, NPopover, NSpace, NText } from 'naive-ui'
import { clearApiToken, isValidApiToken, setApiToken, useApiAuth } from '@/composables/apiAuth'

const draft = ref('')
const inputError = ref('')
const popoverOpen = ref(false)
const { hasToken, status, message } = useApiAuth()

const alertType = computed(() => {
  if (status.value === 'unauthorized') return 'error'
  if (status.value === 'unavailable') return 'warning'
  return 'info'
})
const statusText = computed(() => message.value || (
  hasToken.value
    ? 'Token held in memory for this tab only; the server checks it on each request.'
    : 'Public scanning remains available. Protected controls require a 32+ character URL-safe API token.'
))
const showClear = computed(() => hasToken.value || status.value !== 'missing')

function saveToken() {
  inputError.value = ''
  if (!isValidApiToken(draft.value)) {
    inputError.value = 'Enter at least 32 URL-safe characters.'
    return
  }
  setApiToken(draft.value)
  draft.value = ''
  popoverOpen.value = false
}

function clearToken() {
  draft.value = ''
  inputError.value = ''
  clearApiToken()
}
</script>

<template>
  <n-popover v-model:show="popoverOpen" trigger="click" placement="bottom-end" :width="340">
    <template #trigger>
      <n-button size="tiny" :type="hasToken ? 'success' : 'warning'">
        {{ hasToken ? 'API token in memory' : 'Enter API token' }}
      </n-button>
    </template>

    <div class="api-auth-panel">
      <n-text strong>Control API authentication</n-text>
      <n-alert :type="alertType" :bordered="false" style="margin: 10px 0">
        {{ statusText }}
      </n-alert>
      <label class="token-label" for="funding-arb-api-token">API token</label>
      <input
        id="funding-arb-api-token"
        v-model="draft"
        type="password"
        autocomplete="off"
        autocapitalize="off"
        spellcheck="false"
        placeholder="32+ URL-safe characters"
        :aria-invalid="Boolean(inputError)"
        class="token-input"
        @keydown.enter.prevent="saveToken"
      />
      <n-text v-if="inputError" type="error" class="input-error" role="alert">
        {{ inputError }}
      </n-text>
      <n-space justify="end" style="margin-top: 12px">
        <n-button v-if="showClear" size="small" @click="clearToken">Clear token</n-button>
        <n-button size="small" type="primary" :disabled="!draft" @click="saveToken">
          Use in this tab
        </n-button>
      </n-space>
    </div>
  </n-popover>
</template>

<style scoped>
.api-auth-panel {
  display: flex;
  flex-direction: column;
}

.token-label {
  margin-bottom: 5px;
  font-size: 12px;
  color: rgba(255, 255, 255, 0.72);
}

.token-input {
  box-sizing: border-box;
  width: 100%;
  min-height: 34px;
  padding: 0 10px;
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 4px;
  background: #18181c;
  color: #f3f3f5;
  font: inherit;
}

.token-input:focus {
  border-color: #18a058;
  outline: 2px solid rgba(24, 160, 88, 0.25);
}

.input-error {
  margin-top: 4px;
  font-size: 12px;
}
</style>
