import re
import json
import logging
import datetime
from typing import List, Dict, Any, Optional
from app.config import Config
from app.database import get_db

logger = logging.getLogger("zwol_sync")


def clean_phone(val: str) -> str:
    if not val:
        return ""
    digits = re.sub(r"[^0-9]", "", val)
    if digits.startswith("0031"):
        digits = "31" + digits[4:]
    elif digits.startswith("0") and len(digits) == 10:
        digits = "31" + digits[1:]
    return digits


def clean_zip(val: str) -> str:
    if not val:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", val).upper()


def clean_str(val: Any) -> str:
    if val is None:
        return ""
    return str(val).strip()


def normalize_text(val: Any) -> str:
    """Normaliseert tekst voor vergelijking: uniformeert quotes, apostrofs en spaties."""
    if val is None:
        return ""
    s = str(val).strip()
    s = re.sub(r"[\u2018\u2019\u0060\u00B4\u201A\u201B\u2032]", "'", s)
    s = re.sub(r"[\u201C\u201D\u201E\u201F\u2033]", '"', s)
    s = re.sub(r"\s+", " ", s)
    return s


def format_silvasoft_address(street: Optional[str], unit: Optional[str], unit_appendix: Optional[str]) -> str:
    """
    Combineert Silvasoft straat, unit en unit_appendix tot een natuurlijk Nederlands adres.
    Voorkomt dubbele toevoegingen (bijv. unit='21 a' met app='a' -> '21 a', niet '21 aa')
    en herstelt versnipperde huisnummers uit de Silvasoft API (bijv. '36 B b' -> '36 Bb').
    """
    s = (street or "").strip()
    u = (unit or "").strip()
    app = (unit_appendix or "").strip()

    if u:
        # Silvasoft API fragmenteert samengestelde toevoegingen met spaties, bijv. '36 B b' -> '36 Bb'
        u = re.sub(r'(\d+)\s+([A-Za-z])\s+([A-Za-z0-9]+)\b', r'\1 \2\3', u)

    if u and app:
        # Als de unit al eindigt op de toevoeging (ongeacht hoofd-/kleine letters), niet nogmaals toevoegen
        if not u.lower().endswith(app.lower()):
            if u[-1].isdigit() and app[0].isalnum():
                u = f"{u}{app}"
            else:
                u = f"{u} {app}"
    elif not u and app:
        u = app

    if s and u:
        return f"{s} {u}"
    return s or u


def normalize_address(val: Any) -> str:
    """Normaliseert adres voor vergelijking: verwijdert overtollige spaties tussen cijfers en letters."""
    if val is None:
        return ""
    s = normalize_text(val).lower()
    # Maak bijv. '36 bb', '36 b b' en '36bb' gelijkvormig voor vergelijking
    s = re.sub(r'(\d+)\s+([a-z0-9])', r'\1\2', s)
    s = re.sub(r'\s+', ' ', s)
    return s.strip()



class DiffService:
    def get_comparison_data(self, filter_type: str = "all") -> Dict[str, Any]:
        """
        Berekent alle verschillen tussen Silvasoft en HubSpot.
        filter_type: 'all', 'diffs', 'new_in_silvasoft', 'in_sync'
        """
        prefix = Config.DB_TABLE_PREFIX

        with get_db() as db:
            ss_rows = db.fetchall(f"SELECT * FROM {prefix}silvasoft_cache ORDER BY CAST(customer_number AS SIGNED) ASC")
            hs_rows = db.fetchall(f"SELECT * FROM {prefix}hubspot_cache")
            matches = db.fetchall(f"SELECT * FROM {prefix}matches")
            ignored_rows = db.fetchall(f"SELECT customer_number, contact_id FROM {prefix}ignored_contacts")
            ignored_comp_rows = db.fetchall(f"SELECT customer_number FROM {prefix}ignored_companies")
            pending_rows = db.fetchall(f"""
                SELECT id, target, action, entity_id, field_name, new_value, scheduled_at, payload_json
                FROM {prefix}queue
                WHERE status = 'PENDING'
            """)

        ignored_set = {(str(r["customer_number"]), str(r["contact_id"])) for r in ignored_rows}
        ignored_comp_set = {str(r["customer_number"]) for r in ignored_comp_rows}
        hs_by_id = {str(h["hubspot_id"]): h for h in hs_rows}
        match_by_ss = {str(m["silvasoft_customer_number"]): str(m["hubspot_id"]) for m in matches}

        # Indexeer pending wachtrijtaken
        pending_company_tasks = {}
        pending_contact_tasks = {}
        for pr in pending_rows:
            ent = str(pr.get("entity_id") or "")
            fld = str(pr.get("field_name") or "")
            act = str(pr.get("action") or "")
            if act == "UPDATE_FIELD":
                if fld:
                    pending_company_tasks[(ent, fld)] = pr
            else:
                # Contact taken (UPDATE_CONTACT_*, ADD_CONTACT)
                if act:
                    pending_contact_tasks[(ent, act)] = pr
                if fld:
                    pending_contact_tasks[(ent, fld)] = pr

        companies_list = []
        stats = {
            "total_silvasoft": len(ss_rows),
            "in_sync": 0,
            "diffs_found": 0,
            "new_in_silvasoft": 0,
            "ignored_companies": len(ignored_comp_rows),
            "unmatched": 0
        }

        for ss in ss_rows:
            ss["formatted_address"] = format_silvasoft_address(ss.get("street"), ss.get("unit"), ss.get("unit_appendix"))
            cust_no = str(ss["customer_number"])
            hs_id = match_by_ss.get(cust_no)
            hs = hs_by_id.get(hs_id) if hs_id else None

            # Case 1: Nog niet in HubSpot
            if not hs:
                if cust_no in ignored_comp_set:
                    if filter_type in ("all", "ignored"):
                        companies_list.append({
                            "customer_number": cust_no,
                            "name": ss["name"],
                            "status": "IGNORED",
                            "status_label": "Genegeerd voor HubSpot",
                            "status_badge": "badge-secondary",
                            "silvasoft": ss,
                            "hubspot": None,
                            "diffs": [],
                            "contacts_diff": [],
                            "identical_fields": [],
                            "identical_contacts": [],
                            "ignored_contacts": [],
                            "pending_fields": [],
                            "pending_contacts": [],
                            "pending_count": 0,
                            "identical_count": 0
                        })
                    continue

                stats["new_in_silvasoft"] += 1
                if filter_type in ("all", "new_in_silvasoft"):
                    companies_list.append({
                        "customer_number": cust_no,
                        "name": ss["name"],
                        "status": "NEW_IN_SILVASOFT",
                        "status_label": "Nog niet in HubSpot",
                        "status_badge": "badge-warning",
                        "silvasoft": ss,
                        "hubspot": None,
                        "diffs": [],
                        "contacts_diff": [],
                        "identical_fields": [],
                        "identical_contacts": [],
                        "ignored_contacts": [],
                        "pending_fields": [],
                        "pending_contacts": [],
                        "pending_count": 0,
                        "identical_count": 0
                    })
                continue

            # Case 2: Gekoppeld aan HubSpot bedrijf -> Verschillen berekenen
            diffs = []
            identical_fields = []
            pending_fields = []

            def check_field_diff(field_name, field_label, ss_val, hs_val, is_equal):
                if not is_equal:
                    p_task = pending_company_tasks.get((cust_no, field_name)) or pending_company_tasks.get((hs_id, field_name))
                    if p_task:
                        sched_dt = p_task.get("scheduled_at")
                        sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                        pending_fields.append({
                            "field": field_name,
                            "label": field_label,
                            "silvasoft": ss_val or "(leeg)",
                            "hubspot": hs_val or "(leeg)",
                            "target": p_task.get("target"),
                            "new_value": p_task.get("new_value") or "",
                            "scheduled_at": sched_str,
                            "task_id": p_task.get("id")
                        })
                    else:
                        diffs.append({
                            "field": field_name,
                            "label": field_label,
                            "silvasoft": ss_val or "(leeg)",
                            "hubspot": hs_val or "(leeg)"
                        })
                else:
                    identical_fields.append({
                        "field": field_name,
                        "label": field_label,
                        "silvasoft": ss_val or "(leeg)",
                        "hubspot": hs_val or "(leeg)"
                    })

            # 1. Bedrijfsnaam
            ss_name = clean_str(ss.get("name"))
            hs_name = clean_str(hs.get("name"))
            check_field_diff("name", "Bedrijfsnaam", ss_name, hs_name, normalize_text(ss_name).lower() == normalize_text(hs_name).lower())

            # 2. KvK
            ss_kvk = clean_str(ss.get("kvk_number"))
            hs_kvk = clean_str(hs.get("kvk_number"))
            check_field_diff("kvk_number", "KvK-nummer", ss_kvk, hs_kvk, ss_kvk == hs_kvk or (not ss_kvk and not hs_kvk))

            # 3. Straat & Huisnummer
            ss_street = format_silvasoft_address(ss.get("street"), ss.get("unit"), ss.get("unit_appendix"))
            ss["formatted_address"] = ss_street
            hs_street = clean_str(hs.get("street"))
            is_street_equal = (
                normalize_address(ss_street) == normalize_address(hs_street)
                or (not ss_street and not hs_street)
            )
            check_field_diff("street", "Straat & Huisnummer", ss_street, hs_street, is_street_equal)

            # 4. Postcode
            ss_zip = clean_zip(ss.get("postal_code"))
            hs_zip = clean_zip(hs.get("postal_code"))
            check_field_diff("postal_code", "Postcode", ss.get("postal_code"), hs.get("postal_code"), ss_zip == hs_zip or (not ss_zip and not hs_zip))

            # 5. Plaats
            ss_city = clean_str(ss.get("city"))
            hs_city = clean_str(hs.get("city"))
            check_field_diff("city", "Plaats", ss_city, hs_city, normalize_text(ss_city).lower() == normalize_text(hs_city).lower() or (not ss_city and not hs_city))

            # 6. Telefoon
            ss_ph = clean_phone(ss.get("phone"))
            hs_ph = clean_phone(hs.get("phone"))
            check_field_diff("phone", "Telefoonnummer", ss.get("phone"), hs.get("phone"), ss_ph == hs_ph or (not ss_ph and not hs_ph))

            # 7. Website
            ss_web = clean_str(ss.get("website")).lower()
            hs_web = clean_str(hs.get("website") or hs.get("domain")).lower()
            check_field_diff("website", "Website", ss.get("website"), hs.get("website") or hs.get("domain"), ss_web == hs_web or (not ss_web and not hs_web))

            # Contactpersonen vergelijken
            ss_contacts = json.loads(ss.get("contacts_json") or "[]")
            hs_contacts = json.loads(hs.get("contacts_json") or "[]")
            contacts_diff, identical_contacts, ignored_contacts, pending_contacts = self._compare_contacts(
                ss_contacts, hs_contacts, cust_no, hs_id, ignored_set, pending_contact_tasks
            )

            diff_count = len(diffs) + len(contacts_diff)
            pending_count = len(pending_fields) + len(pending_contacts)
            identical_count = len(identical_fields) + len(identical_contacts) + len(ignored_contacts) + pending_count

            if diff_count == 0 and pending_count == 0:
                stats["in_sync"] += 1
                status = "IN_SYNC"
                status_label = "Volledig synchroon"
                status_badge = "badge-success"
            elif diff_count == 0 and pending_count > 0:
                stats["in_sync"] += 1
                status = "PENDING_SYNC"
                status_label = f"⏳ In wachtrij ({pending_count})"
                status_badge = "badge-info"
            else:
                stats["diffs_found"] += 1
                status = "DIFF_FOUND"
                status_label = f"{diff_count} verschillen"
                status_badge = "badge-danger"

            # Filter toepassen
            is_matched = True
            show_card = False
            if filter_type == "all":
                show_card = True
            elif filter_type == "diffs" and diff_count > 0:
                show_card = True
            elif filter_type == "in_sync" and diff_count == 0:
                show_card = True

            if show_card:
                companies_list.append({
                    "customer_number": cust_no,
                    "hubspot_id": hs_id,
                    "name": ss["name"],
                    "status": status,
                    "status_label": status_label,
                    "status_badge": status_badge,
                    "silvasoft": ss,
                    "hubspot": hs,
                    "diffs": diffs,
                    "contacts_diff": contacts_diff,
                    "identical_fields": identical_fields,
                    "identical_contacts": identical_contacts,
                    "ignored_contacts": ignored_contacts,
                    "pending_fields": pending_fields,
                    "pending_contacts": pending_contacts,
                    "pending_count": pending_count,
                    "identical_count": identical_count
                })

        return {
            "stats": stats,
            "companies": companies_list
        }

    def _compare_contacts(self, ss_contacts: List[Dict], hs_contacts: List[Dict], cust_no: str = "", hs_id: str = "", ignored_set: set = None, pending_by_entity_field: dict = None):
        """Vergelijkt contactpersonen op e-mail en naam, met deduplicatie, negeer-filter en wachtrij-herkenning."""
        diffs = []
        identical_contacts = []
        ignored_contacts = []
        pending_contacts = []
        if ignored_set is None:
            ignored_set = set()
        if pending_by_entity_field is None:
            pending_by_entity_field = {}

        # 1. Schoon Silvasoft contacten op (geen facturatie-afdelingen en ontdubbelen)
        clean_ss = []
        seen_ss = set()
        for sc in ss_contacts:
            s_name = (sc.get("name") or "").strip()
            if not s_name or "administratie" in s_name.lower():
                continue
            key = sc.get("guid") or sc.get("email") or s_name.lower()
            if key not in seen_ss:
                seen_ss.add(key)
                clean_ss.append(sc)

        # 2. Ontdubbel HubSpot contacten op ID
        clean_hs = []
        seen_hs_ids = set()
        for hc in hs_contacts:
            cid = str(hc.get("id") or "")
            if not cid or cid in seen_hs_ids:
                continue
            seen_hs_ids.add(cid)
            clean_hs.append(hc)

        matched_hs_ids = set()

        for sc in clean_ss:
            s_mail = (sc.get("email") or "").lower().strip()
            s_name = (sc.get("name") or "").lower().strip()

            # Zoek match in HubSpot
            match_hc = None
            for hc in clean_hs:
                h_mail = (hc.get("email") or "").lower().strip()
                h_name = (hc.get("name") or "").lower().strip()
                if s_mail and h_mail and s_mail == h_mail:
                    match_hc = hc
                    break
                elif s_name and h_name and s_name == h_name:
                    match_hc = hc
                    break

            if match_hc:
                matched_hs_ids.add(str(match_hc.get("id")))
                c_name = sc.get("name") or match_hc.get("name")

                # A. Contactpersoon Naam vergelijken
                s_nm = clean_str(sc.get("name"))
                h_nm = clean_str(match_hc.get("name"))
                if normalize_text(s_nm).lower() == normalize_text(h_nm).lower() and s_nm:
                    identical_contacts.append({
                        "label": f"👤 Naam: {c_name}",
                        "silvasoft": s_nm,
                        "hubspot": h_nm
                    })
                elif normalize_text(s_nm).lower() != normalize_text(h_nm).lower() and (s_nm or h_nm):
                    p_task = (
                        pending_by_entity_field.get((cust_no, "name")) or
                        pending_by_entity_field.get((cust_no, "UPDATE_CONTACT_NAME")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "name")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "UPDATE_CONTACT_NAME"))
                    )
                    if p_task:
                        sched_dt = p_task.get("scheduled_at")
                        sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                        pending_contacts.append({
                            "type": "PENDING_CONTACT_FIELD",
                            "label": f"👤 Naam: {c_name}",
                            "silvasoft": s_nm or "(leeg)",
                            "hubspot": h_nm or "(leeg)",
                            "target": p_task.get("target"),
                            "new_value": p_task.get("new_value") or "",
                            "scheduled_at": sched_str,
                            "task_id": p_task.get("id")
                        })
                    else:
                        diffs.append({
                            "type": "CONTACT_NAME_DIFF",
                            "contact_name": c_name,
                            "contact_email": sc.get("email") or match_hc.get("email"),
                            "silvasoft": s_nm or "(leeg)",
                            "hubspot": h_nm or "(leeg)",
                            "hs_contact_id": str(match_hc.get("id")),
                            "ss_contact_guid": str(sc.get("guid") or "")
                        })

                # B. Contactpersoon E-mail vergelijken
                s_mail_clean = clean_str(sc.get("email")).lower()
                h_mail_clean = clean_str(match_hc.get("email")).lower()
                if s_mail_clean == h_mail_clean and s_mail_clean:
                    identical_contacts.append({
                        "label": f"👤 E-mail: {c_name}",
                        "silvasoft": sc.get("email"),
                        "hubspot": match_hc.get("email")
                    })
                elif s_mail_clean != h_mail_clean and (s_mail_clean or h_mail_clean):
                    p_task = (
                        pending_by_entity_field.get((cust_no, "email")) or
                        pending_by_entity_field.get((cust_no, "UPDATE_CONTACT_EMAIL")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "email")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "UPDATE_CONTACT_EMAIL"))
                    )
                    if p_task:
                        sched_dt = p_task.get("scheduled_at")
                        sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                        pending_contacts.append({
                            "type": "PENDING_CONTACT_FIELD",
                            "label": f"👤 E-mail: {c_name}",
                            "silvasoft": sc.get("email") or "(leeg)",
                            "hubspot": match_hc.get("email") or "(leeg)",
                            "target": p_task.get("target"),
                            "new_value": p_task.get("new_value") or "",
                            "scheduled_at": sched_str,
                            "task_id": p_task.get("id")
                        })
                    else:
                        diffs.append({
                            "type": "CONTACT_EMAIL_DIFF",
                            "contact_name": c_name,
                            "contact_email": sc.get("email") or match_hc.get("email"),
                            "silvasoft": sc.get("email") or "(leeg)",
                            "hubspot": match_hc.get("email") or "(leeg)",
                            "hs_contact_id": str(match_hc.get("id")),
                            "ss_contact_guid": str(sc.get("guid") or "")
                        })

                # C. Contactpersoon Telefoonnummer vergelijken
                s_ph = clean_phone(sc.get("phone"))
                h_ph = clean_phone(match_hc.get("phone"))
                if s_ph != h_ph and (s_ph or h_ph):
                    # Check of er een pending taak is voor contact phone
                    p_task = (
                        pending_by_entity_field.get((cust_no, "phone")) or
                        pending_by_entity_field.get((cust_no, "UPDATE_CONTACT_PHONE")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "phone")) or
                        pending_by_entity_field.get((str(match_hc.get("id")), "UPDATE_CONTACT_PHONE"))
                    )
                    if p_task:
                        sched_dt = p_task.get("scheduled_at")
                        sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                        pending_contacts.append({
                            "label": f"👤 Tel: {c_name}",
                            "contact_name": c_name,
                            "silvasoft": sc.get("phone") or "(leeg)",
                            "hubspot": match_hc.get("phone") or "(leeg)",
                            "target": p_task.get("target"),
                            "new_value": p_task.get("new_value") or "",
                            "scheduled_at": sched_str,
                            "task_id": p_task.get("id")
                        })
                    else:
                        diffs.append({
                            "type": "CONTACT_PHONE_DIFF",
                            "contact_name": c_name,
                            "contact_email": sc.get("email") or match_hc.get("email"),
                            "silvasoft": sc.get("phone") or "(leeg)",
                            "hubspot": match_hc.get("phone") or "(leeg)",
                            "hs_contact_id": str(match_hc.get("id")),
                            "ss_contact_guid": str(sc.get("guid") or "")
                        })
                elif s_ph == h_ph and (s_ph or h_ph):
                    identical_contacts.append({
                        "label": f"👤 Tel: {c_name}",
                        "silvasoft": sc.get("phone") or "(leeg)",
                        "hubspot": match_hc.get("phone") or "(leeg)"
                    })
            else:
                # Staat alleen in Silvasoft
                # Check of er een pending ADD_CONTACT taak naar HubSpot is
                p_task = pending_by_entity_field.get((hs_id, "ADD_CONTACT"))
                if p_task:
                    sched_dt = p_task.get("scheduled_at")
                    sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                    pending_contacts.append({
                        "label": f"👤 Contact (naar HS): {sc.get('name')}",
                        "contact_name": sc.get("name"),
                        "silvasoft": sc.get("name"),
                        "hubspot": "Wordt aangemaakt in HS",
                        "target": "HUBSPOT",
                        "new_value": sc.get("name"),
                        "scheduled_at": sched_str,
                        "task_id": p_task.get("id")
                    })
                else:
                    diffs.append({
                        "type": "CONTACT_ONLY_IN_SILVASOFT",
                        "contact_name": sc.get("name"),
                        "contact_email": sc.get("email"),
                        "contact_phone": sc.get("phone"),
                        "silvasoft": sc.get("name"),
                        "hubspot": "Niet aanwezig in HubSpot"
                    })

        # Check contacten die alleen in HubSpot staan
        for hc in clean_hs:
            cid = str(hc.get("id") or "")
            if cid not in matched_hs_ids:
                if (str(cust_no), cid) in ignored_set:
                    ignored_contacts.append({
                        "type": "CONTACT_IGNORED",
                        "contact_name": hc.get("name"),
                        "contact_email": hc.get("email"),
                        "contact_phone": hc.get("phone"),
                        "hs_contact_id": cid,
                        "label": f"👤 Contact (genegeerd): {hc.get('name')}",
                        "silvasoft": "Niet in Silvasoft (genegeerd)",
                        "hubspot": hc.get("name")
                    })
                else:
                    # Check of er een pending ADD_CONTACT taak naar Silvasoft is
                    p_task = pending_by_entity_field.get((cust_no, "ADD_CONTACT"))
                    if p_task:
                        sched_dt = p_task.get("scheduled_at")
                        sched_str = sched_dt.strftime("%H:%M") if isinstance(sched_dt, datetime.datetime) else (str(sched_dt)[11:16] if sched_dt else "binnenkort")
                        pending_contacts.append({
                            "label": f"👤 Contact (naar SS): {hc.get('name')}",
                            "contact_name": hc.get("name"),
                            "silvasoft": "Wordt aangemaakt in SS",
                            "hubspot": hc.get("name"),
                            "target": "SILVASOFT",
                            "new_value": hc.get("name"),
                            "scheduled_at": sched_str,
                            "task_id": p_task.get("id")
                        })
                    else:
                        diffs.append({
                            "type": "CONTACT_ONLY_IN_HUBSPOT",
                            "contact_name": hc.get("name"),
                            "contact_email": hc.get("email"),
                            "contact_phone": hc.get("phone"),
                            "silvasoft": "Niet aanwezig in Silvasoft",
                            "hubspot": hc.get("name"),
                            "hs_contact_id": cid,
                            "ss_has_contact": len(clean_ss) > 0,
                            "ss_primary_name": clean_ss[0].get("name") if clean_ss else ""
                        })

        return diffs, identical_contacts, ignored_contacts, pending_contacts


diff_service = DiffService()
