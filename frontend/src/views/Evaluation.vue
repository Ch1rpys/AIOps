<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue';
import { ElAlert, ElButton, ElSkeleton } from 'element-plus';
import { api, errorMessage } from '../api';
import { adminToken } from '../composables/session';
import {
  durationLabel,
  rootCauseName,
  scenarioName,
  shortId,
  shortTimeLabel,
  timeLabel,
} from '../display';
import type {
  EvaluationArm,
  EvaluationArmSummary,
  EvaluationRunDetail,
  EvaluationRunRow,
  EvaluationSummary,
  ScenarioKey,
} from '../types';

const runs = ref<EvaluationRunRow[]>([]);
const summary = ref<EvaluationSummary | null>(null);
const details = ref<Record<string, EvaluationRunDetail>>({});
const expanded = ref<string>('');
const loading = ref(false);
const error = ref('');
const notice = ref('');

// 真值默认不显示：只有显式切到「评测视图」才请求带真值的返回体。
const evaluationView = ref(false);
const scenario = ref<ScenarioKey>('MYSQL_DOWN');
const arms = ref<EvaluationArm[]>(['A', 'B']);
const cohort = ref<'standard' | 'adversarial' | 'unseen'>('standard');
const triggering = ref(false);
const uploading = ref(false);
const attachmentId = ref('');
const attachmentName = ref('');

let version = 0;
let disposed = false;
let poller: number | undefined;

const canTrigger = computed(() => !!adminToken.value.trim());

const armLabel: Record<EvaluationArm, string> = {
  A: 'A · 单次诊断',
  B: 'B · 多 Agent 复核',
  C: 'C · 增加视觉证据',
  L: 'L · 闭环规划',
  R: 'R · 影子搜索基线',
};

function percent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '不适用';
  return `${(value * 100).toFixed(1)}%`;
}

function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return '未知';
  return value < 1000 ? `${value} ms` : `${(value / 1000).toFixed(1)} 秒`;
}

function tokenCell(total: number | null, known: number, partial = 0): string {
  // token 未返回就标「未知」，不用 0 顶替；部分已知也要说出来。
  if (total === null) {
    return partial ? `未知（${partial} 条仅部分已知）` : '未知';
  }
  return `${total}（${known} 条有 usage${partial ? `，${partial} 条仅部分已知` : ''}）`;
}

function excludedText(arm: EvaluationArmSummary): string {
  // 剔除了哪些样本要写清楚：分母不能悄悄变小。
  const parts: string[] = [];
  if (arm.invalid_runs) parts.push(`${arm.invalid_runs} 次故障未成立`);
  if (arm.errors) parts.push(`${arm.errors} 次错误`);
  if (arm.budget_exceeded) parts.push(`${arm.budget_exceeded} 次超限`);
  return parts.join('、');
}

function denominatorNote(arm: EvaluationArmSummary): string {
  // 契约违约按「自信地答错」计入分母（§9.1），也要说明白。
  if (!arm.contract_violations) return '';
  return `${arm.contract_violations} 次契约违约按答错计入分母`;
}

function truthCell(row: EvaluationRunRow): string {
  if (!evaluationView.value) return '不显示';
  if (row.truth_scenario) return scenarioName(row.truth_scenario);
  return '—';
}

async function load() {
  const requestedVersion = ++version;
  loading.value = true;
  error.value = '';
  try {
    const [list, totals] = await Promise.all([
      api.evaluationRuns({ limit: 50, evaluationView: evaluationView.value }),
      api.evaluationSummary(),
    ]);
    if (requestedVersion !== version || disposed) return;
    runs.value = list;
    summary.value = totals;
  } catch (cause) {
    if (requestedVersion === version && !disposed) {
      // 汇总不需要令牌，列表失败（例如评测视图缺令牌）时不要把汇总一起清掉。
      runs.value = [];
      error.value = errorMessage(cause);
    }
  } finally {
    if (requestedVersion === version && !disposed) loading.value = false;
  }
}

async function toggleDetail(row: EvaluationRunRow) {
  if (expanded.value === row.id) {
    expanded.value = '';
    return;
  }
  expanded.value = row.id;
  if (details.value[row.id]) return;
  try {
    details.value = {
      ...details.value,
      [row.id]: await api.evaluationRun(row.id, evaluationView.value),
    };
  } catch (cause) {
    error.value = errorMessage(cause);
  }
}

async function uploadAttachment(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  if (!file) return;
  uploading.value = true;
  error.value = '';
  try {
    const base64 = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '');
      reader.onerror = () => reject(new Error('读取文件失败'));
      reader.readAsDataURL(file);
    });
    const uploaded = await api.uploadEvaluationAttachment(file.name, base64);
    attachmentId.value = uploaded.id;
    attachmentName.value = uploaded.filename;
    notice.value = uploaded.observed_at_known
      ? `截图已就绪：识别到 ${uploaded.anomalies.length} 处异常`
      : '截图已就绪，但图里没有可识别的时间，观测时间标为未知';
  } catch (cause) {
    error.value = errorMessage(cause);
    attachmentId.value = '';
    attachmentName.value = '';
  } finally {
    uploading.value = false;
    input.value = '';
  }
}

function onCohortChange(event: Event) {
  const value = (event.target as HTMLSelectElement).value;
  cohort.value = value === 'adversarial' || value === 'unseen' ? value : 'standard';
}

async function trigger() {
  if (!canTrigger.value) {
    error.value = '请先在右上角「设置」中粘贴 ADMIN_TOKEN。';
    return;
  }
  triggering.value = true;
  error.value = '';
  notice.value = '';
  try {
    if (arms.value.includes('C') && !attachmentId.value) {
      error.value = 'C 臂需要先上传截图：没有视觉证据时它等价于 B 臂，是假对照。';
      return;
    }
    await api.triggerEvaluation(
      cohort.value === 'unseen' ? null : scenario.value,
      arms.value.filter((arm) => arm !== 'R'),
      `EVAL-${Date.now()}`,
      [...(attachmentId.value ? [attachmentId.value] : [])],
      cohort.value,
    );
    notice.value =
      cohort.value === 'adversarial'
        ? '对抗样本已触发：日志和截图里写入了错误根因。它不计入普通正确率。'
        : cohort.value === 'unseen'
          ? '未见故障已触发：只抽 holdout，按校准签名计分，不与旧正确率合成。'
          : '实验已触发：注入故障 → 固化证据快照 → 各臂在同一快照上诊断。';
    await load();
  } catch (cause) {
    error.value = errorMessage(cause);
  } finally {
    triggering.value = false;
  }
}

watch(evaluationView, load);

onMounted(async () => {
  await load();
  // 实验在后台执行，完成后自动刷新列表。
  poller = window.setInterval(() => {
    if (!disposed && !document.hidden) load();
  }, 8000);
});

onUnmounted(() => {
  disposed = true;
  version++;
  if (poller) window.clearInterval(poller);
});
</script>

<template>
  <div class="page-header">
    <div>
      <h1>盲诊评测</h1>
      <p class="muted text-12" style="margin-top: 4px">
        独立链路：只诊断、不提议处置，不产生任何执行动作。真值不进入模型上下文，也不出现在工作台。
      </p>
      <p class="help-text">
        A 比单次诊断，B 比同一份证据上的复核，C 比加上截图之后会不会改变结论。
      </p>
      <p class="help-text">正确率不含对抗样本，也不含应用固定 500 这类负例，更不含未见故障。</p>
      <p class="help-text">未见故障按校准签名覆盖计分。R 臂只在影子里搜修复，不进入盲诊 prompt。</p>
    </div>
    <ElButton :loading="loading" @click="load">刷新</ElButton>
  </div>

  <ElAlert v-if="error" :title="error" type="error" :closable="false" show-icon />
  <ElAlert v-if="notice" :title="notice" type="success" :closable="false" show-icon />

  <section class="proto-card">
    <div class="section-heading">
      <h2>触发一次盲诊实验</h2>
    </div>
    <div class="filter-bar">
      <label>
        <span>样本组</span>
        <select :value="cohort" :disabled="triggering" @change="onCohortChange">
          <option value="standard">普通盲诊</option>
          <option value="adversarial">对抗样本</option>
          <option value="unseen">未见故障（holdout）</option>
        </select>
      </label>
      <label v-if="cohort !== 'unseen'">
        <span>注入场景</span>
        <select v-model="scenario" :disabled="triggering">
          <option value="MYSQL_DOWN">MySQL 服务停止</option>
          <option value="REDIS_DOWN">Redis 服务停止</option>
          <option value="NGINX_502">Nginx 上游不可用</option>
          <option value="NGINX_UPSTREAM">Nginx 上游配置错误</option>
          <option value="APP_HTTP_500">应用固定 500（负例，不恢复）</option>
        </select>
      </label>
      <p v-else class="help-text">未见故障只抽 holdout。无令牌界面不会展示配方原语。</p>
      <label>
        <span>实验臂</span>
        <select v-model="arms" multiple :disabled="triggering" size="3" style="min-height: 82px">
          <option value="A">A · 单次诊断</option>
          <option value="B">B · 多 Agent 复核</option>
          <option value="C">C · 增加视觉证据</option>
        </select>
      </label>
      <label v-if="arms.includes('C')">
        <span>视觉证据截图</span>
        <input
          type="file"
          accept="image/png,image/jpeg,image/webp"
          :disabled="triggering || uploading"
          @change="uploadAttachment"
        />
        <span class="muted text-12">
          {{
            uploading
              ? '上传并识别中…'
              : attachmentId
                ? `已就绪：${attachmentName}`
                : 'C 臂必须先上传截图，否则会被拒绝（不做降级伪装）'
          }}
        </span>
      </label>
      <ElButton type="primary" :loading="triggering" :disabled="!canTrigger" @click="trigger">
        触发实验
      </ElButton>
      <span class="muted text-12" style="margin-left: auto">
        {{ canTrigger ? '实验会停止目标容器，结束后自动恢复环境' : '需要管理员令牌才能触发' }}
      </span>
    </div>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>各臂对照</h2>
    </div>
    <ElSkeleton v-if="loading && !summary" :rows="3" animated />
    <div v-else-if="!summary || !summary.arms.length" class="empty">暂无实验记录。</div>
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>实验臂</th>
            <th>有效样本</th>
            <th>根因正确率</th>
            <th>拒答率</th>
            <th>自相矛盾</th>
            <th>平均耗时（臂内）</th>
            <th>平均模型调用</th>
            <th>token 用量</th>
            <th>错误处置提议数</th>
            <th>拦截率</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="arm in summary.arms" :key="arm.arm">
            <td>{{ armLabel[arm.arm] }}</td>
            <td>
              {{ arm.valid_runs }} / {{ arm.runs }}
              <div v-if="excludedText(arm)" class="muted text-12">
                （{{ excludedText(arm) }}，已剔除）
              </div>
              <div v-if="denominatorNote(arm)" class="muted text-12">
                （{{ denominatorNote(arm) }}）
              </div>
            </td>
            <td>{{ percent(arm.accuracy) }}</td>
            <td>{{ percent(arm.rejection_rate) }}</td>
            <td>
              {{ arm.contradictory }}
              <span class="muted text-12">（自证充分却答错）</span>
            </td>
            <td>{{ durationLabel(arm.avg_latency_ms ?? undefined) }}</td>
            <td>
              {{ arm.model_calls_avg ?? '未知' }}
              <span v-if="arm.model_calls_unknown" class="muted text-12">
                （{{ arm.model_calls_unknown }} 条未知未计入）
              </span>
            </td>
            <td>
              {{ tokenCell(arm.token_usage_total, arm.token_usage_known, arm.token_usage_partial) }}
            </td>
            <td class="muted">不适用</td>
            <td class="muted">不适用</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="summary && summary.arms.length" class="table-scroll" style="margin-top: 12px">
      <table class="probe-table">
        <thead>
          <tr>
            <th>实验臂</th>
            <th>模型时间</th>
            <th>采证时间</th>
            <th>容器停止</th>
            <th>容器恢复</th>
            <th>实验总耗时</th>
            <th>人工审批等待</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="arm in summary.arms" :key="`timing-${arm.arm}`">
            <td>{{ arm.arm }}</td>
            <td>{{ ms(arm.timing.model_ms) }}</td>
            <td>{{ ms(arm.timing.collect_ms) }}</td>
            <td>{{ ms(arm.timing.container_ms) }}</td>
            <td>{{ ms(arm.timing.restore_ms) }}</td>
            <td>{{ ms(arm.timing.experiment_total_ms) }}</td>
            <td class="muted">不适用</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="summary && summary.end_to_end" class="section-heading" style="margin-top: 16px">
      <h2>端到端对照（闭环路径）</h2>
    </div>
    <div v-if="summary && summary.end_to_end" class="filter-bar">
      <span class="muted text-12">处置提议</span>
      <span class="text-12">{{ summary.end_to_end.proposals }} 条</span>
      <span class="muted text-12">错误处置提议数</span>
      <span class="text-12">
        {{ summary.end_to_end.illegal_proposals }} 条
        {{
          summary.end_to_end.illegal_proposal_rate === null
            ? '（不适用）'
            : `（${(summary.end_to_end.illegal_proposal_rate * 100).toFixed(1)}%）`
        }}
      </span>
      <span class="muted text-12">拦截率</span>
      <span class="text-12">
        {{
          summary.end_to_end.block_rate === null
            ? '不适用'
            : `${(summary.end_to_end.block_rate * 100).toFixed(1)}%`
        }}
      </span>
      <span class="muted text-12">恢复成功率</span>
      <span class="text-12">
        {{
          summary.end_to_end.recovery_success_rate === null
            ? '不适用'
            : `${(summary.end_to_end.recovery_success_rate * 100).toFixed(1)}%`
        }}
      </span>
    </div>
    <p v-if="summary && summary.end_to_end" class="help-text">
      {{ summary.end_to_end.note }}
    </p>

    <div v-if="summary?.adversarial" class="section-heading" style="margin-top: 16px">
      <h2>对抗样本（不计入正确率）</h2>
    </div>
    <div v-if="summary?.adversarial" class="filter-bar">
      <span class="muted text-12">样本</span>
      <span class="text-12">{{ summary.adversarial.runs }} 条</span>
      <span class="muted text-12">根因被带偏</span>
      <span class="text-12">
        {{
          summary.adversarial.root_cause_biased === null
            ? '不适用'
            : `${summary.adversarial.root_cause_biased}（${((summary.adversarial.root_cause_biased_rate ?? 0) * 100).toFixed(1)}%）`
        }}
      </span>
      <span class="muted text-12">非法动作提议</span>
      <span class="text-12">
        {{
          summary.adversarial.illegal_action_proposed === null
            ? '不适用'
            : `${summary.adversarial.illegal_action_proposed} 条`
        }}
      </span>
      <span class="muted text-12">非法动作执行</span>
      <span class="text-12">
        {{
          summary.adversarial.illegal_action_executed === null
            ? '不适用'
            : `${summary.adversarial.illegal_action_executed} 次`
        }}
      </span>
    </div>
    <p v-if="summary?.adversarial" class="help-text">{{ summary.adversarial.note }}</p>

    <div v-if="summary?.abstain" class="section-heading" style="margin-top: 16px">
      <h2>负例（不计入正确率）</h2>
    </div>
    <div v-if="summary?.abstain" class="filter-bar">
      <span class="muted text-12">样本</span>
      <span class="text-12">{{ summary.abstain.runs }} 条</span>
      <span class="muted text-12">拒答或 OTHER</span>
      <span class="text-12">
        {{ summary.abstain.abstained === null ? '不适用' : `${summary.abstain.abstained} 条` }}
      </span>
    </div>
    <p v-if="summary?.abstain" class="help-text">{{ summary.abstain.note }}</p>

    <div v-if="summary?.unseen" class="section-heading" style="margin-top: 16px">
      <h2>未见故障（单独分母）</h2>
    </div>
    <div v-if="summary?.unseen" class="filter-bar">
      <span class="muted text-12">有效样本</span>
      <span class="text-12">{{ summary.unseen.unseen_n ?? '不适用' }}</span>
      <span class="muted text-12">签名覆盖</span>
      <span class="text-12">{{ percent(summary.unseen.unseen_signature_recall) }}</span>
      <span class="muted text-12">签名精确率</span>
      <span class="text-12">{{ percent(summary.unseen.unseen_signature_precision) }}</span>
      <span class="muted text-12">漂移剔除</span>
      <span class="text-12">{{ summary.unseen.unseen_drift_excluded ?? '不适用' }}</span>
      <span class="muted text-12">弱解析</span>
      <span class="text-12">{{ summary.unseen.unseen_weak_parse ?? '不适用' }}</span>
    </div>
    <p v-if="summary?.unseen" class="help-text">
      {{
        summary.unseen.note ||
        '未见故障不与 canonical 正确率合成。漂移不算模型答错。缺失 failing_check_ids 时弱解析并标明。'
      }}
    </p>

    <div v-if="summary?.platform" class="section-heading" style="margin-top: 16px">
      <h2>影子保真与 R 臂</h2>
    </div>
    <div v-if="summary?.platform" class="filter-bar">
      <span class="muted text-12">影子可用</span>
      <span class="text-12">{{
        summary.platform.twin_available == null
          ? '不适用'
          : summary.platform.twin_available
            ? '是'
            : '否'
      }}</span>
      <span class="muted text-12">试修 / 跳过</span>
      <span class="text-12">
        {{ summary.platform.twin_trials ?? '不适用' }} /
        {{ summary.platform.twin_skipped ?? '不适用' }}
      </span>
      <span class="muted text-12">保真度</span>
      <span class="text-12">{{ percent(summary.platform.twin_fidelity) }}</span>
      <span class="muted text-12">部分恢复预测</span>
      <span class="text-12">{{ percent(summary.platform.twin_partial_predictions) }}</span>
      <span class="muted text-12">R 臂消签名</span>
      <span class="text-12">{{ percent(summary.platform.r_arm_resolve_rate) }}</span>
    </div>

    <ul v-if="summary" class="help-text" style="margin-top: 10px">
      <li v-for="note in summary.notes" :key="note">{{ note }}</li>
    </ul>
  </section>

  <section class="proto-card">
    <div class="section-heading">
      <h2>实验记录</h2>
    </div>
    <div class="filter-bar">
      <label class="inline-label">
        <input v-model="evaluationView" type="checkbox" />
        <span>评测视图（显示真值与判卷结果）</span>
      </label>
      <span class="muted text-12" style="margin-left: auto">
        真值只由评测服务持有；切换视图需要管理员令牌。
      </span>
    </div>

    <ElSkeleton v-if="loading && !runs.length" :rows="5" animated />
    <div v-else-if="!runs.length" class="empty">还没有实验记录。</div>
    <div v-else class="table-scroll">
      <table class="probe-table">
        <thead>
          <tr>
            <th>记录</th>
            <th>实验臂</th>
            <th>结论</th>
            <th>证据充分</th>
            <th>故障成立</th>
            <th>模型调用</th>
            <th>耗时</th>
            <th>时间</th>
            <th style="text-align: right">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="row in runs" :key="row.id">
            <tr>
              <td class="mono" :title="row.id">{{ shortId(row.id) }}</td>
              <td>
                {{ armLabel[row.arm] ?? row.arm }}
                <div v-if="row.cohort === 'adversarial'" class="muted text-12">对抗样本</div>
                <div v-else-if="row.cohort === 'abstain'" class="muted text-12">负例</div>
                <div v-else-if="row.cohort === 'unseen'" class="muted text-12">未见故障</div>
              </td>
              <td>
                {{ row.root_cause ? rootCauseName(row.root_cause) : '—' }}
                <div v-if="row.summary" class="muted text-12">{{ row.summary }}</div>
              </td>
              <td>
                <span v-if="row.evidence_sufficient === null" class="muted text-12">—</span>
                <span
                  v-else
                  class="state-badge"
                  :data-tone="row.evidence_sufficient ? 'success' : 'warning'"
                >
                  {{ row.evidence_sufficient ? '是' : '否' }}
                </span>
              </td>
              <td>
                <span v-if="row.fault_present === null" class="muted text-12">未知</span>
                <span
                  v-else
                  class="state-badge"
                  :data-tone="row.fault_present ? 'success' : 'danger'"
                >
                  {{ row.fault_present ? '成立' : '未成立' }}
                </span>
              </td>
              <td>{{ row.model_calls ?? '未知' }}</td>
              <td>{{ durationLabel(row.latency_ms ?? undefined) }}</td>
              <td class="muted text-12" :title="timeLabel(row.created_at)">
                {{ shortTimeLabel(row.created_at) }}
              </td>
              <td style="text-align: right; white-space: nowrap">
                <button type="button" class="text-button text-12" @click="toggleDetail(row)">
                  {{ expanded === row.id ? '收起' : '展开' }}
                </button>
                <span v-if="evaluationView" class="muted text-12" style="margin-left: 8px">
                  真值：{{ truthCell(row) }}
                </span>
              </td>
            </tr>
            <tr v-if="expanded === row.id">
              <td colspan="9">
                <div v-if="details[row.id]" class="text-12 mono" style="white-space: pre-wrap">
                  <strong>模型原始结论</strong>
                  <pre>{{ JSON.stringify(details[row.id]?.diagnosis, null, 2) }}</pre>
                  <strong>引用的证据快照</strong>
                  <pre>{{ JSON.stringify(details[row.id]?.evidence, null, 2) }}</pre>
                </div>
                <div v-else class="muted text-12">正在读取…</div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>
  </section>
</template>
