"use client";

import React, { useState } from "react";
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { IconButton } from "../subframe/components/IconButton";
import { DropdownMenu } from "../subframe/components/DropdownMenu";
import * as SubframeCore from "@subframe/core";
import { Button } from "../subframe/components/Button";
import { Slider } from "../subframe/components/Slider";
import { TextArea } from "../subframe/components/TextArea";


const audioChoices = [
  {
    language: {
      name: "English (US)",
      code: "en-US"  
    },
    voices: [
      {
        gender: "female",
        code: "af_bella"
      },
      {
        gender: "male",
        code: "am_michael"
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
        code: "bf_emma"
      },
      {
        gender: "male",
        code: "bm_lewis"
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
        code: "ff_siwis"
      }
    ]
  },
  {
    language: {
      name: "Hindi (India)",
      code: "hi-IN"
    },
    voices: [
      {
        gender: "female",
        code: "hf_alpha"
      },
      {
        gender: "male",
        code: "hm_omega"
      }
    ]
  },
  {
    language: {
      name: "Italian (Italy)",
      code: "it-IT"
    },
    voices: [
      {
        gender: "female",
        code: "if_sara"
      },
      {
        gender: "male",
        code: "im_nicola"
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
        code: "jf_alpha"
      },
      {
        gender: "male",
        code: "jm_kumo"
      }
    ]
  },
  {
    language: {
      name: "Mandarin (China)",
      code: "cmn-CN"
    },
    voices: [
      {
        gender: "female",
        code: "zf_xiaobei"
      },
      {
        gender: "male",
        code: "zm_yunjian"
      }
    ]
  },
  {
    language: {
      name: "Portuguese (Brazil)",
      code: "pt-BR"
    },
    voices: [
      {
        gender: "female",
        code: "pf_dora"
      },
      {
        gender: "male",
        code: "pm_alex"
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
        code: "ef_dora"
      },
      {
        gender: "male",
        code: "em_alex"
      }
    ]
  }
]

const audioIndex = 0
const voiceIndex = 0

const defaultAudioSettings = {
  audioIndex,
  voiceIndex,
  transcript: {
    type: 'model',
    text: '',
  },
  language: audioChoices[audioIndex].language,
  voice: audioChoices[audioIndex].voices[voiceIndex],
  speed: 1.0
}


function AudioMenu({ open, setOpen, audioSettings, setAudioSettings, setProductType }) {
  const [currentAudioSettings, setCurrentAudioSettings] = useState(audioSettings ? audioSettings : defaultAudioSettings)
  const [transcriptError, setTranscriptError] = useState('');
 
  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full max-h-[768px] w-full flex-col items-start gap-2">
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
                Speech Settings
              </span>
            </div>
          </div>
          <div className="flex w-full flex-col items-start gap-4 px-8 py-8 overflow-auto">
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
                variant={currentAudioSettings.voiceIndex && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentAudioSettings({
                    ...currentAudioSettings,
                    voice: audioChoices[currentAudioSettings.audioIndex].voices[0],
                    voiceIndex: 0,
                  })
                }}
              >
                Female
              </Button>
              <Button
                className="h-8 grow shrink-0 basis-0"
                disabled={audioChoices[currentAudioSettings.audioIndex].voices.length < 2}
                variant={!currentAudioSettings.voiceIndex && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentAudioSettings({
                    ...currentAudioSettings,
                    voice: audioChoices[currentAudioSettings.audioIndex].voices[1],
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
                  className="w-[195px]"
                  variant="neutral-secondary"
                  iconRight="FeatherChevronDown"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
                >
                  {currentAudioSettings?.language?.name}
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
                      setCurrentAudioSettings({
                        ...currentAudioSettings,
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
            <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-300  mt-4" />
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
                variant={currentAudioSettings.transcript.type !== 'model' && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentAudioSettings({
                    ...currentAudioSettings,
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
                variant={currentAudioSettings.transcript.type !== 'custom' && "brand-secondary"}
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setCurrentAudioSettings({
                    ...currentAudioSettings,
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
            {currentAudioSettings.transcript.type === 'custom' &&
            <TextArea className="h-auto w-full flex-none" label="" 
              helpText={transcriptError}
              error={transcriptError.length > 0}
            >
              <TextArea.Input
                className="h-auto min-h-[96px] w-full flex-none resize-none"
                placeholder="Copy and paste your transcript here..."
                value={currentAudioSettings.transcript.text}
                onChange={(event: React.ChangeEvent<HTMLTextAreaElement>) => {
                  if (event.target.value.length > 1500) {
                    setTranscriptError('Please enter less than 1,500 characters')
                    return
                  } else { 
                    setTranscriptError('')
                  }
                  setCurrentAudioSettings({
                    ...currentAudioSettings,
                    transcript: {
                      type: 'custom',
                      text: event.target.value
                    }
                  })
                }}
              />
            </TextArea>}

            {/* <div className="flex w-full flex-col items-start gap-6">
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
                  value={[currentAudioSettings.speed]}
                  onValueChange={(value: number[]) => {
                    setCurrentAudioSettings({
                      ...currentAudioSettings,
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
                setAudioSettings(currentAudioSettings)
                setProductType({
                  productName: 'Audio',
                  type: 'Audio'
                })
              }}
            >
              Save
            </Button>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default AudioMenu;