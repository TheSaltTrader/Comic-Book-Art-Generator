# Comic Book Art Creator — one-shot setup for a fresh machine.
# Installs the engine (ComfyUI), a Python environment, and the starter
# model pack (~36 GB). Requires: git, Python 3.12, an NVIDIA GPU (Blackwell
# supported), and roughly 60 GB of free disk.
#
#   powershell -ExecutionPolicy Bypass -File setup.ps1            # full setup
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -SkipModels # no downloads

param([switch]$SkipModels)

$ErrorActionPreference = "Stop"
$P = $PSScriptRoot

Write-Host "== Comic Book Art Creator setup ==" -ForegroundColor Cyan

# ---- prerequisites -------------------------------------------------------
foreach ($tool in "git", "py") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        Write-Host "MISSING: $tool is not installed or not on PATH." -ForegroundColor Red
        exit 1
    }
}

# ---- engine --------------------------------------------------------------
if (-not (Test-Path "$P\ComfyUI\main.py")) {
    Write-Host "Cloning ComfyUI engine..."
    git clone --depth 1 https://github.com/comfyanonymous/ComfyUI.git "$P\ComfyUI"
}

if (-not (Test-Path "$P\venv\Scripts\python.exe")) {
    Write-Host "Creating Python environment..."
    py -3.12 -m venv "$P\venv"
}
$PY = "$P\venv\Scripts\python.exe"

Write-Host "Installing PyTorch (CUDA 12.8) + engine requirements (this is several GB)..."
& $PY -m pip install --upgrade pip
& $PY -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
& $PY -m pip install -r "$P\ComfyUI\requirements.txt"
& $PY -m pip install rembg onnxruntime websocket-client requests

& $PY -c "import torch; assert torch.cuda.is_available(), 'CUDA not available - check your NVIDIA driver'; print('GPU OK:', torch.cuda.get_device_name(0))"

# ---- folders -------------------------------------------------------------
foreach ($d in "models\checkpoints", "models\loras", "models\vae",
               "models\upscale_models", "output\_raw") {
    New-Item -ItemType Directory -Force (Join-Path $P $d) | Out-Null
}

# ---- models --------------------------------------------------------------
if (-not $SkipModels) {
    $ck = "$P\models\checkpoints"
    $lo = "$P\models\loras"
    $downloads = @(
        @($ck, 'flux1-dev-fp8.safetensors',            'https://huggingface.co/Comfy-Org/flux1-dev/resolve/main/flux1-dev-fp8.safetensors'),
        @($ck, 'Juggernaut-XL-v9.safetensors',          'https://huggingface.co/RunDiffusion/Juggernaut-XL-v9/resolve/main/Juggernaut-XL_v9_RunDiffusionPhoto_v2.safetensors'),
        @($ck, 'DreamShaperXL-Turbo-v2.1.safetensors',  'https://huggingface.co/Lykon/dreamshaper-xl-v2-turbo/resolve/main/DreamShaperXL_Turbo_v2_1.safetensors'),
        @($ck, 'Animagine-XL-4.0.safetensors',          'https://huggingface.co/cagliostrolab/animagine-xl-4.0/resolve/main/animagine-xl-4.0-opt.safetensors'),
        @($lo, 'Flux_RetroComic_v2.safetensors',        'https://huggingface.co/renderartist/retrocomicflux/resolve/main/Retro_Comic_Flux_v2_renderartist.safetensors'),
        @($lo, 'Flux_ArtStyle_XLabs.safetensors',       'https://huggingface.co/XLabs-AI/flux-lora-collection/resolve/main/art_lora_comfy_converted.safetensors'),
        @($lo, 'Flux_Realism_XLabs.safetensors',        'https://huggingface.co/XLabs-AI/flux-lora-collection/resolve/main/realism_lora_comfy_converted.safetensors'),
        @($lo, 'SDXL_GraphicNovel.safetensors',         'https://huggingface.co/blink7630/graphic-novel-illustration/resolve/main/Graphic_Novel_Illustration-000007.safetensors'),
        @($lo, 'SDXL_BW_Manga.safetensors',             'https://huggingface.co/alvdansen/BandW-Manga/resolve/main/BW-000014.safetensors'),
        @($lo, 'SDXL_LineArt_Manga.safetensors',        'https://huggingface.co/artificialguybr/LineAniRedmond-LinearMangaSDXL-V2/resolve/main/LineAniRedmondV2-Lineart-LineAniAF.safetensors'),
        @($lo, 'SDXL_StyleEnhancer_Anime.safetensors',  'https://huggingface.co/Linaqruf/style-enhancer-xl-lora/resolve/main/style-enhancer-xl.safetensors')
    )
    foreach ($d in $downloads) {
        $dest = Join-Path $d[0] $d[1]
        if (Test-Path $dest) { Write-Host "have    $($d[1])"; continue }
        Write-Host "getting $($d[1]) ..."
        curl.exe -SL --retry 5 --retry-delay 10 -C - -o $dest $d[2]
        if ($LASTEXITCODE -ne 0) { Write-Host "FAILED  $($d[1])" -ForegroundColor Red }
    }
}

Write-Host ""
Write-Host "Setup complete. Launch with ComicArtCreator\ComicArtCreator.exe" -ForegroundColor Green
Write-Host "(or 'Start Comic Art Creator.bat'). First start loads a model into"
Write-Host "VRAM and takes a minute."
