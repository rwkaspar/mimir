"""Names of the systemd units this app controls.

Single source of truth so a service rename can't drift across call sites.
"""

# The service that owns the display (DRM master + framebuffer). The native
# slideshow (frame/slideshow.py) replaced the old cog/labwc kiosk, so every
# place that used to stop/restart 'photo_frame_cage' to reclaim the display
# (sleep/wake, orientation change, freeing RAM for a transcode) must manage
# THIS unit instead. Starting the old cage service on top of the running
# slideshow brought the old cog/HTML UI back over the native display.
DISPLAY_SERVICE = 'photo_frame_slideshow'

# The e-ink variant has no always-on display daemon; its "display owner" is the
# wake-draw-sleep cycle (frame/eink_cycle.py). Restarting it redraws the panel.
EINK_SERVICE = 'photo_frame_eink'


def variant(config) -> str:
    """Hardware variant from the config: 'lcd' (default) or 'eink'."""
    return 'eink' if (config or {}).get('variant') == 'eink' else 'lcd'


def display_service(config) -> str:
    """The unit that owns the display for this frame's variant."""
    return EINK_SERVICE if variant(config) == 'eink' else DISPLAY_SERVICE
