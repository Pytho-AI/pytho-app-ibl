import React from "react";
import * as SubframeCore from "@subframe/core";
import { useNavigate, useLocation } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";

interface AppSection {
  id: string;
  label: string;
  icon: string;
  route: string | null;
}

const APP_SECTIONS: AppSection[] = [
  { id: "home", label: "Home", icon: "FeatherHome", route: "/missioncommand" },
  // Atlas is EVENT-scoped — the route template resolves against the
  // active projectId at click time (no event selected -> events home).
  { id: "atlas", label: "Atlas", icon: "FeatherMap", route: "/missioncommand/events/:projectId/operations-map" },
  { id: "anvil", label: "Anvil", icon: "FeatherTarget", route: "/missioncommand/coa-pipeline" },
  { id: "crucible", label: "Crucible", icon: "FeatherShield", route: null },
  { id: "tome", label: "Tome", icon: "FeatherBookOpen", route: "/missioncommand/session" },
];

function getActiveApp(pathname: string): string {
  if (pathname.includes("/operations-map")) return "atlas";
  if (pathname.includes("/coa-pipeline")) return "anvil";
  if (pathname.includes("/session") || pathname.includes("/agents")) return "tome";
  if (pathname === "/" || pathname === "/missioncommand" || pathname.includes("/whitecell") || pathname.includes("/events")) return "home";
  return "home";
}

export function AppRail() {
  const navigate = useNavigate();
  const location = useLocation();
  const { projectId } = (useAppContext() || {}) as any;
  const activeApp = getActiveApp(location.pathname);

  // Substitute the active event into project-scoped route templates.
  // Without an active event there is no event map — land on the events
  // home so the analyst picks one.
  const resolveRoute = (route: string): string => {
    if (!route.includes(":projectId")) return route;
    if (!projectId || projectId === "default") return "/missioncommand";
    return route.replace(":projectId", projectId);
  };

  return (
    <div className="flex flex-col items-center w-[60px] min-w-[60px] bg-neutral-50 border-r border-solid border-neutral-border py-4 gap-1 mobile:hidden">
      {APP_SECTIONS.map((section) => {
        const isActive = section.id === activeApp;
        const isDisabled = section.route === null;

        return (
          <button
            key={section.id}
            onClick={() => {
              if (section.route) {
                navigate(resolveRoute(section.route));
              }
            }}
            disabled={isDisabled}
            className={SubframeCore.twClassNames(
              "flex flex-col items-center justify-center gap-0.5 w-full py-2 px-1 rounded-md border-0 bg-transparent cursor-pointer transition-colors",
              isActive
                ? "text-brand-700 bg-brand-100"
                : "text-subtext-color hover:text-default-font hover:bg-neutral-100",
              isDisabled && "opacity-40 cursor-not-allowed hover:bg-transparent hover:text-subtext-color"
            )}
            title={isDisabled ? `${section.label} (coming soon)` : section.label}
          >
            <SubframeCore.Icon
              className="text-[20px]"
              name={section.icon}
            />
            <span className="text-[9px] font-medium leading-tight mt-0.5">
              {section.label}
            </span>
          </button>
        );
      })}
    </div>
  );
}
