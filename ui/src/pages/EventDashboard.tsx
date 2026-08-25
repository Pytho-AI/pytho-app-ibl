"use client";

import React, { useEffect, useState } from "react";
import { Badge } from "../subframe/components/Badge";
import { Button } from "../subframe/components/Button";
import { DataFieldHorizontal } from "../subframe/components/DataFieldHorizontal";
import { IconButton } from "../subframe/components/IconButton";
import { Tabs } from "../subframe/components/Tabs";
import { Table } from "../subframe/components/Table";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { useNavigate, useLocation, useParams } from "react-router-dom";
import AARSection from "../components/AarSection";
import { useAppContext } from "../lib/contextLib";
import { useProjectData } from "../lib/useProjectData";
import ActivityLogViewer from "../components/ActivityLogViewer";
import DeleteProject from "../components/DeleteProject";
import { getEventStatusBadgeVariant } from "../lib/statusUtils";

interface ProjectDetails {
  projectId: string;
  userId?: string;
  name: string;
  description?: string;
  status: string;
  eventDate?: string;
  createdAt: number;
  updatedAt: number;
}

function EventDashboard() {
  const nav = useNavigate();
  const location = useLocation();
  const { projectId: projectIdFromParams } = useParams<{ projectId: string }>();
  const { user, projectMap, projectId: projectIdFromContext, setProjectId } = useAppContext();

  // Use projectId from URL params (source of truth), fall back to context
  const projectId = projectIdFromParams ?? projectIdFromContext;

  // Automatically load projectMap if missing
  useProjectData(projectId);

  // Update context projectId from URL
  useEffect(() => {
    if (projectIdFromParams && projectIdFromParams !== projectIdFromContext) {
      setProjectId(projectIdFromParams);
    }
  }, [projectIdFromParams, projectIdFromContext, setProjectId]);

  const [project, setProject] = useState<ProjectDetails | null>(null);
  const [activeTab, setActiveTab] = useState<"information" | "data">("information");
  const [openConfirmDeleteProject, setOpenConfirmDeleteProject] = useState(false);

  useEffect(() => {
    if (projectId && projectMap[projectId]?.projectDetails) {
      setProject(projectMap[projectId].projectDetails);
    }
  }, [projectId, projectMap]);

  // Get the project owner's userId
  const projectOwnerId = projectMap[projectId]?.projectDetails?.userId;

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
          <div className="flex w-full items-center gap-4">
            <span className="text-heading-2 font-heading-2 text-default-font">
              Event Management: {project.name}
            </span>
          </div>
          <div className="flex w-full items-center justify-center rounded-md px-8 py-6 border-2 border-solid border-brand-300 bg-brand-50">
            <Button
              className="rounded-full"
              variant="brand-primary"
              size="large"
              iconRight="FeatherArrowRight"
              onClick={() => {
                setProjectId(projectId);
                nav("/missioncommand/session");
              }}
            >
              Create products
            </Button>
          </div>
          <div className="flex w-full flex-col items-start gap-4">
            <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
              <div className="flex w-full items-center justify-between">
                <div className="flex items-end flex-shrink-0">
                  <Tabs className="!w-auto [&>div:last-child]:hidden">
                    <Tabs.Item
                      active={activeTab === "information"}
                      onClick={() => setActiveTab("information")}
                    >
                      Event Information
                    </Tabs.Item>
                    <Tabs.Item
                      active={activeTab === "data"}
                      onClick={() => setActiveTab("data")}
                    >
                      Event Data
                    </Tabs.Item>
                  </Tabs>
                </div>
                <div className="flex items-center gap-2">
                  <IconButton
                    variant="neutral-primary"
                    size="small"
                    icon="FeatherEdit3"
                    onClick={() => {
                      setProjectId(projectId);
                      nav("/missioncommand/events/update", { state: { from: location.pathname } });
                    }}
                  />
                  {user?.userId === projectOwnerId && (
                    <IconButton
                      variant="neutral-primary"
                      size="small"
                      icon="FeatherTrash2"
                      onClick={() => {
                        setOpenConfirmDeleteProject(true);
                      }}
                    />
                  )}
                </div>
              </div>
              {activeTab === "information" && (
                <div className="flex w-full flex-col items-start gap-3">
                  <DataFieldHorizontal icon="FeatherFolderEdit" label="Event Name">
                    <span className="text-body-bold font-body-bold text-default-font">
                      {project.name}
                    </span>
                  </DataFieldHorizontal>
                  <DataFieldHorizontal icon="FeatherCalendar" label="Event Date">
                    <span className="text-body-bold font-body-bold text-default-font">
                      {project.eventDate || "Not set"}
                    </span>
                  </DataFieldHorizontal>
                  <DataFieldHorizontal icon="FeatherBarChart2" label="Status">
                    <Badge variant={getEventStatusBadgeVariant(project.status)}>
                      {project.status}
                    </Badge>
                  </DataFieldHorizontal>
                  <DataFieldHorizontal icon="FeatherText" label="Description">
                    <span className="text-body-bold font-body-bold text-default-font">
                      {project.description || "No description"}
                    </span>
                  </DataFieldHorizontal>
                </div>
              )}
              {activeTab === "data" && (
                <div className="flex w-full flex-col items-start gap-3">
                  {(() => {
                    const docs = projectMap[projectId]?.projectDetails?.docs || [];
                    return docs.length > 0 ? (
                      <Table
                        header={
                          <Table.HeaderRow>
                            <Table.HeaderCell>Name</Table.HeaderCell>
                          </Table.HeaderRow>
                        }
                      >
                        {docs.map((doc: any, index: number) => (
                          <Table.Row key={doc.key || index}>
                            <Table.Cell>
                              <a
                                href={doc.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-body-bold font-body-bold text-brand-700 hover:text-brand-800 hover:underline"
                              >
                                {doc.name || "Untitled Document"}
                              </a>
                            </Table.Cell>
                          </Table.Row>
                        ))}
                      </Table>
                    ) : (
                      <div className="flex w-full items-center justify-center py-8">
                        <span className="text-body font-body text-subtext-color">
                          No documents uploaded to this event
                        </span>
                      </div>
                    );
                  })()}
                </div>
              )}
            </div>
          </div>
          <AARSection />
          <div className="flex w-full items-start gap-4">
            <div className="flex w-full flex-col items-start gap-4">
              <ActivityLogViewer userId={user.userId} tag={"event:" + projectId} />
            </div>
          </div>
        </div>
      </div>
      <DeleteProject
        open={openConfirmDeleteProject}
        setOpen={setOpenConfirmDeleteProject}
        projectId={projectId}
        onDelete={() => {
          nav("/missioncommand/whitecell");
        }}
      />
    </DefaultPageLayout>
  );
}

export default EventDashboard;
