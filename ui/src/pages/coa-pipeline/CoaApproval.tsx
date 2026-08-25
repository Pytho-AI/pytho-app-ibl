"use client";

import { useState, useEffect } from "react";
import { Badge } from "../../subframe/components/Badge";
import { Button } from "../../subframe/components/Button";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import { TextArea } from "../../subframe/components/TextArea";
import * as SubframeCore from "@subframe/core";
import type {
  GeneratedCOA,
  CriteriaComparison,
  Recommendation,
} from "./types";
import { riskBadgeVariant, confidenceBadgeVariant } from "./utils";
import * as coaApi from "./coaApi";

interface CoaApprovalProps {
  coas: GeneratedCOA[];
  recommendation: Recommendation | null;
  criteriaComparison: CriteriaComparison | null;
  runId?: string | null;
  savedApproval?: coaApi.ApprovalData | null;
}

function CoaApproval({
  coas,
  recommendation,
  criteriaComparison,
  runId,
  savedApproval,
}: CoaApprovalProps) {
  const [commanderGuidance, setCommanderGuidance] = useState("");
  const [approvalStatus, setApprovalStatus] = useState<
    "pending" | "approved" | "approved_modified" | "approved_multiple" | "returned"
  >("pending");
  const [selectedForApproval, setSelectedForApproval] = useState<number[]>([]);
  const [guidanceRequired, setGuidanceRequired] = useState<string | null>(null);

  // Restore saved approval state when loading a run
  useEffect(() => {
    if (!savedApproval) return;
    setApprovalStatus(savedApproval.status as any);
    setSelectedForApproval(savedApproval.selected_coas || []);
    setCommanderGuidance(savedApproval.commander_guidance || "");
  }, [savedApproval]);

  // Pre-select the recommended COA when data first loads
  useEffect(() => {
    if (recommendation?.recommended_coa_index && selectedForApproval.length === 0 && approvalStatus === "pending") {
      setSelectedForApproval([recommendation.recommended_coa_index]);
    }
  }, [recommendation]);

  const recommendedIndex = recommendation?.recommended_coa_index;
  const recommendedCoa = coas.find((c) => c.coa_number === recommendedIndex);
  const hasData = coas.length > 0;

  function toggleCoaSelection(coaNumber: number) {
    if (approvalStatus !== "pending") return;
    setSelectedForApproval((prev) =>
      prev.includes(coaNumber)
        ? prev.filter((n) => n !== coaNumber)
        : [...prev, coaNumber]
    );
  }

  // Persist approval decision via API
  function persistApproval(
    status: string,
    selectedCoas: number[],
    guidance?: string
  ) {
    if (!runId) return;
    coaApi
      .saveApproval(runId, {
        status,
        selected_coas: selectedCoas,
        commander_guidance: guidance || commanderGuidance,
      })
      .catch((err) => console.error("Failed to save approval:", err));
  }

  // Derive strengths/weaknesses from criteria comparison per COA
  function getStrengthsForCoa(coaNumber: number): string[] {
    if (!criteriaComparison) return [];
    const strengths: string[] = [];
    for (const criterion of criteriaComparison.criteria) {
      const rating = criterion.ratings.find((r) => r.coa_index === coaNumber);
      if (rating?.rating === "Strong") {
        strengths.push(rating.rationale);
      }
    }
    return strengths;
  }

  function getRisksForCoa(coaNumber: number): { text: string; severity: string }[] {
    if (!criteriaComparison) return [];
    const risks: { text: string; severity: string }[] = [];
    for (const criterion of criteriaComparison.criteria) {
      const rating = criterion.ratings.find((r) => r.coa_index === coaNumber);
      if (rating?.rating === "Weak") {
        risks.push({ text: rating.rationale, severity: "error" });
      } else if (rating?.rating === "Moderate") {
        risks.push({ text: rating.rationale, severity: "warning" });
      }
    }
    return risks;
  }

  function handleApprove() {
    if (selectedForApproval.length === 0) return;
    setApprovalStatus("approved");
    persistApproval("approved", selectedForApproval);
  }

  function handleApproveModified() {
    if (selectedForApproval.length === 0) return;
    if (!commanderGuidance.trim()) {
      setGuidanceRequired("Enter modifications before approving");
      return;
    }
    setGuidanceRequired(null);
    setApprovalStatus("approved_modified");
    persistApproval("approved_modified", selectedForApproval);
  }

  function handleApproveMultiple() {
    if (selectedForApproval.length < 2) return;
    setApprovalStatus("approved_multiple");
    persistApproval("approved_multiple", selectedForApproval);
  }

  function handleReturn() {
    if (!commanderGuidance.trim()) {
      setGuidanceRequired("Enter refinement direction before returning");
      return;
    }
    setGuidanceRequired(null);
    setSelectedForApproval([]);
    setApprovalStatus("returned");
    persistApproval("returned", []);
  }

  const selectedLabel = selectedForApproval.length > 0
    ? selectedForApproval.map((n) => `COA ${n}`).join(", ")
    : `COA ${recommendedIndex || "—"}`;

  const statusConfig = {
    pending: {
      icon: "FeatherClock" as const,
      variant: "warning" as const,
      label: "Awaiting Commander Decision",
      sublabel: "Staff brief complete",
    },
    approved: {
      icon: "FeatherCheckCircle" as const,
      variant: "success" as const,
      label: `${selectedLabel} Approved`,
      sublabel: "Commander has approved the selected COA",
    },
    approved_modified: {
      icon: "FeatherEdit2" as const,
      variant: "success" as const,
      label: `${selectedLabel} Approved with Modifications`,
      sublabel: "Commander has approved with guidance modifications",
    },
    approved_multiple: {
      icon: "FeatherLayers" as const,
      variant: "success" as const,
      label: `${selectedLabel} Approved`,
      sublabel: "Commander has approved multiple COAs for further development",
    },
    returned: {
      icon: "FeatherRotateCcw" as const,
      variant: "error" as const,
      label: "Returned for Refinement",
      sublabel: "Commander has directed further analysis",
    },
  };

  const currentStatus = statusConfig[approvalStatus];

  return (
    <div className="flex w-full grow shrink-0 basis-0 items-start overflow-hidden">
      {/* Left panel — scrollable content */}
      <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6 self-stretch px-6 py-6 overflow-y-auto">
        {/* No data state */}
        {!hasData && (
          <div className="flex w-full grow items-center justify-center px-6 py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-neutral-300"
                name="FeatherCheckSquare"
              />
              <span className="text-body font-body text-subtext-color">
                No COA data available. Complete previous pipeline steps first.
              </span>
            </div>
          </div>
        )}

        {hasData && (
          <>
            {/* Staff Recommendation Banner */}
            {recommendation && recommendedCoa && (
              <div className="flex w-full flex-col items-start gap-4 rounded-md border-2 border-solid border-brand-600 bg-brand-50 px-6 py-4">
                <div className="flex w-full items-center gap-3">
                  <IconWithBackground
                    variant="brand"
                    size="medium"
                    icon="FeatherStar"
                  />
                  <div className="flex grow shrink-0 basis-0 flex-col items-start">
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      Staff Recommendation
                    </span>
                    <span className="text-body font-body text-subtext-color">
                      Based on wargaming results and comparison criteria
                    </span>
                  </div>
                  <Badge variant="brand" icon="FeatherAward">
                    COA {recommendedIndex} Recommended
                  </Badge>
                </div>
                <div className="flex w-full flex-col items-start gap-2">
                  <span className="text-body font-body text-default-font">
                    {recommendation.recommendation_summary}
                  </span>
                  <div className="flex items-center gap-2">
                    <Badge variant="success" icon="FeatherTrendingUp">
                      {(recommendedCoa.metrics.expected_value * 100).toFixed(0)}% Success Rate
                    </Badge>
                    <Badge
                      variant={riskBadgeVariant(recommendedCoa.risk.level) as any}
                      icon="FeatherAlertTriangle"
                    >
                      {recommendedCoa.risk.level.charAt(0).toUpperCase() +
                        recommendedCoa.risk.level.slice(1)}{" "}
                      Risk
                    </Badge>
                    {recommendation.confidence_level && (
                      <Badge
                        variant={confidenceBadgeVariant(recommendation.confidence_level) as any}
                      >
                        {recommendation.confidence_level} confidence
                      </Badge>
                    )}
                  </div>
                </div>
              </div>
            )}

            {/* COAs under consideration */}
            <div className="flex w-full items-center justify-between">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-2 font-heading-2 text-default-font">
                  Courses of Action Under Consideration
                </span>
                {approvalStatus === "pending" && (
                  <span className="text-caption font-caption text-subtext-color">
                    Click a COA to select it for approval
                  </span>
                )}
              </div>
              <span className="text-body font-body text-subtext-color">
                {coas.length} COA{coas.length !== 1 ? "s" : ""} Presented
              </span>
            </div>

            <div className="flex w-full gap-4 items-stretch">
              {coas.map((coa) => {
                const isRecommended = coa.coa_number === recommendedIndex;
                const strengths = getStrengthsForCoa(coa.coa_number);
                const risks = getRisksForCoa(coa.coa_number);
                const ev = coa.metrics.expected_value;
                const isSelected = selectedForApproval.includes(coa.coa_number);
                const isPending = approvalStatus === "pending";

                return (
                  <div
                    key={coa.coa_number}
                    onClick={() => toggleCoaSelection(coa.coa_number)}
                    className={`flex grow shrink-0 basis-0 flex-col items-start gap-4 rounded-md border px-4 py-4 transition-all ${
                      isPending ? "cursor-pointer hover:shadow-md" : ""
                    } ${
                      isSelected && !isPending
                        ? "border-2 border-solid border-success-600 bg-success-50"
                        : isSelected && isPending
                        ? "border-2 border-solid border-brand-600 bg-brand-50 ring-2 ring-brand-200"
                        : isRecommended
                        ? "border-2 border-solid border-brand-300 bg-brand-50"
                        : "border border-solid border-neutral-border bg-neutral-50"
                    }`}
                  >
                    {/* Card header */}
                    <div className="flex w-full items-center justify-between">
                      <span className="text-heading-3 font-heading-3 text-default-font">
                        COA {coa.coa_number}
                      </span>
                      <div className="flex items-center gap-2">
                        {isSelected && !isPending && (
                          <Badge variant="success" icon="FeatherCheck">
                            Approved
                          </Badge>
                        )}
                        {isSelected && isPending && (
                          <Badge variant="brand" icon="FeatherCheckSquare">
                            Selected
                          </Badge>
                        )}
                        {isRecommended && (
                          <Badge variant={isSelected && isPending ? "brand" : "neutral"} icon="FeatherStar">
                            Recommended
                          </Badge>
                        )}
                        {!isRecommended && !isSelected && (
                          <Badge variant="neutral">
                            {coa.risk.level}
                          </Badge>
                        )}
                      </div>
                    </div>

                    {/* Operational approach */}
                    <div className="flex w-full flex-col items-start gap-2">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        OPERATIONAL APPROACH
                      </span>
                      <span className="text-body font-body text-default-font">
                        {coa.summary}
                      </span>
                    </div>

                    <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />

                    {/* Key strengths */}
                    <div className="flex w-full flex-col items-start gap-2">
                      <span className="text-caption-bold font-caption-bold text-success-600">
                        KEY STRENGTHS
                      </span>
                      <div className="flex w-full flex-col items-start gap-1">
                        {strengths.length > 0 ? (
                          strengths.map((s, idx) => (
                            <div key={idx} className="flex items-center gap-2">
                              <SubframeCore.Icon
                                className="text-caption font-caption text-success-600"
                                name="FeatherCheck"
                              />
                              <span className="text-caption font-caption text-default-font">
                                {s}
                              </span>
                            </div>
                          ))
                        ) : (
                          <div className="flex items-center gap-2">
                            <SubframeCore.Icon
                              className="text-caption font-caption text-success-600"
                              name="FeatherCheck"
                            />
                            <span className="text-caption font-caption text-default-font">
                              {coa.outcome.description}
                            </span>
                          </div>
                        )}
                      </div>
                    </div>

                    {/* Key risks */}
                    <div className="flex w-full flex-col items-start gap-2">
                      <span
                        className={`text-caption-bold font-caption-bold ${
                          risks.some((r) => r.severity === "error")
                            ? "text-error-600"
                            : "text-warning-600"
                        }`}
                      >
                        KEY RISKS
                      </span>
                      <div className="flex w-full flex-col items-start gap-1">
                        {risks.length > 0 ? (
                          risks.map((r, idx) => (
                            <div key={idx} className="flex items-center gap-2">
                              <SubframeCore.Icon
                                className={`text-caption font-caption ${
                                  r.severity === "error"
                                    ? "text-error-600"
                                    : "text-warning-600"
                                }`}
                                name="FeatherAlertTriangle"
                              />
                              <span className="text-caption font-caption text-default-font">
                                {r.text}
                              </span>
                            </div>
                          ))
                        ) : (
                          <div className="flex items-center gap-2">
                            <SubframeCore.Icon
                              className="text-caption font-caption text-warning-600"
                              name="FeatherAlertTriangle"
                            />
                            <span className="text-caption font-caption text-default-font">
                              {coa.risk.description}
                            </span>
                          </div>
                        )}
                      </div>
                    </div>

                    <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />

                    {/* Staff assessment */}
                    <div className="flex w-full items-center justify-between">
                      <span className="text-caption font-caption text-subtext-color">
                        Staff Assessment
                      </span>
                      <span
                        className={`text-caption-bold font-caption-bold ${
                          isRecommended ? "text-brand-600" : "text-default-font"
                        }`}
                      >
                        {(ev * 100).toFixed(0)}% Success Probability
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </div>

      {/* Right sidebar — Commander Decision */}
      <div className="flex w-96 flex-none flex-col items-start gap-6 self-stretch border-l border-solid border-neutral-border bg-neutral-50 px-6 py-6 overflow-y-auto">
        <div className="flex w-full flex-col items-start gap-2">
          <span className="text-heading-2 font-heading-2 text-default-font">
            Commander Decision
          </span>
          <span className="text-body font-body text-subtext-color">
            Formal command decision point
          </span>
        </div>

        {/* Decision Status */}
        <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
          <span className="text-caption-bold font-caption-bold text-subtext-color">
            DECISION STATUS
          </span>
          <div className="flex w-full items-center gap-3">
            <IconWithBackground
              variant={currentStatus.variant}
              size="medium"
              icon={currentStatus.icon}
            />
            <div className="flex grow shrink-0 basis-0 flex-col items-start">
              <span className="text-body-bold font-body-bold text-default-font">
                {currentStatus.label}
              </span>
              <span className="text-caption font-caption text-subtext-color">
                {currentStatus.sublabel}
              </span>
            </div>
          </div>
        </div>

        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />

        {/* Commander Actions */}
        <div className="flex w-full flex-col items-start gap-3">
          <span className="text-caption-bold font-caption-bold text-subtext-color">
            COMMANDER ACTIONS
          </span>
          <Button
            className="h-12 w-full flex-none"
            variant="brand-primary"
            size="large"
            icon="FeatherCheck"
            disabled={!hasData || approvalStatus !== "pending" || selectedForApproval.length === 0}
            onClick={handleApprove}
          >
            {selectedForApproval.length > 0
              ? `Approve ${selectedForApproval.map((n) => `COA ${n}`).join(", ")}`
              : "Select a COA to Approve"}
          </Button>
          <Button
            className="h-12 w-full flex-none"
            variant="neutral-secondary"
            size="large"
            icon="FeatherEdit2"
            disabled={!hasData || approvalStatus !== "pending" || selectedForApproval.length === 0}
            onClick={handleApproveModified}
          >
            Approve with Modification
          </Button>
          <Button
            className="h-12 w-full flex-none"
            variant="neutral-secondary"
            size="large"
            icon="FeatherLayers"
            disabled={!hasData || approvalStatus !== "pending" || selectedForApproval.length < 2}
            onClick={handleApproveMultiple}
          >
            {selectedForApproval.length >= 2
              ? `Approve ${selectedForApproval.length} COAs`
              : "Select 2+ COAs to Approve Multiple"}
          </Button>
          <Button
            className="h-12 w-full flex-none"
            variant="neutral-tertiary"
            size="large"
            icon="FeatherRotateCcw"
            disabled={!hasData || approvalStatus !== "pending"}
            onClick={handleReturn}
          >
            Return for Refinement
          </Button>
        </div>

        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />

        {/* Commander Guidance */}
        <div className="flex w-full flex-col items-start gap-3">
          <div className="flex w-full items-center justify-between">
            <span className="text-caption-bold font-caption-bold text-subtext-color">
              COMMANDER GUIDANCE
            </span>
            {guidanceRequired && (
              <span className="text-caption font-caption text-error-600">
                {guidanceRequired}
              </span>
            )}
          </div>
          <TextArea
            className={`h-auto w-full flex-none ${guidanceRequired ? "ring-2 ring-error-300" : ""}`}
            variant="filled"
            helpText="Add direction, accepted risks, or modifications"
          >
            <TextArea.Input
              className="h-auto min-h-[128px] w-full flex-none"
              placeholder="Enter commander guidance or notes..."
              value={commanderGuidance}
              onChange={(e) => {
                setCommanderGuidance(e.target.value);
                if (guidanceRequired && e.target.value.trim()) {
                  setGuidanceRequired(null);
                }
              }}
            />
          </TextArea>
        </div>

        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />

        {/* Risk Acceptance */}
        {recommendation && recommendedCoa && (
          <>
            <div className="flex w-full flex-col items-start gap-3">
              <span className="text-caption-bold font-caption-bold text-subtext-color">
                RISK ACCEPTANCE
              </span>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-warning-600 bg-warning-50 px-3 py-3">
                <div className="flex items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-warning-600"
                    name="FeatherAlertTriangle"
                  />
                  <span className="text-caption-bold font-caption-bold text-default-font">
                    Accepted Risks (COA {recommendedIndex})
                  </span>
                </div>
                <span className="text-caption font-caption text-default-font">
                  {recommendation.key_risks}
                </span>
              </div>
            </div>

            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
          </>
        )}
      </div>
    </div>
  );
}

export default CoaApproval;
