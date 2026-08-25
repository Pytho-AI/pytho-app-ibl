"use client";

import React, { useState, useRef } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import * as SubframeCore from "@subframe/core";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { Button } from "../subframe/components/Button";
import { localUpload } from "../components/FileUploader";
import { useAppContext } from "../lib/contextLib";

function VectorDBUpdate({ setDocuments }) {
  const { user } = useAppContext();

  const [open, setOpen] = useState(false)
  const [files, setFiles] = useState([])
  const [fileUploadError, setFileUploadError] = useState('');
  const [uploading, setUploading] = useState(false)
  const [uploaded, setUploaded] = useState(false)
  const [updating, setUpdating] = useState(false)
  const fileInputRef = useRef(null);

  const allowedTypes = [".pdf", ".docx", ".txt"]

  async function uploadFiles(files) {
    const droppedFiles = Array.from(files);
    const filteredFiles = droppedFiles.filter(f => allowedTypes.some(t => f.name.toLowerCase().endsWith(t)))
    if (filteredFiles.length != droppedFiles.length) {
      setFileUploadError('Only PDF, .docx, .txt are allowed')
      return
    }

    setUploading(true)
    setUploaded(false)
    setFileUploadError('')

    try {
      const docs = []
      for (const file of files) {
        docs.push(await localUpload(file))  
      }
      
      setFiles((prevFiles) => [...prevFiles, ...docs]);
    } catch (e) {
      setFileUploadError("Failed to upload. Please try again.")
    }

    setUploading(false)
    setUploaded(true)
  } 

  async function updateDB() {
    try {
      for (const file of files) {
        const requestOptions = {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          signal: AbortSignal.timeout(60 * 60 * 1000), // 1 hour = 3600000 ms
          body: JSON.stringify({ 
            user_id: user.userId,
            file_id: file.key,
            link: file.url,
            title: file.title 
          })
        };

        await fetch("/api/insert_document", requestOptions)      
      }

      // query backend for updated list
      const response = await fetch("/api/query_documents", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ query: null }),
      });

      if (response.ok) {
        const data = await response.json();
        setDocuments(data)
      }

      setFiles([]);
      setOpen(false)
      setUpdating(false)
      setUploaded(false)
    } catch (e) {
      console.log(e)
      setFileUploadError("Failed to update vector db")
    }
  }

  async function handleFileChange(event) {
    event.preventDefault();
    if (event.target.files.length == 0) {
      return
    } 
  
    uploadFiles(event.target.files)
  }

  const handleDragOver = (event) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy'; // Visual cue for drop effect
  };

  const handleDrop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    
    uploadFiles(event.dataTransfer.files)
  };

  return (
    <>
    <div>
      <IconButton
        variant="brand-primary"
        onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
          setOpen(true)
        }}
      />
    </div>
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full min-w-[768px] flex-col items-center gap-2 bg-default-background px-6 py-6 mobile:h-full mobile:w-full">
        <span className="text-heading-1 font-heading-1 text-default-font">
          Upload Data
        </span>
        {/* TODO: add reference to domains */}
        <span className="text-body font-body text-default-font"> 
          Upload data sources to be used at the system level. For example: doctrine, TTPs, ROEs, Commander's Guidance, etc.  
        </span>
        <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-default-background shadow-sm px-6 py-6">
          <div className="flex w-full items-center gap-2 pb-4">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-default-font"
              name="FeatherFiles"
            />
            <span className="text-heading-3 font-heading-3 text-default-font">
              Upload Files
            </span>
            <span className="text-body font-body text-subtext-color">
              PDF, .docx, .txt etc
            </span>
          </div>
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
                onClick={() =>  fileInputRef.current.click()}
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
                    Click to select files or drag to upload
                  </span>
                  <span className="text-caption font-caption text-subtext-color text-center">
                    Up to 100 files, max file size 5MB
                  </span>
                </div>
              </div>
            </TextArea>
            <div className="flex w-full flex-col items-start gap-2">
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
        {/* <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-default-background shadow-sm px-6 py-6">
          <div className="flex w-full items-center gap-2 pb-4">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-default-font"
              name="FeatherGlobe"
            />
            <span className="text-heading-3 font-heading-3 text-default-font">
              From the Web
            </span>
            <span className="text-body font-body text-subtext-color">
              Insert Links Below (separate multiple links by comma)
            </span>
          </div>
          <TextArea className="h-auto w-full flex-none" label="" helpText={linkError} error={linkError.length > 0}>
            <TextArea.Input
              className="h-auto min-h-[96px] w-full flex-none resize-none"
              placeholder="www.bbc.com, www.abc.com, www.npr.org"
              value=""
              onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => {}}
            />
          </TextArea>
        </div> */}
        {/* <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
          <div className="flex w-full items-center gap-2 px-6 py-6">
            <span className="text-heading-3 font-heading-3 text-default-font">
              Google Scholar
            </span>
            <span className="text-body font-body text-subtext-color">
              Check the box below to search literature from Google Scholar.
            </span>
          </div>  
        </div>
        <CheckboxCard
          className="h-auto w-full flex-none"
          checked={googleScholarSelected}
          onCheckedChange={(checked: boolean) => { setGoogleScholarSelected(checked) }}
        >
          <span className="text-body-bold font-body-bold text-default-font">
            Enable Google Scholar
          </span>
        </CheckboxCard> */}
        
        <div className="flex w-full items-start justify-end gap-2">
          <Button
            variant="brand-secondary"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              setOpen(false)
            }}
          >
            Cancel
          </Button>
          <Button
            loading={updating}
            disabled={!uploaded}
            icon={null}
            iconRight={null}
            onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
              event.preventDefault()
              setUpdating(true)
              updateDB()
            }}
          >
            Save
          </Button>
        </div>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        hidden
        accept=".pdf,.docx,.txt"
        multiple
        onChange={handleFileChange}
      />
    </DialogLayout>
    </>
  );
}

export default VectorDBUpdate;