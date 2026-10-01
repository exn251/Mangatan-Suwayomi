# -*- coding: utf-8 -*-

# ==============================================================================
# 1. Standard Library Imports
# ==============================================================================
from abc import ABC, abstractmethod
import asyncio
import gc
import io
import math
import os
import random
import re
import shutil
import threading
import time
from typing import Callable, Optional, TypedDict
import urllib.request

# ==============================================================================
# 2. Core Image, Math & Geometry Libraries
# ==============================================================================
import cv2
import numpy as np
from PIL import Image
import pyclipper
from shapely.geometry import Polygon

# ==============================================================================
# 3. Machine Learning & Inference Engines
# ==============================================================================
from huggingface_hub import hf_hub_download
import onnxruntime as ort

# ==============================================================================
# 4. Networking & Google Lens Protocols
# ==============================================================================
import curl_cffi.requests
from google.protobuf.json_format import MessageToDict

try:
    from lens_protos.lens_overlay_server_pb2 import (
        LensOverlayServerRequest,
        LensOverlayServerResponse,
    )
    from lens_protos.lens_overlay_platform_pb2 import PLATFORM_WEB
    from lens_protos.lens_overlay_surface_pb2 import SURFACE_CHROMIUM
    from lens_protos.lens_overlay_filters_pb2 import AUTO_FILTER
except ImportError as e:
    print(
        f"Warning: Failed to import lens_protos. Ensure the 'lens_protos' "
        f"directory is in your project root or PYTHONPATH. Details: {e}"
    )


class BoundingBox(TypedDict):
    x: float
    y: float
    width: float
    height: float


class Bubble(TypedDict):
    text: str
    tightBoundingBox: BoundingBox
    orientation: float
    font_size: float
    confidence: float


class Engine(ABC):
    """Base class for OCR engines with aspect-ratio-aware webtoon chunking."""
    CHUNK_HEIGHT: int = 2500
    OVERLAP: int = 350
    MIN_WEBTOON_ASPECT_RATIO: float = 1.85

    @abstractmethod
    async def ocr(self, img: Image.Image) -> list[Bubble]:
        pass

    def _suppress_duplicate_bubbles(
        self,
        bubbles: list[Bubble],
        full_width: int,
        full_height: int,
        iou_threshold: float = 0.50,
    ) -> list[Bubble]:
        """Suppresses duplicate detections across chunk overlaps and merges boundary-split text boxes."""
        if not bubbles:
            return []

        items = []
        for b in bubbles:
            box = b["tightBoundingBox"]
            px_x1 = box["x"] * full_width
            px_y1 = box["y"] * full_height
            px_x2 = (box["x"] + box["width"]) * full_width
            px_y2 = (box["y"] + box["height"]) * full_height
            items.append({
                "bubble": b,
                "box": [px_x1, px_y1, px_x2, px_y2],
                "area": max(1.0, (px_x2 - px_x1) * (px_y2 - px_y1)),
            })

        items.sort(key=lambda item: item["area"], reverse=True)
        keep = []

        while items:
            current = items.pop(0)
            curr_b = current["bubble"]
            curr_box = list(current["box"])
            remaining = []

            for item in items:
                other_b = item["bubble"]
                other_box = item["box"]

                x1 = max(curr_box[0], other_box[0])
                y1 = max(curr_box[1], other_box[1])
                x2 = min(curr_box[2], other_box[2])
                y2 = min(curr_box[3], other_box[3])

                inter_w = max(0.0, x2 - x1)
                inter_h = max(0.0, y2 - y1)
                inter_area = inter_w * inter_h

                area1 = (curr_box[2] - curr_box[0]) * (curr_box[3] - curr_box[1])
                area2 = (other_box[2] - other_box[0]) * (other_box[3] - other_box[1])
                min_area = max(1.0, min(area1, area2))
                union_area = area1 + area2 - inter_area

                if min_area <= 0:
                    remaining.append(item)
                    continue

                ios = inter_area / min_area
                iou = (inter_area / union_area) if union_area > 0 else 0.0

                c_clean = re.sub(r'\s+', '', curr_b["text"])
                o_clean = re.sub(r'\s+', '', other_b["text"])
                same_text = (c_clean == o_clean) and len(c_clean) > 0
                is_sub_text = (o_clean in c_clean or c_clean in o_clean) and min(len(c_clean), len(o_clean)) > 0

                # Strict chunk-overlap deduplication criteria
                if (iou >= iou_threshold) or (same_text and ios >= 0.35) or (is_sub_text and ios >= 0.60):
                    curr_box[0] = min(curr_box[0], other_box[0])
                    curr_box[1] = min(curr_box[1], other_box[1])
                    curr_box[2] = max(curr_box[2], other_box[2])
                    curr_box[3] = max(curr_box[3], other_box[3])

                    if len(other_b["text"]) > len(curr_b["text"]):
                        curr_b["text"] = other_b["text"]
                        curr_b["confidence"] = other_b["confidence"]
                else:
                    remaining.append(item)

            w_px = curr_box[2] - curr_box[0]
            h_px = curr_box[3] - curr_box[1]
            curr_b["tightBoundingBox"] = BoundingBox(
                x=curr_box[0] / full_width,
                y=curr_box[1] / full_height,
                width=w_px / full_width,
                height=h_px / full_height,
            )
            if curr_b.get("orientation") is None:
                curr_b["orientation"] = 90.0 if h_px > w_px else 0.0

            keep.append(curr_b)
            items = remaining

        return keep

    async def _process_webtoon_chunked(
        self,
        img: Image.Image,
        process_chunk_fn: Callable[[Image.Image], list[Bubble]],
        engine_label: str = "OCR"
    ) -> list[Bubble]:
        """Generic aspect-ratio-aware webtoon chunk processor with boundary deduplication."""
        full_width, full_height = img.size
        aspect_ratio = full_height / max(1, full_width)
        is_webtoon = (aspect_ratio >= self.MIN_WEBTOON_ASPECT_RATIO) and (full_height > self.CHUNK_HEIGHT)

        # 1. Single pass for standard manga pages
        if not is_webtoon:
            if asyncio.iscoroutinefunction(process_chunk_fn):
                return await process_chunk_fn(img)
            return await asyncio.to_thread(process_chunk_fn, img)

        # 2. Chunking for tall webtoon strips
        print(f"[Info] {engine_label}: Webtoon strip ({full_width}x{full_height}, ratio {aspect_ratio:.2f}), processing chunks...")
        chunks = []
        y_offset = 0
        while y_offset < full_height:
            chunk_end = min(y_offset + self.CHUNK_HEIGHT, full_height)
            chunk_image = img.crop((0, y_offset, full_width, chunk_end))
            chunks.append((chunk_image, y_offset, chunk_end - y_offset))

            if chunk_end >= full_height:
                break
            y_offset += self.CHUNK_HEIGHT - self.OVERLAP

        all_raw_results: list[Bubble] = []
        for chunk_img, offset_y, chunk_h in chunks:
            if asyncio.iscoroutinefunction(process_chunk_fn):
                chunk_results = await process_chunk_fn(chunk_img)
            else:
                chunk_results = await asyncio.to_thread(process_chunk_fn, chunk_img)

            for item in chunk_results:
                bbox = item["tightBoundingBox"]
                bbox["y"] = (bbox["y"] * chunk_h + offset_y) / full_height
                bbox["height"] = (bbox["height"] * chunk_h) / full_height
                all_raw_results.append(item)

        return self._suppress_duplicate_bubbles(all_raw_results, full_width, full_height)


class GoogleLens(Engine):
    """
    Google Lens Protobuf engine with Lanczos upscaling, global server-wide
    throttling, auto-retry on burst errors, and aspect-ratio-aware webtoon handling.
    """

    _global_semaphore = threading.Semaphore(2)

    def __init__(
        self,
        jpeg_quality: int = 95,
        target_longest_edge: int = 2400,
        max_edge: int = 2560,
        max_upscale_factor: float = 2.5,
    ):
        super().__init__()
        self.jpeg_quality = jpeg_quality
        self.target_longest_edge = target_longest_edge
        self.max_edge = max_edge
        self.max_upscale_factor = max_upscale_factor

        self.CHUNK_HEIGHT = 2000
        self.OVERLAP = 300

    def _preprocess_to_jpeg_bytes(self, image: Image.Image) -> tuple[bytes, int, int]:
        if image.mode != "RGB":
            image = image.convert("RGB")

        w, h = image.size
        longest_edge = max(w, h)

        # 1. Upscale low-res scans using Lanczos
        if longest_edge < self.target_longest_edge:
            scale = min(self.max_upscale_factor, self.target_longest_edge / max(1, longest_edge))
            if scale > 1.05:
                new_w = max(1, int(round(w * scale)))
                new_h = max(1, int(round(h * scale)))
                image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

        # 2. Downscale oversized scans to stay within Google Lens limits
        elif longest_edge > self.max_edge:
            scale = self.max_edge / float(longest_edge)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            image = image.resize((new_w, new_h), Image.Resampling.LANCZOS)

        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=self.jpeg_quality, subsampling=0)
        return buf.getvalue(), image.width, image.height

    async def _ocr_single(self, session: curl_cffi.requests.AsyncSession, chunk_img: Image.Image) -> list[Bubble]:
        img_bytes, img_width, img_height = self._preprocess_to_jpeg_bytes(chunk_img)

        request = LensOverlayServerRequest()
        request.objects_request.request_context.request_id.uuid = random.randint(0, 2**64 - 1)
        request.objects_request.request_context.request_id.sequence_id = 0
        request.objects_request.request_context.request_id.image_sequence_id = 0
        request.objects_request.request_context.request_id.analytics_id = random.randbytes(16)

        request.objects_request.request_context.client_context.platform = PLATFORM_WEB
        request.objects_request.request_context.client_context.surface = SURFACE_CHROMIUM
        request.objects_request.request_context.client_context.locale_context.language = "ja"
        request.objects_request.request_context.client_context.locale_context.region = "Asia/Tokyo"

        filter_obj = request.objects_request.request_context.client_context.client_filters.filter.add()
        filter_obj.filter_type = AUTO_FILTER

        request.objects_request.image_data.payload.image_bytes = img_bytes
        request.objects_request.image_data.image_metadata.width = img_width
        request.objects_request.image_data.image_metadata.height = img_height

        payload = request.SerializeToString()
        headers = {
            "Host": "lensfrontend-pa.googleapis.com",
            "Connection": "keep-alive",
            "Content-Type": "application/x-protobuf",
            "X-Goog-Api-Key": "AIzaSyDr2UxVnv_U85AbhhY8XSHSIavUW0DC-sY",
            "Sec-Fetch-Mode": "no-cors",
            "Sec-Fetch-Dest": "empty",
        }

        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            await asyncio.to_thread(self._global_semaphore.acquire)
            try:
                res = await session.post(
                    "https://lensfrontend-pa.googleapis.com/v1/crupload",
                    data=payload,
                    headers=headers,
                    impersonate="chrome",
                    timeout=30,
                )
            except Exception as e:
                if attempt == MAX_RETRIES - 1:
                    print(f"[GoogleLens] Connection error after {MAX_RETRIES} attempts: {e}")
                    return []
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            finally:
                self._global_semaphore.release()

            if res.status_code == 200:
                try:
                    response_proto = LensOverlayServerResponse()
                    response_proto.ParseFromString(res.content)
                    response_dict = MessageToDict(response_proto, preserving_proto_field_name=True)
                    return self.transform(response_dict)
                except Exception as e:
                    print(f"[GoogleLens] Protobuf parse error: {e}")
                    return []

            elif res.status_code in (429, 500, 502, 503, 504):
                backoff_time = 2.0 * (attempt + 1)
                print(f"[GoogleLens] Server returned {res.status_code}. Backing off {backoff_time:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})...")
                await asyncio.sleep(backoff_time)
            else:
                print(f"[GoogleLens] API Error (Code: {res.status_code}): {res.content[:150]}")
                return []

        print(f"[GoogleLens] Request failed after {MAX_RETRIES} retries.")
        return []

    async def ocr(self, img: Image.Image) -> list[Bubble]:
        full_width, full_height = img.size
        aspect_ratio = full_height / max(1, full_width)
        is_webtoon = (aspect_ratio >= self.MIN_WEBTOON_ASPECT_RATIO) and (full_height > self.CHUNK_HEIGHT)

        async with curl_cffi.requests.AsyncSession(impersonate="chrome") as session:
            # Single pass for standard manga pages
            if not is_webtoon:
                return await self._ocr_single(session, img)

            # Chunking for tall vertical webtoons
            print(f"[Info] GoogleLens: Webtoon strip ({full_width}x{full_height}, ratio {aspect_ratio:.2f}), processing chunks...")
            chunks = []
            y_offset = 0
            while y_offset < full_height:
                chunk_end = min(y_offset + self.CHUNK_HEIGHT, full_height)
                chunk_image = img.crop((0, y_offset, full_width, chunk_end))
                chunks.append((chunk_image, y_offset, chunk_end - y_offset))

                if chunk_end >= full_height:
                    break
                y_offset += self.CHUNK_HEIGHT - self.OVERLAP

            async def process_chunk(chunk_img, offset_y, chunk_h):
                results = await self._ocr_single(session, chunk_img)
                for item in results:
                    bbox = item["tightBoundingBox"]
                    bbox["y"] = (bbox["y"] * chunk_h + offset_y) / full_height
                    bbox["height"] = (bbox["height"] * chunk_h) / full_height
                return results

            chunk_results_list = await asyncio.gather(
                *(process_chunk(c_img, off_y, c_h) for c_img, off_y, c_h in chunks)
            )

            all_raw_results: list[Bubble] = []
            for sublist in chunk_results_list:
                all_raw_results.extend(sublist)

            return self._suppress_duplicate_bubbles(all_raw_results, full_width, full_height)

    def transform(self, response_dict: dict) -> list[Bubble]:
        output_json: list[Bubble] = []
        objects_response = response_dict.get("objects_response", {})
        text_data = objects_response.get("text", {})
        text_layout = text_data.get("text_layout", {})
        paragraphs = text_layout.get("paragraphs", [])

        for p in paragraphs:
            for l in p.get("lines", []):
                line_text = ""
                for w in l.get("words", []):
                    word_text = w.get("plain_text", "")
                    separator = w.get("text_separator", "")
                    if separator == "SPACE":
                        separator = " "
                    elif separator == "NEWLINE":
                        separator = "\n"
                    elif separator == "TEXT_SEPARATOR_UNSPECIFIED":
                        separator = ""
                    line_text += word_text + separator

                line_text = line_text.strip().replace("･･･", "…")
                if not line_text:
                    continue

                l_bbox = l.get("geometry", {}).get("bounding_box", {})
                center_x = float(l_bbox.get("center_x", 0.0))
                center_y = float(l_bbox.get("center_y", 0.0))
                width = float(l_bbox.get("width", 0.0))
                height = float(l_bbox.get("height", 0.0))

                rotation_z = float(l_bbox.get("rotation_z", 0.0))
                angle_deg = math.degrees(rotation_z) if abs(rotation_z) < 2 * math.pi else rotation_z
                snapped_angle = 90.0 if height > width else 0.0
                actual_angle = snapped_angle + angle_deg

                bubble = Bubble(
                    text=line_text,
                    tightBoundingBox=BoundingBox(
                        x=center_x - width / 2,
                        y=center_y - height / 2,
                        width=width,
                        height=height,
                    ),
                    orientation=round(actual_angle, 1),
                    font_size=0.04,
                    confidence=0.98,
                )
                output_json.append(bubble)

        return output_json
  
class MangaOCR(Engine):
    """
    CPU-Torch MangaOCR Engine:
    - Detection & Deskewing: Meiki Small v0 ONNX + AngleNet Distill 96x96 FP16 (CPU)
    - Recognition: Official PyTorch manga-ocr (safetensors CPU)
    - Lens-style webtoon strip chunking & 4-point perspective deskewing
    """

    DROPLET_CHARS: set[str] = {
        "し", "つ", "ひ", "く", "ノ", "と", "へ", "S", "s", "2", "1",
        "c", "C", "I", "l", "ー", "〜", "・", "、", "…", ".."
    }
    REJECT_PATTERNS: set[str] = {"..", "...", "……"}

    def __init__(
        self,
        confidence_threshold: float = 0.44,
        target_angle_size: int = 96,
        batch_size: int = 4,
        force_cpu: bool = True
    ):
        super().__init__()
        self.confidence_threshold = confidence_threshold
        self.target_angle_size = target_angle_size
        self.batch_size = batch_size
        self.force_cpu = force_cpu

        self.CHUNK_HEIGHT = 2500
        self.OVERLAP = 350

        self.ANGLE_BINS = np.arange(180, dtype=np.float32)
        self.RAD_BINS = np.radians(2.0 * self.ANGLE_BINS)
        self.SIN_BINS = np.sin(self.RAD_BINS)
        self.COS_BINS = np.cos(self.RAD_BINS)

        self._ocr_semaphore = threading.Semaphore(1)

        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()

        self.models_dir = os.path.join(script_dir, "models")
        os.makedirs(self.models_dir, exist_ok=True)

        self._init_detector_and_anglenet()
        self._init_manga_ocr()

    def _init_detector_and_anglenet(self):
        self.meiki_path = os.path.join(self.models_dir, "meiki.text.detect.small.v0.onnx")
        if not os.path.exists(self.meiki_path):
            print("[MangaOCR-Torch] Downloading Meiki detector model...")
            self.meiki_path = hf_hub_download(
                repo_id="rtr46/meiki.text.detect.v0",
                filename="meiki.text.detect.small.v0.onnx",
                local_dir=self.models_dir
            )

        angle_fn = "anglenet_v0_1_distill_96x96_fp16.onnx"
        self.angle_path = os.path.join(self.models_dir, angle_fn)
        if not os.path.exists(self.angle_path):
            print(f"[MangaOCR-Torch] Downloading AngleNet model '{angle_fn}'...")
            try:
                self.angle_path = hf_hub_download(
                    repo_id="Kellenok/anglenet",
                    filename=angle_fn,
                    local_dir=self.models_dir
                )
            except Exception:
                angle_fn = "anglenet_v0_1_distill_96x96.onnx"
                self.angle_path = hf_hub_download(
                    repo_id="Kellenok/anglenet",
                    filename=angle_fn,
                    local_dir=self.models_dir
                )

        cpu_opts = ort.SessionOptions()
        cpu_opts.intra_op_num_threads = min(4, os.cpu_count() or 4)
        cpu_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        cpu_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

        self.sess_meiki = ort.InferenceSession(self.meiki_path, sess_options=cpu_opts, providers=['CPUExecutionProvider'])
        self.sess_angle = ort.InferenceSession(self.angle_path, sess_options=cpu_opts, providers=['CPUExecutionProvider'])
        self.angle_is_fp16 = "float16" in self.sess_angle.get_inputs()[0].type

    def _init_manga_ocr(self):
        try:
            from manga_ocr import MangaOcr as MOCR
            import logging
            from loguru import logger

            logger.disable('manga_ocr')
            logging.getLogger('transformers').setLevel(logging.ERROR)

            # Safetensors drop-in avoids CVE-2025-32434 torch.load block
            self.manga_ocr = MOCR(
                pretrained_model_name_or_path="tatsumoto/manga-ocr-base",
                force_cpu=self.force_cpu
            )
            print(f"[MangaOCR-Torch] Loaded PyTorch manga-ocr (Device: {'CPU' if self.force_cpu else 'GPU'})")
        except Exception as e:
            print(f"[Error] Failed to initialize PyTorch manga-ocr: {e}")
            raise

    def _clean_mechanical_noise(self, boxes: list[tuple[int, int, int, int]], W: int, H: int, min_area: int = 120, min_side: int = 8):
        filtered = []
        for (x1, y1, x2, y2) in boxes:
            w, h = x2 - x1, y2 - y1
            if w * h < min_area or min(w, h) < min_side:
                continue
            if (x1 <= 3 or x2 >= W - 3 or y1 <= 3 or y2 >= H - 3) and (w < 10 or h < 10):
                continue
            filtered.append((x1, y1, x2, y2))
        return filtered

    def _predict_angle(self, crop_gray: np.ndarray) -> float:
        ch, cw = crop_gray.shape[:2]
        if ch <= 0 or cw <= 0:
            return 0.0
        scale = min(self.target_angle_size / cw, self.target_angle_size / ch)
        nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
        resized = cv2.resize(crop_gray, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        pad = np.zeros((self.target_angle_size, self.target_angle_size), dtype=np.uint8)
        pad[(self.target_angle_size - nh) // 2 : (self.target_angle_size - nh) // 2 + nh,
            (self.target_angle_size - nw) // 2 : (self.target_angle_size - nw) // 2 + nw] = resized

        inp = (pad.astype(np.float16 if self.angle_is_fp16 else np.float32) / 255.0)[None, None, :, :]
        angle_outs = self.sess_angle.run(None, {self.sess_angle.get_inputs()[0].name: inp})
        logits = angle_outs[0][0].astype(np.float32)

        probs = np.exp(logits - np.max(logits))
        probs /= np.sum(probs)

        sin_sum = float(np.sum(probs * self.SIN_BINS))
        cos_sum = float(np.sum(probs * self.COS_BINS))
        return float((0.5 * np.degrees(np.arctan2(sin_sum, cos_sum))) % 180.0)

    def _deskew_and_crop(self, img_bgr: np.ndarray, img_gray: np.ndarray, x1: int, y1: int, x2: int, y2: int):
        w, h = x2 - x1, y2 - y1
        H, W = img_gray.shape[:2]

        pw, ph = int(w * 0.10), int(h * 0.10)
        crop_g = img_gray[max(0, y1 - ph):min(H, y2 + ph), max(0, x1 - pw):min(W, x2 + pw)]

        pred_deg = self._predict_angle(crop_g)
        is_vert = bool(h >= w)
        rot_delta = float((pred_deg - 90.0) if is_vert else (pred_deg if pred_deg <= 90.0 else pred_deg - 180.0))

        aspect_ratio = max(w, h) / max(1.0, min(w, h))
        is_tilted = bool(abs(rot_delta) >= 0.5 and aspect_ratio >= 1.10)

        if is_tilted:
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            w_scaled = (x2 - x1) * 1.05
            h_scaled = float(y2 - y1)
            rad = math.radians(rot_delta)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            hw, hh = w_scaled / 2.0, h_scaled / 2.0
            corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
            poly = [[cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a] for dx, dy in corners]

            pts = np.array(poly, dtype=np.float32)
            tl, tr, br, bl = pts[0], pts[1], pts[2], pts[3]
            target_w = max(8, int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))))
            target_h = max(8, int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))))
            dst_pts = np.array([[0, 0], [target_w - 1, 0], [target_w - 1, target_h - 1], [0, target_h - 1]], dtype=np.float32)
            M = cv2.getPerspectiveTransform(pts, dst_pts)
            crop_bgr = cv2.warpPerspective(img_bgr, M, (target_w, target_h), borderMode=cv2.BORDER_REPLICATE)
        else:
            pad_y = max(2, int(h * 0.05))
            iy1, iy2 = max(0, y1 - pad_y), min(H, y2 + pad_y)
            crop_bgr = img_bgr[iy1:iy2, x1:x2].copy()

        return crop_bgr, rot_delta, is_tilted

    def _detect_chunk(self, img_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
        H, W = img_bgr.shape[:2]
        size = 640
        ratio = min(size / W, size / H)
        nw, nh = int(round(W * ratio)), int(round(H * ratio))
        resized = cv2.resize(img_bgr, (nw, nh))

        pad = np.zeros((size, size, 3), dtype=np.uint8)
        pad_w, pad_h = (size - nw) // 2, (size - nh) // 2
        pad[pad_h : pad_h + nh, pad_w : pad_w + nw] = resized

        inp = (pad.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        inp_sz = np.array([[size, size]], dtype=np.int64)

        _, raw_boxes, raw_scores = self.sess_meiki.run(None, {"images": inp, "orig_target_sizes": inp_sz})
        valid_mask = raw_scores[0] >= self.confidence_threshold
        boxes_norm = raw_boxes[0][valid_mask]

        boxes = []
        for b in boxes_norm:
            x1 = int(max(0, (float(b[0]) - pad_w) / ratio))
            y1 = int(max(0, (float(b[1]) - pad_h) / ratio))
            x2 = int(min(W, (float(b[2]) - pad_w) / ratio))
            y2 = int(min(H, (float(b[3]) - pad_h) / ratio))

            w = x2 - x1
            h = y2 - y1
            if w < 4 or h < 4:
                continue

            if h > w * 1.2:
                x2 = min(W, int(x2 + w * 0.10))

            boxes.append((x1, y1, x2, y2))

        return boxes

    def _ocr_single_chunk(self, img: Image.Image) -> list[Bubble]:
        if img.mode != "RGB":
            img = img.convert("RGB")

        chunk_w, chunk_h = img.size
        img_np = np.array(img)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)

        raw_boxes = self._detect_chunk(img_bgr)
        boxes = self._clean_mechanical_noise(raw_boxes, chunk_w, chunk_h)
        if not boxes:
            return []

        crop_data = []
        for (x1, y1, x2, y2) in boxes:
            crop_bgr, rot_delta, is_tilted = self._deskew_and_crop(img_bgr, img_gray, x1, y1, x2, y2)
            if crop_bgr is None or crop_bgr.size == 0 or crop_bgr.shape[0] < 4 or crop_bgr.shape[1] < 4:
                continue

            pil_crop = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
            is_vert = (y2 - y1) >= (x2 - x1)
            snapped_angle = 90.0 if is_vert else 0.0
            actual_angle = float((snapped_angle + rot_delta) if is_tilted else snapped_angle)

            crop_data.append((pil_crop, (x1, y1, x2, y2), actual_angle))

        if not crop_data:
            return []

        results_bubbles: list[Bubble] = []
        device = self.manga_ocr.model.device

        for i in range(0, len(crop_data), self.batch_size):
            batch_chunk = crop_data[i : i + self.batch_size]
            batch_images = [item[0] for item in batch_chunk]
            batch_boxes = [item[1] for item in batch_chunk]
            batch_angles = [item[2] for item in batch_chunk]

            try:
                pixel_values = self.manga_ocr.processor(
                    batch_images,
                    return_tensors="pt"
                ).pixel_values.to(device)

                with self._ocr_semaphore:
                    generated_ids = self.manga_ocr.model.generate(
                        pixel_values,
                        max_new_tokens=64,
                        num_beams=1,
                    )

                batch_texts = self.manga_ocr.tokenizer.batch_decode(generated_ids, skip_special_tokens=True)

                for text, (x1, y1, x2, y2), angle in zip(batch_texts, batch_boxes, batch_angles):
                    text = re.sub(r'\s+', '', text).strip()
                    if not text or text in self.REJECT_PATTERNS:
                        continue

                    w = float(x2 - x1)
                    h = float(y2 - y1)
                    area = w * h

                    if len(text) == 1:
                        if area < 400 or max(w, h) < 32 or text in self.DROPLET_CHARS or text.isascii():
                            continue

                    results_bubbles.append(Bubble(
                        text=text,
                        tightBoundingBox=BoundingBox(
                            x=float(x1 / chunk_w),
                            y=float(y1 / chunk_h),
                            width=float(w / chunk_w),
                            height=float(h / chunk_h)
                        ),
                        orientation=float(round(angle, 1)),
                        font_size=0.04,
                        confidence=0.96
                    ))

            except Exception as e:
                print(f"[Warning] MangaOCR-Torch batch failed at {i}: {e}")
                continue

        return results_bubbles

    async def ocr(self, img: Image.Image) -> list[Bubble]:
        return await self._process_webtoon_chunked(img, self._ocr_single_chunk, "MangaOCR-Torch")

class MangaOCRDirectML(Engine):
    """
    Hybrid Manga/Webtoon OCR Engine:
    - Text Detection & Deskewing: Meiki Small v0 ONNX + AngleNet Distill 96x96 FP16 (CPU)
    - Recognition: Manga OCR (TrOCR ONNX via Optimum/Transformers on DirectML GPU / CPU)
    - Lens-style webtoon chunking with auto-unloading on standby
    """

    DROPLET_CHARS: set[str] = {
        "し", "つ", "ひ", "く", "ノ", "と", "へ", "S", "s", "2", "1",
        "c", "C", "I", "l", "ー", "〜", "・", "、", "…", ".."
    }
    REJECT_PATTERNS: set[str] = {"..", "...", "……"}

    def __init__(
        self,
        confidence_threshold: float = 0.44,
        target_angle_size: int = 96,
        batch_size: int = 32,
        timeout_seconds: int = 600,
        pretrained_mangaocr_path: str = "mangaocronnx"
    ):
        super().__init__()
        self.confidence_threshold = confidence_threshold
        self.target_angle_size = target_angle_size
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

        self.CHUNK_HEIGHT = 2500
        self.OVERLAP = 350

        self.ANGLE_BINS = np.arange(180, dtype=np.float32)
        self.RAD_BINS = np.radians(2.0 * self.ANGLE_BINS)
        self.SIN_BINS = np.sin(self.RAD_BINS)
        self.COS_BINS = np.cos(self.RAD_BINS)

        self._lock = threading.Lock()
        self._gpu_semaphore = threading.Semaphore(1)
        self.last_access_time = time.time()
        self.models_loaded = False

        self.sess_meiki = None
        self.sess_angle = None
        self.angle_is_fp16 = False
        self.processor = None
        self.recognition_model = None

        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()

        self.models_dir = os.path.join(script_dir, "models")
        os.makedirs(self.models_dir, exist_ok=True)

        self.pretrained_mangaocr_path = pretrained_mangaocr_path
        self._verify_and_download_model_files()
        self._load_models()

        monitor_thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        monitor_thread.start()

    def _verify_and_download_model_files(self):
        self.meiki_path = os.path.join(self.models_dir, "meiki.text.detect.small.v0.onnx")
        if not os.path.exists(self.meiki_path):
            print("[MangaOCRDirectML] Downloading Meiki detector model...")
            self.meiki_path = hf_hub_download(
                repo_id="rtr46/meiki.text.detect.v0",
                filename="meiki.text.detect.small.v0.onnx",
                local_dir=self.models_dir
            )

        angle_fn = "anglenet_v0_1_distill_96x96_fp16.onnx"
        self.angle_path = os.path.join(self.models_dir, angle_fn)
        if not os.path.exists(self.angle_path):
            print(f"[MangaOCRDirectML] Downloading AngleNet model '{angle_fn}'...")
            try:
                self.angle_path = hf_hub_download(
                    repo_id="Kellenok/anglenet",
                    filename=angle_fn,
                    local_dir=self.models_dir
                )
            except Exception:
                angle_fn = "anglenet_v0_1_distill_96x96.onnx"
                self.angle_path = hf_hub_download(
                    repo_id="Kellenok/anglenet",
                    filename=angle_fn,
                    local_dir=self.models_dir
                )

        if os.path.isabs(self.pretrained_mangaocr_path):
            local_ocr_path = self.pretrained_mangaocr_path
        else:
            local_ocr_path = os.path.join(self.models_dir, self.pretrained_mangaocr_path)

        os.makedirs(local_ocr_path, exist_ok=True)

        required_ocr_files = [
            ("config.json", None),
            ("generation_config.json", None),
            ("preprocessor_config.json", None),
            ("special_tokens_map.json", None),
            ("tokenizer.json", None),
            ("tokenizer_config.json", None),
            ("vocab.txt", None),
            ("encoder_model.onnx", "onnx"),
            ("decoder_model_merged.onnx", "onnx")
        ]

        print(f"[MangaOCRDirectML] Verifying Manga OCR files in: {local_ocr_path}")
        for file_name, subfolder in required_ocr_files:
            file_path = os.path.join(local_ocr_path, file_name)
            if not os.path.exists(file_path):
                print(f"[MangaOCRDirectML] Downloading '{file_name}' from Hugging Face...")
                if subfolder:
                    hf_file_path = f"{subfolder}/{file_name}"
                    downloaded_path = hf_hub_download(
                        repo_id="xingliao/manga-ocr-onnx-full",
                        filename=hf_file_path,
                        local_dir=local_ocr_path
                    )
                    shutil.move(downloaded_path, file_path)
                    try:
                        os.rmdir(os.path.dirname(downloaded_path))
                    except OSError:
                        pass
                else:
                    hf_hub_download(
                        repo_id="xingliao/manga-ocr-onnx-full",
                        filename=file_name,
                        local_dir=local_ocr_path
                    )

        self.ocr_model_source = local_ocr_path

    def _load_models(self):
        if self.models_loaded:
            return

        print("[MangaOCRDirectML] Loading Meiki, AngleNet & MangaOCR models.")
        try:
            cpu_opts = ort.SessionOptions()
            cpu_opts.intra_op_num_threads = min(4, os.cpu_count() or 4)
            cpu_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            cpu_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

            self.sess_meiki = ort.InferenceSession(self.meiki_path, sess_options=cpu_opts, providers=['CPUExecutionProvider'])
            self.sess_angle = ort.InferenceSession(self.angle_path, sess_options=cpu_opts, providers=['CPUExecutionProvider'])
            self.angle_is_fp16 = "float16" in self.sess_angle.get_inputs()[0].type

            from transformers import TrOCRProcessor
            from optimum.onnxruntime import ORTModelForVision2Seq

            sess_options = ort.SessionOptions()
            sess_options.log_severity_level = 3
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            sess_options.enable_mem_pattern = True

            self.processor = TrOCRProcessor.from_pretrained(self.ocr_model_source, use_fast=True)

            try:
                self.recognition_model = ORTModelForVision2Seq.from_pretrained(
                    self.ocr_model_source,
                    provider="DmlExecutionProvider",
                    provider_options={"device_id": 0},
                    use_merged=True,
                    use_io_binding=False,
                    export=False,
                    session_options=sess_options
                )
                print("[Info] Manga OCR loaded with DirectML GPU acceleration.")
            except Exception as e:
                print(f"[Warning] DirectML init failed ({e}), falling back to CPU...")
                self.recognition_model = ORTModelForVision2Seq.from_pretrained(
                    self.ocr_model_source,
                    provider="CPUExecutionProvider",
                    use_merged=True,
                    use_io_binding=False,
                    export=False,
                    session_options=sess_options
                )

            self.models_loaded = True
            self.last_access_time = time.time()
            print("[MangaOCRDirectML] All models loaded successfully.")

        except Exception as e:
            print(f"[Error] Failed to load MangaOCRDirectML pipeline: {e}")
            raise

    def _unload_models(self):
        if not self.models_loaded:
            return

        print(f"[MangaOCRDirectML] Standby timeout ({self.timeout_seconds}s) reached. Unloading models.")
        self.sess_meiki = None
        self.sess_angle = None
        self.processor = None
        self.recognition_model = None
        self.models_loaded = False
        gc.collect()

    def _maintenance_loop(self):
        while True:
            time.sleep(10)
            with self._lock:
                if self.models_loaded and (time.time() - self.last_access_time > self.timeout_seconds):
                    self._unload_models()

    def _clean_mechanical_noise(self, boxes: list[tuple[int, int, int, int]], W: int, H: int, min_area: int = 120, min_side: int = 8):
        filtered = []
        for (x1, y1, x2, y2) in boxes:
            w, h = x2 - x1, y2 - y1
            if w * h < min_area or min(w, h) < min_side:
                continue
            if (x1 <= 3 or x2 >= W - 3 or y1 <= 3 or y2 >= H - 3) and (w < 10 or h < 10):
                continue
            filtered.append((x1, y1, x2, y2))
        return filtered

    def _predict_angle(self, crop_gray: np.ndarray) -> float:
        ch, cw = crop_gray.shape[:2]
        if ch <= 0 or cw <= 0:
            return 0.0
        scale = min(self.target_angle_size / cw, self.target_angle_size / ch)
        nw, nh = max(1, int(cw * scale)), max(1, int(ch * scale))
        resized = cv2.resize(crop_gray, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR)
        pad = np.zeros((self.target_angle_size, self.target_angle_size), dtype=np.uint8)
        pad[(self.target_angle_size - nh) // 2 : (self.target_angle_size - nh) // 2 + nh,
            (self.target_angle_size - nw) // 2 : (self.target_angle_size - nw) // 2 + nw] = resized

        inp = (pad.astype(np.float16 if self.angle_is_fp16 else np.float32) / 255.0)[None, None, :, :]
        angle_outs = self.sess_angle.run(None, {self.sess_angle.get_inputs()[0].name: inp})
        logits = angle_outs[0][0].astype(np.float32)

        probs = np.exp(logits - np.max(logits))
        probs /= np.sum(probs)

        sin_sum = float(np.sum(probs * self.SIN_BINS))
        cos_sum = float(np.sum(probs * self.COS_BINS))
        return float((0.5 * np.degrees(np.arctan2(sin_sum, cos_sum))) % 180.0)

    def _deskew_and_crop(self, img_bgr: np.ndarray, img_gray: np.ndarray, x1: int, y1: int, x2: int, y2: int):
        w, h = x2 - x1, y2 - y1
        H, W = img_gray.shape[:2]

        pw, ph = int(w * 0.10), int(h * 0.10)
        crop_g = img_gray[max(0, y1 - ph):min(H, y2 + ph), max(0, x1 - pw):min(W, x2 + pw)]

        pred_deg = self._predict_angle(crop_g)
        is_vert = bool(h >= w)
        rot_delta = float((pred_deg - 90.0) if is_vert else (pred_deg if pred_deg <= 90.0 else pred_deg - 180.0))

        aspect_ratio = max(w, h) / max(1.0, min(w, h))
        is_tilted = bool(abs(rot_delta) >= 0.5 and aspect_ratio >= 1.10)

        if is_tilted:
            cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            w_scaled = (x2 - x1) * 1.05
            h_scaled = float(y2 - y1)
            rad = math.radians(rot_delta)
            cos_a, sin_a = math.cos(rad), math.sin(rad)
            hw, hh = w_scaled / 2.0, h_scaled / 2.0
            corners = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
            poly = [[cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a] for dx, dy in corners]

            pts = np.array(poly, dtype=np.float32)
            tl, tr, br, bl = pts[0], pts[1], pts[2], pts[3]
            target_w = max(8, int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))))
            target_h = max(8, int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))))
            dst_pts = np.array([[0, 0], [target_w - 1, 0], [target_w - 1, target_h - 1], [0, target_h - 1]], dtype=np.float32)
            M = cv2.getPerspectiveTransform(pts, dst_pts)
            crop_bgr = cv2.warpPerspective(img_bgr, M, (target_w, target_h), borderMode=cv2.BORDER_REPLICATE)
        else:
            pad_y = max(2, int(h * 0.05))
            iy1, iy2 = max(0, y1 - pad_y), min(H, y2 + pad_y)
            crop_bgr = img_bgr[iy1:iy2, x1:x2].copy()

        return crop_bgr, rot_delta, is_tilted

    def _detect_chunk(self, img_bgr: np.ndarray) -> list[tuple[int, int, int, int]]:
        H, W = img_bgr.shape[:2]
        size = 640
        ratio = min(size / W, size / H)
        nw, nh = int(round(W * ratio)), int(round(H * ratio))
        resized = cv2.resize(img_bgr, (nw, nh))

        pad = np.zeros((size, size, 3), dtype=np.uint8)
        pad_w, pad_h = (size - nw) // 2, (size - nh) // 2
        pad[pad_h : pad_h + nh, pad_w : pad_w + nw] = resized

        inp = (pad.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]
        inp_sz = np.array([[size, size]], dtype=np.int64)

        _, raw_boxes, raw_scores = self.sess_meiki.run(None, {"images": inp, "orig_target_sizes": inp_sz})
        valid_mask = raw_scores[0] >= self.confidence_threshold
        boxes_norm = raw_boxes[0][valid_mask]

        boxes = []
        for b in boxes_norm:
            x1 = int(max(0, (float(b[0]) - pad_w) / ratio))
            y1 = int(max(0, (float(b[1]) - pad_h) / ratio))
            x2 = int(min(W, (float(b[2]) - pad_w) / ratio))
            y2 = int(min(H, (float(b[3]) - pad_h) / ratio))

            w = x2 - x1
            h = y2 - y1
            if w < 4 or h < 4:
                continue

            if h > w * 1.2:
                x2 = min(W, int(x2 + w * 0.10))

            boxes.append((x1, y1, x2, y2))

        return boxes

    def _ocr_single_chunk(self, img: Image.Image) -> list[Bubble]:
        if img.mode != "RGB":
            img = img.convert("RGB")

        chunk_w, chunk_h = img.size
        img_np = np.array(img)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)
        img_gray = cv2.cvtColor(img_np, cv2.COLOR_RGB2GRAY)

        raw_boxes = self._detect_chunk(img_bgr)
        boxes = self._clean_mechanical_noise(raw_boxes, chunk_w, chunk_h)
        if not boxes:
            return []

        crop_data = []
        for (x1, y1, x2, y2) in boxes:
            crop_bgr, rot_delta, is_tilted = self._deskew_and_crop(img_bgr, img_gray, x1, y1, x2, y2)
            if crop_bgr is None or crop_bgr.size == 0 or crop_bgr.shape[0] < 4 or crop_bgr.shape[1] < 4:
                continue

            pil_crop = Image.fromarray(cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2RGB))
            is_vert = (y2 - y1) >= (x2 - x1)
            snapped_angle = 90.0 if is_vert else 0.0
            actual_angle = float((snapped_angle + rot_delta) if is_tilted else snapped_angle)

            crop_data.append((pil_crop, (x1, y1, x2, y2), actual_angle))

        if not crop_data:
            return []

        results_bubbles: list[Bubble] = []
        for i in range(0, len(crop_data), self.batch_size):
            batch_chunk = crop_data[i : i + self.batch_size]
            batch_images = [item[0] for item in batch_chunk]
            batch_boxes = [item[1] for item in batch_chunk]
            batch_angles = [item[2] for item in batch_chunk]

            try:
                pixel_values = self.processor(
                    images=batch_images,
                    return_tensors="pt",
                    padding=True
                ).pixel_values

                with self._gpu_semaphore:
                    generated_ids = self.recognition_model.generate(
                        pixel_values,
                        max_new_tokens=64,
                        num_beams=1,
                        use_cache=False,
                    )

                batch_texts = self.processor.batch_decode(generated_ids, skip_special_tokens=True)

                for text, (x1, y1, x2, y2), angle in zip(batch_texts, batch_boxes, batch_angles):
                    text = re.sub(r'\s+', '', text).strip()
                    if not text or text in self.REJECT_PATTERNS:
                        continue

                    w = float(x2 - x1)
                    h = float(y2 - y1)
                    area = w * h

                    if len(text) == 1:
                        if area < 400 or max(w, h) < 32 or text in self.DROPLET_CHARS or text.isascii():
                            continue

                    results_bubbles.append(Bubble(
                        text=text,
                        tightBoundingBox=BoundingBox(
                            x=float(x1 / chunk_w),
                            y=float(y1 / chunk_h),
                            width=float(w / chunk_w),
                            height=float(h / chunk_h)
                        ),
                        orientation=float(round(angle, 1)),
                        font_size=0.04,
                        confidence=0.96
                    ))

            except Exception as e:
                print(f"[Warning] MangaOCRDirectML batch failed at {i}: {e}")
                continue

        return results_bubbles

    async def ocr(self, img: Image.Image) -> list[Bubble]:
        with self._lock:
            self.last_access_time = time.time()
            if not self.models_loaded:
                self._load_models()

        return await self._process_webtoon_chunked(img, self._ocr_single_chunk, "MangaOCRDirectML")


class PPOCRv6Manga(Engine):
    """
    All-in-One Japanese Manga & Webtoon OCR Engine:
    - Detector: DBNet Manga Detector v0.2 FP16 (DirectML GPU / CPU fallback)
    - Recognizer: PP-OCRv6 Manga v0.2 fine-tune (DirectML GPU / CPU fallback)
    - Full Page Presets: thresh=0.15, box_thresh=0.25, unclip_ratio=1.4
    - Reading Order v2: Block-aware speech balloon union-find clustering
    - DirectML Batching & Standby auto-unloading
    - Lens-style webtoon chunking
    """

    HF_REPO_ID = "Kellenok/PP-OCRv6_manga"
    DROPLET_CHARS: set[str] = {
        "し", "つ", "ひ", "く", "ノ", "と", "つっ", "へ", "S", "s", "2", "1", "c", "C", "I", "l", "ー", "〜", "・", "、", "…", ".."
    }
    REJECT_PATTERNS: set[str] = {"..", "...", "……"}

    def __init__(
        self,
        thresh: float = 0.15,
        box_thresh: float = 0.25,
        unclip_ratio: float = 1.4,
        filter_furigana: bool = False,
        rec_confidence_threshold: float = 0.35,
        batch_size: int = 32,
        timeout_seconds: int = 600
    ):
        super().__init__()
        self.thresh = thresh
        self.box_thresh = box_thresh
        self.unclip_ratio = unclip_ratio
        self.filter_furigana = filter_furigana
        self.rec_confidence_threshold = rec_confidence_threshold
        self.batch_size = batch_size
        self.timeout_seconds = timeout_seconds

        self.CHUNK_HEIGHT = 2500
        self.OVERLAP = 350

        self._lock = threading.Lock()
        self._gpu_semaphore = threading.Semaphore(1)
        self.last_access_time = time.time()
        self.models_loaded = False

        self.sess_det = None
        self.sess_rec = None
        self.det_is_fp16 = False
        self.rec_is_fp16 = False
        self.can_batch_rec = False

        try:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        except NameError:
            script_dir = os.getcwd()

        self.models_dir = os.path.join(script_dir, "models", "kellen_ppocr")
        os.makedirs(self.models_dir, exist_ok=True)

        self._load_dictionary()
        self._verify_and_download_model_files()
        self._load_models()

        monitor_thread = threading.Thread(target=self._maintenance_loop, daemon=True)
        monitor_thread.start()

    def _resolve_or_download(self, subpath: str) -> str:
        candidates = [
            os.path.join(self.models_dir, subpath),
            os.path.join(self.models_dir, os.path.basename(subpath)),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c
        if hf_hub_download is not None:
            print(f"[PPOCRv6Manga] Downloading '{subpath}' from {self.HF_REPO_ID}...", flush=True)
            return hf_hub_download(repo_id=self.HF_REPO_ID, filename=subpath, local_dir=self.models_dir)
        raise FileNotFoundError(f"Model file {subpath} not found.")

    def _load_dictionary(self):
        dict_path = os.path.join(self.models_dir, "ppocrv6_dict.txt")
        if not os.path.exists(dict_path):
            try:
                dict_path = self._resolve_or_download("ppocrv6_dict.txt")
            except Exception:
                url = "https://raw.githubusercontent.com/PaddlePaddle/PaddleOCR/main/ppocr/utils/dict/ppocrv6_dict.txt"
                urllib.request.urlretrieve(url, dict_path)

        self.dict_chars = ["blank"]
        if os.path.exists(dict_path):
            with open(dict_path, encoding="utf-8") as f:
                for line in f:
                    self.dict_chars.append(line.rstrip("\r\n"))
        self.dict_chars.append(" ")

    def _verify_and_download_model_files(self):
        try:
            self.det_path = self._resolve_or_download("det/manga_det_v0.2_fp16.onnx")
        except Exception:
            self.det_path = self._resolve_or_download("det/manga_det_v0.2.onnx")

        try:
            self.rec_path = self._resolve_or_download("rec/manga_rec_v0.2_fp16.onnx")
        except Exception:
            self.rec_path = self._resolve_or_download("rec/manga_rec_v0.2.onnx")

    def _load_models(self):
        if self.models_loaded:
            return

        print("[PPOCRv6Manga] Initializing v0.2 models on DirectML GPU / CPU...", flush=True)
        try:
            available = ort.get_available_providers()
            use_dml = 'DmlExecutionProvider' in available
            providers = ['DmlExecutionProvider', 'CPUExecutionProvider'] if use_dml else ['CPUExecutionProvider']

            sess_opts = ort.SessionOptions()
            sess_opts.intra_op_num_threads = min(4, os.cpu_count() or 4)
            sess_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

            try:
                self.sess_det = ort.InferenceSession(self.det_path, sess_options=sess_opts, providers=providers)
            except Exception as e:
                print(f"[PPOCRv6Manga] Detector fallback to CPU ({e})...")
                self.sess_det = ort.InferenceSession(self.det_path, sess_options=sess_opts, providers=['CPUExecutionProvider'])

            self.det_is_fp16 = "float16" in self.sess_det.get_inputs()[0].type

            try:
                self.sess_rec = ort.InferenceSession(self.rec_path, sess_options=sess_opts, providers=providers)
            except Exception as e:
                print(f"[PPOCRv6Manga] Recognizer fallback to CPU ({e})...")
                cpu_opts = ort.SessionOptions()
                cpu_opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
                self.sess_rec = ort.InferenceSession(self.rec_path, sess_options=cpu_opts, providers=['CPUExecutionProvider'])

            self.rec_is_fp16 = "float16" in self.sess_rec.get_inputs()[0].type
            rec_in_shape = self.sess_rec.get_inputs()[0].shape
            self.can_batch_rec = (isinstance(rec_in_shape[0], str) or rec_in_shape[0] is None or rec_in_shape[0] == -1)

            self.models_loaded = True
            self.last_access_time = time.time()
            print("[PPOCRv6Manga] Pipeline loaded successfully.", flush=True)

        except Exception as e:
            print(f"[Error] Failed to initialize PPOCRv6Manga: {e}")
            raise

    def _unload_models(self):
        if not self.models_loaded:
            return
        print(f"[PPOCRv6Manga] Standby timeout ({self.timeout_seconds}s) reached. Unloading models.")
        self.sess_det = None
        self.sess_rec = None
        self.models_loaded = False
        gc.collect()

    def _maintenance_loop(self):
        while True:
            time.sleep(10)
            with self._lock:
                if self.models_loaded and (time.time() - self.last_access_time > self.timeout_seconds):
                    self._unload_models()

    @staticmethod
    def _unclip_pp(box: np.ndarray, ratio: float = 1.4) -> Optional[np.ndarray]:
        poly = Polygon(box)
        if poly.area <= 0 or poly.length <= 0:
            return None
        distance = poly.area * ratio / poly.length
        off = pyclipper.PyclipperOffset()
        off.AddPath(box, pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        paths = off.Execute(distance)
        if not paths:
            return None
        paths = sorted(paths, key=lambda p: Polygon(np.array(p)).area if len(p) >= 3 else 0.0, reverse=True)
        return np.array(paths[0], dtype=np.float32)

    @staticmethod
    def _box_score_fast(pred: np.ndarray, box: np.ndarray) -> float:
        h, w = pred.shape[:2]
        b = box.copy().astype(np.int32)
        xmin = np.clip(np.floor(b[:, 0].min()).astype(np.int32), 0, w - 1)
        xmax = np.clip(np.ceil(b[:, 0].max()).astype(np.int32), 0, w - 1)
        ymin = np.clip(np.floor(b[:, 1].min()).astype(np.int32), 0, h - 1)
        ymax = np.clip(np.ceil(b[:, 1].max()).astype(np.int32), 0, h - 1)
        if xmax < xmin or ymax < ymin:
            return 0.0
        mask = np.zeros((ymax - ymin + 1, xmax - xmin + 1), dtype=np.uint8)
        bb = b.copy()
        bb[:, 0] -= xmin
        bb[:, 1] -= ymin
        cv2.fillPoly(mask, [bb.reshape(-1, 2)], 1)
        return float(cv2.mean(pred[ymin:ymax + 1, xmin:xmax + 1], mask)[0])

    @staticmethod
    def _contains_kanji(text: str) -> bool:
        return any(('\u4e00' <= ch <= '\u9fff') or ('\u3400' <= ch <= '\u4dbf') for ch in text)

    @classmethod
    def _is_furigana_pair(cls, sub_line: dict, main_line: dict, is_vertical: bool = True) -> bool:
        text_sub = sub_line['text'].strip()
        text_main = main_line['text'].strip()
        if not text_sub or not text_main:
            return False

        if cls._contains_kanji(text_sub):
            return False
        if any(p in text_sub for p in ['「', '」', '『', '』', '！', '？', '!', '?', '…', '。', '、', '―', 'ー']):
            return False
        if len(text_sub) > 8:
            return False

        if not cls._contains_kanji(text_main):
            return False

        box_sub = sub_line['box']
        box_main = main_line['box']

        sub_w = box_sub['xmax'] - box_sub['xmin']
        sub_h = box_sub['ymax'] - box_sub['ymin']
        main_w = box_main['xmax'] - box_main['xmin']
        main_h = box_main['ymax'] - box_main['ymin']

        sub_thickness = min(sub_w, sub_h)
        main_thickness = min(main_w, main_h)

        if sub_thickness > 32.0 or sub_thickness > main_thickness * 0.70:
            return False

        sub_len = max(sub_w, sub_h)
        main_len = max(main_w, main_h)
        if sub_len > main_len * 1.05:
            return False

        if len(text_sub) >= 3:
            char_size_sub = sub_len / max(1, len(text_sub))
            char_size_main = main_len / max(1, len(text_main))
            if char_size_sub >= char_size_main * 0.85:
                return False

        proximity_limit = min(16.0, max(8.0, main_thickness * 0.35))
        overlap_ratio = 0.05

        if is_vertical:
            if box_sub['cx'] <= box_main['cx']:
                return False
            oy = max(0.0, min(box_sub['ymax'], box_main['ymax']) - max(box_sub['ymin'], box_main['ymin']))
            if (oy / max(0.001, min(sub_h, main_h))) < overlap_ratio:
                return False
            return max(0.0, box_sub['xmin'] - box_main['xmax']) <= proximity_limit
        else:
            if box_sub['cy'] >= box_main['cy']:
                return False
            ox = max(0.0, min(box_sub['xmax'], box_main['xmax']) - max(box_sub['xmin'], box_main['xmin']))
            if (ox / max(0.001, min(sub_w, main_w))) < overlap_ratio:
                return False
            return max(0.0, box_main['ymin'] - box_sub['ymax']) <= proximity_limit

    @staticmethod
    def _get_rotate_crop_image(img: np.ndarray, points: list) -> tuple[np.ndarray, np.ndarray]:
        points = np.array(points, dtype=np.float32)
        rect = np.zeros((4, 2), dtype="float32")
        s = points.sum(axis=1)
        rect[0] = points[np.argmin(s)]
        rect[2] = points[np.argmax(s)]
        diff = np.diff(points, axis=1)
        rect[1] = points[np.argmin(diff)]
        rect[3] = points[np.argmax(diff)]
        (tl, tr, br, bl) = rect

        maxWidth = max(int(np.hypot(br[0] - bl[0], br[1] - bl[1])), int(np.hypot(tr[0] - tl[0], tr[1] - tl[1])))
        maxHeight = max(int(np.hypot(tr[0] - br[0], tr[1] - br[1])), int(np.hypot(tl[0] - bl[0], tr[1] - bl[1])))

        if maxWidth <= 0 or maxHeight <= 0:
            return np.zeros((10, 10, 3), dtype=np.uint8), points

        dst = np.array([[0, 0], [maxWidth - 1, 0], [maxWidth - 1, maxHeight - 1], [0, maxHeight - 1]], dtype="float32")
        M = cv2.getPerspectiveTransform(rect, dst)
        warped = cv2.warpPerspective(img, M, (maxWidth, maxHeight), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_LINEAR)

        new_points = points
        if maxHeight > maxWidth and (maxWidth / float(maxHeight)) >= 0.25 and maxWidth >= 24:
            gray = cv2.cvtColor(warped, cv2.COLOR_BGR2GRAY) if warped.ndim == 3 else warped
            _, bin_img = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            proj = np.sum(bin_img, axis=0)
            x1, x2 = 0, maxWidth

            r_start, r_end = int(maxWidth * 0.55), int(maxWidth * 0.90)
            zero_cols = [x for x in range(r_start, r_end) if proj[x] == 0]
            if zero_cols:
                split_r = zero_cols[0]
                right_mask = bin_img[:, split_r:]
                right_rows = np.where(np.sum(right_mask, axis=1) > 0)[0]
                if len(right_rows) >= 8:
                    left_mask = bin_img[:, :split_r]
                    left_rows = np.sum(left_mask, axis=1) > 0
                    overlap = np.sum(left_rows[right_rows]) if len(right_rows) else 0
                    alongside_ratio = overlap / float(len(right_rows)) if len(right_rows) else 0
                    right_area = np.sum(right_mask > 0)
                    left_area = np.sum(left_mask > 0)
                    if alongside_ratio >= 0.85 and 15 <= right_area <= 0.35 * left_area and right_rows[0] < int(maxHeight * 0.75):
                        x2 = split_r

            l_start = max(3, int(maxWidth * 0.10))
            l_end = int(maxWidth * 0.25)
            l_zeros = [x for x in range(l_start, l_end) if proj[x] == 0]
            if l_zeros:
                split_l = l_zeros[-1] + 1
                left_mask = bin_img[:, :split_l]
                left_rows = np.where(np.sum(left_mask, axis=1) > 0)[0]
                if len(left_rows) >= 8:
                    mid_mask = bin_img[:, split_l:x2]
                    mid_rows = np.sum(mid_mask, axis=1) > 0
                    overlap = np.sum(mid_rows[left_rows]) if len(left_rows) else 0
                    alongside_ratio = overlap / float(len(left_rows)) if len(left_rows) else 0
                    left_area = np.sum(left_mask > 0)
                    mid_area = np.sum(mid_mask > 0)
                    if alongside_ratio >= 0.85 and 15 <= left_area <= 0.35 * mid_area and left_rows[0] < int(maxHeight * 0.75):
                        x1 = split_l

            if (x2 - x1) >= 16 and (x1 > 0 or x2 < maxWidth):
                warped = warped[:, x1:x2]
                _, M_inv = cv2.invert(M)
                trimmed_corners = np.array([[[x1, 0], [x2 - 1, 0], [x2 - 1, maxHeight - 1], [x1, maxHeight - 1]]], dtype=np.float32)
                new_points = cv2.perspectiveTransform(trimmed_corners, M_inv)[0]

        return warped, new_points

    @staticmethod
    def _clean_manga_vertical_crop(crop_bgr: np.ndarray) -> np.ndarray:
        if crop_bgr is None or crop_bgr.size == 0 or crop_bgr.shape[0] < 4 or crop_bgr.shape[1] < 4:
            return crop_bgr
        was_horizontal = (crop_bgr.shape[0] < crop_bgr.shape[1])
        c = cv2.rotate(crop_bgr, cv2.ROTATE_90_CLOCKWISE) if was_horizontal else crop_bgr

        h, w = c.shape[:2]
        if h <= w or (w / float(h)) < 0.25 or w < 24:
            return crop_bgr

        gray = cv2.cvtColor(c, cv2.COLOR_BGR2GRAY) if len(c.shape) == 3 else c
        _, bin_img = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        proj = np.sum(bin_img, axis=0)
        x1, x2 = 0, w

        r_start, r_end = int(w * 0.55), int(w * 0.90)
        zero_cols = [x for x in range(r_start, r_end) if proj[x] == 0]
        if zero_cols:
            split_r = zero_cols[0]
            right_mask = bin_img[:, split_r:]
            right_rows = np.where(np.sum(right_mask, axis=1) > 0)[0]
            if len(right_rows) >= 8:
                left_mask = bin_img[:, :split_r]
                left_rows = np.sum(left_mask, axis=1) > 0
                overlap = np.sum(left_rows[right_rows]) if len(right_rows) else 0
                alongside_ratio = overlap / float(len(right_rows)) if len(right_rows) else 0
                right_area = np.sum(right_mask > 0)
                left_area = np.sum(left_mask > 0)
                if alongside_ratio >= 0.85 and 15 <= right_area <= 0.35 * left_area and right_rows[0] < int(h * 0.75):
                    x2 = split_r

        l_start = max(3, int(w * 0.10))
        l_end = int(w * 0.25)
        l_zeros = [x for x in range(l_start, l_end) if proj[x] == 0]
        if l_zeros:
            split_l = l_zeros[-1] + 1
            left_mask = bin_img[:, :split_l]
            left_rows = np.where(np.sum(left_mask, axis=1) > 0)[0]
            if len(left_rows) >= 8:
                mid_mask = bin_img[:, split_l:x2]
                mid_rows = np.sum(mid_mask, axis=1) > 0
                overlap = np.sum(mid_rows[left_rows]) if len(left_rows) else 0
                alongside_ratio = overlap / float(len(left_rows)) if len(left_rows) else 0
                left_area = np.sum(left_mask > 0)
                mid_area = np.sum(mid_mask > 0)
                if alongside_ratio >= 0.85 and 15 <= left_area <= 0.35 * mid_area and left_rows[0] < int(h * 0.75):
                    x1 = split_l

        if (x2 - x1) >= 16:
            c_clean = c[:, x1:x2]
            return cv2.rotate(c_clean, cv2.ROTATE_90_COUNTERCLOCKWISE) if was_horizontal else c_clean
        return crop_bgr

    @staticmethod
    def _vert(b: dict) -> bool:
        return (b["ymax"] - b["ymin"]) > (b["xmax"] - b["xmin"])

    @classmethod
    def _thick(cls, b: dict) -> float:
        return (b["xmax"] - b["xmin"]) if cls._vert(b) else (b["ymax"] - b["ymin"])

    @classmethod
    def _touch(cls, a: dict, b: dict, grow: float) -> bool:
        ta, tb = cls._thick(a), cls._thick(b)
        g = grow * min(ta, tb)
        return not (a["xmax"] + g < b["xmin"] or b["xmax"] + g < a["xmin"] or
                    a["ymax"] + g < b["ymin"] or b["ymax"] + g < a["ymin"])

    @staticmethod
    def _order_lines(ls: list[dict], vert: bool) -> list[dict]:
        if vert:
            cols = []
            for b in sorted(ls, key=lambda b: -b["cx"]):
                for c in cols:
                    if min(b["xmax"], c["xmax"]) - max(b["xmin"], c["xmin"]) > 0.5 * min(b["xmax"] - b["xmin"], c["xmax"] - c["xmin"]):
                        c["ls"].append(b)
                        c["xmin"] = min(c["xmin"], b["xmin"])
                        c["xmax"] = max(c["xmax"], b["xmax"])
                        break
                else:
                    cols.append({"ls": [b], "xmin": b["xmin"], "xmax": b["xmax"]})
            cols.sort(key=lambda c: -(c["xmin"] + c["xmax"]))
            return [b for c in cols for b in sorted(c["ls"], key=lambda b: b["ymin"])]

        rows = []
        for b in sorted(ls, key=lambda b: b["cy"]):
            for r in rows:
                if min(b["ymax"], r["ymax"]) - max(b["ymin"], r["ymin"]) > 0.5 * min(b["ymax"] - b["ymin"], r["ymax"] - r["ymin"]):
                    r["ls"].append(b)
                    r["ymin"] = min(r["ymin"], b["ymin"])
                    r["ymax"] = max(r["ymax"], b["ymax"])
                    break
            else:
                rows.append({"ls": [b], "ymin": b["ymin"], "ymax": b["ymax"]})
        rows.sort(key=lambda r: r["ymin"] + r["ymax"])
        return [b for r in rows for b in sorted(r["ls"], key=lambda b: b["xmin"])]

    @classmethod
    def _sort_reading_order_v2(cls, boxes: list[dict], is_vertical: bool = True, grow: float = 0.6) -> list[dict]:
        if not boxes:
            return []
        n = len(boxes)
        par = list(range(n))

        def find(i):
            while par[i] != i:
                par[i] = par[par[i]]
                i = par[i]
            return i

        for i in range(n):
            for j in range(i + 1, n):
                if cls._vert(boxes[i]) == cls._vert(boxes[j]) and cls._touch(boxes[i], boxes[j], grow):
                    par[find(i)] = find(j)

        blocks = {}
        for i in range(n):
            blocks.setdefault(find(i), []).append(boxes[i])

        B = []
        for ls in blocks.values():
            if len(ls) > 1:
                v = sum(cls._vert(b) for b in ls) * 2 >= len(ls)
            else:
                v = cls._vert(ls[0]) if abs((ls[0]["ymax"] - ls[0]["ymin"]) - (ls[0]["xmax"] - ls[0]["xmin"])) > 4 else is_vertical

            B.append({
                "ls": cls._order_lines(ls, v),
                "xmin": min(b["xmin"] for b in ls),
                "xmax": max(b["xmax"] for b in ls),
                "ymin": min(b["ymin"] for b in ls),
                "ymax": max(b["ymax"] for b in ls)
            })

        B.sort(key=lambda b: b["ymin"])
        bands = []
        for b in B:
            for bd in bands:
                if min(b["ymax"], bd["ymax"]) - max(b["ymin"], bd["ymin"]) > 0.3 * min(b["ymax"] - b["ymin"], bd["ymax"] - bd["ymin"]):
                    bd["bs"].append(b)
                    bd["ymin"] = min(bd["ymin"], b["ymin"])
                    bd["ymax"] = max(bd["ymax"], b["ymax"])
                    break
            else:
                bands.append({"bs": [b], "ymin": b["ymin"], "ymax": b["ymax"]})

        out = []
        for bd in sorted(bands, key=lambda d: d["ymin"]):
            for b in sorted(bd["bs"], key=lambda b: -(b["xmin"] + b["xmax"]) if is_vertical else (b["xmin"] + b["xmax"])):
                out += b["ls"]
        return out

    def _detect_boxes_dbnet(self, img_bgr: np.ndarray) -> list[dict]:
        H, W = img_bgr.shape[:2]

        margin = 16
        pad_img = cv2.copyMakeBorder(img_bgr, margin, margin, margin, margin, cv2.BORDER_CONSTANT, value=[255, 255, 255])
        pH, pW = pad_img.shape[:2]

        target_max = max(pH, pW)
        target_min = min(pH, pW)
        if target_max / max(1, target_min) > 2:
            scale = min(1.0, (0.75 * 960.0 * 960.0 / (pH * pW)) ** 0.5)
        else:
            scale = 960.0 / target_max if target_max > 960 else (max(1.0, 480.0 / target_max) if target_max < 480 else 1.0)

        th = max(int(round(pH * scale / 32) * 32), 64)
        tw = max(int(round(pW * scale / 32) * 32), 64)

        inp = cv2.resize(pad_img, (tw, th)).astype(np.float32) / 255.0
        inp = (inp - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
        inp_tensor = inp.transpose((2, 0, 1))[np.newaxis, ...]

        inp_dtype = np.float16 if self.det_is_fp16 else np.float32
        inp_name = self.sess_det.get_inputs()[0].name

        with self._gpu_semaphore:
            pred_map = self.sess_det.run(None, {inp_name: inp_tensor.astype(inp_dtype)})[0][0, 0]

        pred_map = pred_map.astype(np.float32)
        bitmap = pred_map > self.thresh
        contours, _ = cv2.findContours((bitmap * 255).astype(np.uint8), cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        raw_boxes = []

        for cnt in contours:
            pts = cnt.squeeze(1)
            if pts.ndim != 2 or pts.shape[0] < 4:
                continue
            score = self._box_score_fast(pred_map, pts)
            if score < self.box_thresh:
                continue

            unclipped = self._unclip_pp(pts, self.unclip_ratio)
            if unclipped is None or len(unclipped) == 0:
                continue

            unclipped[:, 0] = unclipped[:, 0] * (pW / float(tw)) - margin
            unclipped[:, 1] = unclipped[:, 1] * (pH / float(th)) - margin
            xmin = max(0.0, float(np.min(unclipped[:, 0])))
            xmax = min(float(W), float(np.max(unclipped[:, 0])))
            ymin = max(0.0, float(np.min(unclipped[:, 1])))
            ymax = min(float(H), float(np.max(unclipped[:, 1])))
            if (xmax - xmin) < 6 or (ymax - ymin) < 6:
                continue

            (rcx, rcy), (rw, rh), angle = cv2.minAreaRect(unclipped.astype(np.float32))
            padded_rect = ((rcx, rcy), (rw + 4.0, rh + 4.0), angle)
            box_4pts = cv2.boxPoints(padded_rect)
            box_4pts[:, 0] = np.clip(box_4pts[:, 0], 0, W)
            box_4pts[:, 1] = np.clip(box_4pts[:, 1], 0, H)

            xmin = max(0.0, float(np.min(box_4pts[:, 0])))
            xmax = min(float(W), float(np.max(box_4pts[:, 0])))
            ymin = max(0.0, float(np.min(box_4pts[:, 1])))
            ymax = min(float(H), float(np.max(box_4pts[:, 1])))

            raw_boxes.append({
                'xmin': xmin, 'xmax': xmax, 'ymin': ymin, 'ymax': ymax,
                'cx': (xmin + xmax) / 2.0, 'cy': (ymin + ymax) / 2.0,
                'score': float(score),
                'pts': box_4pts.tolist(),
                'rect_angle': float(angle)
            })

        return raw_boxes

    def _recognize_lines_batched(self, crops_data: list[dict]) -> list[dict]:
        if not crops_data:
            return []

        rec_dtype = np.float16 if self.rec_is_fp16 else np.float32
        rec_inp_name = self.sess_rec.get_inputs()[0].name
        results = []

        batch_size = self.batch_size if self.can_batch_rec else 1

        for i in range(0, len(crops_data), batch_size):
            chunk = crops_data[i:i + batch_size]
            max_w = max(item['target_w'] for item in chunk)
            bucket_w = int(math.ceil(max_w / 64.0) * 64)

            batch_tensor = np.zeros((len(chunk), 3, 48, bucket_w), dtype=np.float32)
            for j, item in enumerate(chunk):
                cw = item['target_w']
                batch_tensor[j, :, :, :cw] = item['c_inp']

            with self._gpu_semaphore:
                logits = self.sess_rec.run(None, {rec_inp_name: batch_tensor.astype(rec_dtype)})[0]

            out = logits.astype(np.float32)
            is_logits = (np.max(out) > 1.0 or np.min(out) < 0.0)
            if is_logits:
                exp_out = np.exp(out - np.max(out, axis=-1, keepdims=True))
                probs = exp_out / np.sum(exp_out, axis=-1, keepdims=True)
            else:
                probs = out

            indices = np.argmax(probs, axis=-1)

            for j, item in enumerate(chunk):
                line_indices = indices[j]
                line_probs = probs[j]

                valid_t = min(line_indices.shape[0], max(1, int(round(item['target_w'] / 4.0))))
                chars, confs, prev = [], [], -1

                for t_step in range(valid_t):
                    idx = int(line_indices[t_step])
                    if idx != prev and idx != 0 and idx < len(self.dict_chars):
                        chars.append(self.dict_chars[idx])
                        confs.append(float(line_probs[t_step, idx]))
                    prev = idx

                text = "".join(chars).strip()
                text = re.sub(r'\s+', '', text)
                if not text or text in self.REJECT_PATTERNS:
                    continue

                b = item['orig_box']
                w_box = b['xmax'] - b['xmin']
                h_box = b['ymax'] - b['ymin']
                area_box = w_box * h_box

                if len(text) == 1 and (area_box < 400 or max(w_box, h_box) < 32 or text in self.DROPLET_CHARS or text.isascii()):
                    continue

                avg_conf = float(np.mean(confs)) if confs else 0.0
                if avg_conf < self.rec_confidence_threshold:
                    continue

                new_pts_arr = item['new_pts_arr']
                xmin = float(np.min(new_pts_arr[:, 0]))
                xmax = float(np.max(new_pts_arr[:, 0]))
                ymin = float(np.min(new_pts_arr[:, 1]))
                ymax = float(np.max(new_pts_arr[:, 1]))
                line_vert = (ymax - ymin) >= (xmax - xmin)

                b_updated = dict(b)
                b_updated['pts'] = new_pts_arr.tolist()
                b_updated['xmin'] = xmin
                b_updated['xmax'] = xmax
                b_updated['ymin'] = ymin
                b_updated['ymax'] = ymax
                b_updated['cx'] = (xmin + xmax) / 2.0
                b_updated['cy'] = (ymin + ymax) / 2.0

                results.append({
                    "box": b_updated,
                    "text": text,
                    "confidence": avg_conf,
                    "is_vertical": line_vert,
                    "is_furigana": False
                })

        return results

    def _ocr_single_chunk(self, img: Image.Image) -> list[Bubble]:
        if img.mode != "RGB":
            img = img.convert("RGB")

        chunk_w, chunk_h = img.size
        img_np = np.array(img)
        img_bgr = cv2.cvtColor(img_np, cv2.COLOR_RGB2BGR)

        raw_boxes = self._detect_boxes_dbnet(img_bgr)
        if not raw_boxes:
            return []

        vert_weight, horiz_weight = 0.0, 0.0
        for b in raw_boxes:
            bw = b['xmax'] - b['xmin']
            bh = b['ymax'] - b['ymin']
            area = bw * bh
            if bh > bw * 1.1:
                vert_weight += area
            elif bw > bh * 1.1:
                horiz_weight += area
            else:
                vert_weight += area * 0.5

        is_page_vertical = vert_weight >= horiz_weight
        sorted_boxes = self._sort_reading_order_v2(raw_boxes, is_vertical=is_page_vertical)

        crops_data = []
        for b in sorted_boxes:
            crop, new_pts = self._get_rotate_crop_image(img_bgr, b['pts'])
            if crop.size == 0 or crop.shape[0] < 2 or crop.shape[1] < 2:
                continue

            crop = self._clean_manga_vertical_crop(crop)
            if crop.shape[0] > crop.shape[1]:
                crop = cv2.rotate(crop, cv2.ROTATE_90_COUNTERCLOCKWISE)

            ch, cw = crop.shape[:2]
            natural_w = int(round(48.0 * cw / max(1, ch)))
            target_w = max(16, min(2400 if natural_w > 2000 else 640, natural_w))

            c_inp = cv2.resize(crop, (target_w, 48)).astype(np.float32)
            c_inp = cv2.cvtColor(c_inp, cv2.COLOR_BGR2RGB) / 255.0
            c_inp = ((c_inp - 0.5) / 0.5).transpose((2, 0, 1))

            crops_data.append({
                'c_inp': c_inp,
                'target_w': target_w,
                'new_pts_arr': np.array(new_pts, dtype=np.float32),
                'orig_box': b
            })

        temp_lines = self._recognize_lines_batched(crops_data)
        if not temp_lines:
            return []

        if len(temp_lines) > 1:
            for i, l1 in enumerate(temp_lines):
                for j, l2 in enumerate(temp_lines):
                    if i == j:
                        continue
                    if self._is_furigana_pair(l1, l2, is_vertical=is_page_vertical):
                        l1['is_furigana'] = True
                        break

        results_bubbles: list[Bubble] = []
        for line in temp_lines:
            if self.filter_furigana and line['is_furigana']:
                continue

            b = line['box']
            bx = max(0.0, b['xmin'])
            by = max(0.0, b['ymin'])
            bw = min(float(chunk_w), b['xmax']) - bx
            bh = min(float(chunk_h), b['ymax']) - by

            orientation_angle = 90.0 if line['is_vertical'] else 0.0

            results_bubbles.append(Bubble(
                text=line['text'],
                tightBoundingBox=BoundingBox(
                    x=float(bx / chunk_w),
                    y=float(by / chunk_h),
                    width=float(bw / chunk_w),
                    height=float(bh / chunk_h)
                ),
                orientation=float(round(orientation_angle, 1)),
                font_size=0.04,
                confidence=float(round(line['confidence'], 4))
            ))

        return results_bubbles

    async def ocr(self, img: Image.Image) -> list[Bubble]:
        with self._lock:
            self.last_access_time = time.time()
            if not self.models_loaded:
                self._load_models()

        return await self._process_webtoon_chunked(img, self._ocr_single_chunk, "PPOCRv6Manga")


def initialize_engine(engine_name: str) -> Engine:
    engine_name = engine_name.strip().lower()

    if engine_name in ("lens", "googlelens"):
        return GoogleLens()
    elif engine_name == "mangaocrdirectml":
        return MangaOCRDirectML()
    elif engine_name == "ppocrv6manga":
        return PPOCRv6Manga()
    elif engine_name == "mangaocr":
        return MangaOCR()
    else:
        raise ValueError(f"Invalid engine: {engine_name}")
