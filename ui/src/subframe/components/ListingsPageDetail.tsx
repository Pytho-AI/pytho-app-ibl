"use client";
/*
 * Documentation:
 * Listings Page Detail — https://app.subframe.com/0ae0a78e3a6e/library?component=Listings+Page+Detail_023f7e53-1ebf-4595-bcf7-9d9279e314b0
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface ListingsPageDetailRootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  icon?: SubframeCore.IconName;
  label?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}

const ListingsPageDetailRoot = React.forwardRef<
  HTMLElement,
  ListingsPageDetailRootProps
>(function ListingsPageDetailRoot(
  {
    icon = "FeatherMapPin",
    label,
    children,
    className,
    ...otherProps
  }: ListingsPageDetailRootProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "flex w-56 items-start gap-4",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <SubframeCore.Icon
        className="text-heading-2 font-heading-2 text-default-font"
        name={icon}
      />
      <div className="flex flex-col items-start gap-1 pt-0.5">
        {label ? (
          <span className="w-full text-body-bold font-body-bold text-default-font">
            {label}
          </span>
        ) : null}
        {children ? (
          <div className="flex w-full flex-col items-start gap-2">
            {children}
          </div>
        ) : null}
      </div>
    </div>
  );
});

export const ListingsPageDetail = ListingsPageDetailRoot;
