"""tiffslide

a somewhat drop-in replacement for openslide-python using tifffile and zarr

"""
from __future__ import annotations

import math
import re
import sys
from types import TracebackType
from typing import Any
from typing import Dict
from typing import Iterator
from typing import Mapping
from typing import Optional
from typing import Tuple
from typing import Type
from typing import Union
from warnings import warn

if sys.version_info[:2] >= (3, 8):
    from functools import cached_property
    from importlib.metadata import version
else:
    from importlib_metadata import version
    # noinspection PyUnresolvedReferences
    from backports.cached_property import cached_property

import zarr
from PIL import Image
from tifffile import TiffFile
from tifffile import TiffFileError as TiffFileError
from tifffile import TiffPageSeries
# noinspection PyProtectedMember
from tifffile.tifffile import svs_description_metadata

from tiffslide._types import PathOrFileLike

__all__ = [
    "PROPERTY_NAME_COMMENT",
    "PROPERTY_NAME_VENDOR",
    "PROPERTY_NAME_QUICKHASH1",
    "PROPERTY_NAME_BACKGROUND_COLOR",
    "PROPERTY_NAME_OBJECTIVE_POWER",
    "PROPERTY_NAME_MPP_X",
    "PROPERTY_NAME_MPP_Y",
    "PROPERTY_NAME_BOUNDS_X",
    "PROPERTY_NAME_BOUNDS_Y",
    "PROPERTY_NAME_BOUNDS_WIDTH",
    "PROPERTY_NAME_BOUNDS_HEIGHT",
    "TiffSlide",
    "TiffFileError",
]

# all relevant tifffile version numbers work with this.
_TIFFFILE_VERSION = tuple(int(x) if x.isdigit() else x for x in version("tifffile").split("."))


# === constants =======================================================

PROPERTY_NAME_COMMENT = u'tiffslide.comment'
PROPERTY_NAME_VENDOR = u'tiffslide.vendor'
PROPERTY_NAME_QUICKHASH1 = u'tiffslide.quickhash-1'
PROPERTY_NAME_BACKGROUND_COLOR = u'tiffslide.background-color'
PROPERTY_NAME_OBJECTIVE_POWER = u'tiffslide.objective-power'
PROPERTY_NAME_MPP_X = u'tiffslide.mpp-x'
PROPERTY_NAME_MPP_Y = u'tiffslide.mpp-y'
PROPERTY_NAME_BOUNDS_X = u'tiffslide.bounds-x'
PROPERTY_NAME_BOUNDS_Y = u'tiffslide.bounds-y'
PROPERTY_NAME_BOUNDS_WIDTH = u'tiffslide.bounds-width'
PROPERTY_NAME_BOUNDS_HEIGHT = u'tiffslide.bounds-height'


# === classes =========================================================

class TiffSlide:
    """
    tifffile backed whole slide image container emulating openslide.OpenSlide
    """

    def __init__(self, filename: PathOrFileLike):
        self.ts_tifffile: TiffFile = TiffFile(filename)  # may raise TiffFileError
        self.ts_filename = filename
        self._zarr_grp: Optional[Union[zarr.core.Array, zarr.hierarchy.Group]] = None
        self._metadata: Optional[Dict[str, Any]] = None

    def __enter__(self) -> TiffSlide:
        return self

    def __exit__(self,
                 exc_type: Optional[Type[BaseException]],
                 exc_val: Optional[BaseException],
                 exc_tb: Optional[TracebackType]) -> None:
        self.close()

    def close(self) -> None:
        if self._zarr_grp:
            try:
                self._zarr_grp.close()
            except AttributeError:
                pass  # Arrays dont need to be closed
            self._zarr_grp = None
        self.ts_tifffile.close()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.ts_filename!r})"

    @classmethod
    def detect_format(cls, filename: PathOrFileLike) -> Optional[str]:
        """return the detected format as a str or None if unknown/unimplemented"""
        _vendor_compat_map = dict(
            svs='aperio',
            # add more when needed
        )
        with TiffFile(filename) as t:
            for prop, vendor in _vendor_compat_map.items():
                if getattr(t, f"is_{prop}"):
                    return vendor
        return None

    @property
    def dimensions(self) -> Tuple[int, int]:
        """return the width and height of level 0"""
        series0 = self.ts_tifffile.series[0]
        assert series0.ndim == 3, "loosen restrictions in future versions"
        h, w, _ = series0.shape
        return w, h

    @property
    def level_count(self) -> int:
        """return the number of levels"""
        return len(self.ts_tifffile.series[0].levels)

    @property
    def level_dimensions(self) -> Tuple[Tuple[int, int], ...]:
        """return the dimensions of levels as a list"""
        return tuple(
            lvl.shape[1::-1]
            for lvl in self.ts_tifffile.series[0].levels
        )

    @property
    def level_downsamples(self) -> Tuple[float, ...]:
        """return the downsampling factors of levels as a list"""
        w0, h0 = self.dimensions
        return tuple(
            math.sqrt((w0*h0) / (w*h))
            for w, h in self.level_dimensions
        )

    @cached_property
    def properties(self) -> Dict[str, Any]:
        """image properties / metadata as a dict"""
        if self._metadata is None:
            aperio_desc = self.ts_tifffile.pages[0].description

            if _TIFFFILE_VERSION >= (2021, 6, 14):
                # tifffile 2021.6.14 fixed the svs parsing.
                _aperio_desc = aperio_desc
                _aperio_recovered_header = None  # no need to recover

            else:
                # this emulates the new description parsing for older versions
                _aperio_desc = re.sub(r';Aperio [^;|]*(?=[|])', '', aperio_desc, count=1)
                _aperio_recovered_header = aperio_desc.split("|", 1)[0]
                assert _aperio_recovered_header.startswith("Aperio"), "please report this bug upstream"

            try:
                aperio_meta = svs_description_metadata(_aperio_desc)
            except ValueError as err:
                if "invalid Aperio image description" in str(err):
                    warn(f"{err} - {self!r}")
                    aperio_meta = {}
                else:
                    raise
            else:
                # Normalize the aperio metadata
                aperio_meta.pop("", None)
                aperio_meta.pop("Aperio Image Library", None)
                if aperio_meta and "Header" not in aperio_meta:
                    aperio_meta["Header"] = _aperio_recovered_header

            md = {
                PROPERTY_NAME_COMMENT: aperio_desc,
                PROPERTY_NAME_VENDOR: "aperio",
                PROPERTY_NAME_QUICKHASH1: None,
                PROPERTY_NAME_BACKGROUND_COLOR: None,
                PROPERTY_NAME_OBJECTIVE_POWER: aperio_meta.get("AppMag", None),
                PROPERTY_NAME_MPP_X: aperio_meta.get("MPP", None),
                PROPERTY_NAME_MPP_Y: aperio_meta.get("MPP", None),
                PROPERTY_NAME_BOUNDS_X: None,
                PROPERTY_NAME_BOUNDS_Y: None,
                PROPERTY_NAME_BOUNDS_WIDTH: None,
                PROPERTY_NAME_BOUNDS_HEIGHT: None,
            }
            md.update({f"aperio.{k}": v for k, v in sorted(aperio_meta.items())})
            for lvl, (ds, (width, height)) in enumerate(zip(
                    self.level_downsamples, self.level_dimensions,
            )):
                page = self.ts_tifffile.series[0].levels[lvl].pages[0]
                md[f"tiffslide.level[{lvl}].downsample"] = ds
                md[f"tiffslide.level[{lvl}].height"] = height
                md[f"tiffslide.level[{lvl}].width"] = width
                md[f"tiffslide.level[{lvl}].tile-height"] = page.tilelength
                md[f"tiffslide.level[{lvl}].tile-width"] = page.tilewidth

            md["tiff.ImageDescription"] = aperio_desc
            self._metadata = md
        return self._metadata

    @cached_property
    def associated_images(self) -> Mapping[str, Image.Image]:
        """return associated images as a mapping of names to PIL images"""
        return _LazyAssociatedImagesDict(self.ts_tifffile)

    def get_best_level_for_downsample(self, downsample: float) -> int:
        """return the best level for a given downsampling factor"""
        if downsample <= 1.0:
            return 0
        for lvl, ds in enumerate(self.level_downsamples):
            if ds >= downsample:
                return lvl - 1
        return self.level_count - 1

    @property
    def ts_zarr_grp(self) -> Union[zarr.core.Array, zarr.hierarchy.Group]:
        """return the tiff image as a zarr array or group

        NOTE: this is extra functionality and not part of the drop-in behaviour
        """
        if self._zarr_grp is None:
            store = self.ts_tifffile.series[0].aszarr()
            self._zarr_grp = zarr.open(store, mode='r')
        return self._zarr_grp

    def read_region(self, location: Tuple[int, int], level: int, size: Tuple[int, int]) -> Image.Image:
        """return the requested region as a PIL.Image

        Parameters
        ----------
        location :
            pixel location (x, y) in level 0 of the image
        level :
            target level used to read the image
        size :
            size (width, height) of the requested region
        """
        base_x, base_y = location
        base_w, base_h = self.dimensions
        level_w, level_h = self.level_dimensions[level]
        level_rw, level_rh = size
        level_rx = (base_x * level_w) // base_w
        level_ry = (base_y * level_h) // base_h
        if isinstance(self.ts_zarr_grp, zarr.core.Array):
            arr = self.ts_zarr_grp[level_ry:level_ry + level_rh, level_rx:level_rx + level_rw]
        else:
            arr = self.ts_zarr_grp[level, level_ry:level_ry + level_rh, level_rx:level_rx + level_rw]
        return Image.fromarray(arr)

    def get_thumbnail(self, size: Tuple[int, int]) -> Image.Image:
        """return the thumbnail of the slide as a PIL.Image with a maximum size"""
        slide_w, slide_h = self.dimensions
        thumb_w, thumb_h = size
        downsample = max(slide_w / thumb_w, slide_h / thumb_h)
        level = self.get_best_level_for_downsample(downsample)
        tile = self.read_region((0, 0), level, self.level_dimensions[level])
        # Apply on solid background
        bg_color = f"#{self.properties[PROPERTY_NAME_BACKGROUND_COLOR] or 'ffffff'}"
        thumb = Image.new('RGB', tile.size, bg_color)
        thumb.paste(tile, None, None)
        thumb.thumbnail(size, Image.ANTIALIAS)
        return thumb


# === internal utility classes ========================================

class _LazyAssociatedImagesDict(Mapping[str, Image.Image]):
    """lazily load associated images"""

    def __init__(self, tifffile: TiffFile):
        series = tifffile.series[1:]
        self._k: Dict[str, TiffPageSeries] = {s.name.lower(): s for s in series}
        self._m: Dict[str, Image.Image] = {}

    def __repr__(self) -> str:
        args = ", ".join(
            f"{name!r}: <lazy-loaded PIL.Image.Image size={s.shape[1]}x{s.shape[0]} ...>"
            for name, s in self._k.items()
        )
        return f"{{{args}}}"

    def __getitem__(self, k: str) -> Image.Image:
        if k in self._m:
            return self._m[k]
        else:
            s = self._k[k]
            self._m[k] = img = Image.fromarray(s.asarray())
            return img

    def __len__(self) -> int:
        return len(self._k)

    def __iter__(self) -> Iterator[str]:
        yield from self._k