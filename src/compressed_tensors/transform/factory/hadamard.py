# Copyright (c) 2021 - present / Neuralmagic, Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Optional, Union

import math
import torch
from compressed_tensors.transform import TransformArgs, TransformScheme
from compressed_tensors.transform.factory.base import TransformBase, TransformFactory
from compressed_tensors.transform.utils.hadamard import deterministic_hadamard_matrix
from compressed_tensors.transform.utils.matrix import (
    apply_transform_weight,
    get_transform_size,
)
from compressed_tensors.utils import get_execution_device, get_offloaded_device
from compressed_tensors.utils.helpers import ParameterizedDefaultDict
from torch import Tensor, device, dtype
from torch.nn import Linear, Module, Parameter


@TransformFactory.register("hadamard")
class HadamardFactory(TransformFactory):
    """
    Factory used to apply hadamard transforms to a model

    :param name: name associated with transform scheme
    :param scheme: transform scheme which defines how transforms should be created
    :param seed: random seed used to transform weight randomization
    """

    def __init__(self, name: str, scheme: TransformScheme, seed: Optional[int] = None):
        super().__init__(name, scheme, seed)
        self.weights = ParameterizedDefaultDict(self._create_weight)
        self.perms = ParameterizedDefaultDict(self._create_permutation)

    def create_transform(self, module: Module, args: TransformArgs):
        """
        Create a HadamardTransform for applying to a module. Transforms with the same
        size, dtype, and device are cached

        :param module: parent module that transform will be applied to
        :param args: defines how the transform will be applied to the module
        """
        assert hasattr(module, "weight")
        size = get_transform_size(module, args.location, self.scheme.head_dim)
        dtype = module.weight.dtype
        device = get_offloaded_device(module)
        exec_device = get_execution_device(module)

        factory_kwargs = {"construct_device": exec_device}
        weight = self.weights.get(size, dtype, device, factory_kwargs=factory_kwargs)
        perm = self.perms[weight] if self.scheme.randomize else None
        return HadamardTransform(weight, perm, args, type(module))

    def _create_weight(
        self,
        size: int,
        dtype: dtype,
        device: device,
        construct_device: device,
    ) -> Parameter:
        # construct on execution device, cache on offload device
        data = deterministic_hadamard_matrix(size, dtype, construct_device)
        data = data.to(device=device)
        return Parameter(data, requires_grad=self.scheme.requires_grad)

    def _create_permutation(self, weight: Parameter) -> Parameter:
        data = torch.randperm(weight.size(0), generator=self.generator)
        return Parameter(data, requires_grad=False)


class HadamardTransform(TransformBase):
    def __init__(
        self,
        weight: Parameter,
        perm: Optional[Parameter],
        args: TransformArgs,
        module_type: type[torch.nn.Module],
    ):
        super().__init__()
        self.weight = weight
        self.perm = perm
        self.args = args
        self.module_type = module_type
        self._scale = math.sqrt(weight.size(0))

    def forward(self, value: Tensor) -> Tensor:
        weight = self.weight

        if self.perm is not None:
            weight = weight[self.perm][:, self.perm]

        if self.args.inverse:
            weight = weight.T
 
        return apply_transform_weight(
            weight, value, self.args.location, self.module_type
        ) / self._scale
