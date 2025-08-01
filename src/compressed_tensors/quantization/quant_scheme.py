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

import warnings
from copy import deepcopy
from typing import Any, Dict, List, Optional

from compressed_tensors.quantization.quant_args import (
    DynamicType,
    QuantizationArgs,
    QuantizationStrategy,
    QuantizationType,
)
from pydantic import BaseModel, model_validator


__all__ = [
    "QuantizationScheme",
    "preset_name_to_scheme",
    "is_preset_scheme",
]


class QuantizationScheme(BaseModel):
    """
    Set of QuantizationArgs defining how the weights, inputs and outputs of target list
    of modules should be quantized

    :param targets: list of modules to apply the QuantizationArgs to, can be layer
    names, layer types or a regular expression, typically ["Linear"]
    :param weights: quantization config for layer weights
    :param input_activations: quantization config for layer inputs
    :param output_activations: quantization config for layer outputs
    """

    targets: List[str]
    weights: Optional[QuantizationArgs] = None
    input_activations: Optional[QuantizationArgs] = None
    output_activations: Optional[QuantizationArgs] = None

    @model_validator(mode="after")
    def validate_model_after(model: "QuantizationScheme") -> "QuantizationScheme":
        inputs = model.input_activations
        outputs = model.output_activations
        weights = model.weights

        if inputs is not None:
            if inputs.actorder is not None:
                raise ValueError("Cannot apply actorder to input activations")

        if outputs is not None:
            if outputs.actorder is not None:
                raise ValueError("Cannot apply actorder to output activations")

        if (
            inputs and weights
            and weights.strategy == QuantizationStrategy.GROUP 
            and inputs.strategy == QuantizationStrategy.GROUP
            and weights.group_size != inputs.group_size
        ):
            warnings.warn(
                "Using GROUP strategy for both weights and input_activations "
                f"with different group sizes ({weights.group_size} vs {inputs.group_size}) "
                "may complicate fused kernel implementations. Consider using "
                "TENSOR_GROUP strategy for both or matching group sizes.",
                UserWarning,
                stacklevel=2
            )

        return model


"""
Pre-Set Quantization Scheme Args
"""


def preset_name_to_scheme(name: str, targets: List[str]) -> QuantizationScheme:
    """
    :param name: preset quantization settings name. must exist in upper case in
        PRESET_SCHEMES
    :param targets: list of quantization targets to be passed to the Scheme
    :return: new QuantizationScheme for a given name with the given targets
    """
    name = name.upper()

    if name not in PRESET_SCHEMES:
        raise KeyError(
            f"Unknown preset scheme name {name}, "
            f"available names: {list(PRESET_SCHEMES.keys())}"
        )

    scheme_args = deepcopy(PRESET_SCHEMES[name])  # deepcopy to avoid args references
    return QuantizationScheme(
        targets=targets,
        **scheme_args,
    )


def is_preset_scheme(name: str) -> bool:
    """
    :param name: preset quantization settings name
    :return: True if the name is a preset scheme name
    """
    return name.upper() in PRESET_SCHEMES


UNQUANTIZED = dict()

NVFP4A16 = dict(
    weights=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TENSOR_GROUP,
        symmetric=True,
        dynamic=False,
        group_size=16,
    )
)


NVFP4 = dict(
    weights=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TENSOR_GROUP,
        symmetric=True,
        dynamic=False,
        group_size=16,
    ),
    input_activations=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TENSOR_GROUP,
        symmetric=True,
        dynamic=DynamicType.LOCAL,
        group_size=16,
    ),
)

# 8 bit integer weights and 8 bit activations quantization
INT8_W8A8 = dict(
    weights=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.CHANNEL,
        symmetric=True,
        dynamic=False,
    ),
    input_activations=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.TOKEN,
        symmetric=True,
        dynamic=True,
        observer=None,
    ),
)

# 8 bit integer weights only quantization
W8A16 = dict(
    weights=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.CHANNEL,
        symmetric=True,
        dynamic=False,
    ),
)

# 4 bit integer weights only quantization
W4A16 = dict(
    weights=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.GROUP,
        group_size=128,
        symmetric=True,
        dynamic=False,
    ),
)

# 4 bit integer weights only asymmetric quantization
W4A16_ASYM = dict(
    weights=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.GROUP,
        group_size=128,
        symmetric=False,
        dynamic=False,
    ),
)

# 4 bit integer weights and 8 bit activations quantization
INT8_W4A8 = dict(
    weights=QuantizationArgs(
        num_bits=4,
        type=QuantizationType.INT,
        group_size=128,
        strategy=QuantizationStrategy.GROUP,
        symmetric=True,
        dynamic=False,
    ),
    input_activations=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.INT,
        strategy=QuantizationStrategy.TOKEN,
        symmetric=True,
        dynamic=True,
        observer=None,
    ),
)

# FP8 weights and FP8 activations quantization
FP8 = dict(
    weights=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TENSOR,
        symmetric=True,
        dynamic=False,
    ),
    input_activations=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TENSOR,
        symmetric=True,
        dynamic=False,
    ),
)

# FP8 weights and FP8 dynamic activations quantization
FP8_DYNAMIC = dict(
    weights=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.CHANNEL,
        symmetric=True,
        dynamic=False,
    ),
    input_activations=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.TOKEN,
        symmetric=True,
        dynamic=True,
        observer=None,
    ),
)

# Block‐wise FP8 (deepseekv3-style quantization):
# static 128x128 per‐block weights and
# dynamic per‐token‐group activations
FP8_BLOCK = dict(
    weights=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.BLOCK,
        symmetric=True,
        dynamic=False,
        block_structure=[128, 128],
    ),
    input_activations=QuantizationArgs(
        num_bits=8,
        type=QuantizationType.FLOAT,
        strategy=QuantizationStrategy.GROUP,
        symmetric=True,
        dynamic=True,
        observer=None,
        group_size=128,
    ),
)

PRESET_SCHEMES = {
    # Unquantized (no-op)
    "UNQUANTIZED": UNQUANTIZED,
    # Integer weight only schemes
    "W8A16": W8A16,
    "W4A16": W4A16,
    "W4A16_ASYM": W4A16_ASYM,
    # Integer weight and activation schemes
    "W8A8": INT8_W8A8,
    "INT8": INT8_W8A8,  # alias for W8A8
    "W4A8": INT8_W4A8,
    # Float weight and activation schemes
    "FP8": FP8,
    "FP8_DYNAMIC": FP8_DYNAMIC,
    "FP8_BLOCK": FP8_BLOCK,
    "NVFP4A16": NVFP4A16,
    "NVFP4": NVFP4,
}
