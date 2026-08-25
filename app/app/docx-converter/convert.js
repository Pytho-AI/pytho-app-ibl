#!/usr/bin/env node
// DOCX converter -- exact copy of pytho-vite's agentService.js htmlToDocx + addWatermarkToDocx
// Usage: echo '{"html":"...","outputPath":"/tmp/out.docx","header":"...","watermark":"..."}' | node convert.js

const { Document, Packer, Paragraph, TextRun, ImageRun, AlignmentType, Header, Footer, PageNumber, NumberFormat, PageOrientation, Table, TableRow, TableCell, WidthType, BorderStyle, XmlComponent } = require('docx');
const cheerio = require('cheerio');
const JSZip = require('jszip');
const fs = require('fs');
const path = require('path');

// ---------------------------------------------------------------------------
// Canonical-on-disk SDT wrappers — post-process the rendered DOCX zip to
// inject w:sdt block-level wrappers around the marker regions emitted by
// utils/exporter.py via the data-pytho-tag attribute.
//
// docx-npm's higher-level Paragraph/Table API doesn't accept arbitrary
// w:sdt wrappers (no public API to construct an SdtBlock). The reliable
// path that works across docx-npm versions is to:
//   1. Build the document body normally (paragraphs ignore data-pytho-tag),
//      while collecting the data-pytho-tag → paragraph-range map.
//   2. After Packer produces the .docx zip, open document.xml, find every
//      paragraph by its insertion order, and wrap consecutive paragraphs
//      that belong to the same marker region in a w:sdt block.
//
// `_pythoMarkerStream` is the list of marker boundaries collected during
// HTML walk. Each entry is `{tag, alias, start, end}` where start/end are
// 0-based paragraph indices into the body's <w:p> sequence.
// ---------------------------------------------------------------------------

const PYTHO_NS_W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main';

function xmlEscapeAttr(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function randomSdtId() {
  // OOXML w:id is an int32; positive, non-zero.
  return Math.floor(Math.random() * 2147483646) + 1;
}

// Post-process the produced DOCX buffer: walk the body's <w:p> sequence
// in order, group consecutive paragraphs by their marker (tracked via
// `markers`), and wrap each group in a block-level w:sdt with the
// recorded tag + alias. Returns the patched buffer.
async function injectPythoSdtWrappers(buffer, markers) {
  if (!markers || markers.length === 0) return buffer;
  let zip;
  try {
    zip = await JSZip.loadAsync(buffer);
  } catch (_) {
    return buffer;
  }
  const docXml = await zip.file('word/document.xml')?.async('string');
  if (!docXml) return buffer;
  // Build a paragraph-index map by walking <w:p> elements in body order.
  // We use a state-machine line-by-line scan; the docx-npm output uses
  // self-closing tags only at element boundaries so a simple regex on
  // `<w:p ` / `</w:p>` is reliable.
  //
  // CRITICAL: count ONLY top-level <w:p> elements — those that sit
  // directly under <w:body>, not the <w:p> elements INSIDE <w:tc>
  // table cells. The marker firstIdx/lastIdx tracked above index into
  // the cheerio `allEls` walker, which does NOT descend into
  // <table>/<tr>/<td> children (cheerio's `$(el).find` over the body
  // selector list collects block-level wrappers, but the marker pre-
  // pass uses the allEls index which treats a whole <table> as ONE
  // entry — see _markerDivs build above). If we counted every <w:p>
  // here (including cell-internal ones), markers placed AFTER a table
  // would index into <w:p> elements that sit INSIDE the table's last
  // <w:tc>, and the injected <w:sdt> open/close would land inside a
  // table cell — producing OOXML where <w:sdt> spans <w:tc>
  // boundaries (Opening and ending tag mismatch: tc and sdtContent
  // / sdtContent and tc). Word / python-docx / Collabora reject the
  // file as corrupt.
  //
  // Fix: scan for <w:tc> / </w:tc> alongside <w:p> / </w:p>, track
  // tc-nesting depth, and only record <w:p> offsets where depth==0
  // (i.e. the <w:p> is body-level, not cell-internal).
  const tokenRe = /<w:p(?:\s|>|\/)|<\/w:p>|<w:tc(?:\s|>|\/)|<\/w:tc>/g;
  const opens = [];
  const closes = [];
  let tcDepth = 0;
  let m;
  while ((m = tokenRe.exec(docXml)) !== null) {
    const tok = m[0];
    if (tok.startsWith('<w:tc')) { tcDepth++; continue; }
    if (tok.startsWith('</w:tc>')) { tcDepth--; continue; }
    if (tcDepth > 0) continue;
    if (tok.startsWith('</w:p>')) closes.push(m.index + tok.length);
    else opens.push(m.index);
  }
  if (opens.length === 0 || opens.length !== closes.length) return buffer;
  // Sort markers by start paragraph index ascending; wrap each.
  const sorted = [...markers].sort((a, b) => a.start - b.start);
  let patched = '';
  let cursor = 0;
  // Walk paragraph indices, emitting SDT open before opens[start], SDT close
  // after closes[end]. We process paragraphs in document order so we don't
  // need recursive nesting at this layer (the exporter currently wraps each
  // marker region as a flat top-level block; nested regions will need
  // sorted-by-start-then-by-end-desc ordering — that's a follow-up).
  // Build a sparse open/close index keyed by paragraph index.
  const openAtPara = {};
  const closeAtPara = {};
  for (const mk of sorted) {
    if (typeof mk.start !== 'number' || typeof mk.end !== 'number') continue;
    if (mk.start < 0 || mk.end >= opens.length || mk.start > mk.end) continue;
    const sdtPr = `<w:sdt><w:sdtPr><w:tag w:val="${xmlEscapeAttr(mk.tag)}"/><w:alias w:val="${xmlEscapeAttr(mk.alias || '')}"/><w:id w:val="${randomSdtId()}"/><w:lock w:val="unlocked"/></w:sdtPr><w:sdtContent>`;
    const sdtClose = `</w:sdtContent></w:sdt>`;
    openAtPara[mk.start] = (openAtPara[mk.start] || '') + sdtPr;
    closeAtPara[mk.end] = sdtClose + (closeAtPara[mk.end] || '');
  }
  // Stitch the output: copy chunks of docXml interleaved with wrappers.
  patched = '';
  cursor = 0;
  for (let i = 0; i < opens.length; i++) {
    const oStart = opens[i];
    const cEnd = closes[i];
    // Emit prefix up to <w:p ...
    if (oStart > cursor) {
      patched += docXml.substring(cursor, oStart);
    }
    if (openAtPara[i]) patched += openAtPara[i];
    // Emit <w:p ...> through </w:p>
    patched += docXml.substring(oStart, cEnd);
    if (closeAtPara[i]) patched += closeAtPara[i];
    cursor = cEnd;
  }
  if (cursor < docXml.length) patched += docXml.substring(cursor);
  zip.file('word/document.xml', patched);
  return await zip.generateAsync({ type: 'nodebuffer' });
}


// Embed image from <img src="..."> nodes. Resolves src against an optional
// imageBaseDir option, the artifact /files/<owner>/<aid>/... convention,
// an absolute path, then a known mock-map test location. Width/height are
// derived from the img's inline `style="width:Xpt;height:Ypt"` (or
// `width=`/`height=` attrs); defaults are sized to fit the page-usable width.
//
// Image inlining is REQUIRED — leaving an <img> as a URL reference yields
// a DOCX zip with no `word/media/*` parts. Collabora opens the doc, tries
// to fetch the URL from inside its container (where /files/... is
// unroutable), and renders the doc with the map missing. The exporter
// emits map figures with `src="/files/<owner>/<artifact_id>/<field>.jpg"`
// (see render_artifact_html's shape="map" branch), and the file on disk
// is at `<UPLOAD_DIR>/<owner>/<artifact_id>/<field>.jpg`. So we strip the
// `/files/` prefix and resolve against `imageBaseDir` (which the python
// caller sets to UPLOAD_DIR).
function _loadImageBuffer(src, imageBaseDir) {
  if (!src) return null;
  const tried = [];
  const candidates = [];
  // /files/<owner>/<aid>/<file> → <imageBaseDir>/<owner>/<aid>/<file>
  if (imageBaseDir && src.startsWith('/files/')) {
    candidates.push(path.join(imageBaseDir, src.slice('/files/'.length)));
  }
  if (src.startsWith('/')) candidates.push(src);
  if (imageBaseDir) candidates.push(path.join(imageBaseDir, src));
  // /mock/maps/M-####.png is the convention the harness uses; resolve against
  // /tmp/mock_maps so visual walks have a real PNG even though the mock tool
  // returns a fabricated path.
  if (src.startsWith('/mock/maps/')) candidates.push(src.replace('/mock/maps/', '/tmp/mock_maps/'));
  candidates.push(src);  // last resort: relative to cwd
  for (const p of candidates) {
    tried.push(p);
    try {
      if (fs.existsSync(p)) return { buf: fs.readFileSync(p), path: p };
    } catch (_) {}
  }
  return { tried };
}

async function addWatermarkToDocx(buffer, watermarkText, watermarkColor = '#c0c0c0', watermarkFont = "'Arial Black', Arial, sans-serif", watermarkSize = '75') {
  const zip = await JSZip.loadAsync(buffer);
  watermarkColor = watermarkColor || '#c0c0c0';
  watermarkFont = watermarkFont || "'Arial Black', Arial, sans-serif";
  watermarkSize = watermarkSize || '75';
  const fillColor = watermarkColor.startsWith('#') ? watermarkColor : `#${watermarkColor}`;
  const primaryFont = watermarkFont.split(',')[0].trim().replace(/['"]/g, '');

  const sizePercent = parseInt(watermarkSize, 10) || 75;
  const scale = (sizePercent / 100) * 0.9;
  const pageWidth = 612;
  const cos45 = 0.7071;
  const aspectRatio = 4;
  const baseHeight = pageWidth / (cos45 * (aspectRatio + 1));
  const baseWidth = aspectRatio * baseHeight;
  const shapeWidth = Math.round(baseWidth * scale * 100) / 100;
  const shapeHeight = Math.round(baseHeight * scale * 100) / 100;

  const vmlShape = `
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
    <v:shape id="PowerPlusWaterMarkObject" o:spid="_x0000_s2049" type="#_x0000_t136"
      style="position:absolute;margin-left:0;margin-top:0;width:${shapeWidth}pt;height:${shapeHeight}pt;rotation:-45;z-index:-251658752;mso-position-horizontal:center;mso-position-horizontal-relative:page;mso-position-vertical:center;mso-position-vertical-relative:page"
      o:allowincell="f" fillcolor="${fillColor}" stroked="f">
      <v:fill opacity=".5"/>
      <v:textpath style="font-family:&quot;${primaryFont}&quot;;font-size:72pt;" string="${watermarkText}"/>
    </v:shape>`;

  const headerXml = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
       xmlns:v="urn:schemas-microsoft-com:vml"
       xmlns:o="urn:schemas-microsoft-com:office:office"
       xmlns:w10="urn:schemas-microsoft-com:office:word"
       xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:p>
    <w:pPr>
      <w:pStyle w:val="Header"/>
    </w:pPr>
    <w:r>
      <w:pict>${vmlShape}</w:pict>
    </w:r>
  </w:p>
</w:hdr>`;

  const headerPath = 'word/header1.xml';
  const existingHeader = zip.file(headerPath);

  if (existingHeader) {
    let headerContent = await existingHeader.async('string');
    headerContent = headerContent.replace('</w:hdr>', `<w:p><w:r><w:pict>${vmlShape}</w:pict></w:r></w:p></w:hdr>`);
    zip.file(headerPath, headerContent);
  } else {
    zip.file(headerPath, headerXml);

    const contentTypesPath = '[Content_Types].xml';
    let contentTypes = await zip.file(contentTypesPath).async('string');
    if (!contentTypes.includes('header1.xml')) {
      contentTypes = contentTypes.replace('</Types>',
        '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/></Types>');
      zip.file(contentTypesPath, contentTypes);
    }

    const relsPath = 'word/_rels/document.xml.rels';
    let rels = await zip.file(relsPath).async('string');
    if (!rels.includes('header1.xml')) {
      const rIdMatches = rels.match(/rId(\d+)/g) || [];
      const maxId = Math.max(...rIdMatches.map(r => parseInt(r.replace('rId', ''))), 0);
      const newRId = `rId${maxId + 1}`;
      rels = rels.replace('</Relationships>',
        `<Relationship Id="${newRId}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/></Relationships>`);
      zip.file(relsPath, rels);

      const docPath = 'word/document.xml';
      let docXml = await zip.file(docPath).async('string');
      if (docXml.includes('<w:sectPr')) {
        docXml = docXml.replace(/<w:sectPr([^>]*)>/g, `<w:sectPr$1><w:headerReference w:type="default" r:id="${newRId}"/>`);
      } else {
        docXml = docXml.replace('</w:body>', `<w:sectPr><w:headerReference w:type="default" r:id="${newRId}"/></w:sectPr></w:body>`);
      }
      zip.file(docPath, docXml);
    }
  }

  return await zip.generateAsync({ type: 'nodebuffer' });
}

const DEFAULT_BODY_FONT = 'Times New Roman';

function extractFontFamily(styleAttr) {
  if (!styleAttr) return null;
  const match = styleAttr.match(/font-family:\s*([^;]+)/);
  if (!match) return null;
  return match[1].split(',')[0].trim().replace(/['"]/g, '');
}

async function htmlToDocx(html, options = {}) {
  const { header, headerStyle, footerText, footerStyle, watermark, watermarkColor, watermarkFont, watermarkSize, defaultFont } = options;
  const bodyFont = defaultFont || DEFAULT_BODY_FONT;

  html = html.replace(/<!--\s*pagebreak\s*-->/gi, '<div class="mce-pagebreak"></div>');

  const $ = cheerio.load(html);

  // Extract page margins + orientation from pytho-doc-settings if present.
  // `pageOrientation` ('portrait' | 'landscape') is written by the exporter
  // (utils/exporter.py:render_artifact_html) and by the DocEditor save path
  // (DocEditor.tsx) so landscape products round-trip with the right paper
  // size on save. An explicit options.orientation (callers passing it through
  // html_to_docx) still wins so existing call sites are untouched.
  const settingsScript = $('script#pytho-doc-settings');
  let pageMargins = { top: 1440, right: 1440, bottom: 1440, left: 1440 };
  let settingsOrientation = null;
  if (settingsScript.length) {
    try {
      const settings = JSON.parse(settingsScript.html());
      if (settings.pageMargins) {
        const m = settings.pageMargins;
        pageMargins = {
          top: Math.round((m.top ?? 1) * 1440),
          right: Math.round((m.right ?? 1) * 1440),
          bottom: Math.round((m.bottom ?? 1) * 1440),
          left: Math.round((m.left ?? 1) * 1440),
        };
      }
      if (settings.pageOrientation === 'portrait' || settings.pageOrientation === 'landscape') {
        settingsOrientation = settings.pageOrientation;
      }
    } catch (_) {}
    settingsScript.remove();
  }
  // Embedded pageOrientation wins when the caller did not pass one explicitly
  // (the DocEditor save path doesn't — it just forwards the HTML).
  if (settingsOrientation && !options.orientation) {
    options.orientation = settingsOrientation;
  }

  // Strip body-level classification banners — Word stamps the banner via the
  // page header on every page (see Header/Footer wiring below). Keeping the
  // body banner would double-stamp it on page 1. The HTML preview in
  // DocEditor still renders the banner for the analyst; only the DOCX path
  // suppresses it. Class contract: `ibl-classification-banner` set by
  // utils/exporter.py:_classification_banner_html.
  $('.ibl-classification-banner').remove();
  // Strip compact pipe-joined meta-band ribbons — these flatten into an
  // unreadable wall of pipes in Word (the cell wraps mid-line and the
  // visual structure collapses). The full kv-table below the ribbon has the
  // same fields formatted as rows. HTML preview keeps the ribbon.
  // Class contract: `ibl-meta-band` set by utils/exporter.py:_meta_band_html.
  $('.ibl-meta-band').remove();

  const paragraphs = [];
  let nextParagraphHasPageBreak = false;

  const processList = (listEl) => {
    const $list = $(listEl);
    const isOrdered = listEl.name === 'ol';
    let itemNumber = 0;

    $list.children('li').each((liIdx, liEl) => {
      itemNumber++;
      const $li = $(liEl);
      const styleAttr = $li.attr('style') || $list.attr('style') || '';
      const fontSizeMatch = styleAttr.match(/font-size:\s*(\d+)pt/);
      const fontSize = fontSizeMatch ? parseInt(fontSizeMatch[1]) * 2 : 24;
      const listFont = extractFontFamily(styleAttr) || bodyFont;
      const marginBottomMatch = styleAttr.match(/margin-bottom:\s*(\d+)pt/);
      const marginBottom = marginBottomMatch ? parseInt(marginBottomMatch[1]) * 20 : 120;

      const runs = [];

      if (isOrdered) {
        runs.push(new TextRun({ text: `${itemNumber}. `, font: listFont, size: fontSize }));
      } else {
        runs.push(new TextRun({ text: '• ', font: listFont, size: fontSize }));
      }

      const processListNode = (node, inherited = {}) => {
        if (node.type === 'text') {
          let text = node.data;
          text = text.replace(/\u00a0/g, ' ');
          if (text) {
            runs.push(new TextRun({
              text, bold: inherited.bold, italics: inherited.italic,
              underline: inherited.underline ? {} : undefined,
              font: inherited.font || listFont, size: fontSize,
            }));
          }
        } else if (node.type === 'tag') {
          const newInherited = { ...inherited };
          if (node.name === 'strong' || node.name === 'b') newInherited.bold = true;
          if (node.name === 'em' || node.name === 'i') newInherited.italic = true;
          if (node.name === 'u') newInherited.underline = true;
          const nodeStyle = node.attribs?.style || '';
          const inlineFont = extractFontFamily(nodeStyle);
          if (inlineFont) newInherited.font = inlineFont;
          if (node.children) node.children.forEach(child => processListNode(child, newInherited));
        }
      };

      liEl.children.forEach(child => processListNode(child, {}));

      if (runs.length > 0) {
        paragraphs.push(new Paragraph({
          children: runs, alignment: AlignmentType.LEFT,
          spacing: { before: 0, after: marginBottom, line: 360 },
          indent: { left: 720 },
        }));
      }
    });
  };

  const thinBorder = { style: BorderStyle.SINGLE, size: 1, color: '000000' };
  const defaultTableBorders = {
    top: thinBorder, bottom: thinBorder,
    left: thinBorder, right: thinBorder,
  };

  const processTable = (tableEl) => {
    const $table = $(tableEl);
    const rows = [];
    // Build columnWidths from the first row's cell widths so docx-npm produces a
    // proper <w:tblGrid> instead of defaulting every column to 100 twips (~5pt
    // wide), which crushes content. Page-usable width depends on orientation:
    // Page-usable DXA: 8.5"/11" page width minus left+right margins.
    // Margins come from pytho-doc-settings; default is 1" / 1440 DXA each
    // side. Recompute usable width from actual margins so table widths
    // match the exporter's _PAGE_USABLE_DXA computation. Landscape uses
    // 11" page width, portrait 8.5".
    const _pageWidthDxa = options.orientation === 'landscape' ? 15840 : 12240;
    const PAGE_USABLE_DXA = _pageWidthDxa - pageMargins.left - pageMargins.right;
    const firstRow = $table.find('tr').first();
    const firstRowCells = firstRow.find('td, th').toArray();
    const columnWidths = [];
    if (firstRowCells.length > 0) {
      // Pass 1: collect per-cell intent (DXA value, PCT fraction, or null=auto)
      const intents = firstRowCells.map((cell) => {
        const s = $(cell).attr('style') || '';
        const m = s.match(/width:\s*(\d+(?:\.\d+)?)(%|px|pt)/);
        if (!m) return { kind: 'auto' };
        const v = parseFloat(m[1]);
        if (m[2] === '%') return { kind: 'pct', frac: v / 100 };
        return { kind: 'dxa', dxa: v };
      });
      // Pass 2: distribute remaining width to AUTO columns evenly.
      let usedDxa = 0, autoCount = 0;
      intents.forEach((it) => {
        if (it.kind === 'dxa') usedDxa += it.dxa;
        else if (it.kind === 'pct') usedDxa += Math.round(it.frac * PAGE_USABLE_DXA);
        else autoCount++;
      });
      const remaining = Math.max(0, PAGE_USABLE_DXA - usedDxa);
      const perAuto = autoCount > 0 ? Math.round(remaining / autoCount) : 0;
      intents.forEach((it) => {
        if (it.kind === 'dxa') columnWidths.push(it.dxa);
        else if (it.kind === 'pct') columnWidths.push(Math.round(it.frac * PAGE_USABLE_DXA));
        else columnWidths.push(perAuto || Math.round(PAGE_USABLE_DXA / firstRowCells.length));
      });
    }

    $table.find('tr').each((_, trEl) => {
      const cells = [];
      $(trEl).find('td, th').each((_, cellEl) => {
        const $cell = $(cellEl);
        const cellStyle = $cell.attr('style') || '';
        const cellFont = extractFontFamily(cellStyle) || bodyFont;
        const cellFontSizeMatch = cellStyle.match(/font-size:\s*(\d+(?:\.\d+)?)pt/);
        const cellFontSize = cellFontSizeMatch ? Math.round(parseFloat(cellFontSizeMatch[1]) * 2) : 24;
        const isTh = cellEl.name === 'th';
        // Parse cell-level CSS properties that affect the run: color,
        // font-weight, text-transform. The "filled bar" headings carry
        // these on the TD itself rather than on a wrapper inside.
        const cellColorMatch = cellStyle.match(/(?:^|;|\s)color:\s*(#[0-9a-fA-F]+)/);
        const cellColor = cellColorMatch ? cellColorMatch[1].replace('#', '') : null;
        const cellBoldByStyle = /font-weight:\s*(bold|[6789]\d\d)/i.test(cellStyle);
        const cellUppercase = /text-transform:\s*uppercase/i.test(cellStyle);

        const cellRuns = [];

        const processCellNode = (node, inherited = {}) => {
          if (node.type === 'text') {
            let text = node.data;
            text = text.replace(/\u00a0/g, ' ');
            if (cellUppercase) text = text.toUpperCase();
            if (text) {
              cellRuns.push(new TextRun({
                text,
                bold: inherited.bold,
                italics: inherited.italic,
                underline: inherited.underline ? {} : undefined,
                font: inherited.font || cellFont,
                size: cellFontSize,
                color: inherited.color || cellColor || undefined,
              }));
            }
          } else if (node.type === 'tag') {
            const newInherited = { ...inherited };
            if (node.name === 'strong' || node.name === 'b') newInherited.bold = true;
            if (node.name === 'em' || node.name === 'i') newInherited.italic = true;
            if (node.name === 'u') newInherited.underline = true;
            const nodeStyle = node.attribs?.style || '';
            if (nodeStyle.includes('font-weight: bold') || nodeStyle.includes('font-weight:bold')) newInherited.bold = true;
            if (nodeStyle.includes('font-style: italic') || nodeStyle.includes('font-style:italic')) newInherited.italic = true;
            if (nodeStyle.includes('text-decoration: underline') || nodeStyle.includes('text-decoration:underline')) newInherited.underline = true;
            const inlineFont = extractFontFamily(nodeStyle);
            if (inlineFont) newInherited.font = inlineFont;
            const inlineColor = nodeStyle.match(/(?:^|;|\s)color:\s*(#[0-9a-fA-F]+)/);
            if (inlineColor) newInherited.color = inlineColor[1].replace('#', '');
            if (node.name === 'br') {
              cellRuns.push(new TextRun({ break: 1 }));
            }
            if (node.children) {
              node.children.forEach(child => processCellNode(child, newInherited));
            }
          }
        };

        cellEl.children.forEach(child => {
          if (child.type === 'tag' && child.name === 'p') {
            const pStyle = child.attribs?.style || '';
            const pFont = extractFontFamily(pStyle) || cellFont;
            const pBold = pStyle.includes('font-weight: bold') || pStyle.includes('font-weight:bold');
            child.children?.forEach(grandchild => processCellNode(grandchild, { bold: isTh || pBold || cellBoldByStyle, font: pFont }));
          } else {
            processCellNode(child, { bold: isTh || cellBoldByStyle });
          }
        });

        if (cellRuns.length === 0) {
          cellRuns.push(new TextRun({ text: '' }));
        }

        const widthMatch = cellStyle.match(/width:\s*(\d+(?:\.\d+)?)(%|px|pt)/);
        const cellWidth = widthMatch
          ? { size: parseFloat(widthMatch[1]) * (widthMatch[2] === '%' ? 100 : 1), type: widthMatch[2] === '%' ? WidthType.PERCENTAGE : WidthType.DXA }
          : { size: 0, type: WidthType.AUTO };

        const bgMatch = cellStyle.match(/background-color:\s*([^;]+)/);

        // Parse CSS `padding` on the cell into TableCell margins so the
        // S_TD / S_TH_HEADER_ROW spec (padding:6pt 10pt) lands in OOXML
        // tableCellMargins. Supported forms:
        //   padding: 6pt 10pt          → v=6, h=10
        //   padding: 6pt 10pt 6pt 10pt → t=6, r=10, b=6, l=10
        //   padding: 7pt               → t=r=b=l=7
        // 1pt = 20 twips/DXA. Default top/bottom 100 twips, left/right 100
        // twips applied when no `padding:` declared (matches docx-npm default).
        const padMatch = cellStyle.match(/padding:\s*([^;]+)/);
        let cellMargins;
        if (padMatch) {
          const parts = padMatch[1].trim().split(/\s+/).map(s => {
            const m = s.match(/^(\d+(?:\.\d+)?)\s*(pt|px)?$/);
            if (!m) return 0;
            const v = parseFloat(m[1]);
            const unit = m[2] || 'pt';
            // 1pt = 20 twips; 1px ≈ 15 twips (treating CSS px as 0.75pt).
            return Math.round(unit === 'px' ? v * 15 : v * 20);
          });
          let top, right, bottom, left;
          if (parts.length === 1) { top = right = bottom = left = parts[0]; }
          else if (parts.length === 2) { top = bottom = parts[0]; right = left = parts[1]; }
          else if (parts.length === 3) { top = parts[0]; right = left = parts[1]; bottom = parts[2]; }
          else { top = parts[0]; right = parts[1]; bottom = parts[2]; left = parts[3]; }
          cellMargins = { top, right, bottom, left };
        }

        cells.push(new TableCell({
          children: [new Paragraph({ children: cellRuns })],
          columnSpan: parseInt($cell.attr('colspan')) || 1,
          rowSpan: parseInt($cell.attr('rowspan')) || 1,
          width: cellWidth,
          borders: defaultTableBorders,
          shading: bgMatch ? { fill: bgMatch[1].trim().replace('#', ''), type: 'clear' } : undefined,
          margins: cellMargins,
        }));
      });
      if (cells.length > 0) {
        // cantSplit keeps each table row from being broken across pages —
        // prevents orphaned cells when a row falls at the page boundary.
        rows.push(new TableRow({ children: cells, cantSplit: true }));
      }
    });

    if (rows.length === 0) return null;

    const tableOpts = {
      rows,
      // OOXML CT_TblWidth pct unit = 50ths-of-a-percent — 5000 == 100%.
      // docx-npm appends a `%` to the value but stores the raw number, so
      // pass 5000 (not 100) for full page width. Earlier code passed 100
      // which renders as 2% width; LibreOffice tolerated it via auto-fit
      // but the cell-shading render dropped when many tables stacked up.
      width: { size: 5000, type: WidthType.PERCENTAGE },
    };
    if (columnWidths.length > 0) tableOpts.columnWidths = columnWidths;
    return new Table(tableOpts);
  };

  // Approximate page-usable width in EMU for image sizing: 914400 EMU = 1 inch.
  // 6.5" portrait = 5,943,600; 9.5" landscape = 8,686,800.
  const PAGE_USABLE_EMU = options.orientation === 'landscape' ? 8686800 : 5943600;

  // Canonical-on-disk marker stream — populated as we walk
  // data-pytho-tag-carrying <div> elements. Each entry records the
  // (tag, alias, start_paragraph_index, end_paragraph_index) for the
  // wrapper region. Post-Packer, injectPythoSdtWrappers stitches w:sdt
  // around the paragraph range in the generated document.xml.
  const _pythoMarkerStream = [];

  const allEls = $('p, h1, h2, h3, h4, h5, h6, div, ol, ul, table, hr, figure, img').toArray();

  // Pre-pass: identify every <div data-pytho-tag>, find its descendant
  // block elements within `allEls`, and capture (firstDescIdx, lastDescIdx)
  // so we can close the marker at the right paragraph boundary. We use
  // cheerio's contains/find to identify descendants; for nested markers
  // each parent marker spans the union of its children's blocks.
  const _markerDivs = []; // [{el, firstIdx, lastIdx, tag, alias}]
  const _allElsIndex = new Map();
  allEls.forEach((e, i) => _allElsIndex.set(e, i));
  allEls.forEach((el) => {
    if (el.name !== 'div') return;
    const tag = el.attribs && el.attribs['data-pytho-tag'];
    if (!tag) return;
    const alias = (el.attribs && el.attribs['data-pytho-alias']) || '';
    const desc = $(el).find('p, h1, h2, h3, h4, h5, h6, div, ol, ul, table, hr, figure, img').toArray();
    const inWalker = desc.filter(e => _allElsIndex.has(e));
    if (inWalker.length === 0) return;
    const first = _allElsIndex.get(inWalker[0]);
    const last = _allElsIndex.get(inWalker[inWalker.length - 1]);
    _markerDivs.push({ el, firstIdx: first, lastIdx: last, tag, alias });
  });

  // Sort markers so we open largest first / close smallest first
  // (standard nested-region pattern). Open = ascending firstIdx; ties
  // broken by lastIdx descending so a larger enclosing region opens
  // before a smaller nested one at the same start.
  _markerDivs.sort((a, b) => {
    if (a.firstIdx !== b.firstIdx) return a.firstIdx - b.firstIdx;
    return b.lastIdx - a.lastIdx;
  });

  // Stash open/close events by paragraph-index sentinels. The post-Packer
  // pass converts paragraph-INDEX to <w:p> ordinals; here we record
  // PRE-walk allEls indices.
  const _openAt = {}; // allElsIdx -> [markers to open before allElsIdx is processed]
  const _closeAt = {}; // allElsIdx -> [markers to close after allElsIdx is processed]
  for (const md of _markerDivs) {
    (_openAt[md.firstIdx] = _openAt[md.firstIdx] || []).push(md);
    (_closeAt[md.lastIdx] = _closeAt[md.lastIdx] || []).push(md);
  }

  // paragraphs.length BEFORE and AFTER each allEls iteration. After the
  // walk we know exactly which paragraph indices belong to each allEls
  // element; the marker open/close fires from this table.
  const _paraBeforeIdx = new Array(allEls.length);
  const _paraAfterIdx = new Array(allEls.length);

  allEls.forEach((el, idx) => {
    _paraBeforeIdx[idx] = paragraphs.length;
    // Handle tables
    if (el.name === 'table') {
      const table = processTable(el);
      if (table) {
        paragraphs.push(table);
        // docx-npm collapses the next paragraph's spacing-before when it
        // immediately follows a table. Insert a small empty paragraph so the
        // headings/section breaks below the table have visible breathing room.
        paragraphs.push(new Paragraph({
          children: [new TextRun({ text: '' })],
          spacing: { before: 0, after: 120 },
        }));
      }
      return;
    }
    // <hr> — emit an empty paragraph with a bottom border (no text).
    // Used by the exporter's Sources section separator and as a per-section
    // page-break marker for heavy COA / operation blocks. When the inline
    // style declares `page-break-before:always` we toggle the NEXT
    // paragraph's pageBreakBefore flag rather than the hr itself (the hr
    // paragraph carries the border; the break goes on the heading that
    // follows it).
    if (el.name === 'hr') {
      const hrStyle = el.attribs?.style || '';
      const pageBreak = /page-break-before:\s*always/i.test(hrStyle);
      if (pageBreak) {
        nextParagraphHasPageBreak = true;
        return; // skip the rule line on a page-break (clean break instead)
      }
      paragraphs.push(new Paragraph({
        children: [new TextRun({ text: '' })],
        border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: '000000', space: 1 } },
        spacing: { before: 240, after: 120 },
      }));
      return;
    }
    // <img> — embed image inline. Used by GRINTSUM (map figure) and any
    // schema that wants a figure embedded in the rendered DOCX. Resolves
    // src against options.imageBaseDir / a /mock/maps/ → /tmp/mock_maps/
    // convention so visual harness renders pick up placeholder maps.
    if (el.name === 'img' || el.name === 'figure') {
      const $img = el.name === 'figure' ? $(el).find('img').first() : $(el);
      // Map figures (figure.ibl-map-embed) are aspect-locked at ~3:2 and
      // routinely approach the full page-usable height. When one lands near
      // a page boundary Word splits the image across pages. Force a clean
      // page break before the figure so the map always renders whole.
      const isMapFigure = el.name === 'figure'
        && /\bibl-map-embed\b/.test($(el).attr('class') || '');
      const src = $img.attr('src');
      const loaded = _loadImageBuffer(src, options.imageBaseDir);
      const styleA = ($img.attr('style') || '') + ' ' + ($(el).attr('style') || '');
      const widthAttr = parseInt($img.attr('width') || '0') || 0;
      const heightAttr = parseInt($img.attr('height') || '0') || 0;
      const styleW = styleA.match(/width:\s*(\d+(?:\.\d+)?)pt/);
      const styleH = styleA.match(/height:\s*(\d+(?:\.\d+)?)pt/);
      // Default to ~85% of page-usable width, preserving aspect (4:3-ish for a map).
      let widthEmu, heightEmu;
      if (styleW) widthEmu = Math.round(parseFloat(styleW[1]) * 12700);  // pt → EMU
      else if (widthAttr) widthEmu = Math.round(widthAttr * 9525);       // px → EMU
      else widthEmu = Math.round(PAGE_USABLE_EMU * 0.82);
      if (styleH) heightEmu = Math.round(parseFloat(styleH[1]) * 12700);
      else if (heightAttr) heightEmu = Math.round(heightAttr * 9525);
      else heightEmu = Math.round(widthEmu * 0.72);                       // ~3:2 default
      if (loaded && loaded.buf) {
        const ext = (path.extname(loaded.path).slice(1) || 'png').toLowerCase();
        const docxType = ['jpg', 'jpeg', 'gif', 'bmp', 'png'].includes(ext)
          ? (ext === 'jpeg' ? 'jpg' : ext) : 'png';
        // Alt-text is the LiveEditor join key. For map figures we encode
        // `pytho-map:<map_id>` so the pytho_graphics.py UNO script returns
        // it as the image's Title, and the frontend can pair each Writer
        // graphic to its map_embeds.json entry. Reads the data-map-id /
        // data-map-field attrs the exporter stamped on the <figure>.
        let altText = $img.attr('alt') || '';
        if (isMapFigure) {
          const mapId = $(el).attr('data-map-id') || '';
          const mapField = $(el).attr('data-map-field') || '';
          const tag = (mapId || mapField).trim();
          if (tag) altText = `pytho-map:${tag}`;
        }
        try {
          const img = new ImageRun({
            data: loaded.buf, type: docxType,
            altText: altText ? { title: altText, description: altText, name: altText } : undefined,
            transformation: {
              width: Math.round(widthEmu / 9525),    // ImageRun uses pixels
              height: Math.round(heightEmu / 9525),
            },
          });
          // keepNext: true so the image stays with its caption paragraph —
          // GRINTSUM map figure was page-breaking between the image and the
          // "Figure 1. …" caption, leaving an orphan caption on the next page.
          paragraphs.push(new Paragraph({
            children: [img],
            alignment: AlignmentType.CENTER,
            spacing: { before: 80, after: 80 },
            keepNext: true,
            pageBreakBefore: isMapFigure,
          }));
        } catch (err) {
          paragraphs.push(new Paragraph({
            children: [new TextRun({ text: `[image load failed: ${src} — ${err.message}]`, italics: true, color: '666666', size: 18 })],
            alignment: AlignmentType.CENTER,
          }));
        }
      } else {
        const tries = loaded && loaded.tried ? loaded.tried.join(', ') : '(none)';
        paragraphs.push(new Paragraph({
          children: [new TextRun({ text: `[image not found: ${src} — tried ${tries}]`, italics: true, color: '666666', size: 18 })],
          alignment: AlignmentType.CENTER,
        }));
      }
      return;
    }
    // Headings / bold paragraphs followed immediately by a block element get
    // keepNext so they don't end up orphaned at the bottom of a page above
    // their content. GRINTSUM map heading was getting separated from its
    // image; CCA "Camps" sub-header was getting separated from its value.
    // All h1-h6 keep with whatever follows (table, image, or another paragraph).
    const nextEl = allEls[idx + 1];
    const isCurrentHeading = /^h[1-6]$/.test(el.name);
    const keepWithNextTable = nextEl && (
      nextEl.name === 'table' ||
      nextEl.name === 'img' ||
      nextEl.name === 'figure' ||
      (isCurrentHeading && /^(p|h[1-6]|ol|ul|div)$/.test(nextEl.name))
    );

    if (el.name === 'ol' || el.name === 'ul') { processList(el); return; }

    const $el = $(el);
    if ($el.hasClass('mce-pagebreak') || $el.hasClass('pagebreak')) { nextParagraphHasPageBreak = true; return; }
    if ($el.hasClass('doc-watermark') || $el.hasClass('doc-header') || $el.hasClass('doc-footer')) return;

    // Define styleAttr up-front so the div-with-block-children branch
    // below can read margin-left from it without a temporal-dead-zone
    // crash. Prior code defined styleAttr below the div handling and
    // worked only because divs that triggered the indent propagation
    // happened to have a style attr that was already inherited via the
    // parent's _indent_dxa token (which the regex still matched). The
    // canonical-on-disk data-pytho-tag wrappers exercise this branch
    // unconditionally — wrappers contain block <p> children — so the
    // const-before-use ordering bug surfaced.
    const styleAttr = $el.attr('style') || '';

    // <div> wrappers used for indentation (sub-paragraph grouping in INTSUM
    // Para 2/3/4) contain block children (h3 + p) that are independently
    // enumerated by cheerio's selector below. Skip the div itself — its block
    // children will be processed in document order on their own; otherwise
    // they get DOUBLE-rendered (once flattened inline as the div paragraph,
    // once as their own block). Inline `margin-left` is preserved on the
    // child block elements via the post-processing indent below.
    if (el.name === 'div') {
      const hasBlockChild = el.children && el.children.some(
        c => c.type === 'tag' && ['p', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                                    'div', 'ol', 'ul', 'table', 'hr',
                                    'figure', 'img'].includes(c.name)
      );
      if (hasBlockChild) {
        // Propagate the div's margin-left onto its block children so the
        // indent survives the flatten.
        const divIndentMatch = styleAttr.match(/margin-left:\s*(\d+(?:\.\d+)?)pt/);
        if (divIndentMatch) {
          const indentDxa = Math.round(parseFloat(divIndentMatch[1]) * 20);
          el.children.forEach(c => {
            if (c.type === 'tag') {
              const existing = c.attribs && c.attribs.style ? c.attribs.style : '';
              const additionalStyle = `__indent-dxa:${indentDxa}`;
              c.attribs = c.attribs || {};
              c.attribs.style = existing ? `${existing};${additionalStyle}` : additionalStyle;
            }
          });
        }
        return;  // children enumerated separately
      }
    }

    // styleAttr was already defined above the div block-child handler.
    let textAlignMatch = styleAttr.match(/text-align:\s*(\w+)/);
    let textAlign = textAlignMatch ? textAlignMatch[1] : null;
    if (!textAlign) {
      let parent = $el.parent();
      while (parent.length && !textAlign) {
        const parentMatch = (parent.attr('style') || '').match(/text-align:\s*(\w+)/);
        if (parentMatch) textAlign = parentMatch[1];
        parent = parent.parent();
      }
    }

    let alignment = AlignmentType.LEFT;
    if (textAlign === 'center') alignment = AlignmentType.CENTER;
    else if (textAlign === 'right') alignment = AlignmentType.RIGHT;
    else if (textAlign === 'justify') alignment = AlignmentType.JUSTIFIED;

    const fontSizeMatch = styleAttr.match(/font-size:\s*(\d+)pt/);
    const headingSizes = { h1: 48, h2: 36, h3: 28, h4: 24, h5: 22, h6: 20 };
    const fontSize = fontSizeMatch ? parseInt(fontSizeMatch[1]) * 2 : (headingSizes[el.name] || 24);
    const fontFamily = extractFontFamily(styleAttr) || bodyFont;
    const marginTopMatch = styleAttr.match(/margin-top:\s*(\d+)pt/);
    const marginBottomMatch = styleAttr.match(/margin-bottom:\s*(\d+)pt/);
    const marginTop = marginTopMatch ? parseInt(marginTopMatch[1]) * 20 : 0;
    const marginBottom = marginBottomMatch ? parseInt(marginBottomMatch[1]) * 20 : 200;
    const lineHeightMatch = styleAttr.match(/line-height:\s*([\d.]+)/);
    const lineHeight = lineHeightMatch ? Math.round(parseFloat(lineHeightMatch[1]) * 240) : 360;

    const isHeading = /^h[1-6]$/.test(el.name);
    const paragraphBold = isHeading || styleAttr.includes('font-weight: bold') || styleAttr.includes('font-weight:bold');
    const paragraphItalic = styleAttr.includes('font-style: italic') || styleAttr.includes('font-style:italic');
    const paragraphUnderline = styleAttr.includes('text-decoration: underline') || styleAttr.includes('text-decoration:underline');

    // Bottom rule under headings — exporter declares `border-bottom:Xpt solid #000000`
    // on h1/h2. docx-npm renders this via paragraph border (`w:pBdr/w:bottom`).
    let paragraphBorderBottom = null;
    const borderBottomMatch = styleAttr.match(/border-bottom:\s*([\d.]+)pt\s+solid\s+(#[0-9a-fA-F]+)/);
    if (borderBottomMatch) {
      paragraphBorderBottom = {
        style: BorderStyle.SINGLE,
        size: Math.max(2, Math.round(parseFloat(borderBottomMatch[1]) * 4)),  // pt → eighths-of-point
        color: borderBottomMatch[2].replace('#', ''),
        space: 4,
      };
    }
    // Left rule beside headings — exporter declares `border-left:Xpt solid #color`
    // on the per-section H3 sub-bar in WEM ("MANEUVER (TRACKED)" etc).
    let paragraphBorderLeft = null;
    const borderLeftMatch = styleAttr.match(/border-left:\s*([\d.]+)pt\s+solid\s+(#[0-9a-fA-F]+)/);
    if (borderLeftMatch) {
      paragraphBorderLeft = {
        style: BorderStyle.SINGLE,
        size: Math.max(2, Math.round(parseFloat(borderLeftMatch[1]) * 4)),
        color: borderLeftMatch[2].replace('#', ''),
        space: 4,
      };
    }
    // Paragraph background fill — exporter declares `background-color:#xxxxxx`
    // on the filled-bar section heading H2 (dark fill, white text) and the
    // sub-section H3 (light gray fill). docx-npm renders this as paragraph
    // shading on the property element.
    let paragraphShading = null;
    const paraBgMatch = styleAttr.match(/background-color:\s*(#[0-9a-fA-F]+)/);
    if (paraBgMatch) {
      paragraphShading = {
        type: 'clear', color: 'auto',
        fill: paraBgMatch[1].replace('#', ''),
      };
    }
    // Text color override for the heading run — exporter sets white text on
    // dark-filled section bars. We need to apply this to the runs so the
    // text isn't lost against the fill.
    let runTextColorOverride = null;
    const colorMatch = styleAttr.match(/(?:^|;|\s)color:\s*(#[0-9a-fA-F]+)/);
    if (colorMatch) {
      runTextColorOverride = colorMatch[1].replace('#', '');
    }
    // text-transform: uppercase — pre-process the text so it ships uppercased
    // (docx-npm has no native CSS transform; this is the closest match).
    const isUppercase = /text-transform:\s*uppercase/i.test(styleAttr);

    const runs = [];
    const processNode = (node, inherited = {}) => {
      if (node.type === 'text') {
        let text = node.data;
        text = text.replace(/\u00a0/g, ' ');
        if (isUppercase) text = text.toUpperCase();
        if (text && text.trim()) {
          runs.push(new TextRun({
            text, bold: inherited.bold, italics: inherited.italic,
            underline: inherited.underline ? {} : undefined,
            font: inherited.font || fontFamily, size: fontSize,
            color: inherited.color || runTextColorOverride || undefined,
            shading: inherited.shading,
          }));
        }
      } else if (node.type === 'tag') {
        const newInherited = { ...inherited };
        if (node.name === 'strong' || node.name === 'b') newInherited.bold = true;
        if (node.name === 'em' || node.name === 'i') newInherited.italic = true;
        if (node.name === 'u') newInherited.underline = true;
        const nodeStyle = node.attribs?.style || '';
        if (nodeStyle.includes('font-weight: bold') || nodeStyle.includes('font-weight:bold')) newInherited.bold = true;
        if (nodeStyle.includes('font-style: italic') || nodeStyle.includes('font-style:italic')) newInherited.italic = true;
        if (nodeStyle.includes('text-decoration: underline') || nodeStyle.includes('text-decoration:underline')) newInherited.underline = true;
        const inlineFont = extractFontFamily(nodeStyle);
        if (inlineFont) newInherited.font = inlineFont;
        // Inline color (e.g., the [ANALYST INPUT REQUIRED] red badge span)
        const inlineColor = nodeStyle.match(/(?:^|;|\s)color:\s*(#[0-9a-fA-F]+)/);
        if (inlineColor) newInherited.color = inlineColor[1].replace('#', '');
        // Inline background \u2014 convert.js docx-npm renders run-level shading
        // as a character-level background fill.
        const inlineBg = nodeStyle.match(/background-color:\s*(#[0-9a-fA-F]+)/);
        if (inlineBg) newInherited.shading = {
          type: 'clear', color: 'auto', fill: inlineBg[1].replace('#', ''),
        };
        if (node.name === 'br') runs.push(new TextRun({ break: 1 }));
        if (node.children) node.children.forEach(child => processNode(child, newInherited));
      }
    };

    const initialStyles = {
      bold: paragraphBold, italic: paragraphItalic, underline: paragraphUnderline,
      color: runTextColorOverride || undefined,
    };
    el.children.forEach(child => processNode(child, initialStyles));

    const paraOpts = {
      alignment,
      spacing: { before: marginTop, after: marginBottom, line: lineHeight },
      pageBreakBefore: nextParagraphHasPageBreak,
      keepNext: keepWithNextTable,
    };
    if (paragraphBorderBottom || paragraphBorderLeft) {
      paraOpts.border = {};
      if (paragraphBorderBottom) paraOpts.border.bottom = paragraphBorderBottom;
      if (paragraphBorderLeft) paraOpts.border.left = paragraphBorderLeft;
    }
    if (paragraphShading) paraOpts.shading = paragraphShading;
    // Indent propagated from a parent <div style="margin-left:Xpt"> wrapper —
    // see the block-child div handling above. Twips already computed.
    const indentMatch = styleAttr.match(/__indent-dxa:(\d+)/);
    if (indentMatch) {
      paraOpts.indent = { left: parseInt(indentMatch[1]) };
    }

    if (runs.length > 0) {
      paragraphs.push(new Paragraph({ ...paraOpts, children: runs }));
    } else {
      paragraphs.push(new Paragraph({ ...paraOpts, children: [new TextRun({ text: '' })] }));
    }
    nextParagraphHasPageBreak = false;
  });

  // Post-walk: snapshot paraAfter for every element. Since the forEach
  // doesn't have a uniform "after" hook, we recompute via the
  // _paraBeforeIdx array — _paraAfterIdx[i] = _paraBeforeIdx[i+1] for
  // i < length-1, and paragraphs.length for the last element.
  for (let i = 0; i < allEls.length; i++) {
    _paraAfterIdx[i] = (i + 1 < allEls.length) ? _paraBeforeIdx[i + 1] : paragraphs.length;
  }
  // Convert _markerDivs to _pythoMarkerStream entries using the now-known
  // (firstIdx, lastIdx) -> (startPara, endPara) mapping.
  for (const md of _markerDivs) {
    const startPara = _paraBeforeIdx[md.firstIdx];
    const endPara = _paraAfterIdx[md.lastIdx] - 1;
    if (typeof startPara !== 'number' || typeof endPara !== 'number') continue;
    if (endPara < startPara) continue;
    _pythoMarkerStream.push({ tag: md.tag, alias: md.alias, start: startPara, end: endPara });
  }

  const headers = {};
  const footers = {};

  const pageNumberParagraph = new Paragraph({
    children: [new TextRun({ children: [PageNumber.CURRENT], size: 22, font: 'Arial', color: '000000' })],
    alignment: AlignmentType.CENTER,
  });

  // Build the doctrinal-citation footer paragraph (optional). Shown above the
  // page number on every page so analysts can locate the doctrinal anchor
  // from any page, not just the last sources page.
  let doctrinalFooterParagraph = null;
  if (footerText && footerText !== 'none') {
    const fAlign = footerStyle?.align === 'left' ? AlignmentType.LEFT
                 : footerStyle?.align === 'right' ? AlignmentType.RIGHT
                 : AlignmentType.CENTER;
    const fSizeNum = parseInt((footerStyle?.fontSize || '8pt').replace('pt', '')) || 8;
    const fColor = (footerStyle?.textColor || '#404040').replace('#', '');
    const fFont = (footerStyle?.fontFamily || 'Arial, sans-serif').split(',')[0].trim();
    doctrinalFooterParagraph = new Paragraph({
      children: [new TextRun({
        text: footerText,
        italics: footerStyle?.italic !== false,
        bold: footerStyle?.bold || false,
        size: fSizeNum * 2, color: fColor, font: fFont,
      })],
      alignment: fAlign,
    });
  }

  if (header && header !== 'none') {
    const position = headerStyle?.position || 'header';
    const align = headerStyle?.align === 'center' ? AlignmentType.CENTER
                : headerStyle?.align === 'right' ? AlignmentType.RIGHT
                : AlignmentType.LEFT;
    const fontSizeStr = headerStyle?.fontSize || '11pt';
    const fontSizeNum = parseInt(fontSizeStr.replace('pt', '')) || 11;
    const fontSizeHalfPts = fontSizeNum * 2;
    const textColor = (headerStyle?.textColor || '#808080').replace('#', '');
    const hdrFontFamily = (headerStyle?.fontFamily || 'Arial, sans-serif').split(',')[0].trim();
    // fillColor turns the centered banner paragraph into a filled-color strip
    // (DoDM 5200.01: UNCLASSIFIED green, CUI purple, SECRET red, etc.).
    // We apply both paragraph shading (background) and a wide single-cell
    // border to give the visual of a full-width filled banner.
    const fillColorRaw = headerStyle?.fillColor;
    const fillColor = fillColorRaw ? fillColorRaw.replace('#', '') : null;
    const shadingOpt = fillColor ? { shading: { type: 'clear', color: 'auto', fill: fillColor } } : {};

    const makeBannerPara = () => {
      const lines = String(header).split('\n');
      const children = [];
      lines.forEach((line, i) => {
        if (i > 0) children.push(new TextRun({ break: 1 }));
        children.push(new TextRun({
          text: line, bold: headerStyle?.bold || false, italics: headerStyle?.italic || false,
          underline: headerStyle?.underline ? {} : undefined,
          size: fontSizeHalfPts, color: textColor, font: hdrFontFamily,
        }));
      });
      return new Paragraph({ children, alignment: align, ...shadingOpt });
    };

    if (position === 'header' || position === 'both') {
      headers.default = new Header({ children: [makeBannerPara()] });
    }
    if (position === 'footer' || position === 'both') {
      const footerChildren = [makeBannerPara()];
      if (doctrinalFooterParagraph) footerChildren.push(doctrinalFooterParagraph);
      footerChildren.push(pageNumberParagraph);
      footers.default = new Footer({ children: footerChildren });
    } else {
      const footerChildren = [];
      if (doctrinalFooterParagraph) footerChildren.push(doctrinalFooterParagraph);
      footerChildren.push(pageNumberParagraph);
      footers.default = new Footer({ children: footerChildren });
    }
  } else {
    const footerChildren = [];
    if (doctrinalFooterParagraph) footerChildren.push(doctrinalFooterParagraph);
    footerChildren.push(pageNumberParagraph);
    footers.default = new Footer({ children: footerChildren });
  }

  const headerFontSize = headerStyle?.fontSize ? parseInt(headerStyle.fontSize.replace('pt', '')) : 11;
  const textHeightTwips = headerFontSize * 20;
  const centerPosition = Math.round(pageMargins.top / 2);
  const headerDistance = Math.max(100, Math.round(centerPosition - textHeightTwips / 2));

  const document = new Document({
    creator: 'Pytho AI',
    title: 'Generated Document',
    sections: [{
      properties: {
        page: {
          margin: {
            top: pageMargins.top,
            right: pageMargins.right,
            bottom: pageMargins.bottom,
            left: pageMargins.left,
            header: headerDistance,
          },
          // docx-npm's createPageSize SWAPS width↔height when orientation is
          // LANDSCAPE (see node_modules/docx/dist/index.cjs createPageSize).
          // To get landscape Letter (11"×8.5" = w15840×h12240 twips) on the
          // wire we must pass {width: 12240, height: 15840, orientation:
          // LANDSCAPE} — the library does the swap for us. Passing landscape
          // dimensions directly results in PORTRAIT-shaped output with the
          // orient flag set, which Word renders as portrait.
          size: options.orientation === 'landscape'
            ? { width: 12240, height: 15840, orientation: PageOrientation.LANDSCAPE }
            : { width: 12240, height: 15840, orientation: PageOrientation.PORTRAIT },
        },
      },
      headers, footers,
      children: paragraphs
    }],
  });

  let buffer = await Packer.toBuffer(document);

  // Canonical-on-disk: post-Packer, inject w:sdt block-level wrappers
  // around each recorded marker region. This is a string-rewrite over
  // word/document.xml — see injectPythoSdtWrappers for the algorithm.
  // Done BEFORE the watermark step so the watermark's headerN.xml is
  // not affected.
  try {
    buffer = await injectPythoSdtWrappers(buffer, _pythoMarkerStream);
  } catch (e) {
    // Marker injection is best-effort — fall back to no-markers DOCX
    // rather than blocking the export on a marker-side bug. The
    // canonical-on-disk importer reads markers when present and falls
    // back to schema_state.json otherwise.
    console.error('pytho marker injection failed: ' + (e && e.message));
  }

  if (watermark && watermark !== 'none') {
    buffer = await addWatermarkToDocx(buffer, watermark, watermarkColor, watermarkFont, watermarkSize);
  }

  return buffer;
}

// Main: read JSON from stdin, convert, write to outputPath
async function main() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const input = JSON.parse(Buffer.concat(chunks).toString());

  const buffer = await htmlToDocx(input.html, {
    header: input.header,
    headerStyle: input.headerStyle,
    footerText: input.footerText,
    footerStyle: input.footerStyle,
    watermark: input.watermark,
    watermarkColor: input.watermarkColor,
    watermarkFont: input.watermarkFont,
    watermarkSize: input.watermarkSize,
    defaultFont: input.defaultFont,
    orientation: input.orientation,
    imageBaseDir: input.imageBaseDir,
  });

  fs.writeFileSync(input.outputPath, buffer);
  console.log(JSON.stringify({ success: true, path: input.outputPath, size: buffer.length }));
}

main().catch(err => {
  console.error(JSON.stringify({ success: false, error: err.message }));
  process.exit(1);
});
