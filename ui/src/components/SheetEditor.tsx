/**
 * SheetEditor — back-compat re-export of LiveEditor.
 *
 * The XLSX-only iframe wrapper was generalized into a format-agnostic
 * LiveEditor (Calc / Writer / Impress all open through the same
 * iframe + WOPI surface; the per-format dispatch happens server-side in
 * the WOPI handler's MIME response). Existing imports of `SheetEditor`
 * keep working by going through this re-export.
 *
 * Prefer the `LiveEditor` name in new code.
 */
export { default, type FrameDirtyState, type LiveEditorHandle, type MapEmbed } from "./LiveEditor";
