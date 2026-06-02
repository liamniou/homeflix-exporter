#!/usr/bin/env python3
"""Prometheus exporter for Homeflix (Plex, Radarr, Sonarr, etc. setup)"""

import os
import re
import signal
import sys
import time
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Generator

import requests
from prometheus_client import start_http_server, Gauge, Info

# --- Config via env vars ---
PLEX_DB_PATH = os.environ.get(
    "PLEX_DB_PATH",
    "/var/lib/plexmediaserver/Library/Application Support/"
    "Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db",
)
SCRAPE_INTERVAL_SECONDS = int(os.environ.get("SCRAPE_INTERVAL_SECONDS", "30"))
EXPORTER_PORT = int(os.environ.get("EXPORTER_PORT", "9108"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# Optional: filter by library_section_id; leave empty for all
_lib_ids = os.environ.get("LIBRARY_SECTION_IDS", "").strip()
LIBRARY_SECTION_IDS: list[int] = (
    [int(x) for x in _lib_ids.split(",") if x.strip()] if _lib_ids else []
)

# Plex server URL for API checks
PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400")

# Path to Preferences.xml for config monitoring
PLEX_PREFS_PATH = os.environ.get(
    "PLEX_PREFS_PATH",
    "/var/lib/plexmediaserver/Library/Application Support/"
    "Plex Media Server/Preferences.xml",
)

# Expected custom connection URL (for validation)
EXPECTED_CUSTOM_URL = os.environ.get("EXPECTED_CUSTOM_URL", "")

# External URL for accessibility check (e.g., https://plex.tokyo3.eu)
PLEX_EXTERNAL_URL = os.environ.get("PLEX_EXTERNAL_URL", "")
PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "")

# Sonarr configuration - supports multiple instances
# Format: SONARR_INSTANCES=name|url|apikey,name2|url2|apikey2
# Or legacy single instance: SONARR_URL + SONARR_API_KEY
SONARR_INSTANCES: list[tuple[str, str, str]] = []  # [(name, url, apikey), ...]
_sonarr_instances = os.environ.get("SONARR_INSTANCES", "").strip()
if _sonarr_instances:
    for instance in _sonarr_instances.split(","):
        parts = instance.strip().split("|")
        if len(parts) == 3:
            name, url, apikey = parts
            SONARR_INSTANCES.append((name.strip(), url.strip().rstrip("/"), apikey.strip()))
else:
    # Legacy single instance support
    _sonarr_url = os.environ.get("SONARR_URL", "").strip().rstrip("/")
    _sonarr_key = os.environ.get("SONARR_API_KEY", "").strip()
    if _sonarr_url and _sonarr_key:
        SONARR_INSTANCES.append(("default", _sonarr_url, _sonarr_key))

# Radarr configuration - supports multiple instances
# Format: RADARR_INSTANCES=name|url|apikey,name2|url2|apikey2
# Or legacy single instance: RADARR_URL + RADARR_API_KEY
RADARR_INSTANCES: list[tuple[str, str, str]] = []  # [(name, url, apikey), ...]
_radarr_instances = os.environ.get("RADARR_INSTANCES", "").strip()
if _radarr_instances:
    for instance in _radarr_instances.split(","):
        parts = instance.strip().split("|")
        if len(parts) == 3:
            name, url, apikey = parts
            RADARR_INSTANCES.append((name.strip(), url.strip().rstrip("/"), apikey.strip()))
else:
    # Legacy single instance support
    _radarr_url = os.environ.get("RADARR_URL", "").strip().rstrip("/")
    _radarr_key = os.environ.get("RADARR_API_KEY", "").strip()
    if _radarr_url and _radarr_key:
        RADARR_INSTANCES.append(("default", _radarr_url, _radarr_key))

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# --- Prometheus metrics ---
plex_unavailable_media = Gauge(
    "plex_unavailable_media_count",
    "Number of Plex media files marked unavailable by Plex (deleted_at set during library scan)",
    ["library_section_id", "library_name"],
)

plex_total_media = Gauge(
    "plex_total_media_count",
    "Total number of media parts in the library",
    ["library_section_id", "library_name"],
)

plex_exporter_info = Info(
    "homeflix_exporter",
    "Information about the Homeflix exporter",
)

plex_scrape_success = Gauge(
    "homeflix_exporter_scrape_success",
    "Whether the last scrape was successful (1=success, 0=failure)",
)

plex_scrape_duration = Gauge(
    "homeflix_exporter_scrape_duration_seconds",
    "Duration of the last scrape in seconds",
)

# --- Plex configuration metrics ---
plex_config_publish_enabled = Gauge(
    "plex_config_publish_enabled",
    "Whether server publishing to plex.tv is enabled (1=enabled, 0=disabled)",
)

plex_config_manual_port_mode = Gauge(
    "plex_config_manual_port_mode",
    "Whether manual port mapping mode is enabled (1=enabled, 0=disabled)",
)

plex_config_manual_port = Gauge(
    "plex_config_manual_port",
    "The configured manual port mapping port number",
)

plex_config_custom_url_valid = Gauge(
    "plex_config_custom_url_valid",
    "Whether custom connections URL matches expected (1=valid, 0=invalid/missing)",
)

plex_config_relay_enabled = Gauge(
    "plex_config_relay_enabled",
    "Whether Plex relay is enabled (1=enabled, 0=disabled)",
)

plex_myplex_signin_ok = Gauge(
    "plex_myplex_signin_ok",
    "Whether Plex is signed into MyPlex (1=ok, 0=not signed in)",
)

plex_server_claimed = Gauge(
    "plex_server_claimed",
    "Whether Plex server is claimed (1=claimed, 0=unclaimed)",
)

plex_config_info = Info(
    "plex_config",
    "Plex server configuration details",
)

# --- External accessibility metrics ---
plex_external_accessible = Gauge(
    "plex_external_accessible",
    "Whether Plex is accessible via external URL (1=accessible, 0=not accessible)",
)

plex_external_library_count = Gauge(
    "plex_external_library_count",
    "Number of libraries visible via external URL (-1 if check failed)",
)

plex_external_response_time = Gauge(
    "plex_external_response_time_seconds",
    "Response time for external URL check in seconds",
)

# --- Sonarr metrics ---
sonarr_missing_available = Gauge(
    "sonarr_missing_available_episodes",
    "Number of missing episodes that have been digitally released",
    ["instance"],
)

sonarr_missing_total = Gauge(
    "sonarr_missing_total_episodes",
    "Total number of missing episodes in Sonarr",
    ["instance"],
)

sonarr_api_success = Gauge(
    "sonarr_api_success",
    "Whether the last Sonarr API call was successful (1=success, 0=failure)",
    ["instance"],
)

# --- Radarr metrics ---
radarr_missing_available = Gauge(
    "radarr_missing_available_movies",
    "Number of missing movies that have been digitally released",
    ["instance"],
)

radarr_missing_total = Gauge(
    "radarr_missing_total_movies",
    "Total number of missing monitored movies in Radarr",
    ["instance"],
)

radarr_api_success = Gauge(
    "radarr_api_success",
    "Whether the last Radarr API call was successful (1=success, 0=failure)",
    ["instance"],
)

# --- Missing content detail metrics (with metadata for dashboards) ---
sonarr_missing_episode = Gauge(
    "sonarr_missing_episode",
    "Missing episode that has been digitally released (value=1 per item)",
    ["instance", "title", "year", "poster_url"],
)

radarr_missing_movie = Gauge(
    "radarr_missing_movie",
    "Missing movie that has been released (value=1 per item)",
    ["instance", "title", "year", "poster_url"],
)

# --- Queue warning metrics (items needing manual attention) ---
sonarr_queue_warning = Gauge(
    "sonarr_queue_warning",
    "Sonarr queue item needing manual attention (value=1 when issue exists)",
    ["instance", "title", "year", "poster_url", "message"],
)

radarr_queue_warning = Gauge(
    "radarr_queue_warning",
    "Radarr queue item needing manual attention (value=1 when issue exists)",
    ["instance", "title", "year", "poster_url", "message"],
)

# Graceful shutdown flag
running = True


def signal_handler(signum: int, frame) -> None:
    """Handle shutdown signals gracefully."""
    global running
    logger.info("Received signal %d, shutting down...", signum)
    running = False


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    """Create a read-only connection to the Plex database."""
    uri = f"file:{PLEX_DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        yield conn
    finally:
        conn.close()


def validate_database() -> bool:
    """Check if the Plex database exists and is accessible."""
    db_path = Path(PLEX_DB_PATH)
    if not db_path.exists():
        logger.error("Plex database not found: %s", PLEX_DB_PATH)
        return False
    if not db_path.is_file():
        logger.error("Path is not a file: %s", PLEX_DB_PATH)
        return False
    try:
        with get_connection() as conn:
            conn.execute("SELECT 1 FROM library_sections LIMIT 1")
        logger.info("Database connection validated successfully")
        return True
    except sqlite3.Error as e:
        logger.error("Cannot connect to database: %s", e)
        return False


def build_media_query() -> str:
    """Build SQL query to get media part counts per library, split by deleted_at status.

    Plex sets deleted_at on media_parts when a library scan detects a file is missing.
    We count those as unavailable rather than doing filesystem checks from the container,
    because the media volume may be a remote/FUSE mount that is not reliably accessible
    inside the Docker container.
    """
    base = """
        SELECT
            library_sections.id AS library_section_id,
            library_sections.name AS library_name,
            COUNT(*) AS total,
            SUM(CASE WHEN media_parts.deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS unavailable
        FROM media_parts
        JOIN media_items ON media_items.id = media_parts.media_item_id
        JOIN metadata_items ON metadata_items.id = media_items.metadata_item_id
        JOIN library_sections ON library_sections.id = metadata_items.library_section_id
        {filter_clause}
        GROUP BY library_sections.id, library_sections.name
    """
    if LIBRARY_SECTION_IDS:
        placeholders = ",".join("?" for _ in LIBRARY_SECTION_IDS)
        filter_clause = f"WHERE library_sections.id IN ({placeholders})"
    else:
        filter_clause = ""
    return base.format(filter_clause=filter_clause)


MEDIA_QUERY = build_media_query()


def parse_preferences() -> dict[str, str]:
    """Parse Plex Preferences.xml and return settings as a dict."""
    prefs: dict[str, str] = {}
    prefs_path = Path(PLEX_PREFS_PATH)
    
    if not prefs_path.exists():
        logger.warning("Preferences.xml not found: %s", PLEX_PREFS_PATH)
        return prefs
    
    try:
        content = prefs_path.read_text()
        # Parse XML attributes using regex (simple and safe for this format)
        for match in re.finditer(r'(\w+)="([^"]*)"', content):
            prefs[match.group(1)] = match.group(2)
    except Exception as e:
        logger.error("Error reading Preferences.xml: %s", e)
    
    return prefs


def check_plex_api() -> dict[str, str]:
    """Check Plex server API for status information."""
    status: dict[str, str] = {}
    
    try:
        resp = requests.get(f"{PLEX_URL}/", timeout=5)
        resp.raise_for_status()
        content = resp.text
        
        # Parse XML attributes
        for match in re.finditer(r'(\w+)="([^"]*)"', content):
            status[match.group(1)] = match.group(2)
    except requests.RequestException as e:
        logger.error("Error checking Plex API: %s", e)
    
    return status


def collect_config_metrics() -> None:
    """Collect Plex configuration metrics from Preferences.xml and API."""
    try:
        # Read preferences file
        prefs = parse_preferences()
        
        # Check Plex API
        api_status = check_plex_api()
        
        # PublishServerOnPlexOnlineKey
        publish_enabled = prefs.get("PublishServerOnPlexOnlineKey", "0") == "1"
        plex_config_publish_enabled.set(1 if publish_enabled else 0)
        
        # ManualPortMappingMode
        manual_mode = prefs.get("ManualPortMappingMode", "0") == "1"
        plex_config_manual_port_mode.set(1 if manual_mode else 0)
        
        # ManualPortMappingPort
        manual_port = prefs.get("ManualPortMappingPort", "0")
        try:
            plex_config_manual_port.set(int(manual_port))
        except ValueError:
            plex_config_manual_port.set(0)
        
        # customConnections validation
        custom_url = prefs.get("customConnections", "")
        if EXPECTED_CUSTOM_URL:
            url_valid = custom_url == EXPECTED_CUSTOM_URL
        else:
            url_valid = bool(custom_url)  # Just check it's not empty
        plex_config_custom_url_valid.set(1 if url_valid else 0)
        
        # RelayEnabled
        relay_enabled = prefs.get("RelayEnabled", "1") == "1"
        plex_config_relay_enabled.set(1 if relay_enabled else 0)
        
        # MyPlex signin state from API
        signin_state = api_status.get("myPlexSigninState", "")
        plex_myplex_signin_ok.set(1 if signin_state == "ok" else 0)
        
        # Server claimed status from API
        claimed = api_status.get("claimed", "0") == "1"
        plex_server_claimed.set(1 if claimed else 0)
        
        # Set info metric with configuration details
        plex_config_info.info({
            "custom_connections": custom_url,
            "manual_port": manual_port,
            "publish_enabled": "true" if publish_enabled else "false",
            "manual_mode": "true" if manual_mode else "false",
            "myplex_signin_state": signin_state,
            "server_claimed": "true" if claimed else "false",
        })
        
        # Log warnings for misconfiguration
        if not publish_enabled:
            logger.warning("Server publishing is DISABLED")
        if not manual_mode:
            logger.warning("Manual port mapping mode is DISABLED")
        if not url_valid:
            logger.warning("Custom URL mismatch: got '%s', expected '%s'", 
                         custom_url, EXPECTED_CUSTOM_URL or "(any non-empty)")
        if signin_state != "ok":
            logger.warning("MyPlex signin state: %s", signin_state or "unknown")
        if not claimed:
            logger.warning("Server is NOT claimed")
            
    except Exception as e:
        logger.exception("Error collecting config metrics: %s", e)


def collect_external_metrics() -> None:
    """Check if Plex is accessible via external URL and count libraries."""
    if not PLEX_EXTERNAL_URL:
        logger.debug("External URL not configured, skipping external check")
        return
    
    try:
        start_time = time.time()
        
        # Build URL with token if provided
        url = f"{PLEX_EXTERNAL_URL}/library/sections"
        if PLEX_TOKEN:
            url += f"?X-Plex-Token={PLEX_TOKEN}"
        
        resp = requests.get(url, timeout=10)
        response_time = time.time() - start_time
        plex_external_response_time.set(response_time)
        
        if resp.status_code == 200:
            # Count libraries by counting title attributes
            content = resp.text
            libraries = re.findall(r'title="[^"]*"', content)
            library_count = len(libraries)
            
            plex_external_accessible.set(1)
            plex_external_library_count.set(library_count)
            logger.debug(
                "External check OK: %d libraries in %.2fs",
                library_count,
                response_time,
            )
        else:
            plex_external_accessible.set(0)
            plex_external_library_count.set(-1)
            logger.warning(
                "External check failed: HTTP %d from %s",
                resp.status_code,
                PLEX_EXTERNAL_URL,
            )
            
    except requests.RequestException as e:
        plex_external_accessible.set(0)
        plex_external_library_count.set(-1)
        plex_external_response_time.set(-1)
        logger.warning("External check failed: %s", e)
    except Exception as e:
        logger.exception("Error in external check: %s", e)
        plex_external_accessible.set(0)
        plex_external_library_count.set(-1)


def parse_date(date_str: str | None) -> datetime | None:
    """Parse ISO date string from Sonarr/Radarr API."""
    if not date_str:
        return None
    try:
        # Handle various ISO formats
        date_str = date_str.replace("Z", "+00:00")
        if "T" in date_str:
            return datetime.fromisoformat(date_str)
        else:
            return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def collect_sonarr_metrics() -> None:
    """Collect metrics from Sonarr API for missing episodes."""
    if not SONARR_INSTANCES:
        logger.debug("Sonarr not configured, skipping")
        return

    # Clear previous missing episode details
    sonarr_missing_episode.clear()

    for instance_name, sonarr_url, sonarr_api_key in SONARR_INSTANCES:
        try:
            headers = {"X-Api-Key": sonarr_api_key}
            now = datetime.now(timezone.utc)

            # Get all missing episodes (paginated)
            missing_total = 0
            missing_available = 0
            missing_titles: list[str] = []
            page = 1
            page_size = 100

            while True:
                resp = requests.get(
                    f"{sonarr_url}/api/v3/wanted/missing",
                    headers=headers,
                    params={
                        "page": page,
                        "pageSize": page_size,
                        "includeSeries": "true",
                        "monitored": "true",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                records = data.get("records", [])
                if not records:
                    break

                for episode in records:
                    missing_total += 1

                    # Check if digitally released
                    air_date = parse_date(episode.get("airDateUtc"))
                    if air_date and air_date <= now:
                        missing_available += 1
                        series = episode.get("series", {})
                        series_title = series.get("title", "Unknown")
                        year = str(series.get("year", ""))
                        season = episode.get("seasonNumber", 0)
                        ep_num = episode.get("episodeNumber", 0)
                        ep_title = episode.get("title", "")
                        full_title = f"{series_title} S{season:02d}E{ep_num:02d}"

                        # Get poster URL from series images
                        poster_url = ""
                        for image in series.get("images", []):
                            if image.get("coverType") == "poster":
                                remote_url = image.get("remoteUrl", "")
                                if remote_url:
                                    poster_url = remote_url
                                    break

                        # Set detailed metric
                        sonarr_missing_episode.labels(
                            instance=instance_name,
                            title=full_title,
                            year=year,
                            poster_url=poster_url,
                        ).set(1)

                        missing_titles.append(f"{full_title} - {ep_title}")

                # Check if there are more pages
                total_records = data.get("totalRecords", 0)
                if page * page_size >= total_records:
                    break
                page += 1

            sonarr_missing_total.labels(instance=instance_name).set(missing_total)
            sonarr_missing_available.labels(instance=instance_name).set(missing_available)
            sonarr_api_success.labels(instance=instance_name).set(1)

            if missing_available > 0:
                logger.info(
                    "Sonarr [%s]: %d missing episodes available for download",
                    instance_name,
                    missing_available,
                )
                for title in missing_titles:
                    logger.info("  Sonarr [%s] Missing: %s", instance_name, title)

        except requests.RequestException as e:
            logger.warning("Sonarr [%s] API error: %s", instance_name, e)
            sonarr_api_success.labels(instance=instance_name).set(0)
        except Exception as e:
            logger.exception("Error collecting Sonarr [%s] metrics: %s", instance_name, e)
            sonarr_api_success.labels(instance=instance_name).set(0)


def collect_radarr_metrics() -> None:
    """Collect metrics from Radarr API for missing movies."""
    if not RADARR_INSTANCES:
        logger.debug("Radarr not configured, skipping")
        return

    # Clear previous missing movie details
    radarr_missing_movie.clear()

    for instance_name, radarr_url, radarr_api_key in RADARR_INSTANCES:
        try:
            headers = {"X-Api-Key": radarr_api_key}
            now = datetime.now(timezone.utc)

            # Get all movies
            resp = requests.get(
                f"{radarr_url}/api/v3/movie",
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            movies = resp.json()

            missing_total = 0
            missing_available = 0
            missing_titles: list[str] = []

            for movie in movies:
                # Only count monitored movies without files
                if not movie.get("monitored", False):
                    continue
                if movie.get("hasFile", False):
                    continue

                missing_total += 1

                # Check if digitally or physically released
                digital_release = parse_date(movie.get("digitalRelease"))
                physical_release = parse_date(movie.get("physicalRelease"))

                is_available = False
                if digital_release and digital_release <= now:
                    is_available = True
                elif physical_release and physical_release <= now:
                    is_available = True

                if is_available:
                    missing_available += 1
                    title = movie.get("title", "Unknown")
                    year = str(movie.get("year", ""))

                    # Get poster URL from movie images
                    poster_url = ""
                    for image in movie.get("images", []):
                        if image.get("coverType") == "poster":
                            remote_url = image.get("remoteUrl", "")
                            if remote_url:
                                poster_url = remote_url
                                break

                    # Set detailed metric
                    radarr_missing_movie.labels(
                        instance=instance_name,
                        title=title,
                        year=year,
                        poster_url=poster_url,
                    ).set(1)

                    missing_titles.append(f"{title} ({year})")

            radarr_missing_total.labels(instance=instance_name).set(missing_total)
            radarr_missing_available.labels(instance=instance_name).set(missing_available)
            radarr_api_success.labels(instance=instance_name).set(1)

            if missing_available > 0:
                logger.info(
                    "Radarr [%s]: %d missing movies available for download",
                    instance_name,
                    missing_available,
                )
                for title in missing_titles:
                    logger.info("  Radarr [%s] Missing: %s", instance_name, title)

        except requests.RequestException as e:
            logger.warning("Radarr [%s] API error: %s", instance_name, e)
            radarr_api_success.labels(instance=instance_name).set(0)
        except Exception as e:
            logger.exception("Error collecting Radarr [%s] metrics: %s", instance_name, e)
            radarr_api_success.labels(instance=instance_name).set(0)


def should_skip_queue_item(messages: list[str]) -> bool:
    """Check if a queue item should be skipped based on its messages.
    
    Returns True if ANY message indicates the item is not actionable
    (e.g., custom format issues that require no action).
    """
    skip_prefixes = [
        "Not a Custom Format upgrade",
        "Not an upgrade for existing",
        "Found archive file, might need to be extracted",
    ]
    
    for message in messages:
        for prefix in skip_prefixes:
            if message.startswith(prefix):
                return True
    
    return False


def collect_sonarr_queue_warnings() -> None:
    """Collect Sonarr queue items that need manual attention."""
    if not SONARR_INSTANCES:
        return

    # Clear previous queue warnings
    sonarr_queue_warning.clear()

    for instance_name, sonarr_url, sonarr_api_key in SONARR_INSTANCES:
        try:
            headers = {"X-Api-Key": sonarr_api_key}

            # Get queue with series info
            resp = requests.get(
                f"{sonarr_url}/api/v3/queue",
                headers=headers,
                params={
                    "includeSeries": "true",
                    "includeEpisode": "true",
                    "pageSize": 100,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            records = data.get("records", [])
            for item in records:
                # Check if item needs attention (warning or error status)
                tracked_status = item.get("trackedDownloadStatus", "").lower()
                if tracked_status not in ("warning", "error"):
                    continue

                # Get series info
                series = item.get("series", {})
                series_title = series.get("title", "Unknown")
                year = str(series.get("year", ""))

                # Build poster URL from series images
                poster_url = ""
                for image in series.get("images", []):
                    if image.get("coverType") == "poster":
                        remote_url = image.get("remoteUrl", "")
                        if remote_url:
                            poster_url = remote_url
                            break

                # Get status messages
                status_messages = item.get("statusMessages", [])
                messages = []
                for sm in status_messages:
                    sm_title = sm.get("title", "")
                    sm_msgs = sm.get("messages", [])
                    if sm_msgs:
                        messages.extend(sm_msgs)
                    elif sm_title:
                        messages.append(sm_title)

                # Skip if any message indicates this is not actionable
                if should_skip_queue_item(messages):
                    continue

                # Deduplicate and join
                messages = list(dict.fromkeys(messages))
                message = "; ".join(messages) if messages else "Unknown issue"

                # Episode info for better title
                episode = item.get("episode", {})
                if episode:
                    season = episode.get("seasonNumber", 0)
                    ep_num = episode.get("episodeNumber", 0)
                    full_title = f"{series_title} S{season:02d}E{ep_num:02d}"
                else:
                    full_title = series_title

                sonarr_queue_warning.labels(
                    instance=instance_name,
                    title=full_title,
                    year=year,
                    poster_url=poster_url,
                    message=message,
                ).set(1)

                logger.warning(
                    "Sonarr [%s] queue warning: %s (%s) - %s",
                    instance_name,
                    full_title,
                    year,
                    message,
                )

        except requests.RequestException as e:
            logger.warning("Sonarr [%s] queue API error: %s", instance_name, e)
        except Exception as e:
            logger.exception("Error collecting Sonarr [%s] queue warnings: %s", instance_name, e)


def collect_radarr_queue_warnings() -> None:
    """Collect Radarr queue items that need manual attention."""
    if not RADARR_INSTANCES:
        return

    # Clear previous queue warnings
    radarr_queue_warning.clear()

    for instance_name, radarr_url, radarr_api_key in RADARR_INSTANCES:
        try:
            headers = {"X-Api-Key": radarr_api_key}

            # Get queue with movie info
            resp = requests.get(
                f"{radarr_url}/api/v3/queue",
                headers=headers,
                params={
                    "includeMovie": "true",
                    "pageSize": 100,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            records = data.get("records", [])
            for item in records:
                # Check if item needs attention (warning or error status)
                tracked_status = item.get("trackedDownloadStatus", "").lower()
                if tracked_status not in ("warning", "error"):
                    continue

                # Get movie info
                movie = item.get("movie", {})
                title = movie.get("title", item.get("title", "Unknown"))
                year = str(movie.get("year", ""))

                # Build poster URL from movie images
                poster_url = ""
                for image in movie.get("images", []):
                    if image.get("coverType") == "poster":
                        remote_url = image.get("remoteUrl", "")
                        if remote_url:
                            poster_url = remote_url
                            break

                # Get status messages
                status_messages = item.get("statusMessages", [])
                messages = []
                for sm in status_messages:
                    sm_title = sm.get("title", "")
                    sm_msgs = sm.get("messages", [])
                    if sm_msgs:
                        messages.extend(sm_msgs)
                    elif sm_title:
                        messages.append(sm_title)

                # Skip if any message indicates this is not actionable
                if should_skip_queue_item(messages):
                    continue

                # Deduplicate and join
                messages = list(dict.fromkeys(messages))
                message = "; ".join(messages) if messages else "Unknown issue"

                radarr_queue_warning.labels(
                    instance=instance_name,
                    title=title,
                    year=year,
                    poster_url=poster_url,
                    message=message,
                ).set(1)

                logger.warning(
                    "Radarr [%s] queue warning: %s (%s) - %s",
                    instance_name,
                    title,
                    year,
                    message,
                )

        except requests.RequestException as e:
            logger.warning("Radarr [%s] queue API error: %s", instance_name, e)
        except Exception as e:
            logger.exception("Error collecting Radarr [%s] queue warnings: %s", instance_name, e)


def collect_metrics() -> None:
    """Collect metrics from Plex database using Plex's own deleted_at tracking.

    Plex sets media_parts.deleted_at when a library scan detects a file is missing.
    This approach trusts Plex's scan-based detection rather than re-checking files
    from the container filesystem (which may not have the same mount visibility).
    """
    start_time = time.time()
    
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            params = LIBRARY_SECTION_IDS if LIBRARY_SECTION_IDS else []
            cur.execute(MEDIA_QUERY, params)
            rows = cur.fetchall()

        # Clear and set metrics
        plex_unavailable_media.clear()
        plex_total_media.clear()

        for library_id, library_name, total, unavailable in rows:
            labels = {
                "library_section_id": str(library_id),
                "library_name": library_name,
            }
            plex_unavailable_media.labels(**labels).set(unavailable)
            plex_total_media.labels(**labels).set(total)

            if unavailable > 0:
                logger.info(
                    "Library '%s': total=%d, unavailable=%d",
                    library_name,
                    total,
                    unavailable,
                )

        plex_scrape_success.set(1)
        duration = time.time() - start_time
        plex_scrape_duration.set(duration)
        logger.debug("Metrics collected in %.2fs", duration)

    except Exception as e:
        logger.exception("Error collecting metrics: %s", e)
        plex_scrape_success.set(0)


def main() -> None:
    """Main entry point."""
    # Setup signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("Starting Homeflix unavailable media exporter on port %d", EXPORTER_PORT)
    logger.info("Database path: %s", PLEX_DB_PATH)
    logger.info("Scrape interval: %d seconds", SCRAPE_INTERVAL_SECONDS)

    if LIBRARY_SECTION_IDS:
        logger.info("Filtering library sections: %s", LIBRARY_SECTION_IDS)

    # Validate database connection
    if not validate_database():
        logger.error("Failed to validate database, exiting...")
        sys.exit(1)

    # Log config monitoring settings
    logger.info("Plex URL: %s", PLEX_URL)
    logger.info("Preferences path: %s", PLEX_PREFS_PATH)
    if EXPECTED_CUSTOM_URL:
        logger.info("Expected custom URL: %s", EXPECTED_CUSTOM_URL)
    if PLEX_EXTERNAL_URL:
        logger.info("External URL for accessibility check: %s", PLEX_EXTERNAL_URL)

    # Log Sonarr/Radarr configuration
    if SONARR_INSTANCES:
        logger.info("Sonarr instances: %d", len(SONARR_INSTANCES))
        for name, url, _ in SONARR_INSTANCES:
            logger.info("  - %s: %s", name, url)
    else:
        logger.info("Sonarr not configured")

    if RADARR_INSTANCES:
        logger.info("Radarr instances: %d", len(RADARR_INSTANCES))
        for name, url, _ in RADARR_INSTANCES:
            logger.info("  - %s: %s", name, url)
    else:
        logger.info("Radarr not configured")

    # Set exporter info
    plex_exporter_info.info({
        "version": "1.6.0",
        "db_path": PLEX_DB_PATH,
    })

    # Start HTTP server
    start_http_server(EXPORTER_PORT)
    logger.info("Exporter started successfully")

    # Main loop
    while running:
        collect_metrics()
        collect_config_metrics()
        collect_external_metrics()
        collect_sonarr_metrics()
        collect_radarr_metrics()
        collect_sonarr_queue_warnings()
        collect_radarr_queue_warnings()
        # Sleep in smaller increments for faster shutdown response
        for _ in range(SCRAPE_INTERVAL_SECONDS):
            if not running:
                break
            time.sleep(1)

    logger.info("Exporter shutdown complete")


if __name__ == "__main__":
    main()
