import { useRef, useEffect, useCallback, useState } from "react";
import type { GenerationConfig, GenerationState } from "./types";

const WS_URL = import.meta.env.VITE_COA_WS_URL || "ws://localhost:8765";

const PLACEHOLDER_WORLD_STATE = `SITUATION: A mechanized infantry battalion task force is conducting offensive operations in a semi-urban environment. Enemy forces (reinforced company strength) have established defensive positions in the town of OBJ IRON, controlling key terrain and lines of communication. Friendly forces have completed movement to attack positions. Weather is clear, terrain is mixed urban/rural with limited avenues of approach. Civilian population remains in the area. Rules of engagement require minimizing collateral damage.`;

const MAX_RECONNECT_ATTEMPTS = 3;
const BASE_RECONNECT_DELAY_MS = 1000;

type ConnectionStatus = "disconnected" | "connecting" | "connected" | "reconnecting";

const INITIAL_STATE: GenerationState = {
  isGenerating: false,
  progress: 0,
  status: "",
  error: "",
  etaSeconds: null,
  runId: null,
  copMapId: null,
  coas: [],
  scenario: null,
  treeVisualizationHtml: null,
  comparison: null,
  recommendation: null,
  criteriaComparison: null,
  decisionMatrix: null,
  wargameAnalyses: [],
};

export interface UseCoaWebSocketReturn {
  generation: GenerationState;
  setGeneration: React.Dispatch<React.SetStateAction<GenerationState>>;
  connectionStatus: ConnectionStatus;
  startGeneration: (config: GenerationConfig) => void;
  cancelGeneration: () => void;
  proceedToWargaming: () => void;
  proceedToComparison: () => void;
  resumeRun: (runId: string, resumeFrom: string, autoProceed?: string[]) => void;
}

export function useCoaWebSocket(): UseCoaWebSocketReturn {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pendingConfigRef = useRef<GenerationConfig | null>(null);

  const [generation, setGeneration] = useState<GenerationState>(INITIAL_STATE);
  const [connectionStatus, setConnectionStatus] = useState<ConnectionStatus>("disconnected");

  const generationRef = useRef(generation);
  generationRef.current = generation;

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (reconnectTimerRef.current) clearTimeout(reconnectTimerRef.current);
      if (wsRef.current) wsRef.current.close();
    };
  }, []);

  const handleMessage = useCallback((event: MessageEvent) => {
    const data = JSON.parse(event.data);
    switch (data.type) {
      case "status":
        setGeneration((prev) => ({
          ...prev,
          status: data.message || data.phase,
          scenario: data.scenario_info || prev.scenario,
          runId: data.run_id || prev.runId,
        }));
        if (data.phase === "complete") {
          setGeneration((prev) => ({ ...prev, isGenerating: false }));
        }
        break;
      case "progress": {
        const pct = (data.completed / data.total) * 100;
        setGeneration((prev) => ({
          ...prev,
          progress: pct,
          status: `Simulating... ${data.completed}/${data.total}`,
          etaSeconds: data.eta_seconds ?? null,
          treeVisualizationHtml:
            data.tree_visualization_html || prev.treeVisualizationHtml,
        }));
        break;
      }
      case "coas_extracted": {
        const extractedCoas = data.courses_of_action || [];
        // Backend sends paused=true when waiting at Gate 1, false when auto-proceeding
        const paused = data.paused !== false;
        setGeneration((prev) => ({
          ...prev,
          isGenerating: !paused,
          status: paused
            ? `${extractedCoas.length} course${extractedCoas.length !== 1 ? "s" : ""} of action extracted — review and proceed to wargaming`
            : "Running wargaming analysis...",
          coas: extractedCoas,
          scenario: data.scenario || prev.scenario,
          treeVisualizationHtml:
            data.tree_visualization_html || prev.treeVisualizationHtml,
        }));
        break;
      }
      case "wargame_progress": {
        const wargamePct = data.pct != null
          ? data.pct
          : (data.completed / data.total) * 100;
        setGeneration((prev) => ({
          ...prev,
          progress: wargamePct,
          status: data.step || "Running wargame analysis...",
          etaSeconds: data.eta_seconds ?? null,
        }));
        break;
      }
      case "wargame_complete": {
        // Backend sends paused=true when waiting at Gate 2, false when auto-proceeding
        const paused = data.paused !== false;
        setGeneration((prev) => ({
          ...prev,
          isGenerating: !paused,
          runId: data.run_id || prev.runId,
          progress: 100,
          status: paused
            ? "Wargaming complete — review results and proceed to comparison"
            : "Running COA comparison...",
          wargameAnalyses: data.wargame_analyses || [],
        }));
        break;
      }
      case "result": {
        const coas = data.courses_of_action || [];
        setGeneration((prev) => ({
          ...prev,
          isGenerating: false,
          status: "Comparison complete!",
          coas: coas.length > 0 ? coas : prev.coas,
          scenario: data.scenario || prev.scenario,
          treeVisualizationHtml:
            data.tree_visualization_html || prev.treeVisualizationHtml,
          comparison: data.comparison || null,
          recommendation: data.recommendation || null,
          criteriaComparison: data.criteria_comparison || null,
          decisionMatrix: data.decision_matrix || null,
          wargameAnalyses: data.wargame_analyses || prev.wargameAnalyses,
        }));
        break;
      }
      case "error":
        setGeneration((prev) => ({
          ...prev,
          isGenerating: false,
          error: data.message || "AI generation failed",
        }));
        break;
    }
  }, []);

  const connect = useCallback(
    (config: GenerationConfig) => {
      // Close any existing connection
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }

      setConnectionStatus("connecting");

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionStatus("connected");
        reconnectAttemptsRef.current = 0;
        setGeneration((prev) => ({ ...prev, status: "Analyzing situation..." }));

        ws.send(
          JSON.stringify({
            type: "run",
            world_state: config.worldState || PLACEHOLDER_WORLD_STATE,
            objective: config.objective,
            cop_map_id: config.copMapId || null,
            user_id: config.userId || null,
            parameters: {
              num_simulations: config.numSimulations || 5,
              num_coas: config.numCoas,
              creation_mode: config.creationMode,
              use_doctrinal_branching: config.useDoctrinalBranching ?? true,
              divergence: config.divergence,
            },
          })
        );
      };

      ws.onmessage = handleMessage;

      ws.onerror = () => {
        // Only set error if we haven't successfully connected yet
        if (connectionStatus === "connecting") {
          setGeneration((prev) => ({
            ...prev,
            isGenerating: false,
            error:
              "Failed to connect to AI service. Make sure the server is running.",
          }));
          setConnectionStatus("disconnected");
        }
      };

      ws.onclose = (event) => {
        wsRef.current = null;
        const wasConnected = connectionStatus === "connected" || connectionStatus === "reconnecting";

        // Attempt reconnection if the close was unexpected and we were mid-generation
        if (
          wasConnected &&
          !event.wasClean &&
          reconnectAttemptsRef.current < MAX_RECONNECT_ATTEMPTS
        ) {
          setConnectionStatus("reconnecting");
          const delay =
            BASE_RECONNECT_DELAY_MS *
            Math.pow(2, reconnectAttemptsRef.current);
          reconnectAttemptsRef.current += 1;

          setGeneration((prev) => ({
            ...prev,
            status: `Connection lost — reconnecting (attempt ${reconnectAttemptsRef.current}/${MAX_RECONNECT_ATTEMPTS})...`,
          }));

          reconnectTimerRef.current = setTimeout(() => {
            if (pendingConfigRef.current) {
              connect(pendingConfigRef.current);
            }
          }, delay);
        } else {
          setConnectionStatus("disconnected");
        }
      };
    },
    [handleMessage, connectionStatus]
  );

  const startGeneration = useCallback(
    (config: GenerationConfig) => {
      pendingConfigRef.current = config;
      reconnectAttemptsRef.current = 0;

      setGeneration({
        ...INITIAL_STATE,
        isGenerating: true,
        etaSeconds: 600,
        copMapId: config.copMapId || null,
        status: "Connecting to AI...",
      });

      try {
        connect(config);
      } catch {
        setGeneration((prev) => ({
          ...prev,
          isGenerating: false,
          error: "Failed to connect to AI service",
        }));
      }
    },
    [connect]
  );

  const cancelGeneration = useCallback(() => {
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({ type: "cancel" }));
      wsRef.current.close();
    }
    setGeneration((prev) => ({
      ...prev,
      isGenerating: false,
      status: "",
    }));
    setConnectionStatus("disconnected");
  }, []);

  const sendIfOpen = useCallback((msg: object) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify(msg));
    }
  }, []);

  const resumeRun = useCallback(
    (runId: string, resumeFrom: string, autoProceed: string[] = []) => {
      // Close any existing connection
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }

      reconnectAttemptsRef.current = 0;
      setGeneration((prev) => ({
        ...prev,
        isGenerating: true,
        error: "",
        progress: 0,
        etaSeconds: 600,
        runId,
        status: `Resuming run from ${resumeFrom}...`,
      }));
      setConnectionStatus("connecting");

      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnectionStatus("connected");
        ws.send(
          JSON.stringify({
            type: "resume",
            run_id: runId,
            resume_from: resumeFrom,
            auto_proceed: autoProceed,
          })
        );
      };

      ws.onmessage = handleMessage;

      ws.onerror = () => {
        setGeneration((prev) => ({
          ...prev,
          isGenerating: false,
          error: "Failed to connect to AI service for resume.",
        }));
        setConnectionStatus("disconnected");
      };

      ws.onclose = () => {
        wsRef.current = null;
        setConnectionStatus("disconnected");
      };
    },
    [handleMessage]
  );

  const proceedToWargaming = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      sendIfOpen({ type: "proceed_wargame" });
      setGeneration((prev) => ({
        ...prev,
        isGenerating: true,
        progress: 0,
        etaSeconds: 600,
        status: "Starting wargaming analysis...",
      }));
    } else if (generationRef.current.runId) {
      // No active WebSocket (loaded from saved run) — resume with auto-proceed
      resumeRun(generationRef.current.runId, "phase1", ["proceed_wargame"]);
    } else {
      setGeneration((prev) => ({
        ...prev,
        error: "No active connection or saved run to resume from.",
      }));
    }
  }, [sendIfOpen, resumeRun]);

  const proceedToComparison = useCallback(() => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      sendIfOpen({ type: "proceed_comparison" });
      setGeneration((prev) => ({
        ...prev,
        isGenerating: true,
        status: "Starting COA comparison analysis...",
      }));
    } else if (generationRef.current.runId) {
      // No active WebSocket (loaded from saved run) — resume with auto-proceed
      resumeRun(generationRef.current.runId, "phase2", ["proceed_comparison"]);
    } else {
      setGeneration((prev) => ({
        ...prev,
        error: "No active connection or saved run to resume from.",
      }));
    }
  }, [sendIfOpen, resumeRun]);

  return {
    generation,
    setGeneration,
    connectionStatus,
    startGeneration,
    cancelGeneration,
    proceedToWargaming,
    proceedToComparison,
    resumeRun,
  };
}
