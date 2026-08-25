import React from "react";
import * as SubframeCore from "@subframe/core";

interface DisabledNavItemProps {
  icon: SubframeCore.IconName;
  label: string;
}

export function DisabledNavItem({ icon, label }: DisabledNavItemProps) {
  return (
    <div
      className="flex h-8 w-full items-center gap-2 rounded-md px-3 py-1 opacity-40 cursor-not-allowed"
      title={`${label} (coming soon)`}
    >
      <SubframeCore.Icon
        className="text-body font-body text-subtext-color"
        name={icon}
      />
      <span className="line-clamp-1 grow shrink-0 basis-0 text-caption-bold font-caption-bold text-subtext-color">
        {label}
      </span>
    </div>
  );
}
