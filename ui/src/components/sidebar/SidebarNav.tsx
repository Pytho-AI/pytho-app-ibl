// Ported from pytho-vite packages/frontend/src/components/sidebar/
// SidebarNav.tsx (UI unification) — identical component; the only edge
// adaptations are the context-field defaults, fetch in place of the
// cloud apiClient, and edge route shapes (see sidebarConfig.ts).
import React, { useState, useEffect, useCallback, useMemo } from "react";
import * as SubframeCore from "@subframe/core";
import { SidebarWithNestedSectionsAndSearch } from "../../subframe/components/SidebarWithNestedSectionsAndSearch";
import { Select } from "../../subframe/components/Select";
import { DropdownMenu } from "../../subframe/components/DropdownMenu";
import { useNavigate, useLocation } from "react-router-dom";
import { useAppContext } from "../../lib/contextLib";
import { DisabledNavItem } from "./DisabledNavItem";
import {
  GLOBAL_NAV_ITEMS,
  EVENT_NAV_ITEMS,
  EVENT_SETTINGS_SECTION,
  PRODUCT_SECTIONS,
  ENABLED_PRODUCTS,
  resolveRoute,
  getRoutePrefix,
  type SidebarNavItem,
  type SidebarProductSection,
} from "./sidebarConfig";

interface SidebarNavProps {
  mode: "expanded" | "collapsed" | "mobile";
  onFeedbackClick: () => void;
  onNavigate?: () => void;
}

function getSelectedId(pathname: string): string {
  if (pathname.includes("/events/") && pathname.includes("/dashboard")) return "exerciseDashboard";
  if (pathname.includes("/events/") && pathname.includes("/aar")) return "insightsReports";
  if (pathname.includes("/doctrines")) return "doctrine";
  if (pathname.includes("/data/static")) return "dataManagement";
  if (pathname.includes("/operations-map")) return "mapWorkspace";
  if (pathname.includes("/graph-viewer")) return "graphViewer";
  if (pathname.includes("/coa-pipeline")) return "coaLifecycle";
  if (pathname.includes("/notifications")) return "notifications";
  if (pathname === "/missioncommand" || pathname.includes("/whitecell")) return "eventManagement";
  return "";
}

function sectionContainsSelected(section: SidebarProductSection, selectedId: string): boolean {
  return section.items.some((item) => item.id === selectedId);
}

export function SidebarNav({ mode, onFeedbackClick, onNavigate }: SidebarNavProps) {
  // Edge context carries a subset of the cloud's fields — the missing
  // ones (eventsRefreshTrigger, unreadNotificationCount) default inert.
  const appContext = (useAppContext() || {}) as any;
  const { user, projectId, setProjectId, setProjectMap } = appContext;
  const eventsRefreshTrigger = appContext.eventsRefreshTrigger ?? 0;
  const unreadNotificationCount = appContext.unreadNotificationCount ?? 0;
  const nav = useNavigate();
  const location = useLocation();
  const selectedId = getSelectedId(location.pathname);
  const prefix = getRoutePrefix(location.pathname);
  const [projects, setProjects] = useState<any[]>([]);
  const [loadingProjects, setLoadingProjects] = useState(false);

  const isAdmin = user?.role?.role !== "participant";
  const hasActiveEvent = projectId && projectId !== "default";

  const allSectionIds = useMemo(() => {
    const ids = [EVENT_SETTINGS_SECTION.id];
    PRODUCT_SECTIONS.filter((s) => ENABLED_PRODUCTS.has(s.id)).forEach((s) => ids.push(s.id));
    return ids;
  }, []);

  const [openSections, setOpenSections] = useState<Set<string>>(() => {
    try {
      const saved = sessionStorage.getItem("sidebarSections");
      if (saved) return new Set(JSON.parse(saved));
    } catch { /* ignore */ }
    return new Set(allSectionIds);
  });

  useEffect(() => {
    sessionStorage.setItem("sidebarSections", JSON.stringify([...openSections]));
  }, [openSections]);

  useEffect(() => {
    const sectionForSelected = [...PRODUCT_SECTIONS, EVENT_SETTINGS_SECTION].find((s) =>
      s.items.some((item) => item.id === selectedId)
    );
    if (sectionForSelected && !openSections.has(sectionForSelected.id)) {
      setOpenSections((prev) => new Set([...prev, sectionForSelected.id]));
    }
  }, [selectedId]);

  const toggleSection = useCallback((sectionId: string, open: boolean) => {
    setOpenSections((prev) => {
      const next = new Set(prev);
      if (open) next.add(sectionId);
      else next.delete(sectionId);
      return next;
    });
  }, []);

  useEffect(() => {
    if (user?.userId || user?.email) loadProjects();
  }, [user?.userId, user?.email, eventsRefreshTrigger]);

  async function loadProjects() {
    if (!user?.userId && !user?.email) return;
    setLoadingProjects(true);
    try {
      const resp = await fetch(
        `/api/projects/${encodeURIComponent(user.userId || user.email)}`);
      const fetched = resp.ok ? await resp.json() : [];
      setProjects(fetched || []);
      setProjectMap((currentMap: any) => {
        const updatedMap: any = { ...currentMap };
        for (const project of fetched) {
          if (!updatedMap[project.projectId]?.projectDetails) {
            updatedMap[project.projectId] = { projectDetails: project };
          }
        }
        if (currentMap?.default) updatedMap.default = currentMap.default;
        return updatedMap;
      });
    } catch {
      // keep existing on error
    } finally {
      setLoadingProjects(false);
    }
  }

  const handleNav = useCallback(
    (route: string) => {
      onNavigate?.();
      nav(resolveRoute(route, projectId || "", prefix));
    },
    [nav, projectId, prefix, onNavigate]
  );

  const handleItemClick = useCallback(
    (item: SidebarNavItem) => {
      if (item.disabled || !item.route) return;
      handleNav(item.route);
    },
    [handleNav]
  );

  const handleGlobalItemClick = useCallback(
    (item: SidebarNavItem) => {
      if (item.disabled || !item.route) return;
      if (item.id === "eventManagement") {
        // Edge events home (WhiteCellHome) — no participant dashboard
        // or projects list page on the edge yet.
        setProjectId("default");
        onNavigate?.();
        nav(`/${prefix}`);
        return;
      }
      handleNav(item.route);
    },
    [handleNav, setProjectId, user, prefix, nav, onNavigate]
  );

  const handleEventSelect = useCallback(
    (eventId: string) => {
      setProjectId(eventId);
      onNavigate?.();
      const currentPath = location.pathname;
      if (projectId && projectId !== "default" && currentPath.includes(projectId)) {
        nav(currentPath.replace(projectId, eventId));
      } else {
        nav(`/${prefix}/events/${eventId}/dashboard`);
      }
    },
    [setProjectId, nav, prefix, onNavigate, location.pathname, projectId]
  );

  const handleFeedback = useCallback(() => {
    onNavigate?.();
    onFeedbackClick();
  }, [onNavigate, onFeedbackClick]);

  if (mode === "collapsed") {
    return <CollapsedNav
      selectedId={selectedId}
      hasActiveEvent={hasActiveEvent}
      isAdmin={isAdmin}
      projects={projects}
      projectId={projectId}
      loadingProjects={loadingProjects}
      unreadNotificationCount={unreadNotificationCount}
      onGlobalItemClick={handleGlobalItemClick}
      onItemClick={handleItemClick}
      onEventSelect={handleEventSelect}
      onFeedbackClick={handleFeedback}
    />;
  }

  return (
    <>
      {/* Global Section */}
      <div className="flex w-full flex-col items-start gap-1">
        {GLOBAL_NAV_ITEMS.map((item) =>
          item.disabled ? (
            <DisabledNavItem key={item.id} icon={item.icon} label={item.label} />
          ) : (
            <SidebarWithNestedSectionsAndSearch.NavItem
              key={item.id}
              icon={item.icon}
              selected={selectedId === item.id && !hasActiveEvent}
              onClick={() => handleGlobalItemClick(item)}
              rightSlot={
                item.id === "notifications" && unreadNotificationCount > 0 ? (
                  <span className="flex items-center justify-center min-w-[20px] h-[20px] px-1.5 rounded-full bg-error-600 text-white text-xs font-bold">
                    {unreadNotificationCount > 99 ? "99+" : unreadNotificationCount}
                  </span>
                ) : undefined
              }
            >
              {item.label}
            </SidebarWithNestedSectionsAndSearch.NavItem>
          )
        )}
      </div>

      {/* Divider */}
      <div className="flex w-full px-3 py-2">
        <div className="h-px w-full bg-neutral-border" />
      </div>

      {/* Event Selector */}
      <div className="flex w-full flex-col items-start gap-1">
        <div className="flex w-full flex-col items-start gap-1 px-3 py-1">
          <span className="w-full text-caption-bold font-caption-bold text-subtext-color">
            Active Event
          </span>
        </div>
        <div className="w-full px-1">
          <Select
            placeholder={loadingProjects ? "Loading..." : "Select an event"}
            value={hasActiveEvent ? projectId : undefined}
            onValueChange={handleEventSelect}
            className={SubframeCore.twClassNames(
              "w-full",
              hasActiveEvent && "[&>div]:border-brand-500 [&>div]:bg-brand-50 [&_span]:text-brand-700 [&_span]:font-semibold"
            )}
          >
            {projects.map((project: any) => (
              <Select.Item key={project.projectId} value={project.projectId}>
                {project.name}
              </Select.Item>
            ))}
          </Select>
        </div>
      </div>

      {/* Event-Scoped Top-Level Items */}
      {hasActiveEvent && (
        <div className="flex w-full flex-col items-start gap-1 pt-2">
          {EVENT_NAV_ITEMS.map((item) => (
            <SidebarWithNestedSectionsAndSearch.NavItem
              key={item.id}
              icon={item.icon}
              selected={selectedId === item.id}
              onClick={() => handleItemClick(item)}
            >
              {item.label}
            </SidebarWithNestedSectionsAndSearch.NavItem>
          ))}
        </div>
      )}

      {/* Event-Scoped Sections */}
      {hasActiveEvent && (
        <div className="flex w-full flex-col items-start">
          {/* Event Settings */}
          {isAdmin && (
            <SidebarWithNestedSectionsAndSearch.NavSection
              label={EVENT_SETTINGS_SECTION.label}
              icon={EVENT_SETTINGS_SECTION.icon}
              open={openSections.has(EVENT_SETTINGS_SECTION.id)}
              onOpenChange={(open: boolean) => toggleSection(EVENT_SETTINGS_SECTION.id, open)}
            >
              {EVENT_SETTINGS_SECTION.items.map((item) =>
                item.disabled ? (
                  <DisabledNavItem key={item.id} icon={item.icon} label={item.label} />
                ) : (
                  <SidebarWithNestedSectionsAndSearch.NavItem
                    key={item.id}
                    icon={item.icon}
                    selected={selectedId === item.id}
                    onClick={() => handleItemClick(item)}
                  >
                    {item.label}
                  </SidebarWithNestedSectionsAndSearch.NavItem>
                )
              )}
            </SidebarWithNestedSectionsAndSearch.NavSection>
          )}

          {/* Product Sections */}
          {PRODUCT_SECTIONS.filter((s) => ENABLED_PRODUCTS.has(s.id)).map((section) => (
            <SidebarWithNestedSectionsAndSearch.NavSection
              key={section.id}
              label={section.label}
              icon={section.icon}
              open={openSections.has(section.id)}
              onOpenChange={(open: boolean) => toggleSection(section.id, open)}
            >
              {section.items.map((item) =>
                item.disabled ? (
                  <DisabledNavItem key={item.id} icon={item.icon} label={item.label} />
                ) : (
                  <SidebarWithNestedSectionsAndSearch.NavItem
                    key={item.id}
                    icon={item.icon}
                    selected={selectedId === item.id}
                    onClick={() => handleItemClick(item)}
                  >
                    {item.label}
                  </SidebarWithNestedSectionsAndSearch.NavItem>
                )
              )}
            </SidebarWithNestedSectionsAndSearch.NavSection>
          ))}
        </div>
      )}

      {/* No event selected hint */}
      {!hasActiveEvent && (
        <div className="flex w-full flex-col items-center px-4 py-8">
          <span className="text-caption font-caption text-subtext-color text-center">
            Select an event above to access Crucible, Atlas, Anvil, and Tome
          </span>
        </div>
      )}

      {/* Feedback - pushed to bottom */}
      <div className="mt-auto pt-4 w-full">
        <SidebarWithNestedSectionsAndSearch.NavItem
          icon="FeatherMessageCircle"
          onClick={handleFeedback}
        >
          Submit Feedback
        </SidebarWithNestedSectionsAndSearch.NavItem>
      </div>
    </>
  );
}

// ── Collapsed Mode ──

interface CollapsedNavProps {
  selectedId: string;
  hasActiveEvent: boolean;
  isAdmin: boolean;
  projects: any[];
  projectId: string;
  loadingProjects: boolean;
  unreadNotificationCount: number;
  onGlobalItemClick: (item: SidebarNavItem) => void;
  onItemClick: (item: SidebarNavItem) => void;
  onEventSelect: (eventId: string) => void;
  onFeedbackClick: () => void;
}

function CollapsedNav({
  selectedId,
  hasActiveEvent,
  isAdmin,
  projects,
  projectId,
  loadingProjects,
  unreadNotificationCount,
  onGlobalItemClick,
  onItemClick,
  onEventSelect,
  onFeedbackClick,
}: CollapsedNavProps) {
  return (
    <div className="flex w-full grow shrink-0 basis-0 flex-col items-center gap-1 overflow-auto">
      {/* Global items */}
      {GLOBAL_NAV_ITEMS.map((item) => (
        <CollapsedIconButton
          key={item.id}
          icon={item.icon}
          label={item.id === "notifications" && unreadNotificationCount > 0 ? `${item.label} (${unreadNotificationCount})` : item.label}
          badge={item.id === "notifications" ? unreadNotificationCount : undefined}
          disabled={item.disabled}
          selected={selectedId === item.id && !hasActiveEvent}
          onClick={() => onGlobalItemClick(item)}
        />
      ))}

      {/* Divider */}
      <div className="flex w-full px-2 py-1">
        <div className="h-px w-full bg-neutral-border" />
      </div>

      {/* Event selector flyout */}
      <SubframeCore.DropdownMenu.Root>
        <SubframeCore.DropdownMenu.Trigger asChild>
          <div
            className={SubframeCore.twClassNames(
              "flex w-full items-center justify-center px-2 py-2 rounded cursor-pointer",
              hasActiveEvent
                ? "bg-brand-50 dark:bg-brand-950 text-brand-600"
                : "hover:bg-neutral-50 dark:hover:bg-neutral-200 text-default-font"
            )}
            title={hasActiveEvent ? "Change event" : "Select event"}
          >
            <SubframeCore.Icon
              className="text-body font-body"
              name="FeatherFolder"
            />
          </div>
        </SubframeCore.DropdownMenu.Trigger>
        <SubframeCore.DropdownMenu.Portal>
          <SubframeCore.DropdownMenu.Content side="right" align="start" sideOffset={4} asChild>
            <DropdownMenu>
              <div className="px-3 py-1.5">
                <span className="text-caption-bold font-caption-bold text-subtext-color">
                  {loadingProjects ? "Loading..." : "Select Event"}
                </span>
              </div>
              {projects.map((project: any) => (
                <DropdownMenu.DropdownItem
                  key={project.projectId}
                  icon={project.projectId === projectId ? "FeatherCheck" : "FeatherCircle"}
                  onClick={() => onEventSelect(project.projectId)}
                >
                  {project.name}
                </DropdownMenu.DropdownItem>
              ))}
            </DropdownMenu>
          </SubframeCore.DropdownMenu.Content>
        </SubframeCore.DropdownMenu.Portal>
      </SubframeCore.DropdownMenu.Root>

      {/* Event-scoped items */}
      {hasActiveEvent && (
        <>
          <div className="flex w-full px-2 py-1">
            <div className="h-px w-full bg-neutral-border" />
          </div>

          {/* Event top-level items */}
          {EVENT_NAV_ITEMS.map((item) => (
            <CollapsedIconButton
              key={item.id}
              icon={item.icon}
              label={item.label}
              selected={selectedId === item.id}
              onClick={() => onItemClick(item)}
            />
          ))}

          {/* Event Settings flyout (admin only) */}
          {isAdmin && (
            <CollapsedFlyoutSection
              section={EVENT_SETTINGS_SECTION}
              selectedId={selectedId}
              onItemClick={onItemClick}
            />
          )}

          {/* Product sections */}
          {PRODUCT_SECTIONS.filter((s) => ENABLED_PRODUCTS.has(s.id)).map((section) => {
            const enabledItems = section.items.filter((i) => !i.disabled);
            if (enabledItems.length === 1) {
              return (
                <CollapsedIconButton
                  key={section.id}
                  icon={section.icon}
                  label={section.label}
                  selected={sectionContainsSelected(section, selectedId)}
                  onClick={() => onItemClick(enabledItems[0])}
                />
              );
            }
            return (
              <CollapsedFlyoutSection
                key={section.id}
                section={section}
                selectedId={selectedId}
                onItemClick={onItemClick}
              />
            );
          })}
        </>
      )}

      {/* Feedback at bottom */}
      <div className="mt-auto pt-2">
        <CollapsedIconButton
          icon="FeatherMessageCircle"
          label="Submit Feedback"
          onClick={onFeedbackClick}
        />
      </div>
    </div>
  );
}

function CollapsedIconButton({
  icon,
  label,
  disabled,
  selected,
  badge,
  onClick,
}: {
  icon: SubframeCore.IconName;
  label: string;
  disabled?: boolean;
  selected?: boolean;
  badge?: number;
  onClick?: () => void;
}) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "relative flex w-full items-center justify-center px-2 py-2 rounded cursor-pointer",
        selected
          ? "bg-brand-50 dark:bg-brand-950"
          : "hover:bg-neutral-50 dark:hover:bg-neutral-200",
        disabled && "opacity-40 cursor-not-allowed"
      )}
      onClick={disabled ? undefined : onClick}
      title={disabled ? `${label} (coming soon)` : label}
    >
      <SubframeCore.Icon
        className={SubframeCore.twClassNames(
          "text-body font-body",
          selected ? "text-brand-700" : "text-default-font"
        )}
        name={icon}
      />
      {badge != null && badge > 0 && (
        <span className="absolute -top-0.5 -right-0.5 flex items-center justify-center min-w-[16px] h-[16px] px-1 rounded-full bg-error-600 text-white text-[10px] font-bold">
          {badge > 99 ? "99+" : badge}
        </span>
      )}
    </div>
  );
}

function CollapsedFlyoutSection({
  section,
  selectedId,
  onItemClick,
}: {
  section: SidebarProductSection;
  selectedId: string;
  onItemClick: (item: SidebarNavItem) => void;
}) {
  return (
    <SubframeCore.DropdownMenu.Root>
      <SubframeCore.DropdownMenu.Trigger asChild>
        <div
          className={SubframeCore.twClassNames(
            "flex w-full items-center justify-center px-2 py-2 rounded cursor-pointer",
            sectionContainsSelected(section, selectedId)
              ? "bg-brand-50 dark:bg-brand-950"
              : "hover:bg-neutral-50 dark:hover:bg-neutral-200"
          )}
          title={section.label}
        >
          <SubframeCore.Icon
            className="text-body font-body text-default-font"
            name={section.icon}
          />
        </div>
      </SubframeCore.DropdownMenu.Trigger>
      <SubframeCore.DropdownMenu.Portal>
        <SubframeCore.DropdownMenu.Content side="right" align="start" sideOffset={4} asChild>
          <DropdownMenu>
            <div className="px-3 py-1.5">
              <span className="text-caption-bold font-caption-bold text-subtext-color">
                {section.label}
              </span>
            </div>
            {section.items.map((item) => (
              <DropdownMenu.DropdownItem
                key={item.id}
                icon={item.icon}
                onClick={item.disabled ? undefined : () => onItemClick(item)}
                className={item.disabled ? "opacity-40 cursor-not-allowed" : ""}
              >
                {item.label}
              </DropdownMenu.DropdownItem>
            ))}
          </DropdownMenu>
        </SubframeCore.DropdownMenu.Content>
      </SubframeCore.DropdownMenu.Portal>
    </SubframeCore.DropdownMenu.Root>
  );
}
