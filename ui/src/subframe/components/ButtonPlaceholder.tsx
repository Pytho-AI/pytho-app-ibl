"use client";
/*
 * Documentation:
 * Button Placeholder — https://app.subframe.com/0ae0a78e3a6e/library?component=Button+Placeholder_d356a0d4-ef5f-4935-b708-9627901e3617
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface ButtonPlaceholderRootProps
  extends React.HTMLAttributes<HTMLDivElement> {
  icon?: SubframeCore.IconName;
  children?: React.ReactNode;
  className?: string;
}

const ButtonPlaceholderRoot = React.forwardRef<
  HTMLElement,
  ButtonPlaceholderRootProps
>(function ButtonPlaceholderRoot(
  {
    icon = "FeatherPlus",
    children,
    className,
    ...otherProps
  }: ButtonPlaceholderRootProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "group/d356a0d4 flex w-full cursor-pointer items-center gap-2 rounded-md border border-dashed border-neutral-300 px-4 py-3 hover:bg-neutral-100",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <SubframeCore.Icon
        className="text-heading-3 font-heading-3 text-subtext-color"
        name={icon}
      />
      {children ? (
        <span className="text-body-bold font-body-bold text-subtext-color">
          {children}
        </span>
      ) : null}
    </div>
  );
});

export const ButtonPlaceholder = ButtonPlaceholderRoot;
