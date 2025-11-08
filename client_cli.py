#!/usr/bin/env python3
"""
client.py

Connect4 client. Interactive terminal client that uses the JSON-over-TCP protocol.

Run:
    python client.py --host localhost --port 4000 --name alice

Behavior:
 - Connects, sends HELLO, waits for START.
 - Prints every SENT/RECV JSON with timestamps.
 - Renders board on UPDATE and prompts user when it's their turn.
 - Sends MOVE messages and handles INVALID/ACK/UPDATE/WIN/DRAW/QUIT.
"""
import socket
import threading
import json
import time
import sys

LOG_LOCK = threading.Lock()

def log(msg):
    with LOG_LOCK:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} CLIENT: {msg}")

class Client:
    def __init__(self, host, port, name='player'):
        self.host = host
        self.port = port
        self.name = name
        self.sock = None
        self.player_id = None
        self.symbol = None
        self.board = None
        self.my_turn = False
        self.running = True
        self.recv_lock = threading.Lock()

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((self.host, self.port))
        # start a background listener
        threading.Thread(target=self._listen, daemon=True).start()
        # send HELLO
        self._send({"type":"HELLO","payload":{"name":self.name}})

    def _send(self, obj):
        try:
            line = json.dumps(obj, separators=(',', ':')) + '\n'
            self.sock.sendall(line.encode('utf-8'))
            log(f"SENT -> {line.strip()}")
        except Exception as e:
            log(f"Send error: {e}")
            self.running = False

    def _listen(self):
        try:
            while self.running:
                # read bytes until newline
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
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError as e:
                    log(f"JSON decode error: {e}")
                    continue
                self._handle(msg)
        except Exception as e:
            log(f"Read/connection error: {e}")
        finally:
            self.running = False
            try:
                self.sock.close()
            except:
                pass

    def _handle(self, msg):
        t = msg.get('type')
        p = msg.get('payload') or {}
        if t == 'WELCOME':
            # server does not assign numeric id here in this simple protocol
            pass
        elif t == 'WAIT':
            print('Waiting for opponent...')
        elif t == 'START':
            self.symbol = p.get('you')
            self.my_turn = bool(p.get('first_turn', False))
            print(f"Game started. You are {self.symbol}. My turn? {self.my_turn}")
            # If it's our turn, prompt
            if self.my_turn:
                self._prompt_move()
        elif t == 'UPDATE':
            self.board = p.get('board')
            next_turn = p.get('next_turn')
            self._print_board()
            # Determine our numeric id from symbol
            if self.symbol == 'X':
                our_id = 1
            elif self.symbol == 'O':
                our_id = 2
            else:
                our_id = None
            self.my_turn = (next_turn == our_id)
            if self.my_turn:
                self._prompt_move()
        elif t == 'INVALID':
            reason = p.get('reason')
            print(f"Invalid move: {reason}")
            # re-prompt
            self._prompt_move()
        elif t == 'ACK':
            # optional; we just log it
            pass
        elif t == 'WIN':
            winner = p.get('winner')
            if (winner == 1 and self.symbol == 'X') or (winner == 2 and self.symbol == 'O'):
                print('You WIN!')
            else:
                print('You LOSE!')
            self.running = False
        elif t == 'DRAW':
            print('Game DRAW')
            self.running = False
        elif t == 'QUIT':
            reason = p.get('reason', '')
            print('Opponent quit or disconnected.', reason)
            self.running = False
        elif t == 'ERROR':
            print('Server error:', p.get('reason'))
        else:
            print('Unknown message type:', t)

    def _prompt_move(self):
        # Run input in a separate thread to avoid blocking listener
        def ask():
            while self.running:
                try:
                    col_s = input('Enter column (0-6): ')
                except EOFError:
                    # user closed input (e.g., ctrl-d)
                    self._send({"type":"QUIT"})
                    self.running = False
                    return
                col_s = col_s.strip()
                if not col_s:
                    continue
                try:
                    col = int(col_s)
                except ValueError:
                    print('Please type an integer between 0 and 6')
                    continue
                if col < 0 or col > 6:
                    print('Out of range (0-6)')
                    continue
                self._send({"type":"MOVE","payload":{"col":col}})
                return
        threading.Thread(target=ask, daemon=True).start()

    def _print_board(self):
        if not self.board:
            return
        print('\nBoard:')
        for r in range(len(self.board)):
            row = self.board[r]
            def s(v):
                if v == 0: return '.'
                if v == 1: return 'X'
                if v == 2: return 'O'
            print('|' + ' '.join(s(x) for x in row) + '|')
        print(' 0 1 2 3 4 5 6\n')

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', default='localhost')
    parser.add_argument('--port', type=int, default=4000)
    parser.add_argument('--name', default='player')
    args = parser.parse_args()

    c = Client(args.host, args.port, args.name)
    try:
        c.connect()
    except Exception as e:
        log(f"Connection error: {e}")
        sys.exit(1)

    try:
        while c.running:
            time.sleep(0.1)
    except KeyboardInterrupt:
        try:
            c._send({"type":"QUIT"})
        except:
            pass
        c.running = False
        sys.exit(0)
