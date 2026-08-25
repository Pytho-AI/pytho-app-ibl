/**
 * ChatAgent — chrome-level floating intel-agent chat sidebar.
 *
 * Mounted ONCE at the App root (App.tsx) inside <ChatProvider> +
 * <IntelAgentWsProvider>. The WS is now a SINGLETON owned by
 * IntelAgentWsProvider (see lib/IntelAgentWs.tsx); this component
 * subscribes to it on a per-sessionId basis and shows the visible chat.
 *
 * Drives off `useChatContext()`:
 *   - DocEditorPage on mount checks /api/sessions/<id>/artifacts; if no
 *     artifact yet AND pending_product_type is present, calls
 *     openChat({sessionId, ..., initialMessage, manualProductType, onArtifact}).
 *   - Same-session re-opens (closeChat → openChat, navigation back to
 *     DocEditor) keep the panel mounted and the WebSocket alive. Context's
 *     consumedRef blocks the synthesized "Generate {TYPE}" prompt from
 *     firing twice during a refresh.
 *
 * Resilience (new):
 *   - WS lives at chrome level, survives every route change.
 *   - Reconnect + heartbeat + resume-on-reconnect are handled inside
 *     IntelAgentWsProvider — this component just renders the "reconnecting"
 *     banner when the provider reports status !== "open".
 *
 * Idle state (no sessionId): render the tab anyway IF a connection issue
 * is in progress (so the banner is reachable). Otherwise render nothing.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import { useLocation } from "react-router-dom";
import { useChatContext } from "../lib/ChatContext";
import { useAppContext } from "../lib/contextLib";
import IrcChatPanel from "./IrcChatPanel";
import { useIntelAgentWs, type WsStatus } from "../lib/IntelAgentWs";
import { clientWarn } from "../lib/clientLog";
import { logAnalystEvent } from "../lib/analystEvent";
import * as SubframeCore from "@subframe/core";

const COLLAPSED_TAB_WIDTH = 40;
// 320 (not 400) so a landscape doc (1056px body + 192px page padding +
// AppRail) still fits on a 1440-class laptop screen without the body
// shrinking to fit and looking visually portrait.
const OPEN_PANEL_WIDTH = 320;

// ---------------------------------------------------------------------------
// IDX26 design tokens (from scratch/docs/ibl/IDX26 Flow Ideas.html). Hardcoded
// onto the chat sidebar so the agent panel matches the IDX26 flow mock
// regardless of which app-wide theme is active. The dark palette is
// intentional — the sidebar is a chrome element distinct from the routed
// page body.
// ---------------------------------------------------------------------------
const IDX26 = {
  bg:      "#070a14",
  bg2:     "#0c1120",
  bg3:     "#131a2c",
  bg4:     "#1b2438",
  line:    "#1e2940",
  line2:   "#2a364f",
  text:    "#e8ecf5",
  text2:   "#aab3c8",
  text3:   "#6c7791",
  accent:  "#4f8cff",
  accent2: "#2563eb",
  green:   "#34d399",
  yellow:  "#fbbf24",
  red:     "#f87171",
  pink:    "#ff5c93",
  mono:    'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
  userGrad: "linear-gradient(180deg,#2a4cb6 0%,#1f3a90 100%)",
};

// Route-name table — pathname → human-readable label shown in the chat
// header's scope chip. The chat agent uses this so the analyst always sees
// what page the sidebar is currently scoped to. Matched longest-prefix-first
// so dynamic segments (events/:id/dashboard) resolve correctly.
const PAGE_LABELS: Array<[RegExp, string]> = [
  [/^\/missioncommand\/operations-map\/render/,  "Map Render"],
  [/^\/missioncommand\/operations-map/,          "Operations Map"],
  [/^\/missioncommand\/docs\/[^/]+/,             "Doc Editor"],
  [/^\/missioncommand\/coa-pipeline/,            "COA Pipeline"],
  [/^\/missioncommand\/early-bird-summary/,      "Early Bird"],
  [/^\/missioncommand\/doctrines/,               "Doctrines"],
  [/^\/missioncommand\/data\/static/,            "Static Data"],
  [/^\/missioncommand\/data\/stream/,            "Stream Data"],
  [/^\/missioncommand\/events\/aar\/[^/]+/,      "AAR"],
  [/^\/missioncommand\/events\/aar\/create/,     "Create AAR"],
  [/^\/missioncommand\/events\/[^/]+\/aar/,      "Lessons Learned"],
  [/^\/missioncommand\/events\/[^/]+\/dashboard/, "Event Dashboard"],
  [/^\/missioncommand\/events\/dashboard/,       "Event Dashboard"],
  [/^\/missioncommand\/events\/(create|update)/, "Configure Event"],
  [/^\/missioncommand\/notifications/,           "Notifications"],
  [/^\/missioncommand\/session/,                 "Chat"],
  [/^\/missioncommand\/agents/,                  "Agents"],
  [/^\/missioncommand\/whitecell/,               "White Cell"],
  [/^\/missioncommand\/?$/,                      "Dashboard"],
  [/^\/$/,                                        "Dashboard"],
];

function labelForPath(pathname: string): string {
  for (const [re, label] of PAGE_LABELS) {
    if (re.test(pathname)) return label;
  }
  // Fallback: last non-empty segment, prettified.
  const seg = pathname.split("/").filter(Boolean).pop() || "";
  if (!seg) return "Pytho";
  return seg.charAt(0).toUpperCase() + seg.slice(1).replace(/-/g, " ");
}

type MessageRole = "user" | "assistant" | "system" | "error" | "artifact" | "operations_expert_event";

interface ArtifactCard {
  artifact_id: string;
  artifact_type: string;
  htmlLink?: string | null;
  docxLink?: string | null;
  pptxLink?: string | null;
  // XLSX-canonical artifacts (15 doctrinally-XLSX products per
  // EXCEL_DOCTRINAL_ALIGNMENT.md). When present and no DOCX/PPTX
  // companion, DocEditorPage mounts <SheetEditor> for Collabora
  // live edit instead of <DocEditor>.
  xlsxLink?: string | null;
  gaps?: string[];
}

/** Incremental events streamed from the OE / IA / PW loops AND the
 *  parallel-DAG runner. See `handlers/intel_agent.py::run_operations_expert_loop`,
 *  `_push_event` in `intel_agent.main`, and `runtime/dag_executor.run_dag`.
 *  The terminal reply (with `answer` + `intel_agent.artifacts` for the OE
 *  path, OR an `artifact_complete` frame for the DAG path) lands at end of
 *  turn — these events are the progress log that precedes it. */
type OeEventType =
  | "data_integration_start"
  | "operations_expert_thinking"
  | "operations_expert_tool_call"
  | "operations_expert_tool_result"
  | "intel_agent_dispatch_start"
  | "intel_agent_tool_call"
  | "product_writer_dispatch_start"
  | "artifact_emitted"
  // Parallel-DAG runner events (runtime/dag_executor.py). Surface as
  // progress-log rows in the same scrollback so the analyst sees per-node
  // start/done milestones identical to OE tool calls.
  | "dag_start"
  | "dag_wave_start"
  | "dag_node_start"
  | "dag_node_done"
  | "dag_node_failed"
  | "dag_done";

interface OeEvent {
  type: OeEventType;
  text?: string;
  name?: string;
  args?: Record<string, any>;
  summary?: string;
  brief?: string;
  artifact_id?: string;
  type_?: string;
  artifact_type?: string;
  htmlLink?: string | null;
  docxLink?: string | null;
  pptxLink?: string | null;
  xlsxLink?: string | null;
  /** data_integration_start: user-facing status text shown in the loading row. */
  message?: string;
  // DAG event extras — surfaced as a single-line label per row.
  node_id?: string;
  node_type?: string;
  product_type?: string;
  wave?: number;
  wave_size?: number;
  duration_ms?: number;
  error?: string;
  where?: string;
}

interface Message {
  id: string;
  role: MessageRole;
  /** HTML content; intel_agent returns HTML in its `answer` field. */
  content: string;
  timestamp: number;
  /** Populated for role === "artifact" — rendered as a download card. */
  artifact?: ArtifactCard;
  /** Populated for role === "operations_expert_event" — incremental progress log. */
  event?: OeEvent;
}

const OE_EVENT_TYPES = new Set<string>([
  "data_integration_start",
  "operations_expert_thinking",
  "operations_expert_tool_call",
  "operations_expert_tool_result",
  "intel_agent_dispatch_start",
  "intel_agent_tool_call",
  "product_writer_dispatch_start",
  "artifact_emitted",
  // DAG runner events surface in the same progress-log lane.
  "dag_start",
  "dag_wave_start",
  "dag_node_start",
  "dag_node_done",
  "dag_node_failed",
  "dag_done",
]);

function truncate(s: string, n = 200): string {
  if (!s) return "";
  return s.length <= n ? s : s.slice(0, n - 1) + "…";
}

function summarizeArgs(args?: Record<string, any>): string {
  if (!args || typeof args !== "object") return "";
  try {
    const parts: string[] = [];
    for (const [k, v] of Object.entries(args)) {
      let vs: string;
      if (typeof v === "string") vs = v;
      else if (typeof v === "number" || typeof v === "boolean") vs = String(v);
      else vs = JSON.stringify(v);
      parts.push(`${k}=${truncate(vs, 40)}`);
    }
    return parts.join(", ");
  } catch {
    return "";
  }
}

/**
 * Compact "Connection lost — reconnecting…" pill shown in the header when
 * the singleton WS is in the reconnecting / connecting state. Auto-clears
 * the moment status flips back to "open". Visible globally — i.e. even if
 * the chat panel isn't open, the analyst sees this pill via the tab.
 */
function ReconnectBanner({ status, attempts }: { status: WsStatus; attempts: number }) {
  if (status === "open") return null;
  // We render the banner only when it's actionable. "closed" only happens on
  // unmount (provider cleanup) — no analyst signal needed there.
  if (status === "closed") return null;
  const label =
    status === "reconnecting"
      ? `Connection lost — reconnecting${attempts > 1 ? ` (attempt ${attempts})` : "…"}`
      : "Connecting…";
  return (
    <div
      data-testid="ws-reconnect-banner"
      style={{
        position: "fixed",
        top: 8,
        right: 8,
        zIndex: 1000,
        background: "#b08400",
        color: "#fff",
        padding: "4px 10px",
        borderRadius: 12,
        fontSize: 11,
        fontWeight: 600,
        boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
        fontFamily: "inherit",
      }}
    >
      {label}
    </div>
  );
}

/** Collapsed drawer tab — a chat icon on the dark gradient rail. Click to
 *  open the chat drawer. Shared by the no-session and collapsed states. */
function CollapsedChatTab({ onOpen, title }: { onOpen: () => void; title: string }) {
  return (
    <div
      data-testid="chat-agent-tab"
      onClick={onOpen}
      title={title}
      style={{
        width: COLLAPSED_TAB_WIDTH,
        height: "100%",
        background: IDX26.userGrad,
        color: "#fff",
        cursor: "pointer",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        userSelect: "none",
      }}
    >
      <SubframeCore.Icon name="FeatherMessageSquare" className="text-[18px]" />
    </div>
  );
}

export default function ChatAgent() {
  const {
    isOpen,
    openChat,
    closeChat,
    sessionId,
    projectId,
    userId,
    initialMessage,
    manualProductType,
    onArtifact,
    initialMessageConsumed,
    markInitialMessageConsumed,
    currentArtifactId,
  } = useChatContext();
  // Per-doc shared analyst chat (IRC). When an artifact is open in
  // the editor (DocEditorPage pushes loadedArtifactId into
  // currentArtifactId), this panel REPLACES the legacy OE-conversation
  // scrollback as the visible chat surface. ChatAgentInner stays
  // mounted (CSS-hidden) so the /ws/intel_agent WS keeps processing
  // artifact_emitted / stage_event frames — the Generate flow needs
  // those side effects (onArtifact, the timeline pill, etc.).
  const { user } = useAppContext() || ({} as any);
  const userEmail: string = (user?.email || user?.userId || "") as string;
  const userFullName: string = (
    user?.firstName && user?.lastName
      ? `${user.firstName} ${user.lastName}`
      : (user?.email || user?.userId || "")
  ) as string;

  const wsApi = useIntelAgentWs();
  const location = useLocation();
  const pageLabel = labelForPath(location.pathname);

  // While a doc page is mid-load, reserve the chat column's final
  // width as an empty placeholder. Two reasons:
  //   1. Layout stability — if we return zero width here, the Collabora
  //      iframe loads with the FULL parent width and Impress computes
  //      its initial fit-to-slide against that. The chat column then
  //      appears once currentArtifactId lands, shrinks the iframe, and
  //      Impress is now rendering a slide sized for the larger viewport
  //      → horizontal scroll into empty space. The user reproduces this
  //      every refresh. Reserving the column width up front means the
  //      iframe sees its final width from frame 1.
  //   2. We still don't want the "INTEL AGENT" drawer flashing through
  //      the loading window, so the placeholder renders no chat UI and
  //      mounts no ChatAgentInner (no WS subscribe noise during load).
  // DocEditorPage pushes the artifact_id into ChatContext.currentArtifactId
  // via the LiveEditor onEditorReady callback (App_LoadingStatus →
  // Document_Loaded), so an empty value on the docs route means "doc
  // not yet painted."
  const onDocsRoute = location.pathname.startsWith("/missioncommand/docs/");
  // Chat lives only on a document (the doc-editor route); no chat elsewhere.
  if (!onDocsRoute) {
    return (
      <ReconnectBanner status={wsApi.status} attempts={wsApi.reconnectAttempts} />
    );
  }
  const docPageLoading = onDocsRoute && !currentArtifactId;
  if (docPageLoading) {
    return (
      <>
        <ReconnectBanner status={wsApi.status} attempts={wsApi.reconnectAttempts} />
        <div
          data-testid="chat-agent-column-placeholder"
          style={{
            flex: `0 0 ${isOpen ? OPEN_PANEL_WIDTH : COLLAPSED_TAB_WIDTH}px`,
            height: "100%",
            background: IDX26.bg2,
            borderLeft: `1px solid ${IDX26.line}`,
          }}
        />
      </>
    );
  }

  // The chat sidebar is ALWAYS mounted — every route shows at least the
  // collapsed tab so the analyst can pull the agent open from anywhere
  // (Dashboard, Operations Map, Doc Editor, Event Dashboard, etc.). When
  // there's no active sessionId, clicking the tab mints a fresh ad-hoc
  // sessionId so the analyst can start a chat from any page.
  if (!sessionId) {
    return (
      <>
        <ReconnectBanner status={wsApi.status} attempts={wsApi.reconnectAttempts} />
        <div
          data-testid="chat-agent-column"
          style={{
            flex: `0 0 ${COLLAPSED_TAB_WIDTH}px`,
            height: "100%",
            display: "flex",
            flexDirection: "row",
            borderLeft: `1px solid ${IDX26.line}`,
            background: IDX26.bg2,
          }}
        >
          <CollapsedChatTab
            title="Open chat"
            onOpen={() => {
              const adhocId =
                (window.crypto && typeof window.crypto.randomUUID === "function")
                  ? `adhoc-${window.crypto.randomUUID()}`
                  : `adhoc-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
              openChat({
                sessionId: adhocId,
                projectId: projectId || "",
                userId: userId || "",
                expand: true,
              });
            }}
          />
        </div>
      </>
    );
  }

  // Forward initialMessage only on the FIRST mount per sessionId. After it
  // has been consumed (artifact arrived or analyst sent a follow-up), a
  // refresh / re-open won't re-fire the synthesized "Generate {TYPE}".
  const forwardedInitialMessage = initialMessageConsumed
    ? undefined
    : initialMessage || undefined;
  const forwardedManualProductType = initialMessageConsumed
    ? undefined
    : manualProductType || undefined;

  // Wrap onArtifact so initialMessage is marked consumed the moment OE
  // produces an artifact (prevents a same-session refresh from re-firing).
  function handleArtifact(artifact: ArtifactCard) {
    markInitialMessageConsumed();
    if (onArtifact) {
      try {
        onArtifact(artifact);
      } catch (e) {
        clientWarn("ChatAgent.onArtifact", e);
      }
    }
  }

  // The panel is ALWAYS mounted once sessionId is set — visibility is toggled
  // via `display`. That keeps ChatAgentInner's message log alive across
  // collapse/expand cycles. The keyed-by-sessionId remount still happens
  // cleanly when sessionId itself changes.
  return (
    <>
      <ReconnectBanner status={wsApi.status} attempts={wsApi.reconnectAttempts} />
      <div
        data-testid="chat-agent-column"
        className="relative flex h-full flex-col border-l border-solid border-neutral-border bg-default-background"
        style={{ flex: `0 0 ${isOpen ? OPEN_PANEL_WIDTH : COLLAPSED_TAB_WIDTH}px` }}
      >
        {/* Single open/close toggle — the only chat control. */}
        <button
          type="button"
          data-testid="chat-agent-toggle"
          onClick={() =>
            isOpen ? closeChat() : openChat({ sessionId, projectId, userId, expand: true })
          }
          title={isOpen ? "Close chat" : "Open chat"}
          aria-label={isOpen ? "Close chat" : "Open chat"}
          className="absolute right-2 top-2 z-[5] flex items-center justify-center rounded border border-solid border-neutral-border bg-default-background p-1 text-subtext-color hover:bg-neutral-50"
        >
          <SubframeCore.Icon
            name={isOpen ? "FeatherChevronRight" : "FeatherMessageSquare"}
            className="text-body"
          />
        </button>

        {/* Mounted-but-hidden so its WS side effects keep firing. */}
        <div
          style={{
            position: "absolute",
            width: 0,
            height: 0,
            overflow: "hidden",
            visibility: "hidden",
            pointerEvents: "none",
          }}
        >
          <ChatAgentInner
            key={sessionId}
            sessionId={sessionId}
            projectId={projectId}
            userId={userId}
            pageLabel={pageLabel}
            initialMessage={forwardedInitialMessage}
            manualProductType={forwardedManualProductType}
            onArtifact={handleArtifact}
            onRequestClose={closeChat}
          />
        </div>

        {isOpen && (
          <div
            style={{
              flex: 1,
              minWidth: 0,
              minHeight: 0,
              display: "flex",
              flexDirection: "column",
            }}
          >
            <IrcChatPanel
              artifactId={currentArtifactId}
              email={userEmail}
              fullName={userFullName}
            />
          </div>
        )}
      </div>
    </>
  );
}

interface ChatAgentInnerProps {
  sessionId: string;
  projectId?: string;
  userId?: string;
  pageLabel?: string;
  initialMessage?: string;
  manualProductType?: string;
  onArtifact: (artifact: ArtifactCard) => void;
  onRequestClose: () => void;
}

function ChatAgentInner({
  sessionId,
  projectId,
  userId,
  pageLabel,
  initialMessage,
  manualProductType,
  onArtifact,
  onRequestClose,
}: ChatAgentInnerProps) {
  const wsApi = useIntelAgentWs();
  const chatCtx = useChatContext();

  // Chat history is persisted server-side per session_id — every turn writes
  // a session_details row via intel_agent.save_turn (question, answer,
  // agent_trace, timings). On mount we hydrate from GET
  // /api/session_details/{sessionId}; live events from the WS subscription
  // are appended in-memory. Refresh / nav-away preserves the history because
  // the backend is the source of truth.
  const [messages, setMessages] = useState<Message[]>([]);
  // Tracks the hydration round so a late-arriving response for a session
  // the user already navigated away from doesn't overwrite the new
  // session's loaded state.
  const hydrationKeyRef = useRef<string>("");
  // Last-seen message id (ms-precision timestamp) used by the WS reconnect
  // path to backfill segments that landed while we were offline. Ref, not
  // state — we update it on every appended message but don't need to
  // re-render when it changes.
  const lastMsgIdRef = useRef<number>(0);

  useEffect(() => {
    if (!sessionId) return;
    const myKey = `${sessionId}:${Date.now()}`;
    hydrationKeyRef.current = myKey;
    let cancelled = false;
    (async () => {
      try {
        // /api/sessions/{id}/messages returns the per-segment chat history
        // written by handlers.intel_agent.append_segment. Each row is one
        // user_msg or assistant_msg; ordered by timestamp asc. Server is
        // source of truth — F5, sidebar nav, multi-tab all re-hydrate
        // from this single endpoint.
        const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
        if (!res.ok || cancelled || hydrationKeyRef.current !== myKey) return;
        const body = await res.json();
        const arr = Array.isArray(body?.messages) ? body.messages : [];
        const hydrated: Message[] = [];
        for (const m of arr) {
          const content = String(m.content || "");
          if (!content) continue;
          const role = m.role === "user" ? "user"
                      : m.role === "assistant" ? "assistant"
                      : null;
          if (!role) continue;
          hydrated.push({
            id: `seg-${m.id}`,
            role,
            content,
            timestamp: Number(m.created_at_ms || m.id || 0),
          });
        }
        if (!cancelled && hydrationKeyRef.current === myKey) {
          setMessages(hydrated);
          // Track the highest message id we've seen so the WS reconnect
          // gap-fill can resume from here.
          if (arr.length > 0) {
            lastMsgIdRef.current = Math.max(
              ...arr.map((m: any) => Number(m.id || 0))
            );
          }
        }
      } catch (err) {
        clientWarn("ChatAgent.hydrate_session", err, { sessionId });
      }
    })();
    return () => { cancelled = true; };
  }, [sessionId]);

  // Gap-fill on WS reconnect. When the singleton WS transitions from
  // !open → open AFTER initial hydration (i.e. a reconnect, not first
  // connect), pull any segments persisted server-side during the gap
  // via /messages?since=<lastMsgIdRef>. Live segments arriving over the
  // newly-open WS will append normally; the REST gap-fill covers the
  // disconnect interval where push wasn't possible.
  const wsOpenPrevRef = useRef<boolean>(false);
  useEffect(() => {
    const isOpen = wsApi.status === "open";
    const wasOpen = wsOpenPrevRef.current;
    wsOpenPrevRef.current = isOpen;
    if (!sessionId) return;
    if (!isOpen || wasOpen) return; // Only fire on the !open→open transition.
    if (lastMsgIdRef.current <= 0) return; // No baseline; mount hydration owns this.
    let cancelled = false;
    (async () => {
      try {
        const since = lastMsgIdRef.current;
        const res = await fetch(
          `/api/sessions/${encodeURIComponent(sessionId)}/messages?since=${since}`
        );
        if (!res.ok || cancelled) return;
        const body = await res.json();
        const arr = Array.isArray(body?.messages) ? body.messages : [];
        if (!arr.length) return;
        const missed: Message[] = [];
        for (const m of arr) {
          const content = String(m.content || "");
          if (!content) continue;
          const role = m.role === "user" ? "user"
                      : m.role === "assistant" ? "assistant"
                      : null;
          if (!role) continue;
          missed.push({
            id: `seg-${m.id}`,
            role,
            content,
            timestamp: Number(m.created_at_ms || m.id || 0),
          });
        }
        if (!cancelled && missed.length) {
          // Dedupe against existing rendered messages by `id` (the seg-N
          // key). Live WS-pushed messages currently don't bump
          // lastMsgIdRef, so the same segment could come in both via
          // push and via gap-fill — dedupe on id to keep the list clean.
          setMessages((prev) => {
            const seen = new Set(prev.map((m) => m.id));
            const fresh = missed.filter((m) => !seen.has(m.id));
            return fresh.length ? [...prev, ...fresh] : prev;
          });
          lastMsgIdRef.current = Math.max(
            ...arr.map((m: any) => Number(m.id || 0))
          );
        }
      } catch (err) {
        clientWarn("ChatAgent.gap_fill", err, { sessionId });
      }
    })();
    return () => { cancelled = true; };
  }, [wsApi.status, sessionId]);

  const [draft, setDraft] = useState<string>("");
  const [status, setStatus] = useState<
    "idle" | "sending" | "thinking"
  >("idle");
  // Integration status pinned above the composer: tracks when the backend
  // started the data_integration phase (Cougaar fetch + push to geo-agent)
  // and what message to surface. Cleared the moment the first downstream
  // agent activity event arrives — by then the LLM dispatch has begun and
  // the user has visible progress. Independent of `status` (which tracks
  // composer state); both can be active at once.
  const [integrationStartedAt, setIntegrationStartedAt] = useState<number | null>(null);
  const [integrationMessage, setIntegrationMessage] = useState<string>("");
  // Ticking elapsed seconds — recomputed once per second while integration
  // is active. Stops the moment integrationStartedAt → null.
  const [integrationElapsedSec, setIntegrationElapsedSec] = useState<number>(0);
  useEffect(() => {
    if (integrationStartedAt == null) {
      setIntegrationElapsedSec(0);
      return;
    }
    setIntegrationElapsedSec(Math.floor((Date.now() - integrationStartedAt) / 1000));
    const id = setInterval(() => {
      setIntegrationElapsedSec(Math.floor((Date.now() - integrationStartedAt) / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [integrationStartedAt]);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const messageContainerRef = useRef<HTMLDivElement | null>(null);
  // Guards initialMessage firing exactly once per panel mount.
  const initialMessageSentRef = useRef<boolean>(false);
  // Latest onArtifact in a ref so the WS subscription stays stable across
  // re-renders without resubscribing.
  const onArtifactRef = useRef(onArtifact);
  useEffect(() => {
    onArtifactRef.current = onArtifact;
  }, [onArtifact]);

  // Display label for the timeline pill (lives in ChatContext).
  const productLabel = useMemo(() => {
    return manualProductType || sessionId;
  }, [manualProductType, sessionId]);

  // Register this session for the timeline UI as soon as we have a
  // manualProductType (Generate-button flow). beginSession is idempotent.
  useEffect(() => {
    if (!sessionId) return;
    chatCtx.beginSession({ sessionId, productLabel });
  // chatCtx.beginSession is stable; we only re-run when label changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, productLabel]);

  // Subscribe to the singleton WS for THIS sessionId. The provider routes
  // frames whose payload.sessionId matches; we filter further for our UI
  // shapes (OE events, stage_event, turn_summary, terminal answer).
  useEffect(() => {
    if (!sessionId) return;
    const unsubscribe = wsApi.subscribe(sessionId, {
      onMessage: (payload: any) => {
        // ---- generate_product_started: ACK from the DAG dispatcher ----
        // Carries job_id + product_type. Push job_id onto the URL so a
        // mid-generation refresh can resolve the active job via
        // /api/jobs/{id}. This is the only resilience hook the
        // bare-minimum jobs system needs — event replay is future work
        // (DESIGN §13). Same handling regardless of product_type because
        // both the DAG path (INTSUM) and the OE-manual fallback (non-
        // INTSUM) emit the same ACK from main._dispatch_generate_product.
        if (payload && payload.type === "generate_product_started") {
          const jobId = String(payload.job_id || "");
          if (jobId && typeof window !== "undefined") {
            try {
              const url = new URL(window.location.href);
              if (url.searchParams.get("job") !== jobId) {
                url.searchParams.set("job", jobId);
                window.history.replaceState(null, "", url.toString());
              }
            } catch (e) {
              clientWarn("ChatAgent.persist_job_id", e);
            }
          }
          return;
        }

        // ---- artifact_complete: DAG path terminal frame ---------------
        // The parallel-DAG runner (runtime/dag_executor) emits this as
        // its terminal "I'm done" signal instead of the OE path's
        // `{intel_agent: {artifacts: [...]}}` shape. Translate into the
        // same ArtifactCard the OE-terminal branch builds so DocEditor's
        // onArtifact callback fires uniformly.
        if (payload && payload.type === "artifact_complete") {
          const card: ArtifactCard = {
            artifact_id: String(payload.artifact_id || ""),
            artifact_type: String(payload.product_type || payload.artifact_type || ""),
            htmlLink: payload.htmlLink ?? null,
            docxLink: payload.docxLink ?? null,
            pptxLink: payload.pptxLink ?? null,
            // XLSX path — when the backend emits xlsxLink and no
            // narrative link, DocEditorPage routes to SheetEditor.
            xlsxLink: payload.xlsxLink ?? null,
            gaps: Array.isArray(payload.gaps) ? payload.gaps : [],
          };
          // Telemetry: a delivered product + the gaps the agent had to flag =
          // where the agent couldn't fully do the analyst's job. Fire-and-forget.
          logAnalystEvent({
            event_type: "chat.product_ready",
            surface: "chat",
            product_type: card.artifact_type || null,
            session_id: sessionId,
            artifact_id: card.artifact_id || null,
            value_num: card.gaps.length,
            detail: { gap_count: card.gaps.length, gaps: card.gaps },
          });
          setIntegrationStartedAt(null);
          setIntegrationMessage("");
          setMessages((prev) => [
            ...prev,
            {
              id: `art-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              role: "artifact",
              content: "",
              timestamp: Date.now(),
              artifact: card,
            },
          ]);
          chatCtx.attachArtifact(sessionId, card);
          if (typeof payload.turn_total_ms === "number") {
            // Best-effort timeline finalization for the DAG path — the
            // runner doesn't emit a turn_summary so feed the rollup
            // directly off artifact_complete.
            chatCtx.finalizeSession(sessionId, {
              total_duration_ms: Number(payload.turn_total_ms),
              per_stage_breakdown: [],
              artifact_id: card.artifact_id,
            });
          }
          if (onArtifactRef.current) {
            try {
              onArtifactRef.current(card);
            } catch (e) {
              clientWarn("ChatAgent.onArtifact", e);
            }
          }
          setStatus("idle");
          return;
        }

        // ---- error (dispatcher-emitted) -------------------------------
        // The DAG dispatcher emits `{type:"error", where, error}` on
        // terminal failures. The OE-manual fallback path drops a similar
        // frame on its own catch. Surface as an error message in the
        // scrollback so the analyst sees what failed rather than just
        // staring at a spinner forever.
        if (
          payload && payload.type === "error" &&
          (typeof payload.error === "string" || typeof payload.detail === "string")
        ) {
          const errText = String(payload.error || payload.detail || "unknown error");
          const where = payload.where ? ` (${payload.where})` : "";
          setIntegrationStartedAt(null);
          setIntegrationMessage("");
          setMessages((prev) => [
            ...prev,
            {
              id: `err-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              role: "error",
              content: `Generation failed${where}: ${errText}`,
              timestamp: Date.now(),
            },
          ]);
          chatCtx.markSessionError(sessionId);
          setStatus("idle");
          return;
        }

        // ---- stage_event → ChatContext timeline -----------------------
        if (payload && payload.type === "stage_event") {
          chatCtx.appendStageEvent(sessionId, {
            stage: String(payload.stage || ""),
            started_at: payload.started_at,
            completed_at: payload.completed_at,
            duration_ms: Number(payload.duration_ms || 0),
            extra: payload.extra || {},
            client_received_at: Date.now(),
          });
          return;
        }

        // ---- turn_summary → finalize the timeline ---------------------
        if (payload && payload.type === "turn_summary") {
          chatCtx.finalizeSession(sessionId, {
            total_duration_ms: Number(payload.total_duration_ms || 0),
            per_stage_breakdown: Array.isArray(payload.per_stage_breakdown)
              ? payload.per_stage_breakdown
              : [],
            artifact_id: payload.artifact_id,
          });
          // Advance lastMsgIdRef to the segment ids carried in this
          // turn_summary so the next WS reconnect's gap-fill resumes from
          // the right point (otherwise we'd refetch all segments since
          // initial hydration on every reconnect).
          const ids = [payload.user_segment_id, payload.assistant_segment_id]
            .map((v) => Number(v || 0))
            .filter((v) => v > 0);
          if (ids.length) {
            lastMsgIdRef.current = Math.max(lastMsgIdRef.current, ...ids);
          }
          return;
        }

        // ---- OE/IA/PW progress log entries ----------------------------
        if (
          payload &&
          typeof payload === "object" &&
          typeof payload.type === "string" &&
          OE_EVENT_TYPES.has(payload.type)
        ) {
          const event: OeEvent = payload;
          // data_integration_start arms the sticky composer-top status row
          // with a ticking timer. NOT a scrollback entry — skip the history
          // append. Once armed, the timer ticks through the WHOLE
          // generation; downstream events update the visible message but
          // keep the timer running. It clears only on a terminal frame
          // (artifact_complete, terminal answer, error) — see those handlers
          // above for the matching setIntegrationStartedAt(null) calls.
          if (event.type === "data_integration_start") {
            setIntegrationStartedAt(Date.now());
            setIntegrationMessage(event.message || "Integrating live data from Cougaar…");
            return;
          }
          // Live phase labels — refresh the message as the pipeline advances
          // so the analyst sees what the agent is currently doing alongside
          // the elapsed timer.
          if (integrationStartedAt != null) {
            if (event.type === "operations_expert_thinking") {
              setIntegrationMessage("Operations expert reasoning…");
            } else if (event.type === "intel_agent_dispatch_start") {
              setIntegrationMessage("Intel agent gathering live data…");
            } else if (event.type === "intel_agent_tool_call") {
              setIntegrationMessage(
                event.name ? `Intel agent: ${event.name}…` : "Intel agent: tool call…"
              );
            } else if (event.type === "product_writer_dispatch_start") {
              setIntegrationMessage(
                event.artifact_type
                  ? `Writer drafting ${event.artifact_type}…`
                  : "Writer drafting…"
              );
            } else if (event.type === "dag_start") {
              setIntegrationMessage("DAG runner started…");
            } else if (event.type === "dag_wave_start") {
              setIntegrationMessage(
                event.wave != null
                  ? `Wave ${event.wave} (${event.wave_size ?? "?"} nodes) running…`
                  : "DAG wave running…"
              );
            } else if (event.type === "dag_node_start") {
              setIntegrationMessage(
                event.node_id ? `Running ${event.node_id}…` : "DAG node running…"
              );
            } else if (event.type === "dag_node_done") {
              setIntegrationMessage(
                event.node_id ? `Completed ${event.node_id}` : "DAG node completed"
              );
              // Mirror each completed DAG node into StageTimeline as a
              // synthetic stage_event so the per-node latency breakdown
              // shows in the same timeline as the legacy stage_event
              // frames. Stage label: node_type/node_id (e.g.
              // "header_field/period_start_dtg"). `summary` from the
              // backend lands in `extra.summary` so summarizeExtra picks
              // it up.
              if (event.node_id) {
                chatCtx.appendStageEvent(sessionId, {
                  stage: event.node_type
                    ? `${event.node_type}/${event.node_id}`
                    : event.node_id,
                  duration_ms: Number(event.dur_ms || 0),
                  extra: {
                    wave: event.wave,
                    node_type: event.node_type,
                    ok: event.ok !== false,
                    summary: event.summary,
                    error: event.error,
                  },
                  client_received_at: Date.now(),
                });
              }
            }
            // artifact_emitted does NOT clear — terminal artifact_complete /
            // terminal answer above already does that for the canonical
            // end-of-turn signals.
          }
          // Suppress artifact_emitted from the chat scrollback. The
          // backend fires it twice per artifact (once pre-render with
          // null links, once post-render with real links) — both lines
          // would be redundant with the artifact CARD that
          // artifact_complete / intel_agent.artifacts builds below
          // (download links + product type + id all live there).
          if (event.type === "artifact_emitted") {
            return;
          }
          setMessages((prev) => [
            ...prev,
            {
              id: `evt-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              role: "operations_expert_event",
              content: "",
              timestamp: Date.now(),
              event,
            },
          ]);
          return;
        }

        // ---- Artifact frame -------------------------------------------
        const artifacts = payload?.intel_agent?.artifacts;
        if (Array.isArray(artifacts) && artifacts.length > 0) {
          const a = artifacts[0] || {};
          const card: ArtifactCard = {
            artifact_id: String(a.artifact_id || ""),
            artifact_type: String(a.type || a.artifact_type || ""),
            htmlLink: a.htmlLink ?? null,
            docxLink: a.docxLink ?? null,
            pptxLink: a.pptxLink ?? null,
            xlsxLink: a.xlsxLink ?? null,
            gaps: Array.isArray(a.gaps) ? a.gaps : [],
          };
          setMessages((prev) => [
            ...prev,
            {
              id: `art-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
              role: "artifact",
              content: "",
              timestamp: Date.now(),
              artifact: card,
            },
          ]);
          chatCtx.attachArtifact(sessionId, card);
          if (onArtifactRef.current) {
            try {
              onArtifactRef.current(card);
            } catch (e) {
              clientWarn("ChatAgent.onArtifact", e);
            }
          }
        }

        // ---- Terminal answer ------------------------------------------
        const answer = payload?.answer ?? payload?.message ?? "";
        if (!answer) return;
        // Backstop: clear integration timer in case it survived (no agent
        // events between data_integration_start and terminal). The composer
        // status row disappears.
        setIntegrationStartedAt(null);
        setIntegrationMessage("");
        setMessages((prev) => [
          ...prev,
          {
            id: `a-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
            role: payload?.errorMessage ? "error" : "assistant",
            content: String(answer),
            timestamp: Date.now(),
          },
        ]);
        if (payload?.errorMessage) {
          chatCtx.markSessionError(sessionId);
        }
        setStatus("idle");
      },
    });
    return unsubscribe;
  // chatCtx mutators are stable; subscribe only depends on sessionId.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, wsApi]);

  // Fire the FIRST message of this session. Two shapes, picked by whether
  // ChatContext supplied a `manualProductType`:
  //
  //   1. Generate-button flow (manualProductType set) — send a structured
  //      `{type:"generate_product", product_type, sessionId, ...}` frame.
  //      Backend routes it through `_dispatch_generate_product` in main.py:
  //         INTSUM     → parallel-DAG runner (~12s).
  //         non-INTSUM → operations-expert manual fallback (legacy baseline).
  //      Either way, the frontend sees the same event vocabulary because
  //      both branches emit through publishers that share frame types
  //      (stage_event, OE events, artifact frames, turn_summary, error).
  //
  //   2. Analyst typed a free-form first message (manualProductType empty,
  //      initialMessage carries the prompt) — keep the legacy
  //      `{question:{prompt:...}}` frame so OE handles it via natural-
  //      language dispatch. This path is unchanged.
  //
  // Either way we also push a local user-bubble so the analyst sees what
  // was just requested in the scrollback (the structured frame has no
  // human-readable prompt string).
  useEffect(() => {
    if (!sessionId) return;
    if (initialMessageSentRef.current) return;
    // Either initialMessage (legacy) OR manualProductType (Generate-button)
    // is enough to fire the first frame. If neither is set, the panel is in
    // an analyst-typing-first-message idle state; do nothing.
    if (!initialMessage && !manualProductType) return;
    initialMessageSentRef.current = true;
    setStatus("sending");

    if (manualProductType) {
      // Generate-button: structured frame. NOTE: no synthesized natural-
      // language prompt — the backend gates on `type` and routes straight
      // to the DAG / OE-manual fallback. We deliberately do NOT push a
      // local user-bubble for this initial frame: the legacy path didn't
      // either, and the hydration race (async session_details fetch
      // completes after this effect, setMessages(hydrated) clobbers any
      // local push for fresh sessions) would drop it anyway.
      const sent = wsApi.send(sessionId, {
        type: "generate_product",
        product_type: manualProductType,
        sessionId,
        session_id: sessionId,
        projectId: projectId || "",
        project_id: projectId || "",
        userId: userId || "",
        user_id: userId || "",
        // initialMessage doubles as the analyst's free-text context when
        // the Generate-button flow surfaces a context field in the UI.
        // Today the modal doesn't collect one — empty string in practice.
        context: "",
      });
      void sent;
      setStatus("thinking");
      return;
    }

    // Legacy free-form first message (analyst typed it).
    const sent = wsApi.send(sessionId, {
      action: "chat",
      type: "intel_agent",
      sessionId,
      projectId: projectId || "",
      userId: userId || "",
      question: { prompt: initialMessage },
      title: initialMessage,
    });
    if (!sent) {
      setStatus("thinking");
    } else {
      setStatus("thinking");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, initialMessage, manualProductType]);

  // Auto-scroll to bottom on new message.
  useEffect(() => {
    const el = messageContainerRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  function send() {
    const text = draft.trim();
    if (!text) return;
    setMessages((prev) => [
      ...prev,
      {
        id: `u-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: "user",
        content: text,
        timestamp: Date.now(),
      },
    ]);
    setDraft("");
    setStatus("sending");
    const sent = wsApi.send(sessionId, {
      action: "chat",
      type: "intel_agent",
      sessionId,
      projectId: projectId || "",
      userId: userId || "",
      question: { prompt: text },
      title: text,
    });
    if (!sent) {
      // The send was queued — let the user know we're not stuck, just
      // waiting on reconnect.
      setErrorMsg("queued (waiting for reconnect)");
    } else {
      setErrorMsg("");
    }
    setStatus("thinking");
  }

  // Derive a header status string blending local + WS status.
  const headerStatus = (() => {
    if (wsApi.status === "reconnecting") return "reconnecting…";
    if (wsApi.status === "connecting") return "connecting…";
    if (status === "sending") return "sending…";
    if (status === "thinking") return "thinking…";
    if (errorMsg) return errorMsg;
    return "connected";
  })();

  return (
    <div
      data-testid="chat-agent-inner"
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: IDX26.bg2,
        color: IDX26.text,
        borderLeft: `1px solid ${IDX26.line}`,
        fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif',
        position: "relative",
      }}
    >
      {/* No internal header bar — the chat panel slides under the page's
          existing top nav. Collapse button is a small floating affordance
          at the top-right of the messages area; scope context is conveyed
          by the composer placeholder ("Ask within <Page>…") and the live-
          status dot on that placeholder, not by a redundant header chrome. */}
      <button
        type="button"
        onClick={onRequestClose}
        aria-label="Collapse chat"
        title="Collapse chat"
        style={{
          position: "absolute",
          top: 8,
          right: 8,
          zIndex: 2,
          background: IDX26.bg3,
          color: IDX26.text2,
          border: `1px solid ${IDX26.line2}`,
          cursor: "pointer",
          padding: "2px 7px",
          borderRadius: 4,
          fontSize: 13,
          lineHeight: 1,
        }}
      >
        ›
      </button>

      <div
        ref={messageContainerRef}
        data-testid="chat-agent-messages"
        style={{
          flex: 1,
          overflowY: "auto",
          padding: 14,
          display: "flex",
          flexDirection: "column",
          gap: 10,
          background: IDX26.bg,
        }}
      >
        {messages.map((m) => (
          m.role === "operations_expert_event" && m.event ? (
            <OeEventRow key={m.id} event={m.event} />
          ) : (
          <div
            key={m.id}
            data-role={m.role}
            style={{
              alignSelf: m.role === "user" ? "flex-end" : "flex-start",
              maxWidth: "90%",
              padding: "9px 12px",
              borderRadius: 9,
              background:
                m.role === "user" ? IDX26.userGrad
                : m.role === "error" ? "rgba(248,113,113,0.10)"
                : IDX26.bg3,
              color:
                m.role === "user" ? "#fff"
                : m.role === "error" ? IDX26.red
                : IDX26.text,
              border:
                m.role === "user" ? `1px solid ${IDX26.accent}`
                : m.role === "error" ? `1px solid ${IDX26.red}`
                : `1px solid ${IDX26.line}`,
              fontSize: 13,
              lineHeight: 1.5,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {m.role === "assistant" ? (
              <div dangerouslySetInnerHTML={{ __html: m.content }} />
            ) : m.role === "artifact" && m.artifact ? (
              <div style={{ minWidth: 220 }}>
                <div style={{ fontWeight: 600, marginBottom: 4 }}>
                  {m.artifact.artifact_type} {m.artifact.artifact_id}
                </div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 4 }}>
                  {m.artifact.docxLink && (
                    <a
                      href={m.artifact.docxLink}
                      download
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        fontSize: 12, padding: "4px 8px",
                        background: "#eef2f7", color: "#2c3e50",
                        borderRadius: 4, textDecoration: "none",
                        border: "1px solid #c8d2dd",
                      }}
                    >DOCX</a>
                  )}
                  {m.artifact.pptxLink && (
                    <a
                      href={m.artifact.pptxLink}
                      download
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        fontSize: 12, padding: "4px 8px",
                        background: "#eef2f7", color: "#2c3e50",
                        borderRadius: 4, textDecoration: "none",
                        border: "1px solid #c8d2dd",
                      }}
                    >PPTX</a>
                  )}
                  {m.artifact.xlsxLink && (
                    <a
                      href={m.artifact.xlsxLink}
                      download
                      target="_blank"
                      rel="noreferrer"
                      style={{
                        fontSize: 12, padding: "4px 8px",
                        background: "#eef2f7", color: "#2c3e50",
                        borderRadius: 4, textDecoration: "none",
                        border: "1px solid #c8d2dd",
                      }}
                    >XLSX</a>
                  )}
                </div>
                {m.artifact.gaps && m.artifact.gaps.length > 0 && (
                  <div style={{ marginTop: 6, fontSize: 11, color: "#a25b00" }}>
                    {m.artifact.gaps.length} gap{m.artifact.gaps.length === 1 ? "" : "s"}
                  </div>
                )}
              </div>
            ) : (
              m.content
            )}
          </div>
          )
        ))}
      </div>

      {/* Sticky integration status — pinned above the composer (NOT in
          the scrollback). Spinner + message + ticking elapsed timer.
          Appears the moment the backend emits data_integration_start;
          disappears the moment any agent activity event arrives OR the
          terminal answer lands. Matches Chat.tsx's spinner+text status
          style (animate-spin border-2 border-blue-500 border-t-transparent). */}
      {integrationStartedAt != null && (
        <div
          data-testid="chat-integration-status"
          style={{
            padding: "8px 14px",
            background: IDX26.bg2,
            borderTop: `1px solid ${IDX26.line}`,
            display: "flex",
            alignItems: "center",
            gap: 10,
            fontSize: 12,
            fontFamily: IDX26.mono,
            color: IDX26.text2,
          }}
        >
          <span
            aria-hidden
            style={{
              width: 14, height: 14,
              border: "2px solid rgba(79,140,255,0.35)",
              borderTopColor: IDX26.accent,
              borderRadius: "50%",
              animation: "ibl-chat-spin 0.8s linear infinite",
              flexShrink: 0,
              display: "inline-block",
            }}
          />
          <span style={{ flex: "1 1 auto", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
            {integrationMessage || "Integrating live data from Cougaar…"}
          </span>
          <span
            style={{
              flexShrink: 0,
              color: IDX26.accent,
              fontVariantNumeric: "tabular-nums",
              minWidth: 40,
              textAlign: "right",
            }}
          >
            {`${Math.floor(integrationElapsedSec / 60)}:${String(integrationElapsedSec % 60).padStart(2, "0")}`}
          </span>
          <style>{`@keyframes ibl-chat-spin { to { transform: rotate(360deg) } }`}</style>
        </div>
      )}
      {/* Scope + status sliver above the composer. Tiny — not a header,
          just an inline indicator so analyst can see what page the chat
          is bound to and whether the WS is up. Lives directly above the
          input where it's visible without competing with the page nav. */}
      <div
        style={{
          padding: "6px 14px",
          background: IDX26.bg2,
          borderTop: `1px solid ${IDX26.line}`,
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 10.5,
          fontFamily: IDX26.mono,
          color: IDX26.text3,
          letterSpacing: "0.5px",
        }}
      >
        <span style={{
          width: 6, height: 6, borderRadius: "50%",
          background: wsApi.status === "open" ? IDX26.green : IDX26.yellow,
          boxShadow: wsApi.status === "open"
            ? "0 0 6px rgba(52,211,153,0.6)"
            : "0 0 6px rgba(251,191,36,0.6)",
          flex: "0 0 auto",
        }} />
        {pageLabel && (
          <span style={{
            color: IDX26.text2,
            textTransform: "uppercase",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            flex: "1 1 auto",
            minWidth: 0,
          }}>
            {pageLabel}
          </span>
        )}
        <span style={{ flex: "0 0 auto" }}>{headerStatus}</span>
      </div>
      <form
        data-testid="chat-agent-form"
        onSubmit={(e) => { e.preventDefault(); send(); }}
        style={{
          padding: "10px 14px 14px",
          display: "flex",
          gap: 8,
          background: IDX26.bg2,
        }}
      >
        <textarea
          data-testid="chat-agent-input"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              send();
            }
          }}
          placeholder={pageLabel ? `Ask within ${pageLabel}…` : "Ask…"}
          rows={2}
          style={{
            flex: 1,
            resize: "none",
            padding: "10px 13px",
            fontSize: 13,
            fontFamily: "inherit",
            background: IDX26.bg3,
            color: IDX26.text,
            border: `1px solid ${IDX26.line2}`,
            borderRadius: 9,
            outline: "none",
          }}
        />
        <button
          type="submit"
          disabled={!draft.trim() || status === "sending"}
          style={{
            padding: "0 16px",
            background: IDX26.userGrad,
            color: "#fff",
            border: `1px solid ${IDX26.accent}`,
            borderRadius: 9,
            cursor: "pointer",
            fontSize: 13,
            fontWeight: 600,
            opacity: !draft.trim() || status === "sending" ? 0.55 : 1,
          }}
        >
          Send
        </button>
      </form>
    </div>
  );
}

/** OeEventRow — dim, tight progress log row. Distinct visual treatment from
 *  the analyst↔OE bubble: no border, smaller font, muted color. Reads like
 *  "log of what the agent is doing" rather than a conversational turn.
 *
 *  operations_expert_thinking is collapsible — click to expand the full reasoning trace
 *  (Gemma's thinking is often multi-paragraph and we don't want it dominating
 *  the chat scrollback). */
function OeEventRow({ event }: { event: OeEvent }) {
  const [expanded, setExpanded] = useState<boolean>(false);
  const baseStyle: CSSProperties = {
    alignSelf: "stretch",
    fontSize: 11,
    lineHeight: 1.4,
    color: IDX26.text3,
    fontFamily: IDX26.mono,
    padding: "2px 4px",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  };
  // data_integration_start is rendered as a sticky composer-top status row
  // (see <IntegrationStatus> below) — NOT inline in the scrollback. The
  // backend still emits it as a stream event so the composer-top indicator
  // can pick up the message and the elapsed timer.
  if (event.type === "data_integration_start") {
    return null;
  }
  if (event.type === "operations_expert_thinking") {
    const full = event.text || "";
    const shown = expanded ? full : truncate(full, 200);
    return (
      <div
        data-event-type="operations_expert_thinking"
        style={{ ...baseStyle, fontStyle: "italic", cursor: full.length > 200 ? "pointer" : "default" }}
        onClick={() => { if (full.length > 200) setExpanded((v) => !v); }}
        title={full.length > 200 ? (expanded ? "Click to collapse" : "Click to expand") : undefined}
      >
        Thinking — {shown}
      </div>
    );
  }
  if (event.type === "operations_expert_tool_call") {
    const argSummary = summarizeArgs(event.args);
    return (
      <div data-event-type="operations_expert_tool_call" style={baseStyle}>
        Tool call — {event.name}{argSummary ? ` (${truncate(argSummary, 120)})` : ""}
      </div>
    );
  }
  if (event.type === "operations_expert_tool_result") {
    return (
      <div data-event-type="operations_expert_tool_result" style={baseStyle}>
        Result — {event.name}: {truncate(event.summary || "", 160)}
      </div>
    );
  }
  if (event.type === "intel_agent_dispatch_start") {
    return (
      <div data-event-type="intel_agent_dispatch_start" style={{ ...baseStyle, color: "#6a7280" }}>
        Intel agent — {truncate(event.brief || "", 200)}
      </div>
    );
  }
  if (event.type === "intel_agent_tool_call") {
    return (
      <div data-event-type="intel_agent_tool_call" style={{ ...baseStyle, paddingLeft: 18 }}>
        · {event.name}
      </div>
    );
  }
  if (event.type === "product_writer_dispatch_start") {
    return (
      <div data-event-type="product_writer_dispatch_start" style={{ ...baseStyle, color: "#6a7280" }}>
        Product writer drafting{event.artifact_type ? ` ${event.artifact_type}` : ""}…
      </div>
    );
  }
  if (event.type === "artifact_emitted") {
    return (
      <div data-event-type="artifact_emitted" style={{ ...baseStyle, color: "#27ae60" }}>
        Emitted — {event.type_ || event.artifact_type || "artifact"} {event.artifact_id}
      </div>
    );
  }
  return null;
}
