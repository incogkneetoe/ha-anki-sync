"""Anki Sync: a headless Anki collection for Home Assistant.

Serves an AnkiConnect-style JSON API on port 8765 (reached through Home
Assistant Ingress) and keeps the collection in sync with AnkiWeb.

POST / with {"action": "...", "params": {...}} returns {"result": ..., "error": ...}.
GET / shows a small status page.
"""

import html
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from anki.collection import Collection
from anki.notes import NoteFieldsCheckResult
from anki.sync_pb2 import SyncAuth

DATA = os.environ.get("ANKI_SYNC_DATA", "/data")
COL_PATH = os.path.join(DATA, "collection.anki2")
AUTH_PATH = os.path.join(DATA, "auth.json")
OPTIONS_PATH = os.path.join(DATA, "options.json")
PORT = 8765
# Ingress requests arrive from the Supervisor; nothing else may talk to us.
ALLOWED_CLIENTS = {"172.30.32.2", "127.0.0.1"}

lock = threading.RLock()
state = {
    "last_sync": None,
    "last_sync_result": "never synced",
    "login_error": None,
    "dirty": False,
}


def log(msg):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def load_options():
    try:
        with open(OPTIONS_PATH) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


OPTIONS = load_options()
col = Collection(COL_PATH)


# ---------------------------------------------------------------- AnkiWeb auth

def load_auth():
    try:
        with open(AUTH_PATH) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    if data.get("username") != OPTIONS.get("ankiweb_username"):
        return None
    return SyncAuth(hkey=data["hkey"], endpoint=data.get("endpoint") or None)


def save_auth(auth, username):
    with open(AUTH_PATH, "w") as f:
        json.dump({"username": username, "hkey": auth.hkey, "endpoint": auth.endpoint}, f)
    os.chmod(AUTH_PATH, 0o600)


def get_auth():
    auth = load_auth()
    if auth:
        return auth
    user = OPTIONS.get("ankiweb_username") or ""
    pw = OPTIONS.get("ankiweb_password") or ""
    if not user or not pw:
        state["login_error"] = "AnkiWeb username and password not set in the app configuration"
        return None
    try:
        auth = col.sync_login(username=user, password=pw, endpoint=None)
    except Exception as e:  # wrong password, network, etc.
        state["login_error"] = f"AnkiWeb login failed: {e}"
        log(state["login_error"])
        return None
    state["login_error"] = None
    save_auth(auth, user)
    log("Logged in to AnkiWeb")
    return auth


# ---------------------------------------------------------------- sync

def full_sync(auth, server_usn, upload):
    col.close_for_full_sync()
    try:
        col.full_upload_or_download(auth=auth, server_usn=server_usn, upload=upload)
    finally:
        col.reopen(after_full_sync=True)


def do_sync(mode="auto"):
    """mode: auto (normal sync; full download only when this collection is empty),
    upload (force full upload, overwriting AnkiWeb), download (force full download)."""
    with lock:
        auth = get_auth()
        if not auth:
            raise Exception(state["login_error"])
        if mode in ("upload", "download"):
            full_sync(auth, None, mode == "upload")
            result = f"full {mode} complete"
        else:
            out = col.sync_collection(auth, sync_media=False)
            if out.new_endpoint:
                auth = SyncAuth(hkey=auth.hkey, endpoint=out.new_endpoint)
                save_auth(auth, OPTIONS.get("ankiweb_username"))
            req = out.required
            if req in (out.NO_CHANGES, out.NORMAL_SYNC):
                result = "synced"
            elif req == out.FULL_DOWNLOAD or (req == out.FULL_SYNC and col.card_count() == 0):
                if col.card_count() == 0:
                    full_sync(auth, None, False)
                    result = "full download complete (first sync)"
                else:
                    result = "full_sync_required: AnkiWeb wants this collection replaced by a download. Call sync with mode=download to accept."
            elif req == out.FULL_UPLOAD:
                result = "full_sync_required: AnkiWeb needs a full upload from here. Make sure the phone has synced first, then call sync with mode=upload."
            else:
                result = "full_sync_required: both sides changed in a way that needs a one-way sync. Sync the phone first, then call sync with mode=upload (keep this copy) or mode=download (keep AnkiWeb's)."
            if out.server_message:
                result += f" | AnkiWeb: {out.server_message}"
        state["last_sync"] = time.strftime("%Y-%m-%d %H:%M:%S")
        state["last_sync_result"] = result
        if not result.startswith("full_sync_required"):
            state["dirty"] = False
        log(f"Sync ({mode}): {result}")
        return result


def sync_loop():
    interval = max(5, int(OPTIONS.get("sync_interval_minutes") or 15)) * 60
    next_regular = 0
    while True:
        now = time.time()
        if state["dirty"] or now >= next_regular:
            try:
                do_sync("auto")
            except Exception as e:
                state["last_sync_result"] = f"error: {e}"
                log(f"Sync error: {e}")
            next_regular = now + interval
        time.sleep(10)


# ---------------------------------------------------------------- helpers

def model_by_name(name):
    m = col.models.by_name(name)
    if not m:
        raise Exception(f"note type not found: {name}")
    return m


def deck_id(name, create=True):
    did = col.decks.id_for_name(name)
    if did is None:
        if not create:
            raise Exception(f"deck not found: {name}")
        did = col.decks.id(name)
    return did


def note_info(nid):
    n = col.get_note(nid)
    m = n.note_type()
    cards = n.cards()
    return {
        "noteId": n.id,
        "modelName": m["name"],
        "deckNames": sorted({col.decks.name(c.did) for c in cards}),
        "tags": n.tags,
        "fields": {f["name"]: {"value": n.fields[i], "order": i} for i, f in enumerate(m["flds"])},
        "cards": [c.id for c in cards],
        "mod": n.mod,
    }


def card_info(cid):
    c = col.get_card(cid)
    return {
        "cardId": c.id,
        "note": c.nid,
        "deckName": col.decks.name(c.did),
        "ord": c.ord,
        "type": c.type,
        "queue": c.queue,
        "due": c.due,
        "interval": c.ivl,
        "factor": c.factor,
        "reps": c.reps,
        "lapses": c.lapses,
    }


def build_note(spec):
    m = model_by_name(spec["modelName"])
    n = col.new_note(m)
    for k, v in spec.get("fields", {}).items():
        n[k] = v
    tags = spec.get("tags") or []
    n.tags = tags.split() if isinstance(tags, str) else list(tags)
    opts = spec.get("options") or {}
    if not opts.get("allowDuplicate", False) and n.fields_check() == NoteFieldsCheckResult.DUPLICATE:
        raise Exception("cannot create note because it is a duplicate")
    if n.fields_check() == NoteFieldsCheckResult.EMPTY:
        raise Exception("cannot create note because the first field is empty")
    return n


def tag_list(tags):
    return tags.split() if isinstance(tags, str) else list(tags)


def mark_dirty():
    state["dirty"] = True


# ---------------------------------------------------------------- actions

def a_version(p):
    return 6


def a_status(p):
    return {
        "logged_in": load_auth() is not None,
        "login_error": state["login_error"],
        "last_sync": state["last_sync"],
        "last_sync_result": state["last_sync_result"],
        "pending_changes": state["dirty"],
        "notes": col.note_count(),
        "cards": col.card_count(),
        "decks": {d.name: len(col.find_cards(f'"deck:{d.name}"')) for d in col.decks.all_names_and_ids()},
        "new_cards": len(col.find_cards("is:new")),
        "due_cards": len(col.find_cards("is:due")),
    }


def a_sync(p):
    return do_sync(p.get("mode", "auto"))


def a_deckNames(p):
    return sorted(d.name for d in col.decks.all_names_and_ids())


def a_createDeck(p):
    did = deck_id(p["deck"])
    mark_dirty()
    return did


def a_deleteDecks(p):
    ids = [deck_id(d, create=False) for d in p["decks"]]
    if not p.get("cardsToo", False):
        for did in ids:
            if col.decks.card_count(did, include_subdecks=True):
                raise Exception(f"deck {col.decks.name(did)} is not empty; pass cardsToo=true to delete its cards")
    col.decks.remove(ids)
    mark_dirty()
    return None


def a_modelNames(p):
    return sorted(m.name for m in col.models.all_names_and_ids())


def a_modelFieldNames(p):
    return [f["name"] for f in model_by_name(p["modelName"])["flds"]]


def a_modelTemplates(p):
    m = model_by_name(p["modelName"])
    return {t["name"]: {"Front": t["qfmt"], "Back": t["afmt"]} for t in m["tmpls"]}


def a_modelStyling(p):
    return {"css": model_by_name(p["modelName"])["css"]}


def a_createModel(p):
    mm = col.models
    m = mm.new(p["modelName"])
    if p.get("isCloze"):
        m["type"] = 1
    for f in p["inOrderFields"]:
        mm.add_field(m, mm.new_field(f))
    for t in p["cardTemplates"]:
        tmpl = mm.new_template(t.get("Name", "Card 1"))
        tmpl["qfmt"], tmpl["afmt"] = t["Front"], t["Back"]
        mm.add_template(m, tmpl)
    if p.get("css"):
        m["css"] = p["css"]
    mm.add(m)
    mark_dirty()
    return mm.by_name(p["modelName"])["id"]


def a_updateModelTemplates(p):
    m = model_by_name(p["model"]["name"])
    for t in m["tmpls"]:
        if t["name"] in p["model"]["templates"]:
            new = p["model"]["templates"][t["name"]]
            t["qfmt"] = new.get("Front", t["qfmt"])
            t["afmt"] = new.get("Back", t["afmt"])
    col.models.update_dict(m)
    mark_dirty()
    return None


def a_updateModelStyling(p):
    m = model_by_name(p["model"]["name"])
    m["css"] = p["model"]["css"]
    col.models.update_dict(m)
    mark_dirty()
    return None


def a_findNotes(p):
    return list(col.find_notes(p["query"]))


def a_findCards(p):
    return list(col.find_cards(p["query"]))


def a_notesInfo(p):
    ids = p.get("notes") or list(col.find_notes(p["query"]))
    return [note_info(n) for n in ids]


def a_cardsInfo(p):
    return [card_info(c) for c in p["cards"]]


def a_addNote(p):
    spec = p["note"]
    n = build_note(spec)
    col.add_note(n, deck_id(spec["deckName"]))
    mark_dirty()
    return n.id


def a_addNotes(p):
    out = []
    for spec in p["notes"]:
        try:
            n = build_note(spec)
            col.add_note(n, deck_id(spec["deckName"]))
            out.append(n.id)
        except Exception as e:
            out.append({"error": str(e)})
    mark_dirty()
    return out


def a_updateNoteFields(p):
    n = col.get_note(p["note"]["id"])
    for k, v in p["note"]["fields"].items():
        n[k] = v
    col.update_note(n)
    mark_dirty()
    return None


def a_updateNote(p):
    spec = p["note"]
    n = col.get_note(spec["id"])
    for k, v in (spec.get("fields") or {}).items():
        n[k] = v
    if "tags" in spec:
        n.tags = tag_list(spec["tags"])
    col.update_note(n)
    mark_dirty()
    return None


def a_addTags(p):
    col.tags.bulk_add(p["notes"], " ".join(tag_list(p["tags"])))
    mark_dirty()
    return None


def a_removeTags(p):
    col.tags.bulk_remove(p["notes"], " ".join(tag_list(p["tags"])))
    mark_dirty()
    return None


def a_deleteNotes(p):
    col.remove_notes(p["notes"])
    mark_dirty()
    return None


def a_changeDeck(p):
    col.set_deck(p["cards"], deck_id(p["deck"]))
    mark_dirty()
    return None


def a_suspend(p):
    col.sched.suspend_cards(p["cards"])
    mark_dirty()
    return True


def a_unsuspend(p):
    col.sched.unsuspend_cards(p["cards"])
    mark_dirty()
    return True


CARD_KEYS = {"lapses": int, "reps": int, "factor": int, "ivl": int, "due": int}


def a_setSpecificValueOfCard(p):
    """params: card, keys [..], newValues [..]. Allowed keys: lapses, reps, factor, ivl, due."""
    c = col.get_card(int(p["card"]))
    keys, vals = p["keys"], p["newValues"]
    if len(keys) != len(vals):
        raise Exception("keys and newValues must be the same length")
    for k in keys:
        if k not in CARD_KEYS:
            raise Exception(f"unsupported card key: {k}")
    for k, v in zip(keys, vals):
        setattr(c, k, CARD_KEYS[k](v))
    col.update_card(c)
    mark_dirty()
    return [True] * len(keys)


def a_forgetCards(p):
    """params: cards, restorePosition (default true), resetCounts (default true)."""
    col.sched.schedule_cards_as_new(
        p["cards"],
        restore_position=bool(p.get("restorePosition", True)),
        reset_counts=bool(p.get("resetCounts", True)),
    )
    mark_dirty()
    return None


def a_changeNoteType(p):
    """params: notes, newModelName, fieldMap {newField: oldField or null}"""
    nids = p["notes"]
    old = col.get_note(nids[0]).note_type()
    new = model_by_name(p["newModelName"])
    old_names = [f["name"] for f in old["flds"]]
    info = col.models.change_notetype_info(old_notetype_id=old["id"], new_notetype_id=new["id"])
    req = info.input
    req.note_ids.extend(nids)
    fmap = p.get("fieldMap") or {}
    req.new_fields[:] = [
        old_names.index(fmap[f["name"]]) if fmap.get(f["name"]) in old_names else -1
        for f in new["flds"]
    ]
    col.models.change_notetype_of_notes(req)
    mark_dirty()
    return None


def a_getDeckLimits(p):
    c = col.decks.config_dict_for_deck_id(deck_id(p["deck"], create=False))
    return {"preset": c["name"], "newPerDay": c["new"]["perDay"], "reviewsPerDay": c["rev"]["perDay"]}


def a_setDeckLimits(p):
    c = col.decks.config_dict_for_deck_id(deck_id(p["deck"], create=False))
    if "newPerDay" in p:
        c["new"]["perDay"] = int(p["newPerDay"])
    if "reviewsPerDay" in p:
        c["rev"]["perDay"] = int(p["reviewsPerDay"])
    col.decks.update_config(c)
    mark_dirty()
    return {"preset": c["name"], "newPerDay": c["new"]["perDay"], "reviewsPerDay": c["rev"]["perDay"]}


ACTIONS = {name[2:]: fn for name, fn in globals().items() if name.startswith("a_")}


def run_action(action, params):
    if action == "multi":
        return [run_action(a["action"], a.get("params") or {}) for a in params["actions"]]
    fn = ACTIONS.get(action)
    if not fn:
        raise Exception(f"unsupported action: {action}")
    with lock:
        return fn(params or {})


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        if self.client_address[0] in ALLOWED_CLIENTS:
            return True
        self._send(403, json.dumps({"result": None, "error": "forbidden"}))
        return False

    def do_GET(self):
        if not self._allowed():
            return
        with lock:
            s = a_status({})
        rows = "".join(
            f"<tr><td>{html.escape(str(k))}</td><td>{html.escape(json.dumps(v) if isinstance(v, dict) else str(v))}</td></tr>"
            for k, v in s.items()
        )
        page = (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            "<title>Anki Sync</title><style>body{font-family:system-ui;margin:16px}"
            "td{padding:6px 10px;border-bottom:1px solid #ddd;vertical-align:top}</style>"
            f"<h2>Anki Sync</h2><table>{rows}</table>"
        )
        self._send(200, page, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._allowed():
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length) or b"{}")
            result = run_action(req.get("action"), req.get("params") or {})
            self._send(200, json.dumps({"result": result, "error": None}))
        except Exception as e:
            log(traceback.format_exc())
            self._send(200, json.dumps({"result": None, "error": str(e)}))

    def log_message(self, fmt, *args):
        pass


def main():
    log(f"Anki Sync starting; collection has {col.card_count()} cards")
    threading.Thread(target=sync_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
