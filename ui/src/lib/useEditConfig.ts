/**
 * useEditConfig — SPA boot-time feature-flag fetch.
 *
 * One module-cached fetch of /api/config that every consumer reads. The
 * shape is currently:
 *   { collabora_narrative_edit: boolean }
 * The hook returns `null` while the fetch is in flight so consumers can
 * branch on loading vs ready vs error without flicker.
 *
 * Add flags as they land — the backend endpoint returns them all in one
 * payload so the cache doesn't fragment per flag.
 */
import { useEffect, useState } from "react";

export interface EditConfig {
  collabora_narrative_edit: boolean;
}

let _cache: Promise<EditConfig> | null = null;

function fetchConfig(): Promise<EditConfig> {
  if (_cache) return _cache;
  _cache = fetch("/api/config").then((r) => {
    if (!r.ok) throw new Error(`/api/config returned ${r.status}`);
    return r.json();
  }).catch((e) => {
    // On error, clear the cache so a retry doesn't return the rejected
    // promise forever; surface a conservative default.
    _cache = null;
    console.warn("useEditConfig: /api/config failed, defaulting to OFF", e);
    return { collabora_narrative_edit: false } as EditConfig;
  });
  return _cache;
}

export function useEditConfig(): EditConfig | null {
  const [config, setConfig] = useState<EditConfig | null>(null);
  useEffect(() => {
    let cancelled = false;
    fetchConfig().then((c) => { if (!cancelled) setConfig(c); });
    return () => { cancelled = true; };
  }, []);
  return config;
}

export default useEditConfig;
