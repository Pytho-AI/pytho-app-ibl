"use client";

import React, { useEffect, useMemo, useState } from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { Table } from "../subframe/components/Table";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { TextField } from "../subframe/components/TextField";
import { TextArea } from "../subframe/components/TextArea";
import { Checkbox } from "../subframe/components/Checkbox";
import { Badge } from "../subframe/components/Badge";

const PAGE_ITEMS_COUNT = 10;

interface DoctrineDoc {
  title: string;
  doc_id: string;
  link: string;
  chunks_count?: number;
}

interface Citation {
  doc_id: string;
  title?: string;
  page?: number | null;
  link?: string;
  snippet: string;
}

type Message =
  | { role: "user"; content: string }
  | { role: "assistant"; content: string; citations: Citation[] };

// Open a doctrine PDF at the cited page. The link is already a relative
// /files/... path normalized by the backend, so the browser resolves it
// against the current host (through nginx to the backend).
function openDoctrinePdf(link?: string, page?: number | null) {
  if (!link) return;
  const url = page != null ? `${link}#page=${page}` : link;
  window.open(url, "_blank", "noopener");
}

function Doctrines() {
  const [currentPageIndex, setCurrentPageIndex] = useState(0);
  const [doctrines, setDoctrines] = useState<DoctrineDoc[]>([]);
  const sources = doctrines;

  const totalPages = Math.ceil(sources.length / PAGE_ITEMS_COUNT);

  const [text, setText] = useState("");
  const [debouncedText, setDebouncedText] = useState("");

  // Selection: doc_id -> doc. Keyed (not index-based) so it survives search
  // re-fetches and pagination. selected_doc_ids for the chat = Object.keys.
  const [selected, setSelected] = useState<Record<string, DoctrineDoc>>({});
  const selectedIds = useMemo(() => Object.keys(selected), [selected]);
  const selectedDocs = useMemo(() => Object.values(selected), [selected]);
  const selectedCount = selectedIds.length;

  // Chat state
  const [chatOpen, setChatOpen] = useState(false);
  const [chatInput, setChatInput] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [pending, setPending] = useState(false);

  // Debounce the search box 500ms after the user stops typing.
  useEffect(() => {
    const handler = setTimeout(() => setDebouncedText(text), 500);
    return () => clearTimeout(handler);
  }, [text]);

  useEffect(() => {
    queryDoctrines(debouncedText);
    setCurrentPageIndex(0);
  }, [debouncedText]);

  const pageButtons: (number | "ellipsis")[] = useMemo(() => {
    if (totalPages <= 10) {
      return Array.from({ length: totalPages }, (_, i) => i);
    }
    return [...Array.from({ length: 4 }, (_, i) => i), "ellipsis", totalPages - 1];
  }, [totalPages]);

  async function queryDoctrines(queryText: string) {
    const response = await fetch("/api/query_doctrines", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query: queryText }),
    });
    if (!response.ok) throw new Error(`Error ${response.status}`);
    const data = await response.json();
    setDoctrines(Array.isArray(data) ? data : []);
  }

  useEffect(() => {
    queryDoctrines("");
  }, []);

  const toggleDoc = (doc: DoctrineDoc) => {
    setSelected((prev) => {
      const next = { ...prev };
      if (next[doc.doc_id]) delete next[doc.doc_id];
      else next[doc.doc_id] = doc;
      return next;
    });
  };

  const submitAsk = async () => {
    const question = chatInput.trim();
    if (!question || pending) return;
    setMessages((prev) => [...prev, { role: "user", content: question }]);
    setChatInput("");
    setPending(true);
    try {
      const response = await fetch("/api/doctrine/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, selected_doc_ids: selectedIds }),
      });
      if (!response.ok) throw new Error(`Error ${response.status}`);
      const data: { answer: string; citations?: Citation[] } = await response.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: data.answer, citations: data.citations ?? [] },
      ]);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setMessages((prev) => [
        ...prev,
        { role: "assistant", content: `Error: ${msg}`, citations: [] },
      ]);
    } finally {
      setPending(false);
    }
  };

  const pageDocs = sources.slice(
    currentPageIndex * PAGE_ITEMS_COUNT,
    (currentPageIndex + 1) * PAGE_ITEMS_COUNT,
  );

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full items-stretch gap-4 overflow-hidden bg-default-background">
        {/* Left: doctrine browser */}
        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6 px-8 py-8 overflow-auto">
          <div className="flex w-full items-center gap-4">
            <span className="text-heading-2 font-heading-2 text-default-font">
              Doctrines
            </span>
            <span className="text-body font-body text-subtext-color">
              {sources.length} publications
            </span>
            <div className="grow" />
            <Button
              variant={chatOpen ? "neutral-secondary" : "brand-primary"}
              icon="FeatherMessageSquare"
              onClick={() => setChatOpen((v) => !v)}
            >
              {chatOpen
                ? "Close chat"
                : selectedCount > 0
                  ? `Chat (${selectedCount})`
                  : "Open chat"}
            </Button>
          </div>

          <span className="text-body font-body text-subtext-color">
            Curated doctrinal reference library. Search, then check publications to
            focus the chat — or ask against the whole library.
          </span>

          <TextField label="" helpText="" icon="FeatherSearch" className="h-auto w-96 flex-none">
            <TextField.Input
              placeholder="Search doctrine..."
              value={text}
              onChange={(event: React.ChangeEvent<HTMLInputElement>) => setText(event.target.value)}
            />
          </TextField>

          {/* Selection chip strip */}
          {selectedCount > 0 && (
            <div className="flex w-full flex-col gap-2 rounded-md border border-solid border-neutral-border bg-neutral-50 px-3 py-2">
              <div className="flex w-full items-center justify-between">
                <span className="text-caption font-caption text-subtext-color">
                  {selectedCount} selected
                </span>
                <button
                  type="button"
                  onClick={() => setSelected({})}
                  className="cursor-pointer text-caption-bold font-caption-bold text-error-700 hover:underline"
                >
                  Clear all
                </button>
              </div>
              <div className="flex w-full flex-wrap items-center gap-1.5">
                {selectedDocs.map((doc) => (
                  <button
                    key={doc.doc_id}
                    type="button"
                    onClick={() => toggleDoc(doc)}
                    title={`${doc.title} • Click to remove`}
                    className="inline-flex"
                  >
                    <Badge variant="brand" iconRight="FeatherX">
                      {doc.title}
                    </Badge>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <Table
              header={
                <Table.HeaderRow>
                  <Table.HeaderCell className="w-10 flex-none" />
                  <Table.HeaderCell className="w-12 flex-none">#</Table.HeaderCell>
                  <Table.HeaderCell>Document Name</Table.HeaderCell>
                  <Table.HeaderCell className="w-12 flex-none" />
                </Table.HeaderRow>
              }
            >
              {pageDocs.map((doc, index) => (
                <Table.Row key={doc.doc_id || index}>
                  <Table.Cell className="w-10 flex-none">
                    <Checkbox
                      checked={!!selected[doc.doc_id]}
                      onCheckedChange={() => toggleDoc(doc)}
                    />
                  </Table.Cell>
                  <Table.Cell className="h-12 w-12 flex-none">
                    <span className="whitespace-nowrap text-body font-body text-neutral-500">
                      {currentPageIndex * PAGE_ITEMS_COUNT + index + 1}
                    </span>
                  </Table.Cell>
                  <Table.Cell>
                    <span className="text-body-bold font-body-bold text-default-font break-words">
                      {doc.title}
                    </span>
                  </Table.Cell>
                  <Table.Cell className="w-12 flex-none">
                    <IconButton
                      size="medium"
                      icon="FeatherExternalLink"
                      onClick={() => openDoctrinePdf(doc.link)}
                    />
                  </Table.Cell>
                </Table.Row>
              ))}
              {pageDocs.length === 0 && (
                <Table.Row>
                  <Table.Cell />
                  <Table.Cell />
                  <Table.Cell>
                    <span className="text-body font-body text-subtext-color">
                      No doctrine matches "{text}".
                    </span>
                  </Table.Cell>
                  <Table.Cell />
                </Table.Row>
              )}
            </Table>

            {totalPages > 1 && (
              <div className="flex w-full items-center justify-center gap-1 px-4 py-4">
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
                <div className="flex items-center justify-center gap-1">
                  {pageButtons.map((p, idx) =>
                    p === "ellipsis" ? (
                      <span key={`ellipsis-${idx}`} className="select-none px-2">
                        …
                      </span>
                    ) : (
                      <Button
                        key={p}
                        variant={p === currentPageIndex ? "brand-secondary" : "neutral-tertiary"}
                        size="medium"
                        onClick={() => setCurrentPageIndex(p)}
                      >
                        {String(p + 1)}
                      </Button>
                    ),
                  )}
                </div>
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
            )}
          </div>
        </div>

        {/* Right: chat panel */}
        {chatOpen && (
          <ChatPanel
            selectedCount={selectedCount}
            messages={messages}
            input={chatInput}
            setInput={setChatInput}
            onSubmit={submitAsk}
            pending={pending}
            onClose={() => setChatOpen(false)}
          />
        )}
      </div>
    </DefaultPageLayout>
  );
}

// ── Chat panel ──

interface ChatPanelProps {
  selectedCount: number;
  messages: Message[];
  input: string;
  setInput: (v: string) => void;
  onSubmit: () => void;
  pending: boolean;
  onClose: () => void;
}

function ChatPanel({ selectedCount, messages, input, setInput, onSubmit, pending, onClose }: ChatPanelProps) {
  const scoped = selectedCount > 0;
  const empty = messages.length === 0;
  return (
    <div className="flex w-[460px] flex-none flex-col border-l border-solid border-neutral-border bg-default-background">
      <div className="flex items-center gap-2 border-b border-solid border-neutral-border px-4 py-3">
        <span className="text-heading-3 font-heading-3 text-default-font">Ask Doctrine</span>
        {scoped ? (
          <Badge variant="brand">{`${selectedCount} selected`}</Badge>
        ) : (
          <Badge variant="neutral">All doctrine</Badge>
        )}
        <div className="grow" />
        <IconButton icon="FeatherX" onClick={onClose} />
      </div>

      <div className="flex grow shrink-0 basis-0 flex-col gap-4 overflow-auto px-4 py-4">
        {empty ? (
          <div className="m-auto flex flex-col items-center gap-3 text-center">
            <span className="text-heading-3 font-heading-3 text-default-font">
              Chat with your doctrine
            </span>
            <span className="text-body font-body text-subtext-color">
              Ask a question against the whole library, or check specific
              publications to focus the answer.
            </span>
          </div>
        ) : (
          messages.map((m, i) =>
            m.role === "user" ? (
              <UserMessage key={i} text={m.content} />
            ) : (
              <AssistantMessage key={i} text={m.content} citations={m.citations} />
            ),
          )
        )}
        {pending && (
          <div className="flex items-center gap-2 text-subtext-color">
            <span className="text-caption font-caption">Thinking…</span>
          </div>
        )}
      </div>

      <div className="flex flex-col gap-2 border-t border-solid border-neutral-border px-4 py-3">
        <TextArea className="w-full" label="" helpText="">
          <TextArea.Input
            placeholder="Ask a question…  (Enter to send)"
            value={input}
            onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setInput(e.target.value)}
            onKeyDown={(e: React.KeyboardEvent<HTMLTextAreaElement>) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                onSubmit();
              }
            }}
            rows={3}
          />
        </TextArea>
        <div className="flex items-center justify-end">
          <Button
            variant="brand-primary"
            disabled={pending || !input.trim()}
            icon="FeatherSend"
            onClick={onSubmit}
          >
            Ask
          </Button>
        </div>
      </div>
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-lg bg-brand-600 px-3 py-2">
        <span className="whitespace-pre-wrap text-body font-body text-white">{text}</span>
      </div>
    </div>
  );
}

function AssistantMessage({ text, citations }: { text: string; citations: Citation[] }) {
  // Citations come back deduped by (doc_id, page) from the backend.
  return (
    <div className="flex max-w-[95%] flex-col gap-2 self-start">
      <div className="rounded-lg border border-solid border-neutral-border bg-neutral-50 px-3 py-2">
        <span className="whitespace-pre-wrap text-body font-body text-default-font">{text}</span>
      </div>
      {citations.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {citations.map((c, i) => (
            <button
              key={i}
              type="button"
              onClick={() => openDoctrinePdf(c.link, c.page)}
              className="flex items-center gap-1 rounded border border-solid border-neutral-border bg-default-background px-2 py-0.5 hover:bg-brand-50 cursor-pointer"
              title={c.snippet}
            >
              <span className="text-caption-bold font-caption-bold text-brand-700">
                {c.title || c.doc_id}
              </span>
              {c.page != null && (
                <span className="text-caption font-caption text-subtext-color">
                  p.{c.page}
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default Doctrines;
