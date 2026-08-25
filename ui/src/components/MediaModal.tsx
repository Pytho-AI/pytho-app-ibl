"use client";

import { useState } from "react";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { RadioCardGroup } from "../subframe/components/RadioCardGroup";
import { DialogLayout } from "../subframe/layouts/DialogLayout";

interface MediaModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  productType: string;
  onConfirm: (postCount: number) => void;
  onCancel: () => void;
}

function MediaModal({ open, setOpen, productType, onConfirm, onCancel}: MediaModalProps) {
  const [selectedPostCount, setSelectedPostCount] = useState<string>("1");

  const getPostCount = (value: string): number => {
    const counts: { [key: string]: number } = {
      "1": 1,
      "3": 3,
      "5": 5,
      "10": 10
    };
    return counts[value] || 1;
  };

  const handleConfirm = () => {
    const postCount = getPostCount(selectedPostCount);
    onConfirm(postCount);
    setSelectedPostCount("1"); // Reset for next time
  };

  const handleCancel = () => {
    setSelectedPostCount("1"); // Reset
    onCancel();
  };

  return (
    <DialogLayout open={open} onOpenChange={setOpen}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Create {productType === "Tweet" ? "Tweets" : "Posts"}
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={handleCancel}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  How many {productType}?
                </span>
                <span className="text-body font-body text-default-font">
                  Select the number of {productType == "Tweet" ? "tweets" : "posts"} you'd like to create.
                </span>
              </div>
              <div className="flex w-full flex-col items-start gap-6">
                <RadioCardGroup
                  className="h-auto w-full flex-none"
                  value={selectedPostCount}
                  onValueChange={(value: string) => setSelectedPostCount(value)}
                >
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
                    <RadioCardGroup.RadioCard value="1">
                      <div className="flex flex-col items-start pr-2">
                        <span className="w-full text-body-bold font-body-bold text-default-font">
                          1 {productType == "Tweet" ? "tweet" : "post"}
                        </span>
                      </div>
                    </RadioCardGroup.RadioCard>
                    <RadioCardGroup.RadioCard value="3">
                      <div className="flex flex-col items-start pr-2">
                        <span className="w-full text-body-bold font-body-bold text-default-font">
                          3 {productType == "Tweet" ? "tweets" : "posts"}
                        </span>
                      </div>
                    </RadioCardGroup.RadioCard>
                    <RadioCardGroup.RadioCard value="5">
                      <div className="flex flex-col items-start pr-2">
                        <span className="w-full text-body-bold font-body-bold text-default-font">
                          5 {productType == "Tweet" ? "tweets" : "posts"}
                        </span>
                      </div>
                    </RadioCardGroup.RadioCard>
                    <RadioCardGroup.RadioCard value="10">
                      <div className="flex flex-col items-start pr-2">
                        <span className="w-full text-body-bold font-body-bold text-default-font">
                          10 {productType == "Tweet" ? "tweets" : "posts"}
                        </span>
                      </div>
                    </RadioCardGroup.RadioCard>
                  </div>
                </RadioCardGroup>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  onClick={handleCancel}
                >
                  Cancel
                </Button>
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  onClick={handleConfirm}
                >
                  Create {productType === "Tweet" ? "Tweets" : "Posts"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default MediaModal;