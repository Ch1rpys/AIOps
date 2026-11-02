<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { useRoute } from 'vue-router';
import {
  ElAlert,
  ElButton,
  ElInput,
  ElMessage,
  ElPopconfirm,
  ElPopover,
  ElSkeleton,
} from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import { updateRecent } from '../composables/recentIncidents';
import { useEnvironment } from '../composables/environment';
import {
  actionName,
  durationLabel,
  evidenceKindLabels,
  objectValue,
  probeName,
  riskName,
  rootCauseName,
  scenarioName,
  shortId,
  shortTimeLabel,
  statusLabels,
  statusTone,
  terminalStatuses,
  timeLabel,
} from '../display';
import ServiceStatusBar from '../components/ServiceStatusBar.vue';
import EvidencePanel from '../components/EvidencePanel.vue';
import WorkflowTrace from '../components/WorkflowTrace.vue';
import VerificationPanel from '../components/VerificationPanel.vue';
import DetailDrawer from '../components/DetailDrawer.vue';
import type { AuditRecord, Incident, Json, Probe } from '../types';

const route = useRoute();
const id = computed(() => String(route.params.id));
const incident = ref<Incident | null>(null);
const { health, error: environmentError } = useEnvironment();

const fetchError = ref('');
const actionError = ref('');
const fetching = ref(false);
const busy = ref(false);

const comment = ref('');
const showCommentInput = ref(false);
const expandReason = ref(false);
const copiedId = ref(false);
let copyTimer: ReturnType<typeof setTimeout> | undefined;

// 高亮证据 ID
const highlightedEvidenceId = ref<string>('');

// 统一详情抽屉状态 (V1-08)
const drawerVisible = ref(false);
const drawerTitle = ref('');
const drawerSubtitle = ref('');
const drawerBadgeText = ref('');
const drawerBadgeTone = ref('neutral');
const drawerActiveTab = ref('summary');
const drawerRawPayload = ref<unknown>(null);
const drawerMode = ref<'step' | 'steps_all' | 'evidence' | 'diagnosis' | 'verification' | 'audit'>(
  'evidence',
);

// 当前选中的实体
const inspectedStep = ref<AuditRecord | null>(null);
const inspectedEvidence = ref<AuditRecord | null>(null);
const inspectedStageName = ref('');

const diagnosisSummary = computed(() => {
  const text = incident.value?.detail.diagnosis?.summary ?? '';
  const firstParagraph = text.split('\n').find((line) => line.trim()) ?? text;
  return firstParagraph.length > 180 ? `${firstParagraph.slice(0, 180)}…` : firstParagraph;
});

const approval = computed(() =>
  incident.value?.records
    .filter(
      (record) =>
        record.kind === 'approval' && record.payload.plan_id === incident.value?.detail.plan_id,
    )
    .at(-1),
);

const execution = computed(() =>
  incident.value?.records
    .filter(
      (record) =>
        record.kind === 'execution' && record.payload.plan_id === incident.value?.detail.plan_id,
    )
    .at(-1),
);

const canAct = computed(
  () =>
    !!adminToken.value.trim() &&
    !!health.value?.admin_configured &&
    !busy.value &&
    !fetchError.value,
);

const blockedReason = computed(() => {
  if (!health.value?.admin_configured) return '服务端未配置 ADMIN_TOKEN';
  if (!adminToken.value.trim()) return '当前为只读模式，请在右上角设置中输入管理员令牌';
  if (busy.value) return '操作执行中…';
  return '';
});

let timer: ReturnType<typeof setTimeout> | undefined;
let disposed = false;
let version = 0;

async function load() {
  clearTimeout(timer);
  const requestedId = id.value;
  const requestedVersion = ++version;
  fetching.value = true;
  try {
    const result = await api.incident(requestedId);
    if (requestedId !== id.value || requestedVersion !== version || disposed) return;
    incident.value = result;
    updateRecent(result);
    fetchError.value = '';
  } catch (cause) {
    if (requestedVersion === version && !disposed) fetchError.value = errorMessage(cause);
  } finally {
    if (requestedVersion === version && !disposed) {
      fetching.value = false;
      if (
        fetchError.value ||
        (incident.value && !terminalStatuses.includes(incident.value.status))
      ) {
        timer = setTimeout(load, 2000);
      }
    }
  }
}

async function act(decision?: 'approve' | 'reject') {
  if (!canAct.value || !incident.value) return;
  const requestedId = id.value;
  const planId = incident.value.detail.plan_id;
  busy.value = true;
  actionError.value = '';
  try {
    if (decision) {
      if (!planId) return;
      await api.approve(requestedId, planId, decision, comment.value);
      ElMessage.success(
        decision === 'approve'
          ? '审批已提交，执行和复测结果以服务端记录为准。'
          : '拒绝决定已提交。',
      );
    } else {
      await api.diagnose(requestedId);
      ElMessage.success('诊断任务已提交。');
    }
    if (requestedId === id.value && !disposed) await load();
  } catch (cause) {
    if (requestedId === id.value && !disposed) actionError.value = errorMessage(cause);
  } finally {
    busy.value = false;
  }
}

async function copyId() {
  try {
    await navigator.clipboard.writeText(id.value);
    copiedId.value = true;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copiedId.value = false;
    }, 1200);
    ElMessage.success('工单 ID 已复制');
  } catch {
    ElMessage.error('复制失败，请手动选取');
  }
}

// M3：候选根因、请求补采的理由与「新增证据排除了什么」
const candidates = computed(() => incident.value?.detail.diagnosis?.candidates ?? []);
const requestedChecks = computed(() => incident.value?.detail.diagnosis?.requested_checks ?? []);
const collectedSources = computed(() =>
  (incident.value?.detail.supplement?.collected ?? []).map((item) => item.source ?? ''),
);
const hasCandidateDetail = computed(
  () =>
    candidates.value.length > 0 ||
    requestedChecks.value.length > 0 ||
    !!incident.value?.detail.supplement,
);

function evidenceSource(id: string): string {
  const record = incident.value?.records.find(
    (item) => item.kind === 'evidence' && item.payload.id === id,
  );
  return record ? String(record.payload.source ?? id) : id;
}

// 依据 n 条定位处理 (V1-06)
function locateCitations() {
  const ids = incident.value?.detail.diagnosis?.evidence_ids ?? [];
  if (!ids.length) {
    ElMessage.info('暂无关联的证据引用');
    return;
  }
  const firstId = ids[0];
  highlightedEvidenceId.value = firstId;
  const el = document.getElementById(`evidence-${firstId}`);
  if (el) {
    el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  setTimeout(() => {
    highlightedEvidenceId.value = '';
  }, 1800);
}

// 辅助类型提取
function asNumber(val: unknown): number | undefined {
  return typeof val === 'number' ? val : undefined;
}

function getSampleProbes(record: AuditRecord): Probe[] {
  if (Array.isArray(record.payload.probes)) {
    return (record.payload.probes as unknown[]).map((raw) => {
      const p = objectValue(raw as Json);
      return {
        name: String(p.name || ''),
        verdict: (p.verdict === 'PASS' || p.verdict === 'FAIL' || p.verdict === 'UNKNOWN'
          ? p.verdict
          : 'UNKNOWN') as Probe['verdict'],
        observed: objectValue(p.observed),
        captured_at: typeof p.captured_at === 'string' ? p.captured_at : record.created_at,
      };
    });
  }
  return [];
}

// 抽屉打开器 (V1-08)
function openStepDrawer(payload: { stageName: string; step?: AuditRecord }) {
  drawerMode.value = 'step';
  inspectedStageName.value = payload.stageName;
  inspectedStep.value = payload.step ?? null;
  drawerTitle.value = `${payload.stageName}阶段详情`;
  drawerSubtitle.value = payload.step
    ? `记录 ID: ${shortId(payload.step.id)}`
    : '本阶段尚未生成执行记录';
  drawerBadgeText.value = payload.step ? String(payload.step.payload.status || 'DONE') : '未执行';
  drawerBadgeTone.value = payload.step?.payload.status === 'FAILED' ? 'danger' : 'info';
  drawerRawPayload.value = payload.step ? payload.step.payload : { message: '尚未执行' };
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

function openHistoryDrawer() {
  drawerMode.value = 'steps_all';
  drawerTitle.value = '节点运行轨迹记录';
  const steps = incident.value?.records.filter((r) => r.kind === 'step') ?? [];
  drawerSubtitle.value = `共 ${steps.length} 次阶段状态流转`;
  drawerBadgeText.value = `${steps.length} 节点`;
  drawerBadgeTone.value = 'info';
  drawerRawPayload.value = steps.map((s) => s.payload);
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

function openEvidenceDrawer(record: AuditRecord) {
  drawerMode.value = 'evidence';
  inspectedEvidence.value = record;
  const kindLabel = evidenceKindLabels[String(record.payload.kind)] || record.payload.kind;
  drawerTitle.value = `${kindLabel} · ${record.payload.source}`;
  drawerSubtitle.value = `采集时间: ${timeLabel(String(record.payload.captured_at || record.created_at))}`;
  const isCited = (incident.value?.detail.diagnosis?.evidence_ids ?? []).includes(
    String(record.payload.id),
  );
  drawerBadgeText.value = isCited ? '诊断引用' : String(record.payload.kind);
  drawerBadgeTone.value = isCited ? 'info' : 'neutral';
  drawerRawPayload.value = record.payload.raw ?? record.payload;
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

function openDiagnosisDrawer() {
  if (!incident.value?.detail.diagnosis) return;
  drawerMode.value = 'diagnosis';
  drawerTitle.value = 'HolmesGPT 智能诊断详情';
  // §6.4：模型自报的 confidence 未经校准，不能当成成功概率展示。
  drawerSubtitle.value = `模型自报置信度 ${(incident.value.detail.diagnosis.confidence * 100).toFixed(0)}%（未经校准） · 来源 HolmesGPT`;
  drawerBadgeText.value = incident.value.detail.diagnosis.evidence_sufficient
    ? '证据充分'
    : '证据不足';
  drawerBadgeTone.value = incident.value.detail.diagnosis.evidence_sufficient
    ? 'success'
    : 'warning';
  drawerRawPayload.value = incident.value.detail.diagnosis;
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

function openVerificationDrawer() {
  drawerMode.value = 'verification';
  drawerTitle.value = '工单复测采样详情';
  const verdict = incident.value?.detail.verification?.verdict || '等待复测';
  drawerSubtitle.value = `裁决结论: ${verdict} · 由独立探针裁决`;
  drawerBadgeText.value = verdict;
  drawerBadgeTone.value =
    verdict === 'PASS' ? 'success' : verdict === 'FAIL' ? 'danger' : 'warning';
  const samples =
    incident.value?.records.filter((r) => r.kind === 'probe_sample').map((r) => r.payload) ?? [];
  drawerRawPayload.value = samples.length ? samples : incident.value?.detail.verification;
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

function openAuditDrawer() {
  drawerMode.value = 'audit';
  drawerTitle.value = '工单完整审计记录';
  const records = incident.value?.records ?? [];
  drawerSubtitle.value = `关联工单 ${shortId(id.value)} · 共 ${records.length} 条审计日志`;
  drawerBadgeText.value = `${records.length} 条`;
  drawerBadgeTone.value = 'neutral';
  drawerRawPayload.value = records;
  drawerActiveTab.value = 'summary';
  drawerVisible.value = true;
}

onMounted(load);
watch(id, () => {
  incident.value = null;
  fetchError.value = '';
  actionError.value = '';
  comment.value = '';
  showCommentInput.value = false;
  expandReason.value = false;
  load();
});

watch(
  () => incident.value?.detail.plan_id,
  () => {
    comment.value = '';
    showCommentInput.value = false;
  },
);

onUnmounted(() => {
  disposed = true;
  version++;
  clearTimeout(timer);
  clearTimeout(copyTimer);
});
</script>

<template>
  <ElAlert
    v-if="fetchError || actionError"
    :title="actionError || fetchError"
    type="error"
    :closable="false"
    show-icon
  />

  <ElSkeleton v-if="fetching && !incident" :rows="10" animated />

  <div v-if="!incident && !fetching" class="empty proto-card">
    无法读取工单。<ElButton @click="load">重新加载</ElButton>
  </div>

  <template v-if="incident">
    <!-- 页头规范 (V1-02) -->
    <div class="page-header">
      <div class="page-header-title-row">
        <h1>{{ scenarioName(incident.scenario_key) }}</h1>
        <span class="state-badge" :data-tone="statusTone(incident.status)">
          {{ statusLabels[incident.status] }}
        </span>

        <ElPopover placement="bottom-start" :width="320" trigger="click">
          <template #reference>
            <button type="button" class="info-popover-trigger" aria-label="查看工单信息">
              <span class="mono">{{ shortId(id) }}</span>
              <span class="muted text-12">信息 ℹ</span>
            </button>
          </template>
          <div class="metadata-list" style="margin: 0">
            <dt>工单 ID</dt>
            <dd>
              <span class="mono text-12" style="user-select: all">{{ id }}</span>
              <button
                type="button"
                class="text-button text-12"
                style="margin-left: 6px"
                @click="copyId"
              >
                {{ copiedId ? '✓ 已复制' : '复制' }}
              </button>
            </dd>
            <dt>创建时间</dt>
            <dd class="text-12">{{ timeLabel(incident.created_at) }}</dd>
            <dt>数据模式</dt>
            <dd class="text-12">{{ incident.mode }}</dd>
            <dt>场景编码</dt>
            <dd class="mono text-12">{{ incident.scenario_key }}</dd>
          </div>
        </ElPopover>
      </div>

      <div class="page-header-actions">
        <a
          class="header-action-btn secondary-btn"
          :href="`/api/incidents/${encodeURIComponent(id)}/report.md`"
          download
        >
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            aria-hidden="true"
          >
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
            <polyline points="7 10 12 15 17 10" />
            <line x1="12" y1="15" x2="12" y2="3" />
          </svg>
          下载报告
        </a>
        <ElButton
          v-if="incident.status === 'INJECTED'"
          type="primary"
          :disabled="!canAct"
          :loading="busy"
          @click="act()"
        >
          开始智能诊断
        </ElButton>
        <ElButton :loading="fetching" :disabled="busy" @click="load"> 刷新 </ElButton>
      </div>
    </div>

    <!-- TEST/MOCK 提示 (4.1) -->
    <div v-if="incident.mode === 'TEST/MOCK'" class="test-banner">
      TEST/MOCK · 本工单使用测试夹具，不是真实 Docker 或模型结果
    </div>

    <ElAlert
      v-if="incident.detail.error"
      :title="`${incident.detail.error.code} · ${incident.detail.error.message}`"
      type="error"
      :closable="false"
      show-icon
    />

    <div v-if="environmentError" class="notice">
      当前环境自检失败：{{ environmentError }}。工单历史观测仍按采集时间保留。
    </div>

    <!-- 环境状态条 (V1-04) -->
    <ServiceStatusBar :lab="health?.lab ?? null" />

    <!-- 流程跟踪条 (V1-05) -->
    <WorkflowTrace
      :incident="incident"
      @inspect-step="openStepDrawer"
      @inspect-history="openHistoryDrawer"
    />

    <!-- 主体双栏 (4.2) -->
    <div class="workbench-two-col">
      <div class="col-main">
        <!-- 根因诊断卡片 (V1-06) -->
        <section class="proto-card">
          <div class="section-heading">
            <h2>根因诊断</h2>
            <div
              v-if="incident.detail.diagnosis"
              style="display: flex; align-items: center; gap: 8px"
            >
              <span
                class="state-badge"
                :data-tone="incident.detail.diagnosis.evidence_sufficient ? 'success' : 'warning'"
              >
                {{ incident.detail.diagnosis.evidence_sufficient ? '证据充分' : '证据不足' }}
              </span>
            </div>
          </div>

          <template v-if="incident.detail.diagnosis">
            <div class="root-cause-banner">
              <div class="root-cause-title">
                {{ rootCauseName(incident.detail.diagnosis.root_cause) }}
              </div>
              <span class="mono muted text-12"> ({{ incident.detail.diagnosis.root_cause }}) </span>
            </div>

            <p class="root-cause-summary">
              {{ diagnosisSummary }}
            </p>

            <div class="root-cause-foot">
              <button type="button" class="text-button text-12" @click="openDiagnosisDrawer">
                查看完整诊断分析 →
              </button>

              <button
                v-if="incident.detail.diagnosis.evidence_ids?.length"
                type="button"
                class="text-button text-12"
                @click="locateCitations"
              >
                定位诊断依据 ({{ incident.detail.diagnosis.evidence_ids.length }} 条) ↗
              </button>
            </div>
          </template>

          <div v-else class="empty compact">
            <p>尚未取得有效诊断。</p>
            <ElButton
              v-if="incident.status === 'INJECTED'"
              type="primary"
              :disabled="!canAct"
              :loading="busy"
              @click="act()"
            >
              开始智能诊断
            </ElButton>
          </div>
        </section>

        <!-- M3：候选根因与受限补采的可核对说明 -->
        <section v-if="hasCandidateDetail" class="proto-card">
          <div class="section-heading">
            <h2>候选根因与补采</h2>
            <span v-if="incident.detail.supplement_stopped" class="state-badge" data-tone="warning">
              {{ incident.detail.supplement_stopped }}
            </span>
          </div>

          <div v-if="requestedChecks.length" class="filter-bar">
            <span class="muted text-12">为什么需要这项检查</span>
            <span v-for="item in requestedChecks" :key="item.check_id" class="text-12">
              <strong class="mono">{{ item.check_id }}</strong>
              <span class="muted">{{ item.reason }}</span>
            </span>
          </div>

          <div v-if="incident.detail.supplement" class="filter-bar">
            <span class="muted text-12">补采到的观测</span>
            <span v-for="(item, index) in collectedSources" :key="index" class="mono text-12">
              {{ item }}
            </span>
          </div>

          <div v-if="candidates.length" class="table-scroll">
            <table class="probe-table">
              <thead>
                <tr>
                  <th>候选根因</th>
                  <th>支持证据</th>
                  <th>新增证据排除了什么</th>
                  <th>仍缺失的检查</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in candidates" :key="item.root_cause">
                  <td>
                    {{ rootCauseName(item.root_cause) }}
                    <div class="mono muted text-12">{{ item.root_cause }}</div>
                  </td>
                  <td>{{ item.support_evidence_ids.length }} 条</td>
                  <td>
                    <span v-if="!item.against_evidence_ids.length" class="muted text-12">
                      暂无
                    </span>
                    <span
                      v-for="againstId in item.against_evidence_ids"
                      :key="againstId"
                      class="mono text-12"
                      style="margin-right: 6px"
                    >
                      {{ evidenceSource(againstId) }}
                    </span>
                  </td>
                  <td class="muted text-12">
                    {{ item.missing_checks.join('、') || '无' }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <p class="help-text">
            只展示可核对的结论摘要与证据引用，不展示模型隐藏思考过程；模型自报置信度未经校准。
          </p>
        </section>

        <!-- 紧凑证据面板 (V1-07) -->
        <EvidencePanel
          :records="incident.records"
          :reference-ids="incident.detail.diagnosis?.evidence_ids ?? []"
          :highlight-id="highlightedEvidenceId"
          @inspect-evidence="openEvidenceDrawer"
        />
        <!-- 底部审计入口 (V1-08) -->
        <div style="margin-top: 12px; text-align: right">
          <button type="button" class="text-button text-12" @click="openAuditDrawer">
            查看工单完整审计记录 ({{ incident.records.length }} 条) →
          </button>
        </div>
      </div>

      <!-- 右栏：处置与审批 + 工单复测 (V1-09 / V1-10) -->
      <aside class="col-side">
        <!-- 处置与审批卡片 (V1-09) -->
        <section class="proto-card approval-panel">
          <div class="section-heading">
            <h2>处置与审批</h2>
            <span
              v-if="execution"
              class="state-badge"
              :data-tone="execution.payload.state === 'SUCCEEDED' ? 'success' : 'danger'"
            >
              执行{{ execution.payload.state === 'SUCCEEDED' ? '完成' : '失败' }}
            </span>
            <span
              v-else-if="approval"
              class="state-badge"
              :data-tone="approval.payload.decision === 'approve' ? 'info' : 'neutral'"
            >
              {{ approval.payload.decision === 'approve' ? '已批准' : '已拒绝' }}
            </span>
          </div>

          <template v-if="incident.detail.plan">
            <!-- 三项核心要素突出展示 (动作、目标、风险) -->
            <div class="plan-highlight-grid">
              <div class="plan-prop-item">
                <span class="plan-prop-label">处置动作</span>
                <span class="plan-prop-val">{{ actionName(incident.detail.plan.action_id) }}</span>
              </div>
              <div class="plan-prop-item">
                <span class="plan-prop-label">目标容器</span>
                <span class="plan-prop-val mono">{{ incident.detail.plan.target }}</span>
              </div>
              <div class="plan-prop-item">
                <span class="plan-prop-label">风险等级</span>
                <span class="plan-prop-val">{{ riskName(incident.detail.plan.risk) }}</span>
              </div>
            </div>

            <div class="plan-reason-wrapper">
              <p class="plan-reason-text" :class="{ 'is-expanded': expandReason }">
                <span class="plan-reason-prefix">理由：</span>{{ incident.detail.plan.reason }}
              </p>
              <button
                v-if="incident.detail.plan.reason && incident.detail.plan.reason.length > 70"
                type="button"
                class="text-button text-12 reason-toggle-btn"
                @click="expandReason = !expandReason"
              >
                {{ expandReason ? '收起理由 ↑' : '展开完整理由 ↓' }}
              </button>
            </div>

            <div class="expected-probes-row">
              <span class="muted text-12">必需探针:</span>
              <span
                v-for="chk in incident.detail.plan.expected_checks"
                :key="chk"
                class="check-chip mono"
              >
                {{ probeName(chk) }}
              </span>
            </div>

            <!-- 待审批状态 (V1-09) -->
            <template v-if="incident.status === 'WAITING_APPROVAL'">
              <div class="notice approval-notice" style="margin-bottom: 12px">
                批准后启动目标服务，恢复结果以独立复测为准。
              </div>

              <!-- 折叠的审批意见输入 -->
              <div class="approval-comment-section">
                <button
                  v-if="!showCommentInput && !comment"
                  type="button"
                  class="text-button text-12 add-comment-btn"
                  @click="showCommentInput = true"
                >
                  + 添加审批意见
                </button>
                <div v-else class="comment-input-box">
                  <div class="comment-label-row">
                    <label class="field-label" for="approval-comment">审批意见（可选）</label>
                    <button
                      type="button"
                      class="text-button text-12 comment-collapse-btn"
                      @click="showCommentInput = false"
                    >
                      收起
                    </button>
                  </div>
                  <ElInput
                    id="approval-comment"
                    v-model="comment"
                    type="textarea"
                    :rows="2"
                    :maxlength="300"
                    show-word-limit
                    :disabled="busy"
                    placeholder="填写决策理由或批注..."
                  />
                </div>
              </div>

              <p v-if="blockedReason" class="inline-error" style="padding: 0; margin-bottom: 8px">
                {{ blockedReason }}
              </p>

              <div class="approval-actions-bar">
                <ElPopconfirm
                  title="确认拒绝此计划并终止当前处置？"
                  confirm-button-text="确认拒绝"
                  cancel-button-text="取消"
                  :width="280"
                  popper-class="approval-popconfirm"
                  placement="top-start"
                  @confirm="act('reject')"
                >
                  <template #reference>
                    <ElButton :disabled="!canAct">拒绝计划</ElButton>
                  </template>
                </ElPopconfirm>

                <ElPopconfirm
                  :title="`确认批准【${actionName(incident.detail.plan.action_id)}】并恢复容器【${incident.detail.plan.target}】？`"
                  confirm-button-text="批准并执行"
                  cancel-button-text="取消"
                  :width="300"
                  popper-class="approval-popconfirm"
                  placement="top-end"
                  @confirm="act('approve')"
                >
                  <template #reference>
                    <ElButton type="primary" :disabled="!canAct" :loading="busy">
                      批准并执行
                    </ElButton>
                  </template>
                </ElPopconfirm>
              </div>
            </template>

            <!-- 审批回执 -->
            <div v-if="approval" class="approval-receipt-box">
              <div class="receipt-title">
                {{ approval.payload.decision === 'approve' ? '人工已批准' : '人工已拒绝' }}
              </div>
              <p
                v-if="approval.payload.comment"
                style="color: var(--ink-secondary); margin-bottom: 4px"
              >
                {{ approval.payload.comment }}
              </p>
              <div class="muted text-12">
                {{ approval.payload.operator }} · {{ timeLabel(approval.created_at) }}
              </div>
            </div>

            <!-- 执行回执摘要 -->
            <div v-if="execution" class="approval-receipt-box" style="margin-top: 8px">
              <div class="receipt-title">动作执行回执 ({{ execution.payload.state }})</div>
              <div class="text-12 mono muted">
                目标: {{ execution.payload.target }} · 耗时:
                {{ durationLabel(asNumber(execution.payload.elapsed_ms)) }}
              </div>
            </div>
          </template>

          <div v-else class="empty compact">处置计划生成后可在此审批</div>
        </section>

        <!-- 工单复测卡片 (V1-10) -->
        <VerificationPanel :incident="incident" @inspect-verification="openVerificationDrawer" />
      </aside>
    </div>

    <!-- 统一详情侧栏 (V1-08) -->
    <DetailDrawer
      v-model="drawerVisible"
      v-model:active-tab="drawerActiveTab"
      :title="drawerTitle"
      :subtitle="drawerSubtitle"
      :badge-text="drawerBadgeText"
      :badge-tone="drawerBadgeTone"
      :raw-payload="drawerRawPayload"
    >
      <template #summary>
        <!-- 阶段详情视图 -->
        <div v-if="drawerMode === 'step' && inspectedStep" class="metadata-list">
          <dt>阶段名称</dt>
          <dd>
            <strong>{{ inspectedStageName }}</strong> ({{ inspectedStep.payload.phase }})
          </dd>
          <dt>执行状态</dt>
          <dd>
            <span
              class="state-badge"
              :data-tone="inspectedStep.payload.status === 'FAILED' ? 'danger' : 'success'"
            >
              {{ inspectedStep.payload.status }}
            </span>
          </dd>
          <dt>轮次 (Attempt)</dt>
          <dd>第 {{ inspectedStep.payload.attempt || 1 }} 轮</dd>
          <dt>耗时时间</dt>
          <dd>{{ durationLabel(asNumber(inspectedStep.payload.elapsed_ms)) }}</dd>
          <dt>开始时间</dt>
          <dd>
            {{ timeLabel(String(inspectedStep.payload.started_at || inspectedStep.created_at)) }}
          </dd>
          <dt>输入摘要</dt>
          <dd class="mono text-12">
            {{ JSON.stringify(inspectedStep.payload.input) || '无显式输入' }}
          </dd>
          <dt>输出摘要</dt>
          <dd class="mono text-12">
            {{ JSON.stringify(inspectedStep.payload.output) || '无显式输出' }}
          </dd>
        </div>
        <div v-else-if="drawerMode === 'step'" class="empty compact">
          本阶段尚未执行或尚无运行记录
        </div>

        <!-- 所有节点历史列表 -->
        <div v-else-if="drawerMode === 'steps_all'" class="evidence-compact-list">
          <div
            v-for="step in incident.records.filter((r) => r.kind === 'step')"
            :key="step.id"
            class="evidence-row-item"
            style="grid-template-columns: 100px 100px 1fr auto"
          >
            <div>
              <strong>R{{ step.payload.attempt || 1 }}</strong> · {{ step.payload.phase }}
            </div>
            <div>
              <span
                class="state-badge"
                :data-tone="step.payload.status === 'FAILED' ? 'danger' : 'success'"
              >
                {{ step.payload.status }}
              </span>
            </div>
            <div class="muted text-12">{{ durationLabel(asNumber(step.payload.elapsed_ms)) }}</div>
            <div class="muted text-12">
              {{ shortTimeLabel(String(step.payload.started_at || step.created_at)) }}
            </div>
          </div>
        </div>

        <!-- 证据详情摘要 -->
        <div v-else-if="drawerMode === 'evidence' && inspectedEvidence" class="metadata-list">
          <dt>证据来源</dt>
          <dd>
            <strong>{{ inspectedEvidence.payload.source }}</strong>
          </dd>
          <dt>证据类型</dt>
          <dd>
            {{
              evidenceKindLabels[String(inspectedEvidence.payload.kind)] ||
              inspectedEvidence.payload.kind
            }}
          </dd>
          <dt>采集时间</dt>
          <dd>
            {{
              timeLabel(
                String(inspectedEvidence.payload.captured_at || inspectedEvidence.created_at),
              )
            }}
          </dd>
          <dt>证据编号</dt>
          <dd class="mono text-12" style="user-select: all">{{ inspectedEvidence.payload.id }}</dd>
          <dt>诊断引用</dt>
          <dd>
            {{
              (incident.detail.diagnosis?.evidence_ids ?? []).includes(
                String(inspectedEvidence.payload.id),
              )
                ? '被当前 HolmesGPT 诊断引用'
                : '常规采集观测，未被当前诊断引用'
            }}
          </dd>
        </div>

        <!-- 诊断详情摘要 -->
        <div
          v-else-if="drawerMode === 'diagnosis' && incident.detail.diagnosis"
          class="metadata-list"
        >
          <dt>根因结论</dt>
          <dd>
            <strong>{{ rootCauseName(incident.detail.diagnosis.root_cause) }}</strong>
          </dd>
          <dt>根因编码</dt>
          <dd class="mono">{{ incident.detail.diagnosis.root_cause }}</dd>
          <dt>模型自报置信度（未经校准）</dt>
          <dd>
            <strong>{{ (incident.detail.diagnosis.confidence * 100).toFixed(0) }}%</strong>
            <span class="muted text-12" style="margin-left: 6px"
              >（仅供诊断参考，恢复以独立探针裁决）</span
            >
          </dd>
          <dt>诊断分析</dt>
          <dd style="line-height: 22px; white-space: pre-wrap">
            {{ incident.detail.diagnosis.summary }}
          </dd>
          <dt>引用证据</dt>
          <dd class="mono text-12">
            {{ incident.detail.diagnosis.evidence_ids?.join(', ') || '暂无引用 ID' }}
          </dd>
        </div>

        <!-- 采样详情摘要 -->
        <div v-else-if="drawerMode === 'verification'" class="probe-compact-list">
          <div
            v-for="record in incident.records.filter((r) => r.kind === 'probe_sample')"
            :key="record.id"
            class="approval-receipt-box"
          >
            <div style="display: flex; justify-content: space-between; margin-bottom: 6px">
              <strong>第 {{ record.payload.attempt || 1 }} 轮采样</strong>
              <span class="muted text-12">{{ timeLabel(record.created_at) }}</span>
            </div>
            <div
              v-if="Array.isArray(record.payload.probes)"
              style="display: flex; flex-direction: column; gap: 6px"
            >
              <div
                v-for="(p, pIdx) in getSampleProbes(record)"
                :key="pIdx"
                class="probe-compact-row"
              >
                <div>
                  <strong class="mono">{{ probeName(p.name) }}</strong>
                  <div class="mono text-12 muted">{{ JSON.stringify(p.observed) }}</div>
                </div>
                <span class="verdict" :data-verdict="p.verdict">{{ p.verdict }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- 审计日志列表摘要 -->
        <div v-else-if="drawerMode === 'audit'" class="evidence-compact-list">
          <div
            v-for="rec in incident.records"
            :key="rec.id"
            class="evidence-row-item"
            style="grid-template-columns: 120px minmax(0, 1fr) auto"
          >
            <div>
              <strong>{{ rec.kind }}</strong>
            </div>
            <div
              class="mono text-12"
              style="overflow: hidden; text-overflow: ellipsis; white-space: nowrap"
            >
              {{ JSON.stringify(rec.payload) }}
            </div>
            <div class="muted text-12">{{ shortTimeLabel(rec.created_at) }}</div>
          </div>
        </div>
      </template>
    </DetailDrawer>
  </template>
</template>
