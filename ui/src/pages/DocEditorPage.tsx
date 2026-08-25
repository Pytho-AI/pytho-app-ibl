/**
 * DocEditorPage — standalone route wrapper around <DocEditor>.
 *
 * Surface for the Generate-button UX rework. The flow is:
 *
 *   1. ProductLibraryModal: analyst clicks Generate on a product card.
 *   2. Modal navigates *immediately* to
 *        /missioncommand/docs/:sessionId?pending_product_type=<KEY>
 *                                       &project_id=<id>
 *                                       &user_id=<id>
 *                                       &product_name=<display name>
 *      and closes itself.
 *   3. This page mounts and FIRST hits GET /api/sessions/<sessionId>/artifacts
 *      to check whether OE already finished a generation in this session.
 *        a. If an artifact exists → load it directly into DocEditor, strip
 *           query params via navigate-replace, and DO NOT touch the chat
 *           panel. (This is the refresh-during/after-generation path.)
 *        b. If no artifact exists AND ?pending_product_type is present →
 *           open the chrome chat panel via ChatContext.openChat with a
 *           synthesized "Generate {TYPE}" prompt. ChatContext tracks which
 *           sessionIds have already consumed their initialMessage so a
 *           refresh during a 100-150s generation won't re-fire the prompt.
 *        c. If neither → show a "no document loaded" hint.
 *   4. When OE emits an artifact, ChatAgent forwards it through the
 *      onArtifact callback registered via openChat → this page fetches the
 *      htmlLink and feeds it into DocEditor's `initialValue`, which
 *      re-mounts the TinyMCE editor with the real document.
 *
 * Replaces the old behavior where ChatAgent was mounted inside DocEditor
 * (the WS torn down on every navigation) and the page re-fired the
 * synthesized "Generate WEM" prompt every refresh. The synchronous
 * /api/products/generate endpoint is kept on the backend for scripted
 * callers, but the UI does not use it anymore — see handlers/product.py.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams, useNavigate } from "react-router-dom";
import * as SubframeCore from "@subframe/core";
import DocEditor from "../components/DocEditor";
import LiveEditor, { type LiveEditorHandle, type MapEmbed } from "../components/LiveEditor";
// MapEmbed type stays imported — onEditMap callback and the edit-map
// overlay (editingMap state) still consume it. The map-embeds dropdown
// state was deleted along with the chrome dropdown consumer.
import { IconButton } from "../subframe/components/IconButton";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { useAppContext } from "../lib/contextLib";
import { useChatContext, type ChatArtifactEvent } from "../lib/ChatContext";
import { useEditConfig } from "../lib/useEditConfig";
import { clientWarn, clientError } from "../lib/clientLog";
import StageTimeline from "../components/StageTimeline";
import FinalizeDownstreamsModal, {
  type ImmediateDownstream,
} from "../components/FinalizeDownstreamsModal";
// Task 6: the map surface moved into geo-agent. The Edit-map overlay
// below iframes /geo-app/operations-map; no direct component imports.

function synthesizePrompt(productType: string, productName: string): string {
  // Mirrors the analyst-facing prompt that handlers/product.py:generate uses
  // when the legacy synchronous endpoint is called. Same wording so OE
  // recognizes this as a Generate-button dispatch and avoids the
  // confirmation-loop pattern when it has enough OPORD context to proceed.
  const label = productName && productName !== productType
    ? `${productType} (${productName})`
    : productType;
  return (
    `Generate a ${label} now. This is a manual-dispatch request from the ` +
    `analyst — proceed without asking for confirmation unless OPORD/FRAGO/` +
    `ANNEX_L_INTEL context is insufficient. Read the OPORD for scope, ` +
    `dispatch the intel agent and the product writer, and emit the artifact ` +
    `in this turn. Use doctrinal defaults for any header field you cannot ` +
    `derive (canonical-now DTG, UNCLASSIFIED classification, current phase ` +
    `from OPORD).`
  );
}

interface ServerArtifact {
  artifact_id: string;
  artifact_type: string;
  htmlLink?: string | null;
  docxLink?: string | null;
  pptxLink?: string | null;
  // XLSX-canonical artifacts (15 doctrinally-XLSX products). When set,
  // DocEditorPage mounts <SheetEditor> for live co-edit via Collabora
  // instead of <DocEditor>.
  xlsxLink?: string | null;
  timestamp?: number;
  gaps?: string[];
  // Approval lifecycle fields — written by handlers/product.py's
  // /api/sessions/<id>/artifacts and /api/products/autodraft_latest
  // responses. "draft" is the implicit default for newly emitted
  // artifacts; "published" is set after a successful POST /finalize.
  // approved_by / approved_at are populated for finalize callers (epoch
  // ms for approved_at). Live WS ChatArtifactEvents don't carry these
  // fields — they always default to "draft" on first emission and
  // flip via the analyst's toggle, not the WS frame.
  status?: string;
  approved_by?: string;
  approved_at?: number;
}

// Canonical-on-disk finalize response shape — mirrors
// handlers/product.py:finalize_artifact return payload. The full
// schema_state_diff lives in the audit log; the endpoint surfaces only
// the summary fields the SPA needs to message the analyst.
interface FinalizeWarning {
  code: string;
  field_path: string;
  detail: string;
}

interface FinalizeResponse {
  artifact_id: string;
  product_type: string;
  mtime: number;
  structured_data_summary: {
    fields_added: string[];
    fields_removed: string[];
    fields_modified_count: number;
  };
  warnings: FinalizeWarning[];
  immediate_downstreams: ImmediateDownstream[];
}

// Validation-failure body returned by the finalize endpoint on 422 —
// FastAPI nests its detail object verbatim, so the structured warnings
// land at body.detail.warnings (each entry is [code, field_path, detail]
// before serialization, but FinalizeResponse renames them to the dict
// shape on the happy path). On 422 they come back as raw 3-tuples.
interface FinalizeValidationDetail {
  message: string;
  warnings: [string, string, string][];
}

export default function DocEditorPage() {
  const { sessionId: routeSessionId = "" } = useParams<{ sessionId: string }>();
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const { user, projectId: appProjectId } = useAppContext();
  const { openChat, setOnArtifact, setCurrentArtifactId } = useChatContext();

  const pendingProductType = searchParams.get("pending_product_type") || "";
  const productName = searchParams.get("product_name") || pendingProductType;
  // When the dashboard navigates here to OPEN an existing product (vs. the
  // generate flow), it sets ?opening=1 so the load placeholders read
  // "Opening…" instead of the misleading "Generating…".
  const isOpening = searchParams.get("opening") === "1";
  const loadVerb = isOpening ? "Opening" : "Generating";
  const loadVerbLc = isOpening ? "opening" : "generating";
  // The product's doctrinal page_orientation, set by the Generate-button
  // navigation (ProductLibraryModal.runGenerate). Passed straight through to
  // DocEditor so the skeleton + initial TinyMCE content_style page-bound
  // correctly during the generation window. Defaults to portrait if absent.
  // Orientation IS the format choice — landscape ⇒ PPTX slide (16:9, no
  // DRAFT watermark), portrait ⇒ DOCX Letter (8.5×11, DRAFT default). One
  // decision, one URL param.
  const pendingOrientation =
    searchParams.get("pending_orientation") === "landscape" ? "landscape" : "portrait";
  // Carried from ProductLibraryModal navigation. Lets us route to
  // SheetEditor (Collabora iframe) IMMEDIATELY on mount when the
  // pending product is xlsx-canonical — analyst never sees the
  // TinyMCE skeleton flash before the artifact arrives.
  const pendingFormat = searchParams.get("pending_format") || "";
  // project_id / user_id are taken from the query string (set by
  // ProductLibraryModal at navigation time) so the chrome chat-panel WS
  // handshake can use them even before AppContext has hydrated. AppContext
  // provides fall-throughs when the query string is absent (e.g. a
  // direct-link refresh after the bookmark loses its query string).
  const queryProjectId = searchParams.get("project_id") || "";
  const queryUserId = searchParams.get("user_id") || "";

  const initialMessage = useMemo(() => {
    if (!pendingProductType) return "";
    return synthesizePrompt(pendingProductType, productName);
  }, [pendingProductType, productName]);

  const [htmlContent, setHtmlContent] = useState<string>("");
  const [docxLink, setDocxLink] = useState<string>("");
  const [pptxLink, setPptxLink] = useState<string>("");
  // XLSX artifact lives in Collabora, not TinyMCE. When present, the
  // render branches to <SheetEditor>. Also carries the artifact_id used
  // to mint a WOPI access token.
  const [xlsxLink, setXlsxLink] = useState<string>("");
  const [xlsxArtifactId, setXlsxArtifactId] = useState<string>("");
  const [productNameState, setProductNameState] = useState<string>(productName || "");
  // Track the loaded artifact's product_type so the Regenerate button can
  // re-fire generation against the right product type even when we landed
  // via the cache-read path (where pending_product_type isn't in the URL).
  const [loadedProductType, setLoadedProductType] = useState<string>("");
  // Approval toggle: which artifact is open, its draft/published status, an
  // in-flight guard, and who approved it + when (surfaced next to the toggle).
  // Note on "published": the toggle's visible state is local-only under the
  // canonical-on-disk model. Draft → Final fires POST /finalize which
  // rebuilds schema_state.json from markers in the deliverable (stateless —
  // no DRAFT/SAVED/FINAL state machine on the backend). Final → Draft is a
  // pure UI rollback; previously-imported state stays in schema_state.json.
  const [loadedArtifactId, setLoadedArtifactId] = useState<string>("");
  const [artifactStatus, setArtifactStatus] = useState<string>("draft");
  const [approvalBusy, setApprovalBusy] = useState<boolean>(false);
  const [approvedBy, setApprovedBy] = useState<string>("");
  const [approvedAt, setApprovedAt] = useState<number>(0);
  // Downstream-refire modal state. Opens on a successful /finalize and lists
  // the immediate_downstreams returned by the endpoint. Closing it without
  // submitting has no backend effect — the finalize already landed.
  const [downstreamModalOpen, setDownstreamModalOpen] = useState<boolean>(false);
  const [downstreamRefireBusy, setDownstreamRefireBusy] = useState<boolean>(false);
  const [immediateDownstreams, setImmediateDownstreams] = useState<ImmediateDownstream[]>([]);
  const [finalizedProductType, setFinalizedProductType] = useState<string>("");
  const [loadError, setLoadError] = useState<string>("");
  const [editorIsDirty, setEditorIsDirty] = useState<boolean>(false);
  // Lookup phase — true until /api/sessions/<id>/artifacts returns. Blocks
  // the "no document loaded" hint from flashing during the round-trip.
  const [lookupPending, setLookupPending] = useState<boolean>(true);
  // Set when boot decides this page has no valid context (no pending
  // product, no loaded artifact, no in-flight job). The redirect
  // useEffect below fires nav() once this flips true.
  const [shouldRedirectHome, setShouldRedirectHome] = useState<boolean>(false);
  // ── LiveEditor (Collabora) state ─────────────────────────────────────────
  // Imperative handle for save-first actions (download / regenerate flush
  // via Action_Save before proceeding) and fullscreen UI-mode toggle.
  const liveEditorRef = useRef<LiveEditorHandle | null>(null);
  const [liveFullscreen, setLiveFullscreen] = useState<boolean>(false);
  // Edit-map overlay state. When set, we render <GeoAgentMap> on top of
  // the LiveEditor. On "Done editing" we POST /refresh_map and reload.
  const [editingMap, setEditingMap] = useState<MapEmbed | null>(null);
  const [mapRefreshing, setMapRefreshing] = useState<boolean>(false);
  // Read the live-edit feature flag (one boot-time /api/config fetch).
  const editConfig = useEditConfig();
  const liveEditNarrativeOn = !!editConfig?.collabora_narrative_edit;
  // Guards re-running the lookup + openChat on every render. The lookup +
  // openChat handshake must fire ONCE per sessionId per mount, irrespective
  // of which prop / context values changed downstream.
  const bootRef = useRef<string>("");

  // Once OE emits an artifact (live via WS) or we find one already attached
  // to this session (REST), fetch its HTML and feed it into DocEditor. The
  // editor's existing useEffect[initialValue] re-mounts TinyMCE on a
  // non-empty value (editorKey++), so the pending overlay clears
  // automatically when htmlContent becomes non-empty.
  async function loadArtifact(artifact: ChatArtifactEvent | ServerArtifact) {
    setLoadError("");
    setLoadedArtifactId(artifact.artifact_id || "");
    // The IRC channel join is deferred to onEditorReady (below) — the
    // chrome chat panel mounts only AFTER the LiveEditor iframe has
    // fired Document_Loaded, so the analyst doesn't see an "INTEL
    // AGENT" drawer flash through the brief loading-spinner window.
    // ChatContext.currentArtifactId stays "" until the editor reports
    // ready.
    //
    // Approval-lifecycle fields live on ServerArtifact only; the WS
    // ChatArtifactEvent never carries them (new artifacts emit as
    // "draft" implicitly — the toggle flips status, the WS frame
    // doesn't). Narrow with `"status" in artifact` so the union
    // resolves to ServerArtifact for the optional-field reads.
    if ("status" in artifact) {
      setArtifactStatus(artifact.status || "draft");
      setApprovedBy(artifact.approved_by || "");
      setApprovedAt(artifact.approved_at || 0);
    } else {
      setArtifactStatus("draft");
      setApprovedBy("");
      setApprovedAt(0);
    }
    // XLSX-canonical artifact path: skip the HTML fetch entirely. The
    // file lives in storage and the analyst edits it live in Collabora.
    // artifact.artifact_id is the WOPI handle SheetEditor mints a token
    // against.
    //
    // Take the XLSX branch when xlsxLink is present AND there's no DOCX
    // or PPTX companion (i.e., the 15 XLSX-canonical products where
    // output_formats=["xlsx"]). Hybrid products (DOCX+XLSX siblings)
    // still get DocEditor for the narrative + XLSX as a sibling download.
    const xlsx = artifact.xlsxLink || "";
    const hasNarrativeArtifact = !!(artifact.htmlLink || artifact.docxLink || artifact.pptxLink);
    if (xlsx && !hasNarrativeArtifact) {
      setXlsxLink(xlsx);
      setXlsxArtifactId(artifact.artifact_id || "");
      if (artifact.artifact_type) {
        setProductNameState((cur) => cur || artifact.artifact_type);
        setLoadedProductType(artifact.artifact_type);
      }
      return;
    }
    const link = artifact.htmlLink || "";
    if (!link) {
      // Analyst-facing banner stays doctrinally neutral — no module names,
      // no exporter references, no WS-reply talk. Full diagnostic goes to
      // the browser console + /api/client_log so we can debug from logs.
      const diagnostic = {
        reason: "artifact_missing_html_link",
        artifact_id: artifact.artifact_id,
        artifact_type: artifact.artifact_type,
        has_docx: !!artifact.docxLink,
        has_pptx: !!artifact.pptxLink,
        has_xlsx: !!artifact.xlsxLink,
      };
      // eslint-disable-next-line no-console
      console.error("[DocEditorPage] artifact missing htmlLink", diagnostic);
      clientError("DocEditorPage.artifact_missing_html_link", "missing htmlLink", diagnostic);
      setLoadError("Unable to load this product. Try again or refresh the page.");
      return;
    }
    if (artifact.docxLink) setDocxLink(artifact.docxLink);
    if (artifact.pptxLink) setPptxLink(artifact.pptxLink);
    // Hybrid (DOCX + XLSX) artifacts: surface the XLSX download alongside
    // the DocEditor view. We still render TinyMCE for the narrative;
    // analysts get the XLSX as a sibling download.
    if (xlsx) {
      setXlsxLink(xlsx);
      setXlsxArtifactId(artifact.artifact_id || "");
    }
    if (artifact.artifact_type) {
      setProductNameState((cur) => cur || artifact.artifact_type);
      setLoadedProductType(artifact.artifact_type);
    }
    try {
      const resp = await fetch(`${link}?t=${Date.now()}`);
      if (!resp.ok) {
        // eslint-disable-next-line no-console
        console.error("[DocEditorPage] HTML fetch non-OK", { link, status: resp.status });
        clientError("DocEditorPage.artifact_html_fetch_failed", `HTTP ${resp.status}`, {
          link, http_status: resp.status, artifact_id: artifact.artifact_id,
        });
        setLoadError("Unable to load this product. Try again or refresh the page.");
        return;
      }
      let html = await resp.text();
      // Mirror Chat.openDocEditor's pagebreak normalization.
      html = html.replace(/\[PAGE_BREAK\]/g, "<!-- pagebreak -->");
      setHtmlContent(html);
    } catch (err: any) {
      // eslint-disable-next-line no-console
      console.error("[DocEditorPage] HTML fetch threw", err);
      clientError("DocEditorPage.artifact_html_fetch_threw", err, {
        link, artifact_id: artifact.artifact_id,
      });
      setLoadError("Unable to load this product. Try again or refresh the page.");
    }
  }

  // Approval toggle handler. `approve` true -> Final (canonical-on-disk
  // finalize: rebuild schema_state.json from markers in the deliverable,
  // then surface a modal listing immediate downstream products to refire).
  // `approve` false -> revert the visible state to Draft locally. There is
  // NO un-finalize endpoint — the architecture is stateless, schema_state.json
  // is whatever the last importer wrote, and rolling the UI back to Draft
  // does not undo the prior import.
  async function setApproval(approve: boolean) {
    if (!loadedArtifactId || approvalBusy) return;
    if (approve === (artifactStatus === "published")) return;
    setApprovalBusy(true);
    try {
      if (approve) {
        // Flush in-flight LiveEditor edits BEFORE the importer reads the
        // deliverable. The importer walks markers in the on-disk file; any
        // pending edits sitting in the Collabora session would be invisible
        // to it and silently dropped. Best-effort — if save() rejects we
        // still proceed (the analyst explicitly asked to finalize).
        if (liveEditorRef.current && editorIsDirty) {
          try { await liveEditorRef.current.save(); } catch { /* best-effort */ }
        }
        // Stamp the approver so finalize persists it on the published header.
        // The status flip now lives in the DB (SessionDetail header.status), so
        // the dashboard's DRAFT/FINAL badge and the "Approved by …" caption
        // survive a navigate-back / reload instead of being editor-local only.
        const approverName =
          user?.firstName && user?.lastName
            ? `${user.firstName} ${user.lastName}`
            : user?.email || user?.userId || "";
        const finalizeQs = new URLSearchParams();
        if (user?.userId) finalizeQs.set("user_id", String(user.userId));
        if (approverName) finalizeQs.set("approved_by", approverName);
        const r = await fetch(
          `/api/artifacts/${encodeURIComponent(loadedArtifactId)}/finalize?${finalizeQs.toString()}`,
          { method: "POST" },
        );
        if (r.ok) {
          const d = (await r.json()) as FinalizeResponse;
          setArtifactStatus("published");
          setApprovedBy(approverName);
          setApprovedAt(d.mtime ? d.mtime * 1000 : Date.now());
          // Open the downstream-refire modal. If immediate_downstreams is
          // empty the modal still opens — it surfaces a "nothing depends on
          // this artifact" message and the Cancel button closes it. That
          // keeps the UX feedback explicit instead of silently swallowing
          // the click.
          setImmediateDownstreams(d.immediate_downstreams || []);
          setFinalizedProductType(d.product_type || loadedProductType || "");
          setDownstreamModalOpen(true);
        } else if (r.status === 422) {
          // Validation failure — backend rejected the finalize because
          // required fields are missing. Toggle stays in Draft. Surface
          // ONLY the missing-field list to the analyst (no module names,
          // no schema_state references). Full payload to console + log.
          let detail: FinalizeValidationDetail | null = null;
          try {
            const body = await r.json();
            detail = body?.detail ?? null;
          } catch { /* fall through to generic */ }
          // eslint-disable-next-line no-console
          console.error("[DocEditorPage] finalize 422", detail);
          clientError("DocEditorPage.finalize_validation_failed", "422", { detail });
          const missingFields = (detail?.warnings || [])
            .filter((w) => (w[0] || "").startsWith("missing_required"))
            .map((w) => w[1])
            .filter((path) => !!path);
          if (missingFields.length > 0) {
            setLoadError(`Required fields missing: ${missingFields.join(", ")}`);
          } else {
            setLoadError("Required fields missing.");
          }
        } else {
          // 404 / 500 / anything else. Generic message to the analyst;
          // full body to console + /api/client_log for diagnosis.
          const body = await r.text().catch(() => "");
          // eslint-disable-next-line no-console
          console.error("[DocEditorPage] finalize failed", {
            http_status: r.status, body,
          });
          clientError("DocEditorPage.finalize_failed", `HTTP ${r.status}`, {
            http_status: r.status, body,
          });
          setLoadError("Unable to finalize this product. Try again or refresh the page.");
        }
      } else {
        // Final → Draft. Persist the revert so the dashboard's badge flips
        // back to DRAFT too — the approval status lives in the SessionDetail
        // header (the same place finalize writes it), flipped via
        // POST /api/artifacts/{id}/revert. The importer's field values in
        // schema_state.json are NOT undone; only the approval status +
        // approver stamp are cleared, and the editor returns to writable mode.
        const ok = window.confirm(
          "Revert to Draft?\n\n" +
          "This flips the product back to Draft and re-enables editing. The " +
          "field values captured at the last finalize stay in place until you " +
          "finalize again.",
        );
        if (!ok) return;
        try {
          await fetch(
            `/api/artifacts/${encodeURIComponent(loadedArtifactId)}/revert`,
            { method: "POST" },
          );
        } catch {
          /* best-effort — the local UI still rolls back to Draft */
        }
        setArtifactStatus("draft");
        setApprovedBy("");
        setApprovedAt(0);
      }
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[DocEditorPage] finalize toggle threw", e);
      clientError("DocEditorPage.finalize_toggle_threw", e, {
        artifact_id: loadedArtifactId,
      });
      setLoadError("Unable to finalize this product. Try again or refresh the page.");
    } finally {
      setApprovalBusy(false);
    }
  }

  // Modal submit handler — fires the refire-immediate-downstream endpoint
  // for the analyst's selected downstream product_types and closes the
  // modal. The endpoint already serializes one hop only; this client does
  // not recurse. If the analyst finalizes a freshly-refired downstream
  // later, THAT finalize's response opens a new modal for the next hop.
  async function refireSelectedDownstreams(productTypes: string[]) {
    if (!loadedArtifactId || downstreamRefireBusy) return;
    if (productTypes.length === 0) {
      setDownstreamModalOpen(false);
      return;
    }
    setDownstreamRefireBusy(true);
    try {
      const r = await fetch(
        `/api/artifacts/${encodeURIComponent(loadedArtifactId)}/refire-immediate-downstream`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ product_types: productTypes }),
        },
      );
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        // eslint-disable-next-line no-console
        console.error("[DocEditorPage] downstream refire non-OK", {
          http_status: r.status, body,
        });
        clientError("DocEditorPage.downstream_refire_failed", `HTTP ${r.status}`, {
          http_status: r.status, body,
        });
        setLoadError("Couldn't refire downstream products. Try again later.");
        return;
      }
      // 200 — the endpoint DISPATCHED the jobs and returns immediately with
      // { dispatched: [...], errors: [...] } (errors = immediate dispatch
      // failures like unknown product_type; generation failures surface on
      // the dashboard). Return the dispatched list so the modal flips to its
      // confirmation phase instead of closing — the analyst tracks live
      // progress on the dashboard.
      let dispatched: string[] = productTypes;
      let errors: Array<{ product_type: string; error: string }> = [];
      try {
        const body = await r.json();
        if (Array.isArray(body?.dispatched)) dispatched = body.dispatched;
        errors = (body?.errors || []) as Array<{ product_type: string; error: string }>;
        if (errors.length > 0) {
          // eslint-disable-next-line no-console
          console.error("[DocEditorPage] downstream refire dispatch errors", errors);
          clientError("DocEditorPage.downstream_refire_partial", "dispatch errors", { errors });
        }
      } catch { /* tolerate non-JSON body */ }
      return { dispatched, errors };
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[DocEditorPage] downstream refire threw", e);
      clientError("DocEditorPage.downstream_refire_threw", e, {
        artifact_id: loadedArtifactId,
      });
      setLoadError("Couldn't refire downstream products. Try again later.");
    } finally {
      setDownstreamRefireBusy(false);
    }
  }

  // Boot sequence — runs once per (mount, sessionId). Order is critical:
  //   1. Lookup existing artifacts via REST.
  //   2a. If found → load directly, strip query params (refresh-safe).
  //   2b. If empty AND pending_product_type → kick off chrome chat.
  //   2c. If empty AND no pending → surface a hint.
  useEffect(() => {
    if (!routeSessionId) return;
    if (bootRef.current === routeSessionId) return;
    bootRef.current = routeSessionId;

    let cancelled = false;
    async function boot() {
      setLookupPending(true);
      let serverArtifacts: ServerArtifact[] = [];
      try {
        const resp = await fetch(`/api/sessions/${encodeURIComponent(routeSessionId)}/artifacts`);
        if (resp.ok) {
          const body = await resp.json();
          serverArtifacts = (body?.artifacts || []) as ServerArtifact[];
        }
      } catch (err) {
        // Network error — fall through to the live-WS path so the user can
        // still see chat. Surface a hint though.
        clientWarn("DocEditorPage.artifact_lookup", err);
      }
      if (cancelled) return;

      if (serverArtifacts.length > 0) {
        // Refresh-safe path: an artifact already exists. Load it and clean
        // the URL so a subsequent F5 hits this same branch unambiguously.
        // We pick the most recent (the list is sorted asc by timestamp).
        const latest = serverArtifacts[serverArtifacts.length - 1];
        await loadArtifact(latest);
        // Bring the chat panel back even on refresh — the user expects to
        // see the OE conversation context (and to be able to follow up)
        // when they return to an existing artifact. Without this, the
        // ChatAgent renders nothing because its sessionId is never set
        // (only pendingProductType branches call openChat). NO synthesized
        // initialMessage on this branch — generation already happened.
        const userId = queryUserId || user?.userId || user?.email || "";
        const pid = queryProjectId || appProjectId || "";
        openChat({
          sessionId: routeSessionId,
          projectId: pid,
          userId,
          onArtifact: loadArtifact,
        });
        // Strip lookup-only params (pending_*, project_id, user_id) but
        // trip, then clears it from the URL itself.
        const keepParams = new URLSearchParams();
        const qs = keepParams.toString();
        navigate(
          `/missioncommand/docs/${encodeURIComponent(routeSessionId)}${qs ? `?${qs}` : ""}`,
          { replace: true },
        );
        setLookupPending(false);
        return;
      }

      // No artifact in this session yet. If we have a pending_product_type
      // hint, this is a fresh Generate-button click — open the chrome chat
      // sidebar. The structured `generate_product` WS frame fires from
      // ChatAgent on first mount; ChatAgent also pushes the returned
      // job_id onto the URL as ?job=<id> so a mid-generation refresh
      // can pick the work back up via /api/jobs/{id} below.
      //
      // If we DON'T have pending_product_type but we DO have ?job=<id>,
      // this is a refresh during / after a generation. Probe the job
      // registry to figure out where we are:
      //   - completed → load the artifact (poll sessions/artifacts in
      //     case the lookup above raced the job persist), show editor.
      //   - in_progress → open the chat panel WITHOUT initialMessage so
      //     we just listen for the artifact frame on the existing WS
      //     subscription. The backend is still running it.
      //   - failed / interrupted → surface the error.
      // Event replay (DESIGN §13) is future work; this is the minimum
      // reconnect path that keeps a 12s INTSUM run survivable across F5.
      const jobIdParam = searchParams.get("job") || "";
      if (pendingProductType) {
        const userId = queryUserId || user?.userId || user?.email || "";
        const pid = queryProjectId || appProjectId || "";

        // Cache-read: query the autodraft pool BEFORE firing the DAG.
        // If an autodraft already exists for this product type, load
        // it directly — analyst sees the warm draft in ~1s instead of
        // waiting for a full LLM regen. Falls through to chat-trigger
        // on cache miss (cold product types still get a fresh DAG run).
        //
        // Skip the cache-read when force_regen=1 is in the URL — that's
        // the Regenerate button's signal that the analyst explicitly
        // wants a fresh DAG run even though the cache is warm.
        const forceRegen = searchParams.get("force_regen") === "1";
        let autodraftHit = false;
        try {
          if (forceRegen) throw new Error("force_regen — bypass cache");
          const adResp = await fetch(
            `/api/products/autodraft_latest?product_type=${encodeURIComponent(pendingProductType)}`
          );
          if (adResp.ok) {
            const adBody = await adResp.json();
            const adArtifacts = (adBody?.artifacts || []) as ServerArtifact[];
            if (adArtifacts.length > 0) {
              const latestAd = adArtifacts[adArtifacts.length - 1];
              await loadArtifact(latestAd);
              // Keep the chat panel open so the analyst can iterate on
              // the autodraft (clarifying Qs, follow-ups). NO initialMessage
              // — generation didn't run, nothing to caption.
              openChat({
                sessionId: routeSessionId,
                projectId: pid,
                userId,
                onArtifact: loadArtifact,
              });
              autodraftHit = true;
              setLookupPending(false);
            }
          }
        } catch (err) {
          // Cache-read failure is non-fatal — fall through to chat trigger.
          clientWarn("DocEditorPage.autodraft_cache_read", err);
        }

        if (!autodraftHit) {
          openChat({
            sessionId: routeSessionId,
            projectId: pid,
            userId,
            initialMessage,
            manualProductType: pendingProductType,
            onArtifact: loadArtifact,
          });
        }
      } else if (jobIdParam) {
        try {
          const resp = await fetch(`/api/jobs/${encodeURIComponent(jobIdParam)}`);
          if (!resp.ok) {
            // eslint-disable-next-line no-console
            console.error("[DocEditorPage] job lookup non-OK", {
              job_id: jobIdParam, http_status: resp.status,
            });
            clientError("DocEditorPage.job_lookup_not_ok", `HTTP ${resp.status}`, {
              job_id: jobIdParam, http_status: resp.status,
            });
            setLoadError("Couldn't restore the previous generation.");
          } else {
            const job = await resp.json();
            const jobStatus = String(job?.status || "");
            if (jobStatus === "completed") {
              // Refetch artifacts: by the time the job flipped to
              // completed, /api/sessions/<id>/artifacts should return
              // it. If not, the artifact_id is on the job itself —
              // surface it for the manual fallback case (DESIGN §13
              // mentions /api/jobs/{id}/events; we just use the rolled-
              // up artifact_id field).
              try {
                const r2 = await fetch(`/api/sessions/${encodeURIComponent(routeSessionId)}/artifacts`);
                if (r2.ok) {
                  const b2 = await r2.json();
                  const arr = (b2?.artifacts || []) as ServerArtifact[];
                  if (arr.length > 0) {
                    await loadArtifact(arr[arr.length - 1]);
                  }
                }
              } catch (e) {
                clientWarn("DocEditorPage.completed_refetch", e);
              }
              const userId = queryUserId || user?.userId || user?.email || "";
              const pid = queryProjectId || appProjectId || "";
              openChat({
                sessionId: routeSessionId,
                projectId: pid,
                userId,
                onArtifact: loadArtifact,
              });
            } else if (jobStatus && jobStatus !== "completed" && jobStatus !== "failed") {
              // Match the unified job registry's vocabulary: the autodraft
              // path writes "running" (not "in_progress"), while the legacy
              // OE path writes "in_progress" / "queued". Anything that isn't
              // a terminal state ("completed" / "failed") means "still
              // going" — just re-attach the chat WS so the terminal frame
              // arrives.
              const userId = queryUserId || user?.userId || user?.email || "";
              const pid = queryProjectId || appProjectId || "";
              // NO initialMessage — the backend already kicked off this
              // job from the original click; we're just here to receive
              // the terminal frame on the WS we re-establish.
              openChat({
                sessionId: routeSessionId,
                projectId: pid,
                userId,
                onArtifact: loadArtifact,
              });
            } else {
              // failed / interrupted / unknown — analyst-facing message stays
              // generic; full job payload (status, error, container_id,
              // request_payload, last_event_type, job_id) goes to console + log.
              // eslint-disable-next-line no-console
              console.error("[DocEditorPage] job in non-running terminal state", {
                job_id: jobIdParam, status: jobStatus, job,
              });
              clientError("DocEditorPage.job_terminal_failed", `status=${jobStatus || "unknown"}`, {
                job_id: jobIdParam, status: jobStatus, job,
              });
              setLoadError("Couldn't restore the previous generation.");
            }
          }
        } catch (e) {
          // eslint-disable-next-line no-console
          console.error("[DocEditorPage] job lookup threw", e);
          clientError("DocEditorPage.job_lookup_threw", e, { job_id: jobIdParam });
          setLoadError("Couldn't restore the previous generation.");
        }
      } else {
        // No pending product, no loaded artifact, no in-flight job. The
        // analyst landed here without a valid entry path (likely an old
        // bookmark or stripped URL). Redirect to the dashboard library
        // instead of stranding them on a confusing banner. The redirect
        // useEffect below handles the actual navigation once boot completes.
        setShouldRedirectHome(true);
      }
      setLookupPending(false);
    }
    boot();
    return () => { cancelled = true; };
  // openChat / setOnArtifact / loadArtifact are stable; we deliberately key
  // the entire boot off routeSessionId so re-renders don't re-fire it.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeSessionId]);

  // Whenever we re-render with htmlContent already loaded (e.g. coming back
  // from a navigation), keep the chrome chat panel's onArtifact pointed at
  // THIS page's loadArtifact so subsequent OE turns in the same session
  // still flow into the editor view.
  useEffect(() => {
    if (!routeSessionId) return;
    setOnArtifact(loadArtifact);
    return () => { setOnArtifact(null); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeSessionId]);

  // Clear the chrome chat panel's IRC channel binding on unmount so
  // navigating away from the editor (back to the dashboard, another
  // route) takes the per-doc chat with us — IrcChatPanel falls back
  // to its "no doc open" placeholder and Ergo sees a clean PART.
  useEffect(() => {
    return () => { setCurrentArtifactId(""); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Redirect to the dashboard library when boot decides this page has no
  // valid context. Guard with all the same flags the banner used to check
  // — keeps the redirect from racing the boot's own state writes (e.g.
  // a cache-hit landing loadedArtifactId concurrent with the boot's
  // setShouldRedirectHome). The guard means the only path that fires
  // nav() is "boot completed AND nothing loaded AND no pending hint".
  useEffect(() => {
    if (!shouldRedirectHome) return;
    if (lookupPending) return;
    if (loadedArtifactId) return;
    if (pendingProductType) return;
    navigate("/missioncommand/whitecell", { replace: true });
  }, [shouldRedirectHome, lookupPending, loadedArtifactId, pendingProductType, navigate]);

  // Bind the IRC channel for the TinyMCE mount path. The LiveEditor path
  // defers the binding to onEditorReady (so the chat drawer doesn't flash
  // through the Collabora loading spinner). TinyMCE has no equivalent
  // ready callback, so bind as soon as the artifact id is known. Legacy
  // path — DocEditor is slated for removal, so this stays lightweight.
  useEffect(() => {
    if (!loadedArtifactId) return;
    // Skip — the LiveEditor's onEditorReady handles binding on that path.
    // We detect "LiveEditor path" by the absence of htmlContent (TinyMCE
    // is fed via htmlContent) AND presence of xlsxLink, OR by the
    // liveEditNarrativeOn flag with docx/pptx links. Easiest correctness:
    // when htmlContent is set we're definitively on the TinyMCE path.
    if (!htmlContent) return;
    setCurrentArtifactId(loadedArtifactId);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedArtifactId, htmlContent]);

  // ── Live-edit routing decision ───────────────────────────────────────────
  // XLSX is always LiveEditor (no flag dependency — it's the original
  // surface). DOCX / PPTX go to LiveEditor only when COLLABORA_NARRATIVE_EDIT
  // is on; otherwise fall through to the legacy TinyMCE DocEditor.
  // pendingFormat is set by ProductLibraryModal at navigation time;
  // landed-artifact links are the post-load fallback. Don't decide until
  // the flag config has loaded (editConfig != null) to avoid a flash of
  // the wrong editor.
  const liveFormat: "xlsx" | "docx" | "pptx" | null = (() => {
    const f = (pendingFormat || "").toLowerCase();
    if (f === "xlsx" || !!xlsxLink) return "xlsx";
    if (editConfig === null) return null; // not ready — keep TinyMCE branch hidden
    if (!liveEditNarrativeOn) return null;
    // Explicit pendingFormat wins over link presence. Products with
    // pptx as the primary output (GRINTSUM, civil considerations, etc.)
    // pass pending_format=pptx; the artifact emits BOTH docxLink and
    // pptxLink so the analyst can still download the docx, but the
    // editor surface MUST open the pptx first. Without this short-circuit
    // the `f === "docx" || !!docxLink` form would resolve docx whenever
    // a hybrid product set both links, defeating the schema's primary-
    // format declaration.
    if (f === "docx") return "docx";
    if (f === "pptx") return "pptx";
    if (docxLink) return "docx";
    if (pptxLink) return "pptx";
    return null;
  })();
  const isLiveEdit = liveFormat !== null;

  // ── Save-first helper: flush iframe edits before download / regenerate ──
  async function saveLiveThen<T>(action: () => Promise<T> | T): Promise<T> {
    if (editorIsDirty && liveEditorRef.current) {
      try { await liveEditorRef.current.save(); } catch { /* best-effort */ }
    }
    return action();
  }

  // The done hook from the geo-agent edit iframe. Ref-dispatched so the
  // listener (mounted once) always calls the fresh closure over
  // editingMap.
  const onEditMapDoneRef = useRef<(imageUrl?: string) => void>(() => {});
  useEffect(() => {
    const onMessage = (ev: MessageEvent) => {
      const data = ev?.data;
      if (!data || typeof data !== "object" || data.type !== "geoagent.editDone") return;
      if (data.error && !data.image_url) {
        // geo-agent's export render failed — close the overlay, keep the
        // document's previous image, surface the failure. No fallback
        // re-render from this side: rendering is geo-agent's job.
        setEditingMap(null);
        setLoadError(
          "Map edits were saved, but the map image render failed — the " +
          "document keeps its previous map image. Re-open Edit map and " +
          "hit Done editing to retry.",
        );
        return;
      }
      onEditMapDoneRef.current(
        typeof data.image_url === "string" ? data.image_url : undefined,
      );
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // Invoked by the `geoagent.editDone` hook from the edit-map iframe.
  // geo-agent has ALREADY rendered + persisted the new JPG itself
  // (render=1 on the iframe URL) — imageUrl points at it. This side only
  // asks the backend to download + embed it into the deliverable.
  async function onEditMapDone(imageUrl?: string) {
    const target = editingMap;
    if (!target) return;
    setEditingMap(null);
    if (!liveEditorRef.current) return;
    setMapRefreshing(true);
    try {
      // Flush analyst's pending text/table edits BEFORE the server-side
      // image replace so they survive the zip swap.
      await liveEditorRef.current.save();
      const aid = target.artifact_id || (xlsxArtifactId || "");
      const mid = target.map_id;
      if (!aid || !mid) {
        clientWarn("DocEditorPage.refresh_map_missing_ids", { aid, mid });
        return;
      }
      const res = await fetch(
        `/api/artifacts/${encodeURIComponent(aid)}/refresh_map/${encodeURIComponent(mid)}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ image_url: imageUrl || null }),
        },
      );
      if (!res.ok) {
        clientWarn("DocEditorPage.refresh_map_failed", {
          status: res.status, body: await res.text().catch(() => ""),
        });
        return;
      }
      const body = await res.json().catch(() => ({} as any));
      const editorImageUrl = body?.editor_image_url as string | undefined;
      // ONLY path: UNO swap via CallPythonScript. Keeps the edit session
      // alive — analyst stays on their slide, no 15-45s Collabora
      // parse-from-scratch, no boot screen, no slide jump. The iframe-
      // reload fallback was removed deliberately: it takes 15-45s on
      // the airgapped spark and the analyst loses their slide position,
      // which is worse than a clean error message. If UNO ever fails
      // here, fix UNO — don't paper over it with a slow reload.
      if (!editorImageUrl) {
        clientWarn("DocEditorPage.refresh_map_missing_url", {
          aid, mid, slide_index: target.slide_index,
        });
        setLoadError(
          "Map updated on disk, but no editor URL was returned. The map " +
          "image will refresh next time this product opens.",
        );
        return;
      }
      const swapResult = await liveEditorRef.current.replacePicture(
        mid, editorImageUrl,
        target.slide_index ?? null,
      );
      if (swapResult.status === "replaced") {
        return;
      }
      clientWarn("DocEditorPage.uno_swap_failed", {
        status: swapResult.status, mid, editorImageUrl,
        slide_index: target.slide_index,
      });
      setLoadError(
        "Map image regenerated but the in-document swap failed " +
        `(status: ${swapResult.status}). Re-open this product to see ` +
        "the updated map.",
      );
    } catch (e) {
      clientWarn("DocEditorPage.refresh_map_threw", e);
    } finally {
      setMapRefreshing(false);
    }
  }
  onEditMapDoneRef.current = onEditMapDone;

  return (
    <DefaultPageLayout>
      {(() => {
        if (isLiveEdit) {
          const liveUserId = queryUserId || user?.userId || user?.email || "";
          const liveFriendly = user?.email || queryUserId || "Analyst";
          // artifact_id from the landed artifact. WOPI mint resolves
          // `uploads/_autodraft/<artifact_id>/schema_state.json`, so
          // this MUST be the schema-stamped artifact_id (e.g.
          // `Threat_Overlay_20260530T031144Z`), NOT the routeSessionId
          // UUID. Before PR #52 the autodraft session_id and the
          // artifact_id happened to be the same string; the bus is
          // now demuxed (session = `autodraft-<uuid>`, artifact_id =
          // `<PRODUCT>_<ts>`) and stripping `autodraft-` off the
          // session_id returned the UUID — never a real artifact_id
          // on disk. Fix: prefer loadedArtifactId / xlsxArtifactId
          // (both set by loadArtifact). No fallback to routeSessionId
          // — until the artifact lands, render the "Generating…"
          // placeholder rather than dial WOPI with a doomed id.
          const effectiveArtifactId = xlsxArtifactId || loadedArtifactId || "";
          // Wrapper layout: fullscreen mode drops to a thin chrome strip
          // (download + dirty + exit-fullscreen) and gives the rest of
          // the viewport to the iframe. Normal mode keeps the regular
          // page chrome.
          const inFs = liveFullscreen;
          return (
            <div className={
              inFs
                ? "fixed inset-0 z-50 flex flex-col bg-default-background"
                : "relative flex h-full w-full flex-col bg-default-background"
            }>
              {/* Edit-chrome top strip. Shown on ALL formats. Drops the
                  back arrow (DefaultPageLayout's top bar already has
                  one — duplicate noise). In fullscreen mode, collapses
                  to a thin strip with just download + dirty + exit-fs. */}
              <div className="flex w-full min-w-0 items-center gap-2 overflow-x-auto border-b border-solid border-neutral-border bg-default-background px-3 py-1.5">
                <span className="min-w-0 truncate text-body-bold font-body-bold text-default-font">
                  {productNameState || pendingProductType || "Document"}
                  {!effectiveArtifactId && (
                    <span className="ml-2 text-caption font-caption text-subtext-color">
                      — {loadVerbLc}…
                    </span>
                  )}
                </span>
                <div className="ml-auto flex shrink-0 items-center gap-1.5">
                  {/* Dirty / save indicator */}
                  <span
                    className="text-caption font-caption text-subtext-color"
                    title="Saved-state indicator"
                  >
                    {mapRefreshing ? "Refreshing map…"
                      : editorIsDirty ? "● Unsaved"
                      : "✓ Saved"}
                  </span>
                  {/* No chrome map-dropdown. Edit-map affordance lives on
                      the map itself via LiveEditor's overlay buttons.
                      Products whose render can't support the overlay
                      (Threat Model — convert.js doesn't inline image
                      bytes) opt out of live edit via wopi.py's
                      _DISALLOWED_LIVE_EDIT_PRODUCTS and route to TinyMCE,
                      where the in-doc edit-map button already exists. */}
                  {/* Draft | Final approval toggle — mirrors the TinyMCE
                      chrome's toggle (PR #52). LiveEditor chrome was missing
                      it pre-merge; both surfaces now expose the same
                      lifecycle control so the dashboard's FINAL badge
                      reflects analyst action from either editor. Hidden in
                      fullscreen — the thin chrome strip in inFs mode is
                      reserved for download + exit-fs to keep the iframe
                      area maximal. */}
                  {loadedArtifactId && !inFs && (
                    <div className="flex items-center gap-2">
                      {artifactStatus === "published" && approvedBy && (
                        <span
                          className="text-caption font-caption text-subtext-color"
                          title={
                            approvedAt
                              ? `Approved ${new Date(approvedAt).toLocaleString()}`
                              : "Approved"
                          }
                        >
                          Approved by {approvedBy}
                        </span>
                      )}
                      <div className="inline-flex items-center overflow-hidden rounded-md border border-solid border-neutral-border text-caption font-caption">
                        <button
                          type="button"
                          disabled={approvalBusy}
                          onClick={() => setApproval(false)}
                          title="Mark as draft (editable)"
                          className={
                            artifactStatus !== "published"
                              ? "px-3 py-1 bg-warning-100 text-warning-800 font-caption-bold disabled:opacity-50"
                              : "px-3 py-1 text-subtext-color hover:bg-neutral-50 disabled:opacity-50"
                          }
                        >
                          Draft
                        </button>
                        <div className="h-5 w-px bg-neutral-border" />
                        <button
                          type="button"
                          disabled={approvalBusy}
                          onClick={() => setApproval(true)}
                          title="Approve — mark this product final"
                          className={
                            artifactStatus === "published"
                              ? "px-3 py-1 bg-success-100 text-success-800 font-caption-bold disabled:opacity-50"
                              : "px-3 py-1 text-subtext-color hover:bg-neutral-50 disabled:opacity-50"
                          }
                        >
                          Final
                        </button>
                      </div>
                    </div>
                  )}
                  {loadedArtifactId && !inFs && (
                    <button
                      type="button"
                      onClick={() =>
                        navigate(
                          `/missioncommand/graph-viewer?artifact_id=${encodeURIComponent(loadedArtifactId)}`,
                        )
                      }
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="View this product's COUGAAR data as a graph"
                    >
                      <SubframeCore.Icon className="text-caption" name="FeatherShare2" />
                      Data graph
                    </button>
                  )}
                  {liveFormat === "xlsx" && xlsxLink && !inFs && (
                    <button
                      type="button"
                      onClick={() => saveLiveThen(() => { window.location.href = xlsxLink; })}
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="Save + download XLSX"
                    >
                      ⬇ XLSX
                    </button>
                  )}
                  {liveFormat === "docx" && docxLink && !inFs && (
                    <button
                      type="button"
                      onClick={() => saveLiveThen(() => { window.location.href = docxLink; })}
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="Save + download DOCX"
                    >
                      ⬇ DOCX
                    </button>
                  )}
                  {liveFormat === "pptx" && pptxLink && !inFs && (
                    <button
                      type="button"
                      onClick={() => saveLiveThen(() => { window.location.href = pptxLink; })}
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="Save + download PPTX"
                    >
                      ⬇ PPTX
                    </button>
                  )}
                  <button
                    type="button"
                    onClick={() => {
                      // Toggle our outer chrome AND post Action_ChangeUIMode
                      // so Collabora switches its internal UI mode in
                      // lockstep (compact ↔ notebookbar) without reloading
                      // the iframe.
                      const next = !inFs;
                      setLiveFullscreen(next);
                      liveEditorRef.current?.setUiMode(next ? "notebookbar" : "compact");
                    }}
                    className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font hover:bg-neutral-100"
                    title={inFs ? "Exit fullscreen" : "Fullscreen"}
                  >
                    {inFs ? "⤴ Exit" : "⛶ Fullscreen"}
                  </button>
                </div>
              </div>

              {loadError && !lookupPending && (
                <div className="whitespace-pre-wrap px-3 py-2 text-caption text-error-700 border-b border-solid border-error-200 bg-error-50">
                  {loadError}
                </div>
              )}

              <div className="relative flex-1 overflow-hidden">
                {effectiveArtifactId ? (
                  <LiveEditor
                    ref={liveEditorRef}
                    artifactId={effectiveArtifactId}
                    userId={liveUserId}
                    userFriendlyName={liveFriendly}
                    productName={productNameState || pendingProductType}
                    // FINAL/published artifacts lock the iframe into
                    // read-only. The Draft|Final toggle flips this and
                    // re-mounts the iframe (Collabora can't switch a
                    // running session between edit and readonly; the
                    // permission is read at WOPI/iframe load time).
                    readOnly={artifactStatus === "published"}
                    onEditorReady={() => {
                      // Defer the IRC chat panel mount until the
                      // Collabora iframe has actually painted the
                      // document — avoids the "INTEL AGENT" drawer
                      // briefly flashing on top of the loading
                      // spinner. loadArtifact() set loadedArtifactId
                      // (which mounts the LiveEditor); this callback
                      // is what tells ChatAgent it's safe to show
                      // the chat column.
                      if (loadedArtifactId) {
                        setCurrentArtifactId(loadedArtifactId);
                      }
                    }}
                    onDirtyChange={(s) => setEditorIsDirty(s === "dirty")}
                    onEditMap={(embed) => setEditingMap(embed)}
                    onUnsupported={(reason) => {
                      // Should only fire for docx/pptx when the narrative
                      // flag flips off mid-session. Generic analyst message;
                      // diagnostic reason to console + log.
                      // eslint-disable-next-line no-console
                      console.error("[DocEditorPage] live edit unsupported", { reason });
                      clientError("DocEditorPage.live_edit_unsupported", reason);
                      setLoadError("This product can't be opened for live editing. Refresh to try again.");
                    }}
                  />
                ) : (
                  <div className="flex h-full w-full items-center justify-center">
                    <div className="text-caption text-subtext-color">
                      {loadVerb} {productNameState || pendingProductType || "document"}…
                    </div>
                  </div>
                )}

                {/* Edit-map overlay — same UX as the legacy TinyMCE path
                    (parent-page overlay on top of the editor; editor
                    stays mounted underneath). On Done editing, refresh
                    the image inside the deliverable + reload the iframe. */}
                {editingMap && (
                  // z-[1100] required: the LiveEditor's Edit Map overlay
                  // button is `position: absolute` with `zIndex: 1000`
                  // inside the iframe wrapper. With a lower z-index here
                  // (z-20 worked before that button existed), the button
                  // leaked through the map editor overlay even after the
                  // analyst clicked it. Anything > 1000 puts the editor
                  // and its chrome bar above the overlay button stacking.
                  //
                  // Pure iframe — ALL edit chrome (the "Editing map for …"
                  // banner, Done editing, 16:9 exporter framing) renders
                  // inside geo-agent's map UI. render=1: Done triggers
                  // geo-agent's OWN export render; the `geoagent.editDone`
                  // hook (listener below) carries the persisted image_url
                  // that onEditMapDone hands to /refresh_map for the
                  // document-side embed. No map UI lives on this side.
                  <div className="absolute inset-0 z-[1100] flex flex-col bg-[#1a1a1a]">
                    <div className="relative flex-1">
                      {(() => {
                        const q = new URLSearchParams();
                        q.set("map_id", editingMap.map_id);
                        const pt = loadedProductType || pendingProductType || "";
                        if (pt) q.set("product_type", pt);
                        const uid = queryUserId || user?.userId || user?.email || "";
                        if (uid) q.set("user_id", uid);
                        q.set("editEnabled", "1");
                        q.set("render", "1");
                        if (editingMap.bbox_lonlat && Array.isArray(editingMap.bbox_lonlat) && editingMap.bbox_lonlat.length === 4) {
                          q.set("bbox", (editingMap.bbox_lonlat as number[]).join(","));
                        }
                        const base = (import.meta as any).env?.VITE_GEO_APP_BASE || "/geo-app";
                        return (
                          <iframe
                            src={`${base}/operations-map?${q.toString()}`}
                            title="Edit Map"
                            style={{ position: "absolute", inset: 0,
                                     width: "100%", height: "100%", border: 0,
                                     display: "block", background: "#0b1120" }}
                            allow="clipboard-read; clipboard-write; fullscreen"
                          />
                        );
                      })()}
                    </div>
                  </div>
                )}
              </div>
            </div>
          );
        }
        // Initial-load spinner — shown while the boot's REST lookup is
        // in flight and the format hasn't been resolved yet. Replaces
        // the brief flash of the TinyMCE docx skeleton that used to
        // render during the lookup window for xlsx (and any other
        // LiveEditor-bound) products. Once `lookupPending` flips false
        // we either entered the LiveEditor branch above (when
        // pending_format / xlsxLink / liveEditNarrativeOn picked a
        // live-edit format) or we drop through to the TinyMCE branch
        // below for the legacy narrative path.
        if (lookupPending) {
          return (
            <div className="relative flex h-full w-full items-center justify-center bg-default-background">
              <div className="flex flex-col items-center gap-2 text-subtext-color">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-neutral-border border-t-default-font" />
                <span className="text-caption font-caption">
                  {isOpening ? "Opening" : "Loading"}{" "}
                  {productNameState || pendingProductType || "document"}…
                </span>
              </div>
            </div>
          );
        }
        // Bare-URL fallback — boot decided this page has nothing valid to
        // render and the redirect useEffect is about to fire nav() to the
        // dashboard. Show a tiny "Redirecting…" spinner instead of the
        // empty TinyMCE skeleton, which would flash for a single frame.
        if (shouldRedirectHome && !loadedArtifactId && !pendingProductType) {
          return (
            <div className="relative flex h-full w-full items-center justify-center bg-default-background">
              <div className="flex flex-col items-center gap-2 text-subtext-color">
                <div className="h-5 w-5 animate-spin rounded-full border-2 border-neutral-border border-t-default-font" />
                <span className="text-caption font-caption">Redirecting…</span>
              </div>
            </div>
          );
        }
        // DOCX / PPTX / narrative path — original chrome stays.
        return (
          <div className="relative flex h-full w-full flex-col bg-default-background">
            <div className="flex w-full items-center gap-2 border-b border-solid border-neutral-border bg-default-background px-3 py-1.5">
              <IconButton
                size="small"
                icon="FeatherArrowLeft"
                onClick={() => {
                  if (editorIsDirty) {
                    if (!window.confirm("You have unsaved changes. Leave anyway?")) return;
                  }
                  navigate(-1);
                }}
              />
              <span className="text-body-bold font-body-bold text-default-font">
                Document Editor
                {pendingProductType && !htmlContent && (
                  <span className="ml-2 text-caption font-caption text-subtext-color">
                    — {loadVerbLc} {productNameState || pendingProductType}…
                  </span>
                )}
              </span>
              {(docxLink || pptxLink || loadedProductType || pendingProductType) && (
                <div className="ml-auto flex items-center gap-1.5">
                  {loadedArtifactId && (
                    <div className="flex items-center gap-2">
                      {artifactStatus === "published" && approvedBy && (
                        <span
                          className="text-caption font-caption text-subtext-color"
                          title={
                            approvedAt
                              ? `Approved ${new Date(approvedAt).toLocaleString()}`
                              : "Approved"
                          }
                        >
                          Approved by {approvedBy}
                        </span>
                      )}
                      {/* Draft | Final segmented toggle */}
                      <div className="inline-flex items-center overflow-hidden rounded-md border border-solid border-neutral-border text-caption font-caption">
                        <button
                          type="button"
                          disabled={approvalBusy}
                          onClick={() => setApproval(false)}
                          title="Mark as draft (editable)"
                          className={
                            artifactStatus !== "published"
                              ? "px-3 py-1 bg-warning-100 text-warning-800 font-caption-bold disabled:opacity-50"
                              : "px-3 py-1 text-subtext-color hover:bg-neutral-50 disabled:opacity-50"
                          }
                        >
                          Draft
                        </button>
                        <div className="h-5 w-px bg-neutral-border" />
                        <button
                          type="button"
                          disabled={approvalBusy}
                          onClick={() => setApproval(true)}
                          title="Approve — mark this product final"
                          className={
                            artifactStatus === "published"
                              ? "px-3 py-1 bg-success-100 text-success-800 font-caption-bold disabled:opacity-50"
                              : "px-3 py-1 text-subtext-color hover:bg-neutral-50 disabled:opacity-50"
                          }
                        >
                          Final
                        </button>
                      </div>
                    </div>
                  )}
                  {loadedArtifactId && (
                    <button
                      type="button"
                      onClick={() =>
                        navigate(
                          `/missioncommand/graph-viewer?artifact_id=${encodeURIComponent(loadedArtifactId)}`,
                        )
                      }
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="View this product's COUGAAR data as a graph"
                    >
                      <SubframeCore.Icon className="text-caption" name="FeatherShare2" />
                      Data graph
                    </button>
                  )}
                  {docxLink && (
                    <a
                      href={docxLink}
                      download
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font text-default-font no-underline hover:bg-neutral-100"
                      title="Download DOCX"
                    >
                      ⬇ DOCX
                    </a>
                  )}
                  {pptxLink && (
                    <a
                      href={pptxLink}
                      download
                      className="flex items-center gap-1 rounded-md border border-solid border-neutral-border bg-neutral-50 px-2 py-1 text-caption font-caption text-default-font no-underline hover:bg-neutral-100"
                      title="Download PPTX"
                    >
                      ⬇ PPTX
                    </a>
                  )}
                </div>
              )}
            </div>

            {loadError && !lookupPending && (
              <div className="whitespace-pre-wrap px-3 py-2 text-caption text-error-700 border-b border-solid border-error-200 bg-error-50">
                {loadError}
              </div>
            )}

            {routeSessionId && (
              <div className="px-3 pt-2">
                <StageTimeline sessionId={routeSessionId} />
              </div>
            )}

            <div className="flex-1 overflow-auto">
              <DocEditor
                initialValue={htmlContent}
                htmlLink=""
                docxLink={docxLink}
                pptxLink={pptxLink}
                productName={productNameState}
                sessionId={routeSessionId}
                pendingOrientation={pendingOrientation}
                pendingMessage={
                  pendingProductType && !htmlContent && !lookupPending
                    ? `Asking the operations expert to generate ${productNameState || pendingProductType}.`
                    : ""
                }
                onDirtyChange={(dirty: boolean) => setEditorIsDirty(dirty)}
                onClose={() => navigate(-1)}
              />
            </div>
          </div>
        );
      })()}

      {/* Canonical-on-disk downstream-refire modal. Opens on a successful
          POST /api/artifacts/<id>/finalize and lists every immediate
          downstream product type whose schema declares a depends_on entry
          for the finalized product. Closing without submitting has no
          backend effect — finalize already landed when the modal opened. */}
      <FinalizeDownstreamsModal
        open={downstreamModalOpen}
        setOpen={setDownstreamModalOpen}
        upstreamProductType={finalizedProductType}
        downstreams={immediateDownstreams}
        onConfirm={refireSelectedDownstreams}
        onDone={() => {
          setDownstreamModalOpen(false);
          // replace, not push: approval completes back at the dashboard.
          // Pushing left the product page underneath, so the first Back
          // press reloaded it instead of returning to the dashboard. (The
          // duplicate history entry the Collabora iframe used to add on the
          // Draft→Final permission flip is now prevented at the source — see
          // LiveEditor's iframe `key`.)
          navigate("/missioncommand", { replace: true });
        }}
        busy={downstreamRefireBusy}
      />
    </DefaultPageLayout>
  );
}
