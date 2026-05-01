cat << 'EOF' > NIJI_CONTEXT.md
# PROJECT CONTEXT & ROADMAP: Niji (虹)
**Last Update:** Abril 2026
**Target Architecture:** ARM64 Headless Embedded System
**Primary Maintainer:** Joshua / Samir

## 1. Visión General del Proyecto
Niji (虹) es un sistema embebido diseñado para actualizar un televisor AOC de 49" (1080p, modelo LE49F1361, sin Smart TV) convirtiéndolo en un nodo central de multimedia y telemetría. 

**Objetivos principales:**
1. Recepción y decodificación de streams de audio y video (ej. YouTube, Spotify) vía SSH sin interfaz gráfica (X11/Wayland) para minimizar consumo de RAM.
2. Actuar como monitor de telemetría de sistemas robóticos y embebidos futuros (Proyecto *Hybrid* - Drone STM32, Proyecto *Yatagarasu*, y un brazo robótico) usando ROS2.
3. Operar con un overhead de CPU/RAM extremadamente bajo.
4. Poder mandar comandos de reproducción, pausa, siguiente, etc. a Niji vía SSH.
5. Poder reproducir Spotify. (Actualmente no podemos reproducir Spotify)
6. Minimizar el consumo de RAM y CPU.
7. Minimizar el desgaste de la microSD (TLC).
8. Minimizar el tiempo de espera de decodificación de yt-dlp por lo que para iniciar un video se demora 45 segundos y al reproducir un mix no se tiene que esperar 45 segundos para que inicie el siguiente video. 

## 2. Stack Tecnológico & Hardware
* **Hardware Principal:** Raspberry Pi Zero 2 W (Quad-core Cortex-A53, 512MB RAM).
* **Almacenamiento:** SanDisk Ultra microSDHC 32GB (TLC - Vulnerable a Write Amplification).
* **Display:** AOC 49" 1080p (Conectado vía puerto Mini-HDMI).
* **Sistema Operativo:** Ubuntu Server 24.04 LTS (64-bit) / Headless.
* **Red:** Interfaz `wlan0` configurada por Netplan (Red Home WiFi). IPv6 deshabilitado por kernel para evitar bloqueos TLS.
* **Stack Robótica:** ROS2 (Jazzy) comunicación vía DDS.

## 3. Estado Actual del Sistema

### 🟢 Qué FUNCIONA (Estable & Verificado)
* **Boot & Network:** Conexión WiFi estable, sincronización NTP activa (`systemd-timesyncd`). IPv6 bloqueado exitosamente. Netplan gestiona múltiples APs. RAM disponible: 409MiB, Temp: 52.6°C.
* **Protección de Almacenamiento:** `log2ram` instalado y activo (limitado a 40MB). El journal físico fue purgado (Vacuum a 10MB) previniendo el desgaste prematuro de la microSD TLC.

### 🔴 Qué probablemente NO funcionará (Bugs y Limitaciones Físicas)
* **Decodificación AV1:** La CPU Quad-Core de la Zero 2 W no puede decodificar streams en formato AV1 a tasas de refresco normales (causa frame-drops masivos). **Mitigación:** Filtro yt-dlp `[vcodec^=avc1]` rechaza AV1/VP9.
* **Audio OPUS/Float Puro sobre HDMI:** El controlador ALSA (`hw:0,0`) rechaza formatos de punto flotante sin resampling. **Mitigación:** Flag `--audio-format=s16 --audio-samplerate=48000` fuerza PCM 16-bit 48kHz (nativo HDMI).
* **Decodificacion por CPU en vez de GPU:** Al usar un reproductor como mpv se puede usar la CPU para decodificar video, pero no se puede usar la GPU para decodificar video. **Mitigación:** Se intento varias opciones desde usar "-copy" hasta usar hwdec=drm/kms, pero no se pudo usar la GPU para decodificar video. Recomendar megores reproductores para decodificar video en una Raspberry Pi Zero 2 W

##  4. Roadmap y Siguientes Pasos

**Fase 1: Sistema Base de Reproducción Multimedia** [🔄 EN PROGRESO - Testing v1.0]
- [ ] Sistema de discovery de formatos via yt-dlp (480/720/1080p H.264 + AAC)
- [ ] Elegir un mejor reproductor para decodificar video en una Raspberry Pi Zero 2 W respaldado por proyectos y no adivinar y perder el tiempo probando reproductores al azar.
- [ ] Debugging de captura de URL (yt-dlp quiet mode + tail)

**Fase 2: Middleware Multimedia & Control Remoto** [DISEÑO]
- [ ] Crear sistema de control remoto para que niji pueda controlar la reproducción, pausa, siguiente, y controlar el volumen desde una interfaz web.

**Fase 3: Integración ROS2** [DISEÑO]
- [x] Instalar `ros-jazzy-ros-base`
- [ ] Connectar y sincronizar el reloj de niji con los otros nodos.
- [ ] Verificar comunicación DDS entre RPi Niji ↔ Estación *Albireo* (Ryzen 5) ↔ *Altair* (Ryzen 7) ↔ *Niji* ( Raspberry Pi Zero 2 W).
- [ ] Publicar tópicos de salud del sistema (`/system/cpu_load`, `/system/temp`, `/system/memory`) para que el resto de nodos puedan ver el estado de niji.

**Fase 4: Telemetría Gráfica del Hybrid Drone** [SPEC]
- [ ] Evaluar si levantar servidor gráfico mínimo (`openbox` + fbpanel) sin X11 (usar Wayland-lite o  framebuffer directo)
- [ ] Dashboard que dibuje:
  * Stream de video desde niji-play en región principal
  * Telemetría IMU (MPU6050) del Hybrid Drone en overlay gráfico
  * Logs de ROS2 en región de texto scrollable
  * Control de volumen + pausa/resume del media player (botones virtuales)