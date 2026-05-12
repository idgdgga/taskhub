from __future__ import annotations

import hashlib
import hmac
import re
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.utils import timezone

from .models import Task, TaskApplication

PROVIDER = "mobidea"
_CLICK_RE = re.compile(r"^mb_(?P<application_id>\d+)_(?P<signature>[0-9a-f]{16})$")


def _click_secret() -> str:
    return (getattr(settings, "MOBIDEA_CLICK_SECRET", "") or getattr(settings, "SECRET_KEY", "")).strip()


def _sign_application_id(application_id: int) -> str:
    secret = _click_secret().encode("utf-8")
    body = f"mobidea:{int(application_id)}".encode("utf-8")
    return hmac.new(secret, body, hashlib.sha256).hexdigest()[:16]


def build_click_id(application: TaskApplication) -> str:
    return f"mb_{application.pk}_{_sign_application_id(application.pk)}"


def parse_click_id(click_id: str) -> int | None:
    match = _CLICK_RE.match((click_id or "").strip())
    if not match:
        return None
    application_id = int(match.group("application_id"))
    expected = _sign_application_id(application_id)
    if not hmac.compare_digest(expected, match.group("signature")):
        return None
    return application_id


def is_mobidea_task(task: Task) -> bool:
    if task.interaction_type == Task.INTERACTION_CPA_OFFER:
        cfg = task.interaction_config or {}
        return (cfg.get("provider") or PROVIDER) == PROVIDER
    cfg = task.interaction_config or {}
    return (cfg.get("provider") or "").strip().lower() == PROVIDER


def _replace_tracking_placeholders(url: str, click_id: str, site: str) -> tuple[str, bool]:
    encoded_click_id = quote(click_id, safe="")
    encoded_site = quote(site, safe="")
    replacements = {
        "ADD_CLICK_ID_HERE": encoded_click_id,
        "PASS_SITE_HERE": encoded_site,
        "{click_id}": encoded_click_id,
        "{{click_id}}": encoded_click_id,
        "{external_id}": encoded_click_id,
        "{{EXTERNAL_ID}}": encoded_click_id,
        "{site}": encoded_site,
        "{{site}}": encoded_site,
    }
    changed = False
    out = url
    for needle, replacement in replacements.items():
        if needle in out:
            out = out.replace(needle, replacement)
            changed = True
    return out, changed


def _set_query_defaults(url: str, values: dict[str, str]) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in values.items():
        query.setdefault(key, value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _tracking_template(task: Task) -> str:
    cfg = task.interaction_config or {}
    for key in (
        "mobidea_tracking_url_template",
        "tracking_url_template",
        "tracking_url",
        "target_url",
        "offer_url",
    ):
        value = str(cfg.get(key) or "").strip()
        if value:
            return value
    return ""


def ensure_action_url(application: TaskApplication) -> str:
    task = application.task
    template = _tracking_template(task)
    if not template:
        return ""

    cfg = task.interaction_config or {}
    click_id = application.external_click_id or build_click_id(application)
    site = str(cfg.get("site") or getattr(settings, "MOBIDEA_DEFAULT_SITE", "taskhub") or "taskhub").strip()
    url, changed = _replace_tracking_placeholders(template, click_id, site)
    if not changed:
        url = _set_query_defaults(url, {"pub_click_id": click_id, "site": site})

    update_fields: list[str] = []
    if application.external_provider != PROVIDER:
        application.external_provider = PROVIDER
        update_fields.append("external_provider")
    if application.external_click_id != click_id:
        application.external_click_id = click_id
        update_fields.append("external_click_id")
    if not application.external_started_at:
        application.external_started_at = timezone.now()
        update_fields.append("external_started_at")
    if update_fields:
        update_fields.append("updated_at")
        application.save(update_fields=update_fields)
    return url
