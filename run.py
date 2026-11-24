import uvicorn

from oversight.api.app import prepare_snapshot
from oversight.logs import configure_logging, server_log_config
from oversight.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    # An API without a service key must be an explicit choice (API_ALLOW_INSECURE).
    settings.require_api_auth()
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
