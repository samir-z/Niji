#!/usr/bin/env python3
"""
niji-play.py — Daemon de reproducción multimedia para Niji (虹)
Autor: Joshua / Nova
Uso:
    python3 niji-play.py <url>           # Reproducir un video
    python3 niji-play.py --mix <url>     # Reproducir un mix/playlist de YouTube
    python3 niji-play.py --socket-only   # Solo levantar el servidor (para reconexión)

Control vía socket: usar niji-ctl.py
"""

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib

import sys
import os
import json
import socket
import threading
import subprocess
import time
import signal
import logging

# ─────────────────────────────────────────────
# CONFIGURACIÓN
# ─────────────────────────────────────────────
SOCKET_PATH = "/tmp/niji.sock"
COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www.youtube.com_cookies.txt")
LOG_FILE = "/tmp/niji-play.log"

# Formato de video: m3u8 con avc1 preferido (pre-muxeado, sin DASH headaches)
# Fallback a DASH si no hay m3u8 disponible
FORMAT_SELECTOR = (
    "best[vcodec^=avc1][protocol=m3u8_native][height<=1080]"
    "/best[vcodec^=avc1][protocol=m3u8_native]"
    "/best[vcodec^=avc1][height<=1080]"
    "/best[vcodec^=avc1]"
)

# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("niji")

# ─────────────────────────────────────────────
# YT-DLP URL RESOLVER
# ─────────────────────────────────────────────
class YtDlpResolver:
    def __init__(self, cookies_path: str):
        self.cookies_path = cookies_path
        self._check_cookies()

    def _check_cookies(self):
        if os.path.exists(self.cookies_path):
            log.info(f"Cookies cargadas: {self.cookies_path}")
        else:
            log.warning(f"Cookies NO encontradas en: {self.cookies_path}")
            log.warning("Reproduciendo sin autenticación — mixes personalizados no funcionarán")

    def _base_cmd(self):
        cmd = ["yt-dlp", "--no-warnings", "--quiet"]
        if os.path.exists(self.cookies_path):
            cmd += ["--cookies", self.cookies_path]
        return cmd

    def get_stream_info(self, url: str) -> dict | None:
        """Resuelve URL + metadata (título, resolución, duración) de un video."""
        log.info(f"Resolviendo URL: {url}")
        # Un solo comando que devuelve todo tabulado
        cmd = self._base_cmd() + [
            "-f", FORMAT_SELECTOR,
            "--print", "%(url)s\t%(height)s\t%(duration_string)s\t%(title)s",
            url
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
            if result.returncode == 0 and result.stdout.strip():
                line = result.stdout.strip().split("\n")[0]
                parts = line.split("\t", 3)
                if len(parts) < 4:
                    return None
                stream_url, height, duration_str, title = parts
                log.info(f"URL resuelta OK — {title} [{height}p, {duration_str}]")
                return {
                    "url": stream_url,
                    "height": height,          # ej: "1080"
                    "duration": duration_str,  # ej: "3:45"
                    "title": title
                }
            else:
                log.error(f"yt-dlp error: {result.stderr.strip()}")
                return None
        except subprocess.TimeoutExpired:
            log.error("yt-dlp timeout (45s)")
            return None
        except Exception as e:
            log.error(f"yt-dlp excepción: {e}")
            return None

    def get_playlist_entries(self, url: str, max_entries: int = 50) -> list[dict]:
        """Extrae las entradas de un mix/playlist de YouTube."""
        log.info(f"Extrayendo playlist: {url}")
        cmd = self._base_cmd() + [
            "--flat-playlist",
            "--print", "%(id)s\t%(title)s",
            "--playlist-end", str(max_entries),
            url
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if result.returncode == 0 and result.stdout.strip():
                entries = []
                for line in result.stdout.strip().split("\n"):
                    if "\t" in line:
                        vid_id, title = line.split("\t", 1)
                        entries.append({
                            "id": vid_id,
                            "title": title,
                            "url": f"https://www.youtube.com/watch?v={vid_id}"
                        })
                log.info(f"Playlist: {len(entries)} videos encontrados")
                return entries
            else:
                log.error(f"yt-dlp playlist error: {result.stderr.strip()}")
                return []
        except subprocess.TimeoutExpired:
            log.error("yt-dlp playlist timeout (60s)")
            return []


# ─────────────────────────────────────────────
# QUEUE MANAGER
# ─────────────────────────────────────────────
class PlaylistManager:
    def __init__(self):
        self._queue: list[dict] = []   # {"url": ..., "title": ...}
        self._index: int = -1
        self._lock = threading.Lock()

    def load_single(self, url: str, title: str = ""):
        with self._lock:
            self._queue = [{"url": url, "title": title or url}]
            self._index = 0

    def load_playlist(self, entries: list[dict]):
        with self._lock:
            self._queue = [{"url": e["url"], "title": e["title"]} for e in entries]
            self._index = 0

    def current(self) -> dict | None:
        with self._lock:
            if 0 <= self._index < len(self._queue):
                return self._queue[self._index]
            return None

    def next(self) -> dict | None:
        with self._lock:
            if self._index + 1 < len(self._queue):
                self._index += 1
                return self._queue[self._index]
            return None

    def prev(self) -> dict | None:
        with self._lock:
            if self._index - 1 >= 0:
                self._index -= 1
                return self._queue[self._index]
            return None

    def has_next(self) -> bool:
        with self._lock:
            return self._index + 1 < len(self._queue)

    def status(self) -> dict:
        with self._lock:
            return {
                "index": self._index,
                "total": len(self._queue),
                "current_title": self._queue[self._index]["title"] if 0 <= self._index < len(self._queue) else "N/A"
            }


# ─────────────────────────────────────────────
# GSTREAMER PLAYER
# ─────────────────────────────────────────────
class NijiPlayer:
    def __init__(self, resolver: YtDlpResolver, playlist: PlaylistManager):
        Gst.init(None)
        self.resolver = resolver
        self.playlist = playlist
        self.loop = GLib.MainLoop()
        self.pipeline = None
        self._paused = False
        self._volume = 1.0
        self._loading = False
        self._running = True
        # Metadata del track actual
        self._current_title = "N/A"
        self._current_quality = "?"
        self._current_duration = "?"

    def _build_pipeline(self, stream_url: str) -> Gst.Pipeline:
        """Construye el pipeline GStreamer para la URL resuelta."""
        pipeline = Gst.ElementFactory.make("playbin", "player")
        pipeline.set_property("uri", stream_url)
        pipeline.set_property("volume", self._volume)

        # En Albireo: autovideosink + autoaudiosink (X11/Wayland + PulseAudio)
        # En Niji:    kmssink sync=true + alsasink sync=true  (Headless + HDMI)
        video_sink = Gst.ElementFactory.make("autovideosink", "vsink")
        audio_sink = Gst.ElementFactory.make("autoaudiosink", "asink")
        pipeline.set_property("video-sink", video_sink)
        pipeline.set_property("audio-sink", audio_sink)

        return pipeline

    def _on_bus_message(self, bus, message):
        mtype = message.type

        if mtype == Gst.MessageType.EOS:
            log.info("EOS — fin del video")
            self._on_end_of_stream()

        elif mtype == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.error(f"GStreamer ERROR: {err.message}")
            log.debug(f"Debug: {debug}")
            self._on_end_of_stream()

        elif mtype == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            log.warning(f"GStreamer WARN: {warn.message}")

        elif mtype == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                _, new, _ = message.parse_state_changed()
                log.debug(f"Pipeline state → {new.value_nick}")

    def _on_end_of_stream(self):
        """Cuando termina un video, pasa al siguiente de la queue."""
        self._stop_pipeline()
        if self.playlist.has_next():
            next_item = self.playlist.next()
            log.info(f"Siguiente: {next_item['title']}")
            # Lanzar en thread para no bloquear el bus
            threading.Thread(target=self._play_item, args=(next_item,), daemon=True).start()
        else:
            log.info("Queue terminada. Esperando comandos.")

    def _stop_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self._paused = False

    def _play_item(self, item: dict):
        """Resuelve y reproduce un item de la queue. Bloqueante."""
        if self._loading:
            log.warning("Ya hay una resolución en curso, ignorando")
            return

        self._loading = True
        self._stop_pipeline()

        log.info(f"▶ Cargando: {item['title']}")
        info = self.resolver.get_stream_info(item["url"])

        if not info:
            log.error(f"No se pudo resolver URL de: {item['title']}")
            self._loading = False
            if self.playlist.has_next():
                next_item = self.playlist.next()
                self._play_item(next_item)
            return

        # Guardar metadata del track
        self._current_title = info["title"]
        self._current_quality = info["height"]
        self._current_duration = info["duration"]
        # Actualizar también el título en la queue (útil para video único que llega como URL)
        item["title"] = info["title"]

        self.pipeline = self._build_pipeline(info["url"])
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            log.error("Fallo al iniciar pipeline")
            self._stop_pipeline()
            self._loading = False
            return

        log.info(f"▶ Reproduciendo: {info['title']} [{info['height']}p | {info['duration']}]")
        self._paused = False
        self._loading = False

    def play(self, url: str, is_playlist: bool = False):
        """Punto de entrada principal para reproducir."""
        def _load():
            if is_playlist:
                entries = self.resolver.get_playlist_entries(url)
                if not entries:
                    log.error("No se pudo cargar la playlist")
                    return
                self.playlist.load_playlist(entries)
                log.info(f"Mix cargado: {len(entries)} videos en queue")
            else:
                self.playlist.load_single(url)

            current = self.playlist.current()
            if current:
                self._play_item(current)

        threading.Thread(target=_load, daemon=True).start()

    # ── Comandos de Control ──────────────────
    def cmd_pause_resume(self) -> str:
        if not self.pipeline:
            return "ERROR: No hay nada reproduciéndose"
        if self._paused:
            self.pipeline.set_state(Gst.State.PLAYING)
            self._paused = False
            log.info("⏸→▶ Resume")
            return "OK: Resumido"
        else:
            self.pipeline.set_state(Gst.State.PAUSED)
            self._paused = True
            log.info("▶→⏸ Pause")
            return "OK: Pausado"

    def cmd_next(self) -> str:
        if self._loading:
            return "ERROR: Cargando video, espera"
        next_item = self.playlist.next()
        if next_item:
            log.info(f"⏭ Siguiente: {next_item['title']}")
            threading.Thread(target=self._play_item, args=(next_item,), daemon=True).start()
            return f"OK: Siguiente → {next_item['title']}"
        return "ERROR: No hay siguiente en la queue"

    def cmd_prev(self) -> str:
        if self._loading:
            return "ERROR: Cargando video, espera"
        prev_item = self.playlist.prev()
        if prev_item:
            log.info(f"⏮ Anterior: {prev_item['title']}")
            threading.Thread(target=self._play_item, args=(prev_item,), daemon=True).start()
            return f"OK: Anterior → {prev_item['title']}"
        return "ERROR: No hay anterior en la queue"

    def cmd_stop(self) -> str:
        self._stop_pipeline()
        self._current_title = "N/A"
        self._current_quality = "?"
        self._current_duration = "?"
        log.info("⏹ Stop — saliendo")
        # Salir del GLib MainLoop limpiamente desde el thread correcto
        GLib.idle_add(self.loop.quit)
        return "OK: Detenido"

    def cmd_volume(self, val: float) -> str:
        val = max(0.0, min(1.0, val))
        self._volume = val
        if self.pipeline:
            self.pipeline.set_property("volume", val)
        log.info(f"🔊 Volumen: {int(val*100)}%")
        return f"OK: Volumen {int(val*100)}%"

    def cmd_status(self) -> str:
        state = "PAUSED" if self._paused else ("LOADING" if self._loading else "PLAYING")
        info = self.playlist.status()

        # Posición actual desde el pipeline
        position_str = "0:00"
        if self.pipeline and not self._loading:
            ok, pos_ns = self.pipeline.query_position(Gst.Format.TIME)
            if ok and pos_ns >= 0:
                pos_s = pos_ns // Gst.SECOND
                position_str = f"{pos_s // 60}:{pos_s % 60:02d}"

        return json.dumps({
            "state": state if self.pipeline or self._loading else "STOPPED",
            "volume": int(self._volume * 100),
            "track": info["index"] + 1,
            "total": info["total"],
            "title": self._current_title,
            "quality": f"{self._current_quality}p" if self._current_quality != "?" else "?",
            "duration": self._current_duration,
            "position": position_str
        })

    def run(self):
        """Arranca el GLib main loop (necesario para los buses de GStreamer)."""
        log.info("GLib MainLoop iniciado")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_pipeline()
            log.info("Player detenido")


# ─────────────────────────────────────────────
# SOCKET CONTROL SERVER
# ─────────────────────────────────────────────
class ControlServer:
    """
    Servidor Unix socket que acepta comandos en texto plano:
      pause  → togglea pausa
      next   → siguiente video
      prev   → video anterior
      stop   → detiene
      vol 80 → volumen al 80%
      status → info del estado actual
    """
    def __init__(self, player: NijiPlayer, socket_path: str = SOCKET_PATH):
        self.player = player
        self.socket_path = socket_path
        self._running = True

    def _handle_client(self, conn: socket.socket):
        try:
            data = conn.recv(256).decode().strip().lower()
            if not data:
                return

            log.info(f"[CMD] → {data!r}")
            parts = data.split()
            cmd = parts[0]

            if cmd == "pause":
                response = self.player.cmd_pause_resume()
            elif cmd == "next":
                response = self.player.cmd_next()
            elif cmd == "prev":
                response = self.player.cmd_prev()
            elif cmd == "stop":
                response = self.player.cmd_stop()
            elif cmd == "vol" and len(parts) > 1:
                try:
                    val = float(parts[1]) / 100.0
                    response = self.player.cmd_volume(val)
                except ValueError:
                    response = "ERROR: vol necesita un número (0-100)"
            elif cmd == "status":
                response = self.player.cmd_status()
            else:
                response = f"ERROR: Comando desconocido '{cmd}'"

            conn.sendall((response + "\n").encode())
        except Exception as e:
            log.error(f"Error en handle_client: {e}")
        finally:
            conn.close()

    def start(self):
        """Levanta el socket server en un thread dedicado."""
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(5)
        os.chmod(self.socket_path, 0o666)
        log.info(f"Control socket: {self.socket_path}")

        def _accept_loop():
            while self._running:
                try:
                    server.settimeout(1.0)
                    conn, _ = server.accept()
                    threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
                except socket.timeout:
                    continue
                except Exception as e:
                    if self._running:
                        log.error(f"Socket error: {e}")
                    break
            server.close()
            if os.path.exists(self.socket_path):
                os.remove(self.socket_path)

        t = threading.Thread(target=_accept_loop, daemon=True)
        t.start()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Uso:")
        print("  python3 niji-play.py <url>             # Video único")
        print("  python3 niji-play.py --mix <url>       # Mix/Playlist")
        print()
        print("Control (en otra terminal):")
        print("  python3 niji-ctl.py status")
        print("  python3 niji-ctl.py pause")
        print("  python3 niji-ctl.py next")
        print("  python3 niji-ctl.py prev")
        print("  python3 niji-ctl.py vol 80")
        print("  python3 niji-ctl.py stop")
        sys.exit(1)

    is_mix = "--mix" in sys.argv
    url = sys.argv[-1]

    resolver = YtDlpResolver(COOKIES_PATH)
    playlist = PlaylistManager()
    player = NijiPlayer(resolver, playlist)

    # Levantar control socket
    ctrl = ControlServer(player)
    ctrl.start()

    # Manejar Ctrl+C limpiamente
    def _sigint(sig, frame):
        log.info("Ctrl+C → cerrando...")
        player._stop_pipeline()
        player.loop.quit()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    # Iniciar reproducción
    log.info(f"Niji (虹) arrancando — {'Mix' if is_mix else 'Video'}: {url}")
    player.play(url, is_playlist=is_mix)

    # Bloquear en el GLib loop (necesario para GStreamer bus)
    player.run()


if __name__ == "__main__":
    main()
