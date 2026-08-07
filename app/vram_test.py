import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from comic_art_creator import gpu_vram_gb, VRAM_HEADROOM_GB

v = gpu_vram_gb()
print("detected VRAM:", v, "GB")
assert v is not None and v > 30, "5090 should report ~31.8 GB"
for label, size in (("turbo 6.5GB", 6.5), ("flux 16.1GB", 16.1),
                    ("hypothetical 40GB", 40.0)):
    fits = size + VRAM_HEADROOM_GB <= v
    print(f"  {label}: {'green (fits)' if fits else 'greyed (exceeds)'}")
print("OK")
