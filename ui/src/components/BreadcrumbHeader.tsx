"use client";

import React from "react";
import { Breadcrumbs } from "../subframe/components/Breadcrumbs";

interface BreadcrumbItem {
  label: string;
  onClick?: () => void;
}

interface BreadcrumbHeaderProps {
  items: BreadcrumbItem[];
}

export default function BreadcrumbHeader({ items }: BreadcrumbHeaderProps) {
  if (!items || items.length === 0) {
    return null;
  }

  return (
    <div className="flex w-full items-center gap-2 border-b border-solid border-neutral-border px-8 py-2">
      <div className="flex grow shrink-0 basis-0 items-center gap-2">
        <Breadcrumbs>
          {items.map((item, index) => {
            const isLast = index === items.length - 1;
            return (
              <React.Fragment key={index}>
                <Breadcrumbs.Item
                  active={isLast}
                  onClick={item.onClick}
                  className={item.onClick ? "cursor-pointer" : ""}
                >
                  {item.label}
                </Breadcrumbs.Item>
                {!isLast && <Breadcrumbs.Divider />}
              </React.Fragment>
            );
          })}
        </Breadcrumbs>
      </div>
    </div>
  );
}
