"use client";
/*
 * Documentation:
 * Chat Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Chat+Button_caa85658-e634-4442-893c-fae09631ebfd
 * Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Button_3b777358-b86b-40af-9327-891efc6826fe
 * Switch — https://app.subframe.com/0ae0a78e3a6e/library?component=Switch_7a464794-9ea9-4040-b1de-5bfb2ce599d9
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { Button } from "./Button";
import { Switch } from "./Switch";

interface ItemProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title?: React.ReactNode;
  subtitle?: React.ReactNode;
  rightSlot?: React.ReactNode;
  leftSlot?: React.ReactNode;
  className?: string;
}

const Item = React.forwardRef<HTMLElement, ItemProps>(function Item(
  { title, subtitle, rightSlot, leftSlot, className, ...otherProps }: ItemProps,
  ref
) {
  return (
    <div
      className={SubframeCore.twClassNames(
        "group/363ebd26 flex w-full cursor-pointer items-center gap-4 rounded-sm px-3 py-3 hover:bg-neutral-50",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      {leftSlot ? (
        <div className="flex items-center justify-center gap-4">{leftSlot}</div>
      ) : null}
      <div className="flex grow shrink-0 basis-0 flex-col items-start justify-center">
        {title ? (
          <span className="text-body font-body text-default-font">{title}</span>
        ) : null}
        {subtitle ? (
          <span className="text-caption font-caption text-subtext-color">
            {subtitle}
          </span>
        ) : null}
      </div>
      {rightSlot ? (
        <div className="flex flex-col items-end justify-center gap-2">
          {rightSlot}
        </div>
      ) : null}
    </div>
  );
});

interface ChatButtonRootProps extends React.HTMLAttributes<HTMLDivElement> {
  text?: React.ReactNode;
  className?: string;
}

const ChatButtonRoot = React.forwardRef<HTMLElement, ChatButtonRootProps>(
  function ChatButtonRoot(
    { text, className, ...otherProps }: ChatButtonRootProps,
    ref
  ) {
    return (
      <SubframeCore.Popover.Root>
        <SubframeCore.Popover.Trigger asChild={true}>
          <div
            className={SubframeCore.twClassNames(
              "group/caa85658 flex h-10 cursor-pointer items-center justify-center gap-2 rounded-sm px-3 py-2 hover:bg-neutral-100",
              className
            )}
            ref={ref as any}
            {...otherProps}
          >
            {text ? (
              <span className="text-heading-3 font-heading-3 text-subtext-color">
                {text}
              </span>
            ) : null}
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-neutral-400"
              name="FeatherChevronDown"
            />
          </div>
        </SubframeCore.Popover.Trigger>
        <SubframeCore.Popover.Portal>
          <SubframeCore.Popover.Content
            side="bottom"
            align="start"
            sideOffset={4}
            asChild={true}
          >
            <div className="flex flex-col items-center gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-3 py-3 shadow-lg group-hover/caa85658:flex-col group-hover/caa85658:flex-nowrap group-hover/caa85658:items-start group-hover/caa85658:justify-start group-hover/caa85658:gap-2 group-hover/caa85658:px-2 group-hover/caa85658:py-2">
              <Item
                title="ChatGPT Plus"
                subtitle="Our smartest model & more"
                rightSlot={<Button variant="neutral-secondary">Upgrade</Button>}
              />
              <Item
                title="ChatGPT"
                subtitle="Great for everyday tasks"
                rightSlot={
                  <SubframeCore.Icon
                    className="text-body font-body text-default-font group-hover/caa85658:text-heading-2 group-hover/caa85658:font-heading-2"
                    name="FeatherCheckCircle2"
                  />
                }
              />
              <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-200" />
              <Item
                title="Temporary chat"
                subtitle=""
                rightSlot={<Switch />}
                leftSlot={
                  <div className="flex items-start gap-4 group-hover/caa85658:h-8 group-hover/caa85658:w-8 group-hover/caa85658:flex-none group-hover/caa85658:items-center group-hover/caa85658:justify-center">
                    <SubframeCore.Icon
                      className="text-body font-body text-default-font group-hover/caa85658:text-heading-2 group-hover/caa85658:font-heading-2 group-hover/caa85658:text-subtext-color"
                      name="FeatherMessageCircle"
                    />
                  </div>
                }
              />
            </div>
          </SubframeCore.Popover.Content>
        </SubframeCore.Popover.Portal>
      </SubframeCore.Popover.Root>
    );
  }
);

export const ChatButton = Object.assign(ChatButtonRoot, {
  Item,
});
