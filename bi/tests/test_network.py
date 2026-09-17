import copy
import json
import sys
from pathlib import Path

import pytest

BI = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BI / "scripts"))
import network_attach as net

CID = "a" * 64


class FakeDocker:
    def __init__(self, attached=False, alias=True, ids=None, network=True):
        self.container = {
            "Id": CID,
            "Name": "/sahabino-postgres-1",
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "sahabino",
                    "com.docker.compose.service": "postgres",
                }
            },
            "State": {"Running": True},
            "NetworkSettings": {"Networks": {}},
        }
        self.network = {"Name": "sahabino-bi-db", "Driver": "bridge", "Containers": {}}
        self.connected = 0
        self.ids = ids if ids is not None else [CID]
        self.exists = network
        if attached:
            self.attach(alias)

    def attach(self, alias=True):
        self.network["Containers"][CID] = {"Name": "sahabino-postgres-1"}
        self.container["NetworkSettings"]["Networks"]["sahabino-bi-db"] = {
            "Aliases": ["sahabino-postgres-bi"] if alias else ["postgres"]
        }

    def __call__(self, *argv):
        if argv[:1] == ("ps",):
            return "\n".join(self.ids)
        if argv[:2] == ("inspect", "--type"):
            if argv[2] == "network" and not self.exists:
                raise net.NetworkError("Docker network inspect failed")
            return json.dumps(
                [copy.deepcopy(self.container if argv[2] == "container" else self.network)]
            )
        if argv[:2] == ("network", "connect"):
            assert argv[2:4] == ("--alias", "sahabino-postgres-bi")
            self.connected += 1
            self.attach()
            return ""
        raise AssertionError(argv)


def test_first_attachment_and_repeated_idempotent(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(net, "docker", fake)
    assert net.operate("sahabino", "postgres", "sahabino-bi-db", CID, "attach") == CID
    assert fake.connected == 1
    assert net.operate("sahabino", "postgres", "sahabino-bi-db", CID, "attach") == CID
    assert fake.connected == 1


def test_absent_ambiguous_and_wrong_container(monkeypatch):
    for ids in ([], [CID, "b" * 64]):
        monkeypatch.setattr(net, "docker", FakeDocker(ids=ids))
        with pytest.raises(net.NetworkError, match="exactly one"):
            net.discover("sahabino", "postgres")
    fake = FakeDocker()
    monkeypatch.setattr(net, "docker", fake)
    with pytest.raises(net.NetworkError, match="differs"):
        net.operate("sahabino", "postgres", "sahabino-bi-db", "b" * 64, "attach")
    assert fake.connected == 0
    fake.container["Config"]["Labels"]["com.docker.compose.project"] = "other"
    with pytest.raises(net.NetworkError, match="mismatch"):
        net.discover("sahabino", "postgres")


def test_correct_attachment_missing_alias_and_unavailable_network(monkeypatch):
    fake = FakeDocker(attached=True)
    monkeypatch.setattr(net, "docker", fake)
    assert net.operate("sahabino", "postgres", "sahabino-bi-db", CID, "check") == CID
    fake = FakeDocker(attached=True, alias=False)
    monkeypatch.setattr(net, "docker", fake)
    with pytest.raises(net.NetworkError, match="do NOT disconnect"):
        net.operate("sahabino", "postgres", "sahabino-bi-db", CID, "attach")
    assert fake.connected == 0
    fake = FakeDocker(network=False)
    monkeypatch.setattr(net, "docker", fake)
    with pytest.raises(net.NetworkError, match="create explicitly"):
        net.operate("sahabino", "postgres", "sahabino-bi-db", CID, "attach")
    assert fake.connected == 0


def test_unavailable_docker(monkeypatch):
    def unavailable(*_):
        raise net.NetworkError("Docker unavailable")

    monkeypatch.setattr(net, "docker", unavailable)
    with pytest.raises(net.NetworkError, match="unavailable"):
        net.discover("sahabino", "postgres")
