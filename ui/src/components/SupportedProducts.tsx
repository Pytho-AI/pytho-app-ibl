"use client";

import React, { useState } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { Button } from "../subframe/components/Button";
import { Accordion  } from "../subframe/components/Accordion";
import Feedback from "./Feedback";

function SupportedProducts({ open, setOpen }) {
  const [openFeedback, setOpenFeedback] = useState(false)

  return (
    <>
      <Feedback open={ openFeedback } setOpen={ setOpenFeedback } message={ "Tell us more about the products you are interested in." }  />
      <DialogLayout open={open} onOpenChange={() => {}}>
        <div className="container max-w-none flex grow shrink-0 basis-0 flex-col items-center gap-2 self-stretch bg-default-background p-0">
          <div className="flex w-full flex-col items-start gap-4 rounded-md  bg-default-background shadow-sm">
            <Accordion
              autoFocus
              trigger={
                <div className="flex w-full items-center gap-2 px-2 py-4">
                  <span className="grow shrink-0 basis-0 text-heading-2 font-heading-2 text-default-font text-center">
                    Supported Products
                  </span>
                </div>
              }
              defaultOpen={true}
            >
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 border-t border-solid border-neutral-border px-6 py-6">
                <span className="w-full whitespace-pre-wrap text-body font-body text-default-font">
                  {
                    "Pytho can create the products below. Please reach out with other product types that you want to create!\n\n- Facebook Posts\n- Tweets\n- F-77 Reports\n- News articles\n- Media reports\n- Timelines\n"
                  }
                </span>
                <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-200" />
                <div className="flex w-full items-center justify-center gap-2 py-2">
                  <Button
                    autoFocus={false}
                    variant="neutral-secondary"
                    onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                      setOpen(false)
                      setOpenFeedback(true)
                    }}
                  >
                    Request Products
                  </Button>
                  <Button
                    onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                      setOpen(false)
                    }}
                  >
                    Close
                  </Button>
                </div>
              </div>
            </Accordion>
          </div>
        </div>
      </DialogLayout>
    </>
  );
}

export default SupportedProducts;