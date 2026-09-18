import copy
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
import metabase_sync as sync

LEGACY_SQL_SHA256 = [
    "b0fb457b19502d60b17d9b9c0ace6f6c273c6f46d631ed9e957a5d6396789adf",
    "1e3b044cc99a486bac828902c1d8d73f1352b097fa35651f1cfec7fa3b690d51",
    "7c8495cde39f3c8dcad4c3d2966b1dfe67417b9dcb0f5b68b6f5d4aec2c60a31",
    "a957c053143d34eb448eae02edd18ad02655c601fa77bdb657c6d5d47ea9a158",
    "89f34b349eface354572ef29b8b3935e3369c3e52415859b01fd1ebfb2bc1c74",
    "b3cb1a57b1fa5475cc8f3a78e3247c2ea8f0161b7381e97e0d638ad95d902223",
    "893fa76ec9425f73551d6fa1d51c5be3a9fa9dfe11896ec705d7a260d6db8886",
    "edbc01703d1fb6eb2e888d7333ba90149a6ddd38bd039634e4f7df39e526d5e6",
    "6edb18da5c9d335497de22340c46ec7788d750e516fed79fe5838122eaebc75c",
    "aeac64378d8ace26d1bbf73310c0520e33b66f3fb64a31181fd426ed69e0cc5f",
    "ca1e18a4a0472cf02a8bb40084035e52c77f198f872e50fed5dcf70ab31ce47e",
    "2d1673e1b4fee78fd1a8ad51d4d86aa5561786c7e6643705c816e74a71fc9bf2",
    "e9ab4f58e380ca015b2def0ef5df354dca281d9234b0a05d144a1f2e1d5fb977",
    "c38f779fc2c6e954c9c922adb54dafe5816e8d391c846df6f344230f2ed5e190",
    "d650d68a09b2209be3d0f0721032192f9f76d0c7ec25bc110ed0ef8b66befd88",
    "8e810a620c46e00f0865c9daffb528a2908fbab9f40c115db057a51433d0249f",
    "c7b063a4f4ef41ce3c3350dfadf6e7f0e7eb188736f4702a847a31f85b5ceebc",
    "09bf054b8dd6dc4020fb183f9d200bdb17abd7045a38b163ad6ef1ad6d87190a",
    "43c31ad96ec40fd4aef341c9ff20ffbb0c87956487be57c0cfcb7cfe1478f6f2",
    "995f42a662f7635e32d29733708a33dbb48547355cee6c262ac6f20c2398603e",
    "fe371477cb376f65b1d7eb97a71ac8bc4dffccf29647c59c8bebf9a90d098220",
    "7528ee75d1c0a9c7104448e7801a14dce70ccdcac3d9ecdf98959583ba3a6fca",
    "3c9149e8a06e608796f509d0a0c527a77c276e3d668e0e4e01265504eebff8b3",
    "5def0af95e4c061a9097369829a6583ea89a1721eff24ec433486816e9bac373",
]
LEGACY_DASHBOARD_SHA256 = {
    "executive": "545520c0157b6f89e58b108657f9af1e91d594c22dfed5664b1cb20a3fd0c0d8",
    "store": "7986fc0ce5927f95a079707b1178b877569b4841a4407e5325fa8bcbf49f7f1a",
    "reviews": "a2710ef081755110254f3d6c3d55a15d99d94ffb31035ebfc2350ba16f6d7df9",
    "sentiment": "616cbc643452fef07041cd6572b63fb5beb2d5fdeee405c91d6bb647ac9b67fc",
    "network": "e47e99b11ab9e6abe2c28e1226b663285db74bbb895205705a1df0da9c9eec20",
    "experience": "4c983a34230df97098c95d741df303ddb3651c5f84852ab76e9298690417ffa5",
    "release": "b7fdfa0dc1818b1cae6b198280778154d5a88dc438c254daf726b4a50fc0f1c6",
}


def test_existing_q01_q24_and_dashboard_definitions_are_unchanged():
    manifest = sync.load_manifest()
    assert [
        hashlib.sha256((BI / q["sql"]).read_text().replace("\r\n", "\n").encode()).hexdigest()
        for q in manifest["questions"][:24]
    ] == LEGACY_SQL_SHA256
    actual = {
        d["key"]: hashlib.sha256(
            json.dumps(d, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        for d in manifest["dashboards"]
    }
    assert actual == LEGACY_DASHBOARD_SHA256


class FakeAPI:
    """Contract-shaped fake, NOT a claim of real Metabase end-to-end compatibility."""

    def __init__(self, version="v0.63.18", sentiment=True, network=True):
        self.version = version
        self.sentiment = sentiment
        self.network = network
        self.db = True
        self.fail = None
        self.denied = False
        self.unsafe = False
        self.experiment_rows = []
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
            if query.startswith("SELECT EXISTS") and "pg_attribute" in query:
                return {
                    "data": {
                        "rows": [
                            [
                                (
                                    self.sentiment
                                    if "sentiment_" in query
                                    else self.network
                                    if "network_" in query
                                    else True
                                )
                                for _ in re.findall("EXISTS\\(", query)
                            ]
                        ]
                    }
                }
            if query.startswith("SELECT has_column_privilege"):
                value = (
                    self.sentiment
                    if "sentiment_" in query
                    else self.network
                    if "network_" in query
                    else True
                )
                return {"data": {"rows": [[value for _ in query.split(",")]]}}
            if "SELECT EXISTS(SELECT 1 FROM public.review_observations" in query:
                return {"data": {"rows": [[self.sentiment]]}}
            if query == "SELECT count(*)::bigint FROM public.network_captures":
                return {"data": {"rows": [[len(self.experiment_rows)]]}}
            if query.startswith("SELECT nc.id::text"):
                return {"data": {"rows": copy.deepcopy(self.experiment_rows)}}
            if query.startswith("SELECT * FROM ("):
                return {"data": {"rows": []}}
            raise AssertionError("Unexpected SQL " + query[:75])
        m = re.fullmatch(
            r"/api/(collection|card|dashboard)(?:/(\d+))?(?:\?legacy-mbql=true)?", path
        )
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


def run_sync(api, path, apply, experiment=None, release=None):
    m = sync.load_manifest()
    sync.validate_contract(api, m["metabase_version"])
    experiment = experiment or sync.validate_experiment()
    release = release or sync.validate_release()
    dbid, sent = sync.check_source(api, m, experiment, release)
    instance = sync.Sync(api, m, sync.read_state(path), path, apply, (experiment, release, sent))
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
        and len(api.data["card"]) == 42
        and len(api.data["dashboard"]) == 7
    )
    assert sum(len(d["dashcards"]) for d in api.data["dashboard"].values()) == 45
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
    assert len(api.data["card"]) == len(sync.load_manifest()["questions"]) + 1


def test_missing_database_schema_gating_bad_credentials_and_version(tmp_path):
    api = FakeAPI()
    api.db = False
    with pytest.raises(sync.SyncError, match="Exact source database"):
        run_sync(api, tmp_path / "state", True)
    assert not api.data["card"]
    api = FakeAPI(sentiment=False)
    actions = run_sync(api, tmp_path / "state", True)
    assert len(api.data["card"]) == 37 and len(api.data["dashboard"]) == 6
    assert ("SKIP_CAPABILITY", "dashboard:sentiment") in actions
    assert len(api.data["dashboard"][next(iter(api.data["dashboard"]))]["dashcards"]) > 0
    api = FakeAPI(network=False)
    actions = run_sync(api, tmp_path / "network-state", True)
    assert len(api.data["card"]) == 30  # q26-q36 and q38 are withheld; readiness/base remain
    network_dashboard = next(
        d for d in api.data["dashboard"].values() if d["name"] == "Network Benchmark"
    )
    assert len(network_dashboard["dashcards"]) == 1
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


def test_network_capability_removal_preserves_existing_cards(tmp_path):
    api = FakeAPI(network=True)
    state = tmp_path / "state"
    run_sync(api, state, True)
    dashboard = next(d for d in api.data["dashboard"].values() if d["name"] == "Network Benchmark")
    old_ids = [x["card_id"] for x in dashboard["dashcards"]]
    api.network = False
    actions = run_sync(api, state, True)
    assert ("SKIP_CAPABILITY_PRESERVE", "dashboard:network") in actions
    assert [x["card_id"] for x in dashboard["dashcards"]] == old_ids


def test_private_manifests_compile_and_gate_against_source(tmp_path):
    from metadata import validate_experiment, validate_release

    experiment = validate_experiment(BI / "tests" / "fixtures" / "experiment_manifest.json")
    release = validate_release(BI / "tests" / "fixtures" / "release_manifest.json")
    private_note = "PRIVATE VALIDATION NOTE MUST NOT BE SAVED"
    experiment.records[0]["validation_notes"] = private_note
    release.records[0]["notes"] = "PRIVATE RELEASE NOTE MUST NOT BE SAVED"
    api = FakeAPI()
    api.experiment_rows = [
        [
            row["capture_id"],
            row["application_package"],
            row["scenario"],
            row["test_file_size_bytes"],
            "analyzed",
            True,
            True,
            True,
            0,
            800.0,
            10.0,
        ]
        for row in experiment.records
    ]
    state = tmp_path / "state"
    run_sync(api, state, True, experiment, release)
    q25 = next(card for card in api.data["card"].values() if "question:q25" in card["description"])
    saved_sql = q25["dataset_query"]["native"]["query"]
    assert experiment.records[0]["capture_id"] in saved_sql
    assert private_note not in saved_sql
    assert experiment.records[0]["test_file_sha256"] not in saved_sql
    q37 = next(card for card in api.data["card"].values() if "question:q37" in card["description"])
    assert release.records[0]["release_event_id"] in q37["dataset_query"]["native"]["query"]
    assert "PRIVATE RELEASE NOTE MUST NOT BE SAVED" not in q37["dataset_query"]["native"]["query"]
    state_text = state.read_text()
    assert experiment.records[0]["capture_id"] in state_text
    assert private_note not in state_text
