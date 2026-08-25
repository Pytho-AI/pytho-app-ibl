"use client";

import React, { useState } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";

interface AAR {
  id: number;
  name: string;
}

interface DeleteAarProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  aar: AAR | null;
  onDeleteSuccess?: () => void;
}

function DeleteAar({ open, setOpen, aar, onDeleteSuccess }: DeleteAarProps) {
  const [isLoading, setIsLoading] = useState(false);

  async function deleteAAR(aarId: number) {
    try {
      const response = await fetch(`/api/aar/${aarId}`, {
        method: "DELETE",
      });

      if (response.status === 204) {
        return true;
      }

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to delete lesson learned input");
      }

      return true;
    } catch (error) {
      console.error("Error deleting lesson learned input:", error);
      return false;
    }
  }

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Delete Lesson Learned Input
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
                  Are you sure you want to delete{" "}
                  <b>{aar?.name || "this lesson learned input"}</b>?
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
                    if (!aar) return;
                    setIsLoading(true);
                    const success = await deleteAAR(aar.id);
                    if (success) {
                      setOpen(false);
                      if (onDeleteSuccess) {
                        onDeleteSuccess();
                      }
                    }
                    setIsLoading(false);
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

export default DeleteAar;
