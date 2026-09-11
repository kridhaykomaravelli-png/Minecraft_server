import hashlib
import json
import socket
import struct
import threading
import time
import uuid
 

# Configuration

HOST = "0.0.0.0"
PORT = 25565
MOTD = "A Python Minecraft Server (from scratch!)"
MAX_PLAYERS = 20
PROTOCOL_VERSION = 47          # Minecraft 1.8.9
GAME_MODE = 1                  # 0 = survival, 1 = creative (avoids hunger/fall damage logic we don't implement)
DIFFICULTY = 0                 # 0 = peaceful (avoids needing to spawn mobs)
CHUNK_RADIUS = 4               # chunks in each direction from spawn (4 -> 9x9 = 81 chunks)
SPAWN_X, SPAWN_Y, SPAWN_Z = 0, 5, 0
 

# Protocol helpers 

def encode_varint(value: int) -> bytes:
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)
 
 
def encode_string(s: str) -> bytes:
    data = s.encode("utf-8")
    return encode_varint(len(data)) + data
 
 
def encode_position(x: int, y: int, z: int) -> bytes:
    # Packed 64-bit: 26 bits x | 12 bits y | 26 bits z  (pre-1.14 format)
    value = ((x & 0x3FFFFFF) << 38) | ((y & 0xFFF) << 26) | (z & 0x3FFFFFF)
    return struct.pack(">q", value if value < 2**63 else value - 2**64)
 
 
def recv_exact(conn: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("socket closed")
        buf.extend(chunk)
    return bytes(buf)
 
 
def read_varint_from_socket(conn: socket.socket) -> int:
    num = 0
    for i in range(5):
        b = recv_exact(conn, 1)[0]
        num |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            if num & 0x80000000:
                num -= 1 << 32
            return num
    raise ValueError("VarInt too long")
 
 
class Buffer:
    """Cursor over a bytes payload, for reading fields out of a packet body."""
 
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
 
    def read(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        self.pos += n
        return chunk
 
    def read_varint(self) -> int:
        num = 0
        for i in range(5):
            b = self.read(1)[0]
            num |= (b & 0x7F) << (7 * i)
            if not (b & 0x80):
                if num & 0x80000000:
                    num -= 1 << 32
                return num
        raise ValueError("VarInt too long")
 
    def read_string(self) -> str:
        length = self.read_varint()
        return self.read(length).decode("utf-8")
 
    def read_unsigned_short(self) -> int:
        return struct.unpack(">H", self.read(2))[0]
 
    def read_bool(self) -> bool:
        return self.read(1)[0] != 0
 
    def read_long(self) -> int:
        return struct.unpack(">q", self.read(8))[0]
 
    def read_double(self) -> float:
        return struct.unpack(">d", self.read(8))[0]
 
    def read_float(self) -> float:
        return struct.unpack(">f", self.read(4))[0]
 
 
def send_packet(conn: socket.socket, packet_id: int, data: bytes = b"") -> None:
    body = encode_varint(packet_id) + data
    frame = encode_varint(len(body)) + body
    conn.sendall(frame)
 
 
def read_packet(conn: socket.socket) -> "tuple[int, Buffer]":
    length = read_varint_from_socket(conn)
    payload = recv_exact(conn, length)
    buf = Buffer(payload)
    packet_id = buf.read_varint()
    return packet_id, buf
 
 
# World platform created

def build_flat_section() -> bytes:
    """One 16x16x16 section: bedrock, dirt x3, grass, then air, fully lit."""
    blocks = bytearray(4096 * 2)
    for y in range(16):
        if y == 0:
            block_id = 7        # bedrock
        elif 1 <= y <= 3:
            block_id = 3        # dirt
        elif y == 4:
            block_id = 2        # grass block
        else:
            block_id = 0        # air
        value = block_id << 4   # metadata 0
        for z in range(16):
            for x in range(16):
                idx = (y * 16 + z) * 16 + x
                blocks[idx * 2] = value & 0xFF
                blocks[idx * 2 + 1] = (value >> 8) & 0xFF
    block_light = bytes([0xFF]) * 2048  # full light everywhere
    sky_light = bytes([0xFF]) * 2048
    return bytes(blocks) + block_light + sky_light
 
 
_FLAT_SECTION = build_flat_section()
_BIOME_ARRAY = bytes([1]) * 256  # 1 = plains, for every column
 
 
def build_chunk_packet(chunk_x: int, chunk_z: int) -> bytes:
    primary_bitmask = 0b1  # only section 0 (y 0-15) present
    data = _FLAT_SECTION + _BIOME_ARRAY
    body = (
        struct.pack(">i", chunk_x)
        + struct.pack(">i", chunk_z)
        + b"\x01"  # ground-up continuous = true
        + encode_varint(primary_bitmask)
        + encode_varint(len(data))
        + data
    )
    return body
 
 
def send_world(conn: socket.socket) -> None:
    for cx in range(-CHUNK_RADIUS, CHUNK_RADIUS + 1):
        for cz in range(-CHUNK_RADIUS, CHUNK_RADIUS + 1):
            send_packet(conn, 0x21, build_chunk_packet(cx, cz))
 
 

# Offline-mode Identity

def offline_uuid(username: str) -> str:
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{username}".encode("utf-8")).digest())
    digest[6] = (digest[6] & 0x0F) | 0x30  # version 3
    digest[8] = (digest[8] & 0x3F) | 0x80  # variant
    return str(uuid.UUID(bytes=bytes(digest)))
 
 

# Shared server state

clients_lock = threading.Lock()
clients: "dict[socket.socket, str]" = {}  # conn -> username, PLAY-state clients only
 
 
def broadcast_chat(message: str) -> None:
    packet = encode_string(json.dumps({"text": message})) + b"\x00"  # position 0 = chat box
    with clients_lock:
        dead = []
        for conn in clients:
            try:
                send_packet(conn, 0x02, packet)
            except OSError:
                dead.append(conn)
        for conn in dead:
            clients.pop(conn, None)
 
 

# Per-connection handling

def send_status_response(conn: socket.socket) -> None:
    with clients_lock:
        online = len(clients)
    response = {
        "version": {"name": "1.8.9", "protocol": PROTOCOL_VERSION},
        "players": {"max": MAX_PLAYERS, "online": online, "sample": []},
        "description": {"text": MOTD},
    }
    send_packet(conn, 0x00, encode_string(json.dumps(response)))
 
 
def handle_status(conn: socket.socket) -> None:
    while True:
        packet_id, buf = read_packet(conn)
        if packet_id == 0x00:  # Request
            send_status_response(conn)
        elif packet_id == 0x01:  # Ping
            payload = buf.read(8)
            send_packet(conn, 0x01, payload)
            return
 
 
def handle_login(conn: socket.socket) -> "tuple[str, str] | None":
    packet_id, buf = read_packet(conn)
    if packet_id != 0x00:
        return None
    username = buf.read_string()
    player_uuid = offline_uuid(username)
    send_packet(conn, 0x02, encode_string(player_uuid) + encode_string(username))
    return username, player_uuid
 
 
def send_join_sequence(conn: socket.socket, entity_id: int) -> None:
    join_data = (
        struct.pack(">i", entity_id)
        + struct.pack(">B", GAME_MODE)
        + struct.pack(">b", 0)          # dimension: overworld
        + struct.pack(">B", DIFFICULTY)
        + struct.pack(">B", MAX_PLAYERS)
        + encode_string("flat")
        + b"\x00"                        # reduced debug info: false
    )
    send_packet(conn, 0x01, join_data)
 
    send_packet(conn, 0x05, encode_position(SPAWN_X, SPAWN_Y, SPAWN_Z))
 
    send_world(conn)
 
    pos_data = (
        struct.pack(">d", SPAWN_X + 0.5)
        + struct.pack(">d", float(SPAWN_Y))
        + struct.pack(">d", SPAWN_Z + 0.5)
        + struct.pack(">f", 0.0)
        + struct.pack(">f", 0.0)
        + struct.pack(">b", 0)          # flags: all absolute
    )
    send_packet(conn, 0x08, pos_data)
 
 
def keep_alive_loop(conn: socket.socket, stop_event: threading.Event) -> None:
    keep_alive_id = 0
    while not stop_event.is_set():
        try:
            keep_alive_id += 1
            send_packet(conn, 0x00, encode_varint(keep_alive_id))
        except OSError:
            return
        stop_event.wait(10)
 
 
def handle_play(conn: socket.socket, username: str, entity_id: int) -> None:
    stop_event = threading.Event()
    ka_thread = threading.Thread(target=keep_alive_loop, args=(conn, stop_event), daemon=True)
    ka_thread.start()
 
    with clients_lock:
        clients[conn] = username
    broadcast_chat(f"* {username} joined the game")
 
    try:
        while True:
            packet_id, buf = read_packet(conn)
            if packet_id == 0x01:  # Chat Message (serverbound)
                message = buf.read_string()
                print(f"<{username}> {message}")
                broadcast_chat(f"<{username}> {message}")
            # All other play packets (movement, digging, etc.) are simply
            # discarded here - the framing already consumed exactly the
            # right number of bytes, so we don't need to parse fields we
            # don't act on.
    except (ConnectionError, OSError):
        pass
    finally:
        stop_event.set()
        with clients_lock:
            clients.pop(conn, None)
        broadcast_chat(f"* {username} left the game")
 
 
_entity_id_counter = [0]
_entity_id_lock = threading.Lock()
 
 
def next_entity_id() -> int:
    with _entity_id_lock:
        _entity_id_counter[0] += 1
        return _entity_id_counter[0]
 
 
def handle_client(conn: socket.socket, addr) -> None:
    try:
        # --- Handshake ---
        packet_id, buf = read_packet(conn)
        if packet_id != 0x00:
            conn.close()
            return
        buf.read_varint()          # protocol version (not enforced)
        buf.read_string()          # server address the client used
        buf.read_unsigned_short()  # server port the client used
        next_state = buf.read_varint()
 
        if next_state == 1:
            handle_status(conn)
        elif next_state == 2:
            result = handle_login(conn)
            if result is None:
                return
            username, _player_uuid = result
            entity_id = next_entity_id()
            send_join_sequence(conn, entity_id)
            handle_play(conn, username, entity_id)
    except (ConnectionError, OSError, ValueError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass
 
 
def main() -> None:
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((HOST, PORT))
    server_sock.listen(5)
    print(f"Pure-Python Minecraft server listening on {HOST}:{PORT}")
    print(f"Connect with a Minecraft {'/'.join(['1.8', '1.8.9'])} client via Direct Connect.")
 
    try:
        while True:
            conn, addr = server_sock.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        print("Shutting down.")
    finally:
        server_sock.close()
 
 
if __name__ == "__main__":
    main()
