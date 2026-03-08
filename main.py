import asyncio
import json
import random
import string
import hashlib
import os
from typing import Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from supabase import create_client, Client
import uvicorn

# ── Supabase setup ──────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "YOUR_SUPABASE_ANON_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Cardinal Minigame")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory room state ─────────────────────────────────────────────────────
rooms: Dict[str, dict] = {}
# rooms[room_code] = {
#   "players": { username: { "ws": WebSocket, "score": 0, "time_left": 60 } },
#   "host": username,
#   "status": "waiting" | "playing" | "finished",
#   "current_questions": { username: { "number": int, "answer": str } },
#   "timers": { username: asyncio.Task }
# }

# ── Number to Indonesian words ────────────────────────────────────────────────
ONES = ["", "satu", "dua", "tiga", "empat", "lima", "enam", "tujuh", "delapan", "sembilan",
        "sepuluh", "sebelas", "dua belas", "tiga belas", "empat belas", "lima belas",
        "enam belas", "tujuh belas", "delapan belas", "sembilan belas"]
TENS = ["", "", "dua puluh", "tiga puluh", "empat puluh", "lima puluh",
        "enam puluh", "tujuh puluh", "delapan puluh", "sembilan puluh"]

def spell_number(n: int) -> str:
    if n < 0:
        return "minus " + spell_number(-n)
    if n == 0:
        return "nol"
    if n < 20:
        return ONES[n]
    if n < 100:
        t = TENS[n // 10]
        o = ONES[n % 10]
        return t + (" " + o if o else "")
    if n < 1000:
        h = n // 100
        rem = n % 100
        prefix = "seratus" if h == 1 else ONES[h] + " ratus"
        return prefix + (" " + spell_number(rem) if rem else "")
    if n < 1_000_000:
        th = n // 1000
        rem = n % 1000
        prefix = "seribu" if th == 1 else spell_number(th) + " ribu"
        return prefix + (" " + spell_number(rem) if rem else "")
    if n < 1_000_000_000:
        m = n // 1_000_000
        rem = n % 1_000_000
        return spell_number(m) + " juta" + (" " + spell_number(rem) if rem else "")
    if n < 1_000_000_000_000:
        b = n // 1_000_000_000
        rem = n % 1_000_000_000
        return spell_number(b) + " miliar" + (" " + spell_number(rem) if rem else "")
    return str(n)

def format_number(n: int) -> str:
    return f"{n:,}".replace(",", ".")

def generate_question() -> dict:
    # Weighted range: more interesting numbers
    tier = random.choices(
        ["ribuan", "puluhan_ribu", "ratusan_ribu", "jutaan", "puluhan_juta", "miliaran"],
        weights=[15, 20, 20, 20, 15, 10]
    )[0]
    ranges = {
        "ribuan": (1_000, 9_999),
        "puluhan_ribu": (10_000, 99_999),
        "ratusan_ribu": (100_000, 999_999),
        "jutaan": (1_000_000, 9_999_999),
        "puluhan_juta": (10_000_000, 999_999_999),
        "miliaran": (1_000_000_000, 9_999_999_999),
    }
    lo, hi = ranges[tier]
    n = random.randint(lo, hi)
    return {"number": n, "display": format_number(n), "answer": spell_number(n)}

def normalize(text: str) -> str:
    return " ".join(text.strip().lower().split())

def generate_room_code() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=6))

# ── Auth endpoints ────────────────────────────────────────────────────────────
class AuthRequest(BaseModel):
    username: str
    password: str

def hash_password(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()

@app.post("/api/register")
async def register(req: AuthRequest):
    username = req.username.strip().lower()
    if len(username) < 3:
        raise HTTPException(400, "Username minimal 3 karakter")
    if len(req.password) < 4:
        raise HTTPException(400, "Password minimal 4 karakter")
    # Check existing
    existing = supabase.table("users").select("id").eq("username", username).execute()
    if existing.data:
        raise HTTPException(400, "Username sudah dipakai")
    pw_hash = hash_password(req.password)
    result = supabase.table("users").insert({"username": username, "password_hash": pw_hash}).execute()
    if not result.data:
        raise HTTPException(500, "Gagal registrasi")
    return {"ok": True, "username": username}

@app.post("/api/login")
async def login(req: AuthRequest):
    username = req.username.strip().lower()
    pw_hash = hash_password(req.password)
    result = supabase.table("users").select("id,username").eq("username", username).eq("password_hash", pw_hash).execute()
    if not result.data:
        raise HTTPException(401, "Username atau password salah")
    return {"ok": True, "username": result.data[0]["username"]}

# ── Room endpoints ────────────────────────────────────────────────────────────
@app.post("/api/room/create")
async def create_room(body: dict):
    username = body.get("username", "").strip().lower()
    if not username:
        raise HTTPException(400, "Username required")
    code = generate_room_code()
    while code in rooms:
        code = generate_room_code()
    rooms[code] = {
        "players": {},
        "host": username,
        "status": "waiting",
        "current_questions": {},
        "timers": {},
    }
    # Save to supabase
    supabase.table("rooms").insert({"code": code, "host": username, "status": "waiting"}).execute()
    return {"ok": True, "room_code": code}

@app.get("/api/room/{code}")
async def get_room(code: str):
    code = code.upper()
    result = supabase.table("rooms").select("*").eq("code", code).execute()
    if not result.data:
        raise HTTPException(404, "Room tidak ditemukan")
    r = result.data[0]
    return {"ok": True, "room": r}

# ── WebSocket ─────────────────────────────────────────────────────────────────
async def send_to(ws: WebSocket, msg: dict):
    try:
        await ws.send_text(json.dumps(msg))
    except Exception:
        pass

async def broadcast_room(room_code: str, msg: dict, exclude: Optional[str] = None):
    room = rooms.get(room_code)
    if not room:
        return
    for uname, pdata in list(room["players"].items()):
        if uname != exclude:
            await send_to(pdata["ws"], msg)

async def get_player_list(room_code: str):
    room = rooms[room_code]
    return [
        {"username": u, "score": d["score"], "time_left": d["time_left"], "is_host": u == room["host"]}
        for u, d in room["players"].items()
    ]

async def start_player_timer(room_code: str, username: str):
    room = rooms.get(room_code)
    if not room:
        return
    player = room["players"].get(username)
    if not player:
        return

    while room["status"] == "playing" and player["time_left"] > 0:
        await asyncio.sleep(1)
        if room["status"] != "playing":
            break
        player["time_left"] = max(0, player["time_left"] - 1)
        # Broadcast timer update to all
        await broadcast_room(room_code, {
            "type": "timer_update",
            "username": username,
            "time_left": player["time_left"]
        })
        if player["time_left"] <= 0:
            # Player lost
            await send_to(player["ws"], {"type": "game_over", "result": "lose", "reason": "Waktu habis!"})
            await broadcast_room(room_code, {
                "type": "opponent_lost",
                "username": username,
                "reason": "Waktu habis"
            }, exclude=username)
            room["status"] = "finished"
            # Update scores to supabase
            await save_game_result(room_code, room)
            break

async def save_game_result(room_code: str, room: dict):
    try:
        for uname, pdata in room["players"].items():
            supabase.table("game_history").insert({
                "room_code": room_code,
                "username": uname,
                "score": pdata["score"],
                "time_left": pdata["time_left"]
            }).execute()
        supabase.table("rooms").update({"status": "finished"}).eq("code", room_code).execute()
    except Exception as e:
        print(f"Save error: {e}")

@app.websocket("/ws/{room_code}/{username}")
async def websocket_endpoint(websocket: WebSocket, room_code: str, username: str):
    room_code = room_code.upper()
    username = username.strip().lower()

    if room_code not in rooms:
        await websocket.close(code=4004, reason="Room tidak ditemukan")
        return

    room = rooms[room_code]
    if room["status"] == "playing":
        await websocket.close(code=4003, reason="Game sudah berlangsung")
        return

    await websocket.accept()

    # Add player
    room["players"][username] = {"ws": websocket, "score": 0, "time_left": 60}

    # Notify all players
    players = await get_player_list(room_code)
    await broadcast_room(room_code, {"type": "room_update", "players": players})
    await send_to(websocket, {"type": "joined", "room_code": room_code, "username": username, "is_host": username == room["host"]})

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            msg_type = data.get("type")

            # ── Start game (host only) ──────────────────────────────────────
            if msg_type == "start_game":
                if username != room["host"]:
                    await send_to(websocket, {"type": "error", "message": "Hanya host yang bisa mulai"})
                    continue
                if len(room["players"]) < 2:
                    await send_to(websocket, {"type": "error", "message": "Butuh minimal 2 pemain"})
                    continue

                room["status"] = "playing"
                supabase.table("rooms").update({"status": "playing"}).eq("code", room_code).execute()

                # Send first question to each player
                for uname, pdata in room["players"].items():
                    q = generate_question()
                    room["current_questions"][uname] = q
                    await send_to(pdata["ws"], {
                        "type": "game_start",
                        "question": q["display"],
                        "time_left": 60
                    })
                    # Start individual timer
                    task = asyncio.create_task(start_player_timer(room_code, uname))
                    room["timers"][uname] = task

            # ── Submit answer ───────────────────────────────────────────────
            elif msg_type == "submit_answer":
                if room["status"] != "playing":
                    continue
                player = room["players"].get(username)
                if not player or player["time_left"] <= 0:
                    continue

                answer = data.get("answer", "")
                q = room["current_questions"].get(username)
                if not q:
                    continue

                correct = normalize(answer) == normalize(q["answer"])

                if correct:
                    player["score"] += 1
                    # Deduct 10 seconds from opponents
                    for opp_name, opp_data in room["players"].items():
                        if opp_name != username:
                            opp_data["time_left"] = max(0, opp_data["time_left"] - 10)
                            await send_to(opp_data["ws"], {
                                "type": "time_penalty",
                                "seconds": 10,
                                "time_left": opp_data["time_left"],
                                "from": username
                            })
                            # Check if opponent ran out of time
                            if opp_data["time_left"] <= 0:
                                await send_to(opp_data["ws"], {
                                    "type": "game_over",
                                    "result": "lose",
                                    "reason": f"Waktu habis karena penalti dari {username}!"
                                })
                                await send_to(player["ws"], {
                                    "type": "game_over",
                                    "result": "win",
                                    "reason": f"Kamu menang! {opp_name} kehabisan waktu.",
                                    "score": player["score"]
                                })
                                room["status"] = "finished"
                                await save_game_result(room_code, room)

                    if room["status"] == "finished":
                        continue

                    # Send new question
                    new_q = generate_question()
                    room["current_questions"][username] = new_q
                    await send_to(player["ws"], {
                        "type": "correct",
                        "score": player["score"],
                        "time_left": player["time_left"],
                        "next_question": new_q["display"]
                    })
                else:
                    await send_to(websocket, {
                        "type": "wrong",
                        "correct_answer": q["answer"],
                        "question": q["display"]
                    })

            # ── Leave room ──────────────────────────────────────────────────
            elif msg_type == "leave":
                break

    except WebSocketDisconnect:
        pass
    finally:
        # Cleanup
        if username in room["players"]:
            del room["players"][username]
        if username in room["timers"]:
            room["timers"][username].cancel()
            del room["timers"][username]

        if room["status"] == "playing" and room["players"]:
            # Remaining player wins
            room["status"] = "finished"
            for uname, pdata in room["players"].items():
                await send_to(pdata["ws"], {
                    "type": "game_over",
                    "result": "win",
                    "reason": f"{username} meninggalkan game. Kamu menang!",
                    "score": pdata["score"]
                })
        elif not room["players"]:
            # Empty room, cleanup
            if room_code in rooms:
                del rooms[room_code]
            return

        # Notify remaining players
        if room_code in rooms and room["players"]:
            players = await get_player_list(room_code)
            await broadcast_room(room_code, {"type": "room_update", "players": players})

# ── Serve frontend ────────────────────────────────────────────────────────────
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=7171, reload=True)
