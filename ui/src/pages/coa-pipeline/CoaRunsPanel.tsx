"use client";

import React, { useState, useEffect } from "react";
import { Button } from "../../subframe/components/Button";
import { Badge } from "../../subframe/components/Badge";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import { IconButton } from "../../subframe/components/IconButton";
import * as SubframeCore from "@subframe/core";
import * as coaApi from "./coaApi";
import type { RunSummary, RunDetail } from "./coaApi";

// ---------------------------------------------------------------------------
// Phase display config
// ---------------------------------------------------------------------------

const PHASE_CONFIG: Record<
  string,
  { label: string; variant: string; icon: string }
> = {
  initializing: { label: "Saved Draft", variant: "neutral", icon: "FeatherFileText" },
  phase1: { label: "Development", variant: "warning", icon: "FeatherGitBranch" },
  phase2: { label: "Wargaming", variant: "warning", icon: "FeatherSword" },
  comparison_complete: { label: "Awaiting Approval", variant: "brand", icon: "FeatherBarChart2" },
  approved: { label: "Approved", variant: "success", icon: "FeatherCheckCircle" },
};

function formatDate(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface CoaRunsPanelProps {
  onClose: () => void;
  onLoadRun: (detail: RunDetail) => void;
  userId?: string | null;
}

// ---------------------------------------------------------------------------
// Run Card
// ---------------------------------------------------------------------------

function RunCard({
  run,
  onLoad,
  onDelete,
}: {
  run: RunSummary;
  onLoad: (runId: string) => void;
  onDelete: (runId: string) => void;
}) {
  const phase = PHASE_CONFIG[run.current_phase] || PHASE_CONFIG.initializing;

  return (
    <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
      {/* Header row */}
      <div className="flex w-full items-center justify-between">
        <div className="flex items-center gap-2">
          <SubframeCore.Icon
            className="text-body font-body text-subtext-color"
            name={phase.icon as any}
          />
          <span className="text-caption font-caption text-subtext-color">
            {formatDate(run.created_at)}
          </span>
        </div>
        <Badge variant={phase.variant as any}>{phase.label}</Badge>
      </div>

      {/* Objective */}
      <span className="text-body-bold font-body-bold text-default-font line-clamp-2">
        {run.objective || "No objective"}
      </span>

      {/* Meta + Actions */}
      <div className="flex w-full items-center justify-between border-t border-solid border-neutral-border pt-3">
        <div className="flex items-center gap-3">
          {run.num_coas > 0 && (
            <span className="text-caption font-caption text-subtext-color">
              {run.num_coas} COA{run.num_coas !== 1 ? "s" : ""}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <IconButton
            size="small"
            icon="FeatherTrash2"
            onClick={(e: React.MouseEvent) => {
              e.stopPropagation();
              if (window.confirm(`Delete run from ${formatDate(run.created_at)}?`)) {
                onDelete(run.run_id);
              }
            }}
          />
          <Button
            variant="brand-secondary"
            size="small"
            icon="FeatherPlay"
            onClick={() => onLoad(run.run_id)}
          >
            Load
          </Button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main Panel
// ---------------------------------------------------------------------------

function CoaRunsPanel({
  onClose,
  onLoadRun,
  userId,
}: CoaRunsPanelProps) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Fetch runs on mount — always filter by user_id when available
  useEffect(() => {
    if (!userId) {
      setRuns([]);
      setIsLoading(false);
      return;
    }

    let cancelled = false;
    setIsLoading(true);
    setError(null);

    coaApi
      .listRuns(userId)
      .then((data) => {
        if (!cancelled) setRuns(data.runs);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [userId]);

  const handleLoad = async (runId: string) => {
    try {
      const detail = await coaApi.getRun(runId, true);
      onLoadRun(detail);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDelete = async (runId: string) => {
    try {
      await coaApi.deleteRun(runId);
      setRuns((prev) => prev.filter((r) => r.run_id !== runId));
    } catch (err: any) {
      setError(err.message);
    }
  };

  return (
    <div className="flex h-full w-96 shrink-0 flex-col items-start border-l border-solid border-neutral-border bg-default-background">
      {/* Header */}
      <div className="flex w-full items-center justify-between border-b border-solid border-neutral-border px-4 py-3">
        <div className="flex items-center gap-2">
          <IconWithBackground variant="brand" size="medium" icon="FeatherArchive" />
          <span className="text-heading-3 font-heading-3 text-default-font">
            Saved Runs
          </span>
        </div>
        <IconButton size="small" icon="FeatherX" onClick={onClose} />
      </div>

      {/* Content */}
      <div className="flex w-full grow flex-col items-start gap-3 overflow-y-auto px-4 py-4">
        {isLoading && (
          <div className="flex w-full grow items-center justify-center py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Loader size="medium" />
              <span className="text-body font-body text-subtext-color">
                Loading saved runs...
              </span>
            </div>
          </div>
        )}

        {error && (
          <div className="flex w-full items-center gap-2 rounded-md border border-solid border-error-200 bg-error-50 px-3 py-2">
            <SubframeCore.Icon
              className="text-body font-body text-error-600"
              name="FeatherAlertCircle"
            />
            <span className="text-caption font-caption text-error-700">
              {error}
            </span>
          </div>
        )}

        {!isLoading && !error && runs.length === 0 && (
          <div className="flex w-full grow items-center justify-center py-12">
            <div className="flex flex-col items-center gap-3">
              <SubframeCore.Icon
                className="text-heading-1 font-heading-1 text-neutral-300"
                name="FeatherArchive"
              />
              <span className="text-body font-body text-subtext-color">
                No saved runs yet
              </span>
              <span className="text-caption font-caption text-subtext-color text-center">
                Runs are saved automatically as you progress through the lifecycle
              </span>
            </div>
          </div>
        )}

        {!isLoading &&
          runs.map((run) => (
            <RunCard
              key={run.run_id}
              run={run}
              onLoad={handleLoad}
              onDelete={handleDelete}
            />
          ))}
      </div>
    </div>
  );
}

export default CoaRunsPanel;
