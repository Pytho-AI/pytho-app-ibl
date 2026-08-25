"use client";
/*
 * Documentation:
 * Bold footer — https://app.subframe.com/0ae0a78e3a6e/library?component=Bold+footer_9be52776-82ea-4a81-a6ab-8dc900bd6b8b
 * Social Feed Card Small — https://app.subframe.com/0ae0a78e3a6e/library?component=Social+Feed+Card+Small_7261bfa3-f0b9-4a55-8e3e-7a4b484711ca
 * Link Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Link+Button_a4ee726a-774c-4091-8c49-55b659356024
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { SocialFeedCardSmall } from "./SocialFeedCardSmall";
import { LinkButton } from "./LinkButton";

interface BoldFooterRootProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
}

const BoldFooterRoot = React.forwardRef<HTMLElement, BoldFooterRootProps>(
  function BoldFooterRoot(
    { className, ...otherProps }: BoldFooterRootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "flex w-full flex-col items-start gap-2",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <div className="flex w-full flex-col items-center justify-center gap-6 border-t border-solid border-neutral-100 px-6 py-24">
          <SocialFeedCardSmall
            className="h-auto w-auto flex-none self-start"
            title="Pytho AI, Inc. "
            caption="San Francisco, CA"
          />
          <div className="flex w-full flex-wrap items-center justify-center gap-6">
            <div className="flex min-w-[144px] grow shrink-0 basis-0 flex-col items-center gap-4">
              <LinkButton>Careers</LinkButton>
            </div>
            <div className="flex min-w-[144px] grow shrink-0 basis-0 flex-col items-center gap-4">
              <LinkButton>News</LinkButton>
            </div>
            <div className="flex min-w-[144px] grow shrink-0 basis-0 flex-col items-center gap-4">
              <LinkButton>Cookie Policy</LinkButton>
            </div>
          </div>
          <div className="flex w-full max-w-[1280px] flex-col items-center gap-12">
            <div className="flex w-full max-w-[768px] flex-col items-center gap-4">
              <span className="font-['Montserrat'] text-[14px] font-[500] leading-[20px] text-subtext-color text-center">
                © Pytho 2024
              </span>
            </div>
          </div>
        </div>
      </div>
    );
  }
);

export const BoldFooter = BoldFooterRoot;
