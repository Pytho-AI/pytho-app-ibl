"use client";

import { Badge } from "../../subframe/components/Badge";
import { Button } from "../../subframe/components/Button";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import { Progress } from "../../subframe/components/Progress";
import * as SubframeCore from "@subframe/core";
import type {
  GeneratedCOA,
  CriteriaComparison as CriteriaComparisonType,
  DecisionMatrix as DecisionMatrixType,
  Recommendation as RecommendationType,
} from "./types";
import { evColorClass, riskBadgeVariant, ratingBadgeVariant, confidenceBadgeVariant } from "./utils";

interface CoaComparisonProps {
  coas: GeneratedCOA[];
  criteriaComparison: CriteriaComparisonType | null;
  decisionMatrix: DecisionMatrixType | null;
  recommendation: RecommendationType | null;
  isGenerating: boolean;
  status: string;
  onProceedToApproval: () => void;
}

function CoaComparison({
  coas,
  criteriaComparison,
  decisionMatrix,
  recommendation,
  isGenerating,
  status,
  onProceedToApproval,
}: CoaComparisonProps) {
  const hasData = criteriaComparison !== null || decisionMatrix !== null;
  const recommendedIndex = recommendation?.recommended_coa_index;

  // Get ranked COAs from decision matrix, or fall back to coas order
  const rankedCoas = decisionMatrix?.coas
    ? [...decisionMatrix.coas].sort((a, b) => a.rank - b.rank)
    : null;

  return (
    <div className="flex w-full grow shrink-0 basis-0 flex-col items-start overflow-hidden">
      {/* Main content area */}
      <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 overflow-auto px-6 py-6">
        {/* Header */}
        <div className="flex w-full items-center justify-between">
          <div className="flex items-center gap-3">
            <IconWithBackground
              variant="brand"
              size="large"
              icon="FeatherBarChart2"
            />
            <div className="flex flex-col items-start">
              <span className="text-heading-2 font-heading-2 text-default-font">
                COA Comparison
              </span>
              <span className="text-body font-body text-subtext-color">
                {hasData
                  ? "Criteria evaluation, decision matrix, and staff recommendation"
                  : "Comparison data will appear here after COA generation completes"}
              </span>
            </div>
          </div>
        </div>

        {/* Loading state */}
        {!hasData && isGenerating && (
          <div className="flex w-full grow items-center justify-center px-6 py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-brand-600 animate-pulse"
                name="FeatherBarChart2"
              />
              <span className="text-body-bold font-body-bold text-default-font">
                {status || "Running comparison analysis..."}
              </span>
              <Progress value={undefined} />
            </div>
          </div>
        )}

        {/* No data state */}
        {!hasData && !isGenerating && (
          <div className="flex w-full grow items-center justify-center px-6 py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-neutral-300"
                name="FeatherBarChart2"
              />
              <span className="text-body font-body text-subtext-color">
                No comparison data available. Complete wargaming in Step 3 first.
              </span>
            </div>
          </div>
        )}

        {hasData && (
          <>
            {/* COA Summary Cards */}
            <div className="flex w-full flex-col items-start gap-4">
              <span className="text-heading-2 font-heading-2 text-default-font">
                Courses of Action Under Comparison
              </span>
              <div className="flex w-full items-start gap-4">
                {coas.map((coa) => {
                  const isRecommended = coa.coa_number === recommendedIndex;
                  const matrixEntry = decisionMatrix?.coas.find(
                    (m) => m.coa_index === coa.coa_number
                  );
                  return (
                    <div
                      key={coa.coa_number}
                      className={`flex grow shrink-0 basis-0 flex-col items-start gap-3 rounded-md border border-solid px-4 py-4 ${
                        isRecommended
                          ? "border-brand-600 bg-brand-50"
                          : "border-neutral-border bg-neutral-50"
                      }`}
                    >
                      <div className="flex w-full items-center justify-between">
                        <div className="flex items-center gap-2">
                          <IconWithBackground
                            variant={isRecommended ? "brand" : "neutral"}
                            size="medium"
                            icon="FeatherTarget"
                          />
                          <span className="text-heading-3 font-heading-3 text-default-font">
                            COA {coa.coa_number}
                          </span>
                        </div>
                        <div className="flex items-center gap-2">
                          {matrixEntry && (
                            <Badge variant="neutral">
                              Rank #{matrixEntry.rank}
                            </Badge>
                          )}
                          {isRecommended && (
                            <Badge variant="brand">Staff Preferred</Badge>
                          )}
                        </div>
                      </div>
                      <span className="text-body-bold font-body-bold text-default-font line-clamp-2">
                        {coa.summary}
                      </span>
                      <div className="flex w-full items-center gap-4">
                        <span
                          className={`text-caption font-caption ${evColorClass(coa.metrics.expected_value)}`}
                        >
                          EV: {(coa.metrics.expected_value * 100).toFixed(0)}%
                        </span>
                        <Badge
                          variant={riskBadgeVariant(coa.risk.level) as any}
                        >
                          {coa.risk.level} risk
                        </Badge>
                        {matrixEntry && (
                          <span className="text-caption font-caption text-subtext-color">
                            Score: {matrixEntry.score_percentage.toFixed(0)}%
                          </span>
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>

            {/* Criteria Comparison Table */}
            {criteriaComparison && criteriaComparison.criteria.length > 0 && (
              <div className="flex w-full flex-col items-start gap-4">
                <div className="flex w-full items-center justify-between">
                  <span className="text-heading-2 font-heading-2 text-default-font">
                    Criteria Comparison
                  </span>
                  <span className="text-caption font-caption text-subtext-color">
                    Criteria defined by commander during COA guidance
                  </span>
                </div>
                <div className="flex w-full flex-col items-start overflow-hidden rounded-md border border-solid border-neutral-border">
                  {/* Table header */}
                  <div className="flex w-full items-center bg-neutral-100">
                    <div className="flex w-48 flex-none items-center border-r border-solid border-neutral-border px-4 py-3">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        CRITERION
                      </span>
                    </div>
                    {coas.map((coa) => {
                      const isRecommended = coa.coa_number === recommendedIndex;
                      const isLast = coa.coa_number === coas[coas.length - 1]?.coa_number;
                      return (
                        <div
                          key={coa.coa_number}
                          className={`flex grow shrink-0 basis-0 items-center justify-center px-4 py-3 ${
                            !isLast ? "border-r border-solid border-neutral-border" : ""
                          }`}
                        >
                          <span
                            className={`text-caption-bold font-caption-bold ${
                              isRecommended ? "text-brand-600" : "text-subtext-color"
                            }`}
                          >
                            COA {coa.coa_number}
                          </span>
                        </div>
                      );
                    })}
                  </div>

                  {/* Table rows */}
                  {criteriaComparison.criteria.map((criterion, idx) => {
                    const isLast = idx === criteriaComparison.criteria.length - 1;
                    return (
                      <div
                        key={criterion.name}
                        className={`flex w-full bg-default-background items-stretch ${
                          !isLast ? "border-b border-solid border-neutral-border" : ""
                        }`}
                      >
                        <div className="flex w-48 flex-none flex-col items-start justify-center gap-1 border-r border-solid border-neutral-border px-4 py-3">
                          <span className="text-body-bold font-body-bold text-default-font">
                            {criterion.name}
                          </span>
                          <span className="text-caption font-caption text-subtext-color">
                            Weight: {criterion.weight}
                          </span>
                        </div>
                        {coas.map((coa) => {
                          const rating = criterion.ratings.find(
                            (r) => r.coa_index === coa.coa_number
                          );
                          const isLastCoa =
                            coa.coa_number === coas[coas.length - 1]?.coa_number;
                          return (
                            <div
                              key={coa.coa_number}
                              className={`flex grow shrink-0 basis-0 flex-col items-center justify-center gap-2 px-4 py-3 ${
                                !isLastCoa
                                  ? "border-r border-solid border-neutral-border"
                                  : ""
                              }`}
                            >
                              {rating ? (
                                <>
                                  <Badge
                                    variant={ratingBadgeVariant(rating.rating) as any}
                                  >
                                    {rating.rating}
                                  </Badge>
                                  <span className="text-caption font-caption text-subtext-color text-center">
                                    {rating.rationale}
                                  </span>
                                </>
                              ) : (
                                <span className="text-caption font-caption text-subtext-color">
                                  —
                                </span>
                              )}
                            </div>
                          );
                        })}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Decision Matrix */}
            {rankedCoas && rankedCoas.length > 0 && (
              <div className="flex w-full flex-col items-start gap-4">
                <span className="text-heading-2 font-heading-2 text-default-font">
                  Decision Matrix
                </span>
                <div className="flex w-full flex-col items-start gap-3">
                  {rankedCoas.map((entry) => {
                    const isRecommended = entry.coa_index === recommendedIndex;
                    return (
                      <div
                        key={entry.coa_index}
                        className={`flex w-full items-center gap-4 rounded-md border border-solid px-4 py-3 ${
                          isRecommended
                            ? "border-brand-600 bg-brand-50"
                            : "border-neutral-border bg-neutral-50"
                        }`}
                      >
                        <div className="flex w-8 items-center justify-center">
                          <span className="text-heading-3 font-heading-3 text-default-font">
                            #{entry.rank}
                          </span>
                        </div>
                        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1">
                          <div className="flex items-center gap-2">
                            <span className="text-body-bold font-body-bold text-default-font">
                              COA {entry.coa_index}: {entry.coa_title}
                            </span>
                            {isRecommended && (
                              <Badge variant="brand">Recommended</Badge>
                            )}
                          </div>
                        </div>
                        <div className="flex items-center gap-4">
                          <div className="flex flex-col items-end gap-1">
                            <span className="text-caption-bold font-caption-bold text-subtext-color">
                              SCORE
                            </span>
                            <span className="text-body-bold font-body-bold text-default-font">
                              {entry.weighted_score.toFixed(1)} / {entry.max_possible.toFixed(1)}
                            </span>
                          </div>
                          <div className="flex w-32 flex-col items-start gap-1">
                            <Progress value={entry.score_percentage} />
                            <span className="text-caption font-caption text-subtext-color">
                              {entry.score_percentage.toFixed(0)}%
                            </span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* Key Tradeoffs + Staff Recommendation */}
            <div className="flex w-full items-start gap-6">
              {/* Key Tradeoffs */}
              {recommendation?.comparative_analysis && (
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4">
                  <div className="flex items-center gap-2">
                    <SubframeCore.Icon
                      className="text-heading-3 font-heading-3 text-warning-600"
                      name="FeatherAlertTriangle"
                    />
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      Key Tradeoffs
                    </span>
                  </div>
                  <div className="flex w-full flex-col items-start gap-3">
                    {recommendation.comparative_analysis
                      .split(/\n+/)
                      .filter((line) => line.trim())
                      .map((line, idx) => (
                        <div key={idx} className="flex w-full items-start gap-2">
                          <SubframeCore.Icon
                            className="text-body font-body text-subtext-color mt-0.5"
                            name="FeatherArrowRight"
                          />
                          <span className="text-body font-body text-default-font">
                            {line.trim().replace(/^[-•*]\s*/, "")}
                          </span>
                        </div>
                      ))}
                  </div>
                </div>
              )}

              {/* Staff Recommendation */}
              {recommendation && (
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4 rounded-md border border-solid border-brand-600 bg-brand-50 px-4 py-4">
                  <div className="flex w-full items-center justify-between">
                    <div className="flex items-center gap-2">
                      <SubframeCore.Icon
                        className="text-heading-3 font-heading-3 text-brand-600"
                        name="FeatherCheckCircle"
                      />
                      <span className="text-heading-3 font-heading-3 text-default-font">
                        Staff Recommendation
                      </span>
                    </div>
                    <Badge variant="brand">
                      COA {recommendation.recommended_coa_index}
                    </Badge>
                  </div>
                  <div className="flex w-full flex-col items-start gap-3">
                    <div className="flex w-full flex-col items-start gap-1">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        PREFERRED COURSE OF ACTION
                      </span>
                      <span className="text-body-bold font-body-bold text-default-font">
                        {recommendation.recommendation_summary}
                      </span>
                    </div>
                    <div className="flex w-full flex-col items-start gap-1">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        KEY REASON
                      </span>
                      <span className="text-body font-body text-default-font">
                        {recommendation.reasoning}
                      </span>
                    </div>
                    <div className="flex w-full flex-col items-start gap-1">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        RISK ACCEPTED
                      </span>
                      <span className="text-body font-body text-default-font">
                        {recommendation.key_risks}
                      </span>
                    </div>
                    <div className="flex w-full items-center gap-2">
                      <span className="text-caption-bold font-caption-bold text-subtext-color">
                        CONFIDENCE:
                      </span>
                      <Badge
                        variant={confidenceBadgeVariant(recommendation.confidence_level) as any}
                      >
                        {recommendation.confidence_level}
                      </Badge>
                      <span className="text-caption font-caption text-subtext-color">
                        {recommendation.confidence_explanation}
                      </span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      {/* Bottom action bar */}
      <div className="flex w-full flex-none items-center justify-end gap-3 border-t border-solid border-neutral-border bg-neutral-50 px-6 py-4">
        <Button
          size="large"
          iconRight="FeatherArrowRight"
          disabled={!hasData || isGenerating}
          onClick={onProceedToApproval}
        >
          Proceed to COA Approval
        </Button>
      </div>
    </div>
  );
}

export default CoaComparison;
