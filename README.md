# kindle-docs

Offizielle Dokumentation von DevOps-, Infrastruktur- und Security-Projekten als EPUB, mit einem
OPDS-Katalog für KOReader. Jede Nacht wird jede Doku-Seite geprüft; ein Buch wird nur neu gebaut,
wenn sich sein Text wirklich geändert hat.

- Katalog für E-Reader: `https://amid68.github.io/kindle-docs/catalog.xml`
- Übersicht im Browser: `https://amid68.github.io/kindle-docs/`

## Auf dem Kindle einrichten

1. Im Dateimanager von KOReader oben auf das Lupen-Symbol tippen, **OPDS catalog** wählen.
2. Oben links auf das Menü-Symbol (☰) tippen, **Add catalog** wählen.
3. Catalog name: `DevOps Docs`, Catalog URL: `https://amid68.github.io/kindle-docs/catalog.xml`, speichern.
4. Den neuen Eintrag öffnen, eine Kategorie wählen, ein Buch antippen und **EPUB** wählen.

Das Datum auf dem Cover ist der Stand der Doku. Steht im Katalog ein neueres Datum, das Buch einfach
noch einmal herunterladen und die alte Datei ersetzen lassen.

## Wie die Bücher aktuell bleiben

Der GitHub-Workflow `.github/workflows/build.yml` läuft jede Nacht um 02:17 UTC:

1. Er holt den veröffentlichten Stand (Branch `gh-pages`) und den HTTP-Cache der letzten Nacht.
2. `build.py` geht jede Doku-Seite in Lesereihenfolge ab. Dank ETag/Last-Modified antworten die
   meisten Server nur mit „unverändert“, das kostet fast nichts.
3. Aus dem bereinigten Text wird ein Fingerabdruck berechnet. Ist er gleich dem veröffentlichten,
   wird das Buch übersprungen: keine Bilder laden, kein pandoc, keine neue Datei.
4. Nur wenn sich mindestens eine Datei geändert hat, wird `gh-pages` neu veröffentlicht. Ohne
   Änderung passiert nichts.

Eine Änderung an `build.py` oder `epub.css` baut alle Bücher neu, eine Änderung an `books.toml`
nur die betroffenen.

Von Hand starten: GitHub → Actions → build → **Run workflow** (mit „force“ wird alles neu gebaut).

## Ein Buch hinzufügen

Einen Eintrag in `books.toml` anlegen:

```toml
[[book]]
id = "karpenter"
title = "Karpenter"
category = "Kubernetes"
start = "https://karpenter.sh/docs/"
```

Lokal testen, bevor du pushst:

```bash
python3 build.py --only karpenter --max-pages 20 --site /tmp/kd-test
```

Danach `/tmp/kd-test/books/karpenter.epub` ansehen. Optionen für schwierige Seiten (`scope`, `nav`,
`content`, `exclude`, `follow_content`, `method`) stehen oben in `books.toml`.

## Lokal bauen

```bash
pip install -r requirements.txt
brew install pandoc
python3 build.py
```

Ergebnis liegt in `site/`. Der Cache in `.cache/` macht weitere Läufe schnell.

## Lizenz der Inhalte

Die Texte gehören den jeweiligen Projekten (meist CC BY 4.0 oder Apache 2.0). Jedes Buch nennt auf
der ersten Seite seine Quelle.
