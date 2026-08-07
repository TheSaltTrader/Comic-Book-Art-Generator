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

APP_VERSION = "1.3.1"

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
        "  clip_vision: clip_vision\n", encoding="utf-8")
    cmd = [str(engine_python()), "main.py",
           "--listen", ENGINE_HOST, "--port", str(ENGINE_PORT),
           "--extra-model-paths-config", str(EXTRA_PATHS_YAML),
           "--output-directory", str(RAW_OUT),
           "--disable-auto-launch"]
    log = open(PROJECT / "engine.log", "w", encoding="utf-8", errors="replace")
    subprocess.Popen(cmd, cwd=str(ENGINE_DIR), stdout=log,
                     stderr=subprocess.STDOUT, creationflags=NO_WINDOW,
                     env=_contained_env())


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


def download_model_update(entry, status_cb):
    """Stream one updated model to a temp file, then swap it in place."""
    url = (f"https://huggingface.co/{entry['repo']}/resolve/main/"
           f"{entry['remote_file']}")
    dest = MODELS / entry["dir"] / entry["local"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    done, total_mb = 0, entry["size"] / (1024**2)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 22):
                fh.write(chunk)
                done += len(chunk)
                if done % (1 << 26) < (1 << 22):  # every ~64 MB
                    status_cb(f"Updating {entry['local']}: "
                              f"{done / (1024**2):.0f}/{total_mb:.0f} MB")
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


def build_graph(p):
    """Build a ComfyUI prompt graph from generation params dict."""
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
        denoise = p.get("border_denoise", 1.0)
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
    g["8"] = {"class_type": "SaveImage",
              "inputs": {"filename_prefix": "cbac", "images": ["7", 0]}}
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
                   "logo, watermark, signature, caption, label, subtitles")

BORDER_TEMPLATES = {
    "Franchise (game / movie / comic)":
        "epic decorative border frame themed after {theme}, filled with "
        "iconic visual motifs, props, character silhouettes, colors and "
        "symbols of {theme}, richly detailed frame covering all four edges "
        "and corners of the image, themed corner ornaments, completely "
        "textless artwork with no lettering or logos, plain empty solid "
        "dark center panel, high detail",
    "Arcade cabinet bezel":
        "vibrant airbrushed bezel border art themed after {theme}, dynamic "
        "action motifs and characters of {theme} woven around the frame, "
        "detailed border filling all four edges and corners of the image, "
        "completely textless artwork with no lettering or logos, plain "
        "empty solid dark center panel, high detail",
    "Material / concept":
        "{theme}, ornate decorative border frame design, richly detailed "
        "frame filling all four edges and corners of the image, themed "
        "corner ornaments, completely textless artwork with no lettering, "
        "plain empty solid dark center panel, high detail",
}


BEZEL_POSITIONS = {
    "Left panel":    lambda W, H, w, h: (int(0.01 * W), (H - h) // 2),
    "Right panel":   lambda W, H, w, h: (W - w - int(0.01 * W), (H - h) // 2),
    "Top left":      lambda W, H, w, h: (int(0.01 * W), int(0.01 * H)),
    "Top center":    lambda W, H, w, h: ((W - w) // 2, int(0.005 * H)),
    "Top right":     lambda W, H, w, h: (W - w - int(0.01 * W), int(0.01 * H)),
    "Bottom left":   lambda W, H, w, h: (int(0.01 * W), H - h - int(0.01 * H)),
    "Bottom center": lambda W, H, w, h: ((W - w) // 2, H - h - int(0.005 * H)),
    "Bottom right":  lambda W, H, w, h: (W - w - int(0.01 * W),
                                         H - h - int(0.01 * H)),
}


def make_collage(paths, w, h):
    """Grid-fit multiple reference images onto one w×h canvas (cover-crop
    per cell) — how several references become a single img2img init."""
    import math
    paths = list(paths)[:9]
    n = max(1, len(paths))
    cols = 1 if n == 1 else (2 if n <= 4 else 3)
    rows = math.ceil(n / cols)
    canvas = Image.new("RGB", (w, h), (16, 16, 20))
    cw, ch = w // cols, h // rows
    for i, p in enumerate(paths):
        img = Image.open(p).convert("RGB")
        s = max(cw / img.width, ch / img.height)
        img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1),
                         Image.LANCZOS)
        x0, y0 = (img.width - cw) // 2, (img.height - ch) // 2
        img = img.crop((x0, y0, x0 + cw, y0 + ch))
        canvas.paste(img, ((i % cols) * cw, (i // cols) * ch))
    return canvas


def compose_bezel(border_path, items, clip=False):
    """Composite character/art PNGs onto a border. Each item: dict with
    path, pos (BEZEL_POSITIONS key), size (% of border height), flip,
    dx/dy nudge (% of canvas)."""
    base = Image.open(border_path).convert("RGBA")
    W, H = base.size
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for it in items:
        img = Image.open(it["path"]).convert("RGBA")
        target_h = max(16, int(H * it["size"] / 100))
        scale = target_h / img.height
        img = img.resize((max(1, int(img.width * scale)), target_h),
                         Image.LANCZOS)
        if it.get("flip"):
            img = img.transpose(Image.FLIP_LEFT_RIGHT)
        x, y = BEZEL_POSITIONS[it["pos"]](W, H, img.width, img.height)
        x += int(W * it.get("dx", 0) / 100)
        y += int(H * it.get("dy", 0) / 100)
        layer.paste(img, (x, y), img)   # paste clips at canvas edges
    if clip:
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"),
                                           base.getchannel("A")))
    out = base.copy()
    out.alpha_composite(layer)
    return out


def cut_center(img, thickness_pct):
    """Make the center of a border/frame image fully transparent, leaving
    a border of the given thickness (percent of the shorter side)."""
    img = img.convert("RGBA")
    w, h = img.size
    t = max(8, int(min(w, h) * thickness_pct / 100))
    mask = Image.new("L", (w, h), 255)
    ImageDraw.Draw(mask).rectangle((t, t, w - t, h - t), fill=0)
    mask = mask.filter(ImageFilter.GaussianBlur(1.5))
    img.putalpha(mask)
    return img


# --------------------------------------------------------------------------
# generation worker
# --------------------------------------------------------------------------

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
        if params.get("ref_images") and \
                params.get("ref_mode") in ("style", "character"):
            params["style_ref_names"] = [self._upload_ref(rp)
                                         for rp in params["ref_images"]]
            params["ref_weight_type"] = "style transfer" \
                if params["ref_mode"] == "style" else "standard"
        elif params.get("ref_images"):
            collage = make_collage(params["ref_images"], params["width"],
                                   params["height"])
            params["ref_image_name"] = self._upload_pil(
                collage, "cbac_ref_collage.png")
        elif params.get("ref_image"):
            params["ref_image_name"] = self._upload_ref(params["ref_image"])
        if params.get("border_cut"):
            w, h = params["width"], params["height"]
            # mask is slightly wider than the final cut so the art runs
            # past the transparency line instead of stopping at it
            inner = int(min(w, h) * min(45, params["border_cut"] + 5) / 100)
            if params.get("border_refs"):
                # reference images seed the border zone; the preserved
                # center is painted black so it stays empty either way
                bg = make_collage(params["border_refs"], w, h)
                params["border_denoise"] = params.get("denoise", 0.7)
            else:
                bg = Image.new("RGB", (w, h), (8, 8, 10))
            ImageDraw.Draw(bg).rectangle(
                (inner, inner, w - inner, h - inner), fill=(8, 8, 10))
            mask = Image.new("RGB", (w, h), (255, 255, 255))
            ImageDraw.Draw(mask).rectangle(
                (inner, inner, w - inner, h - inner), fill=(0, 0, 0))
            params["border_assets"] = (
                self._upload_pil(bg, "cbac_border_bg.png"),
                self._upload_pil(mask, "cbac_border_mask.png"))
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
                    self.q.put(("image", img, p))
            self.q.put(("done", None))
        finally:
            ws.close()

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

    def _await_images(self, ws, prompt_id):
        ws.settimeout(600)
        while True:
            msg = ws.recv()
            if isinstance(msg, bytes):
                continue  # binary preview frames — ignored
            data = json.loads(msg)
            t, d = data.get("type"), data.get("data", {})
            if t == "progress":
                self.q.put(("progress", d.get("value", 0), d.get("max", 1)))
            elif t == "execution_error" and d.get("prompt_id") == prompt_id:
                raise RuntimeError(d.get("exception_message", "engine error"))
            elif t == "executing" and d.get("prompt_id") == prompt_id \
                    and d.get("node") is None:
                break  # finished
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
        self._auto_negative = None  # last negative set by a preset (vs typed)
        self._model_display = {}    # "name · 6.5 GB" -> raw filename
        self._pending_loras = set()
        self._persist_job = None
        self.ref_paths = []        # img2img reference images
        self.border_ref_paths = []  # border-maker reference images

        self._build_ui()
        self._apply_ui_state(self.settings.get("ui", {}))
        self._refresh_models()     # disk scan — fills dropdowns before engine
        self._wire_autosave()      # every change saved as it happens
        self._schedule_persist()   # baseline save right away
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll_queue)
        threading.Thread(target=self._boot_engine, daemon=True).start()
        threading.Thread(target=self._check_updates_bg, daemon=True).start()
        root.after(600, self._first_run_check)

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
               "that's why lists are empty.\n\n")
        if setup_exe.exists():
            msg += ("Run Setup now? It downloads everything needed "
                    "(~36 GB) and this app will find it automatically "
                    "afterwards (hit ↻ or restart).")
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
        s.configure("TCombobox", padding=4)
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

        def _left_wheel(e):
            self.left_canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
        self.left_canvas.bind("<MouseWheel>", _left_wheel)
        left.bind("<MouseWheel>", _left_wheel)
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
                           lambda _e: self._refresh_preset_list())
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
        self.preset_var = StringVar(value=NONE_PRESET)
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

        # size + settings
        ttk.Label(left, text="CANVAS & SETTINGS", style="Head.TLabel").grid(
            row=r, sticky=W, pady=(8, 0)); r += 1
        srow = ttk.Frame(left); srow.grid(row=r, sticky=NSEW, pady=2); r += 1
        srow.columnconfigure(0, weight=1)
        self.size_var = StringVar(value="Portrait — cover (832x1216)")
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

        # reference images (img2img — one or several)
        ttk.Label(left, text="REFERENCE IMAGES (optional — the AI redraws "
                             "them per your prompt; several = collage)",
                  style="Head.TLabel").grid(row=r, sticky=W, pady=(8, 0)); r += 1
        rrow = ttk.Frame(left); rrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        rrow.columnconfigure(1, weight=1)
        ttk.Button(rrow, text="🖼 Load…", width=9,
                   command=self._pick_ref).grid(row=0, column=0)
        self.ref_var = StringVar(value="none — text only")
        ttk.Label(rrow, textvariable=self.ref_var, style="Dim.TLabel",
                  wraplength=240).grid(row=0, column=1, sticky=W, padx=6)
        ttk.Button(rrow, text="✕", width=3,
                   command=self._clear_ref).grid(row=0, column=2)
        self.ref_mode_var = StringVar(value="redraw")
        for val, txt in (
                ("redraw", "Redraw composition — keeps the image's layout "
                           "(img2img, all models)"),
                ("style", "Copy the style — new scene painted in the "
                          "reference's look (SDXL models)"),
                ("character", "Use the character — put the reference's "
                              "subject in new scenes (SDXL models)")):
            ttk.Radiobutton(left, text=txt, variable=self.ref_mode_var,
                            value=val).grid(row=r, sticky=W); r += 1
        chrow = ttk.Frame(left); chrow.grid(row=r, sticky=NSEW, pady=(0, 4)); r += 1
        ttk.Label(chrow, text="Change amount",
                  style="Dim.TLabel").pack(side="left")
        self.change_var = DoubleVar(value=60)
        ttk.Scale(chrow, from_=10, to=95, variable=self.change_var,
                  length=150).pack(side="left", padx=6)
        self.change_lab = ttk.Label(chrow, text="60%", width=5,
                                    style="Dim.TLabel")
        self.change_lab.pack(side="left")
        self.change_var.trace_add(
            "write", lambda *_a: self.change_lab.config(
                text=f"{int(self.change_var.get())}%"))

        # generate
        self.go_btn = ttk.Button(left, text="⚡  GENERATE", style="Go.TButton",
                                 command=self._generate)
        self.go_btn.grid(row=r, sticky=NSEW, pady=(12, 4)); r += 1
        self.progress = ttk.Progressbar(left, mode="determinate")
        self.progress.grid(row=r, sticky=NSEW, pady=2); r += 1
        self.status_var = StringVar(value="Starting engine…")
        ttk.Label(left, textvariable=self.status_var,
                  style="Dim.TLabel", wraplength=400).grid(row=r, sticky=W); r += 1

        # ---------- border maker (bottom of the panel) ----------
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
        bsrow = ttk.Frame(left); bsrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(bsrow, text="Style", style="Dim.TLabel").grid(row=0,
                                                                column=0)
        self.border_style_var = StringVar(value=list(BORDER_TEMPLATES)[0])
        ttk.Combobox(bsrow, textvariable=self.border_style_var,
                     state="readonly", exportselection=False,
                     values=list(BORDER_TEMPLATES),
                     width=34).grid(row=0, column=1, padx=(4, 0), sticky=W)
        bmrow = ttk.Frame(left); bmrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        bmrow.columnconfigure(1, weight=1)
        ttk.Label(bmrow, text="Model", style="Dim.TLabel").grid(row=0,
                                                                column=0)
        self.border_model_var = StringVar()
        self.border_model_dd = ttk.Combobox(bmrow,
                                            textvariable=self.border_model_var,
                                            state="readonly", exportselection=False)
        self.border_model_dd.grid(row=0, column=1, padx=(4, 0), sticky="ew")
        barow = ttk.Frame(left); barow.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(barow, text="Aspect", style="Dim.TLabel").grid(row=0,
                                                                 column=0)
        self.border_aspect_var = StringVar(value=list(BORDER_SIZES)[1])
        ttk.Combobox(barow, textvariable=self.border_aspect_var,
                     state="readonly", exportselection=False,
                     values=list(BORDER_SIZES),
                     width=26).grid(row=0, column=1, padx=(4, 8), sticky=W)
        ttk.Label(barow, text="Variations", style="Dim.TLabel").grid(row=0,
                                                                     column=2)
        self.border_count_var = IntVar(value=1)
        ttk.Spinbox(barow, from_=1, to=10, textvariable=self.border_count_var,
                    exportselection=False,
                    width=4).grid(row=0, column=3, padx=(4, 0))
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
        ttk.Label(left, text="Refs guide the border art (uses the 'Change "
                             "amount' slider above for influence).",
                  style="Dim.TLabel", wraplength=400).grid(row=r, sticky=W); r += 1
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
        ttk.Button(left, text="⚡ Generate border",
                   command=self._generate_border).grid(row=r, sticky="ew",
                                                       pady=(4, 8)); r += 1

        # ---------- right column: preview + gallery ----------
        right = ttk.Frame(root, padding=(0, 12, 12, 12))
        right.grid(row=0, column=1, sticky=NSEW)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)

        self.canvas = Canvas(right, bg=BG2, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky=NSEW)
        self.canvas.bind("<Configure>", lambda e: self._show_current())

        brow = ttk.Frame(right); brow.grid(row=1, column=0, sticky=NSEW, pady=(8, 4))
        ttk.Button(brow, text="💾 Save As…", command=self._save_as).pack(side="left")
        ttk.Button(brow, text="📁 Open output folder",
                   command=lambda: os.startfile(OUTPUT)).pack(side="left", padx=6)
        ttk.Button(brow, text="⬇ Get LoRAs (CivitAI)",
                   command=self._civitai_dialog).pack(side="left", padx=6)
        ttk.Button(brow, text="🧩 Bezel composer",
                   command=self._composer_dialog).pack(side="left", padx=6)
        ttk.Button(brow, text="⭐ Add to training set",
                   command=self._add_to_training).pack(side="left", padx=6)
        self.info_var = StringVar(value="")
        ttk.Label(brow, textvariable=self.info_var,
                  style="Dim.TLabel").pack(side="right")

        # gallery strip — horizontally scrollable, with its own clear button
        gwrap = ttk.Frame(right)
        gwrap.grid(row=2, column=0, sticky=NSEW, pady=(4, 0))
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
            "ref_mode": self.ref_mode_var.get(),
            "border_refs": self.border_ref_paths,
            "change": int(self.change_var.get()),
            "border_theme": self._get(self.border_prompt_box),
            "border_auto": self.border_auto_var.get(),
            "border_aspect": self.border_aspect_var.get(),
            "border_thick": int(self.border_thick_var.get()),
            "border_style": self.border_style_var.get(),
            "border_model": self._model_display.get(
                self.border_model_var.get(), self.border_model_var.get()),
            "border_count": self.border_count_var.get(),
            "size": self.size_var.get(),
            "steps": self.steps_var.get(),
            "batch": self.batch_var.get(),
            "transparent": self.transparent_var.get(),
            "seed": self.seed_var.get(),
            "random_seed": self.random_seed_var.get(),
            "auto_negative": self._auto_negative,
        }

    def _apply_ui_state(self, st):
        if not st:
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
            if st.get("ref_mode") in ("redraw", "style", "character"):
                self.ref_mode_var.set(st["ref_mode"])
            brefs = st.get("border_refs", [])
            self.border_ref_paths = [p for p in brefs if Path(p).exists()]
            if self.border_ref_paths:
                first = Path(self.border_ref_paths[0]).name
                self.border_ref_var.set(
                    first if len(self.border_ref_paths) == 1 else
                    f"{len(self.border_ref_paths)} images ({first}, …)")
            self.change_var.set(st.get("change", 60))
            self._set(self.border_prompt_box, st.get("border_theme", ""))
            self.border_auto_var.set(st.get("border_auto", True))
            if st.get("border_aspect") in BORDER_SIZES:
                self.border_aspect_var.set(st["border_aspect"])
            self.border_thick_var.set(st.get("border_thick", 14))
            if st.get("border_style") in BORDER_TEMPLATES:
                self.border_style_var.set(st["border_style"])
            if st.get("border_model"):
                self.border_model_var.set(st["border_model"])
            self.border_count_var.set(st.get("border_count", 1))
            if st.get("size") in SIZE_PRESETS:
                self.size_var.set(st["size"])
            self.steps_var.set(st.get("steps", "auto"))
            self.batch_var.set(st.get("batch", 1))
            self.transparent_var.set(st.get("transparent", False))
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
                    self.transparent_var, self.random_seed_var,
                    self.lora_strength, self.change_var, self.ref_mode_var,
                    self.border_auto_var, self.border_aspect_var,
                    self.border_thick_var, self.border_style_var,
                    self.border_model_var, self.border_count_var):
            var.trace_add("write", self._schedule_persist)
        for box in (self.prompt_box, self.negative_box, self.style_box,
                    self.border_prompt_box):
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
        ups = check_model_updates()
        eng = check_engine_update()
        if ups or eng:
            self.ui_queue.put(("updates", ups, eng))

    def _download_updates(self, ups, eng=None):
        ok, locked = 0, 0
        for u in ups:
            try:
                if download_model_update(
                        u, lambda s: self.ui_queue.put(("status", s))):
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
    def _boot_engine(self):
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

    def _refresh_models(self):
        ckpts = list_checkpoints()
        self._model_display = {}
        for name in ckpts:
            gb = self._size_gb(name)
            disp = f"{name}  ·  {gb:.1f} GB" if gb else name
            self._model_display[disp] = name
        self.model_dd["values"] = list(self._model_display)
        self.border_model_dd["values"] = list(self._model_display)
        if not ckpts:
            self.status_var.set(
                "No models installed — run Setup.exe (or drop .safetensors "
                "into models\\checkpoints), then hit ↻.")

        cur = self._model_raw()
        if cur not in ckpts:
            last = self.settings.get("last_model")
            cur = last if last in ckpts else (ckpts[0] if ckpts else "")
        for disp, raw in self._model_display.items():
            if raw == cur:
                self.model_var.set(disp)
                break
        # border model is fully independent: only default it when empty or
        # its file is gone — never because the main model changed
        bcur = self._model_display.get(self.border_model_var.get(),
                                       self.border_model_var.get())
        if not bcur or bcur not in ckpts:
            bcur = cur
        for disp, raw in self._model_display.items():
            if raw == bcur:
                self.border_model_var.set(disp)
                break

        self._refresh_preset_list()

        loras = list_loras()
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

    def _reuse_seed(self):
        if self.session:
            _, params, _ = self.session[self.current]
            self.seed_var.set(str(params["seed"]))
            self.random_seed_var.set(False)

    # -------------------------------------------------- generation
    def _generate(self):
        if self.busy:
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
        full_prompt = f"{prompt}, {style}" if style else prompt
        model = self._model_raw()
        if not model:
            messagebox.showerror("Model", "No model selected — hit ↻ or wait "
                                          "for downloads to finish.")
            return

        strength = round(self.lora_strength.get(), 2)
        loras = [(name, strength) for name in self._selected_loras()]

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
                      preset=self.preset_var.get(),
                      ref_images=list(self.ref_paths),
                      ref_mode=self.ref_mode_var.get(),
                      style_weight=round(self.change_var.get() / 100, 2),
                      denoise=round(self.change_var.get() / 100, 2))
        if self.ref_paths and self.ref_mode_var.get() in ("style",
                                                          "character"):
            if model_family(model) in ("flux", "schnell"):
                messagebox.showinfo(
                    "Reference mode",
                    "'Copy the style' and 'Use the character' use "
                    "IP-Adapter, which works with SDXL-family models "
                    "(Juggernaut, DreamShaper, Animagine).\nPick one of "
                    "those — or switch to 'Redraw composition' for Flux.")
                return
            if not self._style_support_ok():
                if messagebox.askyesno(
                        "Install style add-on",
                        "Style/character references need the IP-Adapter "
                        "add-on (a node plus ~3.2 GB of models), which "
                        "isn't installed yet.\n\nInstall it now? The "
                        "engine restarts when done — then hit Generate "
                        "again."):
                    threading.Thread(target=self._install_style_support,
                                     daemon=True).start()
                return

        self.settings["last_model"] = model
        self._persist()
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
                    self.status_var.set(f"Saved  {path.name}")
                elif kind == "done":
                    self.busy = False
                    self.go_btn.state(["!disabled"])
                    self.progress["value"] = 0
                elif kind == "engine_ready":
                    self.status_var.set("Engine ready.")
                    self._refresh_models()
                elif kind == "vram":
                    self.vram_note.set(
                        f"Your GPU: {msg[1]:.1f} GB VRAM — each model's size "
                        f"must stay under this (keep ~2 GB headroom).")
                elif kind == "models_changed":
                    self._refresh_models()
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
    def _show_current(self):
        self.canvas.delete("all")
        if self.current is None or not self.session:
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="Your art appears here", fill=FG_DIM,
                font=("Segoe UI", 16))
            return
        img, params, path = self.session[self.current]
        cw = max(self.canvas.winfo_width(), 50)
        ch = max(self.canvas.winfo_height(), 50)
        scale = min(cw / img.width, ch / img.height, 1.0)
        disp = img.resize((int(img.width * scale), int(img.height * scale)),
                          Image.LANCZOS)
        self._tk_img = ImageTk.PhotoImage(disp)
        self.canvas.create_image(cw // 2, ch // 2, image=self._tk_img)
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
        # keep the newest thumbnail in view
        self.gallery.update_idletasks()
        self.gallery_canvas.xview_moveto(1.0)

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
        img, params, _ = self.session[self.current]
        path = filedialog.asksaveasfilename(
            defaultextension=".png", filetypes=[("PNG image", "*.png")],
            initialfile=f"comic_seed{params['seed']}.png")
        if path:
            shutil.copy2(self.session[self.current][2], path)
            self.status_var.set(f"Saved to {path}")

    # -------------------------------------------------- border maker
    def _generate_border(self):
        """Generate a themed 4:3 / 16:9 border frame with a transparent
        center — for overlays, bezels and framing. No text allowed."""
        theme = self._get(self.border_prompt_box)
        if not theme:
            messagebox.showinfo("Border prompt", "Describe the border first "
                                "— a short theme ('haunted forest, gnarled "
                                "branches') or a full precise prompt.")
            return
        if self.busy:
            messagebox.showinfo("Busy", "Wait for the current generation "
                                "to finish.")
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        w, h = BORDER_SIZES[self.border_aspect_var.get()]
        if self.border_auto_var.get():
            template = BORDER_TEMPLATES.get(
                self.border_style_var.get(),
                list(BORDER_TEMPLATES.values())[-1])
            prompt = template.format(theme=theme)
        else:
            prompt = theme  # user's prompt, verbatim
        model = self._model_display.get(self.border_model_var.get(),
                                        self.border_model_var.get()) \
            or self._model_raw()
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        strength = round(self.lora_strength.get(), 2)
        params = dict(
            prompt=prompt, user_prompt=theme,
            style=f"border frame — {self.border_style_var.get()}",
            negative=BORDER_NEGATIVE, model=model,
            loras=[(n, strength) for n in self._selected_loras()],
            width=w, height=h, seed=seed, steps=None, cfg=None,
            batch=max(1, min(10, self.border_count_var.get())),
            random_seed=self.random_seed_var.get(),
            transparent=False, preset="border maker",
            border_cut=int(self.border_thick_var.get()),
            border_refs=list(self.border_ref_paths),
            denoise=round(self.change_var.get() / 100, 2))
        self.busy = True
        self.go_btn.state(["disabled"])
        self.progress["value"] = 0
        gen = Generator(self.ui_queue)
        threading.Thread(target=gen.run, args=(params,),
                         daemon=True).start()
        self.status_var.set("Generating border…")

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

    # -------------------------------------------------- bezel composer
    def _composer_dialog(self):
        """Composite character/art PNGs onto a border — the way franchise
        bezels are really made: generated frame + official renders."""
        dlg = Toplevel(self.root)
        dlg.title("Bezel composer — put characters & art on a border")
        dlg.configure(bg=BG)
        dlg.geometry("760x560")
        frm = ttk.Frame(dlg, padding=14)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(0, weight=1)

        ttk.Label(frm, text=(
            "HOW TO USE — 1) Load a border: best results with a Border-maker "
            "output (border_*.png in the output\n"
            "folder — transparent center, art on the edges) or any bezel PNG "
            "with a transparent screen hole.\n"
            "2) Add character/art images: transparent-background PNGs "
            "(game/movie renders) work best.\n"
            "3) Place them: side panels for tall characters (size 60–90%), "
            "corners for emblems (20–35%).\n"
            "Nudge sliders fine-tune position. 'Keep screen clear' stops "
            "images from covering the center hole."),
            style="Dim.TLabel", justify="left").grid(row=0, sticky=W)

        # border picker
        borrow = ttk.Frame(frm); borrow.grid(row=1, sticky="ew", pady=(10, 4))
        borrow.columnconfigure(1, weight=1)
        ttk.Button(borrow, text="🖼 Load border…",
                   command=lambda: pick_border()).grid(row=0, column=0)
        self._comp_border = None
        bvar = StringVar(value="none")
        ttk.Label(borrow, textvariable=bvar, style="Dim.TLabel").grid(
            row=0, column=1, sticky=W, padx=8)

        def pick_border():
            p = filedialog.askopenfilename(
                initialdir=str(OUTPUT),
                filetypes=[("PNG with transparency", "*.png")], parent=dlg)
            if p:
                self._comp_border = p
                bvar.set(Path(p).name)

        # image items
        items_frame = ttk.Frame(frm)
        items_frame.grid(row=2, sticky="nsew", pady=4)
        items_frame.columnconfigure(0, weight=1)
        frm.rowconfigure(2, weight=1)
        self._comp_items = []

        def add_item():
            if len(self._comp_items) >= 6:
                return
            p = filedialog.askopenfilename(
                filetypes=[("Images", "*.png;*.webp")], parent=dlg)
            if not p:
                return
            row = ttk.Frame(items_frame)
            row.pack(fill="x", pady=2)
            item = {"path": p, "row": row,
                    "pos": StringVar(value="Left panel"
                                     if len(self._comp_items) % 2 == 0
                                     else "Right panel"),
                    "size": IntVar(value=75), "flip": BooleanVar(value=False),
                    "dx": IntVar(value=0), "dy": IntVar(value=0)}
            ttk.Label(row, text=Path(p).name[:22], width=22,
                      style="Dim.TLabel").pack(side="left")
            ttk.Combobox(row, textvariable=item["pos"], state="readonly",
                         exportselection=False, width=13,
                         values=list(BEZEL_POSITIONS)).pack(side="left",
                                                            padx=3)
            ttk.Label(row, text="size%", style="Dim.TLabel").pack(side="left")
            ttk.Spinbox(row, from_=10, to=100, textvariable=item["size"],
                        exportselection=False, width=4).pack(side="left",
                                                             padx=2)
            ttk.Checkbutton(row, text="flip",
                            variable=item["flip"]).pack(side="left", padx=2)
            ttk.Label(row, text="x±", style="Dim.TLabel").pack(side="left")
            ttk.Spinbox(row, from_=-30, to=30, textvariable=item["dx"],
                        exportselection=False, width=4).pack(side="left")
            ttk.Label(row, text="y±", style="Dim.TLabel").pack(side="left")
            ttk.Spinbox(row, from_=-30, to=30, textvariable=item["dy"],
                        exportselection=False, width=4).pack(side="left")
            ttk.Button(row, text="✕", width=3,
                       command=lambda: remove_item(item)).pack(side="right")
            self._comp_items.append(item)

        def remove_item(item):
            item["row"].destroy()
            self._comp_items.remove(item)

        ttk.Button(frm, text="＋ Add image…", command=add_item).grid(
            row=3, sticky=W, pady=4)

        clip_var = BooleanVar(value=False)
        out_var = StringVar(value="")
        bottom = ttk.Frame(frm); bottom.grid(row=4, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(bottom, text="Keep screen area clear",
                        variable=clip_var).pack(side="left")
        ttk.Label(frm, textvariable=out_var, style="Dim.TLabel",
                  wraplength=700).grid(row=5, sticky=W, pady=(6, 0))

        def build(preview):
            if not self._comp_border:
                out_var.set("Load a border first.")
                return
            if not self._comp_items:
                out_var.set("Add at least one image.")
                return
            try:
                items = [dict(path=i["path"], pos=i["pos"].get(),
                              size=i["size"].get(), flip=i["flip"].get(),
                              dx=i["dx"].get(), dy=i["dy"].get())
                         for i in self._comp_items]
                img = compose_bezel(self._comp_border, items,
                                    clip=clip_var.get())
            except Exception as e:
                out_var.set(f"Compose failed: {e}")
                return
            if preview:
                pv = Toplevel(dlg)
                pv.title("Preview")
                pv.configure(bg=BG)
                disp = img.copy()
                disp.thumbnail((900, 560))
                tkimg = ImageTk.PhotoImage(disp)
                lbl = ttk.Label(pv, image=tkimg)
                lbl.image = tkimg
                lbl.pack(padx=8, pady=8)
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT / f"border_composed_{stamp}.png"
            meta = PngInfo()
            meta.add_text("comic_art_creator", json.dumps(
                {"border": Path(self._comp_border).name,
                 "images": [Path(i["path"]).name for i in items],
                 "tool": "bezel composer"}))
            img.save(path, pnginfo=meta)
            params = dict(model="bezel composer", seed=0)
            self.ui_queue.put(("finished_image", img, params, path))
            out_var.set(f"Saved {path.name} — it's in the gallery and the "
                        "output folder.")

        ttk.Button(bottom, text="👁 Preview",
                   command=lambda: build(True)).pack(side="right", padx=(6, 0))
        ttk.Button(bottom, text="🧩 Compose & save", style="Go.TButton",
                   command=lambda: build(False)).pack(side="right")

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
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
