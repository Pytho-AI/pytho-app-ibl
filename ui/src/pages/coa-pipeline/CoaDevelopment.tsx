"use client";

import { useState } from "react";
import { Button } from "../../subframe/components/Button";
import { IconButton } from "../../subframe/components/IconButton";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import * as SubframeCore from "@subframe/core";
import type { GenerationState } from "./types";
import CoaCard from "./CoaCard";
import CoaDetailPanel from "./CoaDetailPanel";

interface CoaDevelopmentProps {
  generation: GenerationState;
  onCancelGeneration: () => void;
  onProceedToWargaming: () => void;
}

function CoaDevelopment({ generation, onCancelGeneration, onProceedToWargaming }: CoaDevelopmentProps) {
  const [selectedCoa, setSelectedCoa] = useState<number | null>(null);

  const {
    isGenerating,
    progress,
    status,
    error,
    etaSeconds,
    coas,
    scenario,
    treeVisualizationHtml,
  } = generation;

  const selectedCoaData = coas.find((c) => c.coa_number === selectedCoa);

  return (
    <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6 overflow-auto">
      {/* Header */}
      <div className="flex w-full items-center justify-between">
        <div className="flex items-center gap-3">
          <IconWithBackground
            variant="brand"
            size="large"
            icon="FeatherGitBranch"
          />
          <div className="flex flex-col items-start">
            <span className="text-heading-2 font-heading-2 text-default-font">
              COA Development
            </span>
            <span className="text-body font-body text-subtext-color">
              {isGenerating
                ? "AI agents are generating courses of action..."
                : coas.length > 0
                ? `${coas.length} course${coas.length !== 1 ? "s" : ""} of action generated`
                : "Waiting for generation to begin"}
            </span>
          </div>
        </div>
        {isGenerating && (
          <Button
            variant="neutral-secondary"
            size="small"
            icon="FeatherX"
            onClick={onCancelGeneration}
          >
            Cancel Generation
          </Button>
        )}
      </div>

      {/* Progress section */}
      {isGenerating && (
        <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-brand-200 bg-brand-50 px-6 py-4">
          <div className="flex w-full items-center gap-3">
            <SubframeCore.Loader className="h-5 w-5 text-brand-600" />
            <span className="text-body-bold font-body-bold text-default-font">
              {status}
            </span>
          </div>
          <div className="flex w-full items-center gap-2">
            <div className="h-2 grow rounded-full bg-neutral-200 overflow-hidden">
              <div
                className="h-full bg-brand-600 transition-all duration-300"
                style={{ width: `${progress}%` }}
              />
            </div>
            <span className="text-caption font-caption text-subtext-color tabular-nums whitespace-nowrap">
              {progress.toFixed(0)}%
              {etaSeconds != null && etaSeconds > 0 && (
                <> &middot; {etaSeconds >= 60 ? `${Math.floor(etaSeconds / 60)}m ${Math.round(etaSeconds % 60)}s` : `${Math.round(etaSeconds)}s`} remaining</>
              )}
            </span>
          </div>
        </div>
      )}

      {/* Error banner */}
      {error && (
        <div className="flex w-full items-center gap-3 rounded-md border border-solid border-error-400 bg-error-50 px-4 py-3">
          <SubframeCore.Icon
            className="text-body font-body text-error-600"
            name="FeatherAlertCircle"
          />
          <span className="text-body font-body text-error-800">{error}</span>
        </div>
      )}

      {/* Scenario Info with Players */}
      {scenario && (
        <div className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4">
          <div className="flex w-full flex-col items-start gap-1">
            <div className="flex w-full items-center gap-2">
              <IconWithBackground icon="FeatherMap" variant="brand" />
              <span className="text-body-bold font-body-bold text-default-font">
                Scenario: {scenario.name}
              </span>
            </div>
            {scenario.description && (
              <span className="text-caption font-caption text-subtext-color text-justify">
                {scenario.description}
              </span>
            )}
          </div>

          {/* Players Section */}
          {scenario.players && Object.keys(scenario.players).length > 0 && (
            <div className="flex w-full flex-col items-start gap-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Players ({Object.keys(scenario.players).length})
              </span>
              <div className="flex w-full flex-col items-start gap-2">
                {Object.entries(scenario.players).map(([key, player]: [string, any]) => {
                  const playerColor = player.color || "rgba(128, 128, 128, 0.8)";
                  const isStartingPlayer = Number(key) === scenario.starting_player;
                  const displayLabel = player.display_label || player.category || "Unknown";

                  return (
                    <div
                      key={key}
                      className="flex w-full flex-col items-start gap-1 rounded-md px-3 py-2 border bg-default-background border-neutral-border"
                    >
                      <div className="flex w-full items-center gap-2">
                        <span className="text-caption-bold font-caption-bold text-default-font">
                          {player.name}
                        </span>
                        {isStartingPlayer && (
                          <span className="text-caption font-caption px-2 py-0.5 rounded-full bg-neutral-100 text-subtext-color">
                            Primary
                          </span>
                        )}
                        <span
                          className="text-caption font-caption ml-auto font-medium"
                          style={{ color: playerColor.replace("0.8)", "1)") }}
                        >
                          {displayLabel}
                        </span>
                      </div>
                      {player.objective && (
                        <span className="text-caption font-caption text-subtext-color">
                          Objective: {player.objective}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}

      {/* Tree visualization - iframe with download and info tooltip */}
      {treeVisualizationHtml && (
        <div className="flex w-full flex-col items-start gap-2">
          <div className="flex w-full items-center justify-between">
            <div className="flex items-center gap-2">
              <IconWithBackground icon="FeatherGitBranch" variant="brand" />
              <span className="text-body-bold font-body-bold text-default-font">
                Search Tree Visualization
              </span>
              {/* Info Button with Tooltip */}
              <div className="relative group">
                <div className="flex items-center justify-center w-5 h-5 rounded-full bg-neutral-200 hover:bg-neutral-300 cursor-help transition-colors">
                  <SubframeCore.Icon
                    className="text-subtext-color"
                    name="FeatherInfo"
                    style={{ fontSize: "12px" }}
                  />
                </div>
                <div className="absolute left-0 top-7 z-50 w-80 p-4 rounded-lg bg-default-background border border-solid border-neutral-border shadow-xl opacity-0 invisible group-hover:opacity-100 group-hover:visible transition-all duration-200">
                  <div className="flex flex-col gap-3">
                    <div>
                      <span className="text-caption-bold font-caption-bold text-default-font">Understanding the Tree</span>
                      <p className="text-caption text-subtext-color mt-1">
                        This tree shows all possible action sequences explored by the AI simulation.
                      </p>
                    </div>
                    <div className="flex flex-col gap-2">
                      <div className="flex items-start gap-2">
                        <span className="text-blue-500">●</span>
                        <span className="text-caption text-subtext-color"><b className="text-default-font">Nodes</b> = States after an action. Larger nodes were explored more.</span>
                      </div>
                      <div className="flex items-start gap-2">
                        <span className="text-neutral-500">◆</span>
                        <span className="text-caption text-subtext-color"><b className="text-default-font">Edges</b> = Actions taken. Hover on diamonds to see the action.</span>
                      </div>
                      <div className="flex items-start gap-2">
                        <span className="text-green-500">↑</span>
                        <span className="text-caption text-subtext-color"><b className="text-default-font">Values</b> = Expected success probability (0-1) from your perspective.</span>
                      </div>
                    </div>
                  </div>
                  <div className="absolute -top-1 left-4 w-2 h-2 bg-default-background border-l border-t border-solid border-neutral-border rotate-45" />
                </div>
              </div>
              {/* Download Button */}
              <IconButton
                size="small"
                icon="FeatherDownload"
                onClick={() => {
                  if (treeVisualizationHtml) {
                    const blob = new Blob([treeVisualizationHtml], { type: "text/html" });
                    const url = URL.createObjectURL(blob);
                    const a = document.createElement("a");
                    a.href = url;
                    a.download = "simulation-tree.html";
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                  }
                }}
              />
            </div>
            <span className="text-caption font-caption text-subtext-color">
              Updated every 10 simulations
            </span>
          </div>
          <div className="w-full rounded-md border border-solid border-neutral-border overflow-hidden bg-default-background">
            <iframe
              srcDoc={treeVisualizationHtml}
              className="w-full h-[400px] border-0"
              title="Search Tree Visualization"
            />
          </div>
        </div>
      )}

      {/* Generated COAs list */}
      {coas.length > 0 && (
        <div className="flex w-full items-start gap-6">
          <div className="flex grow shrink-0 basis-0 flex-col items-start gap-3">
            <span className="text-heading-3 font-heading-3 text-default-font">
              Generated Courses of Action
            </span>
            {coas.map((coa) => (
              <CoaCard
                key={coa.coa_number}
                coa={coa}
                isSelected={selectedCoa === coa.coa_number}
                onClick={() => setSelectedCoa(coa.coa_number)}
              />
            ))}
          </div>

          {/* COA detail panel */}
          <div className="flex w-96 flex-none flex-col items-start gap-4">
            {selectedCoaData ? (
              <CoaDetailPanel coa={selectedCoaData} />
            ) : (
              <div className="flex w-full flex-col items-center justify-center gap-3 rounded-md border border-dashed border-neutral-300 bg-neutral-50 px-4 py-12">
                <SubframeCore.Icon
                  className="text-heading-2 font-heading-2 text-neutral-400"
                  name="FeatherMousePointer"
                />
                <span className="text-body font-body text-subtext-color text-center">
                  Select a COA from the list to view details
                </span>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Empty state when not generating and no COAs */}
      {!isGenerating && !error && coas.length === 0 && !scenario && (
        <div className="flex w-full grow items-center justify-center">
          <div className="flex flex-col items-center gap-3">
            <SubframeCore.Icon
              className="text-heading-1 font-heading-1 text-neutral-300"
              name="FeatherGitBranch"
            />
            <span className="text-body font-body text-subtext-color">
              No courses of action yet. Go back to Step 1 to configure and start generation.
            </span>
          </div>
        </div>
      )}

      {/* Proceed to Wargaming */}
      <div className="flex w-full items-center justify-end pt-2">
        <Button
          size="large"
          iconRight="FeatherArrowRight"
          disabled={isGenerating || coas.length === 0}
          onClick={onProceedToWargaming}
        >
          Proceed to Wargaming
        </Button>
      </div>
    </div>
  );
}

export default CoaDevelopment;
