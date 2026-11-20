"""FastAPI service: unpack OCI Image Layout tars and export single-platform
OCI layouts for isolated sites."""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from functools import partial
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from .exporter import export_platform
from .sample_image import build_sample_image
from .unpacker import CHUNK, UnpackError, unpack_image

logger = logging.getLogger("oci-unpacker")

WORK_ROOT = os.environ.get(
    "UNPACK_WORKDIR", os.path.join(tempfile.gettempdir(), "oci-unpacker")
)
SAMPLE_IMAGE_PATH = os.path.join(WORK_ROOT, "sample-image.tar")

# os/architecture/variant become part of the output name; keep them boring.
_SAFE_PART = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(WORK_ROOT, exist_ok=True)
    # Drop leftovers from previous runs so a crash never leaks state between
    # requests.
    for name in os.listdir(WORK_ROOT):
        if name.startswith("req-"):
            shutil.rmtree(os.path.join(WORK_ROOT, name), ignore_errors=True)
    build_sample_image(SAMPLE_IMAGE_PATH)
    yield


app = FastAPI(
    title="OCI Image Unpacker",
    version="1.0.0",
    description=(
        "Upload an OCI Image Layout tar containing exactly one manifest and "
        "get the reconstructed `rootfs.tar` back.\n\n"
        "* verifies blob presence, size and SHA-256, plus config `diff_ids`;\n"
        "* supports plain and gzip layers;\n"
        "* whiteouts / opaque markers only delete lower-layer content;\n"
        "* hardlink overwrite is byte-safe for the other end;\n"
        "* paths and links can never escape the per-request sandbox;\n"
        "* any missing/corrupt/illegal input fails the whole request.\n\n"
        "`POST /export` takes a multi-platform layout and returns a pruned "
        "layout with only the requested os/architecture[/variant], plus the "
        "BuildKit attestations that reference that manifest.\n\n"
        "Try it: download `GET /sample-image`, then upload it to `POST /unpack`."
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": "oci-image-unpacker",
        "docs": "/docs",
        "endpoints": {
            "unpack": "POST /unpack",
            "export_platform": "POST /export",
            "sample_image": "GET /sample-image",
            "healthz": "GET /healthz",
        },
    }


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"status": "ok"}


@app.get(
    "/sample-image",
    summary="Download a sample OCI Image Layout tar",
    response_description="sample-image.tar -- a single-manifest OCI Image Layout "
    "with deletions, an opaque directory and a hardlink overwrite",
    responses={200: {"content": {"application/x-tar": {}}}},
)
def sample_image():
    if not os.path.isfile(SAMPLE_IMAGE_PATH):
        raise HTTPException(status_code=404, detail="sample image not available")
    return FileResponse(
        SAMPLE_IMAGE_PATH, media_type="application/x-tar", filename="sample-image.tar"
    )


def _checked_part(value, field):
    if not _SAFE_PART.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"invalid {field}: {value!r} "
            "(letters, digits, dot, dash and underscore only)",
        )
    return value


def _platform_file_stem(os_name, architecture, variant):
    stem = f"{os_name}-{architecture}"
    if variant:
        stem += f"-{variant}"
    return stem


@app.post(
    "/export",
    summary="Export one platform's OCI Image Layout tar",
    response_description="A pruned OCI Image Layout tar for the requested platform",
    responses={
        200: {"content": {"application/x-tar": {}}},
        400: {
            "description": "Target platform missing or ambiguous, or retained "
            "blobs fail verification -- nothing is returned."
        },
    },
)
def export(
    file: UploadFile = File(
        ...,
        description="Full, uncompressed OCI Image Layout tar, possibly "
        "multi-platform and with BuildKit attestations",
    ),
    os_name: str = Form(..., alias="os", description="Target operating system, e.g. linux"),
    architecture: str = Form(..., description="Target architecture, e.g. arm64"),
    variant: Optional[str] = Form(
        None,
        description="Optional target variant, e.g. v8. Omit to match only "
        "entries that declare no variant.",
    ),
):
    os_name = _checked_part(os_name, "os")
    architecture = _checked_part(architecture, "architecture")
    if variant is not None:
        variant = _checked_part(variant, "variant")

    work = tempfile.mkdtemp(prefix="req-", dir=WORK_ROOT)
    try:
        upload_path = os.path.join(work, "upload.tar")
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out, CHUNK)
        stem = _platform_file_stem(os_name, architecture, variant)
        out_path = export_platform(
            upload_path,
            os.path.join(work, "export.tar"),
            os_name,
            architecture,
            variant,
        )
    except UnpackError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        logger.exception("unexpected error while exporting")
        raise HTTPException(status_code=500, detail="internal error")
    return FileResponse(
        out_path,
        media_type="application/x-tar",
        filename=f"{stem}.tar",
        background=BackgroundTask(partial(shutil.rmtree, work, ignore_errors=True)),
    )


@app.post(
    "/unpack",
    summary="Unpack an OCI Image Layout tar into rootfs.tar",
    response_description="The reconstructed root filesystem as rootfs.tar",
    responses={
        200: {"content": {"application/x-tar": {}}},
        400: {
            "description": "Missing blobs, corrupt data, digest/diff_id mismatch "
            "or illegal paths -- nothing is returned."
        },
    },
)
def unpack(
    file: UploadFile = File(
        ..., description="OCI Image Layout tar containing exactly one manifest"
    ),
):
    # Every request gets its own scratch directory: requests never interfere
    # with each other, and a failed request leaves nothing behind.
    work = tempfile.mkdtemp(prefix="req-", dir=WORK_ROOT)
    try:
        upload_path = os.path.join(work, "upload.tar")
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out, CHUNK)
        rootfs_tar = unpack_image(upload_path, work)
    except UnpackError as exc:
        shutil.rmtree(work, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        logger.exception("unexpected error while unpacking")
        raise HTTPException(status_code=500, detail="internal error")
    return FileResponse(
        rootfs_tar,
        media_type="application/x-tar",
        filename="rootfs.tar",
        background=BackgroundTask(partial(shutil.rmtree, work, ignore_errors=True)),
    )
