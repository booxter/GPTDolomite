# Third Party
from transformers import DynamicCache
from transformers.modeling_flash_attention_utils import _flash_attention_forward
import torch

# Local
from ...enums import AttentionHeadType, PositionEmbeddingType
from ..position_embedding import apply_rotary_pos_emb
from .base import Attention


class FlashAttention2(Attention):
    def forward(
        self,
        hidden_states: torch.Tensor,
        past_key_values: DynamicCache | None = None,
        attention_mask: torch.Tensor | None = None,
        rope_cos_sin: torch.Tensor | None = None,
        cu_seqlens: torch.Tensor | None = None,
        max_seqlen: torch.Tensor | None = None,
    ) -> torch.Tensor:
        # ==========================================================================================
        # hidden_states -> (batch_size, query_length, num_heads * head_dim)
        # ==========================================================================================

        query, key, value = self._prepare_qkv_for_forward(hidden_states)

        # ==========================================================================================
        # query -> (batch_size, num_heads, query_length, head_dim)
        # key -> (batch_size, num_key_value_heads, query_length, head_dim)
        # value -> (batch_size, num_key_value_heads, query_length, head_dim)
        # ==========================================================================================

        if self.position_embedding_type == PositionEmbeddingType.rope:
            query = apply_rotary_pos_emb(query, rope_cos_sin)
            key = apply_rotary_pos_emb(key, rope_cos_sin)

        if past_key_values is not None:
            key, value = past_key_values.update(key, value, self.layer_idx)

        # ==========================================================================================
        # query -> (batch_size, num_heads, query_length, head_dim)
        # key -> (batch_size, num_key_value_heads, key_length, head_dim)
        # value -> (batch_size, num_key_value_heads, key_length, head_dim)
        # ==========================================================================================

        # TODO avoid this extra transpose
        query = query.transpose(1, 2)
        if self.attention_head_type == AttentionHeadType.mqa:
            key = key.squeeze(1).unsqueeze(2)
            value = value.squeeze(1).unsqueeze(2)
        else:
            key = key.transpose(1, 2)
            value = value.transpose(1, 2)

        # ==========================================================================================
        # query -> (batch_size, query_length, num_heads, head_dim)
        # key -> (batch_size, key_length, num_heads, head_dim)
        # value -> (batch_size, key_length, num_heads, head_dim)
        # ==========================================================================================

        softmax_scale = self._get_softmax_scale()
        dropout_p = self.attn_pdrop if self.training else 0

        batch_size, query_length = query.shape[:2]

        attn_output = _flash_attention_forward(
            query_states=query,
            key_states=key,
            value_states=value,
            attention_mask=attention_mask,
            query_length=query_length,
            is_causal=self.causal,
            dropout=dropout_p,
            softmax_scale=softmax_scale,
        )

        attn_output = attn_output.view(batch_size, query_length, -1)

        # ==========================================================================================
        # attn_output -> (batch_size, query_length, num_heads * head_dim)
        # ==========================================================================================

        attn_output = self.c_proj(attn_output)
        attn_output = self.resid_dropout(attn_output)

        return attn_output
