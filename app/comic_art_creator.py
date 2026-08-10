r"""Comic Book Art Creator — local, unrestricted comic art studio.

A desktop frontend for a headless ComfyUI engine. Everything runs on the
local GPU; nothing leaves the machine.

Layout of the project folder:
    ComicArtCreator\  packaged exe (this file frozen) — or run app\ from venv
    ComfyUI\    engine (started automatically, headless)
    venv\       python environment for the engine + helpers
    models\     checkpoints / loras / vae  (wired via extra_model_paths.yaml)
    output\     finished art (auto-saved) ; output/_raw is the engine scratch
"""

import base64
import ctypes
import json
import os
import re
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
import queue as queue_mod
from ctypes import wintypes
from datetime import datetime
from io import BytesIO
from pathlib import Path
from tkinter import (Tk, Text, Canvas, Listbox, StringVar, IntVar, BooleanVar,
                     DoubleVar, filedialog, messagebox, Toplevel, END, WORD,
                     NSEW, W, E)
from tkinter import ttk

import requests
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk
from PIL.PngImagePlugin import PngInfo

APP_VERSION = "1.12.0"

if getattr(sys, "frozen", False):
    # packaged onefile exe lives in the project root, next to Setup.exe
    PROJECT = Path(sys.executable).resolve().parent
    APP_DIR = PROJECT / "app"
else:
    APP_DIR = Path(__file__).resolve().parent
    PROJECT = APP_DIR.parent

ENGINE_DIR = PROJECT / "ComfyUI"


def engine_python():
    """Python runtime for the engine: the bundled runtime installed by
    Setup.exe first (works on machines with nothing installed), a dev venv
    as fallback."""
    for cand in (PROJECT / "python" / "python.exe",
                 PROJECT / "venv" / "Scripts" / "python.exe"):
        if cand.exists():
            return cand
    return PROJECT / "python" / "python.exe"

MODELS = PROJECT / "models"
OUTPUT = PROJECT / "output"
RAW_OUT = OUTPUT / "_raw"
SETTINGS_FILE = APP_DIR / "settings.json"
PRESETS_FILE = APP_DIR / "presets.json"
MANIFEST_FILE = APP_DIR / "models_manifest.json"
EXTRA_PATHS_YAML = PROJECT / "extra_model_paths.yaml"

def _contained_env():
    """Environment that keeps every helper's downloads/caches inside the
    project folder (nothing lands in the user profile or %LOCALAPPDATA%)."""
    env = dict(os.environ)
    env["U2NET_HOME"] = str(MODELS / "rembg")      # rembg BG-removal model
    env["HF_HOME"] = str(MODELS / "hf_cache")      # any HuggingFace caching
    env["PIP_NO_CACHE_DIR"] = "1"
    return env


UPSCALE_MODEL = "RealESRGAN_x4plus.pth"   # optional 4x hi-res pass

ENGINE_HOST = "127.0.0.1"   # loopback only — engine is never exposed to LAN
ENGINE_PORT = 8188
ENGINE_URL = f"http://{ENGINE_HOST}:{ENGINE_PORT}"
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

SIZE_PRESETS = {
    "Portrait — cover (832x1216)": (832, 1216),
    "Portrait — page (896x1152)": (896, 1152),
    "Square (1024x1024)": (1024, 1024),
    "Landscape — panel (1216x832)": (1216, 832),
    "Wide — splash (1344x768)": (1344, 768),
    "Large portrait (1024x1536)": (1024, 1536),
    "Large square (1408x1408)": (1408, 1408),
}

NONE_LORA = "— none —"
NONE_PRESET = "— none (raw prompt) —"

# colors
BG = "#17171c"
BG2 = "#20202a"
BG3 = "#2a2a38"
FG = "#e8e8f0"
FG_DIM = "#9a9ab0"
ACCENT = "#e94560"
ACCENT2 = "#4ecca3"


# --------------------------------------------------------------------------
# Windows DPAPI — encrypts the CivitAI key with the user's login credentials
# so settings.json never holds it in plaintext.
# --------------------------------------------------------------------------

class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data):
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_encrypt(text):
    if not text:
        return ""
    inb, out = _blob(text.encode("utf-8")), _DATA_BLOB()
    if ctypes.windll.crypt32.CryptProtectData(ctypes.byref(inb), None, None,
                                              None, None, 0, ctypes.byref(out)):
        try:
            return base64.b64encode(
                ctypes.string_at(out.pbData, out.cbData)).decode()
        finally:
            ctypes.windll.kernel32.LocalFree(out.pbData)
    return ""


def dpapi_decrypt(b64):
    if not b64:
        return ""
    try:
        inb, out = _blob(base64.b64decode(b64)), _DATA_BLOB()
        if ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(out)):
            try:
                return ctypes.string_at(out.pbData, out.cbData).decode("utf-8")
            finally:
                ctypes.windll.kernel32.LocalFree(out.pbData)
    except Exception:
        pass
    return ""


# --------------------------------------------------------------------------
# engine management + API
# --------------------------------------------------------------------------

VRAM_HEADROOM_GB = 2.0   # a model needs its size + this much working room


def gpu_vram_gb():
    """Total VRAM of the NVIDIA GPU in GB, or None if no NVIDIA GPU /
    driver is present. Works before the engine is up."""
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total",
                            "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return int(r.stdout.strip().splitlines()[0]) / 1024
    except Exception:
        pass
    return None


def engine_alive(timeout=2):
    try:
        requests.get(f"{ENGINE_URL}/system_stats", timeout=timeout)
        return True
    except requests.RequestException:
        return False


def start_engine():
    """Spawn headless ComfyUI with our model paths and scratch output dir."""
    # (re)write the model-paths config with THIS machine's absolute path,
    # so the folder works wherever it is unzipped
    EXTRA_PATHS_YAML.write_text(
        "comic_art_creator:\n"
        f"  base_path: {MODELS}\n"
        "  checkpoints: checkpoints\n"
        "  loras: loras\n"
        "  vae: vae\n"
        "  upscale_models: upscale_models\n"
        "  ipadapter: ipadapter\n"
        "  clip_vision: clip_vision\n"
        "  diffusion_models: diffusion_models\n"
        "  text_encoders: text_encoders\n", encoding="utf-8")
    cmd = [str(engine_python()), "main.py",
           "--listen", ENGINE_HOST, "--port", str(ENGINE_PORT),
           "--extra-model-paths-config", str(EXTRA_PATHS_YAML),
           "--output-directory", str(RAW_OUT),
           "--disable-auto-launch"]
    log = open(PROJECT / "engine.log", "w", encoding="utf-8", errors="replace")
    subprocess.Popen(cmd, cwd=str(ENGINE_DIR), stdout=log,
                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                     env=_contained_env())
    _mark_engine_owned()


ENGINE_OWNER_FILE = PROJECT / "engine_owner.json"


def _mark_engine_owned():
    """Record that THIS process started the engine, so a later boot can
    tell its own engine from a stray one left by another instance."""
    try:
        ENGINE_OWNER_FILE.write_text(
            json.dumps({"pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass


def engine_is_ours():
    """True if the live engine was started by a process that is still us
    or still running (our marker pid). A foreign/stale engine returns
    False so the caller can warn or restart it."""
    try:
        pid = json.loads(ENGINE_OWNER_FILE.read_text(encoding="utf-8"))["pid"]
    except (OSError, ValueError, KeyError):
        return False
    if pid == os.getpid():
        return True
    # is that pid still alive? (Windows tasklist check, no extra deps)
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, creationflags=NO_WINDOW,
            timeout=5).stdout
        return str(pid) in out
    except Exception:
        return False


def single_instance_handle():
    """Windows named mutex: returns (handle, already_running). Keep the
    handle alive for the process lifetime; None on non-Windows."""
    if os.name != "nt":
        return None, False
    try:
        h = ctypes.windll.kernel32.CreateMutexW(
            None, False, "Global\\ComicBookArtCreator_singleton")
        already = ctypes.windll.kernel32.GetLastError() == 183  # ALREADY_EXISTS
        return h, already
    except Exception:
        return None, False


def api_get(path):
    r = requests.get(f"{ENGINE_URL}{path}", timeout=15)
    r.raise_for_status()
    return r.json()


def scan_models(sub):
    """List model files straight from disk — works before the engine is up.

    safetensors only: .ckpt files are pickle-based and can execute code
    when loaded, so they are deliberately not offered.
    """
    d = MODELS / sub
    if not d.is_dir():
        return []
    return sorted((f.name for f in d.glob("*.safetensors")), key=str.lower)


def _ensure_lora_dir():
    d = MODELS / "loras"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _api_choices(node, field):
    try:
        return api_get(f"/object_info/{node}")[node]["input"]["required"][field][0]
    except Exception:
        return []


def list_checkpoints():
    return list(dict.fromkeys(
        _api_choices("CheckpointLoaderSimple", "ckpt_name")
        + scan_models("checkpoints")))


def list_loras():
    return list(dict.fromkeys(
        _api_choices("LoraLoader", "lora_name") + scan_models("loras")))


# the app's own versioned LoRAs (e.g. SDXL_BorderFrames_v3): only the
# current shipped version should be offered — hide superseded ones so
# old attempts that didn't work don't clutter the list. The canonical
# names are the module constants (e.g. BORDER_LORA_FILE), read at call
# time so there's a single source of truth.
def _canonical_family(fname):
    """('sdxl_borderframes', 3) for 'SDXL_BorderFrames_v3.safetensors',
    else None — the family key and version of a versioned LoRA name."""
    mobj = re.match(r"^(.*?)_v(\d+)\.safetensors$", fname, re.IGNORECASE)
    if not mobj:
        return None
    return mobj.group(1).lower(), int(mobj.group(2))


def _visible_loras(loras):
    canonical = {}   # family -> the one shipped filename to keep
    for fname in (BORDER_LORA_FILE,):
        fam = _canonical_family(fname)
        if fam:
            canonical[fam[0]] = fname
    out = []
    for name in loras:
        fam = _canonical_family(name)
        if fam and fam[0] in canonical and name != canonical[fam[0]]:
            continue   # a superseded version of one of our shipped LoRAs
        out.append(name)
    return out


# --------------------------------------------------------------------------
# model update check (HuggingFace)
# --------------------------------------------------------------------------

def check_model_updates():
    """Compare managed model files against their HuggingFace originals.

    Returns manifest entries whose remote size differs from the local file
    (an updated release) or whose local file is missing. Size comparison is
    cheap and catches every re-released file.
    """
    try:
        manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    updates, repo_cache = [], {}
    for entry in manifest:
        # direct-URL entry (e.g. our own trained LoRA on a GitHub release):
        # size comes from a HEAD request, not the HF blob API
        if entry.get("url"):
            local = MODELS / entry["dir"] / entry["local"]
            size = 0
            try:
                h = requests.head(entry["url"], timeout=20,
                                  allow_redirects=True)
                size = int(h.headers.get("content-length", 0))
            except requests.RequestException:
                pass
            if not local.exists():
                updates.append(dict(entry, size=size, missing=True))
            elif size and local.stat().st_size != size:
                updates.append(dict(entry, size=size, missing=False))
            continue
        repo = entry["repo"]
        if repo not in repo_cache:
            try:
                r = requests.get(
                    f"https://huggingface.co/api/models/{repo}?blobs=true",
                    timeout=20)
                repo_cache[repo] = r.json().get("siblings", []) if r.ok else []
            except requests.RequestException:
                repo_cache[repo] = []
        sib = next((s for s in repo_cache[repo]
                    if s.get("rfilename") == entry["remote_file"]), None)
        if not sib or not sib.get("size"):
            continue
        local = MODELS / entry["dir"] / entry["local"]
        if not local.exists() or local.stat().st_size != sib["size"]:
            updates.append(dict(entry, size=sib["size"],
                                missing=not local.exists()))
    return updates


def download_model_update(entry, status_cb, prog_cb=None):
    """Stream one updated model to a temp file, then swap it in place."""
    url = entry.get("url") or (
        f"https://huggingface.co/{entry['repo']}/resolve/main/"
        f"{entry['remote_file']}")
    dest = MODELS / entry["dir"] / entry["local"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    done = 0
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0)) \
            or entry.get("size") or 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 22):
                fh.write(chunk)
                done += len(chunk)
                if prog_cb and total:
                    prog_cb(done, total)
                if done % (1 << 26) < (1 << 22):  # every ~64 MB
                    status_cb(f"Updating {entry['local']}: "
                              f"{done / (1024**2):.0f}"
                              + (f"/{total / (1024**2):.0f} MB"
                                 if total else " MB"))
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(tmp, dest)
        return True
    except PermissionError:
        # engine holds the old file — leave .tmp; applied on next start
        return False


# --------------------------------------------------------------------------
# engine (tool) update check — GitHub master vs the recorded install
# --------------------------------------------------------------------------

ENGINE_VERSION_FILE = PROJECT / "engine_version.json"
IPA_NODE_ZIP = ("https://github.com/cubiq/ComfyUI_IPAdapter_plus/"
                "archive/refs/heads/main.zip")
COMFY_COMMITS_API = ("https://api.github.com/repos/comfyanonymous/"
                     "ComfyUI/commits/master")
COMFY_ZIP_URL = ("https://github.com/comfyanonymous/ComfyUI/"
                 "archive/refs/heads/master.zip")


def check_engine_update():
    """Return {'sha': new} when GitHub has a newer engine than recorded.
    First run on an existing install records the current state silently."""
    try:
        r = requests.get(COMFY_COMMITS_API, timeout=20,
                         headers={"Accept": "application/vnd.github+json"})
        if not r.ok:
            return None
        sha = r.json()["sha"]
    except Exception:
        return None
    try:
        local = json.loads(
            ENGINE_VERSION_FILE.read_text(encoding="utf-8"))["sha"]
    except Exception:
        try:
            ENGINE_VERSION_FILE.write_text(json.dumps({"sha": sha}),
                                           encoding="utf-8")
        except OSError:
            pass
        return None
    return {"sha": sha} if sha != local else None


APP_RELEASES_API = ("https://api.github.com/repos/TheSaltTrader/"
                    "Comic-Book-Art-Generator/releases/latest")


def _version_tuple(v):
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) if nums else (0,)


def check_app_update():
    """Return {'tag','zip_url'} when GitHub's latest release is newer than
    this running build, else None. Only meaningful for the frozen exe."""
    try:
        r = requests.get(APP_RELEASES_API, timeout=20,
                         headers={"Accept": "application/vnd.github+json"})
        if not r.ok:
            return None
        j = r.json()
        tag = j.get("tag_name", "")
        if _version_tuple(tag) <= _version_tuple(APP_VERSION):
            return None
        zurl = next((a["browser_download_url"] for a in j.get("assets", [])
                     if a.get("name", "").lower().endswith(".zip")), None)
        return {"tag": tag, "zip_url": zurl} if zurl else None
    except Exception:
        return None


def apply_app_update(zip_url, status_cb):
    """Download the release zip, extract the exe(s), and swap them in.
    Renames the running exe aside (Windows can't overwrite a running exe),
    copies the new one in, and returns the path to relaunch."""
    tmp = PROJECT / "_app_upd_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    zpath = tmp / "release.zip"
    status_cb("Downloading the new version…")
    with requests.get(zip_url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(zpath, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    status_cb("Unpacking…")
    with zipfile.ZipFile(zpath) as z:
        z.extractall(tmp)
    new_app = next(tmp.rglob("ComicArtCreator.exe"), None)
    new_setup = next(tmp.rglob("Setup.exe"), None)
    if not new_app:
        raise RuntimeError("the release zip has no ComicArtCreator.exe")
    for cur, new in ((PROJECT / "ComicArtCreator.exe", new_app),
                     (PROJECT / "Setup.exe", new_setup)):
        if not new:
            continue
        try:
            if cur.exists():
                old = cur.with_name(cur.stem + "_old_"
                                    + str(os.getpid()) + ".exe")
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    pass
                os.replace(cur, old)
            shutil.copy2(new, cur)
        except OSError as e:
            raise RuntimeError(f"could not replace {cur.name}: {e}")
    shutil.rmtree(tmp, ignore_errors=True)
    return PROJECT / "ComicArtCreator.exe"


def kill_engine():
    """Stop any running ComfyUI engine process (ours from this or a
    previous session)."""
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
         "Where-Object { $_.CommandLine -like '*ComfyUI*main.py*' } | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"],
        capture_output=True, creationflags=NO_WINDOW, timeout=60)


def update_engine(new_sha, status_cb):
    """Swap in the latest engine, preserving user data, then refresh its
    python packages."""
    tmp = PROJECT / "_upd_tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        z = tmp / "comfy.zip"
        status_cb("Downloading engine update…")
        with requests.get(COMFY_ZIP_URL, stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(z, "wb") as fh:
                for chunk in r.iter_content(1 << 22):
                    fh.write(chunk)
        status_cb("Installing engine update…")
        with zipfile.ZipFile(z) as zf:
            zf.extractall(tmp)
        new_dir = next(tmp.glob("ComfyUI-*"))
        keep = tmp / "keep"
        keep.mkdir()
        for sub in ("user", "input", "custom_nodes"):
            s = ENGINE_DIR / sub
            if s.exists():
                shutil.move(str(s), str(keep / sub))
        shutil.rmtree(ENGINE_DIR)
        shutil.move(str(new_dir), str(ENGINE_DIR))
        for sub in ("user", "input", "custom_nodes"):
            s = keep / sub
            if s.exists():
                shutil.move(str(s), str(ENGINE_DIR / sub))
        status_cb("Updating engine packages…")
        subprocess.run([str(engine_python()), "-m", "pip", "install", "-q",
                        "-r", str(ENGINE_DIR / "requirements.txt")],
                       capture_output=True, creationflags=NO_WINDOW,
                       env=_contained_env(), timeout=1800)
        ENGINE_VERSION_FILE.write_text(json.dumps({"sha": new_sha}),
                                       encoding="utf-8")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# workflow builder
# --------------------------------------------------------------------------

def model_family(name):
    n = name.lower()
    if "schnell" in n:
        return "schnell"
    if "flux" in n:
        return "flux"
    if "turbo" in n or "lightning" in n:
        return "turbo"
    if "animagine" in n or "illustrious" in n or "noob" in n or "pony" in n:
        return "anime"
    return "sdxl"


FAMILY_DEFAULTS = {
    "flux":  dict(steps=20, cfg=1.0, sampler="euler", scheduler="simple"),
    "schnell": dict(steps=4, cfg=1.0, sampler="euler", scheduler="simple"),
    "turbo": dict(steps=8,  cfg=2.0, sampler="dpmpp_sde", scheduler="karras"),
    "anime": dict(steps=28, cfg=5.5, sampler="euler_ancestral", scheduler="normal"),
    "sdxl":  dict(steps=30, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
}


EDITOR_ENGINES = {
    "Flux Kontext — best overall edits": "kontext",
    "Qwen Image Edit — best text removal (Apache)": "qwen",
}
EDITOR_VRAM = {"kontext": 16, "qwen": 24, "wan": 16, "wanflf": 20}
KONTEXT_FILE = "flux1-dev-kontext_fp8_scaled.safetensors"
QWEN_EDIT_FILE = "qwen_image_edit_2511_fp8mixed.safetensors"
QWEN_TE_FILE = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
QWEN_VAE_FILE = "qwen_image_vae.safetensors"
FLUX_CLIP_SRC = "flux1-dev-fp8.safetensors"   # donates CLIP+VAE to Kontext


def build_kontext_graph(p):
    """Instruction edit via FLUX.1 Kontext: the loaded image(s) are the
    context, the prompt says what to change."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": KONTEXT_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": FLUX_CLIP_SRC}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 1]}}
    img_ref, nid, sid = None, 10, 20
    for name in p["edit_image_names"][:4]:
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        if img_ref is None:
            img_ref = [str(nid), 0]
        else:
            g[str(sid)] = {"class_type": "ImageStitch",
                           "inputs": {"image1": img_ref,
                                      "image2": [str(nid), 0],
                                      "direction": "right",
                                      "match_image_size": True,
                                      "spacing_width": 0,
                                      "spacing_color": "white"}}
            img_ref = [str(sid), 0]
            sid += 1
        nid += 1
    g["30"] = {"class_type": "FluxKontextImageScale",
               "inputs": {"image": img_ref}}
    g["31"] = {"class_type": "VAEEncode",
               "inputs": {"pixels": ["30", 0], "vae": ["2", 2]}}
    g["32"] = {"class_type": "ReferenceLatent",
               "inputs": {"conditioning": ["4", 0], "latent": ["31", 0]}}
    g["33"] = {"class_type": "FluxGuidance",
               "inputs": {"conditioning": ["32", 0], "guidance": 2.5}}
    g["34"] = {"class_type": "ConditioningZeroOut",
               "inputs": {"conditioning": ["4", 0]}}
    # by default the output canvas follows the reference; with out_size
    # the reference only conditions and a fresh canvas sets the size
    latent_ref = ["31", 0]
    if p.get("out_size"):
        ow, oh = p["out_size"]
        g["35"] = {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": ow, "height": oh, "batch_size": 1}}
        latent_ref = ["35", 0]
    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["33", 0],
                         "negative": ["34", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": p.get("steps") or 20,
                         "cfg": 1.0, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0}}
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["2", 2]}}
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": ["7", 0]}}
    return g


def build_qwen_edit_graph(p):
    """Instruction edit via Qwen-Image-Edit (Apache 2.0) — strongest at
    removing/altering text in images."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": QWEN_EDIT_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": QWEN_TE_FILE, "type": "qwen_image",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": QWEN_VAE_FILE}}
    load_refs, nid = [], 10
    for name in p["edit_image_names"][:3]:
        g[str(nid)] = {"class_type": "LoadImage", "inputs": {"image": name}}
        load_refs.append([str(nid), 0])
        nid += 1
    enc_pos = {"clip": ["2", 0], "prompt": p["prompt"], "vae": ["3", 0]}
    enc_neg = {"clip": ["2", 0], "prompt": "", "vae": ["3", 0]}
    for i, ref in enumerate(load_refs, 1):
        enc_pos[f"image{i}"] = ref
        enc_neg[f"image{i}"] = ref
    g["20"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": enc_pos}
    g["21"] = {"class_type": "TextEncodeQwenImageEditPlus", "inputs": enc_neg}
    g["30"] = {"class_type": "ImageScaleToTotalPixels",
               "inputs": {"image": load_refs[0],
                          "upscale_method": "lanczos", "megapixels": 1.0,
                          "resolution_steps": 1}}
    g["31"] = {"class_type": "VAEEncode",
               "inputs": {"pixels": ["30", 0], "vae": ["3", 0]}}
    latent_ref = ["31", 0]
    if p.get("out_size"):
        ow, oh = p["out_size"]
        g["35"] = {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": ow, "height": oh, "batch_size": 1}}
        latent_ref = ["35", 0]
    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["20", 0],
                         "negative": ["21", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": p.get("steps") or 20,
                         "cfg": 2.5, "sampler_name": "euler",
                         "scheduler": "simple", "denoise": 1.0}}
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["3", 0]}}
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": ["7", 0]}}
    return g


WAN_FILE = "wan2.2_ti2v_5B_fp16.safetensors"
WAN_TE_FILE = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
WAN_VAE_FILE = "wan2.2_vae.safetensors"
WAN_NEGATIVE = ("blurry, distorted, deformed, extra limbs, morphing, text, "
                "watermark, background clutter, scene change, camera "
                "movement, zoom, pan")
ANIM_SIZES = {
    "Square (704x704)": (704, 704),
    "Landscape (1280x704)": (1280, 704),
    "Portrait (704x1280)": (704, 1280),
}
ANIM_KEEP = {
    "all frames (24 fps)": 1,
    "every 2nd (12 fps)": 2,
    "every 3rd (8 fps)": 3,
    "every 4th (6 fps)": 4,
}
ANIM_LOOPS = ["Seamless (auto-cut — default)",
              "Ping-pong (perfect loop)", "Crossfade (blend ends)"]
ANIM_PRESET_HINT = "— pick a preset action —"
ANIM_PRESETS = {
    "Attacking (melee combo)":
        "attacking with a rapid melee combo, throwing alternating punches "
        "and strikes with the whole upper body, hips rotating into each "
        "blow, feet shifting stance, continuous repeating attack cycle",
    "Blocking (raise guard)":
        "raising both arms into a defensive block, bracing and flinching "
        "against impacts, body rocking back slightly with each hit "
        "absorbed, guard bobbing up and down in a repeating cycle",
    "Casting a spell":
        "casting a spell with both arms sweeping in wide circular "
        "gestures, hands weaving in front of the chest, clothes and hair "
        "flowing with the motion, body swaying, continuous repeating "
        "casting cycle",
    "Celebrating (victory)":
        "celebrating a victory, pumping both fists into the air "
        "repeatedly, bouncing on the spot, head thrown back, whole body "
        "bouncing with joy in a continuous repeating cycle",
    "Climbing":
        "climbing upward hand over hand, arms reaching up alternately, "
        "knees lifting high to find footholds, whole body pulling upward "
        "in a clear repeating climbing cycle",
    "Crouching":
        "crouching down low and rising back up, knees bending deeply, "
        "arms out for balance, weight shifting smoothly, continuous "
        "repeating crouch cycle",
    "Dancing":
        "dancing energetically, hips swaying side to side, arms waving "
        "above the head, feet stepping in rhythm, whole body grooving in "
        "a continuous repeating dance cycle",
    "Dodging":
        "dodging side to side, ducking and weaving with quick head and "
        "torso movement, feet shuffling, shoulders dipping left and "
        "right in a fast repeating cycle",
    "Dying (defeated)":
        "staggering and collapsing in defeat, clutching the chest, knees "
        "buckling, falling to the knees and slumping forward with "
        "dramatic full-body motion",
    "Falling":
        "falling through the air, arms and legs flailing, body tumbling "
        "slightly, hair and clothes whipping upward, continuous dramatic "
        "falling motion",
    "Flying":
        "flying forward with cape and clothes rippling in the wind, arms "
        "stretched ahead, body bobbing gently up and down, legs "
        "trailing, continuous smooth flying cycle",
    "Idle (breathing)":
        "standing idle in a ready stance, chest rising and falling with "
        "deep breaths, arms swaying slightly, weight shifting gently "
        "from foot to foot in a subtle repeating idle cycle",
    "Jumping":
        "jumping straight up and landing, knees bending deep before "
        "launch, arms swinging up for lift, feet leaving the ground, "
        "landing in a crouch, continuous repeating jump cycle",
    "Kicking":
        "throwing high kicks, legs snapping up alternately toward head "
        "height, arms out for balance, hips rotating into each kick, "
        "continuous repeating kicking cycle",
    "Laughing":
        "laughing hard, head thrown back, shoulders shaking up and "
        "down, one hand slapping the knee, belly heaving, whole body "
        "bouncing in a continuous repeating laugh",
    "Punching":
        "throwing powerful alternating punches, fists snapping forward "
        "one after the other, shoulders and hips rotating into each "
        "punch, feet planted in a fighting stance, continuous repeating "
        "punching cycle",
    "Running":
        "running fast with knees lifting high, arms pumping hard, feet "
        "leaving the ground mid-stride, body leaning forward, hair and "
        "clothes streaming back, continuous repeating running cycle",
    "Shooting":
        "aiming and firing a weapon, arms raised, recoil kicking the "
        "arms and shoulders back with each shot, body bracing and "
        "recovering, continuous repeating shooting cycle",
    "Slashing (sword)":
        "slashing with a sword in wide sweeping arcs, blade swinging "
        "diagonally across the body, hips and shoulders rotating into "
        "each swing, feet shifting stance, continuous repeating "
        "slashing cycle",
    "Sneaking":
        "sneaking forward on tiptoe with exaggerated slow steps, knees "
        "lifting high, body crouched low, arms raised carefully, head "
        "scanning side to side, continuous repeating sneaking cycle",
    "Stomping walk (heavy)":
        "stomping forward with heavy powerful steps, whole body "
        "shifting weight side to side with each footfall, arms swinging "
        "with momentum, shoulders rocking, continuous heavy walking "
        "cycle",
    "Taunting":
        "taunting mockingly, beckoning with one hand in a come-here "
        "gesture, shrugging shoulders, head tilting side to side, body "
        "swaying cockily in a continuous repeating cycle",
    "Walking":
        "walking forward with a steady stride, legs alternating in full "
        "steps, arms swinging naturally at the sides, whole body moving "
        "in a continuous walk cycle",
    "Walking (side view)":
        "walking in place in profile view, legs lifting and stepping in "
        "a clear repeating cycle, knees rising visibly, arms pumping "
        "back and forth, exaggerated cartoon walk cycle",
    "Waving":
        "waving hello with one arm raised high, hand sweeping side to "
        "side widely, shoulders and torso swaying along, continuous "
        "repeating waving cycle",
}


def frame_diff(a, b):
    """Mean pixel difference between two frames (0-255 scale)."""
    hst = ImageChops.difference(a.convert("RGB"),
                                b.convert("RGB")).histogram()
    return sum(hst[i % 256] * (i % 256)
               for i in range(len(hst))) / (a.width * a.height * 3)


def best_loop_cut(frames, min_len=8):
    """Cut the clip at its most similar frame pair — but only among
    segments that actually CONTAIN motion. Without the motion filter the
    cutter grabs the quietest chunk of a weak clip and the loop looks
    nearly static."""
    n = len(frames)
    if n < min_len + 3:
        return frames
    small = [f.convert("L").resize((64, 64)) for f in frames]

    def sdiff(a, b):
        hst = ImageChops.difference(a, b).histogram()
        return sum(hst[i] * i for i in range(len(hst))) / (64 * 64)

    succ = [sdiff(small[k], small[k + 1]) for k in range(n - 1)]
    clip_motion = sum(succ) / max(1, len(succ))
    best, fallback = None, None
    for i in range(0, max(1, n // 3)):
        for j in range(i + min_len, n):
            d = sdiff(small[i], small[j])
            if fallback is None or d < fallback[0]:
                fallback = (d, i, j)
            seg = succ[i:j - 1]
            seg_motion = sum(seg) / max(1, len(seg))
            if seg_motion < 0.6 * clip_motion:
                continue   # quiet segment — would loop as near-static
            if best is None or d < best[0]:
                best = (d, i, j)
    _, i, j = best if best is not None else fallback
    return frames[i:j]
ANIM_MOTION = {
    "Strong (recommended)":
        "Large, exaggerated, theatrical motion — the whole body moves "
        "dramatically and continuously through every single frame",
    "Normal":
        "Clear, continuous full-body motion throughout the clip",
    "Subtle":
        "Subtle, natural motion",
}
WAN_FLF_FILE = "wan2.1_flf2v_720p_14B_fp8_e4m3fn.safetensors"
WAN_CLIPVIS_FILE = "clip_vision_h.safetensors"
WAN21_VAE_FILE = "wan_2.1_vae.safetensors"


def build_wan_flf_graph(p):
    """Seamless loop via Wan 2.1 first-last-frame: the animation starts
    AND ends on the character's exact pose, so it loops playing forward —
    no ping-pong, no crossfade."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_FLF_FILE,
                         "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_TE_FILE, "type": "wan",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": WAN21_VAE_FILE}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 0]}}
    g["5"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", WAN_NEGATIVE),
                         "clip": ["2", 0]}}
    g["6"] = {"class_type": "LoadImage",
              "inputs": {"image": p["anim_image_name"]}}
    g["11"] = {"class_type": "CLIPVisionLoader",
               "inputs": {"clip_name": WAN_CLIPVIS_FILE}}
    g["12"] = {"class_type": "CLIPVisionEncode",
               "inputs": {"clip_vision": ["11", 0], "image": ["6", 0],
                          "crop": "none"}}
    g["7"] = {"class_type": "WanFirstLastFrameToVideo",
              "inputs": {"positive": ["4", 0], "negative": ["5", 0],
                         "vae": ["3", 0], "width": p["width"],
                         "height": p["height"], "length": p["length"],
                         "batch_size": 1,
                         "clip_vision_start_image": ["12", 0],
                         "clip_vision_end_image": ["12", 0],
                         "start_image": ["6", 0], "end_image": ["6", 0]}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["7", 0],
                         "negative": ["7", 1], "latent_image": ["7", 2],
                         "seed": p["seed"], "steps": p.get("steps") or 30,
                         "cfg": 5.5,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "cbac_anim",
                          "images": ["9", 0]}}
    return g


def build_wan_graph(p):
    """Image-to-video via Wan 2.2 5B: the character image animates per
    the action prompt; every frame comes back as an image."""
    g = {}
    g["1"] = {"class_type": "UNETLoader",
              "inputs": {"unet_name": WAN_FILE, "weight_dtype": "default"}}
    g["2"] = {"class_type": "CLIPLoader",
              "inputs": {"clip_name": WAN_TE_FILE, "type": "wan",
                         "device": "default"}}
    g["3"] = {"class_type": "VAELoader",
              "inputs": {"vae_name": WAN_VAE_FILE}}
    g["4"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": ["2", 0]}}
    g["5"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", WAN_NEGATIVE),
                         "clip": ["2", 0]}}
    g["6"] = {"class_type": "LoadImage",
              "inputs": {"image": p["anim_image_name"]}}
    g["7"] = {"class_type": "Wan22ImageToVideoLatent",
              "inputs": {"vae": ["3", 0], "width": p["width"],
                         "height": p["height"], "length": p["length"],
                         "batch_size": 1, "start_image": ["6", 0]}}
    g["8"] = {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "positive": ["4", 0],
                         "negative": ["5", 0], "latent_image": ["7", 0],
                         "seed": p["seed"], "steps": 20, "cfg": 5.0,
                         "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}}
    g["9"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["8", 0], "vae": ["3", 0]}}
    g["10"] = {"class_type": "SaveImage",
               "inputs": {"filename_prefix": "cbac_anim",
                          "images": ["9", 0]}}
    return g


_REMBG_DIR_CODE = (
    "import sys, pathlib; from PIL import Image; "
    "from rembg import remove, new_session; "
    "src = pathlib.Path(sys.argv[1]); dst = pathlib.Path(sys.argv[2]); "
    "s = new_session(sys.argv[3]); "
    "[remove(Image.open(f).convert('RGBA'), session=s).save(dst / f.name) "
    "for f in sorted(src.glob('*.png'))]")


def remove_background_dir(src_dir, dst_dir):
    """One rembg session for a whole folder of frames."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    (MODELS / "rembg").mkdir(parents=True, exist_ok=True)
    r = subprocess.run([str(engine_python()), "-c", _REMBG_DIR_CODE,
                        str(src_dir), str(dst_dir), "isnet-general-use"],
                       capture_output=True, text=True, timeout=1800,
                       creationflags=NO_WINDOW, env=_contained_env())
    if r.returncode != 0:
        raise RuntimeError(f"frame background removal failed: "
                           f"{r.stderr[-400:]}")


STAGE_BG = (200, 200, 205)   # the neutral staging color used for prep


def estimate_bg(img, patch=24):
    """Actual background color of a RAW (pre-cutout) frame, sampled from
    its four corners — the video model repaints the stage, so the true
    color drifts from the nominal staging gray."""
    import numpy as np
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    corners = np.concatenate([
        arr[:patch, :patch].reshape(-1, 3),
        arr[:patch, -patch:].reshape(-1, 3),
        arr[-patch:, :patch].reshape(-1, 3),
        arr[-patch:, -patch:].reshape(-1, 3)])
    return tuple(float(v) for v in corners.mean(axis=0))


def defringe(img, bg=STAGE_BG, erode=1, feather=0.5, alpha_floor=30):
    """Un-blend the background color out of semi-transparent edge pixels
    (pass the color measured from the RAW frame via estimate_bg), drop
    faint halo pixels, then tighten and smooth the silhouette."""
    import numpy as np
    arr = np.asarray(img.convert("RGBA")).astype(np.float32)
    a = arr[..., 3:4] / 255.0
    rgb = arr[..., :3]
    bg_arr = np.array(bg, dtype=np.float32)
    fg = (rgb - bg_arr * (1.0 - a)) / np.maximum(a, 1e-4)
    fg = np.clip(fg, 0, 255)
    alpha_ch = np.where(arr[..., 3] < alpha_floor, 0.0, arr[..., 3])
    out = np.concatenate([fg, alpha_ch[..., None]],
                         axis=-1).astype(np.uint8)
    res = Image.fromarray(out, "RGBA")
    alpha = res.getchannel("A")
    if erode:
        alpha = alpha.filter(ImageFilter.MinFilter(erode * 2 + 1))
    if feather:
        alpha = alpha.filter(ImageFilter.GaussianBlur(feather))
    res.putalpha(alpha)
    return res


def apply_loop(frames, mode):
    if mode.startswith("Ping-pong") and len(frames) > 2:
        return frames + frames[-2:0:-1]
    if mode.startswith("Crossfade") and len(frames) > 8:
        k = min(6, len(frames) // 4)
        out = list(frames)
        for i in range(k):
            t = (i + 1) / (k + 1)
            idx = len(frames) - k + i
            out[idx] = Image.blend(frames[idx].convert("RGBA"),
                                   frames[i].convert("RGBA"), t)
        return out
    return list(frames)


def save_gif(frames, path, fps, transparent):
    dur = max(20, int(round(1000 / fps)))
    if transparent:
        conv = []
        for f in frames:
            rgba = f.convert("RGBA")
            alpha = rgba.getchannel("A")
            p_img = rgba.convert("RGB").convert(
                "P", palette=Image.ADAPTIVE, colors=255)
            mask = alpha.point(lambda a: 255 if a <= 128 else 0)
            p_img.paste(255, mask)
            conv.append(p_img)
        conv[0].save(path, save_all=True, append_images=conv[1:],
                     duration=dur, loop=0, disposal=2, transparency=255)
    else:
        rgb = [f.convert("RGB") for f in frames]
        rgb[0].save(path, save_all=True, append_images=rgb[1:],
                    duration=dur, loop=0)


def save_video(frames, path, fps, webm=False, bg=(20, 20, 26)):
    """Encode frames to MP4 (h264) or WebM (vp9) via PyAV — self-contained
    (PyAV ships its own ffmpeg, so no external install). Video can't carry
    transparency, so RGBA frames are composited on a solid background;
    the GIF/sprite-sheet keep the alpha."""
    import av
    import numpy as np
    w, h = frames[0].size
    w -= w % 2
    h -= h % 2   # h264/vp9 need even dimensions
    container = av.open(str(path), mode="w")
    codec = "libvpx-vp9" if webm else "libx264"
    stream = container.add_stream(codec, rate=int(round(fps)))
    stream.width, stream.height = w, h
    stream.pix_fmt = "yuv420p"
    if not webm:
        stream.options = {"crf": "18", "preset": "medium"}
    try:
        for f in frames:
            flat = Image.new("RGB", (w, h), bg)
            rgba = f.convert("RGBA").resize((w, h), Image.LANCZOS)
            flat.paste(rgba, (0, 0), rgba)
            vf = av.VideoFrame.from_ndarray(np.asarray(flat), format="rgb24")
            for pkt in stream.encode(vf):
                container.mux(pkt)
        for pkt in stream.encode():
            container.mux(pkt)
    finally:
        container.close()


def save_sprite_sheet(frames, png_path, json_path, fps):
    """Pack frames into a single grid PNG (alpha preserved) with a JSON
    atlas describing the layout — drop-in for game engines/frontends."""
    import json as _json
    import math
    n = len(frames)
    fw, fh = frames[0].size
    cols = int(math.ceil(math.sqrt(n)))
    rows = int(math.ceil(n / cols))
    sheet = Image.new("RGBA", (cols * fw, rows * fh), (0, 0, 0, 0))
    for i, f in enumerate(frames):
        sheet.paste(f.convert("RGBA"), ((i % cols) * fw, (i // cols) * fh))
    sheet.save(png_path)
    _json.dump({"frame_width": fw, "frame_height": fh, "columns": cols,
                "rows": rows, "frame_count": n, "fps": int(round(fps))},
               open(json_path, "w", encoding="utf-8"), indent=2)


def build_graph(p):
    """Build a ComfyUI prompt graph from generation params dict."""
    if p.get("edit_image_names"):
        return build_qwen_edit_graph(p) if p.get("editor") == "qwen" \
            else build_kontext_graph(p)
    fam = model_family(p["model"])
    d = FAMILY_DEFAULTS[fam]
    steps = p.get("steps") or d["steps"]
    cfg = p.get("cfg") if p.get("cfg") is not None else d["cfg"]

    g = {}
    g["1"] = {"class_type": "CheckpointLoaderSimple",
              "inputs": {"ckpt_name": p["model"]}}

    # optional lora chain: model+clip pass through each loader
    model_ref, clip_ref = ["1", 0], ["1", 1]
    nid = 20
    for lora_name, strength in p.get("loras", []):
        g[str(nid)] = {"class_type": "LoraLoader",
                       "inputs": {"lora_name": lora_name,
                                  "strength_model": strength,
                                  "strength_clip": strength,
                                  "model": model_ref, "clip": clip_ref}}
        model_ref, clip_ref = [str(nid), 0], [str(nid), 1]
        nid += 1

    g["2"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p["prompt"], "clip": clip_ref}}
    g["3"] = {"class_type": "CLIPTextEncode",
              "inputs": {"text": p.get("negative", ""), "clip": clip_ref}}

    pos_ref = ["2", 0]
    if fam in ("flux", "schnell"):
        g["4"] = {"class_type": "FluxGuidance",
                  "inputs": {"guidance": 3.5, "conditioning": pos_ref}}
        pos_ref = ["4", 0]

    # style reference (IP-Adapter): the refs teach the model a look, the
    # prompt keeps full control of the composition — SDXL families only
    if p.get("style_ref_names"):
        img_ref, nid, bid = None, 31, 41
        for name in p["style_ref_names"][:6]:
            g[str(nid)] = {"class_type": "LoadImage",
                           "inputs": {"image": name}}
            if img_ref is None:
                img_ref = [str(nid), 0]
            else:
                g[str(bid)] = {"class_type": "ImageBatch",
                               "inputs": {"image1": img_ref,
                                          "image2": [str(nid), 0]}}
                img_ref = [str(bid), 0]
                bid += 1
            nid += 1
        g["50"] = {"class_type": "IPAdapterUnifiedLoader",
                   "inputs": {"preset": "PLUS (high strength)",
                              "model": model_ref}}
        g["51"] = {"class_type": "IPAdapter",
                   "inputs": {"model": ["50", 0], "ipadapter": ["50", 1],
                              "image": img_ref,
                              "weight": p.get("style_weight", 0.8),
                              "start_at": 0.0, "end_at": 1.0,
                              "weight_type": p.get("ref_weight_type",
                                                   "standard")}}
        model_ref = ["51", 0]

    denoise = 1.0
    latent_ref = ["5", 0]
    if p.get("border_assets"):
        # masked generation: art is only painted in the border zone, the
        # center physically stays empty regardless of the theme
        bg_name, mask_name = p["border_assets"]
        g["10"] = {"class_type": "LoadImage", "inputs": {"image": bg_name}}
        g["5"] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": ["10", 0], "vae": ["1", 2]}}
        g["12"] = {"class_type": "LoadImage", "inputs": {"image": mask_name}}
        g["13"] = {"class_type": "ImageToMask",
                   "inputs": {"image": ["12", 0], "channel": "red"}}
        g["14"] = {"class_type": "SetLatentNoiseMask",
                   "inputs": {"samples": ["5", 0], "mask": ["13", 0]}}
        latent_ref = ["14", 0]
    elif p.get("ref_image_name"):
        # img2img: reference image -> scale to canvas -> encode to latent
        g["10"] = {"class_type": "LoadImage",
                   "inputs": {"image": p["ref_image_name"]}}
        g["11"] = {"class_type": "ImageScale",
                   "inputs": {"image": ["10", 0], "width": p["width"],
                              "height": p["height"],
                              "upscale_method": "lanczos",
                              "crop": "center"}}
        g["5"] = {"class_type": "VAEEncode",
                  "inputs": {"pixels": ["11", 0], "vae": ["1", 2]}}
        denoise = p.get("denoise", 0.6)
    elif fam in ("flux", "schnell"):
        g["5"] = {"class_type": "EmptySD3LatentImage",
                  "inputs": {"width": p["width"], "height": p["height"],
                             "batch_size": 1}}
    else:
        g["5"] = {"class_type": "EmptyLatentImage",
                  "inputs": {"width": p["width"], "height": p["height"],
                             "batch_size": 1}}

    g["6"] = {"class_type": "KSampler",
              "inputs": {"model": model_ref, "positive": pos_ref,
                         "negative": ["3", 0], "latent_image": latent_ref,
                         "seed": p["seed"], "steps": steps, "cfg": cfg,
                         "sampler_name": d["sampler"], "scheduler": d["scheduler"],
                         "denoise": denoise}}
    g["7"] = {"class_type": "VAEDecode",
              "inputs": {"samples": ["6", 0], "vae": ["1", 2]}}
    img_out = ["7", 0]
    # optional hi-res pass: run the decoded image through a 4x upscale model
    if p.get("upscale") and (MODELS / "upscale_models" / UPSCALE_MODEL).exists():
        g["40"] = {"class_type": "UpscaleModelLoader",
                   "inputs": {"model_name": UPSCALE_MODEL}}
        g["41"] = {"class_type": "ImageUpscaleWithModel",
                   "inputs": {"upscale_model": ["40", 0], "image": img_out}}
        img_out = ["41", 0]
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": img_out}}
    return g


# --------------------------------------------------------------------------
# background removal (transparency) — runs in the engine venv so the
# packaged exe stays small and rembg's heavy deps never enter this process.
# --------------------------------------------------------------------------

_REMBG_CODE = ("import sys; from PIL import Image; "
               "from rembg import remove, new_session; "
               "img = Image.open(sys.argv[1]).convert('RGBA'); "
               "remove(img, session=new_session(sys.argv[3])).save(sys.argv[2])")


def remove_background(img):
    RAW_OUT.mkdir(parents=True, exist_ok=True)
    (MODELS / "rembg").mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RAW_OUT) as td:
        src, dst = Path(td) / "in.png", Path(td) / "out.png"
        img.save(src)
        r = subprocess.run([str(engine_python()), "-c", _REMBG_CODE, str(src),
                            str(dst), "isnet-general-use"],
                           capture_output=True, text=True, timeout=600,
                           creationflags=NO_WINDOW, env=_contained_env())
        if r.returncode != 0 or not dst.exists():
            raise RuntimeError(f"background removal failed: {r.stderr[-400:]}")
        out = Image.open(dst)
        out.load()
        return out


BORDER_SIZES = {
    "4:3  (1152x864)": (1152, 864),
    "16:9 (1280x720)": (1280, 720),
    "16:9 HD (1920x1080 — best on Flux)": (1920, 1080),
}
BORDER_NEGATIVE = ("text, letters, words, numbers, writing, typography, "
                   "logo, watermark, signature, caption, label, subtitles, "
                   "title banner, gibberish text, full scene, landscape, "
                   "scenery in the center, filled center, busy center, "
                   "background illustration in the middle, picture in the "
                   "middle, framed painting, content in the center, "
                   "full body character standing in the center, person in "
                   "the middle, large figure in the center, mascot in the "
                   "center, character blocking the center")

BORDER_CLEAN_PROMPT = (
    "Remove everything inside the frame. The entire center area must "
    "become plain solid white, completely empty, with no characters, no "
    "creatures, no objects, no scenery and no text in the middle. Keep "
    "the decorative border frame around the edges exactly as it is, "
    "unchanged. Only empty out the center.")
BORDER_SAME_MODEL = "Same as main (default)"
BORDER_LORA_FILE = "SDXL_BorderFrames_v1.safetensors"
BORDER_TRIGGER = "cbacframe"
BORDER_TEMPLATE = (
    "a highly detailed ornate decorative {theme} frame, presented as a "
    "single frame-shaped object floating at the center of a plain solid "
    "pure white background, a wide even margin of empty white space "
    "separates the ornate frame from all four edges so the frame does "
    "NOT touch the edges of the image, the entire middle is plain empty "
    "pure white with absolutely nothing in it, the frame has an "
    "irregular decorative outer silhouette with protruding ornaments and "
    "an intricate organic inner edge, elaborate corner pieces and themed "
    "cartouches built from the props, symbols and colors of {theme}, "
    "ornaments of varying depth, ornate game UI bezel, concept art "
    "quality, intricate detail, completely textless with no lettering, "
    "only the frame itself is colored and detailed, everything around it "
    "and in the center is pure flat white")



def _add_margin(img, margin_pct):
    """Shrink the (already alpha-cut) frame and center it on a fully
    transparent canvas, so the frame floats free of the image edges like
    the reference bezels instead of bleeding to the border."""
    if margin_pct <= 0:
        return img
    w, h = img.size
    scale = 1.0 - 2 * margin_pct / 100.0
    sw, sh = max(1, int(w * scale)), max(1, int(h * scale))
    small = img.resize((sw, sh), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(small, ((w - sw) // 2, (h - sh) // 2), small)
    return canvas


def _center_hole(img, thresh=70):
    """Flood the plain central region by its own color and return
    (hole_bool_array, frac, edge_touch). A clean framed center is a large
    single void that does not reach the image edges."""
    import numpy as np
    w, h = img.size
    cx, cy = w // 2, h // 2
    seeds = [(cx, cy), (cx, int(h * 0.35)), (cx, int(h * 0.65)),
             (int(w * 0.3), cy), (int(w * 0.7), cy),
             (int(w * 0.4), int(h * 0.4)), (int(w * 0.6), int(h * 0.6))]
    fill = img.convert("RGB")
    sent = (255, 0, 255)
    for s in seeds:
        try:
            ImageDraw.floodfill(fill, s, sent, thresh=thresh)
        except Exception:
            pass
    a = np.asarray(fill)
    hole = ((a[..., 0] == 255) & (a[..., 1] == 0) & (a[..., 2] == 255))
    frac = float(hole.mean())
    edge_touch = bool(hole[0].any() or hole[-1].any()
                      or hole[:, 0].any() or hole[:, -1].any())
    return hole, frac, edge_touch


def border_center_clean(img):
    """True when the frame already has a large, clean, enclosed empty
    center (so no second Kontext clean-up pass is needed)."""
    try:
        _hole, frac, edge_touch = _center_hole(img.convert("RGBA"))
        return 0.30 < frac < 0.9 and not edge_touch
    except Exception:
        return True   # never block generation on a detection hiccup


def cut_center(img, thickness_pct, margin_pct=6):
    """Turn a frame render into a floating transparent-background bezel.

    The border LoRA draws the frame in color with a plain (white/grey)
    empty center. We flood that center region by its OWN color so the
    transparency follows the frame's REAL organic inner edge (octagon,
    arch, scalloped, …) instead of a rectangular cut. Then we add a
    transparent outer margin so the frame doesn't touch the screen edge.

    Falls back to a soft rectangle only when no clean center is found
    (e.g. the theme filled the middle), so a border is never worse than
    before."""
    import numpy as np
    img = img.convert("RGBA")
    w, h = img.size
    rect_t = max(8, int(min(w, h) * thickness_pct / 100))

    def rect_alpha():
        m = Image.new("L", (w, h), 255)
        ImageDraw.Draw(m).rectangle(
            (rect_t, rect_t, w - rect_t, h - rect_t), fill=0)
        return m.filter(ImageFilter.GaussianBlur(2))

    try:
        # generous threshold keys the plain center whatever its shade,
        # following its organic outline; must be a real central void
        hole, frac, edge_touch = _center_hole(img, 70)
        if 0.18 < frac < 0.9 and not edge_touch:
            alpha = np.where(hole, 0, 255).astype(np.uint8)
            # never eat the outermost frame pixels
            m = max(3, rect_t // 3)
            alpha[:m, :] = 255; alpha[-m:, :] = 255
            alpha[:, :m] = 255; alpha[:, -m:] = 255
            am = Image.fromarray(alpha, "L").filter(
                ImageFilter.GaussianBlur(1.2))
            img.putalpha(am)
            return _add_margin(img, margin_pct)
    except Exception:
        pass
    img.putalpha(rect_alpha())
    return _add_margin(img, margin_pct)


# --------------------------------------------------------------------------
# generation worker
# --------------------------------------------------------------------------

class ChannelQueue:
    """Relabels progress messages so each section drives its own bar."""

    def __init__(self, q, channel):
        self.q, self.channel = q, channel

    def put(self, msg):
        if msg and msg[0] == "progress":
            self.q.put((self.channel,) + tuple(msg[1:]))
        else:
            self.q.put(msg)


def load_ragmap(path):
    """Load a .ragmap.json produced alongside a trained LoRA. Resolves
    each entry's image to an absolute path (relative to the map file /
    its image_dir). Schema — cbac-ragmap/1:
      {"schema":"cbac-ragmap/1","name":..,"lora":<file>,"trigger":<word>,
       "image_dir":"images","weight":0.8,"top_k":4,
       "entries":[{"image":"0001.png","keywords":[...],"caption":"..."}]}"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    base = Path(path).resolve().parent
    img_dir = base / data.get("image_dir", "")
    for e in data.get("entries", []):
        ip = Path(e.get("image", ""))
        e["_path"] = str(ip if ip.is_absolute() else (img_dir / ip))
    data["_base"] = str(base)
    return data


def ragmap_retrieve(ragmap, prompt, k=None):
    """Return the top-k entries whose keywords/caption best match the
    prompt words (falls back to the first k valid entries if nothing
    matches, so the LoRA always gets representative guidance)."""
    if not ragmap:
        return []
    k = k or int(ragmap.get("top_k", 4))
    words = set(re.findall(r"[a-z0-9]+", (prompt or "").lower()))
    scored = []
    for e in ragmap.get("entries", []):
        if not Path(e.get("_path", "")).exists():
            continue
        text = " ".join(e.get("keywords", [])) + " " + e.get("caption", "")
        ewords = set(re.findall(r"[a-z0-9]+", text.lower()))
        scored.append((len(words & ewords), e))
    scored.sort(key=lambda t: t[0], reverse=True)   # stable → original order on ties
    return [e for _s, e in scored[:k]]


def make_collage(paths, w, h):
    """Compose the reference image(s) onto one w x h canvas — a lone ref
    is letterboxed, several tile in a grid. The editor redraws this
    canvas, so its aspect drives the output aspect."""
    canvas = Image.new("RGB", (w, h), (32, 32, 36))
    n = max(1, len(paths))
    cols = max(1, min(n, int((n * w / max(1, h)) ** 0.5 + 0.5)))
    rows = -(-n // cols)
    cw, ch = w // cols, h // rows
    for k, p in enumerate(paths):
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        img.thumbnail((cw, ch), Image.LANCZOS)
        canvas.paste(img, ((k % cols) * cw + (cw - img.width) // 2,
                           (k // cols) * ch + (ch - img.height) // 2))
    return canvas


class Generator:
    """Queues a prompt and streams progress/results back through a queue."""

    def __init__(self, ui_queue):
        self.q = ui_queue
        self.client_id = str(uuid.uuid4())

    def run(self, params):
        try:
            self._run(params)
        except Exception as e:
            self.q.put(("error", f"{type(e).__name__}: {e}"))

    def _run(self, params):
        import websocket  # websocket-client
        if params.get("ref_images"):
            # instruction editing: the images are context for the prompt
            if params.get("ref_collage_size"):
                # border mode: shape the refs to the border canvas so the
                # editor's output keeps the requested aspect
                w0, h0 = params["ref_collage_size"]
                collage = make_collage(params["ref_images"], w0, h0)
                params["edit_image_names"] = [
                    self._upload_pil(collage, "cbac_border_ref.png")]
            else:
                params["edit_image_names"] = [self._upload_ref(rp)
                                              for rp in params["ref_images"]]
        # RAG map: upload the retrieved example images so build_graph can
        # feed them through IP-Adapter as visual guidance
        if params.get("rag_ref_paths"):
            names = []
            for rp in params["rag_ref_paths"]:
                try:
                    if Path(rp).exists():
                        names.append(self._upload_ref(rp))
                except Exception:
                    pass
            if names:
                params["style_ref_names"] = names
        # prompt-only borders now generate the FULL frame (no restrictive
        # edge-band mask): the model/LoRA draws a complete ornate frame
        # with a plain center, and cut_center carves the center transparent
        # along the frame's real inner silhouette afterwards.
        ws = websocket.WebSocket()
        ws.connect(f"ws://{ENGINE_HOST}:{ENGINE_PORT}/ws?clientId={self.client_id}",
                   timeout=30)
        try:
            for i in range(params["batch"]):
                p = dict(params)
                if i > 0:
                    p["seed"] = random.randrange(2**32) if params["random_seed"] \
                        else params["seed"] + i
                self.q.put(("status", f"Generating {i + 1}/{params['batch']} "
                                      f"(seed {p['seed']})…"))
                graph = build_graph(p)
                r = requests.post(f"{ENGINE_URL}/prompt",
                                  json={"prompt": graph,
                                        "client_id": self.client_id},
                                  timeout=30)
                if r.status_code == 400:
                    try:
                        j = r.json()
                        msg = j.get("error", {}).get("message",
                                                     "invalid workflow")
                        bad = ", ".join(sorted(
                            v.get("class_type", k)
                            for k, v in (j.get("node_errors") or {}).items()))
                        if bad:
                            msg += f" (nodes: {bad})"
                    except Exception:
                        msg = "engine rejected the workflow"
                    raise RuntimeError(
                        f"Engine rejected the request: {msg}. If this "
                        "mentions IPAdapter, use the style/character mode "
                        "once more — the app will offer to install the "
                        "missing add-on.")
                r.raise_for_status()
                prompt_id = r.json()["prompt_id"]
                images = self._await_images(ws, prompt_id)
                for img_meta in images:
                    img = self._fetch_image(img_meta)
                    if params.get("border_clean") \
                            and not params.get("edit_image_names") \
                            and not border_center_clean(img):
                        img = self._clean_border_center(ws, img, p)
                    self.q.put(("image", img, p))
            self.q.put(("done", None))
        finally:
            ws.close()

    def _clean_border_center(self, ws, img, p):
        """Second pass: the frame came out with content in the middle —
        run it through Flux Kontext to empty the center, keeping the
        frame. Returns the cleaned image, or the original on any failure
        so a border is never lost to the clean-up step."""
        try:
            self.q.put(("status", "Cleaning the center (2nd pass, "
                                  "Flux Kontext)…"))
            name = self._upload_pil(img.convert("RGB"),
                                    "cbac_border_pre.png")
            cp = dict(p, prompt=BORDER_CLEAN_PROMPT,
                      edit_image_names=[name], editor="kontext",
                      out_size=(img.width, img.height), steps=20)
            graph = build_kontext_graph(cp)
            r = requests.post(f"{ENGINE_URL}/prompt",
                              json={"prompt": graph,
                                    "client_id": self.client_id},
                              timeout=30)
            r.raise_for_status()
            cleaned = self._await_images(ws, r.json()["prompt_id"])
            if cleaned:
                return self._fetch_image(cleaned[0])
        except Exception as e:
            self.q.put(("status", f"Center clean-up skipped ({e}); using "
                                  "the original frame."))
        return img

    def _upload_pil(self, img, name):
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        r = requests.post(f"{ENGINE_URL}/upload/image",
                          files={"image": (name, buf)},
                          data={"overwrite": "true"}, timeout=60)
        r.raise_for_status()
        j = r.json()
        return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") \
            else j["name"]

    def _upload_ref(self, path):
        """Upload the reference image to the engine's input folder."""
        with open(path, "rb") as fh:
            r = requests.post(f"{ENGINE_URL}/upload/image",
                              files={"image": (Path(path).name, fh)},
                              data={"overwrite": "true"}, timeout=60)
        r.raise_for_status()
        j = r.json()
        name = j["name"]
        if j.get("subfolder"):
            name = f"{j['subfolder']}/{name}"
        return name

    def _await_images(self, ws, prompt_id, timeout=600):
        ws.settimeout(timeout)
        loading_told = False
        while True:
            msg = ws.recv()
            if isinstance(msg, bytes):
                continue  # binary preview frames — ignored
            data = json.loads(msg)
            t, d = data.get("type"), data.get("data", {})
            if t == "progress":
                loading_told = True
                self.q.put(("progress", d.get("value", 0), d.get("max", 1)))
            elif t == "execution_error" and d.get("prompt_id") == prompt_id:
                raise RuntimeError(d.get("exception_message", "engine error"))
            elif t == "executing" and d.get("prompt_id") == prompt_id:
                if d.get("node") is None:
                    break  # finished
                if not loading_told:
                    loading_told = True
                    self.q.put(("status",
                                "Loading the model into GPU memory — the "
                                "bar starts once it's loaded (big models "
                                "take a while on first use)…"))
        hist = api_get(f"/history/{prompt_id}")[prompt_id]
        images = []
        for node_out in hist["outputs"].values():
            images.extend(node_out.get("images", []))
        return images

    def _fetch_image(self, meta):
        r = requests.get(f"{ENGINE_URL}/view",
                         params={"filename": meta["filename"],
                                 "subfolder": meta.get("subfolder", ""),
                                 "type": meta.get("type", "output")},
                         timeout=60)
        r.raise_for_status()
        return Image.open(BytesIO(r.content)).convert("RGBA")


# --------------------------------------------------------------------------
# main window
# --------------------------------------------------------------------------

class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Comic Book Art Creator v{APP_VERSION}")
        root.geometry("1500x940")
        root.minsize(1200, 780)
        root.configure(bg=BG)
        self._style()

        self.presets = json.loads(PRESETS_FILE.read_text(encoding="utf-8"))["presets"]
        self.settings = self._load_settings()
        self.ui_queue = queue_mod.Queue()
        self.session = []          # list of (PIL image, params, path)
        self.current = None        # index into session
        self.busy = False
        self.job_queue = []        # batch queue of pending jobs
        self._batch_active = False
        self.ragmap = None         # loaded RAG map (dict) or None
        self.ragmap_path = None
        self._auto_negative = None  # last negative set by a preset (vs typed)
        self._model_display = {}    # "name · 6.5 GB" -> raw filename
        self._pending_loras = set()
        self._persist_job = None
        self.ref_paths = []        # img2img reference images
        self.border_ref_paths = []  # border-maker reference images
        self.anim_image_path = None  # animator character image
        self.vram_gb = gpu_vram_gb()   # None = no NVIDIA GPU detected
        self._model_fits = {}      # raw name -> fits in VRAM
        self._last_fit_display = ""
        self._warned_vram = set()   # tight-fit warnings shown once each

        self._build_ui()
        self._apply_ui_state(self.settings.get("ui", {}))
        self._refresh_models()     # disk scan — fills dropdowns before engine
        self._wire_autosave()      # every change saved as it happens
        self._schedule_persist()   # baseline save right away
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll_queue)
        threading.Thread(target=self._boot_engine, daemon=True).start()
        threading.Thread(target=self._check_updates_bg, daemon=True).start()
        if self.vram_gb is not None:
            threading.Thread(target=self._vram_poll, daemon=True).start()
        root.after(600, self._first_run_check)

    def _vram_poll(self):
        """Live GPU memory readings for the top-right meter."""
        while True:
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used,memory.total",
                     "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=10,
                    creationflags=NO_WINDOW)
                if r.returncode == 0 and r.stdout.strip():
                    used, total = [int(x.strip()) for x in
                                   r.stdout.strip().splitlines()[0]
                                   .split(",")]
                    self.ui_queue.put(("vram_live", used, total))
            except Exception:
                pass
            time.sleep(3)

    def _first_run_check(self):
        """On a machine where Setup hasn't run (or ran engine-only),
        say so plainly and offer to launch Setup.exe."""
        runtime_missing = not (PROJECT / "python" / "python.exe").exists() \
            and not (PROJECT / "venv" / "Scripts" / "python.exe").exists()
        models_missing = not scan_models("checkpoints")
        if not (runtime_missing or models_missing):
            return
        setup_exe = PROJECT / "Setup.exe"
        what = []
        if runtime_missing:
            what.append("the AI engine")
        if models_missing:
            what.append("the models")
        msg = (f"This installation is missing {' and '.join(what)} — "
               "the app cannot create art until Setup completes the "
               "installation.\n\n")
        if setup_exe.exists():
            msg += ("Run Setup now? It downloads everything needed and "
                    "this app will find it automatically afterwards "
                    "(hit ↻ or restart).")
            if messagebox.askyesno("Setup required", msg):
                subprocess.Popen([str(setup_exe)])
        else:
            messagebox.showinfo(
                "Setup required",
                msg + "Setup.exe was not found next to this app — unzip "
                      "the full release and run Setup.exe first.")

    # -------------------------------------------------- ui scaffolding
    def _style(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure(".", background=BG, foreground=FG, fieldbackground=BG3,
                    bordercolor=BG3, lightcolor=BG3, darkcolor=BG3,
                    troughcolor=BG2, arrowcolor=FG, insertcolor=FG)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Dim.TLabel", foreground=FG_DIM)
        s.configure("Head.TLabel", foreground=ACCENT2,
                    font=("Segoe UI", 10, "bold"))
        s.configure("TButton", background=BG3, padding=6)
        s.map("TButton", background=[("active", "#3a3a4e")])
        s.configure("Go.TButton", background=ACCENT, foreground="white",
                    font=("Segoe UI", 12, "bold"), padding=10)
        s.map("Go.TButton", background=[("active", "#ff5e7a"),
                                        ("disabled", BG3)])
        s.configure("Danger.TButton", background="#c0392b",
                    foreground="white", padding=6)
        s.map("Danger.TButton", background=[("active", "#e74c3c")])
        s.configure("TCheckbutton", background=BG, foreground=FG)
        s.map("TCheckbutton", background=[("active", BG)])
        s.configure("TRadiobutton", background=BG, foreground=FG)
        s.map("TRadiobutton", background=[("active", BG)])
        # readonly comboboxes must show their saved value in a readable
        # colour in EVERY state (readonly, focused, hovered) — otherwise
        # the selected text renders in default colours that vanish against
        # the dark field and the box looks empty until you click it.
        s.configure("TCombobox", padding=4, foreground=FG,
                    fieldbackground=BG3, background=BG3,
                    selectbackground=BG3, selectforeground=FG)
        s.map("TCombobox",
              fieldbackground=[("readonly", BG3), ("focus", BG3)],
              foreground=[("readonly", FG), ("focus", FG),
                          ("disabled", FG_DIM)],
              selectbackground=[("readonly", BG3), ("focus", BG3)],
              selectforeground=[("readonly", FG), ("focus", FG)],
              background=[("readonly", BG3)])
        for name, col in (("Fit", ACCENT2), ("Warn", "#e74c3c"),
                          ("NoFit", "#77778a")):
            s.configure(f"{name}.TCombobox", padding=4, foreground=col,
                        fieldbackground=BG3, selectbackground=BG3,
                        selectforeground=col)
            s.map(f"{name}.TCombobox",
                  fieldbackground=[("readonly", BG3), ("focus", BG3)],
                  foreground=[("readonly", col), ("focus", col)],
                  selectbackground=[("readonly", BG3), ("focus", BG3)],
                  selectforeground=[("readonly", col), ("focus", col)])
        s.configure("Horizontal.TProgressbar", background=ACCENT2,
                    troughcolor=BG2)
        s.configure("TSpinbox", padding=4)
        s.configure("TFrame", background=BG)

    def _text(self, parent, height):
        return Text(parent, height=height, wrap=WORD, bg=BG3, fg=FG,
                    insertbackground=FG, relief="flat", padx=8, pady=6,
                    font=("Segoe UI", 10), undo=True)

    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=0, minsize=450)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        # ---------- left column: controls (scrollable) ----------
        left_wrap = ttk.Frame(root)
        left_wrap.grid(row=0, column=0, sticky=NSEW)
        left_wrap.rowconfigure(0, weight=1)
        left_wrap.columnconfigure(0, weight=1)
        self.left_canvas = Canvas(left_wrap, bg=BG, highlightthickness=0,
                                  width=432)
        self.left_canvas.grid(row=0, column=0, sticky=NSEW)
        left_sb = ttk.Scrollbar(left_wrap, orient="vertical",
                                command=self.left_canvas.yview)
        left_sb.grid(row=0, column=1, sticky="ns")
        self.left_canvas.configure(yscrollcommand=left_sb.set)
        left = ttk.Frame(self.left_canvas, padding=12)
        left_win = self.left_canvas.create_window((0, 0), window=left,
                                                  anchor="nw")
        left.bind("<Configure>",
                  lambda _e: self.left_canvas.configure(
                      scrollregion=self.left_canvas.bbox("all")))
        self.left_canvas.bind(
            "<Configure>",
            lambda e: self.left_canvas.itemconfigure(left_win, width=e.width))

        def _wheel_router(e):
            # children swallow wheel events; route them to the panel
            # whenever the pointer is anywhere inside the left canvas
            w = self.root.winfo_containing(e.x_root, e.y_root)
            while w is not None:
                if w is self.lora_list:
                    return None      # its own scrollbar handles it
                if w is self.left_canvas:
                    self.left_canvas.yview_scroll(
                        -1 if e.delta > 0 else 1, "units")
                    return "break"
                w = w.master
            return None
        self.root.bind_all("<MouseWheel>", _wheel_router, add="+")
        left.columnconfigure(0, weight=1)
        r = 0

        ttk.Label(left, text="PROMPT", style="Head.TLabel").grid(row=r, sticky=W); r += 1
        self.prompt_box = self._text(left, 6)
        self.prompt_box.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1

        ttk.Label(left, text="NEGATIVE PROMPT (kept until you clear it)",
                  style="Head.TLabel").grid(row=r, sticky=W); r += 1
        self.negative_box = self._text(left, 2)
        self.negative_box.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1

        # model row — picked FIRST; the preset list adapts to it
        ttk.Label(left, text="MODEL (pick first)", style="Head.TLabel").grid(
            row=r, sticky=W); r += 1
        mrow = ttk.Frame(left); mrow.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1
        mrow.columnconfigure(0, weight=1)
        self.model_var = StringVar()
        self.model_dd = ttk.Combobox(mrow, textvariable=self.model_var,
                                     state="readonly", exportselection=False)
        self.model_dd.grid(row=0, column=0, sticky="ew")
        self.model_dd.bind("<<ComboboxSelected>>",
                           lambda _e: self._on_model_pick())
        ttk.Button(mrow, text="↻", width=3,
                   command=self._refresh_models).grid(row=0, column=1, padx=(6, 0))
        self.vram_note = StringVar(
            value="Sizes shown per model — your video card RAM must be above "
                  "that number (keep ~2 GB headroom).")
        ttk.Label(left, textvariable=self.vram_note, style="Dim.TLabel",
                  wraplength=400).grid(row=r, sticky=W); r += 1

        # preset row — filtered to styles that suit the selected model
        ttk.Label(left, text="ART STYLE PRESET (styles for the selected "
                             "model)", style="Head.TLabel").grid(
            row=r, sticky=W, pady=(6, 0)); r += 1
        prow = ttk.Frame(left); prow.grid(row=r, sticky=NSEW, pady=(2, 2)); r += 1
        prow.columnconfigure(0, weight=1)
        self.preset_var = StringVar(value="Marvel House Style")
        self.preset_dd = ttk.Combobox(prow, textvariable=self.preset_var,
                                      values=[NONE_PRESET], state="readonly", exportselection=False)
        self.preset_dd.grid(row=0, column=0, sticky="ew")
        self.preset_dd.bind("<<ComboboxSelected>>", self._on_preset)
        ttk.Button(prow, text="Try example", width=11,
                   command=self._use_example).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(prow, text="Clear", width=6,
                   command=self._clear_all).grid(row=0, column=2, padx=(6, 0))

        ttk.Label(left, text="Style text (appended to your prompt — editable):",
                  style="Dim.TLabel").grid(row=r, sticky=W, pady=(6, 0)); r += 1
        self.style_box = self._text(left, 3)
        self.style_box.grid(row=r, sticky=NSEW, pady=(2, 8)); r += 1

        # loras — one list, tick any number of them
        ttk.Label(left, text="LORAS (tick to apply — match SDXL/Flux to the model)",
                  style="Head.TLabel").grid(row=r, sticky=W); r += 1
        lframe = ttk.Frame(left); lframe.grid(row=r, sticky=NSEW, pady=2); r += 1
        lframe.columnconfigure(0, weight=1)
        self.lora_list = Listbox(lframe, selectmode="multiple",
                                 exportselection=False, height=6, bg=BG3,
                                 fg=FG, relief="flat", highlightthickness=0,
                                 selectbackground=ACCENT,
                                 selectforeground="white",
                                 activestyle="none", font=("Segoe UI", 9))
        self.lora_list.grid(row=0, column=0, sticky="ew")
        lsb = ttk.Scrollbar(lframe, orient="vertical",
                            command=self.lora_list.yview)
        self.lora_list.configure(yscrollcommand=lsb.set)
        lsb.grid(row=0, column=1, sticky="ns")
        self.lora_list.bind("<<ListboxSelect>>",
                            lambda _e: self._schedule_persist())
        limprow = ttk.Frame(left); limprow.grid(row=r, sticky=NSEW,
                                                pady=(2, 0)); r += 1
        ttk.Button(limprow, text="➕ Add LoRA file…",
                   command=self._import_lora).pack(side="left")
        ttk.Button(limprow, text="↻", width=3,
                   command=self._refresh_models).pack(side="left", padx=(4, 0))
        ttk.Button(limprow, text="📁 LoRA folder",
                   command=lambda: os.startfile(
                       str(_ensure_lora_dir()))).pack(side="left", padx=(4, 0))
        strow = ttk.Frame(left); strow.grid(row=r, sticky=NSEW, pady=(0, 4)); r += 1
        ttk.Label(strow, text="LoRA strength",
                  style="Dim.TLabel").pack(side="left")
        self.lora_strength = DoubleVar(value=0.8)
        ttk.Scale(strow, from_=0.0, to=1.5, variable=self.lora_strength,
                  length=160).pack(side="left", padx=6)
        self.lora_strength_lab = ttk.Label(strow, text="0.80", width=5,
                                           style="Dim.TLabel")
        self.lora_strength_lab.pack(side="left")
        self.lora_strength.trace_add(
            "write", lambda *_a: self.lora_strength_lab.config(
                text=f"{self.lora_strength.get():.2f}"))
        # RAG map — pairs example images with a LoRA; retrieves the closest
        # ones as visual guidance (IP-Adapter) at generation time
        ragrow = ttk.Frame(left); ragrow.grid(row=r, sticky=NSEW, pady=(2, 0))
        r += 1
        ragrow.columnconfigure(1, weight=1)
        ttk.Button(ragrow, text="🧭 RAG map…", width=12,
                   command=self._pick_ragmap).grid(row=0, column=0)
        self.ragmap_var = StringVar(value="none")
        ttk.Label(ragrow, textvariable=self.ragmap_var, style="Dim.TLabel",
                  wraplength=230).grid(row=0, column=1, sticky=W, padx=6)
        ttk.Button(ragrow, text="✕", width=3,
                   command=self._clear_ragmap).grid(row=0, column=2)

        # size + settings
        ttk.Label(left, text="CANVAS & SETTINGS", style="Head.TLabel").grid(
            row=r, sticky=W, pady=(8, 0)); r += 1
        srow = ttk.Frame(left); srow.grid(row=r, sticky=NSEW, pady=2); r += 1
        srow.columnconfigure(0, weight=1)
        self.size_var = StringVar(value="Wide — splash (1344x768)")
        ttk.Combobox(srow, textvariable=self.size_var, state="readonly",
                     exportselection=False,
                     values=list(SIZE_PRESETS)).grid(row=0, column=0, sticky="ew")

        grow = ttk.Frame(left); grow.grid(row=r, sticky=NSEW, pady=4); r += 1
        ttk.Label(grow, text="Steps", style="Dim.TLabel").grid(row=0, column=0)
        self.steps_var = StringVar(value="auto")
        ttk.Combobox(grow, textvariable=self.steps_var, width=6,
                     exportselection=False,
                     values=["auto", "8", "12", "20", "28", "35", "50"]).grid(
            row=0, column=1, padx=(4, 12))
        ttk.Label(grow, text="Variations", style="Dim.TLabel").grid(row=0,
                                                                    column=2)
        self.batch_var = IntVar(value=1)
        ttk.Spinbox(grow, from_=1, to=10, textvariable=self.batch_var,
                    exportselection=False,
                    width=4).grid(row=0, column=3, padx=(4, 12))
        self.transparent_var = BooleanVar(value=False)
        ttk.Checkbutton(grow, text="Transparent BG",
                        variable=self.transparent_var).grid(row=0, column=4)
        self.upscale_var = BooleanVar(value=False)
        ttk.Checkbutton(grow, text="Upscale 4x (hi-res)",
                        variable=self.upscale_var).grid(row=0, column=5,
                                                        padx=(12, 0))

        seedrow = ttk.Frame(left); seedrow.grid(row=r, sticky=NSEW, pady=4); r += 1
        ttk.Label(seedrow, text="Seed", style="Dim.TLabel").grid(row=0, column=0)
        self.seed_var = StringVar(value="0")
        self.seed_entry = ttk.Entry(seedrow, textvariable=self.seed_var,
                                    exportselection=False, width=12)
        self.seed_entry.grid(row=0, column=1, padx=(4, 10))
        self.random_seed_var = BooleanVar(value=True)
        ttk.Checkbutton(seedrow, text="Random",
                        variable=self.random_seed_var).grid(row=0, column=2)
        ttk.Button(seedrow, text="Reuse last", width=10,
                   command=self._reuse_seed).grid(row=0, column=3, padx=(10, 0))

        # image editor — Gemini-style instruction editing
        ttk.Label(left, text="IMAGE EDITOR (optional — load image(s) and "
                             "your prompt edits them: change things, remove "
                             "text, move characters to new scenes)",
                  style="Head.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W, pady=(8, 0)); r += 1
        rrow = ttk.Frame(left); rrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        rrow.columnconfigure(1, weight=1)
        ttk.Button(rrow, text="🖼 Load…", width=9,
                   command=self._pick_ref).grid(row=0, column=0)
        self.ref_var = StringVar(value="none — text only")
        ttk.Label(rrow, textvariable=self.ref_var, style="Dim.TLabel",
                  wraplength=160).grid(row=0, column=1, sticky=W, padx=6)
        self.editor_use_btn = ttk.Button(
            rrow, text="Use selected", width=12,
            command=self._use_selected_for_editor)
        self.editor_use_btn.grid(row=0, column=2, padx=(0, 4))
        self.editor_use_btn.state(["disabled"])   # until history has images
        ttk.Button(rrow, text="✕", width=3,
                   command=self._clear_ref).grid(row=0, column=3)
        erow = ttk.Frame(left); erow.grid(row=r, sticky=NSEW, pady=(0, 4)); r += 1
        erow.columnconfigure(1, weight=1)
        ttk.Label(erow, text="Editor", style="Dim.TLabel").grid(row=0,
                                                                column=0)
        self.editor_var = StringVar()
        self._editor_display = {}
        self.editor_dd = ttk.Combobox(erow, textvariable=self.editor_var,
                                      state="readonly",
                                      exportselection=False)
        self.editor_dd.grid(row=0, column=1, padx=(4, 0), sticky="ew")
        self.editor_dd.bind("<<ComboboxSelected>>",
                            lambda _e: self._on_editor_pick())
        self._refresh_editor_list()
        self.editor_canvas_var = BooleanVar(value=True)
        ttk.Checkbutton(left, text="Output at Canvas size",
                        variable=self.editor_canvas_var).grid(
            row=r, sticky=W); r += 1
        self.change_var = DoubleVar(value=60)   # border-ref influence

        # generate
        gorow = ttk.Frame(left); gorow.grid(row=r, sticky=NSEW,
                                            pady=(12, 4)); r += 1
        gorow.columnconfigure(0, weight=1)
        self.go_btn = ttk.Button(gorow, text="⚡  GENERATE", style="Go.TButton",
                                 command=self._generate)
        self.go_btn.grid(row=0, column=0, sticky=NSEW)
        ttk.Button(gorow, text="＋Q", width=4,
                   command=lambda: self._generate(queue=True)).grid(
            row=0, column=1, padx=(4, 0))
        pbrow = ttk.Frame(left); pbrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        pbrow.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(pbrow, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pct_var = StringVar(value="")
        ttk.Label(pbrow, textvariable=self.pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(pbrow, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))
        self.status_var = StringVar(value="Starting engine…")
        ttk.Label(left, textvariable=self.status_var,
                  style="Dim.TLabel", wraplength=400).grid(row=r, sticky=W); r += 1

        # ---------- animator (old-school sprite animation) ----------
        ttk.Label(left, text="ANIMATOR — animate a character image into "
                             "sprite frames & GIF", style="Head.TLabel",
                  wraplength=400, justify="left").grid(
            row=r, sticky=W, pady=(14, 0)); r += 1
        arow = ttk.Frame(left); arow.grid(row=r, sticky=NSEW, pady=2); r += 1
        arow.columnconfigure(1, weight=1)
        ttk.Button(arow, text="🖼 Character…", width=12,
                   command=self._pick_anim_image).grid(row=0, column=0)
        self.anim_img_var = StringVar(value="none")
        ttk.Label(arow, textvariable=self.anim_img_var, style="Dim.TLabel",
                  wraplength=180).grid(row=0, column=1, sticky=W, padx=6)
        ttk.Button(arow, text="Use current", width=11,
                   command=self._use_current_for_anim).grid(row=0, column=2)
        ttk.Button(arow, text="✕", width=3,
                   command=self._clear_anim_image).grid(row=0, column=3,
                                                        padx=(4, 0))
        prow = ttk.Frame(left); prow.grid(row=r, sticky=NSEW, pady=(4, 0))
        r += 1
        prow.columnconfigure(1, weight=1)
        ttk.Label(prow, text="Preset", style="Dim.TLabel").grid(row=0,
                                                                column=0)
        self.anim_preset_var = StringVar(value=ANIM_PRESET_HINT)
        pcb = ttk.Combobox(prow, textvariable=self.anim_preset_var,
                           state="readonly", exportselection=False,
                           values=list(ANIM_PRESETS), width=30)
        pcb.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        pcb.bind("<<ComboboxSelected>>", self._on_anim_preset)
        ttk.Label(left, text="Action (what the character does):",
                  style="Dim.TLabel").grid(row=r, sticky=W); r += 1
        self.anim_prompt_box = self._text(left, 2)
        self.anim_prompt_box.grid(row=r, sticky="ew", pady=(2, 4)); r += 1
        a2 = ttk.Frame(left); a2.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a2, text="Seconds", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.anim_secs_var = IntVar(value=3)
        ttk.Spinbox(a2, from_=1, to=5, textvariable=self.anim_secs_var,
                    exportselection=False, width=3).grid(row=0, column=1,
                                                         padx=(4, 10))
        ttk.Label(a2, text="Keep", style="Dim.TLabel").grid(row=0, column=2)
        self.anim_keep_var = StringVar(value=list(ANIM_KEEP)[1])
        ttk.Combobox(a2, textvariable=self.anim_keep_var, state="readonly",
                     exportselection=False, values=list(ANIM_KEEP),
                     width=17).grid(row=0, column=3, padx=(4, 0))
        a3 = ttk.Frame(left); a3.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a3, text="Loop", style="Dim.TLabel").grid(row=0, column=0)
        self.anim_loop_var = StringVar(value=ANIM_LOOPS[0])
        ttk.Combobox(a3, textvariable=self.anim_loop_var, state="readonly",
                     exportselection=False, values=ANIM_LOOPS,
                     width=22).grid(row=0, column=1, padx=(4, 10))
        self.anim_size_var = StringVar(value=list(ANIM_SIZES)[0])
        ttk.Combobox(a3, textvariable=self.anim_size_var, state="readonly",
                     exportselection=False, values=list(ANIM_SIZES),
                     width=18).grid(row=0, column=2)
        a3b = ttk.Frame(left); a3b.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a3b, text="Motion", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.anim_motion_var = StringVar(value=list(ANIM_MOTION)[0])
        ttk.Combobox(a3b, textvariable=self.anim_motion_var,
                     state="readonly", exportselection=False,
                     values=list(ANIM_MOTION),
                     width=22).grid(row=0, column=1, padx=(4, 0))
        a4 = ttk.Frame(left); a4.grid(row=r, sticky=NSEW, pady=2); r += 1
        self.anim_transparent_var = BooleanVar(value=True)
        ttk.Checkbutton(a4, text="Transparent frames",
                        variable=self.anim_transparent_var).pack(side="left")
        self.anim_gif_var = BooleanVar(value=True)
        ttk.Checkbutton(a4, text="Make GIF",
                        variable=self.anim_gif_var).pack(side="left",
                                                         padx=(12, 0))
        self.anim_zip_var = BooleanVar(value=True)
        ttk.Checkbutton(a4, text="Zip folder",
                        variable=self.anim_zip_var).pack(side="left",
                                                         padx=(12, 0))
        a5 = ttk.Frame(left); a5.grid(row=r, sticky=NSEW, pady=(0, 2)); r += 1
        self.anim_sheet_var = BooleanVar(value=False)
        ttk.Checkbutton(a5, text="Sprite sheet",
                        variable=self.anim_sheet_var).pack(side="left")
        self.anim_video_var = StringVar(value="none")
        ttk.Label(a5, text="Video", style="Dim.TLabel").pack(side="left",
                                                             padx=(12, 2))
        ttk.Combobox(a5, textvariable=self.anim_video_var, state="readonly",
                     exportselection=False,
                     values=["none", "MP4", "WebM"], width=7).pack(side="left")
        anrow = ttk.Frame(left); anrow.grid(row=r, sticky=NSEW,
                                            pady=(4, 2)); r += 1
        anrow.columnconfigure(0, weight=1)
        ttk.Button(anrow, text="🎬 Generate animation",
                   command=self._generate_animation).grid(row=0, column=0,
                                                          sticky="ew")
        ttk.Button(anrow, text="＋Q", width=4,
                   command=lambda: self._generate_animation(queue=True)).grid(
            row=0, column=1, padx=(4, 0))
        apb = ttk.Frame(left); apb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        apb.columnconfigure(0, weight=1)
        self.anim_progress = ttk.Progressbar(apb, mode="determinate")
        self.anim_progress.grid(row=0, column=0, sticky="ew")
        self.anim_pct_var = StringVar(value="")
        ttk.Label(apb, textvariable=self.anim_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(apb, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))

        # ---------- border maker (very bottom) ----------
        ttk.Label(left, text="BORDER MAKER — themed frame, transparent "
                             "center, no text", style="Head.TLabel").grid(
            row=r, sticky=W, pady=(14, 0)); r += 1
        ttk.Label(left, text="Border prompt — a theme (franchise, movie, "
                             "game, material) or a full precise prompt:",
                  style="Dim.TLabel").grid(row=r, sticky=W); r += 1
        self.border_prompt_box = self._text(left, 3)
        self.border_prompt_box.grid(row=r, sticky="ew", pady=(2, 2)); r += 1
        self.border_auto_var = BooleanVar(value=True)
        ttk.Checkbutton(left, text="Add frame wording automatically "
                                   "(uncheck to use your prompt verbatim)",
                        variable=self.border_auto_var).grid(
            row=r, sticky=W, pady=(0, 4)); r += 1
        bmod = ttk.Frame(left); bmod.grid(row=r, sticky=NSEW, pady=2); r += 1
        bmod.columnconfigure(1, weight=1)
        ttk.Label(bmod, text="Model", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.border_model_var = StringVar(value=BORDER_SAME_MODEL)
        self.border_model_dd = ttk.Combobox(bmod,
                                            textvariable=self.border_model_var,
                                            state="readonly",
                                            exportselection=False, width=34)
        self.border_model_dd.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.border_model_dd.bind("<<ComboboxSelected>>",
                                  lambda _e: self._refresh_border_styles())
        bsty = ttk.Frame(left); bsty.grid(row=r, sticky=NSEW, pady=2); r += 1
        bsty.columnconfigure(1, weight=1)
        ttk.Label(bsty, text="Style", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.border_style_var = StringVar(value=NONE_PRESET)
        self.border_style_dd = ttk.Combobox(bsty,
                                            textvariable=self.border_style_var,
                                            state="readonly",
                                            exportselection=False, width=34)
        self.border_style_dd.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(left, text="LoRAs, Variations, Steps, Seed and Editor "
                             "still come from the main controls.",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        barow = ttk.Frame(left); barow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(barow, text="Aspect", style="Dim.TLabel").grid(row=0,
                                                                 column=0)
        self.border_aspect_var = StringVar(value=list(BORDER_SIZES)[2])
        ttk.Combobox(barow, textvariable=self.border_aspect_var,
                     state="readonly", exportselection=False,
                     values=list(BORDER_SIZES),
                     width=30).grid(row=0, column=1, padx=(4, 0), sticky=W)
        brefrow = ttk.Frame(left); brefrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        brefrow.columnconfigure(1, weight=1)
        ttk.Button(brefrow, text="🖼 Refs…", width=8,
                   command=self._pick_border_refs).grid(row=0, column=0)
        self.border_ref_var = StringVar(value="none")
        ttk.Label(brefrow, textvariable=self.border_ref_var,
                  style="Dim.TLabel", wraplength=250).grid(row=0, column=1,
                                                           sticky=W, padx=6)
        ttk.Button(brefrow, text="✕", width=3,
                   command=self._clear_border_refs).grid(row=0, column=2)
        ttk.Label(left, text="Refs use the Image editor above: they are "
                             "redrawn as the border (style, characters and "
                             "composition carry over).",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        btrow = ttk.Frame(left); btrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(btrow, text="Thickness", style="Dim.TLabel").pack(side="left")
        self.border_thick_var = DoubleVar(value=14)
        ttk.Scale(btrow, from_=6, to=30, variable=self.border_thick_var,
                  length=150).pack(side="left", padx=6)
        self.border_thick_lab = ttk.Label(btrow, text="14%", width=5,
                                          style="Dim.TLabel")
        self.border_thick_lab.pack(side="left")
        self.border_thick_var.trace_add(
            "write", lambda *_a: self.border_thick_lab.config(
                text=f"{int(self.border_thick_var.get())}%"))
        bvrow = ttk.Frame(left); bvrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(bvrow, text="Variations", style="Dim.TLabel").pack(
            side="left")
        self.border_batch_var = IntVar(value=1)
        ttk.Spinbox(bvrow, from_=1, to=10, textvariable=self.border_batch_var,
                    exportselection=False, width=4).pack(side="left",
                                                         padx=(6, 0))
        ttk.Label(bvrow, text="(makes several borders from one prompt so you "
                             "can pick the best)", style="Dim.TLabel").pack(
            side="left", padx=(8, 0))
        self.border_clean_var = BooleanVar(value=True)
        ttk.Checkbutton(left, text="Auto-clean center (2nd pass with Flux "
                                   "Kontext when a theme fills the middle)",
                        variable=self.border_clean_var).grid(
            row=r, sticky=W, pady=(2, 0)); r += 1
        borow = ttk.Frame(left); borow.grid(row=r, sticky=NSEW,
                                            pady=(4, 2)); r += 1
        borow.columnconfigure(0, weight=1)
        ttk.Button(borow, text="⚡ Generate border",
                   command=self._generate_border).grid(row=0, column=0,
                                                       sticky="ew")
        ttk.Button(borow, text="＋Q", width=4,
                   command=lambda: self._generate_border(queue=True)).grid(
            row=0, column=1, padx=(4, 0))
        bpb = ttk.Frame(left); bpb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        bpb.columnconfigure(0, weight=1)
        self.border_progress = ttk.Progressbar(bpb, mode="determinate")
        self.border_progress.grid(row=0, column=0, sticky="ew")
        self.border_pct_var = StringVar(value="")
        ttk.Label(bpb, textvariable=self.border_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
        ttk.Button(bpb, text="✕ Cancel", width=9,
                   command=self._cancel_generation).grid(row=0, column=2,
                                                         padx=(4, 0))

        # ---------- batch queue (very bottom) ----------
        self.queue_count_var = StringVar(value="Batch queue (0)")
        ttk.Label(left, textvariable=self.queue_count_var,
                  style="Head.TLabel").grid(row=r, sticky=W,
                                            pady=(14, 0)); r += 1
        ttk.Label(left, text="Use ＋Q next to any Generate button to add the "
                             "current settings as a job, then Run all.",
                  style="Dim.TLabel", wraplength=400,
                  justify="left").grid(row=r, sticky=W); r += 1
        qframe = ttk.Frame(left); qframe.grid(row=r, sticky=NSEW, pady=2)
        r += 1
        qframe.columnconfigure(0, weight=1)
        self.queue_list = Listbox(qframe, selectmode="extended", height=4,
                                  bg=BG3, fg=FG, relief="flat",
                                  highlightthickness=0, activestyle="none",
                                  exportselection=False, font=("Segoe UI", 9))
        self.queue_list.grid(row=0, column=0, sticky="ew")
        qsb = ttk.Scrollbar(qframe, orient="vertical",
                            command=self.queue_list.yview)
        self.queue_list.configure(yscrollcommand=qsb.set)
        qsb.grid(row=0, column=1, sticky="ns")
        qbtns = ttk.Frame(left); qbtns.grid(row=r, sticky=NSEW,
                                            pady=(2, 8)); r += 1
        ttk.Button(qbtns, text="▶ Run all",
                   command=self._run_queue).pack(side="left")
        ttk.Button(qbtns, text="Remove",
                   command=self._remove_queued).pack(side="left", padx=6)
        ttk.Button(qbtns, text="Clear",
                   command=self._clear_queue).pack(side="left")

        # ---------- right column: preview + gallery ----------
        right = ttk.Frame(root, padding=(0, 12, 12, 12))
        right.grid(row=0, column=1, sticky=NSEW)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        # live VRAM meter (top right): green bar = used / max MB
        vrow = ttk.Frame(right)
        vrow.grid(row=0, column=0, sticky="e", pady=(0, 6))
        self.vram_label_var = StringVar(
            value="GPU — MB" if self.vram_gb is not None
            else "no NVIDIA GPU")
        ttk.Label(vrow, textvariable=self.vram_label_var,
                  style="Dim.TLabel").pack(side="left", padx=(0, 8))
        self.vram_bar = ttk.Progressbar(vrow, mode="determinate",
                                        length=220)
        self.vram_bar.pack(side="left")

        self.canvas = Canvas(right, bg=BG2, highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky=NSEW)
        self.canvas.bind("<Configure>", lambda e: self._show_current())

        brow = ttk.Frame(right); brow.grid(row=2, column=0, sticky=NSEW, pady=(8, 4))
        ttk.Button(brow, text="💾 Save As…", command=self._save_as).pack(side="left")
        ttk.Button(brow, text="🗑 Delete image",
                   command=self._delete_current).pack(side="left", padx=6)
        ttk.Button(brow, text="📁 Open output folder",
                   command=lambda: os.startfile(OUTPUT)).pack(side="left", padx=6)
        ttk.Button(brow, text="⬇ Get LoRAs (CivitAI)",
                   command=self._civitai_dialog).pack(side="left", padx=6)
        ttk.Button(brow, text="⭐ Add to training set",
                   command=self._add_to_training).pack(side="left", padx=6)
        self.info_var = StringVar(value="")
        ttk.Label(brow, textvariable=self.info_var,
                  style="Dim.TLabel").pack(side="right")

        # gallery strip — horizontally scrollable, with its own clear button
        gwrap = ttk.Frame(right)
        gwrap.grid(row=3, column=0, sticky=NSEW, pady=(4, 0))
        gwrap.columnconfigure(0, weight=1)
        self.gallery_canvas = Canvas(gwrap, height=96, bg=BG,
                                     highlightthickness=0)
        self.gallery_canvas.grid(row=0, column=0, sticky="ew")
        self.gallery = ttk.Frame(self.gallery_canvas)
        self._gallery_win = self.gallery_canvas.create_window(
            (0, 0), window=self.gallery, anchor="nw")
        gsb = ttk.Scrollbar(gwrap, orient="horizontal",
                            command=self.gallery_canvas.xview)
        gsb.grid(row=1, column=0, sticky="ew")
        self.gallery_canvas.configure(xscrollcommand=gsb.set)
        self.gallery.bind(
            "<Configure>",
            lambda _e: self.gallery_canvas.configure(
                scrollregion=self.gallery_canvas.bbox("all")))
        self.gallery_canvas.bind(
            "<MouseWheel>",
            lambda e: self.gallery_canvas.xview_scroll(
                -1 if e.delta > 0 else 1, "units"))
        gbtns = ttk.Frame(gwrap)
        gbtns.grid(row=0, column=1, rowspan=2, sticky="s", padx=(8, 0))
        ttk.Button(gbtns, text="🗑 Clear history",
                   command=self._clear_history).pack(fill="x", pady=(0, 3))
        ttk.Button(gbtns, text="❌ Delete art files…",
                   command=self._delete_history_files).pack(fill="x")

    # -------------------------------------------------- persistence
    def _get(self, box):
        return box.get("1.0", END).strip()

    def _set(self, box, text):
        box.delete("1.0", END)
        if text:
            box.insert("1.0", text)

    def _selected_loras(self):
        return [self.lora_list.get(i) for i in self.lora_list.curselection()]

    def _pick_ragmap(self):
        """Load a .ragmap.json (paired with a trained LoRA). At generation
        the app retrieves the closest example images and feeds them as
        IP-Adapter visual guidance alongside the LoRA."""
        path = filedialog.askopenfilename(
            title="Load RAG map",
            filetypes=[("RAG map", "*.ragmap.json"),
                       ("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            rag = load_ragmap(path)
        except Exception as e:
            messagebox.showerror("RAG map", f"Could not read the RAG map: {e}")
            return
        n = len([e for e in rag.get("entries", [])
                 if Path(e.get("_path", "")).exists()])
        if not n:
            messagebox.showwarning(
                "RAG map", "This RAG map has no usable images (the entry "
                "image paths don't resolve next to the map file).")
            return
        self.ragmap = rag
        self.ragmap_path = path
        lora = rag.get("lora", "")
        self.ragmap_var.set(
            f"{rag.get('name', Path(path).stem)} — {n} imgs"
            + (f" · LoRA {lora}" if lora else ""))
        # nudge if the paired LoRA isn't installed / IP-Adapter not ready
        notes = []
        if lora and lora not in list_loras():
            notes.append(f"paired LoRA '{lora}' isn't installed — add it "
                         "with ➕ Add LoRA file")
        if not self._style_support_ok():
            if messagebox.askyesno(
                    "Enable image guidance",
                    "RAG image guidance uses IP-Adapter, which isn't set up "
                    "yet (a ~1 GB one-time download).\n\nInstall it now? "
                    "(Without it, the RAG map still helps by adding its "
                    "captions to your prompt as text.)"):
                threading.Thread(target=self._install_style_support,
                                 daemon=True).start()
        if notes:
            self.status_var.set("RAG map loaded. " + "; ".join(notes))
        else:
            self.status_var.set(
                f"RAG map '{rag.get('name', '')}' loaded — its example "
                "images will guide generations on SDXL models.")
        self._schedule_persist()

    def _clear_ragmap(self):
        self.ragmap = None
        self.ragmap_path = None
        self.ragmap_var.set("none")
        self._schedule_persist()

    def _import_lora(self):
        """Browse for .safetensors LoRA file(s), copy them into
        models\\loras, then refresh the list and tick the new ones."""
        paths = filedialog.askopenfilenames(
            title="Add LoRA file(s)",
            filetypes=[("LoRA (safetensors)", "*.safetensors"),
                       ("All files", "*.*")])
        if not paths:
            return
        dest_dir = _ensure_lora_dir()
        added, skipped = [], []
        for src in paths:
            src = Path(src)
            if src.suffix.lower() != ".safetensors":
                # .ckpt/.pt are pickle-based and can execute code on load
                skipped.append(f"{src.name} (only .safetensors is allowed)")
                continue
            dest = dest_dir / src.name
            if dest.exists() and dest.resolve() == src.resolve():
                added.append(src.name)   # already in the folder
                continue
            if dest.exists():
                if not messagebox.askyesno(
                        "Replace LoRA",
                        f"{src.name} is already in your LoRA folder. "
                        "Replace it?"):
                    skipped.append(f"{src.name} (kept existing)")
                    continue
            try:
                shutil.copy2(src, dest)
                added.append(src.name)
            except OSError as e:
                skipped.append(f"{src.name} ({e})")
        # make the freshly added ones tick on the next list rebuild
        self._pending_loras = getattr(self, "_pending_loras", set()) | set(added)
        self._refresh_models()
        parts = []
        if added:
            parts.append(f"Added {len(added)} LoRA(s): "
                         + ", ".join(added))
        if skipped:
            parts.append("Skipped: " + "; ".join(skipped))
        self.status_var.set("  ·  ".join(parts) or "No LoRAs added.")

    def _collect_ui_state(self):
        return {
            "prompt": self._get(self.prompt_box),
            "negative": self._get(self.negative_box),
            "style": self._get(self.style_box),
            "preset": self.preset_var.get(),
            "model": self._model_raw(),
            "loras": self._selected_loras(),
            "lora_strength": round(self.lora_strength.get(), 2),
            "ref_images": self.ref_paths,
            "editor": self._editor_engine(),
            "editor_canvas": self.editor_canvas_var.get(),
            "border_refs": self.border_ref_paths,
            "change": int(self.change_var.get()),
            "anim_image": self.anim_image_path or "",
            "anim_action": self._get(self.anim_prompt_box),
            "anim_secs": self.anim_secs_var.get(),
            "anim_keep": self.anim_keep_var.get(),
            "anim_loop": self.anim_loop_var.get(),
            "anim_size": self.anim_size_var.get(),
            "anim_transparent": self.anim_transparent_var.get(),
            "anim_motion": self.anim_motion_var.get(),
            "anim_gif": self.anim_gif_var.get(),
            "anim_zip": self.anim_zip_var.get(),
            "anim_sheet": self.anim_sheet_var.get(),
            "anim_video": self.anim_video_var.get(),
            "border_theme": self._get(self.border_prompt_box),
            "border_auto": self.border_auto_var.get(),
            "border_model": self.border_model_var.get(),
            "border_style": self.border_style_var.get(),
            "border_aspect": self.border_aspect_var.get(),
            "border_thick": int(self.border_thick_var.get()),
            "border_clean": self.border_clean_var.get(),
            "border_batch": self.border_batch_var.get(),
            "size": self.size_var.get(),
            "steps": self.steps_var.get(),
            "batch": self.batch_var.get(),
            "transparent": self.transparent_var.get(),
            "upscale": self.upscale_var.get(),
            "ragmap_path": self.ragmap_path,
            "seed": self.seed_var.get(),
            "random_seed": self.random_seed_var.get(),
            "auto_negative": self._auto_negative,
        }

    DEFAULT_LORAS = {"SDXL_BW_Manga.safetensors",
                     "SDXL_GraphicNovel.safetensors",
                     "SDXL_LineArt_Manga.safetensors"}

    def _apply_ui_state(self, st):
        if not st:
            # first run: comic-style starter selection
            self._pending_loras = set(self.DEFAULT_LORAS)
            self._on_preset()
            return
        try:
            self._set(self.prompt_box, st.get("prompt", ""))
            self._set(self.negative_box, st.get("negative", ""))
            self._set(self.style_box, st.get("style", ""))
            self.preset_var.set(st.get("preset", NONE_PRESET))
            if st.get("model"):
                self.model_var.set(st["model"])
            saved_loras = st.get("loras", [])
            # tolerate the old [[name, strength], ...] format
            self._pending_loras = {l[0] if isinstance(l, list) else l
                                   for l in saved_loras}
            self.lora_strength.set(st.get("lora_strength", 0.8))
            refs = st.get("ref_images") or \
                ([st["ref_image"]] if st.get("ref_image") else [])
            self.ref_paths = [p for p in refs if Path(p).exists()]
            if self.ref_paths:
                first = Path(self.ref_paths[0]).name
                self.ref_var.set(first if len(self.ref_paths) == 1 else
                                 f"{len(self.ref_paths)} images ({first}, …)")
            # editor intentionally NOT restored: Flux Kontext is always
            # the default at launch (per-session changes still allowed)
            self.editor_canvas_var.set(st.get("editor_canvas", True))
            brefs = st.get("border_refs", [])
            self.border_ref_paths = [p for p in brefs if Path(p).exists()]
            if self.border_ref_paths:
                first = Path(self.border_ref_paths[0]).name
                self.border_ref_var.set(
                    first if len(self.border_ref_paths) == 1 else
                    f"{len(self.border_ref_paths)} images ({first}, …)")
            self.change_var.set(st.get("change", 60))
            ai = st.get("anim_image", "")
            if ai and Path(ai).exists():
                self.anim_image_path = ai
                self.anim_img_var.set(Path(ai).name)
            self._set(self.anim_prompt_box, st.get("anim_action", ""))
            self.anim_secs_var.set(st.get("anim_secs", 3))
            if st.get("anim_keep") in ANIM_KEEP:
                self.anim_keep_var.set(st["anim_keep"])
            # anim_loop intentionally NOT restored: auto-cut is always
            # the default at launch (per-run changes still allowed)
            if st.get("anim_size") in ANIM_SIZES:
                self.anim_size_var.set(st["anim_size"])
            self.anim_transparent_var.set(st.get("anim_transparent", True))
            if st.get("anim_motion") in ANIM_MOTION:
                self.anim_motion_var.set(st["anim_motion"])
            self.anim_gif_var.set(st.get("anim_gif", True))
            self.anim_zip_var.set(st.get("anim_zip", True))
            self.anim_sheet_var.set(st.get("anim_sheet", False))
            if st.get("anim_video") in ("none", "MP4", "WebM"):
                self.anim_video_var.set(st["anim_video"])
            self._set(self.border_prompt_box, st.get("border_theme", ""))
            self.border_auto_var.set(st.get("border_auto", True))
            if st.get("border_model"):
                self.border_model_var.set(st["border_model"])
            if st.get("border_style"):
                self.border_style_var.set(st["border_style"])
            if st.get("border_aspect") in BORDER_SIZES:
                self.border_aspect_var.set(st["border_aspect"])
            self.border_thick_var.set(st.get("border_thick", 14))
            self.border_clean_var.set(st.get("border_clean", True))
            self.border_batch_var.set(st.get("border_batch", 1))
            if st.get("size") in SIZE_PRESETS:
                self.size_var.set(st["size"])
            self.steps_var.set(st.get("steps", "auto"))
            self.batch_var.set(st.get("batch", 1))
            self.transparent_var.set(st.get("transparent", False))
            self.upscale_var.set(st.get("upscale", False))
            rmp = st.get("ragmap_path")
            if rmp and Path(rmp).exists():
                try:
                    self.ragmap = load_ragmap(rmp)
                    self.ragmap_path = rmp
                    nm = self.ragmap.get("name", Path(rmp).stem)
                    self.ragmap_var.set(f"{nm} (loaded)")
                except Exception:
                    self.ragmap = None
            self.seed_var.set(st.get("seed", "0"))
            self.random_seed_var.set(st.get("random_seed", True))
            self._auto_negative = st.get("auto_negative")
        except Exception:
            pass  # a broken settings file must never block startup

    def _persist(self):
        self.settings["ui"] = self._collect_ui_state()
        self._save_settings()

    def _wire_autosave(self):
        """Persist every change as it happens (debounced), so settings
        survive any kind of exit — including a killed process."""
        for var in (self.model_var, self.preset_var, self.size_var,
                    self.steps_var, self.seed_var, self.batch_var,
                    self.transparent_var, self.upscale_var,
                    self.random_seed_var,
                    self.lora_strength, self.change_var, self.editor_var,
                    self.editor_canvas_var,
                    self.border_auto_var, self.border_aspect_var,
                    self.border_model_var, self.border_style_var,
                    self.border_thick_var, self.border_clean_var,
                    self.border_batch_var, self.anim_secs_var,
                    self.anim_keep_var, self.anim_loop_var,
                    self.anim_size_var, self.anim_transparent_var,
                    self.anim_motion_var, self.anim_gif_var,
                    self.anim_zip_var, self.anim_sheet_var,
                    self.anim_video_var):
            var.trace_add("write", self._schedule_persist)
        for box in (self.prompt_box, self.negative_box, self.style_box,
                    self.border_prompt_box, self.anim_prompt_box):
            box.bind("<KeyRelease>", self._schedule_persist)
            box.bind("<FocusOut>", self._schedule_persist)

    def _schedule_persist(self, *_a):
        if self._persist_job is not None:
            self.root.after_cancel(self._persist_job)
        self._persist_job = self.root.after(700, self._persist_now)

    def _persist_now(self):
        self._persist_job = None
        try:
            self._persist()
        except Exception:
            pass

    def _on_close(self):
        try:
            self._persist()
        finally:
            self.root.destroy()

    def _clear_all(self):
        self._set(self.prompt_box, "")
        self._set(self.negative_box, "")
        self._set(self.style_box, "")
        self.preset_var.set(NONE_PRESET)
        self._auto_negative = None
        self._persist()

    # -------------------------------------------------- model updates
    def _check_updates_bg(self):
        # first, apply any update downloaded last session while its file
        # was locked by the engine
        try:
            manifest = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            manifest = []
        for entry in manifest:
            dest = MODELS / entry["dir"] / entry["local"]
            tmp = dest.with_suffix(".tmp")
            if tmp.exists():
                try:
                    os.replace(tmp, dest)
                except OSError:
                    pass
        # app self-update (frozen exe only) — offer before model/engine
        if getattr(sys, "frozen", False):
            app_up = check_app_update()
            if app_up:
                self.ui_queue.put(("app_update", app_up))
        ups = check_model_updates()
        eng = check_engine_update()
        if ups or eng:
            self.ui_queue.put(("updates", ups, eng))

    def _do_app_update(self, info):
        try:
            newexe = apply_app_update(
                info["zip_url"],
                lambda s: self.ui_queue.put(("status", s)))
            self.ui_queue.put(("status", f"Updated to {info['tag']}. "
                                         "Restarting…"))
            subprocess.Popen([str(newexe)], cwd=str(PROJECT))
            self.root.after(800, self.root.destroy)
        except Exception as e:
            self.ui_queue.put(("error", f"App update failed: {e}. You can "
                                        "download the latest release "
                                        "manually from GitHub."))

    def _download_updates(self, ups, eng=None):
        ok, locked = 0, 0
        for u in ups:
            try:
                if download_model_update(
                        u, lambda s: self.ui_queue.put(("status", s)),
                        lambda d, t: self.ui_queue.put(("progress", d, t))):
                    ok += 1
                else:
                    locked += 1
            except Exception as e:
                self.ui_queue.put(("status",
                                   f"Update failed for {u['local']}: {e}"))
        if eng:
            try:
                self.ui_queue.put(("status", "Stopping engine for update…"))
                kill_engine()
                update_engine(eng["sha"],
                              lambda s: self.ui_queue.put(("status", s)))
                self.ui_queue.put(("status", "Engine updated — restarting…"))
                threading.Thread(target=self._boot_engine,
                                 daemon=True).start()
            except Exception as e:
                self.ui_queue.put(("status", f"Engine update failed: {e}"))
        note = f"Updates done: {ok} model(s) installed"
        if locked:
            note += f", {locked} will apply on next start (file in use)"
        if eng:
            note += ", engine updated"
        self.ui_queue.put(("status", note))
        self.ui_queue.put(("models_changed", None))

    # -------------------------------------------------- engine boot
    def _restart_engine(self):
        self.ui_queue.put(("status", "Restarting engine…"))
        kill_engine()
        time.sleep(2)
        self._boot_engine()

    def _boot_engine(self):
        if engine_alive() and not engine_is_ours():
            # a foreign engine (another instance / leftover dev run) holds
            # the port — using it can send results to the wrong window
            self.ui_queue.put(("status", "Note: an engine was already "
                                         "running on port 8188 (another "
                                         "instance or a previous session). "
                                         "Using it — close extra windows if "
                                         "results seem to go missing."))
        if not engine_alive():
            self.ui_queue.put(("status", "Starting local engine (first start "
                                         "takes a minute)…"))
            try:
                start_engine()
            except Exception as e:
                self.ui_queue.put(("error", f"Could not start engine: {e}"))
                return
            for _ in range(180):
                if engine_alive():
                    break
                time.sleep(2)
            else:
                self.ui_queue.put(("error", "Engine did not come up — see "
                                            "engine.log in the project folder."))
                return
        # a stale engine from an old session may have been started with
        # wrong model paths: if it can't see models that exist on disk,
        # restart it with our config (start_engine rewrites the yaml)
        if not getattr(self, "_engine_heal_tried", False):
            try:
                disk = set(scan_models("checkpoints"))
                known = set(_api_choices("CheckpointLoaderSimple",
                                         "ckpt_name"))
                if disk and disk - known:
                    self._engine_heal_tried = True
                    self.ui_queue.put(("status",
                                       "Engine can't see the model folder "
                                       "— restarting it with correct "
                                       "paths…"))
                    kill_engine()
                    time.sleep(2)
                    start_engine()
                    for _ in range(180):
                        if engine_alive():
                            break
                        time.sleep(2)
            except Exception:
                pass
        try:
            stats = api_get("/system_stats")
            vram = stats["devices"][0]["vram_total"] / (1024**3)
            self.ui_queue.put(("vram", vram))
        except Exception:
            pass
        self.ui_queue.put(("engine_ready", None))

    # -------------------------------------------------- ui handlers
    def _on_preset(self, _e=None):
        p = self._preset()
        if not p:
            self._set(self.style_box, "")
            return
        self._set(self.style_box, p["style"])
        # replace the negative only if the user hasn't customized it:
        # empty box, or box still holding a previous preset's negative
        current = self._get(self.negative_box)
        if not current or current == self._auto_negative:
            self._set(self.negative_box, p["negative"])
            self._auto_negative = p["negative"]

    def _preset(self):
        name = self.preset_var.get()
        return next((p for p in self.presets if p["name"] == name), None)

    def _refresh_preset_list(self):
        """List only presets that suit the selected model's family — but
        never clear the user's current selection: it stays even if the
        model changed; only the dropdown's option list adapts."""
        fam = model_family(self._model_raw() or "")
        want = "anime" if fam == "anime" else \
            ("flux" if fam in ("flux", "schnell") else "sdxl")
        names = [NONE_PRESET] + [p["name"] for p in self.presets
                                 if p.get("model_hint", "sdxl") == want]
        self.preset_dd["values"] = names

    def _editor_engine(self):
        return self._editor_display.get(self.editor_var.get(), "kontext")

    def _best_sdxl_model(self):
        """Best installed SDXL-family checkpoint for the border LoRA —
        Juggernaut preferred, then any non-Flux model that fits."""
        ck = list_checkpoints()
        pref = next((c for c in ck if "juggernaut" in c.lower()), None)
        if pref and self._model_fits.get(pref, "ok") != "block":
            return pref
        return next((c for c in ck
                     if model_family(c) not in ("flux", "schnell")
                     and self._model_fits.get(c, "ok") != "block"), None)

    def _border_model_raw(self):
        disp = self.border_model_var.get()
        display = getattr(self, "_model_display", {})
        if disp and disp != BORDER_SAME_MODEL and disp in display:
            return display[disp]
        return self._model_raw()

    def _border_style(self):
        return next((p for p in self.presets
                     if p["name"] == self.border_style_var.get()), None)

    def _refresh_border_styles(self):
        """Style options follow the border's model family — the current
        selection is never cleared, only the option list adapts."""
        fam = model_family(self._border_model_raw() or "")
        want = "anime" if fam == "anime" else \
            ("flux" if fam in ("flux", "schnell") else "sdxl")
        self.border_style_dd["values"] = [NONE_PRESET] + [
            p["name"] for p in self.presets
            if p.get("model_hint", "sdxl") == want]

    def _editor_tier(self, engine):
        """ok = comfortable, warn = loads but slow, block = too big."""
        if self.vram_gb is None:
            return "ok"
        req = EDITOR_VRAM.get(engine, 16)
        if req <= self.vram_gb:
            return "ok"
        if req - 4 <= self.vram_gb:
            return "warn"
        return "block"

    def _editor_fits(self, engine):
        return self._editor_tier(engine) != "block"

    def _refresh_editor_list(self):
        """Editor entries show their VRAM requirement; tight fits show
        red, ones beyond the card are greyed and blocked."""
        prev = self._editor_engine() if self._editor_display else "kontext"
        self._editor_display = {}
        for label, engine in EDITOR_ENGINES.items():
            req = EDITOR_VRAM.get(engine, 16)
            disp = f"{label} — needs ~{req} GB VRAM"
            tier = self._editor_tier(engine)
            if tier == "warn":
                disp += "  — tight fit: slow"
            elif tier == "block":
                disp += "  — exceeds GPU memory"
            self._editor_display[disp] = engine
        self.editor_dd["values"] = list(self._editor_display)
        self.editor_dd.configure(postcommand=self._color_editor_dropdown)
        target = prev if self._editor_fits(prev) else next(
            (e for e in EDITOR_ENGINES.values() if self._editor_fits(e)),
            prev)
        for disp, engine in self._editor_display.items():
            if engine == target:
                self.editor_var.set(disp)
                break

    def _color_editor_dropdown(self):
        try:
            pd = self.root.tk.call("ttk::combobox::PopdownWindow",
                                   self.editor_dd)
            lb = f"{pd}.f.l"
            for i, disp in enumerate(self.editor_dd["values"]):
                tier = self._editor_tier(
                    self._editor_display.get(disp, "kontext"))
                self.root.tk.call(lb, "itemconfigure", i, "-foreground",
                                  self.TIER_COLORS.get(tier, ACCENT2))
        except Exception:
            pass

    def _on_editor_pick(self):
        engine = self._editor_engine()
        tier = self._editor_tier(engine)
        if tier == "block":
            messagebox.showwarning(
                "Not enough GPU memory",
                f"This editor needs ~{EDITOR_VRAM.get(engine, 16)} GB of "
                f"VRAM — your card has {self.vram_gb:.1f} GB. Pick a "
                "green editor instead.")
            for disp, e in self._editor_display.items():
                if self._editor_fits(e):
                    self.editor_var.set(disp)
                    break
        elif tier == "warn" and engine not in self._warned_vram:
            self._warned_vram.add(engine)
            messagebox.showwarning(
                "Tight VRAM fit",
                f"This editor needs ~{EDITOR_VRAM.get(engine, 16)} GB — "
                f"your card has {self.vram_gb:.1f} GB. It will load and "
                "run, but expect noticeably slower edits while memory "
                "is paged.")

    def _vram_block_msg(self, raw):
        gb = self._size_gb(raw)
        messagebox.showwarning(
            "Not enough GPU memory",
            f"{raw} is {gb:.1f} GB and needs about "
            f"{gb + VRAM_HEADROOM_GB:.0f} GB of VRAM — your card has "
            f"{self.vram_gb:.1f} GB. Pick a green model instead.")

    def _on_model_pick(self):
        raw = self._model_raw()
        tier = self._model_fits.get(raw, "ok")
        if tier == "block":
            self._vram_block_msg(raw)
            if self._last_fit_display:
                self.model_var.set(self._last_fit_display)
        else:
            if tier == "warn" and raw not in self._warned_vram:
                self._warned_vram.add(raw)
                messagebox.showwarning(
                    "Tight VRAM fit",
                    f"{raw} will load and run, but it nearly fills your "
                    f"card ({self.vram_gb:.1f} GB) — expect noticeably "
                    "slower generation while memory is paged.")
            self._last_fit_display = self.model_var.get()
        self._update_model_entry_style()
        self._refresh_preset_list()

    def _use_example(self):
        p = self._preset()
        if not p:
            messagebox.showinfo("Pick a preset",
                                "Choose an art style preset first.")
            return
        self._set(self.prompt_box, p["example"])
        self._on_preset()

    def _model_raw(self):
        """Raw checkpoint filename behind the decorated dropdown text."""
        return self._model_display.get(self.model_var.get(),
                                       self.model_var.get())

    @staticmethod
    def _size_gb(name):
        try:
            return (MODELS / "checkpoints" / name).stat().st_size / (1024**3)
        except OSError:
            return 0

    TIER_COLORS = {"ok": ACCENT2, "warn": "#e74c3c", "block": "#5a5a6a"}
    TIER_STYLES = {"ok": "Fit.TCombobox", "warn": "Warn.TCombobox",
                   "block": "NoFit.TCombobox"}

    def _model_tier_of(self, gb):
        """ok = comfortable, warn = loads but slow (tight fit),
        block = exceeds the card."""
        if self.vram_gb is None or not gb:
            return "ok"
        if gb + VRAM_HEADROOM_GB <= self.vram_gb:
            return "ok"
        if gb <= self.vram_gb:
            return "warn"
        return "block"

    def _color_model_dropdown(self, dd):
        """Green = fits, red = tight fit (slow), grey = too big."""
        try:
            pd = self.root.tk.call("ttk::combobox::PopdownWindow", dd)
            lb = f"{pd}.f.l"
            for i, disp in enumerate(dd["values"]):
                raw = self._model_display.get(disp, disp)
                tier = self._model_fits.get(raw, "ok")
                self.root.tk.call(lb, "itemconfigure", i, "-foreground",
                                  self.TIER_COLORS.get(tier, ACCENT2))
        except Exception:
            pass

    def _update_model_entry_style(self):
        tier = self._model_fits.get(self._model_raw(), "ok")
        self.model_dd.configure(style=self.TIER_STYLES.get(
            tier, "Fit.TCombobox"))

    def _refresh_models(self):
        ckpts = list_checkpoints()
        self._model_display = {}
        self._model_fits = {}
        for name in ckpts:
            gb = self._size_gb(name)
            tier = self._model_tier_of(gb)
            self._model_fits[name] = tier
            disp = f"{name}  ·  {gb:.1f} GB" if gb else name
            if tier == "warn":
                disp += "  — tight fit: slow"
            elif tier == "block":
                disp += "  — exceeds GPU memory"
            self._model_display[disp] = name
        self.model_dd["values"] = list(self._model_display)
        self.model_dd.configure(
            postcommand=lambda: self._color_model_dropdown(self.model_dd))
        if not ckpts:
            self.status_var.set(
                "No models installed — run Setup.exe (or drop .safetensors "
                "into models\\checkpoints), then hit ↻.")
        if self.vram_gb is None:
            self.vram_note.set(
                "⚠ No NVIDIA GPU detected — this app requires an NVIDIA "
                "card with a current driver; generation will not work.")
        else:
            n_fit = sum(1 for t in self._model_fits.values() if t == "ok")
            self.vram_note.set(
                f"Your GPU: {self.vram_gb:.1f} GB VRAM — green fits, red "
                f"runs but slow, grey too big ({n_fit}/{len(ckpts)} "
                "comfortable).")

        cur = self._model_raw()
        if cur not in ckpts:
            last = self.settings.get("last_model")
            cur = last if last in ckpts else ""
            if not cur or self._model_fits.get(cur, "ok") == "block":
                cur = next((c for c in ckpts
                            if self._model_fits.get(c) == "ok"),
                           ckpts[0] if ckpts else "")
        for disp, raw in self._model_display.items():
            if raw == cur:
                self.model_var.set(disp)
                if self._model_fits.get(raw, "ok") != "block":
                    self._last_fit_display = disp
                break
        self._update_model_entry_style()
        self._refresh_preset_list()
        self.border_model_dd["values"] = [BORDER_SAME_MODEL] + \
            list(self._model_display)
        self._refresh_border_styles()

        loras = _visible_loras(list_loras())
        keep = set(self._selected_loras()) | getattr(self, "_pending_loras",
                                                     set())
        self._pending_loras = set()
        self.lora_list.delete(0, END)
        for idx, name in enumerate(loras):
            self.lora_list.insert(END, name)
            if name in keep:
                self.lora_list.selection_set(idx)

    def _pick_ref(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
            ("All files", "*.*")])
        if paths:
            self.ref_paths = list(paths)
            first = Path(self.ref_paths[0]).name
            self.ref_var.set(first if len(self.ref_paths) == 1 else
                             f"{len(self.ref_paths)} images ({first}, …)")
            self.status_var.set("Reference(s) loaded — 'Change amount' sets "
                                "how strongly the AI transforms them.")
            self._schedule_persist()

    def _clear_ref(self):
        self.ref_paths = []
        self.ref_var.set("none — text only")
        self._schedule_persist()

    def _pick_border_refs(self):
        paths = filedialog.askopenfilenames(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp;*.bmp"),
            ("All files", "*.*")])
        if paths:
            self.border_ref_paths = list(paths)
            first = Path(self.border_ref_paths[0]).name
            self.border_ref_var.set(
                first if len(self.border_ref_paths) == 1 else
                f"{len(self.border_ref_paths)} images ({first}, …)")
            self._schedule_persist()

    def _clear_border_refs(self):
        self.border_ref_paths = []
        self.border_ref_var.set("none")
        self._schedule_persist()

    # -------------------------------------------------- batch queue
    def _enqueue(self, kind, label, payload):
        self.job_queue.append({"kind": kind, "label": label,
                               "payload": payload})
        self._refresh_queue()
        self.status_var.set(f"Queued: {label}  ({len(self.job_queue)} in "
                            "batch). Add more, then Run all.")

    def _refresh_queue(self):
        if not hasattr(self, "queue_list"):
            return
        self.queue_list.delete(0, END)
        for j in self.job_queue:
            self.queue_list.insert(END, f"[{j['kind']}] {j['label']}")
        self.queue_count_var.set(f"Batch queue ({len(self.job_queue)})")

    def _remove_queued(self):
        sel = list(self.queue_list.curselection())
        for i in reversed(sel):
            if 0 <= i < len(self.job_queue):
                del self.job_queue[i]
        self._refresh_queue()

    def _clear_queue(self):
        self.job_queue = []
        self._refresh_queue()

    def _run_queue(self):
        if getattr(self, "_batch_active", False):
            return
        if not self.job_queue:
            self.status_var.set("Batch queue is empty — use the ＋Q buttons "
                                "to add jobs.")
            return
        if self.busy and self._busy_guard():
            return
        self._batch_active = True
        self.busy = True
        self.go_btn.state(["disabled"])
        threading.Thread(target=self._queue_worker, daemon=True).start()

    def _queue_worker(self):
        jobs = list(self.job_queue)
        total = len(jobs)
        try:
            for i, j in enumerate(jobs, 1):
                self.ui_queue.put(("status", f"Batch {i}/{total}: "
                                             f"{j['label']}…"))
                try:
                    if j["kind"] == "gen":
                        Generator(self.ui_queue).run(j["payload"])
                    elif j["kind"] == "border":
                        Generator(ChannelQueue(self.ui_queue,
                                               "border_progress")
                                  ).run(j["payload"])
                    elif j["kind"] == "anim":
                        self._run_animation(j["payload"])
                except Exception as e:
                    self.ui_queue.put(("status",
                                       f"Batch job {i} failed: {e}"))
            self.ui_queue.put(("batch_done", total))
        finally:
            self._batch_active = False

    def _busy_guard(self):
        """True = abort the new request. Offers to cancel the running job
        so a stuck/slow generation can't lock the app."""
        if getattr(self, "_batch_active", False):
            messagebox.showinfo("Batch running",
                                "A batch queue is running. Wait for it to "
                                "finish, or clear the queue first.")
            return True
        if not self.busy:
            return False
        if messagebox.askyesno(
                "Generation in progress",
                "A generation is still running (big models can take many "
                "minutes — watch the progress bar).\n\nCancel it now?"):
            try:
                requests.post(f"{ENGINE_URL}/interrupt", timeout=5)
            except requests.RequestException:
                pass
            self.busy = False
            self.go_btn.state(["!disabled"])
            self.progress["value"] = 0
            self.pct_var.set("")
            self.status_var.set("Cancelled.")
        return True

    def _cancel_generation(self):
        """The ✕ Cancel next to each progress bar — stops the running
        job (image, border or animation) and frees the buttons."""
        if not self.busy:
            return
        try:
            requests.post(f"{ENGINE_URL}/interrupt", timeout=5)
        except requests.RequestException:
            pass
        self.busy = False
        self.go_btn.state(["!disabled"])
        for bar, var in ((self.progress, self.pct_var),
                         (self.anim_progress, self.anim_pct_var),
                         (self.border_progress, self.border_pct_var)):
            bar["value"] = 0
            var.set("")
        self.status_var.set("Cancelled.")

    def _use_selected_for_editor(self):
        if self.current is None or not self.session:
            return
        _img, _params, path = self.session[self.current]
        if str(path).lower().endswith(".gif"):
            self.status_var.set("Pick a still image for the editor — GIFs "
                                "can't be edited directly.")
            return
        self.ref_paths = [str(path)]
        self.ref_var.set("selection")
        self.status_var.set(f"Editor now works on the selected image "
                            f"({Path(path).name}) — the prompt is the "
                            "instruction.")
        self._schedule_persist()

    def _update_editor_btn(self):
        if self.session:
            self.editor_use_btn.state(["!disabled"])
        else:
            self.editor_use_btn.state(["disabled"])

    def _reuse_seed(self):
        if self.session:
            _, params, _ = self.session[self.current]
            self.seed_var.set(str(params["seed"]))
            self.random_seed_var.set(False)

    # -------------------------------------------------- generation
    def _generate(self, queue=False):
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        prompt = self._get(self.prompt_box)
        if not prompt:
            messagebox.showinfo("Prompt", "Write a prompt first — or pick a "
                                          "preset and hit 'Try example'.")
            return
        style = self._get(self.style_box)
        # a reference is a full override: the image supplies the art
        # style, so the preset's style text (and LoRAs) are not applied
        if self.ref_paths:
            full_prompt = prompt
        else:
            full_prompt = f"{prompt}, {style}" if style else prompt
        editing = bool(self.ref_paths)
        editor = self._editor_engine()
        model = self._model_raw()
        if editing:
            if not self._ensure_editor_ready(editor):
                return
            model = f"editor:{editor}"
        else:
            if not model:
                messagebox.showerror("Model", "No model selected — hit ↻ "
                                              "or wait for downloads to "
                                              "finish.")
                return
            tier = self._model_fits.get(model, "ok")
            if tier == "block":
                self._vram_block_msg(model)
                return
            if tier == "warn":
                self.status_var.set("Running close to the VRAM limit — "
                                    "generation will be slower.")
            # the engine must actually know this model, or generation
            # 400s — a stale engine (old model paths) needs a restart
            known = _api_choices("CheckpointLoaderSimple", "ckpt_name")
            if known and model not in known and \
                    (MODELS / "checkpoints" / model).exists():
                if messagebox.askyesno(
                        "Engine restart needed",
                        "The engine was started with old settings and "
                        "can't see this model yet.\n\nRestart the engine "
                        "now? (takes ~a minute, then hit Generate again)"):
                    threading.Thread(target=self._restart_engine,
                                     daemon=True).start()
                return

        strength = round(self.lora_strength.get(), 2)
        loras = [] if self.ref_paths else \
            [(name, strength) for name in self._selected_loras()]

        # RAG map: retrieve the closest example images for this prompt and
        # feed them as IP-Adapter guidance (SDXL only); auto-apply the
        # paired LoRA + trigger. Falls back to caption text if IP-Adapter
        # isn't installed. Only in plain generation (not editing).
        rag_refs = []
        rag_weight = 0.8
        if self.ragmap and not editing:
            if model_family(model) in ("flux", "schnell"):
                self.status_var.set("RAG image guidance needs an SDXL model "
                                    "(Juggernaut/DreamShaper) — skipped for "
                                    "this Flux model.")
            else:
                hits = ragmap_retrieve(self.ragmap, prompt)
                rag_weight = float(self.ragmap.get("weight", 0.8))
                lf = self.ragmap.get("lora", "")
                if lf and lf in list_loras() and \
                        lf not in [n for n, _s in loras]:
                    loras.append((lf, strength))
                trg = self.ragmap.get("trigger", "")
                if trg and trg.lower() not in full_prompt.lower():
                    full_prompt = f"{trg}, {full_prompt}"
                if self._style_support_ok():
                    rag_refs = [h["_path"] for h in hits]
                else:
                    caps = "; ".join(h.get("caption", "") for h in hits
                                     if h.get("caption"))
                    if caps:
                        full_prompt = f"{full_prompt}, {caps}"
                    self.status_var.set("RAG map applied as text (install "
                                        "IP-Adapter via 🧭 RAG map… for image "
                                        "guidance).")

        w, h = SIZE_PRESETS[self.size_var.get()]
        try:
            seed = int(self.seed_var.get())
        except ValueError:
            seed = 0
        if self.random_seed_var.get():
            seed = random.randrange(2**32)
            self.seed_var.set(str(seed))
        steps = None if self.steps_var.get() == "auto" else int(self.steps_var.get())

        params = dict(prompt=full_prompt, user_prompt=prompt, style=style,
                      negative=self._get(self.negative_box),
                      model=model, loras=loras, width=w, height=h, seed=seed,
                      steps=steps, cfg=None, batch=self.batch_var.get(),
                      random_seed=self.random_seed_var.get(),
                      transparent=self.transparent_var.get(),
                      upscale=self.upscale_var.get(),
                      preset=self.preset_var.get(),
                      ref_images=list(self.ref_paths),
                      rag_ref_paths=rag_refs, style_weight=rag_weight,
                      editor=editor,
                      out_size=(w, h) if editing
                      and self.editor_canvas_var.get() else None,
                      denoise=round(self.change_var.get() / 100, 2))
        if editing:
            self.status_var.set("Editing with "
                                f"{'Flux Kontext' if editor == 'kontext' else 'Qwen Image Edit'} "
                                "— the prompt is the instruction; preset "
                                "and LoRAs are ignored.")
        else:
            self.settings["last_model"] = model
        self._persist()
        if queue:
            self._enqueue("gen", (prompt or "art")[:38], params)
            return
        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress["value"] = 0
        gen = Generator(self.ui_queue)
        threading.Thread(target=gen.run, args=(params,), daemon=True).start()

    def _poll_queue(self):
        # the reschedule in `finally` must survive anything a handler
        # throws, otherwise one bad message would freeze the whole UI
        try:
            while True:
                msg = self.ui_queue.get_nowait()
                try:
                    self._handle_msg(msg)
                except Exception as e:
                    try:
                        self.status_var.set(f"UI error: {e}")
                    except Exception:
                        pass
        except queue_mod.Empty:
            pass
        finally:
            self.root.after(100, self._poll_queue)

    def _handle_msg(self, msg):
                kind = msg[0]
                if kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "progress":
                    _, val, mx = msg
                    self.progress["maximum"] = mx
                    self.progress["value"] = val
                    self.pct_var.set(f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "anim_progress":
                    _, val, mx = msg
                    self.anim_progress["maximum"] = mx
                    self.anim_progress["value"] = val
                    self.anim_pct_var.set(
                        f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "border_progress":
                    _, val, mx = msg
                    self.border_progress["maximum"] = mx
                    self.border_progress["value"] = val
                    self.border_pct_var.set(
                        f"{val * 100 / max(1, mx):.0f}%")
                elif kind == "image":
                    _, img, params = msg
                    threading.Thread(target=self._finish_image,
                                     args=(img, params), daemon=True).start()
                elif kind == "finished_image":
                    _, img, params, path = msg
                    self.session.append((img, params, path))
                    self.current = len(self.session) - 1
                    self._show_current()
                    self._add_thumb(self.current)
                    self._update_editor_btn()
                    self.status_var.set(f"Saved  {path.name}")
                elif kind == "done":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    for bar, var in ((self.progress, self.pct_var),
                                     (self.anim_progress,
                                      self.anim_pct_var),
                                     (self.border_progress,
                                      self.border_pct_var)):
                        bar["value"] = 0
                        var.set("")
                elif kind == "engine_ready":
                    self.status_var.set("Engine ready.")
                    self._refresh_models()
                elif kind == "vram":
                    self.vram_gb = msg[1]
                    self._refresh_models()   # recolor with engine's number
                    self._refresh_editor_list()
                elif kind == "vram_live":
                    _, used, total = msg
                    self.vram_bar["maximum"] = total
                    self.vram_bar["value"] = used
                    self.vram_label_var.set(
                        f"GPU {used:,} / {total:,} MB")
                elif kind == "models_changed":
                    self._refresh_models()
                elif kind == "app_update":
                    info = msg[1]
                    if messagebox.askyesno(
                            "App update available",
                            f"A newer version of Comic Book Art Creator "
                            f"({info['tag']}) is available "
                            f"(you have v{APP_VERSION}).\n\n"
                            "Download and install it now? The app will "
                            "restart when done."):
                        threading.Thread(target=self._do_app_update,
                                         args=(info,), daemon=True).start()
                elif kind == "updates":
                    ups = msg[1]
                    eng = msg[2] if len(msg) > 2 else None
                    lines = [
                        f"  •  {u['local']}   ({u['size'] / (1024**3):.1f} GB)"
                        + ("   — not installed" if u.get("missing")
                           else "   — new release")
                        for u in ups]
                    if eng:
                        lines.append("  •  ComfyUI engine — new version "
                                     "available")
                    total = sum(u["size"] for u in ups) / (1024**3)
                    if messagebox.askyesno(
                            "Updates available",
                            "Newer versions were found for:\n\n"
                            + "\n".join(lines)
                            + f"\n\nModel download: {total:.1f} GB. "
                            "Update now?\n(Updates install in the "
                            "background; the engine restarts briefly if "
                            "it is updated.)"):
                        threading.Thread(target=self._download_updates,
                                         args=(ups, eng),
                                         daemon=True).start()
                elif kind == "batch_done":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    self.job_queue = []
                    self._refresh_queue()
                    self.status_var.set(f"Batch complete — {msg[1]} job(s) "
                                        "finished.")
                elif kind == "error":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    self.status_var.set(f"Error: {msg[1]}")

    def _finish_image(self, img, params):
        """Post-process (transparency) + auto-save. Runs off the UI thread."""
        try:
            if params.get("border_cut"):
                img = cut_center(img, params["border_cut"])
            elif params["transparent"]:
                self.ui_queue.put(("status", "Removing background…"))
                img = remove_background(img)
            path = self._autosave(img, params)
            self.ui_queue.put(("finished_image", img, params, path))
        except Exception as e:
            self.ui_queue.put(("error", f"post-process: {e}"))

    def _autosave(self, img, params):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        prefix = "border_" if params.get("border_cut") else ""
        name = f"{prefix}{stamp}_seed{params['seed']}.png"
        path = OUTPUT / name
        meta = PngInfo()
        record = {k: params[k] for k in ("user_prompt", "style", "negative",
                                         "model", "loras", "width", "height",
                                         "seed", "steps", "preset",
                                         "transparent")}
        if params.get("ref_images"):
            record["ref_images"] = [Path(p).name
                                    for p in params["ref_images"]]
            record["change"] = params.get("denoise")
        meta.add_text("comic_art_creator", json.dumps(record))
        meta.add_text("parameters", params["prompt"])
        img.save(path, pnginfo=meta)
        return path

    # -------------------------------------------------- preview + gallery
    def _draw_frame(self, img):
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        scale = min(cw / img.width, ch / img.height, 1.0)
        disp = img.resize((int(img.width * scale), int(img.height * scale)),
                          Image.LANCZOS)
        self.canvas.delete("all")
        self._tk_img = ImageTk.PhotoImage(disp)
        self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img)

    def _stop_gif(self):
        if getattr(self, "_gif_job", None):
            self.root.after_cancel(self._gif_job)
            self._gif_job = None
        self._gif_frames = []

    def _play_gif(self, path):
        try:
            from PIL import ImageSequence
            im = Image.open(path)
            frames, durs = [], []
            for fr in ImageSequence.Iterator(im):
                frames.append(fr.convert("RGBA"))
                durs.append(max(20, int(fr.info.get("duration", 80))))
        except Exception:
            return False
        if not frames:
            return False
        self._gif_frames, self._gif_durs, self._gif_idx = frames, durs, 0
        self._gif_tick()
        return True

    def _gif_tick(self):
        frames = getattr(self, "_gif_frames", [])
        if not frames:
            return
        i = self._gif_idx % len(frames)
        self._draw_frame(frames[i])
        self._gif_idx += 1
        self._gif_job = self.root.after(self._gif_durs[i], self._gif_tick)

    def _show_current(self):
        self._stop_gif()
        self.canvas.delete("all")
        if self.current is None or not self.session:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="Your art appears here", fill=FG_DIM,
                font=("Segoe UI", 16))
            return
        img, params, path = self.session[self.current]
        if str(path).lower().endswith(".gif") and Path(path).exists() \
                and self._play_gif(path):
            self.info_var.set(f"{params['model']}  ·  animated GIF  ·  "
                              f"seed {params['seed']}")
            return
        self._draw_frame(img)
        self.info_var.set(f"{params['model'].split('.')[0]}  ·  "
                          f"{img.width}×{img.height}  ·  seed {params['seed']}")

    def _add_thumb(self, idx):
        img, _p, _path = self.session[idx]
        th = img.copy()
        th.thumbnail((84, 84))
        tk_th = ImageTk.PhotoImage(th)
        btn = ttk.Button(self.gallery, image=tk_th,
                         command=lambda i=idx: self._select(i))
        btn.image = tk_th
        btn.pack(side="left", padx=2)
        # double-click sends the image straight to the Animator
        btn.bind("<Double-Button-1>",
                 lambda _e, i=idx: self._thumb_to_animator(i))
        # keep the newest thumbnail in view
        self.gallery.update_idletasks()
        self.gallery_canvas.xview_moveto(1.0)

    def _rebuild_gallery(self):
        for child in self.gallery.winfo_children():
            child.destroy()
        for idx in range(len(self.session)):
            self._add_thumb(idx)
        if not self.session:
            self.gallery_canvas.xview_moveto(0.0)

    def _delete_current(self):
        """Delete the selected image from disk and the history strip —
        keep only the results worth keeping."""
        if self.current is None or not self.session:
            self.status_var.set("Select an image in the gallery first.")
            return
        _img, _params, path = self.session[self.current]
        if not messagebox.askyesno(
                "Delete image",
                f"Permanently delete {Path(path).name} from disk?"):
            return
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            self.status_var.set(f"Could not delete {Path(path).name}: {e}")
            return
        del self.session[self.current]
        self.current = len(self.session) - 1 if self.session else None
        self._rebuild_gallery()
        self._show_current()
        self._update_editor_btn()
        self.status_var.set(f"Deleted {Path(path).name} — "
                            f"{len(self.session)} image(s) left in history.")

    def _clear_history(self):
        """Empty the session gallery. Files already saved in output\\ are
        untouched — this only clears the in-app history strip."""
        if not self.session:
            return
        self.session = []
        self.current = None
        for child in self.gallery.winfo_children():
            child.destroy()
        self.gallery_canvas.xview_moveto(0.0)
        self._show_current()
        self._update_editor_btn()
        self.status_var.set("History cleared — saved images remain in the "
                            "output folder.")

    def _delete_history_files(self):
        """Permanently delete all generated art from disk, after an
        explicit red-text confirmation."""
        files = [f for f in OUTPUT.glob("*.png")] \
            + [f for f in RAW_OUT.glob("*.png")]
        if not files:
            self.status_var.set("No generated images to delete.")
            return
        mb = sum(f.stat().st_size for f in files) / (1 << 20)

        dlg = Toplevel(self.root)
        dlg.title("Delete generated art")
        dlg.configure(bg=BG)
        dlg.resizable(False, False)
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=18)
        frm.pack(fill="both", expand=True)
        from tkinter import Label as TkLabel
        TkLabel(frm, text="⚠  This permanently deletes "
                          f"{len(files)} generated image"
                          f"{'s' if len(files) != 1 else ''} "
                          f"({mb:.0f} MB) from the output folder.\n"
                          "This cannot be undone.",
                fg="#e74c3c", bg=BG, font=("Segoe UI", 10, "bold"),
                justify="left", wraplength=420).pack(anchor="w")
        brow = ttk.Frame(frm)
        brow.pack(fill="x", pady=(14, 0))

        def do_delete():
            n = 0
            for f in files:
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
            self._clear_history()
            self.status_var.set(f"Deleted {n} image files permanently.")
            dlg.destroy()

        ttk.Button(brow, text="Delete permanently", style="Danger.TButton",
                   command=do_delete).pack(side="right")
        ttk.Button(brow, text="Cancel",
                   command=dlg.destroy).pack(side="right", padx=(0, 8))
        dlg.grab_set()

    def _select(self, idx):
        self.current = idx
        self._show_current()

    def _save_as(self):
        if self.current is None:
            return
        _img, params, src = self.session[self.current]
        is_gif = str(src).lower().endswith(".gif")
        ext = ".gif" if is_gif else ".png"
        ftypes = [("GIF animation", "*.gif")] if is_gif \
            else [("PNG image", "*.png")]
        path = filedialog.asksaveasfilename(
            defaultextension=ext, filetypes=ftypes,
            initialfile=f"comic_seed{params['seed']}{ext}")
        if path:
            shutil.copy2(src, path)
            self.status_var.set(f"Saved to {path}")

    # -------------------------------------------------- border maker
    def _generate_border(self, queue=False):
        """Generate a themed 4:3 / 16:9 border frame with a transparent
        center — for overlays, bezels and framing. No text allowed."""
        theme = self._get(self.border_prompt_box)
        if not theme:
            messagebox.showinfo("Border prompt", "Describe the border first "
                                "— a short theme ('haunted forest, gnarled "
                                "branches') or a full precise prompt.")
            return
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        w, h = BORDER_SIZES[self.border_aspect_var.get()]
        refs = list(self.border_ref_paths)
        editor = self._editor_engine()
        base = BORDER_TEMPLATE.format(theme=theme) \
            if self.border_auto_var.get() else theme
        sty = self._border_style()
        if sty:
            base += ", " + sty["style"]
        if refs:
            # references go through the image editor: it redraws them as
            # the border, carrying over style, characters and composition
            if not self._ensure_editor_ready(editor, "border_progress"):
                return
            prompt = "redraw this image as " + base
            model = f"editor:{editor}"
            loras = []
        else:
            prompt = base
            model = self._border_model_raw()
            if not model:
                messagebox.showerror("Model", "No model selected — pick "
                                              "one in the Border maker or "
                                              "the main controls.")
                return
            have_border_lora = BORDER_LORA_FILE in list_loras()
            # the trained border LoRA is SDXL-only and is what makes a real
            # frame — if it's installed but the chosen model is Flux, route
            # the border to the best SDXL checkpoint so the LoRA applies
            if have_border_lora and model_family(model) in ("flux", "schnell"):
                sdxl = self._best_sdxl_model()
                if sdxl:
                    model = sdxl
                    self.status_var.set(
                        f"Border maker using {sdxl} + the trained frame "
                        "LoRA (Flux can't use it).")
            if self._model_fits.get(model, "ok") == "block":
                self._vram_block_msg(model)
                return
            strength = round(self.lora_strength.get(), 2)
            loras = [(n, strength) for n in self._selected_loras()]
            if have_border_lora and \
                    model_family(model) not in ("flux", "schnell"):
                if BORDER_LORA_FILE not in [n for n, _s in loras]:
                    loras.append((BORDER_LORA_FILE, 0.8))
                prompt = f"{BORDER_TRIGGER}, " + prompt
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        steps = None if self.steps_var.get() == "auto" \
            else int(self.steps_var.get())
        # 2nd-pass center clean-up needs Kontext installed and fitting VRAM
        clean = bool(self.border_clean_var.get() and not refs
                     and not self._editor_missing("kontext")
                     and self._editor_tier("kontext") != "block")
        if self.border_clean_var.get() and not refs and not clean:
            self.status_var.set("Auto-clean center off: Flux Kontext isn't "
                                "installed or won't fit VRAM.")
        params = dict(
            prompt=prompt, user_prompt=theme, style="border frame",
            negative=BORDER_NEGATIVE, model=model, loras=loras,
            width=w, height=h, seed=seed, steps=steps, cfg=None,
            batch=max(1, min(10, self.border_batch_var.get())),
            random_seed=self.random_seed_var.get(),
            transparent=False, preset="border maker",
            upscale=self.upscale_var.get(),
            border_cut=int(self.border_thick_var.get()),
            border_clean=clean,
            ref_images=refs, editor=editor,
            ref_collage_size=(w, h) if refs else None,
            out_size=(w, h) if refs else None)
        if queue:
            self._enqueue("border", "border: " + theme[:30], params)
            return
        self.busy = True
        self.go_btn.state(["disabled"])
        self.border_progress["value"] = 0
        gen = Generator(ChannelQueue(self.ui_queue, "border_progress"))
        threading.Thread(target=gen.run, args=(params,),
                         daemon=True).start()
        self.status_var.set("Generating border…")

    # -------------------------------------------------- image editor
    EDITOR_FILES = {
        "kontext": [("diffusion_models", KONTEXT_FILE)],
        "qwen": [("diffusion_models", QWEN_EDIT_FILE),
                 ("text_encoders", QWEN_TE_FILE),
                 ("vae", QWEN_VAE_FILE)],
        "wan": [("diffusion_models", WAN_FILE),
                ("text_encoders", WAN_TE_FILE),
                ("vae", WAN_VAE_FILE)],
        "wanflf": [("diffusion_models", WAN_FLF_FILE),
                   ("text_encoders", WAN_TE_FILE),
                   ("clip_vision", WAN_CLIPVIS_FILE),
                   ("vae", WAN21_VAE_FILE)],
    }
    EDITOR_UNETS = {"kontext": KONTEXT_FILE, "qwen": QWEN_EDIT_FILE,
                    "wan": WAN_FILE, "wanflf": WAN_FLF_FILE}

    def _editor_missing(self, engine):
        missing = [(d, f) for d, f in self.EDITOR_FILES[engine]
                   if not (MODELS / d / f).exists()]
        if engine == "kontext" and \
                not (MODELS / "checkpoints" / FLUX_CLIP_SRC).exists():
            missing.append(("checkpoints", FLUX_CLIP_SRC))
        return missing

    def _install_editor(self, engine, missing, channel="progress"):
        try:
            entries = json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
        except Exception:
            entries = []
        for d, f in missing:
            e = next((x for x in entries
                      if x["dir"] == d and x["local"] == f), None)
            if not e:
                self.ui_queue.put(("error", f"no download source for {f}"))
                return
            self.ui_queue.put(("status", f"Downloading {f}…"))
            try:
                download_model_update(
                    dict(e, size=0),
                    lambda s: self.ui_queue.put(("status", s)),
                    lambda d, t: self.ui_queue.put((channel, d, t)))
            except Exception as ex:
                self.ui_queue.put(("error", f"download failed: {ex}"))
                return
        self.ui_queue.put(("status", "Editor installed — hit Generate "
                                     "again."))

    def _ensure_editor_ready(self, editor, channel="progress"):
        """True when the editor can run now; otherwise guides the user
        (VRAM block, download offer, engine restart) and returns False."""
        tier = self._editor_tier(editor)
        if tier == "block":
            messagebox.showwarning(
                "Not enough GPU memory",
                f"This editor needs about {EDITOR_VRAM.get(editor, 16)} "
                f"GB of VRAM — your card has {self.vram_gb:.1f} GB.")
            return False
        if tier == "warn":
            self.status_var.set("Running close to the VRAM limit — "
                                "expect slower processing.")
        missing = self._editor_missing(editor)
        if missing:
            gb = "12" if editor == "kontext" else "28"
            if messagebox.askyesno(
                    "Install editor",
                    f"The image editor needs {len(missing)} model "
                    f"file(s) (~{gb} GB) that aren't installed yet."
                    "\n\nDownload now? Watch the status bar; hit "
                    "Generate again when it says done."):
                threading.Thread(target=self._install_editor,
                                 args=(editor, missing, channel),
                                 daemon=True).start()
            return False
        if not self._engine_knows_editor(editor):
            self.status_var.set("Restarting engine to load the editor "
                                "files — wait for 'Engine ready.', "
                                "then Generate again.")
            threading.Thread(target=self._restart_engine,
                             daemon=True).start()
            return False
        return True

    def _engine_knows_editor(self, engine):
        try:
            unets = _api_choices("UNETLoader", "unet_name")
        except Exception:
            return True   # engine not up yet; boot flow handles that
        return self.EDITOR_UNETS.get(engine, KONTEXT_FILE) in unets

    # -------------------------------------------------- style add-on
    def _style_support_ok(self):
        """IP-Adapter node loaded in the engine AND its models on disk."""
        try:
            api_get("/object_info/IPAdapterUnifiedLoader")
            node_ok = True
        except Exception:
            node_ok = False
        return node_ok and bool(scan_models("ipadapter")) \
            and bool(scan_models("clip_vision"))

    def _install_style_support(self):
        """Self-install the IP-Adapter node + models, then restart the
        engine — for installs that predate v1.3.0."""
        try:
            node_dir = ENGINE_DIR / "custom_nodes" / "ComfyUI_IPAdapter_plus"
            if not node_dir.exists():
                self.ui_queue.put(("status", "Installing IP-Adapter node…"))
                tmpd = PROJECT / "_upd_tmp"
                shutil.rmtree(tmpd, ignore_errors=True)
                tmpd.mkdir(parents=True)
                z = tmpd / "ipa.zip"
                with requests.get(IPA_NODE_ZIP, stream=True,
                                  timeout=60) as r:
                    r.raise_for_status()
                    with open(z, "wb") as fh:
                        for c in r.iter_content(1 << 20):
                            fh.write(c)
                with zipfile.ZipFile(z) as zf:
                    zf.extractall(tmpd)
                inner = next(tmpd.glob("ComfyUI_IPAdapter_plus-*"))
                node_dir.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(inner), str(node_dir))
                shutil.rmtree(tmpd, ignore_errors=True)
            try:
                entries = json.loads(
                    MANIFEST_FILE.read_text(encoding="utf-8"))
            except Exception:
                entries = []
            for e in entries:
                if e["dir"] in ("ipadapter", "clip_vision") and \
                        not (MODELS / e["dir"] / e["local"]).exists():
                    self.ui_queue.put(("status",
                                       f"Downloading {e['local']}…"))
                    download_model_update(
                        dict(e, size=0),
                        lambda s: self.ui_queue.put(("status", s)))
            self.ui_queue.put(("status",
                               "Restarting engine with IP-Adapter…"))
            kill_engine()
            time.sleep(2)
            threading.Thread(target=self._boot_engine, daemon=True).start()
            self.ui_queue.put(("status", "IP-Adapter installed — wait for "
                                         "'Engine ready.', then Generate "
                                         "again."))
        except Exception as e:
            self.ui_queue.put(("error", f"IP-Adapter install failed: {e}"))

    # -------------------------------------------------- animator
    def _pick_anim_image(self):
        path = filedialog.askopenfilename(filetypes=[
            ("Images", "*.png;*.jpg;*.jpeg;*.webp"), ("All files", "*.*")])
        if path:
            self.anim_image_path = path
            self.anim_img_var.set(Path(path).name)
            self._schedule_persist()

    def _thumb_to_animator(self, idx):
        if idx >= len(self.session):
            return
        self.current = idx
        self._show_current()
        _img, _params, path = self.session[idx]
        self.anim_image_path = str(path)
        self.anim_img_var.set(Path(path).name)
        self.status_var.set(f"{Path(path).name} loaded into the Animator — "
                            "describe an action and hit 🎬.")
        self._schedule_persist()

    def _use_current_for_anim(self):
        if self.current is None or not self.session:
            self.status_var.set("Select an image in the gallery first.")
            return
        _img, _params, path = self.session[self.current]
        self.anim_image_path = str(path)
        self.anim_img_var.set(Path(path).name)
        self._schedule_persist()

    def _clear_anim_image(self):
        self.anim_image_path = None
        self.anim_img_var.set("none")
        self._schedule_persist()

    def _on_anim_preset(self, _event=None):
        txt = ANIM_PRESETS.get(self.anim_preset_var.get())
        if txt:
            self._set(self.anim_prompt_box, txt)
            self._schedule_persist()

    def _generate_animation(self, queue=False):
        if not queue and self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        if not self.anim_image_path or \
                not Path(self.anim_image_path).exists():
            messagebox.showinfo("Character", "Load a character image first "
                                             "(🖼 Character… or Use "
                                             "current).")
            return
        action = self._get(self.anim_prompt_box)
        if not action:
            messagebox.showinfo("Action", "Describe the action first — "
                                          "e.g. 'walk cycle', 'sword "
                                          "slash', 'idle breathing, cape "
                                          "swaying'.")
            return
        if not self._ensure_editor_ready("wan", "anim_progress"):
            return
        w, h = ANIM_SIZES[self.anim_size_var.get()]
        secs = max(1, min(5, self.anim_secs_var.get()))
        base_fps = 24
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        p = dict(image=self.anim_image_path, action=action, w=w, h=h,
                 length=base_fps * secs + 1, base_fps=base_fps,
                 keep_every=ANIM_KEEP.get(self.anim_keep_var.get(), 2),
                 loop=self.anim_loop_var.get(),
                 transparent=self.anim_transparent_var.get(),
                 gif=self.anim_gif_var.get(),
                 motion=self.anim_motion_var.get(),
                 zip=self.anim_zip_var.get(),
                 sheet=self.anim_sheet_var.get(),
                 video=self.anim_video_var.get(), seed=seed)
        if queue:
            self._enqueue("anim", "anim: " + action[:30], p)
            return
        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress["value"] = 0
        self._persist()
        threading.Thread(target=self._run_animation, args=(p,),
                         daemon=True).start()

    def _run_animation(self, p):
        try:
            status = lambda s: self.ui_queue.put(("status", s))
            status("Animating — uploading character…")
            gen = Generator(ChannelQueue(self.ui_queue, "anim_progress"))
            # auto-prep: isolate the character and stage it on neutral
            # gray — video models animate poorly on black voids/cutouts
            status("Preparing character (background staging)…")
            src_img = Image.open(p["image"]).convert("RGBA")
            cut = remove_background(src_img)
            stage = Image.new("RGBA", cut.size, (200, 200, 205, 255))
            stage.alpha_composite(cut)
            name = gen._upload_pil(stage.convert("RGB"),
                                   "cbac_anim_prepped.png")
            motion = ANIM_MOTION.get(p.get("motion", ""),
                                     list(ANIM_MOTION.values())[0])
            action = p["action"]
            if len(action.split()) <= 3:
                # terse actions ("walking") generate weak motion — expand
                # them into an explicit full-body cycle
                action = (f"the character is {action}, performing the "
                          "motion as a clear full-body cycle with large "
                          "limb movements repeating continuously")
            prompt = (f"{action}. {motion}. The character stays "
                      "centered in frame against a flat plain solid "
                      "background, full body always visible, locked "
                      "camera, no camera movement, no scene change.")
            gp = dict(prompt=prompt, anim_image_name=name, width=p["w"],
                      height=p["h"], length=p["length"], seed=p["seed"])
            graph = build_wan_graph(gp)
            import websocket
            ws = websocket.WebSocket()
            ws.connect(f"ws://{ENGINE_HOST}:{ENGINE_PORT}/ws"
                       f"?clientId={gen.client_id}", timeout=30)
            try:
                r = requests.post(f"{ENGINE_URL}/prompt",
                                  json={"prompt": graph,
                                        "client_id": gen.client_id},
                                  timeout=30)
                if r.status_code == 400:
                    raise RuntimeError("engine rejected the animation "
                                       "workflow — try again after the "
                                       "engine restarts")
                r.raise_for_status()
                pid = r.json()["prompt_id"]
                status(f"Animating {p['length']} frames — takes a few "
                       "minutes…")
                metas = gen._await_images(ws, pid, timeout=2400)  # live %
            finally:
                ws.close()
            if not metas:
                raise RuntimeError("animation produced no frames")
            status(f"Fetching {len(metas)} frames…")
            frames = [gen._fetch_image(m) for m in metas]
            kept = frames[::p["keep_every"]]
            if p["loop"].startswith("Seamless"):
                kept = best_loop_cut(kept)

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            slug = re.sub(r"[^a-z0-9]+", "_",
                          p["action"].lower())[:30].strip("_") or "anim"
            out_dir = OUTPUT / "animations" / f"{stamp}_{slug}"
            frames_dir = out_dir / "frames"
            frames_dir.mkdir(parents=True, exist_ok=True)

            if p["transparent"]:
                status("Removing backgrounds from "
                       f"{len(kept)} frames…")
                raw_dir = out_dir / "_raw_frames"
                raw_dir.mkdir(parents=True, exist_ok=True)
                for i, f in enumerate(kept):
                    f.save(raw_dir / f"frame_{i:03d}.png")
                remove_background_dir(raw_dir, frames_dir)
                status("Cleaning frame edges (defringe)…")
                raw_files = sorted(raw_dir.glob("*.png"))
                bgs = [estimate_bg(Image.open(rf)) for rf in raw_files]
                kept = []
                for i, fp in enumerate(sorted(frames_dir.glob("*.png"))):
                    bg = bgs[i] if i < len(bgs) else STAGE_BG
                    f = defringe(Image.open(fp).convert("RGBA"), bg=bg)
                    f.save(fp)   # frames on disk get clean edges too
                    kept.append(f)
                shutil.rmtree(raw_dir, ignore_errors=True)
            else:
                for i, f in enumerate(kept):
                    f.save(frames_dir / f"frame_{i:03d}.png")

            result_path = frames_dir / "frame_000.png"
            looped = kept if p["loop"].startswith("Seamless") \
                else apply_loop(kept, p["loop"])
            fps_out = p.get("base_fps", 24) / p["keep_every"]
            extras = []
            if p.get("gif", True):
                result_path = out_dir / "animation.gif"
                save_gif(looped, result_path, fps_out, p["transparent"])
                extras.append("GIF")
            if p.get("sheet"):
                try:
                    status("Packing sprite sheet…")
                    save_sprite_sheet(kept, out_dir / "spritesheet.png",
                                      out_dir / "spritesheet.json", fps_out)
                    extras.append("sprite sheet")
                except Exception as e:
                    status(f"Sprite sheet skipped ({e}).")
            vid = (p.get("video") or "none").lower()
            if vid in ("mp4", "webm"):
                try:
                    status(f"Encoding {vid.upper()} video…")
                    vpath = out_dir / f"animation.{vid}"
                    save_video(looped, vpath, fps_out, webm=(vid == "webm"))
                    extras.append(vid.upper())
                except Exception as e:
                    status(f"{vid.upper()} export skipped ({e}).")
            if p["zip"]:
                shutil.make_archive(str(out_dir), "zip", out_dir)
            self.ui_queue.put(("finished_image", kept[0].copy(),
                              dict(model="animator", seed=p["seed"]),
                              result_path))
            status(f"Animation done: {len(kept)} frames"
                   + ("".join(f" + {e}" for e in extras))
                   + f" in {out_dir.name}"
                   + (" (+zip)" if p["zip"] else "")
                   + ". Select it in the gallery to watch it play.")
            self.ui_queue.put(("done", None))
        except Exception as e:
            self.ui_queue.put(("error", f"animation: {e}"))

    # -------------------------------------------------- training dataset
    def _add_to_training(self):
        """Copy the current image + a caption into training\\dataset —
        the raw material for training your own style LoRA (see
        TRAINING.md). The dataset is the asset that carries your style
        to any current or future model."""
        if self.current is None or not self.session:
            self.status_var.set("Generate or select an image first.")
            return
        _img, params, path = self.session[self.current]
        ds = PROJECT / "training" / "dataset"
        ds.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = ds / f"{stamp}_{path.stem}.png"
        shutil.copy2(path, dest)
        caption = params.get("prompt") or params.get("user_prompt") \
            or "comic book style artwork"
        dest.with_suffix(".txt").write_text(caption, encoding="utf-8")
        self.status_var.set(
            f"Added to training set ({len(list(ds.glob('*.png')))} images) "
            "— captions are editable .txt files; see TRAINING.md.")

    # -------------------------------------------------- civitai loras
    def _civitai_dialog(self):
        dlg = Toplevel(self.root)
        dlg.title("Download LoRA from CivitAI")
        dlg.configure(bg=BG)
        dlg.geometry("560x220")
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="CivitAI model page URL:").grid(row=0, column=0,
                                                            sticky=W)
        url_var = StringVar()
        ttk.Entry(frm, textvariable=url_var).grid(row=0, column=1, sticky=NSEW,
                                                  padx=6, pady=4)
        ttk.Label(frm, text="API key (free, from civitai.com "
                            "account settings):").grid(row=1, column=0, sticky=W)
        key_var = StringVar(value=dpapi_decrypt(
            self.settings.get("civitai_key_enc", "")))
        ttk.Entry(frm, textvariable=key_var, show="•").grid(row=1, column=1,
                                                            sticky=NSEW, padx=6,
                                                            pady=4)
        out_var = StringVar(value="")
        ttk.Label(frm, textvariable=out_var, style="Dim.TLabel",
                  wraplength=520).grid(row=3, column=0, columnspan=2, sticky=W,
                                       pady=6)

        def go():
            self.settings["civitai_key_enc"] = dpapi_encrypt(key_var.get().strip())
            self.settings.pop("civitai_key", None)  # remove any legacy plaintext
            self._save_settings()
            threading.Thread(target=self._civitai_download,
                             args=(url_var.get().strip(), key_var.get().strip(),
                                   out_var), daemon=True).start()

        ttk.Button(frm, text="Download to models\\loras",
                   command=go).grid(row=2, column=1, sticky=E, pady=4)

    def _civitai_download(self, url, key, out_var):
        try:
            out_var.set("Resolving…")
            m = re.search(r"models/(\d+)", url)
            if not m:
                out_var.set("Could not find a model id in that URL.")
                return
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            info = requests.get(f"https://civitai.com/api/v1/models/{m.group(1)}",
                                headers=headers, timeout=30).json()
            ver = info["modelVersions"][0]
            f = next((f for f in ver["files"]
                      if f["name"].endswith(".safetensors")), None)
            if f is None:
                out_var.set("No .safetensors file in that model — refusing "
                            "other formats (pickle files can execute code).")
                return
            # strip any path components the server sends — basename only
            fname = Path(f["name"]).name
            if not fname.endswith(".safetensors"):
                out_var.set("Unexpected file name — aborted.")
                return
            out_var.set(f"Downloading {fname} ({f['sizeKB'] / 1024:.0f} MB)…")
            if not str(f["downloadUrl"]).startswith("https://"):
                out_var.set("Refusing non-HTTPS download URL — aborted.")
                return
            r = requests.get(f["downloadUrl"], headers=headers, stream=True,
                             timeout=60)
            r.raise_for_status()
            dest = (MODELS / "loras" / fname).resolve()
            if dest.parent != (MODELS / "loras").resolve():
                out_var.set("Bad destination path — aborted.")
                return
            with open(dest, "wb") as fh:
                for chunk in r.iter_content(1 << 20):
                    fh.write(chunk)
            out_var.set(f"Done — {fname}. Hit ↻ next to the model box to "
                        f"see it in the LoRA lists.")
            self.ui_queue.put(("status", f"LoRA installed: {fname}"))
        except Exception as e:
            out_var.set(f"Download failed: {e}")

    # -------------------------------------------------- settings
    def _load_settings(self):
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_settings(self):
        SETTINGS_FILE.write_text(json.dumps(self.settings, indent=2),
                                 encoding="utf-8")


def main():
    root = Tk()
    _mutex_handle, already = single_instance_handle()
    if already:
        from tkinter import messagebox as _mb
        if not _mb.askyesno(
                "Already running",
                "Comic Book Art Creator appears to be already running.\n\n"
                "Running a second copy can make generations and progress go "
                "to the wrong window, and both share one engine.\n\n"
                "Open another window anyway?"):
            root.destroy()
            return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
