#!/usr/bin/env python3
"""
client_gui.py

Connect 4 GUI client for the networked Connect 4 game.

Uses the same JSON-over-TCP protocol as client_cli.py.

Run:
    python client_gui.py --host localhost --port 4000 --name Alice
"""
import socket
import threading
import json
import time
import tkinter as tk
from tkinter import messagebox

CELL_SIZE = 80
ROWS, COLS = 6, 7

# --- Logging ---
import sys
def log(msg):
    print(f"{time.strftime('%H:%M:%S')} CLIENT: {msg}", file=sys.stdout, flush=True)

# --- Networking ---
class GameClient:
    def __init__(self, host, port, name, on_update, on_status):
        self.host = host
        self.port = port
        self.name = name
        self.on_update = on_update
        self.on_status = on_status
        self.sock = None
        self.running = True
        self.symbol = None
        self.my_turn = False
        self.board = [[0]*COLS for _ in range(ROWS)]

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        threading.Thread(target=self._listen, daemon=True).start()
        self._send({"type":"HELLO","payload":{"name":self.name}})

    def stop(self):
        self.running = False
        try:
            self._send({"type":"QUIT"})
        except:
            pass
        try:
            self.sock.close()
        except:
            pass

    def _send(self, obj):
        line = json.dumps(obj) + "\n"
        try:
            self.sock.sendall(line.encode('utf-8'))
            log(f"SENT -> {line.strip()}")
        except Exception as e:
            log(f"Send error: {e}")
            self.stop()

    def move(self, col):
        if self.my_turn:
            self._send({"type":"MOVE","payload":{"col":col}})

    def _listen(self):
        try:
            while self.running:
                buf = bytearray()
                while True:
                    ch = self.sock.recv(1)
                    if not ch:
                        raise ConnectionError("server closed")
                    if ch == b'\n':
                        break
                    buf.extend(ch)
                line = buf.decode('utf-8').strip()
                if not line:
                    continue
                log(f"RECV <- {line}")
                msg = json.loads(line)
                self._handle(msg)
        except Exception as e:
            log(f"Network error: {e}")
        finally:
            self.on_status("Disconnected from server")
            self.running = False

    def _handle(self, msg):
        t = msg.get('type')
        p = msg.get('payload', {})
        if t == "WAIT":
            self.on_status("Waiting for opponent...")
        elif t == "START":
            self.symbol = p.get('you')
            self.my_turn = bool(p.get('first_turn', False))
            self.on_status(f"Game started! You are {self.symbol}")
        elif t == "UPDATE":
            self.board = p.get('board', self.board)
            next_turn = p.get('next_turn')
            self.my_turn = (next_turn == (1 if self.symbol=="X" else 2))
            self.on_update(self.board)
            self.on_status("Your turn!" if self.my_turn else "Opponent's turn...")
        elif t == "INVALID":
            self.on_status(f"Invalid move: {p.get('reason')}")
        elif t == "WIN":
            winner = p.get('winner')
            you = (self.symbol == "X" and winner == 1) or (self.symbol == "O" and winner == 2)
            msg = "You WIN!" if you else "You LOSE!"
            self.on_status(msg)
            messagebox.showinfo("Game Over", msg)
            self.running = False
        elif t == "DRAW":
            self.on_status("Draw!")
            messagebox.showinfo("Game Over", "It's a draw!")
            self.running = False
        elif t == "QUIT":
            self.on_status("Opponent disconnected.")
            messagebox.showinfo("Disconnected", "Opponent quit the game.")
            self.running = False
        elif t == "ERROR":
            self.on_status(f"Error: {p.get('reason')}")
        else:
            log(f"Unknown message type: {t}")

# --- GUI ---
class Connect4GUI:
    def __init__(self, root, host, port, name):
        self.root = root
        self.client = GameClient(host, port, name, self.update_board, self.set_status)
        self.canvas = tk.Canvas(root, width=COLS*CELL_SIZE, height=ROWS*CELL_SIZE, bg="blue")
        self.canvas.pack()
        self.status_label = tk.Label(root, text="Connecting...", font=("Arial", 14))
        self.status_label.pack(pady=10)

        self.canvas.bind("<Button-1>", self.on_click)

        self.cells = [[None]*COLS for _ in range(ROWS)]
        self.draw_empty_board()

        threading.Thread(target=self.client.start, daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def draw_empty_board(self):
        for r in range(ROWS):
            for c in range(COLS):
                x1 = c*CELL_SIZE + 10
                y1 = r*CELL_SIZE + 10
                x2 = x1 + CELL_SIZE - 20
                y2 = y1 + CELL_SIZE - 20
                self.cells[r][c] = self.canvas.create_oval(x1, y1, x2, y2, fill="white")

    def update_board(self, board):
        for r in range(ROWS):
            for c in range(COLS):
                val = board[r][c]
                color = "white"
                if val == 1:
                    color = "red"
                elif val == 2:
                    color = "yellow"
                self.canvas.itemconfig(self.cells[r][c], fill=color)

    def on_click(self, event):
        col = event.x // CELL_SIZE
        if 0 <= col < COLS:
            if self.client.my_turn:
                self.client.move(col)
            else:
                self.set_status("Not your turn!")

    def set_status(self, text):
        self.status_label.config(text=text)

    def on_close(self):
        self.client.stop()
        self.root.destroy()

# --- Run GUI ---
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=4000)
    parser.add_argument("--name", default="player")
    args = parser.parse_args()

    root = tk.Tk()
    root.title(f"Connect 4 - {args.name}")
    app = Connect4GUI(root, args.host, args.port, args.name)
    root.mainloop()