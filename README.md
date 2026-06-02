# homeflix-exporter

Prometheus exporter for Plex/Sonarr/Radarr (homeflix) missing media monitoring.

Image: `ghcr.io/liamniou/homeflix-exporter:latest`

## Environment

- `PLEX_DB_PATH`, `PLEX_PREFS_PATH`, `PLEX_URL`, `PLEX_EXTERNAL_URL`, `EXPECTED_CUSTOM_URL`
- `PLEX_TOKEN`
- `SONARR_INSTANCES` — comma-separated `name|url|api_key`
- `RADARR_INSTANCES` — comma-separated `name|url|api_key`
- `SCRAPE_INTERVAL_SECONDS`, `EXPORTER_PORT`, `LOG_LEVEL`, `PATH_MAPPINGS`
