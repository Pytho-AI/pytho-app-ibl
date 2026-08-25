"use client";
/*
 * Documentation:
 * Chat Start Card — https://app.subframe.com/0ae0a78e3a6e/library?component=Chat+Start+Card_8d2ec65f-eec0-403c-ac81-6084e3044a9b
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface ChatStartCardRootProps extends React.HTMLAttributes<HTMLDivElement> {
  icon?: SubframeCore.IconName;
  text?: React.ReactNode;
  variant?: "default" | "purple" | "yellow" | "green";
  className?: string;
}

const ChatStartCardRoot = React.forwardRef<HTMLElement, ChatStartCardRootProps>(
  function ChatStartCardRoot(
    {
      icon = "FeatherPlus",
      text,
      variant = "default",
      className,
      ...otherProps
    }: ChatStartCardRootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "group/8d2ec65f flex h-full w-40 cursor-pointer flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4 shadow-sm hover:bg-neutral-50",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <SubframeCore.Icon
          className={SubframeCore.twClassNames(
            "text-heading-3 font-heading-3 text-brand-500",
            {
              "text-success-500": variant === "green",
              "text-warning-500": variant === "yellow",
              "text-error-500": variant === "purple",
            }
          )}
          name={icon}
        />
        {text ? (
          <span className="line-clamp-2 w-full text-body font-body text-subtext-color">
            {text}
          </span>
        ) : null}
      </div>
    );
  }
);

export const ChatStartCard = ChatStartCardRoot;
