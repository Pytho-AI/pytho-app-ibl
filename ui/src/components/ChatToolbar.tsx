"use client";

import React, { useState, useEffect } from "react";
import { Tooltip } from "../subframe/components/Tooltip";
import * as SubframeCore from "@subframe/core";
import { IconButton } from "../subframe/components/IconButton";
import { AiChatToolbar } from "../subframe/components/AiChatToolbar";
import { TextArea } from "../subframe/components/TextArea";
import { Avatar } from "../subframe/components/Avatar";
import { Button } from "../subframe/components/Button";
import { List } from "../subframe/components/List";
import { useAppContext } from "../lib/contextLib";
import Feedback from "./Feedback";
import * as htmlToImage from 'html-to-image';

function formattedDateString(timestamp) {
  const date = new Date(timestamp)

  const formattedDate = date.toLocaleDateString(); // e.g., "12/16/2024"
  const formattedTime = date.toLocaleTimeString(); // e.g., "12:18:05 PM"
  return `${formattedDate} ${formattedTime}`;
}

function getCommentHashKey(message) {
  return message.sessionId + ':' + message.timestamp
}

function ChatToolbar({ commentRef, downloadId, question, answer, onShareClick,
  shouldShowShareButton, shouldShowCommentButton, disableShareButton, message }) {

  const { user, projectId } = useAppContext();
  const [disableFeedback, setDisableFeedback] = useState(false)
  const [showCommentUI, setShowCommentUI] = useState(commentRef ? true : false)
  const [openFeedback, setOpenFeedback] = useState(false)
  const [feedbackMessage, setFeedbackMessage] = useState("")
  const [feedbackId, setFeedbackId] = useState(null)
  const [comments, setComments] = useState([])
  const [isCommenting, setIsCommenting] = useState(false)
  const [users, setUsers] = useState([])
  const [comment, setComment] = useState("")
  const [taggedUsers, setTaggedUsers] = useState([])
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);

  async function submitFeedback(sentiment) {
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-user-email": user.email,
      },
      body: JSON.stringify({
        sentiment,
        question,
        answer,
        project_id: projectId,
      }),
    });
    const data = await response.json();
    setFeedbackId(data.id);
    setDisableFeedback(true)
  }

  return (
    <>
    <AiChatToolbar className="mt-6">
      <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              disabled={disableFeedback}
              size="small"
              icon="FeatherThumbsUp"
              onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                await submitFeedback("liked")
                setOpenFeedback(true)
                setFeedbackMessage("What did you like about this response?")
              }}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Good response</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider>
      <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              disabled={disableFeedback}
              size="small"
              icon="FeatherThumbsDown"
              onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                await submitFeedback("disliked")
                setOpenFeedback(true)
                setFeedbackMessage("How can we make this response better?")
              }}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Bad response</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider>
      {/* <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              size="small"
              icon="FeatherRefreshCw"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Regenerate</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider> */}
      <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              size="small"
              icon="FeatherDownload"
              onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                const divToConvert = document.getElementById(downloadId)
                const html = `
<!DOCTYPE html>
<html>
  <head>
    <style>
      .w-full {
        width: 100%;
      }
      
      .h-full {
        height: 100%;
      }

      .items-start {
        align-items: flex-start;
      }

      .items-center {
      	align-items: center;
      }
      
      .flex-none {
        flex: none;
      }

      .grow {
        flex-grow: 1;
      } 
      
      .shrink-0 {
        flex-shrink: 0;
      } 
      
      .basis-0 {
      	flex-basis: 0px;
      }
      
      .gap-2 {
        gap: 8px; 
      }
      
      .gap-4 {
        gap: 16px;          
      }

      .gap-24 {
        gap: 96px;
      }

      .flex-col {
      	flex-direction: column;
      }

      .self-stretch {
      	align-self: stretch;
      }
      
      .overflow-hidden {
        overflow: hidden;
      }

      .object-cover	{
        object-fit: cover;
      }
      
      .rounded-md {
        border-radius: 0.375rem;
      } 
      
      .border {
        border-width: 1px;
      }
      
      .border-solid {
      	border-style: solid;
      }
      
      .border-neutral-border {
      	border-color: rgb(250 250 250);
      } 
      
      .bg-default-background {
      }
      
      .shadow-sm {
      	box-shadow: 0 1px 2px 0 rgb(0 0 0 / 0.05);
      }

      .text-body-bold {
      } 
      
      .font-body-bold {
      } 
      
      .text-default-font {
      }
      
      .justify-center {
      	justify-content: center;
      }
      
      table {
        margin-top: 1.5rem; 
        width: 100%;	
      }

      th, td {
        border-width: 1px;
        border-style: solid;
      }        
    </style>
  </head>
  <body>
    ${divToConvert?.outerHTML}
  </body>
</html>`
                let converted: Blob = new Blob([html], { type: 'application/msword' });
                let fileName = "pytho-content.doc";
                const downloadLink = document.createElement("a");
                
                const childWithImageClass = divToConvert.querySelector(":scope > .image-download");
                if (childWithImageClass) {
                  converted = await htmlToImage.toBlob(divToConvert, { quality: 1, cacheBust: true })
                  fileName = "pytho-content.png";  
                }              

                const audioSources = divToConvert.querySelectorAll('audio source') 
                if (audioSources.length > 0) {
                  converted = await (await fetch(audioSources[0].src, { mode: 'cors' })).blob()
                  fileName = "pytho-content.mp3";
                }

                const videoSources = divToConvert.querySelectorAll('video source') 
                if (videoSources.length > 0) {
                  converted = await (await fetch(videoSources[0].src, { mode: 'cors' })).blob()
                  fileName = "pytho-content.mp4";
                }
                
                const url = URL.createObjectURL(converted);
                downloadLink.href = url
                downloadLink.download = fileName
                downloadLink.click();
                window.URL.revokeObjectURL(url)
              }}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Download</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider>
      {shouldShowShareButton &&
      <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              // disabled={disableShareButton}
              disabled={true}
              size="small"
              icon="FeatherShare2"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                onShareClick()  
              }}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Share</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider>}
      {shouldShowCommentButton && 
      <SubframeCore.Tooltip.Provider>
        <SubframeCore.Tooltip.Root>
          <SubframeCore.Tooltip.Trigger asChild={true}>
            <IconButton
              disabled={disableFeedback}
              size="small"
              icon="FeatherMessageCircle"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setShowCommentUI(!showCommentUI)
              }}
            />
          </SubframeCore.Tooltip.Trigger>
          <SubframeCore.Tooltip.Portal>
            <SubframeCore.Tooltip.Content
              side="bottom"
              align="center"
              sideOffset={4}
              asChild={true}
            >
              <Tooltip>Comment</Tooltip>
            </SubframeCore.Tooltip.Content>
          </SubframeCore.Tooltip.Portal>
        </SubframeCore.Tooltip.Root>
      </SubframeCore.Tooltip.Provider>}
      <Feedback open={ openFeedback } setOpen={ setOpenFeedback } message={ feedbackMessage } feedbackId={ feedbackId } />
    </AiChatToolbar>
    {showCommentUI &&
    <div className="flex w-full flex-col items-start gap-6 mt-4" ref={commentRef}>
      <div className="flex w-full flex-col items-start gap-4 border-b border-solid border-neutral-border">
        <div className="flex w-full items-center gap-2 border-t border-solid border-neutral-border py-4">
          <TextArea
            className="h-auto grow shrink-0 basis-0"
            label=""
            helpText=""
          >
            <TextArea.Input
              className="min-h-full w-full resize-none overflow-y-hidden"
              rows={1}
              placeholder="Leave a comment here..."
              value={comment}
              onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                setComment(event.target.value)

                let removedTaggedUsers = 0
                while (taggedUsers.length > removedTaggedUsers 
                  && taggedUsers[taggedUsers.length-1-removedTaggedUsers].lastIndex > event.target.value.length) {
                  removedTaggedUsers += 1    
                }

                if (removedTaggedUsers > 0) { // if at least one user is removed
                  const lastRemovedUser = taggedUsers[taggedUsers.length - removedTaggedUsers]
                  // remove the entire user name
                  if (lastRemovedUser.startIndex <= event.target.value.length) {
                    setComment(event.target.value.slice(0, lastRemovedUser.startIndex))
                  }
                  taggedUsers.splice(taggedUsers.length - removedTaggedUsers, removedTaggedUsers)
                  setTaggedUsers(taggedUsers)
                }

                if (event.target.value[event.target.value.length-1] === '@') {
                  setSuggestions(users);
                  setShowSuggestions(true);
                } else if (showSuggestions) {
                  if (!event.target.value.includes("@")) { // user removed @
                    setShowSuggestions(false)
                  } else {
                    const value = event.target.value.slice(event.target.value.indexOf("@")+1);
                    const filteredSuggestions = users.filter(user =>
                      (user.firstName.toLowerCase().includes(value.toLowerCase()) 
                        || user.lastName.toLowerCase().includes(value.toLowerCase()))
                    );
                    if (filteredSuggestions.length === 0) {
                      setShowSuggestions(false)  
                    }
                    setSuggestions(filteredSuggestions);
                  }
                }
              }}
            />
          </TextArea>
          <Button
            variant="brand-secondary"
            loading={isCommenting}
            onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
              setIsCommenting(true)

              const commentDetails = {
                id: getCommentHashKey(message),
                timestamp: Date.now(),
                type: 'message',
                projectId,
                userId: user.userId,
                email: user.email,
                firstName: user.firstName,
                lastName: user.lastName,
                comment,
              }

              const commentorIds = Array.from(new Set(comments.map(comment => comment.userId)))
              const taggedUserIds = Array.from(new Set(taggedUsers.map(t => t.user.userId)))
              const notificationDetails = {
                title: message.title,
                creatorId: message.userId,
                taggedUserIds,
                commentorIds,
              }

              await API.post("activities", `/comments`, {
                body: {
                  comment: commentDetails,
                  notify: notificationDetails,
                }
              });

              setComment("")
              setComments([commentDetails, ...comments])

              setIsCommenting(false)
            }}
          >
            Comment
          </Button>
        </div>
        {showSuggestions && 
        <List className="h-auto">
          {suggestions.map((user, index) => 
          <List.ListItem
            key={index}
            leftSlot={<Avatar>{user.firstName.toUpperCase()[0]+user.lastName.toUpperCase()[0]}</Avatar>}
            onClick={() => {
              setShowSuggestions(false);
              const startIndex = comment.indexOf("@")
              const updatedComment = comment.slice(0, startIndex) + user.firstName + ' ' + user.lastName + ' '
              const lastIndex = updatedComment.length - 1
              setComment(updatedComment)
              setTaggedUsers([...taggedUsers, 
                { user, startIndex, lastIndex }])
            }}
            rightSlot={
              !user.isOwner && <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-neutral-400"
                name="FeatherPlus"
              />
            }
          >
            <span className="w-full text-body-bold font-body-bold text-default-font">
              {user.firstName} {user.lastName}
            </span>
            <span className="w-full text-caption font-caption text-subtext-color">
              { user.email }
            </span>
          </List.ListItem>
          )}
        </List>}
      </div>
      {comments.map((comment, index) => 
      <div key={index} className="flex w-full items-start gap-2">
        <div className="flex flex-col items-center gap-2 self-stretch">
          <Avatar>{comment.firstName.toUpperCase()[0]+comment.lastName.toUpperCase()[0]}</Avatar>
        </div>
        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-caption-bold font-caption-bold text-subtext-color">
              {formattedDateString(comment.timestamp)}
            </span>
          </div>
          <span className="whitespace-pre-wrap text-body font-body text-default-font">
            {comment.comment}
          </span>
        </div>
      </div>)}
      <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-200" />
    </div>}
  </>
  );
}

export default ChatToolbar;