"use client";

import React, { useEffect, useState, useRef } from "react";
import * as SubframeCore from "@subframe/core";
import { Badge } from "../subframe/components/Badge";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { Table } from "../subframe/components/Table";
import { Avatar } from "../subframe/components/Avatar";
import { LinkButton } from "../subframe/components/LinkButton";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { useNavigate, useParams } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";
import { useProjectData } from "../lib/useProjectData";
import DeleteAar from "../components/DeleteAar";
import AddObservationsModal from "../components/AddObservationsModal";
import AarAnalysisPanel from "../components/AarAnalysisPanel";
import DocEditor from "../components/DocEditor";
import ProductLibraryModal from "../components/ProductLibraryModal";
import { getEventStatusBadgeVariant } from "../lib/statusUtils";
import mammoth from "mammoth";

interface AAR {
  id: number;
  name: string;
  description: string | null;
  event_id: string | null;
  created_at: string;
  updated_at: string;
  contributor_name?: string | null;
}

interface ProjectDetails {
  projectId: string;
  name: string;
  description?: string;
  status: string;
  eventDate?: string;
}

function LessonLearnedDashboard() {
  const nav = useNavigate();
  const { projectId: projectIdFromParams } = useParams<{ projectId: string }>();
  const { user, projectMap, projectId: projectIdFromContext, setProjectId } = useAppContext();

  // Use projectId from URL params (source of truth)
  const projectId = projectIdFromParams || projectIdFromContext;

  // Automatically load projectMap if missing
  useProjectData(projectId);

  // Update context projectId from URL
  useEffect(() => {
    if (projectIdFromParams && projectIdFromParams !== projectIdFromContext) {
      setProjectId(projectIdFromParams);
    }
  }, [projectIdFromParams, projectIdFromContext, setProjectId]);

  const [project, setProject] = useState<ProjectDetails | null>(null);
  const [aars, setAars] = useState<AAR[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [openConfirmDeleteAar, setOpenConfirmDeleteAar] = useState(false);
  const [deleteAarTarget, setDeleteAarTarget] = useState<AAR | null>(null);
  const [editObservationModalOpen, setEditObservationModalOpen] = useState(false);
  const [editAarTarget, setEditAarTarget] = useState<AAR | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 10;

  // Executive Summary generation state
  const [selectedTemplate, setSelectedTemplate] = useState<any>(null);
  const [generatingExecSummary, setGeneratingExecSummary] = useState(false);
  const [generationProgress, setGenerationProgress] = useState("");
  const [generationError, setGenerationError] = useState<string | null>(null);
  const [latestSummary, setLatestSummary] = useState<{ htmlLink: string; docxLink: string; sessionId: string } | null>(null);

  // DocEditor state
  const [docEditorOpen, setDocEditorOpen] = useState(false);
  const [currentDocContent, setCurrentDocContent] = useState("");
  const [currentHtmlLink, setCurrentHtmlLink] = useState("");
  const [currentDocxLink, setCurrentDocxLink] = useState("");
  const [currentProductName, setCurrentProductName] = useState("");
  const [currentSessionId, setCurrentSessionId] = useState("");
  const [editorIsDirty, setEditorIsDirty] = useState(false);
  const [showDiscardModal, setShowDiscardModal] = useState(false);
  const editorSaveRef = useRef<(() => Promise<void>) | null>(null);

  // ProductLibrary state
  const [openProductLibrary, setOpenProductLibrary] = useState(false);
  const [productTypes, setProductTypes] = useState<any[]>([]);
  const [loadingProducts, setLoadingProducts] = useState(false);

  // WebSocket ref
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (projectId && projectMap[projectId]?.projectDetails) {
      setProject(projectMap[projectId].projectDetails);
    }
  }, [projectId, projectMap]);

  useEffect(() => {
    if (projectId) {
      fetchAARs();
    }
  }, [projectId]);

  // Reset to page 1 when list changes
  useEffect(() => {
    setCurrentPage(1);
  }, [aars.length]);

  const fetchAARs = async () => {
    if (!projectId) return;

    try {
      setLoading(true);
      const response = await fetch(`/api/aar?event_id=${projectId}`);
      if (!response.ok) {
        throw new Error("Failed to fetch lesson learned inputs");
      }
      const data = await response.json();
      setAars(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setLoading(false);
    }
  };

  const handleAARClick = (aarId: number, aarName: string) => {
    nav(`/missioncommand/events/aar/${aarId}`, {
      state: { aarName, projectId, fromLessonLearnedDashboard: true },
    });
  };

  /** Text-based observation has description (Add Observations). Hotwash has transcript, may have empty description. */
  const isTextObservation = (aar: AAR) => !!aar.description?.trim();

  const formatTimestamp = (dateString: string): string => {
    const date = new Date(dateString);
    const month = date.toLocaleDateString("en-US", { month: "short" });
    const day = date.getDate();
    const year = date.getFullYear();
    const time = date.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    return `${month} ${day}, ${year} ${time}`;
  };

  const getInitials = (name: string | null | undefined): string => {
    if (!name || !name.trim()) return "—";
    const parts = name.trim().split(/\s+/);
    if (parts.length >= 2) {
      return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
    }
    return name.substring(0, 2).toUpperCase();
  };

  const downloadTranscript = async (aar: AAR) => {
    try {
      const response = await fetch(`/api/aar/${aar.id}/transcript`);
      if (!response.ok) {
        throw new Error("Failed to fetch transcript");
      }
      const transcriptData = await response.json();

      let content = `Lesson Learned Transcript: ${aar.name}\n`;
      content += `Date: ${formatTimestamp(aar.created_at)}\n`;
      if (aar.contributor_name) {
        content += `Contributor: ${aar.contributor_name}\n`;
      }
      content += `\n${"=".repeat(60)}\n\n`;

      if (transcriptData.length === 0) {
        content += "No transcript available yet.\n";
      } else {
        transcriptData.forEach((entry: any) => {
          content += `[${entry.timestamp}] ${entry.speaker}:\n`;
          content += `${entry.text}\n\n`;
        });
      }

      const blob = new Blob([content], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${aar.name.replace(/[^a-z0-9]/gi, "_")}_transcript.txt`;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    } catch (error) {
      console.error("Error downloading transcript:", error);
    }
  };

  // Pagination
  const totalPages = Math.ceil(aars.length / itemsPerPage);
  const paginatedAars = aars.slice(
    (currentPage - 1) * itemsPerPage,
    currentPage * itemsPerPage
  );
  const startItem =
    aars.length > 0 ? (currentPage - 1) * itemsPerPage + 1 : 0;
  const endItem = Math.min(currentPage * itemsPerPage, aars.length);

  // Cleanup WebSocket on unmount
  useEffect(() => {
    return () => { wsRef.current?.close(); };
  }, []);

  // Load latest executive summary for this project
  useEffect(() => {
    if (!projectId) return;
    (async () => {
      try {
        const res = await fetch(`/api/sessions/${projectId}`);
        if (!res.ok) return;
        const sessions = await res.json();
        // Find the latest exec-summary session
        for (const raw of sessions) {
          const session = typeof raw === "string" ? JSON.parse(raw) : raw;
          if (session.sessionId?.startsWith("exec-summary-")) {
            // Fetch session details to get the latest doc links
            const detailRes = await fetch(`/api/session_details/${session.sessionId}`);
            if (!detailRes.ok) continue;
            let details = await detailRes.json();
            if (typeof details === "string") details = JSON.parse(details);
            // Find the last message with doc links (could be original or a save)
            const msgs = details.messages || [];
            for (let i = msgs.length - 1; i >= 0; i--) {
              if (msgs[i].htmlLink) {
                setLatestSummary({
                  htmlLink: msgs[i].htmlLink,
                  docxLink: msgs[i].docxLink || "",
                  sessionId: session.sessionId,
                });
                return;
              }
            }
          }
        }
      } catch (e) {
        console.error("Error loading latest exec summary:", e);
      }
    })();
  }, [projectId]);

  const DEFAULT_TEMPLATE = {
    productName: "Executive Summary",
    template_path: "uploads/default_exec_summary.pdf.html",
    template_body_path: "uploads/default_exec_summary.pdf_body.html",
  };

  const gatherAarContent = async (): Promise<string> => {
    const contents: string[] = [];
    for (const aar of aars) {
      if (isTextObservation(aar)) {
        const text = aar.description?.trim();
        if (text) {
          contents.push(`--- ${aar.name} (Observation by ${aar.contributor_name || "Unknown"}) ---\n${text}`);
        }
      } else {
        try {
          const response = await fetch(`/api/aar/${aar.id}/transcript`);
          if (response.ok) {
            const transcriptData = await response.json();
            if (transcriptData.length > 0) {
              const transcriptText = transcriptData
                .map((entry: any) => `[${entry.timestamp}] ${entry.speaker}: ${entry.text}`)
                .join("\n");
              contents.push(`--- ${aar.name} (Audio Recording by ${aar.contributor_name || "Unknown"}) ---\n${transcriptText}`);
            }
          }
        } catch (err) {
          console.error(`Error fetching transcript for AAR ${aar.id}:`, err);
        }
      }
    }
    return contents.join("\n\n");
  };

  const openDocEditor = async (htmlLink: string, docxLink = "", productName = "", sessionId = "") => {
    try {
      let htmlContent = "";
      if (htmlLink) {
        const response = await fetch(`${htmlLink}?t=${Date.now()}`);
        if (response.ok) htmlContent = await response.text();
      }
      if (!htmlContent && docxLink) {
        const docxResponse = await fetch(`${docxLink}?t=${Date.now()}`);
        if (docxResponse.ok) {
          const arrayBuffer = await docxResponse.arrayBuffer();
          htmlContent = (await mammoth.convertToHtml({ arrayBuffer })).value;
        }
      }
      if (htmlContent) {
        htmlContent = htmlContent.replace(/\[PAGE_BREAK\]/g, "<!-- pagebreak -->");
        setCurrentDocContent(htmlContent);
        setCurrentHtmlLink(htmlLink || "");
        setCurrentDocxLink(docxLink || "");
        setCurrentProductName(productName || "Executive Summary");
        setCurrentSessionId(sessionId);
        setDocEditorOpen(true);
      } else {
        setGenerationError("Could not load the generated document.");
      }
    } catch (error: any) {
      setGenerationError(`Error loading document: ${error?.message || error}`);
    }
  };

  const generateExecutiveSummary = async () => {
    if (aars.length === 0) return;

    setGeneratingExecSummary(true);
    setGenerationProgress("Gathering lesson learned inputs...");
    setGenerationError(null);

    try {
      const aarContent = await gatherAarContent();
      if (!aarContent.trim()) {
        setGenerationError("No content found in lesson learned inputs.");
        setGeneratingExecSummary(false);
        return;
      }

      const templateConfig = selectedTemplate || DEFAULT_TEMPLATE;

      setGenerationProgress("Connecting to generation service...");
      const protocolPrefix = window.location.protocol === "https:" ? "wss:" : "ws:";
      let host = window.location.host;
      if (host.startsWith("localhost")) host = "localhost:8000";

      const sessionId = `exec-summary-${Date.now()}`;
      const ws = new WebSocket(`${protocolPrefix}//${host}/ws/chat/${sessionId}`);
      wsRef.current = ws;

      ws.onopen = () => {
        setGenerationProgress("Starting document generation...");
        ws.send(JSON.stringify({
          action: "chat",
          userId: user.userId,
          email: user.email,
          projectId: projectId,
          sessionId,
          session: [],
          question: {
            prompt: `Create an After Action Review executive summary report for senior leadership.\n\nThe following are lesson learned inputs (observations and hotwash transcripts) from the event:\n\n${aarContent}\n\nSummarize the event scope and context. Identify key findings across all inputs. List main recommendations. Present a concise executive overview with key findings and recommendations.`,
            productType: {
              productName: templateConfig.productName || "Executive Summary",
              template_path: templateConfig.template_path,
              template_body_path: templateConfig.template_body_path,
            },
          },
          docs: [],
          baseline: { docs: false, links: false },
          chatSettings: {
            answerSize: "detailed",
            shouldCiteSources: false,
            includeRelatedQuestions: false,
            creativity: "Regular",
          },
        }));
      };

      ws.onmessage = async ({ data: raw }) => {
        const data = JSON.parse(raw);

        if (data.type === "status") {
          setGenerationProgress(data.message);
          return;
        }

        // Final result
        setGeneratingExecSummary(false);
        setGenerationProgress("");
        ws.close();

        if (data.htmlLink) {
          setLatestSummary({ htmlLink: data.htmlLink, docxLink: data.docxLink || "", sessionId });
          await openDocEditor(data.htmlLink, data.docxLink, data.productName || "Executive Summary", sessionId);
        } else {
          setGenerationError("Generation completed but no document was produced. Please try again.");
        }
      };

      ws.onerror = () => {
        setGenerationError("Connection to generation service failed. Please try again.");
        setGeneratingExecSummary(false);
        setGenerationProgress("");
      };

      ws.onclose = () => {
        wsRef.current = null;
      };
    } catch (err) {
      setGenerationError(err instanceof Error ? err.message : "An unexpected error occurred.");
      setGeneratingExecSummary(false);
      setGenerationProgress("");
    }
  };

  const cancelGeneration = () => {
    wsRef.current?.close();
    setGeneratingExecSummary(false);
    setGenerationProgress("");
  };

  const fetchProductTypes = async () => {
    setLoadingProducts(true);
    try {
      const response = await fetch("/api/product_types");
      if (response.ok) setProductTypes(await response.json());
    } catch (e) {
      console.error("Error fetching product types:", e);
    } finally {
      setLoadingProducts(false);
    }
  };

  if (!project) {
    return (
      <DefaultPageLayout>
        <div className="flex h-full w-full items-center justify-center">
          <span className="text-body font-body text-subtext-color">
            Loading event details...
          </span>
        </div>
      </DefaultPageLayout>
    );
  }

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full flex-col items-start gap-2">
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 px-4 py-4 overflow-auto">
          {/* Header Section */}
          <div className="flex w-full items-center justify-between border-b border-solid border-neutral-border bg-neutral-50 px-6 py-4 rounded-md">
            <div className="flex items-center gap-4">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-2 font-heading-2 text-default-font">
                  {project.name}
                </span>
                {project.eventDate && (
                  <span className="text-body font-body text-subtext-color">
                    {project.eventDate}
                  </span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-4">
              <div className="flex flex-col items-end gap-1">
                <Badge
                  variant={getEventStatusBadgeVariant(project.status)}
                  icon="FeatherClock"
                >
                  {project.status}
                </Badge>
                <span className="text-caption font-caption text-subtext-color">
                  {aars.length} {aars.length === 1 ? "input" : "inputs"}
                </span>
              </div>
            </div>
          </div>

          {/* Lesson Learned List Section */}
          <div className="flex w-full flex-col items-start gap-6 rounded-md border border-solid border-neutral-border bg-default-background px-6 py-6">
            <div className="flex w-full items-center justify-between">
              <span className="text-heading-3 font-heading-3 text-default-font">
                Lesson Learned Inputs
              </span>
            </div>

            {loading ? (
              <div className="flex w-full items-center justify-center py-8">
                <span className="text-body font-body text-subtext-color">
                  Loading lesson learned inputs...
                </span>
              </div>
            ) : error ? (
              <div className="flex w-full items-center justify-center py-8">
                <span className="text-body font-body text-red-600">
                  {error}
                </span>
              </div>
            ) : aars.length === 0 ? (
              <div className="flex w-full flex-col items-center justify-center gap-4 py-12">
                <IconWithBackground variant="brand" size="large" icon="FeatherMic" />
                <div className="flex flex-col items-center gap-2">
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    No lesson learned inputs yet
                  </span>
                  <span className="text-body font-body text-subtext-color text-center">
                    Create lesson learned inputs from the Event Dashboard to see them here.
                  </span>
                </div>
              </div>
            ) : (
              <>
                <Table
                  header={
                    <Table.HeaderRow>
                      <Table.HeaderCell>Type</Table.HeaderCell>
                      <Table.HeaderCell>Title</Table.HeaderCell>
                      <Table.HeaderCell>Timestamp</Table.HeaderCell>
                      <Table.HeaderCell>Contributor</Table.HeaderCell>
                      <Table.HeaderCell />
                    </Table.HeaderRow>
                  }
                >
                  {paginatedAars.map((aar) => (
                    <Table.Row key={aar.id}>
                      <Table.Cell>
                        <div className="flex items-center gap-2">
                          <IconWithBackground
                            variant="brand"
                            size="small"
                            icon={isTextObservation(aar) ? "FeatherFileText" : "FeatherMic"}
                          />
                          <span className="text-body font-body text-default-font">
                            {isTextObservation(aar) ? "Observation" : "Hotwash"}
                          </span>
                        </div>
                      </Table.Cell>
                      <Table.Cell>
                        <span className="text-body-bold font-body-bold text-default-font">
                          {aar.name}
                        </span>
                      </Table.Cell>
                      <Table.Cell>
                        <span className="text-body font-body text-subtext-color">
                          {formatTimestamp(aar.created_at)}
                        </span>
                      </Table.Cell>
                      <Table.Cell>
                        <div className="flex items-center gap-2">
                          <Avatar size="x-small" image="">
                            {getInitials(aar.contributor_name)}
                          </Avatar>
                          <span className="text-body font-body text-default-font">
                            {aar.contributor_name ?? "—"}
                          </span>
                        </div>
                      </Table.Cell>
                      <Table.Cell>
                        <div className="flex items-center justify-end gap-2">
                          <IconButton
                            variant="neutral-tertiary"
                            size="small"
                            icon={isTextObservation(aar) ? "FeatherEdit3" : "FeatherFileText"}
                            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                              e.stopPropagation();
                              if (isTextObservation(aar)) {
                                setEditAarTarget(aar);
                                setEditObservationModalOpen(true);
                              } else {
                                handleAARClick(aar.id, aar.name);
                              }
                            }}
                            title={isTextObservation(aar) ? "Edit observation" : "View details"}
                          />
                          <IconButton
                            variant="neutral-tertiary"
                            size="small"
                            icon="FeatherDownload"
                            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                              e.stopPropagation();
                              downloadTranscript(aar);
                            }}
                            title={isTextObservation(aar) ? "Download observation" : "Download transcript"}
                          />
                          <IconButton
                            variant="neutral-tertiary"
                            size="small"
                            icon="FeatherTrash2"
                            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                              e.stopPropagation();
                              setOpenConfirmDeleteAar(true);
                              setDeleteAarTarget(aar);
                            }}
                            title="Delete"
                          />
                        </div>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table>
                <div className="flex w-full items-center justify-between">
                  <span className="text-caption font-caption text-subtext-color">
                    Showing {startItem}-{endItem} of {aars.length} inputs
                  </span>
                  <div className="flex items-center gap-2">
                    <IconButton
                      disabled={currentPage === 1}
                      variant="neutral-tertiary"
                      size="small"
                      icon="FeatherChevronLeft"
                      onClick={() =>
                        setCurrentPage((prev) => Math.max(1, prev - 1))
                      }
                    />
                    <IconButton
                      disabled={currentPage >= totalPages}
                      variant="neutral-tertiary"
                      size="small"
                      icon="FeatherChevronRight"
                      onClick={() =>
                        setCurrentPage((prev) =>
                          Math.min(totalPages, prev + 1)
                        )
                      }
                    />
                  </div>
                </div>
              </>
            )}
          </div>

          {/* AI-Powered Lesson Learned Analysis */}
          <AarAnalysisPanel aars={aars} projectId={projectId || ""} />

          {/* Report Generation */}
          <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-6 py-6">
            <div className="flex w-full items-center justify-between">
              <div className="flex items-center gap-2">
                <SubframeCore.Icon
                  className="text-heading-3 font-heading-3 text-subtext-color"
                  name="FeatherFileText"
                />
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Report Generation
                </span>
              </div>
            </div>
            <div className="flex w-full items-stretch gap-4">
              {/* Executive Summary — Active */}
              <div className="flex grow shrink-0 basis-0 flex-col items-start justify-between gap-3 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                <div className="flex items-center gap-2">
                  <IconWithBackground
                    variant="brand"
                    size="medium"
                    icon="FeatherFileText"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Executive Summary
                  </span>
                </div>
                <span className="text-caption font-caption text-subtext-color">
                  High-level overview for senior leadership with key findings and
                  recommendations
                </span>
                {/* Template indicator */}
                <div className="flex w-full items-center gap-2">
                  <SubframeCore.Icon className="text-caption text-subtext-color" name="FeatherFile" />
                  <span className="text-caption font-caption text-subtext-color truncate">
                    {selectedTemplate ? selectedTemplate.productName : "Default Template"}
                  </span>
                  {selectedTemplate && (
                    <LinkButton variant="neutral" size="small" onClick={() => setSelectedTemplate(null)}>
                      Reset
                    </LinkButton>
                  )}
                </div>
                {generationError && (
                  <span className="text-caption font-caption text-red-600">
                    {generationError}
                  </span>
                )}
                <div className="flex items-center gap-2">
                  <Button
                    variant="brand-primary"
                    size="small"
                    icon={generatingExecSummary ? "FeatherLoader" : "FeatherFilePlus"}
                    disabled={generatingExecSummary || aars.length === 0}
                    loading={generatingExecSummary}
                    onClick={generateExecutiveSummary}
                  >
                    {generatingExecSummary ? "Generating..." : "Generate"}
                  </Button>
                  {latestSummary && (
                    <Button
                      variant="neutral-secondary"
                      size="small"
                      icon="FeatherEye"
                      disabled={generatingExecSummary}
                      onClick={() => openDocEditor(latestSummary.htmlLink, latestSummary.docxLink, "Executive Summary", latestSummary.sessionId)}
                    >
                      View Latest
                    </Button>
                  )}
                  <Button
                    variant="neutral-secondary"
                    size="small"
                    icon="FeatherRepeat"
                    disabled={generatingExecSummary}
                    onClick={() => {
                      fetchProductTypes();
                      setOpenProductLibrary(true);
                    }}
                  >
                    Change Template
                  </Button>
                </div>
              </div>
              {/* Lessons-Learned Brief — Coming Soon */}
              <div className="flex grow shrink-0 basis-0 flex-col items-start justify-between gap-3 rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4 opacity-60 pointer-events-none">
                <div className="flex items-center gap-2">
                  <IconWithBackground
                    variant="warning"
                    size="medium"
                    icon="FeatherLightbulb"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Lessons-Learned Brief
                  </span>
                  <Badge variant="neutral">Coming Soon</Badge>
                </div>
                <span className="text-caption font-caption text-subtext-color">
                  Detailed analysis of lessons identified with supporting evidence and
                  recommendations
                </span>
                <Button variant="brand-secondary" size="small" icon="FeatherFilePlus" disabled>
                  Generate
                </Button>
              </div>
              {/* Decision Timeline — Coming Soon */}
              <div className="flex grow shrink-0 basis-0 flex-col items-start justify-between gap-3 rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4 opacity-60 pointer-events-none">
                <div className="flex items-center gap-2">
                  <IconWithBackground
                    variant="success"
                    size="medium"
                    icon="FeatherActivity"
                  />
                  <span className="text-body-bold font-body-bold text-default-font">
                    Decision Timeline Report
                  </span>
                  <Badge variant="neutral">Coming Soon</Badge>
                </div>
                <span className="text-caption font-caption text-subtext-color">
                  Chronological sequence of decisions, RFIs, and world state changes
                </span>
                <Button variant="brand-secondary" size="small" icon="FeatherFilePlus" disabled>
                  Generate
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <DeleteAar
        open={openConfirmDeleteAar}
        setOpen={setOpenConfirmDeleteAar}
        aar={deleteAarTarget}
        onDeleteSuccess={() => {
          fetchAARs();
          setCurrentPage(1);
        }}
      />

      {/* Edit Observation Modal */}
      <AddObservationsModal
        open={editObservationModalOpen}
        onOpenChange={setEditObservationModalOpen}
        projectId={projectId}
        onSuccess={fetchAARs}
        editMode={true}
        existingAar={editAarTarget}
      />

      {/* Generation Progress Modal */}
      {generatingExecSummary && !docEditorOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#00000099]">
          <div className="flex flex-col items-center gap-4 rounded-md border border-solid border-neutral-border bg-default-background p-8 shadow-lg max-w-md">
            <div className="flex items-center gap-3">
              <SubframeCore.Icon
                className="text-default-font animate-spin text-2xl"
                name="FeatherLoader"
              />
              <span className="text-heading-3 font-heading-3 text-default-font">
                Generating Executive Summary
              </span>
            </div>
            <span className="text-body font-body text-subtext-color text-center">
              {generationProgress || "Preparing..."}
            </span>
            <Button
              variant="neutral-secondary"
              size="small"
              onClick={cancelGeneration}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* DocEditor Overlay */}
      {docEditorOpen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-default-background">
          <div className="flex w-full items-center gap-2 border-b border-solid border-neutral-border bg-default-background px-3 py-1.5">
            <IconButton
              size="small"
              icon="FeatherArrowLeft"
              onClick={() => {
                if (!editorIsDirty) {
                  setDocEditorOpen(false);
                  setCurrentDocContent("");
                  setCurrentHtmlLink("");
                  setEditorIsDirty(false);
                } else {
                  setShowDiscardModal(true);
                }
              }}
            />
            <span className="text-body-bold font-body-bold text-default-font">
              Document Editor — Executive Summary
            </span>
          </div>
          <div className="flex-1 overflow-auto">
            <DocEditor
              initialValue={currentDocContent}
              htmlLink={currentHtmlLink}
              docxLink={currentDocxLink}
              productName={currentProductName}
              sessionId={currentSessionId}
              onDirtyChange={(dirty: boolean) => setEditorIsDirty(dirty)}
              onRegisterSave={(saveFn: () => Promise<void>) => { editorSaveRef.current = saveFn; }}
              onClose={() => {
                setDocEditorOpen(false);
                setCurrentDocContent("");
                setCurrentHtmlLink("");
                setEditorIsDirty(false);
              }}
              onSaveStart={async (html: string, docSettings: any) => {
                try {
                  const formData = new FormData();
                  formData.append("doc_link", currentHtmlLink);
                  formData.append("content", html);
                  formData.append("session_id", currentSessionId);
                  formData.append("project_id", projectId || "");
                  formData.append("product_name", currentProductName || "Executive Summary");
                  formData.append("user_id", user?.email || user?.userId || "");
                  if (docSettings?.header) formData.append("header", docSettings.header);
                  if (docSettings?.headerStyle) formData.append("header_style", JSON.stringify(docSettings.headerStyle));
                  if (docSettings?.watermark) formData.append("watermark", docSettings.watermark);
                  if (docSettings?.watermarkColor) formData.append("watermark_color", docSettings.watermarkColor);
                  if (docSettings?.watermarkFont) formData.append("watermark_font", docSettings.watermarkFont);
                  if (docSettings?.watermarkSize) formData.append("watermark_size", docSettings.watermarkSize);
                  const result = await fetch("/api/save/", { method: "POST", body: formData }).then((r) => r.json());
                  if (result.htmlLink && result.docxLink) {
                    setCurrentHtmlLink(result.htmlLink);
                    setCurrentDocxLink(result.docxLink);
                    setLatestSummary({ htmlLink: result.htmlLink, docxLink: result.docxLink, sessionId: currentSessionId });
                  }
                  setDocEditorOpen(false);
                  setShowDiscardModal(false);
                  setEditorIsDirty(false);
                } catch (err: any) {
                  console.error("[LessonLearned] Save failed:", err);
                }
              }}
            />
          </div>
        </div>
      )}

      {/* Discard Changes Dialog */}
      <DialogLayout open={showDiscardModal} onOpenChange={setShowDiscardModal}>
        <div className="flex w-full flex-col items-start gap-6 rounded-md border border-solid border-neutral-border bg-default-background p-6 shadow-lg">
          <div className="flex flex-col items-start gap-2">
            <span className="text-heading-3 font-heading-3 text-default-font">Unsaved Changes</span>
            <span className="text-body font-body text-subtext-color">You have unsaved changes. Would you like to save before leaving?</span>
          </div>
          <div className="flex w-full items-center justify-end gap-3">
            <Button variant="neutral-secondary" onClick={() => {
              setShowDiscardModal(false);
              setDocEditorOpen(false);
              setCurrentDocContent("");
              setCurrentHtmlLink("");
              setEditorIsDirty(false);
            }}>Discard</Button>
            <Button variant="brand-primary" onClick={async () => {
              if (editorSaveRef.current) await editorSaveRef.current();
            }}>Save</Button>
          </div>
        </div>
      </DialogLayout>

      {/* Product Library Modal for template selection */}
      <ProductLibraryModal
        open={openProductLibrary}
        setOpen={setOpenProductLibrary}
        productTypes={productTypes}
        loading={loadingProducts}
        setProductType={(pt: any) => {
          setSelectedTemplate(pt);
          setOpenProductLibrary(false);
        }}
        onNewProductType={(pt: any) => {
          setProductTypes((prev) => [pt, ...prev.filter((p) => p.productTypeId !== pt.productTypeId)]);
          setSelectedTemplate(pt);
        }}
        onDeleteProductType={(id: string) => {
          setProductTypes((prev) => prev.filter((p) => p.productTypeId !== id));
          if (selectedTemplate?.productTypeId === id) setSelectedTemplate(null);
        }}
      />
    </DefaultPageLayout>
  );
}

export default LessonLearnedDashboard;
