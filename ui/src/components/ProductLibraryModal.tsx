"use client";

import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import { Button } from "../subframe/components/Button";
import { Loader } from "../subframe/components/Loader";
import CreateProduct from "./CreateProduct";
import DeleteProductModal from "./DeleteProductModal";
import { uuid } from "../lib/uuid";

interface ProductLibraryModalProps {
  open: boolean;
  setOpen: (open: boolean) => void;
  productTypes: any[];
  loading?: boolean;
  setProductType: (pt: any) => void;
  onNewProductType: (pt: any) => void;
  onDeleteProductType: (id: string) => void;
  // Manual-dispatch (Generate button) plumbing — Req 4.
  // The Generate button lives on each product card and POSTs to
  // /api/products/generate, then hands the resulting htmlLink to the
  // owning page's existing DocEditor mount via `onGenerated`.
  projectId?: string;
  userId?: string;
  onGenerated?: (result: GeneratedArtifact) => void;
}

export interface GeneratedArtifact {
  session_id: string;
  artifact_id: string;
  artifact_type: string;
  htmlLink?: string | null;
  docxLink?: string | null;
  pptxLink?: string | null;
  gaps?: string[];
}

function ProductCard({
  product,
  onSelect,
  onEdit,
  onDelete,
  onViewTemplate,
  onGenerate,
  generating,
  canGenerate,
}: {
  product: any;
  onSelect: () => void;
  onEdit?: () => void;
  onDelete?: () => void;
  onViewTemplate?: () => void;
  onGenerate?: () => void;
  generating?: boolean;
  canGenerate?: boolean;
}) {
  // Body text: doctrine.purpose (one-sentence card description) when surfaced
  // by /api/product_types for built-ins; falls back to productOverview
  // (doctrinal citation) for custom uploads.
  const body = product.purpose || product.productOverview || "No description provided.";
  const formats: string[] = Array.isArray(product.output_formats) ? product.output_formats : [];
  return (
    <button
      type="button"
      className="flex w-full flex-col items-start gap-3 rounded-md border border-solid border-neutral-border bg-neutral-50 px-3 py-3 cursor-pointer hover:border-brand-300 transition-colors text-left"
      onClick={onSelect}
    >
      <div className="flex w-full items-center justify-between">
        <div className="flex items-baseline gap-2 truncate pr-2">
          <span className="text-heading-3 font-heading-3 text-default-font truncate">
            {product.productLongName || product.productName}
          </span>
          {product.productLongName && product.productLongName !== product.productName && (
            <span className="rounded bg-neutral-100 px-1.5 py-0.5 text-caption font-caption text-subtext-color flex-none">
              {product.productName}
            </span>
          )}
        </div>
        <div className="flex items-center gap-0.5 flex-none">
          {onViewTemplate && (
            <IconButton
              variant="neutral-tertiary"
              size="small"
              icon="FeatherEye"
              onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                e.stopPropagation();
                onViewTemplate();
              }}
            />
          )}
          {onEdit && (
            <IconButton
              variant="neutral-tertiary"
              size="small"
              icon="FeatherEdit3"
              onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                e.stopPropagation();
                onEdit();
              }}
            />
          )}
          {onDelete && (
            <IconButton
              variant="neutral-tertiary"
              size="small"
              icon="FeatherTrash2"
              onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
                e.stopPropagation();
                onDelete();
              }}
            />
          )}
        </div>
      </div>
      <span className="line-clamp-3 text-caption font-caption text-subtext-color">
        {body}
      </span>
      {(formats.length > 0 || product.shape) && (
        <div className="flex w-full flex-wrap items-center gap-1.5 text-caption font-caption text-subtext-color">
          {product.shape && (
            <span className="rounded bg-neutral-100 px-1.5 py-0.5">
              {product.shape}
            </span>
          )}
          {formats.map((f) => (
            <span key={f} className="rounded bg-neutral-100 px-1.5 py-0.5 uppercase">
              {f}
            </span>
          ))}
        </div>
      )}
      {onGenerate && (
        <div className="flex w-full items-center justify-end pt-1">
          <Button
            variant="brand-primary"
            size="small"
            icon={generating ? undefined : "FeatherPlay"}
            disabled={!!generating || canGenerate === false}
            onClick={(e: React.MouseEvent<HTMLButtonElement>) => {
              e.stopPropagation();
              onGenerate();
            }}
          >
            {generating ? "Generating..." : "Generate"}
          </Button>
        </div>
      )}
    </button>
  );
}

function ProductLibraryModal({
  open,
  setOpen,
  productTypes,
  loading = false,
  setProductType,
  onNewProductType,
  onDeleteProductType,
  projectId,
  userId,
  onGenerated: _onGenerated,
}: ProductLibraryModalProps) {
  // `onGenerated` is retained on the props interface for backward compat with
  // the old chat-page wiring (Chat.tsx passes a handler that opens the
  // returned htmlLink in DocEditor). The new Generate flow navigates the
  // user directly to DocEditor and streams the artifact in over the
  // /ws/intel_agent socket — the modal never resolves a result itself.
  void _onGenerated;
  const navigate = useNavigate();
  const [openCreateProduct, setOpenCreateProduct] = useState(false);
  const [editProductType, setEditProductType] = useState<any>(null);
  const [deleteTarget, setDeleteTarget] = useState<any>(null);
  const [deleting, setDeleting] = useState(false);
  const [generateError, setGenerateError] = useState<string | null>(null);

  // Generate click now navigates IMMEDIATELY to DocEditor with a fresh
  // session_id and the pending-product-type flag. DocEditor mounts in a
  // placeholder state, opens its ChatAgent (the persistent intel-agent
  // socket), and seeds the FIRST WS message with a synthesized "Generate
  // {productKey}" prompt. OE's confirmation requests + final artifact then
  // stream into the same sidebar the analyst is already looking at.
  //
  // The legacy synchronous /api/products/generate path is intentionally
  // dropped from the UI — that path black-boxes 60-180s of OE work behind
  // a frozen gray button and gives the analyst no way to answer OE's
  // confirmation prompts (Req: see CLAUDE/ChatPanel UX rework). The
  // endpoint is kept on the backend for scripted callers (see
  // handlers/product.py:generate). `onGenerated` is no longer invoked
  // here; DocEditor itself listens for the artifact WS event.
  function runGenerate(pt: any) {
    if (!pt?.template?.productKey) {
      setGenerateError("Generate is only available for the built-in doctrinal products.");
      return;
    }
    if (!projectId || !userId) {
      setGenerateError("Missing project or user context — cannot generate.");
      return;
    }
    setGenerateError(null);
    const sessionId = uuid();
    const productKey = pt.template.productKey as string;
    // First (and only) entry in output_formats tells DocEditorPage which
    // editor surface to mount BEFORE the artifact arrives — so analysts
    // skip the TinyMCE skeleton flash when generating an xlsx-canonical
    // product. Falls back to the singular output_format if a custom
    // product type doesn't carry the plural list yet.
    const formats: string[] = Array.isArray(pt.output_formats) && pt.output_formats.length
      ? pt.output_formats
      : (pt.output_format ? [pt.output_format] : []);
    const pendingFormat = formats[0] || "";
    const params = new URLSearchParams({
      session_id: sessionId,
      pending_product_type: productKey,
      project_id: projectId,
      user_id: userId,
      product_name: pt.productName || productKey,
      // The product's doctrinal page_orientation (from the schema, surfaced
      // by /api/product_types). DocEditor uses this to render the skeleton
      // and the initial TinyMCE content_style with the correct page bounds
      // BEFORE the artifact arrives — otherwise the skeleton renders portrait
      // and snaps to landscape only after generation completes.
      // Orientation IS the format choice. Landscape products ARE PPTX slides
      // (16:9, no DRAFT watermark); portrait products ARE DOCX Letter pages
      // (8.5×11, DRAFT default). DocEditor derives page shape + watermark
      // from this one value.
      pending_orientation: (pt.page_orientation === 'landscape' ? 'landscape' : 'portrait'),
      pending_format: pendingFormat,
    });
    setOpen(false);
    navigate(`/missioncommand/docs/${encodeURIComponent(sessionId)}?${params.toString()}`);
  }

  function handleSelect(pt: any) {
    setProductType(pt);
    setOpen(false);
  }

  function handleEdit(pt: any) {
    setEditProductType(pt);
    setOpenCreateProduct(true);
  }

  function handleDelete(pt: any) {
    setDeleteTarget(pt);
  }

  function handleViewTemplate(pt: any) {
    if (!pt.file?.key) return;
    window.open("/files/" + pt.file.key, "_blank");
  }

  async function confirmDelete() {
    if (!deleteTarget?.productTypeId) return;
    setDeleting(true);
    try {
      await fetch(`/api/product_types/${deleteTarget.productTypeId}?user_id=${deleteTarget.userId}`, {
        method: "DELETE",
      });
      onDeleteProductType(deleteTarget.productTypeId);
    } catch (error) {
      console.error("Error deleting product type:", error);
    } finally {
      setDeleting(false);
      setDeleteTarget(null);
    }
  }

  function handleNewProductType(updatedProductType: any) {
    onNewProductType(updatedProductType);
    setEditProductType(null);
  }

  function handleDeleteProductType(deletedProductTypeId: string) {
    onDeleteProductType(deletedProductTypeId);
    setEditProductType(null);
  }

  return (
    <>
      <DialogLayout open={open} onOpenChange={setOpen}>
        <div className="flex w-[1024px] h-[600px] flex-col items-start gap-6 px-6 py-6">
          <div className="flex w-full items-center justify-between flex-none">
            <span className="text-heading-2 font-heading-2 text-default-font">
              Product Library
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="brand-secondary"
                icon="FeatherPlus"
                onClick={() => {
                  setEditProductType(null);
                  setOpenCreateProduct(true);
                }}
              >
                Create Your Own
              </Button>
              <IconButton
                icon="FeatherX"
                onClick={() => setOpen(false)}
              />
            </div>
          </div>

          {generateError && (
            <div className="w-full rounded-md border border-solid border-error-400 bg-error-50 px-3 py-2 text-caption font-caption text-error-700">
              {generateError}
            </div>
          )}
          <div className="w-full flex-1 overflow-y-auto">
            {loading ? (
              <div className="flex w-full items-center justify-center py-20">
                <Loader size="large" />
              </div>
            ) : productTypes.length === 0 ? (
              <div className="flex w-full items-center justify-center py-12">
                <span className="text-body font-body text-subtext-color">
                  No products yet. Create your first one!
                </span>
              </div>
            ) : (
              <div className="w-full items-start gap-4 grid grid-cols-3">
                {productTypes.filter((pt) => pt.productName !== "Doctrines").map((pt) => {
                  // Generate button only on built-in doctrinal products
                  // (custom uploads ride the existing chat-driven path).
                  const canShowGenerate = !!pt.template?.productKey && !!projectId && !!userId;
                  return (
                    <ProductCard
                      key={pt.productTypeId}
                      product={pt}
                      onSelect={() => handleSelect(pt)}
                      onViewTemplate={pt.file?.key ? () => handleViewTemplate(pt) : undefined}
                      onEdit={pt.isBuiltin ? undefined : () => handleEdit(pt)}
                      onDelete={pt.isBuiltin ? undefined : () => handleDelete(pt)}
                      onGenerate={canShowGenerate ? () => runGenerate(pt) : undefined}
                      generating={false}
                      canGenerate={canShowGenerate}
                    />
                  );
                })}
              </div>
            )}
          </div>
        </div>
      </DialogLayout>
      <CreateProduct
        open={openCreateProduct}
        setOpen={(isOpen) => {
          setOpenCreateProduct(isOpen);
          if (!isOpen) {
            setEditProductType(null);
          }
        }}
        onNewProductType={handleNewProductType}
        onDeleteProductType={handleDeleteProductType}
        productTypes={productTypes}
        editProductType={editProductType}
      />
      <DeleteProductModal
        open={!!deleteTarget}
        setOpen={(isOpen) => {
          if (!isOpen) setDeleteTarget(null);
        }}
        productName={deleteTarget?.productLongName || deleteTarget?.productName || ""}
        onConfirmDelete={confirmDelete}
        deleting={deleting}
      />
    </>
  );
}

export default ProductLibraryModal;
