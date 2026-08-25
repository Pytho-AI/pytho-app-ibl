"use client";
/*
 * Documentation:
 * Modern footer2 — https://app.subframe.com/0ae0a78e3a6e/library?component=Modern+footer2_b593a20c-6751-4686-a3de-3db74dbd1056
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { useTheme } from "../../lib/themeLib";

interface ModernFooter2RootProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
}

const ModernFooter2Root = React.forwardRef<HTMLElement, ModernFooter2RootProps>(
  function ModernFooter2Root(
    { className, ...otherProps }: ModernFooter2RootProps,
    ref
  ) {
    const { theme } = useTheme();
    const logoSrc = theme === 'light' ? '/ggvf8l4lhv21z3kgovuv-light.png' : '/ggvf8l4lhv21z3kgovuv.png';
    return (
      <div
        className={SubframeCore.twClassNames(
          "flex w-full flex-col items-start justify-center gap-6 border-t border-solid border-neutral-100 px-6 py-12",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        <span className="font-['Inter'] text-[14px] font-[500] leading-[20px] text-default-font -tracking-[0.01em]">
          Contact Us
        </span>
        <img
          className="h-20 w-64 flex-none object-cover"
          src={logoSrc}
        />
      </div>
    );
  }
);

export const ModernFooter2 = ModernFooter2Root;
