<script setup lang="ts">
import { computed, ref } from 'vue';
import { durationLabel, objectValue } from '../display';
import type { AuditRecord, Incident } from '../types';

const props = defineProps<{ incident: Incident }>();
const emit = defineEmits<{
  (e: 'inspect-step', payload: { stageName: string; step?: AuditRecord }): void;
  (e: 'inspect-history'): void;
}>();

const stages = [
  { phase: 'COLLECTING', label: '采证' },
  { phase: 'DIAGNOSING', label: '诊断' },
  { phase: 'PLANNING', label: '规划' },
  { phase: 'APPROVAL', label: '审批' },
  { phase: 'EXECUTING', label: '执行' },
  { phase: 'VERIFYING', label: '复测' },
];

const steps = computed(() => props.incident.records.filter((r) => r.kind === 'step'));

// 轮次计算：是否有第 2 轮记录
const maxAttempt = computed(() => {
  const attempts = steps.value.map((s) => Number(s.payload.attempt || 1));
  return Math.max(1, props.incident.detail.attempt || 1, ...attempts);
});

const currentViewAttempt = ref<number>(props.incident.detail.attempt || 1);

const totalDuration = computed(() => {
  const matching = steps.value.filter(
    (s) => Number(s.payload.attempt || 1) === currentViewAttempt.value,
  );
  if (!matching.length) return undefined;
  return matching.reduce(
    (sum, step) =>
      sum + (typeof step.payload.elapsed_ms === 'number' ? step.payload.elapsed_ms : 0),
    0,
  );
});

function stageSteps(phase: string): AuditRecord[] {
  return steps.value.filter(
    (step) =>
      step.payload.phase === phase &&
      Number(step.payload.attempt || 1) === currentViewAttempt.value,
  );
}

function stageElapsed(phase: string): number | undefined {
  const timed = stageSteps(phase).filter((step) => typeof step.payload.elapsed_ms === 'number');
  if (!timed.length) return undefined;
  // 同一阶段可能有多步，例如诊断之后还有执行许可校验。
  // 阶段耗时是这些步骤的合计，不能只取最后一步。
  return timed.reduce((sum, step) => sum + Number(step.payload.elapsed_ms), 0);
}

function representativeStep(phase: string): AuditRecord | undefined {
  const matching = stageSteps(phase);
  const failed = matching.find((step) => step.payload.status === 'FAILED');
  if (failed) return failed;
  return matching.find((step) => step.payload.step_kind === phase) ?? matching.at(-1);
}

function flowState(
  phase: string,
): 'done' | 'current' | 'waiting' | 'failed' | 'unknown' | 'pending' {
  const attempt = currentViewAttempt.value;
  const isLatestAttempt = attempt === (props.incident.detail.attempt || 1);
  const match = stageSteps(phase).at(-1);

  if (match?.payload.status === 'FAILED') return 'failed';

  if (phase === 'VERIFYING' && match) {
    const verification = objectValue(objectValue(match.payload.output).verification);
    if (verification.verdict === 'FAIL') return 'failed';
    if (verification.verdict === 'UNKNOWN') return 'unknown';
  }

  // 审批中
  if (isLatestAttempt && props.incident.status === 'WAITING_APPROVAL' && phase === 'APPROVAL') {
    return 'waiting';
  }

  // 当前运行节点
  const currentPhaseMap: Record<string, string> = {
    COLLECTING: 'COLLECTING',
    DIAGNOSING: 'DIAGNOSING',
    PLANNING: 'PLANNING',
    EXECUTING: 'EXECUTING',
    VERIFYING: 'VERIFYING',
  };
  if (isLatestAttempt && props.incident.status === currentPhaseMap[phase]) {
    return 'current';
  }

  if (match) return 'done';
  return 'pending';
}

function subText(phase: string, state: string): string {
  if (state === 'current') return '进行中';
  if (state === 'waiting') return '待审批';
  if (state === 'failed') return '失败';
  if (state === 'unknown') return '未知';
  if (state === 'done') {
    const elapsed = stageElapsed(phase);
    if (elapsed !== undefined) return durationLabel(elapsed);
    return '已完成';
  }
  return '';
}

function handleNodeClick(stage: { phase: string; label: string }) {
  const step = representativeStep(stage.phase);
  emit('inspect-step', { stageName: stage.label, step });
}

// 计算每段连线状态，杜绝连线中间断层
function segmentDone(index: number): boolean {
  if (index >= stages.length - 1) return false;
  const current = flowState(stages[index].phase);
  const next = flowState(stages[index + 1].phase);
  return current === 'done' && (next === 'done' || next === 'current' || next === 'waiting');
}
</script>

<template>
  <section class="proto-card workflow-compact-card">
    <div class="workflow-header-row">
      <div class="workflow-header-left">
        <h2>处置流程</h2>
        <span class="workflow-attempt-badge">
          第 {{ currentViewAttempt }}/{{ Math.max(2, maxAttempt) }} 轮
        </span>
        <button
          v-if="maxAttempt > 1"
          type="button"
          class="text-button text-12 attempt-switch-btn"
          @click="currentViewAttempt = currentViewAttempt === 1 ? 2 : 1"
        >
          切换至第 {{ currentViewAttempt === 1 ? 2 : 1 }} 轮
        </button>
      </div>

      <div class="workflow-header-right">
        <span v-if="totalDuration !== undefined" class="muted text-12">
          累计耗时 <strong>{{ durationLabel(totalDuration) }}</strong>
        </span>
        <button
          type="button"
          class="text-button text-12 history-link-btn"
          @click="emit('inspect-history')"
        >
          运行记录 ({{ steps.length }}) →
        </button>
      </div>
    </div>

    <div class="workflow-track-wrapper">
      <ol class="workflow-stages-track">
        <li
          v-for="(stage, index) in stages"
          :key="stage.phase"
          class="stage-node-item"
          :data-state="flowState(stage.phase)"
        >
          <!-- 节点间的水平连接线段 (前导线) -->
          <span
            v-if="index > 0"
            class="stage-line-segment stage-line-prev"
            :class="{ 'segment-done': segmentDone(index - 1) }"
            aria-hidden="true"
          ></span>

          <!-- 节点间的水平连接线段 (后置线) -->
          <span
            v-if="index < stages.length - 1"
            class="stage-line-segment stage-line-next"
            :class="{ 'segment-done': segmentDone(index) }"
            aria-hidden="true"
          ></span>

          <button
            type="button"
            class="stage-node-btn"
            :aria-label="`查看${stage.label}阶段详情`"
            @click="handleNodeClick(stage)"
          >
            <span class="stage-circle">
              <!-- 完成态：SVG精细对勾 -->
              <svg
                v-if="flowState(stage.phase) === 'done'"
                class="stage-icon-svg"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path
                  d="M3.5 8.5L6.5 11.5L12.5 4.5"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                  stroke-linejoin="round"
                />
              </svg>

              <!-- 进行态：旋转动画 -->
              <span
                v-else-if="flowState(stage.phase) === 'current'"
                class="stage-spinner"
                aria-hidden="true"
              ></span>

              <!-- 等待审批态：SVG 感叹号 -->
              <svg
                v-else-if="flowState(stage.phase) === 'waiting'"
                class="stage-icon-svg"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path
                  d="M8 3.5V9.5"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                />
                <circle cx="8" cy="12.5" r="1.2" fill="currentColor" />
              </svg>

              <!-- 失败态：SVG 叉号 -->
              <svg
                v-else-if="flowState(stage.phase) === 'failed'"
                class="stage-icon-svg"
                viewBox="0 0 16 16"
                fill="none"
              >
                <path
                  d="M4.5 4.5L11.5 11.5M11.5 4.5L4.5 11.5"
                  stroke="currentColor"
                  stroke-width="2"
                  stroke-linecap="round"
                />
              </svg>

              <!-- 未知态：问号 -->
              <span v-else-if="flowState(stage.phase) === 'unknown'" class="stage-text-icon"
                >?</span
              >

              <!-- 默认待执行：序号 -->
              <span v-else class="stage-text-icon">{{ index + 1 }}</span>
            </span>

            <strong class="stage-label-text">{{ stage.label }}</strong>
            <span class="stage-sub-text">
              {{ subText(stage.phase, flowState(stage.phase)) }}
            </span>
          </button>
        </li>
      </ol>
    </div>
  </section>
</template>
