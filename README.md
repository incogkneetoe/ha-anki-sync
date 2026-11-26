# Anki Sync for Home Assistant

Lets Claude add, edit and delete Mark's AnkiDroid flashcards from any chat, with nothing manual on the phone except a normal sync.

## The big picture

```
 You, in any Claude chat
        |
        |  "add these 10 words to Vocabulary"
        v
 Claude (claude.ai)
        |
        |  ha-mcp connector  (already set up, already authenticated,
        |                     reaches the Pi through Tailscale Funnel)
        v
 Home Assistant on the Pi 5
        |
        |  Ingress  (Home Assistant's private doorway into its apps;
        |            nothing here is exposed to the internet)
        v
 Anki Sync app  (this repo, running as a Home Assistant app)
   - holds a full copy of your Anki collection
   - edits it with the official Anki engine
        |
        |  syncs every 15 minutes, and about 10 seconds after any change
        v
 AnkiWeb  (Anki's free sync server)
        ^
        |  auto sync when you open or close the app
        |
 AnkiDroid on your phone
```

Think of AnkiWeb as the shared hub. Your phone and the Pi are two copies of the same collection, and both sync to the hub. This is exactly how Anki desktop on a computer would work alongside your phone. The Pi is simply a computer running Anki with no screen, and Claude is the one "using" it.

## The pieces

| Piece | What it is | Where it lives |
|---|---|---|
| **Anki Sync app** | A small Python program built on the official `anki` library (the same engine inside Anki desktop). It keeps a collection file on the Pi and answers requests like "add this note". | This repo. Home Assistant downloads it and builds it on the Pi. |
| **Collection** | Your cards, decks, note types and review history in one database file. | `/data/collection.anki2` inside the app's private storage on the Pi. Included in your Home Assistant backups. |
| **AnkiWeb login** | Your AnkiWeb email and password. | Typed by you into the app's Configuration tab. The app trades them for a sync key on first login and keeps only that key (`/data/auth.json`). Never stored in this repo. |
| **ha-mcp** | The connector that already lets Claude control Home Assistant. It can pass requests through to an app's API. | Already installed on Home Assistant. |
| **Ingress** | Home Assistant's built-in proxy into apps. Only Home Assistant itself can reach the app; the app refuses every other connection. | Built into Home Assistant. |

## How a change flows

1. You ask Claude for a change in any chat.
2. Claude sends a request through ha-mcp to the Anki Sync app, for example `addNote` with the word, definition and pronunciation.
3. The app makes the change in the collection on the Pi and marks it as pending.
4. About 10 seconds later the app syncs with AnkiWeb.
5. Next time you open or close AnkiDroid (with automatic sync on), the phone syncs and the card appears.

Reviews flow the other way: when you study on the phone and it syncs, the Pi picks up your progress on its next sync. That lets Claude see what you're struggling with, such as leeches and lapse counts.

## Normal syncs vs. full syncs

This is the one thing worth understanding well.

- **Normal sync:** each side sends only what changed. Both sides can change different cards, or even the same card, and Anki merges them. Adding, editing, deleting, tagging, moving cards between decks and studying are all normal syncs. This is 95% of what Claude will do.
- **Full sync:** one side completely replaces the other. Anki requires this after *structural* changes, meaning creating a new note type, or adding or removing fields or card templates. Anki can't merge those, so one copy has to win.

How the app handles full syncs:

- **First run:** the Pi's collection is empty, so it automatically downloads everything from AnkiWeb.
- **After that, it never overwrites anything on its own.** If a full sync is needed, the status shows `full_sync_required` and Claude has to choose deliberately.
- **Safe procedure for structural changes:** sync your phone first, then Claude makes the change and uploads. Your phone will then say a full sync is needed; choose **Download** (keep AnkiWeb's version). Nothing is lost because the phone had already synced.

If you study on the phone *between* your last sync and Claude's upload, those reviews are lost when the phone downloads. That's why the rule is: phone syncs first.

## Setup (one time)

1. **Get AnkiWeb up to date.** Import your latest collection file on the phone, then sync. If AnkiDroid asks, choose **Upload** so AnkiWeb holds the cleaned-up collection.
2. **Turn on auto sync in AnkiDroid:** Settings, then Sync, then Automatic synchronization.
3. **Install the app** (Claude can do this through ha-mcp): add this repository's URL to the Home Assistant app store, then install **Anki Sync**. Home Assistant builds it on the Pi, which takes a few minutes the first time.
4. **Enter your AnkiWeb login:** Settings, then Apps, then Anki Sync, then the Configuration tab. Fill in `ankiweb_username` and `ankiweb_password`, save, then start the app.
5. **Check it:** open the Anki Sync panel in the Home Assistant sidebar. It should show your card count and a recent `last_sync_result` of "full download complete (first sync)", then "synced".

## Everyday use

Just ask Claude, in any chat:

- "Add *perspicacious* to Vocabulary."
- "Fix the pronunciation on Seigniorage."
- "Delete the Bulgarian flag card."
- "Which cards keep failing?"
- "Lower my new cards per day to 15."

Then open AnkiDroid, and the change is there after it syncs.

## Security

- The app only accepts connections from Home Assistant's Ingress proxy. It has no port open on your network or the internet.
- Claude reaches it only through ha-mcp, which is already protected by its own secret webhook URL over Tailscale Funnel.
- The repo is public but contains only code. Your password, sync key and cards never leave the Pi and AnkiWeb.
- To cut Claude off from your cards, stop the Anki Sync app (or remove the ha-mcp connector).

## Troubleshooting

| Symptom | Fix |
|---|---|
| Panel shows `login_error` | Check the username and password in the Configuration tab, then restart the app. |
| `full_sync_required` in the status | Sync the phone, then ask Claude to finish the sync (it will upload or download as appropriate). |
| Phone doesn't show a new card | Pull down to sync in AnkiDroid. Check the panel's `last_sync` time. |
| App won't start after an update | Check the app's Log tab and ask Claude to look at it. |

## API reference (for Claude)

`POST /` with JSON `{"action": "<name>", "params": {...}}`. The response is `{"result": ..., "error": null | "message"}`. These are AnkiConnect-compatible names where AnkiConnect has an equivalent. Reach it with ha-mcp's `ha_manage_app` in proxy mode (`slug` = the installed slug, `path` = `/`, `method` = `POST`).

| Action | Params | Notes |
|---|---|---|
| `status` | | counts, decks, login and sync state |
| `sync` | `mode`: `auto` (default), `upload`, `download` | `upload` and `download` are full syncs; follow the safe procedure above |
| `deckNames`, `createDeck`, `deleteDecks` | `deck`; `decks`, `cardsToo` | |
| `modelNames`, `modelFieldNames`, `modelTemplates`, `modelStyling` | `modelName` | |
| `createModel` | `modelName`, `inOrderFields`, `cardTemplates` [{Name, Front, Back}], `css`, `isCloze` | structural: needs a full sync |
| `updateModelTemplates`, `updateModelStyling` | `model` {name, templates or css} | |
| `findNotes`, `findCards` | `query` (Anki search syntax) | |
| `notesInfo` | `notes` [ids] or `query` | fields, tags, decks, card ids |
| `cardsInfo` | `cards` [ids] | interval, lapses, reps, due |
| `addNote`, `addNotes` | `note` / `notes`: {deckName, modelName, fields, tags, options.allowDuplicate} | rejects duplicates by default |
| `updateNoteFields`, `updateNote` | `note` {id, fields, tags} | |
| `addTags`, `removeTags` | `notes`, `tags` | |
| `deleteNotes` | `notes` | |
| `changeDeck` | `cards`, `deck` | |
| `suspend`, `unsuspend` | `cards` | |
| `setSpecificValueOfCard` | `card`, `keys`, `newValues` | keys: lapses, reps, factor, ivl, due (e.g. clear leech lapses, reset ease) |
| `forgetCards` | `cards`, `restorePosition`, `resetCounts` | reset cards to new (Anki's Forget) |
| `changeNoteType` | `notes`, `newModelName`, `fieldMap` {newField: oldField} | structural: needs a full sync |
| `getDeckLimits`, `setDeckLimits` | `deck`, `newPerDay`, `reviewsPerDay` | |
| `multi` | `actions` [{action, params}] | batch several calls |
| `version` | | returns 6 |

## Repo layout

```
repository.yaml          tells Home Assistant this repo is an app store
anki_sync/config.yaml    app definition: name, version, options, Ingress
anki_sync/Dockerfile     builds the app: Python + the official anki library
anki_sync/server.py      the app itself
anki_sync/DOCS.md        short help shown in Home Assistant's Documentation tab
```
