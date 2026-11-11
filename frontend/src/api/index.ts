import { adminToken } from '../composables/session';
import type {
  Attachment,
  EvaluationArm,
  ImpactGraph,
  IncidentReview,
  EvaluationRunDetail,
  EvaluationRunRow,
  EvaluationSummary,
  Health,
  Incident,
  IncidentSummary,
  Scenario,
  ScenarioKey,
  Status,
} from '../types';

async function request<T>(
  path: string,
  method = 'GET',
  body?: unknown,
  key?: string,
  admin = false,
): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  // 令牌只发给确实需要管理员权限的请求：写操作与评测视图。
  // 普通只读接口不带令牌，凭据不必铺满所有请求。
  if (admin && adminToken.value) headers['X-Admin-Token'] = adminToken.value;
  if (key) headers['Idempotency-Key'] = key;
  try {
    const response = await fetch(`/api${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(45_000),
    });
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new Error(`服务未返回有效数据（HTTP ${response.status}），请检查后端连接。`);
    }
    const payload = await response.json();
    if (!response.ok) {
      if (payload.error?.code === 'FORBIDDEN') {
        // 令牌只保存在页面内存，刷新即失效；错误提示需直接指出下一步。
        throw new Error(
          'FORBIDDEN · 令牌不匹配或已失效，请在右上角「设置」中重新粘贴 .env 里 ADMIN_TOKEN= 后面的值',
        );
      }
      throw new Error(
        `${payload.error?.code ?? response.status} · ${payload.error?.message ?? '请求失败'}`,
      );
    }
    return payload as T;
  } catch (error) {
    if (error instanceof TypeError) {
      throw new Error('无法连接平台 API，请检查后端服务或 SSH 转发后重新自检。');
    }
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new Error('请求超时。操作结果可能已保存，请先刷新工单；重试将沿用幂等键。');
    }
    throw error;
  }
}

export const api = {
  health: () => request<Health>('/health'),
  scenarios: () => request<Scenario[]>('/scenarios'),
  incidents: (
    filters: { status?: Status; scenario?: ScenarioKey; limit?: number; offset?: number } = {},
  ) => {
    const query = new URLSearchParams();
    for (const [key, value] of Object.entries(filters)) {
      if (value !== undefined) query.set(key, String(value));
    }
    return request<IncidentSummary[]>(`/incidents?${query}`);
  },
  incident: (id: string) => request<Incident>(`/incidents/${encodeURIComponent(id)}`),
  inject: (scenario: ScenarioKey, key: string, poison = false) =>
    request<{ incident_id: string }>(
      `/scenarios/${encodeURIComponent(scenario)}/inject`,
      'POST',
      { confirm: true, poison },
      key,
      true,
    ),
  reset: (key: string) => request('/lab/reset', 'POST', { confirm: true }, key, true),
  diagnose: (id: string) =>
    request(`/incidents/${encodeURIComponent(id)}/diagnose`, 'POST', undefined, undefined, true),
  approve: (id: string, planId: string, decision: 'approve' | 'reject', comment: string) =>
    request(
      `/incidents/${encodeURIComponent(id)}/approval`,
      'POST',
      { plan_id: planId, decision, comment },
      `${planId}-${decision}`,
      true,
    ),
  addAttachment: (id: string, filename: string, contentBase64: string, note = '') =>
    request<Attachment>(
      `/incidents/${encodeURIComponent(id)}/attachments`,
      'POST',
      { filename, content_base64: contentBase64, note },
      undefined,
      true,
    ),
  incidentAttachments: (id: string) =>
    request<Attachment[]>(`/incidents/${encodeURIComponent(id)}/attachments`),
  incidentImpact: (id: string) =>
    request<ImpactGraph>(`/incidents/${encodeURIComponent(id)}/impact`),
  incidentReview: (id: string) =>
    request<IncidentReview>(`/incidents/${encodeURIComponent(id)}/review`),
  evaluationRuns: (options: { limit?: number; offset?: number; evaluationView?: boolean } = {}) => {
    const query = new URLSearchParams();
    if (options.limit !== undefined) query.set('limit', String(options.limit));
    if (options.offset !== undefined) query.set('offset', String(options.offset));
    if (options.evaluationView) query.set('view', 'evaluation');
    return request<EvaluationRunRow[]>(
      `/evaluation/runs?${query}`,
      'GET',
      undefined,
      undefined,
      !!options.evaluationView,
    );
  },
  evaluationSummary: () => request<EvaluationSummary>('/evaluation/summary'),
  evaluationRun: (id: string, evaluationView = false) =>
    request<EvaluationRunDetail>(
      `/evaluation/runs/${encodeURIComponent(id)}${evaluationView ? '?view=evaluation' : ''}`,
      'GET',
      undefined,
      undefined,
      evaluationView,
    ),
  triggerEvaluation: (
    scenario: ScenarioKey,
    arms: EvaluationArm[],
    key: string,
    attachmentIds: string[] = [],
    cohort: 'standard' | 'adversarial' = 'standard',
  ) =>
    request<{
      accepted: boolean;
      scenario: string;
      arms: EvaluationArm[];
      cohort: string;
      status: string;
    }>(
      '/evaluation/runs',
      'POST',
      { scenario, arms, confirm: true, attachment_ids: attachmentIds, cohort },
      key,
      true,
    ),
  uploadEvaluationAttachment: (filename: string, contentBase64: string, note = '') =>
    request<{
      id: string;
      filename: string;
      observed_at: string | null;
      observed_at_known: boolean;
      anomalies: { description: string }[];
      uncertain: string[];
    }>(
      '/evaluation/attachments',
      'POST',
      { filename, content_base64: contentBase64, note },
      undefined,
      true,
    ),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请检查后端连接。';
}
