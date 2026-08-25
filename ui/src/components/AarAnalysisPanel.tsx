"use client";

import React, { useState, useRef, useEffect } from "react";
import * as SubframeCore from "@subframe/core";
import { IconButton } from "../subframe/components/IconButton";
import { Table } from "../subframe/components/Table";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { TextField } from "../subframe/components/TextField";
import { LinkButton } from "../subframe/components/LinkButton";

interface AAR {
  id: number;
  name: string;
  description: string | null;
  event_id: string | null;
  created_at: string;
  updated_at: string;
  contributor_name?: string | null;
}

interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  message: string;
}

interface AarAnalysisPanelProps {
  aars: AAR[];
  projectId: string;
}

const EXAMPLE_PROMPTS = [
  "What were the biggest decision bottlenecks?",
  "Where did assumptions break down?",
  "Summarize communications challenges",
  "What lessons learned emerged?",
];

/** Local WebSocket URL for AAR (same as AarModule) */
const getWsUrl = (): string => {
  if (typeof window === "undefined") return "ws://localhost:8000/ws/aar";
  const host = window.location.hostname === "localhost" ? "localhost:8000" : window.location.host;
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${host}/ws/aar`;
};

export default function AarAnalysisPanel({ aars, projectId }: AarAnalysisPanelProps) {
  const [selectedAarIds, setSelectedAarIds] = useState<Set<number>>(new Set());
  const [question, setQuestion] = useState("");
  const [isLoading, setIsLoading] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 6;
  const chatContainerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WebSocket | null>(null);

  const isTextObservation = (aar: AAR) => !!aar.description?.trim();

  const totalPages = Math.ceil(aars.length / itemsPerPage);
  const paginatedAars = aars.slice((currentPage - 1) * itemsPerPage, currentPage * itemsPerPage);
  const startItem = aars.length > 0 ? (currentPage - 1) * itemsPerPage + 1 : 0;
  const endItem = Math.min(currentPage * itemsPerPage, aars.length);

  useEffect(() => {
    if (chatContainerRef.current) {
      chatContainerRef.current.scrollTop = chatContainerRef.current.scrollHeight;
    }
  }, [chatMessages]);

  useEffect(() => {
    return () => {
      wsRef.current?.close();
    };
  }, []);

  const toggleAarSelection = (aarId: number) => {
    setSelectedAarIds((prev) => {
      const newSet = new Set(prev);
      if (newSet.has(aarId)) newSet.delete(aarId);
      else newSet.add(aarId);
      return newSet;
    });
  };

  const clearAllSelections = () => setSelectedAarIds(new Set());

  const fetchAarContent = async (aarIds: number[]): Promise<string> => {
    const contents: string[] = [];
    for (const aarId of aarIds) {
      const aar = aars.find((a) => a.id === aarId);
      if (!aar) continue;
      const contributorInfo = aar.contributor_name || "Unknown";
      try {
        if (isTextObservation(aar)) {
          const text = aar.description?.trim() || "";
          if (text) {
            contents.push(`\n--- ${aar.name} (Text Observation by ${contributorInfo}) ---\n${text}`);
          }
        } else {
          const response = await fetch(`/api/aar/${aarId}/transcript`);
          if (response.ok) {
            const transcriptData = await response.json();
            if (transcriptData.length > 0) {
              const transcriptText = transcriptData
                .map((entry: { timestamp: string; speaker: string; text: string }) =>
                  `[${entry.timestamp}] ${entry.speaker}: ${entry.text}`
                )
                .join("\n");
              contents.push(`\n--- ${aar.name} (Audio Recording by ${contributorInfo}) ---\n${transcriptText}`);
            }
          }
        }
      } catch (err) {
        console.error(`Error fetching content for AAR ${aarId}:`, err);
      }
    }
    return contents.join("\n\n");
  };

  const handleAnalyze = async (customQuestion?: string) => {
    const questionToUse = customQuestion ?? question;
    if (!questionToUse.trim() || selectedAarIds.size === 0) {
      setError("Please select at least one lesson learned input and enter a question.");
      return;
    }
    setIsLoading(true);
    setError(null);

    const userMsg: ChatMessage = {
      id: `user-${Date.now()}`,
      role: "user",
      message: questionToUse,
    };
    setChatMessages((prev) => [...prev, userMsg]);
    setQuestion("");

    try {
      const combinedContent = await fetchAarContent(Array.from(selectedAarIds));
      if (!combinedContent.trim()) {
        setError("No content available in the selected lesson learned inputs.");
        setIsLoading(false);
        return;
      }

      const wsUrl = getWsUrl();
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        ws.send(
          JSON.stringify({
            action: "analyze",
            question: questionToUse,
            content: combinedContent,
            selected_count: selectedAarIds.size,
          })
        );
      };

      ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.status === "processing") return;
        if (data.status === "success" && data.response) {
          setChatMessages((prev) => [
            ...prev,
            { id: `ai-${Date.now()}`, role: "assistant", message: data.response },
          ]);
        } else if (data.status === "error") {
          setError(data.error || "Failed to analyze lesson learned inputs");
        }
        setIsLoading(false);
        ws.close();
      };

      ws.onerror = () => {
        setError("WebSocket connection failed. Please try again.");
        setIsLoading(false);
      };

      ws.onclose = () => {
        wsRef.current = null;
      };
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
      setIsLoading(false);
    }
  };

  const handleExamplePromptClick = (prompt: string) => {
    if (selectedAarIds.size === 0) {
      setError("Please select at least one lesson learned input first.");
      return;
    }
    handleAnalyze(prompt);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleAnalyze();
    }
  };

  const clearChat = () => {
    setChatMessages([]);
    setError(null);
  };

  return (
    <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-6 py-6">
      <div className="flex w-full items-center justify-between">
        <div className="flex items-center gap-3">
          <SubframeCore.Icon
            className="text-heading-3 font-heading-3 text-brand-primary"
            name="FeatherMessageSquare"
          />
          <span className="text-heading-3 font-heading-3 text-default-font">
            AI-Powered Lesson Learned Analysis
          </span>
        </div>
        {chatMessages.length > 0 && (
          <LinkButton variant="neutral" size="small" onClick={clearChat}>
            Clear conversation
          </LinkButton>
        )}
      </div>

      <div className="flex w-full items-start gap-6">
        <div className="flex w-80 flex-none flex-col items-start gap-3">
          <span className="text-caption-bold font-caption-bold text-subtext-color">
            SELECT INPUTS TO ANALYZE
          </span>

          {aars.length === 0 ? (
            <div className="flex w-full items-center justify-center py-4 rounded-md bg-neutral-50">
              <span className="text-caption font-caption text-subtext-color">
                No lesson learned inputs available
              </span>
            </div>
          ) : (
            <>
              <div className="flex w-full max-h-56 overflow-y-auto rounded-md border border-solid border-neutral-200">
                <Table
                  header={
                    <Table.HeaderRow>
                      <Table.HeaderCell />
                      <Table.HeaderCell>Input</Table.HeaderCell>
                    </Table.HeaderRow>
                  }
                >
                  {paginatedAars.map((aar) => (
                    <Table.Row
                      key={aar.id}
                      clickable
                      onClick={() => toggleAarSelection(aar.id)}
                    >
                      <Table.Cell>
                        <SubframeCore.Icon
                          className={`text-body font-body ${
                            selectedAarIds.has(aar.id) ? "text-brand-primary" : "text-neutral-400"
                          }`}
                          name={selectedAarIds.has(aar.id) ? "FeatherCheckSquare" : "FeatherSquare"}
                        />
                      </Table.Cell>
                      <Table.Cell>
                        <div className="flex items-center gap-2">
                          <IconWithBackground
                            variant={isTextObservation(aar) ? "neutral" : "brand"}
                            size="x-small"
                            icon={isTextObservation(aar) ? "FeatherFileText" : "FeatherMic"}
                          />
                          <span className="text-caption font-caption text-default-font truncate max-w-[180px]">
                            {aar.name}
                          </span>
                        </div>
                      </Table.Cell>
                    </Table.Row>
                  ))}
                </Table>
              </div>

              <div className="flex w-full items-center justify-between">
                <span className="text-caption font-caption text-subtext-color">
                  {startItem}-{endItem} of {aars.length}
                </span>
                <div className="flex items-center gap-1">
                  <IconButton
                    disabled={currentPage === 1}
                    variant="neutral-tertiary"
                    size="small"
                    icon="FeatherChevronLeft"
                    onClick={() => setCurrentPage((p) => Math.max(1, p - 1))}
                  />
                  <IconButton
                    disabled={currentPage >= totalPages}
                    variant="neutral-tertiary"
                    size="small"
                    icon="FeatherChevronRight"
                    onClick={() => setCurrentPage((p) => Math.min(totalPages, p + 1))}
                  />
                </div>
              </div>

              {selectedAarIds.size > 0 && (
                <div className="flex w-full items-center justify-between rounded-md border border-solid border-brand-600 bg-brand-50 px-3 py-2">
                  <span className="text-caption-bold font-caption-bold text-brand-600">
                    {selectedAarIds.size} selected
                  </span>
                  <LinkButton variant="brand" size="small" onClick={clearAllSelections}>
                    Clear
                  </LinkButton>
                </div>
              )}
            </>
          )}
        </div>

        <div className="flex grow flex-col items-start gap-4">
          {error && (
            <div className="flex w-full items-center gap-2 rounded-md bg-error-50 px-3 py-2">
              <SubframeCore.Icon className="text-error-600" name="FeatherAlertCircle" />
              <span className="text-caption font-caption text-error-600 grow">{error}</span>
              <IconButton
                variant="neutral-tertiary"
                size="small"
                icon="FeatherX"
                onClick={() => setError(null)}
              />
            </div>
          )}

          {chatMessages.length > 0 || isLoading ? (
            <div
              ref={chatContainerRef}
              className="flex w-full max-h-64 flex-col items-start gap-4 overflow-y-auto"
            >
              {chatMessages.map((msg) => (
                <div key={msg.id} className="flex w-full items-start gap-4">
                  {msg.role === "assistant" && (
                    <img
                      className="h-8 w-7 flex-none object-cover"
                      src="/jlyuhbewnu0gdyipbb6e.png"
                      alt=""
                    />
                  )}
                  <div
                    className={`flex grow shrink-0 basis-0 flex-col items-start gap-2 rounded-md px-4 py-4 ${
                      msg.role === "user" ? "bg-brand-50 ml-auto max-w-[80%]" : "bg-neutral-50"
                    }`}
                  >
                    <span className="text-body-bold font-body-bold text-brand-primary">
                      {msg.role === "user" ? "You" : "Analysis Agent"}
                    </span>
                    <div className="whitespace-pre-wrap text-body font-body text-default-font">
                      {msg.message}
                    </div>
                  </div>
                </div>
              ))}
              {isLoading && (
                <div className="flex w-full items-start gap-4">
                  <img
                    className="h-8 w-7 flex-none object-cover"
                    src="/jlyuhbewnu0gdyipbb6e.png"
                    alt=""
                  />
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2 rounded-md bg-neutral-50 px-4 py-4">
                    <span className="text-body-bold font-body-bold text-brand-primary">
                      Analysis Agent
                    </span>
                    <span className="whitespace-pre-wrap text-body font-body text-default-font">
                      Thinking...
                    </span>
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex w-full flex-col items-start gap-2">
              <span className="text-caption-bold font-caption-bold text-subtext-color">
                EXAMPLE PROMPTS
              </span>
              <div className="flex w-full flex-wrap gap-2">
                {EXAMPLE_PROMPTS.map((prompt, idx) => (
                  <div
                    key={idx}
                    className="flex items-center gap-2 rounded-md border border-solid border-neutral-300 bg-neutral-100 px-3 py-2 cursor-pointer hover:bg-neutral-200 transition-colors"
                    onClick={() => handleExamplePromptClick(prompt)}
                  >
                    <SubframeCore.Icon
                      className="text-caption font-caption text-subtext-color"
                      name="FeatherZap"
                    />
                    <span className="text-caption font-caption text-default-font">{prompt}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div className="flex h-14 w-full flex-none items-center justify-center gap-2 rounded-md bg-neutral-100 px-2 py-2">
            <TextField className="h-auto grow shrink-0 basis-0">
              <TextField.Input
                placeholder="Ask a question about selected inputs..."
                value={question}
                onChange={(e: React.ChangeEvent<HTMLInputElement>) => setQuestion(e.target.value)}
                onKeyDown={handleKeyDown}
                disabled={isLoading}
              />
            </TextField>
            <IconButton
              disabled={isLoading || selectedAarIds.size === 0 || !question.trim()}
              size="large"
              icon="FeatherArrowRight"
              onClick={() => handleAnalyze()}
            />
          </div>
        </div>
      </div>
    </div>
  );
}
