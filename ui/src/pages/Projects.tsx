"use client";

import React from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import * as SubframeCore from "@subframe/core";
import { IconButton } from "../subframe/components/IconButton";
import { Table } from "../subframe/components/Table";
import { Badge } from "../subframe/components/Badge";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../lib/contextLib";


function convertDateToLocalTime(dateString) {
  var date = new Date(dateString)
  var userTimezoneOffset = date.getTimezoneOffset() * 60000;
  return new Date(date.getTime() + userTimezoneOffset).toLocaleDateString();
}
function Projects() {
  const nav = useNavigate()
  const { projectMap, setProjectId } = useAppContext();

  function displayProjects() {
    const projects = []
    
    for (const projectId in projectMap) {
      const project = projectMap[projectId]
      if (project.projectDetails) {
        projects.push(project.projectDetails)  
      }
    }

    projects.sort((a, b) => b.createdAt - a.createdAt);
    
    return (
      <Table
        header={
          <Table.HeaderRow>
            <Table.HeaderCell />
            <Table.HeaderCell>Name</Table.HeaderCell>
            <Table.HeaderCell>Status</Table.HeaderCell>
            <Table.HeaderCell>Event Date</Table.HeaderCell>
            <Table.HeaderCell className="h-auto grow shrink-0 basis-0 self-stretch">
              Edit
            </Table.HeaderCell>
            
          </Table.HeaderRow>
        }
      >
        {projects.map((project, index) => (
        <Table.Row key={index}>
          <Table.Cell>
            <span className="grow shrink-0 basis-0 whitespace-nowrap text-body font-body text-neutral-500 text-center">
              {index + 1}
            </span>
          </Table.Cell>
          <Table.Cell>
            <div className="flex items-center gap-2">
              <SubframeCore.Icon
                className="text-heading-3 font-heading-3 text-subtext-color"
                name="FeatherClipboardList"
              />
              <span className="whitespace-nowrap text-body-bold font-body-bold text-neutral-700">
                {project.name}
              </span>
            </div>
          </Table.Cell>
          <Table.Cell>
            <Badge variant={project.status == "Completed"? "success": "warning"}>{project.status}</Badge>
          </Table.Cell>
          <Table.Cell>
            <span className="whitespace-nowrap text-body font-body text-neutral-500">
              { convertDateToLocalTime(project.eventDate) }
            </span>
          </Table.Cell>
          <Table.Cell className="h-auto w-auto flex-none">
            <div className="flex items-center justify-end">
              <IconButton
                size="medium"
                icon="FeatherEdit3"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setProjectId(project.projectId)
                  nav("/missioncommand/events/update");
                }}
              />
            </div>
          </Table.Cell>
          <Table.Cell className="h-12 w-auto flex-none">
            <div className="flex grow shrink-0 basis-0 items-center justify-end">
              <IconButton
                size="medium"
                icon="FeatherArrowRight"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  setProjectId(project.projectId)
                  nav(`/missioncommand/events/${project.projectId}/dashboard`);
                }}
              />
            </div>
          </Table.Cell>
        </Table.Row>))}
      </Table>)
  }


  return (
    <DefaultPageLayout>
      <div className="container max-w-none flex h-full w-full flex-col items-start gap-4 bg-default-background py-12">
        <div className="flex w-full flex-col items-start gap-2 mobile:flex-col mobile:gap-4">
          <div className="flex w-full flex-wrap items-center gap-2 mobile:flex-row mobile:flex-wrap mobile:gap-4">
            <div className="flex grow shrink-0 basis-0 items-center gap-2 mobile:items-center mobile:justify-start">
              <SubframeCore.Icon
                className="text-heading-2 font-heading-2 text-brand-600"
                name="FeatherRocket"
              />
              <span className="text-heading-2 font-heading-2 text-default-font">
                Project Management
              </span>
            </div>
            <a className="text-body font-body text-default-font hover:underline cursor-pointer" onClick={(event: React.MouseEvent<HTMLButtonElement>) => 
            {
              nav("/missioncommand/events/create");
            }}
            >
              Create New Event
            </a>
            <div className="flex items-center gap-2 mobile:h-auto mobile:grow mobile:shrink-0 mobile:basis-0">
              <IconButton
                variant="brand-primary"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                  nav("/missioncommand/events/create");
                }}
              />
            </div>
          </div>
        </div>
        <div className="flex w-full flex-wrap items-start gap-2 mobile:flex-row mobile:flex-wrap mobile:gap-4">
          <div className="flex flex-wrap items-start gap-2">
            <span>All Events</span>
            {/* <SubframeCore.Popover.Root>
              <SubframeCore.Popover.Trigger asChild={true}>
                <Button
                  variant="neutral-tertiary"
                  onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
                >
                  All Projects
                </Button>
              </SubframeCore.Popover.Trigger>
              <SubframeCore.Popover.Portal>
                <SubframeCore.Popover.Content
                  side="bottom"
                  align="center"
                  sideOffset={4}
                  asChild={true}
                >
                  <div className="flex flex-col items-start gap-1 rounded-md border border-solid border-neutral-border bg-default-background px-3 py-3 shadow-lg">
                    <span className="text-caption font-caption text-subtext-color">
                      Add filters
                    </span>
                  </div>
                </SubframeCore.Popover.Content>
              </SubframeCore.Popover.Portal>
            </SubframeCore.Popover.Root> */}
          </div>
        </div>
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-2 rounded-md border border-solid border-neutral-border bg-neutral-50 pt-0.5 overflow-auto">
          { displayProjects() }
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default Projects;