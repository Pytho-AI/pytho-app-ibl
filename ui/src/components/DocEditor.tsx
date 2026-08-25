import { useEffect, useState, useRef } from 'react';
import { createPortal } from 'react-dom';
import { Editor } from '@tinymce/tinymce-react';
import { API } from 'aws-amplify';
import config from '../config';
import { useAppContext } from "../lib/contextLib";
import { getFirstName, getLastName } from "../lib/userUtils";
import { useTheme } from "../lib/themeLib";
import { load } from 'cheerio';
import { diffLines } from 'diff';
import { DialogLayout } from "../subframe/layouts/DialogLayout";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { ToggleGroup } from "../subframe/components/ToggleGroup";
import { HexColorPicker } from "react-colorful";
import { clientWarn, clientInfo } from "../lib/clientLog";
// Task 6: the analyst-facing map (GeoAgentProvider + tactical overlays
// + doctrinal paint + chrome) now lives in the geo-agent container
// under /geo-app/operations-map. The Edit-map button embeds it as an
// iframe below — no direct component imports here.
// index.css moved to geo-agent's bundle; iframe loads its own styles.
// ChatAgent is now mounted at chrome level (App.tsx → ChatAgent) so
// the /ws/intel_agent WebSocket survives navigation. DocEditorPage drives it
// via useChatContext.openChat.

// Compute edit deltas between original and edited content
// Finds the actual changed substring by locating first and last difference
function computeEditDeltas(original: string, edited: string): Array<{old_string: string, new_string: string, occurrence: number}> {
  const deltas: Array<{old_string: string, new_string: string, occurrence: number}> = [];
  
  if (original === edited) return deltas;
  
  // Find first differing character
  let start = 0;
  while (start < original.length && start < edited.length && original[start] === edited[start]) {
    start++;
  }
  
  // Find last differing character (from end)
  let endOriginal = original.length;
  let endEdited = edited.length;
  while (endOriginal > start && endEdited > start && original[endOriginal - 1] === edited[endEdited - 1]) {
    endOriginal--;
    endEdited--;
  }
  
  const oldText = original.slice(start, endOriginal);
  const newText = edited.slice(start, endEdited);
  
  if (oldText || newText) {
    // Count occurrence of oldText in original up to start position
    let occurrence = 1;
    if (oldText) {
      let searchPos = 0;
      while (searchPos < start) {
        const idx = original.indexOf(oldText, searchPos);
        if (idx === -1 || idx >= start) break;
        occurrence++;
        searchPos = idx + 1;
      }
    }
    
    deltas.push({
      old_string: oldText,
      new_string: newText,
      occurrence: occurrence
    });
  }
  
  return deltas;
}

// Keep diffLines import for potential future use but use custom diff above
function _unused_diffLines_computeEditDeltas(original: string, edited: string): Array<{old_string: string, new_string: string, occurrence: number}> {
  const deltas: Array<{old_string: string, new_string: string, occurrence: number}> = [];
  
  // Use line-based diff
  const changes = diffLines(original, edited);
  
  // Track position in original to determine occurrence index
  let originalPosition = 0;
  
  let i = 0;
  while (i < changes.length) {
    const change = changes[i];
    
    if (!change.added && !change.removed) {
      // Unchanged - advance position in original
      originalPosition += change.value.length;
      i++;
      continue;
    }
    
    if (change.removed) {
      const next = changes[i + 1];
      const oldText = change.value;
      
      // Count which occurrence this is by counting matches before current position
      const textBeforeChange = original.slice(0, originalPosition);
      let occurrence = 1;
      let searchPos = 0;
      while ((searchPos = textBeforeChange.indexOf(oldText, searchPos)) !== -1) {
        occurrence++;
        searchPos += 1;
      }
      
      if (next && next.added) {
        // Replacement
        deltas.push({
          old_string: oldText,
          new_string: next.value,
          occurrence: occurrence
        });
        originalPosition += oldText.length;
        i += 2;
        continue;
      } else {
        // Pure deletion
        deltas.push({
          old_string: oldText,
          new_string: '',
          occurrence: occurrence
        });
        originalPosition += oldText.length;
      }
    } else if (change.added) {
      // Pure insertion - needs context to locate
      // Use previous unchanged content as anchor
      const prevChange = i > 0 ? changes[i - 1] : null;
      if (prevChange && !prevChange.added && !prevChange.removed) {
        const anchor = prevChange.value.slice(-50);
        if (anchor) {
          // Count occurrence of anchor
          const textBeforeChange = original.slice(0, originalPosition);
          let occurrence = 1;
          let searchPos = 0;
          while ((searchPos = textBeforeChange.indexOf(anchor, searchPos)) !== -1) {
            occurrence++;
            searchPos += 1;
          }
          
          deltas.push({
            old_string: anchor,
            new_string: anchor + change.value,
            occurrence: occurrence
          });
        }
      }
    }
    
    i++;
  }
  
  return deltas;
}

// Header style interface
interface HeaderStyle {
  fontFamily: string;
  fontSize: string;
  textColor: string;
  bgColor: string;
  bold: boolean;
  italic: boolean;
  underline: boolean;
  align: string;
  position: 'header' | 'footer' | 'both';
}

// Default header style - 11pt, no bold, gray text
const defaultHeaderStyle: HeaderStyle = {
  fontFamily: 'Arial, sans-serif',
  fontSize: '11pt',
  textColor: '#808080',
  bgColor: '#ffffff',
  bold: false,
  italic: false,
  underline: false,
  align: 'center',
  position: 'header'
};

// Unified font list matching TinyMCE defaults (excluding symbol fonts)
const FONT_OPTIONS = [
  { name: 'Andale Mono', value: "'Andale Mono', monospace" },
  { name: 'Arial', value: 'Arial, sans-serif' },
  { name: 'Arial Black', value: "'Arial Black', sans-serif" },
  { name: 'Book Antiqua', value: "'Book Antiqua', serif" },
  { name: 'Comic Sans MS', value: "'Comic Sans MS', cursive" },
  { name: 'Courier New', value: "'Courier New', monospace" },
  { name: 'Georgia', value: 'Georgia, serif' },
  { name: 'Helvetica', value: 'Helvetica, sans-serif' },
  { name: 'Impact', value: 'Impact, sans-serif' },
  { name: 'Tahoma', value: 'Tahoma, sans-serif' },
  { name: 'Times New Roman', value: "'Times New Roman', serif" },
  { name: 'Trebuchet MS', value: "'Trebuchet MS', sans-serif" },
  { name: 'Verdana', value: 'Verdana, sans-serif' },
];

// Custom font dropdown that renders fonts in their actual typeface
function FontDropdown({ value, onChange, className }: { value: string, onChange: (value: string) => void, className?: string }) {
  const [isOpen, setIsOpen] = useState(false);
  const [canScrollMore, setCanScrollMore] = useState(true);
  const [hoveredFont, setHoveredFont] = useState<string | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  const selectedFont = FONT_OPTIONS.find(f => f.value === value) || FONT_OPTIONS[1]; // Default to Arial
  
  // Position dropdown when opening - maximize available vertical space
  useEffect(() => {
    if (isOpen && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect();
      const viewportHeight = window.innerHeight;
      const spaceBelow = viewportHeight - rect.bottom - 16; // 16px margin from bottom
      const spaceAbove = rect.top - 16; // 16px margin from top
      
      // Open downward if more space below, otherwise upward
      const openUpward = spaceAbove > spaceBelow && spaceBelow < 200;
      const maxHeight = openUpward ? spaceAbove : spaceBelow;
      
      setDropdownStyle({
        position: 'fixed',
        top: openUpward ? 'auto' : rect.bottom + 4,
        bottom: openUpward ? (viewportHeight - rect.top + 4) : 'auto',
        left: rect.left,
        width: rect.width,
        maxHeight: Math.max(200, maxHeight),
        zIndex: 9999,
      });
    }
  }, [isOpen]);
  
  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (buttonRef.current && !buttonRef.current.contains(event.target as Node) &&
          dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
  
  return (
    <div className={`relative ${className || ''}`}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full p-2 rounded border border-solid border-neutral-border bg-default-background text-default-font text-left flex items-center justify-between"
        style={{ fontFamily: selectedFont.value }}
      >
        <span className="truncate">{selectedFont.name}</span>
        <svg className="w-4 h-4 ml-2 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={isOpen ? "M5 15l7-7 7 7" : "M19 9l-7 7-7-7"} />
        </svg>
      </button>
      {isOpen && createPortal(
        <div 
          ref={dropdownRef}
          style={{...dropdownStyle, pointerEvents: 'auto'}}
          className="bg-default-background border border-solid border-neutral-border rounded shadow-lg flex flex-col"
          onMouseDown={(e) => e.stopPropagation()}
        >
          <div 
            ref={listRef}
            className="overflow-y-auto flex-1 overflow-x-hidden"
            onScroll={(e) => {
              const el = e.currentTarget;
              setCanScrollMore(el.scrollTop + el.clientHeight < el.scrollHeight - 5);
            }}
          >
            {FONT_OPTIONS.map((font) => (
              <button
                key={font.value}
                type="button"
                onMouseDown={(e) => e.stopPropagation()}
                onMouseEnter={() => setHoveredFont(font.value)}
                onMouseLeave={() => setHoveredFont(null)}
                onClick={(e) => { e.stopPropagation(); onChange(font.value); setIsOpen(false); }}
                className="w-full px-3 py-1.5 text-left cursor-pointer"
                style={{ 
                  fontFamily: font.value, 
                  backgroundColor: font.value === value ? '#3b82f6' : hoveredFont === font.value ? 'rgba(128,128,128,0.3)' : 'transparent',
                  color: font.value === value ? '#ffffff' : 'inherit'
                }}
              >
                {font.name}
              </button>
            ))}
          </div>
          <div className="flex items-center justify-center py-1 text-subtext-color" style={{ visibility: canScrollMore ? 'visible' : 'hidden' }}>
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
            </svg>
          </div>
        </div>,
        document.body
      )}
    </div>
  );
}

// Font size options for header
const FONT_SIZE_OPTIONS = [
  '8pt', '9pt', '10pt', '11pt', '12pt', '14pt', '16pt', '18pt', '20pt', '24pt', '28pt', '32pt', '36pt'
];

// Custom font size dropdown
function FontSizeDropdown({ value, onChange, className }: { value: string, onChange: (value: string) => void, className?: string }) {
  const [isOpen, setIsOpen] = useState(false);
  const [hoveredSize, setHoveredSize] = useState<string | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const [dropdownStyle, setDropdownStyle] = useState<React.CSSProperties>({});
  
  // Position dropdown when opening
  useEffect(() => {
    if (isOpen && buttonRef.current) {
      const rect = buttonRef.current.getBoundingClientRect();
      const viewportHeight = window.innerHeight;
      const spaceBelow = viewportHeight - rect.bottom - 16;
      const spaceAbove = rect.top - 16;
      
      const openUpward = spaceAbove > spaceBelow && spaceBelow < 200;
      const maxHeight = openUpward ? spaceAbove : spaceBelow;
      
      setDropdownStyle({
        position: 'fixed',
        top: openUpward ? 'auto' : rect.bottom + 4,
        bottom: openUpward ? (viewportHeight - rect.top + 4) : 'auto',
        left: rect.left,
        width: rect.width,
        maxHeight: Math.max(150, maxHeight),
        zIndex: 9999,
      });
    }
  }, [isOpen]);
  
  // Close dropdown when clicking outside
  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (buttonRef.current && !buttonRef.current.contains(event.target as Node) &&
          dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);
  
  return (
    <div className={`relative ${className || ''}`}>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setIsOpen(!isOpen)}
        className="w-full p-2 rounded border border-solid border-neutral-border bg-default-background text-default-font text-left flex items-center justify-between"
      >
        <span>{value}</span>
        <svg className="w-4 h-4 ml-2 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={isOpen ? "M5 15l7-7 7 7" : "M19 9l-7 7-7-7"} />
        </svg>
      </button>
      {isOpen && createPortal(
        <div 
          ref={dropdownRef}
          style={{...dropdownStyle, pointerEvents: 'auto'}}
          className="bg-default-background border border-solid border-neutral-border rounded shadow-lg overflow-y-auto"
          onMouseDown={(e) => e.stopPropagation()}
        >
          {FONT_SIZE_OPTIONS.map((size) => (
            <button
              key={size}
              type="button"
              onMouseDown={(e) => e.stopPropagation()}
              onMouseEnter={() => setHoveredSize(size)}
              onMouseLeave={() => setHoveredSize(null)}
              onClick={(e) => { e.stopPropagation(); onChange(size); setIsOpen(false); }}
              className="w-full px-3 py-1.5 text-left cursor-pointer"
              style={{ 
                backgroundColor: size === value ? '#3b82f6' : hoveredSize === size ? 'rgba(128,128,128,0.3)' : 'transparent',
                color: size === value ? '#ffffff' : 'inherit'
              }}
            >
              {size}
            </button>
          ))}
        </div>,
        document.body
      )}
    </div>
  );
}

// Generate CSS for header/footer (as actual document elements)
// Uses Word-standard margins: 1 inch (25.4mm) page margins, 0.5 inch (12.7mm) header/footer from edge
function generateClassificationCSS(text: string, style: HeaderStyle): string {
  if (!text || text === 'none') return '';
  
  const fontWeight = style.bold ? 'bold' : 'normal';
  const fontStyle = style.italic ? 'italic' : 'normal';
  const textDecoration = style.underline ? 'underline' : 'none';
  
  return `
    /* Classification marking styling - Word-standard spacing */
    .doc-header,
    .doc-footer {
      display: block;
      width: 100%;
      text-align: ${style.align};
      font-family: ${style.fontFamily};
      font-size: ${style.fontSize};
      font-weight: ${fontWeight};
      font-style: ${fontStyle};
      text-decoration: ${textDecoration};
      color: rgba(0, 0, 0, 0.5);
      background-color: transparent;
      padding: 12px 0;
      margin: 0;
      box-sizing: border-box;
      line-height: 1.4;
      /* Prevent dragging/selection */
      user-select: none;
      -webkit-user-drag: none;
      pointer-events: none;
      cursor: default;
    }
    .doc-header {
      margin-bottom: 20px; /* Gap before content */
    }
    .doc-footer {
      margin-top: 20px; /* Gap after content */
    }
    
    /* Page number styling - uses CSS counter */
    .doc-page-number {
      display: block;
      width: 100%;
      text-align: center;
      font-family: ${style.fontFamily};
      font-size: 11pt;
      color: #000000;
      background-color: transparent;
      margin-top: 16px;
      padding: 0;
      line-height: 1.2;
      /* Prevent dragging/selection */
      user-select: none;
      -webkit-user-drag: none;
      pointer-events: none;
      cursor: default;
    }
  `;
}

// Watermark is rendered as React overlay only - no CSS generation needed

// Generate base page CSS for export (Word-standard: 1 inch margins = 25.4mm, A4 size)
// Note: TinyMCE editor view uses simpler styling; this is primarily for export
function generatePageCSS(): string {
  return `
    /* Print/Export page styling */
    @media print {
      @page {
        size: A4;
        margin: 25.4mm; /* 1 inch margins */
      }
      body {
        background: white;
        margin: 0;
        padding: 0;
      }
    }
  `;
}

export default function DocEditor({ initialValue, htmlLink, docxLink, pptxLink = "", productName, sessionId, onSave = null, onSaveStart = null, onSaveError = null, onDirtyChange = null, onClose = null, onRegisterSave = null, onEditDeltas = null, onBufferSync = null, chatInitialMessage = "", onChatArtifact = null, pendingMessage = "", pendingOrientation = "portrait", chatManualProductType = "" }) {
  const { user, projectId } = useAppContext();
  const [extractedStyles, setExtractedStyles] = useState("")
  const [extractedBody, setExtractedBody] = useState("")
  const [editorKey, setEditorKey] = useState(0); // 🔑 force reinit
  // Edit-map overlay state. When set, render the embedded map editor on
  // top of the TinyMCE editor (which stays mounted underneath). Done
  // editing clears this back to null + cache-busts the figure's img.
  // No navigation — the analyst never leaves this DocEditor instance.
  const [editMap, setEditMap] = useState<{
    mapId: string; bbox: string; artifactId: string; productType: string;
  } | null>(null);
  // Stash setEditMap on a ref so the TinyMCE editor.on('click') closure
  // (registered once in the editor.on('init') effect) can call the
  // CURRENT setter without restaling.
  const setEditMapRef = useRef(setEditMap);
  setEditMapRef.current = setEditMap;
  const originalContentRef = useRef<string>(""); // Track original for delta computation
  const bufferSyncedRef = useRef<boolean>(false); // Only sync buffer once per editor instance
  
  // Document settings state - defaults to none (user must explicitly set)
  const [header, setHeader] = useState<string>('UNCLASSIFIED\nFOR EXERCISE USE ONLY');
  const [headerStyle, setHeaderStyle] = useState<HeaderStyle>(defaultHeaderStyle);
  // Default watermark follows the same orientation⇒format mapping the page
  // shape does: landscape = slide deliverable = no DRAFT watermark (it looks
  // wrong on presentation slides); portrait = working DOCX = DRAFT default.
  const [watermark, setWatermark] = useState<string>(
    pendingOrientation === 'landscape' ? 'none' : 'DRAFT'
  );
  const [showHeaderModal, setShowHeaderModal] = useState(false);
  const [tempHeaderText, setTempHeaderText] = useState('');
  const [tempStyle, setTempStyle] = useState<HeaderStyle>(defaultHeaderStyle);
  const [showWatermarkModal, setShowWatermarkModal] = useState(false);
  const [tempWatermarkText, setTempWatermarkText] = useState('');
  const [tempWatermarkColor, setTempWatermarkColor] = useState('#c0c0c0');
  const [tempWatermarkFont, setTempWatermarkFont] = useState("'Arial Black', sans-serif");
  const [tempWatermarkSize, setTempWatermarkSize] = useState('75');
  const [watermarkColor, setWatermarkColor] = useState('#c0c0c0');
  const [watermarkFont, setWatermarkFont] = useState("'Arial Black', sans-serif");
  const [watermarkSize, setWatermarkSize] = useState('75');
  // Page orientation. Three source paths checked at mount, in order:
  //   1. extractDocSettings(initialValue) — if the artifact is ALREADY in
  //      initialValue at mount time (refresh-safe path after artifact lookup),
  //      read the embedded pytho-doc-settings JSON the exporter writes. This
  //      runs synchronously in the useState initializer so the very first
  //      TinyMCE init uses the correct page-bound dimensions.
  //   2. `pendingOrientation` prop — Generate-button skeleton phase carries
  //      the product's doctrinal page_orientation through from URL params
  //      (ProductLibraryModal.runGenerate → DocEditorPage → here). Used so
  //      the skeleton + initial TinyMCE content_style page-bound correctly
  //      while OE generates.
  //   3. Fallback 'portrait' if neither resolves.
  // After mount, the useEffect[initialValue] re-extracts settings whenever a
  // new artifact arrives and setPageOrientation flips state if it differs.
  // Landscape products (MCOO, SITEMP, THRT, CARVER, GRINTSUM, CCA) round-trip
  // with {pageWidth: 1056, pageHeight: 816}; convert.js re-emits landscape
  // DOCX on save when this state is landscape.
  const [pageOrientation, setPageOrientation] = useState<'portrait' | 'landscape'>(() => {
    if (initialValue) {
      try {
        const m = initialValue.match(/<script[^>]*id="pytho-doc-settings"[^>]*>([\s\S]*?)<\/script>/i);
        if (m && m[1]) {
          const settings = JSON.parse(m[1]);
          if (settings.pageOrientation === 'landscape') return 'landscape';
          if (settings.pageOrientation === 'portrait') return 'portrait';
        }
      } catch {/* fall through to pendingOrientation */}
    }
    return pendingOrientation === 'landscape' ? 'landscape' : 'portrait';
  });
  const [showTextColorPicker, setShowTextColorPicker] = useState(false);
  const [showBgColorPicker, setShowBgColorPicker] = useState(false);
  const [showWatermarkColorPicker, setShowWatermarkColorPicker] = useState(false);
  const editorRef = useRef<any>(null);
  
  // Refs to hold current values for TinyMCE callbacks (which capture stale closures)
  const headerRef = useRef<string>(header);
  const headerStyleRef = useRef<HeaderStyle>(headerStyle);
  const watermarkRef = useRef<string>(watermark);
  const watermarkColorRef = useRef<string>(watermarkColor);
  const watermarkFontRef = useRef<string>(watermarkFont);
  const watermarkSizeRef = useRef<string>(watermarkSize);
  const pageOrientationRef = useRef<'portrait' | 'landscape'>(pageOrientation);
  // Generate-button flow: when set, the doc is "pending" — no real content yet.
  // The skeleton is rendered INSIDE the TinyMCE iframe (so it inherits the
  // Letter page bounds + padding) via setContent on init. The watermark
  // injection in updatePageLayout is suppressed while this is truthy because
  // there is nothing to mark "DRAFT" against.
  const pendingMessageRef = useRef<string>(pendingMessage || "");
  useEffect(() => { pendingMessageRef.current = pendingMessage || ""; }, [pendingMessage]);

  // Keep refs in sync with state
  useEffect(() => { headerRef.current = header; }, [header]);
  useEffect(() => { headerStyleRef.current = headerStyle; }, [headerStyle]);
  useEffect(() => { watermarkRef.current = watermark; }, [watermark]);
  useEffect(() => { watermarkColorRef.current = watermarkColor; }, [watermarkColor]);
  useEffect(() => { watermarkFontRef.current = watermarkFont; }, [watermarkFont]);
  useEffect(() => { watermarkSizeRef.current = watermarkSize; }, [watermarkSize]);
  useEffect(() => { pageOrientationRef.current = pageOrientation; }, [pageOrientation]);
  
  // Classification is now rendered as React overlay, no TinyMCE styling needed
  
  // Watermark is now rendered as React overlay, no TinyMCE background needed
    

  function extractStyle(html) {
    const $ = load(html); // Load HTML with Cheerio
    let styles = $('style').map((_, el) => $(el).html()).get().join('\n');
    // Remove any watermark/header CSS to avoid duplicates
    styles = styles.replace(/\.doc-watermark[^}]*\}/g, '');
    styles = styles.replace(/\.doc-header[^}]*\}/g, '');
    styles = styles.replace(/\.doc-footer[^}]*\}/g, '');
    styles = styles.replace(/body\s*\{[^}]*background-image[^}]*\}/g, '');
    return styles;
  }

  function extractBodyContent(html) {
    if (!html) return '';
    try {
      const $ = load(html);
      
      // Remove ALL saved watermark/header elements by class
      $('.doc-watermark, .doc-header, .doc-footer').remove();
      $('.doc-header, .doc-footer, .doc-page-number').remove();
      $('v\\:shape, v\\:shapetype, v\\:formulas, v\\:path, v\\:textpath, v\\:fill, v\\:handles').remove();
      
      // Remove any element with position:fixed, rotate, or watermark-like z-index
      $('[style]').each((_, el) => {
        const style = $(el).attr('style') || '';
        if (style.includes('position:fixed') || style.includes('position: fixed') || 
            style.includes('rotate') || style.includes('z-index:-') ||
            style.includes('z-index: -')) {
          $(el).remove();
        }
      });
      
      let body = $('body').html() || html;
      
      // Regex cleanup for anything missed
      body = body.replace(/<!--\[if gte mso[\s\S]*?\[endif\]-->/gi, '');
      body = body.replace(/<v:[^>]*>[\s\S]*?<\/v:[^>]*>/gi, '');
      body = body.replace(/<v:[^>]*\/>/gi, '');
      
      // Remove standalone DRAFT/UNCLASSIFIED/etc at start of body
      // These are header/watermark leftovers, not real content
      const keywords = ['DRAFT', 'FINAL', 'UNCLASSIFIED', 'CLASSIFIED', 'FOR EXERCISE ONLY', 'PYTHO GENERATED', 'SECRET', 'TOP SECRET', 'CONFIDENTIAL'];
      for (const keyword of keywords) {
        // Match <p>KEYWORD</p> or <div>KEYWORD</div> or just KEYWORD at start
        const patterns = [
          new RegExp(`^\\s*<p[^>]*>\\s*${keyword}\\s*</p>\\s*`, 'gi'),
          new RegExp(`^\\s*<div[^>]*>\\s*${keyword}\\s*</div>\\s*`, 'gi'),
          new RegExp(`^\\s*${keyword}\\s*(?=<)`, 'gi'),
        ];
        for (const pattern of patterns) {
          body = body.replace(pattern, '');
        }
      }
      
      // Strip leading empty paragraphs/divs
      body = body.replace(/^(\s*<(p|div)[^>]*>\s*<\/(p|div)>\s*)+/gi, '');
      
      return body.trim();
    } catch (e) {
      console.error('[extractBodyContent] Error:', e);
      return html;
    }
  }

  // Extract document settings (header, style, watermark, watermarkColor, watermarkFont, watermarkSize, pageOrientation) from HTML
  function extractDocSettings(html: string): { header?: string, headerStyle?: HeaderStyle, watermark?: string, watermarkColor?: string, watermarkFont?: string, watermarkSize?: string, pageOrientation?: string } | null {
    try {
      const match = html.match(/<script[^>]*id="pytho-doc-settings"[^>]*>([\s\S]*?)<\/script>/i);
      if (match && match[1]) {
        return JSON.parse(match[1]);
      }
    } catch (e) {
      console.warn('[extractDocSettings] Failed to parse:', e);
    }
    return null;
  }

  useEffect(() => {
    if (!initialValue) return;
    const body = extractBodyContent(initialValue);
    setExtractedStyles(extractStyle(initialValue))
    setExtractedBody(body)
    originalContentRef.current = body; // Store original for delta computation
    
    // Extract and apply saved document settings
    const savedSettings = extractDocSettings(initialValue);
    if (savedSettings) {
      // Use `in` so an explicit empty-string header from the artifact
      // clears the DocEditor default ('UNCLASSIFIED\nFOR EXERCISE USE ONLY').
      // ATP 2-01.3 Fig 6-9 and other published deliverables that carry
      // classification chrome via convert.js page headers need the body-
      // level banner suppressed so it doesn't double-render.
      if ('header' in savedSettings) {
        setHeader(savedSettings.header ?? '');
      }
      if (savedSettings.headerStyle) {
        setHeaderStyle(savedSettings.headerStyle);
      }
      if (savedSettings.watermark) {
        setWatermark(savedSettings.watermark);
      }
      if (savedSettings.watermarkColor) {
        setWatermarkColor(savedSettings.watermarkColor);
      }
      if (savedSettings.watermarkFont) {
        setWatermarkFont(savedSettings.watermarkFont);
      }
      if (savedSettings.watermarkSize) {
        setWatermarkSize(savedSettings.watermarkSize);
      }
      if (savedSettings.pageOrientation === 'portrait' || savedSettings.pageOrientation === 'landscape') {
        setPageOrientation(savedSettings.pageOrientation);
        // Watermark default tracks orientation: landscape = slide deliverable
        // = no DRAFT; portrait = docx draft. Only flip if the artifact's
        // embedded settings didn't explicitly carry a watermark value (else
        // we'd clobber a user's "FINAL"/"WORKING COPY" override).
        if (!savedSettings.watermark) {
          setWatermark(savedSettings.pageOrientation === 'landscape' ? 'none' : 'DRAFT');
        }
      }
    }

    setEditorKey(prev => prev + 1);
  }, [initialValue]);


  // Calculate watermark rotation for A4 diagonal
  const watermarkAngle = -55; // degrees for A4 portrait diagonal
  
  // Build overlay CSS for header (injected into TinyMCE iframe)
  // Positioned in the margin area like Word headers
  // Headers, footers, and watermarks are now rendered as per-page positioned divs
  // (handled by the useEffect below that responds to watermark/header changes)
  
  // Update per-page headers, footers, and watermarks when settings change
  // When watermark or header changes, trigger a re-render of overlays
  // by calling the editor's updatePageLayout if available
  useEffect(() => {
    if (editorRef.current) {
      // Trigger content change to force overlay update
      const editor = editorRef.current;
      if (editor.fire) {
        editor.fire('input');
      }
    }
  }, [watermark, watermarkColor, watermarkFont, watermarkSize, header, headerStyle]);
  
  // Embedded map editor render. Mounted in-place inside the DocEditor
  // container; TinyMCE underneath stays mounted (display:none) so chat
  // state, scroll, doc content all survive.
  //
  // ALL edit chrome (the "Editing map for …" banner + Done editing
  // button) is MAP chrome and renders inside the geo-agent iframe. This
  // side only listens for the `geoagent.editDone` hook: geo-agent has
  // already rendered + persisted the new JPG itself (render=1); we hand
  // its image_url to the refresh endpoint, which downloads + embeds —
  // no render is triggered from here.
  const onEditMapDone = async (imageUrl?: string, hookMapId?: string) => {
    const aid = editMap?.artifactId || "";
    setEditMap(null);
    if (!aid) return;
    try {
      const res = await fetch(
        `/api/artifacts/${encodeURIComponent(aid)}/refresh_map`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            image_url: imageUrl || null,
            map_id: hookMapId || editMap?.mapId || null,
          }),
        },
      );
      if (!res.ok) return;
      const body = await res.json();
      const cacheBust = body?.cache_bust;
      if (!cacheBust) return;
      const docEl = editorRef.current?.getDoc?.();
      if (!docEl) return;
      docEl.querySelectorAll(
        `figure.ibl-map-embed[data-artifact-id="${aid}"] img`,
      ).forEach((img: Element) => {
        const el = img as HTMLImageElement;
        const base = (el.getAttribute("src") || "").split("?")[0];
        if (base) el.setAttribute("src", `${base}?v=${cacheBust}`);
      });
    } catch (err) {
      clientWarn("DocEditor.refresh_map_after_overlay_done", err);
    }
  };
  const onEditMapDoneRef = useRef(onEditMapDone);
  onEditMapDoneRef.current = onEditMapDone;

  // The done hook from the geo-agent edit iframe.
  useEffect(() => {
    const onMessage = (ev: MessageEvent) => {
      const data = ev?.data;
      if (!data || typeof data !== "object" || data.type !== "geoagent.editDone") return;
      onEditMapDoneRef.current(
        typeof data.image_url === "string" ? data.image_url : undefined,
        typeof data.map_id === "string" ? data.map_id : undefined,
      );
    };
    window.addEventListener("message", onMessage);
    return () => window.removeEventListener("message", onMessage);
  }, []);

  // Parse the figure's data-bbox into a [w, s, e, n] tuple for
  // GeoAgentMap's `bounds` fit-bounds prop. Editor mode wants to land on
  // the exact bbox the static product render used — passing bounds (vs
  // center/zoom) lets MapLibre fit the camera precisely regardless of
  // container aspect ratio, and overrides any stale savedViewport stored
  // on the map record from a previous session.
  let _editMapBounds: [number, number, number, number] | undefined;
  if (editMap?.bbox) {
    const _p = editMap.bbox.split(",").map((s) => parseFloat(s.trim()));
    if (_p.length === 4 && _p.every((n) => Number.isFinite(n))) {
      _editMapBounds = [_p[0], _p[1], _p[2], _p[3]];
    }
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', width: '100%', height: '100%', position: 'relative' }}>
      {editMap && (
        <div
          data-testid="edit-map-overlay"
          style={{
            position: "absolute", inset: 0, background: "#1a1a1a",
            zIndex: 10, display: "flex", flexDirection: "column",
          }}
        >
          <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
            {/* Pure iframe embed — ALL edit chrome (banner, Done editing,
                16:9 exporter framing) renders inside geo-agent's map UI.
                render=1: this product embeds a rendered figure, so Done
                triggers geo-agent's own export render; the editDone hook
                above carries the persisted image_url we hand to
                /refresh_map for the document-side embed. */}
            {(() => {
              const q = new URLSearchParams();
              q.set("map_id", editMap.mapId);
              if (editMap.productType) q.set("product_type", editMap.productType);
              if (userId) q.set("user_id", userId);
              q.set("editEnabled", "1");
              q.set("render", "1");
              if (_editMapBounds && Array.isArray(_editMapBounds) && _editMapBounds.length === 4) {
                q.set("bbox", _editMapBounds.join(","));
              }
              const base = (import.meta as any).env?.VITE_GEO_APP_BASE || "/geo-app";
              return (
                <iframe
                  src={`${base}/operations-map?${q.toString()}`}
                  title="Edit Map"
                  style={{ position: "absolute", inset: 0, width: "100%",
                           height: "100%", border: 0, display: "block",
                           background: "#0b1120" }}
                  allow="clipboard-read; clipboard-write; fullscreen"
                />
              );
            })()}
          </div>
        </div>
      )}
      {/* Custom Header Modal */}
      <DialogLayout open={showHeaderModal} onOpenChange={setShowHeaderModal}>
        <div className="flex h-full w-full items-center justify-center">
          <div className="w-[320px]">
            <div className="flex w-full flex-col items-start rounded-lg bg-default-background shadow-lg" style={{ padding: '20px', gap: '20px' }}>
              
              {/* Show color picker OR form, not both */}
              {showTextColorPicker ? (
                <div className="flex w-full flex-col" style={{ gap: '20px' }}>
                  <div className="text-body-bold font-body-bold text-default-font">Text Color</div>
                  <style>{`.text-color-picker .react-colorful { width: 100%; height: 120px; } .text-color-picker .react-colorful__saturation { border-radius: 4px 4px 0 0; } .text-color-picker .react-colorful__hue { height: 12px; border-radius: 0 0 4px 4px; }`}</style>
                  <div className="text-color-picker">
                    <HexColorPicker color={tempStyle.textColor} onChange={(color) => {
                      setTempStyle({...tempStyle, textColor: color});
                      // Live preview
                      headerStyleRef.current = {...headerStyleRef.current, textColor: color};
                      if (editorRef.current) editorRef.current.fire('input');
                    }} />
                  </div>
                  <div className="flex justify-center" style={{ gap: '12px' }}>
                    {['#000000', '#808080', '#ff0000', '#ff6600', '#00cc00', '#0066ff', '#9933ff'].map((color) => (
                      <button
                        key={color}
                        type="button"
                        onClick={() => {
                          setTempStyle({...tempStyle, textColor: color});
                          // Live preview
                          headerStyleRef.current = {...headerStyleRef.current, textColor: color};
                          if (editorRef.current) editorRef.current.fire('input');
                        }}
                        className="w-6 h-6 rounded-full border-2 border-solid border-neutral-border cursor-pointer hover:scale-110 transition-transform"
                        style={{ backgroundColor: color }}
                      />
                    ))}
                  </div>
                  <Button className="w-full h-10" onClick={() => setShowTextColorPicker(false)}>Done</Button>
                </div>
              ) : (
                <>
                <div className="flex w-full flex-col items-start gap-1">
                  <span className="text-body-bold font-body-bold text-default-font">Header Text</span>
                  <input
                    type="text"
                    value={tempHeaderText}
                    onChange={(e) => {
                      const newText = e.target.value;
                      setTempHeaderText(newText);
                      // Live preview
                      headerRef.current = newText || 'none';
                      if (editorRef.current) editorRef.current.fire('input');
                    }}
                    className="w-full p-2 rounded border border-solid border-neutral-border bg-default-background text-default-font"
                    placeholder="Enter header text..."
                  />
                </div>
                
                <div className="flex w-full items-end" style={{ gap: '20px' }}>
                  <div className="flex flex-col items-start gap-1 flex-1">
                    <span className="text-caption font-caption text-subtext-color">Font</span>
                    <FontDropdown
                      value={tempStyle.fontFamily}
                      onChange={(value) => {
                        setTempStyle({...tempStyle, fontFamily: value});
                        // Live preview
                        headerStyleRef.current = {...headerStyleRef.current, fontFamily: value};
                        if (editorRef.current) editorRef.current.fire('input');
                      }}
                      className="w-full"
                    />
                  </div>
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Size</span>
                    <FontSizeDropdown
                      value={tempStyle.fontSize}
                      onChange={(value) => {
                        setTempStyle({...tempStyle, fontSize: value});
                        // Live preview
                        headerStyleRef.current = {...headerStyleRef.current, fontSize: value};
                        if (editorRef.current) editorRef.current.fire('input');
                      }}
                      className="w-[70px]"
                    />
                  </div>
                </div>

                <div className="flex w-full items-end" style={{ gap: '20px' }}>
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Style</span>
                    <div className="flex rounded-md border border-solid border-neutral-border overflow-hidden">
                      <button
                        type="button"
                        onClick={() => {
                          const newBold = !tempStyle.bold;
                          setTempStyle({...tempStyle, bold: newBold});
                          // Live preview
                          headerStyleRef.current = {...headerStyleRef.current, bold: newBold};
                          if (editorRef.current) editorRef.current.fire('input');
                        }}
                        className="px-3 py-1.5 text-sm font-bold border-r border-neutral-border"
                        style={{ 
                          backgroundColor: tempStyle.bold ? '#3b82f6' : 'transparent',
                          color: tempStyle.bold ? '#ffffff' : 'inherit'
                        }}
                      >B</button>
                      <button
                        type="button"
                        onClick={() => {
                          const newItalic = !tempStyle.italic;
                          setTempStyle({...tempStyle, italic: newItalic});
                          // Live preview
                          headerStyleRef.current = {...headerStyleRef.current, italic: newItalic};
                          if (editorRef.current) editorRef.current.fire('input');
                        }}
                        className="px-3 py-1.5 text-sm italic border-r border-neutral-border"
                        style={{ 
                          backgroundColor: tempStyle.italic ? '#3b82f6' : 'transparent',
                          color: tempStyle.italic ? '#ffffff' : 'inherit'
                        }}
                      >I</button>
                      <button
                        type="button"
                        onClick={() => {
                          const newUnderline = !tempStyle.underline;
                          setTempStyle({...tempStyle, underline: newUnderline});
                          // Live preview
                          headerStyleRef.current = {...headerStyleRef.current, underline: newUnderline};
                          if (editorRef.current) editorRef.current.fire('input');
                        }}
                        className="px-3 py-1.5 text-sm underline"
                        style={{ 
                          backgroundColor: tempStyle.underline ? '#3b82f6' : 'transparent',
                          color: tempStyle.underline ? '#ffffff' : 'inherit'
                        }}
                      >U</button>
                    </div>
                  </div>

                  <div className="flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Alignment</span>
                    <ToggleGroup value={tempStyle.align} onValueChange={(value: string) => {
                      setTempStyle({...tempStyle, align: value});
                      // Live preview
                      headerStyleRef.current = {...headerStyleRef.current, align: value};
                      if (editorRef.current) editorRef.current.fire('input');
                    }}>
                      <ToggleGroup.Item icon="FeatherAlignLeft" value="left" />
                      <ToggleGroup.Item icon="FeatherAlignCenter" value="center" />
                      <ToggleGroup.Item icon="FeatherAlignRight" value="right" />
                    </ToggleGroup>
                  </div>

                  <div className="flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Color</span>
                    <button
                      type="button"
                      onClick={() => setShowTextColorPicker(true)}
                      className="rounded-md border-2 border-solid border-neutral-border shadow-sm hover:border-neutral-400 transition-colors cursor-pointer"
                      style={{ backgroundColor: tempStyle.textColor, width: '40px', height: '40px', minWidth: '40px' }}
                    />
                  </div>
                </div>

                <div className="flex w-full items-center" style={{ gap: '12px', marginTop: '20px' }}>
                  <Button
                    className="flex-1 h-10"
                    variant="neutral-secondary"
                    onClick={() => {
                      // Revert refs to committed state values
                      headerRef.current = header;
                      headerStyleRef.current = headerStyle;
                      if (editorRef.current) editorRef.current.fire('input');
                      setShowHeaderModal(false);
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    className="flex-1 h-10"
                    onClick={() => {
                      setHeader(tempHeaderText || 'none');
                      setHeaderStyle({...tempStyle, position: 'header'});
                      setShowHeaderModal(false);
                    }}
                  >
                    Apply
                  </Button>
                </div>
                </>
              )}
            </div>
          </div>
        </div>
      </DialogLayout>

      
      {/* Custom Watermark Modal */}
      <DialogLayout open={showWatermarkModal} onOpenChange={setShowWatermarkModal}>
        <div className="flex h-full w-full items-center justify-center">
          <div className="w-[320px]">
            <div className="flex w-full flex-col items-start rounded-lg bg-default-background shadow-lg" style={{ padding: '20px', gap: '20px' }}>
              
              {showWatermarkColorPicker ? (
                <div className="flex w-full flex-col" style={{ gap: '20px' }}>
                  <div className="text-body-bold font-body-bold text-default-font">Watermark Color</div>
                  <style>{`.wm-color-picker .react-colorful { width: 100%; height: 120px; } .wm-color-picker .react-colorful__saturation { border-radius: 4px 4px 0 0; } .wm-color-picker .react-colorful__hue { height: 12px; border-radius: 0 0 4px 4px; }`}</style>
                  <div className="wm-color-picker">
                    <HexColorPicker color={tempWatermarkColor} onChange={(color) => {
                      setTempWatermarkColor(color);
                      // Live preview
                      watermarkColorRef.current = color;
                      if (editorRef.current) editorRef.current.fire('input');
                    }} />
                  </div>
                  <div className="flex justify-center" style={{ gap: '12px' }}>
                    {['#000000', '#808080', '#ff0000', '#ff6600', '#00cc00', '#0066ff', '#9933ff'].map((color) => (
                      <button
                        key={color}
                        type="button"
                        onClick={() => {
                          setTempWatermarkColor(color);
                          // Live preview
                          watermarkColorRef.current = color;
                          if (editorRef.current) editorRef.current.fire('input');
                        }}
                        className="w-6 h-6 rounded-full border-2 border-solid border-neutral-border cursor-pointer hover:scale-110 transition-transform"
                        style={{ backgroundColor: color }}
                      />
                    ))}
                  </div>
                  <Button className="w-full" onClick={() => setShowWatermarkColorPicker(false)}>Done</Button>
                </div>
              ) : (
                <>
                <div className="flex w-full flex-col items-start gap-1">
                  <span className="text-body-bold font-body-bold text-default-font">Watermark Text</span>
                  <input
                    type="text"
                    value={tempWatermarkText}
                    onChange={(e) => {
                      const newText = e.target.value;
                      setTempWatermarkText(newText);
                      // Live preview
                      watermarkRef.current = newText || 'none';
                      if (editorRef.current) editorRef.current.fire('input');
                    }}
                    className="w-full p-2 rounded border border-solid border-neutral-border bg-default-background text-default-font"
                    placeholder="Enter watermark text..."
                  />
                </div>

                <div className="flex w-full items-end" style={{ gap: '20px' }}>
                  <div className="flex-1 flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Font</span>
                    <FontDropdown
                      value={tempWatermarkFont}
                      onChange={(value) => {
                        setTempWatermarkFont(value);
                        // Live preview
                        watermarkFontRef.current = value;
                        if (editorRef.current) editorRef.current.fire('input');
                      }}
                      className="w-full"
                    />
                  </div>
                  <div className="flex flex-col items-start gap-1">
                    <span className="text-caption font-caption text-subtext-color">Color</span>
                    <button
                      type="button"
                      onClick={() => setShowWatermarkColorPicker(true)}
                      className="rounded-md border-2 border-solid border-neutral-border shadow-sm hover:border-neutral-400 transition-colors cursor-pointer"
                      style={{ backgroundColor: tempWatermarkColor, width: '40px', height: '40px', minWidth: '40px' }}
                      title="Watermark color"
                    />
                  </div>
                </div>

                <div className="flex w-full items-end" style={{ gap: '20px' }}>
                  <div className="flex-1 flex flex-col items-start gap-1">
                    <div className="flex w-full justify-between">
                      <span className="text-caption font-caption text-subtext-color">Size</span>
                      <span className="text-caption font-caption text-subtext-color">{tempWatermarkSize}%</span>
                    </div>
                    <input
                      type="range"
                      min="50"
                      max="100"
                      step="5"
                      value={tempWatermarkSize}
                      onChange={(e) => {
                        const newSize = e.target.value;
                        setTempWatermarkSize(newSize);
                        // Update ref immediately for live preview
                        watermarkSizeRef.current = newSize;
                        // Trigger editor update
                        if (editorRef.current) {
                          editorRef.current.fire('input');
                        }
                      }}
                      className="w-full h-2 rounded-lg appearance-none cursor-pointer bg-neutral-200 dark:bg-neutral-700"
                    />
                  </div>
                </div>

                <div className="flex w-full items-center" style={{ gap: '12px', marginTop: '20px' }}>
                  <Button
                    className="flex-1 h-10"
                    variant="neutral-secondary"
                    onClick={() => {
                      // Revert refs to committed state values
                      watermarkRef.current = watermark;
                      watermarkColorRef.current = watermarkColor;
                      watermarkFontRef.current = watermarkFont;
                      watermarkSizeRef.current = watermarkSize;
                      if (editorRef.current) editorRef.current.fire('input');
                      setShowWatermarkModal(false);
                    }}
                  >
                    Cancel
                  </Button>
                  <Button
                    className="flex-1 h-10"
                    onClick={() => {
                      setWatermark(tempWatermarkText || 'none');
                      setWatermarkColor(tempWatermarkColor);
                      setWatermarkFont(tempWatermarkFont);
                      setWatermarkSize(tempWatermarkSize);
                      setShowWatermarkModal(false);
                    }}
                  >
                    Apply
                  </Button>
                </div>
                </>
              )}
            </div>
          </div>
        </div>
      </DialogLayout>

      {/* Editor pane. The Intel Agent chat sidebar previously rendered HERE
          as a sibling column; it has been lifted out to ChatAgent
          (chrome-level fixed overlay) so the /ws/intel_agent WebSocket
          survives navigation between DocEditor, Chat, and OperationsMap.
          DocEditorPage drives that panel via useChatContext.openChat. */}
      <div style={{ display: 'flex', flex: 1, minHeight: 0, width: '100%' }}>
        <div style={{ flex: 1, minWidth: 0, display: 'flex', position: 'relative' }}>
          <Editor
        tinymceScriptSrc='/tinymce/js/tinymce/tinymce.min.js'
        licenseKey='gpl'
        // apiKey='<REDACTED>'
        // Compound key: include pageOrientation so flipping it forces a
        // remount with a fresh content_style (the body's max-width and
        // min-height are interpolated at init time only — without a remount
        // the existing iframe keeps the stale orientation CSS even after the
        // state flips). editorKey alone covers initialValue changes;
        // appending the orientation covers same-content orientation flips.
        key={`${editorKey}-${pageOrientation}`} // 🔁 reinitialize when key changes
      init={{
        promotion: false, 
        license_key: 'gpl',
        branding: false,
        statusbar: false,
        // ai_shortcuts: [
        //   { title: 'Summarize content', prompt: 'Provide the key points and concepts in this content in a succinct summary.', selection: true },
        //   { title: 'Improve writing', prompt: 'Rewrite this content with no spelling mistakes, proper grammar, and with more descriptive language, using best writing practices without losing the original meaning.', selection: true },
        //   { title: 'Simplify language', prompt: 'Rewrite this content with simplified language and reduce the complexity of the writing, so that the content is easier to understand.', selection: true },
        //   { title: 'Expand upon', prompt: 'Expand upon this content with descriptive language and more detailed explanations, to make the writing easier to understand and increase the length of the content.', selection: true },
        //   { title: 'Trim content', prompt: 'Remove any repetitive, redundant, or non-essential writing in this content without changing the meaning or losing any key information.', selection: true }, 
        // ],
        //selector: 'textarea',
        // CSS reset + document styles to ensure consistent rendering
        // A4 page dimensions: 210mm x 297mm, with 1 inch (25.4mm) margins
        content_style: `
          /* Word-like page styling. Page dimensions per orientation:
             portrait  = 816px × 1056px (8.5" × 11" Letter at 96dpi)
             landscape = 1056px × 816px (11"  × 8.5" Letter at 96dpi) */
          html {
            margin: 0 !important;
            padding: 20px !important;
            background: #808080 !important;
            overflow: visible !important;
            height: auto !important;
            min-height: 100% !important;
          }
          body {
            /* Typography MUST match the exporter's S_BODY constant so the
               editor preview is WYSIWYG against the DOCX/PPTX outputs.
               extractBodyContent strips the body's inline style attribute,
               so we re-state the same values here as the editor defaults. */
            font-family: 'Arial', sans-serif;
            font-size: 10pt;
            color: #000000;
            line-height: 1.45;
            /* Page orientation determines both the deliverable format AND
               the aspect ratio — the two are a single decision:
                 - landscape ⇒ PPTX slide, 13.33"×7.5" widescreen
                   = 1280×720 px at 96dpi, aspect 16:9 (1.778). Matches
                   render_artifact_pptx slide_width/slide_height.
                 - portrait ⇒ DOCX Letter, 8.5"×11" = 816×1056 px,
                   aspect 0.773.
               Width is responsive (100% of iframe, capped at the
               deliverable's page width). Height is DERIVED from the
               aspect ratio — never set independently. */
            width: 100% !important;
            max-width: ${pageOrientation === 'landscape' ? '1280px' : '816px'} !important;
            aspect-ratio: ${pageOrientation === 'landscape' ? '16 / 9' : '816 / 1056'};
            margin: 0 auto !important;
            padding: 47px 96px 96px 96px !important; /* 0.49" top (like export), 1" sides and bottom */
            box-sizing: border-box !important; /* padding INSIDE the width/height */
            overflow: visible !important; /* Allow content overflow / multi-page extension below */
            background-color: white; /* Use background-color, not background, so backgroundImage isn't reset */
            box-shadow: 0 0 10px rgba(0,0,0,0.3);
            position: relative;
          }
          /* Match Word default spacing - content at ~96px from page top */
          /* With 47px padding, need 49px margin to reach 96px */
          /* EXCLUDE the map figure: it's full-bleed edge-to-edge and the
             updatePageLayout figure-push loop sets its own inline
             marginTop to overcome the 47px body padding. A 49px
             !important here would override the push (net +49 vs -47),
             leaving the figure 96px below the page top — overflows the
             page bottom by ~96px on landscape (where figure height ==
             pageHeight). */
          body > *:first-child:not(.ibl-map-embed) {
            margin-top: 49px !important;
          }
          /* Mirror S_BODY-family typography so headings/tables that rely on
             inheritance match the export. Inline styles in the exporter
             output override these for elements that carry an explicit
             style; bare elements pick these defaults up. */
          p { margin: 0 0 6pt 0; font-size: 10pt; line-height: 1.45; }
          table { border-collapse: collapse; font-family: 'Arial', sans-serif; font-size: 9pt; }
          td, th { font-size: 9pt; line-height: 1.45; vertical-align: top; }
          
          /* Overlays are absolutely positioned - don't affect text flow */
          .page-header, .page-footer, .page-watermark, .page-break-visual {
            pointer-events: none;
            user-select: none;
          }

          /* Map figure: WYSIWYG with the PPTX/DOCX map slide — edge-to-
             edge on the page, no body padding, no border. PPTX-bound
             (landscape) is 16:9, matching the slide; DOCX-bound (portrait)
             is 8.5:11, matching the page. The figcaption (Edit-map button)
             overlays the bottom-right corner so the figure box height
             stays at its aspect-ratio computed height.
             Negative margins overcome body padding (96px sides, 96px
             bottom). margin-top stays free so updatePageLayout's
             figure-push loop can set inline marginTop to shift the
             figure onto its own page; the page-bleed logic in that
             loop accounts for the -47px top offset. */
          .ibl-map-embed {
            position: relative;
            display: block;
            width: calc(100% + 192px);
            max-width: none;
            margin-left: -96px;
            margin-right: -96px;
            margin-bottom: -96px;
            aspect-ratio: ${pageOrientation === 'landscape' ? '16 / 9' : '8.5 / 11'};
            overflow: hidden;
            border: 0;
            background: #eef2f7;
          }
          .ibl-map-embed > img {
            position: absolute;
            top: 0; left: 0;
            width: 100%;
            height: 100%;
            object-fit: cover;
            object-position: center;
          }
          .ibl-map-embed > figcaption {
            position: absolute;
            bottom: 24pt; right: 6pt;
            margin: 0;
            background: rgba(255,255,255,0.85);
            border-radius: 3px;
            padding: 2px 4px;
          }

          /* Generate-button skeleton — rendered as the initial body content
             while OE generates the artifact. Inherits the Letter page bounds
             from doc-editor-body's padding (96px sides, 47px top). All
             skeleton elements pulse on the same keyframes; staggered via
             animation-delay per element. The skeleton block is replaced via
             setContent the moment the real artifact HTML lands (DocEditor
             re-mounts on editorKey change, see useEffect[initialValue]). */
          @keyframes pytho-skel-pulse {
            0%, 100% { opacity: 0.55; }
            50%      { opacity: 1.0; }
          }
          .pytho-skeleton-root { user-select: none; cursor: default; }
          .pytho-skel {
            background: #e5e7eb;
            border-radius: 3px;
            animation: pytho-skel-pulse 1.4s ease-in-out infinite;
          }
          .pytho-skel-classif { background: #c8d8c8; height: 36px; width: 100%; }
          .pytho-skel-title   { height: 28px; width: 60%; margin: 18px 0 14px 0; animation-delay: 100ms; }
          .pytho-skel-line-a  { height: 14px; width: 80%; margin-bottom: 6px;  animation-delay: 200ms; }
          .pytho-skel-line-b  { height: 14px; width: 65%; margin-bottom: 6px;  animation-delay: 300ms; }
          .pytho-skel-line-c  { height: 14px; width: 90%; margin-bottom: 18px; animation-delay: 400ms; }
          .pytho-skel-table   { display: flex; flex-direction: column; }
          .pytho-skel-tr      { display: flex; flex-direction: row; }
          .pytho-skel-cell    {
            background: #ececef;
            border: 1px solid #d8dade;
            height: 24px;
            flex: 1 1 0;
            animation: pytho-skel-pulse 1.4s ease-in-out infinite;
          }
          .pytho-skel-tr .pytho-skel-cell:first-child { flex: 0 0 18%; }
          .pytho-skel-tr + .pytho-skel-tr .pytho-skel-cell { margin-top: -1px; }
          .pytho-skel-cell + .pytho-skel-cell { margin-left: -1px; }
          .pytho-skel-footer  { height: 22px; width: 100%; margin-top: 22px; background: #c8d8c8; animation-delay: 1100ms; }
          .pytho-skel-pg      { height: 10px; width: 30%; margin-top: 8px; animation-delay: 1200ms; }
        `,
        exportword_converter_style: extractedStyles,
        exportpdf_converter_style: extractedStyles,
        // Preserve special characters properly
        entity_encoding: 'raw',
        // CRITICAL: Preserve inline styles for document export AND for
        // the WYSIWYG editor preview. The exporter's S_H1 / S_H2 /
        // S_H2_FILLED / S_TH_* / S_TD constants use text-transform (for
        // uppercase headings), border-bottom (heading underline rule),
        // background-color (filled section bars + table header rows),
        // and position/top/left/right/bottom (map figure overlay). If
        // any of these is missing here, TinyMCE silently strips the
        // style and the editor preview diverges from the export.
        valid_styles: {
          '*': 'font-family,font-size,font-weight,font-style,text-align,text-indent,text-decoration,color,background-color,background,margin,margin-top,margin-bottom,margin-left,margin-right,padding,padding-top,padding-bottom,padding-left,padding-right,border,border-top,border-right,border-bottom,border-left,border-width,border-style,border-color,border-radius,width,min-width,max-width,height,min-height,max-height,line-height,letter-spacing,vertical-align,display,position,top,right,bottom,left,text-transform,white-space,word-break,overflow,box-sizing,object-fit,object-position,page-break-inside,page-break-before,page-break-after,break-inside,break-before,break-after,opacity,transform'
        },
        // Allow style attribute on all elements.
        // figure/figcaption/button + img/[data-*]/[class] are required for the
        // map-embed flow (exporter.py:_render_map_embed). Without these listed,
        // TinyMCE strips the `<figure class="ibl-map-embed">` wrapper, the
        // Edit-map button, and the data-* attrs the NodeChange listener reads
        // on click. The [*] wildcard preserves class/data-*/style/role/etc.
        extended_valid_elements: 'div[*],p[*],span[*],h1[*],h2[*],h3[*],h4[*],h5[*],h6[*],table[*],tr[*],td[*],th[*],thead[*],tbody[*],ul[*],ol[*],li[*],strong[*],em[*],a[*],img[*],br[*],hr[*],figure[*],figcaption[*],button[*]',
        // Don't strip any styles
        valid_children: '+body[style]',
        height: '100%',
        width: '100%',
        resize: false,
        content_css: false, // Don't load default content CSS
        body_class: 'doc-editor-body',
        //skin: 'oxide-dark',
        //content_css: 'dark',
        plugins: [
          // Core editing features
          'anchor', 'autolink', 'charmap', 'codesample', 'emoticons', 'image', 'link', 'lists', 'media', 'searchreplace', 'table', 'visualblocks', 'wordcount', 'pagebreak',
          // Your account includes a free trial of TinyMCE premium features
          // Try the most popular premium features until May 20, 2025:
          // 'checklist', 'mediaembed', 'casechange', 'formatpainter', 'pageembed', 'a11ychecker', 'tinymcespellchecker', 'permanentpen', 'powerpaste', // 'ai',
          // // 'advtable', 'advcode', 'editimage', 'advtemplate', 'ai', 'mentions', 'tinycomments', 
          // 'tableofcontents', 'footnotes', 'mergetags', 'autocorrect', 'typography', 'inlinecss', 'markdown', // 'importword', 
          // 'exportword', 'exportpdf'
        ],
        toolbar: 'ai_button | undo redo | blocks fontfamily fontsize | bold italic underline strikethrough | table',
        // Render every button (wrap to a second row) instead of the default
        // 'floating' overflow, which measures container width once at init
        // and hides overflowing buttons behind a "⋯". The editor column
        // shrinks AFTER init when the chat panel auto-opens, so floating mode
        // left trailing icons hidden until a resize/keystroke forced a
        // re-measure. 'wrap' keeps all icons visible on first paint.
        toolbar_mode: 'wrap',
        menubar: 'file edit view insert format tools table docsettings translate',
        // Page break configuration
        pagebreak_separator: '<!-- pagebreak -->',
        pagebreak_split_block: true,
        tinycomments_mode: 'embedded',
        tinycomments_author: 'Author name',
        mergetags_list: [
          { value: 'First.Name', title: 'First Name' },
          { value: 'Email', title: 'Email' },
        ],
        // ai_request: (request, respondWith) => respondWith.string(() => {

        //   return waitForAnswer(request)
        // }),

        setup: function (editor) {
          editorRef.current = editor;
          
          // Sync buffer on init - this is when the editor is ready
          // Function to reflow content at page boundaries and add overlays
          const updatePageLayout = () => {
            const doc = editor.getDoc();
            if (!doc?.body) return;

            // Generate-button skeleton phase: the body holds a placeholder
            // skeleton that's intrinsically taller than one slide (many
            // pulsing block divs stacked). If we let the page splitter run
            // over it, the skeleton gets sliced across pages, which looks
            // worse than the real artifact will. Skip pagination entirely
            // while pending and just clamp the body to exactly one slide-
            // page (overflow hidden so any extra skeleton rows just sit
            // off-page until the artifact replaces them).
            if (pendingMessageRef.current && !originalContentRef.current) {
              const isLandscapeP = pageOrientationRef.current === 'landscape';
              const refW = isLandscapeP ? 1280 : 816;
              const refH = isLandscapeP ? 720 : 1056;
              const bodyW = doc.body.offsetWidth || refW;
              const onePage = bodyW / (refW / refH);
              doc.body.style.setProperty('height',     `${Math.floor(onePage)}px`, 'important');
              doc.body.style.setProperty('min-height', `${Math.floor(onePage)}px`, 'important');
              doc.body.style.setProperty('overflow',   'hidden', 'important');
              // Strip prior overlays so a re-fire during pending stays clean.
              doc.querySelectorAll('.page-watermark, .page-watermark-container, .page-header, .page-footer, .page-break-visual').forEach((el: Element) => el.remove());
              return;
            }

            // Page geometry is a single decision tied to orientation:
            //   - landscape ⇒ PPTX slide, 13.33"×7.5", aspect 16:9.
            //     Reference dims: 1280×720 px at 96dpi.
            //   - portrait  ⇒ DOCX Letter, 8.5"×11", aspect 8.5:11.
            //     Reference dims: 816×1056 px.
            // The body's `max-width` caps at the page width, but
            // `body.offsetWidth` is what the iframe actually allocates —
            // when the iframe parent is narrower, body shrinks to fit.
            // pageHeight has to scale PROPORTIONALLY with the actual body
            // width or the aspect distorts. Margins scale with the page so
            // they read as the same physical inch (0.49"/1") at any size.
            const isLandscape = pageOrientationRef.current === 'landscape';
            const refWidth  = isLandscape ? 1280 : 816;
            const refHeight = isLandscape ? 720  : 1056;
            const aspect = refWidth / refHeight;
            const pageWidth = doc.body.offsetWidth || refWidth;
            const pageHeight = pageWidth / aspect;
            const topMargin    = pageHeight * (47 / refHeight); // 0.49"
            const bottomMargin = pageHeight * (96 / refHeight); // 1"
            const contentHeight = pageHeight - topMargin - bottomMargin;
            const usableContentHeight = contentHeight; // No extra header margin needed

            // Remove overlays only
            doc.querySelectorAll('.page-watermark, .page-watermark-container, .page-header, .page-footer, .page-break-visual').forEach((el: Element) => el.remove());

            // Forced page-break markers — content the exporter (or analyst)
            // emits to demand a hard slide split that respects this splitter's
            // aspect-locked pageHeight. Marker is `<div class="pytho-page-break">`.
            // We expand its height so its top fills the remainder of the
            // current page; the next real block then naturally starts at the
            // next page boundary, and Check 3 below pushes that block down
            // 120px to clear the header zone. Reset → measure → set in two
            // passes so the offsets we read aren't polluted by the previous
            // pass's height.
            const breakMarkers = doc.body.querySelectorAll('.pytho-page-break');
            breakMarkers.forEach((m: Element) => {
              const el = m as HTMLElement;
              el.style.display = 'block';
              el.style.visibility = 'hidden';
              el.style.height = '';
              el.style.margin = '0';
              el.style.padding = '0';
              el.style.border = '0';
              el.style.background = 'transparent';
              el.removeAttribute('data-page-push');
            });

            // Reset minHeight to measure actual content height
            doc.body.style.minHeight = '';
            void doc.body.offsetHeight;

            // Expand each marker to fill the remainder of its current page.
            // Compute against the live offsetTop so we honor whatever pushes
            // upstream markers already created on this same pass.
            breakMarkers.forEach((m: Element) => {
              const el = m as HTMLElement;
              const top = el.offsetTop;
              const currentPage = Math.floor(top / pageHeight);
              const nextPageStart = (currentPage + 1) * pageHeight;
              const fillHeight = Math.max(1, nextPageStart - top);
              el.style.height = `${fillHeight}px`;
              void doc.body.offsetHeight;
            });
            
            // Figures stay aspect-locked (CSS aspect-ratio is the ONLY
            // height-determining rule). When a figure would cross a page
            // boundary, push it to the next page via margin-top — parallel
            // to the .pytho-page-break block-push loop further down. Drop
            // any leftover --map-cap from prior CSS so the figure expands
            // to its true aspect-ratio height regardless.
            const figures = doc.body.querySelectorAll('figure.ibl-map-embed');
            figures.forEach((fig: Element) => {
              const figEl = fig as HTMLElement;
              figEl.style.removeProperty('--map-cap');
              if (!figEl.offsetParent) return;
              // If a previous push already moved this figure, reset before
              // measuring so the push math sees the true natural offsetTop.
              if (figEl.hasAttribute('data-page-push')) {
                figEl.style.marginTop = '';
                figEl.removeAttribute('data-page-push');
              }
            });
            void doc.body.offsetHeight;
            figures.forEach((fig: Element) => {
              const figEl = fig as HTMLElement;
              if (!figEl.offsetParent) return;
              const figTop = figEl.offsetTop;
              const figHeight = figEl.offsetHeight;
              const currentPage = Math.floor(figTop / pageHeight);
              // The map figure is full-page edge-to-edge (CSS aspect-ratio
              // matches page aspect; negative margins cancel body padding
              // sides + bottom). On its hosting page we want top:0 of the
              // page — body padding contributes 47px; negative-margin-top
              // by 47 cancels that. Push to the NEXT page boundary when
              // the figure can't fit on the current page.
              const wantTopOnCurrentPage = currentPage * pageHeight - topMargin;
              const pageEnd = (currentPage + 1) * pageHeight;
              const figFitsCurrentPage = figTop + figHeight - topMargin <= pageEnd;
              const isFirstOnPage = figTop <= currentPage * pageHeight + topMargin + 1;
              if (figFitsCurrentPage && isFirstOnPage) {
                // Already at page top — slide up to overcome body top padding.
                const pushAmount = wantTopOnCurrentPage - figTop;
                if (pushAmount !== 0) {
                  figEl.style.marginTop = `${pushAmount}px`;
                  figEl.setAttribute('data-page-push', 'true');
                }
              } else {
                // Push the FIGURE itself to the next page top. Map pages
                // are full-bleed self-contained slides with no header zone
                // — pulling a preceding heading along (pushAmount + 120)
                // would land the heading 120px into the next page, the
                // figure ~145px in, and since figure height == pageHeight
                // the figure would bleed ~145px past page bottom. The
                // preceding heading stays on its natural page.
                const nextPageTop = (currentPage + 1) * pageHeight - topMargin;
                const pushAmount = nextPageTop - figTop;
                if (pushAmount > 0 && pushAmount < pageHeight) {
                  figEl.style.marginTop = `${pushAmount}px`;
                  figEl.setAttribute('data-page-push', 'true');
                }
              }
            });
            void doc.body.offsetHeight;

            // Compute the set of pages occupied by a map figure. Those
            // pages get NO header / footer / watermark overlay (the
            // figure is full-bleed, edge-to-edge — chrome would paint
            // on top of the map and break WYSIWYG with the PPTX slide
            // which has zero chrome on the map slide).
            const mapPageIndices = new Set<number>();
            figures.forEach((fig: Element) => {
              const figEl = fig as HTMLElement;
              if (!figEl.offsetParent) return;
              const figTop = figEl.offsetTop;
              const figBottom = figTop + figEl.offsetHeight;
              const startPage = Math.max(0, Math.floor((figTop + topMargin) / pageHeight));
              const endPage = Math.floor((figBottom + topMargin - 1) / pageHeight);
              for (let p = startPage; p <= endPage; p++) mapPageIndices.add(p);
            });

            // Find actual content bottom (last element's bottom position)
            // Only count elements with actual content (text or images)
            let maxContentBottom = 0;
            const allElements = doc.body.querySelectorAll('p, h1, h2, h3, h4, h5, h6, div:not([data-mce-bogus]), table, ul, ol, img, figure');
            allElements.forEach((el: Element) => {
              const htmlEl = el as HTMLElement;
              const isImg = htmlEl.tagName === 'IMG';
              const hasContent = isImg || (htmlEl.textContent && htmlEl.textContent.trim().length > 0);
              if (htmlEl.offsetParent && hasContent) {
                const bottom = htmlEl.offsetTop + htmlEl.offsetHeight;
                if (bottom > maxContentBottom) maxContentBottom = bottom;
              }
            });
            
            // Use content bottom for page calculation
            const numPages = Math.max(1, Math.floor(maxContentBottom / pageHeight) + 1);
            
            // Push blocks that cross page boundaries - iterate until stable
            // Header is in margin area (48px before content start)
            // Content starts at contentStart on each page
            //
            // EXCLUDE figure.ibl-map-embed — the map figure is full-bleed
            // edge-to-edge and was already positioned by the figure-push
            // loop above. The generic block-push adds a +120px header-zone
            // shim that's WRONG for map figures (their host page has no
            // header — mapPageIndices skips header rendering — so the
            // shim leaves a 120px gap above the figure and pushes the
            // figure 120px past the page bottom).
            const blocks = doc.body.querySelectorAll('p, h1, h2, h3, h4, h5, h6, div:not(.page-header):not(.page-footer):not(.page-watermark):not(.page-break-visual):not(.pytho-page-break):not([data-mce-bogus]), table, ul, ol, figure:not(.ibl-map-embed)');

            // First, reset all previous pushes
            blocks.forEach((block: Element) => {
              const el = block as HTMLElement;
              if (el.hasAttribute('data-page-push')) {
                el.style.marginTop = '';
                el.removeAttribute('data-page-push');
              }
            });
            void doc.body.offsetHeight; // Force reflow
            
            // Iterate until no more pushes needed (max 10 iterations to prevent infinite loop)
            let pushOccurred = true;
            let iterations = 0;
            while (pushOccurred && iterations < 10) {
              pushOccurred = false;
              iterations++;
              
              blocks.forEach((block: Element) => {
                const el = block as HTMLElement;
                if (!el.offsetParent) return;
                if (el.hasAttribute('data-page-push')) return; // Already pushed

                const blockTop = el.offsetTop;
                const blockHeight = el.offsetHeight;
                const blockBottom = blockTop + blockHeight;
                
                const currentPage = Math.floor(blockTop / pageHeight);
                const pageStart = currentPage * pageHeight;
                // Header at 48px, ~20px tall = ends ~68px. Push content to 120px for proper gap
                const headerZoneEnd = pageStart + 120;
                // Content ends at topMargin + usableContentHeight from page start
                const pageContentEnd = pageStart + topMargin + usableContentHeight;
                
                // Check 1: Element crosses page boundary - push to next page.
                // The "fits within one page" guard uses a small slack so a
                // block within ~5% of usable height (e.g. an aspect-locked
                // map figure capped to content height) still gets pushed.
                const fitsOnePage = blockHeight <= usableContentHeight * 1.05;
                if (blockTop < pageContentEnd && blockBottom > pageContentEnd && fitsOnePage) {
                  const nextContentStart = (currentPage + 1) * pageHeight;
                  // Push to 120px from page start (header ends at ~68px, 52px gap)
                  const pushAmount = nextContentStart - blockTop + 120;
                  if (pushAmount > 0 && pushAmount < pageHeight) {
                    el.style.marginTop = `${pushAmount}px`;
                    el.setAttribute('data-page-push', 'true');
                    pushOccurred = true;
                  }
                }
                // Check 2: Element starts in footer/bottom margin zone - push to next page
                else if (blockTop >= pageContentEnd && blockTop < (currentPage + 1) * pageHeight) {
                  const nextContentStart = (currentPage + 1) * pageHeight;
                  // Push to 120px from page start (header ends at ~68px, 52px gap)
                  const pushAmount = nextContentStart - blockTop + 120;
                  if (pushAmount > 0 && pushAmount < pageHeight) {
                    el.style.marginTop = `${pushAmount}px`;
                    el.setAttribute('data-page-push', 'true');
                    pushOccurred = true;
                  }
                }
                // Check 3: Element starts in header zone (not first page) - push down to clear header
                else if (currentPage > 0 && blockTop >= pageStart && blockTop < headerZoneEnd) {
                  const pushAmount = headerZoneEnd - blockTop; // Push to end of header zone (96px from page)
                  if (pushAmount > 0) {
                    el.style.marginTop = `${pushAmount}px`;
                    el.setAttribute('data-page-push', 'true');
                    pushOccurred = true;
                  }
                }
              });
              
              if (pushOccurred) {
                void doc.body.offsetHeight; // Force reflow after pushes
              }
            }
            
            // Recalculate content bottom after margins are applied
            let finalContentBottom = 0;
            allElements.forEach((el: Element) => {
              const htmlEl = el as HTMLElement;
              const isImg = htmlEl.tagName === 'IMG';
              const hasContent = isImg || (htmlEl.textContent && htmlEl.textContent.trim().length > 0);
              if (htmlEl.offsetParent && hasContent) {
                const bottom = htmlEl.offsetTop + htmlEl.offsetHeight;
                if (bottom > finalContentBottom) finalContentBottom = bottom;
              }
            });
            // Calculate which page the content ends on
            const finalNumPages = Math.max(1, Math.floor(finalContentBottom / pageHeight) + 1);
            
            // Set body height for pages
            // With border-box: height = visual height (padding included)
            const targetHeight = finalNumPages * pageHeight;
            
            // Force height explicitly - not just min-height
            doc.body.style.setProperty('min-height', `${targetHeight}px`, 'important');
            doc.body.style.setProperty('height', `${targetHeight}px`, 'important');
            
            // Force reflow and verify
            void doc.body.offsetHeight;
            console.log('[DocEditor] Body height set:', {
              targetHeight,
              actualHeight: doc.body.offsetHeight,
              computedHeight: getComputedStyle(doc.body).height,
              finalNumPages
            });
            
            // Also ensure the html element allows body to expand
            const htmlEl = doc.documentElement;
            if (htmlEl) {
              htmlEl.style.setProperty('height', 'auto', 'important');
              htmlEl.style.setProperty('min-height', `${targetHeight + 40}px`, 'important');
            }
            
            // Debug: log actual dimensions
            console.log('[DocEditor] Page layout:', {
              finalNumPages,
              targetHeight,
              actualBodyHeight: doc.body.offsetHeight,
              bodyScrollHeight: doc.body.scrollHeight,
              computedMinHeight: getComputedStyle(doc.body).minHeight,
              computedHeight: getComputedStyle(doc.body).height,
              finalContentBottom
            });
            
            for (let i = 0; i < finalNumPages; i++) {
              // In TinyMCE, absolute top:0 = content edge (same as offsetTop 0)
              // So we use offsetTop-based coordinates for overlays
              // Page i content: offsetTop i*1056 to i*1056+840
              // Header 72px before content, footer 60px after content
              const contentStart = i * pageHeight;
              const contentEnd = contentStart + contentHeight;

              // Map-figure pages get NO chrome — the figure is full-bleed
              // edge-to-edge, matching the PPTX map slide which has zero
              // chrome. Header / footer / watermark would paint on top of
              // the map and break WYSIWYG.
              const isMapPage = mapPageIndices.has(i);

              // Header positioned in margin area (centered in top padding)
              if (!isMapPage && headerRef.current && headerRef.current !== 'none') {
                const headerDiv = doc.createElement('div');
                headerDiv.className = 'page-header';
                headerDiv.setAttribute('data-mce-bogus', 'all');
                headerDiv.contentEditable = 'false';
                headerDiv.textContent = headerRef.current;
                
                // Header in the top margin area - vertically centered at 48px from page start
                // Use transform to center regardless of font size
                const headerCenter = contentStart + 48;
                
                // Use headerStyle settings
                const style = headerStyleRef.current;
                const fontWeight = style.bold ? 'bold' : 'normal';
                const fontStyle = style.italic ? 'italic' : 'normal';
                const textDecoration = style.underline ? 'underline' : 'none';
                const textColor = style.textColor || '#808080';
                
                headerDiv.style.cssText = `position:absolute;top:${headerCenter}px;left:0;right:0;transform:translateY(-50%);padding:0 48px;box-sizing:border-box;text-align:${style.align};font-family:${style.fontFamily};font-size:${style.fontSize};font-weight:${fontWeight};font-style:${fontStyle};text-decoration:${textDecoration};color:${textColor};white-space:pre-line;pointer-events:none;user-select:none;cursor:default;z-index:10;`;
                doc.body.appendChild(headerDiv);
              }
              
              // Footer 60px after content ends + 72px shift — skipped on
              // map pages (WYSIWYG with chromeless PPTX map slide).
              if (!isMapPage) {
                const footerDiv = doc.createElement('div');
                footerDiv.className = 'page-footer';
                footerDiv.setAttribute('data-mce-bogus', 'all');
                footerDiv.contentEditable = 'false';
                footerDiv.textContent = String(i + 1);
                footerDiv.style.cssText = `position:absolute;top:${contentEnd + 60}px;left:0;right:0;text-align:center;font-family:Arial,sans-serif;font-size:12px;color:rgba(0,0,0,0.6);pointer-events:none;user-select:none;cursor:default;z-index:10;`;
                doc.body.appendChild(footerDiv);
              }
              
              // Page break bar at page boundary (not on first page)
              if (i > 0) {
                const breakDiv = doc.createElement('div');
                breakDiv.className = 'page-break-visual';
                breakDiv.setAttribute('data-mce-bogus', 'all');
                breakDiv.contentEditable = 'false';
                // Position at the page boundary (i * pageHeight)
                const pageBreakPos = i * pageHeight;
                breakDiv.style.cssText = `position:absolute;top:${pageBreakPos - 10}px;left:-96px;right:-96px;height:20px;background:#808080;pointer-events:none;z-index:20;`;
                doc.body.appendChild(breakDiv);
              }
              
              // Watermark centered on each page. Suppressed entirely while
              // the doc is in the Generate-button pending state — nothing to
              // mark "DRAFT" against when the body is a placeholder skeleton.
              // Also suppressed on map pages (WYSIWYG with chromeless PPTX
              // map slide).
              if (
                !isMapPage &&
                !pendingMessageRef.current &&
                watermarkRef.current && watermarkRef.current !== 'none'
              ) {
                // Create a clipping container for the watermark (prevents overflow)
                const wmContainer = doc.createElement('div');
                wmContainer.className = 'page-watermark-container';
                wmContainer.setAttribute('data-mce-bogus', 'all');
                wmContainer.contentEditable = 'false';
                // Container covers the entire page and clips overflow
                wmContainer.style.cssText = `position:absolute;top:${contentStart}px;left:0;right:0;height:${pageHeight}px;overflow:hidden;pointer-events:none;z-index:1;`;
                
                const watermarkDiv = doc.createElement('div');
                watermarkDiv.className = 'page-watermark';
                watermarkDiv.setAttribute('data-mce-bogus', 'all');
                watermarkDiv.contentEditable = 'false';
                watermarkDiv.textContent = watermarkRef.current;
                // Calculate font size so rotated watermark touches page edges at 100%
                //
                // Editor geometry (matches Letter page at 96dpi):
                //   Page body: 816px x 1056px (8.5" x 11")
                //   Watermark container: spans full page width (left:0, right:0)
                //   Page height: 1056px (container clips at this height)
                //
                // Transform order (CSS reads right-to-left):
                //   translate(-50%,-50%) rotate(-45deg) scaleY(2.0)
                //   1. scaleY(2.0): text box becomes width W, height 2H
                //   2. rotate(-45deg): bounding box becomes square-ish
                //   3. translate: centers it
                //
                // After scaleY(2) and rotate(-45deg), the bounding box dimensions are:
                //   rotatedWidth = W*cos(45) + 2H*sin(45) = 0.707*W + 1.414*H
                //   rotatedHeight = W*sin(45) + 2H*cos(45) = 0.707*W + 1.414*H
                //
                // Text dimensions:
                //   W = fontSize * charWidthRatio * textLen
                //   H = fontSize (line height ≈ font size)
                //
                // So: rotatedSize = fontSize * (0.707 * charWidthRatio * textLen + 1.414)
                //
                // For watermark to touch the PAGE edges (816px width) at 100%:
                //   fontSize = pageWidth / (0.707 * charWidthRatio * textLen + 1.414)
                //
                const wmSizePercent = parseInt(watermarkSizeRef.current || '75', 10);
                const textLen = watermarkRef.current.length;

                // Watermark sized to span the page edges — uses the orientation-
                // dependent page width set above (816 portrait / 1056 landscape).
                const charWidthRatio = 0.35; // Narrower ratio - accounts for condensed fonts and better visual fit
                const cos45 = 0.7071;
                const sin45 = 0.7071;
                const scaleYFactor = 1.5;
                
                // rotatedWidth = fontSize * (cos45 * charWidthRatio * textLen + sin45 * scaleYFactor)
                // fontSize = pageWidth / (0.707 * (charWidthRatio * textLen + 2))
                const maxFontSize = pageWidth / (cos45 * (charWidthRatio * textLen + scaleYFactor));
                const wmFontSize = Math.max(24, Math.round(maxFontSize * wmSizePercent / 100));
                // Convert hex color to rgba with 0.5 opacity (matches export)
                const wmColor = watermarkColorRef.current || '#c0c0c0';
                const r = parseInt(wmColor.slice(1, 3), 16);
                const g = parseInt(wmColor.slice(3, 5), 16);
                const b = parseInt(wmColor.slice(5, 7), 16);
                const wmFont = watermarkFontRef.current || "'Arial Black', sans-serif";
                // Position at center of page (50% of pageHeight), not content area
                watermarkDiv.style.cssText = `position:absolute;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-45deg) scaleY(1.5);font-family:${wmFont};font-size:${wmFontSize}px;color:rgba(${r},${g},${b},0.5);white-space:nowrap;pointer-events:none;user-select:none;cursor:default;`;
                wmContainer.appendChild(watermarkDiv);
                doc.body.appendChild(wmContainer);
              }
            }
            
            // Add page break bar at the END of the last page (at full page height)
            const lastPageEnd = finalNumPages * pageHeight;
            const endBreakDiv = doc.createElement('div');
            endBreakDiv.className = 'page-break-visual';
            endBreakDiv.setAttribute('data-mce-bogus', 'all');
            endBreakDiv.contentEditable = 'false';
            // Position at the actual page boundary
            endBreakDiv.style.cssText = `position:absolute;top:${lastPageEnd - 10}px;left:-96px;right:-96px;height:300px;background:#808080;pointer-events:none;z-index:20;`; 
            doc.body.appendChild(endBreakDiv);
          };
          
          editor.on('init', () => {
            // Skeleton injection moved out of the init handler — it now flows
            // in through TinyMCE's `initialValue` prop directly (set to
            // SKELETON_HTML when pendingMessage is active). The CSS classes
            // are already declared in content_style and the animation lives
            // in the iframe stylesheet so the skeleton renders as soon as
            // the body inflates with the initial content.

            // Small delay to ensure content is fully processed
            setTimeout(() => {
              const normalizedContent = editor.getContent({ format: 'raw' });
              originalContentRef.current = normalizedContent;
              // Sync buffer to backend if callback provided
              if (onBufferSync && !bufferSyncedRef.current) {
                bufferSyncedRef.current = true;
                onBufferSync(normalizedContent);
              }

              // Initial layout after content loads - use fixed position overlays
              setTimeout(() => {
                updatePageLayout();
                // Reset dirty state after initial layout (overlay injection triggers dirty)
                setTimeout(() => editor.setDirty(false), 50);
              }, 100);
            }, 100);
          });
          
          // Update page layout after every edit
          editor.on('input', () => {
            requestAnimationFrame(updatePageLayout);
          });

          // Recompute layout when the iframe body resizes. Window resize
          // and (in some browsers) browser zoom change body.offsetWidth,
          // which means pageHeight (= width / aspect) and break-visual
          // offsets need to be recomputed. Without this, breaks/figures
          // stay at their cached positions while the body reflows under
          // them, distorting visual page aspect at any zoom level other
          // than the one the doc loaded at. Debounce via rAF to coalesce
          // rapid resize events.
          let _layoutRafPending = false;
          const scheduleRelayout = () => {
            if (_layoutRafPending) return;
            _layoutRafPending = true;
            requestAnimationFrame(() => {
              _layoutRafPending = false;
              updatePageLayout();
            });
          };
          const _bodyResizeObserver = new ResizeObserver(scheduleRelayout);
          try {
            _bodyResizeObserver.observe(editor.getDoc().body);
          } catch { /* SSR / detached iframe — skip */ }
          // Also listen to window resize on the iframe's contentWindow
          // (browser zoom on Firefox triggers this even when offsetWidth
          // technically stays constant, because layout viewport adjusts).
          const _iframeWin = editor.getWin();
          if (_iframeWin) {
            _iframeWin.addEventListener('resize', scheduleRelayout);
          }
          // Outer-window listeners — the iframe's contentWindow does NOT
          // always fire 'resize' on browser-level zoom (Cmd+/-) because
          // the iframe CSS pixel dimensions stay constant. The OUTER
          // window's resize + a ResizeObserver on the iframe ELEMENT
          // itself catches:
          //   - browser-level zoom (Cmd+/-) — outer viewport scales,
          //     iframe rendered size changes physically
          //   - container layout changes (sidebar toggle, drawer resize)
          //     — iframe element's CSS pixel size changes
          // Without these, the page-aspect distortion the user reported
          // (figure box drifts off 16:9 on browser zoom) persists because
          // updatePageLayout never re-fires.
          window.addEventListener('resize', scheduleRelayout);
          const _iframeEl = editor.iframeElement as HTMLIFrameElement | undefined;
          const _outerIframeObserver = _iframeEl
            ? new ResizeObserver(scheduleRelayout)
            : null;
          try {
            if (_outerIframeObserver && _iframeEl) {
              _outerIframeObserver.observe(_iframeEl);
            }
          } catch { /* */ }
          editor.on('remove', () => {
            try { _bodyResizeObserver.disconnect(); } catch { /* */ }
            try { _outerIframeObserver?.disconnect(); } catch { /* */ }
            try { window.removeEventListener('resize', scheduleRelayout); } catch { /* */ }
            if (_iframeWin) {
              try { _iframeWin.removeEventListener('resize', scheduleRelayout); } catch { /* */ }
            }
          });

          // Edit-map button hook. The exporter emits
          // <figure class="ibl-map-embed" data-map-id data-bbox data-artifact-id>
          // wrapping the map <img> + a <button class="ibl-edit-map">. We catch
          // any click inside the iframe, walk up to the .ibl-edit-map button,
          // read its parent figure's data-* attrs, and open the operations-map
          // editor in a NEW TAB so DocEditor stays mounted in the original
          // tab. On Done editing the new tab broadcasts via BroadcastChannel
          // and closes; the listener above cache-busts the figure img.
          editor.on('click', (e: any) => {
            const target = e.target as HTMLElement | null;
            if (!target) return;
            const btn = target.closest('.ibl-edit-map') as HTMLElement | null;
            if (!btn) return;
            e.preventDefault();
            e.stopPropagation();
            const figure = btn.closest('figure.ibl-map-embed') as HTMLElement | null;
            if (!figure) {
              clientWarn('DocEditor.edit_map_no_parent', 'click landed on .ibl-edit-map but no enclosing figure.ibl-map-embed');
              return;
            }
            const mapId = figure.getAttribute('data-map-id') || '';
            const bbox = figure.getAttribute('data-bbox') || '';
            const artifactId = figure.getAttribute('data-artifact-id') || '';
            let ppt = '';
            try {
              const curParams = new URLSearchParams(window.location.search);
              ppt = curParams.get('pending_product_type') || '';
            } catch { /* SSR / no window */ }
            clientInfo('DocEditor.edit_map_open',
              `map_id=${mapId} artifact=${artifactId} product=${ppt}`);
            // No navigation. Mount the embedded map editor on top of
            // this DocEditor (TinyMCE editor stays mounted in the DOM,
            // hidden under the overlay). Done editing in the overlay
            // unmounts it + cache-busts the figure img — analyst sees
            // the just-edited map redraw with no context loss.
            setEditMapRef.current({
              mapId, bbox, artifactId, productType: ppt,
            });
          });
          
          // Handle backspace at start of pushed elements to allow page deletion
          editor.on('keydown', (e: KeyboardEvent) => {
            if (e.key === 'Backspace') {
              const selection = editor.selection;
              const node = selection.getNode();
              const rng = selection.getRng();
              
              // Find the block element
              let block: HTMLElement | null = node as HTMLElement;
              while (block && !['P', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'DIV', 'LI'].includes(block.tagName)) {
                block = block.parentElement;
              }
              
              if (block && block.hasAttribute('data-page-push')) {
                // Check if cursor is at the very start of this block
                if (rng.startOffset === 0) {
                  const firstChild = block.firstChild;
                  if (!firstChild || rng.startContainer === block || rng.startContainer === firstChild) {
                    // User is backspacing at start of a pushed element - remove its margin
                    block.style.marginTop = '';
                    block.removeAttribute('data-page-push');
                    requestAnimationFrame(updatePageLayout);
                  }
                }
              }
            }
          });
          
          // Helper to inject/update all document setting elements
          // Classification header/footer and watermark are rendered as React overlays
          // Document content stays clean - settings applied only on export

          // Classification is handled via CSS pseudo-elements (::before/::after)
          // No DOM injection needed - the useEffect updates the style when header changes
          
          // Document Settings menu items for top menubar
          
          // Classification menu items
          editor.ui.registry.addMenuItem('header_unclassified', {
            text: 'UNCLASSIFIED',
            onAction: () => {
              setHeader('UNCLASSIFIED');
              setHeaderStyle(defaultHeaderStyle);
            }
          });
          
          editor.ui.registry.addMenuItem('header_exercise', {
            text: 'FOR EXERCISE ONLY',
            onAction: () => {
              setHeader('FOR EXERCISE ONLY');
              setHeaderStyle(defaultHeaderStyle);
            }
          });
          
          editor.ui.registry.addMenuItem('header_pytho', {
            text: 'PYTHO GENERATED',
            onAction: () => {
              setHeader('PYTHO GENERATED');
              setHeaderStyle(defaultHeaderStyle);
            }
          });
          
          editor.ui.registry.addMenuItem('header_custom', {
            text: 'Custom...',
            onAction: () => {
              // Open React modal instead of TinyMCE dialog
              setTempHeaderText(headerRef.current !== 'none' ? headerRef.current : '');
              setTempStyle({...headerStyleRef.current});
              setShowHeaderModal(true);
            }
          });
          
          editor.ui.registry.addMenuItem('header_none', {
            text: 'None',
            onAction: () => {
              setHeader('none');
            }
          });
          
          // Header nested menu
          editor.ui.registry.addNestedMenuItem('header_menu', {
            text: 'Header',
            getSubmenuItems: () => 'header_unclassified header_exercise header_pytho header_custom header_none'
          });
          
          // Watermark menu items
          editor.ui.registry.addMenuItem('watermark_draft', {
            text: 'DRAFT',
            onAction: () => {
              setWatermark('DRAFT');
            }
          });
          
          editor.ui.registry.addMenuItem('watermark_final', {
            text: 'FINAL',
            onAction: () => {
              setWatermark('FINAL');
            }
          });
          
          editor.ui.registry.addMenuItem('watermark_custom', {
            text: 'Custom...',
            onAction: () => {
              // Open React modal instead of TinyMCE dialog
              setTempWatermarkText(watermarkRef.current !== 'none' ? watermarkRef.current : '');
              setTempWatermarkColor(watermarkColorRef.current || '#c0c0c0');
              setTempWatermarkFont(watermarkFontRef.current || "'Arial Black', sans-serif");
                      setTempWatermarkSize(watermarkSizeRef.current || '75');
              setShowWatermarkColorPicker(false);
              setShowWatermarkModal(true);
            }
          });
          
          editor.ui.registry.addMenuItem('watermark_none', {
            text: 'None',
            onAction: () => {
              setWatermark('none');
            }
          });
          
          // Watermark nested menu
          editor.ui.registry.addNestedMenuItem('watermark_menu', {
            text: 'Watermark',
            getSubmenuItems: () => 'watermark_draft watermark_final watermark_custom watermark_none'
          });

          // Translate menu items
          const translateLanguages = [
            'Arabic', 'Chinese (Simplified)', 'Chinese (Traditional)', 'Czech', 'Danish',
            'Dutch', 'English', 'Finnish', 'French', 'German', 'Greek', 'Hebrew', 'Hindi',
            'Hungarian', 'Indonesian', 'Italian', 'Japanese', 'Korean', 'Norwegian', 'Polish',
            'Portuguese', 'Romanian', 'Russian', 'Spanish', 'Swahili', 'Swedish', 'Thai', 'Turkish',
            'Ukrainian', 'Vietnamese',
          ];

          function translateDocument(targetLanguage: string) {
            const loadingDialog = editor.windowManager.open({
              title: `Translating to ${targetLanguage}...`,
              body: {
                type: 'panel',
                items: [{
                  type: 'htmlpanel',
                  html: `<p style="padding: 10px;">Translating document to ${targetLanguage}. This may take a few minutes...</p>`
                }]
              },
              buttons: []
            });

            try {
              const host = window.location.hostname + ':8000';
              const socket = new WebSocket(`ws://${host}/ws/chat/${user.userId || 'translate'}`);
              const docContent = editor.getContent({ format: 'raw' });

              socket.onopen = () => {
                socket.send(JSON.stringify({
                  action: 'chat',
                  type: 'doc_translate',
                  userId: user.userId,
                  email: user.email,
                  targetLanguage,
                  content: docContent,
                  sessionId,
                  projectId,
                }));
              };

              socket.onmessage = ({ data: msgData }) => {
                try {
                  const parsed = JSON.parse(msgData);
                  if (parsed.type === 'pong') return;

                  if (parsed.type === 'doc_translate_result') {
                    socket.close();
                    loadingDialog.close();
                    if (parsed.status === 'success' && parsed.content) {
                      editor.setContent(parsed.content);
                    } else {
                      editor.windowManager.alert(`Translation failed: ${parsed.error || 'Unknown error'}`);
                    }
                    return;
                  }

                  if (parsed.type === 'error') {
                    socket.close();
                    loadingDialog.close();
                    editor.windowManager.alert(parsed.message || 'Translation failed');
                  }
                } catch (_) { /* ignore non-JSON */ }
              };

              socket.onerror = () => {
                loadingDialog.close();
                editor.windowManager.alert('Connection error. Please try again.');
              };
            } catch (e) {
              console.error('[DocEditor] Translation error:', e);
              loadingDialog.close();
              editor.windowManager.alert('Something went wrong!');
            }
          }

          const translateMenuItemNames: string[] = [];
          translateLanguages.forEach((lang) => {
            const itemName = `translate_${lang.toLowerCase().replace(/[^a-z]/g, '_')}`;
            translateMenuItemNames.push(itemName);
            editor.ui.registry.addMenuItem(itemName, {
              text: lang,
              onAction: () => translateDocument(lang),
            });
          });

          // Shared save function - prepares HTML and hands off to parent
          const performSave = async () => {
            // Get content - 'html' format respects data-mce-bogus and excludes our overlays
            let body = editor.getContent({ format: 'html' });
            
            // Get current values from refs
            const currentHeader = headerRef.current;
            const currentHeaderStyle = headerStyleRef.current;
            const currentWatermark = watermarkRef.current;
            const currentWatermarkColor = watermarkColorRef.current;
            const currentWatermarkFont = watermarkFontRef.current;
            const currentWatermarkSize = watermarkSizeRef.current;
            
            // Build inline styles for header
            const fontWeight = currentHeaderStyle.bold ? 'bold' : 'normal';
            const fontStyle = currentHeaderStyle.italic ? 'italic' : 'normal';
            const textDecoration = currentHeaderStyle.underline ? 'underline' : 'none';
            
            // Calculate watermark font size with percentage scaling
            const wmSizePercent = parseInt(currentWatermarkSize || '100', 10);
            const wmBaseSize = currentWatermark && currentWatermark !== 'none' 
              ? Math.max(80, Math.min(180, 800 / currentWatermark.length))
              : 120;
            const watermarkFontSize = Math.round(wmBaseSize * wmSizePercent / 100);
            
            // Include all document settings CSS in export
            const docSettingsCSS = `
              ${generatePageCSS()}
              
              /* Body setup */
              body {
                font-family: Arial, sans-serif;
                margin: 0;
                padding: 1in; /* 1 inch margins */
                min-height: 100vh;
                box-sizing: border-box;
                position: relative;
              }
              
              /* Classification header */
              .doc-header {
                display: block;
                width: 100%;
                text-align: ${currentHeaderStyle.align};
                font-family: ${currentHeaderStyle.fontFamily};
                font-size: ${currentHeaderStyle.fontSize};
                font-weight: ${fontWeight};
                font-style: ${fontStyle};
                text-decoration: ${textDecoration};
                color: ${currentHeaderStyle.textColor};
                background-color: transparent;
                padding: 12px 0;
                margin: 0 0 20px 0;
                line-height: 1.4;
              }
              
              /* Classification footer */
              .doc-footer {
                display: block;
                width: 100%;
                text-align: ${currentHeaderStyle.align};
                font-family: ${currentHeaderStyle.fontFamily};
                font-size: ${currentHeaderStyle.fontSize};
                font-weight: ${fontWeight};
                font-style: ${fontStyle};
                text-decoration: ${textDecoration};
                color: ${currentHeaderStyle.textColor};
                background-color: transparent;
                padding: 12px 0;
                margin: 20px 0 0 0;
                line-height: 1.4;
              }
              
              /* Watermark - centered behind content */
              .doc-watermark {
                position: fixed;
                top: 50%;
                left: 50%;
                transform: translate(-50%, -50%) rotate(-45deg);
                font-size: ${watermarkFontSize}px;
                font-family: Arial, sans-serif;
                font-weight: bold;
                color: rgba(180, 180, 180, 0.25);
                pointer-events: none;
                z-index: -1;
                white-space: nowrap;
                user-select: none;
                letter-spacing: 8px;
              }
              
              /* Page number */
              .doc-page-number {
                display: block;
                width: 100%;
                text-align: center;
                font-family: ${currentHeaderStyle.fontFamily};
                font-size: 11pt;
                color: #000000;
                background-color: transparent;
                margin-top: 16px;
                padding: 0;
                line-height: 1.2;
              }
              
              /* Page break styling for export/print */
              .mce-pagebreak {
                display: block;
                page-break-after: always;
                break-after: page;
                border: 0;
                margin: 0;
                padding: 0;
                height: 0;
                visibility: hidden;
              }
              
              @media print {
                @page {
                  size: A4;
                  margin: 1in; /* 1 inch margins */
                }
                
                body {
                  padding: 0;
                }
                
                .doc-watermark {
                  position: fixed;
                  top: 50%;
                  left: 50%;
                  transform: translate(-50%, -50%) rotate(-45deg);
                }
                
                .mce-pagebreak {
                  page-break-after: always;
                  break-after: page;
                }
              }
            `;
            
            // Build header/footer/watermark HTML
            const position = currentHeaderStyle.position || 'header';
            
            // Use inline styles for Word compatibility
            const exportTextColor = currentHeaderStyle.textColor || '#808080';
            const headerStyle = `display:block;width:100%;text-align:${currentHeaderStyle.align};font-family:${currentHeaderStyle.fontFamily};font-size:${currentHeaderStyle.fontSize};font-weight:${fontWeight};font-style:${fontStyle};text-decoration:${textDecoration};color:${exportTextColor};padding:12px 0;margin:0 0 20px 0;`;
            const footerStyle = `display:block;width:100%;text-align:${currentHeaderStyle.align};font-family:${currentHeaderStyle.fontFamily};font-size:${currentHeaderStyle.fontSize};font-weight:${fontWeight};font-style:${fontStyle};text-decoration:${textDecoration};color:${exportTextColor};padding:12px 0;margin:20px 0 0 0;`;
            
            const headerHTML = currentHeader && currentHeader !== 'none' && (position === 'header' || position === 'both')
              ? `<div style="${headerStyle}">${currentHeader}</div>` 
              : '';
            
            const footerHTML = currentHeader && currentHeader !== 'none' && (position === 'footer' || position === 'both')
              ? `<div style="${footerStyle}">${currentHeader}</div>` 
              : '';
            
            // VML Watermark for Word compatibility - match editor style
            // Convert watermark color to RGB for rgba()
            const wmColorHex = currentWatermarkColor || '#c0c0c0';
            const wmR = parseInt(wmColorHex.slice(1, 3), 16);
            const wmG = parseInt(wmColorHex.slice(3, 5), 16);
            const wmB = parseInt(wmColorHex.slice(5, 7), 16);
            
            // Extract primary font name for VML (first font in the stack, without quotes)
            const wmFontStack = currentWatermarkFont || "'Arial Black', sans-serif";
            const wmPrimaryFont = wmFontStack.split(',')[0].trim().replace(/['"]/g, '');
            
            // VML textpath doesn't support bold/italic/underline - use font-weight:bold for emphasis
            
            const watermarkHTML = currentWatermark && currentWatermark !== 'none' 
              ? `<!--[if gte mso 9]>
                <v:shapetype id="_x0000_t136" coordsize="21600,21600" o:spt="136" adj="10800" path="m@7,l@8,m@5,21600l@6,21600e">
                  <v:formulas>
                    <v:f eqn="sum #0 0 10800"/>
                    <v:f eqn="prod #0 2 1"/>
                    <v:f eqn="sum 21600 0 @1"/>
                    <v:f eqn="sum 0 0 @2"/>
                    <v:f eqn="sum 21600 0 @3"/>
                    <v:f eqn="if @0 @3 0"/>
                    <v:f eqn="if @0 21600 @1"/>
                    <v:f eqn="if @0 0 @2"/>
                    <v:f eqn="if @0 @4 21600"/>
                    <v:f eqn="mid @5 @6"/>
                    <v:f eqn="mid @8 @5"/>
                    <v:f eqn="mid @7 @8"/>
                    <v:f eqn="mid @6 @7"/>
                    <v:f eqn="sum @6 0 @5"/>
                  </v:formulas>
                  <v:path textpathok="t" o:connecttype="custom" o:connectlocs="@9,0;@10,10800;@11,21600;@12,10800" o:connectangles="270,180,90,0"/>
                  <v:textpath on="t" fitshape="t"/>
                  <v:handles>
                    <v:h position="#0,bottomRight" xrange="6629,14971"/>
                  </v:handles>
                  <o:lock v:ext="edit" text="t" shapetype="t"/>
                </v:shapetype>
                <v:shape type="#_x0000_t136" style="position:absolute;margin-left:0;margin-top:0;width:800pt;height:280pt;rotation:315;z-index:-251658752;mso-position-horizontal:center;mso-position-horizontal-relative:margin;mso-position-vertical:center;mso-position-vertical-relative:margin" fillcolor="${wmColorHex}" stroked="f">
                  <v:fill opacity=".5"/>
                  <v:textpath style="font-family:&quot;${wmPrimaryFont}&quot;;font-size:72pt;font-weight:bold;v-text-kern:t" fitshape="t" string="${currentWatermark}"/>
                </v:shape>
                <![endif]-->
                <div style="position:fixed;top:50%;left:50%;transform:translate(-50%,-50%) rotate(-45deg) scaleY(1.5);font-size:210pt;font-family:${wmFontStack};color:rgba(${wmR},${wmG},${wmB},0.5);pointer-events:none;z-index:-1;white-space:nowrap;">${currentWatermark}</div>` 
              : '';
            
            const html = `<!DOCTYPE html>
<html xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">
  <head>
    <meta charset="UTF-8">
    <!--[if gte mso 9]>
    <xml>
      <o:shapedefaults v:ext="edit" spidmax="1026"/>
    </xml>
    <![endif]-->
    <style type="text/css">
    v\\:* { behavior: url(#default#VML); }
    o\\:* { behavior: url(#default#VML); }
    body {
      font-family: Arial, sans-serif;
      margin: 0;
      padding: 1in;
      min-height: 100vh;
      box-sizing: border-box;
      position: relative;
    }
    ol, ul {
      margin: 0.5em 0;
      padding-left: 2em;
    }
    li {
      margin: 0.25em 0;
    }
    .doc-header, .doc-footer {
      display: block;
      width: 100%;
      text-align: ${currentHeaderStyle.align};
      font-family: ${currentHeaderStyle.fontFamily};
      font-size: ${currentHeaderStyle.fontSize};
      font-weight: ${fontWeight};
      font-style: ${fontStyle};
      text-decoration: ${textDecoration};
      color: ${currentHeaderStyle.textColor};
      padding: 12px 0;
    }
    .doc-header {
      margin: 0 0 20px 0;
    }
    .doc-footer {
      margin: 20px 0 0 0;
    }
    .doc-watermark {
      position: fixed;
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%) rotate(-45deg) scaleY(2.0);
      font-size: 200pt;
      font-family: Calibri, Arial, sans-serif;
      font-weight: bold;
      color: rgba(192, 192, 192, 0.5);
      pointer-events: none;
      z-index: -1;
      white-space: nowrap;
    }
    .doc-page-number {
      display: block;
      width: 100%;
      text-align: center;
      font-family: ${currentHeaderStyle.fontFamily};
      font-size: 11pt;
      margin-top: 20px;
    }
    @media print {
      @page { size: A4; margin: 1in; }
      body { padding: 0; }
      .doc-watermark {
        position: fixed;
        top: 50%;
        left: 50%;
      }
    }
    ${extractedStyles}
    </style>
  </head>
  <body>
    ${watermarkHTML}
    ${headerHTML}
    ${body}
    ${footerHTML}
  </body>
</html>`
            
            // Compute edit deltas for agent learning + buffer sync
            if (onEditDeltas && originalContentRef.current) {
              const deltas = computeEditDeltas(originalContentRef.current, body);
              if (deltas.length > 0) {
                onEditDeltas(deltas);
                // Update original so next save only captures incremental changes
                originalContentRef.current = body;
              }
            }
            
            // Build document settings JSON to embed in HTML. `pageOrientation`
            // round-trips so convert.js can re-select the landscape branch on
            // save-back (DocEditor preview already flipped orientation based on
            // the value the exporter wrote).
            const docSettingsJson = JSON.stringify({
              header: currentHeader,
              headerStyle: currentHeaderStyle,
              watermark: currentWatermark,
              watermarkColor: currentWatermarkColor,
              watermarkFont: currentWatermarkFont,
              watermarkSize: currentWatermarkSize,
              pageOrientation: pageOrientationRef.current
            });
            
            // Build clean HTML for saving (no headers/watermarks - just content, but with embedded settings)
            const cleanHtml = `<!DOCTYPE html>
<html>
  <head>
    <meta charset="UTF-8">
    <script type="application/json" id="pytho-doc-settings">${docSettingsJson}</script>
    <style type="text/css">
    body {
      font-family: Arial, sans-serif;
      margin: 0;
      padding: 1in;
      min-height: 100vh;
      box-sizing: border-box;
    }
    ${extractedStyles}
    </style>
  </head>
  <body>
    ${body}
  </body>
</html>`;
            
            // Pass clean HTML for save, plus document settings for DOCX generation
            if (onSaveStart) {
              onSaveStart(cleanHtml || body, {
                header: currentHeader,
                headerStyle: currentHeaderStyle,
                watermark: currentWatermark,
                watermarkColor: currentWatermarkColor,
                watermarkFont: currentWatermarkFont,
                watermarkSize: currentWatermarkSize
              });
            }
          };

          // Register save function for external access (e.g., from modal)
          if (onRegisterSave) {
            onRegisterSave(performSave);
          }
    
          // Register save and export menu item
          editor.ui.registry.addMenuItem('saveAndExport', {
            icon: "save",
            text: 'Save and Export',
            onAction: async () => {
              if (editor.isDirty()) {
                await performSave();
              } else if (onSave) {
                onSave();
              }
            }
          });

          editor.ui.registry.addButton('ai_button', {
            icon: 'ai-prompt',
            text: 'Planning Agent',
            onAction: function () {
              // Get the currently selected node
              const selectedNode = editor.selection.getNode();

              // Try to find the nearest <table> ancestor
              const tableNode = editor.dom.getParent(selectedNode, 'table');

              // Return the table if found, otherwise return the selected node
              const resultNode = tableNode || selectedNode;

              const selectedText = resultNode.outerHTML;
              if (!selectedText) return;
              
              // STEP 1: Prompt Dialog
              editor.windowManager.open({
                width: 800,   // width in pixels
                height: 400,  // height in pixels
                title: 'Ask Planning Agent',
                body: {
                  type: 'panel',
                  items: [
                    {
                      type: 'textarea',
                      name: 'prompt',
                      label: 'Enter your request',
                    },
                    {
                      type: 'htmlpanel',
                      html: '<div style="margin: 12px 0;"></div>'
                    }
                  ]
                },
                
                buttons: [
                  { type: 'cancel', text: 'Cancel' },
                  { type: 'submit', text: 'Ask', primary: true }
                ],
                onSubmit: function (api) {

                  const loadingDialog = editor.windowManager.open({
                    title: 'Waiting for Planning Agent...',
                    body: {
                      type: 'panel',
                      items: [
                        {
                          type: 'htmlpanel',
                          html: `<p style="padding: 10px;">⏳ Waiting for the response...</p>`
                        }
                      ]
                    },
                    buttons: []
                  });

                  const question = api.getData().prompt;
                  api.close();

                  try {
                    const socket = new WebSocket(config.webSocketApiGateway.URL);
                    const document = editor.getContent({ format: 'raw' });
                    
                    socket.onopen = () => {
                      socket.send(
                        JSON.stringify({
                          action: 'chat',
                          type: 'doc_chat',
                          userId: user.userId,
                          email: user.email,
                          firstName: getFirstName(user),
                          lastName: getLastName(user),
                          question,
                          context: selectedText,
                          document,
                          projectId
                        })
                      );
                    };

                    socket.onmessage = ({ data }) => {
                      data = JSON.parse(data)
                      socket.close();
                      loadingDialog.close()
                      if (data.errorMessage) {
                        editor.windowManager.alert('Something went wrong!');
                        return
                      }
                      editor.windowManager.open({
                        title: `Planning Agent's Suggestion`,
                        body: {
                          type: 'panel',
                          items: [
                            {
                              type: 'htmlpanel',
                              html: `<div style="white-space: pre-wrap; background:#F5F5F5; padding:10px; border-radius:4px;">
                                      ${data.answer}
                                    </div>`
                            }
                          ]
                        },
                        buttons: [
                          { type: 'cancel', text: 'Cancel' },
                          {
                            type: 'custom',
                            name: 'use',
                            text: 'Use this',
                            primary: true
                          }
                        ],
                        onAction: function (confirmApi, details) {
                          if (details.name === 'use') {
                            editor.dom.setOuterHTML(resultNode, data.answer);
                            confirmApi.close();
                          }
                        }
                      })
                    };

                  } catch (e) {
                    console.log(e)
                    editor.windowManager.alert('Something went wrong!');
                  }
                }
              });
            },
            onSetup: function (buttonApi) {
              const handler = () => {
                const selectedText = editor.selection.getContent({ format: 'text' }).trim();
                if (selectedText.length > 1000) {
                  editor.windowManager.alert('Selected text is too long. Please select a shorter portion');
                  return
                }

                buttonApi.setEnabled(selectedText.length > 0);
              };
              editor.on('NodeChange', handler);
              return () => editor.off('NodeChange', handler);
            }
          });
        },

        // Add custom menus
        menu: {
          file: {
            title: 'File',
            items: 'newdocument saveAndExport print'
          },
          docsettings: {
            title: 'Document Settings',
            items: 'header_menu watermark_menu'
          },
          translate: {
            title: 'Translate',
            items: 'translate_arabic translate_chinese__simplified_ translate_chinese__traditional_ translate_czech translate_danish translate_dutch translate_english translate_finnish translate_french translate_german translate_greek translate_hebrew translate_hindi translate_hungarian translate_indonesian translate_italian translate_japanese translate_korean translate_norwegian translate_polish translate_portuguese translate_romanian translate_russian translate_spanish translate_swahili translate_swedish translate_thai translate_turkish translate_ukrainian translate_vietnamese'
          }
        }
    

      }}
      initialValue={
        // Generate-button flow: when pendingMessage is set and no artifact
        // is loaded yet, feed the animated skeleton HTML directly as
        // TinyMCE's initial content. The CSS classes are declared in
        // content_style and the keyframes live in the iframe stylesheet,
        // so the skeleton animates as soon as TinyMCE inflates the body.
        // When the real artifact lands, extractedBody is non-empty and the
        // editorKey changes — TinyMCE remounts with the artifact content.
        (pendingMessage && !extractedBody)
          ? (
              '<div class="pytho-skeleton-root" contenteditable="false">' +
                '<div class="pytho-skel pytho-skel-classif"> </div>' +
                '<div class="pytho-skel pytho-skel-title"> </div>' +
                '<div class="pytho-skel pytho-skel-line-a"> </div>' +
                '<div class="pytho-skel pytho-skel-line-b"> </div>' +
                '<div class="pytho-skel pytho-skel-line-c"> </div>' +
                '<div class="pytho-skel-table">' +
                  [0,1,2,3,4,5].map(r => (
                    '<div class="pytho-skel-tr">' +
                      [0,1,2,3].map(c => (
                        `<div class="pytho-skel-cell" style="animation-delay:${500 + r*80 + c*40}ms"> </div>`
                      )).join('') +
                    '</div>'
                  )).join('') +
                '</div>' +
                '<div class="pytho-skel pytho-skel-footer"> </div>' +
                '<div class="pytho-skel pytho-skel-pg"> </div>' +
              '</div>'
            )
          : extractedBody
      }
      onDirty={() => {
        if (onDirtyChange) onDirtyChange(true);
      }}
    />
          {/* Pending skeleton was a React overlay here — moved INSIDE the
              TinyMCE iframe via editor.on('init') → setContent(SKELETON_HTML).
              That way it inherits the doctrinal page bounds + margins from
              doc-editor-body, and the watermark is suppressed via
              pendingMessageRef in updatePageLayout while the skeleton shows. */}
        </div>
        {/* ChatAgent removed — now mounted at chrome level via
            ChatAgent (App.tsx). DocEditorPage handles the openChat
            handshake (initialMessage / manualProductType / onArtifact). */}
      </div>
    </div>
  );
}