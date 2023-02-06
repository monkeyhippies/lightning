# Copyright The Lightning AI team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from lightning_fabric.plugins.precision.deepspeed import DeepSpeedPrecision
from lightning_fabric.plugins.precision.double import DoublePrecision
from lightning_fabric.plugins.precision.fsdp import FSDPPrecision
from lightning_fabric.plugins.precision.native_amp import MixedPrecision
from lightning_fabric.plugins.precision.precision import Precision
from lightning_fabric.plugins.precision.tpu import TPUPrecision
from lightning_fabric.plugins.precision.tpu_bf16 import TPUBf16Precision

__all__ = [
    "DeepSpeedPrecision",
    "DoublePrecision",
    "MixedPrecision",
    "Precision",
    "TPUPrecision",
    "TPUBf16Precision",
    "FSDPPrecision",
]
