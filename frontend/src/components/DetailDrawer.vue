<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';

const props = withDefaults(
  defineProps<{
    modelValue: boolean;
    title: string;
    subtitle?: string;
    badgeText?: string;
    badgeTone?: string;
    tabs?: { key: string; label: string }[];
    activeTab?: string;
    rawPayload?: unknown;
    rawText?: string;
  }>(),
  {
    subtitle: '',
    badgeText: '',
    badgeTone: 'neutral',
    tabs: () => [
      { key: 'summary', label: '摘要' },
      { key: 'raw', label: '原始数据' },
    ],
    activeTab: 'summary',
    rawPayload: undefined,
    rawText: '',
  },
);

const emit = defineEmits<{
  (e: 'update:modelValue', value: boolean): void;
  (e: 'update:activeTab', value: string): void;
}>();

const copied = ref(false);
let copyTimer: ReturnType<typeof setTimeout> | undefined;
const drawerRef = ref<HTMLElement | null>(null);
const currentTab = ref(props.activeTab);

watch(
  () => props.activeTab,
  (val) => {
    currentTab.value = val;
  },
);

const formattedRaw = computed(() => {
  if (props.rawText) return props.rawText;
  if (props.rawPayload !== undefined) {
    return typeof props.rawPayload === 'string'
      ? props.rawPayload
      : JSON.stringify(props.rawPayload, null, 2);
  }
  return '';
});

function close() {
  emit('update:modelValue', false);
}

function selectTab(key: string) {
  currentTab.value = key;
  emit('update:activeTab', key);
}

async function copyRaw() {
  if (!formattedRaw.value) return;
  try {
    await navigator.clipboard.writeText(formattedRaw.value);
    copied.value = true;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copied.value = false;
    }, 1200);
    ElMessage.success('已复制到剪贴板');
  } catch {
    ElMessage.error('复制失败，请手动选取');
  }
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.modelValue) {
    close();
  }
}

watch(
  () => props.modelValue,
  (isOpen) => {
    if (isOpen) {
      document.body.style.overflow = 'hidden';
      nextTick(() => {
        drawerRef.value?.focus();
      });
    } else {
      document.body.style.overflow = '';
      copied.value = false;
    }
  },
);

onMounted(() => {
  window.addEventListener('keydown', handleKeydown);
});

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown);
  document.body.style.overflow = '';
  clearTimeout(copyTimer);
});
</script>

<template>
  <Teleport to="body">
    <Transition name="drawer-fade">
      <div v-if="modelValue" class="drawer-overlay" @click.self="close">
        <div
          ref="drawerRef"
          class="detail-drawer"
          role="dialog"
          aria-modal="true"
          :aria-label="title"
          tabindex="-1"
        >
          <header class="drawer-header">
            <div class="drawer-title-area">
              <div class="drawer-title-row">
                <h3 class="drawer-title">{{ title }}</h3>
                <span v-if="badgeText" class="state-badge" :data-tone="badgeTone">
                  {{ badgeText }}
                </span>
              </div>
              <p v-if="subtitle" class="drawer-subtitle">{{ subtitle }}</p>
            </div>
            <button class="drawer-close-btn" type="button" aria-label="关闭详情" @click="close">
              ✕
            </button>
          </header>

          <nav v-if="tabs && tabs.length > 1" class="drawer-tabs">
            <button
              v-for="t in tabs"
              :key="t.key"
              type="button"
              class="drawer-tab-btn"
              :class="{ active: currentTab === t.key }"
              @click="selectTab(t.key)"
            >
              {{ t.label }}
            </button>
          </nav>

          <div class="drawer-body">
            <div v-show="currentTab === 'summary'" class="drawer-tab-pane">
              <slot name="summary">
                <slot />
              </slot>
            </div>

            <div v-show="currentTab === 'raw'" class="drawer-tab-pane">
              <div class="drawer-raw-toolbar">
                <span class="muted text-12">原始格式数据</span>
                <button type="button" class="drawer-copy-btn" :class="{ copied }" @click="copyRaw">
                  <span v-if="copied">✓ 已复制</span>
                  <span v-else>复制原文</span>
                </button>
              </div>
              <pre class="drawer-pre">{{ formattedRaw || '无原始数据' }}</pre>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
.drawer-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.35);
  backdrop-filter: blur(2px);
  z-index: 2000;
  display: flex;
  justify-content: flex-end;
}

.detail-drawer {
  width: min(520px, calc(100vw - 32px));
  height: 100vh;
  background: var(--surface);
  box-shadow: -4px 0 24px rgba(0, 0, 0, 0.08);
  display: flex;
  flex-direction: column;
  outline: none;
  animation: drawerSlideIn 200ms cubic-bezier(0.16, 1, 0.3, 1);
}

.drawer-header {
  padding: 18px 20px;
  border-bottom: 1px solid var(--line);
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  background: var(--surface);
  flex-shrink: 0;
}

.drawer-title-area {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.drawer-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.drawer-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--ink);
  line-height: 24px;
  margin: 0;
}

.drawer-subtitle {
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
  margin: 0;
}

.drawer-close-btn {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  transition: background-color 120ms ease;
  flex-shrink: 0;
}

.drawer-close-btn:hover {
  background: var(--surface-hover);
  color: var(--ink);
}

.drawer-tabs {
  display: flex;
  gap: 20px;
  padding: 0 20px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
  flex-shrink: 0;
}

.drawer-tab-btn {
  padding: 10px 0;
  border: 0;
  background: transparent;
  color: var(--muted);
  font-size: 14px;
  position: relative;
  cursor: pointer;
  transition: color 120ms ease;
}

.drawer-tab-btn:hover {
  color: var(--ink);
}

.drawer-tab-btn.active {
  color: var(--blue);
  font-weight: 600;
}

.drawer-tab-btn.active::after {
  content: '';
  position: absolute;
  bottom: -1px;
  left: 0;
  right: 0;
  height: 2px;
  background: var(--blue);
}

.drawer-body {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
  min-height: 0;
}

.drawer-tab-pane {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.drawer-raw-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 8px;
}

.drawer-copy-btn {
  padding: 5px 12px;
  font-size: 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--ink-secondary);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: all 120ms ease;
}

.drawer-copy-btn:hover {
  border-color: var(--blue-line);
  color: var(--blue);
}

.drawer-copy-btn.copied {
  background: var(--green-soft);
  color: var(--green-dark);
  border-color: var(--green-line);
}

.drawer-pre {
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 14px;
  font-family: Consolas, 'Cascadia Code', monospace;
  font-size: 13px;
  line-height: 1.6;
  color: var(--ink);
  overflow-x: auto;
  max-height: none;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
}

@keyframes drawerSlideIn {
  from {
    transform: translateX(16px);
    opacity: 0.8;
  }
  to {
    transform: translateX(0);
    opacity: 1;
  }
}

.drawer-fade-enter-active,
.drawer-fade-leave-active {
  transition: opacity 160ms ease;
}

.drawer-fade-enter-from,
.drawer-fade-leave-to {
  opacity: 0;
}

@media (prefers-reduced-motion: reduce) {
  .detail-drawer {
    animation: none;
  }
  .drawer-fade-enter-active,
  .drawer-fade-leave-active {
    transition: none;
  }
}
</style>
