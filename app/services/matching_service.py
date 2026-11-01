import re
import json
import difflib
import datetime
import logging
from typing import List, Dict, Any, Optional
from app.config import Config
from app.database import get_db

logger = logging.getLogger("zwol_sync")

GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "live.nl", "live.com",
    "yahoo.com", "icloud.com", "me.com", "msn.com"
}


def normalize_zip(val: str) -> str:
    if not val:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", val).upper()


def normalize_name(val: str) -> str:
    if not val:
        return ""
    s = val.lower()
    # Strip web TLDs (bijv. bier-online.nl -> bier-online)
    s = re.sub(r"\.(nl|com|org|net|eu|be|online|shop|info)\b", "", s)
    # Strip rechtsvormen en algemene horeca termen
    for drop in ["b.v.", "bv", "v.o.f.", "vof", "stichting", "biercafé", "biercafe", "café", "cafe", "brouwerij", "restaurant"]:
        s = re.sub(r"\b" + re.escape(drop) + r"\b", "", s)
    s = re.sub(r"[^a-z0-9]", "", s)
    return s.strip()


def normalize_city(val: str) -> str:
    if not val:
        return ""
    s = val.lower().strip()
    s = re.sub(r"^'s[\s-]?", "", s)
    for drop in ["aan den rijn", "aan de rijn", "den", "der", "de", "het", "aan zee"]:
        s = re.sub(r"\b" + re.escape(drop) + r"\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s).strip()


def extract_domain(url_or_email: str) -> str:
    if not url_or_email:
        return ""
    s = url_or_email.lower().strip()
    if "@" in s:
        s = s.split("@")[-1]
    s = re.sub(r"^https?://", "", s)
    s = re.sub(r"^www\.", "", s)
    s = s.split("/")[0].strip()
    return s


class MatchingService:
    def run_auto_matching(self) -> Dict[str, int]:
        """
        Draait de gelaagde matching-engine tussen Silvasoft en HubSpot cache.
        Bestaande matches blijven behouden.
        """
        prefix = Config.DB_TABLE_PREFIX
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as db:
            ss_companies = db.fetchall(f"SELECT * FROM {prefix}silvasoft_cache")
            hs_companies = db.fetchall(f"SELECT * FROM {prefix}hubspot_cache")
            existing_matches = db.fetchall(f"SELECT silvasoft_customer_number, hubspot_id FROM {prefix}matches")

            matched_ss_ids = {m["silvasoft_customer_number"] for m in existing_matches}
            matched_hs_ids = {m["hubspot_id"] for m in existing_matches}

            # Bouw lookups voor HubSpot
            hs_by_custno = {}
            hs_by_kvk = {}
            hs_by_zippad = {}
            hs_by_domain = {}
            hs_by_email = {}

            for h in hs_companies:
                hid = str(h["hubspot_id"])
                custno = (h.get("silvasoft_customer_number") or "").strip()
                if custno:
                    hs_by_custno[custno] = hid

                kvk = (h.get("kvk_number") or "").strip()
                if kvk and len(kvk) >= 8:
                    hs_by_kvk[kvk] = hid

                z = normalize_zip(h.get("postal_code") or "")
                street = h.get("street") or ""
                num_match = re.search(r"\b(\d+)\b", street)
                unit = num_match.group(1) if num_match else ""
                if z and unit:
                    hs_by_zippad[f"{z}_{unit}"] = hid

                # Bedrijfswebsite en domein
                dom = extract_domain(h.get("website") or h.get("domain") or "")
                if dom and len(dom) > 4 and dom not in GENERIC_EMAIL_DOMAINS:
                    hs_by_domain[dom] = hid

                # Bedrijfs- en contactpersoon e-mails
                raw = json.loads(h.get("raw_json") or "{}")
                comp_email = (raw.get("properties", {}).get("email") or "").lower().strip()
                if comp_email and "@" in comp_email:
                    hs_by_email[comp_email] = hid
                    c_dom = extract_domain(comp_email)
                    if c_dom and len(c_dom) > 4 and c_dom not in GENERIC_EMAIL_DOMAINS:
                        hs_by_domain[c_dom] = hid

                cts = json.loads(h.get("contacts_json") or "[]")
                for c in cts:
                    em = (c.get("email") or "").lower().strip()
                    if em and "@" in em:
                        hs_by_email[em] = hid
                        c_dom = extract_domain(em)
                        if c_dom and len(c_dom) > 4 and c_dom not in GENERIC_EMAIL_DOMAINS:
                            hs_by_domain[c_dom] = hid

            new_matches_count = 0

            for s in ss_companies:
                cust_no = str(s["customer_number"])
                if cust_no in matched_ss_ids:
                    continue

                target_hs_id = None
                method = ""
                score = 100

                # 1. Direct ID match
                if cust_no in hs_by_custno:
                    target_hs_id = hs_by_custno[cust_no]
                    method = "id_property"
                    score = 100

                # 2. KvK match
                if not target_hs_id:
                    s_kvk = (s.get("kvk_number") or "").strip()
                    if s_kvk and len(s_kvk) >= 8 and s_kvk in hs_by_kvk:
                        candidate_id = hs_by_kvk[s_kvk]
                        if candidate_id not in matched_hs_ids:
                            target_hs_id = candidate_id
                            method = "kvk"
                            score = 99

                # 3. Postcode + Huisnummer match
                if not target_hs_id:
                    s_z = normalize_zip(s.get("postal_code") or "")
                    s_unit = (s.get("unit") or "").strip()
                    if s_z and s_unit:
                        key = f"{s_z}_{s_unit}"
                        if key in hs_by_zippad:
                            candidate_id = hs_by_zippad[key]
                            if candidate_id not in matched_hs_ids:
                                target_hs_id = candidate_id
                                method = "postcode_huisnummer"
                                score = 95

                # 4. Exact Contactpersoon / Bedrijfs e-mail match
                if not target_hs_id:
                    s_emails = set()
                    if s.get("email"):
                        s_emails.add(s["email"].lower().strip())
                    s_cts = json.loads(s.get("contacts_json") or "[]")
                    for c in s_cts:
                        if c.get("email"):
                            s_emails.add(c["email"].lower().strip())

                    for em in s_emails:
                        if em in hs_by_email:
                            candidate_id = hs_by_email[em]
                            if candidate_id not in matched_hs_ids:
                                target_hs_id = candidate_id
                                method = f"email_{em.split('@')[-1]}"
                                score = 95
                                break

                # 5. Domein match (bedrijfsdomein of contactpersoondomein)
                if not target_hs_id:
                    s_doms = set()
                    for em in s_emails:
                        d = extract_domain(em)
                        if d and len(d) > 4 and d not in GENERIC_EMAIL_DOMAINS:
                            s_doms.add(d)
                    if s.get("website"):
                        d = extract_domain(s["website"])
                        if d and len(d) > 4 and d not in GENERIC_EMAIL_DOMAINS:
                            s_doms.add(d)

                    for d in s_doms:
                        if d in hs_by_domain:
                            candidate_id = hs_by_domain[d]
                            if candidate_id not in matched_hs_ids:
                                target_hs_id = candidate_id
                                method = "domain"
                                score = 90
                                break

                # 6. Exacte genormaliseerde naam (+ optioneel plaats match)
                if not target_hs_id:
                    s_norm_name = normalize_name(s.get("name") or "")
                    s_city = normalize_city(s.get("city") or "")
                    if len(s_norm_name) >= 4:
                        for h in hs_companies:
                            hid = str(h["hubspot_id"])
                            if hid in matched_hs_ids:
                                continue
                            h_norm_name = normalize_name(h.get("name") or "")
                            h_city = normalize_city(h.get("city") or "")
                            if s_norm_name == h_norm_name and (not s_city or not h_city or s_city == h_city):
                                target_hs_id = hid
                                method = "name_exact"
                                score = 88
                                break

                # 7. Fuzzy naam + plaats match
                if not target_hs_id:
                    s_norm_name = normalize_name(s.get("name") or "")
                    s_city = normalize_city(s.get("city") or "")
                    if len(s_norm_name) >= 5 and s_city:
                        for h in hs_companies:
                            hid = str(h["hubspot_id"])
                            if hid in matched_hs_ids:
                                continue
                            h_norm_name = normalize_name(h.get("name") or "")
                            h_city = normalize_city(h.get("city") or "")
                            if s_city and h_city and s_city == h_city:
                                ratio = difflib.SequenceMatcher(None, s_norm_name, h_norm_name).ratio()
                                if ratio >= 0.88:
                                    target_hs_id = hid
                                    method = f"name_fuzzy_{int(ratio*100)}"
                                    score = 80
                                    break

                if target_hs_id and target_hs_id not in matched_hs_ids:
                    db.execute(f"""
                        INSERT INTO {prefix}matches (
                            silvasoft_customer_number, hubspot_id, match_method,
                            confidence_score, is_confirmed, matched_at
                        ) VALUES (%s, %s, %s, %s, %s, %s)
                    """, (cust_no, target_hs_id, method, score, 1, now_str))

                    matched_ss_ids.add(cust_no)
                    matched_hs_ids.add(target_hs_id)
                    new_matches_count += 1

            return {
                "total_silvasoft": len(ss_companies),
                "total_hubspot": len(hs_companies),
                "total_matched": len(matched_ss_ids),
                "newly_matched": new_matches_count,
                "unmatched_silvasoft": len(ss_companies) - len(matched_ss_ids)
            }

    def manual_match(self, silvasoft_customer_number: str, hubspot_id: str):
        """Koppelt handmatig een Silvasoft-klant aan een HubSpot-bedrijf."""
        prefix = Config.DB_TABLE_PREFIX
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with get_db() as db:
            db.execute(
                f"DELETE FROM {prefix}matches WHERE silvasoft_customer_number = %s OR hubspot_id = %s",
                (silvasoft_customer_number, hubspot_id)
            )
            db.execute(f"""
                INSERT INTO {prefix}matches (
                    silvasoft_customer_number, hubspot_id, match_method,
                    confidence_score, is_confirmed, matched_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """, (silvasoft_customer_number, hubspot_id, "manual", 100, 1, now_str))

    def unmatch(self, silvasoft_customer_number: str):
        """Verbreekt de koppeling tussen een Silvasoft-klant en HubSpot-bedrijf."""
        prefix = Config.DB_TABLE_PREFIX
        with get_db() as db:
            db.execute(
                f"DELETE FROM {prefix}matches WHERE silvasoft_customer_number = %s",
                (silvasoft_customer_number,)
            )


matching_service = MatchingService()

