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

Jede Download-Adresse und jede Eintrags-ID trägt die Version (`books/falco.epub?v=…`). Dadurch
erkennen KOReaders OPDS-Sync und das Bookshelf-Plugin aktualisierte Bücher als neu: im
Bookshelf-Regal verliert ein aktualisiertes Buch sein „heruntergeladen“-Zeichen.

Das Datum auf dem Cover ist der Stand der Doku. Steht im Katalog ein neueres Datum, das Buch einfach
noch einmal herunterladen und die alte Datei ersetzen lassen.

## Wie die Bücher aktuell bleiben

Der GitHub-Workflow `.github/workflows/build.yml` läuft jede Nacht um 02:17 UTC:

1. Er holt den veröffentlichten Stand (Branch `gh-pages`) und den HTTP-Cache der letzten Nacht.
2. Für jedes Buch mit `git` in `books.toml` fragt er die GitHub-API nach dem letzten Commit im
   Doku-Ordner. Gibt es keinen neuen, wird das Buch übersprungen, ohne die Website abzurufen.
   Ein Abruf zählt nur, wenn er mindestens 6 Stunden nach dem letzten Commit lief (so lange darf
   der Deploy der Website dauern), und jedes Buch wird spätestens nach 7 Tagen trotzdem neu geprüft.
3. Alle übrigen Bücher (ohne `git` oder mit neuem Commit): `build.py` geht jede Doku-Seite in
   Lesereihenfolge ab. Dank ETag/Last-Modified antworten die meisten Server nur mit „unverändert“,
   das kostet fast nichts.
4. Hat keine einzige Seite eines Buchs neuen Inhalt geliefert (nur „304 unverändert“ oder derselbe
   Text) und ist die Seitenliste gleich, wird das Buch übersprungen, ohne HTML zu verarbeiten.
5. Sonst wird aus dem bereinigten Text ein Fingerabdruck berechnet. Ist er gleich dem veröffentlichten,
   wird das Buch übersprungen: keine Bilder laden, kein pandoc, keine neue Datei.
6. Nur wenn sich mindestens eine Datei geändert hat, wird `gh-pages` neu veröffentlicht. Ohne
   Änderung passiert nichts.

Eine Änderung an `epub.css` oder an `OUTPUT_VERSION` in `build.py` baut alle Bücher neu, eine Änderung an `books.toml`
nur die betroffenen.

### Cache

- `.cache/pages`: jede abgerufene Seite, zlib-komprimiert, mit ETag/Last-Modified. Wird zwischen
  den Läufen in GitHub gespeichert. Damit fragt der nächtliche Lauf nur „geändert?“ und bekommt
  meist ein kurzes „304 unverändert“ zurück.
- `.cache/git-state.json`: pro Buch der zuletzt geprüfte Commit und wann. Liegt mit im GitHub-Cache.
- `.cache/media`: Bilder. Nur lokal; in GitHub werden sie nur für Bücher geladen, die sich geändert
  haben.
- Antwortet ein Server mit 429 (zu viele Anfragen), wird für diesen Host automatisch langsamer
  angefragt. Bleibt eine Seite vorübergehend unerreichbar, wird die zwischengespeicherte Kopie
  genommen; die Änderung kommt dann in der nächsten Nacht.

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
