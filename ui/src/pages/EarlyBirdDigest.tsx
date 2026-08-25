"use client";

import * as SubframeCore from "@subframe/core";
import { DefaultPageLayout } from "../subframe";
import { useAppContext } from "../lib/contextLib";

function EarlyBirdDigest() {
  const { user } = useAppContext()

  return (
    <DefaultPageLayout>
      <div className="flex grow shrink-0 basis-0 flex-col items-center bg-default-background w-full">
        <div className="flex w-full flex-col items-center justify-center gap-24 px-6 pt-48 pb-7">
          <span className="max-w-[576px] whitespace-pre-wrap font-['Inter'] text-[56px] font-[600] leading-[62px] text-subtext-color text-center -tracking-[0.04em]">
            Welcome {user.firstName}
          </span>
          <span className="w-full min-w-[320px] whitespace-pre-wrap font-['Inter'] text-[32px] font-[400] leading-[44px] text-subtext-color text-center -tracking-[0.03em]">
            {
              `Today is ${new Date().toLocaleDateString('en-US', { 
                weekday: 'long',
                year: 'numeric', 
                month: 'long', 
                day: 'numeric' 
                })} \n\nHere is your `
            } <span className="underline underline-offset-8 cursor-pointer"
                onClick={() => {
                  window.open("https://www.defensenews.com/ebb/", '_blank')
                }}
              >Early Bird</span> summary
          </span>
        </div>
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-center justify-center gap-6 bg-default-background px-6 pt-6 pb-24">
          <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-200" />
          <div className="flex w-full max-w-[1024px] flex-wrap items-start gap-12">
            <span className="px-10 w-full min-w-[320px] font-['Inter'] text-[17px] font-[400] leading-[28px] text-default-font -tracking-[0.03em]"
              dangerouslySetInnerHTML={{ __html: user.summary.ebb}}>
            </span>
          </div>
        </div>
      </div>
      {/* <div className="flex grow shrink-0 basis-0 flex-col items-center self-stretch bg-default-background">
        <div className="flex w-full flex-col items-center justify-center gap-24 px-6 py-24">
          <span className="max-w-[576px] whitespace-pre-wrap font-['Inter'] text-[56px] font-[600] leading-[62px] text-default-font text-center -tracking-[0.04em]">
            Welcome {user.firstName}
          </span>
        </div>
        <div className="flex w-full flex-col items-center justify-center gap-6 bg-default-background px-6 py-24 from-transparent via-neutral-700 to-neutral-900">
          <div className="flex w-full max-w-[1024px] flex-wrap items-start gap-12">
            <span className="min-w-[320px] grow shrink-0 basis-0 whitespace-pre-wrap font-['Inter'] text-[40px] font-[600] leading-[44px] text-default-font -tracking-[0.03em]">
              {`Today is ${new Date().toLocaleDateString('en-US', { 
                weekday: 'long',
                year: 'numeric', 
                month: 'long', 
                day: 'numeric' 
                })}. \n\nHere is a summary of the Early Bird DIgest.`}
              
            </span>
            <span className="border-1 min-w-[320px] grow shrink-0 basis-0 font-['Inter'] text-[17px] font-[400] leading-[24px] text-default-font -tracking-[0.01em]"
              dangerouslySetInnerHTML={{ __html: user.summary.ebb}}
            >
              
            </span>
          </div>
        </div>
      </div> */}
    </DefaultPageLayout>

  );
}

export default EarlyBirdDigest;