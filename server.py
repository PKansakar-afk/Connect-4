#!/usr/bin/env python3
"""
server.py

Simple Connect 4 server using TCP sockets and line-delimited JSON messages.

Run:
    python server.py --host 0.0.0.0 --port 4000

Behavior:
 - Accepts clients that send a HELLO message.
 - Pairs clients into games (matchmaker).
 - Maintains authoritative game state, validates moves, broadcasts UPDATE/WIN/DRAW.
 - Logs every sent and received JSON line with timestamps.
"""
import socket
import threading
import json
import time
from queue import Queue

HOST = '0.0.0.0'
PORT = 4000

LOG_LOCK = threading.Lock()

def log(msg):
    with LOG_LOCK:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} SERVER: {msg}")

class ClientHandler:
    def __init__(self, conn, addr):
        self.conn = conn
        self.addr = addr
        # use buffered text I/O to make line reading easier
        self.f = conn.makefile(mode='rwb', buffering=0)
        # we'll use text encoding/decoding manually
        self.name = None
        self.player_id = None
        self.game = None
        self.lock = threading.Lock()
        self.closed = False

    def send(self, obj):
        try:
            line = json.dumps(obj, separators=(',', ':')) + '\n'
            with self.lock:
                self.conn.sendall(line.encode('utf-8'))
            log(f"SENT to {self.addr} -> {line.strip()}")
        except Exception as e:
            log(f"Error sending to {self.addr}: {e}")
            self.close()

    def recv_line(self):
        try:
            # read raw bytes until newline
            buf = bytearray()
            while True:
                ch = self.conn.recv(1)
                if not ch:
                    return None
                if ch == b'\n':
                    break
                buf.extend(ch)
            line = buf.decode('utf-8').strip()
            if not line:
                return None
            log(f"RECV from {self.addr} <- {line}")
            try:
                return json.loads(line)
            except json.JSONDecodeError as e:
                log(f"JSON decode error from {self.addr}: {e}")
                return {"type":"ERROR","payload":{"reason":"bad json"}}
        except Exception as e:
            log(f"Error reading from {self.addr}: {e}")
            return None

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except:
            pass
        try:
            self.conn.close()
        except:
            pass

class GameSession:
    ROWS = 6
    COLS = 7

    def __init__(self, p1: ClientHandler, p2: ClientHandler):
        self.board = [[0]*self.COLS for _ in range(self.ROWS)]
        self.players = {1: p1, 2: p2}
        p1.player_id = 1
        p2.player_id = 2
        p1.game = self
        p2.game = self
        self.current = 1
        self.lock = threading.Lock()
        log(f"Game created between {p1.addr} and {p2.addr}")

    def start(self):
        # Notify players with START (includes identities and who moves first)
        self.players[1].send({"type":"START","payload":{"opponent":str(self.players[2].addr),"you":"X","first_turn":True}})
        self.players[2].send({"type":"START","payload":{"opponent":str(self.players[1].addr),"you":"O","first_turn":False}})
        # spawn threads to listen to players
        threading.Thread(target=self._listen, args=(1,), daemon=True).start()
        threading.Thread(target=self._listen, args=(2,), daemon=True).start()
        # Send initial UPDATE (empty board)
        self.broadcast_update()

    def _listen(self, pid):
        client = self.players[pid]
        while True:
            msg = client.recv_line()
            if msg is None:
                # client disconnected
                self._handle_quit(pid)
                return
            if not isinstance(msg, dict):
                # malformed message
                client.send({"type":"ERROR","payload":{"reason":"malformed message"}})
                continue
            t = msg.get('type')
            if t == 'MOVE':
                col = msg.get('payload',{}).get('col')
                self._handle_move(pid, col)
            elif t == 'QUIT':
                self._handle_quit(pid)
                return
            elif t == 'HELLO':
                # ignore HELLO if already in game
                client.send({"type":"ERROR","payload":{"reason":"already in game"}})
            else:
                client.send({"type":"ERROR","payload":{"reason":"unknown message type"}})

    def _handle_move(self, pid, col):
        with self.lock:
            if pid != self.current:
                self.players[pid].send({"type":"INVALID","payload":{"reason":"not your turn"}})
                return
            # Validate column
            if not isinstance(col, int) or col < 0 or col >= self.COLS:
                self.players[pid].send({"type":"INVALID","payload":{"reason":"invalid column"}})
                return
            # Find lowest empty row
            row = None
            for r in range(self.ROWS-1, -1, -1):
                if self.board[r][col] == 0:
                    row = r
                    break
            if row is None:
                self.players[pid].send({"type":"INVALID","payload":{"reason":"column full"}})
                return
            # Place disc
            self.board[row][col] = pid
            self.players[pid].send({"type":"ACK","payload":{"info":"move accepted"}})
            log(f"Player {pid} placed at row {row}, col {col}")
            # Check for win
            if self._check_win(row, col, pid):
                self.broadcast_update()
                self.broadcast({"type":"WIN","payload":{"winner":pid}})
                self._end()
                return
            # Check for draw (top row full)
            if all(self.board[0][c] != 0 for c in range(self.COLS)):
                self.broadcast_update()
                self.broadcast({"type":"DRAW"})
                self._end()
                return
            # Switch turns
            self.current = 1 if self.current == 2 else 2
            self.broadcast_update()

    def broadcast(self, obj):
        for p in self.players.values():
            try:
                p.send(obj)
            except Exception:
                pass

    def broadcast_update(self):
        obj = {"type":"UPDATE","payload":{"board":self.board,"next_turn":self.current}}
        self.broadcast(obj)

    def _check_win(self, row, col, pid):
        # check 4 directions
        directions = [(0,1),(1,0),(1,1),(1,-1)]
        for dr,dc in directions:
            cnt = 1
            # forward
            r,c = row+dr, col+dc
            while 0<=r<self.ROWS and 0<=c<self.COLS and self.board[r][c]==pid:
                cnt += 1; r += dr; c += dc
            # backward
            r,c = row-dr, col-dc
            while 0<=r<self.ROWS and 0<=c<self.COLS and self.board[r][c]==pid:
                cnt += 1; r -= dr; c -= dc
            if cnt >= 4:
                return True
        return False

    def _handle_quit(self, pid):
        other = 1 if pid==2 else 2
        try:
            self.players[other].send({"type":"QUIT","payload":{"reason":"opponent disconnected"}})
        except Exception:
            pass
        self._end()

    def _end(self):
        try:
            for p in self.players.values():
                p.close()
        except Exception:
            pass

# --- Server main loop ---
waiting = Queue()

def client_thread(conn, addr):
    handler = ClientHandler(conn, addr)
    log(f"New connection from {addr}")
    # Expect HELLO first
    msg = handler.recv_line()
    if not msg or msg.get('type') != 'HELLO':
        handler.send({"type":"ERROR","payload":{"reason":"expected HELLO"}})
        handler.close()
        return
    handler.name = msg.get('payload',{}).get('name','')
    handler.send({"type":"WELCOME","payload":{"player_id":None}})
    log(f"{addr} said HELLO name={handler.name}")
    # Put on waiting queue and notify client to wait
    waiting.put(handler)
    handler.send({"type":"WAIT"})
    # The client's socket stays open; GameSession will take over when matched.

def matchmaker():
    while True:
        p1 = waiting.get()
        p2 = waiting.get()
        # if any of them are closed/disconnected, skip appropriately
        if getattr(p1, 'closed', False):
            if not getattr(p2, 'closed', False):
                # put p2 back
                waiting.put(p2)
            continue
        if getattr(p2, 'closed', False):
            waiting.put(p1)
            continue
        game = GameSession(p1, p2)
        game.start()

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default=HOST)
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((args.host, args.port))
    s.listen(100)
    log(f"Listening on {args.host}:{args.port}")

    # spawn matcher thread
    threading.Thread(target=matchmaker, daemon=True).start()

    try:
        while True:
            conn, addr = s.accept()
            threading.Thread(target=client_thread, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        log('Shutting down')
        try:
            s.close()
        except:
            pass
