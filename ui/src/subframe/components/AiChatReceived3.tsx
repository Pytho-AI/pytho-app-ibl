"use client";
/*
 * Documentation:
 * AI Chat Received3 — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Received3_e0d8e5b9-56fa-4e5d-b0c8-37dbebfaa242
 * Tooltip — https://app.subframe.com/0ae0a78e3a6e/library?component=Tooltip_ccebd1e9-f6ac-4737-8376-0dfacd90c9f3
 * Icon Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Icon+Button_af9405b1-8c54-4e01-9786-5aad308224f6
 * AI Chat Toolbar — https://app.subframe.com/0ae0a78e3a6e/library?component=AI+Chat+Toolbar_e3cd651a-0a0f-441e-8ea8-b0fe066515e7
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { Tooltip } from "./Tooltip";
import { IconButton } from "./IconButton";
import { AiChatToolbar } from "./AiChatToolbar";

interface AiChatReceived3RootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  className?: string;
}

const AiChatReceived3Root = React.forwardRef<
  HTMLElement,
  AiChatReceived3RootProps
>(function AiChatReceived3Root(
  { children, className, ...otherProps }: AiChatReceived3RootProps,
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
      <img className="h-6 w-6 flex-none rounded-md border border-solid border-neutral-border object-cover [clip-path:circle()]" />
      <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
        {children ? (
          <div className="flex w-full flex-col items-start pt-1.5">
            {children}
          </div>
        ) : null}
        <AiChatToolbar>
          <SubframeCore.Tooltip.Provider>
            <SubframeCore.Tooltip.Root>
              <SubframeCore.Tooltip.Trigger asChild={true}>
                <IconButton size="small" icon="FeatherCopy" />
              </SubframeCore.Tooltip.Trigger>
              <SubframeCore.Tooltip.Portal>
                <SubframeCore.Tooltip.Content
                  side="bottom"
                  align="center"
                  sideOffset={4}
                  asChild={true}
                >
                  <Tooltip>Good response</Tooltip>
                </SubframeCore.Tooltip.Content>
              </SubframeCore.Tooltip.Portal>
            </SubframeCore.Tooltip.Root>
          </SubframeCore.Tooltip.Provider>
          <SubframeCore.Tooltip.Provider>
            <SubframeCore.Tooltip.Root>
              <SubframeCore.Tooltip.Trigger asChild={true}>
                <IconButton size="small" icon="FeatherDownload" />
              </SubframeCore.Tooltip.Trigger>
              <SubframeCore.Tooltip.Portal>
                <SubframeCore.Tooltip.Content
                  side="bottom"
                  align="center"
                  sideOffset={4}
                  asChild={true}
                >
                  <Tooltip>Copy</Tooltip>
                </SubframeCore.Tooltip.Content>
              </SubframeCore.Tooltip.Portal>
            </SubframeCore.Tooltip.Root>
          </SubframeCore.Tooltip.Provider>
        </AiChatToolbar>
      </div>
    </div>
  );
});

export const AiChatReceived3 = AiChatReceived3Root;
