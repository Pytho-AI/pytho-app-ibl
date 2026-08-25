import React from "react";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import * as SubframeCore from "@subframe/core";
import type { GeneratedCOA } from "./types";
import { evColorClass, riskPillClasses, resourceColorClass } from "./utils";

interface CoaDetailPanelProps {
  coa: GeneratedCOA;
}

function CoaDetailPanel({ coa }: CoaDetailPanelProps) {
  return (
    <>
      {/* Summary */}
      <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
        <span className="text-heading-3 font-heading-3 text-default-font">
          COA {coa.coa_number} Details
        </span>
        <span className="text-body font-body text-default-font">
          {coa.summary}
        </span>
        {/* Outcome status badge */}
        {coa.outcome.status && (
          <span
            className={`rounded-full px-2 py-0.5 text-caption font-caption ${
              coa.outcome.status === "success"
                ? "bg-success-100 text-success-700"
                : coa.outcome.status === "draw"
                ? "bg-warning-100 text-warning-700"
                : coa.outcome.status === "failure"
                ? "bg-error-100 text-error-700"
                : "bg-neutral-100 text-subtext-color"
            }`}
          >
            {coa.outcome.status}
            {coa.outcome.winner ? ` — ${coa.outcome.winner}` : ""}
          </span>
        )}
      </div>

      {/* Risk Assessment */}
      <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
        <div className="flex w-full items-center gap-2">
          <IconWithBackground icon="FeatherAlertTriangle" variant="warning" />
          <span className="text-body-bold font-body-bold text-default-font">
            Risk Assessment
          </span>
          <span
            className={`ml-auto rounded-full px-2 py-0.5 text-caption font-caption ${riskPillClasses(coa.risk.level)}`}
          >
            {coa.risk.level}
          </span>
        </div>
        <span className="text-body font-body text-subtext-color">
          {coa.risk.description}
        </span>
        {coa.risk.confidence_width != null && (
          <span className="text-caption font-caption text-subtext-color">
            Confidence width: {coa.risk.confidence_width.toFixed(3)}
          </span>
        )}
      </div>

      {/* Expected Outcome */}
      <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
        <div className="flex w-full items-center gap-2">
          <IconWithBackground icon="FeatherCheckCircle" variant="success" />
          <span className="text-body-bold font-body-bold text-default-font">
            Expected Outcome
          </span>
        </div>
        <span className="text-body font-body text-subtext-color">
          {coa.outcome.description}
        </span>
      </div>

      {/* Action Sequence */}
      {coa.actions.length > 0 && (
        <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
          <div className="flex w-full items-center gap-2">
            <IconWithBackground icon="FeatherList" variant="brand" />
            <span className="text-body-bold font-body-bold text-default-font">
              Action Sequence
            </span>
          </div>
          <div className="flex w-full flex-col items-start gap-2">
            {coa.actions.map((action, idx) => (
              <div
                key={idx}
                className="flex w-full items-start gap-3 border-b border-solid border-neutral-100 pb-2 last:border-b-0"
              >
                <span className="text-caption-bold font-caption-bold text-brand-600 mt-0.5">
                  T{action.turn}
                </span>
                <div className="flex grow flex-col items-start">
                  <span className="text-caption-bold font-caption-bold text-default-font">
                    {action.actor}: {action.action}
                  </span>
                  <span className="text-caption font-caption text-subtext-color">
                    {action.result}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Per-player Time & Resource Breakdown */}
      {(coa.time || coa.resources) && (
        <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
          <div className="flex w-full items-center gap-2">
            <IconWithBackground icon="FeatherUsers" variant="brand" />
            <span className="text-body-bold font-body-bold text-default-font">
              Per-Player Breakdown
            </span>
          </div>
          <div className="flex w-full flex-col items-start gap-2">
            {Object.entries(coa.time || coa.resources || {})
              .filter(([key]) => key !== "total_turns")
              .map(([playerName]) => {
                const timeData = coa.time?.[playerName] as { hours: number } | undefined;
                const resData = coa.resources?.[playerName];
                return (
                  <div
                    key={playerName}
                    className="flex w-full items-center justify-between border-b border-solid border-neutral-100 pb-2 last:border-b-0"
                  >
                    <span className="text-caption-bold font-caption-bold text-default-font">
                      {playerName}
                    </span>
                    <div className="flex items-center gap-3">
                      {timeData && (
                        <span className="text-caption font-caption text-subtext-color">
                          {timeData.hours.toFixed(1)}h
                        </span>
                      )}
                      {resData && (
                        <span
                          className={`text-caption font-caption ${resourceColorClass(resData.used)}`}
                        >
                          {(resData.used * 100).toFixed(0)}% resources
                        </span>
                      )}
                    </div>
                  </div>
                );
              })}
          </div>
        </div>
      )}

      {/* Metrics */}
      <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4">
        <span className="text-caption-bold font-caption-bold text-subtext-color">
          METRICS
        </span>
        <div className="flex w-full items-center justify-between">
          <span className="text-caption font-caption text-subtext-color">
            Expected Value
          </span>
          <span
            className={`text-caption-bold font-caption-bold ${evColorClass(coa.metrics.expected_value)}`}
          >
            {(coa.metrics.expected_value * 100).toFixed(1)}%
          </span>
        </div>
        <div className="flex w-full items-center justify-between">
          <span className="text-caption font-caption text-subtext-color">
            Confidence Interval
          </span>
          <span className="text-caption-bold font-caption-bold text-default-font">
            [{coa.metrics.confidence_interval.lower.toFixed(3)},{" "}
            {coa.metrics.confidence_interval.upper.toFixed(3)}]
          </span>
        </div>
        <div className="flex w-full items-center justify-between">
          <span className="text-caption font-caption text-subtext-color">
            Uncertainty
          </span>
          <span className="text-caption-bold font-caption-bold text-default-font">
            {(coa.metrics.uncertainty * 100).toFixed(1)}%
          </span>
        </div>
        {coa.metrics.time_cost != null && (
          <div className="flex w-full items-center justify-between">
            <span className="text-caption font-caption text-subtext-color">
              Time Cost
            </span>
            <span className="text-caption-bold font-caption-bold text-default-font">
              {coa.metrics.time_cost.toFixed(1)}h
            </span>
          </div>
        )}
        {coa.metrics.resources_used != null && (
          <div className="flex w-full items-center justify-between">
            <span className="text-caption font-caption text-subtext-color">
              Resources Used
            </span>
            <span
              className={`text-caption-bold font-caption-bold ${resourceColorClass(coa.metrics.resources_used)}`}
            >
              {(coa.metrics.resources_used * 100).toFixed(0)}%
            </span>
          </div>
        )}
        {coa.metrics.resources_remaining != null && (
          <div className="flex w-full items-center justify-between">
            <span className="text-caption font-caption text-subtext-color">
              Resources Remaining
            </span>
            <span className="text-caption-bold font-caption-bold text-default-font">
              {(coa.metrics.resources_remaining * 100).toFixed(0)}%
            </span>
          </div>
        )}
        <div className="flex w-full items-center justify-between">
          <span className="text-caption font-caption text-subtext-color">
            Simulations
          </span>
          <span className="text-caption-bold font-caption-bold text-default-font">
            {coa.metrics.visits}
          </span>
        </div>
      </div>

      {/* Feasibility (if available) */}
      {coa.feasibility && (
        <div
          className={`flex w-full flex-col items-start gap-2 rounded-md border border-solid px-4 py-3 ${
            coa.feasibility.feasible
              ? "border-success-300 bg-success-50"
              : "border-error-300 bg-error-50"
          }`}
        >
          <div className="flex items-center gap-2">
            <SubframeCore.Icon
              className={`text-body font-body ${
                coa.feasibility.feasible ? "text-success-600" : "text-error-600"
              }`}
              name={coa.feasibility.feasible ? "FeatherCheckCircle" : "FeatherXCircle"}
            />
            <span
              className={`text-caption-bold font-caption-bold ${
                coa.feasibility.feasible ? "text-success-700" : "text-error-700"
              }`}
            >
              {coa.feasibility.feasible ? "Doctrinally Feasible" : "Feasibility Issues"}
            </span>
          </div>
          {coa.feasibility.issues.length > 0 && (
            <ul className="list-disc pl-5">
              {coa.feasibility.issues.map((issue, idx) => (
                <li key={idx} className="text-caption font-caption text-error-700">
                  {issue}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </>
  );
}

export default React.memo(CoaDetailPanel);
