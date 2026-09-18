#!/usr/bin/env python3
"""Explicit Sahabino BI manifest -> Metabase OSS API synchronizer (plan is read-only)."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from urllib import error, parse, request

from metadata import MetadataError, render_sql, validate_experiment, validate_release

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest" / "content.json"
DEFAULT_STATE = ROOT / "secrets" / "metabase_sync_state.json"
MARK = "sahabino-bi:"
DML = re.compile(
    r"\b(?:ALTER|ANALYZE|CALL|COPY|CREATE|DELETE|DO|DROP|EXECUTE|GRANT|INSERT|MERGE|REVOKE|TRUNCATE|UPDATE|VACUUM)\b",
    re.I,
)
BLOCK = re.compile(r"\[\[(.*?)\]\]", re.S)
TAG = re.compile(r"\{\{([a-z_]+)\}\}")
CAPABILITIES = (
    "base",
    "sentiment",
    "network_readiness",
    "network",
    "cross_domain",
    "release_readiness",
    "release_base",
    "release_network",
)


class SyncError(Exception):
    pass


def hash_value(v):
    return hashlib.sha256(
        json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def load_manifest(path=MANIFEST):
    m = json.loads(Path(path).read_text())
    if (
        m.get("schema_version") != 1
        or m.get("metabase_version") != "v0.63.18"
        or not m.get("database_name")
    ):
        raise SyncError("Manifest version/database mismatch")
    objects = [(typ, item) for typ in ("collections", "questions", "dashboards") for item in m[typ]]
    keys = {t: set() for t in ("collections", "questions", "dashboards")}
    for t, item in objects:
        if not item.get("key") or not item.get("name") or item["key"] in keys[t]:
            raise SyncError("Duplicate or missing manifest logical key/name")
        keys[t].add(item["key"])
    for c in m["collections"]:
        if c["parent"] is not None and c["parent"] not in keys["collections"]:
            raise SyncError("Missing parent collection")
    for q in m["questions"]:
        if q["collection"] not in keys["collections"] or q["capability"] not in CAPABILITIES:
            raise SyncError("Invalid question collection/capability")
        path = (ROOT / q["sql"]).resolve()
        if not path.is_relative_to((ROOT / "questions").resolve()) or not path.is_file():
            raise SyncError("Question SQL path escapes questions/ or absent")
        sql = render_sql(
            path.read_text(), validate_experiment(), validate_release(), {}
        )
        raw_sql = render_unfiltered(sql)
        clean = re.sub(r"/\*.*?\*/|--[^\n]*", " ", raw_sql, flags=re.S)
        if (
            not clean.strip().upper().startswith(("SELECT", "WITH"))
            or DML.search(clean)
            or not clean.strip().endswith(";")
            or ";" in clean.strip()[:-1]
        ):
            raise SyncError("Unsafe SQL shape in " + q["key"])
        actual = set(TAG.findall(sql))
        if actual != set(q.get("parameters", {})) or not all(
            v in ("text", "date") for v in q["parameters"].values()
        ):
            raise SyncError("Manifest SQL template tag mismatch in " + q["key"])
    for d in m["dashboards"]:
        if d["collection"] not in keys["collections"] or d["capability"] not in CAPABILITIES:
            raise SyncError("Invalid dashboard collection/capability")
        cards = d["cards"]
        ids = [c["question"] for c in cards]
        if len(ids) != len(set(ids)) or any(k not in keys["questions"] for k in ids):
            raise SyncError("Unknown/duplicate dashboard card")
        for card in cards:
            if (
                any(type(card[v]) is not int for v in ("row", "col", "size_x", "size_y"))
                or card["row"] < 0
                or card["col"] < 0
                or card["size_x"] < 1
                or card["size_y"] < 1
                or card["col"] + card["size_x"] > 12
            ):
                raise SyncError("Invalid dashboard layout")
        for i, a in enumerate(cards):
            for b in cards[i + 1 :]:
                if (
                    a["row"] < b["row"] + b["size_y"]
                    and b["row"] < a["row"] + a["size_y"]
                    and a["col"] < b["col"] + b["size_x"]
                    and b["col"] < a["col"] + a["size_x"]
                ):
                    raise SyncError("Dashboard cards overlap")
        fkeys = []
        for f in d["filters"]:
            if f["key"] in fkeys or f["type"] not in ("string/=", "date/single"):
                raise SyncError("Duplicate/unknown dashboard filter")
            fkeys.append(f["key"])
            for mapping in f["mappings"]:
                question = next(
                    (q for q in m["questions"] if q["key"] == mapping["question"]), None
                )
                if (
                    question is None
                    or question["key"] not in ids
                    or mapping["tag"] not in question["parameters"]
                    or mapping["tag"] != f["key"]
                ):
                    raise SyncError("Invalid filter mapping")
                if ("date/" in f["type"]) != (question["parameters"][mapping["tag"]] == "date"):
                    raise SyncError("Filter/tag type mismatch")
    return m


def render_unfiltered(sql):
    """Remove only optional Metabase blocks; all variables MUST be optional."""
    text = BLOCK.sub("", sql)
    if TAG.search(text) or "[[" in text or "]]" in text:
        raise SyncError("Non-optional/invalid SQL template tag")
    return text


def secret(path):
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise SyncError("API key file missing or symlinked")
    mode = stat.S_IMODE(p.stat().st_mode)
    if mode & 0o077:
        raise SyncError("API key file must not be readable by group/other (chmod 600)")
    value = p.read_text().strip()
    if not value or any(c in value for c in "\r\n"):
        raise SyncError("Invalid API key file")
    return value


class HTTPAPI:
    """No redirects, proxy, cookie, or off-host credential forwarding."""

    def __init__(self, endpoint, key, allow_remote=False):
        u = parse.urlsplit(endpoint)
        if u.username or u.password or u.query or u.fragment or u.path not in ("", "/"):
            raise SyncError("API endpoint must be bare origin without credentials/path/query")
        if u.hostname not in ("127.0.0.1", "localhost", "::1") and not (
            allow_remote and u.scheme == "https"
        ):
            raise SyncError(
                "Only loopback allowed by default; remote requires explicit --allow-remote with HTTPS"
            )
        if u.scheme not in ("http", "https"):
            raise SyncError("Invalid API endpoint scheme")

        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise SyncError("HTTP redirect prohibited to protect API credential")

        self.base = endpoint.rstrip("/")
        self.key = key
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def call(self, method, path, payload=None):
        if not path.startswith("/api/") or "://" in path:
            raise SyncError("Invalid API path")
        headers = {"X-API-Key": self.key, "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        req = request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=30) as resp:
                body = resp.read(8_000_000)
                return json.loads(body) if body else {}
        except error.HTTPError as e:
            if e.code in (401, 403):
                raise SyncError(
                    "API authorization rejected (HTTP " + str(e.code) + "); key not shown"
                ) from None
            raise SyncError(
                "Metabase API request failed (HTTP "
                + str(e.code)
                + ", "
                + method
                + " "
                + path.split("?")[0]
                + "); response redacted"
            ) from None
        except (error.URLError, TimeoutError, OSError, ValueError):
            raise SyncError(
                "Metabase API unavailable or invalid JSON ("
                + method
                + " "
                + path.split("?")[0]
                + "); details suppressed"
            ) from None


def as_items(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        if data.get("total", len(data["data"])) > len(data["data"]):
            raise SyncError("API list is paginated; refusing incomplete identity search")
        return data["data"]
    raise SyncError("Unrecognized API list response; refuse potentially ambiguous ownership")


def marker(kind, key):
    return "[" + MARK + kind + ":" + key + "]"


def described(description, kind, key):
    return (description.strip() + "\n\n" + marker(kind, key)).strip()


def normal_collection(r):
    location = r.get("location")
    parent = int(location.strip("/").split("/")[-1]) if location and location.strip("/") else None
    return {
        "name": r.get("name"),
        "description": r.get("description"),
        "parent_id": r.get("parent_id", parent),
    }


def normal_question(r):
    native = (r.get("dataset_query") or {}).get("native") or {}
    return {
        "name": r.get("name"),
        "description": r.get("description"),
        "collection_id": r.get("collection_id"),
        "display": r.get("display"),
        "visualization_settings": r.get("visualization_settings") or {},
        "dataset_query": {
            "database": (r.get("dataset_query") or {}).get("database"),
            "type": (r.get("dataset_query") or {}).get("type"),
            "native": {
                "query": native.get("query"),
                "template-tags": native.get("template-tags") or {},
            },
        },
    }


def normal_dashboard(r):
    params = [
        {k: p.get(k) for k in ("id", "name", "slug", "type")} for p in r.get("parameters", [])
    ]
    cards = []
    for c in r.get("dashcards", []):
        cid = c.get("card_id") or (c.get("card") or {}).get("id")
        mapping = [
            {k: m.get(k) for k in ("parameter_id", "card_id", "target")}
            for m in c.get("parameter_mappings", [])
        ]
        cards.append(
            {
                "card_id": cid,
                "row": c.get("row"),
                "col": c.get("col"),
                "size_x": c.get("size_x"),
                "size_y": c.get("size_y"),
                "parameter_mappings": sorted(mapping, key=lambda x: x["parameter_id"]),
            }
        )
    return {
        "name": r.get("name"),
        "description": r.get("description"),
        "collection_id": r.get("collection_id"),
        "parameters": sorted(params, key=lambda x: x["id"]),
        "dashcards": sorted(cards, key=lambda x: (str(x["card_id"]), x["row"] or 0)),
    }


def key_id(kind, key):
    return kind + ":" + key


def card_read_path(path):
    """Metabase >= 0.57 returns MBQL 5 by default; request documented legacy form.

    POST/PUT still receive the manifest's validated legacy native-query payload.
    This suffix is for reading saved Questions only (never mutation endpoints).
    """
    return path + "?legacy-mbql=true"


def is_legacy_read_checkpoint(record, actual, want):
    """Verify a checkpoint written by our *old*, MBQL5-unaware normalizer.

    An old checkpoint cannot attest to its SQL or tags: require the live legacy
    API response to match the complete desired payload before repairing state.
    Reject any changed metadata, corrupted checkpoint, or divergent SQL.
    """
    old = record.get("remote")
    if not isinstance(old, dict) or hash_value(old) != record.get("remote_hash"):
        return False
    legacy = old.get("dataset_query")
    if not isinstance(legacy, dict) or not isinstance(legacy.get("native"), dict):
        return False
    if set(legacy) != {"database", "type", "native"} or set(legacy["native"]) != {
        "query",
        "template-tags",
    }:
        return False
    if legacy["type"] is not None or legacy["native"] != {"query": None, "template-tags": {}}:
        return False
    if (
        actual != want
        or old.get("dataset_query", {}).get("database") != actual["dataset_query"]["database"]
    ):
        return False
    return all(old.get(k) == actual.get(k) for k in actual if k != "dataset_query") and set(
        old
    ) == set(actual)


def desired_question(q, dbid, collection_id, context=None):
    context = context or (validate_experiment(), validate_release(), {})
    sql = render_sql((ROOT / q["sql"]).read_text(), *context)
    tags = {
        name: {
            "id": name,
            "name": name,
            "display-name": name.replace("_", " ").title(),
            "type": kind,
        }
        for name, kind in q["parameters"].items()
    }
    return {
        "name": q["name"],
        "description": described(q["description"], "question", q["key"]),
        "collection_id": collection_id,
        "display": q["display"],
        "visualization_settings": q["visualization_settings"],
        "dataset_query": {
            "database": dbid,
            "type": "native",
            "native": {"query": sql, "template-tags": tags},
        },
    }


def desired_dashboard(d, collection_id, qids, enabled):
    actual = [c for c in d["cards"] if c["question"] in enabled]
    filters = []
    for f in d["filters"]:
        if any(m["question"] in enabled for m in f["mappings"]):
            filters.append(
                {
                    "id": "sahabino_" + d["key"] + "_" + f["key"],
                    "name": f["name"],
                    "slug": f["key"],
                    "type": f["type"],
                }
            )
    mapped = []
    for c in actual:
        qid = qids[c["question"]]
        mm = []
        for f in d["filters"]:
            for m in f["mappings"]:
                if m["question"] == c["question"]:
                    mm.append(
                        {
                            "parameter_id": "sahabino_" + d["key"] + "_" + f["key"],
                            "card_id": qid,
                            "target": ["variable", ["template-tag", m["tag"]]],
                        }
                    )
        mapped.append(
            {
                "card_id": qid,
                **{k: c[k] for k in ("row", "col", "size_x", "size_y")},
                "parameter_mappings": sorted(mm, key=lambda x: x["parameter_id"]),
            }
        )
    return {
        "name": d["name"],
        "description": described(d["description"], "dashboard", d["key"]),
        "collection_id": collection_id,
        "parameters": sorted(filters, key=lambda x: x["id"]),
        "dashcards": sorted(mapped, key=lambda x: (str(x["card_id"]), x["row"])),
    }


def validate_contract(api, version):
    health = api.call("GET", "/api/health")
    if health.get("status") != "ok":
        raise SyncError("Metabase API health is not ok")
    props = api.call("GET", "/api/session/properties")
    v = props.get("version") or {}
    tag = v.get("tag") if isinstance(v, dict) else v
    if tag != version:
        raise SyncError(
            "Metabase version mismatch or cannot verify pinned version; expected " + version
        )
    spec = api.call("GET", "/api/docs/openapi.json")
    if not isinstance(spec, dict) or not isinstance(spec.get("paths"), dict):
        raise SyncError("Official live Metabase OpenAPI contract missing")
    paths = {
        re.sub(r"\{[^}]+\}", ":id", p.removeprefix("/api")): ops for p, ops in spec["paths"].items()
    }
    wanted = {
        "/collection": ("get", "post"),
        "/collection/:id": ("get",),
        "/card": ("get", "post"),
        "/card/:id": ("get", "put"),
        "/dashboard": ("get", "post"),
        "/dashboard/:id": ("get", "put"),
        "/database": ("get",),
        "/dataset": ("post",),
    }
    for path, methods in wanted.items():
        if not any(
            all(m in ops for m in methods) for p, ops in paths.items() if p.rstrip("/") == path
        ):
            raise SyncError("Pinned-version OpenAPI endpoint missing: " + path + " " + str(methods))


def capability_enabled(capability, capabilities):
    network_ready = capabilities.get("network_state") in (
        "NETWORK_SCHEMA_READY",
        "NETWORK_EMPTY",
        "NETWORK_DATA_AVAILABLE",
        "NETWORK_COMPARISON_INSUFFICIENT",
        "NETWORK_COMPARISON_READY",
    )
    return {
        "base": True,
        "sentiment": capabilities.get("sentiment_state") == "SENTIMENT_DATA_AVAILABLE",
        "network_readiness": True,
        "network": network_ready,
        "cross_domain": network_ready,
        "release_readiness": True,
        "release_base": True,
        "release_network": network_ready,
    }[capability]


def check_source(api, manifest, experiment=None, release=None):
    experiment = experiment or validate_experiment()
    release = release or validate_release()
    matches = [
        d
        for d in as_items(api.call("GET", "/api/database"))
        if d.get("name") == manifest["database_name"] and not d.get("is_sample")
    ]
    if len(matches) != 1:
        raise SyncError("Exact source database name missing or ambiguous; no first-ID fallback")
    dbid = matches[0]["id"]

    def dataset(query):
        result = api.call(
            "POST",
            "/api/dataset",
            {"database": dbid, "type": "native", "native": {"query": query, "template-tags": {}}},
        )
        if result.get("status") in ("failed", "error") or not isinstance(
            (result.get("data") or {}).get("rows"), list
        ):
            raise SyncError("Source database query/permissions failed; no content changes")
        return result["data"]["rows"]

    identity = dataset("SELECT current_user::text, current_database()::text")
    if len(identity) != 1 or identity[0] != ["sahabino_bi_reader", "sahabino"]:
        raise SyncError(
            "Metabase source connection must use sahabino_bi_reader on sahabino (not admin)"
        )
    # Permission probes return booleans only; never read raw sensitive data.
    from schema import BASE, NETWORK, SENSITIVE, SENTIMENT

    sensitive = [(t, c) for t, cols in SENSITIVE.items() for c in cols]
    safety = [
        "has_database_privilege(current_user,current_database(),'CREATE')",
        "has_database_privilege(current_user,current_database(),'TEMP')",
        "has_schema_privilege(current_user,'public','CREATE')",
        "has_table_privilege(current_user,'public.reviews','SELECT')",
        "has_table_privilege(current_user,'public.review_observations','SELECT')",
        "EXISTS(SELECT 1 FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user))",
        "EXISTS(SELECT 1 FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user))",
        "EXISTS(SELECT 1 FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user))",
    ]
    for table, col in sensitive:
        safety.append(
            "CASE WHEN EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='"
            + table
            + "' AND column_name='"
            + col
            + "') "
            "THEN has_column_privilege(current_user,'public."
            + table
            + "','"
            + col
            + "','SELECT') ELSE false END"
        )
    safety.append("current_setting('default_transaction_read_only')='on'")
    checks = dataset("SELECT " + ",\n ".join(safety))
    if len(checks) != 1 or checks[0] != [False] * (len(safety) - 1) + [True]:
        raise SyncError(
            "Source role unsafe: inherited, write, sensitive or schema privileges detected; no changes"
        )

    def schema_present(schema):
        items = [(table, column) for table, cols in schema.items() for column in cols]
        expressions = [
            "EXISTS(SELECT 1 FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relname='"
            + t
            + "' AND a.attname='"
            + c
            + "' AND a.attnum>0 AND NOT a.attisdropped)"
            for t, c in items
        ]
        rows = dataset("SELECT " + ",".join(expressions))
        return len(rows) == 1 and all(x is True for x in rows[0])

    def grants_present(schema):
        items = [(table, column) for table, cols in schema.items() for column in cols]
        rows = dataset(
            "SELECT "
            + ",".join(
                "has_column_privilege(current_user,'public."
                + table
                + "','"
                + column
                + "','SELECT')"
                for table, column in items
            )
        )
        return len(rows) == 1 and all(x is True for x in rows[0])

    if not schema_present(BASE) or not grants_present(BASE):
        raise SyncError("Missing required base schema columns/reader access; no content changes")
    sentiment = schema_present(SENTIMENT) and grants_present(SENTIMENT)
    if sentiment:
        # even with schema present, an empty classification population must not be fabricated.
        sentiment = bool(
            dataset(
                "SELECT EXISTS(SELECT 1 FROM public.review_observations WHERE sentiment_status='done' AND sentiment_label IN ('positive','neutral','negative'))"
            )[0][0]
        )
    if not schema_present(NETWORK):
        network_state = "NETWORK_SCHEMA_MISSING"
    elif not grants_present(NETWORK):
        network_state = "NETWORK_GRANTS_MISSING"
    else:
        capture_count = dataset("SELECT count(*)::bigint FROM public.network_captures")[0][0]
        network_state = "NETWORK_EMPTY" if capture_count == 0 else "NETWORK_DATA_AVAILABLE"

    if experiment.records and network_state not in ("NETWORK_SCHEMA_MISSING", "NETWORK_GRANTS_MISSING"):
        ids = ",".join("'" + row["capture_id"] + "'::uuid" for row in experiment.records)
        db_rows = dataset(
            "SELECT nc.id::text,a.package_name,nc.scenario,nc.transfer_file_size_bytes,nc.status,"
            "(nar.capture_id IS NOT NULL),coalesce(nar.comparison_ready,false),"
            "coalesce(nar.direction_metadata_available,false),coalesce(nar.truncated_packet_count,0),"
            "nar.observed_primary_payload_span_ms,nar.effective_file_throughput_mbps "
            "FROM public.network_captures nc JOIN public.applications a ON a.id=nc.application_id "
            "LEFT JOIN public.network_analysis_results nar ON nar.capture_id=nc.id WHERE nc.id IN ("
            + ids
            + ")"
        )
        by_id = {row[0]: row for row in db_rows}
        eligible_groups = {}
        for row in experiment.records:
            db = by_id.get(row["capture_id"])
            if db is None:
                raise SyncError("Verified experiment references a capture absent from the source")
            if db[1] != row["application_package"] or db[2] != row["scenario"]:
                raise SyncError("Experiment capture attribution/scenario conflicts with source")
            if db[3] != row["test_file_size_bytes"] or db[4] != "analyzed" or not row["transfer_completed"]:
                raise SyncError("Experiment transfer size/status is incomplete or inconsistent")
            if db[5] and db[6] and db[7] and db[8] == 0 and db[9] and db[10] is not None:
                key = tuple(
                    row[name]
                    for name in (
                        "experiment_id", "session_id", "application_package", "scenario",
                        "file_cohort_id", "test_file_size_bytes", "app_version", "device_model",
                        "android_version", "network_type", "network_profile", "capture_tool",
                        "capture_tool_version", "experiment_phase",
                    )
                )
                eligible_groups[key] = eligible_groups.get(key, 0) + 1
        experiment_state = "VERIFIED_EXPERIMENT_AVAILABLE"
        if network_state != "NETWORK_EMPTY":
            network_state = (
                "NETWORK_COMPARISON_READY"
                if any(count >= 3 for count in eligible_groups.values())
                else "NETWORK_COMPARISON_INSUFFICIENT"
            )
    else:
        experiment_state = "MANIFEST_NOT_SUPPLIED" if not experiment.records else "NETWORK_NOT_READY"

    capabilities = {
        "network_state": network_state,
        "experiment_state": experiment_state,
        "release_state": (
            "VERIFIED_RELEASE_METADATA_AVAILABLE"
            if any(
                row["release_date_verified"] and row["release_date_precision"] == "day"
                for row in release.records
            )
            else ("RELEASE_METADATA_UNVERIFIED" if release.records else "MANIFEST_NOT_SUPPLIED")
        ),
        "sentiment_state": "SENTIMENT_DATA_AVAILABLE" if sentiment else "SENTIMENT_UNAVAILABLE",
        "topic_state": "ANNOTATIONS_UNAVAILABLE",
    }
    context = (experiment, release, capabilities)
    for q in manifest["questions"]:
        if not capability_enabled(q["capability"], capabilities):
            continue
        compiled = render_sql((ROOT / q["sql"]).read_text(), *context)
        raw = render_unfiltered(compiled).strip().rstrip(";")
        dataset("SELECT * FROM (" + raw + ") AS sahabino_bi_validation LIMIT 0")
    return dbid, capabilities


def read_state(path):
    path = Path(path)
    if not path.exists():
        return {"format": 1, "objects": {}}
    if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SyncError("Sync state file must be private, regular and non-symlink")
    state = json.loads(path.read_text())
    if state.get("format") != 1 or not isinstance(state.get("objects"), dict):
        raise SyncError("Invalid local ownership state")
    return state


def write_state(path, state):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise SyncError("Refuse state symlink")
    fd, temp = tempfile.mkstemp(prefix=".sync-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as fp:
            os.fchmod(fp.fileno(), 0o600)
            json.dump(state, fp, indent=2, sort_keys=True)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Sync:
    def __init__(self, api, manifest, state, path, apply, context=None):
        self.api = api
        self.m = manifest
        self.state = state
        self.path = path
        self.apply = apply
        self.ids = {"collection": {}, "question": {}, "dashboard": {}}
        self.actions = []
        self.lists = {}
        self.context = context or (validate_experiment(), validate_release(), {})

    def inventory(self):
        for kind, endpoint in [
            ("collection", "/api/collection"),
            ("question", "/api/card"),
            ("dashboard", "/api/dashboard"),
        ]:
            self.lists[kind] = as_items(self.api.call("GET", endpoint))

    def reconcile(self, kind, item, want, parent_id=None):
        k = key_id(kind, item["key"])
        record = self.state["objects"].get(k)
        collection = want.get("collection_id") if kind != "collection" else parent_id
        candidates = [
            o
            for o in self.lists[kind]
            if o.get("name") == item["name"]
            and (o.get("collection_id") if kind != "collection" else o.get("parent_id"))
            == collection
        ]
        # Collections GET often gives location "/<id>/" rather than parent_id; same-name is fail-closed.
        if kind == "collection":
            candidates = [o for o in self.lists[kind] if o.get("name") == item["name"]]
        norm = {
            "collection": normal_collection,
            "question": normal_question,
            "dashboard": normal_dashboard,
        }[kind]
        if record:
            rid = record["id"]
            path = {
                "collection": "/api/collection/",
                "question": "/api/card/",
                "dashboard": "/api/dashboard/",
            }[kind] + str(rid)
            remote = self.api.call("GET", card_read_path(path) if kind == "question" else path)
            if remote.get("id") != rid or marker(kind, item["key"]) not in (
                remote.get("description") or ""
            ):
                raise SyncError(
                    "Ownership marker/ID mismatch for " + k + "; manual recovery required"
                )
            if remote.get("archived") is True:
                raise SyncError(
                    "Managed object manually archived: "
                    + k
                    + "; refusing silent unarchive or replacement"
                )
            actual = norm(remote)
            if hash_value(actual) != record.get("remote_hash"):
                if kind == "question" and is_legacy_read_checkpoint(record, actual, want):
                    self.actions.append(("REPAIR_CHECKPOINT", k))
                    if self.apply:
                        self.state["objects"][k] = {
                            "id": rid,
                            "remote_hash": hash_value(actual),
                            "remote": actual,
                        }
                        write_state(self.path, self.state)
                    self.ids[kind][item["key"]] = rid
                    return rid
                raise SyncError(
                    "Manual edit, incompatible prior checkpoint, or differing live SQL for "
                    + k
                    + "; refusing overwrite"
                )
            if len(candidates) > 1 or any(c["id"] != rid for c in candidates):
                raise SyncError("Ambiguous duplicate managed name for " + k)
            if actual == want:
                self.actions.append(("SKIP", k))
                self.ids[kind][item["key"]] = rid
                return rid
            self.actions.append(("UPDATE", k))
            if self.apply:
                payload = copy.deepcopy(want)
                if kind == "dashboard":
                    # PUT must round-trip all dashboard cards; each existing card keeps its own ID.
                    old = {
                        (c.get("card_id") or (c.get("card") or {}).get("id")): c
                        for c in remote.get("dashcards", [])
                    }
                    # Metabase validates dashcard IDs for uniqueness, including new
                    # negative temporary IDs. A shared -1 for all new cards is invalid.
                    for index, c in enumerate(payload["dashcards"]):
                        c["id"] = old.get(c["card_id"], {}).get("id", -1 - index)
                    ids = [c["id"] for c in payload["dashcards"]]
                    if len(ids) != len(set(ids)):
                        raise SyncError(
                            "Duplicate dashboard-card IDs; refusing unsafe update for " + k
                        )
                    if len(old) > len(payload["dashcards"]):
                        raise SyncError("Unmanaged dashcard present; refusing to delete")
                self.api.call("PUT", path, payload)
                self.record(k, rid, path, norm, want)
            self.ids[kind][item["key"]] = rid
            return rid
        if candidates or any(
            marker(kind, item["key"]) in (o.get("description") or "") for o in self.lists[kind]
        ):
            raise SyncError(
                "Untracked same-name/marked object "
                + k
                + "; never adopt or overwrite without ownership recovery"
            )
        self.actions.append(("CREATE", k))
        if not self.apply:
            # virtual negative IDs are stable within a plan, no requests mutate server.
            vid = -1 - len(self.actions)
            self.ids[kind][item["key"]] = vid
            return vid
        endpoint = {
            "collection": "/api/collection",
            "question": "/api/card",
            "dashboard": "/api/dashboard",
        }[kind]
        payload = copy.deepcopy(want)
        if kind == "collection":
            payload = {
                "name": item["name"],
                "description": want["description"],
                "parent_id": parent_id,
            }
        if kind == "dashboard":
            # Create empty shell and checkpoint immediately, then update cards in second stage.
            payload = {x: want[x] for x in ("name", "description", "collection_id", "parameters")}
        result = self.api.call("POST", endpoint, payload)
        rid = result.get("id")
        if type(rid) is not int or rid < 1:
            raise SyncError(
                "POST " + k + " returned no numeric ID; inspect remote marker before retry"
            )
        path = endpoint + "/" + str(rid)
        self.record(k, rid, path, norm, None if kind == "dashboard" else want)
        self.ids[kind][item["key"]] = rid
        self.lists[kind].append(
            {
                "id": rid,
                "name": item["name"],
                "collection_id": collection,
                "description": want["description"],
            }
        )
        if kind == "dashboard":
            shell = self.api.call("GET", path)
            cards = copy.deepcopy(want["dashcards"])
            # Each unsaved dashboard card needs a distinct negative temp ID.
            for index, c in enumerate(cards):
                c["id"] = -1 - index
            self.actions.append(("ASSEMBLE", k))
            self.api.call(
                "PUT",
                path,
                {
                    **{x: want[x] for x in ("name", "description", "collection_id", "parameters")},
                    "dashcards": cards,
                },
            )
            self.record(k, rid, path, norm, want)
        return rid

    def record(self, k, rid, path, norm, want):
        obj = self.api.call("GET", card_read_path(path) if k.startswith("question:") else path)
        if obj.get("id") != rid or marker(*k.split(":", 1)) not in (obj.get("description") or ""):
            raise SyncError(
                "Created/updated "
                + k
                + " but cannot verify marker: partial completion, manual recovery"
            )
        actual = norm(obj)
        if want is not None and actual != want:
            # Do not record a "clean desired" state without verifying exact round-trip.
            self.state["objects"][k] = {
                "id": rid,
                "remote_hash": hash_value(actual),
                "remote": actual,
            }
            write_state(self.path, self.state)
            raise SyncError(
                "Remote payload differs from expected for "
                + k
                + "; state checkpointed; no silent overwrite"
            )
        self.state["objects"][k] = {"id": rid, "remote_hash": hash_value(actual), "remote": actual}
        write_state(self.path, self.state)

    def run(self, dbid, capabilities):
        if isinstance(capabilities, bool):
            capabilities = {
                "network_state": "NETWORK_SCHEMA_MISSING",
                "experiment_state": "MANIFEST_NOT_SUPPLIED",
                "release_state": "MANIFEST_NOT_SUPPLIED",
                "sentiment_state": "SENTIMENT_DATA_AVAILABLE" if capabilities else "SENTIMENT_UNAVAILABLE",
                "topic_state": "ANNOTATIONS_UNAVAILABLE",
            }
        self.context = (self.context[0], self.context[1], capabilities)
        self.inventory()
        for c in self.m["collections"]:
            parent = self.ids["collection"].get(c["parent"]) if c["parent"] else None
            want = {
                "name": c["name"],
                "description": described(c["description"], "collection", c["key"]),
                "parent_id": parent,
            }
            self.reconcile("collection", c, want, parent)
        for q in self.m["questions"]:
            if not capability_enabled(q["capability"], capabilities):
                self.actions.append(("SKIP_CAPABILITY", "question:" + q["key"]))
                continue
            self.reconcile(
                "question",
                q,
                desired_question(q, dbid, self.ids["collection"][q["collection"]], self.context),
            )
        for d in self.m["dashboards"]:
            enabled_questions = {
                q["key"] for q in self.m["questions"] if capability_enabled(q["capability"], capabilities)
            }
            disabled_cards = [
                card["question"] for card in d["cards"] if card["question"] not in enabled_questions
            ]
            if disabled_cards and key_id("dashboard", d["key"]) in self.state["objects"]:
                self.actions.append(("SKIP_CAPABILITY_PRESERVE", "dashboard:" + d["key"]))
                continue
            if not any(card["question"] in enabled_questions for card in d["cards"]):
                self.actions.append(("SKIP_CAPABILITY", "dashboard:" + d["key"]))
                continue
            want = desired_dashboard(
                d,
                self.ids["collection"][d["collection"]],
                self.ids["question"],
                enabled_questions,
            )
            self.reconcile("dashboard", d, want)
        return self.actions


def main(argv=None, api_override=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("plan", "apply"))
    p.add_argument("--endpoint", default="http://127.0.0.1:3001")
    p.add_argument("--api-key-file", default=str(ROOT / "secrets" / "metabase_api_key"))
    p.add_argument("--state-file", default=str(DEFAULT_STATE))
    p.add_argument("--allow-remote", action="store_true")
    p.add_argument("--experiment-manifest")
    p.add_argument("--release-manifest")
    a = p.parse_args(argv)
    sync = None
    try:
        m = load_manifest()
        experiment = validate_experiment(a.experiment_manifest)
        release = validate_release(a.release_manifest)
        if not experiment.valid or not release.valid:
            raise SyncError("Private experiment/release manifest validation failed")
        state = read_state(a.state_file)
        api = api_override or HTTPAPI(a.endpoint, secret(a.api_key_file), a.allow_remote)
        validate_contract(api, m["metabase_version"])
        dbid, capabilities = check_source(api, m, experiment, release)
        print(
            "OK: exact source identity, reader privileges, SQL/schema, API health/version/OpenAPI verified"
        )
        if capabilities["sentiment_state"] != "SENTIMENT_DATA_AVAILABLE":
            print(
                "SKIP_CAPABILITY: sentiment schema/grants or classified data unavailable; optional cards/dashboard withheld"
            )
        print(
            "CAPABILITIES: "
            + ", ".join(k + "=" + v for k, v in sorted(capabilities.items()))
        )
        sync = Sync(
            api,
            m,
            state,
            a.state_file,
            a.action == "apply",
            (experiment, release, capabilities),
        )
        actions = sync.run(dbid, capabilities)
        for operation, item in actions:
            print(operation + ": " + item)
        print(
            ("APPLY" if a.action == "apply" else "PLAN")
            + f": completed {len(actions)} action assessments; no unrelated content deleted"
        )
        return 0
    except (SyncError, MetadataError, ValueError, OSError, KeyError, TypeError) as exc:
        if sync:
            for op, item in sync.actions:
                print(op + ": " + item)
        print("BLOCKER: " + str(exc), file=sys.stderr)
        print(
            "PARTIAL FAILURE: review listed operations and private state checkpoint; never blindly delete remote objects",
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
