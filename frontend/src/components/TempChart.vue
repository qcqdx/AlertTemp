<script setup>
import * as echarts from 'echarts'
import { onMounted, onUnmounted, ref, watch } from 'vue'

const props = defineProps({
  // [{ name, points: [{time, value|avg}] }]
  series: { type: Array, required: true },
  thresholds: { type: Object, default: null }, // { warn_low, warn_high, crit_low, crit_high }
})

const el = ref(null)
let chart = null

const COLORS = ['#4da3ff', '#35c26e', '#c78bff']

function thresholdLines(thresholds) {
  if (!thresholds) return []
  const lines = []
  const mk = (value, label, color, width) =>
    value !== null &&
    value !== undefined &&
    lines.push({
      yAxis: value,
      label: { formatter: `${label} ${value}°`, color, position: 'insideEndTop' },
      lineStyle: { color, type: 'dashed', width },
    })
  mk(thresholds.warn_high, 'Перегрев', '#f5a623', 1)
  mk(thresholds.warn_low, 'Переохл.', '#4da3ff', 1)
  mk(thresholds.crit_high, 'Крит.', '#f04a4a', 1.5)
  mk(thresholds.crit_low, 'Крит.', '#b04aff', 1.5)
  return lines
}

function render() {
  if (!chart) return
  const option = {
    animation: false,
    backgroundColor: 'transparent',
    grid: { left: 55, right: 20, top: 30, bottom: 55 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#1a212b',
      borderColor: '#2c3644',
      textStyle: { color: '#e6ebf2' },
      valueFormatter: (v) => (v == null ? '—' : `${Number(v).toFixed(2)} °C`),
    },
    legend: { textStyle: { color: '#8b97a7' } },
    xAxis: {
      type: 'time',
      axisLine: { lineStyle: { color: '#2c3644' } },
      axisLabel: { color: '#8b97a7' },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: '#8b97a7', formatter: '{value}°' },
      splitLine: { lineStyle: { color: '#212a36' } },
    },
    dataZoom: [
      { type: 'inside' },
      { type: 'slider', height: 22, bottom: 8, borderColor: '#2c3644' },
    ],
    series: props.series.map((s, i) => ({
      name: s.name,
      type: 'line',
      showSymbol: false,
      connectNulls: false,
      lineStyle: { width: 1.6, color: COLORS[i % COLORS.length] },
      itemStyle: { color: COLORS[i % COLORS.length] },
      data: s.points.map((p) => [p.time, p.avg ?? p.value]),
      markLine:
        i === 0
          ? { silent: true, symbol: 'none', data: thresholdLines(props.thresholds) }
          : undefined,
    })),
  }
  chart.setOption(option, true)
}

function resize() {
  chart?.resize()
}

onMounted(() => {
  chart = echarts.init(el.value)
  render()
  window.addEventListener('resize', resize)
})
onUnmounted(() => {
  window.removeEventListener('resize', resize)
  chart?.dispose()
})
watch(() => [props.series, props.thresholds], render, { deep: true })
</script>

<template>
  <div ref="el" class="chart"></div>
</template>
