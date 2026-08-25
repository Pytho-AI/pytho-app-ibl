"use client";

import React from "react";
import { useEffect, useState, useRef } from 'react'
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import * as SubframeCore from "@subframe/core";
import { LinkButton } from "../subframe/components/LinkButton";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { AiChatSent } from "../subframe/components/AiChatSent";
import { AiChatReceived } from "../subframe/components/AiChatReceived";
import { useAppContext } from "../lib/contextLib";
import { uuid } from "../lib/uuid";
import { API } from 'aws-amplify';
import { useNavigate } from "react-router-dom";
import { useLocation } from 'react-router-dom';
import FakeTweet from "fake-tweet";
import Facebook from "../components/Facebook.tsx";
import ChatToolbar from "../components/ChatToolbar.tsx";
import { ChatMessageBar } from "../subframe/components/ChatMessageBar.tsx"
import DocEditor from "../components/DocEditor";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import mammoth from 'mammoth';
import '../custom.css'



const messageType = {
  answer: "answer",
  question: "question",
};

const defaultChatSettings = {
  shouldCiteSources: true,
  answerSize: "Short",
  includeRelatedQuestions: false,
  creativity: "Regular",
}

// Frontend progress simulator for media product types that don't stream
// backend status messages. Mirrors the "text + percentage" pattern used by
// docgen. Each phase advances the percentage linearly from the previous
// phase's endPct over `duration` seconds; the final phase holds near 100%
// until the real answer arrives.
type SimulatorPhase = { text: string; endPct: number; duration: number }

// Key used for the plain "Ask Anything" flow (no product type selected).
const ASK_ANYTHING_KEY = '__ask_anything__'

// Doctrines uses the same retrieval+LLM flow as Ask Anything on the backend,
// so it gets the same simulated progress phases.
const askAnythingPhases: SimulatorPhase[] = [
  { text: 'Searching knowledge base...', endPct: 25, duration: 4 },
  { text: 'Reading sources...', endPct: 55, duration: 6 },
  { text: 'Thinking...', endPct: 80, duration: 8 },
  { text: 'Drafting response...', endPct: 95, duration: 10 },
  { text: 'Almost there...', endPct: 98, duration: 60 },
]

const mediaSimulatorPhases: Record<string, SimulatorPhase[]> = {
  [ASK_ANYTHING_KEY]: askAnythingPhases,
  'Doctrines': askAnythingPhases,
  'Audio': [
    { text: 'Drafting transcript...', endPct: 25, duration: 8 },
    { text: 'Synthesizing voice...', endPct: 65, duration: 12 },
    { text: 'Rendering audio...', endPct: 90, duration: 10 },
    { text: 'Almost there...', endPct: 98, duration: 60 },
  ],
  'Image': [
    { text: 'Composing image...', endPct: 15, duration: 30 },
    { text: 'Rendering pixels...', endPct: 45, duration: 60 },
    { text: 'Polishing details...', endPct: 75, duration: 90 },
    { text: 'Finalizing image...', endPct: 92, duration: 90 },
    { text: 'Almost there...', endPct: 98, duration: 120 },
  ],
  'Facebook Post': [
    { text: 'Drafting posts...', endPct: 25, duration: 6 },
    { text: 'Generating post images...', endPct: 75, duration: 20 },
    { text: 'Assembling feed...', endPct: 92, duration: 8 },
    { text: 'Almost there...', endPct: 98, duration: 60 },
  ],
  'Tweet': [
    { text: 'Drafting tweets...', endPct: 35, duration: 6 },
    { text: 'Polishing copy...', endPct: 75, duration: 8 },
    { text: 'Assembling thread...', endPct: 92, duration: 6 },
    { text: 'Almost there...', endPct: 98, duration: 60 },
  ],
  'Video': [
    { text: 'Drafting transcript...', endPct: 20, duration: 10 },
    { text: 'Rendering scenes...', endPct: 60, duration: 20 },
    { text: 'Composing audio...', endPct: 85, duration: 15 },
    { text: 'Almost there...', endPct: 98, duration: 60 },
  ],
}

function computeSimulatedProgress(phases: SimulatorPhase[], elapsedSec: number) {
  let prevPct = 0
  let prevTime = 0
  for (const phase of phases) {
    const phaseEnd = prevTime + phase.duration
    if (elapsedSec <= phaseEnd) {
      const ratio = phase.duration > 0 ? (elapsedSec - prevTime) / phase.duration : 1
      const pct = Math.floor(prevPct + ratio * (phase.endPct - prevPct))
      return { text: phase.text, pct: Math.max(0, Math.min(99, pct)) }
    }
    prevPct = phase.endPct
    prevTime = phaseEnd
  }
  const last = phases[phases.length - 1]
  return { text: last.text, pct: Math.min(99, last.endPct) }
}

async function updateSession(pastSession, messages, session, projectDocs) {
  
  const parsedMessages = []

  for (const message of messages) {
    if (message.type == messageType.question) {
      parsedMessages.push(message)
    } else {
      let product = message.productType?.productName
      
      let response;
      if (product == "Facebook Post"){
        response = {
          type: messageType.answer,
          fbInjects: message.content,
        }
      } else if (product == "Tweet") {
        response = {
          type: messageType.answer,
          tweetInjects: message.content,
        }
      } else {
        const isCustomProduct = message.content?.includes('you asked for.') || message.content?.includes('you saved') || message.isCustomProduct;
        response = processHtmlResponse(message.content, isCustomProduct, message)
      }
      parsedMessages.push({
        ...response,
        timestamp: message.timestamp,
        sessionId: message.sessionId,
        isShared: message.isShared,
        userId: message.userId,
        title: message.title,
        plan: message.plan,
        isCustomProduct: message.isCustomProduct,
        htmlLink: message.htmlLink,
        docxLink: message.docxLink,
        productName: message.productName,
      }) 
    }
  }

  pastSession.messages = parsedMessages
  pastSession.session = session

  return pastSession
}

function displayDocs(docs) {
  return <div className="flex h-10 flex-none items-center gap-2 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
    {docs.map((doc, index) => {
      const fileName = doc.name?.split('.', 1)[0]
      const name = fileName?.length > 15 ? fileName?.substring(0, 15) + ".." : fileName
      const docType = doc.name.endsWith(".pdf") ? 
        "/sq5avpgdx7iyewnqgxe9.png" : 
        "/te7x34spuvttca0hnzuu.png"
      
      const docElement = <>
        <img
          className="h-8 w-8 flex-none object-cover"
          src={ docType }
          />
          <span className="text-body font-body text-default-font">
            { name }
          </span>
          <img className="flex-none" />
        </>

      return <div className="flex items-center gap-2" key={index}>
        { docElement }
        {(index < docs.length -1) &&<div className="flex w-px flex-none flex-col items-center gap-2 self-stretch bg-neutral-200" />}  
      </div>})}
  </div>

}

function displayProjectDocs() {
  return <div className="flex h-10 flex-none items-center gap-2 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
    <div className="flex items-center gap-2">
      <img
        className="h-8 w-8 flex-none object-cover"
        src="/te7x34spuvttca0hnzuu.png"
      />
      <div className="flex flex-col items-start gap-2">
        <span className="h-4 flex-none text-body font-body text-default-font">
          Using Shared Project Files
        </span>
        <span className="text-caption font-caption text-subtext-color">
          See all files in Project Settings
        </span>
      </div>
      <img className="flex-none" />
    </div>
  </div>
}


function processHtmlResponse(htmlResponse, isCustomProduct = false, message = null) {
  if (isCustomProduct) {
    if (message?.htmlLink) {
      return {
        type: messageType.answer,
        content: htmlResponse,
        isCustomProduct: true,
        htmlLink: message.htmlLink,
        docxLink: message.docxLink,
        productName: message.productName,
      };
    }

    const linkPattern = /<a href="([^"]+)"[^>]*>Here<\/a> is the (?:edited )?([^<]+) you (?:asked for|saved)/;
    const match = htmlResponse.match(linkPattern);
    if (match) {
      return {
        type: messageType.answer,
        content: htmlResponse,
        isCustomProduct: true,
        htmlLink: match[1],
        productName: match[2],
      };
    }
  }

  function parse(re, text) {
    const matches = re.exec(text)
    if (matches) {
      var degrees = parseInt(matches[1])
      var minutes = parseInt(matches[2])
      var seconds = parseInt(matches[3])
      var direction = matches[4]

      var dd = degrees + minutes / 60 + seconds / (60 * 60);

      if (direction == "S" || direction == "W") {
        dd = dd * -1;
      } // Don't do anything for N or E
      return dd;
    }

    return null
  }
  
  function parseMap(text) {
    const mapPattern = /[\s|\n|\r]*?<map>((.|\n)*?)<\/map>[\s|\n|\r]*?/gm
    const matches = [...text.matchAll(mapPattern)]
    const maps = []
    
    for (let i = 0; i < matches.length; i++) {
      try {
        const mapJSON = JSON.parse(matches[i][1].trim())
        if (mapJSON && ((mapJSON.units && mapJSON.units.length > 0) || (mapJSON.infrastructure && mapJSON.infrastructure.length > 0))) {
          mapJSON['id'] = Math.floor(Math.random() * 1000).toString()
          maps.push(mapJSON)
        }
      } catch (e) {
        // handle this
      }
    }
    return maps  
  }

  const latitude = parse(/\s*([0-9]+)°\s*([0-9]+)['|']\s*([0-9]+\.?[0-9]*)["|"]\s*([N|S])/g, htmlResponse)
  const longitude = parse(/\s*([0-9]+)°\s*([0-9]+)['|']\s*([0-9]+\.?[0-9]*)["|"]\s*([E|W])/g, htmlResponse) 
  const questionPattern = /[\s|\n|\r]*<question>((.|\n)*?)<\/question>[\s|\n|\r]*/gm
  const confidencePattern = /[\s|\n|\r]*<confidence>((.|\n)*?)<\/confidence>[\s|\n|\r]*/gm
  const cotPattern = /[\s|\n|\r]*?<chain_of_thoughts>((.|\n)*?)<\/chain_of_thoughts>[\s|\n|\r]*?/gm
  const followupMatches = [...htmlResponse.matchAll(questionPattern)]
  const confidenceMatches = [...htmlResponse.matchAll(confidencePattern)]
  const cotMatches = [...htmlResponse.matchAll(cotPattern)]

  var confidence = confidenceMatches.length > 0 ? confidenceMatches[0][1].trim() : null
  if (confidence) {
    confidence = ('' + Math.max(0, parseInt(confidence) - Math.floor(Math.random() * 5))) + '%' 
  }

  const cot = cotMatches.length > 0 ? cotMatches[0][1].trim().replace(/\n/g, "<br/>") : null

  htmlResponse = htmlResponse
    .replace(confidencePattern, '')
    .replace(cotPattern, '')

  const maps = parseMap(htmlResponse)
  const followupQuestions = []
  for (let i = 0; i < followupMatches.length; i++) {
    const q = followupMatches[i][1].trim()
    followupQuestions.push(q.replace(/^\d+. /, ""))  
  }


  const ampMatches = [...htmlResponse.matchAll(/\s[^\s"]*&/gm)]
  for (let i = 0; i < ampMatches.length; i++) {
    htmlResponse = htmlResponse.replace(ampMatches[i][0], ampMatches[i][0] + 'amp;')
  }

  const reportClasses = ['table', 'th', 'tr', 'td', 'ol', 'ul'] 
  for (const reportClass of reportClasses) {
    const regexBegin = new RegExp(`[\\s|\\n|\\r]*<${reportClass}`, "g")
    htmlResponse = htmlResponse.replace(regexBegin, `<${reportClass} class="report-${reportClass}" `)
    const regexEnd = new RegExp(`[\\s|\\n|\\r]*</${reportClass}`, "g")
    htmlResponse = htmlResponse.replace(regexEnd, `</${reportClass}`)
  }
  
  const bodyPattern = /<body>((.|\n)*?)<\/body>/gm
  const bodyMatches = [...htmlResponse.matchAll(bodyPattern)]

  if (bodyMatches.length > 0) { // contains html 
    htmlResponse = bodyMatches[0][1]
    
    htmlResponse = htmlResponse
      .replace(/\n/g, "")

    htmlResponse = `<div class="report">${htmlResponse}</div>`
  }

  //setTypeEffect(true)
  htmlResponse = htmlResponse.replace(/<location>((.|\n)+?)<\/location>\n*/g, '')
    .replace(/<map>((.|\n)+?)<\/map>\n*/g, '')
    .replace(/<tweet>((.|\n)+?)<\/tweet>\n*/g, '')
    .replace(/<post>((.|\n)+?)<\/post>\n*/g, '')
    .replace(/<image>((.|\n)+?)<\/image>\n*/g, '')
    .replace(/<scenario>\n*/g, '')
    .replace(/<\/scenario>\n*/g, '')
    .replace(/<answer>\n*/g, '')
    .replace(/<\/answer>\n*/g, '')
    .replace(questionPattern, '')
    .trim()
    .replace(/\n/g, "<br/>")
    .replace('context:', 'context')

  return {
    maps: maps,
    type: messageType.answer,
    content: htmlResponse,
    latitude: latitude,
    longitude: longitude,
    confidence: confidence,

    cot: cot,
    followupQuestions: followupQuestions,
  }
}


function Chat() {
  const { user, projectId, projectMap, setProjectMap } = useAppContext();
  const [question, setQuestion] = useState({ prompt: '', productType: '' });
  const [dataSources, setDataSources] = useState([]);
  const [docs, setDocs] = useState([]);
  const [audioSettings, setAudioSettings] = useState(null)
  const [mediaSettings, setMediaSettings] = useState(null)
  const [videoSettings, setVideoSettings] = useState(null)
  const [chatSettings, setChatSettings] = useState(defaultChatSettings)
  const chatSettingsRef = useRef(chatSettings)
  useEffect(() => {
    chatSettingsRef.current = chatSettings
  }, [chatSettings])
  const [messages, setMessages] = useState([])
  const [onRequest, setOnRequest] = useState(false)
  const progressSimulatorRef = useRef<ReturnType<typeof setInterval> | null>(null)
  const messagesEndRef = useRef(null);

  function stopProgressSimulator() {
    if (progressSimulatorRef.current) {
      clearInterval(progressSimulatorRef.current)
      progressSimulatorRef.current = null
    }
  }

  function startProgressSimulator(productName: string) {
    // Empty/missing product name means plain "Ask Anything".
    const key = productName || ASK_ANYTHING_KEY
    const phases = mediaSimulatorPhases[key]
    if (!phases) return

    stopProgressSimulator()
    const startTime = Date.now()

    const tick = () => {
      const elapsedSec = (Date.now() - startTime) / 1000
      const { text, pct } = computeSimulatedProgress(phases, elapsedSec)
      const content = `${text} (${pct}%)`
      setMessages(prev => {
        const last = prev[prev.length - 1]
        if (last?.type === 'status') {
          return [...prev.slice(0, -1), { type: 'status', content }]
        }
        return [...prev, { type: 'status', content }]
      })
    }

    tick()
    progressSimulatorRef.current = setInterval(tick, 500)
  }

  useEffect(() => {
    return () => {
      stopProgressSimulator()
    }
  }, [])
  const commentRef = useRef(null);
  const [isShared, setIsShared] = useState(false)
  const [audioOpen, setAudioOpen] = useState(false)
  const [docEditorOpen, setDocEditorOpen] = useState(false);
  const [currentDocContent, setCurrentDocContent] = useState("");
  const [currentDocLink, setCurrentDocLink] = useState("");
  const [currentHtmlLink, setCurrentHtmlLink] = useState("");
  const [currentDocxLink, setCurrentDocxLink] = useState("");
  const [currentProductName, setCurrentProductName] = useState("");
  const [currentSessionId, setCurrentSessionId] = useState("");
  const [editorIsDirty, setEditorIsDirty] = useState(false);
  const [showDiscardModal, setShowDiscardModal] = useState(false);
  const editorSaveRef = useRef<(() => Promise<void>) | null>(null);
  const downloadDocx = (url: string, name?: string) => {
    const a = document.createElement('a');
    a.href = url;
    a.download = `${name || 'document'}.docx`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  };
  const nav = useNavigate()
  const location = useLocation();

  function setProductType(productType) {
    if (productType.productName == "Audio") {
      setAudioOpen(true)
    }

    setQuestion({
      ...question,
      productType
    })

    // if (productType.isAgentic) {
    //   nav("/missioncommand/agents", { 
    //     replace: true,
    //     state: {
    //       question: {
    //         ...question,
    //         productType
    //     }
    //   }});
    // }
  }

  async function handleShare(index) {
    const message = messages[index]
    const pastSessionId = message.sessionId

    const sharedSession = await API.put('activities', `/sessions/${pastSessionId}/share/${message.timestamp}`, {
      body: {
        email: user.email, 
      }
    });
    const newSession = await updateSession(
      {
        sessionId: sharedSession.sessionId,
        date: 0,
        timestamp: sharedSession.timestamp,
        title: sharedSession.title,
        docsSummary: sharedSession.docsSummary,
        sessionDocs: sharedSession.sessionDocs,
        dataSources: sharedSession.dataSources,
        isShared: sharedSession.isShared,
      }, 
      sharedSession.messages, 
      sharedSession.session,
      projectMap[projectId]?.projectDetails?.docs || []
    )
    
    message.isShared = true
    const sessions = projectMap[projectId].sessions
    const pastSession = sessions.get(pastSessionId)
    pastSession.messages = messages

    setIsShared(true)
    updateProject(newSession, pastSession)
  } 

  async function showSession(pastSession, messageSessionId=null, messageTimestamp=null, allSessions=null) {
    // not yet fetched
    let updatedSession = pastSession
    if (!pastSession.messages) {
      const response = await fetch(`/api/session_details/${pastSession.sessionId}`)
      
      if (response.ok) {
        const sessionResult = JSON.parse(await response.json())
        pastSession.steps = sessionResult.steps  
        updatedSession = await updateSession(pastSession, sessionResult.messages, sessionResult.session,
          projectMap[projectId]?.projectDetails?.docs || []
        )
   
      } else {
        const error = await response.json()
        console.log(`Failed to fetch seesion details ${error}`) 
      }
    }

    updatedSession.messages.forEach((message) => {
      message.displayComments = message.sessionId === messageSessionId && message.timestamp === messageTimestamp
    })
    
    updateProject(updatedSession, null, allSessions)

    // if (pastSession.type == 'agentic') {
    //   nav("/missioncommand/agents", { 
    //     replace: true,
    //     state: {
    //       sessionId: pastSession.sessionId,
    //   }});
    // }
  }

  function groupSessions() {
    
    if (projectMap[projectId] && projectMap[projectId].sessions) {

      const sessions = projectMap[projectId].sessions
      
      // Guard against non-iterable sessions (e.g., empty object {}) #TODO: Fix this
      if (!sessions || typeof sessions[Symbol.iterator] !== 'function') {
        return <></>
      }
      
      const groupedItems = new Map();
      const dateLabels = new Map([
        [0, 'Today'],
        [1, 'Yesterday'],
        [6, 'Last 7 days'],
        [29, 'Last 30 days'],
        [30, 'Before 30 days'],
      ])

      for (const [key, item] of sessions) {
        if ((item.isShared || false) === isShared) {
          const { date } = item;
          
          if (!groupedItems.has(date)) {
            groupedItems.set(date, []);
          }
          groupedItems.get(date).push(item); 
        }
      }
      
      const listItems = [];
  
      const sortedDates = [...groupedItems.keys()].sort();

      sortedDates.map((date) => {
        const sessions = groupedItems.get(date)
        sessions.sort((a, b) => b.timestamp - a.timestamp);

        listItems.push(
          <div className="flex w-full items-start gap-2" key={date}>
            <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1 pb-4 pt-4">
              <span className="text-caption font-caption text-subtext-color">
              { dateLabels.get(date) }
              </span>    
            </div>
          </div>
        )
        
        sessions.map((pastSession) => {
          listItems.push(
            <div className="flex w-full items-start gap-2" key={pastSession.sessionId}>
              <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1 pb-2">
                <a className="text-body font-body text-default-font hover:underline cursor-pointer" onClick={async()=> {
                  // intel_agent sessions are the Generate-button / product-editor
                  // flow — their SessionDetail row has no `messages` array shaped
                  // for the legacy Chat scroll, so showSession is a noop. Land
                  // the analyst in the DocEditor route; DocEditorPage's boot
                  // hits /api/sessions/<id>/artifacts and loads the artifact.
                  if (pastSession.type === 'intel_agent') {
                    nav(`/missioncommand/docs/${encodeURIComponent(pastSession.sessionId)}`)
                    return
                  }
                  await showSession(pastSession)
                }}>
                { pastSession.title?.length > 45 ? pastSession.title.slice(0, 42) + '...' : pastSession.title }
                </a>
              </div>
            </div>
          )

        })
      })

      return <div className="flex w-full flex-col items-start overflow-y-auto">
        {listItems}      
      </div>

    }

    return <></>
  }
  
  function onTooltipClicked(prompt) {
    setDocs([])
    setDataSources([])
    getAnswer(
      projectMap[projectId].sessionId, 
      projectMap[projectId].sessions, {
        prompt,
        productType: ''
      },
      []
    )
  }

  async function openDocEditor(htmlLink, docxLink = '', productName = '', sessionId = '') {
    try {
      let htmlContent = '';
      if (htmlLink) {
        const response = await fetch(`${htmlLink}?t=${Date.now()}`);
        if (response.ok) htmlContent = await response.text();
      }
      if (!htmlContent && docxLink) {
        const docxResponse = await fetch(`${docxLink}?t=${Date.now()}`);
        if (docxResponse.ok) {
          const arrayBuffer = await docxResponse.arrayBuffer();
          htmlContent = (await mammoth.convertToHtml({ arrayBuffer })).value;
        }
      }
      if (htmlContent) {
        htmlContent = htmlContent.replace(/\[PAGE_BREAK\]/g, '<!-- pagebreak -->');
        setCurrentDocContent(htmlContent);
        setCurrentDocLink(htmlLink || '');
        setCurrentHtmlLink(htmlLink || '');
        setCurrentDocxLink(docxLink || '');
        setCurrentProductName(productName || 'document');
        setCurrentSessionId(sessionId || projectMap[projectId]?.sessionId || '');
        setDocEditorOpen(true);
      } else {
        alert('Could not load document content');
      }
    } catch (error: any) {
      alert(`Error loading document: ${error?.message || error}`);
    }
  }

  function getAnswer(
    sessionId=projectMap[projectId].sessionId, 
    sessions=projectMap[projectId].sessions,
    currentQuestion=question,
    currentDocs=docs,
    currentDataSources=dataSources,
    currentAudioSettings=audioSettings,
    currentVideoSettings=videoSettings,
    currentChatSettings=chatSettings
  ) {

    if (currentQuestion.prompt.length < 2 || onRequest) {
      return
    }

    let pastSession = sessions.get(sessionId)

    setOnRequest(true)
    const newMessages = [...messages, {
      type: messageType.question,
      content: currentQuestion.prompt,
      firstName: user.firstName,
      lastName: user.lastName,
    }]
    setMessages(newMessages);
    setQuestion({
      ...currentQuestion,
      prompt: ''
    })

    // Kick off a frontend progress simulator for media product types.
    // Backend doc/template flows stream real status messages and will
    // override these ticks via the `status` handler below.
    startProgressSimulator(currentQuestion?.productType?.productName)
    
    const baseline = currentDocs?.length > 0 ? {     // focus mode
      docs: false,
      links: false,
    } : projectMap[projectId]?.projectDetails?.baseline || {
      docs: true,
      links: true, 
    }

    let allDocs = currentDocs?.length > 0 ? currentDocs : (projectMap[projectId]?.projectDetails?.docs || [])
    

    try {
      const protocolPrefix = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      let { host } = window.location; // nb: window location contains the port, so host will be localhost:5173 in dev
      if (host.startsWith('localhost')) {
        host = 'localhost:8000'
      }

      const webSocket = new WebSocket(`${protocolPrefix}//${host}/ws/chat/test`); // change 'test' to user id 
      
      let sentChatSettings;
      webSocket.onopen = () => {
        // Use ref so we always send the latest chat settings (avoids stale closure after toggling in modal)
        sentChatSettings = chatSettingsRef.current ?? currentChatSettings
        webSocket.send(
          JSON.stringify({
            action: 'chat',
            userId: user.userId,
            email: user.email,
            firstName: user.firstName,
            lastName: user.lastName,
            projectId: projectId,
            sessionId: sessionId,
            session: pastSession?.session || [],
            question: currentQuestion,
            docs: allDocs,
            sessionDocs: currentDocs,
            baseline,
            docsSummary: pastSession?.docsSummary,
            isShared,
            audioSettings: currentAudioSettings,
            chatSettings: sentChatSettings,
            mediaSettings: mediaSettings
            // videoSettings: currentVideoSettings,
          })
        );
      };

      webSocket.onmessage = ({ data }) => {
        
        data = JSON.parse(data)
        console.log(data)

        // Handle status messages during generation - update in place
        if (data.type === 'status') {
          setMessages(prev => {
            const last = prev[prev.length - 1]
            if (last?.type === 'status') {
              return [...prev.slice(0, -1), { type: 'status', content: data.message }]
            }
            return [...prev, { type: 'status', content: data.message }]
          })
          return
        }

        // Remove status message when final response arrives
        stopProgressSimulator()
        setMessages(prev => prev.filter(m => m.type !== 'status'))

        let product = data['question']['productType']['productName']
      
        let response;
        if (product == "Facebook Post"){
          response = {
            type: messageType.answer,
            fbInjects: data["answer"],
          }
        } else if (product == "Tweet") {
          response = {
            type: messageType.answer,
            tweetInjects: data["answer"],
          }
        } else {
          const isCustomProduct = data.answer.includes('you asked for.') || data.isCustomProduct;
          response = processHtmlResponse(data.answer, isCustomProduct, data)
        }

        const currentMessages = [...newMessages, {
          ...response,
          timestamp: data.timestamp,
          userId: data.userId,
          title: data.title,
          sessionId,
          htmlLink: data.htmlLink,
          docxLink: data.docxLink,
          productName: data.productName,
          includeRelatedQuestions: sentChatSettings?.includeRelatedQuestions,
        }]

        if (pastSession) {
          pastSession.messages = currentMessages
          pastSession.session = data.session
          pastSession.docsSummary = data.docsSummary
        } else {
          pastSession = {
            sessionId,
            date: 0,
            timestamp: data.timestamp,
            title: data.title,
            messages: currentMessages,
            session: data.session,
            docsSummary: data.docsSummary,
            sessionDocs: currentDocs,
            dataSources: currentDataSources,
            isShared,
          }  
        }
        
        showSession(pastSession, null, null, sessions)
        
        webSocket.close();
        setOnRequest(false)
      };

    } catch (e) {
      stopProgressSimulator()
      setMessages(prev => prev.filter(m => m.type !== 'status'))
      setOnRequest(false);
    }

  }

  function getEpoch(dayBefore) {
    const today = new Date();

    const startDay = new Date(today);
    startDay.setDate(today.getDate() - dayBefore);

    return startDay.getTime();
  }

  async function fetchPastSessions() {
    // if there is already some sessions use timestamp for pagination
    const sessionsMap = new Map()
    const realProjectId = projectId === 'default' ? 'default-' + user.userId : projectId
 
    try {
      const response = await fetch(`/api/sessions/${realProjectId}`)
      
      if (response.ok) {
        const pastSessions = await response.json()

        for (let i=0; i<pastSessions.length; i++) {
          const pastSession = JSON.parse(pastSessions[i]) 
          if (getEpoch(1) < pastSession.timestamp) {
            pastSession.date = 0
          } else if (getEpoch(2) < pastSession.timestamp) {
            pastSession.date = 1
          } else if (getEpoch(7) < pastSession.timestamp) {
            pastSession.date = 6
          } else if (getEpoch(30) < pastSession.timestamp) {
            pastSession.date = 29
          } else {
            pastSession.date = 30
          }
  
          sessionsMap.set(pastSession.sessionId, pastSession)
        } 
      } else {
        const error = await response.json()
        console.log(`Failed to fetch past seesions ${error}`) 
      }
    } catch(e) {
      console.log(`Failed to fetch past seesions ${e}`) 
    }
    return sessionsMap    
  }

  function startNewChat() {
    stopProgressSimulator()
    setOnRequest(false)
    setVideoSettings(null)
    setAudioSettings(null)
    resetSession([], [])
  }

  async function resetSession(docs, dataSources, forceFetch=false, messageSessionId=null, messageTimestamp=null, shouldOpenComments=false) {
    const sessionId = messageSessionId || uuid()
    const sessions = (!forceFetch && projectMap[projectId]?.sessions instanceof Map) 
      ? projectMap[projectId].sessions 
      : await fetchPastSessions()
    setProjectMap(projectMap => ({...projectMap, [projectId]: {
      ...projectMap[projectId], 
      sessionId,
      sessions,
    }}));
    setMessages([])
    setDocs(docs)
    setDataSources(dataSources)

    if (messageSessionId && sessions.has(messageSessionId)) {
      if (shouldOpenComments) {
        await showSession(sessions.get(messageSessionId), messageSessionId, messageTimestamp, sessions)
      } else {
        await showSession(sessions.get(messageSessionId))
      }
    }
    return { sessionId, sessions }
  }

  function updateProject(updatedSession, oldSession=null, allSessions=null) {
    const sessions = projectMap[projectId].sessions || allSessions
    sessions.set(updatedSession.sessionId, updatedSession)
    
    if (oldSession) {
      sessions.set(oldSession.sessionId, oldSession)
    }
    setDocs(updatedSession.sessionDocs || [])
    setDataSources(updatedSession.dataSources || [])
    setMessages(updatedSession.messages)
    setProjectMap(projectMap => ({...projectMap, [projectId]: {
      ...projectMap[projectId], 
      sessionId: updatedSession.sessionId,
      sessions
    }}))
  }

  useEffect(() => {
    async function onLoad(docs, dataSources, question, audioSettings, videoSettings, messageSessionId, messageTimestamp, shouldOpenComments) {
      const result = await resetSession(docs, dataSources, false, messageSessionId, messageTimestamp, shouldOpenComments)
      getAnswer(result.sessionId, result.sessions, question, docs, dataSources, audioSettings, videoSettings)  
    }

    const docs = location.state?.docs || []
    const dataSources = location.state?.dataSources || []
    const audioSettings = location.state?.audioSettings
    const videoSettings = location.state?.videoSettings
    const question = location.state?.question || { prompt: '', productType: '' }
    
    setAudioSettings(audioSettings)
    setVideoSettings(videoSettings)
    setQuestion(question)
    onLoad(docs, dataSources, question, audioSettings, videoSettings, location.state?.sessionId, location.state?.timestamp, location.state?.shouldOpenComments)
    setIsShared(location.state?.isShared ? true : false)
    window.history.replaceState({}, '')
    
  }, [projectId]);

  useEffect(() => {
    if (commentRef.current) {
      commentRef.current.scrollIntoView({ behavior: 'smooth' });
    } else if (messagesEndRef.current) {
      messagesEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [messages]);


  const timerRef = useRef<NodeJS.Timeout | null>(null);
  const inputRef = useRef(null);
  
    const stripTrailingOver = (txt: string) =>
      txt.replace(/\bover\.\s*$/i, "").trim();
  
    useEffect(() => {
      // Cleanup when component unmounts
      if (inputRef.current) {
        inputRef.current.focus();
      }
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
        timerRef.current = setTimeout(() => {
          const cleaned = stripTrailingOver(nextPrompt);
          setQuestion(q => ({ ...q, prompt: cleaned }));
          getAnswer(
            projectMap[projectId].sessionId, 
            projectMap[projectId].sessions,
            { ...question, prompt: cleaned }
          )
        }, 3000);
      }
    };

  return (
    <DefaultPageLayout>
      {docEditorOpen && (
        <div className="relative flex h-full w-full flex-col bg-default-background">
          <div className="flex w-full items-center gap-2 border-b border-solid border-neutral-border bg-default-background px-3 py-1.5">
            <IconButton
              size="small"
              icon="FeatherArrowLeft"
              onClick={() => {
                if (!editorIsDirty) {
                  setDocEditorOpen(false);
                  setCurrentDocContent("");
                  setCurrentHtmlLink("");
                  setEditorIsDirty(false);
                } else {
                  setShowDiscardModal(true);
                }
              }}
            />
            <span className="text-body-bold font-body-bold text-default-font">
              Document Editor
            </span>
          </div>
          <div className="flex-1 overflow-auto">
            <DocEditor
              initialValue={currentDocContent}
              htmlLink={currentHtmlLink}
              docxLink={currentDocxLink}
              productName={currentProductName}
              sessionId={currentSessionId}
              onDirtyChange={(dirty) => setEditorIsDirty(dirty)}
              onRegisterSave={(saveFn) => { editorSaveRef.current = saveFn; }}
              onSave={() => { if (currentDocxLink) downloadDocx(currentDocxLink, currentProductName); }}
              onClose={() => {
                setDocEditorOpen(false);
                setCurrentDocContent("");
                setCurrentHtmlLink("");
                setEditorIsDirty(false);
              }}
              onSaveStart={async (html, docSettings) => {
                const tempId = `saving-${Date.now()}`;
                const savedProductName = currentProductName;
                const savedSessionId = currentSessionId;

                const savingMessage = {
                  type: messageType.answer,
                  content: `<span class="text-subtext-color">Saving ${savedProductName || 'document'}...</span>`,
                  isCustomProduct: true,
                  productName: savedProductName,
                  sessionId: savedSessionId,
                  timestamp: Date.now(),
                  tempId,
                };
                setMessages((prev) => [...prev, savingMessage]);
                setDocEditorOpen(false);
                setShowDiscardModal(false);
                setEditorIsDirty(false);

                try {
                  const formData = new FormData();
                  formData.append('doc_link', currentHtmlLink);
                  formData.append('content', html);
                  formData.append('session_id', savedSessionId || projectMap[projectId]?.sessionId || '');
                  formData.append('project_id', projectId || '');
                  formData.append('product_name', savedProductName || '');
                  formData.append('user_id', user?.email || user?.userId || '');
                  if (docSettings?.header) formData.append('header', docSettings.header);
                  if (docSettings?.headerStyle) formData.append('header_style', JSON.stringify(docSettings.headerStyle));
                  if (docSettings?.watermark) formData.append('watermark', docSettings.watermark);
                  if (docSettings?.watermarkColor) formData.append('watermark_color', docSettings.watermarkColor);
                  if (docSettings?.watermarkFont) formData.append('watermark_font', docSettings.watermarkFont);
                  if (docSettings?.watermarkSize) formData.append('watermark_size', docSettings.watermarkSize);
                  const result = await fetch('/api/save/', { method: 'POST', body: formData }).then(r => r.json());

                  if (result.htmlLink && result.docxLink) {
                    const finalContent = result.savedMessage;

                    setMessages((prev) => prev.map((msg) =>
                      msg.tempId === tempId
                        ? {
                            ...msg,
                            content: finalContent,
                            htmlLink: result.htmlLink,
                            docxLink: result.docxLink,
                            timestamp: result.timestamp,
                            tempId: undefined
                          }
                        : msg
                    ));
                    setCurrentHtmlLink(result.htmlLink);
                    setCurrentDocxLink(result.docxLink);
                    downloadDocx(result.docxLink, savedProductName);
                  }
                } catch (err: any) {
                  console.error('[Chat] Save failed:', err);
                  setMessages((prev) => prev.map((msg) =>
                    msg.tempId === tempId
                      ? { ...msg, content: `<span class="text-error-700">Save failed: ${err.message || 'Unknown error'}</span>` }
                      : msg
                  ));
                }
              }}
            />
          </div>
        </div>
      )}
      <DialogLayout open={showDiscardModal} onOpenChange={setShowDiscardModal}>
        <div className="flex w-full flex-col items-start gap-6 rounded-md border border-solid border-neutral-border bg-default-background p-6 shadow-lg">
          <div className="flex flex-col items-start gap-2">
            <span className="text-heading-3 font-heading-3 text-default-font">Unsaved Changes</span>
            <span className="text-body font-body text-subtext-color">You have unsaved changes. Would you like to save before leaving?</span>
          </div>
          <div className="flex w-full items-center justify-end gap-3">
            <Button variant="neutral-secondary" onClick={() => {
              setShowDiscardModal(false);
              setDocEditorOpen(false);
              setCurrentDocContent("");
              setCurrentHtmlLink("");
              setEditorIsDirty(false);
            }}>Discard</Button>
            <Button variant="brand-primary" onClick={async () => {
              if (editorSaveRef.current) await editorSaveRef.current();
            }}>Save</Button>
          </div>
        </div>
      </DialogLayout>
      <div className={docEditorOpen ? "hidden" : "contents"}>
      <div className="flex h-full w-full items-start justify-end gap-2">
        <div className="container max-w-none flex grow shrink-0 basis-0 flex-col items-center gap-6 self-stretch bg-default-background pt-12">
          <div className="flex w-full max-w-[660px] items-center gap-4">
            <div className="flex grow shrink-0 basis-0 items-center gap-2">
              {projectId != 'default' &&
                <div className="flex flex-col items-start gap-2">
                  <div className="flex items-center gap-2">
                    <SubframeCore.Icon
                      className="text-heading-2 font-heading-2 text-default-font"
                      name="FeatherFolder"
                    />
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      { projectMap[projectId].projectDetails.name }
                    </span>
                  </div>
                  <span className="text-body font-body text-subtext-color">
                    { projectMap[projectId].projectDetails.description }
                  </span>
                </div>
              }
              {projectId === 'default' && <>
                <SubframeCore.Icon
                  className="text-heading-2 font-heading-2 text-default-font"
                  name="FeatherBot"
                />
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Workflows
                </span>
              </>}
            </div>
            {projectId != 'default' &&
            <IconButton
              icon="FeatherEdit3"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                nav("/missioncommand/events/update");
              }}
            />
            }
          </div>
          {projectId != 'default' &&
          <div className="flex w-full max-w-[660px] items-center justify-end gap-2">
            <Button
              className="h-8 grow shrink-0 basis-0"
              variant={ isShared && "brand-secondary" }
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                if (isShared) {
                  setIsShared(false)
                  resetSession([], [])
                }
              }}
            >
              Personal
            </Button>
            <Button
              disabled
              className="h-8 grow shrink-0 basis-0"
              variant={ !isShared && "brand-secondary" }
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                if (!isShared) {
                  setIsShared(true)
                  resetSession([], [], true)
                }
              }}
            >
              Shared Space
            </Button>
          </div>}
          <div className="flex w-full max-w-[660px] grow shrink-0 basis-0 flex-col items-start">
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 pb-4 overflow-auto">
              {messages.map((item, index) => (
                <div key={index} className="w-full"> 
                  {item.type === messageType.answer && 
                  <AiChatReceived>
                    <div id={"content-" + index}>
                      {item.isCustomProduct ? (
                        <div>
                          <span 
                            className="grow shrink-0 basis-0 whitespace-pre-wrap text-body font-body text-default-font" 
                            dangerouslySetInnerHTML={{ __html: item.content }}
                          />
                          <Button
                            className="mt-4"
                            variant="brand-primary"
                            onClick={() => openDocEditor(item.htmlLink || item.docLink, item.docxLink, item.productName, item.sessionId)}
                          >
                            Open in Editor
                          </Button>
                        </div>
                      ) : (
                        <>
                          <span className="grow shrink-0 basis-0 whitespace-pre-wrap text-body font-body text-default-font" dangerouslySetInnerHTML={{ __html: item.content}} />
                          {item.tweetInjects && item.tweetInjects.map((inject, index) => (
                          <div className="w-full image-download" key={index}>
                            <br />
                            <FakeTweet config={inject} /> 
                          </div>
                          ))}
                          {item.fbInjects && item.fbInjects.map((inject, index) => (
                            <div className="w-full image-download" key={index}>
                              <Facebook post={ inject }/>
                              <br /> 
                            </div>
                          ))}
                          {item.includeRelatedQuestions !== false && item.followupQuestions && item.followupQuestions.length > 0 &&
                          <>
                            <br />
                            <span className="grow shrink-0 basis-0 whitespace-pre-wrap text-body font-body text-default-font font-bold">
                              Related Questions
                            </span>
                            <ul style={{paddingLeft: '20px'}}>
                            {item.followupQuestions.map((prompt, index) => (
                              <li  key={index}>
                              <LinkButton
                                className="text-left"
                                variant="neutral"
                                size="medium"
                                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                                  const newQuestion = {
                                    prompt: prompt,
                                    productType: '' 
                                  }
                                  
                                  getAnswer(
                                    projectMap[projectId].sessionId, 
                                    projectMap[projectId].sessions,
                                    newQuestion
                                  )
                                }}
                                >
                                {prompt}
                              </LinkButton>
                              </li> ))}
                            </ul>
                          </>}
                        </>
                      )}
                      </div>
                    <ChatToolbar
                      commentRef={messages[index].displayComments ? commentRef : null}
                      downloadId={"content-" + index} 
                      question={index > 0 ? messages[index-1]?.content : ''} 
                      answer={messages[index].content}
                      onShareClick={() => { handleShare(index) }}
                      message={messages[index]}
                      shouldShowShareButton={!isShared && projectId != 'default'}
                      shouldShowCommentButton={isShared}
                      disableShareButton={item.isShared}
                    />
                  </AiChatReceived>}
                  {item.type === 'status' &&
                  <AiChatReceived>
                    <div className="flex items-center gap-2">
                      <div className="animate-spin h-4 w-4 border-2 border-blue-500 border-t-transparent rounded-full" />
                      <span className="text-body font-body text-neutral-400">{item.content}</span>
                    </div>
                  </AiChatReceived>}
                  {item.type === messageType.question && 
                  <AiChatSent avatar={item.firstName ? item.firstName.toUpperCase()[0]+item.lastName.toUpperCase()[0] 
                    : 'Q'}>
                    <span className="grow shrink-0 basis-0 text-body font-body text-default-font text-success-700">
                      {item.content}
                    </span>
                  </AiChatSent>}
                </div>
                  
              ))}
              <div className="h-px" ref={messagesEndRef} />
            </div>
            
            <div className="flex w-full flex-col items-start justify-center gap-3 py-4">  
            { (projectMap[projectId]?.projectDetails?.docs?.length > 0 && displayProjectDocs()) || 
              (docs?.length > 0 && displayDocs(docs))  }            
              <ChatMessageBar
                onRequest={onRequest}
                getAnswer={getAnswer}
                startNewChat={startNewChat}
                question={question}
                setQuestion={setQuestion}
                productType={question.productType}
                setProductType={setProductType}
                audioSettings={audioSettings}
                setAudioSettings={setAudioSettings}
                mediaSettings={mediaSettings}
                setMediaSettings={setMediaSettings}
                videoSettings={videoSettings}
                setVideoSettings={setVideoSettings}
                chatSettings={chatSettings}
                setChatSettings={setChatSettings}
                docs={docs}
                dataSources={dataSources}
                onFocusMode={resetSession}
                onGeneratedProduct={(result: any) => {
                  // Manual-dispatch (Req 4): the Generate button in
                  // ProductLibraryModal POSTs /api/products/generate and
                  // returns {htmlLink, docxLink, artifact_id, ...}. Open the
                  // result in DocEditor using the same loader the chat path
                  // uses for analyst-published artifacts.
                  if (result?.htmlLink || result?.docxLink || result?.pptxLink) {
                    openDocEditor(
                      result.htmlLink || '',
                      result.docxLink || result.pptxLink || '',
                      result.artifact_type || 'document',
                      result.session_id || '',
                    );
                  }
                }}
              />
            </div>
            {messages.length > 0 && docs.length > 0 &&
            <div className="flex w-full items-center justify-center gap-2">
              <Button
                variant="brand-secondary"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  resetSession([], [])
                }}
              >
                Start New Chat
              </Button>
            </div>}
          </div>
        </div>
        <div className="flex w-60 flex-none flex-col items-start gap-6 self-stretch rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
          <div className="flex w-full flex-col items-start gap-6">
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              {projectId === 'default' ? "Quick ": projectMap[projectId].projectDetails?.name } Chat History
            </span>
          </div>
          {groupSessions()}
        </div>
      </div>
      </div>
    </DefaultPageLayout>
  );
}

export default Chat;