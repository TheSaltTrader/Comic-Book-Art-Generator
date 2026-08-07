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

APP_VERSION = "1.7.2"

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


def download_model_update(entry, status_cb, prog_cb=None):
    """Stream one updated model to a temp file, then swap it in place."""
    url = (f"https://huggingface.co/{entry['repo']}/resolve/main/"
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
ANIM_LOOPS = ["Seamless (generated loop — best)",
              "Ping-pong (perfect loop)", "Crossfade (blend ends)", "None"]
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
                         "seed": p["seed"], "steps": 20, "cfg": 5.0,
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

BORDER_TEMPLATE = (
    "epic decorative border frame themed after {theme}, filled with "
    "iconic visual motifs, props, character silhouettes, colors and "
    "symbols of {theme}, richly detailed frame covering all four edges "
    "and corners of the image, themed corner ornaments, completely "
    "textless artwork with no lettering or logos, plain empty solid "
    "dark center panel, high detail")



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

class ChannelQueue:
    """Relabels progress messages so each section drives its own bar."""

    def __init__(self, q, channel):
        self.q, self.channel = q, channel

    def put(self, msg):
        if msg and msg[0] == "progress":
            self.q.put((self.channel,) + tuple(msg[1:]))
        else:
            self.q.put(msg)


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
        if params.get("border_cut") and not params.get("edit_image_names"):
            # prompt-only borders: masked generation keeps the center empty
            w, h = params["width"], params["height"]
            # mask is slightly wider than the final cut so the art runs
            # past the transparency line instead of stopping at it
            inner = int(min(w, h) * min(45, params["border_cut"] + 5) / 100)
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

    def _await_images(self, ws, prompt_id, timeout=600):
        ws.settimeout(timeout)
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
        self.anim_image_path = None  # animator character image
        self.vram_gb = gpu_vram_gb()   # None = no NVIDIA GPU detected
        self._model_fits = {}      # raw name -> fits in VRAM
        self._last_fit_display = ""

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
        s.configure("TCombobox", padding=4)
        s.configure("Fit.TCombobox", padding=4, foreground=ACCENT2)
        s.map("Fit.TCombobox", fieldbackground=[("readonly", BG3)],
              foreground=[("readonly", ACCENT2)])
        s.configure("NoFit.TCombobox", padding=4, foreground="#77778a")
        s.map("NoFit.TCombobox", fieldbackground=[("readonly", BG3)],
              foreground=[("readonly", "#77778a")])
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
        self.editor_var = StringVar(value=list(EDITOR_ENGINES)[0])
        ttk.Combobox(erow, textvariable=self.editor_var, state="readonly",
                     exportselection=False,
                     values=list(EDITOR_ENGINES)).grid(row=0, column=1,
                                                       padx=(4, 0),
                                                       sticky="ew")
        self.editor_canvas_var = BooleanVar(value=False)
        ttk.Checkbutton(left, text="Output at canvas size (re-stage into "
                                   "the size selected below instead of "
                                   "keeping the image's size)",
                        variable=self.editor_canvas_var).grid(
            row=r, sticky=W); r += 1
        self.change_var = DoubleVar(value=60)   # border-ref influence

        # generate
        self.go_btn = ttk.Button(left, text="⚡  GENERATE", style="Go.TButton",
                                 command=self._generate)
        self.go_btn.grid(row=r, sticky=NSEW, pady=(12, 4)); r += 1
        pbrow = ttk.Frame(left); pbrow.grid(row=r, sticky=NSEW, pady=2); r += 1
        pbrow.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(pbrow, mode="determinate")
        self.progress.grid(row=0, column=0, sticky="ew")
        self.pct_var = StringVar(value="")
        ttk.Label(pbrow, textvariable=self.pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))
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
        ttk.Label(left, text="Action (what the character does):",
                  style="Dim.TLabel").grid(row=r, sticky=W); r += 1
        self.anim_prompt_box = self._text(left, 2)
        self.anim_prompt_box.grid(row=r, sticky="ew", pady=(2, 4)); r += 1
        a2 = ttk.Frame(left); a2.grid(row=r, sticky=NSEW, pady=2); r += 1
        ttk.Label(a2, text="Seconds", style="Dim.TLabel").grid(row=0,
                                                               column=0)
        self.anim_secs_var = IntVar(value=2)
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
        ttk.Button(left, text="🎬 Generate animation",
                   command=self._generate_animation).grid(
            row=r, sticky="ew", pady=(4, 2)); r += 1
        apb = ttk.Frame(left); apb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        apb.columnconfigure(0, weight=1)
        self.anim_progress = ttk.Progressbar(apb, mode="determinate")
        self.anim_progress.grid(row=0, column=0, sticky="ew")
        self.anim_pct_var = StringVar(value="")
        ttk.Label(apb, textvariable=self.anim_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))

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
        ttk.Label(left, text="Uses the settings above: model, LoRAs, "
                             "Variations, Steps, Seed and Editor all come "
                             "from the main controls.",
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
        ttk.Button(left, text="⚡ Generate border",
                   command=self._generate_border).grid(row=r, sticky="ew",
                                                       pady=(4, 2)); r += 1
        bpb = ttk.Frame(left); bpb.grid(row=r, sticky=NSEW, pady=(0, 8)); r += 1
        bpb.columnconfigure(0, weight=1)
        self.border_progress = ttk.Progressbar(bpb, mode="determinate")
        self.border_progress.grid(row=0, column=0, sticky="ew")
        self.border_pct_var = StringVar(value="")
        ttk.Label(bpb, textvariable=self.border_pct_var, width=5,
                  style="Dim.TLabel").grid(row=0, column=1, padx=(6, 0))

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
            "editor": self.editor_var.get(),
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
            "anim_gif": self.anim_gif_var.get(),
            "anim_zip": self.anim_zip_var.get(),
            "border_theme": self._get(self.border_prompt_box),
            "border_auto": self.border_auto_var.get(),
            "border_aspect": self.border_aspect_var.get(),
            "border_thick": int(self.border_thick_var.get()),
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
            if st.get("editor") in EDITOR_ENGINES:
                self.editor_var.set(st["editor"])
            self.editor_canvas_var.set(st.get("editor_canvas", False))
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
            self.anim_secs_var.set(st.get("anim_secs", 2))
            if st.get("anim_keep") in ANIM_KEEP:
                self.anim_keep_var.set(st["anim_keep"])
            if st.get("anim_loop") in ANIM_LOOPS:
                self.anim_loop_var.set(st["anim_loop"])
            if st.get("anim_size") in ANIM_SIZES:
                self.anim_size_var.set(st["anim_size"])
            self.anim_transparent_var.set(st.get("anim_transparent", True))
            self.anim_gif_var.set(st.get("anim_gif", True))
            self.anim_zip_var.set(st.get("anim_zip", True))
            self._set(self.border_prompt_box, st.get("border_theme", ""))
            self.border_auto_var.set(st.get("border_auto", True))
            if st.get("border_aspect") in BORDER_SIZES:
                self.border_aspect_var.set(st["border_aspect"])
            self.border_thick_var.set(st.get("border_thick", 14))
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
                    self.lora_strength, self.change_var, self.editor_var,
                    self.editor_canvas_var,
                    self.border_auto_var, self.border_aspect_var,
                    self.border_thick_var, self.anim_secs_var,
                    self.anim_keep_var, self.anim_loop_var,
                    self.anim_size_var, self.anim_transparent_var,
                    self.anim_gif_var, self.anim_zip_var):
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
        ups = check_model_updates()
        eng = check_engine_update()
        if ups or eng:
            self.ui_queue.put(("updates", ups, eng))

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

    def _vram_block_msg(self, raw):
        gb = self._size_gb(raw)
        messagebox.showwarning(
            "Not enough GPU memory",
            f"{raw} is {gb:.1f} GB and needs about "
            f"{gb + VRAM_HEADROOM_GB:.0f} GB of VRAM — your card has "
            f"{self.vram_gb:.1f} GB. Pick a green model instead.")

    def _on_model_pick(self):
        raw = self._model_raw()
        if not self._model_fits.get(raw, True):
            self._vram_block_msg(raw)
            if self._last_fit_display:
                self.model_var.set(self._last_fit_display)
        else:
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

    def _model_fits_vram(self, gb):
        if self.vram_gb is None or not gb:
            return True   # unknown GPU or unknown size: don't block
        return gb + VRAM_HEADROOM_GB <= self.vram_gb

    def _color_model_dropdown(self, dd):
        """Green = fits this GPU, grey = exceeds its memory (Tk popdown)."""
        try:
            pd = self.root.tk.call("ttk::combobox::PopdownWindow", dd)
            lb = f"{pd}.f.l"
            for i, disp in enumerate(dd["values"]):
                raw = self._model_display.get(disp, disp)
                fits = self._model_fits.get(raw, True)
                self.root.tk.call(lb, "itemconfigure", i, "-foreground",
                                  ACCENT2 if fits else "#5a5a6a")
        except Exception:
            pass

    def _update_model_entry_style(self):
        fits = self._model_fits.get(self._model_raw(), True)
        self.model_dd.configure(style="Fit.TCombobox" if fits
                                else "NoFit.TCombobox")

    def _refresh_models(self):
        ckpts = list_checkpoints()
        self._model_display = {}
        self._model_fits = {}
        for name in ckpts:
            gb = self._size_gb(name)
            fits = self._model_fits_vram(gb)
            self._model_fits[name] = fits
            disp = f"{name}  ·  {gb:.1f} GB" if gb else name
            if not fits:
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
            n_fit = sum(1 for f in self._model_fits.values() if f)
            self.vram_note.set(
                f"Your GPU: {self.vram_gb:.1f} GB VRAM — green models fit, "
                f"greyed models exceed it ({n_fit}/{len(ckpts)} usable).")

        cur = self._model_raw()
        if cur not in ckpts:
            last = self.settings.get("last_model")
            cur = last if last in ckpts else ""
            if not cur or not self._model_fits.get(cur, True):
                cur = next((c for c in ckpts if self._model_fits.get(c)),
                           ckpts[0] if ckpts else "")
        for disp, raw in self._model_display.items():
            if raw == cur:
                self.model_var.set(disp)
                if self._model_fits.get(raw, True):
                    self._last_fit_display = disp
                break
        self._update_model_entry_style()
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

    def _busy_guard(self):
        """True = abort the new request. Offers to cancel the running job
        so a stuck/slow generation can't lock the app."""
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
    def _generate(self):
        if self._busy_guard():
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
        editor = EDITOR_ENGINES.get(self.editor_var.get(), "kontext")
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
            if not self._model_fits.get(model, True):
                self._vram_block_msg(model)
                return
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
    def _generate_border(self):
        """Generate a themed 4:3 / 16:9 border frame with a transparent
        center — for overlays, bezels and framing. No text allowed."""
        theme = self._get(self.border_prompt_box)
        if not theme:
            messagebox.showinfo("Border prompt", "Describe the border first "
                                "— a short theme ('haunted forest, gnarled "
                                "branches') or a full precise prompt.")
            return
        if self._busy_guard():
            return
        if not engine_alive():
            messagebox.showerror("Engine", "Engine is not running yet.")
            return
        w, h = BORDER_SIZES[self.border_aspect_var.get()]
        refs = list(self.border_ref_paths)
        editor = EDITOR_ENGINES.get(self.editor_var.get(), "kontext")
        base = BORDER_TEMPLATE.format(theme=theme) \
            if self.border_auto_var.get() else theme
        if refs:
            # references go through the image editor: it redraws them as
            # the border, carrying over style, characters and composition
            if not self._ensure_editor_ready(editor):
                return
            prompt = "redraw this image as " + base
            model = f"editor:{editor}"
            loras = []
        else:
            # mimic the main generation settings: model + LoRAs
            prompt = base
            model = self._model_raw()
            if not model:
                messagebox.showerror("Model", "No model selected in the "
                                              "main controls.")
                return
            if not self._model_fits.get(model, True):
                self._vram_block_msg(model)
                return
            strength = round(self.lora_strength.get(), 2)
            loras = [(n, strength) for n in self._selected_loras()]
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        steps = None if self.steps_var.get() == "auto" \
            else int(self.steps_var.get())
        params = dict(
            prompt=prompt, user_prompt=theme, style="border frame",
            negative=BORDER_NEGATIVE, model=model, loras=loras,
            width=w, height=h, seed=seed, steps=steps, cfg=None,
            batch=max(1, min(10, self.batch_var.get())),
            random_seed=self.random_seed_var.get(),
            transparent=False, preset="border maker",
            border_cut=int(self.border_thick_var.get()),
            ref_images=refs, editor=editor,
            ref_collage_size=(w, h) if refs else None,
            out_size=(w, h) if refs else None)
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

    def _install_editor(self, engine, missing):
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
                    lambda d, t: self.ui_queue.put(("progress", d, t)))
            except Exception as ex:
                self.ui_queue.put(("error", f"download failed: {ex}"))
                return
        self.ui_queue.put(("status", "Editor installed — hit Generate "
                                     "again."))

    def _ensure_editor_ready(self, editor):
        """True when the editor can run now; otherwise guides the user
        (VRAM block, download offer, engine restart) and returns False."""
        need = 16 if editor == "kontext" else 24
        if self.vram_gb is not None and self.vram_gb < need:
            messagebox.showwarning(
                "Not enough GPU memory",
                f"This editor needs about {need} GB of VRAM — your "
                f"card has {self.vram_gb:.1f} GB.")
            return False
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
                                 args=(editor, missing),
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

    def _generate_animation(self):
        if self._busy_guard():
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
        seamless = self.anim_loop_var.get().startswith("Seamless")
        if not self._ensure_editor_ready("wanflf" if seamless else "wan"):
            return
        w, h = ANIM_SIZES[self.anim_size_var.get()]
        secs = max(1, min(5, self.anim_secs_var.get()))
        base_fps = 16 if seamless else 24   # Wan 2.1 FLF is a 16 fps model
        seed = random.randrange(2**32) if self.random_seed_var.get() \
            else int(self.seed_var.get() or 0)
        p = dict(image=self.anim_image_path, action=action, w=w, h=h,
                 length=base_fps * secs + 1, base_fps=base_fps,
                 seamless=seamless,
                 keep_every=ANIM_KEEP.get(self.anim_keep_var.get(), 2),
                 loop=self.anim_loop_var.get(),
                 transparent=self.anim_transparent_var.get(),
                 gif=self.anim_gif_var.get(),
                 zip=self.anim_zip_var.get(), seed=seed)
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
            name = gen._upload_ref(p["image"])
            prompt = (f"{p['action']}. The character performs the action "
                      "smoothly in place, full body visible, flat plain "
                      "solid background, locked camera, no camera "
                      "movement.")
            gp = dict(prompt=prompt, anim_image_name=name, width=p["w"],
                      height=p["h"], length=p["length"], seed=p["seed"])
            graph = build_wan_flf_graph(gp) if p.get("seamless") \
                else build_wan_graph(gp)
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
            if p.get("seamless") and len(frames) > 2:
                frames = frames[:-1]   # last frame == first: drop the dupe
            kept = frames[::p["keep_every"]]

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
                kept = [Image.open(fp).convert("RGBA")
                        for fp in sorted(frames_dir.glob("*.png"))]
                shutil.rmtree(raw_dir, ignore_errors=True)
            else:
                for i, f in enumerate(kept):
                    f.save(frames_dir / f"frame_{i:03d}.png")

            result_path = frames_dir / "frame_000.png"
            if p.get("gif", True):
                looped = kept if p.get("seamless") \
                    else apply_loop(kept, p["loop"])
                fps_out = p.get("base_fps", 24) / p["keep_every"]
                result_path = out_dir / "animation.gif"
                save_gif(looped, result_path, fps_out, p["transparent"])
            if p["zip"]:
                shutil.make_archive(str(out_dir), "zip", out_dir)
            self.ui_queue.put(("finished_image", kept[0].copy(),
                              dict(model="animator", seed=p["seed"]),
                              result_path))
            status(f"Animation done: {len(kept)} frames"
                   + (" + GIF" if p.get("gif", True) else "")
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
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
