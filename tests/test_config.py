from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from oversight.config import CUSTOM_TRACKED, SYSTEM_TRACKED, FieldsConfig, ProcessConfig
from oversight.errors import ConfigError
from oversight.settings import PROJECT_ROOT

from tests.conftest import CONFIG_FIXTURES


def test_real_config_files_load() -> None:
    process = ProcessConfig.load(PROJECT_ROOT / "config" / "process.yaml")
    fields = FieldsConfig.load(PROJECT_ROOT / "config" / "fields.yaml")
    assert "AIL" in process.projects
    assert fields.id_of("story_points") == "customfield_10016"


def test_stage_lookup_is_case_insensitive(process: ProcessConfig) -> None:
    assert process.stage_of("AIL", "in progress") == "development"
    assert process.stage_of("AIL", "DONE") == "done"
    assert process.stage_of("AIL", "In Review") is None
    assert process.stage_of("AIL", None) is None


def test_ordered_stages_only_include_mapped_stages(process: ProcessConfig) -> None:
    assert process.ordered_stages("AIL") == ["backlog", "development", "done"]


def test_work_stages_run_from_development_to_before_done(process: ProcessConfig) -> None:
    assert process.work_stages() == [
        "development",
        "code_review",
        "dev_signoff",
        "testing",
        "test_review",
        "test_signoff",
    ]


def test_missing_evidence_lists_null_sources(process: ProcessConfig) -> None:
    assert process.missing_evidence("AIL") == [
        "code_review",
        "dev_signoff",
        "testing",
        "test_review",
        "test_signoff",
    ]
    assert not process.has_evidence("AIL", "code_review")


def test_unknown_project_is_a_config_error(process: ProcessConfig) -> None:
    with pytest.raises(ConfigError, match="XYZ"):
        process.project("XYZ")


def _write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "process.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _base() -> dict:
    return yaml.safe_load((CONFIG_FIXTURES / "process.yaml").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["projects"]["AIL"]["status_to_stage"].update({"QA": "qa"}), "unknown stage"),
        (lambda d: d["projects"]["AIL"]["estimate"].update({"hours_per_point": 0}), "hours_per_point"),
        (lambda d: d["projects"]["AIL"]["estimate"].update({"basis": "hours"}), "original_estimate"),
        (lambda d: d["projects"]["AIL"]["evidence"].update({"qa": None}), "unknown stage"),
        (lambda d: d.update({"stages": ["backlog", "done", "done"]}), "unique"),
        (lambda d: d["projects"]["AIL"]["status_to_stage"].update({"to do": "backlog"}), "ignoring case"),
        (lambda d: d.update({"start_stage": "done", "done_stage": "development"}), "before"),
        (lambda d: d.update({"surprise": 1}), "surprise"),
    ],
)
def test_invalid_process_configs_are_rejected(tmp_path: Path, mutate, message: str) -> None:
    data = _base()
    mutate(data)
    with pytest.raises(ConfigError, match=message):
        ProcessConfig.load(_write(tmp_path, data))


def test_missing_and_broken_files_are_config_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        ProcessConfig.load(tmp_path / "nope.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("stages: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        ProcessConfig.load(bad)


def test_unreadable_files_are_config_errors_not_tracebacks(tmp_path: Path) -> None:
    utf16 = tmp_path / "process.yaml"
    utf16.write_text((CONFIG_FIXTURES / "process.yaml").read_text(encoding="utf-8"), encoding="utf-16")
    with pytest.raises(ConfigError, match="not UTF-8"):
        ProcessConfig.load(utf16)
    with pytest.raises(ConfigError, match="cannot read"):
        FieldsConfig.load(tmp_path)  # a folder, not a file


def test_a_utf8_file_with_a_byte_order_mark_loads(tmp_path: Path) -> None:
    path = tmp_path / "process.yaml"
    path.write_text((CONFIG_FIXTURES / "process.yaml").read_text(encoding="utf-8"), encoding="utf-8-sig")
    assert ProcessConfig.load(path).estimate_for("AIL").basis == "points"


def test_missing_evidence_also_names_stages_left_out(tmp_path: Path) -> None:
    data = _base()
    evidence = data["projects"]["AIL"]["evidence"]
    del evidence["testing"]
    evidence["code_review"] = {"remote_link_contains": "pull-requests"}
    process = ProcessConfig.load(_write(tmp_path, data))
    assert process.missing_evidence("AIL") == ["dev_signoff", "testing", "test_review", "test_signoff"]
    assert not process.has_evidence("AIL", "testing")
    assert process.has_evidence("AIL", "code_review")


def test_stages_the_workflow_maps_need_no_evidence_entry(tmp_path: Path) -> None:
    data = _base()
    project = data["projects"]["AIL"]
    project["status_to_stage"].update({"Code Review": "code_review", "Testing": "testing"})
    project["evidence"] = {"test_signoff": None}
    process = ProcessConfig.load(_write(tmp_path, data))
    assert process.missing_evidence("AIL") == ["dev_signoff", "test_review", "test_signoff"]


def test_tracked_changelog_fields_fit_one_bulk_request(fields: FieldsConfig) -> None:
    tracked = fields.tracked_changelog_fields()
    assert tracked["status"] == "status"
    assert tracked["customfield_10016"] == "story_points"
    assert tracked["customfield_10020"] == "sprint"
    assert len(tracked) == len(SYSTEM_TRACKED) + len(CUSTOM_TRACKED) == 10


def test_search_fields_include_custom_ids_once(fields: FieldsConfig) -> None:
    wanted = fields.search_fields()
    assert wanted[:3] == ["project", "issuetype", "summary"]
    for field_id in ("customfield_10016", "customfield_10020", "customfield_10000", "customfield_10139"):
        assert wanted.count(field_id) == 1


def test_fields_round_trip_through_dump(tmp_path: Path, fields: FieldsConfig) -> None:
    path = tmp_path / "sub" / "fields.yaml"
    fields.dump(path)
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Generated by `python -m oversight doctor`")
    assert FieldsConfig.load(path) == fields
    assert FieldsConfig.load(path).id_of("epic_link") is None
    assert [p.name for p in path.parent.iterdir()] == ["fields.yaml"]  # no temporary file left


def test_a_people_field_copied_on_other_boards_is_requested_tracked_and_kept_under_each_id(
    tmp_path: Path, fields: FieldsConfig
) -> None:
    programmer = fields.fields["programmer"]
    assert programmer is not None
    copied = fields.model_copy(update={"fields": {
        **fields.fields, "programmer": programmer.model_copy(update={"also": ["customfield_20068", "customfield_20069"]})}})
    assert copied.ids_of("programmer") == (programmer.id, "customfield_20068", "customfield_20069")
    assert copied.id_of("programmer") == programmer.id
    wanted = copied.search_fields()
    assert [one for one in wanted if one not in fields.search_fields()] == ["customfield_20068", "customfield_20069"]
    assert len(wanted) == len(set(wanted)) == len(fields.search_fields()) + 2
    tracked = copied.tracked_changelog_fields()
    assert tracked["customfield_20068"] == tracked["customfield_20069"] == tracked[programmer.id] == "programmer"
    path = tmp_path / "fields.yaml"
    copied.dump(path)
    assert FieldsConfig.load(path) == copied
    # A field with no copies is written as before, with no empty list.
    assert path.read_text(encoding="utf-8").count("also:") == 1


def test_a_failed_dump_keeps_the_previous_file(
    tmp_path: Path, fields: FieldsConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "fields.yaml"
    path.write_text("previous\n", encoding="utf-8")

    def interrupted(_source: object, _target: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("oversight.config.os.replace", interrupted)
    with pytest.raises(OSError, match="disk full"):
        fields.dump(path)
    assert path.read_text(encoding="utf-8") == "previous\n"
    assert [p.name for p in tmp_path.iterdir()] == ["fields.yaml"]
