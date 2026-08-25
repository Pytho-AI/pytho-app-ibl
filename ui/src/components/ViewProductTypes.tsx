"use client";

import { useState, useEffect } from "react";
import { Button } from "../subframe/components/Button";
import { useAppContext } from "../lib/contextLib";
import ProductLibraryModal, { GeneratedArtifact } from "./ProductLibraryModal";

function ViewProductTypes({
  productType,
  setProductType,
  disabled = false,
  onGenerated,
}: {
  productType: any;
  setProductType: (pt: any) => void;
  disabled?: boolean;
  onGenerated?: (result: GeneratedArtifact) => void;
}) {
  const { user, projectId } = useAppContext();
  const [openProductLibrary, setOpenProductLibrary] = useState(false)
  const [productTypes, setProductTypes] = useState<any[]>([])
  const [loadingProducts, setLoadingProducts] = useState(true)

  async function fetchProductTypes() {
    setLoadingProducts(true)
    try {
      const response = await fetch("/api/product_types")
      if (response.ok) {
        const results = await response.json()
        setProductTypes(results)
      }
    } catch(e) {    
      console.error("Error fetching product types:", e)
    } finally {
      setLoadingProducts(false)
    }
  }

  useEffect(() => {
    fetchProductTypes();
  }, []);

  useEffect(() => {
    if (openProductLibrary) {
      fetchProductTypes();
    }
  }, [openProductLibrary]);

  function onNewProductType(updatedProductType) {
    const exists = productTypes.some(pt => pt.productTypeId === updatedProductType.productTypeId);
    if (exists) {
      setProductTypes(productTypes.map(pt => 
        pt.productTypeId === updatedProductType.productTypeId ? updatedProductType : pt
      ))
      if (productType?.productTypeId === updatedProductType.productTypeId) {
        setProductType(updatedProductType)
      }
    } else {
      setProductTypes([updatedProductType, ...productTypes])
      setProductType(updatedProductType)
    }
  }

  function onDeleteProductType(deletedProductTypeId) {
    setProductTypes(productTypes.filter(pt => pt.productTypeId !== deletedProductTypeId))
    if (productType?.productTypeId === deletedProductTypeId) {
      setProductType('')
    }
  }

  // Doctrines has its own dedicated pill in ChatMessageBar, so don't show
  // the active "Wargaming Products" state for it.
  const showActive = productType?.isAgentic && productType?.productName !== 'Doctrines'

  return (<>
    {showActive ? <Button
      className="h-auto w-auto flex-none"
      variant="brand-primary"
      iconRight="FeatherBookmarkX"
      disabled={disabled}
      onClick={() => {
        setProductType('')
      }}
    >
      {productType.productLongName || productType.productName}
    </Button> :
    <Button
      className="h-auto w-auto flex-none"
      variant="neutral-secondary"
      disabled={disabled}
      onClick={() => setOpenProductLibrary(true)}
    >
      Wargaming Products
    </Button>}
    <ProductLibraryModal
      open={openProductLibrary}
      setOpen={setOpenProductLibrary}
      productTypes={productTypes}
      loading={loadingProducts}
      setProductType={setProductType}
      onNewProductType={onNewProductType}
      onDeleteProductType={onDeleteProductType}
      projectId={projectId}
      userId={user?.userId || user?.email}
      onGenerated={onGenerated}
    />
  </>)
}

export default ViewProductTypes;
