"use client";

import React from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { Badge } from "../subframe/components/Badge";
import { Tabs } from "../subframe/components/Tabs";
import { Button } from "../subframe/components/Button";
import { Avatar } from "../subframe/components/Avatar";
import { Feed } from "../subframe/components/Feed";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { Checkbox } from "../subframe/components/Checkbox";
import { CheckboxGroup } from "../subframe/components/CheckboxGroup";
import { useNavigate } from "react-router-dom";
import { useLocation } from "react-router-dom";
import * as SubframeCore from "@subframe/core";
import { ChatButton } from "../subframe/components/ChatButton";
import { DropdownMenu } from "../subframe/components/DropdownMenu";

import { TextArea } from "../subframe/components/TextArea";

import { useEffect, useState, useRef } from 'react'
import { useAppContext } from "../lib/contextLib";
import { uuid } from "../lib/uuid";
import DocEditor from "../components/DocEditor.tsx";
//import EvaluationDrawer from "../components/EvaluationDrawer.tsx";
//import HistoryDrawer from "../components/HistoryDrawer.tsx"
//import GoogleMapWithHeatmap from "../components/GoogleMapWithHeatmap.tsx";


function Agents() {
  const { user, setUser, projectId, projectMap, setProjectMap } = useAppContext();
  const [productType, setProductType] = useState('Products')
  const [question, setQuestion] = useState({ prompt: '', productType: { productName: 'Ask Anything', type: 'Ask Anything' }});
  const [activeTab, setActiveTab] = useState("");
  const [evalResult, setEvalResult] = useState("")
  const [messages, setMessages] = useState([])
  const [executionSteps, setExecutionSteps] = useState([])
  const [onRequest, setOnRequest] = useState(false)
  const [editorContent, setEditorContent] = useState("")
  const socketRef = useRef(null);
  const projectMapRef = useRef(projectMap);
  const messagesRef = useRef(messages);
  const executionStepsRef = useRef(executionSteps);
  const nav = useNavigate();
  const location = useLocation();

  
  useEffect(() => {
    projectMapRef.current = projectMap; // Always stores the latest value
  }, [projectMap]);

  useEffect(() => {
    executionStepsRef.current = executionSteps; // Always stores the latest value
  }, [executionSteps]);

  useEffect(() => {
    messagesRef.current = messages; // Always stores the latest value
  }, [messages]);

  const messageType = {
    answer: "answer",
    question: "question",
  };

  function showSession(sessionId) {
    const sessions = projectMap[projectId].sessions
    let pastSession = sessions.get(sessionId)
    
    if (pastSession.steps?.length > 0) {
      setExecutionSteps(pastSession.steps)
    }

    setMessages(pastSession.messages)
    setQuestion({
      ...question,
      productType: ''
    })
  }

  async function loadHtml(url) {
    console.log(url)
    try {
      const response = await fetch(url);
      const html = await response.text();
      //console.log(html);

      setEditorContent(html)
    } catch (error) {
      console.error('Fetch failed:', error);
    }
  }


  function resetSession() {
    // const sessionId = messageSessionId || uuid()
    // const sessions = (!forceFetch && projectMap[projectId]?.sessions) || await fetchPastSessions()
    const sessionId = uuid()
    const sessions = projectMap[projectId]?.sessions || new Map()
    setProjectMap(projectMap => ({...projectMap, [projectId]: {
      ...projectMap[projectId], 
      sessionId,
      sessions,
    }}));
    setMessages([])
    // setDocs(docs)
    // setDataSources(dataSources)

    // if (messageSessionId && sessions.has(messageSessionId)) {
    //   if (shouldOpenComments) {
    //     await showSession(sessions.get(messageSessionId), messageSessionId, messageTimestamp, sessions)
    //   } else {
    //     await showSession(sessions.get(messageSessionId))
    //   }
    // }
    return { sessionId, sessions }
  }


  useEffect(() => {
    let reconnectTimer;

    const connect = () => {
      let { host } = window.location; // nb: window location contains the port, so host will be localhost:5173 in dev
      if (host.startsWith('localhost')) {
        host = 'localhost:8000'
      }

      socketRef.current = new WebSocket(`ws://${host}/ws/agents/test`); // change 'test' to user id

      socketRef.current.onopen = () => {
        console.log("WebSocket connected");  
        const result = resetSession()
        if (location.state?.question) {
          setQuestion(location.state?.question)
          getAnswer(result.sessionId, result.sessions, location.state?.question)
        } else if (location.state?.sessionId) {
          showSession(location.state?.sessionId)
        } else {
          nav("/missioncommand"); // user directly hit /agents .... move them back
        }
      };

      socketRef.current.onmessage = ({ data }) => {
        console.log(data)
        data = JSON.parse(data)
        const projectMap = projectMapRef.current
        const sessionId = projectMap[projectId].sessionId
        const sessions = projectMap[projectId].sessions
        let pastSession = sessions.get(sessionId)
        if (data.type === 'plan') {
          setOnRequest(false)
          if (data.answer?.includes('|DONE|')) {
            data.answer = data.answer.replace('|DONE|', '')
            execute(pastSession?.plan, sessionId)
          }
          const response = processHtmlResponse(data.answer)
          const currentMessages = [...messagesRef.current, {
            ...response,
            timestamp: data.timestamp,
            userId: data.userId,
            title: data.title,
            plan: data.plan,
            sessionId,
            // add information for constructing backend session
          }]

          if (!pastSession) {
            pastSession = {
              sessionId,
              type: 'agentic',
              date: 0,
              timestamp: data.timestamp,
              title: data.title,
              // docs: allDocs,
              // dataSources: currentDataSources,
              // isShared,
            }  
          }

          pastSession.messages = currentMessages
          pastSession.session = data.session
          pastSession.docsSummary = data.docsSummary
          // save the latest plan
          pastSession.plan = data.plan || pastSession.plan
          pastSession.title = pastSession.plan?.task
          pastSession.timestamp = Date.now()

          //showSession(pastSession, null, null, sessions)
          updateProject(pastSession)


        } else if (data.type === 'executing') {
          setExecutionSteps([
            ...executionStepsRef.current,
            data  
          ])

        } else if (data.type === 'execution_stop') {
          
          setExecutionSteps(prev => {
            prev[data.executedStepIndex].content = data.content
            prev[data.executedStepIndex].type = data.type

            return [...prev]
          });

        } else if (data.type === 'execution_combined') {
          setOnRequest(false)
          if (data.evalResult) {
            setEvalResult(data.evalResult)
          }

          const allSteps = [
            ...executionStepsRef.current,
            data  
          ]

          setExecutionSteps(allSteps)
          pastSession.steps = allSteps
          updateProject(pastSession)
          console.log(data.link)

        } else if (data.type === 'execution_avatar') {
          setOnRequest(false)
          const allSteps = [
            ...executionStepsRef.current,
            data  
          ]

          setUser({
            ...user,
            avatars: [{
              ...data.avatar,
              projectId,
            }, ...user.avatars]
          })
          setExecutionSteps(allSteps)
          pastSession.steps = allSteps
          updateProject(pastSession)

          nav("/avatar", { 
            replace: true,
            state: { 
              avatar: data.avatar
            }
          });
        }

      };

      socketRef.current.onclose = (e) => {
        if (socketRef.current) {
          console.log('Closed:', e.code, e.reason);
          console.log("WebSocket disconnected, attempting to reconnect...");
          reconnectTimer = setTimeout(connect, 3000); // Reconnect after 3 seconds
        }
      };

      socketRef.current.onerror = (error) => {
        console.error("WebSocket error:", error);
        socketRef.current.close();
      };
    };

    connect();

    return () => {
      clearTimeout(reconnectTimer);
      console.log("shutting down socket")
      socketRef.current?.close();
      socketRef.current = null
    };
  }, []);

  const sendMessage = (data) => {
    console.log(data.length)
    if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
      socketRef.current.send(data);
    }
  };

  function processHtmlResponse(htmlResponse) {
    // function parse(re, text) {
    //   const matches = re.exec(text)
    //   if (matches) {
    //     var degrees = parseInt(matches[1])
    //     var minutes = parseInt(matches[2])
    //     var seconds = parseInt(matches[3])
    //     var direction = matches[4]

    //     var dd = degrees + minutes / 60 + seconds / (60 * 60);

    //     if (direction == "S" || direction == "W") {
    //       dd = dd * -1;
    //     } // Don't do anything for N or E
    //     return dd;
    //   }

    //   return null
    // }

    // function parseMap(text) {
    //   const mapPattern = /[\s|\n|\r]*?<map>((.|\n)*?)<\/map>[\s|\n|\r]*?/gm
    //   const matches = [...text.matchAll(mapPattern)]
    //   const maps = []

    //   for (let i = 0; i < matches.length; i++) {
    //     try {
    //       const mapJSON = JSON.parse(matches[i][1].trim())
    //       if (mapJSON && ((mapJSON.units && mapJSON.units.length > 0) || (mapJSON.infrastructure && mapJSON.infrastructure.length > 0))) {
    //         mapJSON['id'] = Math.floor(Math.random() * 1000).toString()
    //         maps.push(mapJSON)
    //       }
    //     } catch (e) {
    //       // handle this
    //     }
    //   }
    //   return maps  
    // }

    // function parseTweets(tweets) {
    //   const tweetInjects = []

    //   for (let i = 0; i < tweets.length; i++) {
    //     const nameMatches = [...tweets[i][1].matchAll(/<name>((.|\n)*?)<\/name>/gm)]
    //     const avatarMatches = [...tweets[i][1].matchAll(/<avatar>((.|\n)*?)<\/avatar>/gm)]
    //     const messageMatches = [...tweets[i][1].matchAll(/<message>((.|\n)*?)<\/message>/gm)]
    //     const imageMatches = [...tweets[i][1].matchAll(/<image>((.|\n)*?)<\/image>/gm)]
    //     if (nameMatches.length > 0 && avatarMatches.length > 0 && messageMatches.length > 0) {
    //       const name = nameMatches[0][1]
    //       const nickname = name.replace(/\s/g,'_').toLowerCase() +  Math.floor(Math.random() * 100)
    //       tweetInjects.push({
    //         user: {
    //           nickname: nickname,
    //           name: name,
    //           avatar: avatarMatches[0][1],
    //           verified: Math.random() > .6 ? true : false,
    //           locked: false
    //         },
    //         hour: Math.ceil(Math.random() * 10),
    //         display: "default",
    //         text: messageMatches[0][1],
    //         //date: "",
    //         //app: "",
    //         retweets: Math.floor(Math.random() * 100),
    //         quotedTweets: Math.floor(Math.random() * 10),
    //         likes: Math.floor(Math.random() * 10000),
    //         ...(imageMatches.length > 0 && { media: imageMatches[0][1] })
    //       })
    //     }  
    //   }

    //   return tweetInjects
    // }

    // const tweets = [...htmlResponse.matchAll(/<tweet>((.|\n)*?)<\/tweet>/gm)]
    // const fbPosts = [...htmlResponse.matchAll(/<post>((.|\n)*?)<\/post>/gm)]

    // const latitude = parse(/\s*([0-9]+)°\s*([0-9]+)['|’]\s*([0-9]+\.?[0-9]*)["|”]\s*([N|S])/g, htmlResponse)
    // const longitude = parse(/\s*([0-9]+)°\s*([0-9]+)['|’]\s*([0-9]+\.?[0-9]*)["|”]\s*([E|W])/g, htmlResponse) 
    // const questionPattern = /[\s|\n|\r]*<question>((.|\n)*?)<\/question>[\s|\n|\r]*/gm
    // const confidencePattern = /[\s|\n|\r]*<confidence>((.|\n)*?)<\/confidence>[\s|\n|\r]*/gm
    // const cotPattern = /[\s|\n|\r]*?<chain_of_thoughts>((.|\n)*?)<\/chain_of_thoughts>[\s|\n|\r]*?/gm
    // const followupMatches = [...htmlResponse.matchAll(questionPattern)]
    // const confidenceMatches = [...htmlResponse.matchAll(confidencePattern)]
    // const cotMatches = [...htmlResponse.matchAll(cotPattern)]

    // var confidence = confidenceMatches.length > 0 ? confidenceMatches[0][1].trim() : null
    // if (confidence) {
    //   confidence = ('' + Math.max(0, parseInt(confidence) - Math.floor(Math.random() * 5))) + '%' 
    // }

    // const cot = cotMatches.length > 0 ? cotMatches[0][1].trim().replace(/\n/g, "<br/>") : null

    // htmlResponse = htmlResponse
    //   .replace(confidencePattern, '')
    //   .replace(cotPattern, '')

    // const maps = parseMap(htmlResponse)
    // const followupQuestions = []
    // for (let i = 0; i < followupMatches.length; i++) {
    //   const q = followupMatches[i][1].trim()
    //   followupQuestions.push(q.replace(/^\d+. /, ""))  
    // }


    // const ampMatches = [...htmlResponse.matchAll(/\s[^\s"]*&/gm)]
    // for (let i = 0; i < ampMatches.length; i++) {
    //   htmlResponse = htmlResponse.replace(ampMatches[i][0], ampMatches[i][0] + 'amp;')
    // }

    // if (contextDocs) {
    //   for (let i = 0; i < contextDocs.length; i++) {
    //     htmlResponse = htmlResponse.replaceAll(`s3://doc.${i}`, contextDocs[i].url)
    //   }
    // }

    // const reportClasses = ['table', 'th', 'tr', 'td', 'ol', 'ul'] 
    // for (const reportClass of reportClasses) {
    //   const regexBegin = new RegExp(`[\\s|\\n|\\r]*<${reportClass}`, "g")
    //   htmlResponse = htmlResponse.replace(regexBegin, `<${reportClass} class="report-${reportClass}" `)
    //   const regexEnd = new RegExp(`[\\s|\\n|\\r]*</${reportClass}`, "g")
    //   htmlResponse = htmlResponse.replace(regexEnd, `</${reportClass}`)
    // }

    // const bodyPattern = /<body>((.|\n)*?)<\/body>/gm
    // const bodyMatches = [...htmlResponse.matchAll(bodyPattern)]

    // if (bodyMatches.length > 0) { // contains html 
    //   htmlResponse = bodyMatches[0][1]

    //   htmlResponse = htmlResponse
    //     .replace(/\n/g, "")

    //   htmlResponse = `<div class="report">${htmlResponse}</div>`
    // }

    // //setTypeEffect(true)
    // htmlResponse = htmlResponse.replace(/<location>((.|\n)+?)<\/location>\n*/g, '')
    //   .replace(/<map>((.|\n)+?)<\/map>\n*/g, '')
    //   .replace(/<tweet>((.|\n)+?)<\/tweet>\n*/g, '')
    //   .replace(/<post>((.|\n)+?)<\/post>\n*/g, '')
    //   .replace(/<image>((.|\n)+?)<\/image>\n*/g, '')
    //   .replace(/<scenario>\n*/g, '')
    //   .replace(/<\/scenario>\n*/g, '')
    //   .replace(/<answer>\n*/g, '')
    //   .replace(/<\/answer>\n*/g, '')
    //   .replace(questionPattern, '')
    //   .trim()
    //   .replace(/\n/g, "<br/>")
    //   .replace('context:', 'context')

    const index = htmlResponse.indexOf('<plan>')
    const content = index !== -1 ? htmlResponse.substring(0, index) : htmlResponse;

    return {
      // maps: maps,
      type: messageType.answer,
      content
      // latitude: latitude,
      // longitude: longitude,
      // confidence: confidence,

      // tweetInjects: parseTweets(tweets),
      // fbInjects: parseTweets(fbPosts),
      // cot: cot,
      // followupQuestions: followupQuestions,
    }
  }


  function updateProject(updatedSession, oldSession=null, allSessions=null) {
    const projectMap = projectMapRef.current
    const sessions = projectMap[projectId].sessions || allSessions
    sessions.set(updatedSession.sessionId, updatedSession)

    if (oldSession) {
      sessions.set(oldSession.sessionId, oldSession)
    }
    // setDocs(updatedSession.docs)
    // setDataSources(updatedSession.dataSources || [])
    setMessages(updatedSession.messages)
    // setProjectMap(projectMap => ({...projectMap, [projectId]: {
    //   ...projectMap[projectId], 
    //   sessionId: updatedSession.sessionId,
    //   sessions
    // }}))
  }


  function execute(plan, sessionId) {

    try {
      setOnRequest(true)

      const docs = projectMap[projectId]?.projectDetails?.docs || []

      sendMessage(
        JSON.stringify({
          action: 'chat',
          type: 'execute',
          userId: user.userId,
          email: user.email,
          firstName: user.firstName,
          lastName: user.lastName,
          projectId: projectId,
          sessionId,
          plan,
          docs,
          // docs: allDocs,
          // dataSources: currentDataSources,
          // baseline,
          // docsSummary: pastSession?.docsSummary,
          // isShared,
          // audioSettings: currentAudioSettings,
          // videoSettings: currentVideoSettings,
        })
      )


    } catch (e) {

    }  

  } 

  function getAnswer(
    sessionId=projectMap[projectId].sessionId, 
    sessions=projectMap[projectId].sessions,
    currentQuestion=question,
  ) {
    
    if (currentQuestion.prompt.length < 2 || onRequest) {
      return
    }
    
    setExecutionSteps([])
    setEditorContent("") // may need to change this
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

    const docs = projectMap[projectId]?.projectDetails?.docs || []

    try {

      sendMessage(
        JSON.stringify({
          action: 'chat',
          type: 'plan',
          userId: user.userId,
          email: user.email,
          firstName: user.firstName,
          lastName: user.lastName,
          projectId,
          sessionId,
          session: pastSession?.session || [],
          question: currentQuestion,
          docs,
          plan: pastSession?.plan
          // docs: allDocs,
          // dataSources: currentDataSources,
          // baseline,
          // docsSummary: pastSession?.docsSummary,
          // isShared,
          // audioSettings: currentAudioSettings,
          // videoSettings: currentVideoSettings,
        })
      );


    } catch (e) {
      console.log(e)
    } 

  }

  return (
    <DefaultPageLayout>
      <div className="flex h-full w-full flex-col items-start bg-default-background">
        <div className="flex w-full items-center gap-2">
          <div className="flex grow shrink-0 basis-0 flex-wrap items-start justify-between border-b border-solid border-neutral-border px-6 py-6">
            <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
              <span className="w-full text-heading-2 font-heading-2 text-default-font">
                {projectId?.startsWith('default') ? "Workflows": projectMap[projectId].projectDetails?.name }
              </span>
              <span className="text-heading-3 font-heading-3 text-subtext-color">
                {question?.productType?.productLongName || question?.productType?.productName || 'Agent'} Workflow
              </span>
            </div>
            <div className="flex items-center gap-2 cursor-pointer" onClick={() =>  {
              nav("/missioncommand/session");
            }}>
              <SubframeCore.Icon
                className="text-heading-2 font-heading-2 text-default-font self-end"
                name="FeatherArrowLeft"
              />
              <span className="text-heading-2 font-heading-2 text-default-font">
                Back to Chat
              </span>
            </div>
            {/* <Tabs className="h-auto w-auto flex-none">
              <Tabs.Item active={activeTab === 'history'} onClick={() => setActiveTab('history')}>History</Tabs.Item>
              <Tabs.Item active={activeTab === 'eval'} onClick={() => setActiveTab('eval')}>Evals</Tabs.Item>
            </Tabs>
            <EvaluationDrawer activeTab={activeTab} setActiveTab={setActiveTab} evalResult={evalResult} />
            <HistoryDrawer activeTab={activeTab} setActiveTab={setActiveTab} /> */}
          </div>
        </div>
        <div className="flex w-full grow shrink-0 basis-0 h-12 flex-wrap items-start">
          <div className="flex min-w-96 w-1/4 flex-none flex-col items-start self-stretch border-r border-solid border-neutral-border h-full">
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-4 px-4 py-4 overflow-auto overscroll-none">
              <Feed>
                {/* <Feed.Item
                  leftSlot={
                    <IconWithBackground size="small" icon="FeatherBot" />
                  }
                  timestamp=""
                  isLast={true}
                >
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
                    // <span className="w-full text-body font-body text-subtext-color text-right">
                    //   Just now
                    // </span> 
                    // <span className="text-body font-body text-success-700">
                      Select the product type from the dropdown below to get started
                    </span>
                    <SubframeCore.DropdownMenu.Root>
                      <SubframeCore.DropdownMenu.Trigger asChild={true}>
                        <ChatButton
                          className="h-auto w-auto flex-none"
                          text={productType}
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
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('ASCOPE/PMESII')}
                            >
                              ASCOPE/PMESII
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('CCIRs')}
                            >
                              CCIRs
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('COA Analysis')}
                            >
                              COA Analysis
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('Collection Plan')}
                            >
                              Collection Plan
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('Country Brief')}
                            >
                              Country Brief
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('HVT List')}
                            >
                              HVT List
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('IIR')}
                            >
                              IIR
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('Intelligence Estimate')}
                            >
                              Intelligence Estimate
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('Memorandum')}
                            >
                              Memorandum
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('SIGACT')}
                            >
                              SIGACT
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('SITEMP')}
                            >
                              SITEMP
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('SITREP')}
                            >
                              SITREP
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('SPOTREP')}
                            >
                              SPOTREP
                            </DropdownMenu.DropdownItem>
                            <DropdownMenu.DropdownItem
                              className="h-5 w-full flex-none"
                              icon={null}
                              onClick={() => setProductType('Target Package')}
                            >
                              Target Package
                            </DropdownMenu.DropdownItem>
                          </DropdownMenu>
                        </SubframeCore.DropdownMenu.Content>
                      </SubframeCore.DropdownMenu.Portal>
                    </SubframeCore.DropdownMenu.Root>
                  </div>
                </Feed.Item> */}

              {messages.map((item, index) => (
                <div key={index} className="w-full"> 
                  {item.type === messageType.question && <Feed.Item
                    leftSlot={
                      <Avatar size="small" image="">
                        {/* {user.firstName.toUpperCase()[0]+user.lastName.toUpperCase()[0]} */}
                        Q
                      </Avatar>
                    }
                    timestamp=""
                    isLast={index === messages.length-1}
                  >
                    <div className="flex flex-col items-start gap-2">
                      {/* <span className="w-full text-body font-body text-subtext-color text-right">
                        2m ago
                      </span> */}
                      <span className="text-body font-body text-default-font">
                        {item.content}
                      </span>
                    </div>
                  </Feed.Item>}

                  {item.type === messageType.answer && !item.plan && <Feed.Item  
                    leftSlot={
                      <IconWithBackground size="small" icon="FeatherBot" />
                    }
                    timestamp=""
                    isLast={index === messages.length-1}
                  >
                    <div className="flex flex-col items-start gap-2">
                      {/* <span className="w-full text-body font-body text-subtext-color text-right">
                        Just now
                      </span> */}
                      <span className="text-body font-body text-success-700" dangerouslySetInnerHTML={{ __html: item.content?.replace('|DONE|', '')}}>
                      
                      </span>
                    </div>
                  </Feed.Item>}

                  {item.type === messageType.answer && item.plan && <Feed.Item
                    leftSlot={
                      <IconWithBackground size="small" icon="FeatherBot" />
                    }
                    comment={
                      <Feed.Comment className="text-success-700">
                        What do you think of this plan?
                      </Feed.Comment>
                    }
                    timestamp=""
                    isLast={index === messages.length-1}
                  >
                    <div className="flex flex-col items-start gap-2">
                      <span className="text-body-bold font-body-bold text-success-700" dangerouslySetInnerHTML={{ __html: item.content?.replace('|DONE|', '')}}>
                        
                      </span>
                    </div>
                    <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2 pt-2">
                      {/* <div className="flex items-start gap-2">
                        <span className="text-body font-body text-subtext-color">
                          1.
                        </span>
                        <div className="flex flex-col items-start gap-2">
                          <span className="text-body font-body text-success-700">
                            Gather data from the following sources:
                          </span>
                          <CheckboxGroup label="Data to include" helpText="">
                            {item.plan.dataSources.map((source, index) => (
                              <Checkbox
                                defaultChecked={true}
                                key={index}
                                label={source}
                                //checked={true}
                                onCheckedChange={(checked: boolean) => {}}
                              />
                            ))}
                          </CheckboxGroup>
                        </div>
                      </div> */}
                      {item.plan.steps.map((step, index) => (
                        <div className="flex items-start gap-2" key={index+1}>
                          <span className="text-body font-body text-subtext-color">
                            {index+1}.
                          </span>
                          <span className="text-body font-body text-success-700" dangerouslySetInnerHTML={{ __html: step}}>

                          </span>
                        </div>
                      ))}

                      {/* <div className="flex items-start gap-2">
                        <span className="text-body font-body text-subtext-color">
                          {item.plan.steps.length+2}.
                        </span>
                        <span className="text-body font-body text-success-700">
                          Evaluate the response.
                        </span>
                      </div>
                      <div className="flex items-start gap-2">
                        <span className="text-body font-body text-subtext-color">
                        {item.plan.steps.length+3}.
                        </span>
                        <span className="text-body font-body text-success-700">
                          Deliver to you in .docx format.
                        </span>
                      </div> */}
                    </div>
                  </Feed.Item>}
                </div>
              ))}




              </Feed>
            </div>
            <div className="flex w-full flex-col items-start gap-4 border-t border-solid border-neutral-border bg-neutral-50 px-4 py-4">
              <div className="flex flex-row w-full h-full">
                <TextArea.Input
                  rows={1}
                  disabled={onRequest}
                  className="min-h-full w-full resize-none overflow-y-hidden"
                  placeholder="Create a product..."
                  value={question.prompt}
                  onChange={(
                    event: React.ChangeEvent<HTMLInputElement>
                  ) => setQuestion({
                    ...question,
                    prompt: event.target.value,
                  })}
                  onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
                    if (event.keyCode === 13) {
                      getAnswer()
                    }
                  }}
                />
              </div>
              {!onRequest ? <Button
                className="h-8 w-full flex-none"
                icon="FeatherSend"
                // disabled={question.prompt.length < 3}
                //loading={onRequest}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  getAnswer()
                }}
              >
                Send Message
              </Button> : <Button
                className="h-8 w-full flex-none animate-pulse"
                variant="brand-secondary"
                icon="FeatherPauseCircle"
                // disabled={question.prompt.length < 3}
                //loading={onRequest}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  nav("/missioncommand/session");
                }}
              >
                Stop Agent
              </Button>}
            </div>
          </div>
          {!editorContent ? <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6 px-6 w-full h-full">
            {executionSteps.length > 0 && <div className="flex h-[90vh] w-full flex-col items-start gap-2">
              <div className="flex w-full items-center justify-between py-6">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Executing Tasks
                </span>
              </div>
              <div className="flex-1 overflow-auto overflow-auto overscroll-none h-full w-full">
                <div className="w-full h-full px-4 py-4 h-full">  
                  <Feed>
                    {executionSteps.map((step, index) => (<Feed.Item
                      key={index+1}
                      className="w-full"
                      isLast={index === executionSteps.length-1}
                      leftSlot={
                        step.type === 'executing' ? <IconWithBackground size="medium" icon="FeatherLoader" className="spin animate-spin"/> : <IconWithBackground variant="success" size="medium" />
                      }

                      // timestamp="2m ago"
                    >
                      {step.title && <div className="mb-2">
                        <span className="text-body-bold font-body-bold text-default-font" dangerouslySetInnerHTML={{ __html: step.title}}>
                        </span>
                        {step.type === 'executing' && <div><span className="text-body font-body text-subtext-color">
                          executing...
                        </span></div>}
                      </div>}
                      {/* {step.map && <GoogleMapWithHeatmap mapData={step.map} />} */}
                      {step.content && <div className="w-full items-start gap-2 px-8 gap-4 rounded-md bg-neutral-50 py-2" key={index+1} onClick={
                        (e) => {
                          e.preventDefault()
                          if (step.avatar) {
                            nav("/avatar", { 
                              replace: true,
                              state: { 
                                avatar: step.avatar
                              }
                             });
                          } else if (step.link) {
                            loadHtml(step.link)
                          }
                        }}>
                        <span className="text-body text-default-font" dangerouslySetInnerHTML={{ __html: step.content.replaceAll('<pre>', "<pre class='whitespace-pre-wrap'>")}}></span>
                      </div>}
                    </Feed.Item>))}

                    {/* <Feed.Item
                      leftSlot={
                        <IconWithBackground variant="success" size="medium" />
                      }
                      comment={
                        <Feed.Comment>
                          Identify and prioritize High-Value Targets within the 71st
                          ACG..
                        </Feed.Comment>
                      }
                      //timestamp="1m ago"
                    >
                      <span className="text-body-bold font-body-bold text-default-font">
                        Task initialized
                      </span>
                      <span className="text-body font-body text-subtext-color">
                        Task plan loaded successfully
                      </span>
                    </Feed.Item>
                    <Feed.Item
                      leftSlot={
                        <IconWithBackground variant="success" size="medium" />
                      }
                      comment={
                        <CheckboxGroup label="Data Sources" helpText="">
                          <Checkbox
                            label="HUMINT"
                            checked={false}
                            onCheckedChange={(checked: boolean) => {}}
                          />
                          <Checkbox
                            label="OSINT"
                            checked={false}
                            onCheckedChange={(checked: boolean) => {}}
                          />
                          <Checkbox
                            label="SIGINT"
                            checked={false}
                            onCheckedChange={(checked: boolean) => {}}
                          />
                          <Checkbox
                            label="JOINT DATABASE"
                            checked={false}
                            onCheckedChange={(checked: boolean) => {}}
                          />
                        </CheckboxGroup>
                      }
                      timestamp="1m ago"
                    >
                      <span className="text-body-bold font-body-bold text-default-font">
                        Connecting with Data Sources
                      </span>
                      <span className="text-body font-body text-subtext-color">
                        Querying sources....
                      </span>
                    </Feed.Item>
                    <Feed.Item
                      leftSlot={
                        <IconWithBackground size="medium" icon="FeatherLoader" />
                      }
                      comment={
                        <div className="flex w-full flex-col items-start gap-4 rounded-md bg-neutral-50 px-2 py-2">
                          <span className="w-full whitespace-pre-wrap text-monospace-body font-monospace-body text-brand-primary">
                            {
                              'import requests\nimport re\nimport spacy\nfrom bs4 import OSINT\nfrom googlesearch import search\n\n# Load NLP model for entity recognition\nnlp = spacy.load("en_core_web_sm")\n\n# Target organization keyword\ntarget_org = "71st ACG"\n\n# Function to search Google for relevant articles\ndef google_search(query, num_results=5):\n    """Performs a Google search and returns top URLs."""\n    search_results = []\n    for result in search(query, num_results=num_results, lang="en"):\n        search_results.append(result)\n    return search_results'
                            }
                          </span>
                        </div>
                      }
                      timestamp="Just now"
                      isLast={true}
                    >
                      <span className="text-body-bold font-body-bold text-default-font">
                        Processing Data
                      </span>
                      <span className="text-body font-body text-subtext-color">
                        Smart and contextualized search relevant to 71st ACG
                      </span>
                    </Feed.Item> */}
                  </Feed>
                </div>
              </div>
              {/* <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" /> */}
            </div>}
            {/* <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
              <div className="flex w-full items-center justify-between">
                <span className="text-body-bold font-body-bold text-default-font">
                  HVT List
                </span>
                <Badge variant="warning">Pending</Badge>
              </div>
              <div className="flex w-full flex-wrap items-start gap-4">
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
                  <span className="text-caption font-caption text-subtext-color">
                    Token Usage
                  </span>
                  <span className="text-body-bold font-body-bold text-default-font">
                    2.4 K / 1 M
                  </span>
                </div>
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
                  <span className="text-caption font-caption text-subtext-color">
                    API Calls
                  </span>
                  <span className="text-body-bold font-body-bold text-default-font">
                    147 / 1000
                  </span>
                </div>
                <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
                  <span className="text-caption font-caption text-subtext-color">
                    Runtime
                  </span>
                  <span className="text-body-bold font-body-bold text-default-font">
                    4h 23m
                  </span>
                </div>
                <Button
                  variant="neutral-primary"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
                >
                  Open
                </Button>
              </div>
            </div> */}
          </div> : 
          <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6 w-full h-full">
            <DocEditor initialValue={editorContent}/>
          </div>}
        </div> 
      </div>
    </DefaultPageLayout>
  );
}

export default Agents;