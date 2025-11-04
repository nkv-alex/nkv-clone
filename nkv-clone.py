#!/usr/bin/env python3
"utility to automatically clone configuration from one server to another"

import os
import sys
import subprocess
import shutil
import time
import json


######################
###  OS FUNCTIONS  ###
######################

def run(cmd, check=True):
    return subprocess.run(cmd, shell=True, check=check, capture_output=True, text=True)




#####################
##  MAIN VARIABLES ##
#####################

Mode = ""
UDP_PORT = 49999
config_file = "nkv-clone-config.json"
interfaces_json = "interfaces.json"
SERVICIOS = [
    "sshd",
    "nginx",
    "cron",
    "docker",
    "network-manager",
    "dnsmasq"
    "nfs-server",
    "smbd",
    "isc-dhcp-server"
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


def send_to_hosts(payload, port=UDP_PORT, timeout=2.0, send=True):
    import socket, struct, fcntl, time, uuid, json, os

    DISCOVER_MESSAGE_PREFIX = "DISCOVER_REQUEST"
    RESPONSE_PREFIX = "DISCOVER_RESPONSE"
    HOSTS_FILE = "nkv-clone-coms.json"

    
    global interfaces
    internals = [iface for iface, v in interfaces.items() if v["type"] == "internal"]

    def get_broadcast(iface):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            return socket.inet_ntoa(fcntl.ioctl(
                s.fileno(),
                0x8919,  # SIOCGIFBRDADDR
                struct.pack('256s', iface.encode('utf-8')[:15])
            )[20:24])
        except Exception as e:
            print(f"[net] Error getting broadcast for {iface}: {e}")
            return "255.255.255.255"

    def save_hosts(discovered):
        try:
            with open(HOSTS_FILE, "w") as f:
                json.dump(discovered, f, indent=2)
            print(f"[store] {len(discovered)} hosts saved in {HOSTS_FILE}")
        except Exception as e:
            print(f"[store] Error saving hosts: {e}")

    discovered_total = {}

    for iface in internals:
        broadcast_ip = get_broadcast(iface)
        print(f"[discover:{iface}] using broadcast {broadcast_ip}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(timeout)

        token = str(uuid.uuid4())[:8]
        discover_msg = f"{DISCOVER_MESSAGE_PREFIX}:{token}"
        sock.sendto(discover_msg.encode(), (broadcast_ip, port))
        print(f"[discover:{iface}] Broadcast sent, waiting {timeout}s...")

        start_time = time.time()
        while True:
            try:
                data, addr = sock.recvfrom(1024)
                text = data.decode(errors="ignore")
                ip, _ = addr
                if text.startswith(RESPONSE_PREFIX):
                    parts = text.split(":", 2)
                    hostname = parts[1] if len(parts) > 1 else ip
                    nodeid = parts[2] if len(parts) > 2 else ""
                    discovered_total[ip] = {"hostname": hostname.strip(), "nodeid": nodeid.strip()}
                    print(f"[discover:{iface}] response from {ip} -> {hostname}")
            except socket.timeout:
                break
            if time.time() - start_time > timeout:
                break

    if not discovered_total:
        print("[discover] No listeners detected.")
        return {}

    print(f"[discover] Total {len(discovered_total)} hosts found:")
    for ip, info in discovered_total.items():
        print(f"  - {ip} ({info.get('hostname')})")

    save_hosts(discovered_total)

    if send:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(2.0)  
        for ip in discovered_total.keys():
            try:
                sock.sendto(payload.encode(), (ip, port))
                print(f"[send] payload sent to {ip}")
                
               
                try:
                    data, addr = sock.recvfrom(1024) 
                    print(f"[recv] response from {addr[0]}: {data.decode(errors='ignore')}")
                except socket.timeout:
                    print(f"[recv] no response from {ip}")
                
            except Exception as e:
                print(f"[send] failed to send to {ip}: {e}")


    return discovered_total


##############################
###  CHECK SYS FUNCTION    ###
##############################
def service_status(name):
    """
    Verifica si el servicio existe y si está activo.
    True = existe y está activo
    False = existe pero está inactivo o fallido
    No se agrega si no existe
    """
    # Primero verificamos si la unidad existe
    check = subprocess.run(
        f"systemctl list-unit-files | grep -w {name}.service",
        shell=True,
        capture_output=True,
        text=True
    )

    if check.returncode != 0:
        print(f"[WARN] {name}: servicio no encontrado en el sistema.")
        return None

    # Consultar estado actual
    proc = subprocess.run(
        f"systemctl is-active {name}",
        shell=True,
        capture_output=True,
        text=True
    )

    status = proc.stdout.strip() or "unknown"
    active = status in ("active", "activating", "sleeping")

    # Cargar JSON existente o crear nuevo
    data = {}
    if os.path.exists(config_file):
        try:
            with open(config_file, "r") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            print(f"[WARN] {config_file} corrupto, se regenerará.")

    # Registrar servicio aunque esté inactivo
    data[name] = active

    # Guardar cambios
    with open(config_file, "w") as f:
        json.dump(data, f, indent=4)

    print(f"[INFO] {name}: {status} -> {active}")
    return active


def check_all_services():
    """Escanea todos los servicios existentes en el sistema."""
    result = subprocess.run(
        "systemctl list-unit-files --type=service --no-pager --no-legend | awk '{print $1}'",
        shell=True,
        capture_output=True,
        text=True
    )
    servicios = [s.replace(".service", "") for s in result.stdout.splitlines() if s.strip()]

    print(f"[INFO] Detectados {len(servicios)} servicios.\n")

    for s in servicios:
        service_status(s)


#######################
###  MAIN LOGIC     ###
#######################
def main():
    check_all_services()
    





if __name__ == "__main__":
    main()
