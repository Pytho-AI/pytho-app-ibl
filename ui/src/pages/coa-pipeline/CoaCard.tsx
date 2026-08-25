import React from "react";
import * as SubframeCore from "@subframe/core";
import type { GeneratedCOA } from "./types";
import { evColorClass, riskPillClasses, resourceColorClass } from "./utils";

interface CoaCardProps {
  coa: GeneratedCOA;
  isSelected: boolean;
  onClick: () => void;
}

function CoaCard({ coa, isSelected, onClick }: CoaCardProps) {
  return (
    <div
      className={`flex w-full cursor-pointer flex-col items-start gap-3 rounded-md border border-solid px-4 py-4 transition-colors ${
        isSelected
          ? "border-brand-600 bg-brand-50"
          : "border-neutral-border bg-default-background hover:border-brand-200 hover:bg-brand-50/50"
      }`}
      onClick={onClick}
    >
      <div className="flex w-full items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-body-bold font-body-bold text-default-font">
            COA {coa.coa_number}
          </span>
          <span
            className={`rounded-full px-2 py-0.5 text-caption font-caption ${riskPillClasses(coa.risk.level)}`}
          >
            {coa.risk.level} risk
          </span>
        </div>
        <span
          className={`text-body-bold font-body-bold ${evColorClass(coa.metrics.expected_value)}`}
        >
          EV: {(coa.metrics.expected_value * 100).toFixed(0)}%
        </span>
      </div>
      <span className="text-body font-body text-subtext-color line-clamp-2">
        {coa.summary}
      </span>
      {/* Metrics row */}
      <div className="flex w-full flex-wrap items-center gap-4">
        <div className="flex items-center gap-1">
          <SubframeCore.Icon
            className="text-caption font-caption text-subtext-color"
            name="FeatherBarChart2"
          />
          <span className="text-caption font-caption text-subtext-color">
            CI: [{coa.metrics.confidence_interval.lower.toFixed(2)},{" "}
            {coa.metrics.confidence_interval.upper.toFixed(2)}]
          </span>
        </div>
        <div className="flex items-center gap-1">
          <SubframeCore.Icon
            className="text-caption font-caption text-subtext-color"
            name="FeatherActivity"
          />
          <span className="text-caption font-caption text-subtext-color">
            Uncertainty: {(coa.metrics.uncertainty * 100).toFixed(0)}%
          </span>
        </div>
        {coa.metrics.time_cost != null && (
          <div className="flex items-center gap-1">
            <SubframeCore.Icon
              className="text-caption font-caption text-subtext-color"
              name="FeatherClock"
            />
            <span className="text-caption font-caption text-subtext-color">
              {coa.metrics.time_cost.toFixed(1)}h
            </span>
          </div>
        )}
        {coa.metrics.resources_used != null && (
          <div className="flex items-center gap-1">
            <SubframeCore.Icon
              className={`text-caption font-caption ${resourceColorClass(coa.metrics.resources_used)}`}
              name="FeatherPackage"
            />
            <span className="text-caption font-caption text-subtext-color">
              Resources: {(coa.metrics.resources_used * 100).toFixed(0)}%
            </span>
          </div>
        )}
        <div className="flex items-center gap-1">
          <SubframeCore.Icon
            className="text-caption font-caption text-subtext-color"
            name="FeatherEye"
          />
          <span className="text-caption font-caption text-subtext-color">
            {coa.metrics.visits} sims
          </span>
        </div>
      </div>
    </div>
  );
}

export default React.memo(CoaCard);
