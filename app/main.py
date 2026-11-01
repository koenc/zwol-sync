import logging
import datetime
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
        ss = db.fetchone(f"SELECT * FROM {prefix}silvasoft_cache WHERE customer_number = %s", (str(cust_no),))

    if not ss:
        return jsonify({"success": False, "error": "Klant niet gevonden in Silvasoft cache."}), 404

    street_combined = ss.get("street") or ""
    if ss.get("unit"):
        street_combined = f"{street_combined} {ss.get('unit')}".strip()
    if ss.get("unit_appendix"):
        street_combined = f"{street_combined}{ss.get('unit_appendix')}".strip()

    payload = {
        "name": ss.get("name") or "",
        "address": street_combined,
        "zip": ss.get("postal_code") or "",
        "city": ss.get("city") or "",
        "phone": ss.get("phone") or "",
        "website": ss.get("website") or "",
        "silvasoft_customer_number": str(cust_no),
        "silvasoft_sync_status": "Actieve Klant"
    }
    if ss.get("kvk_number"):
        payload["kvk_nummer"] = ss.get("kvk_number")

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


# Initialisatie
try:
    init_db()
    start_queue_worker()
except Exception as e:
    logger.warning(f"Database initialisatie bij opstart: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=Config.PORT, debug=True)
