import copy
import json
import re
import sys
from pathlib import Path

import pytest

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
import metabase_sync as sync


class FakeAPI:
    """Contract-shaped fake, NOT a claim of real Metabase end-to-end compatibility."""

    def __init__(self, version="v0.63.18", sentiment=True):
        self.version = version
        self.sentiment = sentiment
        self.db = True
        self.fail = None
        self.denied = False
        self.unsafe = False
        self.data = {"collection": {}, "card": {}, "dashboard": {}}
        self.calls = []
        self.next = 10
        self.dc = 1000

    def call(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if self.denied:
            raise sync.SyncError("API authorization rejected (HTTP 401); key not shown")
        if self.fail == (method, path):
            self.fail = None
            raise sync.SyncError("Simulated partial failure at " + method + " " + path)
        if path == "/api/health":
            return {"status": "ok"}
        if path == "/api/session/properties":
            return {"version": {"tag": self.version}}
        if path == "/api/docs/openapi.json":
            p = {
                x: {m: {} for m in methods}
                for x, methods in {
                    "/api/collection": ("get", "post"),
                    "/api/collection/{id}": ("get",),
                    "/api/card": ("get", "post"),
                    "/api/card/{id}": ("get", "put"),
                    "/api/dashboard": ("get", "post"),
                    "/api/dashboard/{id}": ("get", "put"),
                    "/api/database": ("get",),
                    "/api/dataset": ("post",),
                }.items()
            }
            return {"paths": p}
        if path == "/api/database":
            return {
                "data": [{"name": "Sahabino BI Source", "id": 27}] if self.db else [],
                "total": 1 if self.db else 0,
            }
        if path == "/api/dataset":
            query = payload["native"]["query"]
            if query.startswith("SELECT current_user"):
                return {"data": {"rows": [["sahabino_bi_reader", "sahabino"]]}}
            if query.startswith("SELECT has_database_privilege"):
                from schema import SENSITIVE

                total = 8 + sum(len(v) for v in SENSITIVE.values())
                out = [False] * total + [True]
                if self.unsafe:
                    out[8] = True
                return {"data": {"rows": [out]}}
            if query.startswith("SELECT EXISTS") and "information_schema.columns" in query:
                return {
                    "data": {
                        "rows": [
                            [
                                self.sentiment if "sentiment_" in query else True
                                for _ in re.findall("EXISTS\\(", query)
                            ]
                        ]
                    }
                }
            if "SELECT EXISTS(SELECT 1 FROM public.review_observations" in query:
                return {"data": {"rows": [[self.sentiment]]}}
            if query.startswith("SELECT * FROM ("):
                return {"data": {"rows": []}}
            raise AssertionError("Unexpected SQL " + query[:75])
        m = re.fullmatch(r"/api/(collection|card|dashboard)(?:/(\d+))?", path)
        assert m, path
        kind, rid = m.group(1), m.group(2)
        if method == "GET" and rid is None:
            return copy.deepcopy(list(self.data[kind].values()))
        if method == "GET":
            return copy.deepcopy(self.data[kind][int(rid)])
        if method == "POST":
            self.next += 1
            id = self.next
            item = copy.deepcopy(payload)
            item["id"] = id
            if kind == "dashboard":
                item.setdefault("dashcards", [])
            self.data[kind][id] = item
            return copy.deepcopy(item)
        if method == "PUT":
            old = self.data[kind][int(rid)]
            new = copy.deepcopy(payload)
            if kind == "dashboard" and "dashcards" in new:
                for c in new["dashcards"]:
                    if c.get("id", -1) < 0:
                        self.dc += 1
                        c["id"] = self.dc
            old.update(new)
            return copy.deepcopy(old)
        raise AssertionError((method, path))


def run_sync(api, path, apply):
    m = sync.load_manifest()
    sync.validate_contract(api, m["metabase_version"])
    dbid, sent = sync.check_source(api, m)
    instance = sync.Sync(api, m, sync.read_state(path), path, apply)
    return instance.run(dbid, sent)


def test_empty_then_second_run_idempotent_card_mappings_and_positions(tmp_path):
    api = FakeAPI()
    state = tmp_path / "state.json"
    actions = run_sync(api, state, False)
    assert any(a[0] == "CREATE" for a in actions) and not state.exists()
    assert not api.data["card"] and not api.data["dashboard"]
    first = run_sync(api, state, True)
    assert (
        len(api.data["collection"]) == 3
        and len(api.data["card"]) == 24
        and len(api.data["dashboard"]) == 4
    )
    assert sum(len(d["dashcards"]) for d in api.data["dashboard"].values()) == 27
    for d in api.data["dashboard"].values():
        ids = [c["card_id"] for c in d["dashcards"]]
        assert len(ids) == len(set(ids))
        expected = {p["id"] for p in d["parameters"]}
        assert all(
            m["parameter_id"] in expected
            and m["card_id"] == c["card_id"]
            and m["target"][0] == "variable"
            for c in d["dashcards"]
            for m in c["parameter_mappings"]
        )
        assert all(c["col"] >= 0 and c["col"] + c["size_x"] <= 12 for c in d["dashcards"])
    previous = (len(api.data["collection"]), len(api.data["card"]), len(api.data["dashboard"]))
    second = run_sync(api, state, True)
    assert all(a[0] == "SKIP" for a in second)
    assert previous == (
        len(api.data["collection"]),
        len(api.data["card"]),
        len(api.data["dashboard"]),
    )
    assert state.is_file() and state.stat().st_mode & 0o077 == 0


def test_sql_change_only_question_update_and_unrelated_untouched(tmp_path, monkeypatch):
    api = FakeAPI()
    state = tmp_path / "state.json"
    run_sync(api, state, True)
    other = {
        "id": 999,
        "name": "My private unrelated card",
        "collection_id": 123,
        "description": "unrelated content",
    }
    api.data["card"][999] = copy.deepcopy(other)
    original = Path.read_text

    def change(path, *a, **kw):
        result = original(path, *a, **kw)
        if str(path).endswith("q02_store_rating_daily.sql"):
            return result.replace(
                "deterministic last successful",
                "deterministic last successful (controlled revision)",
            )
        return result

    monkeypatch.setattr(Path, "read_text", change)
    actions = run_sync(api, state, True)
    assert [x for x in actions if x[0] == "UPDATE"] == [("UPDATE", "question:q02")]
    assert api.data["card"][999] == other
    assert len(api.data["card"]) == 25


def test_missing_database_schema_gating_bad_credentials_and_version(tmp_path):
    api = FakeAPI()
    api.db = False
    with pytest.raises(sync.SyncError, match="Exact source database"):
        run_sync(api, tmp_path / "state", True)
    assert not api.data["card"]
    api = FakeAPI(sentiment=False)
    actions = run_sync(api, tmp_path / "state", True)
    assert len(api.data["card"]) == 19 and len(api.data["dashboard"]) == 3
    assert ("SKIP_CAPABILITY", "dashboard:sentiment") in actions
    assert len(api.data["dashboard"][next(iter(api.data["dashboard"]))]["dashcards"]) > 0
    api = FakeAPI()
    api.denied = True
    with pytest.raises(sync.SyncError, match="authorization"):
        run_sync(api, tmp_path / "bad", True)
    api = FakeAPI(version="v0.64.0")
    with pytest.raises(sync.SyncError, match="version mismatch"):
        run_sync(api, tmp_path / "bad", True)


def test_partial_failure_checkpoint_and_conflict(tmp_path):
    api = FakeAPI()
    path = tmp_path / "state"
    api.fail = ("POST", "/api/card")
    with pytest.raises(sync.SyncError, match="partial failure"):
        run_sync(api, path, True)
    saved = sync.read_state(path)["objects"]
    assert len(saved) == 3 and all(x.startswith("collection:") for x in saved)
    assert len(api.data["card"]) == 0
    run_sync(api, path, True)
    card = next(iter(api.data["card"].values()))
    card["description"] += " manual edit"
    with pytest.raises(sync.SyncError, match="Manual edit"):
        run_sync(api, path, True)


def test_unowned_name_not_adopted_and_invalid_manifest_rejected(tmp_path):
    api = FakeAPI()
    api.data["collection"][888] = {"id": 888, "name": "Sahabino BI", "description": "foreign"}
    with pytest.raises(sync.SyncError, match="Untracked"):
        run_sync(api, tmp_path / "state", True)
    m = sync.load_manifest()
    m["dashboards"][0]["cards"].append(copy.deepcopy(m["dashboards"][0]["cards"][0]))
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(m))
    with pytest.raises(sync.SyncError, match="duplicate"):
        sync.load_manifest(path)


def test_secret_and_redirect_protection(tmp_path):
    file = tmp_path / "key"
    file.write_text("example-private-key")
    file.chmod(0o644)
    with pytest.raises(sync.SyncError, match="chmod"):
        sync.secret(file)
    file.chmod(0o600)
    assert sync.secret(file) == "example-private-key"
    with pytest.raises(sync.SyncError, match="Only loopback"):
        sync.HTTPAPI("https://example.com", "secret")
    with pytest.raises(sync.SyncError, match="bare origin"):
        sync.HTTPAPI("http://127.0.0.1:3001/path", "secret")


def test_unsafe_source_sensitive_access_blocks_all_changes(tmp_path):
    api = FakeAPI()
    api.unsafe = True
    with pytest.raises(sync.SyncError, match="Source role unsafe"):
        run_sync(api, tmp_path / "state", True)
    assert not any(api.data[x] for x in ("collection", "card", "dashboard"))


def test_manual_archival_is_not_silently_reversed(tmp_path):
    api = FakeAPI()
    state = tmp_path / "state"
    run_sync(api, state, True)
    card = next(iter(api.data["card"].values()))
    card["archived"] = True
    with pytest.raises(sync.SyncError, match="manually archived"):
        run_sync(api, state, True)
    assert card["archived"] is True
