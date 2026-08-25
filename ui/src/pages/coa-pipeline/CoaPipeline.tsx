"use client";

import React, { useState, useCallback } from "react";
import { Button } from "../../subframe/components/Button";
import { Stepper } from "../../subframe/components/Stepper";
import { DefaultPageLayout } from "../../subframe/layouts/DefaultPageLayout";
import { useAppContext } from "../../lib/contextLib";
import CoaPreparation from "./CoaPreparation";
import CoaDevelopment from "./CoaDevelopment";
import CoaWargame from "./CoaWargame";
import CoaComparison from "./CoaComparison";
import CoaApproval from "./CoaApproval";
import CoaRunsPanel from "./CoaRunsPanel";
import { useCoaWebSocket } from "./useCoaWebSocket";
import type { GenerationConfig } from "./types";
import type { RunDetail, ApprovalData } from "./coaApi";

function CoaPipeline() {
  const { user } = useAppContext();
  const [activeStep, setActiveStep] = useState(1);
  const [showRunsPanel, setShowRunsPanel] = useState(false);
  const [initialValues, setInitialValues] = useState<RunDetail["context"] | undefined>(undefined);
  const [savedApproval, setSavedApproval] = useState<ApprovalData | null>(null);

  const {
    generation,
    setGeneration,
    startGeneration,
    cancelGeneration,
    proceedToWargaming,
    proceedToComparison,
  } = useCoaWebSocket();

  const handleStartGeneration = useCallback(
    (config: GenerationConfig) => {
      setInitialValues(undefined);
      setSavedApproval(null);
      startGeneration({ ...config, userId: user?.userId || null });
      setActiveStep(2);
    },
    [startGeneration, user?.userId]
  );

  const handleProceedToWargaming = useCallback(() => {
    proceedToWargaming();
    setActiveStep(3);
  }, [proceedToWargaming]);

  const handleProceedToComparison = useCallback(() => {
    proceedToComparison();
    setActiveStep(4);
  }, [proceedToComparison]);

  // Load a saved run (view-only) — populate GenerationState and jump to step
  const handleLoadRun = useCallback(
    (detail: RunDetail) => {
      const phase = detail.current_phase;

      // Map backend data → GenerationState
      const coas = detail.results?.courses_of_action
        || detail.phase1?.all_coas
        || [];
      const scenario = detail.results?.scenario
        || detail.phase1?.scenario
        || null;
      const treeVisualizationHtml = detail.results?.tree_visualization_html
        || detail.tree_visualization_html
        || null;
      const comparison = detail.results?.comparison || null;
      const recommendation = detail.results?.recommendation || null;
      const criteriaComparison = detail.results?.criteria_comparison || null;
      const decisionMatrix = detail.results?.decision_matrix || null;
      const wargameAnalyses =
        detail.results?.wargame_analyses
        || detail.phase2?.wargame_analyses
        || [];

      setGeneration({
        isGenerating: false,
        progress: 100,
        status: "Loaded from saved run",
        error: "",
        runId: detail.run_id,
        copMapId: detail.context?.cop_map_id || null,
        coas,
        scenario,
        treeVisualizationHtml,
        comparison,
        recommendation,
        criteriaComparison,
        decisionMatrix,
        wargameAnalyses,
      });

      // Populate saved approval if present
      setSavedApproval(detail.approval || null);

      // Jump to appropriate step
      if (phase === "initializing") {
        setInitialValues(detail.context);
        setActiveStep(1);
      } else if (phase === "phase1") {
        setActiveStep(2);
      } else if (phase === "phase2") {
        setActiveStep(3);
      } else if (phase === "comparison_complete") {
        setActiveStep(4);
      } else if (phase === "approved") {
        setActiveStep(5);
      } else {
        setActiveStep(2);
      }

      setShowRunsPanel(false);
    },
    [setGeneration]
  );

  function getStepVariant(step: number): "default" | "completed" | "active" {
    if (step === activeStep) return "active";
    if (step < activeStep) return "completed";
    return "default";
  }

  function renderStep() {
    switch (activeStep) {
      case 1:
        return (
          <CoaPreparation
            onStartGeneration={handleStartGeneration}
            initialValues={initialValues}
            userId={user?.userId}
          />
        );
      case 2:
        return (
          <CoaDevelopment
            generation={generation}
            onCancelGeneration={cancelGeneration}
            onProceedToWargaming={handleProceedToWargaming}
          />
        );
      case 3:
        return (
          <CoaWargame
            coas={generation.coas}
            wargameAnalyses={generation.wargameAnalyses}
            isGenerating={generation.isGenerating}
            progress={generation.progress}
            status={generation.status}
            error={generation.error}
            etaSeconds={generation.etaSeconds}
            onProceedToComparison={handleProceedToComparison}
          />
        );
      case 4:
        return (
          <CoaComparison
            coas={generation.coas}
            criteriaComparison={generation.criteriaComparison}
            decisionMatrix={generation.decisionMatrix}
            recommendation={generation.recommendation}
            isGenerating={generation.isGenerating}
            status={generation.status}
            onProceedToApproval={() => setActiveStep(5)}
          />
        );
      case 5:
        return (
          <CoaApproval
            coas={generation.coas}
            recommendation={generation.recommendation}
            criteriaComparison={generation.criteriaComparison}
            runId={generation.runId}
            savedApproval={savedApproval}
          />
        );
      default:
        return null;
    }
  }

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full items-stretch bg-default-background">
        {/* Main content */}
        <div className="flex min-w-0 grow flex-col items-start overflow-auto">
          <div className="flex w-full flex-col items-start gap-4 border-b border-solid border-neutral-border bg-neutral-50 px-6 py-4">
            <div className="flex w-full items-center justify-between">
              <span className="text-heading-2 font-heading-2 text-default-font">
                COA Lifecycle
              </span>
              <Button
                variant="neutral-secondary"
                size="small"
                icon="FeatherArchive"
                onClick={() => setShowRunsPanel((prev) => !prev)}
              >
                Saved Runs
              </Button>
            </div>
            <Stepper>
              <Stepper.Step
                variant={getStepVariant(1)}
                firstStep={true}
                stepNumber="1"
                label="COA Preparation"
                onClick={() => setActiveStep(1)}
              />
              <Stepper.Step
                variant={getStepVariant(2)}
                stepNumber="2"
                label="COA Development"
                onClick={() => setActiveStep(2)}
              />
              <Stepper.Step
                variant={getStepVariant(3)}
                stepNumber="3"
                label="COA Wargame"
                onClick={() => setActiveStep(3)}
              />
              <Stepper.Step
                variant={getStepVariant(4)}
                stepNumber="4"
                label="COA Comparison"
                onClick={() => setActiveStep(4)}
              />
              <Stepper.Step
                variant={getStepVariant(5)}
                lastStep={true}
                stepNumber="5"
                label="COA Approval"
                onClick={() => setActiveStep(5)}
              />
            </Stepper>
          </div>

          {renderStep()}
        </div>

        {/* Saved Runs side panel */}
        {showRunsPanel && (
          <CoaRunsPanel
            onClose={() => setShowRunsPanel(false)}
            onLoadRun={handleLoadRun}
            userId={user?.userId}
          />
        )}
      </div>
    </DefaultPageLayout>
  );
}

export default CoaPipeline;
