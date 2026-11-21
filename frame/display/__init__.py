"""Display backends.

mimir drives two very different kinds of display:

  * ``lcd``  — an HDMI panel. The native slideshow daemon (frame/slideshow.py)
    owns it: photos to the framebuffer, videos through GStreamer/kmssink, a
    touch menu on top. Always on, sleeps at night.
  * ``eink`` — an SPI e-paper panel. Nothing runs between pictures: the frame
    wakes, draws ONE image, arms an RTC alarm and powers off
    (frame/eink_cycle.py). The panel keeps the picture at zero power.

`variant` in the config picks one (frame/services.py). Everything that is not
the display — config, photo sync, the phone UI, the setup screen — is shared.

This package holds the e-paper side: a small ``DisplayBackend`` interface with
one implementation per panel family (frame/display/eink.py), so the cycle does
not care whether it is talking to a Waveshare panel, a Pimoroni Inky or a PNG
file on a development machine.
"""


class DisplayBackend:
    """A panel that shows one still image.

    width/height are the panel's pixel size in LANDSCAPE (long side first);
    `show()` takes an image either that size or its portrait transpose and puts
    it on the panel. `palette` names the colours the panel can show (see
    frame/display/eink.py PALETTES). A backend whose library dithers better than
    we do (calibrated colours) sets `dithers_itself` and is handed the full-colour
    image instead.
    """

    name = 'base'
    width = 0
    height = 0
    palette = 'bw'
    dithers_itself = False

    def show(self, image):
        raise NotImplementedError

    def close(self):
        pass
