"use client";
/*
 * Documentation:
 * Text Field2 — https://app.subframe.com/0ae0a78e3a6e/library?component=Text+Field2_b3431891-7052-47cd-95ad-518718d791d7
 */

import React from "react";
import * as SubframeCore from "@subframe/core";

interface InputProps
  extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "placeholder"> {
  placeholder?: React.ReactNode;
  value?: string;
  onChange?: (event: React.ChangeEvent<HTMLInputElement>) => void;
  className?: string;
}

const Input = React.forwardRef<HTMLElement, InputProps>(function Input(
  { placeholder, className, ...otherProps }: InputProps,
  ref
) {
  return (
    <input
      className={SubframeCore.twClassNames(
        "h-full w-full border-none bg-transparent text-body font-body text-default-font outline-none placeholder:text-neutral-400",
        className
      )}
      placeholder={placeholder as string}
      ref={ref as any}
      {...otherProps}
    />
  );
});

interface TextField2RootProps
  extends React.LabelHTMLAttributes<HTMLLabelElement> {
  disabled?: boolean;
  error?: boolean;
  variant?: "outline" | "filled";
  label?: React.ReactNode;
  helpText?: React.ReactNode;
  icon?: SubframeCore.IconName;
  iconRight?: SubframeCore.IconName;
  children?: React.ReactNode;
  className?: string;
}

const TextField2Root = React.forwardRef<HTMLElement, TextField2RootProps>(
  function TextField2Root(
    {
      disabled = false,
      error = false,
      variant = "outline",
      label,
      helpText,
      icon = null,
      iconRight = null,
      children,
      className,
      ...otherProps
    }: TextField2RootProps,
    ref
  ) {
    return (
      <label
        className={SubframeCore.twClassNames(
          "group/b3431891 flex flex-col items-start gap-1",
          className
        )}
        ref={ref as any}
        {...otherProps}
      >
        {label ? (
          <span className="text-caption-bold font-caption-bold text-default-font">
            {label}
          </span>
        ) : null}
        <div
          className={SubframeCore.twClassNames(
            "flex h-8 w-full flex-none items-center gap-1 rounded-md border border-solid border-neutral-border bg-default-background px-2 group-focus-within/b3431891:border group-focus-within/b3431891:border-solid group-focus-within/b3431891:border-brand-primary",
            {
              "border border-solid border-neutral-100 bg-neutral-100 group-hover/b3431891:border group-hover/b3431891:border-solid group-hover/b3431891:border-neutral-border group-focus-within/b3431891:bg-default-background":
                variant === "filled",
              "border border-solid border-error-600": error,
              "border border-solid border-neutral-200 bg-neutral-200": disabled,
            }
          )}
        >
          <SubframeCore.Icon
            className="text-body font-body text-subtext-color"
            name={icon}
          />
          {children ? (
            <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch px-1">
              {children}
            </div>
          ) : null}
          <SubframeCore.Icon
            className={SubframeCore.twClassNames(
              "text-body font-body text-subtext-color",
              { "text-error-500": error }
            )}
            name={iconRight}
          />
        </div>
        {helpText ? (
          <span
            className={SubframeCore.twClassNames(
              "text-caption font-caption text-subtext-color",
              { "text-error-700": error }
            )}
          >
            {helpText}
          </span>
        ) : null}
      </label>
    );
  }
);

export const TextField2 = Object.assign(TextField2Root, {
  Input,
});
