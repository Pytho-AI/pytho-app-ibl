/**
 * StageTimeline — collapsible per-stage latency breakdown for a single
 * product-generation session.
 *
 * Reads from `useChatContext().sessions[sessionId]`. Renders:
 *
 *   {productLabel} · {total}s · {status}
 *     TTFA: {ttfa}s
 *     ├─ tool_get_oob (RED)        2.7s    63 units
 *     ├─ tool_get_recent_intel    18.3s   241 obs
 *     ...
 *
 * One click on the header expands the row list; default is collapsed.
 *
 * TTFA = trigger → first artifact byte visible. We compute this from
 * `firstArtifactAt - startedAt` (both ChatContext-tracked wall-clocks). The
 * backend's per-stage durations come straight from the stage_event frames.
 */

import { useState } from "react";
import { useChatContext, type StageEvent } from "../lib/ChatContext";

interface StageTimelineProps {
  sessionId: string;
  /** Default open state. Defaults to false (collapsed). */
  defaultOpen?: boolean;
}

function fmtSec(ms: number | undefined | null): string {
  if (!ms || ms < 0) return "0.0s";
  const s = ms / 1000;
  if (s >= 100) return `${s.toFixed(1)}s`;
  if (s >= 10) return `${s.toFixed(1)}s`;
  return `${s.toFixed(1)}s`;
}

function summarizeExtra(extra: Record<string, any> | undefined): string {
  // The backend's `extra` field is per-stage free-form. Surface the
  // commonly-present, doctrinally-meaningful counts only — anything else
  // is dropped to keep each row to a single line.
  if (!extra || typeof extra !== "object") return "";
  const parts: string[] = [];
  if (typeof extra.rows_returned === "number") {
    parts.push(`${extra.rows_returned} rows`);
  }
  if (typeof extra.units_returned === "number") {
    parts.push(`${extra.units_returned} units`);
  }
  if (typeof extra.observations_returned === "number") {
    parts.push(`${extra.observations_returned} obs`);
  }
  if (extra.supplemented_from_fixture === true) {
    parts.push("sparse, flagged");
  }
  if (typeof extra.measures_returned === "number") {
    parts.push(`${extra.measures_returned} measures`);
  }
  // DAG-node synthesized stage_events (see ChatAgent.tsx dag_node_done
  // → appendStageEvent translation). `summary` is the per-node rollup
  // (e.g. "63 units" / "draft INTSUM-180600Z-181200Z"). `wave` indexes
  // the parallel batch. `error` surfaces when ok=false.
  if (typeof extra.summary === "string" && extra.summary) {
    parts.push(extra.summary);
  }
  if (extra.ok === false && typeof extra.error === "string" && extra.error) {
    parts.push(`failed: ${extra.error}`);
  }
  if (typeof extra.wave === "number") {
    parts.push(`wave ${extra.wave}`);
  }
  return parts.join(", ");
}

function StageRow({ ev, isLast }: { ev: StageEvent; isLast: boolean }) {
  const summary = summarizeExtra(ev.extra);
  const prefix = isLast ? "└─" : "├─";
  return (
    <div
      data-stage={ev.stage}
      style={{
        display: "grid",
        gridTemplateColumns: "16px minmax(140px, 1fr) 60px 1fr",
        gap: 6,
        fontSize: 11,
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        color: "var(--color-default-font)",
        padding: "1px 0",
      }}
    >
      <span style={{ color: "var(--color-subtext-color)" }}>{prefix}</span>
      <span>{ev.stage}</span>
      <span style={{ textAlign: "right", color: "var(--color-subtext-color)" }}>
        {fmtSec(ev.duration_ms)}
      </span>
      <span style={{ color: "var(--color-subtext-color)" }}>{summary}</span>
    </div>
  );
}

export default function StageTimeline({
  sessionId,
  defaultOpen = false,
}: StageTimelineProps) {
  const { sessions } = useChatContext();
  const [expanded, setExpanded] = useState<boolean>(defaultOpen);

  const session = sessions[sessionId];
  if (!session) return null;

  // Total: prefer the backend's authoritative total_duration_ms; otherwise
  // sum the per-stage durations (which under-counts intra-stage gaps but
  // is the best we have until turn_summary lands).
  const total =
    session.turnSummary?.total_duration_ms ??
    session.stageEvents.reduce((acc, ev) => acc + (ev.duration_ms || 0), 0);

  // TTFA — trigger to first artifact byte visible to analyst.
  const ttfa =
    session.firstArtifactAt !== null
      ? session.firstArtifactAt - session.startedAt
      : null;

  const statusColor =
    session.status === "ready"
      ? "#27ae60"
      : session.status === "error"
        ? "#b00020"
        : "#b08400";

  return (
    <div
      data-testid="stage-timeline"
      data-session-id={sessionId}
      style={{
        border: "1px solid var(--color-neutral-border)",
        borderRadius: 6,
        background: "var(--color-neutral-50)",
        padding: 8,
        margin: "6px 0",
        fontFamily: "inherit",
      }}
    >
      <div
        onClick={() => setExpanded((v) => !v)}
        style={{
          cursor: "pointer",
          display: "flex",
          alignItems: "center",
          gap: 8,
          fontSize: 12,
          fontWeight: 600,
          color: "var(--color-default-font)",
        }}
        title={expanded ? "Click to collapse" : "Click to expand"}
      >
        <span style={{ fontSize: 10, color: "var(--color-subtext-color)" }}>
          {expanded ? "▾" : "▸"}
        </span>
        <span>{session.productLabel}</span>
        <span style={{ color: "var(--color-subtext-color)", fontWeight: 400 }}>
          · {fmtSec(total)} ·
        </span>
        <span style={{ color: statusColor }}>{session.status}</span>
      </div>

      <div
        style={{
          fontSize: 11,
          color: "var(--color-subtext-color)",
          marginTop: 4,
          fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        }}
      >
        TTFA: {ttfa !== null ? fmtSec(ttfa) : "—"}
        {session.stageEvents.length > 0 && (
          <span style={{ marginLeft: 12 }}>
            {session.stageEvents.length} stage{session.stageEvents.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {expanded && (
        <div
          data-testid="stage-timeline-rows"
          style={{ marginTop: 6, display: "flex", flexDirection: "column" }}
        >
          {session.stageEvents.length === 0 ? (
            <div
              style={{
                fontSize: 11,
                color: "var(--color-subtext-color)",
                fontStyle: "italic",
              }}
            >
              waiting for first stage_event…
            </div>
          ) : (
            session.stageEvents.map((ev, idx) => (
              <StageRow
                key={`${ev.stage}-${idx}`}
                ev={ev}
                isLast={idx === session.stageEvents.length - 1}
              />
            ))
          )}
        </div>
      )}
    </div>
  );
}
