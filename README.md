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
