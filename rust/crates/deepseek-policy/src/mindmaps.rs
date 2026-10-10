//! Generate downloadable SVG mind maps, mirroring
//! `deepseek_infra/infra/tool_runtime/mindmaps.py`.
//!
//! Pure layout + SVG. Persistence goes through [`crate::generated_files`].

use serde_json::{Map, Value, json};

use crate::app_error::{AppError, codes};
use crate::core_utils::python_truthy;
use crate::entropy::Entropy;
use crate::generated_files::store_generated_file;
use crate::memory_index::python_round;
use crate::python_json::value_str;

pub const MAX_NODES: usize = 120;
pub const MAX_DEPTH: i32 = 6;
pub const MAX_LABEL_CHARS: usize = 120;

const NODE_W: f64 = 190.0;
const NODE_H: f64 = 60.0;
const H_GAP: f64 = 30.0;
const V_GAP: f64 = 50.0;
const CLUSTER_PAD: f64 = 24.0;
const TITLE_BAND_H: f64 = 46.0;
const CLUSTER_GAP: f64 = 48.0;
const MARGIN: f64 = 40.0;
const HEADER_H: f64 = 84.0;

const NODE_FILL: &str = "F3F0FF";
const NODE_STROKE: &str = "9F7AEA";
const NODE_TEXT: &str = "1F2933";
const EDGE_COLOR: &str = "475569";
const TITLE_COLOR: &str = "0F172A";
const SUBTITLE_COLOR: &str = "64748B";
const BG_COLOR: &str = "FFFFFF";

const THEMES: [(&str, &str, &str); 6] = [
    ("3B82F6", "E8F1FE", "1E3A8A"),
    ("22C55E", "E9F7EF", "15803D"),
    ("F97316", "FEF1E4", "9A3412"),
    ("8B5CF6", "F3EBFC", "6B21A8"),
    ("06B6D4", "E6F7FB", "155E75"),
    ("E11D48", "FEECF0", "9F1239"),
];

#[derive(Debug, Clone)]
struct Node {
    label: String,
    children: Vec<Node>,
}

#[derive(Debug, Clone)]
struct Placed {
    label: String,
    x: f64,
    y: f64,
    parent: Option<usize>,
}

#[derive(Debug, Clone)]
struct Cluster {
    label: String,
    theme: (&'static str, &'static str, &'static str),
    x: f64,
    y: f64,
    w: f64,
    h: f64,
    nodes: Vec<Placed>,
}

/// Mirrors `_clean_label`.
pub fn clean_label(value: &str, limit: usize) -> String {
    regex::Regex::new(r"\s+")
        .expect("static regex")
        .replace_all(value, " ")
        .trim()
        .chars()
        .take(limit)
        .collect()
}

fn python_or_empty(value: Option<&Value>) -> String {
    match value {
        Some(found) if python_truthy(found) => value_str(found),
        _ => String::new(),
    }
}

/// Mirrors `_normalize_nodes`.
fn normalize_nodes(value: &Value, depth: i32, counter: &mut usize) -> Vec<Node> {
    if depth > MAX_DEPTH {
        return Vec::new();
    }
    let Value::Array(items) = value else {
        return Vec::new();
    };
    let mut result = Vec::new();
    for item in items {
        if *counter >= MAX_NODES {
            break;
        }
        let (label, raw_children) = match item {
            Value::String(text) => (clean_label(text, MAX_LABEL_CHARS), Value::Array(Vec::new())),
            Value::Object(fields) => {
                let label = clean_label(
                    &{
                        let from_label = python_or_empty(fields.get("label"));
                        if !from_label.is_empty() {
                            from_label
                        } else {
                            let from_title = python_or_empty(fields.get("title"));
                            if !from_title.is_empty() {
                                from_title
                            } else {
                                python_or_empty(fields.get("name"))
                            }
                        }
                    },
                    MAX_LABEL_CHARS,
                );
                let children = fields.get("children").cloned().unwrap_or(Value::Null);
                (label, children)
            }
            _ => continue,
        };
        if label.is_empty() {
            continue;
        }
        *counter += 1;
        result.push(Node {
            label,
            children: normalize_nodes(&raw_children, depth + 1, counter),
        });
    }
    result
}

fn count_nodes(children: &[Node]) -> usize {
    children
        .iter()
        .map(|child| 1 + count_nodes(&child.children))
        .sum()
}

fn is_cjk(ch: char) -> bool {
    (ch as u32) > 0x2E7F
}

fn text_width(text: &str, font_size: f64) -> f64 {
    text.chars()
        .map(|ch| font_size * if is_cjk(ch) { 1.0 } else { 0.56 })
        .sum()
}

fn is_ascii_word(token: &str) -> bool {
    token.chars().next().is_some_and(|ch| !is_cjk(ch))
}

fn tokenize(text: &str) -> Vec<String> {
    let mut tokens = Vec::new();
    let mut buffer = String::new();
    for ch in text.chars() {
        if ch == ' ' {
            if !buffer.is_empty() {
                tokens.push(std::mem::take(&mut buffer));
            }
            continue;
        }
        if is_cjk(ch) {
            if !buffer.is_empty() {
                tokens.push(std::mem::take(&mut buffer));
            }
            tokens.push(ch.to_string());
        } else {
            buffer.push(ch);
        }
    }
    if !buffer.is_empty() {
        tokens.push(buffer);
    }
    tokens
}

fn join_tokens(tokens: &[String]) -> String {
    let mut out = String::new();
    for (index, token) in tokens.iter().enumerate() {
        if index > 0 && (is_ascii_word(token) || is_ascii_word(&tokens[index - 1])) {
            out.push(' ');
        }
        out.push_str(token);
    }
    out
}

fn wrap_label(label: &str, max_width: f64, font_size: f64, max_lines: usize) -> Vec<String> {
    let tokens = tokenize(label);
    if tokens.is_empty() {
        return vec![String::new()];
    }
    let space_w = font_size * 0.3;
    let mut lines: Vec<Vec<String>> = Vec::new();
    let mut current: Vec<String> = Vec::new();
    let mut current_w = 0.0;
    let mut truncated = false;
    for token in tokens {
        let add_w = text_width(&token, font_size) + if current.is_empty() { 0.0 } else { space_w };
        if !current.is_empty() && current_w + add_w > max_width {
            lines.push(std::mem::take(&mut current));
            if lines.len() >= max_lines {
                truncated = true;
                current.clear();
                break;
            }
            current_w = text_width(&token, font_size);
            current = vec![token];
        } else {
            current_w += add_w;
            current.push(token);
        }
    }
    if !current.is_empty() && lines.len() < max_lines {
        lines.push(current);
    }
    let mut rendered: Vec<String> = lines
        .into_iter()
        .take(max_lines)
        .map(|line| join_tokens(&line))
        .collect();
    if truncated {
        if let Some(last) = rendered.last_mut() {
            *last = format!("{}…", last.trim_end());
        }
    }
    if rendered.is_empty() {
        vec![String::new()]
    } else {
        rendered
    }
}

fn layout_cluster_nodes(children: &[Node]) -> (Vec<Placed>, f64, f64) {
    let mut placed: Vec<Placed> = Vec::new();
    let mut leaf_cursor = 0usize;

    fn place(
        node: &Node,
        depth: i32,
        parent: Option<usize>,
        placed: &mut Vec<Placed>,
        leaf_cursor: &mut usize,
    ) -> usize {
        let index = placed.len();
        placed.push(Placed {
            label: String::new(),
            x: 0.0,
            y: 0.0,
            parent: None,
        });
        let kids = &node.children;
        let x = if kids.is_empty() {
            let x = *leaf_cursor as f64 * (NODE_W + H_GAP);
            *leaf_cursor += 1;
            x
        } else {
            let child_indices: Vec<usize> = kids
                .iter()
                .map(|child| place(child, depth + 1, Some(index), placed, leaf_cursor))
                .collect();
            let min_x = child_indices
                .iter()
                .map(|c| placed[*c].x)
                .fold(f64::INFINITY, f64::min);
            let max_x = child_indices
                .iter()
                .map(|c| placed[*c].x)
                .fold(f64::NEG_INFINITY, f64::max);
            (min_x + max_x) / 2.0
        };
        placed[index] = Placed {
            label: node.label.clone(),
            x,
            y: f64::from(depth) * (NODE_H + V_GAP),
            parent,
        };
        index
    }

    for child in children {
        place(child, 0, None, &mut placed, &mut leaf_cursor);
    }
    if placed.is_empty() {
        return (Vec::new(), 0.0, 0.0);
    }
    let min_x = placed
        .iter()
        .map(|item| item.x)
        .fold(f64::INFINITY, f64::min);
    for item in &mut placed {
        item.x -= min_x;
    }
    let content_w = placed
        .iter()
        .map(|item| item.x)
        .fold(f64::NEG_INFINITY, f64::max)
        + NODE_W;
    let content_h = placed
        .iter()
        .map(|item| item.y)
        .fold(f64::NEG_INFINITY, f64::max)
        + NODE_H;
    (placed, content_w, content_h)
}

fn layout_clusters(top_level: &[Node]) -> (Vec<Cluster>, f64, f64) {
    let mut clusters = Vec::new();
    let mut x_cursor = MARGIN;
    let container_y = HEADER_H;
    let mut max_h: f64 = 0.0;
    for (cluster_index, cluster) in top_level.iter().enumerate() {
        let (inner, content_w, content_h) = layout_cluster_nodes(&cluster.children);
        let title_w = text_width(&cluster.label, 15.0);
        let container_w = content_w.max(title_w).max(150.0) + 2.0 * CLUSTER_PAD;
        let container_h = TITLE_BAND_H + content_h + CLUSTER_PAD;
        let container_x = x_cursor;
        let offset_x = container_x + (container_w - content_w) / 2.0;
        let offset_y = container_y + TITLE_BAND_H;
        let nodes = inner
            .into_iter()
            .map(|item| Placed {
                label: item.label,
                x: offset_x + item.x,
                y: offset_y + item.y,
                parent: item.parent,
            })
            .collect();
        clusters.push(Cluster {
            label: cluster.label.clone(),
            theme: THEMES[cluster_index % THEMES.len()],
            x: container_x,
            y: container_y,
            w: container_w,
            h: container_h,
            nodes,
        });
        x_cursor = container_x + container_w + CLUSTER_GAP;
        max_h = max_h.max(container_h);
    }
    let total_w = x_cursor - CLUSTER_GAP + MARGIN;
    let total_h = container_y + max_h + MARGIN;
    (clusters, total_w, total_h)
}

fn xml_escape(value: &str) -> String {
    value
        .replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn clip_title(label: &str, container_w: f64) -> String {
    let max_width = container_w - 2.0 * CLUSTER_PAD;
    if text_width(label, 15.0) <= max_width {
        return label.to_string();
    }
    let mut clipped: String = label.to_string();
    while !clipped.is_empty() && text_width(&format!("{clipped}…"), 15.0) > max_width {
        clipped.pop();
    }
    if clipped.is_empty() {
        label.chars().next().map(String::from).unwrap_or_default()
    } else {
        format!("{clipped}…")
    }
}

fn fmt1(value: f64) -> String {
    format!("{value:.1}")
}

fn render_node(x: f64, y: f64, label: &str) -> Vec<String> {
    let lines = wrap_label(label, NODE_W - 22.0, 13.0, 3);
    let mut parts = vec![format!(
        "<rect x=\"{}\" y=\"{}\" width=\"{}\" height=\"{}\" rx=\"9\" fill=\"#{NODE_FILL}\" stroke=\"#{NODE_STROKE}\" stroke-width=\"1.5\" filter=\"url(#ndshadow)\"/>",
        fmt1(x),
        fmt1(y),
        NODE_W as i64,
        NODE_H as i64
    )];
    let line_height = 17.0;
    let start_y =
        y + NODE_H / 2.0 - (lines.len().saturating_sub(1) as f64) * line_height / 2.0 + 5.0;
    for (line_index, line) in lines.iter().enumerate() {
        parts.push(format!(
            "<text x=\"{}\" y=\"{}\" text-anchor=\"middle\" fill=\"#{NODE_TEXT}\" font-size=\"13\" font-weight=\"500\" font-family=\"Microsoft YaHei, Arial, sans-serif\">{}</text>",
            fmt1(x + NODE_W / 2.0),
            fmt1(start_y + line_index as f64 * line_height),
            xml_escape(line)
        ));
    }
    parts
}

fn render_svg(
    title: &str,
    subtitle: &str,
    clusters: &[Cluster],
    total_w: f64,
    total_h: f64,
) -> String {
    let width = python_round(total_w) as i64;
    let height = python_round(total_h) as i64;
    let mut parts = vec![
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>".to_string(),
        format!(
            "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"{width}\" height=\"{height}\" viewBox=\"0 0 {width} {height}\" role=\"img\" aria-label=\"{}\">",
            xml_escape(title)
        ),
        "<defs>".to_string(),
        "<filter id=\"ndshadow\" x=\"-20%\" y=\"-20%\" width=\"140%\" height=\"140%\"><feDropShadow dx=\"0\" dy=\"2\" stdDeviation=\"3\" flood-color=\"#0f172a\" flood-opacity=\"0.12\"/></filter>".to_string(),
        "</defs>".to_string(),
        format!("<rect width=\"100%\" height=\"100%\" fill=\"#{BG_COLOR}\"/>"),
    ];
    let center_x = width as f64 / 2.0;
    parts.push(format!(
        "<text x=\"{}\" y=\"44\" text-anchor=\"middle\" fill=\"#{TITLE_COLOR}\" font-size=\"23\" font-weight=\"700\" font-family=\"Microsoft YaHei, Arial, sans-serif\">{}</text>",
        fmt1(center_x),
        xml_escape(title)
    ));
    if !subtitle.is_empty() {
        parts.push(format!(
            "<text x=\"{}\" y=\"68\" text-anchor=\"middle\" fill=\"#{SUBTITLE_COLOR}\" font-size=\"14\" font-family=\"Microsoft YaHei, Arial, sans-serif\">{}</text>",
            fmt1(center_x),
            xml_escape(subtitle)
        ));
    }
    for cluster in clusters {
        let (border, fill, title_color) = cluster.theme;
        parts.push(format!(
            "<rect x=\"{}\" y=\"{}\" width=\"{}\" height=\"{}\" rx=\"16\" fill=\"#{fill}\" stroke=\"#{border}\" stroke-width=\"1.8\"/>",
            fmt1(cluster.x),
            fmt1(cluster.y),
            fmt1(cluster.w),
            fmt1(cluster.h)
        ));
        parts.push(format!(
            "<text x=\"{}\" y=\"{}\" text-anchor=\"middle\" fill=\"#{title_color}\" font-size=\"15\" font-weight=\"700\" font-family=\"Microsoft YaHei, Arial, sans-serif\">{}</text>",
            fmt1(cluster.x + cluster.w / 2.0),
            fmt1(cluster.y + 29.0),
            xml_escape(&clip_title(&cluster.label, cluster.w))
        ));
    }
    for cluster in clusters {
        for node in &cluster.nodes {
            let Some(parent) = node.parent else {
                continue;
            };
            let parent_node = &cluster.nodes[parent];
            let sx = parent_node.x + NODE_W / 2.0;
            let sy = parent_node.y + NODE_H;
            let tx = node.x + NODE_W / 2.0;
            let tip_y = node.y - 1.0;
            let line_end_y = node.y - 7.0;
            let midy = (sy + line_end_y) / 2.0;
            parts.push(format!(
                "<path d=\"M {} {} C {} {}, {} {}, {} {}\" fill=\"none\" stroke=\"#{EDGE_COLOR}\" stroke-width=\"2\"/>",
                fmt1(sx),
                fmt1(sy),
                fmt1(sx),
                fmt1(midy),
                fmt1(tx),
                fmt1(midy),
                fmt1(tx),
                fmt1(line_end_y)
            ));
            parts.push(format!(
                "<path d=\"M {} {} L {} {} L {} {} Z\" fill=\"#{EDGE_COLOR}\"/>",
                fmt1(tx - 5.0),
                fmt1(line_end_y),
                fmt1(tx + 5.0),
                fmt1(line_end_y),
                fmt1(tx),
                fmt1(tip_y)
            ));
        }
    }
    for cluster in clusters {
        for node in &cluster.nodes {
            parts.extend(render_node(node.x, node.y, &node.label));
        }
    }
    parts.push("</svg>".to_string());
    parts.join("\n")
}

fn outline(nodes: &[Node]) -> Value {
    let items: Vec<Value> = nodes
        .iter()
        .take(MAX_NODES)
        .map(|node| {
            json!({
                "label": node.label,
                "children": outline(&node.children),
            })
        })
        .collect();
    Value::Array(items)
}

/// Mirrors `create_mindmap`.
pub fn create_mindmap(
    title: &str,
    nodes: &Value,
    subtitle: &str,
    root: &std::path::Path,
    entropy: &dyn Entropy,
    now_epoch: f64,
) -> Result<Value, AppError> {
    let clean_title = clean_label(title, MAX_LABEL_CHARS);
    if clean_title.is_empty() {
        return Err(AppError {
            message: "Mind map requires a title.".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let mut counter = 0usize;
    let children = normalize_nodes(nodes, 1, &mut counter);
    if children.is_empty() {
        return Err(AppError {
            message: "Mind map requires at least one node.".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let (clusters, total_w, total_h) = layout_clusters(&children);
    let svg = render_svg(
        &clean_title,
        &clean_label(subtitle, MAX_LABEL_CHARS),
        &clusters,
        total_w,
        total_h,
    );
    let stored = store_generated_file(&clean_title, "svg", root, entropy, now_epoch, |path| {
        std::fs::write(path, &svg)
    })?;
    let mut result = Map::new();
    result.insert("fileId".to_string(), stored["fileId"].clone());
    result.insert("filename".to_string(), stored["filename"].clone());
    result.insert("format".to_string(), json!("svg"));
    result.insert(
        "nodeCount".to_string(),
        json!(count_nodes(&children) as i64),
    );
    result.insert("downloadUrl".to_string(), stored["downloadUrl"].clone());
    result.insert("title".to_string(), json!(clean_title));
    result.insert("outline".to_string(), outline(&children));
    Ok(Value::Object(result))
}

/// Render the SVG without writing a file — for the parity probe.
pub fn render_mindmap_svg(title: &str, nodes: &Value, subtitle: &str) -> Result<String, AppError> {
    let clean_title = clean_label(title, MAX_LABEL_CHARS);
    if clean_title.is_empty() {
        return Err(AppError {
            message: "Mind map requires a title.".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let mut counter = 0usize;
    let children = normalize_nodes(nodes, 1, &mut counter);
    if children.is_empty() {
        return Err(AppError {
            message: "Mind map requires at least one node.".to_string(),
            code: codes::INVALID_PAYLOAD,
            status: 400,
        });
    }
    let (clusters, total_w, total_h) = layout_clusters(&children);
    Ok(render_svg(
        &clean_title,
        &clean_label(subtitle, MAX_LABEL_CHARS),
        &clusters,
        total_w,
        total_h,
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::entropy::SystemEntropy;
    use crate::generated_files;

    fn sample_nodes() -> Value {
        json!([
            {
                "label": "Market analysis",
                "children": [
                    {"label": "User profile", "children": []},
                    {"label": "Competition", "children": [{"label": "Pricing", "children": []}]}
                ]
            },
            {
                "label": "Product strategy",
                "children": [
                    {"label": "Core features", "children": []},
                    {"label": "Launch rhythm", "children": []}
                ]
            }
        ])
    }

    #[test]
    fn empty_title_and_empty_nodes_are_invalid() {
        let root = std::env::temp_dir().join(format!("mindmap-empty-{}", std::process::id()));
        let err = create_mindmap("", &sample_nodes(), "", &root, &SystemEntropy, 0.0).unwrap_err();
        assert_eq!(err.message, "Mind map requires a title.");
        let err = create_mindmap("Empty", &json!([]), "", &root, &SystemEntropy, 0.0).unwrap_err();
        assert_eq!(err.message, "Mind map requires at least one node.");
    }

    #[test]
    fn create_mindmap_writes_an_svg() {
        let root = std::env::temp_dir().join(format!("mindmap-ok-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let result = create_mindmap(
            "Growth plan",
            &sample_nodes(),
            "2026",
            &root,
            &SystemEntropy,
            generated_files::system_now(),
        )
        .unwrap();
        assert_eq!(result["format"], "svg");
        assert!(result["nodeCount"].as_i64().unwrap() >= 6);
        assert!(
            result["downloadUrl"]
                .as_str()
                .unwrap()
                .starts_with("/api/download?id=")
        );
        let file_id = result["fileId"].as_str().unwrap();
        let path = generated_files::resolve_generated_file(&root, file_id).unwrap();
        let text = std::fs::read_to_string(path).unwrap();
        assert!(text.contains("<svg"));
        assert!(text.contains("Growth plan"));
        let _ = std::fs::remove_dir_all(&root);
    }

    #[test]
    fn tokenize_and_join_match_the_oracle() {
        assert!(is_cjk('中'));
        assert!(is_ascii_word("word"));
        assert!(!is_ascii_word(""));
        assert_eq!(
            tokenize("hello 世界 test"),
            vec!["hello", "世", "界", "test"]
        );
        assert_eq!(join_tokens(&["中".into(), "文".into()]), "中文");
        assert_eq!(
            join_tokens(&["中".into(), "word".into(), "文".into()]),
            "中 word 文"
        );
        assert_eq!(xml_escape("\"<&"), "&quot;&lt;&amp;");
    }
}
