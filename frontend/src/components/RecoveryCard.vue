<script setup lang="ts">
import { computed, ref } from 'vue';
import { toPng } from 'html-to-image';
import { ElMessage } from 'element-plus';
import {
  actionName,
  durationLabel,
  probeName,
  rootCauseName,
  scenarioName,
  verdictLabels,
} from '../display';
import type { ImpactNode, IncidentReview, Probe } from '../types';

const props = defineProps<{
  review: IncidentReview;
  afterSample?: Probe[];
  verdict?: string;
  elapsedMs?: number;
}>();

const capture = ref<HTMLElement | null>(null);
const shareInput = ref<HTMLInputElement | null>(null);
const exporting = ref(false);

function lastByName(probes: Probe[]): Map<string, Probe> {
  const latest = new Map<string, Probe>();
  for (const probe of probes) latest.set(probe.name, probe);
  return latest;
}

const beforeByName = computed(() => lastByName(props.review.probes_before));
const afterByName = computed(() => {
  const sample = props.afterSample?.length
    ? props.afterSample
    : lastRound(props.review.probes_after);
  return lastByName(sample);
});

function lastRound(flat: Probe[]): Probe[] {
  const latest = new Map<string, Probe>();
  for (const probe of flat) latest.set(probe.name, probe);
  return [...latest.values()];
}

const rows = computed(() => {
  const names = new Set<string>();
  for (const [name, probe] of beforeByName.value) {
    if (probe.verdict === 'FAIL') names.add(name);
  }
  for (const name of afterByName.value.keys()) names.add(name);
  return [...names].map((name) => ({
    name,
    before: beforeByName.value.get(name),
    after: afterByName.value.get(name),
  }));
});

const faultNodes = computed(() =>
  props.review.impact_graph.nodes.filter(
    (node) => node.observed_anomaly || (node.state !== 'running' && node.state !== 'unknown'),
  ),
);

const executionState = computed(() => {
  const state = props.review.approved_action.execution?.state;
  return typeof state === 'string' ? state : '';
});

const shareUrl = computed(
  () => `${window.location.origin}/incidents/${props.review.incident_id}/card`,
);

function observedText(probe: Probe | undefined): string {
  if (!probe) return '无这条观测';
  const observed = probe.observed;
  if (observed.status_code !== undefined) return `HTTP ${String(observed.status_code)}`;
  if (observed.mysql_error !== undefined) return `MySQL ${String(observed.mysql_error)}`;
  if (observed.error !== undefined) return String(observed.error);
  if (observed.ping !== undefined) return `PING ${String(observed.ping)}`;
  const row = observed.row;
  if (Array.isArray(row)) return `结果 ${row.map(String).join(', ')}`;
  return probe.verdict === 'PASS' ? '检查通过' : '观测异常';
}

function nodeText(node: ImpactNode): string {
  return `${node.id} · ${node.state === 'exited' ? '已停止' : node.state}`;
}

async function exportImage() {
  if (!capture.value) return;
  exporting.value = true;
  try {
    const url = await toPng(capture.value, { pixelRatio: 2, backgroundColor: '#f6f7f9' });
    const link = document.createElement('a');
    link.href = url;
    link.download = `recovery-${props.review.incident_id}.png`;
    link.click();
  } catch {
    ElMessage.error('导出图片失败，请重试');
  } finally {
    exporting.value = false;
  }
}

function copyFromInput(): boolean {
  const input = shareInput.value;
  if (!input) return false;
  input.focus();
  input.select();
  return document.execCommand('copy');
}

async function copyLink() {
  const copied = copyFromInput();
  if (copied) {
    ElMessage.success('链接已复制。只在这台机器、服务开着时能打开。');
    return;
  }
  try {
    await navigator.clipboard.writeText(shareUrl.value);
    ElMessage.success('链接已复制。只在这台机器、服务开着时能打开。');
  } catch {
    ElMessage.error('复制失败，请手动选取下面的地址');
  }
}
</script>

<template>
  <div class="recovery-shell">
    <article ref="capture" class="recovery-card">
      <header class="recovery-head">
        <p class="eyebrow">修复对比</p>
        <dl class="score-strip">
          <div>
            <dt>根因</dt>
            <dd>{{ rootCauseName(review.root_cause) }}</dd>
          </div>
          <div>
            <dt>批准动作</dt>
            <dd>
              {{
                review.approved_action.plan
                  ? `${actionName(review.approved_action.plan.action_id)} → ${review.approved_action.plan.target}`
                  : '没有已批准的动作'
              }}
            </dd>
          </div>
          <div>
            <dt>复测</dt>
            <dd>{{ verdict ? verdictLabels[verdict] || verdict : '尚未复测' }}</dd>
          </div>
          <div>
            <dt>耗时</dt>
            <dd>{{ durationLabel(elapsedMs) }}</dd>
          </div>
        </dl>
        <h2>
          {{ scenarioName(review.scenario) }}{{ review.status === 'RESOLVED' ? '已恢复' : '' }}
        </h2>
        <p class="mono muted">{{ review.incident_id }}</p>
      </header>

      <div class="compare">
        <section class="pane pane-before">
          <h3>故障时</h3>
          <ul v-if="faultNodes.length" class="node-list">
            <li v-for="node in faultNodes" :key="node.id">{{ nodeText(node) }}</li>
          </ul>
          <ul class="probe-list">
            <li v-for="row in rows" :key="row.name">
              <strong>{{ probeName(row.name) }}</strong>
              <span>{{ row.before?.verdict ?? '无' }} · {{ observedText(row.before) }}</span>
            </li>
          </ul>
        </section>
        <section class="pane pane-after">
          <h3>恢复后</h3>
          <p v-if="!rows.some((row) => row.after)" class="empty-after">复测还没有采样。</p>
          <ul v-else class="probe-list">
            <li v-for="row in rows" :key="row.name">
              <strong>{{ probeName(row.name) }}</strong>
              <span>{{ row.after?.verdict ?? '未复测' }} · {{ observedText(row.after) }}</span>
            </li>
          </ul>
        </section>
      </div>

      <section class="ai-work">
        <h3>AI 做了什么</h3>
        <p class="cause">{{ rootCauseName(review.root_cause) }}</p>
        <p class="summary">{{ review.root_cause_summary || '没有诊断摘要' }}</p>
        <ul>
          <li>引用证据 {{ review.root_cause_evidence_ids.length }} 条</li>
          <li v-if="review.approved_action.plan">
            批准动作
            {{ actionName(review.approved_action.plan.action_id) }}
            → {{ review.approved_action.plan.target }}
          </li>
          <li v-else>没有已批准的动作</li>
          <li>
            执行结果
            {{ executionState === 'SUCCEEDED' ? '成功' : executionState || '无' }}
          </li>
        </ul>
      </section>
      <p class="local-note">分享链接只在这台机器、服务还开着的时候能打开。</p>
    </article>

    <div class="recovery-actions">
      <button type="button" class="action" :disabled="exporting" @click="exportImage">
        {{ exporting ? '正在导出…' : '导出图片' }}
      </button>
      <button type="button" class="action" @click="copyLink">复制链接</button>
      <input
        ref="shareInput"
        class="share-url"
        readonly
        :value="shareUrl"
        aria-label="本机查看链接"
        @focus="shareInput?.select()"
      />
    </div>
  </div>
</template>

<style scoped>
.recovery-shell {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.recovery-card {
  background: var(--bg);
  border: 1px solid var(--line);
  border-radius: 16px;
  padding: 20px;
}
.recovery-head h2 {
  margin: 4px 0;
  font-size: 22px;
}
.eyebrow {
  margin: 0;
  color: var(--green-dark);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.score-strip {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
  margin: 12px 0 0;
}
.score-strip div {
  padding: 8px 10px;
  border-radius: 10px;
  background: var(--surface);
  border: 1px solid var(--line);
}
.score-strip dt {
  color: var(--muted);
  font-size: 12px;
}
.score-strip dd {
  margin: 2px 0 0;
  font-weight: 700;
}
.compare {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-top: 16px;
}
.pane {
  border-radius: 12px;
  padding: 14px;
  min-height: 180px;
}
.pane h3 {
  margin: 0 0 10px;
  font-size: 16px;
}
.pane-before {
  background: var(--red-soft);
  border: 1px solid var(--red-line);
  color: var(--red-dark);
}
.pane-after {
  background: var(--green-soft);
  border: 1px solid var(--green-line);
  color: var(--green-dark);
}
.probe-list,
.node-list,
.ai-work ul {
  margin: 0;
  padding-left: 18px;
}
.probe-list li,
.node-list li,
.ai-work li {
  margin: 6px 0;
  line-height: 1.45;
}
.probe-list strong {
  display: block;
}
.ai-work {
  margin-top: 16px;
  padding: 14px;
  border-radius: 12px;
  background: var(--surface);
  border: 1px solid var(--line);
}
.ai-work h3,
.cause {
  margin: 0;
}
.cause {
  margin-top: 8px;
  font-size: 16px;
}
.summary {
  color: var(--ink-secondary);
  line-height: 1.5;
}
.local-note,
.empty-after {
  color: var(--muted);
  font-size: 12px;
}
.recovery-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
.share-url {
  flex: 1 1 240px;
  min-width: 0;
  height: 36px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--muted);
  font-family: var(--mono);
  font-size: 12px;
}
.action {
  height: 36px;
  padding: 0 14px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink);
  cursor: pointer;
}
.action:hover {
  border-color: var(--blue-line);
  color: var(--blue);
}
@media (max-width: 720px) {
  .compare {
    grid-template-columns: 1fr;
  }
}
</style>
