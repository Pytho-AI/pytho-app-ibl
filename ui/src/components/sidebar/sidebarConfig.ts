// Ported from pytho-vite packages/frontend/src/components/sidebar/
// sidebarConfig.ts (UI unification): identical structure — global items,
// the Active Event selector, event-scoped product sections (Crucible /
// Atlas / Anvil / Tome). Only the ROUTE STRINGS differ where the edge
// app's paths differ from the cloud's (events/:projectId instead of
// projects/:projectId, /missioncommand prefix), and cloud pages that
// have not been ported to the edge yet are `disabled` so the sections
// read identically on both apps and light up as the pages arrive.
import type { IconName } from "@subframe/core";

export interface SidebarNavItem {
  id: string;
  label: string;
  icon: IconName;
  route?: string;
  disabled?: boolean;
  adminOnly?: boolean;
}

export interface SidebarProductSection {
  id: string;
  label: string;
  icon: IconName;
  items: SidebarNavItem[];
  adminOnly?: boolean;
}

export const GLOBAL_NAV_ITEMS: SidebarNavItem[] = [
  { id: "eventManagement", label: "Event Management", icon: "FeatherCalendar", route: "" },
  { id: "notifications", label: "Notifications", icon: "FeatherBell", route: "/notifications" },
  { id: "doctrine", label: "Doctrine", icon: "FeatherBook", route: "/doctrines" },
  // Cloud-only page — not ported to the edge yet.
  { id: "recentActivity", label: "Recent Activity", icon: "FeatherActivity", disabled: true },
];

export const EVENT_NAV_ITEMS: SidebarNavItem[] = [
  { id: "exerciseDashboard", label: "Event Dashboard", icon: "FeatherLayout", route: "/events/:projectId/dashboard" },
];

export const EVENT_SETTINGS_SECTION: SidebarProductSection = {
  id: "eventSettings",
  label: "Event Settings",
  icon: "FeatherSettings",
  adminOnly: true,
  items: [
    // Cloud-only page — not ported to the edge yet.
    { id: "usersPermissions", label: "Users & Permissions", icon: "FeatherShield", disabled: true },
    { id: "dataManagement", label: "Data Management", icon: "FeatherDatabase", route: "/data/static" },
  ],
};

export const PRODUCT_SECTIONS: SidebarProductSection[] = [
  {
    id: "crucible",
    label: "Crucible",
    icon: "FeatherShield",
    items: [
      // Cloud-only pages — not ported to the edge yet.
      { id: "rfiManagement", label: "RFI Management", icon: "FeatherMessageCircle", disabled: true },
      { id: "injects", label: "Injects", icon: "FeatherZap", disabled: true },
      { id: "worldState", label: "World State", icon: "FeatherGlobe", disabled: true },
    ],
  },
  {
    id: "atlas",
    label: "Atlas",
    icon: "FeatherMap",
    items: [
      { id: "mapWorkspace", label: "Map Workspace", icon: "FeatherMapPin", route: "/events/:projectId/operations-map" },
      // Edge-only surface (kept reachable until the cloud grows one).
      { id: "graphViewer", label: "Graph Viewer", icon: "FeatherShare2", route: "/graph-viewer" },
    ],
  },
  {
    id: "anvil",
    label: "Anvil",
    icon: "FeatherTarget",
    items: [
      { id: "coaLifecycle", label: "COA Lifecycle", icon: "FeatherGitBranch", route: "/coa-pipeline" },
      // Cloud-only page — not ported to the edge yet.
      { id: "virtualOpt", label: "Virtual OPT", icon: "FeatherUsers", disabled: true },
    ],
  },
  {
    id: "tome",
    label: "Tome",
    icon: "FeatherBookOpen",
    items: [
      // Cloud-only page — not ported to the edge yet.
      { id: "observationsRecordings", label: "Observations & Recordings", icon: "FeatherMic", disabled: true },
      { id: "insightsReports", label: "Insights & Reports", icon: "FeatherBarChart2", route: "/events/:projectId/aar" },
    ],
  },
];

export const ENABLED_PRODUCTS = new Set(["crucible", "atlas", "anvil", "tome"]);

export function resolveRoute(route: string, projectId: string, prefix: string): string {
  return `/${prefix}${route.replace(":projectId", projectId)}`;
}

export function getRoutePrefix(_pathname: string): string {
  // The edge app has a single prefix (the cloud switches thewarroom/ndu).
  return "missioncommand";
}
