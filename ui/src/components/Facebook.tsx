"use client";

import React from "react";
import { Avatar } from "../subframe/components/Avatar";
import { IconButton } from "../subframe/components/IconButton";
import { LinkButton } from "../subframe/components/LinkButton";

function Facebook({ post }) {
  // Stable avatar for "Liked by" — pick once per post so it doesn't change on re-render when user types
  const [likedByAvatarId] = React.useState(
    () => Math.floor(Math.random() * 15) + 1
  );

  return (
    <div className="flex w-full min-h-0 items-start gap-2 bg-neutral-100 dark:bg-slate-900 p-4 rounded-md">
      <div className="flex min-w-0 grow flex-col items-start gap-4">
        <div className="flex min-w-0 flex-col items-start gap-2">
          <div className="flex w-full items-center gap-2">
            <Avatar image={ post.user.avatar }>
            </Avatar>
            <span className="grow shrink-0 basis-0 text-body-bold font-body-bold text-default-font">
              { post.user.name }
            </span>
            <IconButton
              size="small"
              icon="FeatherMoreHorizontal"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
            />
          </div>
          <span className="text-body font-body text-subtext-color">
            { post.hour } hours ago
          </span>
          {post.media && (
            <div className="flex w-full flex-col items-start rounded-md bg-neutral-200 dark:bg-slate-800">
              <img
                className="w-full max-w-full object-contain"
                src={ post.media }
                alt="Post"
                style={{ display: "block", height: "auto" }}
              />
            </div>
          )}
          <div className="flex w-full items-start justify-between">
            <div className="flex items-center gap-2">
              <IconButton
                icon="FeatherHeart"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              />
              <IconButton
                icon="FeatherMessageCircle"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              />
              <IconButton
                icon="FeatherSend"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              />
            </div>
            <IconButton
              icon="FeatherBookmark"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
            />
          </div>
          <div className="flex w-full flex-col items-start gap-1">
            <div className="flex items-center gap-2">
              <Avatar
                size="x-small"
                image={`/avatars/${likedByAvatarId}.jpg`}
              >
                A
              </Avatar>
              <span className="line-clamp-2 text-caption-bold font-caption-bold text-default-font">
                Liked by Irvin and 86 others
              </span>
            </div>
            <span className="line-clamp-2 break-words text-body font-body text-default-font">
              { post.text }
            </span>
            <LinkButton
              size="small"
              onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
            >
              View all
            </LinkButton>
            <div className="flex w-full items-center justify-between">
              <LinkButton
                size="small"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              >
                Add comment...
              </LinkButton>
              <IconButton
                size="small"
                icon="FeatherSmile"
                onClick={(event: React.MouseEvent<HTMLButtonElement>) => {}}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Facebook;