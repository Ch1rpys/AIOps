export type Verdict = 'PASS' | 'FAIL' | 'UNKNOWN';
export type ScenarioKey = 'MYSQL_DOWN' | 'REDIS_DOWN' | 'NGINX_502';
export type Status =
  | 'CREATED'
  | 'INJECTED'
  | 'COLLECTING'
  | 'DIAGNOSING'
  | 'PLANNING'
  | 'WAITING_APPROVAL'
  | 'EXECUTING'
  | 'VERIFYING'
  | 'RESOLVED'
  | 'CANCELLED'
  | 'FAILED'
  | 'UNKNOWN'
  | 'ERROR'
  | 'BLOCKED';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };
export interface Probe {
  name: string;
  verdict: Verdict;
  observed: Record<string, Json>;
  captured_at: string;
}
export interface LabStatus {
  healthy: boolean;
  compose_project: string;
  services: Record<string, { state: string; health: string; project: string }>;
  probes: Probe[];
  error: { code: string; message: string } | null;
  active_incident_id: string | null;
  captured_at: string;
}
export interface Health {
  platform: Verdict;
  mode: 'REAL' | 'TEST/MOCK';
  holmes: { status: Verdict; reason?: string; http_status?: number };
  planner: { status: Verdict; configured: boolean; reason: string };
  admin_configured: boolean;
  lab: LabStatus;
}
export interface Scenario {
  key: ScenarioKey;
  name: string;
  enabled: boolean;
  target: string;
  checks: string[];
  description: string;
  risk: string;
}
export interface IncidentSummary {
  id: string;
  status: Status;
  scenario_key: ScenarioKey;
  mode: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
  root_cause?: string | null;
  verification_verdict?: Verdict | null;
}
export interface Plan {
  action_id: string;
  target: string;
  reason: string;
  risk: string;
  expected_checks: string[];
}
export interface Diagnosis {
  root_cause: string;
  summary: string;
  evidence_ids: string[];
  confidence: number;
  evidence_sufficient: boolean;
}
export interface AuditRecord {
  id: string;
  kind: string;
  created_at: string;
  payload: Record<string, Json>;
}
export interface Incident extends IncidentSummary {
  updated_at: string;
  finished_at: string | null;
  records: AuditRecord[];
  detail: {
    attempt?: number;
    fault_confirmed?: boolean;
    plan_id?: string;
    plan?: Plan;
    diagnosis?: Diagnosis;
    error?: { code: string; message: string };
    verification?: { verdict: Verdict; samples: Probe[][]; captured_at: string };
  };
}
