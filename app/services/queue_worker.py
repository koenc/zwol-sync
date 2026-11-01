import json
import time
import logging
import datetime
import threading
from typing import Dict, Any, Optional
from app.config import Config
from app.database import get_db
from app.services.silvasoft_client import silvasoft_client
from app.services.hubspot_client import hubspot_client

logger = logging.getLogger("zwol_sync")

_WORKER_THREAD = None
_STOP_EVENT = threading.Event()


class QueueService:
    @staticmethod
    def enqueue(target: str, action: str, entity_id: str, field_name: Optional[str],
                old_val: Optional[str], new_val: Optional[str], payload: Optional[dict] = None) -> int:
        """
        Voegt een taak toe aan de wachtrij.
        Als target HUBSPOT is, kan deze optioneel direct worden uitgevoerd.
        """
        prefix = Config.DB_TABLE_PREFIX
        now = datetime.datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        payload_str = json.dumps(payload or {})

        # Silvasoft taken krijgen een cooldown
        with get_db() as db:
            last_ss = db.fetchone(
                f"SELECT MAX(scheduled_at) as max_sched FROM {prefix}queue WHERE target = 'SILVASOFT' AND status = 'PENDING'"
            )
            max_sched = last_ss.get("max_sched") if last_ss else None

            if target == "SILVASOFT":
                if max_sched and isinstance(max_sched, str):
                    try:
                        max_dt = datetime.datetime.strptime(max_sched, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        max_dt = now
                elif max_sched and isinstance(max_sched, datetime.datetime):
                    max_dt = max_sched
                else:
                    max_dt = now

                next_sched = max(now, max_dt + datetime.timedelta(seconds=Config.SILVASOFT_QUEUE_INTERVAL_SECONDS))
                sched_str = next_sched.strftime("%Y-%m-%d %H:%M:%S")
            else:
                sched_str = now_str

            cur = db.execute(f"""
                INSERT INTO {prefix}queue (
                    target, action, entity_id, field_name, old_value, new_value,
                    payload_json, status, attempts, error_message, created_at, scheduled_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', 0, NULL, %s, %s)
            """, (
                target, action, entity_id, field_name, old_val, new_val,
                payload_str, now_str, sched_str
            ))
            task_id = cur.lastrowid

        # Als het een HubSpot taak is, voer direct synchroon uit voor instant feedback
        if target == "HUBSPOT":
            try:
                QueueService.process_task(task_id)
            except Exception as e:
                logger.warning(f"Directe uitvoering van HubSpot taak {task_id} mislukt: {e}")

        return task_id

    @staticmethod
    def process_task(task_id: int):
        prefix = Config.DB_TABLE_PREFIX
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as db:
            task = db.fetchone(f"SELECT * FROM {prefix}queue WHERE id = %s", (task_id,))
            if not task or task.get("status") in ("SUCCESS", "PROCESSING"):
                return

            db.execute(f"UPDATE {prefix}queue SET status = 'PROCESSING' WHERE id = %s", (task_id,))

        target = task.get("target")
        action = task.get("action")
        entity_id = task.get("entity_id")
        field = task.get("field_name")
        new_val = task.get("new_value")
        payload = json.loads(task.get("payload_json") or "{}")

        try:
            if target == "HUBSPOT":
                if action == "UPDATE_FIELD":
                    hs_field_map = {
                        "name": "name",
                        "kvk_number": "kvk_nummer",
                        "street": "address",
                        "postal_code": "zip",
                        "city": "city",
                        "phone": "phone",
                        "website": "website"
                    }
                    hs_prop = hs_field_map.get(field, field)
                    hubspot_client.update_company(entity_id, {hs_prop: new_val})

                elif action == "CREATE_COMPANY":
                    # Maak bedrijf aan in HubSpot vanuit Silvasoft gegevens
                    res = hubspot_client.create_company(payload)
                    new_hs_id = res.get("id")
                    if new_hs_id:
                        from app.services.matching_service import matching_service
                        matching_service.manual_match(entity_id, str(new_hs_id))
                        hubspot_client.sync_companies_to_cache()

                elif action == "ADD_CONTACT":
                    hubspot_client.create_and_associate_contact(entity_id, payload)

                elif action == "UPDATE_CONTACT_PHONE":
                    hubspot_client.update_contact(entity_id, {"phone": new_val})


            elif target == "SILVASOFT":
                if action == "UPDATE_FIELD":
                    import re
                    # Zoek relation_guid op in silvasoft_cache
                    with get_db() as db:
                        ss = db.fetchone(
                            f"SELECT relation_guid FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    rel_guid = ss.get("relation_guid") if ss else None

                    ss_payload = {}
                    if rel_guid:
                        try:
                            ss_payload["RelationGUID"] = int(rel_guid)
                        except (ValueError, TypeError):
                            ss_payload["RelationGUID"] = rel_guid

                    if field == "street":
                        m = re.match(r'^(.*?)\s+(\d+)\s*(.*)$', (new_val or "").strip())
                        if m:
                            s_name, unit, app = m.group(1), m.group(2), m.group(3)
                        else:
                            s_name, unit, app = (new_val or "").strip(), "", ""

                        ss_payload["Address_Street"] = s_name
                        ss_payload["Address_Unit"] = unit
                        ss_payload["Address_UnitAppendix"] = app

                        silvasoft_client.update_company(str(entity_id), ss_payload)

                        with get_db() as db:
                            db.execute(
                                f"UPDATE {prefix}silvasoft_cache SET street = %s, unit = %s, unit_appendix = %s WHERE customer_number = %s",
                                (s_name, unit, app, str(entity_id))
                            )
                    else:
                        ss_field_map = {
                            "name": "Name",
                            "postal_code": "Address_PostalCode",
                            "city": "Address_City",
                            "phone": "Phone",
                            "website": "Website",
                            "kvk_number": "RegistrationNumber",
                            "email": "Email"
                        }
                        ss_key = ss_field_map.get(field, field)
                        ss_payload[ss_key] = new_val

                        silvasoft_client.update_company(str(entity_id), ss_payload)

                        with get_db() as db:
                            db.execute(
                                f"UPDATE {prefix}silvasoft_cache SET {field} = %s WHERE customer_number = %s",
                                (new_val, str(entity_id))
                            )

                elif action == "UPDATE_CONTACT_PHONE":
                    c_guid = payload.get("contact_guid")
                    phone_val = str(new_val or "")
                    with get_db() as db:
                        ss = db.fetchone(
                            f"SELECT relation_guid FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    rel_guid = ss.get("relation_guid") if ss else None

                    ss_payload = {
                        "RelationGUID": int(rel_guid) if rel_guid else None,
                        "Relation_Contact": [
                            {
                                "ContactGUID": str(c_guid),
                                "Phone": phone_val
                            }
                        ]
                    }
                    silvasoft_client.update_company(str(entity_id), ss_payload)
                    with get_db() as db:
                        row = db.fetchone(f"SELECT contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s", (str(entity_id),))
                        if row:
                            cts = json.loads(row["contacts_json"] or "[]")
                            for ct in cts:
                                if ct.get("guid") == str(c_guid):
                                    ct["phone"] = phone_val
                            db.execute(f"UPDATE {prefix}silvasoft_cache SET contacts_json = %s WHERE customer_number = %s", (json.dumps(cts), str(entity_id)))

                elif action == "ADD_CONTACT":
                    c_name = (payload.get("name") or "").strip()
                    parts = c_name.split(" ")
                    first_name = parts[0] if parts else ""
                    last_name = " ".join(parts[1:]) if len(parts) > 1 else first_name
                    c_mail = payload.get("email") or ""
                    c_phone = payload.get("phone") or ""

                    with get_db() as db:
                        ss = db.fetchone(
                            f"SELECT relation_guid, contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    if not ss:
                        raise ValueError(f"Silvasoft relatie #{entity_id} niet gevonden.")

                    existing_cts = json.loads(ss.get("contacts_json") or "[]")
                    if existing_cts and len(existing_cts) > 0:
                        raise ValueError(
                            f"Silvasoft relatie #{entity_id} heeft al een primaire contactpersoon ('{existing_cts[0].get('name')}'). "
                            "Silvasoft staat via de REST API geen extra contactpersonen toe per relatie om overschrijving te voorkomen."
                        )

                    rel_guid = ss.get("relation_guid")

                    ss_payload = {
                        "RelationGUID": int(rel_guid) if rel_guid else None,
                        "Relation_Contact": [
                            {
                                "FirstName": first_name,
                                "LastName": last_name,
                                "Email": c_mail,
                                "Phone": c_phone,
                                "TypeContact": "person"
                            }
                        ]
                    }
                    resp = silvasoft_client.update_company(str(entity_id), ss_payload)
                    if resp and isinstance(resp, list) and len(resp) > 0:
                        raw_cts = resp[0].get("Relation_Contact") or []
                        clean = []
                        for c in raw_cts:
                            if c.get("TypeContact") == "department":
                                continue
                            c_f = (c.get("FirstName") or "").strip()
                            c_p = (c.get("Preposition") or c.get("Prefix") or "").strip()
                            c_l = (c.get("LastName") or c.get("FamilyName") or "").strip()
                            fn = " ".join([p for p in [c_f, c_p, c_l] if p])
                            clean.append({
                                "guid": str(c.get("ContactGUID") or ""),
                                "name": fn,
                                "first_name": c_f,
                                "last_name": c_l,
                                "email": (c.get("Email") or "").strip(),
                                "phone": (c.get("Phone") or c.get("Mobile") or "").strip(),
                                "is_primary": bool(c.get("DefaultContact"))
                            })
                        with get_db() as db:
                            db.execute(f"UPDATE {prefix}silvasoft_cache SET contacts_json = %s WHERE customer_number = %s", (json.dumps(clean), str(entity_id)))


            # Succes registreren
            with get_db() as db:
                db.execute(f"""
                    UPDATE {prefix}queue
                    SET status = 'SUCCESS', processed_at = %s, error_message = NULL
                    WHERE id = %s
                """, (now_str, task_id))

                db.execute(f"""
                    INSERT INTO {prefix}history (entity_name, target_system, change_summary, created_at)
                    VALUES (%s, %s, %s, %s)
                """, (
                    f"Klant #{entity_id}",
                    target,
                    f"Veld '{field}' gewijzigd naar '{new_val}'",
                    now_str
                ))

            logger.info(f"✅ Queue taak #{task_id} ({target} {action}) succesvol verwerkt.")

        except Exception as e:
            logger.error(f"❌ Fout bij verwerken queue taak #{task_id}: {e}")
            with get_db() as db:
                db.execute(f"""
                    UPDATE {prefix}queue
                    SET status = 'FAILED', attempts = attempts + 1, error_message = %s
                    WHERE id = %s
                """, (str(e), task_id))
            raise

    @staticmethod
    def get_queue_summary() -> Dict[str, Any]:
        prefix = Config.DB_TABLE_PREFIX
        with get_db() as db:
            pending = db.fetchall(f"""
                SELECT * FROM {prefix}queue
                WHERE status IN ('PENDING', 'PROCESSING')
                ORDER BY scheduled_at ASC
            """)
            recent_done = db.fetchall(f"""
                SELECT * FROM {prefix}queue
                WHERE status IN ('SUCCESS', 'FAILED')
                ORDER BY id DESC LIMIT 10
            """)
            ss_count = sum(1 for p in pending if p.get("target") == "SILVASOFT")
            hs_count = sum(1 for p in pending if p.get("target") == "HUBSPOT")

        return {
            "pending_count": len(pending),
            "silvasoft_pending": ss_count,
            "hubspot_pending": hs_count,
            "pending_tasks": pending,
            "recent_tasks": recent_done
        }


def _background_queue_worker():
    """Achtergrond thread die wachtrijtaken oppakt volgens schema."""
    logger.info("🕒 Achtergrond queue worker gestart.")
    prefix = Config.DB_TABLE_PREFIX

    while not _STOP_EVENT.is_set():
        try:
            now = datetime.datetime.now()
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")

            with get_db() as db:
                # Zoek oudste PENDING taak die klaar staat om te draaien
                next_task = db.fetchone(f"""
                    SELECT id FROM {prefix}queue
                    WHERE status = 'PENDING' AND scheduled_at <= %s
                    ORDER BY scheduled_at ASC LIMIT 1
                """, (now_str,))

            if next_task:
                task_id = next_task["id"]
                logger.info(f"Worker pakt taak #{task_id} op...")
                QueueService.process_task(task_id)

        except Exception as e:
            logger.warning(f"Queue worker loop fout: {e}")

        # Slaap 10 seconden
        for _ in range(10):
            if _STOP_EVENT.is_set():
                return
            time.sleep(1)


def start_queue_worker():
    global _WORKER_THREAD
    if _WORKER_THREAD is None or not _WORKER_THREAD.is_alive():
        _STOP_EVENT.clear()
        _WORKER_THREAD = threading.Thread(target=_background_queue_worker, daemon=True)
        _WORKER_THREAD.start()

