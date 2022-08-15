from functools import wraps, partial
from itertools import product, chain, islice
import itertools
import functools
import collections
import copy
import operator
import random
import unittest
import math

import torch
import numpy as np
from torch._six import inf
import collections.abc

from typing import Any, Dict, List, Sequence, Tuple, Union, Iterable
from torch.testing import make_tensor
from torch.testing._internal.common_dtype import (
    _dispatch_dtypes, floating_types, floating_types_and, complex_types, floating_and_complex_types,
    floating_and_complex_types_and, all_types_and_complex_and, all_types_and, all_types_and_complex, integral_types_and,
    all_types, double_types, empty_types, complex_types_and, integral_types
)
from torch.testing._internal.common_device_type import \
    (onlyCUDA, onlyNativeDeviceTypes, disablecuDNN, skipCUDAIfNoMagma, skipCUDAIfNoMagmaAndNoCusolver,
     skipCUDAIfNoCusolver, skipCPUIfNoLapack, skipCPUIfNoFFT, skipCUDAIfRocm, skipCUDAIf, precisionOverride,
     skipCPUIfNoMklSparse,
     toleranceOverride, tol, has_cusolver)
from torch.testing._internal.common_cuda import (
    CUDA11OrLater, SM53OrLater, SM60OrLater, with_tf32_off, TEST_CUDNN,
    _get_torch_cuda_version, _get_magma_version)
from torch.testing._internal.common_utils import (
    make_fullrank_matrices_with_distinct_singular_values,
    TEST_WITH_ROCM, IS_WINDOWS, IS_MACOS, TEST_SCIPY,
    torch_to_numpy_dtype_dict, TEST_WITH_ASAN,
    GRADCHECK_NONDET_TOL, slowTest, freeze_rng_state,
)

import torch._refs as refs  # noqa: F401
import torch._refs.nn.functional
import torch._refs.special
import torch._refs.linalg

import torch._prims as prims  # noqa: F401

from torch.utils._pytree import tree_flatten

from distutils.version import LooseVersion

from torch.testing._internal.opinfo.core import (  # noqa: F401
    L,
    M,
    S,
    XS,
    _NOTHING,
    _getattr_qual,
    DecorateInfo,
    SampleInput,
    ErrorInput,
    AliasInfo,
    OpInfo,
    _find_referenced_opinfo,
    _inherit_constructor_args,
    PythonRefInfo,
    _generate_reduction_inputs,
    _generate_reduction_kwargs,
    sample_inputs_reduction,
    ReductionOpInfo,
    reference_inputs_elementwise_binary,
    make_error_inputs_elementwise_binary,
    generate_elementwise_binary_tensors,
    generate_elementwise_binary_arbitrarily_strided_tensors,
    generate_elementwise_binary_small_value_tensors,
    generate_elementwise_binary_large_value_tensors,
    generate_elementwise_binary_extremal_value_tensors,
    generate_elementwise_binary_broadcasting_tensors,
    generate_elementwise_binary_with_scalar_samples,
    generate_elementwise_binary_with_scalar_and_type_promotion_samples,
    generate_elementwise_binary_noncontiguous_tensors,
    sample_inputs_elementwise_binary,
    BinaryUfuncInfo,
    sample_inputs_elementwise_unary,
    generate_elementwise_unary_tensors,
    generate_elementwise_unary_small_value_tensors,
    generate_elementwise_unary_large_value_tensors,
    generate_elementwise_unary_extremal_value_tensors,
    reference_inputs_elementwise_unary,
    UnaryUfuncInfo,
    sample_inputs_spectral_ops,
    SpectralFuncType,
    SpectralFuncInfo,
    ShapeFuncInfo,
    sample_inputs_foreach,
    ForeachFuncInfo,
)

has_scipy_fft = False
if TEST_SCIPY:
    from scipy import stats
    import scipy.spatial
    import scipy.special
    try:
        import scipy.fft
        has_scipy_fft = True
    except ModuleNotFoundError:
        pass


# test if a tensor is close to an integer
def close_to_int(x, eps=0.1):
    if x.is_complex():
        y = torch.abs(torch.view_as_complex(torch.frac(torch.view_as_real(x))))
    else:
        y = torch.abs(torch.frac(x))
    return (y < eps) | (y > (1 - eps))


NumericsFilter = collections.namedtuple('NumericsFilter', ['condition', 'safe_val'])


def _generate_masked_op_mask(input_shape, device, **kwargs):
    yield None
    yield make_tensor(input_shape, dtype=torch.bool, device=device, requires_grad=False)
    if len(input_shape) > 2:
        # broadcast last mask dimension:
        yield make_tensor(input_shape[:-1] + (1,), dtype=torch.bool, device=device, requires_grad=False)
        # broadcast middle mask dimension:
        yield make_tensor(input_shape[:1] + (1,) + input_shape[2:], dtype=torch.bool, device=device, requires_grad=False)
        # broadcast first mask dimension:
        yield make_tensor((1,) + input_shape[1:], dtype=torch.bool, device=device, requires_grad=False)
        # mask.ndim < input.ndim
        yield make_tensor(input_shape[1:], dtype=torch.bool, device=device, requires_grad=False)
        # mask.ndim == 1
        yield make_tensor(input_shape[-1:], dtype=torch.bool, device=device, requires_grad=False)
        # masks that require broadcasting of inputs (mask.ndim >
        # input.ndim) will not be supported, however, we may
        # reconsider this if there will be demand on this kind of
        # degenerate cases.


def sample_inputs_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked reduction operators.

    Masked reduction operator is a reduction operator with trailing
    mask optional argument. A mask is a bool tensor with the same
    shape as input or a shape that is broadcastable to input shape.
    """
    kwargs['supports_multiple_dims'] = op_info.supports_multiple_dims

    for sample_input in sample_inputs_reduction(op_info, device, dtype, requires_grad, **kwargs):
        for mask in _generate_masked_op_mask(sample_input.input.shape, device, **kwargs):
            sample_input_args, sample_input_kwargs = sample_input.args, dict(mask=mask, **sample_input.kwargs)
            yield SampleInput(sample_input.input.detach().requires_grad_(requires_grad),
                              args=sample_input_args, kwargs=sample_input_kwargs)
            if(not requires_grad and dtype.is_floating_point and
               sample_input.input.ndim == 2 and mask is not None and
               mask.shape == sample_input.input.shape):
                for v in [torch.inf, -torch.inf, torch.nan]:
                    t = sample_input.input.detach()
                    t.diagonal(0, -2, -1).fill_(v)
                    yield SampleInput(t.requires_grad_(requires_grad),
                                      args=sample_input_args, kwargs=sample_input_kwargs)


def sample_inputs_sparse_coo_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked reduction operators that support inputs
    with sparse coo layouts.
    """
    if op_info.supports_sparse:
        op_name = op_info.name.replace('_masked.', '')
        for sample_input in sample_inputs_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
            mask = sample_input.kwargs.get('mask')
            if mask is not None:
                sample_input_kwargs = sample_input.kwargs.copy()
                sample_input_kwargs.update(mask=mask.to_sparse())
                yield SampleInput(sample_input.input.to_sparse(),
                                  args=sample_input.args, kwargs=sample_input_kwargs)
            else:
                if op_name in {'prod', 'amax', 'amin'}:
                    # FIXME: for now reductions with non-zero reduction identity and
                    # unspecified mask are not supported for sparse COO
                    # tensors, see torch._masked.prod implementation
                    # for details.
                    continue
                yield SampleInput(sample_input.input.to_sparse(),
                                  args=sample_input.args, kwargs=sample_input.kwargs)


def sample_inputs_sparse_csr_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked reduction operators that support inputs
    with sparse csr layouts.
    """
    if op_info.supports_sparse_csr:
        op_name = op_info.name.replace('_masked.', '')
        for sample_input in sample_inputs_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
            if not (sample_input.input.ndim == 2 and sample_input.kwargs.get('keepdim')):
                # - sparse CSR tensors are always 2-D tensors
                # - masked reduction on CSR tensors are defined only if keepdim is True.
                continue
            mask = sample_input.kwargs.get('mask')
            if mask is not None:
                sample_input_kwargs = sample_input.kwargs.copy()
                sample_input_kwargs.update(mask=mask.to_sparse_csr())
                new_sample = SampleInput(sample_input.input.to_sparse_csr(),
                                         args=sample_input.args, kwargs=sample_input_kwargs)
            else:
                if op_name in ['prod', 'amax', 'amin', 'mean']:
                    # reductions with non-zero reduction identity and
                    # unspecified mask is not supported for sparse CSR
                    # tensors, see torch._masked.prod implementation
                    # for details.
                    continue
                new_sample = SampleInput(sample_input.input.to_sparse_csr(),
                                         args=sample_input.args, kwargs=sample_input.kwargs)
            yield new_sample
            if sample_input.kwargs['dim'] == 0:
                # Reductions of CSR tensors use different implementations for
                # inner and/or outer dimensions. So, as a minimum of testing CSR
                # implementations the following kwargs must be generated:
                #   dict(dim=0, keepdim=True)
                #   dict(dim=1, keepdim=True)
                #   dict(dim=(0, 1), keepdim=True)
                # Here we generate the dim=1 case from the dim=0 case.
                sample_input_kwargs = new_sample.kwargs.copy()
                sample_input_kwargs.update(dim=1)
                yield SampleInput(new_sample.input.clone(),
                                  args=sample_input.args, kwargs=sample_input_kwargs)


def sample_inputs_masked_norm(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked norm.
    """
    for ord in [2.0, 1, float('inf'), float('-inf'), 0]:
        for sample_input in sample_inputs_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
            sample_input_args, sample_input_kwargs = (ord,) + sample_input.args, sample_input.kwargs.copy()
            yield SampleInput(sample_input.input.clone().requires_grad_(requires_grad),
                              args=sample_input_args, kwargs=sample_input_kwargs)


def sample_inputs_masked_std_var(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked std/var.
    """
    for unbiased in [False, True]:
        for sample_input in sample_inputs_masked_reduction(op_info, device, dtype, requires_grad, **kwargs):
            if sample_input.args:
                dim = sample_input.args[0]
                sample_input_args = sample_input.args[:1] + (unbiased,) + sample_input.args[1:]
                sample_input_kwargs = sample_input.kwargs.copy()
            else:
                dim = sample_input.kwargs.get('dim')
                sample_input_args = sample_input.args
                sample_input_kwargs = dict(sample_input.kwargs, unbiased=unbiased)
            if requires_grad:
                if sample_input_kwargs.get('mask') is None:
                    orig_count = torch._masked.sum(torch.ones(sample_input.input.shape, dtype=torch.int64), dim, keepdim=True)
                else:
                    inmask = torch._masked._input_mask(sample_input.input, *sample_input_args, **sample_input_kwargs)
                    orig_count = torch._masked.sum(inmask.new_ones(sample_input.input.shape, dtype=torch.int64),
                                                   dim, keepdim=True, mask=inmask)
                if orig_count.min() <= int(unbiased) + 1:
                    # Skip samples that lead to singularities in var
                    # computation resulting nan values both in var and
                    # autograd output that test_grad_fn cannot handle
                    # correctly. Also, skip samples when the autograd output
                    # for std could not be handled correctly due to torch.sqrt
                    continue
            yield SampleInput(sample_input.input.detach().requires_grad_(requires_grad),
                              args=sample_input_args, kwargs=sample_input_kwargs)

def sample_inputs_tensor_split(op_info, device, dtype, requires_grad, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=dtype,
                         low=None, high=None, requires_grad=requires_grad)

    args_cases = (
        # Cases with tensor indices.
        (torch.tensor([1, 2, 3]),),
        (torch.tensor(1),),
        (torch.tensor([1, 2, 3]), 1),
        (torch.tensor([1, 4, 2, 5, 3, 6])[::2], 1),
        # Cases with list of indices.
        ((2, 4),),
        ((2, 4), 1),
        ((2, 4), -1),
        # Cases with integer section.
        (3,),
        (3, 1),
        (3, -1),
    )

    for args in args_cases:
        yield SampleInput(make_input((S, S, S)), args=args)


def sample_inputs_linalg_det_logdet_slogdet(op_info, device, dtype, requires_grad, **kwargs):
    make_fullrank = make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_fullrank, dtype=dtype, device=device, requires_grad=requires_grad)
    batches = [(), (0, ), (3, )]
    ns = [0, 1, 5]

    is_logdet = (op_info.name == "logdet")

    for batch, n, in product(batches, ns):
        shape = batch + (n, n)
        A = make_arg(*shape)
        # Need to make the matrices in A have positive determinant for autograd
        # To do so, we multiply A by its determinant to flip the sign of its determinant
        if is_logdet and not A.is_complex() and A.numel() > 0:
            s = torch.linalg.slogdet(A).sign
            A = A * s.unsqueeze(-1).unsqueeze(-1)
            A.requires_grad_(requires_grad)
        yield SampleInput(A)


def sample_inputs_linalg_det_singular(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype)

    def make_singular_matrix_batch_base(size, rank):
        assert size[-1] == size[-2]
        assert rank > 0 and rank < size[-1]

        n = size[-1]
        a = make_arg(size[:-2] + (n, rank)) / 10
        b = make_arg(size[:-2] + (rank, n)) / 10
        x = a @ b
        lu, pivs, _ = torch.linalg.lu_factor_ex(x)
        p, l, u = torch.lu_unpack(lu, pivs)
        u_diag_abs = u.diagonal(0, -2, -1).abs()
        u_diag_abs_largest = u_diag_abs.max(dim=-1, keepdim=True).values
        u_diag_abs_smallest_idxs = torch.topk(u_diag_abs, k=(n - rank), largest=False).indices
        u.diagonal(0, -2, -1).div_(u_diag_abs_largest)
        u.diagonal(0, -2, -1)[..., u_diag_abs_smallest_idxs] = torch.finfo(dtype).eps
        matrix = p @ l @ u

        matrix.requires_grad_(requires_grad)
        return matrix

    def sample_generator():
        for batch, size in product(((), (2,), (2, 2)), range(6)):
            shape = batch + (size, size)
            for rank in range(1, size):
                yield make_singular_matrix_batch_base(shape, rank)

    return [SampleInput(t) for t in sample_generator()]


def sample_inputs_linalg_matrix_power(op_info, device, dtype, requires_grad, **kwargs):
    make_fullrank = make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    make_arg_fullrank = partial(make_fullrank, dtype=dtype, device=device, requires_grad=requires_grad)
    # (<matrix_size>, (<batch_sizes, ...>))
    test_sizes = [
        (1, ()),
        (2, (0,)),
        (2, (2,)),
    ]

    for matrix_size, batch_sizes in test_sizes:
        size = batch_sizes + (matrix_size, matrix_size)
        for n in (0, 3, 5):
            yield SampleInput(make_arg(size), args=(n,))
        for n in [-4, -2, -1]:
            yield SampleInput(make_arg_fullrank(*size), args=(n,))

def sample_inputs_hsplit(op_info, device, dtype, requires_grad, **kwargs):
    return (SampleInput(make_tensor((6,), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(2,),),
            SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=([1, 2, 3],),),)

def sample_inputs_vsplit(op_info, device, dtype, requires_grad, **kwargs):
    return (SampleInput(make_tensor((6, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(2,),),
            SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=([1, 2, 3],),),)

def sample_inputs_dsplit(op_info, device, dtype, requires_grad, **kwargs):
    return (SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=([1, 2, 3],),),
            SampleInput(make_tensor((S, S, 6), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(2,),),)

def error_inputs_hsplit(op_info, device, **kwargs):
    err_msg1 = ("torch.hsplit requires a tensor with at least 1 dimension, "
                "but got a tensor with 0 dimensions!")
    si1 = SampleInput(make_tensor((),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)
    err_msg2 = (f"torch.hsplit attempted to split along dimension 1, "
                f"but the size of the dimension {S} "
                f"is not divisible by the split_size 0!")
    si2 = SampleInput(make_tensor((S, S, S),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)

    # Incorrect type for indices_or_section argument
    err_msg3 = ("received an invalid combination of arguments.")
    si3 = SampleInput(make_tensor((S, S, S),
                                  dtype=torch.float32,
                                  device=device),
                      args=("abc",),)
    yield ErrorInput(si1, error_regex=err_msg1)
    yield ErrorInput(si2, error_regex=err_msg2)
    yield ErrorInput(si3, error_type=TypeError, error_regex=err_msg3)

def error_inputs_vsplit(op_info, device, **kwargs):
    err_msg1 = ("torch.vsplit requires a tensor with at least 2 dimension, "
                "but got a tensor with 1 dimensions!")
    si1 = SampleInput(make_tensor((S,),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)
    err_msg2 = (f"torch.vsplit attempted to split along dimension 0, "
                f"but the size of the dimension {S} "
                f"is not divisible by the split_size 0!")
    si2 = SampleInput(make_tensor((S, S, S),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)

    # Incorrect type for indices_or_section argument
    err_msg3 = ("received an invalid combination of arguments.")
    si3 = SampleInput(make_tensor((S, S, S),
                                  dtype=torch.float32,
                                  device=device),
                      args=("abc",),)
    yield ErrorInput(si1, error_regex=err_msg1)
    yield ErrorInput(si2, error_regex=err_msg2)
    yield ErrorInput(si3, error_type=TypeError, error_regex=err_msg3)

def error_inputs_dsplit(op_info, device, **kwargs):
    err_msg1 = ("torch.dsplit requires a tensor with at least 3 dimension, "
                "but got a tensor with 1 dimensions!")
    si1 = SampleInput(make_tensor((S,),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)
    err_msg2 = (f"torch.dsplit attempted to split along dimension 2, "
                f"but the size of the dimension {S} "
                f"is not divisible by the split_size 0!")
    si2 = SampleInput(make_tensor((S, S, S),
                                  dtype=torch.float32,
                                  device=device),
                      args=(0,),)
    return (ErrorInput(si1, error_regex=err_msg1),
            ErrorInput(si2, error_regex=err_msg2),)

def sample_inputs_linalg_multi_dot(op_info, device, dtype, requires_grad, **kwargs):
    # Each test case consists of the sizes in the chain of multiplications
    # e.g. [2, 3, 4, 5] generates matrices (2, 3) @ (3, 4) @ (4, 5)
    test_cases = [
        [1, 2, 1],
        [2, 0, 2],
        [0, 2, 2],
        [2, 2, 2, 2],
        [2, 3, 4, 5],
        [5, 4, 0, 2],
        [2, 4, 3, 5, 3, 2]
    ]

    result = []
    for sizes in test_cases:
        tensors = []
        for size in zip(sizes[:-1], sizes[1:]):
            t = make_tensor(size, dtype=dtype, device=device, requires_grad=requires_grad)
            tensors.append(t)
        result.append(SampleInput(tensors))

    return result

def sample_inputs_linalg_matrix_norm(op_info, device, dtype, requires_grad, **kwargs):
    low_precision_dtypes = (torch.float16, torch.bfloat16, torch.complex32)
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    sizes = ((2, 2), (2, 3, 2))
    if dtype in low_precision_dtypes:
        # svdvals not supported for low precision dtypes
        ords = ('fro', inf, -inf, 1, -1)
    else:
        ords = ('fro', 'nuc', inf, -inf, 1, -1, 2, -2)
    dims = ((-2, -1), (-1, 0))

    for size, ord, dim, keepdim in product(sizes, ords, dims, [True, False]):
        yield SampleInput(make_arg(size), args=(ord, dim, keepdim))

def sample_inputs_linalg_norm(op_info, device, dtype, requires_grad, *, variant=None, **kwargs):
    if variant is not None and variant not in ('subgradient_at_zero',):
        raise ValueError(f"Unsupported variant, expected variant to be 'subgradient_at_zero' but got: {variant}")

    test_sizes = [
        (S,),
        (0,),
        (S, S),
        (0, 0),
        (S, 0),
        (0, S),
        (S, S, S),
        (0, S, S),
        (S, 0, S),
        (0, 0, 0),
    ]

    vector_ords = (None, 0, 0.5, 1, 2, 3.5, inf, -0.5, -1, -2, -3.5, -inf)
    matrix_ords = (None, 'fro', 'nuc', 1, 2, inf, -1, -2, -inf)

    inputs = []

    for test_size in test_sizes:
        is_vector_norm = len(test_size) == 1
        is_matrix_norm = len(test_size) == 2

        for keepdim in [False, True]:
            if not variant == 'subgradient_at_zero':
                inputs.append(SampleInput(
                    make_tensor(
                        test_size, dtype=dtype, device=device, low=None, high=None,
                        requires_grad=requires_grad),
                    kwargs=dict(
                        keepdim=keepdim)))

            if not (is_vector_norm or is_matrix_norm):
                continue

            ords = vector_ords if is_vector_norm else matrix_ords

            for ord in ords:
                if variant == 'subgradient_at_zero':
                    inputs.append(SampleInput(
                        torch.zeros(
                            test_size, dtype=dtype, device=device,
                            requires_grad=requires_grad),
                        args=(ord,),
                        kwargs=dict(keepdim=keepdim)))
                else:
                    inputs.append(SampleInput(
                        make_tensor(
                            test_size, dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad),
                        args=(ord,),
                        kwargs=dict(
                            keepdim=keepdim)))

                    if ord in ['nuc', 'fro']:
                        inputs.append(SampleInput(
                            make_tensor(
                                test_size, dtype=dtype, device=device,
                                low=None, high=None,
                                requires_grad=requires_grad),
                            kwargs=dict(
                                ord=ord,
                                keepdim=keepdim,
                                dim=(0, 1))))

        return inputs

def sample_inputs_as_strided(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # input shape, output shape, output stride, output storage offset
    test_cases = (
        ((1,), (1,), (1,), 0),
        ((3, 3), (2, 2), (1, 2), 0),
        ((3, 3), (2, 2), (1, 2), 1),
        ((16,), (2, 2, 2, 2), (1, 1, 1, 1), 0),
        ((16,), (2, 1, 1, 2), (1, 7, 7, 1), 0),
    )

    for input_shape, output_shape, stride, storage_offset in test_cases:
        input_t = make_arg(input_shape)
        kwargs = dict(storage_offset=storage_offset)
        yield SampleInput(input_t, args=(output_shape, stride), kwargs=kwargs)

    # as_strided on offset, partial views
    # yield SampleInput(make_arg((20,))[5:15], args=((2, 2), (1, 2)))
    # yield SampleInput(make_arg((20,))[5:15], args=((2, 2), (1, 2)), kwargs={'storage_offset': 0})

def sample_inputs_as_strided_scatter(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # input shape, output shape, output stride, output storage offset
    test_cases = [
        ((1,), (1,), (1,), 0),
        ((3, 3), (2, 2), (1, 2), 0),
        ((3, 3), (2, 2), (1, 2), 1),
        ((16,), (2, 2, 2, 2), (1, 1, 1, 1), 0),
        ((16,), (2, 1, 1, 2), (1, 7, 7, 1), 0),
    ]

    samples = []

    for input_shape, output_shape, stride, storage_offset in test_cases:
        input_t = make_arg(input_shape)
        input_src = make_arg(output_shape)
        kwargs = dict(storage_offset=storage_offset)
        samples.append(SampleInput(input_t, args=(input_src, output_shape, stride), kwargs=kwargs))

    return samples

def sample_inputs_combinations(op_info, device, dtype, requires_grad, **kwargs):
    inputs = (
        (0,),
        (0, 1),
        (0, 1, 2, 3),
    )

    rvals = [1, 2, 4]

    products = product(inputs, rvals, [False, True])

    samples = []

    for input_data, r, with_replacement in products:
        input_t = torch.tensor(input_data, device=device, dtype=dtype, requires_grad=requires_grad)
        kwargs = dict(r=r, with_replacement=with_replacement)

        samples.append(SampleInput(input_t, kwargs=kwargs))

    return tuple(samples)

def sample_inputs_cartesian_prod(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(torch.tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # constructs 1-D tensors with varying number of elements
    a = make_arg((0,))
    b = make_arg((0, 1))
    c = make_arg((0, 1, 2, 3))

    samples = []

    # sample with only 1 tensor
    samples.append(SampleInput(
        a
    ))

    # sample with 2 tensors
    samples.append(SampleInput(
        a,
        args=(b,)
    ))

    # sample with 3 tensors
    samples.append(SampleInput(
        a,
        args=(b, c)
    ))

    return tuple(samples)

def sample_inputs_cosine_similarity(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as input_shape, dict of dim and eps
    cases: Tuple[tuple, dict] = (  # type: ignore[assignment]
        ((S, S), {'dim': 1}),
        ((S, 2), {'dim': -1}),
        ((S,), {'dim': 0, 'eps': 0.5}),
        ((), {'dim': 0}),
        ((S, S, M), {'dim': 2}),
        ((S, S), {})
    )

    for input_shape, kwargs in cases:
        yield SampleInput(make_arg(input_shape), args=(make_arg(input_shape),), kwargs=kwargs)
    # Test for Broadcasting
    yield SampleInput(make_arg((1, 2, 3)), args=(make_arg((2, 1, 3)),), kwargs={'dim': -1})
    yield SampleInput(make_arg((1, 2, 3)), args=(make_arg((2, 1, 3)),), kwargs={'dim': -2})
    yield SampleInput(make_arg((2, 3)), args=(make_arg((2, 1, 3)),), kwargs={'dim': -1})

def sample_inputs_batch_norm(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_arg_without_requires_grad = partial(make_tensor, device=device, dtype=dtype, requires_grad=False)

    # Ordered as: input shape, kwargs for training, momentum, eps
    cases: Tuple[Tuple[int], dict] = (  # type: ignore[assignment]
        ((S, S, S), {'training': True, 'momentum': 0.5, 'eps': 0.6}),
        ((3, 2, 4), {'training': False, 'momentum': -1.2}),
        ((3, 1), {'training': True, 'momentum': 0.0}),
        ((0,), {'training': True}),
        ((0,), {'training': False}),
        ((3, 2, 3, 4), {'training': True, 'momentum': -1.0, 'eps': 0.5}),
        ((3, 2, 3, 4), {'training': False, 'momentum': -1.0, 'eps': 0.5}),
        ((2, 1), {}),
    )

    for input_shape, kwargs in cases:
        # args: running mean, running var, weight and bias should necessarily be of shape: (channels,)
        channels = input_shape[1] if len(input_shape) > 1 else 0
        weight = make_arg(channels) if channels > 0 else None
        bias = make_arg(channels) if channels > 0 else None
        running_mean = make_arg_without_requires_grad(channels, low=0)
        running_var = make_arg_without_requires_grad(channels, low=0)

        yield SampleInput(
            make_arg(input_shape),
            args=(
                running_mean,
                running_var,
                weight,
                bias
            ),
            kwargs=kwargs
        )

    # Checking for permutations of weights and biases as `None`
    weights = [channels, None, None]
    biases = [None, channels, None]
    is_training = [True, False, False]

    for weight, bias, training in zip(weights, biases, is_training):
        yield SampleInput(
            make_arg(input_shape),
            args=(
                running_mean,
                running_var,
                make_arg(channels),
                make_arg(channels)
            ),
            kwargs={'training': training}
        )

    # Test case for no optional kwargs
    # running_mean and running_var are required in evaluation mode (training: False) but not in training mode
    yield SampleInput(make_arg((1, 2, 3)), args=(None, None), kwargs={'training': True})

def sample_inputs_nn_activation_relu(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        (()),
        ((S, )),
        ((S, S)),
        ((S, M, S))
    )

    for shape in cases:
        yield SampleInput(make_arg(shape))

def sample_inputs_prelu(op_info, device, dtype, requires_grad, **kwargs):
    op_kwargs = op_info.sample_kwargs(device, dtype, None)[0]
    yield from sample_inputs_elementwise_unary(op_info, device, dtype, requires_grad,
                                               op_kwargs=op_kwargs)

    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        (()),
        ((S, )),
        ((S, S)),
        ((S, M, S))
    )

    for shape in cases:
        for weight in [-1., 0., 0.8, 1.]:
            weight_tensor = torch.tensor(weight, device=device, dtype=dtype, requires_grad=requires_grad)
            yield SampleInput(make_arg(shape), args=(weight_tensor,))

        if len(shape) >= 2:
            channel_size = shape[1]
            yield SampleInput(make_arg(shape), args=(make_arg((channel_size,)),))
    weight_tensor = torch.tensor(1., device=device, dtype=dtype, requires_grad=requires_grad)

    yield SampleInput(make_arg((S, S)), kwargs=dict(weight=weight_tensor,))
    yield SampleInput(make_arg((S, S)), kwargs=dict(weight=make_arg((S,)),))

def reference_inputs_prelu(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_prelu(op, device, dtype, requires_grad, **kwargs)
    yield from reference_inputs_elementwise_unary(op, device, dtype, requires_grad, **kwargs)

def sample_kwargs_prelu_scalar_weight(device, dtype, input):
    weight = torch.rand(tuple(), device=device, dtype=dtype)
    # NumPy does not support bfloat16, so we default to float32 (only for NumPy) in that case
    if dtype == torch.bfloat16:
        weight_cpu = weight.to(dtype=torch.float32, device="cpu")
    else:
        weight_cpu = weight.cpu()
    np_weight = weight_cpu.numpy()
    return ({'weight': weight}, {'weight': np_weight})

def error_inputs_prelu(op, device):
    # Weight has numel != 1, but self.ndim is zero-dim tensor
    inp = make_tensor(tuple(), device=device, dtype=torch.float32)
    weight = make_tensor((2,), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(inp, kwargs={'weight': weight}),
                     error_regex="Not allow zero-dim input tensor.")

    # Weight has numel != 1, but numel does not match channel size
    inp = make_tensor((2, 8, 3), device=device, dtype=torch.float32)
    weight = make_tensor((9,), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(inp, kwargs={'weight': weight}),
                     error_regex="Mismatch of parameter numbers and input channel size.")

    # Weight is neither a scalar nor 1-D tensor
    inp = make_tensor((2, 8, 3), device=device, dtype=torch.float32)
    weight = make_tensor((2, 4), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(inp, kwargs={'weight': weight}),
                     error_regex="prelu: Expected `weight` to be a scalar or 1D tensor, but got ndim = 2")

    # src and index tensors must have the same # of dimensions
def sample_inputs_norm(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # ord = inf is tested in inputs_norm_inf as it fails on some tests
    cases = [
        ((S, S), (2,), '2'),
        ((S, S), (0,), '0'),
        ((S, S), (0.5,), '0_5'),
        ((S, S), (1,), '1'),
        ((S, S), (3,), '3'),
        ((S, S), (-1,), 'neg_1'),
        ((S, S), (-2,), 'neg_2'),
        ((S, S), (-0.5,), 'neg_0_5'),
        ((S, S), (-1.5,), 'neg_1_5'),
    ]

    cases_nonzero_input = (
        ((S, S, S), (1.5,), '1_5_default'),
        ((S, S, S), (1.5, 1), '1_5_dim'),
        ((S, S, S), (1.5, -1), '1_5_neg_dim'),
        ((S, S, S), (1.5, 1, True), 'keepdim_1_5_dim'),
        ((S, S, S), (1.5, -1, True), 'keepdim_1_5_neg_dim'),
    )

    cases_posdim = (
        ((S, S), (-2, 1,), 'neg_2_dim'),
        ((S, S), (-1, 1,), 'neg_1_dim'),
        ((S, S), (0, 1,), '0_dim'),
        ((S, S), (1, 1,), '1_dim'),
        ((S, S), (2, 1,), '2_dim'),
        ((S, S), (3, 1,), '3_dim'),
        ((S, S, S), (2, 1), '2_dim'),
        ((S, S, S), (3, 1), '3_dim'),
        ((S, S, S), (2, 1, True), 'keepdim_2_dim'),
        ((S, S, S), (3, 1, True), 'keepdim_3_dim'),
        ((), (2, 0), '2_dim_scalar'),
        ((), (3, 0), '3_dim_scalar'),
        ((), (2, 0, True), 'keepdim_2_dim_scalar'),
        ((), (3, 0, True), 'keepdim_3_dim_scalar'),
    )

    cases_negdim = ((shape, args[:1] + (-args[1],) + args[2:], name.replace("_dim", "_neg_dim"))
                    for shape, args, name in cases_posdim)

    for shape, args, name in itertools.chain(cases, cases_posdim, cases_negdim):
        yield SampleInput(make_arg(shape), args=args, name=name)

    for shape, args, name in cases_nonzero_input:
        yield SampleInput(make_arg(shape, exclude_zero=True), args=args, name=name)


def sample_inputs_norm_fro(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        ((S, S), (), 'default'),
        ((S, S), ('fro',), 'fro_default'),
        ((S, S), ('fro', [0, 1],), 'fro'),
    )

    for shape, args, name in cases:
        yield SampleInput(make_arg(shape), args=args, name=name)


def sample_inputs_norm_nuc(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        ((S, S), ('nuc',), 'nuc'),
        ((S, S, S), ('nuc', [1, 2]), 'nuc_batched'),
    )

    for shape, args, name in cases:
        yield SampleInput(make_arg(shape), args=args, name=name)


def sample_inputs_norm_inf(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        ((S, S), (-inf,), '-inf'),
        ((S, S), (inf,), 'inf'),
        ((S, S), (inf, 1,), 'inf_2_dim'),
        ((S, S), (inf, -1,), 'inf_2_neg_dim'),
    )

    for shape, args, name in cases:
        yield SampleInput(make_arg(shape), args=args, name=name)

def sample_kwargs_vector_norm(t, **kwargs):
    # orders with / without identity
    def ords():
        has_id = (6, 4, 2, 1, 0, 0.9)
        no_id = (inf, -2.1, -inf)
        if t.numel() == 0:
            dim = kwargs.get("dim")
            if dim is None:
                return has_id
            if not isinstance(dim, Iterable):
                dim = (dim,)
            for d in dim:
                if t.size(d) == 0:
                    return has_id
        return has_id + no_id

    return (((), dict(ord=o)) for o in ords())

def sample_inputs_equal(op, device, dtype, requires_grad, **kwargs):
    make_arg = partial(
        make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes = (
        ((), ()),
        ((S,), ()),
        ((), (S,)),
        ((S, 1), (S,)),
        ((M, S), ()),
        ((S, S), (S, S))
    )

    for shape_lhs, shape_rhs in shapes:
        lhs = make_arg(shape_lhs)
        rhs = make_arg(shape_rhs)
        broadcasts_input = shape_lhs != torch.broadcast_shapes(shape_lhs, shape_rhs)

        yield SampleInput(lhs, args=(rhs,), broadcasts_input=broadcasts_input)
        if shape_lhs == shape_rhs:
            yield SampleInput(lhs, args=(lhs.clone().detach_(),))



def sample_inputs_jiterator(op, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes = (
        ((), ()),
        ((S,), ()),
        ((S, 1), (S,)),
        ((M, S), ()),
        ((S, M, S), (M, S)),
        ((S, M, S), (S, M, S)),
        ((M, 1, S), (M, S)),
        ((M, 1, S), (1, M, S)),
        ((0, 1, 3), (0, 10, 3))
    )

    num_inputs = kwargs.get('num_inputs')
    sample_kwargs = kwargs.get('sample_kwargs', {})

    for shape_lhs, shape_rhs in shapes:
        lhs = make_arg(shape_lhs)

        args = []
        for i in range(num_inputs - 1):
            args.append(make_arg(shape_rhs))
        broadcasts_input = (shape_lhs != torch.broadcast_shapes(shape_lhs, shape_rhs))

        yield SampleInput(lhs, args=tuple(args), kwargs=sample_kwargs, broadcasts_input=broadcasts_input)

def sample_inputs_broadcast_shapes(op, device, dtype, requires_grad, **kwargs):
    shapes = (
        ((), ()),
        ((S,), ()),
        ((S, 1), (S,)),
        ((S, 1), S),
        ((M, S), ()),
        ((S, M, S), (M, S)),
        ((S, M, S), (S, M, S)),
        ((M, 1, S), (M, S)),
        ((M, 1, S), (1, M, S)),
        ((0, 1, 3), (0, 10, 3))
    )

    for shape in shapes:
        inp, *arg0 = shape
        yield SampleInput(inp, args=tuple(arg0))

def sample_inputs_add_sub(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_elementwise_binary(op, device, dtype, requires_grad, **kwargs)

    # Adds alpha kwarg cases
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    lhs = make_arg((S, S), **op.lhs_make_tensor_kwargs)
    rhs = make_arg((S, S), **op.rhs_make_tensor_kwargs)
    if dtype is not torch.bool:
        yield SampleInput(lhs, args=(rhs,), kwargs={'alpha': 2})
    else:
        yield SampleInput(lhs, args=(rhs,), kwargs={'alpha': True})
    neg_alpha = -3.14 if (dtype.is_floating_point or dtype.is_complex) else -3
    lhs = make_arg((S, S), **op.lhs_make_tensor_kwargs)
    rhs = make_arg((S, S), **op.rhs_make_tensor_kwargs)
    if dtype is not torch.bool:
        yield SampleInput(lhs, args=(rhs,), kwargs={'alpha': neg_alpha})
    else:
        yield SampleInput(lhs, args=(rhs,), kwargs={'alpha': False})

def error_inputs_arange(op, device, **kwargs):
    yield ErrorInput(SampleInput(0, args=(3, 0)), error_type=RuntimeError, error_regex='step must be nonzer')
    yield ErrorInput(SampleInput(0, args=(-3, 2)), error_type=RuntimeError, error_regex='bound inconsistent with step sign')
    yield ErrorInput(SampleInput(0, args=(3, -2)), error_type=RuntimeError, error_regex='bound inconsistent with step sign')
    yield ErrorInput(SampleInput(0, args=(float('inf'), 2)), error_type=RuntimeError, error_regex='unsupported range')
    yield ErrorInput(SampleInput(float('-inf'), args=(1, 2)), error_type=RuntimeError, error_regex='unsupported range')

def sample_inputs_arange(op, device, dtype, requires_grad, **kwargs):
    int_samples = (
        # positive direction
        (-1, 2, 2),
        # negative direction
        (2, -3, -1),
        # start == end
        (1, 1, 1),
        (1, 1, -1),
        # divides evenly
        (0, -8, -4),
        (1, 5, 2),
        # bool
        (False, True, True),
        # default step
        (0, 1, None),
        # default start
        (None, 3, None),
    )

    def to_float(start, end, step):
        start = start + 0.1 if start is not None else None
        end = end + 0.1
        step = float(step) if step is not None else None
        return start, end, step

    float_samples = (
        # includes endpoint
        (0., -8. - 1e-6, -4.),
        (1., 5. + 1e-6, 2.),
        (0., -8., -4.),
        (1., 5., 2.),
        *(to_float(start, end, step) for (start, end, step) in int_samples),
    )

    large_samples = (
        (0, 10000, None),
    )

    samples = int_samples + float_samples
    if dtype not in (torch.int8, torch.uint8):
        samples += large_samples

    for start, end, step in samples:
        if start is None:
            assert step is None
            yield SampleInput(end, kwargs={"dtype": dtype, "device": device})
        elif step is None:
            yield SampleInput(start, args=(end,), kwargs={"dtype": dtype, "device": device})
        else:
            yield SampleInput(start, args=(end, step), kwargs={"dtype": dtype, "device": device})

    yield SampleInput(2)
    yield SampleInput(1, args=(3, 1))


def error_inputs_linspace(op, device, **kwargs):
    yield ErrorInput(SampleInput(0, args=(3, -1)), error_type=RuntimeError, error_regex='number of steps must be non-negative')
    yield ErrorInput(SampleInput(0, args=(3, 1.)), error_type=TypeError, error_regex='must be int, not float')


def sample_inputs_linspace(op, device, dtype, requires_grad, **kwargs):
    ends = (-3, 0, 1, 4, 50)
    starts = (-2., 0, 4.3, 50)
    nsteps = (0, 1, 50)
    # Extra case to replicate off-by-one issue on CUDA
    cases = list(product(starts, ends, nsteps)) + [(0, 7, 50)]
    for start, end, nstep in cases:
        if dtype == torch.uint8 and end < 0 or start < 0:
            continue
        yield SampleInput(start, args=(end, nstep), kwargs={"dtype": dtype, "device": device})

    yield SampleInput(1, args=(3, 1))


def sample_inputs_logpace(op, device, dtype, requires_grad, **kwargs):
    ends = (-3, 0, 1.2, 2, 4)
    starts = (-2., 0, 1, 2, 4.3)
    nsteps = (0, 1, 2, 4)
    bases = (2., 1.1) if dtype in (torch.int8, torch.uint8) else (None, 2., 3., 1.1, 5.)
    for start, end, nstep, base in product(starts, ends, nsteps, bases):
        if dtype == torch.uint8 and end < 0 or start < 0:
            continue
        if nstep == 1 and isinstance(start, float) and not (dtype.is_complex or dtype.is_floating_point):
            # https://github.com/pytorch/pytorch/issues/82242
            continue
        if base is None:
            yield SampleInput(start, args=(end, nstep), kwargs={"dtype": dtype, "device": device})
        else:
            yield SampleInput(start, args=(end, nstep, base), kwargs={"dtype": dtype, "device": device})

    yield SampleInput(1, args=(3, 1, 2.))


def sample_inputs_isclose(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_elementwise_binary(op, device, dtype, requires_grad, **kwargs)

    # Creates additional inputs to test the rtol, atol, and equal_nan params
    rtols = [0., 1e-7]
    atols = [0., 1e-7]
    equal_nans = [False, True]

    products = product(rtols, atols, equal_nans)

    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    for rtol, atol, equal_nan in products:
        lhs = make_arg((S, S), **op.lhs_make_tensor_kwargs)
        rhs = make_arg((S, S), **op.rhs_make_tensor_kwargs)

        yield SampleInput(lhs, args=(rhs,),
                          kwargs=dict(rtol=rtol, atol=atol, equal_nan=equal_nan))


def error_inputs_isclose(op, device, **kwargs):
    make_float_arg = partial(make_tensor, device=device, dtype=torch.float, requires_grad=False)

    yield ErrorInput(SampleInput(make_float_arg(()), args=(make_float_arg(()),), kwargs={'rtol': -0.4}),
                     error_type=RuntimeError,
                     error_regex='rtol must be greater than or equal to zero')

    yield ErrorInput(SampleInput(make_float_arg(()), args=(make_float_arg(()),), kwargs={'atol': -0.4}),
                     error_type=RuntimeError,
                     error_regex='atol must be greater than or equal to zero')


def sample_inputs_linalg_vecdot(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    batches = ((), (0,), (1,), (5,))
    ns = (0, 1, 3, 5)
    for b, n in product(batches, ns):
        shape = b + (n,)
        yield SampleInput(make_arg(shape), args=(make_arg(shape),))
        for i in range(len(shape)):
            yield SampleInput(make_arg(shape), args=(make_arg(shape),), kwargs=dict(dim=i))


def sample_inputs_t(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    return (SampleInput(make_arg((1, 2))),
            SampleInput(make_arg((2,))),
            SampleInput(make_arg(())))


def sample_inputs_mm(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_arg_conj(size):
        return make_arg(size).conj().requires_grad_(requires_grad)

    first_shape, second_shape = (S, M), (M, S)

    yield SampleInput(make_arg(first_shape), args=(make_arg(second_shape),))

    if dtype.is_complex:
        yield SampleInput(make_arg(first_shape), args=(make_arg_conj(second_shape),))


def sample_inputs_addmm(op_info, device, dtype, requires_grad, **kwargs):
    alpha_val = kwargs.get('alpha', 2 + 3j if dtype.is_complex else 0.6)
    beta_val = kwargs.get('beta', 1 + 2j if dtype.is_complex else 0.2)
    tests_list = [
        ((2, 3), (2, 2), (2, 3), False)
    ]
    tests_with_lhs_broadcasting = [
        ((1,), (2, 2), (2, 3), True),
        ((), (2, 2), (2, 3), True)
    ]
    test_cases = tests_list + tests_with_lhs_broadcasting  # type: ignore[operator]

    sample_inputs = []

    for shape_a, shape_b, shape_c, broadcasts_input in test_cases:
        sample_inputs.append(
            SampleInput(
                make_tensor(shape_a, dtype=dtype, device=device, requires_grad=requires_grad),
                args=(
                    make_tensor(shape_b, dtype=dtype, device=device,
                                requires_grad=requires_grad),
                    make_tensor(shape_c, dtype=dtype, device=device,
                                requires_grad=requires_grad)),
                kwargs={'alpha': alpha_val, 'beta': beta_val},
                broadcasts_input=broadcasts_input))

    if dtype.is_complex:
        shape = (3, 3)
        sample_inputs.append(
            SampleInput(make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad),
                        args=(
                            make_tensor(shape, dtype=dtype, device=device).mH.requires_grad_(requires_grad),
                            make_tensor(shape, dtype=dtype, device=device,
                                        requires_grad=requires_grad)),
                        kwargs={'alpha': alpha_val, 'beta': beta_val},))
        sample_inputs.append(
            SampleInput(make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad),
                        args=(
                            make_tensor(shape, dtype=dtype, device=device,
                                        requires_grad=requires_grad),
                            make_tensor(shape, dtype=dtype, device=device).mH.requires_grad_(requires_grad)),
                        kwargs={'alpha': alpha_val, 'beta': beta_val},))
    return sample_inputs

def sample_inputs_sparse_sampled_addmm(op_info, device, dtype, requires_grad, **kwargs):
    alpha = 2 + 3j if dtype.is_complex else 0.6
    beta = 1 + 2j if dtype.is_complex else 0.2

    def generator():
        # sparse.sampled_addmm performs: alpha * (A @ B) * sparse_ones_like(C) + beta * C
        for m, n, k in itertools.product([0, 5], repeat=3):
            yield SampleInput(
                torch.eye(m, n, device=device, dtype=dtype)
                .to_sparse_csr()
                .requires_grad_(requires_grad),
                args=(
                    make_tensor(
                        (m, k),
                        device=device,
                        dtype=dtype,
                        requires_grad=requires_grad,
                    ),
                    make_tensor(
                        (k, n),
                        device=device,
                        dtype=dtype,
                        requires_grad=requires_grad,
                    ),
                ),
                kwargs={"alpha": alpha, "beta": beta},
            )

    return list(generator())

def sample_inputs_mv(self, device, dtype, requires_grad, **kwargs):
    return (
        SampleInput(
            make_tensor((S, M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(
                make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            )
        ),
    )

def sample_inputs_bmm(self, device, dtype, requires_grad, **kwargs):
    return (
        SampleInput(
            make_tensor((M, S, M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(
                make_tensor((M, M, S, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            )
        ),
    )

def sample_inputs_dot_vdot(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_arg_conj(size):
        return make_arg(size).conj().requires_grad_(requires_grad)

    sample_inputs = []
    sample_inputs.append(SampleInput(make_arg((S, )), args=(make_arg((S, )),)))
    if dtype.is_complex:
        # dot/vdot for (conj(input), conj(arg_tensor)) and (conj(input), arg_tensor)
        # is tested in test_conj_view (which tests operations with only conjugated input tensor
        # -- not conjugated arg tensors)
        sample_inputs.append(SampleInput(make_arg((S, )), args=(make_arg_conj((S, )),)))
    return sample_inputs

def sample_inputs_addmv(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    test_cases = (((S,), (S, M), (M,), 1, 1, False),
                  ((S,), (S, M), (M,), 0.2, 0.6, False),
                  )

    test_cases_with_broadcast = (((1,), (S, M), (M,), 1, 1, True),
                                 ((1,), (S, M), (M,), 0.2, 0.6, True),
                                 ((), (S, M), (M,), 1, 1, True),
                                 ((), (S, M), (M,), 0.2, 0.6, True),
                                 )

    cases = test_cases + test_cases_with_broadcast

    # addmv performs: beta * M + alpha * (mat @ vec)
    for size, mat, vec, beta, alpha, broadcasts_input in cases:
        yield SampleInput(make_arg(size), args=(make_arg(mat), make_arg(vec)),
                          kwargs=dict(beta=beta, alpha=alpha), broadcasts_input=broadcasts_input)

def sample_inputs_addbmm(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # input_shape, batch1_shape, batch2_shape, beta_val, alpha_val, is_broadcasting
    test_cases = [((S, M), (S, S, S), (S, S, M), 1, 1, False),
                  ((1,), (S, S, S), (S, S, M), 1, 1, True),
                  ((S, M), (S, S, S), (S, S, M), 0.6, 0.2, False),
                  ((1,), (S, S, S), (S, S, M), 0.6, 0.2, True),
                  ((), (S, S, S), (S, S, M), 1, 1, True),
                  ((), (S, S, S), (S, S, M), 0.6, 0.2, True),
                  ]

    for input_shape, batch1_shape, batch2_shape, beta, alpha, is_broadcasting in test_cases:
        if dtype.is_complex:
            beta_complex, alpha_complex = beta * (1 + 2j), alpha * (2 + 3j)
            yield SampleInput(make_arg(input_shape), args=(make_arg(batch1_shape), make_arg(batch2_shape)),
                              kwargs=dict(beta=beta_complex, alpha=alpha_complex), broadcasts_input=is_broadcasting)
        yield SampleInput(make_arg(input_shape), args=(make_arg(batch1_shape), make_arg(batch2_shape)),
                          kwargs=dict(beta=beta, alpha=alpha), broadcasts_input=is_broadcasting)

def sample_inputs_addcmul_addcdiv(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = [(((S, S), (S, S), (S, S)), False),
                  (((S, S), (S, 1), (1, S)), False),
                  (((1,), (S, S, 1), (1, S)), True),
                  (((), (), ()), False),
                  (((S, S), (), ()), True),
                  (((), (S, S, 1), (1, S)), True)
                  ]

    sample_inputs = []
    for input_args, broadcasts_input in test_cases:
        # addcdiv should accept inputs with zero value
        # Currently, it throws ZeroDivisionError when the denominator is zero
        # TODO: exclude_zeros can be removed after https://github.com/pytorch/pytorch/issues/73638 is fixed
        args = tuple(make_tensor(arg, dtype=dtype, device=device, requires_grad=requires_grad,
                     exclude_zero=True) if isinstance(arg, tuple) else arg
                     for arg in input_args)
        sample_inputs.append(SampleInput(
            args[0],
            args=args[1:],
            broadcasts_input=broadcasts_input))

        # addcdiv should accept inputs with zero value
        # Currently, it throws ZeroDivisionError when the denominator is zero
        # TODO: exclude_zeros can be removed after https://github.com/pytorch/pytorch/issues/73638 is fixed
        args = tuple(make_tensor(arg, dtype=dtype, device=device, requires_grad=requires_grad,
                     exclude_zero=True) if isinstance(arg, tuple) else arg
                     for arg in input_args)
        sample_inputs.append(SampleInput(
            args[0],
            args=args[1:],
            kwargs=dict(value=3.14), broadcasts_input=broadcasts_input))

    return tuple(sample_inputs)

def sample_inputs_baddbmm(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = [((S, S, M), (S, S, S), (S, S, M), 1, 1, False),
                  ((1,), (S, S, S), (S, S, M), 1, 1, True),
                  ((S, S, M), (S, S, S), (S, S, M), 0.6, 0.2, False),
                  ((1,), (S, S, S), (S, S, M), 0.6, 0.2, True),
                  ((), (S, S, S), (S, S, M), 1, 1, True),
                  ((), (S, S, S), (S, S, M), 0.6, 0.2, True),
                  ]
    sample_inputs = []
    for (input_shape, batch1_shape, batch2_shape, alpha, beta, broadcasts_input) in test_cases:
        args = (make_tensor(input_shape, dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad),
                make_tensor(batch1_shape, dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad),
                make_tensor(batch2_shape, dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad))

        sample_inputs.append(SampleInput(args[0], args=(args[1], args[2]),
                             kwargs=dict(beta=beta, alpha=alpha), broadcasts_input=broadcasts_input))
        if dtype.is_complex:
            sample_inputs.append(SampleInput(
                args[0].clone().requires_grad_(requires_grad),
                args=(args[1].clone().requires_grad_(requires_grad),
                      args[2].clone().requires_grad_(requires_grad)),
                kwargs=dict(beta=beta * (1 + 2j), alpha=alpha * (2 + 3j)),
                broadcasts_input=broadcasts_input))

    if dtype.is_complex:
        shapes = [(S, S, S), (S, M, S), (S, S, M)]
        args = (make_tensor(shapes[0], dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad),
                make_tensor(shapes[1], dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad),
                make_tensor(shapes[2], dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad))
        sample_inputs.append(
            SampleInput(
                args[0].transpose_(-1, 1),
                args=(args[1].transpose(-1, 1).conj().requires_grad_(requires_grad),
                      args[2].transpose(-1, 1).conj().requires_grad_(requires_grad)),
                kwargs=dict(beta=beta * (1 + 2j), alpha=alpha * (2 + 3j)),))

    return tuple(sample_inputs)

# TODO: add reduction kwargs
def sample_inputs_multilabel_soft_margin_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes = (
        (S,),
        (S, S),
    )

    for shape in shapes:
        # Produce one with weight and one without.
        yield SampleInput(_make_tensor(shape), args=(_make_tensor(shape, requires_grad=False),), kwargs={})
        yield SampleInput(_make_tensor(shape), args=(_make_tensor(shape, requires_grad=False),),
                          kwargs={'weight': _make_tensor(shape, requires_grad=False)})

def sample_inputs_addr(op_info, device, dtype, requires_grad, **kwargs):
    yield SampleInput(
        make_tensor((S, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        args=(
            make_tensor((S, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)))

    yield SampleInput(
        make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        args=(
            make_tensor((S, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)),
        broadcasts_input=True)

    if dtype.is_complex:
        alpha, beta = 0.1 + 0.3j, 0.4 + 0.6j
    elif dtype.is_floating_point:
        alpha, beta = 0.2, 0.6
    else:
        alpha, beta = 2, 3

    yield SampleInput(
        make_tensor((S, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        args=(
            make_tensor((S, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)),
        kwargs=dict(beta=beta, alpha=alpha))

    yield SampleInput(
        make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        args=(
            make_tensor((S, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)),
        kwargs=dict(beta=beta, alpha=alpha),
        broadcasts_input=True)

    # These samples fail gradcheck
    if dtype.is_floating_point and not requires_grad:
        yield SampleInput(
            torch.tensor([[math.nan]], device=device, requires_grad=requires_grad),
            args=(
                torch.tensor([0.0], device=device, requires_grad=requires_grad),
                torch.tensor([0.0], device=device, requires_grad=requires_grad),
            ),
            kwargs=dict(beta=0.0, alpha=0.0),
            broadcasts_input=True)

        yield SampleInput(
            torch.tensor([[0.0]], device=device, requires_grad=requires_grad),
            args=(
                torch.tensor([math.nan], device=device, requires_grad=requires_grad),
                torch.tensor([math.nan], device=device, requires_grad=requires_grad),
            ),
            kwargs=dict(beta=0.0, alpha=0.0),
            broadcasts_input=True)

def sample_inputs_zero_(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = ((), (S, S, S), (S,))

    for shape in cases:
        yield(SampleInput(make_arg(shape)))

# TODO: add reduction kwargs
def sample_inputs_multi_margin_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_target = partial(_make_tensor, dtype=torch.long, requires_grad=False)

    inputs = (
        ((), make_target([], low=0, high=1), {}),
        ((S,), make_target([], low=0, high=S), {"p": 1}),
        ((S,), make_target([1], low=0, high=S), {"p": 2}),
        ((S, M), make_target([S], low=0, high=M), {"margin": 1.0}),
        ((M, S), make_target([M], low=0, high=S), {"weight": None}),
    )

    for input_shape, target, kwargs in inputs:
        yield SampleInput(_make_tensor(input_shape), args=(target,), kwargs=kwargs)

def sample_inputs_logsumexp(self, device, dtype, requires_grad, **kwargs):
    inputs = (
        ((), (0,), True),
        ((S, S), (1,), True),
        ((S, S), (1,), False),
        ((S, S), (-2,), False),
        ((S, S), (0, 1), False),
    )
    samples = []
    # Test large inputs to check numerical stability
    lows = (None, 1e3, 1e6) if dtype in (torch.float32, torch.float64) else (None,)
    for low in lows:
        high = low * 2 if low is not None else None
        for shape, dim, keepdim in inputs:
            t = make_tensor(shape, dtype=dtype, device=device,
                            low=low, high=high,
                            requires_grad=requires_grad)
            samples.append(SampleInput(t, args=(dim, keepdim)))

    return tuple(samples)

def sample_inputs_like_fns(self, device, dtype, requires_grad, **kwargs):
    inputs = [
        ((), {}),
        ((S, S), {}),
        ((0, S, 0), {}),
        ((S,), {'dtype': dtype, 'device': device}),
        # Hard-code some dtypes/devices. We want to test cases where the
        # (dtype, device) is different from the input's (dtype, device)
        ((S,), {'dtype': torch.double}),
        ((S,), {'device': 'cpu'}),
        ((S,), {'dtype': torch.double, 'device': 'cpu'}),
    ]
    if torch.cuda.is_available():
        inputs.append(((S,), {'device': 'cuda'}))

    samples = []
    for shape, kwargs in inputs:
        t = make_tensor(shape, dtype=dtype, device=device,
                        low=None, high=None,
                        requires_grad=requires_grad)
        samples.append(SampleInput(t, kwargs=kwargs))

    return tuple(samples)

def reference_inputs_like_fns(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_like_fns(op, device, dtype, requires_grad, **kwargs)

    # shape
    cases = (
        (), (0,), (1, 0), (1, 1, 4, 5), (5, 3, 0, 1), (1, 4, 3, 1, 1)
    )

    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for shape in cases:
        yield SampleInput(make_arg(shape))
        yield SampleInput(make_arg(shape).transpose(0, -1))
        yield SampleInput(make_arg(shape, noncontiguous=True))
        yield SampleInput(make_arg(shape, noncontiguous=True).transpose(0, -1))

# TODO: add reduction kwargs
def sample_inputs_multilabel_margin_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_target = partial(_make_tensor, dtype=torch.long, requires_grad=False)

    inputs = (
        ([], make_target([], low=0, high=1)),
        ([S], make_target([S], low=0, high=S)),
        ([M, S], make_target([M, S], low=0, high=S)),
    )

    for shape, target in inputs:
        yield SampleInput(_make_tensor(shape), args=(target,))

def get_independent_tensor(tensor):
    return tensor.clone().requires_grad_(tensor.requires_grad)

def sample_inputs_randint_like(self, device, dtype, requires_grad, **kwargs):
    samples = []
    low = 2
    high = 10

    for sample in sample_inputs_like_fns(self, device, dtype, requires_grad, **kwargs):
        # With high
        samples.append(SampleInput(
            sample.input,
            args=(high,) + sample.args,
            kwargs=sample.kwargs))
        # With low and high
        samples.append(SampleInput(
            get_independent_tensor(sample.input),
            args=(low, high,) + sample.args,
            kwargs=sample.kwargs))
    return tuple(samples)

def sample_inputs_margin_ranking_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes = (
        (),
        (S,),
        (S, S),
        (S, S, S),
    )

    margins = (0., 1.)
    reductions = ('sum', 'mean', 'none')

    for shape in shapes:
        for margin, reduction in product(margins, reductions):
            kwargs = {'margin': margin, 'reduction': reduction}
            yield SampleInput(_make_tensor(shape),
                              args=(_make_tensor(shape, requires_grad=False),
                                    _make_tensor(shape, requires_grad=False)),
                              kwargs=kwargs)

def reference_inputs_margin_ranking_loss(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_margin_ranking_loss(op, device, dtype, requires_grad, **kwargs)
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    for reduction in ('sum', 'mean', 'none'):
        if dtype.is_floating_point:  # only supports ints and floats
            # NaN propagation
            inp1 = make_input((10, ))
            inp1[2] = float('nan')
            inp2 = make_input((10, ))
            inp2[4] = float('nan')
            target = make_input((10, ))
            inp2[9] = float('nan')
            yield SampleInput(inp1, args=(inp2, target), kwargs={'reduction': reduction})

            # Inf handling
            inp1 = make_input((10, ))
            inp2[1] = float('inf')
            inp2 = make_input((10, ))
            inp2[4] = float('inf')
            target = make_input((10, ))
            inp2[7] = float('inf')
            yield SampleInput(inp1, args=(inp2, target), kwargs={'reduction': reduction})

        # Broadcasting
        inp1 = make_input((5, 2))
        inp2 = make_input((5, 1))
        target = make_input((1, 2))
        yield SampleInput(inp1, args=(inp2, target), kwargs={'reduction': reduction})

def error_inputs_margin_ranking_loss(op, device, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=torch.float32)
    # invalid reduction value.
    yield ErrorInput(SampleInput(make_input(5, 4), args=(make_input(5, 4), make_input(5, 4),), kwargs={'reduction': 'abc'}),
                     error_type=ValueError, error_regex='is not a valid value')
    # invalid input shapes
    yield ErrorInput(SampleInput(make_input(5, 4), args=(make_input(5, 4), make_input(5,),)),
                     error_regex='margin_ranking_loss : All input tensors should')

def sample_inputs_new_fns(self, device, dtype, requires_grad, **kwargs):
    inputs = [
        ((), (), {}),
        ((S, S), (2, 0), {}),
        ((0, S, 0), (3, 2, 2), {}),
        ((S,), (2, 3), {'dtype': dtype, 'device': device}),
        # Hard-code some dtypes/devices. We want to test cases where the
        # (dtype, device) is different from the input's (dtype, device)
        ((S,), (10,), {'dtype': torch.double}),
        ((S,), (1, 1, 12), {'device': 'cpu'}),
        ((S,), (2, 2, 2), {'dtype': torch.double, 'device': 'cpu'}),
    ]
    if torch.cuda.is_available():
        inputs.append(((S,), (7, 2), {'device': 'cuda'}))

    samples = []
    for input_shape, output_shape, kwargs in inputs:
        t = make_tensor(input_shape, dtype=dtype, device=device,
                        low=None, high=None,
                        requires_grad=requires_grad)
        samples.append(SampleInput(t, args=(output_shape,), kwargs=kwargs))

    return tuple(samples)

def sample_inputs_empty(op, device, dtype, requires_grad, **kwargs):
    # shape
    cases = (
        (), (0,), (1,), (1, 3, 5), (5, 3, 1), (1, 0, 5, 1),
    )

    for case in cases:
        _kwargs = {'device': device, 'dtype': dtype, 'requires_grad': requires_grad}
        yield SampleInput(case, args=(), kwargs=_kwargs)

def sample_inputs_new_full(self, device, dtype, requires_grad, **kwargs):
    def get_val(dtype):
        return make_tensor([], dtype=dtype, device="cpu").item()

    samples = []
    for sample in sample_inputs_new_fns(self, device, dtype, requires_grad, **kwargs):
        # The scalar we are passing to new_full must be the same dtype
        # as the one of the resulting tensor
        use_dtype = sample.kwargs['dtype'] if 'dtype' in sample.kwargs else dtype
        samples.append(SampleInput(
            sample.input, args=sample.args + (get_val(use_dtype),), kwargs=sample.kwargs))
    return tuple(samples)

def sample_inputs_full_like(self, device, dtype, requires_grad, **kwargs):
    def get_val(dtype):
        return make_tensor([], dtype=dtype, device="cpu").item()

    inputs = [
        ((), get_val(dtype), {}),
        ((S, S), get_val(dtype), {}),
        ((0, S, 0), get_val(dtype), {}),
        ((S,), get_val(dtype), {'dtype': dtype, 'device': device}),
        # Hard-code some dtypes/devices. We want to test cases where the
        # (dtype, device) is different from the input's (dtype, device)
        ((S,), get_val(torch.double), {'dtype': torch.double}),
        ((S,), get_val(dtype), {'device': 'cpu'}),
        ((S,), get_val(torch.double), {'dtype': torch.double, 'device': 'cpu'}),
    ]
    if torch.cuda.is_available():
        inputs.append(((S,), get_val(dtype), {'device': 'cuda'}))

    samples = []
    for shape, fill_value, kwargs in inputs:
        t = make_tensor(shape, dtype=dtype, device=device,
                        low=None, high=None,
                        requires_grad=requires_grad)
        samples.append(SampleInput(t, args=(fill_value,), kwargs=kwargs))

    return tuple(samples)

def sample_inputs_multinomial(self, device, dtype, requires_grad, **kwargs):
    cases = [
        ([3], 3, dict()),
        ([10], 3, dict()),
        ([3, 10], 3, dict()),
        ([3], 3, dict(replacement=False)),
        ([3], 3, dict(replacement=True)),
        ([3, 4], 4, dict(replacement=True)),
        ([3, 4], 4, dict(replacement=False)),
    ]

    samples = []
    for shape, num_samples, kwargs in cases:
        t = make_tensor(shape, dtype=dtype, device=device,
                        low=0, high=None,
                        requires_grad=requires_grad)
        samples.append(SampleInput(t, args=(num_samples,), kwargs=kwargs))
    return tuple(samples)

def sample_inputs_normal_common(self, device, dtype, requires_grad, cases, **kwargs):
    def get_value_or_make_tensor(value_or_shape):
        if isinstance(value_or_shape, list):
            return make_tensor(value_or_shape, dtype=dtype, device=device,
                               low=0, high=None,
                               requires_grad=requires_grad)
        return value_or_shape

    samples = []
    for value_or_mean_shape, value_or_std_shape, kwargs in cases:
        mean = get_value_or_make_tensor(value_or_mean_shape)
        std = get_value_or_make_tensor(value_or_std_shape)
        samples.append(SampleInput(mean, args=(std,), kwargs=kwargs))
    return tuple(samples)

def sample_inputs_normal_tensor_first(self, device, dtype, requires_grad, **kwargs):
    # value_or_size, value_or_size, kwargs
    cases = [
        ([], [], {}),
        ([3], [3], {}),
        ([3, 4, 2], [3, 4, 2], {}),
        ([2, 3], 1.1, {}),
        ([1, 2, 3], [5, 2, 3], {}),  # broadcasting
    ]

    return sample_inputs_normal_common(self, device, dtype, requires_grad, cases, **kwargs)

def sample_inputs_normal_tensor_second(self, device, dtype, requires_grad, **kwargs):
    cases = [
        ([3, 4], 0.3, {}),
    ]
    return sample_inputs_normal_common(self, device, dtype, requires_grad, cases, **kwargs)

def sample_inputs_bernoulli(self, device, dtype, requires_grad, **kwargs):
    shapes = [
        [3],
        [],
        [0, 3],
        [2, 3, 4],
    ]

    samples = []
    for shape in shapes:
        t = make_tensor(shape, dtype=dtype, device=device,
                        low=0, high=1,
                        requires_grad=requires_grad)
        samples.append(SampleInput(t))
    return tuple(samples)

def sample_inputs_logcumsumexp(self, device, dtype, requires_grad, **kwargs):
    inputs = (
        ((S, S, S), 0),
        ((S, S, S), 1),
        ((), 0),
    )
    samples = []

    for large_number in (True, False):
        for shape, dim in inputs:
            t = make_tensor(shape, dtype=dtype, device=device,
                            low=None, high=None,
                            requires_grad=requires_grad)

            if large_number and t.dim() > 0:
                t[0] = 10000
            samples.append(SampleInput(t, args=(dim,)))

    return tuple(samples)

def sample_inputs_trace(self, device, dtype, requires_grad, **kwargs):
    return (SampleInput((make_tensor((S, S), dtype=dtype, device=device,
                                     low=None, high=None,
                                     requires_grad=requires_grad))),)


def error_inputs_trace(op, device):
    yield ErrorInput(SampleInput(make_tensor((3, 4, 5), dtype=torch.float32, device=device)), error_regex="expected a matrix")


def sample_inputs_renorm(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    cases = (((S, S, S), (2, 1, 0.5)),
             ((S, S, S), (2, -1, 0.5)),
             ((S, S, S), (1, 2, 3)),
             ((S, S, S), (float('inf'), 2, 0.5)),
             )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)


def sample_inputs_transpose_swapdims(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((1, 2, 3), (-1, -2)),
             ((1, 2, 3), (-1, 2)),
             ((1, 2, 3), (1, -2)),
             ((1, 2, 3), (1, 2)),
             ((), (0, 0)),
             ((1, ), (0, 0)),
             ((M, M), (0, 1)),
             ((S, S, S), (2, 0)), )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)

def _numpy_ref_transpose(a, dim0, dim1):
    if a.ndim <= 1:
        return a

    return np.swapaxes(a, dim0, dim1)

def sample_inputs_adjoint(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    shapes = ((1, 2, 3), (), (M, M), (S, S, S), (S, M, S), (M, S, M, S))
    return (SampleInput(make_arg(shape)) for shape in shapes)

def sample_inputs_T(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    shapes = ((), (M, M))
    return (SampleInput(make_arg(shape)) for shape in shapes)


def sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates invertible inputs for linear algebra ops
    The input is generated as the itertools.product of 'batches' and 'ns'.
    In total this function generates 8 SampleInputs
    'batches' cases include:
        () - single input,
        (0,) - zero batched dimension,
        (2,) - batch of two matrices,
        (1, 1) - 1x1 batch of matrices
    'ns' gives 0x0 and 5x5 matrices.
    Zeros in dimensions are edge cases in the implementation and important to test for in order to avoid unexpected crashes.
    """
    make_fn = make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_fn, dtype=dtype, device=device, requires_grad=requires_grad)

    batches = [(), (0, ), (2, ), (1, 1)]
    ns = [5, 0]

    for batch, n in product(batches, ns):
        yield SampleInput(make_arg(*batch, n, n))

def sample_inputs_linalg_pinv_singular(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function produces factors `a` and `b` to generate inputs of the form `a @ b.t()` to
    test the backward method of `linalg_pinv`. That way we always preserve the rank of the
    input no matter the perturbations applied to it by the gradcheck.
    Note that `pinv` is Frechet-differentiable in a rank-preserving neighborhood.
    """
    batches = [(), (0, ), (2, ), (1, 1)]
    # the size of at least 30 is required to cause failures for the previous implicit implementation
    # of the pinv's backward method, albeit it is slow.
    size = [0, 3, 50]

    for batch, m, n in product(batches, size, size):
        for k in range(min(3, min(m, n))):
            # Note that by making the columns of `a` and `b` orthonormal we make sure that
            # the product matrix `a @ b.t()` has condition number 1 when restricted to its image
            a = torch.rand(*batch, m, k, device=device, dtype=dtype).qr().Q.requires_grad_(requires_grad)
            b = torch.rand(*batch, n, k, device=device, dtype=dtype).qr().Q.requires_grad_(requires_grad)
            yield SampleInput(a, args=(b,))


def sample_inputs_singular_matrix_factors(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function produces two tensors of shape (*, m, k) and (*, n, k) with k <= min(m, n).
    Their matrix product could be used to generate tensor of shape (*, m, n) of rank k.
    """

    batches = [(), (0, ), (2, ), (1, 1)]
    size = [1, 5, 10]

    for batch, m, n in product(batches, size, size):
        for k in range(min(3, min(m, n))):
            a = make_tensor((*batch, m, k), dtype=dtype, device=device, requires_grad=requires_grad)
            b = make_tensor((*batch, n, k), dtype=dtype, device=device, requires_grad=requires_grad)
            yield SampleInput(a, args=(b,), kwargs=kwargs)


def clone_sample(sample, **kwargs):
    """
    Given a SampleInput, this function analyzes its input, args and kwargs,
    and produces a copy with each non-Tensor entry being copied by reference,
    and with each Tensor entry cloned with `t.clone().requires_grad_(t.requires_grad)`
    """

    def clone_tensor(t):
        if isinstance(t, torch.Tensor):
            return t.detach().clone().requires_grad_(t.requires_grad)
        else:
            return t

    sample_kwargs = kwargs if kwargs else sample.kwargs

    return SampleInput(
        clone_tensor(sample.input),
        args=tuple(map(clone_tensor, sample.args)),
        kwargs=dict(((k, clone_tensor(v)) for k, v in sample_kwargs.items()))
    )


def sample_inputs_svd_lowrank(op_info, device, dtype, requires_grad=False, **kwargs):
    for sample in sample_inputs_singular_matrix_factors(op_info, device, dtype, requires_grad, **kwargs):
        *batch, m, k = sample.input.shape
        *_, n, _ = sample.args[0].shape

        # NOTE: since svd_lowrank relies on non rank-revealing SVD,
        # it inherits the problem of unstable behavior with repeated
        # singular values including zeros.
        # Since we want to avoid (repeated) zeros as singular values,
        # we can only use k for q.
        # This issues could be resolved with using a rank-revealing SVD
        # which does not include "zero" singular values.
        op_kwargs = {
            'q': k,
            'M': None
        }

        # without M specified
        yield clone_sample(sample, **op_kwargs)

        # now with M
        # TODO: fix bug in the documentation for svd_lowrank:
        # M has to be (*, m, n), and not (*, 1, n) as written
        # in the documentation
        op_kwargs['M'] = make_tensor((*batch, m, n), dtype=dtype, device=device, requires_grad=requires_grad)
        yield clone_sample(sample, **op_kwargs)

def chunk_iter(iterable, size):
    it = iter(iterable)
    while True:
        chunk = tuple(islice(it, size))
        if not chunk:
            break
        yield chunk

def sample_inputs_pca_lowrank(op_info, device, dtype, requires_grad=False, **kwargs):
    # we reuse samples from svd_lowrank which come in group of two with
    # kwarg['M'] = None and with kwarg['M'] = <some tensor>
    samples = sample_inputs_svd_lowrank(op_info, device, dtype, requires_grad, **kwargs)
    for s1, s2 in chunk_iter(samples, 2):
        del s1.kwargs['M']
        del s2.kwargs['M']
        s1.kwargs['center'] = False
        s2.kwargs['center'] = True
        yield s1
        yield s2

def sample_inputs_linalg_cond(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # autograd is not supported for inputs with zero number of elements
    shapes = ((S, S),
              (2, S, S),
              (2, 1, S, S), )

    for shape in shapes:
        yield SampleInput(make_arg(shape))


def sample_inputs_linalg_vander(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    shapes = ((),
              (1,),
              (S,),
              (2, S),)

    for shape in shapes:
        if len(shape) > 0 and shape[-1] > 1:
            yield SampleInput(make_arg(shape))
        n = shape[-1] if len(shape) > 0 else 1
        for i in range(3):
            # n-1, n, n+1
            N = n + i - 1
            if N < 2:
                continue
            yield SampleInput(make_arg(shape), kwargs=dict(N=N))

def np_vander_batched(x, N=None):
    # Wrapper around np.vander that supports batches of 1 dimension (enough for the tests)
    if x.ndim == 0:
        x = x[np.newaxis]
    if x.ndim == 1:
        y = np.vander(x, N=N, increasing=True)
        return y
    else:
        if N is None:
            N = x.shape[-1]
        y = np.vander(x.ravel(), N=N, increasing=True).reshape((*x.shape, N))
        return y

def np_sinc_with_fp16_as_fp32(x):
    # Wraps numpy's sinc function so that fp16 values are promoted to fp32
    # before sinc is invoked. Context: numpy's sinc returns NaN when evaluated
    # at 0 for fp16.
    if x.dtype == np.float16:
        return np.sinc(x.astype(np.float32))
    else:
        return np.sinc(x)

def sample_inputs_broadcast_to(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = (
        ((S, 1, 1), (S, S, S)),
        ((S, 1, S), (S, S, S)),
        ((S, 1), (S, S, S)),
        ((1,), (S, S, S)),
        ((1, S), (1, 1, S)),
        ((), ()),
        ((), (1, 3, 2)),
    )

    return tuple(
        SampleInput(
            make_tensor(size, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(shape,)) for size, shape in test_cases)

def sample_inputs_broadcast_tensors(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    test_cases: Tuple[tuple] = (((3,), (1, 2, 1), (1, 1), (5, 1, 1),),)

    samples: List[SampleInput] = []
    for shape, *other_shapes in test_cases:
        samples.append(SampleInput(make_arg(shape), args=tuple(make_arg(s) for s in other_shapes)))

    return samples

def reference_inputs_broadcast_tensors(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_broadcast_tensors(op, device, dtype, requires_grad, **kwargs)

    m = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    n = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad, noncontiguous=True)

    cases = (
        ((), (1, 1), (1, 1, 7, 1), (3, 1, 1)),
        ((3, 5, 6), (1, 3, 5, 6), (1, 1, 1, 1, 6), (8, 3, 5, 6))
    )

    for a, b, c, d in cases:
        yield SampleInput(m(a), args=(m(b), m(c), m(d)))
        yield SampleInput(n(a), args=(n(b), n(c), n(d)))

def sample_inputs_block_diag(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    test_cases: Tuple[tuple] = (
        ((1, S), (2, S), (3, S),),
        ((S, 1), (S, 2), (S, 3),),
        ((1,), (2,), (3,),),
        ((2, S), (S,))
    )

    samples: List[SampleInput] = []
    for shape, *other_shapes in test_cases:
        samples.append(SampleInput(make_arg(shape), args=tuple(make_arg(s) for s in other_shapes)))
        # We also want to test mixed complex-non-complex inputs to block_diag
        if dtype == torch.complex32 or dtype == torch.complex64:
            non_complex_dtype = torch.float32 if dtype == torch.complex32 else torch.float64
            make_arg_non_complex = partial(make_tensor, dtype=non_complex_dtype, device=device, requires_grad=requires_grad)
            samples.append(SampleInput(make_arg_non_complex(shape), args=tuple(make_arg(s) for s in other_shapes)))

    return samples

def sample_inputs_cdist(op_info, device, dtype, requires_grad, **kwargs):
    small_S = 2
    test_cases = (
        ((S, S, 2), (S, S + 1, 2)),
        ((S, S), (S, S)),
        ((S, S, S), (S, S, S)),
        ((3, 5), (3, 5)),
        ((2, 3, 5), (2, 3, 5)),
        ((1, 2, 3), (1, 2, 3)),
        ((1, 1), (S, 1)),
        ((0, 5), (4, 5)),
        ((4, 5), (0, 5)),
        ((0, 4, 5), (3, 5)),
        ((4, 5), (0, 3, 5)),
        ((0, 4, 5), (1, 3, 5)),
        ((1, 4, 5), (0, 3, 5)),
        # Using S here would make this one test take 9s
        ((small_S, small_S, small_S + 1, 2), (small_S, small_S, small_S + 2, 2)),
        ((small_S, 1, 1, small_S), (1, small_S, small_S)),
        ((1, 1, small_S), (small_S, 1, small_S, small_S)),
    )

    samples = []
    for cm in ['use_mm_for_euclid_dist', 'donot_use_mm_for_euclid_dist']:
        # FIXME add an override for JIT and revert 0. back to 0
        # since it's accepted by eager
        for p in [0., 1., 2., 3., 0.5, 1.5, 2.5, float("inf")]:
            for t1_size, t2_size in test_cases:
                # The args should never be non-contiguous as this is not supported in the backward
                samples.append(SampleInput(
                    make_tensor(t1_size, dtype=dtype, device=device, requires_grad=requires_grad),
                    args=(make_tensor(t2_size, dtype=dtype, device=device, requires_grad=requires_grad), p, cm)))

    return samples


def sample_inputs_fill_(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype,
                       low=None, high=None, requires_grad=requires_grad)

    cases = (((S, S, S), (1,)),
             ((), (1,)),
             ((S, S, S), (make_arg(()),)))

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)

def _fill_np(a, value):
    a = a.copy()
    a.fill(value)
    return a

def _fill_aten(a, value):
    t = a * False
    with torch.no_grad():
        t.fill_(value)
    return t

def _fill_sample_kwargs(device, dtype, input):
    if dtype is torch.bool:
        value = True
    else:
        value = 3

    return ({'value': value}, {'value': value})

def sample_inputs_comparison_ops(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_elementwise_binary(op, device, dtype, requires_grad, **kwargs)

    # Adds a sample input where both tensors have the same values
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    lhs = make_arg((S, S))
    yield SampleInput(lhs, args=(lhs.clone(),))

def sample_inputs_stack(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # shape x number of tensors
    cases = (
        ((3, 4), 1),
        ((1, 2, 1, 4), 3),
        ((0, 1, 0), 2),)

    for shape, num_tensors in cases:
        tensors = []
        for _ in range(num_tensors):
            tensors.append(make_arg(shape))
        for dim in range(-1, len(shape) - 1):
            yield SampleInput(tensors, args=(dim,))

def sample_inputs_cat_concat(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases: Tuple[tuple, tuple, dict] = (  # type: ignore[assignment]
        ((S, S), (S, S), {'dim': -1}),
        ((S, S), (S, S), {'dim': 1}),
        ((M, S), (S, S), {'dim': 0}),  # different shapes
        ((1, 2, 3), (1, 2, 3), {'dim': -2}),
        ((0,), (0,), {'dim': 0}),  # empty tensor
        ((0, S), (S, S), {'dim': 0}),
        ((1,), (1,), {})  # dim not passed, fallback to default
    )

    for input_shape1, input_shape2, kwargs in cases:
        yield SampleInput([make_arg(input_shape1), make_arg(input_shape2)], kwargs=kwargs)

def reference_inputs_cat(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_cat_concat(op, device, dtype, requires_grad, **kwargs)

    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Noncontiguous type promoting tensors
    a = make_arg((3, 4, 2))
    b = make_arg((3, 2, 2), noncontiguous=True, dtype=torch.double)
    c = make_arg((3, 3, 2), dtype=torch.float16).permute(1, 0, 2)

    yield SampleInput((a, b, c), kwargs={'dim': 1})

    # Special 1D tensor with dim length of 0 case
    a = make_arg((0,))
    b = make_arg((3, 2, 2))

    yield SampleInput((a, b, a))
    yield SampleInput((a, a, a))

def _elementwise_type_promo_np(*args, type_promotion_kind):
    def _maybe_torch(x):
        if isinstance(x, np.ndarray):
            return torch.from_numpy(x)
        return x

    flattened = tree_flatten(args)[0]
    transformed = tuple(_maybe_torch(a) for a in flattened)
    result_dtype, _ = prims.utils.elementwise_dtypes(
        *transformed,
        type_promotion_kind=type_promotion_kind)
    return torch_to_numpy_dtype_dict[result_dtype]

def _cat_np(input_seq, dim=0):
    inputs = tuple(a for a in input_seq if not (a.ndim == 1 and a.size == 0))

    if len(inputs) == 0:
        np_dtype = _elementwise_type_promo_np(
            input_seq,
            type_promotion_kind=prims.utils.ELEMENTWISE_TYPE_PROMOTION_KIND.NO_OPMATH)
        return np.empty(0, dtype=np_dtype)

    return np.concatenate(inputs, axis=dim)

def _floor_divide_np(a, b):
    dtype = _elementwise_type_promo_np(
        a,
        b,
        type_promotion_kind=prims.utils.ELEMENTWISE_TYPE_PROMOTION_KIND.DEFAULT)
    if isinstance(a, np.ndarray):
        a = a.astype(dtype)
    if isinstance(b, np.ndarray):
        b = b.astype(dtype)
    return np.floor_divide(a, b)

def sample_inputs_hstack_dstack_vstack(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    tensor_shapes = (
        # First Tensor being 1-D is special
        # case for hstack
        ((S,), (S,), (S,)),
        ((S, S), (S, S), (S, S)),
    )
    for s1, s2, s3 in tensor_shapes:
        tensors = (make_arg(s1,), make_arg(s2,), make_arg(s3))
        yield SampleInput(tensors)

def error_inputs_hstack_dstack_vstack(op, device):
    make_arg = partial(make_tensor, dtype=torch.int32, device=device, requires_grad=False)
    tensor_shapes = (
        ((S,), (S, S, S, S), (S,)),
    )
    for s1, s2, s3 in tensor_shapes:
        tensors = (make_arg(s1,), make_arg(s2,), make_arg(s3))
        # Different dimension tensor
        yield ErrorInput(SampleInput(tensors), error_regex="Tensors must have same number of dimensions")

    # empty tensor list
    yield ErrorInput(SampleInput(()), error_regex="expects a non-empty TensorList")

def sample_inputs_unbind(op_info, device, dtype, requires_grad, **kwargs):
    # Note: we don't do any tests where we unbind along 0-length dims
    # because in that case unbind returns and empty tuple, and that breaks
    # some asumptions in some backward tests in test_ops.py
    shape_dims = (((S,), 0),
                  ((S, S), 0),
                  ((S, S), 1),
                  ((S, S), -1),
                  ((S, 0, S), 0),
                  ((S, S, S), 1),
                  )
    for shape, dim in shape_dims:
        yield SampleInput(make_tensor(shape, dtype=dtype, device=device,
                                      requires_grad=requires_grad),
                          args=(dim,))

def error_inputs_unbind(op_info, device):
    make_arg = partial(make_tensor, dtype=torch.int32, device=device, requires_grad=False)
    yield ErrorInput(SampleInput(make_arg(()), args=(0,)), error_type=IndexError,
                     error_regex="Dimension specified as 0 but tensor has no dimensions")
    yield ErrorInput(SampleInput(make_arg((2,)), args=(2,)), error_type=IndexError,
                     error_regex="Dimension out of range")

def reference_unbind(t, dim):
    """A numpy implementation of torch.unbind"""
    return tuple(s.squeeze(dim) for s in np.split(t, t.shape[dim], dim))

def sample_inputs_gather(op_info, device, dtype, requires_grad, **kwargs):
    return (
        SampleInput(
            make_tensor((M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(0, gather_variable((S, S), 1, M, True, device=device))),
        SampleInput(
            make_tensor((M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(1, gather_variable((M, S // 2), 0, S, True, device=device))),
        SampleInput(
            make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(0, torch.tensor([0], dtype=torch.int64, device=device))),
        # Empty index tensor case, see: https://github.com/pytorch/pytorch/pull/65006
        SampleInput(
            make_tensor((S,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(0, torch.tensor([], dtype=torch.uint8, device=device))),
        SampleInput(
            make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=(0, torch.tensor(0, dtype=torch.int64, device=device))),
    )

def _fill_indices(idx, dim, dim_size, elems_per_row, m, n, o):
    for i in range(1 if dim == 0 else m):
        for j in range(1 if dim == 1 else n):
            for k in range(1 if dim == 2 else o):
                ii = [i, j, k]
                ii[dim] = slice(0, idx.size(dim) + 1)
                idx[tuple(ii)] = torch.randperm(dim_size)[0:elems_per_row]

def error_inputs_gather(op_info, device, **kwargs):
    # src is [1, 2]
    #        [3, 4]
    src = torch.tensor(((1, 2), (3, 4)), device=device, dtype=torch.float32)

    # idx is [0, 0]
    #        [1, 0]
    idx = torch.tensor(((0, 0), (1, 0)), device=device, dtype=torch.long)

    # Index should be smaller than self except on dimesion 1
    bad_src = make_tensor((1, 1), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(bad_src, args=(1, idx,)),
                     error_regex="Size does not match at dimension 0")

    # Index must have long dtype
    bad_idx = idx.to(torch.int32)
    yield ErrorInput(SampleInput(src, args=(1, bad_idx)),
                     error_regex="Expected dtype int64 for index")

    # TODO: FIXME
    # out.dtype must match src.dtype
    # Creates new src & idx since SampleInputs can't share tensors
    src = torch.tensor(((1, 2), (3, 4)), device=device, dtype=torch.float32)
    idx = torch.tensor(((0, 0), (1, 0)), device=device, dtype=torch.long)
    out = torch.empty((2, 2), device=device, dtype=torch.float64)
    yield ErrorInput(SampleInput(src, args=(1, idx), kwargs={'out': out}),
                     error_regex="Expected out tensor to have dtype")

    # src and index tensors must have the same # of dimensions
    # idx too few dimensions
    src = torch.tensor(((1, 2), (3, 4)), device=device, dtype=torch.float32)
    idx = torch.tensor((0, 0), device=device, dtype=torch.long)
    yield ErrorInput(SampleInput(src, args=(1, idx)),
                     error_regex="Index tensor must have the same number of dimensions")

    # src too few dimensions
    src = torch.tensor((1, 2), device=device, dtype=torch.float32)
    idx = torch.tensor(((0, 0), (1, 0)), device=device, dtype=torch.long)
    yield ErrorInput(SampleInput(src, args=(0, idx)),
                     error_regex="Index tensor must have the same number of dimensions")

    # index out of bounds
    # NOTE: this ErrorInput is guarded because bounds checking does not occur on CUDA devices
    if torch.device(device).type == 'cpu':
        src = torch.tensor(((1, 2), (3, 4)), device=device, dtype=torch.float32)
        idx = torch.tensor(((0, 23), (1, 0)), device=device, dtype=torch.long)
        yield ErrorInput(SampleInput(src, args=(1, idx,)),
                         error_regex="index 23 is out of bounds for dimension")

    x = torch.rand((1,), device=device).expand((3,))
    src = torch.rand((6,), device=device)
    ind = torch.tensor([2, 1, 0], device=device, dtype=torch.int64)

    yield ErrorInput(SampleInput(src, args=(0, ind,), kwargs=dict(out=x)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(src, args=(0, ind,), kwargs=dict(out=src)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(ind.clone(), args=(0, ind[1:],), kwargs=dict(out=ind[:1])),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

def error_inputs_take(op_info, device, **kwargs):
    x = torch.rand((1,), device=device).expand((3,))
    src = torch.rand((6,), device=device)
    ind = torch.tensor([2, 1, 0], device=device, dtype=torch.int64)

    yield ErrorInput(SampleInput(src, args=(ind,), kwargs=dict(out=x)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(src, args=(ind,), kwargs=dict(out=src)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(ind.clone(), args=(ind[1:],), kwargs=dict(out=ind[:-1])),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

# Error inputs for scatter
def error_inputs_scatter_and_scatter_add(op_info, device, **kwargs):
    # Error when self.dtype != src.dtype (and src is not a scalar)
    src = make_tensor((2, 5), device=device, dtype=torch.float32)
    idx = torch.tensor(((0, 1), (1, 2)), device=device, dtype=torch.long)
    dst = torch.zeros((3, 5), device=device, dtype=torch.double)
    yield ErrorInput(SampleInput(dst, args=(0, idx, src)),
                     error_regex="Expected self.dtype to be equal to src.dtype")

    # Index dtype must be long
    src = make_tensor((2, 5), device=device, dtype=torch.float32)
    idx = torch.tensor(((0, 1), (1, 2)), device=device, dtype=torch.int32)
    dst = torch.zeros((3, 5), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(dst, args=(0, idx, src)),
                     error_regex="Expected dtype int64 for index")

    # Index and destination must have the same number of dimensions
    src = make_tensor((2, 5), device=device, dtype=torch.float32)
    idx = torch.tensor(((0, 1), (1, 2)), device=device, dtype=torch.long)
    dst = torch.zeros((3, 5, 3), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(dst, args=(0, idx, src)),
                     error_regex="Index tensor must have the same number of dimensions as self tensor")

    # Index and src must have the same number of dimensions when src is not a scalar
    src = make_tensor((2, 5, 2), device=device, dtype=torch.float32)
    idx = torch.tensor(((34, 1), (1, 2)), device=device, dtype=torch.long)
    dst = torch.zeros((3, 5), device=device, dtype=torch.float32)
    yield ErrorInput(SampleInput(dst, args=(0, idx, src)),
                     error_regex="Index tensor must have the same number of dimensions as src tensor")

    # Index out of bounds
    # NOTE: this ErrorInput is guarded because bounds checking does not occur on CUDA devices
    if torch.device(device).type == 'cpu':
        src = make_tensor((2, 5), device=device, dtype=torch.float32)
        idx = torch.tensor(((34, 1), (1, 2)), device=device, dtype=torch.long)
        dst = torch.zeros((3, 5), device=device, dtype=torch.float32)
        yield ErrorInput(SampleInput(dst, args=(0, idx, src)),
                         error_regex="index 34 is out of bounds for dimension 0 with size 3")

def error_inputs_renorm(op_info, device, **kwargs):
    zero_d = torch.randn((), device=device)
    yield ErrorInput(SampleInput(zero_d, args=(0.5, 0, 1.0)), error_type=RuntimeError,
                     error_regex="needs at least 2 dimensions, got 0 dimensions")

def error_inputs_lstsq(op_info, device, **kwargs):
    zero_d = torch.randn((), device=device)
    yield ErrorInput(SampleInput(zero_d, args=(zero_d,)), error_type=RuntimeError,
                     error_regex="at least 2 dimensions")

def error_inputs_lstsq_grad_oriented(op_info, device, **kwargs):
    zero_d = torch.randn((), device=device)
    yield ErrorInput(SampleInput(zero_d, args=(zero_d, None)), error_type=RuntimeError,
                     error_regex="at least 2 dimensions")

def error_inputs_eig(op_info, device, **kwargs):
    zero_d = torch.randn((), device=device)

    yield ErrorInput(SampleInput(zero_d, args=(False,)), error_type=RuntimeError,
                     error_regex="input should be 2 dimensional")

    yield ErrorInput(SampleInput(zero_d, args=(True,)), error_type=RuntimeError,
                     error_regex="input should be 2 dimensional")

def error_inputs_ormqr(op_info, device, **kwargs):
    # this is only implemented on cpu
    if (torch.device(device).type == 'cpu'):
        zero_d = torch.randn((), device=device)
        yield ErrorInput(SampleInput(zero_d, args=(zero_d, zero_d)), error_type=RuntimeError,
                         error_regex="input must have at least 2 dimensions")

def error_inputs_diag(op_info, device, **kwargs):
    zero_d = torch.randn((), device=device)
    yield ErrorInput(SampleInput(zero_d, args=(0,)), error_type=RuntimeError,
                     error_regex="matrix or a vector expected")
    zero_d = torch.randn(1, 1, 1, device=device)
    yield ErrorInput(SampleInput(zero_d, args=(0,)), error_type=RuntimeError,
                     error_regex="matrix or a vector expected")

def error_inputs_embedding(op_info, device, **kwargs):
    indices = torch.rand(2, 2, device=device).long()
    weights = [
        torch.tensor(1.0, device=device),
        torch.tensor(1.0, device=device).reshape(1, 1, 1),
    ]

    for weight in weights:
        yield ErrorInput(SampleInput(weight, args=(indices,)), error_type=RuntimeError,
                         error_regex="'weight' must be 2-D")


def error_inputs_t(op_info, device, **kwargs):
    yield ErrorInput(
        SampleInput(torch.randn(2, 3, 4, 5, device=device)),
        error_regex="expects a tensor with <= 2",
    )


def error_inputs_multinomial(op_info, device, **kwargs):
    x = torch.empty(1, 2, 3, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(2,)), error_type=RuntimeError,
                     error_regex="prob_dist must be 1 or 2 dim")

    x = torch.empty(1, 2, dtype=torch.long, device=device)
    yield ErrorInput(SampleInput(x, args=(2,)), error_type=RuntimeError,
                     error_regex="multinomial only supports floating-point dtypes for input")

    x = torch.empty(1, 2, dtype=torch.double, device=device)
    y = torch.empty(1, 2, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(2,), kwargs=dict(out=y)), error_type=RuntimeError,
                     error_regex="multinomial expects Long tensor out")

    x = torch.empty(2, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(0,)), error_type=RuntimeError,
                     error_regex="cannot sample n_sample <= 0 samples")

    x = torch.empty(2, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(-1,)), error_type=RuntimeError,
                     error_regex="cannot sample n_sample <= 0 samples")

    x = torch.empty(2, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(3, False,)), error_type=RuntimeError,
                     error_regex="cannot sample n_sample > prob_dist")

    x = torch.empty(16777217, dtype=torch.double, device=device)
    yield ErrorInput(SampleInput(x, args=(3,)), error_type=RuntimeError,
                     error_regex="number of categories cannot exceed")

def error_inputs_gradient(op_info, device, **kwargs):
    for dtype in [torch.long, torch.float32, torch.complex64]:
        t = torch.tensor([[1, 2, 3], [4, 5, 6], [7, 8, 9]], device=device, dtype=dtype)

        dim = (1, 0)
        spacing = [0.1]
        yield ErrorInput(SampleInput(t, kwargs=dict(spacing=spacing, dim=dim, edge_order=1)),
                         error_type=RuntimeError,
                         error_regex='torch.gradient expected spacing to be unspecified, a scalar ')

        yield ErrorInput(SampleInput(t, kwargs=dict(edge_order=3)),
                         error_type=RuntimeError,
                         error_regex='torch.gradient only supports edge_order=1 and edge_order=2.')

        dim = (1, 1)
        spacing = 0.1
        yield ErrorInput(SampleInput(t, kwargs=dict(spacing=spacing, dim=dim, edge_order=1)),
                         error_type=RuntimeError,
                         error_regex='dim 1 appears multiple times in the list of dims')

        dim = (0, 1)
        coordinates = [torch.tensor([1, 2, 4], device='cpu'), torch.tensor([1, 2, 4], device='meta')]
        yield ErrorInput(SampleInput(t, kwargs=dict(spacing=coordinates, dim=dim, edge_order=1)),
                         error_type=RuntimeError,
                         error_regex='torch.gradient expected each tensor to be on the same device,')

        yield ErrorInput(SampleInput(t, kwargs=dict(dim=3)),
                         error_type=IndexError, error_regex='')

        t = torch.tensor([[1], [2], [3]])
        yield ErrorInput(SampleInput(t, kwargs=dict(edge_order=1)),
                         error_type=RuntimeError,
                         error_regex='torch.gradient expected each dimension size to be at least')

        t = torch.tensor([[1, 2], [3, 4]])
        yield ErrorInput(SampleInput(t, kwargs=dict(edge_order=2)),
                         error_type=RuntimeError,
                         error_regex='torch.gradient expected each dimension size to be at least')

def error_inputs_masked_select(op_info, device, **kwargs):
    x = torch.rand((1,), device=device).expand((3,))
    y = torch.rand((6,), device=device)
    mask = torch.tensor([True, False, True, True, False, False], device=device)

    yield ErrorInput(SampleInput(y, args=(mask,), kwargs=dict(out=x)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(y, args=(mask,), kwargs=dict(out=y)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

    yield ErrorInput(SampleInput(mask.clone(), args=(mask,), kwargs=dict(out=mask)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

def error_inputs_index_select(op_info, device, **kwargs):
    x = torch.rand((1, 6), device=device).expand((2, 6))
    y = torch.rand((3, 6), device=device)
    ind = torch.tensor([0, 1], dtype=torch.int64, device=device)

    yield ErrorInput(SampleInput(y, args=(1, ind,), kwargs=dict(out=x)),
                     error_type=RuntimeError,
                     error_regex='unsupported operation')

def error_inputs_logcumsumexp(op_info, device, **kwargs):
    dim = 3
    srcs = [torch.randn(5, 2, device=device), torch.randn(0, 2, device=device)]
    for src in srcs:
        yield ErrorInput(SampleInput(src, args=(dim,)),
                         error_type=IndexError,
                         error_regex='Dimension out of range')

def sample_inputs_take_along_dim(op_info, device, dtype, requires_grad, **kwargs):
    return (SampleInput(make_tensor((S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(gather_variable((S, S), 1, S, True, device=device), 0)),

            # `indices` broadcast
            SampleInput(make_tensor((S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(gather_variable((1, S // 2), 0, S, True, device=device), 1)),

            # `self` broadcast
            SampleInput(make_tensor((1, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(gather_variable((S, S // 2), 0, S, True, device=device), 1)),

            # without `dim` arg
            SampleInput(make_tensor((S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(gather_variable((S, S // 2), 0, S, True, device=device), )),
            SampleInput(make_tensor((S, S), dtype=dtype, device=device,
                                    low=None, high=None,
                                    requires_grad=requires_grad),
                        args=(gather_variable((S, S // 2), 0, S, True, device=device),)),
            )


def error_inputs_aminmax_amax_amin(op_info, device, **kwargs):

    # Error Inputs for zero-dim tensors, when 'dim' arg is not provided.
    shape = (S, 0, S)
    err_msg_amax_amin = "reduction"
    err_msg_aminmax = "cannot compute aminmax over an empty dimension as the operation has no identity"
    if op_info.name in ['amax', 'amin', '_refs.amax', '_refs.amin']:
        yield ErrorInput(SampleInput(torch.rand(shape, device=device)), error_regex=err_msg_amax_amin)
    elif op_info.name in ['aminmax']:
        yield ErrorInput(SampleInput(torch.rand(shape, device=device)), error_regex=err_msg_aminmax)

    # Error Inputs for tensors with more than 64 dimension
    sizes = [1] * 65
    err_msg1 = "only tensors with up to 64 dims are supported"
    yield ErrorInput(SampleInput(torch.randn(sizes, device=device), kwargs={'dim': -1}),
                     error_regex=err_msg1)
    yield ErrorInput(SampleInput(torch.randn(sizes, device=device), kwargs={'dim': 64}),
                     error_regex=err_msg1)

    # Error Inputs for repeated 'dim'
    if op_info.name in ['amax', 'amin', '_refs.amax', '_refs.amin']:
        dims = [(0, 0), (0, -4)]
        err_msg2 = "in the list of dims"
        x = torch.randn(S, S, S, S, device=device)
        for dim in dims:
            yield ErrorInput(SampleInput(x, kwargs={'dim': dim}), error_regex=err_msg2)

    # Error Input for illegal dtype
    input5 = torch.randn(L, L, dtype=torch.float32, device=device)
    max_values = torch.empty(L, dtype=torch.float32, device=device)
    min_values = torch.empty(L, dtype=torch.double, device=device)
    illegal_values = torch.empty(L, dtype=torch.int, device=device)

    err_msg_amax_amin2 = "Expected the dtype for input and out to match"
    err_msg_aminmax2 = "Expected out tensor to have dtype float, but got double instead"

    if op_info.name in ['amax', 'amin', '_refs.amax', '_refs.amin']:
        yield ErrorInput(SampleInput(input5, kwargs={'dim': 0, 'out': illegal_values}),
                         error_regex=err_msg_amax_amin2)
    elif op_info.name in ['aminmax']:
        yield ErrorInput(SampleInput(input5, kwargs={'dim': 0, 'out': (max_values, min_values)}),
                         error_regex=err_msg_aminmax2)

    # Error Inputs for functions to raise an error on specified zero'd dimension as reduction dim
    err_msg3 = "reduction"
    # FIXME: eager and ref impl throw different types of errors
    error_type = IndexError if 'refs' not in op_info.name else RuntimeError
    yield ErrorInput(SampleInput(torch.rand(shape, device=device), kwargs={'dim': 1}),
                     error_type=error_type, error_regex=err_msg3)

def sample_inputs_aminmax(op_info, device, dtype, requires_grad, **kwargs):
    test_cases: Tuple[tuple, dict] = (  # type: ignore[assignment]
        ((S, S, S), {}),
        ((S, S, S), {'dim': 1}),
        ((S, S, S), {'dim': 1, 'keepdim': True}),
        ((), {'dim': 0}),
        ((), {}),
        ((), {'dim': 0, 'keepdim': True}),
    )

    samples: List[SampleInput] = []
    for shape, kwargs in test_cases:
        samples.append(SampleInput(
            make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad),
            kwargs=kwargs))

    return samples

def sample_inputs_diff(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    test_cases = (
        ((1,), 0, None, None),
        ((S,), 0, None, None),
        ((S, 1), 0, None, None),
        ((S, 1), 1, None, None),
        ((S, S), 0, None, None),
        ((S, S), 1, None, None),
        ((S, S), 0, (1, S), (2, S)),
        ((S, S), 0, None, (2, S)),
        ((XS, XS, XS), 1, None, None),
        ((XS, XS, XS), 2, None, None),
        ((XS, XS, XS), 1, (XS, 1, XS), (XS, 1, XS)),
        ((XS, XS, XS), 2, (XS, XS, 1), (XS, XS, 1)),
        ((XS, XS, XS), 2, (XS, XS, XS), (XS, XS, XS)),)

    sample_inputs = []
    for size, dim, size_prepend, size_append in test_cases:
        prepend_size = 0 if (size_prepend is None) else size_prepend[dim]
        append_size = 0 if (size_append is None) else size_append[dim]
        dim_size = size[dim] + prepend_size + append_size
        for n in range(dim_size):
            input_tensor = make_arg(size)
            prepend = make_arg(size_prepend) if size_prepend else None
            append = make_arg(size_append) if size_append else None
            sample_inputs.append(SampleInput(input_tensor, args=(n, dim, prepend, append,)))

    # add some samples with n > dim_size
    sample_inputs.append(SampleInput(make_arg((XS, XS, XS)), args=(S + 1, 1,)))
    sample_inputs.append(SampleInput(make_arg((XS, XS, XS)), args=(S * 3 + 2, 2, make_arg((XS, XS, XS)), make_arg((XS, XS, XS)),)))

    return sample_inputs

def sample_inputs_histogram(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((), (S,), (S, S), (S, S, S), (S, 1, S), (S, 0, S))

    sample_inputs = []
    for size, bin_ct, weighted, density in product(sizes, range(1, 5), [False, True], [False, True]):
        input_tensor = make_arg(size)
        weight_tensor = make_arg(size) if weighted else None

        sample_inputs.append(SampleInput(input_tensor, args=(bin_ct,),
                                         kwargs=dict(weight=weight_tensor, density=density)))

        bins_tensor = make_arg((bin_ct + 1,))
        sample_inputs.append(SampleInput(input_tensor, args=(bins_tensor,),
                                         kwargs=dict(weight=weight_tensor, density=density)))

    return sample_inputs

def sample_inputs_histogramdd(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((S, S), (S, S, S), (S, 1, S), (S, 0, S))
    bin_ct_patterns = ((1, 1, 1, 1, 1), (2, 3, 2, 3, 2), (3, 2, 3, 2, 3))

    sample_inputs = []
    for size, bin_ct_pattern, weighted, density in product(sizes, bin_ct_patterns, [False, True], [False, True]):
        input_tensor = make_arg(size)
        bin_ct = bin_ct_pattern[:size[-1]]
        weight_tensor = make_arg(size[:-1]) if weighted else None

        sample_inputs.append(SampleInput(input_tensor, args=(bin_ct,),
                                         kwargs=dict(weight=weight_tensor, density=density)))

        bins_tensor = [make_arg(ct + 1) for ct in bin_ct]
        sample_inputs.append(SampleInput(input_tensor, args=(bins_tensor,),
                                         kwargs=dict(weight=weight_tensor, density=density)))

    return sample_inputs

def sample_inputs_histc(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((), (S,), (S, S), (S, S, S), (S, 1, S), (S, 0, S))

    sample_inputs = []
    for size, min, max in product(sizes, [0, -10], [0, 10]):
        # construct sample input omitting bins arg
        sample_inputs.append(SampleInput(make_arg(size),
                                         kwargs=dict(min=min, max=max)))

        # construct sample inputs with a few different bins values
        for bins in [1, 3, 10]:
            sample_inputs.append(SampleInput(make_arg(size),
                                             kwargs=dict(bins=bins, min=min, max=max)))

    return sample_inputs

def sample_inputs_bincount(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sample_inputs = []

    for size, weighted in product((S, M), [False, True]):
        input_tensor = torch.randint(0, size, (size,), dtype=dtype, device=device)
        weight_tensor = make_arg((size,)) if weighted else None

        max_val = int(input_tensor.max().item())

        for minlength in [0, max_val // 2, max_val, 2 * max_val]:
            sample_inputs.append(SampleInput(input_tensor,
                                             kwargs=dict(weights=weight_tensor, minlength=minlength)))

    return sample_inputs

def sample_inputs_bucketize(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((), (S,), (S, S), (S, S, S), (S, 1, S), (S, 0, S))

    sample_inputs = []

    for size, out_int32, right in product(sizes, [False, True], [False, True]):
        input_tensor = make_arg(size)
        boundaries = make_arg((S,)).msort()

        sample_inputs.append(SampleInput(input_tensor, args=(boundaries, ),
                                         kwargs=dict(out_int32=out_int32, right=right)))

    return sample_inputs

def sample_inputs_searchsorted(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((0,), (M,), (0, 0), (M, M), (0, 0, 0), (M, M, M))
    inputs = []
    for size, noncontiguous, out_int32, right in product(sizes, [False, True], [False, True], [False, True]):
        unsorted_tensor = make_arg(size, noncontiguous=noncontiguous)
        input_tensor = make_arg(size, noncontiguous=noncontiguous)
        if np.product(size) == 0:
            boundary_tensor = unsorted_tensor
            sorter = make_tensor(size, dtype=torch.int64, device=device, noncontiguous=noncontiguous)
        else:
            boundary_tensor, sorter = torch.sort(unsorted_tensor)
        side = "right" if right else "left"

        inputs.append(SampleInput(boundary_tensor, args=(input_tensor,), kwargs=dict(out_int32=out_int32, right=right)))
        inputs.append(SampleInput(boundary_tensor, args=(input_tensor,), kwargs=dict(out_int32=out_int32, side=side)))

        inputs.append(
            SampleInput(unsorted_tensor, args=(input_tensor,), kwargs=dict(out_int32=out_int32, right=right, sorter=sorter)))
        inputs.append(
            SampleInput(unsorted_tensor, args=(input_tensor,), kwargs=dict(out_int32=out_int32, side=side, sorter=sorter)))
    return inputs

def sample_inputs_gradient(op_info, device, dtype, requires_grad, **kwargs):
    sample_inputs = []
    test_cases_float = (
        ((S,), None, None, 1),
        ((S,), 2., None, 1),
        ((S, S), None, None, 2),
        ((S, S), [2.0, 2.1], None, 1),
        ((S, S), [2.0, 2.1], (0, 1), 1),
        ((4, 4, 4), [2., 1.], (0, 1), 2),
    )
    for size, spacing, dim, edge_order in test_cases_float:
        t = make_tensor(size, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        sample_inputs.append(SampleInput(t, kwargs=dict(dim=dim, spacing=spacing, edge_order=edge_order)))

    test_cases_tensor = (
        ((3, 3, 3), ((1.1, 2.0, 3.5), (4.0, 2, 6.0)), (0, -1), 1),
        ((3, 3, 3), ((1.0, 3.0, 2.0), (8.0, 6.0, 1.0)), (0, 1), 2),
    )
    for size, coordinates, dim, edge_order in test_cases_tensor:
        t = make_tensor(size, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        coordinates_tensor_list = []
        for coords in coordinates:
            # `coords` will always contain floating point values and Python 3.10 does not support this
            # implicit conversion to an integer using `__int__`
            # TODO: this can be simplified after https://github.com/pytorch/pytorch/issues/69316 is fixed
            a = torch.tensor(coords, device=device)
            coordinates_tensor_list.append(a.to(dtype))
        sample_inputs.append(SampleInput(t, kwargs=dict(dim=dim, spacing=coordinates_tensor_list, edge_order=edge_order)))

    return tuple(sample_inputs)

def sample_inputs_getitem(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    test_args = [
        ([1, 2],),
        (slice(0, 3),),
        ([slice(0, 3), 1],),
        ([[0, 2, 3], [1, 3, 3], [0, 0, 2]],),
        ([[0, 0, 3], [1, 1, 3], [0, 0, 2]],),
        ([slice(None), slice(None), [0, 3]],),
        ([slice(None), [0, 3], slice(None)],),
        ([[0, 3], slice(None), slice(None)],),
        ([[0, 3], [1, 2], slice(None)],),
        ([[0, 3], ],),
        ([[0, 3], slice(None)],),
        ([[0, 3], Ellipsis],),
        ([[0, 2, 3], [1, 3, 3], torch.LongTensor([0, 0, 2])],),
        (index_variable(2, S, device=device),),
        (mask_not_all_zeros((S,)),),
    ]

    for args in test_args:
        yield SampleInput(make_arg((S, S, S)), args=args)

    yield SampleInput(make_arg((S, S, S, S)), args=([slice(None), [0, 1], slice(None), [0, 1]],))

def sample_inputs_index_put(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    inputs = []
    for accumulate in [False, True]:
        # Test with indices arg
        inputs.append(SampleInput(
            make_arg((S, S,)),
            args=((index_variable(2, S, device=device),), make_arg((2, S))),
            kwargs=dict(accumulate=accumulate)))

        # Test with mask arg
        mask = torch.zeros(S, dtype=torch.bool) if accumulate else mask_not_all_zeros((S,))
        inputs.append(SampleInput(
            make_arg((S, S)),
            args=((mask, ), make_arg((S,))),
            kwargs=dict(accumulate=accumulate)))

    return inputs

def sample_inputs_sort(op_info, device, dtype, requires_grad, **kwargs):
    def small_3d_unique():
        res = torch.randperm(S * S * S, dtype=torch.int64, device=device).view(S, S, S)
        res = res.to(dtype).requires_grad_(requires_grad)
        return res

    def large_1d_unique():
        res = torch.randperm(L * L * L, dtype=torch.int64, device=device)
        res = res.to(dtype).requires_grad_(requires_grad)
        return res

    samples = []
    # Test case for large tensor.
    samples.append(SampleInput(large_1d_unique()))

    # Test cases for small 3d tensors.
    # Imitates legacy tests from test/test_torch.py
    dims = range(-3, 3)
    flag = [True, False]
    for dim, descending, stable in product(dims, flag, flag):
        # default schema without stable sort
        samples.append(SampleInput(small_3d_unique(),
                                   args=(dim, descending)))
        # schema with stable sort, no CUDA support yet
        if torch.device(device).type == 'cpu':
            samples.append(
                SampleInput(small_3d_unique(),
                            kwargs=dict(dim=dim, descending=descending, stable=stable))
            )

    # Test cases for scalar tensor
    samples.append(SampleInput(torch.tensor(1, dtype=dtype, device=device, requires_grad=requires_grad)))
    samples.append(SampleInput(torch.tensor(1, dtype=dtype, device=device, requires_grad=requires_grad),
                               args=(0,)))
    samples.append(SampleInput(torch.tensor(1, dtype=dtype, device=device, requires_grad=requires_grad),
                               args=(0, True)))

    # Test cases for stable sort
    samples.append(SampleInput(small_3d_unique(),
                   kwargs=dict(stable=True)))
    samples.append(SampleInput(small_3d_unique(),
                   kwargs=dict(dim=0, stable=True)))
    samples.append(SampleInput(small_3d_unique(),
                   kwargs=dict(dim=0, descending=True, stable=True)))
    return samples

def sample_inputs_threshold(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    sizes = ((), (S,), (S, S), (S, S, S))
    samples = []
    for x_size in sizes:
        # threshold and values args must be numbers
        samples.append(SampleInput(make_arg(x_size), args=(make_arg(()).item(), make_arg(()).item())))
    return samples

def sample_inputs_argsort(*args, **kwargs):
    return [sample_input for sample_input in sample_inputs_sort(*args, **kwargs) if "stable" not in sample_input.kwargs]

def sample_inputs_unique(op_info, device, dtype, requires_grad, **kwargs):
    sizes = ((), (S,), (S, S), (S, S, S), (S, 1, S), (S, 0, S))

    sample_inputs = []
    for shape, sorted, return_inverse, return_counts, dim in \
            product(sizes, [False, True], [False, True], [False, True], [None, -2, -1, 0, 1, 2]):
        # torch.unique cannot be called if the input tensor has a zero dimension which isn't the selected dim
        if 0 in shape and shape.index(0) is not dim:
            continue

        # skip invalid dim args
        if dim is not None and (dim < -len(shape) or dim >= len(shape)):
            continue

        kwargs = dict(sorted=sorted, return_inverse=return_inverse, return_counts=return_counts, dim=dim)

        # construct a test case with only one distinct value
        input_t = torch.zeros(shape, dtype=dtype, device=device, requires_grad=requires_grad)
        sample_inputs.append(SampleInput(input_t, kwargs=kwargs.copy()))

        # construct a test case with mixed 0s and 1s
        input_t = make_tensor(shape, dtype=torch.bool, device=device, requires_grad=False)\
            .to(dtype).requires_grad_(requires_grad)
        sample_inputs.append(SampleInput(input_t, kwargs=kwargs.copy()))

        # construct a test case with many different values
        input_t = make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)
        sample_inputs.append(SampleInput(input_t, kwargs=kwargs.copy()))

    return sample_inputs

def sample_inputs_unique_consecutive(*args, **kwargs):
    for sample_input in sample_inputs_unique(*args, **kwargs):
        if not sample_input.kwargs["sorted"]:
            sample_input.kwargs.pop("sorted")
            yield sample_input

def sample_inputs_adaptive_avg_pool1d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        ((0, 8, 8), (5,)),
        ((3, 8, 8), 5),
        ((3, 8, 8), 1)
    )

    for input_shape, output_size in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(output_size,))
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(output_size,))

def sample_inputs_adaptive_avg_pool2d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        ((1, 8, 8, 8), (5, 7)),
        ((2, 8, 8, 8), (None, 7)),
        ((1, 8, 4, 3), (5, None)),
        ((1, 8, 4, 3), (None, None)),
        ((1, 8, 4, 3), (5)),
    )

    for input_shape, output_size in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(output_size,))
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(output_size,))


def sample_inputs_adaptive_avg_pool3d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        ((0, 8, 8, 8, 8), (5, 7, 4)),
        ((1, 8, 4, 3, 7), (None, None, None)),
        ((1, 8, 4, 3, 7), (1, 1, 1)),
        ((3, 3, 8, 8, 6), (5, 7, None)),
        ((1, 3, 8, 8, 6), (5, None, 2)),
        ((3, 3, 8, 8, 6), (None, 3, 2)),
    )

    for input_shape, output_size in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(output_size,))
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(output_size,))

def sample_inputs_adaptive_max_pool1d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        # ((0, 8, 8), (5,)),
        # 0 batch size doesn't work,  cannot reshape tensor of 0 elements into shape [0, 8, -1]
        ((3, 4, 4), 3),
        ((3, 4, 4), 1)
    )

    for shapes, return_idx in product(cases, (True, False)):
        # Batched
        yield SampleInput(make_arg(shapes[0]), args=(shapes[1], return_idx))
        # Unbatched
        yield SampleInput(make_arg(shapes[0][1:]), args=(shapes[1], return_idx))

def sample_inputs_adaptive_max_pool2d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        # ((0, 8, 8, 8), (5, 7)),
        # 0 batch size doesn't work,  cannot reshape tensor of 0 elements into shape [0, 8, -1]
        ((1, 4, 4, 4), (2, 3)),
        ((2, 4, 4, 4), (None, 3)),
        ((2, 4, 4, 4), (1, 1)),
        ((1, 4, 4, 3), (3, None)),
        ((1, 4, 4, 3), (None, None)),
        ((1, 4, 4, 3), (3)),
    )

    for shapes, return_idx in product(cases, (True, False)):
        # Batched
        yield SampleInput(make_arg(shapes[0]), args=(shapes[1], return_idx))
        # Unbatched
        yield SampleInput(make_arg(shapes[0][1:]), args=(shapes[1], return_idx))


def sample_inputs_adaptive_max_pool3d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as (input shape, output size)
    cases = (
        # ((0, 8, 8, 8, 8), (5, 7, 4)),
        # 0 batch size doesn't work,  cannot reshape tensor of 0 elements into shape [0, 8, -1]
        ((1, 4, 4, 3, 5), (None, None, None)),
        ((1, 4, 4, 3, 5), (1, 1, 1)),
        ((3, 3, 4, 4, 6), (2, 3, None)),
        ((1, 3, 4, 4, 6), (3, None, 2)),
        ((3, 3, 4, 4, 6), (None, 3, 2)),
    )

    for shapes, return_idx in product(cases, (True, False)):
        # Batched
        yield SampleInput(make_arg(shapes[0]), args=(shapes[1], return_idx))
        # Unbatched
        yield SampleInput(make_arg(shapes[0][1:]), args=(shapes[1], return_idx))

class _TestParamsMaxPoolBase(object):

    def __init__(self):
        self.kwargs = {
            'kernel_size': [3],
            'stride': [2, None],
            'ceil_mode': [True, False],
            'padding': [0, 1],
            'dilation': [1],
            'return_indices': [True, False]
        }

        self.shapes = [
            [1, 2, None],  # batch
            [2],  # channels
            [3, 6]  # signal
        ]

    def _gen_shape(self):
        for shape in product(*self.shapes):
            # shape[0] is None indicates missing batch dimension
            if shape[0] is None:
                shape = shape[1:]

            yield shape, torch.contiguous_format
            # only 2d (N, C, H, W) rank 4 tensors support channels_last memory format
            if len(self.shapes) == 4 and len(shape) == 4:
                yield shape, torch.channels_last

    def _gen_kwargs(self):
        keys = self.kwargs.keys()
        for values in product(*self.kwargs.values()):
            yield dict(zip(keys, values))

    def gen_input_params(self):
        yield from product(self._gen_shape(), self._gen_kwargs())

class _TestParamsMaxPool1d(_TestParamsMaxPoolBase):

    def __init__(self):
        super().__init__()
        self.kwargs['kernel_size'] += [(3,)]
        self.kwargs['stride'] += [(2,)]
        self.kwargs['padding'] += [(1,)]
        self.kwargs['dilation'] += [(1,)]

class _TestParamsMaxPool2d(_TestParamsMaxPoolBase):

    def __init__(self):
        super().__init__()
        self.kwargs['kernel_size'] += [(3, 2)]
        self.kwargs['stride'] += [(2, 1)]
        self.kwargs['padding'] += [(1, 1)]
        self.kwargs['dilation'] += [(1, 2)]

        self.shapes.append([6])

class _TestParamsMaxPool3d(_TestParamsMaxPoolBase):

    def __init__(self):
        super().__init__()
        self.kwargs['kernel_size'] += [(3, 2, 3)]
        self.kwargs['stride'] += [(2, 1, 2)]
        self.kwargs['dilation'] += [(1, 2, 1)]

        self.shapes.append([6])
        self.shapes.append([5])

def sample_inputs_max_pool(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=False)

    params_generator_type_dict = {
        'nn.functional.max_pool1d': _TestParamsMaxPool1d,
        'nn.functional.max_pool2d': _TestParamsMaxPool2d,
        'nn.functional.max_pool3d': _TestParamsMaxPool3d,
    }

    params_generator = params_generator_type_dict[op_info.name]()
    for (shape, memory_format), kwargs in params_generator.gen_input_params():
        arg = make_arg(shape).to(memory_format=memory_format).requires_grad_(requires_grad)
        yield SampleInput(arg, kwargs=kwargs)

def sample_inputs_normalize(self, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, low=-1, high=1, device=device, dtype=dtype, requires_grad=requires_grad)

    cases: Tuple[Tuple[int], dict] = (  # type: ignore[assignment]
                                     ((2, 1, 4, 5), {'p': 1., 'dim': 2}),
                                     ((2, 3, 4, 5), {'p': 2., 'dim': 1}),
                                     ((1, 2, 4, 5), {'p': 0.5, 'dim': 0}),
                                     ((1, 3, 4, 5), {'p': -1., 'dim': 1}),
                                     ((1, 3, 4, 5), {'p': 0., 'dim': -1}),
                                     ((), {'p': 1.2, 'dim': 0}),
                                     ((2, 3, 4, 5), {}),
                                     ((2, 3, 4, 5), {'eps': 1e-4}))

    for input_shape, kwargs in cases:
        yield SampleInput(make_arg(input_shape), kwargs=kwargs)

def sample_inputs_conv_transpose1d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as shapes for input, weight, bias
    # and a dict of values of (stride, padding, output_padding, groups, dilation)
    cases: Tuple[Tuple[int], Tuple[int], Tuple[int], dict] = (  # type: ignore[assignment]
        ((1, 3, 4), (3, 3, 3), (3,),
         {'stride': (2,), 'padding': 2, 'output_padding': (1,), 'groups': 1}),
        ((2, 2, 4), (2, 2, 4), (4,),
         {'stride': (3,), 'padding': (1,), 'output_padding': (2,), 'groups': 2, 'dilation': (4,)}),
        ((1, 1, 4), (1, 1, 4), (1,),
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1, 'dilation': (2,)}),
        ((1, 1, 4), (1, 2, 3), None,
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1}),
        ((1, 4, 5), (4, 8, 3), None,
         {})
    )

    for input_shape, weight, bias, kwargs in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)


def sample_inputs_conv_transpose2d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as shapes for input, weight, bias
    # and a dict of values of (stride, padding, output_padding, groups, dilation)
    cases: Tuple[Tuple[int], Tuple[int], Tuple[int], dict] = (  # type: ignore[assignment]
        ((1, 3, 4, 4), (3, 3, 3, 3), (3,),
         {'stride': (2, 2), 'padding': 2, 'output_padding': (1, 1), 'groups': 1}),
        ((2, 2, 4, 4), (2, 2, 4, 5), (4,),
         {'stride': (3, 2), 'padding': (1, 2), 'output_padding': (2, 3), 'groups': 2, 'dilation': (4, 4)}),
        ((1, 1, 4, 5), (1, 1, 4, 3), (1,),
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1, 'dilation': (2, 3)}),
        ((1, 1, 4, 3), (1, 2, 3, 4), None,
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1}),
        ((1, 4, 5, 5), (4, 8, 3, 3), None,
         {})
    )

    for input_shape, weight, bias, kwargs in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)

def sample_inputs_conv_transpose3d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as shapes for input, weight, bias
    # and a dict of values of (stride, padding, output_padding, groups, dilation)
    cases: Tuple[Tuple[int], Tuple[int], Tuple[int], dict] = (  # type: ignore[assignment]
        ((1, 3, 4, 4, 4), (3, 3, 3, 3, 3), (3,),
         {'stride': (2, 2, 2), 'padding': 2, 'output_padding': (1, 1, 1), 'groups': 1}),
        ((2, 2, 4, 4, 4), (2, 2, 4, 5, 6), (4,),
         {'stride': (3, 2, 1), 'padding': (1, 2, 3), 'output_padding': (2, 3, 1), 'groups': 2, 'dilation': (4, 4, 4)}),
        ((1, 1, 4, 5, 2), (1, 1, 4, 3, 1), (1,),
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1, 'dilation': (2, 3, 2)}),
        ((1, 1, 4, 3, 4), (1, 2, 3, 4, 5), None,
         {'stride': 2, 'padding': 1, 'output_padding': 1, 'groups': 1}),
        ((1, 4, 5, 5, 5), (4, 8, 3, 3, 3), None,
         {})
    )

    for input_shape, weight, bias, kwargs in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)


def sample_inputs_conv1d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as shapes for input, weight, bias,
    # and a dict of values of (stride, padding, dilation, groups)
    cases: Tuple = (
        ((1, 3, 4), (3, 3, 3), (3,), {'stride': (2,), 'padding': 2, 'groups': 1}),
        ((2, 4, 8), (2, 2, 3), (2,), {'stride': 3, 'padding': 1, 'groups': 2, 'dilation': 2}),
        ((1, 4, 5), (1, 4, 3), None, {'stride': (2,), 'padding': 'valid'}),
        ((2, 2, 4), (2, 1, 4), (2,), {'stride': (1,), 'padding': 'same', 'groups': 2, 'dilation': (2,)}),
        # With defaults
        ((1, 4, 5), (3, 4, 3), None, {}),
    )

    # TODO: (@krshrimali), add error_inputs_func once https://github.com/pytorch/pytorch/pull/67354 is merged
    # Should replace test_conv_modules_raise_error_on_incorrect_input_size and test_conv_shapecheck
    # in test/test_nn.py

    for input_shape, weight, bias, kwargs in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)


def sample_inputs_conv2d(op_info, device, dtype, requires_grad, jit_fail_sample=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as shapes for input, weight, bias
    # and a dict of values of (stride, padding, groups, dilation)
    cases: Tuple = (
        ((1, 3, 4, 4), (3, 3, 3, 3), (3,),
            {'stride': (2, 2), 'padding': 2, 'groups': 1}),
        ((2, 4, 8, 8), (2, 2, 3, 3), (2,),
            {'stride': (3, 2), 'padding': (2, 1), 'groups': 2, 'dilation': (4, 4)}),
        ((1, 4, 5, 5), (1, 4, 2, 3), (1,),
            {'stride': 2, 'padding': 1, 'groups': 1, 'dilation': (2, 3)}),
        ((1, 4, 5, 5), (1, 4, 2, 3), (1,),
            {'stride': 2, 'padding': 1, 'groups': 1, 'dilation': (2, 3)}),
        ((1, 2, 4, 3), (4, 2, 3, 4), None,
            {'stride': 2, 'padding': 1, 'groups': 1}),
        ((1, 4, 5, 5), (1, 4, 2, 3), (1,),
            {'stride': 2, 'padding': "valid"}),
        ((1, 4, 5, 5), (1, 4, 2, 3), (1,),
            {'stride': 1, 'padding': "same", 'dilation': 3}),
        # Below are the group related samples from common_nn.py
        ((2, 4, 6, 6), (4, 1, 3, 3), (4,), {'groups': 4}),
        ((2, 4, 6, 6), (8, 1, 3, 3), (8,), {'groups': 4}),
        ((2, 4, 6, 6), (8, 1, 3, 3), None, {'groups': 4}),
        ((2, 4, 6, 6), (4, 1, 3, 3), (4,), {'groups': 4, 'stride': (3, 2)}),
        ((2, 4, 6, 6), (4, 1, 3, 3), (4,), {'groups': 4, 'padding': (1, 1)}),
        ((2, 4, 5, 5), (4, 1, 2, 2), (4,), {'groups': 4, 'dilation': (2, 2)}),
        ((2, 4, 6, 5), (6, 2, 3, 2), (6,), {'groups': 2}),
        # With defaults
        ((1, 4, 5, 5), (3, 4, 3, 3), None, {}),
    )

    for input_shape, weight, bias, kwargs in cases:
        # Batched
        yield SampleInput(make_arg(input_shape), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)
        # Unbatched
        yield SampleInput(make_arg(input_shape[1:]), args=(
            make_arg(weight),
            make_arg(bias) if bias is not None else bias
        ), kwargs=kwargs)


def sample_inputs_group_norm(opinfo, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as input shape, num groups, and eps
    cases: Tuple[Tuple[int], int, float] = (  # type: ignore[assignment]
        ((1, 6, 3), 2, 0.5),
        ((2, 6, 3), 2, -0.5),
        ((1, 2), 1, None),
        ((0, 2), 1, None),
    )

    for input_shape, num_groups, eps in cases:
        # Shape of weight and bias should be the same as num_channels
        weight = make_arg(input_shape[1])
        bias = make_arg(input_shape[1])
        kwargs = {'weight': weight, 'bias': bias} if eps is None else {'weight': weight, 'bias': bias, 'eps': eps}
        yield SampleInput(
            make_arg(input_shape),
            args=(num_groups,),
            kwargs=kwargs
        )
    # Without any optional args
    yield SampleInput(make_arg((1, 2)), args=(1,))


def sample_inputs_instance_norm(opinfo, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_arg_without_requires_grad = partial(make_tensor, device=device, dtype=dtype, requires_grad=False)

    # Ordered as: input shape, kwargs for momentum, eps
    cases: Tuple[Tuple[int], dict] = (  # type: ignore[assignment]
        ((S, S, S), {'momentum': 0.5, 'eps': 0.6}),
        ((S, S, S), {'momentum': 0.5, 'eps': 0.6, 'use_input_stats': True}),
        ((3, 2, 4), {'momentum': -1.2}),
        ((3, 2, 4), {'momentum': 0.0}),
        ((3, 2, 3, 4), {'momentum': -1.0, 'eps': 0.5}),
        ((3, 2, 3, 4), {'momentum': -1.0, 'eps': 0.5}),
    )

    for input_shape, kwargs in cases:
        # args: running mean, running var, weight and bias should necessarily be of shape: (channels,)
        channels = input_shape[1]
        weight = make_arg(channels)
        bias = make_arg(channels)
        running_mean = make_arg_without_requires_grad(channels, low=0)
        running_var = make_arg_without_requires_grad(channels, low=0)
        new_kwargs = {
            'running_mean': running_mean,
            'running_var': running_var,
            'weight': weight,
            'bias': bias,
            **kwargs
        }

        yield SampleInput(
            make_arg(input_shape),
            args=(),
            kwargs=new_kwargs
        )

    # Checking for permutations of weights and biases as `None`
    # instance_norm assumes that if there's a bias, there's a weight
    weights = [channels, None]
    biases = [None, None]

    for weight_channels, bias_channels in zip(weights, biases):
        running_mean = make_arg_without_requires_grad(channels, low=0)
        running_var = make_arg_without_requires_grad(channels, low=0)
        yield SampleInput(
            make_arg(input_shape),
            args=(),
            kwargs={
                'running_mean': running_mean,
                'running_var': running_var,
                'weight': make_arg(weight_channels) if weight_channels is not None else None,
                'bias': make_arg(bias_channels) if bias_channels is not None else None
            }
        )

    # Test case for no optional kwargs
    yield SampleInput(make_arg((1, 2, 3)), kwargs={})


def sample_inputs_layer_norm(opinfo, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as input shape, normalized_shape and a kwarg dict for eps
    cases: Tuple[Tuple[int], Tuple[int], dict] = (  # type: ignore[assignment]
        ((1, 2, 3), (1, 2, 3), {'eps': 0.5}),
        ((2, 2, 3), (2, 3), {'eps': -0.5}),
        ((1,), (1,), {}),
        ((1, 2), (2,), {}),
        ((0, 1), (1,), {}),
    )

    for input_shape, normalized_shape, kwargs in cases:
        # Shape of weight and bias should be the same as normalized_shape
        weight = make_arg(normalized_shape)
        bias = make_arg(normalized_shape)
        yield SampleInput(
            make_arg(input_shape),
            args=(normalized_shape, weight, bias),
            kwargs=kwargs
        )
    # Without any optional args
    yield SampleInput(make_arg((1, 2)), args=((2,),))

    # TODO: @krshrimali, once to_numpy method in SampleInput class is modified to take None inputs,
    # enable these inputs; see https://github.com/pytorch/pytorch/pull/63276#discussion_r691950400

    # With weight and a `None` bias
    # yield SampleInput(make_arg((1, 2)), args=((2,), make_arg((2,)), None))

    # With `None` weight and bias (tests failing for this, see the link above)
    # yield SampleInput(make_arg((1, 2)), args=((2,), None, make_arg((2,))))


def sample_inputs_native_layer_norm(opinfo, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as input shape, normalized_shape, eps
    cases: Tuple[Tuple[int], Tuple[int], float] = (  # type: ignore[assignment]
        ((1, 2, 3), (1, 2, 3), 0.5),
        ((2, 2, 3), (2, 3), -0.5),
        ((1,), (1,), 1e-5),
        ((1, 2), (2,), 1e-5),
        ((0, 1), (1,), 1e-5),
    )

    for input_shape, normalized_shape, eps in cases:
        # Shape of weight and bias should be the same as normalized_shape
        weight = make_arg(normalized_shape)
        bias = make_arg(normalized_shape)
        yield SampleInput(
            make_arg(input_shape),
            args=(normalized_shape, weight, bias, eps),
        )
        yield SampleInput(
            make_arg(input_shape),
            args=(normalized_shape, None, bias, eps),
        )
        yield SampleInput(
            make_arg(input_shape),
            args=(normalized_shape, weight, None, eps),
        )
        yield SampleInput(
            make_arg(input_shape),
            args=(normalized_shape, None, None, eps),
        )


def error_inputs_native_layer_norm(opinfo, device, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=torch.float32, requires_grad=False)
    input_shape = (1, 2, 3)

    err_msg1 = "Expected normalized_shape to be at least 1-dimensional"
    s1 = SampleInput(
        make_arg(input_shape), args=(tuple(), None, None, 1e-5)
    )
    yield ErrorInput(s1, error_regex=err_msg1)

    normalized_shape = (1, 2, 3)
    weight = make_arg((1, 2))
    err_msg2 = "Expected weight to be of same shape as normalized_shape"
    s2 = SampleInput(
        make_arg(input_shape), args=(normalized_shape, weight, None, 1e-5)
    )
    yield ErrorInput(s2, error_regex=err_msg2)

    bias = make_arg((1, 2))
    err_msg3 = "Expected bias to be of same shape as normalized_shape"
    s3 = SampleInput(
        make_arg(input_shape), args=(normalized_shape, None, bias, 1e-5)
    )
    yield ErrorInput(s3, error_regex=err_msg3)

    err_msg4 = "Given normalized_shape="
    s4 = SampleInput(
        make_arg((2, 2, 3)), args=((2, 2), None, None, 1e-5)
    )
    yield ErrorInput(s4, error_regex=err_msg4)


def sample_inputs_local_response_norm(opinfo, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Ordered as input shape, size and a kwarg dict for alpha, beta, and k
    cases: Tuple[Tuple[int], Tuple[int], dict] = (  # type: ignore[assignment]
        ((1, 6, 3), 2, {'alpha': 3e-05, 'beta': 0.5, 'k': 1.25}),
        ((1, 6, 3), 2, {'beta': 0.5, 'k': 1.25}),
        ((1, 6, 3), 2, {'alpha': 3e-05, 'k': 1.25}),
        ((1, 6, 3), 2, {'alpha': 3e-05, 'beta': 0.5}),
        ((1, 6, 3), 2, {'alpha': 3e-05}),
        ((1, 6, 3), 2, {'beta': 0.5}),
        ((1, 6, 3), 2, {'k': 1.25}),
        ((1, 6, 3), 2, {}),
        ((2, 6, 3), 2, {'alpha': 3e-05, 'beta': 0.5, 'k': 1.25}),
        ((1, 1, 2), 1, {'alpha': 3e-05, 'beta': 0.5, 'k': 1.25}),
        ((0, 1, 2), 1, {'alpha': 3e-05, 'beta': 0.5, 'k': 1.25}),
    )

    for input_shape, size, kwargs in cases:
        yield SampleInput(make_arg(input_shape), args=(size,), kwargs=kwargs)

def sample_inputs_hardswish(self, device, dtype, requires_grad, **kwargs):
    N = 5
    # make sure we are testing -3 -> 3 range. default is -10 -> 10 so maybe unnecessary ?
    tensors = [SampleInput(make_tensor((N * 2, N * 2), device=device, dtype=dtype,
               requires_grad=requires_grad, low=-5, high=5)) for _ in range(1, N)]
    return tensors

def sample_inputs_linear(self, device, dtype, requires_grad, **kwargs):
    features_options = [[3, 4], [8, 8]]
    batch_options: List[List[int]] = [
        [],  # no batch
        [0],
        [8],
        [2, 3],
    ]
    create_tensor = partial(make_tensor, device=device, dtype=dtype,
                            requires_grad=requires_grad, low=-2, high=2)

    sample_inputs = []
    for has_bias, (in_feat, out_feat), batch_shape in \
            itertools.product([True, False], features_options, batch_options):
        input_tensor = create_tensor(batch_shape + [in_feat])
        weight = create_tensor([out_feat, in_feat])
        if not has_bias:
            sample_inputs.append(SampleInput(input_tensor, args=(weight,)))
            continue

        bias = create_tensor([out_feat])
        sample_inputs.append(SampleInput(input_tensor, args=(weight, bias)))
    return sample_inputs

def sample_inputs_bilinear(self, device, dtype, requires_grad, **kwargs):
    features_options = [[3, 4, 5], [8, 8, 8]]
    batch_options: List[List[int]] = [
        [],  # no batch
        [0],
        [8],
        [2, 3],
    ]
    create_tensor = partial(make_tensor, device=device, dtype=dtype,
                            requires_grad=requires_grad, low=-2, high=2)

    sample_inputs = []
    for has_bias, (in_feat1, in_feat2, out_feat), batch_shape in \
            itertools.product([True, False], features_options, batch_options):
        input_tensor1 = create_tensor(batch_shape + [in_feat1])
        input_tensor2 = create_tensor(batch_shape + [in_feat2])
        weight = create_tensor([out_feat, in_feat1, in_feat2])
        if not has_bias:
            sample_inputs.append(SampleInput(input_tensor1, args=(input_tensor2, weight,)))
            continue
        bias = create_tensor([out_feat])
        sample_inputs.append(SampleInput(input_tensor1, args=(input_tensor2, weight, bias)))

    return sample_inputs

def sample_inputs_glu(self, device, dtype, requires_grad, **kwargs):
    features_options = [[2], [2, 4], [8, 8], [3, 6, 8], [1, 4, 6, 7]]
    batch_options: List[List[int]] = [
        [],  # no batch
        [0],
        [8],
        [2, 3],
    ]
    create_tensor = partial(make_tensor, device=device, dtype=dtype,
                            requires_grad=requires_grad, low=-2, high=2)

    sample_inputs = []
    for features, batch_shape in itertools.product(features_options, batch_options):
        ndim = len(features) + len(batch_shape)
        for dim in range(ndim):
            input_tensor = create_tensor(batch_shape + features)
            dim_size = input_tensor.size(dim)
            if dim_size > 0 and dim_size % 2 == 0:
                sample_inputs.append(SampleInput(input_tensor, args=(dim,)))

    return sample_inputs

def sample_inputs_interpolate(mode, self, device, dtype, requires_grad, **kwargs):
    N, C = 2, 3
    D = 4
    S = 3
    L = 5

    align_corners_options: Tuple[Any, ...] = (None,)
    if mode in ('linear', 'bilinear', 'bicubic', 'trilinear'):
        align_corners_options = (True, False, None)
    ranks_for_mode = {
        'nearest': [1, 2, 3],
        'linear': [1],
        'bilinear': [2],
        'bicubic': [2],
        'trilinear': [3],
        'area': [1, 2, 3]
    }

    def shape(size, rank, with_batch_channel=True):
        if with_batch_channel:
            return tuple([N, C] + ([size] * rank))
        return tuple([size] * rank)

    make_arg = partial(make_tensor, device=device, dtype=dtype,
                       requires_grad=requires_grad, low=-1, high=1)

    sample_inputs = []
    for align_corners in align_corners_options:
        for rank in ranks_for_mode[mode]:
            sample_inputs.extend([
                SampleInput(make_arg(shape(D, rank)),
                            args=(shape(S, rank, False), None, mode, align_corners)),
                SampleInput(make_arg(shape(D, rank)),
                            args=(shape(L, rank, False), None, mode, align_corners)),
                SampleInput(make_arg(shape(D, rank)),
                            args=(None, 1.7, mode, align_corners)),
                SampleInput(make_arg(shape(D, rank)),
                            args=(None, 0.6, mode, align_corners)),
            ])

    return sample_inputs

def sample_inputs_upsample(mode, self, device, dtype, requires_grad, **kwargs):
    N, C = 2, 3
    D = 4
    S = 3
    L = 5

    ranks_for_mode = {
        'nearest': [1, 2, 3],
        'bilinear': [2],
    }

    def shape(size, rank, with_batch_channel=True):
        if with_batch_channel:
            return tuple([N, C] + ([size] * rank))
        return tuple([size] * rank)

    make_arg = partial(make_tensor, device=device, dtype=dtype,
                       requires_grad=requires_grad, low=-1, high=1)

    sample_inputs = []
    for rank in ranks_for_mode[mode]:
        sample_inputs.extend([
            SampleInput(make_arg(shape(D, rank)),
                        kwargs=dict(size=shape(S, rank, False))),
            SampleInput(make_arg(shape(D, rank)),
                        kwargs=dict(size=shape(L, rank, False))),
            SampleInput(make_arg(shape(D, rank)),
                        kwargs=dict(scale_factor=1.7)),
            SampleInput(make_arg(shape(D, rank)),
                        kwargs=dict(scale_factor=0.6)),
        ])

    return sample_inputs


def sample_inputs_gelu(self, device, dtype, requires_grad, **kwargs):
    N = 5
    tensors = []
    for _ in range(1, N):
        for approximate in ['none', 'tanh']:
            tensors.append(SampleInput(
                make_tensor((N * 2, N * 2), device=device, dtype=dtype,
                            requires_grad=requires_grad, low=-3, high=3),
                kwargs=dict(approximate=approximate)))
    return tensors


def error_inputs_gelu(op, device, **kwargs):
    # Tests thtat gelu errors out when passed an approximation we don't know.
    yield ErrorInput(SampleInput(make_tensor((), dtype=torch.float, device=device), kwargs={"approximate": "asdf"}),
                     error_regex="approximate argument must be either")


def sample_inputs_max_min_reduction_with_dim(op_info, device, dtype, requires_grad, **kwargs):
    inputs = []
    args_for_reduction_with_dim = (
        ((S, S, S), (1,),),
        ((S, S, S), (1, True, ),),
        ((), (0,),),
        ((), (0, True,),),
    )
    inputs = list((SampleInput(make_tensor(input_tensor, dtype=dtype, device=device,
                                           low=None, high=None,
                                           requires_grad=requires_grad),
                               args=args,))
                  for input_tensor, args in args_for_reduction_with_dim)
    return inputs

def sample_inputs_max_min_reduction_no_dim(op_info, device, dtype, requires_grad, **kwargs):
    inputs = []
    inputs.append(SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                          low=None, high=None,
                                          requires_grad=requires_grad),))
    inputs.append(SampleInput(make_tensor((), dtype=dtype, device=device,
                                          low=None, high=None,
                                          requires_grad=requires_grad),))
    return inputs

def _generate_nan_reduction_inputs(device, dtype, requires_grad, **kwargs):
    yield from _generate_reduction_inputs(device, dtype, requires_grad)
    # NaN only exists for floating point numbers
    if dtype.is_complex or dtype.is_floating_point:
        yield torch.tensor([2, torch.nan, -1], device=device, dtype=dtype, requires_grad=requires_grad)
        yield torch.tensor([[torch.nan, 2], [0, 1]], device=device, dtype=dtype, requires_grad=requires_grad)

def sample_inputs_nan_reduction(supports_multiple_dims):
    # Generates sample inputs for reduction ops that contain the input tensor
    # and dim and keepdim kwargs. If a reduction op needs to test additional
    # args/kwargs then create a separate sample_inputs function
    def fn(op_info, device, dtype, requires_grad, **kwargs):
        inputs = []

        for t in _generate_nan_reduction_inputs(device, dtype, requires_grad):
            # Add case without dim and keepdim kwargs
            inputs.append(SampleInput(t.clone().requires_grad_(requires_grad)))
            for kwargs in _generate_reduction_kwargs(t.ndim, supports_multiple_dims):
                inputs.append(SampleInput(t.clone().requires_grad_(requires_grad),
                                          kwargs=kwargs))

        return inputs

    return fn

def sample_inputs_reduction_quantile(op_info, device, dtype, requires_grad, **kwargs):
    test_quantiles = (0.5, make_tensor((2,), dtype=dtype, device=device, low=0, high=1, requires_grad=requires_grad))
    test_interpolations = ['linear', 'midpoint']

    inputs = []
    for quantiles in test_quantiles:
        for t in _generate_reduction_inputs(device, dtype, requires_grad):
            # Add case without dim and keepdim kwargs
            inputs.append(SampleInput(t.clone().requires_grad_(requires_grad),
                                      args=(quantiles,)))
            for kwargs in _generate_reduction_kwargs(t.ndim, supports_multiple_dims=False):
                # Interpolation kwarg for now is only supported when providing both dim and keepdim
                kwargs.setdefault('dim', 0)
                kwargs.setdefault('keepdim', False)
                for interpolation in test_interpolations:
                    kwargs['interpolation'] = interpolation
                    inputs.append(SampleInput(t.clone().requires_grad_(requires_grad),
                                              args=(quantiles,), kwargs=kwargs))

    return inputs

def sample_inputs_reduction_count_nonzero(*args, **kwargs):
    """Sample inputs for count_nonzero"""
    # count_nonzero does not support keepdim yet
    for sample in sample_inputs_reduction(*args, **kwargs):
        sample.kwargs.pop('keepdim', None)
        yield sample

def sample_inputs_leaky_relu(op_info, device, dtype, requires_grad, **kwargs):
    N = 10
    tensors = [SampleInput(make_tensor((N, N), device=device, dtype=dtype,
               requires_grad=requires_grad)) for _ in range(1, N)]
    return tensors

def sample_inputs_fractional_max_pool2d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Order: input_shape, kernel_size
    cases = (((1, 3, 9, 9), 3),
             ((1, 3, 9, 9), (4, 4)),
             ((1, 3, 9, 9), (6, 6)),
             ((2, 3, 9, 9), (3, 3)),
             ((1, 1, 4, 4), (2, 2)),
             ((1, 2, 6, 6), (4, 4)))

    samples = []

    for input_shape, kernel_size in cases:
        for return_indices in [False, True]:
            # test case passing a single output size
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_size=(2), return_indices=return_indices)
            ))

            # test case passing a tuple output size
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_size=(2, 3), return_indices=return_indices)
            ))

            # test case passing an output ratio
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_ratio=(0.5, 0.5), return_indices=return_indices)
            ))

    return samples

def sample_inputs_fractional_max_pool3d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Order: input_shape, kernel_size
    cases = (((2, 3, 5, 5, 5), (2, 2, 2)),
             ((1, 2, 6, 5, 4), 2),
             ((1, 2, 5, 6, 5), (2, 3, 2)),
             ((1, 2, 6, 6, 6), (2, 3, 2)),
             ((1, 1, 7, 6, 7), (2, 3, 4)),
             ((1, 1, 4, 5, 4), (2, 2, 1)),
             ((1, 1, 8, 7, 6), (4, 3, 2)),
             ((0, 1, 4, 5, 4), (2, 2, 1)))

    samples = []

    for input_shape, kernel_size in cases:
        for return_indices in [False, True]:
            # test case passing a single output size
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_size=(2), return_indices=return_indices)
            ))

            # test case passing a tuple output size
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_size=(2, 3, 2), return_indices=return_indices)
            ))

            # test case passing an output ratio
            samples.append(SampleInput(
                make_arg(input_shape),
                args=(kernel_size,),
                kwargs=dict(output_ratio=(0.5, 0.5, 0.5), return_indices=return_indices)
            ))

    return samples

def sample_inputs_avgpool2d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Order: input_shape, kernel_size, stride, padding, ceil_mode, count_include_pad, divisor_override
    cases = (((1, 3, 9, 9), 3, 1, 1, True, False, 2),
             ((1, 3, 9, 9), (4, 4), (2, 3), 1, True, False, 2),
             ((1, 3, 9, 9), (6, 6), (3, 3), (2, 3), True, True, 2),
             ((2, 3, 9, 9), (3, 3), (1, 1), (1, ), True, False, 2),
             ((1, 1, 4, 4), (2, 2), (), (0, ), False, True, -2),
             ((1, 2, 6, 6), (4, 4), (2, 2), (2, ), True, True, None))

    for input_shape, kernel_size, stride, padding, ceil_mode, count_include_pad, divisor_override in cases:
        yield SampleInput(make_arg(input_shape),
                          args=(kernel_size, stride, padding, ceil_mode, count_include_pad, divisor_override))
    # Case with just input_shape and kernel_size
    yield SampleInput(make_arg((1, 3, 9, 9)), args=((3, 3)))

def sample_inputs_avgpool1d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Order: input_shape, kernel_size, kwargs
    cases: List[Tuple[Tuple[int, ...], Union[int, Tuple[int, ...]], Dict]] = [
        ((2, 3, 9), (3,), dict()),
        ((1, 3, 9), 3, dict(stride=1, padding=1, ceil_mode=True, count_include_pad=False)),
        ((1, 3, 9), (6,), dict(stride=(3,), padding=(2,), ceil_mode=True, count_include_pad=True)),
        ((2, 3, 9), (3,), dict(stride=(1,), padding=(1,), ceil_mode=False, count_include_pad=True)),
        ((0, 3, 9), (6,), dict(stride=(3,), padding=(2,), ceil_mode=False, count_include_pad=True)),
        ((1, 2, 9), (7,), dict(stride=(3,), padding=(2,), ceil_mode=False)),
        ((1, 2, 9), (7,), dict(stride=(3,), padding=(3,), ceil_mode=True)),
        ((1, 2, 9), (7,), dict(stride=(3,), ceil_mode=False)),
        ((1, 2, 9), (7,), dict(stride=(3,), ceil_mode=True)),
    ]

    for input_shape, kernel_size, kwargs in cases:
        yield SampleInput(make_arg(input_shape), args=(kernel_size,), kwargs=kwargs)

def sample_inputs_avgpool3d(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Order: input_shape, kernel_size, stride, padding, ceil_mode, count_include_pad, divisor_override
    cases: List[Tuple[Tuple[int, ...], Union[int, Tuple[int, ...]], Dict]] = [
        ((2, 3, 3, 4, 4), (2, 2, 2), dict()),
        ((1, 2, 4, 4, 4), 2, dict(stride=1, padding=1, ceil_mode=True,
                                  count_include_pad=False, divisor_override=2)),
        ((1, 2, 5, 5, 5), (2, 3, 4), dict(stride=(1, 2, 2), padding=(0, 1, 2), ceil_mode=True,
                                          count_include_pad=True, divisor_override=2)),
        ((1, 2, 5, 5, 5), (2, 3, 4), dict(stride=(1, 2, 2), padding=(0, 1, 2), ceil_mode=False)),
        ((1, 1, 7, 5, 7), (6, 3, 4), dict(stride=(2, 3, 2), padding=(3, 1, 0), ceil_mode=False,
                                          count_include_pad=False, divisor_override=2)),
        ((1, 1, 4, 5, 4), (2, 2, 3), dict(stride=(2, 2, 1), padding=0, ceil_mode=False,
                                          count_include_pad=True, divisor_override=-2)),
        ((1, 1, 6, 5, 6), (4, 5, 6), dict(stride=(2, 3, 2), padding=2, ceil_mode=True,
                                          count_include_pad=True, divisor_override=None)),
        ((0, 1, 4, 5, 4), (2, 3, 1), dict(stride=(2, 1, 2), padding=0, ceil_mode=False,
                                          count_include_pad=True, divisor_override=None)),
    ]

    for input_shape, kernel_size, kwargs in cases:
        yield SampleInput(make_arg(input_shape), args=(kernel_size,), kwargs=kwargs)

def sample_inputs_topk(op_info, device, dtype, requires_grad, **kwargs):
    def get_tensor_input(size):
        return make_tensor(size, dtype=dtype, device=device, requires_grad=requires_grad)

    inputs = []
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3,)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, 1)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, -2)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, 1, True)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, -2, True)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, 1, True, True)))
    inputs.append(SampleInput(get_tensor_input((S, M, S)), args=(3, -2, True, True)))

    inputs.append(SampleInput(get_tensor_input(()), args=(1,)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, 0)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, -1)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, 0, True)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, -1, True)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, 0, True, True)))
    inputs.append(SampleInput(get_tensor_input(()), args=(1, -1, True, True)))

    return inputs

def sample_inputs_outer(op_info, device, dtype, requires_grad, **kwargs):
    inputs = []
    arg_a = make_tensor((S,), dtype=dtype, device=device, requires_grad=requires_grad)
    arg_b = make_tensor((M,), dtype=dtype, device=device, requires_grad=requires_grad)
    inputs.append(SampleInput(arg_a, args=(arg_b,)))
    return inputs

def sample_inputs_dist(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    sizes = ((S, S, S), (S,), (S, 1, S), (), (S, S))
    ps = (2, 4)

    for size_x, size_y, p in product(sizes, sizes, ps):
        yield SampleInput(make_arg(size_x), args=(make_arg(size_y), p))

# Missing to test the nondeterminism of the operation
# https://github.com/pytorch/pytorch/issues/53352
def sample_inputs_index(op_info, device, dtype, requires_grad, **kwargs):
    # target.index_select(dim, idx)
    select = op_info.name == "index_select"
    # target.index_add(dim, idx, source, *, alpha=1)
    add = op_info.name == "index_add"
    # target.index_copy(dim, idx, source)
    copy = op_info.name == "index_copy"
    # target.index_fill(dim, idx, value)
    fill = op_info.name == "index_fill"


    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_permutation = partial(torch.randperm, device=device, dtype=torch.int64)

    def make_idx(n):
        return make_tensor((n,), device=device, dtype=torch.int64, low=0, high=n)

    shapes = [(), (1,), (S, S)]
    # extra parameter for add
    alphas = (-1, 0, 2) if add else (None,)

    for shape, alpha in product(shapes, alphas):
        t = make_arg(shape)
        args = []

        # dim. We handle the scalar case
        dim = 1 if t.ndim == 2 else 0
        args.append(dim)

        # idx They need to be different for copy and add to be deterministic
        make_idx_fn = make_permutation if copy or add else make_idx
        idx = make_idx_fn(t.shape[dim] if t.ndim != 0 else 1)
        args.append(idx)

        # source
        if copy or add:
            args.append(make_arg(shape))
        elif fill:
            # A weird number to catch errors
            args.append(make_arg((1,)).item())

        args = tuple(args)
        kwargs = {} if alpha is None else {"alpha": alpha}

        yield SampleInput(t, args=args, kwargs=kwargs)

def sample_inputs_index_reduce(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_idx(n, m):
        return make_tensor((n,), device=device, dtype=torch.int64, low=0, high=m)

    shapes = [((), ()), ((1,), (1,)), ((S, S), (S, M)), ((S, S, S), (S, M, S))]
    include_selfs = (True, False)
    reduces = ('prod', 'mean', 'amin', 'amax')

    for shape, include_self, reduce in product(shapes, include_selfs, reduces):
        self_shape, src_shape = shape
        # dim. We handle the scalar case
        dim = 1 if len(self_shape) >= 2 else 0
        idx = make_idx(src_shape[dim] if len(src_shape) != 0 else 1,
                       self_shape[dim] if len(self_shape) != 0 else 1)
        args = (dim, idx, make_arg(src_shape), reduce)
        yield SampleInput(make_arg(self_shape),
                          args=args,
                          kwargs={'include_self' : include_self})

    # Sample inputs to test edge cases for backward
    if requires_grad:
        # Check that gradients are propagated correctly for prod when zeros in self/src are reduced
        # This sample tests gradients for the following cases
        # (a) 1 zero reduced (from source (self[0, 1]), from self (self[0, 0]))
        # (b) 2 zeros reduced (1 from src and 1 from self (self[1, 0], self[1, 1])
        # (c) no zeros reduced (self[2, 1], self[2, 2])
        # (d) 2 zeros reduced (both from src) is tested in test/test_autograd.py
        #     test_scatter_index_reduce_prod_gradgrad_error as this case is not supported for gradgrad
        input = torch.tensor([[0, 13], [0, 0], [15, 19]], dtype=dtype, device=device, requires_grad=requires_grad)
        src = torch.tensor([[2, 0], [0, 0], [2, 3], [2, 2]], dtype=dtype, device=device, requires_grad=requires_grad)
        idx = torch.tensor([0, 1, 2, 0], dtype=torch.long, device=device)

        yield SampleInput(input,
                          args=(0, idx, src, 'prod'),
                          kwargs={'include_self': True})

def sample_inputs_mode(op_info, device, dtype, requires_grad, **kwargs):
    inputs = []
    args = (
        ((S, S, S), (),),
        ((S, S, S), (1, ),),
        ((S, S, S), (1, True, ),),
        ((), (),),
        ((), (0,),),
        ((), (0, True,),),
        # Non-fused mode kernel on CUDA
        ((3000,), ()),
    )
    inputs = list((SampleInput(make_tensor(input_tensor, dtype=dtype, device=device,
                                           low=None, high=None,
                                           requires_grad=requires_grad),
                               args=args,))
                  for input_tensor, args in args)
    return inputs

# Missing to test the nondeterminism of the operation
# https://github.com/pytorch/pytorch/issues/53352
def sample_inputs_put(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    make_idx = partial(make_tensor, low=0, dtype=torch.int64, device=device, requires_grad=False)

    S = 3

    # Generic inputs
    idx = torch.randperm(S * S, device=device, dtype=torch.int64)[:S]
    idx_list = [idx, -idx - 1]
    for idx, acc in product(idx_list, (True, False)):
        yield SampleInput(input=make_arg((S, S)),
                          args=(idx.clone(),
                                make_arg((S,)),
                                acc))

    # Scalar cases
    scalar_sizes = [(), (1,)]
    tgt_gen = (make_arg(size) for size in scalar_sizes)
    idx_gen = (make_idx(size, high=1) for size in scalar_sizes)
    src_gen = (make_arg(size) for size in scalar_sizes)
    for tgt, idx, src, acc in product(tgt_gen, idx_gen, src_gen, (True, False)):
        yield SampleInput(input=tgt.clone().requires_grad_(requires_grad),
                          args=(idx.clone(),
                                src.clone().requires_grad_(requires_grad),
                                acc))

    # Empty cases
    tgt_sizes = [(0,), (), (1,), (3, 2)]
    tgt_gen = (make_arg(size) for size in tgt_sizes)
    idx = make_idx((0,), high=1)
    src = make_arg((0,))
    for tgt, acc in product(tgt, (True, False)):
        yield SampleInput(input=tgt.clone().requires_grad_(requires_grad),
                          args=(idx.clone(),
                                src.clone().requires_grad_(requires_grad),
                                acc))

def sample_inputs_take(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    make_idx = partial(make_tensor, low=0, dtype=torch.int64, device=device, requires_grad=False)

    S = 3

    # Generic inputs: take S elements out of S * S
    index = make_idx((S,), high=(S * S))
    for idx in (index, -index - 1):
        yield SampleInput(input=make_arg((S, S)), args=(idx,))

    # Scalar cases
    scalar_sizes = [(), (1,)]
    src_gen = (make_arg(size) for size in scalar_sizes)
    idx_gen = (make_idx(size, high=1) for size in scalar_sizes)
    for src, idx in product(src_gen, idx_gen):
        yield SampleInput(input=src.clone().requires_grad_(requires_grad),
                          args=(idx.clone(),))

    # Empty cases
    src_sizes = [(0,), (), (1,), (3, 2)]
    src_gen = (make_arg(size) for size in src_sizes)

    idx = make_idx((0,), high=1)
    for src in src_gen:
        yield SampleInput(input=src.clone().requires_grad_(requires_grad),
                          args=(idx.clone(),))

def sample_movedim_moveaxis(op_info, device, dtype, requires_grad, **kwargs):
    return (
        SampleInput(
            make_tensor((4, 3, 2, 1), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=([0, 1, 2, 3], [3, 2, 1, 0])),
        SampleInput(
            make_tensor((4, 3, 2, 1), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
            args=([0, -1, -2, -3], [-3, -2, -1, -0]))
    )


def sample_repeat_tile(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    rep_dims = ((), (0, ), (1, ), (0, 2), (1, 1), (2, 3), (2, 3, 2), (0, 2, 3), (2, 1, 1, 1),)
    shapes = ((), (0,), (2,), (3, 0), (3, 2), (3, 0, 1))

    if requires_grad:
        # Tests for variant_consistency_jit, grad, gradgrad
        # are slower. Use smaller bags of `rep_dims` and `shapes`
        # in this case.
        rep_dims = ((), (0, ), (0, 2), (1, 1), (2, 3), (1, 3, 2), (3, 1, 1))  # type: ignore[assignment]
        shapes = ((), (0,), (2,), (3, 2))  # type: ignore[assignment]

    samples = []
    for rep_dim, shape in product(rep_dims, shapes):
        # `torch.repeat` errors for `len(rep_dims) < t.dim()`,
        # so we filter such combinations.
        if op_info.name == 'repeat' and len(rep_dim) < len(shape):
            continue
        samples.append(SampleInput(make_arg(shape), args=(rep_dim,),))

    return samples


def sample_inputs_narrow(op_info, device, dtype, requires_grad, **kwargs):
    shapes_and_args = (
        ((S, S, S), (1, 2, 2)),
        ((S, S, S), (-1, 2, 2)),
        ((S, S, S), (1, 0, 0)),
        ((S, S, S), (-1, 0, 0)),
        ((S, S, S), (2, 1, 2)),
    )

    for shape, args in shapes_and_args:
        tensor = make_tensor(shape, dtype=dtype, device=device, low=None, high=None,
                             requires_grad=requires_grad)
        yield SampleInput(tensor, args=args)

def sample_trapezoid(op_info, device, dtype, requires_grad, **kwargs):
    y_shape_x_shape_and_kwargs = [
        ((2, 3), (2, 3), {}),
        ((2, 3), (2, 3), {'dim': 1}),
        ((6,), (6,), {}),
        ((6,), None, {}),
        # When 'trapezoid' is called with an empty input, it does not produce an output with requires_grad
        # See Issue #{61619}
        # ((6,0), (6,0), {}),
        ((2, 3), (1, 3), {}),
        ((3, 3), (3, 3), {}),
        ((3, 3), (3, 3), {'dim': -2}),
        ((5,), None, {'dx': 2.0}),
        ((2, 2), None, {'dx': 3.0})
    ]
    samples = []
    for y_shape, x_shape, kwarg in y_shape_x_shape_and_kwargs:
        y_tensor = make_tensor(y_shape, dtype=dtype, device=device, low=None, high=None,
                               requires_grad=requires_grad)
        if x_shape is not None:
            x_tensor = make_tensor(x_shape, dtype=dtype, device=device, low=None, high=None,
                                   requires_grad=requires_grad)
            samples.append(SampleInput(y_tensor, args=(x_tensor,), kwargs=kwarg))
        else:
            samples.append(SampleInput(y_tensor, kwargs=kwarg))
    return samples

def sample_cumulative_trapezoid(op_info, device, dtype, requires_grad, **kwargs):

    y_shape_x_shape_and_kwargs = [
        ((2, 3), (2, 3), {}),
        ((2, 3), (2, 3), {'dim': 1}),
        ((6,), (6,), {}),
        ((6,), None, {}),
        # When 'cumulative_trapezoid' is called with an empty input, it does not produce an output with requires_grad
        # See Issue #{61619}
        # ((6,0), (6,0), {}),
        ((2, 3), (1, 3), {}),
        ((3, 3), (3, 3), {}),
        ((3, 3), (3, 3), {'dim': -2}),
        ((5,), None, {'dx': 2.0}),
        ((2, 2), None, {'dx': 3.0})
    ]
    samples = []
    for y_shape, x_shape, kwarg in y_shape_x_shape_and_kwargs:
        y_tensor = make_tensor(y_shape, dtype=dtype, device=device, low=None, high=None,
                               requires_grad=requires_grad)
        if x_shape is not None:
            x_tensor = make_tensor(x_shape, dtype=dtype, device=device, low=None, high=None,
                                   requires_grad=requires_grad)
            samples.append(SampleInput(y_tensor, args=(x_tensor,), kwargs=kwarg))
        else:
            samples.append(SampleInput(y_tensor, kwargs=kwarg))
    return samples

def sample_unsqueeze(op_info, device, dtype, requires_grad, **kwargs):
    shapes_and_axes = [
        ((3, 4, 5), 0),
        ((3, 4, 5), 1),
        ((3, 4, 5), 3),
        ((3, 4, 5), -1),
        ((3, 4, 5), -3),
        ((), 0),
        ((), -1),
        ((1,), 0),
        ((1,), -1),
    ]

    samples = []
    for shape, axis in shapes_and_axes:
        tensor = make_tensor(shape, dtype=dtype, device=device, low=None, high=None,
                             requires_grad=requires_grad)
        samples.append(SampleInput(tensor, args=(axis,),))

    return samples


def sample_inputs_nn_unfold(op_info, device, dtype, requires_grad, **kwargs):
    shapes = ((0, 1, 5, 5), (1, 1, 5, 5), (2, 3, 5, 5))
    kernel_sizes = (2, (2, 2), (3, 3))
    dilations = (1, 2, (1, 2))
    paddings = (0, 1, (1, 1))
    strides = (1, 2, (1, 2))

    cases = product(shapes, kernel_sizes, dilations, paddings, strides)
    for shape, kernel_size, dilation, padding, stride in cases:
        tensor = make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)
        yield SampleInput(tensor, args=(kernel_size, dilation, padding, stride))

    # With default args
    yield SampleInput(make_tensor((1, 1, 5, 5), dtype=dtype, device=device, requires_grad=requires_grad),
                      args=((3, 3),))


def sample_inputs_squeeze(op_info, device, dtype, requires_grad, **kwargs):
    shapes_and_args = (
        ((S, 1, S, 1), ()),
        ((1, 1, 1, 1), ()),
        ((S, 1, S, 1), (1,)),
        ((S, 1, S, 1), (-1,)),
        ((S, 1, S, 1), (2,)),
        ((S, 1, S, 1), (-2,)),
        ((), (0, )),
    )

    for shape, args in shapes_and_args:
        tensor = make_tensor(shape, dtype=dtype, device=device, low=None, high=None,
                             requires_grad=requires_grad)

        yield SampleInput(tensor, args=args)


def sample_inputs_nn_pad(op_info, device, dtype, requires_grad, mode, **kwargs):
    assert mode in ('constant', 'reflect', 'replicate', 'circular')
    if mode in ['reflect', 'replicate']:
        cases: tuple = (  # ignore
            ((1, 3), (1, 2)),
            ((1, 3), (0, 1)),
            ((0, 3, 3), (1, 2)),
            ((0, 3, 3), (0, 1)),
            ((1, 3, 3), (1, 2)),
            ((1, 3, 3), (0, 1)),
            ((1, 3, 3), (0, 2, 0, 1)),
            ((0, 3, 3, 3), (0, 2, 0, 1)),
            ((3, 3, 5, 5), (0, 2, 0, 1)),
            ((3, 3, 5, 5), (1, 1, 1, 1, 1, 1)),
            ((1, 3, 3, 3, 3), (1, 1, 1, 1, 1, 1)),
            ((1, 3, 4, 4), (-1, 1, -2, 1)),
        )
    elif mode == 'constant':
        cases = (
            ((1, 3), (1, 2)),
            ((1, 3), (0, 1)),
            ((1, 3), (0, 2, 0, 1)),
            ((0, 3, 3), (1, 2)),
            ((0, 3, 3), (0, 1)),
            ((0, 3, 3), (0, 2, 0, 1)),
            ((0, 3, 3), (1, 1, 1, 1, 1, 1)),
            ((1, 3, 3), (1, 2)),
            ((1, 3, 3), (0, 1)),
            ((1, 3, 3), (0, 2, 0, 1)),
            ((1, 3, 3), (1, 1, 1, 1, 1, 1)),
            ((0, 3, 3, 3), (1, 2)),
            ((0, 3, 3, 3), (0, 1)),
            ((0, 3, 3, 3), (0, 2, 0, 1)),
            ((0, 3, 3, 3), (1, 1, 1, 1, 1, 1)),
            ((3, 3, 5, 5), (1, 2)),
            ((3, 3, 5, 5), (0, 1)),
            ((3, 3, 5, 5), (0, 2, 0, 1)),
            ((3, 3, 5, 5), (1, 1, 1, 1, 1, 1)),
            ((1, 3, 3, 3, 3), (1, 2)),
            ((1, 3, 3, 3, 3), (0, 1)),
            ((1, 3, 3, 3, 3), (0, 2, 0, 1)),
            ((1, 3, 3, 3, 3), (1, 1, 1, 1, 1, 1)),
            ((1, 3, 4, 4), (-1, 1, -2, 1)),
        )
    else:  # mode == 'circular'
        if dtype == torch.bool:
            # test_dtypes fails on ASAN with for the case ab
            # runtime error: load of value 190, which is not a valid value for type 'bool'
            # Reference: https://github.com/pytorch/pytorch/pull/62814#issuecomment-894156562
            # Reference Issue: https://github.com/pytorch/pytorch/issues/63034
            cases = (
                ((2, 3, 3), (1, 2)),
                ((1, 3, 3), (1, 2)),
            )
        else:
            cases = (
                ((0, 3, 3), (1, 2)),
                ((0, 3, 3), (0, 1)),
                ((1, 3, 3), (1, 2)),
                ((1, 3, 3), (0, 1)),
                ((0, 3, 3, 3), (0, 2, 0, 1)),
                ((3, 3, 5, 5), (0, 2, 0, 1)),
                ((1, 3, 3, 3, 3), (1, 1, 1, 1, 1, 1)),
                ((1, 3, 4, 4), (-1, 1, -2, 1)),
            )

    make_inp = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    if mode == 'constant':
        # Default args
        yield SampleInput(make_inp((1, 3, 3)), args=((2, 2),))

    if mode in ['reflect', 'replicate', 'circular']:
        for shape, pad in cases:
            yield SampleInput(make_inp(shape), args=(pad, mode))
    else:  # mode == 'constant'
        for pad_value in (1., 2.):
            for shape, pad in cases:
                yield SampleInput(make_inp(shape), args=(pad, mode, pad_value))


def sample_inputs_constant_pad_nd(op_info, device, dtype, *args, **kwargs):
    # Inherit sample inputs from nn.pad, but transform them to fit
    # constant_pad_nd's interface
    nn_samples = sample_inputs_nn_pad(op_info, device, dtype, *args,
                                      mode='constant', **kwargs)

    # NOTE: primTorch is more strict about the type of the fill value argument
    # So we must cast it to the correct dtype
    from torch._prims_common import dtype_to_type
    scalar_type = dtype_to_type(dtype)

    def drop_mode_argument(input, pad, mode=None, value=None):
        if value is None:
            return SampleInput(input, args=(pad,))
        else:
            return SampleInput(input, args=(pad, scalar_type(value)))

    for sample in nn_samples:
        yield drop_mode_argument(sample.input, *sample.args, **sample.kwargs)


def np_unary_ufunc_integer_promotion_wrapper(fn):
    # Wrapper that passes PyTorch's default scalar
    #   type as an argument to the wrapped NumPy
    #   unary ufunc when given an integer input.
    #   This mimicks PyTorch's integer->floating point
    #   type promotion.
    #
    # This is necessary when NumPy promotes
    #   integer types to double, since PyTorch promotes
    #   integer types to the default scalar type.

    # Helper to determine if promotion is needed
    def is_integral(dtype):
        return dtype in [np.bool_, bool, np.uint8, np.int8, np.int16, np.int32, np.int64]

    @wraps(fn)
    def wrapped_fn(x):
        # As the default dtype can change, acquire it when function is called.
        # NOTE: Promotion in PyTorch is from integer types to the default dtype
        np_dtype = torch_to_numpy_dtype_dict[torch.get_default_dtype()]

        if is_integral(x.dtype):
            return fn(x.astype(np_dtype))
        return fn(x)

    return wrapped_fn

def sample_inputs_repeat_interleave(op_info, device, dtype, requires_grad, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    return [
        SampleInput(make_input(()), kwargs=dict(repeats=2)),
        SampleInput(make_input((2, 3, 4)), kwargs=dict(repeats=2)),
        SampleInput(make_input((2, 3, 4)), kwargs=dict(repeats=2, dim=1)),
        SampleInput(make_input((2, 3, 4)), kwargs=dict(repeats=torch.arange(3, device=device), dim=1))
    ]


def sample_inputs_stft(op_info, device, dtype, requires_grad, **kwargs):
    def mt(shape, **kwargs):
        return make_tensor(shape, device=device, dtype=dtype,
                           requires_grad=requires_grad, **kwargs)
    yield SampleInput(mt(100), kwargs=dict(n_fft=10))

    for center in [False, True]:
        yield SampleInput(mt(10), kwargs=dict(n_fft=7, center=center))
        yield SampleInput(mt((10, 100)), kwargs=dict(n_fft=16, hop_length=4, center=center))

    window = make_tensor(16, low=.5, high=2.0, dtype=dtype, device=device, requires_grad=requires_grad)
    yield SampleInput(
        mt((2, 100)), kwargs=dict(n_fft=16, window=window, return_complex=True, center=center))
    yield SampleInput(
        mt((3, 100)), kwargs=dict(n_fft=16, window=window, return_complex=True, center=center))
    if not dtype.is_complex:
        yield SampleInput(
            mt((10, 100)), kwargs=dict(n_fft=16, window=window, onesided=False))


def sample_inputs_istft(op_info, device, dtype, requires_grad, **kwargs):
    def mt(shape, **kwargs):
        real_shape = shape if dtype.is_complex else shape + (2,)
        return make_tensor(real_shape, device=device, dtype=dtype,
                           requires_grad=requires_grad, **kwargs)

    yield SampleInput(mt((10, 2)), kwargs=dict(n_fft=10))
    yield SampleInput(mt((6, 3)), kwargs=dict(n_fft=6, onesided=False))
    yield SampleInput(mt((6, 4)), kwargs=dict(n_fft=10, onesided=True))

    for center in [False, True]:
        yield SampleInput(mt((10, 10, 6)), kwargs=dict(n_fft=10, center=center))
        yield SampleInput(mt((1, 9, 10)), kwargs=dict(n_fft=16, hop_length=4, center=center))

    window = make_tensor(10, low=.5, high=2.0, dtype=dtype, device=device, requires_grad=requires_grad)
    yield SampleInput(mt((10, 10, 6)), kwargs=dict(
        n_fft=10, window=window, center=center, return_complex=dtype.is_complex))
    yield SampleInput(mt((10, 10, 10)), kwargs=dict(
        n_fft=10, window=window[:8], win_length=8, center=center, return_complex=True))

    real_window = window if not dtype.is_complex else window.real
    yield SampleInput(mt((10, 5, 6)), kwargs=dict(n_fft=8, window=real_window[:8], center=center))


def sample_inputs_fftshift(op_info, device, dtype, requires_grad, **kwargs):
    def mt(shape, **kwargs):
        return make_tensor(shape, device=device, dtype=dtype,
                           requires_grad=requires_grad, **kwargs)

    yield SampleInput(mt((9, 10)))
    yield SampleInput(mt((50,)), kwargs=dict(dim=0))
    yield SampleInput(mt((5, 11)), kwargs=dict(dim=(1,)))
    yield SampleInput(mt((5, 6)), kwargs=dict(dim=(0, 1)))
    yield SampleInput(mt((5, 6, 2)), kwargs=dict(dim=(0, 2)))


def sample_inputs_linalg_cholesky_inverse(op_info, device, dtype, requires_grad=False, **kwargs):
    from torch.testing._internal.common_utils import random_well_conditioned_matrix

    # Cholesky factorization is for positive-definite matrices
    single_well_conditioned_matrix = random_well_conditioned_matrix(S, S, dtype=dtype, device=device)
    batch_well_conditioned_matrices = random_well_conditioned_matrix(2, S, S, dtype=dtype, device=device)
    single_pd = single_well_conditioned_matrix @ single_well_conditioned_matrix.mH
    batch_pd = batch_well_conditioned_matrices @ batch_well_conditioned_matrices.mH

    inputs = (
        torch.zeros(0, 0, dtype=dtype, device=device),  # 0x0 matrix
        torch.zeros(0, 2, 2, dtype=dtype, device=device),  # zero batch of matrices
        single_pd,
        batch_pd
    )
    test_cases = (torch.linalg.cholesky(a, upper=False) for a in inputs)
    for l in test_cases:
        # generated lower-triangular samples
        l.requires_grad = requires_grad
        yield SampleInput(l)  # upper=False by default
        yield SampleInput(l.detach().clone().requires_grad_(requires_grad), kwargs=dict(upper=False))

        # generate upper-triangular inputs
        u = l.detach().clone().mT.contiguous().requires_grad_(requires_grad)
        yield SampleInput(u, kwargs=dict(upper=True))

def sample_inputs_linalg_ldl_factor(op_info, device, dtype, requires_grad=False, **kwargs):
    from torch.testing._internal.common_utils import (
        random_hermitian_pd_matrix,
        random_symmetric_pd_matrix,
    )

    device = torch.device(device)

    # Symmetric inputs
    yield SampleInput(
        random_symmetric_pd_matrix(S, dtype=dtype, device=device),
        kwargs=dict(hermitian=False),
    )  # single matrix
    yield SampleInput(
        random_symmetric_pd_matrix(S, 2, dtype=dtype, device=device),
        kwargs=dict(hermitian=False),
    )  # batch of matrices
    yield SampleInput(
        torch.zeros(0, 0, dtype=dtype, device=device), kwargs=dict(hermitian=False)
    )  # 0x0 matrix
    yield SampleInput(
        torch.zeros(0, 2, 2, dtype=dtype, device=device), kwargs=dict(hermitian=False)
    )  # zero batch of matrices

    # Hermitian inputs
    # hermitian=True for complex inputs on CUDA is supported only with MAGMA 2.5.4+
    magma_254_available = device.type == 'cuda' and _get_magma_version() >= (2, 5, 4)
    if dtype.is_complex and (device.type == 'cpu' or magma_254_available):
        yield SampleInput(
            random_hermitian_pd_matrix(S, dtype=dtype, device=device),
            kwargs=dict(hermitian=True),
        )  # single matrix
        yield SampleInput(
            random_hermitian_pd_matrix(S, 2, dtype=dtype, device=device),
            kwargs=dict(hermitian=True),
        )  # batch of matrices

def sample_inputs_linalg_ldl_solve(op_info, device, dtype, requires_grad=False, **kwargs):
    # Generate LDL factors of symmetric (and Hermitian on CPU) matrices
    from torch.testing._internal.common_utils import random_hermitian_pd_matrix, random_symmetric_pd_matrix
    device = torch.device(device)
    symmetric_inputs = (
        random_symmetric_pd_matrix(S, dtype=dtype, device=device),  # single matrix
        random_symmetric_pd_matrix(S, 2, dtype=dtype, device=device),  # batch of matrices
        torch.zeros(0, 0, dtype=dtype, device=device),  # 0x0 matrix
        torch.zeros(0, 2, 2, dtype=dtype, device=device),  # zero batch of matrices
    )
    hermitian_inputs = (
        random_hermitian_pd_matrix(S, dtype=dtype, device=device),
        random_hermitian_pd_matrix(S, 2, dtype=dtype, device=device),
    ) if device.type == 'cpu' and dtype.is_complex else ()
    test_cases1 = (torch.linalg.ldl_factor_ex(a, hermitian=False) for a in symmetric_inputs)
    test_cases2 = (torch.linalg.ldl_factor_ex(a, hermitian=True) for a in hermitian_inputs)

    # Symmetric case
    for test_case in test_cases1:
        factors, pivots, _ = test_case
        factors.requires_grad = requires_grad
        for B_batch_shape in ((), factors.shape[:-2]):
            B = make_tensor((*B_batch_shape, factors.shape[-1], S), device=device, dtype=dtype, requires_grad=requires_grad)
            yield SampleInput(factors, args=(pivots, B), kwargs=dict(hermitian=False))
            clone_factors = factors.detach().clone().requires_grad_(requires_grad)
            yield SampleInput(clone_factors, args=(pivots, B), kwargs=dict(hermitian=False))

    # Hermitian case
    for test_case in test_cases2:
        factors, pivots, _ = test_case
        factors.requires_grad = requires_grad
        for B_batch_shape in ((), factors.shape[:-2]):
            B = make_tensor((*B_batch_shape, factors.shape[-1], S), device=device, dtype=dtype, requires_grad=requires_grad)
            yield SampleInput(factors, args=(pivots, B), kwargs=dict(hermitian=True))
            clone_factors = factors.detach().clone().requires_grad_(requires_grad)
            yield SampleInput(clone_factors, args=(pivots, B), kwargs=dict(hermitian=True))

def sample_inputs_linalg_lstsq(op_info, device, dtype, requires_grad=False, **kwargs):
    from torch.testing._internal.common_utils import random_well_conditioned_matrix

    device = torch.device(device)

    drivers: Tuple[str, ...]
    if device.type == 'cuda':
        drivers = ('gels',)
    else:
        drivers = ('gels', 'gelsy', 'gelss', 'gelsd')

    # we generate matrices of shape (..., n + delta, n)
    deltas: Tuple[int, ...]
    if device.type == 'cpu' or has_cusolver():
        deltas = (-1, 0, +1)
    # only square systems if Cusolver is not available
    # becase we solve a lstsq problem with a transposed matrix in the backward
    else:
        deltas = (0,)

    out = []
    for batch, driver, delta in product(((), (3,), (3, 3)), drivers, deltas):
        shape = batch + (3 + delta, 3)
        a = random_well_conditioned_matrix(*shape, dtype=dtype, device=device)
        a.requires_grad_(requires_grad)
        b = make_tensor(shape, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        out.append(SampleInput(a, args=(b,), kwargs=dict(driver=driver)))
    return out

def sample_inputs_householder_product(op_info, device, dtype, requires_grad, **kwargs):
    """
    This function generates input for torch.linalg.householder_product (torch.orgqr).
    The first argument should be a square matrix or batch of square matrices, the second argument is a vector or batch of vectors.
    Empty, square, rectangular, batched square and batched rectangular input is generated.
    """
    # Each column of the matrix is getting multiplied many times leading to very large values for
    # the Jacobian matrix entries and making the finite-difference result of grad check less accurate.
    # That's why gradcheck with the default range [-9, 9] fails and [-2, 2] is used here.
    samples = (
        SampleInput(make_tensor((S, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((S,), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),)),

        SampleInput(make_tensor((S + 1, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((S,), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),)),

        SampleInput(make_tensor((2, 1, S, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((2, 1, S,), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),)),

        SampleInput(make_tensor((2, 1, S + 1, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((2, 1, S,), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),)),

        SampleInput(make_tensor((0, 0), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(make_tensor((0,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),)),

        SampleInput(make_tensor((S, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((0,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),)),

        # m = n = S, k = S - 2
        SampleInput(make_tensor((S, S), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((S - 2,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),)),

        # m = S, n = S -1, k = S - 2
        SampleInput(make_tensor((S, S - 1), dtype=dtype, device=device, low=-2, high=2, requires_grad=requires_grad),
                    args=(make_tensor((S - 2,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),)),
    )

    return samples

def sample_inputs_ormqr(op_info, device, dtype, requires_grad, **kwargs):
    # create a helper function wrapping `make_tensor`
    make_input = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    def gen_inputs():
        batches = [(), (0, ), (2, ), (2, 1)]
        ns = [5, 2, 0]
        tf = [True, False]
        for batch, (m, n), left, transpose in product(batches, product(ns, ns), tf, tf):
            reflectors = make_input((*batch, m, n))
            tau = make_input((*batch, min(m, n)))
            other_matrix_shape = (m, n) if left else (n, m)
            other = make_input((*batch, *other_matrix_shape))
            kwargs = {"left": left, "transpose": transpose}
            yield SampleInput(reflectors, args=(tau, other,), kwargs=kwargs)

    return tuple(gen_inputs())

def sample_inputs_linalg_cholesky(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates always positive-definite input for torch.linalg.cholesky using
    random_hermitian_pd_matrix.
    The input is generated as the itertools.product of 'batches' and 'ns'.
    In total this function generates 8 SampleInputs
    'batches' cases include:
        () - single input,
        (0,) - zero batched dimension,
        (2,) - batch of two matrices,
        (1, 1) - 1x1 batch of matrices
    'ns' gives 0x0 and 5x5 matrices.
    Zeros in dimensions are edge cases in the implementation and important to test for in order to avoid unexpected crashes.
    """
    from torch.testing._internal.common_utils import random_hermitian_pd_matrix

    batches = [(), (0, ), (2, ), (1, 1)]
    ns = [5, 0]
    out = []
    for batch, n, upper in product(batches, ns, [True, False]):
        a = random_hermitian_pd_matrix(n, *batch, dtype=dtype, device=device)
        a.requires_grad = requires_grad
        out.append(SampleInput(a, kwargs={"upper": upper}))
    return out

def sample_inputs_symeig(op_info, device, dtype, requires_grad=False, **kwargs):
    out = sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad)

    for o in out:
        o.kwargs = {"upper": bool(np.random.choice([True, False])),
                    "eigenvectors": True}
        # A gauge-invariant function
        o.output_process_fn_grad = lambda output: (output[0], abs(output[1]))
        yield o

def sample_inputs_linalg_eig(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates input for torch.linalg.eig
    """
    def out_fn(output):
        return output[0], abs(output[1])

    samples = sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad)
    for sample in samples:
        sample.output_process_fn_grad = out_fn
        yield sample

def sample_inputs_linalg_eigh(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates input for torch.linalg.eigh/eigvalsh with UPLO="U" or "L" keyword argument.
    """
    def out_fn(output):
        if isinstance(output, tuple):
            # eigh function
            return output[0], abs(output[1])
        else:
            # eigvalsh function
            return output

    # Samples do not need to be Hermitian, as we're using gradcheck_wrapper_hermitian_input
    samples = sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad)
    for sample in samples:
        sample.kwargs = {"UPLO": np.random.choice(["L", "U"])}
        sample.output_process_fn_grad = out_fn
        yield sample


def sample_inputs_linalg_pinv(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates input for torch.linalg.pinv with hermitian=False keyword argument.
    """
    for o in sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad, **kwargs):
        real_dtype = o.input.real.dtype if dtype.is_complex else dtype
        # requires_grad path for rtol tensor is not implemented
        for rtol in (None, 1.0, torch.tensor(1.0, dtype=real_dtype, device=device)):
            o = clone_sample(o)
            o.kwargs = {"rtol": rtol}
            yield o


def sample_inputs_linalg_pinv_hermitian(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates input for torch.linalg.pinv with hermitian=True keyword argument.
    """
    for o in sample_inputs_linalg_invertible(op_info, device, dtype, requires_grad, **kwargs):
        o.kwargs = {"hermitian": True}
        yield o

def sample_inputs_linalg_solve(op_info, device, dtype, requires_grad=False, vector_rhs_allowed=True, **kwargs):
    """
    This function generates always solvable input for torch.linalg.solve
    We sample a fullrank square matrix (i.e. invertible) A
    The first input to torch.linalg.solve is generated as the itertools.product of 'batches' and 'ns'.
    The second input is generated as the product of 'batches', 'ns' and 'nrhs'.
    In total this function generates 18 SampleInputs
    'batches' cases include:
        () - single input,
        (0,) - zero batched dimension,
        (2,) - batch of two matrices.
    'ns' gives 0x0 and 5x5 matrices.
    and 'nrhs' controls the number of vectors to solve for:
        () - using 1 as the number of vectors implicitly
        (1,) - same as () but explicit
        (3,) - solve for 3 vectors.
    Zeros in dimensions are edge cases in the implementation and important to test for in order to avoid unexpected crashes.
    'vector_rhs_allowed' controls whether to include nrhs = () to the list of SampleInputs.
    torch.solve / triangular_solve / cholesky_solve (opposed to torch.linalg.solve) do not allow
    1D tensors (vectors) as the right-hand-side.
    Once torch.solve / triangular_solve / cholesky_solve and its testing are removed,
    'vector_rhs_allowed' may be removed here as well.
    """
    make_fullrank = make_fullrank_matrices_with_distinct_singular_values
    make_a = partial(make_fullrank, dtype=dtype, device=device, requires_grad=requires_grad)
    make_b = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    batches = [(), (0, ), (2, )]
    ns = [5, 0]
    if vector_rhs_allowed:
        nrhs = [(), (1,), (3,)]
    else:
        nrhs = [(1,), (3,)]

    for n, batch, rhs in product(ns, batches, nrhs):
        yield SampleInput(make_a(*batch, n, n), args=(make_b((batch + (n,) + rhs)),))


def sample_inputs_linalg_solve_triangular(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device)
    bs = (1, 2, 0)
    ns = (3, 0)
    ks = (1, 3, 0)

    for b, n, k, (left, upper, uni) in product(bs, ns, ks, product((True, False), repeat=3)):
        if b == 1:
            A = make_arg((n, n)) if left else make_arg((k, k))
            B = make_arg((n, k))
        else:
            A = make_arg((b, n, n)) if left else make_arg((b, k, k))
            B = make_arg((b, n, k))
        if uni:
            # Not really necessary, but writing it for consistency
            A.diagonal(0, -2, -1).fill_(1.)
        else:
            d = A.diagonal(0, -2, -1)
            d[d.abs() < 1e-6] = 1.
        if upper:
            A.triu_()
        else:
            A.tril_()
        kwargs = {"upper": upper, "left": left, "unitriangular": uni}
        if requires_grad:
            for grad_A, grad_B in product((True, False), repeat=2):
                # Either A or B needs to have a gradient
                if not grad_A and not grad_B:
                    continue
                yield SampleInput(
                    A.clone().requires_grad_(grad_A),
                    args=(B.clone().requires_grad_(grad_B),),
                    kwargs=kwargs)
        else:
            yield SampleInput(A, args=(B,), kwargs=kwargs)

def sample_inputs_legacy_solve(op_info, device, dtype, requires_grad=False, **kwargs):
    """
    This function generates always solvable input for legacy solve functions
    (the ones that are not in torch.linalg module).
    The difference from sample_inputs_linalg_solve is that here the right-hand-side of A x = b equation
    should have b.ndim >= 2, vectors are not allowed.
    Also the arguments order is swapped.
    """
    out = sample_inputs_linalg_solve(
        op_info, device, dtype, requires_grad=requires_grad, vector_rhs_allowed=False
    )

    def out_fn(output):
        return output[0]

    # Reverses tensor order
    for sample in out:
        sample.input, sample.args = sample.args[0], (sample.input,)
        if op_info.name == "solve":
            sample.output_process_fn_grad = out_fn
        yield sample


def sample_inputs_cholesky_solve(op_info, device, dtype, requires_grad=False, **kwargs):
    cholesky_inverse_samples = sample_inputs_linalg_cholesky_inverse(
        op_info, device, dtype, requires_grad=False
    )

    for sample in cholesky_inverse_samples:
        psd_matrix = sample.input
        sample.input = make_tensor(psd_matrix.shape, dtype=dtype, device=device, requires_grad=requires_grad, low=None, high=None)
        sample.args = (psd_matrix.requires_grad_(requires_grad),)
        yield sample


def sample_inputs_lu(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_fullrank_matrices_with_distinct_singular_values,
                       dtype=dtype, device=device, requires_grad=requires_grad)

    # not needed once OpInfo tests support Iterables
    batch_shapes = ((), (3,), (3, 3))
    for batch_shape, get_infos, size_delta in product(batch_shapes, (True, False), (-2, -1, 0, +1, +2)):
        shape = batch_shape + (S + size_delta, S)
        input = make_arg(*shape)
        yield SampleInput(input, args=(True, get_infos))

def sample_inputs_linalg_lu(op_info, device, dtype, requires_grad=False, **kwargs):
    full_rank = (op_info.name == "linalg.lu_factor")
    make_fn = make_tensor if not full_rank else make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_fn, dtype=dtype, device=device, requires_grad=requires_grad)

    def out_fn(output):
        if op_info.name == "linalg.lu":
            return output[1], output[2]
        else:
            return output

    batch_shapes = ((), (3,), (3, 3))
    # pivot=False only supported in CUDA
    pivots = (True, False) if torch.device(device).type == "cuda" else (True,)
    deltas = (-2, -1, 0, +1, +2)
    for batch_shape, pivot, delta in product(batch_shapes, pivots, deltas):
        shape = batch_shape + (S + delta, S)
        # Insanely annoying that make_fullrank_blablabla accepts a *shape and not a tuple!
        A = make_arg(shape) if not full_rank else make_arg(*shape)
        yield SampleInput(A, kwargs={"pivot": pivot}, output_process_fn_grad=out_fn)

def sample_inputs_lu_solve(op_info, device, dtype, requires_grad=False, **kwargs):
    """ Samples the inputs for both linalg.lu_solve and lu_solve """
    make_fn = make_fullrank_matrices_with_distinct_singular_values
    make_a = partial(make_fn, dtype=dtype, device=device)
    make_b = partial(make_tensor, dtype=dtype, device=device)

    def clone(X, requires_grad):
        Y = X.clone()
        Y.requires_grad_(requires_grad)
        return Y

    is_linalg_lu_solve = (op_info.name == "linalg.lu_solve")

    batches = ((), (0, ), (2, ))
    ns = (3, 1, 0)
    nrhs = (4, 1, 0)

    for n, batch, rhs in product(ns, batches, nrhs):
        A = make_a(*(batch + (n, n)))
        LU, pivots = torch.linalg.lu_factor(A)

        B = make_b(batch + (n, rhs))

        grads = (False,) if not requires_grad else (True, False)
        # we try all possible combinations of requires_grad for each input
        for LU_grad, B_grad in product(grads, grads):
            # when requires_grad == True, at least one input has to have requires_grad enabled
            if requires_grad and not LU_grad and not B_grad:
                continue

            if is_linalg_lu_solve:
                for adjoint, left in product((True, False), repeat=2):
                    yield SampleInput(clone(LU, LU_grad),
                                      args=(pivots, clone(B if left else B.mT, B_grad)),
                                      kwargs=dict(adjoint=adjoint, left=left))
            else:
                yield SampleInput(clone(B, B_grad), args=(clone(LU, LU_grad), pivots))

def sample_inputs_lu_unpack(op_info, device, dtype, requires_grad=False, **kwargs):
    def out_fn(output):
        return output[1], output[2]

    for lu_sample in sample_inputs_linalg_lu(op_info, device, dtype, requires_grad, **kwargs):
        lu_data, pivots = torch.linalg.lu_factor(lu_sample.input)
        lu_data.requires_grad_(requires_grad)
        yield SampleInput(lu_data, args=(pivots,), output_process_fn_grad=out_fn)


def sample_inputs_roll(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    args = ((0, 0), (1, 2), (0, 2), (2, 0), (-1, 0), (10000, 1), (2,), ((1, 2, -1), (0, 1, 2)))

    for arg in args:
        yield SampleInput(make_arg((0, 0, 0)), args=arg)
        yield SampleInput(make_arg((S, S, S)), args=arg)


def error_inputs_roll(op_info, device, **kwargs):
    err_msg1 = "`shifts` required"
    s1 = SampleInput(
        make_tensor((S,), dtype=torch.float32, device=device), args=(tuple(),)
    )
    yield ErrorInput(s1, error_regex=err_msg1)

    err_msg2 = ("shifts and dimensions must align")
    s2 = SampleInput(
        make_tensor((S, S), dtype=torch.float32, device=device), args=((2, 1), 0)
    )
    yield ErrorInput(s2, error_regex=err_msg2)

    err_msg3 = ("out of range")
    s3 = SampleInput(
        make_tensor((S, ), dtype=torch.float32, device=device), args=(0, 2)
    )
    yield ErrorInput(s3, error_regex=err_msg3, error_type=IndexError)

def sample_inputs_rot90(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    args = itertools.product(range(-5, 6), [(0, 1), (1, 2), (1, -1)])

    yield SampleInput(make_arg((S, S, S)))
    for arg in args:
        yield SampleInput(make_arg((S, S, S)), args=arg)


def error_inputs_rot90(op_info, device, **kwargs):
    err_msg1 = "expected total rotation dims"
    s1 = SampleInput(
        make_tensor((S, S), dtype=torch.float32, device=device), kwargs={"dims": (0,)}
    )
    yield ErrorInput(s1, error_regex=err_msg1)

    err_msg2 = "expected total dims >= 2"
    s2 = SampleInput(
        make_tensor((S,), dtype=torch.float32, device=device),
    )
    yield ErrorInput(s2, error_regex=err_msg2)

    err_msg3 = "expected rotation dims to be different"
    s3 = SampleInput(
        make_tensor((S, S), dtype=torch.float32, device=device), kwargs={"dims": (1, 1)}
    )
    yield ErrorInput(s3, error_regex=err_msg3)


def sample_inputs_std_var(op_info, device, dtype, requires_grad, **kwargs):
    tensor_nd = partial(make_tensor, (S, S, S), device=device, dtype=dtype,
                        requires_grad=requires_grad)
    tensor_1d = partial(make_tensor, (S,), device=device, dtype=dtype,
                        requires_grad=requires_grad)

    return [
        SampleInput(tensor_nd()),
        SampleInput(tensor_nd(), kwargs=dict(dim=1)),
        SampleInput(tensor_nd(), kwargs=dict(dim=1, unbiased=True, keepdim=True)),
        SampleInput(tensor_1d(), kwargs=dict(dim=0, unbiased=True, keepdim=True)),
        SampleInput(tensor_1d(), kwargs=dict(dim=0, unbiased=False, keepdim=False)),

        SampleInput(tensor_nd(), kwargs=dict(dim=(1,), correction=S // 2)),
        SampleInput(tensor_nd(), kwargs=dict(dim=None, correction=0, keepdim=True)),
    ]


def _generate_correlation_inputs(device, dtype, requires_grad, **kwargs):
    shapes = [(2,), (1, 2), (3, 2), (2, 3)]
    for shape in shapes:
        yield make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)


def sample_inputs_corrcoef(op_info, device, dtype, requires_grad, **kwargs):
    return [SampleInput(t) for t in _generate_correlation_inputs(device, dtype, requires_grad)]


def sample_inputs_cov(op_info, device, dtype, requires_grad, **kwargs):
    inputs = []
    for t in _generate_correlation_inputs(device, dtype, requires_grad):
        inputs.append(SampleInput(t))
        num_observations = t.numel() if t.ndimension() < 2 else t.size(1)
        fweights = make_tensor((num_observations,), dtype=torch.int, device=device, low=1, high=10)
        aweights = make_tensor((num_observations,), dtype=torch.float, device=device, low=0, high=1, requires_grad=requires_grad)
        for correction, fw, aw in product(range(num_observations), [None, fweights], [None, aweights]):
            inputs.append(SampleInput(t.clone().requires_grad_(requires_grad),
                                      kwargs={'correction': correction, 'fweights': fw, 'aweights': aw}))
    return inputs


def error_inputs_cov(op_info, device, **kwargs):
    a = torch.rand(S, device=device)
    error_inputs = []
    error_inputs.append(ErrorInput(
        SampleInput(torch.rand(S, S, S, device=device)),
        error_regex="expected input to have two or fewer dimensions"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'fweights': torch.rand(S, S, device=device)}),
        error_regex="expected fweights to have one or fewer dimensions"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'aweights': torch.rand(S, S, device=device)}),
        error_regex="expected aweights to have one or fewer dimensions"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'fweights': torch.rand(S, device=device)}),
        error_regex="expected fweights to have integral dtype"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'aweights': torch.tensor([1, 1], device=device)}),
        error_regex="expected aweights to have floating point dtype"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'fweights': torch.tensor([1], device=device)}),
        error_regex="expected fweights to have the same numel"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'aweights': torch.rand(1, device=device)}),
        error_regex="expected aweights to have the same numel"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'fweights': torch.tensor([-1, -2, -3, -4 , -5], device=device)}),
        error_regex="fweights cannot be negative"))
    error_inputs.append(ErrorInput(
        SampleInput(a, kwargs={'aweights': torch.tensor([-1., -2., -3., -4., -5.], device=device)}),
        error_regex="aweights cannot be negative"))
    return error_inputs


def sample_inputs_svd(op_info, device, dtype, requires_grad=False, **kwargs):
    make_fullrank = make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_fullrank, dtype=dtype, device=device, requires_grad=requires_grad)

    is_linalg_svd = ("linalg.svd" in op_info.name)
    batches = [(), (0, ), (3, )]
    ns = [0, 3, 5]

    def uniformize(usv):
        S = usv[1]
        k = S.shape[-1]
        U = usv[0][..., :k]
        Vh = usv[2] if is_linalg_svd else usv[2].mH
        Vh = Vh[..., :k, :]
        return U, S, Vh

    def fn_U(usv):
        U, _, _ = uniformize(usv)
        return U.abs()


    def fn_S(usv):
        return uniformize(usv)[1]

    def fn_Vh(usv):
        # We also return S to test
        _, S, Vh = uniformize(usv)
        return S, Vh.abs()

    def fn_UVh(usv):
        U, S, Vh = uniformize(usv)
        return U @ Vh, S

    fns = (fn_U, fn_S, fn_Vh, fn_UVh)

    fullmat = 'full_matrices' if is_linalg_svd else 'some'

    for batch, n, k, fullmat_val, fn in product(batches, ns, ns, (True, False), fns):
        shape = batch + (n, k)
        yield SampleInput(make_arg(*shape), kwargs={fullmat: fullmat_val}, output_process_fn_grad=fn)


def sample_inputs_permute(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = [((1, 2, 3, 4), (0, 2, 3, 1)),
             ((1, 2, 3, 4), (0, -2, -1, 1)),
             ((), ()),
             ((1, 2, 3, 4), (2, 1, 3, 0))]

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=(args,))

def reference_inputs_permute(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_permute(op, device, dtype, requires_grad, **kwargs)

    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (
        ((), ()),
        ((1,), (0,)),
        ((2, 2), (1, 0)),
        ((2, 2), (0, 1)),
        ((2, 0, 1), (0, 2, 1)),
        ((3, 4, 2), (2, 1, 0)),
        ((3, 4, 2), (1, 0, 2)),
        ((3, 4, 2), (0, 1, 2)),
    )

    # Adds tricky permutations and permutations with noncontiguity
    for shape, permutation in cases:
        for p in itertools.permutations(permutation):
            a = make_arg(shape).permute(p)
            yield SampleInput(a, args=(permutation,))

            a = make_arg(shape, noncontiguous=True).permute(p)
            yield SampleInput(a, args=(permutation,))

def sample_inputs_linalg_svdvals(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    batches = [(), (0, ), (2, ), (1, 1)]
    ns = [5, 2, 0]

    for batch, m, n in product(batches, ns, ns):
        yield SampleInput(make_arg(batch + (m, n)))

def error_inputs_softshrink(op, device, **kwargs):
    yield ErrorInput(SampleInput(make_tensor((1,), dtype=torch.float, device=device), kwargs={"lambd": -0.5}),
                     error_regex="lambda must be greater or equal to 0, but found to be -0.5")

def sample_inputs_softshrink(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # The additional sample is to check additional values of lambd beyond the default
    # value (what is already checked by sample_inputs_elementwise_unary)
    for lbda in (0., 0.5):
        yield SampleInput(make_arg(S, S), kwargs={"lambd": lbda})

    yield from sample_inputs_elementwise_unary(op_info, device, dtype, requires_grad)

def sample_inputs_hardshrink(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # The additional sample is to check additional values of lambd beyond the default
    # value (what is already checked by sample_inputs_elementwise_unary)
    # Note that unlike softshrink, lambd is allowed to be negative for hardshrink
    for lbda in (-0.5, 0., 0.5):
        yield SampleInput(make_arg(S, S), kwargs={"lambd": lbda})

    yield from sample_inputs_elementwise_unary(op_info, device, dtype, requires_grad)


def sample_inputs_hardtanh(op_info, device, dtype, requires_grad=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # The additional sample is to check additional values of min_val and max_val beyond the default
    # value (what is already checked by sample_inputs_elementwise_unary)
    for max_val, min_val in ((-0.5, 0.5), (0.5, -0.5), (0., 0.)):
        yield SampleInput(make_arg(S, S), kwargs={"min_val": min_val, "max_val": max_val})

    yield from sample_inputs_elementwise_unary(op_info, device, dtype, requires_grad)

def sample_inputs_eig(op_info, device, dtype, requires_grad=False, **kwargs):
    eigvecs = make_tensor((S, S), device=device, dtype=dtype,
                          low=None, high=None)
    eigvals = make_tensor((S,), device=device, dtype=dtype,
                          low=None, high=None)
    # we produce only diagonazible inputs which do not have
    # complex eigenvalues for real inputs, as there is no
    # backward implementation for real inputs with complex
    # eigenvalues yet.
    input = (eigvecs * eigvals.unsqueeze(-2)) @ eigvecs.inverse()
    input.requires_grad_(requires_grad)

    def process_output(eigpair):
        eigvals, eigvecs = eigpair
        if dtype.is_complex:
            # eig produces eigenvectors which are normalized to 1 norm.
            # Note that if v is an eigenvector, so is v * e^{i \phi},
            # and |v| = |v * e^{i \phi}| = 1.
            # This, however, makes the eigenvector backward computation process
            # rather unstable unless the objective function is gauge-invariant,
            # that is if f(z) == f(|z|), for example.
            # Hence for complex inputs we ignore the phases and return only
            # the absolute values.
            return eigvals, eigvecs.abs()
        else:
            return eigvals, eigvecs

    return [
        SampleInput(
            input,
            kwargs=dict(eigenvectors=True),
            output_process_fn_grad=process_output
        ),
    ]


def sample_inputs_einsum(op_info, device, dtype, requires_grad=False, **kwargs):
    def c(t):
        return t.clone().requires_grad_(requires_grad)

    x = make_tensor((3,), dtype=dtype, device=device, requires_grad=requires_grad)
    y = make_tensor((4,), dtype=dtype, device=device, requires_grad=requires_grad)
    A = make_tensor((2, 3,), dtype=dtype, device=device, requires_grad=requires_grad)
    B = make_tensor((1, 3,), dtype=dtype, device=device, requires_grad=requires_grad)
    C = make_tensor((1, 2, 3,), dtype=dtype, device=device, requires_grad=requires_grad)
    D = make_tensor((1, 3, 4,), dtype=dtype, device=device, requires_grad=requires_grad)
    E = make_tensor((4, 4,), dtype=dtype, device=device, requires_grad=requires_grad)
    H = make_tensor((3, 3,), dtype=dtype, device=device, requires_grad=requires_grad)
    I = make_tensor((1, 3, 1,), dtype=dtype, device=device, requires_grad=requires_grad)

    inputs = []

    # Vector operations
    inputs.append(SampleInput([c(x)], args=('i->',)))                      # sum
    inputs.append(SampleInput([c(x), c(y)], args=('i,j->ij',)))               # outer

    # Matrix operations
    inputs.append(SampleInput([c(A)], args=("ij->i",)))                    # col sum
    inputs.append(SampleInput([c(A), c(B)], args=("ij,kj->ik",)))             # matmul
    inputs.append(SampleInput([c(A), c(E)], args=("ij,Ab->ijAb",)))           # matrix outer product

    # Tensor operations
    inputs.append(SampleInput([c(C), c(D)], args=("aij,ajk->aik",)))          # batch matmul
    inputs.append(SampleInput([c(D), c(E)], args=("aij,jk->aik",)))           # tensor matrix contraction
    inputs.append(SampleInput([c(C), c(B)], args=("ijk,ik->j",)))             # non contiguous

    # Test diagonals
    inputs.append(SampleInput([c(I)], args=('iji->j',)))                   # non-contiguous trace

    # Test ellipsis
    inputs.append(SampleInput([c(H)], args=("i...->...",)))
    inputs.append(SampleInput([c(C), c(x)], args=('...ik, ...j -> ij',)))

    return inputs


def sample_inputs_linalg_qr_geqrf(op_info, device, dtype, requires_grad=False, **kwargs):
    # QR is just well defined when the matrix is full rank
    make_fullrank = make_fullrank_matrices_with_distinct_singular_values
    make_arg = partial(make_fullrank, dtype=dtype, device=device, requires_grad=requires_grad)

    batches = [(), (0,), (2, ), (1, 1)]
    ns = [5, 2, 0]

    for batch, (m, n) in product(batches, product(ns, ns)):
        shape = batch + (m, n)
        yield SampleInput(make_arg(*shape))

def sample_inputs_flip(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    sizes = ((S, M, S), (S, 0, M))
    all_dims = ((0, 1, 2), (0,), (0, 2), (-1,), ())

    for size, dims in product(sizes, all_dims):
        yield SampleInput(make_arg(size), kwargs={"dims": dims})

def sample_inputs_fliplr_flipud(op_info, device, dtype, requires_grad, **kwargs):
    tensors = (
        make_tensor((S, M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        make_tensor((S, 0, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
    )
    return [SampleInput(tensor) for tensor in tensors]

def error_inputs_fliplr(op, device, **kwargs):
    yield ErrorInput(SampleInput(make_tensor((1,), dtype=torch.float, device=device)),
                     error_regex="Input must be >= 2-d.")

def error_inputs_flipud(op, device, **kwargs):
    yield ErrorInput(SampleInput(make_tensor((), dtype=torch.float, device=device)),
                     error_regex="Input must be >= 1-d.")

# TODO: clamp shares tensors among its sample inputs --- we should prohibit this!
def sample_inputs_clamp(op_info, device, dtype, requires_grad, **kwargs):
    x = make_tensor((S, M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
    lb = make_tensor((S, M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
    ub = make_tensor((S, M, S), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)

    def detach(tensor):
        return tensor.clone().detach_().requires_grad_(requires_grad)

    return [
        SampleInput(detach(x), args=(lb, ub)),
        SampleInput(detach(x), args=(detach(lb[0]), detach(ub[0]))),
        SampleInput(detach(x), args=(detach(lb[:, :1]),)),
    ]

def reference_inputs_elementwise_ternary(op, device, dtype, requires_grad, *, sample_inputs_func, supports_scalars=False, **kwargs):
    yield from sample_inputs_func(op, device, dtype, requires_grad, **kwargs)

    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    make_scalar_tensor = partial(make_tensor, (), device='cpu', dtype=dtype, requires_grad=requires_grad)
    supported_dtypes = op.supported_dtypes(device)

    # broadcasting and oncontiguous cases
    cases = (
        ((4, 4), (4, 4), (4, 4)),
        ((4, 4), (1, 4, 4), (4, 4)),
        ((4, 4), (1, 4, 4), (4, 1, 4)),
        ((4, 4, 1), (1, 4, 4), (4, 4)),
        ((4, 1), (1, 4, 4), (1, 4)),
        ((4, 4), (), (4, 4)),
        ((4, 4), (), ()),
        ((), (4, 4), (1, 4, 4)),
    )

    for a, b, c in cases:
        yield SampleInput(make_arg(a), args=(make_arg(b), make_arg(c)))
        yield SampleInput(make_arg(a, noncontiguous=True),
                          args=(make_arg(b).transpose(0, -1), make_arg(c, noncontiguous=True).transpose(0, -1)))

    # scalar cases
    if supports_scalars:
        cases = [
            ((), 1, 2,),
            ((), 1., 2),
            ((4, 4), 1., 2,),
            ((3, 4), make_scalar_tensor(), make_scalar_tensor()),
        ]

        if torch.complex64 in supported_dtypes:
            cases.extend([
                ((3, 1, 4), complex(1, 2), 3.),
            ])

        for a, b, c in cases:
            yield SampleInput(make_arg(a), args=(b, c))

    # type promotion cases
    # int x float
    if torch.float in supported_dtypes and torch.long in supported_dtypes:
        a = make_arg((), dtype=torch.long)
        b = make_arg((1, 4), dtype=torch.float)
        c = make_arg((3, 4))

        cases = (
            (a, b, c),
            (c, a, b),
        )

        for a, b, c in cases:
            yield SampleInput(a, args=(b, c))

    # NaN propagation
    if dtype.is_floating_point or dtype.is_complex:
        nan = float('nan') if dtype.is_floating_point else complex(float('nan'), float('nan'))

        a = make_arg((12,))
        a[4] = nan
        a[7] = nan
        b = make_arg((12,))
        b[1] = nan
        b[7] = nan
        c = make_arg((12,))
        c[9] = nan

        yield SampleInput(a, args=(b, c))


def _clamp_min_numpy(a, min=None):
    return np.maximum(a, min)


def _clamp_max_numpy(a, max=None):
    return np.minimum(a, max)


def _clamp_numpy(a, min=None, max=None):
    if min is None:
        return np.minimum(a, max)
    if max is None:
        return np.maximum(a, min)

    return np.minimum(max, np.maximum(a, min))

def sample_inputs_cross(op_info, device, dtype, requires_grad, **kwargs):
    sample0 = SampleInput(make_tensor((S, 3), device=device, dtype=dtype, requires_grad=requires_grad),
                          args=(make_tensor((S, 3), device=device, dtype=dtype, requires_grad=requires_grad),))
    sample1 = SampleInput(make_tensor((S, 3, S), device=device, dtype=dtype, requires_grad=requires_grad),
                          args=(make_tensor((S, 3, S), device=device, dtype=dtype, requires_grad=requires_grad),),
                          kwargs={'dim': 1})
    sample2 = SampleInput(make_tensor((S, 3), device=device, dtype=dtype, requires_grad=requires_grad),
                          args=(make_tensor((S, 3), device=device, dtype=dtype, requires_grad=requires_grad),),
                          kwargs={'dim': -1})

    return (sample0, sample1, sample2)

def sample_inputs_cumprod(op_info, device, dtype, requires_grad, **kwargs):
    def make_arg(shape):
        # shrink values to be in the interval [-1, +1] for better precision in gradgradcheck
        return make_tensor(shape, dtype=dtype, device=device, low=-1, high=+1, requires_grad=requires_grad)

    def prod_zeros(dim_select):
        assert len(dim_select) == 2
        result = make_arg(3 * (S,))
        result.narrow(dim_select[0], 0, 1).narrow(dim_select[1], 1, 1).zero_()
        result.narrow(dim_select[0], 2, 1).narrow(dim_select[1], 3, 1).zero_()
        result.narrow(dim_select[0], 4, 1).narrow(dim_select[1], 3, 1).zero_()
        return result

    for dim in range(3):
        yield SampleInput(make_arg((S, S, S)), args=(dim,))
    # Scalar tensors and empty tensor
    for size in [(), (1,), (0,)]:
        yield SampleInput(make_arg(size), args=(0,))

    yield SampleInput(prod_zeros([0, 1]), args=(1,))
    yield SampleInput(prod_zeros([0, 2]), args=(1,))
    yield SampleInput(prod_zeros([1, 2]), args=(1,))

    # test dtype kwarg
    yield SampleInput(prod_zeros([1, 2]), args=(1,), kwargs={'dtype': dtype})

def sample_inputs_view_as_complex(op_info, device, dtype, requires_grad, **kwargs):
    return [SampleInput(make_tensor((S, 2), dtype=dtype, device=device, requires_grad=requires_grad),)]

def sample_inputs_view_as_real(op_info, device, dtype, requires_grad, **kwargs):
    tensors = (
        make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),
        make_tensor((), dtype=dtype, device=device, requires_grad=requires_grad)
    )
    return [SampleInput(tensor) for tensor in tensors]

def sample_inputs_prod(op_info, device, dtype, requires_grad, **kwargs):
    def make_arg(shape):
        # shrink values to be in the interval [-1, +1] for better precision in gradgradcheck
        return make_tensor(shape, dtype=dtype, device=device, low=-1, high=+1, requires_grad=requires_grad)

    def prod_single_zero():
        result = make_arg(2 * (S,))
        result[0, 1] = 0
        return result

    for sample in sample_inputs_cumprod(op_info, device, dtype, requires_grad):
        # only Tensor, ignore other inputs
        yield SampleInput(sample.input.clone().requires_grad_(requires_grad))
        yield sample

    # Generates samples with keepdim = True
    for sample in sample_inputs_cumprod(op_info, device, dtype, requires_grad):
        sample.kwargs['keepdim'] = True
        yield sample

    yield SampleInput(prod_single_zero())
    yield SampleInput(make_arg((3, 3, 3)), args=(1,))
    yield SampleInput(make_arg((3, 3, 3)), args=(1,), kwargs={'keepdim': True})

    # test zero scalar tensor
    zero = make_arg(())
    zero.zero_()
    yield SampleInput(zero.clone().requires_grad_(requires_grad))
    yield SampleInput(zero.clone().requires_grad_(requires_grad), args=(0,))
    yield SampleInput(zero.clone().requires_grad_(requires_grad),
                      args=(0,),
                      kwargs={'keepdim': True})

def error_inputs_neg(op_info, device, **kwargs):
    si = SampleInput(torch.tensor((False, True), device=device))
    msg = ("Negation, the `\\-` operator, on a bool tensor is not supported."
           " If you are trying to invert a mask, use the `\\~` or"
           " `logical_not\\(\\)` operator instead.")
    return (ErrorInput(si, error_regex=msg),)

def sample_inputs_diag(op_info, device, dtype, requires_grad, **kwargs):
    vec_sample = SampleInput(make_tensor((M, ), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad))

    tensors = (
        make_tensor((M, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        make_tensor((3, 5), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
        make_tensor((5, 3), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
    )

    args = ((), (2,), (-2,), (1,), (2,))

    samples = []
    for tensor, arg in product(tensors, args):
        samples.append(SampleInput(tensor.clone().requires_grad_(requires_grad), args=arg))

    return samples + [vec_sample]

def sample_inputs_diagonal_diag_embed(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # Shapes for 2D Tensors
    shapes_2d = ((S, S), (3, 5), (5, 3))

    # Shapes for 3D Tensors
    shapes_3d = ((S, S, S),)

    kwargs_2d = (dict(), dict(offset=2), dict(offset=2), dict(offset=1))
    kwargs_3d = (dict(offset=1, dim1=1, dim2=2),
                 dict(offset=2, dim1=0, dim2=1),
                 dict(offset=-2, dim1=0, dim2=1))

    for shape, kwarg in chain(product(shapes_2d, kwargs_2d), product(shapes_3d, kwargs_3d)):
        yield SampleInput(make_arg(shape), kwargs=kwarg)

def reference_inputs_diagonal(op_info, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_diagonal_diag_embed(
        op_info, device, dtype, requires_grad, **kwargs)

    make_arg = partial(
        make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes2d = ((L, M),)
    shapes3d = ((L, M, S),)

    kwargs2d = (
        # dim1 > dim2 is allowed
        dict(dim1=1, dim2=0),
        # negative dims are allowed
        dict(dim1=-2, dim2=-1),
        # out of bounds offset should return an empty tensor
        dict(offset=10000),
    )

    kwargs3d = kwargs2d + (
        # make sure we can use non-sequential dims
        dict(offset=-1, dim1=0, dim2=2),
    )

    samples2d = product(shapes2d, kwargs2d)
    samples3d = product(shapes3d, kwargs3d)

    for shape, kwargs in chain(samples2d, samples3d):
        yield SampleInput(input=make_arg(shape), kwargs=kwargs)

def error_inputs_diagonal(op_info, device, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=torch.float32)

    shapes2d = ((M, L),)
    shapes3d = ((M, S, L),)

    kwargs2d = (
        # dim1 == dim2 is not allowed
        dict(dim1=1, dim2=1),
        # out of bounds dims are not allowed
        dict(dim1=10000),
        dict(dim2=10000),
    )

    kwargs3d = kwargs2d

    samples2d = product(shapes2d, kwargs2d)
    samples3d = product(shapes3d, kwargs3d)

    for shape, kwargs in chain(samples2d, samples3d):
        arg = make_arg(shape)
        sample = SampleInput(input=arg, kwargs=kwargs)

        dim1 = kwargs.get('dim1')
        dim2 = kwargs.get('dim2')
        num_dim = arg.dim()
        bound1 = -num_dim
        bound2 = num_dim - 1
        dim_range = range(bound1, bound2 + 1)
        dim1_cond = dim1 and dim1 not in dim_range
        dim2_cond = dim2 and dim2 not in dim_range

        if dim1 == dim2:
            err = f"diagonal dimensions cannot be identical {dim1}, {dim2}"
            yield ErrorInput(sample, error_regex=err, error_type=RuntimeError)
        elif dim1_cond or dim2_cond:
            err_dim = dim1 if dim1_cond else dim2
            err = (r"Dimension out of range \(expected to be in range of "
                   rf"\[{bound1}, {bound2}\], but got {err_dim}\)")
            yield ErrorInput(sample, error_regex=err, error_type=IndexError)
        else:
            raise RuntimeError("should be unreachable")

def sample_inputs_diagonal_scatter(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # Shapes for 2D Tensors
    shapes_2d = ((M, M), (3, 5), (5, 3))

    # Shapes for 3D Tensors
    shapes_3d = ((M, M, M),)

    args_2d = ((), (2,), (-2,), (1,))
    args_3d = ((1, 1, 2), (2, 0, 1), (-2, 0, 1))

    for input_shape, arg in chain(product(shapes_2d, args_2d), product(shapes_3d, args_3d)):
        input_ = make_arg(input_shape)
        # We can programatically figure out the right shape for src:
        # It should be the same size as input.diagonal(other_args...)
        if not isinstance(arg, tuple):
            arg_tuple = (arg,)
        else:
            arg_tuple = arg
        src_shape = input_.diagonal(*arg_tuple).size()
        src = make_arg(src_shape)
        yield SampleInput(input_, args=(src, *arg_tuple))


def sample_inputs_to_sparse(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    return (SampleInput(make_arg((S, S)), args=(), output_process_fn_grad=lambda x: x.to_dense()),
            SampleInput(make_arg((S, S)), args=(1,), output_process_fn_grad=lambda x: x.to_dense()),)

def sample_inputs_cross_entropy(op_info, device, dtype, requires_grad, **kwargs):
    batch_size, num_classes = shape = (2, 3)
    reductions = ("mean", "sum", "none")

    input_shape_and_kwargs: List[Tuple[Tuple[int, ...], Dict[str, Any]]] = [
        (shape, dict()),
        ((*shape, 1), dict()),
        ((*shape, 1, 2), dict()),
        ((*shape, 1, 2, 3), dict()),
        *[(shape, dict(reduction=reduction)) for reduction in reductions],
        *[
            (
                shape,
                dict(
                    weight=make_tensor((num_classes,), device=device, dtype=dtype),
                    reduction=reduction,
                ),
            )
            for reduction in reductions
        ],
        (shape, dict(ignore_index=1)),
    ]

    sample_inputs = []
    for (input_shape, kwargs), probabilities_target in itertools.product(input_shape_and_kwargs, (False, True)):
        input = make_tensor(input_shape, device=device, dtype=dtype, requires_grad=requires_grad)

        if probabilities_target:
            # ignore_index is not supported for probabilities target
            if "ignore_index" in kwargs:
                continue

            target = make_tensor(
                input_shape,
                low=0,
                high=1,
                device=device,
                dtype=dtype,
                requires_grad=requires_grad,
            )
        else:
            target = make_tensor(
                (batch_size, *input_shape[2:]),
                low=0,
                high=num_classes,
                device=device,
                dtype=torch.long,
            )

            if "ignore_index" in kwargs and torch.all(target == kwargs["ignore_index"]):
                # make sure at least one item in target is not ignored
                target[0] = random.sample(set(range(num_classes)) - {kwargs["ignore_index"]}, 1)[0]

        sample_inputs.append(SampleInput(input, args=(target,), kwargs=kwargs))

    return sample_inputs

# Used for log_softmax, softmax, softmin
def sample_inputs_softmax_variant(op_info, device, dtype, requires_grad, with_dtype=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = [
        ((S, ), (0, )),
        ((S, S), (0, )),
        ((S, S), (1, )),
        ((S, S), (-1, )),
        ((S, M, S), (2, )),
    ]

    # PyTorch on XLA throws an error when passed with dim argument for 0d tensor.
    # See https://github.com/pytorch/xla/issues/3061 for more details.
    if torch.device(device).type != 'xla':
        cases.append(((), (0, )))

    return [
        SampleInput(make_arg(shape), args=dim, kwargs=dict(dtype=torch.float64) if with_dtype else None)
        for shape, dim in cases
    ]

def sample_inputs_masked_softmax(op_info, device, dtype, requires_grad, with_dtype=False, **kwargs):
    """Sample inputs for masked softmax, log_softmax, and softmin.

    Masked normalization operator is a reduction operator with
    trailing mask optional argument. A mask is a bool tensor with the
    same shape as input or a shape that is broadcastable to input
    shape.
    """
    inputs: List[SampleInput] = []
    for sample_input in sample_inputs_softmax_variant(op_info, device, dtype, requires_grad, with_dtype=with_dtype, **kwargs):
        for mask in _generate_masked_op_mask(sample_input.input.shape, device, **kwargs):
            sample_input_args, sample_input_kwargs = sample_input.args, dict(mask=mask, **sample_input.kwargs)
            inputs.append(SampleInput(sample_input.input.clone().requires_grad_(requires_grad),
                                      args=sample_input_args, kwargs=sample_input_kwargs))
    return inputs

def sample_inputs_masked_cumops(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked cumsum and cumprod.
    """
    inputs: List[SampleInput] = []
    for sample_input in sample_inputs_softmax_variant(op_info, device, dtype, requires_grad, **kwargs):
        for mask in _generate_masked_op_mask(sample_input.input.shape, device, **kwargs):
            if type(mask) != torch.Tensor:
                continue
            sample_input_args, sample_input_kwargs = sample_input.args, dict(mask=mask, **sample_input.kwargs)
            if 'keepdim' in sample_input_kwargs:
                sample_input_kwargs.pop('keepdim')
            # dimension is required
            if sample_input_args:
                dim = sample_input.args[0]
            else:
                if 'dim' not in sample_input_kwargs:
                    continue
                dim = sample_input_kwargs.pop('dim')
                sample_input_args = (dim,)
            inputs.append(SampleInput(sample_input.input.clone().requires_grad_(requires_grad),
                                      args=sample_input_args, kwargs=sample_input_kwargs))

    return inputs

def sample_inputs_masked_logaddexp(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked logaddexp.
    """
    inputs: List[SampleInput] = []
    shapes = [(S,), (S, S), (S, M, S)]
    input_mask_lists = [list(_generate_masked_op_mask(shape, device, **kwargs)) for shape in shapes]
    other_mask_lists = [list(_generate_masked_op_mask(shape, device, **kwargs)) for shape in shapes]

    for shape, input_masks, other_masks in zip(shapes, input_mask_lists, other_mask_lists):
        for input_mask, other_mask in zip(input_masks, other_masks):
            input = make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)
            other = make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)
            inputs.append(SampleInput(input.clone().requires_grad_(requires_grad),
                                      args=(other.clone().requires_grad_(requires_grad),),
                                      kwargs=dict(input_mask=input_mask, other_mask=other_mask)))
    return inputs

def sample_inputs_masked_normalize(op_info, device, dtype, requires_grad, **kwargs):
    """Sample inputs for masked normalize.
    """
    inputs: List[SampleInput] = []
    for ord in [2.0, 1, float('inf'), float('-inf'), 0]:
        for sample_input in sample_inputs_softmax_variant(op_info, device, dtype, requires_grad, **kwargs):
            sample_input_args, sample_input_kwargs = (ord,) + sample_input.args, sample_input.kwargs.copy()
            inputs.append(SampleInput(sample_input.input.clone().requires_grad_(requires_grad),
                                      args=sample_input_args, kwargs=sample_input_kwargs))
    return inputs

def sample_inputs_logit(op_info, device, dtype, requires_grad, **kwargs):
    low, high = op_info.domain

    # Note: Operator is very sensitive at points near the
    # start and end of domain and leads to NaN for float16
    # if domain_eps is 1e-5.
    domain_eps = op_info._domain_eps if dtype != torch.float16 else 3e-2

    low = low + domain_eps
    high = high - domain_eps

    samples = (
        SampleInput(make_tensor((S, S, S), dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)),
        SampleInput(make_tensor((S, S, S), dtype=dtype, device=device, low=low,
                                high=high, requires_grad=requires_grad), args=(0.2,)),
        SampleInput(make_tensor((), dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)),
        SampleInput(make_tensor((), dtype=dtype, device=device, low=low,
                                high=high, requires_grad=requires_grad), args=(0.2,)),
    )

    return samples

def sample_inputs_isin(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    # isin has two paths based on the size of elements and test_elements.
    # if elements.numel() < 10 * pow(test_elements.numel(), 0.145):
    yield SampleInput(make_arg((L,)), args=(make_arg((S,)),))
    # else:
    yield SampleInput(make_arg((S,)), args=(make_arg((L,)),))

def sample_inputs_masked_scatter(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    yield SampleInput(make_arg((S, S)), args=(torch.randn(S, S, device=device) > 0, make_arg((S, S))))
    yield SampleInput(make_arg((S, S)), args=(torch.randn((S,), device=device) > 0, make_arg((S, S))))
    yield SampleInput(make_arg((S, S)), args=(bernoulli_scalar().to(device), make_arg((S, S))))
    yield SampleInput(make_arg((S,)),
                      args=(torch.randn(S, S, device=device) > 0, make_arg((S, S))),
                      broadcasts_input=True)


def sample_inputs_masked_fill(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    yield SampleInput(make_arg((S, S)), args=(torch.randn(S, S, device=device) > 0, 10))
    yield SampleInput(make_arg((S, S)), args=(torch.randn(S, S, device=device) > 0, make_arg(())))
    yield SampleInput(make_arg((S, S)), args=(torch.randn(S, device=device) > 0, 10))
    yield SampleInput(make_arg(()), args=(torch.randn((), device=device) > 0, 10))
    yield SampleInput(make_arg(()), args=(torch.randn((), device=device) > 0, make_arg(())))
    yield SampleInput(make_arg((S, S)), args=(torch.randn((), device=device) > 0, 10))

    yield SampleInput(make_arg((S,)),
                      args=(torch.randn(S, S, device=device) > 0, make_arg(())),
                      broadcasts_input=True)
    yield SampleInput(make_arg((S,)),
                      args=(torch.randn(S, S, device=device) > 0, 10),
                      broadcasts_input=True)

    if torch.device(device).type == 'cuda':
        # `self` and `mask` on CUDA but `value` is a CPU scalar tensor.
        yield SampleInput(make_arg((S, S)), args=(torch.randn(S, S, device=device) > 0, torch.randn(())))

def error_inputs_masked_fill(op_info, device, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=torch.float, requires_grad=False)
    # `value` is not a 0-D tensor.
    yield ErrorInput(SampleInput(make_arg((2, 2)), args=(make_arg(()) > 0, make_arg((1,)))),
                     error_regex="only supports a 0-dimensional value tensor, but got tensor with 1 dimension")
    # downcasting complex value (scalar overload)
    yield ErrorInput(SampleInput(make_arg((2, 2)), args=(make_arg(()) > 0, 1j)),
                     error_regex=r"value cannot be converted to type .* without overflow")
    # downcasting complex value (tensor overload)
    yield ErrorInput(SampleInput(torch.ones(2, dtype=torch.long, device=device),
                                 args=(make_arg(()) > 0, torch.tensor(1j, device=device))),
                     error_regex=r"value cannot be converted to type .* without overflow")

    if torch.device(device).type == 'cuda':
        # `self` and `mask` on CPU but `value` is a CUDA scalar tensor.
        yield ErrorInput(SampleInput(torch.randn((S, S), device='cpu'),
                                     args=(torch.randn(S, S, device='cpu') > 0,
                                           torch.randn((), device='cuda'))),
                         error_regex=r"to be on same device")


def sample_inputs_masked_select(op_info, device, dtype, requires_grad, **kwargs):
    samples = (
        SampleInput(make_tensor((M, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.randn(M, M, device=device) > 0,)),

        SampleInput(make_tensor((M, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.randn((M,), device=device) > 0,)),

        SampleInput(make_tensor((M,), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.randn((M, M), device=device) > 0,)),

        SampleInput(make_tensor((M, 1, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.randn((M, M), device=device) > 0,)),

        SampleInput(make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.tensor(1, device=device, dtype=torch.bool),)),

        SampleInput(make_tensor((M, M), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.tensor(1, device=device, dtype=torch.bool),)),

        SampleInput(make_tensor((), dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad),
                    args=(torch.randn((M, M), device=device) > 0,)),
    )

    return samples

def sample_inputs_matrix_exp(op_info, device, dtype, requires_grad, **kwargs):
    samples = (
        SampleInput(make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad)),
        SampleInput(make_tensor((S, S, S), dtype=dtype, device=device, requires_grad=requires_grad)),
    )

    return samples

def sample_inputs_matmul(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = (((L,), (L,)),
                  ((S, M), (M,)),
                  ((M,), (M, S)),
                  ((S, M), (M, S)),
                  ((S, 0), (0, M)),
                  ((S, S, M), (M,)),
                  ((S, S, M), (M, S)),
                  ((S, S, 0), (0, S)),
                  ((M,), (S, M, S)),
                  ((S, M), (S, M, S)),
                  ((0, 0), (S, 0, 0)),
                  ((S, S, M, M), (S, S, M, S)),
                  ((S, S, M, M), (M,)),
                  ((M,), (S, S, M, S)))
    sample_inputs = []
    for lhs_shape, rhs_shape in test_cases:
        lhs = make_tensor(lhs_shape, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        rhs = make_tensor(rhs_shape, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        if op_info.name == 'matmul':
            sample_inputs.append(SampleInput(lhs, args=(rhs,)))
        elif op_info.name == '__rmatmul__':
            sample_inputs.append(SampleInput(rhs, args=(lhs,)))
        else:
            raise RuntimeError("`op_info.name` must be 'matmul' or '__rmatmul__'")
    return tuple(sample_inputs)


def sample_inputs_meshgrid(op_info: OpInfo, device: torch.device, dtype: torch.dtype,
                           requires_grad: bool,
                           *, variant: str, **kwargs) -> List[SampleInput]:
    if variant == 'variadic':
        def make_inputs(
                tensors: List[torch.Tensor]) -> Tuple[Union[torch.Tensor,
                                                            List[torch.Tensor]],
                                                      Tuple[torch.Tensor, ...]]:
            return tensors[0], tuple(tensors[1:])
    elif variant == 'list':
        def make_inputs(
                tensors: List[torch.Tensor]) -> Tuple[Union[torch.Tensor,
                                                            List[torch.Tensor]],
                                                      Tuple[torch.Tensor, ...]]:
            return tensors, ()
    else:
        raise ValueError(
            'Unsupported variant, must be one of {"variadic", "list"}. '
            f'Got "{variant}".')

    SCALAR = torch.Size([])
    VECTOR = torch.Size([3])
    test_cases: List[List[torch.Size]] = [
        [SCALAR],
        [VECTOR],
        [VECTOR, SCALAR],
        [VECTOR, SCALAR, VECTOR],
        [VECTOR, SCALAR, VECTOR, SCALAR],
    ]

    sample_inputs = []
    for shapes, indexing in itertools.product(test_cases, {'xy', 'ij'}):
        input, args = make_inputs(
            [make_tensor(shape, dtype=dtype, device=device, requires_grad=requires_grad)
             for shape in shapes])
        sample_inputs.append(SampleInput(input=input, args=args,
                                         kwargs=dict(indexing=indexing)))
    return sample_inputs

def sample_inputs_polygamma(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    tensor_shapes = ((S, S), ())
    ns = (1, 2, 3, 4, 5)

    for shape, n in product(tensor_shapes, ns):
        yield SampleInput(make_arg(shape), args=(n,))


def sample_inputs_mvlgamma(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    tensor_shapes = ((S, S), ())
    ns = (1, 2, 3, 4, 5)

    # Since the accepted lower bound for input
    # to mvlgamma depends on `p` argument,
    # the following function computes the lower bound
    # which we pass to `make_tensor`.
    def compute_min_val(p):
        return (p - 1.) / 2

    for shape, n in product(tensor_shapes, ns):
        min_val = compute_min_val(n)
        if not dtype.is_floating_point:
            # Round-up minimum value for integral dtypes
            min_val += 1
        else:
            min_val += 2 * torch.finfo(dtype).eps
        yield SampleInput(make_arg(shape, low=min_val), args=(n,))


# Since `mvlgamma` has multiple entries,
# there are multiple common skips for the additional
# entries. Following function is a helper to that end.
def skips_mvlgamma(skip_redundant=False):
    skips = (
        # outside domain values are hard error for mvlgamma op.
        DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_float_domains'),
    )
    if skip_redundant:
        # Redundant tests
        skips = skips + (  # type: ignore[assignment]
            DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
        )
    return skips


# To test reference numerics against multiple values of argument `p`,
# we make multiple OpInfo entries with each entry corresponding to different value of p.
# We run the op tests from test_ops.py only for `p=1` to avoid redundancy in testing.
# Class `MvlGammaInfo` already contains the basic information related to the operator,
# it only takes arguments like `domain`, `skips` and `sample_kwargs`, which
# differ between the entries.
class MvlGammaInfo(UnaryUfuncInfo):
    def __init__(self, variant_test_name, domain, skips, sample_kwargs):
        super(MvlGammaInfo, self).__init__(
            'mvlgamma',
            ref=reference_mvlgamma if TEST_SCIPY else None,
            aliases=('special.multigammaln',),
            variant_test_name=variant_test_name,
            domain=domain,
            decorators=(precisionOverride({torch.float16: 5e-2}),),
            dtypes=all_types_and(torch.bfloat16),
            dtypesIfCUDA=all_types_and(torch.half),
            sample_inputs_func=sample_inputs_mvlgamma,
            supports_forward_ad=True,
            supports_fwgrad_bwgrad=True,
            skips=skips,
            sample_kwargs=sample_kwargs)


def sample_inputs_entr(op_info, device, dtype, requires_grad, **kwargs):
    low, _ = op_info.domain

    if requires_grad:
        low = 0 + op_info._domain_eps

    return (SampleInput(make_tensor((L,), dtype=dtype, device=device,
                                    low=low,
                                    requires_grad=requires_grad)),
            SampleInput(make_tensor((), dtype=dtype, device=device,
                                    low=low,
                                    requires_grad=requires_grad)))

# TODO: Consolidate `i0e` with sample_inputs_unary when `make_tensor`,
#       supports `exclude` argument.
#       For more context: https://github.com/pytorch/pytorch/pull/56352#discussion_r633277617
def sample_inputs_i0_i1(op_info, device, dtype, requires_grad, **kwargs):

    samples = (SampleInput(make_tensor((S,), dtype=dtype, device=device,
                                       requires_grad=requires_grad)),
               SampleInput(make_tensor((), dtype=dtype, device=device,
                                       requires_grad=requires_grad)))

    if requires_grad and op_info.op == torch.special.i0e:
        # NOTE: `i0e`'s first-order gradient is not continous
        # at `0`, hence we don't test `i0e` with any input being `0`.
        # TODO: Remove this when `make_tensor` supports excluding `0`.
        for sample in samples:
            t = sample.input
            t[t == 0] = torch.finfo(dtype).eps  # type: ignore[index]
    elif requires_grad and op_info.op != torch.special.i0e:
        # Special Case for gradient
        # Sample with `0` in the input
        t = make_tensor((S,), dtype=dtype, device=device,
                        requires_grad=requires_grad)
        t[0] = 0

        samples += (SampleInput(t),)  # type: ignore[assignment]

    return samples

def sample_inputs_cumulative_ops(op_info, device, dtype, requires_grad, supports_dtype_kwargs=True, **kwargs):
    def _make_tensor_helper(shape, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    samples = [
        SampleInput(_make_tensor_helper((S, S, S)), args=(0,)),
        SampleInput(_make_tensor_helper((S, S, S)), args=(1,)),
        SampleInput(_make_tensor_helper(()), args=(0,)),
    ]

    if supports_dtype_kwargs:
        # NOTE: if `dtype` is not same as input, then inplace variants fail with
        # `provided dtype must match the dtype of self tensor in cumsum`
        samples.append(SampleInput(_make_tensor_helper((S, S, S)), args=(1,), kwargs={'dtype': dtype}))

    return samples


def sample_inputs_unfold(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = (
        ((), (0, 1, 1)),
        ((S, S, S, S), (0, 3, 1)),
        ((S, S, S, S), (1, 3, 1)),
        ((S, S, S, S), (2, 3, 1)),
        ((S, S, S, S), (3, 3, 1)),
        ((S, S, S, S), (0, 3, 2)),
        ((S, S, S, S), (1, 3, 2)),
        ((S, S, S, S), (2, 3, 2)),
        ((S, S, S, S), (3, 3, 2)),
        ((S, S, S, S), (0, 4, 1)),
        ((S, S, S, S), (1, 4, 1)),
        ((S, S, S, S), (2, 4, 1)),
        ((S, S, S, S), (3, 4, 1)),
        ((M,), (0, 3, 1)),
        ((M,), (0, 3, 2)),
        ((M,), (0, 3, 3)),
        ((1000,), (0, 3, 11)),
        ((1000,), (0, 2, 27)),
        ((10, 10), (0, 1, 2)),
        ((10, 10), (1, 2, 3)),
        ((10, 10), (1, 2, 2)),
        ((S, S, S), (2, 3, 2)),
    )

    sample_inputs = []
    for shape, arguments in test_cases:
        sample_inputs += [SampleInput(make_tensor(shape, dtype=dtype, device=device,
                                      low=None, high=None,
                                      requires_grad=requires_grad),
                                      args=arguments)]
    return sample_inputs

def sample_inputs_split(op_info, device, dtype, requires_grad, *, list_args=False, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    if list_args:
        cases = (
            ((S, S, S), ([int(S / 3), S - int(S / 3) * 2, int(S / 3)],)),
            ((S, S, S), ([int(S / 2), S - int(S / 2) * 2, int(S / 2)], 2),),
            ((S, S, S), ([int(S / 2), S - int(S / 2) * 2, int(S / 2)], -2),)
        )
    else:
        cases = (  # type: ignore[assignment]
            ((S, S, S), (2,)),
            ((S, S, S), (S, 1)),
        )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)


def sample_inputs_split_with_sizes(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    cases = (((S, S, S), ([int(S / 3), S - int(S / 3) * 2, int(S / 3)],)),
             ((S, S, S), ([int(S / 3), S - int(S / 3), 0],)),
             ((S, S, S), ([int(S / 3), S - int(S / 3) * 2, int(S / 3)], 2)),
             ((S, S, S), ([int(S / 3), S - int(S / 3) * 2, int(S / 3)], -2)),
             )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)


def sample_inputs_msort(op_info, device, dtype, requires_grad, **kwargs):
    def apply_grad(t):
        if dtype in floating_types_and(torch.float16, torch.bfloat16):
            t.requires_grad_(requires_grad)

    def large_1d_unique(dtype, device):
        res = torch.randperm(L * L * L, dtype=torch.int64, device=device)
        res = res.to(dtype)
        apply_grad(res)
        return res

    samples = []
    # Test case for large tensor.
    largesample = SampleInput(large_1d_unique(dtype, device))

    sample = SampleInput(make_tensor((S, M, S), dtype=dtype, device=device,
                                     low=None, high=None,
                                     requires_grad=requires_grad))

    return [largesample, sample]

def sample_inputs_lerp(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    samples = (
        # no broadcast
        SampleInput(make_arg((S, S)), args=(make_arg((S, S)), 0.4)),
        # broadcast rhs
        SampleInput(make_arg((S, S)), args=(make_arg((S,)), 0.4)),
        # scalar tensor
        SampleInput(make_arg(()), args=(make_arg(()), 0.4)),
        # broadcast rhs scalar-tensor
        SampleInput(make_arg((S, S)), args=(make_arg(()), 0.4)),
        # broadcast rhs with weight tensor
        SampleInput(make_arg((S, S)), args=(make_arg((S,)), make_arg((S, S)))),
        # broadcast rhs and weight tensor
        SampleInput(make_arg((S, S)), args=(make_arg((S, 1)), make_arg((S,)))),
        # broadcast lhs
        SampleInput(make_arg((S,)), args=(make_arg((S, S)), 0.4), broadcasts_input=True),
        # scalar broadcast_lhs
        SampleInput(make_arg(()), args=(make_arg((S, S)), 0.4), broadcasts_input=True),
        # broadcast all
        SampleInput(make_arg((S, 1)), args=(make_arg((S, S)), 0.4), broadcasts_input=True),
        # tensor broadcast all
        SampleInput(make_arg((S, 1)), args=(make_arg((S, S)), make_arg((S, 1))),
                    broadcasts_input=True),
        # no broadcast with weight tensor
        SampleInput(make_arg((S, S)), args=(make_arg((S, S)), make_arg((S, S)))),
        # broadcast lhs with weight tensor
        SampleInput(make_arg((S,)), args=(make_arg((S, S)), make_arg((S, S))), broadcasts_input=True),
        # broadcast lhs and weight tensor
        SampleInput(make_arg((S,)), args=(make_arg((S, S, S)), make_arg((S, S))), broadcasts_input=True),
        # broadcast lhs and weight tensor variant
        SampleInput(make_arg((S, S)), args=(make_arg((S, S, S)), make_arg((S,))), broadcasts_input=True),
    )

    if dtype.is_complex:
        samples = samples + (  # type: ignore[assignment]
            # no broadcast
            SampleInput(make_arg((S, S)), args=(make_arg((S, S)), 0.4j)),
            SampleInput(make_arg((S, S)), args=(make_arg((S, S)), 1.2 + 0.1j)),
            # broadcast rhs
            SampleInput(make_arg((S, S)), args=(make_arg((S,)), 0.4j)),
            SampleInput(make_arg((S, S)), args=(make_arg((S, S)), 5.4 + 9j)),
            # scalar tensor
            SampleInput(make_arg(()), args=(make_arg(()), 0.4j)),
            SampleInput(make_arg(()), args=(make_arg(()), 6.1 + 0.004j)),
            # broadcast rhs scalar-tensor
            SampleInput(make_arg((S, S)), args=(make_arg(()), 0.4j)),
            SampleInput(make_arg((S, S)), args=(make_arg(()), 1 + 2j)),
        )

    return samples

def sample_inputs_tensordot(self, device, dtype, requires_grad, **kwargs):
    cases = (
        ((2, 2, 2), (2, 2, 2), (2)),
        ((2, 2, 1), (2, 1, 2), ([0, 1], [2, 0])),
    )
    samples = []
    for first_shape, second_shape, dims in cases:
        samples.append(SampleInput(make_tensor(first_shape, dtype=dtype, device=device,
                                   requires_grad=requires_grad),
                       args=(make_tensor(second_shape, dtype=dtype, device=device,
                             requires_grad=requires_grad),),
                       kwargs=dict(dims=dims,)))
    return tuple(samples)

def sample_inputs_kron(op_info, device, dtype, requires_grad, **kwargs):
    test_cases = (
        ((S, S), (M, L)),
    )

    sample_inputs = []
    for input_shape, other_shape in test_cases:
        input = make_tensor(input_shape, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        other = make_tensor(other_shape, dtype=dtype, device=device, low=None, high=None, requires_grad=requires_grad)
        sample = SampleInput(input, args=(other,))
        sample_inputs.append(sample)
    return tuple(sample_inputs)

def sample_inputs_inner(self, device, dtype, requires_grad, **kwargs):
    return (
        SampleInput(
            make_tensor((S, ), dtype=dtype, device=device, requires_grad=requires_grad),
            args=(
                make_tensor((S, ), dtype=dtype, device=device, requires_grad=requires_grad),
            )
        ),
        SampleInput(
            make_tensor((), dtype=dtype, device=device, requires_grad=requires_grad),
            args=(
                make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),
            )
        ),
    )

def sample_inputs_scatter(op_info, device, dtype, requires_grad, **kwargs):
    def _tensor(shape, dtype=dtype, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    def _gather(shape, index_dim, max_indices):
        return gather_variable(shape, index_dim, max_indices, device=device)

    zero = torch.tensor(0, dtype=torch.long, device=device)
    test_cases = (
        (_tensor((M, S)), (0, _gather((S, S), 1, M), _tensor((S, S)))),
        (_tensor((M, S)), (1, _gather((S, S), 0, S), _tensor((S, S)))),
        (_tensor((M, S)), (-1, _gather((S, S), 0, S), _tensor((S, S)))),
        (_tensor((M, S)), (0, _gather((M, S // 2), 1, M), _tensor((M, S // 2)))),
        (_tensor((M, S)), (1, _gather((M, S // 2), 0, S), _tensor((M, S // 2)))),
        (_tensor((M, S)), (-1, _gather((M, S // 2), 0, S), _tensor((M, S // 2)))),
        (_tensor(()), (0, zero.clone().detach(), _tensor(()))),
        (_tensor(()), (0, zero.clone().detach(), 2.5)),
    )

    samples = []
    for tensor, args in test_cases:
        samples.append(SampleInput(tensor, args=args))

        if not requires_grad:
            samples.append(SampleInput(
                tensor.clone().detach(),
                args=args, kwargs={'reduce': 'add'}
            ))

            if dtype.is_floating_point:
                samples.append(SampleInput(
                    tensor.clone().detach(),
                    args=args, kwargs={'reduce': 'multiply'}
                ))

    return samples

def sample_inputs_scatter_add(op_info, device, dtype, requires_grad, **kwargs):
    def _tensor(shape, dtype=dtype, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    def _gather(shape, index_dim, max_indices):
        return gather_variable(shape, index_dim, max_indices, device=device)

    zero = torch.tensor(0, dtype=torch.long, device=device)
    test_cases = (
        (_tensor((M, S)), (0, _gather((S, S), 1, M), _tensor((S, S)))),
        (_tensor((M, S)), (1, _gather((S, S), 0, S), _tensor((S, S)))),
        (_tensor((M, S)), (-1, _gather((S, S), 0, S), _tensor((S, S)))),
        (_tensor((M, S)), (0, _gather((M, S // 2), 1, M), _tensor((M, S // 2)))),
        (_tensor((M, S)), (1, _gather((M, S // 2), 0, S), _tensor((M, S // 2)))),
        (_tensor((M, S)), (-1, _gather((M, S // 2), 0, S), _tensor((M, S // 2)))),
        (_tensor(()), (0, zero.clone().detach(), _tensor(()))),
    )

    return [SampleInput(tensor, args=args) for tensor, args in test_cases]

def sample_inputs_scatter_reduce(op_info, device, dtype, requires_grad, **kwargs):
    def _tensor(shape, dtype=dtype, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    def _gather(shape, index_dim, max_indices):
        return gather_variable(shape, index_dim, max_indices, device=device)

    zero = torch.tensor(0, dtype=torch.long, device=device)
    test_cases = (
        ((M, S), 0, _gather((S, S), 1, M), (S, S)),
        ((M, S), 1, _gather((S, S), 0, S), (S, S)),
        ((M, S), -1, _gather((S, S), 0, S), (S, S)),
        ((M, S), 0, _gather((M, S // 2), 1, M), (M, S // 2)),
        ((M, S), 1, _gather((M, S // 2), 0, S), (M, S // 2)),
        ((M, S), -1, _gather((M, S // 2), 0, S), (M, S // 2)),
        ((), 0, zero.clone().detach(), ()),
    )

    reduce = op_info.variant_test_name
    for args, include_self in product(test_cases, [True, False]):
        inp_shape, dim, index, src_shape = args
        yield SampleInput(_tensor(inp_shape),
                          args=(dim, index, _tensor(src_shape), reduce),
                          kwargs={'include_self': include_self})


    # Sample inputs to test edge cases for backward
    # Check that gradients are propagated correctly for prod when zeros in self/src are reduced
    if requires_grad and reduce == 'prod':
        # This sample tests gradients for the following cases
        # (a) 1 zero reduced (from src (self[0, 1], self[1, 1]), from self (self[0, 0], self[2, 0]))
        # (b) 2 zeros reduced (1 from src and 1 from self (self[1, 0])
        # (c) no zeros reduced (self([2, 1]))
        # (d) 2 zeros reduced (both from src) is tested in test/test_autograd.py
        #     test_scatter_index_reduce_prod_gradgrad_error as this case is not supported for gradgrad
        input = torch.tensor([[0, 13], [0, 17], [0, 19]], dtype=dtype, device=device, requires_grad=requires_grad)
        src = torch.tensor([[0, 1, 2, 3], [0, 4, 0, 1], [2, 3, 5, 6]], dtype=dtype, device=device, requires_grad=requires_grad)
        idx = torch.tensor([[1, 1, 0, 0], [0, 0, 1, 1], [0, 0, 0, 1]], dtype=torch.long, device=device)

        yield SampleInput(input,
                          args=(1, idx, src, reduce),
                          kwargs={'include_self': True})

def sample_inputs_segment_reduce(op_info, device, dtype, requires_grad, *, mode='lengths', **kwargs):
    def _tensor(shape, dtype=dtype, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    zero = torch.tensor(0, dtype=torch.long, device=device)
    test_cases = (
        # inp_shape, dim, lengths, unsafe
        ((S,), 0, [0, 1, 2, 2], False),
        ((S,), 0, [0, 1, 2, 2], True),
        ((S,), 0, [2, 0, 3, 0], False),
        ((S, S), 0, [0, 1, 2, 2], False),
        # test when lengths do not sum to dim size
        ((M, S, S), 0, [1, 2, 0, 6, 0], True),
        # test for higher dimensions
        ((S, S), 1, [[0, 1, 2, 2] for _ in range(S)], False),
        ((S, S), 1, [[2, 0, 3, 0], [0, 1, 2, 2], [3, 0, 2, 0], [1, 1, 1, 2], [0, 1, 2, 2]], False),
        ((S, S, S), 1, [[0, 1, 2, 2] for _ in range(S)], False),
        ((S, S, S), 1, [[2, 0, 3, 0], [0, 1, 2, 2], [3, 0, 2, 0], [1, 1, 1, 2], [0, 1, 2, 2]], False),
    )

    reductions = ["max", "mean", "min", "sum", "prod"]
    for args, reduce, initial in product(test_cases, reductions, [1, 2]):
        inp_shape, dim, lengths, unsafe = args
        lengths_t = torch.tensor(lengths, dtype=torch.long, device=device)
        sample_input_kwargs = {'axis': dim, 'unsafe': unsafe, 'initial': initial}
        if mode == 'lengths':
            sample_input_kwargs['lengths'] = lengths_t
        elif mode == 'offsets':
            zeros_shape = list(lengths_t.shape)
            zeros_shape[dim] = 1
            offsets_t = torch.cat((lengths_t.new_zeros(zeros_shape), lengths_t), dim).cumsum_(dim)
            sample_input_kwargs['offsets'] = offsets_t
        else:
            raise RuntimeError(f"mode most be one of 'offsets' or 'lengths' got '{mode}'.")
        yield SampleInput(_tensor(inp_shape),
                          args=(reduce,),
                          kwargs=sample_input_kwargs)


def sample_inputs_ravel(op_info, device, dtype, requires_grad, **kwargs):
    samples = (SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                       low=None, high=None,
                                       requires_grad=requires_grad)),
               SampleInput(make_tensor((), dtype=dtype, device=device,
                                       low=None, high=None,
                                       requires_grad=requires_grad)),
               SampleInput(make_tensor((S, S, S), dtype=dtype, device=device,
                                       low=None, high=None,
                                       requires_grad=requires_grad, noncontiguous=True)),)

    return samples


def sample_inputs_tril_triu(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    cases = (((M, M), ()),
             ((M, M), (2,),),
             ((S, M, M), ()),
             ((S, M, M), (2,)),
             ((3, 3, S, S), ()),)

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)


def sample_inputs_clone_contiguous(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    yield SampleInput(make_arg((S, M, S)))
    yield SampleInput(make_arg(()))

def reference_inputs_clone_contiguous(op, device, dtype, requires_grad, **kwargs):
    # NOTE: the default memory format for clone is torch.preserve_format, for contiguous it's torch.contiguous_format
    # This exploits that default to test torch.preserve_format for clone, without causing an error when testing contiguous
    yield from sample_inputs_clone_contiguous(op, device, dtype, requires_grad, **kwargs)

    shapes = (
        (3, 5, 6),
        (1, 1, 3, 5, 6),
        (1, 1, 3, 5, 6, 1, 1),
        (1, 0, 3, 5, 0, 2),
        (1, 0, 3, 5, 0, 0, 1, 1, 2),
        (),
    )

    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for shape in shapes:
        yield SampleInput(make_arg(shape))
        yield SampleInput(make_arg(shape).transpose(0, -1))
        yield SampleInput(make_arg(shape, noncontiguous=True))
        yield SampleInput(make_arg(shape, noncontiguous=True).transpose(0, -1))

        yield SampleInput(make_arg(shape), kwargs={'memory_format': torch.contiguous_format})
        yield SampleInput(make_arg(shape).transpose(0, -1), kwargs={'memory_format': torch.contiguous_format})
        yield SampleInput(make_arg(shape, noncontiguous=True), kwargs={'memory_format': torch.contiguous_format})
        yield SampleInput(make_arg(shape, noncontiguous=True).transpose(0, -1), kwargs={'memory_format': torch.contiguous_format})

    # shape, strides, offset
    strided_cases = (
        ((5, 6, 2), (1, 1, 7), 2),
        ((5, 5, 4), (1, 1, 7), 2),
        ((5, 5, 2), (4, 5, 7), 3),
        ((5, 5, 2), (5, 5, 7), 3),
        ((5, 5, 2), (5, 5, 5), 3),
        ((9, 5, 2), (0, 1, 7), 3),
    )

    for shape, strides, offset in strided_cases:
        yield SampleInput(make_arg(500,).as_strided(shape, strides, offset))
        yield SampleInput(make_arg(500,).as_strided(shape, strides, offset), kwargs={'memory_format': torch.contiguous_format})

    # channels last 2D
    yield SampleInput(make_arg((2, 2, 2, 2)), kwargs={'memory_format': torch.channels_last})
    a = make_arg((2, 2, 2, 2)).permute(0, 3, 1, 2)
    yield SampleInput(a, kwargs={'memory_format': torch.channels_last})

    # channels last 3D
    yield SampleInput(make_arg((2, 2, 2, 2, 2)), kwargs={'memory_format': torch.channels_last_3d})
    a = make_arg((2, 2, 2, 2, 2)).permute(0, 4, 1, 2, 3)
    yield SampleInput(a, kwargs={'memory_format': torch.channels_last_3d})


def sample_inputs_sum_to_size(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # list of tuples (shape, shape) defining the shapes of the input and output tensors
    sample_shapes = [
        ((), ()),
        ((S), (1)),
        ((S, S), (1, 1)),
        ((S, S), (1, S)),
        ((S, S), (S, S)),
        ((S, S, S), (S, 1, S)),
    ]

    samples = []

    for input_shape, output_shape in sample_shapes:
        input_t = make_arg(input_shape)
        samples.append(SampleInput(input_t, args=(output_shape,)))

    return samples

def sample_inputs_resize_ops(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device)
    cases = (((S, S, S), (S * S, S)),
             ((), ()),
             ((), (1, 1, 1)),
             )

    for shape, args_or_shape in cases:
        # Update `args` based on operator
        if op_info.name == 'resize_':
            # resize_ takes shape/tuple of ints,
            args = (args_or_shape, )
        elif op_info.name == 'resize_as_':
            # resize_as_ takes another tensor
            args = (make_arg(shape, requires_grad=False), )  # type:ignore[assignment]
        else:
            raise ValueError("sample_inputs_resize_ops is being used with incorrect operator")

        yield(SampleInput(make_arg(shape, requires_grad=requires_grad), args=args))

def sample_inputs_view_reshape(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (
        ((S, S, S), (S * S, S)),
        ((S * S, S), (S, S, S)),
        ((S * S, S), (S, -1, S)),
        ((S * S * 2, S), (S, -1)),
        ((S,), (S,)),
        ((), ()),
        ((), (1,)),
    )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=(args,))

        if kwargs.get("transpose_samples", False) and len(shape) >= 2:
            transposed = make_arg(shape).transpose(0, 1).detach().requires_grad_(requires_grad)
            yield SampleInput(transposed, args=(args,))

def reference_inputs_view_reshape(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_view_reshape(op, device, dtype, requires_grad, **kwargs)

    cases = (
        ((125,), (25, 5)),
        ((25, 25), (1, 5, 5, 1, 5, 1, 5, 1)),
        ((16, 32), (2, 4, 1, 4, 4, 1, 4)),
        ((16, 12), (12, 16)),
        ((1, 16, 12), (12, 16)),
        ((1, 5, 1, 5), (25, 1)),
        ((2, 4, 2), (4, 4)),
        ((1, 4), (1, 1, 2, 1, 2)),
        ((3, 5, 7), (7, 5, 3)),
        ((1,), ()),
        ((5, 0, 2, 3), (5, 0, 2, 3)),
        ((2, 1, 0, 3, 1), (5, 0)),
        ((1,), ()),
        ((4, 5, 6), (4, 5, 6, 1, 1, 1)),
        ((), (1, 1, 1, 1)),
    )

    irreversible_cases = (
        ((), (-1,)),
        ((4, 7, 9, 1, 1), (1, 4, 3, -1, 1)),
    )

    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for a, b in cases:
        yield SampleInput(make_arg(a), args=(b,))
        yield SampleInput(make_arg(b), args=(a,))

        if kwargs.get("transpose_samples", False):
            yield SampleInput(make_arg(a, noncontiguous=True).transpose(0, -1), args=(b,))
        else:
            yield SampleInput(make_arg(a, noncontiguous=True), args=(b,))

    for a, b in irreversible_cases:
        yield SampleInput(make_arg(a), args=(b,))

def error_inputs_reshape(op, device, **kwargs):

    cases = (
        # Reshape to different numel
        ((2,), ()),
        ((1, 3, 0), ()),
        ((4, 3), (4, 2)),
        ((1, 3, 5), (5, 2, 2)),
        # No valid inference
        ((1, 3, 5), (5, -1, 2)),
        # Two inferred shapes
        ((1, 3, 5), (5, -1, -1)),
        ((1), (0, -1)),
        ((0, 5), (0, -1)),
    )

    make_arg = partial(make_tensor, dtype=torch.float32, device=device, requires_grad=False)
    for a, b in cases:
        yield ErrorInput(SampleInput(make_arg(a), args=(b,)), error_type=Exception, error_regex="")


def sample_inputs_view_as_reshape_as(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device)

    cases = (((S, S, S), (S * S, S)),
             ((), ()),
             ((), (1, 1)),
             )

    for case in cases:
        shape, shape_other = case
        inp = make_arg(shape, requires_grad=requires_grad)
        yield(SampleInput(inp, args=(make_arg(shape_other, requires_grad=False),)))

        if op_info.name != "view_as" and len(shape) >= 2:
            yield(SampleInput(
                inp.clone().transpose(0, 1).requires_grad_(requires_grad),
                args=(make_arg(shape_other, requires_grad=False),)))

def sample_inputs_atleast1d2d3d(op_info, device, dtype, requires_grad, **kwargs):
    input_list = []
    shapes = ((S, S, S, S), (S, S, S), (S, S), (S, ), (),)
    make_tensor_partial = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    samples = []
    for shape in shapes:
        input_list.append(make_tensor_partial(shape))
        samples.append(SampleInput(make_tensor_partial(shape)))
    samples.append(SampleInput(input_list, ))
    return samples

def sample_inputs_column_stack(op_info, device, dtype, requires_grad, **kwargs):
    input_list = []
    cases: Tuple[tuple, tuple] = (  # type: ignore[assignment]
        ((S, 2, 1), (S, 3, 1)),
        ((S), (S, 5)), ((), (1, S))
    )
    make_tensor_partial = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for shape1, shape2 in cases:
        input_list.append(SampleInput([make_tensor_partial(shape1), make_tensor_partial(shape2)]))

    return input_list

def sample_inputs_flatten(op_info, device, dtype, requires_grad, **kwargs):
    samples = []
    shapes = ((S, S, S), (S, S), (S, ), (),)
    make_tensor_partial = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for shape in shapes:
        samples.append(SampleInput(make_tensor_partial(shape)))
        if len(shape) > 1:
            samples.append(SampleInput(make_tensor_partial(shape), kwargs=dict(start_dim=1, end_dim=-1)))
    return samples

def reference_inputs_flatten(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_flatten(op, device, dtype, requires_grad, **kwargs)

    # shape x start_dim x end_dim
    cases = (
        ((5, 4, 0, 1, 3, 7), 1, 3),
        ((5, 4, 0, 1, 3, 7), 4, 5),
        ((5, 4, 1, 1, 3, 7), 2, 3),
        ((), 0, -1),
        ((1,), 0, -1),
        ((3, 7, 5), 1, 2),
        ((4, 5), 1, 1),
        ((1, 5, 5, 1, 5, 1, 5, 1), 0, 2),
        ((1, 5, 5, 1, 5, 1, 5, 1), 3, -1),
        ((1, 5, 5, 1, 5, 7, 5, 1), -2, -1),
        ((2, 4, 2), 0, 1),
        ((4, 2, 2), 1, 2),
        ((0, 3, 4, 5), 1, 3),
    )

    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for shape, start, end in cases:
        yield SampleInput(make_arg(shape), args=(start, end,))
        yield SampleInput(make_arg(shape, noncontiguous=True).transpose(0, -1), args=(start, end,))
        yield SampleInput(make_arg(shape).transpose(0, -1), args=(start, end,))

def sample_inputs_unflatten(op_info, device, dtype, requires_grad, **kwargs):
    # in_shape, dim, sizes
    args = (((8,), 0, (8,)),
            ((8,), 0, (4, 2)),
            ((8,), -1, (2, 2, 2)),
            ((8,), -1, (-1, 2)),
            ((3, 6, 2), 1, (2, 3)),
            ((3, 6, 2), -2, (2, 3)),
            ((3, 6, 2), -2, (-1, 3)),
            ((3, 2, 12), 2, (3, 2, 2)),
            ((4, 0), 0, (2, 2)),
            ((4, 0), 1, (2, 0, 0, 0)),
            )
    make_tensor_partial = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)
    for in_shape, dim, sizes in args:
        yield SampleInput(make_tensor_partial(in_shape), args=(dim, sizes))


def sample_inputs_select(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((S, S, S), (1, 2)),
             ((S, S, S), (-1, 2)),
             ((S, S, S), (-1, -1)),
             ((S, S, S), (1, -1)),
             ((S,), (0, 2))
             )

    for shape, args in cases:
        yield SampleInput(make_arg(shape), args=args)


def sample_inputs_select_scatter(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((S, S, S), (S, S), (1, 2)),
             ((S, S, S), (S, S), (-1, 2)),
             ((S, S, S), (S, S), (-1, -1)),
             ((S, S, S), (S, S), (1, -1)),
             ((S,), (), (0, 2))
             )

    for input_shape, src_shape, args in cases:
        input_ = make_arg(input_shape)
        src = make_arg(src_shape)
        yield SampleInput(input_, args=(src, *args))


def sample_inputs_slice_scatter(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((L, L, L), (L, L, L,), (0, 0, L, 1)),
             ((L, L, L), (L // 2, L, L,), (0, L // 2, L, 1)),
             ((L, L, L), (L // 4, L, L,), (0, L // 2, L, 2)),
             ((L, L, L), (L, L, L,), (1, 0, L, 1)),
             ((L, L, L), (L, L // 2, L,), (1, L // 2, L, 1)),
             ((L, L, L), (L, L // 4, L,), (1, L // 2, L, 2)),
             ((L, L, L), (L, L, L,), (2, 0, L, 1)),
             ((L, L, L), (L, L, L // 2,), (2, L // 2, L, 1)),
             ((L, L, L), (L, L, L // 4,), (2, L // 2, L, 2)),
             )

    for input_shape, src_shape, args in cases:
        input_ = make_arg(input_shape)
        src = make_arg(src_shape)
        yield SampleInput(input_, args=(src, *args))

def sample_inputs_expand(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((S, 1, 1), (S, S, S)),
             ((S, 1, S), (S, S, S)),
             ((S, 1, S), (-1, S, -1)),
             ((S, 1, S), (-1, S, S)),
             ((S, 1), (S, S, S)),
             ((1,), (S, S, S)),
             ((1, S), (1, 1, S)),
             ((), ()),
             ((), (1, 3, 2)),
             )

    for case in cases:
        shape, args = case
        yield(SampleInput(make_arg(shape), args=(args, )))

def sample_inputs_conversion(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    shapes = ((),
              (2, 3))
    memory_format_options = [None, torch.contiguous_format]

    for shape, memory_format in itertools.product(shapes, memory_format_options):
        yield SampleInput(make_arg(shape),
                          kwargs={'memory_format': memory_format} if memory_format else {})
    yield SampleInput(make_arg((2, 3, 2, 3)), kwargs={'memory_format': torch.channels_last})

def sample_inputs_expand_as(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device)

    cases = (((S, 1, 1), (S, S, S)),
             ((), ()),
             ((), (1, 1)),
             )

    for shape, shape_other in cases:
        yield(SampleInput(make_arg(shape, requires_grad=requires_grad),
                          args=(make_arg(shape_other, requires_grad=False), )))


def sample_inputs_where(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    def make_bool_mask(shape):
        # Make sure atleast one element is nonzero,
        # except for empty tensor
        mask_t = make_tensor(shape, dtype=torch.bool, device=device, requires_grad=False)

        if mask_t.numel() == 0:
            return mask_t
        elif mask_t.numel() == 1:
            mask_t.fill_(True)
            return mask_t

        if mask_t.sum() == 0:
            def random_index(shape):
                return tuple(map(lambda max_idx: random.randint(0, max_idx), shape))

            mask_t[random_index(mask_t.shape)] = True
            return mask_t

        return mask_t

    cases = (((M, M), (M, M), (M, M), False),
             ((M, 1, M), (M, M), (M, M, 1), True),
             ((), (), (), False),
             ((M, 1, M), (), (M, M, 1), True),
             ((), (M, M), (), True),)

    for shape, mask_shape, other_shape, broadcasts_input in cases:
        yield SampleInput(make_arg(shape),
                          args=(make_bool_mask(mask_shape), make_arg(other_shape)),
                          broadcasts_input=broadcasts_input)

# TODO: add reference inputs for where(condition) signature
def reference_inputs_where(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_where(op, device, dtype, requires_grad, **kwargs)

    make_cond = partial(make_tensor, dtype=torch.bool, device=device, requires_grad=requires_grad)
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # noncontiguous
    c = make_cond((10, 3), noncontiguous=True)
    a = make_arg((10, 1), noncontiguous=True)
    b = make_arg((3, 10, 3)).transpose(0, -1)

    # NOTE that the OpInfo for where takes samples of the form a, cond, b
    yield SampleInput(a, args=(c, b))

    # type promoting
    other_dtype = torch.double if dtype is not torch.double else torch.long
    c = make_cond((10, 3), noncontiguous=True)
    a = make_arg((10, 1), dtype=torch.long)
    b = make_arg((10, 1))

    yield SampleInput(a, args=(c, b))

    # two python scalars
    c = make_cond((10, 3), noncontiguous=True)
    a = make_arg((1,)).item()
    b = make_arg((1,)).item()

    yield SampleInput(a, args=(c, b))

    # NaN propagation
    if dtype.is_floating_point or dtype.is_complex:
        if dtype.is_floating_point:
            nan = float('nan')
        else:
            # dtype.is_complex
            nan = complex(float('nan'), float('nan'))
        c = make_cond((1, 10, 3))
        a = make_arg((10, 3), noncontiguous=True)
        a[2, 1] = nan
        b = make_arg((1, 3))
        b[0, 2] = nan

        yield SampleInput(a, args=(c, b))

    # Python scalars type promotion
    for scalar in (0, 0.0, 2j, False):
        yield SampleInput(scalar, args=(c, b))
        yield SampleInput(a, args=(c, scalar))


def error_inputs_where(op_info, device, **kwargs):
    shape = (S,)
    err_msg = "Expected all tensors to be on the same device"
    for devices in product(('cpu', device), repeat=3):
        if len(set(devices)) == 2:
            si = SampleInput(make_tensor(shape, device=devices[0], dtype=torch.float32),
                             args=(make_tensor(shape, dtype=torch.bool, device=devices[1]),
                             make_tensor(shape, device=devices[2], dtype=torch.float32)))
            yield ErrorInput(si, error_regex=err_msg)

def sample_inputs_nonzero(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    sizes = ((), (S,), (S, S), (S, S, S), (S, 1, S), (S, 0, S))

    inputs = []
    for shape in sizes:
        # construct input without any non-zero elements
        zeros = torch.zeros(shape, dtype=dtype, device=device, requires_grad=requires_grad)
        inputs.append(zeros)

        # construct input with mixed zero and non-zero elements
        mixed = make_arg(shape).requires_grad_(False)
        mask_t = make_tensor(shape, dtype=torch.bool, device=device, requires_grad=False)
        mixed[mask_t] = 0
        inputs.append(mixed)

    for input_t, as_tuple in product(inputs, [False, True]):
        yield(SampleInput(input_t.clone().requires_grad_(requires_grad),
                          kwargs=dict(as_tuple=as_tuple)))

def sample_inputs_chunk(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    cases = (((S, S, S), (2,)),
             ((S, S, S), (S, 1)),
             ((S, S, S), (S, -1)))

    for case in cases:
        shape, args = case
        yield(SampleInput(make_arg(shape), args=args))

def reference_inputs_chunk(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_chunk(op, device, dtype, requires_grad, **kwargs)

    make_arg = partial(make_tensor, dtype=dtype, device=device, requires_grad=requires_grad)

    # shape x chunks x dim
    cases = (
        ((13, 9, 11), 17, -1),
        ((13, 9, 11), 11, -1),
        ((13,), 12, -1),
        ((15,), 12, -1),
        ((15,), 7, 0),
        ((15,), 9, 0),
        ((3, 7), 9, 1),
        ((3, 7), 9, 0),
        ((3, 7), 2, 0),
        ((3, 7), 3, 0),
        ((3, 7), 1, 0),
        ((3, 7), 1, 1),
        ((4, 4), 2, 0),
    )

    for shape, chunks, dim in cases:
        yield SampleInput(make_arg(shape), args=(chunks, dim))

def sample_inputs_kthvalue(op_info, device, dtype, requires_grad, **kwargs):
    def _tensor(shape, dtype=dtype, low=None, high=None):
        return make_tensor(shape, dtype=dtype, device=device, low=low, high=high, requires_grad=requires_grad)

    test_cases = [
        (_tensor((S, S, S)), (2,)),
        (_tensor((S, S, S)), (2, 1,)),
        (_tensor((S, S, S)), (2, -1,)),
        (_tensor((S, S, S)), (2, 1, True,)),
        (_tensor((S, S, S)), (2, -1, True,)),
        (_tensor((S,)), (2, 0,)),
        (_tensor((S,)), (2, 0, True,)),
        (_tensor(()), (1,)),
        (_tensor(()), (1, 0,)),
        (_tensor(()), (1, 0, True))
    ]

    return [SampleInput(tensor, args=args) for tensor, args in test_cases]

def error_inputs_kthvalue(op_info, device, **kwargs):
    # tests overlapping output fails
    t = make_tensor(10, dtype=torch.float32, device=device)
    indices = torch.empty((), device=device, dtype=torch.long)
    si = SampleInput(t, args=(5,), kwargs={'out': (t, indices)})

    k_out_of_range_err = "selected number k out of range for dimension"
    return (ErrorInput(si, error_regex="unsupported operation"),
            ErrorInput(SampleInput(torch.randn(2, 2, device=device), args=(3, 0)),
                       error_regex=k_out_of_range_err),
            ErrorInput(SampleInput(torch.randn(2, 2, device=device), args=(3,)),
                       error_regex=k_out_of_range_err),
            ErrorInput(SampleInput(torch.tensor(2, device=device), args=(3,)),
                       error_regex=k_out_of_range_err),)

def sample_inputs_dropout(op_info, device, dtype, requires_grad, *,
                          train=None, valid_input_dim=None, **kwargs):
    make_arg = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    if valid_input_dim:
        cases = ((S,) * i for i in valid_input_dim)
    else:
        cases = ((S, S), (S,), ())
    p_vals = [0.0, 0.5, 1.0]
    # This is to handle special case for feature_alpha_dropout which has different
    # supported dtypes depending on `train` parameter
    training_vals = [train] if train is not None else [True, False]

    for case, p, training in product(cases, p_vals, training_vals):
        yield SampleInput(make_arg(case), kwargs=dict(p=p, training=training))
    yield SampleInput(make_arg(case), kwargs=dict())


def sample_inputs_embedding_bag(op_info, device, dtype, requires_grad, **kwargs):
    def make_input(shape):
        return make_tensor(shape, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_long_input(shape, *, low, high, noncontiguous=False):
        return make_tensor(shape, device=device, dtype=torch.long, low=low, high=high,
                           noncontiguous=noncontiguous)

    def make_per_sample_weight(flag, idx):
        # a tensor of float / double weights, or None
        # to indicate all weights should be taken to be 1
        if flag:
            return make_input(idx.shape)
        return None

    offsets = torch.tensor([0, 3], device=device, dtype=torch.long)
    for generate_per_sample_weight in (True, False):
        for mode in ('sum', 'mean', 'max'):
            # per_sample_weights is only supported for mode='sum' (got mode='****')
            if generate_per_sample_weight and mode in ('mean', 'max'):
                continue

            # 1-D index tensor
            idx = make_long_input((S,), low=0, high=M)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((M, S)), args=(idx,),
                              kwargs={'offsets': offsets, 'mode': mode,
                                      'per_sample_weights': per_sample_weights})

            idx = make_long_input((S,), low=0, high=M, noncontiguous=True)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((M, S)), args=(idx,),
                              kwargs={'offsets': offsets, 'mode': mode,
                                      'per_sample_weights': per_sample_weights})

            # bag with zero length
            idx = make_long_input((S,), low=0, high=M, noncontiguous=True)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((M, S)), args=(idx,),
                              kwargs={'offsets': torch.tensor([0, 0, 3], device=device, dtype=torch.long),
                                      'mode': mode,
                                      'per_sample_weights': per_sample_weights})

            # 2-D index tensor
            idx = make_long_input((S, S), low=0, high=M)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((M, S)), args=(idx,),
                              kwargs={'mode': mode, 'per_sample_weights': per_sample_weights})

            idx = make_long_input((S, S), low=0, high=M, noncontiguous=True)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((M, S)), args=(idx,),
                              kwargs={'mode': mode, 'per_sample_weights': per_sample_weights})

            # The gradient vector at `padding_idx` is not updated.
            # Negative padding_idx
            idx = make_long_input((6,), low=0, high=S)
            idx[0] = 4
            idx[4] = 4
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((S, S)), args=(idx,),
                              kwargs={'padding_idx': -1, 'offsets': offsets,
                                      'mode': mode, 'per_sample_weights': per_sample_weights},)

            idx = make_long_input((3, 3), low=0, high=S)
            # Positive padding_idx
            idx[0, 0] = 2
            idx[1, 1] = 2
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(make_input((S, S)), args=(idx,),
                              kwargs={'padding_idx': 2, 'mode': mode,
                                      'per_sample_weights': per_sample_weights},)

            idx = make_long_input((6, ), low=0, high=S)
            weights = make_input((S, S))
            offsets_ = torch.tensor([0, 3, 6], device=device, dtype=torch.long)
            per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
            yield SampleInput(weights, args=(idx,),
                              kwargs={'mode': mode, 'offsets': offsets_, 'include_last_offset': True},)

            if not requires_grad:
                # Following inputs return different gradient from the numerical gradient.
                # This is expected and relevant tests are present in `test_nn.py`.

                # Due to inplace renorming of weight, the numerical gradient doesn't match the
                # analytical gradient.
                idx = make_long_input((2, 2), low=0, high=S)
                weights = make_input((S, S)) * 2
                per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
                yield SampleInput(weights, args=(idx,),
                                  kwargs={'max_norm': 1., 'mode': mode,
                                          'per_sample_weights': per_sample_weights},)

                idx = make_long_input((6, ), low=0, high=S)
                weights = make_input((S, S)) * 2
                per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
                yield SampleInput(weights, args=(idx,),
                                  kwargs={'max_norm': 1., 'norm_type': 1.0,
                                          'mode': mode, 'offsets': offsets,
                                          'per_sample_weights': per_sample_weights},)

                if mode != 'max':
                    # Scale the gradient based on the inverse frequency of a particular index.
                    # Note : smax mode does not support sparse weights
                    idx = make_long_input((2, 2), low=0, high=S)
                    idx[0, 0] = 1
                    idx[0, 1] = 1
                    weights = make_input((S, S))
                    per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
                    yield SampleInput(weights, args=(idx,),
                                      kwargs={'scale_grad_by_freq': True, 'mode': mode,
                                              'per_sample_weights': per_sample_weights},)

                    # gradcheck not implemented for sparse tensors.
                    # Note : max mode does not support sparse weights
                    idx = make_long_input((6, ), low=0, high=S)
                    weights = make_input((S, S))
                    per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
                    yield SampleInput(weights, args=(idx,),
                                      kwargs={'sparse': True, 'offsets': offsets,
                                              'mode': mode, 'per_sample_weights': per_sample_weights})

                    idx = make_long_input((6, ), low=0, high=S)
                    idx[0] = 1  # freq more than 1
                    idx[1] = 1  # freq more than 1
                    idx[3] = 0  # padding_idx
                    weights = make_input((S, S)) * 2
                    per_sample_weights = make_per_sample_weight(generate_per_sample_weight, idx)
                    yield SampleInput(weights, args=(idx,),
                                      kwargs={'sparse': True, 'scale_grad_by_freq': True, 'padding_idx': 0,
                                              'max_norm': 1., 'offsets': offsets,
                                              'mode': mode, 'per_sample_weights': per_sample_weights})


def sample_inputs_embedding(op_info, device, dtype, requires_grad, **kwargs):
    def make_input(shape):
        return make_tensor(shape, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_long_input(shape, *, low, high):
        return make_tensor(shape, device=device, dtype=torch.long, low=low, high=high)

    # 0-D index tensor
    idx = make_long_input((), low=0, high=M)
    yield SampleInput(make_input((M, S)), args=(idx,),)

    # 1-D index tensor
    idx = make_long_input((S,), low=0, high=M)
    yield SampleInput(make_input((M, S)), args=(idx,),)

    # 2-D index tensor
    idx = make_long_input((S, S), low=0, high=M)
    yield SampleInput(make_input((M, S)), args=(idx,),)

    if not requires_grad:
        # Following inputs return different gradient from the numerical gradient.
        # This is expected and relevant tests are present in `test_nn.py`.

        # The gradient vector at `padding_idx` is not updated.
        idx = make_long_input((2, 2), low=0, high=S)
        idx[0, 0] = 2
        idx[1, 1] = 2
        yield SampleInput(make_input((S, S)), args=(idx,), kwargs={'padding_idx': 2},)

        idx = make_long_input((2, 2), low=0, high=S)
        idx[0, 0] = 4
        idx[1, 1] = 4
        yield SampleInput(make_input((S, S)), args=(idx,), kwargs={'padding_idx': -1},)

        # Due to inplace renorming of weight, the numerical gradient doesn't match the
        # analytical gradient.
        idx = make_long_input((2, 2), low=0, high=S)
        weights = make_input((S, S)) * 2
        yield SampleInput(weights, args=(idx,), kwargs={'max_norm': 1.},)

        idx = make_long_input((2, 2), low=0, high=S)
        weights = make_input((S, S)) * 2
        yield SampleInput(weights, args=(idx,), kwargs={'max_norm': 1., 'norm_type': 1.0},)

        # Scale the gradient based on the inverse frequency of a particular index.
        idx = make_long_input((2, 2), low=0, high=S)
        idx[0, 0] = 1
        idx[0, 1] = 1
        weights = make_input((S, S))
        yield SampleInput(weights, args=(idx,), kwargs={'scale_grad_by_freq': True},)

        # gradcheck not implemented for sparse tensors.
        idx = make_long_input((2, 2), low=0, high=S)
        weights = make_input((S, S))
        yield SampleInput(weights, args=(idx,), kwargs={'sparse': True})

        idx = make_long_input((3, 3), low=0, high=S)
        idx[0, 0] = 1  # freq more than 1
        idx[0, 1] = 1  # freq more than 1
        idx[1, 0] = 0  # padding_idx
        weights = make_input((S, S)) * 2
        yield SampleInput(weights, args=(idx,),
                          kwargs={'sparse': True, 'scale_grad_by_freq': True,
                                  'padding_idx': 0, 'max_norm': 1.})


def sample_inputs_one_hot(op_info, device, dtype, requires_grad, **kwargs):
    def make_input(shape, *, low, high):
        return make_tensor(shape, device=device, dtype=dtype, low=low, high=high, requires_grad=requires_grad)

    shapes = ((), (S,), (L, M, S))
    num_classess = (-1, 10)

    return [
        SampleInput(
            make_input(
                shape,
                low=0,
                high=10 if num_classes == -1 else num_classes // 2,
            ),
            kwargs=dict(num_classes=num_classes),
        )
        for shape, num_classes in itertools.product(shapes, num_classess)
    ]

def sample_inputs_tensorinv(op_info, device, dtype, requires_grad, **kwargs):
    make_arg = make_fullrank_matrices_with_distinct_singular_values

    def make_input():
        return make_arg(12, 12, device=device, dtype=dtype, requires_grad=requires_grad)

    # lhs / rhs shape can have any number of dimensions as long as their product equals 12
    shapes = [
        ((2, 2, 3), (12, 1)),
        ((4, 3), (6, 1, 2)),
    ]

    samples = []
    for shape_lhs, shape_rhs in shapes:
        inp = make_input().reshape(*shape_lhs, *shape_rhs).detach()
        inp.requires_grad_(requires_grad)
        samples.append(SampleInput(inp, kwargs=dict(ind=len(shape_lhs))))

    return samples

def sample_inputs_tensorsolve(op_info, device, dtype, requires_grad, **kwargs):
    a_shapes = [(2, 3, 6), (3, 4, 4, 3)]
    # Zero-dim tensors are not supported in NumPy, so we skip them for now.
    # NumPy is used in reference check tests.
    # See https://github.com/numpy/numpy/pull/20482 for tracking NumPy bugfix.
    # a_shapes += [(0, 0, 1, 2, 3, 0)]
    dimss = [None, (0, 2)]

    for a_shape, dims in itertools.product(a_shapes, dimss):
        a = make_tensor(a_shape, dtype=dtype, device=device, requires_grad=requires_grad)
        b = make_tensor(a_shape[:2], dtype=dtype, device=device, requires_grad=requires_grad)
        yield SampleInput(a, args=(b,), kwargs=dict(dims=dims))

def sample_inputs_loss(op_info, device, dtype, requires_grad, **kwargs):
    rhs_requires_grad = kwargs.get('rhs_requires_grad', requires_grad)
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    # Although most losses also support the reduce and size_average combination instead of reduce, the former is
    # deprecated since 0.4.1 and thus is not tested
    shapes_and_kwargs = (
        ((), None),
        ((S,), dict(reduction="mean")),
        ((S,), dict(reduction="sum")),
        ((S,), dict(reduction="none")),
        ((S, S), None),
        ((S, S, S), None),
    )

    for shape, kwargs in shapes_and_kwargs:
        yield SampleInput(_make_tensor(shape),
                          args=(_make_tensor(shape, requires_grad=rhs_requires_grad),),
                          kwargs=kwargs)

def sample_inputs_grid_sample(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    batch_size = 2
    num_channels = 3
    modes = ("bilinear", "nearest")
    align_cornerss = (False, True)
    padding_modes = ("zeros", "border", "reflection")

    sample_inputs = []
    for dim in (2, 3):

        modes_ = (*modes, "bicubic") if dim == 2 else modes

        for mode, padding_mode, align_corners in itertools.product(modes_, padding_modes, align_cornerss):
            sample_inputs.append(
                SampleInput(
                    _make_tensor((batch_size, num_channels, *[S] * dim)),
                    args=(_make_tensor((batch_size, *[S] * dim, dim)),),
                    kwargs=dict(
                        mode=mode,
                        padding_mode=padding_mode,
                        align_corners=align_corners,
                    )
                )
            )

    return sample_inputs

def sample_inputs_cosine_embedding_loss(op_info, device, dtype, requires_grad, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    def make_target(shape):
        shape = () if len(shape) == 1 else (shape[0], )
        t = torch.randint(0, 2, shape, device=device, dtype=torch.long)
        # Label with -1 or 1
        t = t * 2 - 1
        target = t.to(dtype=dtype).detach_().requires_grad_(requires_grad)
        return target

    shapes = ((S, S), (S,))
    reductions = ('none', 'mean', 'sum')
    for s, r in product(shapes, reductions):
        yield SampleInput(
            make_input(s),
            args=(make_input(s), make_target(s)),
            kwargs=dict(reduction=r, margin=random.uniform(-1, 1))
        )

def sample_inputs_ctc_loss(op_info, device, dtype, requires_grad, **kwargs):
    input_length = 50
    batch = 16
    num_char = 20
    target_length = 30

    def make_log_probs(s):
        t = make_tensor(s, device=device, dtype=dtype)
        log_probs = t.log_softmax(2).to(device=device, dtype=dtype).detach().requires_grad_(requires_grad=requires_grad)
        return log_probs

    reductions = ('none', 'mean', 'sum')
    zero_inf = (True, False)
    for r, z in product(reductions, zero_inf):
        log_probs = make_log_probs((input_length, batch, num_char))
        targets = torch.randint(1, num_char, (batch, target_length), dtype=torch.long, device=device)
        input_lengths = torch.full((batch, ), input_length, dtype=torch.long, device=device)
        target_lengths = torch.randint(10, target_length, (batch, ), dtype=torch.long, device=device)

        yield SampleInput(log_probs, args=(targets, input_lengths, target_lengths,), kwargs=dict(reduction=r, zero_infinity=z))

def sample_inputs_nll_loss(op_info, device, dtype, requires_grad, **kwargs):
    shape = (2, 3)
    num_classes = shape[1]
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    # FIXME: Derivative wrt. weight not implemented
    make_weight = partial(make_tensor, num_classes, device=device, dtype=dtype, requires_grad=False)

    def make_target(shape, zeros=False):
        s = (shape[0], *shape[2:]) if len(shape) > 1 else ()
        if zeros:
            return torch.zeros(s, device=device, dtype=torch.long)
        else:
            return make_tensor(s,
                               low=0,
                               high=shape[1] if len(shape) > 1 else shape[0],
                               device=device,
                               dtype=torch.long)


    def gen_shape_kwargs():
        # Batched, non-batched and 2d
        shapes = (shape, (num_classes,), shape + (2, 2))
        reductions = ('none', 'mean', 'sum')
        for reduction, s in product(reductions, shapes):
            yield make_input(s), make_target(s), dict(reduction=reduction)
            yield make_input(s), make_target(s), dict(weight=make_weight(), reduction=reduction)
            yield make_input(s), make_target(s), dict(weight=make_weight(low=0), reduction=reduction)
            yield make_input(s), make_target(s), dict(weight=make_weight(high=0), reduction=reduction)
            t = make_target(s)
            ignore = num_classes // 2
            # If "mean", nll returns NaN, so it's not differentiable at those points
            if t.eq(ignore).all() and reduction == "mean":
                t.fill_(0)
            yield make_input(s), t, dict(ignore_index=num_classes // 2, reduction=reduction)
            yield make_input(s), t, dict(ignore_index=num_classes // 2, reduction=reduction, weight=make_weight())
            # Test ignoring all the targets
            # If "mean", nll returns NaN, so it's not differentiable at those points
            if reduction != "mean":
                yield make_input(s), make_target(s, zeros=True), dict(ignore_index=0, reduction=reduction)

    for input, target, kwargs in gen_shape_kwargs():
        yield SampleInput(input, args=(target,), kwargs=kwargs)

def sample_inputs_binary_cross_entropy_with_logits(
    op_info, device, dtype, requires_grad, **kwargs
):
    make = partial(make_tensor, device=device, dtype=dtype)
    make_prob = partial(make, low=0, high=1)
    reductions = ("mean", "sum", "none")

    def make_weight_shape_kwargs():
        kwargs = []
        for shape in ((1,), (1, S), (S), (S, S)):
            kwargs.extend([((S, S), dict(reduction=reduction, weight=make(shape))) for reduction in reductions])
        return kwargs

    shapes_and_kwargs = [
        *[(shape, None) for shape in ((), (1,), (S,), (S, S), (S, S, S))],
        *[((S, S), dict(reduction=reduction)) for reduction in reductions],
        *make_weight_shape_kwargs(),
        *[((S, S), dict(reduction=reduction, pos_weight=make((S,), low=0))) for reduction in reductions],
        *[((S, S), dict(reduction=reduction, weight=make((S, S)), pos_weight=make((S,), low=0))) for reduction in reductions],
    ]

    for shape, kwargs in shapes_and_kwargs:
        yield SampleInput(
            make(shape, requires_grad=requires_grad),
            args=(make_prob(shape, requires_grad=requires_grad),),
            kwargs=kwargs,
        )

def sample_inputs_argwhere(op_info, device, dtype, requires_grad, **kwargs):
    yield SampleInput(torch.tensor([1, 0, 2, 0], dtype=dtype, device=device, requires_grad=requires_grad))
    mask = torch.tensor([[0, 1, 0, 1, 0],
                         [1, 1, 1, 1, 0],
                         [0, 0, 0, 1, 0],
                         [1, 0, 1, 1, 0],
                         [1, 0, 0, 1, 0]], dtype=torch.bool, device=device)
    t = make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad)
    t[mask] = 0
    yield SampleInput(t)

    t = make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad, noncontiguous=True)
    t[mask] = 0
    yield SampleInput(t)

    t = make_tensor((S, 0), dtype=dtype, device=device, requires_grad=requires_grad)
    yield SampleInput(t)

    yield SampleInput(torch.zeros((S,), dtype=dtype, device=device, requires_grad=requires_grad))
    yield SampleInput(make_tensor((), dtype=dtype, device=device, requires_grad=requires_grad))

def _generate_sample_shape_reduction():
    shapes = ((S,), (S, S), (S, S, S))
    reductions = ('none', 'mean', 'sum')
    for s, r in product(shapes, reductions):
        yield s, r

def sample_inputs_gaussian_nll_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    # Set low slightly above 0 so gradcheck doesn't accidentally dip below 0
    make_var = partial(make_tensor, low=0.1, device=device, dtype=dtype, requires_grad=requires_grad)

    def gen_shape(shape):
        yield shape
        # Broadcast
        yield (*shape[:-1], 1)
        yield shape[:-1]

    def gen_shape_kwargs():
        for s, r in _generate_sample_shape_reduction():
            for t_s, v_s in product(gen_shape(s), gen_shape(s)):
                yield _make_tensor(s), _make_tensor(t_s), make_var(v_s), dict(reduction=r)
                yield (
                    _make_tensor(s), _make_tensor(t_s), make_var(v_s),
                    dict(full=True, reduction=r)
                )
                yield (
                    _make_tensor(s), _make_tensor(t_s), make_var(v_s),
                    dict(eps=random.uniform(1e-6, 1e-3), reduction=r)
                )
                yield (
                    _make_tensor(s), _make_tensor(t_s), make_var(v_s),
                    dict(full=True, eps=random.uniform(1e-6, 1e-3), reduction=r)
                )

    for input, target, var, kwargs in gen_shape_kwargs():
        yield SampleInput(input, args=(target, var, ), kwargs=kwargs)

def _generate_sample_inputs_nn_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    for s, r in _generate_sample_shape_reduction():
        yield _make_tensor(s), _make_tensor(s), dict(reduction=r)

def sample_inputs_hinge_embedding_loss(op_info, device, dtype, requires_grad, **kwargs):
    for input, target, d in _generate_sample_inputs_nn_loss(op_info, device, dtype, requires_grad, **kwargs):
        # target should contain either 1 or -1 as per docs
        mask = torch.rand_like(target) > 0.5
        target[mask] = 1
        target[~mask] = -1
        d['margin'] = random.uniform(-9, 9)
        yield SampleInput(input, args=(target, ), kwargs=d)

    # scalar input and target.
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)
    yield SampleInput(_make_tensor(()), args=(_make_tensor(()), ))

def error_inputs_hinge_embedding_loss(op, device, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=torch.float32)
    # invalid reduction value
    yield ErrorInput(SampleInput(make_input(5, 4), args=(make_input(5, 4),), kwargs={'reduction': 'abc'}),
                     error_type=ValueError, error_regex='is not a valid value')

def reference_inputs_hinge_embedding_loss(op, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_hinge_embedding_loss(op, device, dtype, requires_grad, **kwargs)
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    for reduction in ('sum', 'mean', 'none'):
        if dtype.is_floating_point:  # only supports ints and floats
            # NaN propagation
            inp = make_input((10, ))
            inp[2] = float('nan')
            target = make_input((10, ))
            # target should contain either 1 or -1 as per docs
            mask = torch.rand_like(target) > 0.5
            target[mask] = -1
            target[~mask] = 1
            yield SampleInput(inp, args=(target,), kwargs={'reduction': reduction})

            # Inf Handling
            inp = make_input((10, ))
            inp[4] = float('inf')
            target = make_input((10, ))
            mask = torch.rand_like(target) > 0.5
            target[mask] = -1
            target[~mask] = 1
            yield SampleInput(inp, args=(target,), kwargs={'reduction': reduction})

        # Broadcasting
        inp = make_input((5, 5))
        target = make_input((1, 5))
        mask = torch.rand_like(target) > 0.5
        target[mask] = -1
        target[~mask] = 1
        yield SampleInput(inp, args=(target,), kwargs={'reduction': reduction})

def sample_inputs_huber_loss(op_info, device, dtype, requires_grad, **kwargs):
    for input, target, d in _generate_sample_inputs_nn_loss(op_info, device, dtype, requires_grad, **kwargs):
        d['delta'] = random.uniform(1e-3, 9)
        yield SampleInput(input, args=(target, ), kwargs=d)

def sample_inputs_poisson_nll_loss(op_info, device, dtype, requires_grad, **kwargs):
    _make_tensor = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    def gen_shape_kwargs():
        for s, r in _generate_sample_shape_reduction():
            for li in (True, False):
                for f in (True, False):
                    i1 = _make_tensor(s)
                    i2 = _make_tensor(s)
                    # For Poisson NLL Loss,
                    # target is assumed to be from
                    # Poisson Distribution which
                    # always has positive samples
                    t1 = _make_tensor(s, low=0)
                    t2 = _make_tensor(s, low=0)

                    if not li:
                        i1.abs_()
                        i2.abs_()
                    t1.abs_()
                    t2.abs_()

                    yield (
                        i1, t1,
                        dict(log_input=li, full=f, reduction=r)
                    )
                    yield (
                        i2, t2,
                        dict(log_input=li, full=f,
                             eps=random.uniform(1e-8, 1e-3),
                             reduction=r)
                    )

    for input, target, kwargs in gen_shape_kwargs():
        yield SampleInput(input, args=(target, ), kwargs=kwargs)

def sample_inputs_triplet_margin_loss(op_info, device, dtype, requires_grad, with_distance=False, **kwargs):
    make = partial(make_tensor, (S, M), device=device, dtype=dtype, requires_grad=requires_grad)

    kwargss = (
        *[dict(margin=margin) for margin in (1e-6, 1.0, 10.0)],
        dict(swap=True),
        *[dict(reduction=reduction) for reduction in ("mean", "sum", "none")],
    )

    for kwargs in kwargss:
        input = make()
        args = (make(), make())
        if with_distance:
            kwargs["distance_function"] = torch.nn.PairwiseDistance()
        yield SampleInput(input, args=args, kwargs=kwargs)

def sample_inputs_pairwise_distance(op_info, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shape = (3,)
    batched_shape = (2, *shape)
    shapes_and_kwargs = [
        (shape, None),
        (batched_shape, None),
        (shape, dict(keepdim=True)),
        (batched_shape, dict(keepdim=True)),
        (shape, dict(p=5.0)),
        (shape, dict(p=-1.0)),
        (shape, dict(eps=1.0)),
    ]

    return [
        SampleInput(make(shape), args=(make(shape),), kwargs=kwargs) for shape, kwargs in shapes_and_kwargs
    ]

def sample_inputs_pixel_shuffle(op_info, device, dtype, requires_grad, **kwargs):
    return [
        SampleInput(
            make_tensor((1, 9, 2, 2), device=device, dtype=dtype, requires_grad=requires_grad),
            kwargs=dict(upscale_factor=upscale_factor),
        )
        for upscale_factor in (1, 3)
    ]

def sample_inputs_pixel_unshuffle(op_info, device, dtype, requires_grad, **kwargs):
    return [
        SampleInput(
            make_tensor((1, 1, 6, 6), device=device, dtype=dtype, requires_grad=requires_grad),
            kwargs=dict(downscale_factor=downscale_factor),
        )
        for downscale_factor in (1, 3)
    ]

def sample_inputs_binary_cross_entropy(op_info, device, dtype, requires_grad, logits=False, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype)
    make_prob = partial(make, low=0, high=1)

    reductions = ("mean", "sum", "none")

    shapes_and_kwargs = [
        *[(shape, None) for shape in ((), (1,), (S,), (S, S), (S, S, S))],
        *[((S, S), dict(reduction=reduction)) for reduction in reductions],
        *[((S, S), dict(reduction=reduction, weight=make((S, S)))) for reduction in reductions],
    ]

    if logits:
        shapes_and_kwargs.extend(
            [((S, S), dict(reduction=reduction, pos_weight=make((S,), low=0))) for reduction in reductions]
        )

    for shape, kwargs in shapes_and_kwargs:
        yield SampleInput(
            (make if logits else make_prob)(shape, requires_grad=requires_grad),
            args=(make_prob(shape, requires_grad=requires_grad),),
            kwargs=kwargs,
        )

def sample_inputs_allclose(op_info, device, dtype, requires_grad, **kwargs):
    samples = []
    sample_shapes = [(), (S), (S, S, S)]
    atols = [1e-2, 1e-16]
    rtols = [1e-1, 0.5]
    eps = 1e-8
    for s, rtol, atol in product(sample_shapes, rtols, atols):
        # close sample
        t = make_tensor(s, device=device, dtype=dtype, requires_grad=requires_grad)
        close = (t + atol).detach().requires_grad_(requires_grad)
        close_sample = SampleInput(t, args=(close,), kwargs=dict(rtol=rtol, atol=atol))
        samples.append(close_sample)

        # random sample
        a = make_tensor(s, device=device, dtype=dtype, requires_grad=requires_grad)
        b = make_tensor(s, device=device, dtype=dtype, requires_grad=requires_grad)
        r_sample = SampleInput(a, args=(b,), kwargs=dict(rtol=rtol, atol=atol))
        samples.append(r_sample)

    return samples

def sample_inputs_l1_loss(op_info, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_loss(op_info, device, dtype, requires_grad, **kwargs)

    # In addition to the regular test cases, we add two for mixed floating point and complex inputs
    if dtype.is_complex:
        make = partial(make_tensor, (), device=device, requires_grad=requires_grad)
        yield SampleInput(make(dtype=dtype), args=(make(dtype=torch.double),))
        yield SampleInput(make(dtype=torch.double), args=(make(dtype=dtype),))

def sample_inputs_smooth_l1_loss(op_info, device, dtype, requires_grad, **kwargs):
    yield from sample_inputs_loss(op_info, device, dtype, requires_grad, **kwargs)

    make = partial(make_tensor, (S, S), device=device, dtype=dtype, requires_grad=requires_grad)

    # This test case always triggers the smooth condition, since absolute difference of input and target
    # is smaller than beta
    yield SampleInput(make(low=0, high=2), args=(make(low=-2, high=0),), kwargs=dict(beta=5))
    yield SampleInput(make(), args=(make(),), kwargs=dict(beta=0))

def sample_inputs_kl_div(op_info, device, dtype, requires_grad, **kwargs):
    make = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    shapes_and_reduction = [
        ((2,), "mean"),
        ((2, 3), "mean"),
        ((2, 3, 4), "mean"),
        ((2,), "none"),
        ((2,), "batchmean"),
        ((2,), "sum"),
    ]

    sample_inputs = []
    for (shape, reduction), log_target in itertools.product(shapes_and_reduction, (True, False)):
        # input should be log-probability, i.e. lie in (-inf, 0]
        input = make(shape, low=None, high=0)
        # target should be a probability by default, i.e. lie in [0, 1], and a log-probability if log_target is set,
        # i.e. lie in (-inf, 0]
        target = make(shape, low=None, high=0) if log_target else make(shape, low=0, high=1)
        sample_inputs.append(
            SampleInput(input, args=(target,), kwargs=dict(reduction=reduction, log_target=log_target))
        )
    return sample_inputs

def sample_inputs_pdist(op_info, device, dtype, requires_grad, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    yield from (SampleInput(make_input((n, m))) for n, m in itertools.product((1, S), repeat=2))
    yield from (SampleInput(make_input((S, S)), kwargs=dict(p=p)) for p in (0.0, 1.0, 2.0, 10.0, float("inf")))

def reference_pdist(input, p=2):
    pdist = scipy.spatial.distance.pdist
    if p == 0:
        output = pdist(input, "hamming") * input.shape[1]
    elif p == float("inf"):
        output = pdist(input, lambda x, y: np.abs(x - y).max())
    else:
        output = pdist(input, "minkowski", p=p)
    return output.astype(input.dtype)

def sample_inputs_diagflat(op_info, device, dtype, requires_grad, **kwargs):
    make_input = partial(make_tensor, device=device, dtype=dtype, requires_grad=requires_grad)

    return [
        SampleInput(make_input(())),
        SampleInput(make_input((2,))),
        SampleInput(make_input((2, 2))),
        SampleInput(make_input((2,)), kwargs=dict(offset=1)),
        SampleInput(make_input((2,)), kwargs=dict(offset=-1)),
    ]

def sample_inputs_max_unpool(op_info, device, dtype, requires_grad, **kwargs):
    unpool_name_to_pool_method_dict = {
        'nn.functional.max_unpool1d': torch.nn.functional.max_pool1d,
        'nn.functional.max_unpool2d': torch.nn.functional.max_pool2d,
        'nn.functional.max_unpool3d': torch.nn.functional.max_pool3d
    }

    unpool_name_to_dim = {
        'nn.functional.max_unpool1d': 1,
        'nn.functional.max_unpool2d': 2,
        'nn.functional.max_unpool3d': 3
    }

    unpool_to_pool_name_dict = dict((
        (k, f'nn.functional.{v.__name__}') for k, v in unpool_name_to_pool_method_dict.items()
    ))

    pool_dim = unpool_name_to_dim[op_info.name]
    pool_method = unpool_name_to_pool_method_dict[op_info.name]

    pool_op_info = copy.copy(op_info)
    pool_op_info.name = unpool_to_pool_name_dict[op_info.name]

    for sample in sample_inputs_max_pool(pool_op_info, device, dtype, requires_grad, **kwargs):
        # shapes (C, ...) do not work as of now,
        # see https://github.com/pytorch/pytorch/issues/68337
        # TODO: remove once the issue is resolved
        if sample.input.dim() != pool_dim + 2:
            continue

        # No dilation > 1 for max_unpool,
        # see https://github.com/pytorch/pytorch/issues/68420
        if sample.kwargs['dilation'] != 1:
            continue

        # Can't unpool without indices
        if sample.kwargs['return_indices']:
            pool, indices = pool_method(sample.input, **sample.kwargs)
            # arg has to be a leaf
            arg = pool.detach().requires_grad_(requires_grad)
            sample_kwargs = {
                'kernel_size': sample.kwargs['kernel_size'],
                'stride': sample.kwargs['stride'],
                'padding': sample.kwargs['padding'],
                # output_size could be None but we specify it explicitly
                # to compensate for the information lose in pool due
                # to the floor/ceil operation used to compute the shapes
                'output_size': sample.input.size()
            }

            yield SampleInput(arg, args=(indices,), kwargs=sample_kwargs)

def sample_inputs_max_unpool_grad(op_info, device, dtype, requires_grad, **kwargs):
    for sample in sample_inputs_max_unpool(op_info, device, dtype, requires_grad, **kwargs):
        indices = sample.args[0]
        # The samples for max_unpool are generated with max_pool.
        # It could be that a single element from the max_pool's
        # input is mapped to several locations in its output.
        # This situation leads to failed gradchecks because
        # the finite difference algorithm perturbes the elements
        # of the output one by one, and not in classes of
        # equivalences determined by whether two elements
        # in the output are coming from the same location in the
        # input (simply put, they have the same corresponding index).
        # So, there are two ways to resolve this issue:
        # 1. Extract a pertubation for one element and apply it all
        #    the elements from the same equivalence class, or
        # 2. Make sure that the equivalence classes are all singletons,
        # i.e. the index tensor has to be comprised of only unique
        # indices.
        # Here we go with the solution 2, the easiest of all.
        if indices.unique().numel() == indices.numel():
            yield sample

foreach_unary_op_db: List[OpInfo] = [
    ForeachFuncInfo('exp'),
    ForeachFuncInfo('acos'),
    ForeachFuncInfo('asin'),
    ForeachFuncInfo('atan'),
    ForeachFuncInfo('cos'),
    ForeachFuncInfo('cosh'),
    ForeachFuncInfo('log'),
    ForeachFuncInfo('log10'),
    ForeachFuncInfo('log2'),
    ForeachFuncInfo('tan'),
    ForeachFuncInfo('tanh'),
    ForeachFuncInfo('sin'),
    ForeachFuncInfo('sinh'),

    ForeachFuncInfo(
        'neg',
        dtypes=all_types_and_complex(),
        dtypesIfCUDA=all_types_and_complex(),
        sample_inputs_func=sample_inputs_foreach,
    ),

    ForeachFuncInfo(
        'sqrt',
        dtypes=floating_and_complex_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_and_complex_types_and(torch.half),
    ),

    ForeachFuncInfo(
        'ceil',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'erf',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'erfc',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'expm1',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'floor',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'log1p',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half),
    ),

    ForeachFuncInfo(
        'round',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'frac',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'reciprocal',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half),
    ),

    ForeachFuncInfo(
        'sigmoid',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half),
    ),

    ForeachFuncInfo(
        'trunc',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
    ),

    ForeachFuncInfo(
        'abs',
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.half),
        dtypesIfCUDA=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
    ),
]

foreach_binary_op_db: List[OpInfo] = [
    ForeachFuncInfo(
        "add",
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        supports_alpha_param=True,
    ),
    ForeachFuncInfo(
        "sub",
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        supports_alpha_param=True,
    ),
    ForeachFuncInfo(
        "mul",
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        skips=(
            # Ref: https://github.com/pytorch/pytorch/issues/77946
            DecorateInfo(unittest.skip("Unable to reproduce failure locally"), "TestForeach",
                         "test_binary_op_scalarlist_fastpath",
                         device_type='cuda', dtypes=(torch.float16,)),
        )
    ),
    ForeachFuncInfo(
        "div",
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
        skips=(
            # Ref: https://github.com/pytorch/pytorch/issues/77946
            DecorateInfo(unittest.skip("Unable to reproduce failure locally"), "TestForeach",
                         "test_binary_op_scalarlist_fastpath",
                         device_type='cuda', dtypes=(torch.float16,)),
        )
    ),
]

foreach_pointwise_op_db: List[ForeachFuncInfo] = [
    ForeachFuncInfo(
        "addcmul",
        dtypes=all_types_and_complex(),
        dtypesIfCUDA=all_types_and_complex_and(torch.half, torch.bfloat16),
    ),
    ForeachFuncInfo(
        "addcdiv",
        dtypes=all_types_and_complex(),
        dtypesIfCUDA=all_types_and_complex_and(torch.half, torch.bfloat16),
    ),
]

foreach_minmax_op_db: List[ForeachFuncInfo] = [
    ForeachFuncInfo(
        "maximum",
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bool),
    ),
    ForeachFuncInfo(
        "minimum",
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bool),
    ),
]

foreach_reduce_op_db: List[ForeachFuncInfo] = [
    ForeachFuncInfo(
        "norm",
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.bfloat16),
    ),
]

def reference_sign(x):
    if x.dtype == np.bool_:
        # `np.sign` doesn't support `bool`.
        # >>> np.sign(True)
        # ufunc 'sign' did not contain a loop
        # with signature matching types dtype('bool') -> dtype('bool')
        return np.sign(x, dtype=np.uint8).astype(np.bool_)
    return np.sign(x)


def reference_sgn(x):
    # NumPy doesn't have an equivalent to `torch.sgn` when the dtype is complex.
    # For complex inputs, `np.sign` returns sign(x.real) + 0j if x.real != 0 else sign(x.imag) + 0j.
    # while `torch.sgn` returns, 0 if abs(input) == 0 else input/abs(input)
    if x.dtype not in [np.complex64, np.complex128]:
        return reference_sign(x)

    out = (x / np.abs(x))
    if out.ndim == 0:
        # Handle x == 0 case
        if (x == 0):
            # Can't assign to np.complex object
            # So make a new one.
            return np.array(complex(0, 0), dtype=x.dtype)
        return out

    # Handle x == 0 case
    mask = (x == 0)
    out[mask] = complex(0, 0)
    return out


def reference_sigmoid(x):
    # 'scipy.special.expit' not supported for the input types
    if x.dtype in [np.complex64, np.complex128]:
        return (1 / (1 + np.exp(-x)))
    return scipy.special.expit(x)


def reference_logsigmoid(x):
    return np.where(
        x < 0,
        x - np.log1p(np.exp(x)),
        -np.log1p(np.exp(-x)))


def reference_hardsigmoid(x):
    intermediate = x / 6 + 0.5
    y = np.clip(intermediate, 0, None)
    return np.where(y > 1, 1, y).astype(x.dtype)


def reference_lgamma(x):
    # scipy.special.gammaln returns `-inf` when input is `-inf`.
    # While Pytorch, C and C++, all return `inf` when input is `-inf`.
    # Reference:
    # https://en.cppreference.com/w/cpp/numeric/math/lgamma
    # https://en.cppreference.com/w/c/numeric/math/lgamma

    # To handle the above discrepancy,
    # we replace -inf with inf so values
    # that were originally -inf map to inf as expected
    if x.dtype.kind == 'f':
        x = np.where(x == float('-inf'), np.array(float('inf'), dtype=x.dtype), x)

    out = scipy.special.gammaln(x)

    if x.dtype == np.float16:
        # `scipy.special.gammaln` returns output of float32 when input is float16,
        # while `torch.lgamma` preserves `float16`. But due to smaller range of float16,
        # Pytorch version outputs `inf` while SciPy returns finite values.
        out = out.astype(np.float16)

    return out

def reference_polygamma(x, n):
    # WEIRD `scipy.special.polygamma` behavior
    # >>> scipy.special.polygamma(0, np.array(501, dtype=np.float32)).dtype
    # dtype('float64')
    # >>> scipy.special.polygamma(0, np.array([501], dtype=np.float32)).dtype
    # dtype('float32')
    #
    # Thus we cast output to the default torch dtype or preserve double
    result_dtype = torch_to_numpy_dtype_dict[torch.get_default_dtype()]
    if x.dtype == np.double:
        result_dtype = np.double
    return scipy.special.polygamma(n, x).astype(result_dtype)


def reference_mvlgamma(x, d):
    if x.dtype == np.float16:
        return scipy.special.multigammaln(x, d).astype(np.float16)

    return scipy.special.multigammaln(x, d)

def reference_softplus(input, beta=1, threshold=20):
    non_linear = input * beta <= threshold
    output = input.copy()
    output[non_linear] = np.log(1 + np.exp(beta * input[non_linear])) / beta
    return output

def reference_gelu(X, *, approximate='none'):
    def _gelu_ref(X):
        return X * stats.norm.cdf(X)

    def _tanh_gelu_ref(X):
        M_SQRT_2_PI = math.sqrt(2 / math.pi)
        Z = M_SQRT_2_PI * (X + 0.044715 * np.power(X, 3.0))
        return 0.5 * X * (1.0 + np.tanh(Z))

    if approximate == 'tanh':
        return _tanh_gelu_ref(X)
    else:
        return _gelu_ref(X)


def reference_one_hot(a: np.ndarray, num_classes: int = -1) -> np.ndarray:
    if num_classes == -1:
        num_classes = int(np.amax(a) + 1)

    idcs = a.reshape(-1) + np.arange(0, a.size, dtype=np.int64) * num_classes
    one_hot = np.zeros((a.size, num_classes), dtype=a.dtype)
    np.put(one_hot, idcs, 1)
    return one_hot.reshape(*a.shape, -1)


def reference_mse_loss(input, target, reduction="mean"):
    se = (input - target) ** 2
    if reduction == "mean":
        return np.mean(se)
    elif reduction == "sum":
        return np.sum(se)
    else:  # reduction == "none"
        return se


def wrapper_set_seed(op, *args, **kwargs):
    """Wrapper to set seed manually for some functions like dropout
    See: https://github.com/pytorch/pytorch/pull/62315#issuecomment-896143189 for more details.
    """
    with freeze_rng_state():
        torch.manual_seed(42)
        return op(*args, **kwargs)


def reference_layer_norm(inp: np.ndarray, normalized_shape: Tuple[int], weight=None, bias=None, eps=1e-5):
    return reference_native_layer_norm(inp, normalized_shape, weight, bias, eps)[0]


def reference_native_layer_norm(inp: np.ndarray, normalized_shape: Tuple[int], weight, bias, eps):
    feature_size = np.prod(normalized_shape)
    inp_view = inp.reshape(-1, feature_size)  # type: ignore[call-overload]
    mean = inp_view.mean(axis=-1, keepdims=True)
    var = inp_view.var(axis=-1, ddof=0, keepdims=True)
    Y = (inp_view - mean) / np.sqrt(var + eps)
    if weight is None and bias is not None:
        Y = Y + bias.reshape(-1)
    elif weight is not None and bias is None:
        Y = Y * weight.reshape(-1)
    elif weight is not None and bias is not None:
        Y = Y * weight.reshape(-1) + bias.reshape(-1)
    axis = inp.ndim - len(normalized_shape)
    stat_shape = inp.shape[:axis] + (1,) * len(normalized_shape)
    return Y.reshape(*inp.shape), mean.reshape(stat_shape), (1.0 / np.sqrt(var + eps)).reshape(stat_shape)


def reference_group_norm(inp: np.ndarray, num_groups: int, weight=None, bias=None, eps=1e-5):
    inp_view = inp
    if np.prod(inp.shape) != 0:
        inp_view = inp.reshape((inp.shape[0], num_groups, -1))
    mean = inp_view.mean(axis=-1, keepdims=True)
    var = inp_view.var(axis=-1, ddof=0, keepdims=True)
    Y = (inp_view - mean) / np.sqrt(var + eps)
    Y = Y.reshape(inp.shape)
    if weight is not None:
        # weight is a vector of length equal to the channel
        if len(Y.shape) > 2:
            weight = np.tile(np.expand_dims(weight, 1), [1] + list(inp.shape[2:]))
        Y = Y * weight
    if bias is not None:
        # bias is a vector of length equal to the channel
        if len(Y.shape) > 2:
            bias = np.tile(np.expand_dims(bias, 1), [1] + list(inp.shape[2:]))
        Y = Y + bias
    return Y


# using a custom reference function since numpy only has a string side arg (instead of right and side) and doesn't
# have an out_int32 arg. Additionally, numpy doesn't support searchsorted with ND arrays, so this splits those into
# stacked 1D cases
def reference_searchsorted(sorted_sequence, boundary, out_int32=False, right=False, side='left', sorter=None):
    side = 'right' if (right or side == 'right') else 'left'
    if len(sorted_sequence.shape) == 1 :
        ret = np.searchsorted(sorted_sequence, boundary, side=side, sorter=sorter)
        return ret.astype(np.int32) if out_int32 else ret
    elif sorted_sequence.shape[0] == 0:
        if sorter is not None:
            sorter = sorter.flatten()
        ret = np.searchsorted(sorted_sequence.flatten(), boundary.flatten(), side=side, sorter=sorter)
        ret = ret.astype(np.int32) if out_int32 else ret
        return ret.reshape(boundary.shape)
    else:
        # numpy searchsorted only supports 1D inputs so we split up ND inputs
        orig_shape = boundary.shape
        num_splits = np.prod(sorted_sequence.shape[:-1])
        splits = range(0, num_splits)
        sorted_sequence, boundary = sorted_sequence.reshape(num_splits, -1), boundary.reshape(num_splits, -1)
        if sorter is not None:
            sorter = sorter.reshape(num_splits, -1)

        split_sequence = [sorted_sequence[i] for i in splits]
        split_boundary = [boundary[i] for i in splits]
        split_sorter = [sorter[i] if (sorter is not None) else None for i in splits]

        split_ret = [np.searchsorted(s_seq, b, side=side, sorter=s_sort)
                     for (s_seq, b, s_sort) in zip(split_sequence, split_boundary, split_sorter)]
        split_ret = [i.astype(np.int32) for i in split_ret] if out_int32 else split_ret
        return np.stack(split_ret).reshape(orig_shape)


def gradcheck_wrapper_hermitian_input(op, input, *args, **kwargs):
    """Gradcheck wrapper for functions that take Hermitian matrices as input.

    They require a modified function because the finite-difference algorithm
    for calculating derivatives does not preserve the Hermitian property of the input.
    """
    return op(input + input.mH, *args, **kwargs)


def gradcheck_wrapper_triangular_input(op, *args, upper=False, idx=0, **kwargs):
    """Gradcheck wrapper for functions that take lower or upper triangular matrices as input.

    They require a modified function because the finite-difference algorithm
    for calculating derivatives does not preserve the triangular property of the input.
    `idx` is used to specific which `args[idx]` is to be triangularized.
    """
    triangular_arg = args[idx].triu() if upper else args[idx].tril()
    return op(*args[:idx], triangular_arg, *args[idx + 1:], upper, **kwargs)


def gradcheck_wrapper_triangular_input_real_positive_diagonal(op, *args, upper=False, idx=0, **kwargs):
    """Gradcheck wrapper for functions that take lower/upper triangular matrices
    with real and positive diagonals, for example, cholesky-like operations.
    """
    arg = args[idx]
    arg_diag = arg.diagonal(0, -2, -1)
    arg_diag_embed = torch.diag_embed(arg_diag)
    id_diag_tensor = torch.ones_like(arg_diag)
    id_tensor = torch.diag_embed(id_diag_tensor)
    # new_arg = arg - diag(arg) + I
    new_arg = arg - arg_diag_embed + id_tensor
    return gradcheck_wrapper_triangular_input(
        op, *args[:idx], new_arg, *args[idx + 1:],
        upper=upper, idx=idx, **kwargs
    )


def gradcheck_wrapper_masked_operation(op, input, *args, **kwargs):
    """Gradcheck wrapper for masked operations.

    When mask is specified, replaces masked-out elements with zeros.

    Use for operations that produce non-finite masked-out elements,
    for instance, for minimum and maximum reductions.
    """
    output = op(input, *args, **kwargs)
    mask = kwargs.get('mask')
    if mask is not None:
        output_mask = torch._masked._output_mask(op, input, *args, **kwargs)
        output = torch.where(output_mask, output, output.new_zeros([]))
    return output


def gradcheck_wrapper_masked_pointwise_operation(op, input, *args, **kwargs):
    """Gradcheck wrapper for masked pointwise operations. Assumes that the result
    will be masked iff both tensors are masked at a specific index

    When mask is specified, replaces masked-out elements with zeros.

    Use for operations that produce non-finite masked-out elements,
    for instance, for minimum and maximum reductions.
    """
    output = op(input, *args, **kwargs)
    input_mask = kwargs.get('input_mask')
    other_mask = kwargs.get('other_mask')
    if input_mask is not None and other_mask is not None:
        combined_mask = torch.logical_and(input_mask, other_mask)
        new_kwargs = dict(mask=combined_mask, **kwargs)
        output_mask = torch._masked._input_mask(input, *args, **new_kwargs)
        output = torch.where(output_mask, output, output.new_zeros([]))
    return output


def reference_reduction_numpy(f, supports_keepdims=True):
    """Wraps a NumPy reduction operator.

    The wrapper function will forward dim, keepdim, mask, and identity
    kwargs to the wrapped function as the NumPy equivalent axis,
    keepdims, where, and initiak kwargs, respectively.

    Args:
        f: NumPy reduction operator to wrap
        supports_keepdims (bool, optional): Whether the NumPy operator accepts
            keepdims parameter. If it does not, the wrapper will manually unsqueeze
            the reduced dimensions if it was called with keepdim=True. Defaults to True.

    Returns:
        Wrapped function

    """
    @wraps(f)
    def wrapper(x: np.ndarray, *args, **kwargs):
        # Copy keys into a set
        keys = set(kwargs.keys())

        dim = kwargs.pop('dim', None)
        keepdim = kwargs.pop('keepdim', False)

        if 'dim' in keys:
            dim = tuple(dim) if isinstance(dim, Sequence) else dim

            # NumPy reductions don't accept dim=0 for scalar inputs
            # so we convert it to None if and only if dim is equivalent
            if x.ndim == 0 and dim in {0, -1, (0,), (-1,)}:
                kwargs['axis'] = None
            else:
                kwargs['axis'] = dim

        if 'keepdim' in keys and supports_keepdims:
            kwargs['keepdims'] = keepdim

        if 'mask' in keys:
            mask = kwargs.pop('mask')
            if mask is not None:
                assert mask.layout == torch.strided
                kwargs['where'] = mask.cpu().numpy()

        if 'identity' in keys:
            identity = kwargs.pop('identity')
            if identity is not None:
                if identity.dtype is torch.bfloat16:
                    identity = identity.cpu().to(torch.float32)
                else:
                    identity = identity.cpu()
                kwargs['initial'] = identity.numpy()

        if 'unbiased' in keys:
            unbiased = kwargs.pop('unbiased')
            if unbiased is not None:
                kwargs['ddof'] = int(unbiased)

        result = f(x, *args, **kwargs)

        # Unsqueeze reduced dimensions if NumPy does not support keepdims
        if keepdim and not supports_keepdims and x.ndim > 0:
            dim = list(range(x.ndim)) if dim is None else dim
            result = np.expand_dims(result, dim)

        return result

    return wrapper

def loss_reference_reduction_wrapper(fn):
    def wrapper(input, target, *, size_average=None, reduce=None, reduction="mean", **other_kwargs):
        if size_average is not None or reduce is not None:
            raise RuntimeError(
                "The keyword arguments 'size_average' and 'reduce' are deprecated and not supported by this wrapper"
            )
        output = fn(input, target, **other_kwargs)
        if reduction == "mean":
            return np.mean(output)
        elif reduction == "sum":
            return np.sum(output)
        else:  # reduction == "none"
            return output

    return wrapper

@loss_reference_reduction_wrapper
def reference_smooth_l1_loss(input, target, beta=1.0):
    diff = input - target
    abs_diff = np.abs(diff)
    above_threshold = abs_diff >= beta

    loss = np.empty_like(input)
    loss[above_threshold] = abs_diff[above_threshold] - 0.5 * beta
    loss[~above_threshold] = diff[~above_threshold] ** 2 / (2 * beta)

    return loss

def reference_std_var(f):
    """Forwards unbiased/correction kwargs as NumPy's equivalent ddof"""
    g = reference_reduction_numpy(f)

    @wraps(g)
    def wrapper(x: np.ndarray, *args, **kwargs):
        assert not ('unbiased' in kwargs and 'correction' in kwargs)

        if 'unbiased' in kwargs:
            kwargs['ddof'] = int(kwargs.pop('unbiased'))
        elif 'correction' in kwargs:
            kwargs['ddof'] = kwargs.pop('correction')

        return g(x, *args, **kwargs)

    return wrapper

def generate_std_var_kwargs(t: torch.Tensor, **kwargs):
    """Generates unbiased/correction kwargs for std/var operators"""
    yield ((), {'unbiased': True})
    yield ((), {'unbiased': False})

    # Currently, calling std with correction is only enabled when
    # both dim and keepdim are provided.
    if 'dim' in kwargs and 'keepdim' in kwargs:
        yield ((), {'correction': 0})
        yield ((), {'correction': 1})

        numel = torch.tensor(t.shape)[kwargs.get('dim')].prod()
        yield ((), {'correction': numel // 2})

def error_inputs_mean(op_info, device, **kwargs):
    err_msg1 = (r"mean\(\): could not infer output dtype. "
                r"Input dtype must be either a floating point or complex dtype. "
                r"Got: Long")
    si1 = SampleInput(
        make_tensor((3, 4, 5), dtype=torch.int64, device=device),
        args=([],))

    err_msg2 = (r"mean\(\): could not infer output dtype. "
                r"Optional dtype must be either a floating point or complex dtype. "
                r"Got: Long")
    si2 = SampleInput(
        make_tensor((3, 4, 5), dtype=torch.float32, device=device),
        args=([],),
        kwargs={"dtype": torch.int64})

    err_msg3 = "Expected out tensor to have dtype double, but got float instead"
    si3 = SampleInput(
        make_tensor((3, 4, 5), dtype=torch.int64, device=device),
        args=([],),
        kwargs={
            "dtype": torch.float64,
            "out": make_tensor([], dtype=torch.float32, device=device),
        })

    return (ErrorInput(si1, error_regex=err_msg1),
            ErrorInput(si2, error_regex=err_msg2),
            ErrorInput(si3, error_regex=err_msg3))

# numpy implementation of torch.flatten
# unfortunately there's no np.flatten. we figure out the desired shape and call np.reshape
def reference_flatten(input, start_dim=0, end_dim=-1):
    in_shape = input.shape
    in_rank = len(in_shape)
    for d in start_dim, end_dim:
        if not((in_rank == 0 and d in (-1, 0)) or -in_rank <= d < in_rank):
            raise IndexError(f"Dimension out of range (expected to be in range of [{-in_rank}, {in_rank-1}], but got {d}")
    end_dim = end_dim if end_dim >= 0 else in_rank + end_dim
    start_dim = start_dim if start_dim >= 0 else in_rank + start_dim
    if in_rank == 0:
        end_dim = start_dim
    if end_dim < start_dim:
        raise RuntimeError("flatten() has invalid args: start_dim cannot come after end_dim")
    flatten_bit_dim = functools.reduce(operator.mul, in_shape[start_dim:end_dim + 1], 1)
    out_shape = in_shape[:start_dim] + (flatten_bit_dim,) + in_shape[end_dim + 1:]
    return np.reshape(input, out_shape)

# Operator database (sorted alphabetically)
op_db: List[OpInfo] = [
    UnaryUfuncInfo('abs',
                   aliases=('absolute', ),
                   ref=np.abs,
                   dtypes=all_types_and_complex_and(torch.half, torch.bfloat16, torch.chalf),
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
                   skips=(
                       DecorateInfo(unittest.skip("In-place abs not supported for complex tensors"), 'TestGradients',
                                    'test_inplace_grad', dtypes=(torch.cdouble,)),
                       DecorateInfo(unittest.skip("In-place abs not supported for complex tensors"), 'TestGradients',
                                    'test_inplace_gradgrad', dtypes=(torch.cdouble,)),
                       DecorateInfo(unittest.skip("In-place abs not supported for complex tensors"), 'TestGradients',
                                    'test_inplace_forward_mode_AD', dtypes=(torch.cdouble,)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat]),
                       # Reference: https://github.com/pytorch/pytorch/issues/49224
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=[torch.int8], active_if=TEST_WITH_ASAN),
                       # TODO: Fix test_out_arg_all_dtypes as torch.empty_like(expected_output) where expected_output=op(input)
                       # We can break the logic of the loop over all possible types but it is OK.
                       # https://github.com/pytorch/pytorch/blob/master/test/test_unary_ufuncs.py#L440-L449
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_out_arg_all_dtypes',
                                    dtypes=[torch.cfloat, torch.cdouble]),
                   ),
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_forward_ad=True),
    # NOTE: CPU complex acos produces incorrect outputs (https://github.com/pytorch/pytorch/issues/42952)
    UnaryUfuncInfo('acos',
                   aliases=('arccos', ),
                   ref=np.arccos,
                   domain=(-1, 1),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.bfloat16: 1e-1,
                                                  torch.complex64: 1e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                                    device_type='cuda', dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       # Failing with wrong imaginary sign on at least some Windows jobs
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       # Failing with wrong imaginary sign on at least some Windows jobs
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_grad',
                                    dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_method_grad',
                                    dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_inplace_grad',
                                    dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_forward_mode_AD',
                                    dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_inplace_forward_mode_AD',
                                    dtypes=[torch.cdouble], active_if=IS_WINDOWS),)),
    # NOTE: the derivative for inplace acosh is not implemented
    UnaryUfuncInfo('acosh',
                   aliases=('arccosh', ),
                   ref=np.arccosh,
                   domain=(1, None),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   decorators=(precisionOverride({torch.bfloat16: 5e-2}),),
                   supports_inplace_autograd=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                                    device_type='cuda', dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       # Failing with wrong imaginary sign on at least some Windows jobs
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                   ),
                   # acosh is not defined at x < 1 (real)
                   reference_numerics_filter=NumericsFilter(
                       condition=lambda x: (x < 1 if not x.is_complex() else torch.zeros_like(x, dtype=torch.bool)),
                       safe_val=2)),
    BinaryUfuncInfo('add',
                    # NumPy has no builtin reference for the alpha kwarg, but it is easy enough to emulate
                    ref=lambda input, other, *, alpha=1: np.add(input, other) if alpha == 1 \
                    else np.add(input, np.multiply(alpha, other)),
                    dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16,
                                                     torch.float16, torch.chalf),
                    assert_autodiffed=True,
                    sample_inputs_func=sample_inputs_add_sub,
                    supports_fwgrad_bwgrad=True,
                    supports_forward_ad=True,
                    supports_two_python_scalars=True,
                    decorators=(
                        DecorateInfo(
                            toleranceOverride({torch.chalf: tol(atol=1e-2, rtol=0)}),
                            'TestBinaryUfuncs', 'test_reference_numerics'),
                    ),
                    skips=(
                        # boolean alpha not handled properly
                        DecorateInfo(unittest.expectedFailure,
                                     'TestCudaFuserOpInfo',
                                     'test_nvfuser_correctness',
                                     dtypes=(torch.bool,)),
                        # boolean alpha not handled properly
                        DecorateInfo(unittest.expectedFailure,
                                     'TestNNCOpInfo',
                                     'test_nnc_correctness',
                                     dtypes=(torch.bool,)),
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestCommon',
                                     'test_numpy_refs',
                                     dtypes=(torch.complex128,)),
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics_extremal_values',
                                     dtypes=(torch.complex64, torch.complex128)),
                    )),
    OpInfo('arange',
           dtypes=all_types_and(torch.bfloat16, torch.float16),
           supports_out=True,
           supports_autograd=False,
           is_factory_function=True,
           error_inputs_func=error_inputs_arange,
           sample_inputs_func=sample_inputs_arange,
           skips=(
               # https://github.com/pytorch/pytorch/issues/81774
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),

               # Tests that assume input is a tensor or sequence of tensors
               DecorateInfo(unittest.expectedFailure, "TestCommon", "test_noncontiguous_samples"),
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),

               # Lazy tensor failures
               DecorateInfo(unittest.expectedFailure, 'TestLazyOpInfo', 'test_dispatched_to_lazy'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestLazyOpInfo', 'test_correctness'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestLazyOpInfo', 'test_correctness_with_reusing_ir'),

               # Exception raised from analyzeImpl at ../torch/csrc/jit/ir/alias_analysis.cpp:608
               # We don't have an op for aten::arange but it isn't a special case.
               # Argument types: bool, bool, bool, int, int, Device, boo
               DecorateInfo(unittest.expectedFailure, 'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
               DecorateInfo(unittest.expectedFailure, 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness'),

               # Captured graph does not contain aten::arange (succeeds on complex!)
               # g: graph():
               #   %25 : Long(1, strides=[1], requires_grad=0, device=cpu) = prim::Constant[value={1}]()
               #   return (%25)
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),

               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
           )),
    BinaryUfuncInfo('clamp_max',
                    ref=_clamp_max_numpy,
                    dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
                    supports_forward_ad=True,
                    supports_rhs_python_scalar=False,
                    supports_fwgrad_bwgrad=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=False),
                    skips=(
                        # RuntimeError: "max_elementwise_cuda" not implemented for 'ComplexFloat'
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion',
                                     device_type='cuda'),
                        # dispatch to lazy test failed
                        DecorateInfo(unittest.expectedFailure, 'TestLazyOpInfo', 'test_dispatched_to_lazy'),
                        # test error disabled since rhs non-tensor python scalar is supported
                        DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_errors'),
                    )),
    BinaryUfuncInfo('clamp_min',
                    ref=_clamp_min_numpy,
                    dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
                    supports_forward_ad=True,
                    supports_rhs_python_scalar=False,
                    supports_fwgrad_bwgrad=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=False),
                    skips=(
                        # RuntimeError: "min_elementwise_cuda" not implemented for 'ComplexFloat'
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion',
                                     device_type='cuda'),
                        # dispatch to lazy test failed
                        DecorateInfo(unittest.expectedFailure, 'TestLazyOpInfo', 'test_dispatched_to_lazy'),
                        # test error disabled since rhs non-tensor python scalar is supported
                        DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_errors'),
                    )),
    BinaryUfuncInfo('mul',
                    aliases=('multiply',),
                    dtypes=all_types_and_complex_and(torch.chalf, torch.float16, torch.bfloat16, torch.bool),
                    assert_autodiffed=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True),
    BinaryUfuncInfo('sub',
                    # NumPy has no builtin reference for the alpha kwarg, but it is easy enough to emulate
                    ref=lambda input, other, *, alpha=1: np.subtract(input, np.multiply(alpha, other)),
                    aliases=('subtract',),
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.chalf),
                    assert_autodiffed=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    sample_inputs_func=sample_inputs_add_sub,
                    supports_two_python_scalars=True,
                    decorators=(
                        DecorateInfo(
                            toleranceOverride({torch.float16: tol(atol=1e-2, rtol=0)}),
                            'TestBinaryUfuncs', 'test_reference_numerics'),
                        DecorateInfo(
                            toleranceOverride({torch.chalf: tol(atol=1e-2, rtol=0)}),
                            'TestCommon', 'test_complex_half_reference_testing', device_type='cpu'),
                        DecorateInfo(
                            toleranceOverride({torch.chalf: tol(atol=5e-3, rtol=0)}),
                            'TestDecomp', 'test_comprehensive', device_type='cpu'),
                        DecorateInfo(
                            toleranceOverride({torch.chalf: tol(atol=5e-3, rtol=0)}),
                            'TestDecomp', 'test_quick', device_type='cpu'),
                    ),
                    skips=(
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics',
                                     dtypes=(torch.uint8,)),
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics_small_values',
                                     dtypes=(torch.uint8,)),
                    )),
    OpInfo('addmm',
           # This addmm OpInfo is for when alpha and beta are not both equal to 1.
           # alpha=beta=1 is tested in the following opinfo, because that special case will
           # trigger addmm being decomposed by a jit pass.
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfROCM=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16] if CUDA11OrLater else []),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_addmm,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('addmm',
           # When alpha=beta=1 as compile-time constants, JIT will decompose addmm into mm and add.
           variant_test_name='decomposed',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16] if(CUDA11OrLater or TEST_WITH_ROCM) else []),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           autodiff_nonfusible_nodes=['aten::add', 'aten::mm'],
           sample_inputs_func=partial(sample_inputs_addmm, alpha=1, beta=1),
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
               # https://github.com/pytorch/pytorch/issues/71784
               DecorateInfo(unittest.skip('Skipped!'), 'TestNNCOpInfo', 'test_nnc_correctness',
                            device_type='cpu', dtypes=(torch.float16,)),
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_correctness', dtypes=(torch.float16,)),
           )),
    OpInfo('addmv',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.complex64, torch.complex128,
                                           *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_addmv),
    OpInfo('addbmm',
           ref=lambda M, batch1, batch2, beta=1, alpha=1: np.add(np.multiply(np.asarray(beta, dtype=M.dtype), M),
                                                                 np.multiply(np.asarray(alpha, dtype=batch1.dtype),
                                                                             np.sum(np.matmul(batch1, batch2), axis=0))),
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16]
                                                       if (SM53OrLater and CUDA11OrLater) or TEST_WITH_ROCM else []),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1.3e-05, rtol=1.3e-05),
                                      torch.complex64: tol(atol=1e-05, rtol=1.2e-03)}),
                   'TestCommon', 'test_numpy_refs')],
           skips=(
               # NVIDIA only assures that bfloat16 is supported by bmm if SM >= 5.3
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes', device_type='cuda', active_if=not SM53OrLater),
               # addbmm does not correctly warn when resizing out= inputs
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
               # https://github.com/pytorch/pytorch/issues/55907
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
           ),
           sample_inputs_func=sample_inputs_addbmm),
    OpInfo('baddbmm',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.complex64, torch.complex128,
                                           *[torch.bfloat16] if CUDA11OrLater or TEST_WITH_ROCM else []),
           backward_dtypesIfCUDA=floating_types_and(torch.float16,
                                                    *[torch.bfloat16] if SM53OrLater or TEST_WITH_ROCM else [],
                                                    torch.complex64, torch.complex128),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.complex64: tol(atol=1e-05, rtol=1.2e-03)}),
                   'TestCommon', 'test_variant_consistency_eager', device_type='cuda'),
               DecorateInfo(
                   toleranceOverride({torch.complex64: tol(atol=1e-05, rtol=1.2e-03)}),
                   'TestMathBits', 'test_conj_view', device_type='cuda')],
           sample_inputs_func=sample_inputs_baddbmm,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('dot',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           assert_autodiffed=True,
           sample_inputs_func=sample_inputs_dot_vdot,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('vdot',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_dot_vdot,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('bmm',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16]
                                                       if (SM53OrLater and CUDA11OrLater) or TEST_WITH_ROCM else []),
           assert_autodiffed=True,
           assert_jit_shape_analysis=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # NVIDIA only assures that bfloat16 is supported by bmm if SM >= 5.3
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes', device_type='cuda', active_if=not SM53OrLater),
           ),
           sample_inputs_func=sample_inputs_bmm),
    OpInfo('mv',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_mv),
    OpInfo('addr',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
           backward_dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
           backward_dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, *[torch.bfloat16]
                                                           if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           # Reference: https://github.com/pytorch/pytorch/issues/50747
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Reference: https://github.com/pytorch/pytorch/issues/50747
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16)),
           ),
           sample_inputs_func=sample_inputs_addr,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    OpInfo('addcmul',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # TODO: update sample inputs with for_inplace_variant kwarg to support this test
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager'),
               # 76047
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness',
                            dtypes=(torch.int8, torch.int16, torch.int32, torch.int64)),
           ),
           sample_inputs_func=sample_inputs_addcmul_addcdiv),
    OpInfo('addcdiv',
           dtypes=floating_and_complex_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # TODO: update sample inputs with for_inplace_variant kwarg to support this test
               DecorateInfo(unittest.expectedFailure,
                            'TestCommon',
                            'test_variant_consistency_eager'),
           ),
           sample_inputs_func=sample_inputs_addcmul_addcdiv),
    UnaryUfuncInfo('asin',
                   aliases=('arcsin', ),
                   ref=np.arcsin,
                   domain=(-1, 1),
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   decorators=[
                       DecorateInfo(
                           toleranceOverride({torch.float16: tol(atol=1e-05, rtol=1e-03)}),
                           'TestUnaryUfuncs', device_type='cuda'),
                       precisionOverride({torch.bfloat16: 1e-2}),
                   ],
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    # NOTE: derivative for inplace asinh is not implemented
    UnaryUfuncInfo('asinh',
                   aliases=('arcsinh', ),
                   ref=np.arcsinh,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   decorators=(precisionOverride({torch.bfloat16: 5e-2}),),
                   supports_inplace_autograd=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    UnaryUfuncInfo('atan',
                   aliases=('arctan', ),
                   ref=np.arctan,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   decorators=(precisionOverride({torch.bfloat16: 1e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    active_if=TEST_WITH_ROCM, device_type='cuda'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    active_if=TEST_WITH_ROCM, device_type='cuda', dtypes=[torch.complex128]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    BinaryUfuncInfo('atan2',
                    aliases=('arctan2',),
                    dtypes=all_types_and(torch.bool, torch.bfloat16),
                    dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    promotes_int_to_float=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # Incorrectly attempts to use a scalar for the second argument
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),
                    )),
    UnaryUfuncInfo('atanh',
                   aliases=('arctanh', ),
                   ref=np.arctanh,
                   domain=(-1, 1),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   decorators=(precisionOverride({torch.bfloat16: 1e-2}),),
                   supports_inplace_autograd=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.cfloat],
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    active_if=TEST_WITH_ROCM, device_type='cuda', dtypes=[torch.complex128]),
                   )),
    OpInfo('allclose',
           dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           ref=np.allclose,
           supports_autograd=False,
           supports_forward_ad=False,
           sample_inputs_func=sample_inputs_allclose,
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
           ),
           supports_out=False),
    OpInfo('broadcast_to',
           ref=np.broadcast_to,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_broadcast_to),
    OpInfo('broadcast_shapes',
           op=torch.broadcast_shapes,
           ref=np.broadcast_shapes if np.lib.NumpyVersion(np.__version__) >= '1.20.0' else None,
           dtypes=_dispatch_dtypes((torch.float32,)),
           supports_out=False,
           supports_gradgrad=False,
           assert_autodiffed=False,
           supports_autograd=False,
           supports_scripting=False,
           sample_inputs_func=sample_inputs_broadcast_shapes,
           skips=(
               # https://github.com/pytorch/pytorch/issues/64997
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # skip dtype tests since broadcast_shape is not device dependent.
               # having dtypes limited to torch.float32 would cause test_dtypes to report unexpected success
               DecorateInfo(unittest.skip('Skipped!'), 'TestCommon', 'test_dtypes'),
               # skip these tests since we have non tensor input
               DecorateInfo(unittest.skip('Skipped!'), "TestCommon", "test_noncontiguous_samples"),
               DecorateInfo(unittest.skip('Skipped!'), 'TestCommon', 'test_variant_consistency_eager'),
               DecorateInfo(unittest.skip('Skipped!'), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('broadcast_tensors',
           ref=np.broadcast_arrays,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_broadcast_tensors,
           reference_inputs_func=reference_inputs_broadcast_tensors,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               # https://github.com/pytorch/pytorch/issues/64997
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # JIT does not support variadic tensors.
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":252,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=[torch.float32]),
           )),
    OpInfo('block_diag',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # Default batching rule in core doesn't work for ops with TensorList args
           check_batched_forward_grad=False,
           skips=(
               # https://github.com/pytorch/pytorch/issues/64997
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # JIT does not support variadic tensors.
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":252,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=[torch.float32]),
           ),
           sample_inputs_func=sample_inputs_block_diag),
    UnaryUfuncInfo('bitwise_not',
                   ref=np.bitwise_not,
                   dtypes=integral_types_and(torch.bool),
                   operator_variant=operator.invert,
                   supports_autograd=False),
    BinaryUfuncInfo('bitwise_left_shift',
                    op=torch.bitwise_left_shift,
                    dtypes=integral_types(),
                    dtypesIfCUDA=integral_types(),
                    operator_variant=operator.lshift,
                    inplace_operator_variant=operator.ilshift,
                    supports_autograd=False,
                    supports_one_python_scalar=True,
                    rhs_make_tensor_kwargs=dict(low=0),
                    skips=(
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
                    )),
    BinaryUfuncInfo('bitwise_right_shift',
                    op=torch.bitwise_right_shift,
                    dtypes=integral_types(),
                    dtypesIfCUDA=integral_types(),
                    operator_variant=operator.rshift,
                    inplace_operator_variant=operator.irshift,
                    supports_autograd=False,
                    supports_one_python_scalar=True,
                    rhs_make_tensor_kwargs=dict(low=0),
                    skips=(
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
                    )),
    OpInfo('combinations',
           op=torch.combinations,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           supports_out=False,
           sample_inputs_func=sample_inputs_combinations),
    OpInfo('cartesian_prod',
           op=torch.cartesian_prod,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_cartesian_prod,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270
               DecorateInfo(unittest.expectedFailure,
                            'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
           )),
    OpInfo('cdist',
           dtypes=floating_types(),
           supports_out=False,
           supports_gradgrad=False,
           assert_autodiffed=False,
           sample_inputs_func=sample_inputs_cdist),
    UnaryUfuncInfo('ceil',
                   ref=np.ceil,
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True),
    OpInfo('cholesky',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_cholesky,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],),
    OpInfo('cholesky_inverse',
           dtypes=floating_and_complex_types(),
           backward_dtypes=floating_and_complex_types(),
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           check_batched_gradgrad=True,
           sample_inputs_func=sample_inputs_linalg_cholesky_inverse,
           gradcheck_wrapper=gradcheck_wrapper_triangular_input_real_positive_diagonal,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # Strides are not the same! Original strides were ((4, 2, 1),) and strides are now ((4, 1, 2),)
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),)),
    OpInfo('cholesky_solve',
           op=torch.cholesky_solve,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_cholesky_solve,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_wrapper=lambda *args, **kwargs: gradcheck_wrapper_triangular_input(*args, idx=1, **kwargs),
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack]),
    OpInfo('chunk',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           sample_inputs_func=sample_inputs_chunk,
           reference_inputs_func=reference_inputs_chunk,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('clone',
           ref=np.copy,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           sample_inputs_func=sample_inputs_clone_contiguous,
           reference_inputs_func=reference_inputs_clone_contiguous,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           skips=(
               # TypeError: _copy_dispatcher() got an unexpected keyword argument 'memory_format'
               # (NumPy reference needs to be extended with memory_format)
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_numpy_ref'),
           ),),
    OpInfo('contiguous',
           op=lambda x, *args, **kwargs: x.contiguous(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           sample_inputs_func=sample_inputs_clone_contiguous,
           reference_inputs_func=reference_inputs_clone_contiguous,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           autodiff_fusible_nodes=['aten::contiguous'],
           assert_jit_shape_analysis=True,
           supports_out=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
           )),
    OpInfo('sum_to_size',
           op=lambda x, *args, **kwargs: x.sum_to_size(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_sum_to_size,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float,)),),),
    OpInfo('symeig',
           dtypes=floating_and_complex_types(),
           check_batched_grad=False,
           check_batched_gradgrad=False,
           sample_inputs_func=sample_inputs_symeig,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           ),
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack, with_tf32_off]),
    OpInfo('clamp',
           aliases=('clip',),
           ref=_clamp_numpy,
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_clamp,
           reference_inputs_func=partial(reference_inputs_elementwise_ternary, sample_inputs_func=sample_inputs_clamp),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # nvFuser and NNC appear to not handle boolean clamp
               DecorateInfo(unittest.expectedFailure,
                            'TestCudaFuserOpInfo',
                            'test_nvfuser_correctness',
                            dtypes=(torch.bool,)),
               DecorateInfo(unittest.expectedFailure,
                            'TestNNCOpInfo',
                            'test_nnc_correctness',
                            dtypes=(torch.bool,)),
           )),
    UnaryUfuncInfo('positive',
                   ref=np.positive,
                   dtypes=all_types_and_complex_and(torch.half, torch.bfloat16, torch.chalf),
                   supports_out=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   ),
    UnaryUfuncInfo('conj',
                   ref=np.conj,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16,
                                                    torch.half, torch.chalf),
                   supports_sparse=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   # See https://github.com/pytorch/pytorch/pull/78358
                   check_batched_forward_grad=False,
                   supports_out=False),
    UnaryUfuncInfo('conj_physical',
                   decomp_aten_name='_conj_physical',
                   ref=np.conj,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16,
                                                    torch.half, torch.chalf),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       # RuntimeError: inputSet && outputSet
                       # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":118,
                       # please report a bug to PyTorch.
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32, )),
                       DecorateInfo(unittest.skip("Skipped! conj_physical_ not implemented for sparse"),
                                    'TestSparseUnaryUfuncs', 'test_inplace'),
                   )),
    OpInfo('resolve_conj',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_view_as_real,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           ),
    OpInfo('resolve_neg',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           sample_inputs_func=sample_inputs_view_as_real,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           ),
    OpInfo('view_as_real',
           dtypes=complex_types(),
           supports_forward_ad=True,
           supports_out=False,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_view_as_real,
           test_conjugated_samples=False,
           ),
    OpInfo('view_as_complex',
           dtypes=floating_types_and(torch.half),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           test_neg_view=False,
           sample_inputs_func=sample_inputs_view_as_complex,
           skips=(
               # RuntimeError: Tensor must have a last dimension with stride 1
               DecorateInfo(unittest.expectedFailure, "TestCommon", "test_noncontiguous_samples"),
               # RuntimeError: "eq_cpu" not implemented for 'ComplexHalf'
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness', dtypes=(torch.half,)),
               # RuntimeError: "eq_cpu" not implemented for 'ComplexHalf'
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo', 'test_nvfuser_correctness', dtypes=(torch.half,)),
           )),
    BinaryUfuncInfo('complex',
                    dtypes=floating_types_and(torch.half),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # Test doesn't account for complex's type promotion semantics
                        DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion'),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out', device_type='mps'),
                    )),
    BinaryUfuncInfo('copysign',
                    dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                    promotes_int_to_float=True,
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True),
    OpInfo('corrcoef',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half,
                                                  *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_corrcoef,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
           ),
           supports_out=False),
    UnaryUfuncInfo('cos',
                   ref=np.cos,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   handles_large_floats=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.bfloat16: 1e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.cfloat, torch.cdouble,), device_type='cpu', active_if=IS_WINDOWS),
                       # This fails on CUDA but passes on ROCm
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.cdouble,), device_type='cuda'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_MACOS),
                       # AssertionError: Tensor-likes are not close!
                       # Greatest absolute difference: nan at index (700,) (up to 1e-05 allowed)
                       # Greatest relative difference: nan at index (700,) (up to 0.001 allowed)
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda',
                                    dtypes=(torch.chalf,), active_if=IS_WINDOWS),
                   )),
    UnaryUfuncInfo('cosh',
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.cosh),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/48641
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.int8]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_MACOS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu',
                                    dtypes=[torch.cfloat, torch.cdouble], active_if=IS_MACOS),
                       # AssertionError: Tensor-likes are not close!
                       # Greatest absolute difference: nan at index (6000,) (up to 1e-05 allowed)
                       # Greatest relative difference: nan at index (6000,) (up to 0.001 allowed)
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda',
                                    dtypes=(torch.chalf,), active_if=IS_WINDOWS),
                   )),
    OpInfo('cov',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half,
                                                  *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           backward_dtypesIfCUDA=all_types_and_complex_and(torch.half, *[torch.bfloat16]
                                                           if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_cov,
           error_inputs_func=error_inputs_cov,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
               # Float did not match double
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_grad'),
               # Jacobian mismatch
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_gradgrad'),
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_forward_mode_AD'),
               DecorateInfo(unittest.skip("Barely fails"), 'TestGradients', 'test_fn_fwgrad_bwgrad'),
               # JIT test not working for tensor kwargs (https://github.com/pytorch/pytorch/issues/58507)
               # RuntimeError:
               # undefined value tensor:
               #   File "<string>", line 3
               # def the_method(i0):
               #     return torch.cov(i0, correction=0, fweights=None, aweights=tensor([0.0518, 0.4681], dtype=torch.float32, requires_grad=True)) # noqa: B950
               #                                                                ~~~~~~ <--- HERE
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           )),
    OpInfo('cross',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half),
           sample_inputs_func=sample_inputs_cross,
           supports_fwgrad_bwgrad=True,
           supports_out=True,
           supports_forward_ad=True),
    OpInfo('linalg.cross',
           ref=lambda x, y, dim=-1: np.cross(x, y, axis=dim),
           op=torch.linalg.cross,
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half),
           aten_name='linalg_cross',
           sample_inputs_func=sample_inputs_cross,
           supports_out=True,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True),
    OpInfo('cumsum',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # cumsum does not handle correctly out= dtypes
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
           ),
           sample_inputs_func=sample_inputs_cumulative_ops),
    OpInfo('cumprod',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # cumprod does not handle correctly out= dtypes
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
           ),
           # gradgradcheck fails in fast_mode=True: #56275
           sample_inputs_func=sample_inputs_cumprod,
           gradcheck_fast_mode=False),
    OpInfo('cummax',
           dtypes=all_types_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=partial(sample_inputs_cumulative_ops, supports_dtype_kwargs=False),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
           ),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    OpInfo('cummin',
           dtypes=all_types_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=partial(sample_inputs_cumulative_ops, supports_dtype_kwargs=False),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
           ),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    UnaryUfuncInfo('deg2rad',
                   ref=np.radians,
                   decorators=(precisionOverride({torch.bfloat16: 7e-1,
                                                  torch.float16: 7e-1}),),
                   dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/51283#issuecomment-770614273
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16]),
                   )),
    OpInfo('diff',
           op=torch.diff,
           # np.diff has np._NoValue as default values for prepend and append, compare_with_reference breaks if prepend/append
           # are set as None when converting to numpy
           ref=lambda input, n=1, dim=-1, prepend=np._NoValue, append=np._NoValue: (
               np.diff(input, n, dim, np._NoValue if prepend is None else prepend, np._NoValue if append is None else append)
           ),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_diff,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
           )),
    BinaryUfuncInfo('div',
                    aliases=('divide',),
                    variant_test_name='no_rounding_mode',
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
                    # Runs very slowly on slow gradcheck - alternatively reduce input sizes
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    promotes_int_to_float=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True,
                    assert_autodiffed=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=True),),
    BinaryUfuncInfo('div',
                    aliases=('divide',),
                    variant_test_name='trunc_rounding',
                    dtypes=all_types_and(torch.half, torch.bfloat16),
                    sample_inputs_func=partial(sample_inputs_elementwise_binary, sample_kwargs=dict(rounding_mode="trunc")),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    promotes_int_to_float=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True,
                    assert_autodiffed=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=True),
                    skips=(
                        # RuntimeError: MALFORMED INPUT: Unhandled node kind (in computeValue): aten::div
                        DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_working'),
                    )),
    BinaryUfuncInfo('div',
                    aliases=('divide',),
                    variant_test_name='floor_rounding',
                    dtypes=all_types_and(torch.half, torch.bfloat16),
                    sample_inputs_func=partial(sample_inputs_elementwise_binary, sample_kwargs=dict(rounding_mode="floor")),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    promotes_int_to_float=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True,
                    assert_autodiffed=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=True),
                    skips=(
                        # RuntimeError: MALFORMED INPUT: Unhandled node kind (in computeValue): aten::div
                        DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_working'),
                    )),
    BinaryUfuncInfo('true_divide',
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
                    supports_forward_ad=True,
                    promotes_int_to_float=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True,
                    rhs_make_tensor_kwargs=dict(exclude_zero=True)),
    OpInfo('equal',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           ref=lambda input, other: (input == other).all(),
           sample_inputs_func=sample_inputs_equal,
           supports_autograd=False,
           supports_tracing=False,
           skips=(
           )),
    UnaryUfuncInfo('exp',
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.exp),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/50093#pullrequestreview-561791547
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=[torch.bfloat16]),
                       # Reference: https://github.com/pytorch/pytorch/issues/48010
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
                   ),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    OpInfo('expand',
           op=lambda self, shape: self.expand(shape),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_expand,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           supports_out=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
           )),
    OpInfo('expand_as',
           op=lambda self, other: self.expand_as(other),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_expand_as,
           supports_out=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),),
           ),
    OpInfo('diag',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_diag,
           error_inputs_func=error_inputs_diag),
    OpInfo('diag_embed',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           supports_out=False,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_diagonal_diag_embed),
    OpInfo('diagonal',
           # They are not strictly aliases as they have diverging defaults, but we can see them as aliases for testing purposes
           # If we add tests that test the function against the alias, make linalg.diagonal into its own OpInfo
           aliases=('linalg.diagonal',),
           aten_backward_name='diagonal_backward',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_diagonal_diag_embed,
           reference_inputs_func=reference_inputs_diagonal,
           error_inputs_func=error_inputs_diagonal),
    OpInfo('diagonal_scatter',
           dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_diagonal_scatter),
    BinaryUfuncInfo('eq',
                    ref=np.equal,
                    dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
                    always_returns_bool=True,
                    supports_autograd=False,
                    sample_inputs_func=sample_inputs_comparison_ops,
                    skips=(
                    )),
    BinaryUfuncInfo('fmax',
                    op=torch.fmax,
                    dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # RuntimeError: "max_elementwise_cuda" not implemented for 'ComplexFloat'
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
                    )),
    BinaryUfuncInfo('fmin',
                    op=torch.fmin,
                    dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # RuntimeError: "min_elementwise_cuda" not implemented for 'ComplexFloat'
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
                    )),
    BinaryUfuncInfo('fmod',
                    ref=np.fmod,
                    dtypes=all_types_and(torch.float16, torch.bfloat16),
                    dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    assert_autodiffed=None,
                    rhs_make_tensor_kwargs={'exclude_zero': True},
                    decorators=(
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_contig_vs_every_other',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_non_contig',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_reference_numerics',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_reference_numerics_small_values',
                                     dtypes=(torch.uint8,)),
                    )),
    BinaryUfuncInfo('remainder',
                    ref=np.remainder,
                    dtypes=all_types_and(torch.float16, torch.bfloat16),
                    dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    assert_autodiffed=None,
                    operator_variant=operator.mod,
                    inplace_operator_variant=operator.imod,
                    supports_one_python_scalar=True,
                    rhs_make_tensor_kwargs={'exclude_zero': True},
                    decorators=(
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_contig_vs_every_other',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_non_contig',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_reference_numerics',
                                     dtypes=(torch.bfloat16,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs',
                                     'test_reference_numerics_small_values',
                                     dtypes=(torch.uint8,)),
                        # Fails on XLA
                        # False is not true : Tensors failed to compare as equal!
                        # Attempted to compare equality of tensors with different dtypes
                        DecorateInfo(unittest.skip("Skipped!"), 'TestOpInfo', device_type='xla', dtypes=(torch.long,)),
                    )),
    UnaryUfuncInfo('frac',
                   ref=lambda x: np.modf(x)[0],
                   dtypes=floating_types_and(torch.bfloat16, torch.float16),
                   dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=(torch.bfloat16, torch.float16, torch.float32, torch.float64)),
                       # 76047
                       DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness',
                                    dtypes=(torch.float32, torch.float64)),
                   )),
    SpectralFuncInfo('fft.fft',
                     aten_name='fft_fft',
                     decomp_aten_name='_fft_c2c',
                     ref=np.fft.fft,
                     ndimensional=SpectralFuncType.OneD,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     ),
    SpectralFuncInfo('fft.fft2',
                     aten_name='fft_fft2',
                     ref=np.fft.fft2,
                     decomp_aten_name='_fft_c2c',
                     ndimensional=SpectralFuncType.TwoD,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     decorators=[precisionOverride(
                         {torch.float: 1e-4, torch.cfloat: 1e-4})],
                     ),
    SpectralFuncInfo('fft.fftn',
                     aten_name='fft_fftn',
                     decomp_aten_name='_fft_c2c',
                     ref=np.fft.fftn,
                     ndimensional=SpectralFuncType.ND,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     decorators=[precisionOverride(
                         {torch.float: 1e-4, torch.cfloat: 1e-4})]),
    SpectralFuncInfo('fft.hfft',
                     aten_name='fft_hfft',
                     decomp_aten_name='_fft_c2r',
                     ref=np.fft.hfft,
                     ndimensional=SpectralFuncType.OneD,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     check_batched_gradgrad=False,
                     skips=(
                         # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
                         DecorateInfo(
                             unittest.skip("Skipped!"),
                             'TestSchemaCheckModeOpInfo',
                             'test_schema_correctness',
                             dtypes=(torch.complex64, torch.complex128)
                         ),
                     )),
    SpectralFuncInfo('fft.hfft2',
                     aten_name='fft_hfft2',
                     decomp_aten_name='_fft_c2r',
                     ref=scipy.fft.hfft2 if has_scipy_fft else None,
                     ndimensional=SpectralFuncType.TwoD,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     check_batched_gradgrad=False,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 2e-4, torch.cfloat: 2e-4}),
                             'TestFFT', 'test_reference_nd')],
                     skips=(
                         # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
                         DecorateInfo(
                             unittest.skip("Skipped!"),
                             'TestSchemaCheckModeOpInfo',
                             'test_schema_correctness'
                         ),
                     )),
    SpectralFuncInfo('fft.hfftn',
                     aten_name='fft_hfftn',
                     decomp_aten_name='_fft_c2r',
                     ref=scipy.fft.hfftn if has_scipy_fft else None,
                     ndimensional=SpectralFuncType.ND,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     check_batched_gradgrad=False,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 2e-4, torch.cfloat: 2e-4}),
                             'TestFFT', 'test_reference_nd'), ],
                     skips=(
                         # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
                         DecorateInfo(
                             unittest.skip("Skipped!"),
                             'TestSchemaCheckModeOpInfo',
                             'test_schema_correctness'
                         ),
                     )),
    SpectralFuncInfo('fft.rfft',
                     aten_name='fft_rfft',
                     decomp_aten_name='_fft_r2c',
                     ref=np.fft.rfft,
                     ndimensional=SpectralFuncType.OneD,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     check_batched_grad=False,
                     skips=(
                     ),
                     check_batched_gradgrad=False),
    SpectralFuncInfo('fft.rfft2',
                     aten_name='fft_rfft2',
                     decomp_aten_name='_fft_r2c',
                     ref=np.fft.rfft2,
                     ndimensional=SpectralFuncType.TwoD,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     check_batched_grad=False,
                     check_batched_gradgrad=False,
                     decorators=[
                         precisionOverride({torch.float: 1e-4}),
                     ],),
    SpectralFuncInfo('fft.rfftn',
                     aten_name='fft_rfftn',
                     decomp_aten_name='_fft_r2c',
                     ref=np.fft.rfftn,
                     ndimensional=SpectralFuncType.ND,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     check_batched_grad=False,
                     check_batched_gradgrad=False,
                     decorators=[
                         precisionOverride({torch.float: 1e-4}),
                     ],),
    SpectralFuncInfo('fft.ifft',
                     aten_name='fft_ifft',
                     decomp_aten_name='_fft_c2c',
                     ref=np.fft.ifft,
                     ndimensional=SpectralFuncType.OneD,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),),
    SpectralFuncInfo('fft.ifft2',
                     aten_name='fft_ifft2',
                     decomp_aten_name='_fft_c2c',
                     ref=np.fft.ifft2,
                     ndimensional=SpectralFuncType.TwoD,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 1e-4, torch.cfloat: 1e-4}),
                             'TestFFT', 'test_reference_nd')],
                     ),
    SpectralFuncInfo('fft.ifftn',
                     aten_name='fft_ifftn',
                     decomp_aten_name='_fft_c2c',
                     ref=np.fft.ifftn,
                     ndimensional=SpectralFuncType.ND,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 1e-4, torch.cfloat: 1e-4}),
                             'TestFFT', 'test_reference_nd')],
                     ),
    SpectralFuncInfo('fft.ihfft',
                     aten_name='fft_ihfft',
                     decomp_aten_name='_fft_r2c',
                     ref=np.fft.ihfft,
                     ndimensional=SpectralFuncType.OneD,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     skips=(
                     ),
                     check_batched_grad=False),
    SpectralFuncInfo('fft.ihfft2',
                     aten_name='fft_ihfft2',
                     decomp_aten_name='_fft_r2c',
                     ref=scipy.fft.ihfftn if has_scipy_fft else None,
                     ndimensional=SpectralFuncType.TwoD,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     check_batched_grad=False,
                     check_batched_gradgrad=False,
                     decorators=(
                         # The values for attribute 'shape' do not match: torch.Size([5, 6, 5]) != torch.Size([5, 6, 6]).
                         DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
                         DecorateInfo(precisionOverride({torch.float: 2e-4}), 'TestFFT', 'test_reference_nd'),
                         # Mismatched elements!
                         DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
                         DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warnings'))),
    SpectralFuncInfo('fft.ihfftn',
                     aten_name='fft_ihfftn',
                     decomp_aten_name='_fft_r2c',
                     ref=scipy.fft.ihfftn if has_scipy_fft else None,
                     ndimensional=SpectralFuncType.ND,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archss
                     dtypesIfCUDA=all_types_and(torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half,)),
                     check_batched_grad=False,
                     check_batched_gradgrad=False,
                     decorators=[
                         # The values for attribute 'shape' do not match: torch.Size([5, 6, 5]) != torch.Size([5, 6, 6]).
                         DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
                         # Mismatched elements!
                         DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
                         DecorateInfo(
                             precisionOverride({torch.float: 2e-4}),
                             'TestFFT', 'test_reference_nd')],
                     ),
    SpectralFuncInfo('fft.irfft',
                     aten_name='fft_irfft',
                     decomp_aten_name='_fft_c2r',
                     ref=np.fft.irfft,
                     ndimensional=SpectralFuncType.OneD,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     check_batched_gradgrad=False),
    SpectralFuncInfo('fft.irfft2',
                     aten_name='fft_irfft2',
                     decomp_aten_name='_fft_c2r',
                     ref=np.fft.irfft2,
                     ndimensional=SpectralFuncType.TwoD,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     check_batched_gradgrad=False,
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 1e-4, torch.cfloat: 1e-4}),
                             'TestFFT', 'test_reference_nd')],
                     ),
    SpectralFuncInfo('fft.irfftn',
                     aten_name='fft_irfftn',
                     decomp_aten_name='_fft_c2r',
                     ref=np.fft.irfftn,
                     ndimensional=SpectralFuncType.ND,
                     # https://github.com/pytorch/pytorch/issues/80411
                     gradcheck_fast_mode=True,
                     supports_forward_ad=True,
                     supports_fwgrad_bwgrad=True,
                     # See https://github.com/pytorch/pytorch/pull/78358
                     check_batched_forward_grad=False,
                     dtypes=all_types_and_complex_and(torch.bool),
                     # rocFFT doesn't support Half/Complex Half Precision FFT
                     # CUDA supports Half/ComplexHalf Precision FFT only on SM53 or later archs
                     dtypesIfCUDA=all_types_and_complex_and(
                         torch.bool, *() if (TEST_WITH_ROCM or not SM53OrLater) else (torch.half, torch.complex32)),
                     check_batched_gradgrad=False,
                     decorators=[
                         DecorateInfo(
                             precisionOverride({torch.float: 1e-4, torch.cfloat: 1e-4}),
                             'TestFFT', 'test_reference_nd')],
                     ),
    OpInfo('fft.fftshift',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           sample_inputs_func=sample_inputs_fftshift,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           ),
    OpInfo('fft.ifftshift',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           sample_inputs_func=sample_inputs_fftshift,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           ),
    OpInfo('stft',
           decorators=[
               skipCPUIfNoFFT,
               DecorateInfo(unittest.skip("Skipped! stft does not match the native function"),
                            'TestJit', 'test_variant_consistency_jit'),
           ],
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_stft,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_out=False,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           ),
    OpInfo('istft',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_istft,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_out=False,
           decorators=(
               DecorateInfo(unittest.skip("Skipped! istft does not match the native function"),
                            'TestJit', 'test_variant_consistency_jit'),
           ),
           skips=(
               skipCPUIfNoFFT,
               # gradcheck fails on ROCm (gh-68429)
               # grad is computed improperly (probably for weights tensor)
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_grad'),
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
           )),
    UnaryUfuncInfo('floor',
                   ref=np.floor,
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True),
    OpInfo('flip',
           op=torch.flip,
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_flip,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('fliplr',
           op=torch.fliplr,
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_fliplr_flipud,
           error_inputs_func=error_inputs_fliplr,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('flipud',
           op=torch.flipud,
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_fliplr_flipud,
           error_inputs_func=error_inputs_flipud,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('sparse.sampled_addmm',
           dtypes=floating_and_complex_types(),
           supports_autograd=True,
           sample_inputs_func=sample_inputs_sparse_sampled_addmm,
           decorators=[
               skipCUDAIf(_get_torch_cuda_version() < (11, 3), "cusparseSDDMM was added in 11.2.1"),
               skipCPUIfNoMklSparse, ],
           skips=(
               # NotImplementedError: Tensors of type SparseCsrTensorImpl do not have is_contiguous
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_noncontiguous_samples'),
               # RuntimeError: Sparse CSR tensors do not have strides.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestTags', 'test_tags'),
               # RuntimeError: sampled_addmm: Expected result to have sparse csr layout, but got Strided
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out_warning'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestCompositeCompliance', 'test_operator'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestCompositeCompliance', 'test_backward'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_conj_view'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_conj_view'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view'),
               # RuntimeError: Sparse CSR tensors do not have strides
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # RuntimeError: unsupported memory format option Preserve
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # GradcheckError: gradcheck expects all tensor inputs are dense when check_sparse_nnz is set to False
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_fwgrad_bwgrad'),
               # GradcheckError: gradcheck expects all tensor inputs are dense when check_sparse_nnz is set to False
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_grad'),
               # GradcheckError: gradcheck expects all tensor inputs are dense when check_sparse_nnz is set to False
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
               # GradcheckError: gradcheck expects all tensor inputs are dense when check_sparse_nnz is set to False
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_forward_mode_AD'),
           )),
    UnaryUfuncInfo('i0',
                   ref=np_unary_ufunc_integer_promotion_wrapper(
                       scipy.special.i0) if TEST_SCIPY else None,
                   aliases=('special.i0',),
                   decorators=(precisionOverride({torch.bfloat16: 3e-1,
                                                  torch.float16: 5e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   backward_dtypes=floating_types(),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_i0_i1,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.int8,)),
                   )),
    UnaryUfuncInfo('special.i0e',
                   aten_name='special_i0e',
                   ref=scipy.special.i0e if TEST_SCIPY else None,
                   decorators=(precisionOverride({torch.bfloat16: 3e-1,
                                                  torch.float16: 3e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   backward_dtypes=floating_types(),
                   sample_inputs_func=sample_inputs_i0_i1,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('special.i1',
                   aten_name='special_i1',
                   ref=np_unary_ufunc_integer_promotion_wrapper(scipy.special.i1) if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool),
                   dtypesIfCUDA=all_types_and(torch.bool),
                   sample_inputs_func=sample_inputs_i0_i1,
                   decorators=(
                       DecorateInfo(toleranceOverride({
                           torch.float32: tol(atol=1e-4, rtol=0),
                           torch.bool: tol(atol=1e-4, rtol=0)})),
                   ),
                   skips=(
                       DecorateInfo(unittest.skip("Incorrect result!"),
                                    'TestUnaryUfuncs',
                                    'test_reference_numerics_large',
                                    dtypes=(torch.int8,)),
                   ),
                   supports_fwgrad_bwgrad=True,
                   supports_forward_ad=True),
    UnaryUfuncInfo('special.i1e',
                   aten_name='special_i1e',
                   ref=scipy.special.i1e if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool),
                   dtypesIfCUDA=all_types_and(torch.bool),
                   sample_inputs_func=sample_inputs_i0_i1,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('special.ndtr',
                   aten_name='special_ndtr',
                   decorators=(precisionOverride({torch.bfloat16: 5e-3,
                                                  torch.float16: 5e-4}),),
                   ref=scipy.special.ndtr if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Dispatch stub: unsupported device typemeta
                       DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_fwgrad_bwgrad', device_type='meta'),
                   )),
    BinaryUfuncInfo('floor_divide',
                    ref=_floor_divide_np,
                    dtypes=all_types_and(torch.half, torch.bfloat16),
                    supports_autograd=False,
                    rhs_make_tensor_kwargs=dict(exclude_zero=True),
                    supports_two_python_scalars=True,
                    skips=(
                        # AssertionError: Results of original model and exported/imported version of model differed
                        DecorateInfo(unittest.skip('Skipped!'), 'TestJit', 'test_variant_consistency_jit'),
                        # bfloat16 floor_divide compared with a float32 reference works inconsistently
                        DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs',
                                     dtypes=(torch.bfloat16,)),
                        # int8 floor divide has different results for -128 // -1 vs. NumPy
                        DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs', 'test_reference_numerics_small_values',
                                     dtypes=(torch.int8,)),
                        # The following tests fails on some jobs
                        DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs', 'test_reference_numerics_extremal_values',
                                     dtypes=(torch.float16,)),
                    )),
    UnaryUfuncInfo('frexp',
                   op=torch.frexp,
                   ref=np.frexp,
                   dtypes=floating_types_and(torch.half, torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half),
                   # skip testing torch.frexp as it is not supported by ROCm platform yet
                   decorators=[],
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # skips below tests as torch.frexp returns tuple-like (mantissa, exponent) as outputs,
                       # while theses tests currently requires output to a single tensor.
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_batch_vs_slicing'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_contig_vs_every_other'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_contig_vs_transposed'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_non_contig_expand'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_variant_consistency'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_out_arg_all_dtypes'),

                       # skips test_reference_numerics due to error in Windows CI.
                       # The np.frexp returns exponent as np.intc dtype on Windows platform,
                       # and np.intc does not have the correspond torch dtype
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    active_if=IS_WINDOWS),
                   )),
    BinaryUfuncInfo('ge',
                    ref=np.greater_equal,
                    aliases=('greater_equal',),
                    dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                    always_returns_bool=True,
                    supports_autograd=False,
                    skips=(
                    )),
    OpInfo('geqrf',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_qr_geqrf,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           supports_autograd=False,
           skips=(
               # FIXME: geqrf can't forward with complex inputs that require grad
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_dtypes'),
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
           )),
    BinaryUfuncInfo('gt',
                    ref=np.greater,
                    aliases=('greater',),
                    dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                    always_returns_bool=True,
                    supports_autograd=False,
                    skips=(
                    )),
    UnaryUfuncInfo('imag',
                   ref=np.imag,
                   dtypes=complex_types_and(torch.chalf),
                   supports_out=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   # See https://github.com/pytorch/pytorch/issues/66357
                   # RuntimeError: view_as_real doesn't work on unresolved conjugated tensors.
                   check_batched_forward_grad=False,
                   skips=(
                       # Skip since real and imag don't have out variants.
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_out_arg_all_dtypes'),
                   )),
    OpInfo('gradient',
           dtypes=floating_and_complex_types_and(torch.int8, torch.int16,
                                                 torch.int32, torch.int64,
                                                 torch.bfloat16, torch.half),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # following tests give a runtime error with undefined value tensor
               # see discussion : https://github.com/pytorch/pytorch/issues/56660
               # RuntimeError:
               # Arguments for call are not valid.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32, torch.complex64)),  # noqa: B950
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
           ),
           supports_inplace_autograd=False,
           sample_inputs_func=sample_inputs_gradient,
           error_inputs_func=error_inputs_gradient),
    OpInfo('inverse',
           op=torch.inverse,
           dtypes=floating_and_complex_types(),
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_linalg_invertible,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', '.test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', '.test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('isin',
           dtypes=all_types(),
           dtypesIfCUDA=all_types_and(torch.half),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_isin),
    OpInfo('kthvalue',
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_kthvalue,
           error_inputs_func=error_inputs_kthvalue),
    BinaryUfuncInfo('le',
                    ref=np.less_equal,
                    aliases=('less_equal',),
                    dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                    always_returns_bool=True,
                    supports_autograd=False,
                    skips=(
                    )),
    OpInfo('linalg.det',
           op=torch.linalg.det,
           aliases=('det',),
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           aten_name='linalg_det',
           sample_inputs_func=sample_inputs_linalg_det_logdet_slogdet,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack,
                       DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-3, rtol=1e-3)}))],
           check_batched_gradgrad=False,
           supports_inplace_autograd=False),
    OpInfo('linalg.det',
           op=torch.linalg.det,
           variant_test_name='singular',
           aliases=('det',),
           dtypes=double_types(),
           backward_dtypes=double_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           aten_name='linalg_det',
           sample_inputs_func=sample_inputs_linalg_det_singular,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack,
                       DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-3, rtol=1e-3)}))],
           check_batched_gradgrad=False,
           supports_inplace_autograd=False,
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), "TestGradients", 'test_fn_fwgrad_bwgrad'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
               # dtypes are tested in the suite above, no need to repeat it for singular
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes'),
           )),
    OpInfo('linalg.cholesky',
           aten_name='linalg_cholesky',
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_linalg_cholesky,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],),
    OpInfo('linalg.cholesky_ex',
           aten_name='linalg_cholesky_ex',
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_linalg_cholesky,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           ),
    OpInfo('linalg.vecdot',
           aten_name='linalg_vecdot',
           ref=lambda x, y, *, dim=-1: (x.conj() * y).sum(dim),
           dtypes=floating_and_complex_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_linalg_vecdot,
           check_batched_forward_grad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('linalg.cond',
           aten_name='linalg_cond',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_cond,
           check_batched_gradgrad=False,
           check_batched_forward_grad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],),
    OpInfo('linalg.eig',
           aten_name='linalg_eig',
           op=torch.linalg.eig,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_eig,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # AssertionError: Scalars are not equal!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out', device_type='cpu'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           ),
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack, with_tf32_off],
           ),
    OpInfo('linalg.eigvals',
           aten_name='linalg_eigvals',
           op=torch.linalg.eigvals,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_invertible,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # Pre-existing condition; Needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
               # exits early on eager extremal value test
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.eigh',
           aten_name='linalg_eigh',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_eigh,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack, with_tf32_off],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.eigvalsh',
           aten_name='linalg_eigvalsh',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_eigh,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # Pre-existing condition; Needs to be fixed
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.householder_product',
           aten_name='linalg_householder_product',
           op=torch.linalg.householder_product,
           aliases=('orgqr', ),
           dtypes=floating_and_complex_types(),
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           # TODO: backward uses in-place operations that vmap doesn't like
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_householder_product,
           decorators=[
               skipCUDAIfNoCusolver, skipCPUIfNoLapack,
               DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-3, rtol=1e-3)})),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
           ]),
    OpInfo('linalg.ldl_factor',
           aten_name='linalg_ldl_factor',
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_linalg_ldl_factor,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, skipCUDAIfRocm],
           ),
    OpInfo('linalg.ldl_factor_ex',
           aten_name='linalg_ldl_factor_ex',
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_linalg_ldl_factor,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, skipCUDAIfRocm],
           ),
    OpInfo('linalg.ldl_solve',
           aten_name='linalg_ldl_solve',
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_linalg_ldl_solve,
           decorators=[
               skipCUDAIf(_get_torch_cuda_version() < (11, 4), "not available before CUDA 11.3.1"),
               skipCUDAIfNoCusolver, skipCUDAIfRocm, skipCPUIfNoLapack],
           ),
    OpInfo('linalg.lstsq',
           aten_name='linalg_lstsq',
           dtypes=floating_and_complex_types(),
           supports_out=True,
           sample_inputs_func=sample_inputs_linalg_lstsq,
           error_inputs_func=error_inputs_lstsq,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # we skip gradient checks for this suite as they are tested in
               # variant_test_name='grad_oriented'
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
               # The values for attribute 'shape' do not match
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.lstsq',
           aten_name='linalg_lstsq',
           variant_test_name='grad_oriented',
           # gradchecks for forward AD fails with multi-Tensor outputs
           op=lambda a, b, driver: torch.linalg.lstsq(a, b, driver=driver)[0],
           supports_out=False,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_lstsq,
           error_inputs_func=error_inputs_lstsq_grad_oriented,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # tests do not work with passing lambda for op
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.expectedFailure, 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
           )),
    OpInfo('linalg.matrix_power',
           aliases=('matrix_power',),
           aten_name='linalg_matrix_power',
           dtypes=floating_and_complex_types(),
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_inplace_autograd=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_grad=False,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           sample_inputs_func=sample_inputs_linalg_matrix_power,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           skips=(
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
           )),
    OpInfo('linalg.multi_dot',
           # Need this lambda because gradcheck does not work with TensorList inputs
           aten_name='linalg_multi_dot',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           supports_inplace_autograd=False,
           # Batched grad checks fail for empty input tensors (see https://github.com/pytorch/pytorch/issues/53407)
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_linalg_multi_dot,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           skips=(
               # https://github.com/pytorch/pytorch/issues/67470
               DecorateInfo(unittest.skip("67470!"), 'TestCommon', 'test_noncontiguous_samples'),
               # Fails on XLA.
               # AssertionError: False is not true : Tensors failed to compare as equal!
               DecorateInfo(unittest.skip("Skipped!"), 'TestOpInfo', device_type='xla', dtypes=(torch.long,)),
               # https://github.com/pytorch/pytorch/issues/71774
               DecorateInfo(unittest.skip('Skipped!'), 'TestNNCOpInfo', 'test_nnc_correctness',
                            device_type='cpu', dtypes=(torch.long,)),
           )),
    # NB: linalg.norm has two variants so that different skips can be used for different sample inputs
    OpInfo('linalg.norm',
           aten_name='linalg_norm',
           op=torch.linalg.norm,
           dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           sample_inputs_func=sample_inputs_linalg_norm,
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True),
    OpInfo('linalg.norm',
           op=torch.linalg.norm,
           variant_test_name='subgradients_at_zero',
           dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           sample_inputs_func=partial(sample_inputs_linalg_norm, variant='subgradient_at_zero'),
           aten_name='linalg_norm',
           supports_forward_ad=True,
           # torch.autograd.gradcheck.GradcheckError: While computing batched gradients, got:
           # Could not allocate memory to change Tensor SizesAndStrides!
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           skips=(
               # [NEW] Skips specifically for sample inputs at zero
               # norm's vjp/jvp are not well-conditioned near zero
               DecorateInfo(unittest.expectedFailure, "TestGradients", 'test_fn_gradgrad'),
               DecorateInfo(unittest.expectedFailure, "TestGradients", 'test_fn_fwgrad_bwgrad')
           )),
    OpInfo('linalg.matrix_norm',
           aten_name='linalg_matrix_norm',
           dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           check_batched_gradgrad=False,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           sample_inputs_func=sample_inputs_linalg_matrix_norm),
    OpInfo('linalg.qr',
           aten_name='linalg_qr',
           op=torch.linalg.qr,
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # In-place ops
           check_batched_gradgrad=False,
           sample_inputs_func=sample_inputs_linalg_qr_geqrf,
           decorators=[skipCUDAIfNoCusolver, skipCPUIfNoLapack]),
    OpInfo('linalg.slogdet',
           aten_name='linalg_slogdet',
           op=torch.linalg.slogdet,
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_det_logdet_slogdet,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack]),
    OpInfo('linalg.vander',
           aten_name='linalg_vander',
           ref=np_vander_batched,
           op=torch.linalg.vander,
           dtypes=all_types_and_complex(),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           skips=(
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
           ),
           sample_inputs_func=sample_inputs_linalg_vander),
    ReductionOpInfo(
        'linalg.vector_norm',
        op=torch.linalg.vector_norm,
        identity=0,
        nan_policy='propagate',
        supports_multiple_dims=True,
        complex_to_real=True,
        supports_forward_ad=True,
        # torch.autograd.gradcheck.GradcheckError: While computing batched gradients
        # got: Could not allocate memory to change Tensor SizesAndStrides!
        check_batched_forward_grad=False,
        supports_fwgrad_bwgrad=True,
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        generate_args_kwargs=sample_kwargs_vector_norm,
        aten_name='linalg_vector_norm',
        skips=(
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
        )),
    OpInfo('linspace',
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16),
           is_factory_function=True,
           supports_out=True,
           supports_autograd=False,
           error_inputs_func=error_inputs_linspace,
           sample_inputs_func=sample_inputs_linspace,
           skips=(
               # Tests that assume input is a tensor or sequence of tensors
               DecorateInfo(unittest.expectedFailure, "TestCommon", "test_noncontiguous_samples"),
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),

               # Same failure as arange: cannot find linspace in captured graph
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),

               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),

               # cpu implementation is wrong on some integral types
               # https://github.com/pytorch/pytorch/issues/81996
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_quick',
                            dtypes=(torch.int16, torch.int32, torch.int64), device_type="cpu"),
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_comprehensive',
                            dtypes=(torch.int16, torch.int32, torch.int64), device_type="cpu"),
               # cuda implementation is off-by-one on some inputs due to precision issues
               # https://github.com/pytorch/pytorch/issues/82230
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_quick',
                            dtypes=(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64),
                            device_type="cuda"),
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_comprehensive',
                            dtypes=(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64),
                            device_type="cuda"),
               # UserWarning: CUDA caching allocator reports a memory leak not verified by the driver API
               # in __main__.TestJitCUDA.test_variant_consistency_jit_logspace_cuda_complex64!
               # Caching allocator allocated memory was 0 and is now reported as 307200 on device 0.
               # CUDA driver allocated memory was 1254555648 and is now 1242955776.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            dtypes=(torch.cfloat,), device_type="cuda"),
           )),
    OpInfo('logspace',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.half, torch.bfloat16),
           is_factory_function=True,
           supports_out=True,
           supports_autograd=False,
           error_inputs_func=error_inputs_linspace,
           sample_inputs_func=sample_inputs_logpace,
           skips=(
               # Tests that assume input is a tensor or sequence of tensors
               DecorateInfo(unittest.expectedFailure, "TestCommon", "test_noncontiguous_samples"),
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
               # Same failure as arange: cannot find linspace in captured graph
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),

               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),

               # Off-by-one issue when casting floats to ints
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_quick',
                            dtypes=(torch.int16, torch.int32, torch.int64), device_type="cuda"),
               DecorateInfo(unittest.expectedFailure, 'TestDecomp', 'test_comprehensive',
                            dtypes=(torch.int16, torch.int32, torch.int64), device_type="cuda"),
               # UserWarning: CUDA caching allocator reports a memory leak not verified by the driver API
               # in __main__.TestJitCUDA.test_variant_consistency_jit_logspace_cuda_complex64!
               # Caching allocator allocated memory was 0 and is now reported as 307200 on device 0.
               # CUDA driver allocated memory was 1254555648 and is now 1242955776.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            dtypes=(torch.cfloat,), device_type="cuda"),
           )),
    UnaryUfuncInfo('log',
                   ref=np.log,
                   domain=(0, None),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
                   backward_dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16, torch.chalf),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.bfloat16: 5e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=IS_WINDOWS),
                   ),
                   # log(z)->-inf for |z|->0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: torch.abs(x) < 0.1, safe_val=1)),
    UnaryUfuncInfo('log10',
                   ref=np.log10,
                   domain=(0, None),
                   decorators=(precisionOverride({torch.bfloat16: 5e-2}),),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   assert_autodiffed=True,
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=IS_WINDOWS),
                   ),
                   # log10(z)->-inf for |z|->0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: torch.abs(x) < 0.1, safe_val=1)),
    UnaryUfuncInfo('log1p',
                   ref=np.log1p,
                   aliases=('special.log1p',),
                   domain=(-1, None),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   decorators=(precisionOverride({torch.bfloat16: 1e-1}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   ),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True),
    UnaryUfuncInfo('log2',
                   ref=np.log2,
                   domain=(0, None),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.bfloat16: 1e-1}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.cfloat, torch.cdouble]),
                   ),
                   # log2(z)->-inf for |z|->0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: torch.abs(x) < 0.1, safe_val=1)),
    BinaryUfuncInfo('ldexp',
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    # Runs very slowly on slow gradcheck - alternatively reduce input sizes
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_inplace_autograd=False,
                    promotes_int_to_float=True,
                    supports_out=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # RuntimeError: mul(): functions with out=... arguments don't support
                        # automatic differentiation, but one of the arguments requires grad
                        # https://github.com/pytorch/pytorch/issues/68966
                        DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager'),
                        DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
                        DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
                        DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
                    ),
                    decorators=[
                        DecorateInfo(
                            toleranceOverride({
                                torch.complex64: tol(atol=1e-05, rtol=1e-05)
                            }),
                            'TestCommon', device_type='cpu',
                        ),
                    ], ),
    OpInfo('logaddexp',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.bfloat16),
           dtypesIfROCM=floating_types_and(torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=lambda op_info, device, dtype, requires_grad=False, **kwargs:
           (SampleInput(make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),
                        args=(make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),)),)),
    OpInfo('logaddexp2',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.bfloat16),
           dtypesIfROCM=floating_types_and(torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=lambda op_info, device, dtype, requires_grad=False, **kwargs:
           (SampleInput(make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),
                        args=(make_tensor((S, S), dtype=dtype, device=device, requires_grad=requires_grad),)),)),
    UnaryUfuncInfo('logical_not',
                   ref=np.logical_not,
                   decorators=(precisionOverride({torch.bfloat16: 7e-1,
                                                  torch.float16: 5e-1}),),
                   dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                   supports_autograd=False,
                   skips=(
                       # The function variant always returns BoolTensor
                       # while the inplace variant preserves the input dtype.
                       # >>> t = torch.randn(3)
                       # >>> torch.logical_not(t)
                       # tensor([False, False, False])
                       # >>> torch.logical_not(t).dtype
                       # torch.bool
                       # >>> t.logical_not_().dtype
                       # torch.float32
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_variant_consistency',
                                    dtypes=all_types_and_complex_and(torch.half, torch.bfloat16)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                                    dtypes=all_types_and_complex_and(torch.half, torch.bfloat16)),
                   )),
    BinaryUfuncInfo('lt',
                    ref=np.less,
                    aliases=('less',),
                    dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                    always_returns_bool=True,
                    supports_autograd=False,
                    skips=(
                    )),
    OpInfo('linalg.lu_factor',
           aten_name='linalg_lu_factor',
           op=torch.linalg.lu_factor,
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_lu,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack]),
    OpInfo('linalg.lu_factor_ex',
           aten_name='linalg_lu_factor_ex',
           op=torch.linalg.lu_factor_ex,
           dtypes=floating_and_complex_types(),
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_lu,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack]),
    OpInfo('linalg.lu',
           aten_name='linalg_lu',
           op=torch.linalg.lu,
           dtypes=floating_and_complex_types(),
           # https://github.com/pytorch/pytorch/issues/80411
           # Runs very slowly on slow-gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_lu,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack]),
    OpInfo('lu_unpack',
           op=torch.lu_unpack,
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(skipCPUIfNoLapack,),
           sample_inputs_func=sample_inputs_lu_unpack),
    OpInfo('lu',
           op=torch.lu,
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_lu,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               # we skip jit tests because `lu` is a torch function
               # RuntimeError:
               # 'Tensor (inferred)' object has no attribute or method 'lu'.:
               # File "<string>", line 3
               # def the_method(i0):
               #     return i0.lu(True, True)
               #            ~~~~~ <--- HERE
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # RuntimeError not raised: Expected RuntimeError when calling with input.device=cpu and out.device=cuda
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
           )),
    OpInfo('lu_solve',
           op=torch.lu_solve,
           dtypes=floating_and_complex_types(),
           supports_forward_ad=True,
           # See https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_lu_solve,
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Tests different backward paths"),
                            "TestCommon", "test_floating_inputs_are_differentiable"),),
           decorators=[skipCPUIfNoLapack, skipCUDAIfNoMagmaAndNoCusolver]),
    OpInfo('linalg.lu_solve',
           op=torch.linalg.lu_solve,
           aten_name='linalg_lu_solve',
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_lu_solve,
           skips=(
               DecorateInfo(unittest.skip("Tests different backward paths"),
                            "TestCommon", "test_floating_inputs_are_differentiable"),),
           decorators=[skipCPUIfNoLapack, skipCUDAIfNoMagmaAndNoCusolver]),
    OpInfo('masked_fill',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           sample_inputs_func=sample_inputs_masked_fill,
           error_inputs_func=error_inputs_masked_fill,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           supports_out=False),
    OpInfo('masked_scatter',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_masked_scatter,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           supports_out=False,
           skips=(
           )),
    OpInfo('masked_select',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_masked_select,
           error_inputs_func=error_inputs_masked_select),
    OpInfo('matrix_exp',
           dtypes=floating_and_complex_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           aliases=('linalg.matrix_exp',),
           sample_inputs_func=sample_inputs_matrix_exp,
           # Needs to construct a 2nx2n matrix by copy_ ing into it
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           skips=(
               # times out
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           ),
           supports_out=False,
           ),
    OpInfo('matmul',
           aliases=('linalg.matmul',),
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16]
                                                       if (SM53OrLater and CUDA11OrLater) or TEST_WITH_ROCM else []),
           assert_autodiffed=True,
           assert_jit_shape_analysis=True,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_matmul,
           decorators=[
               # NVIDIA only assures that bfloat16 is supported by bmm if SM >= 5.3
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes', device_type='cuda', active_if=not SM53OrLater),
               # ROCm intermittently fails the test with standard atol/rtol
               DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-4, rtol=0)}),
                            'TestCommon', 'test_noncontiguous_samples', device_type='cuda',
                            active_if=TEST_WITH_ROCM),
               DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-4, rtol=0)}),
                            'TestCommon', 'test_out', device_type='cuda',
                            active_if=TEST_WITH_ROCM),
               # mv for the sample with shapes (S, S, M, M), (M,) has some variance in the
               # backward on CPU
               DecorateInfo(toleranceOverride({torch.float32: tol(atol=0, rtol=1e-5)}),
                            'TestCommon', 'test_noncontiguous_samples',
                            device_type='cpu'), ],
           skips=(
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # https://github.com/pytorch/pytorch/issues/67470
               DecorateInfo(unittest.skip("67470!"),
                            'TestCommon', 'test_noncontiguous_samples',
                            device_type='cpu', dtypes=(torch.long,)),
               # AssertionError: False is not true : Tensors failed to compare as equal!
               DecorateInfo(unittest.skip("Skipped!"), 'TestOpInfo',
                            device_type='xla', dtypes=(torch.long,)),
               # https://github.com/pytorch/pytorch/issues/71774
               DecorateInfo(unittest.skip('Skipped!'), 'TestNNCOpInfo', 'test_nnc_correctness',
                            device_type='cpu', dtypes=(torch.long,)),
           )),
    OpInfo('max',
           variant_test_name='reduction_with_dim',
           dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
           sample_inputs_func=sample_inputs_max_min_reduction_with_dim,
           supports_fwgrad_bwgrad=True,
           skips=(
           ),
           supports_forward_ad=True),
    OpInfo('max',
           variant_test_name='reduction_no_dim',
           dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_max_min_reduction_no_dim,
           skips=(
           )),
    OpInfo('median',
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16),
           # TODO: some signatures of median do support out
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=partial(sample_inputs_reduction, supports_multiple_dims=False)),
    OpInfo('nanmedian',
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16),
           # TODO: some signatures of nanmedian do support out
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=partial(sample_inputs_reduction, supports_multiple_dims=False)),
    OpInfo('var_mean',
           dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_std_var,
           # TODO: some signatures of var_mean do support out
           supports_out=False,
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True),
    OpInfo('std_mean',
           dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_std_var,
           # TODO: some signatures of std_mean do support out
           supports_out=False,
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True),
    OpInfo('meshgrid',
           variant_test_name='variadic_tensors',
           ref=np.meshgrid,
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.bool, torch.float16),
           sample_inputs_func=partial(sample_inputs_meshgrid, variant='variadic'),
           skips=[
               # JIT does not support variadic tensors.
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":252,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # meshgrid is defined in torch.functional to take a
               # variadic list of tensors. Variadic parameters are not
               # compatible with the normalize operator tests.
               DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # Skip operator schema test because this is a functional and not an operator
               DecorateInfo(unittest.skip("Skipped!"), 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
           ],
           supports_out=False,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,),
    OpInfo('meshgrid',
           variant_test_name='list_of_tensors',
           # Unlike the variant above, we do not use np.meshgrid as a
           # ref since it does not officially support list of numpy
           # arrays.
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.bool, torch.float16),
           sample_inputs_func=partial(sample_inputs_meshgrid, variant='list'),
           skips=[
               # meshgrid is defined in torch.functional to take a
               # variadic list of tensors. Variadic parameters are not
               # compatible with the normalize operator tests.
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
           ],
           assert_autodiffed=True,
           supports_out=False,
           autodiff_nonfusible_nodes=[],
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,),
    OpInfo('min',
           variant_test_name='reduction_with_dim',
           dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
           sample_inputs_func=sample_inputs_max_min_reduction_with_dim,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           skips=(
           )),
    OpInfo('min',
           variant_test_name='reduction_no_dim',
           dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_max_min_reduction_no_dim,
           skips=(
           )),
    OpInfo('quantile',
           dtypes=floating_types(),
           sample_inputs_func=sample_inputs_reduction_quantile,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           ),
           # See https://github.com/pytorch/pytorch/issues/66357
           # Relies on copy_ to broadcast, but the forward AD path calls broadcast_to which
           # does not have a batching rule in core
           check_batched_forward_grad=False),
    OpInfo('nanquantile',
           dtypes=floating_types(),
           sample_inputs_func=sample_inputs_reduction_quantile,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           ),
           # See https://github.com/pytorch/pytorch/issues/66357
           # Relies on copy_ to broadcast, but the forward AD path calls broadcast_to which
           # does not have a batching rule in core
           check_batched_forward_grad=False),
    BinaryUfuncInfo(
        'max',
        aliases=('maximum',),
        variant_test_name='binary',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        assert_autodiffed=True,
        ref=np.maximum,
        supports_rhs_python_scalar=False,
        skips=(
            # Incorrectly attempts to use a scalar for the second argument
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),
            # TODO: FIXME: RuntimeError: "max_elementwise_cuda" not implemented for 'ComplexFloat'
            DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion', device_type='cuda'),
        )),
    BinaryUfuncInfo(
        'maximum',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        ref=np.maximum,
        supports_rhs_python_scalar=False,
        skips=(
            # TODO: FIXME: RuntimeError: "max_elementwise_cuda" not implemented for 'ComplexFloat'
            DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion', device_type='cuda'),
        )),
    BinaryUfuncInfo(
        'min',
        aliases=('minimum',),
        variant_test_name='binary',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        assert_autodiffed=True,
        ref=np.minimum,
        supports_rhs_python_scalar=False,
        skips=(
            # Incorrectly attempts to use a scalar for the second argument
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),
            # TODO: FIXME: RuntimeError: "min_elementwise_cuda" not implemented for 'ComplexFloat'
            DecorateInfo(unittest.expectedFailure,
                         'TestBinaryUfuncs',
                         'test_type_promotion',
                         device_type='cuda'),
        )),
    BinaryUfuncInfo(
        'minimum',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        ref=np.minimum,
        supports_rhs_python_scalar=False,
        skips=(
            # TODO: FIXME: RuntimeError: "min_elementwise_cuda" not implemented for 'ComplexFloat'
            DecorateInfo(unittest.expectedFailure,
                         'TestBinaryUfuncs',
                         'test_type_promotion',
                         device_type='cuda'),
        ),
    ),
    BinaryUfuncInfo('logical_and',
                    ref=np.logical_and,
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    supports_autograd=False,
                    always_returns_bool=True,
                    supports_rhs_python_scalar=False),
    BinaryUfuncInfo('logical_or',
                    ref=np.logical_or,
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    supports_autograd=False,
                    always_returns_bool=True,
                    supports_rhs_python_scalar=False),
    BinaryUfuncInfo('logical_xor',
                    ref=np.logical_xor,
                    dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                    supports_autograd=False,
                    always_returns_bool=True,
                    supports_rhs_python_scalar=False,
                    skips=(
                    )),
    BinaryUfuncInfo('bitwise_and',
                    ref=np.bitwise_and,
                    dtypes=integral_types_and(torch.bool),
                    operator_variant=operator.and_,
                    inplace_operator_variant=operator.iand,
                    supports_autograd=False,
                    supports_one_python_scalar=True,
                    skips=(
                        # RuntimeError: "bitwise_and_cuda" not implemented for 'Half'
                        DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs',
                                     'test_type_promotion', device_type='cuda'),
                    )),
    BinaryUfuncInfo('bitwise_or',
                    ref=np.bitwise_or,
                    dtypes=integral_types_and(torch.bool),
                    operator_variant=operator.or_,
                    inplace_operator_variant=operator.ior,
                    supports_autograd=False,
                    supports_one_python_scalar=True,
                    skips=(
                        # TODO: FIXME: RuntimeError: "bitwise_or_cuda" not implemented for 'Half'
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion',
                                     device_type='cuda'),
                    )),
    BinaryUfuncInfo('bitwise_xor',
                    ref=np.bitwise_xor,
                    dtypes=integral_types_and(torch.bool),
                    operator_variant=operator.xor,
                    inplace_operator_variant=operator.ixor,
                    supports_autograd=False,
                    supports_one_python_scalar=True,
                    skips=(
                        # TODO: FIXME: RuntimeError: "bitwise_xor_cuda" not implemented for 'Half'
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion',
                                     device_type='cuda'),
                    )),
    BinaryUfuncInfo('heaviside',
                    ref=lambda a, b: (
                        # necessary because np.heaviside incorrectly returns float64 when passed args of dtype int64
                        np.int64(np.heaviside(a, b)) if a.dtype == np.int64 and b.dtype == np.int64 else np.heaviside(a, b)
                    ),
                    dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
                    supports_autograd=False,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # RuntimeError: heaviside is not yet implemented for tensors with different dtypes.
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion'),
                        # PyTorch's heaviside does not appear to propagate NaNs
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics_extremal_values'),
                    )),
    BinaryUfuncInfo('lcm',
                    ref=np.lcm,
                    dtypes=integral_types_and(),
                    supports_autograd=False,
                    supports_rhs_python_scalar=False),
    BinaryUfuncInfo('gcd',
                    ref=np.gcd,
                    dtypes=integral_types_and(),
                    supports_autograd=False,
                    supports_rhs_python_scalar=False,
                    skips=(
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics_small_values',
                                     dtypes=(torch.int8,)),)),
    BinaryUfuncInfo('isclose',
                    ref=np.isclose,
                    dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
                    sample_inputs_func=sample_inputs_isclose,
                    error_inputs_func=error_inputs_isclose,
                    supports_autograd=False,
                    supports_out=False,
                    supports_rhs_python_scalar=False,
                    skips=(
                        DecorateInfo(unittest.expectedFailure,
                                     'TestCommon',
                                     'test_numpy_refs', dtypes=(torch.complex128,)),
                        # RuntimeError: Short did not match Int
                        DecorateInfo(unittest.expectedFailure,
                                     'TestBinaryUfuncs',
                                     'test_type_promotion'),
                        DecorateInfo(unittest.skip("Skipped!"),
                                     'TestBinaryUfuncs',
                                     'test_reference_numerics_extremal_values'),
                    )),
    # `softmax` supports different dtypes based on whether `dtype` argument,
    # is passed or not. Hence two OpInfo entries, one with dtype and other without.
    # https://github.com/pytorch/pytorch/issues/68752
    OpInfo('softmax',
           aliases=('special.softmax', 'nn.functional.softmax',),
           aten_name='softmax',
           aten_backward_name='_softmax_backward_data',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_softmax_variant,
           assert_jit_shape_analysis=True,
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_out=True),
    OpInfo('softmax',
           aliases=('special.softmax', 'nn.functional.softmax',),
           variant_test_name="with_dtype",
           aten_name='softmax',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=partial(sample_inputs_softmax_variant, with_dtype=True),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_out=True),
    # `softmin` supports different dtypes based on whether `dtype` argument,
    # is passed or not. Hence two OpInfo entries, one with dtype and other without.
    # https://github.com/pytorch/pytorch/issues/68752
    OpInfo('nn.functional.softmin',
           aten_name='softmin',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_softmax_variant,
           assert_jit_shape_analysis=False,
           assert_autodiffed=False,
           supports_forward_ad=True,
           supports_out=False),
    OpInfo('nn.functional.softmin',
           variant_test_name="with_dtype",
           aten_name='softmin',
           dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
           sample_inputs_func=partial(sample_inputs_softmax_variant, with_dtype=True),
           assert_autodiffed=False,
           supports_forward_ad=True,
           supports_out=False),
    OpInfo(
        "nn.functional.cross_entropy",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_cross_entropy,
        supports_out=False,
        supports_forward_ad=True,
        decorators=(
            DecorateInfo(
                toleranceOverride({torch.float32: tol(atol=1e-5, rtol=1e-3)}),
                "TestJit",
                "test_variant_consistency_jit",
                device_type="cpu",
            ),
        ),
        skips=(
            # AssertionError: False is not true : Scalars failed to compare as equal! 0 != 1536
            # test_ops.TestJitCUDA.test_variant_consistency_jit_nn_functional_cross_entropy_cuda_float32 leaked
            # 1536 bytes CUDA memory on device 0
            DecorateInfo(
                unittest.expectedFailure,
                "TestJit",
                "test_variant_consistency_jit",
                device_type="cuda",
            ),
        )
    ),
    OpInfo('nn.functional.normalize',
           dtypes=floating_and_complex_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_normalize,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True),
    OpInfo('aminmax',
           ref=lambda x, dim=None, keepdim=False: (np.amin(x, axis=dim, keepdims=keepdim), np.amax(x, axis=dim, keepdims=keepdim)),
           dtypes=all_types_and(torch.bool),
           dtypesIfCUDA=all_types_and(torch.bool, torch.float16, torch.bfloat16),
           decorators=(onlyNativeDeviceTypes,),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_aminmax,
           error_inputs_func=error_inputs_aminmax_amax_amin,
           skips=(
               # AssertionError: Resizing an out= argument with no elements threw a resize warning!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out', device_type='cpu'),
           )),
    OpInfo('as_strided',
           op=lambda x, size, stride, storage_offset=0:
               torch.as_strided(x, size, stride, storage_offset=storage_offset),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_as_strided,
           skips=(
               # Note: This xfail is fine -- it's inherent to how as_strided works
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_noncontiguous_samples'),
               # AssertionError: False is not true : Scalars failed to compare as equal!
               DecorateInfo(unittest.skip("Errors when storage_offset is included"),
                            'TestCommon', 'test_variant_consistency_eager'),
               # Not close
               DecorateInfo(unittest.skip("Errors when storage_offset is included"),
                            'TestCommon', 'test_complex_half_reference_testing'),
               # Not close
               DecorateInfo(unittest.skip("Errors when storage_offset is included"), 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.skip("Errors when storage_offset is included"), 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.skip("Numerous errors"), 'TestGradients'))),
    OpInfo('as_strided_scatter',
           op=lambda x, src, size, stride, storage_offset=0:
               torch.as_strided_scatter(x, src, size, stride, storage_offset=storage_offset),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_as_strided_scatter,
           skips=(
               DecorateInfo(unittest.skip('Works only for CPU complex64'), 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.skip('Works for float64, fails for everything else'), 'TestMathBits', 'test_neg_view'),
               DecorateInfo(unittest.skip('Works for int64, fails for everything else'), 'TestCommon', 'test_noncontiguous_samples'),  # noqa: B950
               DecorateInfo(unittest.skip('Fails in most cases, passes on LAZY for some reason'), 'TestCommon', 'test_variant_consistency_eager'),  # noqa: B950
               DecorateInfo(unittest.skip('Only fails for LAZY, passes on everything else'), 'TestCompositeCompliance', 'test_backward'),  # noqa: B950
               DecorateInfo(unittest.skip('Passes on complex64 and float32 only'), 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip('Fails on cuda + rocm'), 'TestCommon', 'test_complex_half_reference_testing'),
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_grad'),
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_gradgrad'),
               DecorateInfo(unittest.skip('Passes on complex128 and float64 only'), 'TestGradients', 'test_fn_fwgrad_bwgrad'),)),
    OpInfo('native_layer_norm',
           aten_name='native_layer_norm',
           ref=reference_native_layer_norm,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           assert_jit_shape_analysis=True,
           sample_inputs_func=sample_inputs_native_layer_norm,
           error_inputs_func=error_inputs_native_layer_norm,
           skips=(
               # IndexError: tuple index out of range
               DecorateInfo(unittest.skip('Skipped!'), 'TestGradients', 'test_forward_mode_AD'),
               # Tests fail when weight=None and bias is defined
               # https://github.com/pytorch/pytorch/issues/79705
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_gradgrad'),
               # JIT test also tries to compute double backward, which fails
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # Extremal value issue on aten::native_layer_norm, which returns 'nan' for mean on 'inf' inputs
               # possibly because of the welford implementation.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           )),
    OpInfo('nn.functional.cosine_similarity',
           aten_name="cosine_similarity",
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_cosine_similarity),
    OpInfo('nn.functional.adaptive_avg_pool1d',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_avg_pool1d),
    OpInfo('nn.functional.adaptive_avg_pool2d',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           decorators=(
               # RuntimeError:
               # adaptive_avg_pool2d(Tensor input, int[2] output_size) -> (Tensor):
               # Expected a value of type 'List[int]' for argument 'output_size' but
               # instead found type 'Tuple[NoneType, int]'. :
               #   File "<string>", line 3
               # def the_method(i0):
               #     return torch.nn.functional.adaptive_avg_pool2d(i0, (None, 7))
               #            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ <--- HERE
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_avg_pool2d),
    OpInfo('nn.functional.adaptive_avg_pool3d',
           dtypes=floating_types_and(torch.half),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           decorators=(
               # RuntimeError:
               # adaptive_avg_pool3d(Tensor input, int[3] output_size) -> (Tensor):
               # Expected a value of type 'List[int]' for argument 'output_size' but
               # instead found type 'Tuple[NoneType, NoneType, NoneType]'. :
               #   File "<string>", line 3
               #
               # def the_method(i0):
               #     return torch.nn.functional.adaptive_avg_pool3d(i0, (None, None, None))
               #            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ <--- HERE
               #
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_avg_pool3d),
    OpInfo('nn.functional.adaptive_max_pool1d',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_max_pool1d),
    OpInfo('nn.functional.adaptive_max_pool2d',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           decorators=(
               # RuntimeError:
               # adaptive_max_pool2d(Tensor input, int[2] output_size) -> (Tensor):
               # Expected a value of type 'List[int]' for argument 'output_size' but
               # instead found type 'Tuple[NoneType, int]'. :
               #   File "<string>", line 3
               # def the_method(i0):
               #     return torch.nn.functional.adaptive_max_pool2d(i0, (None, 7))
               #            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ <--- HERE
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_max_pool2d),
    OpInfo('nn.functional.adaptive_max_pool3d',
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           decorators=(
               # RuntimeError:
               # adaptive_max_pool3d(Tensor input, int[3] output_size) -> (Tensor):
               # Expected a value of type 'List[int]' for argument 'output_size' but
               # instead found type 'Tuple[NoneType, NoneType, NoneType]'. :
               #   File "<string>", line 3
               #
               # def the_method(i0):
               #     return torch.nn.functional.adaptive_max_pool3d(i0, (None, None, None))
               #            ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~ <--- HERE
               #
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_adaptive_max_pool3d),
    OpInfo('nn.functional.avg_pool1d',
           aten_name='avg_pool1d',
           supports_autograd=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.int64, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_avgpool1d),
    OpInfo('nn.functional.avg_pool3d',
           aten_name='avg_pool3d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.int64),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_avgpool3d,
           skips=(
               # AssertionError: Tensor-likes are not close!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out', device_type='cpu'),
           )),
    OpInfo(
        "nn.functional.binary_cross_entropy_with_logits",
        aten_name="binary_cross_entropy_with_logits",
        supports_autograd=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
        sample_inputs_func=sample_inputs_binary_cross_entropy_with_logits,
        skips=(
            DecorateInfo(
                unittest.skip("Skipped!"),
                'TestJit',
                'test_variant_consistency_jit',
                dtypes=(torch.float32,)
            ),
        ),
    ),
    UnaryUfuncInfo(
        'nn.functional.relu',
        aten_name="relu",
        ref=lambda a: np.where(a <= 0, 0, a),
        supports_autograd=True,
        dtypes=all_types_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_nn_activation_relu,
        supports_out=False,
        supports_fwgrad_bwgrad=True,
        supports_forward_ad=True),
    OpInfo('nn.functional.conv_transpose1d',
           aten_name='conv_transpose1d',
           aliases=('conv_transpose1d',),
           dtypes=floating_types_and(torch.int64),
           dtypesIfCUDA=floating_types_and(torch.float16, *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_conv_transpose1d,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1e-04, rtol=1.3e-06), }),
                   'TestCommon', 'test_variant_consistency_eager', device_type='cuda')],
           skips=(
               # RuntimeError: !lhs.isAliasOf(rhs)INTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":104, please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False,),
    OpInfo('nn.functional.conv_transpose2d',
           aten_name='conv_transpose2d',
           aliases=('conv_transpose2d',),
           dtypes=floating_types_and(torch.int64),
           dtypesIfCUDA=floating_types_and(torch.float16,
                                           *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_conv_transpose2d,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1e-04, rtol=1.3e-06), }),
                   'TestCommon', 'test_variant_consistency_eager', device_type='cuda')],
           skips=(
               # RuntimeError: !lhs.isAliasOf(rhs)INTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":104, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False,),
    OpInfo('nn.functional.conv_transpose3d',
           aten_name='conv_transpose3d',
           aliases=('conv_transpose3d',),
           dtypes=floating_types_and(torch.int64),
           dtypesIfCUDA=floating_types_and(torch.float16, *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_conv_transpose3d,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           # Runs very slowly on slow-gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1e-04, rtol=1.3e-06), }),
                   'TestCommon', 'test_variant_consistency_eager', device_type='cuda'),
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=2e-04, rtol=2e-04), }),
                   'TestCompositeCompliance', 'test_operator', device_type='cuda'),
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1.3e-04, rtol=1.3e-06), }),
                   'TestCommon', 'test_noncontiguous_samples', device_type='cuda'),
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1e-04, rtol=2e-05), }),
                   'TestCompositeCompliance', 'test_forward_ad', device_type='cuda',
                   active_if=TEST_CUDNN)],
           skips=(
               # RuntimeError: !lhs.isAliasOf(rhs)INTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":104, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip("Skipped! 75029"), 'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
               DecorateInfo(unittest.skip("Skipped! 75363"), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
               DecorateInfo(unittest.skip("Skipped! RuntimeError: bias tensor has to be contiguous"), 'TestGradients',
                            'test_forward_mode_AD', device_type='cuda', active_if=TEST_WITH_ROCM),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad', device_type='cuda',
                            active_if=(not TEST_CUDNN)),
           ),
           supports_out=False,),
    OpInfo('nn.functional.conv1d',
           aliases=('conv1d',),
           aten_name='conv1d',
           dtypes=floating_and_complex_types_and(torch.int64, torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.chalf,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=sample_inputs_conv1d,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=(
               DecorateInfo(
                   toleranceOverride({torch.chalf: tol(atol=1e-2, rtol=1e-2)}),
                   'TestCommon', 'test_complex_half_reference_testing'
               ),
               DecorateInfo(
                   toleranceOverride({torch.chalf: tol(atol=1e-3, rtol=1e-3)}),
                   'TestCudaFuserOpInfo', 'test_nvfuser_correctness',
               ),
           ),
           skips=(
               # RuntimeError: !lhs.isAliasOf(rhs)INTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":103, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # Ref: https://github.com/pytorch/pytorch/issues/75309
               # AssertionError: None mismatch: torch.complex128 is not None
               DecorateInfo(unittest.expectedFailure, 'TestDtypeCustomRules',
                            'test_custom_rules', dtypes=(torch.complex64, torch.complex128)),
               # Ref: https://github.com/pytorch/pytorch/issues/75309
               # RuntimeError: UNSUPPORTED DTYPE: complex
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo',
                            'test_nnc_correctness', dtypes=(torch.complex64, torch.complex128)),
           ),
           supports_expanded_weight=True,
           supports_out=False,),
    OpInfo('nn.functional.conv2d',
           aliases=('conv2d',),
           aten_name='conv2d',
           dtypes=floating_and_complex_types_and(torch.int64, torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.chalf,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           sample_inputs_func=partial(sample_inputs_conv2d),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           decorators=(
               DecorateInfo(
                   toleranceOverride({torch.chalf: tol(atol=6e-2, rtol=5e-2)}),
                   'TestCommon', 'test_complex_half_reference_testing',
               ),
               DecorateInfo(
                   toleranceOverride({torch.chalf: tol(atol=1e-2, rtol=1e-2)}),
                   'TestCudaFuserOpInfo', 'test_nvfuser_correctness',
               ),
           ),
           skips=(
               # RuntimeError: !lhs.isAliasOf(rhs)INTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":103, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Works on some configs!"), 'TestJit', 'test_variant_consistency_jit'),
               # Ref: https://github.com/pytorch/pytorch/issues/75309
               # AssertionError: None mismatch: torch.complex128 is not None
               DecorateInfo(unittest.expectedFailure, 'TestDtypeCustomRules',
                            'test_custom_rules', dtypes=(torch.complex64, torch.complex128)),
               # RuntimeError: UNSUPPORTED DTYPE: complex
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo',
                            'test_nnc_correctness', dtypes=(torch.complex64, torch.complex128)),
           ),
           supports_expanded_weight=True,
           supports_out=False,),
    OpInfo('nn.functional.group_norm',
           aten_name='group_norm',
           aliases=('group_norm',),
           ref=reference_group_norm,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[
               # RuntimeError: Cannot insert a Tensor that requires grad as a constant.
               # Consider making it a parameter or input, or detaching the gradient
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,))
           ],
           sample_inputs_func=sample_inputs_group_norm,
           supports_expanded_weight=True,),
    OpInfo('nn.functional.instance_norm',
           # no ref because instance_norm will often have numerical instability (large numbers or nan)
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           decorators=[
               # RuntimeError: Cannot insert a Tensor that requires grad as a constant.
               # Consider making it a parameter or input, or detaching the gradient
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad',
                            active_if=TEST_WITH_ROCM)
           ],
           sample_inputs_func=sample_inputs_instance_norm,
           supports_expanded_weight=True,),
    OpInfo('nn.functional.layer_norm',
           aten_name='layer_norm',
           aten_backward_name='layer_norm_backward',
           aliases=('layer_norm',),
           ref=reference_layer_norm,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           assert_jit_shape_analysis=True,
           decorators=[
               DecorateInfo(
                   toleranceOverride({torch.float32: tol(atol=1e-05, rtol=1e-03)}),
                   'TestCommon', 'test_numpy_refs'
               )
           ],
           sample_inputs_func=sample_inputs_layer_norm,
           supports_expanded_weight=True,),
    OpInfo('nn.functional.local_response_norm',
           dtypes=floating_types_and(torch.int64, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[
               # RuntimeError: falseINTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185, please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
               DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                            'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
           ],
           sample_inputs_func=sample_inputs_local_response_norm,),
    OpInfo('constant_pad_nd',
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half),
           sample_inputs_func=sample_inputs_constant_pad_nd,
           supports_out=False,
           skips=(
               # bool can't be passed to Scalar arguments in JIT tracer because
               # BoolType is not a subtype of ScalarType.
               DecorateInfo(
                   unittest.expectedFailure, 'TestNNCOpInfo',
                   'test_nnc_correctness', dtypes=(torch.bool,)),
               DecorateInfo(
                   unittest.expectedFailure, 'TestCudaFuserOpInfo',
                   'test_nvfuser_correctness', dtypes=(torch.bool,)),
           )),
    OpInfo('nn.functional.pad',
           variant_test_name='constant',
           aten_name='constant_pad_nd',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half),
           sample_inputs_func=partial(sample_inputs_nn_pad, mode='constant'),
           supports_out=False),
    OpInfo('nn.functional.pad',
           variant_test_name='reflect',
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_and_complex_types(),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half),
           sample_inputs_func=partial(sample_inputs_nn_pad, mode='reflect'),
           skips=(
               # Doesn't have a corresponding aten operator.
               # RuntimeError: falseINTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
           ),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           supports_out=False),
    OpInfo('nn.functional.pad',
           variant_test_name='replicate',
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_and_complex_types(),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half),
           sample_inputs_func=partial(sample_inputs_nn_pad, mode='replicate'),
           skips=(
               # Doesn't have a corresponding aten operator.
               # RuntimeError: falseINTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
           ),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           supports_out=False),
    OpInfo('nn.functional.pad',
           variant_test_name='circular',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half),
           sample_inputs_func=partial(sample_inputs_nn_pad, mode='circular'),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_grad=False,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           skips=(
               # Doesn't have a corresponding aten operator.
               # RuntimeError: falseINTERNAL ASSERT FAILED at
               # "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185, please report a bug to PyTorch.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.float32,)),
           ),
           supports_out=False),
    OpInfo('nn.functional.hardswish',
           aten_name="hardswish",
           aten_backward_name='hardswish_backward',
           supports_autograd=True,
           assert_autodiffed=True,
           sample_inputs_func=sample_inputs_hardswish,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           supports_gradgrad=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           autodiff_nonfusible_nodes=["aten::hardswish"]),
    OpInfo('nn.functional.unfold',
           aten_name='im2col',
           aten_backward_name='im2col_backward',
           dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half),
           sample_inputs_func=sample_inputs_nn_unfold,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           skips=(
               # NOTE: this failure may not reproduce consistently on different systems
               # false INTERNAL ASSERT FAILED at "...torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185
               DecorateInfo(unittest.skip("Internal assert failed!"), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='nearest',
           supports_autograd=True,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           dtypes=floating_types_and(torch.uint8, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.uint8),
           sample_inputs_func=partial(sample_inputs_interpolate, 'nearest'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='linear',
           supports_autograd=True,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           sample_inputs_func=partial(sample_inputs_interpolate, 'linear'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='bilinear',
           supports_fwgrad_bwgrad=True,
           supports_autograd=True,
           supports_forward_ad=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=partial(sample_inputs_interpolate, 'bilinear'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='bicubic',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           sample_inputs_func=partial(sample_inputs_interpolate, 'bicubic'),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='trilinear',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=partial(sample_inputs_interpolate, 'trilinear'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.interpolate',
           aten_name="interpolate",
           variant_test_name='area',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=partial(sample_inputs_interpolate, 'area'),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo('nn.functional.upsample_bilinear',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=partial(sample_inputs_upsample, 'bilinear'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo(
        "nn.functional.soft_margin_loss",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        supports_out=False,
        supports_forward_ad=True,
        # doesn't support grad on target
        sample_inputs_func=partial(sample_inputs_loss, rhs_requires_grad=False),
    ),
    OpInfo('nn.functional.upsample_nearest',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.uint8, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.uint8),
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=partial(sample_inputs_upsample, 'nearest'),
           skips=(
               # RuntimeError: false
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":185,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           supports_out=False),
    OpInfo(
        "nn.functional.margin_ranking_loss",
        dtypes=all_types_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.bfloat16, torch.float16),
        supports_out=False,
        sample_inputs_func=sample_inputs_margin_ranking_loss,
        error_inputs_func=error_inputs_margin_ranking_loss,
        reference_inputs_func=reference_inputs_margin_ranking_loss,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True),
    OpInfo(
        "nn.functional.multi_margin_loss",
        dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        supports_out=False,
        supports_gradgrad=False,
        sample_inputs_func=sample_inputs_multi_margin_loss,
    ),
    OpInfo(
        "nn.functional.multilabel_margin_loss",
        dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        supports_out=False,
        supports_gradgrad=False,
        sample_inputs_func=sample_inputs_multilabel_margin_loss
    ),
    OpInfo('nn.functional.leaky_relu',
           aliases=None,
           aten_name="leaky_relu",
           aten_backward_name='leaky_relu_backward',
           sample_inputs_func=sample_inputs_leaky_relu,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_autograd=True,
           assert_autodiffed=True,
           supports_gradgrad=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           autodiff_nonfusible_nodes=["aten::leaky_relu"]),
    OpInfo(
        "nn.functional.multilabel_soft_margin_loss",
        supports_out=False,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_multilabel_soft_margin_loss,
        supports_forward_ad=True,
        decorators=(
            DecorateInfo(
                toleranceOverride({torch.float32: tol(atol=1e-4, rtol=1e-4)}),
                "TestJit",
                "test_variant_consistency_jit",
            ),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
        ),
        skips=(
            # AssertionError: False is not true : Scalars failed to compare as equal! 0 != 4096
            # __main__.TestJitCUDA.test_variant_consistency_jit_nn_functional_multilabel_soft_margin_loss_cuda_float32
            # leaked 4096 bytes CUDA memory on device 0
            DecorateInfo(
                # Skip instead of expectedFailure because this fails
                # locally for me but passes in CI.
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                device_type="cuda",
            ),
        ),
    ),
    OpInfo('nn.functional.avg_pool2d',
           aten_name='avg_pool2d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_types_and(torch.int64, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_avgpool2d,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out', device_type='cuda'),
           )),
    OpInfo('nn.functional.fractional_max_pool2d',
           supports_autograd=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           op=lambda input, *args, **kwargs:
               wrapper_set_seed(torch.nn.functional.fractional_max_pool2d, input, *args, **kwargs),
           # vmap does not support random operations
           check_batched_forward_grad=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           test_neg_view=False,
           sample_inputs_func=sample_inputs_fractional_max_pool2d,
           decorators=(
               # FIXME: AssertionError: False is not true : Tensors failed to compare as equal!
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'))),
    OpInfo('nn.functional.fractional_max_pool3d',
           supports_autograd=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           op=lambda input, *args, **kwargs:
               wrapper_set_seed(torch.nn.functional.fractional_max_pool3d, input, *args, **kwargs),
           # vmap does not support random operations
           check_batched_forward_grad=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           test_neg_view=False,
           sample_inputs_func=sample_inputs_fractional_max_pool3d,
           decorators=(
               # FIXME: both derivatives are implemented incorrectly
               # https://github.com/pytorch/pytorch/issues/69322
               # FIXME: AssertionError: False is not true : Tensors failed to compare as equal!
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),)),
    OpInfo('nn.functional.max_pool1d',
           aten_name='max_pool1d',
           supports_autograd=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           # TODO: add shape checks
           assert_jit_shape_analysis=False,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           skips=(
               # Pre-existing condition; Needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator', device_type='cpu'),
               DecorateInfo(unittest.skip("Works on some configs"), 'TestNNCOpInfo',
                            'test_nnc_correctness', dtypes=(torch.bfloat16,)),
               # RuntimeError: The tensor has a non-zero number of elements, but its data is not allocated yet.
               # Caffe2 uses a lazy allocation, so you will need to call mutable_data() or raw_mutable_data()
               # to actually allocate memory
               DecorateInfo(unittest.skip("Skipped!"), 'TestTags', 'test_tags'),
           ),
           sample_inputs_func=sample_inputs_max_pool),
    OpInfo('nn.functional.max_pool2d',
           aten_name='max_pool2d',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           # Vmap is not happy with non-contiguous (channels_last) inputs
           check_batched_gradgrad=False,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           assert_jit_shape_analysis=True,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_max_pool),
    OpInfo('nn.functional.max_pool3d',
           aten_name='max_pool3d',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # got: Batching rule not implemented for aten::flatten.using_ints
           check_batched_forward_grad=False,
           # TODO: add shape checks
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           # TODO: investigate nondeterminism
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           sample_inputs_func=sample_inputs_max_pool),
    OpInfo('nn.functional.max_unpool1d',
           aten_name='max_unpool1d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool,
           skips=(
               # Gradients are tested in `variant_test_name=grad` below.
               # We skip tests here because there is non-determinism in backward
               # with gather, when there are writes into the same memory location,
               # and if there are several indices pointing to the same memory,
               # gradcheck is oblivious about that and cannot perturb them all at once
               # (see sample_inputs_max_unpool_grad to find out more).
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_grad'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_forward_mode_AD'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad',
                            device_type='cpu'),
           )),
    OpInfo('nn.functional.max_unpool1d',
           variant_test_name='grad',
           aten_name='max_unpool1d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool_grad),
    OpInfo('nn.functional.max_unpool2d',
           aten_name='max_unpool2d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool,
           skips=(
               # Gradients are tested in `variant_test_name=grad` below.
               # We skip tests here because there is non-determinism in backward
               # with gather, when there are writes into the same memory location,
               # and if there are several indices pointing to the same memory,
               # gradcheck is oblivious about that and cannot perturb them all at once
               # (see sample_inputs_max_unpool_grad to find out more).
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_forward_mode_AD'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_grad'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
           )),
    OpInfo('nn.functional.max_unpool2d',
           variant_test_name='grad',
           aten_name='max_unpool2d',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # Vmap is not happy with non-contiguous (channels_last) inputs
           check_batched_grad=False,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool_grad),
    OpInfo('nn.functional.max_unpool3d',
           aten_name='max_unpool3d',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool,
           skips=(
               # Gradients are tested in `variant_test_name=grad` below.
               # We skip tests here because there is non-determinism in backward
               # with gather, when there are writes into the same memory location,
               # and if there are several indices pointing to the same memory,
               # gradcheck is oblivious about that and cannot perturb them all at once
               # (see sample_inputs_max_unpool_grad to find out more).
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_forward_mode_AD'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_grad'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
           )),
    OpInfo('nn.functional.max_unpool3d',
           variant_test_name='grad',
           aten_name='max_unpool3d',
           supports_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           assert_jit_shape_analysis=False,
           dtypes=floating_types(),
           dtypesIfCUDA=floating_types_and(torch.float16),
           sample_inputs_func=sample_inputs_max_unpool_grad),
    OpInfo('nn.functional.linear',
           aten_name='linear',
           supports_autograd=True,
           sample_inputs_func=sample_inputs_linear,
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfROCM=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16]
                                                       if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           backward_dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16]
                                                                if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           # linear calls mm under the hood which is nondeterministic on CUDA
           # https://pytorch.org/docs/stable/generated/torch.use_deterministic_algorithms.html#torch.use_deterministic_algorithms
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           supports_expanded_weight=True,
           decorators=(
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                            'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
           )),
    OpInfo('nn.functional.bilinear',
           aten_name='bilinear',
           supports_autograd=True,
           sample_inputs_func=sample_inputs_bilinear,
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16,
                                           *[torch.bfloat16] if (SM53OrLater and CUDA11OrLater) or TEST_WITH_ROCM else []),
           skips=(
               # NVIDIA only assures that bfloat16 is supported by bmm if SM >= 5.3
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes', device_type='cuda', active_if=not SM53OrLater),
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness', dtypes=(torch.bfloat16,)),
           ),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('nn.functional.glu',
           aten_name='glu',
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           sample_inputs_func=sample_inputs_glu,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfROCM=floating_types_and(torch.float16, torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           backward_dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    UnaryUfuncInfo(
        'nn.functional.elu',
        aten_backward_name='elu_backward',
        ref=lambda x, alpha=1.0, inplace=False:
            np.maximum(0., x) + np.minimum(0., alpha * (np.exp(x) - 1)),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        sample_kwargs=lambda device, dtype, input:
            ({'alpha': 0.8}, {'alpha': 0.8}),
        inplace_variant=lambda x, alpha=1.0:
            torch.nn.functional.elu(x, alpha, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-03, rtol=1.2e-03),
                    torch.bfloat16: tol(atol=1e-03, rtol=1.2e-03)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ), ],
    ),
    UnaryUfuncInfo(
        'nn.functional.prelu',
        aten_backward_name='prelu_backward',
        ref=lambda x, weight:
            np.maximum(0., x) + np.minimum(0., x) *
            (weight if x.ndim == 1 else weight.reshape([weight.size if i == 1 else 1 for i in range(0, x.ndim)])),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        # test_reference_numerics only tests the case when the weight tensor is a scalar
        sample_kwargs=sample_kwargs_prelu_scalar_weight,
        error_inputs_func=error_inputs_prelu,
        sample_inputs_func=sample_inputs_prelu,
        reference_inputs_func=reference_inputs_prelu,
        decorators=[
            # FIXME: second derivative is implemented but seems to be incorrect
            # https://github.com/pytorch/pytorch/issues/68760
            DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_gradgrad'),
            # RuntimeError: Cannot insert a Tensor that requires grad as a constant.
            # Consider making it a parameter or input, or detaching the gradient
            # https://github.com/pytorch/pytorch/issues/68752
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'), ],
    ),
    UnaryUfuncInfo(
        'nn.functional.celu',
        ref=lambda x, alpha=1.0, inplace=False:
            np.maximum(0., x) + np.minimum(0., alpha * (np.exp(x / alpha) - 1)),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        sample_kwargs=lambda device, dtype, input:
            ({'alpha': 0.8}, {'alpha': 0.8}),
        inplace_variant=lambda x, alpha=1.0:
            torch.nn.functional.celu(x, alpha, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-03, rtol=1.2e-03),
                    torch.bfloat16: tol(atol=1e-03, rtol=1.2e-03)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ), ],
    ),
    UnaryUfuncInfo(
        'nn.functional.rrelu',
        aten_backward_name='rrelu_with_noise_backward',
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.rrelu, input, *args, **kwargs),
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.rrelu, input, *args, inplace=True, **kwargs),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        gradcheck_wrapper=wrapper_set_seed,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        sample_kwargs=lambda device, dtype, input:
            (dict(lower=0., upper=1., training=True), dict(lower=0., upper=1., training=True)),
        sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs=dict(lower=0., upper=1., training=True)),
        decorators=(
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-03, rtol=1.2e-03),
                    torch.bfloat16: tol(atol=1e-03, rtol=1.2e-03)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ),),
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # In-place operations do not play well with forward AD
            # https://github.com/pytorch/pytorch/issues/77447
            DecorateInfo(unittest.expectedFailure, 'TestGradients',
                         'test_inplace_forward_mode_AD'),
            # The noise vector that's generated in these tests is not the same elementwise
            DecorateInfo(unittest.skip("Different noise"), 'TestUnaryUfuncs', 'test_batch_vs_slicing'),
            DecorateInfo(unittest.skip("Different noise"), 'TestUnaryUfuncs', 'test_contig_vs_every_other'),
            DecorateInfo(unittest.skip("Different noise"), 'TestUnaryUfuncs', 'test_non_contig_expand'),
            DecorateInfo(unittest.skip("Different noise"), 'TestUnaryUfuncs', 'test_contig_vs_transposed'),)),
    UnaryUfuncInfo(
        'nn.functional.selu',
        ref=lambda x, inplace=False:
            1.0507009873554804934193349852946 * (
                np.maximum(0., x) + np.minimum(0., 1.6732632423543772848170429916717 * (np.exp(x) - 1))
            ),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,  # depends on 'elu'
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        inplace_variant=lambda x: torch.nn.functional.selu(x, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-2, rtol=1.8e-2),
                    torch.bfloat16: tol(atol=1e-2, rtol=1.8e-2)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ), ],
    ),
    UnaryUfuncInfo(
        'nn.functional.silu',
        aten_backward_name='silu_backward',
        ref=lambda x, inplace=False: x / (1 + np.exp(-x)),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_autograd=True,
        supports_fwgrad_bwgrad=True,
        assert_autodiffed=True,
        supports_out=False,
        inplace_variant=lambda x: torch.nn.functional.silu(x, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-3, rtol=1e-3),
                    torch.bfloat16: tol(atol=1e-4, rtol=1e-4)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ), ],
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                         dtypes=(torch.cfloat,), device_type='cpu'),
        ),
        autodiff_nonfusible_nodes=["aten::silu"],
    ),
    # TODO: combine this with the nn.functional.silu OpInfo when
    # complex autodiff for silu is supported or when
    # the forward bug is fixed
    # Note: silu errors when given inputs that require grad
    #   but it doesn't support grad in their dtype
    #   This is why the dtypes list above passes test_dtypes,
    #   because it's getting lucky and failing in forward
    #   because test_dtypes sets requires_grad to True
    #   THIS IS A BUG
    UnaryUfuncInfo(
        'nn.functional.silu',
        variant_test_name='complex',
        ref=lambda x, inplace=False:
            x / (1 + np.exp(-x)),
        dtypes=complex_types(),
        dtypesIfCUDA=empty_types(),
        supports_forward_ad=False,
        supports_autograd=False,
        assert_autodiffed=False,
        supports_out=False,
        inplace_variant=lambda x: torch.nn.functional.silu(x, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({
                    torch.float16: tol(atol=1e-3, rtol=1e-3),
                    torch.bfloat16: tol(atol=1e-4, rtol=1e-4)
                }),
                'TestUnaryUfuncs', device_type='cuda',
            ), ],
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                         dtypes=(torch.cfloat,), device_type='cpu'),
            # FIXME: intentionally misreports dtypes
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_dtypes'),
            # FIXME: numpy reference diverges: Comparing (nan+nanj) and (-0+0j)
            DecorateInfo(unittest.skip("Skipped!"),
                         'TestUnaryUfuncs', 'test_reference_numerics_large',
                         dtypes=(torch.complex64, torch.cdouble)),
            DecorateInfo(unittest.skip("Skipped!"),
                         'TestUnaryUfuncs', 'test_reference_numerics_small',
                         dtypes=(torch.complex64,)),
            DecorateInfo(unittest.skip("Skipped!"),
                         'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                         dtypes=(torch.complex64,)))),
    UnaryUfuncInfo(
        'nn.functional.hardsigmoid',
        aten_backward_name='hardsigmoid_backward',
        ref=reference_hardsigmoid,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=False,
        supports_forward_ad=True,
        supports_out=False,
        inplace_variant=partial(torch.nn.functional.hardsigmoid, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({torch.float16: tol(atol=1e-04, rtol=0.001)}), 'TestUnaryUfuncs', device_type='cuda',), ],
        skips=[
            # still want to test that first derivative works though second derivative isn't supported
            DecorateInfo(unittest.expectedFailure, 'TestGradients', "test_inplace_gradgrad"),
            # produces 0 instead of nan on ROCM
            DecorateInfo(unittest.expectedFailure,
                         'TestUnaryUfuncs', "test_reference_numerics_extremal",
                         device_type='cuda',
                         active_if=(TEST_WITH_ROCM)), ]
    ),
    UnaryUfuncInfo(
        'nn.functional.logsigmoid',
        aten_name="log_sigmoid",
        aten_backward_name='log_sigmoid_backward',
        ref=reference_logsigmoid,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_autograd=True,
        assert_autodiffed=False,
        supports_forward_ad=True,
        supports_gradgrad=True,
        # autodiff_nonfusible_nodes=["aten::log_sigmoid"],
        decorators=[
            DecorateInfo(
                precisionOverride({torch.float16: 1e-2, torch.bfloat16: 5e-3}),
                'TestUnaryUfuncs', 'test_reference_numerics_small'),
            DecorateInfo(
                precisionOverride({torch.float16: 1e-2, torch.bfloat16: 5e-3}),
                'TestUnaryUfuncs', 'test_reference_numerics_large'),
            DecorateInfo(
                precisionOverride({torch.float16: 1e-2, torch.bfloat16: 5e-3}),
                'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
        ],
        skips=(
            # Resized a non-empty tensor but did not warn about it.
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning', device_type='cpu'),
        ),
    ),
    UnaryUfuncInfo(
        'nn.functional.mish',
        aten_backward_name='mish_backward',
        ref=lambda x: x * np.tanh(reference_softplus(x)),
        dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        inplace_variant=partial(torch.nn.functional.mish, inplace=True),
        decorators=[
            DecorateInfo(
                toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-03)}), 'TestUnaryUfuncs', device_type='cuda',), ],
    ),
    UnaryUfuncInfo(
        'nn.functional.softsign',
        ref=lambda x: x / (np.abs(x) + 1),
        dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16, torch.bool),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        decorators=[
            DecorateInfo(
                toleranceOverride({torch.float16: tol(atol=1e-03, rtol=1.3e-04)}), 'TestUnaryUfuncs',), ],
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                         dtypes=(torch.int, torch.int8)),
            # pytorch computes (0+nanj), numpy computes (-5e-18-1j) for input (-501.-1.0000e+20j)
            DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs',
                         "test_reference_numerics_large", dtypes=(torch.complex64,)),),
    ),
    UnaryUfuncInfo(
        'nn.functional.tanhshrink',
        ref=lambda x: x - np.tanh(x),
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_autograd=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        decorators=[
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                         device_type='cpu', dtypes=[torch.cfloat, torch.cdouble]),
            DecorateInfo(
                toleranceOverride({torch.bfloat16: tol(atol=1e-02, rtol=1.6e-02)}), 'TestUnaryUfuncs',),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
        ],
        skips=(
            # in each case, pytorch will produce a nan while numpy will not
            DecorateInfo(unittest.expectedFailure,
                         'TestUnaryUfuncs', "test_reference_numerics_small",
                         dtypes=(torch.complex64, torch.complex128), active_if=(IS_MACOS)),
            DecorateInfo(unittest.skip("Fails on some jobs works on others!"),
                         'TestUnaryUfuncs', "test_reference_numerics_large",
                         dtypes=(torch.complex64, torch.complex128), active_if=(IS_MACOS)),
            DecorateInfo(unittest.skip("Fails on some jobs works on others!"),
                         'TestUnaryUfuncs', "test_reference_numerics_extremal",
                         dtypes=(torch.complex64, torch.complex128), device_type='cpu',
                         active_if=(IS_MACOS or IS_WINDOWS)),
        ),
    ),
    UnaryUfuncInfo(
        'nn.functional.threshold',
        ref=lambda x, threshold, value: np.where(x <= threshold, value, x).astype(x.dtype),
        dtypes=all_types_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        assert_autodiffed=False,
        supports_gradgrad=True,
        supports_out=False,
        sample_kwargs=lambda device, dtype, input: ({'threshold': 0.123,
                                                    'value': -9},
                                                    {'threshold': 0.123,
                                                    'value': -9}),
        # TODO(whc) should not need sample_inputs_func, but without it
        # kwargs aren't being hooked up properly
        sample_inputs_func=sample_inputs_threshold,
    ),
    OpInfo(
        "nn.functional.triplet_margin_loss",
        sample_inputs_func=sample_inputs_triplet_margin_loss,
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
    ),
    OpInfo(
        "nn.functional.triplet_margin_with_distance_loss",
        sample_inputs_func=partial(sample_inputs_triplet_margin_loss, with_distance=True),
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # This test cannot handle a callable passed to `distance_function`. If we would use
            # `distance_function=None`, the test would pass fine.
            DecorateInfo(
                unittest.expectedFailure,
                "TestJit",
                "test_variant_consistency_jit",
            ),
            DecorateInfo(
                unittest.expectedFailure,
                "TestNormalizeOperators",
                "test_normalize_operator_exhaustive",
            ),
        ),
    ),
    BinaryUfuncInfo('nextafter',
                    dtypes=floating_types_and(torch.bfloat16),
                    supports_autograd=False,
                    supports_rhs_python_scalar=False),
    OpInfo('topk',
           dtypes=all_types_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bfloat16, torch.float16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           sample_inputs_func=sample_inputs_topk),
    # Multiple variants for batch_norm to test with and without cuDNN disabled
    # See https://github.com/pytorch/pytorch/pull/63218#discussion_r688549391 for more details
    OpInfo('nn.functional.batch_norm',
           aten_name='batch_norm',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           assert_jit_shape_analysis=True,
           sample_inputs_func=sample_inputs_batch_norm,
           skips=(
               # see https://github.com/pytorch/pytorch/issues/71286
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness'),
               DecorateInfo(unittest.skip('Skipped!'), 'TestNNCOpInfo', 'test_nnc_correctness',
                            device_type='cpu', dtypes=(torch.bfloat16,)),
               # see https://github.com/pytorch/pytorch/issues/76283
               DecorateInfo(unittest.skip("Fails on UBSAN!"), 'TestCompositeCompliance', 'test_forward_ad',
                            device_type="cpu"),
               # Trying to use forward AD with miopen_batch_norm that does not support it
               # because it has not been implemented yet.
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad',
                            device_type="cuda", active_if=TEST_WITH_ROCM),
               DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                            'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
           )),
    # This variant tests batch_norm with cuDNN disabled only on CUDA devices
    OpInfo('nn.functional.batch_norm',
           variant_test_name='without_cudnn',
           aten_name='batch_norm',
           dtypes=empty_types(),
           dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           decorators=[onlyCUDA, disablecuDNN],
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad',
                            device_type='cpu'),
               DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                            'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
           ),
           sample_inputs_func=sample_inputs_batch_norm),
    OpInfo(
        "nn.functional.binary_cross_entropy",
        aten_backward_name='binary_cross_entropy_backward',
        sample_inputs_func=sample_inputs_binary_cross_entropy,
        dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        gradcheck_fast_mode=False,
        supports_autograd=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        decorators=(
            # RuntimeError: expected int at position 0, but got: Tensor
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestCudaFuserOpInfo",
            ),
            # RuntimeError: expected int at position 0, but got: Tensor
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestNNCOpInfo",
                "test_nnc_correctness",
            ),
            DecorateInfo(
                toleranceOverride({torch.float32: tol(atol=1e-3, rtol=1e-3)}),
                "TestJit",
                "test_variant_consistency_jit",
            ),
        ),
        skips=(
            # RuntimeError: expected int at position 0, but got: Tensor
            DecorateInfo(
                unittest.expectedFailure,
                "TestJit",
                "test_variant_consistency_jit",
            ),
        ),
    ),
    # We have to add 2 OpInfo entry for `igamma` and `igammac`.First is the
    # standard entry, second is to run gradcheck tests on the second argument.
    BinaryUfuncInfo('igamma',
                    dtypes=floating_types_and(torch.bfloat16, torch.float16),
                    aliases=('torch.special.gammainc',),
                    dtypesIfCUDA=floating_types(),
                    # TODO: FIXME
                    supports_rhs_python_scalar=False,
                    supports_autograd=False,
                    skips=(
                        # FIXME: incorrectly tries to pass a rhs scalar
                        DecorateInfo(unittest.expectedFailure, 'TestJit',
                                     'test_jit_alias_remapping'),
                    )),
    # TODO: FIXME, ideally by implemented grad for both inputs
    # BinaryUfuncInfo('igamma',
    #                 variant_test_name='grad_other',
    #                 # Since autograd formula is implemented only for other and
    #                 # gradcheck test verifies the formula for input in SampleInput,
    #                 # we permute the arguments.
    #                 op=lambda self, other, **kwargs: torch.igamma(other, self, **kwargs),
    #                 inplace_variant=None,
    #                 method_variant=None,
    #                 supports_rhs_python_scalar=False,
    #                 rhs_make_tensor_kwargs=dict(requires_grad=False),
    #                 dtypes=floating_types_and(torch.bfloat16, torch.float16),
    #                 backward_dtypesIfCPU=floating_types_and(torch.bfloat16),
    #                 dtypesIfCUDA=floating_types(),
    #                 backward_dtypesIfCUDA=floating_types(),
    #                 supports_inplace_autograd=False,
    #                 skips=(
    #                     # Derivative wrt first tensor not implemented
    #                     DecorateInfo(unittest.expectedFailure, "TestCommon",
    #                                  "test_floating_inputs_are_differentiable"),"),
    #                     # test does not work with passing lambda for op
    #                     # AssertionError: False is not true : Tensors failed to compare as equal!
    #                     DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
    #                     # test fails are we permute the arguments function variant
    #                     # but not for inplace or method.
    #                     DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
    #                     # TypeError: igamma(): argument 'input' (position 1) must be Tensor, not float
    #                     DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs'),
    #                 )),
    BinaryUfuncInfo('igammac',
                    dtypes=floating_types_and(torch.bfloat16, torch.float16),
                    aliases=('torch.special.gammaincc',),
                    dtypesIfCUDA=floating_types(),
                    supports_autograd=False,
                    supports_rhs_python_scalar=False,
                    skips=(
                        # FIXME: incorrectly tries to pass a rhs scalar
                        DecorateInfo(unittest.expectedFailure, 'TestJit',
                                     'test_jit_alias_remapping'),
                    )),
    # TODO: FIXME, ideally by implementing grad for both inputs
    # BinaryUfuncInfo('igammac',
    #                 variant_test_name='grad_other',
    #                 # Since autograd formula is implemented only for other and
    #                 # gradcheck test verifies the formula for input in SampleInput,
    #                 # we permute the arguments
    #                 op=lambda self, other, **kwargs: torch.igammac(other, self, **kwargs),
    #                 inplace_variant=None,
    #                 method_variant=None,
    #                 supports_rhs_python_scalar=False,
    #                 rhs_make_tensor_kwargs=dict(requires_grad=False),
    #                 dtypes=floating_types_and(torch.bfloat16, torch.float16),
    #                 backward_dtypesIfCPU=floating_types_and(torch.bfloat16),
    #                 dtypesIfCUDA=floating_types(),
    #                 backward_dtypesIfCUDA=floating_types(),
    #                 supports_inplace_autograd=False,
    #                 decorators=[
    #                     # Derivative wrt first tensor not implemented
    #                     DecorateInfo(unittest.expectedFailure, "TestCommon",
    #                                  "test_floating_inputs_are_differentiable"),
    #                 ],
    #                 skips=(
    #                     # test does not work with passing lambda for op
    #                     # AssertionError: False is not true : Tensors failed to compare as equal!
    #                     DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
    #                     # test fails are we permute the arguments function variant
    #                     # but not for inplace or method.
    #                     DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
    #                     # TypeError: igammac(): argument 'input' (position 1) must be Tensor, not float
    #                     DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs'),
    #                 )),
    UnaryUfuncInfo('nn.functional.softshrink',
                   aten_name="softshrink",
                   aten_backward_name='softshrink_backward',
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=False,
                   sample_inputs_func=sample_inputs_softshrink,
                   error_inputs_func=error_inputs_softshrink),
    UnaryUfuncInfo('nn.functional.hardshrink',
                   aten_name="hardshrink",
                   aten_backward_name='hardshrink_backward',
                   dtypes=floating_types_and(torch.bfloat16,),
                   dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
                   assert_autodiffed=True,
                   sample_inputs_func=sample_inputs_hardshrink,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   autodiff_nonfusible_nodes=["aten::hardshrink"]),
    UnaryUfuncInfo('nn.functional.hardtanh',
                   aten_name="hardtanh",
                   aten_backward_name='hardtanh_backward',
                   dtypes=floating_types_and(torch.int8, torch.int16, torch.int32, torch.int64, torch.bfloat16),
                   backward_dtypes=all_types(),
                   dtypesIfCUDA=floating_types_and(torch.int8, torch.int16, torch.int32, torch.int64, torch.float16,
                                                   torch.bfloat16),
                   backward_dtypesIfCUDA=floating_types_and(torch.float16),
                   assert_autodiffed=True,
                   sample_inputs_func=sample_inputs_hardtanh,
                   supports_out=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   autodiff_nonfusible_nodes=["aten::hardtanh"]),
    OpInfo('nn.functional.gelu',
           aten_name="gelu",
           aten_backward_name='gelu_backward',
           ref=reference_gelu if TEST_SCIPY else None,
           error_inputs_func=error_inputs_gelu,
           supports_autograd=True,
           assert_autodiffed=True,
           sample_inputs_func=sample_inputs_gelu,
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           supports_gradgrad=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           autodiff_nonfusible_nodes=["aten::gelu"],
           skips=(
               # AssertionError: Tensor-likes are not close!
               # May not replicate in CI
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out'),)),
    UnaryUfuncInfo('nn.functional.relu6',
                   aten_name="relu6",
                   dtypes=all_types_and(torch.bfloat16),
                   backward_dtypes=floating_types(),
                   dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
                   backward_dtypesIfCUDA=floating_types_and(torch.float16),
                   assert_autodiffed=True,
                   supports_out=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   autodiff_nonfusible_nodes=["aten::relu6"]),
    OpInfo('mm',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16]
                                                       if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_mm,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
           )),
    OpInfo('mode',
           op=torch.mode,
           dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Resized a non-empty tensor but did not warn about it
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
           ),
           sample_inputs_func=sample_inputs_mode,),
    MvlGammaInfo(variant_test_name='mvlgamma_p_1',
                 domain=(1, None),
                 skips=skips_mvlgamma() + \
                 (DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                  DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                               dtypes=(torch.float16, torch.int8)),
                  DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                               dtypes=(torch.int8,)),),
                 sample_kwargs=lambda device, dtype, input: ({'p': 1}, {'d': 1})),
    MvlGammaInfo(variant_test_name='mvlgamma_p_3',
                 domain=(2, None),
                 skips=skips_mvlgamma(skip_redundant=True) + (
                     DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                     DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                  dtypes=(torch.float16, torch.int8)),
                     DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                  dtypes=(torch.int8,)),
                 ),
                 sample_kwargs=lambda device, dtype, input: ({'p': 3}, {'d': 3})),
    MvlGammaInfo(variant_test_name='mvlgamma_p_5',
                 domain=(3, None),
                 skips=skips_mvlgamma(skip_redundant=True) + (
                     DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                     DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                  dtypes=(torch.float16, torch.int8)),
                     DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                  dtypes=(torch.int8,)),
                 ),
                 sample_kwargs=lambda device, dtype, input: ({'p': 5}, {'d': 5})),
    BinaryUfuncInfo('ne',
                    ref=np.not_equal,
                    aliases=('not_equal',),
                    dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
                    always_returns_bool=True,
                    supports_autograd=False,
                    skips=(
                    )),
    OpInfo('narrow',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_narrow),
    UnaryUfuncInfo('neg',
                   aliases=('negative', ),
                   ref=np.negative,
                   dtypes=all_types_and_complex_and(torch.half, torch.bfloat16, torch.chalf),
                   error_inputs_func=error_inputs_neg,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True),
    OpInfo('dist',
           op=torch.dist,
           dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           # torch.autograd.gradcheck.GradcheckError: While computing batched gradients, got:
           # Could not allocate memory to change Tensor SizesAndStrides!
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_dist),
    OpInfo('outer',
           op=torch.outer,
           aliases=('ger', ),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_outer,),
    OpInfo('ormqr',
           op=torch.ormqr,
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_ormqr,
           error_inputs_func=error_inputs_ormqr,
           decorators=[skipCUDAIfNoCusolver, skipCPUIfNoLapack],
           skips=(
               # ormqr does not support forward when complex inputs require grad
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_dtypes'),
               # Strides are not the same!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
           )),
    OpInfo('permute',
           ref=np.transpose,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           assert_autodiffed=True,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           assert_jit_shape_analysis=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_permute,
           reference_inputs_func=reference_inputs_permute),
    BinaryUfuncInfo('pow',
                    dtypes=all_types_and_complex_and(torch.half, torch.bfloat16),
                    dtypesIfCUDA=all_types_and_complex_and(torch.half, torch.bfloat16, torch.chalf),
                    ref=np.power,
                    # Due to AVX2 curently not being fully supported for Float16, log_vml_cpu can't be enabled
                    # for Float16, causing this test to fail. pow's autograd for Float16 is thus currently
                    # unsupported on CPU.
                    backward_dtypes=floating_and_complex_types_and(torch.bfloat16),
                    backward_dtypesIfCUDA=floating_and_complex_types_and(torch.bfloat16, torch.half, torch.chalf),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_inplace_autograd=False,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    assert_autodiffed=True,
                    supports_one_python_scalar=True,
                    # Integer types do not support negative exponentes
                    rhs_make_tensor_kwargs=dict(low=0),
                    # Raising negative real numbers to fractional powers is not supported
                    lhs_make_tensor_kwargs=dict(low=0),
                    decorators=(
                        DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-4, rtol=1.3e-05)}),
                                     'TestBinaryUfuncs', 'test_reference_numerics'),
                        DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-4, rtol=1.3e-05),
                                                        torch.complex128: tol(atol=1e-4, rtol=1.3e-05)}),
                                     'TestBinaryUfuncs', 'test_scalar_support'),
                    ),
                    skips=(
                        # Skipping integers because they are being raised to negative powers causing an error
                        DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_reference_numerics_small_values',
                                     dtypes=[torch.int8, torch.int16, torch.int32, torch.int64]),
                        DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_reference_numerics_large_values',
                                     dtypes=[torch.int16, torch.int32, torch.int64]),
                        # FIXME Complex values error with: Greatest absolute difference: nan at index
                        # Ref: https://github.com/pytorch/pytorch/issues/76853
                        # For `chalf`, reference computation in `numpy` is computed in `cfloat`.
                        # Output of `chalf` saturates to `inf` quicker than reference due to its small range
                        # which leads to failure of this test.
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_batch_vs_slicing',
                                     dtypes=(torch.complex32,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_non_contig',
                                     dtypes=(torch.complex32,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics',
                                     dtypes=(torch.complex32,)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_small_values',
                                     dtypes=(torch.complex32, torch.complex64, torch.complex128)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_large_values',
                                     dtypes=(torch.complex32, torch.complex64, torch.complex128)),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_extremal_values',
                                     dtypes=(torch.complex32, torch.complex64, torch.complex128)),
                    )),
    BinaryUfuncInfo('float_power',
                    ref=np.float_power,
                    dtypes=all_types_and_complex_and(torch.half, torch.bfloat16, torch.bool),
                    promotes_int_to_float=True,
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_one_python_scalar=True,
                    # Integer types do not support negative exponentes
                    rhs_make_tensor_kwargs=dict(low=0),
                    # Raising negative real numbers to fractional powers is not supported
                    lhs_make_tensor_kwargs=dict(low=0),
                    decorators=(
                        DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-4, rtol=1.3e-05),
                                                        torch.complex128: tol(atol=1e-4, rtol=1.3e-05)}),
                                     'TestBinaryUfuncs', 'test_scalar_support'),
                    ),
                    skips=(
                        # FIXME
                        # AssertionError: Object comparison failed: torch.float64 != torch.float32
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
                        # -3.43399e+38 is outside the range of representable values of type 'float'
                        DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
                        # Complex values error with: Greatest absolute difference: nan at index
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_small_values',
                                     dtypes=[torch.complex64, torch.complex128]),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_large_values',
                                     dtypes=[torch.complex64, torch.complex128]),
                        DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_reference_numerics_extremal_values',
                                     dtypes=[torch.complex64, torch.complex128]),
                    )),
    OpInfo('qr',
           op=torch.qr,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_qr_geqrf,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # In-place ops
           check_batched_gradgrad=False,
           decorators=[skipCUDAIfNoCusolver, skipCPUIfNoLapack]),
    UnaryUfuncInfo('rad2deg',
                   ref=np.degrees,
                   decorators=(precisionOverride({torch.bfloat16: 7e-1,
                                                  torch.float16: 7e-1}),),
                   dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/51283#issuecomment-770614273
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16]),
                   ),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('real',
                   ref=np.real,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
                   supports_out=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   # See https://github.com/pytorch/pytorch/issues/66357
                   check_batched_forward_grad=False,
                   skips=(
                       # Skip since real and imag don't have out variants.
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_out_arg_all_dtypes'),
                   )),
    OpInfo(
        "roll",
        ref=np.roll,
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
        error_inputs_func=error_inputs_roll,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_roll,
        decorators=(onlyNativeDeviceTypes,),
    ),
    OpInfo(
        "rot90",
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half),
        error_inputs_func=error_inputs_rot90,
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_rot90,
    ),
    # To test reference numerics against multiple values of argument `decimals`,
    # we make multiple OpInfo entries with each entry corresponding to different value of decimals.
    UnaryUfuncInfo('round',
                   ref=np.round,
                   aliases=('special.round',),
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True,),
    UnaryUfuncInfo('round',
                   ref=np.round,
                   variant_test_name='decimals_0',
                   aliases=('special.round',),
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   sample_kwargs=lambda device, dtype, input: ({'decimals': 0}, {'decimals': 0}),
                   sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs={'decimals': 0}),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=False,
                   supports_sparse_csr=False),
    UnaryUfuncInfo('round',
                   ref=np.round,
                   variant_test_name='decimals_3',
                   aliases=('special.round',),
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   sample_kwargs=lambda device, dtype, input: ({'decimals': 3}, {'decimals': 3}),
                   sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs={'decimals': 3}),
                   skips=(
                       # test_ops already tested for this overload with `decimals_0` opinfo entry
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits'),
                   ),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=False,
                   supports_sparse_csr=False),
    UnaryUfuncInfo('round',
                   ref=np.round,
                   variant_test_name='decimals_neg_3',
                   aliases=('special.round',),
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                   sample_kwargs=lambda device, dtype, input: ({'decimals': -3}, {'decimals': -3}),
                   sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs={'decimals': -3}),
                   skips=(
                       # test_ops already tested for this overload with `decimals_0` opinfo entry
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits'),
                   ),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=False,
                   supports_sparse_csr=False),
    UnaryUfuncInfo('sin',
                   ref=np.sin,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   handles_large_floats=False,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Fails on CUDA but passes on ROCm
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.cdouble,), device_type='cuda'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=(torch.cfloat, torch.cdouble,), device_type='cpu', active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.cfloat, torch.cdouble,), device_type='cpu', active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   ),
                   decorators=(precisionOverride({torch.bfloat16: 1e-2}),)),
    UnaryUfuncInfo('sinc',
                   ref=np_sinc_with_fp16_as_fp32,
                   aliases=('special.sinc',),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                   handles_large_floats=False,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.bfloat16: 1e-2,
                                                  torch.float16: 1e-2}),),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/49133
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=[torch.cfloat]),
                   )),
    UnaryUfuncInfo('sinh',
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.sinh),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   decorators=(precisionOverride({torch.float16: 1e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.cdouble,)),
                       # Reference: https://github.com/pytorch/pytorch/issues/48641
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.int8]),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    UnaryUfuncInfo('sign',
                   ref=reference_sign,
                   dtypes=all_types_and(torch.bool, torch.bfloat16, torch.half),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.bfloat16, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/41245
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16, torch.float16, torch.float32, torch.float64]),
                   )),
    UnaryUfuncInfo('sgn',
                   ref=reference_sgn,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
                   backward_dtypes=floating_and_complex_types_and(torch.bfloat16, torch.half),
                   backward_dtypesIfCUDA=floating_and_complex_types_and(torch.bfloat16, torch.half, torch.chalf),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/41245
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16, torch.float16, torch.float32, torch.float64]),
                       # Reference: https://github.com/pytorch/pytorch/issues/53958
                       # Test fails in comparison on Nan as the `equal_nan` is True for
                       # comparing the CPU tensors.
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.complex64, torch.complex128]),
                       # Reference: https://github.com/pytorch/pytorch/issues/48486
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.complex64]),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    OpInfo('split',
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool, torch.chalf),
           sample_inputs_func=partial(sample_inputs_split, list_args=False),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           assert_autodiffed=True),
    OpInfo('split',
           # Cannot declare this aten_name because of
           # test_variant_consistency_jit_split_list_args_cpu_float32
           decomp_aten_name='split_with_sizes',
           variant_test_name='list_args',
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool),
           sample_inputs_func=partial(sample_inputs_split, list_args=True),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('split_with_sizes',
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool, torch.chalf),
           sample_inputs_func=sample_inputs_split_with_sizes,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_autodiffed=True),
    BinaryUfuncInfo('__radd__',
                    op=torch.Tensor.__radd__,
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool),
                    supports_out=False,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),

                    ),
                    assert_autodiffed=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    autodiff_nonfusible_nodes=['aten::add'],),
    BinaryUfuncInfo('__rdiv__',
                    op=torch.Tensor.__rdiv__,
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool),
                    promotes_int_to_float=True,
                    lhs_make_tensor_kwargs={'exclude_zero': True},
                    # Runs very slowly on slow gradcheck - alternatively reduce input sizes
                    gradcheck_fast_mode=True,
                    supports_out=False,
                    skips=(
                        # https://github.com/pytorch/pytorch/issues/76806
                        DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion'),
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
                    ),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    assert_autodiffed=True,
                    autodiff_nonfusible_nodes=['aten::mul', 'aten::reciprocal'],),
    BinaryUfuncInfo('__rmul__',
                    op=torch.Tensor.__rmul__,
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool),
                    supports_out=False,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
                    ),
                    assert_autodiffed=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    autodiff_nonfusible_nodes=['aten::mul'],),
    BinaryUfuncInfo('__rand__',
                    op=torch.Tensor.__rand__,
                    dtypes=integral_types_and(torch.bool),
                    supports_out=False,
                    supports_autograd=False,
                    supports_forward_ad=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                    )),
    BinaryUfuncInfo('__ror__',
                    op=torch.Tensor.__ror__,
                    dtypes=integral_types_and(torch.bool),
                    supports_out=False,
                    supports_autograd=False,
                    supports_forward_ad=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                    )),
    BinaryUfuncInfo('__rxor__',
                    op=torch.Tensor.__rxor__,
                    dtypes=integral_types_and(torch.bool),
                    supports_out=False,
                    supports_autograd=False,
                    supports_forward_ad=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                    )),
    OpInfo('__rmatmul__',
           op=torch.Tensor.__rmatmul__,
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16,
                                                       *[torch.bfloat16]
                                                       if (SM53OrLater and CUDA11OrLater) or TEST_WITH_ROCM else []),
           assert_autodiffed=True,
           sample_inputs_func=sample_inputs_matmul,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           decorators=(
               # NVIDIA only assures that bfloat16 is supported by bmm if SM >= 5.3
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes', device_type='cuda', active_if=not SM53OrLater),
               DecorateInfo(toleranceOverride({torch.complex64: tol(atol=1e-05, rtol=1.2e-03)}),
                            'TestMathBits', 'test_conj_view'),
               DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-05, rtol=1.2e-03)}),
                            'TestCommon', 'test_noncontiguous_samples'),
           ),
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
               # https://github.com/pytorch/pytorch/issues/67470
               DecorateInfo(unittest.skip("67470!"),
                            'TestCommon', 'test_noncontiguous_samples',
                            device_type='cpu', dtypes=(torch.long,)),
               # Fails on XLA.
               # AssertionError: False is not true : Tensors failed to compare as equal
               DecorateInfo(unittest.skip("Skipped!"), 'TestOpInfo', device_type='xla', dtypes=(torch.long,)),
               # https://github.com/pytorch/pytorch/issues/71774
               DecorateInfo(unittest.skip('Skipped!'), 'TestNNCOpInfo', 'test_nnc_correctness',
                            device_type='cpu', dtypes=(torch.long,)),
           )),
    BinaryUfuncInfo('__rmod__',
                    op=torch.Tensor.__rmod__,
                    dtypes=floating_types_and(torch.bfloat16, torch.half,),
                    dtypesIfCUDA=all_types_and(torch.bfloat16, torch.half),
                    # https://github.com/pytorch/pytorch/issues/80411
                    gradcheck_fast_mode=True,
                    supports_out=False,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_two_python_scalars=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
                    ),
                    # Support autograd after torch.remainder(Tensor, Tensor) supports
                    # autograd of the second argument.
                    # https://github.com/pytorch/pytorch/pull/58476/files#r637167630
                    # supports_autograd=False,
                    assert_autodiffed=True,
                    autodiff_nonfusible_nodes=['aten::remainder'],),
    BinaryUfuncInfo('__rpow__',
                    op=torch.Tensor.__rpow__,
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half),
                    # Reference: https://github.com/pytorch/pytorch/issues/54774
                    # "log2" "_vml_cpu" not implemented for Half
                    backward_dtypes=all_types_and_complex_and(torch.bfloat16),
                    backward_dtypesIfCUDA=all_types_and_complex_and(torch.bfloat16, torch.half),
                    supports_out=False,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
                        # TODO: FIXME tolerance is too high
                        DecorateInfo(unittest.skip('Skipped!'), 'TestGradients'),
                    ),
                    assert_autodiffed=True,
                    autodiff_nonfusible_nodes=['aten::pow'],),
    BinaryUfuncInfo('__rsub__',
                    op=torch.Tensor.__rsub__,
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_out=False,
                    supports_two_python_scalars=True,
                    skips=(
                        DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
                        DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit',),
                    ),
                    assert_autodiffed=True,
                    autodiff_nonfusible_nodes=['aten::rsub'],),
    BinaryUfuncInfo('rsub',
                    dtypes=all_types_and_complex_and(torch.bfloat16, torch.half),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_out=False,
                    supports_inplace_autograd=False,
                    assert_autodiffed=None,
                    sample_inputs_func=sample_inputs_add_sub),
    OpInfo('select',
           aten_backward_name='select_backward',
           dtypes=all_types_and_complex_and(torch.bfloat16, torch.half, torch.bool, torch.chalf),
           sample_inputs_func=sample_inputs_select,
           assert_jit_shape_analysis=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('select_scatter',
           dtypes=all_types_and(torch.bfloat16, torch.half, torch.bool),
           sample_inputs_func=sample_inputs_select_scatter,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    OpInfo('slice_scatter',
           dtypes=all_types_and(torch.bfloat16, torch.half, torch.bool),
           sample_inputs_func=sample_inputs_slice_scatter,
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_out=False),
    UnaryUfuncInfo('signbit',
                   ref=np.signbit,
                   dtypes=all_types_and(torch.bool, torch.bfloat16, torch.half),
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_autograd=False,),
    UnaryUfuncInfo('tan',
                   ref=np.tan,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cuda', dtypes=[torch.float64],
                                    active_if=TEST_WITH_ROCM),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   ),
                   # tan(pi/2 * odd_number) is nan
                   reference_numerics_filter=NumericsFilter(
                       condition=lambda x: close_to_int(x / (math.pi * 0.5)), safe_val=math.pi)),
    UnaryUfuncInfo('tanh',
                   ref=np.tanh,
                   aten_backward_name='tanh_backward',
                   aliases=('nn.functional.tanh',),
                   decorators=(precisionOverride({torch.bfloat16: 1e-2}),),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   assert_jit_shape_analysis=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.cfloat, torch.cdouble],
                                    active_if=(IS_MACOS or IS_WINDOWS)),
                       # alias, nn.functional.tanh, will produce (because of warning string saved):
                       # "RuntimeError: Expected to not find "tanh" but found it"
                       DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   ),
                   # tan(j * pi/2 * odd_number) is nan
                   reference_numerics_filter=NumericsFilter(
                       condition=lambda x: (close_to_int(x / (math.pi * 0.5j))
                                            if x.is_complex() else x.new_tensor(False, dtype=torch.bool)),
                       safe_val=0)),
    OpInfo('tensor_split',
           ref=np.array_split,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
           dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Pre-existing condition; Needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
           ),
           sample_inputs_func=sample_inputs_tensor_split,),
    OpInfo('hsplit',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.bfloat16, torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_hsplit,
           error_inputs_func=error_inputs_hsplit,),
    OpInfo('vsplit',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.bfloat16, torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_vsplit,
           error_inputs_func=error_inputs_vsplit,),
    OpInfo('dsplit',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.bfloat16, torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_dsplit,
           error_inputs_func=error_inputs_dsplit,),
    OpInfo('triangular_solve',
           op=torch.triangular_solve,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_legacy_solve,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_wrapper=lambda *args, **kwargs: gradcheck_wrapper_triangular_input(*args, idx=1, **kwargs),
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               # AssertionError: Scalars are not equal!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # Gradcheck fails
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_fwgrad_bwgrad',
                            dtypes=floating_and_complex_types()),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    UnaryUfuncInfo('trunc',
                   aliases=('fix', ),
                   ref=np.trunc,
                   dtypes=floating_types_and(torch.bfloat16),
                   dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True),
    UnaryUfuncInfo('exp2',
                   aliases=('special.exp2', ),
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.exp2),
                   dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('expm1',
                   aliases=('special.expm1', ),
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.expm1),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   assert_autodiffed=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/48926#issuecomment-739734774
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    UnaryUfuncInfo('nan_to_num',
                   ref=np.nan_to_num,
                   dtypes=all_types_and(torch.half, torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.half, torch.bool, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   ),
                   # Passing numpy_kwargs via sample_kwargs, as numpy does comparison
                   # with BFloat16 in float, since it currently doesn't support BFloat16.
                   # Ref: https://github.com/pytorch/pytorch/issues/57982#issuecomment-839150556
                   sample_kwargs=lambda device, dtype, input: ({},
                                                               {'posinf': torch.finfo(torch.bfloat16).max,
                                                                'neginf': torch.finfo(torch.bfloat16).min})
                   if dtype is torch.bfloat16 else ({}, {})),
    UnaryUfuncInfo('reciprocal',
                   ref=np_unary_ufunc_integer_promotion_wrapper(np.reciprocal),
                   dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/45690
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.cfloat, torch.cdouble]),
                       # Reference: https://github.com/pytorch/pytorch/pull/49102#issuecomment-744604601
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=[torch.bfloat16]),
                   )),
    UnaryUfuncInfo('rsqrt',
                   ref=lambda x: np.reciprocal(np.sqrt(x)),
                   domain=(0, None),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   decorators=(precisionOverride({torch.half: 5e-2}),),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=(torch.cfloat, torch.cdouble)),
                       # AssertionError: Tensor-likes are not close!
                       # Greatest absolute difference: nan at index (700,) (up to 0.01 allowed)
                       # Greatest relative difference: nan at index (700,) (up to 0.001 allowed)
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.chalf,)),
                   )),
    UnaryUfuncInfo('sqrt',
                   ref=np.sqrt,
                   supports_sparse=True,
                   domain=(0, None),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(
                       precisionOverride({torch.bfloat16: 7e-2}),
                       DecorateInfo(
                           toleranceOverride({torch.chalf: tol(atol=1e-2, rtol=0)}),
                           'TestUnaryUfuncs', 'test_reference_numerics_large'),
                   ),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/47358
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=(torch.cfloat, torch.cdouble),
                                    active_if=IS_MACOS),
                       # Reference: https://github.com/pytorch/pytorch/pull/47293#issuecomment-721774436
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=(torch.bfloat16,)),
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),
                   )),
    UnaryUfuncInfo('square',
                   ref=np.square,
                   dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
                   decorators=(precisionOverride({torch.complex64: 3e-4, torch.bfloat16: 3e-1}),),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/52549
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.cfloat, torch.cdouble]),
                       # >>> t = torch.tensor(complex(-0.01, float("inf")))
                       # >>> np.square(t.numpy())
                       # (-inf-infj)
                       # >>> t.square()
                       # tensor(-inf-infj)
                       # >>> t.cuda().square()
                       # tensor(inf+nanj, device='cuda:0')
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    device_type='cuda', dtypes=[torch.cfloat, torch.cdouble]),
                       # Reference: https://github.com/pytorch/pytorch/pull/52551#issuecomment-782596181
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16]),
                   ),),
    OpInfo('lerp',
           dtypes=floating_and_complex_types(),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16),
           dtypesIfROCM=floating_and_complex_types_and(torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_lerp,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_autodiffed=True),
    OpInfo('linalg.inv',
           aten_name='linalg_inv',
           op=torch.linalg.inv,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_invertible,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               # AssertionError: Scalars are not equal!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.inv_ex',
           aten_name='linalg_inv_ex',
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_invertible,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               # AssertionError: Scalars are not equal!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    UnaryUfuncInfo('angle',
                   ref=np.angle,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool),
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.bfloat16: 1e-2}),),
                   backward_dtypes=floating_and_complex_types_and(torch.bfloat16, torch.float16),
                   backward_dtypesIfCUDA=floating_and_complex_types_and(torch.chalf),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_complex_to_float=True,
                   skips=(
                       # Ref: https://github.com/pytorch/pytorch/issues/78413
                       DecorateInfo(unittest.expectedFailure, 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    dtypes=(torch.bfloat16, torch.float16, torch.float32, torch.float64),),
                   )),
    UnaryUfuncInfo('isfinite',
                   ref=np.isfinite,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
                   supports_out=False,
                   supports_autograd=False),
    UnaryUfuncInfo('isinf',
                   ref=np.isinf,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
                   supports_out=False,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_autograd=False),
    UnaryUfuncInfo('isposinf',
                   ref=np.isposinf,
                   dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_autograd=False),
    UnaryUfuncInfo('isneginf',
                   ref=np.isneginf,
                   dtypes=all_types_and(torch.bool, torch.bfloat16, torch.float16),
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_autograd=False),
    UnaryUfuncInfo('isreal',
                   ref=np.isreal,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
                   supports_out=False,
                   supports_autograd=False),
    UnaryUfuncInfo('isnan',
                   ref=np.isnan,
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16),
                   supports_out=False,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_autograd=False),
    OpInfo('linalg.solve',
           aten_name='linalg_solve',
           op=torch.linalg.solve,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_solve,
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.solve_ex',
           aten_name='linalg_solve_ex',
           op=torch.linalg.solve_ex,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_solve,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.solve_triangular',
           aten_name='linalg_solve_triangular',
           op=torch.linalg.solve_triangular,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_linalg_solve_triangular,
           supports_fwgrad_bwgrad=True,
           skips=(skipCPUIfNoLapack,),
           # linalg.solve_triangular cannot be batched over because of a call to out.copy_(result);
           supports_forward_ad=True),
    OpInfo('linalg.matrix_rank',
           aten_name='linalg_matrix_rank',
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_linalg_invertible,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           ),
           ),
    OpInfo('linalg.matrix_rank',
           aten_name='linalg_matrix_rank',
           variant_test_name='hermitian',
           dtypes=floating_and_complex_types(),
           supports_autograd=False,
           sample_inputs_func=sample_inputs_linalg_pinv_hermitian,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           ),
           ),
    OpInfo('linalg.pinv',
           aten_name='linalg_pinv',
           op=torch.linalg.pinv,
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_pinv,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               # errors with "leaked XXXX bytes CUDA memory on device 0"
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', device_type='cuda'),)
           ),
    OpInfo('linalg.pinv',
           aten_name='linalg_pinv',
           variant_test_name='singular',
           # pinv is Frechet-differentiable in a rank-preserving neighborhood,
           # so we feed inputs that are the products of two full-rank factors,
           # to avoid any rank changes caused by the perturbations in the gradcheck
           op=lambda a, b: torch.linalg.pinv(a @ b.mT),
           dtypes=floating_and_complex_types(),
           supports_out=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_linalg_pinv_singular,
           # Only large tensors show issues with implicit backward used prior to
           # explicit backward implementation.
           decorators=[slowTest, skipCUDAIfNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # CUDA runs out of memory
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_fwgrad_bwgrad',
                            device_type='cuda', dtypes=[torch.cdouble]),
               # This test takes almost 2 hours to run!
               DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad',
                            device_type='cuda', dtypes=[torch.cdouble]),
           )),
    OpInfo('linalg.pinv',
           aten_name='linalg_pinv',
           variant_test_name='hermitian',
           dtypes=floating_and_complex_types(),
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_linalg_pinv_hermitian,
           gradcheck_wrapper=gradcheck_wrapper_hermitian_input,
           decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )
           ),
    OpInfo('eig',
           op=torch.eig,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_eig,
           error_inputs_func=error_inputs_eig,
           decorators=[
               skipCUDAIfNoMagma,
               skipCPUIfNoLapack,
           ],
           ),
    OpInfo('einsum',
           # we need this lambda because SampleInput expects tensor input as the first argument
           # TODO(@heitorschueroff) update SampleInput to handle such cases
           op=lambda tensors, equation: torch.einsum(equation, tensors),
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.half,
                                                       *[torch.bfloat16] if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           backward_dtypesIfCUDA=floating_and_complex_types_and(torch.half, *[torch.bfloat16]
                                                                if ((SM60OrLater and CUDA11OrLater)
                                                                or TEST_WITH_ROCM) else []),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           # See https://github.com/pytorch/pytorch/issues/66357
           sample_inputs_func=sample_inputs_einsum,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # test does not work with passing lambda for op
               # there's a test `test_einsum` in `test_jit.py` to handle this case
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('svd',
           op=torch.svd,
           dtypes=floating_and_complex_types(),
           sample_inputs_func=sample_inputs_svd,
           # Runs very slowly on slow-gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           # We're using at::allclose, which does not have a batching rule
           check_batched_grad=False,
           check_batched_gradgrad=False,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.svd',
           op=torch.linalg.svd,
           aten_name='linalg_svd',
           decomp_aten_name='_linalg_svd',
           dtypes=floating_and_complex_types(),
           # Runs very slowly on slow-gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           check_batched_forward_grad=False,
           # We're using at::allclose, which does not have a batching rule
           check_batched_grad=False,
           check_batched_gradgrad=False,
           sample_inputs_func=sample_inputs_svd,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('linalg.svdvals',
           op=torch.linalg.svdvals,
           aten_name='linalg_svdvals',
           decomp_aten_name='_linalg_svd',
           dtypes=floating_and_complex_types(),
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           # We're using at::allclose, which does not have a batching rule
           check_batched_gradgrad=False,
           sample_inputs_func=sample_inputs_linalg_svdvals,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack, with_tf32_off]),
    OpInfo('svd_lowrank',
           op=lambda *args, **kwargs: wrapper_set_seed(
               lambda a, b, **kwargs: torch.svd_lowrank(a @ b.mT, **kwargs),
               *args, **kwargs
           ),
           dtypes=floating_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           supports_forward_ad=True,
           sample_inputs_func=sample_inputs_svd_lowrank,
           decorators=[skipCUDAIfNoCusolver, skipCPUIfNoLapack, with_tf32_off,
                       DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-03, rtol=1e-03)}),
                                    'TestCommon', 'test_noncontiguous_samples',
                                    device_type='cuda')],
           skips=(
               # test does not work with passing lambda for op
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('pca_lowrank',
           op=lambda *args, **kwargs: wrapper_set_seed(
               lambda a, b, **kwargs: torch.pca_lowrank(a @ b.mT, **kwargs),
               *args, **kwargs
           ),
           dtypes=floating_types(),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           check_batched_forward_grad=False,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_pca_lowrank,
           decorators=[skipCUDAIfNoCusolver, skipCPUIfNoLapack, with_tf32_off,
                       DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-03, rtol=1e-03)}),
                                    'TestCommon', 'test_noncontiguous_samples',
                                    device_type='cuda')],
           skips=(
               # test does not work with passing lambda for op
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           )),
    BinaryUfuncInfo('polar',
                    dtypes=floating_types(),
                    # this function is undefined if 'abs' values are <0
                    supports_forward_ad=True,
                    lhs_make_tensor_kwargs=dict(low=0),
                    supports_rhs_python_scalar=False,
                    skips=(
                        # RuntimeError: Expected object of scalar type Float but got scalar type Double for second argument
                        DecorateInfo(unittest.skip('Skipped!'), 'TestBinaryUfuncs', 'test_type_promotion'),
                        # GradcheckError: Jacobian computed with forward mode mismatch for output 0 with respect to input 0
                        # Numerical:
                        #  tensor([[0.]], dtype=torch.float64)
                        # Analytical:
                        # tensor([[-0.0047]], dtype=torch.float64, grad_fn=<CopySlices>)
                        DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_fwgrad_bwgrad'),
                    )),
    # TODO(@kshitij12345): Refactor similar to `mvlgamma` entries.
    # To test reference numerics against multiple values of argument `n`,
    # we make multiple OpInfo entries with each entry corresponding to different value of n (currently 0 to 4).
    # We run the op tests from test_ops.py only for `n=0` to avoid redundancy in testing.
    UnaryUfuncInfo('polygamma',
                   op=lambda x, n, **kwargs: torch.polygamma(n, x, **kwargs),
                   variant_test_name='polygamma_n_0',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
                   ),
                   sample_kwargs=lambda device, dtype, input: ({'n': 0}, {'n': 0})),
    # A separate OpInfo entry for special.polygamma is needed to reorder the arguments
    # for the alias. See the discussion here: https://github.com/pytorch/pytorch/pull/59691#discussion_r650261939
    UnaryUfuncInfo('special.polygamma',
                   op=lambda x, n, **kwargs: torch.special.polygamma(n, x, **kwargs),
                   variant_test_name='special_polygamma_n_0',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       # lambda impl
                       DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
                       DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
                   ),
                   sample_kwargs=lambda device, dtype, input: ({'n': 0}, {'n': 0}),
                   # polygamma functions have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    UnaryUfuncInfo('polygamma',
                   op=lambda x, n, **kwargs: torch.polygamma(n, x, **kwargs),
                   variant_test_name='polygamma_n_1',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       # Redundant tests
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       # Mismatch: https://github.com/pytorch/pytorch/issues/55357
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large'),
                   ),
                   sample_kwargs=lambda device, dtype, input: ({'n': 1}, {'n': 1}),
                   # polygamma functions have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    UnaryUfuncInfo('polygamma',
                   op=lambda x, n, **kwargs: torch.polygamma(n, x, **kwargs),
                   variant_test_name='polygamma_n_2',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       # Redundant tests
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       # Mismatch: https://github.com/pytorch/pytorch/issues/55357
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    active_if=TEST_WITH_ROCM),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    active_if=TEST_WITH_ROCM),),
                   sample_kwargs=lambda device, dtype, input: ({'n': 2}, {'n': 2}),
                   # polygamma functions have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    UnaryUfuncInfo('polygamma',
                   op=lambda x, n, **kwargs: torch.polygamma(n, x, **kwargs),
                   variant_test_name='polygamma_n_3',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       # Redundant tests
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       # Mismatch: https://github.com/pytorch/pytorch/issues/55357
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),),
                   sample_kwargs=lambda device, dtype, input: ({'n': 3}, {'n': 3}),
                   # polygamma functions have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    UnaryUfuncInfo('polygamma',
                   op=lambda x, n, **kwargs: torch.polygamma(n, x, **kwargs),
                   variant_test_name='polygamma_n_4',
                   ref=reference_polygamma if TEST_SCIPY else None,
                   decorators=(precisionOverride({torch.float16: 5e-4, torch.float32: 5e-4}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   sample_inputs_func=sample_inputs_polygamma,
                   skips=(
                       # Redundant tests
                       DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestJit'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestCommon'),
                       # Mismatch: https://github.com/pytorch/pytorch/issues/55357
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    active_if=TEST_WITH_ROCM),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    active_if=TEST_WITH_ROCM),),
                   sample_kwargs=lambda device, dtype, input: ({'n': 4}, {'n': 4}),
                   # polygamma functions have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    OpInfo('ravel',
           ref=np.ravel,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_ravel,
           ),
    OpInfo('reshape',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           sample_inputs_func=partial(sample_inputs_view_reshape, transpose_samples=True),
           reference_inputs_func=partial(reference_inputs_view_reshape, transpose_samples=True),
           error_inputs_func=error_inputs_reshape,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           ),
    OpInfo('reshape_as',
           op=lambda x, other: x.reshape_as(other),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           sample_inputs_func=sample_inputs_view_as_reshape_as,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           )),
    OpInfo('view',
           op=lambda x, shape: x.view(shape),
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           sample_inputs_func=partial(sample_inputs_view_reshape, transpose_samples=False),
           reference_inputs_func=partial(reference_inputs_view_reshape, transpose_samples=False),
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           )),
    OpInfo('view_as',
           op=lambda x, other: x.view_as(other),
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_view_as_reshape_as,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           )),
    OpInfo('atleast_1d',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_atleast1d2d3d,
           skips=(
               # JIT does not support variadic tensors.
               # RuntimeError: input->type()->kind() == TypeKind::OptionalType
               # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":252,
               # please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=[torch.float32]),
           ),
           ),
    OpInfo('atleast_2d',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=[torch.float32]),
           ),
           sample_inputs_func=sample_inputs_atleast1d2d3d,
           ),
    OpInfo('atleast_3d',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=[torch.float32]),
           ),
           sample_inputs_func=sample_inputs_atleast1d2d3d,
           ),
    OpInfo('flatten',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           ref=reference_flatten,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_flatten,
           reference_inputs_func=reference_inputs_flatten,
           ),
    OpInfo('unflatten',
           op=torch.unflatten,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_unflatten,
           ),
    OpInfo('column_stack',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),),
           sample_inputs_func=sample_inputs_column_stack,),
    OpInfo('pinverse',
           op=torch.pinverse,
           dtypes=floating_and_complex_types(),
           check_batched_grad=False,
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           supports_out=False,
           sample_inputs_func=sample_inputs_linalg_invertible,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           skips=(
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager',
                            device_type='mps', dtypes=[torch.float32]),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit',
                            device_type='mps', dtypes=[torch.float32]),
           )),
    OpInfo('gather',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_gather,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           error_inputs_func=error_inputs_gather,
           ),
    OpInfo('index_fill',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_index),
    OpInfo('index_copy',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           skips=(
           ),
           sample_inputs_func=sample_inputs_index,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    OpInfo('index_select',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           backward_dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.bfloat16, torch.chalf),
           sample_inputs_func=sample_inputs_index,
           error_inputs_func=error_inputs_index_select,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_jit_shape_analysis=True,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    OpInfo('index_add',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_index,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    OpInfo('index_reduce',
           dtypes=all_types_and(torch.float16, torch.bfloat16),
           supports_out=True,
           skips=(
               # Pre-existing condition (calls .item); needs to be fixed
               DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
           ),
           sample_inputs_func=sample_inputs_index_reduce),
    OpInfo('__getitem__',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_inplace_autograd=False,
           supports_scripting=False,
           op=torch.Tensor.__getitem__,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: False is not true : Scalars failed to compare as equal! 0 != 104448
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit', device_type='cuda'),),
           sample_inputs_func=sample_inputs_getitem),
    OpInfo('index_put',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_inplace_autograd=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           test_neg_view=False,
           sample_inputs_func=sample_inputs_index_put,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # RuntimeError: The following operation failed in the TorchScript interpreter.
               # Traceback of TorchScript (most recent call last):
               #   File "<string>", line 3, in forward
               # def the_method(i0, i1: List[torch.Tensor], i2):
               #     return torch.index_put(i0, i1, i2, accumulate=False)
               #            ~~~~~~~~~~~~~~~ <--- HERE
               # RuntimeError: a leaf Variable that requires grad is being used in an in-place operation.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('sort',
           dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_sort,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
           )),
    OpInfo('unique',
           dtypes=all_types_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bool, torch.float16),
           sample_inputs_func=sample_inputs_unique,
           supports_out=False,
           supports_autograd=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # 76571
               DecorateInfo(unittest.expectedFailure, 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values',
                            dtypes=(torch.float16, torch.float32, torch.float64)),
           )),
    OpInfo('unique_consecutive',
           dtypes=all_types_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bool, torch.float16),
           sample_inputs_func=sample_inputs_unique_consecutive,
           supports_out=False,
           supports_autograd=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # 76571
               DecorateInfo(unittest.expectedFailure, 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values',
                            dtypes=(torch.float16, torch.float32, torch.float64)),
           )),
    OpInfo('put',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_forward_grad=False,
           check_batched_gradgrad=False,  # vmap complains of the sizes
           sample_inputs_func=sample_inputs_put),
    OpInfo('take',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           check_batched_grad=False,  # vmap complains of the sizes
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_take,
           error_inputs_func=error_inputs_take),
    OpInfo('scatter',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_scatter,
           error_inputs_func=error_inputs_scatter_and_scatter_add),
    UnaryUfuncInfo(
        'bfloat16',
        op=lambda x, *args, **kwargs: x.bfloat16(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        skips=(
            # autograd tests don't handle operators that change dtype
            DecorateInfo(unittest.expectedFailure, 'TestGradients'),
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
        )),
    UnaryUfuncInfo(
        'bool',
        op=lambda x, *args, **kwargs: x.bool(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'byte',
        op=lambda x, *args, **kwargs: x.byte(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        # The autograd test runner cannot handle functions that change dtype
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'char',
        op=lambda x, *args, **kwargs: x.char(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        # The autograd test runner cannot handle functions that change dtype
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'double',
        op=lambda x, *args, **kwargs: x.double(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'float',
        op=lambda x, *args, **kwargs: x.float(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        skips=(
            # autograd tests don't handle operators that change dtype
            DecorateInfo(unittest.expectedFailure, 'TestGradients'),
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'half',
        op=lambda x, *args, **kwargs: x.half(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_autograd=True,
        skips=(
            # autograd tests don't handle operators that change dtype
            DecorateInfo(unittest.expectedFailure, 'TestGradients'),
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'int',
        op=lambda x, *args, **kwargs: x.int(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'long',
        op=lambda x, *args, **kwargs: x.long(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'short',
        op=lambda x, *args, **kwargs: x.short(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        supports_autograd=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
            # RuntimeError: attribute lookup is not defined on builtin
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        )),
    UnaryUfuncInfo(
        'chalf',
        op=lambda x, *args, **kwargs: x.chalf(*args, **kwargs),
        dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
        supports_out=False,
        sample_inputs_func=sample_inputs_conversion,
        skips=(
            # autograd tests don't handle operators that change dtype
            DecorateInfo(unittest.expectedFailure, 'TestGradients'),
            # use of lambda doesn't work with test_normalize_operator_exhaustive
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # RuntimeError: "sum_cpu" not implemented for 'ComplexHalf'
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_variant_consistency_eager',
                         device_type='cpu'),
            # TypeError: 'int' object is not iterable
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # RuntimeError: "sum_cpu" not implemented for 'ComplexHalf'
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view',
                         device_type='cpu'),
            # RuntimeError: "sum_cpu" not implemented for 'ComplexHalf'
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view',
                         device_type='cpu'),
            # RuntimeError: "sum_cpu" not implemented for 'ComplexHalf'
            # RuntimeError: "neg_conj_cuda" not implemented for 'ComplexHalf'
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
        )
    ),
    OpInfo('empty_like',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_like_fns,
           reference_inputs_func=reference_inputs_like_fns,
           supports_autograd=False,
           skips=(
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"),
                            "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_noncontiguous_samples'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_complex_half_reference_testing'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_non_standard_bool_values'),
               DecorateInfo(unittest.skip("Expected: empty_like is not comparable"), 'TestCompositeCompliance',
                            'test_operator'),
           )),
    OpInfo('zeros_like',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_like_fns,
           supports_autograd=False,
           skips=(
           )),
    OpInfo('ones_like',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_like_fns,
           supports_autograd=False,
           skips=(
           )),
    OpInfo('randn_like',
           dtypes=floating_types_and(torch.half, torch.bfloat16, torch.complex32, torch.complex64, torch.complex128),
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.randn_like, inp, *args, **kwargs),
           supports_out=False,
           sample_inputs_func=sample_inputs_like_fns,
           supports_autograd=False,
           supports_sparse_csr=True,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip("Expected: randn_like is not comparable between dtypes"),
                            'TestCommon', 'test_complex_half_reference_testing'),
           )),
    OpInfo('rand_like',
           dtypes=floating_types_and(torch.half, torch.bfloat16, torch.complex32, torch.complex64, torch.complex128),
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.randn_like, inp, *args, **kwargs),
           supports_out=False,
           sample_inputs_func=sample_inputs_like_fns,
           supports_autograd=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               DecorateInfo(unittest.skip("Expected: randn_like is not comparable between dtypes"),
                            'TestCommon', 'test_complex_half_reference_testing'),
           )),
    OpInfo('randint_like',
           dtypes=all_types_and(torch.half, torch.bfloat16),
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.randint_like, inp, *args, **kwargs),
           supports_out=False,
           sample_inputs_func=sample_inputs_randint_like,
           supports_autograd=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('full_like',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           supports_out=False,
           sample_inputs_func=sample_inputs_full_like,
           supports_autograd=False,
           skips=(
           )),
    OpInfo('new_zeros',
           op=lambda x, *args, **kwargs: x.new_zeros(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_new_fns,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           ),
           supports_autograd=False),
    OpInfo('new_ones',
           op=lambda x, *args, **kwargs: x.new_ones(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_new_fns,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           ),
           supports_autograd=False),
    OpInfo('new_empty',
           op=lambda x, *args, **kwargs: x.new_empty(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_new_fns,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_noncontiguous_samples'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_non_standard_bool_values'),
               DecorateInfo(unittest.skip("Expected: new_empty is not comparable"), 'TestCompositeCompliance',
                            'test_operator'),
               DecorateInfo(unittest.skip("Expected: new_empty is not comparable"),
                            'TestCommon', 'test_complex_half_reference_testing'),
           ),
           supports_autograd=False),
    OpInfo('empty',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           sample_inputs_func=sample_inputs_empty,
           supports_autograd=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_variant_consistency_eager'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_noncontiguous_samples'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_conj_view'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo', 'test_nnc_correctness'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
               # Empty tensor data is garbage so it's hard to make comparisons with it.
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_non_standard_bool_values'),
               DecorateInfo(unittest.skip("Expected: empty is not comparable"), 'TestCompositeCompliance',
                            'test_operator'),
               # requires_grad doesn't exist in the jit schema
               DecorateInfo(unittest.expectedFailure, 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
               DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                            'TestCommon',
                            'test_out'),
               DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                            'TestCommon',
                            'test_out_warning'),
               DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                            'TestLazyOpInfo'),
               DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                            'TestCommon', 'test_complex_half_reference_testing'),
           )),
    OpInfo('new_full',
           op=lambda x, *args, **kwargs: x.new_full(*args, **kwargs),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf),
           supports_out=False,
           sample_inputs_func=sample_inputs_new_full,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
           ),
           supports_autograd=False),
    OpInfo('multinomial',
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.multinomial, inp, *args, **kwargs),
           method_variant=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.Tensor.multinomial, inp, *args, **kwargs),
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half),
           supports_out=True,
           sample_inputs_func=sample_inputs_multinomial,
           error_inputs_func=error_inputs_multinomial,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Strides are not the same!
               # This may not be reproducible in CI
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_out'),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning')),
           supports_autograd=False),
    OpInfo('normal',
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.normal, inp, *args, **kwargs),
           # The inplace variant (Tensor.normal_) is different from torch.normal
           inplace_variant=None,
           dtypes=floating_types_and(torch.bfloat16, torch.half),
           dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.half),
           supports_out=True,
           sample_inputs_func=sample_inputs_normal_tensor_first,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Tensor-likes are not close!
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
               # Computed gradient is incorrect -- would be an exfail but gradgrad somehow passes
               DecorateInfo(unittest.skip("Gradients are incorrect!"), 'TestGradients'),)),
    OpInfo('normal',
           # This has its own variant b/c OpInfos assume the first arg is a Tensor but it is not here
           variant_test_name='number_mean',
           op=lambda std, mean, *args, **kwargs:
               wrapper_set_seed(torch.normal, mean, std, *args, **kwargs),
           # The inplace variant (Tensor.normal_) is different from torch.normal
           inplace_variant=None,
           dtypes=floating_types_and(torch.bfloat16, torch.half),
           dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.half),
           supports_out=True,
           sample_inputs_func=sample_inputs_normal_tensor_second,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # NotImplementedError not raised
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_fn_fwgrad_bwgrad'),
               # Computed gradient is incorrect -- would be an exfail but gradgrad somehow passes
               DecorateInfo(unittest.skip("Gradients are incorrect!"), 'TestGradients'),)),
    OpInfo('bernoulli',
           op=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.bernoulli, inp, *args, **kwargs),
           # The inplace variant (Tensor.bernoulli_) is different from torch.bernoulli
           inplace_variant=None,
           method_variant=lambda inp, *args, **kwargs:
               wrapper_set_seed(torch.Tensor.bernoulli, inp, *args, **kwargs),
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.half),
           supports_out=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_bernoulli,
           skips=(
               # vmap: We do not yet support calling random operations inside of vmap
               DecorateInfo(unittest.expectedFailure, 'TestGradients', 'test_forward_mode_AD'),
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # AssertionError: JIT Test does not execute any logic
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # Expected RuntimeError when doing an unsafe cast from a result of
               # dtype torch.float32 into an out= with dtype torch.lon
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'))),
    OpInfo('scatter_add',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           sample_inputs_func=sample_inputs_scatter_add,
           error_inputs_func=error_inputs_scatter_and_scatter_add,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           ),
    OpInfo('stack',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_stack,
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # https://github.com/pytorch/pytorch/issues/77046
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
               DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
           ),
           ),
    OpInfo('hstack',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_hstack_dstack_vstack,
           error_inputs_func=error_inputs_hstack_dstack_vstack,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           ),
    BinaryUfuncInfo('hypot',
                    dtypes=floating_types_and(torch.bfloat16),
                    dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_rhs_python_scalar=False),
    OpInfo('histogram',
           dtypes=floating_types(),
           dtypesIfCUDA=_dispatch_dtypes(),  # histogram is only implemented on CPU
           sample_inputs_func=sample_inputs_histogram,
           supports_autograd=False,
           skips=(
               # JIT tests don't work with Tensor keyword arguments
               # https://github.com/pytorch/pytorch/issues/58507
               # RuntimeError:
               # undefined value tensor:
               #   File "<string>", line 3
               # def the_method(i0):
               #     return torch.histogram(i0, 1, weight=tensor(-0.5735, dtype=torch.float32), density=False)
               #                                          ~~~~~~ <--- HERE
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # Not Implemented on XLA.
               DecorateInfo(unittest.skip("Skipped!"), 'TestOpInfo', device_type='xla'),
           )),
    OpInfo('histogramdd',
           dtypes=floating_types(),
           dtypesIfCUDA=_dispatch_dtypes(),  # histogramdd is only implemented on CPU
           sample_inputs_func=sample_inputs_histogramdd,
           supports_autograd=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # JIT tests don't work with Tensor keyword arguments
               # https://github.com/pytorch/pytorch/issues/58507
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('histc',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.int8, torch.int16, torch.int32, torch.int64),
           sample_inputs_func=sample_inputs_histc,
           supports_out=True,
           supports_autograd=False,
           skips=(
               # CUDA histc returns a float tensor but does not correctly warn when passed an integral out tensor
               # "AssertionError: RuntimeError not raised : Expected RuntimeError when doing an unsafe cast
               # from a result of dtype torch.float32 into an out= with dtype torch.long"
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out', device_type='cuda'),
               DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_extremal_values'),
           )),
    OpInfo('bincount',
           dtypes=integral_types_and(),
           sample_inputs_func=sample_inputs_bincount,
           supports_out=False,
           supports_autograd=False,
           skips=(
               # JIT tests don't work with Tensor keyword arguments
               # https://github.com/pytorch/pytorch/issues/58507
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('bucketize',
           dtypes=all_types_and(torch.float16, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16),
           sample_inputs_func=sample_inputs_bucketize,
           supports_autograd=False,
           skips=(
               # JIT tests don't work with Tensor keyword arguments
               DecorateInfo(unittest.skip("Expected failure!"), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('searchsorted',
           dtypes=all_types_and(torch.bfloat16, torch.float16),
           dtypesIfCUDA=all_types_and(torch.float16),
           sample_inputs_func=sample_inputs_searchsorted,
           supports_autograd=False,
           ref=reference_searchsorted,
           skips=(
               # JIT tests don't work with Tensor keyword arguments
               # https://github.com/pytorch/pytorch/issues/58507
               DecorateInfo(unittest.skip("Expected failure!"), 'TestJit', 'test_variant_consistency_jit'),
           )),
    OpInfo('cat',
           ref=_cat_np,
           aliases=('concat',),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.complex32),
           sample_inputs_func=sample_inputs_cat_concat,
           reference_inputs_func=reference_inputs_cat,
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           assert_autodiffed=True,
           skips=(
               # RuntimeError: Arguments for call not valid.
               #               Expected a value of type 'List[Tensor]' for argument
               #               'tensors' but instead found type 'Tensor (inferred)'.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),
               # see https://github.com/pytorch/pytorch/issues/71286
               DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness'),)),
    OpInfo('unbind',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           ref=reference_unbind,
           sample_inputs_func=sample_inputs_unbind,
           error_inputs_func=error_inputs_unbind,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_gradgrad=True,
           supports_out=False,
           ),
    OpInfo('vstack',
           aliases=('row_stack',),
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_hstack_dstack_vstack,
           error_inputs_func=error_inputs_hstack_dstack_vstack,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # RuntimeError: _fn() Expected a value of type
               #   'Tensor (inferred)' for argument 't0' but instead found type 'tuple'.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping'),)),
    OpInfo('dstack',
           dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_hstack_dstack_vstack,
           error_inputs_func=error_inputs_hstack_dstack_vstack,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           ),
    OpInfo('unfold',
           op=lambda x, *args: x.unfold(*args),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           backward_dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           check_batched_gradgrad=False,
           # See https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Skip operator schema test because this is a functional and not an operator
               DecorateInfo(unittest.expectedFailure, 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
           ),
           sample_inputs_func=sample_inputs_unfold),
    OpInfo('msort',
           dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
           check_batched_gradgrad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_msort,
           skips=(
           )),
    OpInfo('movedim',
           aliases=('moveaxis',),
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_movedim_moveaxis),
    OpInfo('renorm',
           dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           sample_inputs_func=sample_inputs_renorm,
           error_inputs_func=error_inputs_renorm),
    ShapeFuncInfo('repeat',
                  op=lambda x, dims: x.repeat(dims),
                  ref=np.tile,
                  dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
                  # https://github.com/pytorch/pytorch/issues/80411
                  gradcheck_fast_mode=True,
                  supports_out=False,
                  supports_forward_ad=True,
                  supports_fwgrad_bwgrad=True,
                  sample_inputs_func=sample_repeat_tile,
                  skips=(
                      DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
                  )),
    OpInfo('squeeze',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           assert_autodiffed=True,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           assert_jit_shape_analysis=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           # https://github.com/pytorch/pytorch/issues/66357
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_squeeze),
    UnaryUfuncInfo(
        'fill',
        op=_fill_aten,
        ref=_fill_np,
        method_variant=None,
        inplace_variant=torch.Tensor.fill_,
        sample_kwargs=_fill_sample_kwargs,
        sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs={'value': True}),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # https://github.com/pytorch/pytorch/issues/66357
        check_batched_forward_grad=False,
        dtypes=all_types_and_complex_and(torch.complex32, torch.bool, torch.float16, torch.bfloat16),
        supports_out=False,
        skips=(
            # JIT has issue when op is passed as lambda
            # AssertionError: JIT Test does not execute any logic
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.skip("No fill_ op"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("No fill_ op"), 'TestNNCOpInfo'),
        )),
    OpInfo('resize_',
           op=lambda x, shape: x.clone().resize_(shape),
           method_variant=None,
           inplace_variant=torch.Tensor.resize_,
           # the test fails because resize_ doesn't work with imag views as expected by the test
           # https://github.com/pytorch/pytorch/issues/65945
           test_neg_view=False,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_autograd=False,
           skips=(
               # Cannot resize variables that require grad
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_dtypes'),
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.skip("Allowed exception"), 'TestCompositeCompliance', 'test_operator'),
           ),
           sample_inputs_func=sample_inputs_resize_ops),
    OpInfo('resize_as_',
           op=lambda x, other: torch.resize_as_(x.clone(), other),
           method_variant=None,
           inplace_variant=torch.Tensor.resize_as_,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_out=False,
           supports_autograd=False,
           skips=(
               # Cannot resize variables that require grad
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_dtypes'),
               DecorateInfo(unittest.skip('Allowed exemption'), 'TestCompositeCompliance', 'test_operator'),
           ),
           sample_inputs_func=sample_inputs_resize_ops),
    OpInfo('take_along_dim',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           supports_inplace_autograd=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_take_along_dim,
           gradcheck_nondet_tol=GRADCHECK_NONDET_TOL),
    ShapeFuncInfo('tile',
                  ref=np.tile,
                  dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
                  # https://github.com/pytorch/pytorch/issues/80411
                  gradcheck_fast_mode=True,
                  supports_out=False,
                  supports_forward_ad=True,
                  supports_fwgrad_bwgrad=True,
                  sample_inputs_func=sample_repeat_tile),
    OpInfo('trapz',  # TODO: in the future, 'trapz' should be made a proper alias of 'trapezoid'
           dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_trapezoid),
    OpInfo('trapezoid',
           dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_trapezoid),
    OpInfo('cumulative_trapezoid',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.bfloat16, torch.float16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           supports_out=False,
           sample_inputs_func=sample_cumulative_trapezoid,),
    OpInfo('unsqueeze',
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           assert_jit_shape_analysis=True,
           assert_autodiffed=True,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           sample_inputs_func=sample_unsqueeze),
    BinaryUfuncInfo('xlogy',
                    aliases=('special.xlogy',),
                    dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                    promotes_int_to_float=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_one_python_scalar=True,
                    skips=(
                        # nan vs nan comparisons
                        # https://github.com/pytorch/pytorch/issues/74279
                        DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                    )),
    OpInfo('zero_',
           op=lambda x: torch.zero_(x.clone()),
           method_variant=None,
           inplace_variant=torch.Tensor.zero_,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           # https://github.com/pytorch/pytorch/issues/80411
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           supports_gradgrad=True,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
           ),
           sample_inputs_func=sample_inputs_zero_),
    BinaryUfuncInfo('special.xlog1py',
                    aten_name='special_xlog1py',
                    dtypes=all_types_and(torch.bool, torch.half, torch.bfloat16),
                    backward_dtypes=all_types_and(torch.bool, torch.bfloat16),
                    backward_dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                    promotes_int_to_float=True,
                    supports_forward_ad=True,
                    supports_fwgrad_bwgrad=True,
                    supports_one_python_scalar=True,
                    skips=(
                        # nan vs 0 comparisons
                        # https://github.com/pytorch/pytorch/issues/74279
                        DecorateInfo(unittest.skip("Skipped!"), 'TestGradients'),
                    )),
    BinaryUfuncInfo('special.zeta',
                    aten_name='special_zeta',
                    dtypes=all_types_and(torch.bool),
                    promotes_int_to_float=True,
                    supports_autograd=False,
                    supports_one_python_scalar=True),
    # TODO: FIXME
    # OpInfo entry to verify the gradient formula of `other`/`q`
    # BinaryUfuncInfo('special.zeta',
    #                 op=lambda q, x, **kwargs: torch.special.zeta(x, q, **kwargs),
    #                 aten_name='special_zeta',
    #                 variant_test_name='grad',
    #                 dtypes=all_types_and(torch.bool),
    #                 promotes_int_to_float=True,
    #                 supports_autograd=True,
    #                 supports_rhs_python_scalar=False,
    #                 decorators=[
    #                     # Derivative wrt first tensor not implemented
    #                     DecorateInfo(unittest.expectedFailure, "TestCommon",
    #                                  "test_floating_inputs_are_differentiable")
    #                 ],
    #                 skips=(
    #                     # Lambda doesn't work in JIT test
    #                     # AssertionError: JIT Test does not execute any logic
    #                     DecorateInfo(unittest.skip("Skipped!"), "TestJit", "test_variant_consistency_jit"),
    #                 )),
    OpInfo('logsumexp',
           aliases=('special.logsumexp',),
           dtypes=all_types_and(torch.bool, torch.bfloat16),
           dtypesIfCUDA=all_types_and(torch.bool, torch.bfloat16, torch.half),
           assert_autodiffed=True,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           gradcheck_fast_mode=False,
           sample_inputs_func=sample_inputs_logsumexp),
    OpInfo('trace',
           dtypes=all_types_and_complex(),
           dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half, torch.bfloat16),
           backward_dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16),
           error_inputs_func=error_inputs_trace,
           supports_inplace_autograd=False,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_trace),
    OpInfo('transpose',
           ref=_numpy_ref_transpose,
           aliases=('swapdims', 'swapaxes'),
           assert_jit_shape_analysis=True,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_transpose_swapdims),
    OpInfo('T',
           op=lambda x: x.T,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit"),),
           sample_inputs_func=sample_inputs_T),
    OpInfo('H',
           op=lambda x: x.H,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit"),),
           sample_inputs_func=sample_inputs_T),
    OpInfo('mT',
           op=lambda x: x.mT,
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit"),),
           sample_inputs_func=sample_inputs_adjoint),
    OpInfo('mH',
           op=lambda x: x.mH,
           aliases=('adjoint',),
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.half, torch.chalf),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit"),),
           sample_inputs_func=sample_inputs_adjoint),
    OpInfo('tril',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_tril_triu),
    OpInfo('triu',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.chalf, torch.bool, torch.half),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_tril_triu),
    OpInfo('kron',
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16),
           # Runs very slowly on slow gradcheck - alternatively reduce input sizes
           gradcheck_fast_mode=True,
           supports_inplace_autograd=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           sample_inputs_func=sample_inputs_kron),
    OpInfo('inner',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16]
                                                       if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           dtypesIfROCM=floating_and_complex_types_and(torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_inner,
           ),
    OpInfo('tensordot',
           dtypes=all_types_and_complex_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, *[torch.bfloat16]
                                                       if (CUDA11OrLater or TEST_WITH_ROCM) else []),
           dtypesIfROCM=floating_and_complex_types_and(torch.half, torch.bfloat16),
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           sample_inputs_func=sample_inputs_tensordot,
           skips=(
               # Skip operator schema test because this is a functional and not an operator.
               # Reference: https://github.com/pytorch/pytorch/issues/54574
               DecorateInfo(unittest.skip("Skipped!"), 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
           )
           ),
    OpInfo('to_sparse',
           op=lambda x, *args: x.to_sparse(*args),
           sample_inputs_func=sample_inputs_to_sparse,
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           backward_dtypes=floating_types(),
           backward_dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
           supports_out=False,
           supports_sparse_csr=True,
           supports_sparse_csc=True,
           check_batched_grad=False,
           check_batched_gradgrad=False,
           skips=(
               # to_sparse does not support automatic differentiation for outputs with complex dtype
               DecorateInfo(unittest.expectedFailure, 'TestGradients',
                            'test_nondifferentiable', dtypes=(torch.cdouble,)),
               # NotImplementedError: Could not run 'aten::normal_' with arguments from the 'SparseCPU' backend
               DecorateInfo(unittest.skip(""), 'TestCommon', 'test_noncontiguous_samples'),
               # TODO: FIXME: complex inputs requiring grad error in forward
               DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_dtypes'),
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               # Allowed exception: sparse tensors don't have strides
               DecorateInfo(unittest.skip("Allowed exception"), 'TestCompositeCompliance', 'test_operator'),
               DecorateInfo(unittest.skip("Allowed exception"), 'TestCompositeCompliance', 'test_backward'),
               DecorateInfo(unittest.skip("Allowed exception"), 'TestTags', 'test_tags'),
               # TODO: implement csr.to_sparse(sample_dim) where sampled_dim is 1.
               DecorateInfo(unittest.skip("csr.to_sparse(1) not implemented. Skipped!"),
                            'TestSparseCSR', 'test_sparse_csr_consistency'),
           )
           ),
    OpInfo('logcumsumexp',
           dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
           backward_dtypes=floating_types_and(torch.bfloat16),
           backward_dtypesIfCUDA=floating_types_and(torch.bfloat16),
           skips=(
               # AssertionError: UserWarning not triggered : Resized a non-empty tensor but did not warn about it.
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning', device_type='cuda'),
           ),
           sample_inputs_func=sample_inputs_logcumsumexp,
           error_inputs_func=error_inputs_logcumsumexp),
    UnaryUfuncInfo('sigmoid',
                   aliases=('special.expit', 'nn.functional.sigmoid'),
                   aten_backward_name='sigmoid_backward',
                   ref=reference_sigmoid if TEST_SCIPY else None,
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.complex64: 1e-1,
                                                  torch.bfloat16: 1e-2}),),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/issues/56012
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.complex64, torch.cdouble]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.chalf, torch.complex64, torch.cdouble]),
                       # alias, nn.functional.sigmoid, will produce (because of warning string saved):
                       # "RuntimeError: Expected to not find "sigmoid" but found it"
                       DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_jit_alias_remapping')),
                   dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and_complex_and(torch.complex32, torch.bool, torch.half, torch.bfloat16),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   assert_autodiffed=True,
                   # sigmoid(z) = 1 / (1 + exp(-z)), at z = j * pi * odd_number, the denominator is zero
                   reference_numerics_filter=NumericsFilter(
                       condition=lambda x: (close_to_int(x / (math.pi * 1j))
                                            if x.is_complex() else x.new_tensor(False, dtype=torch.bool)),
                       safe_val=0)),
    UnaryUfuncInfo('digamma',
                   ref=scipy.special.digamma if TEST_SCIPY else None,
                   aliases=('special.psi', 'special.digamma',),
                   decorators=(precisionOverride({torch.float16: 5e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('special.entr',
                   ref=scipy.special.entr if TEST_SCIPY else None,
                   aten_name='special_entr',
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.float16: 1e-1,
                                                  torch.bfloat16: 1e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.bfloat16, torch.float16]),
                   ),
                   supports_inplace_autograd=False,
                   sample_inputs_func=sample_inputs_entr),
    UnaryUfuncInfo('special.ndtri',
                   ref=scipy.special.ndtri if TEST_SCIPY else None,
                   domain=(0, 1),
                   aten_name='special_ndtri',
                   dtypes=all_types_and(torch.bool),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('special.log_ndtr',
                   aten_name='special_log_ndtr',
                   ref=scipy.special.log_ndtr if TEST_SCIPY else None,
                   dtypes=all_types_and(torch.bool),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   ),
    UnaryUfuncInfo('erf',
                   ref=scipy.special.erf if TEST_SCIPY else None,
                   aliases=('special.erf', ),
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.bfloat16: 1e-2}),),
                   skips=(
                       DecorateInfo(unittest.skip("Skipped! sparse backward not supported"),
                                    'TestSparseUnaryUfuncs', 'test_sparse_fn_grad'),

                   ),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   assert_jit_shape_analysis=True,
                   supports_sparse=True,
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('erfc',
                   ref=scipy.special.erfc if TEST_SCIPY else None,
                   aliases=('special.erfc', ),
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.bfloat16: 1e-2}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   assert_autodiffed=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    UnaryUfuncInfo('erfinv',
                   ref=scipy.special.erfinv if TEST_SCIPY else None,
                   aliases=('special.erfinv', ),
                   decorators=(precisionOverride({torch.float16: 1e-2,
                                                  torch.bfloat16: 1e-2,
                                                  torch.float32: 1e-4}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_sparse_csr=True,
                   supports_sparse_csc=True,
                   supports_sparse_bsr=True,
                   supports_sparse_bsc=True,
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   domain=(-1, 1),
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/49155#issuecomment-742664611
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    active_if=TEST_SCIPY and LooseVersion(scipy.__version__) < "1.4.0"),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    active_if=TEST_SCIPY and LooseVersion(scipy.__version__) < "1.4.0"),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    active_if=TEST_SCIPY and LooseVersion(scipy.__version__) < "1.4.0"),
                   )),
    OpInfo("nn.functional.smooth_l1_loss",
           ref=reference_smooth_l1_loss,
           sample_inputs_func=sample_inputs_smooth_l1_loss,
           dtypes=floating_types_and(torch.float16, torch.bfloat16),
           backward_dtypes=floating_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_types_and(torch.float16),
           backward_dtypesIfCUDA=floating_types_and(torch.float16),
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           skips=(
               # RuntimeError: input->type()->kind() == TypeKind::OptionalTypeINTERNAL ASSERT FAILED
               # at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270, please report a bug to PyTorch.
               DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit"),)),
    OpInfo(
        "nn.functional.l1_loss",
        ref=loss_reference_reduction_wrapper(lambda input, target: np.abs(input - target)),
        sample_inputs_func=sample_inputs_l1_loss,
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # RuntimeError: input->type()->kind() == TypeKind::OptionalTypeINTERNAL ASSERT FAILED
            # at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270, please report a bug to PyTorch.
            DecorateInfo(
                unittest.expectedFailure,
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32,),
            ),
        ),
    ),
    UnaryUfuncInfo('lgamma',
                   ref=reference_lgamma if TEST_SCIPY else None,
                   aliases=('special.gammaln', ),
                   decorators=(precisionOverride({torch.float16: 7e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   skips=(
                       # Reference: https://github.com/pytorch/pytorch/pull/50140#discussion_r552615345
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_small',
                                    device_type='cpu', dtypes=[torch.bfloat16]),
                       # Reference: https://github.com/pytorch/pytorch/pull/50140#issuecomment-756150214
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                                    dtypes=[torch.float32, torch.float64], active_if=IS_WINDOWS),
                       DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_large',
                                    dtypes=[torch.float32, torch.float64], active_if=IS_WINDOWS),
                   ),
                   # lgamma have multiple singularities at x <= 0
                   reference_numerics_filter=NumericsFilter(condition=lambda x: x < 0.1, safe_val=1)),
    OpInfo(
        'logdet',
        dtypes=floating_and_complex_types(),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_linalg_det_logdet_slogdet,
        decorators=[skipCUDAIfNoMagma, skipCPUIfNoLapack]),
    # `log_softmax` supports different dtypes based on whether `dtype` argument,
    # is passed or not. Hence two OpInfo entries, one with dtype and other without.
    OpInfo(
        'log_softmax',
        aliases=('special.log_softmax', 'nn.functional.log_softmax'),
        supports_out=True,
        aten_backward_name='_log_softmax_backward_data',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_softmax_variant,
        supports_forward_ad=True,
        assert_autodiffed=True),
    OpInfo(
        'log_softmax',
        variant_test_name='dtype',
        aliases=('special.log_softmax', 'nn.functional.log_softmax'),
        supports_out=True,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
        sample_inputs_func=partial(sample_inputs_softmax_variant, with_dtype=True),
        supports_forward_ad=True,
        assert_autodiffed=True),
    UnaryUfuncInfo('logit',
                   aten_backward_name='logit_backward',
                   ref=scipy.special.logit if TEST_SCIPY else None,
                   domain=(0, 1),
                   aliases=('special.logit', ),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True,
                   decorators=(precisionOverride({torch.bfloat16: 5e-1,
                                                  torch.float16: 5e-1}),),
                   dtypes=all_types_and(torch.bool, torch.bfloat16),
                   dtypesIfCUDA=all_types_and(torch.bool, torch.half, torch.bfloat16),
                   sample_inputs_func=sample_inputs_logit),
    OpInfo('where',
           # Currently only the `input` is tested in gradcheck.
           # If we pass `condition` first, none of the input which supports
           # autograd will be tested. Hence the following lambda.
           op=lambda self, condition, other: torch.where(condition, self, other),
           ref=lambda self, condition, other: np.where(condition, self, other),
           sample_inputs_func=sample_inputs_where,
           reference_inputs_func=reference_inputs_where,
           error_inputs_func=error_inputs_where,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           decorators=(
               DecorateInfo(onlyCUDA, "TestCommon", 'test_errors'),),
           skips=(
               # lambda impl
               DecorateInfo(unittest.expectedFailure, "TestNormalizeOperators", "test_normalize_operator_exhaustive"),
               DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
           ),
           dtypes=all_types_and_complex_and(torch.bool, torch.half, torch.bfloat16, torch.chalf)),
    OpInfo('nonzero',
           dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16, torch.float16, torch.chalf),
           sample_inputs_func=sample_inputs_nonzero,
           supports_autograd=False,
           skips=(
               DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
               # nonzero(): argument 'out' must be Tensor, not tuple
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # https://github.com/pytorch/pytorch/issues/67458
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
               # nonzero is not raising a warning when the out is resized
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
               # Can't find schemas for this operator for some reason
               DecorateInfo(unittest.expectedFailure, 'TestOperatorSignatures', 'test_get_torch_func_signature_exhaustive'),
           )),
    # Following tests are for jiterator's python interface
    # Jiterator can be used to author elementwise CUDA kernel
    # jiterator._create_jit_fn returns a callable that behaves like a regular pytorch op
    # See create_jit_fn in jiterator.py for more information
    UnaryUfuncInfo(
        'jiterator_unary',
        op=torch.cuda.jiterator._create_jit_fn("template <typename T> T unary(T x) { return x * x + x; }"),
        ref=lambda x: x * x + x,
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.bool),
        supports_out=False,
        supports_autograd=False,  # jiterator ops doesn't have backward defined
        decorators=[
            onlyCUDA,
            skipCUDAIfRocm,
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestUnaryUfuncs', 'test_reference_numerics_extremal'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestUnaryUfuncs', 'test_reference_numerics_hard'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestUnaryUfuncs', 'test_reference_numerics_normal'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestUnaryUfuncs', 'test_reference_numerics_small'),
        ],
        skips=(
            # Jiterator ops doesn't support neg or conj view
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # Jiterator ops doesn't suport CompositeCompliantTensor
            # Following test should expectedFailure, but it's causing cascading failures in CUDA, thus skipped
            DecorateInfo(unittest.skip("skip"), 'TestCompositeCompliance', 'test_operator'),
            # Skip reference_numerics tests for bool type, as the defined function doesn't work for bool
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_extremal',
                         dtypes=[torch.bool]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_hard',
                         dtypes=[torch.bool]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestUnaryUfuncs', 'test_reference_numerics_normal',
                         dtypes=[torch.bool]),
            # Expected failure: torch.jiterator_unary is not a valid op
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # Skip Nvfuser
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo'),
        )
    ),
    BinaryUfuncInfo(
        'jiterator_binary',
        op=torch.cuda.jiterator._create_jit_fn(
            "template <typename T> T binary(T x, T y, T alpha) { return x + alpha * y; }", alpha=1),
        ref=lambda input, other, *, alpha=1: np.add(input, other) if alpha == 1 \
            else np.add(input, np.multiply(alpha, other)),
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.bool),
        sample_inputs_func=partial(sample_inputs_jiterator, num_inputs=2, alpha=-3.14),
        supports_out=False,
        supports_autograd=False,  # jiterator ops doesn't have backward defined
        supports_rhs_python_scalar=False,
        decorators=[onlyCUDA, skipCUDAIfRocm],
        skips=(
            # Jiterator ops doesn't support neg or conj view
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # Jiterator ops doesn't suport CompositeCompliantTensor
            # Following test should expectedFailure, but it's causing cascading failures in CUDA, thus skipped
            DecorateInfo(unittest.skip("skip"), 'TestCompositeCompliance', 'test_operator'),
            # Expected failure: torch.jiterator_binary is not a valid op
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # Skip Nvfuser
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo'),
        )
    ),
    OpInfo(
        'jiterator_4inputs_with_extra_args',
        op=torch.cuda.jiterator._create_jit_fn(
            "template <typename T> T binary(T i0, T i1, T i2, T i3, T alpha, T beta) { return alpha * i0 + beta * i1 + i2 + i3; }",
            alpha=1, beta=1),
        ref=lambda i0, i1, i2, i3, *, alpha=1, beta=1: alpha * i0 + beta * i1 + i2 + i3,
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.bool),
        sample_inputs_func=partial(sample_inputs_jiterator, num_inputs=4, alpha=3.14, beta=-4.20),
        supports_out=False,
        supports_autograd=False,  # jiterator ops doesn't have backward defined
        decorators=[onlyCUDA, skipCUDAIfRocm],
        skips=(
            # Jiterator ops doesn't support neg or conj view
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # Jiterator ops doesn't suport CompositeCompliantTensor
            # Following test should expectedFailure, but it's causing cascading failures in CUDA, thus skipped
            DecorateInfo(unittest.skip("skip"), 'TestCompositeCompliance', 'test_operator'),
            # Expected failure: torch.jiterator_4inputs_with_extra_args is not a valid op
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # Skip Nvfuser
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo'),
        )
    ),
    BinaryUfuncInfo(
        'jiterator_binary_return_by_ref',
        op=torch.cuda.jiterator._create_multi_output_jit_fn(
            """
            template <typename T>
            T binary_return_by_ref(T i0, T i1, T& out0) {
                out0 = i0 + i1;
            }
            """,
            num_outputs=1),
        ref=lambda i0, i1: i0 + i1,
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.bool),
        sample_inputs_func=partial(sample_inputs_jiterator, num_inputs=2, alpha=-0.42),
        supports_out=False,
        supports_autograd=False,  # jiterator ops doesn't have backward defined
        supports_rhs_python_scalar=False,
        decorators=[onlyCUDA, skipCUDAIfRocm],
        skips=(
            # Jiterator ops doesn't support neg or conj view
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # Jiterator ops doesn't suport CompositeCompliantTensor
            # Following test should expectedFailure, but it's causing cascading failures in CUDA, thus skipped
            DecorateInfo(unittest.skip("skip"), 'TestCompositeCompliance', 'test_operator'),
            # Expected failure: torch.jiterator_4inputs_with_extra_args is not a valid op
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # Skip Nvfuser
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo'),
        )
    ),
    OpInfo(
        'jiterator_2inputs_2outputs',
        op=torch.cuda.jiterator._create_multi_output_jit_fn(
            """
            template <typename T>
            T binary_2outputs(T i0, T i1, T& out0, T& out1) {
                out0 = i0 + i1;
                out1 = i0 - i1;
            }
            """,
            num_outputs=2),
        ref=lambda i0, i1, *, alpha=1: (i0 + i1, i0 - i1),
        dtypes=all_types_and_complex_and(torch.bfloat16, torch.float16, torch.bool),
        sample_inputs_func=partial(sample_inputs_jiterator, num_inputs=2),
        supports_out=False,
        supports_autograd=False,  # jiterator ops doesn't have backward defined
        decorators=[onlyCUDA, skipCUDAIfRocm],
        skips=(
            # Jiterator ops doesn't support neg or conj view
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # Jiterator ops doesn't suport CompositeCompliantTensor
            # Following test should expectedFailure, but it's causing cascading failures in CUDA, thus skipped
            DecorateInfo(unittest.skip("skip"), 'TestCompositeCompliance', 'test_operator'),
            # Expected failure: torch.jiterator_4inputs_with_extra_args is not a valid op
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # Skip Nvfuser
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo'),
        )
    ),
    # `torch.norm` has multiple code paths depending on the value of `p`.
    # These paths have different dtype support. Also JIT supports,
    # most variants but not all of them. So we split the OpInfo entries,
    # for `norm` based on the code-paths and JIT support.
    OpInfo(
        "norm",
        sample_inputs_func=sample_inputs_norm,
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # AssertionError: RuntimeError not raised : Expected RuntimeError when doing an unsafe cast from a result
            # of dtype torch.float32 into an out= with dtype torch.long
            DecorateInfo(
                unittest.expectedFailure,
                "TestCommon",
                "test_out",
                device_type="meta",
            ),
        ),
    ),
    OpInfo('norm',
           variant_test_name='nuc',
           aten_name='nuclear_norm',
           sample_inputs_func=sample_inputs_norm_nuc,
           decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack],
           check_batched_gradgrad=False,
           # torch.autograd.gradcheck.GradcheckError: While computing batched gradients
           # got: Could not allocate memory to change Tensor SizesAndStrides!
           check_batched_forward_grad=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           dtypes=floating_and_complex_types(),
           dtypesIfCUDA=floating_and_complex_types(),
           skips=(
               # RuntimeError not raised :
               # Expected RuntimeError when calling with input.device=cpu and out.device=cuda
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # RuntimeError:
               # Arguments for call are not valid.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.complex64, torch.float32,)),  # noqa: B950
           )
           ),
    OpInfo('norm',
           variant_test_name='fro',
           aten_name='frobenius_norm',
           sample_inputs_func=sample_inputs_norm_fro,
           dtypes=floating_and_complex_types_and(torch.bfloat16),
           dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.bfloat16),
           supports_forward_ad=True,
           # torch.autograd.gradcheck.GradcheckError: While computing batched gradients
           # got: Could not allocate memory to change Tensor SizesAndStrides!
           check_batched_forward_grad=False,
           supports_fwgrad_bwgrad=True,
           skips=(
               # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
               DecorateInfo(
                   unittest.skip("Skipped!"),
                   'TestSchemaCheckModeOpInfo',
                   'test_schema_correctness',
                   dtypes=(torch.complex64, torch.complex128)),
               # Expected RuntimeError when calling with input.device=cpu and out.device=cuda
               DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),
               # Arguments for call are not valid.
               DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit', dtypes=(torch.complex64, torch.float32,)),  # noqa: B950
           )),
    OpInfo(
        "norm",
        variant_test_name="inf",
        sample_inputs_func=sample_inputs_norm_inf,
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # fast gradcheck produces NaNs
        gradcheck_fast_mode=False,
        skips=(
            # AssertionError: RuntimeError not raised : Expected RuntimeError when doing an unsafe cast from a result
            # of dtype torch.float32 into an out= with dtype torch.long
            DecorateInfo(
                unittest.expectedFailure,
                "TestCommon",
                "test_out",
                device_type="meta",
            ),
        ),
    ),
    OpInfo('t',
           sample_inputs_func=sample_inputs_t,
           supports_out=False,
           supports_forward_ad=True,
           supports_fwgrad_bwgrad=True,
           # See https://github.com/pytorch/pytorch/pull/78358
           check_batched_forward_grad=False,
           # vmap does not support inplace views
           check_inplace_batched_forward_grad=False,
           autodiff_fusible_nodes=[],  # aliases inputs, shouldn't be fused
           autodiff_nonfusible_nodes=[],  # aliases inputs, shouldn't be fused
           dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
           assert_autodiffed=True,
           error_inputs_func=error_inputs_t),
    UnaryUfuncInfo('special.erfcx',
                   ref=scipy.special.erfcx if TEST_SCIPY else None,
                   aten_name='special_erfcx',
                   decorators=(toleranceOverride({torch.float32: tol(atol=0, rtol=4e-6), }),),
                   dtypes=all_types_and(torch.bool),
                   supports_forward_ad=True,
                   supports_fwgrad_bwgrad=True),
    OpInfo(
        "nn.functional.dropout",
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout, input, *args, **kwargs),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # Probably because we have used lambda for the op here
            # AssertionError: JIT Test does not execute any logic
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # inplace variant dispatches to dropout kernel, while on CUDA
            # the op dispatches to _fused_dropout (with a few more conditions)
            # hence, different values and this skip here
            DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view', device_type='cuda'),),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # https://github.com/pytorch/pytorch/issues/66357
        check_batched_forward_grad=False,
        supports_out=False,
        sample_inputs_func=sample_inputs_dropout,
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout, input, *args, **kwargs, inplace=True)),
    OpInfo(
        "nn.functional.dropout2d",
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout2d, input, *args, **kwargs),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        check_batched_forward_grad=False,
        # As per the docs, valid input dims are (3, 4)
        sample_inputs_func=partial(sample_inputs_dropout, valid_input_dim=(3, 4)),
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout2d, input, *args, **kwargs, inplace=True)),
    OpInfo(
        "nn.functional.dropout3d",
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout3d, input, *args, **kwargs),
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        check_batched_forward_grad=False,
        # As per the docs, valid input dims are (4, 5)
        sample_inputs_func=partial(sample_inputs_dropout, valid_input_dim=(4, 5)),
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.dropout3d, input, *args, **kwargs, inplace=True)),
    # In training mode, feature_alpha_dropout currently doesn't support inputs of complex dtype
    # unlike when `train=False`, it supports complex inputs, hence 2 OpInfos to cover all cases
    OpInfo(
        "nn.functional.feature_alpha_dropout",
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.feature_alpha_dropout, input, *args, **kwargs),
        variant_test_name="with_train",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # torch.autograd.gradcheck.GradcheckError: While computing batched gradients, got:
            # vmap: We do not yet support calling random operations inside of vmap.
            # Please perform random operations outside of vmap as a workaround
            DecorateInfo(unittest.expectedFailure, 'TestGradients', "test_forward_mode_AD"),
            DecorateInfo(unittest.expectedFailure, 'TestGradients', "test_inplace_forward_mode_AD"),),
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        # As per the docs, valid input dims are (4, 5)
        sample_inputs_func=partial(sample_inputs_dropout, train=True, valid_input_dim=(4, 5)),
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.feature_alpha_dropout, input, *args, **kwargs, inplace=True)),
    OpInfo(
        "nn.functional.feature_alpha_dropout",
        op=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.feature_alpha_dropout, input, *args, **kwargs),
        variant_test_name="without_train",
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),),
        gradcheck_wrapper=wrapper_set_seed,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False,
        sample_inputs_func=partial(sample_inputs_dropout, train=False),
        inplace_variant=lambda input, *args, **kwargs:
            wrapper_set_seed(torch.nn.functional.feature_alpha_dropout, input, *args, **kwargs, inplace=True)),
    OpInfo(
        "nn.functional.one_hot",
        ref=reference_one_hot,
        supports_out=False,
        dtypes=_dispatch_dtypes((torch.int64,)),
        sample_inputs_func=sample_inputs_one_hot,
    ),
    OpInfo(
        "nn.functional.embedding",
        aten_backward_name="embedding_dense_backward",
        # We use lambda to reshuffle the positional arguments.
        # This is because currently only the `input` field of SampleInput
        # is tested in gradient tests.
        op=lambda weight, idx, **kwargs: torch.nn.functional.embedding(idx, weight, **kwargs),
        dtypes=floating_types_and(torch.bfloat16, torch.float16),
        sample_inputs_func=sample_inputs_embedding,
        error_inputs_func=error_inputs_embedding,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # Reference: https://github.com/pytorch/pytorch/issues/67084
            DecorateInfo(unittest.skip("Skipped!"), 'TestMathBits', 'test_neg_view', device_type='cuda'),
            # Not a problem: embedding does weird stuff to its input (it renormalizes)
            DecorateInfo(unittest.skip('Allowed exemption'), 'TestCompositeCompliance', 'test_operator'),
        ),
        supports_expanded_weight=True,
        supports_out=False,
    ),
    OpInfo(
        "nn.functional.embedding_bag",
        # We use lambda to reshuffle the positional arguments.
        # This is because currently only the `input` field of SampleInput
        # is tested in gradient tests.
        op=lambda weight, idx, **kwargs: torch.nn.functional.embedding_bag(idx, weight, **kwargs),
        dtypes=floating_types_and(torch.float16),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        # backward is not supported for mode `max` and dtype `bfloat16`
        backward_dtypesIfCUDA=floating_types_and(torch.float16),
        sample_inputs_func=sample_inputs_embedding_bag,
        skips=(
            # lambda impl
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # Not a problem: embedding_bag does weird stuff to its input (it renormalizes)
            DecorateInfo(unittest.skip('Allowed exemption'), 'TestCompositeCompliance', 'test_operator'),
        ),
        gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
        supports_out=False,
        supports_gradgrad=False,
    ),
    UnaryUfuncInfo(
        "nn.functional.softplus",
        aten_backward_name='softplus_backward',
        ref=reference_softplus,
        sample_kwargs=lambda device, dtype, input: ({'beta': 3, 'threshold': .2}, {'beta': 3, 'threshold': .2}),
        sample_inputs_func=partial(sample_inputs_elementwise_unary, op_kwargs={'beta': 3, 'threshold': .2}),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        decorators=(
            DecorateInfo(
                toleranceOverride
                ({
                    torch.half: tol(atol=1e-2, rtol=1e-2),
                    torch.bfloat16: tol(atol=1e-2, rtol=1e-2),
                }),
                'TestUnaryUfuncs'),
        ),
    ),
    OpInfo(
        "linalg.tensorinv",
        ref=np.linalg.tensorinv,
        dtypes=floating_and_complex_types(),
        sample_inputs_func=sample_inputs_tensorinv,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # See https://github.com/pytorch/pytorch/pull/78358
        check_batched_forward_grad=False,
        decorators=[skipCPUIfNoLapack, skipCUDAIfNoMagmaAndNoCusolver],
    ),
    OpInfo(
        "linalg.tensorsolve",
        ref=lambda a, b, dims=None: np.linalg.tensorsolve(a, b, axes=dims),
        dtypes=floating_and_complex_types(),
        sample_inputs_func=sample_inputs_tensorsolve,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        decorators=[skipCUDAIfNoMagmaAndNoCusolver, skipCPUIfNoLapack,
                    DecorateInfo(toleranceOverride({torch.float32: tol(atol=1e-03, rtol=1e-03)}),
                                 'TestCommon', 'test_noncontiguous_samples',
                                 device_type='cuda')],
    ),
    OpInfo(
        "nn.functional.mse_loss",
        aten_backward_name='mse_loss_backward',
        ref=loss_reference_reduction_wrapper(lambda input, target: (input - target) ** 2),
        sample_inputs_func=sample_inputs_loss,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        dtypes=floating_types_and(torch.float16),
        backward_dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        backward_dtypesIfCUDA=floating_types_and(torch.bfloat16, torch.float16),
        skips=(
            # RuntimeError: input->type()->kind() == TypeKind::OptionalType
            # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":252,
            # please report a bug to PyTorch.
            DecorateInfo(unittest.expectedFailure, "TestJit", "test_variant_consistency_jit", dtypes=(torch.float32,),),
        ),
    ),
    OpInfo(
        "nn.functional.grid_sample",
        dtypes=floating_types(),
        dtypesIfCUDA=floating_types_and(torch.float16),
        supports_out=False,
        sample_inputs_func=sample_inputs_grid_sample,
        supports_gradgrad=False,
        gradcheck_nondet_tol=1e-15),
    OpInfo(
        "argwhere",
        ref=np.argwhere,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        supports_out=False,
        supports_autograd=False,
        sample_inputs_func=sample_inputs_argwhere,
    ),
    ReductionOpInfo(
        'all',
        identity=True,
        supports_multiple_dims=False,
        supports_autograd=False,
        result_dtype=torch.bool,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.all),
        skips=(
            # FIXME: does not support passing keepdim without dim
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: does not support dim=None
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_none'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_none_keepdim'),
            # FIXME: uint8 input returns uint8 instead of bool
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_result_dtype', dtypes=[torch.uint8]),
        ),
    ),
    ReductionOpInfo(
        'any',
        identity=False,
        supports_multiple_dims=False,
        supports_autograd=False,
        result_dtype=torch.bool,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.any),
        skips=(
            # FIXME: does not support passing keepdim without dim
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: does not support dim=None
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_none'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_none_keepdim'),
            # FIXME: uint8 input returns uint8 instead of bool
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_result_dtype', dtypes=[torch.uint8]),
        ),
    ),
    ReductionOpInfo(
        'amax',
        nan_policy='propagate',
        supports_forward_ad=True,
        check_batched_forward_grad=False,
        supports_fwgrad_bwgrad=True,
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        ref=reference_reduction_numpy(np.amax),
        skips=(
            # FIXME: reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
        ),
        error_inputs_func=error_inputs_aminmax_amax_amin,
    ),
    ReductionOpInfo(
        'amin',
        nan_policy='propagate',
        supports_forward_ad=True,
        check_batched_forward_grad=False,
        supports_fwgrad_bwgrad=True,
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        ref=reference_reduction_numpy(np.amin),
        skips=(
            # FIXME: reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
        ),
        error_inputs_func=error_inputs_aminmax_amax_amin,
    ),
    ReductionOpInfo(
        'argmax',
        supports_multiple_dims=False,
        supports_autograd=False,
        assert_jit_shape_analysis=True,
        result_dtype=torch.int64,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.argmax, supports_keepdims=False),
        skips=(
            # FIXME: keepdim parameter is ignored when dim=None
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_none_keepdim'),
        ),
    ),
    ReductionOpInfo(
        'argmin',
        supports_multiple_dims=False,
        supports_autograd=False,
        result_dtype=torch.int64,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.argmin, supports_keepdims=False),
        skips=(
            # FIXME: keepdim parameter is ignored when dim=None
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_none_keepdim'),
        ),
    ),
    ReductionOpInfo(
        'count_nonzero',
        identity=0,
        supports_out=False,
        supports_autograd=False,
        result_dtype=torch.int64,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_reduction_count_nonzero,
        ref=reference_reduction_numpy(np.count_nonzero),
        skips=(
            # FIXME: count_nonzero does not accept keepdim kwarg
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_none_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_single_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_multi_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_multi_unsorted_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_offbounds_keepdim'),
            # FIXME: dim=[] reduces all dimensions
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
        ),
    ),
    ReductionOpInfo(
        'mean',
        nan_policy='propagate',
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # FIXME: mean needs 'dim' parameter when using the 'out' overload.
        # Adding it with 'generate_args_kwargs' does not work, since these also get passed
        # onto the reference implementations.
        supports_out=False,
        assert_autodiffed=True,
        assert_jit_shape_analysis=True,
        promotes_int_to_float=True,
        dtypes=floating_and_complex_types_and(torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.mean),
        error_inputs_func=error_inputs_mean,
        skips=(
            # FIXME: mean does not support passing keepdim without passing dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: mean reduces all dimensions when dim=[]
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: improve precision
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input',
                         dtypes=[torch.float16]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_extremal_values',
                         device_type='cuda', dtypes=[torch.complex64]),
        ),
    ),
    ReductionOpInfo(
        'nanmean',
        nan_policy='omit',
        assert_autodiffed=True,
        promotes_int_to_float=True,
        supports_forward_ad=True,
        check_batched_forward_grad=False,
        supports_fwgrad_bwgrad=True,
        dtypes=floating_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_nan_reduction(supports_multiple_dims=True),
        ref=reference_reduction_numpy(np.nanmean),
        skips=(
            # AssertionError: False is not true :
            # Failure in testing nodes' autodifferentiation.
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
            # FIXME: prod reduces all dimensions when dim=[]
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: improve precision
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input',
                         dtypes=[torch.float16]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_duplicate_values',
                         device_type='cuda', dtypes=[torch.float16]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_extremal_values',
                         device_type='cuda', dtypes=[torch.complex64]),
        ),
    ),
    ReductionOpInfo(
        'std',
        nan_policy='propagate',
        supports_out=False,
        complex_to_real=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        assert_autodiffed=True,
        promotes_int_to_float=True,
        check_batched_forward_grad=False,
        dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
        dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_std_var,
        ref=reference_std_var(np.std),
        generate_args_kwargs=generate_std_var_kwargs,
        skips=(
            # FIXME: cannot specify keepdim without dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: dim=[] reduces all dimensions
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: improve precision
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_duplicate_values'),
            # NumPy is giving NaN for this
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_large_input'),
        ),
    ),
    ReductionOpInfo(
        'var',
        nan_policy='propagate',
        supports_out=False,
        assert_autodiffed=True,
        promotes_int_to_float=True,
        complex_to_real=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        check_batched_forward_grad=False,
        dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
        dtypesIfCUDA=floating_and_complex_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_std_var,
        ref=reference_std_var(np.var),
        generate_args_kwargs=generate_std_var_kwargs,
        skips=(
            # FIXME: cannot specify keepdim without dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: dim=[] reduces all dimensions
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: improve precision
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_duplicate_values'),
            # NumPy is giving NaN for this
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_large_input'),
        ),
    ),
    ReductionOpInfo(
        'prod',
        identity=1,
        nan_policy='propagate',
        supports_multiple_dims=False,
        # https://github.com/pytorch/pytorch/issues/80411
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        promotes_int_to_int64=True,
        gradcheck_nondet_tol=GRADCHECK_NONDET_TOL,
        dtypes=all_types_and_complex_and(torch.bool),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
        sample_inputs_func=sample_inputs_prod,
        ref=reference_reduction_numpy(np.prod),
        skips=(
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
            # FIXME: prod does not support passing keepdim without passing dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: prod reduces all dimensions when dim=[]
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: prod does not support passing None to dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_none'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_none_keepdim'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input',
                         dtypes=[torch.float16, torch.complex64]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_duplicate_values',
                         dtypes=[torch.uint8, torch.float16, torch.complex64]),
        ),
    ),
    ReductionOpInfo(
        'sum',
        identity=0,
        nan_policy='propagate',
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        promotes_int_to_int64=True,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
        ref=reference_reduction_numpy(np.sum),
        skips=(
            # FIXME: sum does not support passing keepdim without passing dim
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_default_keepdim'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: improve precision
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input',
                         dtypes=[torch.float16]),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_duplicate_values',
                         dtypes=[torch.float16]),
        ),
    ),
    ReductionOpInfo(
        'nansum',
        identity=0,
        nan_policy='omit',
        supports_out=True,
        promotes_int_to_int64=True,
        supports_forward_ad=True,
        check_batched_forward_grad=False,
        supports_fwgrad_bwgrad=True,
        dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_nan_reduction(supports_multiple_dims=True),
        ref=reference_reduction_numpy(np.nansum),
        skips=(
            # please report a bug to PyTorch.
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
            # FIXME: nansum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # FIXME: flaky test so skipped instead of xfailed
            # possibly bad low precision reference in numpy
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_ref_small_input',
                         dtypes=[torch.float16]),
        ),
    ),
    ReductionOpInfo(
        '_masked.sum',
        ref=reference_reduction_numpy(np.sum),
        method_variant=None,
        identity=0,
        nan_policy='propagate',
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_sparse=True,
        supports_sparse_csr=True,
        promotes_int_to_int64=True,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        skips=(
            DecorateInfo(unittest.skip("Failing on some jobs"), 'TestReductions', 'test_reference_masked',
                         dtypes=(torch.bool, torch.int8, torch.int16, torch.int32)),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: undefined value tensor
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.bfloat16: tol(atol=1e-03, rtol=1e-03)}),
                         'TestReductions', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-03, rtol=1e-03)}),
                         'TestReductions', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-03)}),
                         'TestReductions', 'test_ref_small_input'),
        ],
        sample_inputs_func=sample_inputs_masked_reduction,
        sample_inputs_sparse_coo_func=sample_inputs_sparse_coo_masked_reduction,
        sample_inputs_sparse_csr_func=sample_inputs_sparse_csr_masked_reduction
    ),
    ReductionOpInfo(
        '_masked.prod',
        ref=reference_reduction_numpy(np.prod),
        method_variant=None,
        identity=1,
        nan_policy='propagate',
        # https://github.com/pytorch/pytorch/issues/80411
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_sparse=True,
        supports_sparse_csr=True,
        promotes_int_to_int64=True,
        # FIXME: "prod_cpu" not implemented for 'BFloat16'
        # FIXME: "prod_cpu" not implemented for 'Half'
        dtypes=all_types_and_complex_and(torch.bool),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        skips=(
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.skip("Skipped!"), 'TestCompositeCompliance', 'test_forward_ad'),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.skip("Failing on some jobs"), 'TestReductions', 'test_reference_masked',
                         dtypes=(torch.bool, torch.int8, torch.int16, torch.int32),),
            # FIXME: "cuda_scatter_gather_base_kernel_func" not implemented for ... (used for sparse_coo inputs)
            DecorateInfo(unittest.skip("Skipped!"), 'TestMasked', 'test_mask_layout', device_type='cuda',
                         dtypes=(torch.bool, torch.int8, torch.uint8, torch.int16, torch.int32,
                                 torch.int64, torch.complex64, torch.complex128)),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-03, rtol=1e-02)}),
                         'TestReductions', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-03, rtol=1e-03)}),
                         'TestReductions', 'test_ref_duplicate_values'),
        ],
        sample_inputs_func=sample_inputs_masked_reduction,
        sample_inputs_sparse_coo_func=sample_inputs_sparse_coo_masked_reduction,
        sample_inputs_sparse_csr_func=sample_inputs_sparse_csr_masked_reduction,
    ),
    OpInfo(
        '_masked.cumsum',
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        method_variant=None,
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
        ),
        # Can reuse the same inputs; dim is required in both
        sample_inputs_func=sample_inputs_masked_cumops,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
    ),
    OpInfo(
        '_masked.cumprod',
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        method_variant=None,
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
        ),
        # Can reuse the same inputs; dim is required in both
        sample_inputs_func=sample_inputs_masked_cumops,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
    ),
    ReductionOpInfo(
        '_masked.amax',
        nan_policy='propagate',
        supports_out=False,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        supports_sparse=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_sparse_csr=True,
        ref=reference_reduction_numpy(np.amax),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: amax reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: Unknown builtin op: aten::iinfo
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
            # FIXME: "cuda_scatter_gather_base_kernel_func" not implemented for ... (used for sparse_coo inputs)
            # FIXME: "_segment_reduce_lengths_cpu/cuda" not implemented for ... (used for sparse_csr inputs)
            DecorateInfo(unittest.skip("Skipped!"), 'TestMasked', 'test_mask_layout',
                         dtypes=(torch.bool, torch.int8, torch.uint8, torch.int16, torch.int32,
                                 torch.int64, torch.complex64, torch.complex128)),
        ),
        sample_inputs_func=sample_inputs_masked_reduction,
        sample_inputs_sparse_coo_func=sample_inputs_sparse_coo_masked_reduction,
        sample_inputs_sparse_csr_func=sample_inputs_sparse_csr_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.amin',
        nan_policy='propagate',
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        supports_sparse=True,
        supports_sparse_csr=True,
        ref=reference_reduction_numpy(np.amin),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: amax reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: Unknown builtin op: aten::iinfo
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # FIXME: "cuda_scatter_gather_base_kernel_func" not implemented for ... (used for sparse_coo inputs)
            # FIXME: "_segment_reduce_lengths_cpu/cuda" not implemented for ... (used for sparse_csr inputs)
            DecorateInfo(unittest.skip("Skipped!"), 'TestMasked', 'test_mask_layout',
                         dtypes=(torch.bool, torch.int8, torch.uint8, torch.int16, torch.int32,
                                 torch.int64, torch.complex64, torch.complex128)),
        ),
        sample_inputs_func=sample_inputs_masked_reduction,
        sample_inputs_sparse_coo_func=sample_inputs_sparse_coo_masked_reduction,
        sample_inputs_sparse_csr_func=sample_inputs_sparse_csr_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.argmax',
        supports_out=False,
        supports_multiple_dims=False,
        supports_autograd=False,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.argmax, supports_keepdims=False),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # initial is not a keyword for argmax
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_reference_masked'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness', dtypes=(torch.bfloat16,)),
        ),
        sample_inputs_func=sample_inputs_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.argmin',
        supports_out=False,
        supports_multiple_dims=False,
        supports_autograd=False,
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        ref=reference_reduction_numpy(np.argmin, supports_keepdims=False),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # initial is not a keyword for argmin
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_reference_masked'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.expectedFailure, 'TestNNCOpInfo', 'test_nnc_correctness', dtypes=(torch.bfloat16,)),
        ),
        sample_inputs_func=sample_inputs_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.mean',
        ref=reference_reduction_numpy(np.mean) if np.lib.NumpyVersion(np.__version__) >= '1.20.2' else None,
        method_variant=None,
        nan_policy='propagate',
        supports_out=False,
        supports_sparse_csr=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        promotes_int_to_float=True,
        dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16, torch.bool),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_ref_duplicate_values',
                         dtypes=(torch.bool,)),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_reference_masked',
                         dtypes=(torch.bool,)),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_ref_small_input',
                         dtypes=(torch.bool,)),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: undefined value tensor
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # FIXME: "_segment_reduce_lengths_cpu/cuda" not implemented for ... (used for sparse_csr inputs)
            DecorateInfo(unittest.skip("Skipped!"), 'TestMasked', 'test_mask_layout',
                         dtypes=(torch.bool, torch.int8, torch.uint8, torch.int16, torch.int32,
                                 torch.int64, torch.complex64, torch.complex128)),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-03, rtol=1e-03)}),
                         'TestReductions', 'test_reference_masked'),
        ],
        sample_inputs_func=sample_inputs_masked_reduction,
        sample_inputs_sparse_csr_func=sample_inputs_sparse_csr_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    OpInfo(
        '_masked.median',
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16),
        method_variant=None,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
        ),
        sample_inputs_func=sample_inputs_masked_softmax,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.norm',
        identity=0,
        method_variant=None,
        nan_policy='propagate',
        supports_out=False,
        promotes_int_to_float=True,
        dtypes=floating_types_and(torch.float16, torch.bfloat16),
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # torch.jit.frontend.NotSupportedError: Compiled functions
            # can't take variable number of arguments or use
            # keyword-only arguments with defaults
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_masked_norm,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    ReductionOpInfo(
        '_masked.var',
        ref=reference_reduction_numpy(np.var) if np.lib.NumpyVersion(np.__version__) >= '1.20.2' else None,
        method_variant=None,
        nan_policy='propagate',
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # See https://github.com/pytorch/pytorch/pull/78358
        check_batched_forward_grad=False,
        promotes_int_to_float=True,
        dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
        skips=(
            # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
            DecorateInfo(
                unittest.skip("Skipped!"),
                'TestSchemaCheckModeOpInfo',
                'test_schema_correctness',
                dtypes=(torch.complex64, torch.complex128)),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: undefined value tensor
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02),
                                            torch.bfloat16: tol(atol=1e-03, rtol=1e-03)}),
                         'TestReductions', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestReductions', 'test_ref_small_input'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestMasked', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
        ],
        sample_inputs_func=sample_inputs_masked_std_var,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        check_batched_grad=True,
    ),
    ReductionOpInfo(
        '_masked.std',
        ref=reference_reduction_numpy(np.std) if np.lib.NumpyVersion(np.__version__) >= '1.20.2' else None,
        method_variant=None,
        nan_policy='propagate',
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # See https://github.com/pytorch/pytorch/pull/78358
        check_batched_forward_grad=False,
        promotes_int_to_float=True,
        dtypes=all_types_and_complex_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.float16, torch.bfloat16),
        skips=(
            # Issue with conj and torch dispatch, see https://github.com/pytorch/pytorch/issues/82479
            DecorateInfo(
                unittest.skip("Skipped!"),
                'TestSchemaCheckModeOpInfo',
                'test_schema_correctness',
                dtypes=(torch.complex64, torch.complex128)),
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: sum reduces all dimensions when dim=[]
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.expectedFailure, 'TestReductions', 'test_dim_empty_keepdim'),
            # RuntimeError: undefined value tensor
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.skip('Skipped!'), 'TestCudaFuserOpInfo', 'test_nvfuser_correctness',
                         dtypes=(torch.float16,)),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestReductions', 'test_reference_masked'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestReductions', 'test_ref_small_input'),
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestMasked', 'test_reference_masked'),
        ],
        sample_inputs_func=sample_inputs_masked_std_var,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        check_batched_grad=True,
    ),
    OpInfo(
        '_masked.softmax',
        method_variant=None,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_masked_softmax,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        supports_forward_ad=True,
        supports_out=False),
    OpInfo(
        '_masked.log_softmax',
        method_variant=None,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_masked_softmax,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        decorators=[
            DecorateInfo(toleranceOverride({torch.bfloat16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestMasked', 'test_reference_masked'),
        ],
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        supports_forward_ad=True,
        supports_out=False),
    OpInfo(
        '_masked.softmin',
        method_variant=None,
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_masked_softmax,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
        ),
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        supports_forward_ad=True,
        supports_out=False),
    OpInfo(
        '_masked.normalize',
        method_variant=None,
        dtypes=floating_and_complex_types_and(torch.half, torch.bfloat16),
        sample_inputs_func=sample_inputs_masked_normalize,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            DecorateInfo(unittest.expectedFailure, 'TestJit', 'test_variant_consistency_jit'),
            # RuntimeError: "clamp_min_cpu" not implemented for 'Half'
            DecorateInfo(unittest.expectedFailure, 'TestMasked', 'test_reference_masked',
                         device_type='cpu', dtypes=[torch.half]),
        ),
        gradcheck_wrapper=gradcheck_wrapper_masked_operation,
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        supports_out=False),
    OpInfo(
        '_masked.logaddexp',
        dtypes=floating_types_and(torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        check_batched_forward_grad=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestGradients', 'test_fn_gradgrad'),
        ),
        sample_inputs_func=sample_inputs_masked_logaddexp,
        gradcheck_wrapper=gradcheck_wrapper_masked_pointwise_operation
    ),
    ReductionOpInfo(
        '_masked.logsumexp',
        dtypes=all_types_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        method_variant=None,
        nan_policy='propagate',
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestNormalizeOperators', 'test_normalize_operator_exhaustive'),
            # FIXME: reduces all dimensions when dim=[]
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_dim_empty_keepdim'),
            # Identity can't be -torch.inf without overflow
            DecorateInfo(unittest.skip("Skipped!"), 'TestReductions', 'test_empty_tensor_empty_slice'),
            # NotSupportedError: Compiled functions can't ... use keyword-only arguments with defaults
            DecorateInfo(unittest.skip("Skipped!"), 'TestJit', 'test_variant_consistency_jit'),
            # all the values are the same except for -inf vs nan
            DecorateInfo(unittest.skip("Skipped!"), 'TestDecomp', 'test_comprehensive'),
        ),
        sample_inputs_func=sample_inputs_masked_reduction,
        gradcheck_wrapper=gradcheck_wrapper_masked_operation
    ),
    OpInfo(
        "nn.functional.ctc_loss",
        dtypes=floating_types(),
        supports_out=False,
        sample_inputs_func=sample_inputs_ctc_loss,
        skips=(
            # https://github.com/pytorch/pytorch/issues/67462
            # torch.autograd.gradcheck.GradcheckError: Jacobian mismatch for output 0 with respect to input 0
            DecorateInfo(
                unittest.expectedFailure,
                "TestGradients",
                "test_fn_grad",
                dtypes=(torch.float64,),
            ),
            # RuntimeError: derivative for aten::_ctc_loss_backward is not implemented
            DecorateInfo(
                unittest.expectedFailure,
                "TestGradients",
                "test_fn_gradgrad",
                dtypes=(torch.float64,),
            ),
            # RuntimeError: derivative for aten::_ctc_loss_backward is not implemented
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32,),
            ),
            # Operation calls data_ptr() somewhere; needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
        ),
    ),
    OpInfo(
        "nn.functional.cosine_embedding_loss",
        dtypes=all_types_and(torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_cosine_embedding_loss,
    ),
    OpInfo(
        "nn.functional.nll_loss",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        sample_inputs_func=sample_inputs_nll_loss,
        supports_forward_ad=True,
        assert_jit_shape_analysis=True,
        skips=(
            # RuntimeError:
            # undefined value tensor:
            #   File "<string>", line 3
            # def the_method(i0, i1):
            #     return torch.nn.functional.nll_loss(i0, i1, weight=tensor([8.4784, 1.7658, 4.3228], dtype=torch.float32))
            #                                                        ~~~~~~ <--- HERE
            DecorateInfo(unittest.skip("Skipped!"), "TestJit", "test_variant_consistency_jit", dtypes=(torch.float32,),),
        ),
    ),
    OpInfo(
        "nn.functional.gaussian_nll_loss",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        # Runs very slowly on slow gradcheck - alternatively reduce input sizes
        gradcheck_fast_mode=True,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_gaussian_nll_loss,
        skips=(
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_forward_ad'),
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_operator'),
            # JIT does not support variadic tensors.
            # RuntimeError: input->type()->kind() == TypeKind::OptionalType
            # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270,
            # please report a bug to PyTorch.
            DecorateInfo(unittest.skip("Skipped!"), "TestJit", "test_variant_consistency_jit", dtypes=(torch.float32,),),
        ),
        decorators=(
            DecorateInfo(toleranceOverride({torch.float16: tol(atol=1e-02, rtol=1e-02),
                                            torch.bfloat16: tol(atol=1e-02, rtol=1e-02)}),
                         'TestCudaFuserOpInfo', 'test_nvfuser_correctness'),
        )
    ),
    OpInfo(
        "nn.functional.hinge_embedding_loss",
        dtypes=floating_types_and(torch.bfloat16),
        dtypesIfCUDA=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_hinge_embedding_loss,
        error_inputs_func=error_inputs_hinge_embedding_loss,
        reference_inputs_func=reference_inputs_hinge_embedding_loss,
    ),
    OpInfo(
        "nn.functional.huber_loss",
        aten_backward_name='huber_loss_backward',
        dtypes=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        sample_inputs_func=sample_inputs_huber_loss,
        skips=(
            # JIT does not support variadic tensors.
            # RuntimeError: input->type()->kind() == TypeKind::OptionalType
            # INTERNAL ASSERT FAILED at "../torch/csrc/jit/passes/utils/check_alias_annotation.cpp":270,
            # please report a bug to PyTorch.
            DecorateInfo(unittest.skip("Skipped!"), "TestJit", "test_variant_consistency_jit", dtypes=(torch.float32,),),
        )
    ),
    OpInfo(
        "nn.functional.pdist",
        ref=reference_pdist,
        sample_inputs_func=sample_inputs_pdist,
        dtypes=floating_types(),
        supports_out=False,
        supports_gradgrad=False),
    OpInfo(
        "nn.functional.poisson_nll_loss",
        dtypes=all_types_and(torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        sample_inputs_func=sample_inputs_poisson_nll_loss,
    ),
    OpInfo(
        "argsort",
        dtypes=all_types_and(torch.bool, torch.float16, torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_argsort,
        supports_out=False,
        supports_autograd=False,
        skips=(
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32,),
            ),
        ),
    ),
    OpInfo(
        "repeat_interleave",
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16, torch.chalf),
        backward_dtypesIfCUDA=floating_and_complex_types_and(torch.float16, torch.bfloat16, torch.chalf),
        sample_inputs_func=sample_inputs_repeat_interleave,
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # See https://github.com/pytorch/pytorch/pull/78358
        check_batched_forward_grad=False,
        skips=(
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32, torch.complex64),
            ),
        ),
    ),
    OpInfo(
        "nn.functional.pairwise_distance",
        ref=lambda a, b, p=2.0, eps=1e-6, keepdim=False: (
            np.sum(np.abs(a - b + eps) ** p, axis=-1, keepdims=keepdim) ** (1 / p)
        ),
        sample_inputs_func=sample_inputs_pairwise_distance,
        dtypes=all_types_and_complex_and(torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32, torch.complex64),
            ),
        ),
    ),
    OpInfo(
        "nn.functional.pixel_shuffle",
        sample_inputs_func=sample_inputs_pixel_shuffle,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # https://github.com/pytorch/pytorch/issues/82235
            DecorateInfo(
                unittest.expectedFailure,
                'TestSchemaCheckModeOpInfo',
                'test_schema_correctness',
                device_type='cuda',
            ),
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32, torch.complex64),
            ),
        ),
    ),
    OpInfo(
        "nn.functional.pixel_unshuffle",
        sample_inputs_func=sample_inputs_pixel_unshuffle,
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        skips=(
            # https://github.com/pytorch/pytorch/issues/82235
            DecorateInfo(
                unittest.expectedFailure,
                'TestSchemaCheckModeOpInfo',
                'test_schema_correctness',
                device_type='cuda',
            ),
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                dtypes=(torch.float32, torch.complex64),
            ),
        ),
    ),
    OpInfo(
        "nn.functional.kl_div",
        sample_inputs_func=sample_inputs_kl_div,
        dtypes=floating_types_and(torch.bfloat16, torch.int8, torch.int16, torch.int32, torch.int64),
        dtypesIfCUDA=floating_types_and(
            torch.float16, torch.bfloat16, torch.int8, torch.int16, torch.int32, torch.int64
        ),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
    ),
    OpInfo(
        "diagflat",
        ref=lambda input, offset=0: np.diagflat(input, k=offset),
        sample_inputs_func=sample_inputs_diagflat,
        dtypes=all_types_and_complex_and(torch.bool, torch.bfloat16),
        dtypesIfCUDA=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        supports_out=False,
        supports_forward_ad=True,
        supports_fwgrad_bwgrad=True,
        # See https://github.com/pytorch/pytorch/pull/78358
        check_batched_forward_grad=False,
    ),
    OpInfo(
        'scatter_reduce',
        variant_test_name='sum',
        # complex not added to dtypes as complex gradients are not properly handled
        # and scatter_reduce hasn't been added to the whitelist in gen_variable_type yet
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        sample_inputs_func=sample_inputs_scatter_reduce,
    ),
    OpInfo(
        'scatter_reduce',
        variant_test_name='prod',
        # complex not added to dtypes as complex gradients are not properly handled
        # and scatter_reduce hasn't been added to the whitelist in gen_variable_type yet
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_scatter_reduce,
        skips=(
            # Pre-existing condition (calls .item); needs to be fixed
            DecorateInfo(unittest.expectedFailure, 'TestCompositeCompliance', 'test_backward'),
        ),
    ),
    OpInfo(
        'scatter_reduce',
        variant_test_name='mean',
        # complex not added to dtypes as complex gradients are not properly handled
        # and scatter_reduce hasn't been added to the whitelist in gen_variable_type yet
        dtypes=all_types_and(torch.float16, torch.bfloat16),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_scatter_reduce,
    ),
    OpInfo(
        'scatter_reduce',
        variant_test_name='amin',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_scatter_reduce,
    ),
    OpInfo(
        'scatter_reduce',
        variant_test_name='amax',
        dtypes=all_types_and(torch.float16, torch.bfloat16, torch.bool),
        dtypesIfCUDA=all_types_and(torch.float16, torch.bfloat16),
        sample_inputs_func=sample_inputs_scatter_reduce,
    ),
    OpInfo(
        'segment_reduce',
        variant_test_name='lengths',
        dtypes=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        # RuntimeError: derivative for aten::_segment_reduce_backward is not implemented
        supports_gradgrad=False,
        sample_inputs_func=sample_inputs_segment_reduce,
        skips=(
            # FIXME: CUDA driver API confirmed a leak in
            # __main__.TestJitCUDA.test_variant_consistency_jit_segment_reduce_cuda_float32
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                device_type="cuda",
            ),
        ),
    ),
    OpInfo(
        'segment_reduce',
        variant_test_name='offsets',
        dtypes=floating_types_and(torch.float16, torch.bfloat16),
        supports_out=False,
        # RuntimeError: derivative for aten::_segment_reduce_backward is not implemented
        supports_gradgrad=False,
        sample_inputs_func=partial(sample_inputs_segment_reduce, mode='offsets'),
        skips=(
            # FIXME: CUDA driver API confirmed a leak in
            # __main__.TestJitCUDA.test_variant_consistency_jit_segment_reduce_cuda_float32
            DecorateInfo(
                unittest.skip("Skipped!"),
                "TestJit",
                "test_variant_consistency_jit",
                device_type="cuda",
            ),
        ),
    ),
    UnaryUfuncInfo(
        'special.airy_ai',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-03,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=lambda x: scipy.special.airy(x)[0] if TEST_SCIPY else None,
        skips=(
            DecorateInfo(
                unittest.skip("Skipped!"),
                'TestUnaryUfuncs',
                'test_reference_numerics_large',
            ),
        ),
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.bessel_j0',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-04,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.j0 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.bessel_j1',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-04,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.j1 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.bessel_y0',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-04,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.y0 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.bessel_y1',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-04,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.y1 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.chebyshev_polynomial_t',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.chebyshev_polynomial_u',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.chebyshev_polynomial_v',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.chebyshev_polynomial_w',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.hermite_polynomial_h',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.hermite_polynomial_he',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.laguerre_polynomial_l',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.legendre_polynomial_p',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.modified_bessel_i0',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-03,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.i0 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.modified_bessel_i1',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-03,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.i1 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.modified_bessel_k0',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-03,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.k0 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.modified_bessel_k1',
        decorators=(
            precisionOverride(
                {
                    torch.float32: 1e-03,
                    torch.float64: 1e-05,
                },
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.k1 if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.scaled_modified_bessel_k0',
        decorators=(
            toleranceOverride(
                {
                    torch.float32: tol(atol=1e-03, rtol=1e-03),
                    torch.float64: tol(atol=1e-05, rtol=1e-03),
                }
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.k0e if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.scaled_modified_bessel_k1',
        decorators=(
            toleranceOverride(
                {
                    torch.float32: tol(atol=1e-03, rtol=1e-03),
                    torch.float64: tol(atol=1e-05, rtol=1e-03),
                }
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=scipy.special.k1e if TEST_SCIPY else None,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.shifted_chebyshev_polynomial_t',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.shifted_chebyshev_polynomial_u',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.shifted_chebyshev_polynomial_v',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    BinaryUfuncInfo(
        'special.shifted_chebyshev_polynomial_w',
        dtypes=all_types_and(torch.bool),
        promotes_int_to_float=True,
        skips=(
            DecorateInfo(unittest.skip("Skipping - testing takes an unreasonably long time, #79528")),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCudaFuserOpInfo'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestNNCOpInfo'),
        ),
        supports_one_python_scalar=True,
        supports_autograd=False,
    ),
    UnaryUfuncInfo(
        'special.spherical_bessel_j0',
        decorators=(
            toleranceOverride(
                {
                    torch.float32: tol(atol=1e-03, rtol=1e-03),
                    torch.float64: tol(atol=1e-05, rtol=1e-03),
                }
            ),
        ),
        dtypes=all_types_and(torch.bool),
        ref=lambda x: scipy.special.spherical_jn(0, x) if TEST_SCIPY else None,
        supports_autograd=False,
    ),
]

class ReductionPythonRefInfo(ReductionOpInfo):
    '''
    An OpInfo for a Python reference of an elementwise unary operation.
    '''
    def __init__(
            self,
            name,  # the stringname of the callable Python reference
            *,
            op=None,  # the function variant of the operation, populated as torch.<name> if None
            torch_opinfo_name,  # the string name of the corresponding torch opinfo
            torch_opinfo_variant_name='',  # the variant name for corresponding torch opinfo
            supports_nvfuser=True,
            **kwargs):  # additional kwargs override kwargs inherited from the torch opinfo

        self.torch_opinfo_name = torch_opinfo_name
        self.torch_opinfo_variant_name = torch_opinfo_variant_name
        self.torch_opinfo = _find_referenced_opinfo(torch_opinfo_name, torch_opinfo_variant_name)
        self.supports_nvfuser = supports_nvfuser
        assert isinstance(self.torch_opinfo, ReductionOpInfo)

        inherited = self.torch_opinfo._original_reduction_args
        ukwargs = _inherit_constructor_args(name, op, inherited, kwargs)

        # See https://github.com/pytorch/pytorch/issues/77216
        self.validate_view_consistency = False

        super().__init__(**ukwargs)

class ElementwiseUnaryPythonRefInfo(UnaryUfuncInfo):
    '''
    An OpInfo for a Python reference of an elementwise unary operation.
    '''
    def __init__(
            self,
            name,  # the stringname of the callable Python reference
            *,
            op=None,  # the function variant of the operation, populated as torch.<name> if None
            torch_opinfo_name,  # the string name of the corresponding torch opinfo
            torch_opinfo_variant_name='',  # the variant name for corresponding torch opinfo
            supports_nvfuser=True,
            **kwargs):  # additional kwargs override kwargs inherited from the torch opinfo

        self.torch_opinfo_name = torch_opinfo_name
        self.torch_opinfo_variant_name = torch_opinfo_variant_name
        self.torch_opinfo = _find_referenced_opinfo(torch_opinfo_name, torch_opinfo_variant_name)
        self.supports_nvfuser = supports_nvfuser
        assert isinstance(self.torch_opinfo, UnaryUfuncInfo)

        inherited = self.torch_opinfo._original_unary_ufunc_args
        ukwargs = _inherit_constructor_args(name, op, inherited, kwargs)

        super(ElementwiseUnaryPythonRefInfo, self).__init__(**ukwargs)

class ElementwiseBinaryPythonRefInfo(BinaryUfuncInfo):
    '''
    An OpInfo for a Python reference of an elementwise binary operation.
    '''
    def __init__(
            self,
            name,  # the stringname of the callable Python reference
            *,
            op=None,  # the function variant of the operation, populated as torch.<name> if None
            torch_opinfo_name,  # the string name of the corresponding torch opinfo
            torch_opinfo_variant_name='',  # the variant name for corresponding torch opinfo
            supports_nvfuser=True,
            **kwargs):  # additional kwargs override kwargs inherited from the torch opinfo

        self.torch_opinfo_name = torch_opinfo_name
        self.torch_opinfo_variant_name = torch_opinfo_variant_name
        self.torch_opinfo = _find_referenced_opinfo(torch_opinfo_name, torch_opinfo_variant_name)
        self.supports_nvfuser = supports_nvfuser
        assert isinstance(self.torch_opinfo, BinaryUfuncInfo)

        inherited = self.torch_opinfo._original_binary_ufunc_args
        ukwargs = _inherit_constructor_args(name, op, inherited, kwargs)

        super(ElementwiseBinaryPythonRefInfo, self).__init__(**ukwargs)

class SpectralFuncPythonRefInfo(SpectralFuncInfo):
    '''
    An OpInfo for a Python reference of an elementwise unary operation.
    '''
    def __init__(
            self,
            name,  # the stringname of the callable Python reference
            *,
            op=None,  # the function variant of the operation, populated as torch.<name> if None
            torch_opinfo_name,  # the string name of the corresponding torch opinfo
            torch_opinfo_variant='',
            supports_nvfuser=True,
            **kwargs):  # additional kwargs override kwargs inherited from the torch opinfo

        self.torch_opinfo_name = torch_opinfo_name
        self.torch_opinfo = _find_referenced_opinfo(torch_opinfo_name, torch_opinfo_variant)
        self.supports_nvfuser = supports_nvfuser
        assert isinstance(self.torch_opinfo, SpectralFuncInfo)

        inherited = self.torch_opinfo._original_spectral_func_args
        ukwargs = _inherit_constructor_args(name, op, inherited, kwargs)

        super().__init__(**ukwargs)


# Separate registry for experimental Python Reference OpInfos.
python_ref_db = [
    #
    # Elementwise Unary OpInfos
    #
    ElementwiseUnaryPythonRefInfo(
        "_refs.abs",
        torch_opinfo_name="abs",
        skips=(
            # Reference result was farther (0.0) from the precise computation
            # than the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.chalf,), device_type='cpu', active_if=not (IS_MACOS or IS_WINDOWS)),
            # Reference result was farther (0.0) from the precise computation
            # than the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.chalf,), device_type='cpu', active_if=not (IS_MACOS or IS_WINDOWS)),
        )
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.acos",
        torch_opinfo_name="acos",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.acosh",
        torch_opinfo_name="acosh",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.asin",
        torch_opinfo_name="asin",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.asinh",
        torch_opinfo_name="asinh",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.arange",
        torch_opinfo_name="arange",
        skips=(
            # Tests that assume input is a tensor or sequence of tensors
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),
            # See https://github.com/pytorch/pytorch/issues/82364
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out_warning'),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_out'),

            # Prims arange does not follow aten
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_meta',
                         dtypes=(torch.int64,)),
        ),
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.linspace",
        torch_opinfo_name="linspace",
        skips=(
            # Tests that assume input is a tensor or sequence of tensors
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),

            # cpu implementation is wrong on some integral types
            # https://github.com/pytorch/pytorch/issues/81996
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.int16, torch.int32, torch.int64), device_type="cpu"),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.int16, torch.int32, torch.int64), device_type="cpu"),

            # cuda implementation is off-by-one on some inputs due to precision issues
            # https://github.com/pytorch/pytorch/issues/82230
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor',
                         dtypes=(torch.uint8, torch.int8, torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
        ),
        # returns a view of an intermediate tensor (prims.to_dtype)
        validate_view_consistency=False,
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.logspace",
        torch_opinfo_name="logspace",
        skips=(
            # Tests that assume input is a tensor or sequence of tensors
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.expectedFailure, 'TestMathBits', 'test_neg_conj_view'),

            # Off-by-one issue when casting floats to ints
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor',
                         dtypes=(torch.int16, torch.int32, torch.int64),
                         device_type="cuda"),
        ),
        # returns a view of an intermediate tensor (prims.to_dtype)
        validate_view_consistency=False,
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.meshgrid",
        torch_opinfo_name="meshgrid",
        torch_opinfo_variant_name="variadic_tensors",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.meshgrid",
        torch_opinfo_name="meshgrid",
        torch_opinfo_variant_name="list_of_tensors",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.atan",
        torch_opinfo_name="atan",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.atanh",
        torch_opinfo_name="atanh",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.bitwise_not",
        torch_opinfo_name="bitwise_not",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.ceil",
        torch_opinfo_name="ceil",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.conj_physical",
        torch_opinfo_name="conj_physical",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.cos",
        torch_opinfo_name="cos",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.cosh",
        torch_opinfo_name="cosh",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.digamma",
        torch_opinfo_name="digamma",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.erf",
        torch_opinfo_name="erf",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.erfinv",
        torch_opinfo_name="erfinv",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.erfc",
        torch_opinfo_name="erfc",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.exp",
        torch_opinfo_name="exp",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.expm1",
        torch_opinfo_name="expm1",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.exp2",
        torch_opinfo_name="exp2",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.fill",
        torch_opinfo_name="fill",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.floor",
        torch_opinfo_name="floor",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.frac",
        torch_opinfo_name="frac",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.imag",
        torch_opinfo_name="imag",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.isfinite",
        torch_opinfo_name="isfinite",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.isinf",
        torch_opinfo_name="isinf",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.isposinf",
        torch_opinfo_name="isposinf",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.isneginf",
        torch_opinfo_name="isneginf",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.isnan",
        torch_opinfo_name="isnan",
        supports_out=True,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.i0",
        torch_opinfo_name="i0",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.lgamma",
        torch_opinfo_name="lgamma",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.log",
        torch_opinfo_name="log",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.log1p",
        torch_opinfo_name="log1p",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.log10",
        torch_opinfo_name="log10",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.log2",
        torch_opinfo_name="log2",
    ),
    PythonRefInfo(
        "_refs.logsumexp",
        torch_opinfo_name="logsumexp",
        # When keepdim=False logsumexp function uses squeeze operation
        # that is not yet exposed in nvFuser's Python API.
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.log_softmax",
        torch_opinfo_name="log_softmax",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nan_to_num",
        torch_opinfo_name="nan_to_num",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.neg",
        torch_opinfo_name="neg",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.positive",
        torch_opinfo_name="positive",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.real",
        torch_opinfo_name="real",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.reciprocal",
        torch_opinfo_name="reciprocal",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.round",
        torch_opinfo_name="round",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.rsqrt",
        torch_opinfo_name="rsqrt",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.sigmoid",
        torch_opinfo_name="sigmoid",
        # Reference: https://github.com/pytorch/pytorch/issues/56012
        handles_complex_extremal_values=False,
        handles_large_floats=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.sign",
        torch_opinfo_name="sign",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.signbit",
        torch_opinfo_name="signbit",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.sin",
        torch_opinfo_name="sin",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.sinh",
        torch_opinfo_name="sinh",
    ),
    PythonRefInfo(
        "_refs.softmax",
        torch_opinfo_name="softmax",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.sqrt",
        torch_opinfo_name="sqrt",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.square",
        torch_opinfo_name="square",
        skips=(
            # AssertionError: Reference result was farther (2.2417024338305655e-07) from the precise computation
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref_executor', dtypes=(torch.complex64,)),
        ),
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.tan",
        torch_opinfo_name="tan",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.tanh",
        torch_opinfo_name="tanh",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.trunc",
        torch_opinfo_name="trunc",
    ),
    #
    # Elementwise Unary Special OpInfos
    #
    ElementwiseUnaryPythonRefInfo(
        "_refs.special.i0e",
        torch_opinfo_name="special.i0e",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.special.i1",
        torch_opinfo_name="special.i1",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.special.i1e",
        torch_opinfo_name="special.i1e",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.special.logit",
        torch_opinfo_name="logit",
        supports_nvfuser=False,
    ),
    #
    # Elementwise Unary nn.functional OpInfos
    #
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.celu",
        torch_opinfo_name="nn.functional.celu",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.threshold",
        torch_opinfo_name="nn.functional.threshold",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.nn.functional.dropout",
        torch_opinfo_name="nn.functional.dropout",
        decorators=(
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestCommon',
                         'test_python_ref'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestCommon',
                         'test_python_ref_torch_fallback'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestCommon',
                         'test_out'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestCommon',
                         'test_out_warning'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestMathBits',
                         'test_conj_view'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestMathBits',
                         'test_neg_conj_view'),
            DecorateInfo(unittest.skip("Expected: dropout is not comparable"),
                         'TestMathBits',
                         'test_neg_view'),
            # dropout is not comparable
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor'),
        )
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.elu",
        torch_opinfo_name="nn.functional.elu",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.hardtanh",
        torch_opinfo_name="nn.functional.hardtanh",
        supports_nvfuser=False,
    ),
    PythonRefInfo(  # TODO: Port this to an UnaryOpInfo
        "_refs.nn.functional.gelu",
        torch_opinfo_name="nn.functional.gelu",
    ),
    PythonRefInfo(
        "_refs.nn.functional.layer_norm",
        torch_opinfo_name="nn.functional.layer_norm",
        skips=(
            # Reference result was farther (3.5762786809723224e-07) from the precise computation
            # than the torch result was (2.5068410824946596e-07)!
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref',
                         dtypes=(torch.float32,), device_type='cpu'),
        ),
    ),
    PythonRefInfo(
        "_refs.nn.functional.leaky_relu",
        torch_opinfo_name="nn.functional.leaky_relu",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.prelu",
        torch_opinfo_name="nn.functional.prelu",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.relu",
        torch_opinfo_name="nn.functional.relu",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.relu6",
        torch_opinfo_name="nn.functional.relu6",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.mish",
        torch_opinfo_name="nn.functional.mish",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.selu",
        torch_opinfo_name="nn.functional.selu",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.softplus",
        torch_opinfo_name="nn.functional.softplus",
    ),
    PythonRefInfo(
        "_refs.nn.functional.l1_loss",
        torch_opinfo_name="nn.functional.l1_loss",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.nn.functional.margin_ranking_loss",
        torch_opinfo_name="nn.functional.margin_ranking_loss",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.nn.functional.mse_loss",
        torch_opinfo_name="nn.functional.mse_loss",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.nn.functional.hinge_embedding_loss",
        torch_opinfo_name="nn.functional.hinge_embedding_loss",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.tanhshrink",
        torch_opinfo_name="nn.functional.tanhshrink",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.hardshrink",
        torch_opinfo_name="nn.functional.hardshrink",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.nn.functional.softshrink",
        torch_opinfo_name="nn.functional.softshrink",
        supports_nvfuser=False,
    ),
    #
    # Elementwise Binary Reference OpInfos
    #
    ElementwiseBinaryPythonRefInfo(
        "_refs.add",
        torch_opinfo_name="add",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.atan2",
        torch_opinfo_name="atan2",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.bitwise_and",
        torch_opinfo_name="bitwise_and",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.bitwise_left_shift",
        torch_opinfo_name="bitwise_left_shift",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.bitwise_or",
        torch_opinfo_name="bitwise_or",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.bitwise_xor",
        torch_opinfo_name="bitwise_xor",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.copysign",
        torch_opinfo_name="copysign",
        supports_nvfuser=False,
        skips=(
            # RuntimeError: Expected divisor (b) to be on the same device (cuda:0) as dividend (a), but it is found on cpu!
            DecorateInfo(unittest.skip("Skipped!"), 'TestBinaryUfuncs', 'test_type_promotion'),
        )
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.div",
        torch_opinfo_name="div",
        torch_opinfo_variant_name="no_rounding_mode",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        supports_nvfuser=False,
        skips=(
            # NotImplementedError: argument of type: <class 'complex'>
            DecorateInfo(
                unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref_executor',
                dtypes=(torch.complex32, torch.complex64, torch.complex128,)
            ),
            # Reference result was farther (0.7433461727239705) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
            # Reference result was farther (0.7433461727239705) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.div",
        torch_opinfo_name="div",
        torch_opinfo_variant_name="trunc_rounding",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.div",
        torch_opinfo_name="div",
        torch_opinfo_variant_name="floor_rounding",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.eq",
        torch_opinfo_name="eq",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.float_power",
        torch_opinfo_name="float_power",
        supports_nvfuser=False,
        skips=(
            # Test doesn't account for float -> double type promotion
            DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion'),
        )
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.floor_divide",
        torch_opinfo_name="floor_divide",
        rhs_make_tensor_kwargs=dict(exclude_zero=True),
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        supports_nvfuser=False,
        # bfloat16 floor_divide compared with a float32 reference works inconsistently
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref',
                         dtypes=(torch.bfloat16,)),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.bfloat16,)),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.fmax",
        torch_opinfo_name="fmax",
        supports_rhs_python_scalar=False,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.fmin",
        torch_opinfo_name="fmin",
        supports_rhs_python_scalar=False,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.fmod",
        torch_opinfo_name="fmod",
        rhs_make_tensor_kwargs={'exclude_zero': True},
        supports_rhs_python_scalar=True,
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref',
                         dtypes=(torch.bfloat16,), device_type='cpu'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.bfloat16,), device_type='cpu'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.gcd",
        torch_opinfo_name="gcd",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.ge",
        torch_opinfo_name="ge",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.gt",
        torch_opinfo_name="gt",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.heaviside",
        torch_opinfo_name="heaviside",
        supports_rhs_python_scalar=False,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.hypot",
        torch_opinfo_name="hypot",
        supports_rhs_python_scalar=False,
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.igamma",
        torch_opinfo_name="igamma",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.igammac",
        torch_opinfo_name="igammac",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.isclose",
        torch_opinfo_name="isclose",
        supports_nvfuser=False,
        skips=(
            # Intentional xfail -- isclose does not type promote
            DecorateInfo(unittest.expectedFailure, 'TestBinaryUfuncs', 'test_type_promotion'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.lcm",
        torch_opinfo_name="lcm",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.le",
        torch_opinfo_name="le",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.logical_and",
        torch_opinfo_name="logical_and",
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.logical_not",
        torch_opinfo_name="logical_not",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.logical_or",
        torch_opinfo_name="logical_or",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.logical_xor",
        torch_opinfo_name="logical_xor",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.lt",
        torch_opinfo_name="lt",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.maximum",
        torch_opinfo_name="maximum",
        supports_nvfuser=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.minimum",
        torch_opinfo_name="minimum",
        supports_nvfuser=False,
        skips=(
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.mul",
        torch_opinfo_name="mul",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        skips=(
            # Reference result was farther (0.0) from the precise computation
            # than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor',
                dtypes=(torch.complex32,),
            ),

            # Reference result was farther (0.0) from the precise computation
            # than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                dtypes=(torch.complex32,), device_type='cuda'
            ),
            # Reference result was farther (0.0) from the precise computation
            # than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                dtypes=(torch.complex32,), device_type='cuda'
            ),
        )
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.ne",
        torch_opinfo_name="ne",
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.nextafter",
        torch_opinfo_name="nextafter",
        supports_nvfuser=False,
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.pow",
        torch_opinfo_name="pow",
        supports_nvfuser=False,  # clone default
        skips=(
            # Reference result was farther (inf) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor',
                dtypes=(torch.complex32,),
            ),
            # Reference result was farther (inf) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
            # Reference result was farther (inf) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.remainder",
        torch_opinfo_name="remainder",
        skips=(
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref',
                         dtypes=(torch.bfloat16,), device_type='cpu'),
            DecorateInfo(unittest.skip("Skipped!"), 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.bfloat16,), device_type='cpu'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.rsub",
        torch_opinfo_name="rsub",
        # https://github.com/pytorch/pytorch/issues/76944
        skips=(
            # Reference result was farther (nan) from the precise computation than
            # the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.chalf,), device_type='cpu'),
            # Reference result was farther (nan) from the precise computation than
            # the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.chalf,), device_type='cpu'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.sub",
        torch_opinfo_name="sub",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        skips=(
            # Reference result was farther (nan) from the precise computation than
            # the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                         dtypes=(torch.chalf,), device_type='cpu'),
            # Reference result was farther (nan) from the precise computation than
            # the torch result was (nan)!
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                         dtypes=(torch.chalf,), device_type='cpu'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.true_divide",
        torch_opinfo_name="true_divide",
        # https://github.com/pytorch/pytorch/issues/76944
        supports_two_python_scalars=False,
        supports_one_python_scalar=True,
        skips=(
            # Reference result was farther (0.7433461727239705) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor',
                dtypes=(torch.complex32,),
            ),
            # Reference result was farther (0.7433461727239705) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
            # Reference result was farther (0.7433461727239705) from the precise
            # computation than the torch result was (nan)!
            DecorateInfo(
                unittest.expectedFailure, 'TestCommon', 'test_python_ref_torch_fallback',
                dtypes=(torch.complex32,), device_type="cuda"
            ),
        ),
    ),
    #
    # Elementwise Binary Special OpInfos
    #
    ElementwiseBinaryPythonRefInfo(
        "_refs.special.zeta",
        torch_opinfo_name="special.zeta",
        supports_one_python_scalar=True,
        supports_nvfuser=False,
    ),
    #
    # Elementwise Ternary Reference OpInfos
    #
    ElementwiseBinaryPythonRefInfo(
        "_refs.clamp_min",
        torch_opinfo_name="clamp_min",
        supports_nvfuser=False,
        skips=(
            # test error disabled since rhs non-tensor python scalar is supported
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    ElementwiseBinaryPythonRefInfo(
        "_refs.clamp_max",
        torch_opinfo_name="clamp_max",
        supports_nvfuser=False,
        skips=(
            # test error disabled since rhs non-tensor python scalar is supported
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    PythonRefInfo(
        "_refs.clamp",
        torch_opinfo_name="clamp",
        supports_nvfuser=False,
    ),
    #
    # Data Conversion & Data Movement Opinfos
    #
    PythonRefInfo(
        "_refs.clone",
        torch_opinfo_name="clone",
        supports_nvfuser=False,
    ),
    #
    # View & Shape OpInfos
    #
    PythonRefInfo(
        "_refs.atleast_1d",
        torch_opinfo_name="atleast_1d",
        validate_view_consistency=False,
        supports_nvfuser=False
    ),
    PythonRefInfo(
        "_refs.atleast_2d",
        torch_opinfo_name="atleast_2d",
        validate_view_consistency=False,
        supports_nvfuser=False
    ),
    PythonRefInfo(
        "_refs.atleast_3d",
        torch_opinfo_name="atleast_3d",
        validate_view_consistency=False,
        supports_nvfuser=False
    ),
    PythonRefInfo(
        "_refs.as_strided",
        torch_opinfo_name="as_strided",
        # FIXME: doesn't support chalf
        dtypes=all_types_and_complex_and(torch.bool, torch.float16, torch.bfloat16),
        supports_nvfuser=False,
        skips=(
            # TODO: fix and/or update to xfails
            DecorateInfo(unittest.skip("Errors when storage_offset is included"),
                         'TestCommon', 'test_python_ref_meta'),
            # cloned_mutable_input.is_same(returned_output) INTERNAL ASSERT FAILED
            DecorateInfo(unittest.skip("Errors when storage_offset is included"), 'TestMathBits', 'test_neg_view'),
            DecorateInfo(unittest.skip("Errors when storage_offset is included"), 'TestMathBits', 'test_conj_view'),
            DecorateInfo(unittest.skip("Errors when storage_offset is included"), 'TestMathBits', 'test_neg_conj_view'),
        ),
    ),
    PythonRefInfo(
        "_refs.broadcast_shapes",
        torch_opinfo_name="broadcast_shapes",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.broadcast_tensors",
        torch_opinfo_name="broadcast_tensors",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.broadcast_to",
        torch_opinfo_name="broadcast_to",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.cat",
        torch_opinfo_name="cat",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.chunk",
        torch_opinfo_name="chunk",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.column_stack",
        torch_opinfo_name="column_stack",
        supports_nvfuser=False,
    ),
    ElementwiseUnaryPythonRefInfo(
        "_refs.conj",
        torch_opinfo_name="conj",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.constant_pad_nd",
        torch_opinfo_name="constant_pad_nd",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.contiguous",
        torch_opinfo_name="contiguous",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.dsplit",
        torch_opinfo_name="dsplit",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.diagonal",
        torch_opinfo_name="diagonal",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.dstack",
        torch_opinfo_name="dstack",
        supports_nvfuser=False,
        skips=(
            # https://github.com/pytorch/pytorch/issues/78613
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    PythonRefInfo(
        "_refs.expand",
        torch_opinfo_name="expand",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.flatten",
        torch_opinfo_name="flatten",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.flip",
        torch_opinfo_name="flip",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.fliplr",
        torch_opinfo_name="fliplr",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.flipud",
        torch_opinfo_name="flipud",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.hstack",
        torch_opinfo_name="hstack",
        supports_nvfuser=False,
        skips=(
            # https://github.com/pytorch/pytorch/issues/78613
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    PythonRefInfo(
        "_refs.narrow",
        torch_opinfo_name="narrow",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.native_layer_norm",
        torch_opinfo_name="native_layer_norm",
    ),
    PythonRefInfo(
        "_refs.permute",
        torch_opinfo_name="permute",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.ravel",
        torch_opinfo_name="ravel",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.reshape",
        torch_opinfo_name="reshape",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.roll",
        torch_opinfo_name="roll",
        validate_view_consistency=False,
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.rot90",
        torch_opinfo_name="rot90",
        validate_view_consistency=False,
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.stack",
        torch_opinfo_name="stack",
        supports_nvfuser=False,
        validate_view_consistency=False,
    ),
    PythonRefInfo(
        "_refs.squeeze",
        torch_opinfo_name="squeeze",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.tensor_split",
        torch_opinfo_name="tensor_split",
        skips=(
            # TensorMeta doesn't support tolist
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_meta'),
            # RuntimeError: no _refs support for torch.Tensor.tolist
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref'),
            # RuntimeError: .tolist() is not supported for tensor subclasses.
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_executor'),
        )
    ),
    PythonRefInfo(
        "_refs.hsplit",
        torch_opinfo_name="hsplit",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.vsplit",
        torch_opinfo_name="vsplit",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.transpose",
        torch_opinfo_name="transpose",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.t",
        torch_opinfo_name="t",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.unsqueeze",
        torch_opinfo_name="unsqueeze",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.view",
        torch_opinfo_name="view",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.vstack",
        torch_opinfo_name="vstack",
        supports_nvfuser=False,
        skips=(
            # https://github.com/pytorch/pytorch/issues/78613
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref_errors'),
        ),
    ),
    PythonRefInfo(
        "_refs.unflatten",
        torch_opinfo_name="unflatten",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.unbind",
        torch_opinfo_name="unbind",
        supports_nvfuser=False,
    ),
    #
    # Reduction Reference OpInfos
    #
    ReductionPythonRefInfo(
        "_refs.all",
        torch_opinfo_name="all",
    ),
    ReductionPythonRefInfo(
        "_refs.amax",
        torch_opinfo_name="amax",
    ),
    ReductionPythonRefInfo(
        "_refs.amin",
        torch_opinfo_name="amin",
    ),
    ReductionPythonRefInfo(
        "_refs.any",
        torch_opinfo_name="any",
    ),
    ReductionPythonRefInfo(
        "_refs.mean",
        torch_opinfo_name="mean",
        supports_out=True,
    ),
    ReductionPythonRefInfo(
        "_refs.std",
        torch_opinfo_name="std",
        supports_out=True,
    ),
    # std_mean and var_mean are not ReductionInfos
    PythonRefInfo(
        "_refs.std_mean",
        torch_opinfo_name="std_mean",
        validate_view_consistency=False,
    ),
    ReductionPythonRefInfo(
        "_refs.sum",
        torch_opinfo_name="sum",
        supports_out=True,
    ),
    ReductionPythonRefInfo(
        "_refs.prod",
        torch_opinfo_name="prod",
        supports_out=True,
        supports_nvfuser=False,
    ),
    ReductionPythonRefInfo(
        "_refs.var",
        torch_opinfo_name="var",
        supports_out=True,
    ),
    PythonRefInfo(
        "_refs.var_mean",
        torch_opinfo_name="var_mean",
        validate_view_consistency=False,
    ),
    #
    # Linear Algebra Operators
    #
    PythonRefInfo(
        "_refs.addr",
        torch_opinfo_name="addr",
        supports_nvfuser=False,
        decorators=(
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref',),
        ),
    ),
    PythonRefInfo(
        "_refs.trace",
        torch_opinfo_name="trace",
        decorators=(
            # TODO: torch.diag is currently not supported by either refs, meta funcs, or NVFuser
            DecorateInfo(unittest.expectedFailure, 'TestCommon', 'test_python_ref'),
            DecorateInfo(unittest.skip("diag is not supported by meta"), 'TestCommon', 'test_python_ref_meta'),
            DecorateInfo(unittest.skip("diag is not supported by nvfuser"), 'TestCommon', 'test_python_ref_executor'),
        ),
    ),
    PythonRefInfo(
        "_refs.norm",
        torch_opinfo_name="norm",
        supports_out=True,
        # Uses svdvals which does not support nvfuser
        supports_nvfuser=False,
        # Uses vector_norm inside and vector_norm is affected by
        # https://github.com/pytorch/pytorch/issues/77216
        validate_view_consistency=False,
    ),
    #
    # torch.linalg
    #
    ReductionPythonRefInfo(
        "_refs.linalg.vector_norm",
        torch_opinfo_name="linalg.vector_norm",
        supports_out=True,
        supports_nvfuser=False,  # clone_default
    ),
    PythonRefInfo(
        "_refs.linalg.matrix_norm",
        torch_opinfo_name="linalg.matrix_norm",
        supports_out=True,
        # Uses svdvals which does not support nvfuser
        supports_nvfuser=False,
        # Uses vector_norm inside and vector_norm is affected by
        # https://github.com/pytorch/pytorch/issues/77216
        validate_view_consistency=False,
    ),
    PythonRefInfo(
        "_refs.linalg.norm",
        torch_opinfo_name="linalg.norm",
        supports_out=True,
        # Uses svdvals which does not support nvfuser
        supports_nvfuser=False,
        # Uses vector_norm inside and vector_norm is affected by
        # https://github.com/pytorch/pytorch/issues/77216
        validate_view_consistency=False,
    ),
    PythonRefInfo(
        "_refs.linalg.svd",
        torch_opinfo_name="linalg.svd",
        supports_out=True,
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.linalg.svdvals",
        torch_opinfo_name="linalg.svdvals",
        supports_out=True,
        supports_nvfuser=False,
    ),
    #
    # Tensor Creation Reference OpInfos
    #
    PythonRefInfo(
        "_refs.empty",
        torch_opinfo_name="empty",
        skips=(
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref_torch_fallback'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out_warning'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_view'),
            # FIXME: shouldn't check empty results
            DecorateInfo(unittest.skip("Can't check result for empty"), 'TestCommon', 'test_python_ref_executor'),
        ),
    ),
    PythonRefInfo(
        "_refs.empty_like",
        torch_opinfo_name="empty_like",
        supports_nvfuser=False,
        skips=(
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref_torch_fallback'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out_warning'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_view'),
            # FIXME: should not compare results of empty_like
            DecorateInfo(unittest.skip("Can't check result for empty_like"), 'TestCommon', 'test_python_ref_executor'),
        ),
    ),
    PythonRefInfo(
        "_refs.new_empty",
        torch_opinfo_name="new_empty",
        supports_nvfuser=False,
        skips=(
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_python_ref_torch_fallback'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestCommon',
                         'test_out_warning'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_conj_view'),
            DecorateInfo(unittest.skip("Expected: empty is not comparable"),
                         'TestMathBits',
                         'test_neg_view'),
            # FIXME: should not compare results of empty_like
            DecorateInfo(unittest.skip("Can't check result for new_empty"), 'TestCommon', 'test_python_ref_executor'),
        ),
    ),
    PythonRefInfo(
        "_refs.new_full",
        torch_opinfo_name="new_full",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.new_ones",
        torch_opinfo_name="new_ones",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.new_zeros",
        torch_opinfo_name="new_zeros",
        supports_nvfuser=False,
    ),
    #
    # Conditional Reference OpInfos
    #
    PythonRefInfo(
        "_refs.masked_fill",
        torch_opinfo_name="masked_fill",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.where",
        torch_opinfo_name="where",
        op=lambda self, condition, other: refs.where(condition, self, other),
        supports_nvfuser=False,
    ),
    #
    # Test-related functions
    #
    PythonRefInfo(
        "_refs.allclose",
        torch_opinfo_name="allclose",
        supports_nvfuser=False,
    ),
    #
    # FFT OpInfos
    #
    SpectralFuncPythonRefInfo(
        "_refs.fft.fft",
        torch_opinfo_name="fft.fft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ifft",
        torch_opinfo_name="fft.ifft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.rfft",
        torch_opinfo_name="fft.rfft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.irfft",
        torch_opinfo_name="fft.irfft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.hfft",
        torch_opinfo_name="fft.hfft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ihfft",
        torch_opinfo_name="fft.ihfft",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.fftn",
        torch_opinfo_name="fft.fftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ifftn",
        torch_opinfo_name="fft.ifftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.rfftn",
        torch_opinfo_name="fft.rfftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.irfftn",
        torch_opinfo_name="fft.irfftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.hfftn",
        torch_opinfo_name="fft.hfftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ihfftn",
        torch_opinfo_name="fft.ihfftn",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.fft2",
        torch_opinfo_name="fft.fft2",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ifft2",
        torch_opinfo_name="fft.ifft2",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.rfft2",
        torch_opinfo_name="fft.rfft2",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.irfft2",
        torch_opinfo_name="fft.irfft2",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.hfft2",
        torch_opinfo_name="fft.hfft2",
        supports_nvfuser=False,
    ),
    SpectralFuncPythonRefInfo(
        "_refs.fft.ihfft2",
        torch_opinfo_name="fft.ihfft2",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.fft.fftshift",
        torch_opinfo_name="fft.fftshift",
        supports_nvfuser=False,
    ),
    PythonRefInfo(
        "_refs.fft.ifftshift",
        torch_opinfo_name="fft.ifftshift",
        supports_nvfuser=False,
    ),
]

# Common operator groupings
ops_and_refs = op_db + python_ref_db
unary_ufuncs = [op for op in op_db if isinstance(op, UnaryUfuncInfo)]
binary_ufuncs = [op for op in op_db if isinstance(op, BinaryUfuncInfo)]
binary_ufuncs_and_refs = tuple(op for op in ops_and_refs if isinstance(op, BinaryUfuncInfo))
spectral_funcs = [op for op in op_db if isinstance(op, SpectralFuncInfo)]
sparse_unary_ufuncs = [op for op in op_db if isinstance(op, UnaryUfuncInfo) and op.supports_sparse]
sparse_csr_unary_ufuncs = [op for op in op_db if isinstance(op, UnaryUfuncInfo) and op.supports_sparse_csr]
sparse_reduction_ops = [op for op in op_db if isinstance(op, ReductionOpInfo) and op.supports_sparse]
shape_funcs = [op for op in op_db if isinstance(op, ShapeFuncInfo)]
reduction_ops = [op for op in op_db if isinstance(op, ReductionOpInfo)]
reference_filtered_ops = [op for op in reduction_ops if op.ref is not None]
reference_masked_ops = [op for op in reference_filtered_ops if op.name.startswith('_masked.')]
sparse_masked_reduction_ops = [op for op in sparse_reduction_ops if op.name.startswith('_masked.')]

# TODO: review porting these to make_tensor
def index_variable(shape, max_indices, device=torch.device('cpu')):
    if not isinstance(shape, tuple):
        shape = (shape,)
    index = torch.rand(*shape, dtype=torch.double, device=device).mul_(max_indices).floor_().long()
    return index

def gather_variable(shape, index_dim, max_indices, duplicate=False, device=torch.device('cpu')):
    assert len(shape) == 2
    assert index_dim < 2
    batch_dim = 1 - index_dim
    index = torch.zeros(*shape, dtype=torch.long, device=device)
    for i in range(shape[index_dim]):
        index.select(index_dim, i).copy_(
            torch.randperm(max_indices, device=device)[:shape[batch_dim]])
    if duplicate:
        index.select(batch_dim, 0).copy_(index.select(batch_dim, 1))
    return index

def bernoulli_scalar():
    return torch.tensor(0, dtype=torch.bool).bernoulli_()

def mask_not_all_zeros(shape):
    assert len(shape) > 0
    while True:
        result = torch.randn(shape).gt(0)
        if result.sum() > 0:
            return result


# TODO: move all tri/tril/triu testing to tensor creation op test suite and remove
#   these from here
def _compare_trilu_indices(
        self, row, col, offset=0, dtype=torch.long, device='cpu'):
    if row == 0 or col == 0:
        # have to handle this separately as tril and triu does not take
        # empty matrix as input
        self.assertEqual(
            torch.empty(0, 2, dtype=dtype, device=device).transpose(0, 1),
            torch.tril_indices(row, col, offset, dtype=dtype, device=device))

        self.assertEqual(
            torch.empty(0, 2, dtype=dtype, device=device).transpose(0, 1),
            torch.triu_indices(row, col, offset, dtype=dtype, device=device))

    else:
        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(
            torch.ones(row, col, device='cpu')
                 .tril(offset).nonzero().to(dtype).transpose(0, 1),
            torch.tril_indices(row, col, offset, dtype=dtype, device=device))

        # TODO(#38095): Replace assertEqualIgnoreType. See issue #38095
        self.assertEqualIgnoreType(
            torch.ones(row, col, device='cpu')
                 .triu(offset).nonzero().to(dtype).transpose(0, 1),
            torch.triu_indices(row, col, offset, dtype=dtype, device=device))


def _compare_large_trilu_indices(
        self, row, col, offset=0, dtype=torch.long, device='cpu'):
    l = torch.ones(row, col, dtype=dtype, device='cpu').tril(offset) \
             .nonzero()[-100:-1, :].transpose(0, 1).to(device)
    torch.cuda.empty_cache()

    r = torch.tril_indices(
        row, col, offset, dtype=dtype, device=device)[:, -100:-1]
    self.assertEqual(l, r)
    torch.cuda.empty_cache()

    l = torch.ones(row, col, dtype=dtype, device='cpu').triu(offset) \
             .nonzero()[-100:-1, :].transpose(0, 1).to(device)
    torch.cuda.empty_cache()

    r = torch.triu_indices(
        row, col, offset, dtype=dtype, device=device)[:, -100:-1]
    self.assertEqual(l, r)
    torch.cuda.empty_cache()

# (
#   row
#   col
#   offset (optional)
#   dtype (optional)
# )
tri_tests_args = [
    (1, 1),
    (3, 3),
    (3, 3, 1),
    (3, 3, 2),
    (3, 3, 200),
    (3, 3, -1),
    (3, 3, -2),
    (3, 3, -200),
    (0, 3, 0),
    (0, 3, 1),
    (0, 3, -1),
    (0, 1, 2),
    (1, 0, 2),
    (3, 0, 0),
    (3, 0, 1),
    (3, 0, -1),
    (0, 0, 0),
    (0, 0, 1),
    (0, 0, -1),
    (3, 6, 0),
    (3, 6, 1),
    (3, 6, 3),
    (3, 6, 9),
    (3, 6, -1),
    (3, 6, -3),
    (3, 6, -9),
    (6, 3, 0),
    (6, 3, 1),
    (6, 3, 3),
    (6, 3, 9),
    (6, 3, -1),
    (6, 3, -3),
    (6, 3, -9),
    (258, 253, 1, torch.float32),
    (257, 258, 1, torch.float64),
    (258, 258, 1, torch.short),
    (3, 513, 1, torch.long),
    (513, 3, 1, torch.int),
    (513, 0, 1, torch.double),
    (1024, 1024),
    (1024, 1024, 500, torch.float32),
    (1024, 1024, 1023),
    (1024, 1024, -500),
    (1023, 1025),
    (1025, 1023, 1022),
    (1024, 1024, -500),
    (3, 2028),
    (3, 2028, 1),
    (3, 2028, -1),
    (2028, 3),
    (2028, 1),
    (2028, 1, -1)
]

tri_large_tests_args: List[Tuple[int, ...]] = [
    # Large test cases below are deliberately commented out to speed up CI
    # tests and to avoid OOM error. When modifying implementations of
    # tril_indices and triu_indices, please enable these tests and make sure
    # they pass.
    #
    # (1, 268435455),
    # (5000, 5000),
    # (10000, 10000),
    # (268435455, 1),
    # (134217727, 2, 1),
    # (2, 134217727, 1),
    # (536870901, 1),
    # (1, 536870901),
    # (268435455, 2, 1),
    # (2, 268435455, 1)
]


def run_additional_tri_tests(self, device):
    x = torch.ones(
        3, 3, dtype=torch.long, device=device, layout=torch.strided)
    l = x.tril(0).nonzero().transpose(0, 1)
    u = x.triu(0).nonzero().transpose(0, 1)
    self.assertEqual(l, torch.tril_indices(3, 3, device=device))
    self.assertEqual(
        l, torch.tril_indices(3, 3, device=device, layout=torch.strided))

    self.assertEqual(u, torch.triu_indices(3, 3, device=device))
    self.assertEqual(
        u, torch.triu_indices(3, 3, device=device, layout=torch.strided))

    self.assertRaises(
        RuntimeError,
        lambda: torch.triu_indices(
            1, 1, device=device, layout=torch.sparse_coo))

    self.assertRaises(
        RuntimeError,
        lambda: torch.tril_indices(
            1, 1, device=device, layout=torch.sparse_coo))
