<script setup lang="ts">
import { ref, watch } from 'vue';
import { ElPopover } from 'element-plus';
import { serviceStatusText, shortTimeLabel, timeLabel } from '../display';
import type { LabStatus } from '../types';

const props = defineProps<{ lab: LabStatus | null }>();

const serviceDefs = [
  { key: 'demo-mysql', name: 'MySQL' },
  { key: 'demo-redis', name: 'Redis' },
  { key: 'demo-app', name: 'App' },
  { key: 'demo-nginx', name: 'Nginx' },
];

const changedKeys = ref<Set<string>>(new Set());
const previousStates = ref<Record<string, string>>({});

function tone(service: string) {
  const snapshot = props.lab?.services[service];
  if (!snapshot?.project || snapshot.project !== props.lab?.compose_project) return 'warning';
  const { state, health } = snapshot;
  if (state === 'running' && health === 'healthy') return 'success';
  if (state === 'exited' || state === 'dead' || health === 'unhealthy') return 'danger';
  return 'warning';
}

watch(
  () => props.lab?.services,
  (newServices) => {
    if (!newServices) return;
    const changed = new Set<string>();
    for (const def of serviceDefs) {
      const current = `${newServices[def.key]?.state ?? ''}_${newServices[def.key]?.health ?? ''}`;
      const prev = previousStates.value[def.key];
      if (prev !== undefined && prev !== current) {
        changed.add(def.key);
      }
      previousStates.value[def.key] = current;
    }
    if (changed.size > 0) {
      changedKeys.value = changed;
      setTimeout(() => {
        changedKeys.value = new Set();
      }, 600);
    }
  },
  { deep: true },
);
</script>

<template>
  <div class="service-status-strip service-cards-bar" aria-label="当前环境状态">
    <div class="service-strip-left">
      <span class="env-strip-label">当前环境</span>
      <div class="service-strip-items">
        <ElPopover
          v-for="def in serviceDefs"
          :key="def.key"
          placement="bottom"
          :width="260"
          trigger="click"
        >
          <template #reference>
            <button
              type="button"
              class="service-chip-btn service-card-item"
              :class="{ 'state-changed': changedKeys.has(def.key) }"
              :aria-label="`${def.name} 状态详情`"
            >
              <span class="status-dot" :data-tone="tone(def.key)"></span>
              <strong>{{ def.name }}</strong>
              <div
                class="service-health-tags"
                style="display: inline-flex; align-items: center; gap: 4px"
              >
                <span
                  class="state-badge"
                  :data-tone="tone(def.key)"
                  style="font-size: 11px; padding: 1px 5px"
                >
                  {{ lab?.services[def.key]?.state || 'UNKNOWN' }}
                </span>
                <span class="muted text-12">
                  {{
                    serviceStatusText(lab?.services[def.key]?.state, lab?.services[def.key]?.health)
                  }}
                </span>
              </div>
            </button>
          </template>

          <div class="metadata-list" style="margin: 0">
            <dt>服务名称</dt>
            <dd>
              <strong>{{ def.name }}</strong>
            </dd>
            <dt>容器名称</dt>
            <dd class="mono">{{ def.key }}</dd>
            <dt>运行状态</dt>
            <dd>{{ lab?.services[def.key]?.state || '未知' }}</dd>
            <dt>健康检查</dt>
            <dd>{{ lab?.services[def.key]?.health || '未配置/未知' }}</dd>
            <dt>所属项目</dt>
            <dd class="mono">
              {{ lab?.services[def.key]?.project || lab?.compose_project || '未知' }}
            </dd>
          </div>
          <div style="margin-top: 10px; text-align: right">
            <RouterLink to="/environment" class="text-link text-12">前往环境管理 →</RouterLink>
          </div>
        </ElPopover>
      </div>
    </div>

    <ElPopover placement="bottom-end" :width="240" trigger="click">
      <template #reference>
        <button
          type="button"
          class="env-strip-time"
          style="cursor: pointer; background: transparent; border: 0"
        >
          <span class="muted">采样时间:</span>
          <span>{{ lab ? shortTimeLabel(lab.captured_at) : '未取得' }}</span>
        </button>
      </template>
      <div class="text-12">
        <div style="font-weight: 600; margin-bottom: 4px">环境采样时间</div>
        <div class="mono muted">{{ lab ? timeLabel(lab.captured_at) : '未取得环境数据' }}</div>
        <div style="margin-top: 6px; color: var(--muted); font-size: 11px">
          环境自检每 15 秒更新一次
        </div>
      </div>
    </ElPopover>
  </div>
</template>
