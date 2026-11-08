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

        with get_db() as db:
            cur = db.execute(f"""
                INSERT INTO {prefix}queue (
                    target, action, entity_id, field_name, old_value, new_value,
                    payload_json, status, attempts, error_message, created_at, scheduled_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'PENDING', 0, NULL, %s, %s)
            """, (
                target, action, entity_id, field_name, old_val, new_val,
                payload_str, now_str, now_str
            ))
            task_id = cur.lastrowid

        # Als het een HubSpot taak is, voer direct synchroon uit voor instant feedback
        if target == "HUBSPOT":
            QueueService.process_task(task_id)

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

                    # Update ook direct de lokale hubspot_cache voor instant weergave
                    hs_cache_cols = {
                        "name": "name",
                        "kvk_number": "kvk_number",
                        "street": "street",
                        "postal_code": "postal_code",
                        "city": "city",
                        "phone": "phone",
                        "website": "website"
                    }
                    c_col = hs_cache_cols.get(field)
                    if c_col:
                        with get_db() as db:
                            db.execute(
                                f"UPDATE {prefix}hubspot_cache SET {c_col} = %s WHERE hubspot_id = %s",
                                (new_val, str(entity_id))
                            )

                elif action == "CREATE_COMPANY":
                    # Maak bedrijf aan in HubSpot vanuit Silvasoft gegevens
                    res = hubspot_client.create_company(payload)
                    new_hs_id = res.get("id")
                    if new_hs_id:
                        from app.services.matching_service import matching_service
                        matching_service.manual_match(entity_id, str(new_hs_id))

                        # Koppel eventuele contactpersonen uit Silvasoft direct aan het nieuwe bedrijf
                        with get_db() as db:
                            ss = db.fetchone(f"SELECT contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s", (str(entity_id),))
                        if ss and ss.get("contacts_json"):
                            try:
                                for ct in json.loads(ss["contacts_json"] or "[]"):
                                    c_email = (ct.get("email") or "").strip()
                                    c_first = (ct.get("first_name") or "").strip()
                                    c_last = (ct.get("last_name") or "").strip()
                                    c_ph = (ct.get("phone") or "").strip()
                                    if c_email or c_first or c_last:
                                        hubspot_client.create_and_associate_contact(str(new_hs_id), {
                                            "first_name": c_first,
                                            "last_name": c_last,
                                            "email": c_email,
                                            "phone": c_ph
                                        })
                            except Exception as e_c:
                                logger.warning(f"Kon contactpersoon niet koppelen aan nieuw bedrijf #{new_hs_id}: {e_c}")

                        hubspot_client.sync_companies_to_cache()

                elif action == "ADD_CONTACT":
                    res = hubspot_client.create_and_associate_contact(entity_id, payload)
                    new_contact_id = res.get("id") if isinstance(res, dict) else ""
                    # Voeg direct toe aan contacts_json in sync_hubspot_cache
                    with get_db() as db:
                        row = db.fetchone(f"SELECT contacts_json FROM {prefix}hubspot_cache WHERE hubspot_id = %s", (str(entity_id),))
                        if row:
                            cts = json.loads(row["contacts_json"] or "[]")
                            if not any(str(c.get("id")) == str(new_contact_id) for c in cts):
                                cts.append({
                                    "id": str(new_contact_id),
                                    "name": payload.get("name") or f"{payload.get('first_name', '')} {payload.get('last_name', '')}".strip(),
                                    "first_name": payload.get("first_name", ""),
                                    "last_name": payload.get("last_name", ""),
                                    "email": payload.get("email", ""),
                                    "phone": payload.get("phone", ""),
                                    "is_primary": len(cts) == 0
                                })
                                db.execute(f"UPDATE {prefix}hubspot_cache SET contacts_json = %s WHERE hubspot_id = %s", (json.dumps(cts), str(entity_id)))

                elif action == "UPDATE_CONTACT_PHONE":
                    hubspot_client.update_contact(entity_id, {"phone": new_val})
                    # Update contact in hubspot cache
                    with get_db() as db:
                        row = db.fetchone(f"SELECT hubspot_id, contacts_json FROM {prefix}hubspot_cache WHERE contacts_json LIKE %s", (f"%{entity_id}%",))
                        if row:
                            cts = json.loads(row["contacts_json"] or "[]")
                            for ct in cts:
                                if str(ct.get("id")) == str(entity_id):
                                    ct["phone"] = new_val
                            db.execute(f"UPDATE {prefix}hubspot_cache SET contacts_json = %s WHERE hubspot_id = %s", (json.dumps(cts), row["hubspot_id"]))

                elif action == "UPDATE_CONTACT_EMAIL":
                    hubspot_client.update_contact(entity_id, {"email": new_val})
                    # Update contact in hubspot cache
                    with get_db() as db:
                        row = db.fetchone(f"SELECT hubspot_id, contacts_json FROM {prefix}hubspot_cache WHERE contacts_json LIKE %s", (f"%{entity_id}%",))
                        if row:
                            cts = json.loads(row["contacts_json"] or "[]")
                            for ct in cts:
                                if str(ct.get("id")) == str(entity_id):
                                    ct["email"] = new_val
                            db.execute(f"UPDATE {prefix}hubspot_cache SET contacts_json = %s WHERE hubspot_id = %s", (json.dumps(cts), row["hubspot_id"]))

                elif action == "UPDATE_CONTACT_NAME":
                    parts = (new_val or "").strip().split(" ", 1)
                    first_name = parts[0] if parts else ""
                    last_name = parts[1] if len(parts) > 1 else ""
                    hubspot_client.update_contact(entity_id, {"firstname": first_name, "lastname": last_name})
                    # Update contact in hubspot cache
                    with get_db() as db:
                        row = db.fetchone(f"SELECT hubspot_id, contacts_json FROM {prefix}hubspot_cache WHERE contacts_json LIKE %s", (f"%{entity_id}%",))
                        if row:
                            cts = json.loads(row["contacts_json"] or "[]")
                            for ct in cts:
                                if str(ct.get("id")) == str(entity_id):
                                    ct["name"] = (new_val or "").strip()
                                    ct["first_name"] = first_name
                                    ct["last_name"] = last_name
                            db.execute(f"UPDATE {prefix}hubspot_cache SET contacts_json = %s WHERE hubspot_id = %s", (json.dumps(cts), row["hubspot_id"]))


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
                        if payload and "Address_Street" in payload:
                            s_name = payload["Address_Street"]
                            unit = payload.get("Address_Unit") or ""
                            app = payload.get("Address_UnitAppendix") or ""
                        else:
                            m = re.match(r'^(.*?)\s+(\d+)\s*(.*)$', (new_val or "").strip())
                            if m:
                                s_name, unit, app = m.group(1), m.group(2), m.group(3)
                            else:
                                s_name, unit, app = (new_val or "").strip(), "", ""

                        # Voorkom dat Address_Unit al de toevoeging bevat als er een Address_UnitAppendix is
                        # Silvasoft voegt Address_UnitAppendix namelijk samen met Address_Unit
                        if unit and app:
                            unit_clean = re.sub(r'[\s\-]*' + re.escape(app) + r'\b', '', unit, flags=re.IGNORECASE).strip()
                            if unit_clean:
                                unit = unit_clean
                        elif unit and not app:
                            m_split = re.match(r'^(\d+)\s*([a-zA-Z0-9]+)$', unit)
                            if m_split:
                                unit = m_split.group(1)
                                app = m_split.group(2)

                        ss_payload["Address_Street"] = s_name
                        if unit:
                            ss_payload["Address_Unit"] = unit
                        if app:
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
                            f"SELECT relation_guid, contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    rel_guid = ss.get("relation_guid") if ss else None
                    if not c_guid and ss and ss.get("contacts_json"):
                        try:
                            cts = json.loads(ss.get("contacts_json") or "[]")
                            if cts and cts[0].get("guid"):
                                c_guid = cts[0].get("guid")
                        except Exception:
                            pass

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
                            db.execute(
                                f"UPDATE {prefix}silvasoft_cache SET contacts_json = %s, primary_contact_phone = %s WHERE customer_number = %s",
                                (json.dumps(cts), phone_val, str(entity_id))
                            )

                elif action == "UPDATE_CONTACT_EMAIL":
                    c_guid = payload.get("contact_guid")
                    email_val = str(new_val or "")
                    with get_db() as db:
                        ss = db.fetchone(
                            f"SELECT relation_guid, contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    rel_guid = ss.get("relation_guid") if ss else None
                    if not c_guid and ss and ss.get("contacts_json"):
                        try:
                            cts = json.loads(ss.get("contacts_json") or "[]")
                            if cts and cts[0].get("guid"):
                                c_guid = cts[0].get("guid")
                        except Exception:
                            pass

                    ss_payload = {
                        "RelationGUID": int(rel_guid) if rel_guid else None,
                        "Relation_Contact": [
                            {
                                "ContactGUID": str(c_guid),
                                "Email": email_val
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
                                    ct["email"] = email_val
                            db.execute(
                                f"UPDATE {prefix}silvasoft_cache SET contacts_json = %s, primary_contact_email = %s WHERE customer_number = %s",
                                (json.dumps(cts), email_val, str(entity_id))
                            )

                elif action == "UPDATE_CONTACT_NAME":
                    c_guid = payload.get("contact_guid")
                    parts = (new_val or "").strip().split(" ", 1)
                    first_name = parts[0] if parts else ""
                    last_name = parts[1] if len(parts) > 1 else ""

                    with get_db() as db:
                        ss = db.fetchone(
                            f"SELECT relation_guid, contacts_json FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                            (str(entity_id),)
                        )
                    rel_guid = ss.get("relation_guid") if ss else None
                    if not c_guid and ss and ss.get("contacts_json"):
                        try:
                            cts = json.loads(ss.get("contacts_json") or "[]")
                            if cts and cts[0].get("guid"):
                                c_guid = cts[0].get("guid")
                        except Exception:
                            pass

                    ss_payload = {
                        "RelationGUID": int(rel_guid) if rel_guid else None,
                        "Relation_Contact": [
                            {
                                "ContactGUID": str(c_guid),
                                "FirstName": first_name,
                                "LastName": last_name
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
                                    ct["name"] = (new_val or "").strip()
                                    ct["first_name"] = first_name
                                    ct["last_name"] = last_name
                            db.execute(
                                f"UPDATE {prefix}silvasoft_cache SET contacts_json = %s, primary_contact_name = %s WHERE customer_number = %s",
                                (json.dumps(cts), (new_val or "").strip(), str(entity_id))
                            )

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
            fail_now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            logger.error(f"❌ Fout bij verwerken queue taak #{task_id}: {e}")
            with get_db() as db:
                db.execute(f"""
                    UPDATE {prefix}queue
                    SET status = 'FAILED', attempts = attempts + 1, processed_at = %s, error_message = %s
                    WHERE id = %s
                """, (fail_now_str, str(e), task_id))
            raise

    @staticmethod
    def get_queue_summary() -> Dict[str, Any]:
        prefix = Config.DB_TABLE_PREFIX
        now = datetime.datetime.now()
        cooldown_sec = getattr(Config, "SILVASOFT_QUEUE_INTERVAL_SECONDS", 600)

        with get_db() as db:
            pending = db.fetchall(f"""
                SELECT * FROM {prefix}queue
                WHERE status IN ('PENDING', 'PROCESSING')
                ORDER BY CASE WHEN target = 'HUBSPOT' THEN 0 ELSE 1 END, id ASC
            """)
            recent_done = db.fetchall(f"""
                SELECT * FROM {prefix}queue
                WHERE status IN ('SUCCESS', 'FAILED')
                ORDER BY COALESCE(processed_at, created_at) DESC, id DESC LIMIT 25
            """)
            last_ss = db.fetchone(f"""
                SELECT processed_at FROM {prefix}queue
                WHERE target = 'SILVASOFT' AND status IN ('SUCCESS', 'FAILED') AND processed_at IS NOT NULL
                ORDER BY processed_at DESC LIMIT 1
            """)

        ss_count = sum(1 for p in pending if p.get("target") == "SILVASOFT")
        hs_count = sum(1 for p in pending if p.get("target") == "HUBSPOT")

        # Dynamische ETA berekening voor pending Silvasoft taken
        base_ss_time = now
        if last_ss and last_ss.get("processed_at"):
            last_p = last_ss["processed_at"]
            if isinstance(last_p, str):
                try:
                    last_dt = datetime.datetime.strptime(last_p, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    last_dt = now
            elif isinstance(last_p, datetime.datetime):
                last_dt = last_p
            else:
                last_dt = now

            earliest_next = last_dt + datetime.timedelta(seconds=cooldown_sec)
            if earliest_next > now:
                base_ss_time = earliest_next

        ss_idx = 0
        for task in pending:
            if task.get("target") == "HUBSPOT":
                task["estimated_str"] = "Z.s.m."
                task["scheduled_at_formatted"] = "z.s.m."
            else:
                if task.get("status") == "PROCESSING":
                    task["estimated_str"] = "Nu bezig..."
                    task["scheduled_at_formatted"] = "nu"
                else:
                    eta = base_ss_time + datetime.timedelta(seconds=ss_idx * cooldown_sec)
                    if eta.date() == now.date():
                        task["estimated_str"] = eta.strftime("%H:%M")
                        task["scheduled_at_formatted"] = eta.strftime("%H:%M")
                    else:
                        task["estimated_str"] = eta.strftime("%d-%m %H:%M")
                        task["scheduled_at_formatted"] = eta.strftime("%d-%m %H:%M")
                    ss_idx += 1

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

    # Herstart-herstel: zet taken die tijdens crash/reboot in PROCESSING stonden terug op PENDING
    try:
        with get_db() as db:
            cur = db.execute(f"""
                UPDATE {prefix}queue
                SET status = 'PENDING'
                WHERE status = 'PROCESSING'
            """)
            if cur and cur.rowcount > 0:
                logger.info(f"🔄 {cur.rowcount} vastgelopen taken hersteld naar PENDING.")
    except Exception as e:
        logger.warning(f"Fout bij herstellen van PROCESSING taken bij startup: {e}")

    cooldown_seconds = getattr(Config, "SILVASOFT_QUEUE_INTERVAL_SECONDS", 600)

    while not _STOP_EVENT.is_set():
        try:
            now = datetime.datetime.now()

            # 1. HubSpot taken hebben prioriteit en kunnen direct lopen
            next_hs = None
            with get_db() as db:
                next_hs = db.fetchone(f"""
                    SELECT id FROM {prefix}queue
                    WHERE target = 'HUBSPOT' AND status = 'PENDING'
                    ORDER BY id ASC LIMIT 1
                """)

            if next_hs:
                task_id = next_hs["id"]
                logger.info(f"Worker pakt direct HubSpot taak #{task_id} op...")
                QueueService.process_task(task_id)
                continue

            # 2. Silvasoft taken (strikt 1 per cooldown_seconds, standaard 600s / 10 min)
            with get_db() as db:
                last_ss = db.fetchone(f"""
                    SELECT processed_at FROM {prefix}queue
                    WHERE target = 'SILVASOFT' AND status IN ('SUCCESS', 'FAILED') AND processed_at IS NOT NULL
                    ORDER BY processed_at DESC LIMIT 1
                """)

            eligible = True
            if last_ss and last_ss.get("processed_at"):
                last_p = last_ss["processed_at"]
                if isinstance(last_p, str):
                    try:
                        last_dt = datetime.datetime.strptime(last_p, "%Y-%m-%d %H:%M:%S")
                    except ValueError:
                        last_dt = now
                elif isinstance(last_p, datetime.datetime):
                    last_dt = last_p
                else:
                    last_dt = now

                elapsed = (now - last_dt).total_seconds()
                if elapsed < cooldown_seconds:
                    eligible = False

            if eligible:
                next_ss = None
                with get_db() as db:
                    next_ss = db.fetchone(f"""
                        SELECT id FROM {prefix}queue
                        WHERE target = 'SILVASOFT' AND status = 'PENDING'
                        ORDER BY id ASC LIMIT 1
                    """)

                if next_ss:
                    task_id = next_ss["id"]
                    logger.info(f"Worker pakt Silvasoft taak #{task_id} op (cooldown verstreken)...")
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

