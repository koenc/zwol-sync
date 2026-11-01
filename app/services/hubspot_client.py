import time
import json
import logging
import datetime
import requests
import urllib3
from typing import Dict, List, Any, Optional
from app.config import Config
from app.database import get_db

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("zwol_sync")


class HubSpotClient:
    BASE_URL = "https://api.hubapi.com"

    COMPANY_PROPERTIES = [
        "name",
        "domain",
        "website",
        "address",
        "city",
        "zip",
        "country",
        "phone",
        "kvk_nummer",
        "kvk",
        "silvasoft_customer_number",
        "silvasoft_sync_status",
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
        "associatedcompanyid"
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
        """Controleert of 'silvasoft_customer_number' bestaat bij bedrijven in HubSpot, maakt deze anders aan."""
        if not self.access_token:
            return
        url = f"{self.BASE_URL}/crm/v3/properties/companies/silvasoft_customer_number"
        resp = self._request_with_retry("GET", url)
        if resp.status_code == 404:
            logger.info("Aanmaken custom property 'silvasoft_customer_number' in HubSpot...")
            create_url = f"{self.BASE_URL}/crm/v3/properties/companies"
            payload = {
                "name": "silvasoft_customer_number",
                "label": "Silvasoft Klantnummer",
                "type": "string",
                "fieldType": "text",
                "groupName": "companyinformation",
                "description": "Klantnummer gekoppeld vanuit Silvasoft boekhouding."
            }
            c_resp = self._request_with_retry("POST", create_url, json=payload)
            if c_resp.status_code in (200, 201):
                logger.info("✅ Property 'silvasoft_customer_number' succesvol aangemaakt in HubSpot.")

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
                kvk = (props.get("kvk_nummer") or props.get("kvk") or "").strip()
                street = (props.get("address") or "").strip()
                zip_code = (props.get("zip") or "").strip()
                city = (props.get("city") or "").strip()
                phone = (props.get("phone") or "").strip()
                silvasoft_cust_no = (props.get("silvasoft_customer_number") or "").strip()

                # Gekoppelde contacten verzamelen
                contact_list = []
                primary_name = primary_email = primary_phone = ""
                assocs = c.get("associations", {}).get("contacts", {}).get("results", [])
                for a in assocs:
                    cid = str(a.get("id"))
                    c_data = contacts_by_id.get(cid)
                    if c_data:
                        c_props = c_data.get("properties") or {}
                        c_first = (c_props.get("firstname") or "").strip()
                        c_last = (c_props.get("lastname") or "").strip()
                        c_full = f"{c_first} {c_last}".strip()
                        c_mail = (c_props.get("email") or "").strip()
                        c_ph = (c_props.get("phone") or c_props.get("mobilephone") or "").strip()
                        c_job = (c_props.get("jobtitle") or "").strip()

                        contact_entry = {
                            "id": cid,
                            "name": c_full,
                            "first_name": c_first,
                            "last_name": c_last,
                            "email": c_mail,
                            "phone": c_ph,
                            "job_title": c_job,
                            "is_primary": False
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
                            street = %s, postal_code = %s, city = %s, phone = %s,
                            silvasoft_customer_number = %s, contacts_json = %s,
                            primary_contact_name = %s, primary_contact_email = %s, primary_contact_phone = %s,
                            raw_json = %s, synced_at = %s
                        WHERE hubspot_id = %s
                    """, (
                        name, domain, website, kvk,
                        street, zip_code, city, phone,
                        silvasoft_cust_no, contacts_json,
                        primary_name, primary_email, primary_phone,
                        raw_json, now_str, hubspot_id
                    ))
                else:
                    db.execute(f"""
                        INSERT INTO {prefix}hubspot_cache (
                            hubspot_id, name, domain, website, kvk_number,
                            street, postal_code, city, phone,
                            silvasoft_customer_number, contacts_json,
                            primary_contact_name, primary_contact_email, primary_contact_phone,
                            raw_json, synced_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        hubspot_id, name, domain, website, kvk,
                        street, zip_code, city, phone,
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
        resp = self._request_with_retry("POST", url, json={"properties": properties})
        resp.raise_for_status()
        return resp.json()

    def create_and_associate_contact(self, company_id: str, contact_data: Dict[str, str]) -> Dict[str, Any]:
        """Maakt een contactpersoon aan en koppelt deze direct aan het bedrijf."""
        url = f"{self.BASE_URL}/crm/v3/objects/contacts"
        payload = {
            "properties": {
                "firstname": contact_data.get("first_name", ""),
                "lastname": contact_data.get("last_name", ""),
                "email": contact_data.get("email", ""),
                "phone": contact_data.get("phone", "")
            },
            "associations": [
                {
                    "to": {"id": company_id},
                    "types": [
                        {
                            "associationCategory": "HUBSPOT_DEFINED",
                            "associationTypeId": 2  # contact_to_company
                        }
                    ]
                }
            ]
        }
        resp = self._request_with_retry("POST", url, json=payload)
        resp.raise_for_status()
        return resp.json()


hubspot_client = HubSpotClient()

