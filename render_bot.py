# ============================================================
# STUDIO SOUS-TITRES — HARDSUB + EXTRACTION — RENDER
# ============================================================
# Ce fichier vit dans un dépôt GitHub, déployé sur Render (Docker).
# API_ID / API_HASH / BOT_TOKEN / ADMIN_ID se règlent comme variables
# d'environnement dans Render (onglet Environment), jamais écrits en
# clair ici. Un petit serveur web répond aux pings d'UptimeRobot pour
# empêcher Render de mettre le service en veille.
# ============================================================

VERSION = "11.0"

import os
import re
import sys
import json
import time
import html
import shutil
import struct
import asyncio
import itertools
import functools
import importlib
import traceback
import subprocess
from pathlib import Path


# ============================================================
# TRANSFERTS RAPIDES : cryptg
# ============================================================
# Sans cryptg, Telethon chiffre chaque octet en Python pur : très lent.
# Il doit être installé AVANT le chargement de Telethon.

def ensure_cryptg():
    """Retourne 'ok', 'restart' (installé mais Telethon déjà chargé) ou 'missing'."""
    try:
        import cryptg  # noqa: F401
        return "ok"
    except ImportError:
        pass

    if os.environ.get("SUBBOT_NO_PIP"):
        return "missing"

    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--timeout", "30", "--retries", "1", "cryptg"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300,
        )
        importlib.invalidate_caches()
        import cryptg  # noqa: F401
    except Exception:
        return "missing"

    return "restart" if "telethon.crypto.aes" in sys.modules else "ok"


CRYPTG = ensure_cryptg()

import aiohttp
from telethon import TelegramClient, events, utils
from telethon.errors import FloodWaitError
from telethon.tl.functions.upload import SaveBigFilePartRequest
from telethon.tl.types import DocumentAttributeVideo, InputFileBig


# ============================================================
# TES INFORMATIONS
# ============================================================

def required_env(name):
    value = os.environ.get(name)
    if not value:
        print(f"ERREUR : la variable d'environnement {name} n'est pas définie.")
        print("Ajoute-la dans Render : Environment > Add Environment Variable.")
        sys.exit(1)
    return value


API_ID = int(os.environ.get("API_ID", "27763934"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "8966970753"))

BOT_TITLE = "Studio Sous-titres"


# ============================================================
# DOSSIERS : réglages et fichiers temporaires dans le conteneur Render
# ============================================================
# Le disque de Render (plan gratuit) n'est pas permanent : un nouveau
# déploiement (nouveau push sur GitHub) repart de zéro. Tant que le
# service reste éveillé (grâce à UptimeRobot), rien n'est perdu entre
# deux conversations.

DATA_DIR = Path(os.environ.get("SUBBOT_DATA") or "/app/data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
DRIVE_OK = True   # conservé pour l'écran Administration ; voir DATA_DIR ci-dessus

LOCAL = Path(os.environ.get("SUBBOT_LOCAL") or "/app/local")
WORK = LOCAL / "work"
shutil.rmtree(WORK, ignore_errors=True)          # purge des anciens fichiers temporaires
WORK.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
USERS_PATH = DATA_DIR / "users.json"
RESTART_PATH = LOCAL / "restart.json"


def load_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def save_json(path, data):
    """Écriture sûre : fichier temporaire puis remplacement (jamais de fichier à moitié écrit)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        print(f"[sauvegarde] {path.name} : {e}")


# ============================================================
# CONSTANTES
# ============================================================

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".wmv"}
SUB_EXT = {".srt", ".ass", ".ssa", ".vtt"}

# Limite Telegram pour un bot : 2 Go par fichier
MAX_SIZE = 2000 * 1024 * 1024

# Transferts rapides (plusieurs requêtes en parallèle)
FAST_WORKERS = 10
FAST_CHUNK = 512 * 1024
FAST_MIN_UPLOAD = 10 * 1024 * 1024   # au-dessus : envoi rapide

MAX_JOBS_PER_USER = 3          # vidéos ouvertes en même temps par personne
JOB_TTL = 30 * 60              # une demande inactive est fermée après 30 minutes
MAX_SUB_SIZE = 5 * 1024 * 1024 # taille maximale d'un fichier de sous-titres

ACCESS_MODES = {
    "approval": "Sur approbation",
    "closed": "Admin seulement",
    "open": "Ouvert à tous",
}

# Netteté des vidéos recompressées : (libellé, préréglage x264, CRF)
SHARP_PROFILES = {
    "fast": ("Rapide", "veryfast", 21),
    "balanced": ("Équilibrée", "veryfast", 18),
    "max": ("Maximale", "fast", 17),
}
ENCODE_TUNE = "animation"    # réglage pour animes / dessins ; None pour des films
AUDIO_BITRATE = "192k"

# Compression (réservé à l'administrateur) : réduire vraiment la taille du
# fichier, quitte à perdre en qualité. (libellé, hauteur max, CRF, audio)
COMPRESS_PROFILES = {
    "light": ("Légère", 720, 26, "128k"),
    "medium": ("Moyenne", 480, 30, "96k"),
    "strong": ("Forte", 360, 34, "64k"),
}

# Polices essayées (dans cet ordre) pour les sous-titres incrustés
FONT_CANDIDATES = ("Liberation Sans", "Noto Sans", "DejaVu Sans")

# ---------- Look « télé » des sous-titres ----------
# Couleurs ASS : &HAABBGGRR (AA = transparence, 00 = opaque)
STYLE_PRESETS = {
    "tele": {
        "label": "Télé",
        "primary": "&H00FFFFFF", "outline": "&H00000000", "back": "&H90000000",
        "border": 1, "outline_w": 2, "shadow": 1,
    },
    "jaune": {
        "label": "Jaune",
        "primary": "&H0000FFFF", "outline": "&H00000000", "back": "&H90000000",
        "border": 1, "outline_w": 2, "shadow": 1,
    },
    "boite": {
        "label": "Boîte",
        "primary": "&H00FFFFFF", "outline": "&H99000000", "back": "&H00000000",
        "border": 3, "outline_w": 3, "shadow": 0,
    },
}

# Taille de police (sur une image de 288 lignes, comme les sous-titres télé)
SIZE_PRESETS = {
    "s": ("Petite", 16),
    "m": ("Moyenne", 19),
    "l": ("Grande", 23),
}

DEFAULT_PREFS = {"style": "tele", "size": "m", "fmt": "srt"}

# Codec du sous-titre -> (extension du fichier extrait, codec de sortie)
SUB_EXTRACT = {
    "subrip": (".srt", "srt"),
    "srt": (".srt", "srt"),
    "webvtt": (".srt", "srt"),
    "mov_text": (".srt", "srt"),
    "text": (".srt", "srt"),
    "ass": (".ass", "copy"),
    "ssa": (".ass", "copy"),
    "hdmv_pgs_subtitle": (".sup", "copy"),
}

# Sous-titres « texte » (les images comme PGS ne peuvent pas être gravées)
TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "webvtt", "mov_text", "text"}

LANG_HINTS = {
    "fr": "fra", "fra": "fra", "fre": "fra", "french": "fra",
    "francais": "fra", "vf": "fra", "vostfr": "fra",
    "en": "eng", "eng": "eng", "english": "eng",
    "es": "spa", "spa": "spa", "spanish": "spa",
    "pt": "por", "por": "por",
    "ar": "ara", "ara": "ara", "arabic": "ara",
    "de": "ger", "ger": "ger", "deu": "ger",
    "it": "ita", "ita": "ita",
    "ja": "jpn", "jpn": "jpn", "jp": "jpn",
}

LANG_NAMES = {
    "fra": "Français", "fre": "Français", "eng": "English", "spa": "Español",
    "por": "Português", "ara": "Arabe", "ger": "Deutsch", "deu": "Deutsch",
    "ita": "Italiano", "jpn": "Japonais", "rus": "Russe", "kor": "Coréen",
    "chi": "Chinois", "zho": "Chinois", "tur": "Turc", "pol": "Polonais",
    "dut": "Néerlandais", "nld": "Néerlandais", "ind": "Indonésien",
    "vie": "Vietnamien", "tha": "Thaï", "hin": "Hindi", "heb": "Hébreu",
    "swe": "Suédois", "ukr": "Ukrainien", "cze": "Tchèque", "ces": "Tchèque",
    "gre": "Grec", "ell": "Grec", "rum": "Roumain", "ron": "Roumain",
    "hun": "Hongrois", "fin": "Finnois", "dan": "Danois", "nor": "Norvégien",
    "may": "Malais", "msa": "Malais", "fil": "Filipino", "und": "Inconnue",
}

CODEC_LABELS = {
    "subrip": "SRT", "srt": "SRT", "ass": "ASS", "ssa": "SSA",
    "webvtt": "VTT", "mov_text": "TXT", "text": "TXT",
    "hdmv_pgs_subtitle": "PGS", "dvd_subtitle": "VOBSUB", "dvb_subtitle": "DVB",
}


# ============================================================
# RÉGLAGES ET UTILISATEURS (sauvegardés sur le Drive)
# ============================================================

DEFAULT_CONFIG = {"access": "approval", "sharp": "balanced", "auto_lang": "fra"}

cfg = {**DEFAULT_CONFIG, **load_json(CONFIG_PATH, {})}
users = load_json(USERS_PATH, {})     # "id" -> fiche de l'utilisateur


def save_config():
    save_json(CONFIG_PATH, cfg)


def save_users():
    save_json(USERS_PATH, users)


STATE = {"font": None}

jobs = {}             # jid -> demande en cours
awaiting = {}         # uid -> ce que le bot attend de cette personne
waiting = []          # jid en attente de leur tour de traitement
job_counter = itertools.count(1)
bg_tasks = set()
work_lock = asyncio.Lock()          # un seul traitement lourd à la fois
RESTART_REQUESTED = False
STARTED_AT = time.time()


# ============================================================
# CLIENT TELEGRAM
# ============================================================

client = TelegramClient(
    str(LOCAL / "bot_session"),
    API_ID,
    API_HASH,
    connection_retries=10,
    retry_delay=2,
    request_retries=8,
    auto_reconnect=True,
)


# ============================================================
# OUTILS
# ============================================================

esc = html.escape

# ============================================================
# DESIGN — présentation commune à tous les écrans
# ============================================================

DIV = "┄" * 20


def header(icon, title, sub=None):
    """En-tête d'écran : icône + titre, sous-titre discret en dessous."""
    lines = [f"<b>{icon}  {esc(title)}</b>"]
    if sub:
        lines.append(f"<i>{esc(sub)}</i>")
    return "\n".join(lines)


def kv(pairs):
    """
    Mini-tableau clé/valeur aligné (police à chasse fixe), pour un rendu
    net et régulier plutôt que du texte brut à largeur variable.
    """
    pairs = [(k, v) for k, v in pairs if v is not None]
    if not pairs:
        return ""
    width = max(len(k) for k, _ in pairs)
    body = "\n".join(f"{k}{' ' * (width - len(k))} │ {v}" for k, v in pairs)
    return "<code>" + esc(body) + "</code>"


def bullets(items, mark="▸"):
    return "\n".join(f"{mark} {esc(x)}" for x in items)




def clean_name(name):
    name = os.path.basename(name)
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name.strip() or "file"


def format_size(size):
    if not size:
        return "0 o"
    size = float(size)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024:
            if unit == "o":
                return f"{size:.0f} o"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} To"


def format_duration(seconds):
    if not seconds:
        return "N/A"
    seconds = int(float(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def fmt_time(seconds):
    seconds = int(max(seconds, 0))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


def bar(pct, width=12):
    filled = int(width * pct / 100)
    return "▰" * filled + "▱" * (width - filled)


def stamp():
    return time.strftime("%d/%m/%Y %H:%M")


# ---------- Analyse d'une vidéo ----------

def probe_all(path):
    """Une seule lecture du fichier : durée, taille de l'image, nombre d'audios, sous-titres."""
    info = {"duration": None, "width": None, "height": None, "audios": 0}
    tracks = []

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height"
            ":stream_tags=language,title:stream_disposition=forced",
            "-of", "json", str(path),
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=90)
        data = json.loads(output.decode())
    except Exception:
        return info, tracks

    if data.get("format"):
        info["duration"] = data["format"].get("duration")

    for s in data.get("streams", []):
        kind = s.get("codec_type")

        if kind == "video" and not info["width"] and s.get("width"):
            info["width"], info["height"] = s["width"], s["height"]

        elif kind == "audio":
            info["audios"] += 1

        elif kind == "subtitle":
            tags = s.get("tags") or {}
            tracks.append({
                "index": s["index"],
                "codec": s.get("codec_name") or "?",
                "lang": tags.get("language") or "und",
                "title": tags.get("title") or "",
                "forced": bool((s.get("disposition") or {}).get("forced")),
            })

    return info, tracks


def media_info(path):
    info, _ = probe_all(path)
    return info


def quality(info):
    if info["height"]:
        return f'{info["height"]}p'
    return "N/A"


def create_thumbnail(video, out_dir):
    output = Path(out_dir) / "auto.jpg"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "00:00:03", "-i", str(video),
                "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "4", str(output),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
        )
        if output.exists() and output.stat().st_size > 0:
            return str(output)
    except Exception:
        pass
    return None


# ---------- Pistes de sous-titres ----------

def track_labels(tracks):
    """Un libellé propre et unique pour chaque piste de sous-titres."""
    labels = {}
    seen = {}

    for t in tracks:
        name = LANG_NAMES.get(t["lang"]) or t["lang"].upper()
        codec = CODEC_LABELS.get(t["codec"], t["codec"].upper())

        extra = []
        title = (t["title"] or "").strip()
        if title and title.lower() != name.lower():
            extra.append(title[:16])
        if t["forced"]:
            extra.append("Forcés")

        base = name + (f" ({', '.join(extra)})" if extra else "")
        label = f"{base} · {codec}"

        seen[label] = seen.get(label, 0) + 1
        if seen[label] > 1:
            label += f" #{seen[label]}"

        labels[t["index"]] = label

    return labels


def pick_auto_track(tracks):
    """Piste texte choisie automatiquement : la langue prioritaire (réglable), sinon la meilleure piste."""
    text = [t for t in tracks if t["codec"] in TEXT_SUB_CODECS]
    if not text:
        return None

    lang_key = cfg.get("auto_lang", "fra")
    variants = AUTO_LANG_OPTIONS.get(lang_key, AUTO_LANG_OPTIONS["fra"])[1]
    preferred = [t for t in text if t["lang"] in variants] if variants else []

    def score(t):
        title = (t["title"] or "").lower()
        points = 0
        if t["forced"]:
            points -= 2
        if any(w in title for w in ("sign", "song", "forced", "forcé", "commentary")):
            points -= 2
        if any(w in title for w in ("full", "complet", "dialog")):
            points += 1
        return points

    return max(preferred or text, key=score)


def guess_lang(name):
    for token in re.split(r"[^a-z]+", name.lower()):
        if token in LANG_HINTS:
            return LANG_HINTS[token]
    return "und"


# ---------- Nettoyage des sous-titres : jamais penchés, toujours lisibles ----------

TAG_RE = re.compile(r"</?[a-zA-Z][^>\n]*>")        # <i>, </b>, <font ...>, <c.color>...
BRACE_RE = re.compile(r"\{[^}\n]*\}")               # {\an8}, {\i1}...
CUE_RE = re.compile(
    r"(\d+):(\d\d):(\d\d)[,.](\d{1,3})\s*-->\s*(\d+):(\d\d):(\d\d)[,.](\d{1,3})"
)


def decode_text(data):
    """Décode un fichier de sous-titres, même en Windows-1252 ou UTF-16."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16")

    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError:
            return data.decode("latin-1")


def clean_srt(text, strip_tags=True):
    """
    Nettoie un SRT.
    strip_tags=True : supprime tout le formatage (italique, gras, couleurs...).
    C'est ce qui garantit des sous-titres droits et uniformes, comme à la télé.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = BRACE_RE.sub("", text)
    if strip_tags:
        text = TAG_RE.sub("", text)

    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip() + "\n"


def parse_cues(text):
    """Liste des répliques : (début, fin, texte) en secondes."""
    cues = []

    def seconds(h, m, s, ms):
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000

    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [line for line in block.split("\n") if line.strip()]
        for i, line in enumerate(lines):
            m = CUE_RE.search(line)
            if m:
                g = m.groups()
                cues.append((seconds(*g[:4]), seconds(*g[4:]), "\n".join(lines[i + 1:])))
                break

    return cues


def pick_preview_time(cues):
    """Instant d'une belle réplique (2 lignes si possible) pour l'aperçu."""
    if not cues:
        return 5.0

    pool = [c for c in cues if c[2] and (c[1] - c[0]) >= 1.0] or cues
    third = pool[len(pool) // 3:] or pool
    two_lines = [c for c in third if "\n" in c[2]]
    start, end, _ = (two_lines or third)[0]
    return start + min(1.0, (end - start) / 2)


def subtitle_to_srt(raw_path, out_path, strip_tags=True):
    """
    Convertit un fichier .srt / .ass / .ssa / .vtt en SRT propre.
    Retourne le nombre de répliques. Lève ValueError si le fichier est inutilisable.
    """
    raw_path = Path(raw_path)
    out_path = Path(out_path)
    ext = raw_path.suffix.lower()

    if ext not in SUB_EXT:
        raise ValueError("Format non pris en charge (.srt, .ass, .ssa, .vtt)")

    if raw_path.stat().st_size > MAX_SUB_SIZE:
        raise ValueError("Fichier trop gros pour des sous-titres (5 Mo maximum)")

    text = decode_text(raw_path.read_bytes())

    if ext != ".srt":
        tmp_in = out_path.with_name(out_path.stem + "_in" + ext)
        tmp_out = out_path.with_name(out_path.stem + "_conv.srt")
        tmp_in.write_text(text, encoding="utf-8")

        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_in), "-c:s", "srt", str(tmp_out)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120,
        )
        if result.returncode != 0 or not tmp_out.exists():
            raise ValueError("Impossible de lire ce fichier de sous-titres")

        text = decode_text(tmp_out.read_bytes())

    text = clean_srt(text, strip_tags)
    count = len(parse_cues(text))

    if count == 0:
        raise ValueError("Aucune réplique trouvée dans ce fichier")

    out_path.write_text(text, encoding="utf-8")
    return count


# ---------- Style « télé » ----------

def sharp_key():
    key = cfg.get("sharp")
    return key if key in SHARP_PROFILES else "balanced"


def style_string(style_key, size_key):
    """Style ASS complet : droit, gras, contour net, marges de sécurité télé."""
    st = STYLE_PRESETS.get(style_key) or STYLE_PRESETS["tele"]
    size = SIZE_PRESETS.get(size_key) or SIZE_PRESETS["m"]
    font = STATE.get("font") or "DejaVu Sans"

    return (
        f"FontName={font},FontSize={size[1]},Bold=1,Italic=0,Underline=0,StrikeOut=0,"
        f"PrimaryColour={st['primary']},SecondaryColour={st['primary']},"
        f"OutlineColour={st['outline']},BackColour={st['back']},"
        f"BorderStyle={st['border']},Outline={st['outline_w']},Shadow={st['shadow']},"
        "Alignment=2,MarginL=24,MarginR=24,MarginV=16"
    )


def subtitle_filter(srt_path, style_key, size_key):
    """Filtre ffmpeg qui grave un fichier SRT dans l'image."""
    return f"subtitles={srt_path}:force_style='{style_string(style_key, size_key)}'"


def detect_gpu():
    """
    Teste si ffmpeg peut réellement encoder avec le GPU Nvidia (NVENC) sur cette
    machine Colab. Un vrai mini-encodage est lancé, pas juste une liste de codecs :
    ffmpeg peut connaître NVENC sans qu'un GPU soit présent ou activé.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
                "-i", "nullsrc=s=320x240:d=0.2",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
        )
        return result.returncode == 0
    except Exception:
        return False


def video_codec_args(vf=None, min_profile=None, force_cpu=False):
    """
    Réglages d'encodage vidéo/audio selon la netteté choisie.
    min_profile : netteté minimale garantie, quel que soit le réglage.
    force_cpu : ignore le GPU même s'il est disponible (utilisé en repli
    si l'encodage GPU échoue en cours de traitement).
    """
    order = list(SHARP_PROFILES)
    key = sharp_key()
    if min_profile and order.index(key) < order.index(min_profile):
        key = min_profile

    _, preset, crf = SHARP_PROFILES[key]

    if STATE.get("gpu") and not force_cpu:
        # NVENC : encodage matériel Nvidia, beaucoup plus rapide que le CPU.
        # La gravure des sous-titres (-vf) reste calculée sur le CPU, seul
        # l'encodage final profite du GPU.
        args = [
            "-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr",
            "-cq", str(crf), "-b:v", "0", "-profile:v", "high", "-pix_fmt", "yuv420p",
        ]
    else:
        args = ["-c:v", "libx264", "-preset", preset, "-crf", str(crf)]
        if ENCODE_TUNE:
            args += ["-tune", ENCODE_TUNE]
        args += ["-profile:v", "high", "-pix_fmt", "yuv420p"]

    if vf:
        args += ["-vf", vf]
    args += ["-c:a", "aac", "-b:a", AUDIO_BITRATE]
    return args


# ---------- Polices ----------

def detect_font():
    try:
        out = subprocess.check_output(
            ["fc-list", ":", "family"],
            text=True, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        return None

    low = out.lower()
    for name in FONT_CANDIDATES:
        if name.lower() in low:
            return name
    return None


def ensure_fonts():
    """
    Installe une belle police si elle manque : sans elle, les sous-titres
    incrustés utilisent une police de secours souvent laide ou penchée.
    """
    font = detect_font()
    if font == FONT_CANDIDATES[0]:
        return font

    try:
        for cmd in (
            ["apt-get", "update", "-qq"],
            ["apt-get", "install", "-y", "-qq", "fonts-liberation"],
            ["fc-cache", "-f"],
        ):
            subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
    except Exception:
        pass

    return detect_font() or font


# ---------- Divers ----------

def make_unique(path):
    """
    Ajoute quelques octets invisibles à la fin d'une vidéo (MP4 / MKV).
    Sans ça, Telegram reconnaît un fichier identique déjà envoyé et
    réutilise l'ancien (ancienne miniature, ancien nom, ancien type).
    """
    ext = Path(path).suffix.lower()
    payload = os.urandom(16)

    if ext in (".mp4", ".m4v", ".mov"):
        tail = struct.pack(">I", 8 + len(payload)) + b"free" + payload
    elif ext in (".mkv", ".webm"):
        tail = b"\xec\x90" + payload
    else:
        return

    with open(path, "ab") as f:
        f.write(tail)


PROGRESS_KEYS = (
    "frame=", "fps=", "stream_", "bitrate=", "total_size=", "out_time",
    "dup_frames=", "drop_frames=", "speed=", "progress=",
)


async def run_ffmpeg(args, duration=None, on_pct=None):
    """
    Lance ffmpeg sans bloquer le bot, avec progression.
    Retourne (succès, message d'erreur). Peut être interrompu.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-nostdin", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    errors = []

    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break

            line = raw.decode(errors="ignore").strip()
            if not line:
                continue

            if line.startswith("out_time_ms="):
                if on_pct and duration:
                    try:
                        seconds = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    await on_pct(max(0, min(seconds / duration * 100, 100)))
            elif not line.startswith(PROGRESS_KEYS):
                errors.append(line)

        code = await proc.wait()

    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
        raise

    return code == 0, " | ".join(errors[-3:])[:300]


def run_in_background(coro):
    task = asyncio.create_task(coro)
    bg_tasks.add(task)
    task.add_done_callback(bg_tasks.discard)
    return task


def safe(handler):
    """Une erreur inattendue dans un bouton ou un message ne bloque jamais le bot."""

    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        try:
            return await handler(event, *args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[erreur] {handler.__name__} : {type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                if hasattr(event, "data"):
                    await event.answer("Une erreur est survenue, réessaie.")
                else:
                    await toast(event.chat_id, "Une erreur est survenue, réessaie.")
            except Exception:
                pass

    return wrapper


async def send_retry(*args, attempts=3, **kwargs):
    """Envoi d'un fichier : une coupure réseau passagère est réessayée."""
    for n in range(attempts):
        try:
            return await client.send_file(*args, **kwargs)
        except (asyncio.CancelledError, FloodWaitError):
            raise
        except (ConnectionError, OSError, asyncio.TimeoutError) as e:
            if n == attempts - 1:
                raise
            print(f"[réseau] envoi interrompu ({type(e).__name__}) : nouvelle tentative {n + 2}/{attempts}")
            await asyncio.sleep(2 * (n + 1))


# ============================================================
# BOT API — panneaux à boutons colorés
# ============================================================
# Telethon reçoit les messages et transfère les fichiers.
# L'interface (panneaux, boutons) passe par l'API HTTP des bots.

class ApiError(Exception):
    pass


http = None


async def bot_api(method, **params):
    """
    Appel à l'API des bots. Une coupure réseau passagère (« Connection reset
    by peer »...) est réessayée automatiquement au lieu de faire échouer
    le traitement en cours.
    """
    global http

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    network_errors = (aiohttp.ClientError, asyncio.TimeoutError, OSError)
    last_error = None

    for attempt in range(5):
        try:
            if http is None or http.closed:
                http = aiohttp.ClientSession(
                    connector=aiohttp.TCPConnector(keepalive_timeout=10),
                    timeout=aiohttp.ClientTimeout(total=60),
                )

            async with http.post(url, json=params) as resp:
                data = await resp.json(content_type=None)

        except network_errors as e:
            last_error = e
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        if data.get("ok"):
            return data["result"]

        retry = (data.get("parameters") or {}).get("retry_after")
        if retry and attempt < 4:
            await asyncio.sleep(retry + 1)
            continue

        raise ApiError(data.get("description", "erreur inconnue"))

    raise ApiError(f"réseau indisponible ({type(last_error).__name__})")


def btn(label, data, style=None):
    """Un bouton. style : 'primary' (bleu), 'success' (vert), 'danger' (rouge)."""
    return {"text": label, "data": data, "style": style}


def render_markup(rows):
    keyboard = []
    for row in rows or []:
        line = []
        for b in row:
            item = {"text": b["text"], "callback_data": b["data"]}
            if b.get("style"):
                item["style"] = b["style"]
            line.append(item)
        keyboard.append(line)
    return {"inline_keyboard": keyboard}


async def deliver(method, base, text, rows):
    """Envoie ou modifie un message."""
    payload = dict(base)
    payload["text"] = text
    payload["parse_mode"] = "HTML"
    payload["link_preview_options"] = {"is_disabled": True}
    if rows:
        payload["reply_markup"] = render_markup(rows)

    return await bot_api(method, **payload)


class Panel:
    """Un message du bot qu'on met à jour au lieu d'en envoyer de nouveaux."""

    def __init__(self, chat_id, message_id):
        self.chat_id = chat_id
        self.message_id = message_id

    async def edit(self, text, rows=None):
        try:
            await deliver(
                "editMessageText",
                {"chat_id": self.chat_id, "message_id": self.message_id},
                text,
                rows,
            )
        except ApiError as e:
            if "not modified" not in str(e).lower():
                print(f"[panneau] {e}")

    async def delete(self):
        try:
            await bot_api("deleteMessage", chat_id=self.chat_id, message_id=self.message_id)
        except ApiError:
            pass


async def send_panel(chat_id, text, rows=None):
    result = await deliver("sendMessage", {"chat_id": chat_id}, text, rows)
    return Panel(chat_id, result["message_id"])


async def toast(chat_id, text, seconds=3.5):
    """Petit message qui disparaît tout seul."""
    try:
        panel = await send_panel(chat_id, f"<i>{esc(text)}</i>")
    except ApiError:
        return

    async def later():
        await asyncio.sleep(seconds)
        await panel.delete()

    run_in_background(later())


async def wipe(event):
    """Supprime le message de l'utilisateur pour garder la conversation propre."""
    try:
        await event.delete()
    except Exception:
        pass


async def set_commands():
    """Menu des commandes : simple pour tout le monde, complet pour l'admin."""
    user_cmds = [
        {"command": "start", "description": "Accueil"},
        {"command": "style", "description": "Style des sous-titres"},
        {"command": "help", "description": "Aide"},
        {"command": "cancel", "description": "Annuler l'opération"},
    ]
    admin_cmds = user_cmds + [
        {"command": "admin", "description": "Administration"},
        {"command": "users", "description": "Utilisateurs"},
        {"command": "broadcast", "description": "Message à tous les utilisateurs"},
        {"command": "restart", "description": "Redémarrer le bot"},
    ]

    try:
        await bot_api("setMyCommands", commands=user_cmds)
    except ApiError as e:
        print(f"[commandes] {e}")

    try:
        await bot_api(
            "setMyCommands",
            commands=admin_cmds,
            scope={"type": "chat", "chat_id": ADMIN_ID},
        )
    except ApiError as e:
        print(f"[commandes admin] {e} (envoie /start au bot depuis ton compte, puis /restart)")


# ============================================================
# PROGRESSION
# ============================================================

class Progress:
    """Barre de progression avec vitesse et temps restant."""

    def __init__(self, job):
        self.job = job
        self.title = ""
        self.hint = ""
        self.last = 0.0
        self.active = True
        self.t0 = time.time()
        self.prev = (self.t0, 0)

    def _text(self, pct, info=""):
        text = (
            header("▶", self.title)
            + f"\n{DIV}\n"
            + f"<blockquote>{esc(self.job['filename'])}</blockquote>\n"
            + f"{bar(pct)}  <b>{pct:.0f}%</b>"
        )
        if info:
            text += f"\n{kv([('Détail', info)])}"
        if self.hint:
            text += f"\n<i>{esc(self.hint)}</i>"
        return text

    async def _push(self, text):
        if self.active:
            jid = self.job["id"]
            await self.job["panel"].edit(
                text,
                [[btn("Arrêter", f"stop|{jid}", "danger")]],
            )

    async def start(self, title, hint=""):
        self.title = title
        self.hint = hint
        self.t0 = time.time()
        self.prev = (self.t0, 0)
        self.last = self.t0
        await self._push(self._text(0))

    def telethon(self, current, total):
        now = time.time()
        if now - self.last < 3 and current < total:
            return

        prev_t, prev_c = self.prev
        speed = (current - prev_c) / (now - prev_t) if now > prev_t else 0
        self.prev = (now, current)
        self.last = now

        pct = (current * 100 / total) if total else 0

        parts = [f"{format_size(current)} / {format_size(total)}"]
        if speed > 0:
            parts.append(f"{format_size(speed)}/s")
            if total:
                parts.append("reste " + fmt_time((total - current) / speed))

        asyncio.ensure_future(self._push(self._text(pct, " · ".join(parts))))

    async def pct(self, value):
        now = time.time()
        if now - self.last < 3:
            return
        self.last = now

        info = ""
        if value > 1:
            elapsed = now - self.t0
            info = "reste " + fmt_time(elapsed * (100 - value) / value)

        await self._push(self._text(value, info))

    def stop(self):
        self.active = False


# ============================================================
# TRANSFERTS RAPIDES (plusieurs requêtes en parallèle)
# ============================================================
# Au lieu de demander le fichier morceau par morceau, le bot en
# demande plusieurs à la fois. Si quelque chose ne va pas, il
# repasse automatiquement en mode classique.

async def fast_download(msg, dest, size, on_progress=None, workers=FAST_WORKERS):
    """Télécharge un fichier avec plusieurs requêtes en même temps."""
    if not size:
        raise ValueError("taille inconnue")

    dc_id, location = utils.get_input_location(msg.media)

    n_chunks = (size + FAST_CHUNK - 1) // FAST_CHUNK
    workers = max(1, min(workers, n_chunks))
    stride = workers * FAST_CHUNK
    done = 0

    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_TRUNC)

    async def worker(i):
        nonlocal done

        count = (n_chunks - i + workers - 1) // workers   # blocs de ce worker
        offset = i * FAST_CHUNK
        k = 0

        it = client.iter_download(
            location,
            offset=offset,
            stride=stride,
            chunk_size=FAST_CHUNK,
            request_size=FAST_CHUNK,
            dc_id=dc_id,
        )

        try:
            async for chunk in it:
                if not chunk:
                    break

                os.pwrite(fd, chunk, offset + k * stride)
                k += 1
                done += len(chunk)

                if on_progress:
                    on_progress(done, size)

                if k >= count:
                    break
        finally:
            try:
                await it.close()
            except Exception:
                pass

    try:
        tasks = [asyncio.create_task(worker(i)) for i in range(workers)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
    finally:
        os.close(fd)

    if os.path.getsize(dest) != size:
        raise RuntimeError("téléchargement incomplet")

    await verify_samples(location, dc_id, dest, size, n_chunks)


async def verify_samples(location, dc_id, dest, size, n_chunks):
    """
    Vérifie le début, le milieu et la fin du fichier en les redemandant
    à Telegram un par un. Si quelque chose ne colle pas, le bot refait
    le téléchargement en mode classique.
    """
    picks = sorted({0, n_chunks // 2, n_chunks - 1})
    fd = os.open(str(dest), os.O_RDONLY)

    try:
        for index in picks:
            offset = index * FAST_CHUNK
            expected = os.pread(fd, min(FAST_CHUNK, size - offset), offset)

            it = client.iter_download(
                location,
                offset=offset,
                chunk_size=FAST_CHUNK,
                request_size=FAST_CHUNK,
                limit=1,
                dc_id=dc_id,
            )

            got = b""
            try:
                async for chunk in it:
                    got = chunk
                    break
            finally:
                try:
                    await it.close()
                except Exception:
                    pass

            if got != expected:
                raise RuntimeError("vérification du téléchargement échouée")
    finally:
        os.close(fd)


async def fast_upload(path, on_progress=None, workers=FAST_WORKERS):
    """
    Envoie un fichier avec plusieurs requêtes en même temps.
    Retourne un fichier prêt à être passé à send_file.
    """
    path = Path(path)
    size = path.stat().st_size
    part_size = FAST_CHUNK
    parts = (size + part_size - 1) // part_size

    if parts > 4000:
        raise ValueError("fichier trop gros")

    file_id = int.from_bytes(os.urandom(8), "big", signed=True)
    sem = asyncio.Semaphore(workers)
    sent = 0
    tasks = []

    async def push(index, data):
        nonlocal sent
        try:
            await client(SaveBigFilePartRequest(
                file_id=file_id,
                file_part=index,
                file_total_parts=parts,
                bytes=data,
            ))
            sent += len(data)
            if on_progress:
                on_progress(sent, size)
        finally:
            sem.release()

    try:
        with open(path, "rb") as f:
            for index in range(parts):
                await sem.acquire()

                # Une erreur est déjà survenue : on arrête tout de suite
                for t in tasks:
                    if t.done() and not t.cancelled() and t.exception():
                        sem.release()
                        raise t.exception()

                data = f.read(part_size)
                tasks.append(asyncio.create_task(push(index, data)))

        await asyncio.gather(*tasks)

    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    return InputFileBig(id=file_id, parts=parts, name=path.name)


async def upload_handle(path, on_progress=None):
    """
    Prépare l'envoi : rapide en parallèle pour les fichiers de plus de 10 Mo,
    sinon (ou en cas de souci) on renvoie simplement le chemin du fichier.
    """
    size = os.path.getsize(path)

    if size < FAST_MIN_UPLOAD:
        return str(path)

    t0 = time.time()

    try:
        handle = await fast_upload(path, on_progress)
    except asyncio.CancelledError:
        raise
    except FloodWaitError:
        raise
    except Exception as e:
        print(f"[rapide] envoi en parallèle impossible ({type(e).__name__}: {e}) -> mode classique")
        return str(path)

    seconds = max(time.time() - t0, 0.001)
    print(f"[envoi] {format_size(size)} en {seconds:.1f} s ({format_size(size / seconds)}/s)")
    return handle


dl_sem = asyncio.Semaphore(1)   # 1 seul téléchargement en avance à la fois, même avec plusieurs utilisateurs


def low_memory():
    """
    True si la RAM libre est critique. Sert à repousser les téléchargements
    en avance quand plusieurs personnes utilisent le bot en même temps.
    """
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemAvailable", "MemFree"):
                    info[key] = int(rest.strip().split()[0]) * 1024
        free = info.get("MemAvailable", info.get("MemFree", 0))
        return 0 < free < 1200 * 1024 * 1024   # moins de 1,2 Go disponible
    except Exception:
        return False


async def download_source(job, on_progress=None):
    """Télécharge la vidéo d'origine (rapide, avec repli classique)."""
    path = job["dir"] / ("src_" + clean_name(job["filename"]))

    msg = job.get("orig")
    if msg is None:
        msg = await client.get_messages(job["chat_id"], ids=job["message_id"])
        if not msg:
            raise RuntimeError("Message d'origine introuvable (supprimé ?)")
        job["orig"] = msg

    size = job["size"]

    def report(done, total):
        job["dl"]["done"] = done
        job["dl"]["total"] = total or size
        if on_progress:
            on_progress(done, total or size)

    t0 = time.time()

    try:
        await fast_download(msg, path, size, report)
    except asyncio.CancelledError:
        raise
    except FloodWaitError:
        raise
    except Exception as e:
        print(f"[rapide] téléchargement en parallèle impossible ({type(e).__name__}: {e}) -> mode classique")
        path.unlink(missing_ok=True)
        job["dl"]["done"] = 0
        await client.download_media(msg, file=str(path), progress_callback=report)

    seconds = max(time.time() - t0, 0.001)
    print(f"[téléchargement] {format_size(size)} en {seconds:.1f} s ({format_size(size / seconds)}/s)")

    job.setdefault("times", {})["download"] = seconds
    job["src"] = str(path)
    return path


async def predownload(job):
    """
    Télécharge la vidéo en arrière-plan pendant que tu choisis tes options,
    puis l'analyse et met le panneau à jour (« Prêt · 1080p · 23:45 · … »).
    Si la mémoire de la machine est momentanément critique (plusieurs
    personnes actives en même temps), le téléchargement patiente plutôt
    que de risquer de faire planter toute la session.
    """
    for _ in range(60):          # jusqu'à 2 minutes d'attente, par pas de 2 s
        if not low_memory() or job["id"] not in jobs:
            break
        await asyncio.sleep(2)

    if job["id"] not in jobs:
        return

    async with dl_sem:
        path = await download_source(job)

    info, tracks = await asyncio.to_thread(probe_all, path)
    job["info_src"] = info
    if job["tracks"] is None:
        job["tracks"] = tracks

    job["ready"] = True
    await refresh_if_idle(job)


# ============================================================
# GESTION DES DEMANDES
# ============================================================

def touch(job):
    job["touched"] = time.time()


def drop_job(jid):
    """Supprime une demande et tous ses fichiers temporaires."""
    job = jobs.pop(jid, None)

    if jid in waiting:
        waiting.remove(jid)

    if job:
        aw = awaiting.get(job["uid"])
        if aw and aw.get("jid") == jid:
            awaiting.pop(job["uid"], None)

        task = job.get("dl_task")
        if task and not task.done():
            task.cancel()

        shutil.rmtree(job["dir"], ignore_errors=True)

    return job


def jobs_of(uid):
    return [j for j in jobs.values() if j["uid"] == uid]


async def ensure_source(job, progress):
    """
    Retourne la vidéo d'origine sur le disque.
    Elle est déjà téléchargée (ou en cours) grâce au téléchargement en avance.
    """
    src = job.get("src")
    if src and Path(src).exists():
        return Path(src)

    task = job.get("dl_task")

    # Le téléchargement en avance est en cours : on attend en montrant l'avancement
    if task and not task.done():
        await progress.start("Téléchargement")

        while not task.done():
            await asyncio.wait({task}, timeout=2)
            progress.telethon(job["dl"]["done"], job["dl"]["total"])

    if task and task.done() and not task.cancelled() and task.exception() is None:
        src = job.get("src")
        if src and Path(src).exists():
            return Path(src)

    # Repli : téléchargement direct
    await progress.start("Téléchargement")
    return await download_source(job, progress.telethon)


class Turn:
    """
    Attend son tour : un seul traitement lourd à la fois pour tout le monde.
    Affiche la position dans la file quand il faut patienter.
    """

    def __init__(self, job):
        self.job = job

    async def __aenter__(self):
        job = self.job

        if work_lock.locked():
            waiting.append(job["id"])
            place = len(waiting)
            await job["panel"].edit(
                header("⏳", "En attente")
                + f"\n{DIV}\n"
                + f"<blockquote>{esc(job['filename'])}</blockquote>\n"
                + f"<i>Position {place} dans la file. Ça démarre tout seul.</i>",
                [[btn("Arrêter", f"stop|{job['id']}", "danger")]],
            )

        await work_lock.acquire()

        if job["id"] in waiting:
            waiting.remove(job["id"])

        return self

    async def __aexit__(self, *exc):
        work_lock.release()
        return False


# ============================================================
# ACCÈS ET UTILISATEURS
# ============================================================

STATUS_LABELS = {
    "pending": "En attente",
    "approved": "Approuvé",
    "refused": "Refusé",
    "blocked": "Bloqué",
    "open": "Libre",
    "admin": "Admin",
}

ACCESS_SHORT = {"approval": "Approbation", "closed": "Fermé", "open": "Ouvert"}

# Langue prioritaire pour le choix automatique du Hardsub, réglable dans Administration
AUTO_LANG_OPTIONS = {
    "fra": ("Français", ("fra", "fre", "fr")),
    "eng": ("English", ("eng", "en")),
    "spa": ("Español", ("spa", "es")),
    "por": ("Português", ("por", "pt")),
    "ara": ("Arabe", ("ara", "ar")),
    "any": ("Aucune priorité", ()),
}

notice_at = {}          # (uid, sorte) -> dernier envoi (évite de répéter les mêmes messages)


def display_name(sender):
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    name = (first + " " + last).strip()
    return name or getattr(sender, "username", None) or "Utilisateur"


def register_user(uid, sender=None, status="pending"):
    """Crée ou met à jour la fiche d'une personne."""
    rec = users.get(str(uid))

    if not rec:
        rec = {
            "status": status, "since": time.time(), "decided": 0,
            "name": "Utilisateur", "username": "", "uses": 0,
            **DEFAULT_PREFS,
        }
        users[str(uid)] = rec

    if sender is not None:
        rec["name"] = display_name(sender)
        rec["username"] = getattr(sender, "username", None) or ""

    save_users()
    return rec


def role_of(uid):
    """admin, approved, pending, refused, blocked, closed ou unknown."""
    if uid == ADMIN_ID:
        return "admin"

    rec = users.get(str(uid))

    if rec and rec["status"] == "blocked":
        return "blocked"

    mode = cfg["access"]

    if mode == "open":
        return "approved"

    if rec and rec["status"] == "approved":
        return "approved"

    if mode == "closed":
        return "closed"

    if rec and rec["status"] in ("pending", "refused"):
        return rec["status"]

    return "unknown"


def prefs(uid):
    """Style, taille et format d'extraction choisis par la personne."""
    rec = users.get(str(uid)) or {}
    out = dict(DEFAULT_PREFS)

    if rec.get("style") in STYLE_PRESETS:
        out["style"] = rec["style"]
    if rec.get("size") in SIZE_PRESETS:
        out["size"] = rec["size"]
    if rec.get("fmt") in ("srt", "orig"):
        out["fmt"] = rec["fmt"]

    return out


def set_pref(uid, key, value):
    rec = users.get(str(uid))
    if not rec:
        rec = register_user(uid, None, "admin" if uid == ADMIN_ID else "open")
    rec[key] = value
    save_users()


def counts():
    tally = {}
    for rec in users.values():
        tally[rec["status"]] = tally.get(rec["status"], 0) + 1
    return tally


def recipients():
    """Toutes les personnes joignables pour une diffusion (sans l'admin ni les bloqués)."""
    return [
        int(u) for u, r in users.items()
        if r["status"] in ("approved", "open") and int(u) != ADMIN_ID
    ]


def cooldown(uid, kind, seconds):
    """True si ce message n'a pas été envoyé récemment à cette personne."""
    now = time.time()
    if now - notice_at.get((uid, kind), 0) < seconds:
        return False
    notice_at[(uid, kind)] = now
    return True


async def notify_admin_request(uid):
    rec = users.get(str(uid))
    if not rec:
        return

    who = rec["name"]
    if rec["username"]:
        who += f" (@{rec['username']})"

    try:
        await send_panel(
            ADMIN_ID,
            header("◆", "Nouvelle demande d'accès")
            + f"\n{DIV}\n"
            + kv([("Nom", who), ("ID", uid)])
            + "\n\n<i>Cette personne souhaite utiliser le bot.</i>",
            [
                [
                    btn("Approuver", f"adm|set|{uid}|approved|r", "success"),
                    btn("Refuser", f"adm|set|{uid}|refused|r"),
                ],
                [btn("Bloquer", f"adm|set|{uid}|blocked|r", "danger")],
            ],
        )
    except ApiError as e:
        print(f"[admin] impossible de prévenir l'admin : {e} (envoie /start au bot depuis ton compte)")


async def gate(event):
    """
    True si la personne peut utiliser le bot.
    Sinon, gère la demande d'accès (message, prévenir l'admin) et retourne False.
    """
    uid = event.sender_id
    role = role_of(uid)

    if role == "admin":
        if str(uid) not in users:
            register_user(uid, None, "admin")
        return True

    if role == "blocked":
        return False

    if role == "approved":
        if str(uid) not in users:
            try:
                sender = await event.get_sender()
            except Exception:
                sender = None
            register_user(uid, sender, "open")
        return True

    chat = event.chat_id

    if role == "closed":
        if cooldown(uid, "closed", 600):
            await send_panel(
                chat,
                header("◆", BOT_TITLE) + f"\n{DIV}\n<i>Ce bot est privé pour le moment.</i>",
            )
        return False

    if role == "unknown":
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None

        register_user(uid, sender, "pending")
        await send_panel(
            chat,
            header("◆", BOT_TITLE, "Accès sur approbation")
            + f"\n{DIV}\n"
            + "Ta demande a été envoyée à l'administrateur.\n"
            "<i>Tu seras prévenu dès qu'elle sera acceptée.</i>",
        )
        await notify_admin_request(uid)
        return False

    if role == "pending":
        if cooldown(uid, "pending", 60):
            await send_panel(
                chat,
                header("⏳", BOT_TITLE) + f"\n{DIV}\n<i>Ta demande est toujours en attente de validation.</i>",
            )
        return False

    if role == "refused":
        rec = users[str(uid)]

        # Nouvelle demande possible après 24 h
        if time.time() - rec.get("decided", 0) > 86400:
            rec["status"] = "pending"
            save_users()
            await send_panel(
                chat,
                header("◆", BOT_TITLE) + f"\n{DIV}\n<i>Nouvelle demande envoyée à l'administrateur.</i>",
            )
            await notify_admin_request(uid)
        elif cooldown(uid, "refused", 600):
            await send_panel(
                chat,
                header("✕", BOT_TITLE) + f"\n{DIV}\n<i>Ta demande d'accès a été refusée.</i>",
            )
        return False

    return False


async def decide(uid, status):
    """Applique la décision de l'admin et prévient la personne."""
    rec = users.get(str(uid))
    if not rec:
        return

    rec["status"] = status
    rec["decided"] = time.time()
    save_users()

    try:
        if status == "approved":
            await send_panel(
                uid,
                header("✓", "Accès accordé") + f"\n{DIV}\n\n" + home_text(uid),
                home_rows(uid),
            )
        elif status == "refused":
            await send_panel(
                uid,
                header("✕", BOT_TITLE) + f"\n{DIV}\n<i>Ta demande d'accès a été refusée.</i>",
            )
    except ApiError as e:
        print(f"[accès] impossible de prévenir {uid} : {e}")


# ============================================================
# ÉCRANS : ACCUEIL, AIDE, STYLE
# ============================================================

def style_label(style_key, size_key):
    style = STYLE_PRESETS[style_key]["label"]
    return f"{style} · {SIZE_PRESETS[size_key][0]}"


def home_text(uid):
    p = prefs(uid)
    fmt = "SRT" if p["fmt"] == "srt" else "Original"

    return (
        header("✦", BOT_TITLE, "Hardsub et sous-titres, prêts pour Telegram")
        + f"\n{DIV}\n"
        + kv([("Style", style_label(p["style"], p["size"])), ("Extraction", fmt)])
        + "\n\n▶ Envoie une vidéo pour commencer."
    )


def home_rows(uid):
    rows = [[
        btn("Style des sous-titres", "nav|style", "primary"),
        btn("Aide", "nav|help"),
    ]]
    if uid == ADMIN_ID:
        rows.append([btn("Administration", "nav|admin")])
    return rows


def help_text():
    steps = [
        "Envoie une vidéo.",
        "Choisis Hardsub ou Télécharger les sous-titres.",
        "Hardsub : choisis les sous-titres (vidéo ou ton fichier), le style, puis lance.",
        "Tu reçois une vidéo avec des sous-titres nets, comme à la télé.",
    ]
    return (
        header("▸", "Comment ça marche")
        + f"\n{DIV}\n"
        + "\n".join(f"{i}. {esc(s)}" for i, s in enumerate(steps, 1))
        + "\n\n<i>Tes messages sont supprimés au fur et à mesure "
        "pour garder la conversation propre.</i>"
    )


def help_rows():
    return [[btn("Retour", "nav|home")]]


def style_text(uid):
    p = prefs(uid)
    fmt = "SRT (recommandé)" if p["fmt"] == "srt" else "Original (ASS, PGS…)"

    return (
        header("◆", "Style des sous-titres")
        + f"\n{DIV}\n"
        + kv([
            ("Style", STYLE_PRESETS[p["style"]]["label"]),
            ("Taille", SIZE_PRESETS[p["size"]][0]),
            ("Extraction", fmt),
        ])
        + "\n\n<i>Texte droit, gras, avec contour : lisible sur toutes les images. "
        "Appuie sur Aperçu pour voir le rendu.</i>"
    )


def style_rows(uid):
    p = prefs(uid)

    def sel(active):
        return "primary" if active else None

    return [
        [btn(v["label"], f"pref|style|{k}", sel(p["style"] == k)) for k, v in STYLE_PRESETS.items()],
        [btn(name, f"pref|size|{k}", sel(p["size"] == k)) for k, (name, _) in SIZE_PRESETS.items()],
        [
            btn("Extraction : SRT", "pref|fmt|srt", sel(p["fmt"] == "srt")),
            btn("Extraction : Original", "pref|fmt|orig", sel(p["fmt"] == "orig")),
        ],
        [btn("Aperçu", "pref|preview", "success"), btn("Retour", "nav|home")],
    ]


# ============================================================
# ÉCRANS : UNE VIDÉO
# ============================================================

def status_pairs(job):
    """Paires clé/valeur décrivant l'état de la vidéo, pour le tableau aligné."""
    if not job["ready"]:
        return [("État", "Téléchargement en cours…")]

    info = job.get("info_src")
    if not info:
        return [("État", "Prête")]

    pairs = [("Taille", format_size(job["size"]))]
    if info["height"]:
        pairs.append(("Image", f"{info['height']}p"))
    if info["duration"]:
        pairs.append(("Durée", format_duration(info["duration"])))
    if info["audios"]:
        pairs.append(("Audio", f"{info['audios']} piste" + ("s" if info["audios"] > 1 else "")))
    n = len(job["tracks"] or [])
    pairs.append(("Sous-titres", f"{n} piste" + ("s" if n > 1 else "") if n else "aucun"))
    return pairs


SCREEN_TITLES = {
    "menu": ("▪", "Nouvelle vidéo"),
    "extract": ("⇩", "Télécharger les sous-titres"),
    "src": ("◆", "Source des sous-titres"),
    "hs": ("▶", "Hardsub"),
    "compress": ("⇲", "Compresser"),
}


def job_text(job, note=""):
    view = job["view"]
    icon, title = SCREEN_TITLES[view]

    lines = [
        header(icon, title),
        DIV,
        f"<blockquote>{esc(job['filename'])}</blockquote>",
        kv(status_pairs(job)),
    ]

    if view == "menu":
        lines += ["", "Que veux-tu faire ?"]

    elif view == "extract":
        tracks = job["tracks"] or []
        fmt = "SRT" if job["fmt"] == "srt" else "Original"

        if tracks:
            lines += ["", kv([("Pistes", len(tracks)), ("Format", fmt)])]
            lines += ["", "Appuie sur une piste pour la recevoir."]
        else:
            lines += ["", "<i>Aucun sous-titre dans cette vidéo.</i>"]

    elif view == "src":
        if awaiting.get(job["uid"], {}).get("jid") == job["id"]:
            lines += [
                "",
                "<b>Envoie maintenant ton fichier de sous-titres.</b>",
                "<i>Formats : .srt · .ass · .ssa · .vtt</i>",
            ]
        else:
            has_text = any(t["codec"] in TEXT_SUB_CODECS for t in (job["tracks"] or []))
            if has_text:
                lines += ["", "Choisis une piste de la vidéo, ou envoie ton propre fichier."]
            else:
                lines += ["", "<i>Aucune piste texte : envoie ton fichier de sous-titres.</i>"]

    elif view == "hs":
        lines += [
            "",
            kv([
                ("Sous-titres", job["srt_label"] or "Aucun"),
                ("Style", style_label(job["style"], job["fsize"])),
            ]),
            "",
            "<i>Sous-titres gravés dans l'image : droits, gras, avec contour, "
            "comme à la télé.</i>",
        ]

    elif view == "compress":
        _, h, crf, ab = COMPRESS_PROFILES[job["compress"]]
        lines += [
            "",
            kv([("Qualité", COMPRESS_PROFILES[job["compress"]][0]), ("Cible", f"{h}p")]),
            "",
            "<i>Réduit nettement la taille du fichier (image et son moins fins).</i>",
        ]

    if note:
        lines += ["", note]

    return "\n".join(lines)


def job_rows(job):
    jid = job["id"]
    view = job["view"]

    def sel(active):
        return "primary" if active else None

    if view == "menu":
        rows = [
            [
                btn("Télécharger les sous-titres", f"scan|{jid}"),
                btn("Hardsub", f"hsopen|{jid}", "primary"),
            ],
        ]
        if job["uid"] == ADMIN_ID:
            rows.append([btn("Compresser", f"cmpopen|{jid}")])
        rows.append([btn("Annuler", f"cancel|{jid}", "danger")])
        return rows

    if view == "compress":
        rows = [[
            btn(v[0], f"cmpq|{jid}|{k}", sel(job["compress"] == k))
            for k, v in COMPRESS_PROFILES.items()
        ]]
        rows.append([
            btn("Lancer", f"cmprun|{jid}", "success"),
            btn("Retour", f"view|{jid}|menu"),
        ])
        return rows

    if view == "extract":
        tracks = job["tracks"] or []
        labels = track_labels(tracks)
        rows = []

        if tracks:
            rows.append([
                btn("Format SRT", f"xfmt|{jid}|srt", sel(job["fmt"] == "srt")),
                btn("Format Original", f"xfmt|{jid}|orig", sel(job["fmt"] == "orig")),
            ])
            rows.append([btn(f"Tout télécharger ({len(tracks)})", f"xtall|{jid}", "primary")])

            line = []
            for t in tracks:
                done = t["index"] in job["extracted"]
                line.append(btn(
                    labels[t["index"]],
                    f"xt|{jid}|{t['index']}",
                    "success" if done else None,
                ))
                if len(line) == 2:
                    rows.append(line)
                    line = []
            if line:
                rows.append(line)

        rows.append([btn("Retour", f"view|{jid}|menu")])
        return rows

    if view == "src":
        tracks = job["tracks"] or []
        text_tracks = [t for t in tracks if t["codec"] in TEXT_SUB_CODECS]
        labels = track_labels(tracks)
        rows = []

        auto = pick_auto_track(tracks)
        if auto:
            name = LANG_NAMES.get(auto["lang"]) or auto["lang"].upper()
            rows.append([btn(f"Auto · {name}", f"hsrc|{jid}|auto", "primary")])

        for t in text_tracks:
            chosen = job["srt_from"] == ("track", t["index"])
            rows.append([btn(labels[t["index"]], f"hsrc|{jid}|{t['index']}", sel(chosen))])

        waiting_file = awaiting.get(job["uid"], {}).get("jid") == jid
        rows.append([btn("Envoyer mon fichier", f"hsrc|{jid}|file", sel(waiting_file))])
        rows.append([btn("Retour", f"view|{jid}|hs")])
        return rows

    # ---------------- HARDSUB ----------------
    rows = [[
        btn(
            "Changer les sous-titres" if job["srt"] else "Choisir les sous-titres",
            f"view|{jid}|src",
            None if job["srt"] else "primary",
        )
    ]]
    rows.append([
        btn(v["label"], f"hstyle|{jid}|{k}", sel(job["style"] == k))
        for k, v in STYLE_PRESETS.items()
    ])
    rows.append([
        btn(name, f"hsize|{jid}|{k}", sel(job["fsize"] == k))
        for k, (name, _) in SIZE_PRESETS.items()
    ])

    if job["srt"]:
        rows.append([
            btn("Aperçu", f"prev|{jid}"),
            btn("Lancer le Hardsub", f"run|{jid}", "success"),
        ])

    rows.append([btn("Retour", f"view|{jid}|menu")])
    return rows


async def render_panel(job, note="", resend=False):
    text = job_text(job, note)
    rows = job_rows(job)

    if resend:
        old = job["panel"]
        job["panel"] = await send_panel(job["chat_id"], text, rows)
        await old.delete()
    else:
        await job["panel"].edit(text, rows)


async def refresh_panel(job, note="", resend=False):
    """Met à jour le panneau (ou le renvoie en bas de la conversation)."""
    touch(job)
    async with job["ui_lock"]:
        await render_panel(job, note, resend)


async def refresh_if_idle(job):
    """
    Met le panneau à jour en arrière-plan (vidéo prête), sans jamais écraser
    ce que la personne est en train de faire.
    """
    if job["id"] not in jobs:
        return

    async with job["ui_lock"]:
        if job["state"] == "choosing" and job["view"] in ("menu", "hs") and job["id"] in jobs:
            try:
                await render_panel(job)
            except Exception:
                pass


# ============================================================
# ÉCRANS : ADMINISTRATION
# ============================================================

def running_count():
    return sum(1 for j in jobs.values() if j["state"] == "running")


def admin_text():
    tally = counts()
    up = fmt_time(time.time() - STARTED_AT)
    saved = "Disque Render (persiste tant que le service reste éveillé)"

    return (
        header("⚙", "Administration", f"En ligne depuis {up} · version {VERSION}")
        + f"\n{DIV}\n"
        + kv([
            ("Accès", ACCESS_MODES[cfg["access"]]),
            ("Approuvés", tally.get("approved", 0) + tally.get("open", 0)),
            ("En attente", tally.get("pending", 0)),
            ("Bloqués", tally.get("blocked", 0)),
            ("En cours", running_count()),
            ("File d'attente", len(waiting)),
            ("Netteté", SHARP_PROFILES[sharp_key()][0]),
            ("Encodage", "GPU (NVENC)" if STATE.get("gpu") else "CPU"),
            ("Langue auto", AUTO_LANG_OPTIONS.get(cfg.get("auto_lang", "fra"), AUTO_LANG_OPTIONS["fra"])[0]),
            ("Sauvegarde", saved),
        ])
    )


def admin_rows():
    def sel(active):
        return "primary" if active else None

    pending = counts().get("pending", 0)
    users_label = f"Utilisateurs ({len(users)})" + (f" · {pending} en attente" if pending else "")

    return [
        [btn(ACCESS_SHORT[k], f"adm|access|{k}", sel(cfg["access"] == k)) for k in ACCESS_MODES],
        [btn(SHARP_PROFILES[k][0], f"adm|sharp|{k}", sel(sharp_key() == k)) for k in SHARP_PROFILES],
        [btn(v[0], f"adm|lang|{k}", sel(cfg.get("auto_lang", "fra") == k)) for k, v in list(AUTO_LANG_OPTIONS.items())[:3]],
        [btn(v[0], f"adm|lang|{k}", sel(cfg.get("auto_lang", "fra") == k)) for k, v in list(AUTO_LANG_OPTIONS.items())[3:]],
        [btn(users_label, "adm|users|0", "success" if pending else None)],
        [btn("Diffusion", "adm|bc"), btn("Redémarrer", "adm|rs", "danger")],
        [btn("Fermer", "nav|close")],
    ]


USERS_PER_PAGE = 6


def sorted_users():
    order = {"pending": 0, "approved": 1, "open": 2, "refused": 3, "blocked": 4}
    items = [(u, r) for u, r in users.items() if int(u) != ADMIN_ID]
    return sorted(items, key=lambda x: (order.get(x[1]["status"], 9), -x[1].get("since", 0)))


def users_text(page):
    items = sorted_users()
    pages = max(1, (len(items) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    tally = counts()

    text = (
        header("☰", "Utilisateurs")
        + f"\n{DIV}\n"
        + kv([
            ("Approuvés", tally.get("approved", 0) + tally.get("open", 0)),
            ("En attente", tally.get("pending", 0)),
            ("Refusés", tally.get("refused", 0)),
            ("Bloqués", tally.get("blocked", 0)),
        ])
    )

    if not items:
        text += "\n\n<i>Personne n'a encore utilisé le bot.</i>"
    else:
        text += f"\n\n<i>Page {page + 1}/{pages} · appuie sur une personne pour la gérer.</i>"

    return text


def users_rows(page):
    items = sorted_users()
    pages = max(1, (len(items) + USERS_PER_PAGE - 1) // USERS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = items[page * USERS_PER_PAGE:(page + 1) * USERS_PER_PAGE]

    rows = []
    for uid, rec in chunk:
        name = rec["name"][:22]
        status = STATUS_LABELS.get(rec["status"], rec["status"])
        style = "success" if rec["status"] == "pending" else None
        rows.append([btn(f"{name} · {status}", f"adm|u|{uid}", style)])

    if pages > 1:
        rows.append([
            btn("‹", f"adm|users|{max(page - 1, 0)}"),
            btn(f"{page + 1}/{pages}", f"adm|users|{page}"),
            btn("›", f"adm|users|{min(page + 1, pages - 1)}"),
        ])

    rows.append([btn("Retour", "nav|admin")])
    return rows


def user_text(uid):
    rec = users.get(str(uid))
    if not rec:
        return "<i>Utilisateur introuvable.</i>"

    who = rec["name"]
    if rec["username"]:
        who += f" (@{rec['username']})"

    since = time.strftime("%d/%m/%Y", time.localtime(rec.get("since", 0)))

    return (
        header("▪", "Utilisateur")
        + f"\n{DIV}\n"
        + kv([
            ("Nom", who),
            ("ID", uid),
            ("Statut", STATUS_LABELS.get(rec["status"], rec["status"])),
            ("Depuis", since),
            ("Vidéos", rec.get("uses", 0)),
        ])
    )


def user_rows(uid):
    rec = users.get(str(uid))
    rows = []

    if rec:
        status = rec["status"]
        if status in ("pending", "refused", "blocked", "open"):
            rows.append([btn("Approuver" if status != "blocked" else "Débloquer",
                             f"adm|set|{uid}|approved", "success")])
        if status == "pending":
            rows.append([btn("Refuser", f"adm|set|{uid}|refused")])
        if status != "blocked":
            rows.append([btn("Bloquer", f"adm|set|{uid}|blocked", "danger")])

    rows.append([btn("Retour à la liste", "adm|users|0")])
    return rows


def broadcast_prompt_text():
    n = len(recipients())
    return (
        header("⇧", "Diffusion", f"{n} destinataire(s)")
        + f"\n{DIV}\n"
        "Envoie le message à diffuser : texte, photo, vidéo…\n"
        "<i>Il sera copié tel quel à chaque utilisateur.</i>"
    )


def restart_text():
    n = len(jobs)
    extra = f"\n<i>{n} demande(s) en cours seront perdues.</i>" if n else ""
    return (
        header("↻", "Redémarrer le bot ?")
        + f"\n{DIV}\n"
        "Le bot se reconnecte et recharge le script depuis ton Drive."
        f"{extra}"
    )


# ============================================================
# COMMANDES
# ============================================================

def private_only(handler):
    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        if not getattr(event, "is_private", True):
            return
        return await handler(event, *args, **kwargs)
    return wrapper


def admin_only(handler):
    @functools.wraps(handler)
    async def wrapper(event, *args, **kwargs):
        if event.sender_id != ADMIN_ID:
            return
        return await handler(event, *args, **kwargs)
    return wrapper


@client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?(?:\s.*)?$"))
@safe
@private_only
async def cmd_start(event):
    if not await gate(event):
        return

    await wipe(event)
    uid = event.sender_id

    if uid == ADMIN_ID and not STATE.get("admin_menu"):
        STATE["admin_menu"] = True
        await set_commands()

    await send_panel(event.chat_id, home_text(uid), home_rows(uid))


@client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
@safe
@private_only
async def cmd_help(event):
    if not await gate(event):
        return

    await wipe(event)
    await send_panel(event.chat_id, help_text(), help_rows())


@client.on(events.NewMessage(pattern=r"^/style(?:@\w+)?$"))
@safe
@private_only
async def cmd_style(event):
    if not await gate(event):
        return

    await wipe(event)
    uid = event.sender_id
    await send_panel(event.chat_id, style_text(uid), style_rows(uid))


@client.on(events.NewMessage(pattern=r"^/cancel(?:@\w+)?$"))
@safe
@private_only
async def cmd_cancel(event):
    if not await gate(event):
        return

    await wipe(event)
    uid = event.sender_id
    awaiting.pop(uid, None)

    for job in jobs_of(uid):
        if job["state"] != "running":
            drop_job(job["id"])
            await job["panel"].edit(header("✕", "Opération annulée"))

    await toast(event.chat_id, "Opération annulée.")


@client.on(events.NewMessage(pattern=r"^/admin(?:@\w+)?$"))
@safe
@private_only
@admin_only
async def cmd_admin(event):
    await wipe(event)
    await send_panel(event.chat_id, admin_text(), admin_rows())


@client.on(events.NewMessage(pattern=r"^/users(?:@\w+)?$"))
@safe
@private_only
@admin_only
async def cmd_users(event):
    await wipe(event)
    await send_panel(event.chat_id, users_text(0), users_rows(0))


@client.on(events.NewMessage(pattern=r"^/broadcast(?:@\w+)?$"))
@safe
@private_only
@admin_only
async def cmd_broadcast(event):
    await wipe(event)
    await start_broadcast(event.chat_id)


@client.on(events.NewMessage(pattern=r"^/restart(?:@\w+)?$"))
@safe
@private_only
@admin_only
async def cmd_restart(event):
    await wipe(event)
    await send_panel(
        event.chat_id,
        restart_text(),
        [[btn("Redémarrer maintenant", "adm|rsgo", "danger"), btn("Annuler", "nav|close")]],
    )


# ============================================================
# RÉCEPTION (vidéos, sous-titres, texte)
# ============================================================

sub_seq = itertools.count(1)


def _dl_done(task):
    if task.cancelled():
        return
    error = task.exception()
    if error:
        print(f"[téléchargement en avance] {type(error).__name__}: {error}")


async def new_job(event):
    msg = event.message
    f = msg.file
    uid = event.sender_id
    chat = event.chat_id

    filename = f.name or f"video{f.ext or '.mp4'}"
    size = f.size or 0

    if size > MAX_SIZE:
        await toast(chat, f"Vidéo trop grosse ({format_size(size)}). Limite des bots : 2 Go.", 6)
        return

    if uid != ADMIN_ID and len(jobs_of(uid)) >= MAX_JOBS_PER_USER:
        await toast(chat, f"Tu as déjà {MAX_JOBS_PER_USER} vidéos ouvertes. Termine-en ou annules-en une.", 6)
        return

    try:
        free = shutil.disk_usage(WORK).free
    except OSError:
        free = None
    if free is not None and size and free < size * 2.5 + 400 * 1024 * 1024:
        await toast(chat, "Le bot manque d'espace disque pour l'instant. Réessaie dans quelques minutes.", 6)
        return

    if uid != ADMIN_ID and low_memory():
        await toast(chat, "Le bot est très sollicité pour l'instant. Réessaie dans une minute.", 6)
        return

    jid = next(job_counter)
    work_dir = WORK / str(jid)
    work_dir.mkdir(parents=True, exist_ok=True)
    p = prefs(uid)

    job = {
        "id": jid,
        "uid": uid,
        "state": "choosing",
        "view": "menu",
        "chat_id": chat,
        "message_id": msg.id,
        "filename": filename,
        "size": size,
        "tracks": None,
        "info_src": None,
        "ready": False,
        "srt": None,
        "srt_label": None,
        "srt_from": None,
        "style": p["style"],
        "fsize": p["size"],
        "fmt": p["fmt"],
        "compress": "medium",
        "extracted": set(),
        "track_names": {},
        "used_names": set(),
        "times": {},
        "dir": work_dir,
        "src": None,
        "orig": msg,
        "dl": {"done": 0, "total": size},
        "dl_task": None,
        "panel": None,
        "task": None,
        "t0": None,
        "touched": time.time(),
        "ui_lock": asyncio.Lock(),
    }

    job["panel"] = await send_panel(chat, job_text(job), job_rows(job))
    jobs[jid] = job

    # La vidéo se télécharge déjà pendant que tu choisis tes options
    task = run_in_background(predownload(job))
    task.add_done_callback(_dl_done)
    job["dl_task"] = task


async def attach_subtitle(event, jid):
    """Reçoit un fichier de sous-titres, le nettoie et l'associe à la vidéo."""
    uid = event.sender_id
    job = jobs.get(jid)

    if not job or job["uid"] != uid:
        awaiting.pop(uid, None)
        await wipe(event)
        await toast(event.chat_id, "Cette demande a expiré.")
        return

    touch(job)
    name = clean_name(event.message.file.name or "subtitle.srt")
    ext = Path(name).suffix.lower()
    idx = next(sub_seq)

    raw = job["dir"] / f"sub{idx}_raw{ext}"
    await event.message.download_media(file=str(raw))
    await wipe(event)

    out = job["dir"] / f"sub{idx}.srt"

    try:
        cues = await asyncio.to_thread(subtitle_to_srt, raw, out)
    except Exception as e:
        await refresh_panel(job, f"<i>Sous-titres refusés : {esc(str(e))}</i>")
        return

    awaiting.pop(uid, None)
    job["srt"] = out
    job["srt_label"] = f"{name} · {cues} répliques"
    job["srt_from"] = ("file", name)
    job["view"] = "hs"

    await refresh_panel(job, f"<i>Sous-titres ajoutés : {esc(name)}</i>")


@client.on(events.NewMessage(incoming=True))
@safe
@private_only
async def on_message(event):
    msg = event.message
    text = (event.raw_text or "").strip()
    uid = event.sender_id
    chat = event.chat_id

    # Les commandes ont leurs propres gestionnaires
    if text.startswith("/") and not msg.media:
        return

    if not await gate(event):
        return

    aw = awaiting.get(uid)

    # L'admin envoie le message à diffuser
    if aw and aw["kind"] == "broadcast" and uid == ADMIN_ID:
        await broadcast_received(event, aw)
        return

    has_file = bool(msg.media) and not msg.web_preview and msg.file is not None

    if has_file:
        name = msg.file.name or ""
        ext = Path(name).suffix.lower()
        mime = msg.file.mime_type or ""

        # Un fichier de sous-titres attendu
        if aw and aw["kind"] == "sub" and ext in SUB_EXT:
            await attach_subtitle(event, aw["jid"])
            return

        is_video = bool(msg.video) or mime.startswith("video/") or ext in VIDEO_EXT

        if is_video:
            await new_job(event)
            return

        await wipe(event)
        if ext in SUB_EXT:
            await toast(chat, "Envoie d'abord la vidéo, puis choisis Hardsub.", 5)
        else:
            await toast(chat, "Envoie une vidéo (MKV, MP4…) pour commencer.", 5)
        return

    if not text:
        return

    await wipe(event)

    if aw and aw["kind"] == "sub":
        await toast(chat, "Envoie un fichier .srt, .ass ou .vtt.")
    else:
        await toast(chat, "Envoie une vidéo pour commencer.")


# ============================================================
# BOUTONS
# ============================================================

@client.on(events.CallbackQuery)
@safe
async def on_button(event):
    uid = event.sender_id
    role = role_of(uid)

    if role not in ("admin", "approved"):
        await event.answer("Accès non autorisé.", alert=True)
        return

    try:
        parts = event.data.decode().split("|")
    except Exception:
        await event.answer()
        return

    kind = parts[0]
    panel = Panel(event.chat_id, event.message_id)

    # ---------------- NAVIGATION ----------------
    if kind == "nav":
        target = parts[1] if len(parts) > 1 else ""
        await event.answer()
        awaiting.pop(uid, None)

        if target == "home":
            await panel.edit(home_text(uid), home_rows(uid))
        elif target == "help":
            await panel.edit(help_text(), help_rows())
        elif target == "style":
            await panel.edit(style_text(uid), style_rows(uid))
        elif target == "admin" and uid == ADMIN_ID:
            await panel.edit(admin_text(), admin_rows())
        elif target == "close":
            await panel.delete()
        return

    # ---------------- PRÉFÉRENCES ----------------
    if kind == "pref":
        action = parts[1] if len(parts) > 1 else ""
        value = parts[2] if len(parts) > 2 else None

        if action == "style" and value in STYLE_PRESETS:
            set_pref(uid, "style", value)
            await event.answer(f"Style : {STYLE_PRESETS[value]['label']}")
        elif action == "size" and value in SIZE_PRESETS:
            set_pref(uid, "size", value)
            await event.answer(f"Taille : {SIZE_PRESETS[value][0]}")
        elif action == "fmt" and value in ("srt", "orig"):
            set_pref(uid, "fmt", value)
            await event.answer("Extraction : " + ("SRT" if value == "srt" else "Original"))
        elif action == "preview":
            await event.answer("Aperçu…")
            run_in_background(send_style_sample(event.chat_id, uid))
            return
        else:
            await event.answer()
            return

        await panel.edit(style_text(uid), style_rows(uid))
        return

    # ---------------- ADMINISTRATION ----------------
    if kind == "adm":
        if uid != ADMIN_ID:
            await event.answer("Réservé à l'administrateur.", alert=True)
            return
        await admin_action(event, panel, parts)
        return

    # ---------------- ACTIONS D'UNE VIDÉO ----------------
    try:
        jid = int(parts[1])
        value = parts[2] if len(parts) > 2 else None
    except (IndexError, ValueError):
        await event.answer()
        return

    job = jobs.get(jid)

    if not job:
        await event.answer("Cette demande a expiré.", alert=True)
        return

    if job["uid"] != uid and uid != ADMIN_ID:
        await event.answer("Ce n'est pas ta demande.", alert=True)
        return

    touch(job)

    # Arrêter un traitement en cours
    if kind == "stop":
        task = job.get("task")
        if job["state"] == "running" and task and not task.done():
            await event.answer("Arrêt en cours…")
            task.cancel()
        else:
            await event.answer()
        return

    if job["state"] == "running":
        await event.answer("Déjà en cours…")
        return

    # Annuler
    if kind == "cancel":
        drop_job(jid)
        await event.answer("Annulé")
        await job["panel"].edit(header("✕", "Opération annulée"))
        return

    # ---------------- NAVIGATION ENTRE LES ÉCRANS ----------------
    if kind == "view":
        if value not in ("menu", "hs", "src", "compress"):
            await event.answer()
            return

        aw = awaiting.get(uid)
        if aw and aw["kind"] == "sub" and value != "src":
            awaiting.pop(uid, None)

        job["view"] = value
        await event.answer()
        await refresh_panel(job)
        return

    # ---------------- TÉLÉCHARGER LES SOUS-TITRES ----------------
    if kind == "scan":
        if job["tracks"] is not None:
            job["view"] = "extract"
            await event.answer()
            await refresh_panel(job)
            return

        await event.answer("Analyse…")
        job["task"] = run_in_background(scan_subs(job))
        return

    if kind == "xfmt" and value in ("srt", "orig"):
        job["fmt"] = value
        set_pref(uid, "fmt", value)
        await event.answer("Format : " + ("SRT" if value == "srt" else "Original"))
        await refresh_panel(job)
        return

    if kind == "xt":
        try:
            index = int(value)
        except (TypeError, ValueError):
            await event.answer()
            return

        await event.answer("Extraction…")
        job["task"] = run_in_background(extract_tracks(job, [index]))
        return

    if kind == "xtall":
        indices = [t["index"] for t in (job["tracks"] or [])]
        await event.answer("Extraction…")
        job["task"] = run_in_background(extract_tracks(job, indices))
        return

    # ---------------- COMPRESSION (réservé à l'administrateur) ----------------
    if kind in ("cmpopen", "cmpq", "cmprun"):
        if uid != ADMIN_ID:
            await event.answer("Réservé à l'administrateur.", alert=True)
            return

        if kind == "cmpopen":
            job["view"] = "compress"
            await event.answer()
            await refresh_panel(job)
            return

        if kind == "cmpq" and value in COMPRESS_PROFILES:
            job["compress"] = value
            await event.answer("Qualité : " + COMPRESS_PROFILES[value][0])
            await refresh_panel(job)
            return

        if kind == "cmprun":
            await event.answer("C'est parti")
            job["task"] = run_in_background(process_compress(job))
            return

        await event.answer()
        return

    # ---------------- HARDSUB ----------------
    if kind == "hsopen":
        if job["srt"] is not None:
            job["view"] = "hs"
            await event.answer()
            await refresh_panel(job)
            return

        tracks = job["tracks"]
        if tracks is not None and not any(t["codec"] in TEXT_SUB_CODECS for t in tracks):
            job["view"] = "src"
            await event.answer()
            await refresh_panel(job)
            return

        await event.answer("Préparation…")
        job["task"] = run_in_background(hs_open(job))
        return

    if kind == "hsrc":
        if value == "file":
            awaiting[uid] = {"kind": "sub", "jid": jid}
            job["view"] = "src"
            await event.answer()
            await refresh_panel(job)
            return

        if value == "auto":
            auto = pick_auto_track(job["tracks"] or [])
            if not auto:
                await event.answer("Aucune piste texte.", alert=True)
                return
            index = auto["index"]
        else:
            try:
                index = int(value)
            except (TypeError, ValueError):
                await event.answer()
                return

        awaiting.pop(uid, None)
        await event.answer("Préparation…")
        job["task"] = run_in_background(use_track(job, index))
        return

    if kind == "hstyle" and value in STYLE_PRESETS:
        job["style"] = value
        set_pref(uid, "style", value)
        await event.answer(f"Style : {STYLE_PRESETS[value]['label']}")
        await refresh_panel(job)
        return

    if kind == "hsize" and value in SIZE_PRESETS:
        job["fsize"] = value
        set_pref(uid, "size", value)
        await event.answer(f"Taille : {SIZE_PRESETS[value][0]}")
        await refresh_panel(job)
        return

    if kind == "prev":
        if not job["srt"]:
            await event.answer("Choisis d'abord des sous-titres.", alert=True)
            return

        await event.answer("Aperçu…")
        job["task"] = run_in_background(send_preview(job))
        return

    if kind == "run":
        if not job["srt"]:
            await event.answer("Choisis d'abord des sous-titres.", alert=True)
            return

        awaiting.pop(uid, None)
        await event.answer("C'est parti")
        job["task"] = run_in_background(process_hardsub(job))
        return

    await event.answer()


# ============================================================
# ADMINISTRATION : ACTIONS
# ============================================================

async def admin_action(event, panel, parts):
    action = parts[1] if len(parts) > 1 else ""

    if action == "access" and len(parts) > 2 and parts[2] in ACCESS_MODES:
        cfg["access"] = parts[2]
        save_config()
        await event.answer("Accès : " + ACCESS_MODES[parts[2]])
        await panel.edit(admin_text(), admin_rows())
        return

    if action == "sharp" and len(parts) > 2 and parts[2] in SHARP_PROFILES:
        cfg["sharp"] = parts[2]
        save_config()
        await event.answer("Netteté : " + SHARP_PROFILES[parts[2]][0])
        await panel.edit(admin_text(), admin_rows())
        return

    if action == "lang" and len(parts) > 2 and parts[2] in AUTO_LANG_OPTIONS:
        cfg["auto_lang"] = parts[2]
        save_config()
        await event.answer("Langue Hardsub auto : " + AUTO_LANG_OPTIONS[parts[2]][0])
        await panel.edit(admin_text(), admin_rows())
        return

    if action == "users":
        try:
            page = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            page = 0
        await event.answer()
        await panel.edit(users_text(page), users_rows(page))
        return

    if action == "u" and len(parts) > 2:
        await event.answer()
        await panel.edit(user_text(parts[2]), user_rows(parts[2]))
        return

    if action == "set" and len(parts) > 3 and parts[3] in ("approved", "refused", "blocked"):
        uid = int(parts[2])
        status = parts[3]
        from_request = len(parts) > 4 and parts[4] == "r"

        await decide(uid, status)
        await event.answer(STATUS_LABELS[status])

        if from_request:
            rec = users.get(str(uid), {})
            await panel.edit(
                f"<b>Demande traitée</b>\n\n<blockquote>{esc(rec.get('name', ''))}\n"
                f"Décision : {STATUS_LABELS[status]}</blockquote>"
            )
        else:
            await panel.edit(user_text(str(uid)), user_rows(str(uid)))
        return

    if action == "bc":
        await event.answer()
        await start_broadcast(event.chat_id, panel)
        return

    if action == "bcgo":
        bc = STATE.get("bc")
        if not bc:
            await event.answer("Rien à envoyer.", alert=True)
            return
        await event.answer("Envoi…")
        run_in_background(broadcast_task(panel, bc))
        return

    if action == "bcno":
        bc = STATE.pop("bc", None)
        awaiting.pop(ADMIN_ID, None)
        await event.answer("Annulé")
        if bc:
            try:
                await bot_api("deleteMessage", chat_id=bc["chat"], message_id=bc["msg_id"])
            except ApiError:
                pass
        await panel.edit(admin_text(), admin_rows())
        return

    if action == "rs":
        await event.answer()
        await panel.edit(
            restart_text(),
            [[btn("Redémarrer maintenant", "adm|rsgo", "danger"), btn("Retour", "nav|admin")]],
        )
        return

    if action == "rsgo":
        await event.answer("Redémarrage…")
        run_in_background(do_restart(panel))
        return

    await event.answer()


# ============================================================
# SOUS-TITRES : ANALYSE, PISTE CHOISIE
# ============================================================

async def finish_task(job, note="", view=None, resend=False):
    """Fin d'une tâche : la personne retrouve son panneau à jour."""
    if job["id"] not in jobs:
        return

    job["state"] = "choosing"
    if view:
        job["view"] = view

    try:
        await refresh_panel(job, note, resend=resend)
    except Exception:
        pass


def error_note(e):
    if isinstance(e, asyncio.CancelledError):
        return "<i>✕ Opération arrêtée.</i>"
    if isinstance(e, FloodWaitError):
        return f"<i>⚠ Telegram demande {e.seconds} secondes d'attente.</i>"
    return f"<i>⚠ Erreur : {esc(type(e).__name__)}: {esc(str(e))}</i>"


async def scan_subs(job):
    """Cherche toutes les pistes de sous-titres de la vidéo (pour les télécharger)."""
    job["state"] = "running"
    progress = Progress(job)
    note = ""
    view = "extract"

    try:
        await ensure_source(job, progress)
        if job["tracks"] is None:
            _, job["tracks"] = await asyncio.to_thread(probe_all, job["src"])

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        view = "menu"
        note = error_note(e)

    finally:
        progress.stop()
        await finish_task(job, note, view)


async def prepare_track(job, index, progress, src):
    """Lit une piste de la vidéo et la convertit en SRT propre, prêt à être gravé."""
    tracks = job["tracks"] or []
    track = next((t for t in tracks if t["index"] == index), None)
    if not track:
        raise RuntimeError("Piste introuvable.")

    await progress.start("Préparation des sous-titres")

    raw = job["dir"] / f"track_{index}.srt"
    ok, err = await run_ffmpeg(["-i", str(src), "-map", f"0:{index}", "-c:s", "srt", str(raw)])

    if not ok or not raw.exists():
        raise RuntimeError(f"Lecture de la piste impossible. {err}")

    out = job["dir"] / f"track_{index}_clean.srt"
    cues = await asyncio.to_thread(subtitle_to_srt, raw, out)

    labels = track_labels(tracks)
    job["srt"] = out
    job["srt_label"] = f"{labels[index]} · {cues} répliques"
    job["srt_from"] = ("track", index)


async def use_track(job, index):
    """Un bouton de piste a été choisi."""
    job["state"] = "running"
    progress = Progress(job)
    note = ""
    view = "hs"

    try:
        src = await ensure_source(job, progress)
        await prepare_track(job, index, progress, src)

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        view = "src"
        note = error_note(e)

    finally:
        progress.stop()
        await finish_task(job, note, view)


async def hs_open(job):
    """Ouvre le Hardsub : choisit tout seul la meilleure piste (français complet) si possible."""
    job["state"] = "running"
    progress = Progress(job)
    note = ""
    view = "hs"

    try:
        src = await ensure_source(job, progress)

        if job["tracks"] is None:
            _, job["tracks"] = await asyncio.to_thread(probe_all, src)

        if job["srt"] is None:
            auto = pick_auto_track(job["tracks"])
            if auto:
                await prepare_track(job, auto["index"], progress, src)
            else:
                view = "src"

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        view = "menu"
        note = error_note(e)

    finally:
        progress.stop()
        await finish_task(job, note, view)


# ============================================================
# SOUS-TITRES : TÉLÉCHARGEMENT
# ============================================================

async def extract_tracks(job, indices):
    """Extrait et envoie les pistes demandées (une ou toutes)."""
    job["state"] = "running"
    progress = Progress(job)
    note = ""

    try:
        async with Turn(job):
            src = await ensure_source(job, progress)

            labels = track_labels(job["tracks"])
            by_index = {t["index"]: t for t in job["tracks"]}
            stem = clean_name(Path(job["filename"]).stem)
            as_srt = job["fmt"] == "srt"

            items = []      # (chemin, légende)
            failed = []

            for n, index in enumerate(indices, 1):
                t = by_index.get(index)
                if not t:
                    continue

                label = labels[index]
                convert = as_srt and t["codec"] in TEXT_SUB_CODECS

                if convert:
                    ext, codec = ".srt", "srt"
                else:
                    ext, codec = SUB_EXTRACT.get(t["codec"], (".mks", "copy"))

                key = (index, ext)
                name = job["track_names"].get(key)
                if not name:
                    base = f"{stem}.{t['lang']}"
                    name = f"{base}{ext}"
                    k = 2
                    while name in job["used_names"]:
                        name = f"{base}.{k}{ext}"
                        k += 1
                    job["used_names"].add(name)
                    job["track_names"][key] = name

                out = job["dir"] / clean_name(name)

                await progress.start(f"Extraction {n}/{len(indices)}", esc(label))

                args = ["-i", str(src), "-map", f"0:{index}"]
                args += ["-c", "copy"] if codec == "copy" else ["-c:s", codec]
                args.append(str(out))

                ok, err = await run_ffmpeg(args)

                if not ok or not out.exists() or out.stat().st_size == 0:
                    failed.append(label)
                    continue

                if convert:
                    # Réplique lisible partout : on retire seulement les codes ASS
                    text = decode_text(out.read_bytes())
                    out.write_text(clean_srt(text, strip_tags=False), encoding="utf-8")

                items.append((out, label, index))

            await progress.start("Envoi", f"{len(items)} fichier(s)")

            sent = await send_subtitle_files(job["chat_id"], items)
            for _, _, index in items[:sent]:
                job["extracted"].add(index)

            if sent == 1 and not failed and len(indices) == 1:
                note = f"<i>✓ Piste envoyée : {esc(labels[indices[0]])}</i>"
            else:
                note = f"<b>✓ {sent}/{len(indices)} piste(s) envoyée(s)</b>"
                if failed:
                    lines = "\n".join(f"⚠ {esc(x)} — échec" for x in failed)
                    note += f"\n<blockquote>{lines}</blockquote>"

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        note = error_note(e)

    finally:
        progress.stop()
        await finish_task(job, note, "extract", resend=True)


async def send_subtitle_files(chat_id, items):
    """Envoie les fichiers de sous-titres (en album quand il y en a plusieurs). Retourne le nombre envoyé."""
    if not items:
        return 0

    if len(items) == 1:
        path, label, _ = items[0]
        await send_retry(chat_id, str(path), caption=label, force_document=True, parse_mode=None)
        return 1

    sent = 0
    for i in range(0, len(items), 10):
        group = items[i:i + 10]
        try:
            await send_retry(
                chat_id,
                [str(p) for p, _, _ in group],
                caption=[label for _, label, _ in group],
                force_document=True,
                parse_mode=None,
            )
            sent += len(group)
        except (asyncio.CancelledError, FloodWaitError):
            raise
        except Exception as e:
            # Album refusé : on envoie un par un
            print(f"[album] refusé ({type(e).__name__}: {e}) -> envoi un par un")
            for path, label, _ in group:
                await send_retry(chat_id, str(path), caption=label, force_document=True, parse_mode=None)
                sent += 1

    return sent


# ============================================================
# APERÇUS
# ============================================================

async def delete_later(chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot_api("deleteMessage", chat_id=chat_id, message_id=message_id)
    except ApiError:
        pass


async def send_style_sample(chat_id, uid):
    """Image d'exemple du style choisi, sur fond clair et sombre."""
    p = prefs(uid)
    tmp = WORK / f"sample_{uid}"
    tmp.mkdir(parents=True, exist_ok=True)

    srt = tmp / "sample.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n"
        "Voilà un sous-titre net et lisible,\ncomme à la télé.\n",
        encoding="utf-8",
    )
    out = tmp / "sample.jpg"

    try:
        graph = f"[0:v][1:v]hstack,{subtitle_filter(srt, p['style'], p['size'])}"
        ok, err = await run_ffmpeg([
            "-f", "lavfi", "-i", "color=c=0xe9e9e9:s=480x540:d=1",
            "-f", "lavfi", "-i", "color=c=0x101820:s=480x540:d=1",
            "-filter_complex", graph,
            "-frames:v", "1", "-q:v", "3", str(out),
        ])
        if not ok or not out.exists():
            raise RuntimeError(err or "ffmpeg")

        msg = await send_retry(
            chat_id, str(out),
            caption=f"Aperçu · {style_label(p['style'], p['size'])}",
            force_document=False, parse_mode=None,
        )
        run_in_background(delete_later(chat_id, msg.id, 120))

    except Exception as e:
        print(f"[aperçu] {type(e).__name__}: {e}")
        await toast(chat_id, "Aperçu impossible pour le moment.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def send_preview(job):
    """Image de la vidéo avec un vrai sous-titre gravé, pour juger le rendu."""
    job["state"] = "running"
    progress = Progress(job)
    note = ""

    try:
        src = await ensure_source(job, progress)

        text = Path(job["srt"]).read_text(encoding="utf-8")
        at = pick_preview_time(parse_cues(text))

        out = job["dir"] / "preview.jpg"
        out.unlink(missing_ok=True)

        vf = "scale=960:-2," + subtitle_filter(job["srt"], job["style"], job["fsize"])
        ok, err = await run_ffmpeg([
            "-ss", f"{at:.3f}", "-copyts", "-i", str(src),
            "-vf", vf, "-frames:v", "1", "-q:v", "3", str(out),
        ])

        if not ok or not out.exists():
            raise RuntimeError(f"Aperçu impossible. {err}")

        msg = await send_retry(
            job["chat_id"], str(out),
            caption=f"Aperçu · {style_label(job['style'], job['fsize'])}",
            force_document=False, parse_mode=None,
        )
        run_in_background(delete_later(job["chat_id"], msg.id, 180))

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        note = error_note(e)

    finally:
        progress.stop()
        await finish_task(job, note, "hs")


# ============================================================
# HARDSUB
# ============================================================

async def send_output(job, progress, output_path, final_name):
    """Envoie la vidéo finale, toujours en vidéo (lecture directe dans Telegram)."""
    info = await asyncio.to_thread(media_info, output_path)
    q = quality(info)
    d = format_duration(info["duration"])
    out_size = os.path.getsize(output_path)

    thumb = await asyncio.to_thread(create_thumbnail, output_path, job["dir"])
    caption = Path(final_name).stem[:1024]

    # Octets invisibles : évite que Telegram réutilise un ancien envoi identique
    try:
        await asyncio.to_thread(make_unique, output_path)
    except Exception:
        pass

    attributes = [
        DocumentAttributeVideo(
            duration=int(float(info["duration"] or 0)),
            w=info["width"] or 0,
            h=info["height"] or 0,
            supports_streaming=True,
        )
    ]

    await progress.start("Envoi")
    t_upload = time.time()

    handle = await upload_handle(output_path, progress.telethon)

    options = dict(
        caption=caption or None,
        parse_mode=None,
        force_document=False,
        thumb=thumb,
        attributes=attributes,
        supports_streaming=True,
    )

    try:
        await send_retry(
            job["chat_id"],
            handle,
            progress_callback=progress.telethon if isinstance(handle, str) else None,
            **options,
        )
    except (asyncio.CancelledError, FloodWaitError):
        raise
    except Exception as e:
        if isinstance(handle, str):
            raise
        print(f"[rapide] envoi rapide refusé ({type(e).__name__}: {e}) -> mode classique")
        await send_retry(
            job["chat_id"],
            str(output_path),
            progress_callback=progress.telethon,
            **options,
        )

    job["times"]["upload"] = time.time() - t_upload
    progress.stop()

    return {"q": q, "d": d, "size": out_size}


async def process_compress(job):
    """Compresse la vidéo (réservé à l'administrateur) : réduit nettement la taille."""
    jid = job["id"]
    work_dir = job["dir"]
    job["state"] = "running"
    job["t0"] = time.time()

    progress = Progress(job)
    label, height, crf, audio_br = COMPRESS_PROFILES[job["compress"]]

    try:
        async with Turn(job):
            input_path = await ensure_source(job, progress)

            info_src = await asyncio.to_thread(media_info, input_path)
            duration = float(info_src["duration"]) if info_src["duration"] else None
            in_size = os.path.getsize(input_path)

            final_name = clean_name(Path(job["filename"]).stem) + "_compressed.mp4"
            output_path = work_dir / clean_name(final_name)

            vf = None
            if info_src["height"] and info_src["height"] > height:
                vf = f"scale=-2:{height}:flags=lanczos"

            await progress.start(f"Compression · {label}", "Peut prendre plusieurs minutes")

            t_convert = time.time()

            args = [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
                "-profile:v", "high", "-pix_fmt", "yuv420p",
            ]
            if vf:
                args += ["-vf", vf]
            args += ["-c:a", "aac", "-b:a", audio_br]

            ok, err = await run_ffmpeg(
                [
                    "-i", str(input_path),
                    "-map", "0:v:0", "-map", "0:a:0?",
                    *args,
                    "-sn", "-movflags", "+faststart",
                    str(output_path),
                ],
                duration,
                progress.pct,
            )

            if not ok:
                raise RuntimeError(f"La compression a échoué. {err}")

            job["times"]["convert"] = time.time() - t_convert
            input_path.unlink(missing_ok=True)

            res = await send_output(job, progress, output_path, final_name)

            reduction = (1 - res["size"] / in_size) * 100 if in_size else 0

            text = (
                header("✓", "Compression terminée") + f"\n{DIV}\n"
                + kv([
                    ("Avant", format_size(in_size)),
                    ("Après", format_size(res["size"])),
                    ("Réduction", f"{reduction:.0f} %"),
                    ("Qualité", label),
                    ("Résolution", res["q"]),
                ])
            )
            await job["panel"].edit(text)

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        progress.stop()

        if isinstance(e, asyncio.CancelledError):
            await job["panel"].edit(header("✕", "Traitement arrêté"))
        elif isinstance(e, FloodWaitError):
            await job["panel"].edit(
                header("⚠", "Telegram demande d'attendre") + f"\n{DIV}\n<i>{e.seconds} secondes.</i>"
            )
        else:
            await job["panel"].edit(
                header("⚠", "Erreur") + f"\n{DIV}\n<blockquote>{esc(f'{type(e).__name__}: {e}')}</blockquote>"
            )

    finally:
        progress.stop()
        drop_job(jid)


async def process_hardsub(job):
    """Hardsub : grave les sous-titres dans l'image et envoie une vidéo nette."""
    jid = job["id"]
    work_dir = job["dir"]
    job["state"] = "running"
    job["t0"] = time.time()

    progress = Progress(job)

    try:
        async with Turn(job):

            # ---------------- TÉLÉCHARGEMENT ----------------
            input_path = await ensure_source(job, progress)

            info_src = await asyncio.to_thread(media_info, input_path)
            duration = float(info_src["duration"]) if info_src["duration"] else None

            # Vidéo en résolution très élevée : on limite à 1080p pour éviter
            # de surcharger la machine (RAM/CPU) pendant le Hardsub.
            downscaled = False
            vf = subtitle_filter(job["srt"], job["style"], job["fsize"])
            if info_src["height"] and info_src["height"] > 1080:
                vf = f"scale=-2:1080:flags=lanczos,{vf}"
                downscaled = True

            final_name = clean_name(Path(job["filename"]).stem) + ".mp4"
            output_path = work_dir / clean_name(final_name)

            # ---------------- HARDSUB ----------------
            await progress.start("Hardsub", "Peut prendre plusieurs minutes")

            t_convert = time.time()

            ok, err = await run_ffmpeg(
                [
                    "-i", str(input_path),
                    "-map", "0:v:0", "-map", "0:a?",
                    *video_codec_args(vf, min_profile="balanced"),
                    "-sn", "-movflags", "+faststart",
                    str(output_path),
                ],
                duration,
                progress.pct,
            )

            gpu_fallback = False

            if not ok and STATE.get("gpu"):
                # Le GPU a échoué en cours de route (quota Colab, pilote...) :
                # on réessaie aussitôt en CPU, pour ne jamais bloquer la personne.
                gpu_fallback = True
                output_path.unlink(missing_ok=True)

                ok, err = await run_ffmpeg(
                    [
                        "-i", str(input_path),
                        "-map", "0:v:0", "-map", "0:a?",
                        *video_codec_args(vf, min_profile="balanced", force_cpu=True),
                        "-sn", "-movflags", "+faststart",
                        str(output_path),
                    ],
                    duration,
                    progress.pct,
                )

            if not ok:
                raise RuntimeError(f"Le Hardsub a échoué. {err}")

            if gpu_fallback:
                STATE["gpu"] = False
                print("[GPU] encodage GPU indisponible en cours de session -> repli sur le CPU.")

            job["times"]["convert"] = time.time() - t_convert
            input_path.unlink(missing_ok=True)

            # ---------------- ENVOI ----------------
            res = await send_output(job, progress, output_path, final_name)

            # ---------------- RÉSUMÉ ----------------
            times = job["times"]
            stages = [("download", "téléchargement"), ("convert", "hardsub"), ("upload", "envoi")]
            detail = " · ".join(f"{label} {fmt_time(times[k])}" for k, label in stages if k in times)

            rows = [
                ("Fichier", final_name),
                ("Taille", f"{format_size(res['size'])} · {res['q']} · {res['d']}"),
                ("Sous-titres", job["srt_label"] or ""),
                ("Style", style_label(job["style"], job["fsize"])),
            ]
            if detail:
                rows.append(("Détail", detail))
            rows.append(("Traité en", fmt_time(time.time() - job["t0"])))

            text = header("✓", "Terminé") + f"\n{DIV}\n" + kv(rows)

            warn = []
            if downscaled:
                warn.append("Réduite à 1080p (source plus grande) pour limiter la charge du serveur.")
            if gpu_fallback:
                warn.append("GPU indisponible pendant le traitement : terminé sur le CPU.")
            if warn:
                text += "\n\n" + "\n".join(f"<i>⚠ {esc(w)}</i>" for w in warn)

            await job["panel"].edit(text)

            rec = users.get(str(job["uid"]))
            if rec is not None:
                rec["uses"] = rec.get("uses", 0) + 1
                save_users()

    except (asyncio.CancelledError, FloodWaitError, Exception) as e:
        progress.stop()

        if isinstance(e, asyncio.CancelledError):
            await job["panel"].edit(header("✕", "Traitement arrêté"))
        elif isinstance(e, FloodWaitError):
            await job["panel"].edit(
                header("⚠", "Telegram demande d'attendre")
                + f"\n{DIV}\n"
                + f"<i>{e.seconds} secondes avant de réessayer.</i>"
            )
        else:
            message = esc(f"{type(e).__name__}: {e}")
            await job["panel"].edit(
                header("⚠", "Erreur") + f"\n{DIV}\n" + f"<blockquote>{message}</blockquote>"
            )

    finally:
        progress.stop()
        drop_job(jid)


# ============================================================
# DIFFUSION (message à tous les utilisateurs)
# ============================================================

async def start_broadcast(chat_id, panel=None):
    text = broadcast_prompt_text()
    rows = [[btn("Annuler", "adm|bcno", "danger")]]

    if panel is None:
        panel = await send_panel(chat_id, text, rows)
    else:
        await panel.edit(text, rows)

    STATE.pop("bc", None)
    awaiting[ADMIN_ID] = {"kind": "broadcast", "panel": panel}


async def broadcast_received(event, aw):
    """L'admin a envoyé le message à diffuser : on demande confirmation."""
    STATE["bc"] = {"chat": event.chat_id, "msg_id": event.message.id}
    awaiting.pop(ADMIN_ID, None)

    n = len(recipients())
    await aw["panel"].edit(
        header("⇧", "Diffusion") + f"\n{DIV}\n"
        f"{kv([('Destinataires', n)])}\n\n"
        "Ton message sera copié tel quel à chacun d'eux. Confirmer ?",
        [[btn("Envoyer à tous", "adm|bcgo", "success"), btn("Annuler", "adm|bcno", "danger")]],
    )


async def broadcast_task(panel, bc):
    targets = recipients()
    sent = 0
    failed = 0

    STATE.pop("bc", None)

    for i, uid in enumerate(targets, 1):
        try:
            await bot_api(
                "copyMessage",
                chat_id=uid,
                from_chat_id=bc["chat"],
                message_id=bc["msg_id"],
            )
            sent += 1
        except ApiError as e:
            failed += 1
            low = str(e).lower()
            if any(w in low for w in ("blocked", "deactivated", "chat not found")):
                rec = users.get(str(uid))
                if rec is not None:
                    rec["unreachable"] = True

        if i % 10 == 0 or i == len(targets):
            await panel.edit(
                header("⇧", "Diffusion en cours") + f"\n{DIV}\n"
                f"{bar(i * 100 / len(targets))}  <b>{i}/{len(targets)}</b>"
            )

        await asyncio.sleep(0.05)      # bien sous la limite de Telegram (30 messages/s)

    save_users()

    try:
        await bot_api("deleteMessage", chat_id=bc["chat"], message_id=bc["msg_id"])
    except ApiError:
        pass

    await panel.edit(
        header("✓", "Diffusion terminée") + f"\n{DIV}\n"
        f"{kv([('Envoyés', sent), ('Échecs', failed)])}\n\n"
        "<i>Les échecs sont surtout des personnes qui ont bloqué le bot.</i>",
        [[btn("Retour", "nav|admin")]],
    )


# ============================================================
# REDÉMARRAGE
# ============================================================

async def do_restart(panel):
    """Arrête le processus ; Render le relance automatiquement tout seul."""
    global RESTART_REQUESTED

    save_json(RESTART_PATH, {"chat_id": panel.chat_id, "message_id": panel.message_id})

    await panel.edit(
        "<b>Redémarrage…</b>\n\n<i>Le bot revient dans une minute environ.</i>"
    )

    RESTART_REQUESTED = True

    for job in list(jobs.values()):
        task = job.get("task")
        if task and not task.done():
            task.cancel()

    await asyncio.sleep(1)
    await client.disconnect()
    os._exit(0)   # Render relance automatiquement un service qui s'arrête


# ============================================================
# ENTRETIEN
# ============================================================

async def janitor():
    """Ferme les demandes oubliées pour libérer le disque (surtout avec plusieurs utilisateurs)."""
    while True:
        await asyncio.sleep(60)
        now = time.time()

        for jid, job in list(jobs.items()):
            if job["state"] != "running" and now - job["touched"] > JOB_TTL:
                drop_job(jid)
                await job["panel"].edit(
                    header("⏳", "Session expirée") + f"\n{DIV}\n" + "<i>Renvoie la vidéo pour recommencer.</i>"
                )


# ============================================================
# DÉMARRAGE
# ============================================================

def transfers_label():
    if CRYPTG == "ok":
        return "Rapides"
    if CRYPTG == "restart":
        return "Redémarre la session"
    return "Lents (cryptg absent)"


async def web_server():
    """
    Mini serveur web, exigé par Render pour un service web gratuit.
    Répond « OK » à toute requête : c'est cette page qu'UptimeRobot
    vient visiter régulièrement pour empêcher la mise en veille.
    """
    from aiohttp import web

    async def health(request):
        return web.Response(text="Studio Sous-titres : en ligne.")

    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Serveur web de surveillance actif sur le port {port}.")


async def main():
    print(f"{BOT_TITLE} · version {VERSION}")
    print("Connexion à Telegram...")

    await client.start(bot_token=BOT_TOKEN)

    # Belle police pour les sous-titres incrustés
    STATE["font"] = await asyncio.to_thread(ensure_fonts)
    STATE["gpu"] = await asyncio.to_thread(detect_gpu)

    await set_commands()
    run_in_background(janitor())
    await web_server()

    # Redémarrage demandé depuis Telegram : on prévient l'admin que c'est fini
    restart_info = load_json(RESTART_PATH, {})
    if restart_info:
        try:
            RESTART_PATH.unlink()
            await Panel(restart_info["chat_id"], restart_info["message_id"]).edit(
                f"<b>Bot redémarré</b>\n\n<i>Version {VERSION} · service relancé sur Render.</i>"
            )
        except Exception:
            pass

    me = await client.get_me()

    print()
    print("===================================")
    print("BOT CONNECTÉ")
    print("===================================")
    print(f"Bot : @{me.username}")
    print(f"Admin : {ADMIN_ID}")
    print(f"Sauvegarde : disque Render -> {DATA_DIR}")
    print(f"Accès : {ACCESS_MODES[cfg['access']]} · {len(users)} utilisateur(s) connu(s)")
    print(f"Police des sous-titres : {STATE['font'] or 'par défaut'}")
    print(f"Transferts : {transfers_label()}")
    print(f"Encodage : {'GPU (NVENC détecté)' if STATE.get('gpu') else 'CPU (pas de GPU actif ou détecté)'}")
    if CRYPTG == "missing":
        print("ATTENTION : cryptg n'est pas installé, les envois et téléchargements seront lents.")
    print("Le bot est prêt.")
    print("===================================")

    try:
        await client.run_until_disconnected()
    finally:
        # Arrêt propre : on stoppe les tâches et on ferme la connexion HTTP
        pending = [t for t in list(bg_tasks) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5)

        if http is not None and not http.closed:
            await http.close()

        print("Bot arrêté." + (" Redémarrage en cours…" if RESTART_REQUESTED else ""))


if __name__ == "__main__":
    asyncio.run(main())
