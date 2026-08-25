import requests
import threading
from pathlib import Path
import logging
import base64
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from utils import config

logger = logging.getLogger(__name__)

# Serialize access to the Stability API (single-request-at-a-time)
_sd_api_lock = threading.Lock()

# HARDCODED SETTINGS
API_URL = config.Config.IMAGE_API()
IMAGE_WIDTH = 384
IMAGE_HEIGHT = 384
# Facebook post frame: wide landscape to match chat UI (max-w ~660px)
POST_IMAGE_WIDTH = 512
POST_IMAGE_HEIGHT = 288
NUM_INFERENCE_STEPS = 15
GUIDANCE_SCALE = 7.5
NEGATIVE_PROMPT = "bad anatomy, signature, watermark, username, error, missing limbs, extra limbs, error, lowres, low quality, blurry"


def generate_image(prompt: str):
    """
    Generate an image using the local Stable Diffusion WebUI API.

    Args:
        prompt (str): The input text prompt to generate the image.
    
    Returns:
        str: HTML string with the prompt and image element.
    """
    filename = str(random.randint(0, 9999)) + ".png"
    
    image_file_path = os.path.join(config.Config.UPLOAD_DIR(), filename)
    image_file_url = f'/files/{filename}'
    
    try:
        payload = {
            "prompt": prompt,
            "negative_prompt": NEGATIVE_PROMPT,
            "width": IMAGE_WIDTH,
            "height": IMAGE_HEIGHT,
            "steps": NUM_INFERENCE_STEPS,
            "cfg_scale": GUIDANCE_SCALE,
            "save_images": True,
            "do_not_save_samples": False,
            "do_not_save_grid": True
        }
        
        logger.info(f"Generating image for prompt: '{prompt[:60]}...'")
        _sd_api_lock.acquire()
        try:
            response = requests.post(API_URL, json=payload)
            response.raise_for_status()
            data = response.json()
        finally:
            _sd_api_lock.release()

        # Extract first image from API (base64)
        if "images" in data and data["images"]:
            image_b64 = data["images"][0]
            image_bytes = base64.b64decode(image_b64)
            
            with open(image_file_path, "wb") as f:
                f.write(image_bytes)
            
            logger.info(f"Image saved to {image_file_path}")
            
            return f"""<span class="font-bold">Prompt</span><br/>{prompt}<br/>
<img src="{image_file_url}" alt="Generated image" style="max-width: 100%; height: auto;">"""
        else:
            raise ValueError("No image returned from API.")
            
    except Exception as e:
        logger.error(f"Failed to generate image: {e}")
        return f"""<span class="font-bold">Error</span><br/>Failed to generate image: {str(e)}"""


def generate_image_url(
    prompt: str,
    timeout: int = 300,
    width: int | None = None,
    height: int | None = None,
) -> str | None:
    """
    Generate an image using the local Stable Diffusion WebUI API and return
    only the URL for use in Facebook posts (or other flows that need a media URL).

    Args:
        prompt (str): The input text prompt to generate the image.
        timeout (int): Request timeout in seconds for the SD API call.
        width (int | None): Image width (default IMAGE_WIDTH). Use POST_IMAGE_WIDTH for post frame.
        height (int | None): Image height (default IMAGE_HEIGHT). Use POST_IMAGE_HEIGHT for post frame.

    Returns:
        str | None: Relative URL like /files/{filename}.png on success, None on failure.
    """
    prompt = (prompt or "").strip()
    if not prompt:
        logger.warning("generate_image_url called with empty prompt, skipping")
        return None
    # SD works best with short prompts; long post text can be truncated
    prompt_for_sd = prompt[:500] if len(prompt) > 500 else prompt

    w = width if width is not None else IMAGE_WIDTH
    h = height if height is not None else IMAGE_HEIGHT

    filename = str(random.randint(0, 999999)) + ".png"
    image_file_path = os.path.join(config.Config.UPLOAD_DIR(), filename)

    try:
        payload = {
            "prompt": prompt_for_sd,
            "negative_prompt": NEGATIVE_PROMPT,
            "width": w,
            "height": h,
            "steps": NUM_INFERENCE_STEPS,
            "cfg_scale": GUIDANCE_SCALE,
            "save_images": True,
            "do_not_save_samples": False,
            "do_not_save_grid": True,
        }
        logger.info(f"Generating image via {API_URL} for prompt: '{prompt_for_sd[:80]}...'")
        _sd_api_lock.acquire()
        try:
            response = requests.post(API_URL, json=payload, timeout=timeout)
            response.raise_for_status()
            data = response.json()
        finally:
            _sd_api_lock.release()

        if "images" in data and data["images"]:
            image_b64 = data["images"][0]
            image_bytes = base64.b64decode(image_b64)
            with open(image_file_path, "wb") as f:
                f.write(image_bytes)
            logger.info(f"Image saved to {image_file_path}")
            return f"/files/{filename}"
        logger.error("SD API response missing 'images' key or empty; keys: %s", list(data.keys()) if isinstance(data, dict) else "not a dict")
        return None
    except Exception as e:
        logger.exception("Failed to generate image URL: %s", e)
        return None


def generate_image_urls(
    prompts: list[str],
    timeout: int = 300,
    max_workers: int = 5,
    width: int | None = None,
    height: int | None = None,
) -> list[str | None]:
    """
    Generate images for multiple prompts in parallel; returns URLs in same order as prompts.

    Args:
        prompts: List of text prompts (one per image).
        timeout: Request timeout in seconds per SD API call (use higher when N is large).
        max_workers: Max concurrent threads.
        width: Image width (default IMAGE_WIDTH). Use POST_IMAGE_* for Facebook post frame.
        height: Image height (default IMAGE_HEIGHT).

    Returns:
        List of relative URLs like /files/{filename}.png, or None for failures, in prompt order.
    """
    n = len(prompts)
    if n == 0:
        return []
    results: list[str | None] = [None] * n
    workers = min(max_workers, n)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_idx = {
            executor.submit(generate_image_url, p, timeout, width, height): i
            for i, p in enumerate(prompts)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                logger.warning("Image generation failed for prompt index %s: %s", idx, e)
                results[idx] = None
    return results