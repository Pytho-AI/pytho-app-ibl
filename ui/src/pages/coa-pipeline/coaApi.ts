/**
 * HTTP API client for the COA Engine saved runs.
 *
 * Talks to the aiohttp server co-launched alongside the WebSocket server.
 * Pattern follows geo-agent/utils/api.js createApiClient().
 */

const BASE_URL =
  import.meta.env.VITE_COA_API_URL || "http://localhost:8766";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface RunSummary {
  run_id: string;
  created_at: string | null;
  world_state_summary: string;
  objective: string;
  current_phase: string;
  num_coas: number;
  mode: string;
  resumable: boolean;
  user_id?: string | null;
}

export interface RunListResponse {
  runs: RunSummary[];
  total: number;
}

export interface RunDetail {
  run_id: string;
  current_phase: string;
  resumable: boolean;
  context?: {
    world_state: string;
    objective: string;
    parameters: Record<string, any>;
    cop_map_id?: string | null;
    user_id?: string | null;
    detected_num_agents?: number;
    scenario_name?: string;
    player_categories?: Record<string, string>;
  };
  phase1?: {
    all_coas: any[];
    doctrinal_validations: any[];
    coa_results: any[];
    scenario?: any;
  };
  tree_visualization_html?: string | null;
  phase2?: {
    wargame_analyses: any[];
  };
  results?: Record<string, any>;
  approval?: ApprovalData;
}

export interface ApprovalData {
  status: string;
  selected_coas: number[];
  commander_guidance: string;
  approved_at?: string;
}

// ---------------------------------------------------------------------------
// API functions
// ---------------------------------------------------------------------------

async function request<T>(
  path: string,
  options?: RequestInit
): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const response = await fetch(url, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options?.headers,
    },
  });

  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(
      body.error || `Request failed: ${response.status} ${response.statusText}`
    );
  }

  return response.json();
}

export function saveDraft(draft: {
  world_state: string;
  objective: string;
  parameters: Record<string, any>;
  cop_map_id?: string | null;
  user_id?: string | null;
  run_id?: string | null;
}): Promise<{ ok: boolean; run_id: string }> {
  return request("/api/runs", {
    method: "POST",
    body: JSON.stringify(draft),
  });
}

export function listRuns(userId?: string): Promise<RunListResponse> {
  const params = userId ? `?user_id=${encodeURIComponent(userId)}` : "";
  return request<RunListResponse>(`/api/runs${params}`);
}

export function getRun(runId: string, includeTree = false): Promise<RunDetail> {
  const params = includeTree ? "?include_tree=true" : "";
  return request<RunDetail>(`/api/runs/${encodeURIComponent(runId)}${params}`);
}

export function saveApproval(
  runId: string,
  approval: ApprovalData
): Promise<{ ok: boolean; run_id: string }> {
  return request(`/api/runs/${encodeURIComponent(runId)}/approval`, {
    method: "PUT",
    body: JSON.stringify(approval),
  });
}

export function deleteRun(
  runId: string
): Promise<{ ok: boolean; run_id: string }> {
  return request(`/api/runs/${encodeURIComponent(runId)}`, {
    method: "DELETE",
  });
}
