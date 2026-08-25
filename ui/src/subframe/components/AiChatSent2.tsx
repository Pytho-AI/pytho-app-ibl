"use client";
/*
 * Documentation:
 * AI Chat Sent2 — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Sent2_52968b77-ae95-4216-83ad-99bc949b765d
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface AiChatSent2RootProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  avatar?: string;
  className?: string;
}

const AiChatSent2Root = React.forwardRef<HTMLElement, AiChatSent2RootProps>(
  function AiChatSent2Root(
    { children, avatar, className, ...otherProps }: AiChatSent2RootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "flex w-full items-start gap-4 py-2",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <img className="h-8 w-7 flex-none object-cover [clip-path:circle()]" />
        {children ? (
          <div className="flex grow shrink-0 basis-0 items-center gap-4 pt-1.5">
            {children}
          </div>
        ) : null}
      </div>
    );
  }
);

export const AiChatSent2 = AiChatSent2Root;
