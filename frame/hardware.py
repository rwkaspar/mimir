"""Hardware capability detection.

Lets the video transcode adapt to the host board: a RAM-constrained Pi
Zero 2W keeps the conservative single-threaded, pre-scaled, screen-freeing
path, while a roomier Pi 4/5 transcodes multi-threaded in one pass at higher
quality without touching the display.
"""
import os
import logging

logger = logging.getLogger(__name__)

# Boards below this much RAM are treated as memory-constrained (Pi Zero 2W
# ~512 MB, Pi 3 ~1 GB). Pi 4/5 have 2–8 GB and clear it comfortably.
LOW_MEMORY_THRESHOLD_MB = 1536


def total_memory_mb() -> int:
    """Total system RAM in MB (conservative 1024 fallback if unreadable)."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError):
        pass
    return 1024


def cpu_count() -> int:
    return os.cpu_count() or 1


def is_low_memory() -> bool:
    return total_memory_mb() < LOW_MEMORY_THRESHOLD_MB


def transcode_profile(full_power: bool = False) -> dict:
    """Return transcode parameters tuned to the host hardware.

    `full_power` means the display is off (we transcode during sleep), so we
    can use every core and normal priority without hurting the UI. When it is
    False the display is up (a rare inline fallback when sleep is disabled),
    so we spare a core, run at low priority, and — on low-memory boards — stop
    the kiosk to free RAM.

    Quality/bitrate/fps and the pre-scale pass depend only on the board's
    class (a Pi Zero can neither decode 1080p60 smoothly nor spare the RAM for
    a one-pass blur-fill of a large source), not on `full_power`.
    """
    mem = total_memory_mb()
    cores = cpu_count()
    low = mem < LOW_MEMORY_THRESHOLD_MB
    if full_power:
        threads = min(cores, 4)       # display off → use all cores
    else:
        threads = 1 if low else max(1, min(cores - 1, 4))  # spare one for UI
    profile = {
        'low_memory': low,
        'mem_mb': mem,
        'cores': cores,
        'full_power': full_power,
        'threads': threads,
        'nice': 0 if full_power else 15,
        'stop_cage': (not full_power) and low,
        'pre_scale': low,      # extra down-scale pass only when memory-bound
        # Ceiling for the pre-scale target so a weak board isn't asked to
        # blur-fill huge frames. The actual target is min(screen, this); for a
        # 1920x1200 panel a Pi Zero caps at 1920 (full quality for that screen)
        # while a hypothetical 4K panel would still be bounded to Full HD.
        'max_pre_scale': 1920 if low else 7680,
        # FINAL output resolution ceiling. Native kiosk plays video directly
        # via v4l2h264dec!kmssink (no browser), which decodes 1920x1200 at
        # realtime with 0 QoS drops and ample CMA/gpu_mem headroom, so we
        # target the panel's full resolution (sharp, fills the screen). The
        # old 1280 cap existed only because the WPE browser decode path was
        # CMA-starved and upscaled — obsolete now. Keep ÷2 for yuv420p.
        'max_output': 1920 if low else 7680,
        # x264 speed preset. NOT 'ultrafast' — that preset disables the
        # in-loop deblocking filter (and scenecut detection), which leaves
        # visible block artifacts at macroblock edges and in motion ("glitch").
        # Since weak boards now transcode during sleep with every core free and
        # at a capped output resolution, 'veryfast' (deblock back on) is well
        # within budget and decodes much cleaner on the Pi HW decoder.
        'preset': 'veryfast' if low else 'fast',
        'crf': 24 if low else 23,
        'maxrate_k': 6000 if low else 8000,
        'bufsize_k': 12000 if low else 16000,
        'fps_cap': 30 if low else 60,
        # H.264 level must admit the output frame size: 1920x1200 (and the
        # 1200x1920 portrait variant) is 9000 macroblocks/frame, which exceeds
        # level 4.2 (MaxFS 8704) — needs 5.0. The Pi HW decoder handles 5.0.
        'level': '5.0' if low else '4.2',
    }
    return profile
