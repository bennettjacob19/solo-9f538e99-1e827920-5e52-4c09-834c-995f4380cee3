"""Export one platform's image from an OCI Image Layout tar.

The uploaded layout may contain many platforms (optionally behind a
sub-index) and BuildKit provenance manifests.  The export keeps exactly
one image manifest plus the BuildKit attestations whose
``vnd.docker.reference.digest`` annotation points at that manifest, and
drops every other blob.

Guarantees provided by this module:

* platform selection is exact: ``os`` and ``architecture`` must match the
  image config verbatim, and a requested ``variant`` matches only entries
  whose config declares that same variant (omitting ``variant`` matches
  only entries that declare none);
* the target must be unique -- zero or several matches fail the request;
* every retained manifest, config and layer blob is checked for presence,
  exact size and SHA-256, streamed in chunks; blobs that are not retained
  are never read fully and never written;
* retained blobs are copied byte-for-byte (layers are never decompressed
  or otherwise rewritten) and a blob shared by several descriptors is
  written exactly once;
* ``index.json`` is rewritten to list only the retained image manifest
  (its descriptor is rebuilt for the retained bytes); ``oci-layout`` is
  copied verbatim;
* blobs of platforms that were not selected may be missing entirely --
  the export still succeeds as long as retained content verifies.
"""

from __future__ import annotations

import hashlib
import json
import os
import tarfile

from .unpacker import CHUNK, UnpackError, _parse_digest

MT_INDEX = "application/vnd.oci.image.index.v1+json"
MT_INDEX_DOCKER = "application/vnd.docker.distribution.manifest.list.v2+json"
MT_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
MT_MANIFEST_DOCKER = "application/vnd.docker.distribution.manifest.v2+json"

INDEX_TYPES = {MT_INDEX, MT_INDEX_DOCKER}
MANIFEST_TYPES = {MT_MANIFEST, MT_MANIFEST_DOCKER}

ANNOTATION_REF_DIGEST = "vnd.docker.reference.digest"
ANNOTATION_REF_TYPE = "vnd.docker.reference.type"
ATTESTATION_TYPE = "attestation"

BLOB_DIR = "blobs/sha256"

FIXED_MTIME = 0  # deterministic output


class _HashingReader:
    """Wrap a blob stream, hashing exactly the bytes handed to tarfile."""

    def __init__(self, stream):
        self._stream = stream
        self._hash = hashlib.sha256()
        self.count = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = CHUNK
        data = self._stream.read(size)
        self._hash.update(data)
        self.count += len(data)
        return data

    def hexdigest(self):
        return self._hash.hexdigest()


class _LayoutTar:
    """Read-only, name-addressed view of the uploaded layout tar.

    Only regular files are addressable; ``./`` prefixes are normalised and
    entries with absolute or ``..`` paths are ignored (they can never be
    addressed, and nothing is extracted to disk).
    """

    def __init__(self, path):
        try:
            self._tf = tarfile.open(path, "r:*")
        except (tarfile.TarError, OSError) as e:
            raise UnpackError(f"cannot read uploaded tar: {e}")
        self._members = {}
        try:
            for member in self._tf:
                if not member.isreg():
                    continue
                name = _normalize_member_name(member.name)
                if name is not None:
                    # A later duplicate wins, like a normal tar extraction.
                    self._members[name] = member
        except (tarfile.TarError, OSError) as e:
            self.close()
            raise UnpackError(f"cannot read uploaded tar: {e}")

    def close(self):
        try:
            self._tf.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _open(self, name):
        member = self._members.get(name)
        if member is None:
            return None
        try:
            return self._tf.extractfile(member)
        except (tarfile.TarError, OSError) as e:
            raise UnpackError(f"cannot read {name!r}: {e}")

    def open_name(self, name):
        return self._open(name)

    def open_blob(self, hexd):
        return self._open(f"{BLOB_DIR}/{hexd}")

    def read_name(self, name, what):
        stream = self._open(name)
        if stream is None:
            raise UnpackError(f"missing {what}")
        with stream:
            try:
                return stream.read()
            except (tarfile.TarError, OSError) as e:
                raise UnpackError(f"cannot read {what}: {e}")

    def read_json(self, name, what):
        raw = self.read_name(name, what)
        return _parse_json(raw, what)


def _normalize_member_name(name):
    if not isinstance(name, str) or "\x00" in name:
        return None
    if name.startswith("/"):
        return None
    parts = []
    for part in name.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    return "/".join(parts)


def _parse_json(raw, what):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise UnpackError(f"invalid {what}: {e}")
    if not isinstance(value, dict):
        raise UnpackError(f"invalid {what}: not a JSON object")
    return value


def _descriptor(value, what):
    """Validate a descriptor; return (hex digest, size, mediaType)."""
    if not isinstance(value, dict):
        raise UnpackError(f"{what}: invalid descriptor")
    hexd = _parse_digest(value.get("digest"), what)
    size = value.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise UnpackError(f"{what}: descriptor missing size")
    return hexd, size, value.get("mediaType")


def _verified_json(src, hexd, size, what):
    raw = src.read_name(f"{BLOB_DIR}/{hexd}", what)
    if len(raw) != size:
        raise UnpackError(
            f"{what}: size mismatch for sha256:{hexd} "
            f"(expected {size}, got {len(raw)})"
        )
    if hashlib.sha256(raw).hexdigest() != hexd:
        raise UnpackError(f"{what}: sha256 mismatch for blob sha256:{hexd}")
    return raw, _parse_json(raw, what)


def _platform(config, entry_platform=None):
    """Effective platform of one image: config values win.

    Docker-format manifests sometimes only carry the platform in the index
    descriptor (or omit the variant from the config), so the descriptor is
    used as a fallback for whatever the config leaves empty.
    """
    merged = {}
    if isinstance(entry_platform, dict):
        for key in ("os", "architecture", "variant"):
            value = entry_platform.get(key)
            if isinstance(value, str) and value:
                merged[key] = value
    for key in ("os", "architecture", "variant"):
        value = config.get(key)
        if isinstance(value, str) and value:
            merged[key] = value
    os_name = merged.get("os")
    arch = merged.get("architecture")
    variant = merged.get("variant")
    if not os_name:
        raise UnpackError("image config: missing os")
    if not arch:
        raise UnpackError("image config: missing architecture")
    return os_name, arch, variant


def _normalize_variant(variant):
    return None if variant in (None, "") else variant


def _manifest_entries(src, index):
    """Resolve index.json to a list of image-manifest descriptors.

    Two shapes are supported: index.json listing image manifests directly,
    or index.json referencing exactly one sub-index that lists them.
    """
    manifests = index.get("manifests")
    if not isinstance(manifests, list) or not manifests:
        raise UnpackError("index.json: no manifests")

    index_entries = []
    for i, entry in enumerate(manifests):
        if not isinstance(entry, dict):
            raise UnpackError(f"index.json manifest {i}: invalid descriptor")
        media_type = entry.get("mediaType")
        if media_type in INDEX_TYPES:
            index_entries.append((entry, media_type))
        elif isinstance(media_type, str) and media_type not in MANIFEST_TYPES:
            # Unknown artifacts (signatures, SBOMs, ...) are simply not
            # image manifests; they are never retained.
            continue
        if not isinstance(media_type, str):
            raise UnpackError(f"index.json manifest {i}: missing mediaType")

    if not index_entries:
        return manifests
    if len(index_entries) > 1 or len(manifests) != 1:
        raise UnpackError(
            "index.json must either list image manifests directly or "
            "reference exactly one sub-index"
        )

    entry, _ = index_entries[0]
    hexd, size, _ = _descriptor(entry, "sub-index")
    _, sub_index = _verified_json(src, hexd, size, "sub-index")
    sub_manifests = sub_index.get("manifests")
    if not isinstance(sub_manifests, list) or not sub_manifests:
        raise UnpackError("sub-index: no manifests")
    for i, sub_entry in enumerate(sub_manifests):
        if not isinstance(sub_entry, dict):
            raise UnpackError(f"sub-index manifest {i}: invalid descriptor")
        media_type = sub_entry.get("mediaType")
        if media_type in INDEX_TYPES:
            raise UnpackError("sub-index: nested indexes are not supported")
        if media_type not in MANIFEST_TYPES:
            raise UnpackError(
                f"sub-index manifest {i}: unsupported mediaType {media_type!r}"
            )
    return sub_manifests


class _Candidate:
    __slots__ = ("entry", "hex", "size", "manifest", "os", "architecture", "variant")

    def __init__(self, entry, hexd, size, manifest, platform):
        self.entry = entry
        self.hex = hexd
        self.size = size
        self.manifest = manifest
        self.os, self.architecture, self.variant = platform


def _discover(src, entries):
    """Parse manifests/configs; unusable candidates are skipped."""
    candidates = []
    manifests = {}  # hex -> (entry, manifest dict)
    for entry in entries:
        try:
            hexd, size, _ = _descriptor(entry, "manifest")
            _, manifest = _verified_json(src, hexd, size, "manifest")
            manifests[hexd] = (entry, manifest)
            config_desc = manifest.get("config")
            chex, csize, _ = _descriptor(config_desc, f"manifest sha256:{hexd} config")
            _, config = _verified_json(
                src, chex, csize, f"manifest sha256:{hexd} config"
            )
            platform = _platform(config, entry.get("platform"))
        except UnpackError:
            # An attestation or foreign artifact is not an image and may
            # legitimately be unusable (e.g. the blobs it needs were pruned
            # from another platform); that must not stop the export.
            continue
        candidates.append(_Candidate(entry, hexd, size, manifest, platform))
    return candidates, manifests


def _select(candidates, os_name, architecture, variant):
    want_variant = _normalize_variant(variant)
    matches = [
        c
        for c in candidates
        if c.os == os_name
        and c.architecture == architecture
        and c.variant == want_variant
    ]
    label = f"{os_name}/{architecture}"
    if want_variant is not None:
        label += f"/{want_variant}"
    if not matches:
        raise UnpackError(f"no image found for platform {label}")
    if len(matches) > 1:
        raise UnpackError(
            f"platform {label} matches {len(matches)} images; "
            "refusing to export an ambiguous selection"
        )
    return matches[0]


def _is_attestation_of(manifest, target_hex):
    annotations = manifest.get("annotations")
    if not isinstance(annotations, dict):
        return False
    if annotations.get(ANNOTATION_REF_DIGEST) != f"sha256:{target_hex}":
        return False
    ref_type = annotations.get(ANNOTATION_REF_TYPE)
    return ref_type in (None, ATTESTATION_TYPE)


def _output_descriptor(entry, size):
    desc = {
        "mediaType": entry["mediaType"],
        "digest": entry["digest"],
        "size": size,
    }
    for key in ("annotations", "platform"):
        if isinstance(entry.get(key), dict):
            desc[key] = entry[key]
    return desc


def _add_bytes(tf, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = FIXED_MTIME
    info.uid = info.gid = 0
    tf.addfile(info, _BytesReader(data))


class _BytesReader:
    def __init__(self, data):
        self._data = data
        self._pos = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._pos
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


def _add_dir(tf, name):
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    info.mtime = FIXED_MTIME
    info.uid = info.gid = 0
    tf.addfile(info)


def _copy_blob(tf, src, hexd, size, what, written):
    """Verify and copy one blob in chunks, byte-for-byte; dedupe by digest."""
    if hexd in written:
        return
    stream = src.open_blob(hexd)
    if stream is None:
        raise UnpackError(f"{what}: missing blob sha256:{hexd}")

    info = tarfile.TarInfo(f"{BLOB_DIR}/{hexd}")
    info.size = size
    info.mode = 0o644
    info.mtime = FIXED_MTIME
    info.uid = info.gid = 0

    with stream:
        reader = _HashingReader(stream)
        try:
            tf.addfile(info, reader)
        except (OSError, tarfile.TarError) as e:
            if reader.count != size:
                raise UnpackError(
                    f"{what}: size mismatch for sha256:{hexd} "
                    f"(expected {size}, got {reader.count})"
                )
            raise UnpackError(f"{what}: cannot read blob sha256:{hexd}: {e}")
        if reader.count != size:
            raise UnpackError(
                f"{what}: size mismatch for sha256:{hexd} "
                f"(expected {size}, got {reader.count})"
            )
        if reader.hexdigest() != hexd:
            raise UnpackError(f"{what}: sha256 mismatch for blob sha256:{hexd}")
    written.add(hexd)


def _retained_blobs(candidate):
    """Yield (hex, size, what) for the manifest, config and layers."""
    hexd, size, _ = _descriptor(candidate.entry, "manifest")
    yield hexd, size, "manifest"

    config_desc = candidate.manifest.get("config")
    chex, csize, _ = _descriptor(config_desc, "config")
    yield chex, csize, "config"

    layers = candidate.manifest.get("layers")
    if not isinstance(layers, list):
        raise UnpackError(f"manifest sha256:{candidate.hex}: missing layers")
    for i, layer in enumerate(layers):
        lhex, lsize, _ = _descriptor(layer, f"layer {i}")
        yield lhex, lsize, f"layer {i}"


def _attestations_for(src, entries, target, manifests):
    """Find attestation manifests that reference *target*.

    A manifest blob that cannot be read (pruned other-platform blobs) or
    cannot be parsed is skipped -- it cannot be identified as a proof of
    the target.  Anything identified is retained *and verified* while the
    output is written, so a corrupt proof fails the export rather than
    silently disappearing.
    """
    attested = []
    seen = set()
    for entry in entries:
        try:
            hexd, size, _ = _descriptor(entry, "manifest")
        except UnpackError:
            continue
        if hexd in seen or hexd == target.hex:
            continue
        seen.add(hexd)

        known = manifests.get(hexd)
        if known is not None:
            if _is_attestation_of(known[1], target.hex):
                attested.append(known)
            continue

        # Not a usable image candidate (e.g. a corrupt config).  Parse the
        # manifest bytes without trusting them first, so an identified
        # attestation still gets verified on copy instead of vanishing.
        try:
            raw = src.read_name(f"{BLOB_DIR}/{hexd}", "manifest")
            manifest = _parse_json(raw, "manifest")
        except UnpackError:
            continue
        if _is_attestation_of(manifest, target.hex):
            attested.append((entry, manifest))
    return attested


def _write_export(src, out_path, target, attested):
    tmp = out_path + ".tmp"
    try:
        with tarfile.open(tmp, "w", format=tarfile.PAX_FORMAT) as tf:
            _add_bytes(tf, "oci-layout", src.read_name("oci-layout", "oci-layout"))

            index = {
                "schemaVersion": 2,
                "mediaType": MT_INDEX,
                "manifests": [
                    _output_descriptor(target.entry, target.size)
                ],
            }
            _add_bytes(tf, "index.json", json.dumps(index).encode())

            _add_dir(tf, "blobs")
            _add_dir(tf, "blobs/sha256")

            written = set()
            for hexd, size, what in _retained_blobs(target):
                _copy_blob(tf, src, hexd, size, what, written)

            for entry, manifest in attested:
                hexd, size, _ = _descriptor(entry, "attestation manifest")
                _copy_blob(tf, src, hexd, size, "attestation manifest", written)
                config_desc = manifest.get("config")
                chex, csize, _ = _descriptor(config_desc, "attestation config")
                _copy_blob(tf, src, chex, csize, "attestation config", written)
                layers = manifest.get("layers")
                if not isinstance(layers, list):
                    raise UnpackError(
                        f"attestation manifest sha256:{hexd}: missing layers"
                    )
                for i, layer in enumerate(layers):
                    lhex, lsize, _ = _descriptor(layer, f"attestation layer {i}")
                    _copy_blob(
                        tf, src, lhex, lsize, f"attestation layer {i}", written
                    )
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def export_platform(
    upload_path, out_path, os_name, architecture, variant=None
):
    """Write a pruned OCI layout tar for one platform; return *out_path*.

    Raises :class:`UnpackError` when the target platform is missing or
    ambiguous, or when any retained blob fails verification.
    """
    with _LayoutTar(upload_path) as src:
        src.read_json("oci-layout", "oci-layout")
        index = src.read_json("index.json", "index.json")
        entries = _manifest_entries(src, index)
        candidates, manifests = _discover(src, entries)
        target = _select(candidates, os_name, architecture, variant)

        attested = _attestations_for(src, entries, target, manifests)

        _write_export(src, out_path, target, attested)
    return out_path
