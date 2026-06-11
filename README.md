# 📖 Log Book — Home Assistant Event-Topologie

Ein erweitertes Logbuch für Home Assistant: Es liest die echten HA-Logbook-Events
und macht sichtbar **warum** sich Geräte ändern — als interaktiver Prozess-Graph
(Auslöser → Automatisierung → Folge-Aktionen), inklusive echter Bedingungs-Ergebnisse
aus den Automation-Traces.

> ⚠️ **Sicherheit:** Dieses Repo enthält **keine** Zugangsdaten. Deinen Home-Assistant
> Token trägst du lokal in `config.json` ein (siehe unten) — diese Datei ist per
> `.gitignore` ausgeschlossen und darf **niemals** committed werden.

## ✨ Features

- 🔎 **Volltextsuche** über alle Logs (Nachricht, Gerät, Entität, Benutzer, Automatisierung)
- 🕸️ **Prozess-Topologie**: interaktiver Node-Graph, der die Ursachenkette eines Events zeigt
  (Benutzer → Helfer → Automatisierung → Folge-Aktionen) — beliebig nach oben verfolgbar
- ⚙️ **Echte Automatisierungs-Config** (Trigger/Bedingungen/Aktionen) im HA-Stil dargestellt
- ✅/❌ **Bedingungs-Ergebnisse aus echten Traces** (welche Bedingung war wahr/falsch)
- ⛔ **Fehlgeschlagene Automatisierungen** (durch Bedingungen blockiert) werden erfasst und angezeigt
- 🗄️ **Trace-Archiv (60 Tage)** — Bedingungs-Ergebnisse bleiben verfügbar, auch nachdem HA seine letzten Traces überschreibt
- 📟 **Geräte-Detailseite** mit Statistik, häufigsten Auslösern/Benutzern und Muster-Erkennung
- 🔍 **Muster-Erkennung** (Regelmäßigkeit, Uhrzeit-Häufung, Korrelationen)
- ⭐ **Watchlist & Alerts** für beobachtete Geräte
- 🟢 **Live-Indikator** + ⬇ **CSV/JSON-Export**
- 🌙 Dark Mode

## 🔑 Voraussetzungen

- Home Assistant mit aktiviertem **Recorder/Logbook**
- Ein **Long-Lived Access Token** (HA → Profil → *Sicherheit* → *Langlebige Zugangstokens*)
- Python 3.10+ (für den Standalone-Server)

## ⚙️ Einrichtung (Standalone-Server)

Aktuell läuft Log Book als eigenständiger Server, der die volle Oberfläche bereitstellt.

```bash
# 1) Abhängigkeiten
pip install -r requirements.txt

# 2) Konfiguration anlegen
cp config.example.json config.json
#   -> ha_url + ha_token eintragen
#   (alternativ per Umgebungsvariablen: HA_URL, HA_TOKEN)

# 3) Echte HA-Logs einmalig laden
python populate_realistic_logs.py

# 4) Server starten
python server.py
#   -> http://localhost:8080
```

Der Server holt fortlaufend neue Events sowie Automation-Traces live aus Home Assistant.

## 📦 Installation über HACS

1. HACS → **⋮** → *Benutzerdefinierte Repositories*
2. Repository-URL eintragen: `https://github.com/Dragoberts/log_book`
   — Kategorie **Integration**
3. „Log Book" installieren und Home Assistant neu starten.

> ℹ️ **Hinweis:** Über HACS wird die Integration unter `custom_components/log_book`
> installiert. Die reichhaltige Topologie-Oberfläche wird derzeit vom mitgelieferten
> Standalone-Server (`server.py`) bereitgestellt. Eine vollständige Einbindung als
> natives HA-Sidebar-Panel (Ingress) ist als nächster Schritt geplant.

## 🛠️ Konfiguration

| Schlüssel  | Beschreibung                                   | Beispiel                          |
|------------|------------------------------------------------|-----------------------------------|
| `ha_url`   | URL deiner Home-Assistant-Instanz              | `http://homeassistant.local:8123` |
| `ha_token` | Langlebiger Zugangstoken                       | `eyJ…`                            |

Werte können auch über die Umgebungsvariablen `HA_URL` / `HA_TOKEN` gesetzt werden
(diese haben Vorrang vor `config.json`).

## 📄 Lizenz

MIT
