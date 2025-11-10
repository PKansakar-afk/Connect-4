#!/usr/bin/env python3
"""

Features:
 - JSON-over-TCP protocol (newline-delimited) with versioning, seq, request_id, timestamp
 - Handshake: HELLO -> WELCOME -> READY -> MATCHED
 - Matchmaker / rooms: pairs waiting players into game sessions (multiple concurrent games)
 - Heartbeat: server expects PING from clients; responds with PONG; detects timeouts
 - ACKs: server sends ACK for important messages (e.g., MOVE)
 - CHAT: message multiplexing (chat + game)
 - RESYNC/STATE: on demand state resend
 - Logging of all received/sent messages (with timestamps and direction)
 - Graceful handling of disconnects
"""

import socket
import threading
import json
import time
import uuid
from queue import Queue, SimpleQueue

# --- Configuration ---
HOST = "0.0.0.0"
PORT = 4000
PROTOCOL_VERSION = "1.1"
HEARTBEAT_INTERVAL = 10      # seconds - how often clients should PING
HEARTBEAT_TIMEOUT = 25       # seconds - server considers client dead after this
MATCHMAKER_BATCH = 2         # pair 2 players per game
MAX_LISTEN = 100

# --- Utilities ---
def now_ts():
    return time.time()

def make_msg(msg_type, payload=None, seq=None):
    """Create the standardized message wrapper."""
    return {
        "version": PROTOCOL_VERSION,
        "seq": seq if seq is not None else None,
        "request_id": str(uuid.uuid4()),
        "type": msg_type,
        "timestamp": now_ts(),
        "payload": payload or {}
    }

def dumps_line(obj):
    """Serialize message to JSON text terminated by newline."""
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")

def log_server(text):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SERVER: {text}", flush=True)

# --- Server Objects ---
class ClientConnection:
    """
    Represents a connected client.
    Holds networking socket, addressing, metadata, and last heartbeat time.
    The server uses this object to send messages to the client and manage state.
    """
    def __init__(self, conn: socket.socket, addr):
        self.conn = conn
        self.addr = addr
        self.lock = threading.Lock()   # lock for sending
        self.name = None               # provided in HELLO
        self.player_uuid = str(uuid.uuid4())  # unique identifier for reconnects (not persisted)
        self.room_id = None            # which room/game this client belongs to
        self.player_id = None          # 1 or 2 when in a game
        self.last_ping = now_ts()
        self.alive = True
        self.seq_counter = 0           # per-client seq generator for outgoing messages

    def next_seq(self):
        self.seq_counter += 1
        return self.seq_counter

    def send(self, msg_type, payload=None, seq=None):
        """Send a wrapped message to this client (thread-safe)."""
        try:
            if not self.alive:
                return
            msg = make_msg(msg_type, payload=payload, seq=seq)
            raw = dumps_line(msg)
            with self.lock:
                self.conn.sendall(raw)
            log_server(f"SENT to {self.addr} -> {msg}")
        except Exception as e:
            log_server(f"Send error to {self.addr}: {e}")
            self.alive = False
            # closing handled by caller

    def recv_line(self):
        """
        Read bytes until newline. Returns parsed JSON dict or None on disconnect/error.
        Uses socket.recv to avoid blocking on makefile buffering issues.
        """
        try:
            buf = bytearray()
            while True:
                b = self.conn.recv(1)
                if not b:
                    return None
                if b == b'\n':
                    break
                buf.extend(b)
            line = buf.decode("utf-8").strip()
            if not line:
                return None
            log_server(f"RECV from {self.addr} <- {line}")
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                # Inform client about bad JSON
                self.send("ERROR", {"reason": "bad json"})
                return None
        except Exception as e:
            log_server(f"Read error from {self.addr}: {e}")
            return None

    def close(self):
        self.alive = False
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.conn.close()
        except:
            pass

# --- Game logic utilities ---
ROWS = 6
COLS = 7

def init_board():
    return [[0 for _ in range(COLS)] for _ in range(ROWS)]

def board_drop(board, col, pid):
    """
    Simulate dropping a piece into column 'col' for player pid (1 or 2).
    Return (row_index, board) or (None, board) if column full.
    """
    for r in range(ROWS - 1, -1, -1):
        if board[r][col] == 0:
            board[r][col] = pid
            return r, board
    return None, board

def check_win(board, r, c, pid):
    """Check 4-in-a-row in 4 directions from (r,c)."""
    directions = [(0,1), (1,0), (1,1), (1,-1)]
    for dr, dc in directions:
        count = 1
        rr, cc = r+dr, c+dc
        while 0 <= rr < ROWS and 0 <= cc < COLS and board[rr][cc] == pid:
            count += 1
            rr += dr; cc += dc
        rr, cc = r-dr, c-dc
        while 0 <= rr < ROWS and 0 <= cc < COLS and board[rr][cc] == pid:
            count += 1
            rr -= dr; cc -= dc
        if count >= 4:
            return True
    return False

# --- Room and match management ---
class Room:
    """
    Room holds two players and the game state.
    """
    def __init__(self, room_id, p1: ClientConnection, p2: ClientConnection):
        self.room_id = room_id
        self.players = {1: p1, 2: p2}
        p1.room_id = room_id; p2.room_id = room_id
        p1.player_id = 1; p2.player_id = 2
        self.board = init_board()
        self.current = 1   # whose turn (1 or 2)
        self.lock = threading.Lock()
        self.started_at = now_ts()
        self.ended = False

    def other(self, pid):
        return 1 if pid == 2 else 2

    def broadcast(self, msg_type, payload=None, seq=None):
        """Send same message to both players."""
        for p in self.players.values():
            p.send(msg_type, payload=payload, seq=seq)

    def send_update(self):
        payload = {"board": self.board, "next_turn": self.current, "room_id": self.room_id}
        self.broadcast("UPDATE", payload=payload)

    def end_game(self):
        self.ended = True
        try:
            for p in self.players.values():
                p.room_id = None
                p.player_id = None
                # Leave connections open; client can request RESYNC or new match
        except:
            pass

# Global server state
waiting_queue = Queue()      # queue of ClientConnection (waiting for match)
rooms = {}                   # room_id -> Room
clients_lock = threading.Lock()
active_clients = set()       # track all clients for heartbeats/scanning

# --- Matchmaker Thread ---
def matchmaker_loop():
    """Continuously pair waiting players into rooms."""
    while True:
        p1 = waiting_queue.get()   # blocks until an item available
        # find next available player (skip dead ones)
        while not p1.alive:
            # discard and get next
            p1 = waiting_queue.get()
        # block until second player available
        p2 = waiting_queue.get()
        while not p2.alive:
            p2 = waiting_queue.get()
        # Create room
        room_id = str(uuid.uuid4())
        room = Room(room_id, p1, p2)
        rooms[room_id] = room
        log_server(f"Matched {p1.addr} and {p2.addr} into room {room_id}")
        # Inform both clients (MATCHED info includes session/role)
        p1.send("MATCHED", {"room_id": room_id, "opponent": p2.name or str(p2.addr), "you": "X", "first_turn": True}, seq=p1.next_seq())
        p2.send("MATCHED", {"room_id": room_id, "opponent": p1.name or str(p1.addr), "you": "O", "first_turn": False}, seq=p2.next_seq())
        # send initial UPDATE (empty board)
        room.send_update()
        # spawn a dedicated thread to run game event loop / monitoring (optional)
        threading.Thread(target=room_event_loop, args=(room,), daemon=True).start()

def room_event_loop(room: Room):
    """
    Monitors the room for timeouts or to perform room-wide housekeeping.
    (In this simple server, gameplay is event-driven by client messages.)
    """
    while not room.ended:
        time.sleep(1)
        # If either player has died/disconnected, inform the other and end
        for pid, client in room.players.items():
            if not client.alive:
                other = room.players[room.other(pid)]
                try:
                    other.send("QUIT", {"reason": "opponent disconnected"})
                except:
                    pass
                room.end_game()
                rooms.pop(room.room_id, None)
                log_server(f"Room {room.room_id} ended due to disconnect.")
                return
        # no other periodic actions currently; heartbeat handled globally

# --- Heartbeat monitor thread (server-side) ---
def heartbeat_monitor():
    """
    Periodically check all active clients for liveness (based on last_ping).
    If a client hasn't pinged in HEARTBEAT_TIMEOUT seconds, mark it dead and clean up.
    """
    while True:
        time.sleep(5)
        now = now_ts()
        with clients_lock:
            dead_clients = []
            for c in list(active_clients):
                if not c.alive:
                    dead_clients.append(c)
                    continue
                if now - c.last_ping > HEARTBEAT_TIMEOUT:
                    log_server(f"Client {c.addr} timed out (last_ping={c.last_ping})")
                    c.alive = False
                    dead_clients.append(c)
            # Clean up dead clients: remove from waiting queue (can't remove from queue easily),
            # leave rooms (room_event_loop handles), and close sockets.
            for dc in dead_clients:
                try:
                    dc.close()
                except:
                    pass
                try:
                    active_clients.discard(dc)
                except:
                    pass

# --- Client handler ---
def handle_client(conn, addr):
    """
    Main per-client thread: handles handshake, places client in waiting queue
    and then continues to read incoming messages (PING, MOVE, CHAT, RESYNC, etc).
    """
    client = ClientConnection(conn, addr)
    with clients_lock:
        active_clients.add(client)
    log_server(f"New connection from {addr}")

    # 1) Expect HELLO for handshake
    hello = client.recv_line()
    if not hello or hello.get("type") != "HELLO":
        client.send("ERROR", {"reason": "expected HELLO"})
        client.close()
        with clients_lock:
            active_clients.discard(client)
        return
    # record name (if provided)
    client.name = hello.get("payload", {}).get("name") or f"{addr}"
    # Respond with WELCOME (with server version and assigned client UUID)
    client.send("WELCOME", {"server_version": PROTOCOL_VERSION, "player_uuid": client.player_uuid}, seq=client.next_seq())

    # Wait for READY from client (client should call READY after it's ready to be matched)
    ready = client.recv_line()
    if not ready or ready.get("type") != "READY":
        client.send("ERROR", {"reason": "expected READY"})
        client.close()
        with clients_lock:
            active_clients.discard(client)
        return

    # Client is ready -> add to waiting queue for matchmaker
    waiting_queue.put(client)
    client.send("WAITING", {"info": "waiting for opponent"}, seq=client.next_seq())
    log_server(f"Client {client.addr} ({client.name}) queued for matchmaker")

    # Enter main loop to process messages from this client until disconnect
    while client.alive:
        msg = client.recv_line()
        if msg is None:
            # disconnected or decode error
            break
        mtype = msg.get("type")
        payload = msg.get("payload", {})
        seq = msg.get("seq")

        # Update heartbeat time if we see PING-type heartbeat
        if mtype == "PING":
            client.last_ping = now_ts()
            # reply with PONG
            client.send("PONG", {}, seq=client.next_seq())
            continue

        # Allow client to request server-side resync of state for its room
        if mtype == "RESYNC":
            # if client in a room, respond with STATE
            if client.room_id and client.room_id in rooms:
                room = rooms[client.room_id]
                state_payload = {"board": room.board, "next_turn": room.current, "room_id": room.room_id}
                client.send("STATE", state_payload, seq=client.next_seq())
            else:
                client.send("ERROR", {"reason": "not in a room"}, seq=client.next_seq())
            continue

        # Allow client to gracefully leave
        if mtype == "LEAVE":
            client.send("BYE", {"info": "left"}, seq=client.next_seq())
            client.close()
            break

        # CHAT messages: if in a room, forward to other player
        if mtype == "CHAT":
            text = payload.get("message", "")
            if client.room_id and client.room_id in rooms:
                room = rooms[client.room_id]
                other_id = room.other(client.player_id)
                other = room.players.get(other_id)
                # annotate chat with sender name
                chat_payload = {"from": client.name, "message": text}
                # send chat to both so both UI can display (or to other player only if desired)
                room.broadcast("CHAT", chat_payload, seq=client.next_seq())
            else:
                client.send("ERROR", {"reason": "not in a room"}, seq=client.next_seq())
            continue

        # MOVE messages: only processed when client is in a room
        if mtype == "MOVE":
            if not client.room_id or client.room_id not in rooms:
                client.send("ERROR", {"reason": "not in a room"}, seq=client.next_seq())
                continue
            room = rooms[client.room_id]
            with room.lock:
                if room.ended:
                    client.send("ERROR", {"reason": "game ended"}, seq=client.next_seq())
                    continue
                # Check turn
                if client.player_id != room.current:
                    client.send("INVALID", {"reason": "not your turn"}, seq=client.next_seq())
                    continue
                col = payload.get("col")
                if not isinstance(col, int) or not (0 <= col < COLS):
                    client.send("INVALID", {"reason": "invalid column"}, seq=client.next_seq())
                    continue
                row, _ = board_drop(room.board, col, client.player_id)
                if row is None:
                    client.send("INVALID", {"reason": "column full"}, seq=client.next_seq())
                    continue
                # valid move: send ACK referencing the incoming seq (application-level ack)
                client.send("ACK", {"ack_seq": msg.get("seq")}, seq=client.next_seq())
                log_server(f"Room {room.room_id}: Player {client.player_id} placed at {row},{col}")
                # Check win
                if check_win(room.board, row, col, client.player_id):
                    # broadcast final UPDATE then WIN
                    room.send_update()
                    room.broadcast("WIN", {"winner": client.player_id}, seq=client.next_seq())
                    room.end_game()
                    rooms.pop(room.room_id, None)
                    log_server(f"Room {room.room_id}: Player {client.player_id} wins")
                    continue
                # Check draw (top row all filled)
                if all(room.board[0][c] != 0 for c in range(COLS)):
                    room.send_update()
                    room.broadcast("DRAW", {}, seq=client.next_seq())
                    room.end_game()
                    rooms.pop(room.room_id, None)
                    log_server(f"Room {room.room_id}: Draw")
                    continue
                # otherwise switch turn and broadcast UPDATE
                room.current = room.other(room.current)
                room.send_update()
            continue

        # Unknown message types
        client.send("ERROR", {"reason": f"unknown message type {mtype}"}, seq=client.next_seq())

    # End of per-client loop -> clean up
    log_server(f"Connection {client.addr} closed")
    client.close()
    # If client was in waiting queue, it will be skipped by matchmaker when popped (we don't remove from queue)
    with clients_lock:
        active_clients.discard(client)
    # Leave room: room_event_loop will detect and notify other player

# --- Main server start ---
def main():
    # Start background matchmaker
    threading.Thread(target=matchmaker_loop, daemon=True).start()
    threading.Thread(target=heartbeat_monitor, daemon=True).start()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((HOST, PORT))
    s.listen(MAX_LISTEN)
    log_server(f"Listening on {HOST}:{PORT}")

    try:
        while True:
            conn, addr = s.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log_server("Shutting down server")
    finally:
        try:
            s.close()
        except:
            pass

if __name__ == "__main__":
    main()