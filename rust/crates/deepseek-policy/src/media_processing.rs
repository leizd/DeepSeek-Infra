//! Retained media extraction and segment/citation semantics, without state writes.
//! The worker binds these results to an original fenced preparation before any
//! library, segment or index publication. Python is an offline oracle only.
use crate::{
    app_error::{AppError, codes},
    core_utils::{python_float_opt, python_int_opt, python_truthy, text_or_empty},
    entropy::Entropy,
    media_schema::{MAX_SEGMENT_TEXT_CHARS, normalize_media_path},
    workspace_schema::{normalize_source_ref, redact_sensitive_text, validate_workspace_id},
};
use blake2::{
    Blake2bVar,
    digest::{Update, VariableOutput},
};
use serde_json::{Value, json};

fn first<'a>(object: &'a Value, keys: &[&str]) -> Option<&'a Value> {
    keys.iter()
        .filter_map(|key| object.get(*key))
        .find(|v| python_truthy(v))
}

fn pick_text(object: &Value, keys: &[&str]) -> String {
    text_or_empty(first(object, keys)).trim().to_owned()
}

fn rounded(value: f64, digits: usize) -> f64 {
    // Decimal formatting rounds the original binary f64, including even ties.
    format!("{value:.digits$}").parse().unwrap_or(value)
}

fn time_range(value: &Value) -> Vec<f64> {
    let Some(items) = value.as_array().filter(|v| v.len() >= 2) else {
        return vec![];
    };
    let (Some(start), Some(end)) = (python_float_opt(&items[0]), python_float_opt(&items[1]))
    else {
        return vec![];
    };
    let start = start.max(0.0);
    vec![rounded(start, 3), rounded(end.max(start), 3)]
}

fn item_time_range(value: &Value) -> Vec<f64> {
    if value.get("timeRange").is_some_and(Value::is_array) {
        time_range(&value["timeRange"])
    } else {
        time_range(&json!([value.get("startSec"), value.get("endSec")]))
    }
}

fn duration(metadata: &Value) -> f64 {
    metadata
        .get("durationSec")
        .and_then(python_float_opt)
        .unwrap_or(0.0)
        .max(0.0)
}

fn integer(value: Option<&Value>, fallback: i64) -> Result<i64, AppError> {
    let Some(value) = value else {
        return Ok(fallback);
    };
    python_int_opt(Some(value)).ok_or_else(|| AppError {
        code: codes::INTERNAL,
        status: 500,
        message: "Media processing failed: invalid integer metadata".into(),
    })
}

fn transcript_chunks(input: &str) -> Vec<String> {
    let normalized = input
        .replace("\r\n", "\n")
        .split([
            '\n', '\r', '\u{000b}', '\u{000c}', '\u{001c}', '\u{001d}', '\u{001e}', '\u{0085}',
            '\u{2028}', '\u{2029}',
        ])
        .map(str::trim)
        .collect::<Vec<_>>()
        .join("\n")
        .trim()
        .to_owned();
    let mut pieces = Vec::new();
    for paragraph in normalized
        .split('\n')
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        if paragraph.chars().count() <= 1200 {
            pieces.push(paragraph.to_owned());
            continue;
        }
        let mut current = String::new();
        for word in paragraph.split_whitespace() {
            let candidate = if current.is_empty() {
                word.to_owned()
            } else {
                format!("{current} {word}")
            };
            if candidate.chars().count() <= 1200 {
                current = candidate;
            } else {
                if !current.is_empty() {
                    pieces.push(current);
                }
                current = word.to_owned();
            }
        }
        if !current.is_empty() {
            pieces.push(current);
        }
    }
    let mut result = Vec::new();
    let mut current = String::new();
    for piece in pieces {
        let candidate = if current.is_empty() {
            piece.clone()
        } else {
            format!("{current}\n{piece}")
        };
        if candidate.chars().count() <= 1200 {
            current = candidate;
        } else {
            if !current.is_empty() {
                result.push(current);
            }
            current = piece;
        }
    }
    if !current.is_empty() {
        result.push(current);
    }
    result
}

fn transcript_segments(input: &str, duration: f64) -> Vec<Value> {
    let chunks = transcript_chunks(input);
    let step = if duration > 0.0 {
        duration / chunks.len().max(1) as f64
    } else {
        0.0
    };
    chunks
        .into_iter()
        .enumerate()
        .map(|(index, text)| {
            let mut segment =
                json!({"type":"transcript", "text":text, "confidence":1.0, "index":index});
            if step > 0.0 {
                let start = rounded(index as f64 * step, 3);
                segment["timeRange"] = json!([start, rounded(start + step, 3)]);
            }
            segment
        })
        .collect()
}

fn sorted_items(items: &Value, frames: bool) -> Result<Vec<Value>, AppError> {
    let mut result = Vec::new();
    for (source_index, item) in items.as_array().into_iter().flatten().enumerate() {
        let (text, range, confidence, frame_path) = if item.is_object() {
            (
                pick_text(
                    item,
                    if frames {
                        &["text", "caption"]
                    } else {
                        &["text", "transcript"]
                    },
                ),
                item_time_range(item),
                item.get("confidence").cloned().unwrap_or(json!(1.0)),
                if frames {
                    normalize_media_path(item.get("framePath"))?
                } else {
                    String::new()
                },
            )
        } else {
            (
                text_or_empty(Some(item)).trim().to_owned(),
                vec![],
                json!(1.0),
                String::new(),
            )
        };
        if text.is_empty() {
            continue;
        }
        let start = range.first().copied().unwrap_or(f64::INFINITY);
        let mut value = json!({"type":if frames {"frame"} else {"transcript"},"text":text,"confidence":if frames {json!(1.0)} else {confidence}});
        if !range.is_empty() {
            value["timeRange"] = json!(range);
        }
        if !frame_path.is_empty() {
            value["framePath"] = json!(frame_path);
        }
        result.push((start, source_index, value));
    }
    result.sort_by(|a, b| a.0.total_cmp(&b.0).then_with(|| a.1.cmp(&b.1)));
    Ok(result
        .into_iter()
        .enumerate()
        .map(|(index, (_, _, mut value))| {
            if !frames {
                value["index"] = json!(index);
            }
            value
        })
        .collect())
}

fn page_texts(metadata: &Value) -> Result<Vec<Value>, AppError> {
    let raw = first(metadata, &["pageTexts", "pages"])
        .cloned()
        .unwrap_or(Value::Null);
    let raw = if let Some(text) = raw.as_str() {
        serde_json::from_str(text).unwrap_or(Value::Null)
    } else {
        raw
    };
    let mut result = Vec::new();
    for (index, item) in raw.as_array().into_iter().flatten().enumerate() {
        let (text, page) = if item.is_object() {
            (
                pick_text(item, &["text"]),
                integer(first(item, &["page"]), index as i64 + 1)?,
            )
        } else {
            (
                text_or_empty(Some(item)).trim().to_owned(),
                index as i64 + 1,
            )
        };
        if !text.is_empty() {
            result.push(json!({"page":page,"text":text}));
        }
    }
    Ok(result)
}

fn raw_segments(media: &Value, source: &[u8], ocr_enabled: bool) -> Result<Vec<Value>, AppError> {
    let metadata = &media["metadata"];
    match media["type"].as_str().unwrap_or("") {
        "image" | "screenshot" => {
            let mut text = pick_text(metadata, &["ocrText", "text"]);
            if !source.is_empty() && ocr_enabled {
                match crate::ocr::ocr_image(source) {
                    Ok(extracted) if !extracted.is_empty() => text = extracted,
                    Ok(_) => {}
                    Err(error) if text.is_empty() => return Err(error),
                    Err(_) => {}
                }
            }
            let mut caption = text_or_empty(metadata.get("caption")).trim().to_owned();
            if caption.is_empty() {
                caption = format!("Image: {}", pick_text(media, &["title", "mediaId"]));
            }
            let mut result = vec![];
            if !text.is_empty() {
                result.push(json!({"type":"ocr_text", "text":text, "page":1,"confidence":metadata.get("ocrConfidence").cloned().unwrap_or(json!(1.0))}));
            }
            if !caption.is_empty() && caption != text {
                result.push(json!({"type":"caption","text":caption,"page":1,"confidence":1.0}));
            }
            Ok(result)
        }
        "pdf" => {
            let mut pages = page_texts(metadata)?;
            if !source.is_empty() && pages.is_empty() {
                match crate::pdf_text::extract_pdf_document(source) {
                    Ok(extracted) if !extracted.page_texts.is_empty() => {
                        pages = extracted.page_texts
                    }
                    result => {
                        if ocr_enabled {
                            let (text, _) = crate::ocr::ocr_pdf_document(source)?;
                            let fallback_count = result.as_ref().map(|p| p.page_count).unwrap_or(1);
                            let count =
                                integer(first(metadata, &["pageCount"]), fallback_count)?.max(1);
                            pages = crate::file_routes::fallback_page_texts_from_text(&text, count);
                        } else {
                            return Err(match result {
                                Ok(_) => AppError { code:"pdf_no_selectable_text",status:422,message:"No selectable text found in this PDF. It may be a scanned/image-only PDF and needs OCR.".into() },
                                Err(error) => error,
                            });
                        }
                    }
                }
            }
            if pages.is_empty() {
                let text = pick_text(metadata, &["text", "pdfText"]);
                if !text.is_empty() {
                    pages = crate::file_routes::fallback_page_texts_from_text(
                        &text,
                        integer(first(metadata, &["pageCount"]), 1)?,
                    );
                }
            }
            Ok(pages.into_iter().enumerate().filter_map(|(index,item)| {
                let text = pick_text(&item,&["text"]);
                if text.is_empty() { None } else { Some(json!({"type":"page_text","text":text,"page":item.get("page").cloned().unwrap_or(json!(index+1)),"confidence":1.0})) }
            }).collect())
        }
        "webpage" => {
            let mut text = pick_text(metadata, &["webpageText", "text"]);
            if text.is_empty() && !source.is_empty() {
                text = crate::fetch_url::extract_html_text(source);
            }
            Ok(if text.is_empty() {
                vec![]
            } else {
                vec![json!({"type":"webpage_text","text":text,"confidence":1.0})]
            })
        }
        "audio" | "video" => {
            let mut segments = sorted_items(
                first(metadata, &["transcriptSegments", "segments"]).unwrap_or(&Value::Null),
                false,
            )?;
            if segments.is_empty() {
                segments = transcript_segments(
                    &pick_text(metadata, &["transcript", "transcriptText"]),
                    duration(metadata),
                );
            }
            if media["type"] == "video" {
                let frames = sorted_items(
                    first(metadata, &["frames", "frameCaptions"]).unwrap_or(&Value::Null),
                    true,
                )?;
                for (index, mut frame) in frames.into_iter().enumerate() {
                    // Preserve the original append-time len + index behavior.
                    frame["index"] = json!(segments.len() + index);
                    segments.push(frame);
                }
            }
            Ok(segments)
        }
        _ => Err(AppError::invalid_payload("Unsupported media type")),
    }
}

fn normalize_segment(
    raw: &Value,
    media_id: &str,
    index: usize,
    entropy: &dyn Entropy,
) -> Result<Value, AppError> {
    let kind = pick_text(raw, &["type"]).to_lowercase();
    let kind = if kind.is_empty() { "page_text" } else { &kind };
    if ![
        "ocr_text",
        "caption",
        "transcript",
        "frame",
        "page_text",
        "webpage_text",
    ]
    .contains(&kind)
    {
        return Err(AppError::invalid_payload("Unsupported media segment type"));
    }
    let id = pick_text(raw, &["segmentId"]);
    let id = if id.is_empty() {
        format!("seg_{}", entropy.new_id()?)
    } else {
        validate_workspace_id(&id, "segment id")?
    };
    let confidence = raw
        .get("confidence")
        .and_then(python_float_opt)
        .unwrap_or(1.0);
    let text: String = redact_sensitive_text(&text_or_empty(raw.get("text")))
        .chars()
        .take(MAX_SEGMENT_TEXT_CHARS)
        .collect();
    let mut result = json!({"segmentId":id,"mediaId":validate_workspace_id(media_id,"media id")?,"type":kind,"text":text,
        "confidence":rounded(if confidence.is_nan() {1.0} else {confidence.clamp(0.0,1.0)},4),
        "index":integer(raw.get("index").filter(|v| !v.is_null()),index as i64)?});
    if raw.get("page").is_some_and(|v| !v.is_null()) {
        result["page"] = json!(integer(first(raw, &["page"]), 1)?.max(1));
    }
    let range = time_range(raw.get("timeRange").unwrap_or(&Value::Null));
    if !range.is_empty() {
        result["timeRange"] = json!(range);
    }
    let path = normalize_media_path(raw.get("framePath"))?;
    if !path.is_empty() {
        result["framePath"] = json!(path);
    }
    if raw.get("citation").is_some_and(Value::is_object) {
        result["citation"] = normalize_source_ref(&raw["citation"]);
    }
    Ok(result)
}

fn citation(media: &Value, segment: &Value, ordinal: usize) -> Value {
    let id = pick_text(media, &["mediaId"]);
    let kind = pick_text(media, &["type"]);
    let segment_id = pick_text(segment, &["segmentId"]);
    let page = segment["page"].as_i64().unwrap_or(0);
    let range = segment["timeRange"].as_array().cloned().unwrap_or_default();
    let prefix = if kind == "webpage" { "W" } else { "M" };
    let mut label = format!("{prefix}{}", ordinal.max(1));
    let locator = if page != 0 {
        label.push_str(&format!("-p{page}"));
        format!("page={page}")
    } else if range.len() >= 2 {
        let start = range[0].as_f64().unwrap_or(0.0);
        let secs = (start as i64).max(0);
        label.push_str(&format!(
            "-{:02}:{:02}:{:02}",
            secs / 3600,
            (secs % 3600) / 60,
            secs % 60
        ));
        format!("t={start:.3}")
    } else {
        format!("segment={segment_id}")
    };
    let uri = format!("media://{id}#{locator}");
    json!({"label":label,"uri":uri,"markdown":format!("[^{label}]"),"mediaId":id,"segmentId":segment_id,"page":page,"timeRange":range,"type":kind})
}

/// The same two normalization passes used by process_media + save_segments.
pub fn prepare_segments(
    media: &Value,
    source: &[u8],
    ocr_enabled: bool,
    entropy: &dyn Entropy,
) -> Result<Vec<Value>, AppError> {
    let id = pick_text(media, &["mediaId"]);
    raw_segments(media, source, ocr_enabled)?
        .iter()
        .enumerate()
        .map(|(index, raw)| {
            let mut value = normalize_segment(raw, &id, index, entropy)?;
            value["citation"] = citation(media, &value, index + 1);
            normalize_segment(&value, &id, index, entropy)
        })
        .collect()
}

pub fn segment_metadata(segments: &[Value], indexed: usize) -> Value {
    let page = segments
        .iter()
        .filter_map(|s| s["page"].as_i64())
        .filter(|p| *p > 0)
        .max()
        .unwrap_or(0);
    let duration = segments
        .iter()
        .filter_map(|s| {
            s["timeRange"]
                .as_array()
                .and_then(|r| r.get(1))
                .and_then(Value::as_f64)
        })
        .reduce(f64::max)
        .map_or(json!(0), |value| json!(value));
    json!({"segmentCount":segments.len(),"indexedChunkCount":indexed,"pageCount":page,"durationSec":duration})
}

fn digest(text: &str, size: usize) -> Vec<u8> {
    let mut hash = Blake2bVar::new(size).expect("fixed digest size");
    hash.update(text.as_bytes());
    let mut bytes = vec![0; size];
    hash.finalize_variable(&mut bytes)
        .expect("fixed digest size");
    bytes
}

/// Exact legacy rag_items projection. The returned rows are preparation only;
/// this module never opens the index or acquires write authority.
pub fn index_items(
    media: &Value,
    segments: &[Value],
    dimensions: usize,
    updated_at: i64,
) -> Vec<Value> {
    let media_id = pick_text(media, &["mediaId"]);
    segments.iter().enumerate().filter_map(|(index,segment)| {
        let text=pick_text(segment,&["text"]);
        let segment_id=pick_text(segment,&["segmentId"]);
        if text.is_empty() || segment_id.is_empty() { return None; }
        let citation=&segment["citation"];
        let item_id=format!("media:{media_id}:{segment_id}:{index}");
        let vector_id=u64::from_be_bytes(digest(&item_id,8).try_into().expect("eight bytes")) & 0x7fff_ffff_ffff_ffff;
        let embedding=crate::attachment_context::hash_text_embedding(&text,dimensions);
        let embedding:Vec<Value>=embedding.into_iter().map(|n|json!(n)).collect();
        // upsert_item performs its own second normalization before persistence.
        let embedding=crate::attachment_context::normalize_vector(&embedding,dimensions);
        let hash=crate::core_utils::encode_lower_hex(&digest(&text,12));
        let stored_text:String=text.chars().take(12_000).collect();
        Some(json!({"item_id":item_id,"vector_id":vector_id,"collection":"media","source_id":media_id,
            "project_id":pick_text(media,&["projectId"]),"chunk_index":index,"name":pick_text(media,&["title","mediaId"]),
            "kind":pick_text(media,&["type"]),"scope":"","text":stored_text,"embedding":embedding,"updated_at":updated_at,
            "metadata":{"sourceType":"media","mediaId":media_id,"segmentId":segment_id,"segmentType":pick_text(segment,&["type"]),
                "page":segment["page"].as_i64().unwrap_or(0),"timeRange":segment["timeRange"].as_array().cloned().unwrap_or_default(),
                "framePath":pick_text(segment,&["framePath"]),"citation":pick_text(citation,&["uri"]),"citationLabel":pick_text(citation,&["label"]),"hash":hash}}))
    }).collect()
}
