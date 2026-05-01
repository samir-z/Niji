#!/usr/bin/env python3
"""
niji-ctl.py — Control remoto para el daemon niji-play.py
Uso:
    python3 niji-ctl.py <comando> [args]

Comandos:
    status          → Estado actual (título, calidad, posición, volumen)
    pause           → Pausa / Reanuda
    ff [n]          → Adelantar n segundos (default: 5). Ej: ff 10
    rw [n]          → Retroceder n segundos (default: 5). Ej: rw 10
    next            → Siguiente video
    prev            → Video anterior
    stop            → Detiene y cierra el player
    vol <0-100>     → Ajusta el volumen (ej: vol 80)
    stats           → Tiempos de resolución de URL (cronómetro)
"""

import socket
import sys
import json

SOCKET_PATH = "/tmp/niji.sock"


def send_command(cmd: str) -> str:
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(SOCKET_PATH)
        client.sendall(cmd.encode())
        response = client.recv(1024).decode().strip()
        client.close()
        return response
    except FileNotFoundError:
        return "ERROR: niji-play no está corriendo (socket no encontrado)"
    except ConnectionRefusedError:
        return "ERROR: niji-play no está corriendo"
    except socket.timeout:
        return "ERROR: Timeout — niji-play no respondió"
    except Exception as e:
        return f"ERROR: {e}"


def pretty_status(raw: str) -> str:
    """Formatea la respuesta de 'status' de forma legible."""
    try:
        data = json.loads(raw)
        state_icon = {
            "PLAYING": "▶",
            "PAUSED": "⏸",
            "STOPPED": "⏹",
            "LOADING": "⏳"
        }.get(data.get("state", ""), "?")

        pos = data.get("position", "0:00")
        dur = data.get("duration", "?")
        progress = f"{pos} / {dur}" if dur != "?" else pos

        lines = [
            f"  Estado  : {state_icon} {data.get('state', '?')}",
            f"  Título  : {data.get('title', 'N/A')}",
            f"  Track   : {data.get('track', '?')} / {data.get('total', '?')}",
            f"  Calidad : {data.get('quality', '?')}",
            f"  Tiempo  : {progress}",
            f"  Volumen : {data.get('volume', '?')}%",
        ]
        return "\n".join(lines)
    except json.JSONDecodeError:
        return raw  # Si no es JSON, mostrar raw


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    args = sys.argv[1:]
    cmd = " ".join(args).lower()

    response = send_command(cmd)

    # Pretty print para status
    if args[0] == "status" and response.startswith("{"):
        print(pretty_status(response))
    else:
        print(response)


if __name__ == "__main__":
    main()
