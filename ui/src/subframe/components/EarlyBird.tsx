"use client";
/*
 * Documentation:
 * Early Bird — https://app.subframe.com/0ae0a78e3a6e/library?component=Early+Bird_5497ea9c-a0b5-4788-b859-f4bfbe209e8e
 * Chat Start Card — https://app.subframe.com/0ae0a78e3a6e/library?component=Chat+Start+Card_8d2ec65f-eec0-403c-ac81-6084e3044a9b
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { ChatStartCard } from "./ChatStartCard";

interface EarlyBirdRootProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
}

const EarlyBirdRoot = React.forwardRef<HTMLElement, EarlyBirdRootProps>(
  function EarlyBirdRoot(
    { className, ...otherProps }: EarlyBirdRootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "flex h-full w-full flex-col items-center justify-center gap-4 bg-default-background px-6 py-6",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <div className="flex flex-wrap items-center justify-center gap-4">
          <ChatStartCard
            className="h-auto w-40 flex-none"
            icon="FeatherImage"
            text="Create an image for my slide deck"
          />
          <ChatStartCard
            className="h-auto w-40 flex-none"
            icon="FeatherPenLine"
            text="Thank my interviewer"
            variant="purple"
          />
          <ChatStartCard
            icon="FeatherPlaneTakeoff"
            text="Plan a relaxing day"
            variant="yellow"
          />
          <ChatStartCard
            className="h-auto w-40 flex-none"
            icon="FeatherGraduationCap"
            text="Explain nostalgia like I'm 5"
            variant="green"
          />
        </div>
      </div>
    );
  }
);

export const EarlyBird = EarlyBirdRoot;
