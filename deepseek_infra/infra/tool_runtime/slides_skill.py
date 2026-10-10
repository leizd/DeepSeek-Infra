"""Slides skill guidance for local presentation generation.

The desktop app cannot execute Codex skills directly inside DeepSeek, so this
module keeps the user-provided slides skill as promptable runtime guidance and
routes the actual export through the local ``create_pptx`` tool.
"""

from __future__ import annotations

SLIDES_SKILL_NAME = "slides"
SLIDES_SKILL_DESCRIPTION = (
    "Build, edit and export polished, editable PowerPoint-style presentations "
    "through the create_pptx tool. Use when creating, editing, or polishing "
    "slide decks, presentations, or visual summaries."
)

SLIDES_SKILL_REFERENCE = """---
name: slides
description: Build polished, editable PowerPoint decks via the create_pptx tool. Use when creating, editing, or polishing presentations, slide decks, or visual summaries.
---

# Slides Skill

Reference for serious, high-polish decks. "Clean" is not the bar — the target is
an editable deck that reads like a strong editor, analyst, and designer built it
together. Reject "serviceable": if a deck looks like a generic SaaS dashboard, a
consulting card grid, or a template with the subject name swapped in, keep
sharpening it.

## North star: win the contact-sheet test

Picture the whole deck shrunk to thumbnails on one contact sheet. It should show a
coherent visual system, varied slide rhythms, and evidence-led storytelling. At
readable size, every slide must carry a claim, one proof object, and no filler.

## Every slide is a claim

Before choosing a layout, write each slide as a claim:
- claim title — a conclusion, not a topic label. It must fail the noun-swap test:
  if another subject could be dropped in and the title still works, sharpen it.
  - Weak: "Revenue and margin trends" -> Strong: "Growth slowed, but the margin engine kept expanding."
  - Weak: "Expansion drivers" -> Strong: "Backlog is compounding faster than revenue."
- one dominant proof object — the single most convincing structure for that claim
  (a comparison, a process, a ranked set of cards, a thesis line). One per slide.
- a short support note — concise, factual, specific. Numbers beat adjectives.

## The renderer owns the visual system

create_pptx already applies a coherent system: one deterministic accent theme, an
accent eyebrow plus a near-black claim title on every slide, open hairline
composition (no boxed cards), and an auto cover / agenda / page numbers. Do not ask
for fonts, colors, charts, images, or logos — the tool renders none of those. Your
job is the content and the structure, so spend the effort there. Convey brand
through the wording and the claim itself, never a fabricated logo or mascot.

## Contact-sheet rhythm

Vary the layout so the deck looks authored, not generated:
- across ~10 slides use several different layouts, not the same one repeated
- never let 3 consecutive slides share the same layout
- match the layout to each slide's job; don't default everything to plain bullets

## Blocking anti-patterns (fix before delivering)

- title states a topic instead of a conclusion
- a proof too thin for the claim, or one slide trying to make several points at once
- bullets exist only to fill space; equal-role items are uneven or padded out
- every content slide uses the same layout, so the contact sheet reads as a template pack
- a bullet has no "lead: detail" split, so it renders as one flat line with no hierarchy

## Pre-flight quality bar

Self-score the outline on story arc, specificity (noun-swap), rhythm, restraint,
precision, and coherence. If any is weak, rebuild the weakest slides — sharpen
titles, rebalance layouts, cut filler — before generating. Do not ship just
because a file would export."""

SLIDES_RUNTIME_GUIDANCE = """DeepSeek Infra runtime routing:
- This app builds decks through ONE boundary: the `create_pptx` function tool. It renders a real, editable, downloadable 16:9 `.pptx` with python-pptx. There is no artifact-tool, imagegen, headless renderer, or shell/script step here — never claim those, and never answer a PPT request with only an outline, Marp, or Markdown slides.
- Always call `create_pptx` for any request to create / edit / export / polish a PPT / slides / deck / presentation. Pass a `title`, an optional `subtitle`, and an ordered `slides` array of {title, bullets[], layout}.
- Apply the slides-skill quality bar through the fields you control:
  - title — write it as a claim (a conclusion), not a topic; run the noun-swap test.
  - bullets — 3-6 tight, specific items; numbers over adjectives; no filler lines. Write each as "lead: detail" (split on a colon `：`/`:` or a dash `-`/`—`): in every layout the lead renders bold and the detail muted, so each point shows a point AND its proof. A bullet with no split renders as one flat line — always give it a proof.
  - layout — pick the ONE structure that best proves the claim: `cards` for a set of key points, `process` or `timeline` for ordered steps, `comparison` for tradeoffs or A-vs-B, `quote` for a single thesis or section moment, `summary` for closing takeaways, `bullets` only when no structure fits. Use `auto` to let the tool infer from the title.
- Compose for contact-sheet rhythm: vary layouts across the deck and avoid 3 same-layout slides in a row. The tool auto-adds a themed cover, an agenda (for 4+ slides), an accent eyebrow plus a bold claim title on each slide, page numbers, and a deterministic color theme — you do not set those, so spend your effort on claim titles, evidence, and layout variety.
- Default to a 6-10 slide deck when length is unspecified. If the user supplies source material, convert it into editable slide content, not a prose summary.
- After the tool returns, surface the result: state the title and slide count, walk the returned `outline` page by page (title + key points), and hand over the download as a Markdown link such as [下载 PPT](downloadUrl) (valid ~6 hours)."""


def format_slides_skill_context() -> str:
    return f"[Skill: {SLIDES_SKILL_NAME}]\n{SLIDES_SKILL_REFERENCE}\n\n{SLIDES_RUNTIME_GUIDANCE}"
