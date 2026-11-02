<script setup lang="ts">
import { computed } from 'vue';
import { objectValue, probeName, shortTimeLabel } from '../display';
import type { Incident, Json, Probe } from '../types';

const props = defineProps<{ incident: Incident }>();
const emit = defineEmits<{
  (e: 'inspect-verification'): void;
}>();

const verification = computed(() => props.incident.detail.verification);
// M5：窗口内再次失败 → 恢复不稳定。verdict 仍是三值语义，不新增枚举。
const windowState = computed(() => verification.value?.window ?? null);
const unstable = computed(() => Boolean(windowState.value?.instability));

// 提取当前轮次最新采样的探针列表
const latestSampleProbes = computed<Probe[]>(() => {
  const sampleRecords = props.incident.records.filter((r) => r.kind === 'probe_sample');
  if (sampleRecords.length) {
    const latest = sampleRecords.at(-1);
    if (Array.isArray(latest?.payload.probes)) {
      return (latest.payload.probes as unknown[]).map((rawItem) => {
        const p = objectValue(rawItem as Json);
        return {
          name: String(p.name || ''),
          verdict: (p.verdict === 'PASS' || p.verdict === 'FAIL' || p.verdict === 'UNKNOWN'
            ? p.verdict
            : 'UNKNOWN') as Probe['verdict'],
          observed: objectValue(p.observed),
          captured_at: typeof p.captured_at === 'string' ? p.captured_at : latest.created_at,
        };
      });
    }
  }

  const rawSamples = verification.value?.samples ?? [];
  if (rawSamples.length) {
    return rawSamples.at(-1) ?? [];
  }

  return [];
});

function formatProbeObserved(probe: Probe): string {
  const obs = objectValue(probe.observed);
  if (obs.status_code) return `HTTP ${obs.status_code}`;
  if (obs.ping) return `PING: ${obs.ping}`;
  if (obs.db) return `DB: ${obs.db}`;
  if (obs.result) return String(obs.result);
  if (obs.error) return `异常: ${obs.error}`;
  return probe.verdict === 'PASS' ? '检查通过' : '观测异常';
}
</script>

<template>
  <section class="proto-card">
    <div class="section-heading">
      <h2>工单复测</h2>
      <button
        v-if="verification || latestSampleProbes.length"
        type="button"
        class="text-button text-12"
        @click="emit('inspect-verification')"
      >
        采样详情 →
      </button>
    </div>

    <!-- 结论大横幅 (V1-10) -->
    <div
      v-if="verification"
      class="verdict-large-banner verification-result"
      :data-verdict="verification.verdict"
    >
      <div>
        <div class="verdict-title">
          {{
            verification.verdict === 'PASS'
              ? '已恢复 (PASS)'
              : verification.verdict === 'FAIL'
                ? '未恢复 (FAIL)'
                : '结果未知 (UNKNOWN)'
          }}
        </div>
        <div class="verdict-desc">
          {{
            unstable
              ? '窗口内出现过失败采样，恢复不稳定，不能视为已恢复。'
              : verification.verdict === 'PASS'
                ? '本轮场景的全部强制探针通过。'
                : verification.verdict === 'FAIL'
                  ? '实际探针明确失败，请查看采样记录。'
                  : '未取得有效观测，不能判定恢复。'
          }}
        </div>
      </div>
      <span class="muted text-12" style="white-space: nowrap">
        <span v-if="unstable" class="state-badge" data-tone="danger" style="margin-right: 6px">
          恢复不稳定
        </span>
        {{ shortTimeLabel(verification.captured_at) }}
      </span>
    </div>

    <div v-else class="verdict-large-banner verification-result" data-verdict="WAITING">
      <div>
        <div class="verdict-title" style="color: var(--ink-secondary)">等待复测</div>
        <div class="verdict-desc">处置执行后由独立探针判定恢复结果</div>
      </div>
    </div>

    <div v-if="windowState?.enabled" class="probe-compact-list sample-details">
      <div class="probe-compact-row">
        <div style="min-width: 0; display: flex; flex-direction: column">
          <strong style="font-size: 13px">连续健康窗口</strong>
          <span class="muted text-12">
            连续通过 {{ windowState.consecutive_passes }} / {{ windowState.required_passes }} 次 ·
            窗口内失败 {{ windowState.failures }} 次 · 采样 {{ windowState.samples }} 次
          </span>
        </div>
        <span class="state-badge" :data-tone="windowState.stable ? 'success' : 'warning'">
          {{ windowState.stable ? '窗口内稳定' : '窗口内不稳定' }}
        </span>
      </div>
      <p class="help-text">窗口内健康只说明这一段时间的观测，不代表永久不再复发。</p>
    </div>

    <!-- 紧凑探针行展示 -->
    <div v-if="latestSampleProbes.length" class="probe-compact-list sample-details">
      <div v-for="probe in latestSampleProbes" :key="probe.name" class="probe-compact-row">
        <div style="min-width: 0; display: flex; flex-direction: column">
          <strong class="mono" style="font-size: 13px">{{ probeName(probe.name) }}</strong>
          <span class="muted text-12">{{ formatProbeObserved(probe) }}</span>
        </div>
        <span class="verdict" :data-verdict="probe.verdict">
          {{ probe.verdict }}
        </span>
      </div>
    </div>

    <div v-else-if="!verification" class="empty compact">尚未取得复测采样。</div>
  </section>
</template>
