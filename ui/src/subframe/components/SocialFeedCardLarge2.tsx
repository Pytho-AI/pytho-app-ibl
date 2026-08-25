"use client";
/*
 * Documentation:
 * Social Feed Card Large2 — https://app.subframe.com/0ae0a78e3a6e/library?component=Social+Feed+Card+Large2_2d89f915-4ec3-49dd-a5e8-d88f5e90e6d1
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface SocialFeedCardLarge2RootProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  image?: string;
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  caption?: React.ReactNode;
  className?: string;
}

const SocialFeedCardLarge2Root = React.forwardRef<
  HTMLElement,
  SocialFeedCardLarge2RootProps
>(function SocialFeedCardLarge2Root(
  {
    image,
    title,
    subtitle,
    caption,
    className,
    ...otherProps
  }: SocialFeedCardLarge2RootProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "flex w-full cursor-pointer flex-col items-start overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <div className="flex h-48 w-full flex-none flex-col items-center justify-center gap-2 overflow-hidden border-b border-solid border-neutral-border bg-neutral-50">
        {image ? <img className="w-full flex-none" src={image} /> : null}
      </div>
      <div className="flex w-full flex-col items-start gap-1 px-4 py-4">
        {title ? (
          <span className="line-clamp-2 w-full text-body-bold font-body-bold text-default-font">
            {title}
          </span>
        ) : null}
        {subtitle ? (
          <span className="line-clamp-3 w-full text-body font-body text-subtext-color">
            {subtitle}
          </span>
        ) : null}
        {caption ? (
          <span className="line-clamp-1 w-full text-caption font-caption text-subtext-color">
            {caption}
          </span>
        ) : null}
      </div>
    </div>
  );
});

export const SocialFeedCardLarge2 = SocialFeedCardLarge2Root;
