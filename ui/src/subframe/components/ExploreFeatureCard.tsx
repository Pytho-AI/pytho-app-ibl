"use client";
/*
 * Documentation:
 * Explore Feature Card — https://app.subframe.com/0ae0a78e3a6e/library?component=Explore+Feature+Card_976ac54f-5cc3-4e1c-ba2c-2b323bcaf846
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface ExploreFeatureCardRootProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  image?: string;
  title?: React.ReactNode;
  desc?: React.ReactNode;
  metadata?: React.ReactNode;
  className?: string;
}

const ExploreFeatureCardRoot = React.forwardRef<
  HTMLElement,
  ExploreFeatureCardRootProps
>(function ExploreFeatureCardRoot(
  {
    image,
    title,
    desc,
    metadata,
    className,
    ...otherProps
  }: ExploreFeatureCardRootProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "group/976ac54f flex h-full w-full cursor-pointer items-center gap-4 rounded-md bg-neutral-50 px-6 py-6 hover:bg-neutral-100",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <div className="flex h-20 w-20 flex-none items-center justify-center px-4 py-4">
        {image ? (
          <img
            className="grow shrink-0 basis-0 self-stretch object-cover"
            src={image}
          />
        ) : null}
      </div>
      <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4">
        <div className="flex w-full flex-col items-start gap-1">
          {title ? (
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              {title}
            </span>
          ) : null}
          {desc ? (
            <span className="line-clamp-2 w-full text-caption font-caption text-default-font">
              {desc}
            </span>
          ) : null}
        </div>
        {metadata ? (
          <span className="w-full text-caption font-caption text-subtext-color">
            {metadata}
          </span>
        ) : null}
      </div>
    </div>
  );
});

export const ExploreFeatureCard = ExploreFeatureCardRoot;
