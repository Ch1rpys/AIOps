<script setup lang="ts">
import { objectValue, probeName, shortTimeLabel, timeLabel } from '../display';
import type { Json, Probe } from '../types';

defineProps<{ probes: Probe[] }>();

function formatObservationBrief(observed: Json | undefined): string {
  const obs = objectValue(observed);
  if (obs.status_code) return `HTTP ${obs.status_code}`;
  if (obs.ping) return `PING: ${obs.ping}`;
  if (obs.db) return `DB: ${obs.db}`;
  if (obs.result) return String(obs.result);
  if (obs.error) return `异常: ${obs.error}`;
  return JSON.stringify(observed);
}
</script>

<template>
  <div v-if="!probes.length" class="empty compact">尚未取得探针观测，不能判定恢复结果。</div>
  <div v-else class="table-scroll">
    <table class="probe-table">
      <thead>
        <tr>
          <th>探针名称</th>
          <th>检查结果</th>
          <th>原始观测</th>
          <th>采样时间</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(probe, index) in probes" :key="`${probe.name}-${index}`">
          <td>
            <strong class="mono" style="font-size: 13px">{{ probeName(probe.name) }}</strong>
            <div class="muted text-12 mono">{{ probe.name }}</div>
          </td>
          <td>
            <span class="verdict" :data-verdict="probe.verdict">{{ probe.verdict }}</span>
          </td>
          <td class="probe-observation">
            <details class="probe-observation">
              <summary>{{ formatObservationBrief(probe.observed) }}</summary>
              <pre>{{ JSON.stringify(probe.observed, null, 2) }}</pre>
            </details>
          </td>
          <td class="muted text-12" :title="timeLabel(probe.captured_at)">
            {{ shortTimeLabel(probe.captured_at) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
