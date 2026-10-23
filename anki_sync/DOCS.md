# Anki Sync

A headless Anki collection that syncs with AnkiWeb, so Claude can manage your flashcards through Home Assistant.

## Configuration

- `ankiweb_username`: your AnkiWeb email
- `ankiweb_password`: your AnkiWeb password (used once to get a sync key, then only the key is kept)
- `sync_interval_minutes`: how often to sync with AnkiWeb when nothing has changed (changes sync within about 10 seconds)

## First run

Make sure your phone has synced your latest collection to AnkiWeb first. On first start the app downloads your whole collection from AnkiWeb.

## Status

Open the **Anki Sync** panel in the sidebar to see card counts, login state and the last sync result.

See the full guide, including how normal and full syncs work: https://github.com/incogkneetoe/ha-anki-sync
