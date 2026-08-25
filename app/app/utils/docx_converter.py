import json
import subprocess
import os

CONVERTER_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'docx-converter')
CONVERTER_SCRIPT = os.path.join(CONVERTER_DIR, 'convert.js')


def html_to_docx(html, output_path, header=None, header_style=None, watermark=None, watermark_color=None, watermark_font=None, watermark_size=None, page_margins=None, orientation=None):
    """Convert HTML to DOCX using the Node.js converter (same code as pytho-vite).

    page_margins (optional dict, values in twips):
        { 'top': int, 'right': int, 'bottom': int, 'left': int }
    orientation (optional str): 'portrait' | 'landscape'. When None, convert.js
        falls back to whatever the embedded pytho-doc-settings script declares,
        and finally to portrait. Pass through from the DocEditor save path so
        landscape products round-trip with the right paper size.
    """
    payload = {
        'html': html,
        'outputPath': os.path.abspath(output_path),
        'header': header,
        'headerStyle': header_style,
        'watermark': watermark,
        'watermarkColor': watermark_color,
        'watermarkFont': watermark_font,
        'watermarkSize': watermark_size,
        'pageMargins': page_margins,
        'orientation': orientation,
    }

    result = subprocess.run(
        ['node', CONVERTER_SCRIPT],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=CONVERTER_DIR,
    )

    if result.returncode != 0:
        raise RuntimeError(f'DOCX conversion failed: {result.stderr}')

    response = json.loads(result.stdout.strip().split('\n')[-1])
    if not response.get('success'):
        raise RuntimeError(f'DOCX conversion error: {response.get("error")}')

    return output_path
