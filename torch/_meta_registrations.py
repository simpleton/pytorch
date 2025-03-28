import torch
from torch import Tensor
import torch._prims_common as utils
from torch._prims_common import (
    ELEMENTWISE_TYPE_PROMOTION_KIND,
    check,
    elementwise_dtypes,
)
from torch._prims_common.wrappers import out_wrapper
from torch.utils._pytree import tree_map

from typing import List, Optional

aten = torch.ops.aten

meta_lib = torch.library.Library("aten", "IMPL", "Meta")

meta_table = {}


def register_meta(op, register_dispatcher=True):
    def wrapper(f):
        def add_func(op):
            meta_table[op] = f
            if register_dispatcher:
                name = (
                    op.__name__
                    if op._overloadname != "default"
                    else op.overloadpacket.__name__
                )
                meta_lib.impl(name, f)

        tree_map(add_func, op)
        return f

    return wrapper


def toRealValueType(dtype):
    from_complex = {
        torch.complex32: torch.half,
        torch.cfloat: torch.float,
        torch.cdouble: torch.double,
    }
    return from_complex.get(dtype, dtype)


@register_meta(aten._fft_c2c.default)
def meta_fft_c2c(self, dim, normalization, forward):
    assert self.dtype.is_complex
    return self.new_empty(self.size())


@register_meta(aten._fft_r2c.default)
def meta_fft_r2c(self, dim, normalization, onesided):
    assert self.dtype.is_floating_point
    output_sizes = list(self.size())

    if onesided:
        last_dim = dim[-1]
        last_dim_halfsize = (output_sizes[last_dim] // 2) + 1
        output_sizes[last_dim] = last_dim_halfsize

    return self.new_empty(
        output_sizes, dtype=utils.corresponding_complex_dtype(self.dtype)
    )


@register_meta([aten._fft_c2r.default, aten._fft_c2r.out])
@out_wrapper()
def meta_fft_c2r(self, dim, normalization, lastdim):
    assert self.dtype.is_complex
    output_sizes = list(self.size())
    output_sizes[dim[-1]] = lastdim
    return self.new_empty(output_sizes, dtype=toRealValueType(self.dtype))


@register_meta([aten.conj_physical.out])
def meta_conj_physical_out(self, out):
    return torch._resize_output_(out, self.size(), self.device)


# Implementations below are taken from https://github.com/albanD/subclass_zoo/blob/main/python_meta_tensor.py
@register_meta(aten.index_select.default)
def meta_index_select(self, dim, index):
    result_size = list(self.size())
    if self.dim() > 0:
        result_size[dim] = index.numel()
    return self.new_empty(result_size)


@register_meta(aten.index_select.out)
def meta_index_select_out(self, dim, index, out):
    torch._resize_output_(out, self.size(), self.device)
    return out.copy_(torch.index_select(self, dim, index))


@register_meta([aten.max.default, aten.min.default])
def meta_max(self):
    return self.new_empty(())


@register_meta(aten.angle.default)
def meta_angle(self):
    _, result_dtype = elementwise_dtypes(
        self, type_promotion_kind=ELEMENTWISE_TYPE_PROMOTION_KIND.INT_TO_FLOAT
    )
    return self.new_empty(self.size(), dtype=result_dtype)


@register_meta(aten.angle.out)
def meta_angle_out(self, out):
    torch._resize_output_(out, self.size(), self.device)
    return out.copy_(torch.angle(self))


def squareCheckInputs(self, f_name):
    assert (
        self.dim() >= 2
    ), f"{f_name}: The input tensor must have at least 2 dimensions."
    assert self.size(-1) == self.size(
        -2
    ), f"{f_name}: A must be batches of square matrices, but they are {self.size(-2)} by {self.size(-1)} matrices"


def checkUplo(uplo: str):
    uplo_uppercase = uplo.upper()
    assert (
        len(uplo) == 1 and uplo_uppercase == "U" or uplo_uppercase == "L"
    ), f"Expected UPLO argument to be 'L' or 'U', but got {uplo}"


# @register_meta(aten.linalg_eigh.default)
def meta_linalg_eigh(self, uplo="L"):
    squareCheckInputs(self, "linalg_eigh")
    checkUplo(uplo)
    real_dtype = toRealValueType(self.dtype)
    assert self.dim() >= 2
    values = self.new_empty(self.shape, dtype=real_dtype)
    values.transpose_(-2, -1)
    vectors = self.new_empty(self.shape[:-1])
    return (values, vectors)


@register_meta(aten.reflection_pad2d.default)
def meta_pad2d(self, padding):
    valid_dims = self.size(1) != 0 and self.size(2) != 0
    check(
        (self.ndim == 3 and valid_dims)
        or (self.ndim == 4 and valid_dims and self.size(3) != 0),
        lambda: f"3D or 4D (batch mode) tensor expected for input, but got: {self}",
    )
    if self.ndim == 4:
        nbatch, nplane, input_h, input_w = self.shape
    else:
        nbatch = 1
        nplane, input_h, input_w = self.shape

    pad_l, pad_r, pad_t, pad_b = padding

    output_h = input_h + pad_t + pad_b
    output_w = input_w + pad_l + pad_r

    if self.ndim == 3:
        return self.new_empty((nplane, output_h, output_w))
    else:
        return self.new_empty((nbatch, nplane, output_h, output_w))


@register_meta(aten.dot.default)
def meta_dot(self, tensor):
    check(
        self.dim() == 1 and tensor.dim() == 1,
        lambda: f"1D tensors expected, but got {self.dim()}D and {tensor.dim()}D tensors",
    )
    return self.new_empty(())


def _compute_reduction_shape(self, dims, keepdim):
    if keepdim:
        return tuple(self.shape[i] if i not in dims else 1 for i in range(self.ndim))

    return utils.compute_reduction_output_shape(self.shape, dims)


@register_meta(aten.var_mean.correction)
def meta_var_mean_correction(self, dim, *, correction, keepdim=False):
    dim = utils.reduction_dims(self.shape, dim)
    output_shape = _compute_reduction_shape(self, dim, keepdim)
    result1 = self.new_empty(output_shape, dtype=toRealValueType(self.dtype))
    result2 = self.new_empty(output_shape)
    return result1, result2


@register_meta(aten.inverse.default)
def meta_inverse(self):
    # Bug: https://github.com/pytorch/pytorch/issues/77498
    if self.numel() == 0:
        return torch.empty_like(self)
    r = self.new_empty(self.shape)
    r.transpose_(-2, -1)
    return r


@torch.library.impl(meta_lib, "bernoulli.out")
def meta_bernoulli(self, *, generator=None, out):
    torch._resize_output_(out, self.size(), self.device)
    return out


@register_meta(aten._adaptive_avg_pool2d.default)
def meta_adaptive_avg_pool2d(self, output_size):
    check(
        self.ndim == 3 or self.ndim == 4,
        lambda: f"Expected 3D or 4D tensor, but got {self.shape}",
    )
    return self.new_empty(self.shape[:-2] + tuple(output_size))


@torch.library.impl(meta_lib, "_adaptive_avg_pool3d")
def meta_adaptive_avg_pool3d(self, output_size):
    check(
        self.ndim == 4 or self.ndim == 5,
        lambda: f"Expected 4D or 5D tensor, but got {self.shape}",
    )
    return self.new_empty(self.shape[:-3] + tuple(output_size))


@torch.library.impl(meta_lib, "repeat_interleave.Tensor")
def meta_repeat_interleave_Tensor(repeats, output_size=None):
    if output_size is None:
        raise RuntimeError("cannot repeat_interleave a meta tensor without output_size")
    return repeats.new_empty(output_size)


@register_meta(aten.index.Tensor, register_dispatcher=False)
def meta_index_Tensor(self, indices):
    check(indices, lambda: "at least one index must be provided")
    # aten::index is the internal advanced indexing implementation
    # checkIndexTensorTypes and expandTensors
    result: List[Optional[Tensor]] = []
    for i, index in enumerate(indices):
        if index is not None:
            check(
                index.dtype in [torch.long, torch.int8, torch.bool],
                lambda: "tensors used as indices must be long, byte or bool tensors",
            )
            if index.dtype in [torch.int8, torch.bool]:
                nonzero = index.nonzero()
                k = len(result)
                check(
                    k + index.ndim <= self.ndim,
                    lambda: f"too many indices for tensor of dimension {self.ndim}",
                    IndexError,
                )
                for j in range(index.ndim):
                    check(
                        index.shape[j] == self.shape[k + j],
                        lambda: f"The shape of the mask {index.shape} at index {i} "
                        f"does not match the shape of the indexed tensor {self.shape} at index {k + j}",
                        IndexError,
                    )
                    result.append(nonzero.select(1, j))
            else:
                result.append(index)
        else:
            result.append(index)
    indices = result
    check(
        len(indices) <= self.ndim,
        lambda: f"too many indices for tensor of dimension {self.ndim} (got {len(indices)})",
    )
    # expand_outplace
    import torch._refs as refs  # avoid import cycle in mypy

    indices = list(refs._maybe_broadcast(*indices))
    # add missing null tensors
    while len(indices) < self.ndim:
        indices.append(None)

    # hasContiguousSubspace
    #   true if all non-null tensors are adjacent
    # See:
    # https://numpy.org/doc/stable/user/basics.indexing.html#combining-advanced-and-basic-indexing
    # https://stackoverflow.com/questions/53841497/why-does-numpy-mixed-basic-advanced-indexing-depend-on-slice-adjacency
    state = 0
    has_contiguous_subspace = False
    for index in indices:
        if state == 0:
            if index is not None:
                state = 1
        elif state == 1:
            if index is None:
                state = 2
        else:
            if index is not None:
                break
    else:
        has_contiguous_subspace = True

    # transposeToFront
    # This is the logic that causes the newly inserted dimensions to show up
    # at the beginning of the tensor, if they're not contiguous
    if not has_contiguous_subspace:
        dims = []
        transposed_indices = []
        for i, index in enumerate(indices):
            if index is not None:
                dims.append(i)
                transposed_indices.append(index)
        for i, index in enumerate(indices):
            if index is None:
                dims.append(i)
                transposed_indices.append(index)
        self = self.permute(dims)
        indices = transposed_indices

    # AdvancedIndex::AdvancedIndex
    # Now we can assume the indices have contiguous subspace
    # This is simplified from AdvancedIndex which goes to more effort
    # to put the input and indices in a form so that TensorIterator can
    # take them.  If we write a ref for this, probably that logic should
    # get implemented
    before_shape: List[int] = []
    after_shape: List[int] = []
    replacement_shape: List[int] = []
    for dim, index in enumerate(indices):
        if index is None:
            if replacement_shape:
                after_shape.append(self.shape[dim])
            else:
                before_shape.append(self.shape[dim])
        else:
            replacement_shape = list(index.shape)
    return self.new_empty(before_shape + replacement_shape + after_shape)


@torch.library.impl(meta_lib, "addbmm")
@torch.library.impl(meta_lib, "addbmm.out")
@out_wrapper()
def meta_addbmm(self, batch1, batch2, *, beta=1, alpha=1):
    dim1 = batch1.size(1)
    dim2 = batch2.size(2)
    self = self.expand((dim1, dim2))
    check(batch1.dim() == 3, lambda: "batch1 must be a 3D tensor")
    check(batch2.dim() == 3, lambda: "batch2 must be a 3D tensor")
    check(
        batch1.size(0) == batch2.size(0),
        lambda: f"batch1 and batch2 must have same number of batches, got {batch1.size(0)} and {batch2.size(0)}",
    )
    check(
        batch1.size(2) == batch2.size(1),
        lambda: (
            f"Incompatible matrix sizes for bmm ({batch1.size(1)}x{batch1.size(2)} "
            f"and {batch2.size(1)}x{batch2.size(2)})"
        ),
    )
    check(
        self.size(0) == dim1 and self.size(1) == dim2,
        lambda: "self tensor does not match matmul output shape",
    )
    return self.new_empty(self.size())


@torch.library.impl(meta_lib, "_cdist_forward")
def meta_cdist_forward(x1, x2, p, compute_mode):
    check(
        x1.dim() >= 2,
        lambda: f"cdist only supports at least 2D tensors, X1 got: {x1.dim()}D",
    )
    check(
        x2.dim() >= 2,
        lambda: f"cdist only supports at least 2D tensors, X2 got: {x2.dim()}D",
    )
    check(
        x1.size(-1) == x2.size(-1),
        lambda: f"X1 and X2 must have the same number of columns. X1: {x1.size(-1)} X2: {x2.size(-1)}",
    )
    check(
        utils.is_float_dtype(x1.dtype),
        lambda: "cdist only supports floating-point dtypes, X1 got: {x1.dtype}",
    )
    check(
        utils.is_float_dtype(x2.dtype),
        lambda: "cdist only supports floating-point dtypes, X2 got: {x2.dtype}",
    )
    check(p >= 0, lambda: "cdist only supports non-negative p values")
    check(
        compute_mode >= 0 and compute_mode <= 2,
        lambda: f"possible modes: 0, 1, 2, but was: {compute_mode}",
    )
    r1 = x1.size(-2)
    r2 = x2.size(-2)
    batch_tensor1 = x1.shape[:-2]
    batch_tensor2 = x2.shape[:-2]
    output_shape = list(torch.broadcast_shapes(batch_tensor1, batch_tensor2))
    output_shape.extend([r1, r2])
    return x1.new_empty(output_shape)


@torch.library.impl(meta_lib, "_embedding_bag")
def meta_embedding_bag(
    weight,
    indices,
    offsets,
    scale_grad_by_freq=False,
    mode=0,
    sparse=False,
    per_sample_weights=None,
    include_last_offset=False,
    padding_idx=-1,
):
    check(
        indices.dtype in (torch.long, torch.int),
        lambda: f"expected indices to be long or int, got {indices.dtype}",
    )
    check(
        offsets.dtype in (torch.long, torch.int),
        lambda: f"expected offsets to be long or int, got {offsets.dtype}",
    )
    check(
        utils.is_float_dtype(weight.dtype),
        lambda: f"expected weight to be floating point type, got {weight.dtype}",
    )

    num_bags = offsets.size(0)
    if include_last_offset:
        check(
            num_bags >= 1, lambda: "include_last_offset: numBags should be at least 1"
        )
        num_bags -= 1

    output = weight.new_empty(num_bags, weight.size(1))
    MODE_SUM, MODE_MEAN, MODE_MAX = range(3)

    if per_sample_weights is not None:
        check(
            mode == MODE_SUM,
            lambda: "embedding_bag: per_sample_weights only supported with mode='sum'",
        )
        check(
            per_sample_weights.dtype == weight.dtype,
            lambda: f"expected weight ({weight.dtype}) and per_sample_weights ({per_sample_weights.dtype}) to have same dtype",
        )
        check(
            per_sample_weights.ndim == 1,
            lambda: f"expected per_sample_weights to be 1D tensor, got {per_sample_weights.ndim}D",
        )
        check(
            per_sample_weights.numel() == indices.numel(),
            lambda: (
                f"expected per_sample_weights.numel() ({per_sample_weights.numel()} "
                f"to be the same as indices.numel() ({indices.numel()})"
            ),
        )

    def is_fast_path_index_select_scale(src, scale, output, padding_idx):
        return (
            is_fast_path_index_select(src, output, padding_idx) and scale.stride(0) == 1
        )

    def is_fast_path_index_select(src, output, padding_idx):
        return (
            (src.dtype == torch.float or src.dtype == torch.half)
            and src.stride(1) == 1
            and output.stride(1) == 1
            and padding_idx < 0
        )

    def is_fast_path(src, scale, output, padding_idx):
        if scale is not None:
            return is_fast_path_index_select_scale(src, scale, output, padding_idx)
        else:
            return is_fast_path_index_select(src, output, padding_idx)

    if offsets.device.type != "cpu":
        offset2bag = indices.new_empty(indices.size(0))
        bag_size = indices.new_empty(offsets.size())
        if mode == MODE_MAX:
            max_indices = indices.new_empty(num_bags, weight.size(1))
        else:
            max_indices = indices.new_empty(0)
    else:
        fast_path_sum = is_fast_path(weight, per_sample_weights, output, padding_idx)
        if mode == MODE_MEAN or mode == MODE_MAX or not fast_path_sum:
            offset2bag = offsets.new_empty(indices.size(0))
        else:
            offset2bag = offsets.new_empty(0)
        bag_size = offsets.new_empty(num_bags)
        max_indices = offsets.new_empty(bag_size.size())
    return output, offset2bag, bag_size, max_indices


@register_meta([aten.diag.default, aten.diag.out])
@out_wrapper()
def meta_diag(self, dim=0):
    check(self.dim() in (1, 2), lambda: "matrix or a vector expected")
    if self.dim() == 1:
        sz = self.size(0) + abs(dim)
        return self.new_empty((sz, sz))

    # case: dim is 2
    if dim >= 0:
        sz = min(self.size(0), self.size(1) - dim)
    else:
        sz = min(self.size(0) + dim, self.size(1))
    return self.new_empty((sz,))


@torch.library.impl(meta_lib, "_embedding_bag_forward_only")
def meta_embedding_bag_forward_only(weight, indices, offsets, *args):
    output, offset2bag, bag_size, max_indices = meta_embedding_bag(
        weight, indices, offsets, *args
    )
    if offsets.device.type == "cpu":
        bag_size = offsets.new_empty(offsets.size())
    return output, offset2bag, bag_size, max_indices


def _get_reduction_dtype(input, dtype, promote_int_to_long=True):
    # if specified, dtype takes precedence
    if dtype:
        return dtype

    if input.dtype.is_floating_point or input.dtype.is_complex:
        return input.dtype
    elif promote_int_to_long:
        return torch.long

    return input.dtype


@torch.library.impl(meta_lib, "nansum")
@torch.library.impl(meta_lib, "nansum.out")
@out_wrapper()
def meta_nansum(input, dims=None, keepdim=False, *, dtype=None):
    output_dtype = _get_reduction_dtype(input, dtype, promote_int_to_long=True)
    dims = utils.reduction_dims(input.shape, dims)
    output_shape = _compute_reduction_shape(input, dims, keepdim)
    return input.new_empty(output_shape, dtype=output_dtype)


@torch.library.impl(meta_lib, "nanmedian")
def meta_nanmedian(input):
    output_shape = utils.compute_reduction_output_shape(
        input.shape, tuple(range(input.dim()))
    )
    return input.new_empty(output_shape)


@torch.library.impl(meta_lib, "nanmedian.dim_values")
@torch.library.impl(meta_lib, "nanmedian.dim")
@out_wrapper("values", "indices")
def meta_nanmedian_dim(input, dim=-1, keepdim=False):
    dim = utils.reduction_dims(input.shape, (dim,))
    output_shape = _compute_reduction_shape(input, dim, keepdim)
    return input.new_empty(output_shape), input.new_empty(
        output_shape, dtype=torch.long
    )


@torch.library.impl(meta_lib, "nan_to_num")
def meta_nan_to_num(self, nan=None, posinf=None, neginf=None):
    return self.new_empty(self.shape)


@torch.library.impl(meta_lib, "remainder.Scalar_Tensor")
def meta_remainder_scalar(scalar, other):
    return other % scalar


@torch.library.impl(meta_lib, "logical_not_")
def meta_logical_not_(self):
    return self


# We must also trigger meta registrations from PrimTorch ref
# decompositions
import torch._refs
import torch._refs.nn.functional
import torch._refs.special
