"use client";
/*
 * Documentation:
 * AI Chat Received2 — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Received2_55cd583f-d87e-47ed-b464-ee0bec451ef8
 * AI Chat Toolbar — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Toolbar_e3cd651a-0a0f-441e-8ea8-b0fe066515e7
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { AiChatToolbar } from "./AiChatToolbar";

interface AiChatReceived2RootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  className?: string;
}

const AiChatReceived2Root = React.forwardRef<
  HTMLElement,
  AiChatReceived2RootProps
>(function AiChatReceived2Root(
  { children, className, ...otherProps }: AiChatReceived2RootProps,
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
      <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
        {children ? (
          <div className="flex w-full flex-col items-start pt-1.5">
            {children}
          </div>
        ) : null}
        <AiChatToolbar />
      </div>
    </div>
  );
});

export const AiChatReceived2 = AiChatReceived2Root;
