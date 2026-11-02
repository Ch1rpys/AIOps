<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ElAlert, ElButton, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import {
  durationLabel,
  rootCauseName,
  scenarioName,
  shortId,
  shortTimeLabel,
  timeLabel,
} from '../display';
import type {
  EvaluationArm,
  EvaluationRunDetail,
  EvaluationRunRow,
  EvaluationSummary,
  ScenarioKey,
} from '../types';

const runs = ref<EvaluationRunRow[]>([]);
const summary = ref<EvaluationSummary | null>(null);
const details = ref<Record<string, EvaluationRunDetail>>({});
const expanded = ref<string>('');
const loading = ref(false);
const error = ref('');
const notice = ref('');

// 真值默认不显示：只有显式切到「评测视图」才请求带真值的返回体。
const evaluationView = ref(false);
const scenario = ref<ScenarioKey>('MYSQL_DOWN');
const arms = ref<EvaluationArm[]>(['A', 'B']);
const triggering = ref(false);

let version = 0;
let disposed = false;
let poller: number | undefined;

const canTrigger = computed(() => !!adminToken.value.trim());

const armLabel: Record<EvaluationArm, string> = {
  A: 'A · 单次诊断',
  B: 'B · 多 Agent 复核',
  C: 'C · 增加视觉证据',
};

function percent(value: number | null): string {
  if (value === null || value === undefined) return '不适用';
  return `${(value * 100).toFixed(1)}%`;
}

function truthCell(row: EvaluationRunRow): string {
  if (!evaluationView.value) return '不显示';
  if (row.truth_scenario) return scenarioName(row.truth_scenario);
  return '—';
}

async function load() {
  const requestedVersion = ++version;
  loading.value = true;
  error.value = '';
  try {
    const [list, totals] = await Promise.all([
      api.evaluationRuns({ limit: 50, evaluationView: evaluationView.value }),
      api.evaluationSummary(),
    ]);
    if (requestedVersion !== version || disposed) return;
    runs.value = list;
    summary.value = totals;
  } catch (cause) {
    if (requestedVersion === version && !disposed) {
      runs.value = [];
      summary.value = null;
      error.value = errorMessage(cause);
    }
  } finally {
    if (requestedVersion === version && !disposed) loading.value = false;
  }
}

async function toggleDetail(row: EvaluationRunRow) {
  if (expanded.value === row.id) {
    expanded.value = '';
    return;
  }
  expanded.value = row.id;
  if (details.value[row.id]) return;
  try {
    details.value = {
      ...details.value,
      [row.id]: await api.evaluationRun(row.id, evaluationView.value),
    };
  } catch (cause) {
    error.value = errorMessage(cause);
  }
}

async function trigger() {
  if (!canTrigger.value) {
    error.value = '请先在右上角「设置」中粘贴 ADMIN_TOKEN。';
    return;
  }
  triggering.value = true;
  error.value = '';
  notice.value = '';
  try {
    await api.triggerEvaluation(scenario.value, arms.value, `EVAL-${Date.now()}`);
    notice.value = '实验已触发：注入故障 → 固化证据快照 → 各臂在同一快照上诊断。';
    await load();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    triggering.value = false;
  }
}

watch(evaluationView, load);

onMounted(async () => {
  await load();
  // 实验在后台执行，完成后自动刷新列表。
  poller = window.setInterval(() => {
    if (!disposed && !document.hidden) load();
  }, 8000);
});

onUnmounted(() => {
  disposed = true;
  version++;
  if (poller) window.clearInterval(poller);
});
</script>

<template>
  <div class="page-header">
    <div>
      <h1>盲诊评测</h1>
      <p class="muted text-12" style="margin-top: 4px">
        独立链路：只诊断、不提议处置，不产生任何执行动作。真值不进入模型上下文，也不出现在工作台。
      </p>
    </div>
    <ElButton :loading="loading" @click="load">刷新</ElButton>
  </div>

  <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />
  <ElAlert v-if="notice" :title="notice" type="success" :closable="false" show-icon />

  <section class="proto-card">
    <div class="section-heading">
      <h2>触发一次盲诊实验</h2>
    </div>
    <div class="filter-bar">
      <label>
        <span>注入场景</span>
        <select v-model="scenario" :disabled="triggering">
          <option value="MYSQL_DOWN">MySQL 服务停止</option>
          <option value="REDIS_DOWN">Redis 服务停止</option>
          <option value="NGINX_502">Nginx 上游不可用</option>
        </select>
      </label>
      <label>
        <span>实验臂</span>
        <select v-model="arms" multiple :disabled="triggering" size="3">
          <option value="A">A · 单次诊断</option>
          <option value="B">B · 多 Agent 复核</option>
        </select>
      </label>
      <ElButton type="primary" :loading="triggering" :disabled="!canTrigger" @click="trigger">
        触发实验
      </ElButton>
      <span class="muted text-12" style="margin-left: auto">
        {{ canTrigger ? '实验会停止目标容器，结束后自动恢复环境' : '需要管理员令牌才能触发' }}
      </span>
    </div>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>各臂对照</h2>
    </div>
    <ElSkeleton v-if="loading && !summary" :rows="3" animated />
    <div v-else-if="!summary || !summary.arms.length" class="empty">暂无实验记录。</div>
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>实验臂</th>
            <th>有效样本</th>
            <th>根因正确率</th>
            <th>拒答率</th>
            <th>平均耗时</th>
            <th>平均模型调用</th>
            <th>错误处置提议数</th>
            <th>拦截率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="arm in summary.arms" :key="arm.arm">
            <td>{{ armLabel[arm.arm] }}</td>
            <td>
              {{ arm.valid_runs }} / {{ arm.runs }}
              <span v-if="arm.invalid_runs" class="muted text-12">
                （{{ arm.invalid_runs }} 次故障未成立，已剔除）
              </span>
            </td>
            <td>{{ percent(arm.accuracy) }}</td>
            <td>{{ percent(arm.rejection_rate) }}</td>
            <td>{{ durationLabel(arm.avg_latency_ms ?? undefined) }}</td>
            <td>{{ arm.model_calls_avg ?? '未知' }}</td>
            <td class="muted">不适用</td>
            <td class="muted">不适用</td>
          </tr>
        </tbody>
      </table>
    </div>
    <ul v-if="summary" class="help-text" style="margin-top: 10px">
      <li v-for="note in summary.notes" :key="note">{{ note }}</li>
    </ul>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>实验记录</h2>
    </div>
    <div class="filter-bar">
      <label class="inline-label">
        <input v-model="evaluationView" type="checkbox" />
        <span>评测视图（显示真值与判卷结果）</span>
      </label>
      <span class="muted text-12" style="margin-left: auto">
        真值只由评测服务持有；切换视图需要管理员令牌。
      </span>
    </div>

    <ElSkeleton v-if="loading && !runs.length" :rows="5" animated />
    <div v-else-if="!runs.length" class="empty">还没有实验记录。</div>
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>记录</th>
            <th>实验臂</th>
            <th>结论</th>
            <th>证据充分</th>
            <th>故障成立</th>
            <th>模型调用</th>
            <th>耗时</th>
            <th>时间</th>
            <th style="text-align: right">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in runs" :key="row.id">
            <tr>
              <td class="mono" :title="row.id">{{ shortId(row.id) }}</td>
              <td>{{ row.arm }}</td>
              <td>
                {{ row.root_cause ? rootCauseName(row.root_cause) : '—' }}
                <div v-if="row.summary" class="muted text-12">{{ row.summary }}</div>
              </td>
              <td>
                <span v-if="row.evidence_sufficient === null" class="muted text-12">—</span>
                <span
                  v-else
                  class="state-badge"
                  :data-tone="row.evidence_sufficient ? 'success' : 'warning'"
                >
                  {{ row.evidence_sufficient ? '是' : '否' }}
                </span>
              </td>
              <td>
                <span v-if="row.fault_present === null" class="muted text-12">未知</span>
                <span
                  v-else
                  class="state-badge"
                  :data-tone="row.fault_present ? 'success' : 'danger'"
                >
                  {{ row.fault_present ? '成立' : '未成立' }}
                </span>
              </td>
              <td>{{ row.model_calls ?? '未知' }}</td>
              <td>{{ durationLabel(row.latency_ms ?? undefined) }}</td>
              <td class="muted text-12" :title="timeLabel(row.created_at)">
                {{ shortTimeLabel(row.created_at) }}
              </td>
              <td style="text-align: right; white-space: nowrap">
                <button type="button" class="text-button text-12" @click="toggleDetail(row)">
                  {{ expanded === row.id ? '收起' : '展开' }}
                </button>
                <span v-if="evaluationView" class="muted text-12" style="margin-left: 8px">
                  真值：{{ truthCell(row) }}
                </span>
              </td>
            </tr>
            <tr v-if="expanded === row.id">
              <td colspan="9">
                <div v-if="details[row.id]" class="text-12 mono" style="white-space: pre-wrap">
                  <strong>模型原始结论</strong>
                  <pre>{{ JSON.stringify(details[row.id]?.diagnosis, null, 2) }}</pre>
                  <strong>引用的证据快照</strong>
                  <pre>{{ JSON.stringify(details[row.id]?.evidence, null, 2) }}</pre>
                </div>
                <div v-else class="muted text-12">正在读取…</div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>
