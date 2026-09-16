# Stremio Art Proxy

A self-hosted artwork resolver/proxy for Stremio and Stremio-like clients.

It accepts URLs such as:

```text
https://YOUR-DOMAIN/poster/tmdb:movie:550&imdb:tt0137523&tvdb:.jpg
https://YOUR-DOMAIN/backdrop/tmdb:series:1399&imdb:tt0944947&tvdb:121361.jpg
https://YOUR-DOMAIN/logo/tmdb:movie:550&imdb:tt0137523&tvdb:.jpg
```

The resolver follows a configurable provider chain. The default intent is:

- Posters: ThePosterDB English -> TMDB English -> TVDB English -> TMDB original language -> TVDB original language -> MetaHub -> TMDB primary -> TVDB primary
- Backdrops: TMDB textless -> TVDB textless -> MetaHub -> TMDB primary -> TVDB primary
- Logos: TMDB English -> TVDB English -> TMDB original language -> TVDB original language -> MetaHub -> TMDB primary -> TVDB primary

The first successful image is cached locally and served by the proxy. ThePosterDB assets are kept permanently by default.

## Important provider note

TMDB and TVDB expose documented APIs. ThePosterDB does not expose a comparable documented public item-art lookup API, so this project isolates ThePosterDB discovery in `app/providers/theposterdb.py`. It currently uses the public web search page and parses the same kind of `data-poster-id` asset identifier used by existing TPDb scraping projects. If TPDb changes its HTML, only that provider should need maintenance.

## Features

- FastAPI HTTP service
- Docker Compose deployment
- SQLite persistence
- Disk cache for actual image bytes
- Per-provider timeouts and retries
- Single-flight request deduplication: concurrent requests for the same artwork share one resolution task
- Background ThePosterDB refresh when it is slow/fails during a foreground request
- Negative/failure cooldowns to avoid hammering providers
- HTTP `ETag`, `Last-Modified`, `Cache-Control`, and `Vary` headers
- Admin dashboard to search requested items, inspect the selected art, browse provider candidates, and override the selected image
- Manual overrides survive cache refreshes
- Graceful shutdown waits for in-flight/background work
- Environment-only configuration
- GitHub Actions build/test/publish to GHCR

## Quick start

1. Copy `.env.example` to `.env`.
2. Put your TMDB bearer token and TVDB API key in `.env`.
3. Set `PUBLIC_BASE_URL` to the URL clients will use.
4. Start it:

```bash
docker compose up -d
```

5. Test:

```text
https://YOUR-DOMAIN/health
```

6. Give AIOMetadata a poster URL such as:

```text
https://YOUR-DOMAIN/poster/tmdb:{type}:{tmdb_id?}&imdb:{imdb_id?}&tvdb:{tvdb_id?}.jpg
```

The project is intentionally designed so the URL syntax can be pasted directly into AIOMetadata.

For a single Docker process, the in-memory single-flight map prevents duplicate work for identical simultaneous requests. This is intentionally a one-process design; running multiple app workers would require a shared coordination store such as Redis to preserve that behavior across workers.

## Configuration

See `.env.example`. The most useful controls are:

- `TMDB_BEARER_TOKEN`
- `TVDB_API_KEY`
- `TVDB_USER_PIN` (optional)
- `TMDB_POSTER_SIZE` (default `w500`)
- `TMDB_BACKDROP_SIZE` (default `original`)
- `TMDB_LOGO_SIZE` (default `original`)
- `HTTP_TIMEOUT_SECONDS`
- `TPDB_TIMEOUT_SECONDS`
- `TPDB_BACKGROUND_REFRESH`
- `CACHE_TTL_SECONDS`
- `TPDB_CACHE_TTL_SECONDS` (default `0`, meaning forever)
- `FAILURE_COOLDOWN_SECONDS`
- `MAX_CONCURRENT_PROVIDER_REQUESTS`
- `MAX_IMAGE_BYTES`
- `ADMIN_ENABLED`
- `ADMIN_TOKEN`

## GitHub + GHCR

A complete beginner-friendly setup walkthrough is included below.

### 1. Install GitHub Desktop

GitHub Desktop is absolutely fine for this project. You do not need to learn command-line Git first. GitHub Desktop gives you a visual way to create commits, push changes, and publish the repository.

### 2. Create the repository

On GitHub in your browser:

1. Click **New repository**.
2. Name it something like `stremio-art-proxy`.
3. Choose **Public** or **Private**.
4. Do not add a README or `.gitignore` because this project already contains them.
5. Create the repository.

### 3. Add the project to GitHub Desktop

1. Open GitHub Desktop.
2. **File -> Add local repository**.
3. Choose the folder containing this project.
4. Click **Publish repository**.
5. Select the GitHub repository you just created.

### 4. Push the first commit

The left side of GitHub Desktop will show the project files.

1. Enter a summary such as `Initial version`.
2. Click **Commit to main**.
3. Click **Push origin**.

### 5. Enable GHCR publishing

The workflow `.github/workflows/publish-image.yml` publishes the image to GitHub Container Registry on pushes to `main`, and tags releases such as `v1.2.3`.

GitHub Actions supplies `GITHUB_TOKEN`; the workflow grants `packages: write`, so you do not need to create a separate Docker Hub token for GHCR.

After the first successful run, the package will normally appear under your GitHub account's **Packages** area.

The image will be:

```text
ghcr.io/YOUR-GITHUB-USERNAME/stremio-art-proxy:latest
```

and tagged commits/releases can also produce version tags.

### 6. Making the server pull the image

On your Linux Docker server, your Compose file can use:

```yaml
image: ghcr.io/YOUR-GITHUB-USERNAME/stremio-art-proxy:latest
```

Then:

```bash
docker compose pull
docker compose up -d
```

For a **public** GitHub package, pulling does not require login. If you keep the package private, the server needs GitHub Container Registry authentication with a token that has package read access.

### 7. Normal update workflow

On Windows:

1. Edit code.
2. Run tests locally.
3. In GitHub Desktop, review changed files.
4. Commit.
5. Push origin.
6. GitHub Actions builds/tests/publishes the new image.

On the Linux server:

```bash
docker compose pull
docker compose up -d
```

## Development

```bash
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
uvicorn app.main:app --reload
```

## Current limitations

ThePosterDB discovery is the least stable provider integration. The project does not pretend there is a documented TPDb item-art API when there is not one. The current adapter searches TPDb HTML and uses its stable `data-poster-id`/`api/assets/{id}` mechanism. Exact filtering for TPDb “Original” variation and a perfect ID-only lookup should be treated as the next hardening step.

The admin refresh screen is the place to inspect the candidates that the providers expose, and manual overrides are persisted independently from provider refreshes.

## Security

- Put API keys only in `.env`; never commit `.env`.
- Put the admin UI behind your reverse proxy and/or set `ADMIN_TOKEN`.
- If the app is Internet-facing, strongly consider using your reverse proxy for HTTPS and an additional authentication layer.
- The image proxy is intentionally restricted to known provider domains; it is not a generic SSRF proxy.

### Docker architecture

The GHCR workflow publishes both `linux/amd64` and `linux/arm64` images. Docker automatically selects the matching image variant for the host architecture. On the server, `uname -m` should report `x86_64` for amd64 or `aarch64` for arm64.


### Persistent data

Compose stores application data in `${DOCKER_DATA_DIR:-./data}/art-proxy` on the host and mounts it at `/data` in the container. If your server already defines `DOCKER_DATA_DIR` in its `.env`, use that existing value; no UID/GID setup is required for normal local bind mounts.
