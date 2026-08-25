"use client";
/*
 * Documentation:
 * Chat Message Bar — https://app.subframe.com/0ae0a78e3a6e/library?component=Chat+Message+Bar_9c21d184-68b5-4d91-b85a-3a161659e33a
 * Dropdown Menu — https://app.subframe.com/0ae0a78e3a6e/library?component=Dropdown+Menu_99951515-459b-4286-919e-a89e7549b43b
 * Button — https://app.subframe.com/0ae0a78e3a6e/library?component=Button_3b777358-b86b-40af-9327-891efc6826fe
 * Text Field — https://app.subframe.com/0ae0a78e3a6e/library?component=Text+Field_be48ca43-f8e7-4c0e-8870-d219ea11abfe
 */

import React from "react";
import * as SubframeUtils from "../utils";
import * as SubframeCore from "@subframe/core";
import { DropdownMenu } from "./DropdownMenu";
import { Button } from "./Button";
import { TextArea } from "./TextArea";
import { IconButton } from "./IconButton";
import ViewProductTypes from "../../components/ViewProductTypes";
import { useState } from "react";
import AudioMenu from "../../components/AudioMenu";
// import VideoMenu from "../../components/VideoMenu";
import FocusMode from "../../components/FocusMode";
import ChatSettingsModal from "../../components/ChatSettingsModal";
import MediaModal from "../../components/MediaModal";

interface ChatMessageBarRootProps extends React.HTMLAttributes<HTMLDivElement> {
  className?: string;
  onRequest: boolean;
  getAnswer?: () => void;
  startNewChat?: () => void;
  question?: any;
  setQuestion?: React.Dispatch<React.SetStateAction<any>>;
  audioSettings?: any;
  setAudioSettings?: React.Dispatch<React.SetStateAction<any>>;
  mediaSettings?: any,
  setMediaSettings?: React.Dispatch<React.SetStateAction<any>>;
  videoSettings?: any;
  setVideoSettings?: React.Dispatch<React.SetStateAction<any>>;
  chatSettings?: any;
  setChatSettings?: React.Dispatch<React.SetStateAction<any>>;
  productType: any,
  setProductType?: React.Dispatch<React.SetStateAction<any>>;
  docs?: any;
  dataSources?: any;
  onFocusMode?: any;
  // Manual-dispatch (Generate button) plumbing — Req 4. When the Generate
  // button in ProductLibraryModal succeeds, the parent receives the
  // {artifact_id, htmlLink, docxLink/pptxLink} payload and opens DocEditor.
  onGeneratedProduct?: (result: any) => void;
}

const ChatMessageBarRoot = React.forwardRef<
  HTMLElement,
  ChatMessageBarRootProps
>(function ChatMessageBarRoot(
  { className, onRequest, getAnswer, startNewChat, question, setQuestion, productType, setProductType,
    audioSettings, setAudioSettings, mediaSettings, setMediaSettings, videoSettings, setVideoSettings, chatSettings, setChatSettings,
    docs, dataSources, onFocusMode, onGeneratedProduct, ...otherProps }: ChatMessageBarRootProps,
  ref
) {
  const [mediaType, setMediaType] = useState<string>("");
  const [audioOpen, setAudioOpen] = useState(false);
  const [videoOpen, setVideoOpen] = useState(false);
  const [chatSettingsOpen, setChatSettingsOpen] = useState(false);
  const [focuModeOpen, setFocuModeOpen] = useState(false);
  const [mediaModalOpen, setMediaModalOpen] = useState(false);
  const [selectedMediaType, setSelectedMediaType] = useState<string>("");

  const handleMediaSelection = (type: string, productConfig: any) => {
    setSelectedMediaType(type);
    setMediaModalOpen(true);
    // Temporarily set the product type, will be finalized after modal confirmation
    setVideoSettings(null);
    setAudioSettings(null);
    setMediaType(type);
  };

  const handleMediaModalConfirm = (postCount: number) => {
    // Finalize the product type with post count
    if (selectedMediaType === 'Facebook Post') {
      setProductType({
        productName: 'Facebook Post',
        type: 'facebook_post',
        postCount: postCount
      });
    } else if (selectedMediaType === 'Tweet') {
      setProductType({
        productName: 'Tweet',
        type: 'tweet',
        postCount: postCount
      });
    }
    setMediaModalOpen(false);
  };

  const handleMediaModalCancel = () => {
    setMediaModalOpen(false);
    setMediaType("");
    setSelectedMediaType("");
  };

  const doctrineSelected = question?.productType?.productName === 'Doctrines';

  return (
    <div
      className={SubframeUtils.twClassNames(
        "flex w-full items-center justify-center rounded-sm bg-neutral-100 px-2 py-2",
        className
      )}
      ref={ref as any}
      {...otherProps}
    >
      <div className="flex grow shrink-0 basis-0 flex-col items-start gap-2">
        <div className="flex w-full items-center justify-end gap-2">
          <SubframeCore.Icon
            className="text-body font-body text-default-font"
            name="FeatherMessageCircle"
          />
          <span className="grow shrink-0 basis-0 text-body font-body text-default-font">
            Chat
          </span>
          <Button
            className="h-auto w-auto flex-none"
            variant={docs?.length > 0 ? "brand-primary" : "neutral-secondary"}
            disabled={doctrineSelected}
            onClick={() => {
              setFocuModeOpen(true)
            }}
          >
            Focus
          </Button>
          {question?.productType?.productName === 'Doctrines' ? (
            <Button
              className="h-auto w-auto flex-none"
              variant="brand-primary"
              iconRight="FeatherBookmarkX"
              onClick={() => {
                setProductType('')
              }}
            >
              Doctrines
            </Button>
          ) : (
            <Button
              className="h-auto w-auto flex-none"
              variant="neutral-secondary"
              onClick={() => {
                // Mutually exclusive with media + other product types
                setMediaType("")
                setSelectedMediaType("")
                setAudioSettings(null)
                setVideoSettings(null)
                setProductType({
                  productTypeId: 'builtin-doctrines',
                  productName: 'Doctrines',
                  isAgentic: true,
                  isBuiltin: true,
                  template: { type: 'builtin' },
                })
              }}
            >
              Doctrines
            </Button>
          )}
          {/* <SubframeCore.DropdownMenu.Root>
            <SubframeCore.DropdownMenu.Trigger asChild={true}>
              <Button
                className="h-auto w-auto flex-none"
                iconRight="FeatherChevronDown"
              >
                Avatars
              </Button>
            </SubframeCore.DropdownMenu.Trigger>
            <SubframeCore.DropdownMenu.Portal>
              <SubframeCore.DropdownMenu.Content
                side="bottom"
                align="start"
                sideOffset={2}
                asChild={true}
              >
                <DropdownMenu className="h-auto w-auto flex-none">
                  <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                  >
                    Create an Avatar
                  </DropdownMenu.DropdownItem>
                </DropdownMenu>
              </SubframeCore.DropdownMenu.Content>
            </SubframeCore.DropdownMenu.Portal>
          </SubframeCore.DropdownMenu.Root> */}
          <ViewProductTypes
            productType={question.productType}
            disabled={doctrineSelected}
            setProductType={(pt: any) => {
              // Mutually exclusive with the media pill (Audio/Video/Image/Tweet/Facebook Post)
              if (pt) {
                setMediaType("");
                setSelectedMediaType("");
                setAudioSettings(null);
                setVideoSettings(null);
              }
              setProductType(pt);
            }}
            onGenerated={onGeneratedProduct}
          />
          {audioSettings || videoSettings || mediaType ?
          <Button
            className="h-auto w-auto flex-none"
            variant="brand-primary"
            iconRight="FeatherBookmarkX"
            disabled={doctrineSelected}
            onClick={() => {
              setMediaType(null)
              setVideoSettings(null)
              setAudioSettings(null)
              setProductType('')
              setSelectedMediaType("");
            }}
          >
            {audioSettings ? 'Audio' : videoSettings ? 'Video' : mediaType}
          </Button> :
          <SubframeCore.DropdownMenu.Root>
            <SubframeCore.DropdownMenu.Trigger asChild={true} disabled={doctrineSelected}>
              <Button
                className="h-auto w-auto flex-none"
                variant="neutral-secondary"
                iconRight="FeatherChevronDown"
                disabled={doctrineSelected}
              >
                Media
              </Button>
            </SubframeCore.DropdownMenu.Trigger>
            <SubframeCore.DropdownMenu.Portal>
              <SubframeCore.DropdownMenu.Content
                side="bottom"
                align="start"
                sideOffset={4}
                asChild={true}
              >
                <DropdownMenu className="h-auto w-auto flex-none">
                  {/* <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                    onClick={() => {
                      setAudioSettings(null)
                      setVideoSettings(null)
                      setProductType({
                        productName: 'Avatar',
                        isAgentic: true,
                        template: {
                          type: 'avatar'
                        }
                      })
                    }}
                  >
                    Avatar
                  </DropdownMenu.DropdownItem> */}
                  <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                    onClick={() => {
                      setVideoSettings(null)
                      setAudioOpen(true)
                      
                    }}
                  >
                    Audio
                  </DropdownMenu.DropdownItem>
                  <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                    onClick={() => {
                      setVideoSettings(null)
                      setAudioSettings(null)
                      setMediaType('Image')
                      setProductType({
                        productName: 'Image',
                        type: 'image'
                      })
                    }}
                  >
                    Image
                  </DropdownMenu.DropdownItem>
                  <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                    onClick={() => {
                      handleMediaSelection('Facebook Post', {
                        productName: 'Facebook Post',
                        type: 'facebook_post'
                      });
                    }}
                  >
                    Facebook Post
                  </DropdownMenu.DropdownItem>
                  <DropdownMenu.DropdownItem
                    className="h-auto w-auto flex-none"
                    icon={null}
                    onClick={() => {
                      handleMediaSelection('Tweet', {
                        productName: 'Tweet',
                        type: 'tweet'
                      });
                    }}
                  >
                    Tweets
                  </DropdownMenu.DropdownItem>
                </DropdownMenu>
              </SubframeCore.DropdownMenu.Content>
            </SubframeCore.DropdownMenu.Portal>
          </SubframeCore.DropdownMenu.Root>}
          <Button
            className="h-auto w-auto flex-none"
            variant="neutral-secondary"
            icon="FeatherSettings"
            disabled={productType}
            onClick={() => { 
              setChatSettingsOpen(true);
            }}
          >
            Options
          </Button>
        </div>
        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
        <div className="flex w-full items-center gap-2">
          <TextArea.Input
            className="min-h-full w-full resize-none overflow-y-hidden"
            rows={1}
            disabled={onRequest}
            placeholder={"Start your workflow here..."}
            value={question.prompt}
            onChange={(
              event: React.ChangeEvent<HTMLInputElement>
            ) => 
              setQuestion({
                ...question,
                prompt: event.target.value,
              })
            }
            onKeyUp={async(event: React.MouseEvent<HTMLButtonElement>) => {
              if (event.keyCode === 13) {
                getAnswer()
              }
            }}
          />
          {!onRequest ? <IconButton
            disabled={question.prompt.length < 3}
            //loading={onRequest}
            icon="FeatherArrowRight"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              getAnswer()
            }}
          /> : <IconButton
            size="large"
            icon="FeatherPauseCircle"
            className="spin animate-pulse"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              startNewChat()
            }}
          />}
        </div>
      </div>
      <AudioMenu 
        open={audioOpen} 
        setOpen={setAudioOpen} 
        audioSettings={audioSettings} 
        setAudioSettings={setAudioSettings} 
        setProductType={setProductType} 
      />
      {/* <VideoMenu open={videoOpen} setOpen={setVideoOpen} videoSettings={videoSettings} setVideoSettings={setVideoSettings} />       */}
      <FocusMode 
        open={focuModeOpen} 
        setOpen={setFocuModeOpen} 
        docs={docs} 
        dataSources={dataSources} 
        onFocusMode={onFocusMode} 
      />
      <ChatSettingsModal 
        open={chatSettingsOpen} 
        setOpen={setChatSettingsOpen} 
        chatSettings={chatSettings} 
        setChatSettings={setChatSettings}
      />
      <MediaModal 
        open={mediaModalOpen}
        setOpen={setMediaModalOpen}
        productType={selectedMediaType}
        onConfirm={handleMediaModalConfirm}
        onCancel={handleMediaModalCancel}
        setMediaSettings={setMediaSettings}
      />
    </div>
  );
});

export const ChatMessageBar = ChatMessageBarRoot;
