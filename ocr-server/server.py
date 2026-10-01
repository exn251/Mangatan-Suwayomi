# -*- coding: utf-8 -*-

import argparse
import base64
import io
import json
import os
import threading
import traceback
import time
import requests
import re
from urllib.parse import quote
from collections import defaultdict

import aiohttp
from engines import Engine, initialize_engine
from flask import Flask, jsonify, request, send_file
from PIL import Image
from waitress import serve

# region Config
IP_ADDRESS = "0.0.0.0"
PORT = 3000
CACHE_FILE_PATH = os.path.join(os.getcwd(), "ocr-cache.json")
UPLOAD_FOLDER = "uploads"
IMAGE_CACHE_FOLDER = "image_cache"

AUTO_MERGE_CONFIG = {
    "enabled": True,
    "dist_k": 1.25,                  # Distance factor between lines
    "font_ratio": 1.6,               # Relaxed from 1.3 to 1.6 to prevent short kana rejection
    "perp_tol": 0.5,
    "overlap_min": 0.1,
    "min_line_ratio": 0.4,           # Relaxed from 0.5 to 0.4 for short lines
    "font_ratio_for_mixed": 1.3,     # Relaxed from 1.1 to 1.3
    "mixed_min_overlap_ratio": 0.3,  # Relaxed from 0.5 to 0.3
    "add_space_on_merge": False,
}
# endregion

# region Setup
app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

Image.MAX_IMAGE_PIXELS = None

is_debug_mode = False
ocr_cache = {}
ocr_requests_processed = 0
cache_lock = threading.Lock()
active_job_count = 0
active_job_lock = threading.Lock()
ocr_engine: Engine
# endregion


# region Auto-Merge & Deduplication Logic

class UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, i):
        if self.parent[i] == i:
            return i
        self.parent[i] = self.find(self.parent[i])
        return self.parent[i]

    def union(self, i, j):
        root_i = self.find(i)
        root_j = self.find(j)
        if root_i != root_j:
            if self.rank[root_i] > self.rank[root_j]:
                self.parent[root_j] = root_i
            elif self.rank[root_i] < self.rank[root_j]:
                self.parent[root_i] = root_j
            else:
                self.parent[root_j] = root_i
                self.rank[root_i] += 1
            return True
        return False


def _median(data):
    if not data:
        return 0.0
    sorted_data = sorted(data)
    mid = len(sorted_data) // 2
    if len(sorted_data) % 2 == 0:
        return float((sorted_data[mid - 1] + sorted_data[mid]) / 2.0)
    return float(sorted_data[mid])


def _deduplicate_raw_lines(lines, natural_width, natural_height):
    """
    Suppresses redundant detections, chunk boundary overlaps, and partial sub-boxes
    across all OCR engines before bubble grouping.
    """
    if not lines or len(lines) < 2:
        return lines

    items = []
    for line in lines:
        bbox = line["tightBoundingBox"]
        px_x1 = float(bbox["x"] * natural_width)
        px_y1 = float(bbox["y"] * natural_height)
        px_x2 = float((bbox["x"] + bbox["width"]) * natural_width)
        px_y2 = float((bbox["y"] + bbox["height"]) * natural_height)
        area = max(1.0, (px_x2 - px_x1) * (px_y2 - px_y1))
        
        raw_text = str(line.get("text", "")).strip()
        clean_text = re.sub(r'[\s\u200b\u3000\-_~\u2665\u2661!\uff01?\uff1f\u30fc\u2014\u2015]+', '', raw_text)

        items.append({
            "line": line,
            "box": [px_x1, px_y1, px_x2, px_y2],
            "area": area,
            "text": raw_text,
            "clean_text": clean_text
        })

    items.sort(key=lambda item: item["area"], reverse=True)
    kept_items = []

    while items:
        current = items.pop(0)
        curr_box = list(current["box"])
        curr_text = current["text"]
        curr_clean = current["clean_text"]
        remaining = []

        for other in items:
            other_box = other["box"]
            other_text = other["text"]
            other_clean = other["clean_text"]

            inter_x1 = max(curr_box[0], other_box[0])
            inter_y1 = max(curr_box[1], other_box[1])
            inter_x2 = min(curr_box[2], other_box[2])
            inter_y2 = min(curr_box[3], other_box[3])

            inter_w = max(0.0, inter_x2 - inter_x1)
            inter_h = max(0.0, inter_y2 - inter_y1)
            inter_area = inter_w * inter_h

            area_curr = (curr_box[2] - curr_box[0]) * (curr_box[3] - curr_box[1])
            area_other = (other_box[2] - other_box[0]) * (other_box[3] - other_box[1])
            min_area = max(1.0, min(area_curr, area_other))
            union_area = (area_curr + area_other - inter_area)

            ios = inter_area / min_area
            iou = (inter_area / union_area) if union_area > 0 else 0.0

            is_same_text = (curr_clean == other_clean) and len(curr_clean) > 0
            is_sub_text = (other_clean in curr_clean or curr_clean in other_clean) and min(len(curr_clean), len(other_clean)) > 0

            if (iou >= 0.70) or (ios >= 0.85) or (is_same_text and ios >= 0.30) or (is_sub_text and ios >= 0.45):
                curr_box[0] = min(curr_box[0], other_box[0])
                curr_box[1] = min(curr_box[1], other_box[1])
                curr_box[2] = max(curr_box[2], other_box[2])
                curr_box[3] = max(curr_box[3], other_box[3])

                if len(other_text) > len(curr_text):
                    current["line"]["text"] = other_text
                    curr_text = other_text
                    curr_clean = other_clean
            else:
                remaining.append(other)

        w_px = curr_box[2] - curr_box[0]
        h_px = curr_box[3] - curr_box[1]
        current["line"]["tightBoundingBox"] = {
            "x": float(curr_box[0] / natural_width),
            "y": float(curr_box[1] / natural_height),
            "width": float(w_px / natural_width),
            "height": float(h_px / natural_height),
        }
        kept_items.append(current["line"])
        items = remaining

    return kept_items


def _is_probable_furigana(candidate_line, bubble_lines, is_vertical=True):
    """
    Returns True if candidate_line is ruby furigana rather than a main dialogue column.
    """
    text = str(candidate_line.get("text", "")).strip()
    if not text:
        return True

    # Rule 1: Furigana NEVER contains dialogue punctuation or quotes
    dialogue_marks = {'？', '！', '!', '?', '…', '‥', '「', '」', '『', '』', '—', '─', '―', '♡', '♥', '♪', '~', '～'}
    if any(mark in text for mark in dialogue_marks):
        return False  # Has dialogue punctuation -> Definitely genuine dialogue

    # Rule 2: Furigana almost never contains Kanji
    has_kanji = any(('\u4e00' <= ch <= '\u9fff') or ('\u3400' <= ch <= '\u4dbf') for ch in text)
    if has_kanji:
        return False  # Contains Kanji -> Real dialogue column

    # Rule 3: Thickness check (character width for vertical, character height for horizontal)
    c_box = candidate_line["tightBoundingBox"]
    c_thick = c_box["width"] if is_vertical else c_box["height"]

    bubble_thicknesses = [
        (l["tightBoundingBox"]["width"] if is_vertical else l["tightBoundingBox"]["height"])
        for l in bubble_lines
    ]
    median_thick = _median(bubble_thicknesses)

    # Real furigana is miniature (< 60% of the bubble's dialogue thickness).
    # Dialogue columns (like 「どうか」) are ~70%+ of median thickness.
    if median_thick > 0 and (c_thick / median_thick) < 0.60:
        # Check if pinned alongside a line in this bubble that contains Kanji
        for bl in bubble_lines:
            bl_text = str(bl.get("text", "")).strip()
            bl_has_kanji = any(('\u4e00' <= ch <= '\u9fff') or ('\u3400' <= ch <= '\u4dbf') for ch in bl_text)
            if bl_has_kanji:
                bl_box = bl["tightBoundingBox"]
                if is_vertical and c_box["x"] >= bl_box["x"]:
                    return True  # Vertical ruby to the right of Kanji
                elif not is_vertical and c_box["y"] <= bl_box["y"]:
                    return True  # Horizontal ruby above Kanji
        return True

    return False


def _group_ocr_data(lines, natural_width, natural_height, config):
    if not lines or len(lines) < 2 or not natural_width or not natural_height:
        return [[line] for line in lines]

    CHUNK_MAX_HEIGHT = 3000
    processed_lines = []
    for index, line in enumerate(lines):
        bbox = line["tightBoundingBox"]
        pixel_top = float(bbox["y"] * natural_height)
        pixel_bottom = float((bbox["y"] + bbox["height"]) * natural_height)
        norm_scale = 1000.0 / natural_width

        normalized_bbox = {
            "x": float((bbox["x"] * natural_width) * norm_scale),
            "y": float((bbox["y"] * natural_height) * norm_scale),
            "width": float((bbox["width"] * natural_width) * norm_scale),
            "height": float((bbox["height"] * natural_height) * norm_scale),
        }
        normalized_bbox["right"] = normalized_bbox["x"] + normalized_bbox["width"]
        normalized_bbox["bottom"] = normalized_bbox["y"] + normalized_bbox["height"]

        is_vertical = line.get("orientation") == 90.0 or (normalized_bbox["width"] <= normalized_bbox["height"])
        font_size = normalized_bbox["width"] if is_vertical else normalized_bbox["height"]

        processed_lines.append({
            "original_index": index,
            "is_vertical": is_vertical,
            "font_size": font_size,
            "bbox": normalized_bbox,
            "pixel_top": pixel_top,
            "pixel_bottom": pixel_bottom,
        })

    processed_lines.sort(key=lambda p: p["pixel_top"])

    all_groups = []
    current_line_index = 0
    chunks_processed = 0

    while current_line_index < len(processed_lines):
        chunks_processed += 1
        chunk_start_index = current_line_index
        chunk_end_index = len(processed_lines) - 1

        if natural_height > CHUNK_MAX_HEIGHT:
            chunk_top_y = processed_lines[chunk_start_index]["pixel_top"]
            for i in range(chunk_start_index + 1, len(processed_lines)):
                if (processed_lines[i]["pixel_bottom"] - chunk_top_y) <= CHUNK_MAX_HEIGHT:
                    chunk_end_index = i
                else:
                    break

        chunk_lines = processed_lines[chunk_start_index : chunk_end_index + 1]
        uf = UnionFind(len(chunk_lines))

        horizontal_lines = [l for l in chunk_lines if not l["is_vertical"]]
        vertical_lines = [l for l in chunk_lines if l["is_vertical"]]

        initial_median_h = _median([l["bbox"]["height"] for l in horizontal_lines])
        initial_median_w = _median([l["bbox"]["width"] for l in vertical_lines])

        primary_h = [l for l in horizontal_lines if l["bbox"]["height"] >= initial_median_h * config["min_line_ratio"]]
        primary_v = [l for l in vertical_lines if l["bbox"]["width"] >= initial_median_w * config["min_line_ratio"]]
        
        robust_median_h = _median([l["bbox"]["height"] for l in primary_h]) or initial_median_h or 20.0
        robust_median_w = _median([l["bbox"]["width"] for l in primary_v]) or initial_median_w or 20.0

        for i in range(len(chunk_lines)):
            for j in range(i + 1, len(chunk_lines)):
                line_a, line_b = chunk_lines[i], chunk_lines[j]
                if line_a["is_vertical"] != line_b["is_vertical"]:
                    continue

                is_a_primary = line_a["font_size"] >= (robust_median_w if line_a["is_vertical"] else robust_median_h) * config["min_line_ratio"]
                is_b_primary = line_b["font_size"] >= (robust_median_w if line_b["is_vertical"] else robust_median_h) * config["min_line_ratio"]
                
                font_ratio_threshold = config["font_ratio"]
                if is_a_primary != is_b_primary:
                    font_ratio_threshold = config["font_ratio_for_mixed"]
                
                if line_a["font_size"] == 0 or line_b["font_size"] == 0:
                    continue
                    
                font_ratio = max(line_a["font_size"] / line_b["font_size"], line_b["font_size"] / line_a["font_size"])
                if font_ratio > font_ratio_threshold:
                    continue

                local_font_size = max(line_a["font_size"], line_b["font_size"])
                fallback_median = robust_median_w if line_a["is_vertical"] else robust_median_h
                dist_threshold = max(local_font_size, fallback_median) * config["dist_k"]
                
                if line_a["is_vertical"]:
                    reading_gap = max(0.0, max(line_a["bbox"]["x"], line_b["bbox"]["x"]) - min(line_a["bbox"]["right"], line_b["bbox"]["right"]))
                    perp_overlap = max(0.0, min(line_a["bbox"]["bottom"], line_b["bbox"]["bottom"]) - max(line_a["bbox"]["y"], line_b["bbox"]["y"]))
                else:
                    reading_gap = max(0.0, max(line_a["bbox"]["y"], line_b["bbox"]["y"]) - min(line_a["bbox"]["bottom"], line_b["bbox"]["bottom"]))
                    perp_overlap = max(0.0, min(line_a["bbox"]["right"], line_b["bbox"]["right"]) - max(line_a["bbox"]["x"], line_b["bbox"]["x"]))

                smaller_perp_size = min(line_a["bbox"]["height"] if line_a["is_vertical"] else line_a["bbox"]["width"],
                                        line_b["bbox"]["height"] if line_b["is_vertical"] else line_b["bbox"]["width"])

                if reading_gap > dist_threshold:
                    continue
                if smaller_perp_size > 0 and perp_overlap / smaller_perp_size < config["overlap_min"]:
                    continue
                if is_a_primary != is_b_primary and smaller_perp_size > 0 and (perp_overlap / smaller_perp_size < config["mixed_min_overlap_ratio"]):
                    continue
                
                uf.union(i, j)

        temp_groups = defaultdict(list)
        for i in range(len(chunk_lines)):
            root = uf.find(i)
            temp_groups[root].append(chunk_lines[i])

        chunk_final_groups = [
            [lines[p_line["original_index"]] for p_line in group]
            for group in temp_groups.values()
        ]
        all_groups.extend(chunk_final_groups)
        current_line_index = chunk_end_index + 1

    if is_debug_mode:
        print(f"[AutoMerge] Grouping finished. Initial: {len(lines)}, Final groups: {len(all_groups)} (in {chunks_processed} chunk(s))")
    return all_groups


def _sanitize_bubble(item):
    """Ensures all fields are standard JSON-serializable Python native types."""
    bbox = item["tightBoundingBox"]
    cleaned = {
        "text": str(item.get("text", "")),
        "tightBoundingBox": {
            "x": float(bbox["x"]),
            "y": float(bbox["y"]),
            "width": float(bbox["width"]),
            "height": float(bbox["height"]),
        },
        "orientation": float(item.get("orientation", 90.0 if bbox["height"] > bbox["width"] else 0.0)),
        "font_size": float(item.get("font_size", 0.04)),
        "confidence": float(item.get("confidence", 0.95))
    }
    if "isMerged" in item:
        cleaned["isMerged"] = bool(item["isMerged"])
    if "forcedOrientation" in item:
        cleaned["forcedOrientation"] = str(item["forcedOrientation"])
    return cleaned


def auto_merge_ocr_data(lines, natural_width, natural_height, config):
    if not lines:
        return []

    # Step 1: Pre-suppress spatial duplicates and boundary-split sub-boxes
    lines = _deduplicate_raw_lines(lines, natural_width, natural_height)

    # Step 2: Cluster lines into dialogue bubble groups
    groups = _group_ocr_data(lines, natural_width, natural_height, config)

    # Step 2.5: Absorb enclosed orphan dialogue lines (with Furigana guard)
    multi_groups = [g for g in groups if len(g) >= 2]
    single_groups = [g for g in groups if len(g) == 1]

    if multi_groups and single_groups:
        absorbed_singles = set()
        for s_idx, sg in enumerate(single_groups):
            s_line = sg[0]
            s_box = s_line["tightBoundingBox"]
            s_cx = s_box["x"] + s_box["width"] / 2.0
            s_cy = s_box["y"] + s_box["height"] / 2.0
            s_vert = s_line.get("orientation") == 90.0 or (s_box["height"] >= s_box["width"])

            for mg in multi_groups:
                m_bboxes = [l["tightBoundingBox"] for l in mg]
                min_x = min(b["x"] for b in m_bboxes)
                max_x = max(b["x"] + b["width"] for b in m_bboxes)
                min_y = min(b["y"] for b in m_bboxes)
                max_y = max(b["y"] + b["height"] for b in m_bboxes)

                mg_vert_count = sum(1 for l in mg if l.get("orientation") == 90.0 or (l["tightBoundingBox"]["height"] >= l["tightBoundingBox"]["width"]))
                mg_is_vert = mg_vert_count >= (len(mg) - mg_vert_count)

                # Check spatial containment
                if s_vert == mg_is_vert:
                    pad_x = (max_x - min_x) * 0.05
                    pad_y = (max_y - min_y) * 0.05
                    if (min_x - pad_x <= s_cx <= max_x + pad_x) and (min_y - pad_y <= s_cy <= max_y + pad_y):
                        
                        # --- FURIGANA SAFEGUARD ---
                        if _is_probable_furigana(s_line, mg, is_vertical=mg_is_vert):
                            if is_debug_mode:
                                print(f"[AutoMerge] Skipped absorbing '{s_line.get('text')}' (identified as Furigana).")
                            continue  # Do NOT merge furigana into the dialogue text!

                        mg.append(s_line)
                        absorbed_singles.add(s_idx)
                        if is_debug_mode:
                            print(f"[AutoMerge] Absorbed dialogue column '{s_line.get('text')}' into bubble.")
                        break

        groups = multi_groups + [sg for idx, sg in enumerate(single_groups) if idx not in absorbed_singles]

    final_merged_data = []

    for group in groups:
        if len(group) == 1:
            final_merged_data.append(_sanitize_bubble(group[0]))
            continue

        # Step 3: Intra-group Substring & Containment Deduplication
        sorted_by_len = sorted(group, key=lambda l: len(str(l.get("text", "")).strip()), reverse=True)
        dropped_indices = set()

        for i in range(len(sorted_by_len)):
            if i in dropped_indices:
                continue
            line_a = sorted_by_len[i]
            text_a = re.sub(r'[\s\u200b\u3000]+', '', str(line_a.get("text", "")))
            box_a = line_a["tightBoundingBox"]

            px_a_x1, px_a_x2 = box_a["x"] * natural_width, (box_a["x"] + box_a["width"]) * natural_width
            px_a_y1, px_a_y2 = box_a["y"] * natural_height, (box_a["y"] + box_a["height"]) * natural_height

            for j in range(i + 1, len(sorted_by_len)):
                if j in dropped_indices:
                    continue
                line_b = sorted_by_len[j]
                text_b = re.sub(r'[\s\u200b\u3000]+', '', str(line_b.get("text", "")))
                box_b = line_b["tightBoundingBox"]

                px_b_x1, px_b_x2 = box_b["x"] * natural_width, (box_b["x"] + box_b["width"]) * natural_width
                px_b_y1, px_b_y2 = box_b["y"] * natural_height, (box_b["y"] + box_b["height"]) * natural_height

                inter_w = max(0.0, min(px_a_x2, px_b_x2) - max(px_a_x1, px_b_x1))
                inter_h = max(0.0, min(px_a_y2, px_b_y2) - max(px_a_y1, px_b_y1))
                inter_area = inter_w * inter_h
                area_b = max(1.0, (px_b_x2 - px_b_x1) * (px_b_y2 - px_b_y1))
                ios_b = inter_area / area_b

                # Substring line duplicate inside overlapping text bubble
                if text_b and text_a and (text_b in text_a):
                    col_overlap = max(0.0, min(px_a_x2, px_b_x2) - max(px_a_x1, px_b_x1)) / max(1.0, min(px_a_x2 - px_a_x1, px_b_x2 - px_b_x1))
                    row_overlap = max(0.0, min(px_a_y2, px_b_y2) - max(px_a_y1, px_b_y1)) / max(1.0, min(px_a_y2 - px_a_y1, px_b_y2 - px_b_y1))
                    if col_overlap >= 0.35 or row_overlap >= 0.35 or ios_b >= 0.30:
                        dropped_indices.add(j)
                        continue

                # Heavy spatial containment
                if ios_b >= 0.75:
                    dropped_indices.add(j)
                    continue

        valid_group = [sorted_by_len[k] for k in range(len(sorted_by_len)) if k not in dropped_indices]
        if not valid_group:
            valid_group = group

        if len(valid_group) == 1:
            final_merged_data.append(_sanitize_bubble(valid_group[0]))
            continue

        # Step 4: Determine Group Reading Orientation in Pixel Space
        vertical_lines_count = sum(
            1 for line in valid_group
            if line.get("orientation") == 90.0 or (line["tightBoundingBox"]["height"] * natural_height >= line["tightBoundingBox"]["width"] * natural_width)
        )
        is_vertical_group = vertical_lines_count >= (len(valid_group) - vertical_lines_count)

        # Step 5: Sub-line clustering & 2D Reading Order Sorting
        uf_sort = UnionFind(len(valid_group))
        for i in range(len(valid_group)):
            for j in range(i + 1, len(valid_group)):
                box_a = valid_group[i]["tightBoundingBox"]
                box_b = valid_group[j]["tightBoundingBox"]
                
                px_a_x, px_a_w = box_a["x"] * natural_width, box_a["width"] * natural_width
                px_a_y, px_a_h = box_a["y"] * natural_height, box_a["height"] * natural_height
                px_b_x, px_b_w = box_b["x"] * natural_width, box_b["width"] * natural_width
                px_b_y, px_b_h = box_b["y"] * natural_height, box_b["height"] * natural_height

                if is_vertical_group:
                    overlap = max(0.0, min(px_a_x + px_a_w, px_b_x + px_b_w) - max(px_a_x, px_b_x))
                    min_size = min(px_a_w, px_b_w)
                else:
                    overlap = max(0.0, min(px_a_y + px_a_h, px_b_y + px_b_h) - max(px_a_y, px_b_y))
                    min_size = min(px_a_h, px_b_h)
                
                if min_size > 0 and (overlap / min_size) > 0.40:
                    uf_sort.union(i, j)
        
        sub_lines_dict = defaultdict(list)
        for i in range(len(valid_group)):
            root = uf_sort.find(i)
            sub_lines_dict[root].append(valid_group[i])
            
        sub_lines = list(sub_lines_dict.values())
        
        # 1. Sort elements WITHIN each sub-line (along the primary reading axis)
        for sub in sub_lines:
            if is_vertical_group:
                # Vertical text: Top-to-Bottom
                sub.sort(key=lambda line: line["tightBoundingBox"]["y"])
            else:
                # Horizontal text: Left-to-Right
                sub.sort(key=lambda line: line["tightBoundingBox"]["x"])
                
        # 2. Sort the sub-lines themselves (along the secondary reading axis)
        if is_vertical_group:
            # Vertical text: Right-to-Left columns
            sub_lines.sort(key=lambda sub: -sum(line["tightBoundingBox"]["x"] + line["tightBoundingBox"]["width"] / 2.0 for line in sub) / len(sub))
        else:
            # Horizontal text: Top-to-Bottom rows
            sub_lines.sort(key=lambda sub: sum(line["tightBoundingBox"]["y"] + line["tightBoundingBox"]["height"] / 2.0 for line in sub) / len(sub))
            
        sorted_group = []
        for sub in sub_lines:
            sorted_group.extend(sub)
        
        join_char = " " if config["add_space_on_merge"] else "\u200b"
        combined_text = join_char.join([str(line["text"]).strip() for line in sorted_group if str(line.get("text", "")).strip()])

        group_bboxes = [line["tightBoundingBox"] for line in sorted_group]
        min_x = min(float(b["x"]) for b in group_bboxes)
        min_y = min(float(b["y"]) for b in group_bboxes)
        max_r = max(float(b["x"] + b["width"]) for b in group_bboxes)
        max_b = max(float(b["y"] + b["height"]) for b in group_bboxes)

        final_merged_data.append({
            "text": str(combined_text),
            "isMerged": True,
            "forcedOrientation": "vertical" if is_vertical_group else "horizontal",
            "orientation": 90.0 if is_vertical_group else 0.0,
            "tightBoundingBox": {
                "x": float(min_x),
                "y": float(min_y),
                "width": float(max_r - min_x),
                "height": float(max_b - min_y),
            },
        })

    return final_merged_data

# endregion


# region Utility

def load_cache():
    global ocr_cache
    if os.path.exists(CACHE_FILE_PATH):
        try:
            with open(CACHE_FILE_PATH, "r", encoding="utf-8") as f:
                ocr_cache = json.load(f)
            print(f"[Cache] Loaded {len(ocr_cache)} items from {CACHE_FILE_PATH}")
        except json.JSONDecodeError:
            print("[Cache] Warning: Could not decode JSON. Starting fresh.")
    else:
        print("[Cache] No cache file found. Starting fresh.")


def save_cache():
    if is_debug_mode:
        print("[DEBUG] Saving OCR cache...")
    with open(CACHE_FILE_PATH, "w", encoding="utf-8") as f:
        json.dump(ocr_cache, f, indent=2, ensure_ascii=False)
    if is_debug_mode:
        print("[DEBUG] OCR cache saved successfully.")

# endregion


# region Background Job

def run_chapter_processing_job(base_url, auth_user, auth_pass, context):
    global active_job_count
    with active_job_lock:
        active_job_count += 1

    print(f"[JobRunner] [{context}] Started job for ...{base_url[-40:]}. Active jobs: {active_job_count}")

    page_index, consecutive_errors = 0, 0
    CONSECUTIVE_ERROR_THRESHOLD = 3
    SERVER_URL_BASE = "http://127.0.0.1:3000"

    while consecutive_errors < CONSECUTIVE_ERROR_THRESHOLD:
        image_url = f"{base_url}{page_index}"
        with cache_lock:
            if image_url in ocr_cache:
                print(f"[JobRunner] [{context}] Skip (in cache): {image_url}")
                page_index += 1
                consecutive_errors = 0
                continue

        encoded_url = quote(image_url, safe="")
        encoded_context = quote(context, safe="")
        target_url = f"{SERVER_URL_BASE}/ocr?url={encoded_url}&context={encoded_context}"
        if auth_user:
            target_url += f"&user={auth_user}&pass={auth_pass}"

        try:
            print(f"[JobRunner] [{context}] Requesting: {image_url}")
            response = requests.get(target_url, timeout=45)
            if response.status_code == 200:
                consecutive_errors = 0
            else:
                consecutive_errors += 1
                print(f"[JobRunner] [{context}] Got non-200 status ({response.status_code}) for {image_url}. Errors: {consecutive_errors}")
                if response.status_code == 404:
                    print("[JobRunner] (Page not found, likely end of chapter)")
        except requests.exceptions.RequestException as e:
            consecutive_errors += 1
            print(f"[JobRunner] [{context}] Request failed for {image_url}. Errors: {consecutive_errors}. Details: {e}")

        page_index += 1
        time.sleep(0.1)

    print(f"[JobRunner] [{context}] Finished job for ...{base_url[-40:]}. Reached {consecutive_errors} errors.")
    with active_job_lock:
        active_job_count -= 1

# endregion


# region Endpoints

@app.route("/")
def status_endpoint():
    with cache_lock:
        num_requests, num_cache_items = ocr_requests_processed, len(ocr_cache)
    with active_job_lock:
        active_jobs = active_job_count
    return jsonify({
        "status": "running",
        "message": "Python OCR server is active.",
        "requests_processed": num_requests,
        "items_in_cache": num_cache_items,
        "active_preprocess_jobs": active_jobs,
    })


@app.route("/ocr")
async def ocr_endpoint():
    global ocr_requests_processed
    image_url = request.args.get("url")
    context = request.args.get("context", "No Context")

    if not image_url:
        return jsonify({"error": "Image URL is required"}), 400

    with cache_lock:
        if image_url in ocr_cache:
            cached_entry = ocr_cache[image_url]
            return jsonify(cached_entry.get("data", cached_entry))

    print(f"[OCR] [{context}] Processing: {image_url}")
    try:
        auth_headers = {}
        if auth_user := request.args.get("user"):
            auth_pass = request.args.get("pass", "")
            auth_base64 = base64.b64encode(f"{auth_user}:{auth_pass}".encode("utf-8")).decode("utf-8")
            auth_headers["Authorization"] = f"Basic {auth_base64}"

        async with aiohttp.ClientSession() as session:
            async with session.get(image_url, headers=auth_headers) as response:
                response.raise_for_status()
                image_bytes = await response.read()

        pil_image = Image.open(io.BytesIO(image_bytes))
        rgb_image = pil_image.convert("RGB")
        
        full_width, full_height = rgb_image.size

        raw_results = await ocr_engine.ocr(rgb_image)
        all_final_results = raw_results
        
        if AUTO_MERGE_CONFIG["enabled"] and raw_results:
            all_final_results = auto_merge_ocr_data(raw_results, full_width, full_height, AUTO_MERGE_CONFIG)
        elif raw_results:
            all_final_results = [_sanitize_bubble(b) for b in _deduplicate_raw_lines(raw_results, full_width, full_height)]
        
        with cache_lock:
            ocr_cache[image_url] = {"context": context, "data": all_final_results}
            ocr_requests_processed += 1
            save_cache()

        print(f"[OCR] [{context}] Successful for: {image_url}")
        return jsonify(all_final_results)

    except aiohttp.ClientResponseError as e:
        print(f"[OCR] [{context}] ERROR fetching {image_url}: Status {e.status}")
        return jsonify({"error": f"Failed to fetch image from URL, status: {e.status}"}), e.status
    except Exception as e:
        print(f"[OCR] [{context}] ERROR on {image_url}: {e}")
        if is_debug_mode:
            traceback.print_exc()
        return jsonify({"error": f"An unexpected error occurred: {e}"}), 500


@app.route("/preprocess-chapter", methods=["POST"])
def preprocess_chapter_endpoint():
    data = request.json
    if data is None:
        return jsonify({"error": "Invalid JSON payload"}), 400

    base_url = data.get("baseUrl")
    context = data.get("context", "No Context")

    if not base_url:
        return jsonify({"error": "baseUrl is required"}), 400

    job_thread = threading.Thread(
        target=run_chapter_processing_job,
        args=(base_url, data.get("user"), data.get("pass"), context),
        daemon=True,
    )
    job_thread.start()

    print(f"[Queue] [{context}] Job started in new thread for ...{base_url[-40:]}")
    return jsonify({
        "status": "accepted",
        "message": "Chapter pre-processing job has been started.",
    }), 202


@app.route("/purge-cache", methods=["POST"])
def purge_cache_endpoint():
    with cache_lock:
        count = len(ocr_cache)
        ocr_cache.clear()
        save_cache()
        print(f"[Cache] Purged. Removed {count} items.")
    return jsonify({"status": "success", "message": f"Cache purged. Removed {count} items."})


@app.route("/export-cache")
def export_cache_endpoint():
    if not os.path.exists(CACHE_FILE_PATH):
        return jsonify({"error": "No cache file to export."}), 404
    return send_file(CACHE_FILE_PATH, as_attachment=True, download_name="ocr-cache.json")


@app.route("/import-cache", methods=["POST"])
def import_cache_endpoint():
    if "cacheFile" not in request.files:
        return jsonify({"error": "No file part."}), 400
    file = request.files["cacheFile"]
    if not (file.filename and file.filename.endswith(".json")):
        return jsonify({"error": "Invalid file."}), 400
    try:
        imported_data = json.loads(file.read().decode("utf-8"))
        if not isinstance(imported_data, dict):
            return jsonify({"error": "Invalid cache format."}), 400
        with cache_lock:
            new_items = 0
            for key, value in imported_data.items():
                if key not in ocr_cache:
                    if isinstance(value, list):
                        ocr_cache[key] = {"context": "Imported Data", "data": value}
                    elif isinstance(value, dict) and "data" in value:
                        ocr_cache[key] = value
                    else:
                        continue 
                    new_items += 1
            if new_items > 0:
                save_cache()
            total_items = len(ocr_cache)
        return jsonify({
            "message": f"Import successful. Added {new_items} new items.",
            "total_items_in_cache": total_items,
        })
    except Exception as e:
        return jsonify({"error": f"Import failed: {e}"}), 500


@app.route("/update-cache", methods=["POST"])
def update_cache_endpoint():
    try:
        request_data = request.json
        if not request_data:
            return jsonify({"error": "Invalid JSON payload"}), 400
        
        image_url = request_data.get("url")
        new_data = request_data.get("data")
        context = request_data.get("context", "Updated from client")
        
        if not image_url:
            return jsonify({"error": "URL is required"}), 400
        
        if not isinstance(new_data, list):
            return jsonify({"error": "Data must be an array of OCR results"}), 400
        
        with cache_lock:
            ocr_cache[image_url] = {
                "context": context,
                "data": new_data
            }
            save_cache()
            
            print(f"[Cache] Updated entry for: {image_url[-50:]}")
        
        return jsonify({
            "status": "success",
            "message": f"Cache updated for {image_url}",
            "data_length": len(new_data)
        })
    
    except Exception as e:
        print(f"[Cache] Update failed: {e}")
        if is_debug_mode:
            traceback.print_exc()
        return jsonify({"error": f"Update failed: {e}"}), 500

# endregion


# region Main

def main():
    global ocr_engine, is_debug_mode
    parser = argparse.ArgumentParser(description="Run the Python OCR Server.")
    parser.add_argument("-d", "--debug", action="store_true", help="enable debug mode")
    parser.add_argument("-e", "--engine", type=str, default="lens", help="OCR engine to use: 'lens', 'oneocr', 'anglenet', etc.")
    args = parser.parse_args()
    is_debug_mode = args.debug

    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    os.makedirs(IMAGE_CACHE_FOLDER, exist_ok=True)

    print(f"[Engine] Initializing {args.engine}...")
    try:
        ocr_engine = initialize_engine(args.engine)
        print(f"[Engine] {args.engine} initialization complete.")
    except Exception as e:
        print(f"[Engine] Failed to initialize {args.engine}: {e}")
        raise SystemExit(1)

    load_cache()

    if is_debug_mode:
        print("--- Starting Flask Development Server in DEBUG MODE ---")
        app.run(host=IP_ADDRESS, port=PORT, debug=True, use_reloader=False)
    else:
        print("--- Starting Waitress Production Server ---")
        print(f"URL: http://{IP_ADDRESS}:{PORT}")
        serve(app, host=IP_ADDRESS, port=PORT)


if __name__ == "__main__":
    main()

# endregion
