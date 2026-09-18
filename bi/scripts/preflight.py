#!/usr/bin/env python3
"""Non-destructive audit. Does not create containers, users, databases or networks."""

import argparse
import base64
import json
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

from network_attach import ALIAS, NetworkError, discover, inspect, membership
from schema import BASE, NETWORK, SENSITIVE, SENTIMENT

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    data = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.isidentifier():
                data[k] = v.strip("\"'")
    return data


def query(container, user, db, sql, password_file=None):
    env = os.environ.copy()
    if password_file:
        path = Path(password_file)
        if not path.is_file() or (path.stat().st_mode & 0o077):
            raise RuntimeError("Password secret missing or not mode 0600/0400")
        env["PGPASSWORD"] = path.read_text().rstrip("\r\n")
    # Docker --env NAME inherits client environment. Password NEVER appears in argv/stdout.
    cmd = ["docker", "exec", "-i"]
    if password_file:
        cmd.extend(["--env", "PGPASSWORD"])
    cmd.extend([container, "psql", "-X", "-v", "ON_ERROR_STOP=1", "-A", "-t", "-U", user, "-d", db])
    try:
        result = subprocess.run(
            cmd, input=sql + "\n", capture_output=True, text=True, env=env, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("Connection/psql invocation failed (details redacted)") from e
    finally:
        env.pop("PGPASSWORD", None)
    if result.returncode:
        raise RuntimeError("Database connection or query denied (server diagnostics redacted)")
    return result.stdout.strip()


def privilege_sql():
    required = [(t, c) for t, cs in BASE.items() for c in cs]
    optional = [(t, c) for t, cs in SENTIMENT.items() for c in cs]
    network = [(t, c) for t, cs in NETWORK.items() for c in cs]
    sensitive = [(t, c) for t, cs in SENSITIVE.items() for c in cs]

    def items(rows):
        return ",".join("('%s','%s')" % (t, c) for t, c in rows)

    return f"""
WITH required(table_name,column_name) AS (VALUES {items(required)}),
 optional(table_name,column_name) AS (VALUES {items(optional)}),
 network(table_name,column_name) AS (VALUES {items(network)}),
 sensitive(table_name,column_name) AS (VALUES {items(sensitive)}),
 present AS (SELECT table_name,column_name FROM information_schema.columns WHERE table_schema='public'),
 physical AS (
  SELECT c.relname table_name,a.attname column_name
  FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped
 ),
 forbidden_columns AS (
  SELECT p.table_name,p.column_name FROM present p
  WHERE NOT EXISTS(SELECT 1 FROM required r WHERE r.table_name=p.table_name AND r.column_name=p.column_name)
    AND NOT EXISTS(SELECT 1 FROM optional o WHERE o.table_name=p.table_name AND o.column_name=p.column_name)
    AND NOT EXISTS(SELECT 1 FROM network nw WHERE nw.table_name=p.table_name AND nw.column_name=p.column_name)
    AND has_column_privilege(current_user,format('public.%I',p.table_name),p.column_name,'SELECT')
 ), extra_tables AS (
  SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN('r','p','v','m','f')
   AND has_table_privilege(current_user,c.oid,'SELECT')
 ), write_tables AS (
  SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname NOT IN('pg_catalog','information_schema') AND c.relkind IN('r','p','v','m','f')
   AND has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
 ), dangerous_definers AS (
  SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
  WHERE n.nspname NOT IN('pg_catalog','information_schema') AND p.prosecdef
   AND has_function_privilege(current_user,p.oid,'EXECUTE')
 )
SELECT row_to_json(x)::text FROM (
 SELECT current_user AS role,current_database() AS db,
  (SELECT count(*) FROM required r WHERE NOT EXISTS(SELECT 1 FROM present p WHERE p.table_name=r.table_name AND p.column_name=r.column_name)) AS missing_base,
  (SELECT count(*) FROM required r WHERE NOT has_column_privilege(current_user,format('public.%I',r.table_name),r.column_name,'SELECT')) AS denied_base,
  (SELECT count(*) FROM optional o WHERE NOT EXISTS(SELECT 1 FROM physical p WHERE p.table_name=o.table_name AND p.column_name=o.column_name)) AS missing_sentiment,
  (SELECT count(*) FROM optional o JOIN physical p USING(table_name,column_name)
    WHERE NOT has_column_privilege(current_user,format('public.%I',p.table_name),p.column_name,'SELECT')) AS denied_sentiment,
  (SELECT count(*) FROM network nw WHERE NOT EXISTS(SELECT 1 FROM physical p
    WHERE p.table_name=nw.table_name AND p.column_name=nw.column_name)) AS missing_network,
  (SELECT count(*) FROM network nw JOIN physical p USING(table_name,column_name)
    WHERE NOT has_column_privilege(current_user,format('public.%I',p.table_name),p.column_name,'SELECT')) AS denied_network,
  (SELECT count(*) FROM forbidden_columns) AS forbidden_column_access,
  (SELECT count(*) FROM extra_tables) AS broad_table_access,
  (SELECT count(*) FROM write_tables) AS writable_tables,
  (SELECT count(*) FROM dangerous_definers) AS executable_security_definers,
  has_schema_privilege(current_user,'public','CREATE') AS can_create_in_public,
  has_database_privilege(current_user,current_database(),'CREATE') AS can_create_schema,
  has_database_privilege(current_user,current_database(),'TEMP') AS can_create_temp,
  (SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)) AS memberships,
  (SELECT count(*) FROM pg_class WHERE relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)) AS owned_relations,
  (SELECT count(*) FROM pg_namespace WHERE nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)) AS owned_schemas,
  (SELECT version_num FROM public.alembic_version LIMIT 1) AS alembic_revision
) x;
"""


def evaluate(args):
    issues = []

    def out(level, message):
        print(f"{level}: {message}")
        if level == "BLOCKER":
            issues.append(message)

    env = read_env(ROOT / ".env")
    env_file = ROOT / ".env"
    if not env_file.is_file():
        out("BLOCKER", "BI .env absent; manually configure from .env.example")
    elif env_file.is_symlink() or stat.S_IMODE(env_file.stat().st_mode) & 0o077:
        out("BLOCKER", "BI .env must be a private regular file with mode 0600/0400")
    else:
        out("OK", "Private .env file mode verified (key values not printed)")
    encryption = env.get("MB_ENCRYPTION_SECRET_KEY", "")
    session = env.get("MB_SESSION_SECRET_KEY", "")
    try:
        decoded = base64.b64decode(encryption, validate=True)
    except (ValueError, base64.binascii.Error):
        decoded = b""
    if len(decoded) < 16 or encryption.startswith("CHANGE_ME"):
        out("BLOCKER", "MB_ENCRYPTION_SECRET_KEY must be valid base64 of at least 16 random bytes")
    else:
        out("OK", "Encryption key format verified (entropy not independently verifiable)")
    if len(session) < 16 or session.startswith("CHANGE_ME") or session == encryption:
        out("BLOCKER", "MB_SESSION_SECRET_KEY missing, too short, placeholder, or not independent")
    else:
        out(
            "OK",
            "Session key length and independence verified (entropy not independently verifiable)",
        )
    metadata_secret = env.get("METABASE_APP_DB_PASSWORD_FILE", "")
    if (
        not metadata_secret
        or not Path(metadata_secret).is_file()
        or Path(metadata_secret).is_symlink()
        or (Path(metadata_secret).stat().st_mode & 0o077)
    ):
        out("BLOCKER", "Metabase metadata secret file missing, symlinked or group/world accessible")
    else:
        out("OK", "Metadata secret file access mode verified (value never displayed)")
    network = args.network or env.get("BI_DB_NETWORK", "sahabino-bi-db")
    port = int(env.get("METABASE_PORT", "3001"))
    if port < 1 or port > 65535:
        out("BLOCKER", "Invalid Metabase host port")
        return 2
    try:
        c = discover(args.project, args.service)
        cid = c["Id"]
        out("OK", f"Compose PostgreSQL identity {cid} ({args.project}/{args.service})")
        net = inspect("network", network)
        aliases = membership(c, network)
        if aliases is None or cid not in (net.get("Containers") or {}):
            out("BLOCKER", "PostgreSQL not on expected BI Docker network")
        elif ALIAS not in aliases:
            out(
                "BLOCKER",
                "PostgreSQL attached without exact BI network alias; maintenance required, no automatic reconnect",
            )
        else:
            out("OK", f"BI network {network} membership + alias verified")
    except (NetworkError, ValueError, KeyError):
        cid = None
        out(
            "BLOCKER",
            "Docker unavailable, ambiguous PostgreSQL labels, or BI network missing; use network_attach.py discover/check",
        )
    s = socket.socket()
    s.settimeout(2)
    try:
        s.bind(("127.0.0.1", port))
        out("OK", f"127.0.0.1:{port} free for Metabase")
    except OSError:
        # An occupied port is acceptable only if it demonstrably belongs to this BI Compose service.
        try:
            proc = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    "label=com.docker.compose.project=sahabino-bi",
                    "--filter",
                    "label=com.docker.compose.service=metabase",
                    "--format",
                    "{{.Ports}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            ports = proc.stdout.splitlines() if proc.returncode == 0 else []
            if len(ports) == 1 and f"127.0.0.1:{port}->3000/tcp" in ports[0]:
                out("OK", f"127.0.0.1:{port} occupied by identified BI Metabase container")
            else:
                out(
                    "BLOCKER",
                    f"127.0.0.1:{port} occupied by unidentified process; verify owner before deployment",
                )
        except (OSError, subprocess.TimeoutExpired):
            out("BLOCKER", f"127.0.0.1:{port} occupied; owner cannot be verified safely")
    finally:
        s.close()
    backup = env.get("BI_METADATA_BACKUP_DIR")
    if not backup:
        out(
            "WARN",
            "BI_METADATA_BACKUP_DIR not configured; do not operate without scheduled, restorable metadata backups",
        )
    elif Path(backup).is_dir():
        out("OK", "Metadata backup destination configured (restore not tested)")
    else:
        out(
            "WARN",
            "Configured metadata backup destination missing; operator must create/manage it explicitly",
        )
    if not cid:
        return 2
    try:
        src = json.loads(
            query(cid, args.reader, args.source, privilege_sql(), args.reader_password_file)
        )
        if src["role"] != args.reader or src["db"] != args.source:
            out("BLOCKER", "Reader database identity mismatch")
        else:
            out(
                "OK",
                "Source database connectivity as restricted BI reader (local socket; password authentication may not be exercised)",
            )
        out(
            "OK" if src["missing_base"] == 0 else "BLOCKER",
            f"Required base columns missing: {src['missing_base']}",
        )
        out(
            "OK" if src["denied_base"] == 0 else "BLOCKER",
            f"Required base column SELECT grants denied: {src['denied_base']}",
        )
        if src["missing_sentiment"] == 0 and src["denied_sentiment"] == 0:
            out("OK", "Sentiment columns present and reader grants verified")
        else:
            out(
                "WARN",
                f"Sentiment gated: missing columns={src['missing_sentiment']}, denied columns={src['denied_sentiment']}; review/store remain available",
            )
        if src["missing_network"]:
            out(
                "WARN",
                f"Network capability state=NETWORK_SCHEMA_MISSING columns={src['missing_network']}",
            )
        elif src["denied_network"]:
            out(
                "WARN",
                f"Network capability state=NETWORK_GRANTS_MISSING columns={src['denied_network']}",
            )
        else:
            out(
                "OK",
                "Network capability state=NETWORK_SCHEMA_READY (data sufficiency not checked here)",
            )
        for k in (
            "forbidden_column_access",
            "broad_table_access",
            "writable_tables",
            "executable_security_definers",
            "memberships",
            "owned_relations",
            "owned_schemas",
        ):
            out("OK" if not src[k] else "BLOCKER", f"Reader privilege check {k}={src[k]}")
        for k in ("can_create_in_public", "can_create_schema", "can_create_temp"):
            out("BLOCKER" if src[k] else "OK", f"Reader privilege check {k}={src[k]}")
        out("OK", f"Source Alembic revision: {src['alembic_revision']}")
        if src["alembic_revision"] not in ("20260916_0006", "20260917_0007", "20260917_0008"):
            out(
                "WARN",
                "Source migration revision differs from documented 0006/0007/0008; DBA verify compatibility",
            )
    except (RuntimeError, ValueError, KeyError):
        out(
            "BLOCKER",
            "Source role connectivity, alembic revision, or privilege introspection failed; secrets/DB diagnostics suppressed",
        )
    try:
        text = query(
            cid,
            args.metadata_user,
            args.metadata_db,
            "SELECT row_to_json(x)::text FROM (SELECT current_user AS role,current_database() AS db, has_database_privilege(current_user,current_database(),'CREATE') AS can_create, EXISTS(SELECT 1 FROM pg_database d WHERE d.datallowconn AND d.datname<>current_database() AND (has_database_privilege(current_user,d.datname,'CREATE') OR has_database_privilege(current_user,d.datname,'TEMPORARY'))) AS write_other_db) x;",
            args.metadata_password_file,
        )
        meta = json.loads(text)
        if (
            (meta["role"], meta["db"]) != (args.metadata_user, args.metadata_db)
            or not meta["can_create"]
            or meta["write_other_db"]
        ):
            out(
                "BLOCKER",
                "Metadata role identity, metadata writability or isolation invalid (CREATE/TEMP on other database)",
            )
        else:
            out("OK", "Metadata DB connectivity and CREATE grant (no writes performed)")
    except (RuntimeError, ValueError, KeyError):
        out(
            "BLOCKER", "Metadata role connection or CREATE privilege failed; credentials suppressed"
        )
    return 2 if issues else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True)
    p.add_argument("--service", default="postgres")
    p.add_argument("--network")
    p.add_argument("--reader", default="sahabino_bi_reader")
    p.add_argument("--source", default="sahabino")
    p.add_argument("--reader-password-file")
    p.add_argument("--metadata-user", default="metabase_app")
    p.add_argument("--metadata-db", default="metabase_app")
    p.add_argument("--metadata-password-file")
    try:
        sys.exit(evaluate(p.parse_args()))
    except Exception:
        print("BLOCKER: Unexpected preflight failure (details suppressed)", file=sys.stderr)
        sys.exit(2)
