/**
 * LiveEditor — Office iframe for XLSX / DOCX / PPTX artifacts.
 *
 * Format-agnostic: the WOPI token mint resolves the artifact's format
 * from its schema (output_formats[0] || output_format) and returns the
 * right Content-Type on GetFile, so LibreOffice dispatches to Calc /
 * Writer / Impress without any per-format branching here.
 *
 * No chrome of its own — the parent (DocEditorPage) owns the top
 * edit-chrome strip (download / regenerate / dirty indicator /
 * fullscreen toggle / map dropdown). This component is just the iframe
 * + the map-button overlay system.
 *
 * Lifecycle:
 *   1. Mount → POST /api/wopi/token with {user_id, artifact_id} to mint
 *      a per-user WOPI access token. Different user_id values → distinct
 *      collab sessions → per-user cursors. If the mint returns 403 (the
 *      narrative-edit flag is off and this is a DOCX/PPTX), bubble
 *      `onUnsupported(format)` so the parent can fall back to the
 *      legacy TinyMCE DocEditor for DOCX/PPTX (or surface an error for
 *      XLSX since XLSX is unconditionally LiveEditor).
 *   2. Build iframe URL: /editor/browser/dist/cool.html?WOPISrc=...&access_token=...
 *      proxy-app nginx exposes /browser/, /hosting/, /cool/, /coolws/
 *      directly to editor:9980 (root paths, no net.service_root prefix).
 *   3. PostMessage listener tracks Doc_ModifiedStatus / Action_Save_Resp
 *      / App_LoadingStatus for the parent's dirty-tracking + map-overlay
 *      attach. Exposes an imperative handle to the parent so the chrome's
 *      Download / Regenerate buttons can `Action_Save` first.
 *
 * Map button overlay (PPTX-only for v1):
 *   When `<artifact_dir>/map_embeds.json` carries `twips` per entry, we
 *   reach into `iframe.contentWindow.app.map` (same-origin) to translate
 *   each twips bbox → viewport pixels via the verified internal helpers
 *   _twipsToLatLng + latLngToContainerPoint. Each map gets a "Edit Map"
 *   button overlaid at its bottom-right corner in the PARENT page,
 *   repositioned on `move / zoomend / viewreset / sliderefocus`.
 *
 *   DOCX map_embeds entries omit `twips` (convert.js doesn't expose
 *   per-figure position); for those we render no overlays — the parent
 *   chrome surfaces a "Maps" dropdown instead.
 */
import { forwardRef, useEffect, useImperativeHandle, useMemo, useRef, useState } from "react";
import { logAnalystEvent } from "../lib/analystEvent";

type WopiTokenResponse = {
  token: string;
  wopi_src: string;
  exp: number;
  format: "xlsx" | "docx" | "pptx";
};

export type FrameDirtyState = "clean" | "dirty" | "saving" | "saved" | "error";

export type MapEmbed = {
  map_id: string;
  label: string;
  /** PPTX: 0-based slide index. DOCX: page index resolved at runtime
   *  from the UNO probe (initially null in the sidecar). */
  slide_index?: number | null;
  page_index?: number | null;
  /** PPTX: [x, y, w, h] in twips at write time. DOCX: null in the
   *  sidecar — filled in at runtime by the CallPythonScript probe
   *  (pytho_graphics.get_graphic_objects) joined by alt_text. */
  twips?: [number, number, number, number] | null;
  bbox_lonlat?: [number, number, number, number] | null;
  image_part?: string;
  image_filename?: string;
  artifact_id: string;
  /** DOCX-only: alt-text join key (e.g. "pytho-map:<map_id>") set on
   *  the embedded image by convert.js. Returned as the graphic's
   *  Title/Description by the UNO script, used to pair sidecar entries
   *  with runtime-discovered graphics. */
  alt_text?: string;
};

export interface LiveEditorHandle {
  /** Post Action_Save to the iframe, resolve when Action_Save_Resp arrives. */
  save(): Promise<{ success: boolean }>;
  /** Reload the iframe (worst-case fallback after a server-side file mutation). */
  reload(): void;
  /** Swap an embedded picture in the OPEN document via UNO, without
   *  forcing an iframe reload. Match order:
   *    1. shape.Name == `pytho-map:<mapId>` (new artifacts have this
   *       stamped by the exporter)
   *    2. First Picture shape on `slideIndex` (fallback for older
   *       artifacts whose pictures don't carry the join-key name)
   *  Loads from `editorImagePath` — a path on the editor container's
   *  /uploads volume. Resolves when Collabora posts back
   *  CallPythonScript-Result, or after a fallback timeout. */
  replacePicture(mapId: string, editorImagePath: string,
                 slideIndex?: number | null): Promise<{ status: string }>;
  /** Toggle Collabora's internal UI mode — fullscreen UX flips to
   *  notebookbar (ribbon). The "compact" string is an alias for
   *  "classic" since that's what bundle.js's onChangeUIMode actually
   *  accepts; we keep "compact" in the API surface for symmetry with
   *  how Collabora documents the two-state UX (compact toolbar vs.
   *  notebookbar). */
  setUiMode(mode: "compact" | "notebookbar" | "classic"): void;
}

interface LiveEditorProps {
  artifactId: string;
  userId: string;
  userFriendlyName?: string;
  productName?: string;
  /** Called whenever the dirty state changes (drives chrome's indicator). */
  onDirtyChange?: (state: FrameDirtyState) => void;
  /** Called when an Edit Map button is clicked. Parent opens the
   *  GeoAgentMap overlay. On Done, parent POSTs /refresh_map and calls
   *  `handle.reload()` to re-attach the freshly-replaced image. */
  onEditMap?: (embed: MapEmbed) => void;
  /** Called when the token mint comes back 403 (narrative-edit disabled).
   *  Parent should swap to the TinyMCE fallback for DOCX/PPTX. */
  onUnsupported?: (reason: string) => void;
  /** Called when the embeds sidecar lands so parent can populate the
   *  chrome's "Maps" dropdown (used for DOCX where overlays aren't
   *  feasible). */
  onMapEmbedsLoaded?: (embeds: MapEmbed[]) => void;
  /** When true, mount the Collabora iframe with permission=readonly so
   *  no edits are accepted. Flipping this re-mounts the iframe with the
   *  new permission (Collabora picks up the mode at load time, not
   *  via postMessage). Used by the Draft|Final toggle: published
   *  artifacts open read-only; reverting to draft re-opens editable. */
  readOnly?: boolean;
  /** Fires once per iframe mount when Collabora signals
   *  App_LoadingStatus → Document_Loaded. The parent uses this to
   *  delay mounting of side panels (e.g. the per-doc IRC chat)
   *  until the doc is actually painted, avoiding the "INTEL AGENT"
   *  drawer flashing through the brief loading window. */
  onEditorReady?: () => void;
}

function buildIframeUrl(wopiSrc: string, token: string,
                          opts: { userFriendlyName?: string; userId: string; lang?: string; readOnly?: boolean }): string {
  const params = new URLSearchParams();
  params.set("WOPISrc", wopiSrc);
  params.set("access_token", token);
  params.set("access_token_ttl", String(Date.now() + 6 * 24 * 3600 * 1000));
  params.set("lang", opts.lang || "en-US");
  // The preference the modern Collabora bundle reads at startup is
  // `compactMode` (boolean), NOT `UIMode`. From browser/js/global.js:
  //   const forceCompact = window.prefs.get('compactMode', null);
  //   const shouldUseClassic = forceCompact === 'true' || ...;
  //   return !shouldUseClassic;     // notebookbar (ribbon) if not classic
  // compactMode=true picks the single-toolbar layout for Writer/Impress/
  // Calc uniformly. We also write the same key to localStorage via
  // patch-editor.sh's global.js injection so any persisted compactMode=
  // false from a prior fullscreen session gets overridden on initial
  // load — windowed mode is always compact. Fullscreen toggle flips to
  // notebookbar at runtime by postMessage'ing Action_ChangeUIMode
  // {Mode:"notebookbar"}; the setUiMode handle below does that.
  params.set("ui_defaults",
    "compactMode=true;TextRuler=false;PresentationStatusbar=false");
  params.set("closebutton", "0");
  // Read-only mode for FINAL/published artifacts. Collabora honors
  // `permission` at iframe-load time — postMessage cannot flip an
  // already-loaded session between edit and readonly, so toggling the
  // approval state re-mounts the iframe with the new permission via
  // the iframeUrl useMemo dependency below.
  params.set("permission", opts.readOnly ? "readonly" : "edit");
  // Collabora served under /editor/ prefix. Matches main's cloud+staging
  // nginx pattern (location /editor/browser/ + rewrite to strip /editor).
  return `/editor/browser/dist/cool.html?${params.toString()}`;
}

const LiveEditor = forwardRef<LiveEditorHandle, LiveEditorProps>(function LiveEditor(
  { artifactId, userId, userFriendlyName, productName, onDirtyChange,
    onEditMap, onUnsupported, onMapEmbedsLoaded, readOnly, onEditorReady },
  ref,
) {
  const [tokenState, setTokenState] = useState<WopiTokenResponse | null>(null);
  const [error, setError] = useState<string>("");
  const iframeRef = useRef<HTMLIFrameElement | null>(null);
  // Bumped by reload() to force a fresh <iframe> element (via the `key`
  // below) instead of re-setting src on the live one. Re-navigating the
  // existing iframe pushes a browser history entry whose top URL is still
  // /docs/:id, so Back walks back through stale Collabora states instead of
  // leaving the editor. A fresh element's first load replaces its initial
  // about:blank — same teardown+reload, no history pollution.
  const [reloadNonce, setReloadNonce] = useState(0);
  const dirtyStateRef = useRef<FrameDirtyState>("clean");
  const saveResolversRef = useRef<Array<(v: { success: boolean }) => void>>([]);
  // Pending CallPythonScript invocations, keyed by Function name. Each
  // entry is the resolver that fires when CallPythonScript-Result lands.
  // Today only one inflight at a time per function, so a single-slot
  // map is enough; switch to FIFO list if we add concurrent UNO calls.
  const scriptResolversRef = useRef<
    Map<string, (v: { status: string }) => void>
  >(new Map());
  // Callback refs — the parent passes fresh inline arrows on every
  // render, so listing the callbacks in useEffect deps re-fires the
  // effects every render. With the mint effect that meant: render →
  // token mint → setTokenState → iframeUrl recomputes → iframe re-
  // mounts → parent re-renders → mint again → repeat (~1/sec). The
  // analyst saw cool.html + /api/wopi/token + /map_embeds firing in a
  // continuous loop while the wss handshake to /cool/ was interrupted
  // mid-load each time. Stashing the callbacks in refs lets the
  // effects key off only the IDs they truly depend on; the latest
  // callback is always reachable via .current. Updated by the second
  // effect below on every render with zero cost.
  const onUnsupportedRef = useRef<typeof onUnsupported>(onUnsupported);
  const onMapEmbedsLoadedRef = useRef<typeof onMapEmbedsLoaded>(onMapEmbedsLoaded);
  const onEditorReadyRef = useRef<typeof onEditorReady>(onEditorReady);
  // Fires onEditorReady exactly once per mount of this artifact id;
  // re-arms when the artifactId changes (e.g. analyst switches docs).
  const editorReadyFiredRef = useRef<string>("");
  useEffect(() => {
    onUnsupportedRef.current = onUnsupported;
    onMapEmbedsLoadedRef.current = onMapEmbedsLoaded;
    onEditorReadyRef.current = onEditorReady;
  });
  useEffect(() => {
    editorReadyFiredRef.current = "";
  }, [artifactId]);
  const [mapEmbeds, setMapEmbeds] = useState<MapEmbed[]>([]);
  // Runtime UNO probe result for DOCX (and any other format where the
  // sidecar carries alt_text but no twips). Joined onto mapEmbeds by
  // alt_text === Title to produce overlay-eligible entries with twips.
  const [runtimeGraphics, setRuntimeGraphics] = useState<Array<{
    name: string; title: string; description: string;
    x_twips: number; y_twips: number; w_twips: number; h_twips: number;
    page_index?: number | null;
  }>>([]);
  const [overlayHost, setOverlayHost] = useState<HTMLDivElement | null>(null);

  // Mint
  useEffect(() => {
    if (!artifactId || !userId) {
      setTokenState(null);
      return;
    }
    let cancelled = false;
    (async () => {
      setError("");
      try {
        const res = await fetch("/api/wopi/token", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            user_id: userId,
            artifact_id: artifactId,
            user_friendly_name: userFriendlyName || userId,
          }),
        });
        if (res.status === 403) {
          const body = await res.json().catch(() => ({}));
          if (!cancelled) {
            onUnsupportedRef.current?.(body?.detail || "live edit disabled for this format");
            setTokenState(null);
          }
          return;
        }
        if (!res.ok) {
          const body = await res.text().catch(() => "");
          throw new Error(`token mint failed (${res.status}): ${body || res.statusText}`);
        }
        const payload: WopiTokenResponse = await res.json();
        if (!cancelled) setTokenState(payload);
      } catch (e: any) {
        if (!cancelled) {
          setError(e?.message || String(e));
          setTokenState(null);
        }
      }
    })();
    return () => { cancelled = true; };
  }, [artifactId, userId, userFriendlyName]);

  // Fetch map embeds sidecar (always — empty list when artifact has no maps)
  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`/api/artifacts/${encodeURIComponent(artifactId)}/map_embeds`);
        if (!r.ok) return;
        const body = await r.json();
        if (cancelled) return;
        const embeds: MapEmbed[] = Array.isArray(body?.map_embeds) ? body.map_embeds : [];
        setMapEmbeds(embeds);
        onMapEmbedsLoadedRef.current?.(embeds);
      } catch {
        /* sidecar absent → no overlays, no chrome dropdown */
      }
    })();
    return () => { cancelled = true; };
  }, [artifactId]);

  const iframeUrl = useMemo(() => {
    if (!tokenState) return "";
    return buildIframeUrl(tokenState.wopi_src, tokenState.token,
                            { userFriendlyName, userId, readOnly });
  }, [tokenState, userFriendlyName, userId, readOnly]);

  // PostMessage handler — dirty state, save acks, Document_Loaded,
  // CallPythonScript-Result for the runtime graphics probe.
  useEffect(() => {
    function handler(ev: MessageEvent) {
      const frame = iframeRef.current;
      if (!frame || ev.source !== frame.contentWindow) return;
      let data: any = ev.data;
      if (typeof data === "string") {
        try { data = JSON.parse(data); } catch { return; }
      }
      if (!data || typeof data !== "object") return;
      const msgId = data.MessageId || data.msgId;
      const values = data.Values || data.args || {};
      // Per-edit telemetry: forward EVERY editor message to
      // /api/analyst_event with event_type='editor.<msgId>' so the
      // dashboard can play back every keystroke-grain action. Skips
      // the host→guest handshake messages (PostmessageReady) and the
      // already-instrumented save lifecycle (handled below + on the
      // WOPI server). Defensive try/catch — telemetry never breaks
      // the editor flow.
      try {
        if (
          msgId &&
          msgId !== "Host_PostmessageReady" &&
          msgId !== "CallPythonScript-Result"
        ) {
          logAnalystEvent({
            event_type: `editor.${msgId}`,
            surface: "editor",
            artifact_id: artifactId,
            detail: {
              msg_id: msgId,
              values,
              ts: Date.now(),
            },
          });
        }
      } catch { /* swallow */ }
      let next: FrameDirtyState | null = null;
      if (msgId === "Doc_ModifiedStatus") {
        next = values.Modified ? "dirty" : "clean";
      } else if (msgId === "App_LoadingStatus") {
        if (values.Status === "Frame_Ready" || values.Status === "Document_Loaded") {
          next = "clean";
          // Collabora gates EVERY inbound host postMessage on
          // window.WOPIPostmessageReady which is only set when the
          // editor receives Host_PostmessageReady. Without this, every
          // subsequent postMessage (CallPythonScript, Action_Save,
          // Action_ChangeUIMode, etc.) is silently dropped with
          // "PostMessage ignored: not ready." logged inside the iframe.
          // Send on every loading-status event — idempotent on the
          // editor side, and survives any race between iframe ready and
          // our handler attach.
          try {
            frame.contentWindow?.postMessage(JSON.stringify({
              MessageId: "Host_PostmessageReady",
              Values: {},
            }), "*");
          } catch { /* ignore */ }
          if (values.Status === "Document_Loaded") {
            // Fire onEditorReady exactly once per artifactId mount.
            // Parent uses it to delay side-panel UI (the per-doc IRC
            // chat) until the doc is actually painted, avoiding the
            // brief drawer flash through the loading window.
            if (editorReadyFiredRef.current !== artifactId) {
              editorReadyFiredRef.current = artifactId;
              try { onEditorReadyRef.current?.(); } catch { /* ignore */ }
            }
            // Impress's native _fitWidthZoom (restored in
            // patch-editor.sh) handles the slide-to-viewport fit on its
            // own; no host-side dispatch needed. The navigation sidebar
            // (slide thumbnail rail) stays visible so the analyst can
            // switch slides.
            if (!readOnly) {
              // The doc model is now alive — graphics are placed +
              // their anchor/position is queryable. Fire the UNO
              // probe. Skipped in read-only: the script's
              // controller.select(g) path is rejected by Writer's
              // readonly view AND we render no overlay buttons in
              // read-only anyway, so the result would be discarded.
              try {
                frame.contentWindow?.postMessage(JSON.stringify({
                  MessageId: "CallPythonScript",
                  SendTime: Date.now(),
                  ScriptFile: "pytho_graphics.py",
                  Function:   "get_graphic_objects",
                  Values: {},
                }), "*");
              } catch { /* ignore */ }
            }
          }
        }
      } else if (msgId === "Action_Save_Resp") {
        next = values.success === false ? "error" : "saved";
        const resolvers = saveResolversRef.current.splice(0);
        for (const r of resolvers) r({ success: values.success !== false });
      } else if (msgId === "CallPythonScript-Result") {
        // Script returned `json.dumps({...})`. The wrapping wire shape
        // varies by Collabora version — Values/args may carry the result
        // under different keys. Be defensive about both.
        const raw = values?.result?.value
                 ?? values?.commandValues?.result?.value
                 ?? values?.result
                 ?? values?.commandValues
                 ?? "";
        const text = typeof raw === "string" ? raw : "";
        if (text) {
          try {
            const parsed = JSON.parse(text);
            // get_graphic_objects result — populate the alt-text join
            // table for the Edit Map button overlay.
            if (Array.isArray(parsed?.graphics)) {
              setRuntimeGraphics(parsed.graphics);
            }
            // replace_picture result — resolve the pending promise so
            // onEditMapDone can complete without firing a reload.
            if (typeof parsed?.status === "string") {
              const resolver = scriptResolversRef.current.get("replace_picture");
              if (resolver) {
                scriptResolversRef.current.delete("replace_picture");
                resolver({ status: parsed.status });
              }
            }
          } catch { /* malformed result — leave handlers untouched */ }
        }
      }
      if (next && next !== dirtyStateRef.current) {
        dirtyStateRef.current = next;
        onDirtyChange?.(next);
      }
    }
    window.addEventListener("message", handler);
    return () => window.removeEventListener("message", handler);
  }, [onDirtyChange, readOnly, artifactId]);

  // Imperative handle for the chrome
  useImperativeHandle(ref, () => ({
    async save() {
      const iframe = iframeRef.current;
      if (!iframe?.contentWindow) return { success: false };
      const promise = new Promise<{ success: boolean }>((resolve) => {
        saveResolversRef.current.push(resolve);
        // Safety timeout — 5s. If the iframe doesn't ack, assume best-effort.
        setTimeout(() => {
          const idx = saveResolversRef.current.indexOf(resolve);
          if (idx >= 0) {
            saveResolversRef.current.splice(idx, 1);
            resolve({ success: false });
          }
        }, 5000);
      });
      iframe.contentWindow.postMessage(JSON.stringify({
        MessageId: "Action_Save",
        Values: { Notify: true, ExtendedData: "" },
      }), "*");
      return promise;
    },
    reload() {
      // Remount a fresh iframe (via the key on readOnly+reloadNonce) rather
      // than swapping src on the live element. The src swap reloaded fine but
      // pushed a browser history entry — Back then returned to a stale editor
      // state instead of the dashboard. A keyed remount loads fresh and
      // replaces its initial about:blank, so no entry is added. Brief
      // "Opening…" flash either way.
      setReloadNonce((n) => n + 1);
    },
    async replacePicture(mapId, editorImagePath, slideIndex) {
      const iframe = iframeRef.current;
      if (!iframe?.contentWindow) return { status: "no_iframe" };
      const promise = new Promise<{ status: string }>((resolve) => {
        const prev = scriptResolversRef.current.get("replace_picture");
        if (prev) prev({ status: "superseded" });
        scriptResolversRef.current.set("replace_picture", resolve);
        setTimeout(() => {
          if (scriptResolversRef.current.get("replace_picture") === resolve) {
            scriptResolversRef.current.delete("replace_picture");
            resolve({ status: "timeout" });
          }
        }, 8000);
      });
      // Collabora's UNO bridge expects each Values entry to be a
      // PropertyValue struct {type, value}, NOT a plain primitive.
      // Empirically confirmed: passing plain values landed three
      // `None` positional args in the Python script. Wrap properly so
      // the value comes through as the right type.
      iframe.contentWindow.postMessage(JSON.stringify({
        MessageId: "CallPythonScript",
        SendTime: Date.now(),
        ScriptFile: "pytho_graphics.py",
        Function:   "replace_picture",
        Values: {
          map_id:      { type: "string", value: mapId },
          image_url:   { type: "string", value: editorImagePath },
          slide_index: { type: "long",
                          value: slideIndex == null ? -1 : slideIndex },
        },
      }), "*");
      return promise;
    },
    setUiMode(mode) {
      const iframe = iframeRef.current;
      if (!iframe?.contentWindow) return;
      // "compact" is an API alias for the underlying Collabora
      // "classic" UI mode (bundle.js UIManager.onChangeUIMode rejects
      // anything else). Translate before posting.
      const wireMode = mode === "compact" ? "classic" : mode;
      iframe.contentWindow.postMessage(JSON.stringify({
        MessageId: "Action_ChangeUIMode",
        Values: { Mode: wireMode },
      }), "*");
    },
  }), []);

  // Eligible-for-overlay list: every sidecar entry that has a twips
  // bbox either statically (PPTX — exporter writes them at generation)
  // or dynamically (DOCX — joined onto runtimeGraphics by alt_text =
  // graphic Title returned from the UNO probe). Entries with neither
  // are skipped (no overlay rendered, no chrome fallback).
  const eligibleEmbeds = useMemo<MapEmbed[]>(() => {
    if (mapEmbeds.length === 0) return [];
    const out: MapEmbed[] = [];
    for (const e of mapEmbeds) {
      if (Array.isArray(e.twips) && e.twips.length === 4) {
        out.push(e);
        continue;
      }
      const key = (e.alt_text || "").trim();
      if (!key) continue;
      const g = runtimeGraphics.find(
        gg => (gg.title || "").trim() === key
           || (gg.description || "").trim() === key
           || (gg.name || "").trim() === key,
      );
      if (!g) continue;
      out.push({
        ...e,
        twips: [g.x_twips, g.y_twips, g.w_twips, g.h_twips],
        page_index: g.page_index ?? null,
      });
    }
    return out;
  }, [mapEmbeds, runtimeGraphics]);

  // Map-button overlay subsystem. The button is CSS-anchored to the
  // bottom-right of the overlay host (which spans the iframe) — no
  // per-frame position calculation, no jumping. Same-origin reach into
  // iframe.contentWindow.app.map is only used to read the current slide
  // index for the slide-gate (Impress shows the button only on slides
  // that actually carry a map embed). Reposition fn just toggles
  // display: block / none per embed.
  //
  // A coarse rAF poll runs alongside the Collabora event hooks because
  // the slide-switch event our version emits is unreliable; polling the
  // current part on requestAnimationFrame catches the change within a
  // frame regardless of which event fired.
  useEffect(() => {
    if (!iframeRef.current || !overlayHost) return;
    const eligible = eligibleEmbeds;
    if (eligible.length === 0) return;

    const iframe = iframeRef.current;
    let cancelled = false;
    let unsubscribe: (() => void) | null = null;
    let raf = 0;
    let pollRaf = 0;
    let lastPart: number | undefined = undefined;

    function readCurrentPart(): number | undefined {
      const w = iframe.contentWindow as any;
      const m = w?.app?.map;
      const dl = m?._docLayer;
      if (!m || !dl) return undefined;
      return (typeof dl._selectedPart === "number" ? dl._selectedPart : undefined)
        ?? (typeof dl._currentPart === "number" ? dl._currentPart : undefined)
        ?? (typeof (m as any)._currentPart === "number" ? (m as any)._currentPart : undefined);
    }

    function applyVisibility() {
      if (cancelled) return;
      const currentPart = readCurrentPart();
      for (const embed of eligible) {
        const el = overlayHost!.querySelector<HTMLButtonElement>(
          `[data-pytho-map-id="${cssEscape(embed.map_id)}"]`,
        );
        if (!el) continue;
        // Slide gate. Three cases:
        //  1. Embed has no slide_index (Writer pages / single-canvas
        //     docs) → button always visible.
        //  2. Embed has slide_index AND we can read currentPart →
        //     show iff they match.
        //  3. Embed has slide_index AND we CAN'T read currentPart
        //     (Collabora doc-layer not initialized yet — happens
        //     during the boot "Please wait" window) → hide. We don't
        //     know which slide is showing, so don't speculatively
        //     paint the button. The rAF poll re-runs this fn every
        //     frame; the button appears the moment the doc-layer
        //     comes up on the matching slide.
        const hasSlideIdx =
          embed.slide_index !== undefined && embed.slide_index !== null;
        let visible: boolean;
        if (!hasSlideIdx) {
          visible = true;
        } else if (typeof currentPart === "number") {
          visible = embed.slide_index === currentPart;
        } else {
          visible = false;
        }
        el.style.display = visible ? "block" : "none";
      }
    }

    function schedule() {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(applyVisibility);
    }

    function pollTick() {
      if (cancelled) return;
      const p = readCurrentPart();
      if (p !== lastPart) {
        lastPart = p;
        applyVisibility();
      }
      pollRaf = requestAnimationFrame(pollTick);
    }

    function attach() {
      const w = iframe.contentWindow as any;
      const m = w?.app?.map;
      if (!m) {
        if (!cancelled) setTimeout(attach, 250);
        return;
      }
      const events = [
        "sliderefocus", "currentpartchanged", "partshashchanged",
        "updateparts", "scrollto",
      ].join(" ");
      m.on(events, schedule);
      applyVisibility();
      pollTick();
      unsubscribe = () => {
        try { m.off(events, schedule); } catch { /* ignore */ }
      };
    }

    const onLoad = () => attach();
    iframe.addEventListener("load", onLoad);
    attach();

    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      cancelAnimationFrame(pollRaf);
      iframe.removeEventListener("load", onLoad);
      if (unsubscribe) unsubscribe();
    };
  }, [eligibleEmbeds, overlayHost, iframeUrl]);

  if (error) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-center p-6 text-error-700">
        <div className="text-body-bold mb-2">Could not open {productName || artifactId}.</div>
        <div className="text-caption text-error-700 max-w-xl text-center">{error}</div>
      </div>
    );
  }
  if (!tokenState) {
    return (
      <div className="flex h-full w-full items-center justify-center">
        <div className="text-caption text-subtext-color">Opening…</div>
      </div>
    );
  }

  // Suppress Edit Map overlays in read-only mode — they POST to
  // /refresh_map which would mutate a published artifact. The Draft|Final
  // toggle is the only way back to edit mode.
  const eligible = readOnly ? [] : eligibleEmbeds;

  return (
    <div className="relative h-full w-full">
      <iframe
        // Keyed on permission + reload-nonce so a Draft↔Final flip or an
        // explicit reload() remounts a FRESH iframe element instead of
        // re-navigating the existing one. A same-element src change pushes a
        // browser history entry (top URL is still /docs/:id), so Back walks
        // back through stale Collabora states instead of leaving the editor.
        // A fresh element's first load replaces its initial about:blank — no
        // history entry. See reloadNonce note above.
        key={`${readOnly ? "ro" : "rw"}-${reloadNonce}`}
        ref={iframeRef}
        title={`Live editor for ${productName || artifactId}`}
        src={iframeUrl}
        allow="clipboard-read; clipboard-write; fullscreen"
        style={{
          width: "100%",
          height: "100%",
          border: "0",
          background: "white",
          display: "block",
        }}
      />
      {/* Overlay host: absolutely-positioned div covering the iframe. The
          per-map "Edit Map" buttons live here, repositioned on every
          Collabora viewport-change event via the effect above. */}
      <div
        ref={setOverlayHost}
        style={{
          position: "absolute", inset: 0,
          pointerEvents: "none",   // pass-through; individual buttons re-enable
        }}
      >
        {eligible.map((e) => (
          <button
            key={e.map_id}
            type="button"
            data-pytho-map-id={e.map_id}
            onClick={() => onEditMap?.(e)}
            style={{
              position: "absolute",
              bottom: 16,
              right: 16,
              width: 88,
              height: 28,
              display: "none",
              padding: "0 12px",
              background: "#d9534f",
              color: "#fff",
              fontFamily: "Arial, sans-serif",
              fontSize: 12,
              fontWeight: 600,
              border: "1px solid #c0392b",
              borderRadius: 3,
              boxShadow: "0 2px 6px rgba(0,0,0,0.35)",
              cursor: "pointer",
              pointerEvents: "auto",
              zIndex: 1000,
            }}
            title={`Edit ${e.label}`}
          >
            Edit Map
          </button>
        ))}
      </div>
    </div>
  );
});

/** Minimal CSS.escape polyfill for the attribute selector — handles the
 *  identifiers we generate (alphanumerics, dashes, underscores). */
function cssEscape(s: string): string {
  if (typeof (CSS as any)?.escape === "function") return (CSS as any).escape(s);
  return s.replace(/[^a-zA-Z0-9_-]/g, (c) => `\\${c}`);
}

export default LiveEditor;
