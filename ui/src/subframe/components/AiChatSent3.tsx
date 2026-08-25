"use client";
/*
 * Documentation:
 * AI Chat Sent3 — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Sent3_7cacf142-7779-4d3c-aa2f-79a11485d511
 * Avatar — https://app.subframe.com/0ae0a78e3a6e/library?component=Avatar_bec25ae6-5010-4485-b46b-cf79e3943ab2
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { Avatar } from "./Avatar";

interface AiChatSent3RootProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  avatar?: string;
  className?: string;
}

const AiChatSent3Root = React.forwardRef<HTMLElement, AiChatSent3RootProps>(
  function AiChatSent3Root(
    { children, avatar, className, ...otherProps }: AiChatSent3RootProps,
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
        <Avatar image={avatar}>MF</Avatar>
        {children ? (
          <div className="flex grow shrink-0 basis-0 items-center gap-4 pt-1.5">
            {children}
          </div>
        ) : null}
      </div>
    );
  }
);

export const AiChatSent3 = AiChatSent3Root;
