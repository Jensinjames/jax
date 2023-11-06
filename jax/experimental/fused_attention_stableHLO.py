# Copyright 2023 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from functools import partial

import jax
import jax.numpy as jnp
from jax import core, dtypes
from jax.interpreters import xla
from jax.interpreters import mlir
from jax.interpreters.mlir import ir
from jaxlib.hlo_helpers import custom_call

from jax._src.core import ShapedArray
Array = jnp.ndarray
DType = jnp.dtype
PRNGKey = jnp.ndarray
from typing import Any, Optional

# Create dot_product_attention_fwd_p for forward operation.
_dot_product_attention_fwd_p = core.Primitive("dot_product_attention_fwd")
_dot_product_attention_fwd_p.multiple_results = True
_dot_product_attention_fwd_p.def_impl(partial(xla.apply_primitive, _dot_product_attention_fwd_p))

# Create dot_product_attention_bwd_p for backward operation.
_dot_product_attention_bwd_p = core.Primitive("dot_product_attention_bwd")
_dot_product_attention_bwd_p.multiple_results = True
_dot_product_attention_bwd_p.def_impl(partial(xla.apply_primitive, _dot_product_attention_bwd_p))


def dot_product_attention_fwd(query, key, value, scale=1.0):
    output, activation = _dot_product_attention_fwd_p.bind(query, key, value, scale)
    return output, (activation, query, key, value)

def dot_product_attention_bwd(scale, res, grad_output):
    activation, query, key, value = res
    grad_query, grad_key, grad_value = _dot_product_attention_bwd_p.bind(
        grad_output, query, key, value, activation, scale
    )
    return grad_query, grad_key, grad_value

def _dot_product_attention_fwd_abstract(query, key, value, scale):
    query_dtype = dtypes.canonicalize_dtype(query.dtype)
    key_dtype = dtypes.canonicalize_dtype(key.dtype)
    value_dtype = dtypes.canonicalize_dtype(value.dtype)
    assert query_dtype == key_dtype == value_dtype
    assert query_dtype in [jnp.float16, jnp.bfloat16]

    batch_size, q_seq_len, num_heads, head_dim = query.shape
    _, kv_seq_len, _, _ = key.shape
    output_shape = (batch_size, q_seq_len, num_heads, head_dim)
    activation_shape = (batch_size, num_heads, q_seq_len, kv_seq_len)
    return (
        ShapedArray(output_shape, query_dtype),  # output
        ShapedArray(activation_shape, query_dtype),  # activation
    )


_dot_product_attention_fwd_p.def_abstract_eval(_dot_product_attention_fwd_abstract)


def _dot_product_attention_bwd_abstract(grad_output, query, key, value, activation, scale):
    query_dtype = dtypes.canonicalize_dtype(query.dtype)
    key_dtype = dtypes.canonicalize_dtype(key.dtype)
    value_dtype = dtypes.canonicalize_dtype(value.dtype)
    assert query_dtype == key_dtype == value_dtype
    assert query_dtype in [jnp.float16, jnp.bfloat16]
    
    return (
        ShapedArray(
            query.shape, query_dtype
        ),  # grad query
        ShapedArray(
            key.shape, key_dtype
        ),  # grad key
        ShapedArray(
            value.shape, value_dtype
        ),  # part value
    )


_dot_product_attention_bwd_p.def_abstract_eval(_dot_product_attention_bwd_abstract)

def element_type_to_backend_config_type_mapping(dtype):
    _element_type_to_backend_config_type_mapping = {
        ir.BF16Type.get(): "BF16",
        ir.F16Type.get(): "F16",
    }
    return _element_type_to_backend_config_type_mapping.get(dtype)

def default_layouts(*shapes):
    return [range(len(shape) - 1, -1, -1) for shape in shapes]

def create_dot_product_attention_backend_config(batch_size,
                                                num_heads,
                                                seq_q,
                                                seq_kv,
                                                dtype,
                                                fmha_scale,
                                                dropout_rate,
                                                is_flash_attention,
                                                is_causal_mask):
    # b q_seq num_heads head_dim  -> Q
    # b kv_seq num_heads head_dim -> K
    # b kv_seq num_heads head_dim -> V
    # b num_heads q_seq kv_seq -> P
    # b q_seq num_heads head_dim -> O
    # bmm1: Q @ K -> P
    # bmm2: P @ V -> O
    # bmm2Grad1: P @ dO -> dV
    # bmm2Grad2: dO @ V -> dP
    # bmm1Grad1: dP @ Q -> dK
    # bmm1Grad2: dP @ K -> dQ
    backend_config = {
        "algorithm":{"algo_id":"0","math_type":"TENSOR_OP_MATH","tuning_knobs":{"17":"1","24":"0"},"is_cudnn_frontend":True,"workspace_size":"0"},
        "fmha_scale":fmha_scale,
        "dropout_rate":dropout_rate,
        "bmm1_dot_dimension_numbers":{"lhs_contracting_dimensions":["3"],"rhs_contracting_dimensions":["3"],"lhs_batch_dimensions":["0","2"],"rhs_batch_dimensions":["0","2"]},
        "bmm2_dot_dimension_numbers":{"lhs_contracting_dimensions":["3"],"rhs_contracting_dimensions":["1"],"lhs_batch_dimensions":["0","1"],"rhs_batch_dimensions":["0","2"]},
        "bmm1_grad_gemm1_dot_dimension_numbers":{"lhs_contracting_dimensions":["2"],"rhs_contracting_dimensions":["1"],"lhs_batch_dimensions":["0","1"],"rhs_batch_dimensions":["0","2"]},
        "bmm1_grad_gemm2_dot_dimension_numbers":{"lhs_contracting_dimensions":["3"],"rhs_contracting_dimensions":["1"],"lhs_batch_dimensions":["0","1"],"rhs_batch_dimensions":["0","2"]},
        "bmm2_grad_gemm1_dot_dimension_numbers":{"lhs_contracting_dimensions":["2"],"rhs_contracting_dimensions":["1"],"lhs_batch_dimensions":["0","1"],"rhs_batch_dimensions":["0","2"]},
        "bmm2_grad_gemm2_dot_dimension_numbers":{"lhs_contracting_dimensions":["3"],"rhs_contracting_dimensions":["3"],"lhs_batch_dimensions":["0","2"],"rhs_batch_dimensions":["0","2"]},
        "intermediate_tensor_shape":{"element_type":element_type_to_backend_config_type_mapping(dtype),"dimensions":[str(batch_size),str(num_heads),str(seq_q),str(seq_kv)],"tuple_shapes":[],"layout":{"dim_level_types":[],"dim_unique":[],"dim_ordered":[],"minor_to_major":["3","2","1","0"],"tiles":[],"element_size_in_bits":"0","memory_space":"0","index_primitive_type":"PRIMITIVE_TYPE_INVALID","pointer_primitive_type":"PRIMITIVE_TYPE_INVALID","dynamic_shape_metadata_prefix_bytes":"0"},"is_dynamic_dimension":[False,False,False,False]},
        "seed":"42",
        "is_flash_attention":is_flash_attention,
        "is_causal_mask":is_causal_mask}
    return backend_config

def _dot_product_attention_fwd_cuda_lowering(ctx, query, key, value, scale):
    query_type = ir.RankedTensorType(query.type)
    query_shape = query_type.shape
    key_type = ir.RankedTensorType(key.type)
    key_shape = key_type.shape
    value_type = ir.RankedTensorType(value.type)
    value_shape = value_type.shape
    
    batch_size, q_seq_len, num_heads, head_dim = query_shape
    _, kv_seq_len, _, _ = key_shape

    output_shape = (batch_size, q_seq_len, num_heads, head_dim)
    activation_shape = (batch_size, num_heads, q_seq_len, kv_seq_len)

    opaque = create_dot_product_attention_backend_config(batch_size, num_heads, q_seq_len, kv_seq_len, query_type.element_type, scale, 0, False, False)
    out = custom_call(
        b"__cudnn$fhmaSoftmax",
        result_types=[
            ir.RankedTensorType.get(output_shape, query_type.element_type),
            ir.RankedTensorType.get(activation_shape, query_type.element_type),
        ],
        operands=[query, key, value],
        backend_config=opaque,
        operand_layouts=default_layouts(query_shape, key_shape, value_shape),
        result_layouts=default_layouts(output_shape, activation_shape),
    )
    return out

mlir.register_lowering(
    _dot_product_attention_fwd_p,
    _dot_product_attention_fwd_cuda_lowering,
    platform="gpu",
)

def _dot_product_attention_bwd_cuda_lowering(ctx, grad_output, query, key, value, activation, scale):
    query_type = ir.RankedTensorType(query.type)
    query_shape = query_type.shape
    key_type = ir.RankedTensorType(key.type)
    key_shape = key_type.shape
    value_type = ir.RankedTensorType(value.type)
    value_shape = value_type.shape
    activation_type = ir.RankedTensorType(activation.type)
    activation_shape = activation_type.shape

    batch_size, q_seq_len, num_heads, _ =query_shape
    _, kv_seq_len, _, _ = key_shape


    opaque = create_dot_product_attention_backend_config(batch_size, num_heads, q_seq_len, kv_seq_len, query_type.element_type, scale, 0, False, False)
    out = custom_call(
        b"__cudnn$fhmaSoftmaxBackward",
        result_types=[
            ir.RankedTensorType.get(query_shape, query_type.element_type), # grad query
            ir.RankedTensorType.get(key_shape, key_type.element_type), # grad key
            ir.RankedTensorType.get(value_shape, value_type.element_type), # grad value
        ],
        operands=[query, key, value, activation],
        backend_config=opaque,
        operand_layouts=default_layouts(query_shape, key_shape, value_shape, activation_shape),
        result_layouts=default_layouts(query_shape, key_shape, value_shape),
    )
    return out

mlir.register_lowering(
    _dot_product_attention_bwd_p,
    _dot_product_attention_bwd_cuda_lowering,
    platform="gpu",
)

@partial(jax.custom_vjp, nondiff_argnums=(3,))
def dot_product_attention(query: Array,
                          key: Array,
                          value: Array,
                          scale: float = 1.0):
    """Computes dot-product attention given query, key, and value.
    This is the core function for applying attention based on
    https://arxiv.org/abs/1706.03762. It calculates the attention weights given
    query and key and combines the values using the attention weights.
    batch seq num_heads, head_dim // but all assume Q, K and V will have same
    b q_seq num_heads head_dim  -> Q
    b kv_seq num_heads head_dim -> K
    b kv_seq num_heads head_dim -> V
    Args:
        query: queries for calculating attention with shape of `[batch, q_length,
        num_heads, qk_depth_per_head]`.
        key: keys for calculating attention with shape of `[batch, kv_length,
        num_heads, qk_depth_per_head]`.
        value: values to be used in attention with shape of `[batch, kv_length,
        num_heads, v_depth_per_head]`.
        scale: scale for the query.
    Returns:
        Output of shape `[batch, length, num_heads, v_depth_per_head]`.
    """

    output, _ = dot_product_attention_fwd(query, key, value, scale=scale)
    return output
  
dot_product_attention.defvjp(dot_product_attention_fwd, dot_product_attention_bwd)