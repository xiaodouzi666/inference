# Copyright 2022-2023 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import json
import logging
import os
import shutil
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple, Union

from fsspec import AbstractFileSystem

from ..constants import XINFERENCE_CACHE_DIR, XINFERENCE_ENV_MODEL_SRC
from ..device_utils import get_available_device, is_device_available
from .core import CacheableModelSpec

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


def is_locale_chinese_simplified() -> bool:
    import locale

    try:
        lang, _ = locale.getdefaultlocale()
        return lang == "zh_CN"
    except:
        return False


def download_from_modelscope() -> bool:
    if os.environ.get(XINFERENCE_ENV_MODEL_SRC) == "modelscope":
        return True
    elif is_locale_chinese_simplified():
        return True
    else:
        return False


def symlink_local_file(path: str, local_dir: str, relpath: str) -> str:
    from huggingface_hub.file_download import _create_symlink

    # cross-platform transcription of filename, to be used as a local file path.
    relative_filename = os.path.join(*relpath.split("/"))
    if os.name == "nt":
        if relative_filename.startswith("..\\") or "\\..\\" in relative_filename:
            raise ValueError(
                f"Invalid filename: cannot handle filename '{relative_filename}' on Windows. Please ask the repository"
                " owner to rename this file."
            )
    # Using `os.path.abspath` instead of `Path.resolve()` to avoid resolving symlinks
    local_dir_filepath = os.path.join(local_dir, relative_filename)
    if (
        Path(os.path.abspath(local_dir))
        not in Path(os.path.abspath(local_dir_filepath)).parents
    ):
        raise ValueError(
            f"Cannot copy file '{relative_filename}' to local dir '{local_dir}': file would not be in the local"
            " directory."
        )

    os.makedirs(os.path.dirname(local_dir_filepath), exist_ok=True)
    real_blob_path = os.path.realpath(path)
    _create_symlink(real_blob_path, local_dir_filepath, new_blob=False)
    return local_dir_filepath


def retry_download(
    download_func: Callable,
    model_name: str,
    model_info: Optional[Dict],
    *args,
    **kwargs,
):
    last_ex = None
    for current_attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return download_func(*args, **kwargs)
        except Exception as e:
            remaining_attempts = MAX_ATTEMPTS - current_attempt
            last_ex = e
            logger.debug(
                "Download failed: %s, download func: %s, download args: %s, kwargs: %s",
                e,
                download_func,
                args,
                kwargs,
            )
            logger.warning(
                f"Attempt {current_attempt} failed. Remaining attempts: {remaining_attempts}"
            )

    else:
        model_size = (
            model_info.pop("model_size", None) if model_info is not None else None
        )
        model_format = (
            model_info.pop("model_format", None) if model_info is not None else None
        )
        if model_size is not None or model_format is not None:  # LLM models
            raise RuntimeError(
                f"Failed to download model '{model_name}' "
                f"(size: {model_size}, format: {model_format}) "
                f"after multiple retries"
            ) from last_ex
        else:  # Embedding models
            raise RuntimeError(
                f"Failed to download model '{model_name}' after multiple retries"
            ) from last_ex


def valid_model_revision(
    meta_path: str,
    expected_model_revision: Optional[str],
    expected_model_hub: Optional[str] = None,
) -> bool:
    if not os.path.exists(meta_path):
        return False
    with open(meta_path, "r") as f:
        try:
            meta_data = json.load(f)
        except JSONDecodeError:  # legacy meta file for embedding models
            logger.debug("Legacy meta file detected.")
            return True

        if "model_revision" in meta_data:  # embedding, image
            real_revision = meta_data["model_revision"]
        elif "revision" in meta_data:  # llm
            real_revision = meta_data["revision"]
        else:
            logger.warning(
                f"No `revision` information in the `__valid_download` file. "
            )
            return False
        if expected_model_hub is not None and expected_model_hub != meta_data.get(
            "model_hub", "huggingface"
        ):
            logger.info("Use model cache from a different hub.")
            return True
        else:
            return real_revision == expected_model_revision


def get_cache_dir(model_spec: Any) -> str:
    return os.path.realpath(os.path.join(XINFERENCE_CACHE_DIR, model_spec.model_name))


def is_model_cached(model_spec: Any, name_to_revisions_mapping: Dict):
    cache_dir = get_cache_dir(model_spec)
    meta_path = os.path.join(cache_dir, "__valid_download")
    revisions = name_to_revisions_mapping[model_spec.model_name]
    if model_spec.model_revision not in revisions:  # Usually for UT
        revisions.append(model_spec.model_revision)
    return any([valid_model_revision(meta_path, revision) for revision in revisions])


def is_valid_model_name(model_name: str) -> bool:
    import re

    if len(model_name) == 0:
        return False

    # check if contains +/?%#&=\s
    return re.match(r"^[^+\/?%#&=\s]*$", model_name) is not None


def parse_uri(uri: str) -> Tuple[str, str]:
    import glob
    from urllib.parse import urlparse

    if os.path.exists(uri) or glob.glob(uri):
        return "file", uri
    else:
        parsed = urlparse(uri)
        scheme = parsed.scheme
        path = parsed.netloc + parsed.path
        if parsed.scheme == "" or len(parsed.scheme) == 1:  # len == 1 for windows
            scheme = "file"
        return scheme, path


def is_valid_model_uri(model_uri: Optional[str]) -> bool:
    if not model_uri:
        return False

    src_scheme, src_root = parse_uri(model_uri)

    if src_scheme == "file":
        if not os.path.isabs(src_root):
            raise ValueError(f"Model URI cannot be a relative path: {model_uri}")
        return os.path.exists(src_root)
    else:
        # TODO: handle other schemes.
        return True


def cache_from_uri(
    model_spec: CacheableModelSpec,
    self_hosted_storage: bool = False,
) -> str:
    from fsspec import AbstractFileSystem, filesystem

    cache_dir = os.path.realpath(
        os.path.join(XINFERENCE_CACHE_DIR, model_spec.model_name)
    )
    if os.path.exists(cache_dir):
        logger.info("cache %s exists", cache_dir)
        return cache_dir

    assert model_spec.model_uri is not None
    src_scheme, src_root = parse_uri(model_spec.model_uri)
    if src_root.endswith("/"):
        # remove trailing path separator.
        src_root = src_root[:-1]

    if src_scheme == "file":
        if not os.path.isabs(src_root):
            raise ValueError(
                f"Model URI cannot be a relative path: {model_spec.model_uri}"
            )
        os.makedirs(XINFERENCE_CACHE_DIR, exist_ok=True)
        os.symlink(src_root, cache_dir, target_is_directory=True)
        return cache_dir
    elif src_scheme in ["s3"]:
        # use anonymous connection for self-hosted storage.
        src_fs: AbstractFileSystem = filesystem(src_scheme, anon=self_hosted_storage)
        local_fs: AbstractFileSystem = filesystem("file")

        files_to_download = []
        os.makedirs(cache_dir, exist_ok=True)

        for path, _, files in src_fs.walk(model_spec.model_uri):
            for file in files:
                src_path = f"{path}/{file}"
                local_path = src_path.replace(src_root, cache_dir)
                files_to_download.append((src_path, local_path))

        from concurrent.futures import ThreadPoolExecutor

        failed = False
        with ThreadPoolExecutor(max_workers=min(len(files_to_download), 4)) as executor:
            futures = [
                (
                    src_path,
                    executor.submit(
                        copy_from_src_to_dst, src_fs, src_path, local_fs, local_path
                    ),
                )
                for src_path, local_path in files_to_download
            ]
            for src_path, future in futures:
                if failed:
                    future.cancel()
                else:
                    try:
                        future.result()
                    except:
                        logger.error(f"Download {src_path} failed", exc_info=True)
                        failed = True

        if failed:
            logger.warning(f"Removing cache directory: {cache_dir}")
            shutil.rmtree(cache_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to download model '{model_spec.model_name}' ")
        return cache_dir
    else:
        raise ValueError(f"Unsupported URL scheme: {src_scheme}")


def cache(model_spec: CacheableModelSpec, model_description_type: type):
    if (
        hasattr(model_spec, "model_uri")
        and getattr(model_spec, "model_uri", None) is not None
    ):
        logger.info(f"Model caching from URI: {model_spec.model_uri}")
        return cache_from_uri(model_spec=model_spec)

    cache_dir = os.path.realpath(
        os.path.join(XINFERENCE_CACHE_DIR, model_spec.model_name)
    )
    if not os.path.exists(cache_dir):
        os.makedirs(cache_dir, exist_ok=True)
    meta_path = os.path.join(cache_dir, "__valid_download")
    if valid_model_revision(meta_path, model_spec.model_revision, model_spec.model_hub):
        return cache_dir

    from_modelscope: bool = model_spec.model_hub == "modelscope"
    if from_modelscope:
        from modelscope.hub.snapshot_download import snapshot_download as ms_download

        download_dir = retry_download(
            ms_download,
            model_spec.model_name,
            None,
            model_spec.model_id,
            revision=model_spec.model_revision,
        )
        for subdir, dirs, files in os.walk(download_dir):
            for file in files:
                relpath = os.path.relpath(os.path.join(subdir, file), download_dir)
                symlink_local_file(os.path.join(subdir, file), cache_dir, relpath)
    else:
        from huggingface_hub import snapshot_download as hf_download

        retry_download(
            hf_download,
            model_spec.model_name,
            None,
            model_spec.model_id,
            revision=model_spec.model_revision,
            local_dir=cache_dir,
            local_dir_use_symlinks=True,
        )
    with open(meta_path, "w") as f:
        import json

        desc = model_description_type(None, None, model_spec)
        json.dump(desc.to_dict(), f)
    return cache_dir


def copy_from_src_to_dst(
    _src_fs: "AbstractFileSystem",
    _src_path: str,
    dst_fs: "AbstractFileSystem",
    dst_path: str,
    max_attempt: int = 3,
):
    from tqdm import tqdm

    for attempt in range(max_attempt):
        logger.info(f"Copy from {_src_path} to {dst_path}, attempt: {attempt}")
        try:
            with _src_fs.open(_src_path, "rb") as src_file:
                file_size = _src_fs.info(_src_path)["size"]

                dst_fs.makedirs(os.path.dirname(dst_path), exist_ok=True)
                with dst_fs.open(dst_path, "wb") as dst_file:
                    chunk_size = 1024 * 1024  # 1 MB

                    with tqdm(
                        total=file_size,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                        desc=_src_path,
                    ) as pbar:
                        while True:
                            chunk = src_file.read(chunk_size)
                            if not chunk:
                                break
                            dst_file.write(chunk)
                            pbar.update(len(chunk))
            logger.info(
                f"Copy from {_src_path} to {dst_path} finished, attempt: {attempt}"
            )
            break
        except:
            logger.error(
                f"Failed to copy from {_src_path} to {dst_path} on attempt {attempt + 1}",
                exc_info=True,
            )
            if attempt + 1 == max_attempt:
                raise


def patch_trust_remote_code():
    """sentence-transformers calls transformers without the trust_remote_code=True, some embedding
    models will fail to load, e.g. jina-embeddings-v2-base-en

    :return:
    """
    try:
        from transformers.dynamic_module_utils import resolve_trust_remote_code
    except ImportError:
        logger.error("Patch transformers trust_remote_code failed.")
    else:

        def _patched_resolve_trust_remote_code(*args, **kwargs):
            logger.info("Patched resolve_trust_remote_code: %s %s", args, kwargs)
            return True

        if (
            resolve_trust_remote_code.__code__
            != _patched_resolve_trust_remote_code.__code__
        ):
            resolve_trust_remote_code.__code__ = (
                _patched_resolve_trust_remote_code.__code__
            )


def select_device(device):
    try:
        import torch  # noqa: F401
    except ImportError:
        raise ImportError(
            f"Failed to import module 'torch'. Please make sure 'torch' is installed.\n\n"
        )

    if device == "auto":
        return get_available_device()
    else:
        if not is_device_available(device):
            raise ValueError(f"{device} is unavailable in your environment")

    return device


def convert_float_to_int_or_str(model_size: float) -> Union[int, str]:
    """convert float to int or string

    if float can be presented as int, convert it to int, otherwise convert it to string
    """
    if int(model_size) == model_size:
        return int(model_size)
    else:
        return str(model_size)
