"use client";

import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { TextField } from "../subframe/components/TextField";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { useAppContext } from "../lib/contextLib";

interface CreateRecordingModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string | undefined;
}

function CreateRecordingModal({
  open,
  onOpenChange,
  projectId,
}: CreateRecordingModalProps) {
  const nav = useNavigate();
  const { setProjectId } = useAppContext();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  const handleCreate = async () => {
    if (!name.trim() || !projectId) return;

    setError("");
    setIsSubmitting(true);

    try {
      const response = await fetch("/api/aar", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim() || null,
          event_id: projectId,
        }),
      });

      if (!response.ok) {
        throw new Error("Failed to create recording");
      }

      const data = await response.json();
      setProjectId(projectId);
      onOpenChange(false);
      nav(`/missioncommand/events/aar/${data.id}`, {
        state: {
          aarName: data.name,
          projectId,
          fromLessonLearnedDashboard: false,
        },
      });
    } catch (err) {
      console.error("Error creating recording:", err);
      setError("Failed to create recording. Please try again.");
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <DialogLayout open={open} onOpenChange={onOpenChange}>
      <div className="flex h-full max-h-[768px] w-full flex-col items-start gap-2">
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-center">
          <div className="flex w-full items-center border-b border-solid border-neutral-border px-4 py-4">
            <IconButton
              variant="neutral-tertiary"
              size="medium"
              icon="FeatherX"
              onClick={() => onOpenChange(false)}
            />
            <div className="flex grow shrink-0 basis-0 items-center justify-center gap-2 pr-6">
              <span className="text-heading-2 font-heading-2 text-default-font">
                Record Hotwash
              </span>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-6 px-8 py-8 overflow-auto">
            <div className="flex w-full flex-col items-start gap-6">
              <TextField
                className="h-auto w-full flex-none"
                label="Recording Name"
                helpText=""
              >
                <TextField.Input
                  placeholder="Enter the name of your recording"
                  value={name}
                  onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                    setName(e.target.value)
                  }
                />
              </TextField>
              <TextArea
                className="h-auto w-full flex-none"
                label="Description"
                helpText=""
              >
                <TextArea.Input
                  placeholder="Briefly describe your recording."
                  value={description}
                  onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                    setDescription(e.target.value)
                  }
                />
              </TextArea>
            </div>
            {error && (
              <div className="flex w-full items-center gap-2 rounded-md border border-solid border-error bg-error-50 px-4 py-3">
                <span className="text-caption font-caption text-error">
                  {error}
                </span>
              </div>
            )}
          </div>
        </div>
        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
        <div className="flex h-11 w-full flex-none items-center justify-end gap-2 px-4">
          <div className="flex grow shrink-0 basis-0 items-center justify-end gap-2">
            <Button
              variant="brand-secondary"
              onClick={() => onOpenChange(false)}
              disabled={isSubmitting}
            >
              Cancel
            </Button>
            <Button
              onClick={handleCreate}
              disabled={!name.trim() || isSubmitting}
            >
              {isSubmitting ? "Creating..." : "Create & Record"}
            </Button>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default CreateRecordingModal;
