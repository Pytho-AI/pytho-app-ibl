"use client";

import React, { useState, useRef, useEffect } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import * as SubframeCore from "@subframe/core";
import { TextField } from "../subframe/components/TextField";
import { TextArea } from "../subframe/components/TextArea";
import { Button } from "../subframe/components/Button";
import { localUpload } from "../components/FileUploader";
import { useAppContext } from "../lib/contextLib";
import { uuid } from "../lib/uuid";

function CreateProduct({ open, setOpen, onNewProductType, onDeleteProductType, productTypes, editProductType }) {
  const { user } = useAppContext();
  const allowedTypes = [".docx", ".pdf"]
  const maxSizeInKB = 1000
  const isEditMode = !!editProductType;
  
  const [files, setFiles] = useState([]);
  const [productName, setProductName] = useState("")
  const [productNameError, setProductNameError] = useState("")
  const [productOverview, setProductOverview] = useState("")
  const [fileUploadError, setFileUploadError] = useState('');
  const [uploading, setUploading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [existingFile, setExistingFile] = useState(null)
  const fileInputRef = useRef(null);

  useEffect(() => {
    if (!open) {
      setProductName("")
      setProductOverview("")
      setFiles([])
      setProductNameError("")
      setFileUploadError("")
      setExistingFile(null)
      setUploading(false)
      setSaving(false)
    } else if (open && editProductType) {
      setProductName(editProductType.productName || "")
      setProductOverview(editProductType.productOverview || "")
      if (editProductType.file) {
        setExistingFile(editProductType.file)
      }
    }
  }, [open, editProductType]);

  async function uploadFiles(currentFiles) {
    if (files.length > 0) {
      setFileUploadError('Only one file is allowed')
      return  
    }

    const droppedFiles = Array.from(currentFiles);
    const filteredFiles = droppedFiles.filter(f => allowedTypes.some(t => f.name.toLowerCase().endsWith(t)))
    if (filteredFiles.length != droppedFiles.length) {
      setFileUploadError('Only Docx and PDF files are allowed')
      return
    }

    const file = currentFiles[0]

    if (file.size > maxSizeInKB * 1000) {
      setFileUploadError(`File must be less than ${maxSizeInKB} KB`)
      return
    }

    setUploading(true)
    setFileUploadError('')
    
    try {   
      setFiles([await localUpload(file)]);
    } catch (e) {
      setFileUploadError("Failed to upload. Please try again.")
    }

    setUploading(false)
  } 

  async function handleFileChange(event) {
    if (event.target.files.length == 0) {
      return
    } 
  
    uploadFiles(event.target.files)
  }

  const handleDragOver = (event) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy';
  };

  const handleDrop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    uploadFiles(event.dataTransfer.files)
  };

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full max-h-[768px] w-full flex-col items-start gap-2">
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-center">
          <div className="flex w-full items-center border-b border-solid border-neutral-border px-4 py-4">
            <IconButton
              variant="neutral-tertiary"
              size="medium"
              icon="FeatherX"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpen(false)
              }}
            />
            <div className="flex grow shrink-0 basis-0 items-center justify-center gap-2 pr-6">
              <span className="text-heading-2 font-heading-2 text-default-font">
                {isEditMode ? "Edit Product Type" : "Create a Product Type"}
              </span>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-4 px-8 py-4 overflow-auto">
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              Product Name
            </span>
            <span className="text-body font-body text-default-font">
              What is the name of your product?
            </span>
            <TextField
              className="h-auto w-full flex-none"
              label=""
              helpText={productNameError}
              error={productNameError.length > 0}  
            >
              <TextField.Input
                placeholder="Example: F-77 Report"
                value={productName}
                onChange={(
                  event: React.ChangeEvent<HTMLInputElement>
                ) => {
                  setProductNameError("")
                  setProductName(event.target.value)
                }}
              />
            </TextField>
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              Product Overview
            </span>
                
            <span className="text-body font-body text-default-font">
              In a few sentences, describe your product and its purpose.
            </span>
            <TextArea className="h-auto w-full flex-none" label="" helpText="">
              <TextArea.Input
                className="h-auto min-h-[112px] w-full flex-none resize-none"
                placeholder="Example: An F-77 Report for US Embassies refers to a detailed report that captures the Emergency Action Plan (EAP) for the post. Specifically, it provides critical information on the personnel, dependents, and facilities associated with the embassy. It is used to assist in emergency planning, evacuation, and crisis management."
                value={productOverview}
                onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => {
                  setProductOverview(event.target.value)
                }}
              />
            </TextArea>
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              Upload Product
            </span>
            <span className="text-body font-body text-default-font">
              {isEditMode
                ? "Upload a new file to replace the existing template, or leave empty to keep the current one."
                : "Upload a completed Docx or PDF of your product. Ensure it's not just a template and it is filled out in your desired format."}
            </span>
            <div className="flex w-full flex-col items-start gap-4">
              <TextArea 
                className="w-full rounded-md"
                helpText={fileUploadError}
                error={fileUploadError.length > 0}
              >
                <div 
                  className="flex w-full flex-col items-center justify-center gap-2 rounded-md px-6 py-6 cursor-pointer"
                  onDragOver={handleDragOver}
                  onDrop={handleDrop}
                  onClick={() => fileInputRef.current.click()}
                >
                  {uploading ? 
                  <Button
                    size="large"
                    variant="brand-secondary"
                    loading={uploading}
                    icon="FeatherTrash"
                  /> : 
                  <SubframeCore.Icon
                    className="text-heading-1 font-heading-1 text-brand-700"
                    name="FeatherUploadCloud"
                  />}    
                  <div className="flex flex-col items-center justify-center gap-1">
                    <span className="text-body font-body text-default-font text-center">
                      Click to select a template
                    </span>
                    <span className="text-caption font-caption text-subtext-color text-center">
                      Up to 1 template, max file size {maxSizeInKB} KB
                    </span>
                  </div>
                </div>
              </TextArea>
              <div className="flex w-full flex-col items-start gap-2">
                {existingFile && !files.length && (
                  <div className="flex w-full items-center gap-2 rounded-md bg-neutral-50 px-2 py-2">
                    <div className="flex items-center gap-4 px-1 py-1">
                      <SubframeCore.Icon
                        className="text-body font-body text-brand-700"
                        name="FeatherFile"
                      />
                    </div>
                    <span className="grow shrink-0 basis-0 text-caption-bold font-caption-bold text-default-font">
                      {existingFile.name || "Current template file"}
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      (Upload new file to replace)
                    </span>
                  </div>
                )}
                {files.map((file, index)=> 
                <div key={index} className="flex w-full items-center gap-2 rounded-md bg-neutral-50 px-2 py-2">
                  <div className="flex items-center gap-4 px-1 py-1">
                    <SubframeCore.Icon
                      className="text-body font-body text-brand-700"
                      name="FeatherCheckCircle"
                    />
                  </div>
                  <span className="grow shrink-0 basis-0 text-caption-bold font-caption-bold text-default-font">
                    {file.name}
                  </span>
                  <IconButton
                    size="small"
                    icon="FeatherTrash"
                    onClick={(
                      event: React.MouseEvent<HTMLButtonElement>
                    ) => {
                      files.splice(index, 1);
                      setFiles([...files])
                    }}
                  />
                </div>)}
              </div>
            </div>
          </div>
        </div>
        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
        
        <div className="flex w-full flex-none items-center justify-end px-6 py-4">
          <div className="flex items-center gap-2">
            <Button
              variant="brand-secondary"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpen(false)
              }}
            >
              Cancel
            </Button>
            <Button
              loading={saving}
              onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                setSaving(true)

                let error = false
                if (productName.length === 0) {
                  setProductNameError("A name is required for the product")
                  error = true
                }

                const productNameSet = new Set(
                  productTypes
                    .filter(pt => !isEditMode || pt.productTypeId !== editProductType.productTypeId)
                    .map(pt => pt.productName)
                );
                if (!error && productNameSet.has(productName)) {
                  setProductNameError("Product name is already in use")
                  error = true
                }

                if (!isEditMode && files.length === 0) {
                  setFileUploadError("A sample file is required")
                  error = true
                }

                if (error) {
                  setSaving(false)
                  return
                }

                try {
                  if (isEditMode) {
                    const response = await fetch(`/api/product_types/${editProductType.productTypeId}`, {
                      method: 'PUT',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        productName,
                        productOverview,
                        userId: user.userId,
                        ...(files.length > 0 && { file: { key: files[0].key, name: files[0].name } })
                      })
                    });
                    const result = await response.json()
                    onNewProductType(result)
                  } else {
                    const response = await fetch("/api/product_types", {
                      method: 'POST',
                      headers: { 'Content-Type': 'application/json' },
                      body: JSON.stringify({
                        productTypeId: uuid(),
                        userId: user.userId,
                        email: user.email,
                        productName,
                        productOverview,
                        file: { key: files[0].key, name: files[0].name }
                      })
                    });
                    const result = await response.json()
                    onNewProductType(result)
                  }
                } catch (e) {
                  console.error("Error saving product type:", e)
                }

                setSaving(false)
                setOpen(false)
              }}
            >
              Save
            </Button>
          </div>
        </div>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        hidden
        accept=".docx, .pdf"
        onChange={handleFileChange}
      />
    </DialogLayout>
  );
}

export default CreateProduct;
