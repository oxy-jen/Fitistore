from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from sqlalchemy import inspect

from app.extensions import db
from app.models import ProductDetails, ProductVariantDetails


_ALLOWED_TAGS = {
    "a", "blockquote", "br", "em", "figcaption", "figure", "h2", "h3", "h4",
    "hr", "i", "img", "li", "ol", "p", "strong", "u", "ul",
}
_VOID_TAGS = {"br", "hr", "img"}
_DROP_CONTENT_TAGS = {"iframe", "math", "object", "script", "style", "svg", "template"}


def _safe_content_url(value, *, image=False):
    value = (value or "").strip()
    if not value or "\\" in value:
        return ""
    parsed = urlsplit(value)
    if not parsed.scheme and not parsed.netloc:
        if value.startswith("/") and not value.startswith("//"):
            return value if not image or value.startswith("/static/") else ""
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    if image:
        hostname = parsed.hostname.lower()
        if parsed.scheme != "https" or (hostname != "res.cloudinary.com" and not hostname.endswith(".cloudinary.com")):
            return ""
    return value


class _ProductHtmlSanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.output = []
        self.open_tags = []
        self.dropped_tags = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.dropped_tags:
            if tag in _DROP_CONTENT_TAGS:
                self.dropped_tags.append(tag)
            return
        if tag in _DROP_CONTENT_TAGS:
            self.dropped_tags.append(tag)
            return
        if tag not in _ALLOWED_TAGS:
            return

        values = {name: value or "" for name, value in attrs}
        safe_attrs = []
        if tag == "a":
            href = _safe_content_url(values.get("href"))
            if href:
                safe_attrs.append(("href", href))
            if values.get("title"):
                safe_attrs.append(("title", values["title"][:200]))
        elif tag == "img":
            src = _safe_content_url(values.get("src"), image=True)
            if not src:
                return
            safe_attrs.extend((("src", src), ("alt", values.get("alt", "")[:255])))

        rendered_attrs = "".join(
            f' {name}="{escape(value, quote=True)}"' for name, value in safe_attrs
        )
        self.output.append(f"<{tag}{rendered_attrs}>")
        if tag not in _VOID_TAGS:
            self.open_tags.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() in self.open_tags:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.dropped_tags:
            for index in range(len(self.dropped_tags) - 1, -1, -1):
                if self.dropped_tags[index] == tag:
                    del self.dropped_tags[index:]
                    break
            return
        if tag not in self.open_tags:
            return
        while self.open_tags:
            current = self.open_tags.pop()
            self.output.append(f"</{current}>")
            if current == tag:
                break

    def handle_data(self, data):
        if not self.dropped_tags:
            self.output.append(escape(data, quote=False))

    def close(self):
        super().close()
        while self.open_tags:
            self.output.append(f"</{self.open_tags.pop()}>")


def sanitize_product_html(value):
    parser = _ProductHtmlSanitizer()
    parser.feed(value or "")
    parser.close()
    return "".join(parser.output)


def product_details_schema_ready():
    inspector = inspect(db.engine)
    for model in (ProductDetails, ProductVariantDetails):
        table = model.__table__
        if not inspector.has_table(table.name):
            return False
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        if {column.name for column in table.columns} - columns:
            return False
    return True
