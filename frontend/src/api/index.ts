import { adminToken } from '../composables/session';
import type { Health, Incident, IncidentSummary, Scenario, ScenarioKey, Status } from '../types';

async function request<T>(path: string, method = 'GET', body?: unknown, key?: string): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (method !== 'GET' && adminToken.value) headers['X-Admin-Token'] = adminToken.value;
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
  inject: (scenario: ScenarioKey, key: string) =>
    request<{ incident_id: string }>(
      `/scenarios/${encodeURIComponent(scenario)}/inject`,
      'POST',
      { confirm: true },
      key,
    ),
  reset: (key: string) => request('/lab/reset', 'POST', { confirm: true }, key),
  diagnose: (id: string) => request(`/incidents/${encodeURIComponent(id)}/diagnose`, 'POST'),
  approve: (id: string, planId: string, decision: 'approve' | 'reject', comment: string) =>
    request(
      `/incidents/${encodeURIComponent(id)}/approval`,
      'POST',
      { plan_id: planId, decision, comment },
      `${planId}-${decision}`,
    ),
};

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请检查后端连接。';
}
