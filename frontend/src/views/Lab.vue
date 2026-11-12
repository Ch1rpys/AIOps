<script setup lang="ts">
import { computed, onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElAlert, ElButton, ElDialog, ElInput, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import { useEnvironment } from '../composables/environment';
import ServiceStatusBar from '../components/ServiceStatusBar.vue';
import ProbeTable from '../components/ProbeTable.vue';
import { probeName } from '../display';
import type { Scenario, ScenarioKey } from '../types';

const router = useRouter();
const { health, loading, error: healthError, refresh } = useEnvironment();
const scenarios = ref<Scenario[]>([]);
const scenariosLoading = ref(true);
const error = ref('');
const busy = ref(false);
const injectingKey = ref<string | null>(null);
const confirmation = ref<Scenario | null>(null);
const poison = ref(false);
const catalogCount = ref<number | null>(null);
const recipeCounts = ref<{
  recipe_pool_size: number | null;
  holdout_size: number | null;
  enumerated_size: number | null;
}>({ recipe_pool_size: null, holdout_size: null, enumerated_size: null });
const showBaselineDetails = ref(false);
const injectKeys = new Map<ScenarioKey, string>();
const compileConfirm = ref(false);
const compileKey = ref(crypto.randomUUID());
const recipeId = ref('');

const blockedReason = computed(() => {
  if (!health.value) return '尚未取得有效环境状态，请点击重新检查。';
  if (health.value.lab.active_incident_id)
    return '环境已被活动工单占用，需完成处置或重置后再注入。';
  if (!health.value.lab.healthy) return '当前环境基线未全部通过，请前往环境管理检查。';
  if (!health.value.admin_configured) return '服务端未配置管理员令牌。';
  if (!adminToken.value.trim()) return '当前为只读模式，请在右上角设置中输入管理员令牌。';
  return '';
});

// 基线探针通过统计 (V1-11)
const probeStats = computed(() => {
  const probes = health.value?.lab.probes ?? [];
  const passed = probes.filter((p) => p.verdict === 'PASS').length;
  const failedOrUnknown = probes.filter((p) => p.verdict !== 'PASS');
  return {
    total: probes.length,
    passed,
    allPassed: probes.length > 0 && passed === probes.length,
    abnormal: failedOrUnknown,
  };
});

async function loadScenarios() {
  scenariosLoading.value = true;
  error.value = '';
  try {
    scenarios.value = await api.scenarios();
    try {
      catalogCount.value = (await api.actionCatalog()).count;
    } catch {
      catalogCount.value = null;
    }
    try {
      const recipes = await api.compilerRecipes('seen');
      recipeCounts.value = {
        recipe_pool_size: recipes.recipe_pool_size,
        holdout_size: recipes.holdout_size,
        enumerated_size: recipes.enumerated_size,
      };
    } catch {
      recipeCounts.value = { recipe_pool_size: null, holdout_size: null, enumerated_size: null };
    }
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    scenariosLoading.value = false;
  }
}

async function reload() {
  await Promise.all([refresh(), loadScenarios()]);
}

function stopsContainer(scenario: Scenario): boolean {
  return scenario.key !== 'NGINX_UPSTREAM' && scenario.key !== 'APP_HTTP_500';
}

function injectLead(scenario: Scenario): string {
  if (scenario.key === 'NGINX_UPSTREAM') {
    return `不会停止容器。将把 ${scenario.target} 的上游改到一个无法连接的地址，注入 ${scenario.name}。`;
  }
  if (scenario.key === 'APP_HTTP_500') {
    return `不会停止容器。将让应用的业务接口固定返回 500，注入 ${scenario.name}。`;
  }
  if (scenario.key === 'DUAL_MYSQL_REDIS') {
    return `将同时停止 demo-mysql 和 demo-redis，注入 ${scenario.name}。先批准一个恢复动作，另一个仍失败时再批准第二次。`;
  }
  return `将停止隔离容器 ${scenario.target}，注入 ${scenario.name}。`;
}

async function inject() {
  const selected = confirmation.value;
  if (!selected || busy.value) return;
  confirmation.value = null;
  busy.value = true;
  injectingKey.value = selected.key;
  error.value = '';
  const key = injectKeys.get(selected.key) ?? crypto.randomUUID();
  injectKeys.set(selected.key, key);
  try {
    const result = await api.inject(selected.key, key, poison.value);
    injectKeys.delete(selected.key);
    await router.push(`/incidents/${result.incident_id}`);
  } catch (cause) {
    error.value = errorMessage(cause);
    await refresh();
  } finally {
    busy.value = false;
    injectingKey.value = null;
  }
}

async function injectCompiled() {
  if (busy.value) return;
  compileConfirm.value = false;
  busy.value = true;
  injectingKey.value = 'COMPILED';
  error.value = '';
  try {
    const result = await api.compileInject(compileKey.value, recipeId.value.trim() || undefined);
    compileKey.value = crypto.randomUUID();
    await router.push(`/incidents/${result.incident_id}`);
  } catch (cause) {
    error.value = errorMessage(cause);
    await refresh();
  } finally {
    busy.value = false;
    injectingKey.value = null;
  }
}

onMounted(loadScenarios);
</script>

<template>
  <div class="page-header">
    <div>
      <h1>故障实验</h1>
    </div>
    <div class="button-row">
      <RouterLink to="/environment" class="secondary-link">环境管理</RouterLink>
      <ElButton :loading="loading || scenariosLoading" :disabled="busy" @click="reload">
        重新检查
      </ElButton>
    </div>
  </div>

  <ElAlert
    v-if="error || healthError"
    :title="error || healthError"
    type="error"
    :closable="false"
    show-icon
  />

  <div v-if="health?.mode === 'TEST/MOCK'" class="test-banner">
    TEST/MOCK · 自动化测试夹具，不是真实故障记录
  </div>

  <ElSkeleton v-if="loading && !health" :rows="3" animated />

  <!-- 环境单行状态条 -->
  <ServiceStatusBar :lab="health?.lab ?? null" />

  <!-- 阻断原因提示条 (V1-11) -->
  <div v-if="blockedReason" class="notice" role="status">
    <div>{{ blockedReason }}</div>
    <RouterLink
      v-if="health?.lab.active_incident_id"
      :to="`/incidents/${health.lab.active_incident_id}`"
      class="text-link"
    >
      前往当前工单 →
    </RouterLink>
    <RouterLink v-else-if="!health?.lab.healthy" to="/environment" class="text-link">
      前往环境管理重置 →
    </RouterLink>
  </div>

  <!-- 故障场景卡片区 -->
  <div class="section-heading" style="margin-top: 8px">
    <h2>选择故障场景</h2>
    <span v-if="catalogCount" class="muted text-12">
      修复目录 {{ catalogCount }} 条。配方池 {{ recipeCounts.enumerated_size ?? '尚未枚举' }} /
      holdout {{ recipeCounts.holdout_size ?? '尚未冻结' }}。目录外的命令不会执行。
    </span>
  </div>

  <article class="scenario-card">
    <div
      style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px"
    >
      <h2>随机 seen 配方</h2>
      <span class="state-badge" data-tone="info">编译器</span>
    </div>
    <p class="scenario-desc-line">
      从已冻结的 seen 池抽一条，同时写入真环境和影子。holdout 是评测考题，这里会拒绝。
    </p>
    <div class="button-row" style="margin-top: 8px; align-items: center">
      <ElButton
        type="primary"
        :loading="injectingKey === 'COMPILED'"
        :disabled="!!blockedReason || busy || loading || scenariosLoading"
        @click="compileConfirm = true"
      >
        {{ injectingKey === 'COMPILED' ? '注入中…' : '抽一条 seen 注入' }}
      </ElButton>
      <ElInput v-model="recipeId" placeholder="或粘贴 rec_… 指定配方" style="max-width: 280px" />
    </div>
  </article>

  <ElSkeleton v-if="scenariosLoading && !scenarios.length" :rows="5" animated />
  <div v-else-if="!scenarios.length" class="empty proto-card">
    未取得场景列表。<button class="text-button" @click="loadScenarios">重新加载</button>
  </div>

  <div class="scenario-list">
    <article v-for="scenario in scenarios" :key="scenario.key" class="scenario-card">
      <div
        style="
          display: flex;
          justify-content: space-between;
          align-items: center;
          margin-bottom: 4px;
        "
      >
        <h2>{{ scenario.name }}</h2>
        <span v-if="!scenario.enabled" class="state-badge" data-tone="neutral"> 暂未开放 </span>
      </div>

      <p class="scenario-desc-line" :title="scenario.description">
        {{ scenario.description }}
      </p>

      <dl class="metadata-list" style="margin-bottom: 14px">
        <dt>目标容器</dt>
        <dd class="mono">{{ scenario.target }}</dd>
        <dt>恢复判据</dt>
        <dd>
          <span v-for="check in scenario.checks" :key="check" class="check-chip mono">
            {{ probeName(check) }}
          </span>
        </dd>
      </dl>

      <ElButton
        type="primary"
        :loading="injectingKey === scenario.key"
        :disabled="!!blockedReason || !scenario.enabled || busy || loading || scenariosLoading"
        @click="confirmation = scenario"
      >
        {{ injectingKey === scenario.key ? '注入中…' : '注入此故障' }}
      </ElButton>
    </article>
  </div>

  <!-- 注入前基线探针状态 (V1-11) -->
  <section class="proto-card">
    <div class="section-heading">
      <div style="display: flex; align-items: center; gap: 8px">
        <h2>基线探针自检</h2>
        <span class="state-badge" :data-tone="probeStats.allPassed ? 'success' : 'danger'">
          {{ probeStats.passed }}/{{ probeStats.total }} 通过
        </span>
      </div>
      <button
        type="button"
        class="text-button text-12"
        @click="showBaselineDetails = !showBaselineDetails"
      >
        {{ showBaselineDetails ? '收起探针列表 ↑' : '展开探针列表 ↓' }}
      </button>
    </div>

    <!-- 异常探针直接展示 -->
    <div
      v-if="!probeStats.allPassed && probeStats.abnormal.length"
      class="notice"
      style="margin-bottom: 12px"
    >
      <div>
        发现 {{ probeStats.abnormal.length }} 项异常探针：
        {{ probeStats.abnormal.map((p) => probeName(p.name)).join('、') }}
      </div>
      <RouterLink to="/environment?tab=probes" class="text-link">查看探针详情 →</RouterLink>
    </div>

    <!-- 探针表格折叠展示 -->
    <div v-show="showBaselineDetails">
      <ProbeTable :probes="health?.lab.probes ?? []" />
    </div>
  </section>

  <!-- 确认注入弹窗 -->
  <ElDialog
    :model-value="!!confirmation"
    title="确认注入故障"
    width="min(440px, 92vw)"
    @close="confirmation = null"
  >
    <template v-if="confirmation">
      <p style="font-size: 14px; line-height: 22px">
        {{ injectLead(confirmation) }}
      </p>
      <p class="help-text" style="margin-top: 8px">
        平台随后采集真实日志与探针。恢复仍要在工作台批准白名单动作，或在环境管理中确认重置。
      </p>
      <label class="inline-label" style="margin-top: 12px">
        <input v-model="poison" type="checkbox" />
        <span>附带一段错误日志。它只作为证据，不能当成要执行的指令。</span>
      </label>
    </template>
    <template #footer>
      <ElButton @click="confirmation = null">取消</ElButton>
      <ElButton type="danger" :disabled="busy || loading || !!blockedReason" @click="inject">
        {{ confirmation && stopsContainer(confirmation) ? '确认停止并注入' : '确认注入' }}
      </ElButton>
    </template>
  </ElDialog>

  <ElDialog v-model="compileConfirm" title="确认注入编译器配方" width="min(440px, 92vw)">
    <p style="font-size: 14px; line-height: 22px">
      {{
        recipeId.trim()
          ? `将注入指定配方 ${recipeId.trim()}，同时写入真环境 aiops-demo 和影子 aiops-twin。`
          : '将抽一条 seen 配方，同时写入真环境 aiops-demo 和影子 aiops-twin。'
      }}
      确认框只描述可观测症状，不会展示 holdout 原语。
    </p>
    <p class="help-text" style="margin-top: 8px">
      这不是演示按钮里的六种旧题。若抽到的编号属于 holdout，服务端会拒绝并说明这是评测考题。
    </p>
    <template #footer>
      <ElButton @click="compileConfirm = false">取消</ElButton>
      <ElButton
        type="danger"
        :disabled="busy || loading || !!blockedReason"
        @click="injectCompiled"
      >
        确认双侧注入
      </ElButton>
    </template>
  </ElDialog>
</template>
