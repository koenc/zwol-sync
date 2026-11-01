import time
import json
import logging
import datetime
import re
import requests
import urllib3
from typing import Dict, List, Any, Optional
from app.config import Config
from app.database import get_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("zwol_sync")


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"
    NEWSLETTER_SUBSCRIPTION_ID = "123809007"

    COMPANY_PROPERTIES = [
        "name",
        "domain",
        "website",
        "address",
        "city",
        "zip",
        "country",
        "phone",
        "type",
        "kvk_nummer",
        "kvk",
        "silvasoft_customer_number",
        "createdate",
        "hs_lastmodifieddate"
    ]

    CONTACT_PROPERTIES = [
        "firstname",
        "lastname",
        "email",
        "phone",
        "mobilephone",
        "jobtitle",
        "associatedcompanyid",
        "hs_email_optout",
        "hs_email_communication_subscriptions_opted_in",
        "hs_email_communication_subscriptions_opted_out"
    ]

    def __init__(self, access_token: Optional[str] = None):
        self.access_token = access_token or Config.HUBSPOT_ACCESS_TOKEN
        self.session = requests.Session()
        if self.access_token:
            self.session.headers.update({
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            })

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        max_retries = 3
        backoff_seconds = 2

        for attempt in range(max_retries):
            response = self.session.request(method, url, verify=False, **kwargs)
            if response.status_code == 429:
                wait_time = int(response.headers.get("Retry-After", backoff_seconds))
                logger.warning(f"HubSpot rate limit (429). {wait_time}s wachten...")
                time.sleep(wait_time)
                backoff_seconds *= 2
                continue
            return response
        return response

    def test_connection(self) -> Dict[str, Any]:
        """Controleert of het HubSpot token geldig is."""
        if not self.access_token:
            raise ValueError("HubSpot access token ontbreekt.")
        url = f"{self.BASE_URL}/crm/v3/objects/companies?limit=1"
        resp = self._request_with_retry("GET", url)
        if resp.status_code == 401:
            raise PermissionError("HubSpot authenticatie mislukt (401). Controleer HUBSPOT_ACCESS_TOKEN.")
        resp.raise_for_status()
        return resp.json()

    def ensure_silvasoft_property(self):
        """Controleert of benodigde custom properties ('silvasoft_customer_number' en 'kvk_nummer') bestaan in HubSpot."""
        if not self.access_token:
            return

        needed = [
            {
                "name": "silvasoft_customer_number",
                "label": "Silvasoft Klantnummer",
                "type": "string",
                "fieldType": "text",
                "groupName": "companyinformation",
                "description": "Klantnummer gekoppeld vanuit Silvasoft boekhouding."
            },
            {
                "name": "kvk_nummer",
                "label": "KvK-nummer",
                "type": "string",
                "fieldType": "text",
                "groupName": "companyinformation",
                "description": "Kamer van Koophandel nummer"
            }
        ]

        for prop in needed:
            url = f"{self.BASE_URL}/crm/v3/properties/companies/{prop['name']}"
            resp = self._request_with_retry("GET", url)
            if resp.status_code == 404:
                logger.info(f"Aanmaken custom property '{prop['name']}' in HubSpot...")
                create_url = f"{self.BASE_URL}/crm/v3/properties/companies"
                c_resp = self._request_with_retry("POST", create_url, json=prop)
                if c_resp.status_code in (200, 201):
                    logger.info(f"✅ Property '{prop['name']}' succesvol aangemaakt in HubSpot.")

    def sync_companies_to_cache(self) -> int:
        """Haalt alle bedrijven en hun gekoppelde contacten op uit HubSpot en slaat ze op in sync_hubspot_cache."""
        logger.info("Start ophalen bedrijven uit HubSpot...")
        if not self.access_token:
            raise ValueError("HubSpot access token ontbreekt.")

        self.ensure_silvasoft_property()

        url = f"{self.BASE_URL}/crm/v3/objects/companies"
        all_companies = []
        after = None

        while True:
            params = {
                "limit": 100,
                "properties": ",".join(self.COMPANY_PROPERTIES),
                "associations": "contacts"
            }
            if after:
                params["after"] = after

            resp = self._request_with_retry("GET", url, params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            all_companies.extend(results)

            paging = data.get("paging", {})
            after = paging.get("next", {}).get("after")
            if not after or not results:
                break

        # Haal alle contacten op voor snelle associatie
        contacts_by_id = self._fetch_all_contacts_map()

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = Config.DB_TABLE_PREFIX
        synced_count = 0

        with get_db() as db:
            for c in all_companies:
                hubspot_id = str(c.get("id"))
                props = c.get("properties") or {}
                name = (props.get("name") or "").strip()
                if not name:
                    continue

                domain = (props.get("domain") or "").strip()
                website = (props.get("website") or "").strip()
                company_type = (props.get("type") or "").strip()
                kvk = (props.get("kvk_nummer") or props.get("kvk") or "").strip()
                street = (props.get("address") or "").strip()
                zip_code = (props.get("zip") or "").strip()
                city = (props.get("city") or "").strip()
                phone = (props.get("phone") or "").strip()
                silvasoft_cust_no = (props.get("silvasoft_customer_number") or "").strip()

                # Gekoppelde contacten verzamelen (ontdubbeld op contact ID)
                contact_list = []
                seen_cids = set()
                primary_name = primary_email = primary_phone = ""
                assocs = c.get("associations", {}).get("contacts", {}).get("results", [])
                for a in assocs:
                    cid = str(a.get("id"))
                    if not cid or cid in seen_cids:
                        continue
                    seen_cids.add(cid)
                    c_data = contacts_by_id.get(cid)
                    if c_data:
                        c_props = c_data.get("properties") or {}
                        c_first = (c_props.get("firstname") or "").strip()
                        c_last = (c_props.get("lastname") or "").strip()
                        c_full = f"{c_first} {c_last}".strip()
                        c_mail = (c_props.get("email") or "").strip()
                        c_ph = (c_props.get("phone") or c_props.get("mobilephone") or "").strip()
                        c_job = (c_props.get("jobtitle") or "").strip()

                        # Nieuwsbrief status bepalen
                        opt_in_str = (c_props.get("hs_email_communication_subscriptions_opted_in") or "").strip()
                        opt_out_str = (c_props.get("hs_email_communication_subscriptions_opted_out") or "").strip()
                        global_optout = str(c_props.get("hs_email_optout") or "").lower() in ("true", "1")
                        opt_ins = [s.strip() for s in opt_in_str.split(";") if s.strip()]
                        opt_outs = [s.strip() for s in opt_out_str.split(";") if s.strip()]

                        if global_optout or self.NEWSLETTER_SUBSCRIPTION_ID in opt_outs:
                            newsletter_status = "UNSUBSCRIBED"
                        elif self.NEWSLETTER_SUBSCRIPTION_ID in opt_ins:
                            newsletter_status = "SUBSCRIBED"
                        else:
                            newsletter_status = "NOT_SPECIFIED"

                        contact_entry = {
                            "id": cid,
                            "name": c_full,
                            "first_name": c_first,
                            "last_name": c_last,
                            "email": c_mail,
                            "phone": c_ph,
                            "job_title": c_job,
                            "is_primary": False,
                            "newsletter_status": newsletter_status
                        }
                        contact_list.append(contact_entry)

                        if not primary_name:
                            primary_name = c_full
                            primary_email = c_mail
                            primary_phone = c_ph
                            contact_entry["is_primary"] = True

                contacts_json = json.dumps(contact_list)
                raw_json = json.dumps(c)

                existing = db.fetchone(
                    f"SELECT hubspot_id FROM {prefix}hubspot_cache WHERE hubspot_id = %s",
                    (hubspot_id,)
                )
                if existing:
                    db.execute(f"""
                        UPDATE {prefix}hubspot_cache
                        SET name = %s, domain = %s, website = %s, kvk_number = %s,
                            street = %s, postal_code = %s, city = %s, phone = %s, company_type = %s,
                            silvasoft_customer_number = %s, contacts_json = %s,
                            primary_contact_name = %s, primary_contact_email = %s, primary_contact_phone = %s,
                            raw_json = %s, synced_at = %s
                        WHERE hubspot_id = %s
                    """, (
                        name, domain, website, kvk,
                        street, zip_code, city, phone, company_type,
                        silvasoft_cust_no, contacts_json,
                        primary_name, primary_email, primary_phone,
                        raw_json, now_str, hubspot_id
                    ))
                else:
                    db.execute(f"""
                        INSERT INTO {prefix}hubspot_cache (
                            hubspot_id, name, domain, website, kvk_number,
                            street, postal_code, city, phone, company_type,
                            silvasoft_customer_number, contacts_json,
                            primary_contact_name, primary_contact_email, primary_contact_phone,
                            raw_json, synced_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        hubspot_id, name, domain, website, kvk,
                        street, zip_code, city, phone, company_type,
                        silvasoft_cust_no, contacts_json,
                        primary_name, primary_email, primary_phone,
                        raw_json, now_str
                    ))

                synced_count += 1

            db.execute(f"""
                INSERT INTO {prefix}sync_log (sync_type, status, records_count, message, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, ("hubspot_cache", "success", synced_count, f"{synced_count} HubSpot bedrijven gesynchroniseerd", now_str))

        logger.info(f"HubSpot cache bijgewerkt: {synced_count} bedrijven opgeslagen.")
        return synced_count

    def _fetch_all_contacts_map(self) -> Dict[str, Dict[str, Any]]:
        url = f"{self.BASE_URL}/crm/v3/objects/contacts"
        contacts_map = {}
        after = None

        while True:
            params = {
                "limit": 100,
                "properties": ",".join(self.CONTACT_PROPERTIES)
            }
            if after:
                params["after"] = after

            resp = self._request_with_retry("GET", url, params=params)
            if resp.status_code != 200:
                break
            data = resp.json()
            results = data.get("results", [])
            for c in results:
                cid = str(c.get("id"))
                contacts_map[cid] = c

            after = data.get("paging", {}).get("next", {}).get("after")
            if not after or not results:
                break

        return contacts_map

    def update_company(self, hubspot_id: str, properties: Dict[str, str]) -> Dict[str, Any]:
        """Werkt eigenschappen van een bedrijf bij in HubSpot."""
        url = f"{self.BASE_URL}/crm/v3/objects/companies/{hubspot_id}"
        resp = self._request_with_retry("PATCH", url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def create_company(self, properties: Dict[str, str]) -> Dict[str, Any]:
        """Maakt een nieuw bedrijf aan in HubSpot."""
        url = f"{self.BASE_URL}/crm/v3/objects/companies"
        clean_props = {k: str(v).strip() for k, v in properties.items() if v is not None and str(v).strip() != ""}
        resp = self._request_with_retry("POST", url, json={"properties": clean_props})
        resp.raise_for_status()
        return resp.json()

    def create_and_associate_contact(self, company_id: str, contact_data: Dict[str, str]) -> Dict[str, Any]:
        """Maakt een contactpersoon aan en koppelt deze direct aan het bedrijf. Als contact al bestaat (409), koppel bestaand contact."""
        url = f"{self.BASE_URL}/crm/v3/objects/contacts"
        raw_props = {
            "firstname": contact_data.get("first_name", ""),
            "lastname": contact_data.get("last_name", ""),
            "email": contact_data.get("email", ""),
            "phone": contact_data.get("phone", "")
        }
        clean_props = {k: str(v).strip() for k, v in raw_props.items() if v is not None and str(v).strip() != ""}

        payload = {
            "properties": clean_props,
            "associations": [
                {
                    "to": {"id": company_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 1  # Primary contact_to_company (typeId 1)
                        }
                    ]
                }
            ]
        }
        resp = self._request_with_retry("POST", url, json=payload)
        if resp.status_code == 409:
            # Contactpersoon bestaat al in HubSpot; koppel bestaand contact aan bedrijf
            c_email = (contact_data.get("email") or "").strip()
            logger.info(f"Contactpersoon met e-mail '{c_email}' bestaat al in HubSpot. Koppelen aan bedrijf {company_id}...")
            err_data = resp.json() if resp.text else {}
            msg = err_data.get("message", "")
            m = re.search(r"Existing ID:\s*(\d+)", msg)
            existing_id = m.group(1) if m else None

            if not existing_id and c_email:
                # Zoek ID op via search endpoint
                s_url = f"{self.BASE_URL}/crm/v3/objects/contacts/search"
                s_body = {
                    "filterGroups": [{
                        "filters": [{
                            "propertyName": "email",
                            "operator": "EQ",
                            "value": c_email
                        }]
                    }]
                }
                s_resp = self._request_with_retry("POST", s_url, json=s_body)
                if s_resp.status_code == 200:
                    res_list = s_resp.json().get("results", [])
                    if res_list:
                        existing_id = res_list[0].get("id")

            if existing_id:
                assoc_url = f"{self.BASE_URL}/crm/v3/objects/contacts/{existing_id}/associations/companies/{company_id}/contact_to_company"
                assoc_resp = self._request_with_retry("PUT", assoc_url)
                assoc_resp.raise_for_status()
                logger.info(f"✅ Bestaand contact #{existing_id} succesvol gekoppeld aan HubSpot bedrijf #{company_id}")
                return {"id": existing_id, "associated": True}

        resp.raise_for_status()
        return resp.json()

    def update_contact(self, contact_id: str, properties: Dict[str, str]) -> Dict[str, Any]:
        """Werkt eigenschappen van een contactpersoon bij in HubSpot."""
        url = f"{self.BASE_URL}/crm/v3/objects/contacts/{contact_id}"
        resp = self._request_with_retry("PATCH", url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def subscribe_contact(self, email: str, legal_basis: str = "LEGITIMATE_INTEREST_CLIENT", explanation: str = "Ingeschreven via ZWOL Sync") -> Dict[str, Any]:
        """Schrijft een contactpersoon in voor de ZWOL Nieuwsbrief (123809007)."""
        email_clean = (email or "").strip().lower()
        if not email_clean:
            raise ValueError("E-mailadres is verplicht voor inschrijving.")

        url = f"{self.BASE_URL}/communication-preferences/v3/subscribe"
        payload = {
            "emailAddress": email_clean,
            "subscriptionId": self.NEWSLETTER_SUBSCRIPTION_ID,
            "legalBasis": legal_basis,
            "explanation": explanation
        }
        resp = self._request_with_retry("POST", url, json=payload)
        resp.raise_for_status()
        res_data = resp.json() if resp.text else {"status": "ok"}
        logger.info(f"✅ Contact '{email_clean}' succesvol ingeschreven voor nieuwsbrief (123809007).")
        return res_data

    def update_contact_newsletter_status_in_cache(self, email: str, new_status: str):
        """Werkt de nieuwsbrief-status van een contact bij in de lokale database cache."""
        email_clean = (email or "").strip().lower()
        if not email_clean:
            return

        prefix = Config.DB_TABLE_PREFIX
        with get_db() as db:
            rows = db.fetchall(f"SELECT hubspot_id, contacts_json FROM {prefix}hubspot_cache WHERE contacts_json LIKE %s", (f"%{email_clean}%",))
            for row in rows:
                c_json = row.get("contacts_json")
                if not c_json:
                    continue
                try:
                    contacts = json.loads(c_json)
                    changed = False
                    for c in contacts:
                        if (c.get("email") or "").strip().lower() == email_clean:
                            c["newsletter_status"] = new_status
                            changed = True
                    if changed:
                        db.execute(
                            f"UPDATE {prefix}hubspot_cache SET contacts_json = %s WHERE hubspot_id = %s",
                            (json.dumps(contacts), row["hubspot_id"])
                        )
                except Exception as e:
                    logger.warning(f"Kon contacts_json niet bijwerken voor {row.get('hubspot_id')}: {e}")



hubspot_client = HubSpotClient()

