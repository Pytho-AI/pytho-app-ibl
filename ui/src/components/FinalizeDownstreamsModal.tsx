"use client";

/**
 * FinalizeDownstreamsModal — surfaced by DocEditorPage after a successful
 * POST /api/artifacts/{id}/finalize. The canonical-on-disk importer just
 * walked markers in the deliverable and overwrote schema_state.json; this
 * modal asks the analyst which immediate downstream products (the ones
 * whose schemas declare `artifact.<this_product>` in their depends_on)
 * should be re-fired against the new upstream.
 *
 * Per architecture spec (handlers/product.py:_compute_immediate_downstreams):
 *   - ONE HOP only. No transitive cascade. If the re-fired downstream
 *     itself has downstreams, the analyst finalizes that artifact next
 *     and a fresh modal opens for THAT hop.
 *   - Staleness is informational: "stale" ⇒ at least one pool entry of
 *     this product type predates the finalize mtime. "fresh" ⇒ none do
 *     (or the pool is empty).
 *   - Defaults: every checkbox is checked. The analyst can uncheck any
 *     downstream they don't want refired (e.g., a manually-curated
 *     product that shouldn't be regenerated).
 *
 * Single click: Generate DISPATCHES the refire jobs (the endpoint returns
 * immediately — the jobs run in the background) and then closes the modal
 * and goes to the dashboard, where the unified jobs registry shows live
 * progress. No blocking spinner, no second confirmation step.
 *
 * Layout matches the existing DialogLayout-based modal pattern (see
 * DeleteProductModal.tsx). NOT a new dialog primitive.
 */

import { useEffect, useMemo, useState } from "react";
import { Button } from "../subframe/components/Button";
import { Checkbox } from "../subframe/components/Checkbox";
import { IconButton } from "../subframe/components/IconButton";
import { DialogLayout } from "../subframe/layouts/DialogLayout";

export interface ImmediateDownstream {
  product_type: string;
  in_pool_artifact_ids: string[];
  staleness: "stale" | "fresh";
}

export interface RefireResult {
  dispatched: string[];
  errors?: { product_type: string; error: string }[];
}

interface FinalizeDownstreamsModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  upstreamProductType: string;
  downstreams: ImmediateDownstream[];
  // Resolves once the refire jobs are dispatched. A truthy result means the
  // jobs are running; returning void / throwing leaves the modal open so the
  // analyst can retry.
  onConfirm: (
    productTypes: string[],
  ) => Promise<RefireResult | void> | RefireResult | void;
  // Called after a successful dispatch — closes the modal and goes to the
  // dashboard (where live progress shows).
  onDone?: () => void;
  busy?: boolean;
}

function FinalizeDownstreamsModal({
  open,
  setOpen,
  upstreamProductType,
  downstreams,
  onConfirm,
  onDone,
  busy = false,
}: FinalizeDownstreamsModalProps) {
  // Selection state — every downstream defaults to checked. Reset on each
  // open so a fresh finalize → refire cycle never inherits stale selections
  // from a prior finalize on the same page mount.
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  useEffect(() => {
    if (open) {
      const init: Record<string, boolean> = {};
      for (const d of downstreams) init[d.product_type] = true;
      setSelected(init);
    }
  }, [open, downstreams]);

  const selectedTypes = useMemo(
    () => Object.entries(selected).filter(([, v]) => v).map(([k]) => k),
    [selected],
  );

  // Generate = dispatch the jobs, then go straight to the dashboard. One
  // click; the jobs run in the background and show progress there. On a
  // dispatch failure the parent returns void and surfaces its own error, so
  // the modal stays open for a retry.
  const handleConfirm = async () => {
    const res = await onConfirm(selectedTypes);
    if (res) {
      if (onDone) onDone();
      else setOpen(false);
    }
  };

  return (
    <DialogLayout open={open} onOpenChange={setOpen}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Refire downstream products?
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={() => setOpen(false)}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 px-6 py-6">
              <div className="flex w-full flex-col items-start gap-1">
                <span className="text-body font-body text-default-font">
                  Finalized{upstreamProductType ? <> <b>{upstreamProductType}</b></> : null}. Its
                  structured state was rebuilt from the deliverable.
                </span>
                {downstreams.length > 0 ? (
                  <span className="text-caption font-caption text-subtext-color">
                    Pick which downstream products should be re-fired against
                    the updated upstream. They generate in the background — you
                    go straight to the dashboard to watch progress.
                  </span>
                ) : (
                  <span className="text-caption font-caption text-subtext-color">
                    No downstream products depend on this artifact. Nothing
                    needs to be re-fired.
                  </span>
                )}
              </div>

              {downstreams.length > 0 && (
                <div className="flex w-full flex-col items-start gap-2">
                  {downstreams.map((d) => {
                    const count = d.in_pool_artifact_ids?.length || 0;
                    const isStale = d.staleness === "stale";
                    return (
                      <div
                        key={d.product_type}
                        className="flex w-full items-center gap-3 rounded-md border border-solid border-neutral-border px-3 py-2"
                      >
                        <Checkbox
                          checked={!!selected[d.product_type]}
                          onCheckedChange={(checked: boolean) =>
                            setSelected((cur) => ({ ...cur, [d.product_type]: checked }))
                          }
                          label={
                            <span className="text-body font-body text-default-font">
                              {d.product_type}
                            </span>
                          }
                        />
                        <div className="ml-auto flex items-center gap-2">
                          <span className="text-caption font-caption text-subtext-color">
                            {count} in pool
                          </span>
                          <span
                            className={
                              isStale
                                ? "rounded px-2 py-0.5 text-caption font-caption bg-warning-100 text-warning-800"
                                : "rounded px-2 py-0.5 text-caption font-caption bg-success-100 text-success-800"
                            }
                            title={
                              isStale
                                ? "At least one pool entry predates this finalize — re-fire recommended."
                                : "Pool is fresh relative to this finalize."
                            }
                          >
                            {isStale ? "stale" : "fresh"}
                          </span>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}

              <div className="flex w-full items-center justify-end gap-2">
                {selectedTypes.length === 0 ? (
                  <Button
                    className="h-8"
                    variant="brand-primary"
                    onClick={() => setOpen(false)}
                    disabled={busy}
                  >
                    Close
                  </Button>
                ) : (
                  <Button
                    className="h-8"
                    variant="brand-primary"
                    onClick={handleConfirm}
                    loading={busy}
                  >
                    Generate
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default FinalizeDownstreamsModal;
