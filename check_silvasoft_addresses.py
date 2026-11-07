import re
import csv
import sys
import time
import requests
import urllib3
from pathlib import Path

urllib3.disable_warnings()

sys.path.insert(0, r"C:\Users\koen\OneDrive\Anti-gravity\ZWOL Sync")
from app.database import get_db

PDOK_BASE_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"

def normalize_pc(pc: str) -> str:
    if not pc:
        return ""
    return re.sub(r"\s+", "", str(pc)).upper()

def extract_hn(unit: str) -> str:
    if not unit:
        return ""
    m = re.search(r"\b(\d+)\b", str(unit))
    return m.group(1) if m else ""

def lookup_bag(postal_code: str, house_number: str = ""):
    pc = normalize_pc(postal_code)
    # Check if valid 6-char Dutch postal code (4 digits + 2 letters)
    if not re.match(r"^\d{4}[A-Z]{2}$", pc):
        return None, None, "GEEN_GELDIGE_NL_POSTCODE"
    
    hn = extract_hn(house_number)
    
    # 1. Probeer eerst met postcode + huisnummer
    try:
        if hn:
            url = f"{PDOK_BASE_URL}?fq=postcode:{pc}&fq=huisnummer:{hn}"
            resp = requests.get(url, verify=False, timeout=8)
            if resp.status_code == 200:
                docs = resp.json().get("response", {}).get("docs", [])
                if docs:
                    d = docs[0]
                    return d.get("straatnaam"), d.get("woonplaatsnaam"), "BAG_EXACT"

        # 2. Fallback: alleen postcode (geeft altijd straat en plaats van het postcodegebied)
        url = f"{PDOK_BASE_URL}?fq=postcode:{pc}"
        resp = requests.get(url, verify=False, timeout=8)
        if resp.status_code == 200:
            docs = resp.json().get("response", {}).get("docs", [])
            if docs:
                d = docs[0]
                return d.get("straatnaam"), d.get("woonplaatsnaam"), "BAG_POSTCODE_ONLY"
                
        return None, None, "NIET_GEVONDEN_IN_BAG"
    except Exception as e:
        return None, None, f"FOUT: {e}"

def run_check():
    print("Ophalen van Silvasoft relaties, matches en HubSpot cache...")
    with get_db() as db:
        ss_rows = db.fetchall("SELECT * FROM sync_silvasoft_cache ORDER BY CAST(customer_number AS SIGNED) ASC")
        matches = db.fetchall("SELECT * FROM sync_matches")
        hs_rows = db.fetchall("SELECT * FROM sync_hubspot_cache")

    match_by_ss = {str(m["silvasoft_customer_number"]): str(m["hubspot_id"]) for m in matches}
    hs_by_id = {str(h["hubspot_id"]): h for h in hs_rows}

    results = []
    total = len(ss_rows)
    print(f"Totaal {total} relaties te controleren.")

    mismatch_street_count = 0
    mismatch_city_count = 0
    both_mismatch_count = 0
    ok_count = 0
    no_pc_count = 0

    for idx, ss in enumerate(ss_rows, start=1):
        cno = str(ss["customer_number"])
        name = ss.get("name") or ""
        ss_street = (ss.get("street") or "").strip()
        ss_unit = (ss.get("unit") or "").strip()
        ss_app = (ss.get("unit_appendix") or "").strip()
        ss_pc = (ss.get("postal_code") or "").strip()
        ss_city = (ss.get("city") or "").strip()

        hs_id = match_by_ss.get(cno)
        hs = hs_by_id.get(hs_id) if hs_id else None
        hs_street = (hs.get("street") or "").strip() if hs else ""
        hs_city = (hs.get("city") or "").strip() if hs else ""

        bag_street, bag_city, bag_status = lookup_bag(ss_pc, ss_unit)
        time.sleep(0.04) # Beleefde pauze

        # Vergelijk straat
        street_status = "ONBEKEND"
        if bag_street:
            if ss_street.lower() == bag_street.lower():
                street_status = "GELIJK"
            else:
                street_status = "AFWIJKEND"
        elif not ss_pc:
            street_status = "GEEN_POSTCODE"

        # Vergelijk plaats
        city_status = "ONBEKEND"
        if bag_city:
            if ss_city.lower() == bag_city.lower():
                city_status = "GELIJK"
            else:
                city_status = "AFWIJKEND"
        elif not ss_pc:
            city_status = "GEEN_POSTCODE"

        # Aanbevolen actie
        action = "Geen actie"
        if street_status == "AFWIJKEND" and city_status == "AFWIJKEND":
            action = "Beide corrigeren"
            both_mismatch_count += 1
        elif street_status == "AFWIJKEND":
            action = "Straat corrigeren"
            mismatch_street_count += 1
        elif city_status == "AFWIJKEND":
            action = "Plaats corrigeren"
            mismatch_city_count += 1
        elif street_status == "GELIJK" and city_status == "GELIJK":
            action = "Geen actie (klopt)"
            ok_count += 1
        else:
            action = "Handmatig nakijken (geen/onjuiste postcode)"
            no_pc_count += 1

        results.append({
            "Klantnummer": cno,
            "Bedrijfsnaam": name,
            "Postcode": ss_pc,
            "Huisnummer_Silvasoft": ss_unit,
            "Toevoeging_Silvasoft": ss_app,
            "Straat_Silvasoft": ss_street,
            "Straat_BAG_Kadaster": bag_street or "",
            "Straat_HubSpot": hs_street,
            "Plaats_Silvasoft": ss_city,
            "Plaats_BAG_Kadaster": bag_city or "",
            "Plaats_HubSpot": hs_city,
            "Status_Straat": street_status,
            "Status_Plaats": city_status,
            "Aanbevolen_Actie": action
        })

        if idx % 25 == 0 or idx == total:
            print(f"Voortgang: {idx}/{total} gecontroleerd...")

    # CSV wegschrijven (UTF-8-SIG voor naadloos openen in Excel met speciale tekens)
    out_path = Path(r"C:\Users\koen\OneDrive\Anti-gravity\ZWOL Sync\silvasoft_adres_controle.csv")
    fieldnames = [
        "Klantnummer", "Bedrijfsnaam", "Postcode", 
        "Huisnummer_Silvasoft", "Toevoeging_Silvasoft",
        "Straat_Silvasoft", "Straat_BAG_Kadaster", "Straat_HubSpot",
        "Plaats_Silvasoft", "Plaats_BAG_Kadaster", "Plaats_HubSpot",
        "Status_Straat", "Status_Plaats", "Aanbevolen_Actie"
    ]

    with open(out_path, mode="w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=";")
        writer.writeheader()
        writer.writerows(results)

    print("\n=== RESULTATEN VAN DE CONTROLE ===")
    print(f"Totaal gecontroleerd: {total}")
    print(f"100% OK (Straat & Plaats kloppen al): {ok_count}")
    print(f"Alleen straatnaam wijkt af: {mismatch_street_count}")
    print(f"Alleen plaatsnaam wijkt af: {mismatch_city_count}")
    print(f"Zowel straat als plaats wijken af: {both_mismatch_count}")
    print(f"Totaal afwijkingen te fixen: {mismatch_street_count + mismatch_city_count + both_mismatch_count}")
    print(f"Geen/ongeldige postcode (handmatig): {no_pc_count}")
    print(f"CSV opgeslagen op: {out_path}")

if __name__ == "__main__":
    run_check()

