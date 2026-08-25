"use client";
/*
 * Documentation:
 * AI Chat Suggestion — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Suggestion_b66d445b-b9c7-4cf6-82e2-4f1130e68f92
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface AiChatSuggestionRootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  text?: string;
  className?: string;
}

const AiChatSuggestionRoot = React.forwardRef<
  HTMLElement,
  AiChatSuggestionRootProps
>(function AiChatSuggestionRoot(
  { text, className, ...otherProps }: AiChatSuggestionRootProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "group/b66d445b flex cursor-pointer items-center gap-2 rounded-md bg-neutral-50 px-2 py-2 hover:bg-neutral-100",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <SubframeCore.Icon
        className="text-body font-body text-default-font"
        name="FeatherSparkles"
      />
      {text ? (
        <span className="text-caption-bold font-caption-bold text-default-font">
          {text}
        </span>
      ) : null}
    </div>
  );
});

export const AiChatSuggestion = AiChatSuggestionRoot;
