import assert from 'node:assert/strict'
import test from 'node:test'
import * as echarts from 'echarts/core'
import { SVGRenderer } from 'echarts/renderers'
import {
  buildBacktestEquityChartOption,
  buildPositionsEquityChartOption,
} from '../src/charts/equity.ts'

echarts.use([SVGRenderer])

function renderOption(option) {
  const chart = echarts.init(null, null, {
    renderer: 'svg',
    ssr: true,
    width: 640,
    height: 320,
  })
  try {
    chart.setOption(option)
    return chart.getOption()
  } finally {
    chart.dispose()
  }
}

test('Backtest.vue equity options render through ECharts 6 and escape tooltip text', () => {
  const option = buildBacktestEquityChartOption([
    { ts: '<img src=x onerror=alert(1)>', equity: 1012.5, open_pairs: 2 },
  ])
  assert.ok(option)
  assert.equal(option.series?.[0]?.type, 'line')
  assert.equal(option.xAxis?.type, 'category')

  const formatter = option.tooltip?.formatter
  assert.equal(typeof formatter, 'function')
  const tooltipHtml = formatter([{ dataIndex: 0 }])
  assert.match(tooltipHtml, /&lt;img/)
  assert.doesNotMatch(tooltipHtml, /<img\b/i)

  const rendered = renderOption(option)
  assert.equal(rendered.series?.[0]?.type, 'line')
  assert.deepEqual(rendered.series?.[0]?.data, [1012.5])
})

test('Positions.vue equity options render time-series tuples through ECharts 6', () => {
  const option = buildPositionsEquityChartOption([
    [1700000000000, 12.5],
    [1700003600000, -4.25],
  ])
  assert.equal(option.xAxis?.type, 'time')
  assert.equal(option.yAxis?.type, 'value')
  assert.equal(option.series?.[0]?.type, 'line')

  const rendered = renderOption(option)
  assert.equal(rendered.series?.[0]?.type, 'line')
  assert.deepEqual(rendered.series?.[0]?.data, [
    [1700000000000, 12.5],
    [1700003600000, -4.25],
  ])
})
