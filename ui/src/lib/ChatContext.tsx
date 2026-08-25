/**
 * ChatContext — chrome-level state for the Intel Agent chat sidebar.
 *
 * Why this lives at the App root, not inside DocEditor:
 *
 *   The Generate-button flow opens a /ws/intel_agent WebSocket and waits
 *   100-150s while OE runs. If the WS lives INSIDE DocEditor (the old wiring),
 *   any navigation away from /missioncommand/docs/:sessionId tears the WS
 *   down — the analyst can't see OE responses while flipping over to
 *   OperationsMap to drop a phase line, and a page refresh during generation
 *   restarts the whole turn from scratch.
 *
 *   Lifting the panel + its WS up to App-level means the socket survives any
 *   route change. The context exposes setters DocEditorPage (and any future
 *   surface) uses to seed the FIRST WS frame (initialMessage +
 *   manualProductType) and to register an `onArtifact` callback for the
 *   page-specific reaction (DocEditor swaps placeholder → rendered HTML).
 *
 * Shape:
 *   openChat({ sessionId, projectId, userId, initialMessage?, manualProductType?, onArtifact? })
 *     — set the chat session, open the panel, fire the synthesized first
 *       message if initialMessage is provided (guarded so it never fires twice
 *       per sessionId, even across remounts).
 *   closeChat()         — collapse the panel; WS stays connected.
 *   resetChat()         — clear all session state (used on logout).
 *   isOpen              — controls expanded vs. collapsed tab.
 *   sessionId/projectId/userId — current chat session binding.
 *   initialMessage      — synthesized first-message prompt; consumed once.
 *   manualProductType   — when set, FIRST WS frame carries
 *                          `manual_product_type` so OE runs the manual-dispatch
 *                          prompt (operations_expert_manual.md).
 *   sentInitialFor      — set of sessionIds whose initialMessage has been
 *                          consumed. Used by ChatAgent to decide
 *                          whether to forward initialMessage to ChatAgent
 *                          on this mount or pass undefined.
 *   onArtifact          — page-supplied callback fired when OE emits an
 *                          artifact; DocEditorPage uses this to flip its
 *                          placeholder.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export interface ChatArtifactEvent {
  artifact_id: string;
  artifact_type: string;
  htmlLink?: string | null;
  docxLink?: string | null;
  pptxLink?: string | null;
  // XLSX-canonical artifacts (15 doctrinally-XLSX products per
  // EXCEL_DOCTRINAL_ALIGNMENT.md). When present, DocEditorPage mounts
  // <SheetEditor> instead of <DocEditor>.
  xlsxLink?: string | null;
  gaps?: string[];
}

/**
 * One row in the per-stage latency timeline. Mirrors the backend
 * `stage_event` WS frame produced by handlers/intel_agent.py (the agent
 * working on that file is adding this — see Ask 2 of the frontend brief).
 *
 *   { type:"stage_event", session_id, stage, started_at, completed_at,
 *     duration_ms, extra:{...} }
 *
 * The UI keeps these in the order they arrive. Stages don't repeat within a
 * single turn — the timeline is append-only per session.
 */
export interface StageEvent {
  stage: string;
  started_at?: string;
  completed_at?: string;
  duration_ms: number;
  extra?: Record<string, any>;
  /** Locally recorded wall-clock for "first event arrived" / TTFA math when
   *  the backend doesn't echo started_at for early stages. */
  client_received_at: number;
}

/** End-of-turn rollup emitted alongside the final artifact. */
export interface TurnSummary {
  total_duration_ms: number;
  per_stage_breakdown?: Array<{ stage: string; duration_ms: number }>;
  artifact_id?: string;
}

/**
 * Per-session generation state. ChatContext tracks one of these per
 * sessionId for as long as the page is open. The timeline pill in
 * DocEditorPage / the dev "Generate all" launcher both read from this map.
 */
export interface SessionGenerationState {
  sessionId: string;
  /** Human label for the pill: pulled from `product_name` query param OR
   *  the `manualProductType` OR the first artifact's `artifact_type`. */
  productLabel: string;
  /** Wall-clock at the moment the analyst kicked off the generation (or
   *  reconnected to it). Used as TTFA's baseline if no started_at is in
   *  the first stage_event. */
  startedAt: number;
  /** When the FIRST artifact byte became visible (artifact_emitted OR the
   *  first stage_event with stage === "render_html"). */
  firstArtifactAt: number | null;
  stageEvents: StageEvent[];
  artifact: ChatArtifactEvent | null;
  turnSummary: TurnSummary | null;
  /** "running" until either turnSummary arrives or an error is observed. */
  status: "running" | "ready" | "error";
}

export interface OpenChatOptions {
  sessionId: string;
  projectId?: string;
  userId?: string;
  /** Synthesized first message ("Generate WEM now…"). Consumed once per
   *  sessionId — re-opening with the same sessionId will NOT refire it. */
  initialMessage?: string;
  /** Set on the FIRST WS frame to route OE through operations_expert_manual. */
  manualProductType?: string;
  /** Page-specific reaction when OE emits an artifact. The context tracks
   *  ONE callback at a time; the most recent `openChat` wins. */
  onArtifact?: (artifact: ChatArtifactEvent) => void;
  /** Pop the visible chat panel open. Default false: starting a session
   *  (e.g. the doc-editor Generate flow) wires up the WS without auto-opening
   *  the IRC panel — generation runs via the always-mounted hidden inner, so
   *  the panel stays collapsed until the analyst opens it. Only the
   *  user-initiated open controls pass `expand: true`. */
  expand?: boolean;
}

interface ChatContextValue {
  isOpen: boolean;
  openChat: (opts: OpenChatOptions) => void;
  closeChat: () => void;
  resetChat: () => void;
  /** The artifact id the user is currently editing — feeds the
   *  per-doc IRC channel (#doc-<artifact_id>). DocEditorPage pushes
   *  this on every loadArtifact; cleared when the user leaves the
   *  editor. Empty string ⇒ chat panel renders a "no doc open"
   *  placeholder instead of dialing the IRC server. */
  currentArtifactId: string;
  setCurrentArtifactId: (artifactId: string) => void;
  /** Replace just the onArtifact handler without re-opening / clearing
   *  initial message state. Used by DocEditorPage when it remounts under the
   *  same sessionId (e.g. after a refresh that loaded an artifact from the
   *  REST endpoint) — the chrome panel keeps streaming WS replies into the
   *  page's `handleArtifact` rather than the previous page's stale closure. */
  setOnArtifact: (
    handler: ((artifact: ChatArtifactEvent) => void) | null
  ) => void;
  sessionId: string;
  projectId: string;
  userId: string;
  initialMessage: string;
  manualProductType: string;
  onArtifact: ((artifact: ChatArtifactEvent) => void) | null;
  /** True after the first WS frame for this sessionId has been sent. The
   *  panel uses this to avoid double-sending after a re-render. */
  initialMessageConsumed: boolean;
  /** Marks initialMessage consumed for the current sessionId so we don't
   *  refire it on remount/refresh of the panel inside the same session. */
  markInitialMessageConsumed: () => void;

  // ---- Multiplexed per-session generation timelines -----------------
  /** Map<sessionId, SessionGenerationState>. Surfaced as a plain object so
   *  React's referential-equality check picks up changes. */
  sessions: Record<string, SessionGenerationState>;
  /** Register a brand-new generation session for the timeline pill UI.
   *  Idempotent — calling twice for the same sessionId is a no-op (the
   *  existing state is preserved). */
  beginSession: (opts: {
    sessionId: string;
    productLabel: string;
    startedAt?: number;
  }) => void;
  /** Append a stage_event to a session's timeline. */
  appendStageEvent: (sessionId: string, ev: StageEvent) => void;
  /** Attach an artifact to a session (also marks firstArtifactAt). */
  attachArtifact: (sessionId: string, artifact: ChatArtifactEvent) => void;
  /** Finalize a session with a turn_summary frame. */
  finalizeSession: (sessionId: string, summary: TurnSummary) => void;
  /** Mark a session as errored (used when the WS reports an error frame). */
  markSessionError: (sessionId: string) => void;
  /** Drop a session from the map (used by the dev launcher when clearing
   *  the previous batch). */
  clearSession: (sessionId: string) => void;
}

const ChatContext = createContext<ChatContextValue | null>(null);

export function useChatContext(): ChatContextValue {
  const ctx = useContext(ChatContext);
  if (!ctx) {
    throw new Error(
      "useChatContext must be used inside <ChatProvider>. " +
        "ChatProvider is mounted at App.tsx root level."
    );
  }
  return ctx;
}

export function ChatProvider({ children }: { children: ReactNode }) {
  const [isOpen, setIsOpen] = useState<boolean>(false);
  const [sessionId, setSessionId] = useState<string>("");
  const [projectId, setProjectId] = useState<string>("");
  const [userId, setUserId] = useState<string>("");
  const [initialMessage, setInitialMessage] = useState<string>("");
  const [manualProductType, setManualProductType] = useState<string>("");
  const [onArtifact, setOnArtifactState] = useState<
    ((artifact: ChatArtifactEvent) => void) | null
  >(null);
  // Sessions whose `initialMessage` has already been forwarded to
  // ChatAgent. Used to make the Generate-button bootstrap idempotent
  // across page refreshes — if the analyst hits F5 mid-generation, the page
  // remounts, calls openChat() with the same sessionId+initialMessage, but
  // we will NOT fire the synthesized prompt again.
  const consumedRef = useRef<Set<string>>(new Set());
  const [initialMessageConsumed, setInitialMessageConsumed] =
    useState<boolean>(false);
  const [currentArtifactId, setCurrentArtifactId] = useState<string>("");

  const openChat = useCallback((opts: OpenChatOptions) => {
    // Only pop the panel open when a user-initiated control asks for it
    // (expand: true). Auto-callers (e.g. the doc-editor Generate flow) wire up
    // the session without forcing the IRC panel open — it stays collapsed.
    if (opts.expand) setIsOpen(true);
    setSessionId(opts.sessionId);
    setProjectId(opts.projectId || "");
    setUserId(opts.userId || "");
    setOnArtifactState(() => opts.onArtifact || null);
    // Manual product type lives ONLY across the first frame for this
    // session; subsequent chat messages don't carry it (the OE state on
    // the server already has it cached). Clear it once the FIRST frame is
    // sent (via markInitialMessageConsumed below).
    setManualProductType(opts.manualProductType || "");
    const alreadyConsumed = consumedRef.current.has(opts.sessionId);
    setInitialMessageConsumed(alreadyConsumed);
    setInitialMessage(
      alreadyConsumed ? "" : opts.initialMessage || ""
    );
  }, []);

  const closeChat = useCallback(() => {
    setIsOpen(false);
  }, []);

  const resetChat = useCallback(() => {
    setIsOpen(false);
    setSessionId("");
    setProjectId("");
    setUserId("");
    setInitialMessage("");
    setManualProductType("");
    setOnArtifactState(null);
    consumedRef.current = new Set();
    setInitialMessageConsumed(false);
  }, []);

  const setOnArtifact = useCallback(
    (handler: ((artifact: ChatArtifactEvent) => void) | null) => {
      setOnArtifactState(() => handler);
    },
    []
  );

  const markInitialMessageConsumed = useCallback(() => {
    if (sessionId) consumedRef.current.add(sessionId);
    setInitialMessageConsumed(true);
    // Clear the manual-product-type cache so a subsequent typed message in
    // the same chat doesn't re-route as a manual dispatch.
    setManualProductType("");
    setInitialMessage("");
  }, [sessionId]);

  // ---- Multiplexed timeline state ----------------------------------
  const [sessions, setSessions] = useState<
    Record<string, SessionGenerationState>
  >({});

  const beginSession = useCallback(
    (opts: { sessionId: string; productLabel: string; startedAt?: number }) => {
      setSessions((prev) => {
        if (prev[opts.sessionId]) return prev; // idempotent
        return {
          ...prev,
          [opts.sessionId]: {
            sessionId: opts.sessionId,
            productLabel: opts.productLabel || opts.sessionId,
            startedAt: opts.startedAt ?? Date.now(),
            firstArtifactAt: null,
            stageEvents: [],
            artifact: null,
            turnSummary: null,
            status: "running",
          },
        };
      });
    },
    []
  );

  const appendStageEvent = useCallback(
    (sessionId: string, ev: StageEvent) => {
      setSessions((prev) => {
        const cur = prev[sessionId];
        if (!cur) {
          // Stage event arriving for an unknown session — auto-register so
          // the timeline shows up. This handles the case where a parallel
          // launcher fires N sessions back-to-back and the first stage_event
          // beats beginSession() (in practice the order is fine, but
          // defending against re-ordering keeps the UI honest).
          return {
            ...prev,
            [sessionId]: {
              sessionId,
              productLabel: sessionId,
              startedAt: ev.client_received_at - (ev.duration_ms || 0),
              firstArtifactAt:
                ev.stage === "render_html" ? ev.client_received_at : null,
              stageEvents: [ev],
              artifact: null,
              turnSummary: null,
              status: "running",
            },
          };
        }
        const firstArtifactAt =
          cur.firstArtifactAt ??
          (ev.stage === "render_html" ? ev.client_received_at : null);
        return {
          ...prev,
          [sessionId]: {
            ...cur,
            stageEvents: [...cur.stageEvents, ev],
            firstArtifactAt,
          },
        };
      });
    },
    []
  );

  const attachArtifact = useCallback(
    (sessionId: string, artifact: ChatArtifactEvent) => {
      setSessions((prev) => {
        const cur = prev[sessionId];
        const now = Date.now();
        if (!cur) {
          return {
            ...prev,
            [sessionId]: {
              sessionId,
              productLabel: artifact.artifact_type || sessionId,
              startedAt: now,
              firstArtifactAt: now,
              stageEvents: [],
              artifact,
              turnSummary: null,
              status: "ready",
            },
          };
        }
        return {
          ...prev,
          [sessionId]: {
            ...cur,
            artifact,
            firstArtifactAt: cur.firstArtifactAt ?? now,
            // Flip to "ready" on artifact arrival. The original intent
            // was to hold "running" until turn_summary lands — analyst
            // sees the final total_duration_ms before the pill collapses
            // — but the prior implementation was a tautology
            // (`cur.status === "running" ? cur.status : cur.status`) that
            // never flipped the status, so cache-hit loads (which never
            // get a turn_summary because no DAG ran) showed "running"
            // forever. finalizeSession() still overwrites status when
            // turn_summary arrives on the live-generation path, so the
            // analyst still sees the final duration there.
            status: "ready",
          },
        };
      });
    },
    []
  );

  const finalizeSession = useCallback(
    (sessionId: string, summary: TurnSummary) => {
      setSessions((prev) => {
        const cur = prev[sessionId];
        if (!cur) {
          return {
            ...prev,
            [sessionId]: {
              sessionId,
              productLabel: sessionId,
              startedAt: Date.now() - summary.total_duration_ms,
              firstArtifactAt: Date.now(),
              stageEvents: [],
              artifact: null,
              turnSummary: summary,
              status: "ready",
            },
          };
        }
        return {
          ...prev,
          [sessionId]: { ...cur, turnSummary: summary, status: "ready" },
        };
      });
    },
    []
  );

  const markSessionError = useCallback((sessionId: string) => {
    setSessions((prev) => {
      const cur = prev[sessionId];
      if (!cur) return prev;
      return { ...prev, [sessionId]: { ...cur, status: "error" } };
    });
  }, []);

  const clearSession = useCallback((sessionId: string) => {
    setSessions((prev) => {
      if (!prev[sessionId]) return prev;
      const next = { ...prev };
      delete next[sessionId];
      return next;
    });
  }, []);

  const value = useMemo<ChatContextValue>(
    () => ({
      isOpen,
      openChat,
      closeChat,
      resetChat,
      currentArtifactId,
      setCurrentArtifactId,
      setOnArtifact,
      sessionId,
      projectId,
      userId,
      initialMessage,
      manualProductType,
      onArtifact,
      initialMessageConsumed,
      markInitialMessageConsumed,
      sessions,
      beginSession,
      appendStageEvent,
      attachArtifact,
      finalizeSession,
      markSessionError,
      clearSession,
    }),
    [
      isOpen,
      openChat,
      closeChat,
      resetChat,
      currentArtifactId,
      setOnArtifact,
      sessionId,
      projectId,
      userId,
      initialMessage,
      manualProductType,
      onArtifact,
      initialMessageConsumed,
      markInitialMessageConsumed,
      sessions,
      beginSession,
      appendStageEvent,
      attachArtifact,
      finalizeSession,
      markSessionError,
      clearSession,
    ]
  );

  return <ChatContext.Provider value={value}>{children}</ChatContext.Provider>;
}
