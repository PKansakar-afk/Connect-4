#!/usr/bin/env python3
"""
client_gui.py

Connect 4 GUI client for the networked Connect 4 game.
Uses the upgraded JSON-over-TCP protocol.

Run:
    python client_gui.py --host localhost --port 4000 --name Alice
"""
import socket
import threading
import json
import time
import tkinter as tk
from tkinter import messagebox
import sys
import uuid
import argparse

# --- Configuration ---
CELL_SIZE = 80
ROWS, COLS = 6, 7
PROTOCOL_VERSION = "1.1"
HEARTBEAT_INTERVAL = 8

# --- Logging ---
def log(msg):
    print(f"{time.strftime('%H:%M:%S')} CLIENT: {msg}", file=sys.stdout, flush=True)

# --- Protocol Utilities ---
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

# --- Networking ---
class GameClient:
    """
    This is the Client class from client_cli.py, renamed to GameClient
    and adapted to use GUI callbacks instead of print().
    """
    def __init__(self, host, port, name, on_update, on_status, on_game_over):
        self.host = host
        self.port = port
        self.name = name
        self.sock = None
        self.lock = threading.Lock()
        self.running = True
        self.seq_counter = 0
        self.heartbeat_started = False
        
        # GUI callbacks
        self.on_update = on_update
        self.on_status = on_status
        self.on_game_over = on_game_over # e.g., for messagebox

        # Session state (set by server)
        self.room_id = None
        self.symbol = None      # 'X' or 'O'
        self.player_id = None   # 1 or 2
        self.board = [[0]*COLS for _ in range(ROWS)] # Keep a local copy
        self.my_turn = False

    def next_seq(self):
        self.seq_counter += 1
        return self.seq_counter

    def start(self):
        """Called by GUI to connect."""
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            threading.Thread(target=self.listen_loop, daemon=True).start()
            self.send("HELLO", {"name": self.name}, seq=self.next_seq())
            self.on_status(f"Connecting as {self.name}...")
        except Exception as e:
            log(f"Connection error: {e}")
            self.on_status(f"Connection error: {e}")
            self.running = False

    def stop(self):
        """Called by GUI on close."""
        self.running = False
        try:
            self.send("LEAVE", {}, seq=self.next_seq())
        except:
            pass
        try:
            self.sock.close()
        except:
            pass

    def send(self, msg_type, payload=None, seq=None):
        if not self.running:
            return
        msg = make_msg(msg_type, payload, seq=seq)
        raw = dumps_line(msg)
        try:
            with self.lock:
                self.sock.sendall(raw)
            log(f"SENT -> {msg}")
        except Exception as e:
            log(f"Send error: {e}")
            self.running = False

    def listen_loop(self):
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
                    log(f"Received bad JSON: {line}")
                    continue
                log(f"RECV <- {msg}")
                self.handle_msg(msg)
        except Exception as e:
            log(f"Receive loop ended: {e}")
            self.running = False
        finally:
            self.on_status("Disconnected from server")
            self.running = False

    def heartbeat_loop(self):
        while self.running:
            try:
                self.send("PING", {}, seq=self.next_seq())
            except Exception as e:
                log(f"Heartbeat send error: {e}")
                self.running = False
            time.sleep(HEARTBEAT_INTERVAL)

    def handle_msg(self, msg):
        mtype = msg.get("type")
        payload = msg.get("payload", {})

        if mtype == "WELCOME":
            log(f"Connected to server version {payload.get('server_version')}")
            self.send("READY", {}, seq=self.next_seq())
            # Start heartbeat *after* handshake
            if not self.heartbeat_started:
                threading.Thread(target=self.heartbeat_loop, daemon=True).start()
                self.heartbeat_started = True
        
        elif mtype == "WAITING":
            self.on_status("Waiting for opponent...")

        elif mtype == "MATCHED":
            self.room_id = payload.get("room_id")
            self.symbol = payload.get("you")
            self.player_id = 1 if self.symbol == "X" else 2
            self.my_turn = bool(payload.get("first_turn", False))
            self.on_status(f"Matched! You are {self.symbol}. Opponent: {payload.get('opponent')}")

        elif mtype == "UPDATE":
            self.board = payload.get("board")
            next_turn = payload.get("next_turn")
            self.my_turn = (next_turn == self.player_id)
            # Trigger GUI update
            self.on_update(self.board) 
            self.on_status("Your turn!" if self.my_turn else "Opponent's turn...")

        elif mtype == "ACK":
            log(f"Server ACK for seq={payload.get('ack_seq')}")

        elif mtype == "INVALID":
            self.on_status(f"Invalid move: {payload.get('reason')}")

        elif mtype == "CHAT":
            log(f"[CHAT {payload.get('from')}] {payload.get('message')}")
            # We don't have a chat UI, but we could show it in status
            # self.on_status(f"CHAT: {payload.get('message')}")

        elif mtype == "PONG":
            log("Received PONG")

        elif mtype == "STATE":
            # Resync state, same as UPDATE
            self.board = payload.get("board")
            self.room_id = payload.get("room_id")
            self.my_turn = (payload.get("next_turn") == self.player_id)
            self.on_update(self.board)
            self.on_status("Resynced state. Your turn!" if self.my_turn else "Resynced state. Opponent's turn.")

        elif mtype == "WIN":
            winner = payload.get("winner")
            msg = "You WIN!" if (winner == self.player_id) else "You LOSE!"
            self.on_status(msg)
            self.on_game_over("Game Over", msg)
            self.running = False

        elif mtype == "DRAW":
            self.on_status("Game DRAW")
            self.on_game_over("Game Over", "It's a draw!")
            self.running = False

        elif mtype == "QUIT":
            reason = payload.get("reason", "opponent disconnected")
            self.on_status(f"Opponent quit: {reason}")
            self.on_game_over("Game Over", f"Opponent quit: {reason}")
            self.running = False

        elif mtype == "ERROR":
            self.on_status(f"Server error: {payload.get('reason')}")

        else:
            log(f"Unhandled message type: {mtype}")

    def move(self, col):
        """Called by GUI on_click."""
        if not self.room_id:
            self.on_status("You are not in a game.")
            return
        if not self.my_turn:
            self.on_status("It's not your turn!")
            return
        # Optimistically update status
        self.on_status("Sending move...")
        self.send("MOVE", {"col": col}, seq=self.next_seq())


# --- GUI ---
class Connect4GUI:
    def __init__(self, root, host, port, name):
        self.root = root
        self.client = GameClient(
            host, port, name, 
            self.update_board,  # on_update
            self.set_status,    # on_status
            self.show_game_over # on_game_over
        )
        
        self.canvas = tk.Canvas(root, width=COLS*CELL_SIZE, height=ROWS*CELL_SIZE, bg="#0066CC") # Darker blue
        self.canvas.pack()
        self.status_label = tk.Label(root, text="Connecting...", font=("Arial", 14), relief="sunken", bd=1)
        self.status_label.pack(pady=10, fill="x", padx=10)

        self.canvas.bind("<Button-1>", self.on_click)

        self.cells = [[None]*COLS for _ in range(ROWS)]
        self.draw_empty_board()

        # Start the client
        threading.Thread(target=self.client.start, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def draw_empty_board(self):
        for r in range(ROWS):
            for c in range(COLS):
                x1 = c*CELL_SIZE + 10
                y1 = r*CELL_SIZE + 10
                x2 = x1 + CELL_SIZE - 20
                y2 = y1 + CELL_SIZE - 20
                self.cells[r][c] = self.canvas.create_oval(x1, y1, x2, y2, fill="white", outline="black", width=2)

    def update_board(self, board):
        # This is called from the network thread, so we must schedule
        # the GUI update on the main thread using root.after()
        self.root.after(0, self._draw_board, board)

    def _draw_board(self, board):
        """Actual GUI drawing, runs in main thread."""
        try:
            for r in range(ROWS):
                for c in range(COLS):
                    val = board[r][c]
                    color = "white"
                    if val == 1:
                        color = "#FF4136" # Red
                    elif val == 2:
                        color = "#FFDC00" # Yellow
                    self.canvas.itemconfig(self.cells[r][c], fill=color)
        except Exception as e:
            log(f"Error updating board: {e}")

    def on_click(self, event):
        if not self.client.running:
            return
            
        col = event.x // CELL_SIZE
        if 0 <= col < COLS:
            if self.client.my_turn:
                self.client.move(col)
            else:
                self.set_status("Not your turn!")

    def set_status(self, text):
        # This is called from the network thread, schedule it
        self.root.after(0, self._set_status_text, text)
        
    def _set_status_text(self, text):
        """Actual GUI update, runs in main thread."""
        try:
            self.status_label.config(text=text)
        except Exception as e:
            log(f"Error setting status: {e}")

    def show_game_over(self, title, message):
        # This is called from the network thread, schedule it
        self.root.after(0, lambda: messagebox.showinfo(title, message))

    def on_close(self):
        self.client.stop()
        self.root.destroy()

# --- Run GUI ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--name", default="player")
    args = parser.parse_args()

    root = tk.Tk()
    root.title(f"Connect 4 - {args.name}")
    root.resizable(False, False)
    app = Connect4GUI(root, args.host, args.port, args.name)
    root.mainloop()