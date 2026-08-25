/**
 * `crypto.randomUUID()` is gated to "secure contexts" — HTTPS or
 * `localhost`. Spark-dev served via Tailscale (`http://100.127.2.93:8080/`)
 * is neither, so even when the property exists the call throws. This
 * wrapper falls back to a Math.random-based pseudo-UUID when crypto is
 * unavailable or unusable. Format is RFC 4122 v4-shaped: 8-4-4-4-12 hex.
 */
export function uuid(): string {
  try {
    const c: any = (globalThis as any)?.crypto;
    if (c && typeof c.randomUUID === "function") {
      return c.randomUUID();
    }
  } catch {
    /* fall through to pseudo-uuid */
  }
  // Math.random is not cryptographically strong; we only need session
  // identifiers / form keys here, not security tokens.
  const rnd = (n: number) =>
    [...Array(n)].map(() => Math.floor(Math.random() * 16).toString(16)).join("");
  return `${rnd(8)}-${rnd(4)}-4${rnd(3)}-${(8 + Math.floor(Math.random() * 4)).toString(16)}${rnd(3)}-${rnd(12)}`;
}
