"use client";

import React, { useState, useEffect, useRef } from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { IconButton } from "../subframe/components/IconButton";
import { TextArea } from "../subframe/components/TextArea";
import { ChatStartCard } from "../subframe/components/ChatStartCard";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";
import { Button } from "../subframe";
import FocusMode  from "../components/FocusMode";
import AudioMenu from "../components/AudioMenu";
import VideoMenu from "../components/VideoMenu";
import ViewProductTypes from "../components/ViewProductTypes.tsx";

const surprisePrompts = [
  "How would we use the CV-22 Osprey for a long-range, high altitude, infiltration mission?",
  "Why is the F-16 such a critical aircraft in the USAF inventory?",
  "How does the C-17 contribute to US Air Power?",
  "Is the C-17 an aircraft that can adapt to the threats and missions of the future?",
  "How does a Joint Force Commander employ their Air Operations Center during a near-peer large-scale combat operation?"
]

function Home() {
  const { user } = useAppContext();
  const [productTypes, setProductTypes] = useState([])
  const [question, setQuestion] = useState({prompt: '', productType: ''});
  const [docs, setDocs] = useState([]);
  const [dataSources, setDataSources] = useState([]);
  const [audioOpen, setAudioOpen] = useState(false)
  const [audioSettings, setAudioSettings] = useState(null)
  const [videoSettings, setVideoSettings] = useState(null)
  const nav = useNavigate();

  function startChat(currentQuestion=question) {
    nav("/missioncommand/session", { 
      replace: true,
      state: { 
        docs,
        dataSources,
        audioSettings,
        videoSettings, 
        question: currentQuestion }
     });
  }
  
  function setProductType(productType) {
    if (productType.productName == "Audio") {
      setAudioOpen(true)
    }
    setQuestion({
      ...question,
      productType
    })
  }

  function onFocusMode(docs, dataSources) {
    setDataSources(dataSources)
    setDocs(docs)
  }

  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const inputRef = useRef(null);

  const stripTrailingOver = (txt: string) =>
    txt.replace(/\bover\.\s*$/i, "").trim();

  useEffect(() => {
    if (inputRef.current) {
      inputRef.current.focus();
    }
    // Cleanup when component unmounts
    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const nextPrompt = e.target.value;
    setQuestion(q => ({ ...q, prompt: nextPrompt }));
    // Reset any pending timer
    if (timerRef.current) clearTimeout(timerRef.current);

    if (/\bover\.\s*$/i.test(nextPrompt)) {
      console.log(nextPrompt)
      timerRef.current = setTimeout(() => {
        const cleaned = stripTrailingOver(nextPrompt);
        setQuestion(q => ({ ...q, prompt: cleaned }));
        startChat({ ...question, prompt: cleaned });           // auto‑submit
      }, 3000);
    }
  };

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full flex-col items-start gap-3">
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-start">
          <div className="flex w-full grow shrink-0 basis-0 flex-col items-center justify-center gap-4 bg-default-background px-6 py-6">
            <span className="text-heading-1 font-heading-1 text-default-font">
              The War Room
            </span>
            {/* <span className="w-full text-heading-2 font-heading-2 text-neutral-border text-center">
              AI-Powered Wargaming Operations
            </span> */}
            {/* <span className="text-body font-body text-default-font">
              Workflows
            </span> */}
            <div className="flex flex-col items-center justify-center gap-3 px-4 py-4">
              <div className="flex h-24 w-full flex-col rounded-md bg-neutral-100 px-2 py-1">
                
                {/* <IconButton
                  size="large"
                  icon="FeatherPaperclip"
                  onClick={(
                    event: React.MouseEvent<HTMLButtonElement>
                  ) => {}}
                /> */}
                {/* <SubframeCore.DropdownMenu.Root>
                  <SubframeCore.DropdownMenu.Trigger asChild={true}>
                    <IconButton
                      size="large"
                      icon="FeatherPaperclip"
                      onClick={(
                        event: React.MouseEvent<HTMLButtonElement>
                      ) => {}}
                    />
                  </SubframeCore.DropdownMenu.Trigger>
                  <SubframeCore.DropdownMenu.Portal>
                    <SubframeCore.DropdownMenu.Content
                      side="bottom"
                      align="start"
                      sideOffset={4}
                      asChild={true}
                    >
                      <DropdownMenu>
                        <DropdownMenu.DropdownItem icon="FeatherCloud">
                          Connect to Google Drive
                        </DropdownMenu.DropdownItem>
                        <DropdownMenu.DropdownItem icon="FeatherFilePlus2">
                          Upload from computer
                        </DropdownMenu.DropdownItem>
                      </DropdownMenu>
                    </SubframeCore.DropdownMenu.Content>
                  </SubframeCore.DropdownMenu.Portal>
                </SubframeCore.DropdownMenu.Root> */}
                <div className="flex flex-row w-full h-full">
                  <TextArea.Input
                    className="min-h-full w-full resize-none overflow-y-hidden"
                    ref={inputRef}
                    rows={1}
                    placeholder={
                      videoSettings? "Ask Pytho to create a short and realistic video segment..." : 
                        audioSettings ? "Ask Pytho to create a realistic audio clip..." : "Start your workflow here..."}
                    value={question.prompt}
                    onChange={handleChange}
                  />
                  <IconButton
                    disabled={question.prompt.length < 3}
                    icon="FeatherCornerDownLeft"
                    onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                      startChat()
                    }}
                  />
                </div>
                <div className="flex items-center flex-row">
                  <FocusMode docs={docs} dataSources={dataSources} onFocusMode={onFocusMode} videoSettings={videoSettings} />
                  <AudioMenu audioSettings={audioSettings} setAudioSettings={setAudioSettings} setVideoSettings={setVideoSettings} />
                  <VideoMenu videoSettings={videoSettings} setVideoSettings={setVideoSettings} setAudioSettings={setAudioSettings} docs={docs}  />
                  <ViewProductTypes productType={question.productType} setProductType={setProductType} saveProductTypes={setProductTypes}/>
                </div>
              </div>
            
              <div className="flex flex-wrap items-center justify-center gap-4">
                <ChatStartCard
                  className="w-60 flex-none"
                  icon={null}
                  text="Surprise me. "
                  onClick={() => {
                    startChat({
                      ...question,
                      prompt: surprisePrompts[Math.floor(Math.random() * surprisePrompts.length)]
                    })
                  }}
                />
                {/* <ChatStartCard
                  className="h-auto w-40 flex-none opacity-60"
                  icon="FeatherTwitter"
                  text="Create 10 Tweets from a DoD headline today."
                  variant="purple"
                  // onClick={() => {
                  //   startChat({
                  //     prompt: `Create 10 Tweets from a DoD headline today.`, 
                  //     productType: ''
                  //   })
                  // }}
                /> */}
                <ChatStartCard
                  className="w-60 flex-none"
                  icon={null}
                  text="How does the C-17 contribute to US Air Power?"
                  variant="yellow"
                  onClick={() => {
                    startChat({
                      prompt: `How does the C-17 contribute to US Air Power?`, 
                      productType: ''
                    })
                  }}
                />
                <ChatStartCard
                  className="h-auto w-60 flex-none"
                  icon={null}
                  text="Why is the F-16 such a critical aircraft in the USAF inventory?"
                  variant="green"
                  onClick={() => {
                    startChat({
                      prompt: `Why is the F-16 such a critical aircraft in the USAF inventory?`,
                      productType: ''
                    })
                  }}
                />
              </div>
              <Button
                variant="neutral-primary"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {

                  startChat()
                }}
              >
                Workflow History
              </Button>
            </div>
          </div>
        </div>
      </div>
      <AudioMenu open={audioOpen} setOpen={setAudioOpen} audioSettings={audioSettings} setAudioSettings={setAudioSettings} />
    </DefaultPageLayout>
  );
}

export default Home;