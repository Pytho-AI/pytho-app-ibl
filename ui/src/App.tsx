import { useState, useEffect } from 'react'
import { useLocation } from 'react-router-dom'
import './App.css'
import { AppContext, useAppContext } from './lib/contextLib';
import { ThemeProvider } from './lib/themeLib';
import { ChatProvider } from './lib/ChatContext';
import { IntelAgentWsProvider, useIntelAgentWs } from './lib/IntelAgentWs';
// ChatAgent is now mounted inside DefaultPageLayout (subframe/layouts) — not at App root.
import Routes from "./Routes.tsx";


/**
 * NotificationsBridge — pumps backend notifications into AppContext.
 *
 * Two sources feed the in-memory notifications array exposed by
 * useAppContext():
 *
 *   1. REST bootstrap on auth — GET /api/notifications/{user_id} returns
 *      every notification ever delivered to this analyst, broadcast OR
 *      direct. We slot the result into setNotifications on user login.
 *
 *   2. Live WS push — the backend's KLIEGLIGHT auto-fire writes a row
 *      and broadcasts {type:"notification", id, ...} over /ws/intel_agent
 *      to every connected socket. IntelAgentWs treats sessionless frames
 *      as broadcast and fans them out to every subscriber; the bridge
 *      subscribes with a sessionId of "" so it catches that broadcast
 *      and prepends the row to setNotifications.
 *
 * Mounted inside IntelAgentWsProvider so useIntelAgentWs() resolves,
 * and inside AppContext.Provider so setNotifications resolves.
 */
function NotificationsBridge() {
  const { user, notifications, setNotifications } = useAppContext();
  const ws = useIntelAgentWs();

  // REST bootstrap on user login.
  useEffect(() => {
    if (!user?.userId) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`/api/notifications/${encodeURIComponent(user.userId)}`);
        if (!resp.ok) return;
        const body = await resp.json();
        if (cancelled) return;
        setNotifications(body.notifications || []);
      } catch {
        /* network failure — keep previous notifications array */
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.userId]);

  // Live broadcast subscription. We use sessionId="" because the backend
  // emits notification frames without a sessionId (they're cross-session).
  // IntelAgentWs treats sessionless frames as a broadcast — every
  // subscriber gets them.
  useEffect(() => {
    const handler = (payload: any) => {
      if (
        !payload ||
        (payload.type !== "klieglight_autofire" &&
          payload.type !== "product_generated")
      )
        return;
      // Prepend so newest appears at top of the Notifications page.
      setNotifications((prev: any[]) => {
        // Dedup on id in case the WS pushes the same row twice (e.g.
        // reconnect replays).
        if (payload.id && prev.some(n => n.id === payload.id)) return prev;
        return [{
          id: payload.id,
          kind: payload.type,
          artifact_id: payload.artifact_id,
          autodraft_session_id: payload.autodraft_session_id,
          payload,
          status: "unread",
          created_at: payload.created_at || new Date().toISOString(),
        }, ...prev];
      });
    };
    const unsub = ws.subscribe("", { onMessage: handler });
    return unsub;
  }, [ws, setNotifications]);

  return null;
}


function App() {
  const location = useLocation();
  const [isAuthenticating, setIsAuthenticating] = useState(true);
  const [user, setUser] = useState(null);
  const [projectId, setProjectId] = useState(() => {
    const saved = sessionStorage.getItem('projectId');
    return saved || 'default';
  });

  // The active event is IN THE URL on every event-scoped route
  // (/missioncommand/events/:projectId/...). sessionStorage alone
  // survives same-tab refreshes but disagrees with the URL on deep
  // links and fresh tabs — the sidebar then shows a different event
  // than the page. The URL is the source of truth: adopt its project
  // id whenever they disagree. Routes without one keep the last
  // selection.
  useEffect(() => {
    const m = location.pathname.match(/\/(?:projects|events)\/([0-9a-f][0-9a-f-]{7,})(?:\/|$)/i);
    if (m && m[1] !== projectId) setProjectId(m[1]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname]);
  const [projectMap, setProjectMap] = useState(() => {
    try {
      const saved = sessionStorage.getItem('projectMap');
      if (saved) return JSON.parse(saved);
    } catch (e) { /* ignore parse errors */ }
    return { 'default': {} };
  });
  const [notifications, setNotifications] = useState([])
  
  // Persist projectId to sessionStorage
  useEffect(() => {
    sessionStorage.setItem('projectId', projectId);
  }, [projectId]);

  // Persist projectMap to sessionStorage (only projectDetails, not sessions)
  useEffect(() => {
    try {
      const mapToSave: any = {};
      for (const [key, value] of Object.entries(projectMap)) {
        if (key === 'default') {
          mapToSave[key] = value;
        } else {
          mapToSave[key] = { projectDetails: (value as any)?.projectDetails };
        }
      }
      sessionStorage.setItem('projectMap', JSON.stringify(mapToSave));
    } catch (e) { /* ignore serialization errors */ }
  }, [projectMap]);

  useEffect(() => {
    async function onLoad() {
      
      try {
        const storedUser = localStorage.getItem("user");
        if (storedUser) {
          setUser(JSON.parse(storedUser));
        }
    
      }
      catch(e) {
        if (e !== 'No current user') {
          //onError(e);
        }
      }
      
      setIsAuthenticating(false);
      
    }

    onLoad();
    // Tag for ReAmaze
    //TagManager.initialize({ gtmId: 'GTM-K9KD4RT' });
  }, []);
  
  return (
    <ThemeProvider>
      <div className="bg-default-background h-screen w-screen fixed top-0 left-0 text-left text-default-font flex flex-row">
        {!isAuthenticating &&
        <AppContext.Provider value={{ user, setUser, projectId, setProjectId, projectMap, setProjectMap, notifications, setNotifications }}>
          {/* ChatProvider + WS singleton live at app root so the chat state
              and /ws/intel_agent socket survive route changes. ChatAgent
              itself is mounted INSIDE DefaultPageLayout (sibling of page
              content, beneath the top bar + breadcrumb) so its top edge
              aligns with page content — not at viewport top, which would
              make the page header's horizontal divider lines visually
              bleed into the chat's empty top space. */}
          <ChatProvider>
            <IntelAgentWsProvider>
              <NotificationsBridge />
              <Routes />
            </IntelAgentWsProvider>
          </ChatProvider>
        </AppContext.Provider>}
      </div>
    </ThemeProvider>
  )
}

export default App
