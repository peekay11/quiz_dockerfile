# type: ignore
from js import document, window, Object, setInterval, clearInterval
import random, string
import asyncio

# --- REAL CONFIG ---
URL = "https://kqgywbdgoihbnhosnhjf.supabase.co"
KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImtxZ3l3YmRnb2loYm5ob3NuaGpmIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzM2NDkzOTEsImV4cCI6MjA4OTIyNTM5MX0.5F6mxm6NRGJEeZjfyOkj9BEQPhywGrJZd44a14QHW0E"

# Initialize Supabase
supabase = window.supabase.createClient(URL, KEY)

current_code = ""
my_id = None
timer_interval = None

async def host_game(event):
    global current_code, my_id
    name = document.getElementById("player-name").value or "Host"
    current_code = ''.join(random.choices(string.ascii_uppercase, k=4))
    
    try:
        # 1. Create the Room in Supabase
        await supabase.from_("rooms").insert(Object.fromEntries([
            ["code", current_code], 
            ["is_started", False]
        ]))

        # 2. Add Host to the 'players' table
        res = await supabase.from_("players").insert(Object.fromEntries([
            ["room_code", current_code], 
            ["name", name], 
            ["score", 0]
        ])).select().execute()
        
        my_id = res.data[0].id
        window.showWaiting(current_code, True)
        listen_to_room(current_code)

    except Exception as e:
        window.showDebugError(f"Host Error: {str(e)}")

async def join_game(event):
    global current_code, my_id
    current_code = document.getElementById("join-code").value.upper()
    name = document.getElementById("player-name").value or f"Player_{random.randint(10,99)}"
    
    try:
        # Check if room exists
        check = await supabase.from_("rooms").select("code").eq("code", current_code).execute()
        if not check.data or len(check.data) == 0:
            window.alert("Room not found!")
            return

        # Add Player
        res = await supabase.from_("players").insert(Object.fromEntries([
            ["room_code", current_code], 
            ["name", name], 
            ["score", 0]
        ])).select().execute()
        
        my_id = res.data[0].id
        window.showWaiting(current_code, False)
        listen_to_room(current_code)

    except Exception as e:
        window.showDebugError(f"Join Error: {str(e)}")

async def start_competition(event):
    try:
        # 1. Get all players for this room
        res = await supabase.from_("players").select("*").eq("room_code", current_code).execute()
        players = list(res.data)
        
        if len(players) < 2:
            window.alert("Need at least 2 players!")
            return

        random.shuffle(players)
        
        # 2. Randomly Assign Teams
        for i, p in enumerate(players):
            team_id = 1 if i % 2 == 0 else 2
            await supabase.from_("players").update(Object.fromEntries([["team", team_id]])).eq("id", p.id).execute()
        
        # 3. Start Game: Pick first active player
        await supabase.from_("rooms").update(Object.fromEntries([
            ["is_started", True], 
            ["active_player_id", players[0].id],
            ["current_q_index", 0]
        ])).eq("code", current_code).execute()

    except Exception as e:
        window.showDebugError(f"Start Error: {str(e)}")

def handle_update(payload):
    # This fires when Supabase detects a change
    room_data = payload.new
    if room_data.is_started:
        document.getElementById("start-menu").style.display = "none"
        document.getElementById("game-screen").style.display = "block"
        # Run UI Sync
        asyncio.ensure_future(sync_ui_state(room_data))

async def sync_ui_state(room_data):
    # Show who is playing
    player_res = await supabase.from_("players").select("name").eq("id", room_data.active_player_id).execute()
    if player_res.data:
        active_name = player_res.data[0].name
        document.getElementById("active-player-display").innerText = active_name
    
    # Lock the screen if it's not my turn
    is_my_turn = (int(my_id) == int(room_data.active_player_id))
    document.getElementById("input-lock").style.display = "none" if is_my_turn else "flex"
    
    # Restart the 10s timer
    start_local_timer()

def start_local_timer():
    global timer_interval
    if timer_interval: clearInterval(timer_interval)
    count = 10
    document.getElementById("timer-sec").innerText = "10"
    
    def tick():
        nonlocal count
        count -= 1
        document.getElementById("timer-sec").innerText = str(count)
        if count <= 0:
            clearInterval(timer_interval)
            # Timeout logic: Host would usually skip to next player here
            
    timer_interval = setInterval(tick, 1000)

def listen_to_room(code):
    # Set up Realtime listener
    config = Object.fromEntries([
        ["event", "UPDATE"],
        ["schema", "public"],
        ["table", "rooms"],
        ["filter", f"code=eq.{code}"]
    ])
    supabase.channel(f'room-{code}').on("postgres_changes", config, handle_update).subscribe()

# BINDINGS
document.getElementById("btn-join").onclick = join_game
document.getElementById("btn-start").onclick = start_competition
window.addEventListener("hostGame", host_game)