"use client";
/*
 * Documentation:
 * Sidebar with minimal text sections — Minimal sidebar design with text-based navigation
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import * as SubframeUtils from "../utils";

interface NavItemProps extends React.HTMLAttributes<HTMLDivElement> {
  icon?: SubframeCore.IconName;
  children?: React.ReactNode;
  selected?: boolean;
  className?: string;
}

const NavItem = React.forwardRef<HTMLElement, NavItemProps>(function NavItem(
  {
    icon,
    children,
    selected = false,
    className,
    ...otherProps
  }: NavItemProps,
  ref
) {
  return (
    <div
      className={SubframeUtils.twClassNames(
        "flex w-full cursor-pointer items-center gap-2 rounded-md px-3 py-2 hover:bg-neutral-50 active:bg-neutral-100",
        { "bg-neutral-100 hover:bg-neutral-100 active:bg-neutral-100": selected },
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      {icon ? (
        <SubframeCore.Icon
          className="text-body font-body text-default-font"
          name={icon}
        />
      ) : null}
      {children ? (
        <span className="grow shrink-0 basis-0 text-body-bold font-body-bold text-default-font">
          {children}
        </span>
      ) : null}
    </div>
  );
});

interface NavSectionProps extends React.HTMLAttributes<HTMLDivElement> {
  children?: React.ReactNode;
  label?: React.ReactNode;
  className?: string;
}

const NavSection = React.forwardRef<HTMLElement, NavSectionProps>(
  function NavSection(
    { children, label, className, ...otherProps }: NavSectionProps,
    ref
  ) {
    return (
      <div
        className={SubframeUtils.twClassNames(
          "flex w-full flex-col items-start gap-1 pt-6 first:pt-0",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        {label ? (
          <div className="flex w-full flex-col items-start gap-1 px-3 py-1">
            <span className="w-full text-caption-bold font-caption-bold text-subtext-color">
              {label}
            </span>
          </div>
        ) : null}
        {children ? (
          <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-1">
            {children}
          </div>
        ) : null}
      </div>
    );
  }
);

interface SidebarWithMinimalTextSectionsRootProps
  extends React.HTMLAttributes<HTMLElement> {
  header?: React.ReactNode;
  footer?: React.ReactNode;
  children?: React.ReactNode;
  className?: string;
}

const SidebarWithMinimalTextSectionsRoot = React.forwardRef<
  HTMLElement,
  SidebarWithMinimalTextSectionsRootProps
>(function SidebarWithMinimalTextSectionsRoot(
  {
    header,
    footer,
    children,
    className,
    ...otherProps
  }: SidebarWithMinimalTextSectionsRootProps,
  ref
) {
  return (
    <nav
      className={SubframeUtils.twClassNames(
        "flex h-full w-60 flex-col items-start border-r border-solid border-neutral-border bg-default-background",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      {header ? (
        <div className="flex w-full flex-col items-start gap-2 px-6 py-6">
          {header}
        </div>
      ) : null}
      {children ? (
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-start px-4 py-4 overflow-auto">
          {children}
        </div>
      ) : null}
      {footer ? (
        <div className="flex w-full items-center gap-4 border-t border-solid border-neutral-border px-6 py-6">
          {footer}
        </div>
      ) : null}
    </nav>
  );
});

export const SidebarWithMinimalTextSections = Object.assign(
  SidebarWithMinimalTextSectionsRoot,
  {
    NavItem,
    NavSection,
  }
);
