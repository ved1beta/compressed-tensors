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
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import torch
from compressed_tensors.utils import Aliasable
from compressed_tensors.utils.helpers import deprecated
from pydantic import BaseModel, Field, field_validator, model_validator


__all__ = [
    "FP8_DTYPE",
    "FP8_E4M3_DATA",
    "FP4_E2M1_DATA",
    "FloatArgs",
    "QuantizationType",
    "QuantizationStrategy",
    "QuantizationArgs",
    "round_to_quantized_type",
    "ActivationOrdering",
    "DynamicType",
]


class FloatArgs:
    exponent: int
    mantissa: int
    bits: int
    max: float
    min: float
    dtype: Optional[torch.dtype] = None


class FP4_E2M1_DATA(FloatArgs):
    exponent = 2
    mantissa = 1
    bits = 4
    max = 6.0
    min = -6.0

    @staticmethod
    @torch.compile
    def cast_to_fp4(x):
        sign = torch.sign(x)
        x = torch.abs(x)
        x[(x >= 0.0) & (x <= 0.25)] = 0.0
        x[(x > 0.25) & (x < 0.75)] = 0.5
        x[(x >= 0.75) & (x <= 1.25)] = 1.0
        x[(x > 1.25) & (x < 1.75)] = 1.5
        x[(x >= 1.75) & (x <= 2.5)] = 2.0
        x[(x > 2.5) & (x < 3.5)] = 3.0
        x[(x >= 3.5) & (x <= 5.0)] = 4.0
        x[x > 5.0] = 6.0
        return x * sign


class FP8_E4M3_DATA(FloatArgs):
    exponent = 4
    mantissa = 3
    bits = 8
    max = torch.finfo(torch.float8_e4m3fn).max
    min = torch.finfo(torch.float8_e4m3fn).min
    dtype = torch.float8_e4m3fn


# TODO: Remove soon in favour of a more descriptive FloatArgs
FP8_DTYPE = torch.float8_e4m3fn


class QuantizationType(str, Enum):
    """
    Enum storing quantization type options
    """

    INT = "int"
    FLOAT = "float"


class QuantizationStrategy(str, Enum):
    """
    Enum storing quantization strategy options
    """

    TENSOR = "tensor"
    CHANNEL = "channel"
    GROUP = "group"
    BLOCK = "block"
    TOKEN = "token"
    TENSOR_GROUP = "tensor_group"


class DynamicType(str, Enum):
    """
    Enum storing potential dynamic types.

    1. If dynamic is True, all quantization parameters are generated on the fly.
    2. If dynamic is False, all quantization parameters generated are static.
    3. If "local" is provided, only local quantization parameters are dynamic.

    Note: "local" is only currently supported for NVFP4.

    """

    LOCAL = "local"


class ActivationOrdering(Aliasable, str, Enum):
    """
    Enum storing strategies for activation ordering

    Group: reorder groups and weight\n
    Weight: only reorder weight, not groups. Slightly lower accuracy but also lower
    latency when compared to group actorder\n
    Dynamic: alias for Group\n
    Static: alias for Weight\n
    """

    GROUP = "group"
    WEIGHT = "weight"
    # aliases
    DYNAMIC = "dynamic"
    STATIC = "static"

    @staticmethod
    def get_aliases() -> Dict[str, str]:
        return {
            "dynamic": "group",
            "static": "weight",
        }


class QuantizationArgs(BaseModel, use_enum_values=True):
    """
    User facing arguments used to define a quantization config for weights or
    activations

    :param num_bits: quantization bit depth
    :param type: dtype to quantized to, either int or float
    :param symmetric: whether or not quantization scale is symmetric about zero-point
    :param strategy: string id determining the scope of scale/zero-point to apply
    :param group_size: group length to use for the group strategy
    :param block_structure: 2d block structure to use for the block strategy; must be
        a list of two ints [rows, cols] like [128, 128].
    :param dynamic: set True to perform dynamic quantization - values will not be
        calibrated during calibration phase, instead during inference new quantization
        ranges will be observed with every sample. Defaults to False for static
        quantization. Note that enabling dynamic quantization will change the default
        observer to a memoryless one
    :param actorder: whether to apply group quantization in decreasing order of
        activation. Defaults to None for arbitrary ordering
    """

    num_bits: int = 8
    type: QuantizationType = QuantizationType.INT
    symmetric: bool = True
    group_size: Optional[int] = None
    strategy: Optional[QuantizationStrategy] = None
    block_structure: Optional[List[int]] = None
    dynamic: Union[DynamicType, bool] = False
    actorder: Union[ActivationOrdering, bool, None] = None
    observer: Optional[str] = Field(
        default=None,
        description=(
            "Determines the method of computing quantization parameters (scales and "
            "zero-points). Defaults to min-max when not using dynamic quantization"
        ),
    )
    observer_kwargs: Dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "optional dict of kwargs to be passed directly to torch quantization "
            "Observers constructor excluding quantization range or symmetry"
        ),
    )

    @field_validator("type", mode="before")
    def validate_type(cls, value) -> QuantizationType:
        if isinstance(value, str):
            return QuantizationType(value.lower())

        return value

    @field_validator("group_size", mode="before")
    def validate_group(cls, value) -> Union[int, None]:
        if value is None:
            return value

        if value < -1:
            raise ValueError(
                f"Invalid group size {value}. Use group_size > 0 for "
                "strategy='group' and group_size = -1 for 'channel'"
            )

        return value

    @field_validator("block_structure", mode="before")
    def validate_block_structure(cls, value) -> Optional[List[int]]:
        if value is None:
            return value
        # For backward compatibility, allow string format "2x4", "8x16", etc.
        if isinstance(value, str):
            try:
                return [int(x) for x in value.split("x")]
            except Exception:
                raise ValueError(
                    f"Invalid block_structure '{value}'. Must be a list of two ints [rows, cols]."
                )
        if isinstance(value, (list, tuple)):
            if len(value) != 2 or not all(isinstance(v, int) for v in value):
                raise ValueError(
                    f"Invalid block_structure '{value}'. Must be a list of two ints [rows, cols]."
                )
            return list(value)
        raise ValueError(
            f"Invalid block_structure '{value}'. Must be a list of two ints [rows, cols]."
        )

    @field_validator("strategy", mode="before")
    def validate_strategy(cls, value) -> Union[QuantizationStrategy, None]:
        if isinstance(value, str):
            return QuantizationStrategy(value.lower())

        return value

    @field_validator("actorder", mode="before")
    def validate_actorder(cls, value) -> Optional[ActivationOrdering]:
        if isinstance(value, bool):
            return ActivationOrdering.GROUP if value else None

        if isinstance(value, str):
            return ActivationOrdering(value.lower())

        return value

    @field_validator("dynamic", mode="before")
    def validate_dynamic(cls, value) -> Union[DynamicType, bool]:
        if isinstance(value, str):
            return DynamicType(value.lower())
        return value

    @model_validator(mode="after")
    def validate_model_after(model: "QuantizationArgs") -> "QuantizationArgs":
        # extract user-passed values from dictionary
        strategy = model.strategy
        group_size = model.group_size
        actorder = model.actorder
        dynamic = model.dynamic
        observer = model.observer

        # infer strategy
        if strategy is None:
            if group_size is None:
                strategy = QuantizationStrategy.TENSOR
            elif group_size > 0:
                strategy = QuantizationStrategy.GROUP
            elif group_size == -1:
                strategy = QuantizationStrategy.CHANNEL
            else:
                raise ValueError(
                    f"Invalid group size {group_size}. Use group_size > 0 for "
                    "strategy='group' and group_size = -1 for 'channel'"
                )

        # validate strategy and group
        if strategy == QuantizationStrategy.GROUP:
            if group_size is None or group_size <= 0:
                raise ValueError(
                    f"strategy {strategy} requires group_size to be "
                    "set to a positive value"
                )
        if (
            group_size is not None
            and group_size > 0
            and strategy
            not in (QuantizationStrategy.GROUP, QuantizationStrategy.TENSOR_GROUP)
        ):
            raise ValueError("group_size requires strategy to be set to 'group'")

        # validate activation ordering and strategy
        if actorder is not None and strategy != QuantizationStrategy.GROUP:
            raise ValueError(
                "Must use group quantization strategy in order to apply "
                "activation ordering"
            )

        # infer observer w.r.t. dynamic
        if dynamic:
            supported_strategies = (
                QuantizationStrategy.TOKEN,
                QuantizationStrategy.TENSOR,
                QuantizationStrategy.TENSOR_GROUP,
                QuantizationStrategy.GROUP,
            )
            if strategy not in supported_strategies:
                raise ValueError(
                    f"One of {supported_strategies} must be used for dynamic quantization"
                )

            if (
                dynamic == DynamicType.LOCAL
                and strategy != QuantizationStrategy.TENSOR_GROUP
            ):
                raise ValueError("local is only supported for strategy tensor_group")

            if observer is not None:
                if dynamic is True:  # checking if dynamic is True, not "local"
                    if (
                        observer != "memoryless"
                    ):  # avoid annoying users with old configs
                        warnings.warn(
                            "No observer is used for dynamic quantization, setting to None"
                        )
                    observer = None
            else:
                if dynamic == DynamicType.LOCAL:
                    observer = "minmax"

        elif observer is None:
            # default to minmax for non-dynamic cases
            observer = "minmax"

        # write back modified values
        model.strategy = strategy
        model.observer = observer
        return model

    def pytorch_dtype(self) -> torch.dtype:
        if self.type == QuantizationType.FLOAT:
            if self.num_bits == 8:
                return FP8_E4M3_DATA.dtype
            else:
                raise NotImplementedError("Only num_bits in (8) are supported")
        elif self.type == QuantizationType.INT:
            if self.num_bits <= 8:
                return torch.int8
            elif self.num_bits <= 16:
                return torch.int16
            else:
                return torch.int32
        else:
            raise ValueError(f"Invalid quantization type {self.type}")

    @deprecated("QuantizationArgs.observer")
    def get_observer(self) -> str:
        return self.observer


def round_to_quantized_type(
    tensor: torch.Tensor, args: QuantizationArgs
) -> torch.Tensor:
    """
    Rounds each element of the input tensor to the nearest quantized representation,
    keeping to original dtype

    :param tensor: tensor to round
    :param args: QuantizationArgs to pull appropriate dtype from
    :return: rounded tensor
    """
    original_dtype = tensor.dtype
    if args.type == QuantizationType.FLOAT:
        if args.num_bits == 8:
            rounded = tensor.to(FP8_E4M3_DATA.dtype)
        elif args.num_bits == 4:
            rounded = FP4_E2M1_DATA.cast_to_fp4(tensor)
        else:
            raise NotImplementedError("Only num_bits in (4, 8) are supported")
    elif args.type == QuantizationType.INT:
        rounded = torch.round(tensor)
    else:
        raise ValueError(f"Invalid quantization type {args.type}")

    return rounded.to(original_dtype)
