"use client";

import React, { useState, useEffect } from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { Table } from "../subframe/components/Table";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import VectorDBUpdate from "../components/VectorDBUpdate";
import { TextField } from "../subframe/components/TextField";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { useLocation } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";

const PAGE_ITEMS_COUNT = 10


function ConfirmDeleteDocument({ open, setOpen, documents, setDocuments, deleteIndex }) {
  const { user } = useAppContext();
  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Delete Data Source
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setOpen(false)
                }}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Delete This Data Source
                </span>
                <span className="text-body font-body text-default-font">
                  Are you sure you want to delete <b>{documents[deleteIndex]?.title}</b>?
                </span>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  icon="FeatherArrowLeft"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpen(false)
                  }}
                >
                  Cancel
                </Button>
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  iconRight="FeatherArrowRight"
                  onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                    event.preventDefault()
                    const documentsToBeDeleted = documents[deleteIndex]
                    const url = `/api/delete_document?user_id=${user.userId}&file_id=${documentsToBeDeleted.doc_id}&title=${encodeURIComponent(documentsToBeDeleted.title)}`;

                    await fetch(url, { method: "DELETE" });
                    
                    setDocuments(documents.filter((_, index) => index !== deleteIndex));
                    setOpen(false)
                  }}
                >
                  Confirm
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

function DataView() {
  const location = useLocation();
  const [currentPageIndex, setCurrentPageIndex] = useState(0)
  const [deleteIndex, setDeleteIndex] = useState(-1)
  const [staticData, setStaticData] = useState([])
  const [openConfirmDeleteDocument, setOpenConfirmDeleteDocument] = useState(false)
  const sources = staticData

  const totalPages = Math.floor(sources.length / PAGE_ITEMS_COUNT);

  const [text, setText] = useState("");
  const [debouncedText, setDebouncedText] = useState("");

  // Update debouncedText 500ms after user stops typing
  useEffect(() => {
    const handler = setTimeout(() => setDebouncedText(text), 500);
    return () => clearTimeout(handler); // cleanup
  }, [text]);

  // Run function when debouncedText changes
  useEffect(() => {
    queryDocuments(debouncedText);
  }, [debouncedText]);

  const pageButtons: (number | 'ellipsis')[] = React.useMemo(() => {
    if (totalPages <= 10) {
      return Array.from({ length: totalPages }, (_, i) => i);      // 0‑based page indexes
    }
    return [
      ...Array.from({ length: 4 }, (_, i) => i), 
      'ellipsis',
      totalPages-1,                               
      totalPages                         
    ];
  }, [totalPages]);

  async function queryDocuments(queryText) {
    const response = await fetch("/api/query_documents", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ query: queryText }),
    });

    if (!response.ok) {
      throw new Error(`Error ${response.status}`);
    }

    const data = await response.json();
    setStaticData(data)
  }

  useEffect(() => {
    queryDocuments("")
  }, []);

  function displayData() {
    
    return sources
      .slice(currentPageIndex*PAGE_ITEMS_COUNT, (currentPageIndex+1)*PAGE_ITEMS_COUNT)
      .map((source, index) =>
      <Table.Row key={index}>
        <Table.Cell className="h-12 w-auto flex-none">
          <span className="whitespace-nowrap text-body font-body text-neutral-500">
            {currentPageIndex*PAGE_ITEMS_COUNT + index + 1}
          </span>
        </Table.Cell>
        <Table.Cell>
          <span className="whitespace-nowrap text-body-bold font-body-bold text-default-font">
            {source.title}
          </span>
        </Table.Cell>

        <Table.Cell className="h-12 w-auto flex-none">
          <div className="flex grow shrink-0 basis-0 items-center gap-2 justify-end">     
            <IconButton
              icon="FeatherTrash"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpenConfirmDeleteDocument(true)
                setDeleteIndex(currentPageIndex*PAGE_ITEMS_COUNT + index)
              }}
            />
            <IconButton
              size="medium"
              icon="FeatherArrowRight"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                window.open(source.link, '_blank')  
              }}
            /> 
          </div>
          </Table.Cell>
      </Table.Row>
    )
  }

  return (
    <DefaultPageLayout>
      <div className="container max-w-none flex h-full w-full flex-col items-start gap-12 bg-default-background py-12 overflow-auto">
        {/* <div className="flex w-full flex-col items-start gap-6">
          <span className="w-full text-heading-2 font-heading-2 text-default-font">
            Baseline Model Data
          </span>
        </div> */}
        <div className="flex w-full flex-col items-start gap-6">
          <span className="w-full text-heading-2 font-heading-2 text-default-font">
            { location.pathname.includes('static') ? "Static Data" : "Internet Data" }
          </span>
           <div className="flex w-full items-center justify-between">
            <TextField label="" helpText="" icon="FeatherSearch" className="h-auto w-96 flex-none">
              <TextField.Input
                placeholder="Search"
                value={text}
                onChange={(event: React.ChangeEvent<HTMLInputElement>) => setText(event.target.value)}
              />
            </TextField>
             <VectorDBUpdate setDocuments={setStaticData} />
          </div>
          <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <Table
              header={
                <Table.HeaderRow>
                  <Table.HeaderCell className="h-auto w-auto flex-none">
                    ID
                  </Table.HeaderCell>
                  <Table.HeaderCell>Doccument Name</Table.HeaderCell>
                </Table.HeaderRow>
              }
            >
              { displayData() }  
            </Table>
            <div className="flex w-full items-center justify-center gap-1 px-4 py-4">
              {/* << first / prev */}
              <div className="flex items-center justify-center gap-1">
                <IconButton
                  disabled={currentPageIndex === 0}
                  size="medium"
                  icon="FeatherChevronFirst"
                  onClick={() => setCurrentPageIndex(0)}
                />
                <IconButton
                  disabled={currentPageIndex === 0}
                  size="medium"
                  icon="FeatherChevronLeft"
                  onClick={() => setCurrentPageIndex(currentPageIndex - 1)}
                />
              </div>

              {/* numbered buttons */}
              <div className="flex items-center justify-center gap-1">
                {pageButtons.map((p, idx) =>
                  p === 'ellipsis' ? (
                    <span key={`ellipsis-${idx}`} className="px-2 select-none">
                      …
                    </span>
                  ) : (
                    <Button
                      key={p}
                      variant={p === currentPageIndex ? 'brand-secondary' : 'neutral-tertiary'}
                      size="medium"
                      onClick={() => setCurrentPageIndex(p)}
                    >
                      {p + 1}
                    </Button>
                  )
                )}
              </div>

              {/* next / last >> */}
              <div className="flex items-center justify-center gap-1">
                <IconButton
                  disabled={currentPageIndex + 1 >= totalPages}
                  size="medium"
                  icon="FeatherChevronRight"
                  onClick={() => setCurrentPageIndex(currentPageIndex + 1)}
                />
                <IconButton
                  disabled={currentPageIndex + 1 >= totalPages}
                  size="medium"
                  icon="FeatherChevronLast"
                  onClick={() => setCurrentPageIndex(totalPages - 1)}  
                />
              </div>
            </div>
          </div>
        </div>
      </div>
      <ConfirmDeleteDocument 
        open={openConfirmDeleteDocument} 
        setOpen={setOpenConfirmDeleteDocument}
        documents={staticData}
        setDocuments={setStaticData}
        deleteIndex={deleteIndex}  
      />
    </DefaultPageLayout>
  );
}

export default DataView;