#!/usr/bin/env python3
"""

Features:
 - Uses the same upgraded protocol as server.py
 - Performs handshake: HELLO -> WELCOME -> READY
 - Heartbeat: sends PING periodically and expects PONG (logs)
 - Accepts user input from CLI for MOVE and CHAT
 - Handles ACKs, UPDATE, MATCHED, WIN, DRAW, RESYNC, etc.
 - Logs all messages sent/received with timestamps
 - Provides simple commands:
     /chat your message    -> send chat
     /resync               -> request state
     /leave                -> leave
     /help                 -> show commands
     0..6                  -> send MOVE to column
"""

import socket
import threading
import json
import time
import sys
import uuid

# --- Configuration ---
SERVER_HOST = "localhost"
SERVER_PORT = 4000
PROTOCOL_VERSION = "1.1"
HEARTBEAT_INTERVAL = 8  # client PING interval seconds

# --- Utilities ---
def now_ts():
    return time.time()

def make_msg(msg_type, payload=None, seq=None):
    return {
        "version": PROTOCOL_VERSION,
        "seq": seq if seq is not None else None,
        "request_id": str(uuid.uuid4()),
        "type": msg_type,
        "timestamp": now_ts(),
        "payload": payload or {}
    }

def dumps_line(obj):
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")

def log_client(text):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} CLIENT: {text}", flush=True)

# --- Client class ---
class Client:
    def __init__(self, host, port, name="player"):
        self.host = host
        self.port = port
        self.name = name
        self.sock = None
        self.lock = threading.Lock()
        self.running = True
        self.seq_counter = 0
        self.heartbeat_started = False
        # session state
        self.room_id = None
        self.symbol = None   # 'X' or 'O'
        self.player_id = None
        self.board = None
        self.my_turn = False

    def next_seq(self):
        self.seq_counter += 1
        return self.seq_counter

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        # start listener thread
        threading.Thread(target=self.listen_loop, daemon=True).start()
        # handshake: HELLO -> wait for WELCOME -> send READY
        self.send("HELLO", {"name": self.name}, seq=self.next_seq())
        # Wait for WELCOME asynchronously in listen_loop; send READY after we see WELCOME
        # We'll wait with a simple sleep loop here but not strictly necessary.
        # Start heartbeat thread
        # threading.Thread(target=self.heartbeat_loop, daemon=True).start()

    def send(self, msg_type, payload=None, seq=None):
        msg = make_msg(msg_type, payload, seq=seq)
        raw = dumps_line(msg)
        try:
            with self.lock:
                self.sock.sendall(raw)
            log_client(f"SENT -> {msg}")
        except Exception as e:
            log_client(f"Send error: {e}")
            self.running = False

    def listen_loop(self):
        # read messages line by line
        try:
            while self.running:
                buf = bytearray()
                while True:
                    b = self.sock.recv(1)
                    if not b:
                        raise ConnectionError("server closed")
                    if b == b"\n":
                        break
                    buf.extend(b)
                line = buf.decode("utf-8").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log_client("Received bad JSON")
                    continue
                log_client(f"RECV <- {msg}")
                self.handle_msg(msg)
        except Exception as e:
            log_client(f"Receive loop ended: {e}")
            self.running = False
        finally:
            try:
                self.sock.close()
            except:
                pass

    def heartbeat_loop(self):
        """Periodically send PING to server."""
        while self.running:
            try:
                self.send("PING", {}, seq=self.next_seq())
            except:
                pass
            time.sleep(HEARTBEAT_INTERVAL)

    def handle_msg(self, msg):
        mtype = msg.get("type")
        payload = msg.get("payload", {})
        # Welcome: server handshake response -> send READY
        if mtype == "WELCOME":
            server_version = payload.get("server_version")
            log_client(f"Connected to server version {server_version}")
            # after WELCOME, send READY
            self.send("READY", {}, seq=self.next_seq())
            # Now that the handshake is done, start the heartbeat
            if not self.heartbeat_started:
                threading.Thread(target=self.heartbeat_loop, daemon=True).start()
                self.heartbeat_started = True
            return
        if mtype == "WAITING":
            print("[server] Waiting for opponent...")
            return
        if mtype == "MATCHED":
            self.room_id = payload.get("room_id")
            self.symbol = payload.get("you")
            # Set numeric player_id (X -> 1, O -> 2)
            self.player_id = 1 if self.symbol == "X" else 2
            self.my_turn = bool(payload.get("first_turn", False))
            print(f"Matched! Room {self.room_id} You are {self.symbol}. Your turn? {self.my_turn}")
            return
        if mtype == "UPDATE":
            self.board = payload.get("board")
            next_turn = payload.get("next_turn")
            self.my_turn = (next_turn == self.player_id)
            self.print_board()
            if self.my_turn:
                print("It's your turn. Enter column 0-6 or /help")
            return
        if mtype == "ACK":
            ack_seq = payload.get("ack_seq")
            # we received an app-level ack for our previous message
            log_client(f"Server ACK for seq={ack_seq}")
            return
        if mtype == "INVALID":
            reason = payload.get("reason")
            print(f"Invalid: {reason}")
            return
        if mtype == "CHAT":
            sender = payload.get("from", "unknown")
            text = payload.get("message", "")
            print(f"[CHAT {sender}] {text}")
            return
        if mtype == "PONG":
            # server responded to our PING
            # we might want to log latency etc, here just note
            log_client("Received PONG")
            return
        if mtype == "STATE":
            # resync board state
            self.board = payload.get("board")
            self.room_id = payload.get("room_id")
            self.my_turn = (payload.get("next_turn") == self.player_id)
            print("Resynced state from server.")
            self.print_board()
            return
        if mtype == "WIN":
            winner = payload.get("winner")
            if winner == self.player_id:
                print("You WIN!")
            else:
                print("You LOSE!")
            self.running = False
            return
        if mtype == "DRAW":
            print("Game DRAW")
            self.running = False
            return
        if mtype == "QUIT":
            reason = payload.get("reason", "")
            print(f"Opponent quit: {reason}")
            self.running = False
            return
        if mtype == "ERROR":
            print(f"Server error: {payload.get('reason')}")
            return
        # unknown types fallthrough
        log_client(f"Unhandled message type: {mtype}")

    def print_board(self):
        if not self.board:
            return
        print("\nBoard:")
        for r in range(len(self.board)):
            row = self.board[r]
            def s(v):
                if v == 0: return '.'
                if v == 1: return 'X'
                if v == 2: return 'O'
            print('|' + ' '.join(s(x) for x in row) + '|')
        print(' 0 1 2 3 4 5 6\n')

    # --- User commands ---
    def send_move(self, col):
        if not isinstance(col, int) or not (0 <= col <= 6):
            print("Column must be 0..6")
            return
        if not self.room_id:
            print("You are not in a room / match.")
            return
        if not self.my_turn:
            print("Not your turn.")
            return
        self.send("MOVE", {"col": col}, seq=self.next_seq())

    def send_chat(self, text):
        if not self.room_id:
            print("You are not in a room; chat goes nowhere.")
            return
        self.send("CHAT", {"message": text}, seq=self.next_seq())

    def resync(self):
        self.send("RESYNC", {"room_id": self.room_id}, seq=self.next_seq())

    def leave(self):
        self.send("LEAVE", {}, seq=self.next_seq())
        self.running = False

# --- CLI loop ---
def repl_loop(client: Client):
    print("Commands: /chat <text>, /resync, /leave, /help, or enter column 0..6 to move.")
    while client.running:
        try:
            line = input("> ").strip()
        except EOFError:
            print("Exiting.")
            client.leave()
            break
        if not line:
            continue
        if line.startswith("/"):
            # special commands
            parts = line.split(" ", 1)
            cmd = parts[0].lower()
            arg = parts[1] if len(parts) > 1 else ""
            if cmd == "/chat":
                if not arg:
                    print("Usage: /chat your message")
                    continue
                client.send_chat(arg)
            elif cmd == "/resync":
                client.resync()
            elif cmd == "/leave":
                client.leave()
            elif cmd == "/help":
                print("Commands: /chat <text>, /resync, /leave, /help, or enter column 0..6")
            else:
                print("Unknown command. Type /help")
        else:
            # try to parse as move
            try:
                col = int(line)
                client.send_move(col)
            except ValueError:
                print("Not a command or valid column. Type /help")

# --- Main ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=SERVER_HOST)
    parser.add_argument("--port", type=int, default=SERVER_PORT)
    parser.add_argument("--name", default="player")
    args = parser.parse_args()

    c = Client(args.host, args.port, args.name)
    try:
        c.connect()
    except Exception as e:
        log_client(f"Connection error: {e}")
        sys.exit(1)

    try:
        repl_loop(c)
    except KeyboardInterrupt:
        print("Interrupted, leaving...")
        c.leave()
    # give some time for graceful LEAVE
    time.sleep(0.5)
    sys.exit(0)
