"use client";

import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";

interface DeleteProjectProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  projectId: string;
  onDelete?: () => void;
}

function DeleteProject({ open, setOpen, projectId, onDelete }: DeleteProjectProps) {
  const [isLoading, setIsLoading] = useState(false);
  const { user, projectMap, setProjectMap, setProjectId } = useAppContext();
  const nav = useNavigate();

  const projectName = projectMap[projectId]?.projectDetails?.name || "this event";

  async function deleteCurrentProject() {
    const url = `/api/projects/${encodeURIComponent(projectId)}?user_id=${encodeURIComponent(user.userId)}`;

    const response = await fetch(url, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    });

    if (!response.ok) {
      const err = await response.text();
      throw new Error(`Failed to delete project: ${err}`);
    }

    delete projectMap[projectId];
    setProjectMap({ ...projectMap });
    setProjectId("default");

    if (onDelete) {
      onDelete();
    } else {
      nav("/missioncommand/whitecell");
    }
  }

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Delete Event
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={() => {
                  setOpen(false);
                }}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-body font-body text-default-font">
                  Are you sure you want to delete <b>{projectName}</b>?
                </span>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  icon="FeatherArrowLeft"
                  onClick={() => {
                    setOpen(false);
                  }}
                >
                  Cancel
                </Button>
                <Button
                  loading={isLoading}
                  className="h-8 grow shrink-0 basis-0"
                  iconRight="FeatherArrowRight"
                  onClick={async () => {
                    setIsLoading(true);
                    try {
                      await deleteCurrentProject();
                      setOpen(false);
                    } catch (error) {
                      console.error("Error deleting project:", error);
                    } finally {
                      setIsLoading(false);
                    }
                  }}
                >
                  Confirm
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default DeleteProject;
