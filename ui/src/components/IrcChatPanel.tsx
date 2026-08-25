/**
 * IrcChatPanel — shared-analyst chat for the LiveEditor.
 *
 * Renders the IRC channel #doc-<artifact_id> as a familiar
 * scrollback + input pair. Talks to the Ergo daemon via the
 * useIrcChannel hook; the surrounding ChatAgent component decides
 * when to mount this (an artifact is open in the editor).
 *
 * Design intent:
 *   - Email-prefix nick at the top so the analyst sees who they
 *     are connected as (and so collisions like alice_2 are
 *     obvious).
 *   - Scrollback replay from Ergo's autoreplay-on-join is
 *     visually identical to live messages — there's no
 *     "earlier today" separator. The user joined a channel and
 *     the last 100 messages are theirs to read.
 *   - Status pill (connecting / joined / disconnected) is small;
 *     analysts don't need to fix transport problems and the
 *     reconnect logic handles them.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as SubframeCore from "@subframe/core";
import { useIrcChannel } from "../lib/useIrcChannel";

interface Props {
  /** Artifact id whose per-doc channel this panel renders. Empty
   *  string ⇒ placeholder shown (unless `channel` is given); no IRC
   *  connection opened. */
  artifactId: string;
  /** Explicit channel name (e.g. "#operationalmap"). When set, the
   *  panel joins this fixed room instead of the per-doc channel and
   *  no artifact is required — used by the operations map for a
   *  shared multi-user chat room. */
  channel?: string;
  /** Analyst's email; the local-part becomes the IRC nick. */
  email: string;
  /** Display name for the "Approved by …" / login chrome. Used as
   *  the IRC `realname` field on connect (visible in /whois). */
  fullName?: string;
  /** Collapse the chat sidebar. The legacy ChatAgentInner had a
   *  floating ✕ button at top-right; that button is hidden behind
   *  this overlay, so this header rebinds it. */
  onClose?: () => void;
}

function fmtTime(ts: number): string {
  const d = new Date(ts);
  // Compact 24h time — locale-independent on purpose so analysts
  // see uniform timestamps across browsers.
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `${hh}:${mm}`;
}

/**
 * Stable per-nick color from a tiny palette. Hashes the nick into
 * one of ten earth-tones so each analyst's messages are visually
 * distinguishable without a per-user setting. Deterministic — same
 * nick always gets the same color across reloads.
 */
const NICK_COLORS = [
  "#3b82f6", "#0ea5e9", "#14b8a6", "#22c55e",
  "#84cc16", "#eab308", "#f97316", "#ef4444",
  "#ec4899", "#a855f7",
];
function colorForNick(nick: string): string {
  let h = 0;
  for (let i = 0; i < nick.length; i++) {
    h = ((h << 5) - h + nick.charCodeAt(i)) | 0;
  }
  return NICK_COLORS[Math.abs(h) % NICK_COLORS.length];
}

export default function IrcChatPanel({ artifactId, channel: channelOverride, email, fullName, onClose }: Props) {
  const { channel, nick, status, messages, send, reason } = useIrcChannel({
    artifactId,
    email,
    channel: channelOverride,
  });
  const [draft, setDraft] = useState<string>("");
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const taRef = useRef<HTMLTextAreaElement | null>(null);

  // Auto-grow the composer up to ~6 lines, then scroll.
  useEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [draft]);

  // Auto-scroll to bottom on new messages, but only when the analyst
  // is already near the bottom — preserves scrollback reading if
  // they've scrolled up to find earlier context.
  const lastCountRef = useRef<number>(messages.length);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const prevCount = lastCountRef.current;
    lastCountRef.current = messages.length;
    if (messages.length <= prevCount) return; // only react to additions
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages.length]);

  // First mount of a channel: snap to bottom so the autoreplay
  // history starts the analyst at "right now," not at the oldest
  // replayed message.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [channel]);

  const onSubmit = useCallback((e: React.FormEvent) => {
    e.preventDefault();
    const text = draft.trim();
    if (!text) return;
    send(text);
    setDraft("");
  }, [draft, send]);

  const onKeyDown = useCallback((e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter sends; Shift+Enter inserts a newline for multi-line.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      const text = draft.trim();
      if (!text) return;
      send(text);
      setDraft("");
    }
  }, [draft, send]);

  const statusBadge = useMemo(() => {
    switch (status) {
      case "idle": return { text: "no doc open", tone: "muted" as const };
      case "connecting": return { text: "connecting…", tone: "muted" as const };
      case "connected": return { text: "joining…", tone: "muted" as const };
      case "joined": return { text: "live", tone: "ok" as const };
      case "disconnected": return { text: "reconnecting…", tone: "warn" as const };
      case "error": return { text: "error", tone: "err" as const };
    }
  }, [status]);

  if (!artifactId && !channelOverride) {
    return (
      <div className="flex h-full w-full items-center justify-center p-4 text-caption text-subtext-color">
        Open a document to start a shared-editor chat.
      </div>
    );
  }

  return (
    <div className="flex h-full w-full flex-col bg-default-background">
      {/* Header strip — nick + connection status. The IRC channel
          name (#doc-<artifact_id>) is intentionally NOT surfaced;
          analysts don't need to see the protocol-level routing key,
          they just see "this is the chat for the doc I'm editing."
          The channel value is still used for sending/receiving via
          the hook — it's just not in the UI. */}
      <div className="flex flex-none items-center gap-2 border-b border-solid border-neutral-border pl-3 pr-10 py-2.5">
        <span className="text-caption font-caption text-subtext-color truncate" title={fullName || email}>
          you: <span style={{ color: colorForNick(nick) }}>{nick}</span>
        </span>
        <span
          className={
            "ml-auto rounded px-1.5 py-0.5 text-[10px] font-caption-bold uppercase tracking-wider " +
            (statusBadge.tone === "ok"
              ? "bg-success-100 text-success-800"
              : statusBadge.tone === "warn"
                ? "bg-warning-100 text-warning-800"
                : statusBadge.tone === "err"
                  ? "bg-error-100 text-error-800"
                  : "bg-neutral-100 text-subtext-color")
          }
          title={reason || ""}
        >
          {statusBadge.text}
        </span>
        {onClose && (
          <button
            type="button"
            onClick={onClose}
            aria-label="Close chat"
            title="Close chat"
            className="flex items-center justify-center rounded border border-solid border-neutral-border p-1 text-subtext-color hover:bg-neutral-50"
          >
            <SubframeCore.Icon name="FeatherChevronRight" className="text-body" />
          </button>
        )}
      </div>

      {/* Scrollback. */}
      <div
        ref={scrollRef}
        className="flex flex-1 min-h-0 flex-col gap-2 overflow-y-auto px-3 py-3"
      >
        {messages.length === 0 ? (
          <div className="m-auto text-caption text-subtext-color">
            No messages in this channel yet.
          </div>
        ) : (
          messages.map((m) => (
            <div
              key={m.id}
              className="flex items-baseline gap-2 text-body font-body"
              style={m.isHistory ? { opacity: 0.85 } : undefined}
            >
              <span
                className="font-mono text-caption font-caption text-subtext-color flex-none"
                title={new Date(m.timestamp).toLocaleString()}
              >
                {fmtTime(m.timestamp)}
              </span>
              <span
                className="font-bold flex-none"
                style={{ color: colorForNick(m.nick) }}
              >
                {m.nick}
              </span>
              <span className="text-default-font whitespace-pre-wrap break-words">
                {m.text}
              </span>
            </div>
          ))
        )}
      </div>

      {/* Input. */}
      <form
        onSubmit={onSubmit}
        className="flex flex-none items-end gap-2 border-t border-solid border-neutral-border px-3 py-2.5"
      >
        <textarea
          ref={taRef}
          rows={1}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder={
            status === "joined"
              ? "Message — Enter to send, Shift-Enter for newline"
              : "Waiting to join…"
          }
          disabled={status !== "joined"}
          style={{ minHeight: 36, maxHeight: 140 }}
          className="flex-1 resize-none rounded border border-solid border-neutral-border bg-default-background px-2.5 py-1.5 text-body font-body leading-relaxed text-default-font outline-none focus:border-brand-400 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={status !== "joined" || !draft.trim()}
          className="rounded bg-brand-600 px-3 py-1.5 text-caption-bold font-caption-bold text-white disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}
