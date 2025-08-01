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


import logging
import math
import warnings
from enum import Enum
from typing import List, Optional

import torch
from compressed_tensors.quantization.lifecycle.forward import (
    wrap_module_forward_quantized,
)
from compressed_tensors.quantization.quant_args import (
    FP8_E4M3_DATA,
    ActivationOrdering,
    QuantizationArgs,
    QuantizationStrategy,
)
from compressed_tensors.quantization.quant_config import QuantizationStatus
from compressed_tensors.quantization.quant_scheme import QuantizationScheme
from compressed_tensors.quantization.utils import is_fp4, is_kv_cache_quant_scheme
from compressed_tensors.utils import (
    disable_hf_hook,
    get_execution_device,
    register_offload_parameter,
)
from torch.nn import Module, Parameter


__all__ = [
    "initialize_module_for_quantization",
    "is_attention_module",
    "KVCacheScaleType",
]


_LOGGER = logging.getLogger(__name__)


class KVCacheScaleType(Enum):
    KEY = "k_scale"
    VALUE = "v_scale"


def initialize_module_for_quantization(
    module: Module,
    scheme: Optional[QuantizationScheme] = None,
    force_zero_point: bool = True,
    scale_dtype: Optional[torch.dtype] = None,
):
    """
    attaches appropriate scales, zero points, and observers to a layer
    given its target quantization scheme

    apply to full model with `model.apply(initialize_module_for_quantization)`

    :param module: module to set for calibration
    :param scheme: scheme to use for quantization. if None is provided,
        will attempt to use scheme stored in the module under `quantization_scheme`,
        if not provided, the layer will be skipped
    :param force_zero_point: whether to force initialization of a zero point for
        symmetric quantization
    :param scale_dtype: dtype to used for the scales, if overriding the
        weight dtype as the scale dtype
    """
    # TODO: don't initialize parameters when running decompression
    scheme = scheme or getattr(module, "quantization_scheme", None)
    if scheme is None:
        # no scheme passed and layer not targeted for quantization - skip
        return

    if is_attention_module(module):
        # quantized actions based on calltime status
        _initialize_attn_scales(module)

    else:

        if scheme.input_activations is not None:
            _initialize_scale_zero_point(
                module,
                "input",
                scheme.input_activations,
                force_zero_point=force_zero_point,
                scale_dtype=scale_dtype,
            )

        if scheme.weights is not None:
            if hasattr(module, "weight"):
                weight_shape = None
                if isinstance(module, torch.nn.Linear):
                    weight_shape = module.weight.shape
                _initialize_scale_zero_point(
                    module,
                    "weight",
                    scheme.weights,
                    weight_shape=weight_shape,
                    force_zero_point=force_zero_point,
                    scale_dtype=scale_dtype,
                )
            else:
                _LOGGER.warning(
                    f"module type {type(module)} targeted for weight quantization but "
                    "has no attribute weight, skipping weight quantization "
                    f"for {type(module)}"
                )

        if scheme.output_activations is not None:
            if not is_kv_cache_quant_scheme(scheme):
                _initialize_scale_zero_point(
                    module, "output", scheme.output_activations, scale_dtype=scale_dtype
                )

        module.quantization_scheme = scheme
        module.quantization_status = QuantizationStatus.INITIALIZED

        with disable_hf_hook(module):
            # wrap forward call of module to perform
            # quantized actions based on calltime status
            wrap_module_forward_quantized(module, scheme)


def is_attention_module(module: Module):
    return "attention" in module.__class__.__name__.lower() and (
        hasattr(module, "k_proj")
        or hasattr(module, "v_proj")
        or hasattr(module, "qkv_proj")
    )


def _initialize_scale_zero_point(
    module: Module,
    base_name: str,
    quantization_args: QuantizationArgs,
    weight_shape: Optional[torch.Size] = None,
    force_zero_point: bool = True,
    scale_dtype: Optional[torch.dtype] = None,
):
    if quantization_args.dynamic is True:
        return

    # initialize on execution device to avoid performing quantized ops on cpu
    device = get_execution_device(module)

    # 1. Create global_scales for tensor_group - generates
    # a per tensor scale
    if quantization_args.strategy == QuantizationStrategy.TENSOR_GROUP:
        init_global_scale = Parameter(
            torch.empty(1, dtype=torch.float32, device=device),
            requires_grad=False,
        )
        register_offload_parameter(
            module, f"{base_name}_global_scale", init_global_scale
        )

    # 2. Infer expected scale/zero point shape
    if quantization_args.strategy == QuantizationStrategy.TOKEN:
        expected_shape = (1, 1)
    else:
        expected_shape = 1

    if base_name == "weight" and weight_shape is not None:
        if quantization_args.strategy == QuantizationStrategy.CHANNEL:
            # (output_channels, 1) - only for weights
            expected_shape = (weight_shape[0], 1)
        elif quantization_args.strategy in (
            QuantizationStrategy.TENSOR_GROUP,
            QuantizationStrategy.GROUP,
        ):
            # GROUP/TENSOR_GROUP for both weights and activations
            num_groups = math.ceil(weight_shape[1] / quantization_args.group_size)
            expected_shape = (weight_shape[0], max(num_groups, 1))
        elif quantization_args.strategy == QuantizationStrategy.BLOCK:
            # For block quantization, scale shape should match number of blocks - only for weights
            if quantization_args.block_structure is None:
                raise ValueError("Block quantization requires block_structure to be specified")
            block_height, block_width = quantization_args.block_structure
            rows, cols = weight_shape[-2], weight_shape[-1]
            num_rows_blocks = math.ceil(rows / block_height)
            num_cols_blocks = math.ceil(cols / block_width)
            
            # Warn if dimensions don't divide evenly
            if rows % block_height != 0 or cols % block_width != 0:
                warnings.warn(
                    f"Block quantization: tensor shape {weight_shape} does not divide evenly "
                    f"by block structure {quantization_args.block_structure}. "
                    f"Some blocks will be incomplete which may affect quantization quality.",
                    UserWarning
                )
            
            expected_shape = (num_rows_blocks, num_cols_blocks)
    elif quantization_args.strategy == QuantizationStrategy.BLOCK:
        warnings.warn(
            f"BLOCK quantization not supported for {base_name} activations. "
            f"Falling back to tensor-level quantization.",
            UserWarning
        )
        expected_shape = 1

    # 3. Identify quantization scale and zp dtype
    scale_dtype = scale_dtype if scale_dtype is not None else module.weight.dtype

    if is_fp4(quantization_args=quantization_args):
        scale_dtype = zp_dtype = FP8_E4M3_DATA.dtype
    else:
        # TODO: consider erroring out in the future as if the dtype if not one of these,
        # there is likely bug
        if scale_dtype not in [
            torch.float16,
            torch.bfloat16,
            torch.float32,
            torch.float64,
        ]:
            scale_dtype = torch.float16
        zp_dtype = quantization_args.pytorch_dtype()

    # 4. Initializes empty scale, zero point, and g_idx parameters for the module
    # do not init scales for quantzation_args.dynamic == DynamicType.local
    if not quantization_args.dynamic:
        init_scale = Parameter(
            torch.empty(expected_shape, dtype=scale_dtype, device=device),
            requires_grad=False,
        )
        register_offload_parameter(module, f"{base_name}_scale", init_scale)

    if force_zero_point or not quantization_args.symmetric:
        init_zero_point = Parameter(
            torch.zeros(expected_shape, device=device, dtype=zp_dtype),
            requires_grad=False,
        )
        register_offload_parameter(module, f"{base_name}_zero_point", init_zero_point)

    # only grouped activation ordering has g_idx
    if quantization_args.actorder == ActivationOrdering.GROUP:
        g_idx_shape = (weight_shape[1],)
        g_idx_dtype = torch.int
        init_g_idx = Parameter(
            torch.full(g_idx_shape, -1, device=device, dtype=g_idx_dtype),
            requires_grad=False,
        )
        register_offload_parameter(module, f"{base_name}_g_idx", init_g_idx)


def _initialize_attn_scales(module: Module) -> None:
    """Initlaize k_scale, v_scale for  self_attn"""

    expected_shape = 1  # per tensor

    param = next(module.parameters())
    scale_dtype = param.dtype
    device = param.device

    init_scale = Parameter(
        torch.empty(expected_shape, dtype=scale_dtype, device=device),
        requires_grad=False,
    )
    register_offload_parameter(module, KVCacheScaleType.KEY.value, init_scale)

    init_scale = Parameter(
        torch.empty(expected_shape, dtype=scale_dtype, device=device),
        requires_grad=False,
    )
    register_offload_parameter(module, KVCacheScaleType.VALUE.value, init_scale)
