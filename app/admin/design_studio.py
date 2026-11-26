from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from typing import cast
from urllib.parse import urlsplit

from flask import Blueprint, Response, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import AdminUser, DesignStudioRole, Product, StorefrontDesign, StorefrontDesignAudit, StorefrontDesignRevision
from app.services.design_studio import (
    DEFAULT_OFFERS_TEMPLATE, ROLE_NAMES, audit, parse_lottie_animation, product_api_data, role_for,
    save_revision, schema_ready, snapshot_design, validate_design_payload,
)
from app.services.product_discounts import current_discounted_products
from app.services.product_images import ImageStorageError
from app.services.showcase_media import inspect_media_upload, store_showcase_upload

design_studio_bp = Blueprint("design_studio", __name__, url_prefix="/design-studio")
design_public_bp = Blueprint("design_public", __name__, url_prefix="/design-studio")
EDIT_ROLES = {"owner", "designer", "editor"}


@design_studio_bp.before_request
def require_admin_identity():
    if current_user.is_authenticated:
        _current_admin_or_403()


def _current_admin_or_403() -> AdminUser:
    admin = current_user._get_current_object()
    if not isinstance(admin, AdminUser):
        abort(403)
    return admin


def _role_or_403(*roles):
    if not schema_ready():
        abort(503, description="Design Studio schema is unavailable; run migrate-design-studio-schema.")
    if role_for(_current_admin_or_403()) not in roles:
        abort(403)


def _design_or_404(design_id: int) -> StorefrontDesign:
    design = db.session.get(StorefrontDesign, design_id)
    if not design:
        abort(404)
    return design


def _apply_payload(design, payload, admin, *, new=False):
    for key, value in payload.items():
        if key == "product_ids":
            value = json.dumps(value, separators=(",", ":"))
        elif key == "motion_config":
            value = json.dumps(value, separators=(",", ":"), sort_keys=True)
        if key in {"name", "title", "description", "placement", "html", "css", "javascript", "lottie_json", "product_ids", "template_key", "motion_preset", "motion_config", "starts_at", "expires_at"}:
            setattr(design, key, value)
    design.updated_by_id = admin.id
    design.version = 1 if new else design.version + 1


def _editor_defaults():
    defaults = dict(DEFAULT_OFFERS_TEMPLATE)
    defaults["product_ids"] = [product.id for product in current_discounted_products()]
    defaults["motion_config"] = json.dumps(defaults["motion_config"], separators=(",", ":"), sort_keys=True)
    return defaults


def _payload_from_form(*, allow_empty_name=False):
    form_data = cast(dict[str, object], request.form.to_dict())
    form_data["product_ids"] = request.form.getlist("product_ids")
    if allow_empty_name and not str(form_data.get("name", "")).strip():
        form_data["name"] = "Unsaved design preview"
    raw_motion_config = form_data.get("motion_config", "{}")
    if not isinstance(raw_motion_config, str):
        raise ValueError("Motion settings must be valid JSON.")
    try:
        form_data["motion_config"] = json.loads(raw_motion_config)
    except json.JSONDecodeError as exc:
        raise ValueError("Motion settings must be valid JSON.") from exc
    return validate_design_payload(form_data)


@design_studio_bp.route("")
@login_required
def index():
    if not schema_ready():
        return render_template("admin/design_studio.html", unavailable=True, designs=[], products=[], role=None, users=[], audit_events=[], revisions={})
    role = role_for(_current_admin_or_403())
    if role not in ROLE_NAMES:
        abort(403)
    designs = StorefrontDesign.query.order_by(StorefrontDesign.updated_at.desc(), StorefrontDesign.id.desc()).all()
    revisions = {}
    for revision in StorefrontDesignRevision.query.order_by(StorefrontDesignRevision.created_at.desc()).limit(100).all():
        revisions.setdefault(revision.design_id, []).append(revision)
    audit_events = StorefrontDesignAudit.query.order_by(StorefrontDesignAudit.created_at.desc()).limit(50).all()
    users = AdminUser.query.order_by(AdminUser.id).all() if role == "owner" else []
    assignments = {
        assignment.admin_user_id: assignment.role
        for assignment in DesignStudioRole.query.all()
    } if role == "owner" else {}
    return render_template(
        "admin/design_studio.html", unavailable=False, designs=designs,
        products=Product.query.filter_by(active=True).order_by(Product.name).all(),
        role=role, users=users, assignments=assignments, audit_events=audit_events, revisions=revisions,
        roles=sorted(ROLE_NAMES),
    )


@design_studio_bp.route("/new")
@login_required
def new():
    _role_or_403(*EDIT_ROLES)
    products = Product.query.filter_by(active=True).order_by(Product.name).all()
    return render_template(
        "admin/design_studio.html", unavailable=False, editor=True, design=None,
        products=products, role=role_for(_current_admin_or_403()), designs=[],
        users=[], audit_events=[], revisions={}, default_template=_editor_defaults(),
    )


@design_studio_bp.route("/<int:design_id>/edit")
@login_required
def edit(design_id):
    _role_or_403(*EDIT_ROLES)
    design = _design_or_404(design_id)
    return render_template(
        "admin/design_studio.html", unavailable=False, editor=True, design=design,
        products=Product.query.filter_by(active=True).order_by(Product.name).all(),
        role=role_for(_current_admin_or_403()), designs=[], users=[],
        audit_events=[], revisions={}, default_template=_editor_defaults(),
    )


@design_studio_bp.post("/save")
@login_required
def save():
    _role_or_403(*EDIT_ROLES)
    admin = _current_admin_or_403()
    raw_id = request.form.get("design_id", "").strip()
    if raw_id and not raw_id.isdigit():
        abort(400, description="The design identifier is invalid.")
    design = _design_or_404(int(raw_id)) if raw_id else None
    if design and design.status == "published" and role_for(admin) != "owner":
        abort(403, description="Only the Design Studio owner can directly edit a published design.")
    try:
        payload = _payload_from_form()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("design_studio.edit", design_id=design.id) if design else url_for("design_studio.new"))
    is_new = design is None
    if design is None:
        design = StorefrontDesign(name=payload["name"], created_by_id=admin.id)
        db.session.add(design)
        db.session.flush()
    _apply_payload(design, payload, admin, new=is_new)
    if not is_new and design.status != "draft":
        design.status = "draft"
        design.approved_by_id = None
        design.published_by_id = None
    db.session.flush()
    save_revision(design, admin, "created" if is_new else "edited")
    db.session.commit()
    flash("Design saved as a draft.", "success")
    return redirect(url_for("design_studio.edit", design_id=design.id))


@design_studio_bp.post("/<int:design_id>/workflow")
@login_required
def workflow(design_id):
    design = _design_or_404(design_id)
    admin = _current_admin_or_403()
    action = request.form.get("action", "")
    if action == "submit":
        _role_or_403(*EDIT_ROLES)
        if design.status not in {"draft", "approved"}:
            abort(409)
        design.status = "pending"
    elif action == "approve":
        _role_or_403("owner", "approver")
        if design.status != "pending":
            abort(409)
        design.status = "approved"
        design.approved_by_id = admin.id
    elif action == "publish":
        _role_or_403("owner", "publisher")
        if design.status != "approved":
            abort(409, description="A design must be approved before publishing.")
        if not design.html.strip():
            abort(400, description="A design needs content before it can be published.")
        design.status = "published"
        design.published_by_id = admin.id
    elif action == "unpublish":
        _role_or_403("owner", "publisher")
        design.status = "approved"
        design.published_by_id = None
    elif action == "disable":
        _role_or_403("owner", "publisher")
        design.is_enabled = False
    elif action == "enable":
        _role_or_403("owner", "publisher")
        design.is_enabled = True
    elif action == "duplicate":
        _role_or_403(*EDIT_ROLES)
        snapshot = snapshot_design(design)
        copy = StorefrontDesign(
            name=snapshot["name"], title=snapshot["title"], description=snapshot["description"],
            placement=snapshot["placement"], status="draft", is_enabled=True,
            html=snapshot["html"], css=snapshot["css"], javascript=snapshot["javascript"],
            lottie_json=snapshot["lottie_json"],
            product_ids=json.dumps(snapshot["product_ids"], separators=(",", ":")),
            template_key=snapshot["template_key"], motion_preset=snapshot["motion_preset"],
            motion_config=json.dumps(snapshot["motion_config"], separators=(",", ":"), sort_keys=True),
        )
        copy.name = f"{design.name} copy"[:120]
        copy.created_by_id = admin.id
        copy.updated_by_id = admin.id
        copy.version = 1
        db.session.add(copy)
        db.session.flush()
        save_revision(copy, admin, "duplicated")
        db.session.commit()
        flash("A reusable draft copy was created.", "success")
        return redirect(url_for("design_studio.edit", design_id=copy.id))
    else:
        abort(400)
    audit(action, design, {"status": design.status, "enabled": design.is_enabled}, admin)
    db.session.commit()
    flash("Design workflow updated.", "success")
    return redirect(url_for("design_studio.index"))


@design_studio_bp.post("/<int:design_id>/rollback/<int:revision_id>")
@login_required
def rollback(design_id, revision_id):
    _role_or_403(*EDIT_ROLES)
    design = _design_or_404(design_id)
    revision = StorefrontDesignRevision.query.filter_by(id=revision_id, design_id=design.id).first_or_404()
    values = json.loads(revision.snapshot)
    try:
        payload = validate_design_payload(values)
    except ValueError as exc:
        abort(400, description=f"Revision cannot be restored: {exc}")
    admin = _current_admin_or_403()
    design.status = "draft"
    design.approved_by_id = None
    design.published_by_id = None
    _apply_payload(design, payload, admin)
    db.session.flush()
    save_revision(design, admin, "rolled_back")
    db.session.commit()
    flash("Revision restored as a draft. Submit it for review before republishing.", "success")
    return redirect(url_for("design_studio.edit", design_id=design.id))


@design_studio_bp.route("/<int:design_id>/export")
@login_required
def export_design(design_id):
    _role_or_403(*EDIT_ROLES, "approver", "publisher", "viewer")
    design = _design_or_404(design_id)
    body = json.dumps({"format": "fitystores-design-studio-v1", "design": snapshot_design(design)}, ensure_ascii=False, indent=2)
    audit("exported", design, {"version": design.version}, _current_admin_or_403())
    db.session.commit()
    return Response(body, mimetype="application/json", headers={"Content-Disposition": f'attachment; filename="design-{design.id}.json"'})


@design_studio_bp.post("/import")
@login_required
def import_design():
    _role_or_403(*EDIT_ROLES)
    raw_import = request.form.get("import_json", "")
    if len(raw_import.encode("utf-8")) > 60_000:
        flash("Design imports are limited to 60 KB.", "error")
        return redirect(url_for("design_studio.index"))
    try:
        document = json.loads(raw_import)
        if not isinstance(document, dict) or document.get("format") != "fitystores-design-studio-v1":
            raise ValueError("This is not a supported Design Studio export.")
        payload = validate_design_payload(document.get("design"))
    except (ValueError, json.JSONDecodeError) as exc:
        flash(str(exc), "error")
        return redirect(url_for("design_studio.index"))
    admin = _current_admin_or_403()
    design = StorefrontDesign(name=payload["name"], created_by_id=admin.id)
    db.session.add(design)
    db.session.flush()
    _apply_payload(design, payload, admin, new=True)
    save_revision(design, admin, "imported")
    db.session.commit()
    flash("Imported as an unpublished draft.", "success")
    return redirect(url_for("design_studio.edit", design_id=design.id))


@design_studio_bp.post("/roles/<int:admin_id>")
@login_required
def assign_role(admin_id):
    _role_or_403("owner")
    target = db.session.get(AdminUser, admin_id)
    role = request.form.get("role", "")
    if not target or role not in ROLE_NAMES:
        abort(400)
    assignment = DesignStudioRole.query.filter_by(admin_user_id=target.id).first()
    if assignment is None:
        assignment = DesignStudioRole(admin_user_id=target.id)
        db.session.add(assignment)
    if assignment.role == "owner" and role != "owner":
        owner_count = DesignStudioRole.query.filter_by(role="owner").count()
        if owner_count <= 1:
            abort(409, description="Assign another owner before changing the only owner.")
    assignment.role = role
    audit("role_assigned", details={"admin_user_id": target.id, "role": role}, admin=_current_admin_or_403())
    db.session.commit()
    flash("Design Studio role updated.", "success")
    return redirect(url_for("design_studio.index"))


@design_studio_bp.post("/assets/upload")
@login_required
def upload_asset():
    _role_or_403(*EDIT_ROLES)
    uploaded = request.files.get("media")
    if not uploaded or not uploaded.filename:
        abort(400, description="Choose an image, GIF, or video file.")
    data = uploaded.stream.read(3_500_001)
    try:
        info = inspect_media_upload(Path(uploaded.filename).name, data)
        stored = store_showcase_upload(Path(uploaded.filename).name, data, info)
    except ValueError as exc:
        abort(400, description=str(exc))
    except ImageStorageError as exc:
        current_app.logger.warning("Design Studio asset upload unavailable: %s", str(exc))
        abort(503, description=str(exc))
    return {"ok": True, **stored, **info}


@design_studio_bp.route("/<int:design_id>/frame")
@login_required
def preview_frame(design_id):
    _role_or_403(*EDIT_ROLES, "approver", "publisher", "viewer")
    design = _design_or_404(design_id)
    return _design_response(design)


@design_studio_bp.route("/preview", methods=["GET", "POST"])
@login_required
def preview():
    _role_or_403(*EDIT_ROLES)
    if request.method == "GET":
        form_data = _editor_defaults()
        form_data["motion_config"] = json.loads(form_data["motion_config"])
        try:
            payload = validate_design_payload(form_data)
        except ValueError as exc:
            return _preview_error_response(str(exc)), 400
    else:
        try:
            payload = _payload_from_form(allow_empty_name=True)
        except ValueError as exc:
            return _preview_error_response(str(exc)), 400
    design = StorefrontDesign()
    _apply_payload(design, payload, _current_admin_or_403(), new=True)
    return _design_response(design)


@design_public_bp.route("/render/<int:design_id>")
def public_frame(design_id):
    if not schema_ready():
        abort(404)
    now = datetime.utcnow()
    design = StorefrontDesign.query.filter_by(
        id=design_id, status="published", is_enabled=True,
    ).filter(
        StorefrontDesign.approved_by_id.is_not(None),
        (StorefrontDesign.starts_at.is_(None)) | (StorefrontDesign.starts_at <= now),
        (StorefrontDesign.expires_at.is_(None)) | (StorefrontDesign.expires_at > now),
    ).first_or_404()
    return _design_response(design)


def _design_response(design):
    products = product_api_data(design)
    motion_config = json.loads(design.motion_config or "{}")
    motion_duration = max(100, min(3000, int(motion_config.get("duration", 600))))
    try:
        lottie_animation = parse_lottie_animation(design.lottie_json)
    except ValueError:
        current_app.logger.error("Design Studio design %s has invalid Lottie data; its animation was not rendered.", design.id)
        lottie_animation = None
    product_ids = [product["id"] for product in products]
    linked_products = Product.query.filter(Product.id.in_(product_ids), Product.active.is_(True)).all() if product_ids else []
    by_id = {product.id: product for product in linked_products}
    ordered_products = [by_id[product_id] for product_id in product_ids if product_id in by_id]
    product_cards = render_template("partials/design_studio_products.html", products=ordered_products)
    design_html = design.html.replace("%%FITYSTORES_PRODUCTS%%", product_cards)
    offer_cards = render_template("partials/design_studio_offers.html", products=ordered_products)
    design_html = design_html.replace("%%FITYSTORES_OFFERS%%", offer_cards)
    response = Response(render_template(
        "admin/design_studio_frame.html", design=design, design_html=design_html,
        products=products, motion_duration=motion_duration,
        lottie_animation=lottie_animation,
    ), mimetype="text/html")
    return _secure_frame_response(response)


def _preview_error_response(message):
    response = Response(
        render_template("admin/design_studio_preview_error.html", message=message),
        mimetype="text/html",
    )
    return _secure_frame_response(response)


def _secure_frame_response(response):
    origins = []
    for configured in (current_app.config.get("PUBLIC_BASE_URL", ""), current_app.config.get("SERVER_NAME", "")):
        if not configured:
            continue
        configured = str(configured)
        candidate = configured if "://" in configured else f"{request.scheme}://{configured}"
        parsed = urlsplit(candidate)
        try:
            parsed.port
        except ValueError:
            continue
        if (
            parsed.scheme in {"http", "https"} and parsed.hostname
            and parsed.username is None and parsed.password is None
            and re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", parsed.netloc or "")
        ):
            static_url_path = (current_app.static_url_path or "/static").rstrip("/")
            origins.append(f"{parsed.scheme}://{parsed.netloc}{static_url_path}/")
    static_sources = " ".join(dict.fromkeys(origins))
    if not static_sources:
        current_app.logger.error("Design Studio local media is blocked because no trusted public host is configured.")
        static_sources = "'none'"
    response.headers["Content-Security-Policy"] = (
        "sandbox allow-scripts; default-src 'none'; "
        f"script-src 'unsafe-inline' {static_sources}; style-src 'unsafe-inline'; worker-src blob:; "
        f"img-src {static_sources} https://res.cloudinary.com data:; "
        f"media-src {static_sources} https://res.cloudinary.com; "
        "connect-src 'none'; font-src 'none'; frame-src 'none'; object-src 'none'; "
        "form-action 'none'; base-uri 'none'; navigate-to 'none'; frame-ancestors 'self'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response
