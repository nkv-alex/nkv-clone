#!/usr/bin/env python3
"utility to automatically clone configuration from one server to another"

import os
import sys
import subprocess
import shutil
import time
import json
import socket    
import struct 
import threading                  
from pathlib import Path

######################
###  OS FUNCTIONS  ###
######################

def run(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)




#####################
##  MAIN VARIABLES ##
#####################
HEADER_FMT = "!I Q"                # formato: I=uint32 (len nombre), Q=uint64 (tamaño archivo)
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # tamaño en bytes del header fijo
Mode = ""
UDP_PORT = 49999
config_file = "nkv-clone-config.json"
interfaces_json = "interfaces.json"
services=[
    "/etc/dnsmasq.conf",
    "/etc/dhcp",
    "/etc/apache2",
    "/etc/exports",
    "/etc/iscsi",
    "/etc/samba",
    "/etc/postfix",
    "/etc/vsftpd.conf",
    "/etc/ssh/"
]


##############################
###  COMUNICATION FUNC     ###
##############################

def detect_interfaces():

    """Detect interfaces and saves it in a json.
    """
    global interfaces

    import ipaddress
    # Load existing JSON
    if os.path.exists(interfaces_json):
        try:
            with open(interfaces_json, "r") as f:
                interfaces = json.load(f)
            print(f"[INFO] Configuration loaded from {interfaces_json}")
        except Exception as e:
            print(f"[WARN] Could not read {interfaces_json}: {e}")
            interfaces = {}
    else:
        interfaces = {}
    
    

    #Detect interfaces 
    try:
        res = subprocess.run(
            "ip -o -4 addr show | awk '{print $2,$4}' | grep -Ev '^(lo|docker|veth|br-|virbr|vmnet|tap)' || true",
            capture_output=True,
            text=True,
            check=False,
            shell=True  # <- important
        )
    except Exception as e:
        print(f"[ERROR] Error running 'ip': {e}")
        return {}
    out = res.stdout.strip()
    if not out:
        print("[ERROR] No interfaces with assigned IPv4 found.")
        return interfaces

    print("\n=== Interface detection ===")

    #Process detected interfaces
    updated = False
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue

        iface, addr = parts[0], parts[1]
        try:
            ipif = ipaddress.IPv4Interface(addr)
        except Exception:
            print(f"[WARN] Invalid address in {iface}: {addr}, skipping.")
            continue

        # If already exists and matches, do not ask
        if iface in interfaces and interfaces[iface].get("ip") == str(ipif):
            print(f"[OK] {iface} unchanged → type: {interfaces[iface]['type']}")
            continue

        # Ask only if new or changed
        print(f"\nDetected interface: {iface}")
        print(f"  IP address: {ipif}")
        suggested = "i" if ipif.ip.is_private else "e"
        tipo = input(f"Is this interface internal (i) or external (e)? [{suggested}]: ").strip().lower()
        if tipo == "":
            tipo = suggested
        if tipo not in ("i", "e", "internal", "external"):
            tipo = suggested

        t = "internal" if tipo.startswith("i") else "external"
        interfaces[iface] = {"ip": str(ipif), "type": t}
        updated = True

    #Save changes if there were updates
    if updated:
        try:
            with open(interfaces_json, "w") as f:
                json.dump(interfaces, f, indent=4)
            print(f"[INFO] Configuration updated in {interfaces_json}")
        except Exception as e:
            print(f"[ERROR] Could not save {interfaces_json}: {e}")
    else:
        print("[INFO] No changes in interfaces.")

    #Show summary
    intern = [k for k, v in interfaces.items() if v["type"] == "internal"]
    extern = [k for k, v in interfaces.items() if v["type"] == "external"]
    print("\nFinal summary:")
    print(f"  Internal: {intern}")
    print(f"  External: {extern}")

    return interfaces




# Uso: python file_transfer.py server 0.0.0.0 9000 ./received
#       python file_transfer.py send 192.168.1.10 9000 ./data.json

def send_file(dest_ip: str, dest_port: int, filepath: str) -> None:
    """
    Cliente: conecta al servidor y envía un archivo.
    """
    path = Path(filepath)
    if not path.is_file():
        raise FileNotFoundError(f"Archivo no encontrado: {filepath}")

    filename_bytes = path.name.encode("utf-8")           # nombre en bytes
    filename_len = len(filename_bytes)                   # len nombre
    filesize = path.stat().st_size                       # tamaño en bytes

    # abrir socket y conectar
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.connect((dest_ip, dest_port))                  # conexión TCP
        # enviar header: len(nombre) (4 bytes) + filesize (8 bytes)
        header = struct.pack(HEADER_FMT, filename_len, filesize)
        s.sendall(header)
        # enviar nombre
        s.sendall(filename_bytes)

        # enviar contenido en chunks
        # para no saturar memoria
        with open(path, "rb") as f:
            sent = 0
            while True:
                chunk = f.read(64 * 1024)                # 64KB por chunk
                if not chunk:
                    break
                s.sendall(chunk)
                sent += len(chunk)


def _recv_all(sock: socket.socket, n: int) -> bytes:
    """
    Lee exactamente n bytes del socket (bloqueante hasta obtenerlos o error).
    """
    data = bytearray()
    while len(data) < n:
        packet = sock.recv(n - len(data))
        if not packet:
            raise ConnectionError("Conexión cerrada inesperadamente mientras se leía")
        data.extend(packet)
    return bytes(data)


def handle_client(conn: socket.socket, addr: tuple, save_dir: str) -> None:
    """
    Worker del servidor para una conexión entrante: recibe header, nombre y archivo.
    """
    try:
        # leer header fijo
        header_bytes = _recv_all(conn, HEADER_SIZE)
        name_len, filesize = struct.unpack(HEADER_FMT, header_bytes)

        # leer nombre del archivo 
        name_bytes = _recv_all(conn, name_len)
        filename = name_bytes.decode("utf-8")

        # asegurar directorio destino
        os.makedirs(save_dir, exist_ok=True)
        dest_path = Path(save_dir) / filename

        # escribir contenido en chunks hasta filesize
        remaining = filesize
        with open(dest_path, "wb") as f:
            while remaining > 0:
                chunk_size = 64 * 1024 if remaining >= 64 * 1024 else remaining
                chunk = conn.recv(chunk_size)
                if not chunk:
                    raise ConnectionError("Conexión cerrada durante la transferencia")
                f.write(chunk)
                remaining -= len(chunk)
        # conexión finalizada, archivo guardado
    except Exception as e:
        # logging mínimo
        print(f"[ERROR] {addr}: {e}")
    finally:
        conn.close()

def start_server(listen_ip: str, listen_port: int, save_dir: str) -> None:
    """
    Levanta el servidor que acepta múltiples envíos concurrentes.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((listen_ip, listen_port))
        srv.listen(8)  
        print(f"[INFO] Server listening on {listen_ip}:{listen_port}, saving to {save_dir}")

        try:
            while True:
                conn, addr = srv.accept()
                print(f"[INFO] Conexión entrante desde {addr}")
                # manejar en thread separado
                t = threading.Thread(target=handle_client, args=(conn, addr, save_dir), daemon=True)
                t.start()
        except KeyboardInterrupt:
            print("[INFO] Servidor detenido por operador")


##############################
###  CHECK SYS FUNCTION    ###
##############################
def check_files():
        # Initialize or load the config dictionary
    if os.path.exists(config_file):
        with open(config_file, "r") as f:
            try:
                config = json.load(f)
            except json.JSONDecodeError:
                config = {}
    else:
        config = {}

    # Update services status
    for service in services:
        if service == "":
            continue
        config[service] = True

    # Save the updated configuration
    with open(config_file, "w") as f:
        json.dump(config, f, indent=4)





#######################
###  MAIN LOGIC     ###
#######################
def main():
        # CLI sencillo
    if len(sys.argv) < 2:
        print("Uso mínimo: server|send ...")
        sys.exit(1)

    mode = sys.argv[1].lower()
    if mode == "server":
        if len(sys.argv) != 5:
            print("Uso: python file_transfer.py server <listen_ip> <port> <save_dir>")
            sys.exit(1)
        _, _, ip, port_s, save_dir = sys.argv
        start_server(ip, int(port_s), save_dir)
    elif mode == "send":
        if len(sys.argv) != 5:
            print("Uso: python file_transfer.py send <dest_ip> <port> <file_path>")
            sys.exit(1)
        _, _, ip, port_s, file_path = sys.argv
        send_file(ip, int(port_s), file_path)
        print("[INFO] Envío completado")
    else:
        print("Modo desconocido. Usa 'server' o 'send'.")
        sys.exit(1)





if __name__ == "__main__":
    main()
