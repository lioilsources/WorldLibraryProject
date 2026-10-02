#!/usr/bin/env python3
"""Vyhodí z page cache soubory modelů (posix_fadvise DONTNEED) — bez roota.

Proč: ComfyUI na GB10 vidí jako volnou CUDA paměť jen MemFree, ne MemAvailable.
Page cache po načtení vah (ComfyUI, NIM, vLLM) MemFree sežere, ComfyUI pak
nahraje model „loaded partially; 0.00 MB usable“ a počítá na CPU. 2. 10. 2026
to tak bylo ráno po přepnutí do comfy: page cache 20 GiB, MemFree 20 GiB
(rezerva 8) → CPU render; po tomhle skriptu MemFree 35 GiB a modely celé na GPU.
`drop_caches` chce root, fadvise na čitelné soubory ne. Zahazuje jen čisté
stránky, takže je to bezpečné i za běhu.
"""
import os
import sys

ROOTS = [
    "~/Code/ComfyUI/models",
    "~/deploy/AiStack/cache",
    "~/.cache/huggingface",
    "~/dev/audio/models",
]


def main() -> None:
    seen: set[str] = set()
    files = size = 0
    for root in ROOTS:
        for dirpath, _, names in os.walk(os.path.expanduser(root), followlinks=True):
            for name in names:
                path = os.path.realpath(os.path.join(dirpath, name))
                if path in seen:
                    continue
                seen.add(path)
                try:
                    fd = os.open(path, os.O_RDONLY)
                    try:
                        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
                    finally:
                        os.close(fd)
                    files += 1
                    size += os.path.getsize(path)
                except OSError:
                    pass
    with open("/proc/meminfo") as f:
        free = next(int(l.split()[1]) for l in f if l.startswith("MemFree:")) // 1048576
    print(f"page cache modelů vyhozena: {files} souborů ({size >> 30} GiB), MemFree {free} GiB", file=sys.stderr)


if __name__ == "__main__":
    main()
