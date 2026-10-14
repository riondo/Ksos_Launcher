#!/usr/bin/env python3
"""KSOS Launcher v0.5.B

- Premier lancement (Windows) : assistant d'installation graphique (dossiers, composants,
  raccourcis) qui telecharge RetroArch, les cores libretro et Azahar (3DS).
- Connexion avec les profils du site, liste des jeux, telechargement des ROMs.
- Sauvegardes synchronisees avec le serveur avant/apres chaque partie.
Necessite pour le developpement : Python 3.9+, requests, pillow, py7zr (voir build_windows.bat).
"""
import colorsys
import glob
import hashlib
import io
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import zipfile
from html.parser import HTMLParser
from urllib.parse import quote, unquote

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageTk

VERSION = "0.5B"
IS_WIN = sys.platform.startswith("win")
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)

HOME_DIR = os.path.join(os.path.expanduser("~"), ".ksos-launcher")
CFG_FILE = os.path.join(HOME_DIR, "config.json")
MARKER = os.path.join(HOME_DIR, "installed.json")
LOC_FILE = os.path.join(HOME_DIR, "location.txt")


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def set_data_dir(path):
    """Definit le dossier des donnees (cores, ROMs, saves, RetroArch, Azahar)."""
    g = globals()
    g["APP_DIR"] = path
    for name, sub in (("CORES_DIR", "cores"), ("ROMS_DIR", "roms"), ("SAVES_DIR", "saves"),
                      ("STATES_DIR", "states"), ("RA_DIR", "retroarch"), ("AZ_DIR", "azahar")):
        g[name] = os.path.join(path, sub)
    g["RA_CFG"] = os.path.join(path, "retroarch-append.cfg")


set_data_dir(_read_text(LOC_FILE) or HOME_DIR)
ZERO32 = "0" * 32

# API en HTTPS (mots de passe), ROMs en direct pour ne pas passer par Cloudflare.
DEFAULTS = {
    "api_base": "https://reo.ksos.games/api",
    "rom_base": "http://ksos-jeux.bounceme.net:8081",
    "site_base": "https://reo.ksos.games",  # photos de profil et icones des consoles
    "retroarch": "",
}

CONSOLES = {
    "GB":        {"ext": [".gb"],                  "core": "gambatte",         "label": "Game Boy", "icon": "/images/consoles/gameboy.jpg"},
    "GBC":       {"ext": [".gbc"],                 "core": "gambatte",         "label": "Game Boy Color", "icon": "/images/consoles/gb-color.jpg"},
    "GBA":       {"ext": [".gba"],                 "core": "mgba",             "label": "Game Boy Advance", "icon": "/images/consoles/gba.jpg"},
    "NES":       {"ext": [".nes"],                 "core": "fceumm",           "label": "NES", "icon": "/images/consoles/nes.jpg"},
    "SNES":      {"ext": [".sfc", ".smc"],         "core": "snes9x",           "label": "SNES", "icon": "/images/consoles/snes.jpg"},
    "MEGADRIVE": {"ext": [".md", ".bin", ".gen"],  "core": "genesis_plus_gx",  "label": "Mega Drive", "icon": "/images/consoles/megadrive.jpg"},
    "N64":       {"ext": [".z64"],                 "core": "mupen64plus_next", "label": "Nintendo 64", "icon": "/images/consoles/N64.jpg"},
    "PS1":       {"ext": [".chd", ".pbp", ".iso", ".zip"], "core": "pcsx_rearmed",     "label": "PlayStation", "icon": "/images/consoles/PS1.jpg"},
    "DS":        {"ext": [".nds"],                 "core": "melonds",          "label": "Nintendo DS", "icon": "/images/consoles/NDS.jpg"},
    "3DS":       {"ext": [".3ds", ".cci", ".cxi"], "core": "azahar",           "label": "Nintendo 3DS", "icon": "/images/consoles/3ds.jpg",
                  "standalone": True},
}
GB_FOLDERS = ["Europe", "USA", "Japan", "Hacks", "Homebrew", "Translations", "Rev",
              "Proto-unl-demo-beta-sample"]

CORE_EXT, PLATFORM = (".dll", "windows") if IS_WIN else (".so", "linux")

S = requests.Session()
S.headers["User-Agent"] = "KSOS-Launcher/" + VERSION


def libretro_cores():
    return sorted({c["core"] for c in CONSOLES.values() if not c.get("standalone")})


# ---------------------------------------------------------------- config
def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(HOME_DIR, exist_ok=True)
    with open(CFG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


# ---------------------------------------------------------------- fichiers / telechargements
def find_exe(root, names):
    if not os.path.isdir(root):
        return None
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.lower() in names:
                return os.path.join(dp, f)
    return None


def find_azahar_exe(root):
    exe = find_exe(root, {"azahar.exe", "azahar-qt.exe"})
    if exe or not os.path.isdir(root):
        return exe
    for dp, _, fs in os.walk(root):
        for f in fs:
            low = f.lower()
            if low.startswith("azahar") and low.endswith(".exe") and not any(
                    x in low for x in ("room", "updater", "installer", "uninst")):
                return os.path.join(dp, f)
    return None


def find_retroarch(cfg):
    managed = find_exe(RA_DIR, {"retroarch.exe"})
    if managed:
        return managed
    p = cfg.get("retroarch")
    if p and os.path.exists(p):
        return p
    cands = []
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.environ.get("APPDATA"), "C:\\"):
            if base:
                cands += [os.path.join(base, "RetroArch", "retroarch.exe"),
                          os.path.join(base, "RetroArch-Win64", "retroarch.exe")]
    else:
        w = shutil.which("retroarch")
        if w:
            cands.append(w)
    for c in cands:
        if os.path.exists(c):
            return c
    return None


def download(url, dest, progress=None):
    tmp = dest + ".part"
    with S.get(url, stream=True, timeout=30) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if progress and total:
                    progress(done / total)
    os.replace(tmp, dest)


def rom_is_current(url, path):
    if not os.path.exists(path):
        return False
    try:
        h = S.head(url, timeout=15, allow_redirects=True)
        return int(h.headers.get("content-length", -1)) == os.path.getsize(path)
    except (requests.RequestException, ValueError):
        return False


SEVENZR_URL = "https://www.7-zip.org/a/7zr.exe"  # extracteur 7z officiel (gere LZMA2, BCJ, BCJ2)


def get_7zr():
    exe = os.path.join(HOME_DIR, "7zr.exe")
    if os.path.exists(exe):
        return exe
    os.makedirs(HOME_DIR, exist_ok=True)
    try:
        download(SEVENZR_URL, exe)
        return exe
    except requests.RequestException:
        return None


def extract_7z(archive, dest):
    """Extrait une archive .7z. Essaie 7zr.exe, puis tar de Windows, puis py7zr."""
    os.makedirs(dest, exist_ok=True)
    errors = []
    if IS_WIN:
        exe = get_7zr()
        if exe:
            r = subprocess.run([exe, "x", archive, "-o" + dest, "-y"], capture_output=True,
                               text=True, creationflags=NOWIN)
            if r.returncode == 0:
                return
            errors.append("7zr : " + (r.stdout + r.stderr).strip()[-300:])
        else:
            errors.append("7zr.exe n'a pas pu etre telecharge")
    try:
        r = subprocess.run(["tar", "-xf", archive, "-C", dest], capture_output=True, text=True,
                           creationflags=NOWIN)
        if r.returncode == 0:
            return
        errors.append("tar : " + (r.stdout + r.stderr).strip()[-200:])
    except OSError as e:
        errors.append("tar : %s" % e)
    try:
        import py7zr  # import tardif : le launcher demarre meme si le module manque
        with py7zr.SevenZipFile(archive) as z:
            z.extractall(dest)
        return
    except Exception as e:  # noqa: BLE001
        errors.append("py7zr : %s" % e)
    raise RuntimeError("Extraction impossible. " + " | ".join(errors))


# ---------------------------------------------------------------- installation des composants
def retroarch_url():
    """Derniere version stable de RetroArch ; repli sur la nightly."""
    try:
        r = S.get("https://buildbot.libretro.com/stable/", timeout=20)
        vers = re.findall(r'href="(\d+\.\d+\.\d+)/"', r.text)
        if vers:
            v = max(vers, key=lambda s: tuple(int(x) for x in s.split(".")))
            url = "https://buildbot.libretro.com/stable/%s/windows/x86_64/RetroArch.7z" % v
            if S.head(url, timeout=20, allow_redirects=True).status_code == 200:
                return url
    except requests.RequestException:
        pass
    return "https://buildbot.libretro.com/nightly/windows/x86_64/RetroArch.7z"


def ensure_core(core):
    path = os.path.join(CORES_DIR, core + "_libretro" + CORE_EXT)
    if os.path.exists(path):
        return path
    os.makedirs(CORES_DIR, exist_ok=True)
    url = "https://buildbot.libretro.com/nightly/%s/x86_64/latest/%s_libretro%s.zip" % (
        PLATFORM, core, CORE_EXT)
    r = S.get(url, timeout=120)
    if r.status_code != 200:
        raise RuntimeError("Core introuvable (%s) : %s" % (r.status_code, url))
    zipfile.ZipFile(io.BytesIO(r.content)).extractall(CORES_DIR)
    return path


def pick_azahar_asset(assets):
    """Choisit l'Azahar Windows autonome (pas le core libretro, pas l'installateur)."""
    def ok(a, must):
        low = a["name"].lower()
        return (low.endswith(".zip") and must in low and "libretro" not in low
                and "installer" not in low and "room" not in low)
    for must in ("windows-msvc", "windows-msys2", "windows-mxe", "windows"):
        for a in assets:
            if ok(a, must):
                return a["browser_download_url"], a["name"]
    return None


def azahar_assets():
    """Fichiers de la derniere version d'Azahar. Passe par les pages web de GitHub (pas de
    limite d'API), puis par l'API en repli."""
    errors = []
    try:
        r = S.get("https://github.com/azahar-emu/azahar/releases/latest", timeout=30)
        r.raise_for_status()
        tag = r.url.rstrip("/").split("/")[-1]
        r2 = S.get("https://github.com/azahar-emu/azahar/releases/expanded_assets/" + tag,
                   timeout=30)
        r2.raise_for_status()
        links = re.findall(r'href="(/azahar-emu/azahar/releases/download/[^"]+)"', r2.text)
        assets = [{"name": unquote(l.split("/")[-1]), "browser_download_url": "https://github.com" + l}
                  for l in dict.fromkeys(links)]
        if assets:
            return assets
        errors.append("page web : aucun fichier trouve")
    except requests.RequestException as e:
        errors.append("page web : %s" % e)
    try:
        r = S.get("https://api.github.com/repos/azahar-emu/azahar/releases/latest", timeout=30)
        r.raise_for_status()
        return r.json().get("assets", [])
    except requests.RequestException as e:
        errors.append("API : %s" % e)
    raise RuntimeError("Impossible de lister les versions d'Azahar. " + " | ".join(errors))


def install_azahar(status, progress):
    assets = azahar_assets()
    pick = pick_azahar_asset(assets)
    if not pick:
        raise RuntimeError("Aucun fichier Windows dans la derniere version d'Azahar : " +
                           ", ".join(a["name"] for a in assets))
    url, name = pick
    os.makedirs(AZ_DIR, exist_ok=True)
    arc = os.path.join(AZ_DIR, name)
    status("Azahar : telechargement...")
    download(url, arc, progress)
    status("Azahar : extraction...")
    if name.lower().endswith(".zip"):
        zipfile.ZipFile(arc).extractall(AZ_DIR)
    else:
        extract_7z(arc, AZ_DIR)
    os.remove(arc)
    exe = find_azahar_exe(AZ_DIR)
    if not exe:
        raise RuntimeError("azahar.exe introuvable apres l'extraction")
    # mode portable : Azahar range ses donnees dans le dossier "user" a cote de l'exe
    os.makedirs(os.path.join(os.path.dirname(exe), "user"), exist_ok=True)


def load_install():
    try:
        with open(MARKER, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def default_install_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or HOME_DIR, "KSOS Launcher")


def free_gb(path):
    p = os.path.abspath(path or HOME_DIR)
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return None


def needs_setup(cfg):
    return IS_WIN and load_install() is None


def setup_all(cfg, status, progress, classic=True, threeds=True):
    os.makedirs(APP_DIR, exist_ok=True)
    if classic:
        if IS_WIN and not find_retroarch(cfg):
            arc = os.path.join(APP_DIR, "RetroArch.7z")
            if not os.path.exists(arc):
                status("RetroArch : telechargement (environ 200 Mo)...")
                download(retroarch_url(), arc, progress)
            status("RetroArch : extraction (quelques minutes)...")
            extract_7z(arc, RA_DIR)
            os.remove(arc)
        cores = libretro_cores()
        for i, core in enumerate(cores, 1):
            status("Core %s (%d/%d)..." % (core, i, len(cores)))
            progress(i / len(cores))
            ensure_core(core)
    if threeds and IS_WIN and not find_azahar_exe(AZ_DIR):
        install_azahar(status, progress)
    status("Installation terminee.")


def make_shortcut(folder, target, workdir):
    """Cree un raccourci 'KSOS Launcher' dans le Bureau ('Desktop') ou le menu Demarrer ('Programs')."""
    q = lambda s: s.replace("'", "''")
    ps = ("$d=[Environment]::GetFolderPath('%s');"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'KSOS Launcher.lnk'));"
          "$s.TargetPath='%s';$s.WorkingDirectory='%s';$s.Save()") % (
              folder, q(target), q(workdir))
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True,
                   creationflags=NOWIN)


def install(cfg, opts, status, progress):
    """Installation complete : copie du launcher, composants, raccourcis. Renvoie l'exe installe."""
    data, inst = opts["data_dir"], opts["install_dir"]
    os.makedirs(HOME_DIR, exist_ok=True)
    os.makedirs(data, exist_ok=True)
    os.makedirs(inst, exist_ok=True)
    set_data_dir(data)
    with open(LOC_FILE, "w", encoding="utf-8") as f:
        f.write(data)

    target = None
    if getattr(sys, "frozen", False):
        status("Copie du launcher...")
        target = os.path.join(inst, "KSOSLauncher.exe")
        same = os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(
            os.path.abspath(target))
        if not same:
            shutil.copy2(sys.executable, target)

    setup_all(cfg, status, progress, opts["classic"], opts["threeds"])

    if target:
        if opts["desktop"]:
            status("Raccourci sur le Bureau...")
            make_shortcut("Desktop", target, inst)
        if opts["startmenu"]:
            status("Raccourci dans le menu Demarrer...")
            make_shortcut("Programs", target, inst)
    info = dict(opts)
    info.update({"exe": target, "version": VERSION})
    with open(MARKER, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=2)
    status("Installation terminee.")
    return target


# ---------------------------------------------------------------- API
def parse_users(data):
    if isinstance(data, dict) and "users" in data:
        data = data["users"]
    if isinstance(data, dict):
        return list(data.keys())
    if isinstance(data, list):
        out = []
        for x in data:
            out.append(x if isinstance(x, str) else (x.get("username") or x.get("name")))
        return [u for u in out if u]
    return []


def clean(name):
    return re.sub(r"[^a-zA-Z0-9_-]", "_", name)


def save_name(user, g):
    # meme format que le site : utilisateur_console_nom.srm
    return "%s_%s_%s.srm" % (user, g["console"], clean(g["name"]))


def save_name_3ds(user, g):
    return "%s_%s_%s.3dssave.zip" % (user, g["console"], clean(g["name"]))


def fetch_save(cfg, user, sname):
    r = S.get("%s/save/%s/%s" % (cfg["api_base"], quote(user), quote(sname)), timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def upload_save(cfg, user, g, sname, data):
    r = S.post("%s/save/%s" % (cfg["api_base"], quote(user)),
               files={"save": (sname, data)},
               data={"gameName": g["name"], "consoleName": g["console"],
                     "core": CONSOLES[g["console"]]["core"]},
               timeout=120)
    r.raise_for_status()


# ---------------------------------------------------------------- jeux
class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            for k, v in attrs:
                if k == "href" and v:
                    self.links.append(v)


def list_games(cfg, cname):
    folders = (["/roms/roms/GB/" + f + "/" for f in GB_FOLDERS] if cname == "GB"
               else ["/roms/roms/" + cname + "/"])
    exts = CONSOLES[cname]["ext"]
    games = []
    for folder in folders:
        try:
            r = S.get(cfg["rom_base"] + folder, timeout=20)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        p = LinkParser()
        p.feed(r.text)
        for href in p.links:
            if any(href.lower().endswith(e) for e in exts):
                fname = unquote(href.split("/")[-1])
                games.append({"name": os.path.splitext(fname)[0], "file": fname,
                              "url": cfg["rom_base"] + folder + href, "console": cname})
    games.sort(key=lambda g: g["name"].lower())
    return games


def get_rom(g, status, progress):
    rom_dir = os.path.join(ROMS_DIR, g["console"])
    os.makedirs(rom_dir, exist_ok=True)
    rom_path = os.path.join(rom_dir, g["file"])
    if not rom_is_current(g["url"], rom_path):
        status("Telechargement de la ROM...")
        download(g["url"], rom_path, progress)
    return rom_path


# ---------------------------------------------------------------- lancement RetroArch
def write_append_cfg():
    os.makedirs(SAVES_DIR, exist_ok=True)
    os.makedirs(STATES_DIR, exist_ok=True)
    with open(RA_CFG, "w", encoding="utf-8") as f:
        f.write('savefile_directory = "%s"\n' % SAVES_DIR)
        f.write('savestate_directory = "%s"\n' % STATES_DIR)
        for k in ("sort_savefiles_enable", "sort_savefiles_by_content_enable",
                  "sort_savestates_enable", "savefiles_in_content_dir",
                  "savestates_in_content_dir"):
            f.write('%s = "false"\n' % k)


def play_retroarch(cfg, user, g, retroarch, status, progress):
    core_path = ensure_core(CONSOLES[g["console"]]["core"])
    rom_path = get_rom(g, status, progress)

    status("Recuperation de ta sauvegarde...")
    sname = save_name(user, g)
    os.makedirs(SAVES_DIR, exist_ok=True)
    local_save = os.path.join(SAVES_DIR, os.path.splitext(g["file"])[0] + ".srm")
    data = fetch_save(cfg, user, sname)  # une erreur annule le lancement : on n'ecrase rien
    if data is None:
        if os.path.exists(local_save):
            os.remove(local_save)  # evite de reutiliser la save d'un autre profil
    else:
        with open(local_save, "wb") as f:
            f.write(data)
    before = hashlib.sha256(data).hexdigest() if data else None

    write_append_cfg()
    status("En jeu... (ferme RetroArch pour envoyer la sauvegarde)")
    subprocess.run([retroarch, "-L", core_path, rom_path, "--appendconfig", RA_CFG])

    status("Envoi de la sauvegarde...")
    if os.path.exists(local_save):
        with open(local_save, "rb") as f:
            new = f.read()
        if new.strip(b"\0") and hashlib.sha256(new).hexdigest() != before:
            upload_save(cfg, user, g, sname, new)
            return "Sauvegarde envoyee."
    return "Termine (pas de nouvelle sauvegarde)."


# ---------------------------------------------------------------- lancement 3DS (Azahar natif)
def title_id(path):
    """Identifiant de titre (16 caracteres hex) lu dans l'en-tete NCSD/NCCH, ou None."""
    try:
        with open(path, "rb") as f:
            head = f.read(0x200)
            if head[0x100:0x104] == b"NCSD":
                off = struct.unpack("<I", head[0x120:0x124])[0] * 0x200
                f.seek(off)
                n = f.read(0x200)
            else:
                n = head
    except OSError:
        return None
    if len(n) < 0x120 or n[0x100:0x104] != b"NCCH":
        return None
    return n[0x118:0x120][::-1].hex()


def save_dirs_3ds(user_dir, tid):
    pat = os.path.join(user_dir, "sdmc", "Nintendo 3DS", "*", "*", "title",
                       tid[:8], tid[8:], "data")
    return glob.glob(pat)


def dir_hash(path):
    h = hashlib.sha256()
    for dp, _, fs in sorted(os.walk(path)):
        for f in sorted(fs):
            full = os.path.join(dp, f)
            h.update(os.path.relpath(full, path).encode("utf-8"))
            with open(full, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


def zip_dir(path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for dp, _, fs in os.walk(path):
            for f in fs:
                full = os.path.join(dp, f)
                z.write(full, os.path.relpath(full, path))
    return buf.getvalue()


def safe_extract(zdata, dest):
    os.makedirs(dest, exist_ok=True)
    base = os.path.normpath(dest)
    with zipfile.ZipFile(io.BytesIO(zdata)) as z:
        for m in z.namelist():
            target = os.path.normpath(os.path.join(base, m))
            if target != base and not target.startswith(base + os.sep):
                raise RuntimeError("Archive de sauvegarde invalide")
        z.extractall(base)


def play_3ds(cfg, user, g, status, progress):
    azahar = find_azahar_exe(AZ_DIR)
    if not azahar:
        raise RuntimeError("Azahar n'est pas installe : utilise le bouton "
                           "'Installer les composants'.")
    rom_path = get_rom(g, status, progress)
    tid = title_id(rom_path)
    user_dir = os.path.join(os.path.dirname(azahar), "user")
    os.makedirs(user_dir, exist_ok=True)
    sname = save_name_3ds(user, g)

    before = None
    if tid:
        status("Recuperation de ta sauvegarde...")
        data = fetch_save(cfg, user, sname)  # erreur = lancement annule
        dirs = save_dirs_3ds(user_dir, tid)
        target = dirs[0] if dirs else os.path.join(
            user_dir, "sdmc", "Nintendo 3DS", ZERO32, ZERO32, "title", tid[:8], tid[8:], "data")
        for d in dirs:  # evite de reutiliser la save d'un autre profil
            shutil.rmtree(d, ignore_errors=True)
        if data:
            safe_extract(data, target)
            before = dir_hash(target)

    status("En jeu... (ferme Azahar pour envoyer la sauvegarde)")
    subprocess.run([azahar, rom_path])

    if not tid:
        return "Termine (sauvegarde non synchronisee : identifiant du jeu introuvable)."
    status("Envoi de la sauvegarde...")
    dirs = save_dirs_3ds(user_dir, tid)
    if dirs and os.path.isdir(dirs[0]) and any(os.scandir(dirs[0])):
        if dir_hash(dirs[0]) != before:
            upload_save(cfg, user, g, sname, zip_dir(dirs[0]))
            return "Sauvegarde envoyee."
    return "Termine (pas de nouvelle sauvegarde)."


# ---------------------------------------------------------------- profils et images
def parse_profiles(data):
    """Liste de profils {username, label, emoji, avatar} a partir de /users."""
    if isinstance(data, dict) and "users" in data:
        data = data["users"]
    out = []
    if isinstance(data, dict):
        for key, v in data.items():
            v = v if isinstance(v, dict) else {}
            out.append({"username": key, "label": v.get("label") or key,
                        "emoji": v.get("emoji") or "", "avatar": v.get("avatar") or ""})
    elif isinstance(data, list):
        for x in data:
            if isinstance(x, str):
                out.append({"username": x, "label": x, "emoji": "", "avatar": ""})
            elif isinstance(x, dict):
                u = x.get("username") or x.get("name")
                if u:
                    out.append({"username": u, "label": x.get("label") or u,
                                "emoji": x.get("emoji") or "", "avatar": x.get("avatar") or ""})
    return out


def site_url(cfg, path):
    if path.startswith("http"):
        return path
    return cfg["site_base"].rstrip("/") + "/" + path.lstrip("/")


RS = getattr(Image, "Resampling", Image).LANCZOS


def fetch_pil(url):
    r = S.get(url, timeout=25)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content))


def find_font(size, bold=False, emoji=False):
    if emoji:
        names = ["seguiemj.ttf", "NotoColorEmoji.ttf"]
    elif IS_WIN:
        names = ["segoeuib.ttf" if bold else "segoeui.ttf"]
    else:
        names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    if emoji:
        return None
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def to_circle(img, size):
    img = ImageOps.fit(img.convert("RGBA"), (size, size), RS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    mask = mask.resize((size, size), RS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def rounded(img, size, radius=8):
    img = ImageOps.fit(img.convert("RGBA"), (size, size), RS)
    big = size * 4
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=radius * 4, fill=255)
    mask = mask.resize((size, size), RS)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def placeholder_avatar(name, emoji, size=240):
    """Avatar par defaut : pastille coloree (selon le nom) avec l'emoji ou l'initiale."""
    hue = int(hashlib.md5(name.encode("utf-8")).hexdigest(), 16) % 360
    r, g, b = colorsys.hls_to_rgb(hue / 360.0, 0.42, 0.55)
    img = Image.new("RGBA", (size, size), (int(r * 255), int(g * 255), int(b * 255), 255))
    d = ImageDraw.Draw(img)
    drawn = False
    if emoji:
        f = find_font(int(size * 0.5), emoji=True)
        if f is not None:
            try:
                d.text((size / 2, size / 2), emoji, font=f, embedded_color=True, anchor="mm")
                drawn = True
            except (OSError, ValueError, TypeError):
                drawn = False
    if not drawn:
        d.text((size / 2, size / 2), (name[:1] or "?").upper(), fill="white",
               font=find_font(int(size * 0.5), bold=True), anchor="mm")
    return to_circle(img, size)


def make_bg(w, h):
    """Fond degrade sombre avec deux halos de couleur (calcule en petit puis agrandi)."""
    sw, sh = max(w // 4, 2), max(h // 4, 2)
    top, bottom = (14, 15, 30), (40, 24, 74)
    col = Image.new("RGB", (1, sh))
    for y in range(sh):
        t = y / max(sh - 1, 1)
        col.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    img = col.resize((sw, sh)).convert("RGBA")
    glow = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    d.ellipse((-sw * 0.10, -sh * 0.30, sw * 0.55, sh * 0.55), fill=(110, 80, 255, 120))
    d.ellipse((sw * 0.55, sh * 0.45, sw * 1.15, sh * 1.25), fill=(40, 140, 255, 95))
    glow = glow.filter(ImageFilter.GaussianBlur(max(sw // 8, 2)))
    return Image.alpha_composite(img, glow).resize((w, h), RS).convert("RGB")


# ---------------------------------------------------------------- interface
BG, PANEL, SIDEBAR, HEAD = "#0f1018", "#191b29", "#141622", "#1a1c2c"
ACCENT, ACCENT_HOVER, ROW_SEL, ROW_HOVER = "#7c6cff", "#9185ff", "#2f2b5e", "#1f2236"
TEXT, MUTED = "#eceaf7", "#8b8ea8"
FONT = "Segoe UI" if IS_WIN else "DejaVu Sans"


def F(size, bold=False):
    return (FONT, size, "bold") if bold else (FONT, size)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("KSOS Launcher " + VERSION)
        self.geometry("1000x660")
        self.minsize(860, 560)
        self.configure(bg=BG)
        self.setup_styles()
        self.cfg = load_cfg()
        self.q = queue.Queue()
        self.user = None
        self.profile = None
        self.games = []
        self.shown = []
        self.busy = False
        self.cur_console = None
        self.token = None
        self.status = tk.StringVar(value="")
        self.pbar = None
        self.logbox = None
        self.login_canvas = None
        self.login_state = "loading"
        self.profiles = []
        self.avatars = {}
        self.photos = {}
        self.icon_imgs = {}
        self.blank = tk.PhotoImage(width=34, height=34)
        self._rs_job = None
        prev = load_install() or {}
        self.v_inst = tk.StringVar(value=prev.get("install_dir") or default_install_dir())
        self.v_data = tk.StringVar(value=prev.get("data_dir") or APP_DIR)
        self.v_classic = tk.BooleanVar(value=prev.get("classic", True))
        self.v_3ds = tk.BooleanVar(value=prev.get("threeds", True))
        self.v_desk = tk.BooleanVar(value=prev.get("desktop", True))
        self.v_start = tk.BooleanVar(value=prev.get("startmenu", True))
        self.after(100, self.poll)
        if needs_setup(self.cfg):
            self.show_wiz_welcome()
        else:
            self.show_login()

    # -- style
    def setup_styles(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, font=F(10),
                     bordercolor=PANEL, lightcolor=PANEL, darkcolor=PANEL)
        st.configure("TFrame", background=BG)
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("Title.TLabel", background=BG, foreground="white", font=F(20, True))
        st.configure("TButton", background=PANEL, foreground=TEXT, borderwidth=0,
                     padding=(14, 8), focuscolor=PANEL)
        st.map("TButton", background=[("active", ROW_SEL)])
        st.configure("Accent.TButton", background=ACCENT, foreground="white",
                     padding=(24, 10), font=F(11, True))
        st.map("Accent.TButton", background=[("active", ACCENT_HOVER), ("disabled", "#4a4766")])
        st.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT,
                     padding=8, borderwidth=0)
        st.configure("TCheckbutton", background=BG, foreground=TEXT, focuscolor=BG)
        st.map("TCheckbutton", background=[("active", BG)],
               indicatorcolor=[("selected", ACCENT), ("!selected", PANEL)])
        st.configure("Horizontal.TProgressbar", troughcolor=PANEL, background=ACCENT,
                     borderwidth=0, thickness=8)
        st.configure("Vertical.TScrollbar", background=ROW_SEL, darkcolor=ROW_SEL,
                     lightcolor=ROW_SEL, troughcolor=PANEL, bordercolor=PANEL,
                     arrowcolor=TEXT, borderwidth=0, gripcount=0)
        st.map("Vertical.TScrollbar", background=[("active", ACCENT), ("!active", ROW_SEL)],
               darkcolor=[("active", ACCENT), ("!active", ROW_SEL)],
               lightcolor=[("active", ACCENT), ("!active", ROW_SEL)])

    # -- utilitaires threads
    def poll(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def bg(self, fn, done=None, err=None):
        def run():
            try:
                res = fn()
            except Exception as e:  # noqa: BLE001
                self.q.put(lambda e=e: (err or self.fail)(e))
                return
            if done:
                self.q.put(lambda: done(res))
        threading.Thread(target=run, daemon=True).start()

    def fail(self, e):
        self.busy = False
        self.status.set("")
        messagebox.showerror("Erreur", str(e))

    def clear(self):
        if self._rs_job:
            self.after_cancel(self._rs_job)
            self._rs_job = None
        self.pbar = None
        self.logbox = None
        self.login_canvas = None
        for w in self.winfo_children():
            w.destroy()

    def set_status(self, t):
        def upd():
            self.status.set(t)
            lg = self.logbox
            if lg is not None and lg.winfo_exists():
                lg.configure(state="normal")
                lg.insert("end", t + "\n")
                lg.see("end")
                lg.configure(state="disabled")
        self.q.put(upd)

    def set_progress(self, x):
        def upd():
            if self.pbar is not None and self.pbar.winfo_exists():
                self.pbar.configure(value=x * 100)
        self.q.put(upd)

    def photo(self, key, img, size):
        k = (key, size)
        if k not in self.photos:
            self.photos[k] = ImageTk.PhotoImage(img.resize((size, size), RS))
        return self.photos[k]

    # ============================================================ assistant d'installation
    def wiz_frame(self, title):
        self.clear()
        head = tk.Frame(self, bg=HEAD)
        head.pack(fill="x")
        tk.Label(head, text=title, bg=HEAD, fg="white", font=F(18, True)).pack(
            side="left", padx=28, pady=20)
        foot = ttk.Frame(self, padding=(28, 14))
        foot.pack(side="bottom", fill="x")
        body = ttk.Frame(self, padding=28)
        body.pack(fill="both", expand=True)
        return body, foot

    def show_wiz_welcome(self):
        body, foot = self.wiz_frame("Bienvenue dans KSOS Launcher")
        ttk.Label(body, justify="left", wraplength=640, font=F(11), text=(
            "Cet assistant va installer le launcher et telecharger ce dont il a besoin :\n\n"
            "   \u2022  RetroArch et les cores pour GB, GBA, NES, SNES, Mega Drive, N64, PS1 et DS\n"
            "   \u2022  Azahar pour la Nintendo 3DS\n"
            "   \u2022  des raccourcis pour lancer le launcher\n\n"
            "Une connexion internet est necessaire. Prevois environ 2 Go d'espace libre, "
            "sans compter les jeux.")).pack(anchor="w")
        ttk.Button(foot, text="Quitter", command=self.destroy).pack(side="right", padx=8)
        ttk.Button(foot, text="Suivant  \u203a", style="Accent.TButton",
                   command=self.show_wiz_options).pack(side="right")

    def pick_dir(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or HOME_DIR)
        if d:
            var.set(os.path.normpath(d))

    def show_wiz_options(self):
        body, foot = self.wiz_frame("Options d'installation")

        def dir_row(label, var, hint):
            ttk.Label(body, text=label, font=F(10, True)).pack(anchor="w", pady=(8, 2))
            row = ttk.Frame(body)
            row.pack(fill="x")
            ttk.Entry(row, textvariable=var).pack(side="left", fill="x", expand=True)
            ttk.Button(row, text="Parcourir...",
                       command=lambda: (self.pick_dir(var), upd())).pack(side="left", padx=8)
            ttk.Label(body, text=hint, style="Muted.TLabel").pack(anchor="w", pady=(2, 0))

        free = tk.StringVar()

        def upd(*a):
            g = free_gb(self.v_data.get())
            free.set("Espace libre sur ce disque : %.1f Go" % g if g is not None else "")

        dir_row("Dossier du launcher", self.v_inst, "L'application et ses raccourcis.")
        dir_row("Dossier des donnees", self.v_data,
                "RetroArch, cores, Azahar, jeux telecharges et sauvegardes (choisis un disque "
                "avec de la place).")
        ttk.Label(body, textvariable=free).pack(anchor="w", pady=(4, 10))
        self.v_data.trace_add("write", upd)
        upd()

        ttk.Label(body, text="Composants", font=F(10, True)).pack(anchor="w", pady=(6, 4))
        ttk.Checkbutton(body, variable=self.v_classic,
                        text="Consoles classiques : RetroArch + cores (environ 250 Mo)").pack(anchor="w", pady=2)
        ttk.Checkbutton(body, variable=self.v_3ds,
                        text="Nintendo 3DS : Azahar").pack(anchor="w", pady=2)
        ttk.Checkbutton(body, variable=self.v_desk,
                        text="Creer un raccourci sur le Bureau").pack(anchor="w", pady=2)
        ttk.Checkbutton(body, variable=self.v_start,
                        text="Creer un raccourci dans le menu Demarrer").pack(anchor="w", pady=2)

        back = self.show_wiz_welcome if needs_setup(self.cfg) else self.show_login
        ttk.Button(foot, text="Annuler", command=back).pack(side="right", padx=8)
        ttk.Button(foot, text="Installer", style="Accent.TButton",
                   command=self.run_install).pack(side="right")

    def run_install(self):
        opts = {"install_dir": self.v_inst.get().strip(), "data_dir": self.v_data.get().strip(),
                "classic": self.v_classic.get(), "threeds": self.v_3ds.get(),
                "desktop": self.v_desk.get(), "startmenu": self.v_start.get()}
        if not opts["install_dir"] or not opts["data_dir"]:
            messagebox.showerror("Erreur", "Indique les deux dossiers.")
            return
        self.show_wiz_install()

        def run():
            try:
                target = install(self.cfg, opts, self.set_status, self.set_progress)
            except Exception as e:  # noqa: BLE001
                self.q.put(lambda e=e: self.show_wiz_failed(e))
                return
            self.q.put(lambda: self.show_wiz_done(target))
        threading.Thread(target=run, daemon=True).start()

    def show_wiz_install(self):
        body, foot = self.wiz_frame("Installation en cours...")
        self.pbar = ttk.Progressbar(body, maximum=100, length=640)
        self.pbar.pack(fill="x")
        ttk.Label(body, textvariable=self.status).pack(anchor="w", pady=(10, 6))
        self.logbox = tk.Text(body, height=11, state="disabled", relief="flat", bg=PANEL,
                              fg=TEXT, font=("Consolas" if IS_WIN else "DejaVu Sans Mono", 10),
                              highlightthickness=0, padx=10, pady=8)
        self.logbox.pack(fill="both", expand=True)
        self.status.set("Demarrage...")

    def show_wiz_failed(self, e):
        body, foot = self.wiz_frame("L'installation a echoue")
        ttk.Label(body, text=str(e), wraplength=640, justify="left").pack(anchor="w")
        ttk.Label(body, text="Reessaie. Si l'erreur persiste, note le message ci-dessus.",
                  style="Muted.TLabel").pack(anchor="w", pady=12)
        ttk.Button(foot, text="Quitter", command=self.destroy).pack(side="right", padx=8)
        ttk.Button(foot, text="Reessayer", style="Accent.TButton",
                   command=self.show_wiz_options).pack(side="right")

    def show_wiz_done(self, target):
        body, foot = self.wiz_frame("Installation terminee")
        ttk.Label(body, justify="left", wraplength=640, font=F(11), text=(
            "KSOS Launcher est pret.\n\n"
            "Tu peux le lancer depuis le raccourci, ou tout de suite avec le bouton ci-dessous.")
                  ).pack(anchor="w")
        ttk.Button(foot, text="Fermer", command=self.destroy).pack(side="right", padx=8)

        def launch():
            if target and os.path.exists(target):
                subprocess.Popen([target])
                self.destroy()
            else:
                self.show_login()
        ttk.Button(foot, text="Lancer le launcher", style="Accent.TButton",
                   command=launch).pack(side="right")

    # ============================================================ connexion (profils)
    def show_login(self):
        self.clear()
        self.profile = None
        self.login_state = "loading"
        cv = tk.Canvas(self, highlightthickness=0, bd=0, bg=BG)
        cv.pack(fill="both", expand=True)
        self.login_canvas = cv
        cv.bind("<Configure>", self.on_login_resize)

        def job():
            r = S.get(self.cfg["api_base"] + "/users", timeout=20)
            r.raise_for_status()
            return parse_profiles(r.json())
        self.bg(job, self.set_profiles, self.login_error)

    def login_error(self, e):
        self.login_state = "error:%s" % e
        self.redraw_login()

    def set_profiles(self, profiles):
        self.profiles = profiles
        self.avatars = {}
        self.photos = {}
        self.login_state = "ready"
        for p in profiles:
            self.avatars[p["username"]] = placeholder_avatar(p["label"], p["emoji"])
            if p["avatar"]:
                url = site_url(self.cfg, p["avatar"])
                self.bg(lambda url=url: to_circle(fetch_pil(url), 240),
                        lambda img, p=p: self.set_avatar(p, img), lambda e: None)
        self.redraw_login()

    def set_avatar(self, p, img):
        self.avatars[p["username"]] = img
        for k in [k for k in self.photos if k[0] == p["username"]]:
            del self.photos[k]
        if self.login_canvas is not None and self.login_canvas.winfo_exists():
            self.redraw_login()

    def on_login_resize(self, event):
        if self._rs_job:
            self.after_cancel(self._rs_job)
        self._rs_job = self.after(120, self.redraw_login)

    def redraw_login(self):
        cv = self.login_canvas
        if cv is None or not cv.winfo_exists():
            return
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < 100 or h < 100:
            return
        self.login_bg = ImageTk.PhotoImage(make_bg(w, h))
        cv.delete("all")
        for child in cv.winfo_children():
            if getattr(child, "_is_retry", False):
                child.destroy()
        cv.create_image(0, 0, image=self.login_bg, anchor="nw")
        cv.create_text(w // 2, 80, text="KSOS Launcher", fill="white", font=F(32, True))
        state = self.login_state
        if state == "ready":
            cv.create_text(w // 2, 124, text="Qui joue ?", fill="#cfc9ff", font=F(15))
            self.layout_profiles(cv, w, h)
        elif state == "loading":
            cv.create_text(w // 2, h // 2, text="Chargement des profils...", fill="#cfc9ff",
                           font=F(14))
        else:
            cv.create_text(w // 2, h // 2 - 20, width=w - 160, fill="#ffb4b4", font=F(12),
                           text="Impossible de joindre le serveur.\n" + state[6:])
            btn = ttk.Button(cv, text="Reessayer", style="Accent.TButton", command=self.show_login)
            btn._is_retry = True
            cv.create_window(w // 2, h // 2 + 60, window=btn)
        foot = ttk.Button(cv, text="Installer / reparer les composants",
                          command=self.show_wiz_options)
        foot._is_retry = True
        cv.create_window(w - 24, h - 24, window=foot, anchor="se")
        cv.create_text(24, h - 24, text="v" + VERSION, fill="#6d6f8c", anchor="sw", font=F(9))

    def layout_profiles(self, cv, w, h):
        n = len(self.profiles)
        if not n:
            cv.create_text(w // 2, h // 2, text="Aucun profil.", fill="#cfc9ff", font=F(13))
            return
        size, cell_w, cell_h = 112, 176, 196
        cols = max(1, min(n, (w - 60) // cell_w))
        rows = (n + cols - 1) // cols
        x0 = (w - cols * cell_w) / 2 + cell_w / 2
        top = max(180, (h - rows * cell_h) // 2 + 30)
        for i, p in enumerate(self.profiles):
            cx = x0 + (i % cols) * cell_w
            cy = top + size / 2 + (i // cols) * cell_h
            tag = "card%d" % i
            ring = cv.create_oval(cx - size / 2 - 7, cy - size / 2 - 7, cx + size / 2 + 7,
                                  cy + size / 2 + 7, outline=ACCENT_HOVER, width=4, state="hidden")
            ph = self.photo(p["username"], self.avatars[p["username"]], size)
            cv.create_image(cx, cy, image=ph, tags=(tag,))
            cv.create_text(cx, cy + size / 2 + 26, text=p["label"], fill="white",
                           font=F(12, True), tags=(tag,))
            cv.tag_bind(tag, "<Enter>", lambda e, r=ring: (
                cv.itemconfigure(r, state="normal"), cv.configure(cursor="hand2")))
            cv.tag_bind(tag, "<Leave>", lambda e, r=ring: (
                cv.itemconfigure(r, state="hidden"), cv.configure(cursor="")))
            cv.tag_bind(tag, "<Button-1>", lambda e, p=p: self.pick_profile(p))

    def pick_profile(self, p):
        self.try_login(p, "", False)

    def try_login(self, p, pw, from_prompt):
        def job():
            r = S.post("%s/user/%s/login" % (self.cfg["api_base"], quote(p["username"])),
                       json={"password": pw}, timeout=20)
            if r.status_code == 401:
                return "pw"
            r.raise_for_status()
            return "ok"

        def done(res):
            if res == "ok":
                self.show_main(p)
            elif from_prompt:
                self.show_password_prompt(p, "Mot de passe incorrect")
            else:
                self.show_password_prompt(p, "")
        self.bg(job, done)

    def show_password_prompt(self, p, error):
        cv = self.login_canvas
        if cv is None or not cv.winfo_exists():
            return
        if getattr(self, "prompt", None) is not None and self.prompt.winfo_exists():
            self.prompt.destroy()
        f = tk.Frame(cv, bg=PANEL, padx=40, pady=30, highlightbackground=ACCENT,
                     highlightthickness=1)
        self.prompt = f
        ph = self.photo(p["username"], self.avatars[p["username"]], 96)
        lbl = tk.Label(f, image=ph, bg=PANEL)
        lbl.image = ph
        lbl.pack()
        tk.Label(f, text=p["label"], bg=PANEL, fg="white", font=F(15, True)).pack(pady=(8, 14))
        pw = tk.StringVar()
        e = ttk.Entry(f, textvariable=pw, show="\u2022", width=26)
        e.pack()
        tk.Label(f, text=error, bg=PANEL, fg="#ff9a9a", font=F(10)).pack(pady=(6, 0))
        row = tk.Frame(f, bg=PANEL)
        row.pack(pady=(10, 0))
        go = lambda *a: self.try_login(p, pw.get(), True)
        ttk.Button(row, text="Connexion", style="Accent.TButton", command=go).pack(side="left")
        ttk.Button(row, text="Retour", command=f.destroy).pack(side="left", padx=8)
        e.bind("<Return>", go)
        e.bind("<Escape>", lambda ev: f.destroy())
        f.place(relx=0.5, rely=0.5, anchor="center")
        e.focus_set()

    # ============================================================ ecran principal
    def show_main(self, p):
        self.profile = p
        self.user = p["username"]
        self.clear()
        self.cur_console = None
        self.games, self.shown = [], []

        top = tk.Frame(self, bg=HEAD)
        top.pack(fill="x")
        av = tk.Label(top, bg=HEAD, image=self.photo(self.user, self.avatars[self.user], 40))
        av.pack(side="left", padx=(18, 10), pady=10)
        tk.Label(top, text=p["label"], bg=HEAD, fg="white", font=F(13, True)).pack(side="left")
        ttk.Button(top, text="Changer de profil", command=self.show_login).pack(
            side="right", padx=(6, 18), pady=10)
        ttk.Button(top, text="Composants", command=self.show_wiz_options).pack(
            side="right", pady=10)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True)
        side = tk.Frame(body, bg=SIDEBAR, width=250)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="CONSOLES", bg=SIDEBAR, fg=MUTED, font=F(9, True)).pack(
            anchor="w", padx=22, pady=(20, 8))
        self.side_rows = {}
        for key, c in CONSOLES.items():
            row = tk.Frame(side, bg=SIDEBAR, cursor="hand2")
            row.pack(fill="x", padx=10, pady=2)
            icon = tk.Label(row, image=self.blank, bg=SIDEBAR)
            icon.pack(side="left", padx=(8, 12), pady=6)
            txt = tk.Label(row, text=c["label"], bg=SIDEBAR, fg=TEXT, font=F(11), anchor="w")
            txt.pack(side="left", fill="x", expand=True)
            for w in (row, icon, txt):
                w.bind("<Button-1>", lambda e, k=key: self.select_console(k))
                w.bind("<Enter>", lambda e, k=key: self.hover_row(k, True))
                w.bind("<Leave>", lambda e, k=key: self.hover_row(k, False))
            self.side_rows[key] = [row, icon, txt, False]
            self.load_icon(key, c)

        main = tk.Frame(body, bg=BG)
        main.pack(side="left", fill="both", expand=True, padx=26, pady=20)
        head = tk.Frame(main, bg=BG)
        head.pack(fill="x")
        self.title_lbl = tk.Label(head, text="", bg=BG, fg="white", font=F(22, True))
        self.title_lbl.pack(side="left")
        self.count_lbl = tk.Label(head, text="", bg=BG, fg=MUTED, font=F(11))
        self.count_lbl.pack(side="left", padx=14, pady=(10, 0))

        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *a: self.refresh())
        srow = ttk.Frame(main)
        srow.pack(fill="x", pady=(14, 0))
        ttk.Entry(srow, textvariable=self.search).pack(fill="x")
        ttk.Label(main, text="Recherche", style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

        lf = tk.Frame(main, bg=PANEL)
        lf.pack(fill="both", expand=True, pady=(10, 12))
        sb = ttk.Scrollbar(lf, orient="vertical")
        sb.pack(side="right", fill="y")
        self.listbox = tk.Listbox(lf, bg=PANEL, fg=TEXT, selectbackground=ACCENT,
                                  selectforeground="white", highlightthickness=0, bd=0,
                                  font=F(12), activestyle="none", yscrollcommand=sb.set)
        self.listbox.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=8)
        sb.config(command=self.listbox.yview)
        self.listbox.bind("<Double-Button-1>", lambda e: self.on_play())
        self.listbox.bind("<Return>", lambda e: self.on_play())

        bottom = tk.Frame(main, bg=BG)
        bottom.pack(fill="x")
        self.play_btn = ttk.Button(bottom, text="\u25B6  Jouer", style="Accent.TButton",
                                   command=self.on_play)
        self.play_btn.pack(side="left")
        self.pbar = ttk.Progressbar(bottom, maximum=100, length=220)
        self.pbar.pack(side="left", padx=16)
        ttk.Label(bottom, textvariable=self.status, style="Muted.TLabel").pack(side="left")

        self.select_console(next(iter(CONSOLES)))

    def load_icon(self, key, c):
        if key in self.icon_imgs:
            self.apply_icon(key)
            return
        url = site_url(self.cfg, c["icon"])
        self.bg(lambda: rounded(fetch_pil(url), 34),
                lambda img, k=key: self.set_icon(k, img), lambda e: None)

    def set_icon(self, key, img):
        self.icon_imgs[key] = ImageTk.PhotoImage(img)
        self.apply_icon(key)

    def apply_icon(self, key):
        row = getattr(self, "side_rows", {}).get(key)
        if row and row[1].winfo_exists() and key in self.icon_imgs:
            row[1].configure(image=self.icon_imgs[key])

    def paint_rows(self):
        for key, (row, icon, txt, hov) in self.side_rows.items():
            sel = key == self.cur_console
            col = ROW_SEL if sel else (ROW_HOVER if hov else SIDEBAR)
            for w in (row, icon, txt):
                w.configure(bg=col)
            txt.configure(fg="white" if sel else TEXT, font=F(11, sel))

    def hover_row(self, key, on):
        self.side_rows[key][3] = on
        self.paint_rows()

    def select_console(self, key):
        self.cur_console = key
        self.paint_rows()
        self.title_lbl.configure(text=CONSOLES[key]["label"])
        self.count_lbl.configure(text="")
        self.status.set("Chargement de la liste...")
        self.games, self.shown = [], []
        self.listbox.delete(0, "end")
        tok = self.token = object()
        self.bg(lambda: list_games(self.cfg, key),
                lambda games: self.set_games(games) if tok is self.token else None)

    def set_games(self, games):
        self.games = games
        self.status.set("")
        self.count_lbl.configure(text="%d jeu(x)" % len(games))
        self.refresh()

    def refresh(self):
        term = self.search.get().lower().strip()
        self.shown = [g for g in self.games if term in g["name"].lower()]
        self.listbox.delete(0, "end")
        for g in self.shown:
            self.listbox.insert("end", "  " + g["name"])

    def on_play(self):
        if self.busy:
            return
        sel = self.listbox.curselection()
        if not sel:
            return
        g = self.shown[sel[0]]
        standalone = CONSOLES[g["console"]].get("standalone")
        ra = None
        if not standalone:
            ra = find_retroarch(self.cfg)
            if not ra:
                ra = filedialog.askopenfilename(title="Indique retroarch (executable)")
                if not ra:
                    return
                self.cfg["retroarch"] = ra
                save_cfg(self.cfg)
        self.busy = True
        self.play_btn.state(["disabled"])
        self.pbar.configure(value=0)

        def done(msg):
            self.busy = False
            self.play_btn.state(["!disabled"])
            self.status.set(msg)

        def fail(e):
            self.play_btn.state(["!disabled"])
            self.fail(e)

        if standalone:
            job = lambda: play_3ds(self.cfg, self.user, g, self.set_status, self.set_progress)
        else:
            job = lambda: play_retroarch(self.cfg, self.user, g, ra,
                                         self.set_status, self.set_progress)
        self.bg(job, done, fail)


if __name__ == "__main__":
    App().mainloop()
