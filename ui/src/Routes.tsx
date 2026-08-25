import { Navigate, Route, Routes, useLocation } from "react-router-dom";
import Chat from "./pages/Chat.tsx";
import Root from "./pages/Root.tsx";
import SignIn from "./pages/SignIn.tsx";
import SignUp from "./pages/SignUp.tsx";
// import Projects from "./pages/Projects.tsx";
import CreateProject from "./pages/CreateProject.tsx";
import DataView from "./pages/DataView.tsx";
import Doctrines from "./pages/Doctrines.tsx";
import Notifications from "./pages/Notifications.tsx";
import EarlyBirdDigest from "./pages/EarlyBirdDigest.tsx";
import ResetPasswordRequest from "./pages/ResetPasswordRequest.jsx";
import AuthenticatedRoute from "./components/AuthenticatedRoute.js";
import AarModule from "./pages/AarModule.tsx";
import EventDashboard from "./pages/EventDashboard.tsx";
import CreateAar from "./pages/CreateAar.tsx";
import LessonLearnedDashboard from "./pages/LessonLearnedDashboard.tsx";
import WhiteCellHome from "./pages/WhiteCellHome.tsx";
import Agents from "./pages/Agents.tsx";
import OperationsMap from "./pages/OperationsMap.tsx";
import { useAppContext } from "./lib/contextLib";

/** Legacy /missioncommand/operations-map — the map is EVENT-scoped now
 * (the event map's id IS the projectId). Send old links into the active
 * event's map, preserving the query string (deep links like
 * ?from_artifact= keep working); no active event -> events home. */
function LegacyOperationsMapRedirect() {
  const { projectId } = (useAppContext() || {}) as any;
  const location = useLocation();
  if (projectId && projectId !== "default") {
    return <Navigate to={`/missioncommand/events/${projectId}/operations-map${location.search}`} replace />;
  }
  return <Navigate to="/missioncommand" replace />;
}
// OperationsMapHeadless moved to geo-agent (Task 6). Playwright now
// hits `${geo-agent}/geo-app/headless` served by geo-agent's own bundle.
import CoaPipeline from "./pages/coa-pipeline/CoaPipeline.tsx";
import DocEditorPage from "./pages/DocEditorPage.tsx";
import DocEditorHeadless from "./pages/DocEditorHeadless.tsx";
import GraphViewer from "./pages/GraphViewer.tsx";

export default function Links() {
  return (
    <Routes>
      <Route path="/" element={<AuthenticatedRoute> <WhiteCellHome /> </AuthenticatedRoute>} />
      <Route path="/missioncommand" element={<AuthenticatedRoute> <WhiteCellHome /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/whitecell" element={<AuthenticatedRoute> <WhiteCellHome /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/agents" element={<AuthenticatedRoute> <Agents /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/session" element={<AuthenticatedRoute> <Chat /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/notifications" element={<AuthenticatedRoute> <Notifications /> </AuthenticatedRoute>} />
      {/* <Route path="/missioncommand/events" element={<AuthenticatedRoute> <Projects /> </AuthenticatedRoute>} /> */}
      <Route path="/missioncommand/events/create" element={<AuthenticatedRoute> <CreateProject /> </AuthenticatedRoute>} /> 
      <Route path="/missioncommand/events/update" element={<AuthenticatedRoute> <CreateProject /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/events/dashboard" element={<AuthenticatedRoute> <EventDashboard /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/events/:projectId/dashboard" element={<AuthenticatedRoute> <EventDashboard /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/events/:projectId/aar" element={<AuthenticatedRoute> <LessonLearnedDashboard /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/events/aar/create" element={<AuthenticatedRoute> <CreateAar /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/events/aar/:aarId" element={<AarModule />} />
      <Route path="/missioncommand/data/static" element={<AuthenticatedRoute> <DataView /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/data/stream" element={<AuthenticatedRoute> <DataView /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/doctrines" element={<AuthenticatedRoute> <Doctrines /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/early-bird-summary" element={<AuthenticatedRoute> <EarlyBirdDigest /> </AuthenticatedRoute>} />
      {/* Operations Map — project/event-scoped: the event map's id IS the projectId (pytho-vite 2a606a2 parity) */}
      <Route path="/missioncommand/events/:projectId/operations-map" element={<AuthenticatedRoute> <OperationsMap /> </AuthenticatedRoute>} />
      {/* Legacy unscoped map URL — bookmarks and muscle memory land here.
          Redirects into the ACTIVE event's map (query string preserved),
          or the events home when no event is selected. */}
      <Route path="/missioncommand/operations-map" element={<AuthenticatedRoute> <LegacyOperationsMapRedirect /> </AuthenticatedRoute>} />
      <Route path="/missioncommand/graph-viewer" element={<AuthenticatedRoute> <GraphViewer /> </AuthenticatedRoute>} />
      {/* Headless render route retired — geo-agent's Playwright renderer
          now targets its own frontend at /geo-app/headless. */}
      <Route path="/missioncommand/coa-pipeline" element={<AuthenticatedRoute> <CoaPipeline /> </AuthenticatedRoute>} />
      {/* Standalone DocEditor surface for the Generate-button flow. The page
          opens directly with an empty document + the ChatAgent WS active,
          and swaps in the rendered artifact when OE emits one. See
          pages/DocEditorPage.tsx. */}
      <Route path="/missioncommand/docs/:sessionId" element={<AuthenticatedRoute> <DocEditorPage /> </AuthenticatedRoute>} />
      {/* Headless render route — UNAUTHENTICATED on purpose. Loaded by
          docker-api's Playwright renderer over the internal cluster
          network. Mounts the same <DocEditor> as the analyst surface
          (same TinyMCE config, same content_style, same page-break
          chrome) and signals window.__previewReady once layout is
          stable, then docker-api screenshots the iframe body as one
          long preview.png. See utils/render/preview.py. */}
      <Route path="/missioncommand/docs/render" element={<DocEditorHeadless />} />
      <Route path="/signin" element={<SignIn />} />
      <Route path="/signup" element={<SignUp /> } />
      <Route path="/reset" element={<ResetPasswordRequest />} />
    </Routes>
  );
}