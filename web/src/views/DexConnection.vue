<script setup lang="ts">
import { NAlert, NCard, NText } from 'naive-ui'
import { useI18n } from 'vue-i18n'

const { t } = useI18n()
</script>

<template>
  <n-card :title="t('menu.dex')">
    <n-alert type="warning" :bordered="false">
      <n-text>{{ t('settings.dexUiDisabled') }}</n-text>
    </n-alert>
  </n-card>
</template>
