# 🔄 ZWOL Sync - Silvasoft & HubSpot Synchronisatie

Een desktop-georiënteerde webapplicatie om klant- en bedrijfsgegevens tussen **Silvasoft** (boekhouding / ERP) en **HubSpot** (CRM) naadloos, veilig en beheerst synchroon te houden.

---

## 🎯 Hoofddoelen & Uitgangspunten

1. **Puur Zakelijke Klanten (B2B):**
   * Alleen bedrijven worden gesynchroniseerd; particulieren/consumenten blijven buiten beschouwing.
2. **Leads blijven in HubSpot:**
   * Bedrijven die alleen in HubSpot staan (leads, prospects) worden **nooit** zomaar naar Silvasoft gestuurd. Silvasoft blijft 100% schoon.
3. **Klanten altijd in HubSpot:**
   * Bedrijven in Silvasoft die nog niet in HubSpot staan, kunnen met één klik worden aangemaakt in HubSpot.
4. **Veld-voor-veld Verschillen Beheren (Side-by-Side Diff):**
   * Overzichtelijke desktop-UI waarin per bedrijf en per veld (adres, telefoon, website, KvK, contactpersonen) te zien is wat de verschillen zijn.
   * Met duidelijke knoppen (`➔ Naar HubSpot` of `⬅ Naar Silvasoft`) bepaal je per datapunt welke kant de waarheid is.
5. **Smart Rate-Limit Queue voor Silvasoft:**
   * Wijzigingen naar HubSpot worden direct realtime doorgevoerd.
   * Wijzigingen naar Silvasoft worden in een wachtrij geplaatst en automatisch gedoseerd verwerkt (maximaal 1 verzoek per 4 minuten) om ruim binnen het gratis Silvasoft API-budget (15 req/uur) te blijven.
6. **Slimme Matching Engine:**
   * Automatische koppeling op basis van:
     1. HubSpot custom property `silvasoft_customer_number` (100% gouden match)
     2. KvK-nummer (99.9%)
     3. Postcode + Huisnummer (95%)
     4. Website / E-maildomein (90%)
     5. Genormaliseerde naam + Plaats (85%)
     6. Handmatige koppelings-modal voor twijfelgevallen

---

## 🏛️ Architectuur & Database

* **Backend:** Python 3.11 met Flask & Gunicorn.
* **Database:** MariaDB 10 op de Synology NAS (`127.0.0.1:3307`), database **`zwol`**, tabellen met prefix **`sync_`**:
  * `sync_silvasoft_cache`: Lokale spiegel van Silvasoft-klanten en contacten.
  * `sync_hubspot_cache`: Lokale spiegel van HubSpot-bedrijven en contacten.
  * `sync_matches`: Permanente koppeltabel tussen Silvasoft en HubSpot.
  * `sync_queue`: Achtergrond-wachtrij voor gedoseerde Silvasoft updates.
  * `sync_history`: Volledige audit trail van wie welk veld gewijzigd heeft.
  * `sync_log`: Logboek van achtergrondsynchronisaties.
* **Fallback:** Automatische overschakeling naar lokaal SQLite (`data/zwol_sync.db`) als MariaDB niet bereikbaar is tijdens lokaal testen.

---

## 🚀 Installatie & Deployment op Synology NAS

De applicatie is geconfigureerd om te draaien in `/volume1/docker/zwol-sync` via Docker Compose.

### 1. Repository clonen op de NAS
```bash
cd /volume1/docker
git clone https://github.com/koenc/zwol-sync.git
cd zwol-sync
```

### 2. `.env` configureren
Kopieer `.env.example` naar `.env`:
```bash
cp .env.example .env
```
Vul de inloggegevens in:
* `DB_HOST=127.0.0.1` (of `host.docker.internal` binnen Docker)
* `DB_PORT=3307`
* `DB_NAME=zwol`
* `DB_USER=ketelhuis`
* `DB_PASSWORD=...`
* `SILVASOFT_USERNAME=...`
* `SILVASOFT_API_KEY=...`
* `HUBSPOT_ACCESS_TOKEN=...`

### 3. Container starten
```bash
docker-compose up -d --build
```
De applicatie draait vervolgens op:
`http://<NAS-IP>:8505` (of via je Cloudflare tunnel op bijv. `sync.brouwerijzwol.nl`).
