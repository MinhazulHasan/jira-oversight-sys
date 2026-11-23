import uvicorn

from oversight.api.app import prepare_snapshot
from oversight.logs import configure_logging, server_log_config
from oversight.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    # The API's log as `python -m oversight api` writes it: one JSON object per line (LOG_FORMAT=json), uvicorn's own
    # lines through structlog and its access lines off, since they carried the full query string (a Findings search
    # as typed among it); the app logs each request itself as api_request, without the query.
    configure_logging(settings.log_level, settings.log_format)
    # An API without a service key must be an explicit choice (API_ALLOW_INSECURE).
    settings.require_api_auth()
    # The snapshot at this version's schema before serving, as `python -m oversight api` does; serve_app does it
    # again in every worker, so a reload onto new code migrates it too.
    prepare_snapshot(settings.db_file, access_log_days=settings.access_log_days)
    uvicorn.run(
        "oversight.api.app:serve_app",
        factory=True,
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.api_reload,
        log_level="info",
        log_config=server_log_config(),
        access_log=False,
    )
