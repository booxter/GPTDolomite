# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Credit
# Tri Dao's Triton LayerNorm: https://github.com/Dao-AILab/flash-attention/blob/main/flash_attn/ops/triton/layer_norm.py
# Triton LayerNorm tutorial: https://triton-lang.org/main/getting-started/tutorials/05-layer-norm.html

"""Code taken from torchtitan: https://github.com/pytorch/torchtitan/blob/main/torchtitan/models/norms.py"""

# Standard
import math

# Third Party
import torch
import torch.nn as nn

# Local
from .....utils import is_triton_available

if is_triton_available():
    # Third Party
    import triton
    import triton.language as tl

    @triton.autotune(
        configs=[
            triton.Config({}, num_warps=1),
            triton.Config({}, num_warps=2),
            triton.Config({}, num_warps=4),
            triton.Config({}, num_warps=8),
            triton.Config({}, num_warps=16),
            triton.Config({}, num_warps=32),
        ],
        key=["N"],
    )
    @triton.jit
    def _rms_norm_fwd_kernel(
        X,
        stride_x,
        Y,
        stride_y,
        W,
        Rstd,
        eps,
        M,  # num rows
        N,  # num cols
        block_N: tl.constexpr,
    ):
        row = tl.program_id(0)
        cols = tl.arange(0, block_N)

        # Load input data and weights
        mask = cols < N
        x = tl.load(X + row * stride_x + cols, mask=mask, other=0.0).to(tl.float32)
        w = tl.load(W + cols, mask=mask, other=0.0).to(tl.float32)

        # Compute mean and variance
        xbar = tl.where(cols < N, x, 0.0)
        var = tl.sum(xbar * xbar, axis=0) / N
        rstd = 1 / tl.sqrt(var + eps)

        # Store the reciprocal standard deviation
        tl.store(Rstd + row, rstd)

        # Normalize and apply linear transformation
        x_hat = x * rstd
        y = x_hat * w

        # Write output
        tl.store(Y + row * stride_y + cols, y, mask=mask)

    @triton.autotune(
        configs=[
            triton.Config({}, num_warps=1),
            triton.Config({}, num_warps=2),
            triton.Config({}, num_warps=4),
            triton.Config({}, num_warps=8),
            triton.Config({}, num_warps=16),
            triton.Config({}, num_warps=32),
        ],
        key=["N"],
    )
    @triton.jit
    def _rms_norm_bwd_kernel_sm(
        X,
        stride_x,
        W,
        DY,
        stride_dy,
        DX,
        stride_dx,
        Rstd,
        DW,
        eps,
        M,  # num rows
        N,  # num cols
        rows_per_program,
        block_N: tl.constexpr,
    ):
        row_block_id = tl.program_id(0)
        row_start = row_block_id * rows_per_program
        cols = tl.arange(0, block_N)
        mask = cols < N

        # Load weights
        w = tl.load(W + cols, mask=mask, other=0.0).to(tl.float32)

        # Accumulate gradients for weights
        dw = tl.zeros((block_N,), dtype=tl.float32)

        row_end = min(row_start + rows_per_program, M)
        for row in range(row_start, row_end):
            # Load input, output gradient, and reciprocal standard deviation
            x = tl.load(X + row * stride_x + cols, mask=mask, other=0.0).to(tl.float32)
            dy = tl.load(DY + row * stride_dy + cols, mask=mask, other=0.0).to(
                tl.float32
            )
            rstd = tl.load(Rstd + row)

            # Compute normalized input and gradients
            x_hat = x * rstd
            wdy = w * dy
            dw += dy * x_hat
            c1 = tl.sum(x_hat * wdy, axis=0) / N
            dx = (wdy - x_hat * c1) * rstd

            # Store input gradient
            tl.store(DX + row * stride_dx + cols, dx, mask=mask)

        # Store weight gradients
        tl.store(DW + row_block_id * N + cols, dw, mask=mask)


class _TorchTitanRMSNorm(torch.autograd.Function):
    @staticmethod
    def forward(ctx, x: torch.Tensor, weight: torch.Tensor, eps: float) -> torch.Tensor:
        x_shape_start = x.shape

        # Flatten input
        x = x.view(-1, x.shape[-1])
        if x.stride(-1) != 1:
            x = x.contiguous()
        if weight.stride(-1) != 1:
            weight = weight.contiguous()

        M, N = x.shape
        y = torch.empty_like(x)
        rstd = torch.empty((M,), dtype=torch.float32, device=x.device)

        max_size = 65536 // x.element_size()
        block_N = min(max_size, triton.next_power_of_2(N))

        if N > block_N:
            raise ValueError(f"N {N} must be <= {block_N=}")

        grid = lambda meta: (M,)
        _rms_norm_fwd_kernel[grid](
            x, x.stride(0), y, y.stride(0), weight, rstd, eps, M, N, block_N
        )

        ctx.eps = eps
        ctx.save_for_backward(x, weight, rstd)
        ctx.x_shape_start = x_shape_start

        y = y.reshape(x_shape_start)
        return y

    @staticmethod
    def backward(ctx, dy: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, None]:
        x, weight, rstd = ctx.saved_tensors
        eps = ctx.eps
        x_shape_start = ctx.x_shape_start

        # Flatten input and output gradients
        dy = dy.view(-1, dy.shape[-1])
        if dy.stride(-1) != 1:
            dy = dy.contiguous()

        M, N = dy.shape
        dx = torch.empty_like(x)
        dw = torch.empty_like(weight)

        sm_count = torch.cuda.get_device_properties(x.device).multi_processor_count
        _dw = torch.empty((sm_count, N), dtype=torch.float32, device=weight.device)

        max_size = 65536 // x.element_size()
        block_N = min(max_size, triton.next_power_of_2(N))
        rows_per_sm = math.ceil(M / sm_count)

        if N > block_N:
            raise ValueError(f"N {N} must be <= {block_N=}")

        grid = lambda meta: (sm_count,)
        _rms_norm_bwd_kernel_sm[grid](
            x,
            x.stride(0),
            weight,
            dy,
            dy.stride(0),
            dx,
            dx.stride(0),
            rstd,
            _dw,
            eps,
            M,
            N,
            rows_per_sm,
            block_N,
        )
        dw = _dw.sum(0).to(weight.dtype)
        dx = dx.view(x_shape_start)
        return dx, dw, None


def torchtitan_rmsnorm(
    input: torch.Tensor, weight: torch.Tensor, eps: float
) -> torch.Tensor:
    return _TorchTitanRMSNorm.apply(input, weight, eps)


class TorchTitanRMSNorm(nn.RMSNorm):
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return torchtitan_rmsnorm(x, self.weight, self.eps)
