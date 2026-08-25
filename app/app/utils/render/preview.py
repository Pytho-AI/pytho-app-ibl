"""Playwright-based HTML-side preview renderer for analyst artifacts.

Mirrors utils/render/headless.py (the map renderer) — same proxy-app URL
base, same launch args, same concurrency Semaphore (shared, in fact). The
React route this drives is `pytho-app/ui/src/pages/DocEditorHeadless.tsx`,
which mounts the analyst's <DocEditor> with the artifact loaded out of
/files/, lets TinyMCE finish all its layout passes, then flips
`window.__previewReady = true`.

The output PNG (`preview.png` beside the artifact) is the WYSIWYG
counterpart to the per-page DOCX/PPTX renders from utils/render/doc_pages.py.
Side-by-side: preview.png shows what the analyst sees in the editor, and
page-NN.png shows what the file ends up looking like in Word/PowerPoint
after the convert.js / python-pptx round-trip. Drift between the two is
a regression caught by eye in one glance.

Run order matters: the artifact's HTML must already exist at the
/files/<owner>/<artifact_id>/artifact.html path before this is called —
the headless route fetches it. So this runs AFTER
render_artifact_html + html_path.write_text in render_artifact_files.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Reuse the same base as the map renderer so the network hop only has
# to be configured in one place. Everything runs on the same spark
# host; preview render goes through proxy-app's published 443 on
# localhost (self-signed cert; new_context passes ignore_https_errors).
_PYTHO_APP_BASE = os.environ.get("PYTHO_APP_INTERNAL_BASE", "https://localhost")

# Hard ceiling on how long we'll wait for the React app to load + TinyMCE
# to lay out the document. 45s is generous for a 4264px-tall INTSUM with
# images; bigger products may need a bump.
_PREVIEW_TIMEOUT_MS = int(os.environ.get("PREVIEW_RENDER_TIMEOUT_MS", "60000"))

# Same Semaphore as the map renderer so we never exceed the docker-api
# memory budget by stacking parallel Chromium browsers.
try:
    from utils.render.headless import _CHROMIUM_SEMAPHORE  # type: ignore[attr-defined]
except Exception:  # pragma: no cover — defensive in case of import-order quirks
    _CHROMIUM_SEMAPHORE = asyncio.Semaphore(
        int(os.environ.get("RENDER_CONCURRENCY", "4"))
    )


async def render_preview_to_png(owner: str, artifact_id: str,
                                  out_path: str | Path,
                                  orientation: str = "portrait") -> bool:
    """Render the analyst DocEditor view of `artifact_id` to a long PNG.

    Returns True on success (file written), False on failure. Never raises
    — callers should treat this as best-effort and not block the artifact
    emit if it returns False.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("render_preview_to_png: playwright not installed")
        return False

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    base = _PYTHO_APP_BASE.rstrip("/")
    url = (
        f"{base}/missioncommand/docs/render"
        f"?owner={owner}&artifact_id={artifact_id}&orientation={orientation}"
    )

    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    # Letter portrait at 96 DPI is 816px wide. Landscape is 1056. Give
    # ~50px headroom for the TinyMCE chrome (the toolbar lives OUTSIDE the
    # iframe so it doesn't affect the body capture, but the iframe sits
    # below the toolbar in the page layout). Initial viewport height is
    # arbitrary — we resize after layout settles to match scrollHeight.
    init_width = 1100 if orientation == "portrait" else 1340
    init_height = 1200

    async with _CHROMIUM_SEMAPHORE:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(args=launch_args)
            try:
                ctx = await browser.new_context(
                    viewport={"width": init_width, "height": init_height},
                    device_scale_factor=1.5,
                    # Internal-CA self-signed cert on https://10.0.1.30
                    # / https://172.17.0.1 — without ignore_https_errors
                    # Chromium aborts the navigation at the TLS handshake
                    # and the preview.png is silently empty.
                    ignore_https_errors=True,
                )
                page = await ctx.new_page()
                page.on("pageerror", lambda err: logger.warning("preview-pageerror: %s", err))
                page.on(
                    "console",
                    lambda msg: msg.type in ("error", "warning")
                    and logger.warning("preview-console[%s]: %s", msg.type, msg.text),
                )
                await page.goto(url, wait_until="domcontentloaded",
                                  timeout=_PREVIEW_TIMEOUT_MS)
                # DocEditorHeadless flips this once TinyMCE's iframe body
                # has reported a stable scrollHeight across N consecutive
                # rAF cycles + font ready + image-load barriers + a 2s
                # minimum dwell. See pages/DocEditorHeadless.tsx for the
                # full stability heuristic.
                await page.wait_for_function(
                    "window.__previewReady === true",
                    timeout=_PREVIEW_TIMEOUT_MS,
                )
                # Belt-and-suspenders: even after the latch fires, give
                # the renderer one more frame to commit any layout that
                # was queued during the wait_for_function transition.
                await page.wait_for_timeout(250)
                # Playwright bug microsoft/playwright#12685: elementHandle
                # .screenshot() truncates at viewport for elements taller
                # than the viewport (regression since 1.17.2). For a
                # multi-page DocEditor (e.g. MCOO landscape at ~6 pages
                # = 4300+ CSS px) this silently caps preview.png at the
                # 1200px init_height, leaving everything past page 1
                # rendered as the iframe's transparent area showing the
                # outer page bg.
                #
                # Workaround per the issue thread + docs: resize viewport
                # to the iframe's content height BEFORE the screenshot.
                # DocEditorHeadless stashes window.__previewIframeHeight
                # at stability — read that, set the viewport to it (+
                # outer chrome), and only THEN take the element screenshot.
                iframe_el = await page.query_selector(".tox-edit-area__iframe")
                if iframe_el is None:
                    logger.warning("render_preview_to_png: TinyMCE iframe not found at %s", url)
                    return False
                try:
                    iframe_h = await page.evaluate(
                        "() => Math.ceil((window).__previewIframeHeight || 0)"
                    )
                except Exception:
                    iframe_h = 0
                iframe_h = int(iframe_h or 0)
                if iframe_h > 0:
                    # +200 px headroom for the outer page's TinyMCE chrome
                    # (toolbar / status bar) so the iframe element sits
                    # fully inside the resized viewport.
                    target_h = max(init_height, iframe_h + 200)
                    await page.set_viewport_size(
                        {"width": init_width, "height": target_h}
                    )
                    # Settle one frame after the resize so layout reflows
                    # before capture.
                    await page.wait_for_timeout(150)
                await iframe_el.screenshot(path=str(out_path), type="png")
                return True
            except Exception as e:
                logger.warning("render_preview_to_png: failed for %s/%s: %s",
                               owner, artifact_id, e)
                return False
            finally:
                await browser.close()


def render_preview_to_png_sync(owner: str, artifact_id: str,
                                 out_path: str | Path,
                                 orientation: str = "portrait") -> bool:
    """Sync convenience wrapper for callers outside an asyncio loop.
    Spawns a fresh event loop — fine for one-shot artifact emits, not
    for tight call loops.
    """
    try:
        return asyncio.run(
            render_preview_to_png(owner, artifact_id, out_path, orientation=orientation)
        )
    except Exception as e:
        logger.warning("render_preview_to_png_sync: %s", e)
        return False
