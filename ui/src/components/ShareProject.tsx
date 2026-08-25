"use client";

import React, { useEffect, useState } from "react";
import { DrawerLayout } from "../subframe/layouts/DrawerLayout";
import { IconButton } from "../subframe/components/IconButton";
import { IconWithBackground } from "../subframe/components/IconWithBackground";
import { TextField } from "../subframe/components/TextField";
import { Avatar } from "../subframe/components/Avatar";
import * as SubframeCore from "@subframe/core";
import { List } from "../subframe/components/List";
import { Button } from "../subframe/components/Button";
import { API } from 'aws-amplify';

function ShareProject({ project, sharer, open, setOpen }) {
  const [users, setUsers] = useState([])
  const [newUsers, setNewUsers] = useState([])
  const [allUsers, setAllUsers] = useState([])
  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);

  useEffect(() => {
    // async function onLoad() {
    //   const org = sharer.email.split('@')[1];
    //   const allUsersAsync = API.get('accounts', `/accounts/${org}`);
    //   const currentUsersAsync = API.get('projects', `/events/${project.projectId}/share`);
      
    //   const currentUsers = await currentUsersAsync
    //   const allUsers = await allUsersAsync
    //   const currentUserIds = currentUsers.map(user => user.userId)
      
    //   setAllUsers(allUsers.filter(user => !currentUserIds.includes(user.userId)))
    //   setUsers(currentUsers)
    // }
    
    // onLoad()
  }, [])

  async function deleteUsers(users) {
    for (const user of users) {
      await API.del('projects', `/events/${project.projectId}/share/${user.userId}`, {});
    }
  }

  return (
    <DrawerLayout open={open} onOpenChange={() => {}}>
      <div className="flex w-full h-full min-w-[448px] flex-col items-start gap-4 bg-default-background px-6 py-6">
        <div className="flex w-full items-center gap-2">
          <IconButton
            icon="FeatherChevronLeft"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              setOpen(false)
              setNewUsers([])
            }}
          />
          <span className="text-heading-2 font-heading-2 text-default-font">
            {project.name}
          </span>
        </div>
        <div className="flex w-full items-center justify-center gap-4 px-12 py-12">
          <IconWithBackground size="x-large" icon="FeatherUsers" />
        </div>
        <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-2">
          <TextField
            className="w-full flex-none"
            label={"Share " + project.name}
            helpText=""
          >
            <TextField.Input
              placeholder="Add people..."
              value={inputValue}
              onChange={(event: React.ChangeEvent<HTMLInputElement>) => {
                const value = event.target.value;
                setInputValue(value);

                if (value.length > 0) {
                  const filteredSuggestions = allUsers.filter(user =>
                    (user.firstName.toLowerCase().includes(value.toLowerCase()) 
                      || user.lastName.toLowerCase().includes(value.toLowerCase()))
                  );
                  setSuggestions(filteredSuggestions);
                  setShowSuggestions(true);
                } else {
                  setShowSuggestions(false);
                }

              }}
            />
          </TextField>
          {showSuggestions && 
          <List className="h-auto">
            {suggestions.map((user, index) => 
            <List.ListItem
              key={index}
              leftSlot={<Avatar>{user.firstName.toUpperCase()[0]+user.lastName.toUpperCase()[0]}</Avatar>}
              onClick={() => {
                setInputValue("")
                setShowSuggestions(false);
                setNewUsers([...newUsers, user])
                setUsers([...users, user])
                
                const filteredUsers = allUsers.filter(targetUser =>
                  targetUser.userId != user.userId
                );
                setAllUsers([...filteredUsers])
                
                API.put('projects', `/events/${project.projectId}/share`, { body: {
                  user,
                  ownerId: project.userId,
                  sharerEmail: sharer.email,
                }});
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
          {!showSuggestions && <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-1 mt-10">
            <span className="text-body-bold font-body-bold text-default-font">
              Contributors
            </span>
            <List>
              {users.map((user, index) => 
              <List.ListItem
                key={index}
                className={user.isOwner && "cursor-default" }
                leftSlot={<Avatar>{user.firstName.toUpperCase()[0]+user.lastName.toUpperCase()[0]}</Avatar>}
                onClick={() => {
                  if (!user.isOwner) {
                    users.splice(index, 1)
                    setUsers([...users])
                    setAllUsers([...allUsers, user])

                    const filteredNewUsers = newUsers.filter(newUser =>
                      newUser.userId != user.userId
                    );
                    setNewUsers([...filteredNewUsers])
    
                    deleteUsers([user])
                  }
                }}
                rightSlot={
                  !user.isOwner && <SubframeCore.Icon
                    className="text-heading-3 font-heading-3 text-neutral-400"
                    name="FeatherX"
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
            </List>
          </div>}
        </div>
        <div className="flex w-full items-start gap-4">
          <Button
            className="h-8 grow shrink-0 basis-0"
            variant="neutral-secondary"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              setOpen(false)
              deleteUsers(newUsers)
              setNewUsers([])
            }}
          >
            Cancel
          </Button>
          <Button
            className="h-8 grow shrink-0 basis-0"
            onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
              setOpen(false)
              setNewUsers([])
            }}
          >
            Save
          </Button>
        </div>
      </div>
    </DrawerLayout>
  );
}

export default ShareProject;