"""The IIS pack (deploy/iis): well-formed files that agree with each other and the code, and scripts that run."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from oversight.settings import PROJECT_ROOT, Settings

IIS = PROJECT_ROOT / "deploy" / "iis"
INSTALLER = IIS / "Install-JiraOversight.ps1"
TASK = IIS / "Invoke-OversightTask.ps1"
PUBLISH = IIS / "Publish-Dashboard.ps1"
API_CONFIG = IIS / "api" / "web.config"
DASHBOARD_CONFIG = IIS / "dashboard" / "web.config"
DASHBOARD = PROJECT_ROOT.parent / "jira-oversight-ui"
# The guide is git-ignored: a fresh clone has none, so its text is not checked.
GUIDE = PROJECT_ROOT / "docs" / "DEPLOYMENT_IIS.md"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="needs Windows PowerShell")
PLACEHOLDER = re.compile(r"__[A-Z][A-Z_]*__")
SECRET_WORDS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "WEBHOOK")


def http_platform(path: Path) -> ET.Element:
    found = ET.parse(path).getroot().find("system.webServer/httpPlatform")
    assert found is not None
    return found


def environment(element: ET.Element) -> dict[str, str]:
    return {item.get("name", ""): item.get("value", "") for item in element.iter("environmentVariable")}


def test_the_api_site_runs_uvicorn_on_the_port_iis_hands_it() -> None:
    element = http_platform(API_CONFIG)
    assert element.get("processPath") == "__PYTHON__"
    assert element.get("arguments") == "-m oversight api --host 127.0.0.1 --port %HTTP_PLATFORM_PORT%"
    assert element.get("stdoutLogEnabled") == "true" and element.get("stdoutLogFile") == r"__API_LOGS__\stdout"
    assert element.get("processesPerApplication") == "1"
    assert environment(element)["PYTHONPATH"] == "__BACKEND__"
    assert environment(element)["PYTHONUNBUFFERED"] == "1"


def test_the_dashboard_site_runs_the_standalone_server_on_the_port_iis_hands_it() -> None:
    element = http_platform(DASHBOARD_CONFIG)
    assert element.get("processPath") == "__NODE__"
    assert element.get("arguments") == '--env-file="__DASHBOARD_ENV__" "__DASHBOARD_APP__\\server.js"'
    assert element.get("stdoutLogEnabled") == "true" and element.get("stdoutLogFile") == r"__DASHBOARD_LOGS__\stdout"
    assert element.get("processesPerApplication") == "1"
    variables = environment(element)
    assert variables["PORT"] == "%HTTP_PLATFORM_PORT%" and variables["HOSTNAME"] == "127.0.0.1"
    assert variables["OVERSIGHT_DATA"] == "live" and variables["AUTH_MODE"] == "atlassian"
    assert variables["OVERSIGHT_API_URL"] == "http://127.0.0.1:__API_PORT__"


def test_the_timeouts_let_ask_finish_and_the_api_end_first() -> None:
    api, dashboard = http_platform(API_CONFIG), http_platform(DASHBOARD_CONFIG)
    assert int(api.get("startupTimeLimit", "0")) >= 120
    assert int(dashboard.get("startupTimeLimit", "0")) >= 60

    def seconds(value: str | None) -> int:
        hours, minutes, secs = (int(part) for part in (value or "0:0:0").split(":"))
        return hours * 3600 + minutes * 60 + secs

    assert seconds(api.get("requestTimeout")) >= 120
    assert seconds(dashboard.get("requestTimeout")) > seconds(api.get("requestTimeout"))


@pytest.mark.parametrize("path", [API_CONFIG, DASHBOARD_CONFIG])
def test_the_sites_add_no_security_header_of_their_own_and_hide_the_banners(path: Path) -> None:
    root = ET.parse(path).getroot()
    headers = root.find("system.webServer/httpProtocol/customHeaders")
    assert headers is not None
    # A second copy of the security headers at IIS would fight the dashboard's and the API's.
    assert [item.tag for item in headers] == ["remove"] and headers[0].get("name") == "X-Powered-By"
    request_filtering = root.find("system.webServer/security/requestFiltering")
    assert request_filtering is not None and request_filtering.get("removeServerHeader") == "true"
    assert root.find("system.webServer/httpErrors").get("existingResponse") == "PassThrough"  # type: ignore[union-attr]
    text = path.read_text(encoding="utf-8")
    for header in ("Content-Security-Policy", "Strict-Transport-Security", "X-Frame-Options", "Referrer-Policy"):
        assert f'name="{header}"' not in text


@pytest.mark.parametrize("path", [API_CONFIG, DASHBOARD_CONFIG])
def test_no_secret_is_in_a_web_config(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name in re.findall(r'environmentVariable name="([^"]+)"', text):
        assert not any(word in name.upper() for word in SECRET_WORDS), name
    assert not re.search(r"(?i)(sk-ant|ATATT|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY)", text)


def test_http_goes_to_https_on_the_one_address() -> None:
    rule = ET.parse(DASHBOARD_CONFIG).getroot().find("system.webServer/rewrite/rules/rule")
    assert rule is not None
    action = rule.find("action")
    assert action is not None and action.get("type") == "Redirect" and action.get("redirectType") == "Permanent"
    # A fixed host, never the request's: no open redirect through the Host header.
    assert action.get("url") == "https://__HOST_NAME__/{R:1}"
    condition = rule.find("conditions/add")
    assert condition is not None and condition.get("input") == "{HTTPS}" and condition.get("pattern") == "^OFF$"
    # win-acme answers its http challenge itself: never redirected.
    acme = rule.findall("conditions/add")[1]
    assert acme.get("input") == "{REQUEST_URI}" and acme.get("negate") == "true"
    assert re.match(acme.get("pattern") or "", "/.well-known/acme-challenge/token")
    assert not re.match(acme.get("pattern") or "", "/findings")
    assert ET.parse(API_CONFIG).getroot().find("system.webServer/rewrite") is None


def test_the_installer_fills_every_placeholder_the_web_configs_hold() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    block = installer[installer.index("$values = @{") : installer.index("Write-WebConfig (Join-Path $here 'api")]
    filled = set(re.findall(r"^\s+([A-Z_]+) = ", block, re.MULTILINE))
    texts = [path.read_text(encoding="utf-8") for path in (API_CONFIG, DASHBOARD_CONFIG)]
    held = {name.strip("_") for text in texts for name in PLACEHOLDER.findall(text)}
    assert held and held <= filled, held - filled


def _template(path: Path) -> dict[str, str]:
    """A settings template's NAME=value lines (comments and blank lines left out)."""
    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, _, value = line.partition("=")
            found[name.strip()] = value.strip()
    return found


def test_the_iis_templates_fail_safe(tmp_path: Path) -> None:
    backend = _template(IIS / "backend.env.example")
    dashboard = _template(IIS / "dashboard.env.example")
    for name in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_PROJECTS", "API_SERVICE_KEY", "AWS_REGION",
                 "CORS_ALLOW_ORIGINS", "REPORT_TIMEZONE", "ANTHROPIC_API_KEY", "AWS_ACCESS_KEY_ID", "SMTP_HOST"):
        assert backend[name] == "", name
    assert (backend["LLM_MODE"], backend["DIGEST_CHANNEL"], backend["LOG_FORMAT"]) == ("stub", "file", "json")
    assert (backend["API_DOCS"], backend["API_ALLOW_INSECURE"]) == ("false", "false")
    assert (backend["API_HOST"], backend["API_PORT"]) == ("127.0.0.1", "3300")
    for name in ("APP_URL", "ALLOWED_EMAIL_DOMAINS", "AUTH_SECRET", "OVERSIGHT_SERVICE_KEY", "OVERSIGHT_JIRA_BASE_URL",
                 "ATLASSIAN_CLIENT_ID", "ATLASSIAN_CLIENT_SECRET", "ATLASSIAN_AUTH_URL", "ATLASSIAN_API_URL",
                 "SAMPLE_NOW", "SAMPLE_LATENCY", "SAMPLE_PEOPLE_COMPARISON"):
        assert dashboard[name] == "", name
    assert "ADMIN_EMAILS" not in dashboard and "ACCESS_MODE" not in dashboard
    for text in (str(backend), str(dashboard)):
        assert "@" not in text and "atlassian.net" not in text and "localhost" not in text
    fields = {name.upper() for name in Settings.model_fields}
    assert set(backend) <= fields, set(backend) - fields
    ui_template = DASHBOARD / ".env.example"
    if ui_template.exists():
        assert set(dashboard) <= set(_template(ui_template)), set(dashboard) - set(_template(ui_template))
    copy = tmp_path / "backend.env"
    copy.write_text((IIS / "backend.env.example").read_text(encoding="utf-8"), encoding="utf-8")
    loaded = Settings(_env_file=copy)
    assert loaded.llm_mode == "stub" and loaded.digest_channel == "file" and loaded.api_port == 3300


def test_the_dashboard_template_says_how_to_make_only_some_people_admins() -> None:
    text = (IIS / "dashboard.env.example").read_text(encoding="utf-8")
    assert "# ACCESS_MODE=admin-list" in text and "# ADMIN_EMAILS=" in text
    assert not any(line.startswith(("ACCESS_MODE", "ADMIN_EMAILS")) for line in text.splitlines())


def test_the_pack_is_ascii_and_lf_only() -> None:
    # Windows PowerShell 5.1 reads a script without a byte-order mark in the machine's code page: stay ASCII.
    for path in (INSTALLER, TASK, PUBLISH, API_CONFIG, DASHBOARD_CONFIG, IIS / "backend.env.example",
                 IIS / "dashboard.env.example", *([GUIDE] if GUIDE.exists() else [])):
        data = path.read_bytes()
        assert b"\r\n" not in data, path
        if path.suffix != ".md":
            assert data.isascii(), path


def test_the_docker_build_context_still_leaves_the_pack_out() -> None:
    assert "deploy" in (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()


def powershell(
    *arguments: str, env: dict[str, str] | None = None, timeout: int = 180
) -> subprocess.CompletedProcess[str]:
    assert POWERSHELL is not None
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *arguments],
        env=env or os.environ.copy(), capture_output=True, text=True, timeout=timeout, check=False,
    )


@needs_powershell
@pytest.mark.parametrize("script", [INSTALLER, TASK, PUBLISH], ids=lambda path: path.name)
def test_every_script_parses(script: Path) -> None:
    command = (
        "$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errors); if ($errors) {{ $errors | ForEach-Object {{ $_.Message }}; exit 1 }}"
    )
    done = powershell("-Command", command)
    assert done.returncode == 0, done.stdout + done.stderr


STUB = (
    "@echo off\r\n"
    "echo stub-python %*\r\n"
    'echo {"event": "stub_log", "command": "%3"} 1>&2\r\n'
    "exit /b %STUB_EXIT%\r\n"
)


def run_task(tmp_path: Path, task: str, *, exit_code: int = 0, prune: Path | None = None) -> tuple[int, list[str]]:
    """Run Invoke-OversightTask.ps1 with a stub interpreter; its exit code and the task's log."""
    stub = tmp_path / "py with spaces" / "python.cmd"
    stub.parent.mkdir(exist_ok=True)
    stub.write_text(STUB, encoding="ascii", newline="")
    logs = tmp_path / "log dir"
    options = ["-PruneDir", str(prune)] if prune else []
    done = powershell(
        "-File", str(TASK), "-Task", task, "-Python", str(stub), "-LogDir", str(logs), *options,
        "-LockName", lock_name(tmp_path),
        env=os.environ | {"STUB_EXIT": str(exit_code)},
    )
    (log,) = logs.glob(f"task_{task.lower()}_*.log")
    assert done.returncode == exit_code, done.stdout + done.stderr
    return done.returncode, log.read_text(encoding="utf-8").splitlines()


def lock_name(tmp_path: Path) -> str:
    """A lock of this test's own, so another running cycle never holds it up."""
    return f"Local\\jira-oversight-test-{abs(hash(str(tmp_path)))}"


def ran(lines: list[str]) -> list[str]:
    """The oversight commands the stub ran, in order."""
    return [line.removeprefix("stub-python -m oversight ") for line in lines if line.startswith("stub-python")]


@needs_powershell
@pytest.mark.parametrize(("task", "command"), [
    ("Cycle", "cycle"), ("FullCycle", "cycle --full"), ("Backup", "backup"),
])
def test_each_job_runs_its_command_and_logs_it(tmp_path: Path, task: str, command: str) -> None:
    code, lines = run_task(tmp_path, task)
    assert code == 0 and ran(lines) == [command]
    assert any("start: python -m oversight " + command in line for line in lines)
    assert any("(exit 0," in line for line in lines)
    assert any(line.startswith('{"event": "stub_log"') for line in lines)


@needs_powershell
@pytest.mark.parametrize("task", ["Cycle", "FullCycle", "Backup"])
def test_a_failing_command_fails_the_job_with_its_own_exit_code(tmp_path: Path, task: str) -> None:
    code, lines = run_task(tmp_path, task, exit_code=3)
    assert code == 3
    assert any("FAILED (exit 3)" in line for line in lines)


@needs_powershell
def test_the_backup_job_deletes_old_logs_and_only_log_files(tmp_path: Path) -> None:
    web_logs = tmp_path / "web logs"
    web_logs.mkdir()
    logs = tmp_path / "log dir"
    logs.mkdir()
    old = time.time() - 40 * 86400
    kept = [web_logs / "stdout_1_recent.log", web_logs / "notes.txt", logs / "other.txt"]
    gone = [web_logs / "stdout_1_old.log", logs / "task_cycle_2026-10-01.log"]
    for path in [*kept, *gone]:
        path.write_text("x", encoding="utf-8")
    for path in [web_logs / "notes.txt", logs / "other.txt", *gone]:
        os.utime(path, (old, old))
    run_task(tmp_path, "Backup", prune=web_logs)
    assert all(path.exists() for path in kept) and not any(path.exists() for path in gone)


@needs_powershell
def test_a_cycle_that_cannot_take_the_lock_is_skipped_not_failed(tmp_path: Path) -> None:
    holder = subprocess.Popen(
        [POWERSHELL or "", "-NoProfile", "-Command",
         f"$m = New-Object System.Threading.Mutex($false, '{lock_name(tmp_path)}'); [void]$m.WaitOne(); "
         "[Console]::Out.WriteLine('held'); [Console]::Out.Flush(); Start-Sleep -Seconds 60"],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        stub = tmp_path / "python.cmd"
        stub.write_text(STUB, encoding="ascii", newline="")
        logs = tmp_path / "logs"
        done = powershell("-File", str(TASK), "-Task", "Cycle", "-Python", str(stub), "-LogDir", str(logs),
                          "-WaitMinutes", "0", "-LockName", lock_name(tmp_path), env=os.environ | {"STUB_EXIT": "5"})
        (log,) = logs.glob("task_cycle_*.log")
        text = log.read_text(encoding="utf-8")
        assert done.returncode == 0 and "skipped: another cycle is still running" in text and "stub-python" not in text
    finally:
        holder.kill()
        holder.wait()


@needs_powershell
def test_the_installer_with_whatif_prints_the_plan_and_changes_nothing(tmp_path: Path) -> None:
    backend = tmp_path / "install" / "backend"
    (backend / "oversight").mkdir(parents=True)
    (backend / "oversight" / "__main__.py").write_text("", encoding="utf-8")
    before = sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*"))
    done = powershell(
        "-File", str(INSTALLER), "-HostName", "Oversight.Example.com",
        "-CertificateThumbprint", "01 23 45 67 89 AB CD EF 01 23 45 67 89 AB CD EF 01 23 45 67",
        "-BackendPath", str(backend), "-TaskUser", r"CONTOSO\svc-oversight$", "-WhatIf",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "WhatIf: nothing below is changed." in out and "address: https://oversight.example.com" in out
    for planned in (
        'create the app pool" on target "JiraOversightApi', 'create the app pool" on target "JiraOversightDashboard',
        "set managedRuntimeVersion to ''", "set startMode to 'AlwaysRunning'",
        'create the site on 127.0.0.1:3300:" on target "Jira Oversight API',
        "add the https binding *:443:oversight.example.com",
        "bind the certificate 0123456789ABCDEF0123456789ABCDEF01234567",
        "write web.config", "create it from backend.env.example", "create it from dashboard.env.example",
        r"grant IIS AppPool\JiraOversightApi change", r"grant CONTOSO\svc-oversight$ change",
        r'register the scheduled job (as CONTOSO\svc-oversight$)" on target "\JiraOversight\Sync',
        r'target "\JiraOversight\Full sync', r'target "\JiraOversight\Backup',
    ):
        assert planned in out, planned
    assert "(WhatIf, nothing was changed)" in out and "would be made" in out
    assert sorted(path.relative_to(tmp_path).as_posix() for path in tmp_path.rglob("*")) == before


@needs_powershell
def test_the_installer_without_a_certificate_leaves_https_to_win_acme(tmp_path: Path) -> None:
    backend = tmp_path / "install" / "backend"
    (backend / "oversight").mkdir(parents=True)
    (backend / "oversight" / "__main__.py").write_text("", encoding="utf-8")
    done = powershell("-File", str(INSTALLER), "-HostName", "oversight.example.com", "-BackendPath", str(backend),
                      "-WhatIf")
    assert done.returncode == 0, done.stdout + done.stderr
    out = done.stdout
    assert "No -CertificateThumbprint: the site gets http on oversight.example.com only" in out
    assert "*:80:oversight.example.com" in out
    assert "add the https binding" not in out and "bind the certificate" not in out


@needs_powershell
def test_the_installer_refuses_what_it_cannot_use(tmp_path: Path) -> None:
    for update, said in (
        (["-HostName", "not a host"], "-HostName must be a host name"),
        (["-CertificateThumbprint", "1234"], "40-character thumbprint"),
        (["-BackupAt", "25:00"], "HH:mm"),
        (["-BackendPath", str(tmp_path)], "No backend at"),
    ):
        arguments = {"-HostName": "oversight.example.com", "-CertificateThumbprint": "AB" * 20,
                     "-BackendPath": str(PROJECT_ROOT)}
        arguments.update(dict(zip(update[::2], update[1::2], strict=True)))
        done = powershell("-File", str(INSTALLER), *[part for pair in arguments.items() for part in pair], "-WhatIf")
        assert done.returncode != 0 and said in done.stdout + done.stderr, said


@needs_powershell
def test_the_publisher_with_whatif_changes_nothing(tmp_path: Path) -> None:
    source = tmp_path / "ui"
    source.mkdir()
    (source / "package.json").write_text("{}", encoding="utf-8")
    destination = tmp_path / "dashboard"
    done = powershell("-File", str(PUBLISH), "-SourcePath", str(source), "-DestinationPath", str(destination),
                      "-WhatIf")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "npm ci --ignore-scripts" in done.stdout
    assert "npm run build (standalone output)" in done.stdout and not destination.exists()
    inside = powershell("-File", str(PUBLISH), "-SourcePath", str(source), "-DestinationPath", str(source / "out"),
                        "-WhatIf")
    assert inside.returncode != 0 and "outside the source folder" in inside.stdout + inside.stderr


@pytest.mark.skipif(not (DASHBOARD / "next.config.ts").exists(), reason="the dashboard's folder is not beside this one")
def test_the_dashboard_builds_standalone_when_asked_and_the_publisher_asks() -> None:
    config = (DASHBOARD / "next.config.ts").read_text(encoding="utf-8")
    assert 'process.env.STANDALONE_OUTPUT === "1" ? "standalone"' in config
    assert "$env:STANDALONE_OUTPUT = '1'" in PUBLISH.read_text(encoding="utf-8")


@pytest.mark.skipif(not GUIDE.exists(), reason="docs/DEPLOYMENT_IIS.md is local only, not on this machine")
def test_the_guide_covers_the_pack_and_every_section_the_files_cite() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    headings = {int(number): title for number, title in re.findall(r"^## (\d+)\. (.+)$", guide, re.MULTILINE)}
    assert list(headings) == list(range(1, len(headings) + 1))
    for number, title in {3: "Prerequisites", 4: "Put the code in place", 7: "Settings and secrets", 8: "Install",
                          9: "HTTPS", 10: "First start", 11: "Updating", 12: "Backups"}.items():
        assert headings[number].startswith(title), number
    for path in (INSTALLER, TASK, PUBLISH, API_CONFIG, DASHBOARD_CONFIG, IIS / "backend.env.example",
                 IIS / "dashboard.env.example"):
        for cited in re.findall(r"section (\d+)", path.read_text(encoding="utf-8")):
            assert int(cited) in headings, f"{path.name}: section {cited}"
    block = INSTALLER.read_text(encoding="utf-8").split("param(", 1)[1].split("\n)\n", 1)[0]
    for parameter in re.findall(r"\$([A-Za-z]+)(?: =|,|$)", block, re.MULTILINE):
        assert f"-{parameter}" in guide, parameter
    for needed in ("Install-JiraOversight.ps1", "Invoke-OversightTask.ps1", "Publish-Dashboard.ps1", "-WhatIf",
                   "HttpPlatformHandler", "URL Rewrite", "Node.js 22", "Python 3.13", "read:jira-work",
                   "Enable sharing", "callback URL", "dedicated read-only", "doctor --production",
                   "oversight.healthcheck", "Disable-ScheduledTask", "Rolling back", "BACKUP_KEEP", "stdout_", "127.0.0.1:3300"):
        assert needed in guide, needed
