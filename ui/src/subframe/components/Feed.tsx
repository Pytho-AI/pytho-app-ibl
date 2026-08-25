"use client";
/*
 * Documentation:
 * Feed — https://app.subframe.com/0ae0a78e3a6e/library?component=Feed_7d554e3e-c194-4acf-8d18-5fba3a57892f
 */

import React from "react";
import * as SubframeUtils from "../utils";

interface ItemProps extends React.HTMLAttributes<HTMLDivElement> {
  comment?: React.ReactNode;
  timestamp?: React.ReactNode;
  isLast?: boolean;
  children?: React.ReactNode;
  className?: string;
}

const Item = React.forwardRef<HTMLDivElement, ItemProps>(function Item(
  {
    comment,
    timestamp,
    isLast = false,
    children,
    className,
    ...otherProps
  }: ItemProps,
  ref
) {
  return (
    <div
      className={SubframeUtils.twClassNames(
        "group/c6162651 flex h-full w-full items-start gap-4",
        className
      )}
      ref={ref}
      {...otherProps}
    >
      <div
        className={SubframeUtils.twClassNames(
          "flex grow shrink-0 basis-0 flex-col items-end gap-2 pt-1.5 pb-6",
          { "px-0 py-1": isLast }
        )}
      >
        <div className="flex w-full flex-wrap items-center gap-2">
          {children ? (
            <div className="flex grow shrink-0 basis-0 flex-wrap items-start gap-1">
              {children}
            </div>
          ) : null}
          {timestamp ? (
            <span className="text-caption font-caption text-subtext-color">
              {timestamp}
            </span>
          ) : null}
        </div>
        {comment ? (
          <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-2">
            {comment}
          </div>
        ) : null}
      </div>
    </div>
  );
});

interface CommentProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  className?: string;
}

const Comment = React.forwardRef<HTMLDivElement, CommentProps>(function Comment(
  { children, className, ...otherProps }: CommentProps,
  ref
) {
  return (
    <div
      className={SubframeUtils.twClassNames(
        "flex max-w-[576px] flex-col items-start gap-2 rounded-md bg-neutral-50 px-4 py-2",
        className
      )}
      ref={ref}
      {...otherProps}
    >
      {children ? (
        <span className="whitespace-pre-wrap text-body font-body text-default-font">
          {children}
        </span>
      ) : null}
    </div>
  );
});

interface FeedRootProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  className?: string;
}

const FeedRoot = React.forwardRef<HTMLDivElement, FeedRootProps>(
  function FeedRoot(
    { children, className, ...otherProps }: FeedRootProps,
    ref
  ) {
    return children ? (
      <div
        className={SubframeUtils.twClassNames(
          "flex w-full flex-col items-start",
          className
        )}
        ref={ref}
        {...otherProps}
      >
        {children}
      </div>
    ) : null;
  }
);

export const Feed = Object.assign(FeedRoot, {
  Item,
  Comment,
});
