import type { Json, Status } from './types';

export const statusLabels: Record<Status, string> = {
  CREATED: '准备注入',
  INJECTED: '故障已确认',
  COLLECTING: '采集证据',
  DIAGNOSING: 'HolmesGPT 诊断',
  PLANNING: '制定计划',
  WAITING_APPROVAL: '等待人工审批',
  EXECUTING: '处理审批 / 执行动作',
  VERIFYING: '独立复测',
  RESOLVED: '已验证恢复',
  CANCELLED: '已取消',
  FAILED: '恢复失败',
  UNKNOWN: '结果未知',
  ERROR: '流程异常',
  BLOCKED: '动作被拦截',
};

export const phaseLabels: Record<string, string> = {
  ...statusLabels,
  APPROVAL: '人工审批',
};

export const scenarioLabels: Record<string, string> = {
  MYSQL_DOWN: 'MySQL 故障',
  REDIS_DOWN: 'Redis 故障',
  NGINX_502: 'Nginx 代理故障',
  NGINX_UPSTREAM: 'Nginx 上游配置错误',
  APP_HTTP_500: '应用固定 500',
  DUAL_MYSQL_REDIS: 'MySQL 与 Redis 同时停止',
};

export const rootCauseLabels: Record<string, string> = {
  MYSQL_SERVICE_STOPPED: 'MySQL 服务已停止',
  REDIS_SERVICE_STOPPED: 'Redis 服务已停止',
  UPSTREAM_APP_STOPPED: '上游应用已停止',
  NGINX_UPSTREAM_MISCONFIGURED: 'Nginx 上游配置错误',
  APP_SERVICE_STOPPED: '应用服务已停止',
};

export const actionLabels: Record<string, string> = {
  START_DEMO_MYSQL: '启动 MySQL',
  START_DEMO_REDIS: '启动 Redis',
  START_DEMO_APP: '启动应用',
  RESTORE_NGINX_UPSTREAM: '恢复 Nginx 上游配置',
  RESTORE_APP_MODE: '恢复应用为正常响应',
  NO_ACTION: '不执行恢复动作',
};

export const riskLabels: Record<string, string> = {
  LOW: '低风险',
  MEDIUM: '中风险',
  HIGH: '高风险',
};

export const verdictLabels: Record<string, string> = {
  PASS: '已恢复',
  FAIL: '未恢复',
  UNKNOWN: '结果未知',
};

export const probeLabels: Record<string, string> = {
  mysql_select_1: 'MySQL 连通性 (SELECT 1)',
  app_db_check: '应用数据库检查 (/db-check)',
  redis_roundtrip: 'Redis 读写',
  app_cache_check: '应用缓存检查 (/cache-check)',
  nginx_hello: 'Nginx 代理 (/api/hello)',
  app_health: '应用健康检查',
};

export const evidenceKindLabels: Record<string, string> = {
  container_state: '容器快照',
  probe: '探针观测',
  logs: '服务日志',
  runbook: 'Runbook 文档',
};

export function scenarioName(key: string): string {
  return scenarioLabels[key] ?? key;
}

export function rootCauseName(cause?: string | null): string {
  if (!cause) return '尚无有效诊断';
  return rootCauseLabels[cause] ?? cause;
}

export function actionName(action?: string | null): string {
  if (!action) return '未指定动作';
  return actionLabels[action] ?? action;
}

export function riskName(risk?: string | null): string {
  if (!risk) return '未知';
  return riskLabels[risk] ?? risk;
}

export function probeName(name: string): string {
  return probeLabels[name] ?? name;
}

export function shortId(id: string): string {
  if (!id) return '';
  return id.startsWith('inc_') ? `${id.slice(0, 10)}…` : id;
}

export function timeLabel(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? '未知时间'
    : date.toLocaleString('zh-CN', { hour12: false });
}

export function shortTimeLabel(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '未知时间';
  const hours = String(date.getHours()).padStart(2, '0');
  const minutes = String(date.getMinutes()).padStart(2, '0');
  const seconds = String(date.getSeconds()).padStart(2, '0');
  return `${hours}:${minutes}:${seconds}`;
}

export function serviceStatusText(state?: string, health?: string): string {
  if (!state && !health) return '状态未知';
  if (state === 'running' && health === 'healthy') return '运行正常';
  if (state === 'running' && (!health || health === 'unknown')) return '运行中 · 健康未知';
  if (state === 'running' && health === 'unhealthy') return '运行中 · 异常';
  if (state === 'exited' || state === 'dead') return '已停止';
  return state || '未知状态';
}

export const terminalStatuses: Status[] = [
  'RESOLVED',
  'CANCELLED',
  'FAILED',
  'UNKNOWN',
  'ERROR',
  'BLOCKED',
];

export function statusTone(status: Status): string {
  if (status === 'RESOLVED') return 'success';
  if (['FAILED', 'ERROR', 'BLOCKED'].includes(status)) return 'danger';
  if (['UNKNOWN', 'WAITING_APPROVAL'].includes(status)) return 'warning';
  if (status === 'CANCELLED') return 'neutral';
  return 'info';
}

export function objectValue(value: Json | undefined): Record<string, Json> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
}

export function durationLabel(milliseconds: number | undefined): string {
  if (milliseconds === undefined || !Number.isFinite(milliseconds)) return '未记录';
  if (milliseconds < 1000) return `${milliseconds} ms`;
  return `${(milliseconds / 1000).toFixed(1)} 秒`;
}
