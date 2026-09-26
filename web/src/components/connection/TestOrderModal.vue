<script setup lang="ts">
import { NAlert, NModal, NText } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ (e: 'update:show', value: boolean): void }>()
const { t } = useI18n()
</script>

<template>
  <n-modal :show="props.show" @update:show="emit('update:show', $event)" preset="card" :title="t('settings.walletOrdersDisabledTitle')" style="width: 440px">
    <n-alert type="warning" :bordered="false">
      <n-text>{{ t('settings.walletOrdersDisabled') }}</n-text>
    </n-alert>
  </n-modal>
</template>
