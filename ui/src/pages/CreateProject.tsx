"use client";

import React, { useEffect, useState, useRef } from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { Tabs } from "../subframe/components/Tabs";
import * as SubframeCore from "@subframe/core";
import { TextField } from "../subframe/components/TextField";
import { Select } from "../subframe/components/Select";
import { TextArea } from "../subframe/components/TextArea";
import { Tooltip } from "../subframe/components/Tooltip";
import { useAppContext } from "../lib/contextLib";
import { uuid } from "../lib/uuid";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { useNavigate } from "react-router-dom";
import { useLocation } from 'react-router-dom';
import { localUpload } from "../components/FileUploader";
import ShareProject from "../components/ShareProject";
import { Checkbox } from "../subframe/components/Checkbox";
import { CheckboxGroup } from "../subframe/components/CheckboxGroup";

import { DialogLayout } from "../subframe/layouts/DialogLayout";

function ConfirmDeleteProject({open, setOpen, projectName, deleteCurrentProject}) {
  const [isLoading, setIsLoading] = useState(false);

  return (
    <DialogLayout open={open} onOpenChange={() => {}}>
      <div className="flex h-full w-full items-start gap-2">
        <div className="flex grow shrink-0 basis-0 items-start gap-4">
          <div className="flex grow shrink-0 basis-0 flex-col items-start self-stretch overflow-hidden rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
            <div className="flex w-full items-center justify-between bg-neutral-100 pl-6 pr-2 py-2">
              <span className="text-caption-bold font-caption-bold text-default-font">
                Delete Event
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
                <span className="text-body font-body text-default-font">
                  Are you sure you want to delete <b>{projectName}</b>?
                </span>
              </div>
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  className="h-8 grow shrink-0 basis-0"
                  variant="neutral-secondary"
                  icon="FeatherArrowLeft"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpen(false)
                  }}
                >
                  Cancel
                </Button>
                <Button
                  loading={isLoading}
                  className="h-8 grow shrink-0 basis-0"
                  iconRight="FeatherArrowRight"
                  onClick={async(event: React.MouseEvent<HTMLButtonElement>) => {
                    setIsLoading(true)
                    await deleteCurrentProject() 
                  }}
                >
                  Confirm
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DialogLayout>
  );
}

function CreateProject() {
  const { user, projectId, setProjectId, projectMap, setProjectMap } = useAppContext();
  const [files, setFiles] = useState([]);
  const [fileUploadError, setFileUploadError] = useState('');
  const [uploading, setUploading] = useState(false)
  const [openConfirmDeleteProject, setOpenConfirmDeleteProject] = useState(false)
  const fileInputRef = useRef(null);

  const allowedTypes = [".pdf", ".docx", ".txt"]

  const [name, setName] = useState("")
  const [baseline, setBaseline] = useState({
    docs: true,
    links: true, 
  })
  const [openShareProject, setOpenShareProject] = useState(false)
  const [nameError, setNameError] = useState("")
  const [eventDate, setEventDate] = useState(new Date().toLocaleDateString('en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit'
  }))
  const [status, setStatus] = useState("Not Started")
  const [description, setDescription] = useState("")
  // Scenario dataset — REQUIRED: every event owns its scenario data
  // (the master map, artifact pool, and product suite all materialize
  // from it at creation). Options come from the shipped dataset
  // documents (/api/scenario_datasets).
  const [scenario, setScenario] = useState("")
  const [scenarioOptions, setScenarioOptions] = useState<string[]>([])
  useEffect(() => {
    fetch("/api/scenario_datasets")
      .then((r) => (r.ok ? r.json() : { datasets: [] }))
      .then((body) => setScenarioOptions(body.datasets || []))
      .catch(() => setScenarioOptions([]))
  }, [])
  const [isLoading, setIsLoading] = useState(false);
  const location = useLocation();
  const nav = useNavigate();

  useEffect(() => {
    if (location.pathname.includes('update')) {
      const project = projectMap[projectId]
      setName(project?.projectDetails?.name)
      setEventDate(project?.projectDetails?.eventDate)
      setDescription(project?.projectDetails?.description)
      setScenario(project?.projectDetails?.scenario || "")
      setStatus(project?.projectDetails?.status)
      setFiles(project?.projectDetails?.docs || [])

      if (project?.projectDetails?.baseline) {
        setBaseline(project?.projectDetails?.baseline)
      }
    }
    
  }, [projectId]);

  async function uploadFiles(files) {
    const droppedFiles = Array.from(files);
    const filteredFiles = droppedFiles.filter(f => allowedTypes.some(t => f.name.toLowerCase().endsWith(t)))
    if (filteredFiles.length != droppedFiles.length) {
      setFileUploadError('Only PDF, .docx, .txt are allowed')
      return
    }


    setUploading(true)
    setFileUploadError('')

    try {
      const docs = []
      for (const file of files) {
        docs.push(await localUpload(file))  
      }
      
      setFiles((prevFiles) => [...prevFiles, ...docs]);
    } catch (e) {
      setFileUploadError("Failed to upload. Please try again.")
    }

    setUploading(false)
  } 

  async function handleFileChange(event) {
    if (event.target.files.length == 0) {
      return
    } 
  
    uploadFiles(event.target.files)
  }

  const handleDragOver = (event) => {
    event.preventDefault();
    event.stopPropagation();
    event.dataTransfer.dropEffect = 'copy'; // Visual cue for drop effect
  };

  const handleDrop = (event) => {
    event.preventDefault();
    event.stopPropagation();
    
    uploadFiles(event.dataTransfer.files)
  };

  async function updateCurrentProject(currentProjectId, shouldDelete=false) {
    const timestamp = Date.now()
    const currentProjectDetails = projectMap[currentProjectId]?.projectDetails

    const projectDetails = {
      projectId: currentProjectId,
      userId: currentProjectDetails?.userId || user.userId,
      baseline,
      docs: files,
      name,
      status,
      email: user.email,
      creator: currentProjectDetails?.creator || {
        email: user.email,
        firstName: user.firstName,
        lastName: user.lastName,
      },
      eventDate: eventDate,
      description,
      scenario,
      isDeleted: shouldDelete,
      createdAt: currentProjectDetails?.createdAt || timestamp,
      updatedAt: timestamp,
    }

    const requestOptions = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        user_id: projectDetails.userId,
        project_id: projectDetails.projectId,
        timestamp: projectDetails.createdAt,
        data: JSON.stringify(projectDetails) 
      })
    };

    await fetch("/api/projects", requestOptions)

    return projectDetails
  }

  async function deleteCurrentProject() {
    const projectDetails = projectMap[projectId]?.projectDetails
    
    const requestOptions = {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json' },
    };

    // Construct the query parameters for user_id (since FastAPI expects it)
    const url = `/api/projects/${encodeURIComponent(projectDetails.projectId)}?user_id=${encodeURIComponent(projectDetails.userId)}`;

    const response = await fetch(url, requestOptions);

    if (!response.ok) {
      const err = await response.text();
      throw new Error(`Failed to delete project: ${err}`);
    }

    
    setProjectId('default')

    delete projectMap[projectId]
    setProjectMap({...projectMap});

    nav("/missioncommand/whitecell");  
  }

  async function handleProjectSave() {
    let error = false
    if (name.length < 1) {
      error = true
      setNameError("Event name is required")
    }
    if (!scenario) {
      error = true
      setNameError("Scenario dataset is required — the event's map, "
        + "order of battle, and products all come from it")
    }

    if (error) return

    setIsLoading(true);
    const currentProjectId = location.pathname.includes('update') ? projectId : uuid()
    
    try {
      const projectDetails = await updateCurrentProject(currentProjectId)

      setProjectId(currentProjectId)
      setProjectMap(projectMap => ({...projectMap, [currentProjectId]: {
        projectDetails  
      }}));

      nav("/missioncommand/whitecell"); 
    } catch (e) {
      if (e instanceof Error) {
        setNameError("Failed to create the event")                
      }
    }

    setIsLoading(false);

  }


  return (
    <DefaultPageLayout>
      <ConfirmDeleteProject open={openConfirmDeleteProject} setOpen={setOpenConfirmDeleteProject} projectName={name} deleteCurrentProject={deleteCurrentProject} />
      <div className="container max-w-none flex h-full w-full flex-col items-start gap-8 bg-default-background py-12">
        <div className="flex w-full flex-col items-start gap-2">
          <span className="text-heading-2 font-heading-2 text-default-font">
            { location.pathname.includes('update') ? "Edit existing event" : "Create a new event" }
          </span>
        </div>
        <div className="flex w-full items-center gap-2">
          <Tabs>
            <Tabs.Item active={true}>Event Details</Tabs.Item>
          </Tabs>
          {location.pathname.includes('update') && projectMap[projectId]?.projectDetails?.userId === user.userId &&
          <SubframeCore.Tooltip.Provider>
            <SubframeCore.Tooltip.Root>
              <SubframeCore.Tooltip.Trigger asChild={true}>
                <IconButton
                  className="-ml-10"
                  icon="FeatherTrash2"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    setOpenConfirmDeleteProject(true)
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
                  <Tooltip>Delete This Event</Tooltip>
                </SubframeCore.Tooltip.Content>
              </SubframeCore.Tooltip.Portal>
            </SubframeCore.Tooltip.Root>
          </SubframeCore.Tooltip.Provider>}
        </div>
        <div className="flex w-full items-start gap-6">
          <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6">
            <div className="flex w-full items-center justify-end gap-2">
              <Button
                disabled={true}
                className="h-6 w-24 flex-none"
                variant="brand-secondary"
                icon="FeatherUserPlus"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setOpenShareProject(true)
                }}
              >
                Add Users
              </Button>
            </div>
            {location.pathname.includes('update') && <ShareProject 
              open={openShareProject} 
              setOpen={setOpenShareProject} 
              project={projectMap[projectId]?.projectDetails}
              sharer={user}
            />}
            <div className="flex w-full flex-col items-start gap-6 rounded-md border border-solid border-neutral-border bg-default-background px-6 pt-4 pb-6 shadow-sm">
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6">
                <div className="flex w-full items-start gap-4">
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4">
                    <TextField
                      className="h-auto w-full flex-none"
                      label="Event Name"
                      helpText={nameError}
                      error={nameError.length > 0}
                    >
                      <TextField.Input
                        placeholder="Enter an event name"
                        value={name}
                        onChange={(
                          event: React.ChangeEvent<HTMLInputElement>
                        ) => {
                          setNameError("")
                          setName(event.target.value)
                        }}
                      />
                    </TextField>
                  </div>
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4">
                    <TextField
                      className="h-auto w-full flex-none"
                      label="Event Date"
                      helpText=""
                    > 
                      <TextField.Input
                        value={eventDate}
                        className="[color-scheme:dark]" 
                        type="date"
                        onChange={(
                          event: React.ChangeEvent<HTMLInputElement>
                        ) => {
                          setEventDate(event.target.value)
                        }}
                      />
                    </TextField>
                  </div>
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1">
                    <Select
                      className="h-auto w-full flex-none"
                      label="Status"
                      placeholder="Select Placeholder"
                      helpText=""
                      value={status}
                      onValueChange={(value: string) => {setStatus(value)}}
                    >
                      <div className="flex w-full flex-col items-start">
                        <Select.Item value="Not Started">Not Started</Select.Item>
                        <Select.Item value="In Progress">In Progress</Select.Item>
                        <Select.Item value="Completed">Completed</Select.Item>
                      </div>
                    </Select>
                  </div>
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-1">
                    <Select
                      className="h-auto w-full flex-none"
                      label="Scenario"
                      placeholder="Select scenario dataset"
                      helpText="The event's scenario data — map, order of battle, docs, and products all materialize from it at creation"
                      value={scenario}
                      onValueChange={(value: string) => {setScenario(value)}}
                    >
                      <div className="flex w-full flex-col items-start">
                        {scenarioOptions.map((slug) => (
                          <Select.Item key={slug} value={slug}>{slug}</Select.Item>
                        ))}
                      </div>
                    </Select>
                  </div>
                </div>
                <div className="flex w-full items-start gap-4">
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4">
                    <TextArea
                      className="h-auto min-h-[96px] w-full flex-none"
                      label="Description"
                    >
                      <TextArea.Input
                        className="resize-none"
                        placeholder="Briefly describe your event"
                        value={description}
                        onChange={(
                          event: React.ChangeEvent<HTMLTextAreaElement>
                        ) => {
                          setDescription(event.target.value)
                        }}
                      />
                    </TextArea>
                  </div>
                </div>
              </div>
              <div className="flex w-full flex-col items-start rounded-md border border-solid border-neutral-border bg-default-background shadow-sm">
                <div className="flex w-full items-center gap-2 border-b border-solid border-neutral-border px-6 py-6">
                  <span className="grow shrink-0 basis-0 text-heading-3 font-heading-3 text-default-font text-center">
                    Event Specific Data
                  </span>
                </div>
                <div className="flex w-full flex-col items-start gap-3 px-6 py-6">
                  <div className="flex items-center gap-2">
                    <SubframeCore.Icon
                      className="text-heading-3 font-heading-3 text-default-font"
                      name="FeatherDatabase"
                    />
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      Baseline Model Data
                    </span>
                  </div>
                  <CheckboxGroup
                    label="Turn on:"
                    helpText="Enabling these options will include them in your project-level queries, in addition to data added below."
                  >
                    <div className="flex grow shrink-0 basis-0 items-center justify-end">
                      <Checkbox
                        label="Static Documents"
                        checked={baseline.docs}
                        onCheckedChange={(checked: boolean) => {
                          setBaseline({...baseline, docs: !baseline.docs})
                        }}
                      />
                      <IconButton
                        size="medium"
                        icon="FeatherArrowRight"
                        onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                          window.open("/missioncommand/data/static", '_blank')  
                        }}
                      />
                    </div>
                    <div className="flex grow shrink-0 basis-0 items-center justify-end">
                      <Checkbox
                        disabled
                        label="White-Listed Domains"
                        checked={false}
                        onCheckedChange={(checked: boolean) => {
                          setBaseline({...baseline, links: !baseline.links})
                        }}
                      />
                      <IconButton
                        disabled
                        size="medium"
                        icon="FeatherArrowRight"
                      />
                    </div>
                  </CheckboxGroup>
                  <div className="flex h-px w-full flex-none flex-col items-center gap-2 bg-neutral-border" />
                  <div className="flex w-full items-center gap-2 py-4">
                    <SubframeCore.Icon
                      className="text-heading-2 font-heading-2 text-default-font"
                      name="FeatherFiles"
                    />
                    <span className="text-heading-3 font-heading-3 text-default-font">
                      Upload Event-Level Documents
                    </span>
                    <span className="text-body font-body text-subtext-color">
                      PDF, .docx, .txt etc
                    </span>
                  </div>
                  <div className="flex w-full flex-col items-start gap-4">
                    <TextArea 
                      className="w-full rounded-md"
                      helpText={fileUploadError}
                      error={fileUploadError.length > 0}
                    >
                      <div 
                        className="flex w-full flex-col items-center justify-center gap-2 rounded-md px-6 py-6 cursor-pointer"
                        onDragOver={handleDragOver}
                        onDrop={handleDrop}
                        onClick={() =>  fileInputRef.current.click()}
                      >
                        {uploading ? 
                        <Button
                          size="large"
                          variant="brand-secondary"
                          loading={uploading}
                          icon="FeatherTrash"
                        /> : 
                        <SubframeCore.Icon
                          className="text-heading-1 font-heading-1 text-brand-700"
                          name="FeatherUploadCloud"
                        />}    
                        <div className="flex flex-col items-center justify-center gap-1">
                          <span className="text-body font-body text-default-font text-center">
                            Click to select files or drag to upload
                          </span>
                          <span className="text-caption font-caption text-subtext-color text-center">
                            Up to 100 files, max file size 5MB
                          </span>
                        </div>
                      </div>
                    </TextArea>
                    <div className="flex w-full flex-col items-start gap-2">
                      {files.map((file, index)=> 
                      <div key={index} className="flex w-full items-center gap-2 rounded-md bg-neutral-50 px-2 py-2">
                        <div className="flex items-center gap-4 px-1 py-1">
                          <SubframeCore.Icon
                            className="text-body font-body text-brand-700"
                            name="FeatherCheckCircle"
                          />
                        </div>
                        <span className="grow shrink-0 basis-0 text-caption-bold font-caption-bold text-default-font">
                          {file.name}
                        </span>
                        <IconButton
                          size="small"
                          icon="FeatherTrash"
                          onClick={(
                            event: React.MouseEvent<HTMLButtonElement>
                          ) => {
                            files.splice(index, 1);
                            setFiles([...files])
                          }}
                        />
                      </div>)}
                    </div>
                  </div>
                </div>
              </div>
              
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  variant="brand-secondary"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    nav("/missioncommand/whitecell");
                  }}
                >
                  Cancel
                </Button>
                <Button
                  loading={isLoading}
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                    handleProjectSave()
                  }}
                >
                  Save
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
      <input
        ref={fileInputRef}
        type="file"
        hidden
        accept=".pdf,.docx,.txt"
        multiple
        onChange={handleFileChange}
      />
    </DefaultPageLayout>
  );
}

export default CreateProject;