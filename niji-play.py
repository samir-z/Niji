#!/usr/bin/env python3
"""
niji-play.py — Daemon de reproducción multimedia para Niji (虹)
Autor: Joshua / Nova  |  v1.1

Uso:
    python3 niji-play.py <url>           # Video único
    python3 niji-play.py --mix <url>     # Mix/Playlist de YouTube
"""

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GLib', '2.0')
from gi.repository import Gst, GLib

import sys, os, json, socket, threading, subprocess, signal, logging, time

# ─── CONFIGURACIÓN ────────────────────────────────────────────────────────────
SOCKET_PATH  = "/tmp/niji.sock"
COOKIES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www.youtube.com_cookies.txt")
SEEK_SECONDS = 5          # segundos que avanza/retrocede ff/rw
FORMAT_SELECTOR = (
    "best[vcodec^=avc1][protocol=m3u8_native][height<=1080]"
    "/best[vcodec^=avc1][protocol=m3u8_native]"
    "/best[vcodec^=avc1][height<=1080]"
    "/best[vcodec^=avc1]"
)

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger("niji")


# ─── ESTADÍSTICAS DE RESOLUCIÓN ───────────────────────────────────────────────
class ResolutionStats:
    """Registra y promedia los tiempos de resolución de URL (yt-dlp)."""
    def __init__(self):
        self._times: list[float] = []
        self._lock = threading.Lock()

    def record(self, elapsed: float):
        with self._lock:
            self._times.append(elapsed)
            avg = sum(self._times) / len(self._times)
            log.info(f"⏱  Resolución: {elapsed:.1f}s | Promedio: {avg:.1f}s ({len(self._times)} muestras)")

    def summary(self) -> str:
        with self._lock:
            if not self._times:
                return "Sin muestras aún"
            return (
                f"Muestras: {len(self._times)} | "
                f"Mín: {min(self._times):.1f}s | "
                f"Prom: {sum(self._times)/len(self._times):.1f}s | "
                f"Máx: {max(self._times):.1f}s"
            )


# ─── YT-DLP RESOLVER ──────────────────────────────────────────────────────────
class YtDlpResolver:
    def __init__(self, cookies_path: str, stats: ResolutionStats):
        self.cookies_path = cookies_path
        self.stats = stats
        if os.path.exists(cookies_path):
            log.info(f"Cookies cargadas: {cookies_path}")
        else:
            log.warning("Cookies NO encontradas — mixes personalizados no funcionarán")

    def _base_cmd(self):
        cmd = ["yt-dlp", "--no-warnings", "--quiet"]
        if os.path.exists(self.cookies_path):
            cmd += ["--cookies", self.cookies_path]
        return cmd

    def get_stream_info(self, url: str) -> dict | None:
        """Resuelve URL + metadata. Registra el tiempo que tarda."""
        log.info(f"Resolviendo: {url}")
        cmd = self._base_cmd() + [
            "-f", FORMAT_SELECTOR,
            "--print", "%(url)s\t%(height)s\t%(duration_string)s\t%(title)s",
            url
        ]
        t0 = time.monotonic()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            elapsed = time.monotonic() - t0
            if result.returncode == 0 and result.stdout.strip():
                parts = result.stdout.strip().split("\n")[0].split("\t", 3)
                if len(parts) < 4:
                    return None
                stream_url, height, duration_str, title = parts
                self.stats.record(elapsed)
                log.info(f"✅ {title} [{height}p | {duration_str}]")
                return {"url": stream_url, "height": height,
                        "duration": duration_str, "title": title}
            log.error(f"yt-dlp error: {result.stderr.strip()}")
            return None
        except subprocess.TimeoutExpired:
            log.error("yt-dlp timeout (120s)")
            return None
        except Exception as e:
            log.error(f"yt-dlp excepción: {e}")
            return None

    def get_playlist_entries(self, url: str, max_entries: int = 50) -> list[dict]:
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
                        entries.append({"id": vid_id, "title": title,
                                        "url": f"https://www.youtube.com/watch?v={vid_id}"})
                log.info(f"Playlist: {len(entries)} videos")
                return entries
            log.error(f"yt-dlp playlist error: {result.stderr.strip()}")
            return []
        except subprocess.TimeoutExpired:
            log.error("yt-dlp playlist timeout")
            return []


# ─── PLAYLIST / QUEUE ─────────────────────────────────────────────────────────
class PlaylistManager:
    def __init__(self):
        self._queue: list[dict] = []
        self._index: int = -1
        self._lock = threading.Lock()

    def load_single(self, url: str):
        with self._lock:
            self._queue = [{"url": url, "title": url}]
            self._index = 0

    def load_playlist(self, entries: list[dict]):
        with self._lock:
            self._queue = [{"url": e["url"], "title": e["title"]} for e in entries]
            self._index = 0

    def current(self) -> dict | None:
        with self._lock:
            return self._queue[self._index] if 0 <= self._index < len(self._queue) else None

    def peek_next(self) -> dict | None:
        """Ve el siguiente sin avanzar el índice."""
        with self._lock:
            idx = self._index + 1
            return self._queue[idx] if idx < len(self._queue) else None

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
            title = self._queue[self._index]["title"] if 0 <= self._index < len(self._queue) else "N/A"
            return {"index": self._index, "total": len(self._queue), "current_title": title}


# ─── PLAYER ───────────────────────────────────────────────────────────────────
class NijiPlayer:
    def __init__(self, resolver: YtDlpResolver, playlist: PlaylistManager,
                 stats: ResolutionStats, niji_mode: bool = False):
        Gst.init(None)
        self.resolver   = resolver
        self.playlist   = playlist
        self.stats      = stats
        self._niji_mode = niji_mode
        self.loop       = GLib.MainLoop()
        self.pipeline   = None
        self._paused    = False
        self._volume    = 1.0
        self._loading   = False
        # Metadata del track actual
        self._current_title    = "N/A"
        self._current_quality  = "?"
        self._current_duration = "?"
        # Pre-carga del siguiente video
        self._prefetch_info: dict | None = None
        self._prefetch_url:  str  | None = None
        self._prefetch_lock  = threading.Lock()
        # Seek con debounce (acumula offsets, ejecuta un solo seek)
        self._seek_pending: float = 0.0
        self._seek_timer:   threading.Timer | None = None
        self._seek_lock     = threading.Lock()
        self._last_seek_time: float = 0.0  # para no auto-skipear en error post-seek

    # ── Pipeline ──────────────────────────────────────────────────────────────
    def _build_pipeline(self, stream_url: str) -> Gst.Pipeline:
        pipeline = Gst.ElementFactory.make("playbin", "player")
        pipeline.set_property("uri", stream_url)
        pipeline.set_property("volume", self._volume)
        if self._niji_mode:
            # Niji: headless HDMI via KMS + audio via ALSA
            vsink = Gst.ElementFactory.make("kmssink",  "vsink")
            asink = Gst.ElementFactory.make("alsasink", "asink")
            if vsink is None:
                raise RuntimeError("Plugin 'kmssink' no encontrado. Instala: gstreamer1.0-plugins-bad")
            if asink is None:
                raise RuntimeError("Plugin 'alsasink' no encontrado. Instala: gstreamer1.0-alsa")
            vsink.set_property("sync", True)
            asink.set_property("sync", True)
            log.info("Pipeline: kmssink + alsasink (modo Niji)")
        else:
            # Albireo: X11/Wayland + PulseAudio
            vsink = Gst.ElementFactory.make("autovideosink", "vsink")
            asink = Gst.ElementFactory.make("autoaudiosink", "asink")
            if vsink is None:
                raise RuntimeError("Plugin 'autovideosink' no encontrado. Instala: gstreamer1.0-plugins-good")
            if asink is None:
                raise RuntimeError("Plugin 'autoaudiosink' no encontrado. Instala: gstreamer1.0-plugins-good")
            log.info("Pipeline: autovideosink + autoaudiosink (modo Albireo)")
        pipeline.set_property("video-sink", vsink)
        pipeline.set_property("audio-sink", asink)
        return pipeline

    def _stop_pipeline(self):
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self._paused  = False

    # ── Pre-carga ─────────────────────────────────────────────────────────────
    def _prefetch_next(self):
        """Resuelve en background el URL del siguiente video.
        Espera 15s antes de empezar para no competir con el pipeline en su inicio.
        Reintenta una vez si falla (timeout por carga de CPU).
        """
        next_item = self.playlist.peek_next()
        if not next_item:
            return
        yt_url = next_item["url"]
        with self._prefetch_lock:
            if self._prefetch_url == yt_url and self._prefetch_info:
                return   # ya está en caché
        # Esperar a que el pipeline GStreamer se estabilice (buffering inicial)
        # Evita competencia de CPU cuando Node.js + ffmpeg + GStreamer corren juntos
        time.sleep(15)
        log.info(f"⏳ Pre-cargando: {next_item['title']}")
        info = self.resolver.get_stream_info(yt_url)
        if not info:
            # Reintento único después de 5s (puede haber sido timeout por carga puntual)
            log.warning("Pre-carga falló, reintentando en 5s...")
            time.sleep(5)
            info = self.resolver.get_stream_info(yt_url)
        with self._prefetch_lock:
            self._prefetch_url  = yt_url
            self._prefetch_info = info
        if info:
            log.info(f"⚡ Pre-carga lista: {info['title']}")
        else:
            log.warning(f"Pre-carga falló definitivamente: {next_item['title']} (se resolverá al reproducir)")

    def _consume_prefetch(self, yt_url: str) -> dict | None:
        """Devuelve el stream_info pre-cargado si coincide con la URL pedida."""
        with self._prefetch_lock:
            if self._prefetch_url == yt_url and self._prefetch_info:
                info = self._prefetch_info
                self._prefetch_info = None
                self._prefetch_url  = None
                log.info("⚡ Usando pre-carga — sin espera de resolución")
                return info
        return None

    # ── Bus de mensajes ───────────────────────────────────────────────────────
    def _on_bus_message(self, bus, message):
        t = message.type
        if t == Gst.MessageType.EOS:
            log.info("EOS — fin del video")
            self._next_or_stop()
        elif t == Gst.MessageType.ERROR:
            err, _ = message.parse_error()
            log.error(f"GStreamer ERROR: {err.message}")
            # Guard: si el error ocurre <3s despues de un seek, es del buffer pool
            # de kmssink al seekear HLS. No skipear al siguiente video.
            if time.monotonic() - self._last_seek_time < 3.0:
                log.warning("Error post-seek suprimido (kmssink buffer pool)")
                return
            self._next_or_stop()
        elif t == Gst.MessageType.WARNING:
            warn, _ = message.parse_warning()
            log.warning(f"GStreamer WARN: {warn.message}")

    def _next_or_stop(self):
        self._stop_pipeline()
        if self.playlist.has_next():
            item = self.playlist.next()
            log.info(f"Auto-siguiente: {item['title']}")
            threading.Thread(target=self._play_item, args=(item,), daemon=True).start()
        else:
            log.info("Queue terminada.")

    # ── Reproducción ──────────────────────────────────────────────────────────
    def _play_item(self, item: dict):
        if self._loading:
            log.warning("Resolución ya en curso, ignorando")
            return
        self._loading = True
        self._stop_pipeline()

        yt_url = item["url"]
        # Intentar usar pre-carga, si no resolver en vivo
        info = self._consume_prefetch(yt_url) or self.resolver.get_stream_info(yt_url)

        if not info:
            log.error(f"No se pudo resolver: {item['title']}")
            self._loading = False
            if self.playlist.has_next():
                self._play_item(self.playlist.next())
            return

        # Guardar metadata
        self._current_title    = info["title"]
        self._current_quality  = info["height"]
        self._current_duration = info["duration"]
        item["title"]          = info["title"]   # fix título en video único

        self.pipeline = self._build_pipeline(info["url"])
        bus = self.pipeline.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_bus_message)

        if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            log.error("Fallo al iniciar pipeline")
            self._stop_pipeline()
            self._loading = False
            return

        log.info(f"▶  {info['title']} [{info['height']}p | {info['duration']}]")
        self._paused  = False
        self._loading = False

        # Disparar pre-carga del siguiente en background
        threading.Thread(target=self._prefetch_next, daemon=True).start()

    def play(self, url: str, is_playlist: bool = False):
        def _load():
            if is_playlist:
                entries = self.resolver.get_playlist_entries(url)
                if not entries:
                    log.error("No se pudo cargar la playlist")
                    return
                self.playlist.load_playlist(entries)
                log.info(f"Mix cargado: {len(entries)} videos")
            else:
                self.playlist.load_single(url)
            item = self.playlist.current()
            if item:
                self._play_item(item)
        threading.Thread(target=_load, daemon=True).start()

    # ── Comandos ──────────────────────────────────────────────────────────────
    def cmd_pause_resume(self) -> str:
        if not self.pipeline:
            return "ERROR: No hay nada reproduciéndose"
        if self._paused:
            self.pipeline.set_state(Gst.State.PLAYING)
            self._paused = False
            log.info("⏸→▶ Resume")
            return "OK: Resumido"
        self.pipeline.set_state(Gst.State.PAUSED)
        self._paused = True
        log.info("▶→⏸ Pause")
        return "OK: Pausado"

    def _execute_seek(self):
        """Ejecuta el seek acumulado.
        Pausa antes de seekear: kmssink no re-aloca buffer pool en PAUSED,
        evitando el 'failed to activate buffer pool' en streams HLS.
        """
        with self._seek_lock:
            offset = self._seek_pending
            self._seek_pending = 0.0
            self._seek_timer   = None
        if not self.pipeline or offset == 0:
            return
        ok, pos_ns = self.pipeline.query_position(Gst.Format.TIME)
        base    = pos_ns if (ok and pos_ns > 0) else 0
        new_pos = max(0, base + int(offset * Gst.SECOND))
        # Pausa → seek → reanuda (evita buffer pool failure en kmssink + HLS)
        was_playing = not self._paused
        if was_playing:
            self.pipeline.set_state(Gst.State.PAUSED)
            self.pipeline.get_state(Gst.SECOND * 2)  # esperar PAUSED confirmado
        self.pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            new_pos
        )
        if was_playing:
            self.pipeline.set_state(Gst.State.PLAYING)
        s = new_pos // Gst.SECOND
        log.info(f"⏩ Seek: {offset:+.0f}s → {s//60}:{s%60:02d}")

    def cmd_seek(self, seconds: float) -> str:
        """Acumula el offset y dispara el seek con debounce de 300ms."""
        if not self.pipeline:
            return "ERROR: No hay nada reproduciéndose"
        with self._seek_lock:
            self._seek_pending += seconds
            if self._seek_timer:
                self._seek_timer.cancel()
            self._seek_timer = threading.Timer(0.3, self._execute_seek)
            self._seek_timer.start()
        self._last_seek_time = time.monotonic()
        direction = "adelante" if seconds > 0 else "atrás"
        return f"OK: {abs(int(seconds))}s {direction}"

    def cmd_next(self) -> str:
        if self._loading:
            return "ERROR: Cargando, espera un momento"
        item = self.playlist.next()
        if item:
            log.info(f"⏭ Siguiente: {item['title']}")
            threading.Thread(target=self._play_item, args=(item,), daemon=True).start()
            return f"OK: Siguiente → {item['title']}"
        return "ERROR: No hay siguiente"

    def cmd_prev(self) -> str:
        if self._loading:
            return "ERROR: Cargando, espera un momento"
        item = self.playlist.prev()
        if item:
            log.info(f"⏮ Anterior: {item['title']}")
            threading.Thread(target=self._play_item, args=(item,), daemon=True).start()
            return f"OK: Anterior → {item['title']}"
        return "ERROR: No hay anterior"

    def cmd_stop(self) -> str:
        self._stop_pipeline()
        self._current_title    = "N/A"
        self._current_quality  = "?"
        self._current_duration = "?"
        log.info("⏹ Stop — saliendo")
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
        info  = self.playlist.status()
        pos_str = "0:00"
        if self.pipeline and not self._loading:
            ok, pos_ns = self.pipeline.query_position(Gst.Format.TIME)
            if ok and pos_ns >= 0:
                s = pos_ns // Gst.SECOND
                pos_str = f"{s//60}:{s%60:02d}"
        return json.dumps({
            "state":    state if self.pipeline or self._loading else "STOPPED",
            "volume":   int(self._volume * 100),
            "track":    info["index"] + 1,
            "total":    info["total"],
            "title":    self._current_title,
            "quality":  f"{self._current_quality}p" if self._current_quality != "?" else "?",
            "duration": self._current_duration,
            "position": pos_str
        })

    def cmd_stats(self) -> str:
        return self.stats.summary()

    def run(self):
        log.info("GLib MainLoop iniciado")
        try:
            self.loop.run()
        except KeyboardInterrupt:
            pass
        finally:
            self._stop_pipeline()
            log.info("Player detenido")


# ─── CONTROL SERVER ───────────────────────────────────────────────────────────
class ControlServer:
    """
    Comandos via socket:
      pause       → toggle pausa/resume
      ff          → adelantar 5 segundos
      rw          → retroceder 5 segundos
      next        → siguiente video
      prev        → video anterior
      stop        → detener y salir
      vol <0-100> → volumen
      status      → estado actual (JSON)
      stats       → tiempos de resolución de URL
    """
    def __init__(self, player: NijiPlayer, socket_path: str = SOCKET_PATH):
        self.player      = player
        self.socket_path = socket_path
        self._running    = True

    def _handle_client(self, conn: socket.socket):
        try:
            data  = conn.recv(256).decode().strip().lower()
            if not data:
                return
            log.info(f"[CMD] → {data!r}")
            parts = data.split()
            cmd   = parts[0]

            if   cmd == "pause":  response = self.player.cmd_pause_resume()
            elif cmd == "ff":
                secs = float(parts[1]) if len(parts) > 1 else SEEK_SECONDS
                response = self.player.cmd_seek(+secs)
            elif cmd == "rw":
                secs = float(parts[1]) if len(parts) > 1 else SEEK_SECONDS
                response = self.player.cmd_seek(-secs)
            elif cmd == "next":   response = self.player.cmd_next()
            elif cmd == "prev":   response = self.player.cmd_prev()
            elif cmd == "stop":   response = self.player.cmd_stop()
            elif cmd == "status": response = self.player.cmd_status()
            elif cmd == "stats":  response = self.player.cmd_stats()
            elif cmd == "vol" and len(parts) > 1:
                try:
                    response = self.player.cmd_volume(float(parts[1]) / 100.0)
                except ValueError:
                    response = "ERROR: vol necesita un número (0-100)"
            else:
                response = f"ERROR: Comando desconocido '{cmd}'"

            conn.sendall((response + "\n").encode())
        except Exception as e:
            log.error(f"Error en handle_client: {e}")
        finally:
            conn.close()

    def start(self):
        if os.path.exists(self.socket_path):
            os.remove(self.socket_path)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.socket_path)
        server.listen(5)
        os.chmod(self.socket_path, 0o666)
        log.info(f"Control socket: {self.socket_path}")

        def _loop():
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

        threading.Thread(target=_loop, daemon=True).start()


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print("Uso:")
        print("  python3 niji-play.py <url>")
        print("  python3 niji-play.py --mix <url>")
        print("\nControl (niji-ctl.py):")
        print("  status | pause | ff | rw | next | prev | vol <n> | stop | stats")
        sys.exit(1)

    is_mix    = "--mix"  in sys.argv
    niji_mode = "--niji" in sys.argv
    url       = sys.argv[-1]

    if niji_mode:
        log.info("Modo: Niji (kmssink + alsasink)")
    else:
        log.info("Modo: Albireo (autovideosink + autoaudiosink)")

    stats    = ResolutionStats()
    resolver = YtDlpResolver(COOKIES_PATH, stats)
    playlist = PlaylistManager()
    player   = NijiPlayer(resolver, playlist, stats, niji_mode=niji_mode)

    ctrl = ControlServer(player)
    ctrl.start()

    def _sigint(sig, frame):
        log.info("Ctrl+C → cerrando...")
        player._stop_pipeline()
        player.loop.quit()
        sys.exit(0)
    signal.signal(signal.SIGINT, _sigint)

    log.info(f"Niji (虹) v1.1 — {'Mix' if is_mix else 'Video'}: {url}")
    player.play(url, is_playlist=is_mix)
    player.run()


if __name__ == "__main__":
    main()
