# UI Blueprint Backend

FastAPI service that receives Android screen-recording uploads, runs the
`ui_blueprint` extractor + preview generator in a background thread, and
exposes the results over HTTP.

---

## Endpoints

### Public (no auth required)

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Service health check |
| `GET`  | `/api/domains/{domain_profile_id}` | Fetch a domain profile by ID |

### Auth-required (when `API_KEY` is set)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/sessions` | Upload a clip (`video` MP4 + optional `meta` JSON) |
| `GET`  | `/v1/sessions/{id}` | Poll extraction status |
| `GET`  | `/v1/sessions/{id}/blueprint` | Download blueprint JSON |
| `GET`  | `/v1/sessions/{id}/preview/index` | List preview PNG filenames |
| `GET`  | `/v1/sessions/{id}/preview/{file}` | Download a preview PNG |
| `POST` | `/api/domains/derive` | Derive draft domain profile candidates |
| `PATCH` | `/api/domains/{id}` | Edit a draft profile (409 if not draft) |
| `POST` | `/api/domains/{id}/confirm` | Confirm a draft profile (409 if not draft) |
| `POST` | `/api/blueprints/compile` | Compile blueprint (requires confirmed domain) |
| `GET` | `/api/chat` | List persisted global chat history |
| `POST` | `/api/chat` | Send a message to the UI Blueprint AI assistant |
| `POST` | `/v1/folders` | Create a folder (title optional) |
| `GET`  | `/v1/folders` | List all folders (ordered by created_at desc) |
| `GET`  | `/v1/folders/{id}` | Get folder + latest job status + artifact list |
| `DELETE` | `/v1/folders/{id}` | Delete folder (cascade) |
| `POST` | `/v1/folders/{id}/clip` | Upload clip to folder (creates analyze job) |
| `GET`  | `/v1/folders/{id}/artifacts/{artifact_id}` | Presigned download URL for artifact |
| `POST` | `/v1/folders/{id}/messages` | Send a chat message (AI replies, persisted) |
| `GET`  | `/v1/folders/{id}/messages` | List folder chat history |
| `POST` | `/v1/folders/{id}/jobs` | Enqueue a job (`analyze` or `blueprint`) |
| `GET`  | `/v1/folders/{id}/jobs` | List jobs for the folder |
| `GET`  | `/v1/folders/{id}/jobs/{job_id}` | Get job status |

Auth-required endpoints need `Authorization: Bearer <API_KEY>` header.
When `API_KEY` is not set, all requests are allowed (dev / open mode).

---

## Local development

```bash
# From repo root
pip install ".[video]"
pip install -r backend/requirements.txt

API_KEY=dev-secret uvicorn backend.app.main:app --reload
```

---

## Docker Compose (local smoke test)

```bash
# From repo root
API_KEY=my-secret docker compose up --build
```

The backend container entrypoint runs `alembic -c backend/alembic.ini upgrade head`
before starting Uvicorn. On platforms like Render, the image can be started as-is,
or you can explicitly run `bash backend/entrypoint.sh`.

Upload a clip:

```bash
curl -X POST http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer my-secret" \
  -F "video=@/path/to/recording.mp4" \
  -F 'meta={"device":"Pixel 8","fps":30}'
# → {"session_id":"<uuid>","status":"queued"}

# Poll status
curl http://localhost:8000/v1/sessions/<uuid> \
  -H "Authorization: Bearer my-secret"
```

Derive domain profiles:

```bash
curl -X POST http://localhost:8000/api/domains/derive \
  -H "Authorization: Bearer my-secret" \
  -H "Content-Type: application/json" \
  -d '{
    "media": {"media_id": "vid_001", "media_type": "video"},
    "options": {"hint": "cabinet assembly", "max_candidates": 2}
  }'
# → {"schema_version":"v1.1.0","candidates":[...],"warnings":[]}
```

Chat with the AI assistant:

```bash
curl http://localhost:8000/api/chat \
  -H "Authorization: Bearer my-secret"
# → {"schema_version":"v1.1.0","messages":[...],"tools_available":[...]}

curl -X POST http://localhost:8000/api/chat \
  -H "Authorization: Bearer my-secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "How do I compile a blueprint?"}'
# → {"schema_version":"v1.1.0","reply":"...","tools_available":[...],"user_message":{...},"assistant_message":{...}}
```

---

## Render deployment

When deploying to Render as a Docker service:

- **Pre-Deploy Command**: leave blank (Render cannot reliably parse complex shell syntax in this field).
- **Docker Command**: `bash backend/entrypoint.sh`

The entrypoint script runs Alembic migrations then starts Uvicorn with `exec`.
`$PORT` is set automatically by Render; the script defaults to `8000` when running locally.

---

## Oracle Free Tier deployment

These steps assume a fresh **Oracle Linux 8** (or Ubuntu 22.04) VM with 1 OCPU / 1 GB RAM from the Oracle Always-Free tier.

### 1 — Provision the VM

1. Log in to <https://cloud.oracle.com> → Compute → Instances → **Create Instance**.
2. Choose **VM.Standard.A1.Flex** (Ampere, Always-Free) or **VM.Standard.E2.1.Micro**.
3. Select Oracle Linux 8 or Canonical Ubuntu 22.04 image.
4. Add your SSH public key and note the public IP.

### 2 — Install Docker

**Ubuntu:**
```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```

**Oracle Linux:**
```bash
sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
sudo dnf install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER
```

### 3 — Open firewall port 8000

In the OCI Console: **Networking → Virtual Cloud Networks → your VCN → Security Lists → Ingress Rules → Add Ingress Rule**:
- Source CIDR: `0.0.0.0/0`
- Destination port: `8000`
- Protocol: TCP

Also open in the OS firewall:
```bash
# Oracle Linux
sudo firewall-cmd --permanent --add-port=8000/tcp && sudo firewall-cmd --reload

# Ubuntu (if ufw is active)
sudo ufw allow 8000/tcp
```

### 4 — Deploy

```bash
# Clone the repo
git clone https://github.com/Rogmar0071/ui-blueprint.git
cd ui-blueprint

# Set a strong API key
export API_KEY=$(openssl rand -hex 32)
echo "API_KEY=$API_KEY" > .env   # keep this secret

# Build and start
docker compose --env-file .env up -d --build
```

### 5 — Verify

```bash
curl http://<YOUR_VM_IP>:8000/docs
```

The FastAPI Swagger UI should load.  Use `API_KEY` from your `.env` as the bearer token.

### 6 — Persistent data

The `ui_blueprint_data` Docker volume stores all sessions under `/data`.  
Back it up with:

```bash
docker run --rm -v ui_blueprint_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/ui_blueprint_data.tar.gz /data
```

### 7 — (Optional) Reverse proxy with Nginx

To serve over HTTPS, install Nginx + Certbot, configure a proxy_pass to `localhost:8000`, and expose port 443.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `API_KEY` | *(empty — no auth)* | Bearer token required by auth-required endpoints |
| `DATA_DIR` | `./data` | Root directory for session files |
| `BACKEND_DISABLE_JOBS` | `0` | Set to `1` to skip background jobs (tests) |
| `DATABASE_URL` | *(empty — folder API disabled)* | SQLAlchemy URL for Postgres (or SQLite). Required for `/v1/folders` endpoints. |
| `REDIS_URL` | *(empty — sync fallback)* | Redis / Valkey connection URL for RQ background jobs. When absent, jobs run synchronously. |
| `R2_ENDPOINT` | *(empty — no object storage)* | Cloudflare R2 endpoint URL |
| `R2_BUCKET` | *(empty)* | R2 bucket name |
| `R2_ACCESS_KEY_ID` | *(empty)* | R2 access key ID |
| `R2_SECRET_ACCESS_KEY` | *(empty)* | R2 secret access key |
| `OPENAI_API_KEY` | *(empty — folder chat returns 503)* | Server-side OpenAI credential — required for `/v1/folders/{id}/messages` (returns HTTP 503 when absent). Also enables AI-backed domain derivation and `/api/chat`. **Never returned to clients.** |
| `OPENAI_MODEL_DOMAIN` | `gpt-4.1-mini` | Model used for domain derivation |
| `OPENAI_MODEL_CHAT` | `gpt-4.1-mini` | Model used for `/api/chat` and folder chat |
| `OPENAI_BASE_URL` | `https://api.openai.com` | OpenAI base URL (strip trailing `/v1` if present — added automatically) |
| `OPENAI_TIMEOUT_SECONDS` | `30` | Request timeout in seconds for OpenAI calls |

> **Port**: Render sets `$PORT` automatically; the server binds `${PORT:-8000}` (defaults to `8000` for local dev).
