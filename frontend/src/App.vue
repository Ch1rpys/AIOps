<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import { ElButton, ElDialog, ElInput } from 'element-plus';
import AppLogo from './components/AppLogo.vue';
import { recent, recentError, recentLoading, loadRecent } from './composables/recentIncidents';
import { adminToken } from './composables/session';
import { scenarioName, shortTimeLabel, statusLabels, statusTone } from './display';

const route = useRoute();
const showSettings = ref(false);

const links = [
  { path: '/lab', label: '故障实验', icon: 'lab' },
  { path: '/environment', label: '环境管理', icon: 'env' },
  { path: '/environment?tab=probes', label: '基线探针', icon: 'probe' },
  { path: '/compiler', label: '故障编译器', icon: 'archive' },
  { path: '/twin', label: '影子世界', icon: 'env' },
  { path: '/incidents', label: '故障记录与报告', icon: 'archive' },
  { path: '/evaluation', label: '盲诊评测', icon: 'evaluate' },
];

const workbenchPath = computed(() =>
  recent.value[0] ? `/incidents/${recent.value[0].id}` : '/incidents',
);

const breadcrumb = computed(() => {
  if (route.path.endsWith('/card')) return '修复对比';
  if (route.params.id) return '智能诊断工作台';
  if (route.path === '/environment') return route.query.tab === 'probes' ? '基线探针' : '环境管理';
  if (route.path === '/incidents') return '故障记录与报告';
  if (route.path === '/evaluation') return '盲诊评测';
  if (route.path === '/compiler') return '故障编译器';
  if (route.path === '/twin') return '影子世界';
  return '故障实验';
});

function isActive(path: string) {
  return path.includes('?')
    ? route.path === '/environment' && route.query.tab === 'probes'
    : route.path === path && !(path === '/environment' && route.query.tab === 'probes');
}

// 粘贴 .env 里的整行也能识别；正常令牌是纯十六进制，不含 '='。
function onTokenInput(value: string) {
  const trimmed = value.trim();
  const matched = /^ADMIN_TOKEN\s*=\s*(.+)$/i.exec(trimmed);
  adminToken.value = matched ? matched[1].trim() : trimmed;
}

// 限制最近工单最多展示 4 条 (V1-01)
const recentDisplay = computed(() => recent.value.slice(0, 4));

onMounted(loadRecent);
watch(() => route.fullPath, loadRecent);
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <RouterLink to="/lab" class="brand-section" aria-label="AIOps 控制台">
        <AppLogo :size="28" />
        <div class="brand-info">
          <span class="brand-name">AIOps</span>
        </div>
      </RouterLink>

      <nav class="nav-menu" aria-label="主导航">
        <RouterLink :to="workbenchPath" class="nav-link" :class="{ active: !!route.params.id }">
          <span class="nav-icon" aria-hidden="true">
            <svg
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M22 12h-4l-3 9L9 3l-3 9H2" />
            </svg>
          </span>
          智能诊断工作台
        </RouterLink>

        <RouterLink
          v-for="link in links"
          :key="link.path"
          :to="link.path"
          class="nav-link"
          :class="{ active: isActive(link.path) }"
        >
          <span class="nav-icon" aria-hidden="true">
            <svg
              v-if="link.icon === 'lab'"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M10 2v7.31L4 18h16l-6-8.69V2h-4z" />
            </svg>
            <svg
              v-else-if="link.icon === 'env'"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <rect x="2" y="2" width="20" height="8" rx="2" />
              <rect x="2" y="14" width="20" height="8" rx="2" />
              <line x1="6" y1="6" x2="6.01" y2="6" />
              <line x1="6" y1="18" x2="6.01" y2="18" />
            </svg>
            <svg
              v-else-if="link.icon === 'probe'"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <circle cx="12" cy="12" r="9" />
              <path d="m12 7 2 5-5-2 3-3z" />
            </svg>
            <svg
              v-else-if="link.icon === 'evaluate'"
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M3 3v18h18" />
              <path d="m7 15 3-4 3 3 5-7" />
            </svg>
            <svg
              v-else
              width="16"
              height="16"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
            >
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
              <line x1="16" y1="13" x2="8" y2="13" />
              <line x1="16" y1="17" x2="8" y2="17" />
            </svg>
          </span>
          {{ link.label }}
        </RouterLink>
      </nav>

      <section class="recent-section">
        <div class="recent-title-bar">
          <span>最近工单</span>
          <button class="text-button" :disabled="recentLoading" @click="loadRecent">
            {{ recentLoading ? '…' : '刷新' }}
          </button>
        </div>
        <p v-if="recentError" class="inline-error" role="alert">{{ recentError }}</p>
        <div v-else-if="!recentDisplay.length" class="empty compact">
          {{ recentLoading ? '正在读取…' : '暂无工单' }}
        </div>
        <RouterLink
          v-for="item in recentDisplay"
          :key="item.id"
          :to="`/incidents/${item.id}`"
          class="recent-row"
          :class="{ active: route.params.id === item.id }"
        >
          <div class="recent-row-top">
            <span class="status-dot" :data-tone="statusTone(item.status)"></span>
            <strong>{{ scenarioName(item.scenario_key) }}</strong>
          </div>
          <div class="recent-row-bottom">
            <span>{{ statusLabels[item.status] }}</span>
            <time>{{ shortTimeLabel(item.created_at) }}</time>
          </div>
        </RouterLink>

        <RouterLink to="/incidents" class="recent-foot-link"> 全部记录 → </RouterLink>
      </section>
    </aside>

    <div class="main-wrapper">
      <header class="topbar">
        <div class="topbar-breadcrumb">
          <RouterLink to="/lab">控制台</RouterLink>
          <span>/</span>
          <strong>{{ breadcrumb }}</strong>
        </div>
        <button class="session-button" @click="showSettings = true">
          <span class="status-dot" :data-tone="adminToken.trim() ? 'info' : 'neutral'"></span>
          <span>{{ adminToken.trim() ? '令牌已输入' : '只读' }} · 设置</span>
        </button>
      </header>
      <main class="content-area"><RouterView /></main>
    </div>
  </div>

  <ElDialog v-model="showSettings" title="管理员设置" width="min(440px, 92vw)" destroy-on-close>
    <label for="admin-token" class="field-label">本机管理员令牌</label>
    <ElInput
      id="admin-token"
      :model-value="adminToken"
      type="password"
      show-password
      placeholder="请输入 ADMIN_TOKEN"
      autocomplete="off"
      @update:model-value="onTokenInput"
    />
    <p class="help-text">仅保存在本页，刷新后需重输。操作权限由服务端校验。</p>
    <template #footer>
      <ElButton v-if="adminToken" @click="adminToken = ''">清除令牌</ElButton>
      <ElButton type="primary" @click="showSettings = false">完成</ElButton>
    </template>
  </ElDialog>
</template>
