<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import {
  NCard, NSpace, NText, NTag, NButton, NForm, NFormItem, NInput, NSelect,
  NSwitch, NDivider, NIcon, NSkeleton,
} from 'naive-ui'
import { KeyOutline, CheckmarkCircleOutline } from '@vicons/ionicons5'
import type { VenueConfig, WalletVenueSchema, WalletVenueStatus } from '@/composables/useApi'

const props = defineProps<{
  venue: string
  schema?: WalletVenueSchema
  status?: WalletVenueStatus
  form: Record<string, string>
  meta?: VenueConfig
  loading?: boolean
}>()

const emit = defineEmits<{
  (e: 'connect'): void
  (e: 'disconnect'): void
  (e: 'toggle-live', value: boolean): void
}>()

const { t } = useI18n()
const displayName = computed(() => props.schema?.name || props.venue)
const allFields = computed(() => [
  ...(props.schema?.fields ?? []),
  ...(props.schema?.extra_fields ?? []),
])
const isConnected = computed(() => !!props.status?.connected)

function setField(key: string, value: string) {
  props.form[key] = value
}
</script>

<template>
  <n-card size="small" class="venue-card" :bordered="true">
    <template #header>
      <div class="venue-card__header">
        <div class="venue-card__title-row">
          <n-icon :size="18" :color="isConnected ? '#18a058' : '#666'">
            <KeyOutline />
          </n-icon>
          <div>
            <n-text strong style="font-size: 14px">{{ displayName }}</n-text>
            <n-space :size="6" style="margin-top: 4px">
              <n-tag size="tiny" :bordered="false">CEX</n-tag>
              <n-tag v-if="meta?.scan_capable" size="tiny" :bordered="false" type="success">
                {{ t('settings.scanOk') }}
              </n-tag>
              <n-tag v-if="meta && meta.trade_capable === false" size="tiny" :bordered="false" type="warning">
                {{ t('settings.scanOnly') }}
              </n-tag>
            </n-space>
          </div>
        </div>
        <n-tag size="small" :type="isConnected ? 'success' : 'default'" :bordered="false">
          <template v-if="isConnected" #icon>
            <n-icon :component="CheckmarkCircleOutline" />
          </template>
          {{ isConnected ? t('settings.walletConnected') : t('settings.walletNotConnected') }}
        </n-tag>
      </div>
    </template>

    <template v-if="loading && !schema">
      <n-skeleton text :repeat="3" />
    </template>

    <template v-else-if="isConnected">
      <div class="venue-card__connected">
        <div v-for="(value, key) in (status?.fields_masked ?? {})" :key="key" class="venue-card__row">
          <n-text depth="3" style="font-size: 12px">{{ key }}</n-text>
          <n-text style="font-size: 12px; font-family: monospace">{{ value }}</n-text>
        </div>
        <n-divider style="margin: 10px 0" />
        <div class="venue-card__row">
          <n-text depth="3">{{ t('settings.balance') }}</n-text>
          <n-text strong>{{ (status?.balance_usdc ?? 0).toFixed(2) }} USDC</n-text>
        </div>
        <div v-if="schema?.live_flag" class="venue-card__row" style="margin-top: 8px">
          <n-text depth="3">{{ t('settings.modeLive') }}</n-text>
          <n-switch :value="status?.live_enabled ?? false" @update:value="(value: boolean) => emit('toggle-live', value)" />
        </div>
        <n-button size="small" type="warning" ghost block style="margin-top: 12px" @click="emit('disconnect')">
          {{ t('settings.disconnectWallet') }}
        </n-button>
      </div>
    </template>

    <template v-else>
      <n-space vertical :size="12" class="venue-card__body">
        <n-text depth="3" style="font-size: 12px; line-height: 1.5">
          {{ t('settings.cexApiHint') }}
        </n-text>
        <n-form label-placement="top" size="small" class="venue-card__form">
          <n-form-item
            v-for="field in allFields"
            :key="field.key"
            :label="field.label"
            :show-require-mark="schema?.fields.some((item) => item.key === field.key)"
          >
            <n-input
              v-if="field.type !== 'select'"
              :value="form[field.key] ?? ''"
              :type="field.type === 'password' ? 'password' : 'text'"
              :placeholder="field.placeholder || field.label"
              show-password-on="click"
              @update:value="(value: string) => setField(field.key, value)"
            />
            <n-select
              v-else
              :value="form[field.key] ?? ''"
              :options="field.options?.map((option) => ({ label: option, value: option }))"
              :placeholder="field.label"
              @update:value="(value: string) => setField(field.key, value)"
            />
          </n-form-item>
          <n-button type="primary" size="small" block @click="emit('connect')">
            <template #icon><n-icon :component="KeyOutline" /></template>
            {{ t('settings.saveApiKeys') }}
          </n-button>
        </n-form>
      </n-space>
    </template>
  </n-card>
</template>

<style scoped>
.venue-card {
  height: 100%;
  background: rgba(255, 255, 255, 0.02);
  transition: border-color 0.2s;
}
.venue-card:hover { border-color: rgba(255, 255, 255, 0.12); }
.venue-card__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  width: 100%;
}
.venue-card__title-row { display: flex; align-items: flex-start; gap: 10px; }
.venue-card__connected { display: flex; flex-direction: column; gap: 6px; }
.venue-card__row { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 12px; }
.venue-card__form :deep(.n-form-item-label) { font-size: 12px; padding-bottom: 4px; }
.venue-card__form :deep(.n-form-item) { margin-bottom: 12px; }
.venue-card__body { display: flex; flex-direction: column; justify-content: flex-start; }
</style>
