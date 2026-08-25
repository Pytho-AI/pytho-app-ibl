"""
DocGen: Self-contained document generation pipeline for edge deployment.

Structure analysis → route to single-pass (letters/memos) or multi-pass (sectioned docs).

Uses vLLM's OpenAI-compatible /v1/chat/completions with vision support for
template format replication. Base64 page images are passed as OpenAI
`image_url` content parts (data URIs) attached to the last user message.
"""
import re
import os
import time
import logging
from typing import Callable, Optional
from pathlib import Path

from utils import config, llm

logger = logging.getLogger(__name__)

# vLLM serves a single model selected at serve time. DOCGEN_SCAFFOLD_MODEL /
# DOCGEN_WRITER_MODEL config values are accepted but not forwarded — kept only
# to preserve existing call signatures.

# =============================================================================
# PROMPT TEMPLATES
# =============================================================================

# Analyzes the template to determine if the document has explicit section headings.
# Output is either "SINGLE" (continuous prose like a letter/memo) or
# "SECTIONS:\nHeading1\nHeading2\n..." (document with real named sections).
# Step 1 — vision only: does the document have explicit sections? (yes/no)
_STRUCTURE_VISION_PROMPT = """Look at this document image: [img]

Does it have EXPLICIT section headings that visually divide it into distinct named parts?

RULES:
- Only standalone lines formatted as TITLES or HEADINGS count (bold, centered, uppercase, or clearly separated from body text)
- "From:", "To:", "Date:", "Subject:", "Reference:" are metadata fields -- NOT section headings
- Document titles (e.g. "DIPLOMATIC NOTE", "MEMORANDUM", "WARNING ORDER") are letterhead -- NOT section headings
- Salutations, closing phrases, and signature blocks are NOT section headings
- Continuous flowing paragraphs without named dividers = SINGLE

Output exactly one word -- nothing else:
SINGLE
or
SECTIONS"""

# Step 1b — vision: count sections and analyze structure.
_STRUCTURE_ANALYSIS_PROMPT = """This image shows $PAGE_COUNT$ pages of a document laid out side by side horizontally, separated by thin black borders.

I need you to count entries. An entry begins with a bold heading line (it could be a date like "March 8, 2025" or "14 June 2025", a topic, etc.) and is followed by one or more body paragraphs. Multiple entries appear on each page. Exclude only the document title block at the very top of page 1.

Count per page then total:
$PAGE_LINES$
TOTAL: [sum]

Then on new lines:
ORGANIZING_PRINCIPLE: [what organizes the entries — dates/timeline, topics, phases, numbered items, etc.]
WRITING_REGISTER: [tone, source attribution style, formality, classification conventions]"""

# Step 2 — SCAFFOLD MODE (ACTIVE): produce HTML scaffolds with {{EXPAND}} placeholders for the writer.
_STRUCTURE_HEADINGS_PROMPT = """You are a document planning agent. Produce an HTML scaffold for each section of the new document. A writer will expand the placeholders — you handle structure, styling, and short fixed content.

*** STRUCTURE ANALYSIS (from a prior analysis of the template): ***
$STRUCTURE_ANALYSIS$

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!! NON-NEGOTIABLE: YOUR OUTPUT MUST CONTAIN EXACTLY $ENTRY_COUNT$ !!!
!!! ENTRY SECTIONS (plus one section 0 for the header block).      !!!
!!! THAT IS $ENTRY_COUNT$ ---SECTION--- BLOCKS FOR ENTRIES.        !!!
!!! IF YOU PRODUCE FEWER THAN $ENTRY_COUNT$ ENTRIES, YOUR OUTPUT   !!!
!!! WILL BE REJECTED AND YOU WILL HAVE TO REDO IT.                 !!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

*** TEMPLATE HTML (copy tag names and inline style attributes exactly): ***

$SKELETON_HTML$

!!! CRITICAL CONTENT RULES: !!!
!!! FROM THE TEMPLATE COPY ONLY: tag names, inline style attributes, structural layout. !!!
!!! ALL TEXT CONTENT comes from the TASK — events, dates, actors, scenario names, everything. The TASK tells you what world you are in and what to write about. !!!
!!! DO NOT add classification markings, confidentiality banners, or security labels (e.g. CONFIDENTIAL, EYES ONLY, SECRET) — these are added in post-processing and must NOT appear in your output. !!!
!!! OUTPUT RAW HTML ONLY — NO MARKDOWN CODE FENCES (no ```html, no ```). !!!

*** TIMELINE RULES: ***
- Current date/time: $UTC_NOW$.
- Every date in your output MUST fall within the TASK's stated date range — no exceptions. If the TASK does not specify a date range, derive an appropriate range from the current date/time and the template's date span length.
- ALL DATES MUST USE A CONSISTENT FORMAT THROUGHOUT THE DOCUMENT. Pick the format used in the template and use it for every single date.
- Follow the TASK. If it specifies events, use them. If it is high-level and does not specify events, invent plausible ones within the stated scenario, actors, and date range to fill the document.
- Distribute events realistically across the full date range — do NOT cluster everything at the start or end.
- Events happen ONCE. Do NOT repeat or re-describe the same event on a different date.
- Do NOT copy events from the template.
- Section scopes must be consecutive and non-overlapping — no date appears in two sections.

*** CONTENT RULES: ***
- Every section heading must be UNIQUE and DESCRIPTIVE — no two sections may share the same heading.
- All specifics in {{EXPAND}} descriptions (dates, actors, events) must come from the TASK.

*** YOUR JOB: ***
1. Read the TASK fully. Extract every date, event, actor, organisation, and key fact from it.
2. Read the STRUCTURE ANALYSIS above — it tells you the entry count, organizing principle, and writing register of the template.
3. YOU MUST PRODUCE EXACTLY $ENTRY_COUNT$ ENTRY SECTIONS. This is MANDATORY — not approximate, not "around", EXACTLY $ENTRY_COUNT$. Count your ---SECTION--- blocks as you write. If the TASK has fewer events than entries needed, break events into granular sub-entries: for timeline/chronological formats, split TASK events across multiple dated entries (initial report, intelligence assessment, escalation indicator, diplomatic signal, follow-up confirmation, etc.) to fill the required count. Do NOT collapse multiple entries into one. Do NOT stop early.
4. Assign TASK content to sections following the template's organising principle (date ranges, phases, topics, etc.). Every date in a section MUST fall within that section's scope and within the TASK's overall date range.
5. For EACH section produce a MINIMAL HTML scaffold — your scaffold must be SHORT (under 100 tokens per section):
   - The ONLY text you write directly is: the bold heading/date line in its <p><strong> tag.
   - EVERYTHING ELSE in the section is a single {{EXPAND: one-sentence description}} placeholder inside a <p> tag.
   - DO NOT write sentences, paragraphs, or prose of any kind. NO body text. The writer does ALL writing.
   - A typical section scaffold looks like this (3-4 lines max):
     <p style="..."><strong>March 15, 2025 – Heading Here</strong></p>
     <p style="...">{{EXPAND: describe what the writer should write about, 10-20 words}}</p>
   - If you write ANY body text yourself, the document will be REJECTED.
6. Section 0 — header block: reproduce ONLY the lines that exist in the template header — do NOT add any lines not present in the template. For each line:
   - Alphanumeric codes, series labels, document type titles: copy the TEXT CHARACTER FOR CHARACTER from the template — do not alter spelling, spacing, or numbers.
   - Scenario/conflict description: derive from the TASK.
   - Date range: use TASK start and end dates exactly, in the same format as the template.
   - DO NOT add classification markings, confidentiality banners, or security labels of any kind — these are added in post-processing and must NOT appear in your output.
7. For the writing register: note if/how the template attributes sources (SIGINT, HUMINT, OSINT, DIA, etc.), its tone, writing style, verbosity, word choice, etc. — describe this in each {{EXPAND}} so the writer matches it as closely as possible.
8. Do NOT invent tags or styles not present in the template HTML. Do NOT use <div>, pixel sizes, or colour values.
9. HTML FORMATTING — copy the EXACT tags, nesting, and style="..." strings from the template HTML, character for character:
   - When you write a <p>, copy the EXACT tag and style="..." from the equivalent <p> in the template — do NOT invent or paraphrase
   - When you write a <li>, copy the EXACT tag and style="..." from the equivalent <li> in the template
   - Copy <strong>, <em>, <u> nesting exactly as it appears in the template — do NOT add style attributes inside them
   - Alignment (text-align) goes on the <p> style attribute only, copied from the template
   - Allowed tags ONLY: <p>, <h1>-<h6>, <ul>, <ol>, <li>, <strong>, <em>, <u>
   - FORBIDDEN: <div>, <section>, <header>, <center>, <br>, <span>, <sup>, <sub>, <b>, <i>
   - Do NOT use markdown (no *asterisks*, no _underscores_) — use <strong> and <em> tags instead
   - Every opened tag must have exactly one matching closing tag

*** OUTPUT FORMAT — ---SECTION--- delimiter, first line is label | estimated writer expansion tokens: ***

---SECTION---
[label] | [estimated tokens for writer to expand]
[complete HTML scaffold with real values and {{EXPAND:...}} placeholders]
---SECTION---
[label] | [estimated tokens]
[HTML scaffold]

*** TASK — ALL TEXT CONTENT COMES FROM HERE: ***
$TASK$"""

# Step 2 — DIRECT MODE (BACKUP): write the full document content in one pass.
_STRUCTURE_HEADINGS_PROMPT_DIRECT = """You are a document writer. Write the COMPLETE document as finished HTML, ready for conversion to DOCX. Match the template's structure, formatting, density, and length exactly.

*** STRUCTURE ANALYSIS (from a prior analysis of the template): ***
$STRUCTURE_ANALYSIS$

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!! NON-NEGOTIABLE: YOUR OUTPUT MUST CONTAIN EXACTLY $ENTRY_COUNT$ !!!
!!! ENTRIES (plus the header block).                                !!!
!!! IF YOU PRODUCE FEWER THAN $ENTRY_COUNT$ ENTRIES, YOUR OUTPUT   !!!
!!! WILL BE REJECTED AND YOU WILL HAVE TO REDO IT.                 !!!
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

*** TEMPLATE HTML (copy tag names and inline style attributes exactly): ***

$SKELETON_HTML$

!!! CRITICAL CONTENT RULES: !!!
!!! FROM THE TEMPLATE COPY ONLY: tag names, inline style attributes, structural layout. !!!
!!! ALL TEXT CONTENT comes from the TASK — events, dates, actors, scenario names, everything. The TASK tells you what world you are in and what to write about. !!!
!!! DO NOT add classification markings, confidentiality banners, or security labels (e.g. CONFIDENTIAL, EYES ONLY, SECRET) — these are added in post-processing and must NOT appear in your output. !!!
!!! OUTPUT RAW HTML ONLY — NO MARKDOWN CODE FENCES (no ```html, no ```). !!!

*** TIMELINE RULES: ***
- Every date in your output MUST fall within the TASK's stated date range — no exceptions.
- Follow the TASK. If it specifies events, use them. If it is high-level and does not specify events, invent plausible ones within the stated scenario, actors, and date range to fill the document.
- Distribute events realistically across the full date range — do NOT cluster everything at the start or end.
- Events happen ONCE. Do NOT repeat or re-describe the same event on a different date.
- Do NOT copy events from the template.

*** CONTENT RULES: ***
- Every entry heading must be UNIQUE and DESCRIPTIVE — no two entries may share the same heading.
- Match the DENSITY and LENGTH of each entry to the template — if template entries are 1-3 paragraphs, write 1-3 paragraphs. Do NOT write walls of text.
- Each entry must have the same level of detail, source attribution style, and analytical depth as template entries.

*** YOUR JOB: ***
1. Read the TASK fully. Extract every date, event, actor, organisation, and key fact from it.
2. Read the STRUCTURE ANALYSIS above — it tells you the entry count, organizing principle, and writing register of the template.
3. YOU MUST PRODUCE EXACTLY $ENTRY_COUNT$ ENTRIES. This is MANDATORY — not approximate, not "around", EXACTLY $ENTRY_COUNT$. Count your entries as you write. If the TASK has fewer events than entries needed, break events into granular sub-entries: for timeline/chronological formats, split TASK events across multiple dated entries (initial report, intelligence assessment, escalation indicator, diplomatic signal, follow-up confirmation, etc.) to fill the required count. Do NOT collapse multiple entries into one. Do NOT stop early.
4. Assign TASK content to entries following the template's organising principle (date ranges, phases, topics, etc.).
5. For EACH entry, write the COMPLETE finished content:
   - Bold heading/date line in a <p><strong> tag, matching the template's heading format.
   - Full body paragraphs with finished prose — write as if you are the analyst/author. Match the template's writing register, tone, source attribution style, and paragraph density.
   - Do NOT use placeholders, brackets, or TODO markers. Every entry must be complete, finished text.
6. Header block: reproduce ONLY the lines that exist in the template header — do NOT add any lines not present in the template. For each line:
   - Alphanumeric codes, series labels, document type titles: copy the TEXT CHARACTER FOR CHARACTER from the template — do not alter spelling, spacing, or numbers.
   - Scenario/conflict description: derive from the TASK.
   - Date range: use TASK start and end dates exactly, in the same format as the template.
   - DO NOT add classification markings, confidentiality banners, or security labels of any kind — these are added in post-processing and must NOT appear in your output.
7. Do NOT invent tags or styles not present in the template HTML. Do NOT use <div>, pixel sizes, or colour values.
8. HTML FORMATTING — copy the EXACT tags, nesting, and style="..." strings from the template HTML, character for character:
   - When you write a <p>, copy the EXACT tag and style="..." from the equivalent <p> in the template — do NOT invent or paraphrase
   - When you write a <li>, copy the EXACT tag and style="..." from the equivalent <li> in the template
   - Copy <strong>, <em>, <u> nesting exactly as it appears in the template — do NOT add style attributes inside them
   - Alignment (text-align) goes on the <p> style attribute only, copied from the template
   - Allowed tags ONLY: <p>, <h1>-<h6>, <ul>, <ol>, <li>, <strong>, <em>, <u>
   - FORBIDDEN: <div>, <section>, <header>, <center>, <br>, <span>, <sup>, <sub>, <b>, <i>
   - Do NOT use markdown (no *asterisks*, no _underscores_) — use <strong> and <em> tags instead
   - Every opened tag must have exactly one matching closing tag

*** OUTPUT FORMAT — ---SECTION--- delimiter, first line is the entry label | 0: ***

---SECTION---
[entry label] | 0
[complete finished HTML for this entry — header block, or full entry with heading + body paragraphs]

*** TASK — ALL TEXT CONTENT COMES FROM HERE: ***
$TASK$"""


# Single-pass: generate the COMPLETE document in one call.
# Used for continuous documents (letters, memos, diplomatic notes).
_WRITE_SINGLE_PROMPT = """You are replicating a document based on a reference template. Study the REFERENCE DOCUMENT carefully and generate a new document that follows its EXACT structure, header block, field order, formatting, tone, and length -- adapted with new content matching the task.

Today's date is $TODAY$.

$TEMPLATE_SECTION$

TASK: $INST$

DOCX-COMPATIBLE HTML RULES (MANDATORY):
- Use ONLY: <p>, <h1>-<h6>, <ul>, <ol>, <strong>, <em>, <u>
- EVERY <p> and <li> MUST have the exact font-family and font-size from the reference (copy it from the reference style attributes)
- Alignment on the <p> itself: add text-align to the same style attribute
- Do NOT use <center>, <br>, <div>, or Markdown
- Do NOT add style attributes inside <strong> or <em> -- they inherit from the <p>
- Numbered list -- ALL items in ONE <ol>, copy the <li> style from the reference:
    <ol>
      <li style="font-family: ...; font-size: ...;">First item.</li>
      <li style="font-family: ...; font-size: ...;">Second item.</li>
    </ol>
  WRONG (each item in its own <ol> restarts numbering at 1):
    <ol><li>Item 1.</li></ol><ol><li>Item 2.</li></ol>
- Empty lines: copy a <p> style from the reference and put &nbsp; inside
- Every opened tag (<strong>, <em>) MUST have exactly one matching closing tag

CRITICAL:
1. EXACTLY reproduce the reference document's header block -- same fields, same field order, same labels -- change ONLY the values to match the task
2. Match every field's alignment, font, and style exactly as in the reference -- do NOT alter alignment or weight of any structural element
3. Do NOT add ANY text, lines, labels, or formatting elements not present in the reference document -- no invention whatsoever
4. Generate the COMPLETE document from start to finish -- every structural element in the reference must appear in your output
5. Match every structural block in the reference exactly -- opening, body, closing, and any signature or authentication block
6. THE TOTAL LENGTH OF YOUR OUTPUT MUST MATCH THE REFERENCE DOCUMENT -- do NOT add extra paragraphs or sections
7. Write approximately $TARGET_TOKENS$ tokens
8. Use REAL values matching the task -- dates in the SAME FORMAT as the reference, real names, locations, organizations -- NEVER placeholders
9. Output ONLY HTML -- no explanation, no commentary"""


# Multi-pass: expand {{EXPAND:...}} placeholders in an HTML scaffold.
_WRITE_MULTI_PROMPT = """You are a document writer. You will receive an HTML scaffold containing {{EXPAND: description}} placeholders. Replace every placeholder with rich, detailed HTML content as described. Output the complete HTML with all placeholders replaced — nothing else.

*** HTML SCAFFOLD (expand the placeholders, keep everything else exactly as-is): ***
$SECTION_PLAN$

*** RULES: ***
- OUTPUT MUST START WITH THE FIRST HTML TAG — no preamble, no explanation, no markdown fences.
- Replace EVERY {{EXPAND: ...}} with detailed content matching the description. Match the DENSITY and LENGTH of the template — if template entries are 3-5 sentences, write 3-5 sentences. Do NOT write 15-sentence walls of text unless the template does.
- ALL {{EXPAND}} placeholders must be replaced — NONE may remain in the output.
- Keep ALL tags, inline styles, and non-placeholder text exactly as written — do NOT alter any tag or style attribute.
- Do NOT remove any tags or structural elements from the scaffold.
- ⚠ TARGET LENGTH: approximately $TARGET_TOKENS$ HTML tokens total — write densely until you reach it. DO NOT STOP EARLY.

HTML FORMATTING RULES (MANDATORY):
- When expanding a {{EXPAND}}, produce HTML that uses the EXACT SAME tags, nesting, and style="..." attributes as the surrounding scaffold — copy tag names and style strings character for character from the scaffold
- New <p> tags must use the same style="..." as existing <p> tags in the same section
- New <li> tags must use the same style="..." as existing <li> tags in the scaffold
- New <strong> or <em> must follow the same nesting pattern as in the scaffold — do NOT add style attributes inside them
- Alignment (text-align) goes on the <p> style attribute only
- Allowed tags ONLY: <p>, <h1>-<h6>, <ul>, <ol>, <li>, <strong>, <em>, <u>
- FORBIDDEN: <div>, <section>, <header>, <center>, <br>, <span>, <sup>, <sub>, <b>, <i>, any Markdown
- Do NOT use markdown (no *asterisks*, no _underscores_) — use <strong> and <em> tags instead
- Numbered lists: ALL items in ONE <ol>
- Every opened tag must have exactly one matching closing tag
- OUTPUT IS HTML ONLY — no explanation, no commentary, no markdown fences."""


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _count_template_sections(html: str) -> int:
    """Count distinct dated/titled entries in the template HTML.
    Looks for <strong> or <b> tags containing date-like patterns or
    paragraph-starting bold headings that mark a new section entry.
    Returns count excluding the header block (counted separately as section 0).
    """
    import re
    bold_texts = re.findall(r'<(?:strong|b)[^>]*>([^<]+)</(?:strong|b)>', html, re.IGNORECASE)
    date_pattern = re.compile(
        r'(?:January|February|March|April|May|June|July|August|September|October|November|December'
        r'|\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'
        r'|\d{1,2}/\d{1,2}/\d{2,4})'
        r'.*\d{4}',
        re.IGNORECASE
    )
    header_patterns = re.compile(
        r'ROAD TO CRISIS|ASTRAL SCALE|SCALE \d|^\s*\d{2}\s+\w+\s+\d{4}\s*[-–]\s*\d',
        re.IGNORECASE
    )
    count = 0
    for text in bold_texts:
        text = text.strip()
        if not text or len(text) < 5:
            continue
        if header_patterns.search(text):
            continue
        if date_pattern.search(text) or (len(text) > 10 and '–' in text):
            count += 1
    return max(count, 1)


def _count_tokens(text: str) -> int:
    """Approximate token count for doc-generation budget tracking. Delegates
    to utils.tokenizer (single source of truth, locked to o200k_base)."""
    from utils.tokenizer import count_tokens
    return count_tokens(text, fallback_chars_per_token=4)



def _render_pdf_strip(cache_path: str, key: str, target_height: int = 892) -> dict:
    """Render all PDF pages as one wide horizontal strip at target_height px tall.
    Designed for Gemma3's pan-and-scan viewport (max 892x892 sliding window).
    """
    import base64, io, hashlib
    import fitz
    from PIL import Image

    doc = fitz.open(cache_path)
    total_pages = len(doc)
    page_rect = doc[0].rect
    page_height_pts = page_rect.height
    dpi = (target_height / (page_height_pts / 72.0))
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)

    page_images = []
    for pg_num in range(total_pages):
        pix = doc[pg_num].get_pixmap(matrix=mat)
        img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        page_images.append(img)

    if not page_images:
        return {"error": "No pages rendered"}

    ph = page_images[0].height
    cell_w = target_height  # 892px per cell so pan-and-scan lands on one page
    total_w = cell_w * total_pages
    strip = Image.new("RGB", (total_w, ph), (0, 0, 0))
    for i, img in enumerate(page_images):
        x_offset = i * cell_w + (cell_w - img.width) // 2
        strip.paste(img, (x_offset, 0))

    buf = io.BytesIO()
    strip.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()

    return {
        "base64": b64,
        "total_pages": total_pages,
        "pages_shown": list(range(1, total_pages + 1)),
        "grid": f"strip_{total_w}x{ph}",
    }


def _render_pdf_grid(cache_path: str, start_page: int, key: str) -> dict:
    """Render PDF pages into a grid image using PyMuPDF. Returns dict with base64, total_pages, etc."""
    import base64, io, tempfile, hashlib
    import fitz
    from PIL import Image

    doc = fitz.open(cache_path)
    total_pages = len(doc)
    page_rect = doc[0].rect

    page_width_pts = page_rect.width
    page_height_pts = page_rect.height
    page_aspect = page_width_pts / page_height_pts

    MAX_PIXELS = 1150000
    MIN_EFFECTIVE_DPI = 35
    page_width_in = page_width_pts / 72.0
    page_height_in = page_height_pts / 72.0
    pixels_per_cell = (page_width_in * MIN_EFFECTIVE_DPI) * (page_height_in * MIN_EFFECTIVE_DPI)
    max_pages_by_pixels = int(MAX_PIXELS / pixels_per_cell)

    if page_aspect < 1:
        for cols, rows in [(3, 3), (3, 2), (2, 3), (2, 2), (2, 1), (1, 2), (1, 1)]:
            if cols * rows <= max_pages_by_pixels:
                GRID_COLS, GRID_ROWS = cols, rows
                break
        else:
            GRID_COLS, GRID_ROWS = 1, 1
    else:
        for cols, rows in [(3, 3), (2, 3), (3, 2), (2, 2), (1, 2), (2, 1), (1, 1)]:
            if cols * rows <= max_pages_by_pixels:
                GRID_COLS, GRID_ROWS = cols, rows
                break
        else:
            GRID_COLS, GRID_ROWS = 1, 1

    MAX_PAGES_PER_GRID = GRID_COLS * GRID_ROWS
    RENDER_DPI = 200
    BORDER_PX = 2
    zoom = RENDER_DPI / 72.0
    mat = fitz.Matrix(zoom, zoom)

    first_page = (start_page - 1) if start_page else 0
    first_page = max(0, min(first_page, total_pages - 1))
    last_page = min(first_page + MAX_PAGES_PER_GRID, total_pages)
    pages_in_grid = last_page - first_page
    actual_rows = (pages_in_grid + GRID_COLS - 1) // GRID_COLS

    cache_key = hashlib.md5(f"{key}:{first_page}:{last_page}:{GRID_COLS}x{GRID_ROWS}".encode()).hexdigest()
    grid_cache_path = os.path.join(tempfile.gettempdir(), f"grid_cache_{cache_key}.jpg")

    if os.path.exists(grid_cache_path):
        with open(grid_cache_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode('utf-8')
    else:
        page_images = []
        for pg_num in range(first_page, last_page):
            pix = doc[pg_num].get_pixmap(matrix=mat)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            page_images.append(img)

        if not page_images:
            doc.close()
            return {"error": "No pages rendered"}

        page_w, page_h = page_images[0].size
        grid_w = GRID_COLS * page_w + (GRID_COLS + 1) * BORDER_PX
        grid_h = actual_rows * page_h + (actual_rows + 1) * BORDER_PX
        grid = Image.new('RGB', (grid_w, grid_h), 'black')

        for i, img in enumerate(page_images):
            row_idx = i // GRID_COLS
            col_idx = i % GRID_COLS
            x = BORDER_PX + col_idx * (page_w + BORDER_PX)
            y = BORDER_PX + row_idx * (page_h + BORDER_PX)
            grid.paste(img, (x, y))

        aspect = grid_w / grid_h
        if aspect >= 0.95:
            max_w, max_h = 1092, 1092
        elif aspect >= 0.7:
            max_w, max_h = 951, 1268
        elif aspect >= 0.6:
            max_w, max_h = 896, 1344
        elif aspect >= 0.5:
            max_w, max_h = 819, 1456
        else:
            max_w, max_h = 784, 1568

        scale = min(max_w / grid_w, max_h / grid_h)
        new_w = int(grid_w * scale)
        new_h = int(grid_h * scale)
        grid_resized = grid.resize((new_w, new_h), Image.LANCZOS)
        grid_resized.save(grid_cache_path, format='JPEG', quality=90)

        buffer = io.BytesIO()
        grid_resized.save(buffer, format='JPEG', quality=90)
        b64 = base64.b64encode(buffer.getvalue()).decode('utf-8')

    doc.close()
    return {
        "base64": b64,
        "total_pages": total_pages,
        "pages_shown": list(range(first_page + 1, last_page + 1)),
    }


def _strip_html_to_skeleton(html: str) -> str:
    """Replace all text content in HTML with … placeholders. Preserves every tag and inline style."""
    import re
    return re.sub(r'(?<=>)([^<]+)(?=<)', lambda m: ' … ' if m.group(1).strip() else m.group(1), html)


def _clean_output(text: str) -> str:
    """Strip code fences and trailing meta-commentary from model output."""
    text = re.sub(r'^```(?:html)?\s*\n?', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n?```\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n(?:Document complete|In conclusion|To summarize|In summary)\.?\s*$', '', text, flags=re.IGNORECASE)
    return text.rstrip()


def _pdf_to_docx_html(pdf_path: str) -> tuple:
    """
    Convert a PDF to TinyMCE/DOCX-converter-compatible flow HTML using
    PyMuPDF get_text("dict") for accurate structure and formatting.

    Each PDF block → one or more <p> elements.
    Bold/italic come from span.flags (bit 4 = bold, bit 1 = italic).
    Header label lines (Date:, From:, etc.) are split into separate <p>.
    Numbered list items are collected cross-block and emitted as <ol><li>.

    Returns (html: str, layout: dict) where layout contains page margins in
    twips (for the DOCX converter) and the measured line-height ratio.
    """
    try:
        import fitz
    except ImportError:
        logger.warning("_pdf_to_docx_html: PyMuPDF not available")
        return ""

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        logger.warning(f"_pdf_to_docx_html: cannot open {pdf_path}: {e}")
        return "", {}

    # ------------------------------------------------------------------
    # Measure page layout from the first page
    # ------------------------------------------------------------------
    page0 = doc[0]
    pw = page0.rect.width
    ph = page0.rect.height
    p0_blocks = [b for b in page0.get_text("dict").get("blocks", [])
                 if b.get("type") == 0 and b.get("lines")]

    # ---- font size: modal value across all spans on the page ----
    all_sizes = [s.get("size", 0) for b in p0_blocks
                 for ln in b["lines"] for s in ln["spans"] if s.get("size", 0) > 4]
    avg_font_size = max(set(all_sizes), key=all_sizes.count) if all_sizes else 11.0

    # ---- modal font family (strip Bold/Italic/MT/PS suffixes to get the base name) ----
    import re as _re
    _raw_fonts = [s.get("font", "") for b in p0_blocks
                  for ln in b["lines"] for s in ln["spans"] if s.get("font")]
    def _css_font(raw: str) -> str:
        # e.g. "TimesNewRomanPS-BoldMT" → "Times New Roman"
        #      "Arial-BoldMT" → "Arial"
        #      "CalibriMT" → "Calibri"
        name = _re.sub(r'[-,]?(Bold|Italic|Oblique|MT|PS|Regular|Light|Heavy|Black|Narrow)(.*)', '', raw, flags=_re.IGNORECASE)
        name = _re.sub(r'([a-z])([A-Z])', r'\1 \2', name)  # CamelCase → spaced
        return name.strip() or "Times New Roman"
    _css_fonts = [_css_font(f) for f in _raw_fonts]
    base_font = max(set(_css_fonts), key=_css_fonts.count) if _css_fonts else "Times New Roman"

    # ---- line pitch: average top-to-top distance for consecutive lines within a block ----
    line_pitches: list = []
    for b in p0_blocks:
        lns = b["lines"]
        for k in range(1, len(lns)):
            dy = lns[k]["bbox"][1] - lns[k - 1]["bbox"][1]
            # keep only sane values (between 0.8× and 2× the font size)
            if avg_font_size * 0.8 < dy < avg_font_size * 2.5:
                line_pitches.append(dy)
    avg_pitch = (sum(line_pitches) / len(line_pitches)) if line_pitches else avg_font_size * 1.2
    line_height_ratio = round(avg_pitch / avg_font_size, 3)

    # ---- inter-paragraph extra spacing ----
    # extra = top-of-next-block's-first-line − top-of-prev-block's-last-line − avg_pitch
    # Values smaller than avg_pitch × 0.5 are intra-list micro-gaps (ignored).
    # Values larger than avg_pitch × 2 are structural breaks (e.g. section headers, also ignored).
    inter_extras: list = []
    intra_extras: list = []       # list-item-level micro-gaps
    for k in range(1, len(p0_blocks)):
        if not p0_blocks[k]["lines"] or not p0_blocks[k - 1]["lines"]:
            continue
        nxt = p0_blocks[k]["lines"][0]["bbox"][1]
        prv = p0_blocks[k - 1]["lines"][-1]["bbox"][1]
        extra = nxt - prv - avg_pitch
        if extra <= avg_pitch * 0.5:
            intra_extras.append(max(0.0, extra))
        elif extra < avg_pitch * 2:
            inter_extras.append(extra)
    para_spacing_pt   = round((sum(inter_extras)  / len(inter_extras))  if inter_extras  else avg_pitch * 0.8, 1)
    list_spacing_pt   = round((sum(intra_extras)  / len(intra_extras))  if intra_extras  else 0.0,             1)

    # ---- page margins ----
    all_x0 = [b["bbox"][0] for b in p0_blocks]
    all_x1 = [b["bbox"][2] for b in p0_blocks]
    left_margin_pt  = min(all_x0) if all_x0 else 72.0
    right_margin_pt = pw - max(all_x1) if all_x1 else 72.0

    # Bottom margin: measured from the last page's last text block to the page edge
    last_page   = doc[len(doc) - 1]
    last_ph     = last_page.rect.height
    last_blocks = [b for b in last_page.get_text("dict").get("blocks", [])
                   if b.get("type") == 0 and b.get("lines")]
    bottom_margin_pt = (last_ph - max(b["bbox"][3] for b in last_blocks)) if last_blocks else 72.0

    # Top margin: y-position of the first non-empty text block
    top_margin_pt = 72.0
    for b in p0_blocks:
        txt = "".join(s["text"] for ln in b["lines"] for s in ln["spans"]).strip()
        if txt:
            top_margin_pt = b["bbox"][1]
            break

    PT_TO_TWIPS = 20
    layout = {
        "line_height_ratio": line_height_ratio,
        "para_spacing_pt":   para_spacing_pt,
        "list_spacing_pt":   list_spacing_pt,
        "page_margins": {
            "top":    round(top_margin_pt    * PT_TO_TWIPS),
            "right":  round(right_margin_pt  * PT_TO_TWIPS),
            "bottom": round(bottom_margin_pt * PT_TO_TWIPS),
            "left":   round(left_margin_pt   * PT_TO_TWIPS),
        },
    }
    logger.info(
        f"[DOCGEN] PDF layout: font={avg_font_size}pt lh={line_height_ratio} "
        f"para_gap={para_spacing_pt}pt list_gap={list_spacing_pt}pt "
        f"margins=L{left_margin_pt:.1f}/R{right_margin_pt:.1f}/T{top_margin_pt:.1f}/B{bottom_margin_pt:.1f}pt"
    )

    _HEADER_LABEL = re.compile(
        r'^(Date|From|To|Subject|Reference|Via|Through|For|Classification|Priority'
        r'|Info|DTG|Precedence)\s*:', re.IGNORECASE
    )
    _LIST_PREFIX = re.compile(r'^(\d+)[\.\u200b\s]+\s*')
    _ZW = re.compile(r'[\u200b\u00ad\ufeff\u00a0]')

    def _span_runs(line: dict) -> list:
        """Return list of {text, bold, italic} merging adjacent same-format spans."""
        runs: list = []
        for span in line.get("spans", []):
            text = _ZW.sub('', span["text"])
            if not text:
                continue
            bold   = bool(span.get("flags", 0) & 16)
            italic = bool(span.get("flags", 0) & 2)
            size   = span.get("size", 11.0)
            if runs and runs[-1]["bold"] == bold and runs[-1]["italic"] == italic:
                runs[-1]["text"] += text
            else:
                runs.append({"text": text, "bold": bold, "italic": italic, "size": size})
        return runs

    def _runs_to_html(runs: list) -> str:
        parts = []
        for r in runs:
            t = r["text"]
            if r["bold"] and r["italic"]:
                parts.append(f"<strong><em>{t}</em></strong>")
            elif r["bold"]:
                parts.append(f"<strong>{t}</strong>")
            elif r["italic"]:
                parts.append(f"<em>{t}</em>")
            else:
                parts.append(t)
        return "".join(parts).strip()

    lh_ratio = line_height_ratio           # unitless ratio (e.g. 1.322) — convert.js multiplies by 240
    mgb_blk   = layout["para_spacing_pt"] # between separate PDF blocks
    mgb_intra = 0                          # between lines split from the same block

    def _para_html(runs: list, font_size: float, text_align: str,
                   intra_block: bool = False) -> str | None:
        inner = _runs_to_html(runs)
        if not inner:
            return None
        fs  = f"{font_size:.0f}pt"
        style = f"font-family: {base_font}; font-size: {fs}"
        if text_align != "left":
            style += f"; text-align: {text_align}"
        return f'<p style="{style}">{inner}</p>'

    def _li_html(runs: list, font_size: float) -> str | None:
        inner = _runs_to_html(runs)
        if not inner:
            return None
        fs = f"{font_size:.0f}pt"
        return f'  <li style="font-family: {base_font}; font-size: {fs}">{inner}</li>'

    def _join_runs(base: list, extra: list) -> list:
        """Append extra runs to base, adding a space joiner when needed."""
        if base and extra and not base[-1]["text"].endswith(" "):
            base[-1]["text"] += " "
        base.extend(extra)
        return base

    def _strip_list_prefix(runs: list, prefix_end: int) -> list:
        """Remove the first prefix_end characters (the 'N. ' prefix) from runs."""
        out: list = []
        consumed = 0
        for r in runs:
            t = r["text"]
            if consumed + len(t) <= prefix_end:
                consumed += len(t)
                continue
            start = max(0, prefix_end - consumed)
            stripped = t[start:]
            if stripped:
                out.append({**r, "text": stripped})
            consumed += len(t)
        return out

    # ---- Detect repeated header/footer blocks across pages ----
    # A block is a header/footer if its top-Y (rounded to 2pt) appears on 2+ pages
    # at roughly the same position (top 15% or bottom 15% of page height).
    _PAGE_H = doc[0].rect.height
    _y_page_counts: dict = {}   # rounded_y -> set of page numbers
    for _pn in range(len(doc)):
        _pg = doc[_pn]
        _ph = _pg.rect.height
        for _b in _pg.get_text("dict").get("blocks", []):
            if _b.get("type") != 0 or not _b.get("lines"):
                continue
            _by0 = _b["bbox"][1]
            _by1 = _b["bbox"][3]
            # Only consider blocks in the top or bottom 15% of the page
            if _by0 < _ph * 0.15 or _by1 > _ph * 0.85:
                _ry = round(_by0 / 2) * 2          # bucket to 2pt
                _y_page_counts.setdefault(_ry, set()).add(_pn)
    # Y positions that appear on 2+ pages → header/footer zone
    _hf_y_zones = {_ry for _ry, _pages in _y_page_counts.items() if len(_pages) >= 2}

    def _is_header_footer(block) -> bool:
        """True if this block's top-Y sits in a detected header/footer zone."""
        _by0 = block["bbox"][1]
        _ph  = doc[0].rect.height
        if _by0 >= _ph * 0.15 and block["bbox"][3] <= _ph * 0.85:
            return False   # mid-page, never a header/footer
        _ry = round(_by0 / 2) * 2
        return _ry in _hf_y_zones

    # output_items: list of ('p', html) or ('li', html)
    output_items: list = []

    # Cross-block list state
    in_list       = False
    li_runs: list = []
    li_font_size  = 11.0

    def _flush_li() -> None:
        nonlocal in_list, li_runs
        if li_runs:
            h = _li_html(li_runs, li_font_size)
            if h:
                output_items.append(("li", h))
        li_runs = []
        in_list = False

    for page_num in range(len(doc)):
        page = doc[page_num]
        page_width = page.rect.width

        page_dict = page.get_text("dict")
        text_blocks = [b for b in page_dict.get("blocks", [])
                       if b.get("type") == 0 and b.get("lines")]

        # Determine the normal left margin for this page (the most common block x0)
        if text_blocks:
            left_vals = [round(b["bbox"][0]) for b in text_blocks]
            normal_left = min(left_vals)
        else:
            normal_left = 43.0  # fallback

        for block in text_blocks:
            if _is_header_footer(block):
                continue   # skip page headers/footers

            block_lines = block.get("lines", [])

            block_x0 = block["bbox"][0]

            font_size = 11.0
            if block_lines[0].get("spans"):
                font_size = block_lines[0]["spans"][0].get("size", 11.0)

            text_align = "center" if block_x0 > page_width * 0.25 else "left"

            # Collect plain-text per line (zero-width stripped)
            line_plains = [
                _ZW.sub('', "".join(s["text"] for s in ln.get("spans", []))).strip()
                for ln in block_lines
            ]

            has_labels  = any(_HEADER_LABEL.match(t) for t in line_plains)
            # List block: indented clearly beyond the normal left margin but not centered
            is_list_blk = (block_x0 > normal_left + 10 and block_x0 < page_width * 0.25)

            # ------------------------------------------------------------------
            # Header-label block: split at each "Label: ..." line
            # ------------------------------------------------------------------
            if has_labels:
                _flush_li()
                cur_runs: list = []
                # Collect completed label paragraphs; emit all as intra-block
                # except the very last one (which gets the full inter-block margin).
                pending_label_paras: list = []

                for ln, plain in zip(block_lines, line_plains):
                    spans = _span_runs(ln)
                    if _HEADER_LABEL.match(plain):
                        if cur_runs:
                            pending_label_paras.append((cur_runs, font_size, text_align))
                        # Force the label keyword (e.g. "Date:", "From:") bold
                        # regardless of whether the PDF stored it as bold.
                        m_lbl = _HEADER_LABEL.match(plain)
                        if m_lbl and spans:
                            label_end = m_lbl.end()
                            consumed = 0
                            for r in spans:
                                if consumed < label_end:
                                    r["bold"] = True
                                consumed += len(r["text"])
                        cur_runs = spans
                    else:
                        cur_runs = _join_runs(cur_runs, spans)

                if cur_runs:
                    pending_label_paras.append((cur_runs, font_size, text_align))

                for idx, (runs, fs, ta) in enumerate(pending_label_paras):
                    is_last = (idx == len(pending_label_paras) - 1)
                    h = _para_html(runs, fs, ta, intra_block=not is_last)
                    if h:
                        output_items.append(("p", h))

            # ------------------------------------------------------------------
            # List block: detect "N." starts and accumulate cross-block
            # ------------------------------------------------------------------
            elif is_list_blk:
                li_font_size = font_size
                for ln, plain in zip(block_lines, line_plains):
                    if not plain:
                        continue
                    spans = _span_runs(ln)
                    m = _LIST_PREFIX.match(plain)
                    if m:
                        _flush_li()
                        in_list = True
                        li_font_size = font_size
                        li_runs = _strip_list_prefix(spans, m.end())
                    elif plain.strip() and in_list:
                        li_runs = _join_runs(li_runs, spans)
                    # blank / spacer lines inside list block → skip

            # ------------------------------------------------------------------
            # Regular block: flush any open list item, then one <p> for the block.
            # Split at bold↔non-bold line transitions (signature blocks, name/title).
            # ------------------------------------------------------------------
            else:
                _flush_li()
                chunk_runs: list = []
                prev_all_bold: bool | None = None
                pending_chunks: list = []

                for ln, plain in zip(block_lines, line_plains):
                    if not plain:
                        continue
                    spans = _span_runs(ln)
                    line_all_bold = bool(spans) and all(r["bold"] for r in spans)
                    if prev_all_bold is not None and line_all_bold != prev_all_bold and chunk_runs:
                        pending_chunks.append((list(chunk_runs), font_size, text_align))
                        chunk_runs = []
                    chunk_runs = _join_runs(chunk_runs, spans)
                    prev_all_bold = line_all_bold

                if chunk_runs:
                    pending_chunks.append((list(chunk_runs), font_size, text_align))

                for idx, (runs, fs, ta) in enumerate(pending_chunks):
                    is_last = (idx == len(pending_chunks) - 1)
                    h = _para_html(runs, fs, ta, intra_block=not is_last)
                    if h:
                        output_items.append(("p", h))

    _flush_li()
    doc.close()

    # Wrap consecutive ('li', ...) items in <ol>
    result: list = []
    i = 0
    while i < len(output_items):
        kind, html = output_items[i]
        if kind == "li":
            result.append("<ol>")
            while i < len(output_items) and output_items[i][0] == "li":
                result.append(output_items[i][1])
                i += 1
            result.append("</ol>")
        else:
            result.append(html)
            i += 1

    # Set margin-bottom to 0pt on the very last content element so LibreOffice
    # doesn't count trailing whitespace against the page height.
    # (Removing the attribute entirely causes the converter to use its default 10pt.)
    for j in range(len(result) - 1, -1, -1):
        if result[j].startswith('<p ') or result[j].startswith('  <li '):
            if 'margin-bottom:' in result[j]:
                result[j] = re.sub(r'margin-bottom:\s*[\d.]+pt', 'margin-bottom: 0pt', result[j])
            else:
                # attribute absent → insert it so converter doesn't use its default
                result[j] = result[j].replace(' style="', ' style="margin-bottom: 0pt; ', 1)
            break

    return "\n".join(result), layout


def _validate_headings(headings: list, template_text: str) -> list:
    """Keep only headings that actually appear as standalone lines in the template text."""
    template_lines = {l.strip().rstrip(':').lower() for l in template_text.split('\n') if l.strip()}
    valid = []
    for h in headings:
        h_clean = h.strip().rstrip(':').lower()
        if h_clean and h_clean in template_lines:
            valid.append(h.strip())
    return valid


def _split_html_by_headings(html: str, headings: list) -> tuple:
    """
    Split HTML at heading boundaries. Returns (chunks, p_counts, li_counts).
    Finds each heading's text in the HTML, then slices from the opening tag
    of that heading to the opening tag of the next heading.
    """
    import re as _re
    if not html or not headings:
        return [""] * len(headings), [0] * len(headings), [0] * len(headings)

    positions = []
    for j, h in enumerate(headings):
        pattern = _re.escape(h.strip())
        m = _re.search(pattern, html, _re.IGNORECASE)
        if m:
            # Walk back to the start of the enclosing HTML tag
            tag_start = html.rfind('<', 0, m.start())
            positions.append((j, tag_start if tag_start >= 0 else m.start()))

    positions.sort(key=lambda x: x[1])

    chunks_by_idx = {}
    for k, (h_idx, char_pos) in enumerate(positions):
        next_pos = positions[k + 1][1] if k + 1 < len(positions) else len(html)
        chunks_by_idx[h_idx] = html[char_pos:next_pos].strip()

    chunks   = [chunks_by_idx.get(i, "") for i in range(len(headings))]
    p_counts  = [len(_re.findall(r'<p[ >]', c)) for c in chunks]
    li_counts = [len(_re.findall(r'<li[ >]', c)) for c in chunks]
    return chunks, p_counts, li_counts


def _split_template_by_headings(template_text: str, headings: list) -> list:
    """
    Split template text into chunks at heading boundaries.
    Returns a list of strings (one per heading), each containing the heading line
    and all text up to the next heading.
    """
    lines = template_text.split('\n')
    heading_lower = [h.strip().rstrip(':').lower() for h in headings]

    # Find the line index for each heading (first occurrence)
    heading_line_indices = []
    found = [False] * len(headings)
    for i, line in enumerate(lines):
        stripped = line.strip().rstrip(':').lower()
        for j, hl in enumerate(heading_lower):
            if not found[j] and stripped == hl:
                heading_line_indices.append((j, i))
                found[j] = True
                break

    # Sort by line position so we can slice correctly
    heading_line_indices.sort(key=lambda x: x[1])

    chunks_by_heading_idx = {}
    for k, (heading_idx, line_pos) in enumerate(heading_line_indices):
        next_line_pos = heading_line_indices[k + 1][1] if k + 1 < len(heading_line_indices) else len(lines)
        chunk = '\n'.join(lines[line_pos:next_line_pos]).strip()
        chunks_by_heading_idx[heading_idx] = chunk

    # Return chunks in the original heading order, falling back to "" if a heading wasn't found
    return [chunks_by_heading_idx.get(i, "") for i in range(len(headings))]


def _looks_complete(text: str, target_tokens: int, actual_tokens: int) -> bool:
    """
    Heuristic: does the generated HTML look like a complete document?
    Checks for closing signature patterns and token adequacy.
    """
    if actual_tokens >= target_tokens * 0.80:
        return True
    closing_patterns = [
        r'respectfully', r'sincerely', r'regards,', r'/s/', r'signature',
        r'president of', r'prime minister', r'secretary', r'director',
    ]
    text_lower = text.lower()
    for pat in closing_patterns:
        if re.search(pat, text_lower):
            return True
    return False


# =============================================================================
# vLLM CHAT API
# =============================================================================
# vLLM serves a single model (Gemma-4-26B-A4B-NVFP4) via the OpenAI-compatible
# /v1/chat/completions endpoint. The wrappers below preserve the previous
# (text, token_count) tuple shape that all call sites in this file rely on,
# and translate Ollama-style base64 `images` lists into OpenAI `image_url`
# content parts attached to the last user message. The `model` parameter is
# accepted for backwards compatibility with call sites but ignored — vLLM
# routes to whatever model was launched at serve time.


def _attach_images_to_last_user(messages: list, images: list) -> list:
    """Convert the last user message's `content` from a plain string into the
    OpenAI multimodal content-parts list, appending each base64 image as an
    `image_url` data URI. Returns a new messages list (does not mutate input).
    """
    if not images:
        return messages
    new_messages = [dict(m) for m in messages]
    for msg in reversed(new_messages):
        if msg.get("role") == "user":
            text = msg.get("content", "")
            parts = [{"type": "text", "text": text}] if isinstance(text, str) else list(text)
            for b64 in images:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            msg["content"] = parts
            break
    return new_messages


def _vllm_chat(messages: list, model: str = None, images: list = None, timeout: int = 1800, num_predict: int = -1, temperature: float = None) -> tuple:
    """Drop-in replacement for the old `_ollama_chat`. Returns (text, token_count)."""
    msgs = _attach_images_to_last_user(messages, images) if images else messages
    max_tokens = num_predict if (num_predict and num_predict > 0) else 8000
    kwargs = {"timeout": timeout, "max_tokens": max_tokens}
    if temperature is not None:
        kwargs["temperature"] = temperature
    resp = llm.invoke_vllm_chat(msgs, **kwargs)
    text = (resp["choices"][0]["message"].get("content") or "").strip()
    tokens = (resp.get("usage") or {}).get("completion_tokens", 0)
    return text, tokens


def _vllm_call(prompt: str, model: str = None, system: str = None, images: list = None, timeout: int = 1800, num_predict: int = -1, temperature: float = None) -> tuple:
    """Drop-in replacement for the old `_ollama_call`. Builds a chat message list
    from (system, prompt) and delegates to `_vllm_chat`. Returns (text, tokens).
    """
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return _vllm_chat(messages=messages, model=model, images=images,
                      timeout=timeout, num_predict=num_predict, temperature=temperature)


# =============================================================================
# MAIN ORCHESTRATOR
# =============================================================================

def generate_document(
    task: str,
    steps: list,
    template_path: str = None,
    additional_context: str = None,
    model: str = None,
    on_event: Callable = None,
) -> str:
    """
    Generate a document using the structure-aware pipeline.

    When steps=[] (the normal chat path), performs LLM-driven structure analysis
    to choose between single-pass (continuous docs) and multi-pass (sectioned docs).

    When steps are provided (manual/test), uses multi-pass directly.

    Args:
        task: The user's generation request
        steps: Explicit section steps (skips structure analysis when non-empty)
        template_path: Path to template PDF for vision rendering
        additional_context: Template body HTML/text (used as format reference)
        model: Unused — vLLM serves a single configured model (kept for arg compat)
        on_event: Callback(event_type, content) for progress updates

    Returns:
        The generated document as DOCX-compatible HTML string.
    """
    def log(msg):
        logger.info(f"[DOCGEN] {msg}")

    def notify(msg):
        if on_event:
            try:
                on_event("status", msg)
            except Exception:
                pass

    log(f"Starting DocGen. model={model or config.Config.Q_AND_A_MODEL()}")
    notify("Preparing document generation...")

    from datetime import datetime, timezone
    today = datetime.now().strftime("%m/%d/%Y")
    utc_now = datetime.now(timezone.utc).strftime("%m/%d/%Y %H:%M UTC")

    # =========================================================================
    # 1. Template: vision rendering + plain text extraction
    # =========================================================================
    template_images = []
    plain_text = ""           # plain text from PDF (used for structure analysis)
    format_ref = additional_context or ""   # HTML/text reference (used for write prompts)


    notify("Analyzing template...")

    if template_path and os.path.exists(template_path):
        ext = Path(template_path).suffix.lower()

        if ext == '.pdf':
            try:
                strip_result = _render_pdf_strip(template_path, template_path)
                if "base64" in strip_result:
                    template_images = [strip_result["base64"]]
                    log(f"Vision: strip render {strip_result.get('grid','?')}, {strip_result.get('total_pages','?')} pages")
                else:
                    raise ValueError("strip render returned no image")
            except Exception as e:
                log(f"Strip render failed ({e}), falling back to grid")
                try:
                    grid_result = _render_pdf_grid(template_path, None, template_path)
                    if "base64" in grid_result:
                        template_images = [grid_result["base64"]]
                        log(f"Vision: grid render, {grid_result.get('total_pages', '?')} pages")
                except Exception as e2:
                    log(f"Vision mode failed: {e2}")

            try:
                import fitz
                doc = fitz.open(template_path)
                plain_text = "\n".join(page.get_text() for page in doc)
                doc.close()
                log(f"Extracted {_count_tokens(plain_text)} tokens of plain text from template")
            except Exception as e:
                log(f"PDF text extraction failed: {e}")

            # Convert PDF → structured HTML only if no pre-computed HTML was provided.
            # Normally this is pre-computed at upload time by generate_template() in product.py.
            if not format_ref:
              try:
                    html_body, pdf_layout = _pdf_to_docx_html(template_path)
                    if html_body:
                        import re as _re
                        _p_count  = len(_re.findall(r'<p ',  html_body))
                        _li_count = len(_re.findall(r'<li ', html_body))
                        format_ref = html_body
                        # Override token target with the actual HTML token count so the
                        # LLM writes roughly the same volume as the reference document.
                        _html_tokens = _count_tokens(html_body)
                        # Stored on a dict so generate_document can read it after the block
                        _pdf_element_counts = {"p": _p_count, "li": _li_count,
                                               "tokens": _html_tokens}
                        log(f"PDF→HTML: {len(html_body)} chars, "
                            f"p={_p_count} li={_li_count} tokens={_html_tokens}")
              except Exception as e:
                log(f"PDF→HTML conversion failed, will fall back to plain text: {e}")
                _pdf_element_counts = {}

    elif template_path:
        log(f"Template file not found: {template_path}")

    # Fallback: use format_ref as plain_text if PDF extraction failed
    if not plain_text and format_ref:
        # Strip HTML tags to get something closer to plain text for analysis
        plain_text = re.sub(r'<[^>]+>', ' ', format_ref)
        plain_text = re.sub(r'\s+', ' ', plain_text).strip()

    # Build the text reference block for write prompts
    _elem = locals().get("_pdf_element_counts", {})
    if format_ref:
        _elem_note = ""
        if _elem.get("p"):
            _elem_note = (
                f"\n\nREFERENCE ELEMENT COUNT (HARD LIMIT): "
                f"The reference contains exactly {_elem['p']} <p> elements"
                + (f" and {_elem['li']} <li> elements" if _elem.get("li") else "")
                + ". Your output MUST contain the SAME number of elements -- no more, no less."
            )
        template_section = (
            f"REFERENCE DOCUMENT (replicate this exact style and structure):\n\n"
            f"{format_ref}{_elem_note}"
        )
    elif plain_text:
        template_section = f"REFERENCE DOCUMENT (replicate this exact style and structure):\n\n{plain_text}"
    else:
        template_section = ""

    # Token count of plain text — same unit as LLM's section token estimates
    _pt = plain_text or re.sub(r'<[^>]+>', ' ', format_ref or "")
    _pt = re.sub(r'\s+', ' ', _pt).strip()
    total_template_tokens = _count_tokens(_pt) if _pt else 0
    # Compute actual HTML-to-plaintext token ratio from the template itself
    _html_token_count = _count_tokens(format_ref) if format_ref else 0
    html_overhead_ratio = (_html_token_count / total_template_tokens) if total_template_tokens > 0 else 2.5
    html_overhead_ratio = max(1.5, min(html_overhead_ratio, 5.0))  # clamp to sane range
    log(f"Template tokens: {total_template_tokens} plain / {_html_token_count} HTML (ratio: {html_overhead_ratio:.2f}x)")

    # =========================================================================
    # 2. Determine generation mode
    # =========================================================================
    use_single_pass = False
    plan_tokens = 0

    if steps:
        # Caller provided explicit steps -- go multi-pass directly
        plan_text = "\n".join(f"Section {i+1}: {s}" for i, s in enumerate(steps))
        log(f"Using {len(steps)} provided steps, multi-pass mode")

    else:
        notify("Analyzing template structure... (0%)")

        # Step 1: vision call — count entries and analyze structure
        import re as _re
        _page_count = 1
        try:
            import fitz as _fitz_count
            _doc_count = _fitz_count.open(template_path) if template_path else None
            if _doc_count:
                _page_count = len(_doc_count)
                _doc_count.close()
        except Exception:
            pass

        structure_analysis = ""
        try:
            # Render a separate un-padded strip with thin black borders for counting —
            # the padded strip confuses the counter but the un-padded one got accurate results.
            _counting_images = template_images
            if template_path and Path(template_path).suffix.lower() == '.pdf':
                try:
                    import fitz as _fitz_strip
                    from PIL import Image as _PILImage
                    import io as _io, base64 as _b64
                    _doc_s = _fitz_strip.open(template_path)
                    _target_h = 892
                    _dpi_s = _target_h / (_doc_s[0].rect.height / 72.0)
                    _mat_s = _fitz_strip.Matrix(_dpi_s / 72.0, _dpi_s / 72.0)
                    _border = 3
                    _pimgs = []
                    for _pg in range(len(_doc_s)):
                        _px = _doc_s[_pg].get_pixmap(matrix=_mat_s)
                        _pimgs.append(_PILImage.frombytes("RGB", [_px.width, _px.height], _px.samples))
                    _ph = _pimgs[0].height
                    _tw = sum(im.width for im in _pimgs) + _border * (len(_pimgs) + 1)
                    _strip = _PILImage.new("RGB", (_tw, _ph), (0, 0, 0))
                    _x = _border
                    for im in _pimgs:
                        _strip.paste(im, (_x, 0))
                        _x += im.width + _border
                    _buf = _io.BytesIO()
                    _strip.save(_buf, format="JPEG", quality=85)
                    _counting_images = [_b64.b64encode(_buf.getvalue()).decode()]
                    log(f"Counting strip: {_tw}x{_ph}")
                except Exception as _e_strip:
                    log(f"Counting strip render failed: {_e_strip}, using padded strip")

            page_lines = "\n".join(f"PAGE {i+1}: [count]" for i in range(_page_count))
            analysis_prompt = (
                _STRUCTURE_ANALYSIS_PROMPT
                .replace("$PAGE_COUNT$", str(_page_count))
                .replace("$PAGE_LINES$", page_lines)
            )
            _vision_model = config.Config.DOCGEN_SCAFFOLD_MODEL()
            log(f"Vision model: {_vision_model}")
            analysis_resp, a_tokens = _vllm_call(
                prompt=analysis_prompt,
                model=_vision_model,
                images=_counting_images,
                temperature=0,
            )
            plan_tokens += a_tokens
            raw_analysis = analysis_resp.strip()
            log(f"Structure analysis raw: {raw_analysis}")

            # Extract TOTAL and rewrite as ENTRY_COUNT for downstream consumption
            total_match = _re.search(r'TOTAL:\s*(\d+)', raw_analysis)
            entry_count = int(total_match.group(1)) if total_match else 10

            # Strip everything before first PAGE line and after WRITING_REGISTER line
            clean = _re.sub(r'^.*?(?=PAGE \d+:)', '', raw_analysis, flags=_re.DOTALL)
            # Replace per-page lines + TOTAL with single ENTRY_COUNT
            clean = _re.sub(
                r'(?:PAGE \d+:.*\n?)+TOTAL:\s*\d+',
                f'ENTRY_COUNT: {entry_count}',
                clean,
            )
            # Strip any trailing chatter after WRITING_REGISTER line
            wr_match = _re.search(r'(WRITING_REGISTER:\s*.+)', clean)
            if wr_match:
                op_match = _re.search(r'(ORGANIZING_PRINCIPLE:\s*.+)', clean)
                structure_analysis = f"ENTRY_COUNT: {entry_count}"
                if op_match:
                    structure_analysis += f"\n{op_match.group(1).strip()}"
                structure_analysis += f"\n{wr_match.group(1).strip()}"
            else:
                structure_analysis = f"ENTRY_COUNT: {entry_count}\nORGANIZING_PRINCIPLE: unknown\nWRITING_REGISTER: formal"
            log(f"Structure analysis: {structure_analysis}")
        except Exception as e:
            log(f"Structure analysis call failed: {e}")
            entry_count = 10
            structure_analysis = "ENTRY_COUNT: 10\nORGANIZING_PRINCIPLE: unknown\nWRITING_REGISTER: formal"

        # Step 2: text-only call — generate HTML scaffolds using structure info
        notify("Drafting outline... (5%)")
        llm_section_p_counts = []
        section_plans_raw = []

        headings_prompt = _STRUCTURE_HEADINGS_PROMPT.replace(
            "$TASK$", task
        ).replace(
            "$SKELETON_HTML$", format_ref or "(no HTML available)"
        ).replace(
            "$STRUCTURE_ANALYSIS$", structure_analysis
        ).replace(
            "$ENTRY_COUNT$", str(entry_count)
        ).replace(
            "$UTC_NOW$", utc_now
        )
        try:
            _scaffold_model = config.Config.DOCGEN_SCAFFOLD_MODEL()
            log(f"Scaffold model: {_scaffold_model}")
            headings_resp, h_tokens = _vllm_call(
                prompt=headings_prompt,
                model=_scaffold_model,
            )
            plan_tokens += h_tokens
            log(f"Structure headings response: {headings_resp.strip()[:1200]}")

            import re as _re2
            raw_headings = []
            section_plans_raw = []
            blocks = _re2.split(r'-{3,}SECTION-{3,}', headings_resp, flags=_re2.IGNORECASE)
            for block in blocks:
                block = block.strip()
                if not block:
                    continue
                lines = block.splitlines()
                meta_line = lines[0].strip()
                if '|' in meta_line:
                    meta_parts = [p.strip() for p in meta_line.split('|', 1)]
                    heading = meta_parts[0]
                    if not heading or heading.lower() in ("section heading", "heading", "section heading | token count", "heading | token count"):
                        continue
                    try:
                        p_count = int(_re2.sub(r'[^\d]', '', meta_parts[1])) if len(meta_parts) > 1 else 0
                    except (ValueError, TypeError):
                        p_count = 0
                    spec = "\n".join(lines[1:]).strip()
                else:
                    # No pipe — model wrote content directly after ---SECTION---
                    heading = meta_line[:80].replace('<', '').replace('>', '').strip() or f"Section {len(raw_headings)+1}"
                    p_count = 0
                    spec = block  # entire block is content
                spec = _re2.sub(r'^```[a-zA-Z]*\n?', '', spec, flags=_re2.MULTILINE)
                spec = _re2.sub(r'\n?```$', '', spec, flags=_re2.MULTILINE)
                spec = spec.strip()
                raw_headings.append(heading)
                llm_section_p_counts.append(p_count)
                section_plans_raw.append(spec)

            structure_resp = "SECTIONS:\n" + "\n".join(raw_headings) if raw_headings else "SINGLE"
        except Exception as e:
            log(f"Structure headings call failed: {e}. Defaulting to SINGLE.")
            structure_resp = "SINGLE"
            section_plans_raw = []
            raw_headings = []

        if structure_resp.upper().startswith("SINGLE"):
            use_single_pass = False
            steps = ["Full Document"]
            section_plans_raw = [task]
            llm_section_p_counts = [total_template_tokens]
            plan_text = f"Full document: {task}"
            log("Structure: SINGLE -- routing through multi-pass with 1 section")

        elif "SECTIONS:" in structure_resp.upper() or structure_resp.upper().startswith("SECTIONS"):
            lines = structure_resp.split("\n")
            raw_headings = []
            in_sections = False
            for line in lines:
                if re.match(r'SECTIONS\s*:', line, re.IGNORECASE):
                    in_sections = True
                    continue
                if in_sections and line.strip():
                    raw_headings.append(line.strip())

            if raw_headings:
                steps = raw_headings
                plan_text = "\n".join(f"Section {i+1}: {s}" for i, s in enumerate(steps))
                log(f"Multi-pass with {len(steps)} sections: {steps}")
                log(f"LLM section p_counts: {llm_section_p_counts}")
            else:
                log("SECTIONS response but no headings parsed -- routing through multi-pass with 1 section")
                use_single_pass = False
                steps = ["Full Document"]
                section_plans_raw = [task]
                llm_section_p_counts = [total_template_tokens]
                plan_text = f"Full document: {task}"
        else:
            log(f"Unrecognized structure response, routing through multi-pass with 1 section")
            use_single_pass = False
            steps = ["Full Document"]
            section_plans_raw = [task]
            llm_section_p_counts = [total_template_tokens]
            plan_text = f"Full document: {task}"

    if not steps:
        log("ERROR: No steps resolved. Aborting.")
        return ""

    # =========================================================================
    # 3. Template chunking for multi-pass
    # =========================================================================
    section_template_chunks = []
    section_token_targets = []
    llm_section_p_counts = locals().get("llm_section_p_counts", [])
    section_plans = locals().get("section_plans_raw", [])

    if not use_single_pass:
        n = len(steps)

        # Target per section = HTML tokens already in the scaffold
        #                    + LLM's plain-text expansion estimate converted to HTML tokens.
        # LLM estimates are plain-text word counts; multiply by html_overhead_ratio to get HTML tokens.
        scaffold_html_tokens = [_count_tokens(spec) for spec in section_plans]
        if llm_section_p_counts and len(llm_section_p_counts) == n and any(llm_section_p_counts):
            # Scale LLM expansion estimates so total output matches template HTML size:
            # multiplier = (template_html_tokens - total_scaffold_html) / total_llm_estimates
            total_scaffold = sum(scaffold_html_tokens)
            total_estimates = sum(llm_section_p_counts)
            remaining = _html_token_count - total_scaffold
            expansion_multiplier = (remaining / total_estimates) if total_estimates > 0 else html_overhead_ratio
            expansion_multiplier = max(1.0, expansion_multiplier)
            section_token_targets = [
                max(100, scaffold_html_tokens[i] + int(llm_section_p_counts[i] * expansion_multiplier))
                for i in range(n)
            ]
            log(f"Token targets: scaffold_html={scaffold_html_tokens} + text_expansion={llm_section_p_counts} x {expansion_multiplier:.2f} = {section_token_targets}")
        else:
            # Flat split fallback
            per = max(100, _html_token_count // max(n, 1))
            section_token_targets = [per] * n
            log(f"Token targets: flat split {per} each")

        # Pass the full HTML template as context for every section (no heading split needed)
        section_template_chunks = [format_ref or plain_text or ""] * n

        log(f"Section token targets: {section_token_targets} (total: {sum(section_token_targets)})")

    # =========================================================================
    # 4a. SINGLE-PASS generation
    # =========================================================================
    total_tokens = plan_tokens
    accumulated_text = ""

    if use_single_pass:
        target_tokens = max(200, total_template_tokens)
        notify("Generating document...")
        log(f"Single-pass. Target: {target_tokens} tokens")

        write_prompt = (_WRITE_SINGLE_PROMPT
            .replace("$TODAY$", today)
            .replace("$INST$", task)
            .replace("$TEMPLATE_SECTION$", template_section)
            .replace("$TARGET_TOKENS$", str(target_tokens))
        )

        try:
            # Initial generation
            _writer_model = config.Config.DOCGEN_WRITER_MODEL()
            log(f"Writer model (single-pass): {_writer_model}")
            messages = [{"role": "user", "content": write_prompt}]
            output, out_tokens = _vllm_chat(messages, model=_writer_model, images=template_images)
            output = _clean_output(output)
            total_tokens += out_tokens
            log(f"Single-pass initial: {out_tokens} tokens")

            # Continuation if output looks incomplete (up to 2 rounds)
            for cont_round in range(2):
                if _looks_complete(output, target_tokens, out_tokens):
                    break
                log(f"Output looks incomplete ({out_tokens}/{target_tokens} tokens). Issuing continuation {cont_round+1}.")
                notify("Completing document...")
                messages.append({"role": "assistant", "content": output})
                messages.append({
                    "role": "user",
                    "content": (
                        "Continue the document from exactly where you left off. "
                        "Output ONLY the continuation in the same HTML format. "
                        "Complete the document through its closing and signature. "
                        "Output ONLY HTML -- no explanation."
                    )
                })
                continuation, cont_tokens = _vllm_chat(messages, model=_writer_model)
                continuation = _clean_output(continuation)
                output = output + "\n" + continuation
                out_tokens += cont_tokens
                total_tokens += cont_tokens
                log(f"Continuation {cont_round+1}: {cont_tokens} tokens (running total: {out_tokens})")

            accumulated_text = output

        except Exception as e:
            log(f"Single-pass generation failed: {e}")
            return ""

        if on_event:
            try:
                on_event("section_complete", accumulated_text)
            except Exception:
                pass

    # =========================================================================
    # 4b. MULTI-PASS generation — sliding window batches up to 20k tokens each
    # =========================================================================
    else:
        BATCH_TOKEN_LIMIT = 4096
        n_sections = len(steps)

        # Balanced greedy batching: determine how many batches are needed, then
        # greedily fill each batch targeting total/n_batches tokens so sections
        # are distributed by weight, not by count
        import math
        total_target_tokens = sum(section_token_targets)
        n_batches_needed = max(1, math.ceil(total_target_tokens / BATCH_TOKEN_LIMIT))
        target_per_batch = total_target_tokens / n_batches_needed
        batches = []
        current_batch = []
        current_total = 0
        batches_started = 1
        for i in range(n_sections):
            tok = section_token_targets[i]
            # Start a new batch if we've hit the per-batch target AND there are
            # still more batches available to use
            if current_total >= target_per_batch and current_batch and batches_started < n_batches_needed:
                batches.append(current_batch)
                current_batch = [i]
                current_total = tok
                batches_started += 1
            else:
                current_batch.append(i)
                current_total += tok
        if current_batch:
            batches.append(current_batch)

        _writer_model = config.Config.DOCGEN_WRITER_MODEL()
        log(f"Writer model: {_writer_model}")
        log(f"Batched {n_sections} sections into {len(batches)} LLM calls: {[len(b) for b in batches]} sections each")

        n_batches = len(batches)
        for b_idx, batch in enumerate(batches):
            t0 = time.time()
            pct = 10 + int((b_idx / n_batches) * 90)
            first_i = batch[0]
            last_i  = batch[-1]
            notify(f"Writing sections {first_i+1}–{last_i+1} of {n_sections}... ({pct}%)")

            batch_token_total = sum(section_token_targets[i] for i in batch)

            # Concatenate HTML scaffolds for all sections in this batch
            if len(batch) == 1:
                i = batch[0]
                plan_str = section_plans[i] if i < len(section_plans) else f"<p>Write the '{steps[i]}' section.</p>"
            else:
                plan_str = "\n".join(
                    section_plans[i] if i < len(section_plans) else f"<p>Write the '{steps[i]}' section.</p>"
                    for i in batch
                )

            write_prompt = (_WRITE_MULTI_PROMPT
                .replace("$SECTION_PLAN$", plan_str)
                .replace("$TARGET_TOKENS$", str(batch_token_total)))

            try:
                _writer_model = config.Config.DOCGEN_WRITER_MODEL()
                paragraph, para_tokens = _vllm_call(
                    prompt=write_prompt,
                    model=_writer_model,
                    num_predict=batch_token_total,
                )
            except Exception as e:
                log(f"HTTP error on batch {b_idx+1}: {e}")
                paragraph, para_tokens = "", 0

            paragraph = _clean_output(paragraph)

            total_tokens += para_tokens
            accumulated_text += paragraph + "\n"
            elapsed = time.time() - t0
            log(f"Batch {b_idx+1}/{n_batches} (sections {first_i+1}-{last_i+1}): {para_tokens} tokens in {elapsed:.1f}s. Total: {total_tokens}")

            if on_event:
                try:
                    on_event("section_complete", paragraph)
                except Exception:
                    pass

    notify("Finalizing document... (100%)")
    log(f"ALL DONE. {total_tokens} tokens.")
    return accumulated_text.strip()
