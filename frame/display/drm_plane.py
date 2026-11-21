"""A small picture on its own DRM plane, above whatever else is on screen.

Used for the clock while a video plays: the video goes to the display through
kmssink, and the clock sits on a separate hardware plane that the display
controller blends in. That costs no CPU per video frame — unlike blending the
clock into the decoded frames, which forces a full copy of every frame (on a
Pi Zero 2 W: ~0.75 of a core for 1080p30, against ~0.06 for plain playback).

Only one process can drive KMS at a time, so the plane has to live on the same
DRM file descriptor as the video sink: open the card with `open_card()`, hand
the fd to kmssink (`kmssink fd=<n>`), and put the plane on it with `Plane(fd)`.

Plain ctypes on libdrm (which kmssink needs anyway) — no extra packages.
"""
import ctypes
import fcntl
import glob
import logging
import mmap
import os

logger = logging.getLogger(__name__)

_DRM_CLIENT_CAP_UNIVERSAL_PLANES = 2
_DRM_MODE_OBJECT_PLANE = 0xeeeeeeee
_DRM_PLANE_TYPE_OVERLAY = 0
_FOURCC_ARGB8888 = 0x34325241            # 'AR24', premultiplied alpha
_IOCTL_CREATE_DUMB = 0xC02064B2
_IOCTL_MAP_DUMB = 0xC01064B3
_IOCTL_DESTROY_DUMB = 0xC00464B4

_u32, _u64 = ctypes.c_uint32, ctypes.c_uint64


class _PlaneRes(ctypes.Structure):
    _fields_ = [('count_planes', _u32), ('planes', ctypes.POINTER(_u32))]


class _DrmPlane(ctypes.Structure):
    _fields_ = [('count_formats', _u32), ('formats', ctypes.POINTER(_u32)),
                ('plane_id', _u32), ('crtc_id', _u32), ('fb_id', _u32),
                ('crtc_x', _u32), ('crtc_y', _u32), ('x', _u32), ('y', _u32),
                ('possible_crtcs', _u32), ('gamma_size', _u32)]


class _Res(ctypes.Structure):
    _fields_ = [('count_fbs', ctypes.c_int), ('fbs', ctypes.POINTER(_u32)),
                ('count_crtcs', ctypes.c_int), ('crtcs', ctypes.POINTER(_u32)),
                ('count_connectors', ctypes.c_int),
                ('connectors', ctypes.POINTER(_u32)),
                ('count_encoders', ctypes.c_int),
                ('encoders', ctypes.POINTER(_u32)),
                ('min_width', _u32), ('max_width', _u32),
                ('min_height', _u32), ('max_height', _u32)]


class _ObjProps(ctypes.Structure):
    _fields_ = [('count_props', _u32), ('props', ctypes.POINTER(_u32)),
                ('prop_values', ctypes.POINTER(_u64))]


class _Prop(ctypes.Structure):
    _fields_ = [('prop_id', _u32), ('flags', _u32), ('name', ctypes.c_char * 32)]


class _CreateDumb(ctypes.Structure):
    _fields_ = [('height', _u32), ('width', _u32), ('bpp', _u32),
                ('flags', _u32), ('handle', _u32), ('pitch', _u32),
                ('size', _u64)]


class _MapDumb(ctypes.Structure):
    _fields_ = [('handle', _u32), ('pad', _u32), ('offset', _u64)]


class _DestroyDumb(ctypes.Structure):
    _fields_ = [('handle', _u32)]


_lib = None


def _drm():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL('libdrm.so.2', use_errno=True)
        lib.drmModeGetPlaneResources.restype = ctypes.POINTER(_PlaneRes)
        lib.drmModeGetPlane.restype = ctypes.POINTER(_DrmPlane)
        lib.drmModeGetResources.restype = ctypes.POINTER(_Res)
        lib.drmModeObjectGetProperties.restype = ctypes.POINTER(_ObjProps)
        lib.drmModeGetProperty.restype = ctypes.POINTER(_Prop)
        _lib = lib
    return _lib


def open_card():
    """Open the display's DRM device (the card that has planes). Returns an fd
    the caller owns, or None. The first opener becomes DRM master, exactly as
    kmssink would when it opens the card itself."""
    for dev in sorted(glob.glob('/dev/dri/card*')):
        try:
            fd = os.open(dev, os.O_RDWR | os.O_CLOEXEC)
        except OSError:
            continue
        lib = _drm()
        lib.drmSetClientCap(fd, _u64(_DRM_CLIENT_CAP_UNIVERSAL_PLANES), _u64(1))
        res = lib.drmModeGetPlaneResources(fd)
        n = res.contents.count_planes if res else 0
        if res:
            lib.drmModeFreePlaneResources(res)
        if n:
            return fd
        os.close(fd)
    return None


def premultiplied_bgra(image):
    """RGBA PIL image -> bytes in DRM ARGB8888 layout (B,G,R,A in memory) with
    colour premultiplied by alpha, which is what KMS blends by default."""
    import numpy as np
    a = np.asarray(image.convert('RGBA'), dtype=np.uint16)
    alpha = a[:, :, 3:4]
    rgb = (a[:, :, :3] * alpha + 127) // 255
    out = np.concatenate([rgb[:, :, ::-1], alpha], axis=2).astype(np.uint8)
    return out.tobytes()


class Plane:
    """One overlay plane on the active CRTC of `fd`, showing an RGBA image."""

    def __init__(self, fd):
        self.fd = fd
        self._lib = _drm()
        self._buf = None            # (handle, fb_id, mmap, w, h, pitch)
        self.plane_id, self.crtc_id = self._pick()

    def _plane_type(self, plane_id):
        lib = self._lib
        props = lib.drmModeObjectGetProperties(self.fd, plane_id,
                                               _DRM_MODE_OBJECT_PLANE)
        kind = None
        if props:
            for i in range(props.contents.count_props):
                p = lib.drmModeGetProperty(self.fd, props.contents.props[i])
                if p:
                    if p.contents.name == b'type':
                        kind = props.contents.prop_values[i]
                    lib.drmModeFreeProperty(p)
            lib.drmModeFreeObjectProperties(props)
        return kind

    def _pick(self):
        """(free overlay plane, active crtc). The active CRTC is the one a
        plane is already showing something on (the console/photo framebuffer)."""
        lib = self._lib
        res = lib.drmModeGetResources(self.fd)
        if not res:
            raise RuntimeError('no DRM resources')
        crtcs = [res.contents.crtcs[i] for i in range(res.contents.count_crtcs)]
        lib.drmModeFreeResources(res)
        pres = lib.drmModeGetPlaneResources(self.fd)
        if not pres:
            raise RuntimeError('no DRM planes')
        ids = [pres.contents.planes[i] for i in range(pres.contents.count_planes)]
        lib.drmModeFreePlaneResources(pres)
        planes = []
        for pid in ids:
            p = lib.drmModeGetPlane(self.fd, pid)
            if not p:
                continue
            c = p.contents
            planes.append((pid, c.crtc_id, c.possible_crtcs,
                           [c.formats[i] for i in range(c.count_formats)]))
            lib.drmModeFreePlane(p)
        crtc_id = next((c for _, c, _, _ in planes if c), 0)
        if not crtc_id or crtc_id not in crtcs:
            raise RuntimeError('no active CRTC')
        bit = 1 << crtcs.index(crtc_id)
        for pid, in_use, possible, formats in planes:
            if (not in_use and possible & bit and _FOURCC_ARGB8888 in formats
                    and self._plane_type(pid) == _DRM_PLANE_TYPE_OVERLAY):
                return pid, crtc_id
        raise RuntimeError('no free overlay plane')

    def _free_buf(self):
        if not self._buf:
            return
        handle, fb_id, mm = self._buf[:3]
        self._buf = None
        try:
            mm.close()
        except Exception:
            pass
        self._lib.drmModeRmFB(self.fd, fb_id)
        try:
            fcntl.ioctl(self.fd, _IOCTL_DESTROY_DUMB, _DestroyDumb(handle))
        except OSError:
            pass

    def _alloc(self, w, h):
        cd = _CreateDumb(height=h, width=w, bpp=32)
        fcntl.ioctl(self.fd, _IOCTL_CREATE_DUMB, cd)
        handles = (_u32 * 4)(cd.handle, 0, 0, 0)
        pitches = (_u32 * 4)(cd.pitch, 0, 0, 0)
        offsets = (_u32 * 4)(0, 0, 0, 0)
        fb_id = _u32(0)
        if self._lib.drmModeAddFB2(self.fd, w, h, _FOURCC_ARGB8888, handles,
                                   pitches, offsets, ctypes.byref(fb_id), 0):
            fcntl.ioctl(self.fd, _IOCTL_DESTROY_DUMB, _DestroyDumb(cd.handle))
            raise OSError(ctypes.get_errno(), 'drmModeAddFB2 failed')
        md = _MapDumb(handle=cd.handle)
        fcntl.ioctl(self.fd, _IOCTL_MAP_DUMB, md)
        mm = mmap.mmap(self.fd, cd.size, mmap.MAP_SHARED,
                       mmap.PROT_READ | mmap.PROT_WRITE, offset=md.offset)
        self._buf = (cd.handle, fb_id.value, mm, w, h, cd.pitch)

    def show(self, image, x, y):
        """Put `image` (RGBA) on screen with its top-left corner at (x, y).
        Call again to replace it; the old buffer is freed after the switch."""
        w, h = image.size
        data = premultiplied_bgra(image)
        old, self._buf = self._buf, None
        self._alloc(w, h)
        handle, fb_id, mm, _, _, pitch = self._buf
        if pitch == w * 4:
            mm[:len(data)] = data
        else:
            for row in range(h):
                mm[row * pitch:row * pitch + w * 4] = data[row * w * 4:(row + 1) * w * 4]
        if self._lib.drmModeSetPlane(self.fd, self.plane_id, self.crtc_id,
                                     fb_id, 0, int(x), int(y), w, h,
                                     0, 0, w << 16, h << 16):
            err = ctypes.get_errno()
            self._free_buf()
            self._buf = old
            raise OSError(err, 'drmModeSetPlane failed')
        if old:
            new, self._buf = self._buf, old
            self._free_buf()
            self._buf = new

    def close(self):
        """Take the picture off the screen and free everything (not the fd)."""
        try:
            self._lib.drmModeSetPlane(self.fd, self.plane_id, 0, 0, 0,
                                      0, 0, 0, 0, 0, 0, 0, 0)
        except Exception:
            pass
        self._free_buf()
