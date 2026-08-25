"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import * as SubframeCore from "@subframe/core";
import ForceGraph2D from "react-force-graph-2d";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { Loader } from "../subframe/components/Loader";
import { useTheme } from "../lib/themeLib";

type Product = {
  artifactId: string;
  productType: string;
  generatedAt: string;
  tools: string[];
};

type GNode = {
  id: string;
  label: string;
  type: string;
  subtype: string | null;
  attrs: Record<string, any>;
};
type GEdge = { id: string; source: string; target: string; kind: string };
type Graph = {
  nodes: GNode[];
  edges: GEdge[];
  stats: Record<string, { type: string; total: number; shown: number }>;
};

const TYPE_COLORS: Record<string, string> = {
  unit: "#3b82f6",
  target: "#ef4444",
  observation: "#f59e0b",
  control_measure: "#a855f7",
  civil: "#22c55e",
};
const TYPE_LABELS: Record<string, string> = {
  unit: "Units",
  target: "Targets",
  observation: "Intel",
  control_measure: "Control measures",
  civil: "Civil",
};
const colorFor = (t: string) => TYPE_COLORS[t] || "#94a3b8";

// MIL-STD-2525 standard-identity colors, mirroring the operations map's
// AFFILIATION_COLORS (geo-agent/utils/mapStyles.js) so the graph reads the
// same as the map: hostile red, friend blue, neutral green, unknown amber.
const AFFILIATION_COLORS: Record<string, string> = {
  Hostile: "#ef4444",
  Friend: "#3b82f6",
  Neutral: "#22c55e",
  Unknown: "#eab308",
};

// Resolve a unit's affiliation the same way the backend does
// (cougaar_adapters._transform): explicit attrs.affiliation first, then the
// SIDC standard-identity char (position 1), then the BLUE/RED force_type.
function affiliationOf(attrs: Record<string, unknown> | undefined): string | null {
  const a = String(attrs?.affiliation || "").trim().toLowerCase();
  if (a.startsWith("host") || a.startsWith("enem")) return "Hostile";
  if (a.startsWith("frie")) return "Friend";
  if (a.startsWith("neut")) return "Neutral";
  if (a.startsWith("unk")) return "Unknown";
  const sidc = String(attrs?.sidc || "");
  const si = sidc.length > 1 ? sidc[1].toUpperCase() : "";
  if (si === "3" || si === "F") return "Friend";
  if (si === "6" || si === "H") return "Hostile";
  if (si === "4" || si === "N") return "Neutral";
  if (si === "1" || si === "U") return "Unknown";
  const force = String(attrs?.force_type || "").toUpperCase();
  if (force === "RED") return "Hostile";
  if (force === "BLUE") return "Friend";
  return null;
}

// Node fill color: units are colored by affiliation (enemy = red, friendly =
// blue); every other entity type keeps its fixed type color. Units whose
// affiliation can't be determined fall back to the type color (blue).
function colorForNode(n: { type: string; attrs?: Record<string, unknown> }): string {
  if (n.type === "unit") {
    const aff = affiliationOf(n.attrs);
    if (aff && AFFILIATION_COLORS[aff]) return AFFILIATION_COLORS[aff];
  }
  return colorFor(n.type);
}

// Picker sentinel: this value = the live COUGAAR view; any real artifact id =
// that product's saved snapshot.
const LIVE_SOURCE = "__live__";

// Edge relationship -> color + human label, so it's clear HOW two nodes connect.
const EDGE_COLORS: Record<string, string> = {
  parent: "#64748b",
  observes: "#f59e0b",
  linked_unit: "#ef4444",
  parent_system: "#a855f7",
};
const EDGE_LABELS: Record<string, string> = {
  parent: "subordinate → superior",
  observes: "observed by",
  linked_unit: "linked unit",
  parent_system: "parent target system",
};

function GraphViewer() {
  // Deep link: /missioncommand/graph-viewer?artifact_id=<id> opens straight
  // onto that product (used by the dashboard row's "view data" icon).
  const [searchParams] = useSearchParams();
  const [products, setProducts] = useState<Product[]>([]);
  // Default to Live unless deep-linked to a specific product.
  const [artifactId, setArtifactId] = useState<string>(
    () => searchParams.get("artifact_id") || LIVE_SOURCE,
  );
  const [reloadKey, setReloadKey] = useState(0);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<GNode | null>(null);
  // Entity sidebar: text search, per-type list collapse (list-only, separate
  // from `hidden` which controls canvas visibility), and a whole-sidebar toggle.
  const [entityQuery, setEntityQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [sidebarOpen, setSidebarOpen] = useState(true);

  // Measure the canvas container so ForceGraph2D gets explicit dimensions
  // (it defaults to window size otherwise and overflows the layout).
  const { theme } = useTheme();
  const dark = theme === "dark";
  const canvasWrap = useRef<HTMLDivElement | null>(null);
  const [dims, setDims] = useState({ w: 0, h: 0 });
  // Paint the canvas to match the container's themed background so the graph
  // follows light/dark mode. Re-read when the theme flips.
  const [bgColor, setBgColor] = useState("#f8fafc");
  useLayoutEffect(() => {
    const el = canvasWrap.current;
    if (!el) return;
    const measure = () => setDims({ w: el.clientWidth, h: el.clientHeight });
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    measure();
    return () => ro.disconnect();
  }, []);
  useEffect(() => {
    // Defer to the next frame: the theme class lands on <html> via another
    // effect, which may run AFTER this one. Reading synchronously here samples
    // the PREVIOUS theme's color, so the canvas lags one toggle behind. A
    // rAF runs after that class flip + style recalc, so we read the new color.
    const id = requestAnimationFrame(() => {
      const el = canvasWrap.current;
      if (el) setBgColor(getComputedStyle(el).backgroundColor || "#f8fafc");
    });
    return () => cancelAnimationFrame(id);
  }, [theme]);

  // Load the product picker on mount; default to the newest.
  useEffect(() => {
    (async () => {
      try {
        const r = await fetch("/api/graph/products");
        if (!r.ok) return;
        const d = await r.json();
        const ps: Product[] = Array.isArray(d?.products) ? d.products : [];
        setProducts(ps);
        // Honor a deep-linked artifact; otherwise default to the newest.
        if (ps.length && !artifactId) setArtifactId(ps[0].artifactId);
      } catch {
        /* ignore */
      }
    })();
  }, []);

  useEffect(() => {
    if (!artifactId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setSelected(null);
    (async () => {
      try {
        const url =
          artifactId === LIVE_SOURCE
            ? "/api/graph/live_all"
            : `/api/graph?artifact_id=${encodeURIComponent(artifactId)}`;
        const r = await fetch(url);
        if (!r.ok) {
          if (!cancelled)
            setError(
              r.status === 404
                ? "No COUGAAR data was saved for this product."
                : `Graph request failed (HTTP ${r.status}).`,
            );
          return;
        }
        const d = await r.json();
        if (!cancelled) setGraph(d);
      } catch (e: any) {
        if (!cancelled) setError(e?.message || "Failed to load graph.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [artifactId, reloadKey]);

  const presentTypes = useMemo(() => {
    const s = new Set<string>();
    (graph?.nodes || []).forEach((n) => s.add(n.type));
    return Array.from(s);
  }, [graph]);

  // Sidebar groups: one bucket per present type, each text-filtered by the
  // search box (label, then attrs.name / id as fallbacks). `total` is the
  // unfiltered size so the header can show "matches / total" while searching.
  const entityGroups = useMemo(() => {
    const q = entityQuery.trim().toLowerCase();
    return presentTypes.map((t) => {
      const all = (graph?.nodes || []).filter((n) => n.type === t);
      const items = q
        ? all.filter(
            (n) =>
              (n.label || "").toLowerCase().includes(q) ||
              String(n.attrs?.name || "").toLowerCase().includes(q) ||
              n.id.toLowerCase().includes(q),
          )
        : all;
      return { type: t, total: all.length, items };
    });
  }, [graph, presentTypes, entityQuery]);

  // Visible nodes/edges after the type filter, in react-force-graph shape.
  const data = useMemo(() => {
    const nodes = (graph?.nodes || [])
      .filter((n) => !hidden.has(n.type))
      .map((n) => ({ ...n, color: colorForNode(n) }));
    const ids = new Set(nodes.map((n) => n.id));
    const links = (graph?.edges || [])
      .filter((e) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({ source: e.source, target: e.target, kind: e.kind }));
    return { nodes, links };
  }, [graph, hidden]);

  // Ref to the force graph so we can animate the camera ("walk" to a node).
  const fgRef = useRef<any>(null);
  // Deep-link focus (?focus_kbid=<cougaar kbID>): the unit a map-panel
  // "View data source (Cougaar)" link points at. Node id uses data__ID, so we
  // match on attrs.uuid (== kbID). Selection happens as soon as the graph
  // loads; the camera can only center once the force sim has positioned nodes,
  // so the pending node id is centered from onEngineStop. One-shot via the
  // consumed flag so filter/source changes don't yank the camera again.
  const pendingCenterRef = useRef<string | null>(null);
  const focusConsumedRef = useRef(false);
  // id -> live node object (has x/y after the sim) for the visible set.
  const visibleNodeById = useMemo(
    () => new Map(data.nodes.map((n) => [n.id, n] as const)),
    [data],
  );
  // id -> node for ALL nodes (labels for connections that may be filtered out).
  const allNodesById = useMemo(
    () => new Map((graph?.nodes || []).map((n) => [n.id, n] as const)),
    [graph],
  );
  // Edges touching the selected node, for the side-panel "Connections" list.
  const connections = useMemo(() => {
    if (!selected || !graph) return [] as { otherId: string; kind: string; dir: "in" | "out" }[];
    const out: { otherId: string; kind: string; dir: "in" | "out" }[] = [];
    for (const e of graph.edges) {
      if (e.source === selected.id) out.push({ otherId: e.target, kind: e.kind, dir: "out" });
      else if (e.target === selected.id) out.push({ otherId: e.source, kind: e.kind, dir: "in" });
    }
    return out;
  }, [selected, graph]);
  const neighborIds = useMemo(
    () => new Set(connections.map((c) => c.otherId)),
    [connections],
  );

  // Select a node and animate the camera to it ("walk the graph").
  function walkTo(id: string) {
    const n: any = visibleNodeById.get(id);
    if (!n) return;
    setSelected(n as GNode);
    const fg = fgRef.current;
    if (fg && typeof n.x === "number") {
      fg.centerAt(n.x, n.y, 600);
      fg.zoom(2.5, 600);
    }
  }

  function toggleType(t: string) {
    setHidden((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  }

  function toggleGroupCollapse(t: string) {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      next.has(t) ? next.delete(t) : next.add(t);
      return next;
    });
  }

  // Focus an entity picked from the sidebar: always select it (so the detail
  // panel opens, even for a type currently hidden from the canvas), and center
  // + zoom the camera when the node is on-canvas and has simulated coordinates.
  function focusEntity(id: string) {
    const visible: any = visibleNodeById.get(id);
    const n = (visible || allNodesById.get(id)) as GNode | undefined;
    if (!n) return;
    setSelected(n);
    const fg = fgRef.current;
    if (fg && visible && typeof visible.x === "number") {
      fg.centerAt(visible.x, visible.y, 600);
      fg.zoom(2.5, 600);
    }
  }

  // Auto-focus the deep-linked unit once its graph has loaded. Match on
  // attrs.uuid (the cougaar kbID the map panel passes); fall back to the node
  // id (`unit:<kbID>` when data__ID is absent) or an exact name match. Select
  // immediately so the detail panel opens; defer centering to onEngineStop
  // (nodes have no x/y until the sim runs).
  useEffect(() => {
    if (!graph || focusConsumedRef.current) return;
    const kbid = (searchParams.get("focus_kbid") || "").trim();
    if (!kbid) return;
    const nodes = graph.nodes || [];
    const node =
      nodes.find((n) => n.type === "unit" && n.attrs?.uuid === kbid) ||
      nodes.find((n) => n.attrs?.uuid === kbid) ||
      nodes.find((n) => n.id === `unit:${kbid}`) ||
      nodes.find((n) => (n.attrs?.name || n.label) === kbid);
    if (!node) return;
    focusConsumedRef.current = true;
    setSelected(node);
    pendingCenterRef.current = node.id;
  }, [graph, searchParams]);

  const totalShown = graph
    ? Object.values(graph.stats).reduce((a, s) => a + (s.shown || 0), 0)
    : 0;
  const totalAll = graph
    ? Object.values(graph.stats).reduce((a, s) => a + (s.total || 0), 0)
    : 0;
  const truncated = totalAll > totalShown;

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full min-w-0 flex-col overflow-hidden">
        {/* Top bar */}
        <div className="flex w-full flex-wrap items-center gap-3 border-b border-solid border-neutral-border px-6 py-3">
          <span className="text-heading-3 font-heading-3 text-default-font">
            Graph Viewer
          </span>
          <select
            className="h-8 rounded-md border border-solid border-neutral-border bg-default-background px-2 text-body font-body text-default-font"
            value={artifactId}
            onChange={(e) => setArtifactId(e.target.value)}
          >
            <option value={LIVE_SOURCE}>● Live — current COUGAAR</option>
            {products.map((p) => (
              <option key={p.artifactId} value={p.artifactId}>
                {p.productType || "—"} ·{" "}
                {p.generatedAt ? new Date(p.generatedAt).toLocaleString() : p.artifactId}
              </option>
            ))}
          </select>
          <button
            type="button"
            onClick={() => setReloadKey((k) => k + 1)}
            title={artifactId === LIVE_SOURCE ? "Re-query COUGAAR" : "Reload"}
            className="flex h-8 items-center gap-1 rounded-md border border-solid border-neutral-border bg-default-background px-2 text-caption font-caption text-default-font hover:bg-neutral-50"
          >
            <SubframeCore.Icon name="FeatherRefreshCw" />
            {artifactId === LIVE_SOURCE ? "Refresh" : "Reload"}
          </button>

          <div className="ml-auto text-caption font-caption text-subtext-color">
            {loading
              ? "Loading…"
              : `${data.nodes.length} shown${truncated ? ` · capped from ${totalAll}` : ""}`}
          </div>
        </div>

        {/* Graph + detail */}
        <div className="flex w-full flex-1 min-h-0 min-w-0 overflow-hidden">
          {/* Entity sidebar — searchable, grouped by type. Clicking a row
              focuses that node; the group eye toggle hides/shows that type on
              the canvas (same state as the old top chips). */}
          {sidebarOpen ? (
            <aside className="flex w-72 flex-shrink-0 flex-col border-r border-solid border-neutral-border bg-default-background">
              <div className="flex items-center gap-2 border-b border-solid border-neutral-border px-3 py-2">
                <span className="text-caption-bold font-caption-bold text-default-font">
                  Entities
                </span>
                <span className="text-caption font-caption text-subtext-color">
                  {graph?.nodes.length || 0}
                </span>
                <div className="grow" />
                <button
                  type="button"
                  onClick={() => setSidebarOpen(false)}
                  title="Collapse sidebar"
                  className="rounded p-1 text-subtext-color hover:bg-neutral-50"
                >
                  <SubframeCore.Icon name="FeatherChevronsLeft" />
                </button>
              </div>
              <div className="px-3 py-2">
                <div className="flex h-8 items-center gap-2 rounded-md border border-solid border-neutral-border bg-default-background px-2">
                  <SubframeCore.Icon name="FeatherSearch" className="text-subtext-color" />
                  <input
                    value={entityQuery}
                    onChange={(e) => setEntityQuery(e.target.value)}
                    placeholder="Search entities…"
                    className="w-full bg-transparent text-body font-body text-default-font outline-none placeholder:text-neutral-400"
                  />
                  {entityQuery && (
                    <button
                      type="button"
                      onClick={() => setEntityQuery("")}
                      title="Clear"
                      className="text-subtext-color hover:text-default-font"
                    >
                      <SubframeCore.Icon name="FeatherX" />
                    </button>
                  )}
                </div>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
                {entityGroups.map(({ type, total, items }) => {
                  const off = hidden.has(type);
                  const searching = entityQuery.trim().length > 0;
                  const collapsed = !searching && collapsedGroups.has(type);
                  return (
                    <div key={type} className="mb-1">
                      <div
                        className={
                          "flex items-center gap-1.5 rounded-md px-1.5 py-1 " +
                          (off ? "opacity-50" : "")
                        }
                      >
                        <button
                          type="button"
                          onClick={() => toggleGroupCollapse(type)}
                          title={collapsed ? "Expand" : "Collapse"}
                          className="text-subtext-color hover:text-default-font"
                        >
                          <SubframeCore.Icon
                            name={collapsed ? "FeatherChevronRight" : "FeatherChevronDown"}
                          />
                        </button>
                        <span
                          className="h-2.5 w-2.5 flex-none rounded-full"
                          style={{ backgroundColor: colorFor(type) }}
                        />
                        <span className="text-caption-bold font-caption-bold text-default-font">
                          {TYPE_LABELS[type] || type}
                        </span>
                        <span className="text-caption font-caption text-subtext-color">
                          {searching ? `${items.length}/${total}` : total}
                        </span>
                        <div className="grow" />
                        <button
                          type="button"
                          onClick={() => toggleType(type)}
                          title={off ? "Show on canvas" : "Hide from canvas"}
                          className="rounded p-0.5 text-subtext-color hover:bg-neutral-50"
                        >
                          <SubframeCore.Icon name={off ? "FeatherEyeOff" : "FeatherEye"} />
                        </button>
                      </div>
                      {!collapsed &&
                        items.map((n) => {
                          const isSel = selected?.id === n.id;
                          return (
                            <button
                              key={n.id}
                              type="button"
                              onClick={() => focusEntity(n.id)}
                              title={n.label}
                              className={
                                "flex w-full items-center gap-2 rounded-md py-1 pl-7 pr-2 text-left " +
                                (isSel ? "bg-brand-50" : "hover:bg-neutral-50")
                              }
                            >
                              <span className="truncate text-body font-body text-default-font">
                                {n.label}
                              </span>
                              {n.subtype && (
                                <span className="ml-auto flex-none rounded bg-neutral-100 px-1 text-caption font-caption text-subtext-color">
                                  {n.subtype}
                                </span>
                              )}
                            </button>
                          );
                        })}
                      {!collapsed && searching && items.length === 0 && (
                        <div className="py-1 pl-7 text-caption font-caption text-subtext-color">
                          No matches
                        </div>
                      )}
                    </div>
                  );
                })}
                {(!graph || graph.nodes.length === 0) && (
                  <div className="px-2 py-3 text-caption font-caption text-subtext-color">
                    No entities loaded.
                  </div>
                )}
              </div>
            </aside>
          ) : (
            <div className="flex w-8 flex-shrink-0 flex-col items-center border-r border-solid border-neutral-border bg-default-background py-2">
              <button
                type="button"
                onClick={() => setSidebarOpen(true)}
                title="Show entities"
                className="rounded p-1 text-subtext-color hover:bg-neutral-50"
              >
                <SubframeCore.Icon name="FeatherChevronsRight" />
              </button>
            </div>
          )}
          <div ref={canvasWrap} className="relative flex-1 min-w-0 overflow-hidden bg-neutral-50">
            {loading && (
              <div className="absolute inset-0 z-10 flex items-center justify-center">
                <Loader size="large" />
              </div>
            )}
            {error && (
              <div className="absolute inset-0 z-10 flex items-center justify-center">
                <span className="text-body font-body text-error-700">{error}</span>
              </div>
            )}
            {!loading && !error && data.nodes.length === 0 && (
              <div className="absolute inset-0 z-10 flex items-center justify-center">
                <span className="text-body font-body text-subtext-color">
                  No COUGAAR entities for this product.
                </span>
              </div>
            )}
            {data.nodes.length > 0 && dims.w > 0 && (
              <ForceGraph2D
                ref={fgRef}
                graphData={data}
                width={dims.w}
                height={dims.h}
                backgroundColor={bgColor}
                nodeId="id"
                nodeRelSize={5}
                nodeLabel={(n: any) => `${n.label}  ·  ${n.type}`}
                nodeCanvasObject={(node: any, ctx: any, globalScale: number) => {
                  const r = 5;
                  ctx.beginPath();
                  ctx.arc(node.x, node.y, r, 0, 2 * Math.PI);
                  ctx.fillStyle = node.color;
                  ctx.fill();
                  const isSel = selected?.id === node.id;
                  if (isSel) {
                    ctx.lineWidth = 2.5 / globalScale;
                    ctx.strokeStyle = dark ? "#e2e8f0" : "#0f172a";
                    ctx.stroke();
                  }
                  // Label the focused node + its neighbors always; the rest
                  // only once zoomed in, so it's not a wall of text far out.
                  if (isSel || neighborIds.has(node.id) || globalScale > 1.3) {
                    const fontSize = Math.max(10 / globalScale, 1.5);
                    const raw = node.label || "";
                    const label = raw.length > 26 ? raw.slice(0, 25) + "…" : raw;
                    ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
                    ctx.textAlign = "center";
                    ctx.textBaseline = "top";
                    ctx.fillStyle = isSel
                      ? dark ? "#f8fafc" : "#0f172a"
                      : dark ? "#cbd5e1" : "#475569";
                    ctx.fillText(label, node.x, node.y + r + 1);
                  }
                }}
                nodePointerAreaPaint={(node: any, color: string, ctx: any) => {
                  ctx.fillStyle = color;
                  ctx.beginPath();
                  ctx.arc(node.x, node.y, 6, 0, 2 * Math.PI);
                  ctx.fill();
                }}
                linkColor={(l: any) => EDGE_COLORS[l.kind] || (dark ? "#475569" : "#cbd5e1")}
                linkWidth={1.5}
                linkLabel={(l: any) => EDGE_LABELS[l.kind] || l.kind}
                linkDirectionalArrowLength={4}
                linkDirectionalArrowRelPos={1}
                onNodeClick={(n: any) => setSelected(n as GNode)}
                onLinkClick={(link: any) => {
                  const t =
                    typeof link.target === "object"
                      ? link.target
                      : visibleNodeById.get(link.target);
                  if (t) walkTo(t.id);
                }}
                onBackgroundClick={() => setSelected(null)}
                cooldownTicks={120}
                onEngineStop={() => {
                  // Sim settled → nodes have coordinates. Center the
                  // deep-link target once (then clear so it's a one-shot).
                  const id = pendingCenterRef.current;
                  if (id) {
                    pendingCenterRef.current = null;
                    focusEntity(id);
                  }
                }}
              />
            )}

            {/* Detail panel — overlay so it never changes the canvas width. */}
            {selected && (
              <aside className="absolute right-0 top-0 z-20 flex h-full w-80 flex-col gap-3 overflow-y-auto border-l border-solid border-neutral-border bg-default-background px-4 py-4 shadow-lg">
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  aria-label="Close"
                  className="absolute right-2 top-2 rounded p-1 text-caption text-subtext-color hover:bg-neutral-50"
                >
                  ✕
                </button>
              <div className="flex items-center gap-2">
                <span
                  className="h-3 w-3 rounded-full"
                  style={{ backgroundColor: colorForNode(selected) }}
                />
                <span className="text-caption font-caption uppercase tracking-wider text-subtext-color">
                  {selected.type}
                  {selected.subtype ? ` · ${selected.subtype}` : ""}
                </span>
              </div>
              <span className="text-heading-3 font-heading-3 text-default-font break-words">
                {selected.label}
              </span>
              <div className="flex flex-col gap-1.5">
                {Object.entries(selected.attrs || {}).map(([k, v]) => (
                  <div key={k} className="flex flex-col">
                    <span className="text-caption font-caption text-subtext-color">
                      {k}
                    </span>
                    <span className="text-body font-body text-default-font break-words">
                      {typeof v === "object" ? JSON.stringify(v) : String(v)}
                    </span>
                  </div>
                ))}
              </div>

              {connections.length > 0 && (
                <div className="flex flex-col gap-1">
                  <span className="text-caption font-caption uppercase tracking-wider text-subtext-color">
                    Connections ({connections.length})
                  </span>
                  {connections.map((c, i) => {
                    const other = allNodesById.get(c.otherId);
                    const visible = visibleNodeById.has(c.otherId);
                    return (
                      <button
                        key={i}
                        type="button"
                        disabled={!visible}
                        onClick={() => walkTo(c.otherId)}
                        title={visible ? "Walk to this node" : "Hidden by a type filter"}
                        className="flex items-center gap-2 rounded-md border border-solid border-neutral-border px-2 py-1.5 text-left hover:bg-neutral-50 disabled:opacity-40"
                      >
                        <span style={{ color: EDGE_COLORS[c.kind] || "#64748b" }}>
                          {c.dir === "out" ? "→" : "←"}
                        </span>
                        <span className="flex flex-col">
                          <span className="text-caption font-caption text-subtext-color">
                            {EDGE_LABELS[c.kind] || c.kind}
                          </span>
                          <span className="text-body font-body text-default-font break-words">
                            {other?.label || c.otherId}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
              </aside>
            )}
          </div>
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default GraphViewer;
