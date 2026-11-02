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
  /** M3：候选根因及其支持与反对证据。 */
  candidates?: CandidateRootCause[];
  /** M3：模型请求补采的检查与理由。 */
  requested_checks?: CheckRequest[];
}
export interface CandidateRootCause {
  root_cause: string;
  support_evidence_ids: string[];
  against_evidence_ids: string[];
  missing_checks: string[];
}
export interface CheckRequest {
  check_id: string;
  reason: string;
}
export interface CollectedEvidence {
  id?: string;
  source?: string;
  kind?: string;
}
export interface SupplementRecord {
  round: number;
  requested: CheckRequest[];
  collected: CollectedEvidence[];
  collected_at: string;
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
    /** M3：受限主动补采。 */
    supplement?: SupplementRecord;
    supplement_stopped?: string;
  };
}

// ---- 盲诊评测 ---------------------------------------------------------------

export type EvaluationArm = 'A' | 'B' | 'C';

export interface EvaluationRunRow {
  id: string;
  arm: EvaluationArm;
  snapshot_hash: string;
  root_cause: string | null;
  evidence_sufficient: boolean | null;
  summary: string | null;
  status: string;
  model_calls: number | null;
  latency_ms: number | null;
  token_usage: Record<string, Json> | null;
  fault_present: number | null;
  created_at: string;
  /** 只有显式切到「评测视图」才会返回。 */
  truth_scenario?: ScenarioKey;
  correct?: number | null;
  rejected?: number | null;
}

export interface EvaluationRunDetail extends EvaluationRunRow {
  evidence: { items?: Json };
  diagnosis: Record<string, Json>;
}

export interface EvaluationArmSummary {
  arm: EvaluationArm;
  runs: number;
  valid_runs: number;
  correct: number;
  rejected: number;
  contradictory: number;
  invalid_runs: number;
  budget_exceeded: number;
  errors: number;
  contract_violations: number;
  accuracy: number | null;
  rejection_rate: number | null;
  avg_latency_ms: number | null;
  model_calls_avg: number | null;
}

export interface EvaluationSummary {
  arms: EvaluationArmSummary[];
  /** 纯诊断对照不评估处置提议与拦截率：显示「不适用」，不记 0。 */
  proposal_metrics_applicable: boolean;
  notes: string[];
}
