"use client";

/**
 * ProductTypeList — the left-rail product catalog for the Intelligence
 * Operations dashboard (`WhiteCellHome`). Renders the product types from
 * `/api/product_types`, grouped into colored category sections.
 *
 * Clicking a product item FILTERS the catalog table to that product type
 * (single-select; click again to clear). Category headers are visual
 * grouping only — not clickable. An overflow (⋯) menu on each item
 * exposes edit / delete / view-template for custom (analyst-uploaded)
 * types. The rail does NOT generate products — the dashboard is
 * filter/open only; generation lives in the wargaming-session modal.
 */

import React from "react";
import * as SubframeCore from "@subframe/core";
import { DropdownMenu } from "../subframe/components/DropdownMenu";

export interface ProductTypeListProps {
  productTypes: any[];
  // Clicking a row FILTERS the table to that product type. `selectedKey`
  // is the active filter (the product's template.productKey ||
  // productName) so the row highlights. The overflow callbacks let
  // analysts edit / delete / view the template for custom (non-builtin)
  // types without leaving the rail.
  onRailFilter?: (pt: any) => void;
  selectedKey?: string | null;
  onRailEdit?: (pt: any) => void;
  onRailDelete?: (pt: any) => void;
  onRailViewTemplate?: (pt: any) => void;
}

/** Filter key for a product type — matches the catalog row's
 *  `productType` (short doctrinal code). Built-ins carry it on
 *  template.productKey; custom uploads fall back to productName. */
export function railFilterKey(pt: any): string {
  return pt?.template?.productKey || pt?.productName || "";
}

/** XLSX-canonical products (output_formats === ['xlsx']) open in the
 *  spreadsheet editor and get an XLSX badge in the rail + table. */
export function isXlsxProduct(pt: any): boolean {
  const f = pt?.output_formats;
  return Array.isArray(f) && f.length === 1 && f[0] === "xlsx";
}

/** Primary deliverable format for the badge shown on every rail item +
 *  table row. XLSX-canonical → "xlsx"; otherwise the schema's singular
 *  output_format (docx → portrait page, pptx → landscape slide). Mirrors
 *  the backend's `format` field in list_autodraft_catalog. */
export function productFormat(pt: any): string {
  if (isXlsxProduct(pt)) return "xlsx";
  return pt?.output_format === "pptx" ? "pptx" : "docx";
}

function RailItem({
  product,
  selected,
  format,
  onClick,
  onEdit,
  onDelete,
  onViewTemplate,
}: {
  product: any;
  selected: boolean;
  format: string;
  onClick: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
  onViewTemplate?: () => void;
}) {
  // Rail item — clicking the row body FILTERS the catalog table to this
  // product type (single-select). The overflow (⋯) exposes per-type
  // edit/delete/view-template for custom (non-builtin) products. The
  // `selected` state highlights the active filter.
  //
  // IPOE items get a "Step N: " prefix (mirroring the ATP 2-01.3
  // four-step workflow analysts already think in). The backend
  // surfaces this as `stepLabel` on /api/product_types. Every item gets
  // a format badge (docx / pptx / xlsx) for consistency.
  const baseLabel = product.productLongName || product.productName;
  const label = product.stepLabel
    ? `${product.stepLabel}: ${baseLabel}`
    : baseLabel;
  const hasOverflowActions = !!(onEdit || onDelete || onViewTemplate);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      className={SubframeCore.twClassNames(
        "group flex w-full items-center gap-2 px-3 py-2 text-left rounded-md cursor-pointer transition-colors",
        selected
          ? "bg-brand-100 text-brand-700"
          : "text-default-font hover:bg-neutral-50",
      )}
    >
      <span className="flex-1 text-body font-body truncate">{label}</span>
      {format && (
        <span className="flex-none rounded bg-neutral-100 px-1 py-0.5 text-[10px] font-caption-bold uppercase tracking-wider text-subtext-color">
          {format}
        </span>
      )}
      {hasOverflowActions && (
        <SubframeCore.DropdownMenu.Root>
          <SubframeCore.DropdownMenu.Trigger asChild>
            <button
              type="button"
              onClick={(e: React.MouseEvent<HTMLButtonElement>) =>
                e.stopPropagation()
              }
              className="opacity-0 group-hover:opacity-100 transition-opacity flex-none flex items-center justify-center w-6 h-6 rounded text-subtext-color hover:bg-neutral-100 hover:text-default-font"
              aria-label="More actions"
            >
              <SubframeCore.Icon
                className="text-body font-body"
                name="FeatherMoreHorizontal"
              />
            </button>
          </SubframeCore.DropdownMenu.Trigger>
          <SubframeCore.DropdownMenu.Portal>
            <SubframeCore.DropdownMenu.Content
              side="right"
              align="start"
              sideOffset={4}
              asChild
            >
              <DropdownMenu>
                {onViewTemplate && (
                  <DropdownMenu.DropdownItem
                    icon="FeatherEye"
                    onClick={(e: React.MouseEvent) => {
                      e.stopPropagation();
                      onViewTemplate();
                    }}
                  >
                    View template
                  </DropdownMenu.DropdownItem>
                )}
                {onEdit && (
                  <DropdownMenu.DropdownItem
                    icon="FeatherEdit3"
                    onClick={(e: React.MouseEvent) => {
                      e.stopPropagation();
                      onEdit();
                    }}
                  >
                    Edit
                  </DropdownMenu.DropdownItem>
                )}
                {onDelete && (
                  <DropdownMenu.DropdownItem
                    icon="FeatherTrash2"
                    onClick={(e: React.MouseEvent) => {
                      e.stopPropagation();
                      onDelete();
                    }}
                  >
                    Delete
                  </DropdownMenu.DropdownItem>
                )}
              </DropdownMenu>
            </SubframeCore.DropdownMenu.Content>
          </SubframeCore.DropdownMenu.Portal>
        </SubframeCore.DropdownMenu.Root>
      )}
    </div>
  );
}

// Canonical rail section order — categories not in this list (e.g.
// "Custom" for analyst-uploaded product types) are appended below in
// insertion order. Mirrors PRODUCT_CATEGORY_ORDER in the backend's
// utils/exporter.py — keep these in sync if you add a category.
const RAIL_CATEGORY_ORDER: readonly string[] = [
  "Running Estimates & Reports",
  "IPOE",
  "Collection",
  "Targeting & Fires",
  "Single-INT",
];

// Per-section accent colors. A saturated `text` shade for the header, a
// `dot` swatch for the bullet, and a muted `border` for the section's
// left-edge bar. Rows stay in the default text color — the section
// "owns" its color.
//
// IMPORTANT: Tailwind's JIT scans the source for literal class strings.
// Keep the values as plain template-free strings so the build sees them.
interface CategoryAccent {
  text: string;
  border: string;
  dot: string;
}
const CATEGORY_ACCENT: Record<string, CategoryAccent> = {
  "Running Estimates & Reports": {
    text: "text-emerald-400",
    border: "border-emerald-500/40",
    dot: "bg-emerald-500",
  },
  "IPOE": {
    text: "text-sky-400",
    border: "border-sky-500/40",
    dot: "bg-sky-500",
  },
  "Collection": {
    text: "text-amber-400",
    border: "border-amber-500/40",
    dot: "bg-amber-500",
  },
  "Targeting & Fires": {
    text: "text-rose-400",
    border: "border-rose-500/40",
    dot: "bg-rose-500",
  },
  "Single-INT": {
    text: "text-violet-400",
    border: "border-violet-500/40",
    dot: "bg-violet-500",
  },
};
const DEFAULT_ACCENT: CategoryAccent = {
  text: "text-neutral-400",
  border: "border-neutral-500/40",
  dot: "bg-neutral-500",
};

/** Group a flat product-type list into ordered category sections.
 *  Items missing `category` are dropped (intentional — that's how the
 *  backend hides any uncategorized internal type). */
function groupByCategory(productTypes: any[]): Array<{
  category: string;
  items: any[];
}> {
  const buckets = new Map<string, any[]>();
  for (const pt of productTypes) {
    if (!pt.category) continue;
    const list = buckets.get(pt.category);
    if (list) list.push(pt);
    else buckets.set(pt.category, [pt]);
  }

  // Stable sort within each category using the backend-supplied order.
  for (const list of buckets.values()) {
    list.sort(
      (a, b) =>
        (a.categoryOrder ?? 0) - (b.categoryOrder ?? 0) ||
        (a.productLongName || a.productName || "").localeCompare(
          b.productLongName || b.productName || "",
        ),
    );
  }

  // Canonical categories first (in declared order), then any remaining
  // categories (like "Custom") in insertion order so analyst uploads
  // get a stable group at the bottom.
  const out: Array<{ category: string; items: any[] }> = [];
  for (const cat of RAIL_CATEGORY_ORDER) {
    const items = buckets.get(cat);
    if (items && items.length) {
      out.push({ category: cat, items });
      buckets.delete(cat);
    }
  }
  for (const [category, items] of buckets) {
    if (items.length) out.push({ category, items });
  }
  return out;
}

// Category collapse persists per analyst so the rail remembers which
// sections they folded away. We store the COLLAPSED set (not the expanded
// one), so the default — nothing stored — leaves every category open and
// preserves the rail's at-a-glance scannability.
const COLLAPSED_STORAGE_KEY = "productRail.collapsedCategories";

function loadCollapsedCategories(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(COLLAPSED_STORAGE_KEY);
    const arr = raw ? JSON.parse(raw) : null;
    return Array.isArray(arr) ? new Set(arr) : new Set();
  } catch {
    return new Set();
  }
}

export function ProductTypeList({
  productTypes,
  selectedKey,
  onRailFilter,
  onRailEdit,
  onRailDelete,
  onRailViewTemplate,
}: ProductTypeListProps) {
  // Doctrinal references aren't a writable product, they're a knowledge
  // source — exclude them from the rail.
  const filtered = productTypes.filter((pt) => pt.productName !== "Doctrines");
  const groups = groupByCategory(filtered);

  // Collapsed-category set, hydrated from localStorage on mount. Toggling a
  // category header folds/unfolds its items and writes the new set back so
  // the choice survives reloads.
  const [collapsed, setCollapsed] = React.useState<Set<string>>(
    loadCollapsedCategories,
  );
  const toggleCategory = React.useCallback((category: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(category)) next.delete(category);
      else next.add(category);
      if (typeof window !== "undefined") {
        try {
          window.localStorage.setItem(
            COLLAPSED_STORAGE_KEY,
            JSON.stringify([...next]),
          );
        } catch {
          /* localStorage unavailable (e.g. private mode) — collapse still
             works for this session, it just won't persist. */
        }
      }
      return next;
    });
  }, []);

  return (
    <div className="flex w-full flex-col items-start gap-3">
      {groups.map(({ category, items }) => {
        const accent = CATEGORY_ACCENT[category] || DEFAULT_ACCENT;
        const isCollapsed = collapsed.has(category);
        return (
          <div
            key={category}
            // Left-edge accent bar runs the full height of the section
            // (header + items), so the section reads as one visual group.
            className={SubframeCore.twClassNames(
              "flex w-full flex-col items-start gap-0.5 border-l-2 pl-2",
              accent.border,
            )}
          >
            <button
              type="button"
              onClick={() => toggleCategory(category)}
              aria-expanded={!isCollapsed}
              className="flex w-full items-center gap-2 px-3 pt-2 pb-1 rounded-md cursor-pointer hover:bg-neutral-50 transition-colors"
            >
              <span
                className={SubframeCore.twClassNames(
                  "inline-block h-1.5 w-1.5 rounded-full flex-none",
                  accent.dot,
                )}
              />
              <span
                className={SubframeCore.twClassNames(
                  "text-caption-bold font-caption-bold uppercase tracking-wider",
                  accent.text,
                )}
              >
                {category}
              </span>
              <span className="text-caption font-caption text-subtext-color">
                {items.length}
              </span>
              <SubframeCore.Icon
                className={SubframeCore.twClassNames(
                  "ml-auto flex-none text-caption text-subtext-color transition-transform",
                  isCollapsed ? "-rotate-90" : "",
                )}
                name="FeatherChevronDown"
              />
            </button>
            {!isCollapsed &&
              items.map((pt) => (
                <RailItem
                  key={pt.productTypeId}
                  product={pt}
                  selected={!!selectedKey && railFilterKey(pt) === selectedKey}
                  format={productFormat(pt)}
                  onClick={() => onRailFilter?.(pt)}
                  // Edit / Delete only on custom (non-builtin) types —
                  // built-in doctrinal products are owned by the platform.
                  onEdit={
                    pt.isBuiltin || !onRailEdit
                      ? undefined
                      : () => onRailEdit(pt)
                  }
                  onDelete={
                    pt.isBuiltin || !onRailDelete
                      ? undefined
                      : () => onRailDelete(pt)
                  }
                  onViewTemplate={
                    pt.file?.key && onRailViewTemplate
                      ? () => onRailViewTemplate(pt)
                      : undefined
                  }
                />
              ))}
          </div>
        );
      })}
    </div>
  );
}

export default ProductTypeList;
