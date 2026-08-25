"use client";
/*
 * Documentation:
 * Avatar2 — https://app.subframe.com/0ae0a78e3a6e/library?component=Avatar2_d7dfea6a-86ca-4c9d-83f5-88b52a028668
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface Avatar2RootProps extends React.HTMLAttributes<HTMLDivElement> {
  variant?: "brand" | "neutral" | "error" | "success" | "warning";
  size?: "x-large" | "large" | "medium" | "small" | "x-small";
  children?: React.ReactNode;
  image?: string;
  square?: boolean;
  className?: string;
}

const Avatar2Root = React.forwardRef<HTMLElement, Avatar2RootProps>(
  function Avatar2Root(
    {
      variant = "brand",
      size = "medium",
      children,
      image,
      square = false,
      className,
      ...otherProps
    }: Avatar2RootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "group/d7dfea6a flex h-8 w-8 flex-col items-center justify-center gap-2 overflow-hidden rounded-full bg-brand-100 relative",
          {
            "rounded-md": square,
            "h-5 w-5": size === "x-small",
            "h-6 w-6": size === "small",
            "h-12 w-12": size === "large",
            "h-16 w-16": size === "x-large",
            "bg-warning-100": variant === "warning",
            "bg-success-100": variant === "success",
            "bg-error-100": variant === "error",
            "bg-neutral-100": variant === "neutral",
          },
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        {children ? (
          <span
            className={SubframeCore.twClassNames(
              "line-clamp-1 w-full font-['Inter'] text-[14px] font-[500] leading-[14px] text-brand-800 text-center absolute",
              {
                "font-['Inter'] text-[10px] font-[500] leading-[10px] tracking-normal":
                  size === "x-small" || size === "small",
                "font-['Inter'] text-[18px] font-[500] leading-[18px] tracking-normal":
                  size === "large",
                "font-['Inter'] text-[24px] font-[500] leading-[24px] tracking-normal":
                  size === "x-large",
                "text-warning-800": variant === "warning",
                "text-success-800": variant === "success",
                "text-error-800": variant === "error",
                "text-neutral-800": variant === "neutral",
              }
            )}
          >
            {children}
          </span>
        ) : null}
        {image ? (
          <img
            className={SubframeCore.twClassNames(
              "h-8 w-8 flex-none object-cover absolute",
              {
                "h-5 w-5 flex-none": size === "x-small",
                "h-6 w-6 flex-none": size === "small",
                "h-12 w-12 flex-none": size === "large",
                "h-16 w-16 flex-none": size === "x-large",
              }
            )}
            src={image}
          />
        ) : null}
      </div>
    );
  }
);

export const Avatar2 = Avatar2Root;
