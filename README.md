# earningsbot

## Development

Run `make` or `make help` to list all targets. The default target shows the help.

| Target         | What it does                                              |
| -------------- | --------------------------------------------------------- |
| `install`      | Install dependencies with `uv sync`                       |
| `test`         | Run pytest with branch coverage                           |
| `cov`          | Run tests and also write an HTML coverage report          |
| `lint`         | Check code with ruff                                      |
| `format`       | Format code with ruff (applies changes)                   |
| `format-check` | Check formatting without changing files                   |
| `fix`          | Auto-fix lint issues, then format code                    |
| `typecheck`    | Type-check with ty                                        |
| `check`        | Run format-check, lint, typecheck, and test (`make all`)  |
| `clean`        | Remove caches and coverage artifacts                      |

## Container

The `Containerfile` builds a small image with uv in a build stage. The final image has no uv and no source tree. It runs as a non-root user (UID and GID 10001). The seen-filings database lives in the `/data` volume.

Build the image:

```sh
podman build -t earningsbot:dev .
```

Show the usage text:

```sh
podman run --rm earningsbot:dev --help
```

Poll once and exit. Keep secrets in an env file outside the repo. Never bake them into the image:

```sh
podman run --rm \
  --env-file .env \
  -v earningsbot-data:/data \
  earningsbot:dev --once
```

Poll forever in the background. The default interval is 60 seconds:

```sh
podman run -d --name earningsbot \
  --env-file .env \
  -v earningsbot-data:/data \
  earningsbot:dev --interval 60
```

| Variable                    | Required | Purpose                                                       |
| --------------------------- | -------- | ------------------------------------------------------------- |
| `EDGAR_IDENTITY`            | yes      | Your `Name email` for the SEC User-Agent                      |
| `DISCORD_EARNINGS_WEBHOOKS` | no       | Comma-separated Discord webhook URLs (secrets)                |
| `EARNINGSBOT_STATE_PATH`    | no       | SQLite path. The image sets `/data/seen.sqlite3`              |
| `STOCK_LOGO`                | no       | Logo URL template. `%s` becomes the lowercase ticker          |
| `TRANSPARENT_PNG`           | no       | Image URL for filings without a known ticker logo             |
