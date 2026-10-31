<script setup lang="ts">
import { computed, ref } from 'vue';
import { evidenceKindLabels, objectValue, shortTimeLabel } from '../display';
import type { AuditRecord, Json } from '../types';

const props = defineProps<{
  records: AuditRecord[];
  referenceIds: string[];
  highlightId?: string;
}>();

const emit = defineEmits<{
  (e: 'inspect-evidence', record: AuditRecord): void;
}>();

const categoryFilter = ref<'all' | 'probe' | 'logs' | 'container_state' | 'runbook'>('all');
const onlyCited = ref(false);
const expandedAll = ref(false);

const allEvidence = computed(() => props.records.filter((r) => r.kind === 'evidence'));

const filteredEvidence = computed(() => {
  return allEvidence.value.filter((record) => {
    const kind = String(record.payload.kind || '');
    if (categoryFilter.value !== 'all' && kind !== categoryFilter.value) {
      return false;
    }
    if (onlyCited.value && !props.referenceIds.includes(String(record.payload.id))) {
      return false;
    }
    return true;
  });
});

// 排序规则：被当前诊断引用的优先，其余按时间倒序
const sortedEvidence = computed(() => {
  return [...filteredEvidence.value].sort((a, b) => {
    const aCited = props.referenceIds.includes(String(a.payload.id));
    const bCited = props.referenceIds.includes(String(b.payload.id));
    if (aCited && !bCited) return -1;
    if (!aCited && bCited) return 1;
    const aTime = new Date(String(a.payload.captured_at || a.created_at)).getTime();
    const bTime = new Date(String(b.payload.captured_at || b.created_at)).getTime();
    return bTime - aTime;
  });
});

const displayRows = computed(() => {
  if (expandedAll.value) return sortedEvidence.value;
  return sortedEvidence.value.slice(0, 5);
});

function probeVerdict(rawJson: Json | undefined): string {
  const verdict = objectValue(rawJson).verdict;
  return typeof verdict === 'string' ? verdict : '';
}

function extractSummary(record: AuditRecord): string {
  const kind = String(record.payload.kind || '');
  const raw = objectValue(record.payload.raw);

  if (kind === 'container_state') {
    const entries = Object.entries(raw);
    if (!entries.length) return '容器状态快照';
    return entries
      .map(([name, val]) => {
        const s = objectValue(val);
        const stateStr =
          s.state === 'running'
            ? '运行中'
            : s.state === 'exited'
              ? '已停止'
              : String(s.state || '未知');
        const healthStr = s.health === 'healthy' ? '健康' : s.health === 'unhealthy' ? '异常' : '';
        return `${name} · ${stateStr}${healthStr ? ` (${healthStr})` : ''}`;
      })
      .join('；');
  }

  if (kind === 'probe') {
    const verdict = String(raw.verdict || '');
    const observed = objectValue(raw.observed);
    if (raw.error) return `异常: ${raw.error}`;
    if (observed.status_code) return `HTTP ${observed.status_code}`;
    if (observed.ping) return `PING: ${observed.ping}`;
    if (observed.response) return `返回: ${String(observed.response).slice(0, 40)}`;
    if (observed.db) return `DB: ${observed.db}`;
    return verdict ? `探针判定 ${verdict}` : '探针采样记录';
  }

  if (kind === 'logs') {
    if (typeof raw.text === 'string') {
      const lines = raw.text
        .trim()
        .split('\n')
        .filter((l) => l.trim());
      const lastLine = lines.at(-1) || '';
      return lastLine.length > 70 ? `${lastLine.slice(0, 70)}…` : lastLine;
    }
    return '服务日志记录';
  }

  if (kind === 'runbook') {
    const filename = raw.filename || raw.title;
    if (filename) return String(filename);
    if (raw.sha256) return `SHA256: ${String(raw.sha256).slice(0, 12)}…`;
    return 'Runbook 处置手册';
  }

  return '查看原始观测数据';
}
</script>

<template>
  <section class="proto-card">
    <div class="section-heading">
      <div style="display: flex; align-items: center; gap: 8px">
        <h2>工单历史证据</h2>
        <span class="state-badge" data-tone="neutral">证据 {{ allEvidence.length }}</span>
      </div>
      <span class="muted text-12">历史采样按采集时间保留</span>
    </div>

    <!-- 筛选工具栏 (V1-07) -->
    <div class="evidence-filter-bar">
      <div class="evidence-tabs">
        <button
          type="button"
          class="evidence-tab-btn"
          :class="{ active: categoryFilter === 'all' }"
          @click="categoryFilter = 'all'"
        >
          全部
        </button>
        <button
          type="button"
          class="evidence-tab-btn"
          :class="{ active: categoryFilter === 'probe' }"
          @click="categoryFilter = 'probe'"
        >
          探针
        </button>
        <button
          type="button"
          class="evidence-tab-btn"
          :class="{ active: categoryFilter === 'logs' }"
          @click="categoryFilter = 'logs'"
        >
          日志
        </button>
        <button
          type="button"
          class="evidence-tab-btn"
          :class="{ active: categoryFilter === 'container_state' }"
          @click="categoryFilter = 'container_state'"
        >
          容器
        </button>
        <button
          type="button"
          class="evidence-tab-btn"
          :class="{ active: categoryFilter === 'runbook' }"
          @click="categoryFilter = 'runbook'"
        >
          Runbook
        </button>
      </div>

      <label
        style="
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-size: 13px;
          color: var(--ink-secondary);
          cursor: pointer;
        "
      >
        <input v-model="onlyCited" type="checkbox" style="cursor: pointer" />
        <span>仅看诊断引用 ({{ referenceIds.length }})</span>
      </label>
    </div>

    <!-- 紧凑证据列表 (48-56px 行高，严格对齐) -->
    <div v-if="!sortedEvidence.length" class="empty compact">
      {{ onlyCited ? '暂无被当前诊断引用的证据' : '暂无符合筛选条件的证据' }}
    </div>

    <div v-else class="evidence-compact-list">
      <div
        v-for="record in displayRows"
        :id="`evidence-${record.payload.id}`"
        :key="record.id"
        class="evidence-row-item"
        :class="{ 'highlight-citation': highlightId === String(record.payload.id) }"
      >
        <div class="evidence-source-col">
          <span class="evidence-kind-tag">{{
            evidenceKindLabels[String(record.payload.kind)] || record.payload.kind
          }}</span>
          <span class="evidence-source-text">{{ record.payload.source }}</span>
        </div>

        <div class="evidence-row-summary">
          {{ extractSummary(record) }}
        </div>

        <div class="evidence-badge-col">
          <span
            v-if="referenceIds.includes(String(record.payload.id))"
            class="state-badge"
            data-tone="info"
          >
            诊断引用
          </span>
          <span
            v-else-if="probeVerdict(record.payload.raw)"
            class="verdict"
            :data-verdict="probeVerdict(record.payload.raw)"
          >
            {{ probeVerdict(record.payload.raw) }}
          </span>
        </div>

        <div class="evidence-time-col">
          {{ shortTimeLabel(String(record.payload.captured_at || record.created_at)) }}
        </div>

        <div class="evidence-action-col">
          <button type="button" class="evidence-view-btn" @click="emit('inspect-evidence', record)">
            查看
          </button>
        </div>
      </div>

      <!-- 展开收起控制器 -->
      <div v-if="sortedEvidence.length > 5" class="evidence-list-footer">
        <button type="button" class="text-button text-12" @click="expandedAll = !expandedAll">
          {{ expandedAll ? '收起列表 ↑' : `查看全部 ${sortedEvidence.length} 条 ↓` }}
        </button>
      </div>
    </div>
  </section>
</template>
