/**
 * Shared utilities for displaying user names and initials with safe fallbacks.
 * Use these instead of directly accessing user.firstName / user.lastName to avoid "undefined" display.
 */

export type UserLike = {
  firstName?: string | null;
  lastName?: string | null;
  name?: string | null;
  email?: string | null;
};

/**
 * Returns display name for a user with fallbacks: "FirstName LastName" | name | email | "User".
 */
export function getDisplayName(user: UserLike | null | undefined): string {
  if (!user) return "User";
  if (user.firstName && user.lastName) {
    return `${user.firstName} ${user.lastName}`;
  }
  if (user.firstName) return user.firstName;
  if (user.lastName) return user.lastName;
  if (user.name) return user.name;
  if (user.email) return user.email;
  return "User";
}

/**
 * Returns first/given name for greetings (e.g. "Welcome back, John!").
 */
export function getFirstName(user: UserLike | null | undefined): string {
  if (!user) return "";
  if (user.firstName) return user.firstName;
  if (user.name) {
    const parts = user.name.trim().split(/\s+/);
    return parts[0] ?? "";
  }
  if (user.email) return user.email.split("@")[0] ?? "";
  return "";
}

/**
 * Returns last/family name. Useful for API payloads that expect firstName/lastName.
 */
export function getLastName(user: UserLike | null | undefined): string {
  if (!user) return "";
  if (user.lastName) return user.lastName;
  if (user.name) {
    const parts = user.name.trim().split(/\s+/);
    if (parts.length >= 2) return parts.slice(1).join(" ") ?? "";
    return "";
  }
  return "";
}

/**
 * Returns initials for avatars (e.g. "JS" for John Smith), with safe fallbacks.
 */
export function getInitials(user: UserLike | null | undefined): string {
  if (!user) return "??";
  if (user.firstName && user.lastName) {
    const f = user.firstName[0];
    const l = user.lastName[0];
    if (f && l) return `${f}${l}`.toUpperCase();
  }
  if (user.name) {
    const parts = user.name.trim().split(/\s+/).filter(Boolean);
    if (parts.length >= 2) {
      const a = parts[0][0];
      const b = parts[1][0];
      if (a && b) return `${a}${b}`.toUpperCase();
    }
    if (parts[0]?.[0]) return parts[0][0].toUpperCase();
  }
  if (user.email?.[0]) return user.email[0].toUpperCase();
  return "??";
}
