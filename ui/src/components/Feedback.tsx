"use client";

import React, { useState } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { Button } from "../subframe/components/Button";
function Feedback({ open, setOpen, message, feedbackId }) {
  const [feedback, setFeedback] = useState("");

  const submitFeedback = async () => {
    if (feedback.trim().length < 3) return;

    if (feedbackId) {
      await fetch(`/api/feedback/${feedbackId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ comment: feedback }),
      });
    }

    setFeedback("")
    setOpen(false)
  };

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 px-6 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Welcome
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setOpen(false)
                }}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Share your feedback
                </span>
                <span className="text-body font-body text-default-font">
                  { message }
                </span>
              </div>
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6">
                <TextArea
                  className="h-auto w-full flex-none"
                  label=""
                  helpText=""
                >
                  <TextArea.Input
                    autoFocus
                    className="h-auto min-h-[96px] w-full flex-none resize-none"
                    placeholder="Share your feedback here..."
                    value={feedback}
                    onChange={(
                      event: React.ChangeEvent<HTMLInputElement>
                    ) => setFeedback(event.target.value)
                    }
                    onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                      if (event.keyCode === 13) {
                        submitFeedback()
                      }
                    }}
                  />
                </TextArea>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  icon="FeatherArrowLeft"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpen(false)
                  }}
                >
                  Back
                </Button>
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  iconRight="FeatherArrowRight"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    submitFeedback()
                  }}
                >
                  Submit
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default Feedback;