import logging
import datetime
import json
import re
from flask import Flask, render_template, request, jsonify, redirect, url_for
from app.config import Config, load_env
from app.database import init_db, get_db
from app.services.silvasoft_client import silvasoft_client
from app.services.hubspot_client import hubspot_client
from app.services.matching_service import matching_service
from app.services.diff_service import diff_service
from app.services.queue_worker import QueueService, start_queue_worker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("zwol_sync")

load_env()
app = Flask(__name__)
app.config["SECRET_KEY"] = Config.SECRET_KEY


@app.context_processor
def inject_globals():
    return {
        "hubspot_portal_id": Config.HUBSPOT_PORTAL_ID
    }


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "zwol-sync", "time": datetime.datetime.now().isoformat()})


@app.route("/")
def index():
    filter_type = request.args.get("filter", "diffs")
    data = diff_service.get_comparison_data(filter_type=filter_type)
    queue_summary = QueueService.get_queue_summary()
    return render_template(
        "dashboard.html",
        stats=data["stats"],
        companies=data["companies"],
        active_filter=filter_type,
        queue_summary=queue_summary
    )


@app.route("/newsletter")
def page_newsletter():
    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        companies = db.fetchall(f"""
            SELECT hubspot_id, name, company_type, contacts_json, synced_at
            FROM {prefix}hubspot_cache
            ORDER BY name ASC
        """)

    all_contacts = []
    stats = {
        "total_contacts": 0,
        "subscribed": 0,
        "never_subscribed": 0,
        "unsubscribed": 0,
        "without_type": 0,
        "total_companies": len(companies)
    }

    type_counts = {}
    type_labels_map = hubspot_client.get_company_type_labels_map()
    type_options = hubspot_client.get_company_type_options()

    for c in companies:
        c_type = (c.get("company_type") or "").strip()
        type_label = type_labels_map.get(c_type, c_type) if c_type else "Geen type ingesteld"
        if not c_type:
            stats["without_type"] += 1

        contacts_raw = c.get("contacts_json") or "[]"
        try:
            contacts = json.loads(contacts_raw)
        except Exception:
            contacts = []

        if not contacts:
            continue

        if c_type:
            type_counts[type_label] = type_counts.get(type_label, 0) + len(contacts)

        for ct in contacts:
            c_email = (ct.get("email") or "").strip()
            c_name = (ct.get("name") or "").strip() or "(Naamloos contact)"
            c_status = ct.get("newsletter_status") or "NOT_SPECIFIED"

            stats["total_contacts"] += 1
            if c_status == "SUBSCRIBED":
                stats["subscribed"] += 1
            elif c_status == "UNSUBSCRIBED":
                stats["unsubscribed"] += 1
            else:
                stats["never_subscribed"] += 1

            all_contacts.append({
                "company_id": c.get("hubspot_id"),
                "company_name": c.get("name"),
                "company_type": c_type,
                "company_type_label": type_label,
                "has_type": bool(c_type),
                "contact_id": ct.get("id"),
                "name": c_name,
                "email": c_email,
                "phone": ct.get("phone") or "",
                "job_title": ct.get("job_title") or "",
                "is_primary": bool(ct.get("is_primary")),
                "newsletter_status": c_status
            })

    # Combineer typen met contacten en alle beschikbare opties uit HubSpot
    all_available_type_labels = set(type_counts.keys()).union({opt["label"] for opt in type_options if opt.get("label")})

    return render_template(
        "newsletter.html",
        contacts=all_contacts,
        stats=stats,
        company_types=sorted(all_available_type_labels),
        type_options=type_options
    )


@app.route("/queue")
def page_queue():
    queue_summary = QueueService.get_queue_summary()
    return render_template("queue.html", queue=queue_summary)


@app.route("/history")
def page_history():
    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        history = db.fetchall(f"SELECT * FROM {prefix}history ORDER BY id DESC LIMIT 100")
    return render_template("history.html", history=history)


# --------------------------------------------------------------------------
# API ENDPOINTS
# --------------------------------------------------------------------------

@app.route("/api/sync/silvasoft", methods=["POST"])
def api_sync_silvasoft():
    """Haalt alle zakelijke klanten op uit Silvasoft."""
    try:
        count = silvasoft_client.sync_companies_to_cache()
        match_stats = matching_service.run_auto_matching()
        return jsonify({
            "success": True,
            "message": f"{count} zakelijke klanten gesynchroniseerd uit Silvasoft.",
            "match_stats": match_stats
        })
    except Exception as e:
        logger.error(f"Fout bij synchroniseren Silvasoft: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync/hubspot", methods=["POST"])
def api_sync_hubspot():
    """Haalt alle bedrijven op uit HubSpot."""
    try:
        count = hubspot_client.sync_companies_to_cache()
        match_stats = matching_service.run_auto_matching()
        return jsonify({
            "success": True,
            "message": f"{count} bedrijven gesynchroniseerd uit HubSpot.",
            "match_stats": match_stats
        })
    except Exception as e:
        logger.error(f"Fout bij synchroniseren HubSpot: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/sync/all", methods=["POST"])
def api_sync_all():
    """Ververst beide caches en draait de matching-engine."""
    try:
        ss_cnt = silvasoft_client.sync_companies_to_cache()
        hs_cnt = hubspot_client.sync_companies_to_cache()
        match_stats = matching_service.run_auto_matching()
        return jsonify({
            "success": True,
            "message": f"{ss_cnt} Silvasoft klanten en {hs_cnt} HubSpot bedrijven bijgewerkt.",
            "match_stats": match_stats
        })
    except Exception as e:
        logger.error(f"Fout bij volledige sync: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/match/auto", methods=["POST"])
def api_run_matching():
    """Draait de automatische match-engine opnieuw."""
    try:
        res = matching_service.run_auto_matching()
        return jsonify({"success": True, "stats": res})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/match/manual", methods=["POST"])
def api_manual_match():
    """Handmatig koppelen van een Silvasoft-klant aan een HubSpot-bedrijf."""
    data = request.get_json() or {}
    ss_no = data.get("silvasoft_customer_number")
    hs_id = data.get("hubspot_id")
    if not ss_no or not hs_id:
        return jsonify({"success": False, "error": "Klantnummer en HubSpot-ID zijn verplicht."}), 400

    try:
        matching_service.manual_match(str(ss_no), str(hs_id))
        return jsonify({"success": True, "message": "Succesvol gekoppeld!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/match/unmatch", methods=["POST"])
def api_unmatch():
    data = request.get_json() or {}
    ss_no = data.get("silvasoft_customer_number")
    if not ss_no:
        return jsonify({"success": False, "error": "Klantnummer is verplicht."}), 400

    try:
        matching_service.unmatch(str(ss_no))
        return jsonify({"success": True, "message": "Koppeling verbroken."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/contacts/ignore", methods=["POST"])
def api_ignore_contact():
    data = request.get_json() or {}
    cust_no = str(data.get("customer_number") or "")
    hs_id = str(data.get("hubspot_id") or "")
    contact_id = str(data.get("contact_id") or "")
    name = data.get("contact_name") or ""
    email = data.get("contact_email") or ""

    if not cust_no or not contact_id:
        return jsonify({"success": False, "error": "Klantnummer en contact-ID zijn verplicht."}), 400

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        db.execute(f"""
            INSERT INTO {prefix}ignored_contacts (customer_number, hubspot_id, contact_id, contact_name, contact_email, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE contact_name = VALUES(contact_name), contact_email = VALUES(contact_email)
        """, (cust_no, hs_id, contact_id, name, email, now_str))

    return jsonify({"success": True, "message": f"Contactpersoon '{name}' uitgesloten van synchronisatie."})


@app.route("/api/contacts/unignore", methods=["POST"])
def api_unignore_contact():
    data = request.get_json() or {}
    cust_no = str(data.get("customer_number") or "")
    contact_id = str(data.get("contact_id") or "")

    if not cust_no or not contact_id:
        return jsonify({"success": False, "error": "Klantnummer en contact-ID zijn verplicht."}), 400

    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        db.execute(f"DELETE FROM {prefix}ignored_contacts WHERE customer_number = %s AND contact_id = %s", (cust_no, contact_id))

    return jsonify({"success": True, "message": "Contactpersoon hersteld voor synchronisatie."})


@app.route("/api/companies/ignore", methods=["POST"])
def api_ignore_company():
    data = request.get_json() or {}
    cust_no = str(data.get("customer_number") or "").strip()
    company_name = str(data.get("name") or "").strip()
    reason = str(data.get("reason") or "user_ignored").strip()

    if not cust_no:
        return jsonify({"success": False, "error": "Klantnummer is verplicht."}), 400

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        if db.is_sqlite:
            db.execute(f"""
                INSERT INTO {prefix}ignored_companies (customer_number, company_name, reason, created_at)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(customer_number) DO UPDATE SET
                    company_name = excluded.company_name,
                    reason = excluded.reason,
                    created_at = excluded.created_at
            """, (cust_no, company_name, reason, now_str))
        else:
            db.execute(f"""
                INSERT INTO `{prefix}ignored_companies` (`customer_number`, `company_name`, `reason`, `created_at`)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `company_name` = VALUES(`company_name`),
                    `reason` = VALUES(`reason`),
                    `created_at` = VALUES(`created_at`)
            """, (cust_no, company_name, reason, now_str))

        db.execute(f"""
            INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
            VALUES (%s, %s, %s, %s)
        """, (company_name or f"Klant #{cust_no}", "SILVASOFT", "Bedrijf genegeerd voor HubSpot synchronisatie", now_str))

    return jsonify({"success": True, "message": f"Bedrijf '{company_name}' succesvol genegeerd."})


@app.route("/api/companies/unignore", methods=["POST"])
def api_unignore_company():
    data = request.get_json() or {}
    cust_no = str(data.get("customer_number") or "").strip()

    if not cust_no:
        return jsonify({"success": False, "error": "Klantnummer is verplicht."}), 400

    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        row = db.fetchone(f"SELECT company_name FROM {prefix}ignored_companies WHERE customer_number = %s", (cust_no,))
        company_name = row.get("company_name") if row else f"Klant #{cust_no}"
        db.execute(f"DELETE FROM {prefix}ignored_companies WHERE customer_number = %s", (cust_no,))

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(f"""
            INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
            VALUES (%s, %s, %s, %s)
        """, (company_name, "SILVASOFT", "Bedrijf hersteld voor synchronisatie", now_str))

    return jsonify({"success": True, "message": f"Bedrijf '{company_name}' hersteld voor synchronisatie."})



@app.route("/api/queue/apply-field", methods=["POST"])
def api_apply_field():
    """
    Plaatst één veldwijziging in de wachtrij.
    target: 'HUBSPOT' of 'SILVASOFT'
    """
    data = request.get_json() or {}
    target = data.get("target")
    action = data.get("action", "UPDATE_FIELD")
    entity_id = data.get("entity_id")
    field_name = data.get("field_name")
    new_val = data.get("new_value")
    old_val = data.get("old_value", "")
    payload = data.get("payload")

    if not target or not entity_id:
        return jsonify({"success": False, "error": "Target en entity_id zijn verplicht."}), 400

    try:
        task_id = QueueService.enqueue(
            target=target,
            action=action,
            entity_id=str(entity_id),
            field_name=field_name,
            old_val=old_val,
            new_val=new_val,
            payload=payload
        )
        msg = "Direct bijgewerkt in HubSpot!" if target == "HUBSPOT" else "In de Silvasoft wachtrij geplaatst!"
        return jsonify({"success": True, "task_id": task_id, "message": msg})
    except Exception as e:
        logger.error(f"Fout bij enqueue: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/queue/create-in-hubspot", methods=["POST"])
def api_create_in_hubspot():
    """Maakt een nieuw bedrijf aan in HubSpot op basis van de Silvasoft data."""
    data = request.get_json() or {}
    cust_no = data.get("customer_number")
    if not cust_no:
        return jsonify({"success": False, "error": "Klantnummer is verplicht."}), 400

    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        # 1. Controleer of klant al gekoppeld is in sync_matches
        already_matched = db.fetchone(
            f"SELECT hubspot_id FROM {prefix}matches WHERE silvasoft_customer_number = %s",
            (str(cust_no),)
        )
        if already_matched:
            return jsonify({
                "success": False,
                "error": f"Klant #{cust_no} is al gekoppeld aan HubSpot bedrijf #{already_matched['hubspot_id']}."
            }), 400

        # 2. Controleer of er al een actieve CREATE_COMPANY taak in de wachtrij staat
        pending_task = db.fetchone(
            f"SELECT id FROM {prefix}queue WHERE target = 'HUBSPOT' AND action = 'CREATE_COMPANY' AND entity_id = %s AND status IN ('PENDING', 'PROCESSING')",
            (str(cust_no),)
        )
        if pending_task:
            return jsonify({
                "success": False,
                "error": f"Aanmaken van klant #{cust_no} staat al in de wachtrij (taak #{pending_task['id']})."
            }), 400

        ss = db.fetchone(f"SELECT * FROM {prefix}silvasoft_cache WHERE customer_number = %s", (str(cust_no),))

    if not ss:
        return jsonify({"success": False, "error": "Klant niet gevonden in Silvasoft cache."}), 404

    from app.services.diff_service import format_silvasoft_address
    street_combined = format_silvasoft_address(ss.get("street"), ss.get("unit"), ss.get("unit_appendix"))

    # Domeinnaam bepalen uit website of primaire contactpersoon
    domain = None
    def _clean_domain(val):
        if not val:
            return None
        v = str(val).strip().lower()
        if "@" in v:
            dom = v.split("@")[-1].strip()
        else:
            dom = re.sub(r"^https?://", "", v)
            dom = re.sub(r"^www\.", "", dom)
            dom = dom.split("/")[0].split("?")[0].strip()
        public_mail = {"gmail.com", "hotmail.com", "outlook.com", "live.nl", "yahoo.com", "icloud.com"}
        if dom and "." in dom and dom not in public_mail:
            return dom
        return None

    if ss.get("website"):
        domain = _clean_domain(ss["website"])
    if not domain and ss.get("contacts_json"):
        try:
            for ct in json.loads(ss["contacts_json"] or "[]"):
                m = (ct.get("email") or "").strip()
                d = _clean_domain(m)
                if d:
                    domain = d
                    break
        except Exception:
            pass

    raw_payload = {
        "name": ss.get("name") or "",
        "address": street_combined,
        "zip": ss.get("postal_code") or "",
        "city": ss.get("city") or "",
        "phone": ss.get("phone") or "",
        "website": ss.get("website") or "",
        "silvasoft_customer_number": str(cust_no)
    }
    if domain:
        raw_payload["domain"] = domain
    if ss.get("kvk_number"):
        raw_payload["kvk_nummer"] = ss.get("kvk_number")

    payload = {k: str(v).strip() for k, v in raw_payload.items() if v is not None and str(v).strip() != ""}

    try:
        task_id = QueueService.enqueue(
            target="HUBSPOT",
            action="CREATE_COMPANY",
            entity_id=str(cust_no),
            field_name=None,
            old_val=None,
            new_val=None,
            payload=payload
        )
        return jsonify({"success": True, "task_id": task_id, "message": f"Bedrijf '{ss['name']}' succesvol aangemaakt in HubSpot!"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/queue/status")
def api_queue_status():
    summary = QueueService.get_queue_summary()
    return jsonify({"success": True, "queue": summary})


@app.route("/api/companies/hubspot-search")
def api_search_hubspot():
    """Zoekt HubSpot bedrijven voor de handmatige koppel-autocomplete."""
    q = (request.args.get("q") or "").lower().strip()
    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        if not q:
            rows = db.fetchall(f"SELECT hubspot_id, name, city, postal_code, kvk_number FROM {prefix}hubspot_cache ORDER BY name ASC LIMIT 20")
        else:
            term = f"%{q}%"
            rows = db.fetchall(f"""
                SELECT hubspot_id, name, city, postal_code, kvk_number
                FROM {prefix}hubspot_cache
                WHERE name LIKE %s OR city LIKE %s OR kvk_number LIKE %s
                ORDER BY name ASC LIMIT 20
            """, (term, term, term))
    return jsonify({"success": True, "results": rows})


@app.route("/api/companies/update-type", methods=["POST"])
def api_update_company_type():
    """Werkt het bedrijfstype van een bedrijf direct bij in HubSpot en de lokale cache."""
    data = request.get_json() or {}
    company_id = str(data.get("company_id") or "").strip()
    new_type = str(data.get("type") or "").strip()

    if not company_id:
        return jsonify({"success": False, "error": "Bedrijf-ID is verplicht."}), 400

    try:
        # 1. Update in HubSpot
        hubspot_client.update_company(company_id, {"type": new_type})

        # 2. Update lokale cache
        prefix = Config.DB_TABLE_PREFIX
        company_name = ""
        with get_db() as db:
            row = db.fetchone(f"SELECT name FROM {prefix}hubspot_cache WHERE hubspot_id = %s", (company_id,))
            if row:
                company_name = row.get("name") or ""
            db.execute(
                f"UPDATE {prefix}hubspot_cache SET company_type = %s WHERE hubspot_id = %s",
                (new_type, company_id)
            )

        # 3. Log in geschiedenis
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        type_labels_map = hubspot_client.get_company_type_labels_map()
        type_label = type_labels_map.get(new_type, new_type) if new_type else "Geen type"
        with get_db() as db:
            db.execute(f"""
                INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
                VALUES (%s, %s, %s, %s)
            """, (company_name or f"Bedrijf #{company_id}", "HUBSPOT", f"Bedrijfstype gewijzigd naar '{type_label}'", now_str))

        return jsonify({
            "success": True,
            "company_id": company_id,
            "type": new_type,
            "type_label": type_label,
            "has_type": bool(new_type),
            "message": f"Bedrijfstype van '{company_name}' succesvol aangepast naar '{type_label}'!"
        })
    except Exception as e:
        logger.error(f"Fout bij bijwerken bedrijfstype #{company_id}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/newsletter/subscribe", methods=["POST"])
def api_newsletter_subscribe():
    """Schrijft één contactpersoon in voor de ZWOL Nieuwsbrief in HubSpot."""
    data = request.get_json() or {}
    email = (data.get("email") or "").strip()
    contact_name = data.get("contact_name") or ""
    company_name = data.get("company_name") or ""

    if not email:
        return jsonify({"success": False, "error": "E-mailadres is verplicht."}), 400

    prefix = Config.DB_TABLE_PREFIX
    # Strikte privacy-controle (AVG/GDPR): Mag NOOIT een zelf uitgeschreven contact overschrijven!
    with get_db() as db:
        rows = db.fetchall(f"SELECT contacts_json FROM {prefix}hubspot_cache WHERE contacts_json LIKE %s", (f"%{email}%",))
        for r in rows:
            try:
                for c in json.loads(r.get("contacts_json") or "[]"):
                    if (c.get("email") or "").strip().lower() == email.lower():
                        if c.get("newsletter_status") == "UNSUBSCRIBED":
                            return jsonify({
                                "success": False,
                                "error": "Deze contactpersoon heeft zich zelf afgemeld voor de nieuwsbrief. Conform de privacywetgeving (AVG) mag deze persoon niet handmatig worden aangemeld."
                            }), 400
            except Exception:
                pass

    try:
        hubspot_client.subscribe_contact(email)
        hubspot_client.update_contact_newsletter_status_in_cache(email, "SUBSCRIBED")

        # Registreer in geschiedenis
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        target_name = f"{company_name} - {contact_name} ({email})".strip(" -")
        with get_db() as db:
            db.execute(f"""
                INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
                VALUES (%s, %s, %s, %s)
            """, (target_name, "HUBSPOT", "Ingeschreven voor ZWOL Nieuwsbrief (123809007)", now_str))

        return jsonify({"success": True, "message": f"{email} succesvol ingeschreven voor de nieuwsbrief!"})
    except Exception as e:
        logger.error(f"Fout bij inschrijven {email}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/newsletter/bulk-subscribe", methods=["POST"])
def api_newsletter_bulk_subscribe():
    """Schrijft meerdere niet-ingeschreven contactpersonen in bulk in voor de ZWOL Nieuwsbrief."""
    data = request.get_json() or {}
    target_emails = data.get("emails")  # optionele lijst met specifieke e-mails

    prefix = Config.DB_TABLE_PREFIX
    with get_db() as db:
        companies = db.fetchall(f"SELECT contacts_json FROM {prefix}hubspot_cache")

    eligible_emails = set()
    unsubscribed_emails = set()

    for c in companies:
        try:
            contacts = json.loads(c.get("contacts_json") or "[]")
            for ct in contacts:
                em = (ct.get("email") or "").strip().lower()
                if not em:
                    continue
                st = ct.get("newsletter_status")
                if st == "UNSUBSCRIBED":
                    unsubscribed_emails.add(em)
                elif st == "NOT_SPECIFIED":
                    eligible_emails.add(em)
        except Exception:
            pass

    # Nooit uitgeschreven contacten toestaan
    eligible_emails = eligible_emails - unsubscribed_emails

    if target_emails is not None:
        selected_emails = [e.strip().lower() for e in target_emails if e.strip().lower() in eligible_emails]
    else:
        selected_emails = list(eligible_emails)

    if not selected_emails:
        return jsonify({"success": False, "error": "Geen niet-ingeschreven contacten gevonden om aan te melden."}), 400

    success_count = 0
    errors = []

    for em in selected_emails:
        try:
            hubspot_client.subscribe_contact(em)
            hubspot_client.update_contact_newsletter_status_in_cache(em, "SUBSCRIBED")
            success_count += 1
        except Exception as e:
            errors.append(f"{em}: {str(e)}")

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as db:
        db.execute(f"""
            INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
            VALUES (%s, %s, %s, %s)
        """, ("Bulk Inschrijving", "HUBSPOT", f"{success_count} contacten ingeschreven voor ZWOL Nieuwsbrief", now_str))

    return jsonify({
        "success": True,
        "subscribed_count": success_count,
        "errors_count": len(errors),
        "message": f"{success_count} contacten succesvol ingeschreven voor de nieuwsbrief!"
    })


# Initialisatie
try:
    init_db()
    start_queue_worker()
except Exception as e:
    logger.warning(f"Database initialisatie bij opstart: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=True)
