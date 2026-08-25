"use client";

/**
 * Support-ticket form mounted on the left-nav "Submit Feedback"
 * link. POSTs to /api/tickets which persists into the support_ticket
 * table. Replaces the prior mailto / "Email us" placeholder.
 */

import React, { useState } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import { Button } from "../subframe/components/Button";
import { TextField } from "../subframe/components/TextField";
import { TextArea } from "../subframe/components/TextArea";
import { Select } from "../subframe/components/Select";
import { useAppContext } from "../lib/contextLib";

interface FeedbackProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  // The legacy `message` prop is ACCEPTED for backwards compat with
  // existing call sites in DefaultPageLayout.tsx but DELIBERATELY
  // IGNORED — its value was the old "Please send your email to
  // team@pytho.ai" placeholder. The form self-describes.
  message?: string;
}

const CATEGORIES = [
  { value: "bug", label: "Bug" },
  { value: "feature_request", label: "Feature request" },
  { value: "doctrine", label: "Doctrine / accuracy" },
  { value: "performance", label: "Performance" },
  { value: "other", label: "Other" },
];

function Feedback({ open, setOpen, message: _legacyMessage }: FeedbackProps) {
  const { user } = useAppContext();
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [category, setCategory] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submittedId, setSubmittedId] = useState<number | null>(null);

  function reset() {
    setTitle("");
    setDescription("");
    setCategory("");
    setError(null);
    setSubmitting(false);
    setSubmittedId(null);
  }

  function close() {
    reset();
    setOpen(false);
  }

  async function submit() {
    setError(null);
    if (!title.trim()) {
      setError("Title is required.");
      return;
    }
    if (!description.trim()) {
      setError("Description is required.");
      return;
    }
    setSubmitting(true);
    try {
      const r = await fetch("/api/tickets", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          description: description.trim(),
          category: category || undefined,
          user_email: user?.userId || undefined,
        }),
      });
      if (!r.ok) {
        const body = await r.text().catch(() => "");
        throw new Error(body || `HTTP ${r.status}`);
      }
      const data = await r.json();
      setSubmittedId(Number(data?.ticket_id) || null);
    } catch (e: any) {
      setError(e?.message || "Failed to submit ticket.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <DialogLayout open={open} onOpenChange={(o: boolean) => (o ? null : close())}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 px-6 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Feedback
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={close}
                aria-label="Close"
              />
            </div>

            {submittedId !== null ? (
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
                <div className="flex flex-col items-start gap-2">
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    Thanks — we got it.
                  </span>
                  <span className="text-body font-body text-subtext-color">
                    Your ticket has been saved (#{submittedId}). Engineering
                    pulls these on a regular cadence; a teammate will follow
                    up if more detail is needed.
                  </span>
                </div>
                <div className="flex w-full items-center justify-end gap-2">
                  <Button variant="neutral-secondary" onClick={close}>
                    Close
                  </Button>
                  <Button
                    variant="brand-tertiary"
                    onClick={() => {
                      reset();
                    }}
                  >
                    Submit another
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 px-6 py-6">
                <div className="flex flex-col items-start gap-1">
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    Send feedback
                  </span>
                  <span className="text-body font-body text-subtext-color">
                    Report bugs, request features, or flag doctrine
                    concerns. Tickets go to engineering.
                  </span>
                </div>

                <div className="flex w-full flex-col gap-3">
                  <TextField
                    className="w-full"
                    variant="filled"
                    label="Title"
                    helpText=""
                  >
                    <TextField.Input
                      placeholder="One-line summary"
                      value={title}
                      onChange={(e: React.ChangeEvent<HTMLInputElement>) => {
                        setTitle(e.target.value);
                        setError(null);
                      }}
                    />
                  </TextField>

                  <Select
                    className="w-full"
                    variant="filled"
                    label="Category"
                    placeholder="Choose one (optional)"
                    helpText=""
                    value={category || undefined}
                    onValueChange={(v: string) => setCategory(v)}
                  >
                    {CATEGORIES.map((opt) => (
                      <Select.Item key={opt.value} value={opt.value}>
                        {opt.label}
                      </Select.Item>
                    ))}
                  </Select>

                  <TextArea
                    className="h-auto w-full"
                    variant="filled"
                    label="Description"
                    helpText=""
                  >
                    <TextArea.Input
                      placeholder="What happened, what did you expect, repro steps if applicable…"
                      className="min-h-[140px] resize-none"
                      value={description}
                      onChange={(
                        e: React.ChangeEvent<HTMLTextAreaElement>,
                      ) => {
                        setDescription(e.target.value);
                        setError(null);
                      }}
                    />
                  </TextArea>

                  {error && (
                    <div className="rounded-md border border-solid border-error-300 bg-error-50 px-3 py-2">
                      <span className="text-caption font-caption text-error-700">
                        {error}
                      </span>
                    </div>
                  )}
                </div>

                <div className="flex w-full items-center justify-end gap-2">
                  <Button variant="neutral-secondary" onClick={close}>
                    Cancel
                  </Button>
                  <Button
                    variant="brand-primary"
                    onClick={submit}
                    disabled={submitting}
                  >
                    {submitting ? "Submitting…" : "Submit ticket"}
                  </Button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default Feedback;
