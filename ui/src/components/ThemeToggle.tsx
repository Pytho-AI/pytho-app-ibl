"use client";

import * as React from "react";
import { useTheme } from "../lib/themeLib";
import { IconButton } from "../subframe/components/IconButton";

interface ThemeToggleProps {
  className?: string;
}

export function ThemeToggle({ className }: ThemeToggleProps) {
  const { theme, toggleTheme } = useTheme();

  return (
    <IconButton
      className={className}
      icon={theme === 'light' ? "FeatherMoon" : "FeatherSun"}
      onClick={toggleTheme}
      size="medium"
      variant="neutral-tertiary"
    />
  );
}

export default ThemeToggle;
