"""Generate downloadable SVG mind maps as a clustered top-down flowchart.

Rather than a radial tree, top-level nodes become titled, tinted *cluster
containers* (like Mermaid subgraphs); inside each container the descendants flow
top-to-bottom, connected by solid arrowed edges, drawn as rounded "card" nodes.
This matches the grouped-flowchart look —— 标题容器 + 箭头流向 + 圆角节点 —— that
reads far better than a horizontal radial tree for comparison / process diagrams.
"""

from __future__ import annotations

import html
import re
from typing import Any

from deepseek_infra.core.errors import AppError, ErrorCode
from deepseek_infra.infra.tool_runtime.generated_files import store_generated_file

MAX_NODES = 120
MAX_DEPTH = 6
MAX_LABEL_CHARS = 120

NODE_W = 190          # 节点卡片宽
NODE_H = 60           # 节点卡片高（容纳 2-3 行）
H_GAP = 30            # 同层兄弟子树之间的水平间距
V_GAP = 50            # 层与层之间的垂直间距（留给箭头）
CLUSTER_PAD = 24      # 容器内边距
TITLE_BAND_H = 46     # 容器顶部标题区高度
CLUSTER_GAP = 48      # 容器之间的水平间距
MARGIN = 40           # 画布外边距
HEADER_H = 84         # 顶部留给思维导图标题 + 副标题的区域

NODE_FILL = "F3F0FF"      # 节点卡片浅紫底
NODE_STROKE = "9F7AEA"    # 节点卡片紫色描边
NODE_TEXT = "1F2933"      # 节点文字
EDGE_COLOR = "475569"     # 箭头颜色
TITLE_COLOR = "0F172A"    # 主标题
SUBTITLE_COLOR = "64748B"  # 副标题
BG_COLOR = "FFFFFF"

# 容器配色（按出现顺序循环）：border 描边、fill 浅色底、title 标题色。
_THEMES = (
    ("3B82F6", "E8F1FE", "1E3A8A"),  # 蓝
    ("22C55E", "E9F7EF", "15803D"),  # 绿
    ("F97316", "FEF1E4", "9A3412"),  # 橙
    ("8B5CF6", "F3EBFC", "6B21A8"),  # 紫
    ("06B6D4", "E6F7FB", "155E75"),  # 青
    ("E11D48", "FEECF0", "9F1239"),  # 玫红
)


def create_mindmap(title: str, nodes: Any, *, subtitle: str = "") -> dict[str, Any]:
    clean_title = _clean_label(title)
    if not clean_title:
        raise AppError("Mind map requires a title.", code=ErrorCode.INVALID_PAYLOAD)
    children = _normalize_nodes(nodes, depth=1, counter=[0])
    if not children:
        raise AppError("Mind map requires at least one node.", code=ErrorCode.INVALID_PAYLOAD)

    clusters, total_w, total_h = _layout_clusters(children)
    svg = _render_svg(clean_title, _clean_label(subtitle), clusters, total_w, total_h)
    stored = store_generated_file(clean_title, "svg", lambda path: _write_text(path, svg))
    return {
        "fileId": stored["fileId"],
        "filename": stored["filename"],
        "format": "svg",
        "nodeCount": _count_nodes(children),
        "downloadUrl": stored["downloadUrl"],
        "title": clean_title,
        "outline": _outline(children),
    }


def _clean_label(value: Any, *, limit: int = MAX_LABEL_CHARS) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _normalize_nodes(value: Any, *, depth: int, counter: list[int]) -> list[dict[str, Any]]:
    if depth > MAX_DEPTH or not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if counter[0] >= MAX_NODES:
            break
        if isinstance(item, str):
            label = _clean_label(item)
            raw_children: Any = []
        elif isinstance(item, dict):
            label = _clean_label(item.get("label") or item.get("title") or item.get("name"))
            raw_children = item.get("children")
        else:
            continue
        if not label:
            continue
        counter[0] += 1
        result.append({"label": label, "children": _normalize_nodes(raw_children, depth=depth + 1, counter=counter)})
    return result


def _count_nodes(children: list[dict[str, Any]]) -> int:
    return sum(1 + _count_nodes(child.get("children") or []) for child in children)


# ---------------------------------------------------------------------------
# 布局：每个顶层节点 = 一个容器，容器内的后代做自上而下的整齐树排布
# ---------------------------------------------------------------------------


def _layout_cluster_nodes(children: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float, float]:
    """把容器内的后代森林排成自上而下的树，返回 (节点列表, 内容宽, 内容高)。

    节点坐标相对容器内容区左上角 (0, 0)。每个节点带 ``parent`` 指向同容器内父节点的下标。
    """
    placed: list[dict[str, Any]] = []
    leaf_cursor = [0]

    def place(node: dict[str, Any], depth: int, parent: int | None) -> int:
        index = len(placed)
        placed.append({})  # 占位，子节点排完再回填父节点 x
        kids = node.get("children") or []
        if kids:
            child_indices = [place(child, depth + 1, index) for child in kids]
            xs = [placed[c]["x"] for c in child_indices]
            x = (min(xs) + max(xs)) / 2
        else:
            x = leaf_cursor[0] * (NODE_W + H_GAP)
            leaf_cursor[0] += 1
        placed[index] = {
            "label": node["label"],
            "depth": depth,
            "x": x,
            "y": depth * (NODE_H + V_GAP),
            "parent": parent,
        }
        return index

    for child in children:
        place(child, 0, None)

    if not placed:
        return [], 0.0, 0.0
    min_x = min(item["x"] for item in placed)
    for item in placed:
        item["x"] -= min_x
    content_w = max(item["x"] for item in placed) + NODE_W
    content_h = max(item["y"] for item in placed) + NODE_H
    return placed, content_w, content_h


def _layout_clusters(top_level: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float, float]:
    clusters: list[dict[str, Any]] = []
    x_cursor: float = MARGIN
    container_y = HEADER_H
    max_h = 0.0

    for cluster_index, cluster in enumerate(top_level):
        inner, content_w, content_h = _layout_cluster_nodes(cluster.get("children") or [])
        title_w = _text_width(cluster["label"], 15)
        container_w = max(content_w, title_w, 150.0) + 2 * CLUSTER_PAD
        container_h = TITLE_BAND_H + content_h + (CLUSTER_PAD if content_h else CLUSTER_PAD)
        container_x = x_cursor

        offset_x = container_x + (container_w - content_w) / 2
        offset_y = container_y + TITLE_BAND_H
        nodes = [
            {
                "label": item["label"],
                "x": offset_x + item["x"],
                "y": offset_y + item["y"],
                "parent": item["parent"],
            }
            for item in inner
        ]

        clusters.append(
            {
                "label": cluster["label"],
                "theme": _THEMES[cluster_index % len(_THEMES)],
                "x": container_x,
                "y": container_y,
                "w": container_w,
                "h": container_h,
                "nodes": nodes,
            }
        )
        x_cursor = container_x + container_w + CLUSTER_GAP
        max_h = max(max_h, container_h)

    total_w = x_cursor - CLUSTER_GAP + MARGIN
    total_h = container_y + max_h + MARGIN
    return clusters, total_w, total_h


# ---------------------------------------------------------------------------
# 文本测量 / 换行
# ---------------------------------------------------------------------------


def _is_cjk(char: str) -> bool:
    return ord(char) > 0x2E7F


def _text_width(text: str, font_size: float) -> float:
    width = 0.0
    for char in str(text):
        width += font_size * (1.0 if _is_cjk(char) else 0.56)
    return width


def _is_ascii_word(token: str) -> bool:
    return bool(token) and not _is_cjk(token[0])


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    buffer = ""
    for char in str(text):
        if char == " ":
            if buffer:
                tokens.append(buffer)
                buffer = ""
            continue
        if _is_cjk(char):
            if buffer:
                tokens.append(buffer)
                buffer = ""
            tokens.append(char)
        else:
            buffer += char
    if buffer:
        tokens.append(buffer)
    return tokens


def _wrap_label(label: str, *, max_width: float, font_size: float, max_lines: int = 3) -> list[str]:
    tokens = _tokenize(label)
    if not tokens:
        return [""]
    space_w = font_size * 0.3
    lines: list[list[str]] = []
    current: list[str] = []
    current_w = 0.0
    truncated = False
    for token in tokens:
        add_w = _text_width(token, font_size) + (space_w if current else 0)
        if current and current_w + add_w > max_width:
            lines.append(current)
            if len(lines) >= max_lines:
                truncated = True
                current = []
                break
            current = [token]
            current_w = _text_width(token, font_size)
        else:
            current.append(token)
            current_w += add_w
    if current and len(lines) < max_lines:
        lines.append(current)

    rendered = [_join_tokens(line) for line in lines[:max_lines]]
    if truncated and rendered:
        rendered[-1] = rendered[-1].rstrip() + "…"
    return rendered or [""]


def _join_tokens(tokens: list[str]) -> str:
    # 在 ASCII 词与相邻 token 之间补空格（中英之间、英英之间都加，中中之间不加），
    # 还原“只改 Cache 标记 dirty=1”这类中英混排的自然空格。
    out = ""
    for index, token in enumerate(tokens):
        if index > 0 and (_is_ascii_word(token) or _is_ascii_word(tokens[index - 1])):
            out += " "
        out += token
    return out


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def _render_svg(title: str, subtitle: str, clusters: list[dict[str, Any]], total_w: float, total_h: float) -> str:
    width = int(round(total_w))
    height = int(round(total_h))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_xml(title)}">',
        "<defs>",
        '<filter id="ndshadow" x="-20%" y="-20%" width="140%" height="140%">'
        '<feDropShadow dx="0" dy="2" stdDeviation="3" flood-color="#0f172a" flood-opacity="0.12"/></filter>',
        "</defs>",
        f'<rect width="100%" height="100%" fill="#{BG_COLOR}"/>',
    ]

    # 顶部标题 + 副标题（居中于整幅画布）
    center_x = width / 2
    parts.append(
        f'<text x="{center_x:.1f}" y="44" text-anchor="middle" fill="#{TITLE_COLOR}" '
        f'font-size="23" font-weight="700" font-family="Microsoft YaHei, Arial, sans-serif">{_xml(title)}</text>'
    )
    if subtitle:
        parts.append(
            f'<text x="{center_x:.1f}" y="68" text-anchor="middle" fill="#{SUBTITLE_COLOR}" '
            f'font-size="14" font-family="Microsoft YaHei, Arial, sans-serif">{_xml(subtitle)}</text>'
        )

    # 容器（背景在最底层）
    for cluster in clusters:
        border, fill, title_color = cluster["theme"]
        parts.append(
            f'<rect x="{cluster["x"]:.1f}" y="{cluster["y"]:.1f}" width="{cluster["w"]:.1f}" height="{cluster["h"]:.1f}" '
            f'rx="16" fill="#{fill}" stroke="#{border}" stroke-width="1.8"/>'
        )
        parts.append(
            f'<text x="{cluster["x"] + cluster["w"] / 2:.1f}" y="{cluster["y"] + 29:.1f}" text-anchor="middle" '
            f'fill="#{title_color}" font-size="15" font-weight="700" '
            f'font-family="Microsoft YaHei, Arial, sans-serif">{_xml(_clip_title(cluster["label"], cluster["w"]))}</text>'
        )

    # 箭头（父 → 子，自上而下）
    for cluster in clusters:
        nodes = cluster["nodes"]
        for node in nodes:
            parent = node["parent"]
            if parent is None:
                continue
            parent_node = nodes[int(parent)]
            sx = parent_node["x"] + NODE_W / 2
            sy = parent_node["y"] + NODE_H
            tx = node["x"] + NODE_W / 2
            tip_y = node["y"] - 1          # 箭头尖刚好贴到子节点上边
            line_end_y = node["y"] - 7     # 连线止于箭头底部
            midy = (sy + line_end_y) / 2
            # 连线用三次贝塞尔：父子同列时是竖直线，子节点偏移时自然带弧度。
            parts.append(
                f'<path d="M {sx:.1f} {sy:.1f} C {sx:.1f} {midy:.1f}, {tx:.1f} {midy:.1f}, {tx:.1f} {line_end_y:.1f}" '
                f'fill="none" stroke="#{EDGE_COLOR}" stroke-width="2"/>'
            )
            # 显式画三角箭头（不依赖 marker，部分 SVG 渲染器/转换器不支持 marker-end）。
            parts.append(
                f'<path d="M {tx - 5:.1f} {line_end_y:.1f} L {tx + 5:.1f} {line_end_y:.1f} L {tx:.1f} {tip_y:.1f} Z" '
                f'fill="#{EDGE_COLOR}"/>'
            )

    # 节点卡片（在箭头之上）
    for cluster in clusters:
        for node in cluster["nodes"]:
            parts.extend(_render_node(node["x"], node["y"], str(node["label"])))

    parts.append("</svg>")
    return "\n".join(parts)


def _render_node(x: float, y: float, label: str) -> list[str]:
    lines = _wrap_label(label, max_width=NODE_W - 22, font_size=13, max_lines=3)
    parts = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{NODE_W}" height="{NODE_H}" rx="9" '
        f'fill="#{NODE_FILL}" stroke="#{NODE_STROKE}" stroke-width="1.5" filter="url(#ndshadow)"/>'
    ]
    line_height = 17
    start_y = y + NODE_H / 2 - (len(lines) - 1) * line_height / 2 + 5
    for line_index, line in enumerate(lines):
        parts.append(
            f'<text x="{x + NODE_W / 2:.1f}" y="{start_y + line_index * line_height:.1f}" text-anchor="middle" '
            f'fill="#{NODE_TEXT}" font-size="13" font-weight="500" '
            f'font-family="Microsoft YaHei, Arial, sans-serif">{_xml(line)}</text>'
        )
    return parts


def _clip_title(label: str, container_w: float) -> str:
    max_width = container_w - 2 * CLUSTER_PAD
    if _text_width(label, 15) <= max_width:
        return label
    clipped = label
    while clipped and _text_width(clipped + "…", 15) > max_width:
        clipped = clipped[:-1]
    return (clipped + "…") if clipped else label[:1]


def _outline(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"label": node["label"], "children": _outline(node.get("children") or [])} for node in nodes[:MAX_NODES]]


def _xml(value: Any) -> str:
    return html.escape(str(value or ""), quote=True)


def _write_text(path: Any, text: str) -> None:
    path.write_text(text, encoding="utf-8")
