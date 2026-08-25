"use client";

import React, { useState, useEffect } from "react";
import * as SubframeCore from "@subframe/core";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { Switch } from "../subframe/components/Switch";
import { ToggleGroup } from "../subframe/components/ToggleGroup";
import { Tooltip } from "../subframe/components/Tooltip";
import { DialogLayout } from "../subframe/layouts/DialogLayout";

function ChatSettingsModal({ open, setOpen, chatSettings, setChatSettings }) {
  
  const [currentChatSettings, setCurrentChatSettings] = useState(chatSettings)

  // Sync modal state with parent whenever modal opens or parent settings change
  useEffect(() => {
    if (open) {
      setCurrentChatSettings(chatSettings)
    }
  }, [open, chatSettings])

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Answer Settings
              </span>
              <IconButton
                size="small"
                icon="FeatherX"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setOpen(false)
                }}
              />
            </div>
            <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6 px-6 py-6">
              <div className="flex flex-col items-start gap-1">
                <span className="text-heading-3 font-heading-3 text-default-font">
                  Customize your answers
                </span>
                <span className="text-body font-body text-default-font">
                  Configure how responses are generated and displayed.
                </span>
              </div>
              <div className="flex w-full flex-col items-start gap-6">
                <div className="flex w-full flex-col items-start gap-3">
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-body-bold font-body-bold text-default-font">
                      Creativity
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      Control how creative and imaginative responses are
                    </span>
                  </div>
                  <ToggleGroup value={currentChatSettings.creativity || "Regular"} onValueChange={(value: string) => {
                    setCurrentChatSettings({
                      ...currentChatSettings,
                      creativity: value
                    })
                  }}>
                    <SubframeCore.Tooltip.Provider>
                      <SubframeCore.Tooltip.Root>
                        <SubframeCore.Tooltip.Trigger asChild={true}>
                          <ToggleGroup.Item icon={null} value="Regular">
                            Regular
                          </ToggleGroup.Item>
                        </SubframeCore.Tooltip.Trigger>
                        <SubframeCore.Tooltip.Portal>
                          <SubframeCore.Tooltip.Content side="bottom" align="center" sideOffset={4} asChild={true}>
                            <Tooltip>Sticks to the facts</Tooltip>
                          </SubframeCore.Tooltip.Content>
                        </SubframeCore.Tooltip.Portal>
                      </SubframeCore.Tooltip.Root>
                    </SubframeCore.Tooltip.Provider>
                    <SubframeCore.Tooltip.Provider>
                      <SubframeCore.Tooltip.Root>
                        <SubframeCore.Tooltip.Trigger asChild={true}>
                          <ToggleGroup.Item icon={null} value="Creative">
                            Creative
                          </ToggleGroup.Item>
                        </SubframeCore.Tooltip.Trigger>
                        <SubframeCore.Tooltip.Portal>
                          <SubframeCore.Tooltip.Content side="bottom" align="center" sideOffset={4} asChild={true}>
                            <Tooltip>Adds reasonable inferences and ideas</Tooltip>
                          </SubframeCore.Tooltip.Content>
                        </SubframeCore.Tooltip.Portal>
                      </SubframeCore.Tooltip.Root>
                    </SubframeCore.Tooltip.Provider>
                    <SubframeCore.Tooltip.Provider>
                      <SubframeCore.Tooltip.Root>
                        <SubframeCore.Tooltip.Trigger asChild={true}>
                          <ToggleGroup.Item icon={null} value="Very Creative">
                            Very Creative
                          </ToggleGroup.Item>
                        </SubframeCore.Tooltip.Trigger>
                        <SubframeCore.Tooltip.Portal>
                          <SubframeCore.Tooltip.Content side="bottom" align="center" sideOffset={4} asChild={true}>
                            <Tooltip>Speculates and invents freely</Tooltip>
                          </SubframeCore.Tooltip.Content>
                        </SubframeCore.Tooltip.Portal>
                      </SubframeCore.Tooltip.Root>
                    </SubframeCore.Tooltip.Provider>
                  </ToggleGroup>
                </div>
                <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
                <div className="flex w-full items-center gap-4">
                  <div className="flex grow shrink-0 basis-0 flex-col items-start">
                    <span className="text-body-bold font-body-bold text-default-font">
                      Include sources
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      Show citations and references in answers
                    </span>
                  </div>
                  <Switch
                    checked={currentChatSettings.shouldCiteSources}
                    onCheckedChange={(checked: boolean) => {
                      setCurrentChatSettings({
                        ...currentChatSettings,
                        shouldCiteSources: checked
                      })
                    }}
                  />
                </div>
                <div className="flex w-full items-center gap-4">
                  <div className="flex grow shrink-0 basis-0 flex-col items-start">
                    <span className="text-body-bold font-body-bold text-default-font">
                      Include Related Questions
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      Get helpful follow-up questions
                    </span>
                  </div>
                  <Switch
                    checked={currentChatSettings.includeRelatedQuestions !== false}
                    onCheckedChange={(checked: boolean) => {
                      setCurrentChatSettings({
                        ...currentChatSettings,
                        includeRelatedQuestions: checked
                      })
                    }}
                  />
                </div>
                <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
                <div className="flex w-full flex-col items-start gap-3">
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-body-bold font-body-bold text-default-font">
                      Answer length
                    </span>
                    <span className="text-caption font-caption text-subtext-color">
                      Choose how detailed responses should be
                    </span>
                  </div>
                  <ToggleGroup value={currentChatSettings.answerSize} onValueChange={(value: string) => {
                    setCurrentChatSettings({
                      ...currentChatSettings,
                      answerSize: value
                    })
                  }}>
                    <ToggleGroup.Item icon={null} value="Short">
                      Short
                    </ToggleGroup.Item>
                    <ToggleGroup.Item icon={null} value="Medium">
                      Medium
                    </ToggleGroup.Item>
                    <ToggleGroup.Item icon={null} value="Long">
                      Long
                    </ToggleGroup.Item>
                  </ToggleGroup>
                </div>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpen(false)
                  }}
                >
                  Cancel
                </Button>
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpen(false)
                    setChatSettings(currentChatSettings)
                  }}
                >
                  Save Settings
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

export default ChatSettingsModal;