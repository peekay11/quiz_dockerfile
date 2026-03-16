# type: ignore
from js import document, window
from pyodide.http import pyfetch
from pyodide.ffi import create_proxy
import random, string, asyncio, json

URL = "https://kqgywbdgoihbnhosnhjf.supabase.co"
KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtxZ3l3YmRnb2loYm5ob3NuaGpmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM2NDkzOTEsImV4cCI6MjA4OTIyNTM5MX0.5F6mxm6NRGJEeZjfyOkj9BEQPhywGrJZd44a14QHW0E"
HDRS = {
    "apikey":        KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

REVEAL_SECS   = 3    # seconds to show answer before next question
QUESTION_SECS = 20   # seconds per question (server-anchored deadline)

# ── MODULE STATE ──────────────────────────────────────────
current_code             = ""
my_id                    = None
my_team                  = None
my_name                  = "Connecting..."
is_host                  = False
max_players              = 4
questions_data           = []
_timer_task              = None
_polling                 = True
_has_answered_this_round = False

# ══════════════════════════════════════════════════════════
# REST HELPERS — every call surfaces real Supabase errors
# ══════════════════════════════════════════════════════════

async def sb_get(table, filters=None):
    qs  = "&".join(f"{k}=eq.{v}" for k, v in (filters or {}).items())
    url = f"{URL}/rest/v1/{table}?{qs}" if qs else f"{URL}/rest/v1/{table}"
    r   = await pyfetch(url, method="GET", headers=HDRS)
    if r.status >= 400:
        await _raise_db_error(table, r)
    return await r.json()

async def sb_post(table, data):
    r = await pyfetch(f"{URL}/rest/v1/{table}", method="POST",
                      headers=HDRS, body=json.dumps(data))
    if r.status >= 400:
        await _raise_db_error(table, r)
    rows = await r.json()
    return rows[0] if rows else None

async def sb_patch(table, data, filters):
    qs = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    r  = await pyfetch(f"{URL}/rest/v1/{table}?{qs}", method="PATCH",
                       headers=HDRS, body=json.dumps(data))
    if r.status >= 400:
        await _raise_db_error(table, r)

async def sb_delete(table, filters):
    qs = "&".join(f"{k}=eq.{v}" for k, v in filters.items())
    r  = await pyfetch(f"{URL}/rest/v1/{table}?{qs}", method="DELETE",
                       headers=HDRS)
    if r.status >= 400:
        await _raise_db_error(table, r)

async def sb_rpc(fn, params):
    r = await pyfetch(f"{URL}/rest/v1/rpc/{fn}", method="POST",
                      headers=HDRS, body=json.dumps(params))
    if r.status >= 400:
        await _raise_db_error(f"rpc/{fn}", r)
    return await r.json()

async def _raise_db_error(label, r):
    try:
        err    = await r.json()
        detail = (err.get("message") or err.get("details")
                  or err.get("hint") or str(err))
    except Exception:
        detail = f"HTTP {r.status}"
    raise Exception(f"[{label}] {detail}")

# ══════════════════════════════════════════════════════════
# SAFE NAME
# ══════════════════════════════════════════════════════════

def safe_name(raw):
    if raw is None: return "Connecting..."
    s = str(raw).strip()
    return s if s else "Connecting..."

# ══════════════════════════════════════════════════════════
# REALTIME SUBSCRIPTIONS
# ══════════════════════════════════════════════════════════

def listen_to_room(code):
    from js import window as w, Object
    sb_js = w.supabase.createClient(URL, KEY)
    conf_room = Object.fromEntries([
        ["event", "UPDATE"], ["schema", "public"],
        ["table", "rooms"],  ["filter", f"code=eq.{code}"],
    ])
    conf_players = Object.fromEntries([
        ["event", "*"],      ["schema", "public"],
        ["table", "players"],["filter", f"room_code=eq.{code}"],
    ])
    sb_js.channel(f"room-{code}") \
         .on("postgres_changes", conf_room,    lambda p: handle_room_update(p)) \
         .on("postgres_changes", conf_players, lambda p: handle_player_update(p)) \
         .subscribe()

# ══════════════════════════════════════════════════════════
# LOBBY POLL  (3-second heartbeat while waiting)
# ══════════════════════════════════════════════════════════

async def poll_lobby(code):
    global _polling
    _polling = True
    while _polling:
        await asyncio.sleep(3)
        try:
            players = await sb_get("players", {"room_code": code})
            pl = [_to_dict(p) for p in players]
            window.renderWaiting(pl, max_players)
            _update_ready_button(pl)
        except Exception as e:
            print(f"Lobby poll error: {e}")

def _to_dict(p):
    return {
        "id":       p["id"],
        "name":     safe_name(p.get("name")),
        "team":     p.get("team"),
        "score":    int(p.get("score") or 0),
        "is_ready": bool(p.get("is_ready", False)),
        "is_host":  bool(p.get("is_host",  False)),
        "correct_answers": int(p.get("correct_answers") or 0),
    }

def _update_ready_button(players):
    btn = document.getElementById("btn-start")
    if not btn:
        return
    if not players:
        btn.disabled = True
        return
    ready_count = sum(1 for p in players if p.get("is_ready", False))
    all_ready   = ready_count == len(players)
    btn.disabled  = not all_ready
    btn.innerText = ("START GAME →" if all_ready
                     else f"WAITING FOR READY ({ready_count}/{len(players)})")

# ══════════════════════════════════════════════════════════
# QUESTIONS — Open Trivia DB with local fallback
# ══════════════════════════════════════════════════════════

async def load_questions():
    global questions_data
    OPENTDB = "https://opentdb.com/api.php?amount=10&type=multiple&encode=url3986"
    try:
        r = await pyfetch(OPENTDB)
        if r.status == 200:
            raw = await r.json()
            if raw.get("response_code") == 0:
                questions_data = _parse_opentdb(raw["results"])
                return
    except Exception as e:
        print(f"OpenTDB unavailable ({e}), using local file")
    try:
        r = await pyfetch(f"questions.json?v={random.randint(1,9999)}")
        if r.status == 200:
            questions_data = await r.json()
    except Exception as e:
        window.showDebugError(f"Questions load error: {e}")

def _parse_opentdb(results):
    import urllib.parse
    parsed = []
    for item in results:
        q_text  = urllib.parse.unquote(item["question"])
        correct = urllib.parse.unquote(item["correct_answer"])
        wrongs  = [urllib.parse.unquote(w) for w in item["incorrect_answers"]]
        options = wrongs + [correct]
        random.shuffle(options)
        parsed.append({
            "question": q_text,
            "options":  options,
            "answer":   options.index(correct),
        })
    return parsed

# ══════════════════════════════════════════════════════════
# HOST
# ══════════════════════════════════════════════════════════

async def host_game(event):
    global current_code, my_id, my_team, max_players, is_host, my_name
    raw_name = document.getElementById("player-name").value.strip()
    if not raw_name:
        window.showError("Please enter your name")
        return
    my_name      = raw_name
    max_players  = int(window.getPlayerCount())
    is_private   = bool(window.getIsPrivate())
    current_code = ''.join(random.choices(string.ascii_uppercase, k=4))
    is_host      = True
    try:
        await sb_post("rooms", {
            "code":                current_code,
            "max_players":         max_players,
            "is_started":          False,
            "is_private":          is_private,
            "is_closed":           False,
            "current_q_index":     0,
            "active_player_id":    None,
            "host_id":             None,
            "question_locked_by":  None,
            "question_answered":   False,
            "answers_this_round":  0,
            "question_deadline":   None,
        })
        p = await sb_post("players", {
            "room_code":       current_code,
            "name":            my_name,
            "score":           0,
            "team":            None,
            "is_ready":        False,
            "is_host":         True,
            "is_kicked":       False,
            "correct_answers": 0,
        })
        if p is None:
            raise Exception("Player insert returned no data")
        my_id = p["id"]
        await sb_patch("rooms", {"host_id": my_id}, {"code": current_code})
        window.showWaitingRoom(current_code, True, max_players)
        listen_to_room(current_code)
        asyncio.ensure_future(poll_lobby(current_code))
    except Exception as e:
        window.showDebugError(f"Host Error: {e}")

# ══════════════════════════════════════════════════════════
# JOIN
# ══════════════════════════════════════════════════════════

async def join_game(event):
    global current_code, my_id, my_team, max_players, is_host, my_name
    raw_name = document.getElementById("player-name").value.strip()
    code     = document.getElementById("join-code").value.strip().upper()
    if not raw_name:
        window.showError("Please enter your name")
        return
    if len(code) != 4:
        window.showError("Enter a valid 4-letter code")
        return
    my_name = raw_name
    is_host = False
    try:
        rooms = await sb_get("rooms", {"code": code})
        if not rooms:
            window.showError("Room not found. Check the code.")
            return
        room = rooms[0]
        if room.get("is_started"):
            window.showError("Game already in progress.")
            return
        if room.get("is_closed"):
            window.showError("This room is closed.")
            return
        existing = await sb_get("players", {"room_code": code})
        cap      = int(room.get("max_players", 4))
        if len(existing) >= cap:
            window.showError(f"Room is full ({cap}/{cap}).")
            return
        max_players  = cap
        current_code = code
        p = await sb_post("players", {
            "room_code":       current_code,
            "name":            my_name,
            "score":           0,
            "team":            None,
            "is_ready":        False,
            "is_host":         False,
            "is_kicked":       False,
            "correct_answers": 0,
        })
        if p is None:
            raise Exception("Player insert returned no data")
        my_id = p["id"]
        window.showWaitingRoom(current_code, False, max_players)
        listen_to_room(current_code)
        asyncio.ensure_future(poll_lobby(current_code))
    except Exception as e:
        window.showDebugError(f"Join Error: {e}")

# ══════════════════════════════════════════════════════════
# QUICK JOIN
# ══════════════════════════════════════════════════════════

async def quick_join(event):
    global current_code, my_id, my_team, max_players, is_host, my_name
    raw_name = document.getElementById("player-name").value.strip()
    if not raw_name:
        window.showError("Please enter your name")
        return
    my_name = raw_name
    try:
        rooms = await sb_get("rooms", {
            "is_private":  False,
            "is_started":  False,
            "is_closed":   False,
        })
        target = None
        for r in rooms:
            existing = await sb_get("players", {"room_code": r["code"]})
            if len(existing) < int(r.get("max_players", 4)):
                target = r
                break
        if target is None:
            window.showError("No open rooms found. Host one!")
            return
        document.getElementById("join-code").value = target["code"]
        await join_game(None)
    except Exception as e:
        window.showDebugError(f"Quick Join Error: {e}")

# ══════════════════════════════════════════════════════════
# RECONNECT — Priority 2 fix
# Called on page load if ?rejoin=PLAYER_ID is in the URL
# ══════════════════════════════════════════════════════════

async def try_reconnect():
    """
    If the player refreshed mid-game, restore their state from the DB
    using the get_player_state RPC and rejoin seamlessly.
    """
    global current_code, my_id, my_team, my_name, is_host, max_players
    try:
        stored_id = window.getStoredPlayerId()   # read from localStorage via JS
        if not stored_id:
            return
        result = await sb_rpc("get_player_state", {"p_player_id": int(stored_id)})
        if not result:
            window.clearStoredPlayerId()
            return
        state = result[0] if isinstance(result, list) else result

        # Room is closed or game over — don't rejoin
        if state.get("room_is_closed"):
            window.clearStoredPlayerId()
            return

        # Restore local state
        my_id        = int(state["player_id"])
        my_name      = safe_name(state.get("player_name"))
        my_team      = state.get("player_team")
        is_host      = bool(state.get("player_is_host", False))
        current_code = state["room_code"]
        max_players  = 4   # default; poll will correct it

        window.showToast(f"Welcome back, {my_name}!")
        listen_to_room(current_code)

        if state.get("room_is_started"):
            # Jump straight into the game — build a synthetic room_data object
            class ReconnectRoom:
                pass
            rd = ReconnectRoom()
            rd.current_q_index   = state["room_q_index"]
            rd.is_started        = True
            rd.is_closed         = False
            rd.question_locked_by = state.get("room_locked_by")
            rd.question_answered  = bool(state.get("room_answered", False))
            for s in document.querySelectorAll(".screen"):
                s.classList.remove("active")
            document.getElementById("screen-game").classList.add("active")
            await sync_game(rd)
        else:
            window.showWaitingRoom(current_code, is_host, max_players)
            asyncio.ensure_future(poll_lobby(current_code))

    except Exception as e:
        print(f"Reconnect failed (not mid-game): {e}")
        window.clearStoredPlayerId()

# ══════════════════════════════════════════════════════════
# READY TOGGLE
# ══════════════════════════════════════════════════════════

async def toggle_ready(event):
    if my_id is None:
        return
    try:
        players  = await sb_get("players", {"room_code": current_code})
        me       = next((p for p in players if int(p["id"]) == int(my_id)), None)
        if me is None:
            return
        new_ready = not bool(me.get("is_ready", False))
        await sb_patch("players", {"is_ready": new_ready}, {"id": my_id})
        btn = document.getElementById("btn-ready")
        if btn:
            btn.innerText = "✓ READY" if new_ready else "READY UP"
            btn.className = "btn btn-lime" if new_ready else "btn btn-ghost"
    except Exception as e:
        window.showDebugError(f"Ready Error: {e}")

# ══════════════════════════════════════════════════════════
# KICK (host only)
# ══════════════════════════════════════════════════════════

async def kick_player(player_id):
    if not is_host:
        return
    try:
        await sb_patch("players", {"is_kicked": True}, {"id": player_id})
    except Exception as e:
        window.showDebugError(f"Kick Error: {e}")

# ══════════════════════════════════════════════════════════
# LEAVE ROOM
# ══════════════════════════════════════════════════════════

async def leave_room(event):
    global current_code, my_id, is_host, _polling
    _polling = False
    window.clearStoredPlayerId()
    if my_id is None:
        window.goTo("screen-landing")
        return
    try:
        if is_host:
            players = await sb_get("players", {"room_code": current_code})
            others  = [p for p in players if int(p["id"]) != int(my_id)]
            if others:
                new_host = others[0]
                await sb_patch("players", {"is_host": True},
                               {"id": new_host["id"]})
                await sb_patch("rooms", {"host_id": new_host["id"]},
                               {"code": current_code})
                window.showToast(
                    f"{safe_name(new_host.get('name'))} is the new host")
            else:
                await sb_patch("rooms", {"is_closed": True},
                               {"code": current_code})
        await sb_delete("players", {"id": my_id})
        current_code = ""
        my_id        = None
        is_host      = False
        window.goTo("screen-landing")
    except Exception as e:
        window.showDebugError(f"Leave Error: {e}")

# ══════════════════════════════════════════════════════════
# START GAME
# ══════════════════════════════════════════════════════════

async def start_game(event):
    try:
        players = await sb_get("players", {"room_code": current_code})
        if len(players) < 2:
            window.showError("Need at least 2 players to start")
            return
        not_ready = [p for p in players if not p.get("is_ready", False)]
        if not_ready:
            names = ", ".join(safe_name(p.get("name")) for p in not_ready)
            window.showError(f"Not ready: {names}")
            return
        random.shuffle(players)
        for i, p in enumerate(players):
            await sb_patch("players",
                           {"team": 1 if i % 2 == 0 else 2},
                           {"id": p["id"]})
        await sb_patch("rooms", {
            "is_started":          True,
            "current_q_index":     0,
            "question_locked_by":  None,
            "question_answered":   False,
            "answers_this_round":  0,
            "game_started_at":     "now()",
        }, {"code": current_code})
    except Exception as e:
        window.showDebugError(f"Start Error: {e}")

# ══════════════════════════════════════════════════════════
# SUBMIT ANSWER  — atomic race via DB RPC (Priority 1 fix)
# ══════════════════════════════════════════════════════════

async def submit_answer(selected_idx):
    global _timer_task, _has_answered_this_round
    if _has_answered_this_round:
        return
    _has_answered_this_round = True

    if _timer_task and not _timer_task.done():
        _timer_task.cancel()
        _timer_task = None

    try:
        rooms   = await sb_get("rooms", {"code": current_code})
        room    = rooms[0]
        q_idx   = int(room["current_q_index"])
        correct = int(questions_data[q_idx]["answer"])

        # Flash correct/wrong on buttons immediately
        opts = document.getElementById("options-list") \
                       .querySelectorAll(".opt-btn")
        for i in range(opts.length):
            opts[i].disabled = True
            if i == correct:
                opts[i].classList.add("correct")
            elif i == selected_idx:
                opts[i].classList.add("wrong")

        if selected_idx != correct:
            # Wrong — lock locally, others can still answer
            return

        # ── ATOMIC LOCK via Postgres function ─────────────────────────
        # claim_question_lock returns TRUE only for the FIRST correct answer.
        # Even if two players answer within 1ms, only one row gets updated.
        won = await sb_rpc("claim_question_lock", {
            "p_room_code": current_code,
            "p_player_id": int(my_id),
            "p_q_index":   q_idx,
        })

        if not won:
            window.showToast("⚡ Too slow — someone got there first!")
            return

        # ── We won — score is updated inside increment_score RPC ──────
        await sb_rpc("increment_score", {"player_id": int(my_id)})
        window.showToast(f"✅ {safe_name(my_name)} got it first! +1 point")

        # After reveal delay, advance to next question
        await asyncio.sleep(REVEAL_SECS)
        await _advance_question(q_idx)

    except Exception as e:
        print(f"Submit Error: {e}")
        window.showDebugError(f"Submit Error: {e}")

async def _advance_question(q_idx):
    """Reset per-round state and move to next question."""
    try:
        await sb_patch("rooms", {
            "current_q_index":    q_idx + 1,
            "question_locked_by": None,
            "question_answered":  False,
            "answers_this_round": 0,
            "question_deadline":  None,
        }, {"code": current_code})
    except Exception as e:
        print(f"Advance Error: {e}")

# ══════════════════════════════════════════════════════════
# TIMER  — Priority 2 fix: deadline anchored to DB timestamp
# All clients read question_deadline from the room row,
# so everyone's countdown is identical regardless of when
# they loaded the page.
# ══════════════════════════════════════════════════════════

async def run_timer(deadline_iso=None):
    """
    Count down to deadline_iso (ISO string from DB).
    If deadline_iso is None, falls back to a local QUESTION_SECS timer.
    On expiry the host advances — everyone else just lets it expire.
    """
    global _timer_task
    arc    = document.getElementById("timer-arc")
    sec_el = document.getElementById("timer-sec")
    full   = 125.6

    import js as _js
    if deadline_iso:
        deadline_ms = _js.Date.parse(deadline_iso)
    else:
        deadline_ms = _js.Date.now() + QUESTION_SECS * 1000

    while True:
        now_ms     = _js.Date.now()
        remaining  = max(0, (deadline_ms - now_ms) / 1000)
        secs_left  = int(remaining)
        sec_el.innerText = str(secs_left)
        frac = remaining / QUESTION_SECS
        arc.setAttribute("stroke-dashoffset", str(full * (1 - frac)))

        if remaining <= 0:
            # Time's up — only host advances to prevent duplicate writes
            if is_host:
                try:
                    rooms = await sb_get("rooms", {"code": current_code})
                    room  = rooms[0]
                    q_idx = int(room["current_q_index"])
                    if not room.get("question_answered"):
                        window.showToast("⏰ Time's up! No point awarded.")
                        await _advance_question(q_idx)
                except Exception as e:
                    print(f"Timer advance error: {e}")
            return
        await asyncio.sleep(0.5)   # update every 500ms for smooth countdown

# ══════════════════════════════════════════════════════════
# SYNC GAME UI  — called by realtime on every room UPDATE
# ══════════════════════════════════════════════════════════

async def sync_game(room_data):
    global _timer_task, my_team, _polling, _has_answered_this_round
    _polling = False

    # Save player id to localStorage so reconnect works on refresh
    if my_id is not None:
        window.storePlayerId(my_id)

    players  = await sb_get("players", {"room_code": current_code})
    pl_dicts = [_to_dict(p) for p in players]

    if my_team is None and my_id is not None:
        me = next((p for p in pl_dicts if p["id"] == int(my_id)), None)
        if me:
            my_team = me["team"]

    t1 = sum(p["score"] for p in pl_dicts if str(p.get("team","")) == "1")
    t2 = sum(p["score"] for p in pl_dicts if str(p.get("team","")) == "2")
    document.getElementById("score-t1").innerText = str(t1)
    document.getElementById("score-t2").innerText = str(t2)
    window.renderScoreboardPlayers(pl_dicts)

    idx        = int(room_data.current_q_index)
    q_answered = bool(getattr(room_data, "question_answered", False))
    locked_by  = getattr(room_data, "question_locked_by", None)
    deadline   = getattr(room_data, "question_deadline",  None)

    document.getElementById("q-counter").innerText = f"Q {idx + 1}"

    # Reset per-round client flag on every new question
    if not q_answered:
        _has_answered_this_round = False

    # Banner: show winner name or "EVERYONE ANSWER NOW"
    winner_p = next(
        (p for p in pl_dicts if locked_by and p["id"] == int(locked_by)),
        None)
    banner = document.getElementById("turn-banner")
    if q_answered and winner_p:
        wteam = str(winner_p.get("team",""))
        banner.className = ("turn-banner volt-turn" if wteam == "1"
                            else "turn-banner lime-turn")
        banner.innerHTML = f"<span>⚡ {winner_p['name']} got it first!</span>"
    else:
        banner.className = "turn-banner other-turn"
        banner.innerHTML = "<span>🏁 EVERYONE ANSWER NOW</span>"

    # Not-your-turn lock is gone — everyone answers simultaneously
    document.getElementById("input-lock").style.display  = "none"
    document.getElementById("active-player-display").innerText = "ALL PLAYERS"

    if idx < len(questions_data):
        q       = questions_data[idx]
        correct = int(q["answer"])
        document.getElementById("q-text").innerText = q["question"]
        grid = document.getElementById("options-list")
        grid.innerHTML = ""
        for i, opt in enumerate(q["options"]):
            btn           = document.createElement("button")
            btn.className = "opt-btn"
            btn.innerText = opt
            if q_answered:
                btn.disabled = True
                if i == correct:
                    btn.classList.add("correct")
            else:
                btn.disabled = False
            def make_cb(val):
                return create_proxy(
                    lambda e: asyncio.ensure_future(submit_answer(val)))
            btn.onclick = make_cb(i)
            grid.appendChild(btn)

        # Timer management
        if _timer_task and not _timer_task.done():
            _timer_task.cancel()

        if q_answered:
            # Question done — freeze timer display
            document.getElementById("timer-sec").innerText = "✓"
            document.getElementById("timer-arc") \
                    .setAttribute("stroke-dashoffset", "125.6")
        else:
            # Fresh question — host writes the deadline, everyone reads it
            if is_host and not deadline:
                import js as _js
                dl_ms  = _js.Date.now() + QUESTION_SECS * 1000
                dl_iso = _js.Date.new(dl_ms).toISOString()
                try:
                    await sb_patch("rooms",
                                   {"question_deadline": dl_iso},
                                   {"code": current_code})
                    deadline = dl_iso
                except Exception:
                    pass
            _timer_task = asyncio.ensure_future(run_timer(deadline))
    else:
        # All questions done → results
        if _timer_task and not _timer_task.done():
            _timer_task.cancel()
        show_results(t1, t2, pl_dicts)

# ══════════════════════════════════════════════════════════
# WINNER CALCULATION
# ══════════════════════════════════════════════════════════

def _calc_winner(players):
    ca1 = sum(int(p.get("correct_answers") or p.get("score") or 0)
              for p in players if str(p.get("team","")) == "1")
    ca2 = sum(int(p.get("correct_answers") or p.get("score") or 0)
              for p in players if str(p.get("team","")) == "2")
    if ca1 > ca2: return "VOLT", ca1, ca2
    if ca2 > ca1: return "LIME", ca1, ca2
    return "DRAW", ca1, ca2

def show_results(t1, t2, players):
    window.clearStoredPlayerId()
    for s in document.querySelectorAll(".screen"):
        s.classList.remove("active")
    document.getElementById("screen-results").classList.add("active")
    document.getElementById("final-t1").innerText = str(t1)
    document.getElementById("final-t2").innerText = str(t2)
    winner, _, _ = _calc_winner(players)
    if winner == "VOLT":
        document.getElementById("winner-team-label").innerText   = "⚡ VOLT"
        document.getElementById("winner-team-label").style.color = "var(--volt)"
        document.getElementById("winner-title").style.color      = "var(--volt)"
    elif winner == "LIME":
        document.getElementById("winner-team-label").innerText   = "🟢 LIME"
        document.getElementById("winner-team-label").style.color = "var(--lime)"
        document.getElementById("winner-title").style.color      = "var(--lime)"
    else:
        document.getElementById("winner-trophy").innerText       = "🤝"
        document.getElementById("winner-team-label").innerText   = "IT'S A"
        document.getElementById("winner-title").innerText        = "DRAW!"
        document.getElementById("winner-title").style.color      = "var(--muted)"
    window.renderBreakdown(players)

# ══════════════════════════════════════════════════════════
# REALTIME HANDLERS
# ══════════════════════════════════════════════════════════

def handle_room_update(payload):
    new = payload.new
    if getattr(new, "is_closed", False):
        window.clearStoredPlayerId()
        window.showToast("Host closed the room")
        window.goTo("screen-landing")
        return
    if not new.is_started:
        return
    for s in document.querySelectorAll(".screen"):
        s.classList.remove("active")
    document.getElementById("screen-game").classList.add("active")
    asyncio.ensure_future(sync_game(new))

def handle_player_update(payload):
    new = payload.new
    if my_id is not None and int(getattr(new, "id", -1)) == int(my_id):
        if getattr(new, "is_kicked", False):
            window.clearStoredPlayerId()
            window.showToast("You were removed from the room")
            window.goTo("screen-landing")
            return
    asyncio.ensure_future(_refresh_lobby())

async def _refresh_lobby():
    try:
        players = await sb_get("players", {"room_code": current_code})
        pl = [_to_dict(p) for p in players]
        window.renderWaiting(pl, max_players)
        _update_ready_button(pl)
    except Exception:
        pass

# ══════════════════════════════════════════════════════════
# INIT
# ══════════════════════════════════════════════════════════

async def handle_proceed(event):
    flow = window.getFlow()
    if flow == "host":
        await host_game(event)
    else:
        await join_game(event)

async def init():
    try:
        await load_questions()
        window.addEventListener("proceedFlow",
            create_proxy(lambda e: asyncio.ensure_future(handle_proceed(e))))
        window.addEventListener("startGame",
            create_proxy(lambda e: asyncio.ensure_future(start_game(e))))
        window.addEventListener("toggleReady",
            create_proxy(lambda e: asyncio.ensure_future(toggle_ready(e))))
        window.addEventListener("leaveRoom",
            create_proxy(lambda e: asyncio.ensure_future(leave_room(e))))
        window.addEventListener("quickJoin",
            create_proxy(lambda e: asyncio.ensure_future(quick_join(e))))
        # Attempt reconnect if player refreshed mid-game
        await try_reconnect()
    except Exception as e:
        window.showDebugError(str(e))

asyncio.ensure_future(init())