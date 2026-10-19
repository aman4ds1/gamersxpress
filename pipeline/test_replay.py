import datetime as dt
import json
from pathlib import Path

import pytest

import dryrun
import providers
import replay
from providers import Generation

NOW = dt.datetime(2026, 10, 19, 12, tzinfo=dt.timezone.utc)
CLUSTER_ID = "b939ef7c7f26"

#: Twelve distinct words, one per synthetic claim, so no two claims merge.
WORDS = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
    "golf", "hotel", "india", "juliet", "kilo", "lima",
]


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """A test that writes a temporary config must not reuse a cached parse."""
    providers.clear_config_cache()
    yield
    providers.clear_config_cache()


def listing(root: Path) -> set[str]:
    """Every path under ``root``, relative to it, for an unchanged-listing check."""
    root = Path(root)
    if not root.is_dir():
        return set()
    return {str(path.relative_to(root)) for path in root.rglob("*")}


def write_gathered(data_dir: Path, cluster_id: str = CLUSTER_ID, gathered: dict | None = None) -> Path:
    directory = data_dir / "gathered"
    directory.mkdir(parents=True, exist_ok=True)
    payload = gathered if gathered is not None else dryrun.sample_gathered(cluster_id, NOW)
    path = directory / f"{cluster_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_sheet(
    data_dir: Path,
    cluster_id: str = CLUSTER_ID,
    title: str = dryrun.SAMPLE_TITLE,
    entities: tuple[str, ...] = ("Nvidia",),
) -> Path:
    directory = data_dir / "facts" / "unconfirmed"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": cluster_id,
        "title": title,
        "coherence": {
            "checked": True,
            "primary_entities": [entity.lower() for entity in entities],
            "canonical_entities": list(entities),
        },
        "confirmation": {"rule": "tier1", "confirmed": True, "reason": "confirmed"},
    }
    path = directory / f"{cluster_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def is_grouping_call(json_schema) -> bool:
    return isinstance(json_schema, dict) and "groups" in (json_schema.get("properties") or {})


def many_claim_gathered(cluster_id: str = CLUSTER_ID) -> dict:
    """One source whose body holds every word the synthetic claims cite."""
    text = "nvidia " + " ".join(WORDS) + "."
    return {
        "id": cluster_id,
        "gathered_at": NOW.isoformat(),
        "sources": [
            {
                "link": "https://a.example/1",
                "title": "Nvidia update roundup",
                "source_name": "Alpha",
                "tier": 2,
                "owner": "owner-a",
                "region": "us",
                "status": "ok",
                "text": text,
                "error": None,
            }
        ],
    }


def many_claims_generate(count: int = len(WORDS)):
    """A stub model returning ``count`` distinct grounded claims."""
    payload = {
        "claims": [
            {
                "claim": f"nvidia reported {word} developments",
                "value": f"the {word} development",
                "source_url": "https://a.example/1",
                "confidence": 0.5,
                "origin": "unstated",
                "affiliation": "",
                "kind": "reported",
                "quote_ids": [],
                "attribution_ids": [],
                "speaker": "",
                "speaker_type": "none",
            }
            for word in WORDS[:count]
        ]
    }

    def generate(role, prompt, json_schema=None, *, run_state=None, **kwargs):
        value = {"groups": []} if is_grouping_call(json_schema) else payload
        generation = Generation(role=role, provider="mock", model="m", family="f", value=value)
        if run_state is not None:
            run_state.record(generation)
        return generation

    return generate


# --- replay report -----------------------------------------------------------


def test_replay_loads_gathered_and_sheet_title_and_prints_fields(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    report = replay.run(CLUSTER_ID, generate=dryrun.make_generate("pass"), now=NOW)

    assert report["cluster_id"] == CLUSTER_ID
    assert report["title"] == dryrun.SAMPLE_TITLE
    assert report["entities"] == ["Nvidia"]
    assert report["model_calls"] >= 2

    confirmation = report["confirmation"]
    assert set(confirmation) == {"rule", "confirmed", "reason"}
    assert confirmation["confirmed"] is True
    assert isinstance(confirmation["rule"], str) and confirmation["rule"]

    assert report["fact_groups"]["accepted"] >= 1
    assert report["fact_groups"]["dropped"] >= 0

    assert report["claims"]
    first = report["claims"][0]
    assert set(first) >= {"label", "kind", "central", "evidence"}

    printed = replay.render_report(report)
    assert dryrun.SAMPLE_TITLE in printed
    assert "model calls:" in printed
    assert "confirmation:" in printed
    assert "fact_groups: accepted=" in printed
    assert "claim 1:" in printed


def test_replay_main_prints_the_report(monkeypatch, capsys):
    report = {
        "cluster_id": "abc",
        "title": "A title",
        "entities": ["Nvidia"],
        "model_calls": 3,
        "confirmation": {"rule": "tier1", "confirmed": True, "reason": "confirmed"},
        "fact_groups": {"accepted": 2, "dropped": 1},
        "claims": [
            {
                "label": "Nvidia confirmed it",
                "kind": "confirmed",
                "central": True,
                "evidence": [
                    {
                        "ok": True,
                        "reason": None,
                        "speaker": "Nvidia",
                        "attribution_mode": "explicit",
                        "attribution_quote": "says nvidia",
                    }
                ],
            }
        ],
        "claims_total": 1,
        "sheet_path": "/tmp/abc.json",
    }
    monkeypatch.setattr(replay, "run", lambda *args, **kwargs: report)

    code = replay.main(["abc"])
    captured = capsys.readouterr()

    assert code == 0
    assert "model calls: 3" in captured.out
    assert "confirmation: rule=tier1 confirmed=True reason=confirmed" in captured.out
    assert "fact_groups: accepted=2 dropped=1" in captured.out
    assert "claim 1:" in captured.out
    assert "attribution_mode=explicit" in captured.out


# --- isolation: no writes to the data tree, no breaker -----------------------


def test_replay_writes_nothing_under_the_data_folder(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    real_data = providers.PROJECT_ROOT / "data"
    real_before = listing(real_data)
    data_before = listing(data_dir)

    replay.run(
        CLUSTER_ID,
        generate=dryrun.make_generate("pass"),
        now=NOW,
        output_dir=tmp_path / "out",
    )

    assert listing(real_data) == real_before
    assert listing(data_dir) == data_before


def test_replay_never_touches_the_breaker(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_gathered(data_dir)
    write_sheet(data_dir)
    breaker = data_dir / "breaker.json"
    breaker.write_text('{"consecutive_failures": 2, "open": false}\n', encoding="utf-8")
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    real_breaker = providers.PROJECT_ROOT / "data" / "breaker.json"
    real_before = real_breaker.read_bytes() if real_breaker.is_file() else None
    before = breaker.read_bytes()

    output_dir = tmp_path / "out"
    replay.run(
        CLUSTER_ID,
        generate=dryrun.make_generate("pass"),
        now=NOW,
        output_dir=output_dir,
    )

    assert breaker.read_bytes() == before
    assert not (output_dir / "breaker.json").exists()
    real_after = real_breaker.read_bytes() if real_breaker.is_file() else None
    assert real_after == real_before


# --- clear failures ----------------------------------------------------------


def test_replay_missing_cluster_id_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "DATA_DIR", tmp_path / "data")

    with pytest.raises(replay.ReplayError) as excinfo:
        replay.run("does-not-exist", generate=dryrun.make_generate("pass"), now=NOW)

    message = str(excinfo.value)
    assert "does-not-exist" in message
    assert "gathered" in message


def test_replay_missing_gathered_file_fails(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    write_sheet(data_dir)
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    with pytest.raises(replay.ReplayError) as excinfo:
        replay.run(CLUSTER_ID, generate=dryrun.make_generate("pass"), now=NOW)

    message = str(excinfo.value)
    assert CLUSTER_ID in message
    assert "gathered" in message


def test_replay_main_reports_a_missing_gathered_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(replay, "DATA_DIR", tmp_path / "data")

    code = replay.main([CLUSTER_ID])
    captured = capsys.readouterr()

    assert code != 0
    assert "replay failed" in captured.err
    assert "gathered" in captured.err


# --- claim limit -------------------------------------------------------------


def test_replay_prints_at_most_eight_claims(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    cluster_id = "many-claims"
    write_gathered(data_dir, cluster_id=cluster_id, gathered=many_claim_gathered(cluster_id))
    write_sheet(
        data_dir,
        cluster_id=cluster_id,
        title="Nvidia update roundup",
        entities=("Nvidia",),
    )
    monkeypatch.setattr(replay, "DATA_DIR", data_dir)

    report = replay.run(cluster_id, generate=many_claims_generate(), now=NOW)

    assert report["claims_total"] >= 9
    assert len(report["claims"]) == replay.CLAIM_REPORT_LIMIT == 8

    printed = replay.render_report(report)
    assert "claim 8:" in printed
    assert "claim 9:" not in printed
