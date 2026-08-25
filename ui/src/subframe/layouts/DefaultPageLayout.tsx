// @subframe/sync-disable
"use client";
/*
 * Documentation:
 * Default Page Layout — https://app.subframe.com/0ae0a78e3a6e/library?component=Default+Page+Layout_a57b1c43-310a-493f-b807-8cc88e2452cf
 * Tree View — https://app.subframe.com/0ae0a78e3a6e/library?component=Tree+View_4ed46422-ecc3-41e8-8787-e55ee10cdc75
 * Sidebar with large items — https://app.subframe.com/0ae0a78e3a6e/library?component=Sidebar+with+large+items_70c3656e-47c2-460e-8007-e198804e8862
 */

import React, { useState, useEffect } from "react";
import { useTheme } from "../../lib/themeLib";
import * as SubframeCore from "@subframe/core";
import { SidebarWithMinimalTextSections } from "../components/SidebarWithMinimalTextSections";
import { SidebarNav } from "../../components/sidebar/SidebarNav";
import { DropdownMenu } from "../components/DropdownMenu";
import { IconButton } from "../components/IconButton";
import { Drawer } from "../components/Drawer";
import SideBarFeedback from "../../components/SideBarFeedback";
import BreadcrumbHeader from "../../components/BreadcrumbHeader";
import { ThemeToggle } from "../../components/ThemeToggle";
import { useNavigate, useLocation } from "react-router-dom";
import { useAppContext } from "../../lib/contextLib";
import { useBreadcrumbs } from "../../lib/useBreadcrumbs";
import * as SubframeUtils from "../utils";
import { AppRail } from "../../components/AppRail";
import ChatAgent from "../../components/ChatAgent";

interface DefaultPageLayoutRootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  className?: string;
}

const DefaultPageLayoutRoot = React.forwardRef<
  HTMLElement,
  DefaultPageLayoutRootProps
>(function DefaultPageLayoutRoot(
  { children, className, ...otherProps }: DefaultPageLayoutRootProps,
  ref
) {

  const { user, projectId, setProjectId, notifications } = useAppContext();
  const { theme } = useTheme();
  const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';
  const [openFeedback, setOpenFeedback] = useState(false);
  const [isMobileDrawerOpen, setIsMobileDrawerOpen] = useState(false);
  const location = useLocation();

  const [isCollapsed, setIsCollapsed] = useState(() => {
    const saved = sessionStorage.getItem('sidebarCollapsed');
    return saved === 'true';
  });

  const nav = useNavigate();

  useEffect(() => {
    sessionStorage.setItem('sidebarCollapsed', String(isCollapsed));
  }, [isCollapsed]);


  // Determine selected item based on current route
  const getSelectedId = () => {
    const path = location.pathname;
    if (path === '/missioncommand' || path === '/' || path.includes('/missioncommand/whitecell') || path.includes('/missioncommand/events')) {
      return 'home';
    }
    if (path.includes('/operations-map')) {
      return 'operationsMap';
    }
    if (path.includes('/graph-viewer')) {
      return 'graphViewer';
    }
    if (path.includes('/coa-pipeline')) {
      return 'coaPipeline';
    }
    if (path.includes('/data/static')) {
      return 'staticData';
    }
    if (path.includes('/doctrines')) {
      return 'doctrines';
    }
    return '';
  };

  const handleHomeClick = () => {
    setProjectId('default');
    setIsMobileDrawerOpen(false);
    nav("/missioncommand");
  };

  const handleFeedbackClick = () => {
    setIsMobileDrawerOpen(false);
    setOpenFeedback(true);
  };

  const handleSignOut = () => {
    sessionStorage.removeItem("user");
    sessionStorage.removeItem('sidebarCollapsed');
    sessionStorage.removeItem('projectId');
    sessionStorage.removeItem('projectMap');
    localStorage.clear();
    window.location.reload();
  };

  const getInitials = (u: any): string => {
    if (u?.firstName && u?.lastName) {
      return `${u.firstName[0]}${u.lastName[0]}`.toUpperCase();
    }
    if (u?.email) {
      return u.email[0].toUpperCase();
    }
    return "??";
  };

  const getUserName = (u: any): string => {
    if (u?.firstName && u?.lastName) {
      return `${u.firstName} ${u.lastName}`;
    }
    return u?.email || "User";
  };

  const unreadCount = Array.isArray(notifications) ? notifications.filter((n: any) => !n.read).length : 0;
  const breadcrumbs = useBreadcrumbs();

  return (
    <div
      className={SubframeCore.twClassNames(
        "flex h-screen w-full relative",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      {/* App Rail - leftmost icon strip (toggle via VITE_SHOW_APP_RAIL build arg) */}
      {import.meta.env.VITE_SHOW_APP_RAIL === 'true' && <AppRail />}

      {/* Sidebar - desktop */}
      <SidebarWithMinimalTextSections
        className={SubframeUtils.twClassNames(
          "mobile:hidden",
          isCollapsed ? "w-16" : ""
        )}
        header={
          <div className="flex w-full items-center gap-4">
            {!isCollapsed && (
              <>
                <img
                  className="w-28 flex-none object-cover cursor-pointer"
                  src={logoSrc}
                  alt="Pytho Logo"
                  onClick={handleHomeClick}
                />
              </>
            )}
            <div className={SubframeCore.twClassNames(
              "flex items-center gap-1",
              isCollapsed ? "mx-auto" : "ml-auto"
            )}>
              <button
                onClick={() => setIsCollapsed(!isCollapsed)}
                className="p-2 hover:bg-neutral-100 rounded"
                aria-label={isCollapsed ? "Expand sidebar" : "Collapse sidebar"}
              >
                <SubframeCore.Icon
                  className="text-body font-body text-default-font"
                  name={isCollapsed ? "FeatherChevronRight" : "FeatherChevronLeft"}
                />
              </button>
            </div>
          </div>
        }
      >
        {/* Ported pytho-vite SidebarNav (UI unification): global items,
            Active Event selector, event-scoped Crucible/Atlas/Anvil/Tome
            sections. All nav + event-switch logic lives in the shared
            component; this layout only supplies the chrome. */}
        <SidebarNav
          mode={isCollapsed ? "collapsed" : "expanded"}
          onFeedbackClick={handleFeedbackClick}
        />
      </SidebarWithMinimalTextSections>
      <SideBarFeedback open={openFeedback} setOpen={setOpenFeedback} message={"Help us improve with feedback or feature requests. Please send your email to team@pytho.ai"} />

      {/* Mobile Drawer */}
      <Drawer
        open={isMobileDrawerOpen}
        onOpenChange={setIsMobileDrawerOpen}
        side="left"
      >
        <Drawer.Content side="left">
          <div className="flex w-full flex-col items-start bg-default-background h-full">
            {/* Header */}
            <div className="flex w-full flex-col items-start gap-2 px-6 py-6 border-b border-solid border-neutral-border">
              <div className="flex w-full items-center gap-4">
                <img
                  className="w-28 flex-none object-cover cursor-pointer"
                  src={logoSrc}
                  alt="Pytho Logo"
                  onClick={handleHomeClick}
                />
                <button
                  onClick={() => setIsMobileDrawerOpen(false)}
                  className="p-2 hover:bg-neutral-100 rounded"
                  aria-label="Close menu"
                  title="Close menu"
                >
                  <SubframeCore.Icon
                    className="text-body font-body text-default-font"
                    name="FeatherX"
                  />
                </button>
              </div>
            </div>

            {/* Navigation — same ported SidebarNav, drawer-hosted */}
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start px-4 py-4 overflow-auto">
              <SidebarNav
                mode="mobile"
                onFeedbackClick={handleFeedbackClick}
                onNavigate={() => setIsMobileDrawerOpen(false)}
              />
            </div>
          </div>
          <SideBarFeedback open={openFeedback} setOpen={setOpenFeedback} message={"Help us improve with feedback or feature requests. Please send your email to team@pytho.ai"} />
        </Drawer.Content>
      </Drawer>

      {/* Main content area */}
      <div className="flex flex-1 flex-col min-h-0">
        {/* Top Bar - only over main content area */}
        <div className="flex w-full items-center justify-between border-b border-solid border-neutral-border bg-default-background px-6 py-3 z-10">
          <div className="flex items-center gap-2">
            {/* Back button — hidden on the home/dashboard routes since
                there's nowhere meaningful to go back to from there
                (root is the dashboard itself). Anywhere else, it
                pops the browser history. */}
            {!(
              location.pathname === "/" ||
              location.pathname === "/missioncommand" ||
              location.pathname === "/missioncommand/whitecell"
            ) && (
              <IconButton
                icon="FeatherArrowLeft"
                size="small"
                onClick={() => nav(-1)}
                aria-label="Go back"
              />
            )}
            {/* Hamburger menu button - mobile only */}
            <div className="hidden mobile:block">
              <IconButton
                icon="FeatherMenu"
                size="small"
                onClick={() => setIsMobileDrawerOpen(true)}
                aria-label="Open menu"
              />
            </div>
          </div>
          <div className="flex items-center gap-4 ml-auto mobile:ml-0">
            <ThemeToggle />
            {user?.email && (
              <SubframeCore.DropdownMenu.Root>
                <SubframeCore.DropdownMenu.Trigger asChild={true}>
                  <button className="relative flex items-center justify-center gap-2 px-3 py-2 hover:bg-neutral-50 rounded cursor-pointer focus:outline-none focus:ring-2 focus:ring-neutral-200">
                    <div className="flex items-center justify-center w-8 h-8 rounded-full bg-neutral-200 text-neutral-600 pointer-events-none text-sm font-semibold">
                      {getInitials(user)}
                    </div>
                    {unreadCount > 0 && (
                      <div className="absolute -top-0.5 -right-0.5 flex items-center justify-center min-w-[18px] h-[18px] px-1 rounded-full bg-error-600 text-white text-xs font-bold">
                        {unreadCount > 99 ? '99+' : unreadCount}
                      </div>
                    )}
                  </button>
                </SubframeCore.DropdownMenu.Trigger>
                <SubframeCore.DropdownMenu.Portal>
                  <SubframeCore.DropdownMenu.Content
                    side="bottom"
                    align="end"
                    sideOffset={4}
                    asChild={true}
                  >
                    <DropdownMenu>
                      <div className="px-3 py-2 border-b border-neutral-200 w-full">
                        <span className="text-body-bold font-body-bold text-default-font block">
                          {getUserName(user)}
                        </span>
                        <span className="text-body font-body text-subtext-color block mt-1">
                          {user.email}
                        </span>
                      </div>
                      <DropdownMenu.DropdownItem
                        icon="FeatherBell"
                        onClick={() => {
                          nav("/missioncommand/notifications");
                        }}
                      >
                        <div className="flex items-center gap-2">
                          Notifications
                          {unreadCount > 0 && (
                            <span className="flex items-center justify-center min-w-[20px] h-[20px] px-1.5 rounded-full bg-error-600 text-white text-xs font-bold">
                              {unreadCount > 99 ? '99+' : unreadCount}
                            </span>
                          )}
                        </div>
                      </DropdownMenu.DropdownItem>
                      <DropdownMenu.DropdownItem
                        icon="FeatherLogOut"
                        onClick={handleSignOut}
                      >
                        Sign Out
                      </DropdownMenu.DropdownItem>
                    </DropdownMenu>
                  </SubframeCore.DropdownMenu.Content>
                </SubframeCore.DropdownMenu.Portal>
              </SubframeCore.DropdownMenu.Root>
            )}
          </div>
        </div>

        {/* Breadcrumbs */}
        <BreadcrumbHeader items={breadcrumbs} />

        {/* Page content + Intel Agent chat sidebar as siblings, both sitting
            BELOW the page top bar and breadcrumb. Mounting ChatAgent here
            (rather than at App-root level) means the chat panel's top edge
            aligns with the page-content top edge — the page header's
            horizontal divider lines don't bleed into the chat area because
            the chat now lives below those lines, not parallel to them. */}
        <div className="flex grow shrink-0 basis-0 flex-row min-h-0 self-stretch">
          {children ? (
            <div className="flex flex-1 min-w-0 flex-col items-start gap-4 overflow-y-auto bg-default-background">
              {children}
            </div>
          ) : (
            <div className="flex-1" />
          )}
          <ChatAgent />
        </div>
      </div>
    </div>
  );
});

export const DefaultPageLayout = DefaultPageLayoutRoot;
