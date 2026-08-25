import React, { useRef, useState } from 'react';
import { ReactElement } from "react";
import { IconButton } from "../subframe/components/IconButton";
import { Storage } from "aws-amplify";
import axios from "axios";

export async function s3Upload(file) {
  const filename = `${Date.now()}-${file.name}`;

  const stored = await Storage.vault.put(filename, file, {
    level: 'public',
    contentType: file.type,
  });

  return stored.key;
}

export async function localUpload(file) {
  const formData = new FormData();
  formData.append("file", file);

  const response = (await axios.post("/api/upload/", formData, {
    headers: { "Content-Type": "multipart/form-data" },
  })).data;
  
  return { 
    key: response.key, 
    url: response.url, 
    name: file.name, 
    type: 'file',
    title: response.title,
  }
}


export default function FileUploader({ onFilesUpload }): ReactElement {
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef(null);

  const MAX_SIZE = 50000000

  async function handleFileChange(event) {
    if (event.target.files.length == 0) {
      return
    } 
  
    setUploading(true)  
    const contextDocs = []
      
    for (let i=0; i < event.target.files.length; i++) {
      const file = event.target.files[i]
      
      if (file) {
        const key = await s3Upload(file);
        //const url = await Storage.vault.get(key);
        const url = await Storage.get(key, {
          level: 'public', 
          expires: 604800, // 7 days
        });

        const name = file.name
        contextDocs.push({ key, url, name })
      }
      
    }

    onFilesUpload(contextDocs)
    setUploading(false)
  }
  
  return <>
    <IconButton
      size="medium"
      icon="FeatherPaperclip" 
      loading={uploading}
      onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
        fileInputRef.current.click();
      }}
    />
    <input
      ref={fileInputRef}
      type="file"
      hidden
      accept=".pdf,.docx,.txt"
      multiple
      onChange={handleFileChange}
    />
  </>
    
}