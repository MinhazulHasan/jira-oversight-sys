"""The deployment pack (Dockerfile, .dockerignore, deploy/docker-compose.yml): what reaches an image, and that no
secret is in any file. Read as text only: nothing is built or run, and no file under data/ is opened."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from oversight.settings import PROJECT_ROOT, Settings

DOCKERFILE = PROJECT_ROOT / "Dockerfile"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
COMPOSE = PROJECT_ROOT / "deploy" / "docker-compose.yml"
SECRET_WORDS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "WEBHOOK")


# --- .dockerignore, as Docker reads it --------------------------------------------------------------------------------


def _pattern(text: str) -> re.Pattern[str]:
    """A .dockerignore pattern as Go's filepath.Match reads it, with Docker's ** (any number of folders)."""
    out, i = "", 0
    while i < len(text):
        if text.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif text.startswith("**", i):
            out, i = out + ".*", i + 2
        elif text[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif text[i] == "?":
            out, i = out + "[^/]", i + 1
        elif text[i] == "[":
            end = text.index("]", i)
            out, i = out + text[i : end + 1], end + 1
        else:
            out, i = out + re.escape(text[i]), i + 1
    return re.compile(out)


def read_ignore(path: Path) -> list[tuple[bool, re.Pattern[str]]]:
    rules = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        keep = line.startswith("!")
        cleaned = os.path.normpath(line.lstrip("!").strip("/")).replace("\\", "/")
        rules.append((keep, _pattern(cleaned)))
    return rules


def ignored(rules: list[tuple[bool, re.Pattern[str]]], relative: str) -> bool:
    """Docker's rule: the last pattern that matches the path or one of its parent folders decides."""
    parts = relative.split("/")
    candidates = ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]
    verdict = False
    for keep, pattern in rules:
        if any(pattern.fullmatch(candidate) for candidate in candidates):
            verdict = not keep
    return verdict


def context(root: Path, ignore: Path) -> set[str]:
    """Every file that reaches the build context. A folder is not entered when it is ignored and no ! pattern
    could bring anything inside it back (names only: no file is opened)."""
    rules = read_ignore(ignore)
    found: set[str] = set()
    for folder, dirs, files in os.walk(root):
        base = Path(folder).relative_to(root).as_posix()
        prefix = "" if base == "." else base + "/"
        dirs[:] = [d for d in dirs if not (ignored(rules, prefix + d) and not _could_return(rules, prefix + d))]
        found.update(prefix + name for name in files if not ignored(rules, prefix + name))
    return found


def _could_return(rules: list[tuple[bool, re.Pattern[str]]], folder: str) -> bool:
    return any(keep and (pattern.pattern.startswith(re.escape(folder + "/")) or pattern.fullmatch(folder))
               for keep, pattern in rules)


@pytest.fixture(scope="module")
def image_files() -> set[str]:
    return context(PROJECT_ROOT, DOCKERIGNORE)


def test_the_image_gets_the_code_and_config_only(image_files: set[str]) -> None:
    for needed in ("requirements.txt", "run.py", "oversight/__main__.py", "oversight/healthcheck.py",
                   "oversight/store/schema.sql", "config/process.yaml", "config/rules.yaml", "config/fields.yaml"):
        assert needed in image_files
    tops = {path.split("/")[0] for path in image_files}
    assert tops == {"requirements.txt", "run.py", "oversight", "config"}


def test_no_secret_data_bytecode_or_test_reaches_the_image(image_files: set[str]) -> None:
    for path in image_files:
        name = path.rsplit("/", 1)[-1]
        assert not name.startswith(".env") and not name.endswith(".env"), path
        assert not re.search(r"\.db(-wal|-shm|-journal)?$", name), path
        assert "__pycache__" not in path and not name.endswith((".pyc", ".pyo")), path


def test_the_rules_hold_for_files_that_do_not_exist_yet(tmp_path: Path) -> None:
    for name in ("oversight/.env", "oversight/.env.production", "config/prod.env", "oversight/store/copy.db",
                 "oversight/__pycache__/x.cpython-313.pyc", "data/oversight.db", "tests/test_x.py", "deploy/backend.env",
                 ".env", ".env.example", "notes.md", "oversight/keep.py", "config/process.yaml"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
    (tmp_path / ".dockerignore").write_text(DOCKERIGNORE.read_text(encoding="utf-8"), encoding="utf-8")
    assert context(tmp_path, tmp_path / ".dockerignore") == {"oversight/keep.py", "config/process.yaml"}


def test_every_copy_in_the_dockerfile_has_its_source_in_the_context(image_files: set[str]) -> None:
    sources = re.findall(r"^COPY (?!--from)(\S+)", DOCKERFILE.read_text(encoding="utf-8"), re.MULTILINE)
    assert sources == ["requirements.txt", "oversight", "config", "run.py"]
    for source in sources:
        assert source in image_files or any(path.startswith(source + "/") for path in image_files)


# --- Dockerfile ------------------------------------------------------------------------------------------------------


def test_the_dockerfile_runs_slim_python_313_as_an_unprivileged_user() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "ARG PYTHON_IMAGE=python:3.13-slim" in text
    assert re.search(r"^ENV PYTHONDONTWRITEBYTECODE=1", text, re.MULTILINE)
    users = re.findall(r"^USER (\S+)", text, re.MULTILINE)
    assert users == ["oversight"]
    assert text.index("USER oversight") < text.index("CMD [")
    assert 'VOLUME ["/app/data"]' in text
    assert re.search(r'HEALTHCHECK .*\n\s+CMD \["python", "-m", "oversight.healthcheck"\]', text)
    assert 'CMD ["python", "-m", "oversight", "api"]' in text
    assert "pytest" in text and "grep -v '^pytest=='" in text  # the test runner is not shipped


DASHBOARD = PROJECT_ROOT.parent / "jira-oversight-ui"


def test_neither_image_pulls_more_than_its_base_image() -> None:
    # A `# syntax=` line makes BuildKit pull a third, floating image (docker/dockerfile) before it builds.
    files = [DOCKERFILE, *([DASHBOARD / "Dockerfile"] if (DASHBOARD / "Dockerfile").exists() else [])]
    for path in files:
        assert not re.search(r"^#\s*syntax\s*=", path.read_text(encoding="utf-8"), re.MULTILINE | re.IGNORECASE), path


@pytest.mark.skipif(not (DASHBOARD / "Dockerfile").exists(), reason="the dashboard's folder is not beside this one")
def test_the_dashboards_code_is_roots_and_its_health_check_asks_the_api() -> None:
    text = (DASHBOARD / "Dockerfile").read_text(encoding="utf-8")
    run = text[text.index("AS run"):]
    # The runtime user cannot rewrite the server's code; only Next.js's cache folder is its to write.
    assert "--chown" not in run
    assert "chown node:node .next/cache" in run and run.index("chown node:node") < run.index("USER node")
    # Healthy only when the API answers the dashboard's own settings (a wrong key or a stopped API is unhealthy).
    assert re.search(r'HEALTHCHECK .*\n\s+CMD \["node", "healthcheck.mjs"\]', run)
    assert "COPY --from=build /app/scripts/healthcheck.mjs ./healthcheck.mjs" in run
    assert "!scripts/healthcheck.mjs" in (DASHBOARD / ".dockerignore").read_text(encoding="utf-8").splitlines()
    script = (DASHBOARD / "scripts" / "healthcheck.mjs").read_text(encoding="utf-8")
    assert "OVERSIGHT_SERVICE_KEY" in script and "api/v1/health" in script


def _template(path: Path) -> dict[str, str]:
    """A settings template's NAME=value lines (comments and blank lines left out)."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            found[name.strip()] = value.strip()
    return found


def test_the_production_templates_fail_safe(tmp_path: Path) -> None:
    # Regression: step 5.1 copied the development templates, so a missed line left a developer an admin, the vendor's
    # domain allowed in, a US region for a UK/EU client, and an email digest with no mail server.
    backend = _template(PROJECT_ROOT / "deploy" / "backend.env.example")
    dashboard = _template(PROJECT_ROOT / "deploy" / "dashboard.env.example")
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECTS", "API_SERVICE_KEY", "AWS_REGION",
                 "CORS_ALLOW_ORIGINS", "REPORT_TIMEZONE", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "SMTP_HOST"):
        assert backend[name] == "", name
    assert (backend["LLM_MODE"], backend["DIGEST_CHANNEL"], backend["LOG_FORMAT"]) == ("stub", "file", "json")
    assert (backend["API_DOCS"], backend["API_ALLOW_INSECURE"]) == ("false", "false")
    for name in ("APP_URL", "ADMIN_EMAILS", "ALLOWED_EMAIL_DOMAINS", "AUTH_SECRET", "OVERSIGHT_SERVICE_KEY",
                 "ATLASSIAN_AUTH_URL", "ATLASSIAN_API_URL"):
        assert dashboard[name] == "", name
    for text in (str(backend), str(dashboard)):
        assert "@" not in text and "atlassian.net" not in text and "localhost" not in text
    # Every name is one the code reads: the backend's Settings, the dashboard's own template.
    fields = {name.upper() for name in Settings.model_fields}
    assert set(backend) <= fields, set(backend) - fields
    ui_template = DASHBOARD / ".env.example"
    if ui_template.exists():
        assert set(dashboard) <= set(_template(ui_template))
    # The backend's loads as it stands (the API then refuses to start without a key, and the doctor fails it).
    copy = tmp_path / "backend.env"
    copy.write_text((PROJECT_ROOT / "deploy" / "backend.env.example").read_text(encoding="utf-8"), encoding="utf-8")
    loaded = Settings(_env_file=copy)
    assert loaded.llm_mode == "stub" and loaded.digest_channel == "file"


# The guide is kept out of git (.gitignore): a fresh clone has none, and its text is then not checked.
GUIDE = PROJECT_ROOT / "docs" / "DEPLOYMENT.md"


@pytest.mark.skipif(not GUIDE.exists(), reason="docs/DEPLOYMENT.md is local only, not on this machine")
def test_the_guide_works_as_written_and_says_what_a_new_auth_secret_does() -> None:
    # Regressions (JO-12 review): the guide said a new AUTH_SECRET only signs everyone out, though it re-keys every
    # viewer in the access log; promised a snapshot warning step 3 had already made impossible; used `http2 on;`,
    # which the nginx most distributions package rejects; never wrote a baseline; and named no picture host exactly.
    text = GUIDE.read_text(encoding="utf-8")
    rotation = text[text.index("5. Rotating a secret later") : text.index("## 6. Build and start")]
    assert "new id in the access log" in rotation and "Export the access log first" in rotation
    check = text[text.index("4. Check the deployment's settings.") :][:400]
    assert "only the\n   backups line warns" in check and "no snapshot" not in check
    https = text[text.index("## 7. HTTPS") : text.index("## 8. Backups")]
    assert "listen 443 ssl http2;" in https
    assert not re.search(r"^\s*http2 on;", https, re.MULTILINE)  # only named in prose, as the newer form
    assert "dc run --rm tools baseline --project" in text
    assert "avatar-management--avatars.<region>.prod.public.atl-paas.net" in text


def test_files_are_lf_only() -> None:
    for path in (DOCKERFILE, DOCKERIGNORE, COMPOSE, *([GUIDE] if GUIDE.exists() else [])):
        assert b"\r\n" not in path.read_bytes(), path


# --- the compose file -------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


def test_the_stack_is_the_api_the_dashboard_the_sync_and_the_tools(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert set(services) == {"api", "dashboard", "sync", "tools"}
    assert services["sync"]["command"] == ["python", "-m", "oversight", "cycle", "--loop"]
    assert services["sync"]["image"] == services["api"]["image"] == services["tools"]["image"]
    assert services["tools"]["profiles"] == ["tools"]
    assert services["sync"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["dashboard"]["depends_on"]["api"]["condition"] == "service_healthy"
    for name in ("api", "sync", "tools"):
        assert "oversight-data:/app/data" in services[name]["volumes"]
    assert set(compose["volumes"]) == {"oversight-data", "oversight-reports"}


def test_settings_come_from_env_files_and_no_value_is_a_secret(compose: dict[str, Any]) -> None:
    files = {service: [entry["path"] for entry in spec["env_file"]] for service, spec in compose["services"].items()}
    assert files == {"api": ["./backend.env"], "sync": ["./backend.env"], "tools": ["./backend.env"],
                     "dashboard": ["./dashboard.env"]}
    for service, spec in compose["services"].items():
        for name in spec.get("environment", {}):
            assert not any(word in name.upper() for word in SECRET_WORDS), f"{service}: {name}"
    text = COMPOSE.read_text(encoding="utf-8")
    assert not re.search(r"(?i)(sk-ant|ATATT|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY)", text)
    # The env files stay out of git and out of every image.
    assert "deploy/*.env" in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()


def test_only_the_dashboard_is_published_and_only_on_loopback(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert all("ports" not in services[name] for name in ("api", "sync", "tools"))
    assert services["dashboard"]["ports"] == ["127.0.0.1:${DASHBOARD_PORT:-3400}:3400"]


def test_the_wiring_cannot_be_changed_by_an_env_file(compose: dict[str, Any]) -> None:
    services = compose["services"]
    assert services["dashboard"]["environment"]["OVERSIGHT_API_URL"] == "http://api:3300"
    assert services["dashboard"]["environment"]["OVERSIGHT_DATA"] == "live"
    assert services["dashboard"]["environment"]["AUTH_MODE"] == "atlassian"
    for name in ("api", "sync", "tools"):
        assert services[name]["environment"]["DB_PATH"] == "/app/data/oversight.db"
        assert services[name]["environment"]["API_PORT"] == "3300"


def test_every_service_is_locked_down(compose: dict[str, Any]) -> None:
    for name, spec in compose["services"].items():
        assert spec["cap_drop"] == ["ALL"], name
        assert spec["security_opt"] == ["no-new-privileges:true"], name
        assert spec["pull_policy"] == "never", name
        assert spec["init"] is True, name
    for name in ("api", "sync", "tools"):
        assert compose["services"][name]["read_only"] is True
