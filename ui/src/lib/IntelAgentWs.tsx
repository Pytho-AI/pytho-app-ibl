/**
 * IntelAgentWs — singleton WebSocket to /ws/intel_agent/{client_id}.
 *
 * Why this exists:
 *
 *   Previously each ChatAgentInner mount opened its own WS. That's fine for
 *   ONE artifact-generation flow at a time, but the analyst on a flaky link
 *   needs to:
 *
 *     - keep the WS alive across mid-turn network drops (exponential
 *       backoff reconnect + heartbeat),
 *     - run multiple generations in parallel (HVTL + CARVER + threat model
 *       all streaming over the same socket), each with its own progress
 *       state in the UI.
 *
 *   So we hoist the socket to the App chrome. It opens once when the
 *   provider mounts, stays open as long as the page is loaded, and
 *   multiplexes per-session subscriptions on top.
 *
 * Resilience:
 *   - Reconnect: exponential backoff (500ms → 30s cap) with full jitter,
 *     unbounded attempts. Page-lifetime only — never gives up.
 *   - Heartbeat: send {type:"ping"} every 20s; if no {type:"pong"} back in
 *     25s, the socket is declared dead and forcibly recycled.
 *   - Resume: on reconnect, every active session subscription's
 *     `last_seen_event_id` is sent as {type:"resume",...}. Backend currently
 *     ignores this — we log a WARN and proceed. When backend support lands,
 *     missed stage_events will replay without any client-side changes.
 *
 * Multiplexing:
 *   - Any caller can `subscribe(sessionId, handlers)` to receive WS messages
 *     scoped to a given session. Messages whose body sessionId/session_id
 *     matches are routed to that subscription; messages with no sessionId
 *     are broadcast to every active subscription (e.g. server-wide errors).
 *   - `send(sessionId, payload)` injects sessionId into the payload and
 *     ships it. The backend already keys turns by sessionId, so the only
 *     thing the client has to do is tag outbound frames correctly.
 *
 * Visible state (via useIntelAgentWs):
 *   - status: "connecting" | "open" | "reconnecting" | "closed"
 *   - reconnectAttempts: integer, resets to 0 on first successful pong
 *
 * NOT done here (intentional):
 *   - Tracking per-stage timelines. That's ChatContext's job — the WS
 *     surface just routes raw frames to subscribers, who decide what to do.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { clientWarn } from "./clientLog";

export type WsStatus =
  | "connecting"
  | "open"
  | "reconnecting"
  | "closed";

export interface WsMessage {
  // Backend frames are heterogeneous. The IntelAgentWs layer doesn't try to
  // type-narrow them — it routes by sessionId and passes the raw payload to
  // the subscriber.
  [key: string]: any;
}

export interface SessionSubscription {
  /** Called for every frame routed to this session. Implementations should
   *  guard against unrelated event types — the WS hands them every frame
   *  matching the sessionId, including pings/pongs that slipped through. */
  onMessage: (payload: WsMessage) => void;
  /** Last seen event id (if backend ever emits one); sent on resume. */
  lastSeenEventId?: string | null;
}

interface IntelAgentWsValue {
  status: WsStatus;
  reconnectAttempts: number;
  /** Subscribe a session to incoming frames. Returns an unsubscribe fn.
   *  Multiple subscribes for the same sessionId stack — last-wins is NOT
   *  enforced; each handler is called in registration order. */
  subscribe: (sessionId: string, sub: SessionSubscription) => () => void;
  /** Send a frame on behalf of a session. The sessionId is merged into the
   *  payload as both `sessionId` and `session_id` so handlers using either
   *  shape pick it up. Returns true if the frame was sent immediately; false
   *  if the socket is closed (caller can re-send on reconnect via the
   *  resume mechanism, OR queue + retry — current code just drops). */
  send: (sessionId: string, payload: WsMessage) => boolean;
  /** Last seen event id for a session — used internally for resume but
   *  exposed for debugging. */
  getLastSeenEventId: (sessionId: string) => string | null;
}

const IntelAgentWsContext = createContext<IntelAgentWsValue | null>(null);

export function useIntelAgentWs(): IntelAgentWsValue {
  const ctx = useContext(IntelAgentWsContext);
  if (!ctx) {
    throw new Error(
      "useIntelAgentWs must be used inside <IntelAgentWsProvider>. " +
        "The provider is mounted at App.tsx root level (inside ChatProvider)."
    );
  }
  return ctx;
}

function buildWsUrl(): string {
  const envUrl =
    (import.meta as any)?.env?.VITE_INTEL_AGENT_WS as string | undefined;
  if (envUrl) return envUrl;
  // Same-origin fallback; protocol follows the page so HTTPS deployments get
  // wss:// automatically.
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/intel_agent`;
}

function randomClientId(): string {
  return `chat-${Date.now()}-${Math.floor(Math.random() * 100000)}`;
}

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 30_000;
const HEARTBEAT_INTERVAL_MS = 20_000;
const HEARTBEAT_TIMEOUT_MS = 25_000;

function nextBackoff(attempt: number): number {
  // Full-jitter backoff (Polly/AWS pattern). Pick a random value in
  // [0, min(cap, base*2^attempt)] so a fleet of clients reconnecting after
  // a server blip doesn't dogpile.
  const exp = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt);
  return Math.floor(Math.random() * exp);
}

export function IntelAgentWsProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<WsStatus>("connecting");
  const [reconnectAttempts, setReconnectAttempts] = useState<number>(0);

  // Stable client id for the lifetime of this provider instance (i.e. the
  // page). If the WS reconnects, the same client_id is used so the backend
  // (when it gains resume support) can resume the same session log.
  const clientIdRef = useRef<string>(randomClientId());

  const socketRef = useRef<WebSocket | null>(null);
  const subscriptionsRef = useRef<
    Map<string, Set<SessionSubscription>>
  >(new Map());
  const lastSeenRef = useRef<Map<string, string>>(new Map());
  // Outbox: frames sent while the socket is not open get queued and flushed
  // on next open. Each entry is the JSON-encoded string we'd have sent.
  const outboxRef = useRef<string[]>([]);

  const reconnectAttemptsRef = useRef<number>(0);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const heartbeatTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const heartbeatDeadlineRef = useRef<ReturnType<typeof setTimeout> | null>(
    null
  );
  const mountedRef = useRef<boolean>(true);

  const clearHeartbeat = useCallback(() => {
    if (heartbeatTimerRef.current) {
      clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (heartbeatDeadlineRef.current) {
      clearTimeout(heartbeatDeadlineRef.current);
      heartbeatDeadlineRef.current = null;
    }
  }, []);

  const armHeartbeatDeadline = useCallback((sock: WebSocket) => {
    if (heartbeatDeadlineRef.current) {
      clearTimeout(heartbeatDeadlineRef.current);
    }
    heartbeatDeadlineRef.current = setTimeout(() => {
      // No pong came back in time. Force-close — the onclose handler will
      // schedule a reconnect.
      clientWarn("IntelAgentWs.heartbeat_timeout", "no pong within deadline");
      try {
        sock.close();
      } catch {
        /* noop */
      }
    }, HEARTBEAT_TIMEOUT_MS);
  }, []);

  const startHeartbeat = useCallback(
    (sock: WebSocket) => {
      clearHeartbeat();
      heartbeatTimerRef.current = setInterval(() => {
        if (sock.readyState !== WebSocket.OPEN) return;
        try {
          sock.send(JSON.stringify({ type: "ping" }));
          armHeartbeatDeadline(sock);
        } catch (err) {
          clientWarn("IntelAgentWs.ping_send", err);
        }
      }, HEARTBEAT_INTERVAL_MS);
    },
    [armHeartbeatDeadline, clearHeartbeat]
  );

  // Drain the outbox to a freshly-open socket.
  const flushOutbox = useCallback((sock: WebSocket) => {
    const pending = outboxRef.current;
    if (!pending.length) return;
    outboxRef.current = [];
    for (const frame of pending) {
      try {
        sock.send(frame);
      } catch (err) {
        clientWarn("IntelAgentWs.outbox_flush", err);
        // Put the rest back at the head of the queue — next reconnect will
        // try again.
        outboxRef.current.unshift(frame);
        return;
      }
    }
  }, []);

  // Re-send a {type:"resume",...} for each active session subscription. If
  // the backend doesn't support resume, the frame is harmless (handler
  // ignores unknown shapes); we log a WARN once per reconnect so it's
  // obvious in dev.
  const sendResumeFrames = useCallback((sock: WebSocket) => {
    const subscribedSessions = Array.from(subscriptionsRef.current.keys());
    if (!subscribedSessions.length) return;
    for (const sessionId of subscribedSessions) {
      const lastSeen = lastSeenRef.current.get(sessionId) ?? null;
      try {
        sock.send(
          JSON.stringify({
            type: "resume",
            client_id: clientIdRef.current,
            sessionId,
            session_id: sessionId,
            last_seen_event_id: lastSeen,
          })
        );
      } catch (err) {
        clientWarn("IntelAgentWs.resume_send", err, { sessionId });
      }
    }
    // Backend resume support is best-effort; missed stage_events MAY not
    // replay. Intentionally not logged on every reconnect — that fired on
    // every laptop wake.
  }, []);

  const connect = useCallback(() => {
    if (!mountedRef.current) return;
    const url = (() => {
      const base = buildWsUrl();
      return base.endsWith("/ws/intel_agent")
        ? `${base}/${clientIdRef.current}`
        : base;
    })();

    let sock: WebSocket;
    try {
      sock = new WebSocket(url);
    } catch (err) {
      clientWarn("IntelAgentWs.construct", err, { url });
      scheduleReconnect();
      return;
    }
    socketRef.current = sock;

    sock.onopen = () => {
      if (!mountedRef.current) return;
      setStatus("open");
      // Mark reconnect attempts reset only after we KNOW the link is healthy
      // — i.e. after the first pong (below). For now, keep the count visible
      // until heartbeat confirms.
      sendResumeFrames(sock);
      // Request a full product_records snapshot on every open (initial
      // connect AND reconnect). Backend responds with a
      // product_records_snapshot frame which the useProductChains reducer
      // handles. This ensures the UI re-syncs after any drop without
      // depending on resume support.
      try {
        sock.send(JSON.stringify({ type: "request_snapshot" }));
      } catch (err) {
        clientWarn("IntelAgentWs.request_snapshot", err);
      }
      flushOutbox(sock);
      startHeartbeat(sock);
    };

    sock.onmessage = (evt: MessageEvent) => {
      if (!mountedRef.current) return;
      let payload: WsMessage;
      try {
        payload = JSON.parse(evt.data);
      } catch {
        // Non-JSON frames are surfaced via a synthetic { answer: <raw> }.
        payload = { answer: String(evt.data) };
      }

      // Heartbeat: pong clears the deadline and resets the attempt counter.
      if (payload && payload.type === "pong") {
        if (heartbeatDeadlineRef.current) {
          clearTimeout(heartbeatDeadlineRef.current);
          heartbeatDeadlineRef.current = null;
        }
        if (reconnectAttemptsRef.current !== 0) {
          reconnectAttemptsRef.current = 0;
          setReconnectAttempts(0);
        }
        return;
      }

      // Track last_seen_event_id when the backend includes one (e.g. a
      // stage_event with `event_id`).
      const sessionId =
        (payload && (payload.sessionId || payload.session_id)) || "";
      if (sessionId && typeof payload.event_id === "string") {
        lastSeenRef.current.set(sessionId, payload.event_id);
      }

      // Fan out to subscribers for this session, OR every subscriber if the
      // frame is sessionless.
      const subs = subscriptionsRef.current;
      if (sessionId && subs.has(sessionId)) {
        for (const s of subs.get(sessionId)!) {
          try {
            s.onMessage(payload);
          } catch (err) {
            clientWarn("IntelAgentWs.subscriber_threw", err, { sessionId });
          }
        }
      } else if (!sessionId) {
        // Broadcast.
        for (const set of subs.values()) {
          for (const s of set) {
            try {
              s.onMessage(payload);
            } catch (err) {
              clientWarn("IntelAgentWs.broadcast_subscriber_threw", err);
            }
          }
        }
      }
    };

    sock.onerror = () => {
      // onclose will handle reconnect; we don't trigger anything here.
    };

    sock.onclose = () => {
      clearHeartbeat();
      if (!mountedRef.current) return;
      socketRef.current = null;
      // Anything we tried to send between connect-fail and reconnect lives
      // in the outbox; nothing to do here.
      scheduleReconnect();
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    clearHeartbeat,
    flushOutbox,
    sendResumeFrames,
    startHeartbeat,
  ]);

  const scheduleReconnect = useCallback(() => {
    if (!mountedRef.current) return;
    if (reconnectTimerRef.current) {
      clearTimeout(reconnectTimerRef.current);
    }
    const attempt = reconnectAttemptsRef.current;
    const delay = nextBackoff(attempt);
    reconnectAttemptsRef.current = attempt + 1;
    setReconnectAttempts(attempt + 1);
    setStatus("reconnecting");
    reconnectTimerRef.current = setTimeout(() => {
      reconnectTimerRef.current = null;
      setStatus("connecting");
      connect();
    }, delay);
  }, [connect]);

  // Open on mount; close on unmount.
  useEffect(() => {
    mountedRef.current = true;
    setStatus("connecting");
    connect();
    return () => {
      mountedRef.current = false;
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      clearHeartbeat();
      const sock = socketRef.current;
      socketRef.current = null;
      try {
        sock?.close();
      } catch {
        /* noop */
      }
      setStatus("closed");
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const subscribe = useCallback(
    (sessionId: string, sub: SessionSubscription) => {
      const subs = subscriptionsRef.current;
      if (!subs.has(sessionId)) subs.set(sessionId, new Set());
      subs.get(sessionId)!.add(sub);
      if (sub.lastSeenEventId) {
        lastSeenRef.current.set(sessionId, sub.lastSeenEventId);
      }
      return () => {
        const set = subs.get(sessionId);
        if (!set) return;
        set.delete(sub);
        if (set.size === 0) {
          subs.delete(sessionId);
          // Last subscriber for this session is gone — drop the
          // last_seen_event_id entry so it doesn't leak indefinitely.
          // A future subscribe for the same sessionId will re-seed it
          // from SessionSubscription.lastSeenEventId.
          lastSeenRef.current.delete(sessionId);
        }
      };
    },
    []
  );

  const send = useCallback((sessionId: string, payload: WsMessage) => {
    const tagged = {
      ...payload,
      sessionId: payload.sessionId ?? sessionId,
      session_id: payload.session_id ?? sessionId,
    };
    const frame = JSON.stringify(tagged);
    const sock = socketRef.current;
    if (sock && sock.readyState === WebSocket.OPEN) {
      try {
        sock.send(frame);
        return true;
      } catch (err) {
        clientWarn("IntelAgentWs.direct_send", err, { sessionId });
      }
    }
    // Queue for next open.
    outboxRef.current.push(frame);
    return false;
  }, []);

  const getLastSeenEventId = useCallback(
    (sessionId: string) => lastSeenRef.current.get(sessionId) ?? null,
    []
  );

  const value = useMemo<IntelAgentWsValue>(
    () => ({
      status,
      reconnectAttempts,
      subscribe,
      send,
      getLastSeenEventId,
    }),
    [status, reconnectAttempts, subscribe, send, getLastSeenEventId]
  );

  return (
    <IntelAgentWsContext.Provider value={value}>
      {children}
    </IntelAgentWsContext.Provider>
  );
}
