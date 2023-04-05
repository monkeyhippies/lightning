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
from contextlib import contextmanager
from typing import Any, Generator, Literal

import torch
from lightning_utilities.core.apply_func import apply_to_collection
from torch import Tensor
from torch.nn import Module

from lightning.fabric.plugins.precision.precision import Precision
from lightning.fabric.plugins.precision.utils import _convert_fp_tensor


class HalfPrecision(Precision):
    """Plugin for training with half (``torch.bfloat16``) precision."""

    precision: Literal["bf16-true"] = "bf16-true"

    def convert_module(self, module: Module) -> Module:
        return module.bfloat16()

    @contextmanager
    def module_init_context(self) -> Generator[None, None, None]:
        """A context manager to change the default tensor type when
        initializing the parameters in a module.

        See: :meth:`torch.set_default_tensor_type`
        """
        default_dtype = torch.get_default_dtype()
        torch.set_default_dtype(torch.bfloat16)
        yield
        torch.set_default_dtype(default_dtype)

    @contextmanager
    def forward_context(self) -> Generator[None, None, None]:
        """A context manager to change the default tensor type when
        tensors get created during the module's forward.

        See: :meth:`torch.set_default_tensor_type`
        """
        default_dtype = torch.get_default_dtype()
        torch.set_default_dtype(torch.bfloat16)
        yield
        torch.set_default_dtype(default_dtype)

    def convert_input(self, data: Any) -> Any:
        return apply_to_collection(data, function=_convert_fp_tensor, dtype=Tensor, dst_type=torch.bfloat16)

    def convert_output(self, data: Any) -> Any:
        return apply_to_collection(data, function=_convert_fp_tensor, dtype=Tensor, dst_type=torch.get_default_dtype())
