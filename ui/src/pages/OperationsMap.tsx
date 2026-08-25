"use client";

/**
 * OperationsMap — iframe shell for the geo-agent-hosted analyst map surface.
 *
 * Task 6 of the map API unification plan moved the entire map UI
 * (GeoAgentProvider + GeoAgentMap + tactical overlays + sidebar toggles
 * + right-click generate menu) into the geo-agent container, served by
 * its own React + Vite bundle under /geo-app/operations-map. This page
 * used to mount 1300+ lines of that tree directly; it's now a thin
 * outer chrome (top nav + IRC chat rail) wrapping the iframe.
 *
 * Cross-frame integration is two message types the child postMessages
 * up to us:
 *   { type: 'geoagent.navigate', url } → react-router navigate(url)
 * Everything else — REST, WebSocket, right-click, edit-map, sidebar
 * toggles — stays same-origin inside the iframe (nginx routes /api,
 * /api/v1, /geo-api are all reachable from either origin under the
 * same host).
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { Navigate, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { useAppContext } from "../lib/contextLib";
import IrcChatPanel from "../components/IrcChatPanel";

const GEO_APP_BASE = (import.meta as any).env?.VITE_GEO_APP_BASE || "/geo-app";

function OperationsMapChrome({ iframeSrc, userEmail, userFullName }:
                              { iframeSrc: string; userEmail: string;
                                userFullName: string }) {
  const [chatOpen, setChatOpen] = useState(false);
  return (
    <DefaultPageLayout>
      {/* DefaultPageLayout's content area is a `flex-col items-start`
          column flex, so `flex: 1` alone only grows this row vertically;
          the cross-axis (width) stays shrink-to-content and the
          `width:100%` iframe collapses to its 300px CSS default, hiding
          the map behind the overlay sidebar. width:100% + alignSelf
          stretch force the row to fill the column's full width. */}
      <div style={{ display: "flex", flex: 1, minHeight: 0,
                    width: "100%", alignSelf: "stretch" }}>
        <div style={{ flex: 1, minWidth: 0, position: "relative" }}>
          <iframe
            src={iframeSrc}
            title="Operations Map"
            style={{ width: "100%", height: "100%", border: 0,
                     display: "block", background: "#0b1120" }}
            allow="clipboard-read; clipboard-write; fullscreen"
          />
        </div>
        {chatOpen ? (
          <div className="relative h-full flex" style={{ flex: "0 0 340px",
                                                          minWidth: 260 }}>
            <button
              type="button"
              onClick={() => setChatOpen(false)}
              title="Close chat"
              aria-label="Close chat"
              className="absolute right-2 top-2 z-[5] flex items-center justify-center rounded border border-solid border-neutral-border bg-default-background p-1 text-subtext-color hover:bg-neutral-50"
            >
              ›
            </button>
            <div style={{ flex: 1, minWidth: 0, minHeight: 0,
                          display: "flex", flexDirection: "column" }}>
              <IrcChatPanel
                artifactId=""
                channel="#operationalmap"
                email={userEmail}
                fullName={userFullName}
              />
            </div>
          </div>
        ) : (
          <div className="relative h-full" style={{ flex: "0 0 40px" }}>
            <button
              type="button"
              data-testid="map-chat-toggle"
              onClick={() => setChatOpen(true)}
              title="Open chat"
              aria-label="Open chat"
              className="absolute right-2 top-2 z-[5] flex items-center justify-center rounded border border-solid border-neutral-border bg-default-background p-1.5 text-subtext-color shadow-sm hover:bg-neutral-50"
            >
              💬
            </button>
          </div>
        )}
      </div>
    </DefaultPageLayout>
  );
}

export default function OperationsMap() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const { projectId } = useParams<{ projectId: string }>();
  const appCtx = (useAppContext() || {}) as any;
  const user = appCtx.user || null;

  const userId = (user?.attributes?.sub || "").trim();
  const userEmail = (user?.attributes?.email || "").trim();
  const userFullName = user?.attributes?.given_name && user?.attributes?.family_name
    ? `${user.attributes.given_name} ${user.attributes.family_name}`
    : (userEmail || userId);

  // Forward every query param the analyst arrived with (bbox,
  // from_artifact, product_type, coa_id, etc.) into the iframe URL so
  // the child provider mounts against the same state.
  //
  // Maps are scoped per PROJECT/EVENT, never per user or per
  // deployment: the event map's id IS the projectId (pytho-vite
  // 2a606a2 is the canonical linkage — geo-agent auto-creates the map
  // on first open, every event member lands on the same operating
  // picture). An explicit ?map_id= deep link (per-product render maps
  // from DocEditor flows) still wins — that is a different axis than
  // the event map.
  const iframeSrc = useMemo(() => {
    const q = new URLSearchParams(params);
    if (projectId && !q.get("map_id")) q.set("map_id", projectId);
    if (userId && !q.get("user_id")) q.set("user_id", userId);
    if (userEmail && !q.get("email")) q.set("email", userEmail);
    // Declare THIS shell's graph viewer so the in-map unit panel can
    // deep-link "View data source". geo-agent never hardcodes consumer
    // routes; shells without the surface simply omit the param.
    if (!q.get("graph_viewer_url")) q.set("graph_viewer_url", "/missioncommand/graph-viewer");
    return `${GEO_APP_BASE}/operations-map?${q.toString()}`;
  }, [params, projectId, userId, userEmail]);

  // postMessage bridge — the geo-agent iframe fires
  // `{ type: 'geoagent.navigate', url }` when the analyst's right-click
  // generate flow completes and the artifact is ready to open in the
  // DocEditor. Same for the signal-report panel's "Open in editor".
  useEffect(() => {
    const onMessage = (ev: MessageEvent) => {
      const data = ev?.data;
      if (!data || typeof data !== "object") return;
      if (data.type !== "geoagent.navigate") return;
      const url = typeof data.url === "string" ? data.url : "";
      if (!url) return;
      // Same-origin child, no origin check needed for internal-mesh
      // deployment. If we ever mount the iframe cross-origin, gate on
      // ev.origin here.
      navigate(url);
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, [navigate]);

  // No active event in the URL and no explicit map deep link — nothing
  // to map. Send the analyst to the events home to pick one.
  if (!projectId && !params.get("map_id")) {
    return <Navigate to="/missioncommand" replace />;
  }

  return (
    <OperationsMapChrome
      iframeSrc={iframeSrc}
      userEmail={userEmail}
      userFullName={userFullName}
    />
  );
}
