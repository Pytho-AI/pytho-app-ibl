"use client";

/**
 * DocEditorHeadless — screenshot-mode mount of the analyst DocEditor.
 *
 * Loaded by docker-api's Playwright renderer
 * (pytho-app/app/app/utils/render/preview.py) at
 *   /missioncommand/docs/render?owner=<id>&artifact_id=<id>
 * Renders the same <DocEditor> the analyst edits with — same TinyMCE
 * iframe, same content_style, same page-break chrome — so the captured
 * PNG matches what the analyst sees, not a standalone re-implementation
 * of the page layout.
 *
 * URL contract (both required):
 *   owner         user_id directory under /files/
 *   artifact_id   <PRODUCT>_<UTC_TS> id
 *
 * The route fetches /files/<owner>/<artifact_id>/artifact.html directly
 * (public — no auth), feeds it as `initialValue` into DocEditor, then
 * sets window.__previewReady once TinyMCE's iframe body height has
 * stabilized across two consecutive animation frames (proxy for "all
 * layout passes done"). Playwright waits on that latch before screenshotting.
 *
 * Intentionally NOT wrapped in AuthenticatedRoute — this route is for the
 * internal headless renderer only, reached over the cluster network from
 * docker-api. Mounted under a minimal AppContext.Provider with stub
 * user/projectId so DocEditor's `useAppContext()` doesn't dereference
 * null (the save/translate code paths that read user.userId/email never
 * fire in the headless run).
 */

import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import DocEditor from "../components/DocEditor.tsx";
import { AppContext } from "../lib/contextLib";

// Stub context — DocEditor only reads `user.userId` / `user.email` from
// save handlers and the chat WS, neither of which fire in a headless
// preview render. projectId is read but never written.
const HEADLESS_CTX = {
  user: { userId: "headless", email: "headless@pytho.ai" },
  setUser: () => {},
  projectId: "headless",
  setProjectId: () => {},
  projectMap: {},
  setProjectMap: () => {},
  notifications: [],
  setNotifications: () => {},
};

function HeadlessReadySignal({ editorMountedAt }: { editorMountedAt: number | null }) {
  const firedRef = useRef(false);

  useEffect(() => {
    if (firedRef.current) return;
    if (editorMountedAt === null) return;

    let cancelled = false;

    // TinyMCE drops its iframe under `.tox-edit-area__iframe`. Naive
    // "two consecutive rAF heights match" fires WAY too early — TinyMCE
    // queues several layout passes (font loading, image loading,
    // page-break insertion, watermark/header overlay paint) and the
    // height plateaus briefly between each pass. We need:
    //   1. The iframe must exist AND its body content must be non-empty
    //      (initialValue has reached TinyMCE).
    //   2. Body height must be stable across >= 8 consecutive rAF
    //      measurements (~130ms at 60fps), AND
    //   3. document.fonts.ready must have resolved, AND
    //   4. Every <img> inside the iframe must have loaded (or errored),
    //      so image-induced reflows don't shrink the document mid-capture.
    //   5. We've waited at least 2 seconds since first iframe sighting,
    //      to give TinyMCE's deferred page-break paint enough time.
    const start = Date.now();
    const HARD_TIMEOUT_MS = 45_000;
    const STABLE_RAF_REQUIRED = 8;
    const MIN_DWELL_MS = 2000;

    const waitForStableHeight = async () => {
      let iframeFirstSeenAt = 0;
      while (!cancelled) {
        if (Date.now() - start > HARD_TIMEOUT_MS) {
          // eslint-disable-next-line no-console
          console.warn("DocEditorHeadless: timed out waiting for iframe layout");
          (window as any).__previewReady = true;
          return;
        }
        const iframe = document.querySelector<HTMLIFrameElement>(
          ".tox-edit-area__iframe"
        );
        const doc = iframe?.contentDocument;
        const body = doc?.body;
        // Require non-trivial content: bare TinyMCE skeleton body can
        // be 100-200px before initialValue lands. Wait for real content.
        if (iframe && body && body.scrollHeight > 400 && body.children.length > 1) {
          if (!iframeFirstSeenAt) iframeFirstSeenAt = Date.now();

          // Wait for font loading (header font shifts inflate layout
          // when they swap in) and all image loads.
          if (doc && (doc as any).fonts && (doc as any).fonts.ready) {
            try { await (doc as any).fonts.ready; } catch { /* ignore */ }
          }
          const imgs = Array.from(body.querySelectorAll("img"));
          await Promise.all(
            imgs.map((img) =>
              img.complete
                ? Promise.resolve()
                : new Promise<void>((resolve) => {
                    img.addEventListener("load", () => resolve(), { once: true });
                    img.addEventListener("error", () => resolve(), { once: true });
                  })
            )
          );
          if (cancelled) return;

          // Now collect N consecutive identical rAF measurements.
          const measure = (): Promise<number> =>
            new Promise((resolve) => {
              requestAnimationFrame(() => resolve(body.scrollHeight));
            });
          let last = -1;
          let stable = 0;
          while (!cancelled && stable < STABLE_RAF_REQUIRED) {
            if (Date.now() - start > HARD_TIMEOUT_MS) {
              (window as any).__previewReady = true;
              return;
            }
            const h = await measure();
            if (h === last && h > 400) {
              stable += 1;
            } else {
              stable = 0;
              last = h;
            }
          }
          if (cancelled) return;
          // Enforce minimum dwell so TinyMCE's deferred passes get a
          // chance to run even if they happen to land on a stable plateau.
          const elapsed = Date.now() - iframeFirstSeenAt;
          if (elapsed < MIN_DWELL_MS) {
            await new Promise((r) => setTimeout(r, MIN_DWELL_MS - elapsed));
          }
          // One last measurement after the dwell — if it changed, restart
          // the stability loop. Two rounds of (stable * 8 + dwell) is
          // typically all it takes for the document to fully settle.
          const finalH = body.scrollHeight;
          if (finalH !== last) {
            last = finalH;
            stable = 0;
            continue;
          }

          // Resize the iframe element itself to match its content height
          // so a viewport-clipped screenshot still captures the whole
          // document. TinyMCE styles the iframe with a fixed height by
          // default; expand it.
          try {
            iframe.style.height = `${finalH}px`;
            iframe.style.minHeight = `${finalH}px`;
          } catch {
            /* defensive */
          }
          // Stash dimensions for the Playwright runner to read.
          (window as any).__previewIframeHeight = finalH;
          (window as any).__previewIframeWidth = body.scrollWidth;
          (window as any).__previewReady = true;
          return;
        }
        await new Promise((r) => setTimeout(r, 150));
      }
    };

    waitForStableHeight();

    return () => {
      cancelled = true;
    };
  }, [editorMountedAt]);

  return null;
}

export default function DocEditorHeadless() {
  const [searchParams] = useSearchParams();
  const owner = searchParams.get("owner") || "";
  const artifactId = searchParams.get("artifact_id") || "";
  const orientation =
    searchParams.get("orientation") === "landscape" ? "landscape" : "portrait";

  const [initialValue, setInitialValue] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editorMountedAt, setEditorMountedAt] = useState<number | null>(null);

  // Reset latch on every mount so consecutive Playwright runs sharing a
  // process (shouldn't happen with on-demand Chromium) don't see a stale
  // `true`.
  useEffect(() => {
    (window as any).__previewReady = false;
  }, []);

  // Fetch the artifact's HTML directly from /files/ (public — nginx
  // location /files/ proxies to docker-api without auth gating).
  useEffect(() => {
    let cancelled = false;
    if (!owner || !artifactId) {
      setError("missing owner or artifact_id");
      // Surface ready so Playwright doesn't hang.
      (window as any).__previewReady = true;
      return;
    }
    (async () => {
      try {
        const url = `/files/${encodeURIComponent(owner)}/${encodeURIComponent(artifactId)}/artifact.html`;
        const resp = await fetch(url);
        if (!resp.ok) {
          throw new Error(`HTTP ${resp.status} for ${url}`);
        }
        const text = await resp.text();
        if (!cancelled) {
          setInitialValue(text);
          setEditorMountedAt(Date.now());
        }
      } catch (e) {
        if (!cancelled) {
          setError(String((e as Error).message || e));
          (window as any).__previewReady = true;
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [owner, artifactId]);

  if (error) {
    return (
      <div
        style={{
          position: "fixed",
          inset: 0,
          padding: 24,
          fontFamily: "monospace",
          background: "#fff",
          color: "#900",
        }}
      >
        DocEditorHeadless error: {error}
      </div>
    );
  }
  if (initialValue === null) {
    return (
      <div
        style={{
          position: "fixed",
          inset: 0,
          background: "#fff",
        }}
      />
    );
  }

  return (
    <AppContext.Provider value={HEADLESS_CTX as any}>
      <div
        style={{
          position: "relative",
          width: "100%",
          minHeight: "100vh",
          background: "#fff",
          margin: 0,
          padding: 0,
        }}
      >
        <DocEditor
          initialValue={initialValue}
          htmlLink=""
          docxLink=""
          pptxLink=""
          productName="headless-preview"
          sessionId="headless-preview"
          pendingOrientation={orientation as "portrait" | "landscape"}
        />
        <HeadlessReadySignal editorMountedAt={editorMountedAt} />
      </div>
    </AppContext.Provider>
  );
}
