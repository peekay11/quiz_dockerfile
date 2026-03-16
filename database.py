# type: ignore
from js import document, window
from pyodide.http import pyfetch
from pyodide.ffi import create_proxy
import random, string, asyncio, json

# ── CONFIGURATION ──────────────────────────────────────────
URL = "https://kqgywbdgoihbnhosnhjf.supabase.co"
KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtxZ3l3YmRnb2loYm5ob3NuaGpmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM2NDkzOTEsImV4cCI6MjA4OTIyNTM5MX0.5F6mxm6NRGJEeZjfyOkj9BEQPhywGrJZd44a14QHW0E"
HDRS = {
    "apikey":        KEY,
    "Authorization": f"Bearer {KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

REVEAL_SECS   = 3    
QUESTION_SECS = 20   

# ── MODULE STATE ──────────────────────────────────────────
current_code             = ""
my_id                    = None
my_team                  = None
my_name                  = "Connecting..."
is_host                  = False
max_players              = 4
questions_data           = []
_timer_task              = None
_has_answered_this_round = False
_active_proxies          = [] 

# ══════════════════════════════════════════════════════════
# DATABASE HELPERS
# ══════════════════════════════════════════════════════════

async def sb_request(table, method="GET", body=None, filters=None, rpc=False):
    """Unified helper to handle all Supabase traffic."""
    qs = "&".join(f"{k}=eq.{v}" for k, v in (filters or {}).items())
    base = f"{URL}/rest/v1/{'rpc/' if rpc else ''}{table}"
    url = f"{base}?{qs}" if qs else base
    
    try:
        r = await pyfetch(url, method=method, headers=HDRS, body=json.dumps(body) if body else None)
        if r.status == 429:
            window.showToast("Rate limited! Slowing down...")
            return None
        return await r.json()
    except Exception as e:
        print(f"DB Error: {e}")
        return None

def _to_dict(p):
    return {
        "id": p["id"],
        "name": str(p.get("name") or "Connecting...").strip(),
        "team": p.get("team"),
        "score": int(p.get("score") or 0),
        "is_ready": bool(p.get("is_ready", False)),
        "is_host": bool(p.get("is_host", False)),
        "correct_answers": int(p.get("correct_answers") or 0),
    }

# ══════════════════════════════════════════════════════════
# REALTIME (The Core Fix)
# ══════════════════════════════════════════════════════════

def listen_to_room(code):
    global _active_proxies
    from js import window as w, Object
    
    # Destroy existing proxies to prevent memory leaks and TypeErrors
    for p in _active_proxies:
        try: p.destroy()
        except: pass
    _active_proxies = []

    sb_js = w.supabase.createClient(URL, KEY)
    
    # FIX: Use *args to accept (payload, context) from Supabase JS
    room_proxy = create_proxy(lambda *args: handle_room_update(args[0]))
    player_proxy = create_proxy(lambda *args: handle_player_update(args[0]))
    _active_proxies.extend([room_proxy, player_proxy])

    conf_room = Object.fromEntries([
        ["event", "UPDATE"], ["schema", "public"],
        ["table", "rooms"],  ["filter", f"code=eq.{code}"],
    ])
    conf_players = Object.fromEntries([
        ["event", "*"],      ["schema", "public"],
        ["table", "players"],["filter", f"room_code=eq.{code}"],
    ])
    
    sb_js.channel(f"room-{code}") \
         .on("postgres_changes", conf_room, room_proxy) \
         .on("postgres_changes", conf_players, player_proxy) \
         .subscribe()

# ══════════════════════════════════════════════════════════
# ROOM ACTIONS
# ══════════════════════════════════════════════════════════

async def host_game(event):
    global current_code, my_id, max_players, is_host, my_name
    name = document.getElementById("player-name").value.strip()
    if not name: return window.showError("Enter your name")
    
    my_name, is_host = name, True
    max_players = int(window.getPlayerCount())
    current_code = ''.join(random.choices(string.ascii_uppercase, k=4))
    
    res = await sb_request("rooms", "POST", {
        "code": current_code, "max_players": max_players,
        "is_started": False, "is_private": bool(window.getIsPrivate()),
        "current_q_index": 0
    })
    if res:
        p = await sb_request("players", "POST", {"room_code": current_code, "name": my_name, "is_host": True})
        my_id = p[0]["id"]
        window.storePlayerId(my_id)
        window.showWaitingRoom(current_code, True, max_players)
        listen_to_room(current_code)
        await _refresh_lobby()

async def join_game(event):
    global current_code, my_id, max_players, is_host, my_name
    name = document.getElementById("player-name").value.strip()
    code = document.getElementById("join-code").value.strip().upper()
    if not name or len(code) != 4: return window.showError("Check name and code")
    
    rooms = await sb_request("rooms", "GET", filters={"code": code})
    if not rooms or rooms[0].get("is_started"): return window.showError("Room unavailable")
    
    my_name, current_code, is_host = name, code, False
    max_players = int(rooms[0].get("max_players", 4))
    p = await sb_request("players", "POST", {"room_code": code, "name": name})
    if p:
        my_id = p[0]["id"]
        window.storePlayerId(my_id)
        window.showWaitingRoom(code, False, max_players)
        listen_to_room(code)
        await _refresh_lobby()

async def quick_join(event):
    name = document.getElementById("player-name").value.strip()
    if not name: return window.showError("Enter your name first")
    res = await sb_request("join_public_room", "POST", {"p_player_name": name}, rpc=True)
    if res:
        data = res[0]
        window.storePlayerId(data["new_player_id"])
        window.showWaitingRoom(data["assigned_room_code"], False, 4)
        listen_to_room(data["assigned_room_code"])
        await _refresh_lobby()
    else: window.showToast("No public rooms found")

# ══════════════════════════════════════════════════════════
# GAME LOGIC
# ══════════════════════════════════════════════════════════

async def submit_answer(idx):
    global _has_answered_this_round
    if _has_answered_this_round: return
    _has_answered_this_round = True
    
    room = (await sb_request("rooms", "GET", filters={"code": current_code}))[0]
    q_idx = int(room["current_q_index"])
    correct = int(questions_data[q_idx]["answer"])
    
    opts = document.getElementById("options-list").querySelectorAll(".opt-btn")
    for i, btn in enumerate(opts):
        btn.disabled = True
        if i == correct: btn.classList.add("correct")
        elif i == idx: btn.classList.add("wrong")
            
    if idx == correct:
        won = await sb_request("claim_question_lock", "POST", {"p_room_code": current_code, "p_player_id": int(my_id), "p_q_index": q_idx}, rpc=True)
        if won:
            await sb_request("increment_score", "POST", {"player_id": int(my_id)}, rpc=True)
            await asyncio.sleep(REVEAL_SECS)
            await sb_request(f"rooms?code=eq.{current_code}", "PATCH", {"current_q_index": q_idx + 1, "question_locked_by": None, "question_answered": False, "question_deadline": None})

async def run_timer(deadline_iso):
    import js as _js
    end = _js.Date.parse(deadline_iso) if deadline_iso else _js.Date.now() + 20000
    arc, sec_el = document.getElementById("timer-arc"), document.getElementById("timer-sec")
    while True:
        rem = max(0, (end - _js.Date.now()) / 1000)
        sec_el.innerText = str(int(rem))
        arc.setAttribute("stroke-dashoffset", str(125.6 * (1 - rem/20)))
        if rem <= 0:
            if is_host: await sb_request(f"rooms?code=eq.{current_code}", "PATCH", {"question_answered": True})
            break
        await asyncio.sleep(0.5)

async def sync_game(rd):
    global _has_answered_this_round, _timer_task
    players = await sb_request("players", "GET", filters={"room_code": current_code})
    if not players: return
    
    pl = [_to_dict(p) for p in players]
    window.renderScoreboardPlayers(pl)
    
    idx, ans, dl = int(rd.current_q_index), bool(getattr(rd, "question_answered", False)), getattr(rd, "question_deadline", None)
    if not ans: _has_answered_this_round = False
    if idx >= len(questions_data): return show_results(pl)
    
    q = questions_data[idx]
    grid = document.getElementById("options-list")
    grid.innerHTML = ""
    for i, opt in enumerate(q["options"]):
        btn = document.createElement("button")
        btn.className = "opt-btn"
        btn.innerText = opt
        btn.disabled = ans
        if ans and i == q["answer"]: btn.classList.add("correct")
        btn.onclick = create_proxy(lambda e, val=i: asyncio.ensure_future(submit_answer(val)))
        grid.appendChild(btn)
        
    if _timer_task: _timer_task.cancel()
    if not ans: _timer_task = asyncio.ensure_future(run_timer(dl))

# ══════════════════════════════════════════════════════════
# HANDLERS & INIT
# ══════════════════════════════════════════════════════════

def handle_room_update(p):
    if getattr(p.new, "is_closed", False): window.location.reload()
    if p.new.is_started:
        document.getElementById("screen-waiting").classList.remove("active")
        document.getElementById("screen-game").classList.add("active")
        asyncio.ensure_future(sync_game(p.new))

def handle_player_update(p):
    # If I am kicked, reload
    if my_id and int(getattr(p.new, "id", 0)) == int(my_id) and getattr(p.new, "is_kicked", False):
        window.location.reload()
    asyncio.ensure_future(_refresh_lobby())

async def _refresh_lobby():
    players = await sb_request("players", "GET", filters={"room_code": current_code})
    if players:
        pl = [_to_dict(p) for p in players]
        window.renderWaiting(pl, max_players)
        
        # Update Host button state
        btn = document.getElementById("btn-start")
        if btn:
            ready_count = sum(1 for p in pl if p["is_ready"])
            all_ready = ready_count == len(pl) and len(pl) >= 2
            btn.disabled = not all_ready
            btn.innerText = "START GAME →" if all_ready else f"WAITING FOR READY ({ready_count}/{len(pl)})"

async def init():
    # Load questions once
    r = await pyfetch("https://opentdb.com/api.php?amount=10&type=multiple&encode=url3986")
    raw = await r.json()
    import urllib.parse
    for item in raw["results"]:
        ans = urllib.parse.unquote(item["correct_answer"])
        opts = [urllib.parse.unquote(w) for w in item["incorrect_answers"]] + [ans]
        random.shuffle(opts)
        questions_data.append({"question": urllib.parse.unquote(item["question"]), "options": opts, "answer": opts.index(ans)})
    
    # Global UI Proxies
    window.addEventListener("proceedFlow", create_proxy(lambda e: asyncio.ensure_future(host_game(e) if window.getFlow()=="host" else join_game(e))))
    window.addEventListener("toggleReady", create_proxy(lambda e: asyncio.ensure_future(sb_request(f"players?id=eq.{my_id}", "PATCH", {"is_ready": True}))))
    window.addEventListener("quickJoin", create_proxy(lambda e: asyncio.ensure_future(quick_join(e))))
    window.addEventListener("startGame", create_proxy(lambda e: asyncio.ensure_future(sb_request(f"rooms?code=eq.{current_code}", "PATCH", {"is_started": True}))))

def show_results(pl):
    window.goTo("screen-results")
    window.renderBreakdown(pl)

asyncio.ensure_future(init())