import { useMemo } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { useAppContext } from "./contextLib";
import { useProjectData } from "./useProjectData";

interface BreadcrumbItem {
  label: string;
  onClick?: () => void;
}

export function useBreadcrumbs(
  additionalData?: {
    aarName?: string;
    customLabel?: string;
    projectId?: string;
    fromLessonLearnedDashboard?: boolean;
  }
): BreadcrumbItem[] {
  const location = useLocation();
  const navigate = useNavigate();
  const { projectMap, projectId: contextProjectId } = useAppContext();

  // Read from location.state as fallback when additionalData is not provided
  const locationState = location.state as {
    aarName?: string;
    projectId?: string;
    fromLessonLearnedDashboard?: boolean;
  } | null;

  // Merge: additionalData takes priority, then location.state, then context
  const mergedData = {
    aarName: additionalData?.aarName || locationState?.aarName,
    customLabel: additionalData?.customLabel,
    projectId: additionalData?.projectId || locationState?.projectId,
    fromLessonLearnedDashboard:
      additionalData?.fromLessonLearnedDashboard ??
      locationState?.fromLessonLearnedDashboard,
  };

  // Use projectId from additionalData/locationState if provided, otherwise from context
  const projectId = mergedData.projectId || contextProjectId;

  // Auto-load project data if projectId is present but not in projectMap
  useProjectData(projectId);

  return useMemo(() => {
    const items: BreadcrumbItem[] = [];
    const path = location.pathname;

    // Helper to get project name from projectMap
    const getProjectName = (): string => {
      if (!projectId || projectId === "default") return "...";
      const entry = projectMap[projectId];
      const project = entry?.projectDetails || entry;
      return project?.name || "...";
    };

    // Helper to navigate to event dashboard
    const navigateToProject = () => {
      if (projectId && projectId !== "default") {
        navigate(`/missioncommand/events/${projectId}/dashboard`);
      } else {
        navigate("/missioncommand/events/dashboard");
      }
    };

    // Helper to navigate to Lesson Learned Dashboard
    const navigateToLessonLearnedDashboard = () => {
      if (projectId && projectId !== "default") {
        navigate(`/missioncommand/events/${projectId}/aar`);
      }
    };

    // Home page — no breadcrumbs at all
    if (path === "/" || path === "/missioncommand") {
      return items;
    }

    // Always start with Home
    items.push({
      label: "Home",
      onClick: () => navigate("/missioncommand"),
    });

    // Parse route and build breadcrumbs
    if (path.includes("/missioncommand/whitecell")) {
      items.push({ label: "Intelligence Operations" });
    } else if (path.includes("/missioncommand/agents")) {
      // Agents: Home > Agents
      items.push({ label: "Agents" });
    } else if (path.includes("/missioncommand/session")) {
      // Chat session: Home > [EventName] > Chat
      if (projectId && projectId !== "default") {
        items.push({
          label: getProjectName(),
          onClick: navigateToProject,
        });
      }
      items.push({ label: "Chat" });
    } else if (path.includes("/missioncommand/notifications")) {
      // Notifications: Home > Notifications
      items.push({ label: "Notifications" });
    } else if (/\/missioncommand\/events\/[^/]+\/aar$/.test(path)) {
      // Lesson Learned Dashboard: Home > EventName > Lesson Learned Dashboard
      if (projectId && projectId !== "default") {
        items.push({
          label: getProjectName(),
          onClick: navigateToProject,
        });
      }
      items.push({ label: "Lesson Learned Dashboard" });
    } else if (path.includes("/missioncommand/events/aar/create")) {
      // Create Lesson Learned: Home > [EventName] > Lesson Learned Dashboard > Create
      if (projectId && projectId !== "default") {
        items.push({
          label: getProjectName(),
          onClick: navigateToProject,
        });
        items.push({
          label: "Lesson Learned Dashboard",
          onClick: navigateToLessonLearnedDashboard,
        });
      }
      items.push({ label: "Create Lesson Learned" });
    } else if (path.includes("/missioncommand/events/aar/")) {
      // Lesson Learned detail: Home > [EventName] > Lesson Learned Dashboard > Name
      if (projectId && projectId !== "default") {
        items.push({
          label: getProjectName(),
          onClick: navigateToProject,
        });
        if (mergedData.fromLessonLearnedDashboard) {
          items.push({
            label: "Lesson Learned Dashboard",
            onClick: navigateToLessonLearnedDashboard,
          });
        }
      }
      const aarName = mergedData.aarName || "...";
      items.push({ label: aarName });
    } else if (path.includes("/missioncommand/events/create")) {
      // Create Event: Home > Create Event
      items.push({ label: "Create Event" });
    } else if (path.includes("/missioncommand/events/update")) {
      // Update Event: Home > [EventName] > Update Event
      if (projectId && projectId !== "default") {
        items.push({
          label: getProjectName(),
          onClick: navigateToProject,
        });
      }
      items.push({ label: "Update Event" });
    } else if (path.includes("/missioncommand/events/dashboard") || /\/missioncommand\/events\/[^/]+\/dashboard/.test(path)) {
      // Event Dashboard: Home > EventName
      if (projectId && projectId !== "default") {
        items.push({ label: getProjectName() });
      } else {
        items.push({ label: "Event Dashboard" });
      }
    } else if (path.includes("/missioncommand/coa-pipeline")) {
      // COA Pipeline: Home > COA Pipeline
      items.push({ label: "COA Pipeline" });
    } else if (path.includes("/missioncommand/data/")) {
      // Data pages: Home > Data
      items.push({ label: "Data" });
    } else if (path.includes("/missioncommand/doctrines")) {
      // Doctrines: Home > Doctrines
      items.push({ label: "Doctrines" });
    } else if (path.includes("/missioncommand/early-bird-summary")) {
      // Early Bird Digest: Home > Early Bird Summary
      items.push({ label: "Early Bird Summary" });
    } else if (mergedData.customLabel) {
      // Custom label for any other page
      items.push({ label: mergedData.customLabel });
    }

    return items;
  }, [location.pathname, location.state, projectMap, projectId, mergedData.aarName, mergedData.fromLessonLearnedDashboard, mergedData.customLabel, navigate]);
}
