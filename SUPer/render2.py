#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2023 cibo
# This file is part of SUPer <https://github.com/cubicibo/SUPer>.
#
# SUPer is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# SUPer is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with SUPer.  If not, see <http://www.gnu.org/licenses/>.



from typing import Any, TypeVar, Optional, Type, Union
from enum import Enum
from dataclasses import dataclass
from itertools import combinations, chain, zip_longest

from PIL import Image
from SSIM_PIL import compare_ssim
from numpy import typing as npt
import numpy as np

from skimage.filters import gaussian
from skimage.measure import regionprops, label

#%%
from .utils import get_super_logger, Pos, Dim, BDVideo, TimeConv as TC
from .filestreams import BDNXMLEvent, BaseEvent
from .segments import DisplaySet, PCS, WDS, PDS, ODS, ENDS, WindowDefinition, CObject, Epoch
from .optim import Optimise, Preprocess
from .pgraphics import PGraphics, PGDecoder, PGObject, FadeEffect, PGObjectBuffer
from .palette import Palette, PaletteEntry

_Region = TypeVar('Region')
logger = get_super_logger('SUPer')

#%%
@dataclass(frozen=True)
class Box:
    y : int
    dy: int
    x : int
    dx: int

    @property
    def x2(self) -> int:
        return self.x + self.dx

    @property
    def y2(self) -> int:
        return self.y + self.dy

    @property
    def area(self) -> int:
        return self.dx * self.dy

    @property
    def coords(self) -> tuple[Pos, Pos]:
        return (Pos(self.x, self.y), Pos(self.x2, self.y2))

    @property
    def dims(self) -> Dim:
        return Dim(self.dx, self.dy)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.dy, self.dx)

    @property
    def posdim(self) -> tuple[Pos, Dim]:
        return Pos(self.x, self.y), Dim(self.dx, self.dy)

    @property
    def slice(self) -> tuple[slice]:
        return (slice(self.y, self.y+self.dy),
                slice(self.x, self.x+self.dx))

    @property
    def slice_x(self) -> slice:
        return slice(self.x, self.x+self.dx)

    @property
    def slice_y(self) -> slice:
        return slice(self.y, self.y+self.dy)

    def overlap_with(self, other) -> float:
        intersect = __class__.intersect(self, other)
        return intersect.area/min(self.area, other.area)

    @classmethod
    def intersect(cls, *box) -> 'Box':
        x2 = min(map(lambda b: b.x2, box))
        y2 = min(map(lambda b: b.y2, box))
        x1 = max(map(lambda b: b.x, box))
        y1 = max(map(lambda b: b.y, box))
        dx, dy = (x2-x1), (y2-y1)
        return cls(y1, dy * bool(dy > 0), x1, dx * bool(dx > 0))

    @classmethod
    def from_region(cls, region: _Region) -> 'Box':
        return cls.from_slices(region.slice)

    @classmethod
    def from_slices(cls, slices: tuple[slice]) -> 'Box':
        if len(slices) == 3:
            slyx = slices[1:]
        else:
            slyx = slices
        f_ZWz = lambda slz : (int(slz.start), int(slz.stop-slz.start))
        return cls(*f_ZWz(slyx[0]), *f_ZWz(slyx[1]))

    @classmethod
    def from_hulls(cls, *hulls: list[...]) -> 'Box':
        final_hull = cls(*([None]*4))
        for hull in hulls:
            final_hull
            raise NotImplementedError
        return final_hull

    @classmethod
    def union(cls, *box) -> 'Box':
        x2 = max(map(lambda b: b.x2, box))
        y2 = max(map(lambda b: b.y2, box))
        x1 = min(map(lambda b: b.x, box))
        y1 = min(map(lambda b: b.y, box))
        return cls(y1, y2-y1, x1, x2-x1)

    @classmethod
    def from_events(cls, events: list[BaseEvent]) -> 'Box':
        """
        From a chain of event, find the "working box" to minimise
        memory usage of the buffers while optimising.
        """
        if len(events) == 0:
            raise ValueError("No events given.")

        pxtl, pytl = np.inf, np.inf
        pxbr, pybr = 0, 0
        for event in events:
            pxtl = min(pxtl, event.x)
            pxbr = max(pxbr, event.x + event.width)
            pytl = min(pytl, event.y)
            pybr = max(pybr, event.y + event.height)
        return cls(int(pytl), int(pybr-pytl), int(pxtl), int(pxbr-pxtl))

    @classmethod
    def from_coords(cls, x1: int, y1: int, x2 : int, y2: int) -> 'Box':
        return cls(min(y1, y2), abs(y2-y1), min(x1, x2), abs(x2-x1))
#%%
####
@dataclass(frozen=True)
class ScreenRegion(Box):
    t:  int
    dt: int
    region: _Region

    @classmethod
    def from_slices(cls, slices: tuple[slice], region: Optional[_Region] = None) -> 'ScreenRegion':
        f_ZWz = lambda slz : (int(slz.start), int(slz.stop-slz.start))
        X, Y, T = f_ZWz(slices[2]), f_ZWz(slices[1]), f_ZWz(slices[0])

        if len(slices) != 3:
            raise ValueError("Expected 3 slices (t, y, x).")
        return cls(*Y, *X, *T, region)

    @property
    def temporal_slice(self) -> slice:
        return slice(self.t, self.t2)

    @property
    def temporal_range(self) -> range:
        return range(self.t, self.t2)

    @property
    def spatial_slice(self) -> tuple[slice]:
        return (slice(self.y, self.y2),
                slice(self.x, self.x2))

    @property
    def slice(self) -> tuple[slice]:
        return (slice(self.t, self.t2),
                slice(self.y, self.y2),
                slice(self.x, self.x2))

    @property
    def range(self) -> tuple[range]:
        return (range(self.t, self.t2),
                range(self.y, self.y2),
                range(self.x, self.x2))

    @property
    def t2(self) -> int:
        return self.t + self.dt

    @classmethod
    def from_region(cls, region: _Region) -> 'ScreenRegion':
        return cls.from_slices(region.slice, region)

    @classmethod
    def from_coords(cls, x1: int, y1: int, t1: int, x2: int, y2: int, t2: int, region: _Region) -> 'ScreenRegion':
        return cls(min(y1, y2), abs(y2-y1), min(x1, x2), abs(x2-x1), min(t1, t2), abs(t2-t1), region=region)
####

class WindowOnBuffer:
    def __init__(self, screen_regions: list[ScreenRegion], duration: int) -> None:
        self.srs = screen_regions
        self.duration = duration

    def bitmap_update_mask(self,
           main_box: Box,
           overlap_threshold: float = 0
        ) -> npt.NDArray[np.uint16]:
        """
        Find pixel collisions of different screen areas. Areas that don't collide can
        be optimised on the same bitmap without any visual artifact.
        """
        if not (0 <= overlap_threshold <= 1):
            raise ValueError(f"Overlap threshold not within [0;1], got '{overlap_threshold}'")

        update_mask = np.zeros(self.duration, np.uint8)
        buffer = np.zeros(main_box.shape, dtype=np.uint8)

        #we want to have the time of appearance in order
        srs = sorted(self.srs, key=lambda sr: sr.t)
        active_until = -1
        for ctime in range(self.duration):
            for sr in srs:
                if ctime not in sr.temporal_range:
                    continue
                percentage = np.sum(buffer[sr.spatial_slice] & sr.region.image[ctime-sr.t])/np.sum(sr.region.image[ctime-sr.t])
                if (sr.t > active_until or percentage >= overlap_threshold):
                    update_mask[ctime] = 1
                    buffer *= 0
                active_until = max(active_until, sr.t + sr.dt)
                buffer[sr.spatial_slice] |= sr.region.image[ctime-sr.t]
        return update_mask

    def event_mask(self, boolean: bool = True) -> npt.NDArray[np.uint8]:
        """
        event mask defines the times during which the window displays a composition.
        When zero, the window is just fully transparent, without any composition obj.
        """
        mask = np.zeros(self.duration, dtype=np.uint16)
        if boolean:
            for sr in self.srs:
                mask[sr.temporal_slice] = 1
        else:
            for sr in self.srs:
                mask[sr.temporal_slice] += 1
        return mask

    def get_window(self) -> Box:
        mxy = np.asarray([np.inf, np.inf])
        Mxy = np.asarray([-1, -1])
        for sr in self.srs:
            mxy[:] = np.min([np.asarray((sr.y,  sr.x)),  mxy], axis=0)
            Mxy[:] = np.max([np.asarray((sr.y2, sr.x2)), Mxy], axis=0)
        mxy, Mxy = np.uint32((mxy, Mxy))
        return Box(mxy[0], max(Mxy[0]-mxy[0], 8), mxy[1], max(Mxy[1]-mxy[1], 8))

    def area(self) -> int:
        return self.get_window().area

    def update_mask(self, boolean: bool = True) -> npt.NDArray[np.uint16]:
        """
        Update mask defines roughly when the buffer associated to the window should
        be updated. This is likely to catch false positives, we have to filter them.
        """
        mask = np.zeros((self.duration,), dtype=np.uint16)
        assert_str = "Caught an empty event."

        if boolean:
            for sr in self.srs:
                assert sr.dt > 0, assert_str
                # the event shows up at sr.t
                mask[sr.t] = 1
        else:
            #Usable to filter times when an update is needed and what not.
            for sr in self.srs:
                assert sr.dt > 0, assert_str
                mask[sr.t] += 1
#%%

class PGConvert:
    def __init__(self, windows: dict[int, WindowOnBuffer], events: list[Type[BaseEvent]]) -> None:
        assert type(windows) is dict
        self.windows = windows

    def render_single(self, box: Box, event: Type[BaseEvent], window_id: int) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.uint8]]:
        assert window_id in self.windows, "Unknown window ID."

        bitmap, palette, padding = self._quantize_adapt_palette(event.img)

        bitmap_box = padding*np.ones(box.shape, dtype=np.uint8)
        bitmap_box[event.y-box.y:event.y-box.y+event.height, event.x-box.x:event.x-box.x+event.width] = bitmap

        return palette, bitmap_box[self.windows[window_id].get_window().slice]

    @staticmethod
    def _find_most_transparent(palette: dict[tuple[int], int]) -> tuple[npt.NDArray[np.uint8], Optional[int]]:
        padding_entry = None
        min_alpha = (256, None)
        if len(palette) >= 256:
            assert len(palette) == 256, "More than 256 colors."
            for entry, k in palette.items():
                if entry[-1] == 0 and padding_entry is None:
                    padding_entry = k
                if min_alpha[0] < entry[-1]:
                    min_alpha = (entry[-1], k)
            if padding_entry is None:
                return None, None #Do another turn with 255 colors
        else:
            if (0, 0, 0, 0) in palette:
                padding_entry = palette[(0, 0, 0, 0)]
            else:
                padding_entry = len(palette)
                palette[(0, 0, 0, 0)] = padding_entry
        return np.asarray(list(palette.keys()), dtype=np.uint8), padding_entry

    def _quantize_adapt_palette(self, img) -> tuple[npt.NDArray[np.uint8], npt.NDArray[np.uint8], int]:
        padding, k = None, 0
        while padding is None:
            bitmap, palette = Preprocess.quantize(img, 256-k, kmeans_fade=self.kwargs.get('kmeans_fade', False),
                                                  kmeans_quant=self.kwargs.get('kmeans_quant', False))
            palette, padding = __class__._find_most_transparent(palette)
            k+=1
        return bitmap, palette, padding

    def render_multiple(self,
          box: Box,
          event: Type[BaseEvent],
        ) -> tuple[npt.NDArray[np.uint8], tuple[npt.NDArray[np.uint8]]]:
        """
        Essentially find the PDS and CLU-bitmaps when 2+ compositions
        are on screen simultaneously (they share the same palette)
        """
        bitmap, palette, padding = self._quantize_adapt_palette(event.img)

        bitmap_box = padding*np.ones(box.shape, dtype=np.uint8)
        bitmap_box[event.y-box.y:event.y-box.y+event.height, event.x-box.x:event.x-box.x+event.width] = bitmap

        imgs = [bitmap_box[wob.get_window().slice] for wob in self.windows.values()]
        return (palette, tuple(imgs))

#%%
class GroupingEngine:
    class Mode(Enum):
        SMALLEST_WINDOWS = 'area'
        LEAST_ACQUISITIONS = 'acq'
        SPECIAL_EFFECTS = 'special'

        def __eq__(self, other: Any) -> bool:
            if isinstance(self, self.__class__):
                return self.value == other.value
            if isinstance(other, str):
                return self.value == other.lower()
            return NotImplemented

    def __init__(self, n_groups: int = 2, **kwargs) -> None:
        if n_groups not in range(1, 3):
            raise AssertionError(f"GroupingEngine expects 1 or 2 groups, not '{n_groups}'.")

        self.n_groups = n_groups
        self.candidates = kwargs.pop('candidates', 25)
        self.mode = kwargs.pop('mode', __class__.Mode.SMALLEST_WINDOWS)

        self.no_blur = True
        self.blur_mul = kwargs.pop('blur_mul', 1.1)
        self.blur_c = kwargs.pop('blur_const', 1.5)

        self.kwargs = kwargs

    def coarse_grouping(self, group: list[Type[BaseEvent]]) -> tuple[_Region, npt.NDArray[np.uint8], Box]:
        # SD content should be blurred with lower coeffs. Remove constant.
        blur_mul = self.blur_mul
        blur_c = self.blur_c
        if self.no_blur:
            blur_c = self.kwargs.get('noblur_bc_c', 0.0)
            blur_mul = self.kwargs.get('noblur_bm_c', 1.0)

        box = Box.from_events(group)
        (pxtl, pytl), (w, h) = box.posdim
        ratio_woh = abs(w/h)
        ratio_how = 1/ratio_woh if 1/ratio_woh <= 1 else 1
        ratio_woh = ratio_woh if ratio_woh <= 1.3 else 1.3

        ne_imgs = []
        for event in group:
            imgg = np.asarray(event.img.getchannel('A'), dtype=np.uint8)
            img_blurred = (255*gaussian(imgg, (blur_c + blur_mul*ratio_how, blur_c + blur_mul*ratio_woh)))
            img_blurred[img_blurred <= 0.25] = 0
            img_blurred[img_blurred > 0.25] = 1
            ne_imgs.append(img_blurred)

        gs_graph = np.zeros((len(group), h, w), dtype=np.uint8)
        gs_orig = np.zeros((len(group), h, w), dtype=np.uint8)
        for k, (event, b_img) in enumerate(zip(group, ne_imgs)):
            slice_x = slice(event.x-pxtl, event.x-pxtl+event.width)
            slice_y = slice(event.y-pytl, event.y-pytl+event.height)
            gs_graph[k, slice_y, slice_x] = b_img.astype(np.uint8)
            gs_orig[k, slice_y, slice_x] = np.array(event.img.getchannel('A'))
        return regionprops(label(gs_graph)), gs_orig, box

    def group_and_sort_flat(self, tbox: list[ScreenRegion], box: Box, duration: int) -> Optional[list[tuple[WindowOnBuffer]]]:
        screen = np.zeros((box.dy, box.dx), np.uint16)
        for sr in tbox:
            screen[sr.spatial_slice] = 1

        labeled_screen = label(screen)
        regions = regionprops(labeled_screen)

        if len(regions) == 1:
            return [(WindowOnBuffer(tbox, duration),)]

        # Too many regions, increase blurring and try again
        if len(regions) > 16:
            return None

        new_tbox = [[] for reg in regions]
        fake_srs = []
        for sr in tbox:
            id_owner = labeled_screen[sr.spatial_slice][0,0]-1
            new_tbox[id_owner].append(sr)

        for k, (region, ntb) in enumerate(zip(regions, new_tbox)):
            region.slice = (slice(min(map(lambda x: x.t, ntb)), max(map(lambda x: x.t2, ntb))), *region.slice)
            region._SUPer_rid = k
            fake_srs.append(ScreenRegion.from_region(region))

        results = self.group_and_sort(fake_srs, duration)

        #Inject back the original ScreenRegions
        for res in results:
            for w in res:
                new_sr = []
                for sr in w.srs:
                    new_sr.extend(new_tbox[sr.region._SUPer_rid])
                w.srs = new_sr
        return results

    @staticmethod
    def _get_combinations(n_regions: int) -> map:
        #If we have two composition objects, we want to find out the smallest 2 areas
        # that englobes all the screen regions. We generate all possible arrangement
        region_ids = range(n_regions)
        arrangements = map(lambda combination: set(filter(lambda region_id: region_id >= 0, combination)),
                                   set(combinations(list(region_ids) + [-1]*(n_regions-2), n_regions-1)))
        return arrangements

    def group_and_sort(self, srs: list[ScreenRegion], duration: int) -> list[tuple[WindowOnBuffer]]:
        """
        Seek for minimum areas from the regions, sort them and return them sorted,
        ascending area size. The caller will then choose the best area.
        This function performs an expensive 3D search, only suited for len(srs) <= 16
        """
        windows, areas = {}, {}
        n_regions = len(srs)

        if n_regions == 1 or self.n_groups == 1:
            return [(WindowOnBuffer(srs, duration=duration),)]

        for key, arrangement in enumerate(__class__._get_combinations(n_regions)):
            arr_sr, other_sr = [], []
            for k, sr in enumerate(srs):
                (arr_sr if k in arrangement else other_sr).append(sr)
            windows[key] = (WindowOnBuffer(arr_sr, duration=duration), WindowOnBuffer(other_sr, duration=duration))
            areas[key] = sum(map(lambda wb: wb.area(), windows[key]))

        #Here, we can sort by ascending area – first has the smallest windows
        return [windows[k] for k, _ in sorted(areas.items(), key=lambda x: x[1])]

    def group(self, subgroup: list[Type[BaseEvent]]) -> tuple[list[tuple[WindowOnBuffer]], Box]:
        cls = self.__class__

        trials = 15
        wobs = None
        while trials > 0 and wobs is None:
            trials -= 1
            regions, gs_origs, box = self.coarse_grouping(subgroup)

            tbox = []
            for region in regions:
                region.slice = cls.crop_region(region, gs_origs)
                tbox.append(ScreenRegion.from_region(region))

            wobs = self.group_and_sort_flat(tbox, box, len(subgroup))
            if wobs is None:
                self.no_blur = False
                self.blur_mul += 0.5
                self.blur_c += 0.5
        if wobs is None:
            logger.warning("Grouping Engine giving up optimising layout. Using a single window.")
            wobs = [(WindowOnBuffer(tbox, duration=len(subgroup)),)]
        return self.select_best_wob(wobs, box), box

    def select_best_wob(self, wobs: list[tuple[WindowOnBuffer]], box: Box) -> tuple[WindowOnBuffer]:
        """
        This function has three mode, depending of the config mode:
            - return the pair of WOBs with the minimum area
            - return the pair of WOBs with the least amount of acquisition (rough est)
            - TODOs: the pair of WOBs that would separate best special effects.

        :param wobs: list of wobs pair
        :param box: box containing all wobs
        :return: list of wobs pair ordered ascendingly by total refreshed area.
        """
        if self.mode == __class__.Mode.SPECIAL_EFFECTS:
            logger.warning("Not implemented yet, returning min area.")
        if self.mode in [__class__.Mode.SPECIAL_EFFECTS, __class__.Mode.SMALLEST_WINDOWS]:
            return tuple(sorted(wobs[0], key=lambda x: x.srs[0].t))
        elif self.mode == __class__.Mode.LEAST_ACQUISITIONS:
            scores = []
            #wobs is a list of pairs of wob
            for wobp in wobs[:self.candidates]:
                area_refreshed = 0
                for wob in wobp:
                    mask = wob.bitmap_update_mask(box)
                    area_refreshed += wob.area()*np.sum(mask)
                scores.append(area_refreshed)
            return next(map(lambda ws: ws[0], sorted(zip(wobs, scores), key=lambda ws : ws[1])))
        else:
            raise NotImplementedError("Unknown grouping choice mode.")

    @staticmethod
    def crop_region(region: _Region, gs_origs: npt.NDArray[np.uint8]) -> _Region:
        #Mask out object outside of the active region.
        gs_origs = gs_origs.copy()
        #Apply blurred mask  so we don't catch nearby graphics by working with just rectangles
        gs_origs[region.slice] &= region.image

        cntXl = 0
        while np.all(gs_origs[region.slice[0], region.slice[1],
                              region.slice[2].start+cntXl:region.slice[2].start+1+cntXl] == 0):
            cntXl += 1
        cntXr = -1
        while np.all(gs_origs[region.slice[0], region.slice[1],
                              region.slice[2].stop+cntXr:region.slice[2].stop+1+cntXr] == 0):
            cntXr -= 1
        cntXr += 1
        cntYt = 0
        while np.all(gs_origs[region.slice[0], region.slice[1].start+cntYt:region.slice[1].start+cntYt+1,
                              region.slice[2]] == 0):
            cntYt += 1

        cntYb = -1
        while np.all(gs_origs[region.slice[0], region.slice[1].stop+cntYb:region.slice[1].stop+cntYb+1,
                              region.slice[2]] == 0):
            cntYb -= 1
        cntYb += 1

        f_region = tuple([region.slice[0],
                      slice(region.slice[1].start+cntYt, region.slice[1].stop+cntYb),
                      slice(region.slice[2].start+cntXl, region.slice[2].stop+cntXr)])

        # Refine image mask, this is a bit hacky as we modify the internal variable
        #(but it is what is returned by the .image property so we're good)
        region._cache['image'] = gs_origs[f_region] != 0
        return f_region

#%%
class WOBSAnalyzer:
    def __init__(self, wobs: tuple[WindowOnBuffer], events: list[BDNXMLEvent], box: Box, fps: Union[float, int], bdn: ..., **kwargs):
        self.wobs = wobs
        self.events = events
        self.box = box
        self.target_fps = fps
        self.bdn = bdn
        self.kwargs = kwargs

    def mask_event(self, window, event) -> Optional[npt.NDArray[np.uint8]]:
        if event is not None:
            #+8 for minimum object width and height
            work_plane = np.zeros((self.box.dy+8, self.box.dx+8, 4), dtype=np.uint8)

            hsi = slice(event.x-self.box.x, event.x-self.box.x+event.width)
            vsi = slice(event.y-self.box.y, event.y-self.box.y+event.height)
            work_plane[vsi, hsi, :] = np.array(event.img, dtype=np.uint8)
            event.unload() #Help a bit to save on RAM

            return work_plane[window.y:window.y2, window.x:window.x2, :]
        return None

    def analyze(self):
        woba = []

        #Init
        gens, windows = [], []
        for k, swob in enumerate(self.wobs):
            woba.append(WOBAnalyzer(swob))
            windows.append(swob.get_window())
            gens.append(woba[k].analyze())
            next(gens[-1])

        #get all windowed bitmaps
        pgobjs = [[] for k in range(len(windows))]
        for event in chain(self.events, [None]*2):
            for wid, (window, gen) in enumerate(zip(windows, gens)):
                try:
                    pgobj = gen.send(self.mask_event(window,  event))
                except StopIteration:
                    pgobj = None
                if pgobj is not None:
                    pgobjs[wid].append(pgobj)
        pgobjs_proc = [objs.copy() for objs in pgobjs]

        acqs, absolutes, margins, durs = self.find_acqs(pgobjs_proc, windows)
        states = [PCS.CompositionState.NORMAL] * len(acqs)
        states[0] = PCS.CompositionState.EPOCH_START
        drought = 0

        thresh = self.kwargs.get('quality_factor', 0.8)
        dthresh = self.kwargs.get('dquality_factor', 0.035)
        refresh_rate = max(0, min(self.kwargs.get('refresh_rate', 1.0), 1.0))

        for k, (acq, forced, margin) in enumerate(zip(acqs[1:], absolutes[1:], margins[1:]), 1):
            if forced or (acq and margin > max(thresh-dthresh*drought, 0)):
                states[k] = PCS.CompositionState.ACQUISITION
                drought = 0
            else:
                #try to not do too many acquisitions, as we want to compress the stream.
                drought += 1*refresh_rate
        return self._convert(states, pgobjs, windows, durs)

    def _convert(self, states, pgobjs, windows, durs):
        event_mask = self.wobs[0].event_mask()
        for wb in self.wobs[1:]:
            event_mask += wb.event_mask()

        wd_base = [WindowDefinition.from_scratch(k, w.x+self.box.x, w.y+self.box.y, w.dx, w.dy) for k, w in enumerate(windows)]
        wds_base = WDS.from_scratch(wd_base, pts=0.0)
        n_actions = len(event_mask)
        displaysets = []

        def get_obj(frame, pgobjs: dict[int, list[PGObject]]) -> dict[int, Optional[PGObject]]:
            objs = {k: None for k, objs in enumerate(pgobjs)}

            # TODO: add support for 2 objects on one window
            for wid, pgobj in enumerate(pgobjs):
                for obj in pgobj:
                    if obj.is_active(frame):
                        objs[wid] = obj
            return objs

        def get_pts(c_pts: float) -> float:
            #Set PTS a few ticks before the real timestamp so we swap the graphic plane
            # on time for the frame it is supposed to be on screen !
            return c_pts - 4/90e3

        def get_undisplay(self, i: int, pcs_id: int, wds_base: WDS, palette_id: int) -> DisplaySet:
            c_pts = get_pts(TC.tc2s(self.events[i].tc_out, self.bdn.fps))
            pcs = pcs_fn(pcs_id, PCS.CompositionState.NORMAL, False, palette_id, [], c_pts)
            wds = WDS(bytes(wds_base))
            wds.pts = c_pts
            return DisplaySet([pcs, wds, ENDS.from_scratch(pts=c_pts)])

        def box_to_crop(box: Box) -> dict['str', int]:
            return {'hc_pos': box.x, 'vc_pos': box.y, 'c_w': box.dx, 'c_h': box.dy}

        double_buffering = 2
        i = 0
        ods_reg = [0]*64
        pcs_id = 0
        pal_vn = 0
        c_pts = 0
        pal_id = 0

        pcs_fn = lambda pcs_cnt, state, pal_flag, palette_id, cl, pts:\
                    PCS.from_scratch(*self.bdn.format.value, BDVideo.LUT_PCS_FPS[round(self.target_fps, 3)], pcs_cnt & 0xFFFF, state, pal_flag, palette_id, cl, pts=pts)

        kwargs = self.kwargs.copy()
        kwargs.pop('colors')

        is_compat_mode = kwargs.pop('pgs_compatibility', False)

        try:
            use_pbar = False
            from tqdm import tqdm
        except ModuleNotFoundError:
            from contextlib import nullcontext as tqdm
        else:
            use_pbar = True

        pbar = tqdm(range(n_actions))
        while i < n_actions:
            for k in range(i+1, n_actions+1):
                if k == n_actions or states[k] != PCS.CompositionState.NORMAL:
                    break
            assert k > i

            if durs[i][1] != 0:
                assert i > 0
                displaysets.append(get_undisplay(self, i-1, pcs_id, wds_base, pal_id))
                pcs_id += 1

            c_pts = get_pts(TC.tc2s(self.events[i].tc_in, self.bdn.fps))

            res, pals, o_ods, cobjs, off_screen, cobjs_cropped = [], [], [], [], [], []
            pgobs_items = get_obj(i, pgobjs).items()
            has_two_objs = 0
            for wid, pgo in pgobs_items:
                if pgo is None:
                    continue
                if not np.any(pgo.mask[i-pgo.f:k-pgo.f]):
                    continue
                has_two_objs += 1
            has_two_objs = has_two_objs > 1

            double_buffering = abs(double_buffering - 2)

            for wid, pgo in pgobs_items:
                oid = wid + double_buffering
                if pgo is None:
                    continue
                if not np.any(pgo.mask[i-pgo.f:k-pgo.f]):
                    continue
                imgs_chain = [Image.fromarray(img) for img in pgo.gfx[i-pgo.f:k-pgo.f]]
                # imgs_chain = imgs_chain[:np.max(np.nonzero(pgo.mask[i-pgo.f:k-pgo.f]))+1]
                # if len(imgs_chain) == 0:
                #     logger.warning("Found an empty object, filtering it out.")
                #     continue
                off_screen.append(i+np.max(np.nonzero(pgo.mask[i-pgo.f:k-pgo.f])))
                cobjs.append(CObject.from_scratch(oid, wid, windows[wid].x+self.box.x, windows[wid].y+self.box.y, False))

                cparams = box_to_crop(pgo.box)
                cobjs_cropped.append(CObject.from_scratch(oid, wid, windows[wid].x+self.box.x+cparams['hc_pos'], windows[wid].y+self.box.y+cparams['vc_pos'], False,
                                                              cropped=True, **cparams))
                res.append(Optimise.solve_sequence_fast(imgs_chain, 128 if has_two_objs else 256, **kwargs))
                pals.append(Optimise.diff_cluts(res[-1][1], matrix=self.kwargs.get('bt_colorspace', 'bt709')))

                ods_data = PGraphics.encode_rle(res[-1][0] + 128*(wid == 1 and has_two_objs))
                o_ods += ODS.from_scratch(oid, ods_reg[oid] & 0xFF, res[-1][0].shape[1], res[-1][0].shape[0], ods_data, pts=c_pts)
                ods_reg[oid] += 1
            ####

            if len(pals) == 0:
                displaysets.append(get_undisplay(self, i-1, pcs_id, wds_base, pal_id))
                pcs_id += 1
                i = k
                continue

            pal = pals[0][0]
            if has_two_objs:
                for p in pals[1]:
                    p.offset(128)
                pal |= pals[1][0]
            else:
                pals.append([Palette()] * len(pals[0]))
                off_screen.append(np.inf)

            wds = WDS(bytes(wds_base))
            wds.pts = c_pts

            pds = PDS.from_scratch(pal, p_vn=pal_vn & 0xFF, p_id=pal_id, pts=c_pts)
            pcs = pcs_fn(pcs_id, states[i], False, pal_id, cobjs if is_compat_mode else cobjs_cropped, c_pts)
            ends = ENDS.from_scratch(pts=c_pts)
            displaysets.append(DisplaySet([pcs, wds, pds] + o_ods + [ends]))

            next_pal_full = False
            pcs_id += 1
            pal_vn += 1
            if pal_vn >= 256:
                pal_id = (pal_id + 1) & 0b111
                pal_vn = 0
                next_pal_full = True

            if len(pals[0]) > 1:
                zip_length = max(map(len, pals))
                if off_screen[0] != np.inf and len(pals[0]) < zip_length:
                    pals[0] += [Palette({k: PaletteEntry(16, 128, 128, 0) for k in range(128*(cobjs[0].o_id & 0x01), 128*((cobjs[0].o_id & 0x01)+1))})]
                if off_screen[1] != np.inf and len(pals[1]) < zip_length:
                    pals[1] += [Palette({k: PaletteEntry(16, 128, 128, 0) for k in range(128*(cobjs[1].o_id & 0x01), 128*((cobjs[1].o_id & 0x01)+1))})]

                for z, (p1, p2) in enumerate(zip_longest(pals[0][1:], pals[1][1:], fillvalue=Palette()), i+1):
                    c_pts = get_pts(TC.tc2s(self.events[z].tc_in, self.bdn.fps))
                    pal |= (p1 | p2)
                    assert states[z] == PCS.CompositionState.NORMAL

                    #Is there a know screen clear in the chain? then use palette screen clear here
                    if durs[z][1] != 0:
                        c_pts_und = get_pts(TC.tc2s(self.events[z-1].tc_out, self.bdn.fps))
                        pcs = pcs_fn(pcs_id, states[z], True, pal_id, cobjs if is_compat_mode else cobjs_cropped, c_pts_und)
                        pds = PDS.from_scratch(Palette({k: PaletteEntry(16, 128, 128, 0) for k in range(0, max(pal.palette)+1)}), p_vn=pal_vn & 0xFF, p_id=pal_id, pts=c_pts_und)
                        displaysets.append(DisplaySet([pcs, pds, ENDS.from_scratch(pts=c_pts_und)]))
                        pcs_id += 1
                        pal_vn += 1
                        if pal_vn >= 256:
                            pal_id = (pal_id + 1) & 0b111
                            pal_vn = 0
                        next_pal_full = True

                    pcs = pcs_fn(pcs_id, states[z], True, pal_id, cobjs if is_compat_mode else cobjs_cropped, c_pts)
                    pds = PDS.from_scratch(p1 | p2 if not next_pal_full else pal, p_vn=pal_vn & 0xFF, p_id=pal_id, pts=c_pts)
                    displaysets.append(DisplaySet([pcs, pds, ENDS.from_scratch(pts=c_pts)]))

                    next_pal_full = False
                    pal_vn += 1
                    if pal_vn >= 256:
                        pal_id = (pal_id + 1) & 0b111
                        pal_vn = 0
                        next_pal_full = True
                    pcs_id += 1
                assert z+1 == k
            i = k
            if use_pbar:
                pbar.n = i
                pbar.update()
        if use_pbar:
            pbar.close()
        ####while
        #final "undisplay" displayset
        displaysets.append(get_undisplay(self, -1, pcs_id, wds_base, pal_id))
        pcs_id += 1
        return Epoch(displaysets)

    def find_acqs(self, pgobjs_proc: dict[..., list[...]], windows):
        #get the frame count between each screen update and find where we can do acqs
        gp_clear_dur = PGDecoder.copy_gp_duration(sum(map(lambda x: x.area, windows)))
        is_compat_mode = self.kwargs.get('pgs_compatibility', False)

        durs = self.get_durations()

        dtl = np.zeros((len(durs)), dtype=float)
        valid = np.zeros((len(durs),), dtype=np.bool_)
        absolutes = np.zeros_like(valid)

        objs = [None for objs in pgobjs_proc]

        prev_dt = 6
        for k, (dt, delay) in enumerate(durs):
            margin = (delay + prev_dt)*1/self.bdn.fps
            force_acq = False
            for wid, wd in enumerate(windows):
                if objs[wid] and not objs[wid].is_active(k):
                    objs[wid] = None
                    #force_acq = True
                if len(pgobjs_proc[wid]):
                    if not objs[wid] and pgobjs_proc[wid][0].is_active(k):
                        objs[wid] = pgobjs_proc[wid].pop(0)
                        force_acq = True
                    else:
                        assert not pgobjs_proc[wid][0].is_active(k)

            areas = list(map(lambda obj: obj.area*obj.is_visible(k), filter(lambda x: x is not None, objs)))
            c_areas = areas if is_compat_mode else map(lambda obj: obj.box.area*obj.is_visible(k), filter(lambda x: x is not None, objs))
            td = PGDecoder.decode_display_duration(gp_clear_dur, areas, c_areas)
            valid[k] = (td < margin)
            dtl[k] = 1-td/margin
            absolutes[k] = force_acq
            prev_dt = dt
        return valid, absolutes, dtl, durs
    ####

    def get_durations(self) -> npt.NDArray[np.uint32]:
        """
        Returns the duration of each event in frames.
        Additionally, the offset from the previous event is also returned. This value
        is zero unless there are no PG objects shown at some point in the epoch.
        """
        top = TC.tc2f(self.events[0].tc_in, self.bdn.fps)
        delays = []
        for event in self.events:
            tic = TC.tc2f(event.tc_in, self.bdn.fps)
            toc = TC.tc2f(event.tc_out,self.bdn.fps)
            delays.append((toc-tic, top-tic))
            top = toc
        return delays
####
#%%

class WOBAnalyzer:
    def __init__(self, wob, ssim_threshold: float = 0.95, overlap_threshold: float = 0.995) -> None:
        self.wob = wob
        assert ssim_threshold < 1.0, "Not a valid SSIM threshold"
        self.ssim_threshold = ssim_threshold
        assert 0 < overlap_threshold < 1.0, "Not a valid overlap threshold."
        self.overlap_threshold = overlap_threshold

    @staticmethod
    def get_grayscale(rgba: npt.NDArray[np.uint8]) -> npt.NDArray[np.uint8]:
        rgba = rgba.astype(np.uint16)
        img = np.round(0.2989*rgba[:,:,0] + 0.587*rgba[:,:,1] + 0.114*rgba[:,:,2])
        return (img.clip(0, 255) & (255*(rgba[:,:,3] > 0))).astype(np.uint8)

    def compare(self, bitmap: Image.Image, current: Image.Image) -> tuple[float, float]:
        """
        :param bitmap: (cropped or padded) aggregate of the previous bitmaps
        :param current: current bitmap under analysis
        :return: comparison score between the two
        """
        assert bitmap.width == current.width and bitmap.height == current.height, "Different shapes."

        # Intersect alpha planes
        a_bitmap = np.array(bitmap)
        a_current = np.array(current)
        inters_inv = np.logical_and(a_bitmap[:,:,3] == 0, a_current[:,:,3] == 0)
        inters = np.logical_and(a_bitmap[:,:,3] != 0, a_current[:,:,3] != 0)
        inters_area = np.sum(inters)
        #if the images have the exact same alpha channel, this measure is equal to 1
        overlap = (inters_area > 0) * (inters_area + np.sum(inters_inv))/inters.size

        if overlap < self.overlap_threshold and overlap > 0:
            #score = compare_ssim(bitmap.convert('L'), current.convert('L'))
            #Broadcast transparency mask of current on all channels of ref
            mask = 255*(np.logical_and((a_bitmap[:, :, 3] > 0), (a_current[:, :, 3] > 0)).astype(np.uint8))
            score = compare_ssim(Image.fromarray(a_bitmap & mask[:, :, None]).convert('L'), current.convert('L'))
            cross_percentage = np.sum(mask > 0)/mask.size
        else:
            #Perfect overlap or zero overlap, the current bitmap fits perfectly on the previous
            score = 1.0
            cross_percentage = 1.0
        return score, cross_percentage

    @staticmethod
    def get_patch(image1, image2) -> Image.Image:
        bbox1 = image1.getbbox()
        bbox2 = image2.getbbox()

        #One of the image is fully transparent.
        if bbox1 is None or bbox2 is None:
            return bbox1, bbox2

        f_area = lambda bbox: (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])

        A1 = f_area(bbox1)
        A2 = f_area(bbox2)

        if A1 >= A2:
            return (image1.crop(bbox1), Pos(*bbox1[:2])), image2
        return image1, (image2.crop(bbox2), Pos(*bbox2[:2]))

    @staticmethod
    def correlate_patch(image, patch) -> tuple[float, Type['Box']]:
        image = cv2.cvtColor(np.array(image), cv2.COLOR_RGBA2BGRA)

        #pad image with patch dimension, so we can look at the edges.
        image = cv2.copyMakeBorder(image, patch.height-1, patch.height-1,
                                   patch.width-1, patch.width-1,
                                   cv2.BORDER_CONSTANT, value=[0]*4)

        img_gray = cv2.cvtColor(image, cv2.COLOR_BGRA2GRAY)
        patch_gray = cv2.cvtColor(np.asarray(patch, dtype=np.uint8), cv2.COLOR_RGBA2GRAY)

        #Correlate across entire surface and return the best scores
        res = cv2.matchTemplate(img_gray, patch_gray, cv2.TM_CCOEFF_NORMED)
        score1, score2, p1, p2 = cv2.minMaxLoc(res)
        return (score1, p1), (score2, p2)

    def analyze(self):
        cls = self.__class__
        window = self.wob.get_window()

        bitmaps = []
        alpha_compo = Image.fromarray(np.zeros((window.dy, window.dx, 4), np.uint8))

        mask = []
        event_cnt = 0
        pgo_yield = None
        unseen = 0

        while True:
            rgba = yield pgo_yield
            pgo_yield = None

            if rgba is None:
                if len(bitmaps):
                    bbox = alpha_compo.getbbox()
                    if unseen > 0:
                        mask = mask[:-unseen]
                    pgo_yield = PGObject(np.stack(bitmaps).astype(np.uint8), Box.from_coords(*bbox), mask, f_start)
                    bitmaps = []
                    mask = []
                    continue
                else:
                    break

            has_content = np.any(rgba)
            if has_content or len(mask):
                if not len(mask):
                    f_start = event_cnt
                mask.append(has_content)

                rgba_i = Image.fromarray(rgba)
                score, cross_percentage = self.compare(alpha_compo, rgba_i)
                if score >= max(1.0, self.ssim_threshold + (1-self.ssim_threshold)*(1-cross_percentage)):
                    bitmaps.append(rgba)
                    alpha_compo.alpha_composite(rgba_i)
                else:
                    bbox = alpha_compo.getbbox()
                    pgo_yield = PGObject(np.stack(bitmaps).astype(np.uint8), Box.from_coords(*bbox), mask[:-1-unseen], f_start)

                    #new bitmap
                    mask = mask[-1:]
                    bitmaps = [rgba]
                    f_start = event_cnt
                    alpha_compo = Image.fromarray(rgba.copy())
                unseen = (not has_content)*(unseen + 1)
            event_cnt += 1
        ####while
        return # StopIteration


# def test_diplayset(ds: DisplaySet) -> None:
#     """
#     This function performs hard check on the display set
#     if its structure is bad, it raises an assertion error.
#     This is preferred over a "return false" because a bad displayset
#     will typically crash a hardware decoder and we don't want that.
#     """
#     current_pts = ds.pcs.pts
#     if epoch.ds[kd-1].pcs.pts != prev_pts and current_pts != epoch.ds[kd-1].pcs.pts:
#         prev_pts = epoch.ds[kd-1].pcs.pts
#     else:
#         logger.warning(f"Two displaysets at {current_pts} [s] (internal rendering error?)")

#     if ds.pcs.composition_state != PCS.CompositionState.NORMAL:
#         assert ds.pcs.pal_flag is False, "Palette update on epoch start or acquisition."
#     if ds.wds:
#         assert ds.pcs.pal_flag is False, "Manipulating windows on palette update."
#         assert len(ds.wds.windows) <= 2, "More than two windows."
#     if ds.ods:
#         assert ds.pcs.pal_flag is False, "Defining ODS in palette update."
#         start_cnt, close_cnt = 0, 0
#         for ods in ds.ods:
#             start_cnt += bool(int(ods.flags) & int(ODS.ODSFlags.SEQUENCE_FIRST))
#             close_cnt += bool(int(ods.flags) & int(ODS.ODSFlags.SEQUENCE_LAST))
#         assert start_cnt == close_cnt, "ODS segments flags mismatch."
#     if ds.pds:
#         for pds in ds.pds:
#             if ds.pcs.pal_flag:
#                 assert ds.pcs.pal_id == pds.p_id, "Palette ID mismatch between PCS and PDS on palette update."
#                 assert len(ds) == 3, "Unusual display set structure for a palette update."
#             assert pds.p_id < 8, "Using undefined palette ID."
#             assert pds.n_entries <= 256, "Defining more than 256 palette entries."
#     assert ds.end, "No END segment in DS."
#     assert ds.end.pts >= ds.end.dts, "Not MPEG2 standard compliant."
    ####


#%%
def get_wipe_duration(wds: WDS) -> int:
    return np.ceil(sum(map(lambda w: PGDecoder.FREQ*w.height*w.width/PGDecoder.RC, wds.windows)))

#%%
def set_pts_dts_sc(ds: DisplaySet, buffer: PGObjectBuffer, wipe_duration: int) -> list[tuple[int, int]]:
    ddurs = {}
    for ods in ds.ods:
        if ods.flags & ods.ODSFlags.SEQUENCE_FIRST:
            assert ods.o_id not in ddurs, f"Object {ods.o_id} defined twice in DS."
            if (shape := buffer.get(ods.o_id)) is not None:
                assert (ods.height, ods.width) == shape, "Dimension mismatch, buffer corruption."
            else:
                # Allocate a buffer slot for this object
                assert buffer.allocate_id(ods.o_id, ods.height, ods.width) is True, "Slot already allocated or buffer overflow."
            ddurs[ods.o_id] = np.ceil(ods.height*ods.width*PGDecoder.FREQ/PGDecoder.RD)

    t_decoding = 0
    decode_duration = 0

    if ds.ods:
        if ds.wds:
            if ds.pcs.composition_state == ds.pcs.CompositionState.EPOCH_START:
                decode_duration = np.ceil(ds.pcs.width*ds.pcs.height*PGDecoder.FREQ/PGDecoder.RC)
            else:
                decode_duration = wipe_duration
            object_decode_duration = ddurs.copy()

            windows = {wd.window_id: (wd.height, wd.width) for wd in ds.wds.windows}

            #For every composition object, compute the transfer time
            for k, cobj in enumerate(ds.pcs.cobjects):
                shape = buffer.get(cobj.o_id)
                assert shape is not None, "Object does not exist in buffer."
                w, h = windows[cobj.window_id][0], windows[cobj.window_id][1]

                t_decoding += object_decode_duration.pop(cobj.o_id, 0)

                # Same window -> patent claims the plane is written only once after the two cobj are processed.
                if k == 0 and ds.pcs.n_objects > 1 and ds.pcs.cobjects[1] == cobj.window_id:
                    continue
                copy_dur = np.ceil(w*h*PGDecoder.FREQ/PGDecoder.RC)
                decode_duration = max(decode_duration, t_decoding) + copy_dur
        #This DS defines new objects without displaying them
        elif not ds.pcs.pal_flag:
            decode_duration = sum(ddurs.values())
            t_decoding = decode_duration
        #pal flag with ODS, not desirable.
        else:
            raise AssertionError("Illegal DS (palette update with ODS!)")
    elif ds.wds or ds.pcs.pal_flag:
        assert ds.pcs.composition_state == ds.pcs.CompositionState.NORMAL, "DS refreshes the screen but no valid object exists."
        decode_duration = wipe_duration + 1
    else:
        # No ODS, no WDS, no palette update -> We're just writing palette data or doing a NOP (PCS, END)
        # In this case, PTS and DTS are equals for all segments.
        ...

    #PCS always exist
    dts = np.uint32(ds.pcs.tpts - decode_duration)
    ts_pairs = [(ds.pcs.tpts, dts)]

    if ds.wds:
        ts_pairs.append((np.uint32(ds.pcs.tpts - wipe_duration), dts))
    for pds in ds.pds:
        ts_pairs.append((dts, dts))

    for ods in ds.ods:
        ods_pts = np.uint32(dts + ddurs.get(ods.o_id))
        ts_pairs.append((ods_pts, dts))
        if ods.flags & ods.ODSFlags.SEQUENCE_LAST:
            dts = ods_pts
    ts_pairs.append((dts, dts))
    return ts_pairs

def apply_pts_dts(ds: DisplaySet, ts: tuple[int, int]) -> None:
    assert len(ds) == len(ts), "Timestamps-DS size mismatch."
    for seg, (pts, dts) in zip(ds, ts):
        seg.tpts, seg.tdts = pts, dts

def set_pts_dts(ds: DisplaySet, buffer: PGObjectBuffer, _wrong: bool = False):
    """
    Set PTS and DTS of a DisplaySet.
    Hypothesis: All segments in the DS have their PTS equal to the desired on-screen PTS
    This function will then adapt everything in time according to this desired PTS.

    :param ds:     DisplaySet with PTS set as stated, and DTS=0 for all segments
    :param buffer: PG Object Buffer that can be allocated and fetched during the epoch.
    :param _wrong: Implement Scenarist BD behaviour.
    """
    if ds.pcs.pal_flag:
        assert len(ds) == 3 and isinstance(ds[1], PDS), "Not a valid DS."
        ds.end.tpts = ds.end.tdts = ds.pds[0].tpts = ds.pds[0].tdts = ds.pcs.tdts = ds.pcs.tpts
        return

    ddurs = {}
    for ods in ds.ods:
        if ods.flags & ods.ODSFlags.SEQUENCE_FIRST:
            assert ods.o_id not in ddurs, f"Object {ods.o_id} defined twice in DS."
            if (shape := buffer.get(ods.o_id)) is not None:
                assert (ods.height, ods.width) == shape, "Dimension mismatch, buffer corruption."
            else:
                # Allocate a buffer slot for this object
                assert buffer.allocate_id(ods.o_id, ods.height, ods.width) is True, "Slot already allocated or buffer overflow."
            ddurs[ods.o_id] = np.ceil(ods.height*ods.width*PGDecoder.FREQ/PGDecoder.RD)

    t_copy_window = 0
    t_decoding = 0

    if ds.wds:
        # WDS exist -> display refresh
        wipe_duration = 0
        windows = {wd.window_id: (wd.height, wd.width) for wd in ds.wds.windows}

        if ds.pcs.composition_state == ds.pcs.CompositionState.EPOCH_START:
            wipe_duration = np.ceil(ds.pcs.width*ds.pcs.height*PGDecoder.FREQ/PGDecoder.RC)
        else: #elif ds.pcs.composition_state & ds.pcs.CompositionState.ACQUISITION:
            wipe_duration = sum(map(lambda w: np.ceil(PGDecoder.FREQ*w[0]*w[1]/PGDecoder.RC), windows.values()))

        decode_duration = wipe_duration
        object_decode_duration = ddurs.copy()

        #For every composition object, compute the transfer time
        # taking into account decoding and copying, with cropping parameters
        for cobj in ds.pcs.cobjects:
            shape = buffer.get(cobj.o_id)
            assert shape is not None, "Object does not exist in buffer."
            h, w = shape
            area = h*w if cobj.cropped is False else cobj.c_w*cobj.c_h

            #we copy at most window_area
            area = min(windows[cobj.window_id][0]*windows[cobj.window_id][1], area)
            copy_dur = np.ceil(area*PGDecoder.FREQ/PGDecoder.RC)
            t_copy_window += copy_dur

            t_decoding += object_decode_duration.pop(cobj.o_id, 0)
            decode_duration = max(decode_duration, t_decoding) + copy_dur

        #if both objects are in the same window, they are copied at the same time
        # according to the patent... (probably not, there's no reason for that unless the objects are blended)
        # here I assume a worse condition: combination of both copying time, => overlapping area is counted twice.
        if len(ds.pcs.cobjects) == 2 and ds.pcs.cobjects[0].window_id == ds.pcs.cobjects[1].window_id:
            decode_duration = t_decoding + t_copy_window

        if _wrong and len(ds.ods) == 0:
            # Makes no sense but this is what Scenarist does on [PCS, WDS, (PDS), END]
            # because we only give the time to wipe the windows, not to copy the object
            decode_duration = ds.wds.n_windows + wipe_duration

        # The display set may contain more object that will be displayed later.
        if len(object_decode_duration) > 0:
            #we assume that the objects are properly ordered in the stream (displayed ODS are already processed)
            t_decoding += sum(object_decode_duration.values())

            #the decode duration is then either the total decoding length or the transfer time
            # (PROBABLY WRONG -> Can a DS continue to be decoded after PTS(pcs) has passed?)
            decode_duration = max(decode_duration, t_decoding)
    else: #if ds.wds
        decode_duration = sum(ddurs.values())
        t_decoding = decode_duration

    ds.pcs.tdts = ds.pcs.tpts - decode_duration

    # Doubt: on [PCS(no composition), WDS, END] (= epoch end), we have:
    # DTS(pcs) == DTS(wds), PTS(wds) - DTS(wds) = SUM(CLEAR(WDi)), and PTS(pcs)==PTS(wds)
    # as we don't need any margin to copy any object (there's none to process)
    if ds.wds:
        ds.wds.tdts = ds.pcs.tdts
        ds.wds.tpts -= t_copy_window
    if ds.pds:
        ds.pds[0].tdts = ds.pds[0].tpts = ds.pcs.tdts
    last_dts = ds.pcs.tdts

    #Propagate DTS(ODSi+1) = PTS(ODSi) by ODS blocks (groups of ODS sharing an object_id)
    for k, ods in enumerate(ds.ods):
        if ods.ODSFlags.SEQUENCE_FIRST & ods.flags:
            for ods_other in ds.ods[k:]:
                if ods_other.o_id != ods.o_id:
                    break
                ods.tdts = last_dts
                ods.tpts = ods.tdts + ddurs.get(ods.o_id)
            last_dts = ods.tpts
    ds.end.tdts = ds.end.tpts = ds.pcs.tdts + t_decoding
    if ds.ods:
        assert ds.end.tdts == ds.ods[-1].tpts, "PTS(END) != PTS(ODSlast)"
    return #don't return the DS because we modified by reference already

def is_compliant(epochs: list[Epoch], fps: float, *, _cnt_pts: bool = False) -> bool:
    prev_pts = -1
    last_cbbw = 0
    last_dbbw = 0
    compliant = True
    warnings = 0
    cumulated_ods_size = 0

    coded_bw_ra_pts = [-1] * round(fps)
    coded_bw_ra = [0] * round(fps)

    to_tc = lambda pts: TC.s2tc(pts, fps)

    for ke, epoch in enumerate(epochs):
        ods_acc = 0
        window_area = {}
        objects_sizes = {}

        for kd, ds in enumerate(epoch.ds):
            size_ds = 0
            decoded_this_ds = 0
            coded_this_ds = 0

            current_pts = ds.pcs.pts
            if epoch.ds[kd-1].pcs.pts != prev_pts and current_pts != epoch.ds[kd-1].pcs.pts:
                prev_pts = epoch.ds[kd-1].pcs.pts
                last_cbbw, last_dbbw, last_rc = [0]*3
            else:
                logger.warning(f"Two displaysets at {to_tc(current_pts)} (internal rendering error?)")

            for seg in ds.segments:
                areas2gp = {}
                size_ds += len(bytes(seg))
                if seg.pts != current_pts and current_pts != -1 and _cnt_pts:
                    logger.warning(f"Display set has non-constant pts at {to_tc(seg.pts)} or {to_tc(current_pts)}.")
                    current_pts = -1
                if isinstance(seg, PCS):
                    if int(seg.composition_state) != 0:
                        # On acquisition, the object buffer is flushed
                        ods_acc = 0
                        objects_sizes = {}
                    for cobj in seg.cobjects:
                        areas2gp[cobj.o_id] = -1 if not cobj.cropped else cobj.c_w*cobj.c_h

                elif isinstance(seg, WDS):
                    for w in seg.windows:
                        window_area[w.window_id] = w.width*w.height
                elif isinstance(seg, ODS):
                    if int(seg.flags) & int(ODS.ODSFlags.SEQUENCE_FIRST):
                        if cumulated_ods_size > 0:
                            logger.error("A past ODS was not properly terminated! Stream is critically corrupted!")
                            compliant = False
                        decoded_this_ds += seg.width * seg.height
                        coded_this_ds += seg.rle_len
                        objects_sizes[seg.o_id] = seg.width * seg.height

                    cumulated_ods_size = len(bytes(seg)[2:])

                    if int(seg.flags) & int(ODS.ODSFlags.SEQUENCE_LAST):
                        if cumulated_ods_size > PGDecoder.CODED_BUF_SIZE:
                            logger.warning(f"Object has size >1 MiB at {to_tc(seg.pts)}. Some decoders don't support this. Reduce object complexity?")
                            warnings += 1
                        cumulated_ods_size = 0
                elif isinstance(seg, PDS):
                    if seg.p_id >= 8:
                        logger.warning(f"Using an undefined palette ID at {to_tc(seg.pts)}.")
                        compliant = False

            area_copied = 0
            for idx, area in areas2gp.items():
                area_copied += area if area >= 0 else objects_sizes[idx]
            ####
            ods_acc += decoded_this_ds

            coded_buffer_pts = last_cbbw + coded_this_ds
            decoded_buffer_pts = last_dbbw + decoded_this_ds

            if prev_pts != seg.pts:
                coded_buffer_bandwidth = coded_buffer_pts/abs(seg.pts-prev_pts)
                decoded_buffer_bandwidth = decoded_buffer_pts/abs(seg.pts-prev_pts)
                last_cbbw, last_dbbw = 0, 0
            else:
                # Same PTS, we can't do any calculation -> accumulate to next PTS
                last_cbbw = coded_buffer_pts
                last_dbbw = decoded_buffer_pts
                coded_buffer_bandwidth, decoded_buffer_bandwidth = 0, 0

            # This is probably the hardest constraint to meet: ts_packet are read at, at most Rx=16Mbps
            if coded_buffer_bandwidth > (max_rate := PGDecoder.RX):
                if coded_buffer_bandwidth/max_rate >= 2:
                    logger.warning(f"High instantaneous coded bandwidth at {to_tc(seg.pts)} (not critical - fair warning)")
                else:
                    logger.info(f"High coded bandwidth at {to_tc(seg.pts)} (not critical - fair warning).")
                # This is not an issue unless it happens very frequently, so we don't mark as not compliant

            if prev_pts != seg.pts:
                coded_bw_ra = coded_bw_ra[1:round(fps)]
                coded_bw_ra_pts = coded_bw_ra_pts[1:round(fps)]
                coded_bw_ra.append(coded_buffer_pts)
                coded_bw_ra_pts.append(seg.pts)

            if (rate:=sum(coded_bw_ra)/abs(coded_bw_ra_pts[-1]-coded_bw_ra_pts[0])) > (max_rate:=16*(1024**2)):
                logger.warning(f"Exceeding coded bandwidth at ~{to_tc(seg.pts)}, {100*rate/max_rate:.03f}%.")
                warnings += 1

            if decoded_buffer_bandwidth > PGDecoder.RD:
                logger.warning(f"Exceeding decoded buffer bandwidth at {to_tc(seg.pts)}.")
                warnings +=1

            # Decoded object plane is 4 MiB
            if ods_acc >= PGDecoder.DECODED_BUF_SIZE:
                logger.warning(f"Decoded obect buffer overrun at {to_tc(seg.pts)}.")
                compliant = False

            #On palette update, we re-evaluate the existing graphic plane with a new CLUT.
            # so we are not subject to the Rc constraint.
            if ds.pcs.pal_flag:
                continue

            #We clear the plane (window area) and copy the objects to window. This is done at 32MB/s
            Rc = fps*(sum(window_area.values()) + np.min([area_copied, sum(window_area.values())]))
            nf = TC.s2f(seg.pts, fps) - TC.s2f(prev_pts, fps)
            if nf == 0:
                last_rc += Rc
            elif (last_rc+Rc)/nf > PGDecoder.RC:
                logger.warning(f"Graphic plane overloaded. Graphics may flicker at {to_tc(seg.pts)}.")
                warnings += 1

    if warnings == 0 and compliant:
        logger.info("Output PGS seems compliant.")
    if warnings > 0 and compliant:
        logger.warning(f"Excessive bandwidth detected, requires HW testing (PGS may go out of sync).")
    elif not compliant:
        logger.error("PGStream will crash a HW decoder.")
    return compliant
