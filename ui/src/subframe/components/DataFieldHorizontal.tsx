"use client";
/*
 * Documentation:
 * Data Field Horizontal — https://app.subframe.com/0ae0a78e3a6e/library?component=Data+Field+Horizontal_9873e9ab-e456-4834-a93a-1eaa4c1ee170
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import * as SubframeUtils from "../utils";

interface DataFieldHorizontalRootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  icon?: SubframeCore.IconName;
  label?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}

const DataFieldHorizontalRoot = React.forwardRef<
  HTMLDivElement,
  DataFieldHorizontalRootProps
>(function DataFieldHorizontalRoot(
  {
    icon = "FeatherBadgeInfo",
    label,
    children,
    className,
    ...otherProps
  }: DataFieldHorizontalRootProps,
  ref
) {
  return (
    <div
      className={SubframeUtils.twClassNames(
        "flex w-full items-center gap-2",
        className
      )}
      ref={ref}
      {...otherProps}
    >
      <div className="flex w-32 flex-none items-center gap-2">
        <SubframeCore.Icon
          className="text-body font-body text-subtext-color"
          name={icon}
        />
        {label ? (
          <span className="grow shrink-0 basis-0 text-body font-body text-subtext-color">
            {label}
          </span>
        ) : null}
      </div>
      {children ? (
        <div className="flex min-h-[32px] grow shrink-0 basis-0 items-center gap-2">
          {children}
        </div>
      ) : null}
    </div>
  );
});

export const DataFieldHorizontal = DataFieldHorizontalRoot;
