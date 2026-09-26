import type { EChartsOption } from 'echarts'
import { use } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([LineChart, GridComponent, TooltipComponent, LegendComponent, CanvasRenderer])

export interface BacktestEquityPoint {
  ts: string
  equity: number
  open_pairs?: number
}

export type PositionsEquityPoint = [number, number]

function escapeHtml(value: string): string {
  return value
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;')
}

function finiteNumber(value: number | undefined, fallback = 0): number {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function firstDataIndex(params: unknown): number {
  const first = Array.isArray(params) ? params[0] : params
  if (typeof first !== 'object' || first === null || !('dataIndex' in first)) return 0
  const dataIndex = first.dataIndex
  return typeof dataIndex === 'number' && Number.isInteger(dataIndex) && dataIndex >= 0 ? dataIndex : 0
}

export function buildBacktestEquityChartOption(
  curve: readonly BacktestEquityPoint[],
): EChartsOption | null {
  if (curve.length === 0) return null

  return {
    tooltip: {
      trigger: 'axis',
      formatter: (params: unknown) => {
        const point = curve[firstDataIndex(params)]
        if (!point) return ''
        const equity = finiteNumber(point.equity)
        const openPairs = finiteNumber(point.open_pairs)
        return `${escapeHtml(point.ts)}<br/>Equity: $${equity.toLocaleString()}<br/>Open pairs: ${openPairs}`
      },
    },
    grid: { left: 48, right: 16, top: 24, bottom: 32 },
    xAxis: {
      type: 'category',
      data: curve.map((point) => point.ts.slice(0, 10)),
      axisLabel: { fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { formatter: (value: number) => `$${(value / 1000).toFixed(0)}k` },
    },
    series: [
      {
        type: 'line',
        data: curve.map((point) => finiteNumber(point.equity)),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 2, color: '#18a058' },
        areaStyle: { color: 'rgba(24, 160, 88, 0.12)' },
      },
    ],
  }
}

export function buildPositionsEquityChartOption(
  data: readonly PositionsEquityPoint[],
): EChartsOption {
  return {
    tooltip: {
      trigger: 'axis',
      valueFormatter: (value: unknown) => '$' + finiteNumber(typeof value === 'number' ? value : undefined).toFixed(2),
    },
    grid: { left: 50, right: 20, top: 20, bottom: 30 },
    xAxis: { type: 'time' },
    yAxis: { type: 'value', name: 'PnL (USD)', scale: true },
    series: [
      {
        type: 'line',
        data: data.map(([timestamp, pnl]) => [timestamp, finiteNumber(pnl)]),
        smooth: true,
        showSymbol: false,
        areaStyle: { opacity: 0.1, color: '#18a058' },
        lineStyle: { width: 2, color: '#18a058' },
        itemStyle: { color: '#18a058' },
      },
    ],
  }
}
