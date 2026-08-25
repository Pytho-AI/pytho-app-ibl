import pypandoc
import fitz

from bs4 import BeautifulSoup

def keep_only_html(html):
    soup = BeautifulSoup(html, "html5lib")
    body = soup.body or soup
    return f"""<!DOCTYPE html>
<html lang="en">
<body>
{''.join(str(c) for c in body.contents if c.name)}
</body>
"""

def docx_to_html(docx_path, html_path):
    output = pypandoc.convert_file(docx_path, 'html', format='docx')
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(output)

def pdf_to_html(input_pdf_path, output_html_path):
    """
    Convert a PDF into simple HTML with no attributes or styles.
    Each PDF block becomes a <p> element.
    """
    doc = fitz.open(input_pdf_path)
    html_lines = []

    for page in doc:
        blocks = page.get_text("blocks")  # (x0, y0, x1, y1, text, block_no, ...)
        for block in blocks:
            text = block[4].strip()
            if text:
                # Escape HTML special chars
                safe_text = (
                    text.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;")
                )
                html_lines.append(f"<p>{safe_text}</p>")

    clean_html = "<html><body>\n" + "\n".join(html_lines) + "\n</body></html>"

    with open(output_html_path, "w", encoding="utf-8") as f:
        f.write(clean_html)

    return output_html_path
