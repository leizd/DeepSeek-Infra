# Multimodal Media Layer

Applicable version: v2.7.2.

DeepSeek Infra v2.7.2 makes media a first-class workspace object. A media item can belong to a project, keep source metadata, move through a processing lifecycle, emit citable segments, enter Local RAG, and travel with Project Export.

## Object Model

Media metadata is stored under `.media/library.json`; source files are stored under `.media/objects/{mediaId}/`; extracted segments are stored under `.media/segments/{mediaId}.json`.

```json
{
  "mediaId": "media_xxx",
  "projectId": "proj_xxx",
  "type": "image | pdf | audio | video | webpage | screenshot",
  "title": "Quarterly deck",
  "mimeType": "application/pdf",
  "path": "objects/media_xxx/source.pdf",
  "source": {"kind": "upload | generated | browser | automation | artifact", "refId": "..."},
  "status": "pending | processing | ready | failed",
  "createdAt": "2026-07-01T00:00:00Z",
  "updatedAt": "2026-07-01T00:00:00Z",
  "metadata": {"pageCount": 12, "durationSec": 123}
}
```

Segments are the searchable/citable unit:

```json
{
  "segmentId": "seg_xxx",
  "mediaId": "media_xxx",
  "type": "ocr_text | caption | transcript | frame | page_text | webpage_text",
  "text": "Extracted text...",
  "page": 3,
  "timeRange": [12.3, 18.8],
  "framePath": "objects/media_xxx/frame-001.jpg",
  "confidence": 0.91,
  "citation": {"uri": "media://media_xxx#page=3", "markdown": "[^M1-p3]"}
}
```

## API

- `POST /api/media`: register JSON media or upload multipart media.
- `GET /api/media?projectId=...`: list media, optionally by project, type, or status.
- `GET /api/media/{mediaId}`: read one media object.
- `POST /api/media/{mediaId}/process`: extract segments and index them.
- `GET /api/media/{mediaId}/segments`: list extracted segments.
- `DELETE /api/media/{mediaId}`: delete metadata, segments, source files, and Local RAG media index rows.

The JSON path supports webpage snapshots and transcript imports without requiring heavy local ASR/video dependencies. Audio/video built-ins are intentionally MVP in v2.7.2: metadata plus transcript/frame-caption imports.

## Processing

The ingestion pipeline is unified:

```text
Upload / Capture / Import
  -> Detect MIME
  -> Process
  -> Extract text / OCR / transcript / frames
  -> Index to Local RAG
  -> Generate citations
  -> Attach to Project
```

P0 coverage is image/screenshot OCR text or captions, PDF page text, and webpage snapshot text. Audio and video accept transcript imports, and video accepts frame captions.

## Local RAG

Processed media segments are indexed with `collection="media"` and metadata like:

```json
{
  "sourceType": "media",
  "mediaId": "media_xxx",
  "segmentId": "seg_xxx",
  "projectId": "proj_xxx",
  "page": 2,
  "timeRange": [30.0, 45.0],
  "citation": "media://media_xxx#page=2"
}
```

Project ZIP export includes:

- `media/media.json`
- `media/segments/{mediaId}.json`
- `media/source/...`

Secrets in text media sources and segment payloads are redacted during export.

## Media Skills

v2.7.2 adds built-in Skills that accept `mediaIds`:

- `image_explainer`
- `pdf_reader`
- `webpage_summarizer`
- `audio_transcript_summarizer`
- `video_brief_generator`
- `media_to_report`

The Skill runner expands media IDs into segment/citation context before invoking the model or offline runner.

## Evidence

```bash
python scripts/smoke_media.py --offline --out docs/evidence/media-v2.7.2.json
python evals/runners/run_media_eval.py --strict --out evals/reports/media-v2.7.2.json
```
