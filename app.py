from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
import config
import os
import csv
import re
import json
import threading
import time
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
from translations import get_translator
from us_area_codes import lookup_area_code

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.permanent_session_lifetime = timedelta(days=30)

os.makedirs(config.DATA_DIR, exist_ok=True)
os.makedirs(config.LOGS_DIR, exist_ok=True)

CONTACT_FIELDS = ["name", "phone", "city", "opted_out", "consent_source", "consent_date"]

# Endpoints reachable without being logged in. The webhook must stay public
# so Vonage can reach it; static files must stay public for CSS/JS to load.
PUBLIC_ENDPOINTS = {"login", "setup", "static", "inbound_sms_webhook", "set_language"}


@app.context_processor
def inject_translator():
    lang = session.get("lang", "en")
    return {"t": get_translator(lang), "current_lang": lang}


@app.route("/set-language/<lang>")
def set_language(lang):
    if lang in ("en", "es"):
        session["lang"] = lang
    return redirect(request.referrer or url_for("dashboard"))


def admin_configured():
    s = load_settings()
    return bool(s.get("admin_username") and s.get("admin_password_hash"))


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None

    if not admin_configured():
        return redirect(url_for("setup"))

    if not session.get("user"):
        return redirect(url_for("login", next=request.path))

    return None


@app.route("/setup", methods=["GET", "POST"])
def setup():
    if admin_configured():
        return redirect(url_for("login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username:
            flash(session.get("lang") and get_translator(session.get("lang", "en"))("username_required") or "Username is required.", "error")
            return redirect(url_for("setup"))
        if len(password) < 8:
            flash(get_translator(session.get("lang", "en"))("password_too_short"), "error")
            return redirect(url_for("setup"))
        if password != confirm:
            flash(get_translator(session.get("lang", "en"))("passwords_dont_match"), "error")
            return redirect(url_for("setup"))

        current = load_settings()
        current["admin_username"] = username
        current["admin_password_hash"] = generate_password_hash(password)
        save_settings(current)

        session.permanent = True
        session["user"] = username
        flash("Account created.", "success")
        return redirect(url_for("dashboard"))

    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not admin_configured():
        return redirect(url_for("setup"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        s = load_settings()
        valid = (
            username == s.get("admin_username")
            and s.get("admin_password_hash")
            and check_password_hash(s["admin_password_hash"], password)
        )

        if valid:
            session.permanent = True
            session["user"] = username
            next_url = request.form.get("next") or url_for("dashboard")
            return redirect(next_url)

        flash(get_translator(session.get("lang", "en"))("invalid_credentials"), "error")
        return redirect(url_for("login", next=request.form.get("next", "")))

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/logout")
def logout():
    session.pop("user", None)
    return redirect(url_for("login"))


@app.route("/account/change-password", methods=["POST"])
def change_password():
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    s = load_settings()

    if not check_password_hash(s.get("admin_password_hash", ""), current_password):
        flash(get_translator(session.get("lang", "en"))("current_password_wrong"), "error")
        return redirect(url_for("settings_page"))

    if len(new_password) < 8:
        flash(get_translator(session.get("lang", "en"))("password_too_short"), "error")
        return redirect(url_for("settings_page"))

    if new_password != confirm_password:
        flash(get_translator(session.get("lang", "en"))("passwords_dont_match"), "error")
        return redirect(url_for("settings_page"))

    s["admin_password_hash"] = generate_password_hash(new_password)
    save_settings(s)
    flash(get_translator(session.get("lang", "en"))("password_changed"), "success")
    return redirect(url_for("settings_page"))


def load_settings():
    """Load Vonage + Anthropic + admin account settings, preferring saved settings.json over .env defaults."""
    defaults = {
        "vonage_api_key": config.VONAGE_API_KEY or "",
        "vonage_api_secret": config.VONAGE_API_SECRET or "",
        "vonage_from_number": config.VONAGE_FROM_NUMBER or "",
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY", "") or "",
        "admin_username": "",
        "admin_password_hash": "",
    }
    if os.path.exists(config.SETTINGS_FILE):
        try:
            with open(config.SETTINGS_FILE, encoding="utf-8") as f:
                saved = json.load(f)
            defaults.update({k: v for k, v in saved.items() if v})
        except (json.JSONDecodeError, OSError):
            pass
    return defaults


def save_settings(settings):
    tmp_file = config.SETTINGS_FILE + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(settings, f)
    os.replace(tmp_file, config.SETTINGS_FILE)


def anthropic_is_configured():
    s = load_settings()
    return bool(s.get("anthropic_api_key"))


def generate_message_with_ai(prompt, max_chars, language="en"):
    """Call the Anthropic API to draft an SMS message. Returns (success, text_or_error)."""
    s = load_settings()
    api_key = s.get("anthropic_api_key")
    if not api_key:
        return False, "anthropic_not_configured"

    lang_instruction = "in English" if language == "en" else "in Spanish"
    system_prompt = (
        f"You write concise SMS marketing messages {lang_instruction} for a business. "
        f"The message must be under {max_chars} characters, ideally much shorter. "
        "Do not use quotation marks or hashtags. Do not add a signature or business name unless asked. "
        "You may use {name} literally as a placeholder for the recipient's first name if it fits naturally. "
        "Reply with ONLY the message text, nothing else — no preamble, no explanation."
    )

    try:
        import requests
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 300,
                "system": system_prompt,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )

        if response.status_code == 401:
            return False, "invalid_key"
        if response.status_code != 200:
            detail = response.json().get("error", {}).get("message", f"HTTP {response.status_code}")
            return False, detail

        data = response.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        message = "".join(text_blocks).strip().strip('"')

        if not message:
            return False, "empty_response"
        if len(message) > max_chars:
            message = message[:max_chars].rsplit(" ", 1)[0]

        return True, message

    except requests.exceptions.Timeout:
        return False, "timeout"
    except Exception as e:
        return False, str(e)


def parse_search_with_ai(query, language="en"):
    """Translate a natural-language contact search into structured filters.
    The AI never sees or returns actual contact data — it only extracts filter
    criteria, which are then applied against the real contact list in Python.
    Returns (success, filters_dict_or_error)."""
    s = load_settings()
    api_key = s.get("anthropic_api_key")
    if not api_key:
        return False, "anthropic_not_configured"

    lang_note = "The query may be in English or Spanish." if language == "es" else ""
    system_prompt = (
        "You convert a short natural-language request about searching a contact list "
        "into a strict JSON filter object. Available fields: "
        "'city' (a US city or region name mentioned in the query, or null if none), "
        "'status' (one of 'active', 'opted_out', or 'any' — default 'any' unless the "
        "query clearly asks for unsubscribed/opted-out contacts or explicitly active ones), "
        "'text' (a free-text fragment to match against a contact's name, or null). "
        f"{lang_note} "
        "Respond with ONLY a JSON object with exactly these three keys: city, status, text. "
        "No explanation, no markdown, no code fences."
    )

    try:
        import requests
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 200,
                "system": system_prompt,
                "messages": [{"role": "user", "content": query}],
            },
            timeout=15,
        )

        if response.status_code == 401:
            return False, "invalid_key"
        if response.status_code != 200:
            detail = response.json().get("error", {}).get("message", f"HTTP {response.status_code}")
            return False, detail

        data = response.json()
        text_blocks = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        raw = "".join(text_blocks).strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()

        parsed = json.loads(raw)
        filters = {
            "city": (parsed.get("city") or "").strip(),
            "status": parsed.get("status") if parsed.get("status") in ("active", "opted_out", "any") else "any",
            "text": (parsed.get("text") or "").strip(),
        }
        return True, filters

    except requests.exceptions.Timeout:
        return False, "timeout"
    except (json.JSONDecodeError, KeyError, AttributeError):
        return False, "parse_error"
    except Exception as e:
        return False, str(e)


def normalize_phone(phone, default_country="+1"):
    """Clean a phone number and add the default country code if missing."""
    if not phone:
        return ""
    phone = re.sub(r"[^\d+]", "", phone)
    if phone.startswith("+"):
        return phone
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 10:
        return f"{default_country}{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return phone


def load_contacts():
    if not os.path.exists(config.CONTACTS_FILE):
        return []
    with open(config.CONTACTS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            for field in CONTACT_FIELDS:
                row.setdefault(field, "")
            rows.append(row)
        return rows


def save_contacts(contacts):
    tmp_file = config.CONTACTS_FILE + ".tmp"
    with open(tmp_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CONTACT_FIELDS)
        writer.writeheader()
        for c in contacts:
            row = {field: c.get(field, "") for field in CONTACT_FIELDS}
            writer.writerow(row)
    os.replace(tmp_file, config.CONTACTS_FILE)


def city_from_phone(phone):
    """Infer a US city/state from a phone number's area code, when no
    explicit city was provided. Returns '' if the number isn't a
    recognizable US number or the area code isn't in our table."""
    if not phone or not phone.startswith("+1"):
        return ""
    area_code = phone[2:5]
    city, state = lookup_area_code(area_code)
    if city and state:
        return f"{city}, {state}"
    return ""


def parse_vcf(text):
    """Extract name + phone (+ city, if present) from a vCard (.vcf) export."""
    contacts = []
    name, phone, city = None, None, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("FN:"):
            name = line[3:].strip()
        elif line.startswith("TEL"):
            match = re.search(r":(.+)$", line)
            if match:
                phone = normalize_phone(match.group(1))
        elif line.startswith("ADR"):
            # vCard ADR format: ADR;TYPE=HOME:;;Street;City;State;Zip;Country
            match = re.search(r":(.+)$", line)
            if match:
                parts = match.group(1).split(";")
                if len(parts) > 3 and parts[3].strip():
                    city = parts[3].strip()
        elif line == "END:VCARD":
            if phone:
                final_city = city or city_from_phone(phone)
                contacts.append({"name": name or "", "phone": phone, "city": final_city, "opted_out": "False"})
            name, phone, city = None, None, None
    return contacts


def parse_csv(text):
    contacts = []
    reader = csv.DictReader(text.splitlines())
    for row in reader:
        keys = {k.lower().strip(): k for k in row.keys() if k}
        name = row.get(keys.get("name", ""), "") or ""
        phone_raw = row.get(keys.get("phone", ""), "") or row.get(keys.get("number", ""), "") or ""
        city = (row.get(keys.get("city", ""), "") or "").strip()
        phone = normalize_phone(phone_raw)
        if phone:
            final_city = city or city_from_phone(phone)
            contacts.append({"name": name.strip(), "phone": phone, "city": final_city, "opted_out": "False"})
    return contacts


@app.route("/")
def dashboard():
    all_contacts = load_contacts()
    all_templates = load_templates()

    all_campaigns = load_campaigns()
    stats = {
        "total": len(all_contacts),
        "delivered": sum(int(c.get("sent", 0)) for c in all_campaigns),
        "failed": sum(int(c.get("failed", 0)) for c in all_campaigns),
        "pending": 0,
    }

    recent_sends = []
    if os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE, newline="", encoding="utf-8") as f:
            reader = list(csv.DictReader(f))
        campaign_names = {c["id"]: c["template_name"] for c in all_campaigns}
        for entry in reversed(reader[-8:]):
            recent_sends.append({
                "number": entry["phone"],
                "template": campaign_names.get(entry["campaign_id"], "—"),
                "status": "Delivered" if entry["status"] == "sent" else "Failed",
                "status_class": "ok" if entry["status"] == "sent" else "fail",
            })

    templates_summary = [
        {"name": t["name"], "chars": len(t.get("body", ""))}
        for t in all_templates[:6]
    ]

    return render_template(
        "dashboard.html",
        active="dashboard",
        stats=stats,
        recent_sends=recent_sends,
        templates=templates_summary,
        max_chars=config.MENSAJE_MAX_CHARS,
    )


def get_cities_summary(all_contacts):
    """Distinct cities with contact counts, most common first."""
    city_counts = {}
    for c in all_contacts:
        city = c.get("city", "").strip()
        if city:
            city_counts[city] = city_counts.get(city, 0) + 1
    return sorted(city_counts.items(), key=lambda kv: (-kv[1], kv[0]))


@app.route("/contacts")
def contacts():
    all_contacts = load_contacts()

    search = request.args.get("search", "").strip().lower()
    city_filter = request.args.get("city", "").strip().lower()
    status_filter = request.args.get("status", "").strip().lower()  # active | opted_out | ""

    filtered = all_contacts
    if search:
        filtered = [
            c for c in filtered
            if search in c.get("name", "").lower() or search in c.get("phone", "").lower()
        ]
    if city_filter:
        filtered = [c for c in filtered if city_filter in c.get("city", "").lower()]
    if status_filter == "active":
        filtered = [c for c in filtered if c.get("opted_out") != "True"]
    elif status_filter == "opted_out":
        filtered = [c for c in filtered if c.get("opted_out") == "True"]

    has_filters = bool(search or city_filter or status_filter)

    # Group/order by city so contacts from the same city sit together,
    # instead of appearing in random import order. Contacts with no city
    # detected sort last.
    filtered = sorted(
        filtered,
        key=lambda c: (c.get("city", "").strip() == "", c.get("city", "").lower(), c.get("name", "").lower())
    )

    # Summary of distinct cities with counts, for the overview panel and
    # the autocomplete list — built from ALL contacts, not just the current
    # filtered page, so it always reflects the full picture.
    cities_summary = get_cities_summary(all_contacts)

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    per_page = 50
    total_filtered = len(filtered)
    total_pages = max(1, (total_filtered + per_page - 1) // per_page)
    page = min(page, total_pages)

    start = (page - 1) * per_page
    end = start + per_page
    page_contacts = filtered[start:end]

    display_contacts = []
    for c in page_contacts:
        display_contacts.append({
            "name": c.get("name", ""),
            "phone": c.get("phone", ""),
            "city": c.get("city", "") or "—",
            "opted_out": c.get("opted_out") == "True",
            "consent_source": c.get("consent_source", "") or "—",
            "consent_date": c.get("consent_date", "") or "—",
        })

    return render_template(
        "contacts.html",
        active="contacts",
        contacts=display_contacts,
        contacts_count=len(all_contacts),
        filtered_count=total_filtered,
        search=search,
        city_filter=request.args.get("city", ""),
        status_filter=status_filter,
        has_filters=has_filters,
        cities_summary=cities_summary,
        page=page,
        total_pages=total_pages,
        ai_ready=anthropic_is_configured(),
    )


@app.route("/contacts/smart-search", methods=["POST"])
def smart_search_contacts():
    from flask import jsonify

    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"success": False, "error": "empty_query"}), 400

    if not anthropic_is_configured():
        return jsonify({"success": False, "error": "not_configured"}), 400

    lang = session.get("lang", "en")
    success, result = parse_search_with_ai(query, language=lang)

    if success:
        return jsonify({"success": True, "filters": result})
    return jsonify({"success": False, "error": result}), 502


@app.route("/contacts/upload", methods=["POST"])
def upload_contacts():
    file = request.files.get("contacts_file")
    consent_source = request.form.get("consent_source", "").strip()
    consent_date = request.form.get("consent_date", "").strip() or datetime.now().strftime("%Y-%m-%d")

    if not file or file.filename == "":
        flash("No file selected.", "error")
        return redirect(url_for("contacts"))

    if not consent_source:
        flash("Consent source is required to import contacts.", "error")
        return redirect(url_for("contacts"))

    filename = file.filename.lower()
    if not (filename.endswith(".csv") or filename.endswith(".vcf")):
        flash("Unsupported file type. Please upload a .csv or .vcf file.", "error")
        return redirect(url_for("contacts"))

    try:
        text = file.read().decode("utf-8", errors="ignore")
    except Exception:
        flash("Could not read the file. Please check the file and try again.", "error")
        return redirect(url_for("contacts"))

    if filename.endswith(".vcf"):
        new_contacts = parse_vcf(text)
    else:
        new_contacts = parse_csv(text)

    if not new_contacts:
        flash("No valid contacts found in the file.", "error")
        return redirect(url_for("contacts"))

    for c in new_contacts:
        c["consent_source"] = consent_source
        c["consent_date"] = consent_date

    existing = load_contacts()
    existing_phones = {c["phone"] for c in existing}

    added = 0
    skipped = 0
    for c in new_contacts:
        if c["phone"] not in existing_phones:
            existing.append(c)
            existing_phones.add(c["phone"])
            added += 1
        else:
            skipped += 1

    save_contacts(existing)
    flash(f"{added} contacts imported. {skipped} duplicates skipped.", "success")
    return redirect(url_for("contacts"))


@app.route("/contacts/edit/<path:phone>", methods=["GET", "POST"])
def edit_contact(phone):
    all_contacts = load_contacts()
    target = next((c for c in all_contacts if c["phone"] == phone), None)

    if target is None:
        flash("Contact not found.", "error")
        return redirect(url_for("contacts"))

    if request.method == "POST":
        new_name = request.form.get("name", "").strip()
        new_phone = normalize_phone(request.form.get("phone", "").strip())
        new_city = request.form.get("city", "").strip()
        opted_out = "True" if request.form.get("opted_out") == "on" else "False"

        if not new_phone:
            flash("Phone number cannot be empty.", "error")
            return redirect(url_for("edit_contact", phone=phone))

        if new_phone != phone and any(c["phone"] == new_phone for c in all_contacts):
            flash("Another contact already has that phone number.", "error")
            return redirect(url_for("edit_contact", phone=phone))

        target["name"] = new_name
        target["phone"] = new_phone
        target["city"] = new_city
        target["opted_out"] = opted_out

        save_contacts(all_contacts)
        flash("Contact updated.", "success")
        return redirect(url_for("contacts"))

    return render_template("edit_contact.html", active="contacts", contact=target)


@app.route("/contacts/delete/<path:phone>", methods=["POST"])
def delete_contact(phone):
    all_contacts = load_contacts()
    remaining = [c for c in all_contacts if c["phone"] != phone]

    if len(remaining) == len(all_contacts):
        flash("Contact not found.", "error")
    else:
        save_contacts(remaining)
        flash("Contact deleted.", "success")

    return redirect(url_for("contacts"))


TEMPLATE_FIELDS = ["id", "name", "body", "created_at"]


def load_templates():
    if not os.path.exists(config.TEMPLATES_FILE):
        return []
    with open(config.TEMPLATES_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(reader)


def save_templates(templates):
    tmp_file = config.TEMPLATES_FILE + ".tmp"
    with open(tmp_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TEMPLATE_FIELDS)
        writer.writeheader()
        writer.writerows(templates)
    os.replace(tmp_file, config.TEMPLATES_FILE)


def next_template_id(templates):
    if not templates:
        return "1"
    return str(max(int(t["id"]) for t in templates) + 1)


@app.route("/templates")
def templates_page():
    all_templates = load_templates()
    for t in all_templates:
        t["char_count"] = len(t.get("body", ""))
    return render_template(
        "templates.html",
        active="templates",
        templates=all_templates,
        max_chars=config.MENSAJE_MAX_CHARS,
    )


@app.route("/templates/new", methods=["GET", "POST"])
def new_template():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        body = request.form.get("body", "").strip()

        if not name:
            flash("Template name is required.", "error")
            return redirect(url_for("new_template"))
        if not body:
            flash("Message body cannot be empty.", "error")
            return redirect(url_for("new_template"))
        if len(body) > config.MENSAJE_MAX_CHARS:
            flash(f"Message exceeds the {config.MENSAJE_MAX_CHARS} character limit.", "error")
            return redirect(url_for("new_template"))

        all_templates = load_templates()
        new_tpl = {
            "id": next_template_id(all_templates),
            "name": name,
            "body": body,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        all_templates.append(new_tpl)
        save_templates(all_templates)
        flash("Template created.", "success")
        return redirect(url_for("templates_page"))

    return render_template(
        "template_form.html",
        active="templates",
        template=None,
        max_chars=config.MENSAJE_MAX_CHARS,
        ai_ready=anthropic_is_configured(),
    )


@app.route("/templates/edit/<template_id>", methods=["GET", "POST"])
def edit_template(template_id):
    all_templates = load_templates()
    target = next((t for t in all_templates if t["id"] == template_id), None)

    if target is None:
        flash("Template not found.", "error")
        return redirect(url_for("templates_page"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        body = request.form.get("body", "").strip()

        if not name:
            flash("Template name is required.", "error")
            return redirect(url_for("edit_template", template_id=template_id))
        if not body:
            flash("Message body cannot be empty.", "error")
            return redirect(url_for("edit_template", template_id=template_id))
        if len(body) > config.MENSAJE_MAX_CHARS:
            flash(f"Message exceeds the {config.MENSAJE_MAX_CHARS} character limit.", "error")
            return redirect(url_for("edit_template", template_id=template_id))

        target["name"] = name
        target["body"] = body
        save_templates(all_templates)
        flash("Template updated.", "success")
        return redirect(url_for("templates_page"))

    return render_template(
        "template_form.html",
        active="templates",
        template=target,
        max_chars=config.MENSAJE_MAX_CHARS,
        ai_ready=anthropic_is_configured(),
    )


@app.route("/templates/generate-ai", methods=["POST"])
def generate_ai_template():
    from flask import jsonify

    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return jsonify({"success": False, "error": "empty_prompt"}), 400

    if not anthropic_is_configured():
        return jsonify({"success": False, "error": "not_configured"}), 400

    lang = session.get("lang", "en")
    success, result = generate_message_with_ai(prompt, config.MENSAJE_MAX_CHARS, language=lang)

    if success:
        return jsonify({"success": True, "message": result})
    return jsonify({"success": False, "error": result}), 502


@app.route("/templates/delete/<template_id>", methods=["POST"])
def delete_template(template_id):
    all_templates = load_templates()
    remaining = [t for t in all_templates if t["id"] != template_id]

    if len(remaining) == len(all_templates):
        flash("Template not found.", "error")
    else:
        save_templates(remaining)
        flash("Template deleted.", "success")

    return redirect(url_for("templates_page"))


CAMPAIGN_FIELDS = ["id", "template_id", "template_name", "total_recipients", "sent", "failed",
                    "status", "mode", "created_at", "scheduled_at", "city_filter", "days_filter",
                    "last_progress_at"]
LOG_FIELDS = ["timestamp", "campaign_id", "phone", "status", "detail"]

campaigns_lock = threading.Lock()


def load_campaigns():
    if not os.path.exists(config.CAMPAIGNS_FILE):
        return []
    with open(config.CAMPAIGNS_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            for field in CAMPAIGN_FIELDS:
                row.setdefault(field, "")
            rows.append(row)
        return rows


def save_campaigns(campaigns):
    tmp_file = config.CAMPAIGNS_FILE + ".tmp"
    with open(tmp_file, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CAMPAIGN_FIELDS)
        writer.writeheader()
        for c in campaigns:
            writer.writerow({field: c.get(field, "") for field in CAMPAIGN_FIELDS})
    os.replace(tmp_file, config.CAMPAIGNS_FILE)


def update_campaign_record(campaign_id, **fields):
    """Thread-safe partial update of a single campaign's saved record.
    Any update that touches sent/failed counts also stamps a fresh
    'last_progress_at' heartbeat, used to detect genuinely frozen sends."""
    if "sent" in fields or "failed" in fields:
        fields.setdefault("last_progress_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    with campaigns_lock:
        campaigns = load_campaigns()
        for c in campaigns:
            if c["id"] == campaign_id:
                c.update({k: str(v) for k, v in fields.items()})
                break
        save_campaigns(campaigns)


def get_in_progress_campaign():
    for c in load_campaigns():
        if c.get("status") == "in_progress":
            return c
    return None


def mark_stale_campaigns_interrupted():
    """Run once at startup. Any campaign still marked 'in_progress' means the
    previous process died (restart, redeploy, crash) before finishing — there
    is no background thread actually running for it anymore, so it's honest
    to mark it interrupted rather than leave it looking like it's still going.
    Scheduled and queued campaigns are left untouched — the scheduler thread
    picks them up normally."""
    campaigns = load_campaigns()
    changed = False
    for c in campaigns:
        if c.get("status") == "in_progress":
            c["status"] = "interrupted"
            changed = True
    if changed:
        save_campaigns(campaigns)


def auto_release_frozen_campaigns():
    """Safety net that runs continuously (every scheduler tick) alongside the
    startup check. If a campaign is 'in_progress' but hasn't reported any
    progress in a suspiciously long time (its background thread is likely
    hung, not just sending slowly), it gets released automatically so the
    queue can keep moving — without anyone needing to notice or click
    anything."""
    campaigns = load_campaigns()
    changed = False
    now = datetime.now()
    for c in campaigns:
        if c.get("status") != "in_progress":
            continue
        reference = c.get("last_progress_at") or c.get("created_at")
        if not reference:
            continue
        try:
            reference_dt = datetime.strptime(reference, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (now - reference_dt).total_seconds() > config.STUCK_CAMPAIGN_TIMEOUT_SECONDS:
            c["status"] = "interrupted"
            changed = True
    if changed:
        save_campaigns(campaigns)


def next_campaign_id(campaigns):
    if not campaigns:
        return "1"
    return str(max(int(c["id"]) for c in campaigns) + 1)


def append_send_log(rows):
    file_exists = os.path.exists(config.LOG_FILE)
    with open(config.LOG_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerows(rows)


def vonage_is_configured():
    s = load_settings()
    return bool(s["vonage_api_key"] and s["vonage_api_secret"] and s["vonage_from_number"])


def send_sms_real(client, from_number, to_number, message):
    """Send a single SMS via Vonage. Returns (success, detail)."""
    try:
        response = client.sms.send({
            "to": to_number.replace("+", ""),
            "from": from_number,
            "text": message,
        })
        messages = response.get("messages", [{}])
        status = messages[0].get("status", "1")
        if status == "0":
            return True, "delivered"
        return False, messages[0].get("error-text", "unknown error")
    except Exception as e:
        return False, str(e)


def personalize(body, contact):
    return body.replace("{name}", contact.get("name") or "there")


def estimate_send_duration(count):
    """Return a human-readable estimate of how long a send will take at the
    configured rate limit."""
    seconds = count / max(config.SEND_RATE_PER_SECOND, 0.01)
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"~{int(minutes)} min"
    hours = minutes / 60
    return f"~{hours:.1f} hr"


def get_last_sent_map():
    """Scan the send log once and return {phone: latest datetime sent}."""
    last_sent = {}
    if not os.path.exists(config.LOG_FILE):
        return last_sent
    with open(config.LOG_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("status") != "sent":
                continue
            phone = row.get("phone", "")
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            except (ValueError, KeyError):
                continue
            if phone not in last_sent or ts > last_sent[phone]:
                last_sent[phone] = ts
    return last_sent


def filter_recipients(active_contacts, city_filter="", days_filter=""):
    """Apply optional city and 'not messaged in N days' segmentation filters."""
    filtered = active_contacts

    if city_filter:
        cf = city_filter.strip().lower()
        filtered = [c for c in filtered if cf in c.get("city", "").lower()]

    if days_filter:
        try:
            days = int(days_filter)
        except ValueError:
            days = None
        if days is not None and days > 0:
            last_sent = get_last_sent_map()
            cutoff = datetime.now() - timedelta(days=days)
            filtered = [
                c for c in filtered
                if c["phone"] not in last_sent or last_sent[c["phone"]] < cutoff
            ]

    return filtered


def run_campaign_send(campaign_id, template_body, recipients, mode, vonage_creds):
    """Runs in a background thread. Never touches Flask's request/session —
    everything it needs is passed in as plain values up front."""
    client = None
    from_number = None
    if mode == "live":
        try:
            import vonage
            auth = vonage.Auth(api_key=vonage_creds["vonage_api_key"], api_secret=vonage_creds["vonage_api_secret"])
            client = vonage.Vonage(auth)
            from_number = vonage_creds["vonage_from_number"]
        except Exception as e:
            update_campaign_record(campaign_id, status="completed", sent=0, failed=len(recipients))
            append_send_log([{
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "campaign_id": campaign_id, "phone": "-", "status": "failed",
                "detail": f"Could not start Vonage client: {e}",
            }])
            return

    sent_count = 0
    failed_count = 0

    try:
        for i, contact in enumerate(recipients, start=1):
            try:
                message = personalize(template_body, contact)
                phone = contact.get("phone", "")
                if not phone:
                    raise ValueError("contact missing phone number")

                if mode == "live":
                    success, detail = send_sms_real(client, from_number, phone, message)
                else:
                    success, detail = True, "simulated (Vonage not configured)"
            except Exception as e:
                success, detail = False, f"unexpected error: {e}"
                phone = contact.get("phone", "-") if isinstance(contact, dict) else "-"

            if success:
                sent_count += 1
            else:
                failed_count += 1

            try:
                append_send_log([{
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "campaign_id": campaign_id,
                    "phone": phone,
                    "status": "sent" if success else "failed",
                    "detail": detail,
                }])
            except Exception:
                pass  # never let a logging failure kill the whole send

            if i % config.PROGRESS_SAVE_EVERY == 0 or i == len(recipients):
                try:
                    update_campaign_record(campaign_id, sent=sent_count, failed=failed_count)
                except Exception:
                    pass

        if i < len(recipients):
            # Real sends respect Vonage's rate limit. Simulation mode uses a
            # tiny delay just so progress is visible without waiting for real.
            time.sleep(1.0 / config.SEND_RATE_PER_SECOND if mode == "live" else 0.02)
    finally:
        # No matter what happens above — even a completely unexpected crash —
        # the campaign must never be left stuck at "in_progress" forever,
        # since that would block every future campaign from being created.
        try:
            update_campaign_record(campaign_id, sent=sent_count, failed=failed_count, status="completed")
        except Exception:
            pass


def start_campaign_send(campaign_id, template_body, recipients, mode):
    """Kick off a background send thread, snapshotting Vonage creds up front."""
    vonage_creds = load_settings() if mode == "live" else {}
    thread = threading.Thread(
        target=run_campaign_send,
        args=(campaign_id, template_body, [dict(c) for c in recipients], mode, vonage_creds),
        daemon=True,
    )
    thread.start()


def launch_campaign_now(c):
    """Given a campaign record that's ready to go (scheduled time arrived, or
    its turn in the queue), recompute its audience fresh and start sending."""
    all_templates = load_templates()
    template = next((t for t in all_templates if t["id"] == c["template_id"]), None)
    if not template:
        update_campaign_record(c["id"], status="completed", sent=0, failed=0)
        return

    all_contacts = load_contacts()
    active_contacts = [ct for ct in all_contacts if ct.get("opted_out") != "True"]
    recipients = filter_recipients(active_contacts, c.get("city_filter", ""), c.get("days_filter", ""))

    mode = "live" if vonage_is_configured() else "simulation"
    update_campaign_record(c["id"], mode=mode, total_recipients=len(recipients))
    start_campaign_send(c["id"], template["body"], recipients, mode)


def process_due_scheduled_campaigns():
    """Check for scheduled campaigns whose time has come and launch them.
    Extracted from the loop so it can be triggered directly (used by the
    background scheduler thread every 30s, and callable on-demand)."""
    with campaigns_lock:
        campaigns = load_campaigns()
        due = [
            c for c in campaigns
            if c.get("status") == "scheduled"
            and c.get("scheduled_at")
            and datetime.now() >= datetime.strptime(c["scheduled_at"], "%Y-%m-%d %H:%M:%S")
        ]
        for c in due:
            c["status"] = "in_progress"
        save_campaigns(campaigns)

    for c in due:
        launch_campaign_now(c)


def process_campaign_queue():
    """If nothing is currently sending, start the oldest queued campaign
    (one that was created while another send was active, so it waited its
    turn instead of blocking the person from creating it in the first
    place)."""
    with campaigns_lock:
        campaigns = load_campaigns()
        if any(c.get("status") == "in_progress" for c in campaigns):
            return  # something is already sending — wait for it to finish

        queued = [c for c in campaigns if c.get("status") == "queued"]
        if not queued:
            return
        next_up = sorted(queued, key=lambda c: int(c["id"]))[0]

        for c in campaigns:
            if c["id"] == next_up["id"]:
                c["status"] = "in_progress"
        save_campaigns(campaigns)

    launch_campaign_now(next_up)


def run_scheduler():
    """Background loop (started once at app startup, runs every 30s) that:
    1. Launches scheduled campaigns whose time has come.
    2. Auto-releases any campaign that's been frozen for too long, so it
       never blocks everything else forever.
    3. Promotes the next queued campaign once nothing else is sending —
       this is what lets people create campaigns freely instead of being
       blocked while one is already running."""
    while True:
        try:
            auto_release_frozen_campaigns()
            process_due_scheduled_campaigns()
            process_campaign_queue()
        except Exception:
            pass
        time.sleep(30)


@app.route("/campaigns")
def campaigns_page():
    all_campaigns = load_campaigns()
    all_campaigns.sort(key=lambda c: int(c["id"]), reverse=True)
    return render_template(
        "campaigns.html",
        active="campaigns",
        campaigns=all_campaigns,
        vonage_ready=vonage_is_configured(),
    )


@app.route("/campaigns/preview-audience", methods=["POST"])
def preview_audience():
    data = request.get_json(silent=True) or {}
    all_contacts = load_contacts()
    active_contacts = [c for c in all_contacts if c.get("opted_out") != "True"]
    filtered = filter_recipients(active_contacts, data.get("city", ""), data.get("days", ""))
    return jsonify({"count": len(filtered), "estimated_time": estimate_send_duration(len(filtered)) if filtered else ""})


@app.route("/campaigns/new", methods=["GET", "POST"])
def new_campaign():
    all_templates = load_templates()
    all_contacts = load_contacts()
    active_contacts = [c for c in all_contacts if c.get("opted_out") != "True"]
    in_progress = get_in_progress_campaign()

    if request.method == "POST":
        template_id = request.form.get("template_id", "")
        city_filter = request.form.get("city_filter", "").strip()
        days_filter = request.form.get("days_filter", "").strip()
        scheduled_at_raw = request.form.get("scheduled_at", "").strip()

        target = next((t for t in all_templates if t["id"] == template_id), None)
        if not target:
            flash("Please select a valid template.", "error")
            return redirect(url_for("new_campaign"))

        recipients = filter_recipients(active_contacts, city_filter, days_filter)
        if not recipients:
            flash("No active contacts match the selected audience.", "error")
            return redirect(url_for("new_campaign"))

        campaigns = load_campaigns()
        campaign_id = next_campaign_id(campaigns)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        scheduled_dt = None
        if scheduled_at_raw:
            try:
                scheduled_dt = datetime.strptime(scheduled_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                scheduled_dt = None
            if scheduled_dt and scheduled_dt <= datetime.now():
                scheduled_dt = None  # ignore past/invalid times, send immediately instead

        if scheduled_dt:
            new_campaign_record = {
                "id": campaign_id,
                "template_id": template_id,
                "template_name": target["name"],
                "total_recipients": str(len(recipients)),
                "sent": "0",
                "failed": "0",
                "status": "scheduled",
                "mode": "live" if vonage_is_configured() else "simulation",
                "created_at": timestamp,
                "scheduled_at": scheduled_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "city_filter": city_filter,
                "days_filter": days_filter,
            }
            campaigns.append(new_campaign_record)
            save_campaigns(campaigns)
            flash(
                f"Campaign scheduled for {scheduled_dt.strftime('%b %d, %Y at %I:%M %p')} "
                f"— {len(recipients)} contact(s) will be re-evaluated at send time.",
                "success",
            )
            return redirect(url_for("campaign_detail", campaign_id=campaign_id))

        vonage_ready = vonage_is_configured()
        mode = "live" if vonage_ready else "simulation"

        # If something is already sending, this one waits its turn instead of
        # being blocked outright — the background scheduler will start it
        # automatically the moment the active send finishes (or is stopped).
        should_queue = get_in_progress_campaign() is not None
        initial_status = "queued" if should_queue else "in_progress"

        new_campaign_record = {
            "id": campaign_id,
            "template_id": template_id,
            "template_name": target["name"],
            "total_recipients": str(len(recipients)),
            "sent": "0",
            "failed": "0",
            "status": initial_status,
            "mode": mode,
            "created_at": timestamp,
            "scheduled_at": "",
            "city_filter": city_filter,
            "days_filter": days_filter,
        }
        campaigns.append(new_campaign_record)
        save_campaigns(campaigns)

        if should_queue:
            flash(
                f"Another campaign is currently sending, so this one ({len(recipients)} contacts) "
                f"has been queued and will start automatically as soon as it's free.",
                "success",
            )
        else:
            start_campaign_send(campaign_id, target["body"], recipients, mode)
            flash(
                f"Campaign started for {len(recipients)} contacts. "
                f"Estimated time: {estimate_send_duration(len(recipients))}. "
                f"You can safely leave this page — progress is saved.",
                "success",
            )
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    return render_template(
        "new_campaign.html",
        active="campaigns",
        templates=all_templates,
        active_contacts_count=len(active_contacts),
        vonage_ready=vonage_is_configured(),
        estimated_duration=estimate_send_duration(len(active_contacts)) if active_contacts else "",
        send_rate=config.SEND_RATE_PER_SECOND,
        cities_summary=get_cities_summary(all_contacts),
        in_progress=in_progress,
    )


@app.route("/campaigns/<campaign_id>")
def campaign_detail(campaign_id):
    campaigns = load_campaigns()
    target = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not target:
        flash("Campaign not found.", "error")
        return redirect(url_for("campaigns_page"))

    log_entries = []
    if os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            log_entries = [r for r in reader if r["campaign_id"] == campaign_id]

    failed_count = sum(1 for e in log_entries if e["status"] == "failed")

    return render_template(
        "campaign_detail.html",
        active="campaigns",
        campaign=target,
        log_entries=list(reversed(log_entries))[:100],
        log_total=len(log_entries),
        has_failed=failed_count > 0,
    )


@app.route("/campaigns/<campaign_id>/status")
def campaign_status(campaign_id):
    campaigns = load_campaigns()
    target = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not target:
        return jsonify({"error": "not_found"}), 404
    return jsonify({
        "status": target["status"],
        "sent": int(target["sent"] or 0),
        "failed": int(target["failed"] or 0),
        "total_recipients": int(target["total_recipients"] or 0),
    })


@app.route("/campaigns/<campaign_id>/cancel", methods=["POST"])
def cancel_campaign(campaign_id):
    campaigns = load_campaigns()
    target = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not target or target.get("status") not in ("scheduled", "queued", "in_progress"):
        flash("Only scheduled, queued, or in-progress campaigns can be cancelled.", "error")
        return redirect(url_for("campaigns_page"))

    if target.get("status") in ("scheduled", "queued"):
        update_campaign_record(campaign_id, status="cancelled")
        flash("Campaign cancelled.", "success")
    else:
        # Manually stopping a genuinely stuck/running campaign. Its background
        # thread (if still alive) will simply overwrite this on its next
        # progress save — that's fine, this is meant as a manual unstick
        # tool for campaigns that appear frozen.
        update_campaign_record(campaign_id, status="interrupted")
        flash("Campaign stopped and marked as interrupted.", "success")

    return redirect(url_for("campaigns_page"))


@app.route("/campaigns/<campaign_id>/delete", methods=["POST"])
def delete_campaign(campaign_id):
    campaigns = load_campaigns()
    target = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not target:
        flash("Campaign not found.", "error")
        return redirect(url_for("campaigns_page"))

    if target.get("status") == "in_progress":
        flash("Cannot delete a campaign that is currently sending. Cancel it first.", "error")
        return redirect(url_for("campaigns_page"))

    remaining = [c for c in campaigns if c["id"] != campaign_id]
    save_campaigns(remaining)
    flash("Campaign deleted.", "success")
    return redirect(url_for("campaigns_page"))


@app.route("/campaigns/<campaign_id>/resend-failed", methods=["POST"])
def resend_failed(campaign_id):
    campaigns = load_campaigns()
    original = next((c for c in campaigns if c["id"] == campaign_id), None)
    if not original:
        flash("Campaign not found.", "error")
        return redirect(url_for("campaigns_page"))

    in_progress = get_in_progress_campaign()
    if in_progress:
        flash("A campaign is already sending. Please wait for it to finish before starting another.", "warn")
        return redirect(url_for("campaign_detail", campaign_id=in_progress["id"]))

    failed_phones = set()
    if os.path.exists(config.LOG_FILE):
        with open(config.LOG_FILE, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["campaign_id"] == campaign_id and row["status"] == "failed":
                    failed_phones.add(row["phone"])

    all_templates = load_templates()
    template = next((t for t in all_templates if t["id"] == original["template_id"]), None)
    if not template:
        flash("The original template no longer exists.", "error")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    all_contacts = load_contacts()
    recipients = [c for c in all_contacts if c["phone"] in failed_phones and c.get("opted_out") != "True"]

    if not recipients:
        flash("No failed contacts to resend to (they may have opted out since).", "error")
        return redirect(url_for("campaign_detail", campaign_id=campaign_id))

    vonage_ready = vonage_is_configured()
    mode = "live" if vonage_ready else "simulation"

    campaigns_all = load_campaigns()
    new_id = next_campaign_id(campaigns_all)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    new_record = {
        "id": new_id,
        "template_id": original["template_id"],
        "template_name": f"{original['template_name']} (resend)",
        "total_recipients": str(len(recipients)),
        "sent": "0",
        "failed": "0",
        "status": "in_progress",
        "mode": mode,
        "created_at": timestamp,
        "scheduled_at": "",
        "city_filter": "",
        "days_filter": "",
    }
    campaigns_all.append(new_record)
    save_campaigns(campaigns_all)

    start_campaign_send(new_id, template["body"], recipients, mode)

    flash(f"Resending to {len(recipients)} previously failed contact(s).", "success")
    return redirect(url_for("campaign_detail", campaign_id=new_id))


@app.route("/analytics")
def analytics_page():
    all_campaigns = load_campaigns()
    completed = [c for c in all_campaigns if c.get("status") == "completed"]

    by_date = {}
    for c in completed:
        date_key = (c.get("created_at") or "")[:10]
        if not date_key:
            continue
        entry = by_date.setdefault(date_key, {"sent": 0, "failed": 0})
        entry["sent"] += int(c.get("sent") or 0)
        entry["failed"] += int(c.get("failed") or 0)

    sorted_dates = sorted(by_date.keys())
    chart_labels = sorted_dates
    chart_sent = [by_date[d]["sent"] for d in sorted_dates]
    chart_failed = [by_date[d]["failed"] for d in sorted_dates]

    total_sent = sum(int(c.get("sent") or 0) for c in completed)
    total_failed = sum(int(c.get("failed") or 0) for c in completed)
    total_attempted = total_sent + total_failed
    overall_rate = round((total_sent / total_attempted) * 100, 1) if total_attempted else 0

    campaigns_sorted = sorted(completed, key=lambda c: int(c["id"]), reverse=True)

    return render_template(
        "analytics.html",
        active="analytics",
        chart_labels=chart_labels,
        chart_sent=chart_sent,
        chart_failed=chart_failed,
        total_sent=total_sent,
        total_failed=total_failed,
        overall_rate=overall_rate,
        campaigns=campaigns_sorted[:50],
    )


# Runs once when the app process starts (including after every restart or
# redeploy). Any campaign left "in_progress" from a previous process is
# honestly marked as interrupted rather than shown as still running forever.
mark_stale_campaigns_interrupted()

# Background thread that checks for due scheduled campaigns every 30s.
threading.Thread(target=run_scheduler, daemon=True).start()


def mask_secret(value):
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        api_key = request.form.get("vonage_api_key", "").strip()
        api_secret = request.form.get("vonage_api_secret", "").strip()
        from_number = request.form.get("vonage_from_number", "").strip()
        anthropic_key = request.form.get("anthropic_api_key", "").strip()

        current = load_settings()
        # If the masked value was left unchanged, keep the existing secret
        if api_key and not api_key.startswith("*"):
            current["vonage_api_key"] = api_key
        if api_secret and not api_secret.startswith("*"):
            current["vonage_api_secret"] = api_secret
        if from_number:
            current["vonage_from_number"] = from_number
        if anthropic_key and not anthropic_key.startswith("*"):
            current["anthropic_api_key"] = anthropic_key

        save_settings(current)
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))

    s = load_settings()
    display_settings = {
        "vonage_api_key": mask_secret(s["vonage_api_key"]),
        "vonage_api_secret": mask_secret(s["vonage_api_secret"]),
        "vonage_from_number": s["vonage_from_number"],
        "anthropic_api_key": mask_secret(s.get("anthropic_api_key", "")),
    }
    return render_template(
        "settings.html",
        active="settings",
        settings=display_settings,
        vonage_ready=vonage_is_configured(),
        ai_ready=anthropic_is_configured(),
        max_chars=config.MENSAJE_MAX_CHARS,
    )


@app.route("/settings/test", methods=["POST"])
def test_vonage_connection():
    if not vonage_is_configured():
        flash("Please save your Vonage credentials before testing.", "error")
        return redirect(url_for("settings_page"))

    s = load_settings()
    try:
        import vonage
        auth = vonage.Auth(api_key=s["vonage_api_key"], api_secret=s["vonage_api_secret"])
        client = vonage.Vonage(auth)
        balance = client.account.get_balance()
        flash(f"Connection successful. Account balance: {balance.value} {balance.auto_reload and '(auto-reload on)' or ''}", "success")
    except Exception as e:
        flash(f"Connection failed: {e}", "error")

    return redirect(url_for("settings_page"))


OPT_OUT_KEYWORDS = {"stop", "unsubscribe", "cancel", "baja", "salir"}


@app.route("/optouts")
def optouts_page():
    all_contacts = load_contacts()
    opted_out = [c for c in all_contacts if c.get("opted_out") == "True"]

    search = request.args.get("search", "").strip().lower()
    if search:
        opted_out = [
            c for c in opted_out
            if search in c.get("name", "").lower() or search in c.get("phone", "").lower()
        ]

    return render_template(
        "optouts.html",
        active="optouts",
        optouts=opted_out,
        total_contacts=len(all_contacts),
        search=search,
    )


@app.route("/optouts/add", methods=["POST"])
def add_optout_manual():
    phone = normalize_phone(request.form.get("phone", "").strip())
    if not phone:
        flash("Please enter a valid phone number.", "error")
        return redirect(url_for("optouts_page"))

    all_contacts = load_contacts()
    target = next((c for c in all_contacts if c["phone"] == phone), None)

    if target:
        target["opted_out"] = "True"
        save_contacts(all_contacts)
        flash(f"{phone} marked as opted out.", "success")
    else:
        # Not an existing contact — add a minimal opted-out record so it's
        # never imported or messaged in the future.
        all_contacts.append({
            "name": "",
            "phone": phone,
            "opted_out": "True",
            "consent_source": "Manual opt-out",
            "consent_date": datetime.now().strftime("%Y-%m-%d"),
        })
        save_contacts(all_contacts)
        flash(f"{phone} added to opt-out list.", "success")

    return redirect(url_for("optouts_page"))


@app.route("/optouts/remove/<path:phone>", methods=["POST"])
def remove_optout(phone):
    all_contacts = load_contacts()
    target = next((c for c in all_contacts if c["phone"] == phone), None)

    if not target:
        flash("Contact not found.", "error")
    else:
        target["opted_out"] = "False"
        save_contacts(all_contacts)
        flash(f"{phone} re-subscribed.", "success")

    return redirect(url_for("optouts_page"))


@app.route("/webhooks/inbound-sms", methods=["GET", "POST"])
def inbound_sms_webhook():
    """Vonage calls this URL whenever someone replies to an SMS.
    Vonage accounts can be configured to call this via GET or POST, so both are supported.
    If the reply matches an opt-out keyword, the contact is opted out automatically."""
    if request.method == "GET":
        data = request.args.to_dict()
    else:
        data = request.get_json(silent=True) or request.form.to_dict()

    from_number = normalize_phone(data.get("msisdn") or data.get("from", ""))
    text = (data.get("text") or "").strip().lower()

    if from_number and text in OPT_OUT_KEYWORDS:
        all_contacts = load_contacts()
        target = next((c for c in all_contacts if c["phone"] == from_number), None)
        if target:
            target["opted_out"] = "True"
        else:
            all_contacts.append({
                "name": "",
                "phone": from_number,
                "opted_out": "True",
                "consent_source": "Auto opt-out (SMS reply)",
                "consent_date": datetime.now().strftime("%Y-%m-%d"),
            })
        save_contacts(all_contacts)

    return ("", 204)


if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "false").lower() in ("1", "true", "yes")
    port = int(os.getenv("PORT", 5000))
    app.run(debug=debug_mode, host="0.0.0.0", port=port)
