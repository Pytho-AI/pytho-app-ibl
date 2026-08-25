"use client";

import React from "react";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { DialogLayout } from "../subframe/layouts/DialogLayout";

interface DeleteProductModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  productName: string;
  onConfirmDelete: () => void;
  deleting?: boolean;
  heading?: string;
  error?: string | null;
}

function DeleteProductModal({ open, setOpen, productName, onConfirmDelete, deleting = false, heading = "Delete Custom Product", error = null }: DeleteProductModalProps) {
  return (
    <DialogLayout open={open} onOpenChange={setOpen}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                {heading}
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={() => setOpen(false)}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex w-full items-center gap-3">
                <IconWithBackground
                  variant="error"
                  size="medium"
                  icon="FeatherAlertTriangle"
                />
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1">
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    Delete "{productName}"?
                  </span>
                  <span className="text-body font-body text-default-font">
                    Are you sure you want to delete this product? This action
                    cannot be undone.
                  </span>
                </div>
              </div>
              {error && (
                <span className="w-full text-caption font-caption text-error-600">
                  {error}
                </span>
              )}
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  onClick={() => setOpen(false)}
                  disabled={deleting}
                >
                  Cancel
                </Button>
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="destructive-primary"
                  onClick={onConfirmDelete}
                  loading={deleting}
                >
                  Delete
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default DeleteProductModal;
