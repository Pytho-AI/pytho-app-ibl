"use client";

import React, { useState, useEffect, useCallback } from "react";
import { Button } from "../../subframe/components/Button";
import { CheckboxCard } from "../../subframe/components/CheckboxCard";
import { IconWithBackground } from "../../subframe/components/IconWithBackground";
import { TextArea } from "../../subframe/components/TextArea";
import { TextField } from "../../subframe/components/TextField";
import { ToggleGroup } from "../../subframe/components/ToggleGroup";
import * as SubframeCore from "@subframe/core";
import type { GenerationConfig } from "./types";
import * as coaApi from "./coaApi";

// Task 6: the COP map surface (GeoAgentProvider + tactical overlays)
// moved into the geo-agent container under /geo-app/operations-map.
// The COP map slot below embeds it as an iframe.

const GEO_APP_BASE = (import.meta as any).env?.VITE_GEO_APP_BASE || "/geo-app";

interface CopSummary {
  entityCount: number;
  overlayCount: number;
  routeCount: number;
  summary: string;
}

/**
 * COP map iframe embed. bbox is passed as a URL param so the child
 * GeoAgentProvider mounts against the same fit-bounds state without
 * needing to postMessage a fitBounds command back through the iframe
 * boundary.
 */
function CopMapIframe({ mapId, bbox, userId }:
                        { mapId: string; bbox: number[] | null; userId?: string | null }) {
  const q = new URLSearchParams();
  q.set("map_id", mapId);
  if (bbox && bbox.length === 4) q.set("bbox", bbox.join(","));
  if (userId) q.set("user_id", userId);
  const src = `${GEO_APP_BASE}/operations-map?${q.toString()}`;
  return (
    <iframe
      src={src}
      title="COP Map"
      style={{ width: "100%", height: "100%", border: 0,
               display: "block", background: "#0b1120" }}
      allow="clipboard-read; clipboard-write; fullscreen"
    />
  );
}

interface CoaPreparationProps {
  onStartGeneration: (config: GenerationConfig) => void;
  initialValues?: {
    world_state: string;
    objective: string;
    parameters: Record<string, any>;
    [key: string]: any;
  };
  userId?: string | null;
}

function CoaPreparation({ onStartGeneration, initialValues, userId }: CoaPreparationProps) {
  const creationMode = "agent";
  const [useDoctrinalBranching, setUseDoctrinalBranching] = useState<boolean>(true);
  const [worldState, setWorldState] = useState(
    `A mechanized infantry battalion task force is conducting offensive operations in a semi-urban environment. Enemy forces (reinforced company strength) have established defensive positions in the town of OBJ IRON, controlling key terrain and lines of communication. Friendly forces have completed movement to attack positions. Weather is clear, terrain is mixed urban/rural with limited avenues of approach. Civilian population remains in the area. Rules of engagement require minimizing collateral damage.`
  );
  const [missionGoals, setMissionGoals] = useState(
    "Seize OBJ IRON to destroy enemy defensive positions and secure key terrain and lines of communication"
  );
  const [missionTime, setMissionTime] = useState("");
  const [forcesAvailable, setForcesAvailable] = useState("");
  const [roeConstraints, setRoeConstraints] = useState("");
  const [geography, setGeography] = useState("");
  const [divergence, setDivergence] = useState(65);
  const [coaQuantity, setCoaQuantity] = useState("3");
  const [customQuantity, setCustomQuantity] = useState("");
  const [numSimulations, setNumSimulations] = useState(5);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);
  const [savingDraft, setSavingDraft] = useState(false);
  const [draftSaved, setDraftSaved] = useState(false);
  const [draftRunId, setDraftRunId] = useState<string | null>(null);

  // COP state
  const [copMapId, setCopMapId] = useState<string | null>(null);
  const [copBbox, setCopBbox] = useState<number[] | null>(null);
  const [copLoading, setCopLoading] = useState(false);
  const [copError, setCopError] = useState<string | null>(null);
  const [copSummary, setCopSummary] = useState<CopSummary | null>(null);

  // Pre-fill form from saved run context (re-launch)
  useEffect(() => {
    if (!initialValues) return;
    if (initialValues.world_state) setWorldState(initialValues.world_state);
    if (initialValues.objective) setMissionGoals(initialValues.objective);
    const params = initialValues.parameters || {};
    if (params.divergence != null) setDivergence(params.divergence);
    if (params.num_simulations != null) setNumSimulations(Math.min(params.num_simulations, 10));
    if (params.num_coas != null) setCoaQuantity(String(Math.min(params.num_coas, 3)));
    if (params.use_doctrinal_branching != null) setUseDoctrinalBranching(params.use_doctrinal_branching);
    if (initialValues.cop_map_id) setCopMapId(initialValues.cop_map_id);
  }, [initialValues]);

  const getNumCoas = (): number => {
    if (coaQuantity === "custom") return parseInt(customQuantity) || 3;
    return parseInt(coaQuantity) || 3;
  };

  const handleLockConfig = () => {
    const errors: string[] = [];
    if (!worldState.trim()) errors.push("World State");
    if (!missionGoals.trim()) errors.push("Mission Goals");
    if (coaQuantity === "custom" && !customQuantity.trim()) errors.push("Custom COA Quantity");
    setValidationErrors(errors);
    if (errors.length > 0) return;

    const objectiveParts = [`Mission Goals: ${missionGoals}`];
    if (missionTime.trim()) objectiveParts.push(`Mission Time: ${missionTime}`);
    if (forcesAvailable.trim()) objectiveParts.push(`Forces Available: ${forcesAvailable}`);
    if (roeConstraints.trim()) objectiveParts.push(`ROE Constraints: ${roeConstraints}`);
    if (geography.trim()) objectiveParts.push(`Geography: ${geography}`);

    onStartGeneration({
      worldState,
      objective: objectiveParts.join("\n"),
      numCoas: getNumCoas(),
      creationMode,
      useDoctrinalBranching,
      divergence,
      numSimulations,
      copMapId,
    });
  };

  const handleSaveDraft = useCallback(async () => {
    if (savingDraft) return;
    setSavingDraft(true);
    try {
      const objectiveParts = [`Mission Goals: ${missionGoals}`];
      if (missionTime.trim()) objectiveParts.push(`Mission Time: ${missionTime}`);
      if (forcesAvailable.trim()) objectiveParts.push(`Forces Available: ${forcesAvailable}`);
      if (roeConstraints.trim()) objectiveParts.push(`ROE Constraints: ${roeConstraints}`);
      if (geography.trim()) objectiveParts.push(`Geography: ${geography}`);

      const res = await coaApi.saveDraft({
        world_state: worldState,
        objective: objectiveParts.join("\n"),
        parameters: {
          num_coas: getNumCoas(),
          divergence,
          num_simulations: numSimulations,
          creation_mode: creationMode,
          use_doctrinal_branching: useDoctrinalBranching,
        },
        cop_map_id: copMapId,
        user_id: userId,
        run_id: draftRunId,
      });
      setDraftRunId(res.run_id);
      setDraftSaved(true);
      setTimeout(() => setDraftSaved(false), 2000);
    } catch {
      // silent — draft save is best-effort
    } finally {
      setSavingDraft(false);
    }
  }, [savingDraft, worldState, missionGoals, missionTime, forcesAvailable, roeConstraints, geography, divergence, numSimulations, creationMode, useDoctrinalBranching, copMapId, userId, draftRunId]);

  const handleGenerateCOP = useCallback(async () => {
    if (!worldState.trim() || copLoading) return;

    setCopLoading(true);
    setCopError(null);
    setCopMapId(null);
    setCopSummary(null);

    try {
      const api = createApiClient(GEO_AGENT_API_URL);
      const result = await api.generateCOP(worldState.trim());

      setCopMapId(result.map_id);
      setCopBbox(result.bbox);
      setCopSummary({
        entityCount: result.entities?.features?.length || 0,
        overlayCount: result.overlays?.features?.length || 0,
        routeCount: result.routes?.length || 0,
        summary: result.generation_summary || "",
      });
    } catch (err: any) {
      setCopError(err.message || "Failed to generate COP");
    } finally {
      setCopLoading(false);
    }
  }, [worldState, copLoading]);

  return (
    <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6 overflow-auto">
      <div className="flex w-full items-center justify-between">
        <div className="flex items-center gap-3">
          <IconWithBackground
            variant="brand"
            size="large"
            icon="FeatherSettings"
          />
          <div className="flex flex-col items-start">
            <span className="text-heading-2 font-heading-2 text-default-font">
              COA Setup &amp; Control
            </span>
            <span className="text-body font-body text-subtext-color">
              Configure parameters before agent-powered COA development begins
            </span>
          </div>
        </div>
      </div>
      {/* ── World State ── */}
      <div className={`flex w-full flex-col items-start gap-4 rounded-md border border-solid ${validationErrors.includes("World State") ? "border-error-600 bg-error-50" : "border-brand-primary bg-brand-50"} px-6 py-6`}>
        <div className="flex w-full items-center gap-2">
          <SubframeCore.Icon
            className="text-heading-3 font-heading-3 text-brand-600"
            name="FeatherGlobe"
          />
          <span className="text-heading-3 font-heading-3 text-default-font">
            World State
          </span>
        </div>
        <span className="text-body font-body text-default-font">
          Describe the current operational situation, friendly/enemy forces,
          terrain, weather, and any relevant context for COA generation.
        </span>
        <TextArea
          className="h-auto w-full flex-none"
          variant="outline"
          helpText="Provide as much detail as possible — this forms the foundation for all generated COAs"
        >
          <TextArea.Input
            className="h-auto min-h-[160px] w-full flex-none"
            placeholder="Describe the operational situation..."
            value={worldState}
            onChange={(e) => setWorldState(e.target.value)}
          />
        </TextArea>
        <div className="flex w-full items-center justify-end">
          <Button
            variant="brand-secondary"
            icon="FeatherMap"
            loading={copLoading}
            disabled={!worldState.trim() || copLoading}
            onClick={handleGenerateCOP}
          >
            {copLoading
              ? "Generating..."
              : copMapId
                ? "Regenerate COP"
                : "Generate COP"}
          </Button>
        </div>
      </div>

      {/* ── COP Map (appears after generation) ── */}
      {(copLoading || copMapId || copError) && (
        <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
          <div className="flex w-full items-center justify-between">
            <div className="flex items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-brand-600"
                name="FeatherMap"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                Common Operational Picture
              </span>
            </div>
            {copSummary && (
              <div className="flex items-center gap-4">
                <span className="text-caption-bold font-caption-bold text-brand-600">
                  {copSummary.entityCount} units
                </span>
                <span className="text-caption-bold font-caption-bold text-brand-600">
                  {copSummary.overlayCount} overlays
                </span>
                <span className="text-caption-bold font-caption-bold text-brand-600">
                  {copSummary.routeCount} routes
                </span>
              </div>
            )}
          </div>

          {copError && (
            <div className="flex w-full items-center gap-3 rounded-md border border-solid border-error-400 bg-error-50 px-4 py-3">
              <SubframeCore.Icon
                className="text-body font-body text-error-600"
                name="FeatherAlertCircle"
              />
              <span className="text-body font-body text-error-800">
                {copError}
              </span>
            </div>
          )}

          {copLoading && (
            <div className="flex w-full items-center justify-center py-20">
              <div className="flex flex-col items-center gap-3">
                <div className="animate-spin rounded-full h-8 w-8 border-2 border-brand-200 border-t-brand-600" />
                <span className="text-body font-body text-subtext-color">
                  Analyzing narrative and building operational picture...
                </span>
                <span className="text-caption font-caption text-subtext-color">
                  Extracting forces, overlays, and planning routes
                </span>
              </div>
            </div>
          )}

          {copMapId && (
            <>
              {copSummary?.summary && (
                <p className="text-body font-body text-subtext-color border-l-2 border-solid border-brand-600 pl-3">
                  {copSummary.summary}
                </p>
              )}
              <div className="w-full h-[700px] rounded-md overflow-hidden border border-solid border-neutral-border">
                <CopMapIframe mapId={copMapId} bbox={copBbox} userId={userId} />
              </div>
            </>
          )}
        </div>
      )}

      {/* ── Two-column config layout ── */}
      <div className="flex w-full items-start gap-6">
        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6">
          <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <div className="flex w-full items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-brand-primary"
                name="FeatherHash"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                COA Quantity
              </span>
            </div>
            <span className="text-body font-body text-subtext-color">
              Select the number of Courses of Action to develop.
            </span>
            <div className="flex w-full items-center gap-4">
              <ToggleGroup value={coaQuantity} onValueChange={(value: string) => {
                setCoaQuantity(value);
                setCustomQuantity(value);
              }}>
                <ToggleGroup.Item icon={null} value="1">
                  1 COA
                </ToggleGroup.Item>
                <ToggleGroup.Item icon={null} value="2">
                  2 COAs
                </ToggleGroup.Item>
                <ToggleGroup.Item icon={null} value="3">
                  3 COAs
                </ToggleGroup.Item>
              </ToggleGroup>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <div className="flex w-full items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-brand-primary"
                name="FeatherActivity"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                Number of Simulations
              </span>
            </div>
            <span className="text-body font-body text-subtext-color">
              More simulations increase confidence but take longer to run.
            </span>
            <div className="flex w-full flex-col items-start gap-2">
              <div className="flex w-full items-center justify-between">
                <span className="text-caption-bold font-caption-bold text-subtext-color">
                  Fewer (faster)
                </span>
                <span className="text-caption-bold font-caption-bold text-brand-600">
                  {numSimulations}
                </span>
                <span className="text-caption-bold font-caption-bold text-subtext-color">
                  More (higher confidence)
                </span>
              </div>
              <input
                type="range"
                min={1}
                max={10}
                step={1}
                value={numSimulations}
                onChange={(e) => setNumSimulations(Number(e.target.value))}
                className="w-full accent-brand-600"
              />
              <div className="flex w-full items-center justify-between">
                <span className="text-caption font-caption text-subtext-color">
                  1
                </span>
                <span className="text-caption font-caption text-subtext-color">
                  10
                </span>
              </div>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <div className="flex w-full items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-brand-primary"
                name="FeatherUsers"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                Generation Method
              </span>
            </div>
            <span className="w-full text-body font-body text-subtext-color">
              Choose how COAs will be generated. Doctrine uses FM 3-90 constraint tables (fast, deterministic). AI uses LLM free-text generation.
            </span>
            <div className="flex w-full items-stretch gap-4">
              <CheckboxCard
                className="h-auto grow shrink-0 basis-0"
                hideCheckbox={true}
                checked={useDoctrinalBranching}
                onCheckedChange={() => setUseDoctrinalBranching(true)}
              >
                <div className="flex w-full flex-col items-center gap-3 py-2">
                  <IconWithBackground
                    variant="brand"
                    size="large"
                    icon="FeatherBookOpen"
                  />
                  <div className="flex flex-col items-center gap-1">
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      Doctrine
                    </span>
                    <span className="text-caption font-caption text-subtext-color text-center">
                      COAs generated from FM 3-90 doctrinal constraint tables
                    </span>
                  </div>
                </div>
              </CheckboxCard>
              <CheckboxCard
                className="h-auto grow shrink-0 basis-0"
                hideCheckbox={true}
                checked={!useDoctrinalBranching}
                onCheckedChange={() => setUseDoctrinalBranching(false)}
              >
                <div className="flex w-full flex-col items-center gap-3 py-2">
                  <IconWithBackground
                    variant="success"
                    size="large"
                    icon="FeatherCpu"
                  />
                  <div className="flex flex-col items-center gap-1">
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      AI
                    </span>
                    <span className="text-caption font-caption text-subtext-color text-center">
                      COAs generated via LLM free-text generation
                    </span>
                  </div>
                </div>
              </CheckboxCard>
            </div>
          </div>
        </div>
        <div className="flex w-96 flex-none flex-col items-start gap-6">
          <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <div className="flex w-full items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-brand-primary"
                name="FeatherTarget"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                Goals &amp; Constraints
              </span>
            </div>
            <span className="text-body font-body text-subtext-color">
              Define mission goals and operational boundaries that all COAs
              must respect.
            </span>
            <div className="flex w-full flex-col items-start gap-3">
              <div className={`flex w-full flex-col items-start gap-2 rounded-md border border-solid ${validationErrors.includes("Mission Goals") ? "border-error-600 bg-error-50" : "border-neutral-border bg-default-background"} px-4 py-3`}>
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-success-600"
                    name="FeatherTarget"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Mission Goals<span className="text-error-600"> *</span>
                  </span>
                </div>
                <TextField
                  className="h-auto w-full flex-none"
                  variant="filled"
                  label=""
                  helpText=""
                >
                  <TextField.Input
                    placeholder="Defeat enemy forces in AO IRON"
                    value={missionGoals}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setMissionGoals(event.target.value)
                    }
                  />
                </TextField>
              </div>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-3">
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-warning-600"
                    name="FeatherClock"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Mission Time
                  </span>
                </div>
                <TextField
                  className="h-auto w-full flex-none"
                  variant="filled"
                  label=""
                  helpText=""
                >
                  <TextField.Input
                    placeholder="H-Hour NLT 240600Z"
                    value={missionTime}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setMissionTime(event.target.value)
                    }
                  />
                </TextField>
              </div>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-3">
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-success-600"
                    name="FeatherUsers"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Forces Available
                  </span>
                </div>
                <TextField
                  className="h-auto w-full flex-none"
                  variant="filled"
                  label=""
                  helpText=""
                >
                  <TextField.Input
                    placeholder="1x ABCT, 1x CAB, 1x DIVARTY"
                    value={forcesAvailable}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setForcesAvailable(event.target.value)
                    }
                  />
                </TextField>
              </div>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-3">
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-error-600"
                    name="FeatherShield"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    ROE Constraints
                  </span>
                </div>
                <TextField
                  className="h-auto w-full flex-none"
                  variant="filled"
                  label=""
                  helpText=""
                >
                  <TextField.Input
                    placeholder="No fires within 500m of civilian areas"
                    value={roeConstraints}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setRoeConstraints(event.target.value)
                    }
                  />
                </TextField>
              </div>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-3">
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-brand-600"
                    name="FeatherMapPin"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Geography
                  </span>
                </div>
                <TextField
                  className="h-auto w-full flex-none"
                  variant="filled"
                  label=""
                  helpText=""
                >
                  <TextField.Input
                    placeholder="AO IRON: NK 1000-2000 / 5000-6000"
                    value={geography}
                    onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                      setGeography(event.target.value)
                    }
                  />
                </TextField>
              </div>
              <div className="flex w-full flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-3">
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon
                    className="text-body font-body text-brand-600"
                    name="FeatherSliders"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    COA Divergence
                  </span>
                </div>
                <div className="flex w-full flex-col items-start gap-2">
                  <div className="flex w-full items-center justify-between">
                    <span className="text-caption-bold font-caption-bold text-subtext-color">
                      Conservative
                    </span>
                    <span className="text-caption-bold font-caption-bold text-subtext-color">
                      Highly Divergent
                    </span>
                  </div>
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={divergence}
                    onChange={(e) => setDivergence(Number(e.target.value))}
                    className="w-full accent-brand-600"
                  />
                  <div className="flex w-full items-center justify-between">
                    <span className="text-caption font-caption text-subtext-color">
                      Similar approaches, low risk variance
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      Distinct approaches, wide risk spectrum
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
      <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
      <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-warning-400 bg-warning-50 px-6 py-4">
        <div className="flex w-full items-center gap-3">
          <SubframeCore.Icon
            className="text-heading-3 font-heading-3 text-warning-700"
            name="FeatherAlertTriangle"
          />
          <span className="text-body-bold font-body-bold text-warning-800">
            Confirmation Required
          </span>
        </div>
        <span className="text-body font-body text-warning-800">
          Once locked, AI agents will begin generating COAs based on your
          configuration. You maintain full decision authority and can modify
          or reject any agent-generated content. Agents will not advance the
          process without explicit human approval.
        </span>
      </div>
      {validationErrors.length > 0 && (
        <div className="flex w-full items-center gap-3 rounded-md border border-solid border-error-400 bg-error-50 px-4 py-3">
          <SubframeCore.Icon
            className="text-body font-body text-error-600"
            name="FeatherAlertCircle"
          />
          <span className="text-body font-body text-error-800">
            Please fill in the following required fields: {validationErrors.join(", ")}
          </span>
        </div>
      )}
      <div className="flex w-full items-center justify-end">
        <div className="flex items-center gap-3">
          <Button
            variant={draftSaved ? "brand-secondary" : "neutral-secondary"}
            icon={draftSaved ? "FeatherCheck" : "FeatherSave"}
            loading={savingDraft}
            disabled={savingDraft}
            onClick={handleSaveDraft}
          >
            {savingDraft ? "Saving..." : draftSaved ? "Saved!" : "Save Draft"}
          </Button>
          <Button
            size="large"
            iconRight="FeatherArrowRight"
            onClick={handleLockConfig}
          >
            Lock Configuration &amp; Start COA Development
          </Button>
        </div>
      </div>
    </div>
  );
}

export default CoaPreparation;
