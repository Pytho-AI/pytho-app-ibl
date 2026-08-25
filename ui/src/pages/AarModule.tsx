"use client";

import React, { useState, useRef, useEffect } from "react";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { TextField } from "../subframe/components/TextField";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import * as SubframeCore from "@subframe/core";
import { useParams, useLocation } from "react-router-dom";

interface TranscriptEntry {
  id?: number;
  speaker: string;
  timestamp: string;
  text: string;
  audio_duration?: number;
  created_at?: string;
}

interface ChatMessage {
  id?: number;
  role: 'user' | 'assistant';
  message: string;
  type: 'message' | 'summary';
  created_at?: string;
}

function AarModule() {
  const { aarId } = useParams()
  const location = useLocation();
  const locationState = location.state as {
    aarName?: string;
    projectId?: string;
    fromLessonLearnedDashboard?: boolean;
  } | null;
  const wsRef = useRef<WebSocket | null>(null);
  const [isRecording, setIsRecording] = useState(false);
  const [recordingTime, setRecordingTime] = useState(0);
  const [transcriptEntries, setTranscriptEntries] = useState<TranscriptEntry[]>([]);
  const [summary, setSummary] = useState<string>("");
  const [isGeneratingSummary, setIsGeneratingSummary] = useState(false);
  const [chatMessages, setChatMessages] = useState<ChatMessage[]>([]);
  const [chatInput, setChatInput] = useState("");
  const [isSendingChat, setIsSendingChat] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  const audioContextRef = useRef<AudioContext | null>(null);
  const processorRef = useRef<ScriptProcessorNode | null>(null);
  const audioStreamRef = useRef<MediaStream | null>(null);
  const recordingBufferRef = useRef<Float32Array[]>([]);
  const intervalRef = useRef<NodeJS.Timeout | null>(null);
  const timeIntervalRef = useRef<NodeJS.Timeout | null>(null);
  const recordingStartTimeRef = useRef<number>(0);
  const chatScrollRef = useRef<HTMLDivElement>(null);
  const BACKEND_URL = `/api/transcribe`;
  const CHUNK_DURATION = 10000;
  const SAMPLE_RATE = 16000;

  // Auto-scroll chat to bottom
  useEffect(() => {
    if (chatScrollRef.current) {
      chatScrollRef.current.scrollTop = chatScrollRef.current.scrollHeight;
    }
  }, [chatMessages, isSendingChat, isGeneratingSummary]);

  // Load existing data when component mounts
  useEffect(() => {
    if (aarId) {
      loadExistingData();
    }
  }, [aarId]);

  const loadExistingData = async () => {
    setIsLoading(true);
    try {
      console.log('Loading existing data for lesson learned #', aarId);
      await Promise.all([
        loadTranscriptHistory(),
        loadChatHistory()
      ]);
      console.log('Data loaded successfully');
    } catch (err) {
      console.error('Error loading data:', err);
    } finally {
      setIsLoading(false);
    }
  };

  const loadTranscriptHistory = async () => {
    try {
      const response = await fetch(`/api/aar/${aarId}/transcript`);
      if (response.ok) {
        const transcripts = await response.json();
        setTranscriptEntries(transcripts);
        console.log('Loaded transcript history:', transcripts.length, 'entries');
      }
    } catch (err) {
      console.error('Error loading transcript history:', err);
    }
  };

  const loadChatHistory = async () => {
    try {
      const response = await fetch(`/api/aar/${aarId}/chat`);
      if (response.ok) {
        const messages = await response.json();
        setChatMessages(messages);
        console.log('Loaded chat history:', messages.length, 'messages');
      }
    } catch (err) {
      console.error('Error loading chat history:', err);
    }
  };

  const saveTranscriptEntry = async (entry: TranscriptEntry) => {
    try {
      const response = await fetch(`/api/aar/${aarId}/transcript`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          speaker: entry.speaker,
          timestamp: entry.timestamp,
          text: entry.text,
          audio_duration: recordingTime,
        }),
      });

      if (response.ok) {
        const savedEntry = await response.json();
        console.log('Transcript entry saved to database');
        return savedEntry;
      } else {
        console.error('Failed to save transcript entry');
      }
    } catch (err) {
      console.error('Error saving transcript entry:', err);
    }
  };

  const saveChatMessage = async (
    message: string,
    role: 'user' | 'assistant',
    type: 'message' | 'summary' = 'message'
  ) => {
    try {
      const response = await fetch(`/api/aar/${aarId}/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          message,
          role,
          type,
        }),
      });

      if (response.ok) {
        const savedMessage = await response.json();
        console.log('Chat message saved:', savedMessage.id);
        return savedMessage;
      } else {
        console.error('Failed to save chat message');
      }
    } catch (err) {
      console.error('Error saving chat message:', err);
    }
  };

  // Initialize WebSocket
  useEffect(() => {
    wsRef.current = new WebSocket('ws://localhost:8000/ws/aar');

    wsRef.current.onopen = () => {
      console.log('WebSocket connected');
    };

    wsRef.current.onmessage = async (event) => {
      const data = JSON.parse(event.data);
      const action = data.action;
      console.log('← Received action:', action, 'status:', data.status);

      if (action === 'summary') {
        if (data.status === 'success') {
          const summaryText = data.summary;
          setSummary(summaryText);

          const savedMessage = await saveChatMessage(summaryText, 'assistant', 'summary');

          if (savedMessage) {
            setChatMessages(prev => [...prev, savedMessage]);
          }

          console.log('Summary saved to database');
          setIsGeneratingSummary(false);
        } else if (data.status === 'processing') {
          console.log('Processing summary:', data.message);
        }
      } else if (action === 'chat') {
        if (data.status === 'success') {
          const assistantMessage = data.response;

          const savedMessage = await saveChatMessage(assistantMessage, 'assistant', 'message');

          if (savedMessage) {
            setChatMessages(prev => [...prev, savedMessage]);
          }

          console.log('Chat response saved to database');
          setIsSendingChat(false);
        } else if (data.status === 'processing') {
          console.log('Processing chat:', data.message);
        }
      }

      if (data.status === 'error') {
        console.error('WebSocket error:', data.error);
        alert(`Error: ${data.error}`);
        setIsGeneratingSummary(false);
        setIsSendingChat(false);
      }
    };

    wsRef.current.onerror = (error) => {
      console.error('WebSocket error:', error);
      setIsGeneratingSummary(false);
      setIsSendingChat(false);
    };

    wsRef.current.onclose = () => {
      console.log('WebSocket closed');
      setIsGeneratingSummary(false);
      setIsSendingChat(false);
    };

    return () => {
      wsRef.current?.close();
    };
  }, [aarId]);

  const bufferToWav = (buffer: Float32Array, sampleRate: number): Blob => {
    const numChannels = 1;
    const length = buffer.length * numChannels * 2;
    const arrayBuffer = new ArrayBuffer(44 + length);
    const view = new DataView(arrayBuffer);

    const writeString = (offset: number, string: string) => {
      for (let i = 0; i < string.length; i++) {
        view.setUint8(offset + i, string.charCodeAt(i));
      }
    };

    writeString(0, 'RIFF');
    view.setUint32(4, 36 + length, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, numChannels, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * numChannels * 2, true);
    view.setUint16(32, numChannels * 2, true);
    view.setUint16(34, 16, true);
    writeString(36, 'data');
    view.setUint32(40, length, true);

    const offset = 44;
    for (let i = 0; i < buffer.length; i++) {
      const sample = Math.max(-1, Math.min(1, buffer[i]));
      view.setInt16(offset + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7FFF, true);
    }

    return new Blob([arrayBuffer], { type: 'audio/wav' });
  };

  const formatTime = (seconds: number): string => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const sendToBackend = async (wavBlob: Blob) => {
    try {
      console.log('Sending audio chunk to backend...');

      const formData = new FormData();
      formData.append('audio', wavBlob, 'chunk.wav');

      const response = await fetch(BACKEND_URL, {
        method: 'POST',
        body: formData
      });

      if (response.ok) {
        const data = await response.json();

        const currentTime = Math.floor((Date.now() - recordingStartTimeRef.current) / 1000);
        const newEntry: TranscriptEntry = {
          speaker: "Speaker",
          timestamp: formatTime(currentTime),
          text: data.transcription
        };

        const savedEntry = await saveTranscriptEntry(newEntry);

        if (savedEntry) {
          setTranscriptEntries(prev => [...prev, savedEntry]);
        }

        console.log('Transcription received and saved');
      } else {
        console.error('Backend error:', response.status);
      }
    } catch (err) {
      console.error('Send failed:', err);
    }
  };

  const processAndSendChunk = () => {
    if (recordingBufferRef.current.length === 0) return;

    const totalLength = recordingBufferRef.current.reduce((acc, buf) => acc + buf.length, 0);
    const combinedBuffer = new Float32Array(totalLength);
    let offset = 0;

    for (const buffer of recordingBufferRef.current) {
      combinedBuffer.set(buffer, offset);
      offset += buffer.length;
    }

    const wavBlob = bufferToWav(combinedBuffer, SAMPLE_RATE);
    sendToBackend(wavBlob);
    recordingBufferRef.current = [];
  };

  const startRecording = async () => {
    try {
      console.log('Requesting microphone access...');

      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          sampleRate: SAMPLE_RATE
        }
      });

      audioStreamRef.current = stream;
      recordingStartTimeRef.current = Date.now();
      console.log('Microphone access granted');

      audioContextRef.current = new (window.AudioContext || (window as any).webkitAudioContext)({
        sampleRate: SAMPLE_RATE
      });

      const source = audioContextRef.current.createMediaStreamSource(stream);
      const bufferSize = 4096;
      const processor = audioContextRef.current.createScriptProcessor(bufferSize, 1, 1);
      processorRef.current = processor;

      processor.onaudioprocess = (e) => {
        const inputData = e.inputBuffer.getChannelData(0);
        const buffer = new Float32Array(inputData);
        recordingBufferRef.current.push(buffer);
      };

      source.connect(processor);
      processor.connect(audioContextRef.current.destination);

      intervalRef.current = setInterval(() => {
        processAndSendChunk();
      }, CHUNK_DURATION);

      timeIntervalRef.current = setInterval(() => {
        setRecordingTime(prev => prev + 1);
      }, 1000);

      setIsRecording(true);
      setRecordingTime(0);
      console.log('Recording started');

    } catch (err) {
      console.error('Failed to start recording:', err);
      alert('Failed to access microphone. Please check permissions.');
    }
  };

  const stopRecording = () => {
    processAndSendChunk();

    if (intervalRef.current) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }

    if (timeIntervalRef.current) {
      clearInterval(timeIntervalRef.current);
      timeIntervalRef.current = null;
    }

    if (processorRef.current) {
      processorRef.current.disconnect();
      processorRef.current = null;
    }

    if (audioContextRef.current) {
      audioContextRef.current.close();
      audioContextRef.current = null;
    }

    if (audioStreamRef.current) {
      audioStreamRef.current.getTracks().forEach(track => track.stop());
      audioStreamRef.current = null;
    }

    recordingBufferRef.current = [];
    setIsRecording(false);
    console.log('Recording stopped');
  };

  const downloadTranscript = () => {
    const transcript = transcriptEntries
      .map(entry => `[${entry.timestamp}] ${entry.speaker}: ${entry.text}`)
      .join('\n\n');

    const blob = new Blob([transcript], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `transcript-${new Date().toISOString()}.txt`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    console.log('Transcript downloaded');
  };

  const generateSummary = () => {
    if (transcriptEntries.length === 0) {
      alert('No transcript to summarize');
      return;
    }

    console.log('Generating summary...');
    setIsGeneratingSummary(true);
    setSummary("");

    wsRef.current?.send(JSON.stringify({
      action: "summary",
      transcript: transcriptEntries.map(e => ({
        speaker: e.speaker,
        timestamp: e.timestamp,
        text: e.text
      }))
    }));
  };

  const sendChatMessage = async () => {
    if (!chatInput.trim() || transcriptEntries.length === 0) {
      return;
    }

    const userMessage = chatInput.trim();
    setChatInput("");
    setIsSendingChat(true);

    console.log('Sending chat message:', userMessage.substring(0, 50));

    const savedUserMessage = await saveChatMessage(userMessage, 'user', 'message');

    if (savedUserMessage) {
      setChatMessages(prev => [...prev, savedUserMessage]);
    }

    wsRef.current?.send(JSON.stringify({
      action: "chat",
      question: userMessage,
      transcript: transcriptEntries.map(e => ({
        speaker: e.speaker,
        timestamp: e.timestamp,
        text: e.text
      }))
    }));
  };

  if (isLoading) {
    return (
      <DefaultPageLayout>
        <div className="flex h-full w-full items-center justify-center">
          <div className="flex flex-col items-center gap-4">
            <span className="text-body text-subtext-color">Loading lesson learned data...</span>
          </div>
        </div>
      </DefaultPageLayout>
    );
  }

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full items-start gap-4">
        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4 self-stretch">
          <div className="flex w-full items-center justify-between border-b border-solid border-neutral-border px-6 py-4">
            <div className="flex items-center gap-4">
              <SubframeCore.Icon
                className="text-heading-2 font-heading-2 text-default-font"
                name="FeatherMic"
              />
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Audio Recording
                </span>
                <span className="text-caption font-caption text-subtext-color">
                  Total Time: {formatTime(recordingTime)}
                </span>
              </div>
            </div>
          </div>

          <div className="flex w-full flex-col items-center gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
            <div className="flex items-center gap-4">
              <IconButton
                variant={isRecording ? "destructive-primary" : "neutral-primary"}
                size="large"
                icon={isRecording ? "FeatherCircle" : "FeatherMic"}
                onClick={isRecording ? stopRecording : startRecording}
              />
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  {isRecording ? "Recording in Progress" : "Ready to Record"}
                </span>
                <span className="text-body font-body text-subtext-color">
                  {formatTime(recordingTime)}
                </span>
              </div>
            </div>
            {isRecording && (
              <div className="flex items-center gap-2">
                <Button
                  variant="destructive-primary"
                  size="small"
                  icon="FeatherSquare"
                  onClick={stopRecording}
                >
                  Stop Recording
                </Button>
              </div>
            )}
          </div>

          <div className="flex w-full grow shrink-0 basis-0 items-start gap-4 px-6">
            <div className="flex w-96 flex-none flex-col items-start gap-4 self-stretch rounded-md border border-solid border-neutral-border bg-neutral-50 px-4 py-4">
              <div className="flex w-full items-center justify-between">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Transcript
                </span>
                <div className="flex items-center gap-2">
                  <IconButton
                    size="small"
                    icon="FeatherDownload"
                    onClick={downloadTranscript}
                    disabled={transcriptEntries.length === 0}
                  />
                </div>
              </div>
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 overflow-auto">
                {transcriptEntries.length === 0 ? (
                  <div className="flex w-full items-center justify-center py-8">
                    <span className="text-body font-body text-subtext-color">
                      {isRecording ? "Waiting for transcription..." : "No transcript yet"}
                    </span>
                  </div>
                ) : (
                  transcriptEntries.map((entry) => (
                    <div key={entry.id} className="flex w-full flex-col items-start gap-1">
                      <div className="flex items-center gap-2">
                        <span className="text-caption-bold font-caption-bold text-default-font">
                          {entry.speaker}
                        </span>
                        <span className="text-caption font-caption text-subtext-color">
                          {entry.timestamp}
                        </span>
                      </div>
                      <span className="text-body font-body text-default-font">
                        {entry.text}
                      </span>
                    </div>
                  ))
                )}
              </div>
            </div>

            <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4 self-stretch">
              <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
                <div className="flex w-full items-center gap-4">
                  <span className="text-heading-3 font-heading-3 text-default-font">
                    AI Summary
                  </span>
                  <Button
                    variant="brand-secondary"
                    size="small"
                    icon="FeatherRefreshCw"
                    onClick={generateSummary}
                    disabled={transcriptEntries.length === 0 || isGeneratingSummary}
                  >
                    {isGeneratingSummary ? 'Generating...' : 'Generate'}
                  </Button>
                </div>
              </div>
              <div ref={chatScrollRef} className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 overflow-auto">
                {chatMessages.length === 0 && !summary ? (
                  <div className="flex w-full items-start gap-4">
                    <img
                      className="h-8 w-8 flex-none"
                      src="/jlyuhbewnu0gdyipbb6e.png"
                    />
                    <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2 rounded-md bg-neutral-50 px-4 py-4">
                      <span className="text-body-bold font-body-bold text-brand-primary">
                        {isGeneratingSummary ? "Generating Summary..." : "Live Transcription Active"}
                      </span>
                      <span className="whitespace-pre-wrap text-body font-body text-default-font">
                        {isGeneratingSummary
                          ? "AI is analyzing the transcript and generating a summary..."
                          : isRecording
                            ? "Recording with voice activity detection. Audio sent automatically after pauses."
                            : "Click the microphone button to start recording. Transcriptions will appear automatically after pauses in speech."}
                      </span>
                    </div>
                  </div>
                ) : (
                  <>
                    {chatMessages.map((msg) => (
                      <div key={msg.id} className="flex w-full items-start gap-4">
                        {msg.role === 'assistant' && (
                          <img
                            className="h-8 w-8 flex-none rounded-full object-cover"
                            src="/jlyuhbewnu0gdyipbb6e.png"
                          />
                        )}
                        <div className={`flex grow shrink-0 basis-0 flex-col items-start gap-2 rounded-md px-4 py-4 ${msg.role === 'user' ? 'bg-brand-50 ml-auto max-w-[80%]' : 'bg-neutral-50'
                          }`}>
                          <span className="text-body-bold font-body-bold text-brand-primary">
                            {msg.type === 'summary' ? 'Meeting Summary' : msg.role === 'user' ? 'You' : 'Analysis Agent'}
                          </span>
                          <div className="whitespace-pre-wrap text-body font-body text-default-font">
                            {msg.message}
                          </div>
                        </div>
                      </div>
                    ))}
                    {isSendingChat && (
                      <div className="flex w-full items-start gap-4">
                        <img
                          className="h-8 w-8 flex-none rounded-full object-cover"
                          src="/jlyuhbewnu0gdyipbb6e.png"
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
                  </>
                )}
              </div>
              <div className="flex w-full flex-col items-center justify-center gap-3 px-4 py-4">
                <div className="flex h-14 w-full max-w-[768px] flex-none items-center justify-center gap-2 rounded-md bg-neutral-100 px-2 py-2">
                  <TextField className="h-auto grow shrink-0 basis-0">
                    <TextField.Input
                      placeholder="Ask a question about the recording..."
                      value={chatInput}
                      onChange={(event: React.ChangeEvent<HTMLInputElement>) => setChatInput(event.target.value)}
                      onKeyPress={(event: React.KeyboardEvent<HTMLInputElement>) => {
                        if (event.key === 'Enter' && !event.shiftKey) {
                          event.preventDefault();
                          sendChatMessage();
                        }
                      }}
                    />
                  </TextField>
                  <IconButton
                    disabled={!chatInput.trim() || transcriptEntries.length === 0 || isSendingChat}
                    size="large"
                    icon="FeatherArrowRight"
                    onClick={() => sendChatMessage()}
                  />
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default AarModule;