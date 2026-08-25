"use client";

import { useState } from "react";
import { Badge } from "../../subframe/components/Badge";
import { Button } from "../../subframe/components/Button";
import { DataFieldHorizontal } from "../../subframe/components/DataFieldHorizontal";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import { Progress } from "../../subframe/components/Progress";
import * as SubframeCore from "@subframe/core";
import type { GeneratedCOA, WargameAnalysis } from "./types";
import { evColorClass } from "./utils";

interface CoaWargameProps {
  coas: GeneratedCOA[];
  wargameAnalyses: WargameAnalysis[];
  isGenerating: boolean;
  progress: number;
  status: string;
  error?: string;
  etaSeconds?: number | null;
  onProceedToComparison: () => void;
}

function CoaWargame({ coas, wargameAnalyses, isGenerating, progress, status, error, etaSeconds, onProceedToComparison }: CoaWargameProps) {
  const [selectedCoa, setSelectedCoa] = useState<number | null>(null);

  const hasData = wargameAnalyses.length > 0;
  const selectedAnalysis = wargameAnalyses.find((w) => w.coa_index === selectedCoa);
  const selectedCoaData = coas.find((c) => c.coa_number === selectedCoa);

  // Compute summary stats
  const totalDecisionPoints = wargameAnalyses.reduce(
    (sum, w) => sum + w.decision_points.length,
    0
  );
  const totalCriticalEvents = wargameAnalyses.reduce(
    (sum, w) => sum + w.critical_events.length,
    0
  );
  const totalBranches = wargameAnalyses.reduce(
    (sum, w) => sum + w.branches.length,
    0
  );

  return (
    <div className="flex w-full grow shrink-0 basis-0 flex-col items-start overflow-hidden">
      {/* Main content area */}
      <div className="flex w-full grow shrink-0 basis-0 flex-col items-start overflow-auto">
        {/* Header */}
        <div className="flex w-full flex-col items-start gap-4 px-6 py-4">
          <div className="flex w-full items-center justify-between">
            <div className="flex items-center gap-3">
              <IconWithBackground
                variant="brand"
                size="large"
                icon="FeatherSword"
              />
              <div className="flex flex-col items-start">
                <span className="text-heading-2 font-heading-2 text-default-font">
                  COA Wargame
                </span>
                <span className="text-body font-body text-subtext-color">
                  {hasData
                    ? `${wargameAnalyses.length} COA${wargameAnalyses.length !== 1 ? "s" : ""} analyzed — ARC tables, decision points, and branch plans`
                    : isGenerating
                    ? "Wargaming in progress..."
                    : "Proceed from COA Development to start wargaming"}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Loading state */}
        {!hasData && isGenerating && (
          <div className="flex w-full grow items-center justify-center px-6 py-12">
            <div className="flex w-full max-w-md flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-brand-600 animate-pulse"
                name="FeatherSword"
              />
              <span className="text-body-bold font-body-bold text-default-font text-center">
                {status || "Running wargame analysis..."}
              </span>
              <div className="flex w-full items-center gap-2">
                <Progress value={progress} />
                {progress > 0 && (
                  <span className="text-caption font-caption text-subtext-color tabular-nums whitespace-nowrap">
                    {progress.toFixed(0)}%
                    {etaSeconds != null && etaSeconds > 0 && (
                      <> &middot; {etaSeconds >= 60 ? `${Math.floor(etaSeconds / 60)}m ${Math.round(etaSeconds % 60)}s` : `${Math.round(etaSeconds)}s`} remaining</>
                    )}
                  </span>
                )}
              </div>
            </div>
          </div>
        )}

        {/* Error state */}
        {error && !isGenerating && (
          <div className="flex w-full items-center gap-3 rounded-md border border-solid border-error-400 bg-error-50 mx-6 mt-4 px-4 py-3">
            <SubframeCore.Icon
              className="text-body font-body text-error-600"
              name="FeatherAlertCircle"
            />
            <span className="text-body font-body text-error-800">{error}</span>
          </div>
        )}

        {/* No data state */}
        {!hasData && !isGenerating && !error && (
          <div className="flex w-full grow items-center justify-center px-6 py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-neutral-300"
                name="FeatherShield"
              />
              <span className="text-body font-body text-subtext-color">
                No wargame data available. Generate COAs in Step 2 first.
              </span>
            </div>
          </div>
        )}

        {hasData && (
          <>
            {/* Scrollable COA cards */}
            <div className="flex w-full flex-col items-start gap-4 px-6 py-4">
              <div className="flex w-full items-center justify-between">
                <div className="flex items-center gap-2">
                  <SubframeCore.Icon
                    className="text-heading-3 font-heading-3 text-brand-primary"
                    name="FeatherActivity"
                  />
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    Wargame Results
                  </span>
                </div>
                <span className="text-caption font-caption text-subtext-color">
                  {wargameAnalyses.length} COAs wargamed
                </span>
              </div>

              <div className="flex w-full items-start gap-4 overflow-x-auto pb-2">
                {wargameAnalyses.map((analysis) => {
                  const coa = coas.find((c) => c.coa_number === analysis.coa_index);
                  const isSelected = selectedCoa === analysis.coa_index;
                  const ev = coa?.metrics.expected_value ?? 0;
                  const evColor = evColorClass(ev);
                  const badgeVariant =
                    ev >= 0.7 ? "success" : ev >= 0.4 ? "warning" : "error";

                  return (
                    <div
                      key={analysis.coa_index}
                      className={`flex w-80 flex-none cursor-pointer flex-col items-start gap-3 self-stretch rounded-md border border-solid px-4 py-4 transition-colors ${
                        isSelected
                          ? "border-brand-600 bg-brand-50"
                          : "border-neutral-border bg-neutral-50 hover:border-brand-200"
                      }`}
                      onClick={() => setSelectedCoa(analysis.coa_index)}
                    >
                      <div className="flex w-full items-center justify-between">
                        <span className="text-heading-3 font-heading-3 text-default-font line-clamp-1">
                          COA {analysis.coa_index}: {analysis.coa_title}
                        </span>
                        <Badge variant={badgeVariant as any}>
                          {coa?.risk.level || "—"}
                        </Badge>
                      </div>

                      <div className="flex w-full flex-col items-start gap-2">
                        <DataFieldHorizontal
                          icon="FeatherTrendingUp"
                          label="Expected Value"
                        >
                          <span className={`text-body-bold font-body-bold ${evColor}`}>
                            {(ev * 100).toFixed(0)}%
                          </span>
                        </DataFieldHorizontal>
                        <DataFieldHorizontal
                          icon="FeatherAlertTriangle"
                          label="Decision Points"
                        >
                          <span className="text-body-bold font-body-bold text-default-font">
                            {analysis.decision_points.length}
                          </span>
                        </DataFieldHorizontal>
                        <DataFieldHorizontal
                          icon="FeatherZap"
                          label="Critical Events"
                        >
                          <span className="text-body-bold font-body-bold text-default-font">
                            {analysis.critical_events.length}
                          </span>
                        </DataFieldHorizontal>
                        <DataFieldHorizontal
                          icon="FeatherGitBranch"
                          label="Branch Plans"
                        >
                          <Badge variant={analysis.branches.length > 0 ? "warning" : "success"}>
                            {analysis.branches.length} found
                          </Badge>
                        </DataFieldHorizontal>
                      </div>

                      {/* ARC summary */}
                      <div className="flex w-full flex-col items-start gap-2 border-t border-solid border-neutral-border pt-2">
                        <span className="text-caption-bold font-caption-bold text-subtext-color">
                          ARC TABLE
                        </span>
                        {analysis.arc_table.slice(0, 2).map((arc, idx) => (
                          <div key={idx} className="flex w-full items-start gap-2">
                            <SubframeCore.Icon
                              className="text-caption font-caption text-brand-600 mt-0.5"
                              name="FeatherChevronRight"
                            />
                            <span className="text-caption font-caption text-default-font line-clamp-1">
                              T{arc.turn}: {arc.action}
                            </span>
                          </div>
                        ))}
                        {analysis.arc_table.length > 2 && (
                          <span className="text-caption font-caption text-subtext-color">
                            +{analysis.arc_table.length - 2} more turns
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Selected COA detail */}
            {selectedAnalysis && selectedCoaData && (
              <div className="flex w-full flex-col items-start gap-4 px-6 py-4 border-t border-solid border-neutral-border">
                <div className="flex w-full items-center gap-2">
                  <IconWithBackground icon="FeatherFileText" variant="brand" />
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    COA {selectedAnalysis.coa_index} Wargame Detail
                  </span>
                </div>

                <div className="grid w-full grid-cols-1 gap-4 lg:grid-cols-2">
                  {/* ARC Table */}
                  <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                    <div className="flex w-full items-center gap-2">
                      <IconWithBackground icon="FeatherRepeat" variant="warning" />
                      <span className="text-body-bold font-body-bold text-default-font">
                        Action-Reaction-Counteraction
                      </span>
                    </div>
                    <div className="flex w-full flex-col items-start gap-2 max-h-64 overflow-y-auto">
                      {selectedAnalysis.arc_table.map((arc, idx) => (
                        <div
                          key={idx}
                          className="flex w-full flex-col items-start gap-1 rounded-md bg-neutral-50 px-3 py-2 border border-neutral-100"
                        >
                          <div className="flex w-full items-center gap-2">
                            <span className="text-caption-bold font-caption-bold text-brand-600">
                              Turn {arc.turn}
                            </span>
                          </div>
                          <div className="flex w-full items-start gap-2">
                            <span className="text-caption-bold font-caption-bold text-success-600 w-6 flex-none">A:</span>
                            <span className="text-caption font-caption text-default-font">{arc.action}</span>
                          </div>
                          <div className="flex w-full items-start gap-2">
                            <span className="text-caption-bold font-caption-bold text-error-600 w-6 flex-none">R:</span>
                            <span className="text-caption font-caption text-default-font">{arc.reaction}</span>
                          </div>
                          <div className="flex w-full items-start gap-2">
                            <span className="text-caption-bold font-caption-bold text-brand-600 w-6 flex-none">C:</span>
                            <span className="text-caption font-caption text-default-font">{arc.counteraction}</span>
                          </div>
                          {arc.reaction_alternatives.length > 0 && (
                            <div className="flex w-full flex-col items-start gap-1 mt-1 pl-6">
                              <span className="text-caption font-caption text-subtext-color">
                                Alt reactions: {arc.reaction_alternatives.length}
                              </span>
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Decision Points */}
                  <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                    <div className="flex w-full items-center gap-2">
                      <IconWithBackground icon="FeatherAlertTriangle" variant="error" />
                      <span className="text-body-bold font-body-bold text-default-font">
                        Decision Points
                      </span>
                      <Badge variant="neutral">{selectedAnalysis.decision_points.length}</Badge>
                    </div>
                    {selectedAnalysis.decision_points.length === 0 ? (
                      <span className="text-caption font-caption text-subtext-color">
                        No high-variance decision points identified.
                      </span>
                    ) : (
                      <div className="flex w-full flex-col items-start gap-2 max-h-64 overflow-y-auto">
                        {selectedAnalysis.decision_points.map((dp, idx) => (
                          <div
                            key={idx}
                            className="flex w-full flex-col items-start gap-1 rounded-md bg-neutral-50 px-3 py-2 border border-neutral-100"
                          >
                            <div className="flex w-full items-center justify-between">
                              <span className="text-caption-bold font-caption-bold text-error-600">
                                Turn {dp.turn}
                              </span>
                              <span className="text-caption font-caption text-subtext-color">
                                {dp.options.length} options
                              </span>
                            </div>
                            <span className="text-caption font-caption text-default-font line-clamp-2">
                              {dp.situation}
                            </span>
                            <span className="text-caption font-caption text-subtext-color">
                              {dp.trigger_conditions}
                            </span>
                            <div className="flex w-full items-start gap-2 mt-1">
                              <SubframeCore.Icon
                                className="text-caption font-caption text-success-600 mt-0.5"
                                name="FeatherCheckCircle"
                              />
                              <span className="text-caption font-caption text-success-700 line-clamp-1">
                                Recommended: {dp.recommended_action}
                              </span>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Synchronization Matrix */}
                  <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                    <div className="flex w-full items-center gap-2">
                      <IconWithBackground icon="FeatherClock" variant="brand" />
                      <span className="text-body-bold font-body-bold text-default-font">
                        Synchronization Matrix
                      </span>
                    </div>
                    <div className="flex w-full flex-col items-start gap-2 max-h-64 overflow-y-auto">
                      {selectedAnalysis.sync_matrix.map((entry, idx) => (
                        <div
                          key={idx}
                          className="flex w-full flex-col items-start gap-1 rounded-md bg-neutral-50 px-3 py-2 border border-neutral-100"
                        >
                          <div className="flex w-full items-center justify-between">
                            <span className="text-caption-bold font-caption-bold text-brand-600">
                              Turn {entry.turn}
                            </span>
                            <span className="text-caption font-caption text-subtext-color">
                              {entry.time_hours.toFixed(1)}h
                            </span>
                          </div>
                          {Object.entries(entry.actions_by_player).map(
                            ([player, action]) => (
                              <div key={player} className="flex w-full items-start gap-2">
                                <span className="text-caption-bold font-caption-bold text-default-font w-24 flex-none truncate">
                                  {player}:
                                </span>
                                <span className="text-caption font-caption text-subtext-color line-clamp-1">
                                  {action}
                                </span>
                              </div>
                            )
                          )}
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* Critical Events + Branch Plans */}
                  <div className="flex w-full flex-col items-start gap-4">
                    {/* Critical Events */}
                    <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                      <div className="flex w-full items-center gap-2">
                        <IconWithBackground icon="FeatherZap" variant="warning" />
                        <span className="text-body-bold font-body-bold text-default-font">
                          Critical Events
                        </span>
                        <Badge variant="neutral">{selectedAnalysis.critical_events.length}</Badge>
                      </div>
                      {selectedAnalysis.critical_events.length === 0 ? (
                        <span className="text-caption font-caption text-subtext-color">
                          No high-uncertainty critical events detected.
                        </span>
                      ) : (
                        <div className="flex w-full flex-col items-start gap-2">
                          {selectedAnalysis.critical_events.map((ce, idx) => (
                            <div
                              key={idx}
                              className="flex w-full flex-col items-start gap-1 rounded-md bg-warning-50 px-3 py-2 border border-warning-200"
                            >
                              <div className="flex w-full items-center justify-between">
                                <span className="text-caption-bold font-caption-bold text-warning-700">
                                  Turn {ce.turn}
                                </span>
                                <span className="text-caption font-caption text-subtext-color">
                                  CI width: {(ce.confidence_width * 100).toFixed(0)}%
                                </span>
                              </div>
                              <span className="text-caption font-caption text-default-font line-clamp-2">
                                {ce.situation}
                              </span>
                              <div className="flex w-full items-center gap-4">
                                <span className="text-caption font-caption text-success-600">
                                  Best: {(ce.best_case * 100).toFixed(0)}%
                                </span>
                                <span className="text-caption font-caption text-error-600">
                                  Worst: {(ce.worst_case * 100).toFixed(0)}%
                                </span>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>

                    {/* Branch Plans */}
                    <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                      <div className="flex w-full items-center gap-2">
                        <IconWithBackground icon="FeatherGitBranch" variant="error" />
                        <span className="text-body-bold font-body-bold text-default-font">
                          Branch / Sequel Plans
                        </span>
                        <Badge variant="neutral">{selectedAnalysis.branches.length}</Badge>
                      </div>
                      {selectedAnalysis.branches.length === 0 ? (
                        <span className="text-caption font-caption text-subtext-color">
                          No breakdown points identified — COA maintains viability throughout.
                        </span>
                      ) : (
                        <div className="flex w-full flex-col items-start gap-2">
                          {selectedAnalysis.branches.map((br, idx) => (
                            <div
                              key={idx}
                              className="flex w-full flex-col items-start gap-1 rounded-md bg-error-50 px-3 py-2 border border-error-200"
                            >
                              <div className="flex w-full items-center justify-between">
                                <span className="text-caption-bold font-caption-bold text-error-700">
                                  Branch at Turn {br.branch_point_turn}
                                </span>
                                <span className="text-caption font-caption text-subtext-color">
                                  EV if branched: {(br.expected_value_if_branched * 100).toFixed(0)}%
                                </span>
                              </div>
                              <span className="text-caption font-caption text-default-font line-clamp-2">
                                Trigger: {br.trigger}
                              </span>
                              <div className="flex w-full items-start gap-2 mt-1">
                                <SubframeCore.Icon
                                  className="text-caption font-caption text-brand-600 mt-0.5"
                                  name="FeatherArrowRight"
                                />
                                <span className="text-caption font-caption text-brand-700 line-clamp-2">
                                  {br.recommended_branch_action}
                                </span>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>

      {/* Bottom summary bar */}
      <div className="flex w-full flex-none items-center justify-between border-t border-solid border-neutral-border bg-neutral-50 px-6 py-4">
        <div className="flex items-start gap-6">
          {/* Overall */}
          <div className="flex flex-col items-start gap-1">
            <span className="text-caption-bold font-caption-bold text-subtext-color">
              OVERALL
            </span>
            <div className="flex items-center gap-2">
              <span className="text-body-bold font-body-bold text-default-font">
                {wargameAnalyses.length} COAs wargamed
              </span>
              {hasData && (
                <Badge variant="success">Complete</Badge>
              )}
            </div>
            {hasData && (
              <Progress value={100} />
            )}
          </div>

          {/* Findings */}
          {hasData && (
            <div className="flex flex-col items-start gap-1">
              <span className="text-caption-bold font-caption-bold text-subtext-color">
                FINDINGS
              </span>
              <div className="flex items-center gap-3">
                <div className="flex items-center gap-1">
                  <SubframeCore.Icon
                    className="text-caption font-caption text-error-600"
                    name="FeatherAlertTriangle"
                  />
                  <span className="text-body font-body text-default-font">
                    {totalDecisionPoints} decision pts
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  <SubframeCore.Icon
                    className="text-caption font-caption text-warning-600"
                    name="FeatherZap"
                  />
                  <span className="text-body font-body text-default-font">
                    {totalCriticalEvents} critical events
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  <SubframeCore.Icon
                    className="text-caption font-caption text-brand-600"
                    name="FeatherGitBranch"
                  />
                  <span className="text-body font-body text-default-font">
                    {totalBranches} branches
                  </span>
                </div>
              </div>
            </div>
          )}

        </div>

        <div className="flex items-center gap-3">
          <Button
            size="large"
            iconRight="FeatherArrowRight"
            disabled={!hasData || isGenerating}
            onClick={onProceedToComparison}
          >
            Proceed to Comparison
          </Button>
        </div>
      </div>
    </div>
  );
}

export default CoaWargame;
