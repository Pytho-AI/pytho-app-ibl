/**
 * Minimal IRCv3-over-WebSocket client for the LiveEditor shared
 * analyst chat. Talks to the Ergo daemon via the nginx /chat/socket
 * proxy. NO external dependency — IRC's wire format is small enough
 * to handle in-house, and pulling in irc-framework (~100 KB minified)
 * for the handful of commands we actually use is wasteful.
 *
 * Wire shape (per RFC 1459 / IRCv3 spec):
 *
 *   [@tags] [:prefix] COMMAND param1 param2 ... :trailing-with-spaces\r\n
 *
 * For our use we send: CAP LS, CAP REQ, CAP END, NICK, USER, JOIN,
 * PRIVMSG, PING/PONG. We receive: numerics (001 = welcome, 433 = nick
 * collision, 461 = need more params, etc.), JOIN, PART, PRIVMSG,
 * NOTICE, NICK, PING, batches (chathistory replay).
 *
 * Scrollback: Ergo's `autoreplay-on-join: 100` pushes the last 100
 * PRIVMSGs immediately after JOIN, wrapped in a `BATCH +<id>
 * chathistory <target>` batch. We treat batched and live PRIVMSGs
 * identically — both are messages to render. The batch markers are
 * stripped.
 */

import { clientWarn } from "./clientLog";

export interface IrcMessage {
  /** Server-issued message-id (msgid IRCv3 cap). Stable across
   *  reconnects; used as the React key. Falls back to a synthesized
   *  client id when the server hasn't tagged the frame. */
  id: string;
  /** Sender's nick (the part before "!" in the prefix). Empty for
   *  server-originated notices. */
  nick: string;
  /** Channel the message landed on. */
  target: string;
  /** Message body. */
  text: string;
  /** Server timestamp from the `time` IRCv3 tag (ISO 8601) — set by
   *  Ergo on every PRIVMSG. Falls back to client-receive wall-clock
   *  when absent. */
  timestamp: number;
  /** True if this PRIVMSG arrived inside a `chathistory` batch (i.e.
   *  it's scrollback replay, not a live message). UI uses this to
   *  decide whether to scroll-to-bottom + show a notification. */
  isHistory: boolean;
}

export type IrcEvent =
  | { type: "connecting" }
  | { type: "connected"; nick: string }
  | { type: "joined"; channel: string }
  | { type: "message"; message: IrcMessage }
  | { type: "nick_changed"; oldNick: string; newNick: string }
  | { type: "error"; reason: string }
  | { type: "disconnected"; reason: string };

export type IrcListener = (ev: IrcEvent) => void;

interface ParsedLine {
  tags: Record<string, string>;
  prefix: string;
  command: string;
  params: string[];
}

// IRCv3 CAP REQ is atomic — the server ACKs or NAKs the WHOLE batch.
// `chathistory` isn't a CAP name (it's a command Ergo enables once
// message-tags is acked); requesting it caused Ergo to NAK the whole
// batch, which silently disabled echo-message — so the sender never
// saw their own PRIVMSGs (Ergo broadcasts to channel members EXCEPT
// the sender unless echo-message is active). With one analyst alone
// in a channel, that looks like "messages never went through."
// Drop chathistory from the REQ; keep the others.
const CAP_REQUESTED = [
  "message-tags",       // server-stamped time + msgid
  "server-time",        // ISO time tag
  "echo-message",       // server echoes our own PRIVMSGs back to us
  "batch",              // chathistory replay arrives as a BATCH
];

function parseLine(line: string): ParsedLine | null {
  if (!line) return null;
  let rest = line;
  const tags: Record<string, string> = {};
  if (rest.startsWith("@")) {
    const sp = rest.indexOf(" ");
    if (sp < 0) return null;
    const tagBlob = rest.slice(1, sp);
    rest = rest.slice(sp + 1);
    for (const pair of tagBlob.split(";")) {
      const eq = pair.indexOf("=");
      if (eq < 0) tags[pair] = "";
      else tags[pair.slice(0, eq)] = pair.slice(eq + 1)
        .replace(/\\:/g, ";").replace(/\\s/g, " ")
        .replace(/\\\\/g, "\\").replace(/\\r/g, "\r")
        .replace(/\\n/g, "\n");
    }
  }
  let prefix = "";
  if (rest.startsWith(":")) {
    const sp = rest.indexOf(" ");
    if (sp < 0) return null;
    prefix = rest.slice(1, sp);
    rest = rest.slice(sp + 1);
  }
  const params: string[] = [];
  while (rest.length) {
    if (rest.startsWith(":")) { params.push(rest.slice(1)); break; }
    const sp = rest.indexOf(" ");
    if (sp < 0) { params.push(rest); break; }
    params.push(rest.slice(0, sp));
    rest = rest.slice(sp + 1);
  }
  const command = params.shift() || "";
  return { tags, prefix, command, params };
}

/** Pull the nick out of "nick!user@host" or return "" if the prefix
 *  is a server name (no '!'). */
function nickFromPrefix(prefix: string): string {
  const bang = prefix.indexOf("!");
  return bang < 0 ? "" : prefix.slice(0, bang);
}

/** Sanitize an email-prefix into an IRC-legal nick. Allowed chars:
 *  letters, digits, `-` `_` `[` `]` `\\` `` ` `` `^` `{` `}` per RFC 2812;
 *  anything else (e.g. `.` `+`) is replaced with `_`. Capped at 30
 *  chars to fit Ergo's `nicklen: 32` with room for a uniquifier
 *  suffix. */
export function nickFromEmail(email: string, fallback = "analyst"): string {
  const prefix = (email || "").split("@")[0] || fallback;
  const safe = prefix.replace(/[^A-Za-z0-9_\-\[\]\\`\^{}]/g, "_");
  return (safe || fallback).slice(0, 30);
}

/** Sanitize an artifact_id into an IRC-legal channel-name slug. Used
 *  on top of a "#doc-" prefix. Channel names allow most punctuation
 *  but we keep it strict to avoid surprises in clients that follow
 *  the stricter RFC 1459 rules. */
export function channelForArtifact(artifactId: string): string {
  const slug = (artifactId || "unknown").replace(/[^A-Za-z0-9_\-]/g, "_");
  return `#doc-${slug}`.slice(0, 64);
}

export interface IrcClientOpts {
  /** WebSocket URL, e.g. "wss://host:8080/chat/socket" — built by
   *  the caller using window.location.protocol/host so the same
   *  binary works for both spark-dev:8080 and the AWS proxy. */
  url: string;
  /** Desired nick. Collisions are resolved by appending `_2` `_3`
   *  etc. — never throws ERR_NICKNAMEINUSE up the stack. */
  nick: string;
  /** Display name carried as the IRC `realname` field. Pytho passes
   *  the analyst's full email so a `/whois` shows it. */
  realname?: string;
}

export class IrcClient {
  private ws: WebSocket | null = null;
  private listeners = new Set<IrcListener>();
  private nick: string;
  private opts: IrcClientOpts;
  /** Channels we've JOIN-ed (or are about to). Re-joined on
   *  reconnect so the analyst doesn't have to re-mount the doc. */
  private channels = new Set<string>();
  /** BATCH frames currently in flight, keyed by batch-id. Tracks
   *  which batches are `chathistory` so PRIVMSGs inside them get
   *  isHistory=true. */
  private historyBatches = new Set<string>();
  private nickCollisionSuffix = 1;
  private destroyed = false;
  private reconnectTimer: number | null = null;
  private reconnectAttempt = 0;

  constructor(opts: IrcClientOpts) {
    this.opts = opts;
    this.nick = opts.nick;
  }

  on(fn: IrcListener): () => void {
    this.listeners.add(fn);
    return () => { this.listeners.delete(fn); };
  }

  private emit(ev: IrcEvent) {
    for (const fn of this.listeners) {
      try { fn(ev); } catch (e) { clientWarn("IrcClient.listener", e); }
    }
  }

  connect() {
    if (this.destroyed) return;
    this.emit({ type: "connecting" });
    let ws: WebSocket;
    try { ws = new WebSocket(this.opts.url); }
    catch (e: any) {
      this.scheduleReconnect(`ctor: ${e?.message || e}`);
      return;
    }
    this.ws = ws;
    ws.onopen = () => {
      // CAP negotiation first so server-time + msgid + batch tags
      // arrive on the very first messages.
      this.send(`CAP LS 302`);
      this.send(`CAP REQ :${CAP_REQUESTED.join(" ")}`);
      this.send(`CAP END`);
      this.send(`NICK ${this.nick}`);
      this.send(`USER ${this.nick} 0 * :${this.opts.realname || this.nick}`);
    };
    ws.onmessage = (evt: MessageEvent) => {
      const data = String(evt.data || "");
      for (const raw of data.split(/\r?\n/)) {
        if (!raw) continue;
        this.handleLine(raw);
      }
    };
    ws.onclose = (evt) => {
      this.ws = null;
      const reason = `closed code=${evt.code}${evt.reason ? " " + evt.reason : ""}`;
      this.emit({ type: "disconnected", reason });
      this.scheduleReconnect(reason);
    };
    ws.onerror = (evt) => {
      // onerror fires before onclose; defer the reconnect to onclose
      // so we don't double-schedule.
      clientWarn("IrcClient.ws_error", evt);
    };
  }

  private scheduleReconnect(reason: string) {
    if (this.destroyed) return;
    if (this.reconnectTimer != null) return;
    this.reconnectAttempt = Math.min(this.reconnectAttempt + 1, 6);
    const backoffMs = Math.min(30_000, 500 * (2 ** this.reconnectAttempt));
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, backoffMs);
  }

  destroy() {
    this.destroyed = true;
    if (this.reconnectTimer != null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    try { this.ws?.close(1000, "client destroy"); } catch { /* ignore */ }
    this.ws = null;
    this.listeners.clear();
  }

  private send(line: string) {
    const ws = this.ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(line + "\r\n"); }
    catch (e) { clientWarn("IrcClient.send", e, { line }); }
  }

  /** Send a PRIVMSG to a channel. Splits on `\n` so multi-line text
   *  becomes multiple IRC frames (IRC has no native newline). With
   *  the echo-message CAP active the server echoes the frame back to
   *  us tagged with a real msgid + server-time, which the message
   *  handler renders identically to any other channel-member's
   *  PRIVMSG — no optimistic local-echo path needed. */
  sendMessage(channel: string, text: string) {
    const trimmed = text.trim();
    if (!trimmed) return;
    for (const line of trimmed.split(/\r?\n/)) {
      if (line) this.send(`PRIVMSG ${channel} :${line}`);
    }
  }

  /** Join (or re-join) a channel. Idempotent; safe to call on every
   *  mount of the consumer hook. */
  join(channel: string) {
    if (!channel) return;
    this.channels.add(channel);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send(`JOIN ${channel}`);
    }
  }

  /** Part a channel (used when the analyst closes the doc editor). */
  part(channel: string) {
    if (!channel) return;
    this.channels.delete(channel);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send(`PART ${channel}`);
    }
  }

  private handleLine(raw: string) {
    const parsed = parseLine(raw);
    if (!parsed) return;
    const { tags, prefix, command, params } = parsed;

    if (command === "PING") {
      // Keep-alive — Ergo pings periodically. Respond verbatim.
      this.send(`PONG :${params[0] || ""}`);
      return;
    }

    if (command === "BATCH") {
      // +<id> chathistory <target>  → starting a history replay
      // -<id>                       → ending it
      const ref = params[0] || "";
      if (ref.startsWith("+")) {
        const batchType = params[1] || "";
        if (batchType === "chathistory") this.historyBatches.add(ref.slice(1));
      } else if (ref.startsWith("-")) {
        this.historyBatches.delete(ref.slice(1));
      }
      return;
    }

    if (command === "PRIVMSG") {
      const target = params[0] || "";
      const text = params[1] || "";
      const nick = nickFromPrefix(prefix);
      const batchTag = tags["batch"] || "";
      const isHistory = batchTag !== "" && this.historyBatches.has(batchTag);
      const timeTag = tags["time"];
      const timestamp = timeTag ? Date.parse(timeTag) || Date.now() : Date.now();
      const id = tags["msgid"] || `${timestamp}:${nick}:${Math.random().toString(36).slice(2, 8)}`;
      this.emit({
        type: "message",
        message: { id, nick, target, text, timestamp, isHistory },
      });
      return;
    }

    if (command === "JOIN") {
      // Server confirms our own JOIN, or someone else joined.
      const target = params[0] || "";
      const joiner = nickFromPrefix(prefix);
      if (joiner === this.nick) this.emit({ type: "joined", channel: target });
      return;
    }

    if (command === "NICK") {
      // Server forced a nick change (rare under Ergo with our config)
      // or we changed via /nick.
      const newNick = params[0] || "";
      const oldNick = nickFromPrefix(prefix);
      if (oldNick === this.nick) this.nick = newNick;
      this.emit({ type: "nick_changed", oldNick, newNick });
      return;
    }

    if (command === "001") {
      // RPL_WELCOME — registration complete; safe to JOIN now.
      // The server may have rewritten our nick (case-folded or
      // collision-resolved) — params[0] is authoritative.
      const serverNick = params[0] || this.nick;
      this.nick = serverNick;
      this.reconnectAttempt = 0;
      this.emit({ type: "connected", nick: this.nick });
      for (const ch of this.channels) this.send(`JOIN ${ch}`);
      return;
    }

    if (command === "433") {
      // ERR_NICKNAMEINUSE — collision. Append `_N` and retry.
      this.nickCollisionSuffix += 1;
      const base = this.opts.nick.slice(0, 26);
      this.nick = `${base}_${this.nickCollisionSuffix}`;
      this.send(`NICK ${this.nick}`);
      return;
    }

    if (command === "ERROR") {
      this.emit({ type: "error", reason: params.join(" ") });
      return;
    }
    // Other numerics + commands are accepted but not surfaced; the
    // UI doesn't need 002 / 003 / 005 / 251 etc. for the chat use
    // case. Add cases here as needs grow.
  }

  /** The current effective nick (after collision resolution). */
  currentNick(): string { return this.nick; }
}

/** Build the WS URL for the surrounding page. Same host as the SPA;
 *  scheme upgrades to wss when the page is HTTPS. Path is the nginx
 *  /chat/socket location block. */
export function buildIrcWsUrl(): string {
  const loc = window.location;
  const scheme = loc.protocol === "https:" ? "wss:" : "ws:";
  return `${scheme}//${loc.host}/chat/socket`;
}
