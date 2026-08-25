"use client";

import React, { useState, useRef } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import { DropdownMenu } from "../subframe/components/DropdownMenu";
import * as SubframeCore from "@subframe/core";
import { Button } from "../subframe/components/Button";
import { TextArea } from "../subframe/components/TextArea";
import { s3Upload } from "../components/FileUploader"
import { Storage } from "aws-amplify";

//import { Slider } from "../subframe/components/Slider";


const audioChoices = [
  {
    language: {
      name: "English (US)",
      code: "en-US"  
    },
    voices: [
      {
        gender: "female",
        code: "en-US-Journey-O"
      },
      {
        gender: "male",
        code: "en-US-Journey-D"
      }
    ]
  },
  {
    language: {
      name: "English (UK)",
      code: "en-GB"  
    },
    voices: [
      {
        gender: "female",
        code: "en-GB-Standard-A"
      },
      {
        gender: "male",
        code: "en-GB-Standard-D"
      }
    ]
  },
  {
    language: {
      name: "French (France)",
      code: "fr-FR"  
    },
    voices: [
      {
        gender: "female",
        code: "fr-FR-Standard-A"
      },
      {
        gender: "male",
        code: "fr-FR-Standard-D"
      }
    ]
  },
  {
    language: {
      name: "Japanese (Japan)",
      code: "ja-JP"  
    },
    voices: [
      {
        gender: "female",
        code: "ja-JP-Standard-A"
      },
      {
        gender: "male",
        code: "ja-JP-Standard-C"
      }
    ]
  },
  {
    language: {
      name: "Korean (South Korea)",
      code: "ko-KR"  
    },
    voices: [
      {
        gender: "female",
        code: "ko-KR-Standard-A"
      },
      {
        gender: "male",
        code: "ko-KR-Standard-C"
      }
    ]
  },
  {
    language: {
      name: "Russian (Russia)",
      code: "ru-RU"  
    },
    voices: [
      {
        gender: "female",
        code: "ru-RU-Standard-A"
      },
      {
        gender: "male",
        code: "ru-RU-Standard-D"
      }
    ]
  },
  {
    language: {
      name: "Spanish (Spain)",
      code: "es-ES"  
    },
    voices: [
      {
        gender: "female",
        code: "es-ES-Standard-A"
      },
      {
        gender: "male",
        code: "es-ES-Standard-B"
      }
    ]
  },
  {
    language: {
      name: "Thai (Thailand)",
      code: "th-TH"  
    },
    voices: [
      {
        gender: "female",
        code: "th-TH-Standard-A"
      }
    ]
  },
  {
    language: {
      name: "Ukrainian (Ukraine)",
      code: "uk-UA"  
    },
    voices: [
      {
        gender: "female",
        code: "uk-UA-Standard-A"
      }
    ]
  }
]

const audioIndex = 0
const voiceIndex = 0

const defaultVideoSettings = {
  audioIndex,
  voiceIndex,
  transcript: {
    type: 'model',
    text: '',
  },
  images: {
    type: 'model',
    imageList: [],
  },
  language: audioChoices[audioIndex].language,
  voice: audioChoices[audioIndex].voices[voiceIndex],
  speed: 1.0
}


function VideoMenu({ videoSettings, setVideoSettings, setAudioSettings, docs }) {
  const [fileUploadError, setFileUploadError] = useState('');
  const [transcriptError, setTranscriptError] = useState('');
  const [uploading, setUploading] = useState(false)
  const fileInputRef = useRef(null);
  const [currentVideoSettings, setCurrentVideoSettings] = useState(videoSettings ? videoSettings : defaultVideoSettings)
  const [open, setOpen] = useState(false)

  async function uploadFiles(files) {
    setUploading(true)
    setFileUploadError('')

    try {
      const images = []
      for (const file of files) {
        const key = await s3Upload(file);
        const url = await Storage.get(key, {
          level: 'public', 
          expires: 604800, // 7 days
        });

        const name = file.name
        images.push({ key, url, name })  
      }
      
      const currentImages = currentVideoSettings.images.imageList
      setCurrentVideoSettings({
        ...currentVideoSettings,
        images: {
          type: 'custom',
          imageList: currentImages.concat(images),
        }
      })
    } catch (e) {
      setFileUploadError("Failed to upload. Please try again.")
    }

    setUploading(false)
  } 

  async function handleFileChange(event) {
    if (event.target.files.length == 0) {
      return
    } 
  
    if (event.target.files.length > 6) {
      setFileUploadError('Please select at most 6 images')
      return
    }

    uploadFiles(event.target.files)
  }
  
  return (
    <>
    <div className={videoSettings? "border-solid border-b" : ""}>
      <IconButton
        disabled
        size="medium"
        icon="FeatherVideo" 
        onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
          if (!videoSettings) { 
            setOpen(true) 
          } else {
            setVideoSettings(null)  
          }
        }}
      />
    </div>
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full max-h-[768px] w-full min-w-[390px] flex-col items-start gap-2">
        <div className="flex h-144 w-full flex-none flex-col items-center">
          <div className="flex w-full items-center border-b border-solid border-neutral-border px-4 py-4">
            <IconButton
              variant="neutral-tertiary"
              size="medium"
              icon="FeatherX"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpen(false)
              }}
            />
            <div className="flex grow shrink-0 basis-0 items-center justify-center gap-2">
              <span className="text-heading-2 font-heading-2 text-default-font">
                Video Settings
              </span>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-4 px-8 py-8 overflow-auto">
            <SubframeCore.DropdownMenu.Root>
              <SubframeCore.DropdownMenu.Trigger asChild={true}>
                <div className="flex w-full flex-col items-start gap-6">
                  <span className="w-full text-heading-3 font-heading-3 text-default-font">
                    Transcript
                  </span>
                </div>
              </SubframeCore.DropdownMenu.Trigger>
            </SubframeCore.DropdownMenu.Root>
            <div className="flex w-full items-center justify-end gap-2">
              <Button
                className="h-8 grow shrink-0 basis-0"
                variant={currentVideoSettings.transcript.type !== 'model' && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    transcript: {
                      type: 'model',
                      text: ''
                    }
                  })
                }}
              >
                Model Generated
              </Button>
              <Button
                className="h-8 grow shrink-0 basis-0"
                disabled={docs?.length > 0}
                variant={currentVideoSettings.transcript.type !== 'custom' && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    transcript: {
                      type: 'custom',
                      text: ''
                    }
                  })
                }}
              >
                Custom
              </Button>
            </div>
            {currentVideoSettings.transcript.type === 'custom' &&
            <TextArea className="h-auto w-full flex-none" label="" 
              helpText={transcriptError}
              error={transcriptError.length > 0}
            >
              <TextArea.Input
                className="h-auto min-h-[96px] w-full flex-none resize-none"
                placeholder="Copy and paste your transcript here..."
                value={currentVideoSettings.transcript.text}
                onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => {
                  if (event.target.value.length > 1500) {
                    setTranscriptError('Please enter less than 1,500 characters')
                    return
                  } else { 
                    setTranscriptError('')
                  }
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    transcript: {
                      type: 'custom',
                      text: event.target.value
                    }
                  })
                }}
              />
            </TextArea>}

            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300 mt-4" />
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              Images
            </span>
            <div className="flex w-full items-center justify-end gap-2">
              <Button
                className="h-8 grow shrink-0 basis-0"
                variant={currentVideoSettings.images.type !== 'model' && "brand-secondary"}
                onClick={(
                  event: React.MouseEvent<HTMLButtonElement>
                ) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    images: {
                      type: 'model',
                      imageList: [],
                    }
                  })
                }}
              >
                Model Generated
              </Button>
              <Button
                className="h-8 grow shrink-0 basis-0"
                variant={currentVideoSettings.images.type !== 'custom' && "brand-secondary"}
                onClick={(
                  event: React.MouseEvent<HTMLButtonElement>
                ) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    images: {
                      type: 'custom',
                      imageList: [],
                    }
                  })
                }}
              >
                Custom
              </Button>
            </div>
            {currentVideoSettings.images.type === 'custom' &&
            <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
              <div className="flex w-full flex-col items-start gap-4 px-6 py-6">
                <TextArea 
                  className="w-full rounded-md"
                  helpText={fileUploadError}
                  error={fileUploadError.length > 0}
                >
                  <div className="flex w-full flex-col items-center justify-center gap-2 rounded-md px-2 py-6 cursor-pointer"
                    onClick={()=>fileInputRef.current.click()}
                  > 
                    {uploading? 
                    <Button
                      size="large"
                      variant="brand-secondary"
                      loading={uploading}
                      icon="FeatherTrash"
                    />: 
                    <SubframeCore.Icon
                      className="text-heading-1 font-heading-1 text-brand-700"
                      name="FeatherUploadCloud"
                    />}

                    <div className="flex flex-col items-center justify-center gap-1">
                      <span className="text-body font-body text-default-font text-center">
                        Click to select images (.jpeg / .png) 
                      </span>
                      <span className="text-caption font-caption text-subtext-color text-center">
                        Up to 6 images, max file size 5MB
                      </span>
                    </div>
                  </div>
                </TextArea>
                <div className="flex w-full flex-col items-start gap-2">
                  {currentVideoSettings.images.imageList.map((image, index) =>
                  <div key={index} className="flex w-full items-center gap-2 rounded-md bg-neutral-50 px-2 py-2">
                    <div className="flex items-center gap-4 px-1 py-1">
                      <SubframeCore.Icon
                        className="text-body font-body text-brand-700"
                        name="FeatherCheckCircle"
                      />
                    </div>
                    <span className="grow shrink-0 basis-0 text-caption-bold font-caption-bold text-default-font">
                      {image.name}
                    </span>
                    <IconButton
                      size="small"
                      icon="FeatherTrash"
                      onClick={(
                        event: React.MouseEvent<HTMLButtonElement>
                      ) => {
                        currentVideoSettings.images.imageList.splice(index, 1);
                        setCurrentVideoSettings({
                          ...currentVideoSettings,
                          images: {
                            type: 'custom',
                            imageList: currentVideoSettings.images.imageList,
                          }
                        })
                      }}
                    />
                  </div>)}
                </div>
              </div>
            </div>}
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300 mt-4" />
            <SubframeCore.DropdownMenu.Root>
              <SubframeCore.DropdownMenu.Trigger asChild={true}>
                <div className="flex w-full flex-col items-start gap-6">
                  <span className="w-full text-heading-3 font-heading-3 text-default-font">
                    Voice
                  </span>
                </div>
              </SubframeCore.DropdownMenu.Trigger>
            </SubframeCore.DropdownMenu.Root>
            <div className="flex w-full items-center justify-end gap-2">
              <Button
                className="h-8 grow shrink-0 basis-0"
                variant={currentVideoSettings.voiceIndex && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    voice: audioChoices[currentVideoSettings.audioIndex].voices[0],
                    voiceIndex: 0,
                  })
                }}
              >
                Female
              </Button>
              <Button
                className="h-8 grow shrink-0 basis-0"
                disabled={audioChoices[currentVideoSettings.audioIndex].voices.length < 2}
                variant={!currentVideoSettings.voiceIndex && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentVideoSettings({
                    ...currentVideoSettings,
                    voice: audioChoices[currentVideoSettings.audioIndex].voices[1],
                    voiceIndex: 1,
                  })
                }}
              >
                Male
              </Button>
            </div>
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300 mt-4" />
            <div className="flex w-full flex-col items-start gap-6">
              <span className="w-full text-heading-3 font-heading-3 text-default-font">
                Language
              </span>
            </div>
            <SubframeCore.DropdownMenu.Root>
              <SubframeCore.DropdownMenu.Trigger asChild={true}>
                <Button
                  className="w-[195px] py-4"
                  variant="neutral-secondary"
                  iconRight="FeatherChevronDown"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
                >
                  {currentVideoSettings?.language?.name}
                </Button>
              </SubframeCore.DropdownMenu.Trigger>
              <SubframeCore.DropdownMenu.Portal>
                <SubframeCore.DropdownMenu.Content
                  side="bottom"
                  align="start"
                  sideOffset={4}
                  asChild={true}
                >
                  <DropdownMenu>
                    {audioChoices.map((audio, index) => 
                    <DropdownMenu.DropdownItem key={index} icon={null} onClick={() => 
                      setCurrentVideoSettings({
                        ...currentVideoSettings,
                        language: audio.language,
                        voice: audio.voices[0],
                        audioIndex: index,
                        voiceIndex: 0,
                      })
                    }>
                      {audio.language.name}
                    </DropdownMenu.DropdownItem>
                    )}
                  </DropdownMenu>
                </SubframeCore.DropdownMenu.Content>
              </SubframeCore.DropdownMenu.Portal>
            </SubframeCore.DropdownMenu.Root>
            {/* <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300  mt-4" />
            <div className="flex w-full flex-col items-start gap-6">
              <span className="w-full text-heading-3 font-heading-3 text-default-font">
                Speech Speed
              </span>
              <div className="flex w-full flex-col items-start gap-1">
                <span className="text-caption font-caption text-subtext-color">
                  Use the slider to choose your audio speed.
                </span>
              </div>
              <div className="flex w-full flex-col items-start gap-4">
                <Slider
                  min={0.25}
                  max={1.75}
                  value={[currentVideoSettings.speed]}
                  onValueChange={(value: number[]) => {
                    setCurrentVideoSettings({
                      ...currentVideoSettings,
                      speed: value[0]
                    })
                  }}
                />
                <div className="flex w-full items-center justify-between">
                  <span className="text-caption font-caption text-subtext-color">
                    Slow
                  </span>
                  <span className="text-caption font-caption text-subtext-color">
                    Fast
                  </span>
                </div>
              </div>
            </div> */}
            {/* <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
            <div className="flex w-full flex-col items-start gap-6">
              <span className="w-full text-heading-3 font-heading-3 text-default-font">
                Length
              </span>
            </div>
            <div className="flex w-full items-center justify-end gap-2">
              <Button
                className="h-8 grow shrink-0 basis-0"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              >
                Default
              </Button>
              <Button
                disabled
                className="h-8 grow shrink-0 basis-0"
                variant="neutral-primary"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              >
                30 sec
              </Button>
              <Button
                disabled
                className="h-8 grow shrink-0 basis-0"
                variant="neutral-primary"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              >
                60 sec
              </Button>              
            </div>
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
            <div className="flex w-full flex-col items-start gap-6">
              <span className="w-full text-heading-3 font-heading-3 text-default-font">
                Style
              </span>
            </div>
            <SubframeCore.DropdownMenu.Root>
              <SubframeCore.DropdownMenu.Trigger asChild={true}>
                <Button
                  disabled
                  className="w-[195px]"
                  variant="neutral-secondary"
                  iconRight="FeatherChevronDown"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
                >
                  News
                </Button>
              </SubframeCore.DropdownMenu.Trigger>
              <SubframeCore.DropdownMenu.Portal>
                <SubframeCore.DropdownMenu.Content
                  side="bottom"
                  align="start"
                  sideOffset={4}
                  asChild={true}
                >
                  <DropdownMenu>
                    <DropdownMenu.DropdownItem icon={null}>
                      News
                    </DropdownMenu.DropdownItem>
                    <DropdownMenu.DropdownItem icon={null} disabled>
                      Cinematic
                    </DropdownMenu.DropdownItem>
                    <DropdownMenu.DropdownItem icon={null}>
                      Documentary
                    </DropdownMenu.DropdownItem>
                    <DropdownMenu.DropdownItem icon={null}>
                      Animation
                    </DropdownMenu.DropdownItem>
                  </DropdownMenu>
                </SubframeCore.DropdownMenu.Content>
              </SubframeCore.DropdownMenu.Portal>
            </SubframeCore.DropdownMenu.Root> */}
          </div>
        </div>
        <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300" />
        <div className="flex h-11 w-full flex-none items-center justify-end gap-2 pr-2 pb-2">
          <div className="flex grow shrink-0 basis-0 items-center justify-end gap-2">
            <Button
              variant="brand-secondary"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpen(false)
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                setOpen(false)
                setVideoSettings(currentVideoSettings)
                setAudioSettings(null)
              }}
            >
              Save
            </Button>
          </div>
        </div>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        hidden
        accept=".jpeg, .jpg, .png"
        multiple
        onChange={handleFileChange}
      />
    </DialogLayout>
    </>
  );
}

export default VideoMenu;