"""Product types CRUD endpoints."""
import os
import time
from typing import Optional
from fastapi import APIRouter, HTTPException, status, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from utils.database import get_db, ProductType
from utils import events, parser
from handlers import product

UPLOAD_DIR = "uploads"

router = APIRouter()


DOCTRINES_BUILTIN = {
    "productTypeId": "builtin-doctrines",
    "userId": "__system__",
    "email": None,
    "productName": "Doctrines",
    "productOverview": "Answer from the doctrinal reference library.",
    "file": None,
    "createdAt": 0,
    "updatedAt": 0,
    "isAgentic": True,
    "isBuiltin": True,
    "template": {"type": "builtin"},
}


# Schema-backed built-in product types — the 15 doctrinal intel artifacts in
# `templates/products/`. Frontend dispatches these via `/ws/intel_agent` (the
# template.type tells the UI which WebSocket route to use), or via the
# manual-dispatch path `POST /api/products/generate` from the Generate-button
# UI in ProductLibraryModal.
#
# Surface every metadata field the product card UI needs to render the card +
# run prerequisite checks before generation:
#   - productOverview         doctrinal citation (used as the card subtitle).
#   - purpose                 one-sentence doctrine.purpose body for the card.
#   - shape                   paragraph / row / matrix / structured (rendering hint).
#   - output_format           singular "docx" or "pptx". Determines orientation
#                             (docx → portrait 8.5×11, pptx → landscape 16:9) AND
#                             which export link the card surfaces. One source of
#                             truth; orientation is no longer a separate field.
#   - consumes_artifacts      upstream dependency list. The Generate button uses
#                             this to flag a prerequisite warning when those
#                             upstream artifacts aren't present in the project's
#                             registry yet (HVTL needs SITEMP, Threat Overlay, Threat COA Statement).
def _intel_agent_builtins():
    from templates import PRODUCT_SCHEMAS
    from utils.exporter import PRODUCT_LONG_NAMES, PRODUCT_CATEGORIES
    builtins = []
    for name, schema in PRODUCT_SCHEMAS.items():
        cat_entry = PRODUCT_CATEGORIES.get(name)
        category, category_order, step_label = (
            cat_entry if cat_entry else (None, 0, None)
        )
        doctrine = schema.get("doctrine") or {}
        output_format = schema.get("output_format") or "docx"
        orientation = "landscape" if output_format == "pptx" else "portrait"
        # output_formats (plural) declares the full set the render pipeline
        # emits. For the 15 XLSX-canonical products this is ["xlsx"]; the
        # singular output_format remains the LEGACY format kept around for
        # DISABLE_XLSX fallback. Frontend uses output_formats to decide
        # whether to surface an XLSX badge / route the open click to
        # SheetEditor.
        output_formats = list(schema.get("output_formats") or [output_format])
        legacy_formats = list(schema.get("legacy_formats") or [])
        builtins.append({
            "productTypeId": f"builtin-{name.lower()}",
            "userId": "__system__",
            "email": None,
            "productName": name,
            "productLongName": PRODUCT_LONG_NAMES.get(name, name),
            "productOverview": doctrine.get("citation", "") or "",
            "purpose": doctrine.get("purpose", "") or "",
            "shape": schema.get("shape", "") or "",
            "output_format": output_format,
            "output_formats": output_formats,
            "legacy_formats": legacy_formats,
            "page_orientation": orientation,
            "consumes_artifacts": list(schema.get("consumes_artifacts") or []),
            "file": None,
            "createdAt": 0,
            "updatedAt": 0,
            "isAgentic": True,
            "isBuiltin": True,
            "template": {"type": "intel_agent", "productKey": name},
            "category": category,
            "categoryOrder": category_order,
            "stepLabel": step_label,
        })
    return builtins


class FileInfo(BaseModel):
    key: str
    url: Optional[str] = None
    name: Optional[str] = None


class CreateProductTypeRequest(BaseModel):
    productTypeId: str
    userId: str
    email: Optional[str] = None
    productName: str
    productOverview: Optional[str] = ""
    file: FileInfo


class UpdateProductTypeRequest(BaseModel):
    productName: str
    productOverview: Optional[str] = ""
    file: Optional[FileInfo] = None
    userId: str


def product_to_dict(pt: ProductType) -> dict:
    result = {
        "productTypeId": pt.product_type_id,
        "userId": pt.user_id,
        "email": pt.email,
        "productName": pt.product_name,
        "productOverview": pt.product_overview,
        "file": {"key": pt.file_key, "name": pt.file_name} if pt.file_key else None,
        "createdAt": pt.created_at,
        "updatedAt": pt.updated_at,
        "isAgentic": True,
        "template": {"type": "custom"},
        # Analyst-uploaded product types land in a "Custom" section below
        # the canonical doctrinal groups. The frontend appends any
        # category not in PRODUCT_CATEGORY_ORDER in insertion order — so
        # one "Custom" section, sorted by createdAt via categoryOrder.
        "category": "Custom",
        "categoryOrder": pt.created_at or 0,
        "stepLabel": None,
    }
    if pt.file_key:
        result["template_path"] = os.path.join(UPLOAD_DIR, pt.file_key + ".html")
        result["template_body_path"] = os.path.join(UPLOAD_DIR, pt.file_key + "_body.html")
    return result


def _generate_template_files(file_key: str):
    """Convert uploaded PDF/DOCX to HTML and generate the body template."""
    template_path = os.path.join(UPLOAD_DIR, file_key)
    template_html_path = os.path.join(UPLOAD_DIR, file_key + ".html")
    template_body_path = os.path.join(UPLOAD_DIR, file_key + "_body.html")

    if template_path.endswith(".pdf"):
        parser.pdf_to_html(template_path, template_html_path)
    else:
        parser.docx_to_html(template_path, template_html_path)

    product.generate_template({
        "template_path": template_html_path,
        "template_body_path": template_body_path,
    })


@router.get("/product_types")
def list_product_types(db: Session = Depends(get_db)):
    products = db.query(ProductType).order_by(ProductType.product_name).all()
    return [DOCTRINES_BUILTIN] + _intel_agent_builtins() + [product_to_dict(pt) for pt in products]


@router.get("/product_types/{product_type_id}")
def get_product_type(product_type_id: str, db: Session = Depends(get_db)):
    pt = db.query(ProductType).filter(ProductType.product_type_id == product_type_id).first()
    if not pt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found")
    return product_to_dict(pt)


@router.post("/product_types")
def create_product_type(req: CreateProductTypeRequest, db: Session = Depends(get_db)):
    _generate_template_files(req.file.key)

    pt = ProductType(
        product_type_id=req.productTypeId,
        user_id=req.userId,
        email=req.email,
        product_name=req.productName,
        product_overview=req.productOverview or "",
        file_key=req.file.key,
        file_name=req.file.name,
        created_at=int(time.time() * 1000),
    )

    db.add(pt)
    db.commit()
    db.refresh(pt)

    events.log_event(req.userId, "product_type", f"Created product template {req.productName}")
    return product_to_dict(pt)


@router.put("/product_types/{product_type_id}")
def update_product_type(product_type_id: str, req: UpdateProductTypeRequest, db: Session = Depends(get_db)):
    pt = db.query(ProductType).filter(ProductType.product_type_id == product_type_id).first()
    if not pt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found")

    if pt.user_id != req.userId:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to edit this product type")

    pt.product_name = req.productName
    pt.product_overview = req.productOverview or ""

    if req.file:
        _generate_template_files(req.file.key)
        pt.file_key = req.file.key
        pt.file_name = req.file.name

    pt.updated_at = int(time.time() * 1000)

    db.commit()
    db.refresh(pt)

    return product_to_dict(pt)


@router.delete("/product_types/{product_type_id}")
def delete_product_type(product_type_id: str, user_id: str, db: Session = Depends(get_db)):
    pt = db.query(ProductType).filter(ProductType.product_type_id == product_type_id).first()
    if not pt:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product type not found")

    if pt.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to delete this product type")

    db.delete(pt)
    db.commit()

    return {"success": True, "productTypeId": product_type_id}
