"use client";

import React, { useState, useEffect } from "react";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { TextField } from "../subframe/components/TextField";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import * as SubframeCore from "@subframe/core";

interface ExistingAar {
  id: number;
  name: string;
  description: string | null;
}

interface AddObservationsModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  projectId: string | undefined;
  onSuccess: () => void;
  editMode?: boolean;
  existingAar?: ExistingAar | null;
}

const OBSERVATION_TEMPLATE = `TOPIC:

OBSERVATIONS:
What is the issue?

DISCUSSION:
Discuss and identify the operational impact.

RECOMMENDATION:
What are the recommended improvements?

EVENT DESCRIPTION:
What specific event or series of events exposed this issue?

Name
Rank
Billet/Unit
Date`;

const ALLOWED_TEXT_EXTENSIONS = [".txt"];
const ACCEPT_ATTR = ".txt";

function AddObservationsModal({
  open,
  onOpenChange,
  projectId,
  onSuccess,
  editMode = false,
  existingAar = null,
}: AddObservationsModalProps) {
  const [title, setTitle] = useState("");
  const [observations, setObservations] = useState(OBSERVATION_TEMPLATE);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [uploadedFileName, setUploadedFileName] = useState("");
  const [uploadError, setUploadError] = useState("");
  const fileInputRef = React.useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) {
      setTitle("");
      setObservations("");
      setSubmitError("");
      setUploadedFileName("");
      setUploadError("");
    } else if (editMode && existingAar) {
      setTitle(existingAar.name);
      setObservations(existingAar.description?.trim() || OBSERVATION_TEMPLATE);
    } else {
      setObservations(OBSERVATION_TEMPLATE);
    }
  }, [open, editMode, existingAar]);

  const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (!file) return;
    setUploadError("");
    const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
    if (!ALLOWED_TEXT_EXTENSIONS.includes(ext)) {
      setUploadError(`Please upload a text file (${ALLOWED_TEXT_EXTENSIONS.join(", ")})`);
      if (fileInputRef.current) fileInputRef.current.value = "";
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const text = (reader.result as string) || "";
      setObservations(text);
      setUploadedFileName(file.name);
      if (!title.trim()) {
        const titleFromFile = file.name
          .replace(/\.[^/.]+$/, "")
          .replace(/[_-]/g, " ")
          .trim();
        setTitle(titleFromFile);
      }
    };
    reader.onerror = () => {
      setUploadError("Failed to read file.");
    };
    reader.readAsText(file, "UTF-8");
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const clearUploadedFile = () => {
    setUploadedFileName("");
    setObservations(OBSERVATION_TEMPLATE);
  };

  const handleSubmit = async () => {
    if (!title.trim()) {
      setSubmitError("Observation title is required.");
      return;
    }
    if (!editMode && !projectId) return;

    if (!editMode) {
      const hasModifiedObservations =
        observations.trim() !== OBSERVATION_TEMPLATE.trim();
      const hasUploadedFile = !!uploadedFileName.trim();
      if (!hasModifiedObservations && !hasUploadedFile) {
        setSubmitError("Please upload a text file or modify the observation content.");
        return;
      }
    }

    setSubmitError("");
    setIsSubmitting(true);

    try {
      if (editMode && existingAar) {
        const response = await fetch(`/api/aar/${existingAar.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: title.trim(),
            description: observations.trim() || null,
          }),
        });
        if (!response.ok) {
          throw new Error("Failed to update observation");
        }
      } else {
        const response = await fetch("/api/aar", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: title.trim(),
            description: observations.trim(),
            event_id: projectId,
          }),
        });
        if (!response.ok) {
          throw new Error("Failed to create observation");
        }
      }

      setTitle("");
      setObservations(OBSERVATION_TEMPLATE);
      setUploadedFileName("");
      onOpenChange(false);
      onSuccess();
    } catch (error) {
      console.error(editMode ? "Error updating observation:" : "Error creating observation:", error);
      setSubmitError(
        editMode ? "Failed to update observation. Please try again." : "Failed to create observation. Please try again."
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <DialogLayout open={open} onOpenChange={onOpenChange} className="min-w-[800px] max-w-[1000px]">
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                {editMode ? "Edit Observation" : "Add Observations"}
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={() => onOpenChange(false)}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  {editMode ? "Edit your observation" : "Add observations to your event"}
                </span>
                <span className="text-body font-body text-default-font">
                  {editMode
                    ? "Update the title or content"
                    : "Upload a text file or enter observations manually using the template below"}
                </span>
              </div>
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4">
                <TextField
                  className="h-auto w-full flex-none"
                  label="Observation Title"
                  helpText=""
                >
                  <TextField.Input
                    placeholder="Enter a title for your observation..."
                    value={title}
                    onChange={(e: React.ChangeEvent<HTMLInputElement>) =>
                      setTitle(e.target.value)
                    }
                  />
                </TextField>
                {!editMode && (
                  <div className="flex w-full flex-col items-start gap-2">
                    <input
                      ref={fileInputRef}
                      type="file"
                      accept={ACCEPT_ATTR}
                      onChange={handleFileUpload}
                      style={{ display: "none" }}
                    />
                    <div className="flex items-center gap-2">
                      <Button
                        variant="neutral-secondary"
                        icon="FeatherUpload"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={isSubmitting}
                      >
                        Upload text file
                      </Button>
                      {uploadedFileName && (
                        <Button
                          variant="neutral-tertiary"
                          size="small"
                          onClick={clearUploadedFile}
                          disabled={isSubmitting}
                        >
                          Clear file
                        </Button>
                      )}
                    </div>
                    <span
                      className={`text-caption font-caption ${
                        uploadError ? "text-error" : uploadedFileName ? "text-success" : "text-subtext-color"
                      }`}
                    >
                      {uploadError ||
                        (uploadedFileName ? `Uploaded: ${uploadedFileName}` : ".txt files supported")}
                    </span>
                  </div>
                )}
                <TextArea
                  className="h-auto w-full flex-none"
                  label={editMode ? "Observation Content" : "Observation Content (edit after upload or type here)"}
                  helpText=""
                >
                  <TextArea.Input
                    className="h-auto min-h-[200px] w-full flex-none"
                    placeholder="Type your observations here..."
                    value={observations}
                    onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) =>
                      setObservations(e.target.value)
                    }
                  />
                </TextArea>
              </div>
              {submitError && (
                <div className="flex w-full items-center gap-2 rounded-md border border-solid border-error bg-error-50 px-4 py-3">
                  <SubframeCore.Icon
                    className="text-body font-body text-error"
                    name="FeatherAlertTriangle"
                  />
                  <span className="text-caption font-caption text-error">
                    {submitError}
                  </span>
                </div>
              )}
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  variant="brand-secondary"
                  onClick={() => onOpenChange(false)}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleSubmit}
                  disabled={!title.trim() || isSubmitting}
                >
                  {isSubmitting
                    ? editMode ? "Updating..." : "Adding..."
                    : editMode ? "Update Observation" : "Add Observation"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default AddObservationsModal;
