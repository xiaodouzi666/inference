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
import logging
import os
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ...constants import XINFERENCE_CACHE_DIR
from ..core import CacheableModelSpec, ModelDescription
from ..utils import valid_model_revision
from .whisper import WhisperModel

MAX_ATTEMPTS = 3

logger = logging.getLogger(__name__)

# Used for check whether the model is cached.
# Init when registering all the builtin models.
MODEL_NAME_TO_REVISION: Dict[str, List[str]] = defaultdict(list)
AUDIO_MODEL_DESCRIPTIONS: Dict[str, List[Dict]] = defaultdict(list)


def get_audio_model_descriptions():
    import copy

    return copy.deepcopy(AUDIO_MODEL_DESCRIPTIONS)


class AudioModelFamilyV1(CacheableModelSpec):
    model_family: str
    model_name: str
    model_id: str
    model_revision: str
    multilingual: bool


class AudioModelDescription(ModelDescription):
    def __init__(
        self,
        address: Optional[str],
        devices: Optional[List[str]],
        model_spec: AudioModelFamilyV1,
        model_path: Optional[str] = None,
    ):
        super().__init__(address, devices, model_path=model_path)
        self._model_spec = model_spec

    def to_dict(self):
        return {
            "model_type": "audio",
            "address": self.address,
            "accelerators": self.devices,
            "model_name": self._model_spec.model_name,
            "model_family": self._model_spec.model_family,
            "model_revision": self._model_spec.model_revision,
        }

    def to_version_info(self):
        from .utils import get_model_version

        if self._model_path is None:
            is_cached = get_cache_status(self._model_spec)
            file_location = get_cache_dir(self._model_spec)
        else:
            is_cached = True
            file_location = self._model_path

        return {
            "model_version": get_model_version(self._model_spec),
            "model_file_location": file_location,
            "cache_status": is_cached,
        }


def generate_audio_description(
    image_model: AudioModelFamilyV1,
) -> Dict[str, List[Dict]]:
    res = defaultdict(list)
    res[image_model.model_name].append(
        AudioModelDescription(None, None, image_model).to_version_info()
    )
    return res


def match_audio(model_name: str) -> AudioModelFamilyV1:
    from . import BUILTIN_AUDIO_MODELS
    from .custom import get_user_defined_audios

    for model_spec in get_user_defined_audios():
        if model_spec.model_name == model_name:
            return model_spec

    if model_name in BUILTIN_AUDIO_MODELS:
        return BUILTIN_AUDIO_MODELS[model_name]
    else:
        raise ValueError(
            f"Audio model {model_name} not found, available"
            f"model list: {BUILTIN_AUDIO_MODELS.keys()}"
        )


def cache(model_spec: AudioModelFamilyV1):
    from ..utils import cache

    return cache(model_spec, AudioModelDescription)


def get_cache_dir(model_spec: AudioModelFamilyV1):
    return os.path.realpath(os.path.join(XINFERENCE_CACHE_DIR, model_spec.model_name))


def get_cache_status(
    model_spec: AudioModelFamilyV1,
) -> bool:
    cache_dir = get_cache_dir(model_spec)
    meta_path = os.path.join(cache_dir, "__valid_download")
    return valid_model_revision(meta_path, model_spec.model_revision)


def create_audio_model_instance(
    subpool_addr: str, devices: List[str], model_uid: str, model_name: str, **kwargs
) -> Tuple[WhisperModel, AudioModelDescription]:
    model_spec = match_audio(model_name)
    model_path = cache(model_spec)
    model = WhisperModel(model_uid, model_path, model_spec, **kwargs)
    model_description = AudioModelDescription(
        subpool_addr, devices, model_spec, model_path=model_path
    )
    return model, model_description
