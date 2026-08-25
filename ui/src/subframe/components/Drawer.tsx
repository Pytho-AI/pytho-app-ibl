"use client";
/*
 * Documentation:
 * Drawer — https://app.subframe.com/0ae0a78e3a6e/library?component=Drawer_1e71b2cb-8d72-4e67-b368-8805179e9444
 * Text Field — https://app.subframe.com/0ae0a78e3a6e/library?component=Text+Field_be48ca43-f8e7-4c0e-8870-d219ea11abfe
 * Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Button_3b777358-b86b-40af-9327-891efc6826fe
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface ContentProps
  extends React.ComponentProps<typeof SubframeCore.Drawer.Content> {
  children?: React.ReactNode;
  className?: string;
  side?: "left" | "right";
}

const Content = React.forwardRef<HTMLElement, ContentProps>(function Content(
  { children, className, side = "right", ...otherProps }: ContentProps,
  ref
) {
  return children ? (
    <SubframeCore.Drawer.Content asChild={true} {...otherProps}>
      <div
        className={SubframeCore.twClassNames(
          "flex h-full min-w-[320px] flex-col items-start gap-2 border-solid border-neutral-border bg-default-background",
          side === "left" ? "border-r" : "border-l",
          className
        )}
        ref={ref as any}
      >
        {children}
      </div>
    </SubframeCore.Drawer.Content>
  ) : null;
});

interface DrawerRootProps
  extends React.ComponentProps<typeof SubframeCore.Drawer.Root> {
  children?: React.ReactNode;
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  className?: string;
  side?: "left" | "right";
}

const DrawerRoot = React.forwardRef<HTMLElement, DrawerRootProps>(
  function DrawerRoot(
    { children, className, side = "right", ...otherProps }: DrawerRootProps,
    ref
  ) {
    return children ? (
      <SubframeCore.Drawer.Root asChild={true} {...otherProps}>
        <div
          className={SubframeCore.twClassNames(
            "flex h-full w-full flex-col justify-center gap-2 bg-[#00000066]",
            side === "left" ? "items-start" : "items-end",
            className
          )}
          ref={ref as any}
        >
          {children}
        </div>
      </SubframeCore.Drawer.Root>
    ) : null;
  }
);

export const Drawer = Object.assign(DrawerRoot, {
  Content,
});
