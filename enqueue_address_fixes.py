import csv
import sys
import json
from pathlib import Path

sys.path.insert(0, r"C:\Users\koen\OneDrive\Anti-gravity\ZWOL Sync")
from app.config import Config
from app.database import get_db
from app.services.queue_worker import QueueService
from app.services.diff_service import format_silvasoft_address

CSV_PATH = Path(r"C:\Users\koen\OneDrive\Anti-gravity\ZWOL Sync\silvasoft_adres_controle.csv")

def main():
    if not CSV_PATH.exists():
        print(f"Fout: CSV-bestand niet gevonden op {CSV_PATH}")
        sys.exit(1)

    prefix = Config.DB_TABLE_PREFIX

    with open(CSV_PATH, mode="r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f, delimiter=";")
        rows = list(reader)

    print(f"Inlezen van {len(rows)} rijen uit CSV...")

    with get_db() as db:
        pending_tasks = db.fetchall(f"""
            SELECT id, entity_id, field_name, new_value, scheduled_at 
            FROM {prefix}queue 
            WHERE target = 'SILVASOFT' AND action = 'UPDATE_FIELD' AND status = 'PENDING'
        """)

    # Indexeer bestaande pending taken per (entity_id, field_name)
    pending_map = {(str(p["entity_id"]), str(p["field_name"])): p for p in pending_tasks}

    street_updates = 0
    street_inserts = 0
    city_updates = 0
    city_inserts = 0
    skipped = 0

    print("\nVerwerken van adrescorrecties naar wachtrij...")

    for row in rows:
        action = row.get("Aanbevolen_Actie") or ""
        if "corrigeren" not in action:
            continue

        cno = str(row["Klantnummer"]).strip()
        name = row.get("Bedrijfsnaam") or ""
        bag_street = (row.get("Straat_BAG_Kadaster") or "").strip()
        bag_city = (row.get("Plaats_BAG_Kadaster") or "").strip()
        ss_street = (row.get("Straat_Silvasoft") or "").strip()
        ss_unit = (row.get("Huisnummer_Silvasoft") or "").strip()
        ss_app = (row.get("Toevoeging_Silvasoft") or "").strip()
        ss_city = (row.get("Plaats_Silvasoft") or "").strip()

        # 1. Straatcorrectie
        if row.get("Status_Straat") == "AFWIJKEND" and bag_street:
            formatted_street = format_silvasoft_address(bag_street, ss_unit, ss_app)
            payload = {
                "Address_Street": bag_street,
                "Address_Unit": ss_unit,
                "Address_UnitAppendix": ss_app
            }

            existing = pending_map.get((cno, "street"))
            if existing:
                with get_db() as db:
                    db.execute(f"""
                        UPDATE {prefix}queue 
                        SET new_value = %s, payload_json = %s 
                        WHERE id = %s
                    """, (formatted_street, json.dumps(payload), existing["id"]))
                street_updates += 1
                print(f"[BIJGEWERKT] Klant #{cno} ({name[:25]}): Taak #{existing['id']} straat gecorrigeerd naar '{formatted_street}' (gepland om {existing['scheduled_at']})")
            else:
                task_id = QueueService.enqueue(
                    target="SILVASOFT",
                    action="UPDATE_FIELD",
                    entity_id=cno,
                    field_name="street",
                    old_val=ss_street,
                    new_val=formatted_street,
                    payload=payload
                )
                street_inserts += 1
                print(f"[NIEUW] Klant #{cno} ({name[:25]}): Nieuwe taak #{task_id} aangemaakt voor straat '{formatted_street}'")

        # 2. Plaatsnaamcorrectie (alleen bij echte mismatches, zoals Klant #102 Monster -> Naaldwijk)
        # Den Haag vs 's-Gravenhage slaan we over omdat beide hetzelfde betekenen
        if row.get("Status_Plaats") == "AFWIJKEND" and bag_city:
            is_synonym = (ss_city.lower() in ("den haag", "'s-gravenhage") and bag_city.lower() in ("den haag", "'s-gravenhage"))
            if not is_synonym:
                existing_city = pending_map.get((cno, "city"))
                if existing_city:
                    with get_db() as db:
                        db.execute(f"""
                            UPDATE {prefix}queue 
                            SET new_value = %s 
                            WHERE id = %s
                        """, (bag_city, existing_city["id"]))
                    city_updates += 1
                    print(f"[BIJGEWERKT] Klant #{cno} ({name[:25]}): Taak #{existing_city['id']} plaatsnaam gecorrigeerd naar '{bag_city}'")
                else:
                    task_id = QueueService.enqueue(
                        target="SILVASOFT",
                        action="UPDATE_FIELD",
                        entity_id=cno,
                        field_name="city",
                        old_val=ss_city,
                        new_val=bag_city
                    )
                    city_inserts += 1
                    print(f"[NIEUW] Klant #{cno} ({name[:25]}): Nieuwe taak #{task_id} aangemaakt voor plaats '{bag_city}'")
            else:
                skipped += 1

    print("\n=== KLAAR MET WACHTRIJ VULLEN ===")
    print(f"Bestaande straattaken in wachtrij geüpdatet met BAG-naam: {street_updates}")
    print(f"Nieuwe straattaken toegevoegd aan wachtrij: {street_inserts}")
    print(f"Bestaande plaatstaken geüpdatet: {city_updates}")
    print(f"Nieuwe plaatstaken toegevoegd: {city_inserts}")
    print(f"Synoniemen overgeslagen (Den Haag): {skipped}")
    print(f"Totaal aantal actieve correcties in de wachtrij: {street_updates + street_inserts + city_updates + city_inserts}")

if __name__ == "__main__":
    main()
