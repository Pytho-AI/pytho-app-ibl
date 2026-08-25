"use client";

import React, { useState, useRef, useEffect } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import * as SubframeCore from "@subframe/core";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { Button } from "../subframe/components/Button";
import { localUpload } from "../components/FileUploader";
import { CheckboxCard } from "../subframe/components/CheckboxCard";

function FocusMode({ open, setOpen, docs, dataSources, onFocusMode }) {
  const [files, setFiles] = useState([])
  const [links, setLinks] = useState([])
  const [googleScholarSelected, setGoogleScholarSelected] = useState([])
  const [fileUploadError, setFileUploadError] = useState('');
  const [linkError, setLinkError] = useState('');
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef(null);

  const allowedTypes = [".pdf", ".docx", ".txt"]

  useEffect(() => {
    setFiles(docs) // TODO: divide by file and links
    setGoogleScholarSelected(dataSources && dataSources.constructor === Array && dataSources.includes("googleScholar"))
  }, [docs, dataSources]);

  async function uploadFiles(files) {

    const droppedFiles = Array.from(files);
    const filteredFiles = droppedFiles.filter(f => allowedTypes.some(t => f.name.toLowerCase().endsWith(t)))
    if (filteredFiles.length != droppedFiles.length) {
      setFileUploadError('Only PDF, .docx, .txt are allowed')
      return
    }

    setUploading(true)
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
    event.dataTransfer.dropEffect = 'copy'; // Visual cue for drop effect
  };

  const handleDrop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    
    uploadFiles(event.dataTransfer.files)
  };

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full min-w-[768px] flex-col items-center gap-2 bg-default-background px-6 py-6 mobile:h-full mobile:w-full">
        <span className="text-heading-1 font-heading-1 text-default-font">
          Focus Mode
        </span>
        {/* TODO: add reference to domains */}
        <span className="text-body font-body text-default-font"> 
          Only chat with the files listed below. All other
          model/project data is excluded. 
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
              // put the old settings back
              setFiles(docs)
              setGoogleScholarSelected(dataSources && dataSources.constructor === Array && dataSources.includes("googleScholar"))
            }}
          >
            Cancel
          </Button>
          <Button
            icon={null}
            iconRight={null}
            disabled={uploading}
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              var dataSources = []
              if (googleScholarSelected) {
                dataSources.push('googleScholar')
              }
              onFocusMode(files.concat(links), dataSources)
              setOpen(false)
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
  );
}

export default FocusMode;