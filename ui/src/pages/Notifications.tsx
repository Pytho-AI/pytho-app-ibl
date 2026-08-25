"use client";

import { useState } from "react";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { useAppContext } from "../lib/contextLib";
import { useNavigate } from "react-router-dom";

function displayTime(timestamp: number | string | undefined) {
  if (!timestamp) return "";
  const ts = typeof timestamp === "string"
    ? Date.parse(timestamp)
    : timestamp;
  if (!ts) return "";
  const deltaInMinutes = Math.ceil((Date.now() - ts) / 60000);
  if (deltaInMinutes < 60) {
    return `${deltaInMinutes} minute${deltaInMinutes !== 1 ? "s" : ""} ago`;
  }
  if (deltaInMinutes < 24 * 60) {
    const deltaInHours = Math.ceil(deltaInMinutes / 60);
    return `${deltaInHours} hour${deltaInHours !== 1 ? "s" : ""} ago`;
  }
  const deltaInDays = Math.ceil(deltaInMinutes / (24 * 60));
  return `${deltaInDays} day${deltaInDays !== 1 ? "s" : ""} ago`;
}

function displayNotification(notification: any) {
  // KLIEGLIGHT auto-fire — the only producer wired today.
  if (notification.kind === "klieglight_autofire" ||
      notification.payload?.type === "klieglight_autofire") {
    const p = notification.payload || {};
    const emitter = p.emitter_name || "(unnamed emitter)";
    const kn = p.kliegl_number || notification.artifact_id;
    const mgrs = p.mgrs || "";
    const tail = mgrs ? ` @ ${mgrs}` : "";
    return `KLIEGLIGHT auto-fire ${kn}: emitter ${emitter}${tail}`;
  }
  // Product generation finished/failed (dashboard Generate + autodraft refire).
  if (notification.kind === "product_generated" ||
      notification.payload?.type === "product_generated") {
    const p = notification.payload || {};
    const name = p.long_name || p.product_type || "Product";
    const who = p.by ? ` (by ${p.by})` : "";
    return p.ok === false
      ? `${name} failed to generate${who}`
      : `${name} finished generating${who}`;
  }
  // Legacy tagged-in-comment shape from the previous Notifications panel.
  if (notification.type === "TaggedInComment") {
    const details = notification.details || {};
    const notifierName = `${details.firstName || ""} ${details.lastName || ""}`.trim();
    if (notification.title) {
      return `${notifierName} tagged you in a comment on post: ${notification.title}`;
    }
    return `${notifierName} tagged you in a comment`;
  }
  return notification.payload?.description || notification.kind || "Notification";
}

function isAutofire(notification: any): boolean {
  return notification.kind === "klieglight_autofire" ||
         notification.payload?.type === "klieglight_autofire";
}

function isProductGenerated(notification: any): boolean {
  return (notification.kind === "product_generated" ||
          notification.payload?.type === "product_generated") &&
         notification.payload?.ok !== false &&
         !!notification.payload?.autodraft_session_id;
}

function Notifications() {
  const { user, notifications, setNotifications, projectId, setProjectId } = useAppContext();
  const nav = useNavigate();
  const [busyId, setBusyId] = useState<number | null>(null);

  async function openDraft(notification: any) {
    if (!notification?.id) return;
    if (!user?.userId) {
      console.warn("openDraft: no user.userId — cannot promote draft");
      return;
    }
    // Use the currently-selected projectId. Fallback to "default" if the
    // analyst hasn't picked a project yet — the backend tolerates any
    // string here.
    const targetProjectId = projectId || "default";
    setBusyId(notification.id);
    try {
      const resp = await fetch(`/api/notifications/${notification.id}/open_draft`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          user_id: user.userId,
          project_id: targetProjectId,
        }),
      });
      if (!resp.ok) {
        console.error("open_draft failed", resp.status, await resp.text());
        return;
      }
      const body = await resp.json();
      // Flip the local row's status so it greys out instantly.
      setNotifications((prev: any[]) => prev.map(n =>
        n.id === notification.id ? { ...n, status: "consumed" } : n
      ));
      setProjectId(targetProjectId);
      // Open in DocEditorPage. DocEditorPage's mount fetches
      // /api/sessions/<sessionId>/artifacts and finds the registered
      // KLIEGLIGHT draft this endpoint just persisted.
      nav(`/missioncommand/docs/${encodeURIComponent(body.session_id)}`);
    } finally {
      setBusyId(null);
    }
  }

  // Open a finished autodraft directly in the doc editor (same target as the
  // dashboard row). The artifact already lives in the shared autodraft pool.
  function viewProduct(notification: any) {
    const p = notification.payload || {};
    if (!p.autodraft_session_id) return;
    const params = new URLSearchParams({
      pending_product_type: p.product_type || "",
    });
    nav(
      `/missioncommand/docs/${encodeURIComponent(p.autodraft_session_id)}?${params.toString()}`,
    );
  }

  return (
    <DefaultPageLayout>
      <div className="flex w-full items-start justify-end gap-2 rounded-md bg-neutral-100 px-6 py-6">
        <div className="flex grow shrink-0 basis-0 flex-col items-start gap-6 self-stretch rounded-md border border-solid border-neutral-border bg-neutral-50 px-6 py-6">
          <div className="flex w-full flex-col items-start gap-6">
            <span className="w-full text-heading-3 font-heading-3 text-default-font">
              {user?.firstName || user?.email || "Analyst"}&#39;s Notifications
            </span>
          </div>
          <div className="flex w-full flex-col items-start">
            {(notifications || []).map((notification: any, index: number) => {
              const consumed = notification.status === "consumed";
              const fired = notification.payload?.fired_at_ms || notification.created_at;
              return (
                <div key={notification.id ?? index} className="flex w-full items-start gap-2">
                  <div className={`flex grow shrink-0 basis-0 flex-col items-start gap-1 px-2 pb-4 ${consumed ? "opacity-60" : ""}`}>
                    <span className="text-caption font-caption text-subtext-color">
                      {displayTime(fired)} · {notification.status}
                    </span>
                    <span className="text-body font-body text-default-font">
                      {displayNotification(notification)}
                    </span>
                    {isAutofire(notification) && (
                      <button
                        type="button"
                        disabled={busyId === notification.id || consumed}
                        onClick={() => openDraft(notification)}
                        className="mt-1 rounded bg-brand-600 px-3 py-1 text-caption font-caption text-white hover:bg-brand-700 disabled:opacity-50 disabled:cursor-not-allowed"
                      >
                        {consumed
                          ? "Draft opened"
                          : busyId === notification.id
                            ? "Opening..."
                            : "View draft"}
                      </button>
                    )}
                    {isProductGenerated(notification) && (
                      <button
                        type="button"
                        onClick={() => viewProduct(notification)}
                        className="mt-1 rounded bg-brand-600 px-3 py-1 text-caption font-caption text-white hover:bg-brand-700"
                      >
                        View
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
            {(!notifications || notifications.length === 0) && (
              <span className="text-body font-body text-subtext-color">
                No notifications yet.
              </span>
            )}
          </div>
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default Notifications;
