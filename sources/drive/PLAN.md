# Google Drive Integration Plan

Google Drive as a standalone corpus source — distinct from its role in YouTube slide matching. The PI Drive contains a significant body of material: documents, reports, working papers, meeting notes, research memos, and other content that belongs in the C3PO index independently of any YouTube video.

See `ARCHITECTURE.md` for where Drive fits in the overall C3PO system. For the Drive → YouTube slide matching strategy, see `sources/youtube/PLAN.md`.

---

## What's in Scope

The Drive is heterogeneous. Not all of it belongs in C3PO. The ingestion strategy uses **folder-based scoping** — only explicitly whitelisted folders are indexed, not the entire Drive.

| Content type | Likely formats | Indexable | Notes |
|---|---|---|---|
| Research documents / reports | Google Docs, DOCX, PDF | Yes | Core corpus |
| Working papers / memos | Google Docs, DOCX, PDF | Yes | Core corpus |
| Presentation slides | Google Slides, PPTX, PDF | Yes | Shared with YouTube plan |
| Meeting notes | Google Docs | Yes | Lower weight in retrieval |
| Spreadsheets / data | Google Sheets, XLSX | Partial | Text cells only; structured data needs special handling |
| Case study materials | Google Docs, PDF | Separate | Private namespace; handled in Phase 8 |
| Admin / finance / contracts | Various | No | Excluded by folder scope |
| Email attachments / drafts | Various | No | Excluded |

---

## Authentication and Access

**Service account (recommended for automated pipelines):**
1. Create a service account in Google Cloud Console (same project as YouTube API)
2. Grant it read-only access: share the relevant Drive folders with the service account email
3. Download service account JSON → add path to `.env` and `Code/.env.keys`

**Scopes:**
- `https://www.googleapis.com/auth/drive.readonly`
- `https://www.googleapis.com/auth/documents.readonly`
- `https://www.googleapis.com/auth/spreadsheets.readonly`
- `https://www.googleapis.com/auth/presentations.readonly`

The service account only sees folders explicitly shared with it — a natural permission boundary. Only share whitelisted research folders, not the entire Drive.

---

## Folder Whitelist

Maintain an explicit whitelist in `sources/drive/folder_config.json`. The ingestion script only processes files in whitelisted folders.

```json
{
  "whitelisted_folders": [
    {
      "folder_id": "FOLDER_ID_HERE",
      "label": "PI Research Documents",
      "access_level": "public",
      "recursive": true,
      "notes": "Annual reports, working papers, published research"
    },
    {
      "folder_id": "FOLDER_ID_HERE",
      "label": "PI Meeting Notes",
      "access_level": "member",
      "recursive": false,
      "weight": 0.7,
      "notes": "Board and team meeting notes — member-gated, lower retrieval weight"
    },
    {
      "folder_id": "FOLDER_ID_HERE",
      "label": "Consulting Case Studies",
      "access_level": "private",
      "recursive": true,
      "notes": "Handled separately in Phase 8 — do not index here"
    }
  ]
}
```

`access_level` maps directly to the Pinecone chunk metadata field, controlling who can query this content. `weight` (optional, default 1.0) adjusts retrieval ranking relative to peer-reviewed sources.

---

## Change Detection

Google Drive API provides a **Changes API** (`drive.changes.list`) that returns a delta feed of all file modifications since a given `pageToken`. This is the right mechanism for incremental ingestion — no need to re-scan the whole Drive on each run.

```python
from googleapiclient.discovery import build

def get_changed_files(drive_service, saved_page_token):
    response = drive_service.changes().list(
        pageToken=saved_page_token,
        spaces='drive',
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields='nextPageToken,newStartPageToken,changes(fileId,file(name,mimeType,modifiedTime,parents,trashed))'
    ).execute()

    changed = [c for c in response.get('changes', [])
               if not c['file'].get('trashed')
               and is_in_whitelisted_folder(c['file']['parents'])]

    next_token = response.get('newStartPageToken') or response.get('nextPageToken')
    return changed, next_token
```

Store `next_page_token` in `sources/drive/registry.json` after each run. On the next run, pass it to get only new/modified files. **Deleted or trashed files** appear as `trashed: true` in the changes feed — remove their Pinecone vectors when this occurs.

---

## Extraction by Format

### Google Docs (application/vnd.google-apps.document)

Export as plain text via the Drive API export endpoint — no download of binary format needed:

```python
content = drive_service.files().export(
    fileId=file_id,
    mimeType='text/plain'
).execute()
text = content.decode('utf-8')
```

Alternatively export as DOCX and parse with `python-docx` for more structural fidelity (headings, sections). Plain text is simpler and sufficient for most documents.

### Google Slides (application/vnd.google-apps.presentation)

Use the Slides API for structured per-slide extraction including speaker notes — same approach as the YouTube slide plan:

```python
prs = slides_service.presentations().get(presentationId=file_id).execute()
# Extract text + notes per slide (see sources/youtube/PLAN.md for full code)
```

For Drive-sourced slides that are also matched to YouTube videos, the YouTube ingestion takes precedence — the slide content is ingested once, attached to the video chunk. If a Drive slide file has **no corresponding video**, ingest it here as a standalone document.

### DOCX (application/vnd.openxmlformats-officedocument.wordprocessingml.document)

Download the file, extract with `python-docx`:

```python
import io
from docx import Document

file_content = drive_service.files().get_media(fileId=file_id).execute()
doc = Document(io.BytesIO(file_content))
text = '\n'.join(para.text for para in doc.paragraphs if para.text.strip())
# Preserve heading structure:
sections = [(para.style.name, para.text) for para in doc.paragraphs if para.text.strip()]
```

### PDF (application/pdf)

Download and parse with `pdfplumber` (already in the C3PO stack):

```python
import io, pdfplumber

file_content = drive_service.files().get_media(fileId=file_id).execute()
with pdfplumber.open(io.BytesIO(file_content)) as pdf:
    text = '\n\n'.join(page.extract_text() or '' for page in pdf.pages)
```

For PDFs with poor text extraction (scanned documents, image-heavy PDFs), fall back to Vision LLM or Google Document AI — same approach as the YouTube slide fallback.

### PPTX (application/vnd.openxmlformats-officedocument.presentationml.presentation)

Download and parse with `python-pptx` (same as YouTube plan). Only ingest if not already matched to a YouTube video.

### Google Sheets / XLSX

Text cells only. Skip charts and formatting. Useful for structured lists (e.g., resource inventories, event records) but not for unstructured data:

```python
sheet = sheets_service.spreadsheets().values().get(
    spreadsheetId=file_id,
    range='A1:Z1000'
).execute()
rows = sheet.get('values', [])
# Flatten to text: join header + rows as readable sentences or markdown table
```

Only index sheets where the content is meaningful prose or structured lists. Skip financial spreadsheets, data tables with no context.

---

## Chunking Strategy

Unlike YouTube (chunked by slide/timestamp), Drive documents chunk by semantic structure:

1. **Google Docs / DOCX**: chunk by heading section. If no headings, chunk by paragraph groups (~400 tokens). Preserve the document title and section heading in each chunk's metadata.
2. **PDF documents**: chunk by page, then sub-chunk long pages by paragraph. Preserve page number.
3. **Slides**: chunk by slide (same as YouTube plan). Slides ingested here are standalone (no video).
4. **Sheets**: each row or logical block as one chunk, with column headers as context.

---

## Metadata per Chunk

```python
{
  "source": "drive",
  "drive_file_id": "1BxiMVs0XRA5nFMdKv...",
  "drive_file_name": "PI Annual Report 2025.pdf",
  "drive_folder": "PI Research Documents",
  "mime_type": "application/pdf",
  "access_level": "public",
  "weight": 1.0,
  "modified_date": "2025-11-01",
  "chunk_type": "page" | "section" | "slide" | "row",
  "chunk_index": 3,
  "section_heading": "Findings",  # null if no headings
  "page_number": 4,               # null if not a PDF
}
```

---

## Deletion Handling

When a file is deleted or trashed in Drive (appears in the changes feed), remove all Pinecone vectors with that `drive_file_id` in their metadata:

```python
# Pinecone doesn't support delete-by-metadata directly in serverless
# Store file_id → [vector_ids] in D1 at ingest time, then bulk-delete
def delete_file_vectors(file_id):
    vector_ids = db.query("SELECT vector_id FROM drive_vectors WHERE file_id = ?", [file_id])
    pinecone_index.delete(ids=[row['vector_id'] for row in vector_ids], namespace='drive')
    db.execute("DELETE FROM drive_vectors WHERE file_id = ?", [file_id])
```

This requires a small D1 table (`drive_vectors`) mapping file IDs to vector IDs. Worth maintaining — otherwise deleted documents stay in the index indefinitely.

---

## Ingestion Pipeline

```
Scheduled: daily GitHub Actions cron

1. Load folder_config.json → whitelisted folder IDs + access levels
2. Fetch changes since last_page_token (Drive Changes API)
   - Filter to whitelisted folders only
   - Separate: new/modified files vs trashed files

3. For trashed files:
   - Delete Pinecone vectors via D1 vector ID lookup
   - Remove from D1 drive_vectors table

4. For new/modified files:
   - Skip if MIME type not in supported list
   - Skip if matched to a YouTube video (YouTube ingestion handles it)
   - Extract text by format (Docs, DOCX, PDF, Slides, Sheets)
   - Chunk by document structure
   - Build metadata dict (access_level from folder_config)

5. Embed + upsert
   - Voyage AI voyage-3: embed each chunk
   - Pinecone namespace "drive": upsert with metadata
   - Write (file_id, vector_id) pairs to D1 drive_vectors table

6. Update sources/drive/registry.json:
   - last_page_token, last_ingested, vector_count
```

---

## registry.json

```json
{
  "source": "drive",
  "display_name": "Protocol Institute Google Drive",
  "pinecone_namespace": "drive",
  "vector_count": 0,
  "last_ingested": null,
  "last_page_token": null,
  "schema_version": 1,
  "access_level": "mixed",
  "freshness_cadence": "daily",
  "ingest_script": "ingest/ingest_drive.py",
  "folder_config": "sources/drive/folder_config.json",
  "notes": "Folder-scoped; access_level per folder in folder_config.json. Excludes files already matched to YouTube videos. Deletion tracked via D1 drive_vectors table."
}
```
