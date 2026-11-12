<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElAlert, ElButton, ElInput, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import type { RecipeSummary } from '../types';

const router = useRouter();
const error = ref('');
const loading = ref(false);
const recipes = ref<RecipeSummary[]>([]);
const holdout = ref<RecipeSummary[]>([]);
const showHoldout = ref(false);
const counts = ref<{
  recipe_pool_size: number | null;
  holdout_size: number | null;
  enumerated_size: number | null;
}>({
  recipe_pool_size: null,
  holdout_size: null,
  enumerated_size: null,
});
const query = ref('');
const recipeId = ref('');
const busy = ref(false);
const detail = ref<Record<string, unknown> | null>(null);

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const seen = await api.compilerRecipes('seen');
    recipes.value = seen.recipes;
    counts.value = {
      recipe_pool_size: seen.recipe_pool_size,
      holdout_size: seen.holdout_size,
      enumerated_size: seen.enumerated_size,
    };
    if (showHoldout.value && adminToken.value.trim()) {
      const hidden = await api.compilerRecipes('holdout');
      holdout.value = hidden.recipes;
    }
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    loading.value = false;
  }
}

async function loadHoldout() {
  showHoldout.value = !showHoldout.value;
  if (showHoldout.value) await load();
}

async function inspect(id: string) {
  error.value = '';
  try {
    detail.value = await api.compilerRecipe(id);
  } catch (cause) {
    error.value = errorMessage(cause);
  }
}

async function calibrate(id: string) {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    await api.compilerCalibrate(crypto.randomUUID(), id);
    await load();
    await inspect(id);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function injectSeen() {
  if (busy.value) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await api.compileInject(crypto.randomUUID(), recipeId.value.trim() || undefined);
    await router.push(`/incidents/${result.incident_id}`);
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page-header">
    <div>
      <h1>故障编译器</h1>
      <p class="muted text-12" style="margin-top: 4px">
        配方由登记原语组合生成，规范哈希冻结。holdout 是评测考题，不要在演示里注入。
      </p>
    </div>
    <ElButton :loading="loading" @click="load">刷新</ElButton>
  </div>

  <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />

  <section class="proto-card">
    <div class="section-heading">
      <h2>池子摘要</h2>
    </div>
    <dl class="metadata-list">
      <dt>枚举池</dt>
      <dd>{{ counts.enumerated_size ?? '尚未生成 jsonl' }}</dd>
      <dt>已校准总池</dt>
      <dd>{{ counts.recipe_pool_size ?? '尚未校准' }}</dd>
      <dt>Holdout</dt>
      <dd>{{ counts.holdout_size ?? '尚未冻结' }}</dd>
    </dl>
    <div class="button-row" style="margin-top: 12px">
      <ElButton type="primary" :loading="busy" :disabled="!adminToken.trim()" @click="injectSeen">
        抽一条 seen 注入工作台
      </ElButton>
      <ElInput v-model="recipeId" placeholder="或粘贴 rec_…" style="max-width: 280px" />
    </div>
    <p class="help-text">工作台会拒绝 holdout。确认框只描述可观测症状。</p>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>配方列表</h2>
      <ElInput v-model="query" placeholder="搜索编号或标题" style="max-width: 240px" />
    </div>
    <ElSkeleton v-if="loading && !recipes.length" :rows="4" animated />
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>编号</th>
            <th>标题</th>
            <th>校准</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in recipes.filter(
              (item) => !query || item.id.includes(query) || (item.title || '').includes(query),
            )"
            :key="row.id"
          >
            <td class="mono">{{ row.id }}</td>
            <td>{{ row.title || '—' }}</td>
            <td class="muted text-12">{{ row.calibrated_at || '未校准' }}</td>
            <td>
              <button class="text-button" type="button" @click="inspect(row.id)">查看</button>
              <button
                class="text-button"
                type="button"
                :disabled="!adminToken.trim() || busy"
                @click="calibrate(row.id)"
              >
                在影子里校准
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>Holdout（评测用，勿在演示注入）</h2>
      <button class="text-button" type="button" @click="loadHoldout">
        {{ showHoldout ? '收起' : '展开（需要管理员令牌）' }}
      </button>
    </div>
    <div v-if="showHoldout" class="table-scroll">
      <table class="probe-table">
        <tbody>
          <tr v-for="row in holdout" :key="row.id">
            <td class="mono">{{ row.id }}</td>
            <td>{{ row.title || '折叠详情' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>

  <section v-if="detail" class="proto-card">
    <h2>配方详情</h2>
    <pre class="mono text-12">{{ JSON.stringify(detail, null, 2) }}</pre>
  </section>
</template>
