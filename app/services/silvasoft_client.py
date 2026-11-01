import time
import json
import logging
import datetime
import requests
from typing import Dict, List, Any, Optional
from app.config import Config
from app.database import get_db

logger = logging.getLogger("zwol_sync")


class SilvasoftClient:
    def __init__(self, username: Optional[str] = None, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.username = username or Config.SILVASOFT_USERNAME
        self.api_key = api_key or Config.SILVASOFT_API_KEY
        self.base_url = (base_url or Config.SILVASOFT_BASE_URL).rstrip("/")

    def _headers(self):
        return {
            "ApiKey": self.api_key,
            "Username": self.username,
            "Content-Type": "application/json"
        }

    def _request(self, method: str, endpoint: str, params=None, json_data=None):
        if not self.username or not self.api_key:
            raise ValueError("Silvasoft API-inloggegevens ontbreken in de configuratie.")

        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        for attempt in range(3):
            # Altijd 550ms wachten om onder de 2 req/sec te blijven
            time.sleep(0.55)
            try:
                resp = requests.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    json=json_data,
                    timeout=20,
                    verify=False
                )
                if (resp.status_code == 401 or resp.status_code == 429) and "Ratelimit exceeded" in resp.text:
                    if attempt == 2:
                        raise RuntimeError("Silvasoft API rate limit overschreden. Wacht a.u.b. en probeer het later.")
                    logger.warning(f"Silvasoft rate limit bereikt (poging {attempt+1}/3). 3s wachten...")
                    time.sleep(3.0)
                    continue

                if resp.status_code not in (200, 201):
                    logger.error(f"Silvasoft fout ({method} {endpoint} HTTP {resp.status_code}): {resp.text}")
                    raise RuntimeError(f"Silvasoft API fout ({resp.status_code}): {resp.text}")

                return resp.json()
            except requests.exceptions.RequestException as e:
                if attempt == 2:
                    logger.error(f"Fout bij communicatie met Silvasoft ({method} {endpoint}): {e}")
                    raise RuntimeError(f"Verbindingsfout met Silvasoft: {e}")
                time.sleep(1.5)

        raise RuntimeError(f"Silvasoft API aanroep naar {endpoint} mislukt na 3 pogingen.")

    def sync_companies_to_cache(self) -> int:
        """
        Haalt alle zakelijke klanten op uit Silvasoft en slaat ze op in sync_silvasoft_cache.
        Consumenten (PrivateRelation) worden genegeerd.
        """
        logger.info("Start ophalen zakelijke klanten uit Silvasoft...")
        offset = 0
        limit = 200
        all_relations = []

        while True:
            params = {
                "RelationType": "Customer",
                "relationtype": "Customer",
                "Limit": limit,
                "limit": limit,
                "Offset": offset,
                "offset": offset
            }
            data = self._request("GET", "listrelations/", params=params)
            if not isinstance(data, list) or len(data) == 0:
                break
            all_relations.extend(data)
            if len(data) < limit or len(all_relations) >= 3000:
                break
            offset += limit

        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = Config.DB_TABLE_PREFIX
        synced_count = 0

        with get_db() as db:
            for r in all_relations:
                customer_number = str(r.get("CustomerNumber") or 0)
                is_customer = bool(r.get("IsCustomer"))
                is_private = (r.get("PrivateOrBusiness") == "PrivateRelation")

                # Sla niet-klanten en consumenten over
                if is_private or not is_customer and (customer_number == "0" or not customer_number):
                    continue

                name = (r.get("Name") or "").strip()
                if not name:
                    continue

                relation_guid = str(r.get("RelationGUID") or "")
                kvk_number = str(r.get("RegistrationNumber") or "").strip()
                phone = (r.get("Phone") or "").strip()
                email = (r.get("Email") or "").strip()
                website = (r.get("Website") or "").strip()

                street = (r.get("Address_Street") or "").strip()
                unit = (r.get("Address_Unit") or "").strip()
                unit_appendix = (r.get("Address_UnitAppendix") or "").strip()
                postal_code = (r.get("Address_PostalCode") or "").strip()
                city = (r.get("Address_City") or "").strip()

                # Adres fallback naar Relation_Address indien leeg
                if not street and not city:
                    addrs = r.get("Relation_Address") or []
                    if addrs and isinstance(addrs, list) and len(addrs) > 0:
                        fa = addrs[0]
                        street = (fa.get("Address_Street") or "").strip()
                        unit = (fa.get("Address_Unit") or "").strip()
                        unit_appendix = (fa.get("Address_UnitAppendix") or "").strip()
                        postal_code = (fa.get("Address_PostalCode") or "").strip()
                        city = (fa.get("Address_City") or "").strip()

                # Contactpersonen uitlezen
                contacts_list = []
                primary_name = primary_email = primary_phone = ""
                raw_contacts = r.get("Relation_Contact") or []

                if raw_contacts and isinstance(raw_contacts, list):
                    for c in raw_contacts:
                        c_first = (c.get("FirstName") or "").strip()
                        c_prep = (c.get("Preposition") or c.get("Prefix") or "").strip()
                        c_last = (c.get("LastName") or c.get("FamilyName") or "").strip()
                        full_name = " ".join([p for p in [c_first, c_prep, c_last] if p])
                        c_mail = (c.get("Email") or "").strip()
                        c_phone = (c.get("Phone") or c.get("Mobile") or "").strip()
                        is_default = bool(c.get("DefaultContact"))

                        c_entry = {
                            "guid": str(c.get("ContactGUID") or ""),
                            "name": full_name,
                            "first_name": c_first,
                            "last_name": full_name[len(c_first):].strip() if full_name.startswith(c_first) else full_name,
                            "email": c_mail,
                            "phone": c_phone,
                            "is_primary": is_default
                        }
                        contacts_list.append(c_entry)

                        if is_default or not primary_name:
                            primary_name = full_name
                            primary_email = c_mail
                            primary_phone = c_phone

                contacts_json = json.dumps(contacts_list)
                raw_json = json.dumps(r)

                existing = db.fetchone(
                    f"SELECT customer_number FROM {prefix}silvasoft_cache WHERE customer_number = %s",
                    (customer_number,)
                )
                if existing:
                    db.execute(f"""
                        UPDATE {prefix}silvasoft_cache
                        SET relation_guid = %s, name = %s, kvk_number = %s,
                            street = %s, unit = %s, unit_appendix = %s, postal_code = %s, city = %s,
                            phone = %s, email = %s, website = %s, contacts_json = %s,
                            primary_contact_name = %s, primary_contact_email = %s, primary_contact_phone = %s,
                            raw_json = %s, synced_at = %s
                        WHERE customer_number = %s
                    """, (
                        relation_guid, name, kvk_number,
                        street, unit, unit_appendix, postal_code, city,
                        phone, email, website, contacts_json,
                        primary_name, primary_email, primary_phone,
                        raw_json, now_str, customer_number
                    ))
                else:
                    db.execute(f"""
                        INSERT INTO {prefix}silvasoft_cache (
                            customer_number, relation_guid, name, kvk_number,
                            street, unit, unit_appendix, postal_code, city,
                            phone, email, website, contacts_json,
                            primary_contact_name, primary_contact_email, primary_contact_phone,
                            raw_json, synced_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        customer_number, relation_guid, name, kvk_number,
                        street, unit, unit_appendix, postal_code, city,
                        phone, email, website, contacts_json,
                        primary_name, primary_email, primary_phone,
                        raw_json, now_str
                    ))

                synced_count += 1

            # Log sync
            db.execute(f"""
                INSERT INTO {prefix}sync_log (sync_type, status, records_count, message, created_at)
                VALUES (%s, %s, %s, %s, %s)
            """, ("silvasoft_cache", "success", synced_count, f"{synced_count} zakelijke relaties gesynchroniseerd", now_str))

        logger.info(f"Silvasoft cache bijgewerkt: {synced_count} bedrijven opgeslagen.")
        return synced_count

    def update_company(self, customer_number: str, payload: dict) -> dict:
        """Voert een gerichte update uit op een relatie in Silvasoft."""
        endpoint = "updaterelation/"
        payload["CustomerNumber"] = int(customer_number) if customer_number.isdigit() else customer_number
        logger.info(f"Update verzenden naar Silvasoft voor klant {customer_number}: {payload}")
        return self._request("POST", endpoint, json_data=payload)


silvasoft_client = SilvasoftClient()

