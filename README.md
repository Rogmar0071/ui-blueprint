# RecoB

## Evidence AI prototype

This repository now also includes an Evidence AI prototype under the top-level
`app/`, `interface/`, `prompts/`, `data/`, and `run.py` files, alongside the
existing blueprint tooling below.

The package/distribution name is `RecoB`, which intentionally matches the
repository identity while covering both the blueprint and evidence-query tracks.

### Purpose

Turn a project folder into a non-hallucinating, evidence-only query system:

- ingestion captures file path, line numbers, and chunk hashes
- retrieval returns raw chunks only
- execution falls back to `UNKNOWN` when evidence is missing
- validation rejects unsupported claims and returns `INSUFFICIENT EVIDENCE`

### Quick start

1. Drop project files into `data/raw/`
2. Run:

   ```bash
   python run.py
   ```

3. Ask project-bounded questions such as:

   ```text
   What does EventEngine do?
   ```

The response is always structured as:

- `verified_facts`
- `unknowns`
- `invalid_scope`
- `required_inputs`

> Convert 10-second Android screen-recording clips into a structured "blueprint" suitable for near-human-indistinguishable replay in a custom renderer — and optionally for compiling into automation events.

---

## What is a Blueprint?

A **Blueprint** is a compact, machine-readable JSON document that captures everything a custom renderer needs to reproduce a UI interaction at ~99% human-perceived fidelity:

| Section | Contents |
|---|---|
| `meta` | Device, resolution, FPS, clip duration |
| `assets` | Extracted icon/image crops (by perceptual hash) |
| `elements_catalog` | Stable element definitions with inferred type, style, and content |
| `chunks` | Time-ordered 1-second segments, each with a keyframe scene, per-element tracks, and inferred events |

### How chunking works

The clip is divided into **chunks** (default 1 000 ms each).  
Every chunk contains:

1. **`key_scene`** — a full scene-graph snapshot (all elements with bbox, z-order, opacity) at the chunk start time `t0_ms`. A renderer can seek to any time *t* by jumping to the nearest chunk keyframe.
2. **`tracks`** — parametric curves for each element property (`translate_x`, `translate_y`, `opacity`, …). The simplest model that fits the data is chosen: `step → linear → bezier → spring → sampled`. This preserves native easing / scroll inertia.
3. **`events`** — inferred interactions (`tap`, `swipe`, `scroll`, `type`, …) aligned to absolute timestamps.

Chunking gives **O(1) seek**, compact **delta compression** within each segment, and easy **parallel processing** during generation.

---

## Project structure

```
ui-blueprint/
├── schema/
│   └── blueprint.schema.json   # JSON Schema v1
├── ui_blueprint/
│   ├── __init__.py
│   ├── __main__.py             # CLI entry point
│   ├── extractor.py            # Video → Blueprint pipeline
│   └── preview.py              # Blueprint → PNG preview frames
├── tests/
│   └── test_extractor.py       # Unit + CLI integration tests
├── .github/workflows/ci.yml    # GitHub Actions CI
└── pyproject.toml
```

---

## Quick start

### Install

```bash
pip install ".[dev]"    # test/lint deps, includes imageio[ffmpeg] for video decoding
pip install ".[video]"  # runtime optional video decoder path
```

### Extract a Blueprint from a video

```bash
python -m ui_blueprint extract recording.mp4 -o blueprint.json
```

Options:

| Flag | Default | Description |
|---|---|---|
| `--chunk-ms` | 1000 | Chunk duration (ms) |
| `--sample-fps` | 10 | Frame sampling rate for analysis |
| `--assets-dir DIR` | — | Create an asset-crops directory and record paths |
| `--synthetic` | — | Generate from synthetic metadata (no real video) |

### Render a visual preview

```bash
python -m ui_blueprint preview blueprint.json --out preview_frames/
```

Outputs one PNG per chunk — draws bounding boxes and element labels onto a blank canvas — so you can quickly validate the timeline structure.

### Current extractor behavior

The extractor now runs a real baseline pipeline:

1. **Frame decode** — samples frames with `imageio[ffmpeg]` when installed; otherwise falls back to MP4 metadata parsing.
2. **Baseline detection** — uses deterministic heuristics over background difference, edge masks, and dark-text proposals to find UI regions.
3. **Tracking** — matches detections frame-to-frame with IoU + simple appearance similarity.
4. **Motion fitting** — fits `step`, `linear`, `bezier`, or `sampled` tracks and stores `residual_error`.
5. **Event inference** — currently emits heuristic `scroll` and tap-like events.

### Test without a real video (CI / unit tests)

```bash
python -m ui_blueprint extract --synthetic -o /tmp/test.json
```

---

## Running tests

```bash
pytest tests/ -v
```

CI runs automatically on every push and pull request via GitHub Actions (`.github/workflows/ci.yml`).

---

## Constraints and next steps

### Current state (baseline video extractor)

The extractor now produces **schema-conformant blueprints** from synthetic frames and real MP4 frame samples. The current implementation is intentionally lightweight and deterministic:

| Hook | File | Description |
|---|---|---|
| `_detect_elements()` | `extractor.py` | Background/edge/text-region heuristics; ready to replace with a learned detector |
| `_ocr_region()` | `extractor.py` | Still a stub; add Tesseract/EasyOCR behind a feature flag next |
| `_track_elements()` | `extractor.py` | IoU + mean-color / edge-density appearance matching |
| `_fit_track_curve()` | `extractor.py` | Fits `step`, `linear`, `bezier`, else falls back to `sampled` |
| `_infer_events()` | `extractor.py` | Heuristic scroll and tap-like inference from tracked motion/appearance |

### Adding real detectors

1. Add real OCR content to detections.
2. Improve detection quality with learned UI region proposals.
3. Add list-row stabilization and re-identification for scrolling content.
4. Add spring fitting for Android-native motion.
5. Expand event inference beyond scroll/tap to drag/swipe/type.

### Adding full video decode (no OpenCV required)

```bash
pip install imageio[ffmpeg]
```

The optional `video` extra already installs `imageio[ffmpeg]`, and the extractor will use it automatically when present.

### Automation script compilation

The `events` array in each chunk is the foundation.  
Compile to UIAutomator / Accessibility actions by mapping:
- `tap { x, y }` → `adb shell input tap x y`
- `swipe { path }` → `adb shell input swipe …`
- `type { text }` → `adb shell input text "…"`

### Element tracking improvements

- Use a **list-item template** to avoid ID churn in scroll lists.
- Add an **appearance embedding** model for robust re-identification across transitions.

---

## Schema reference

See [`schema/blueprint.schema.json`](schema/blueprint.schema.json) for the full annotated JSON Schema (draft-07).

---

## AI-Derived Domain Profiles + Blueprint Compiler

`ui_blueprint` includes a **compiler pipeline** that turns video-derived vision
primitives into a structured **Blueprint Artifact** (Blueprint IR). Domains are
never hard-coded; they are *derived by AI* from captured media and must be
confirmed by a user before the compiler will run.

### Key concepts

#### Domain Profile
An AI-derived description of a real-world artifact class. It carries:

| Field | Description |
|---|---|
| `id` | Stable UUID for this profile version |
| `name` | Human-readable name (AI-suggested, editable while draft) |
| `status` | Lifecycle state: `draft` → `confirmed` → `archived` |
| `derived_from` | Provenance: which media + which AI provider produced it |
| `capture_protocol` | Ordered steps the AI recommends for thorough media capture |
| `validators` | Rules used to assess completeness/quality |
| `exporters` | Output targets (WMS import, assembly plan, CAD export, …) |

**Invariant**: Only `confirmed` profiles may be used for compilation.
Once confirmed, a profile is immutable — editing requires creating a new draft.

#### Blueprint Artifact (BlueprintIR)
The compiled output. It is usable by humans, systems, and agents to reconstruct
a real-world artifact. Key fields:

| Field | Description |
|---|---|
| `id` | UUID for this artifact |
| `domain_profile_id` | UUID of the confirmed DomainProfile used |
| `schema_version` | Object schema version (`v1.1.0`) under steering contract v1.1.1 |
| `source` | Media provenance (media_id, optional time range) |
| `entities[]` | Detected parts/features with type, attributes, confidence |
| `relations[]` | Directed edges between entities (e.g. `stacked_on`) |
| `constraints[]` | Structural constraints (e.g. `grid_alignment`) |
| `completeness` | Score 0–1 + list of missing information |
| `provenance[]` | Evidence records (which extractor, which frames, …) |

### Workflow: derive → edit → confirm → compile

```
POST /api/domains/derive          # AI derives draft profile candidates
GET  /api/domains/{id}            # inspect a draft
PATCH /api/domains/{id}           # edit name/steps/validators while still draft
POST /api/domains/{id}/confirm    # lock the profile (non-idempotent)
POST /api/blueprints/compile      # compile BlueprintIR (requires confirmed domain)
```

All endpoints are under `/api` and return `application/json`.
Error responses use the shape `{"error": {"code": "...", "message": "..."}}`.

### Enforced rule: domain must be confirmed

Calling `POST /api/blueprints/compile` without a confirmed domain returns:

```json
{"error": {"code": "domain_not_confirmed", "message": "..."}}
```
HTTP 400. The compiler also raises `BlueprintCompileError` (a `ValueError`) at
the Python level.

### Running the demo

```bash
# Start the backend
pip install -r backend/requirements.txt
API_KEY=secret uvicorn backend.app.main:app --reload

# Derive candidates (Authorization header required when API_KEY is set)
curl -s -X POST http://localhost:8000/api/domains/derive \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{"media":{"media_id":"demo-001","media_type":"video"},"options":{"hint":"warehouse pallet barcodes","max_candidates":3}}' \
  | python3 -m json.tool

# Confirm the first candidate (replace <id> with a domain_profile_id from above)
curl -s -X POST http://localhost:8000/api/domains/<id>/confirm \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{"confirmed_by":"demo-user","note":"looks good"}' \
  | python3 -m json.tool

# Compile the blueprint
curl -s -X POST http://localhost:8000/api/blueprints/compile \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{"media":{"media_id":"demo-001","media_type":"video"},"domain_profile_id":"<id>"}' \
  | python3 -m json.tool
```

> **Note:** `GET /api/domains/{id}` is intentionally public (no auth required) so
> clients can inspect profiles without a bearer token.

### Extending with a real AI provider

Replace `StubDomainDerivationProvider` in `ui_blueprint/domain/derivation.py`:

```python
class MyLLMProvider(DomainDerivationProvider):
    def derive(self, media_input: dict, max_candidates: int = 3) -> list[DomainProfile]:
        # Call your vision/LLM API here; return draft DomainProfile objects.
        ...
```

Then wire it into `backend/app/domain_routes.py` via `_provider = MyLLMProvider()`.

---

## OpenAI configuration

Setting `OPENAI_API_KEY` on the server enables two AI features:

1. **AI domain derivation** — `POST /api/domains/derive` uses GPT instead of the keyword stub.
2. **AI chat** — `POST /api/chat` responds via GPT instead of returning a stub message.

### Two separate secrets — do not confuse them

| Variable | Purpose | Sent to clients? |
|---|---|---|
| `API_KEY` | Service bearer token — protects all mutating endpoints | **No** — stays on server |
| `OPENAI_API_KEY` | Server-side OpenAI credential — used for AI calls | **Never** — stays on server |

Clients only ever need `API_KEY` (passed as `Authorization: Bearer <API_KEY>`).
`OPENAI_API_KEY` is read on the server and never appears in any response or log.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | *(unset — stub mode)* | OpenAI API key |
| `OPENAI_MODEL_DOMAIN` | `gpt-4.1-mini` | Model used by `/api/domains/derive` |
| `OPENAI_MODEL_CHAT` | `gpt-4.1-mini` | Model used by `/api/chat` |
| `OPENAI_BASE_URL` | `https://api.openai.com` | Base URL (supports custom proxies) |
| `OPENAI_TIMEOUT_SECONDS` | `30` | Per-request timeout |

### Render deployment

In the Render **Environment** tab for your web service add:

```
API_KEY          = <generate with: openssl rand -hex 32>
OPENAI_API_KEY   = sk-...
```

Leave `OPENAI_MODEL_DOMAIN`, `OPENAI_MODEL_CHAT`, and `OPENAI_BASE_URL` unset to
use the defaults.

### /api/chat usage

```bash
# Stub reply (OPENAI_API_KEY not configured)
curl -s -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "How do I derive a domain profile?"}' \
  | python3 -m json.tool

# Response shape:
# {
#   "schema_version": "v1.1.0",
#   "reply": "[Stub] You said: ...",
#   "tools_available": ["domains.derive", "domains.confirm", ...]
# }
```

`tools_available` lists the pipeline actions the assistant can describe (no automatic
tool execution yet — information only).

---

## Android app

The Android app (`android/`) records a 10-second screen clip using MediaProjection and saves it directly to the device Gallery — no backend required.

### How recordings are saved

After each recording the clip is inserted into the device Gallery via `MediaStore.Video.Media`:

| Android version | Storage mechanism |
|---|---|
| API 29+ (Android 10+) | Scoped storage: `RELATIVE_PATH = Movies/UIBlueprint`, `IS_PENDING` flag for atomic write |
| API 26–28 (Android 8–9) | MediaStore insert with bytes written through the returned `Uri` |

Clips appear in your Gallery / Files app under **Movies → UIBlueprint** and are named `clip_yyyyMMdd_HHmmss.mp4`.

### Backend upload is disabled by default

`UploadWorker` is present in the source but is **not invoked** in the default app flow.  
Every recording is saved locally and the session list shows `[saved]` on success or `[failed]` on error.

To re-enable backend upload for development:
1. Add `BACKEND_BASE_URL` and `BACKEND_API_KEY` to `android/local.properties`.
2. Replace the `onCaptureDone` call in `MainActivity.kt` with `UploadWorker.enqueue(...)`.

### CI-built APKs (GitHub Actions)

APKs produced by GitHub Actions do **not** include `android/local.properties` (it is gitignored and not generated in CI). Gradle therefore uses its built-in fallback default:

```
BACKEND_BASE_URL = https://ui-blueprint-backend.onrender.com
```

To override this for a local build, add your own `android/local.properties` as described in the section above.

### Build and run on a device

```bash
cd android
./gradlew assembleDebug          # builds debug APK
./gradlew installDebug           # installs to a connected device / emulator
```

Run unit tests (no device needed):

```bash
./gradlew :app:testDebugUnitTest
```

---

## License

MIT

---

## Upload API guidelines

### Accepted file types

| MIME type | Extension | Notes |
|---|---|---|
| `video/mp4` | `.mp4` | Android screen recordings |
| `application/zip` | `.zip` | Repository archives for structural analysis |

### Size limit

Uploads are rejected with **HTTP 413** once they exceed `MAX_UPLOAD_BYTES`
(default **50 MB**).  Override with the environment variable:

```bash
export MAX_UPLOAD_BYTES=104857600   # 100 MB
```

### Single-shot upload

```
POST /v1/sessions
Content-Type: multipart/form-data

Fields:
  video   — file (video/mp4 or application/zip)
  meta    — optional JSON string
```

Returns `{"session_id": "<uuid>", "status": "queued"}`.

### Chunked upload flow

Use chunked upload for files larger than ~5 MB.

**1. Send each chunk:**

```
POST /v1/sessions/chunks
Headers:
  X-Upload-Id:    <stable UUID for this upload>
  X-Chunk-Index:  0-based integer
  X-Total-Chunks: total number of chunks

Body: multipart/form-data  field "chunk"
```

**2. Finalize (assemble all chunks):**

```
PUT /v1/sessions/chunks/{upload_id}/finalize
Body: multipart/form-data  field "meta" (optional JSON string)
```

Returns same shape as single-shot upload on success.

---

## Job queue architecture

```
Android app
  │
  │  POST /v1/sessions  (or chunked upload)
  ▼
FastAPI (main.py)
  │ streams file to /tmp/uploads/<uuid>.ext
  │ creates session directory
  │
  ├──(REDIS_URL set)──► Redis RQ queue ──► worker.py / analysis_job_processor.py
  │
  └──(no Redis)────────► background Thread
```

Session-based jobs call `analysis_job_processor.process_analysis_job()` which:

1. Extracts zip (if applicable) — streaming, with zip-bomb and corrupt-archive guards.
2. Parses Android XML/layout files.
3. Checks Python/Kotlin code syntax.
4. Validates image assets.

Results and per-stage errors are persisted to the `analysis_jobs` table after each stage.

---

## `/v1/analysis/*` API endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/analysis` | Create and enqueue an analysis job for an uploaded file |
| `GET`  | `/v1/analysis/{job_id}` | Get current status and partial results |
| `GET`  | `/v1/analysis/{job_id}/results` | Get full results JSON (terminal jobs only) |
| `DELETE` | `/v1/analysis/{job_id}` | Cancel / delete an analysis job |

All endpoints require `Authorization: Bearer <API_KEY>`.

### Job status values

| Status | Meaning |
|--------|---------|
| `queued` | Job is waiting to be processed |
| `running` | Job is actively being processed |
| `succeeded` | All stages completed without fatal errors |
| `failed` | At least one fatal error; `errors_json` has details |

---

## Error codes reference

| Code | HTTP | Description |
|------|------|-------------|
| `corrupt_archive` | 200 (in job errors) | Zip file is malformed or truncated |
| `unsupported_compression` | 200 (in job errors) | Zip requires a password or unsupported method |
| `zip_bomb` | 200 (in job errors) | Uncompressed size exceeds `MAX_UNCOMPRESSED_BYTES` |
| `413 Request Entity Too Large` | 413 | Upload body exceeds `MAX_UPLOAD_BYTES` |
| `415 Unsupported Media Type` | 415 | MIME type not in allowed set |
| `409 Conflict` | 409 | Chunked finalize called before all chunks arrived |
| `internal_error` | 500 | Unexpected server error (see server logs) |
