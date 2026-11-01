import re
import json
import logging
from typing import List, Dict, Any, Optional
from app.config import Config
from app.database import get_db

logger = logging.getLogger("zwol_sync")


def clean_phone(val: str) -> str:
    if not val:
        return ""
    p = re.sub(r"[^\d+]", "", val)
    if p.startswith("+31"):
        p = "0" + p[3:]
    elif p.startswith("0031"):
        p = "0" + p[4:]
    return p


def clean_zip(val: str) -> str:
    if not val:
        return ""
    return re.sub(r"\s+", "", val).upper()


def clean_str(val: str) -> str:
    return (val or "").strip()


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

        ignored_set = {(str(r["customer_number"]), str(r["contact_id"])) for r in ignored_rows}

        hs_by_id = {str(h["hubspot_id"]): h for h in hs_rows}
        match_by_ss = {str(m["silvasoft_customer_number"]): str(m["hubspot_id"]) for m in matches}

        companies_list = []
        stats = {
            "total_silvasoft": len(ss_rows),
            "in_sync": 0,
            "diffs_found": 0,
            "new_in_silvasoft": 0,
            "unmatched": 0
        }

        for ss in ss_rows:
            cust_no = str(ss["customer_number"])
            hs_id = match_by_ss.get(cust_no)
            hs = hs_by_id.get(hs_id) if hs_id else None

            # Case 1: Nog niet in HubSpot
            if not hs:
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
                        "contacts_diff": []
                    })
                continue

            # Case 2: Gekoppeld aan HubSpot bedrijf -> Verschillen berekenen
            diffs = []
            identical_fields = []

            # 1. Bedrijfsnaam
            ss_name = clean_str(ss.get("name"))
            hs_name = clean_str(hs.get("name"))
            if ss_name.lower() != hs_name.lower():
                diffs.append({
                    "field": "name",
                    "label": "Bedrijfsnaam",
                    "silvasoft": ss_name,
                    "hubspot": hs_name
                })
            else:
                identical_fields.append({
                    "field": "name",
                    "label": "Bedrijfsnaam",
                    "silvasoft": ss_name,
                    "hubspot": hs_name
                })

            # 2. KvK
            ss_kvk = clean_str(ss.get("kvk_number"))
            hs_kvk = clean_str(hs.get("kvk_number"))
            if ss_kvk != hs_kvk and (ss_kvk or hs_kvk):
                diffs.append({
                    "field": "kvk_number",
                    "label": "KvK-nummer",
                    "silvasoft": ss_kvk or "(leeg)",
                    "hubspot": hs_kvk or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "kvk_number",
                    "label": "KvK-nummer",
                    "silvasoft": ss_kvk or "(leeg)",
                    "hubspot": hs_kvk or "(leeg)"
                })

            # 3. Straat & Huisnummer
            ss_street = clean_str(ss.get("street"))
            if ss.get("unit"):
                ss_street = f"{ss_street} {clean_str(ss.get('unit'))}".strip()
            if ss.get("unit_appendix"):
                ss_street = f"{ss_street}{clean_str(ss.get('unit_appendix'))}".strip()

            hs_street = clean_str(hs.get("street"))
            if ss_street.lower() != hs_street.lower() and (ss_street or hs_street):
                diffs.append({
                    "field": "street",
                    "label": "Straat & Huisnummer",
                    "silvasoft": ss_street or "(leeg)",
                    "hubspot": hs_street or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "street",
                    "label": "Straat & Huisnummer",
                    "silvasoft": ss_street or "(leeg)",
                    "hubspot": hs_street or "(leeg)"
                })

            # 4. Postcode
            ss_zip = clean_zip(ss.get("postal_code"))
            hs_zip = clean_zip(hs.get("postal_code"))
            if ss_zip != hs_zip and (ss_zip or hs_zip):
                diffs.append({
                    "field": "postal_code",
                    "label": "Postcode",
                    "silvasoft": ss.get("postal_code") or "(leeg)",
                    "hubspot": hs.get("postal_code") or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "postal_code",
                    "label": "Postcode",
                    "silvasoft": ss.get("postal_code") or "(leeg)",
                    "hubspot": hs.get("postal_code") or "(leeg)"
                })

            # 5. Plaats
            ss_city = clean_str(ss.get("city"))
            hs_city = clean_str(hs.get("city"))
            if ss_city.lower() != hs_city.lower() and (ss_city or hs_city):
                diffs.append({
                    "field": "city",
                    "label": "Plaats",
                    "silvasoft": ss_city or "(leeg)",
                    "hubspot": hs_city or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "city",
                    "label": "Plaats",
                    "silvasoft": ss_city or "(leeg)",
                    "hubspot": hs_city or "(leeg)"
                })

            # 6. Telefoon
            ss_ph = clean_phone(ss.get("phone"))
            hs_ph = clean_phone(hs.get("phone"))
            if ss_ph != hs_ph and (ss_ph or hs_ph):
                diffs.append({
                    "field": "phone",
                    "label": "Telefoonnummer",
                    "silvasoft": ss.get("phone") or "(leeg)",
                    "hubspot": hs.get("phone") or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "phone",
                    "label": "Telefoonnummer",
                    "silvasoft": ss.get("phone") or "(leeg)",
                    "hubspot": hs.get("phone") or "(leeg)"
                })

            # 7. Website
            ss_web = clean_str(ss.get("website")).lower()
            hs_web = clean_str(hs.get("website") or hs.get("domain")).lower()
            if ss_web != hs_web and (ss_web or hs_web):
                diffs.append({
                    "field": "website",
                    "label": "Website",
                    "silvasoft": ss.get("website") or "(leeg)",
                    "hubspot": hs.get("website") or hs.get("domain") or "(leeg)"
                })
            else:
                identical_fields.append({
                    "field": "website",
                    "label": "Website",
                    "silvasoft": ss.get("website") or "(leeg)",
                    "hubspot": hs.get("website") or hs.get("domain") or "(leeg)"
                })

            # Contactpersonen vergelijken
            ss_contacts = json.loads(ss.get("contacts_json") or "[]")
            hs_contacts = json.loads(hs.get("contacts_json") or "[]")
            contacts_diff, identical_contacts, ignored_contacts = self._compare_contacts(ss_contacts, hs_contacts, cust_no, ignored_set)

            is_in_sync = (len(diffs) == 0 and len(contacts_diff) == 0)

            if is_in_sync:
                stats["in_sync"] += 1
                status = "IN_SYNC"
                status_label = "Volledig synchroon"
                status_badge = "badge-success"
            else:
                stats["diffs_found"] += 1
                status = "DIFF_FOUND"
                status_label = f"{len(diffs) + len(contacts_diff)} verschillen"
                status_badge = "badge-danger"

            if filter_type == "all" or (filter_type == "diffs" and not is_in_sync) or (filter_type == "in_sync" and is_in_sync):
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
                    "identical_count": len(identical_fields) + len(identical_contacts) + len(ignored_contacts)
                })

        return {
            "stats": stats,
            "companies": companies_list
        }

    def _compare_contacts(self, ss_contacts: List[Dict], hs_contacts: List[Dict], cust_no: str = "", ignored_set: set = None):
        """Vergelijkt contactpersonen op e-mail en naam, met deduplicatie en negeer-filter."""
        diffs = []
        identical_contacts = []
        ignored_contacts = []
        if ignored_set is None:
            ignored_set = set()

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
                if s_nm.lower() == h_nm.lower() and s_nm:
                    identical_contacts.append({
                        "label": f"👤 Naam: {c_name}",
                        "silvasoft": s_nm,
                        "hubspot": h_nm
                    })
                elif s_nm.lower() != h_nm.lower() and (s_nm or h_nm):
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
                    diffs.append({
                        "type": "CONTACT_ONLY_IN_HUBSPOT",
                        "contact_name": hc.get("name"),
                        "contact_email": hc.get("email"),
                        "contact_phone": hc.get("phone"),
                        "silvasoft": "Niet aanwezig in Silvasoft",
                        "hubspot": hc.get("name"),
                        "hs_contact_id": cid
                    })

        return diffs, identical_contacts, ignored_contacts


diff_service = DiffService()

