<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { ElAlert, ElButton, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import { useEnvironment } from '../composables/environment';
import ServiceStatusBar from '../components/ServiceStatusBar.vue';
import ProbeTable from '../components/ProbeTable.vue';
import { serviceStatusText, trialPredictionText } from '../display';
import type { TwinTrial } from '../types';

const { health, loading, error, refresh } = useEnvironment();
const actionError = ref('');
const message = ref('');
const busy = ref(false);
const logText = ref('');
const logService = ref('demo-mysql');
const trials = ref<TwinTrial[]>([]);

async function ensure() {
  busy.value = true;
  actionError.value = '';
  try {
    await api.twinEnsure(crypto.randomUUID());
    message.value = '已请求拉起影子世界。';
    await refresh();
  } catch (cause) {
    actionError.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function resetTwin() {
  busy.value = true;
  actionError.value = '';
  try {
    await api.twinReset(crypto.randomUUID());
    message.value = '影子已请求重置到基线。';
    await refresh();
  } catch (cause) {
    actionError.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function loadLogs() {
  try {
    const payload = await api.worldLogs('twin', logService.value);
    logText.value = payload.text;
  } catch (cause) {
    actionError.value = errorMessage(cause);
  }
}

async function loadTrials() {
  try {
    trials.value = (await api.twinTrials()).trials;
  } catch {
    trials.value = [];
  }
}

onMounted(async () => {
  await refresh();
  await loadTrials();
});
</script>

<template>
  <div class="page-header">
    <div>
      <h1>影子世界</h1>
      <p class="muted text-12" style="margin-top: 4px">
        独立 Compose 项目 aiops-twin。Holmes 不在这里。试修只跑目录动作，人只批准真环境那一次。
      </p>
    </div>
    <ElButton :loading="loading" @click="refresh">重新自检</ElButton>
  </div>

  <ElAlert
    v-if="error || actionError"
    :title="actionError || error"
    type="error"
    :closable="false"
    show-icon
  />
  <ElAlert v-if="message" :title="message" type="success" :closable="false" show-icon />

  <ServiceStatusBar :lab="health?.lab ?? null" />

  <section class="proto-card">
    <div class="section-heading">
      <h2>影子容器</h2>
      <span
        class="state-badge"
        :data-tone="health?.twin?.status === 'PASS' ? 'success' : 'warning'"
      >
        {{ health?.twin?.status || 'UNKNOWN' }}
      </span>
    </div>
    <p class="help-text">
      锁：{{ health?.twin?.locked ? '占用中' : '空闲' }} · 项目
      {{ health?.twin?.compose_project || 'aiops-twin' }}
    </p>
    <ElSkeleton v-if="loading && !health" :rows="3" animated />
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>服务</th>
            <th>状态</th>
            <th>项目</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(service, name) in health?.twin?.services || {}" :key="name">
            <td class="mono">{{ name }}</td>
            <td>{{ serviceStatusText(service.state, service.health) }}</td>
            <td class="mono muted">{{ service.project }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="button-row" style="margin-top: 12px">
      <ElButton :disabled="!adminToken.trim() || busy" @click="ensure">拉起影子</ElButton>
      <ElButton :disabled="!adminToken.trim() || busy" @click="resetTwin">重置影子基线</ElButton>
    </div>
  </section>

  <section class="proto-card">
    <div class="section-heading"><h2>影子探针</h2></div>
    <ProbeTable :probes="health?.twin?.probes ?? []" />
  </section>

  <section class="proto-card">
    <div class="section-heading"><h2>影子日志</h2></div>
    <label>
      服务
      <select v-model="logService">
        <option>demo-mysql</option>
        <option>demo-redis</option>
        <option>demo-app</option>
        <option>demo-nginx</option>
      </select>
    </label>
    <ElButton style="margin-left: 8px" @click="loadLogs">读取</ElButton>
    <pre class="mono text-12" style="margin-top: 12px; white-space: pre-wrap">{{
      logText || '尚未读取。日志采集带 World，不会读到真环境项目。'
    }}</pre>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>最近试修</h2>
      <button class="text-button" type="button" @click="loadTrials">刷新列表</button>
    </div>
    <div v-if="!trials.length" class="empty compact">还没有试修记录。</div>
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>配方</th>
            <th>动作</th>
            <th>结果</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, index) in trials" :key="row.recipe_id + String(index)">
            <td class="mono">{{ row.recipe_id }}</td>
            <td class="mono">{{ row.action_id }}</td>
            <td>{{ trialPredictionText(row) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
