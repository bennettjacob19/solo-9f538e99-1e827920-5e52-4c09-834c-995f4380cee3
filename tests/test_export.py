"""End-to-end tests for the platform-pruning OCI layout export."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.sample_image import build_sample_image

MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_INDEX = "application/vnd.oci.image.index.v1+json"
MT_CONFIG = "application/vnd.oci.image.config.v1+json"
MT_LAYER = "application/vnd.oci.image.layer.v1.tar"

ANNOTATION_REF_DIGEST = "vnd.docker.reference.digest"
ANNOTATION_REF_TYPE = "vnd.docker.reference.type"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def layer_tar(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tf:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 1_700_000_000
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


class Layout:
    """Tiny in-memory multi-platform OCI layout builder."""

    def __init__(self):
        self.blobs = {}  # hex -> bytes

    def add_blob(self, data: bytes) -> str:
        h = sha(data)
        self.blobs[h] = data
        return h

    def add_image(self, *, variant=None, os_name="linux", arch="amd64",
                  content=b"x", layer_media_type=MT_LAYER, compress=False,
                  annotations=None, config_extra=None):
        raw_layer = layer_tar([("etc/value.txt", content)])
        if compress:
            raw_layer = gzip.compress(raw_layer, mtime=0)
        layer_hex = self.add_blob(raw_layer)
        config = {
            "architecture": arch,
            "os": os_name,
            "rootfs": {"type": "layers", "diff_ids": [f"sha256:{sha(raw_layer)}"]},
        }
        if variant is not None:
            config["variant"] = variant
        if config_extra:
            config.update(config_extra)
        config_b = json.dumps(config, sort_keys=True).encode()
        config_hex = self.add_blob(config_b)
        manifest = {
            "schemaVersion": 2,
            "mediaType": MT_MANIFEST,
            "config": {
                "mediaType": MT_CONFIG,
                "digest": f"sha256:{config_hex}",
                "size": len(config_b),
            },
            "layers": [{
                "mediaType": layer_media_type,
                "digest": f"sha256:{layer_hex}",
                "size": len(raw_layer),
            }],
        }
        if annotations:
            manifest["annotations"] = annotations
        manifest_b = json.dumps(manifest, sort_keys=True).encode()
        manifest_hex = self.add_blob(manifest_b)
        return {
            "descriptor": {
                "mediaType": MT_MANIFEST,
                "digest": f"sha256:{manifest_hex}",
                "size": len(manifest_b),
                "platform": {
                    "os": os_name,
                    "architecture": arch,
                    **({"variant": variant} if variant else {}),
                },
            },
            "manifest": manifest,
            "config": config,
            "layer_hex": layer_hex,
        }

    def add_attestation(self, target_hex, *, content=b"att"):
        layer = layer_tar([("attestation.json", content)])
        layer_hex = self.add_blob(layer)
        config_b = json.dumps({"architecture": "unknown", "os": "unknown"}).encode()
        config_hex = self.add_blob(config_b)
        manifest = {
            "schemaVersion": 2,
            "mediaType": MT_MANIFEST,
            "config": {
                "mediaType": MT_CONFIG,
                "digest": f"sha256:{config_hex}",
                "size": len(config_b),
            },
            "layers": [{
                "mediaType": MT_LAYER,
                "digest": f"sha256:{layer_hex}",
                "size": len(layer),
            }],
            "annotations": {
                ANNOTATION_REF_DIGEST: f"sha256:{target_hex}",
                ANNOTATION_REF_TYPE: "attestation",
            },
        }
        manifest_b = json.dumps(manifest, sort_keys=True).encode()
        manifest_hex = self.add_blob(manifest_b)
        self.last_attestation_layer = layer_hex
        return {
            "mediaType": MT_MANIFEST,
            "digest": f"sha256:{manifest_hex}",
            "size": len(manifest_b),
            "platform": {"os": "unknown", "architecture": "unknown"},
        }

    def to_bytes(self, *, direct=True, drop=(), index_extra=()):
        index = {
            "schemaVersion": 2,
            "mediaType": MT_INDEX,
            "manifests": list(index_extra),
        }
        for image in self.images:
            if direct:
                index["manifests"].append(image["descriptor"])
            else:
                index["manifests"].append(self.sub_index_desc)
        if not direct:
            index["manifests"] = [self.sub_index_desc] + list(index_extra)

        files = {"oci-layout": b'{"imageLayoutVersion":"1.0.0"}'}
        files["index.json"] = json.dumps(index).encode()
        for hexd, data in self.blobs.items():
            if hexd in drop:
                continue
            files[f"blobs/sha256/{hexd}"] = data
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tf:
            for name in sorted(files):
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                info.mtime = 0
                tf.addfile(info, io.BytesIO(files[name]))
        return out.getvalue()

    def make_sub_index(self):
        manifests = [image["descriptor"] for image in self.images]
        raw = json.dumps({
            "schemaVersion": 2, "mediaType": MT_INDEX, "manifests": manifests,
        }).encode()
        self.sub_index_desc = {
            "mediaType": MT_INDEX,
            "digest": f"sha256:{self.add_blob(raw)}",
            "size": len(raw),
        }


def read_files(data: bytes):
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tf:
        for m in tf.getmembers():
            if m.isreg():
                files[m.name] = tf.extractfile(m).read()
            elif m.isdir():
                files.setdefault(m.name + "/", b"")
    return files


def export(client, data: bytes, os_name="linux", arch="amd64", variant=None):
    form = {"os": os_name, "architecture": arch}
    if variant is not None:
        form["variant"] = variant
    return client.post(
        "/export", files={"file": ("image.tar", data, "application/x-tar")}, data=form
    )


def parse_export(data: bytes):
    files = read_files(data)
    index = json.loads(files["index.json"])
    return files, index


def blob_hexes(files):
    return {
        name.split("/")[-1]
        for name in files
        if name.startswith("blobs/sha256/") and not name.endswith("/")
    }


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def multi_platform_layout():
    layout = Layout()
    layout.images = [
        layout.add_image(arch="amd64", content=b"amd64"),
        layout.add_image(arch="arm64", content=b"arm64"),
        layout.add_image(arch="arm64", variant="v8", content=b"arm64v8",
                         compress=True),
    ]
    layout.attestation_desc = layout.add_attestation(
        layout.images[1]["descriptor"]["digest"].split(":")[1]
    )
    layout.images.append({"descriptor": layout.attestation_desc})
    return layout


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_export_single_platform(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/x-tar"

    files, index = parse_export(r.content)
    target = layout.images[0]
    target_hex = target["descriptor"]["digest"].split(":")[1]

    assert [m["digest"] for m in index["manifests"]] == [f"sha256:{target_hex}"]
    desc = index["manifests"][0]
    assert desc["size"] == len(files[f"blobs/sha256/{target_hex}"])
    assert desc["platform"]["architecture"] == "amd64"

    expected = {
        target_hex,
        target["manifest"]["config"]["digest"].split(":")[1],
        target["layer_hex"],
    }
    assert blob_hexes(files) == expected
    # blobs are retained byte-for-byte
    for hexd in expected:
        assert files[f"blobs/sha256/{hexd}"] == layout.blobs[hexd]
    # oci-layout is untouched
    assert files["oci-layout"] == b'{"imageLayoutVersion":"1.0.0"}'
    assert "blobs/" in files


def test_export_keeps_matching_attestation_only(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="arm64")
    assert r.status_code == 200, r.text
    files, index = parse_export(r.content)

    target = layout.images[1]
    target_hex = target["descriptor"]["digest"].split(":")[1]
    att_hex = layout.attestation_desc["digest"].split(":")[1]
    att_manifest = json.loads(files[f"blobs/sha256/{att_hex}"])

    # the attestation survives, still pointing at the original manifest
    assert att_manifest["annotations"][ANNOTATION_REF_DIGEST] == f"sha256:{target_hex}"
    assert att_hex in blob_hexes(files)
    # ...but the index still lists only the image manifest
    assert [m["digest"] for m in index["manifests"]] == [f"sha256:{target_hex}"]


def test_export_drops_other_platform_attestations(client):
    layout = multi_platform_layout()
    # attestation for the amd64 image must not follow the arm64 export
    other = layout.add_attestation(
        layout.images[0]["descriptor"]["digest"].split(":")[1], content=b"other"
    )
    data = layout.to_bytes(index_extra=[other])
    r = export(client, data, arch="arm64")
    assert r.status_code == 200, r.text
    files, _ = parse_export(r.content)
    assert other["digest"].split(":")[1] not in blob_hexes(files)


def test_export_variant_exact_match(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="arm64", variant="v8")
    assert r.status_code == 200, r.text
    files, index = parse_export(r.content)
    target_hex = layout.images[2]["descriptor"]["digest"].split(":")[1]
    assert index["manifests"][0]["digest"] == f"sha256:{target_hex}"
    assert files[f"blobs/sha256/{target_hex}"] == layout.blobs[target_hex]


def test_export_without_variant_ignores_variant_entries(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="arm64")
    assert r.status_code == 200, r.text
    _, index = parse_export(r.content)
    target_hex = layout.images[1]["descriptor"]["digest"].split(":")[1]
    assert index["manifests"][0]["digest"] == f"sha256:{target_hex}"


def test_export_variant_does_not_match_missing_variant(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="amd64", variant="v2")
    assert r.status_code == 400
    assert "no image" in r.json()["detail"]


def test_export_shared_blobs_written_once(client):
    layout = Layout()
    layout.images = []
    # both platforms share the same layer blob
    shared_layer = layout.add_blob(layer_tar([("etc/shared.txt", b"same")]))
    for arch in ("amd64", "arm64"):
        config_b = json.dumps({
            "architecture": arch, "os": "linux",
            "rootfs": {"diff_ids": [f"sha256:{sha(layer_tar([('etc/shared.txt', b'same')]))}"]},
        }).encode()
        config_hex = layout.add_blob(config_b)
        manifest_b = json.dumps({
            "schemaVersion": 2, "mediaType": MT_MANIFEST,
            "config": {"mediaType": MT_CONFIG, "digest": f"sha256:{config_hex}",
                       "size": len(config_b)},
            "layers": [{"mediaType": MT_LAYER, "digest": f"sha256:{shared_layer}",
                        "size": len(layout.blobs[shared_layer])}],
        }, sort_keys=True).encode()
        mhex = layout.add_blob(manifest_b)
        layout.images.append({
            "descriptor": {"mediaType": MT_MANIFEST, "digest": f"sha256:{mhex}",
                           "size": len(manifest_b), "platform": {"os": "linux",
                                                                 "architecture": arch}},
            "manifest": {"layers": [{"digest": f"sha256:{shared_layer}"}]},
        })
    # the same blob appears in the layer list of both manifests too
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 200, r.text
    files, _ = parse_export(r.content)
    names = [n for n in files if n.endswith(shared_layer)]
    assert names == [f"blobs/sha256/{shared_layer}"]


def test_export_with_sub_index(client):
    layout = multi_platform_layout()
    layout.make_sub_index()
    data = layout.to_bytes(direct=False)
    r = export(client, data, arch="arm64", variant="v8")
    assert r.status_code == 200, r.text
    files, index = parse_export(r.content)
    target_hex = layout.images[2]["descriptor"]["digest"].split(":")[1]
    # output lists the manifest directly, no sub-index indirection
    assert [m["digest"] for m in index["manifests"]] == [f"sha256:{target_hex}"]


def test_export_platform_docker_media_types(client):
    layout = Layout()
    layout.images = []
    image = layout.add_image(arch="s390x")
    image["descriptor"]["mediaType"] = (
        "application/vnd.docker.distribution.manifest.v2+json"
    )
    layout.images.append(image)
    r = export(client, layout.to_bytes(), arch="s390x")
    assert r.status_code == 200, r.text
    _, index = parse_export(r.content)
    assert index["manifests"][0]["mediaType"] == (
        "application/vnd.docker.distribution.manifest.v2+json"
    )


# ---------------------------------------------------------------------------
# mismatch / error paths
# ---------------------------------------------------------------------------


def test_export_missing_platform(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), os_name="windows", arch="amd64")
    assert r.status_code == 400
    assert "no image" in r.json()["detail"]


def test_export_os_must_match(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), os_name="linux")
    assert r.status_code == 200
    # and a layout with only windows images does not match linux
    win = Layout()
    win.images = [win.add_image(os_name="windows", arch="amd64")]
    r = export(client, win.to_bytes(), os_name="linux", arch="amd64")
    assert r.status_code == 400


def test_export_ambiguous_platform(client):
    layout = Layout()
    layout.images = [
        layout.add_image(arch="amd64", content=b"one"),
        layout.add_image(arch="amd64", content=b"two"),
    ]
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 400
    assert "matches 2" in r.json()["detail"]


def test_export_missing_unselected_blobs_still_ok(client):
    layout = multi_platform_layout()
    amd64 = layout.images[0]
    r = export(
        client,
        layout.to_bytes(drop={amd64["layer_hex"]}),
        arch="arm64",
    )
    assert r.status_code == 200, r.text


def test_export_missing_selected_blob_fails(client):
    layout = multi_platform_layout()
    amd64 = layout.images[0]
    r = export(
        client,
        layout.to_bytes(drop={amd64["layer_hex"]}),
        arch="amd64",
    )
    assert r.status_code == 400
    assert "missing" in r.json()["detail"]


def test_export_corrupt_selected_layer_fails(client):
    layout = multi_platform_layout()
    amd64 = layout.images[0]
    data = bytearray(layout.blobs[amd64["layer_hex"]])
    data[0] ^= 0xFF
    layout.blobs[amd64["layer_hex"]] = bytes(data)
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 400
    assert "sha256 mismatch" in r.json()["detail"]


def test_export_corrupt_attestation_blob_fails(client):
    layout = multi_platform_layout()
    att_layer = layout.last_attestation_layer
    data = bytearray(layout.blobs[att_layer])
    data[0] ^= 0xFF
    layout.blobs[att_layer] = bytes(data)
    r = export(client, layout.to_bytes(), arch="arm64")
    assert r.status_code == 400
    assert "sha256 mismatch" in r.json()["detail"]


def test_export_truncated_selected_layer_fails(client):
    layout = multi_platform_layout()
    amd64 = layout.images[0]
    layout.blobs[amd64["layer_hex"]] = layout.blobs[amd64["layer_hex"]][:-1]
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 400
    assert "size mismatch" in r.json()["detail"]


def test_export_rejects_invalid_platform_params(client):
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="../etc")
    assert r.status_code == 400
    assert "invalid architecture" in r.json()["detail"]


def test_export_reads_blobs_in_chunks(client, monkeypatch):
    """Blobs are streamed through bounded reads, never slurped whole."""
    from app import exporter

    sizes = []
    original = exporter._HashingReader.read

    def spy(self, size=-1):
        data = original(self, size)
        sizes.append(len(data))
        return data

    monkeypatch.setattr(exporter._HashingReader, "read", spy)
    layout = multi_platform_layout()
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 200, r.text
    assert sizes
    assert max(sizes) <= exporter.CHUNK


def test_export_not_a_tar(client):
    r = export(client, b"this is not a tar at all")
    assert r.status_code == 400


def test_export_failure_leaves_no_partial_file(client):
    layout = multi_platform_layout()
    amd64 = layout.images[0]
    data = bytearray(layout.blobs[amd64["layer_hex"]])
    data[0] ^= 0xFF
    layout.blobs[amd64["layer_hex"]] = bytes(data)
    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 400
    # a valid request right after still works
    r = export(client, multi_platform_layout().to_bytes(), arch="amd64")
    assert r.status_code == 200


def test_export_output_roundtrips_through_unpack(client, tmp_path):
    """A pruned export is itself a valid single-manifest layout."""
    sample_path = tmp_path / "sample.tar"
    build_sample_image(str(sample_path))
    data = sample_path.read_bytes()

    # wrap the single sample image in a multi-platform layout by adding a
    # second (arm64) image next to it
    files = read_files(data)
    layout = Layout()
    layout.images = []
    image = layout.add_image(arch="arm64", content=b"arm64")
    layout.images.append(image)
    for name, raw in files.items():
        if name.startswith("blobs/sha256/"):
            layout.blobs.setdefault(name.split("/")[-1], raw)
    index = json.loads(files["index.json"])
    layout.images.insert(0, {"descriptor": index["manifests"][0]})

    r = export(client, layout.to_bytes(), arch="amd64")
    assert r.status_code == 200, r.text
    r = client.post(
        "/unpack", files={"file": ("image.tar", r.content, "application/x-tar")}
    )
    assert r.status_code == 200, r.text
    rootfs = {}
    with tarfile.open(fileobj=io.BytesIO(r.content), mode="r:") as tf:
        for m in tf.getmembers():
            if m.isreg():
                rootfs[m.name] = tf.extractfile(m).read()
    assert rootfs["bin/tool"] == b"tool-v2\n"
