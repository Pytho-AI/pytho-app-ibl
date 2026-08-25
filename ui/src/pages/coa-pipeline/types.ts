/**
 * Shared TypeScript types for the COA pipeline.
 *
 * All interfaces that cross component boundaries live here so every child
 * component can import from one place instead of reaching into CoaPipeline.tsx.
 */

export interface GenerationConfig {
  worldState: string;
  objective: string;
  numCoas: number;
  creationMode: string;
  useDoctrinalBranching?: boolean;
  divergence: number;
  numSimulations?: number;
  copMapId?: string | null;
  userId?: string | null;
}

export interface GeneratedCOA {
  coa_number: number;
  summary: string;
  metrics: {
    expected_value: number;
    confidence_interval: { lower: number; upper: number };
    uncertainty: number;
    visits: number;
    time_cost?: number;
    resources_used?: number;
    resources_remaining?: number;
  };
  time?: Record<string, { hours: number }> & { total_turns?: number };
  resources?: Record<string, { used: number; remaining: number }>;
  risk: { level: string; description: string; confidence_width?: number };
  outcome: { status: string; description: string; winner?: string | null };
  actions: Array<{ turn: number; actor: string; player_index?: number; action: string; result: string }>;
  feasibility?: { feasible: boolean; issues: string[] };
}

export interface ComparisonSummary {
  best_expected_value?: { coa: number; value: number };
  fastest_execution?: { coa: number; time_hours: number };
  most_resource_efficient?: { coa: number; resources_used: number };
  lowest_risk?: { coa: number; confidence_width: number };
}

export interface CriteriaComparison {
  criteria: Array<{
    name: string;
    weight: string;
    ratings: Array<{
      coa_index: number;
      coa_title: string;
      rating: string;
      rationale: string;
    }>;
  }>;
}

export interface DecisionMatrix {
  coas: Array<{
    coa_index: number;
    coa_title: string;
    weighted_score: number;
    max_possible: number;
    rank: number;
    score_percentage: number;
  }>;
}

export interface Recommendation {
  recommended_coa_index: number;
  recommendation_summary: string;
  reasoning: string;
  comparative_analysis: string;
  key_risks: string;
  confidence_level: string;
  confidence_explanation: string;
  coa_summaries: Array<{
    coa_index: number;
    coa_title: string;
    expected_value: number;
    risk_level: string;
    outcome_status: string;
    outcome_description: string;
  }>;
}

export interface WargameAnalysis {
  coa_index: number;
  coa_title: string;
  arc_table: Array<{
    turn: number;
    action: string;
    reaction: string;
    counteraction: string;
    reaction_alternatives: string[];
  }>;
  decision_points: Array<{
    turn: number;
    situation: string;
    trigger_conditions: string;
    options: Array<{
      action: string;
      player: string;
      expected_outcome: number;
      risk: string;
    }>;
    recommended_action: string;
  }>;
  sync_matrix: Array<{
    turn: number;
    time_hours: number;
    actions_by_player: Record<string, string>;
  }>;
  critical_events: Array<{
    turn: number;
    situation: string;
    confidence_width: number;
    best_case: number;
    worst_case: number;
    description: string;
  }>;
  branches: Array<{
    branch_point_turn: number;
    trigger: string;
    situation_at_branch: string;
    recommended_branch_action: string;
    expected_value_if_branched: number;
  }>;
}

export interface GenerationState {
  isGenerating: boolean;
  progress: number;
  status: string;
  error: string;
  etaSeconds: number | null;
  runId: string | null;
  copMapId: string | null;
  coas: GeneratedCOA[];
  scenario: any;
  treeVisualizationHtml: string | null;
  comparison: ComparisonSummary | null;
  recommendation: Recommendation | null;
  criteriaComparison: CriteriaComparison | null;
  decisionMatrix: DecisionMatrix | null;
  wargameAnalyses: WargameAnalysis[];
}
