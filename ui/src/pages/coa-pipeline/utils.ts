/**
 * Shared helper functions for the COA pipeline UI.
 *
 * These small classifiers were duplicated across 4-5 component files.
 * Centralising them here keeps colour logic consistent and DRY.
 */

/** Tailwind text-color class for an expected-value metric (0-1 scale). */
export function evColorClass(ev: number): string {
  if (ev >= 0.7) return "text-success-600";
  if (ev >= 0.4) return "text-warning-600";
  return "text-error-600";
}

/** Badge variant string for a risk level ("low" | "moderate" | "high"). */
export function riskBadgeVariant(level: string): string {
  if (level === "low") return "success";
  if (level === "moderate") return "warning";
  return "error";
}

/** Tailwind bg + text classes for a risk pill/badge. */
export function riskPillClasses(level: string): string {
  if (level === "low") return "bg-success-100 text-success-700";
  if (level === "moderate") return "bg-warning-100 text-warning-700";
  return "bg-error-100 text-error-700";
}

/** Tailwind text-color class for resource-usage fraction (0-1 scale). */
export function resourceColorClass(used: number): string {
  if (used <= 0.3) return "text-success-600";
  if (used <= 0.6) return "text-warning-600";
  return "text-error-600";
}

/** Badge variant for a criteria-comparison rating ("Strong" | "Moderate" | "Weak"). */
export function ratingBadgeVariant(rating: string): string {
  switch (rating) {
    case "Strong":
      return "success";
    case "Moderate":
      return "warning";
    case "Weak":
      return "error";
    default:
      return "neutral";
  }
}

/** Badge variant for a confidence level ("high" | "medium" | "low"). */
export function confidenceBadgeVariant(level: string): string {
  if (level === "high") return "success";
  if (level === "medium") return "warning";
  return "error";
}
