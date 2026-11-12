<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElAlert, ElButton, ElDialog, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { useEnvironment } from '../composables/environment';
import { adminToken } from '../composables/session';
import ServiceStatusBar from '../components/ServiceStatusBar.vue';
import ProbeTable from '../components/ProbeTable.vue';
import { serviceStatusText, shortId, shortTimeLabel, timeLabel, worldName } from '../display';

const route = useRoute();
const router = useRouter();
const { health, loading, error, refresh } = useEnvironment();

const tab = computed(() =>
  route.query.tab === 'probes'
    ? 'probes'
    : route.query.tab === 'twin'
      ? 'twin'
      : route.query.tab === 'logs'
        ? 'logs'
        : 'services',
);
const confirmation = ref(false);
const busy = ref(false);
const actionError = ref('');
const message = ref('');
const logWorld = ref<'primary' | 'twin'>('primary');
const logService = ref('demo-mysql');
const logText = ref('');
let resetKey = crypto.randomUUID();

const canReset = computed(
  () =>
    !!health.value?.admin_configured && !!adminToken.value.trim() && !busy.value && !loading.value,
);

const worldRows = computed(() => {
  const rows: {
    world: 'primary' | 'twin';
    name: string;
    service: { state: string; health: string; project: string };
  }[] = [];
  for (const [name, service] of Object.entries(health.value?.lab.services ?? {})) {
    rows.push({ world: 'primary', name, service });
  }
  for (const [name, service] of Object.entries(health.value?.twin?.services ?? {})) {
    rows.push({ world: 'twin', name, service });
  }
  return rows;
});

const integrations = computed(() =>
  health.value
    ? [
        {
          name: '平台 API 服务',
          verdict: health.value.platform,
          summary: 'FastAPI 服务运行就绪',
        },
        {
          name: 'Docker / Compose',
          verdict: health.value.lab.error ? 'FAIL' : 'PASS',
          summary: health.value.lab.error?.message ?? '已读取隔离容器快照',
        },
        {
          name: 'HolmesGPT',
          verdict: health.value.holmes.status,
          summary:
            health.value.holmes.reason ??
            `健康接口 HTTP ${health.value.holmes.http_status ?? '未知'}`,
        },
        {
          name: 'Planning LLM',
          verdict: health.value.planner.status,
          summary: `${health.value.planner.configured ? '配置就绪 · 联通未知' : '配置不完整'}；${health.value.planner.reason}`,
        },
      ]
    : [],
);

async function reset() {
  if (!canReset.value) return;
  confirmation.value = false;
  busy.value = true;
  actionError.value = '';
  message.value = '恢复环境并检查基线中…';
  try {
    await api.reset(resetKey);
    resetKey = crypto.randomUUID();
    message.value = '重置完成，服务端已确认四个 demo 服务及全部基线通过。';
  } catch (cause) {
    actionError.value = errorMessage(cause);
    message.value = '';
  } finally {
    busy.value = false;
    await refresh();
  }
}

async function loadLogs() {
  actionError.value = '';
  try {
    const payload = await api.worldLogs(logWorld.value, logService.value);
    logText.value = payload.text;
  } catch (cause) {
    actionError.value = errorMessage(cause);
  }
}
</script>

<template>
  <div class="page-header">
    <div>
      <h1>环境管理</h1>
    </div>
    <ElButton :loading="loading" :disabled="busy" @click="refresh">重新自检</ElButton>
  </div>

  <ElAlert
    v-if="error || actionError"
    :title="actionError || error"
    type="error"
    :closable="false"
    show-icon
  />
  <ElAlert v-if="message" :title="message" type="success" :closable="false" show-icon />

  <div v-if="health?.mode === 'TEST/MOCK'" class="test-banner">
    TEST/MOCK · 隔离测试环境，不是真实 Docker / 模型
  </div>

  <ElSkeleton v-if="loading && !health" :rows="4" animated />

  <!-- 环境单行状态条 -->
  <ServiceStatusBar :lab="health?.lab ?? null" />

  <!-- 4 依赖行状态表 (V1-12) -->
  <section class="proto-card" style="padding: 16px 20px">
    <div class="section-heading" style="margin-bottom: 12px">
      <h2>核心依赖状态</h2>
      <span class="muted text-12">自检由平台自检探针完成</span>
    </div>
    <div class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>服务组件</th>
            <th>状态判定</th>
            <th>详细说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in integrations" :key="item.name" class="integration-item">
            <td>
              <h2 style="font-size: 14px; font-weight: 600; margin: 0; line-height: 20px">
                {{ item.name }}
              </h2>
            </td>
            <td>
              <span class="verdict" :data-verdict="item.verdict">{{ item.verdict }}</span>
            </td>
            <td class="text-12 muted">{{ item.summary }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <!-- 容器 / 探针 + 实验控制 -->
  <div class="environment-layout">
    <section class="proto-card">
      <div class="section-heading">
        <div class="evidence-tabs" aria-label="环境详情标签">
          <button
            type="button"
            class="evidence-tab-btn"
            :class="{ active: tab === 'services' }"
            @click="router.replace('/environment')"
          >
            容器服务
          </button>
          <button
            type="button"
            class="evidence-tab-btn"
            :class="{ active: tab === 'probes' }"
            @click="router.replace('/environment?tab=probes')"
          >
            基线探针
          </button>
          <button
            type="button"
            class="evidence-tab-btn"
            :class="{ active: tab === 'twin' }"
            @click="router.replace('/environment?tab=twin')"
          >
            影子世界
          </button>
          <button
            type="button"
            class="evidence-tab-btn"
            :class="{ active: tab === 'logs' }"
            @click="router.replace('/environment?tab=logs')"
          >
            分界日志
          </button>
        </div>
        <span class="muted text-12"
          >采样时间: {{ health ? shortTimeLabel(health.lab.captured_at) : '—' }}</span
        >
      </div>

      <template v-if="tab === 'services'">
        <div v-if="!health || !Object.keys(health.lab.services).length" class="empty compact">
          未取得容器状态，请检查 Docker 连接。
        </div>
        <div v-else class="table-scroll">
          <table class="probe-table">
            <thead>
              <tr>
                <th>容器服务</th>
                <th>状态</th>
                <th>健康检查</th>
                <th>所属项目</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(service, name) in health.lab.services" :key="name">
                <td class="mono">
                  <strong>{{ name }}</strong>
                </td>
                <td>{{ serviceStatusText(service.state, service.health) }}</td>
                <td>{{ service.health || '未配置' }}</td>
                <td class="mono muted text-12">{{ service.project || '未知' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>

      <template v-else-if="tab === 'probes'">
        <ProbeTable :probes="health?.lab.probes ?? []" />
      </template>

      <template v-else-if="tab === 'twin'">
        <p class="help-text">
          {{ worldName('twin') }} · 锁 {{ health?.twin?.locked ? '占用中' : '空闲' }} · 项目
          {{ health?.twin?.compose_project || 'aiops-twin' }} ·
          {{ health?.twin?.status || 'UNKNOWN' }}
        </p>
        <div v-if="worldRows.length < 8" class="empty compact">
          影子未取得完整八个容器。启动失败时旧六场景仍可用，但不得声称做过试修。
        </div>
        <div v-else class="table-scroll">
          <table class="probe-table">
            <thead>
              <tr>
                <th>世界</th>
                <th>容器服务</th>
                <th>状态</th>
                <th>项目</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="row in worldRows" :key="`${row.world}-${row.name}`">
                <td>{{ worldName(row.world) }}</td>
                <td class="mono">
                  <strong>{{ row.name }}</strong>
                </td>
                <td>{{ serviceStatusText(row.service.state, row.service.health) }}</td>
                <td class="mono muted text-12">{{ row.service.project }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div style="margin-top: 12px">
          <ProbeTable :probes="health?.twin?.probes ?? []" />
        </div>
      </template>

      <template v-else>
        <p class="help-text">日志采集带 World，不会读到另一套 Compose 项目。</p>
        <div class="filter-bar">
          <label>
            世界
            <select v-model="logWorld">
              <option value="primary">真环境</option>
              <option value="twin">影子</option>
            </select>
          </label>
          <label>
            服务
            <select v-model="logService">
              <option>demo-mysql</option>
              <option>demo-redis</option>
              <option>demo-app</option>
              <option>demo-nginx</option>
            </select>
          </label>
          <ElButton @click="loadLogs">读取</ElButton>
        </div>
        <pre class="mono text-12" style="margin-top: 12px; white-space: pre-wrap">{{
          logText || '尚未读取。'
        }}</pre>
      </template>
    </section>

    <!-- 实验控制面板 (V1-12) -->
    <aside class="proto-card environment-control">
      <div class="section-heading">
        <h2>实验控制</h2>
        <span class="state-badge" :data-tone="health?.lab.healthy ? 'success' : 'warning'">
          {{ health ? (health.lab.healthy ? '基线通过' : '基线异常') : '状态未知' }}
        </span>
      </div>

      <dl class="metadata-list" style="margin-bottom: 16px">
        <dt>隔离项目</dt>
        <dd class="mono">{{ health?.lab.compose_project || '未取得' }}</dd>
        <dt>影子项目</dt>
        <dd class="mono">{{ health?.twin?.compose_project || 'aiops-twin' }}</dd>
        <dt>影子锁</dt>
        <dd>{{ health?.twin?.locked ? '占用中' : '空闲' }}</dd>
        <dt>数据模式</dt>
        <dd>{{ health?.mode || 'UNKNOWN' }}</dd>
        <dt>实验租约</dt>
        <dd>
          <RouterLink
            v-if="health?.lab.active_incident_id"
            :to="`/incidents/${health.lab.active_incident_id}`"
            class="text-link mono"
          >
            {{ shortId(health.lab.active_incident_id) }}
          </RouterLink>
          <span v-else>{{ health ? '未占用' : '未知' }}</span>
        </dd>
        <dt>采样时间</dt>
        <dd class="text-12">{{ health ? timeLabel(health.lab.captured_at) : '未取得' }}</dd>
      </dl>

      <div class="notice" style="margin-bottom: 14px">
        重置将同时收回真环境和影子，并重新执行真环境基线探针。
      </div>

      <ElButton
        style="width: 100%"
        :disabled="!canReset"
        :loading="busy"
        @click="confirmation = true"
      >
        重置演示环境
      </ElButton>

      <p
        v-if="!adminToken.trim() || !health?.admin_configured"
        class="help-text"
        style="margin-top: 8px"
      >
        重置需在右上角系统设置中输入管理员令牌。
      </p>
    </aside>
  </div>

  <!-- 重置确认弹窗 (V1-12) -->
  <ElDialog v-model="confirmation" title="确认重置演示环境" width="min(440px, 92vw)">
    <p style="font-size: 14px; line-height: 22px">
      将启动真环境与影子里的
      <strong>demo-mysql、demo-redis、demo-app 和 demo-nginx</strong>，并重新执行真环境基线探针。
    </p>
    <p class="help-text" style="margin-top: 8px">
      {{
        health?.lab.active_incident_id
          ? `当前环境租约属于工单 ${health.lab.active_incident_id}。重置将终止尚未结束的处置。`
          : '已保存的工单记录与审计数据将被安全保留。'
      }}
    </p>
    <template #footer>
      <ElButton @click="confirmation = false">取消</ElButton>
      <ElButton type="primary" :disabled="!canReset" @click="reset">确认重置</ElButton>
    </template>
  </ElDialog>
</template>
