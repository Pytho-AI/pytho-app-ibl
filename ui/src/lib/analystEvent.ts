// Analyst-action telemetry → POST /api/analyst_event.
//
// Part of the analyst data-collection plan: capture where analysts hit
// friction so we can improve the product. STRICTLY fire-and-forget — this
// must NEVER throw, block, or otherwise affect the analyst's UX. A telemetry
// failure is silent.
//
// Relative URL keeps it protocol-agnostic (works under the air-gapped HTTPS
// deployment and plain-HTTP dev alike).

export interface AnalystEventInput {
  event_type: string;
  user_id?: string | null;
  surface?: string | null;
  product_type?: string | null;
  session_id?: string | null;
  artifact_id?: string | null;
  value_num?: number | null;
  detail?: Record<string, unknown> | null;
}

export function logAnalystEvent(evt: AnalystEventInput): void {
  try {
    const body = JSON.stringify(evt);
    // sendBeacon survives page unload — important for abandonment / navigation
    // events. Falls back to a keepalive fetch when unavailable.
    if (
      typeof navigator !== "undefined" &&
      typeof navigator.sendBeacon === "function"
    ) {
      const ok = navigator.sendBeacon(
        "/api/analyst_event",
        new Blob([body], { type: "application/json" }),
      );
      if (ok) return;
    }
    void fetch("/api/analyst_event", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
      keepalive: true,
    }).catch(() => {
      /* swallow — telemetry never affects UX */
    });
  } catch {
    /* swallow — telemetry never affects UX */
  }
}
