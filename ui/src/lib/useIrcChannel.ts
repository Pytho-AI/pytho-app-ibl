/**
 * useIrcChannel — single-channel chat-state hook over IrcClient.
 *
 * Lifecycle: opens an IrcClient on mount, joins the channel, listens
 * for messages, exposes `{messages, send, status, nick}`. On unmount
 * destroys the client (sends QUIT via WS close so Ergo notices the
 * part immediately). Re-runs from scratch when `(nick, channel)`
 * changes — typical when the analyst switches docs.
 *
 * Why ONE client per mount instead of a global multiplex: at our
 * scale (few-dozen analysts, one open doc per analyst-tab) the
 * per-mount model is simpler and the few-extra-WS-connections cost
 * is negligible. Refactor to a global IrcContext + per-channel
 * subscription if/when the UX surfaces multiple channels at once.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  IrcClient,
  buildIrcWsUrl,
  channelForArtifact,
  nickFromEmail,
  type IrcEvent,
  type IrcMessage,
} from "./ircClient";

export type IrcStatus = "idle" | "connecting" | "connected" | "joined" | "disconnected" | "error";

export interface UseIrcChannelOpts {
  /** Artifact id used to derive the channel name. Empty string =
   *  the hook stays idle and never connects (caller hasn't picked
   *  a doc yet) — unless `channel` is given. */
  artifactId: string;
  /** Analyst's email — only the local-part is used as the nick. */
  email: string;
  /** Explicit channel name (e.g. "#operationalmap"). When set it
   *  overrides the artifact-derived "#doc-<id>" channel, so a caller
   *  with no artifact (the operations map) can join a fixed shared
   *  room. Used verbatim — the caller is responsible for a legal
   *  IRC channel name. */
  channel?: string;
}

export interface UseIrcChannelResult {
  channel: string;
  nick: string;
  status: IrcStatus;
  /** Messages sorted oldest → newest. Includes both scrollback
   *  (history batch) and live PRIVMSGs. */
  messages: IrcMessage[];
  /** Send a message to the channel. No-op if not yet joined; the
   *  caller can disable the input on status !== "joined". */
  send: (text: string) => void;
  /** Error/disconnect reason (only set when status==='disconnected'
   *  or 'error'). */
  reason: string | null;
}

export function useIrcChannel(opts: UseIrcChannelOpts): UseIrcChannelResult {
  const { artifactId, email, channel: channelOverride } = opts;
  const nick = useMemo(() => nickFromEmail(email), [email]);
  const channel = useMemo(
    () => channelOverride || (artifactId ? channelForArtifact(artifactId) : ""),
    [channelOverride, artifactId],
  );

  const [status, setStatus] = useState<IrcStatus>("idle");
  const [messages, setMessages] = useState<IrcMessage[]>([]);
  const [reason, setReason] = useState<string | null>(null);
  const clientRef = useRef<IrcClient | null>(null);

  useEffect(() => {
    if (!channel || !nick) {
      setStatus("idle");
      setMessages([]);
      setReason(null);
      return;
    }
    // Fresh state for the new (channel, nick).
    setMessages([]);
    setReason(null);
    setStatus("connecting");
    const client = new IrcClient({
      url: buildIrcWsUrl(),
      nick,
      realname: email || nick,
    });
    clientRef.current = client;
    const off = client.on((ev: IrcEvent) => {
      if (ev.type === "connecting") setStatus("connecting");
      else if (ev.type === "connected") {
        setStatus("connected");
        client.join(channel);
      }
      else if (ev.type === "joined" && ev.channel === channel) setStatus("joined");
      else if (ev.type === "message" && ev.message.target === channel) {
        // De-dup by id (server may resend after reconnect; the batch
        // markers don't cleanly say "everything after this you've
        // already seen", so a defensive id-set is the right model).
        setMessages((prev) => {
          if (prev.some((m) => m.id === ev.message.id)) return prev;
          // Insert keeping the array sorted by timestamp ascending.
          // Most arrivals are append; only history-replay needs a
          // search.
          const last = prev[prev.length - 1];
          if (!last || ev.message.timestamp >= last.timestamp) {
            return [...prev, ev.message];
          }
          const next = prev.slice();
          let i = next.length - 1;
          while (i >= 0 && next[i].timestamp > ev.message.timestamp) i -= 1;
          next.splice(i + 1, 0, ev.message);
          return next;
        });
      }
      else if (ev.type === "disconnected") {
        setStatus("disconnected");
        setReason(ev.reason);
      }
      else if (ev.type === "error") {
        setStatus("error");
        setReason(ev.reason);
      }
    });
    client.connect();
    return () => {
      off();
      client.destroy();
      clientRef.current = null;
    };
  }, [channel, nick, email]);

  const send = (text: string) => {
    if (!channel) return;
    clientRef.current?.sendMessage(channel, text);
  };

  return { channel, nick, status, messages, send, reason };
}
