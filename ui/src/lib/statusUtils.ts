/**
 * Centralized status definitions and utilities
 */

// Event Status Constants
export const EVENT_STATUS = {
  NOT_STARTED: "Not Started",
  IN_PROGRESS: "In Progress",
  COMPLETED: "Completed",
} as const;

export type EventStatus = typeof EVENT_STATUS[keyof typeof EVENT_STATUS];

// Badge Variant Types
export type BadgeVariant = "brand" | "neutral" | "error" | "warning" | "success";

/**
 * Get badge variant for an event status
 *
 * Color semantics:
 * - "Not Started" -> neutral (gray)
 * - "In Progress" -> warning (yellow/orange)
 * - "Completed"   -> success (green)
 */
export function getEventStatusBadgeVariant(status: string): BadgeVariant {
  switch (status) {
    case EVENT_STATUS.NOT_STARTED:
      return "neutral";
    case EVENT_STATUS.IN_PROGRESS:
      return "warning";
    case EVENT_STATUS.COMPLETED:
      return "success";
    default:
      return "neutral";
  }
}
