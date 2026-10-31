<script setup lang="ts">
import { computed, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElAlert, ElButton, ElDialog, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { useEnvironment } from '../composables/environment';
import { adminToken } from '../composables/session';
import ServiceStatusBar from '../components/ServiceStatusBar.vue';
import ProbeTable from '../components/ProbeTable.vue';
import { serviceStatusText, shortId, shortTimeLabel, timeLabel } from '../display';

const route = useRoute();
const router = useRouter();
const { health, loading, error, refresh } = useEnvironment();

const tab = computed(() => (route.query.tab === 'probes' ? 'probes' : 'services'));
const confirmation = ref(false);
const busy = ref(false);
const actionError = ref('');
const message = ref('');
let resetKey = crypto.randomUUID();

const canReset = computed(
  () =>
    !!health.value?.admin_configured && !!adminToken.value.trim() && !busy.value && !loading.value,
);

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

      <template v-else>
        <ProbeTable :probes="health?.lab.probes ?? []" />
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
        重置将恢复四个 demo 服务并重新执行基线探针。
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
      将启动隔离项目内的
      <strong>demo-mysql、demo-redis、demo-app 和 demo-nginx</strong>，并重新执行全部基线探针。
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
