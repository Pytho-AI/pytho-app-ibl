"use client";
/*
 * Documentation:
 * Comment Bar2 — https://app.subframe.com/0ae0a78e3a6e/library?component=Comment+Bar2_e8dc7578-0f61-4a94-a386-ac9308e5cb8e
 * Icon with background — https://app.subframe.com/0ae0a78e3a6e/library?component=Icon+with+background_c5d68c0e-4c0c-4cff-8d8c-6ff334859b3a
 * Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Button_3b777358-b86b-40af-9327-891efc6826fe
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { IconWithBackground } from "./IconWithBackground";
import { Button } from "./Button";

interface CommentBar2RootProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "placeholder"> {
  placeholder?: React.ReactNode;
  className?: string;
}

const CommentBar2Root = React.forwardRef<HTMLElement, CommentBar2RootProps>(
  function CommentBar2Root(
    { placeholder, className, ...otherProps }: CommentBar2RootProps,
    ref
  ) {
    return (
      <div
        className={SubframeCore.twClassNames(
          "flex w-full items-center gap-2 border-t border-solid border-neutral-border py-4",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <IconWithBackground
          variant="neutral"
          size="medium"
          icon="FeatherUser"
        />
        {placeholder ? (
          <span className="grow shrink-0 basis-0 text-body font-body text-subtext-color">
            {placeholder}
          </span>
        ) : null}
        <Button variant="brand-secondary">Comment</Button>
      </div>
    );
  }
);

export const CommentBar2 = CommentBar2Root;
