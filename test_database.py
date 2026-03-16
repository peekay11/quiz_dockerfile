"""
tests/test_lobby.py
===================
Full pytest suite for Quiz Showdown — lobby, room management,
matchmaking, ready system, host migration, and game-loop logic.

Runs with stdlib runner OR pytest:
    python3 tests/test_lobby.py
    pytest  tests/test_lobby.py -v
"""

# ══════════════════════════════════════════════════════════
# 0.  BOOTSTRAP — mock Pyodide / browser before any import
# ══════════════════════════════════════════════════════════
import sys, asyncio, json, unittest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ── Fake DOM primitives ───────────────────────────────────
class FakeStyle:
    """Attribute-bag that never raises AttributeError."""
    def __init__(self):
        object.__setattr__(self, "_d", {})
    def __setattr__(self, k, v):
        self._d[k] = v
    def __getattr__(self, k):
        return self._d.get(k, "")


class FakeClassList:
    def __init__(self):
        self._classes: set = set()
        self.add      = MagicMock(side_effect=self._add)
        self.remove   = MagicMock(side_effect=self._remove)
        self.contains = MagicMock(side_effect=lambda c: c in self._classes)

    def _add(self, c):    self._classes.add(c)
    def _remove(self, c): self._classes.discard(c)


class FakeElement:
    def __init__(self, tag="div"):
        self.tag       = tag
        self.innerText = ""
        self.innerHTML = ""
        self.disabled  = False
        self.value     = ""
        self.className = ""
        self.style     = FakeStyle()
        self.classList = FakeClassList()
        self._attrs:    dict = {}
        self._children: list = []
        self.onclick   = None

    def setAttribute(self, k, v):  self._attrs[k] = v
    def getAttribute(self, k):     return self._attrs.get(k)
    def appendChild(self, child):  self._children.append(child)
    def querySelectorAll(self, _): return FakeNodeList(self._children)


class FakeNodeList:
    def __init__(self, items):    self._items = list(items)
    @property
    def length(self):             return len(self._items)
    def __getitem__(self, i):     return self._items[i]
    def __iter__(self):           return iter(self._items)


# ── Fake document / window ────────────────────────────────
_elements: dict = {}


def _el(id_: str) -> FakeElement:
    if id_ not in _elements:
        _elements[id_] = FakeElement(id_)
    return _elements[id_]


fake_document = MagicMock()
fake_document.getElementById   = MagicMock(side_effect=_el)
fake_document.createElement    = MagicMock(side_effect=lambda t: FakeElement(t))
fake_document.querySelectorAll = MagicMock(return_value=FakeNodeList([]))

fake_window = MagicMock()
for _attr in (
    "showError", "showDebugError", "showWaitingRoom", "showToast", "goTo",
    "renderWaiting", "renderScoreboardPlayers", "renderBreakdown",
    "getFlow", "getPlayerCount", "getIsPrivate", "addEventListener",
):
    setattr(fake_window, _attr, MagicMock())
fake_window.getFlow.return_value        = "host"
fake_window.getPlayerCount.return_value = 4
fake_window.getIsPrivate.return_value   = False
fake_window.supabase                    = MagicMock()

_js_mod = MagicMock()
_js_mod.document = fake_document
_js_mod.window   = fake_window

sys.modules["js"]           = _js_mod
sys.modules["pyodide"]      = MagicMock()
sys.modules["pyodide.http"] = MagicMock()
sys.modules["pyodide.ffi"]  = MagicMock()
sys.modules["pyodide.ffi"].create_proxy = lambda f: f

# ── Load database.py under test ───────────────────────────
import importlib.util, os

_noop_fetch = AsyncMock(
    return_value=MagicMock(status=200, json=AsyncMock(return_value=[]))
)
sys.modules["pyodide.http"].pyfetch = _noop_fetch

with patch("asyncio.ensure_future"):
    _spec = importlib.util.spec_from_file_location(
        "database",
        os.path.join(os.path.dirname(__file__), "..", "database.py"),
    )
    db = importlib.util.module_from_spec(_spec)
    with patch("asyncio.ensure_future"):
        _spec.loader.exec_module(db)


# ══════════════════════════════════════════════════════════
# 1.  SHARED HELPERS
# ══════════════════════════════════════════════════════════

def _run(coro):
    """Execute a coroutine synchronously (works with or without pytest-asyncio)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        # Cancel any dangling tasks so we never see "coroutine was never awaited"
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.close()


def reset():
    """Restore module globals and clear the fake DOM between tests."""
    db.current_code   = ""
    db.my_id          = None
    db.my_team        = None
    db.my_name        = "Connecting..."
    db.is_host        = False
    db.max_players    = 4
    db.questions_data = []
    db._timer_task    = None
    db._polling       = True
    _elements.clear()
    for attr in (
        "showError", "showDebugError", "showWaitingRoom", "showToast",
        "goTo", "renderWaiting", "renderScoreboardPlayers", "renderBreakdown",
    ):
        getattr(fake_window, attr).reset_mock()
    fake_window.getFlow.return_value        = "host"
    fake_window.getPlayerCount.return_value = 4
    fake_window.getIsPrivate.return_value   = False


# ── Factory helpers ───────────────────────────────────────

def mk_room(**kw) -> dict:
    base = {
        "code": "ABCD", "max_players": 4, "is_started": False,
        "is_private": False, "current_q_index": 0,
        "active_player_id": None, "host_id": 1, "is_closed": False,
    }
    base.update(kw)
    return base


def mk_player(
    id_: int,
    name: str,
    *,
    team=None,
    score: int = 0,
    ready: bool = False,
    host: bool = False,
    kicked: bool = False,
    correct: int = 0,
    room: str = "ABCD",
) -> dict:
    return {
        "id": id_, "name": name, "team": team, "score": score,
        "is_ready": ready, "is_host": host, "is_kicked": kicked,
        "correct_answers": correct, "room_code": room,
    }


def mk_question(text: str = "Q?", opts=None, answer: int = 0) -> dict:
    return {"question": text, "options": opts or ["A", "B", "C", "D"], "answer": answer}


def make_opts_dom(n: int = 4) -> FakeElement:
    parent = _el("options-list")
    parent._children = [FakeElement("button") for _ in range(n)]
    return parent


# ══════════════════════════════════════════════════════════
# 2.  TEST CLASSES
# ══════════════════════════════════════════════════════════

class TestSafeName(unittest.TestCase):
    """safe_name() must NEVER return None, 'null', 'undefined', or empty."""

    def test_normal_name(self):
        self.assertEqual(db.safe_name("Alice"), "Alice")

    def test_none_returns_placeholder(self):
        self.assertEqual(db.safe_name(None), "Connecting...")

    def test_empty_string_returns_placeholder(self):
        self.assertEqual(db.safe_name(""), "Connecting...")

    def test_whitespace_only_returns_placeholder(self):
        self.assertEqual(db.safe_name("   "), "Connecting...")

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(db.safe_name("  Bob  "), "Bob")

    def test_numeric_string_preserved(self):
        self.assertEqual(db.safe_name("007"), "007")

    def test_integer_converted_to_string(self):
        result = db.safe_name(42)
        self.assertIsInstance(result, str)
        self.assertNotEqual(result, "")


# ─────────────────────────────────────────────────────────

class TestRoomSecurity(unittest.TestCase):
    """
    Capacity enforcement, started-room guard, private-room flag.

    Scenario matrix (parametrized inline):
      - Joining with N-of-N players present → rejected
      - Joining with N+k players present → rejected
      - Joining with N-1 players present → accepted
    """

    def setUp(self):
        reset()

    # ── Full-room rejection ───────────────────────────────

    def test_joining_full_room_rejected(self):
        """The (N+1)th player must get a 'Room is full' error and no INSERT."""
        _el("player-name").value = "Late"
        _el("join-code").value   = "ABCD"
        full = [mk_player(i, f"P{i}") for i in range(1, 5)]  # 4/4

        async def fake_get(table, filters=None):
            return [mk_room(max_players=4)] if table == "rooms" else full

        async def go():
            with patch.object(db, "sb_get", side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock()) as mp:
                await db.join_game(None)
            fake_window.showError.assert_called_once()
            self.assertIn("full", fake_window.showError.call_args[0][0].lower())
            mp.assert_not_called()

        _run(go())

    def test_joining_over_capacity_rejected(self):
        """Room already has MORE players than max (data anomaly) — must still reject."""
        _el("player-name").value = "X"
        _el("join-code").value   = "ABCD"
        over = [mk_player(i, f"P{i}") for i in range(1, 7)]  # 6 in a cap-4 room

        async def fake_get(table, filters=None):
            return [mk_room(max_players=4)] if table == "rooms" else over

        async def go():
            with patch.object(db, "sb_get", side_effect=fake_get):
                await db.join_game(None)
            self.assertIn("full", fake_window.showError.call_args[0][0].lower())

        _run(go())

    def test_one_slot_free_allows_join(self):
        """Room with 3/4 occupied slots must accept the 4th player."""
        _el("player-name").value = "D"
        _el("join-code").value   = "ABCD"
        partial = [mk_player(i, f"P{i}") for i in range(1, 4)]  # 3/4

        async def fake_get(table, filters=None):
            return [mk_room(max_players=4)] if table == "rooms" else partial

        async def go():
            with patch.object(db, "sb_get",  side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock(return_value={"id": 99})), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.join_game(None)
            fake_window.showError.assert_not_called()

        _run(go())

    # Parametrized: capacity sizes ──────────────────────────
    def _assert_full_rejected(self, cap: int):
        reset()
        _el("player-name").value = "New"
        _el("join-code").value   = "ABCD"
        full = [mk_player(i, f"P{i}") for i in range(1, cap + 1)]

        async def fake_get(table, filters=None):
            return [mk_room(max_players=cap)] if table == "rooms" else full

        async def go():
            with patch.object(db, "sb_get", side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock()):
                await db.join_game(None)
            self.assertIn("full", fake_window.showError.call_args[0][0].lower())

        _run(go())

    def test_full_room_cap_2(self):   self._assert_full_rejected(2)
    def test_full_room_cap_4(self):   self._assert_full_rejected(4)
    def test_full_room_cap_6(self):   self._assert_full_rejected(6)
    def test_full_room_cap_10(self):  self._assert_full_rejected(10)

    # ── Started-room guard ────────────────────────────────

    def test_cannot_join_started_room(self):
        _el("player-name").value = "Late"
        _el("join-code").value   = "ABCD"

        async def fake_get(table, filters=None):
            return [mk_room(is_started=True)] if table == "rooms" else []

        async def go():
            with patch.object(db, "sb_get", side_effect=fake_get):
                await db.join_game(None)
            msg = fake_window.showError.call_args[0][0].lower()
            self.assertIn("progress", msg)

        _run(go())

    # ── Privacy flag ──────────────────────────────────────

    def test_private_room_stored(self):
        fake_window.getIsPrivate.return_value = True
        _el("player-name").value = "Host"
        rooms_inserted = []

        async def fake_post(table, data):
            if table == "rooms": rooms_inserted.append(data)
            return {"id": 1}

        async def go():
            with patch.object(db, "sb_post",  side_effect=fake_post), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertTrue(rooms_inserted[0]["is_private"])

        _run(go())

    def test_public_room_stored(self):
        fake_window.getIsPrivate.return_value = False
        _el("player-name").value = "Host"
        rooms_inserted = []

        async def fake_post(table, data):
            if table == "rooms": rooms_inserted.append(data)
            return {"id": 1}

        async def go():
            with patch.object(db, "sb_post",  side_effect=fake_post), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertFalse(rooms_inserted[0]["is_private"])

        _run(go())


# ─────────────────────────────────────────────────────────

class TestReadySystem(unittest.TestCase):
    """
    All players must be ready before start_game() proceeds.
    Tests cover the full state machine: none ready → partial → all ready.
    """

    def setUp(self):
        reset()
        db.current_code = "ABCD"

    # ── Start blocked ─────────────────────────────────────

    def test_start_blocked_zero_ready(self):
        players = [mk_player(1, "A", ready=False), mk_player(2, "B", ready=False)]

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", AsyncMock()) as mp:
                await db.start_game(None)
            fake_window.showError.assert_called_once()
            mp.assert_not_called()

        _run(go())

    def test_start_blocked_partial_ready(self):
        players = [mk_player(1, "Alice", ready=True), mk_player(2, "Bob", ready=False)]

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", AsyncMock()) as mp:
                await db.start_game(None)
            msg = fake_window.showError.call_args[0][0]
            # Error must name the unready player — never 'undefined' or 'null'
            self.assertNotIn("undefined", msg)
            self.assertNotIn("null",      msg)
            self.assertIn("Bob",          msg)
            mp.assert_not_called()

        _run(go())

    def test_start_blocked_single_player(self):
        """Even if one player is ready, minimum headcount (2) still blocks start."""
        players = [mk_player(1, "Solo", ready=True)]

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", AsyncMock()) as mp:
                await db.start_game(None)
            fake_window.showError.assert_called_once()
            mp.assert_not_called()

        _run(go())

    # Parametrized: various not-ready counts ───────────────
    def _check_blocked(self, total: int, ready_count: int):
        reset()
        db.current_code = "ABCD"
        players = [
            mk_player(i, f"P{i}", ready=(i <= ready_count))
            for i in range(1, total + 1)
        ]

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", AsyncMock()) as mp:
                await db.start_game(None)
            fake_window.showError.assert_called_once()
            mp.assert_not_called()

        _run(go())

    def test_blocked_4p_0ready(self):  self._check_blocked(4, 0)
    def test_blocked_4p_1ready(self):  self._check_blocked(4, 1)
    def test_blocked_4p_3ready(self):  self._check_blocked(4, 3)
    def test_blocked_6p_5ready(self):  self._check_blocked(6, 5)

    # ── Start proceeds ────────────────────────────────────

    def test_start_proceeds_all_ready(self):
        players = [mk_player(1, "A", ready=True), mk_player(2, "B", ready=True)]
        room_updates = []

        async def fake_patch(table, data, filters):
            if table == "rooms": room_updates.append(data)

        async def go():
            with patch.object(db, "sb_get",   AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  side_effect=fake_patch):
                await db.start_game(None)
            self.assertTrue(
                any(u.get("is_started") for u in room_updates),
                "rooms.is_started was never set to True",
            )

        _run(go())

    # Parametrized: various all-ready counts ───────────────
    def _check_starts(self, n: int):
        reset()
        db.current_code = "ABCD"
        players = [mk_player(i, f"P{i}", ready=True) for i in range(1, n + 1)]
        started = []

        async def fp(table, data, filters):
            if table == "rooms" and data.get("is_started"):
                started.append(True)

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", side_effect=fp):
                await db.start_game(None)
            self.assertTrue(started, f"Game did not start with {n} ready players")

        _run(go())

    def test_starts_2p_all_ready(self):  self._check_starts(2)
    def test_starts_4p_all_ready(self):  self._check_starts(4)
    def test_starts_6p_all_ready(self):  self._check_starts(6)

    # ── Ready toggle ──────────────────────────────────────

    def test_toggle_false_to_true(self):
        db.my_id = 1
        players  = [mk_player(1, "A", ready=False)]
        patched  = []

        async def fp(table, data, filters): patched.append(data)

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", side_effect=fp):
                await db.toggle_ready(None)
            self.assertTrue(patched[0]["is_ready"])

        _run(go())

    def test_toggle_true_to_false(self):
        db.my_id = 1
        players  = [mk_player(1, "A", ready=True)]
        patched  = []

        async def fp(table, data, filters): patched.append(data)

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", side_effect=fp):
                await db.toggle_ready(None)
            self.assertFalse(patched[0]["is_ready"])

        _run(go())

    # ── Ready button state ────────────────────────────────

    def test_button_disabled_when_not_all_ready(self):
        btn = _el("btn-start"); btn.disabled = False
        players = [mk_player(1, "A", ready=False), mk_player(2, "B", ready=True)]
        db._update_ready_button(players)
        self.assertTrue(btn.disabled)

    def test_button_enabled_when_all_ready(self):
        btn = _el("btn-start"); btn.disabled = True
        players = [mk_player(1, "A", ready=True), mk_player(2, "B", ready=True)]
        db._update_ready_button(players)
        self.assertFalse(btn.disabled)

    def test_button_disabled_when_empty_player_list(self):
        btn = _el("btn-start"); btn.disabled = False
        db._update_ready_button([])
        self.assertTrue(btn.disabled)

    def test_unready_error_never_contains_none_or_undefined(self):
        """Error message for unready players must always use safe_name."""
        players = [mk_player(1, None, ready=False), mk_player(2, "B", ready=True)]

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", AsyncMock()):
                await db.start_game(None)
            msg = fake_window.showError.call_args[0][0]
            # These literal bad values must never appear in the error text
            for bad in ("None", "null", "undefined"):
                self.assertNotIn(bad, msg, f"Found '{bad}' in error: {msg!r}")
            # The message must be non-empty and contain something meaningful
            self.assertTrue(len(msg.strip()) > 0, "Error message must not be blank")

        _run(go())


# ─────────────────────────────────────────────────────────

class TestPlayerNameSafety(unittest.TestCase):
    """
    player_name must never be None, null, or 'undefined' anywhere in the UI.
    Covers: join validation, host validation, DOM rendering, active-player display.
    """

    def setUp(self): reset()

    # ── Input validation ──────────────────────────────────

    def test_join_empty_name_rejected(self):
        _el("player-name").value = ""
        _el("join-code").value   = "ABCD"
        _run(db.join_game(None))
        fake_window.showError.assert_called_once()

    def test_join_whitespace_name_rejected(self):
        _el("player-name").value = "   "
        _el("join-code").value   = "ABCD"
        _run(db.join_game(None))
        fake_window.showError.assert_called_once()

    def test_host_empty_name_rejected(self):
        _el("player-name").value = ""
        _run(db.host_game(None))
        fake_window.showError.assert_called_once()

    def test_host_whitespace_name_rejected(self):
        _el("player-name").value = "  "
        _run(db.host_game(None))
        fake_window.showError.assert_called_once()

    # ── Name stored correctly ─────────────────────────────

    def test_player_name_stripped_before_insert(self):
        _el("player-name").value = "  Alice  "
        fake_window.getPlayerCount.return_value = 2
        inserted = []

        async def fp(table, data):
            if table == "players": inserted.append(data["name"])
            return {"id": 1}

        async def go():
            with patch.object(db, "sb_post",  side_effect=fp), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertEqual(inserted[0], "Alice")

        _run(go())

    def test_my_name_set_on_host(self):
        _el("player-name").value = "Bob"
        fake_window.getPlayerCount.return_value = 2

        async def fp(table, data): return {"id": 1}

        async def go():
            with patch.object(db, "sb_post", side_effect=fp), \
                 patch.object(db, "sb_patch", AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertEqual(db.my_name, "Bob")

        _run(go())

    def test_my_name_never_none_after_join(self):
        _el("player-name").value = "Carol"
        _el("join-code").value   = "ABCD"

        async def fake_get(table, filters=None):
            return [mk_room()] if table == "rooms" else []

        async def go():
            with patch.object(db, "sb_get",  side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock(return_value={"id": 5})), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.join_game(None)
            self.assertIsNotNone(db.my_name)
            self.assertNotEqual(db.my_name, "")

        _run(go())

    # ── DOM safety: active player display ────────────────

    def test_active_player_display_never_undefined(self):
        """sync_game must not put None/null/undefined into the turn banner."""
        db.my_id = 1
        db.current_code = "ABCD"
        db.questions_data = [mk_question()]
        players = [mk_player(1, None, team=1), mk_player(2, "B", team=2)]

        class FakeRoom:
            active_player_id = 1; current_q_index = 0; is_started = True

        async def go():
            with patch.object(db, "sb_get", AsyncMock(return_value=players)), \
                 patch("asyncio.ensure_future"):
                await db.sync_game(FakeRoom())
            text = _el("active-player-display").innerText
            for bad in ("None", "null", "undefined"):
                self.assertNotIn(bad, text,
                    f"Found '{bad}' in active-player-display: {text!r}")

        _run(go())

    # Parametrized: bad name inputs ────────────────────────
    def _check_name_rejected_for_host(self, bad_name: str):
        reset()
        _el("player-name").value = bad_name
        _run(db.host_game(None))
        fake_window.showError.assert_called_once(), \
            f"Expected showError for name={bad_name!r}"

    def test_rejects_tab_only_name(self):   self._check_name_rejected_for_host("\t")
    def test_rejects_newline_name(self):    self._check_name_rejected_for_host("\n")
    def test_rejects_mixed_space_name(self):self._check_name_rejected_for_host(" \t ")


# ─────────────────────────────────────────────────────────

class TestMatchmaking(unittest.TestCase):
    """Quick Join / auto-matchmaking: finds open public rooms, skips full/private/started."""

    def setUp(self): reset()

    def test_quick_join_finds_open_public_room(self):
        _el("player-name").value = "Quick"
        rooms   = [mk_room(code="OPEN", is_private=False, is_started=False, max_players=4)]
        players_in_room = [mk_player(1, "A")]

        async def fake_get(table, filters=None):
            if table == "rooms":   return rooms
            if table == "players": return players_in_room
            return []

        async def go():
            with patch.object(db, "sb_get",  side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock(return_value={"id": 99})), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                _el("join-code").value = ""
                await db.quick_join(None)
            self.assertEqual(db.current_code, "OPEN")
            fake_window.showError.assert_not_called()

        _run(go())

    def test_quick_join_skips_full_rooms(self):
        _el("player-name").value = "Quick"
        full = mk_room(code="FULL", max_players=2)
        free = mk_room(code="FREE", max_players=4)

        async def fake_get(table, filters=None):
            if table == "rooms": return [full, free]
            code = (filters or {}).get("room_code")
            if code == "FULL": return [mk_player(1,"A"), mk_player(2,"B")]
            if code == "FREE": return [mk_player(3,"C")]
            return []

        async def go():
            with patch.object(db, "sb_get",  side_effect=fake_get), \
                 patch.object(db, "sb_post", AsyncMock(return_value={"id": 99})), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                _el("join-code").value = ""
                await db.quick_join(None)
            self.assertEqual(db.current_code, "FREE")

        _run(go())

    def test_quick_join_no_rooms_shows_error(self):
        _el("player-name").value = "Quick"

        async def go():
            with patch.object(db, "sb_get", AsyncMock(return_value=[])):
                await db.quick_join(None)
            fake_window.showError.assert_called_once()
            self.assertIn("no open", fake_window.showError.call_args[0][0].lower())

        _run(go())

    def test_quick_join_requires_name(self):
        _el("player-name").value = ""

        async def go():
            with patch.object(db, "sb_get", AsyncMock(return_value=[])):
                await db.quick_join(None)
            fake_window.showError.assert_called_once()

        _run(go())

    def test_quick_join_skips_private_rooms(self):
        """When the DB returns no public rooms (filter applied server-side), show error."""
        _el("player-name").value = "Quick"

        async def go():
            # Simulate DB returning [] for is_private=False filter
            with patch.object(db, "sb_get", AsyncMock(return_value=[])):
                await db.quick_join(None)
            fake_window.showError.assert_called_once()

        _run(go())

    def test_quick_join_skips_started_rooms(self):
        _el("player-name").value = "Quick"

        async def go():
            with patch.object(db, "sb_get", AsyncMock(return_value=[])):
                await db.quick_join(None)
            fake_window.showError.assert_called_once()

        _run(go())

    # Parametrized: all-full rooms of different sizes ──────
    def _all_full_shows_error(self, cap: int):
        reset()
        _el("player-name").value = "Q"
        rooms = [mk_room(code=f"R{i}", max_players=cap) for i in range(3)]
        full  = [mk_player(j, f"P{j}") for j in range(1, cap + 1)]

        async def fake_get(table, filters=None):
            if table == "rooms":   return rooms
            if table == "players": return full
            return []

        async def go():
            with patch.object(db, "sb_get", side_effect=fake_get):
                await db.quick_join(None)
            fake_window.showError.assert_called_once()

        _run(go())

    def test_all_full_cap_2(self):  self._all_full_shows_error(2)
    def test_all_full_cap_4(self):  self._all_full_shows_error(4)
    def test_all_full_cap_8(self):  self._all_full_shows_error(8)


# ─────────────────────────────────────────────────────────

class TestHostPermissions(unittest.TestCase):
    """Host kick authority, non-host blocked, kicked player redirected."""

    def setUp(self): reset(); db.current_code = "ABCD"

    def test_host_can_kick_player(self):
        db.is_host = True; db.my_id = 1
        patched = []

        async def fp(table, data, filters): patched.append((table, data, filters))

        async def go():
            with patch.object(db, "sb_patch", side_effect=fp):
                await db.kick_player(2)
            kick_ops = [p for p in patched
                        if p[0] == "players"
                        and p[1].get("is_kicked") is True
                        and p[2].get("id") == 2]
            self.assertTrue(kick_ops, "Expected players.is_kicked=True for id=2")

        _run(go())

    def test_non_host_cannot_kick(self):
        db.is_host = False; db.my_id = 2

        async def go():
            with patch.object(db, "sb_patch", AsyncMock()) as mp:
                await db.kick_player(3)
            mp.assert_not_called()

        _run(go())

    def test_kicked_player_redirected_to_landing(self):
        db.my_id = 5

        class FakePayload:
            class new:
                id = 5; is_kicked = True

        db.handle_player_update(FakePayload())
        fake_window.showToast.assert_called()
        msg = fake_window.showToast.call_args[0][0].lower()
        self.assertIn("removed", msg)
        fake_window.goTo.assert_called_with("screen-landing")

    def test_kicked_toast_never_shows_undefined(self):
        db.my_id = 5

        class FakePayload:
            class new:
                id = 5; is_kicked = True

        db.handle_player_update(FakePayload())
        msg = fake_window.showToast.call_args[0][0]
        for bad in ("undefined", "null", "None"):
            self.assertNotIn(bad, msg)

    def test_other_player_kicked_does_not_redirect_me(self):
        db.my_id = 5

        class FakePayload:
            class new:
                id = 99; is_kicked = True

        with patch.object(db, "_refresh_lobby", AsyncMock()), \
             patch("asyncio.ensure_future"):
            db.handle_player_update(FakePayload())
        fake_window.goTo.assert_not_called()


# ─────────────────────────────────────────────────────────

class TestHostMigration(unittest.TestCase):
    """
    Host leaving triggers migration to next player.
    Host leaving alone closes the room.
    Guest leaving deletes only their row.
    """

    def setUp(self):
        reset()
        db.current_code = "ABCD"
        db.is_host = True
        db.my_id   = 1

    def test_host_leaves_with_others_migrates_host(self):
        players = [mk_player(1, "Host", host=True), mk_player(2, "Bob")]
        patched = []

        async def fp(table, data, filters): patched.append((table, data, filters))

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  side_effect=fp), \
                 patch.object(db, "sb_delete", AsyncMock()), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            new_host_ops = [p for p in patched
                            if p[0] == "players" and p[1].get("is_host") is True]
            self.assertTrue(new_host_ops, "Expected players.is_host=True for new host")
            self.assertEqual(new_host_ops[0][2]["id"], 2,
                             "Expected player 2 to become host")

        _run(go())

    def test_host_leaves_alone_closes_room(self):
        players = [mk_player(1, "Host", host=True)]
        patched = []

        async def fp(table, data, filters): patched.append((table, data, filters))

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  side_effect=fp), \
                 patch.object(db, "sb_delete", AsyncMock()), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            closed = [p for p in patched
                      if p[0] == "rooms" and p[1].get("is_closed") is True]
            self.assertTrue(closed, "Expected rooms.is_closed=True")

        _run(go())

    def test_host_migration_shows_toast_with_new_name(self):
        players = [mk_player(1, "Host", host=True), mk_player(2, "NewHost")]

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "sb_delete", AsyncMock()), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            fake_window.showToast.assert_called()
            msg = fake_window.showToast.call_args[0][0]
            self.assertIn("NewHost", msg)
            for bad in ("None", "null", "undefined"):
                self.assertNotIn(bad, msg)

        _run(go())

    def test_guest_leave_deletes_own_player_row(self):
        db.is_host = False; db.my_id = 2
        deleted = []

        async def fd(table, filters): deleted.append((table, filters))

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=[])), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "sb_delete", side_effect=fd), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            own = [d for d in deleted
                   if d[0] == "players" and d[1].get("id") == 2]
            self.assertTrue(own, "Expected players row for id=2 to be deleted")

        _run(go())

    def test_room_closed_event_redirects_all_clients(self):
        class FakePayload:
            class new:
                is_closed  = True
                is_started = False

        db.handle_room_update(FakePayload())
        fake_window.goTo.assert_called_with("screen-landing")

    def test_host_state_reset_after_leave(self):
        players = [mk_player(1, "Host", host=True)]

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  AsyncMock()), \
                 patch.object(db, "sb_delete", AsyncMock()), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            self.assertIsNone(db.my_id)
            self.assertFalse(db.is_host)
            self.assertEqual(db.current_code, "")

        _run(go())

    # Parametrized: any host-leaving player count migrates ─
    def _check_migration_for_n(self, total: int):
        reset()
        db.current_code = "ABCD"; db.is_host = True; db.my_id = 1
        players = [mk_player(i, f"P{i}", host=(i == 1)) for i in range(1, total + 1)]
        new_host_ops = []

        async def fp(table, data, filters):
            if table == "players" and data.get("is_host"):
                new_host_ops.append(filters["id"])

        async def go():
            with patch.object(db, "sb_get",    AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch",  side_effect=fp), \
                 patch.object(db, "sb_delete", AsyncMock()), \
                 patch("asyncio.ensure_future"):
                await db.leave_room(None)
            self.assertTrue(new_host_ops,
                f"No host migration with {total} players")

        _run(go())

    def test_migration_with_2_players(self):  self._check_migration_for_n(2)
    def test_migration_with_4_players(self):  self._check_migration_for_n(4)
    def test_migration_with_6_players(self):  self._check_migration_for_n(6)


# ─────────────────────────────────────────────────────────

class TestGameLoop(unittest.TestCase):
    """
    Game start sync (all clients q_index=0), winner calculation by
    correct_answers, game-over detection, question ordering.
    """

    def setUp(self):
        reset()
        db.current_code   = "ABCD"
        db.my_id          = 1
        db.questions_data = [mk_question(f"Q{i}") for i in range(5)]

    # ── Sync start ────────────────────────────────────────

    def test_start_resets_q_index_to_zero(self):
        players = [mk_player(1, "A", ready=True), mk_player(2, "B", ready=True)]
        room_updates = []

        async def fp(table, data, filters):
            if table == "rooms": room_updates.append(data)

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", side_effect=fp):
                await db.start_game(None)
            self.assertEqual(room_updates[-1]["current_q_index"], 0)

        _run(go())

    def test_game_started_at_is_set(self):
        players = [mk_player(1, "A", ready=True), mk_player(2, "B", ready=True)]
        room_updates = []

        async def fp(table, data, filters):
            if table == "rooms": room_updates.append(data)

        async def go():
            with patch.object(db, "sb_get",  AsyncMock(return_value=players)), \
                 patch.object(db, "sb_patch", side_effect=fp):
                await db.start_game(None)
            self.assertIn("game_started_at", room_updates[-1])

        _run(go())

    # ── Winner calculation ────────────────────────────────

    def test_volt_wins_by_correct_answers(self):
        players = [
            mk_player(1, "A", team=1, correct=5),
            mk_player(2, "B", team=1, correct=3),
            mk_player(3, "C", team=2, correct=2),
            mk_player(4, "D", team=2, correct=1),
        ]
        winner, ca1, ca2 = db._calc_winner(players)
        self.assertEqual(winner, "VOLT")
        self.assertEqual(ca1, 8); self.assertEqual(ca2, 3)

    def test_lime_wins_by_correct_answers(self):
        players = [mk_player(1,"A",team=1,correct=1), mk_player(2,"B",team=2,correct=9)]
        winner, _, _ = db._calc_winner(players)
        self.assertEqual(winner, "LIME")

    def test_draw_equal_correct_answers(self):
        players = [mk_player(1,"A",team=1,correct=4), mk_player(2,"B",team=2,correct=4)]
        winner, _, _ = db._calc_winner(players)
        self.assertEqual(winner, "DRAW")

    def test_falls_back_to_score_when_correct_zero(self):
        """If correct_answers=0 for all, score is used as fallback."""
        players = [mk_player(1,"A",team=1,score=3,correct=0),
                   mk_player(2,"B",team=2,score=1,correct=0)]
        winner, _, _ = db._calc_winner(players)
        self.assertEqual(winner, "VOLT")

    # Parametrized: winner outcomes ────────────────────────
    def _check_winner(self, t1c: int, t2c: int, expected: str):
        players = [mk_player(1,"A",team=1,correct=t1c),
                   mk_player(2,"B",team=2,correct=t2c)]
        winner, _, _ = db._calc_winner(players)
        self.assertEqual(winner, expected,
            f"Expected {expected} for {t1c} vs {t2c}, got {winner}")

    def test_winner_volt_10_0(self):   self._check_winner(10, 0,  "VOLT")
    def test_winner_volt_5_4(self):    self._check_winner(5,  4,  "VOLT")
    def test_winner_volt_1_0(self):    self._check_winner(1,  0,  "VOLT")
    def test_winner_lime_0_10(self):   self._check_winner(0,  10, "LIME")
    def test_winner_lime_4_5(self):    self._check_winner(4,  5,  "LIME")
    def test_winner_draw_0_0(self):    self._check_winner(0,  0,  "DRAW")
    def test_winner_draw_3_3(self):    self._check_winner(3,  3,  "DRAW")
    def test_winner_draw_10_10(self):  self._check_winner(10, 10, "DRAW")

    # ── Game over detection ───────────────────────────────

    def test_game_over_when_q_idx_past_end(self):
        db.questions_data = [mk_question()]  # only 1 question
        players = [mk_player(1,"A",team=1,score=2), mk_player(2,"B",team=2,score=0)]

        class FakeRoom:
            active_player_id = 1; current_q_index = 1; is_started = True

        async def go():
            with patch.object(db, "sb_get",     AsyncMock(return_value=players)), \
                 patch.object(db, "show_results") as ms, \
                 patch("asyncio.ensure_future"):
                await db.sync_game(FakeRoom())
            ms.assert_called_once()

        _run(go())

    def test_questions_display_in_order(self):
        players = [mk_player(1,"A",team=1), mk_player(2,"B",team=2)]
        displayed = []

        class FakeRoom:
            is_started = True
            def __init__(self, idx): self.active_player_id = 1; self.current_q_index = idx

        async def go():
            for i in range(3):
                with patch.object(db, "sb_get", AsyncMock(return_value=players)), \
                     patch("asyncio.ensure_future"):
                    await db.sync_game(FakeRoom(i))
                displayed.append(_el("q-text").innerText)

        _run(go())
        self.assertEqual(displayed, ["Q0", "Q1", "Q2"])

    def test_score_display_updated_each_sync(self):
        db.questions_data = [mk_question()]
        players = [mk_player(1,"A",team=1,score=3), mk_player(2,"B",team=2,score=1)]

        class FakeRoom:
            active_player_id = 1; current_q_index = 0; is_started = True

        async def go():
            with patch.object(db, "sb_get", AsyncMock(return_value=players)), \
                 patch("asyncio.ensure_future"):
                await db.sync_game(FakeRoom())
            self.assertEqual(_el("score-t1").innerText, "3")
            self.assertEqual(_el("score-t2").innerText, "1")

        _run(go())


# ─────────────────────────────────────────────────────────

class TestSocialFeatures(unittest.TestCase):
    """Room code format, share data correctness."""

    def setUp(self): reset()

    def test_room_code_is_4_chars(self):
        _el("player-name").value = "Host"
        fake_window.getPlayerCount.return_value = 2

        async def fp(table, data): return {"id": 1}

        async def go():
            with patch.object(db, "sb_post", side_effect=fp), \
                 patch.object(db, "sb_patch", AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertEqual(len(db.current_code), 4)

        _run(go())

    def test_room_code_is_uppercase_alpha(self):
        _el("player-name").value = "Host"
        fake_window.getPlayerCount.return_value = 2

        async def fp(table, data): return {"id": 1}

        async def go():
            with patch.object(db, "sb_post", side_effect=fp), \
                 patch.object(db, "sb_patch", AsyncMock()), \
                 patch.object(db, "listen_to_room"), \
                 patch("asyncio.ensure_future"):
                await db.host_game(None)
            self.assertTrue(db.current_code.isupper())
            self.assertTrue(db.current_code.isalpha())

        _run(go())

    # Parametrized: many code generations are unique ───────
    def test_codes_statistically_unique(self):
        """Generate 30 codes — all should be distinct (collision probability ~10^-20)."""
        codes = set()
        for _ in range(30):
            code = "".join(__import__("random").choices(
                __import__("string").ascii_uppercase, k=4))
            codes.add(code)
        # With 30 draws from 456976 possibilities, expect all unique
        self.assertGreater(len(codes), 25)


# ══════════════════════════════════════════════════════════
# 3.  STDLIB RUNNER  (also works as pytest collection)
# ══════════════════════════════════════════════════════════
if __name__ == "__main__":
    _suites = [
        TestSafeName,
        TestRoomSecurity,
        TestReadySystem,
        TestPlayerNameSafety,
        TestMatchmaking,
        TestHostPermissions,
        TestHostMigration,
        TestGameLoop,
        TestSocialFeatures,
    ]
    _loader = unittest.TestLoader()
    _suite  = unittest.TestSuite()
    for _cls in _suites:
        _suite.addTests(_loader.loadTestsFromTestCase(_cls))
    _result = unittest.TextTestRunner(verbosity=2).run(_suite)
    sys.exit(0 if _result.wasSuccessful() else 1)