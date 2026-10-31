<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ElAlert, ElButton, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import {
  rootCauseName,
  scenarioName,
  shortId,
  shortTimeLabel,
  statusLabels,
  statusTone,
  timeLabel,
} from '../display';
import type { IncidentSummary, Scenario, ScenarioKey, Status } from '../types';

const items = ref<IncidentSummary[]>([]);
const scenarios = ref<Scenario[]>([]);
const status = ref<Status | ''>('');
const scenario = ref<ScenarioKey | ''>('');
const offset = ref(0);
const pageSize = 20;
const hasMore = ref(false);
const loading = ref(false);
const error = ref('');

let version = 0;
let disposed = false;

const hasFilters = computed(() => !!status.value || !!scenario.value);

function clearFilters() {
  status.value = '';
  scenario.value = '';
}

async function load() {
  const requestedVersion = ++version;
  loading.value = true;
  error.value = '';
  try {
    const result = await api.incidents({
      status: status.value || undefined,
      scenario: scenario.value || undefined,
      offset: offset.value,
      limit: pageSize + 1,
    });
    if (requestedVersion !== version || disposed) return;
    items.value = result.slice(0, pageSize);
    hasMore.value = result.length > pageSize;
  } catch (cause) {
    if (requestedVersion === version && !disposed) {
      items.value = [];
      hasMore.value = false;
      error.value = errorMessage(cause);
    }
  } finally {
    if (requestedVersion === version && !disposed) loading.value = false;
  }
}

watch([status, scenario], () => {
  if (offset.value) offset.value = 0;
  else load();
});

watch(offset, load);

onMounted(async () => {
  await load();
  try {
    scenarios.value = await api.scenarios();
  } catch (cause) {
    if (!disposed) error.value = errorMessage(cause);
  }
});

onUnmounted(() => {
  disposed = true;
  version++;
});
</script>

<template>
  <div class="page-header">
    <div>
      <h1>故障记录与报告</h1>
    </div>
    <ElButton :loading="loading" @click="load">刷新记录</ElButton>
  </div>

  <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />

  <section class="proto-card">
    <!-- 筛选工具栏 (V1-13) -->
    <div class="filter-bar">
      <label>
        <span>场景</span>
        <select v-model="scenario" :disabled="loading">
          <option value="">全部场景</option>
          <option v-for="item in scenarios" :key="item.key" :value="item.key">
            {{ item.name }}
          </option>
        </select>
      </label>

      <label>
        <span>状态</span>
        <select v-model="status" :disabled="loading">
          <option value="">全部状态</option>
          <option v-for="(label, value) in statusLabels" :key="value" :value="value">
            {{ label }}
          </option>
        </select>
      </label>

      <button
        v-if="hasFilters"
        type="button"
        class="text-button text-12"
        :disabled="loading"
        @click="clearFilters"
      >
        清空筛选
      </button>

      <span class="muted text-12" style="margin-left: auto"> 本页 {{ items.length }} 条记录 </span>
    </div>

    <ElSkeleton v-if="loading" :rows="6" animated />

    <div v-else-if="!items.length && !error" class="empty">
      暂无符合条件的工单。<RouterLink to="/lab" class="text-link">前往故障实验 →</RouterLink>
    </div>

    <div v-else-if="items.length" class="table-scroll">
      <table class="probe-table incidents-table">
        <thead>
          <tr>
            <th>工单 ID</th>
            <th>场景编码</th>
            <th>状态</th>
            <th>根因结论</th>
            <th>工单复测</th>
            <th>创建时间</th>
            <th style="text-align: right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in items" :key="item.id">
            <td>
              <RouterLink
                :to="`/incidents/${item.id}`"
                class="text-link mono incident-link"
                :title="item.id"
              >
                {{ shortId(item.id) }}
              </RouterLink>
              <div class="text-12" style="margin-top: 2px">
                <strong>{{ scenarioName(item.scenario_key) }}</strong>
                <span
                  v-if="item.mode === 'TEST/MOCK'"
                  class="state-badge"
                  data-tone="warning"
                  style="margin-left: 4px; font-size: 10px"
                >
                  TEST
                </span>
              </div>
            </td>
            <td class="mono">{{ item.scenario_key }}</td>
            <td>
              <span class="state-badge" :data-tone="statusTone(item.status)">
                {{ statusLabels[item.status] }}
              </span>
            </td>
            <td>
              {{ item.root_cause ? rootCauseName(item.root_cause) : '—' }}
            </td>
            <td>
              <span
                v-if="item.verification_verdict"
                class="verdict"
                :data-verdict="item.verification_verdict"
              >
                {{ item.verification_verdict }}
              </span>
              <span v-else class="muted text-12">未复测</span>
            </td>
            <td class="muted text-12" :title="timeLabel(item.created_at)">
              {{ shortTimeLabel(item.created_at) }}
            </td>
            <td style="text-align: right; white-space: nowrap">
              <RouterLink
                :to="`/incidents/${item.id}`"
                class="text-link text-12"
                style="margin-right: 10px"
              >
                查看工单
              </RouterLink>
              <a
                :href="`/api/incidents/${encodeURIComponent(item.id)}/report.md`"
                download
                class="text-link text-12"
              >
                Markdown ↓
              </a>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="pagination">
      <ElButton :disabled="!offset || loading" @click="offset = Math.max(0, offset - pageSize)">
        上一页
      </ElButton>
      <span>第 {{ Math.floor(offset / pageSize) + 1 }} 页</span>
      <ElButton :disabled="!hasMore || loading" @click="offset += pageSize"> 下一页 </ElButton>
    </div>
  </section>
</template>
